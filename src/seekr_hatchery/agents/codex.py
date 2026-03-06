"""OpenAI Codex CLI backend."""

import json
import logging
import os
from pathlib import Path
from typing import Literal

from .agent_backend import CONTAINER_HOME, AgentBackend

logger = logging.getLogger("hatchery")


class CodexBackend(AgentBackend):
    kind = "CODEX"
    binary = "codex"
    supports_sessions = False

    # ── Command construction ───────────────────────────────────────────────────

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
        return ["codex", "--dangerously-bypass-approvals-and-sandbox", wrap_up_prompt]

    # ── Docker infrastructure ─────────────────────────────────────────────────

    @staticmethod
    def _read_codex_creds() -> tuple[str | None, Literal["API_KEY", "OAUTH"] | None]:
        """Return (credential, source) from env or ~/.codex/auth.json. Single read."""
        key = os.environ.get("OPENAI_API_KEY")
        if key:
            logger.debug("Using OPENAI_API_KEY from environment")
            return key, "API_KEY"
        auth_file = Path.home() / ".codex" / "auth.json"
        if auth_file.exists():
            try:
                data = json.loads(auth_file.read_text())
            except (json.JSONDecodeError, OSError):
                logger.debug("Failed to parse ~/.codex/auth.json")
                return None, None
            if data.get("OPENAI_API_KEY"):
                logger.debug("Using OPENAI_API_KEY from ~/.codex/auth.json")
                return data["OPENAI_API_KEY"], "API_KEY"
            tokens = data.get("tokens") or {}
            access_token = tokens.get("access_token")
            if access_token:
                logger.debug("Using OAuth access_token from ~/.codex/auth.json")
                return access_token, "OAUTH"
        logger.debug("No OpenAI API key found")
        return None, None

    @staticmethod
    def get_api_key() -> str | None:
        return CodexBackend._read_codex_creds()[0]

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
            return {
                "target_host": "chatgpt.com",
                "inject_header": "authorization",
                "path_prefix": "/backend-api/codex",
            }
        return {"target_host": "api.openai.com", "inject_header": "authorization"}

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
        # Write a fake auth.json that authenticates to our proxy via API key
        # mode, regardless of the user's real auth mode (API key or OAuth).
        # The real credential is injected by the proxy; the container only ever
        # sees the short-lived proxy token.
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

    api_key_missing_hint: str = "Set OPENAI_API_KEY or log in with `codex login` for OAuth authentication."
