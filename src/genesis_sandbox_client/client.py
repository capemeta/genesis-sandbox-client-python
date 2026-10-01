"""Low-level HTTP client for the Genesis Sandbox service (stdlib only)."""

from __future__ import annotations

import email.message
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from typing import Any

from .errors import APIError, ProtocolError, TransportError
from .types import SSEEvent


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Bearer 身份只发往配置的服务，禁止重定向携带凭据。
        return None


def _urlopen(request, timeout=None):
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def _header_message(headers: dict[str, str]) -> email.message.Message:
    """dict 头转 email.message.Message（urllib.error.HTTPError 的类型要求）。"""
    message = email.message.Message()
    for key, value in headers.items():
        message[key] = value
    return message


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _validate_base_url(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base_url must be an absolute http(s) URL")
    # 与 Platform 端点约束一致：拒绝内嵌凭据/查询/片段，也拒绝携带路径前缀。
    if parsed.path not in {"", "/"}:
        raise ValueError("base_url must not contain a path")
    if parsed.scheme != "https" and parsed.hostname not in _LOOPBACK_HOSTS:
        raise ValueError("non-https base_url is only allowed for loopback hosts (localhost/127.0.0.1/::1)")
    return base_url.rstrip("/")


def _validate_token(token: str) -> str:
    """拒绝空白边缘与 CR/LF，避免 Authorization 头注入。"""
    if not isinstance(token, str) or not token or token.strip() != token or any(c in token for c in "\r\n"):
        raise ValueError("token must be a non-empty string without surrounding whitespace or CR/LF")
    return token


def _opaque_cursor_to_int(cursor: int | str | None, name: str = "cursor") -> int:
    """游标对外保持不透明字符串；此处仅内部换算为服务端整数游标。"""
    if cursor is None:
        return 0
    try:
        value = int(str(cursor).strip())
    except ValueError:
        raise ValueError(f"{name} is opaque; pass the next_cursor value returned by read_exec_logs") from None
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


_DEFAULT_TIMEOUT = object()


def _build_environment(
    profile: str | None = None,
    hints: list[str] | None = None,
    strict: bool = False,
    description: str | None = None,
    profile_revision: str | None = None,
) -> dict[str, Any] | None:
    """Build an environment selector dict from profile or hints.

    Args:
        profile: Explicit profile name.
        hints: List of required capabilities for automatic selection.
        strict: If True, fail when not all capabilities are satisfied (only with hints).
        description: Free-text description of the workload to assist auto-selection.
    """
    if profile and hints:
        raise ValueError("profile and hints are mutually exclusive")
    if profile_revision and not profile:
        raise ValueError("profile_revision requires profile")
    if profile:
        exact_profile: dict[str, Any] = {"name": profile}
        if profile_revision:
            exact_profile["revision"] = profile_revision
        return {"profile": exact_profile}
    if hints:
        h: dict[str, Any] = {"capabilities": hints}
        if strict:
            h["strict"] = True
        if description:
            h["description"] = description
        return {"hints": h}
    return None


def _validate_resolution(environment: dict[str, Any] | None, resolution_id: str | None) -> None:
    if environment and resolution_id:
        raise ValueError("resolution_id and environment selector are mutually exclusive")


class Client:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        timeout: float = 30,
        *,
        max_attempts: int = 3,
        retry_base_delay: float = 0.2,
        user_agent: str = "genesis-sandbox-client-python/1",
        token_provider: Callable[[], str] | None = None,
    ) -> None:
        """创建绑定服务端点的客户端。

        ``token_provider``：可选的凭据回调 ``Callable[[], str]``，在每次请求发出前
        取值（含每次重试），优先于固定 ``token``；返回空值时回退到固定 token。
        适用于短期凭据轮转。凭据只发往配置的 base_url，不随重定向转发。
        """
        self.base_url = _validate_base_url(base_url)
        if token is not None:
            token = _validate_token(token)
        if token_provider is not None and not callable(token_provider):
            raise ValueError("token_provider must be callable")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.token = token
        self.token_provider = token_provider
        self.timeout = timeout
        self.max_attempts = max(1, int(max_attempts))
        self.retry_base_delay = max(0.0, float(retry_base_delay))
        self.user_agent = user_agent

    def _auth_token(self) -> str | None:
        """每请求前解析凭据：token_provider 优先于固定 token。"""
        if self.token_provider is not None:
            provided = self.token_provider()
            if provided:
                return _validate_token(provided)
        return self.token

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        retry_safe: bool = False,
    ) -> Any:
        # 写操作重试纪律：仅当请求自带原生幂等身份（idempotency_key / operation_id）
        # 或调用方显式 retry_safe 时才有限重试；重放复用同一 payload，不会生成新
        # operation_id。无身份的写请求即使传入 max_retries 也只发一次。
        has_idempotency_key = isinstance(body, dict) and bool(body.get("idempotency_key") or body.get("operation_id"))
        if max_retries is None:
            max_attempts = (
                self.max_attempts
                if (self._is_idempotent(method) or (method.upper() == "POST" and has_idempotency_key) or retry_safe)
                else 1
            )
        else:
            max_attempts = max(1, int(max_retries))
        if (
            not self._is_idempotent(method)
            and not (method.upper() == "POST" and has_idempotency_key)
            and not retry_safe
        ):
            max_attempts = 1
        delay_seconds = self.retry_base_delay
        last_err: BaseException | None = None

        for attempt in range(max_attempts):
            url = f"{self.base_url}{path}"
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": self.user_agent,
            }
            token = self._auth_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"

            data = None
            if body is not None:
                data = json.dumps(body).encode("utf-8")

            req = urllib.request.Request(url, data=data, headers=headers, method=method)

            try:
                with _urlopen(req, timeout=self.timeout if timeout is None else timeout) as response:
                    if response.status == 204:
                        return None
                    raw = response.read((16 << 20) + 1)
                    if len(raw) > 16 << 20:
                        raise TransportError("sandbox JSON response exceeds limit")
                    res_body = raw.decode("utf-8")
                    if not res_body:
                        return None
                    try:
                        return json.loads(res_body)
                    except ValueError as exc:
                        raise ProtocolError(f"sandbox response is not valid JSON: {exc}") from exc
            except urllib.error.HTTPError as exc:
                last_err = exc
                if method.upper() == "DELETE" and exc.code == 404:
                    return None
                if self._is_transient(exc.code) and attempt < max_attempts - 1:
                    time.sleep(self._retry_after(exc, delay_seconds))
                    delay_seconds *= 2
                    continue
                raise self._api_error(exc) from None
            except ProtocolError:
                raise
            except Exception as exc:
                last_err = exc
                if attempt < max_attempts - 1:
                    time.sleep(delay_seconds)
                    delay_seconds *= 2
                    continue
                raise TransportError(f"sandbox request failed: {exc}") from exc
        raise TransportError(f"sandbox request failed after {max_attempts} attempts: {last_err}")

    def _raw_request(
        self,
        method: str,
        path: str,
        data: bytes | None = None,
        content_type: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: Any = _DEFAULT_TIMEOUT,
    ):
        max_attempts = self._attempts_for(method) if data is None else 1
        delay_seconds = self.retry_base_delay
        last_err: BaseException | None = None

        for attempt in range(max_attempts):
            url = f"{self.base_url}{path}"
            headers: dict[str, str] = {}
            if content_type:
                headers["Content-Type"] = content_type
            token = self._auth_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            headers["Accept"] = "application/json"
            headers["User-Agent"] = self.user_agent
            if extra_headers:
                headers.update(extra_headers)
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                request_timeout = self.timeout if timeout is _DEFAULT_TIMEOUT else timeout
                response = _urlopen(req, timeout=request_timeout)
                if response.status == 429 or 500 <= response.status <= 599:
                    response.close()
                    raise urllib.error.HTTPError(
                        url, response.status, "Transient status", _header_message(headers), None
                    )
                return response
            except urllib.error.HTTPError as exc:
                last_err = exc
                if self._is_transient(exc.code) and attempt < max_attempts - 1:
                    time.sleep(self._retry_after(exc, delay_seconds))
                    delay_seconds *= 2
                    continue
                raise self._api_error(exc) from None
            except Exception as exc:
                last_err = exc
                if attempt < max_attempts - 1:
                    time.sleep(delay_seconds)
                    delay_seconds *= 2
                    continue
                raise TransportError(f"sandbox raw request failed: {exc}") from exc
        raise TransportError(f"sandbox raw request failed after {max_attempts} attempts: {last_err}")

    def _attempts_for(self, method: str) -> int:
        return self.max_attempts if self._is_idempotent(method) else 1

    @staticmethod
    def _is_idempotent(method: str) -> bool:
        return method.upper() in {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}

    @staticmethod
    def _is_transient(status: int) -> bool:
        return status == 429 or status >= 500

    @staticmethod
    def _retry_after(exc: urllib.error.HTTPError, fallback: float) -> float:
        value = exc.headers.get("Retry-After") if exc.headers else None
        if value is None:
            return fallback
        try:
            return max(0.0, float(value))
        except ValueError:
            return fallback

    @staticmethod
    def _api_error(exc: urllib.error.HTTPError) -> APIError:
        try:
            raw = exc.read(65536).decode("utf-8", errors="replace")
            payload = json.loads(raw)
        except Exception:
            payload = {}
            raw = str(exc.reason)
        details = payload.get("details") if isinstance(payload, dict) else {}
        retry_after = Client._retry_after(exc, 0.0)
        return APIError(
            exc.code,
            payload.get("error_code", "") if isinstance(payload, dict) else "",
            payload.get("message", raw) if isinstance(payload, dict) else raw,
            request_id=payload.get("request_id", "") if isinstance(payload, dict) else "",
            details=details if isinstance(details, dict) else {},
            retry_after=retry_after or None,
        )

    def lease(
        self,
        workspace_id: str | None = None,
        profile: str | None = None,
        hints: list[str] | None = None,
        resolution_id: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if workspace_id:
            payload["workspace_id"] = workspace_id
        env = _build_environment(profile, hints)
        _validate_resolution(env, resolution_id)
        if env:
            payload["environment"] = env
        if resolution_id:
            payload["resolution_id"] = resolution_id
        if metadata:
            payload["metadata"] = metadata
        return self._request("POST", "/v1/sandboxes:lease", payload)

    def release(self, sandbox_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/sandboxes/{urllib.parse.quote(sandbox_id)}:release")

    def destroy(self, sandbox_id: str) -> dict[str, Any] | None:
        return self._request("DELETE", f"/v1/sandboxes/{urllib.parse.quote(sandbox_id)}")

    def list_sandboxes(self) -> dict[str, Any]:
        return self._request("GET", "/v1/sandboxes")

    def get_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/sandboxes/{urllib.parse.quote(sandbox_id)}")

    def renew(self, sandbox_id: str, extend_seconds: int) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/sandboxes/{urllib.parse.quote(sandbox_id)}:renew",
            {"extend_seconds": int(extend_seconds)},
        )

    def submit_job(
        self,
        *,
        code: str | None = None,
        command: list[str] | None = None,
        profile: str | None = None,
        hints: list[str] | None = None,
        strict: bool = False,
        description: str | None = None,
        resolution_id: str | None = None,
        timeout_seconds: int | None = None,
        queue_wait_timeout_seconds: int | None = None,
        workspace_id: str | None = None,
        session_id: str | None = None,
        env: dict[str, str] | None = None,
        metadata: dict[str, str] | None = None,
        idempotency_key: str | None = None,
        callback_url: str | None = None,
        input_artifact_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if bool(code) == bool(command):
            raise ValueError("exactly one of code or command is required")
        payload: dict[str, Any] = {}
        environment = _build_environment(profile, hints, strict=strict, description=description)
        _validate_resolution(environment, resolution_id)
        if environment:
            payload["environment"] = environment
        if resolution_id:
            payload["resolution_id"] = resolution_id
        if code:
            payload["code"] = code
        if command:
            payload["command"] = command
        if timeout_seconds:
            payload["timeout_seconds"] = int(timeout_seconds)
        if queue_wait_timeout_seconds:
            payload["queue_wait_timeout_seconds"] = int(queue_wait_timeout_seconds)
        if workspace_id:
            payload["workspace_id"] = workspace_id
        if session_id:
            payload["session_id"] = session_id
        if env:
            payload["env"] = env
        if metadata:
            payload["metadata"] = metadata
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        if callback_url:
            payload["callback_url"] = callback_url
        if input_artifact_ids:
            payload["input_artifact_ids"] = input_artifact_ids
        return self._request("POST", "/v1/jobs", payload)

    def wait_job(self, job_id: str, poll_interval: float = 0.2, max_wait: float = 300.0) -> dict[str, Any]:
        if poll_interval <= 0 or max_wait < 0:
            raise ValueError("poll_interval must be positive and max_wait cannot be negative")
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            result = self.get_job(job_id)
            if result.get("status") in {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}:
                return result
            time.sleep(poll_interval)
        raise TimeoutError(f"job {job_id} did not complete within {max_wait}s")

    def list_jobs(
        self, status: str | None = None, limit: int | None = None, offset: int | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if limit:
            params["limit"] = int(limit)
        if offset:
            params["offset"] = int(offset)
        query = urllib.parse.urlencode(params)
        return self._request("GET", "/v1/jobs" + (f"?{query}" if query else ""))

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/jobs/{urllib.parse.quote(job_id)}")

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/jobs/{urllib.parse.quote(job_id)}:cancel")

    def upload_job_file(self, job_id: str, name: str, content: bytes | str) -> dict[str, Any]:
        if isinstance(content, str):
            content = content.encode("utf-8")
        path = f"/v1/jobs/{urllib.parse.quote(job_id)}/files?name={urllib.parse.quote(name)}"
        with self._raw_request("POST", path, data=content, content_type="application/octet-stream") as response:
            return json.loads(response.read().decode("utf-8"))

    def download_artifact(self, artifact_id: str) -> bytes:
        path = f"/v1/artifacts/{urllib.parse.quote(artifact_id)}"
        with self._raw_request("GET", path) as response:
            return response.read()

    def list_job_artifacts(self, job_id: str, offset: int | None = None, limit: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if offset:
            params["offset"] = int(offset)
        if limit:
            params["limit"] = int(limit)
        query = urllib.parse.urlencode(params)
        path = f"/v1/jobs/{urllib.parse.quote(job_id)}/artifacts"
        if query:
            path += f"?{query}"
        return self._request("GET", path)

    def job_logs(self, job_id: str, cursor: int | str = 0) -> Iterator[SSEEvent]:
        path = f"/v1/jobs/{urllib.parse.quote(job_id)}/logs"
        if cursor:
            path += f"?cursor={int(cursor)}"
        return self._event_stream(path)

    def exec_session(
        self, sandbox_id: str, command: list[str] | None = None, code: str | None = None, language: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if command:
            payload["command"] = command
        if code:
            payload["code"] = code
        if language:
            payload["language"] = language
        return self._request("POST", f"/v1/sandboxes/{urllib.parse.quote(sandbox_id)}/sessions", payload)

    def create_workspace(
        self,
        workspace_id: str | None = None,
        metadata: dict[str, str] | None = None,
        ttl_seconds: int | None = None,
        quota_mb: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if workspace_id:
            payload["workspace_id"] = workspace_id
        if metadata:
            payload["metadata"] = metadata
        if ttl_seconds:
            payload["ttl_seconds"] = int(ttl_seconds)
        if quota_mb:
            payload["quota_mb"] = int(quota_mb)
        return self._request("POST", "/v1/workspaces", payload)

    def get_workspace(self, workspace_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/workspaces/{urllib.parse.quote(workspace_id)}")

    def delete_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        return self._request("DELETE", f"/v1/workspaces/{urllib.parse.quote(workspace_id)}")

    def create_session(
        self,
        workspace_id: str | None = None,
        profile: str | None = None,
        hints: list[str] | None = None,
        strict: bool = False,
        description: str | None = None,
        resolution_id: str | None = None,
        state_policy: str | None = None,
        ttl_seconds: int | None = None,
        metadata: dict[str, str] | None = None,
        workspace_retention: str | None = None,
        workspace_ttl_seconds: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if workspace_id:
            payload["workspace_id"] = workspace_id
        environment = _build_environment(profile, hints, strict=strict, description=description)
        _validate_resolution(environment, resolution_id)
        if environment:
            payload["environment"] = environment
        if resolution_id:
            payload["resolution_id"] = resolution_id
        if state_policy:
            payload["state_policy"] = state_policy
        if ttl_seconds:
            payload["ttl_seconds"] = int(ttl_seconds)
        if metadata:
            payload["metadata"] = metadata
        if workspace_retention:
            payload["workspace_retention"] = workspace_retention
        if workspace_ttl_seconds:
            payload["workspace_ttl_seconds"] = int(workspace_ttl_seconds)
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        return self._request("POST", "/v1/sessions", payload)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/sessions/{urllib.parse.quote(session_id)}")

    def lookup_session(self, idempotency_key: str) -> dict[str, Any] | None:
        """按幂等键查回 Session（会话创建响应丢失后的恢复路径）。

        ``GET /v1/sessions:lookup?idempotency_key=...``：命中返回 Session dict；
        404 返回 None（与 get_exec_by_operation 的 APIError 风格不同——lookup 的
        404 是正常的“未找到”结果，调用方据此决定是否在去重窗口内原样重提）。
        其余错误仍抛 APIError / TransportError。

        注意：服务端 404 无法区分“从未创建”与“去重/回执保留期已过”，无法证明
        未执行过；是否可原样重提须遵守 Provider 的去重保留窗口（契约 §8）。
        """
        if not idempotency_key or not str(idempotency_key).strip():
            raise ValueError("idempotency_key is required")
        try:
            return self._request(
                "GET", f"/v1/sessions:lookup?idempotency_key={urllib.parse.quote(str(idempotency_key))}"
            )
        except APIError as exc:
            if exc.status_code == 404:
                return None
            raise

    def delete_session(self, session_id: str) -> dict[str, Any] | None:
        return self._request("DELETE", f"/v1/sessions/{urllib.parse.quote(session_id)}")

    def suspend_session(self, session_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/sessions/{urllib.parse.quote(session_id)}:suspend")

    def resume_session(self, session_id: str) -> dict[str, Any]:
        # 服务端按 Session 锁串行恢复，重复请求复用已激活的 runtime。
        return self._request("POST", f"/v1/sessions/{urllib.parse.quote(session_id)}:resume", retry_safe=True)

    def renew_session(
        self, session_id: str, extend_seconds: int, timeout: float | None = None, max_retries: int = 1
    ) -> dict[str, Any]:
        # renew 是无原生幂等身份的 POST：按写重试纪律单次发送（显式调大 max_retries
        # 也会被钳制）；重试节奏由调用方（如会话心跳循环）负责。
        return self._request(
            "POST",
            f"/v1/sessions/{urllib.parse.quote(session_id)}:renew",
            {"extend_seconds": int(extend_seconds)},
            timeout=timeout,
            max_retries=max_retries,
        )

    def exec_named_session(
        self,
        session_id: str,
        command: list[str] | None = None,
        code: str | None = None,
        language: str | None = None,
        working_dir: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        timeout: float | None = None,
        operation_id: str | None = None,
        subprocess_policy: str | None = None,
    ) -> dict[str, Any]:
        """同步执行（阻塞至回执）。

        ``operation_id`` 是本次执行的原生幂等身份：传输重试始终复用同一个值。
        未显式传入时本地生成 uuid4——进程在拿到回执前崩溃则该身份丢失，无法
        lookup 恢复、重复提交也可能绕过去重。**生产必须显式传入并持久保存
        operation_id**，默认生成仅用于交互式/可丢弃场景。
        """
        if bool(code) == bool(command):
            raise ValueError("exactly one of code or command is required")
        payload: dict[str, Any] = {"operation_id": operation_id or uuid.uuid4().hex}
        if subprocess_policy:
            if subprocess_policy not in {"allow", "deny"}:
                raise ValueError("invalid subprocess_policy")
            payload["subprocess_policy"] = subprocess_policy
        if command:
            payload["command"] = command
        if code:
            payload["code"] = code
        if language:
            payload["language"] = language
        if working_dir:
            payload["working_dir"] = working_dir
        if env:
            payload["env"] = env
        if timeout_seconds:
            payload["timeout_seconds"] = int(timeout_seconds)
        return self._request(
            "POST",
            f"/v1/sessions/{urllib.parse.quote(session_id)}/exec",
            payload,
            timeout=timeout if timeout is not None else max(self.timeout, (timeout_seconds or 180) + 15),
        )

    def exec_session_async(
        self,
        session_id: str,
        command: list[str] | None = None,
        code: str | None = None,
        language: str | None = None,
        working_dir: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        callback_url: str | None = None,
        operation_id: str | None = None,
        subprocess_policy: str | None = None,
    ) -> dict[str, Any]:
        """异步执行提交，返回含 exec_id 的回执。

        ``operation_id`` 语义同 exec_named_session：重放复用同一身份；未显式传入时
        本地生成 uuid4，进程崩溃后无法 lookup。**生产必须显式传入并持久保存
        operation_id**，响应丢失时用 get_exec_by_operation 按同一身份查回执。
        """
        if bool(code) == bool(command):
            raise ValueError("exactly one of code or command is required")
        payload: dict[str, Any] = {"operation_id": operation_id or uuid.uuid4().hex}
        if subprocess_policy:
            if subprocess_policy not in {"allow", "deny"}:
                raise ValueError("invalid subprocess_policy")
            payload["subprocess_policy"] = subprocess_policy
        if command:
            payload["command"] = command
        if code:
            payload["code"] = code
        if language:
            payload["language"] = language
        if working_dir:
            payload["working_dir"] = working_dir
        if env:
            payload["env"] = env
        if timeout_seconds:
            payload["timeout_seconds"] = int(timeout_seconds)
        if callback_url:
            payload["callback_url"] = callback_url
        return self._request("POST", f"/v1/sessions/{urllib.parse.quote(session_id)}/exec:async", payload)

    def get_exec(self, session_id: str, exec_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/v1/sessions/{urllib.parse.quote(session_id)}/execs/{urllib.parse.quote(exec_id)}"
        )

    def get_exec_by_operation(self, session_id: str, operation_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/sessions/{urllib.parse.quote(session_id)}/execs:lookup?operation_id={urllib.parse.quote(operation_id)}",
        )

    def cancel_exec(
        self, session_id: str, exec_id: str, timeout: float | None = None, max_retries: int = 1
    ) -> dict[str, Any]:
        """请求取消执行，返回 ExecRecord 原始回执。

        取消语义（接受 vs 停止确认）：服务端在确认停止后才返回终态 ExecRecord
        （内联等待至多 5 秒），超时未确认停止以错误表达。因此：

        - 成功返回终态 record = 停止已确认（或执行先完成、以实际终态为准）；
        - 抛错（含 "execution stop was not confirmed"、网络失败、客户端超时）
          = 停止未知，调用方不得当作已停止。

        需要稳定 outcome 分类时用 SandboxSession.cancel_exec_receipt /
        CancelReceipt.from_record；停止未知会以 ExecRecoveryError(phase="cancel")
        抛出。取消不做盲重试（max_retries 默认 1）。
        """
        return self._request(
            "POST",
            f"/v1/sessions/{urllib.parse.quote(session_id)}/execs/{urllib.parse.quote(exec_id)}:cancel",
            timeout=timeout,
            max_retries=max_retries,
        )

    def stream_exec_logs(self, session_id: str, exec_id: str, cursor: int | str = 0) -> Iterator[SSEEvent]:
        """SSE 惰性生成器，逐条产出 SSEEvent。

        ``cursor`` 对外应视为不透明字符串（服务端为整数游标，业务层不得解析），
        仅接受 read_exec_logs 返回的 next_cursor 或 0/None 表示从头读取。
        """
        path = f"/v1/sessions/{urllib.parse.quote(session_id)}/execs/{urllib.parse.quote(exec_id)}/logs"
        if cursor:
            path += f"?cursor={int(cursor)}"
        return self._event_stream(path)

    def read_exec_logs(
        self, session_id: str, exec_id: str, *, cursor: int | str | None = None, byte_budget: int, max_events: int
    ) -> dict[str, Any]:
        """按预算读取一页 exec 日志，返回 dict。

        返回结构::

            {
                "events": [{"cursor": str, "stream": str, "data": str, "bytes": int}, ...],
                "next_cursor": str,   # 不透明游标；继续读取时原样回传
                "exhausted": bool,    # True=流已自然读尽；False=预算耗尽，仍有尾部
                "gap_detected": bool, # True=检测到游标不连续（保留/截断缺口）
            }

        预算语义：``byte_budget`` 按事件 data 的 UTF-8 字节数计，``max_events`` 按
        事件条数计，任一耗尽即返回且 ``exhausted=False``；至少返回一个事件以保证
        单条超预算时不空转。超预算的事件不消费、留待下页（next_cursor 不越过它）。

        缺口语义：事件游标按执行内连续编号；与上一游标不连续即 ``gap_detected``
        为 True（显式标记，不静默跳过），缺失事件不可补齐。

        ``exhausted`` 只表达“日志流读尽”，与执行是否终态相互独立：看到 status
        事件不代表尾部日志已读完，反之亦然（执行状态用 get_exec 表达）。

        对仍在运行的执行，预算未耗尽且暂无新事件时会阻塞等待；需要流式消费或
        自行控制等待时用 stream_exec_logs。
        """
        if not isinstance(byte_budget, int) or isinstance(byte_budget, bool) or byte_budget < 1:
            raise ValueError("byte_budget must be a positive integer")
        if not isinstance(max_events, int) or isinstance(max_events, bool) or max_events < 1:
            raise ValueError("max_events must be a positive integer")
        start = _opaque_cursor_to_int(cursor)
        events: list[dict[str, Any]] = []
        bytes_read = 0
        last_cursor = start
        expected = start + 1
        gap_detected = False
        exhausted = True
        for ev in self.stream_exec_logs(session_id, exec_id, cursor=start):
            try:
                ev_cursor = _opaque_cursor_to_int(ev.id, name="log event cursor")
            except ValueError as exc:
                # 服务端每条日志事件必须携带游标 id；缺失/非法属协议破坏。
                raise ProtocolError(str(exc)) from exc
            size = len(ev.data.encode("utf-8"))
            if events and (bytes_read + size > byte_budget or len(events) >= max_events):
                # 超预算事件不消费：服务端从 next_cursor 之后继续重放，无丢失。
                exhausted = False
                break
            if ev_cursor != expected:
                gap_detected = True
            expected = ev_cursor + 1
            last_cursor = ev_cursor
            bytes_read += size
            events.append({"cursor": str(ev_cursor), "stream": ev.event, "data": ev.data, "bytes": size})
        return {
            "events": events,
            "next_cursor": str(last_cursor),
            "exhausted": exhausted,
            "gap_detected": gap_detected,
        }

    def _event_stream(self, path: str) -> Iterator[SSEEvent]:
        response = self._raw_request(
            "GET",
            path,
            extra_headers={"Accept": "text/event-stream"},
            timeout=None,
        )
        try:
            event_id = ""
            event_type = "message"
            data_lines: list[str] = []
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if line == "":
                    if data_lines:
                        yield SSEEvent(event_id, event_type, "\n".join(data_lines))
                    event_id, event_type, data_lines = "", "message", []
                elif line.startswith("id:"):
                    event_id = line[3:].lstrip()
                elif line.startswith("event:"):
                    event_type = line[6:].lstrip()
                elif line.startswith("data:"):
                    value = line[5:]
                    data_lines.append(value[1:] if value.startswith(" ") else value)
            if data_lines:
                yield SSEEvent(event_id, event_type, "\n".join(data_lines))
        finally:
            response.close()

    def get_session_context(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/sessions/{urllib.parse.quote(session_id)}/context")

    def patch_session_context(self, session_id: str, cwd: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if cwd:
            payload["cwd"] = cwd
        return self._request("PATCH", f"/v1/sessions/{urllib.parse.quote(session_id)}/context", payload)

    def get_catalog(
        self, capability: str | None = None, tag: str | None = None, limit: int | None = None, offset: int | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if capability:
            params["capability"] = capability
        if tag:
            params["tag"] = tag
        if limit is not None:
            params["limit"] = int(limit)
        if offset is not None:
            params["offset"] = int(offset)
        query = urllib.parse.urlencode(params)
        return self._request("GET", "/v1/environment/catalog" + (f"?{query}" if query else ""))

    def resolve_environment(
        self,
        profile: str | None = None,
        hints: list[str] | None = None,
        strict: bool = False,
        description: str | None = None,
        ttl_seconds: int | None = None,
        profile_revision: str | None = None,
        include_facts: bool = False,
    ) -> dict[str, Any]:
        environment = _build_environment(
            profile, hints, strict=strict, description=description, profile_revision=profile_revision
        )
        if environment is None:
            environment = {"hints": {}}
        payload: dict[str, Any] = {"environment": environment}
        if include_facts:
            payload["include_facts"] = True
        if ttl_seconds is not None:
            if int(ttl_seconds) < 60:
                raise ValueError("ttl_seconds must be at least 60")
            payload["ttl_seconds"] = int(ttl_seconds)
        return self._request("POST", "/v1/environment:resolve", payload)

    def upload_session_file(
        self, session_id: str, path: str, content: bytes | str, if_match: str | None = None, create_only: bool = False
    ) -> dict[str, Any]:
        if isinstance(content, str):
            content = content.encode("utf-8")
        target = f"/v1/sessions/{urllib.parse.quote(session_id)}/files?path={urllib.parse.quote(path, safe='')}"
        extra_headers: dict[str, str] = {}
        if if_match:
            extra_headers["If-Match"] = if_match
        if create_only:
            extra_headers["If-None-Match"] = "*"
        with self._raw_request(
            "PUT",
            target,
            data=content,
            content_type="application/octet-stream",
            extra_headers=extra_headers or None,
        ) as response:
            return json.loads(response.read().decode("utf-8"))

    def download_session_file(self, session_id: str, path: str, *, max_bytes: int = 2 * 1024 * 1024) -> bytes:
        """在读取源头限制文件预算；超限拒绝，不返回不完整的成功内容。"""
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= 16 * 1024 * 1024:
            raise ValueError("max_bytes must be between 1 and 16777216")
        target = (
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/files?path={urllib.parse.quote(path, safe='')}"
        )
        with self._raw_request("GET", target) as response:
            content = response.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise ProtocolError("session file exceeds the download byte budget")
        return content

    def list_session_files(
        self, session_id: str, path: str = ".", recursive: bool = False, limit: int | None = None, offset: int = 0
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"path": path, "recursive": str(bool(recursive)).lower(), "offset": int(offset)}
        if limit is not None:
            params["limit"] = int(limit)
        query = urllib.parse.urlencode(params)
        return self._request("GET", f"/v1/sessions/{urllib.parse.quote(session_id)}/files:list?{query}")

    def stat_session_file(self, session_id: str, path: str) -> dict[str, Any]:
        target = f"/v1/sessions/{urllib.parse.quote(session_id)}/files:stat?path={urllib.parse.quote(path, safe='')}"
        return self._request("GET", target)

    def mkdir_session_dir(self, session_id: str, path: str) -> dict[str, Any]:
        target = f"/v1/sessions/{urllib.parse.quote(session_id)}/dirs?path={urllib.parse.quote(path, safe='')}"
        return self._request("POST", target)

    def remove_session_file(
        self, session_id: str, path: str, recursive: bool = False, if_match: str | None = None
    ) -> dict[str, Any] | None:
        params = urllib.parse.urlencode({"path": path, "recursive": str(bool(recursive)).lower()})
        with self._raw_request(
            "DELETE",
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/files?{params}",
            extra_headers={"If-Match": if_match} if if_match else None,
        ) as response:
            data = response.read(16385)
        if len(data) > 16384:
            raise ProtocolError("delete receipt exceeds byte budget")
        return json.loads(data) if data else None

    def get_workspace_view(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/workspace-view")

    def get_session_history(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/history")

    def purge_session_workspace(self, session_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/workspace:purge")

    def prepare_workspace_view(self, session_id: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/workspace-view?action=prepare"
        )

    def seal_workspace_view(self, session_id: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/workspace-view?action=seal"
        )

    def build_dependencies(
        self,
        *,
        resolution_id: str | None = None,
        environment: dict[str, Any] | None = None,
        language: str | None = None,
        manifest: str | None = None,
        lockfile: str | None = None,
        packages: list[str] | None = None,
    ) -> dict[str, Any]:
        if (resolution_id is None) == (environment is None):
            raise ValueError("exactly one of resolution_id or environment is required")
        if environment is not None:
            profile = environment.get("profile") if isinstance(environment, dict) else None
            if (
                not isinstance(profile, dict)
                or not profile.get("name")
                or not profile.get("revision")
                or environment.get("hints") is not None
            ):
                raise ValueError("environment must contain an exact profile name and revision")
        payload: dict[str, Any] = {}
        if resolution_id:
            payload["resolution_id"] = resolution_id
        if environment:
            payload["environment"] = environment
        if language:
            payload["language"] = language
        if manifest:
            payload["manifest"] = manifest
        if lockfile:
            payload["lockfile"] = lockfile
        if packages:
            payload["packages"] = packages
        return self._request("POST", "/v1/dependencies:build", payload)

    def get_dependency_build(self, fingerprint: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/dependencies/{urllib.parse.quote(fingerprint)}")

    def start_gui(
        self,
        sandbox_id: str,
        kind: str | None = None,
        resolution: str | None = None,
        ttl_seconds: int | None = None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if kind:
            payload["kind"] = kind
        if resolution:
            payload["resolution"] = resolution
        if ttl_seconds:
            payload["ttl_seconds"] = ttl_seconds
        if metadata:
            payload["metadata"] = metadata
        return self._request("POST", f"/v1/sandboxes/{urllib.parse.quote(sandbox_id)}/gui:start", payload)

    def stop_gui(self, sandbox_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/sandboxes/{urllib.parse.quote(sandbox_id)}/gui:stop")

    def get_viewer(self, sandbox_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/sandboxes/{urllib.parse.quote(sandbox_id)}/viewer")

    def wait_viewer(
        self, sandbox_id: str, kind: str | None = None, timeout: float = 30, poll_interval: float = 0.5
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last_error: BaseException | None = None
        while time.monotonic() < deadline:
            try:
                viewer = self.get_viewer(sandbox_id)
                if viewer and viewer.get("ready") and (not kind or viewer.get("kind") == kind):
                    return viewer
            except Exception as exc:
                last_error = exc
            time.sleep(poll_interval)
        raise TimeoutError(f"viewer not ready before timeout; last_error={last_error}")

    def patch_sandbox(self, sandbox_id: str, metadata: dict[str, str], resource_version: str) -> dict[str, Any]:
        payload = {"metadata": metadata, "resource_version": resource_version}
        return self._request("PATCH", f"/v1/sandboxes/{urllib.parse.quote(sandbox_id)}/metadata", payload)
