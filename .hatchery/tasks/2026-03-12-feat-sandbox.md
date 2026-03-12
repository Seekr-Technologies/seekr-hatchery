# Task: feat-sandbox

**Status**: complete
**Branch**: hatchery/feat-sandbox
**Created**: 2026-03-12 14:32

## Objective

Add a `hatchery sandbox` command that drops the user into an interactive /bin/bash shell inside the Docker sandbox container, allowing them to inspect the exact environment agents see, check available tools, and debug the sandbox.

## Summary

### Key decisions

- **`_interactive` parameter on `_run_container()`**: Rather than creating a separate function, added a `_interactive: bool = False` parameter to the existing `_run_container()`. When `_interactive=True` with `_command_override`, `-it` flags are added and `subprocess.run` is called without output capture (returns `None`). Default `False` preserves all existing behavior for tests and agent launches.

- **Minimal `launch_sandbox_shell()`**: Deliberately kept simple — no task metadata, no proxy, no session directory, no sentinel files. Just builds the image, mounts the repo read-only at `/repo` with default home mounts and docker config mounts, and drops into the shell.

- **`cmd_sandbox()` CLI**: No task name argument required. Uses `detect_runtime()` directly (not `resolve_runtime()` which requires a worktree). Checks for Dockerfile existence with a clear error message pointing to `hatchery new`.

### Files changed

| File | Change |
|---|---|
| `src/seekr_hatchery/docker.py` | `_interactive` param on `_run_container()`; new `launch_sandbox_shell()` |
| `src/seekr_hatchery/cli.py` | New `cmd_sandbox()` command with `--shell` option |
| `tests/test_docker.py` | `TestRunContainerInteractive` — 5 tests for `-it` flags, capture behavior, return value |
| `tests/test_cli.py` | `TestSandbox` — dispatch, custom shell, missing Dockerfile error; updated help test |
| `tests/test_sandbox.py` | `TestSandboxShell` — integration test for `_interactive=True` code path |

### Verification

- Unit tests: `uv run pytest tests/test_docker.py tests/test_cli.py -v`
- Integration: `uv run pytest tests/test_sandbox.py --integration -v`
- Manual: `hatchery sandbox` in a repo with a Dockerfile drops into bash at `/repo`
