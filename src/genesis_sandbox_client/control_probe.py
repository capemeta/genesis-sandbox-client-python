"""只读控制前提查询；不把Docker配置事实当成工作负载隔离验收。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .errors import ProtocolError


@dataclass(frozen=True)
class RuntimeControlCapabilities:
    available: bool
    runtime_kind: str
    supports_resource_limits: bool

    def __post_init__(self) -> None:
        if type(self.available) is not bool or type(self.supports_resource_limits) is not bool or not isinstance(self.runtime_kind, str) or self.runtime_kind not in {"docker", "runsc"}:
            raise ProtocolError("invalid runtime control facts")


def parse_control_capabilities(value: Any) -> RuntimeControlCapabilities:
    allowed = {"available", "runtime_kind", "default_runtime", "available_runtimes", "runtime_profiles",
               "supported_task_types", "supported_languages", "supported_state_policies", "queue",
               "queue_snapshots", "execd", "supports_runsc", "supports_network_none", "supports_resource_limits", "message"}
    required = {"available", "runtime_kind", "default_runtime", "available_runtimes", "queue", "execd",
                "supports_runsc", "supports_network_none", "supports_resource_limits"}
    if not isinstance(value, dict) or not required.issubset(value) or set(value) - allowed:
        raise ProtocolError("invalid runtime control capability fields")
    for field in ("available", "supports_runsc", "supports_network_none", "supports_resource_limits"):
        if type(value[field]) is not bool:
            raise ProtocolError("invalid runtime control boolean")
    if not isinstance(value["runtime_kind"], str) or value["runtime_kind"] not in {"docker", "runsc"} or not isinstance(value["default_runtime"], str):
        raise ProtocolError("invalid runtime control kind")
    for field in ("available_runtimes", "supported_task_types", "supported_languages", "supported_state_policies"):
        if field not in value:
            continue
        items = value[field]
        if items is None and not value["available"] and field == "available_runtimes":
            continue
        if not isinstance(items, list) or len(items) > 256 or any(not isinstance(item, str) or len(item) > 256 for item in items):
            raise ProtocolError("invalid runtime control list")
    if not isinstance(value["queue"], dict) or not isinstance(value["execd"], dict):
        raise ProtocolError("invalid runtime control components")
    if set(value["execd"]) != {"command", "session", "files", "metrics"} or any(type(item) is not bool for item in value["execd"].values()):
        raise ProtocolError("invalid runtime control execd declaration")
    # 明确只消费driver.Probe与store.Ping的前提事实；注册runtime与execd声明不是隔离证明。
    return RuntimeControlCapabilities(value["available"], value["runtime_kind"], value["supports_resource_limits"])


class RuntimeControlClientMixin:
    def _request(self, method: str, path: str, body: dict[str, Any] | None = None,
                 timeout: float | None = None, max_retries: int | None = None,
                 deadline_seconds: float | None = None) -> Any:
        raise NotImplementedError

    def get_runtime_control_capabilities(self, *, timeout_seconds: float = 10) -> RuntimeControlCapabilities:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise ValueError("control probe timeout must be finite and at most 30 seconds")
        # 即便是GET也只发送一次；整批readiness的截止时间不得被客户端重试累加。
        return parse_control_capabilities(self._request("GET", "/v1/sandbox/capabilities",
                                                       timeout=timeout_seconds, max_retries=1,
                                                       deadline_seconds=timeout_seconds))
