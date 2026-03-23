# Task: sandbox-refresh

**Status**: complete
**Branch**: hatchery/sandbox-refresh
**Created**: 2026-03-19 15:31

## Objective

Add the ability to force a fresh Docker/Podman image build (bypassing the layer cache) so tools installed in the image get updated to their latest versions.

## Context

When `hatchery new` or `hatchery resume` builds the sandbox image, it always hits the layer cache. This means tools installed in the image are never updated unless the Dockerfile itself changes.

## Summary

Added a `--rebuild-sandbox` flag to the `new`, `resume`, and `sandbox` CLI commands. When set, `--no-cache` is passed to the underlying `docker/podman build` command.

**Call chain:**
```
cmd_new/cmd_resume/cmd_sandbox --rebuild-sandbox
  → _launch_new/_launch_resume(no_cache=True)
    → docker.launch_docker/launch_docker_no_worktree/launch_sandbox_shell(no_cache=True)
      → build_docker_image(no_cache=True)
        → [..., "--no-cache", ...]
```

**Files changed:**
- `src/seekr_hatchery/docker.py`: Added `no_cache: bool = False` parameter to `build_docker_image`, `launch_docker`, `launch_docker_no_worktree`, and `launch_sandbox_shell`. The `--no-cache` flag is appended to the build command when `no_cache=True`.
- `src/seekr_hatchery/cli.py`: Added `no_cache: bool = False` parameter to `_launch_new` and `_launch_resume`. Added `--rebuild-sandbox` Click option to `cmd_new`, `cmd_resume`, and `cmd_sandbox`. All call sites thread `no_cache=rebuild_sandbox` through.

**Design decision:** Used `--rebuild-sandbox` rather than `--refresh` or `--no-cache` to be explicit about what is being rebuilt (the sandbox image, not data or config). All parameters default to `False` so existing behaviour is unchanged.
