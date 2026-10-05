"""原删除回执只承认完整身份，不把 404 或目录缺失当作成功。"""

from typing import Any

from .errors import ProtocolError


def session_ownership(value: Any, session_id: str) -> dict[str, Any]:
    fields = {"session_id", "workspace_id", "tenant_id", "principal_id", "user_id", "profile_revision"}
    if (not isinstance(value, dict) or set(value) != fields
            or any(not isinstance(value[key], str) or not value[key] for key in fields)
            or value["session_id"] != session_id):
        raise ProtocolError("original session ownership identity mismatch")
    return value


def purge_receipt(value: Any, session_id: str | None, operation_id: str, request_digest: str) -> dict[str, Any]:
    fields = {"operation_id", "request_digest", "status", "session_id", "workspace_id",
              "profile_revision", "tenant_id", "principal_id", "user_id"}
    if (not isinstance(value, dict) or set(value) != fields
            or any(not isinstance(value[key], str) or not value[key] for key in fields)
            or value["status"] not in {"unknown", "purged"}
            or (session_id is not None and value["session_id"] != session_id) or value["operation_id"] != operation_id
            or value["request_digest"] != request_digest):
        raise ProtocolError("original workspace purge receipt does not match the request")
    return value
