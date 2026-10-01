"""Asynchronous session-backed SandboxSession helper.

Usage (async context manager — preferred):
    async with new_sandbox_async(client, profile="python") as sb:
        result = await sb.run_python('print("hello")')
        print(result.stdout)

Usage (manual lifecycle):
    sb = await open_sandbox_async(client, profile="shell")
    try:
        result = await sb.run_shell("uname -a")
    finally:
        await sb.close()
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from .client import Client
from .errors import APIError, ExecRecoveryError, TransportError
from .session import _to_exec_result
from .types import (
    JOB_TIMEOUT,
    RENEW_EXTEND,
    RENEW_INTERVAL,
    SESSION_TTL,
    CancelReceipt,
    ExecResult,
    SandboxOptions,
    SSEEvent,
)

log = logging.getLogger(__name__)


class AsyncSandboxSession:
    """Async helper backed by ``/v1/sessions/{id}`` APIs.

    Includes a built-in background heartbeat (Eureka-style) implemented as an
    asyncio task that periodically renews the session. Call ``_start_heartbeat``
    once a running loop is available (done automatically by the open helpers).

    The SDK is transport-level sync (stdlib urllib) by design; async methods
    offload blocking I/O via ``asyncio.to_thread`` so the event loop stays
    responsive without pulling in third-party HTTP dependencies.
    """

    def __init__(self, client: Client, session: dict[str, Any], options: SandboxOptions) -> None:
        self._client = client
        self._session = session
        self._session_id = session["session_id"]
        self._sandbox_id = session.get("active_sandbox_id", "")
        self._workspace_id = session.get("workspace_id", "")
        self._opts = options
        self._closed = False
        self._operation_by_exec_id: dict[str, str] = {}

        self._renew_interval = options.renew_interval
        self._renew_extend = options.renew_extend
        self._heartbeat_enabled = options.heartbeat and self._renew_interval > 0 and self._renew_extend > 0
        self._renew_stop = asyncio.Event()
        self._renew_task: asyncio.Task[None] | None = None

    def _start_heartbeat(self) -> None:
        if self._heartbeat_enabled and self._renew_task is None:
            self._renew_task = asyncio.create_task(self._renew_loop(), name=f"sandbox-renew-{self._session_id[:8]}")

    async def _renew_loop(self) -> None:
        try:
            while not self._renew_stop.is_set():
                try:
                    await asyncio.wait_for(self._renew_stop.wait(), timeout=self._renew_interval)
                    return
                except TimeoutError:
                    pass
                try:
                    res = await asyncio.to_thread(
                        self._client.renew_session,
                        self._session_id,
                        self._renew_extend,
                        15,
                        1,
                    )
                    log.debug(
                        "Lease renewed: session=%s new_expires=%s",
                        self._session_id,
                        res.get("expires_at") if res else None,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("Renew failed (will retry): session=%s err=%s", self._session_id, e)
        except asyncio.CancelledError:
            return

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def sandbox_id(self) -> str:
        return self._sandbox_id

    @property
    def workspace_id(self) -> str:
        """Durable workspace identity that can outlive the current container."""
        return self._workspace_id

    @property
    def session(self) -> dict[str, Any]:
        return self._session

    # ─── Execution ─────────────────────────────────────────────────────

    async def run(
        self,
        code_or_cmd: str | list[str],
        *,
        lang: str | None = None,
        timeout: int | None = None,
        working_dir: str | None = None,
        env: dict[str, str] | None = None,
        operation_id: str | None = None,
    ) -> ExecResult:
        """General-purpose execution in the session sandbox.

        env 与 SandboxOptions.env 合并（单次调用优先）；operation_id 未传时本地生成
        uuid4，生产必须显式传入并持久保存。
        """
        kwargs: dict[str, Any] = {}
        if isinstance(code_or_cmd, list):
            kwargs["command"] = code_or_cmd
        else:
            kwargs["code"] = code_or_cmd
            if lang:
                kwargs["language"] = lang
        if working_dir:
            kwargs["working_dir"] = working_dir
        merged_env = {**(self._opts.env or {}), **(env or {})}
        if merged_env:
            kwargs["env"] = merged_env
        if timeout:
            kwargs["timeout_seconds"] = timeout
        operation_id = operation_id or uuid.uuid4().hex
        record = await self._submit_exec(kwargs, operation_id)
        exec_id = record.get("exec_id") if isinstance(record, dict) else None
        if not isinstance(exec_id, str) or not exec_id.strip():
            raise ExecRecoveryError(
                self._session_id,
                operation_id,
                phase="submit",
                cause=ValueError("async exec response did not include exec_id"),
            )
        self._operation_by_exec_id[exec_id] = operation_id
        try:
            while record.get("status") in {"queued", "running"}:
                await asyncio.sleep(0.2)
                record = await self._observe_exec(exec_id, operation_id)
        except asyncio.CancelledError:
            await self._cancel_remote(exec_id)
            raise
        self._record_execution(record)
        return _to_exec_result(record)

    async def run_python(self, code: str, timeout: int = JOB_TIMEOUT, *, operation_id: str | None = None) -> ExecResult:
        return await self.run(code, lang="python", timeout=timeout, operation_id=operation_id)

    async def run_shell(
        self, script: str, timeout: int = JOB_TIMEOUT, *, operation_id: str | None = None
    ) -> ExecResult:
        return await self.run(script, lang="shell", timeout=timeout, operation_id=operation_id)

    async def run_node(self, code: str, timeout: int = JOB_TIMEOUT, *, operation_id: str | None = None) -> ExecResult:
        return await self.run(code, lang="javascript", timeout=timeout, operation_id=operation_id)

    async def run_command(self, *command: str) -> ExecResult:
        return await self.run(list(command))

    # ─── Async Execution ───────────────────────────────────────────────

    async def run_async(
        self,
        code_or_cmd: str | list[str],
        *,
        lang: str | None = None,
        timeout: int | None = None,
        working_dir: str | None = None,
        env: dict[str, str] | None = None,
        operation_id: str | None = None,
    ) -> str:
        """Submit asynchronous execution; returns exec_id for polling.

        env 与 SandboxOptions.env 合并（单次调用优先）；operation_id 语义同 run。
        """
        kwargs: dict[str, Any] = {}
        if isinstance(code_or_cmd, list):
            kwargs["command"] = code_or_cmd
        else:
            kwargs["code"] = code_or_cmd
            if lang:
                kwargs["language"] = lang
        if working_dir:
            kwargs["working_dir"] = working_dir
        merged_env = {**(self._opts.env or {}), **(env or {})}
        if merged_env:
            kwargs["env"] = merged_env
        if timeout:
            kwargs["timeout_seconds"] = timeout
        operation_id = operation_id or uuid.uuid4().hex
        result = await self._submit_exec(kwargs, operation_id)
        exec_id = result.get("exec_id") if isinstance(result, dict) else None
        if not isinstance(exec_id, str) or not exec_id.strip():
            raise ExecRecoveryError(
                self._session_id,
                operation_id,
                phase="submit",
                cause=ValueError("async exec response did not include exec_id"),
            )
        self._operation_by_exec_id[exec_id] = operation_id
        return exec_id

    async def get_exec_by_operation(self, operation_id: str) -> dict[str, Any]:
        """Look up an ExecRecord using the stable identity returned in recovery errors."""
        if not operation_id:
            raise ValueError("operation_id is required")
        try:
            result = await self._observe_by_operation(operation_id)
        except ExecRecoveryError:
            raise
        exec_id = result.get("exec_id")
        if exec_id:
            self._operation_by_exec_id[exec_id] = operation_id
        return result

    async def wait_exec(
        self, exec_id: str, poll_interval: float = 1.0, max_wait: float = 300.0, *, operation_id: str | None = None
    ) -> ExecResult:
        """Poll get_exec until execution completes or max_wait is exceeded."""
        operation_id = operation_id or self._operation_by_exec_id.get(exec_id, "")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait
        while loop.time() < deadline:
            result = await self._observe_exec(exec_id, operation_id)
            operation_id = result.get("operation_id") or operation_id
            if operation_id:
                self._operation_by_exec_id[exec_id] = operation_id
            status = result.get("status", "")
            if status in ("succeeded", "failed", "cancelled", "timed_out", "interrupted"):
                self._record_execution(result)
                return _to_exec_result(result)
            await asyncio.sleep(poll_interval)
        raise TimeoutError(f"exec {exec_id} did not complete within {max_wait}s")

    async def wait_exec_by_operation(
        self, operation_id: str, poll_interval: float = 1.0, max_wait: float = 300.0
    ) -> ExecResult:
        """Resume polling after process/client state loss using the operation identity."""
        record = await self.get_exec_by_operation(operation_id)
        exec_id = record.get("exec_id")
        if not isinstance(exec_id, str) or not exec_id:
            raise ExecRecoveryError(self._session_id, operation_id, phase="lookup")
        if record.get("status") in ("succeeded", "failed", "cancelled", "timed_out", "interrupted"):
            self._record_execution(record)
            return _to_exec_result(record)
        return await self.wait_exec(exec_id, poll_interval, max_wait, operation_id=operation_id)

    async def cancel_exec(self, exec_id: str) -> dict[str, Any]:
        """Cancel a running async execution (raw ExecRecord; 语义见 Client.cancel_exec)."""
        return await asyncio.to_thread(self._client.cancel_exec, self._session_id, exec_id)

    async def cancel_exec_receipt(self, exec_id: str, *, operation_id: str | None = None) -> CancelReceipt:
        """取消并表达「接受 vs 停止确认」（CancelReceipt）。

        成功返回 outcome 为 accepted / already_terminal / stop_confirmed 的回执；
        取消失败或超时（停止未知）抛 ExecRecoveryError(phase="cancel")，携带
        operation_id 供核对，未确认停止前不得回收资源或重跑替代执行。
        """
        if not exec_id:
            raise ValueError("exec_id is required")
        operation_id = operation_id or self._operation_by_exec_id.get(exec_id, "")
        try:
            record = await asyncio.to_thread(self._client.cancel_exec, self._session_id, exec_id)
        except (APIError, TransportError) as error:
            raise ExecRecoveryError(
                self._session_id,
                operation_id,
                exec_id=exec_id,
                phase="cancel",
                cause=error,
            ) from error
        return CancelReceipt.from_record(exec_id, record)

    async def lookup_session(self, idempotency_key: str) -> dict[str, Any] | None:
        """按幂等键查回会话（会话创建响应丢失恢复），404 返回 None。见 Client.lookup_session。"""
        return await asyncio.to_thread(self._client.lookup_session, idempotency_key)

    async def stream_exec_logs(self, exec_id: str, cursor: int | str = 0) -> AsyncIterator[SSEEvent]:
        """异步惰性日志流，逐条产出 SSEEvent（底层为 Client.stream_exec_logs）。"""
        iterator = self._client.stream_exec_logs(self._session_id, exec_id, cursor=cursor)
        sentinel = object()
        try:
            while True:
                item = await asyncio.to_thread(next, iterator, sentinel)
                if item is sentinel:
                    return
                yield cast(SSEEvent, item)
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()

    async def read_exec_logs(
        self, exec_id: str, *, cursor: int | str | None = None, byte_budget: int, max_events: int
    ) -> dict[str, Any]:
        """按预算读取一页日志（events/next_cursor/exhausted/gap_detected）。见 Client.read_exec_logs。"""
        return await asyncio.to_thread(
            self._client.read_exec_logs,
            self._session_id,
            exec_id,
            cursor=cursor,
            byte_budget=byte_budget,
            max_events=max_events,
        )

    # ─── File Operations ───────────────────────────────────────────────

    async def _cancel_remote(self, exec_id: str) -> None:
        """协程被取消时的尽力远端取消（不抛错，避免覆盖 CancelledError）。

        停止未确认只记 error 日志——这是 stop unknown 的降级路径；需要严格
        「停止未知门禁」时使用 cancel_exec_receipt（抛 ExecRecoveryError）。
        """
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._client.cancel_exec, self._session_id, exec_id, timeout=10, max_retries=1),
                timeout=12,
            )
        except Exception as exc:
            log.error("Remote cancellation unconfirmed: exec=%s error_type=%s", exec_id, type(exc).__name__)

    async def _observe_exec(self, exec_id: str, operation_id: str) -> dict[str, Any]:
        return await self._observe_receipt(
            lambda: self._client.get_exec(self._session_id, exec_id),
            operation_id=operation_id,
            exec_id=exec_id,
            phase="observe",
        )

    async def _observe_by_operation(self, operation_id: str) -> dict[str, Any]:
        return await self._observe_receipt(
            lambda: self._client.get_exec_by_operation(self._session_id, operation_id),
            operation_id=operation_id,
            exec_id=None,
            phase="lookup",
        )

    async def _observe_receipt(self, request, *, operation_id: str, exec_id: str | None, phase: str) -> dict[str, Any]:
        retries = 2
        for attempt in range(retries + 1):
            try:
                return await asyncio.to_thread(request)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                retryable = isinstance(error, TransportError) or (isinstance(error, APIError) and error.retryable)
                if retryable and attempt < retries:
                    await asyncio.sleep(0.2 * (2**attempt))
                    continue
                if isinstance(error, APIError) and not error.retryable:
                    raise
                raise ExecRecoveryError(
                    self._session_id,
                    operation_id,
                    exec_id=exec_id,
                    phase=phase,
                    cause=error,
                ) from error
        raise AssertionError("unreachable observation retry state")

    async def _submit_exec(self, kwargs: dict[str, Any], operation_id: str) -> dict[str, Any]:
        # 同一个提交身份贯穿传输重试，取消协程不能留下无主远端执行。
        kwargs["operation_id"] = operation_id
        submit = asyncio.create_task(asyncio.to_thread(self._client.exec_session_async, self._session_id, **kwargs))
        try:
            return await asyncio.shield(submit)
        except asyncio.CancelledError:
            try:
                result = await asyncio.wait_for(asyncio.shield(submit), timeout=5)
                await self._cancel_remote(result["exec_id"])
            except Exception:
                # 提交响应丢失时按操作身份查回执，避免分页列表遗漏已启动执行。
                for _ in range(3):
                    try:
                        result = await asyncio.wait_for(
                            asyncio.to_thread(
                                self._client.get_exec_by_operation, self._session_id, kwargs["operation_id"]
                            ),
                            timeout=5,
                        )
                        await self._cancel_remote(result["exec_id"])
                        break
                    except Exception:
                        await asyncio.sleep(0.2)
                else:
                    log.error("Remote submission cancellation unconfirmed: operation=%s", kwargs["operation_id"])
            raise
        except APIError as error:
            if not error.retryable:
                raise
            raise ExecRecoveryError(
                self._session_id,
                operation_id,
                phase="submit",
                cause=error,
            ) from error
        except TransportError as error:
            raise ExecRecoveryError(
                self._session_id,
                operation_id,
                phase="submit",
                cause=error,
            ) from error

    async def write_file(
        self, path: str, content: bytes | str, *, if_match: str | None = None, create_only: bool = False
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._client.upload_session_file,
            self._session_id,
            path,
            content,
            if_match,
            create_only,
        )

    async def read_file(self, path: str) -> bytes:
        return await asyncio.to_thread(self._client.download_session_file, self._session_id, path)

    async def list_files(self, path: str = ".", recursive: bool = False, limit: int | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._client.list_session_files, self._session_id, path, recursive, limit)

    async def stat_file(self, path: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._client.stat_session_file, self._session_id, path)

    async def mkdir(self, path: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._client.mkdir_session_dir, self._session_id, path)

    async def remove(self, path: str, recursive: bool = False) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._client.remove_session_file, self._session_id, path, recursive)

    # ─── Lifecycle ─────────────────────────────────────────────────────

    async def suspend(self) -> dict[str, Any]:
        """Release the runtime while keeping the session and workspace."""
        session = await asyncio.to_thread(self._client.suspend_session, self._session_id)
        self._session = session or self._session
        self._sandbox_id = (session or {}).get("active_sandbox_id", "")
        return session

    async def resume(self) -> dict[str, Any]:
        """Restore the runtime while preserving the logical session and workspace."""
        session = await asyncio.to_thread(self._client.resume_session, self._session_id)
        self._session = session or self._session
        self._sandbox_id = (session or {}).get("active_sandbox_id", "")
        return session

    async def close(self) -> None:
        if self._closed:
            return
        self._renew_stop.set()
        if self._renew_task is not None:
            self._renew_task.cancel()
            try:
                await self._renew_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        log.info("Closing session: %s", self._session_id)
        await asyncio.to_thread(self._client.delete_session, self._session_id)
        self._closed = True

    # ─── Internal ──────────────────────────────────────────────────────

    def _record_execution(self, result: dict[str, Any] | None) -> None:
        if not result:
            return
        sandbox_id = result.get("sandbox_id") or ""
        if sandbox_id:
            self._sandbox_id = sandbox_id
            self._session["active_sandbox_id"] = sandbox_id


# ─── Factory Functions ─────────────────────────────────────────────────────


def _build_opts(
    profile: str | None,
    hints: list[str] | None,
    strict: bool,
    description: str | None,
    resolution_id: str | None,
    workspace_id: str | None,
    env: dict[str, str] | None,
    metadata: dict[str, str] | None,
    ttl_seconds: int,
    workspace_retention: str | None,
    workspace_ttl_seconds: int | None,
    idempotency_key: str | None,
    heartbeat: bool,
    renew_interval: int,
    renew_extend: int,
) -> SandboxOptions:
    return SandboxOptions(
        profile=profile,
        hints=hints,
        strict=strict,
        description=description,
        resolution_id=resolution_id,
        workspace_id=workspace_id,
        env=env,
        metadata=metadata or {},
        ttl_seconds=ttl_seconds,
        workspace_retention=workspace_retention,
        workspace_ttl_seconds=workspace_ttl_seconds,
        idempotency_key=idempotency_key,
        heartbeat=heartbeat,
        renew_interval=renew_interval,
        renew_extend=renew_extend,
    )


async def _create_session(client: Client, opts: SandboxOptions) -> dict[str, Any]:
    # 会话创建不接受持久环境变量（协议拒绝 env 字段）；opts.env 作为默认执行
    # 环境变量在 run/run_async 时合并下发。
    return await asyncio.to_thread(
        client.create_session,
        workspace_id=opts.workspace_id,
        profile=opts.profile,
        hints=opts.hints,
        strict=opts.strict,
        description=opts.description,
        resolution_id=opts.resolution_id,
        state_policy="session",
        ttl_seconds=opts.ttl_seconds,
        metadata=opts.metadata,
        workspace_retention=opts.workspace_retention,
        workspace_ttl_seconds=opts.workspace_ttl_seconds,
        idempotency_key=opts.idempotency_key,
    )


@asynccontextmanager
async def new_sandbox_async(
    client: Client,
    *,
    profile: str | None = None,
    hints: list[str] | None = None,
    strict: bool = False,
    description: str | None = None,
    resolution_id: str | None = None,
    workspace_id: str | None = None,
    env: dict[str, str] | None = None,
    metadata: dict[str, str] | None = None,
    ttl_seconds: int = SESSION_TTL,
    workspace_retention: str | None = None,
    workspace_ttl_seconds: int | None = None,
    idempotency_key: str | None = None,
    heartbeat: bool = True,
    renew_interval: int = RENEW_INTERVAL,
    renew_extend: int = RENEW_EXTEND,
) -> AsyncIterator[AsyncSandboxSession]:
    """Async context manager that creates and cleans up a session sandbox."""
    opts = _build_opts(
        profile,
        hints,
        strict,
        description,
        resolution_id,
        workspace_id,
        env,
        metadata,
        ttl_seconds,
        workspace_retention,
        workspace_ttl_seconds,
        idempotency_key,
        heartbeat,
        renew_interval,
        renew_extend,
    )
    session = await _create_session(client, opts)
    sb = AsyncSandboxSession(client, session, opts)
    sb._start_heartbeat()
    log.info(
        "AsyncSandboxSession opened: session=%s profile=%s heartbeat=%s", sb.session_id, profile, sb._heartbeat_enabled
    )
    try:
        yield sb
    finally:
        await sb.close()


async def open_sandbox_async(
    client: Client,
    *,
    profile: str | None = None,
    hints: list[str] | None = None,
    strict: bool = False,
    description: str | None = None,
    resolution_id: str | None = None,
    workspace_id: str | None = None,
    env: dict[str, str] | None = None,
    metadata: dict[str, str] | None = None,
    ttl_seconds: int = SESSION_TTL,
    workspace_retention: str | None = None,
    workspace_ttl_seconds: int | None = None,
    idempotency_key: str | None = None,
    heartbeat: bool = True,
    renew_interval: int = RENEW_INTERVAL,
    renew_extend: int = RENEW_EXTEND,
) -> AsyncSandboxSession:
    """Create an AsyncSandboxSession for manual lifecycle management."""
    opts = _build_opts(
        profile,
        hints,
        strict,
        description,
        resolution_id,
        workspace_id,
        env,
        metadata,
        ttl_seconds,
        workspace_retention,
        workspace_ttl_seconds,
        idempotency_key,
        heartbeat,
        renew_interval,
        renew_extend,
    )
    session = await _create_session(client, opts)
    sb = AsyncSandboxSession(client, session, opts)
    sb._start_heartbeat()
    log.info(
        "AsyncSandboxSession opened (manual): session=%s profile=%s heartbeat=%s",
        sb.session_id,
        profile,
        sb._heartbeat_enabled,
    )
    return sb
