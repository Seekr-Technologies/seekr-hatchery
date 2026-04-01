"""Spawn protocol: provision new tasks from inside an agent session.

The core logic creates a git worktree, task file, initial commit, and
metadata entry — the same steps ``hatchery new`` performs, minus the
interactive bits.  The MCP server in ``mcp.py`` exposes this as a tool;
this module is a thin library with one public entry point: ``process_spawn()``.
"""

import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import seekr_hatchery.git as git
import seekr_hatchery.tasks as tasks
import seekr_hatchery.ui as ui

logger = logging.getLogger("hatchery")


@dataclass
class SpawnRequest:
    """A request to spawn a new task."""

    name: str  # normalized task name
    objective: str  # task description
    base: str  # git ref to branch from (parent's branch)
    source_file: Path | None = field(default=None)  # optional origin file (legacy)


def process_spawn(request: SpawnRequest, repo: Path, parent_name: str) -> bool:
    """Provision a new task from a spawn request.  Returns True on success."""
    try:
        # Check for name conflict
        db_path = tasks.task_db_path(repo, request.name)
        if db_path.exists():
            import json

            existing = json.loads(db_path.read_text())
            if existing.get("status") in ("in-progress", "running"):
                logger.warning(
                    "Spawn skipped: task '%s' already exists with status '%s'",
                    request.name,
                    existing["status"],
                )
                return False

        branch = f"hatchery/{request.name}"
        worktree = tasks.worktrees_dir(repo) / request.name

        git.create_worktree(repo, branch, worktree, request.base)
        tasks.write_task_file(worktree, request.name, branch, objective=request.objective)

        tasks.run(["git", "add", ".hatchery/"], cwd=worktree)
        tasks.run(
            ["git", "commit", "-m", f"task({request.name}): add task file (spawned from {parent_name})"],
            cwd=worktree,
        )

        from datetime import datetime

        meta = {
            "name": request.name,
            "branch": branch,
            "worktree": str(worktree),
            "repo": str(repo),
            "status": "in-progress",
            "created": datetime.now().isoformat(),
            "session_id": "",
            "no_worktree": False,
            "spawned_from": parent_name,
        }
        tasks.save_task(meta)

        # Clean up the source file if one was provided (legacy file-based protocol)
        if request.source_file is not None:
            request.source_file.unlink(missing_ok=True)

        try:
            _launch_background(repo, request.name, branch)
        except Exception:
            logger.warning(
                "Failed to auto-launch background container for '%s'; use 'hatchery resume %s' to start manually.",
                request.name,
                request.name,
            )

        ui.info(
            f"  Spawned task '{request.name}' — launching background container. "
            f"Attach with: hatchery attach {request.name}"
        )
        return True

    except Exception:
        logger.exception("Failed to process spawn request for '%s'", request.name)
        return False


def _launch_background(repo: Path, name: str, branch: str) -> None:
    """Fire-and-forget: start the background daemon subprocess."""
    cmd = [
        sys.executable,
        "-m",
        "seekr_hatchery.cli",
        "_spawn-launch",
        "--repo",
        str(repo),
        "--name",
        name,
        "--branch",
        branch,
    ]
    subprocess.Popen(
        cmd,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
