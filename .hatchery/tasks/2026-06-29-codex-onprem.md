# Task: codex-onprem

**Status**: complete
**Branch**: hatchery/codex-onprem
**Created**: 2026-06-29 11:50

## Objective

Codex can be configured to talk to a non-OpenAI provider via a
`[model_providers.<name>]` section in `~/.codex/config.toml` that carries
`experimental_bearer_token`. Common cases include routing Codex
against an internal HTTPS adapter, a local LLM server, or any
non-OpenAI inference endpoint. Before this change, hatchery's
`CodexBackend` only knew about OpenAI / ChatGPT — a user with a
custom provider could run `codex` natively but not inside a hatchery
sandbox.

## Context

Hatchery's existing security model: the real API token never enters the
container. A host-side HTTP proxy starts on an ephemeral port, the
container is given a per-task random proxy token + base URL pointing at
that proxy, and the proxy injects the real token before forwarding over
HTTPS to the upstream API.

For custom-provider mode we needed to:

- detect the configuration purely from the user's existing host setup
  (no new opt-in flag)
- keep the real bearer token on the host
- let the proxy validate TLS against the provider's non-public CA
  without a hatchery-specific config knob
- override the provider's `base_url` and `experimental_bearer_token`
  inside the container at runtime, since the proxy port is only known at
  launch time and codex ignores `OPENAI_BASE_URL` for non-`openai` providers

No internal hostnames, CA paths, or setup-script names appear anywhere
in code, comments, or error messages.

## Summary

### Key decisions

- **Detection**: custom-provider mode is detected by parsing `~/.codex/config.toml`
  with `tomllib`. If the active `model_provider` resolves to a section
  with both `base_url` and `experimental_bearer_token`, hatchery treats
  it as a custom provider. Custom-provider precedence wins over OAuth / `OPENAI_API_KEY` —
  the user explicitly configured the provider, so it is the deliberate
  signal. (See `CodexBackend._read_custom_provider` in
  `src/seekr_hatchery/agents/codex.py`.)
- **Provider name validation**: provider names are restricted to
  `^[A-Za-z0-9_-]+$` so they can be interpolated into the Docker shell
  wrapper without escaping. A name with shell metacharacters causes the
  helper to return `None`, falling through to OpenAI mode rather than
  raising — this matches the "auto-detect" UX (a misconfigured
  config.toml just doesn't activate custom-provider mode).
- **CA trust via OS native trust store**: the proxy's outbound
  `urllib3.PoolManager` is built with a `truststore.SSLContext` so TLS
  verification delegates to the OS native trust store (macOS Keychain,
  Linux `/etc/ssl/certs`, Windows cert store) instead of certifi's
  bundled Mozilla list. Any CA the user has already installed
  system-wide — public or corporate — is trusted automatically. No
  hatchery-specific CA config exists. The `truststore.SSLContext` is
  scoped to the proxy's pool only (the process-wide `ssl` module is
  untouched).
- **Container-side config**: the host `~/.codex/config.toml` is **not**
  bind-mounted in custom-provider mode (it contains the real bearer). Instead,
  `on_before_container_start` writes a sanitized `config.toml` to
  `session_dir/codex_config.toml` on every launch, built from scratch
  (not regex-rewritten) to avoid TOML-formatting edge cases. The
  sanitized file uses a placeholder `base_url` and the proxy token as
  its bearer; the `_DOCKER_WRAPPER` overrides both with the real proxy
  URL/token via `codex --config` at exec time. The file is bind-mounted
  **RW** (per-task scratch) so codex can persist its own runtime state
  (model selection, TUI prefs) without crashing. Project trust for the
  agent's working directory is pre-populated as
  `[projects."<workdir>"] trust_level = "trusted"`, and any existing
  host-side trust entries are copied across verbatim — so codex never
  shows the "Do you trust this directory?" prompt on startup.
- **Model catalog**: `~/.codex/model-catalog.json` is bind-mounted RO at
  the in-container path so the model picker still shows the custom
  models. The sanitized config points `model_catalog_json` at the
  container path only when the host file exists.
- **No auto-refresh**: stale tokens surface as upstream 401 without
  hatchery attempting to refresh out-of-band. This avoids tying hatchery
  to any specific token-rotation workflow.

### Files changed

