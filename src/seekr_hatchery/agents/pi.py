"""pi coding agent backend.

pi (``@earendil-works/pi-coding-agent``) talks to any number of model
providers at once. Rather than hard-code each, hatchery discovers the user's
configured providers from pi's own fetched catalog
(``~/.pi/agent/models-store.json``) and delegates *credential resolution* to
pi's ``auth`` CLI — so env vars, config-value grammar (``$ENV`` / ``!cmd``),
``models.json`` keys, ``auth.json`` entries, and OAuth refresh are all handled
by pi, not reimplemented here.

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

pi has no ``*_BASE_URL`` env var — the only override mechanism is
``~/.pi/agent/models.json``'s ``providers.<id>.baseUrl``, written at
container-exec time (the proxy port is only known then). The real credentials
never touch the container: the container only ever sees the per-launch fake
proxy token; the host-side ``ProxyEndpoint.header_mutator`` swaps in the real
key/token on the way out.
"""

import base64
import binascii
import json
import logging
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from seekr_hatchery.agents.agent_backend import (
    CONTAINER_HOME,
    AgentBackend,
    ProxyEndpoint,
)
from seekr_hatchery.locks import hatchery_lock
from seekr_hatchery.mount import BindMount, Mount, VolumeMount
from seekr_hatchery.utils import npm

logger = logging.getLogger(__name__)

# npm package for the pi CLI. The install line is emitted unpinned; the concrete
# version is resolved to the registry's latest when the Dockerfile is generated
# (and re-pinned by ``hatchery harness update``).
_NPM_PACKAGE = "@earendil-works/pi-coding-agent"

_OPENAI_CODEX = "openai-codex"

# JWT claim path pi-ai's extractAccountId() (openai-codex-responses.js) reads
# the ChatGPT account id from: payload["https://api.openai.com/auth"].chatgpt_account_id
_CHATGPT_JWT_CLAIM_PATH = "https://api.openai.com/auth"

# Shell snippet run at container-exec time (see resources/pi_wrapper.sh for the
# script + rationale). Scans the environment for every HATCHERY_PI_<UP>_ID
# marker and synthesises ~/.pi/agent/{models,auth}.json from the per-launch
# proxy token + port; the real credentials never enter the container.
_DOCKER_WRAPPER: str = (Path(__file__).parent.parent / "resources" / "pi_wrapper.sh").read_text()


def _store_providers() -> dict[str, tuple[str, str]]:
    """Map provider id → ``(api, baseUrl)`` from ``~/.pi/agent/models-store.json``.

    pi's fetched catalog is ``{provider: {"models": [{"api","baseUrl",...}], ...}}``.
    All of a provider's models share one api + baseUrl, so we read the first.
    Returns an empty dict if the store is missing or unparseable.
    """
    store = Path.home() / ".pi" / "agent" / "models-store.json"
    try:
        data = json.loads(store.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, tuple[str, str]] = {}
    for pid, entry in data.items():
        models = entry.get("models") if isinstance(entry, dict) else None
        if not models:
            continue
        api, base_url = models[0].get("api"), models[0].get("baseUrl")
        if api and base_url:
            out[pid] = (api, base_url)
    return out


