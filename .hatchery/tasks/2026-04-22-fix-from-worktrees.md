# Task: fix-from-worktrees

**Status**: complete
**Branch**: hatchery/fix-from-worktrees
**Created**: 2026-04-22 16:10

## Objective

Users cannot start new tasks when invoking `hatchery` from a worktree.

## Context

When a user invokes `hatchery new` from inside a git linked worktree (not the main repo checkout), the container fails immediately with an OCI runtime error:

```
failed to fulfil mount request: open /path/to/worktree/.git/objects: not a directory
```

In a linked worktree, `.git` is a **file** (a gitdir pointer), not a directory. `docker_mounts()` constructs bind-mount paths like `repo/.git/objects` which don't exist as directories on disk, so Docker rejects the mount before the container starts.

`git_root_or_cwd()` uses `git rev-parse --show-toplevel`, which returns the worktree root when run from a linked worktree — so `repo/.git` ends up being the pointer file, not the real git directory.

## Summary

### Key decision: fix at the source, not in docker.py

Rather than adding worktree detection inside `docker_mounts()` or `launch_docker()`, the fix is applied at the `git_root_or_cwd()` / `git_root()` level in `git.py`. When these functions detect that the resolved path's `.git` is a file, they transparently resolve to the main repo root using `git rev-parse --git-common-dir`. All callers (`cli.py`, etc.) are fixed without modification.

This works because hatchery's `.hatchery/` directory, task worktrees, git objects, and refs all live in the main repo. Using the main repo root as `repo` throughout is semantically correct — the user's worktree is just their working context, not the authoritative repo location.

### Files changed

- **`src/seekr_hatchery/git.py`**: Added `_resolve_main_repo(repo)` private helper that calls `git rev-parse --git-common-dir` when `.git` is a file, resolves the path (handling both absolute and relative outputs), and returns the main repo parent. Updated `git_root()` and `git_root_or_cwd()` to call it.
- **`tests/test_git.py`** (new): Unit tests covering normal repo (no-op), linked worktree with absolute common-dir, linked worktree with relative common-dir, fallback on git command failure, and end-to-end `git_root_or_cwd` from a worktree path.

### Gotchas

- `git rev-parse --git-common-dir` can return either an absolute path or a relative path (relative to the worktree root). The helper handles both cases.
- The fix is a no-op for normal checkouts (`.git` is a directory) so there is zero performance impact on the common path.
- All existing tests mock `git_root_or_cwd` at the call site, so they are unaffected by the internal change.
