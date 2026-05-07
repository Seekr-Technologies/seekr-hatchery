# Task: fix-wrong-mount-path

**Status**: complete
**Branch**: hatchery/fix-wrong-mount-path
**Created**: 2026-05-07 15:14

## Objective

Running `hatchery new <name> --no-worktree` from within a linked git worktree mounted the main repository root instead of the current worktree directory in the Docker container.

## Context

`git_root_or_cwd()` always calls `_resolve_main_repo()`, which resolves linked worktrees back to the main repo root. This is correct for normal worktree mode (needs the main repo to create new worktrees) but wrong for `--no-worktree`, where the user expects their current directory to be mounted.

## Summary

**Root cause:** In `cli.py`, the `--no-worktree` path set `worktree = repo`, but `repo` was already resolved to the main repository root by `git_root_or_cwd()` → `_resolve_main_repo()`.

**Fix (2 files changed):**

1. **`src/seekr_hatchery/git.py`** — Added `git_toplevel_or_cwd()`, which returns the raw `git rev-parse --show-toplevel` result without resolving linked worktrees to the main repo. Same signature as `git_root_or_cwd()` but skips `_resolve_main_repo()`.

2. **`src/seekr_hatchery/cli.py:779`** — Changed the `--no-worktree` assignment from `worktree = repo` to `worktree = git.git_toplevel_or_cwd()[0] if in_repo else repo`. This uses the unresolved toplevel (the actual worktree dir) when in a git repo, and falls back to `repo` (which is `cwd`) outside a git repo.

**Behavior matrix:**
- Normal worktree mode: unchanged (uses `repo` = main repo root)
- `--no-worktree` in linked worktree: now correctly uses the linked worktree dir
- `--no-worktree` in main repo: unchanged (toplevel == main repo root)
- Not in a git repo: unchanged (uses `cwd`)

All 618 tests pass, 13 skipped.
