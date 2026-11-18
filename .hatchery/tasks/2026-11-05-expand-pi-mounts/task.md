# Task: expand-pi-mounts

**Status**: complete
**Branch**: expand-pi-mounts
**Created**: 2026-11-05 13:50

## Objective

Make user-added Pi files available automatically in sandboxes without exposing
the host's active authentication/model credentials or losing existing session
history.

## Summary

### Decision

Bind-mount the entire host `~/.pi/agent` RW instead of maintaining an allowlist.
Instructions such as `APPEND_SYSTEM.md`, profiles, missions, caches, npm modules,
and future files are shared immediately. Configuration and package changes now
affect the host and other tasks.

Overlay `auth.json` and `models.json` with task-local bind files in
`session_dir/pi-config/`. The existing exec wrapper regenerates them with proxy
credentials and sanitized host model metadata. Always supply the sanitized input,
using an empty object when no host providers exist. Use separate, initially
UID-1000-writable `pi-bin` and `pi-sessions` volumes for native binaries and history.

### Pi package and ownership

Pi is consolidated under `agents/pi/`: `__init__.py` preserves the public
`PiBackend` import, `backend.py` owns launch/auth/config behavior, `migrations.py`
owns storage upgrades, and `wrapper.sh` is colocated and included in built wheels.
Tests mirror the source in `tests/agents/pi/`. Migration/state management is local
rather than a speculative shared framework; the implementation is 175 production
lines, within the planned 140–200 range. A second concrete backend consumer can
justify extracting common mechanics later.

### Compatibility

Pi owns a sequential storage-layout pathway in `agents/pi/migrations.py`:

- V1: sessions inside the per-task `pi-dir` volume; missing version marker means V1.
- V2: shared agent directory and separate `pi-sessions`/`pi-bin` volumes.

A fresh task takes a no-op V1→V2 step. Existing V1 history is imported read-only
and given non-root ownership. An unexpected destination in unmarked V1 state is
a collision, not evidence of V2; corrupt/newer state and invalid transitions fail
closed. No unmerged-branch interim layout or pending-file compatibility is needed.

A runtime/immutable-task-scoped lock covers version reads, recovery and commit.
Atomic, fsynced JSON under `<task-dir>/pi-container-state/<identity>.json` records
`layout_version` and an optional `IN_PROGRESS` transition with source/target
versions. Progress retains V1 until copy/chown succeeds; completion persists V2
with no transition, so subsequent launches cannot replay old history.

Recovery stops the named orphan helper before discarding partial output and
retrying. Failed cleanup retains in-progress state; missing legacy history during
recovery aborts without deleting remaining partial data. The old source always
stays untouched. This serializes migration only, not task-container execution.

The backend module owns state/version management and concrete steps (Option A),
not a shared framework. Existing config/task/DB migrations stay unchanged: pure
dictionary normalizers would not benefit from container recovery machinery. The
module documents appending sequential upgrades and retiring V1 only after its
support is explicitly dropped. Shared volume preparation has no Pi dispatch.

### Lifecycle boundary

`AgentBackend.migrate_container_state(meta, *, runtime)` is a default no-op
lifecycle phase; Pi delegates it to its migration module. Orchestration runs:
image build → migration → `on_before_container_start` → mounts/seeding → container.
The preparation hook now only stages per-launch model metadata. Both hooks receive
task metadata and the selected runtime; no backend rediscovers the engine.

Migration and preparation run for containerized new/resume/finalize, not native
runs or standalone sandbox shells. Background workers start earlier and stop after
agent exit. Either hook's failure prevents later phases and agent execution.

### Patterns and gotchas

- Single-file shadows allow direct writes but not atomic replacement via rename.
  File-shaped volumes were avoided because their startup symlinks would modify
  the shared host directory.
- Arbitrary backups, including `models.bak`, remain accessible. This is deliberate
  broad sharing, not automatic discovery or filtering of all secret files.
