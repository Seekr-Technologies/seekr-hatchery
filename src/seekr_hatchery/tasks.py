"""Constants, path helpers, task I/O, schema migration, filesystem scaffolding."""

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import seekr_hatchery.ui as ui

logger = logging.getLogger("hatchery")


def run(
    cmd: list[str], cwd: Path | None = None, check: bool = True, sensitive: bool = False
) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing stdout/stderr.

    Logs the command at DEBUG level before running and the result (or
    ``<redacted>`` when *sensitive* is True) afterwards.  On non-zero exit with
    *check=True* the CalledProcessError is re-raised after printing a human-
    readable error to stderr.
    """
    logger.debug("run %s (cwd=%s)", cmd, cwd)
    try:
        result = subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        ui.error(f"command failed (exit {e.returncode}): {' '.join(str(a) for a in e.cmd)}")
        if e.stdout.strip():
            ui.info(f"  stdout: {e.stdout.strip()}")
        if e.stderr.strip():
            ui.info(f"  stderr: {e.stderr.strip()}")
        raise
    if sensitive:
        logger.debug("  -> rc=%d stdout=<redacted> stderr=<redacted>", result.returncode)
    else:
        logger.debug("  -> rc=%d stdout=%r stderr=%r", result.returncode, result.stdout[:200], result.stderr[:200])
    return result


# ---------------------------------------------------------------------------
# Config / paths
# ---------------------------------------------------------------------------

HATCHERY_DIR = Path.home() / ".hatchery"
TASKS_DB_DIR = HATCHERY_DIR / "tasks"
DEFAULT_BASE = "HEAD"  # branch/commit new tasks fork from by default
SCHEMA_VERSION = 1
DB_SCHEMA_VERSION = 1

WORKTREES_SUBDIR = Path(".hatchery") / "worktrees"
DOCKER_CONFIG = Path(".hatchery") / "docker.yaml"

# Inside the container the repo is always mounted here.
CONTAINER_REPO_ROOT = "/repo"

# Included paths (--include) are mounted under this prefix inside the container.
CONTAINER_INCLUDES_ROOT = "/includes"

# Appended to the agent's default system prompt (preserving its built-in
# tool knowledge and workspace awareness). Edit here — single source of truth.
SESSION_SYSTEM = """\
You are working on a task. The task file is at `.hatchery/tasks/<date>-<name>.md`
(the exact path is in your session prompt). This single file serves as brief,
plan, progress log, and final notes — update it in place as you work.

Your workflow:

1. PLAN FIRST: Read the task file, ask any clarifying questions, then propose
   a concrete numbered implementation plan. Do not write any code until the
   user approves the plan.

2. ON APPROVAL: Update the "Agreed Plan" section of the task file with the
   final plan, then proceed to implement it step by step.

3. WHILE EXECUTING: After each step, tick the checkbox in the Progress Log
   and make a descriptive git commit.

4. IF BLOCKED: If you hit something that would materially change the plan,
   stop and discuss before proceeding.

5. ON COMPLETION: Mark Status as "complete". Add a "## Summary" section
   covering key decisions, patterns established, gotchas, and anything a
   future agent should know. Then remove the "## Agreed Plan" and
   "## Progress Log" sections — they are working scaffolding, not permanent
   record. The final file should read as a clean ADR:
   Status/Branch/Created → Objective → Context → Summary.
   This file will be merged into main as the permanent record of this task.
