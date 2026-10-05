"""工作区资源、共享存储查询和视图控制；复用 Client 的认证与传输预算。"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from .errors import APIError, ProtocolError
from .execution_governance import maintenance_proof, maintenance_query
from .json_codec import loads as decode_response_json
from .view_preparation import preparation_receipt, preparation_request
from .workspace_purge import purge_receipt, session_ownership


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
        if isinstance(version, bool) or not isinstance(version, int) or not 0 < version <= 9007199254740991:
            raise ValueError("shared binding version must be a positive safe integer")
    else:
        raise ValueError("invalid workspace binding mode")
    return dict(payload)


class WorkspaceClientMixin:
    def query_workspace_maintenance_proof(self, workspace_id: str, *, context: dict[str, Any],
                                          operation_id: str, source_identity: str,
                                          claim_digest: str) -> dict[str, Any]:
        query = maintenance_query(workspace_id, context, operation_id, source_identity, claim_digest)
        result = self._request("POST", f"/v1/workspaces/{quote(workspace_id, safe='')}/maintenance-proof:query", query)
        return maintenance_proof(result, query)

    def stat_session_file(self, session_id: str, path: str) -> dict[str, Any]:
        target = f"/v1/sessions/{quote(session_id, safe='')}/files:stat?path={quote(path, safe='')}"
        return self._request("GET", target)

    def mkdir_session_dir(self, session_id: str, path: str) -> dict[str, Any]:
        target = f"/v1/sessions/{quote(session_id, safe='')}/dirs?path={quote(path, safe='')}"
        return self._request("POST", target)

    def _request(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _raw_request(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def set_session_file_executable(self, session_id: str, path: str, executable: bool,
                                    if_match: str | None = None, expected_executable: bool | None = None) -> dict[str,
                                        Any]:
        if not isinstance(executable, bool) or (
            expected_executable is not None and not isinstance(expected_executable, bool)
        ):
            raise ValueError("executable permissions must be booleans")
        payload: dict[str, Any] = {"executable": executable}
        if expected_executable is not None:
            payload["expected_executable"] = expected_executable
        target = f"/v1/sessions/{quote(session_id, safe='')}/files:executable?path={quote(path, safe='')}"
        with self._raw_request("PATCH", target, data=json.dumps(payload).encode("utf-8"),
            content_type="application/json",
                               extra_headers={"If-Match": if_match} if if_match else None) as response:
            receipt = response.read(16385)
        if len(receipt) > 16384:
            raise ProtocolError("executable permission receipt exceeds byte budget")
        result = decode_response_json(receipt)
        if not isinstance(result, dict) or not isinstance(result.get("executable"), bool):
            raise ProtocolError("executable permission receipt must contain an executable fact")
        return result

    def create_workspace(self, workspace_id: str | None = None, metadata: dict[str, str] | None = None,
                         ttl_seconds: int | None = None, quota_mb: int | None = None,
                         workspace_binding: dict[str, Any] | None = None,
                         retention_mode: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if workspace_id:
            payload["workspace_id"] = workspace_id
        if metadata:
            payload["metadata"] = metadata
        if ttl_seconds:
            payload["ttl_seconds"] = int(ttl_seconds)
        if quota_mb:
            payload["quota_mb"] = int(quota_mb)
        if retention_mode is not None:
            if retention_mode not in {"ttl", "explicit_delete"}:
                raise ValueError("invalid workspace retention mode")
            payload["retention_mode"] = retention_mode
        if workspace_binding is not None:
            payload["workspace_binding"] = _binding(workspace_binding)
        return self._request("POST", "/v1/workspaces", payload)

    def get_workspace(self, workspace_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/workspaces/{quote(workspace_id, safe='')}")

    def delete_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        return self._request("DELETE", f"/v1/workspaces/{quote(workspace_id, safe='')}")

    def inspect_shared_storage_resource(self, storage_ref: str, resource_id: str) -> dict[str, Any]:
        _binding({"mode": "shared", "storage_ref": storage_ref, "resource_id": resource_id, "binding_version": 1})
        return self._request("GET", f"/v1/storage-resources/{quote(storage_ref, safe='')}/{quote(resource_id,
            safe='')}")

    def get_workspace_view(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/sessions/{quote(session_id, safe='')}/workspace-view")

    def get_session_history(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/sessions/{quote(session_id, safe='')}/history")

    def get_session_ownership(self, session_id: str) -> dict[str, Any]:
        return session_ownership(self._request("GET", f"/v1/sessions/{quote(session_id, safe='')}/ownership"),
            session_id)

    def purge_session_workspace(self, session_id: str, *, operation_id: str, request_digest: str) -> dict[str, Any]:
        payload = preparation_request(operation_id, request_digest)
        result = self._request("POST", f"/v1/sessions/{quote(session_id, safe='')}/workspace:purge", payload)
        return purge_receipt(result, session_id, operation_id, request_digest)

    def lookup_workspace_purge(self, session_id: str, *, operation_id: str, request_digest: str) -> dict[str,
        Any] | None:
        preparation_request(operation_id, request_digest)
        target = (f"/v1/sessions/{quote(session_id, safe='')}/workspace/purges/"
                  f"{quote(operation_id, safe='')}?request_digest={request_digest}")
        try:
            result = self._request("GET", target)
        except APIError as error:
            if error.status_code == 404:
                return None
            raise
        return purge_receipt(result, session_id, operation_id, request_digest)

    def lookup_storage_purge(self, workspace_id: str, *, operation_id: str, request_digest: str) -> dict[str,
        Any] | None:
        preparation_request(operation_id, request_digest)
        target = (f"/v1/workspaces/{quote(workspace_id, safe='')}/purges/"
                  f"{quote(operation_id, safe='')}?request_digest={request_digest}")
        try:
            result = self._request("GET", target)
        except APIError as error:
            if error.status_code == 404:
                return None
            raise
        receipt = purge_receipt(result, None, operation_id, request_digest)
        if receipt["workspace_id"] != workspace_id:
            raise ProtocolError("original storage purge workspace mismatch")
        return receipt

    def prepare_workspace_view(self, session_id: str, *, operation_id: str, request_digest: str) -> dict[str, Any]:
        payload = preparation_request(operation_id, request_digest)
        result = self._request("POST", f"/v1/sessions/{quote(session_id, safe='')}/workspace-view?action=prepare",
            payload)
        return preparation_receipt(result, session_id, operation_id, request_digest)

    def lookup_workspace_preparation(self, session_id: str, *, operation_id: str, request_digest: str) -> dict[str,
        Any] | None:
        preparation_request(operation_id, request_digest)
        target = (f"/v1/sessions/{quote(session_id, safe='')}/workspace-view/preparations/"
                  f"{quote(operation_id, safe='')}?request_digest={request_digest}")
        try:
            result = self._request("GET", target)
        except APIError as error:
            if error.status_code == 404:
                return None
            raise
        return preparation_receipt(result, session_id, operation_id, request_digest)

    def seal_workspace_view(self, session_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/sessions/{quote(session_id, safe='')}/workspace-view?action=seal")
