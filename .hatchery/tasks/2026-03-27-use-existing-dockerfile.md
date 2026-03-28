# Task: use-existing-dockerfile

**Status**: complete
**Branch**: hatchery/use-existing-dockerfile
**Created**: 2026-03-27 16:38

## Objective

Re-implement the lost feature where `hatch new` copies an uncommitted `.hatchery/Dockerfile.<agent>` (and `docker.yaml`) from the repo root into a new worktree instead of generating a fresh template. Also implement `--no-commit-dockerfile` to solve the bootstrap problem for first-time users.

## Context

**Lost feature (from `hatchery/dockerfile` branch, never merged):** commit `53c958d` added `source=repo` logic to `ensure_dockerfile()` and `ensure_docker_config()` so that when creating a worktree, existing uncommitted files in the repo root get copied in rather than overwritten with a template. This branch was closed without merging.

**Bootstrap problem:** Even with the copy logic, a first-time user with no Dockerfile anywhere would get one generated in the worktree and auto-committed — with no way to keep it uncommitted without knowing to act beforehand.

## Summary

### Commit 1: Re-apply the lost copy-from-source feature

**`src/seekr_hatchery/docker.py`:**
- `ensure_dockerfile()` gains `*, source: Path | None = None`. If `source` is provided and a Dockerfile exists there but not in the target `repo`, the file is copied with `shutil.copy2()` and `False` is returned (skipping auto-commit).
- `ensure_docker_config()` gains the same `source` parameter with identical logic.

**`src/seekr_hatchery/cli.py`:**
- `cmd_new()` passes `source=repo` to both calls so the repo root is always checked first.

### Commit 2: `--no-commit-dockerfile` flag

**`src/seekr_hatchery/cli.py`:**
- New `--no-commit-dockerfile` flag on `hatch new`. When set, calls `ensure_dockerfile(repo, backend)` and `ensure_docker_config(repo)` first to generate files at the repo root (prompting the user to edit). Then the existing `source=repo` copy flow carries them into the worktree without committing. Help text notes that `git rm --cached` is needed to undo a missed first run.

**`tests/test_cli.py`:**
- `test_new_help_shows_no_commit_dockerfile_option`
- `test_no_commit_dockerfile_generates_to_repo_root_first` — verifies ensure calls with repo path first, then worktree
- `test_no_commit_dockerfile_skips_dockerfile_commit` — verifies no git commit even when ensure returns True
- `test_no_commit_dockerfile_false_default_commits_when_created` — verifies default behavior unchanged

**`tests/test_filesystem.py`:**
- Source-parameter tests for both `ensure_dockerfile` and `ensure_docker_config`:
  - copies file when present in source
  - returns False when copied (caller skips commit)
  - falls through to template when source has no file
  - ignores source when destination already has the file

### Key design decisions

- The copy returns `False` (not `True`) so the caller's `if df_created or dc_created: git add/commit` block is not triggered — preserving the user's intent to keep the file uncommitted.
- `--no-commit-dockerfile` writes to repo root first, then the `source=repo` copy brings it into the worktree for the current session. The worktree gets the file on disk (needed by `resolve_runtime`) but it is not committed to the branch.
- `shutil` was already imported; no new dependencies.
