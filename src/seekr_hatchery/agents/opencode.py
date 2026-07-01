"""OpenCode AI backend (opencode-ai npm package)."""

import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from seekr_hatchery.agents.agent_backend import CONTAINER_HOME, AgentBackend
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
    def _find_primary_provider(config: dict) -> tuple[str, dict] | None:
        """Return (provider_id, provider_dict) for the first custom provider.

        Iterates the ``provider`` map and skips IDs in ``_BUILTIN_PROVIDER_IDS``.
        Returns None when no custom provider is found.
        """
        providers = config.get("provider", {})
        for pid, pdata in providers.items():
            if pid not in _BUILTIN_PROVIDER_IDS:
                return pid, pdata
        return None

    @staticmethod
    def _read_credentials() -> tuple[str | None, str | None]:
        """Return (api_key, target_host) from the host's opencode config.

        Both values are resolved from {env:VAR} references in the config.
        Returns (None, None) when no custom provider is configured.
        """
        config = OpenCodeBackend._read_opencode_config()
        result = OpenCodeBackend._find_primary_provider(config)
        if result is None:
            return None, None

        _, pdata = result
        opts = pdata.get("options", {})

        raw_key = opts.get("apiKey", "")
        api_key = OpenCodeBackend._resolve_config_ref(raw_key) if raw_key else None
        if not api_key:
            api_key = None

        raw_url = opts.get("baseURL", "")
        target_host = None
        if raw_url:
            resolved = OpenCodeBackend._resolve_env_ref(raw_url)
            parsed = urlparse(resolved)
            target_host = parsed.netloc or None

        return api_key, target_host

    @staticmethod
    def _resolve_model(host_config: dict) -> str | None:
        """Return the model string to pass via ``opencode run -m``.

        ``opencode run`` ignores the ``model`` field in config files and always
        defaults to ``openai/gpt-5.2-codex``.  The model must be passed on the
        CLI with ``-m provider/model``.  We prefer the host's explicit ``model``
        setting, but only if it belongs to the primary (custom) provider — a
        stale ``model`` pointing at a builtin provider (e.g. ``openai/...``)
        would crash with ``ProviderModelNotFoundError`` once ``enabled_providers``
        restricts the sandbox to the custom provider alone.  Otherwise we fall
        back to the first model listed under the primary provider.
        """
        result = OpenCodeBackend._find_primary_provider(host_config)
        if result is None:
            return host_config.get("model")
        provider_id, provider_data = result
        models = provider_data.get("models", {})
        host_model = host_config.get("model")
        if host_model and host_model.startswith(f"{provider_id}/"):
            model_id = host_model.removeprefix(f"{provider_id}/")
            if model_id in models:
                return host_model
        if models:
            return f"{provider_id}/{next(iter(models))}"
        return None

    @staticmethod
    def _build_inline_config(proxy_url: str, proxy_token: str, host_config: dict) -> dict:
        """Build the JSON dict to inject as OPENCODE_CONFIG_CONTENT.

        Replaces the real provider's baseURL and apiKey with the proxy
        equivalents (literal values, not {env:} references).  The provider
        ID, name, npm SDK package, and models are copied from the host
        config so the agent can use the same model names inside the sandbox.

        SECURITY: proxy_token is a random UUID used only to authenticate
        against this task's proxy instance.  The real API key never appears.
        """
        result = OpenCodeBackend._find_primary_provider(host_config)
        if result is None:
            return {
                "permission": "allow",
                "provider": {
                    "proxy": {
                        "npm": "@ai-sdk/openai",
                        "name": "Hatchery Proxy",
                        "options": {
                            "baseURL": proxy_url,
                            "apiKey": proxy_token,
                        },
                    }
                },
            }

        provider_id, provider_data = result
        inline_provider = {**provider_data}
        inline_provider["options"] = {
            **provider_data.get("options", {}),
            "baseURL": proxy_url,
            "apiKey": proxy_token,
        }

        config: dict = {
            # Restrict to only this provider so opencode's free/builtin models
            # don't appear in the model picker inside the sandbox.
            "enabled_providers": [provider_id],
            "provider": {provider_id: inline_provider},
            # Auto-approve all permission prompts inside the sandbox.
            # Equivalent to --dangerously-skip-permissions on `opencode run`.
            # The container is already isolated, so interactive approval adds
            # no security value and breaks the autonomous workflow.
            "permission": "allow",
        }

        # Keep the `model` field in the config for the interactive TUI path
        # (hatchery chat launches `opencode` without `run`, and the TUI *does*
        # honour the config `model` field).  `opencode run` ignores it — the
        # build_*_command methods pass the same model via -m instead.
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
    def proxy_kwargs() -> dict:
        config = OpenCodeBackend._read_opencode_config()
        result = OpenCodeBackend._find_primary_provider(config)
        if result is None:
            raise RuntimeError(
                "no opencode provider configured; add a custom provider with "
                "a baseURL to ~/.config/opencode/opencode.json"
            )
        _, pdata = result
        raw_url = pdata.get("options", {}).get("baseURL", "")
        if not raw_url:
            raise RuntimeError(
                "no opencode provider configured; add a custom provider with "
                "a baseURL to ~/.config/opencode/opencode.json"
            )
        parsed = urlparse(OpenCodeBackend._resolve_config_ref(raw_url))
        target_host = parsed.netloc or None
        if not target_host:
            raise RuntimeError(
                "no opencode provider configured; add a custom provider with "
                "a baseURL to ~/.config/opencode/opencode.json"
            )
        # path_prefix is intentionally omitted: OPENCODE_CONFIG_CONTENT sets the
        # provider's baseURL to the full proxy URL (including the path component),
        # so the proxy receives requests at that path and forwards them unchanged.
        return {
            "target_host": target_host,
            "target_scheme": parsed.scheme or "https",
        }

    @staticmethod
    def make_header_mutator() -> Callable[..., dict[str, str]]:
        api_key, _ = OpenCodeBackend._read_credentials()
        if not api_key:
            raise RuntimeError(
                "no API key found for opencode; ensure apiKey is set in your "
                "custom provider in ~/.config/opencode/opencode.json "
                "(supports {env:VAR} and {file:PATH} references)"
            )

        def _mutate(headers: dict[str, str], *, refresh: bool = False) -> dict[str, str]:
            # refresh=True is a no-op for static API-key sources.
            out = {k: v for k, v in headers.items() if k.lower() not in ("x-api-key", "authorization")}
            out["Authorization"] = f"Bearer {api_key}"
            return out

        return _mutate

    @staticmethod
    def container_env(proxy_token: str, proxy_port: int) -> dict[str, str]:
        """Inject the proxy-backed provider config as OPENCODE_CONFIG_CONTENT.

        The full proxy URL (including the path component from the real baseURL,
        e.g. /v1) is constructed here where proxy_port is known.  The inline
        config JSON uses literal values — no {env:} references — because
        OPENCODE_CONFIG_CONTENT bypasses opencode's env-var substitution.
        """
        config = OpenCodeBackend._read_opencode_config()
        result = OpenCodeBackend._find_primary_provider(config)

        path = "/v1"
        if result is not None:
            _, pdata = result
            raw_url = pdata.get("options", {}).get("baseURL", "")
            if raw_url:
                resolved = OpenCodeBackend._resolve_config_ref(raw_url)
                parsed = urlparse(resolved)
                if parsed.path:
                    path = parsed.path

        proxy_url = f"http://host.docker.internal:{proxy_port}{path}"
        inline_config = OpenCodeBackend._build_inline_config(proxy_url, proxy_token, config)

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
    && npm install -g opencode-ai"""
