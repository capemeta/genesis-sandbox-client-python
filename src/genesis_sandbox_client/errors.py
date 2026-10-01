"""Typed exceptions exposed by the Genesis Sandbox Python SDK."""

from __future__ import annotations

import json
from typing import Any


class SandboxError(Exception):
    """Base class for all SDK-originated errors."""


class APIError(SandboxError):
    """Structured error returned by the Sandbox Service."""

    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        *,
        request_id: str = "",
        details: dict[str, Any] | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.request_id = request_id
        self.details = details or {}
        self.retry_after = retry_after
        super().__init__(
            f"sandbox API {error_code or status_code} "
            f"(status={status_code}, request_id={request_id or 'unknown'}): {message}"
        )

    @property
    def retryable(self) -> bool:
        value = self.details.get("retryable")
        if isinstance(value, bool):
            return value
        return self.status_code == 429 or self.status_code >= 500


class TransportError(SandboxError):
    """Network or protocol failure before a valid API response was received."""


class ProtocolError(TransportError):
    """服务端返回了无法按契约解析的响应（如非法 JSON）。

    继承 TransportError 以保持既有 except TransportError 调用方兼容；
    classify_error 将其归类为 "protocol" 而不是 "transport"。
    """


class ExecRecoveryError(SandboxError):
    """An exec may still be running; use its operation identity to resume observation.

    ``phase`` 语义（Adapter 依赖稳定取值）：
    - ``submit``：提交请求结果未知，远端执行可能已启动，须按 operation_id 查回执。
    - ``observe``：观察回执失败，执行状态未确认，保持原 operation_id 继续观察。
    - ``lookup``：按 operation_id 查回执失败，结果未知。
    - ``cancel``：取消后停止未确认（stop unknown）。终态与停止确认是两件事；
      未确认停止前不得回收资源或再次提交替代执行，须先按 operation_id 核对。
    """

    def __init__(
        self,
        session_id: str,
        operation_id: str,
        *,
        exec_id: str | None = None,
        phase: str,
        cause: BaseException | None = None,
    ) -> None:
        self.session_id = session_id
        self.operation_id = operation_id
        self.exec_id = exec_id
        self.phase = phase
        self.cause = cause
        identity = f"session_id={session_id}, operation_id={operation_id}"
        if exec_id:
            identity += f", exec_id={exec_id}"
        super().__init__(f"exec {phase} requires recovery ({identity})")


def classify_error(exc: BaseException) -> str:
    """把 SDK 异常映射为稳定分类字符串，供上层 Adapter 转换为通用契约错误码。

    返回值（枚举式字符串，取值长期稳定）：

    - ``"invalid_request"``：请求本身不合法，不可重试。
      APIError 4xx（401/403/404/409/412/429 除外）；本地参数校验 ValueError。
    - ``"unauthorized"``：APIError 401/403。
    - ``"not_found"``：APIError 404。
    - ``"conflict"``：APIError 409/412（含 operation/版本冲突）。
    - ``"rate_limited"``：APIError 429。
    - ``"transient"``：APIError 5xx；可按 retry_advice 有限重试观察类请求。
    - ``"transport"``：TransportError（网络/连接层失败，未获得有效 API 响应）。
    - ``"protocol"``：ProtocolError、JSON 解析失败（json.JSONDecodeError /
      UnicodeDecodeError），以及 APIError 3xx（重定向被拒绝，属意外响应）。
    - ``"unknown"``：其余未归类异常（含无 cause 的 ExecRecoveryError）。

    ExecRecoveryError 代表“结果未知、需按 operation_id 核对”，不是可直接映射的
    终态错误；此处按其 cause 递归分类，无 cause 时返回 "unknown"。
    """
    if isinstance(exc, APIError):
        status = exc.status_code
        if status in (401, 403):
            return "unauthorized"
        if status == 404:
            return "not_found"
        if status in (409, 412):
            return "conflict"
        if status == 429:
            return "rate_limited"
        if 400 <= status < 500:
            return "invalid_request"
        if 500 <= status < 600:
            return "transient"
        return "protocol"
    if isinstance(exc, ExecRecoveryError):
        return classify_error(exc.cause) if exc.cause is not None else "unknown"
    if isinstance(exc, ProtocolError):
        return "protocol"
    if isinstance(exc, TransportError):
        return "transport"
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
        return "protocol"
    if isinstance(exc, ValueError):
        return "invalid_request"
    return "unknown"
