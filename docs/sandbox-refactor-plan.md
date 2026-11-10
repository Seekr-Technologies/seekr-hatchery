# Sandbox package refactor — implementation plan

**Status:** implemented as a pure origin/main refactor; independent review and differential validation complete
**Implementation record:** `.hatchery/tasks/2026-11-10-sandbox-refactor/task.md`

**Base:** `origin/main` (`9d671d4`); one `refactor:` commit, no startup optimization

## 1. Goal

Replace the scattered container implementation with a cohesive `seekr_hatchery.sandbox` package. Separate sandbox configuration and launch policy from engine-specific execution, expose explicit module APIs, and preserve existing behavior.

This is a behavior-preserving internal refactor, not a configuration migration or a redesign of task orchestration.

## 2. Current implementation

| File | Lines at planning time | Responsibilities |
| --- | ---: | --- |
| `src/seekr_hatchery/docker.py` | 1,786 | Config models/loading, scaffolding, runtimes, builds, mounts, launch coordination |
| `src/seekr_hatchery/mount.py` | 226 | Mount descriptors, CLI rendering, file-volume startup commands |
| `src/seekr_hatchery/mount_links.py` | 224 | Symlink-aware mount expansion |
| `src/seekr_hatchery/seeded_volumes.py` | 242 | Volume creation, seeding, rollback, cleanup |

Main consumers:

- `cli.py`: configuration, runtime resolution, sandbox shells, cleanup.
- `sessions.py`: scaffolding, configuration editing/validation, includes, launch coordination, harness updates.
- `agents/*`: mount declarations and runtime type annotations.
- `sidecars/*`: mount declarations.
- `models.py`: documentation references; session metadata supplies launch identity and paths.
- Tests: imports, doubles, patch targets, golden command/spec assertions, and integration fixtures.

### Important dependency constraint

Agents and sidecars consume mount types, while sandbox launch coordination consumes agents and sidecars. Importing a sandbox submodule also initializes `sandbox/__init__.py`.

Therefore the package root must not eagerly import launch/configuration modules. Otherwise moving mount types into the package can introduce circular imports even when callers import the leaf submodule directly.

## 3. Scope and non-goals

### In scope

- Move the four implementation files into a responsibility-based package.
- Rename engine-neutral Python concepts away from Docker terminology.
- Establish explicit public interfaces and a safe dependency direction.
- Update all repository callers, annotations, tests, patch targets, and documentation references.
- Split tests to mirror source modules.
- Replace tests of private implementation details with public-contract coverage.
- Preserve origin/main image-build behavior exactly: always invoke the builder, with existing cache flags.

### Out of scope

- Renaming `docker.yaml`, `docker.local.yaml`, Dockerfiles, CLI flags, or persisted keys.
- Changing task/config schemas or resource names.
- Redesigning `SessionMeta`, task lifecycle, or session identity.
- Moving token ownership or git/worktree lifecycle into sandbox code.
- Changing errors from UI/exit behavior to an exception-based API.
- Changing runtime preference or fallback behavior.
- Image fingerprinting, image reuse, startup optimization, or changing remote-input freshness policy.
- Deduplicating existing volume-creation behavior as an incidental cleanup.
- Moving sidecar implementations or generic PTY/interceptor infrastructure.
- Adding compatibility shims unless a supported external Python API is identified.

## 4. Proposed structure

```text
src/seekr_hatchery/sandbox/
├── __init__.py
├── mount_types.py
├── config.py
├── scaffold.py
├── spec.py
├── runtime.py
├── images.py
├── mounts.py
├── mount_links.py
├── volumes.py
└── launch.py
```

Both engine adapters remain in `runtime.py`. They are small, and splitting each into its own module would add indirection without a clear benefit.

### Source-to-destination map

