"""OpenAI Codex CLI backend."""

import json
import logging
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from seekr_hatchery.locks import hatchery_lock

from .agent_backend import CONTAINER_HOME, AgentBackend

logger = logging.getLogger("hatchery")


class CodexBackend(AgentBackend):
    kind = "CODEX"
    binary = "codex"
    supports_sessions = False

    # ── Command construction ───────────────────────────────────────────────────

    # codex (as of v0.121) ignores the OPENAI_BASE_URL environment variable.
    # The only supported mechanism for a custom base URL is openai_base_url in
    # config.toml or --config openai_base_url='"..."' at the CLI.  When running
    # in Docker mode we inject the proxy URL via --config at launch, reading it
    # from the OPENAI_BASE_URL env var that container_env() has already set in
    # the container.  The sh wrapper expands the env var at container startup
    # so the proxy port (ephemeral, unknown at command-build time) is resolved
    # correctly.
    _DOCKER_WRAPPER: str = (
        'exec codex --config "openai_base_url=\\"$OPENAI_BASE_URL\\"" --dangerously-bypass-approvals-and-sandbox "$@"'
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
        # session_id unused — codex has no resume support.
        # Re-run with combined context so the agent knows what to continue.
        prompt = f"{system_prompt}\n\n{initial_prompt}".strip()
        if docker:
            return ["sh", "-c", CodexBackend._DOCKER_WRAPPER, "sh", prompt]
        return ["codex", "--dangerously-bypass-approvals-and-sandbox", prompt]

    @staticmethod
    def build_finalize_command(
        session_id: str,
        system_prompt: str,
        wrap_up_prompt: str,
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        # session_id unused.
        if docker:
            return ["sh", "-c", CodexBackend._DOCKER_WRAPPER, "sh", wrap_up_prompt]
        return ["codex", "--dangerously-bypass-approvals-and-sandbox", wrap_up_prompt]

    # ── Docker infrastructure ─────────────────────────────────────────────────

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
    def home_mounts(session_dir: Path) -> list[str]:
        # Mount all of ~/.codex rw so sessions, state, skills etc. persist.
        # A fake auth.json (proxy token only) is shadow-mounted on top so the
        # real credentials are never visible inside the container.
        fake_auth = session_dir / "codex_auth.json"
        if not fake_auth.exists():
            raise RuntimeError(
                f"codex_auth.json not found in {session_dir} — on_before_container_start must run before home_mounts"
            )
        codex_dir = Path.home() / ".codex"
        return [
            f"{codex_dir}:{CONTAINER_HOME}/.codex:rw",
            f"{fake_auth}:{CONTAINER_HOME}/.codex/auth.json:rw",
        ]

    @staticmethod
    def tmpfs_paths() -> list[str]:
        return []

    @staticmethod
    def proxy_kwargs() -> dict:
        if CodexBackend._detect_auth_source() == "OAUTH":
            return {"target_host": "chatgpt.com", "path_prefix": "/backend-api/codex"}
        return {"target_host": "api.openai.com"}

    @staticmethod
    def make_header_mutator() -> Callable[..., dict[str, str]]:
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
        # Write a fake auth.json that authenticates to our proxy.
        # The real credential is injected by the proxy; the container only ever
        # sees the short-lived proxy token.
        #
        # Always use auth_mode="apikey" regardless of the host's real auth mode.
        # In apikey mode, codex respects OPENAI_BASE_URL (which points to the
        # proxy) for its API calls.  In chatgpt mode, codex bypasses
        # OPENAI_BASE_URL and goes directly to chatgpt.com, so the proxy is
        # never involved.
        #
        # For OAuth hosts, container_env sets OPENAI_BASE_URL without a /v1
        # suffix (e.g. http://proxy) and proxy_kwargs targets
        # chatgpt.com/backend-api/codex, so codex's apikey path
        # ({OPENAI_BASE_URL}/responses) correctly maps to the OAuth endpoint.
        fake_auth = {"auth_mode": "apikey", "OPENAI_API_KEY": proxy_token, "tokens": None}
        (session_dir / "codex_auth.json").write_text(json.dumps(fake_auth))

    dockerfile_install: str = f"""\
# ── OpenAI Codex CLI ──────────────────────────────────────────────────────────
USER root
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \\
    && rm -rf /var/lib/apt/lists/*
USER hatchery
RUN npm config set prefix '{CONTAINER_HOME}/.npm-global' \\
    && npm install -g @openai/codex"""
