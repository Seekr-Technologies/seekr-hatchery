"""Agent backend definitions.

Each concrete ``AgentBackend`` encodes everything hatchery needs to know about
one AI coding agent: how to invoke it (command construction), what it needs
mounted in the sandbox (home mounts, tmpfs), how to authenticate (API key
retrieval, proxy configuration, container env vars), and how to prepare
per-task state before the container starts.

Module-level singletons ``CLAUDE`` and ``CODEX`` are the only instances
callers should use.  ``from_kind()`` resolves a serialised string (e.g.
``"claude"``) back to the appropriate singleton.
"""

import json
import logging
import os
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Literal

import seekr_hatchery.tasks as tasks

logger = logging.getLogger("hatchery")

# Home directory of the non-root user inside every sandbox container.
CONTAINER_HOME = "/home/hatchery"

# Package-bundled skills directory.
_SKILLS_SRC = Path(__file__).parent / "skills"

# Fields in ~/.claude.json that store credentials.  These are stripped from
# per-task copies so the container never sees stored credentials that conflict
# with the proxy token injected as ANTHROPIC_API_KEY.
_AUTH_FIELDS: frozenset[str] = frozenset({"oauthAccount", "apiKey", "primaryApiKey"})


class AgentBackend(ABC):
    """Abstract base class for AI coding agent backends.

    Each concrete backend encodes the full set of capabilities and conventions
    for one agent, covering both command construction and Docker infrastructure.

    All methods are either pure static functions (no instance state) or
    class-level constant attributes.  ``self`` never carries data — the
    only reason the class hierarchy exists is polymorphic dispatch.
    """

    kind: str  # serialisation key stored in task metadata (e.g. "claude")
    binary: str  # executable name on $PATH
    supports_sessions: bool

    # ── Command construction ───────────────────────────────────────────────────

    @staticmethod
    @abstractmethod
    def build_new_command(
        session_id: str,
        system_prompt: str,
        initial_prompt: str,
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        """Return the full CLI invocation for a new session.

        When *docker* is True the command must include any flags needed to
        run non-interactively inside the container (e.g. Claude's
        ``--allow-dangerously-skip-permissions --settings …``).
        *workdir* is the agent's working directory inside the container and
        is only relevant when *docker* is True.
        """

    @staticmethod
    @abstractmethod
    def build_resume_command(
        session_id: str,
        system_prompt: str,
        initial_prompt: str = "",
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        """Return the full CLI invocation to resume an existing session.

        For agents without session support (codex) *session_id* is accepted
        but unused; *initial_prompt* provides task context instead.
        """

    @staticmethod
    @abstractmethod
    def build_finalize_command(
        session_id: str,
        system_prompt: str,
        wrap_up_prompt: str,
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        """Return the full CLI invocation for the wrap-up step."""

    # ── Docker infrastructure — pure functions ────────────────────────────────
    #
    # These return a constructed result and have no side-effects.

    @staticmethod
    @abstractmethod
    def get_api_key() -> str | None:
        """Return the API key for this agent, or None if not configured."""

    @staticmethod
    @abstractmethod
    def home_mounts(session_dir: Path) -> list[str]:
        """Return agent-specific host→container bind-mount strings.

        *session_dir* is ``tasks.task_session_dir(repo, name)`` — the per-task
        directory where ``on_new_task`` may have written config files.

        Common mounts shared by all agents (.gitconfig, uv cache) are added by
        ``docker._default_home_mounts()`` — do not include them here.
        Mount strings use the format ``"host_path:container_path:mode"``.
        """

    @staticmethod
    @abstractmethod
    def tmpfs_paths() -> list[str]:
        """Return container paths that should be shadowed with an empty tmpfs."""

    @staticmethod
    @abstractmethod
    def proxy_kwargs() -> dict:
        """Return keyword arguments to pass to ``proxy.start_proxy()``."""

    @staticmethod
    @abstractmethod
    def container_env(proxy_token: str, proxy_port: int) -> dict[str, str]:
        """Return environment variables to inject into the container."""

    # ── Lifecycle hooks ───────────────────────────────────────────────────────
    #
    # Hooks fire side-effects at specific points in the launch lifecycle.
    # Firing order for each launch type:
    #
    #   New task (cmd_new)
    #     1. on_new_task(session_dir)              one-time task setup
    #     2. on_before_launch(worktree)             every launch, native + Docker
    #     3. on_before_container_start(...)         Docker only
    #        └─ container runs
    #
    #   Resume (cmd_resume)
    #     1. on_before_launch(worktree)             every launch, native + Docker
    #     2. on_before_container_start(...)         Docker only
    #        └─ container runs
    #
    #   Finalize (_post_exit_check)
    #     (no hooks — only command construction is used)

    @staticmethod
    @abstractmethod
    def on_new_task(session_dir: Path) -> None:
        """One-time setup hook called when a new task is created.

        *session_dir* is ``tasks.task_session_dir(repo, name)`` — the per-task
        directory under ``~/.hatchery/tasks/``.  The backend may create or copy
        files here (e.g. a sanitised ``~/.claude.json`` for Claude).

        Not called on resume or finalize.
        """

    @staticmethod
    @abstractmethod
    def on_before_launch(worktree: Path) -> None:
        """Hook called before every agent launch — new, resume, native or Docker.

        *worktree* is the on-disk worktree path (or repo root in no-worktree
        mode).  The backend may write agent-specific files here (e.g. Claude
        writes skill definitions into ``.claude/skills/``).

        Not called before finalize.
        """

    @staticmethod
    @abstractmethod
    def on_before_container_start(
        session_dir: Path,
        proxy_token: str,
        workdir: str,
    ) -> None:
        """Hook called immediately before the Docker container starts.

        Runs on every Docker launch (new and resume); never in native mode.
        *session_dir* is the per-task directory (``tasks.task_session_dir``).
        *proxy_token* is the short-lived token injected as the API key for
        this launch.  *workdir* is the agent's working directory inside the
        container.
        """

    @property
    @abstractmethod
    def dockerfile_install(self) -> str:
        """Dockerfile snippet (RUN block) that installs this agent in the sandbox."""

    @property
    @abstractmethod
    def api_key_missing_hint(self) -> str:
        """Human-readable hint shown when the API key is not configured."""


class ClaudeBackend(AgentBackend):
    kind = "CLAUDE"
    binary = "claude"
    supports_sessions = True

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
        args = ["claude"]
        if docker:
            settings = json.dumps({"skipDangerousModePermissionPrompt": True, "trustedFolders": [workdir]})
            args += ["--allow-dangerously-skip-permissions", "--settings", settings]
        args += [
            "--permission-mode=plan",
            f"--append-system-prompt={system_prompt}",
            f"--session-id={session_id}",
            initial_prompt,
        ]
        return args

    @staticmethod
    def build_resume_command(
        session_id: str,
        system_prompt: str,
        initial_prompt: str = "",
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        args = ["claude"]
        if docker:
            settings = json.dumps({"skipDangerousModePermissionPrompt": True, "trustedFolders": [workdir]})
            args += ["--allow-dangerously-skip-permissions", "--settings", settings]
        args += [
            "--permission-mode=plan",
            f"--append-system-prompt={system_prompt}",
            f"--resume={session_id}",
        ]
        return args

    @staticmethod
    def build_finalize_command(
        session_id: str,
        system_prompt: str,
        wrap_up_prompt: str,
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        args = ["claude"]
        if docker:
            settings = json.dumps({"skipDangerousModePermissionPrompt": True, "trustedFolders": [workdir]})
            args += ["--allow-dangerously-skip-permissions", "--settings", settings]
        args += [
            f"--append-system-prompt={system_prompt}",
            f"--resume={session_id}",
            wrap_up_prompt,
        ]
        return args

    # ── Docker infrastructure ─────────────────────────────────────────────────

    @staticmethod
    def get_api_key() -> str | None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if key:
            logger.debug("Using ANTHROPIC_API_KEY from environment")
            return key
        logger.debug("ANTHROPIC_API_KEY not set, falling back to keychain")
        return ClaudeBackend._get_from_keychain()

    @staticmethod
    def _get_from_keychain() -> str | None:
        if sys.platform != "darwin":
            return None
        logger.debug("Checking macOS Keychain for Claude Code token")
        result = tasks.run(
            ["security", "find-generic-password", "-s", "Claude Code", "-w"],
            check=False,
            sensitive=True,
        )
        if result.returncode == 0:
            token = result.stdout.strip()
            if token:
                logger.debug("Found Claude Code token in macOS Keychain")
                return token
        logger.debug("No Claude Code token found in macOS Keychain")
        return None

    @staticmethod
    def home_mounts(session_dir: Path) -> list[str]:
        mounts = []
        claude_dir = Path.home() / ".claude"
        if claude_dir.exists():
            mounts.append(f"{claude_dir}:{CONTAINER_HOME}/.claude:rw")
        task_json = session_dir / "claude.json"
        if task_json.exists():
            mounts.append(f"{task_json}:{CONTAINER_HOME}/.claude.json:rw")
        return mounts

    @staticmethod
    def tmpfs_paths() -> list[str]:
        # Shadow ~/.claude/backups/ so timestamped credential copies never
        # appear inside the container.
        return [f"{CONTAINER_HOME}/.claude/backups"]

    @staticmethod
    def proxy_kwargs() -> dict:
        return {"target_host": "api.anthropic.com", "inject_header": "x-api-key"}

    @staticmethod
    def container_env(proxy_token: str, proxy_port: int) -> dict[str, str]:
        return {
            "ANTHROPIC_API_KEY": proxy_token,
            "ANTHROPIC_BASE_URL": f"http://host.docker.internal:{proxy_port}",
        }

    @staticmethod
    def on_new_task(session_dir: Path) -> None:
        """Seed a per-task copy of ~/.claude.json with auth fields stripped.

        Copies from the host on first call; subsequent calls (resume) are
        idempotent — the existing copy is kept, but auth fields are stripped
        as a migration guard for pre-sanitisation copies.
        Does nothing if no ~/.claude.json exists on the host.
        """
        src = Path.home() / ".claude.json"
        session_dir.mkdir(parents=True, exist_ok=True)
        task_copy = session_dir / "claude.json"

        if not task_copy.exists():
            if not src.exists():
                return
            try:
                data = json.loads(src.read_text())
            except (json.JSONDecodeError, OSError):
                data = {}
            for field in _AUTH_FIELDS:
                data.pop(field, None)
            task_copy.write_text(json.dumps(data))
            logger.debug("Seeded per-task ~/.claude.json (auth stripped) at %s", task_copy)
            return

    @staticmethod
    def _write_skills(worktree: Path) -> None:
        """Copy all skills from the package into the worktree's .claude/skills/ directory."""
        dest_base = worktree / ".claude" / "skills"
        for skill_dir in _SKILLS_SRC.iterdir():
            if not skill_dir.is_dir():
                continue
            dest = dest_base / skill_dir.name
            dest.mkdir(parents=True, exist_ok=True)
            for src_file in skill_dir.iterdir():
                if src_file.is_file():
                    (dest / src_file.name).write_bytes(src_file.read_bytes())
                    logger.debug("Wrote skill file: %s", dest / src_file.name)

    @staticmethod
    def on_before_launch(worktree: Path) -> None:
        """Copy hatchery skills into the worktree's .claude/skills/ directory."""
        ClaudeBackend._write_skills(worktree)

    @staticmethod
    def on_before_container_start(
        session_dir: Path,
        proxy_token: str,
        workdir: str,
    ) -> None:
        """Pre-seed trust and proxy token approval in the per-task claude.json."""
        claude_json = session_dir / "claude.json"
        ClaudeBackend._seed_trusted_folder(claude_json, workdir)
        ClaudeBackend._seed_proxy_token_approval(claude_json, proxy_token)

    @staticmethod
    def _seed_trusted_folder(claude_json: Path, container_workdir: str) -> None:
        """Pre-seed trust for container_workdir in the per-task claude.json.

        Two fields must be set to suppress the "do you trust this folder" prompt:
          - trustedFolders (top-level list)
          - projects.<path>.hasTrustDialogAccepted (per-project flag)

        Claude reads ~/.claude.json before any --settings override is applied, so
        pre-seeding here is the only reliable way to suppress the startup trust
        prompt without PTY injection.
        """
        try:
            data = json.loads(claude_json.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
        changed = False
        trusted: list[str] = data.get("trustedFolders", [])
        if container_workdir not in trusted:
            trusted.append(container_workdir)
            data["trustedFolders"] = trusted
            changed = True
        projects: dict = data.get("projects", {})
        project: dict = projects.get(container_workdir, {})
        if not project.get("hasTrustDialogAccepted"):
            project["hasTrustDialogAccepted"] = True
            projects[container_workdir] = project
            data["projects"] = projects
            changed = True
        if changed:
            claude_json.write_text(json.dumps(data))
            logger.debug("Seeded trust for %s in per-task claude.json", container_workdir)

    @staticmethod
    def _seed_proxy_token_approval(claude_json: Path, proxy_token: str) -> None:
        """Pre-approve the proxy token in the per-task claude.json.

        Claude Code stores the last 20 characters of previously-seen custom API
        keys in customApiKeyResponses.approved.  We overwrite the entire approved
        list with only our proxy token suffix — this both suppresses the prompt
        and ensures no real-key suffix from the host copy lingers in the file.
        """
        try:
            data = json.loads(claude_json.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
        key_suffix = proxy_token[-20:]
        data["customApiKeyResponses"] = {"approved": [key_suffix], "rejected": []}
        claude_json.write_text(json.dumps(data))
        logger.debug("Set proxy token approval in per-task claude.json")

    dockerfile_install: str = """\
# ── Claude Code ───────────────────────────────────────────────────────────────
RUN curl -fsSL https://claude.ai/install.sh | bash"""

    api_key_missing_hint: str = "Set ANTHROPIC_API_KEY or log in with `claude login` on the host."


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


# ── Module-level singletons ────────────────────────────────────────────────────

CLAUDE: AgentBackend = ClaudeBackend()
CODEX: AgentBackend = CodexBackend()

_REGISTRY: dict[str, AgentBackend] = {b.kind: b for b in [CLAUDE, CODEX]}


def from_kind(kind: str) -> AgentBackend:
    """Return the AgentBackend for *kind*, raising ValueError for unknown values."""
    try:
        return _REGISTRY[kind.upper()]
    except KeyError:
        valid = ", ".join(_REGISTRY)
        raise ValueError(f"unknown agent {kind!r}; valid choices: {valid}") from None
