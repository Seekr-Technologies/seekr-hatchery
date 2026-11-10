"""pi coding agent backend.

pi (``@earendil-works/pi-coding-agent``) talks to any number of model
providers at once. Rather than hard-code each, hatchery discovers the user's
configured providers from pi's fetched catalog
(``~/.pi/agent/models-store.json``) plus custom ``models.json`` entries, and
delegates *credential resolution* to pi's ``auth`` CLI — so env vars,
config-value grammar (``$ENV`` / ``!cmd``), ``models.json`` keys, ``auth.json``
entries, and OAuth refresh are all handled by pi, not reimplemented here.

What stays in this module is bounded and does not grow per provider:

- **Endpoint** (upstream host + base path) — read from the store's ``baseUrl``.
- **Credential injection** — one provider-agnostic mutator. pi already emits
  the correct wire format for whatever provider it targets (``x-api-key`` vs
  ``Authorization: Bearer``, plus any provider-specific extras like OAuth beta
  flags or ``chatgpt-account-id`` + ``originator`` for openai-codex), driven by
  the fake ``auth.json`` we write. We only substitute the real secret for the
  fake proxy token in whichever auth header pi used — keyed on header name, not
  provider.

``openai-codex`` (the ChatGPT-subscription provider) is discovered from the
store like any other — a ChatGPT login makes pi's catalog refresh write it there.
The one thing special about it is irreducible: it is the sole provider that
derives request state (the ChatGPT account id) from its *local* token
client-side, so its endpoint supplies a ``container_token`` — a fake JWT carrying
the real account id — instead of the shared proxy token.

pi has no ``*_BASE_URL`` env var. At container-exec time, Hatchery rewrites
each catalog model's ``baseUrl`` to the proxy origin while retaining its path,
and sets proxy provider URLs in ``models.json``. Mixed paths get explicit
chat-model definitions so static or stale catalogs cannot bypass the proxy.
``PI_OFFLINE=1`` prevents network refresh from replacing the routed catalog.
The real credentials never touch the container: the container only ever
sees the per-launch fake proxy token; the host-side
``ProxyEndpoint.header_mutator`` swaps in the real key/token on the way out.
"""

import base64
import binascii
import copy
import json
import logging
import re
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

import seekr_hatchery.agents.pi.migrations as migrations
from seekr_hatchery.agents.agent_backend import (
    CONTAINER_HOME,
    AgentBackend,
    ProxyEndpoint,
)
from seekr_hatchery.locks import hatchery_lock
from seekr_hatchery.mount import BindMount, Mount, SeedContext, VolumeMount
from seekr_hatchery.utils import npm

if TYPE_CHECKING:
    from seekr_hatchery.docker import ContainerRuntime
    from seekr_hatchery.models import SessionMeta

logger = logging.getLogger(__name__)

# npm package for the pi CLI. The install line is emitted unpinned; the concrete
# version is resolved to the registry's latest when the Dockerfile is generated
# (and re-pinned by ``hatchery harness update``).
_NPM_PACKAGE = "@earendil-works/pi-coding-agent"

_OPENAI_CODEX = "openai-codex"
_HOST_MODELS_FILENAME = "hatchery-host-models.json"
_HOST_MODELS_STORE_FILENAME = "hatchery-host-models-store.json"


def _seed_empty_state(_ctx: SeedContext) -> dict[str, bytes]:
    """Initialize writable volumes for the non-root hatchery user."""
    return {}


# JWT claim path pi-ai's extractAccountId() (openai-codex-responses.js) reads
# the ChatGPT account id from: payload["https://api.openai.com/auth"].chatgpt_account_id
_CHATGPT_JWT_CLAIM_PATH = "https://api.openai.com/auth"

# Shell snippet run at container-exec time (see wrapper.sh for the
# script + rationale). Scans the environment for every HATCHERY_PI_<UP>_ID
# marker and synthesises ~/.pi/agent/{models,auth}.json from the per-launch
# proxy token + port; the real credentials never enter the container.
_DOCKER_WRAPPER: str = Path(__file__).with_name("wrapper.sh").read_text()


