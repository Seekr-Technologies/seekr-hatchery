"""OpenCode AI backend (opencode-ai npm package)."""

import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from seekr_hatchery.agents.agent_backend import CONTAINER_HOME, AgentBackend, ProxyEndpoint
from seekr_hatchery.mount import BindMount, Mount, VolumeMount

logger = logging.getLogger("hatchery")


# Well-known builtin provider IDs that opencode ships with.  Custom providers
# are anything that doesn't appear in this set.
_BUILTIN_PROVIDER_IDS: frozenset[str] = frozenset(
    {
        "openai",
        "anthropic",
        "google",
        "mistral",
        "groq",
        "cohere",
        "amazon-bedrock",
        "azure",
        "openai-compatible",
        "deepseek",
        "xai",
        "vertex",
        "cerebras",
        "ollama",
        "lmstudio",
    }
)

# Default endpoints and env-var names for well-known builtin providers, extracted
# from the opencode-ai binary.  Providers not in this map (e.g. amazon-bedrock,
# azure, vertex) use cloud-specific auth that doesn't fit the simple API-key
# proxy model; users must configure those as custom providers with explicit
# baseURL and apiKey.
_BUILTIN_DEFAULTS: dict[str, tuple[str, str, str, str]] = {
    # provider_id: (host, scheme, path, env_var_name)
    "openai": ("api.openai.com", "https", "/v1", "OPENAI_API_KEY"),
    "anthropic": ("api.anthropic.com", "https", "/v1", "ANTHROPIC_API_KEY"),
    "google": ("generativelanguage.googleapis.com", "https", "/v1beta", "GOOGLE_GENERATIVE_AI_API_KEY"),
    "mistral": ("api.mistral.ai", "https", "/v1", "MISTRAL_API_KEY"),
    "groq": ("api.groq.com", "https", "/openai/v1", "GROQ_API_KEY"),
    "cohere": ("api.cohere.com", "https", "/v2", "COHERE_API_KEY"),
    "deepseek": ("api.deepseek.com", "https", "/v1", "DEEPSEEK_API_KEY"),
    "xai": ("api.x.ai", "https", "/v1", "XAI_API_KEY"),
    "cerebras": ("api.cerebras.ai", "https", "/v1", "CEREBRAS_API_KEY"),
}

# Auth header format per provider.  Most use ``Authorization: Bearer``;
# Anthropic uses ``x-api-key`` and Google uses ``x-goog-api-key``.  All are
# stripped on the inbound side and re-injected with the real key.
_AUTH_HEADER_STRIP: frozenset[str] = frozenset({"authorization", "x-api-key", "x-goog-api-key"})
_AUTH_FORMATS: dict[str, tuple[str, str]] = {
    # provider_id → (header_name, value_template) where {} is replaced by the key
    "anthropic": ("x-api-key", "{}"),
    "google": ("x-goog-api-key", "{}"),
}


