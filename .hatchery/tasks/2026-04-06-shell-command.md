# Task: shell-command

**Status**: complete
**Branch**: hatchery/shell-command
**Created**: 2026-04-06 09:40

## Objective

Add a `hatchery shell <task>` command that spawns a new nested shell in the task's worktree — a normal host shell (not Docker sandbox). Exit with `Ctrl-D` / `exit`.

## Context

Users working with hatchery tasks frequently need to inspect files, run git commands, or do manual work in a task's worktree. Manually `cd`-ing to `.hatchery/worktrees/<name>` is friction. This command provides a direct ergonomic shortcut.

The existing `hatchery sandbox` command opens a Docker shell — `hatchery shell` is the complementary host-side command for a named task.

## Summary

**Single change:** Added `cmd_shell` to `src/seekr_hatchery/cli.py` (after `cmd_status`, before the `config` group).

**Key decisions:**
- Uses `subprocess.run([shell], cwd=worktree)` directly — not `tasks.run()` — because `tasks.run` captures stdout/stderr, which would break the interactive shell.
- Reads `$SHELL` env var with `bash` fallback, consistent with the task spec.
- Validates worktree existence before spawning; prints a friendly error and exits 1 if missing (e.g. archived task without a worktree).
- No `check=True` — the user's shell exit code is not meaningful to hatchery.
- Pattern follows `cmd_done`/`cmd_archive`: `tasks.load_task(repo, name)` → `Path(meta["worktree"])`.

**Files changed:**
- `src/seekr_hatchery/cli.py` — added `cmd_shell` command (~12 lines)
