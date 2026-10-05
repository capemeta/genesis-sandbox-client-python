"""离线校验非 editable 安装构件，拒绝以源码导入冒充 wheel 发布验证。"""
from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import io
import json
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch


def main() -> None:
    installed, wheel = (Path(value).resolve() for value in sys.argv[1:])
    if not wheel.is_file() or wheel.suffix != ".whl" or not installed.is_dir():
        raise ValueError("需要实际 wheel 和独立安装目录")
    sys.path.insert(0, str(installed))
    import genesis_sandbox_client
    from genesis_sandbox_client import Client

    package = Path(genesis_sandbox_client.__file__).resolve()
    if not package.is_relative_to(installed):
        raise AssertionError("SDK 未从独立安装目录加载")
    distribution = importlib.metadata.distribution("genesis-sandbox-client-python")
    if not Path(distribution.locate_file("")).resolve().is_relative_to(installed):
        raise AssertionError("SDK 发布元数据不是独立安装构件")
    assert distribution.version == "0.3.0"
    assert distribution.metadata["Requires-Python"] == ">=3.12"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert {"genesis_sandbox_client/client.py", "genesis_sandbox_client/workspace_client.py",
                "genesis_sandbox_client/session_exec_client.py", "genesis_sandbox_client/py.typed"}.issubset(names)
        assert any(name.endswith(".dist-info/METADATA") for name in names)
        assert any(name.endswith("/LICENSE") for name in names)
    for method in ("inspect_shared_storage_resource", "create_workspace", "get_workspace", "delete_workspace",
                   "mkdir_session_dir", "set_session_file_executable", "stat_session_file", "lookup_session",
                   "renew_session", "get_session_history", "purge_session_workspace", "list_session_execs"):
        assert callable(getattr(Client, method, None)), method
    assert "workspace_binding" in inspect.signature(Client.create_workspace).parameters
    assert "retention_mode" in inspect.signature(Client.create_workspace).parameters

    class Response(io.BytesIO):
        status = 200

    with patch("genesis_sandbox_client.client._urlopen") as transport:
        transport.side_effect = [Response(b"{}"), Response(b"{}"), Response(b'{"executable":true}'),
                                 Response(b'{"items":[],"total":0}')]
        client = Client("https://sandbox.example")
        binding = {"mode": "shared", "storage_ref": "approved", "resource_id": "resource", "binding_version": 7}
        client.create_workspace("resource", workspace_binding=binding, retention_mode="explicit_delete")
        client.inspect_shared_storage_resource("approved", "resource")
        client.set_session_file_executable("session", "work/run", True, if_match="a" * 64)
        assert client.list_session_execs("original", cursor="opaque+/", limit=3) == {"items": [], "total": 0}
        requests = [call.args[0] for call in transport.call_args_list]
        payload = json.loads(requests[0].data)
        assert payload == {"workspace_id": "resource", "workspace_binding": binding,
            "retention_mode": "explicit_delete"}
        assert requests[1].full_url.endswith("storage-resources/approved/resource")
        assert requests[2].method == "PATCH" and requests[2].get_header("If-match") == "a" * 64
        assert requests[3].method == "GET" and requests[3].full_url.endswith("execs?limit=3&cursor=opaque%2B%2F")
    print(json.dumps({"version": distribution.version, "python": sys.version.split()[0],
        "package": str(package), "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "independent_install": True, "protocol_checks": "passed"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
