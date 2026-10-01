"""
Production-grade asynchronous SDK example.

Demonstrates:
  - async with new_sandbox_async — automatic lifecycle management
  - asyncio.gather — run python/shell/node CONCURRENTLY (fan-out)
  - asyncio.to_thread backing — event loop stays responsive
  - Error isolation: one failure doesn't kill other concurrent tasks
  - Semaphore: throttle concurrent sandboxes to avoid quota exhaustion
  - dormant create / Suspend / resume (see demo_dormant_suspend)
  - 完整边界说明（Close 不删 Workspace、ExecResult 字段等）见 production_sync.py 文件头

Run:
    export GENESIS_SANDBOX_BASE_URL=http://127.0.0.1:18010
    export GENESIS_SANDBOX_API_KEY=test-token-1
    uv run python examples/production_async.py

Requires: Python 3.12+
"""

import asyncio
import logging
import os

from genesis_sandbox_client import Client, ExecResult, new_sandbox_async

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("production_async")


# ─────────────────────────────────────────────────────────────────────────────
# Individual async demos
# ─────────────────────────────────────────────────────────────────────────────


async def demo_python(client: Client) -> None:
    log.info("=== Async: Python ===")
    async with new_sandbox_async(client, profile="code-polyglot-basic") as sb:
        r = await sb.run_python("""
import sys, platform
print(f"Python {sys.version}")
print(f"Platform: {platform.system()}")
print("fib(30):", (lambda f: f(f, 30))(lambda f, n: n if n < 2 else f(f, n-1) + f(f, n-2)))
""")
        log.info("Exit: %d", r.exit_code)
        print(r.stdout)


async def demo_shell(client: Client) -> None:
    log.info("=== Async: Shell ===")
    async with new_sandbox_async(client, profile="code-polyglot-basic") as sb:
        r = await sb.run_shell("""
#!/bin/sh
uname -a
df -h /
""")
        log.info("Exit: %d", r.exit_code)
        print(r.stdout)


async def demo_node(client: Client) -> None:
    log.info("=== Async: Node.js ===")
    async with new_sandbox_async(client, profile="code-polyglot-basic") as sb:
        r = await sb.run_node("""
const os = require('os');
console.log('Node:', process.version, '| Platform:', os.platform());
console.log('Squares:', Array.from({length: 5}, (_, i) => i * i));
""")
        log.info("Exit: %d", r.exit_code)
        print(r.stdout)


# ─────────────────────────────────────────────────────────────────────────────
# Key async pattern: fan-out — run python/shell/node CONCURRENTLY
# ─────────────────────────────────────────────────────────────────────────────


async def demo_fanout_concurrent(client: Client) -> None:
    """
    Fan-out: launch 3 sandboxes and 3 jobs all at once.
    Total time ≈ max(individual times), not sum.
    """
    log.info("=== Async: Fan-out (Python + Shell + Node concurrently) ===")

    async def run_one(profile: str, code: str) -> ExecResult:
        async with new_sandbox_async(client, profile=profile) as sb:
            if profile == "code-polyglot-basic":
                return await sb.run_python(code)
            elif profile == "code-polyglot-basic":
                return await sb.run_shell(code)
            elif profile == "code-polyglot-basic":
                return await sb.run_node(code)
            else:
                raise ValueError(profile)

    py_code = "print('python result:', 6 * 7)"
    sh_code = "echo 'shell result:' $(( 6 * 7 ))"
    node_code = "console.log('node result:', 6 * 7)"

    # All three run CONCURRENTLY — this is the key async advantage
    py_r, sh_r, node_r = await asyncio.gather(
        run_one("code-polyglot-basic", py_code),
        run_one("code-polyglot-basic", sh_code),
        run_one("code-polyglot-basic", node_code),
    )

    print("Python:", py_r.stdout.strip())
    print("Shell: ", sh_r.stdout.strip())
    print("Node:  ", node_r.stdout.strip())


# ─────────────────────────────────────────────────────────────────────────────
# Batch with throttling — avoid quota exhaustion
# ─────────────────────────────────────────────────────────────────────────────


