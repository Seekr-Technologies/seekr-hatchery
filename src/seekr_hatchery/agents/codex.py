"""OpenAI Codex CLI backend."""

import functools
import json
import logging
import os
import re
import subprocess
import threading
import time
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from seekr_hatchery.agents.agent_backend import CONTAINER_HOME, AgentBackend
from seekr_hatchery.locks import hatchery_lock
from seekr_hatchery.mount import BindMount, Mount, SeedContext, VolumeMount

if TYPE_CHECKING:
    from seekr_hatchery.docker import Runtime
    from seekr_hatchery.models import SessionMeta

logger = logging.getLogger("hatchery")

# Provider names appear in shell ``--config model_providers.<name>.*`` flags.
# Restrict to a safe character class so an attacker who controls the host
# ``~/.codex/config.toml`` cannot inject shell metacharacters or codex
# config-key fragments.
_PROVIDER_NAME_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_-]+$")

# Allowed characters in a provider's URL path.  The path is interpolated into
# ``OPENAI_BASE_URL`` and then into a shell-quoted ``--config`` flag in
# ``_DOCKER_WRAPPER``; restricting to RFC 3986 unreserved + ``/`` avoids
# quote / metacharacter trouble at the shell boundary.
_BASE_URL_PATH_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9._~/-]*$")

# Extracts the session UUID from a codex rollout filename
# ``rollout-<ISO-timestamp>-<uuid>.jsonl``. The trailing UUID is the id
# that ``codex resume <ID>`` accepts.
_ROLLOUT_UUID_RE: re.Pattern[str] = re.compile(
    r"rollout-[^/]*-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$"
)

# Shell snippet: for the newest rollout file, print one line ``<mtime> <path>``.
# Emits nothing if no rollout exists yet. The Python caller parses the
# mtime and filters out stale rollouts (e.g. from a previous task run
# that reused this volume).
_STAT_NEWEST_ROLLOUT_SH: str = (
    'f=$(ls -1t ~/.codex/sessions/*/*/*/rollout-*.jsonl 2>/dev/null | head -n1); [ -n "$f" ] && stat -c "%Y %n" "$f"'
)

# Grace window (seconds) when comparing rollout mtimes to launch_start.
# Docker/podman on macOS runs the engine inside a Linux VM whose clock can
# drift a second or two from the host; a 5-second window accommodates that
# without letting truly stale rollouts through.
_MTIME_GRACE_SECONDS: float = 5.0


