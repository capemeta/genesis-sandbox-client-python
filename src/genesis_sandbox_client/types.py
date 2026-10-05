"""Common types and constants shared by both sync and async session helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .errors import ProtocolError

SESSION_TTL: int = 300
JOB_TIMEOUT: int = 60

# 内置心跳（类 Eureka Heartbeat）默认参数：
# RENEW_INTERVAL 必须 < 服务端 lease_timeout / 2，保证单次续期失败仍有重试余量。
RENEW_INTERVAL: int = 30
RENEW_EXTEND: int = 90


@dataclass
class SandboxOptions:
    """Configuration for a session-backed SandboxSession helper."""

    profile: str | None = None
    hints: list[str] | None = None
    strict: bool = False
    description: str | None = None
    resolution_id: str | None = None
    workspace_id: str | None = None
    # 默认执行环境变量：随每次 exec 请求下发（会话创建协议拒绝持久 env 字段）；
    # 与单次 run/run_async 的 env 合并，单次调用优先。
    env: dict[str, str] | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    ttl_seconds: int = SESSION_TTL
    workspace_retention: str | None = None
    workspace_ttl_seconds: int | None = None
    idempotency_key: str | None = None
    heartbeat: bool = True
    renew_interval: int = RENEW_INTERVAL
    renew_extend: int = RENEW_EXTEND

    def __post_init__(self) -> None:
        if self.profile and self.hints:
            raise ValueError("profile and hints are mutually exclusive")
        if self.resolution_id and (self.profile or self.hints):
            raise ValueError("resolution_id and environment selector are mutually exclusive")
        if self.ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if self.workspace_retention not in (None, "ttl", "explicit_delete"):
            raise ValueError("workspace_retention must be 'ttl' or 'explicit_delete'")
        if self.workspace_retention == "ttl" and not self.workspace_ttl_seconds:
            raise ValueError("workspace_ttl_seconds is required for ttl retention")
        if self.renew_interval < 0 or self.renew_extend < 0:
            raise ValueError("heartbeat intervals cannot be negative")


@dataclass
class EffectiveEnvironment:
    """Resolved environment information returned by the server.

    Present in responses to CreateSession, SubmitJob, ExecSession, and Resolve.
    Answers: what environment was selected, why, and whether it was degraded.
    """

    profile_name: str = ""
    profile_revision: str = ""
    selection_mode: str = ""  # "profile" or "auto"
    selection_reason: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    degraded: bool = False


@dataclass
class ExecResult:
    """Execution result from a session or job command."""

    exit_code: int
    stdout: str
    stderr: str
    error_code: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    effective_environment: EffectiveEnvironment | None = None

    def ok(self) -> bool:
        # 与 Go SDK 对齐：取消/超时等异常终止会置 error_code（如 EXEC_CANCELLED），
        # 即使 exit_code 为 0 也不是成功执行。
        return self.exit_code == 0 and not self.error_code

    def __repr__(self) -> str:
        preview = self.stdout[:60].replace("\n", "\\n")
        ellipsis = "…" if len(self.stdout) > 60 else ""
        return f"ExecResult(exit_code={self.exit_code}, error_code={self.error_code!r}, stdout={preview!r}{ellipsis})"


@dataclass(frozen=True)
class SSEEvent:
    """One Server-Sent Event from a Job or Session exec log stream."""

    id: str = ""
    event: str = "message"
    data: str = ""

    def json(self) -> Any:
        """Decode ``data`` as JSON."""
        return json.loads(self.data)


@dataclass(frozen=True)
class CancelReceipt:
    """取消回执：区分「接受」与「停止确认」。

    服务端 :cancel 在确认停止后才返回终态 ExecRecord（5 秒内联等待，超时以错误
    表达停止未确认）；本类型把回执映射为稳定 outcome：

    - ``"accepted"``：取消已被接受，但回执仍非终态，停止未确认。
    - ``"already_terminal"``：执行先于取消完成，以实际终态为准，迟到取消不覆盖。
    - ``"stop_confirmed"``：取消导致 cancelled/timed_out 终态，停止已确认。
    - ``"stop_unknown"``：终态没有明确物理停止证据；禁止回收或直接重放。
      传输失败另外抛 ``ExecRecoveryError(phase="cancel")``，进入核对流程。

    ``stop_confirmed`` 是资源回收安全门禁，只使用服务端明确布尔事实。
    """

    exec_id: str
    outcome: str
    status: str = ""
    stop_confirmed: bool = False
    record: dict[str, Any] | None = None

    @classmethod
    def from_record(cls, exec_id: str, record: dict[str, Any] | None) -> CancelReceipt:
        """Map a :cancel ExecRecord response onto the cancel outcome taxonomy."""
        if not isinstance(record, dict) or record.get("exec_id") != exec_id:
            raise ProtocolError("cancel receipt original execution identity mismatch")
        if "stop_confirmed" in record and not isinstance(record["stop_confirmed"], bool):
            raise ProtocolError("cancel receipt physical stop evidence must be boolean")
        status = str((record or {}).get("status") or "")
        stopped = (record or {}).get("stop_confirmed") is True
        if status not in {"queued", "running", "succeeded", "failed", "cancelled", "timed_out", "interrupted"}:
            raise ProtocolError("cancel receipt status is invalid")
        if stopped and status in {"queued", "running"}:
            raise ProtocolError("nonterminal execution cannot confirm physical stop")
        if status in ("cancelled", "timed_out"):
            return cls(exec_id, "stop_confirmed" if stopped else "stop_unknown", status, stopped, record)
        if status in ("succeeded", "failed"):
            return cls(exec_id, "already_terminal" if stopped else "stop_unknown", status, stopped, record)
        if status == "interrupted":
            return cls(exec_id, "already_terminal" if stopped else "stop_unknown", status, stopped, record)
        return cls(exec_id, "accepted", status, False, record)
