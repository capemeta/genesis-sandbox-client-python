"""原工作区准备回执校验；未知结果只能查询，不能隐式重放。"""

from __future__ import annotations

import re
from typing import Any

from .errors import ProtocolError


def preparation_request(operation_id: str, request_digest: str) -> dict[str, str]:
    if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", operation_id):
        raise ValueError("invalid preparation operation identity")
    if not isinstance(request_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", request_digest):
        raise ValueError("invalid preparation request digest")
    return {"operation_id": operation_id, "request_digest": request_digest}


def preparation_receipt(value: Any, session_id: str, operation_id: str, request_digest: str) -> dict[str, Any]:
    fields = {"operation_id", "request_digest", "status", "session_id", "workspace_id", "sandbox_id",
              "profile_revision", "tenant_id", "principal_id", "user_id", "facts"}
    if (not isinstance(value, dict) or set(value) - fields or fields - {"facts"} - set(value)
            or value.get("session_id") != session_id or value.get("operation_id") != operation_id
            or value.get("request_digest") != request_digest or value.get("status") not in {"prepared", "unknown"}):
        raise ProtocolError("preparation receipt does not match original request")
    if not isinstance(value["sandbox_id"], str):
        raise ProtocolError("preparation sandbox identity must be a string")
    for key in ("workspace_id", "tenant_id", "principal_id", "user_id", "profile_revision"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ProtocolError("preparation receipt lacks owner or resource identity")
    if value["status"] == "unknown":
        if "facts" in value:
            raise ProtocolError("unknown preparation cannot contain confirmed facts")
    else:
        facts = value.get("facts")
        fact_fields = {"session_id", "workspace_id", "profile_revision", "os", "arch", "image_digest",
                       "runtime_versions", "runtime_executables", "mechanisms", "resource_limits",
                       "network_mode", "view_state", "readonly_regions"}
        if (not isinstance(value.get("sandbox_id"), str) or not value["sandbox_id"]
                or not isinstance(value.get("profile_revision"), str) or not value["profile_revision"]
                or not isinstance(facts, dict) or facts.get("session_id") != session_id
                or facts.get("workspace_id") != value["workspace_id"]
                or facts.get("profile_revision") != value["profile_revision"]
                or set(facts) - fact_fields or facts.get("view_state") != "prepared"):
            raise ProtocolError("confirmed preparation lacks bound runtime facts")
        for key in ("os", "arch", "image_digest", "network_mode"):
            if not isinstance(facts.get(key), str) or not facts[key]:
                raise ProtocolError("preparation facts lack required runtime fields")
        for key in ("runtime_versions", "runtime_executables"):
            values = facts.get(key)
            if not isinstance(values, dict) or any(not isinstance(name, str) or not name
                    or not isinstance(item, str) or not item for name, item in values.items()):
                raise ProtocolError("preparation runtime facts have invalid values")
        limits = facts.get("resource_limits")
        if not isinstance(limits, dict) or any(not isinstance(name, str) or not name
                or type(item) is not int or not 1 <= item <= 2**53 - 1 for name, item in limits.items()):
            raise ProtocolError("preparation resource facts have invalid values")
        for key in ("mechanisms", "readonly_regions"):
            if key == "readonly_regions" and key not in facts:
                continue
            values = facts.get(key)
            if not isinstance(values, list) or any(not isinstance(item, str) or not item for item in values):
                raise ProtocolError("preparation mechanism facts have invalid values")
    return value