| Destination | What moves | Current source |
| --- | --- | --- |
| `mount_types.py` | MountMode, SeedContext, BindMount, VolumeMount, TmpfsMount, Mount | `mount.py` |
| `config.py` | Config models, migration/read/merge/load/validation, include parsing, feature labels, environment resolution | `docker.py` |
| `scaffold.py` | Dockerfile path/generation, config template creation, harness pinning, DinD Dockerfile snippet/check | `docker.py` |
| `spec.py` | ContainerSpec, build_spec, DinD capability/security assembly | `docker.py` |
| `runtime.py` | Runtime classes/discovery, argv rendering, process/PTY execution | `docker.py` |
| `runtime.py` | Mount CLI rendering and file-volume command wrapping | `mount.py` |
| `images.py` | Fingerprint labels, inspection, builds, build-output streaming | `docker.py` |
| `mounts.py` | Task/shell mount construction, safety validation, git/include mounts, clipboard paths | `docker.py` |
| `mount_links.py` | Existing symlink expansion module | `mount_links.py` |
| `volumes.py` | Existing seeded-volume lifecycle module | `seeded_volumes.py` |
| `launch.py` | Runtime selection policy, launch context, task/shell coordination, clipboard cleanup, sidecar/paste setup | `docker.py` |

### Ownership that stays outside

| Existing location | Retained responsibility |
| --- | --- |
| `sessions.py` | Task lifecycle, worktrees, tokens, image/container naming, config-edit workflows, harness-update orchestration |
| `agents/` | Installation snippets, agent commands, authentication, backend hooks |
| `sidecars/` | Proxy implementations and sidecar lifecycle abstraction |
| `pty_proxy.py`, `stream_interceptor/` | Generic terminal infrastructure |
| `includes.py` | Shared include-path domain types used outside sandbox code |
| `resources/` | Existing templates and seccomp profile |

Boundary: **sessions own task identity and lifecycle; sandbox owns container preparation and execution.**

## 5. Module declaration outlines

The following outlines describe intended declarations and ownership. They are not executable stubs. Existing arguments, defaults, and return contracts remain unless an explicit rename or extraction is listed.

### 5.1 `__init__.py`

Export only dependency-leaf mount types, using an explicit `__all__`:

- MountMode
- SeedContext
- BindMount
- VolumeMount
- TmpfsMount
- Mount

Do not eagerly import config, runtime, images, launch, agents, or sidecars. Internal modules import defining modules directly rather than going through this root facade.

### 5.2 `mount_types.py`

```text
MountMode = Literal["RO", "RW"]

@dataclass(frozen=True)
class SeedContext
    session_dir: Path
    proxy_token: str
    container_workdir: str

class BindMount(BaseModel)
    existing fields and model configuration

class VolumeMount(BaseModel)
    existing fields, seed callable, task_scoped behavior

class TmpfsMount(BaseModel)
    existing fields and model configuration

Mount = Annotated[BindMount | VolumeMount | TmpfsMount, ...]
```

A dependency leaf: no agent, sidecar, session, runtime, or filesystem-operation imports.

### 5.3 `config.py`

```text
class CacheVolume(BaseModel)
    existing fields and validators

class SandboxConfig(sidecars.SidecarConfig)
    existing fields and validators

load_config(hatchery_dir, *, local_hatchery_dir=None) -> SandboxConfig
validate_config_file(path, *, base_path=None) -> str | None
parse_include_entry(entry) -> tuple[str, str]
features(config) -> list[str]
resolve_environment(config) -> dict[str, str]

_migrate_config(data)
_read_config(path)
_merge_config(base, overlay)
_VALID_CAPS
```

Preserve recursive mapping merge, list append, null-list handling, migrations, validation, source-path reporting, and environment-resolution semantics.

### 5.4 `scaffold.py`

```text
DIND_DOCKERFILE_LINES

dockerfile_path(hatchery_dir, backend) -> Path
ensure_dockerfile(hatchery_dir, backend=..., *, prompt=True) -> bool
ensure_config(hatchery_dir, *, prompt=True) -> bool
ensure_local_config(hatchery_dir) -> bool
supports_dind(hatchery_dir, backend) -> bool

_pin_harness_latest(text, backend)
_comment_out(text)
template resource paths
```

`Dockerfile` remains correctly named: it is a concrete build format. Preserve prompts, output, generated bytes, latest-harness pinning, and existing-file behavior.

