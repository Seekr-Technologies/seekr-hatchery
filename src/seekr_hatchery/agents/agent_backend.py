"""AgentBackend abstract base class."""

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from seekr_hatchery.mount import Mount

if TYPE_CHECKING:
    from seekr_hatchery.docker import Runtime
    from seekr_hatchery.models import SessionMeta

# Home directory of the non-root user inside every sandbox container.
CONTAINER_HOME = "/home/hatchery"


class AgentBackend(ABC):
    """Abstract base class for AI coding agent backends.

    Each concrete backend encodes the full set of capabilities and conventions
    for one agent, covering both command construction and Docker infrastructure.

    All methods are either pure static functions (no instance state) or
    class-level constant attributes.  ``self`` never carries data — the
    only reason the class hierarchy exists is polymorphic dispatch.

    Class attributes:
      kind:
        Serialisation key stored in task metadata (e.g. ``"CODEX"``).
      binary:
        Executable name on ``$PATH``.
      supports_sessions:
        True when the agent's CLI can resume a prior conversation by id
        (e.g. ``claude --resume=<uuid>``, ``codex resume <uuid>``). When
        False the resume flow falls back to re-launching with the task
        file as context.
      session_id_pre_generated:
        True when the agent's CLI accepts a session id at launch time
        (e.g. claude's ``--session-id=<uuid>``) and hatchery pre-generates
        a UUID at task-creation time. False when the agent generates its
        own id at runtime (codex) — hatchery leaves ``meta.session_id``
        empty until the ``background_threads`` poller captures the real
        id.
    """

    kind: str
    binary: str
    supports_sessions: bool
    session_id_pre_generated: bool = True

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
    def make_header_mutator() -> Callable[..., dict[str, str]]:
        """Return a callable that transforms outbound request headers.

        Called once at proxy startup. The returned function is invoked for every
        proxied request with the inbound headers (hop-by-hop already stripped).
        It must strip inbound auth headers, inject the real API key in the
        correct format, and return the modified dict.

        The returned callable accepts an optional ``refresh: bool = False``
        keyword argument.  When ``refresh=True`` the backend should attempt to
        obtain a fresh credential (e.g. by firing a short test query for OAuth
        sources) before injecting the token into the returned headers.  For
        ``API_KEY`` sources, ``refresh=True`` is a no-op.

        Raises RuntimeError (with a human-readable message) if no credentials
        are available.
        """

    @staticmethod
    @abstractmethod
    def construct_mounts(session_dir: Path) -> list[Mount]:
        """Return the agent's container mounts.

        *session_dir* is ``sessions.task_session_dir(repo, name)`` — the per-task
        directory where ``on_new_task`` may have written config files.

        Return ``Mount`` objects covering both bind mounts (mode ``"ro"`` /
        ``"rw"``) and any tmpfs paths (mode ``"tmpfs"``, ``src=None``).  Common
        mounts shared by all agents (.gitconfig, uv cache) are added by
        ``docker._default_home_mounts()`` — do not include them here.
        """

    @staticmethod
    @abstractmethod
    def proxy_kwargs() -> dict:
        """Return keyword arguments to pass to ``proxy.api_server()``.

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
    #     4. background_threads(...) → workers      every launch, threads run
    #        └─ container runs (workers alive)         for the launch's lifetime
    #
    #   Resume (cmd_resume)
    #     1. on_before_launch(worktree)             every launch, native + Docker
    #     2. on_before_container_start(...)         Docker only
    #     3. background_threads(...) → workers      every launch
    #        └─ container runs (workers alive)
    #
    #   Finalize (_post_exit_check)
    #     1. background_threads(...) → workers      every launch
    #        └─ container runs (workers alive)

    @staticmethod
    @abstractmethod
    def on_new_task(session_dir: Path) -> None:
        """One-time setup hook called when a new task is created.

        *session_dir* is ``sessions.task_session_dir(repo, name)`` — the per-task
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
        *session_dir* is the per-task directory (``sessions.task_session_dir``).
        *proxy_token* is the short-lived token injected as the API key for
        this launch.  *workdir* is the agent's working directory inside the
        container.
        """

    @staticmethod
    def background_threads(
        meta: "SessionMeta",
        *,
        docker: bool,
        runtime: "Runtime | None",
        launch_start: float,
        stop: threading.Event,
    ) -> list[Callable[[], None]]:
        """Return nullary callables to run in daemon threads for the launch.

        The launch layer starts one thread per callable just before the
        agent process runs, signals *stop* in its finally block, and
        joins each thread with a short timeout.  Each callable may loop
        until *stop* is set, or return early once its work is done —
        both are supported.  Exceptions raised inside a worker are logged
        and swallowed; they never mask the launch's own exceptions.

        Workers may mutate *meta* and call ``sessions.save(meta)`` to
        persist state discovered live during the launch (e.g. an
        auto-generated session id detected on disk).

        Default: no workers.  Backends that need live work override.
        """
        return []

    @property
    @abstractmethod
    def dockerfile_install(self) -> str:
        """Dockerfile snippet (RUN block) that installs this agent in the sandbox."""

    @staticmethod
    def format_image_reference(path: Path) -> str:
        """How this agent expects an image path in its prompt stream.

        When hatchery's PTY proxy captures an image from the user's
        clipboard it saves the file and types the agent's preferred
        reference into the agent's stdin.  The default — a bare
        absolute path — is what Codex's TUI composer expects: it
        recognises any bare path as a file attachment and renders it
        as an ``[Image #N]`` chip.  Backends whose composer expects a
        different syntax should override.
        """
        return str(path)
