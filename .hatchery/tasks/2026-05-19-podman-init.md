# Task: podman-init

**Status**: complete
**Branch**: hatchery/podman-init
**Created**: 2026-05-19 17:44

## Objective

Add `--init` to the container `run` invocation in the hatchery launcher so
that a real init process (catatonit for Podman, tini for Docker) sits at
PID 1 and reaps zombie descendants.

## Context

Long-running hatchery containers were accumulating thousands of zombie
processes because the agent process (`claude`) runs as PID 1 and does not
reliably reap orphaned children spawned by its shell tooling. Observed in
production:

```
hatchery-seekr-hatchery-1d69813a-task-refactor    zombies = 3,401
hatchery-seekr-hatchery-1d69813a-review-refactor  zombies =   106
hatchery-seekr-chain-89c462e6-pure-jobsets        zombies =    38
```

The cgroup has no hard pid limit (`pids.max: max`), so the kernel never
killed the containers — but every `fork()` and `/proc` scan is O(n) in
the task list, so routine tool calls began stalling for seconds in the
worst-hit containers. The fix is to put a proper init at PID 1; both
container runtimes ship one and expose it via `--init`.

## Summary

### Change

Single-flag addition in `src/seekr_hatchery/docker.py:_run_container()`:

```python
cmd = [runtime.binary, "run", "--rm", "--init"]
```

`--init` was applied **unconditionally** (not gated on Podman) because the
zombie problem is a property of PID-1 reaping behavior, which is identical
under Docker (tini) and Podman (catatonit). The match-on-runtime block
below this line is reserved for flags whose semantics actually differ
between runtimes (e.g. `--userns=keep-id`, `--security-opt label=disable`);
`--init` does not belong there.

### Files changed

- `src/seekr_hatchery/docker.py` — one-line change to the initial `cmd`
  list in `_run_container()`, plus a short comment explaining why.
- `tests/test_docker.py` — two assertions in `TestRunContainerRuntime`
  (one per runtime) using the existing `_capture_cmd` helper.

### Verification

- `pytest tests/test_docker.py` → all 103 tests pass, including the two
  new `test_docker_runtime_adds_init` / `test_podman_runtime_adds_init`.
- No existing test asserts the absence of `--init`, so adding the flag
  was non-disruptive.

### Notes for future agents

- The launcher constructs `podman run` / `docker run` args as a Python
  list incrementally in `_run_container()`. Flags that apply to both
  runtimes go in the top-of-function block; runtime-specific flags go
  inside the `match runtime:` switch around line 1118.
- `--init` requires the runtime to ship an init binary. Both Docker
  (recent versions) and Podman do by default. Custom-built minimalist
  runtimes or older Docker (<1.13) would not — not a concern here.
- If anyone later wants to verify PID 1 in a running container:
  `podman exec <name> ps -p 1` should show `catatonit` (or `tini` for
  Docker), not `node` / `claude`.
