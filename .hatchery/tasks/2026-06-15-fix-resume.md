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

Goal: turn each into a graceful, recoverable path so the agent can
launch and the user can decide what to do next. Fold the
`hatchery/fix-resume` change in so this PR is self-contained.

A subsequent code-review pass surfaced 15 additional findings; the
must/should ones are also resolved here.

## Context

Resume runs `cmd_resume` in `src/seekr_hatchery/cli.py`. The previous
implementation interleaved a worktree check and a session-id check,
each calling `sys.exit(1)` on miss. Recreating a worktree relied on
`meta.branch` existing as the base ref; there was no pre-check so a
deleted branch surfaced as a confusing `"invalid reference"` from
`git.create_worktree`. The task-file fallback existed only on a
parallel unmerged branch.

Out of scope (explicitly excluded by user): corrupted `meta.json`,
missing include-repo branches at create time, automatic detection of a
corrupt-but-existing worktree.

## Summary

### Decisions

**Thin CLI glue, recovery logic in `sessions.py`.**
`sessions.restore_worktree_if_needed(meta, *, confirm_recreate)` and
`sessions.resolve_resume_kind(meta)` own the decisions; `cmd_resume`
defines the user-facing `input(...)` callback and threads results into
`_launch`. The two helpers compose: missing worktree + missing branch
goes through the confirm prompt and the recreate-from-default path
inside a single `restore_worktree_if_needed` call.

**Base ref resolved upfront with broad fallback.** `_resolve_recreate_base`
picks the best ref in order: local `meta.branch` → `origin/<meta.branch>`
(after fetch) → local default → `origin/<default>`. This means a
freshly-cloned repo without a local `main` still recovers, and remote-only
work is preserved instead of being silently orphaned. `branch_was_missing`
in the return drives whether the agent gets a `prompt_note` warning.

**Confirm callback consents to the actual action.** The callback signature
is `Callable[[str], bool]` — the resolved `base` is passed in so the y/N
prompt can say "Branch X is also missing — recreate from Y instead?" when
substitution will happen. No more "asked to recreate from X, silently got
Y."

**Status flips also sync the task file's front-matter.** When a
complete/archived task is revived, the worktree's restored file still
carries `**Status**: complete` from the last mark-done. The new
`sessions.update_task_file_status` helper rewrites that line to match
`meta.status` after recreate, preventing `_post_exit_check` from
re-offering "Mark done?" every session.

**Include task branches are preserved on recreate.** `git.create_include_worktrees`
now checks whether the include already has a local `hatchery/<name>`
branch and, if so, attaches the worktree without `-B` so any unmerged
include-side work survives. Fresh include branches still seed from the
include's default.

**`session_id` fallback regenerates and persists.** When `meta.session_id`
is empty, `resolve_resume_kind` generates a fresh uuid, writes it back
to meta via `save()`, and returns `("new", uuid)`. The fallback is
idempotent — subsequent resumes take the normal `("resume", id)` path.

**`prompt_note` plumbing covers resume *and* wrap-up.** `cmd_resume`
threads the note into both the initial `_launch` and (via
`_post_exit_check`) the wrap-up `_launch(kind="finalize")`. `sessions.launch`
prepends it to the `_WRAP_UP_PROMPT` so the wrap-up agent also learns
about degraded recovery (e.g. branch recreated from default).

**Input safety + exit codes at the CLI boundary.** `_confirm_recreate`
catches `EOFError` / `KeyboardInterrupt` from `input()` and raises
`SessionCancelled` instead of leaking a traceback. The
`SessionCancelled` handler in `cmd_resume` calls `sys.exit(1)`
(previously `return` → exit 0), so shell chains like
`hatchery resume foo && next` stop on a user-cancelled resume.

### Files changed

- `src/seekr_hatchery/git.py` —
  - `branch_exists(repo, branch)` — local refs/heads/ lookup, rejects
    empty branch names.
  - `remote_branch_exists(repo, branch, remote="origin")` — refs/remotes/
    lookup; pair with `fetch_remote()` for freshness.
  - `fetch_remote(repo, remote="origin")` — best-effort `git fetch`.
  - `create_include_worktrees` now attaches to an existing
    `hatchery/<name>` branch when present (no `-B` reset).
