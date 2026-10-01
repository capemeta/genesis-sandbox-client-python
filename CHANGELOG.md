# Changelog

All notable changes to this project will be documented in this file.

## v0.3.0 - 2026-10-01

### Changed

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