### 5.5 `spec.py`

```text
@dataclass(frozen=True)
class ContainerSpec
    existing fields

build_spec(
    *,
    image,
    mounts,
    workdir,
    name,
    hatchery_repo,
    container_name,
    agent_cmd,
    user_env=None,
    extra_env=None,
    needs_host_gateway=False,
    dind=False,
    cap_add=None,
    command_override=None,
    interactive=False,
) -> ContainerSpec

_DIND_CAPS
seccomp resource path
```

Owns engine-neutral launch policy: environment precedence, capability/security/device assembly, and platform-gated host-gateway requirements. Engine-specific flags remain in runtime adapters.

### 5.6 `runtime.py`

```text
class ContainerRuntime(ABC)
    binary property
    available() -> bool
    oom_hint(returncode) -> str | None
    render_run_argv(spec) -> list[str]
    run(spec, *, paste_interceptor=None)
    _engine_flags(spec)
    _ensure_volumes(mounts)

class DockerRuntime(ContainerRuntime)
    existing overrides

class PodmanRuntime(ContainerRuntime)
    existing overrides

detect_runtime() -> ContainerRuntime

_mount_to_args(mount)
_file_mount_prestart_cmds(mounts)
_wrap_cmd_for_file_mounts(cmd, mounts)
_render_caps(spec)
_render_security_opt(spec)
_render_devices(spec)
_exec_agent(cmd, paste_interceptor)
_sidecar_dir(dst)
```

Rendering moves here because it translates declarative mounts/specs into engine commands.

Preserve:

- Full argv ordering and values.
- Docker/Podman engine differences.
- Interactive versus command-override execution.
- PTY/paste behavior.
- File-volume symlink setup.
- Existing low-level ensure-volumes behavior.
- Discovery policy distinguishing absent Podman from installed-but-unavailable Podman.

Runtime must not import volume orchestration from `volumes.py`.

### 5.7 `images.py`

```text
build_image(
    repo,
    hatchery_dir,
    image_name,
    backend,
    runtime=None,
    no_cache=False,
    exit_on_error=True,
) -> bool

_stream_build(cmd, cwd, n_lines=4)
_BUILD_FINGERPRINT_LABEL
build-progress/ANSI helpers and constants
```

`build_image` replaces `build_docker_image` by name/location only. It always invokes the builder, exactly as origin/main does.

Preserve:

- Unconditional build invocation, without image inspection, fingerprint labels, or an eager Dockerfile read.
- Existing `--no-cache` forwarding.
- Empty temporary build context.
- Debug versus normal build output.
- Dockerfile path reporting and exit-or-return-false behavior.

### 5.8 `mounts.py`

```text
build_mounts(meta, backend, session_dir, config, ...) -> list[Mount]
build_shell_mounts(repo, config) -> list[Mount]
clipboard_image_dir(session_dir) -> Path
check_host_path_safe_for_mount(repo) -> None

_construct_config_mounts(config)
_construct_volume_mounts(config)
_validate_mounts(mounts)
_git_worktree_mounts(...)
_include_mounts(...)
_default_home_mounts()
_clipboard_image_mount(session_dir)

SYSTEM_MOUNT_BLOCKLIST
_MACOS_SHARED_ROOTS
_VOLUME_NAME_PREFIX
```

`build_shell_mounts` extracts the existing inline mount assembly in `launch_sandbox_shell`; it is not a new policy.

Preserve mount order, modes, git sentinel handling, includes, no-worktree/no-commit behavior, shared caches, symlink expansion, and safety filtering.

The host-path check is shared with launch preparation. Other helpers remain private unless another module genuinely needs their contract.

### 5.9 `mount_links.py`

```text
SKIP_DIRS
expand_link_mounts(mounts) -> list[Mount]
existing traversal/resolution helpers
```

Move with minimal changes to imports and documentation references. Symlink interpretation remains separate from final mount safety validation.

### 5.10 `volumes.py`