"""

def _include_container_basename(path: Path, used: set[str]) -> str:
    """Return a collision-safe container basename for an included path."""
    name = path.name
    if name not in used:
        used.add(name)
        return name
    i = 1
    while f"{name}-{i}" in used:
        i += 1
    result = f"{name}-{i}"
    used.add(result)
    return result


def sandbox_context(
    name: str,
    branch: str,
    worktree: Path,
    repo: Path,
    main_branch: str,
    use_docker: bool,
    no_worktree: bool = False,
    include_paths: list[Path] | None = None,
) -> str:
    """Return a system-prompt section describing the sandbox environment."""
    include_paths = include_paths or []
    if no_worktree and use_docker:
        lines = [
            "# Sandbox Environment",
            "",
            "You are running inside an **isolated Docker container** (no git worktree).",
            "",
            "**Filesystem permissions:**",
            "- `/workspace/` — your working directory (read-write; all edits land here)",
        ]
        if branch:
            lines += [
                "",
                f"**Your branch:** `{branch}`",
                f"**Target branch for PRs:** `{main_branch}`",
                "",
                f"When creating commits or pull requests, target `{main_branch}`. You may push to `{branch}` only.",
            ]
    elif no_worktree:
        lines = [
            "# Sandbox Environment",
            "",
            "You are running **directly in the working directory** (no Docker, no git worktree isolation).",
            "",
            f"**Working directory:** `{worktree}`",
        ]
        if branch:
            lines += [
                f"**Your branch:** `{branch}`",
                f"**Target branch for PRs:** `{main_branch}`",
                "",
                f"When creating commits or pull requests, target `{main_branch}`. You may push to `{branch}` only.",
            ]
    elif use_docker:
        container_worktree = f"{CONTAINER_REPO_ROOT}/.hatchery/worktrees/{name}"
        lines = [
            "# Sandbox Environment",
            "",
            "You are running inside an **isolated Docker container**.",
            "",
            "**Filesystem permissions:**",
            f"- `{container_worktree}/` — your worktree (read-write; all edits land here)",
            f"- `{CONTAINER_REPO_ROOT}/` — the repository (read-only; main-branch files cannot be modified)",
            f"- `{CONTAINER_REPO_ROOT}/.git/objects/` — git object store (read-write; your commits are visible on the host)",
            f"- `{CONTAINER_REPO_ROOT}/.git/refs/heads/hatchery/` — branch refs (read-write for your branch only)",
            "",
            f"**Your branch:** `{branch}`",
            f"**Target branch for PRs:** `{main_branch}`",
            "",
            f"When creating commits or pull requests, target `{main_branch}`. You may push to `{branch}` only.",
        ]
    else:
        lines = [
            "# Sandbox Environment",
            "",
            "You are running in a **native git worktree** (no Docker isolation).",
            "",
            f"**Working directory:** `{worktree}`",
            f"**Repository root:** `{repo}`",
            f"**Your branch:** `{branch}`",
            f"**Target branch for PRs:** `{main_branch}`",
            "",
            f"When creating commits or pull requests, target `{main_branch}`. You may push to `{branch}` only.",
        ]

    if include_paths:
        lines += ["", "**Included paths** (read-write):"]
        used_basenames: set[str] = set()
        for inc in include_paths:
            is_git = (inc / ".git").exists()
            if use_docker:
                basename = _include_container_basename(inc, used_basenames)
                container_inc = f"{CONTAINER_INCLUDES_ROOT}/{basename}"
                if is_git and not no_worktree:
                    container_inc_wt = f"{container_inc}/.hatchery/worktrees/{name}"
                    lines.append(
                        f"- `{container_inc}/` — git repo; your worktree: `{container_inc_wt}/`"
                    )
                else:
                    kind = "git repo" if is_git else "directory"
                    lines.append(f"- `{container_inc}/` — {kind}")
            else:
                # Native mode: report host paths
                if is_git and not no_worktree:
                    wt = inc / WORKTREES_SUBDIR / name
                    lines.append(f"- `{wt}/` — git repo worktree (host path)")
                else:
                    kind = "git repo" if is_git else "directory"
                    lines.append(f"- `{inc}/` — {kind} (host path)")

    return "\n".join(lines)


def task_file_name(name: str) -> str:
    return f"{datetime.now().strftime('%Y-%m-%d')}-{name}.md"


def find_task_file(worktree: Path, name: str) -> Path | None:
    """Find a task's markdown file regardless of creation date."""
    tasks_dir = worktree / ".hatchery" / "tasks"
    matches = sorted(tasks_dir.glob(f"*-{name}.md"))
    return matches[-1] if matches else None


def session_prompt(name: str, worktree: Path) -> str:
    task_path = find_task_file(worktree, name)
    if task_path is None:
        ui.error(f"task file not found for '{name}' in {worktree / '.hatchery' / 'tasks'}")
        sys.exit(1)
    rel_path = str(task_path.relative_to(worktree))
    contents = task_path.read_text()
    return f"The task file is at `{rel_path}`:\n\n{contents}\nPlease begin."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def to_name(raw: str) -> str:
    """Normalise a task name to a filesystem/branch-safe slug."""
    s = raw.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")[:50]


