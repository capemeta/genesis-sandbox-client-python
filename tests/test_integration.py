"""真实服务集成测试：连接 GENESIS_SANDBOX_INTEGRATION=1 时启用的本地/远端沙箱服务。

前置：服务已启动（scripts/start-dev-local.bat），并设置：

    GENESIS_SANDBOX_INTEGRATION=1
    GENESIS_SANDBOX_BASE_URL=http://127.0.0.1:18010
    GENESIS_SANDBOX_API_KEY=<users.local.yaml 中的个人 key>

覆盖矩阵：能力发现、会话生命周期（创建/幂等 lookup/续租/挂起/恢复/清理）、
短任务同步执行、长任务异步执行与轮询、operation_id 恢复、取消回执、
文件往返、多会话并发、单会话并发 exec、错误路径。
"""

from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from genesis_sandbox_client import (
    APIError,
    Client,
    ExecRecoveryError,
    new_sandbox,
    quick_python,
)

INTEGRATION = os.environ.get("GENESIS_SANDBOX_INTEGRATION") == "1"
BASE_URL = os.environ.get("GENESIS_SANDBOX_BASE_URL", "http://127.0.0.1:18010")
API_KEY = os.environ.get("GENESIS_SANDBOX_API_KEY", "")

pytestmark = pytest.mark.skipif(
    not INTEGRATION or not API_KEY,
    reason="set GENESIS_SANDBOX_INTEGRATION=1 and GENESIS_SANDBOX_API_KEY to run integration tests",
)


def make_client(token: str | None = None) -> Client:
    return Client(BASE_URL, token=token or API_KEY, timeout=120)


@pytest.fixture()
def client() -> Client:
    return make_client()


@pytest.fixture()
def session(client: Client):
    """打开一个禁用心跳、TTL 工作区的会话；退出时删除会话并显式删除工作区。

    workspace_retention=ttl 也要等 TTL 清扫；测试内必须立即释放租户配额（4096MB/256MB=16 个）。
    """
    sb = new_sandbox(
        client,
        profile="code-polyglot-basic",
        ttl_seconds=600,
        workspace_retention="ttl",
        workspace_ttl_seconds=900,
        idempotency_key=f"it-py-{uuid.uuid4().hex}",
        heartbeat=False,
    )
    workspace_id = sb.session.get("workspace_id", "")
    try:
        yield sb
    finally:
        # 活跃 exec 会让 close 撞 SESSION_HAS_ACTIVE_EXECS；清理尽力而为。
        try:
            sb.close()
        except (APIError, ExecRecoveryError):
            pass
        try:
            client.delete_session(sb.session_id)
        except (APIError, ExecRecoveryError):
            pass
        if workspace_id:
            try:
                client.delete_workspace(workspace_id)
            except APIError:
                pass


# ─── 能力发现 ────────────────────────────────────────────────────────


def test_catalog_and_profiles(client: Client):
    catalog = client.get_catalog()
    assert catalog["total"] > 0
    names = {item["name"] for item in catalog["items"]}
    assert "code-polyglot-basic" in names
    assert catalog.get("default_profile")


# ─── 会话生命周期 + 短任务 ──────────────────────────────────────────


def test_session_lifecycle_and_short_exec(client: Client, session):
    info = client.get_session(session.session_id)
    assert info["status"] == "active"
    assert info["state_policy"] == "session"
    assert info["workspace_id"]

    # 幂等键 lookup 返回同一会话。
    idem = session.session.get("idempotency_key", "")
    if idem:
        found = client.lookup_session(idem)
        assert found is not None
        assert found["session_id"] == session.session_id

    result = session.run_python("print(1+1)")
    assert result.ok()
    assert result.stdout.strip() == "2"


def test_lookup_unknown_idempotency_key_returns_none(client: Client):
    assert client.lookup_session(f"no-such-{uuid.uuid4().hex}") is None


# ─── 文件往返 ────────────────────────────────────────────────────────


def test_files_round_trip(session):
    session.write_file("notes/input.txt", b"hello integration")
    result = session.run_command("cat", "/workspace/notes/input.txt")
    assert result.ok()
    assert result.stdout == "hello integration"

    data = session.read_file("notes/input.txt")
    assert data == b"hello integration"

    listing = session.list_files("notes")
    assert any(e["name"] == "input.txt" for e in listing["entries"])

    stat = session.stat_file("notes/input.txt")
    assert stat["size"] == len(b"hello integration")

    session.remove("notes/input.txt")
    with pytest.raises(APIError) as missing:
        session.stat_file("notes/input.txt")
    assert missing.value.status_code == 404
    assert missing.value.error_code == "WORKSPACE_PATH_NOT_FOUND"


# ─── 长任务异步执行 + 轮询 + operation_id 恢复 ──────────────────────


def test_long_async_exec_polling_and_lookup(client, session):
    operation_id = f"op-{uuid.uuid4().hex}"
    exec_id = session.run_async(
        "import time\ntime.sleep(8)\nprint('long-done')",
        lang="python",
        timeout=60,
        operation_id=operation_id,
    )

    time.sleep(3)
    mid = client.get_exec(session.session_id, exec_id)
    assert mid["status"] not in ("succeeded", "failed")

    # operation_id lookup 与 exec_id 指向同一记录（崩溃恢复路径）。
    by_op = client.get_exec_by_operation(session.session_id, operation_id)
    assert by_op["exec_id"] == exec_id

    result = session.wait_exec(exec_id, max_wait=60)
    assert result.ok()
    assert result.stdout.strip() == "long-done"


