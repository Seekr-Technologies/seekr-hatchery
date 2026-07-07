# Task: better-logging

**Status**: complete
**Branch**: hatchery/better-logging
**Created**: 2026-07-06 09:17

## Objective

As we perform more complex tasks with hatchery, we need better/transparent logging.

We already have some logging flags, but debugging runtime errors (especially around the proxies) is very hard.

We should:
- Update to log to a file-handler _always_
  - use a log file in the ~/.hatchery dir, alongside the task
- log INFO level always, optional DEBUG
- expand out logging from proxies so we have better signal

## Summary

### Key decisions

1. **Global log file** (`~/.hatchery/hatchery.log`): Chose a single global rotating log file over per-task logs. This covers all commands (including non-task commands like `list`, `status`, `config`) and keeps the implementation simple. A `RotatingFileHandler` (5 MB × 3 backups) prevents unbounded growth.

2. **Dual-level handler design**: The file handler always captures at least INFO, regardless of `--log-level`. The console (stderr) handler respects `--log-level` (default WARNING). The logger level is set to `min(console_level, file_level)` so both handlers receive messages. This means proxy traffic and RBAC decisions are always on disk even when the console is quiet.

3. **Removed `--log-file` flag**: The always-on file handler replaces the opt-in `--log-file` flag. Users who want a different location can symlink or use `hatchery logs`.

4. **Proxy logging at INFO**: API proxy requests, responses, 401 rejections, and 401 credential-refresh retries are now logged at INFO. Upstream errors (502 Bad Gateway) promoted from DEBUG to WARNING. WebSocket upgrade remains at DEBUG. This gives a clear request/response trace in the log file without needing `--log-level DEBUG`.

5. **kubectl RBAC proxy logging at INFO**: RBAC allow/deny decisions and upstream response statuses promoted from DEBUG to INFO. Upstream errors promoted to WARNING. 401 token mismatch was already at WARNING.

6. **`hatchery logs` command**: Added `hatchery logs` (with `-n/--lines` and `-f/--follow`) to make the log file discoverable. Uses `tail` under the hood with a Python fallback.

### Files changed

- `src/seekr_hatchery/cli.py` — new `configure_logging` with always-on `RotatingFileHandler`; removed `--log-file` flag; added `hatchery logs` command; added `LOG_FILE` constant.
- `src/seekr_hatchery/proxy.py` — INFO logging for request/response/401; WARNING for upstream errors.
- `src/seekr_hatchery/kubectl_proxy.py` — INFO logging for RBAC decisions and upstream responses; WARNING for upstream errors.
- `tests/test_logging.py` — new file: tests for `configure_logging` (file handler always created, INFO captured at WARNING console, DEBUG at DEBUG, log file location, append behavior, `--log-file` removed).
- `tests/test_proxy.py` — added `TestProxyLogging` class: 401 rejection at INFO, request/response at INFO, upstream error at WARNING.
- `tests/test_cli.py` — added `logs` to expected command set in help test.
- `README.md` — documented logging behavior, `--log-level`, `hatchery logs` command, and `hatchery.log` in storage layout.

### Patterns established

- The hatchery logger (`logging.getLogger("hatchery")`) is configured once at CLI startup in `configure_logging`. All modules already use this logger — no changes needed outside `cli.py` for the handler setup.
- File handler is wrapped in `try/except OSError` so logging failures never crash the CLI. Console logging still works if the file can't be written.

### Gotchas

- The `home` fixture in `conftest.py` patches `constants.HATCHERY_DIR` at test time, so `LOG_FILE` (computed at import time as `constants.HATCHERY_DIR / "hatchery.log"`) points to the real home in production but is patched via `monkeypatch` in tests. Tests that assert on log file location must patch `constants.HATCHERY_DIR` before calling `configure_logging`.
- The `configure_logging` signature changed from `(level, log_file=None)` to `(level)`. Any external callers (unlikely since this is internal) need updating.