```text
task_volume_prefix(repo, name) -> str
volume_name(meta, logical_name) -> str
prepare_volume_mounts(
    runtime_binary,
    mounts,
    meta,
    session_dir,
    proxy_token,
    container_workdir,
) -> list[Mount]
cleanup_task_volumes(repo, name) -> None

_resolved_name(...)
_ensure_volume(...)
_seed_volume(...)
_seed_files_for(...)
```

Preserve:

- Seed only when creating a volume.
- State persistence across resumes.
- Failed-initialization rollback.
- Task-scoped versus shared volumes.
- Best-effort task-removal cleanup.
- Existing runtime-selection behavior during cleanup.

Because runtime does not import this module, replace the current lazy runtime-adapter import with a normal module import.

Retain the intentional call-time dependency on session naming in `task_volume_prefix`. Moving identity functions is outside this refactor.

### 5.11 `launch.py`

```text
resolve_runtime(hatchery_dir, no_docker, backend=...) -> ContainerRuntime | None
launch_context(meta, runtime) -> tuple[SandboxConfig | None, list[str], str]

run_session(meta, backend, agent_cmd, config, *, ...) -> None
launch_shell(repo, backend, config, runtime, image_name, *, ...) -> None
exec_task_shell(container_name, runtime, shell="/bin/bash") -> None
remove_clipboard_dir(session_dir) -> None

_prepare_sidecars(...)
_make_paste_interceptor(...)
```

`resolve_runtime` stays here rather than in runtime adapters: it enforces launch policy, including explicit native mode and the Dockerfile requirement.

Coordinate, but do not absorb, sidecar implementations and generic terminal infrastructure. Preserve launch ordering, backend hooks, image preparation, seeded-volume preparation, sidecar lifecycle, and shell-session cleanup.

## 6. Naming changes

| Existing Python name | Proposed name |
| --- | --- |
| `DockerConfig` | `config.SandboxConfig` |
| `load_docker_config` | `config.load_config` |
| `validate_docker_config_file` | `config.validate_config_file` |
| `parse_docker_include_entry` | `config.parse_include_entry` |
| `docker_features` | `config.features` |
| `_resolve_environment` | `config.resolve_environment` |
| `ensure_docker_config` | `scaffold.ensure_config` |
| `ensure_local_docker_config` | `scaffold.ensure_local_config` |
| `_dind_dockerfile_ok` | `scaffold.supports_dind` |
| `build_docker_image` | `images.build_image` |
| `launch_sandbox_shell` | `launch.launch_shell` |
| `mount_to_docker_args` | `runtime._mount_to_args` |
| `_construct_docker_mounts` | `mounts._construct_config_mounts` |
| `_docker_mounts_includes` | `mounts._include_mounts` |

Names describing actual formats/engines remain: Dockerfile, DockerRuntime, PodmanRuntime. Keep existing constants such as `DOCKER_CONFIG` and `LOCAL_DOCKER_CONFIG`, persisted filenames, schemas, CLI flags, and user-visible output unchanged.

## 7. Public interface and dependency direction

Prefer explicit module APIs, not a catch-all facade:

```text
import seekr_hatchery.sandbox.config as sandbox_config
import seekr_hatchery.sandbox.images as sandbox_images
import seekr_hatchery.sandbox.launch as sandbox_launch
import seekr_hatchery.sandbox.runtime as sandbox_runtime
import seekr_hatchery.sandbox.scaffold as sandbox_scaffold
```

### External caller map

| Caller | APIs used outside sandbox |
| --- | --- |
| `cli.py` | config.load_config/features; runtime.detect_runtime; launch.resolve_runtime/launch_shell/exec_task_shell/remove_clipboard_dir; volumes.cleanup_task_volumes |
| `sessions.py` | scaffold.dockerfile_path/ensure_dockerfile/ensure_config/ensure_local_config; config.load_config/validate_config_file/parse_include_entry; launch.launch_context/run_session; runtime.detect_runtime; images.build_image |
| `agents/*` | mount_types descriptors/context; runtime annotations under TYPE_CHECKING |
| `sidecars/*` | mount_types descriptors |
| Integration tests | spec.build_spec; mounts.build_mounts/build_shell_mounts; runtime rendering/execution; image preparation |

