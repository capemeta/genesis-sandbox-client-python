"""调用输出协议只携带逻辑单段目录，不接受宿主路径授权。"""

import re


def output_request(value: str | None) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, str) or not re.fullmatch(r"output/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise ValueError("invalid invocation output directory")
    return {"output_directory": value}
