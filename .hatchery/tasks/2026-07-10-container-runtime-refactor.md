# Task: container-runtime-refactor

**Status**: complete
**Branch**: hatchery/better-background
**Created**: 2026-07-10 09:35

## Objective

Split "what the sandbox is" from "how a given engine runs it." Introduce a
backend-agnostic `ContainerSpec` model and a `ContainerRuntime` ABC (mirroring
the existing `AgentBackend` pattern) so that Docker/Podman divergence lives in
one seam, then refactor every caller of the old monolithic `_run_container`
procedural path onto `build_spec(...)` → `runtime.run(spec)`.

This is sub-task A of the background-execution design (see the original
`better-background` design doc). It is behavior-preserving: no user-facing
change, just a clean internal seam that sub-tasks B (detached sidecars) and C
(background execution) build on top of.

## Context

Before this refactor, sandbox construction was monolithic. `docker._run_container`
assembled the entire `docker`/`podman run` argv inline, with engine divergence
handled by scattered conditionals: a `match runtime` block for
`--userns=keep-id` + `--security-opt label=disable`, a `sys.platform` gate on
`--add-host`, a Podman-only OOM hint, and the DinD cap/device/seccomp block.
There was no spec object and no runtime-backend abstraction — in contrast to the
*agent* axis, which already had a clean `AgentBackend` ABC.

This monolithic structure was the reason a prior background-tasks effort sprawled
across every layer and stalled. Giving the sandbox a clean seam first lets
background execution be built on top in small increments.

## Summary

### Decision: `ContainerSpec` (what) + `ContainerRuntime` (how)

**`ContainerSpec`** — a frozen dataclass in `docker.py` (kept cycle-free; `models`
is a deliberate leaf). It captures everything the old inline path assembled:
`image`, `command`, `workdir`, `name`, `container_name`, `mounts`, `env`,
`cap_add`/`cap_drop`, `devices`, `security_opt`, `add_hosts`, `interactive`,
`rm`, `init`, `command_override`, `capture_output`. No engine-specific strings.
DinD content (caps/devices/seccomp) lives in the spec — it is identical across
engines, so it stops being "backend-specific." The `--add-host` platform gate
lives here as spec content (`add_hosts`), not in the renderer.

**`ContainerRuntime`** — an ABC mirroring `AgentBackend`, with `DockerRuntime` /
`PodmanRuntime` subclasses. The only divergence point is the `_engine_flags(spec)`
hook: the base `render_run_argv` and `run` are concrete and shared; Podman
overrides `_engine_flags` to inject `--userns=keep-id` (Linux only) +
`--security-opt label=disable`. This replaces the old `Runtime` enum's scattered
conditionals and the `match runtime` block. `binary`, `available()`, and
`oom_hint()` remain abstract/overridden per subclass.

**The seam:** `run_session` and `launch_sandbox_shell` split into
`build_spec(meta, backend, config, ...)` → `ContainerSpec` and
`runtime.run(spec, ...)`. `_run_container`, `_userns_flags`, and the inline OOM
hint disappear from the procedural path.

**`detect_runtime()`** collapsed into a single module-level pure function (was
`ContainerRuntime.detect()` static method). `docker_available()` /
`podman_available()` module-level wrappers were deleted; all callers (including
`seeded_volumes.py`, `test_sandbox.py` fixture, and the availability unit tests)
now call `DockerRuntime.available()` / `PodmanRuntime.available()` directly.

The old `Runtime` enum was retired entirely. Its only remaining production user
(`seeded_volumes.py`) was migrated to construct `PodmanRuntime()` / `DockerRuntime()`
directly.

### Key decisions

