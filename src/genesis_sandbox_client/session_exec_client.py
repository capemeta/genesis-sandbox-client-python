"""只读执行记录分页；不创建、恢复或重新提交执行。"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

from .errors import ProtocolError


def validate_exec_record(
    value: Any, session: str, execution: str | None = None, operation: str | None = None
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("session_id") != session:
        raise ProtocolError("execution receipt original session mismatch")
    if (
        not isinstance(value.get("exec_id"), str)
        or not value["exec_id"]
        or not isinstance(value.get("operation_id"), str)
        or not value["operation_id"]
    ):
        raise ProtocolError("execution receipt original identity missing")
    if (execution is not None and value["exec_id"] != execution) or (
        operation is not None and value["operation_id"] != operation
    ):
        raise ProtocolError("execution receipt original operation mismatch")
    if value.get("status") not in {"queued", "running", "succeeded", "failed", "cancelled", "timed_out", "interrupted"}:
        raise ProtocolError("invalid execution receipt status")
    if "stop_confirmed" in value and not isinstance(value["stop_confirmed"], bool):
        raise ProtocolError("invalid physical stop evidence")
    if value.get("stop_confirmed") and value["status"] in {"queued", "running"}:
        raise ProtocolError("nonterminal execution cannot confirm physical stop")
    return value


class SessionExecClientMixin:
    def _request(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def list_session_execs(
        self, session_id: str, *, limit: int | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(session_id, str) or not session_id or any(ord(char) < 32 for char in session_id):
            raise ValueError("session_id must be a nonempty identity")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100):
            raise ValueError("limit must be an integer between 1 and 100")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ValueError("cursor must be a nonempty string")
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        target = f"/v1/sessions/{quote(session_id, safe='')}/execs"
        if params:
            target += f"?{urlencode(params)}"
        page = self._request("GET", target)
        if not isinstance(page, dict) or not isinstance(page.get("items"), list):
            raise ProtocolError("invalid execution history page")
        total = page.get("total")
        if (
            isinstance(total, bool)
            or not isinstance(total, int)
            or total < len(page["items"])
            or len(page["items"]) > (limit or 50)
        ):
            raise ProtocolError("invalid execution history page size")
        if "next_cursor" in page and not isinstance(page["next_cursor"], str):
            raise ProtocolError("invalid opaque execution page cursor")
        seen = set()
        for item in page["items"]:
            validate_exec_record(item, session_id)
            if item["exec_id"] in seen:
                raise ProtocolError("duplicate execution history identity")
            seen.add(item["exec_id"])
        return page
