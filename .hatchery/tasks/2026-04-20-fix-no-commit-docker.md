# Task: fix-no-commit-docker

**Status**: complete
**Branch**: hatchery/fix-no-commit-docker
**Created**: 2026-04-20 11:13

## Objective

When running with `--no-commit-docker`, the Dockerfile was being committed as part of the first commit on the branch anyway.

## Context

`hatchery new --no-commit-docker` is designed to keep Docker files uncommitted at the repo root (for local-only use), while still making them available inside the worktree for the Docker build. The existing guard at `cmd_new` lines 604-613 correctly skips the dedicated Docker commit. However, the subsequent task-file commit used `git add .hatchery/`, which swept up the copied Dockerfile into the staging area along with the task file.

**Evidence:** The initial commit on `hatchery/fix-no-commit-docker` (c78dbd5) included both `.hatchery/Dockerfile.claude` and the task file, even though the task was started with `--no-commit-docker`.

## Summary

**Root cause:** `cli.py:631` used `git add .hatchery/` unconditionally for the task-file commit. When `--no-commit-docker` is set, `ensure_dockerfile` copies the Dockerfile into the worktree's `.hatchery/` directory (returning `False` so the Docker commit is skipped), but the broad `git add .hatchery/` captured the copied file anyway.

**Fix:** Scope the add to `.hatchery/tasks/` when `no_commit_docker=True`:
```python
add_path = ".hatchery/tasks/" if no_commit_docker else ".hatchery/"
tasks.run(["git", "add", add_path], cwd=worktree)
```

**Files changed:**
- `src/seekr_hatchery/cli.py` — one-line conditional for `add_path`
- `tests/test_cli.py` — extended `test_no_commit_docker_skips_dockerfile_commit` to assert that `git add .hatchery/tasks/` is called (not `git add .hatchery/`) when the flag is set

**Gotcha for future agents:** The existing test `test_no_commit_docker_skips_dockerfile_commit` only checked the absence of a dedicated Docker commit — it did not catch the Dockerfile leaking into the task-file commit. Always verify that Docker files don't appear in *any* commit when `--no-commit-docker` is active.
