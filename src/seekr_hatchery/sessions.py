"""Session I/O, schema migration, filesystem scaffolding, and lifecycle.

Cross-module constants live in :mod:`seekr_hatchery.constants`; generic
subprocess / editor / naming utilities live in :mod:`seekr_hatchery.utils`.
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import seekr_hatchery.constants as constants
import seekr_hatchery.docker as docker
import seekr_hatchery.git as git
import seekr_hatchery.ui as ui
from seekr_hatchery.constants import (
    CONTAINER_INCLUDES_ROOT,
    CONTAINER_REPO_ROOT,
    DEFAULT_BASE,
    DOCKER_CONFIG,
    WORKTREES_SUBDIR,
)
from seekr_hatchery.includes import IncludeEntry, load_include_entries, serialize_include_entries
from seekr_hatchery.models import SCHEMA_VERSION, SessionMeta
from seekr_hatchery.utils import open_for_editing, run, unique_basename

if TYPE_CHECKING:
    from seekr_hatchery.agents.agent_backend import AgentBackend
    from seekr_hatchery.docker import Runtime

logger = logging.getLogger("hatchery")

__all__ = [
    "IncludeEntry",
    "SessionMeta",
    "load_include_entries",
    "open_for_editing",
    "run",
    "serialize_include_entries",
    "unique_basename",
]


# ---------------------------------------------------------------------------
# Module-local constants
# ---------------------------------------------------------------------------

_TASKS_DB_DIR = constants.HATCHERY_DIR / "tasks"
_DB_SCHEMA_VERSION = 1

# Appended to the agent's default system prompt (preserving its built-in
# tool knowledge and workspace awareness). Edit here — single source of truth.
_SESSION_SYSTEM = """\
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

_WRAP_UP_PROMPT = (
    "The user has indicated they believe this task is complete. "
    "Please review the task file and assess whether the work is actually done. "
    "If it is complete, update the task file: set **Status** to `complete`, "
    "fill in the `## Summary` section documenting key decisions, patterns "
    "established, gotchas, and anything a future agent should know, then remove "
    "the `## Agreed Plan` and `## Progress Log` sections — they are working "
    "scaffolding, not permanent record. The final file should read as a clean "
    "ADR: Status/Branch/Created → Objective → Context → Summary. "
    "If work remains unfinished, tell the user what is still outstanding and ask "
    "whether they want to continue working or close the task out anyway."
)


