# Task: fix-from-head

**Status**: complete
**Branch**: hatchery/fix-from-head
**Created**: 2026-09-10 13:00

## Objective

Provide an actionable error when task creation uses the default base in an empty Git repository.

## Context

An empty repository has an unborn `HEAD`, so `git worktree add ... HEAD` cannot resolve a base ref. The absence of a remote alone is valid when a local commit exists. Creating an orphan branch would create unrelated history, while automatically making an initial commit would unexpectedly mutate the user's repository.

## Summary

- `git.create_worktree()` now detects an unborn default `HEAD` before invoking `git worktree add` and instructs the user to make an initial commit.
- Existing invalid-ref handling remains unchanged for other base refs, including explicit `--from` values.
- Added a real-Git regression test for an empty repository; `tests/test_git.py` and Ruff checks pass.
