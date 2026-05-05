# Task: fix-resume-no-docker

**Status**: complete
**Branch**: hatchery/fix-resume-no-docker
**Created**: 2026-05-01 18:12

## Objective

Sometimes, users delete dockerfiles out of task branches (because they don't want them in a PR).

When resuming a task where the dockerfile is missing, we should:

- Check for dockerfile in repo root
- If missing, run normal dockersetup in repo root. DO NOT COMMIT.
- then, copy dockerfile/config into the task worktree. DO NOT COMMIT here either.
- Startup normally

## Summary

### What changed

`cmd_resume()` in `cli.py` now checks for a missing Dockerfile in the worktree before calling `resolve_runtime()`. If missing (and `--no-docker` was not passed), it restores the Docker files from the repo root — generating from template if the repo root also lacks them — without committing to either location.

### Key decisions

- **Extracted shared helper**: `ensure_docker_files_uncommitted()` in `docker.py` encapsulates the 4-call pattern (ensure in repo root, copy to worktree via `source=`). This is reused by both `cmd_new`'s `--no-commit-docker` path and `cmd_resume`'s restoration logic, eliminating duplication.
- **No-commit semantics only**: The helper is specifically for the uncommitted case. `cmd_new`'s commit path (generate directly in worktree, return True so caller commits) has different semantics and was left as-is.
- **Interactive prompts preserved**: If the repo root also lacks Docker files (unlikely for a resume, but possible), the template generation will prompt the user to edit — same UX as initial setup.

### Files changed

- `src/seekr_hatchery/docker.py` — added `ensure_docker_files_uncommitted()`
- `src/seekr_hatchery/cli.py` — added restoration block in `cmd_resume()`, refactored `cmd_new()` no-commit path to use the helper
- `tests/test_docker.py` — 3 tests for the new helper
- `tests/test_cli.py` — 3 tests for resume restoration (missing dockerfile, present dockerfile, --no-docker flag)

### Gotchas

- The helper always ensures repo root has Docker files first, even if the worktree already has them. The individual `ensure_*` functions no-op when the target exists, so this is harmless but worth knowing.
- Existing resume tests mock `Path` globally, which makes `dockerfile_path()` return a MagicMock (truthy `.exists()`). The new restoration test explicitly mocks `docker.dockerfile_path` to control the exists check.