- `src/seekr_hatchery/sessions.py` —
  - `session_prompt()` — fallback prompt + optional `extra_note` kwarg.
  - `_resolve_recreate_base()` — private helper, four-step base resolution.
  - `restore_worktree_if_needed(meta, *, confirm_recreate)` —
    confirm callback receives the resolved base; calls
    `update_task_file_status` after recreate.
  - `resolve_resume_kind(meta)` — generates + persists a fresh uuid
    when session_id is empty.
  - `update_task_file_status(worktree, name, status)` — rewrites the
    front-matter `**Status**:` line.
  - `launch()` — `prompt_note` kwarg; for `kind="finalize"`,
    `prompt_note` is prepended to `_WRAP_UP_PROMPT`.
- `src/seekr_hatchery/cli.py` —
  - `cmd_resume` — captures `prev_status` before recovery, builds a
    base-aware y/N prompt, wraps `input()` in try/except,
    `sys.exit(1)` on `SessionCancelled`.
  - `_launch` and `_post_exit_check` — `prompt_note` kwarg threaded
    through; wrap-up `_launch(kind="finalize")` passes it down.
- Tests: see `tests/test_git.py`, `tests/test_pure.py`,
  `tests/test_session_io.py`, `tests/test_cli.py`.

### Gotchas / notes for future agents

- **Recovery logic lives in `sessions.py`.** The CLI layer owns only
  the user-facing prompt (the `confirm_recreate` callback). Don't put
  branch / worktree / session_id decision logic back in `cmd_resume`;
  extend `restore_worktree_if_needed` / `resolve_resume_kind` instead.
- **`prompt_note` plumbing is keyword-only and easy to drop.** Three
  call sites carry it: `sessions.launch` ← `cli._launch` ← `cmd_resume`,
  and again through `_post_exit_check` → wrap-up `_launch(kind="finalize")`
  → `sessions.launch`. The chat path (`is_chat=True`) intentionally
  drops it; chats are always `no_worktree=True` so recovery never
  produces a note for them.
- **`branch_exists` is local-only; `remote_branch_exists` is remote-only.**
  Neither fetches on its own. If you need fresh remote refs, call
  `fetch_remote(repo)` first. `_resolve_recreate_base` is the
  canonical caller and already does this.
- **`update_task_file_status` is idempotent and silently no-ops** when
  the file is missing or already declares the target status. It does
  *not* commit the change — the user sees a dirty working tree,
  exactly mirroring what they'd see if mark-done had been called by
  mistake.
- **`create_include_worktrees` now branches on whether the include's
  task branch exists.** If you add a code path that calls it from a
  "fresh task" context, the attach-vs-create-from-base decision will
  still be right (a fresh task has no prior include branch). The
  preserve-existing logic is also reachable from `cmd_new` if a user
  reuses a task name — that's an existing edge case the helper now
  handles more safely.
- **`git.create_worktree` still hard-exits on `"invalid reference"`.**
  We avoid that path on resume by resolving the base upfront. Don't
  catch the exit inside `create_worktree` — `cmd_new` and other
  callers want the hard fail.
- **`session_id` is now mutated and persisted by `resolve_resume_kind`.**
  Callers that load meta, call `resolve_resume_kind`, then re-load
  meta will see the updated session_id (because we save). Code that
  caches the pre-call meta won't see the new id without a re-load.
- **The y/N prompt only fires for in-progress / running statuses.**
  Archived/complete tasks auto-recreate without prompting. If you
  change that, update the tests that use `status="archived"`
  specifically to skip the prompt.
