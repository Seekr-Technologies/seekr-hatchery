# Task: fix-codex-message

**Status**: complete
**Branch**: hatchery/fix-codex-message
**Created**: 2026-09-11 15:45

## Objective

Stop the API proxy from dumping `socketserver` tracebacks into the interactive terminal when an upstream Codex WebSocket connection closes during cleanup.

## Context

The traceback originated at `conn.close()` in the WebSocket branch of `src/seekr_hatchery/sidecars/api_sidecar/proxy.py`. `http.client.HTTPConnection.close()` also closes and flushes its response object. If the WebSocket peer has already reset the socket, that flush can raise `OSError`; because the exception escaped the request handler, `socketserver` printed it to stderr.

## Summary

- WebSocket upstream connection cleanup now catches `OSError` from `conn.close()` and logs a concise warning with the proxy connection ID instead of allowing a handler-thread traceback to reach the terminal.
- Added a regression test in `tests/test_api_sidecar.py` that simulates a reset upstream connection during WebSocket cleanup and verifies the error is logged without escaping the handler.
- `uv run pytest tests/test_api_sidecar.py -q` passed (30 tests), as did focused Ruff format and lint checks for the changed files.
- Repository-wide `pytest` could not complete in this container: importing `cryptography` while setting up the RBAC proxy caused an `Illegal instruction` (exit 132). Repository-wide `ruff format --check .` also reports pre-existing formatting changes in `AGENTS.md` and `.hatchery/tasks/2026-06-02-reduce-virtiofs-burden.md`; neither file was changed by this task.