### Layering rules

1. `mount_types` is a leaf.
2. Agents and sidecars may consume mount types without importing launch coordination.
3. Spec construction consumes mount types but not launch coordination.
4. Runtime consumes specs/mount types and generic terminal infrastructure; it does not consume volume orchestration.
5. Config/scaffolding/images/mount construction/volumes compose lower-level facilities as required.
6. Launch coordinates these components and agents/sidecars.
7. CLI and sessions choose tasks and invoke sandbox interfaces.
8. Internal imports target defining modules, never the package-root export facade.

Document the remaining call-time session-naming dependency instead of hiding it with a new naming abstraction.

## 8. Implementation sequence and checkpoints

### Step 1 — dependency leaves

- Establish test baseline.
- Create the import-light package root.
- Extract mount descriptors and ContainerSpec.
- Update descriptor consumers in agents, sidecars, and tests.
- Add fresh-process import-order smoke tests.

Checkpoint: all affected modules import successfully in both dependency directions; descriptor/spec contracts are unchanged.

### Step 2 — configuration, scaffolding, and spec policy

- Extract configuration loading/model code and neutral names.
- Extract scaffolding and DinD Dockerfile helpers.
- Move build_spec and its policy constants.
- Correct template/seccomp resource paths relative to the new package.
- Preserve output and generated content.

Checkpoint: configuration overlays, validation errors, template generation, and golden specs match baseline.

### Step 3 — runtime and images

- Extract runtime adapters, discovery, rendering, and execution.
- Move mount rendering/file-volume startup wrappers from mount.py.
- Extract build_image and build-output streaming.
- Preserve complete build commands, unconditional builds, and existing error behavior.

Checkpoint: complete Docker/Podman argv, PTY behavior, and repeated unconditional-build tests pass.

### Step 4 — mount and volume preparation

- Extract task mount construction and safety policy.
- Introduce build_shell_mounts by extracting the current shell assembly.
- Move symlink expansion and seeded volumes.
- Remove the unnecessary lazy runtime import in volume cleanup.
- Preserve mount ordering, name derivation, and lifecycle behavior.

Checkpoint: full mount outputs, seed-once/rollback behavior, and cleanup contracts pass.

### Step 5 — launch integration and caller migration

- Extract launch context, runtime selection policy, session/shell coordination.
- Update CLI/session imports and call sites.
- Update runtime annotations, model documentation, sidecar layering documentation, and test doubles/patch targets.
- Remove old docker.py, mount.py, mount_links.py, and seeded_volumes.py after all repository callers migrate.
- Do not retain re-export shims by default.

Checkpoint: no old-module imports/patch targets remain; CLI/session behavior and error output remain unchanged.

### Step 6 — validation and completion

- Finish test splitting and public-contract rewrites.
- Run lint, formatting, full unit suite, and available engine integration tests.
- Review diff for accidental naming/schema/output changes.
- Record decisions and validation limitations.
- Commit in coherent extraction/integration steps.

Initial extraction used serial component-owner subagents. The final result is a pure origin/main refactor as one commit: startup optimization is excluded. Three fresh reviewers and a differential auditor compared directly against origin/main. Original logger identities are retained to preserve diagnostic output and DEBUG-controlled image-build behavior. See the implementation record for final evidence and validation limits.

## 9. Test strategy

### Layout

```text
tests/sandbox/
├── test_mount_types.py
├── test_config.py
├── test_scaffold.py
├── test_spec.py
├── test_runtime.py
├── test_images.py
├── test_mounts.py
├── test_mount_links.py
├── test_volumes.py
├── test_launch.py
└── test_launch_integration.py
```

- Split tests/test_docker.py according to the defining source modules.
- Move tests/test_mount.py and distribute descriptor versus rendering assertions between mount-types and runtime tests.
- Move tests/test_mount_links.py and tests/test_seeded_volumes.py to their matching modules.
- Move existing end-to-end tests/test_sandbox.py to test_launch_integration.py.
- Keep CLI/session/agent/sidecar tests in their existing module-aligned locations.
- Place import-order smoke tests in test_mount_types.py; run each ordering in a fresh subprocess.

