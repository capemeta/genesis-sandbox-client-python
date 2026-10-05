# 更新记录

## Unreleased

暂无待发布变更。

## 0.1.0（首次公开发布候选）

### 首次交付

- 独立 Python 3.12+ SDK，运行时仅依赖标准库，分发包名为 `genesis-sandbox-client-python`，导入名为 `genesis_sandbox_client`。
- 同步/异步会话、Job、执行回执恢复、取消停止确认、心跳、预算受控的日志读取与 WorkspaceFS。
- 工作区生命周期、共享存储绑定、执行治理、输出目录及运行控制能力查询。
- `Client.list_session_execs` 与同步/异步会话 `list_execs` 提供只读分页查询。
- 写请求仅发送一次；未知结果按原身份查询，禁止自动重放未知副作用。
- GitHub Actions 标签发布、PyPI Trusted Publishing，以及独立 wheel 安装与协议校验。

### 版本说明

本版本汇总单仓阶段和独立仓开发阶段的能力，是独立 Python 分发包的首次公开发布候选。此前记录中的 `0.1.x`、`0.2.x`、`0.3.x` 属于内部开发编号，不表示已经发布到 PyPI。版本号无需与服务端或其他语言 SDK 一致。

## 内部开发历史（不属于 PyPI 公开版本）

保留迁移与行为调整记录，以下编号不是本分发包的公开发布序列。

### 内部开发 0.3.0 - 2026-10-01

#### Changed

- Added read-only `Client.list_session_execs` and sync/async session `list_execs` pagination.
- Mutations are sent once even with an idempotency key; unknown outcomes must be resolved by querying the original identity.

- SDK extracted from the `genesis-sandbox` monorepo (`sdks/python`) into this
  standalone uv-managed repository. Public API surface is unchanged.
- **Breaking (import path only):** the import package is renamed from `sandbox`
  to `genesis_sandbox_client`; the distribution name is
  `genesis-sandbox-client-python`. The old top-level name was too generic and
  collided with third-party PyPI packages.
- Modernized typing to the Python 3.12 baseline: builtin generics
  (`dict`/`list`), PEP 604 unions (`X | None`), `collections.abc.Callable`,
  and full annotations across the public `Client` surface.
- `wait_exec` / `wait_viewer` now use `time.monotonic()` instead of wall-clock
  time, so NTP adjustments cannot distort bounded waits.
- `renew_session` default `max_retries` changed from 5 to 1, matching the
  write-retry discipline that was already enforced internally (identity-less
  POSTs are sent exactly once); the session heartbeat loop owns the retry
  cadence.
- `_types` module renamed to public `types`.

#### Removed

- `conftest.py` sys.path hack and example sys.path bootstrap (replaced by the
  src layout with an editable install).

### 内部开发 0.2.0

History from the monorepo era (`genesis-sandbox/sdks/python`): session lookup
recovery, cancel receipts (stop-confirmed gating), budgeted log pagination,
credential rotation via `token_provider`, and endpoint security constraints.

### 单仓初始开发 0.1.0

Initial monorepo SDK: sync/async session helpers with heartbeat, quick job
helpers, typed errors with `classify_error`, SSE log streaming.
