"""
Production-grade synchronous SDK example for Session + WorkspaceFS.

Demonstrates:
  - session-backed helper keeps one workspace alive across multiple steps
  - context manager guarantees delete_session cleanup even on exceptions
  - dormant create / lazy runtime / Suspend / resume (see demo_dormant_suspend)
  - Python / shell / node / command execution inside one helper abstraction
  - WorkspaceFS APIs work without a running container; runtime mounts the same workspace

Boundaries (do not misread this file as the full contract):
  - Default path: open -> write/exec -> close. Suspend is optional, not required.
  - sandbox_id is the current Runtime: may be empty or change after Suspend/resume.
    Use session_id / workspace_id as durable identities.
  - Helper ExecResult includes error_code, truncation flags and EffectiveEnvironment;
    helper also backfills sandbox_id onto sb.sandbox_id.
  - download_session_file returns bytes only (no headers). For environment/sha256 after
    read, use write/stat responses (or low-level Client with headers).
  - close()/delete_session does NOT delete Workspace when retention=explicit_delete
    (default). demo_dormant_suspend shows explicit delete_workspace after close.
  - Viewer/GUI and Job+Artifact are out of scope here (see job_artifact_sync.py).
"""

import logging
import os

from genesis_sandbox_client import Client, new_sandbox

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("production_sync")


def demo_python(client: Client) -> None:
    log.info("=== Session Sync: Python ===")
    with new_sandbox(client, profile="code-polyglot-basic") as sb:
        r = sb.run_python("""
import sys, platform
print(f"Python {sys.version}")
print(f"Platform: {platform.system()}")


def fib(n):
    a, b = 0, 1
    for _ in range(n): a, b = b, a + b
    return a

print("fib(30):", fib(30))
""")
        assert r.ok(), f"Failed: {r}"
        print(r.stdout)


def demo_shell(client: Client) -> None:
    log.info("=== Session Sync: Shell ===")
    with new_sandbox(client, profile="code-polyglot-basic") as sb:
        r = sb.run_shell("""
#!/bin/sh
echo "=== System Info ==="
uname -a
echo "=== Disk ==="
df -h /
""")
        assert r.ok(), f"Failed: {r}"
        print(r.stdout)


def demo_node(client: Client) -> None:
    log.info("=== Session Sync: Node.js ===")
    with new_sandbox(client, profile="code-polyglot-basic") as sb:
        r = sb.run_node("""
const os = require('os');
console.log('Node:', process.version, '| Platform:', os.platform());
const arr = Array.from({length: 5}, (_, i) => i * i);
console.log('Squares:', arr);
""")
        assert r.ok(), f"Failed: {r}"
        print(r.stdout)


def demo_command(client: Client) -> None:
    log.info("=== Session Sync: Raw Command ===")
    with new_sandbox(client, profile="code-polyglot-basic") as sb:
        r = sb.run_command(
            "python",
            "-c",
            "import json; print(json.dumps({'ok': True, 'n': 42}))",
        )
        assert r.ok(), f"Failed: {r}"
        print(r.stdout)


def demo_multi_task_same_workspace(client: Client) -> None:
    """共享的是 Session Workspace，不是“永远同一个 sandbox_id”。"""
    log.info("=== Session Sync: Multi-task on one session workspace ===")
    with new_sandbox(client, profile="code-polyglot-basic") as sb:
        print(f"before any exec: session={sb.session_id} sandbox={sb.sandbox_id!r} workspace={sb.workspace_id}")
        info = sb.write_file("shared/state.txt", "hello from task 1\n")
        print("write environment={}".format(info.get("environment")))

        listing = sb.list_files(".", recursive=True, limit=100)
        print("workspace entries:")
        for entry in listing.get("entries", []):
            print(f"- {entry['path']} ({entry['kind']}) env={entry.get('environment')}")

        # helper read_file 只返回 bytes；元数据请用 stat_file。
        meta = sb.stat_file("shared/state.txt")
        content = sb.read_file("shared/state.txt").decode("utf-8")
        print("shared state via WorkspaceFS (stat environment={}): {}".format(meta.get("environment"), content.strip()))

        r = sb.run_python("""
from pathlib import Path
print(Path('shared/state.txt').read_text())
""")
        assert r.ok(), f"Runtime task failed: {r}"
        print(
            f"after exec: sandbox={sb.sandbox_id} (may change across Suspend/resume)\n"
            f"runtime sees same workspace: {r.stdout.strip()}"
        )


