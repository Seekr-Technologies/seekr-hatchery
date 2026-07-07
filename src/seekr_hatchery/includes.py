"""Include-path types and helpers shared across cli, docker, git, and tasks."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

IncludeMode = Literal["worktree", "rw", "ro"]


class IncludeItem(BaseModel):
    """A validated include entry from docker.yaml.

    Accepts the dict form::

        {"path": "../repo", "mode": "ro"}

    Mode defaults to ``"worktree"`` when omitted.
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    mode: IncludeMode = "worktree"


@dataclass
class IncludeEntry:
    """A resolved include path together with its mount mode.

    mode is one of:
      "worktree" — read-write with branch isolation (creates a hatchery/<name> worktree)
      "rw"       — reference mount, read-write, no worktree
      "ro"       — reference mount, read-only, no worktree
    """

    path: Path
    mode: IncludeMode = "worktree"

    def is_reference(self) -> bool:
        return self.mode in ("rw", "ro")


def serialize_include_entries(entries: list[IncludeEntry]) -> list[dict]:
    """Serialise a list of IncludeEntry objects for storage in meta.json."""
    return [{"path": str(e.path), "mode": e.mode} for e in entries]


def load_include_entries(meta: dict) -> list[IncludeEntry]:
    """Load include entries from a meta.json dict, handling both old and new formats.

    Old format: ``"include": ["/abs/path", ...]``  (all treated as worktree mode)
    New format: ``"include": [{"path": ..., "mode": ...}, ...]``
    """
    raw = meta.get("include", [])
    result: list[IncludeEntry] = []
    for item in raw:
        if isinstance(item, str):
            result.append(IncludeEntry(path=Path(item), mode="worktree"))
        elif isinstance(item, dict):
            raw_path = item.get("path")
            if not raw_path:
                logger.warning("include entry missing 'path' key, skipping: %r", item)
                continue
            path = Path(raw_path)
            mode = item.get("mode", "worktree")
            if mode not in ("worktree", "rw", "ro"):
                logger.warning("Unknown include mode %r for %s; defaulting to 'worktree'", mode, path)
                mode = "worktree"
            result.append(IncludeEntry(path=path, mode=mode))
    return result