def _endpoint_for(provider_id: str) -> tuple[str, str] | None:
    """Return ``(target_host, base_path)`` for *provider_id*, or None if unknown.

    Every provider — openai-codex included — is looked up in the store and split
    from its ``baseUrl`` (e.g. ``https://api.openai.com/v1`` →
    ``("api.openai.com", "/v1")``).
    """
    prov = _store_providers().get(provider_id)
    if not prov:
        return None
    split = urlsplit(prov[1])
    if not split.netloc:
        return None
    return split.netloc, split.path.rstrip("/")


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
        """Per-task volume for ~/.pi/agent so sessions persist across resume.

        No seed: auth.json / models.json are synthesised by
        ``_DOCKER_WRAPPER`` at exec time from the per-launch proxy token and
        port, which aren't known until then. The host's real
        ``~/.pi/agent/auth.json`` (containing real keys/OAuth tokens) is
        never mounted or read into the container.

        Host paths layered on top of the volume, all ``.exists()``-guarded (a
        bind whose source is missing would make Docker create a directory
        there):

        - ``settings.json`` RW — user config, flows both ways.
        - ``models-store.json`` RO — pi's model catalogue; RO keeps upstream
          target resolution host-authoritative.
        - ``npm/node_modules`` RO — pi's installed packages/plugins (subagents
          etc.). ``PI_OFFLINE=1`` means the container can't install them
          itself, so it takes the host's; RO stops per-task installs from
          polluting the host tree. Pure-JS + same arch, so portable (unlike
          ``bin/``, which holds native binaries).

        ``bin/`` and ``sessions/`` stay container-local in the volume.
        """
        agent_dir = Path.home() / ".pi" / "agent"
        agent_dst = f"{CONTAINER_HOME}/.pi/agent"
        mounts: list[Mount] = [VolumeMount(name="pi-dir", dst=agent_dst)]
        settings = agent_dir / "settings.json"
        if settings.exists():
            mounts.append(BindMount(src=settings, dst=f"{agent_dst}/settings.json", mode="RW"))
        store = agent_dir / "models-store.json"
        if store.exists():
            mounts.append(BindMount(src=store, dst=f"{agent_dst}/models-store.json", mode="RO"))
        node_modules = agent_dir / "npm" / "node_modules"
        if node_modules.exists():
            mounts.append(BindMount(src=node_modules, dst=f"{agent_dst}/npm/node_modules", mode="RO"))
        return mounts

    @staticmethod
    def proxy_endpoints() -> list[ProxyEndpoint]:
        """One ``ProxyEndpoint`` per configured provider.

        Candidates are exactly the providers in ``~/.pi/agent/models-store.json``
        — a ChatGPT login makes pi's catalog refresh add openai-codex there like
        any other provider, so it needs no special-casing in discovery. Each is
        probed with ``pi auth check``; only those that resolve to a ready
        credential become endpoints. Every provider uses the same
        provider-agnostic injection mutator: pi shapes the wire headers itself,
        we only swap in the real secret. openai-codex additionally gets a
        ``container_token`` carrying the real ChatGPT account id, since pi reads
        that id from its local token client-side.
        """
        store = _store_providers()

        endpoints: list[ProxyEndpoint] = []
        for provider_id in store:
            endpoint = _endpoint_for(provider_id)
            if endpoint is None:
                continue
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
                    target_host=endpoint[0],
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
        resolved (``container_oauth``), not a hard-coded provider list.
        """
        prefix = "HATCHERY_PI_" + endpoint.key.upper().replace("-", "_")
        resolved = _endpoint_for(endpoint.key)
        base_path = resolved[1] if resolved else ""
        return {
            f"{prefix}_ID": endpoint.key,
            f"{prefix}_BASEURL": f"http://host.docker.internal:{proxy_port}{base_path}",
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
    def on_before_container_start(
        session_dir: Path,
        proxy_token: str,
        workdir: str,
    ) -> None:
        pass  # ~/.pi/agent/{auth,models}.json are written by _DOCKER_WRAPPER at exec time

    dockerfile_install: str = f"""\
# ── pi coding agent ─────────────────────────────────────────────────────────
USER root
# ripgrep (rg) + fd (fdfind) are pi's search tools; installing them on PATH
# stops pi from downloading its own copies into ~/.pi/agent/bin at first run
# (that dir is a fresh per-task volume, so it would re-download every task).
# pi accepts either "fd" or "fdfind" on PATH (tools-manager.ts systemBinaryNames).
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \\
    && apt-get install -y --no-install-recommends nodejs ripgrep fd-find \\
    && rm -rf /var/lib/apt/lists/*
USER hatchery
RUN npm config set prefix '{CONTAINER_HOME}/.npm-global' \\
    && npm install -g --ignore-scripts {_NPM_PACKAGE}"""

    def update(self, dockerfile_text: str) -> tuple[str, str | None, str] | None:
        old_version = npm.current_npm_version(dockerfile_text, _NPM_PACKAGE)
        version = npm.npm_latest_version(_NPM_PACKAGE)
        return npm.pin_npm_install(dockerfile_text, _NPM_PACKAGE, version), old_version, version
