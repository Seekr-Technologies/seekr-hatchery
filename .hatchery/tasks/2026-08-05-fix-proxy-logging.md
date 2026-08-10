# Task: fix-proxy-logging

**Status**: complete
**Branch**: hatchery/fix-proxy-logging
**Created**: 2026-08-05 15:30

## Objective

I get a visual bug every once in a while where the proxy logs like ssl errors are shown in the same chat as claude code. It resolves itself when I resize. Otherwise the text is completely manged. I will attach screenshots for you to see. You should investigate how the proxy logs, ensure they are being sent to the log file and not to stdout

## Context

Screenshots the user attached pinned the actual bug: repeated `urllib3`
`InsecureRequestWarning` lines ("Unverified HTTPS request is being made to
host 'api.anthropic.com'...") printed raw to stderr. This was **not** the
`logger.*` calls in `proxy.py`/`kubectl_proxy.py`, which already went through
`logging_.py` correctly — it was Python's `warnings` module, which writes
straight to `sys.stderr` via `warnings.showwarning`, completely bypassing the
logging pipeline.

Root cause: `api_server()` in `proxy.py` builds its outbound
`urllib3.PoolManager` with a `truststore.SSLContext`. `truststore`
intentionally sets the underlying `ssl.SSLContext.verify_mode` to
`CERT_NONE` (it does its own verification against the OS trust store
post-handshake), so urllib3's `conn.is_verified` check concludes —
incorrectly — that the connection is unverified and fires
`InsecureRequestWarning` on every connection to `api.anthropic.com`. Since
this proxy runs in a background thread of the host process while the
container is attached to the same TTY (`subprocess.run([..., "exec", "-it",
...])`), the raw stderr write corrupts the container's TUI until the next
redraw (resize) — and it slipped past `detach_console_handler()`
(`cli.py:219`), which exists specifically to prevent console output from
corrupting the agent's TUI once the sandbox launches, because that function
only ever touched the `seekr_hatchery` logger, not `warnings`.

## Summary

**Fix:**
1. `src/seekr_hatchery/logging_.py` — added a `py.warnings` logger
   (`_warnings_logger`) managed alongside `_pkg_logger` via a shared
   `_MANAGED_LOGGERS` tuple. `configure_logging()` now attaches the same
   console/file handlers to both and calls `logging.captureWarnings(True)`,
   so `warnings.warn()` routes through the existing logging pipeline instead
   of stderr. `detach_console_handler()` and `task_log()` were updated to
   manage both loggers identically, so warnings behave exactly like any
   other log line (visible pre-launch, silent + file-only after the agent
   sandbox launches).
2. `src/seekr_hatchery/proxy.py` and `src/seekr_hatchery/kubectl_proxy.py` —
   overrode `handle_error()` on each `_ThreadingHTTPServer` subclass to log
   via the module's `logger.warning(..., exc_info=True)` instead of
   `socketserver.BaseServer`'s stdlib default, which also prints tracebacks
   straight to stderr (e.g. on TLS handshake failures against the
   TLS-terminating kubectl RBAC proxy). Same bug class, same fix shape,
   fixed defensively even though the screenshots pointed at the `warnings`
   path specifically.
3. `tests/test_logging.py` — added `TestWarningsCapture` with two tests
   (warnings reach the log file; warnings stay off stderr after
   `detach_console_handler()`), plus a `clean_warnings_logger` fixture.

**Gotcha for future agents:** `logging.captureWarnings()` has a
process-global idempotency guard (`logging._warnings_showwarning`) — once
set, later calls no-op. Meanwhile pytest wraps every test in its own
`warnings.catch_warnings()`, which resets `warnings.showwarning` back to the
stdlib default at each test's teardown. Combined, this meant that after the
*first* test in a session called `configure_logging()`, every later test's
call became a no-op and `warnings.warn()` silently fell back to raw stderr
output — a pure test-ordering artifact, not a real bug (in production,
`configure_logging()` runs exactly once per CLI invocation). The
`clean_warnings_logger` fixture works around it by calling
`logging.captureWarnings(False)` before *and* after each test to force a
clean re-arm.

**Verified:** full test suite (977 passed, 21 skipped, no regressions), plus
a manual repro script simulating CLI startup → pre-launch warning (visible
on console, as intended) → `detach_console_handler()` → post-launch warning
(silent on console, present in `hatchery.log`) — matching the exact failure
mode from the screenshots.