def _read_models_config() -> dict:
    """Read Pi's host ``models.json``, accepting its JSONC-style syntax."""
    path = Path.home() / ".pi" / "agent" / "models.json"
    try:
        text = path.read_text()
    except OSError:
        return {}

    # Match Pi's parser: remove // comments and trailing commas while leaving
    # quoted strings untouched. Pi does not accept block comments.
    text = re.sub(
        r'"(?:\\.|[^"\\])*"|//[^\n]*',
        lambda match: match.group(0) if match.group(0).startswith('"') else "",
        text,
    )
    text = re.sub(
        r'"(?:\\.|[^"\\])*"|,(\s*[}\]])',
        lambda match: match.group(0) if match.group(0).startswith('"') else match.group(1),
        text,
    )
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _provider_target_hosts() -> dict[str, str]:
    """Map providers to their sole effective upstream host.

    The wrapper maps every effective model URL for a provider to one proxy.
    Providers whose model URLs span origins cannot be represented by that
    proxy, so reject them before launching the container rather than silently
    sending requests to the wrong upstream.
    """
    store_path = Path.home() / ".pi" / "agent" / "models-store.json"
    try:
        store = json.loads(store_path.read_text())
    except (json.JSONDecodeError, OSError):
        store = {}
    if not isinstance(store, dict):
        store = {}

    configured_providers = _read_models_config().get("providers", {})
    if not isinstance(configured_providers, dict):
        configured_providers = {}

    provider_ids = dict.fromkeys((*store, *configured_providers))
    target_hosts: dict[str, str] = {}
    for provider_id in provider_ids:
        if not isinstance(provider_id, str):
            continue
        catalog_provider = store.get(provider_id)
        catalog_models = (
            [model for model in catalog_provider.get("models", []) if isinstance(model, dict)]
            if isinstance(catalog_provider, dict) and isinstance(catalog_provider.get("models"), list)
            else []
        )
        provider_config = configured_providers.get(provider_id)
        if not isinstance(provider_config, dict):
            provider_config = {}
        configured_models = (
            [model for model in provider_config.get("models", []) if isinstance(model, dict)]
            if isinstance(provider_config.get("models"), list)
            else []
        )

        inherited_api = next(
            (model["api"] for model in catalog_models if isinstance(model.get("api"), str) and model["api"]),
            "",
        )
        first_configured_model = next(iter(configured_models), {})
        api = provider_config.get("api", first_configured_model.get("api", inherited_api))
        if not isinstance(api, str) or not api:
            continue

        provider_base_url = provider_config.get("baseUrl")
        if not isinstance(provider_base_url, str) or not provider_base_url:
            provider_base_url = ""
        configured_by_id = {
            model["id"]: model for model in configured_models if isinstance(model.get("id"), str) and model["id"]
        }

        effective_urls: list[str] = []
        matched_model_ids: set[str] = set()
        for catalog_model in catalog_models:
            catalog_base_url = catalog_model.get("baseUrl")
            model_id = catalog_model.get("id")
            configured_model = configured_by_id.get(model_id) if isinstance(model_id, str) else None
            if configured_model is not None:
                matched_model_ids.add(model_id)
                model_base_url = configured_model.get("baseUrl")
            else:
                model_base_url = ""
            base_url = model_base_url or provider_base_url or catalog_base_url
            if isinstance(base_url, str) and base_url:
                effective_urls.append(base_url)

        for configured_model in configured_models:
            model_id = configured_model.get("id")
            if isinstance(model_id, str) and model_id in matched_model_ids:
                continue
            base_url = configured_model.get("baseUrl") or provider_base_url
            if isinstance(base_url, str) and base_url:
                effective_urls.append(base_url)

        origins = {
            (
                split.scheme.lower(),
                split.hostname,
                split.port if split.port is not None else {"https": 443, "http": 80}.get(split.scheme.lower()),
            )
            for base_url in effective_urls
            if (split := urlsplit(base_url)).scheme and split.hostname
        }
        if not origins:
            continue
        if len(origins) > 1:
            raise RuntimeError(
                f"pi provider '{provider_id}' uses multiple upstream origins; configure its model and provider base URLs "
                "to use one origin before starting a container"
            )
        scheme, hostname, port = next(iter(origins))
        host = f"[{hostname}]" if ":" in hostname else hostname
        default_port = {"https": 443, "http": 80}.get(scheme)
        target_hosts[provider_id] = f"{host}:{port}" if port is not None and port != default_port else host
    return target_hosts


