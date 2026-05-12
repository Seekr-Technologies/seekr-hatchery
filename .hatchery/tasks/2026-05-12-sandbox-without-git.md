# Task: sandbox-without-git

**Status**: complete
**Branch**: hatchery/sandbox-without-git
**Created**: 2026-05-12 11:21

## Objective

Support running `hatchery new` and `hatchery chat` in a Docker sandbox even when the
current directory is not a git repository.

## Context

Previously, the non-git path in `cmd_new` forced both `no_worktree = True` and
`no_docker = True`, falling back to a fully unsandboxed native run. The same path in
`cmd_chat` hard-errored with "chat requires a git repository".

The Docker `--no-worktree` path (`launch_docker_no_worktree`) mounts the CWD as
`/workspace` and has no git dependency. There was no technical reason to skip it.

## Summary

**Decision: use `.hatchery/` in the CWD** (not a global dir). This is already where
the existing code places task files and Docker config when not in a git repo. It keeps
the same structure users see in a git-repo project, and the Docker build context must
be a sensible working directory.

**Files changed:**

- `src/seekr_hatchery/cli.py`
  - `cmd_new()`: removed `no_docker = True` from the non-git branch; updated the note
    message (no longer mentions "Docker sandbox"); added an `else` block that calls
    `ensure_dockerfile` / `ensure_docker_config` without git-committing them; gated
    `get_default_branch` behind `in_repo`.
  - `cmd_chat()`: removed the hard `sys.exit(1)` for non-git repos; replaced with the
    same `ui.note()`; gated the git add/commit block behind `in_repo`; gated
    `get_default_branch` behind `in_repo`.

- `tests/test_cli.py`
  - Updated `test_auto_enable_prints_note_when_not_in_repo` to assert the new message
    does not contain "Docker sandbox".
  - Added `test_not_in_repo_docker_files_are_created`: ensures `ensure_dockerfile` and
    `ensure_docker_config` are called when not in a git repo.
  - Added `test_not_in_repo_no_docker_flag_skips_docker_files`: ensures those functions
    are NOT called when `--no-docker` is passed.

**Gotcha:** `get_default_branch` runs git commands with `check=False` and returns `"main"`
gracefully when git fails, so it would not crash in non-git mode — but it's semantically
wrong to call it, and gating it produces a cleaner empty-string default.
