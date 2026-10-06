# Sandbox sidecars

Hatchery sidecars are host-side services whose lifecycle brackets a sandbox
container. Hatchery validates them before building the image, starts them before
the container, contributes their mounts and environment, and stops them in
reverse order afterward.

Built-in sidecars:

- [API credential proxy](api-proxy.md)
- [Kubernetes RBAC proxy](kubernetes.md)
- [OCI Object Storage credential proxy](oci.md)
- [S3 credential proxy](s3.md)

Configured sidecars are available in task sessions and in
`hatchery sandbox shell`.
