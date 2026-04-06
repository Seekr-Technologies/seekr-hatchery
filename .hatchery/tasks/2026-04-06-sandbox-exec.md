# Task: sandbox-exec

**Status**: complete
**Branch**: hatchery/sandbox-exec
**Created**: 2026-04-06 09:42

## Objective

Add a way to drop into the shell of a running task's container for inspection/debugging, rather than only supporting a fresh blank sandbox.

## Context

`hatchery sandbox` drops you into a fresh Docker container for exploration. The gap: when an agent is actively running, there's no first-class way to get a shell inside that container without manually finding the container ID via `docker ps`. The most natural name for this is `hatchery exec <task>`, mapping directly to `docker exec` semantics.

## Summary

Added `hatchery exec <task>` — execs an interactive shell into the running container of an in-progress task.

**Key decisions:**

- Named `exec`, not `sandbox <task>`, because it literally does `docker exec` and the semantics are unambiguous. `sandbox` remains a "fresh shell" command.

- Containers were previously unlabeled, making them undiscoverable by task name. Added `--label hatchery.task=<name>` and `--label hatchery.repo=<repo_id>` to every container start (via `_run_container`'s new optional `repo_label` param). Two labels are needed: task name alone collides when the same name exists in multiple repos on the same machine. `repo_id` is the stable `<basename>-<sha256[:8]>` hash already used for metadata storage.

- `exec_task_shell(name, runtime, repo, shell)` does `docker ps --filter label=hatchery.task=<name> --filter label=hatchery.repo=<repo_id>` to find the container, then `docker exec -it <id> <shell>`. No task metadata lookup — a clear "no running container" error is sufficient.

**Files changed:**
- `src/seekr_hatchery/docker.py`: `repo_label` param on `_run_container`; label injection; `exec_task_shell()`; `launch_docker` and `launch_docker_no_worktree` pass `tasks.repo_id(repo)`
- `src/seekr_hatchery/cli.py`: new `cmd_exec` command
- `tests/test_pure.py`: `TestRunContainerLabel`, `TestExecTaskShell`
- `tests/test_cli.py`: `TestExec`; updated `TestHelp` to include `exec`

**Gotchas:**
- `docker ps --filter ancestor=<image>` was considered as an alternative to labels but is less reliable across Docker/Podman versions.
- `_run_container` is a private function; `repo_label` is optional (defaults `None`) so tests that call it directly don't need to change.
- `launch_sandbox_shell` also gets the `hatchery.task=sandbox` label now — harmless, and makes the labeling consistent.
