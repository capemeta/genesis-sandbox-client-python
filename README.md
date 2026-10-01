# Genesis Sandbox Client — Python SDK

Genesis Sandbox 的官方 Python 客户端 SDK。**零第三方依赖**（仅标准库），提供同步与异步两套会话层、类型化错误分类、幂等恢复语义与预算受控的日志/文件读取。

- 服务端协议与实现见 [genesis-sandbox](../genesis-sandbox) 仓库
- 兄弟 SDK：[genesis-sandbox-client-go](https://github.com/capemeta/genesis-sandbox-client-go)、`genesis-sandbox-client-java`、`genesis-sandbox-client-node`

## 安装

要求 Python 3.12+。

```bash
pip install genesis-sandbox-client-python
```

> 包尚未发布到 PyPI：发布前请从源码安装，例如
> `uv pip install git+https://github.com/capemeta/genesis-sandbox-client-python.git`，
> 或本地 `uv pip install -e /path/to/genesis-sandbox-client-python`。

本地开发（uv）：

```bash
uv sync                 # 创建 .venv 并可编辑安装本包 + dev 依赖
uv run pytest           # 运行测试
uv run ruff check .     # lint
uv run mypy src/genesis_sandbox_client   # 类型检查
```

## 快速开始

```python
from genesis_sandbox_client import APIError, Client, TransportError, new_sandbox

client = Client(
    "http://127.0.0.1:18010",  # 非 HTTPS 仅允许回环地址
    token="test-token-1",
    timeout=30,
)

with new_sandbox(client, profile="code-polyglot-basic") as sb:
    sb.write_file("input/name.txt", "Genesis")
    result = sb.run_python("""
from pathlib import Path
print("hello", Path("/workspace/input/name.txt").read_text())
""")
    print(result.stdout)

    exec_id = sb.run_async('print("async")', lang="python")  # 异步提交
    sb.wait_exec(exec_id, max_wait=60)  # 有界等待
```

异步：

```python
async with new_sandbox_async(client, profile="python") as sb:
    result = await sb.run_python('print("hello")')
```

无状态一次性 Job：

```python
from genesis_sandbox_client import quick_python

result = quick_python(client, "print(42)")
```

## 包结构

| 模块 | 说明 |
| --- | --- |
| `genesis_sandbox_client/client.py` | 低层 Client，覆盖 jobs / workspaces / sessions / session WorkspaceFS / artifacts / 环境解析 / 依赖构建 / GUI；含 `lookup_session`、`read_exec_logs` 预算读取、`token_provider` 凭据轮转 |
| `genesis_sandbox_client/session.py` | 同步 `SandboxSession` helper（休眠创建、懒启 Runtime、Suspend、内置心跳、WorkspaceFS、`cancel_exec_receipt`） |
| `genesis_sandbox_client/async_session.py` | 异步 `AsyncSandboxSession` helper（与同步对齐：lookup / 取消回执 / 日志流与预算读取；阻塞 I/O 经 `asyncio.to_thread` 下沉） |
| `genesis_sandbox_client/errors.py` | `APIError` / `TransportError` / `ProtocolError` / `ExecRecoveryError` 类型化错误与 `classify_error` 稳定分类 |
| `genesis_sandbox_client/types.py` | `ExecResult` / `SandboxOptions` / `CancelReceipt` / `SSEEvent` / `EffectiveEnvironment` 与心跳常量 |

包含 `py.typed`（PEP 561），全公开 API 均带类型注解。

## 运行示例

先在 genesis-sandbox 仓库启动本地开发环境（`scripts\start-dev-container.bat`），然后：

```bash
export GENESIS_SANDBOX_BASE_URL=http://127.0.0.1:18010
export GENESIS_SANDBOX_API_KEY=test-token-1

uv run python examples/quickstart.py            # 推荐应用入口
uv run python examples/production_sync.py       # Session + WorkspaceFS 主路径（含 Suspend）
uv run python examples/job_artifact_sync.py     # 单次 Job + Artifact 无状态链路
uv run python examples/production_async.py      # 异步并发（fan-out / 信号量限流）
```

## 错误处理

```python
from genesis_sandbox_client import APIError, ExecRecoveryError, TransportError, classify_error

try:
    result = sb.run_python(code)
except APIError as e:  # 结构化服务端错误
    print(e.status_code, e.error_code, e.request_id, e.retryable, e.retry_after)
except TransportError as e:  # 网络/协议层失败（ProtocolError 是其子类）
    ...
except ExecRecoveryError as e:  # 结果未知，按身份恢复
    record = sb.get_exec_by_operation(e.operation_id)
```

`classify_error(exc)` 返回稳定枚举式字符串（`invalid_request` / `unauthorized` / `not_found` / `conflict` / `rate_limited` / `transient` / `transport` / `protocol` / `unknown`），供上层 Adapter 映射通用契约错误码。

## 关键语义（生产必读）

### 写重试与 operation_id

写操作仅在携带原生幂等身份（`idempotency_key` / `operation_id`）或显式 `retry_safe` 时有限重试；重放复用同一 payload，绝不生成新 `operation_id`。无身份的写请求只发一次（即使传入 `max_retries`）。

`exec_named_session` / `exec_session_async` 未显式传 `operation_id` 时本地生成 uuid4——进程在拿到回执前崩溃则该身份丢失。**生产必须显式传入并持久保存 operation_id**，响应丢失时用 `get_exec_by_operation` / `wait_exec_by_operation` 按同一身份查回执。

### 恢复（ExecRecoveryError）

提交或观察发生不确定的网络错误时抛 `ExecRecoveryError`，携带 `session_id`、`operation_id`、可选 `exec_id` 与 `phase`（`submit` / `observe` / `lookup` / `cancel`）。会话创建响应丢失用 `client.lookup_session(idempotency_key)`（404 返回 None）。

### 取消语义（接受 vs 停止确认）

服务端 `:cancel` 确认停止后才返回终态 ExecRecord。`cancel_exec_receipt(exec_id)` 返回 `CancelReceipt`，outcome 为 `accepted` / `already_terminal` / `stop_confirmed`；**`stop_confirmed` 是资源回收安全门禁**。停止未知抛 `ExecRecoveryError(phase="cancel")`——未确认前不得回收资源或重跑替代执行。

### 日志预算分页

`read_exec_logs(exec_id, *, cursor, byte_budget, max_events)` 返回 `events` / `next_cursor` / `exhausted` / `gap_detected`。游标对外为不透明字符串，原样回传 `next_cursor` 继续读取；事件游标不连续时 `gap_detected=True` 显式标记（缺失不可补齐）。流式消费用 `stream_exec_logs`。

### 凭据与端点安全

- `Client(..., token_provider=lambda: get_short_lived_token())`：每次请求发出前取值（含重试），优先于固定 token。
- Bearer 只发往配置的 `base_url`，不随重定向转发（3xx 按错误返回）。
- `base_url` 必须是无内嵌凭据、无路径/查询/片段的 HTTP(S) 绝对地址；非 HTTPS 仅允许回环地址。
- token 拒绝空白边缘与 CR/LF（防头注入）。

### 依赖构建

依赖构建必须绑定已解析环境：

```python
resolution = client.resolve_environment(hints=["runtime.python"], strict=True, ttl_seconds=1800)
build = client.build_dependencies(
    resolution_id=resolution["resolution_id"],
    language="python",
    lockfile=requirements_lock,
)
```

从 Catalog 选择 Card 时，用 `resolve_environment(profile=card["name"], profile_revision=card["profile_revision"])` 精确绑定后再构建。

## 从旧版迁移（0.2.x → 0.3.0）

SDK 已从 `genesis-sandbox` 仓库迁至本仓库，导入名从 `sandbox` 改为 `genesis_sandbox_client`：

```python
# 旧
from sandbox import Client, new_sandbox

# 新
from genesis_sandbox_client import Client, new_sandbox
```

公开 API（类、函数、异常、语义）保持不变。

## License

Apache-2.0
