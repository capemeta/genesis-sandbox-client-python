"""
Synchronous SDK example for one-shot Job + Artifact flow.

Demonstrates:
  - submit_job + wait_job for stateless execution
  - /workspace/output collected as output_artifacts
  - upload_job_file + input_artifact_ids for input injection

Session + WorkspaceFS flow is shown separately in `production_sync.py`.
"""

import os

from genesis_sandbox_client import Client


def demo_output_artifact(client: Client) -> None:
    print("\n=== Job Sync: Output Artifact Collection ===")
    submitted = client.submit_job(
        profile="code-polyglot-basic",
        code="""
import os
os.makedirs('/workspace/output', exist_ok=True)
with open('/workspace/output/hello.txt', 'w', encoding='utf-8') as f:
    f.write('Hello from Python Sandbox output artifact.')
print('artifact generated')
""",
    )
    result = client.wait_job(submitted["job_id"])
    print(result.get("stdout", "").rstrip())

    job = client.get_job(result["job_id"])
    artifacts = job.get("output_artifacts") or []
    if not artifacts:
        raise RuntimeError(f"No artifacts collected for job {result['job_id']}")

    artifact = artifacts[0]
    print(f"Collected artifact: ID={artifact['artifact_id']} Name={artifact['name']} Size={artifact['size']}")
    content = client.download_artifact(artifact["artifact_id"])
    print("Downloaded artifact content:")
    print(content.decode("utf-8"))


def demo_input_artifact(client: Client) -> None:
    print("\n=== Job Sync: Input Artifact Injection ===")
    submitted1 = client.submit_job(profile="code-polyglot-basic", code="print('step 1 execution completed')")
    job1 = client.wait_job(submitted1["job_id"])
    job_id = job1["job_id"]
    print(f"Job 1 completed. JobID = {job_id}")

    artifact = client.upload_job_file(job_id, "data.csv", "key,value\nitem1,100\nitem2,200\n")
    artifact_id = artifact["artifact_id"]
    print(f"Uploaded input artifact: ID={artifact_id} Name={artifact['name']} Size={artifact['size']}")

    submitted2 = client.submit_job(
        profile="code-polyglot-basic",
        input_artifact_ids=[artifact_id],
        code="""
from pathlib import Path
input_file = Path('/workspace/input/data.csv')
if input_file.exists():
    print('Found input file!')
    print(input_file.read_text())
else:
    print('Input file data.csv not found!')
""",
    )
    job2 = client.wait_job(submitted2["job_id"])
    print(job2.get("stdout", "").rstrip())


if __name__ == "__main__":
    base_url = os.getenv("GENESIS_SANDBOX_BASE_URL", "http://127.0.0.1:18010")
    api_key = os.getenv("GENESIS_SANDBOX_API_KEY", "test-token-1")
    client = Client(base_url, token=api_key, timeout=60)

    demo_output_artifact(client)
    demo_input_artifact(client)
