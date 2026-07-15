"""Pydantic models for hatchery domain objects.

This module is a deliberate leaf: it imports only ``includes`` (itself a
leaf) and is imported by both ``sessions`` and ``docker``. Keeping the
model here means neither of those modules needs to import the other just
for the type — relevant once subsequent refactors move lifecycle logic
into ``sessions``, where ``sessions.launch`` will call ``docker.run_session``
and the cycle would otherwise close.
"""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from seekr_hatchery.includes import IncludeEntry, load_include_entries

SCHEMA_VERSION = 1


SessionStatus = Literal["in-progress", "running", "complete", "archived"]
SessionType = Literal["task", "chat"]


class SessionMeta(BaseModel):
    """Persistent metadata for a hatchery session (task or chat).

    Serialized to ``~/.hatchery/tasks/<repo-id>/<name>/meta.json``.
    Loaded via ``sessions.load()`` (which runs ``migrate()`` first).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    repo: str
    worktree: str

    type: SessionType = "task"
    status: SessionStatus = "in-progress"

    branch: str = ""
    created: str = ""
    completed: str | None = None
    session_id: str | None = None
    agent: str = "CODEX"

    no_worktree: bool = False
    no_commit: bool = False

    # Deliberately permissive: meta.json files in the wild contain include
    # entries in two on-disk shapes — legacy ``list[str]`` (each entry is a
    # path; mode defaults to "worktree") and current ``list[dict]`` with
    # ``{"path": ..., "mode": ...}``. ``list[Any]`` preserves whichever shape
    # was written without coercing it. The typed view is the ``include_entries``
    # property below, which parses the raw list into ``IncludeEntry`` objects
    # via ``load_include_entries``.
    include: list[Any] = []

    schema_version: int = SCHEMA_VERSION

    # ------------------------------------------------------------------
    # Pure-derivation properties (no git/docker/agent deps).
    # Free function is canonical where one exists; property delegates.
    # ------------------------------------------------------------------

    @property
    def is_chat(self) -> bool:
        return self.type == "chat"

    @property
    def is_complete(self) -> bool:
        return self.status == "complete"

    @property
    def repo_path(self) -> Path:
        return Path(self.repo)

    @property
    def worktree_path(self) -> Path:
        return Path(self.worktree)

    @property
    def meta_path(self) -> Path:
        from seekr_hatchery.sessions import task_db_path

        return task_db_path(self.repo_path, self.name)

    @property
    def session_dir(self) -> Path:
        from seekr_hatchery.sessions import task_session_dir

        return task_session_dir(self.repo_path, self.name)

    @property
    def container_name(self) -> str:
        from seekr_hatchery.sessions import container_name

        return container_name(self.repo_path, self.name)

    @property
    def image_name(self) -> str:
        from seekr_hatchery.sessions import image_name

        return image_name(self.repo_path, self.name)

    @property
    def include_entries(self) -> list[IncludeEntry]:
        return load_include_entries({"include": self.include})

    @property
    def hatchery_dir(self) -> Path:
        """The directory that holds this session's hatchery files.

        No-commit mode: out-of-tree store (``~/.hatchery/repos/<id>/``).
        Commit + no_worktree: ``<repo>/.hatchery``.
        Commit + worktree: ``<worktree>/.hatchery``.

        All derived paths (tasks, Dockerfile, docker.yaml) come from this.
        """
        from seekr_hatchery.sessions import repo_store_dir

        if self.no_commit:
            return repo_store_dir(self.repo_path)
        if self.no_worktree:
            return self.repo_path / ".hatchery"
        return self.worktree_path / ".hatchery"

    @property
    def task_dir(self) -> Path:
        """Where the task file lives — ``hatchery_dir / tasks``."""
        return self.hatchery_dir / "tasks"
