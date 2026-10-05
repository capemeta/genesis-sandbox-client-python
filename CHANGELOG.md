# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

### Added

- `Client.get_runtime_control_capabilities` 单次、有界只读查询服务实际运行前提；
  不创建实例、不启动执行，也不将控制可达或 Docker 配置事实视为隔离验收。

- `Client.list_session_execs`（GET /v1/sessions/{id}/execs）：分页列出会话执行记录，
  limit 严格校验 1..100（bool/越界/浮点直接 ValueError，不发请求），cursor 为不透明
  游标；session_id 作为路径段整体编码。`SandboxSession.list_execs` / `AsyncSandboxSession.list_execs`
  提供会话内便捷包装（默认 limit=50）。

## v0.3.0 - 2026-10-01

### Changed

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

### Removed

- `conftest.py` sys.path hack and example sys.path bootstrap (replaced by the
  src layout with an editable install).

## v0.2.0

History from the monorepo era (`genesis-sandbox/sdks/python`): session lookup
recovery, cancel receipts (stop-confirmed gating), budgeted log pagination,
credential rotation via `token_provider`, and endpoint security constraints.

## v0.1.0

Initial monorepo SDK: sync/async session helpers with heartbeat, quick job
helpers, typed errors with `classify_error`, SSE log streaming.
