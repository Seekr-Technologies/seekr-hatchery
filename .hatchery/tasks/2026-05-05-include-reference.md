# Task: include-reference

**Status**: complete
**Branch**: hatchery/include-reference
**Created**: 2026-05-05 09:44

## Objective

Add two "reference" modes for `--include` (mounting additional directories into the sandbox) alongside the existing "worktree" mode, and support switching modes at resume time by editing docker.yaml.

## Context

The existing `--include` feature had one behaviour: for git repos it created a `hatchery/<name>` worktree for branch isolation; for plain directories it did a simple rw mount. The task asked for:

- **reference mode** — include a dir without creating a worktree (read-only or read-write)
- **mode switching** — stop sandbox, edit docker.yaml, resume → worktrees created or removed automatically

## Summary

### Design decision: single `mode` field with 3 values

Rather than separate `mode` + `access` fields, we collapsed everything into one field:

| mode | behaviour |
|------|-----------|
| `worktree` | existing behaviour — creates `hatchery/<name>` branch + worktree (rw) |
| `rw` | reference mount, read-write, no worktree |
| `ro` | reference mount, read-only, no worktree |

### New data type: `IncludeEntry` (tasks.py)

`@dataclass IncludeEntry(path: Path, mode: str = "worktree")` is the central type that flows through all include-related call sites. Helpers: `serialize_include_entries`, `load_include_entries` (with backward-compat for old `list[str]` meta.json format).

### CLI flags added to `hatchery new`

- `--include PATH` — unchanged (worktree mode)
- `--include-rw PATH` — reference, rw
- `--include-ro PATH` — reference, ro

### docker.yaml syntax extended

Accepts both legacy strings (worktree mode) and new dict entries:
```yaml
include:
  - ../other-repo              # worktree (unchanged)
  - path: ../shared-types
    mode: rw
  - path: /docs
    mode: ro
```

`DockerConfig.include` type changed from `list[str]` to `list[str | dict]` with a Pydantic validator. `parse_docker_include_entry()` helper normalises both forms.

### meta.json format change

New: `"include": [{"path": str, "mode": str}, ...]`  
Old format `[str]` is silently upgraded on read via `load_include_entries`.

### Resume reconciliation (`_reconcile_include_modes` in cli.py)

On `hatchery resume`, docker.yaml is re-read and modes are compared to meta.json. If any path changed:
- `worktree → ro/rw`: removes the worktree and deletes the branch
- `ro/rw → worktree`: creates the worktree

Updated meta.json is saved so subsequent resumes are idempotent. CLI-sourced paths (not in docker.yaml) keep their original mode.

### Files changed

- `src/seekr_hatchery/tasks.py` — `IncludeEntry` dataclass + helpers; `sandbox_context` signature
- `src/seekr_hatchery/docker.py` — `DockerConfig.include` validator; `parse_docker_include_entry`; `_docker_mounts_includes` updated; `launch_docker*` signatures
- `src/seekr_hatchery/git.py` — `create/remove_include_worktrees`, `delete_include_branches` filter on `mode == "worktree"`
- `src/seekr_hatchery/cli.py` — new flags; `_resolve_includes`; `_reconcile_include_modes`; all `include_repos: list[Path]` → `list[IncludeEntry]`; meta serialisation
- `src/seekr_hatchery/resources/docker.yaml.template` — updated comments with examples
- `tests/test_git.py`, `tests/test_docker.py`, `tests/test_pure.py`, `tests/test_cli.py` — updated for `IncludeEntry` API; new reference-mode tests added

### Gotchas

- `DockerConfig.include` is now `list[str | dict]` — any code that assumes it's `list[str]` will break. The `parse_docker_include_entry` helper is the canonical way to consume entries.
- `_reconcile_include_modes` has a broad `except Exception` guard around `docker.load_docker_config` — intentional, since a corrupt or missing docker.yaml should not prevent a resume.
- The `TestCliResume::test_resume_dispatched` test in test_cli.py hangs (pre-existing issue, unrelated to this task).
