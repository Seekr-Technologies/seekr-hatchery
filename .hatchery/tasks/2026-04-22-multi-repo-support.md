# Task: multi-repo-support

**Status**: complete
**Branch**: hatchery/multi-repo-support
**Created**: 2026-04-22 16:09

## Objective

Users often need a single hatchery agent session with access to two or more repos simultaneously. The old workaround (create a parent dir, clone repos into it, run `hatchery new --no-worktree`) provided no branch isolation, no worktrees, and no lifecycle cleanup.

## Context

Add a first-class `--include <path>` flag to `hatchery new` that mounts additional directories inside the Docker container at `/includes/<basename>/`, with optional worktree isolation for git repos.

## Summary

### Key decisions

**Mount strategy**: Secondary repos use a simple full rw bind-mount (`-v /host/path:/includes/<basename>:rw`). This naturally includes `.git/` so the agent can commit and changes are visible on the host — no layered git mount needed.

**Worktree isolation**: When an included path is a git repo and worktrees are enabled (the default), `hatchery new` creates a `hatchery/<task-name>` worktree in that secondary repo, just like the primary. The worktree's `.git` pointer file is rewritten for container paths (same technique already used for the primary repo).

**`.git` pointer rewrite**: Worktrees have a `.git` file pointing to `<repo>/.git/worktrees/<name>`, a host-absolute path that doesn't resolve inside the container. We write a corrected pointer to `session_dir/git_ptr_include_<basename>` and bind-mount it over the worktree's `.git` file.

**docker.yaml `include:` section**: Config-file includes merged with CLI `--include` at `hatchery new` time (deduplicated by resolved absolute path).

**Basename collision**: Two included paths sharing a basename get a numeric suffix: `/includes/api-1/`.

**No-worktree mode**: `--include` works too — just a plain rw mount, no worktree created.

**Non-git dirs**: Plain directories are just mounted rw; no worktree, no git pointer.

**Lifecycle**: `"include": ["/abs/path/..."]` stored in task metadata. `resume` reconstructs mounts. `done` removes secondary worktrees. `delete` also deletes the secondary branches.

### Files changed

| File | Change |
|------|--------|
| `src/seekr_hatchery/tasks.py` | `CONTAINER_INCLUDES_ROOT = "/includes"`; `_include_container_basename()` helper; `sandbox_context()` extended with `include_paths` parameter |
| `src/seekr_hatchery/git.py` | `create_include_worktrees()`, `remove_include_worktrees()`, `delete_include_branches()` helpers |
| `src/seekr_hatchery/docker.py` | `include: list[str] = []` on `DockerConfig`; `_unique_basename()` helper; `docker_mounts_includes()`; `launch_docker()` and `launch_docker_no_worktree()` extended with `include_repos` |
| `src/seekr_hatchery/resources/docker.yaml.template` | Commented `include:` section added |
| `src/seekr_hatchery/cli.py` | `--include` option on `cmd_new`; `_resolve_include_repos()` helper; worktree creation/cleanup wired into new/resume/done/delete |
| `tests/test_docker.py` | `TestDockerMountsIncludes`, `TestDockerConfigInclude` |
| `tests/test_pure.py` | `TestSandboxContextIncludePaths` |
| `tests/test_task_io.py` | `TestIncludeRoundTrip` |

### Gotchas

- **`test_no_worktree_skips_git_ptr` assertion**: The assertion `"git_ptr" in m` was too broad — pytest's `tmp_path` for a test named `test_no_worktree_skips_git_ptr` contains the literal string "git_ptr". Changed to check `session_dir / "git_ptr_include_repo-b"` explicitly.
- **`DockerConfig` uses `extra="forbid"`**: The `include` field must be declared on the model; adding unknown fields at load time raises a Pydantic validation error.
- **`_resolve_include_repos()` deduplicates by resolved absolute path**: CLI flags take precedence (listed first), config file entries are appended if not already present.
- **`_launch_finalize()` in cli.py**: This function was not extended with `include_repos` because finalize mode doesn't need to reconstruct docker mounts — it reuses the already-running session.