def _run_pi_auth_check(provider_id: str) -> dict | None:
    """Resolve *provider_id*'s credential via pi's auth CLI, host-side.

    Runs ``pi auth check --provider <id> --json --credentials``, which performs
    pi's full offline credential resolution (env vars, config-value grammar,
    models.json keys, auth.json, OAuth refresh) and prints
    ``{"status","provider","authType","credentials"}``. Returns the parsed dict
    iff ``status == "ready"``, else None (unconfigured / error / pi missing).
    """
    try:
        proc = subprocess.run(
            ["pi", "auth", "check", "--provider", provider_id, "--json", "--credentials"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        logger.debug("pi not found on PATH while resolving %s credential", provider_id)
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if data.get("status") != "ready":
        return None
    return data


def _chatgpt_account_id(access_token: str) -> str | None:
    """Decode the JWT payload segment and extract the ChatGPT account id.

    Mirrors pi-ai's ``extractAccountId()`` (openai-codex-responses.js):
    split on ``.``, base64url-decode the middle segment, and read
    ``payload["https://api.openai.com/auth"].chatgpt_account_id``. Returns
    None (never raises) if the token isn't a well-formed JWT with that claim
    — the caller omits the header rather than crash.
    """
    try:
        parts = access_token.split(".")
        if len(parts) != 3:
            return None
        padding = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
        account_id = payload.get(_CHATGPT_JWT_CLAIM_PATH, {}).get("chatgpt_account_id")
        return account_id or None
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, binascii.Error):
        return None


def _mint_codex_sentinel(account_id: str) -> str:
    """Mint the fake JWT the container presents for openai-codex.

    pi's openai-codex client decodes its *local* token as a JWT and reads the
    ChatGPT account id out of it before sending any request. Embedding the real
    id here lets pi emit the correct ``chatgpt-account-id`` header itself, so
    the proxy only has to swap the bearer token — no openai-codex-specific
    header code on our side. This is not a credential: the account id is not
    secret (pi computes it from its own token in a normal run) and the real
    OAuth token never enters the container. The uuid sig segment carries the
    entropy the proxy's exact-match token check validates against.
    """
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).decode().rstrip("=")
    claim = {_CHATGPT_JWT_CLAIM_PATH: {"chatgpt_account_id": account_id}}
    payload = base64.urlsafe_b64encode(json.dumps(claim).encode()).decode().rstrip("=")
    return f"{header}.{payload}.{uuid.uuid4().hex}"


def _refresh(provider_id: str, state: dict) -> None:
    """Acquire a cross-process lock, then refresh *provider_id*'s OAuth token.

    ``pi auth check`` resolves and (by default) refreshes expired OAuth
    credentials, persisting them, and returns the fresh token in one call — so
    we just re-run it and adopt the new credential. The lock serialises
    concurrent refreshes across processes.
    """
    with hatchery_lock(f"refresh.pi.{provider_id}"):
        check = _run_pi_auth_check(provider_id)
        if check and check.get("credentials"):
            state["token"] = check["credentials"]


