# Task: better-background

**Status**: in-progress
**Branch**: hatchery/better-background
**Created**: 2026-07-10 09:35

## Objective

Design the refactor needed to run hatchery tasks as background processes
(attach/detach, pause, stop), and break that work — which has languished on the
`hatchery/background-tasks` branch — into smaller, independently-mergeable pieces.

This task is **design only**: it produces the architecture and the sub-task
breakdown below. It changes no production code. Each sub-task becomes a future
`hatchery new`.

## Context

Two future capabilities motivate this: (1) run a task fully detached and
re-attach to it later; (2) eventually let one task spawn/coordinate others. Both
sit on top of background execution.

Today execution is fully foreground and terminal-attached. The chain is
`cli._launch` → `sessions.launch` (a `running`↔`in-progress` status bracket) →
`docker.run_session` → `docker._run_container` (`-it`, `--rm`,
`subprocess.run`). There is no detached mode and no PID tracking.

The blocker is that sandbox construction is monolithic. `docker._run_container`
(`docker.py:1017`) assembles the entire `docker`/`podman run` argv inline, with
Docker/Podman divergence handled by scattered conditionals: a `match runtime`
block for `--userns=keep-id` + `--security-opt label=disable`, a `sys.platform`
gate on `--add-host`, a Podman-only OOM hint, and the DinD cap/device/seccomp
block. Mounts are `list[str]`; env is appended inline. There is **no spec object
and no runtime-backend abstraction** — in contrast to the *agent* axis, which is
already a clean `AgentBackend` ABC (`agents/agent_backend.py`), the obvious
template.

Because the sandbox is built this way, the prior background-tasks effort sprawled
horizontally across every layer and stalled. The `hatchery/background-tasks`
branch built bottom-layer scaffolding (docker-compose assembly, a sidecar image +
entrypoint, proxy/kubectl parameterisation) but **never wired any of it into
`run_session`/`sessions.py`/`cli.py`** — it stopped exactly at the integration
boundary where the real complexity lives: proxies run as host threads inside the
foreground CLI, so if the CLI dies the container is orphaned and resume breaks.

The fix is to give the sandbox a clean seam first, then build background execution
on top of it in small increments. That branch is kept only as reference; the
sub-tasks below start fresh.

## Summary

### Decision: backend-agnostic `ContainerSpec` builder + `ContainerRuntime` backend

Split "what the sandbox is" from "how a given engine runs it".

**`ContainerSpec`** — an in-memory, backend-agnostic model (no engine-specific
strings): `image`, `command`, `workdir`, `name`, `mounts`, `env`, `tmpfs`,
`caps_add`/`caps_drop`, `devices`, `security_opt`, `add_hosts`, `network`, `tty`,
`init`, `rm`, `restart`, `sidecars`. DinD content (caps/devices/seccomp) lives in
the spec — it is identical across engines, so it stops being "backend-specific".
`mounts` keeps the existing `"host:container:mode"` string form for the first
refactor to minimize churn (a structured `Mount` type is an optional later
cleanup). The spec may be serialized to the session dir for debugging, but the
in-memory object is the source of truth — **not** a compose file. (Compose was
the old branch's approach; it couples to `docker compose` and needs the separate,
less-reliable `podman-compose`, undermining backend-agnosticism.)

**`ContainerRuntime`** — an ABC mirroring `AgentBackend`, with `DockerRuntime` /
`PodmanRuntime` subclasses that own engine divergence, replacing the `Runtime`
enum's scattered conditionals: `binary`, `available()`/`detect()` (absorbing
`docker_available`/`podman_available`/`detect_runtime`), `render_run_argv(spec)`
(Podman adds `--userns=keep-id` on Linux + `--security-opt label=disable`; Docker
adds nothing), `run(spec, *, paste_interceptor)` (foreground; later gains
`run_detached`), and `oom_hint(returncode)` (Podman-137 messaging).

**The seam:** `run_session` splits into `build_spec(meta, backend, config, ...)`
→ `ContainerSpec` (the `--add-host` platform gate lives here as spec content, not
in the renderer) and `runtime.run(spec, ...)`. `_run_container`, `_userns_flags`,
and the OOM hint disappear from the procedural path.

### Decision: sidecars are host-managed detached processes, not containers

A `Sidecar` models a service abstractly — entrypoint, env, and a reachability
contract ("the agent reaches it at address X") — with a pluggable realization.
The default realization is a **host-managed detached process**, because:

