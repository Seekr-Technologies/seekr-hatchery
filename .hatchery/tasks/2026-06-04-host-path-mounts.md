# Task: host-path-mounts

**Status**: complete
**Branch**: hatchery/host-path-mounts
**Created**: 2026-06-04 15:53

## Objective

Make the sandbox indistinguishable from a native run as far as absolute
paths are concerned: mount the host repo / worktree / cwd at its own host
path inside the container, instead of at a fixed `/repo` or `/workspace`.
The container's WORKDIR then equals the host CWD, and any tool that keys
state off `pwd` — agent per-project directories, lockfiles, generated
artifacts, symlink targets — sees the same absolute paths whether the
session is sandboxed or native.

The motivating symptom was an agent's per-project state directory
fragmenting into one entry per worktree (because every sandbox worktree
had a different container CWD), preventing memory and history from
persisting across tasks on the same repo. Under host-path mirroring, the
per-project directory matches what a native run would write and the
agent's own per-repo consolidation (when it has one) does the right
thing automatically. The fix is general, though — it benefits any tool
with path-keyed state and removes a class of "paths inside the sandbox
don't match the host" footguns (symlinks across repos, absolute paths in
lockfiles, etc.).

## Context

Two solutions were considered:

1. **Compute the agent's project slug ourselves** and inject it via the
   agent's own settings (some agents expose a knob for this). Surgical,
   but per-agent — not every backend has an equivalent — and depends on
   each agent's internal slug rule, which can drift.

2. **Mount container paths at host paths.** Make the sandbox
   indistinguishable from a native run as far as paths are concerned.
   Bigger change but covers everything path-keyed, now and in the
   future. Agents' home-directory subdirs are already bind-mounted into
   the container (so writes from inside hit the host), so once the
   container CWD equals the host CWD, all per-project state lands where
   a native run would write it.

Picked option 2: the requirement is "memories / per-project state must
function exactly the same as if we never sandboxed" — option 1 only
handles one specific agent.

## Summary

The hardcoded container paths `CONTAINER_REPO_ROOT = "/repo"` and
`/workspace` were dropped. Worktree mode now mounts the worktree at
`str(meta.worktree_path)` and lays `.git` / sentinel mounts at
`str(meta.repo_path)/.git/...`. No-worktree mode mounts cwd at
`str(meta.worktree_path)`. The container WORKDIR is the host worktree
path (or host cwd, no-worktree). `launch_sandbox_shell` mounts the repo
at its host path.

Because container paths equal host paths, the worktree's existing `.git`
pointer file (`gitdir: <host_repo>/.git/worktrees/<name>`) already
resolves correctly inside the container — the previous rewrite-and-shadow
machinery (per-session `git_ptr` file plus a bind-mount over the worktree's
`.git`) was removed for the primary worktree.

The `follow_symlinks` walker (`_construct_symlink_mounts`) was simplified
along the same lines. Previously it rejected two link shapes that couldn't
survive the host→container path remap (absolute links into the scan_root,
relative links escaping it). Under host-path mirroring there is no remap,
so both classes now Just Work: absolute internal links are covered by the
scan_root mount, and relative external links resolve to the same absolute
path on both sides — emit a `target:target` mount and they work like
absolute external links did before. The walker collapses to "if target is
outside scan_root and outside the system blocklist, mount it; else skip."

**Key files changed:**
- `src/seekr_hatchery/constants.py` — dropped `CONTAINER_REPO_ROOT`.
- `src/seekr_hatchery/docker.py` — `launch_context`, `build_mounts`
  (both branches), `run_session`, `launch_sandbox_shell` derive paths
  from `meta.repo_path` / `meta.worktree_path` / `repo`. Added
  `_check_host_path_safe_for_mount()` collision guard that rejects host
  paths colliding with critical container paths (`/usr`, `/etc`,
  `CONTAINER_HOME`, etc.).
- `src/seekr_hatchery/sessions.py` — `sandbox_context()` emits host
  paths in the system prompt; the docker-worktree branch tells the
  agent to use `git show main:path` since the RO main-branch file view
  is gone.
- `src/seekr_hatchery/resources/Dockerfile.template` — dropped
  `WORKDIR /repo` (the container runtime sets WORKDIR via `-w` at run
  time using the host path).
- Tests: `test_pure.py`, `test_sandbox.py`, `test_session_io.py` updated
  to assert against fixture host paths instead of hardcoded `/repo` /
  `/workspace`. Includes-related tests still use `/includes/<basename>`
  — included secondary repos are out of scope for this change (see
  follow-up below).

**Tradeoff (call out for future agents):** the RO main-branch file view
that used to exist at `/repo` is gone. The worktree mount now overlays
the parent repo path, so files like `cat <host_repo>/src/foo.py` show
the *worktree branch's* version, not main's. Use `git show main:src/foo.py`
or `git diff main...` instead. `.git/objects` is mounted RW so git
operations work fully.

**Migration note:** existing per-CWD directories under agents' home-state
dirs (anything named after `-repo*` etc.) are not cleaned up
automatically. They're harmless leftovers; the user can `rm -rf` them
after switching over. In-flight sessions started before this change end
up with their session log at the old container-path-derived location;
they're still readable but won't be auto-discovered after the switch.

**Follow-up worth considering:** apply the same host-path-mirroring to
included secondary repos (currently mounted at `/includes/<basename>`).
Same arguments (symlinks/lockfiles work, per-include state consolidates
with native runs, code simplification — drops `_unique_basename`, the
`CONTAINER_INCLUDES_ROOT` constant, and the include-side `.git` pointer
rewrite). Punted to a separate PR to keep the scope contained and the
collision-guard surface area small.

**Things to be aware of when modifying this area:**
- The `_git_worktree_mounts` helper takes `container_root` as a string;
  pass `str(meta.repo_path)` for the primary repo. Included secondary
  repos still mount at `/includes/<basename>` — their host path doesn't
  match the container path, so they still need the per-include `.git`
  pointer rewrite in `_docker_mounts_includes`.
- The primary worktree's `.git` pointer is **not** rewritten. If you
  ever change the container path away from the host path (e.g.,
  reintroduce a fixed `/repo`), you'll need to bring back the pointer
  rewrite + shadow-mount that lived in `run_session` / `build_mounts`.
- The collision guard rejects host repos that resolve to a path on the
  container blocklist (`/`, `/usr`, `/etc`, `CONTAINER_HOME`, ...).
  Subpaths under `/home/hatchery/...` are fine — they just add a subdir
  to the container's home.
