# Task: cache-volumes

**Status**: complete
**Branch**: hatchery/cache-volumes
**Created**: 2026-06-02 16:27

## Objective

We currently encourage the user to mount caches (like uv) as r/w from host. This is a great idea to reduce downloads, but causes serious issues with virtiofs traffic.

Instead, we should support a pattern in docker.yaml, allowing users to create persistent caches, which can be re-used across sandboxes, but do NOT hit host FS.

## Context

The default in `_default_home_mounts()` previously bind-mounted `~/.cache/uv` from
the host into every sandbox. On macOS that routes every cache read/write through
virtiofs — slow for the dense, many-small-files I/O pattern of package
managers (uv, pip, npm) — turning a feature meant to *avoid* network downloads
into a different bottleneck. Named docker/podman volumes live inside the
container engine's storage instead, so they bypass virtiofs while still
persisting across `--rm` containers and being shareable across tasks/repos.

## Summary

### New `volumes:` section in `.hatchery/docker.yaml`

```yaml
volumes:
  - name: uv-cache
    path: /home/hatchery/.cache/uv
```

Each entry maps to a runtime-managed named volume `hatchery-<name>` mounted
at `path` inside the sandbox with mode `rw`. The volume is auto-created on
first launch (`docker/podman volume inspect` then `... volume create` if
missing) and re-used by every subsequent sandbox that mounts it. A bare name
like `uv-cache` is shared across all tasks/repos; users append a suffix
(`uv-cache-myrepo`) for narrower scope. Cleanup is manual:
`docker volume rm hatchery-<name>`.

### Key design decisions

- **Global volume namespace** (`hatchery-<name>`, no per-repo prefix). User
  asked for this explicitly so caches can be shared by every sandbox by
  default. Hatchery's `hatchery-` prefix keeps these visually distinct from
  unrelated user volumes on the host runtime.
- **`Mount.volume: bool` flag** rather than a new dataclass or new
  `MountMode`. `mount_to_docker_args` already emits `-v src:dst:mode` —
  the only difference for a named volume is that src isn't a host path
  (no expanduser, no existence check). The boolean flag keeps the single
  source of truth for the `-v` argument shape and works uniformly for both
  bind mounts and named volumes.
- **Implicit `~/.cache/uv` host mount removed** from `_default_home_mounts()`.
  Existing repos lose the implicit uv cache on first upgrade until they
  uncomment the new template snippet — accepted as a one-time migration cost.
- **Schema version stays at "1"**. The `volumes:` field is purely additive
  with a default, so no migration is required.
- **`None` coercion**: an empty `volumes:` YAML section (all entries
  commented out) parses to `None`, so `DockerConfig` has a before-validator
  that coerces `None` → `[]`, matching the `mounts:` behaviour.

### Patterns established

- New top-level docker-config fields with their own pydantic submodel
  follow `CacheVolume` (extra="forbid", per-field validators with clear
  error messages).
- A named-volume `Mount` is `Mount(src=f"hatchery-{name}", dst=path, mode="rw", volume=True)`.
- Before adding any new mount-source category in the future, prefer
  extending `Mount` rather than introducing a parallel dataclass —
  `mount_to_docker_args` is the conversion choke point.

### Files changed

- `src/seekr_hatchery/mount.py` — `Mount.volume` flag, updated converter.
- `src/seekr_hatchery/docker.py`:
  - `CacheVolume` model, `DockerConfig.volumes`, `validate_volumes` before-validator.
  - `_construct_volume_mounts`, wired into both `build_mounts` branches and `launch_sandbox_shell`.
  - `_ensure_volumes` called from `_run_container`.
  - `_default_home_mounts` no longer mounts `~/.cache/uv`.
- `src/seekr_hatchery/resources/docker.yaml.template` — removed uv host mount,
  added commented `volumes:` section.
- `README.md` — "Persistent cache volumes" subsection; removed `~/.cache/uv`
  from the auto-mounted list.
- Tests: new `TestDockerConfigVolumes`, `TestConstructVolumeMounts`,
  `TestEnsureVolumes`, `TestDefaultHomeMounts`,
  `TestBuildMountsIncludesVolumes`; added `test_named_volume` cases to
  `test_mount.py`.

### Gotchas

- Docker auto-creates referenced volumes on `run -v name:/path`; the
  explicit `_ensure_volumes` step is belt-and-suspenders. It gives us a
  predictable log line and a place to hook future volume tagging (e.g.
  for a `hatchery volume prune` command).
- On podman, `volume create <name>` is **not** idempotent — re-running on
  an existing volume returns non-zero. That's why we `inspect`-first
  rather than always `create`. Don't switch to unconditional `create`.
- The pre-existing `tests/test_kubectl_proxy.py` SIGILLs on this aarch64
  sandbox due to the `cryptography` C extension; this crash predates this
  branch and is unrelated.
