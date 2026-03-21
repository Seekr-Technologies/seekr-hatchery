"""AgentBackend abstract base class."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

# Home directory of the non-root user inside every sandbox container.
CONTAINER_HOME = "/home/hatchery"


class AgentBackend(ABC):
    """Abstract base class for AI coding agent backends.

    Each concrete backend encodes the full set of capabilities and conventions
    for one agent, covering both command construction and Docker infrastructure.

    All methods are either pure static functions (no instance state) or
    class-level constant attributes.  ``self`` never carries data — the
    only reason the class hierarchy exists is polymorphic dispatch.
    """

    kind: str  # serialisation key stored in task metadata (e.g. "CODEX")
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
        run non-interactively inside the container.
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
    def make_header_mutator() -> Callable[[dict[str, str]], dict[str, str]]:
        """Return a callable that transforms outbound request headers.

        Called once at proxy startup. The returned function is invoked for every
        proxied request with the inbound headers (hop-by-hop already stripped).
        It must strip inbound auth headers, inject the real API key in the
        correct format, and return the modified dict.

        Raises RuntimeError (with a human-readable message) if no credentials
        are available.
        """

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
        """Return keyword arguments to pass to ``proxy.start_proxy()``.

        Note: does not include ``header_mutator`` — that is provided separately
        via ``make_header_mutator()``.
        """

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
        files here (e.g. agent-specific configuration).

        Not called on resume or finalize.
        """

    @staticmethod
    @abstractmethod
    def on_before_launch(worktree: Path) -> None:
        """Hook called before every agent launch — new, resume, native or Docker.

        *worktree* is the on-disk worktree path (or repo root in no-worktree
        mode).  The backend may write agent-specific files here.

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
