"""Git helpers: worktree management, branch operations, repo discovery."""

import logging
import shutil
import sys
from pathlib import Path

import seekr_hatchery.tasks as tasks
import seekr_hatchery.ui as ui

logger = logging.getLogger("hatchery")


def git_root() -> Path:
    """Return the root of the current git repository, or exit with an error."""
    result = tasks.run(["git", "rev-parse", "--show-toplevel"], check=False)
    if result.returncode != 0:
        ui.error("not inside a git repository.")
        sys.exit(1)
    return Path(result.stdout.strip())


def git_root_or_cwd() -> tuple[Path, bool]:
    """Return (root, True) if in a git repo, else (Path.cwd(), False)."""
    result = tasks.run(["git", "rev-parse", "--show-toplevel"], check=False)
    if result.returncode == 0:
        return Path(result.stdout.strip()), True
    return Path.cwd(), False


def create_worktree(repo: Path, branch: str, worktree: Path, base: str) -> None:
    """Create a git worktree on *branch* (force-reset to *base*).

    Removes any stale worktree registration for *worktree* first so that a
    previous failed run doesn't block re-creation.
    """
    worktree.parent.mkdir(parents=True, exist_ok=True)
    tasks.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=repo, check=False)
    tasks.run(["git", "worktree", "add", "-B", branch, str(worktree), base], cwd=repo)
    logger.debug("Worktree created at %s", worktree)


def remove_worktree(repo: Path, worktree: Path, force: bool = False) -> None:
    """Remove a git worktree, falling back to manual cleanup if git can't."""
    flags = ["git", "worktree", "remove"]
    if force:
        flags.append("--force")
    result = tasks.run(flags + [str(worktree)], cwd=repo, check=False)
    if result.returncode == 0:
        return
    # git couldn't remove it (directory gone or not registered as a worktree).
    # Clean up manually and prune the stale reference.
    logger.debug("git worktree remove failed (rc=%d), falling back to manual cleanup", result.returncode)
    if worktree.exists():
        try:
            shutil.rmtree(worktree)
        except PermissionError:
            # macOS ACLs (set by Docker's filesystem layer) can block rmdir even for
            # the owner. Strip ACLs and extended attributes, then retry.
            tasks.run(["chmod", "-RN", str(worktree)], check=False)
            shutil.rmtree(worktree, ignore_errors=True)
    tasks.run(["git", "worktree", "prune"], cwd=repo, check=False)


def has_uncommitted_changes(cwd: Path) -> bool:
    """Return True if the working tree has any uncommitted changes."""
    result = tasks.run(["git", "status", "--porcelain"], cwd=cwd, check=False)
    return bool(result.stdout.strip())


def uncommitted_changes_summary(cwd: Path) -> str:
    """Return a short display of uncommitted changes: file list + diff-stat summary."""
    status = tasks.run(["git", "status", "--short"], cwd=cwd, check=False).stdout.strip()
    diff = tasks.run(["git", "diff", "HEAD", "--stat"], cwd=cwd, check=False).stdout.strip()
    parts: list[str] = []
    if status:
        parts.append(status)
    if diff:
        summary_line = diff.splitlines()[-1].strip()
        if "changed" in summary_line:
            parts.append(f"  ({summary_line})")
    return "\n".join(parts)


def delete_branch(repo: Path, branch: str) -> bool:
    """Delete a local branch. Returns True if deleted, False if it didn't exist."""
    result = tasks.run(["git", "branch", "-D", branch], cwd=repo, check=False)
    return result.returncode == 0


def get_default_branch(repo: Path) -> str:
    """Return the repo's default branch name (main / master / etc.)."""
    # Prefer the remote's HEAD pointer — reliable on repos with a remote.
    result = tasks.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=repo, check=False)
    if result.returncode == 0:
        ref = result.stdout.strip()
        if "/" in ref:
            return ref.rsplit("/", 1)[-1]
    # Fallback: check whether common names exist as local branches.
    for candidate in ("main", "master", "develop"):
        r = tasks.run(["git", "rev-parse", "--verify", candidate], cwd=repo, check=False)
        if r.returncode == 0:
            return candidate
    return "main"  # safe last-resort default