async def demo_batch_with_throttle(client: Client) -> None:
    """
    Run 6 Python tasks concurrently, but limit to 3 active sandboxes at once
    using asyncio.Semaphore to stay within quota limits.
    """
    log.info("=== Async: Batch with Semaphore throttle (max 3 concurrent) ===")

    codes = [f"print('task {i}: result =', {i} ** 2)" for i in range(6)]

    sem = asyncio.Semaphore(3)  # at most 3 sandboxes at a time

    async def run_task(idx: int, code: str) -> ExecResult:
        async with sem:
            async with new_sandbox_async(client, profile="code-polyglot-basic") as sb:
                return await sb.run_python(code)

    results = await asyncio.gather(
        *[run_task(i, code) for i, code in enumerate(codes)],
        return_exceptions=True,  # isolate individual failures
    )

    for i, r in enumerate(results):
        if isinstance(r, Exception):
            log.error("Task %d failed: %s", i, r)
        else:
            print(f"Task {i}:", r.stdout.strip())


# ─────────────────────────────────────────────────────────────────────────────
# Async with timeout at the gather level
# ─────────────────────────────────────────────────────────────────────────────


async def demo_gather_with_timeout(client: Client) -> None:
    """Apply an overall timeout to a group of concurrent tasks."""
    log.info("=== Async: Gather with overall timeout ===")

    async def slow_task(n: int) -> ExecResult:
        async with new_sandbox_async(client, profile="code-polyglot-basic") as sb:
            return await sb.run_python(f"import time; time.sleep({n}); print('done')")

    try:
        results = await asyncio.wait_for(
            asyncio.gather(slow_task(1), slow_task(2)),
            timeout=30.0,
        )
        for r in results:
            print(r.stdout.strip())
    except TimeoutError:
        log.warning("Overall gather timed out (expected for demo)")


async def demo_dormant_suspend(client: Client) -> None:
    """异步版休眠 / Suspend / 再唤醒。

    Suspend 场景与心跳规则与同步示例 `production_sync.demo_dormant_suspend` 相同：
      - Suspend 只放 Runtime，保留 Session + Workspace。
      - 心跳不要停：renew_session 在无 Runtime 时仍续 Session TTL。
      - close() 才销毁 Session。
    """
    log.info("=== Async: Dormant create + Suspend + resume ===")
    async with new_sandbox_async(
        client,
        profile="code-polyglot-basic",
        workspace_retention="explicit_delete",
    ) as sb:
        print(f"after create: session={sb.session_id} sandbox={sb.sandbox_id!r}")
        await sb.write_file("notes/async.txt", "async persisted before runtime\n")
        r = await sb.run_python("from pathlib import Path; print(Path('notes/async.txt').read_text())")
        assert r.ok(), r
        print(f"after first exec: sandbox={sb.sandbox_id}\n{r.stdout}")
        await sb.suspend()
        print(f"after suspend: sandbox={sb.sandbox_id!r}")
        content = (await sb.read_file("notes/async.txt")).decode().strip()
        print(f"read after suspend: {content!r}")
        resumed = await sb.resume()
        log.info("explicitly resumed session runtime=%s", resumed.get("active_sandbox_id"))
        r = await sb.run_python(
            "from pathlib import Path; print('resumed:', Path('notes/async.txt').read_text().strip())"
        )
        assert r.ok(), r
        print(f"after resume: sandbox={sb.sandbox_id}\n{r.stdout}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


async def main() -> None:
    base_url = os.getenv("GENESIS_SANDBOX_BASE_URL", "http://127.0.0.1:18010")
    api_key = os.getenv("GENESIS_SANDBOX_API_KEY", "test-token-1")
    client = Client(base_url, token=api_key, timeout=60)

    await demo_python(client)
    await demo_shell(client)
    await demo_node(client)
    await demo_fanout_concurrent(client)
    await demo_batch_with_throttle(client)
    await demo_gather_with_timeout(client)
    await demo_dormant_suspend(client)

    log.info("All async demos completed.")


if __name__ == "__main__":
    asyncio.run(main())
