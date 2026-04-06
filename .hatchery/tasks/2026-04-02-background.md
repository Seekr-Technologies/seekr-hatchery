# Task: background

**Status**: complete
**Branch**: hatchery/background
**Created**: 2026-04-02 09:14

## Objective

Currently, tasks only run in the foreground. We should allow tasks which are running in sandboxes to run in the background.

- When launching a task, it should start in the background
- We should attach to the container

This would then let tasks continue running in the background

## Summary

### What was done

Tasks now start in **detached mode** (`docker run -d -it`) and hatchery immediately attaches to the container (`docker attach`). When the user detaches with Ctrl+P, Ctrl+Q the container keeps running and the host-side API proxy stays alive until the container exits.

### Status model

Renamed the two existing statuses and added a third:

| Status | Meaning |
|--------|---------|
| `attached` | Container running, user is attached (was `running`) |
| `background` | Container running, user detached; proxy alive on host |
| `paused` | Container not running; task can be resumed (was `in-progress`) |

### Key decisions

- **`docker run -d -it --name <container_name>`** — allocating a TTY while starting detached is the correct pattern for `docker attach` to work. The `-it` and `-d` flags are fully compatible.
- **Container name**: `hatchery-{to_name(repo.name)}-{task_name}` — deterministic, derived from the same slugification already used for image names.
- **Proxy lifetime**: The proxy runs as a daemon thread (unchanged). The `_run_container_background` helper calls `docker wait` after the user detaches, keeping the hatchery process (and proxy thread) alive until the container exits. If the user Ctrl+C's the monitor loop, the proxy stops — the container may continue but will lose API access.
- **`on_detach` callback**: `launch_docker` / `launch_docker_no_worktree` accept `on_detach: Callable[[], None] | None`. The CLI passes a closure that sets the task status to `background`. This keeps the status machinery in `cli.py` rather than coupling `docker.py` to task state.
- **`hatchery resume` on background task**: If a task is in `background` status and its container is still running, `cmd_resume` re-attaches directly instead of starting a new agent session.

### Files changed

- `src/seekr_hatchery/docker.py` — `docker_container_name()`, `_is_container_running()`, `_run_container_background()` added; `_run_container()`, `launch_docker()`, `launch_docker_no_worktree()` updated.
- `src/seekr_hatchery/cli.py` — status renames, `on_detach` wiring in `_launch_new`/`_launch_resume`/`_launch_finalize`, `cmd_resume` background re-attach logic.
- `src/seekr_hatchery/ui.py` — new match cases for `attached`, `background`, `paused`.
- `src/seekr_hatchery/tasks.py` — task file template updated to use `paused`.
- `tests/` — all status literal strings updated; new tests for `docker_container_name`, `_is_container_running`, `_run_container_background`, and the `on_detach` status transition.

### Gotchas for future agents

- `docker run -d -it` works correctly (container has TTY but is detached). Don't remove `-it` when adding `-d`.
- `docker wait <name>` blocks until the container stops. With `--rm` the container is removed immediately after stopping, but `docker wait` returns before removal, so there's no race.
- If `docker run` fails with "already in use" AND `_is_container_running` returns True, `_run_container_background` skips to `docker attach` — this provides basic resume-by-relaunch behaviour.
- `command_override` / sandbox shell paths still use the old foreground `subprocess.run(cmd)` because `container_name=None` is the default.