class OpenCodeBackend(AgentBackend):
    """Backend for SST's opencode-ai (https://opencode.ai).

    Auth flow (Docker mode only):
      1. host reads real API key from config (env var or file reference)
         and stores it in a proxy closure — it never enters the container.
      2. container_env() injects OPENCODE_CONFIG_CONTENT with the full provider
         config JSON, replacing the real baseURL with the proxy URL and the
         real apiKey with the proxy token.
      3. opencode reads OPENCODE_CONFIG_CONTENT at startup.
      4. all LLM requests go to the host proxy which validates the proxy token,
         strips it, injects the real API key, and forwards to the real endpoint.
    """

    kind = "OPENCODE"
    binary = "opencode"
    # OpenCode tracks sessions in a SQLite DB at ~/.local/share/opencode/opencode.db.
    # The opencode-data named volume persists that directory across container runs,
    # and `opencode --continue` (or `opencode run --continue`) resumes the most
    # recent session.  Hatchery doesn't track the opencode session ID because
    # opencode manages it; resumption is handled by --continue alone.
    supports_sessions = True

    # ── Command construction ───────────────────────────────────────────────────

    # opencode has two launch modes:
    #   - `opencode` (TUI, default command): interactive, stays open. Use --prompt
    #     to pre-seed the first message. Honours the `model` field in config.
    #   - `opencode run <msg>`: one-shot. Responds and exits. Ignores the `model`
    #     config field; requires -m to select a model.
    #
    # hatchery new/resume need the interactive TUI (the user must approve the plan
    # and continue the conversation, mirroring codex's interactive session).
    # hatchery finalize uses `opencode run` — wrap-up is autonomous, no interaction.
    #
    # Both modes load config from ~/.config/opencode/ (config.json → opencode.json →
    # opencode.jsonc).  OPENCODE_CONFIG_CONTENT is higher precedence but `opencode run`
    # historically ignores provider/model fields from it, so the sh wrapper writes the
    # config to the global config path to cover all versions.
    _RUN_WRAPPER: str = (
        "printf '%s' \"$OPENCODE_CONFIG_CONTENT\""
        " | tee /home/hatchery/.config/opencode/config.json"
        " /home/hatchery/.config/opencode/opencode.json > /dev/null"
        ' && exec opencode "$@"'
    )

    @staticmethod
    def _model_flag() -> list[str]:
        """Return ``["-m", model]`` for ``opencode run``, or ``[]`` if unset.

        ``opencode run`` ignores the ``model`` field in config files and always
        defaults to ``openai/gpt-5.2-codex``.  Passing ``-m`` on the CLI is the
        only way to select a model for autonomous task mode.
        """
        config = OpenCodeBackend._read_opencode_config()
        model = OpenCodeBackend._resolve_model(config)
        return ["-m", model] if model else []

    @staticmethod
    def build_new_command(
        session_id: str,
        system_prompt: str,
        initial_prompt: str,
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        # session_id unused — opencode session resumption is via --continue, not an ID.
        prompt = f"{system_prompt}\n\n{initial_prompt}".strip()
        # Empty prompt means chat mode — launch the interactive TUI with no initial
        # message. --print-logs must NOT be passed here: it writes raw log lines to
        # stderr while the TUI is rendering, corrupting the display.
        if not prompt:
            if docker:
                return ["sh", "-c", OpenCodeBackend._RUN_WRAPPER, "sh"]
            return ["opencode"]
        # Non-empty prompt: launch the interactive TUI with --prompt so the message
        # is pre-seeded and the session stays open for plan approval + follow-up.
        # The TUI honours the `model` field from config, so no -m flag is needed.
        if docker:
            return ["sh", "-c", OpenCodeBackend._RUN_WRAPPER, "sh", "--prompt", prompt]
        return ["opencode", "--prompt", prompt]

    @staticmethod
    def build_resume_command(
        session_id: str,
        system_prompt: str,
        initial_prompt: str = "",
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        # session_id unused — opencode session IDs are generated inside the
        # container and don't survive across runs without a persistent volume.
        # --continue resumes the last session opencode created in this container.
        prompt = f"{system_prompt}\n\n{initial_prompt}".strip()
        # No --print-logs for TUI mode — same reason as build_new_command.
        if not prompt:
            # Resume the last session interactively, no new prompt.
            if docker:
                return ["sh", "-c", OpenCodeBackend._RUN_WRAPPER, "sh", "--continue"]
            return ["opencode", "--continue"]
        # Resume the last session and inject a follow-up prompt. The TUI stays open.
        if docker:
            return ["sh", "-c", OpenCodeBackend._RUN_WRAPPER, "sh", "--continue", "--prompt", prompt]
        return ["opencode", "--continue", "--prompt", prompt]

    @staticmethod
    def build_finalize_command(
        session_id: str,
        system_prompt: str,
        wrap_up_prompt: str,
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        # Finalize is autonomous — no user interaction needed, so `opencode run`
        # (one-shot) is the right mode. -m is required because `opencode run`
        # ignores the `model` config field. --print-logs is intentionally NOT
        # passed: it dumps raw timestamp=... level=INFO lines to stderr that the
        # user sees as noise during the wrap-up step.
        model_flag = OpenCodeBackend._model_flag()
        if docker:
            return ["sh", "-c", OpenCodeBackend._RUN_WRAPPER, "sh", "run", *model_flag, wrap_up_prompt]
        return ["opencode", "run", *model_flag, wrap_up_prompt]

    # ── Docker infrastructure ─────────────────────────────────────────────────

    @staticmethod
    def _read_opencode_config() -> dict:
        """Parse ~/.config/opencode/opencode.json; return {} on any error."""
        config_path = Path.home() / ".config" / "opencode" / "opencode.json"
        if not config_path.exists():
            return {}
        try:
            return json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.debug("Failed to parse ~/.config/opencode/opencode.json")
            return {}

    @staticmethod
    def _resolve_env_ref(value: str) -> str:
        """Expand {env:VAR_NAME} references using os.environ.

        Unset variables resolve to empty string, matching opencode's behaviour.
        """
        return re.sub(r"\{env:([^}]+)\}", lambda m: os.environ.get(m.group(1), ""), value)

    @staticmethod
    def _resolve_file_ref(value: str) -> str:
        """Expand {file:PATH} references by reading the file at PATH.

        Supports ~ expansion. Missing or unreadable files resolve to empty
        string, matching opencode's behaviour for unset credentials.
        """

        def _read(m: re.Match) -> str:
            try:
                return Path(m.group(1)).expanduser().read_text().strip()
            except OSError:
                return ""

        return re.sub(r"\{file:([^}]+)\}", _read, value)

    @staticmethod
    def _resolve_config_ref(value: str) -> str:
        """Expand all opencode credential references in value.

        Handles both {env:VAR_NAME} and {file:PATH} formats — the only two
        reference types currently supported by opencode's config parser.
        Env refs are resolved first so a {file:} path could theoretically
        embed an {env:} reference (unusual but consistent).
        """
        return OpenCodeBackend._resolve_file_ref(OpenCodeBackend._resolve_env_ref(value))

    @staticmethod
    def _resolve_provider_key(provider_id: str, provider_data: dict) -> str | None:
        """Resolve the API key for a provider.

        Checks explicit ``apiKey`` in options first (with {env:} and {file:}
        refs), then falls back to the standard env var for builtin providers.
        """
        opts = provider_data.get("options", {})
        raw_key = opts.get("apiKey", "")
        if raw_key:
            key = OpenCodeBackend._resolve_config_ref(raw_key)
            return key if key else None
        defaults = _BUILTIN_DEFAULTS.get(provider_id)
        if defaults:
            return os.environ.get(defaults[3]) or None
        return None

    @staticmethod
    def _proxyable_providers(config: dict) -> list[tuple[str, dict]]:
        """Return ``[(provider_id, provider_data), ...]`` for providers with resolvable keys.

        Includes both custom providers (with explicit baseURL+apiKey) and
        builtin providers (with keys from env vars or explicit apiKey).
        Excludes providers whose API key cannot be resolved.
        """
        result: list[tuple[str, dict]] = []
        for pid, pdata in config.get("provider", {}).items():
            if OpenCodeBackend._resolve_provider_key(pid, pdata):
                result.append((pid, pdata))
        return result

    @staticmethod
    def _provider_host_scheme(provider_id: str, provider_data: dict) -> tuple[str, str]:
        """Return ``(host, scheme)`` for a provider's upstream endpoint."""
        opts = provider_data.get("options", {})
        raw_url = opts.get("baseURL", "")
        if raw_url:
            resolved = OpenCodeBackend._resolve_config_ref(raw_url)
            parsed = urlparse(resolved)
            return parsed.netloc, parsed.scheme or "https"
        defaults = _BUILTIN_DEFAULTS.get(provider_id)
        if defaults:
            return defaults[0], defaults[1]
        return "", "https"

    @staticmethod
    def _provider_path(provider_id: str, provider_data: dict) -> str:
        """Return the URL path component for a provider (e.g. ``/v1``)."""
        opts = provider_data.get("options", {})
        raw_url = opts.get("baseURL", "")
        if raw_url:
            resolved = OpenCodeBackend._resolve_config_ref(raw_url)
            parsed = urlparse(resolved)
            return parsed.path or ""
        defaults = _BUILTIN_DEFAULTS.get(provider_id)
        if defaults:
            return defaults[2]
        return "/v1"

    @staticmethod
    def _make_key_mutator(api_key: str, provider_id: str) -> Callable[..., dict[str, str]]:
        """Return a header mutator that injects the real API key for this provider."""
        header_name, value_template = _AUTH_FORMATS.get(provider_id, ("Authorization", "Bearer {}"))
        header_value = value_template.format(api_key)

        def _mutate(headers: dict[str, str], *, refresh: bool = False) -> dict[str, str]:
            out = {k: v for k, v in headers.items() if k.lower() not in _AUTH_HEADER_STRIP}
            out[header_name] = header_value
            return out

        return _mutate

    @staticmethod
    def _resolve_model(host_config: dict) -> str | None:
        """Return the model string to pass via ``opencode run -m``, or None.

        Pure function on the config dict — no key resolution or filesystem
        access.  Prefers the host's explicit ``model`` setting (if its provider
        is in the config), then falls back to the first model from any
        provider.  Returns None when a builtin provider is present (opencode's
        default model will be available) or when no providers are configured
        (native mode).  Raises RuntimeError only when custom providers are
        present but none have models.
        """
        providers = host_config.get("provider", {})
        if not providers:
            return host_config.get("model")

        # 1. If config has `model` and its provider is in the config, return it
        host_model = host_config.get("model")
        if host_model and "/" in host_model:
            provider_id = host_model.split("/")[0]
            if provider_id in providers:
                pdata = providers[provider_id]
                if provider_id in _BUILTIN_PROVIDER_IDS:
                    return host_model
                models = pdata.get("models", {})
                model_id = host_model.removeprefix(f"{provider_id}/")
                if model_id in models:
                    return host_model

        # 2. Look for any provider with models
        for pid, pdata in providers.items():
            models = pdata.get("models", {})
            if models:
                return f"{pid}/{next(iter(models))}"

        # 3. If any builtin exists, return None (opencode's default will work)
        for pid in providers:
            if pid in _BUILTIN_PROVIDER_IDS:
                return None

        # 4. Custom providers present but none have models — can't resolve a model
        raise RuntimeError(
            "no models configured; add models to your custom provider in "
            "~/.config/opencode/opencode.json or set the appropriate API key "
            "env var (e.g. OPENAI_API_KEY) to use a builtin provider"
        )

    @staticmethod
    def _build_inline_config(proxy_urls: dict[str, str], proxy_token: str, host_config: dict) -> dict:
        """Build the JSON dict to inject as OPENCODE_CONFIG_CONTENT.

        Each provider in ``proxy_urls`` gets its own ``baseURL`` pointing to
        its dedicated proxy port, with the ``apiKey`` replaced by the proxy
        token.  The provider ID, name, npm SDK package, and models are copied
        from the host config so the agent can use the same model names inside
        the sandbox.

        SECURITY: proxy_token is a random UUID used only to authenticate against
        this task's proxy instances.  The real API keys never appear.
        """
        providers = host_config.get("provider", {})

        inline_providers: dict[str, dict] = {}
        enabled: list[str] = []
        for pid in proxy_urls:
            pdata = providers.get(pid, {})
            inline_provider = {**pdata}
            inline_provider["options"] = {
                **pdata.get("options", {}),
                "baseURL": proxy_urls[pid],
                "apiKey": proxy_token,
            }
            inline_providers[pid] = inline_provider
            enabled.append(pid)

        config: dict = {
            "enabled_providers": enabled,
            "provider": inline_providers,
            "permission": "allow",
        }

        model = OpenCodeBackend._resolve_model(host_config)
        if model:
            config["model"] = model

        return config

    # Cross-task host-shared paths under ~/.config/opencode/. RW binds so
    # in-container mutations propagate back to the host (a skill edited or agent
    # created in one task is visible to the next). These overlay the per-task
    # opencode-state volume the way codex's ~/.codex binds overlay its volume.
    # Dirs only — files (e.g. opencode.json) are deliberately not bound because
    # OPENCODE_CONFIG_CONTENT injects the proxy-backed provider config at a
    # higher precedence than the on-disk config, and binding the host's config
    # file would shadow the proxy URL/token with the real credentials.
    _CROSS_TASK_OPENCODE_DIRS: tuple[str, ...] = (
        "skills",
        "agents",
        "commands",
        "plugins",
    )

    @staticmethod
    def construct_mounts(session_dir: Path) -> list[Mount]:
        """Per-task volumes for opencode's XDG data directories.

        opencode uses the standard XDG layout, spreading state across three
        directories:

        - ``~/.config/opencode/``   — config files, installed plugins
        - ``~/.local/share/opencode/`` — SQLite DB (``opencode.db``) holding all
          session history, messages, projects, snapshots
        - ``~/.local/state/opencode/``  — prompt history, model preferences

        Without persisting ``~/.local/share/opencode/``, the SQLite DB is lost
        when the container exits and ``opencode --continue`` on resume finds no
        prior session — starting fresh with no conversation history.

        Two task-scoped named volumes preserve the config and data directories
        across container runs so ``hatchery resume`` picks up where the previous
        run left off.

        Bind mounts overlay specific subdirectories inside
        ``~/.config/opencode/`` so they cross task boundaries (skills, agents,
        commands, plugins) and stay in sync with host edits. Layered mounts on
        top of a volume mount are handled by the kernel — writes at the bind
        paths go to the host, everything else goes to the volume. This mirrors
        how the codex backend handles ``~/.codex/memories`` and ``~/.codex/skills``.

        opencode also searches ``~/.claude/skills/`` and ``~/.agents/skills/``
        for Claude- and agent-compatible skills, so those are bound RW too.

        Provider config is injected via OPENCODE_CONFIG_CONTENT (precedence 6)
        which overrides any opencode.json that may accumulate in the volume
        (precedence 2), so stale config files never clobber the proxy settings.
        The host's ``opencode.json`` is deliberately NOT bound — doing so would
        leak the real API key and endpoint into the container.
        """
        mounts: list[Mount] = [
            VolumeMount(
                name="opencode-state",
                dst=f"{CONTAINER_HOME}/.config/opencode",
            ),
            VolumeMount(
                name="opencode-data",
                dst=f"{CONTAINER_HOME}/.local/share/opencode",
            ),
        ]
        host_opencode = Path.home() / ".config" / "opencode"
        for name in OpenCodeBackend._CROSS_TASK_OPENCODE_DIRS:
            p = host_opencode / name
            if p.exists():
                mounts.append(BindMount(src=p, dst=f"{CONTAINER_HOME}/.config/opencode/{name}", mode="RW"))
        # Claude- and agent-compatible skill dirs — opencode searches these too.
        for compat in (".claude", ".agents"):
            p = Path.home() / compat / "skills"
            if p.exists():
                mounts.append(BindMount(src=p, dst=f"{CONTAINER_HOME}/{compat}/skills", mode="RW"))
        return mounts

    @staticmethod
    def proxy_endpoints() -> list[ProxyEndpoint]:
        """Return one :class:`ProxyEndpoint` per provider with resolvable credentials.

        Each endpoint gets its own proxy on its own ephemeral port.  Custom
        providers need an explicit ``baseURL`` and ``apiKey``; builtin
        providers (openai, anthropic, …) are auto-discovered via their
        standard env vars when no explicit ``apiKey`` is set.
        """
        config = OpenCodeBackend._read_opencode_config()
        proxied = OpenCodeBackend._proxyable_providers(config)

        endpoints: list[ProxyEndpoint] = []
        for pid, pdata in proxied:
            api_key = OpenCodeBackend._resolve_provider_key(pid, pdata)
            if not api_key:
                continue
            host, scheme = OpenCodeBackend._provider_host_scheme(pid, pdata)
            if not host:
                continue
            mutator = OpenCodeBackend._make_key_mutator(api_key, pid)
            endpoints.append(
                ProxyEndpoint(
                    key=pid,
                    header_mutator=mutator,
                    target_host=host,
                    target_scheme=scheme,
                )
            )

        if not endpoints:
            raise RuntimeError(
                "no opencode provider configured; add a custom provider with a "
                "baseURL and apiKey to ~/.config/opencode/opencode.json, or set "
                "the appropriate API key env var (e.g. OPENAI_API_KEY)"
            )

        return endpoints

    @staticmethod
    def container_env(proxy_token: str, proxy_ports: dict[str, int]) -> dict[str, str]:
        """Inject the proxy-backed provider config as OPENCODE_CONFIG_CONTENT.

        Each provider's ``baseURL`` points to its own proxy port.  The inline
        config JSON uses literal values — no {env:} references — because
        OPENCODE_CONFIG_CONTENT bypasses opencode's env-var substitution.
        """
        config = OpenCodeBackend._read_opencode_config()
        proxied = OpenCodeBackend._proxyable_providers(config)

        proxy_urls: dict[str, str] = {}
        for pid, pdata in proxied:
            port = proxy_ports[pid]
            path = OpenCodeBackend._provider_path(pid, pdata)
            proxy_urls[pid] = f"http://host.docker.internal:{port}{path}"

        inline_config = OpenCodeBackend._build_inline_config(proxy_urls, proxy_token, config)

        return {
            "OPENCODE_CONFIG_CONTENT": json.dumps(inline_config),
            # Bypass permission prompts via env var rather than CLI flag, which
            # avoids version-specific flag availability issues across opencode releases.
            "OPENCODE_DANGEROUSLY_SKIP_PERMISSIONS": "true",
        }

    # ── Lifecycle hooks ───────────────────────────────────────────────────────

    @staticmethod
    def on_new_task(session_dir: Path) -> None:
        pass

    @staticmethod
    def on_before_launch(worktree: Path) -> None:
        pass

    @staticmethod
    def on_before_container_start(
        session_dir: Path,
        proxy_token: str,
        workdir: str,
    ) -> None:
        pass

    dockerfile_install: str = f"""\
# ── OpenCode AI ───────────────────────────────────────────────────────────────
USER root
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \\
    && rm -rf /var/lib/apt/lists/*
USER hatchery
RUN npm config set prefix '{CONTAINER_HOME}/.npm-global' \\
    && npm install -g opencode-ai@1.18.10"""
