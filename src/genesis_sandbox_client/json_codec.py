"""响应JSON拒绝重复字段和非有限数值，避免原调用身份被最后字段覆盖。"""

import json
import math
from typing import Any

from .errors import ProtocolError


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("sandbox response contains duplicate JSON fields")
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise ProtocolError("sandbox response contains non-finite JSON number")


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ProtocolError("sandbox response contains overflowing JSON number")
    return number


def loads(value: str | bytes) -> Any:
    return json.loads(value, object_pairs_hook=_object, parse_constant=_nonfinite, parse_float=_float)
