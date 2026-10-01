"""推荐的 Python Session 调用方式。"""

import os
import uuid

from genesis_sandbox_client import APIError, Client, TransportError, new_sandbox


def main() -> None:
    client = Client(
        os.getenv("GENESIS_SANDBOX_BASE_URL", "http://127.0.0.1:18010"),
        token=os.getenv("GENESIS_SANDBOX_API_KEY"),
        timeout=30,
    )
    with new_sandbox(
        client,
        profile="code-polyglot-basic",
        idempotency_key=f"quickstart-{uuid.uuid4()}",
    ) as sb:
        sb.write_file("input/name.txt", "Genesis")
        result = sb.run_python(
            """
from pathlib import Path
name = Path("/workspace/input/name.txt").read_text()
print(f"hello {name}")
"""
        )
        if not result.ok():
            raise RuntimeError(
                f"execution failed: exit={result.exit_code} code={result.error_code} stderr={result.stderr}"
            )
        print(result.stdout, end="")

        exec_id = sb.run_async('print("async complete")', lang="python", timeout=30)
        sb.wait_exec(exec_id, max_wait=60)

        sb.suspend()
        sb.resume()
        # resume 已显式确认新 Runtime 与原 Workspace 绑定。
        sb.run_python('print("resumed")')


if __name__ == "__main__":
    try:
        main()
    except APIError as error:
        raise SystemExit(
            f"API error code={error.error_code} status={error.status_code} "
            f"request_id={error.request_id} retryable={error.retryable}: {error.message}"
        ) from error
    except TransportError as error:
        raise SystemExit(f"transport error: {error}") from error