- **Previously unfixed review findings (won't-fix this PR):**
  empty `meta.branch` flows uncaught into git (requires malformed meta;
  `cmd_new` never produces it); `on_new_task` re-firing on resume-as-new
  (latent — Codex's hook is a no-op); corrupt-but-existing worktree
  silently passes `.exists()` (pre-existing gap; expanding the check
  would widen scope to all worktree-using callers); corrupted
  `meta.json` (raises raw `JSONDecodeError`/`ValidationError` instead of
  a clean CLI error); lack of any locking around concurrent `hatchery
  resume` invocations on the same task.
- **`test_kubectl_proxy` SIGILLs on this sandbox host** (unrelated arch
  mismatch). Run `uv run pytest --ignore=tests/test_kubectl_proxy.py`.

### Follow-up pass: fetch-failure messaging, cli.py cleanup, review response

A second pass (originally tracked as a separate `finish-resume` task,
folded in here) closed out remaining gaps before merge:

**`_resolve_recreate_base` now distinguishes "confirmed absent on
origin" from "couldn't check origin."** It previously called
`git.fetch_remote()` and ignored the return value — if the fetch failed
(flaky network, auth issue, remote gone), the code proceeded to check
`remote_branch_exists` against whatever stale refs happened to be on
disk, and would silently fall back to recreating the worktree from the
default branch (losing the task's prior work) while telling the user
"branch is missing locally and on origin" — a claim that overstated
confidence since origin was never actually queried. The function's
return type is now a 3-tuple `(base_ref, branch_was_missing,
remote_check_failed)`. `remote_check_failed` is `True` only when the
code fell through to a fallback tier *and* `fetch_remote` itself
failed — in that case remote checks are skipped entirely (an unfetched
remote-tracking ref can't be trusted) and the fallback base is chosen
from local refs only. `restore_worktree_if_needed` surfaces this via a
distinct `ui.warn(...)` ("couldn't verify branch ... (fetch failed)")
and a distinct `prompt_note` wording.

**Dockerfile-restore-on-resume moved into
`sessions.restore_dockerfile_if_needed(meta, backend, repo, *,
no_docker)`.** This was inlined in `cmd_resume` — a degraded-state
recovery decision that belongs beside `restore_worktree_if_needed`, not
in the CLI layer. Pure behavior-preserving extraction.

**`cmd_resume` reuses `_cli_includes_to_entries`** instead of
re-inlining the same tuple-to-`IncludeEntry` conversion the helper
already did elsewhere in `cli.py`.

**`_confirm_recreate` promoted from a closure to a top-level
function.** `_confirm_recreate_worktree(name, branch, prev_status,
base) -> bool` in `cli.py` takes every value it needs as an explicit
argument instead of capturing `cmd_resume`'s locals, and is bound via
`functools.partial(_confirm_recreate_worktree, name, meta.branch,
prev_status)` before being passed to `restore_worktree_if_needed` as
the `confirm_recreate` callback. Independently callable/testable, no
closure.

**`git.create_include_worktrees` no longer force-removes an existing
worktree directory.** The prior "attach to existing branch" path
unconditionally ran `git worktree remove --force` before re-adding,
even when the worktree directory was already present and possibly
holding uncommitted work — that work would be silently destroyed. It
now checks `worktree.exists()` first: if the worktree is already
there, it's left alone entirely; if the branch exists but the worktree
directory is gone, `git worktree prune` (safe — only clears
administrative metadata for worktrees whose directories no longer
exist) runs before a plain `add`, with no `remove --force` in that path
at all.

**Test placement in `test_cli.py` trimmed to CLI-only behavior.**
`test_resume_missing_branch_recreates_from_default` and
`test_resume_missing_worktree_and_branch_compose` were removed — they
mostly re-asserted base-resolution correctness already covered at the
unit level by `TestResolveRecreateBase` / `TestRestoreWorktreeIfNeeded`
in `test_session_io.py`. The remaining degraded-state CLI tests
(`test_resume_missing_worktree_confirm_yes_recreates`, `..._eof_aborts_cleanly`,
`..._confirm_no_aborts`) were rewritten against the real `git_repo`
fixture instead of mocking `git.branch_exists` / `git.create_worktree`
/ `git.create_include_worktrees`, since those tests exist to cover
CLI-only behavior (the y/N prompt, EOF→cancel, exit codes) — not
git-decision correctness, which is already covered elsewhere.

- **Full test suite**: `uv run pytest --ignore=tests/test_kubectl_proxy.py -q`
  → 768 passed, 19 skipped. `uv run inv format --check` passes.