def demo_dormant_suspend(client: Client) -> None:
    """休眠创建 → 无容器写文件 → 懒启 Runtime → Suspend → 再唤醒。

    Suspend 适用场景：
      - Agent 多轮对话空闲期：要保留工作区文件，但不想持续占用容器配额/CPU。
      - 长流程等待用户确认或外部回调：逻辑 Session 未结束，Runtime 可先放下。
      - 给其他任务腾并发容器额度：Suspend ≠ close（不是删除 Session）。

    Suspend 之后心跳要不要特殊处理？
      - 不需要停心跳，也不需要换 API。
      - helper 内置心跳继续调用 renew_session：
          · 始终延长 Session TTL（以及 retention=ttl 时的 Workspace TTL）
          · 若当前没有 active Runtime，服务端会跳过沙箱 lease 续期（预期行为）
      - 错误做法：Suspend 后停心跳 → Session 仍可能因 TTL 到期被回收。
      - close()/delete_session 才会销毁 Session；Suspend 只释放 Runtime。
      - close 默认不会删除 Workspace（explicit_delete）；本 demo 结束后显式 delete_workspace。
    """
    log.info("=== Session Sync: Dormant create + Suspend + resume ===")
    sb = new_sandbox(
        client,
        profile="code-polyglot-basic",
        # 与服务端 sessions.workspace_retention 默认一致；显式写出便于对照。
        workspace_retention="explicit_delete",
    )
    workspace_id = sb.workspace_id
    try:
        print(
            f"after create: session={sb.session_id} sandbox={sb.sandbox_id!r} "
            f"workspace={workspace_id} (empty sandbox means dormant)"
        )
        if sb.sandbox_id:
            log.warning("expected empty sandbox_id right after create, got %s", sb.sandbox_id)

        # 无容器也可写文件；响应 environment=workspace。
        info = sb.write_file("notes/hello.txt", "persisted before runtime\n")
        print(
            f"write while dormant: environment={info.get('environment')} "
            f"sandbox_path={info.get('sandbox_path')} sha256={info.get('sha256')}"
        )

        # 条件写：If-Match 为上次 sha256 时才覆盖（多 Agent 并发写同一路径时用）。
        info = sb.write_file(
            "notes/hello.txt",
            "updated with If-Match\n",
            if_match=info.get("sha256"),
        )
        print("conditional write ok, new sha256={}".format(info.get("sha256")))

        # 首次执行：懒启 Runtime；helper 会把返回的 sandbox_id 回填。
        r = sb.run_python("""
from pathlib import Path
print(Path('notes/hello.txt').read_text())
""")
        assert r.ok(), f"first exec failed: {r}"
        print(f"after first exec: sandbox={sb.sandbox_id}\nruntime sees file:\n{r.stdout}")

        # Suspend：释放容器；Session/Workspace/心跳继续。
        suspended = sb.suspend()
        active = (suspended or {}).get("active_sandbox_id", "")
        print(f"after suspend: sandbox={sb.sandbox_id!r} active_sandbox_id={active!r}")

        # Suspend 期间仍可读写 Workspace。bytes 来自 read_file；environment 来自 stat。
        meta = sb.stat_file("notes/hello.txt")
        content = sb.read_file("notes/hello.txt").decode("utf-8").strip()
        print("read after suspend (no container): environment={} content={!r}".format(meta.get("environment"), content))

        resumed = sb.resume()
        log.info("explicitly resumed session runtime=%s", resumed.get("active_sandbox_id"))
        r = sb.run_python("""
from pathlib import Path
print('resumed:', Path('notes/hello.txt').read_text().strip())
""")
        assert r.ok(), f"resume exec failed: {r}"
        print(f"after resume: sandbox={sb.sandbox_id}\n{r.stdout}")
    finally:
        # close：停心跳 + delete_session + 放 Runtime。不会自动删 Workspace。
        sb.close()
        try:
            client.delete_workspace(workspace_id)
            print(f"explicit delete_workspace({workspace_id}) after close — demo cleanup done")
        except Exception as exc:  # noqa: BLE001
            log.warning("delete_workspace after close (demo cleanup): %s", exc)


if __name__ == "__main__":
    base_url = os.getenv("GENESIS_SANDBOX_BASE_URL", "http://127.0.0.1:18010")
    api_key = os.getenv("GENESIS_SANDBOX_API_KEY", "test-token-1")
    client = Client(base_url, token=api_key, timeout=60)

    demo_python(client)
    demo_shell(client)
    demo_node(client)
    demo_command(client)
    demo_multi_task_same_workspace(client)
    demo_dormant_suspend(client)

    log.info("Session + WorkspaceFS sync demos completed.")
