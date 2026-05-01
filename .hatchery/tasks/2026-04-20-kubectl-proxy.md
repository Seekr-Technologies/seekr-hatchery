# Task: kubectl-proxy

**Status**: complete
**Branch**: hatchery/kubectl-proxy
**Created**: 2026-04-20 10:34

## Objective

> I want to give the hatchery agent access to kubectl, but do not want to give them full access.
 
  Can we design a system where:
 
  - We add a `kubectl` feature
  - This gives the agent in the sandbox a working `kubectl` command
  - We proxy commands out of the container
  - We can create a virtual RBAC for the agent, limiting verbs/scope

## Context

The user wanted Kubernetes access for agents without full cluster exposure. The key constraint was that filtering had to be reliable — naive command-text parsing would be fragile (e.g. `kubectl get pods -A`, compound commands, flags in any order). The final approach filters at the HTTP API level, not the command level, making it robust by construction.

## Summary

### Architecture

```
container
  kubectl (real binary, mounted from host)
  KUBECONFIG → http://host.docker.internal:{RBAC_PORT}
       │  HTTP, bearer token from kubeconfig
       ▼
host: _RBACProxyHandler (our HTTP server, ephemeral port)
       │  parse k8s API URL → (namespace, resource, subresource)
       │  block exec/attach/portforward/proxy subresources
       │  check RBAC rules (allowlist, fail-closed)
       ▼
host: kubectl proxy --port=0 --address=127.0.0.1
       │  adds real credentials, forwards to API server
       ▼
Kubernetes API server
```

Filtering is at the HTTP API URL level (`/api/v1/namespaces/{ns}/{resource}`), not by parsing kubectl command strings. This correctly handles `kubectl get pods -A` (`GET /api/v1/pods`, namespace=""), `kubectl exec` (blocked at `/exec` subresource), and any other kubectl variant.

### Configuration (docker.yaml)

```yaml
kubectl:
  rules:
    - verbs: [get, list, watch]
      resources: ["*"]
      namespaces: ["default", "staging"]
    - verbs: [logs]
      resources: [pods]
      namespaces: ["*"]
```

- `verbs` maps to HTTP methods: get/list/watch→GET, create→POST, update→PUT, patch→PATCH, delete→DELETE
- `namespaces: ["*"]` matches all namespaces **including** cluster-scoped queries (no `/namespaces/` in URL)
- `namespaces: ["default"]` only matches namespaced requests; blocks `kubectl get pods -A`
- `namespaces: [""]` explicitly allows cluster-scoped queries
- Empty rules → deny all (fail-closed)
- `exec`, `attach`, `portforward`, `proxy` subresources are **always blocked**

### Files Changed

- **`src/seekr_hatchery/kubectl_proxy.py`** (new): Core module with `KubectlRBACRule`/`KubectlConfig` models, `parse_k8s_url()`, `check_rbac()`, `_RBACProxyHandler`, `start_rbac_proxy()`, `start_kubectl_proxy_proc()`, `make_kubeconfig()`.
- **`src/seekr_hatchery/docker.py`**: Added `kubectl: KubectlConfig | None = None` to `DockerConfig`, updated `docker_features()`, added `add_host_gateway` param to `_run_container()`, added `_setup_kubectl()`/`_teardown_kubectl()` helpers called from `launch_docker()` and `launch_docker_no_worktree()`.
- **`src/seekr_hatchery/resources/docker.yaml.template`**: Added commented kubectl section with example rules, context field, and namespace semantics.
- **`tests/test_kubectl_proxy.py`** (new): 49 tests covering URL parsing, RBAC logic, model validation, kubeconfig generation, and HTTP integration against a mock kubectl proxy.

### Configuration (docker.yaml) — full example

```yaml
kubectl:
  context: my-dev-cluster   # optional: pin kubeconfig context (default: host's active context)
  rules:
    - verbs: [get, list, watch]
      resources: ["*"]
      namespaces: ["default", "staging"]
    - verbs: [logs]
      resources: [pods]
      namespaces: ["*"]
```

### Key Decisions

1. **`kubectl proxy` as backend** (not direct kubeconfig mount): Credentials never enter the container; the RBAC proxy controls what reaches `kubectl proxy`.
2. **URL-level filtering** (not command parsing): Reliable against all kubectl flag combinations.
3. **Token auth via kubeconfig**: Same bearer-token pattern as the existing API key proxy; per-task stable token at `~/.hatchery/tasks/.../kubectl_proxy_token`.
4. **Mounting host's `kubectl` binary** (not Dockerfile change): No image rebuild required. Falls back gracefully with a warning if `kubectl` is not on host PATH.
5. **Schema version unchanged**: `kubectl: None` default is backward-compatible; existing `docker.yaml` files load without modification.
6. **`context:` field for multi-context hosts**: Users with multiple kubeconfig contexts need to pin which one `kubectl proxy` uses; the host's active context may not be the intended cluster. `KubectlConfig.context` passes `--context` to `kubectl proxy`.
6. **`launch_sandbox_shell` not modified**: No session_dir available; kubectl not supported in interactive sandbox shell.

### Gotchas for Future Agents

- Binary mount requires matching OS/arch (Linux→Linux works; macOS host→Linux container fails — the kubectl binary won't run).
- With multiple kubeconfig contexts on the host, `kubectl proxy` uses the active context unless `context:` is set in docker.yaml. A 401 from inside the container typically means the active context doesn't have valid credentials — use `context: <name>` to pin the right one.
- `docker.yaml` is read from the **repo root** for all launch paths (`hatchery new`, `hatchery sandbox`). Uncommitted changes to docker.yaml are visible to new tasks.
- Watch/log streaming (`kubectl logs -f`, `--watch`) works fine via chunked HTTP — no WebSocket needed since interactive subresources are blocked.
- The `--add-host=host.docker.internal:host-gateway` flag on Linux is now added when *either* the API proxy or kubectl proxy is active (previously it was only added inside the API proxy block).
