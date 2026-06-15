# Task: fix-resume

**Status**: complete
**Branch**: hatchery/fix-resume-2
**Created**: 2026-06-15 09:14

## Objective

`hatchery resume <name>` hard-exited in several degraded states beyond
the missing-task-file case already handled on the (not-yet-merged)
`hatchery/fix-resume` branch:

- The **worktree directory** was deleted on an `in-progress` /
  `running` task → `ui.error` + `sys.exit(1)`.
- The **git branch** named in `meta.branch` was deleted → on
  archived/complete tasks the auto-recreate hit `git.create_worktree`'s
  `"invalid reference"` exit.
- The **session_id** was missing → hard exit even though the worktree
  and task file were still usable.
- The **task file** was missing → hard exit (from the pre-existing
  bug fixed on `hatchery/fix-resume` but not yet on `main`).

Goal: turn each of those into a graceful, recoverable path so the agent
can launch and the user can decide what to do next. Fold the
`hatchery/fix-resume` change in so this PR is self-contained.

## Context

Resume runs `cmd_resume` in `src/seekr_hatchery/cli.py`. The previous
implementation interleaved a worktree check and a session-id check, each
calling `sys.exit(1)` on miss. Recreating a worktree relied on
`meta.branch` existing as the base ref; there was no pre-check so a
deleted branch surfaced as a confusing `"invalid reference"` from
`git.create_worktree`. The task-file fallback existed only on a parallel
unmerged branch.

The user explicitly excluded two other failure modes from scope:
corrupted `meta.json` and missing include-repo branches.

## Summary

### Decisions

**Three explicit gates, composed top-to-bottom.** `cmd_resume` now
checks worktree, branch, and session_id as separate steps that flow
into a single `_launch()` call. Each gate either degrades cleanly or
asks the user for explicit consent. The gates compose: a missing
worktree on an in-progress task that also has a deleted branch goes
through both the y/N confirm *and* the recreate-from-default path.

**Worktree gate behavior depends on status.** Archived/complete tasks
still auto-recreate without prompting (existing flow — the archive
contract is "removable + restorable"). In-progress / running tasks
prompt `[y/N]` because silently rebuilding the worktree could mask
unexpected loss of work; the user must opt in.

**Missing branch recreates from the default branch.** When
`git.branch_exists` is False, the worktree is rebuilt from
`git.get_default_branch(repo)` (main/master/develop) with a `ui.warn`
and a `prompt_note` threaded into the agent's initial prompt so it
knows the worktree may not contain prior work. The `meta.branch` is
still used as the *target* branch name (so the recreated work lands on
the same branch label).

**Missing session_id falls back to `kind="new"`.** Rather than
inventing a synthetic session ID (which the backend would ignore
anyway), we launch the backend's `build_new_command` with the existing
worktree and task file. The agent gets full context; only the
agent-side conversation history is lost.

**Prompt-note plumbing is keyword-only and optional.**
`sessions.session_prompt(name, worktree, extra_note="")` and
`sessions.launch(..., prompt_note="")` both gained a default-empty
keyword. Every existing caller is unchanged. The note is prepended
verbatim to the agent's initial prompt — separated by a blank line —
when non-empty.

### Files changed

- `src/seekr_hatchery/git.py` — added `branch_exists(repo, branch)`
  using `git rev-parse --verify --quiet refs/heads/<branch>`. Pure
  read-only — no `ui.*`, no `sys.exit`. The `refs/heads/` qualifier
  prevents a same-named tag from satisfying the check.
- `src/seekr_hatchery/sessions.py` — `session_prompt()` no longer
  exits on missing task file; emits `ui.warn` and returns a fallback
  prompt that names the task and lists common causes. New optional
  `extra_note` kwarg is prepended when set. `launch()` gained a
  matching `prompt_note` kwarg that threads through to
  `session_prompt`.
- `src/seekr_hatchery/cli.py` — `cmd_resume` rewritten as three
  gates (worktree → branch → session_id). `_launch` gained
  `prompt_note` kwarg that forwards into `sessions.launch`. The y/N
  prompt reuses the existing pattern from `_do_delete`
  (`input(...).strip().lower() == "y"`).
- `tests/test_git.py` — `TestBranchExists` with three cases
  (existing branch, missing branch, same-named tag).
- `tests/test_pure.py` — replaced `test_file_not_found_exits` with
  `test_file_not_found_returns_fallback_prompt` +
  `test_file_not_found_when_tasks_dir_absent`; added two
  `extra_note` tests.
- `tests/test_session_io.py` — `TestSessionLaunch._drive` accepts
  `prompt_note`; new `test_resume_succeeds_when_task_file_missing`
  and `test_resume_prompt_note_prepended`.
- `tests/test_cli.py` — five new `TestCliResume` cases: missing-wt
  confirm yes, missing-wt confirm no, missing-branch from-default,
  missing-wt+missing-branch composition, missing-session_id →
  `kind="new"`.

### Gotchas / notes for future agents

- `prompt_note` is keyword-only on both `sessions.launch` and
  `cli._launch`. If you add another caller, remember to pass it
  through — silent loss would degrade the agent's context only in the
  recreated-branch case, so a test won't necessarily catch it. The
  `test_resume_prompt_note_prepended` test guards the
  `launch → session_prompt` link; `test_resume_missing_branch_recreates_from_default`
  guards the `cmd_resume → _launch` link.
- The y/N prompt only fires for in-progress / running statuses. If you
  change the archived/complete auto-recreate to also confirm, update
  the `test_resume_missing_branch_recreates_from_default` test — it
  uses `status="archived"` precisely to avoid the prompt.
- `git.create_worktree` still calls `sys.exit(1)` on `"invalid
  reference"` — we just avoid hitting that path by pre-checking with
  `branch_exists`. Don't be tempted to also catch the exit inside
  `create_worktree`; the precheck is cleaner and other call sites
  (e.g. `new`) want the hard exit.
- `branch_exists` qualifies with `refs/heads/`. If you ever need to
  also accept remote-tracking branches or tags, add a separate helper
  rather than weakening this one — `cmd_resume`'s recovery logic
  assumes a local branch name.
- The previous `hatchery/fix-resume` branch can be closed/superseded;
  its `session_prompt` change is now folded in here. Its task file
  (`.hatchery/tasks/2026-06-01-fix-resume.md`) is *not* on `main`, so
  it does not need to be removed from this branch.
- `test_kubectl_proxy` SIGILLs on the sandbox host (unrelated arch
  mismatch). Run `uv run pytest --ignore=tests/test_kubectl_proxy.py`.
  All 748 other tests pass.
