"""Synchronous session-backed SandboxSession helper.

Usage (context manager — preferred):
    with new_sandbox(client, profile="python") as sb:
        result = sb.run_python('print("hello")')
        print(result.stdout)

Usage (manual lifecycle):
    sb = new_sandbox(client, profile="shell")
    try:
        result = sb.run_shell("uname -a")
    finally:
        sb.close()
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

from .client import Client
from .errors import APIError, ExecRecoveryError, TransportError
from .types import (
    JOB_TIMEOUT,
    RENEW_EXTEND,
    RENEW_INTERVAL,
    SESSION_TTL,
    CancelReceipt,
    EffectiveEnvironment,
    ExecResult,
    SandboxOptions,
    SSEEvent,
)

log = logging.getLogger(__name__)


class SandboxSession:
    """Synchronous helper backed by ``/v1/sessions/{id}`` APIs.

    Includes a built-in background heartbeat (Eureka-style) that periodically
    renews the session so long-running work is not reclaimed by the server's
    Lease Reaper. The heartbeat is a daemon thread stopped on ``close()``.
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
        self._lock = threading.Lock()

        self._renew_interval = options.renew_interval
        self._renew_extend = options.renew_extend
        self._renew_stop = threading.Event()
        self._renew_thread: threading.Thread | None = None
        self._heartbeat_error: Exception | None = None
        if options.heartbeat and self._renew_interval > 0 and self._renew_extend > 0:
            self._renew_thread = threading.Thread(
                target=self._renew_loop,
                name=f"sandbox-renew-{self._session_id[:8]}",
                daemon=True,
            )
            self._renew_thread.start()

        log.info(
            "SandboxSession opened: session=%s sandbox=%s profile=%s expires=%s heartbeat=%s",
            self._session_id,
            self._sandbox_id,
            options.profile,
            session.get("expires_at"),
            self._renew_thread is not None,
        )

    def _renew_loop(self) -> None:
        while not self._renew_stop.wait(self._renew_interval):
            try:
                res = self._client.renew_session(self._session_id, self._renew_extend, timeout=15, max_retries=1)
                log.debug(
                    "Lease renewed: session=%s new_expires=%s",
                    self._session_id,
                    res.get("expires_at") if res else None,
                )
            except Exception as e:  # noqa: BLE001
                self._heartbeat_error = e
                self._renew_stop.set()
                log.warning("Heartbeat suspended; inspect original session before renewing: session=%s",
                    self._session_id)
                return

    @property
    def heartbeat_error(self) -> Exception | None:
        return self._heartbeat_error

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

    def __enter__(self) -> SandboxSession:
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ─── Execution ─────────────────────────────────────────────────────

    def run(
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

        Args:
            code_or_cmd: Source code string or command list.
            lang: Language hint (python, shell, javascript) when code_or_cmd is code.
            timeout: Execution timeout in seconds.
            working_dir: Working directory override.
            env: Extra environment variables（与 SandboxOptions.env 合并，单次调用优先）。
            operation_id: 原生幂等身份。未传时本地生成 uuid4；生产必须显式传入并
                持久保存，否则进程崩溃后无法按 operation_id 恢复查询。
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
        kwargs["operation_id"] = operation_id
        try:
            result = self._client.exec_named_session(self._session_id, **kwargs)
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
        self._record_execution(result)
        return _to_exec_result(result)

    def run_python(self, code: str, timeout: int = JOB_TIMEOUT, *, operation_id: str | None = None) -> ExecResult:
        return self.run(code, lang="python", timeout=timeout, operation_id=operation_id)

    def run_shell(self, script: str, timeout: int = JOB_TIMEOUT, *, operation_id: str | None = None) -> ExecResult:
        return self.run(script, lang="shell", timeout=timeout, operation_id=operation_id)

    def run_node(self, code: str, timeout: int = JOB_TIMEOUT, *, operation_id: str | None = None) -> ExecResult:
        return self.run(code, lang="javascript", timeout=timeout, operation_id=operation_id)

    def run_command(self, *command: str) -> ExecResult:
        return self.run(list(command))

    # ─── Async Execution ───────────────────────────────────────────────

    def run_async(
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

        operation_id 语义同 run：生产必须显式传入并持久保存。
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
        kwargs["operation_id"] = operation_id
        try:
            result = self._client.exec_session_async(self._session_id, **kwargs)
        except (APIError, TransportError) as error:
            raise ExecRecoveryError(
                self._session_id,
                operation_id,
                phase="submit",
                cause=error,
            ) from error
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

    def list_execs(self, *, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        return self._client.list_session_execs(self._session_id, limit=limit, cursor=cursor)

    def get_exec_by_operation(self, operation_id: str) -> dict[str, Any]:
        """Look up an ExecRecord by its durable Session-scoped operation identity."""
        if not operation_id:
            raise ValueError("operation_id is required")
        try:
            result = self._client.get_exec_by_operation(self._session_id, operation_id)
        except Exception as error:
            raise ExecRecoveryError(
                self._session_id,
                operation_id,
                phase="lookup",
                cause=error,
            ) from error
        exec_id = result.get("exec_id")
        if exec_id:
            self._operation_by_exec_id[exec_id] = operation_id
        return result

    def wait_exec(
        self, exec_id: str, poll_interval: float = 1.0, max_wait: float = 300.0, *, operation_id: str | None = None
    ) -> ExecResult:
        """Poll get_exec until execution completes or max_wait is exceeded."""
        operation_id = operation_id or self._operation_by_exec_id.get(exec_id, "")
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            result = self._observe_exec(exec_id, operation_id)
            operation_id = result.get("operation_id") or operation_id
            if operation_id:
                self._operation_by_exec_id[exec_id] = operation_id
            status = result.get("status", "")
            if status in ("succeeded", "failed", "cancelled", "timed_out", "interrupted"):
                self._record_execution(result)
                return _to_exec_result(result)
            time.sleep(poll_interval)
        raise TimeoutError(f"exec {exec_id} did not complete within {max_wait}s")

    def wait_exec_by_operation(
        self, operation_id: str, poll_interval: float = 1.0, max_wait: float = 300.0
    ) -> ExecResult:
        """按 operation_id 恢复等待（进程/客户端状态丢失后的恢复路径）。"""
        record = self.get_exec_by_operation(operation_id)
        exec_id = record.get("exec_id")
        if not isinstance(exec_id, str) or not exec_id:
            raise ExecRecoveryError(self._session_id, operation_id, phase="lookup")
        if record.get("status") in ("succeeded", "failed", "cancelled", "timed_out", "interrupted"):
            self._record_execution(record)
            return _to_exec_result(record)
        return self.wait_exec(exec_id, poll_interval, max_wait, operation_id=operation_id)

    def _observe_exec(self, exec_id: str, operation_id: str) -> dict[str, Any]:
        retries = 2
        for attempt in range(retries + 1):
            try:
                return self._client.get_exec(self._session_id, exec_id)
            except Exception as error:
                retryable = isinstance(error, TransportError) or (isinstance(error, APIError) and error.retryable)
                if retryable and attempt < retries:
                    time.sleep(0.2 * (2**attempt))
                    continue
                if isinstance(error, APIError) and not error.retryable:
                    raise
                raise ExecRecoveryError(
                    self._session_id,
                    operation_id,
                    exec_id=exec_id,
                    phase="observe",
                    cause=error,
                ) from error
        raise AssertionError("unreachable observation retry state")

    def cancel_exec(self, exec_id: str) -> dict[str, Any]:
        """Cancel a running async execution (raw ExecRecord; 语义见 Client.cancel_exec)."""
        return self._client.cancel_exec(self._session_id, exec_id)

    def cancel_exec_receipt(self, exec_id: str, *, operation_id: str | None = None) -> CancelReceipt:
        """取消并表达「接受 vs 停止确认」（CancelReceipt）。

        成功返回 outcome 为 accepted / already_terminal / stop_confirmed 的回执；
        取消失败或超时（停止未知）抛 ExecRecoveryError(phase="cancel")，携带
        operation_id 供核对，未确认停止前不得回收资源或重跑替代执行。
        """
        if not exec_id:
            raise ValueError("exec_id is required")
        operation_id = operation_id or self._operation_by_exec_id.get(exec_id, "")
        try:
            record = self._client.cancel_exec(self._session_id, exec_id)
        except (APIError, TransportError) as error:
            raise ExecRecoveryError(
                self._session_id,
                operation_id,
                exec_id=exec_id,
                phase="cancel",
                cause=error,
            ) from error
        return CancelReceipt.from_record(exec_id, record)

    def lookup_session(self, idempotency_key: str) -> dict[str, Any] | None:
        """按幂等键查回会话（会话创建响应丢失恢复），404 返回 None。见 Client.lookup_session。"""
        return self._client.lookup_session(idempotency_key)

    def stream_exec_logs(self, exec_id: str, cursor: int | str = 0) -> Iterator[SSEEvent]:
        """惰性 SSE 日志流（SSEEvent 迭代器）；cursor 见 Client.stream_exec_logs。"""
        return self._client.stream_exec_logs(self._session_id, exec_id, cursor=cursor)

    def read_exec_logs(
        self, exec_id: str, *, cursor: int | str | None = None, byte_budget: int, max_events: int
    ) -> dict[str, Any]:
        """按预算读取一页日志（events/next_cursor/exhausted/gap_detected）。见 Client.read_exec_logs。"""
        return self._client.read_exec_logs(
            self._session_id, exec_id, cursor=cursor, byte_budget=byte_budget, max_events=max_events
        )

    # ─── File Operations ───────────────────────────────────────────────

    def write_file(
        self, path: str, content: bytes | str, *, if_match: str | None = None, create_only: bool = False
    ) -> dict[str, Any]:
        return self._client.upload_session_file(
            self._session_id, path, content, if_match=if_match, create_only=create_only
        )

    def read_file(self, path: str) -> bytes:
        return self._client.download_session_file(self._session_id, path)

    def list_files(self, path: str = ".", recursive: bool = False, limit: int | None = None) -> dict[str, Any]:
        return self._client.list_session_files(self._session_id, path=path, recursive=recursive, limit=limit)

    def stat_file(self, path: str) -> dict[str, Any]:
        return self._client.stat_session_file(self._session_id, path)

    def mkdir(self, path: str) -> dict[str, Any]:
        return self._client.mkdir_session_dir(self._session_id, path)

    def remove(self, path: str, recursive: bool = False) -> dict[str, Any] | None:
        return self._client.remove_session_file(self._session_id, path, recursive=recursive)

    # ─── Lifecycle ─────────────────────────────────────────────────────

    def suspend(self) -> dict[str, Any]:
        """Release the runtime while keeping the session and workspace."""
        session = self._client.suspend_session(self._session_id)
        self._session = session or self._session
        self._sandbox_id = (session or {}).get("active_sandbox_id", "")
        return session

    def resume(self) -> dict[str, Any]:
        """Restore the runtime while preserving the logical session and workspace."""
        session = self._client.resume_session(self._session_id)
        self._session = session or self._session
        self._sandbox_id = (session or {}).get("active_sandbox_id", "")
        return session

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
        self._renew_stop.set()
        if self._renew_thread is not None:
            self._renew_thread.join(timeout=20.0)
        log.info("Closing session: %s", self._session_id)
        self._client.delete_session(self._session_id)
        with self._lock:
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


def new_sandbox(
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
) -> SandboxSession:
    """Create a session-backed SandboxSession helper with built-in heartbeat."""
    opts = SandboxOptions(
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
    # 会话创建不接受持久环境变量（协议拒绝 env 字段）；opts.env 作为默认执行
    # 环境变量在 run/run_async 时合并下发。
    session = client.create_session(
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
    return SandboxSession(client, session, opts)


# ─── Layer 3: Quick Helpers (Job mode, no session) ─────────────────────────


def quick_run(client: Client, command: list[str], **kwargs: Any) -> ExecResult:
    """One-shot Job execution (no session). Returns ExecResult."""
    submitted = client.submit_job(command=command, **kwargs)
    result = client.wait_job(submitted["job_id"])
    return _to_exec_result(result)


def quick_python(client: Client, code: str, **kwargs: Any) -> ExecResult:
    """One-shot Python Job execution (no session, auto profile=python)."""
    kwargs.setdefault("profile", "python")
    submitted = client.submit_job(code=code, **kwargs)
    result = client.wait_job(submitted["job_id"])
    return _to_exec_result(result)


# ─── Helpers ───────────────────────────────────────────────────────────────


def _parse_effective_environment(data: dict[str, Any] | None) -> EffectiveEnvironment | None:
    """Parse effective_environment from a response dict."""
    if not data:
        return None
    ee = data.get("effective_environment")
    if not ee or not isinstance(ee, dict):
        return None
    return EffectiveEnvironment(
        profile_name=ee.get("profile_name", ""),
        profile_revision=ee.get("profile_revision", ""),
        selection_mode=ee.get("selection_mode", ""),
        selection_reason=ee.get("selection_reason") or [],
        capabilities=ee.get("capabilities") or [],
        degraded=ee.get("degraded", False),
    )


def _to_exec_result(result: dict[str, Any] | None) -> ExecResult:
    if not result:
        return ExecResult(exit_code=-1, stdout="", stderr="", error_code="no_response")
    return ExecResult(
        exit_code=result.get("exit_code", -1),
        stdout=result.get("stdout", ""),
        stderr=result.get("stderr", ""),
        error_code=result.get("error_code", ""),
        stdout_truncated=result.get("stdout_truncated", False),
        stderr_truncated=result.get("stderr_truncated", False),
        effective_environment=_parse_effective_environment(result),
    )