- `src/seekr_hatchery/proxy.py` — `api_server()` builds its
  `urllib3.PoolManager` with `ssl_context=truststore.SSLContext(...)` so
  outbound TLS uses the OS native trust store.  Adds `truststore` as a
  runtime dependency.
- `src/seekr_hatchery/agents/codex.py` — added `_read_custom_provider`,
  `_custom_provider_section`, `_custom_provider_top_level` helpers; custom-provider
  branches in `proxy_kwargs`, `make_header_mutator`, `container_env`,
  `construct_mounts`, `on_before_container_start`; extended
  `_DOCKER_WRAPPER` with a conditional on `HATCHERY_CODEX_PROVIDER`.
- `src/seekr_hatchery/docker.py` — wrapped the `backend.proxy_kwargs()`
  call inside `_maybe_api_server` so `RuntimeError` surfaces via
  `ui.error` + `sys.exit(1)`.
- `tests/test_agent_codex.py` — new `_make_custom_provider_config` helper and
  test classes covering detection, proxy_kwargs (incl. neutral-message
  assertion and missing-CA paths), header mutator, container env, the
  docker wrapper, construct_mounts, and the sanitized-config writer.
  Critical assertion: the synthesized config never contains the host's
  real bearer (literal substring search).
- `tests/test_proxy.py` — asserts the proxy's `PoolManager` is built
  with a `truststore.SSLContext`.
- `tests/test_docker.py` — error-path test for `_maybe_api_server`
  surfacing `proxy_kwargs` errors as `sys.exit(1)`.
- `README.md` — new "Custom Codex providers" subsection under
  "Docker sandbox", neutral language.

### Gotchas for future agents

- **codex needs RW on `~/.codex/config.toml`.** The first attempt
  mounted the sanitized config RO; codex's first-run trust prompt
  tried to persist the answer back, hit the RO mount, and crashed the
  TUI with `failed to persist config at ~/.codex/config.toml`. The fix
  is twofold: (1) bind the sanitized file RW (it's per-task scratch,
  regenerated each launch — no security regression), and (2)
  pre-populate `[projects."<workdir>"] trust_level = "trusted"` so the
  prompt doesn't appear in the first place. If you change the mount
  back to RO, codex's TUI will break on first launch.



- **The proxy port is unknown at `on_before_container_start` time.** The
  sanitized config writes a placeholder `base_url` and bearer; the
  `_DOCKER_WRAPPER` overrides both with `$OPENAI_BASE_URL` /
  `$OPENAI_API_KEY` at container exec. Do not try to bake the proxy
  port into the synthesized config.
- **`_seed_codex_dir` is harmless overlap in custom-provider mode** — it still
  writes a fake `auth.json` with `auth_mode=apikey`, but the active
  provider uses its own `experimental_bearer_token`, so `auth.json` is
  consulted only as a fallback for the built-in `openai` provider. Left
  in place to keep the seed logic uniform.
- **Synthesizing from scratch beats regex-rewriting** the host config.
  The previous draft of this plan considered regex sanitization; an
  early review correctly flagged that TOML's quoting rules make this
  brittle. The synthesizer emits only the keys the in-container codex
  needs (`model`, `model_provider`, `model_reasoning_effort`,
  `model_catalog_json` when applicable, and the single
  `[model_providers.<name>]` table). TUI preferences and project trust
  on the host are not preserved — accept this if asked to fix something
  in that vein.
- **SIGILL in `tests/test_kubectl_proxy.py` is pre-existing** on the
  aarch64 sandbox runtime (cryptography wheel issue), not caused by
  these changes. Use `--ignore=tests/test_kubectl_proxy.py` if running
  the full suite in this environment.

### Verification

Automated: `uv run pytest tests/` (764 passed, 19 skipped, ignoring
`test_kubectl_proxy.py` per the SIGILL note above). `uv run ruff format`
and `uv run ruff check` clean.

Manual (requires an actual custom-provider setup and live cluster
access — not exercised in this task):

1. With a custom-provider `~/.codex/config.toml` and the upstream's CA installed
   in your OS trust store, `hatchery new probe` should reach the
   provider.
2. Inside the running container, `cat ~/.codex/config.toml` shows the
   proxy token (not the real bearer) and a placeholder base_url.
3. Model picker lists the configured custom models.
4. A stale host bearer surfaces a clean 401 from the upstream — no
   auto-refresh, no internal-script references.