### Contract coverage

| Area | Required contracts |
| --- | --- |
| Mount types | Full defaults/serialization and discriminated union behavior |
| Config | Full loaded outputs, overlay merge, migration, validation/source-path errors, environment resolution |
| Scaffolding | Generated files, prompts, no overwrite, harness pin behavior, resource availability |
| Spec | Complete spec outputs, environment precedence, DinD policy, host-gateway platform gate |
| Runtime | Complete Docker/Podman argv, interactive/override paths, PTY routing, file-volume startup setup, runtime discovery |
| Images | Always build on repeated calls; no image inspection/labels/eager file read; complete Docker/Podman commands; no-cache flag; empty context; error contract |
| Mounts | Complete mount lists and modes; worktree/no-worktree/no-commit; git sentinels; includes; caches; clipboard; symlinks; unsafe paths |
| Links | Existing absolute/relative/multi-hop, traversal pruning, already-covered-target behavior |
| Volumes | Task/shared naming, seed-once, normalized payloads, rollback, persistence, best-effort cleanup |
| Launch | Backend hooks and preparation order, sidecar lifecycle, mount/spec handoff, shell cleanup on success/failure |
| Native mode | No runtime discovery/invocation when explicitly disabled |
| Imports/resources | Fresh-process import order; templates/seccomp still present in installed package layout |

### Replace private-helper tests

| Existing direct private coverage | Public entry point |
| --- | --- |
| _stream_build | images.build_image |
| Include/home mount helpers | mounts.build_mounts / build_shell_mounts |
| _validate_mounts | Mount-building APIs |
| Paste-interceptor and sidecar setup | Launch APIs |
| Seed normalization | volumes.prepare_volume_mounts |

Do not promote helpers to public solely to preserve their old tests.

Use real temporary files/git repositories, full-output assertions, and hand-written process-boundary doubles. Avoid mocking internal assembly steps or asserting private state.

### Integration tests

- Preserve integration markers and normal opt-in behavior.
- Exercise real Docker and Podman when available.
- Verify volume persistence and file-volume symlinks.
- Verify worktree git writes and protected mounts.
- Verify unconditional builds and existing no-cache behavior.
- Distinguish native Docker from a Podman compatibility shim before claiming Docker engine coverage.

## 10. Validation commands and acceptance criteria

Run:

- `uv run ruff check .`
- `uv run ruff format --check src tests`
- `uv run pytest tests/sandbox -q`
- `uv run pytest -q`
- Integration tests using the repository's existing opt-in mechanism.
- `git diff --check`
- Search for old module imports and old test patch targets.

Acceptance:

1. The old implementation modules are removed and no repository callers depend on them.
2. Package imports work independently of import order.
3. Configuration/task serialization, resource names, CLI flags, and user-visible output are unchanged.
4. Complete spec/argv/mount contracts match baseline.
5. Image preparation always builds as on origin/main; no startup optimization is included.
6. Resource paths work after relocation and installation.
7. Tests/lint pass, or environmental blockers are precisely recorded.
8. No unrelated task/session/sidecar redesign is mixed into the change.

### Known baseline limitations

- The full suite currently aborts with a native illegal-instruction crash while importing cryptography.x509 in kubectl-sidecar TLS setup.
- Re-run baseline and refactor validation directly against origin/main; do not reuse evidence from the optimization branch.
- Repository-wide Ruff formatting checks Markdown examples and reports pre-existing differences in AGENTS.md and an older task record; source/test formatting passes.
- In this sandbox, the docker executable is a Podman compatibility shim, not a native Docker engine.

## 11. Review checkpoints

Feedback is especially useful on:

1. The explicit-submodule API versus a larger package-root facade.
2. Moving mount descriptors into sandbox while keeping __init__.py import-light.
3. The distinction between launch policy and low-level runtime execution.
4. Keeping session naming and task ownership outside this package.
5. Scope: mechanical extraction plus neutral names and contract-test cleanup, without deeper behavioral redesign.