- `BindMount.follow_links` uses `bool | Literal["SHALLOW", "RECURSIVE"]`.
  `True` and `"SHALLOW"` are equivalent; `"RECURSIVE"` scans discovered directory
  targets too, and `False` disables discovery. Enum-style values are uppercase.
  Pi opts into the shared recursive resolver, with no resource-name special cases
  or symlink discovery in the backend. Relative nested links resolve from each
  target's container location; host ancestry stops cycles without suppressing
  independent aliases. Companion mounts inherit RO/RW and expansion is idempotent.
- Symlink expansion skips paths hidden by overlays, including credential files
  and per-task directories; directory exclusions still apply. It also refuses
  companions whose source overlaps a hidden host entry, its resolved symlink
  target, or an ancestor directory. Credential aliases cannot bypass overlays;
  same-source/same-mode nested binds remain shareable. New external symlinks may
  require container recreation.
- Declared overlays retain ancestor guards, but generated child companions cannot
  suppress later-discovered parent targets. Late parents mount before generated
  descendants; unrelated/shallow-only discovery order is preserved. Generated
  ancestor coverage is reusable only for matching source mapping and access mode;
  specific child RO/RW overlays survive in either discovery order. Recursive scan
  scheduling is independent of mount-emission deduplication and keyed by source,
  container destination, mode and scan strength, so prior SHALLOW coverage cannot
  suppress a later RECURSIVE request. Generated child overlays are scanned in
  their own mode; exact-destination conflicts retain first-declaration behavior.
- Existing containers keep their current mounts until recreated; history migrates
  when their V1 storage state is upgraded.

### Files and validation

Changed the backend interface and Pi/Codex implementations, `docker.py`, `mount.py`,
`mount_links.py`, matching tests/fixtures, and `README.md`; generic volume preparation
remains backend-agnostic. Existing configuration and metadata migrations were not
folded into container-state migration machinery.

- Targeted unit and live Podman tests: 488 passed, covering credential shadows
  (missing/regular/symlinked originals), user-file sharing, symlinked instructions
  and nested dependencies, non-root volume writes, history migration, preservation
  on subsequent resumes, rollback/retry after a failed copy, and generic recursive
  traversal with absolute/relative targets, aliases, cycles, and overlays.
  Lifecycle tests verify image-build → migration → preparation → mount construction
  → seeding → run, with either hook failure preventing later steps. Routing covers
  new/resume/finalize; default no-op and native behavior remain covered.
- Version-path tests cover implicit/explicit V1, fresh no-op upgrade, completed V2
  without engine queries, strict corrupt/future/invalid-transition rejection,
  collision refusal, readable in-progress state, and recovery/commit boundaries.
  A delegated worker implemented the migration/test seam; a fresh read-only reviewer
  found no actionable issues after source/test inspection.
- Built and extracted the wheel outside the checkout: public `PiBackend`, local
  wrapper, new migration module, fresh V1→V2 and completed no-op all passed.
- Panel follow-up validation covers host/source alias protection with live read/write
  checks, both parent/child discovery orders, KeyboardInterrupt, failed cleanup,
  actual process death leaving an orphan helper, missing-source recovery, and
  overlapping hooks waiting for a committed import. Fixes were kept in three
  separate commits as requested: alias protection, discovery order, and migration
  recovery/serialization.
- Broad suite excluding `test_kubectl_sidecar.py`: 1,393 passed, 46 skipped.
  The full suite crashes with SIGILL importing cryptography's native extension
  during unrelated kubectl TLS setup in this container.
- Requirement-aware companion reuse adds both-order regressions for RW/RO parent
  and child combinations, mixed shallow/recursive requests, source mappings and
  relative aliases. Twelve new live Podman variants verify permissions and actual
  dependency reads. Original review reproductions now pass on both stack stages;
  the infrastructure snapshot passed 405 focused tests. A fresh reviewer found
  no actionable issues in the fix. The fix remains in the infrastructure commit;
  the Pi-specific implementation is unchanged apart from this task record.
- Ruff formatting/lint and `git diff --check` passed.
- CI portability follow-up: the system-path symlink test expects the resolved
  `/usr/bin/env` source while retaining the literal destination. Some runners
  symlink `env` to a separate coreutils installation; production already handles
  this correctly.
