# OCI Object Storage credential proxy

The OCI sidecar exposes standard API-key profiles from `~/.oci/config` without
mounting the host configuration or private keys into the sandbox. It supports
the common OCI setup where the config points at a PEM key:

```ini
[DEFAULT]
user=ocid1.user.oc1...
fingerprint=00:11:22:...
tenancy=ocid1.tenancy.oc1...
region=us-ashburn-1
key_file=~/.oci/oci-api.pem
```

Configure the profiles to expose in `.hatchery/docker.yaml`:

```yaml
oci:
  default: DEFAULT
  profiles:
    DEFAULT:
      rules:
        - path: oci://my-namespace/project-artifacts/team-a/
          permissions: [READ, LIST, WRITE]
    PRODUCTION:
      rules:
        - path: oci://my-namespace/production-artifacts/published/
          permissions: [READ, LIST]
```

Each key under `profiles` is the profile name in the host OCI config and the
name visible inside the sandbox. When exactly one profile is configured, omit
`default`; Hatchery selects it automatically. Multiple profiles require a
`default`.

Use `config_file` to select a nonstandard host config path:

```yaml
oci:
  config_file: ~/.config/oci/config
  profiles:
    DEFAULT: {}
```

Before building the image, Hatchery checks that OpenSSL is available, every
profile exists, required API-key fields are present, and each `key_file`
exists. Instance principals, resource principals, and browser/session-token
profiles are not supported.

## Credential isolation

For each launch, Hatchery generates a random synthetic OCI identity and private
key. The sandbox receives a read-only OCI config containing only those
identities. Requests go to a host-side proxy, which maps the synthetic identity
to the selected host profile and re-signs the request with the host PEM key.

The host config and `oci-api.pem` never enter the sandbox. The generated
environment configures OCI CLI profile selection and its endpoint override.
SDK code must pass the `OCI_CLI_ENDPOINT` value to its Object Storage client
explicitly.

Commercial-realm regions default to
`https://objectstorage.<region>.oraclecloud.com`. For another OCI realm or a
dedicated region, configure that profile's trusted HTTPS endpoint explicitly:

```yaml
oci:
  profiles:
    GOVERNMENT:
      endpoint: https://objectstorage.example-realm-domain
```

The same sidecar is available in task sessions and `hatchery sandbox shell`.

## Path policies

`rules` is an optional additive allowlist. Omitting it preserves the host
profile's Object Storage authority; `rules: []` denies everything. Paths have
the form `oci://namespace/bucket[/object-prefix]`.

Permissions are:

- `READ` — get or inspect objects.
- `LIST` — inspect a bucket or list objects within an allowed prefix.
- `WRITE` — upload objects and multipart parts.
- `DELETE` — delete objects.

Object names are literal prefixes, so trailing slashes are significant.
Overlapping rules combine permissions. OCI IAM remains the final authority, so
least-privilege host profiles are still recommended.

When rules are configured, unsupported or ambiguous operations fail closed.
Namespace and bucket administration, copy/rename actions, PAR management, bulk
operations, and unknown actions are denied. Multipart creation without an
object name is also denied because it cannot be safely matched to a prefix.

## Client compatibility

The proxy supports content-length request bodies and reuses the
`x-content-sha256` value produced by OCI clients when re-signing uploads.
Chunked request bodies are rejected. The host API key may be unencrypted or use
the `pass_phrase` setting in the OCI config.
