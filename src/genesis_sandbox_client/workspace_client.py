"""工作区资源、共享存储查询和视图控制；复用 Client 的认证与传输预算。"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote


def _binding(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) - {"mode", "storage_ref", "resource_id", "binding_version"}:
        raise ValueError("workspace_binding contains unknown fields")
    mode = payload.get("mode")
    if mode == "isolated":
        if set(payload) != {"mode"}:
            raise ValueError("isolated binding cannot reference shared storage")
    elif mode == "shared":
        if set(payload) != {"mode", "storage_ref", "resource_id", "binding_version"}:
            raise ValueError("shared binding requires resource identity and version")
        for key in ("storage_ref", "resource_id"):
            value = payload[key]
            if not isinstance(value, str) or not value or len(value) > 128 or any(
                not (char.isascii() and (char.isalnum() or char in "-_.")) for char in value
            ) or value in {".", ".."}:
                raise ValueError("shared binding requires opaque resource identity")
        version = payload["binding_version"]
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise ValueError("shared binding version must be positive")
    else:
        raise ValueError("invalid workspace binding mode")
    return dict(payload)


class WorkspaceClientMixin:
    def _request(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def create_workspace(self, workspace_id: str | None = None, metadata: dict[str, str] | None = None,
                         ttl_seconds: int | None = None, quota_mb: int | None = None,
                         workspace_binding: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if workspace_id:
            payload["workspace_id"] = workspace_id
        if metadata:
            payload["metadata"] = metadata
        if ttl_seconds:
            payload["ttl_seconds"] = int(ttl_seconds)
        if quota_mb:
            payload["quota_mb"] = int(quota_mb)
        if workspace_binding is not None:
            payload["workspace_binding"] = _binding(workspace_binding)
        return self._request("POST", "/v1/workspaces", payload)

    def get_workspace(self, workspace_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/workspaces/{quote(workspace_id, safe='')}")

    def delete_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        return self._request("DELETE", f"/v1/workspaces/{quote(workspace_id, safe='')}")

    def inspect_shared_storage_resource(self, storage_ref: str, resource_id: str) -> dict[str, Any]:
        _binding({"mode": "shared", "storage_ref": storage_ref, "resource_id": resource_id, "binding_version": 1})
        return self._request("GET", f"/v1/storage-resources/{quote(storage_ref, safe='')}/{quote(resource_id, safe='')}")

    def get_workspace_view(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/sessions/{quote(session_id, safe='')}/workspace-view")

    def get_session_history(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/sessions/{quote(session_id, safe='')}/history")

    def purge_session_workspace(self, session_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/sessions/{quote(session_id, safe='')}/workspace:purge")

    def prepare_workspace_view(self, session_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/sessions/{quote(session_id, safe='')}/workspace-view?action=prepare")

    def seal_workspace_view(self, session_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/sessions/{quote(session_id, safe='')}/workspace-view?action=seal")
