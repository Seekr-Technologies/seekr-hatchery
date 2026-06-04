# Task: reduce-virtiofs-burden

**Status**: complete
**Branch**: hatchery/reduce-virtiofs-burden
**Created**: 2026-06-02

## Objective

Move per-task agent state off the host filesystem (and therefore off
virtio-fs on macOS) onto container-engine-managed storage. Agent state
that lives on the host transits virtio-fs on every read/write and gets
scanned by host EDR — under concurrent-sandbox load this produces
correlated I/O stalls. Land a general mechanism so any backend can
move its hot files off virtio-fs without one-off plumbing.

## Context

`docker.yaml` already supports user-declared cache volumes (the
[cache-volumes](2026-06-02-cache-volumes.md) work) for things like `~/.cache/uv`.
Those volumes are cross-task shared and start empty — fine for caches, not
useful for agent state that needs per-task isolation and meaningful initial
contents.

This task adds:

1. A unified **Mount** type built from Pydantic models, replacing the previous
   flat `Mount` dataclass.
2. A **seeded volume** lifecycle: per-task named volumes that get populated
   on first launch by a backend-supplied callable, persist across resumes,
   and are cleaned up on `hatchery rm`.

## What changed

### 1. Unified `Mount` tagged union

`src/seekr_hatchery/mount.py` now defines three Pydantic models discriminated
by a literal `kind` field:

```python
class BindMount(BaseModel):
    kind: Literal["BIND"] = "BIND"
    src: Path
    dst: str
    mode: MountMode = "RW"

class VolumeMount(BaseModel):
    kind: Literal["VOLUME"] = "VOLUME"
    name: str
    dst: str
    mode: MountMode = "RW"
    is_file: bool = False
    seed: Callable[[SeedContext], Mapping[str, bytes] | bytes] | None = None
    task_scoped: bool = True

class TmpfsMount(BaseModel):
    kind: Literal["TMPFS"] = "TMPFS"
    dst: str

Mount = Annotated[BindMount | VolumeMount | TmpfsMount, Field(discriminator="kind")]
```

Backends return `list[Mount]` from `construct_mounts`. The launch path
dispatches via `isinstance`. `mount_to_docker_args` serialises each variant
to the right CLI flag — `-v` for binds and dir-shaped volumes, `--mount
type=volume,...,subpath=...` for file-shaped volumes, `--tmpfs` for tmpfs.

The discriminator is what lets docker.yaml grow `kind: VOLUME` entries
later if we want user-declared seeded volumes (out of scope here).

### 2. Seeded-volume lifecycle

`src/seekr_hatchery/seeded_volumes.py` materialises `VolumeMount` entries
before the launch:

- **Per-task name resolution**: `task_scoped=True` (default) maps the
  logical spec name to `{meta.container_name}-vol-{name}`. User-config
  volumes set `task_scoped=False` and keep their name verbatim.
- **Ensure**: `<runtime> volume inspect` → on miss, `volume create`.
- **Seed once**: if a volume is newly created and the spec has a `seed`
  callable, call it with `SeedContext(session_dir, proxy_token,
  container_workdir)` and stream the result into the volume via a
  helper container (`<runtime> run --rm -i --user 0:0 -v vol:/seed
  <meta.image_name> sh -c 'tar -xf - -C /seed && chown -R 1000:1000 /seed'`).
- **Resume**: on subsequent launches the volume already exists; the seed
  callable is NOT re-invoked. The container's writes persist.
- **Rollback**: if the seed callable raises or the helper container fails,
  the half-created volume is `rm --force`d so the next launch reseeds
  cleanly.

The helper image is the already-built sandbox image — no separate
dependency to pull or keep in sync with the host runtime.

### 3. File-shaped volume mounts (`is_file=True`)

Named volumes always present as directories on the kernel side, so
backends that need to surface a single file at a fixed in-container
path (e.g. some config file the agent hardcodes) declare `is_file=True`.
`mount_to_docker_args` emits subpath syntax for these:

```
--mount type=volume,source=NAME,target=/the/file/path,subpath=basename
```

The kernel surfaces a single file from the volume at `target`. Reads
and in-place writes (`open(O_TRUNC) + write`) work. **Atomic rename
writes (`mv tmp target`) fail with `EBUSY`** because the target is a
kernel mount point — backends that need atomic-rename support need a
parent-dir-mount + symlink approach instead (deliberately not
implemented here; the one consumer of `is_file=True` so far happens
to fall back to in-place writes on rename failure).

The seed callable for `is_file=True` returns raw `bytes`; the
lifecycle keys them under `basename(dst)` inside the volume so the
subpath mount finds them.

### 4. Cleanup on `hatchery rm`

`cleanup_task_volumes(repo, name)` enumerates every volume with the
per-task prefix and removes it. Wired into `cli._do_delete` **only** —
`mark_done` and `chat_post_exit` deliberately do not clean up, so
`hatchery resume <task>` preserves accumulated agent state (sessions,
history, per-project flags, etc.) until the user explicitly deletes
the task.

The `--filter name=` flag means different things on docker (substring)
and podman (regex), so the implementation passes the bare prefix to
narrow at the runtime and re-anchors with `startswith` in Python.

## What stayed the same

- Bind mounts: every existing call site (the worktree mount, repo
  layering, kubectl proxy, includes, etc.) was mechanically migrated to
  `BindMount` with no behaviour change.
- Tmpfs: same — `TmpfsMount`.
- User-config cache volumes from `docker.yaml`: still work, now expressed
  as `VolumeMount(task_scoped=False)`. Tests pin this contract.
- Backend interface signatures (`construct_mounts`, `on_new_task`,
  `on_before_container_start`): same. CodexBackend's
  `on_before_container_start` becomes a no-op since the auth synthesis
  moved into the seed callable; the abstract slot stays put until
  another cleanup pass.

## Verification

- `pytest --ignore=tests/test_kubectl_proxy.py` — full unit suite
  passes. `test_mount.py` and `test_seeded_volumes.py` are the
  new/rewritten suites; the docker / filesystem / agent_codex suites
  were mechanically migrated to the new Mount types.
- `pytest --integration tests/test_seeded_volumes.py` — 8 integration
  tests against real podman: full lifecycle (create + seed + read),
  resume idempotency, rollback on seed failure, `task_scoped=False`
  contract for user-config volumes, selective cleanup.
- Manual smoke: launch + use a sandbox; the seeded volumes appear under
  `podman volume ls` with names matching `{container_name}-vol-{spec}`;
  `hatchery rm <task>` removes them.

## Followups (not in this PR)

- User-declared seeded volumes via `docker.yaml` (would need a `seed:`
  field or a separate source path).
- `hatchery prune` for orphan volumes left behind by crashed sessions
  (the per-task cleanup on `rm` covers the normal path).
- Drop the now-no-op `on_before_container_start` from the abstract
  base, since codex's only use of it (auth.json synthesis) moved into
  the seed callable.