def repo_id(repo: Path) -> str:
    """Stable, human-readable identifier for a repo path."""
    short_hash = hashlib.sha256(str(repo).encode()).hexdigest()[:8]
    basename = to_name(repo.name)[:20]
    return f"{basename}-{short_hash}"


def task_dir(repo: Path, name: str) -> Path:
    """Unified directory for all per-task state (metadata + session files)."""
    return TASKS_DB_DIR / repo_id(repo) / name


def task_db_path(repo: Path, name: str) -> Path:
    return task_dir(repo, name) / "meta.json"


def task_session_dir(repo: Path, name: str) -> Path:
    """Session state lives in the same unified task directory."""
    return task_dir(repo, name)


def worktrees_dir(repo: Path) -> Path:
    """Worktrees live inside the repo under .hatchery/worktrees, which is gitignored."""
    return repo / WORKTREES_SUBDIR


def db_meta_path() -> Path:
    """Path to the DB-level schema version file: ~/.hatchery/meta.json"""
    return HATCHERY_DIR / "meta.json"


def migrate_db() -> None:
    """Run the DB-level migration chain. Called at CLI startup.

    Reads ~/.hatchery/meta.json (or assumes v0 if absent), runs each
    migration block in order, then writes the updated version.
    """
    meta_path = db_meta_path()
    if meta_path.exists():
        try:
            v = json.loads(meta_path.read_text()).get("schema_version", 0)
        except (json.JSONDecodeError, KeyError):
            v = 0
    else:
        v = 0

    if v >= DB_SCHEMA_VERSION:
        return  # nothing to do

    # v0 → v1: promote scoped <name>.json → unified <name>/meta.json
    # Flat tasks/<name>.json files (oldest format) are left in place — they
    # are lazily migrated by load_task() on demand.
    if v == 0:
        if TASKS_DB_DIR.exists():
            for repo_subdir in TASKS_DB_DIR.iterdir():
                if not repo_subdir.is_dir():
                    continue
                for scoped_file in list(repo_subdir.glob("*.json")):
                    name = scoped_file.stem
                    dest = repo_subdir / name / "meta.json"
                    if dest.exists():
                        scoped_file.unlink()  # unified dir already has it
                        continue
                    try:
                        data = json.loads(scoped_file.read_text())
                    except json.JSONDecodeError:
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(json.dumps(data, indent=2))
                    scoped_file.unlink()
                    logger.info("DB migrate v0→v1: promoted %s → %s", scoped_file, dest)
        v = 1

    # Write updated DB meta
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps({"schema_version": v}, indent=2))
    logger.debug("DB schema version written: %d", v)


def migrate(meta: dict) -> dict:
    """Bring a task dict up to the current schema version.

    Add a new `if v == N` block here whenever the schema changes.
    Each block should make the minimal edit to reach version N+1,
    then increment meta["schema_version"]. The final state will
    always be SCHEMA_VERSION.
    """
    v = meta.get("schema_version", 0)

    # v0 -> v1: initial versioned schema (just stamps the version)
    if v == 0:
        meta["schema_version"] = 1
        v = 1

    return meta


def load_task(repo: Path, name: str) -> dict:
    path = task_db_path(repo, name)
    logger.debug("Loading task metadata from %s", path)
    if path.exists():
        return migrate(json.loads(path.read_text()))
    ui.error(f"task '{name}' not found.")
    sys.exit(1)


def save_task(meta: dict) -> None:
    path = task_db_path(Path(meta["repo"]), meta["name"])
    path.parent.mkdir(parents=True, exist_ok=True)
    meta["schema_version"] = SCHEMA_VERSION
    logger.debug("Saving task metadata to %s", path)
    path.write_text(json.dumps(meta, indent=2))


def repo_tasks_for_current_repo(repo: Path) -> list[dict]:
    """Return all tasks belonging to the given repo, newest first."""
    if not TASKS_DB_DIR.exists():
        return []
    repo_str = str(repo)
    found: list[dict] = []

    subdir = TASKS_DB_DIR / repo_id(repo)
    if subdir.exists():
        # New unified dirs: tasks/<repo-id>/<name>/meta.json
        for f in subdir.glob("*/meta.json"):
            try:
                meta = json.loads(f.read_text())
                found.append(meta)
            except (json.JSONDecodeError, KeyError):
                continue

    # Flat files (pre-migration fallback)
    for f in TASKS_DB_DIR.glob("*.json"):
        try:
            meta = json.loads(f.read_text())
            if meta.get("repo") == repo_str:
                found.append(meta)
        except (json.JSONDecodeError, KeyError):
            continue

    return sorted(found, key=lambda t: t.get("created", ""), reverse=True)