@functools.lru_cache(maxsize=1)
def _host_config_data() -> dict:
    """Return the parsed contents of ``~/.codex/config.toml``.

    Cached for the lifetime of the Python process so the same launch
    reads the host config exactly once — avoids drift between
    ``proxy_kwargs`` / ``make_header_mutator`` / ``container_env`` /
    ``construct_mounts`` / ``on_before_container_start`` if the file is
    rewritten mid-launch (e.g. by a token-rotation script).

    Returns ``{}`` if the file is absent, unreadable, mis-encoded, or
    not valid TOML.

    Tests that mutate ``~/.codex/config.toml`` between assertions must
    call ``_host_config_data.cache_clear()`` — the autouse ``home``
    fixture in ``tests/conftest.py`` does so for every test.
    """
    cfg = Path.home() / ".codex" / "config.toml"
    if not cfg.exists():
        return {}
    try:
        return tomllib.loads(cfg.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.debug("Failed to parse %s: %s", cfg, exc)
        return {}


def _extract_uuid_from_path(path: str) -> str | None:
    """Return the session UUID embedded in a codex rollout filename, or None."""
    match = _ROLLOUT_UUID_RE.search(path)
    return match.group(1) if match else None


def _probe_session_id(
    meta: "SessionMeta",
    *,
    docker: bool,
    runtime: "Runtime | None",
    launch_start: float,
) -> str | None:
    """Return codex's session UUID for this launch, or ``None`` if not yet visible.

    Codex writes rollout files at
    ``~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl``; the
    trailing UUID is the id that ``codex resume <ID>`` accepts. Both
    probe paths pick the newest such file, verify its mtime falls
    within this launch, and extract the UUID from the filename.
    """
    if docker:
        assert runtime is not None
        return _probe_session_id_docker(meta, runtime, launch_start)
    return _probe_session_id_native(launch_start)


def _probe_session_id_docker(
    meta: "SessionMeta",
    runtime: "Runtime",
    launch_start: float,
) -> str | None:
    cmd = [runtime.binary, "exec", meta.container_name, "sh", "-c", _STAT_NEWEST_ROLLOUT_SH]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("codex probe (docker): exec failed: %s", exc)
        return None
    if result.returncode != 0:
        logger.debug("codex probe (docker): exec rc=%s stderr=%r", result.returncode, result.stderr[:200])
        return None

    line = result.stdout.strip()
    if not line:
        return None
    # ``stat -c "%Y %n"`` output: ``<mtime-epoch> <path>``.
    mtime_str, _, path = line.partition(" ")
    if not path:
        logger.warning("codex probe (docker): unparseable stat output: %r", line)
        return None
    try:
        mtime = float(mtime_str)
    except ValueError:
        logger.warning("codex probe (docker): non-numeric mtime %r for %s", mtime_str, path)
        return None
    if mtime < launch_start - _MTIME_GRACE_SECONDS:
        logger.debug(
            "codex probe (docker): skipping stale rollout %s (mtime=%.1f, launch_start=%.1f)",
            path,
            mtime,
            launch_start,
        )
        return None

    sid = _extract_uuid_from_path(path)
    if sid is None:
        logger.warning("codex probe (docker): no UUID in filename %r", path)
    else:
        logger.info("codex probe (docker): extracted session id %s from %s", sid, path)
    return sid


def _probe_session_id_native(launch_start: float) -> str | None:
    sessions_dir = Path.home() / ".codex" / "sessions"
    if not sessions_dir.exists():
        logger.debug("codex probe (native): sessions dir does not exist: %s", sessions_dir)
        return None
    fresh: list[tuple[float, Path]] = []
    for p in sessions_dir.glob("*/*/*/rollout-*.jsonl"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime >= launch_start - _MTIME_GRACE_SECONDS:
            fresh.append((mtime, p))
    if not fresh:
        logger.debug("codex probe (native): no fresh rollouts under %s (launch_start=%.1f)", sessions_dir, launch_start)
        return None
    fresh.sort(reverse=True)
    winner = fresh[0][1]
    sid = _extract_uuid_from_path(str(winner))
    if sid is None:
        logger.warning("codex probe (native): no UUID in filename %s", winner)
    else:
        logger.info("codex probe (native): extracted session id %s from %s", sid, winner)
    return sid


def _make_session_id_poller(
    meta: "SessionMeta",
    *,
    docker: bool,
    runtime: "Runtime | None",
    launch_start: float,
    stop: threading.Event,
) -> Callable[[], None]:
    """Return a closure that polls for codex's session UUID and persists it."""

    def _poll() -> None:
        from seekr_hatchery import sessions

        logger.info(
            "codex session-id poller started (docker=%s, runtime=%s, container=%s, current=%s)",
            docker,
            runtime.binary if runtime else None,
            meta.container_name if docker else "n/a",
            meta.session_id or "<unset>",
        )
        attempt = 0
        while not stop.is_set():
            if stop.wait(1.0):
                logger.info("codex session-id poller stopped after %d attempts without capture", attempt)
                return
            attempt += 1
            try:
                sid = _probe_session_id(meta, docker=docker, runtime=runtime, launch_start=launch_start)
            except Exception as exc:
                logger.debug("codex session-id probe #%d failed (will retry): %s", attempt, exc)
                continue
            if sid is None:
                continue
            if sid == meta.session_id:
                logger.info("codex session_id confirmed on attempt %d: %s", attempt, sid)
                return
            prior = meta.session_id
            meta.session_id = sid
            sessions.save(meta)
            if prior:
                logger.info("codex session_id updated on attempt %d: %s → %s", attempt, prior, sid)
            else:
                logger.info("codex session_id captured on attempt %d: %s", attempt, sid)
            return

    return _poll


class CodexBackend(AgentBackend):
    kind = "CODEX"
    binary = "codex"
    supports_sessions = True
    # Codex generates its own session UUID; the poller captures it live.
    session_id_pre_generated = False

    # ── Command construction ───────────────────────────────────────────────────

    # codex (as of v0.121) ignores the OPENAI_BASE_URL environment variable.
    # The only supported mechanism for a custom base URL is openai_base_url in
    # config.toml or --config openai_base_url='"..."' at the CLI.  When running
    # in Docker mode we inject the proxy URL via --config at launch, reading it
    # from the OPENAI_BASE_URL env var that container_env() has already set in
    # the container.  The sh wrapper expands the env var at container startup
    # so the proxy port (ephemeral, unknown at command-build time) is resolved
    # correctly.
    #
    # When ``HATCHERY_CODEX_PROVIDER`` is set (custom-provider mode), the wrapper
    # additionally overrides the provider's ``base_url`` and
    # ``experimental_bearer_token`` at runtime so codex routes that provider
    # through the hatchery proxy with the proxy token.  Provider names are
    # validated against ``_PROVIDER_NAME_RE`` before being placed in the env
    # var, so interpolating ``$HATCHERY_CODEX_PROVIDER`` here is shell-safe.
    # ``check_for_update_on_startup=false`` suppresses the interactive
    # "Update available" prompt that otherwise blocks resume launches
    # while codex waits for the user to press enter. The Codex image is
    # rebuilt by hatchery, not upgraded interactively by the agent.
    _DOCKER_WRAPPER: str = (
        'if [ -n "${HATCHERY_CODEX_PROVIDER:-}" ]; then '
        "exec codex "
        "--config check_for_update_on_startup=false "
        '--config "model_providers.${HATCHERY_CODEX_PROVIDER}.base_url=\\"$OPENAI_BASE_URL\\"" '
        '--config "model_providers.${HATCHERY_CODEX_PROVIDER}.experimental_bearer_token=\\"$OPENAI_API_KEY\\"" '
        '--dangerously-bypass-approvals-and-sandbox "$@"; '
        "fi; "
        "exec codex "
        "--config check_for_update_on_startup=false "
        '--config "openai_base_url=\\"$OPENAI_BASE_URL\\"" '
        '--dangerously-bypass-approvals-and-sandbox "$@"'
    )

    @staticmethod
    def build_new_command(
        session_id: str,
        system_prompt: str,
        initial_prompt: str,
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        # Combine system and initial prompts — codex has no separate system
        # prompt flag so we prepend the context directly.
        prompt = f"{system_prompt}\n\n{initial_prompt}".strip()
        if docker:
            return ["sh", "-c", CodexBackend._DOCKER_WRAPPER, "sh", prompt]
        return ["codex", "--dangerously-bypass-approvals-and-sandbox", prompt]

    @staticmethod
    def build_resume_command(
        session_id: str,
        system_prompt: str,
        initial_prompt: str = "",
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        """Resume codex's prior session so in-agent state is preserved.

        - With a known ``session_id``: ``codex resume <sid>``.
        - Without one, in docker: ``codex resume --last``. The per-task
          ``~/.codex/`` volume means "last" is unambiguously this task's
          own rollout.
        - Without one, in native: fall back to a fresh session with the
          combined context. ``cli.py`` bails before reaching us if
          ``session_id`` is missing in the native flow, so this is a
          defensive path.
        """
        if session_id:
            resume_args = ["resume", session_id]
        elif docker:
            resume_args = ["resume", "--last"]
        else:
            prompt = f"{system_prompt}\n\n{initial_prompt}".strip()
            return ["codex", "--dangerously-bypass-approvals-and-sandbox", prompt]

        if docker:
            return ["sh", "-c", CodexBackend._DOCKER_WRAPPER, "sh", *resume_args]
        return ["codex", "--dangerously-bypass-approvals-and-sandbox", *resume_args]

    @staticmethod
    def build_finalize_command(
        session_id: str,
        system_prompt: str,
        wrap_up_prompt: str,
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        """Wrap-up with the same session context as new/resume.

        - With a known ``session_id``: ``codex exec resume <sid> <wrap_up>``.
        - Without one, in docker: ``codex exec resume --last <wrap_up>``.
        - Without one, in native: fall back to a fresh non-interactive
          ``codex exec <wrap_up>``.
        """
        if session_id:
            exec_args = ["exec", "resume", session_id, wrap_up_prompt]
        elif docker:
            exec_args = ["exec", "resume", "--last", wrap_up_prompt]
        else:
            exec_args = ["exec", wrap_up_prompt]

        if docker:
            return ["sh", "-c", CodexBackend._DOCKER_WRAPPER, "sh", *exec_args]
        return ["codex", "--dangerously-bypass-approvals-and-sandbox", *exec_args]

    # ── Docker infrastructure ─────────────────────────────────────────────────

    @staticmethod
    def _read_custom_provider() -> tuple[str, str, str] | None:
        """Return ``(provider_name, base_url, bearer_token)`` if the host
        config.toml describes a custom provider, else ``None``.

        Detection rule: the active ``model_provider`` in
        ``~/.codex/config.toml`` resolves to a section that contains both
        ``base_url`` and ``experimental_bearer_token``.  This matches any
        non-OpenAI provider configured with a static bearer; it is the
        deliberate signal that the user wants codex routed somewhere
        other than the OpenAI default.

        Both the provider name and the URL path are validated against
        conservative character classes (``_PROVIDER_NAME_RE`` /
        ``_BASE_URL_PATH_RE``).  Anything outside those classes — quotes,
        spaces, shell metacharacters — is treated as "not configured"
        rather than risk shell injection at the ``_DOCKER_WRAPPER``
        boundary or breakage at the TOML / codex ``--config`` boundary.
        """
        data = _host_config_data()

        provider = data.get("model_provider")
        if not isinstance(provider, str) or not provider:
            return None
        if not _PROVIDER_NAME_RE.match(provider):
            logger.debug(
                "Ignoring custom codex provider %r: name must match %s",
                provider,
                _PROVIDER_NAME_RE.pattern,
            )
            return None

        providers = data.get("model_providers")
        if not isinstance(providers, dict):
            return None
        section = providers.get(provider)
        if not isinstance(section, dict):
            return None

        base_url = section.get("base_url")
        bearer = section.get("experimental_bearer_token")
        if not isinstance(base_url, str) or not base_url:
            return None
        if not isinstance(bearer, str) or not bearer:
            return None

        # Reject base_url path containing characters that would break the
        # shell-quoted ``--config`` flag in ``_DOCKER_WRAPPER``.
        url_path = urlsplit(base_url).path
        if not _BASE_URL_PATH_RE.match(url_path):
            logger.debug(
                "Ignoring custom codex provider %r: base_url path %r must match %s",
                provider,
                url_path,
                _BASE_URL_PATH_RE.pattern,
            )
            return None

        return provider, base_url, bearer

    @staticmethod
    def _custom_provider_section() -> dict | None:
        """Return the full ``[model_providers.<active>]`` table from the
        host config, or ``None`` if custom-provider detection fails.

        Used by ``on_before_container_start`` when synthesising the
        sanitized in-container config — we need ``wire_api`` (and any
        other passthrough fields) in addition to the three values that
        ``_read_custom_provider`` returns.
        """
        data = _host_config_data()
        provider = data.get("model_provider")
        if not isinstance(provider, str):
            return None
        providers = data.get("model_providers")
        if not isinstance(providers, dict):
            return None
        section = providers.get(provider)
        if not isinstance(section, dict):
            return None
        return section

    @staticmethod
    def _custom_provider_top_level() -> dict:
        """Return the top-level keys we want to mirror into the sanitized
        in-container config.toml (``model``, ``model_reasoning_effort``).

        Returns an empty dict when the host config.toml is missing or
        unparsable.
        """
        data = _host_config_data()
        out: dict[str, str] = {}
        for key in ("model", "model_reasoning_effort"):
            v = data.get(key)
            if isinstance(v, str):
                out[key] = v
        return out

    @staticmethod
    def _read_codex_creds() -> tuple[str | None, Literal["API_KEY", "OAUTH"] | None]:
        """Return (credential, source) from env or ~/.codex/auth.json. Single read."""
        auth_file = Path.home() / ".codex" / "auth.json"
        data: dict = {}
        if auth_file.exists():
            try:
                data = json.loads(auth_file.read_text())
            except (json.JSONDecodeError, OSError):
                logger.debug("Failed to parse ~/.codex/auth.json")

        auth_mode = data.get("auth_mode", "")

        # Explicit OAuth login ("oauth" or "chatgpt" auth_mode): use the OAuth
        # access_token and ignore any OPENAI_API_KEY env var or file field.
        # An env var set before the user switched to OAuth would otherwise shadow
        # the OAuth tokens and force API-key mode, causing the proxy to target
        # api.openai.com instead of chatgpt.com.
        if auth_mode in ("oauth", "chatgpt"):
            tokens = data.get("tokens") or {}
            access_token = tokens.get("access_token")
            if access_token:
                logger.debug("Using OAuth access_token from ~/.codex/auth.json (auth_mode=%s)", auth_mode)
                return access_token, "OAUTH"
            logger.debug("auth_mode is %s but no access_token found", auth_mode)
            return None, None

        # API-key mode (or no auth.json / unknown auth_mode): env var first, then file.
        key = os.environ.get("OPENAI_API_KEY")
        if key:
            logger.debug("Using OPENAI_API_KEY from environment")
            return key, "API_KEY"
        if data.get("OPENAI_API_KEY"):
            logger.debug("Using OPENAI_API_KEY from ~/.codex/auth.json")
            return data["OPENAI_API_KEY"], "API_KEY"

        # Fallback: OAuth tokens even if auth_mode wasn't explicitly set.
        tokens = data.get("tokens") or {}
        access_token = tokens.get("access_token")
        if access_token:
            logger.debug("Using OAuth access_token from ~/.codex/auth.json (no auth_mode)")
            return access_token, "OAUTH"

        logger.debug("No OpenAI API key found")
        return None, None

    @staticmethod
    def _detect_auth_source() -> Literal["API_KEY", "OAUTH"] | None:
        return CodexBackend._read_codex_creds()[1]

    @staticmethod
    def construct_mounts(session_dir: Path) -> list[Mount]:
        """Per-task volume for ~/.codex + bind mounts for cross-task state.

        The volume starts almost empty — the seed only synthesises
        ``auth.json`` so the in-container codex authenticates against the
        hatchery proxy and never sees the host's real credentials.
        Everything else codex creates as it runs lives in the volume:
        sessions, history, sqlite state, caches, logs, etc. — all on the
        runtime's native filesystem rather than virtio-fs, and per-task
        so concurrent sandboxes don't fight over the same files.

        Bind mounts overlay specific paths inside ``~/.codex/`` so they
        cross task boundaries (memories) or stay in sync with host edits
        (config.toml, models_cache.json). Layered mounts on top of a
        volume mount are handled by the kernel — writes at the bind
        paths go to the host, everything else goes to the volume.

        Custom-provider mode skips the host ``config.toml`` bind because
        it contains the real bearer token; ``on_before_container_start``
        writes a sanitized copy to ``session_dir`` that is bound RW so
        codex can persist its own runtime state (project trust, model
        choice, TUI preferences) without crashing on startup.  The
        sanitized file is per-task, regenerated from the host config on
        every launch, and only ever contains the proxy-token bearer —
        so RW does not weaken the "real bearer never enters the
        container" property.  Project trust for the working directory
        is pre-populated in the file by ``on_before_container_start``
        so codex doesn't prompt the user.  The host's
        ``model-catalog.json`` is bound RO so the in-container model
        picker shows the configured custom models.
        """
        mounts: list[Mount] = [
            VolumeMount(
                name="codex-dir",
                dst=f"{CONTAINER_HOME}/.codex",
                seed=CodexBackend._seed_codex_dir,
            ),
        ]
        host_codex = Path.home() / ".codex"
        custom = CodexBackend._read_custom_provider() is not None
        cross_task_names: tuple[str, ...]
        if custom:
            # Skip ``config.toml`` — would leak the real bearer token.
            cross_task_names = ("memories", "skills", "models_cache.json")
        else:
            cross_task_names = ("memories", "skills", "config.toml", "models_cache.json")
        for name in cross_task_names:
            p = host_codex / name
            if p.exists():
                mounts.append(BindMount(src=p, dst=f"{CONTAINER_HOME}/.codex/{name}", mode="RW"))

        if custom:
            catalog = host_codex / "model-catalog.json"
            if catalog.exists():
                mounts.append(BindMount(src=catalog, dst=f"{CONTAINER_HOME}/.codex/model-catalog.json", mode="RO"))
            # Sanitized config.toml written by ``on_before_container_start``;
            # RW so codex can persist project trust / model selection /
            # TUI prefs.  Per-task and rewritten each launch, so writes
            # are effectively scratch.
            mounts.append(
                BindMount(
                    src=session_dir / "codex_config.toml",
                    dst=f"{CONTAINER_HOME}/.codex/config.toml",
                    mode="RW",
                )
            )
        return mounts

    @staticmethod
    def _seed_codex_dir(ctx: SeedContext) -> Mapping[str, bytes]:
        """Initial contents of the per-task ~/.codex volume.

        Only ``auth.json`` is synthesised — codex populates everything
        else (sessions, logs, sqlite state, caches) inside the volume
        as it runs.

        Always uses ``auth_mode="apikey"`` regardless of the host's real
        mode. In apikey mode codex respects ``OPENAI_BASE_URL`` (which
        ``container_env`` sets to the proxy). For OAuth hosts,
        ``container_env`` and ``proxy_kwargs`` together route codex's
        apikey path through the OAuth backend; the container never sees
        the host's OAuth tokens.

        In custom-provider mode the active provider authenticates via its
        own ``experimental_bearer_token`` (set in the sanitized
        in-container ``config.toml``), so this ``auth.json`` is harmless
        overlap — codex only falls back to it for the built-in ``openai``
        provider.
        """
        fake_auth = {
            "auth_mode": "apikey",
            "OPENAI_API_KEY": ctx.proxy_token,
            "tokens": None,
        }
        return {"auth.json": json.dumps(fake_auth).encode()}

    @staticmethod
    def proxy_kwargs() -> dict:
        # Custom-provider mode wins over OAuth / API-key — the user
        # explicitly configured a different upstream in config.toml.
        #
        # The provider's URL path (e.g. ``/v1``) lives in the container's
        # ``OPENAI_BASE_URL`` — see ``container_env`` — not in
        # ``path_prefix``.  Putting it in both would forward to
        # ``<host>/v1/v1/responses`` and yield a 404 from the upstream.
        # This mirrors the OpenAI API-key path (target_host=api.openai.com,
        # container sees ``…/v1``).
        #
        # TLS verification uses the OS trust store via
        # ``truststore.SSLContext`` in ``proxy.api_server``, so any
        # non-public CA the user has installed system-wide is trusted
        # automatically — no hatchery-specific CA config needed.
        custom = CodexBackend._read_custom_provider()
        if custom is not None:
            _provider, base_url, _bearer = custom
            host = urlsplit(base_url).netloc
            if not host:
                raise RuntimeError(f"codex provider base_url {base_url!r} has no host component")
            return {"target_host": host}

        if CodexBackend._detect_auth_source() == "OAUTH":
            return {"target_host": "chatgpt.com", "path_prefix": "/backend-api/codex"}
        return {"target_host": "api.openai.com"}

    @staticmethod
    def make_header_mutator() -> Callable[..., dict[str, str]]:
        custom = CodexBackend._read_custom_provider()
        if custom is not None:
            _provider, _base_url, bearer = custom

            def _custom_provider_mutate(headers: dict[str, str], *, refresh: bool = False) -> dict[str, str]:
                # refresh is a no-op: the bearer comes from the host
                # config.toml and is rotated out-of-band by whatever
                # workflow populates that file.
                _ = refresh
                out = {k: v for k, v in headers.items() if k.lower() not in ("x-api-key", "authorization")}
                out["Authorization"] = f"Bearer {bearer}"
                return out

            return _custom_provider_mutate

        token, source = CodexBackend._read_codex_creds()
        if not token:
            raise RuntimeError(
                "no API token found. Set OPENAI_API_KEY or log in with `codex login` for OAuth authentication."
            )

        state: dict = {"token": token}

        def _refresh() -> None:
            """Acquire a cross-process lock, check if already refreshed, then refresh."""
            with hatchery_lock("refresh.codex"):
                # Another process may have already refreshed — check first.
                new_token, _ = CodexBackend._read_codex_creds()
                if new_token and new_token != state["token"]:
                    state["token"] = new_token
                    return

                auth_file = Path.home() / ".codex" / "auth.json"
                old_mtime = auth_file.stat().st_mtime if auth_file.exists() else 0
                old_token = state["token"]
                proc = subprocess.Popen(
                    ["codex", "exec", "hello"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    current_mtime = auth_file.stat().st_mtime if auth_file.exists() else 0
                    if current_mtime > old_mtime:
                        new_token, _ = CodexBackend._read_codex_creds()
                        if new_token:
                            state["token"] = new_token
                        break
                    if proc.poll() is not None:
                        new_token, _ = CodexBackend._read_codex_creds()
                        if new_token and new_token != old_token:
                            state["token"] = new_token
                        break
                    time.sleep(0.5)
                else:
                    proc.kill()
                    proc.wait()

        def _mutate(headers: dict[str, str], *, refresh: bool = False) -> dict[str, str]:
            if refresh and source == "OAUTH":
                _refresh()
            out = {k: v for k, v in headers.items() if k.lower() not in ("x-api-key", "authorization")}
            out["Authorization"] = f"Bearer {state['token']}"
            return out

        return _mutate

    @staticmethod
    def container_env(proxy_token: str, proxy_port: int) -> dict[str, str]:
        custom = CodexBackend._read_custom_provider()
        if custom is not None:
            provider, base_url, _bearer = custom
            path = urlsplit(base_url).path.rstrip("/")
            # ``HATCHERY_CODEX_PROVIDER`` activates the custom-provider branch
            # in ``_DOCKER_WRAPPER`` — codex is told to use
            # ``model_providers.<name>.base_url=$OPENAI_BASE_URL`` and
            # ``…experimental_bearer_token=$OPENAI_API_KEY`` so the request
            # leaves the container as ``Authorization: Bearer <proxy_token>``
            # to the host proxy, which substitutes the real bearer.
            return {
                "OPENAI_API_KEY": proxy_token,
                "OPENAI_BASE_URL": f"http://host.docker.internal:{proxy_port}{path}",
                "HATCHERY_CODEX_PROVIDER": provider,
            }
        if CodexBackend._detect_auth_source() == "OAUTH":
            # OAuth mode: proxy forwards to chatgpt.com/backend-api/codex/responses.
            # Codex appends /responses to OPENAI_BASE_URL, so no /v1 suffix here.
            base = f"http://host.docker.internal:{proxy_port}"
        else:
            # API key mode: proxy forwards to api.openai.com/v1/responses.
            # OpenAI SDK expects /v1 in the base URL.
            base = f"http://host.docker.internal:{proxy_port}/v1"
        return {"OPENAI_API_KEY": proxy_token, "OPENAI_BASE_URL": base}

    @staticmethod
    def on_new_task(session_dir: Path) -> None:
        pass  # no per-task config needed for codex

    @staticmethod
    def on_before_launch(worktree: Path) -> None:
        pass  # no worktree setup needed for codex

    @staticmethod
    def on_before_container_start(
        session_dir: Path,
        proxy_token: str,
        workdir: str,
    ) -> None:
        """Synthesise a sanitized ``config.toml`` for custom-provider mode.

        For OpenAI / OAuth hosts this is a no-op — the synthetic
        ``auth.json`` is created by the VolumeMount seed.

        For a host with a custom codex provider, the host's
        ``config.toml`` contains the real bearer token and must not be
        bind-mounted into the container.  Instead, write a fresh copy to
        ``session_dir/codex_config.toml`` that:

        - keeps the same ``model`` / ``model_provider`` /
          ``model_reasoning_effort`` so the agent picks the same model
        - rewrites ``model_catalog_json`` to the container-side path of
          the RO-bound ``model-catalog.json`` (when the host file
          exists)
        - mirrors the provider's ``wire_api`` so request shaping matches
        - replaces ``base_url`` with a placeholder
          (``http://placeholder/`` — overridden at exec time by the
          ``_DOCKER_WRAPPER`` using ``$OPENAI_BASE_URL``)
        - replaces ``experimental_bearer_token`` with the per-task
          proxy token (overridden at exec time using ``$OPENAI_API_KEY``)
        - pre-populates ``[projects.<workdir>]`` with
          ``trust_level = "trusted"`` so codex doesn't show the
          first-run "Do you trust this directory?" prompt (which on
          older codex builds also tries to persist the answer back to
          ``config.toml`` and crashes the TUI when persistence fails).
          Only the workdir is trusted — any other paths the host has
          trusted don't exist inside the container, so copying them
          would be inert.
        """
        custom = CodexBackend._read_custom_provider()
        if custom is None:
            return
        provider, _base_url, _bearer = custom
        section = CodexBackend._custom_provider_section() or {}
        top = CodexBackend._custom_provider_top_level()

        wire_api = section.get("wire_api", "responses")
        section_name = section.get("name", provider)

        host_catalog = Path.home() / ".codex" / "model-catalog.json"
        include_catalog = host_catalog.exists()

        # json.dumps gives us a safely-escaped TOML basic string (TOML
        # basic strings use JSON-style escapes).
        lines: list[str] = []
        if "model" in top:
            lines.append(f"model = {json.dumps(top['model'])}")
        lines.append(f"model_provider = {json.dumps(provider)}")
        if "model_reasoning_effort" in top:
            lines.append(f"model_reasoning_effort = {json.dumps(top['model_reasoning_effort'])}")
        if include_catalog:
            lines.append(f"model_catalog_json = {json.dumps(f'{CONTAINER_HOME}/.codex/model-catalog.json')}")
        lines.append("")
        lines.append(f"[model_providers.{provider}]")
        lines.append(f"name = {json.dumps(section_name)}")
        lines.append(f"wire_api = {json.dumps(wire_api)}")
        # Placeholder values — the wrapper overrides both via --config at
        # runtime once $OPENAI_BASE_URL (which encodes the ephemeral proxy
        # port) and $OPENAI_API_KEY (the proxy token) are known.
        lines.append('base_url = "http://placeholder/"')
        lines.append(f"experimental_bearer_token = {json.dumps(proxy_token)}")

        # Pre-populate project trust for the workdir so codex doesn't
        # prompt — and so older codex builds don't try to persist the
        # answer back to config.toml and crash the TUI when the write
        # is refused.
        if workdir:
            lines.append("")
            lines.append(f"[projects.{json.dumps(workdir)}]")
            lines.append('trust_level = "trusted"')

        session_dir.mkdir(parents=True, exist_ok=True)
        out = session_dir / "codex_config.toml"
        out.write_text("\n".join(lines) + "\n")
        out.chmod(0o600)

    @staticmethod
    def background_threads(
        meta: "SessionMeta",
        *,
        docker: bool,
        runtime: "Runtime | None",
        launch_start: float,
        stop: threading.Event,
    ) -> list[Callable[[], None]]:
        """Poll for codex's rollout file and persist the session UUID.

        Codex generates its session UUID at launch — there is no CLI flag
        to pre-set it — and stores rollouts at
        ``~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`` within
        ~1s of startup. We detect the file live so ``meta.session_id``
        is on disk before the process exits, which means resume works
        even if hatchery is killed mid-session.

        The poller runs on every launch (new + resume). On resume codex
        may create a new rollout file for the resumed thread; capturing
        the newest id keeps the chain fresh. Both probe paths apply an
        mtime filter against ``launch_start`` so rollouts from a previous
        task run (that reused this task's docker volume) never leak in.
        """
        return [_make_session_id_poller(meta, docker=docker, runtime=runtime, launch_start=launch_start, stop=stop)]

    dockerfile_install: str = f"""\
# ── OpenAI Codex CLI ──────────────────────────────────────────────────────────
USER root
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \\
    && rm -rf /var/lib/apt/lists/*
USER hatchery
RUN npm config set prefix '{CONTAINER_HOME}/.npm-global' \\
    && npm install -g @openai/codex"""
