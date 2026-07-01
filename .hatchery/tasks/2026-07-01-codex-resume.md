# Task: codex-resume

**Status**: complete
**Branch**: hatchery/codex-resume
**Created**: 2026-07-01 08:38

## Objective

Add true session-level resume for codex tasks. Previously, resuming a
codex task re-launched it as a fresh session with the task file as
context, losing all in-agent conversation state. We want
`hatchery resume` to hand codex the same session id it was using
before, so codex continues the actual conversation.

## Context

Codex supports `codex resume <sid>` and `codex exec resume <sid> "<prompt>"`,
but has **no CLI flag to pre-set the session UUID** at launch — codex
generates its own UUID on first run and stores rollouts at
`~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl` within ~1s
of startup. To make resume work we let codex generate its own UUID,
detect it **live** during the launch, persist it to `meta.session_id`,
and consume it on subsequent launches.

Live detection (versus post-exit) is critical: if hatchery itself is
SIGKILL'd or the machine crashes mid-session, we still have the UUID
persisted and resume works.

## Summary

### The generalized extension point

Added `AgentBackend.background_threads()` — a new lifecycle hook
returning a list of nullary callables:

```python
@staticmethod
def background_threads(
    meta: SessionMeta,
    *,
    docker: bool,
    runtime: "Runtime | None",
    launch_start: float,
    stop: threading.Event,
) -> list[Callable[[], None]]:
    return []
```

`sessions.launch()` starts one daemon thread per returned callable,
signals `stop` in its `finally` block, and joins each thread with a
2-second timeout. Each callable may loop until `stop` fires or return
early once its work is done. Worker exceptions are caught and logged
so they cannot mask the launch's own exceptions.

The base implementation returns `[]`; only codex overrides. Future
needs — metrics collectors, watchdog timers, token refreshers, log
tailers — plug into the same hook.

### The codex poller

`_make_session_id_poller` returns a closure that polls
`_probe_session_id` every ~1s. On the first capture that differs
from the current `meta.session_id`, it saves and returns.

`_probe_session_id` dispatches to one of two implementations:

- **Docker**: `docker exec` runs `stat -c "%Y %n"` on the newest
  `~/.codex/sessions/*/*/*/rollout-*.jsonl`. Python parses the
  `<mtime> <path>` output, filters stale by mtime, and extracts the
  UUID from the filename with `_ROLLOUT_UUID_RE`.
- **Native**: globs the host `~/.codex/sessions/` tree, filters by
  the same mtime rule, sorts newest-first, extracts the UUID from
  the filename.

Both paths apply a 5-second grace window (`_MTIME_GRACE_SECONDS`) to
accommodate Podman-Machine / Docker Desktop VM clock skew on macOS.

### `session_id` lifecycle

Backends now declare `session_id_pre_generated: bool` on
`AgentBackend`:

- **True** (default) — the agent's CLI accepts a session id at
  launch (`--session-id=<uuid>`). `sessions.create()` pre-generates
  a UUID and writes it to `meta.session_id`.
- **False** (codex) — the agent generates its own id at runtime.
  `sessions.create()` leaves `meta.session_id` empty; the poller
  captures the real id on first launch.

This mattered because `sessions.create()` was unconditionally
pre-generating a v4 UUID for every task and, for codex, the poller
short-circuited on `if meta.session_id: return []` — so hatchery
kept passing that meaningless v4 to `codex resume`, which correctly
rejected it. The fix is per-backend gating of the pre-generation.

### Command shapes

| kind                    | args after wrapper                                                            |
| ----------------------- | ----------------------------------------------------------------------------- |
| new                     | `["<prompt>"]`                                                                |
| resume w/ session_id    | `["resume", "<sid>"]`                                                         |
| resume w/o session_id   | docker → `["resume", "--last"]`; native → fresh prompt fallback (defensive)   |
| finalize w/ session_id  | `["exec", "resume", "<sid>", "<wrap_up>"]`                                    |
| finalize w/o session_id | docker → `["exec", "resume", "--last", "<wrap_up>"]`; native → `["exec", "<wrap_up>"]` |

The `codex … "$@"` docker wrapper needed no edits — multi-arg
passthrough Just Works. `--dangerously-bypass-approvals-and-sandbox`
sits at the top level, before `"$@"`, and codex accepts it at both
`codex` and `codex exec` scopes. The wrapper also passes
`--config check_for_update_on_startup=false` so codex's interactive
"Update available" prompt doesn't block automated resume launches
in the sandbox.

### Files touched

- `src/seekr_hatchery/agents/agent_backend.py` — new
  `session_id_pre_generated` class attr; `background_threads` as a
  concrete `return []` default; class-attribute docs in the
  docstring; lifecycle firing-order block.
- `src/seekr_hatchery/agents/codex.py` — `supports_sessions = True`,
  `session_id_pre_generated = False`; updated `build_resume_command`
  and `build_finalize_command` to consume `session_id`;
  `_ROLLOUT_UUID_RE`, split `_probe_session_id_docker` /
  `_probe_session_id_native`, `_make_session_id_poller`;
  `--config check_for_update_on_startup=false` added to the docker
  wrapper.
- `src/seekr_hatchery/sessions.py` — thread lifecycle in `launch()`
  and `_wrap_worker` helper; conditional pre-generation of
  `session_id` gated on `backend.session_id_pre_generated`.
- `src/seekr_hatchery/agents/claude.py` — no changes (inherits the
  base default of `[]` workers).
- `tests/conftest.py` — SpyBackend `background_threads` override
  records the call for lifecycle-order assertions.
- Tests updated: `test_agent_codex.py`, `test_agent_claude.py`,
  `test_cli.py`, `test_session_io.py`.

### Gotchas

- **Worker exceptions vs KeyboardInterrupt.** `_wrap_worker` catches
  `Exception` (not `BaseException`) so a Ctrl-C during a worker still
  propagates. Covered by
  `test_worker_exception_does_not_mask_agent_exit`.
- **`background_threads` fires on finalize too.** The lifecycle
  docstring calls this out. The codex poller runs during finalize
  as well, but only overwrites `meta.session_id` if the probed id
  differs — and the finalize path uses `codex exec resume <sid>`
  which resumes the existing thread rather than starting a new one,
  so the probe usually confirms rather than updates.
- **Native mode `--last` fallback is intentionally absent.** Native
  `~/.codex/` is a shared host directory across tasks, so `--last`
  could pick up another task's rollout. `cli.py`'s
  `if not meta.session_id` guard bails first; the fresh-prompt
  fallback in `build_resume_command` is defensive.
- **`docker exec` cost is small.** ~50ms per probe against a running
  container. A helper container per poll would be an order of
  magnitude more expensive.
- **Dirty-volume defence via mtime.** If `hatchery delete` fails to
  clean up the per-task codex volume, a subsequent `hatchery new`
  with the same task name will find old rollouts on the volume. The
  mtime filter drops anything older than `launch_start - 5s` so
  the poller only ever sees rollouts written during this launch.
- **First capture typically lands on the second poll.** First poll
  fires at t≈1s (`stop.wait(1.0)`) — before that the container may
  still be starting. Codex writes the rollout within ~1s of process
  start, so poll 2 or 3 usually captures it.