# ---------------------------------------------------------------------------
# Repo scaffolding
# ---------------------------------------------------------------------------


def write_task_file(worktree: Path, name: str, branch: str, objective: str | None = None) -> Path:
    tasks_dir = worktree / ".hatchery" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    task_path = tasks_dir / task_file_name(name)
    logger.debug("Task file written at %s", task_path)
    branch_line = f"**Branch**: {branch}" if branch else "**Branch**: (none — no-worktree mode)"
    if objective is not None:
        body = f"""\
# Task: {name}

**Status**: in-progress
{branch_line}
**Created**: {datetime.now().strftime("%Y-%m-%d %H:%M")}

## Objective

{objective}

## Agreed Plan

*(To be filled in after planning discussion)*

## Progress Log

*(Steps will appear here once the plan is agreed)*

## Summary

*(Fill in on completion — then remove Agreed Plan and Progress Log above.
Cover: key decisions made, patterns established, files changed, gotchas,
and anything a future agent working in this repo should know.)*
"""
    else:
        body = f"""\
# Task: {name}

**Status**: in-progress
{branch_line}
**Created**: {datetime.now().strftime("%Y-%m-%d %H:%M")}

## Objective

TODO: describe what needs to be done

## Context

TODO: any background, links, or constraints the agent should know about

## Agreed Plan

*(To be filled in after planning discussion)*

## Progress Log

*(Steps will appear here once the plan is agreed)*

## Summary

*(Fill in on completion — then remove Agreed Plan and Progress Log above.
Cover: key decisions made, patterns established, files changed, gotchas,
and anything a future agent working in this repo should know.)*
"""
    task_path.write_text(body)
    return task_path


def ensure_tasks_dir(repo: Path) -> None:
    """Create .hatchery/ and add a README so its purpose is clear."""
    hatchery_dir = repo / ".hatchery"
    logger.debug("Ensuring .hatchery dir at %s", hatchery_dir)
    hatchery_dir.mkdir(exist_ok=True)
    tasks_dir = hatchery_dir / "tasks"
    tasks_dir.mkdir(exist_ok=True)
    readme = hatchery_dir / "README.md"
    if not readme.exists():
        readme.write_text("""\
# .hatchery

Each file under `tasks/` is a record of a completed task, written by the agent
that performed the work. Named `YYYY-MM-DD-<task-name>.md`.

Future agents should browse these files for context on past decisions,
patterns, and gotchas before starting new work.
""")
        ui.info("  Created .hatchery/README.md")


def ensure_gitignore(repo: Path) -> None:
    """Make sure .hatchery/worktrees/ is gitignored.

    Without this, git status in the main repo will show every file inside every
    worktree as untracked, which is very noisy.
    """
    gitignore = repo / ".gitignore"
    entry = str(WORKTREES_SUBDIR) + "/"

    if gitignore.exists():
        lines = gitignore.read_text().splitlines()
        if any(line.strip() == entry for line in lines):
            logger.debug("gitignore entry '%s' already present", entry)
            return  # already present
        content = gitignore.read_text()
        sep = "" if content.endswith("\n") else "\n"
        gitignore.write_text(content + sep + entry + "\n")
    else:
        gitignore.write_text(entry + "\n")

    ui.info(f"  Added {entry} to .gitignore")


def open_for_editing(path: Path) -> None:
    """Open a file for the user to edit, then wait until they are done.

    If $EDITOR is set it is launched directly and expected to block until the
    user is done (e.g. EDITOR=emacsclient, EDITOR=vim, EDITOR=nano).
    If $EDITOR is not set, fall back to the OS default opener (non-blocking)
    and prompt for Enter.
    """
    editor = os.environ.get("EDITOR")
    if editor:
        subprocess.run([editor, str(path)])
    else:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)])
        else:
            subprocess.run(["xdg-open", str(path)])
        input(f"Edit {path.name}, then press Enter to continue...")