- The API proxy and kubectl RBAC proxy exist to keep host credentials (API keys,
  OAuth refresh tokens, host kubeconfig) **on the host, out of the agent
  container**. Containerizing them would inject those credentials into a second
  image, weakening the core security model, and adds a `Dockerfile.sidecar` to
  build/maintain (the old branch's path).
- Reachability already works via `host.docker.internal` + `--add-host` on Linux;
  Docker Desktop / podman-machine provide it on macOS.
- It is backend-agnostic — both engines reach a host process identically, with no
  compose dependency.

The abstraction leaves room for a containerized realization later if isolation
ever demands it, but that is not the default.

### Sub-task breakdown (implement in order A → B → C)

**A — Sandbox runtime builder refactor** (behavior-preserving). Introduce
`ContainerSpec` + `ContainerRuntime`/`DockerRuntime`/`PodmanRuntime`; refactor
`run_session` into `build_spec(...)` + `runtime.run(spec)`; delete
`_run_container`; move userns / `label=disable` / OOM hint onto the runtime
backends. Sidecars stay exactly as today — host context managers
(`_maybe_api_server`, `_kubectl_context`) whose outputs (proxy port → env,
kubectl mounts, `--add-host`) feed the spec. *Success:* no behavior change;
existing pytest suite green (update only tests that asserted on the old inline
argv); an `--integration` launch works on both Docker and Podman. Mergeable
alone.

**B — Detached sidecar lifetime.** Extract a `SidecarManager` that runs the proxy
+ kubectl chain as **detached host processes** (setsid/double-fork), PID-tracked
in `session_dir`, health-checked, and torn down explicitly rather than on
context-manager exit. Reference (do not cherry-pick) the old branch's
parameterisation for shape: `proxy.api_server(bind_port=...)`,
`kubectl_proxy.persist_or_load_cert(...)` (cert persisted so a restarted proxy
still matches the agent's kubeconfig). Agent still foreground-attached, but
proxies now outlive the CLI — this fixes the orphaned-proxy bug that sank the
original effort. Mergeable alone.

**C — Background task execution.** Add `runtime.run_detached(spec)` (drop `--rm`,
keep `--name`); attach via `docker/podman attach` (Ctrl-P Ctrl-Q detach) or the
existing `cmd_exec` primitive; add `hatchery stop` (tear down container +
detached sidecars); derive status (`running`/`backgrounded`) from `inspect` + an
attacher PID dir and wire it into `list`/`status`. Builds on A (spec + detached
run) and B (sidecars already detached).

**Future (out of scope, enabled by the above):** task-spawns-task and inter-task
coordination.

### Notes for the next agent

- `docker.py` is the entire runtime/sandbox layer; `agents/agent_backend.py` is
  the ABC to copy for `ContainerRuntime`. `models.py` is a deliberate leaf to
  avoid a `sessions`↔`docker` import cycle — keep `ContainerSpec` free of that
  cycle too (put it in `docker.py` or a new leaf module, not `sessions`).
- `sessions.launch` is intentionally non-interactive (all prompts live in
  `cli.py`); preserve that boundary.
- `cmd_exec` (`cli.py:840`) already `docker exec`s into a running container by
  deterministic `container_name` — a ready-made attach primitive for sub-task C.
- Tests use hand-written doubles (`SpyBackend`) over mocks, and `tests/` mirrors
  `src/` one-to-one (see `AGENTS.md`).

## Agreed Plan

### Sub-task A — Sandbox runtime builder refactor (behavior-preserving)

#### A1. Introduce `ContainerSpec` (frozen dataclass, in `docker.py`)

Captures everything `_run_container` currently assembles inline — no
engine-specific strings:

```python
@dataclass(frozen=True)
class ContainerSpec:
    image: str
    command: list[str]
    workdir: str
    name: str                       # HATCHERY_TASK env value
    container_name: str | None      # --name flag
    mounts: list[Mount]
    env: dict[str, str]             # HATCHERY_TASK, HATCHERY_REPO, agent env
    cap_add: list[str] = field(default_factory=list)
    cap_drop: list[str] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)
    security_opt: list[str] = field(default_factory=list)
    add_hosts: list[str] = field(default_factory=list)
    interactive: bool = True        # -it vs not
    rm: bool = True                 # --rm
    init: bool = True               # --init
    command_override: list[str] | None = None
    capture_output: bool = False    # for non-interactive override
```

**Location:** `docker.py` (keeps it cycle-free — `sessions` and `docker`
already import `models`, and `docker` is the runtime layer).

#### A2. Introduce `ContainerRuntime` ABC + `DockerRuntime` / `PodmanRuntime`

Mirrors `AgentBackend` structure:

```python
class ContainerRuntime(ABC):
    @property
    @abstractmethod
    def binary(self) -> str: ...

    @staticmethod
    @abstractmethod
    def available() -> bool: ...

    @abstractmethod
    def render_run_argv(self, spec: ContainerSpec) -> list[str]: ...

    @abstractmethod
    def run(self, spec: ContainerSpec, *, paste_interceptor=None) -> subprocess.CompletedProcess | None: ...

    @abstractmethod
    def oom_hint(self, returncode: int) -> str | None: ...

class DockerRuntime(ContainerRuntime): ...
class PodmanRuntime(ContainerRuntime): ...
```

- `binary` → `"docker"` / `"podman"`
- `available()` → absorbs `docker_available()` / `podman_available()`
- `render_run_argv(spec)` — assembles `[binary, "run", ...]` from the spec.
  Podman adds `--userns=keep-id` on Linux + `--security-opt label=disable`.
  DinD cap-drop/cap-add/devices/seccomp render from spec fields (identical
  across engines). Absorbs `_userns_flags()` and the `match runtime` block.
- `run(spec, *, paste_interceptor)` — calls `render_run_argv` then executes
  via `_exec_agent` (interactive) or `subprocess.run` (command_override).
  Absorbs `_run_container`'s execution tail + OOM hint messaging.
- `oom_hint(returncode)` → Podman-137 message; Docker returns `None`.
- `detect()` class method on the ABC absorbs `detect_runtime()` — tries
  PodmanRuntime, then DockerRuntime, exits if neither.

Module-level `docker_available()` / `podman_available()` / `detect_runtime()`
become thin wrappers delegating to the new classes (backward compat for
`seeded_volumes.py`, tests, etc.).

#### A3. Implement `build_spec()` — extracts spec assembly from callers

One function that takes the assembled inputs and returns a `ContainerSpec`.
Moves env-var assembly, `--add-host` platform gate, DinD cap/device/seccomp
assembly here — all spec content, not in the renderer.  `--add-host` lives
in `spec.add_hosts` as `"host.docker.internal:host-gateway"` on Linux when
proxy is active or `add_host_gateway` is set.

#### A4. Refactor `run_session` → `build_spec(...)` + `runtime.run(spec)`

`run_session` keeps its current responsibilities (sentinel files, mount
construction, image build, sidecar context managers) but replaces the
`_run_container(...)` call with `build_spec(...)` + `runtime.run(spec)`.
The `Runtime` enum parameter type changes to `ContainerRuntime` throughout
the call chain: `sessions.launch` → `docker.run_session` → `runtime.run`.

#### A5. Refactor `launch_sandbox_shell` → `build_spec(...)` + `runtime.run(spec)`

Same pattern. The `_command_override` + `_interactive` parameters become
`spec.command_override` + `spec.interactive` + `spec.capture_output`.

#### A6. Refactor `exec_task_shell` — use `runtime.binary`

Trivial: `runtime` parameter type changes from `Runtime` to `ContainerRuntime`.
No behavior change.

#### A7. Refactor `build_docker_image` — use `runtime.binary`

Parameter type changes from `Runtime` to `ContainerRuntime`. Uses
`runtime.binary` (same as before).

#### A8. Keep `Runtime` enum as deprecated alias

Keep `Runtime` enum but add a `to_runtime()` method returning the ABC
instance. External callers (`seeded_volumes.py`, `agents/codex.py`,
`AgentBackend.background_threads`, tests) can migrate incrementally.
Module-level `docker_available()` / `podman_available()` / `detect_runtime()`
delegate to the new classes.

#### A9. Update tests

- `test_docker.py`: `TestRunContainerRuntime` and `TestRunContainerInteractive`
  → rewrite to test `build_spec()` + `runtime.render_run_argv()` + `runtime.run()`.
  Assert on `ContainerSpec` fields and rendered argv, not `_run_container` calls.
- `test_pure.py`: `TestRuntime` → update for `ContainerRuntime` ABC.
  `TestExecTaskShell` → update signature.
- `test_cli.py`: `detect_runtime` mocks → return `ContainerRuntime` instances.
- `test_sandbox.py`: `runtime` fixture → return `ContainerRuntime` instance.
- `test_agent_codex.py`: `Runtime.DOCKER` → `DockerRuntime()` in probe tests.
- `test_seeded_volumes.py`: No changes (uses module-level functions which
  remain as wrappers).

#### A10. Delete dead code

- `_run_container` — fully replaced by `build_spec` + `runtime.run`.
- `_userns_flags` — absorbed into `PodmanRuntime.render_run_argv`.

### Success criteria
- No behavior change in production code paths.
- `uv run pytest` (excluding `--integration`) fully green.
- `uv run ruff check .` and `uv run ruff format .` clean.
- `_run_container` and `_userns_flags` no longer exist.

## Progress Log

- [x] A1. Introduce `ContainerSpec` frozen dataclass in `docker.py`
- [x] A2. Introduce `ContainerRuntime` ABC + `DockerRuntime` / `PodmanRuntime`
      (with `_engine_flags` hook eliminating run/render duplication)
- [x] A3. Implement `build_spec()` — extracts spec assembly from callers
- [x] A4. Refactor `run_session` → `build_spec(...)` + `runtime.run(spec)`
- [x] A5. Refactor `launch_sandbox_shell` → `build_spec(...)` + `runtime.run(spec)`
- [x] A6. Refactor `exec_task_shell` — use `runtime.binary`
- [x] A7. Refactor `build_docker_image` — use `runtime.binary`
- [x] A8. Retired `Runtime` enum entirely; migrated `seeded_volumes.py`
- [x] A9. Updated tests: golden argv/spec assertions, oom_hint tests,
      integration test migration (build_spec + runtime.run + _engine_flags seam)
- [x] A10. Deleted dead code (`_run_container`, `_userns_flags`, module-level `_ensure_volumes`)
- [x] Cleanup: stale annotations, docstrings, comments, mutable defaults fixed

## Summary

*(Fill in on completion — then remove Agreed Plan and Progress Log above.
Cover: key decisions made, patterns established, files changed, gotchas,
and anything a future agent working in this repo should know.)*