def sandbox_context(
    name: str,
    branch: str,
    worktree: Path,
    repo: Path,
    main_branch: str,
    use_docker: bool,
    no_worktree: bool = False,
    include_paths: list[IncludeEntry] | None = None,
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
        lines.append("")
        lines.append("**Included paths:**")
        used_basenames: set[str] = set()
        for entry in include_paths:
            inc = entry.path
            is_git = (inc / ".git").exists()
            if use_docker:
                basename = unique_basename(inc.name, used_basenames)
                used_basenames.add(basename)
                container_inc = f"{CONTAINER_INCLUDES_ROOT}/{basename}"
                if entry.mode == "worktree" and is_git and not no_worktree:
                    container_inc_wt = f"{container_inc}/.hatchery/worktrees/{name}"
                    lines.append(f"- `{container_inc}/` — git repo (read-write); your worktree: `{container_inc_wt}/`")
                else:
                    access_label = "read-only" if entry.mode == "ro" else "read-write"
                    kind = "git repo" if is_git else "directory"
                    lines.append(f"- `{container_inc}/` — {kind} ({access_label})")
            else:
                # Native mode: report host paths
                if entry.mode == "worktree" and is_git and not no_worktree:
                    wt = inc / WORKTREES_SUBDIR / name
                    lines.append(f"- `{wt}/` — git repo worktree (host path, read-write)")
                else:
                    access_label = "read-only" if entry.mode == "ro" else "read-write"
                    kind = "git repo" if is_git else "directory"
                    lines.append(f"- `{inc}/` — {kind} (host path, {access_label})")

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


def _task_dir(repo: Path, name: str) -> Path:
    """Unified directory for all per-task state (metadata + session files)."""
    return _TASKS_DB_DIR / repo_id(repo) / name


def task_db_path(repo: Path, name: str) -> Path:
    return _task_dir(repo, name) / "meta.json"


def task_session_dir(repo: Path, name: str) -> Path:
    """Session state lives in the same unified task directory."""
    return _task_dir(repo, name)


def worktrees_dir(repo: Path) -> Path:
    """Worktrees live inside the repo under .hatchery/worktrees, which is gitignored."""
    return repo / WORKTREES_SUBDIR


def _db_meta_path() -> Path:
    """Path to the DB-level schema version file: ~/.hatchery/meta.json"""
    return constants.HATCHERY_DIR / "meta.json"


def migrate_db() -> None:
    """Run the DB-level migration chain. Called at CLI startup.

    Reads ~/.hatchery/meta.json (or assumes v0 if absent), runs each
    migration block in order, then writes the updated version.
    """
    meta_path = _db_meta_path()
    if meta_path.exists():
        try:
            v = json.loads(meta_path.read_text()).get("schema_version", 0)
        except (json.JSONDecodeError, KeyError):
            v = 0
    else:
        v = 0

    if v >= _DB_SCHEMA_VERSION:
        return  # nothing to do

    # v0 → v1: promote scoped <name>.json → unified <name>/meta.json
    # Flat tasks/<name>.json files (oldest format) are left in place — they
    # are lazily migrated by load_task() on demand.
    if v == 0:
        if _TASKS_DB_DIR.exists():
            for repo_subdir in _TASKS_DB_DIR.iterdir():
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


def _migrate(meta: dict) -> dict:
    """Bring a task dict up to the current schema version.

    Add a new ``if v == N`` block here whenever the schema changes.
    Each block should make the minimal edit to reach version N+1,
    then increment meta["schema_version"]. The final state will
    always be SCHEMA_VERSION.

    Contract with ``SessionMeta`` (which uses ``extra="forbid"``):
    every field that appears in real-world meta.json files must be
    either declared on the model **or** stripped/translated here
    before validation. If a future schema version retires a field,
    this is where the dict must drop it. Missing fields are fine —
    Pydantic field defaults fill them in at validation time.

    Forward-version guard: if meta.json was written by a *newer*
    hatchery than this one knows how to read, exit with a clear
    error before Pydantic sees the dict. The alternative — letting
    SessionMeta's ``extra="forbid"`` raise a ValidationError — would
    surface as a confusing Pydantic stack trace; this gives the user
    an actionable "please upgrade" message instead.
    """
    v = meta.get("schema_version", 0)

    if v > SCHEMA_VERSION:
        ui.error(
            f"session metadata was written by a newer hatchery "
            f"(schema v{v}); this version supports up to v{SCHEMA_VERSION}. "
            f"Please upgrade hatchery."
        )
        sys.exit(1)

    # v0 -> v1: initial versioned schema (just stamps the version)
    if v == 0:
        meta["schema_version"] = 1
        v = 1

    return meta


def load_task(repo: Path, name: str) -> dict:
    path = task_db_path(repo, name)
    logger.debug("Loading task metadata from %s", path)
    if path.exists():
        return _migrate(json.loads(path.read_text()))
    ui.error(f"task '{name}' not found.")
    sys.exit(1)


def save_task(meta: dict) -> None:
    path = task_db_path(Path(meta["repo"]), meta["name"])
    path.parent.mkdir(parents=True, exist_ok=True)
    meta["schema_version"] = SCHEMA_VERSION
    logger.debug("Saving task metadata to %s", path)
    # sort_keys=True so the on-disk JSON is deterministic regardless of how
    # the meta dict (or SessionMeta) was constructed. Existing meta.json
    # files will reorder alphabetically on next save; semantically identical.
    path.write_text(json.dumps(meta, indent=2, sort_keys=True))


def load(repo: Path, name: str) -> SessionMeta:
    """Load and validate session metadata as a SessionMeta instance.

    Runs the migration chain on the raw dict before validation so legacy
    fields are normalised. Exits the process if the file is missing.
    """
    return SessionMeta.model_validate(load_task(repo, name))


def save(meta: SessionMeta) -> None:
    """Persist a SessionMeta to disk.

    Uses ``exclude_none=True`` so fields like ``completed=None`` aren't
    written until they're actually set — matches the dict-based behaviour
    where keys were only added when assigned.
    """
    save_task(meta.model_dump(mode="json", exclude_none=True))


# ---------------------------------------------------------------------------
# Session-scoped tokens (moved from docker.py — they live on the session, not
# on the container runtime).
# ---------------------------------------------------------------------------


def get_or_create_proxy_token(repo: Path, name: str) -> str:
    """Return the stable API proxy token for this session, creating it on first call.

    The token is persisted in the session directory so it stays constant across
    container restarts. A stable token means the agent's cached credential in
    the per-task config directory continues to match the API key env var on
    subsequent launches — no repeated dialogs.
    """
    session_dir = task_session_dir(repo, name)
    session_dir.mkdir(parents=True, exist_ok=True)
    token_file = session_dir / "proxy_token"
    if token_file.exists():
        token = token_file.read_text().strip()
        logger.debug("Reusing proxy token for session %r", name)
        return token
    token = str(uuid.uuid4())
    token_file.write_text(token)
    logger.debug("Created proxy token for session %r", name)
    return token


def get_or_create_kubectl_token(session_dir: Path) -> str:
    """Return the stable kubectl RBAC proxy token, creating it on first call."""
    token_file = session_dir / "kubectl_proxy_token"
    if token_file.exists():
        return token_file.read_text().strip()
    token = str(uuid.uuid4())
    token_file.write_text(token)
    return token


# ---------------------------------------------------------------------------
# Container / image naming (moved from docker.py — derived from session
# identity, not docker-specific behaviour).
# ---------------------------------------------------------------------------


def image_name(repo: Path, name: str) -> str:
    """Return the container image tag for a given repo and session name."""
    return f"hatchery/{to_name(repo.name)}:{name}"


def container_name(repo: Path, name: str) -> str:
    """Return the deterministic container name for a session.

    Uses repo_id (basename + path hash) rather than bare basename to avoid
    collisions between repos with the same directory name at different paths.
    """
    return f"hatchery-{repo_id(repo)}-{name}"


# ---------------------------------------------------------------------------
# Launch-path helpers (moved from cli.py — these are session lifecycle
# concerns, not CLI parsing/UI concerns).
# ---------------------------------------------------------------------------


class SessionCancelled(Exception):
    """Raised by lifecycle functions when the user aborts mid-flow.

    CLI wrappers catch this and exit cleanly without a traceback. Useful for
    editor cancels, declined confirmation prompts, etc., where lifecycle code
    needs to bail without printing its own error.
    """


def is_task_complete(task_file_content: str) -> bool:
    """True iff the task file's front-matter status is ``complete``."""
    return bool(re.search(r"^\*\*Status\*\*:\s*complete", task_file_content, re.MULTILINE))


def set_status(repo: Path, name: str, status: str) -> None:
    """Update the persisted status field for a session in-place.

    Uses the dict-based load/save path because the launch hot path flips
    ``in-progress`` ↔ ``running`` on every start/exit; a single-field update
    doesn't need full model validation.
    """
    meta = load_task(repo, name)
    meta["status"] = status
    save_task(meta)


def session_env(name: str, repo: Path) -> dict[str, str]:
    """Env vars that identify the hatchery session to child processes."""
    return {**os.environ, "HATCHERY_TASK": name, "HATCHERY_REPO": str(repo)}


def docker_context(
    runtime,  # docker.Runtime | None — typed at call site to avoid cycle
    worktree: Path | None,
    repo: Path,
):
    """Return (config, features, container_workdir) for the launch path.

    Pass ``worktree=None`` for no-worktree mode; the container path becomes
    ``/workspace``. When ``runtime`` is None we're in native mode and the
    workdir field is unused.
    """
    config = docker.load_docker_config(worktree or repo) if runtime else None
    features = docker.docker_features(config) if config else []
    if runtime:
        container_workdir = (
            "/workspace" if worktree is None else f"{CONTAINER_REPO_ROOT}/{worktree.relative_to(repo)}"
        )
    else:
        container_workdir = ""
    return config, features, container_workdir


def next_chat_name(repo: Path) -> str:
    """Return the lowest available chat-N name for this repo."""
    used: set[int] = set()
    for meta in repo_tasks_for_current_repo(repo):
        if meta.get("type") == "chat":
            name = meta.get("name", "")
            if name.startswith("chat-") and name[5:].isdigit():
                used.add(int(name[5:]))
    n = 1
    while n in used:
        n += 1
    return f"chat-{n}"


def merge_includes_with_config(
    cli_entries: list[IncludeEntry],
    config_includes: list,
    repo: Path,
) -> list[IncludeEntry]:
    """Merge already-constructed CLI include entries with docker.yaml entries.

    CLI entries take priority. For each ``config_includes`` raw entry
    (``str`` or ``IncludeItem`` dict from docker.yaml):
    - resolve the path against *repo* if relative,
    - skip if the path doesn't exist (with a warning),
    - if a CLI entry already covers the same path, keep the CLI mode and
      ``ui.note`` the override,
    - otherwise append as a new entry with the config-declared mode.

    Caller is responsible for translating click's tuple-shaped flag input
    into IncludeEntry objects — the CLI shape (3 separate tuples grouped
    by mode) lives in cli.py.
    """
    seen: set[Path] = {e.path for e in cli_entries}
    result = list(cli_entries)

    for entry in config_includes:
        path_str, config_mode = docker.parse_docker_include_entry(entry)
        raw = Path(path_str)
        resolved = (repo / raw).resolve() if not raw.is_absolute() else raw.resolve()
        if not resolved.exists():
            ui.warn(f"docker.yaml include path does not exist, skipping: {path_str}")
            continue
        if resolved in seen:
            existing = next(e for e in result if e.path == resolved)
            if existing.mode != config_mode:
                ui.note(
                    f"--include* flag overrides docker.yaml mode for {path_str}: "
                    f"{config_mode!r} → {existing.mode!r}"
                )
        else:
            seen.add(resolved)
            result.append(IncludeEntry(path=resolved, mode=config_mode))

    return result


def merge_include_updates(
    current_entries: list[IncludeEntry],
    updates: list[IncludeEntry],
    meta: SessionMeta,
) -> list[IncludeEntry]:
    """Merge resume-time --include* flags into the existing entry list.

    For each update:
    - If a matching path already exists, replace its mode (creating or removing
      worktrees as needed).
    - Otherwise, append as a new entry.

    Persists ``meta.include`` to meta.json before returning.
    """
    if not updates:
        return current_entries

    by_path = {e.path: e for e in current_entries}

    for update in updates:
        existing = by_path.get(update.path)
        if existing is None:
            # New path — create worktree if needed (base resolved per-repo).
            git.create_include_worktrees([update], meta.name)
            by_path[update.path] = update
        elif existing.mode == update.mode:
            pass  # no-op
        else:
            if not existing.is_reference() and update.is_reference():
                ui.info(f"include mode {existing.mode!r} → {update.mode!r} for {update.path}; removing worktree.")
                # Pass existing (mode="worktree") so remove_include_worktrees acts on it.
                git.remove_include_worktrees([existing], meta.name)
            elif existing.is_reference() and not update.is_reference():
                ui.info(f"include mode {existing.mode!r} → {update.mode!r} for {update.path}; creating worktree.")
                git.create_include_worktrees([update], meta.name)
            by_path[update.path] = update

    # Preserve original ordering, appending new entries at the end.
    original_paths = [e.path for e in current_entries]
    original_path_set = {e.path for e in current_entries}
    new_paths = [e.path for e in updates if e.path not in original_path_set]
    result = [by_path[p] for p in original_paths] + [by_path[p] for p in new_paths]

    meta.include = serialize_include_entries(result)
    save(meta)
    return result


# ---------------------------------------------------------------------------
# Lifecycle: private helpers
# ---------------------------------------------------------------------------


def _final_commit(worktree: Path, message: str) -> None:
    """Stage everything in *worktree* and commit with *message*."""
    run(["git", "add", "-A"], cwd=worktree)
    run(["git", "commit", "-m", message], cwd=worktree)


def _check_not_in_progress(repo: Path, name: str, *, label: str = "session") -> None:
    """Exit if a session with this name is currently in-progress/running."""
    path = task_db_path(repo, name)
    if not path.exists():
        return
    try:
        existing = json.loads(path.read_text())
    except json.JSONDecodeError:
        return
    if existing.get("status") in ("in-progress", "running"):
        ui.error(f"{label} '{name}' is already in-progress. Choose a different name or resume it.")
        sys.exit(1)


def _commit_docker_files(backend: "AgentBackend", worktree: Path) -> None:
    """Stage and commit any newly created Docker scaffolding files."""
    ui.info("  Committing...")
    run(
        [
            "git",
            "add",
            str(docker.dockerfile_path(worktree, backend).relative_to(worktree)),
            str(DOCKER_CONFIG),
        ],
        cwd=worktree,
    )
    run(
        ["git", "commit", "-m", "chore: add hatchery Docker configuration"],
        cwd=worktree,
    )


# ---------------------------------------------------------------------------
# Lifecycle: public API
# ---------------------------------------------------------------------------


def mark_done(meta: SessionMeta, *, commit_changes: bool = False) -> None:
    """Mark a session complete: optionally commit, remove worktree, remove
    include worktrees, set status=complete + completed=now, persist meta.

    Interactive prompts (the commit confirmation, the Summary warning)
    live in the CLI; this function takes a boolean.
    """
    include_repos = load_include_entries({"include": meta.include})

    if not meta.no_worktree:
        if meta.worktree_path.exists():
            if commit_changes:
                _final_commit(meta.worktree_path, f"task({meta.name}): final checkpoint")
            git.remove_worktree(meta.repo_path, meta.worktree_path)
            ui.info(f"Worktree removed: {meta.worktree_path}")
        else:
            ui.info(f"Worktree already gone: {meta.worktree_path}")

        if include_repos:
            git.remove_include_worktrees(include_repos, meta.name)

    meta.status = "complete"
    meta.completed = datetime.now().isoformat()
    save(meta)

    if not meta.no_worktree:
        ui.info(f"Branch retained: {meta.branch}")
    ui.success(f"Task '{meta.name}' marked complete.")


def archive(meta: SessionMeta, *, commit_changes: bool = False) -> None:
    """Park a session: remove worktree, keep branch + meta around for resume.

    For no_worktree sessions, only flips status. Interactive prompts (the
    checkpoint-before-archive confirmation) stay in the CLI.
    """
    include_repos = load_include_entries({"include": meta.include})

    if meta.no_worktree:
        ui.note(f"Task '{meta.name}': no-worktree task — no worktree or branch to remove.")
    else:
        if meta.worktree_path.exists():
            if commit_changes:
                _final_commit(meta.worktree_path, f"task({meta.name}): checkpoint before archive")
            git.remove_worktree(meta.repo_path, meta.worktree_path)
            ui.info(f"Worktree removed: {meta.worktree_path}")
        else:
            ui.info(f"Task '{meta.name}': worktree not found (may already be removed).")
        if include_repos:
            git.remove_include_worktrees(include_repos, meta.name)
        ui.info(f"Branch retained: {meta.branch}")

    meta.status = "archived"
    save(meta)
    ui.success(f"Task '{meta.name}' archived. Resume with: hatchery resume {meta.name}")


def delete(meta: SessionMeta) -> None:
    """Permanently delete: remove worktree (force), delete branch, remove
    include worktrees and branches, unlink meta.json.

    Interactive confirmation prompts stay in the CLI.
    """
    include_repos = load_include_entries({"include": meta.include})

    if not meta.no_worktree:
        if meta.worktree_path.exists():
            git.remove_worktree(meta.repo_path, meta.worktree_path, force=True)
        if git.delete_branch(meta.repo_path, meta.branch):
            ui.info(f"Branch deleted: {meta.branch}")
        else:
            ui.info(f"Could not delete branch {meta.branch} (may already be gone)")

        if include_repos:
            git.remove_include_worktrees(include_repos, meta.name)
            git.delete_include_branches(include_repos, meta.name)

    task_db_path(meta.repo_path, meta.name).unlink(missing_ok=True)
    ui.success(f"Task '{meta.name}' deleted.")


def create(
    *,
    name: str,
    repo: Path,
    type: Literal["task", "chat"],
    backend: "AgentBackend",
    base: str | None = None,
    no_worktree: bool = False,
    no_commit: bool = False,
    no_commit_docker: bool = False,
    no_docker: bool = False,
    in_repo: bool = True,
    include_entries: list[IncludeEntry] | None = None,
    objective: str | None = None,
    use_editor: bool = False,
) -> SessionMeta:
    """Create a new session end-to-end and persist it.

    Sets up the worktree + branch (tasks only, when not no_worktree), include
    worktrees, Docker scaffolding (Dockerfile + docker.yaml), optionally the
    task markdown file, and writes meta.json. Returns the persisted
    ``SessionMeta``.

    For ``type="chat"``: no worktree/branch, no task file. ``no_worktree`` is
    forced to True; ``worktree`` in the meta points at ``repo``.

    Raises ``SessionCancelled`` if ``use_editor`` is True and the user saves
    the task file unchanged — the worktree, branch, and include worktrees
    are rolled back first.

    Interactive prompts (the objective prompt, the post-exit menu) do NOT
    live in this function — pass ``objective`` as a string for the
    non-editor path, or call with ``use_editor=True`` to launch the
    editor on the freshly written template.
    """
    include_entries = list(include_entries or [])
    is_chat = type == "chat"
    session_id = str(uuid.uuid4())

    if is_chat:
        no_worktree = True
        worktree = repo
        branch = ""
    else:
        ensure_tasks_dir(repo)
        if in_repo:
            ensure_gitignore(repo)

    _check_not_in_progress(repo, name, label="chat" if is_chat else "task")

    # Tracked for KeyboardInterrupt + editor-cancel rollback.
    cleanup_worktree: Path | None = None
    cleanup_branch: str | None = None
    cleanup_includes: list[IncludeEntry] = []

    try:
        if is_chat:
            df_created = docker.ensure_dockerfile(repo, backend)
            dc_created = docker.ensure_docker_config(repo)
            if in_repo and not no_commit and (df_created or dc_created):
                _commit_docker_files(backend, repo)
        else:
            if no_worktree:
                worktree = repo
                branch = ""
                ui.info(f"Creating task: {name}")
            else:
                branch = f"hatchery/{name}"
                worktree = worktrees_dir(repo) / name
                ui.info(f"Creating task: {name}")
                resolved_base = base or DEFAULT_BASE
                if resolved_base == DEFAULT_BASE and in_repo:
                    default = git.get_default_branch(repo)
                    fetch_result = run(["git", "fetch", "origin"], cwd=repo, check=False)
                    if fetch_result.returncode == 0:
                        resolved_base = f"origin/{default}"
                    else:
                        logger.debug("git fetch origin failed; using local %s as base", resolved_base)
                elif in_repo:
                    git._fetch_if_remote(resolved_base, repo)
                git.create_worktree(repo, branch, worktree, resolved_base)
                cleanup_worktree = worktree
                cleanup_branch = branch

            if include_entries:
                git.create_include_worktrees(include_entries, name)
                cleanup_includes = list(include_entries)

            if in_repo:
                if no_commit_docker or no_commit:
                    docker.ensure_docker_files_uncommitted(repo, worktree, backend)
                    df_created = dc_created = False
                else:
                    df_created = docker.ensure_dockerfile(worktree, backend, source=repo)
                    dc_created = docker.ensure_docker_config(worktree, source=repo)
                if df_created or dc_created:
                    _commit_docker_files(backend, worktree)
            elif not no_docker:
                docker.ensure_dockerfile(worktree, backend)
                docker.ensure_docker_config(worktree)

            # Re-read docker.yaml from the worktree; new include entries get materialised.
            post_config = docker.load_docker_config(worktree)
            post_include_paths = {x.path for x in include_entries}
            new_entries: list[IncludeEntry] = []
            for raw_entry in post_config.include:
                path_str, mode = docker.parse_docker_include_entry(raw_entry)
                resolved = (worktree / Path(path_str)).resolve() if not Path(path_str).is_absolute() else Path(path_str).resolve()
                if resolved not in post_include_paths and resolved.exists():
                    new_entries.append(IncludeEntry(path=resolved, mode=mode))
                    post_include_paths.add(resolved)
            if new_entries:
                git.create_include_worktrees(new_entries, name)
                include_entries = include_entries + new_entries
                cleanup_includes = list(include_entries)

            if use_editor:
                task_path = write_task_file(worktree, name, branch)
                content_before = task_path.read_text()
                ui.info("Opening task file for editing...")
                open_for_editing(task_path)
                if task_path.read_text() == content_before:
                    ui.warn("Task file unchanged — cancelled.")
                    if cleanup_worktree is not None:
                        git.remove_worktree(repo, cleanup_worktree)
                        if cleanup_branch:
                            git.delete_branch(repo, cleanup_branch)
                    if cleanup_includes:
                        git.remove_include_worktrees(cleanup_includes, name)
                        git.delete_include_branches(cleanup_includes, name)
                    raise SessionCancelled()
            else:
                write_task_file(worktree, name, branch, objective=objective)

            if in_repo and not no_commit:
                run(["git", "add", ".hatchery/tasks/"], cwd=worktree)
                run(["git", "commit", "-m", f"task({name}): add task file"], cwd=worktree)
    except KeyboardInterrupt:
        if cleanup_worktree is not None:
            git.remove_worktree(repo, cleanup_worktree)
            if cleanup_branch:
                git.delete_branch(repo, cleanup_branch)
        if cleanup_includes:
            git.remove_include_worktrees(cleanup_includes, name)
            git.delete_include_branches(cleanup_includes, name)
        raise

    meta = SessionMeta(
        name=name,
        repo=str(repo),
        worktree=str(worktree),
        type=type,
        status="in-progress",
        branch=branch,
        created=datetime.now().isoformat(),
        session_id=session_id,
        no_worktree=no_worktree,
        no_commit=no_commit,
        agent=backend.kind,
        include=serialize_include_entries(include_entries),
    )
    # save_task (dict path) preserves the on-disk shape callers compared
    # against in PR1's tests. SessionMeta.model_dump(exclude_none=True)
    # drops completed=None / session_id=None when unset — matches the
    # pre-refactor behaviour.
    save_task(meta.model_dump(mode="json", exclude_none=True))
    return meta


def launch(
    meta: SessionMeta,
    *,
    kind: Literal["new", "resume", "finalize"],
    backend: "AgentBackend",
    runtime: "Runtime | None",
    main_branch: str,
    session_id: str,
    no_cache: bool = False,
    include_repos: list[IncludeEntry] | None = None,
) -> list[str]:
    """Launch an agent session: build the agent command, run it (inside the
    container if a runtime is given, natively otherwise), and bracket the
    run with the ``running`` ↔ ``in-progress`` status flip.

    Returns the docker feature list active at launch time so callers can
    use it in their post-exit banner. Does NOT prompt the user after the
    agent exits — post-exit interaction lives in the CLI layer to keep
    the sessions ↔ cli layering one-way.

    The status flip is unconditional: any prior ``complete`` or
    ``archived`` status is rewritten to ``running`` for the duration of
    the launch and ``in-progress`` after exit. This is intentional —
    reviving completed or archived tasks is a supported flow (cmd_resume
    handles both, sometimes tasks get marked complete by accident). The
    caller is responsible for the launchable-state decision; launch()
    just runs the agent.
    """
    include_repos = include_repos or []
    is_chat = meta.is_chat

    if kind == "new":
        backend.on_new_task(meta.session_dir)
    if kind in ("new", "resume"):
        backend.on_before_launch(meta.worktree_path)

    if is_chat:
        system_prompt = ""
        initial_prompt = ""
    else:
        env_ctx = sandbox_context(
            meta.name,
            meta.branch,
            meta.worktree_path,
            meta.repo_path,
            main_branch,
            bool(runtime),
            meta.no_worktree,
            include_paths=include_repos,
        )
        system_prompt = _SESSION_SYSTEM + "\n" + env_ctx
        initial_prompt = "" if kind == "finalize" else session_prompt(meta.name, meta.worktree_path)

    config, features, container_workdir = docker_context(
        runtime, None if meta.no_worktree else meta.worktree_path, meta.repo_path
    )

    docker_flag = bool(runtime)
    if kind == "new":
        agent_cmd = backend.build_new_command(
            session_id, system_prompt, initial_prompt, docker=docker_flag, workdir=container_workdir
        )
    elif kind == "resume":
        agent_cmd = backend.build_resume_command(
            session_id, system_prompt, initial_prompt, docker=docker_flag, workdir=container_workdir
        )
    else:  # finalize
        agent_cmd = backend.build_finalize_command(
            session_id, system_prompt, _WRAP_UP_PROMPT, docker=docker_flag, workdir=container_workdir
        )

    if is_chat:
        ui.chat_banner(meta.name, meta.repo_path, features=features)
    else:
        ui.banner(
            meta.name, meta.repo_path,
            branch=meta.branch, sandbox=bool(runtime),
            worktree=not meta.no_worktree, features=features,
        )

    set_status(meta.repo_path, meta.name, "running")
    try:
        if runtime:
            # Tokens are resolved here (filesystem side-effects belong to
            # sessions, not docker). Pure identifiers (image_name,
            # container_name, session_dir) ride along on the SessionMeta —
            # docker accesses them through meta properties.
            proxy_token = get_or_create_proxy_token(meta.repo_path, meta.name)
            kubectl_proxy_token = (
                get_or_create_kubectl_token(meta.session_dir) if config and config.kubernetes else None
            )
            docker.run_session(
                meta, backend, agent_cmd, config,
                proxy_token=proxy_token,
                kubectl_proxy_token=kubectl_proxy_token,
                runtime=runtime,
                no_cache=no_cache,
                include_entries=include_repos,
            )
        else:
            os.chdir(meta.worktree_path)
            subprocess.run(agent_cmd, env=session_env(meta.name, meta.repo_path))
    finally:
        set_status(meta.repo_path, meta.name, "in-progress")

    return features


def repo_tasks_for_current_repo(repo: Path) -> list[dict]:
    """Return all tasks belonging to the given repo, newest first."""
    if not _TASKS_DB_DIR.exists():
        return []
    repo_str = str(repo)
    found: list[dict] = []

    subdir = _TASKS_DB_DIR / repo_id(repo)
    if subdir.exists():
        # New unified dirs: tasks/<repo-id>/<name>/meta.json
        for f in subdir.glob("*/meta.json"):
            try:
                meta = json.loads(f.read_text())
                found.append(meta)
            except (json.JSONDecodeError, KeyError):
                continue

    # Flat files (pre-migration fallback)
    for f in _TASKS_DB_DIR.glob("*.json"):
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
