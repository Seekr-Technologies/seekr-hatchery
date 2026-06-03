# Task: fix-resume

**Status**: complete
**Branch**: hatchery/fix-resume
**Created**: 2026-06-01 15:55

## Objective

`hatchery resume <name>` failed hard with `Error: task file not found
for '<name>' in .../​.hatchery/tasks` whenever the task's markdown file
wasn't present in the worktree's `.hatchery/tasks/` directory. This
happens during ordinary use — the agent may have switched branches,
deleted/renamed the file, or used the same worktree for several
tasks. The metadata, session ID, and worktree are all still intact in
those cases, so resume should succeed regardless of working-tree
state.

## Context

`session_prompt()` in `src/seekr_hatchery/sessions.py` called
`sys.exit(1)` when `find_task_file()` returned `None`. It is invoked
unconditionally from `sessions.launch()` for both `new` and `resume`
kinds, so a missing task file on resume was unrecoverable. The new
flow can't trigger it (the file is always freshly written), but
resume legitimately can.

## Summary

### Decision

When the task file is missing on resume, **degrade gracefully** —
don't try to recover the file from git, don't switch branches, don't
re-create the file. Emit a warning and return a short fallback prompt
that tells the agent the file is missing and asks it to check
`git status`/branch state before doing further work. This matches
the user's stated goal of "resuming on the worktree that exists on
whatever branch it is on" and keeps the change tiny.

We considered (and rejected) recovering the task file content from
`meta.branch` via `git show`. The agent can do that itself if useful,
and the simpler fix is easier to reason about.

### Files changed

- `src/seekr_hatchery/sessions.py` — `session_prompt()` no longer
  exits on missing file. It calls `ui.warn(...)` and returns a
  fallback prompt that names the task, lists common causes, and
  instructs the agent to inspect git state and ask the user. Signature
  unchanged.
- `tests/test_pure.py` — `TestSessionPrompt.test_file_not_found_exits`
  replaced by `test_file_not_found_returns_fallback_prompt` plus a
  second test for the case where `.hatchery/tasks/` itself doesn't
  exist.
- `tests/test_session_io.py` — added
  `TestSessionLaunch.test_resume_succeeds_when_task_file_missing`,
  which exercises `sessions.launch(kind="resume")` end-to-end with no
  task file present and asserts the resume command is built with the
  fallback prompt content.

### Gotchas / notes for future agents

- `session_prompt()` is still called for `kind="new"` — that path is
  unaffected because `write_task_file()` runs before `launch()` and
  the file is always present at that point.
- Three other callers of `find_task_file()` (in `cli.py`) already
  handled `None` correctly (`_do_mark_done`, `_post_exit_check`,
  `cmd_status`); they were intentionally left untouched.
- The fallback prompt uses `"not present"` and includes the task name
  so the agent has enough to act on. If you change the wording, update
  the assertions in `TestSessionPrompt` and the new
  `TestSessionLaunch` test (they look for `"not present"`/`"missing"`
  and the task name).
- Full pytest suite passes (702 passed, 13 skipped). `test_kubectl_proxy`
  crashes with SIGILL on this sandbox host due to a pre-existing
  architecture mismatch in the proxy binary — unrelated to this
  change. Run the suite with `--ignore=tests/test_kubectl_proxy.py`
  if you hit the same crash here.
