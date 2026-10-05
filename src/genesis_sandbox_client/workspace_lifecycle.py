"""可信父任务的持续存储保活；不续期或恢复计算实例。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from .errors import ProtocolError


def _operation_id(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or value in {".", ".."} or any(
        not (char.isascii() and (char.isalnum() or char in "_.-")) for char in value
    ):
        raise ValueError("operation_id must be an opaque identifier")
    return value


def _integer(value: int, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside the integer budget")
    return value


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value):
        raise ValueError("lifecycle time must be an aware microsecond RFC3339 timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("lifecycle time must be timezone-aware")
    return parsed.astimezone(UTC)


def workspace_lifecycle_request_digest(payload: dict[str, Any]) -> str:
    """固定六字段规范化摘要，供独立消费者核对原持久意图。"""
    fields = {"expected_revision", "hold_seconds", "operation_id", "retention_seconds", "state", "terminal_at"}
    if not isinstance(payload, dict) or set(payload) - fields or not {"expected_revision", "operation_id",
        "state"} <= set(payload):
        raise ValueError("invalid lifecycle request fields")
    _operation_id(payload["operation_id"])
    _integer(payload["expected_revision"], "expected_revision", 0, 9007199254740990)
    hold = _integer(payload.get("hold_seconds", 0), "hold_seconds", 0, 86400)
    retention = _integer(payload.get("retention_seconds", 0), "retention_seconds", 0, 30 * 86400)
    terminal = payload.get("terminal_at")
    if not isinstance(payload["state"], str):
        raise ValueError("invalid lifecycle state")
    if payload["state"] == "terminal":
        if hold or not retention or terminal is None:
            raise ValueError("invalid terminal lifecycle")
    elif payload["state"] not in {"active", "paused"} or terminal is not None or retention:
        raise ValueError("invalid nonterminal lifecycle")
    if terminal is not None:
        parsed = _timestamp(terminal)
        terminal = parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    canonical = {
        "expected_revision": payload["expected_revision"], "hold_seconds": payload.get("hold_seconds", 0),
        "operation_id": payload["operation_id"], "retention_seconds": payload.get("retention_seconds", 0),
        "state": payload["state"], "terminal_at": terminal,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",",
        ":")).encode("utf-8")).hexdigest()


def _receipt(value: Any, workspace_id: str, operation_id: str) -> dict[str, Any]:
    keys = {"operation_id", "workspace_id", "revision", "state", "deadline", "terminal_at", "request_digest"}
    if not isinstance(value, dict) or set(value) - keys or not keys - {"terminal_at"} <= set(value):
        raise ProtocolError("invalid lifecycle receipt fields")
    if value["workspace_id"] != workspace_id or value["operation_id"] != operation_id:
        raise ProtocolError("lifecycle receipt identity mismatch")
    revision = value["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or not 1 <= revision <= 9007199254740991:
        raise ProtocolError("invalid lifecycle revision")
    digest = value["request_digest"]
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ProtocolError("invalid lifecycle request digest")
    if not isinstance(value["state"], str) or value["state"] not in {"active", "paused", "terminal"}:
        raise ProtocolError("invalid lifecycle state")
    try:
        deadline = _timestamp(value["deadline"])
        if deadline.tzinfo is None:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as error:
        raise ProtocolError("invalid lifecycle deadline") from error
    if (value["state"] == "terminal") != (value.get("terminal_at") is not None):
        raise ProtocolError("lifecycle terminal identity mismatch")
    if value["state"] == "terminal":
        try:
            terminal = _timestamp(value["terminal_at"])
            if terminal.tzinfo is None or terminal > deadline:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as error:
            raise ProtocolError("invalid lifecycle terminal time") from error
    return value


class WorkspaceLifecycleClientMixin:
    def _request(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def control_workspace_lifecycle(
        self, workspace_id: str, operation_id: str, expected_revision: int, state: str,
        *, hold_seconds: int = 0, terminal_at: datetime | None = None, retention_seconds: int = 0,
    ) -> dict[str, Any]:
        _operation_id(operation_id)
        _integer(expected_revision, "expected_revision", 0, 9007199254740990)
        _integer(hold_seconds, "hold_seconds", 0, 86400)
        _integer(retention_seconds, "retention_seconds", 0, 30 * 86400)
        payload: dict[str, Any] = {
            "operation_id": operation_id, "expected_revision": expected_revision, "state": state,
        }
        if state in {"active", "paused"}:
            if terminal_at is not None or retention_seconds:
                raise ValueError("non-terminal lifecycle cannot carry terminal retention")
            payload["hold_seconds"] = hold_seconds
        elif state == "terminal":
            if hold_seconds or retention_seconds == 0 or not isinstance(terminal_at,
                datetime) or terminal_at.tzinfo is None:
                raise ValueError("terminal lifecycle requires an aware terminal_at and bounded retention")
            payload["terminal_at"] = terminal_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
            payload["retention_seconds"] = retention_seconds
        else:
            raise ValueError("invalid lifecycle state")
        # 传输层变更请求仅发一次；未知副作用必须改用原 operation_id 查询。
        result = self._request("POST", f"/v1/workspaces/{quote(workspace_id, safe='')}/lifecycle", payload)
        result = _receipt(result, workspace_id, operation_id)
        digest_ok = result["request_digest"] == workspace_lifecycle_request_digest(payload)
        revision_ok = result["revision"] == expected_revision + 1
        if not digest_ok or not revision_ok or result["state"] != state:
            raise ProtocolError("lifecycle receipt differs from original intent")
        if state == "terminal":
            assert terminal_at is not None
            if (_timestamp(result["terminal_at"]) != terminal_at
                    or _timestamp(result["deadline"]) != terminal_at + timedelta(seconds=retention_seconds)):
                raise ProtocolError("lifecycle receipt differs from parent terminal time or retention")
        return result

    def lookup_workspace_lifecycle(self, workspace_id: str, operation_id: str) -> dict[str, Any]:
        _operation_id(operation_id)
        result = self._request(
            "GET", f"/v1/workspaces/{quote(workspace_id, safe='')}/lifecycle:lookup?operation_id={quote(operation_id,
                safe='')}",
        )
        if not isinstance(result, dict) or set(result) != {"state", "receipt"}:
            raise ProtocolError("invalid lifecycle lookup fields")
        if result["state"] == "found":
            _receipt(result["receipt"], workspace_id, operation_id)
        elif result["state"] not in {"unknown", "history_expired"} or result["receipt"] is not None:
            raise ProtocolError("invalid lifecycle lookup state")
        return result