def _build_injection_mutator(
    provider_id: str, token: str, kind: Literal["API_KEY", "OAUTH"]
) -> Callable[..., dict[str, str]]:
    """One provider-agnostic mutator: swap the fake token for the real one.

    pi already emits the correct wire format for whatever provider it is
    talking to — ``x-api-key`` vs ``Authorization: Bearer``, plus any
    provider-specific extras (OAuth beta flags, ``chatgpt-account-id`` +
    ``originator`` for openai-codex) — because the fake ``auth.json`` we write
    tells it the credential kind. All that is
    left to us is substituting the real secret for the fake proxy token in
    whichever auth header pi used. We key on the header *name*, not the token
    value, and leave every other header pi set untouched.
    """
    state: dict = {"token": token}

    def _mutate(headers: dict[str, str], *, refresh: bool = False) -> dict[str, str]:
        if refresh and kind == "OAUTH":
            _refresh(provider_id, state)
        secret = state["token"]
        out: dict[str, str] = {}
        for k, v in headers.items():
            lk = k.lower()
            if lk == "x-api-key":
                out[k] = secret
            elif lk == "authorization" and v.startswith("Bearer "):
                out[k] = f"Bearer {secret}"
            else:
                out[k] = v
        return out

    return _mutate


class PiBackend(AgentBackend):
    kind = "PI"
    binary = "pi"
    supports_sessions = True
    # pi accepts --session-id <id>, creating it if missing — hatchery
    # pre-mints the UUID.
    session_id_pre_generated = True

    # ── Command construction ───────────────────────────────────────────────────

    # ``--session-id`` is pi's exact-id flag (verified via `pi --help`, v0.84.2):
    # "Use exact project session ID, creating it if missing" — safe for both a
    # brand-new id (new) and a previously-used one (resume), so the same flag
    # covers both flows deterministically, unlike --continue/--resume (no id)
    # or --session (accepts partial-UUID fuzzy matching, not exact-id-only).
    @staticmethod
    def build_new_command(
        session_id: str,
        system_prompt: str,
        initial_prompt: str,
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        args = ["--session-id", session_id, "--append-system-prompt", system_prompt, initial_prompt]
        if docker:
            return ["sh", "-c", PiBackend._DOCKER_WRAPPER, "sh", *args]
        return ["pi", *args]

    @staticmethod
    def build_resume_command(
        session_id: str,
        system_prompt: str,
        initial_prompt: str = "",
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        args = ["--session-id", session_id, "--append-system-prompt", system_prompt]
        if docker:
            return ["sh", "-c", PiBackend._DOCKER_WRAPPER, "sh", *args]
        return ["pi", *args]

    @staticmethod
    def build_finalize_command(
        session_id: str,
        system_prompt: str,
        wrap_up_prompt: str,
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        # --print / -p: "Non-interactive mode: process prompt and exit" (pi --help).
        args = ["--print", "--session-id", session_id, "--append-system-prompt", system_prompt, wrap_up_prompt]
        if docker:
            return ["sh", "-c", PiBackend._DOCKER_WRAPPER, "sh", *args]
        return ["pi", *args]

    _DOCKER_WRAPPER = _DOCKER_WRAPPER

    # ── Docker infrastructure ─────────────────────────────────────────────────

    @staticmethod
    def construct_mounts(session_dir: Path) -> list[Mount]:
        """Share global Pi state, shadowing credentials and platform-local state.

        Single-file bind mounts keep wrapper-generated auth/models/catalog off the host
        without inserting symlinks into the shared directory. They deliberately
        do not support atomic replacement. Backups and arbitrary user files are
        shared as-is; this is not a general secret-file filter.
        """
        agent_dir = Path.home() / ".pi" / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        agent_dst = f"{CONTAINER_HOME}/.pi/agent"
        mounts: list[Mount] = [BindMount(src=agent_dir, dst=agent_dst, follow_links="RECURSIVE")]

        config_dir = session_dir / "pi-config"
        config_dir.mkdir(parents=True, exist_ok=True)
        for name in ("auth.json", "models.json", "models-store.json"):
            source = config_dir / name
            # Preserve existing task-local state on reconstruction; the wrapper
            # refreshes both files with the current proxy configuration at exec.
            try:
                with source.open("x") as stream:
                    stream.write("{}")
            except FileExistsError:
                pass
            mounts.append(BindMount(src=source, dst=f"{agent_dst}/{name}"))
        mounts.extend(
            [
                VolumeMount(name="pi-bin", dst=f"{agent_dst}/bin", seed=_seed_empty_state),
                VolumeMount(name="pi-sessions", dst=f"{agent_dst}/sessions", seed=_seed_empty_state),
            ]
        )
        # Snapshot the host catalog as an isolated read-only wrapper input.
        # Always shadow this name, even when no host catalog exists.
        host_store = session_dir / _HOST_MODELS_STORE_FILENAME
        try:
            catalog = (agent_dir / "models-store.json").read_text()
        except OSError:
            catalog = "{}"
        host_store.write_text(catalog)
        mounts.append(BindMount(src=host_store, dst=f"{agent_dst}/{_HOST_MODELS_STORE_FILENAME}", mode="RO"))
        host_models = session_dir / _HOST_MODELS_FILENAME
        if host_models.exists():
            mounts.append(BindMount(src=host_models, dst=f"{agent_dst}/{_HOST_MODELS_FILENAME}", mode="RO"))
        return mounts

    @staticmethod
    def proxy_endpoints() -> list[ProxyEndpoint]:
        """One ``ProxyEndpoint`` per configured provider.

        Candidates come from ``~/.pi/agent/models-store.json`` plus custom or
        overridden providers in ``models.json``. A ChatGPT login makes pi's
        catalog refresh add openai-codex like any other provider, so it needs no
        special-casing in discovery. Each candidate is probed with
        ``pi auth check``; only those that resolve to a ready credential become
        endpoints. Every provider uses the same
        provider-agnostic injection mutator: pi shapes the wire headers itself,
        we only swap in the real secret. openai-codex additionally gets a
        ``container_token`` carrying the real ChatGPT account id, since pi reads
        that id from its local token client-side.
        """
        target_hosts = _provider_target_hosts()

        endpoints: list[ProxyEndpoint] = []
        for provider_id, target_host in target_hosts.items():
            check = _run_pi_auth_check(provider_id)
            if not check or not check.get("credentials"):
                continue
            kind: Literal["API_KEY", "OAUTH"] = "OAUTH" if check.get("authType") == "oauth" else "API_KEY"
            credentials = check["credentials"]
            container_token = None
            if provider_id == _OPENAI_CODEX:
                account_id = _chatgpt_account_id(credentials)
                if account_id:
                    container_token = _mint_codex_sentinel(account_id)
            endpoints.append(
                ProxyEndpoint(
                    key=provider_id,
                    header_mutator=_build_injection_mutator(provider_id, credentials, kind),
                    target_host=target_host,
                    container_token=container_token,
                    container_oauth=kind == "OAUTH",
                )
            )

        if not endpoints:
            raise RuntimeError(
                "no pi provider credentials resolved. Log in with `pi auth login` (or set the provider's "
                "API-key env var) on the host, and ensure `pi` is on PATH."
            )
        return endpoints

    @staticmethod
    def container_env(endpoint: ProxyEndpoint, proxy_token: str, proxy_port: int) -> dict[str, str]:
        """Provider-scoped env vars the ``_DOCKER_WRAPPER`` reads at exec time.

        Called once per ``ProxyEndpoint``; keys are namespaced by provider id
        so endpoints' env vars never collide once merged. ``_ID`` carries the
        real provider id (the env-key form is uppercased/underscored) and
        ``_SHAPE`` tells the wrapper whether to write an api_key- or
        oauth-shaped fake auth entry — derived from the credential the endpoint
        resolved (``container_oauth``), not a hard-coded provider list. The
        proxy origin deliberately carries no provider path: the wrapper applies
        it to each model's existing base path.
        """
        prefix = "HATCHERY_PI_" + endpoint.key.upper().replace("-", "_")
        return {
            f"{prefix}_ID": endpoint.key,
            f"{prefix}_PROXY_ORIGIN": f"http://host.docker.internal:{proxy_port}",
            f"{prefix}_KEY": proxy_token,
            f"{prefix}_SHAPE": "oauth" if endpoint.container_oauth else "api_key",
        }

    @staticmethod
    def on_new_task(session_dir: Path) -> None:
        pass  # no per-task config needed for pi

    @staticmethod
    def on_before_launch(worktree: Path) -> None:
        pass  # no worktree setup needed for pi

    @staticmethod
    def migrate_container_state(meta: "SessionMeta", *, runtime: "ContainerRuntime") -> None:
        migrations.upgrade_container_state(meta, runtime=runtime)

    @staticmethod
    def on_before_container_start(
        meta: "SessionMeta",
        proxy_token: str,
        workdir: str,
        *,
        runtime: "ContainerRuntime",
    ) -> None:
        """Materialize sanitized host model metadata for this launch.

        The wrapper needs a normal file beside the generated ``models.json`` so
        it can preserve custom models and provider options while replacing
        routing and authentication. A per-task copy avoids mounting the host's
        credential-bearing file into the sandbox.
        """
        session_dir = meta.session_dir
        destination = session_dir / _HOST_MODELS_FILENAME
        config = _read_models_config()
        providers = config.get("providers")
        if not isinstance(providers, dict):
            # Always shadow this path too: a similarly named host file must
            # never become the wrapper's unsanitized configuration input.
            session_dir.mkdir(parents=True, exist_ok=True)
            destination.write_text("{}")
            return

        sanitized = copy.deepcopy(config)
        sanitized_providers = sanitized["providers"]
        for entry in sanitized_providers.values():
            if not isinstance(entry, dict):
                continue
            entry.pop("apiKey", None)
            PiBackend._sanitize_auth_headers(entry, proxy_token)
            for key in ("models", "modelOverrides"):
                models = entry.get(key)
                values = models.values() if isinstance(models, dict) else models if isinstance(models, list) else []
                for model in values:
                    if isinstance(model, dict):
                        PiBackend._sanitize_auth_headers(model, proxy_token)

        session_dir.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(sanitized))

    @staticmethod
    def _sanitize_auth_headers(entry: dict, proxy_token: str) -> None:
        """Replace common credential headers while retaining other options."""
        headers = entry.get("headers")
        if not isinstance(headers, dict):
            return
        for name in list(headers):
            lower = name.lower()
            if lower == "x-api-key":
                headers[name] = proxy_token
            elif lower == "authorization":
                headers[name] = f"Bearer {proxy_token}"

    dockerfile_install: str = f"""\
# ── pi coding agent ─────────────────────────────────────────────────────────
USER root
# ripgrep (rg) + fd (fdfind) are pi's search tools; installing them on PATH
# stops pi from downloading its own copies into ~/.pi/agent/bin at first run
# (that dir is a fresh per-task volume, so it would re-download every task).
# pi accepts either "fd" or "fdfind" on PATH (tools-manager.ts systemBinaryNames).
# eMudhra mitigation: Debian trixie's nodejs is still Node 20; use patched Node 22.23.4.
# Return to apt-get install nodejs npm once trixie ships Node >=22.23.4.
COPY --from=node:22.23.4-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d /usr/local/ /usr/local/
RUN apt-get update && apt-get install -y --no-install-recommends ripgrep fd-find \\
    && rm -rf /var/lib/apt/lists/*
USER hatchery
RUN npm config set prefix '{CONTAINER_HOME}/.npm-global' \\
    && npm install -g --ignore-scripts {_NPM_PACKAGE}"""

    def update(self, dockerfile_text: str) -> tuple[str, str | None, str] | None:
        old_version = npm.current_npm_version(dockerfile_text, _NPM_PACKAGE)
        version = npm.npm_latest_version(_NPM_PACKAGE)
        return npm.pin_npm_install(dockerfile_text, _NPM_PACKAGE, version), old_version, version
