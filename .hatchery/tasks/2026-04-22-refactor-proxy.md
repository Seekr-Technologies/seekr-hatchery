# Task: refactor-proxy

**Status**: complete
**Branch**: hatchery/refactor-proxy
**Created**: 2026-04-22 10:56

## Objective

Refactor the agent API proxy lifecycle in docker.py to use a context manager, consistent with `_kubectl_context` introduced in hatchery/kubectl-proxy.

## Context

`_run_container` previously called `proxy.start_proxy()` at the top and `proxy.stop_proxy()` in a `finally` block, mixing two concerns: building/running the docker command, and managing the proxy process lifetime.

`_kubectl_context` established the pattern that lifecycle management belongs in a context manager at the call site, not inside `_run_container`.

## Summary

### What was done

Added `_api_proxy_context(mutator, proxy_token, backend) -> Generator[ApiProxy | None, None, None]` to `docker.py` (placed adjacent to `_kubectl_context`). When `mutator` is `None` it yields `None` immediately (no-op). Otherwise it starts the proxy, yields an `ApiProxy(port=..., base_url=...)` dataclass, and stops the proxy on exit.

`ApiProxy` is a public dataclass (not private) since it crosses the module boundary — it is referenced in `tests/test_sandbox.py`.

Updated `_run_container` to accept `proxy_port: int | None = None` as a plain parameter. Removed the `proxy_server` local variable, the `proxy.start_proxy()` call, and both `proxy.stop_proxy()` calls from the two `finally` blocks. Also removed the `try/finally` wrapper around the `_command_override` branch since there's no longer anything to clean up there.

Updated all three call sites to use paired `with` blocks:
```python
with (
    _api_proxy_context(mutator, proxy_token, backend) as api_proxy,
    _kubectl_context(config, session_dir) as kubectl_mounts,
):
    mounts.extend(kubectl_mounts)
    _run_container(..., proxy_port=api_proxy.port if api_proxy else None, add_host_gateway=bool(kubectl_mounts))
```

### Key decision: `add_host_gateway` stays

The task notes suggested removing `add_host_gateway` since both `proxy_port` and `kubectl_mounts` are visible at the call site. However, `_run_container` doesn't receive `kubectl_mounts` — those are folded into `mounts` before the call. Removing `add_host_gateway` would break the case where `launch_sandbox_shell` has kubectl configured but no API proxy (`proxy_port=None`): the container still needs `host.docker.internal` to reach the kubectl RBAC proxy on the host.

Resolution: keep `add_host_gateway: bool = False` in `_run_container`, but set it at call sites using `bool(kubectl_mounts)` instead of `(config.kubernetes is not None)`. This is semantically equivalent (kubectl_mounts is non-empty iff kubernetes is configured) but reads more directly from what's actually been set up.

### Test changes

`TestRunContainerRuntime._capture_cmd` previously monkeypatched `proxy_mod.start_proxy` and `proxy_mod.stop_proxy` to inject a predictable port. Since `_run_container` no longer calls those, the monkeypatching was removed and `proxy_port` is now passed directly to `_run_container`. `test_no_api_key_env_when_mutator_is_none` had its `start_proxy` assertion guard removed for the same reason.

### Files changed

- `src/seekr_hatchery/docker.py` — added `ApiProxy` dataclass and `_api_proxy_context`, updated `_run_container` signature and body, updated three call sites
- `tests/test_docker.py` — removed proxy monkeypatching from `_capture_cmd`, pass `proxy_port` directly
- `tests/test_sandbox.py` — updated `no_wt_run` fixture to wrap `_run_container` with `_api_proxy_context`
