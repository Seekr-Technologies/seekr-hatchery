# API credential proxy

The real API key never enters the container. Hatchery starts a lightweight
host-side HTTP reverse proxy on an ephemeral port immediately before launching
the container.

For Codex with OpenAI, the container receives:

- `OPENAI_API_KEY` — a random per-task proxy token.
- `OPENAI_BASE_URL` — the host proxy URL
  (`http://host.docker.internal:<port>`).

The SDK inside the container uses these transparently. The proxy validates the
inbound token, strips credentials sent by the container, injects the real API
key in the appropriate upstream format, and forwards the request over HTTPS.
A jailbroken or adversarially prompted agent can read only the proxy token,
which is worthless outside the running proxy.

The proxy token is stable per task so cached credentials remain valid across
`resume` launches.

## Custom Codex providers

If `~/.codex/config.toml` configures a custom provider with
`experimental_bearer_token`, Hatchery routes the host proxy to that provider
instead of OpenAI. Detection is automatic. The container receives only the
per-task proxy token in its scrubbed configuration.

TLS verification uses the operating system trust store through
[`truststore`](https://truststore.readthedocs.io/). Install private corporate
CAs in the OS trust store; no Hatchery-specific CA configuration is required.

Hatchery does not refresh a custom provider's static bearer token. Update the
host configuration through the provider's normal workflow when it rotates.
