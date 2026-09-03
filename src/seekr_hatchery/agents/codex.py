"""OpenAI Codex CLI backend."""

import copy
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

import tomli_w

from seekr_hatchery.agents.agent_backend import CONTAINER_HOME, AgentBackend, ProxyEndpoint
from seekr_hatchery.locks import hatchery_lock
from seekr_hatchery.mount import BindMount, Mount, SeedContext, VolumeMount

if TYPE_CHECKING:
    from seekr_hatchery.docker import ContainerRuntime
    from seekr_hatchery.models import SessionMeta

logger = logging.getLogger(__name__)

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

# Every ``base_url`` in the container-side config.toml.  ``_DOCKER_WRAPPER``
# overrides the active provider's from ``$OPENAI_BASE_URL`` at exec time;
# anything it misses points somewhere unroutable rather than a real upstream.
_PLACEHOLDER_BASE_URL: str = "http://placeholder/"

# In-container path of the RO-bound host ``model-catalog.json``.
_CONTAINER_MODEL_CATALOG: str = f"{CONTAINER_HOME}/.codex/model-catalog.json"


@functools.lru_cache(maxsize=1)
def _host_config_data() -> dict:
    """Return the parsed contents of ``~/.codex/config.toml``.

    Cached for the lifetime of the Python process so the same launch
    reads the host config exactly once — avoids drift between
    ``proxy_endpoints`` / ``container_env`` /
    ``_render_container_config`` if the file is rewritten mid-launch
    (e.g. by a token-rotation script).

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
    runtime: "ContainerRuntime | None",
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
    runtime: "ContainerRuntime",
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
    runtime: "ContainerRuntime | None",
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
    # ``model_provider`` is pinned on the same branch so provider selection
    # survives whatever codex later rewrites into its own ``config.toml``.
    # ``check_for_update_on_startup=false`` suppresses the interactive
    # "Update available" prompt that otherwise blocks resume launches
    # while codex waits for the user to press enter. The Codex image is
    # rebuilt by hatchery, not upgraded interactively by the agent.
    _DOCKER_WRAPPER: str = (
        'if [ -n "${HATCHERY_CODEX_PROVIDER:-}" ]; then '
        "exec codex "
        "--config check_for_update_on_startup=false "
        '--config "model_provider=\\"${HATCHERY_CODEX_PROVIDER}\\"" '
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
    def _render_container_config(proxy_token: str, workdir: str) -> str:
        """Return the TOML body of the container-side ``~/.codex/config.toml``.

        The host config is the starting point, so the user's settings
        carry into the sandbox; the edits below make it safe to run
        there.  One path for every host setup — OpenAI API key, ChatGPT
        OAuth, or a custom on-prem provider.

        Comments and key order are not preserved (``tomllib`` in,
        ``tomli_w`` out).  Only the semantics matter inside a sandbox.
        """
        # Deep-copy: ``_host_config_data`` is process-cached and shared with
        # ``_read_custom_provider`` / ``container_env``.
        data = copy.deepcopy(_host_config_data())

        # Scrub credentials from every provider, not just the active one — the
        # container reaches its upstream only through the hatchery proxy, and
        # the TUI's model picker can switch providers mid-session.  The wrapper
        # re-injects the real proxy URL/token for the active provider at exec.
        providers = data.get("model_providers")
        if isinstance(providers, dict):
            for section in providers.values():
                if not isinstance(section, dict):
                    continue
                if "base_url" in section:
                    section["base_url"] = _PLACEHOLDER_BASE_URL
                if "experimental_bearer_token" in section:
                    section["experimental_bearer_token"] = proxy_token
        if "openai_base_url" in data:
            data["openai_base_url"] = _PLACEHOLDER_BASE_URL

        # Host path → container path, or drop it: a host path that doesn't
        # exist in the container would only produce a load error.
        if (Path.home() / ".codex" / "model-catalog.json").exists():
            data["model_catalog_json"] = _CONTAINER_MODEL_CATALOG
        else:
            data.pop("model_catalog_json", None)

        # Trust the workdir so codex doesn't prompt on startup.  Host entries
        # are replaced, not merged — those paths don't exist in the container.
        if workdir:
            data["projects"] = {workdir: {"trust_level": "trusted"}}
        else:
            data.pop("projects", None)

        return tomli_w.dumps(data)

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

        The volume is seeded with ``auth.json`` and ``config.toml`` so the
        in-container codex authenticates against the hatchery proxy and
        never sees the host's real credentials.  Everything else codex
        creates as it runs lives in the volume: sessions, history, sqlite
        state, caches, logs, etc. — all on the runtime's native
        filesystem rather than virtio-fs, and per-task so concurrent
        sandboxes don't fight over the same files.

        **``config.toml`` is deliberately not a mount.**  Codex saves it
        atomically (write ``config.toml.tmp``, then ``rename()`` over the
        target), and a single-file bind mount cannot receive a rename:
        the target is a kernel mount point, so the syscall fails with
        ``EBUSY`` — and the tmp file is on the volume while the target is
        on virtio-fs, so it is a cross-device rename besides.  Either one
        is enough to break codex's config persistence ("failed to persist
        config at ~/.codex/config.toml").  Seeding the file into the
        volume makes it an ordinary file on the volume's own filesystem,
        which is what codex needs.  The same reasoning applies to
        ``models_cache.json``, which codex also rewrites.

        The consequence is that host ``config.toml`` edits reach *new*
        tasks only, and the sandbox never writes back to the host file.
        Both are intended: a sandbox silently rewriting the user's real
        codex config — per task, concurrently — would be a bug.

        Bind mounts remain for state that should cross task boundaries.
        ``memories``, ``skills`` and ``prompts`` are *directories*: rename
        inside a mounted directory is an ordinary operation, so they are
        immune to the hazard above.  ``AGENTS.md`` is a single-file bind and
        so is not, but codex only reads it — nothing renames over it.
        ``model-catalog.json`` is bound RO —
        codex only ever reads it (it is the target of
        ``model_catalog_json``), so no rename lands on it.  Layered mounts
        on top of a volume mount are resolved by the kernel: writes at the
        bind paths go to the host, everything else goes to the volume.

        The user-authored paths all set ``follow_links``: they are the ones
        people keep in a dotfiles repo and symlink into place, and container
        ``$HOME`` does not mirror host ``$HOME``, so a symlinked entry inside
        one would otherwise dangle in the sandbox even with its parent
        mounted.  See :mod:`seekr_hatchery.mount_links`.  The flag only adds
        mounts; these ones are unchanged, so the directories are still bound
        whole and codex can create new entries in ``memories`` as before.

        A mount whose own source is a symlink needs nothing from the flag —
        ``-v`` resolves the source path, so the container gets the target's
        inode and no link survives to dangle.  That covers ``AGENTS.md`` as a
        symlinked file, and would equally cover a symlinked ``skills``
        directory.  The flag is declared on it anyway, where it is inert:
        the intent ("resolve links in the user's own config") is worth
        stating once for the whole group rather than splitting the loop over
        an implementation detail of the mechanism.
        """
        mounts: list[Mount] = [
            VolumeMount(
                name="codex-dir",
                dst=f"{CONTAINER_HOME}/.codex",
                seed=CodexBackend._seed_codex_dir,
            ),
        ]
        host_codex = Path.home() / ".codex"
        for name in ("AGENTS.md", "memories", "skills", "prompts"):
            p = host_codex / name
            if p.exists():
                mounts.append(
                    BindMount(
                        src=p,
                        dst=f"{CONTAINER_HOME}/.codex/{name}",
                        mode="RW",
                        follow_links=True,
                    )
                )

        catalog = host_codex / "model-catalog.json"
        if catalog.exists():
            mounts.append(BindMount(src=catalog, dst=_CONTAINER_MODEL_CATALOG, mode="RO"))
        return mounts

    @staticmethod
    def _seed_codex_dir(ctx: SeedContext) -> Mapping[str, bytes]:
        """Initial contents of the per-task ~/.codex volume.

        ``auth.json`` and ``config.toml`` are synthesised; codex populates
        everything else (sessions, logs, sqlite state, caches) inside the
        volume as it runs.  The seed runs once, when the volume is
        created — on resume codex's own edits to ``config.toml`` (default
        model, TUI preferences, migration prompts) are preserved.

        ``auth.json`` always uses ``auth_mode="apikey"`` regardless of the
        host's real mode. In apikey mode codex respects
        ``OPENAI_BASE_URL`` (which ``container_env`` sets to the proxy).
        For OAuth hosts, ``container_env`` and ``proxy_endpoints`` together
        route codex's apikey path through the OAuth backend; the
        container never sees the host's OAuth tokens.

        With a custom provider the active provider authenticates via its
        own ``experimental_bearer_token`` (set in ``config.toml``), so
        this ``auth.json`` is harmless overlap — codex only falls back to
        it for the built-in ``openai`` provider.
        """
        fake_auth = {
            "auth_mode": "apikey",
            "OPENAI_API_KEY": ctx.proxy_token,
            "tokens": None,
        }
        return {
            "auth.json": json.dumps(fake_auth).encode(),
            "config.toml": CodexBackend._render_container_config(ctx.proxy_token, ctx.container_workdir).encode(),
        }

    @staticmethod
    def _proxy_target() -> dict:
        """Return ``{"target_host": ..., "path_prefix": ...}`` for the active auth source.

        Custom-provider mode wins over OAuth / API-key — the user
        explicitly configured a different upstream in config.toml.

        The provider's URL path (e.g. ``/v1``) lives in the container's
        ``OPENAI_BASE_URL`` — see ``container_env`` — not in
        ``path_prefix``.  Putting it in both would forward to
        ``<host>/v1/v1/responses`` and yield a 404 from the upstream.
        This mirrors the OpenAI API-key path (target_host=api.openai.com,
        container sees ``…/v1``).

        TLS verification uses the OS trust store via
        ``truststore.SSLContext`` in ``sidecars.api_sidecar.proxy.api_server``, so any
        non-public CA the user has installed system-wide is trusted
        automatically — no hatchery-specific CA config needed.
        """
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
    def _build_header_mutator() -> Callable[..., dict[str, str]]:
        """Return a callable that transforms outbound request headers.

        Strips inbound auth headers, injects the real API key in the
        correct format, and returns the modified dict. Accepts an optional
        ``refresh: bool = False`` keyword argument: when True, attempts to
        obtain a fresh credential (OAuth sources only; API_KEY is a no-op)
        before injecting the token.

        Raises RuntimeError (with a human-readable message) if no
        credentials are available.
        """
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
    def proxy_endpoints() -> list[ProxyEndpoint]:
        target = CodexBackend._proxy_target()
        mutator = CodexBackend._build_header_mutator()
        return [
            ProxyEndpoint(
                key="default",
                header_mutator=mutator,
                target_host=target["target_host"],
                path_prefix=target.get("path_prefix", ""),
            )
        ]

    @staticmethod
    def container_env(endpoint_key: str, proxy_token: str, proxy_port: int) -> dict[str, str]:
        del endpoint_key  # codex has exactly one endpoint
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
        """No-op — the container-side ``~/.codex`` is built by the volume seed.

        ``_seed_codex_dir`` synthesises both ``auth.json`` and
        ``config.toml`` from ``SeedContext``, which carries the same
        *proxy_token* and *workdir* this hook receives.  Nothing needs to
        be staged on the host beforehand.
        """

    @staticmethod
    def background_threads(
        meta: "SessionMeta",
        *,
        docker: bool,
        runtime: "ContainerRuntime | None",
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
