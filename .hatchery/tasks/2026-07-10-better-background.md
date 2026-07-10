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

*(To be filled in after planning discussion)*

## Progress Log

*(Steps will appear here once the plan is agreed)*

## Summary

*(Fill in on completion — then remove Agreed Plan and Progress Log above.
Cover: key decisions made, patterns established, files changed, gotchas,
and anything a future agent working in this repo should know.)*
