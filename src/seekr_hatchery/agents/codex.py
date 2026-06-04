"""OpenAI Codex CLI backend."""

import json
import logging
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal

from seekr_hatchery.agents.agent_backend import CONTAINER_HOME, AgentBackend
from seekr_hatchery.locks import hatchery_lock
from seekr_hatchery.mount import BindMount, Mount, SeedContext, VolumeMount

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
        """
        mounts: list[Mount] = [
            VolumeMount(
                name="codex-dir",
                dst=f"{CONTAINER_HOME}/.codex",
                seed=CodexBackend._seed_codex_dir,
            ),
        ]
        host_codex = Path.home() / ".codex"
        # Cross-task host-shared paths: RW binds so in-container
        # mutations propagate back to the host (a memory recorded or
        # skill edited in one task is visible to the next; the model
        # cache and config edits stay in sync).
        for name in ("memories", "skills", "config.toml", "models_cache.json"):
            p = host_codex / name
            if p.exists():
                mounts.append(BindMount(src=p, dst=f"{CONTAINER_HOME}/.codex/{name}", mode="RW"))
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
        """
        fake_auth = {
            "auth_mode": "apikey",
            "OPENAI_API_KEY": ctx.proxy_token,
            "tokens": None,
        }
        return {"auth.json": json.dumps(fake_auth).encode()}

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
        """No-op — synthesising the fake ``auth.json`` moved into the
        VolumeMount's seed callable (``_seed_codex_dir``)."""
        return

    dockerfile_install: str = f"""\
# ── OpenAI Codex CLI ──────────────────────────────────────────────────────────
USER root
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \\
    && rm -rf /var/lib/apt/lists/*
USER hatchery
RUN npm config set prefix '{CONTAINER_HOME}/.npm-global' \\
    && npm install -g @openai/codex"""