- **`_engine_flags` hook, not duplicated methods.** The first iteration declared
  `render_run_argv` and `run` as `@abstractmethod` on the base and copied the body
  into both subclasses — 90% copy-paste that doubled the edit surface. The fix is
  a single `_engine_flags(spec) -> list[str]` hook on the base (returns `[]` for
  Docker), called right before `-w` in `render_run_argv` to preserve the exact old
  argv order. Podman overrides it. This is also the test seam: integration tests
  patch `type(runtime)._engine_flags` to suppress userns in nested-container
  scenarios (replacing the old `_userns_flags` monkeypatch).

- **`mounts` stays structured.** The spec uses the existing `Mount` type
  (`BindMount` / `VolumeMount`) rather than raw `"host:container:mode"` strings,
  matching the mount refactor that already landed on main. `mount_to_docker_args`
  renders each mount to argv flags.

- **`detect_runtime` is a pure function, not a class method.** It has no need for
  `self`; making it a module-level function keeps `ContainerRuntime` focused on
  "how to run a container" and makes the no-runtime branch easy to test.

### Files changed

| File | Change |
|------|--------|
| `src/seekr_hatchery/docker.py` | `ContainerSpec`, `ContainerRuntime`/`DockerRuntime`/`PodmanRuntime`, `build_spec()`, `detect_runtime()` pure fn; deleted `_run_container`, `_userns_flags`, `docker_available`, `podman_available`, `Runtime` enum |
| `src/seekr_hatchery/seeded_volumes.py` | `cleanup_task_volumes` constructs `PodmanRuntime()`/`DockerRuntime()` directly |
| `src/seekr_hatchery/cli.py` | `detect_runtime()` call sites unchanged (pure function) |
| `src/seekr_hatchery/sessions.py` | `runtime` param type → `ContainerRuntime` |
| `src/seekr_hatchery/pty_proxy.py` | Docstring fix (garbled find-replace artifact) |
| `src/seekr_hatchery/agents/agent_backend.py`, `agents/codex.py` | `Runtime` enum references → `ContainerRuntime` |
| `tests/test_docker.py` | Golden argv/spec assertions, `oom_hint` tests, availability tests repointed to classes, `_engine_flags` seam |
| `tests/test_pure.py` | `Runtime` enum tests → `ContainerRuntime` tests |
| `tests/test_sandbox.py` | Integration tests migrated to `build_spec` + `runtime.run`; `runtime` fixture uses class `.available()` |
| `tests/test_cli.py` | `detect_runtime` mocks return `ContainerRuntime` instances |
| `tests/test_agent_codex.py` | `Runtime.DOCKER` → `DockerRuntime()` |
| `tests/test_seeded_volumes.py` | Patches `DockerRuntime.available`/`PodmanRuntime.available` (was `docker_available`/`podman_available`) |

### Gotchas for future agents

- **Argv order matters.** The golden tests pin the exact argv order. The
  `_engine_flags` hook is placed *before* `-w` to reproduce the old order. If you
  add new flags to `render_run_argv`, add a golden assertion for them.
- **Podman+DinD emits `--security-opt label=disable` twice** — once from the DinD
  `security_opt` list and once from `_engine_flags`. This is pre-existing and
  idempotent. Don't "fix" it without verifying Docker+DinD separately.
- **`ContainerSpec` has no `sidecars` field yet.** That's a sub-task B addition.
  The current spec models only the single foreground container.
- **`detect_runtime()` is a pure function now**, not a classmethod. Tests that
  need to control it patch `seekr_hatchery.docker.detect_runtime` (or the
  `PodmanRuntime.available`/`DockerRuntime.available` static methods it calls).
- **Mutable-default smell avoided:** `run_session` and `build_docker_image`
  default `runtime` to `None` and resolve `runtime = runtime or DockerRuntime()`
  inside, rather than sharing a module-level instance.

### Validation

- `uv run pytest` (excluding `--integration`) — fully green.
- `uv run ruff check .` and `uv run ruff format .` — clean.
- `_run_container` and `_userns_flags` no longer exist.
- `Runtime` enum no longer exists.
- `docker_available` / `podman_available` no longer exist.
