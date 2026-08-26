"""Pydantic models for hatchery domain objects.

This module is a deliberate leaf: it imports only ``includes`` (itself a
leaf) and is imported by both ``sessions`` and ``docker``. Keeping the
model here means neither of those modules needs to import the other just
for the type — relevant once subsequent refactors move lifecycle logic
into ``sessions``, where ``sessions.launch`` will call ``docker.run_session``
and the cycle would otherwise close.
"""

import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from seekr_hatchery.includes import IncludeEntry, load_include_entries

logger = logging.getLogger(__name__)

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

    # False when --branch pointed at a branch that already existed (local or
    # remote) and the worktree attached to it instead of creating it.
    # delete()/rollback paths must never `git branch -D` a branch hatchery
    # doesn't own.
    branch_owned: bool = True

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

        No-commit mode: ``<repo>/.hatchery`` (never committed, hidden via
        ``.git/info/exclude`` — see ``ensure_git_exclude``).
        Commit + no_worktree: ``<repo>/.hatchery``.
        Commit + worktree: ``<worktree>/.hatchery``.

        All derived paths (tasks, Dockerfile, docker.yaml) come from this.
        """
        if self.no_commit or self.no_worktree:
            return self.repo_path / ".hatchery"
        return self.worktree_path / ".hatchery"

    @property
    def task_dir(self) -> Path:
        """Where the task file lives — ``hatchery_dir / tasks``."""
        return self.hatchery_dir / "tasks"

    @property
    def task_file(self) -> Path | None:
        """The path to this task's markdown file, or None if not found."""
        from seekr_hatchery.sessions import find_task_file

        return find_task_file(self.task_dir, self.name)


# ── kubectl sidecar config ───────────────────────────────────────────────────


_KNOWN_VERBS: frozenset[str] = frozenset(
    {"get", "list", "watch", "create", "update", "patch", "delete", "deletecollection", "*"}
)


class KubectlRBACRule(BaseModel):
    """Single allowlist rule for the kubectl RBAC proxy.

    A request is allowed if it matches all three fields of at least one rule.
    ``"*"`` acts as a wildcard for that field.

    ``namespaces`` uses ``""`` (empty string) to match cluster-scoped requests
    (those without a ``/namespaces/{name}/`` segment in the URL, e.g.
    ``kubectl get pods -A`` or ``kubectl get nodes``).
    """

    verbs: list[str]
    """k8s verbs: get, list, watch, create, update, patch, delete, or ``*``.

    Client-side kubectl commands like ``describe``, ``logs``, ``exec`` are NOT
    valid RBAC verbs — they resolve to HTTP methods (``describe`` → ``GET``,
    ``exec`` → blocked subresource).  Unknown verbs are warned at load time and
    will never match any request.
    """

    resources: list[str]
    """Resource kinds: pods, services, deployments, etc., or ``*``."""

    namespaces: list[str] = ["*"]
    """Namespace names.  ``*`` matches everything.  ``""`` matches cluster-scoped
    (all-namespace / non-namespaced) requests."""

    @field_validator("verbs")
    @classmethod
    def _warn_unknown_verbs(cls, verbs: list[str]) -> list[str]:
        unknown = [v for v in verbs if v not in _KNOWN_VERBS]
        if unknown:
            logger.warning(
                "kubectl RBAC rules contain unrecognized verb(s) %s — "
                "these will never match any request. "
                "Valid verbs: %s. "
                "Note: 'describe' is a kubectl client command, not a k8s verb "
                "(it issues GET requests, which 'get' already covers).",
                unknown,
                sorted(_KNOWN_VERBS - {"*"}),
            )
        return verbs


class KubectlConfig(BaseModel):
    """Top-level kubectl proxy configuration loaded from docker.yaml."""

    context: str | None = None
    """Kubeconfig context to use.  Defaults to the host's active context.
    Set this when you have multiple contexts and want to pin which cluster
    the agent can reach (e.g. ``context: my-dev-cluster``)."""

    rules: list[KubectlRBACRule] = []
    """Allowlist rules.  Empty list means deny everything (fail-closed)."""