def test_cancel_long_exec_receipt(session):
    exec_id = session.run_async(
        "import time\ntime.sleep(60)\nprint('nope')", lang="python", timeout=90
    )
    time.sleep(2)
    receipt = session.cancel_exec_receipt(exec_id)
    assert receipt.outcome in ("accepted", "already_terminal", "stop_confirmed", "stop_unknown")

    result = session.wait_exec(exec_id, max_wait=60)
    assert not result.ok()
    assert result.error_code in ("cancelled", "EXEC_CANCELLED", "")


# ─── 挂起 / 恢复 / 续租 ─────────────────────────────────────────────


def test_suspend_resume_lifecycle(client: Client, session):
    assert session.run_python("print('warm')").ok()

    session.suspend()
    info = client.get_session(session.session_id)
    assert info.get("active_sandbox_id", "") == ""

    # Python SDK 有显式 resume：恢复 runtime 后工作区文件仍在。
    session.write_file("keep.txt", b"persisted")
    session.resume()
    result = session.run_python("print(open('/workspace/keep.txt').read().strip())")
    assert result.ok()
    assert result.stdout.strip() == "persisted"


def test_suspend_with_active_exec_conflicts(client: Client, session):
    session.run_async("import time\ntime.sleep(15)\nprint('x')", lang="python", timeout=60)
    time.sleep(2)
    with pytest.raises(APIError) as exc_info:
        client.suspend_session(session.session_id)
    assert exc_info.value.error_code in ("SESSION_HAS_ACTIVE_EXEC", "SESSION_HAS_ACTIVE_EXECS")


def test_renew_extends_expiry(client: Client, session):
    before = client.get_session(session.session_id)
    # 续租语义：newExpiry = now + extend，只能延长（会话 TTL 600s，续 900s 才可见）。
    after = client.renew_session(session.session_id, 900)
    assert after["expires_at"] > before["expires_at"]


# ─── 并发 ────────────────────────────────────────────────────────────


def test_concurrent_sessions(client: Client):
    def worker(idx: int, attempt: int = 0) -> tuple[int, str]:
        # 容器懒启动在首个 exec 时才消耗配额；全局 CPU 配额（4 核）可能被前序测试
        # draining 中的容器暂时占满。服务端标记 429 可重试，这里做有界退避重试。
        try:
            sb = new_sandbox(
                client,
                profile="code-polyglot-basic",
                ttl_seconds=600,
                workspace_retention="ttl",
                workspace_ttl_seconds=900,
                heartbeat=False,
                idempotency_key=f"it-py-conc-{idx}-{uuid.uuid4().hex}",
            )
        except APIError as error:
            if attempt < 4 and error.retryable:
                time.sleep(2)
                return worker(idx, attempt + 1)
            raise
        workspace_id = sb.session.get("workspace_id", "")
        try:
            out = ""
            for j in range(2):
                result = sb.run_python(f"print({idx}*100+{j})")
                assert result.ok(), result
                out += result.stdout.strip() + ","
            return idx, out
        except APIError as error:
            if attempt < 4 and error.retryable:
                time.sleep(2)
                return worker(idx, attempt + 1)
            raise
        finally:
            sb.close()
            if workspace_id:
                try:
                    client.delete_workspace(workspace_id)
                except APIError:
                    pass

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(worker, range(3)))
    for idx, out in results:
        assert out == f"{idx * 100 + 0},{idx * 100 + 1},"


def test_concurrent_async_execs_in_one_session(client: Client, session):
    exec_ids = [
        session.run_async(f"import time\ntime.sleep({3 + i})\nprint('c{i}')", lang="python", timeout=60)
        for i in range(3)
    ]
    for i, exec_id in enumerate(exec_ids):
        result = session.wait_exec(exec_id, max_wait=90)
        assert result.ok()
        assert result.stdout.strip() == f"c{i}"

    records = client.list_session_execs(session.session_id, limit=100)
    listed = {record["exec_id"] for record in records["items"]}
    assert set(exec_ids) <= listed
    for exec_id in exec_ids:
        record = client.get_exec(session.session_id, exec_id)
        assert record["status"] == "succeeded"


# ─── 清理语义 ────────────────────────────────────────────────────────


def test_cleanup_semantics(client: Client):
    sb = new_sandbox(
        client,
        profile="code-polyglot-basic",
        ttl_seconds=600,
        workspace_retention="ttl",
        workspace_ttl_seconds=900,
        heartbeat=False,
    )
    workspace_id = sb.session.get("workspace_id", "")
    session_id = sb.session_id
    sb.close()
    if workspace_id:
        client.delete_workspace(workspace_id)

    with pytest.raises(APIError) as exc_info:
        client.get_session(session_id)
    assert exc_info.value.status_code == 404

    # 重复删除幂等：成功或 404，绝不允许 5xx。
    try:
        client.delete_session(session_id)
    except APIError as error:
        assert error.status_code < 500

    with pytest.raises((APIError, ExecRecoveryError)):
        sb.run_python("print('zombie')")


# ─── 错误路径 ────────────────────────────────────────────────────────


def test_unknown_profile_rejected(client: Client):
    with pytest.raises(APIError) as exc_info:
        new_sandbox(client, profile="no-such-profile", heartbeat=False)
    assert exc_info.value.error_code == "PROFILE_NOT_FOUND"


def test_invalid_token_rejected():
    bad = make_client(token="definitely-wrong")
    with pytest.raises(APIError) as exc_info:
        bad.get_catalog()
    assert exc_info.value.status_code == 401


# ─── 一次性 Job ──────────────────────────────────────────────────────


def test_quick_python_job(client: Client):
    result = quick_python(client, "print('quick-42')", profile="code-polyglot-basic")
    assert result.ok()
    assert result.stdout.strip() == "quick-42"
