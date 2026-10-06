# Kubernetes RBAC proxy

The Kubernetes sidecar exposes selected host kubeconfig contexts without
mounting the host kubeconfig into the sandbox. Each configured context gets a
host-side `kubectl proxy` and an RBAC-filtering proxy. Hatchery writes a
sandbox-only kubeconfig containing the filtered endpoints.

Enable the kubectl installation block in `.hatchery/Dockerfile`, then configure
`.hatchery/docker.yaml`:

```yaml
kubernetes:
  contexts:
    - context: my-dev-cluster
      rules:
        - verbs: [get, list, watch, create, update, patch, delete]
          resources: ["*"]
          namespaces: ["*"]
    - context: my-production-cluster
      rules:
        - verbs: [get, list, watch]
          resources: ["*"]
          namespaces: ["*"]
```

The first healthy context is the default. Select another with
`kubectl --context <name> ...`. A context that cannot start is omitted with a
warning; startup fails if no configured context can start.

A top-level `context:` and `rules:` pair is available as a single-context
shorthand and cannot be combined with `contexts:`.

## Rules

Rules are an additive allowlist. Unmatched requests are denied.

- Kubernetes verbs map to HTTP methods: `get`, `list`, and `watch` use GET;
  `create` uses POST; `update` uses PUT; `patch` uses PATCH; and `delete`
  or `deletecollection` use DELETE.
- `describe` is a kubectl command rather than an API verb; allow `get`.
- Namespace `"*"` matches all namespaces.
- Namespace `""` matches cluster-scoped requests.
- The `exec`, `attach`, `portforward`, and `proxy` subresources are always
  blocked.
