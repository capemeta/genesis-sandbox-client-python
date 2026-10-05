"""原调用治理身份与维护只读证明；不从文件名或 UID 区间推导授权。"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from .errors import ProtocolError

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CONTEXT = {
    "tenant_id",
    "user_id",
    "run_id",
    "trace_id",
    "authorization_scope",
    "decision_reference",
    "subject_kind",
    "invocation_id",
    "execution_id",
    "attempt_id",
}


def _object(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{name} requires exact original fields")
    return value


def _opaque(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 256
        or any(ord(char) < 32 or ord(char) == 127 or char in "\u2028\u2029" for char in value)
    ):
        raise ValueError(f"{name} requires a bounded opaque identity")
    return value


def call_context(value: Any) -> dict[str, Any]:
    context = _object(value, _CONTEXT, "context")
    for key in ("tenant_id", "user_id", "run_id", "trace_id", "authorization_scope"):
        _opaque(context[key], key)
    if context["subject_kind"] != "enterprise":
        raise ValueError("enterprise call context required")
    if context["decision_reference"] is not None:
        _opaque(context["decision_reference"], "decision_reference")
    if any(context[key] is not None for key in ("invocation_id", "execution_id", "attempt_id")):
        for key in ("invocation_id", "execution_id", "attempt_id"):
            _opaque(context[key], key)
    return copy.deepcopy(context)


def lease_ref(value: Any) -> dict[str, Any]:
    lease = _object(value, {"provider_id", "lease_id", "workspace", "generation", "expires_at"}, "lease_ref")
    workspace = _object(
        lease["workspace"], {"workspace_key", "provider_id", "resource_id", "scope", "generation"}, "workspace_ref"
    )
    for obj, keys in (
        (lease, ("provider_id", "lease_id")),
        (workspace, ("workspace_key", "provider_id", "resource_id", "scope")),
    ):
        for key in keys:
            _opaque(obj[key], key)
        if type(obj["generation"]) is not int or not 0 < obj["generation"] <= 2**63 - 1:
            raise ValueError("original reference generation must be a positive int64")
    if lease["provider_id"] != workspace["provider_id"]:
        raise ValueError("original lease/workspace provider mismatch")
    if not isinstance(lease["expires_at"], str):
        raise ValueError("original lease timestamp required")
    timestamp = datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00"))
    if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(timestamp):
        raise ValueError("original lease timestamp must be UTC")
    return copy.deepcopy(lease)


def trusted_governance(value: Any) -> dict[str, Any]:
    record = _object(
        value,
        {
            "context",
            "lease_ref",
            "platform_operation_id",
            "request_digest",
            "source_identity",
            "workspace_id",
            "authorized_work_write",
        },
        "trusted_governance",
    )
    call_context(record["context"])
    if any(record["context"][key] is None for key in ("invocation_id", "execution_id", "attempt_id")):
        raise ValueError("governed execution requires complete original invocation identity")
    lease_ref(record["lease_ref"])
    if record["lease_ref"]["workspace"]["scope"] != record["context"]["authorization_scope"]:
        raise ValueError("original lease scope differs from trusted execution context")
    for key in ("platform_operation_id", "workspace_id"):
        _opaque(record[key], key)
    for key in ("request_digest", "source_identity"):
        if not isinstance(record[key], str) or not _DIGEST.fullmatch(record[key]):
            raise ValueError(f"{key} must be a lowercase SHA256")
    if type(record["authorized_work_write"]) is not bool:
        raise ValueError("work-write authorization must be a trusted boolean")
    if not record["authorized_work_write"]:
        raise ValueError("per-invocation read-only work enforcement is unavailable")
    return copy.deepcopy(record)


def maintenance_query(
    workspace_id: str, context: dict[str, Any], operation_id: str, source_identity: str, claim_digest: str
) -> dict[str, Any]:
    payload = {
        "workspace_id": _opaque(workspace_id, "workspace_id"),
        "context": call_context(context),
        "operation_id": _opaque(operation_id, "operation_id"),
        "source_identity": source_identity,
    }
    if not isinstance(source_identity, str) or not _DIGEST.fullmatch(source_identity):
        raise ValueError("source_identity must be a lowercase SHA256")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if claim_digest != hashlib.sha256(raw).hexdigest():
        raise ValueError("original maintenance query digest mismatch")
    return {**payload, "claim_digest": claim_digest}


def maintenance_proof(value: Any, query: dict[str, Any]) -> dict[str, Any]:
    try:
        proof = _object(
            value,
            {
                "context",
                "operation_id",
                "claim_digest",
                "source_identity",
                "root_device",
                "root_inode",
                "calls",
                "leaves",
            },
            "maintenance_proof",
        )
        for key in ("context", "operation_id", "claim_digest", "source_identity"):
            if proof[key] != query[key]:
                raise ValueError("original maintenance query scope changed")
        for key in ("root_device", "root_inode"):
            if type(proof[key]) is not int or not 0 <= proof[key] <= 2**63 - 1:
                raise ValueError("source root tuple exceeds signed int64")
        if proof["root_inode"] == 0 or not isinstance(proof["calls"], list) or len(proof["calls"]) > 10000:
            raise ValueError("original same-source call list exceeds bound")
        calls: dict[str, dict[str, Any]] = {}
        seen_uids: set[int] = set()
        for call in proof["calls"]:
            _object(
                call,
                {
                    "call_id",
                    "context",
                    "lease_ref",
                    "operation_id",
                    "request_digest",
                    "execution_id",
                    "output_directory",
                    "uid",
                    "gid",
                    "root_device",
                    "root_inode",
                    "authorized_work_write",
                    "stop_confirmed",
                },
                "maintenance_call",
            )
            call_context(call["context"])
            if (
                any(call["context"][key] is None for key in ("invocation_id", "execution_id", "attempt_id"))
                or call["execution_id"] != call["context"]["execution_id"]
            ):
                raise ValueError("original call execution identity mismatch")
            lease_ref(call["lease_ref"])
            if call["lease_ref"]["workspace"]["scope"] != call["context"]["authorization_scope"]:
                raise ValueError("original call lease scope differs from context")
            for key in ("call_id", "operation_id", "execution_id"):
                _opaque(call[key], key)
            if (
                call["context"]["tenant_id"] != query["context"]["tenant_id"]
                or call["context"]["user_id"] != query["context"]["user_id"]
            ):
                raise ValueError("foreign same-source call owner")
            if call["stop_confirmed"] is not True or type(call["authorized_work_write"]) is not bool:
                raise ValueError("original stop/work-write facts missing")
            if not isinstance(call["request_digest"], str) or not _DIGEST.fullmatch(call["request_digest"]):
                raise ValueError("original request digest missing")
            for key in ("uid", "gid", "root_inode", "root_device"):
                if type(call[key]) is not int or not 0 <= call[key] <= 2**63 - 1:
                    raise ValueError("original allocation tuple exceeds signed int64")
            if min(call["uid"], call["gid"], call["root_inode"]) < 1 or call["root_device"] != proof["root_device"]:
                raise ValueError("original same-source allocation mismatch")
            if call["call_id"] in calls or call["uid"] in seen_uids:
                raise ValueError("ambiguous original allocation identity")
            if not isinstance(call["output_directory"], str) or not re.fullmatch(
                r"output/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", call["output_directory"]
            ):
                raise ValueError("original output root is not a single managed segment")
            calls[call["call_id"]] = call
            seen_uids.add(call["uid"])
        if not isinstance(proof["leaves"], list) or len(proof["leaves"]) > 100000:
            raise ValueError("maintenance leaf budget exceeded")
        seen_paths: set[str] = set()
        for leaf in proof["leaves"]:
            _object(leaf, {"path", "device", "inode", "uid", "gid", "mode", "directory", "call_id"}, "maintenance_leaf")
            path = leaf["path"]
            if (
                not isinstance(path, str)
                or path.split("/")[0] not in {"work", "output"}
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or path in seen_paths
            ):
                raise ValueError("maintenance leaf path invalid or duplicate")
            seen_paths.add(path)
            for key in ("device", "inode", "uid", "gid", "mode"):
                if type(leaf[key]) is not int or not 0 <= leaf[key] <= 2**63 - 1:
                    raise ValueError("maintenance leaf tuple invalid")
            if (
                leaf["inode"] < 1
                or leaf["device"] != proof["root_device"]
                or leaf["mode"] > 0o7777
                or type(leaf["directory"]) is not bool
            ):
                raise ValueError("maintenance leaf source/type/mode invalid")
            if leaf["call_id"] is not None and leaf["call_id"] not in calls:
                raise ValueError("maintenance leaf references an unknown original call")
            if leaf["call_id"] is not None:
                owner = calls[leaf["call_id"]]
                if path == owner["output_directory"]:
                    if (
                        not leaf["directory"]
                        or leaf["uid"] != 0
                        or leaf["gid"] != owner["gid"]
                        or leaf["device"] != owner["root_device"]
                        or leaf["inode"] != owner["root_inode"]
                        or leaf["mode"] != 0o770
                    ):
                        raise ValueError("original managed output root changed")
                elif (
                    leaf["uid"] != owner["uid"]
                    or leaf["gid"] not in {owner["gid"], 65532}
                    or (path.startswith("work/") and not owner["authorized_work_write"])
                    or (path.startswith("output/") and not path.startswith(owner["output_directory"] + "/"))
                ):
                    raise ValueError("maintenance leaf original call ownership mismatch")
            elif leaf["uid"] not in {0, 65532} or (
                path.startswith("output/") and leaf["uid"] == 0 and leaf["gid"] >= 100001
            ):
                raise ValueError("foreign leaf original call identity absent")
        if not {"work", "output"} <= seen_paths:
            raise ValueError("maintenance public root tuples absent")
        for call in calls.values():
            if not any(
                leaf["path"] == call["output_directory"] and leaf["call_id"] == call["call_id"]
                for leaf in proof["leaves"]
            ):
                raise ValueError("original call output root proof absent")
        return copy.deepcopy(proof)
    except (ValueError, TypeError, KeyError) as exc:
        raise ProtocolError(str(exc)) from exc
