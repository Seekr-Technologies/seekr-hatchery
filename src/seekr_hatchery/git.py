"""Git helpers: worktree management, branch operations, repo discovery."""

import logging
import shutil
import subprocess
import sys
from pathlib import Path

import seekr_hatchery.sessions as sessions
import seekr_hatchery.ui as ui
from seekr_hatchery.includes import IncludeEntry

logger = logging.getLogger("hatchery")


def _resolve_main_repo(repo: Path) -> Path:
    """If repo is a git linked worktree, resolve to the main repository root.

    In a linked worktree, .git is a file pointing to the worktree-specific
    metadata inside the main repo's .git/worktrees/<name>/.  Git objects,
    refs, and all shared state live in the main repo's .git/.  Returning the
    main repo root ensures callers always work with a .git directory rather
    than a .git file, so Docker bind-mount paths like .git/objects resolve
    correctly.
    """
    git_path = repo / ".git"
    if not git_path.is_file():
        return repo  # normal checkout — nothing to resolve
    # Linked worktree: find the common git dir (main repo's .git).
    result = sessions.run(["git", "rev-parse", "--git-common-dir"], cwd=repo, check=False)
    if result.returncode != 0:
        ui.error("could not resolve git common directory for linked worktree.")
        sys.exit(1)
    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (repo / common_dir).resolve()
    # common_dir is e.g. /path/to/main-repo/.git; parent is the main repo root.
    return common_dir.parent


def git_root() -> Path:
    """Return the root of the current git repository, or exit with an error."""
    result = sessions.run(["git", "rev-parse", "--show-toplevel"], check=False)
    if result.returncode != 0:
        ui.error("not inside a git repository.")
        sys.exit(1)
    return _resolve_main_repo(Path(result.stdout.strip()))


def git_root_or_cwd() -> tuple[Path, bool]:
    """Return (root, True) if in a git repo, else (Path.cwd(), False)."""
    result = sessions.run(["git", "rev-parse", "--show-toplevel"], check=False)
    if result.returncode == 0:
        return _resolve_main_repo(Path(result.stdout.strip())), True
    return Path.cwd(), False


def create_worktree(repo: Path, branch: str, worktree: Path, base: str) -> None:
    """Create a git worktree on *branch* (force-reset to *base*).

    Removes any stale worktree registration for *worktree* first so that a
    previous failed run doesn't block re-creation.  Exits cleanly with an
    informative message if *base* does not exist in *repo*.
    """
    worktree.parent.mkdir(parents=True, exist_ok=True)
    sessions.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=repo, check=False)
    try:
        sessions.run(["git", "worktree", "add", "-B", branch, str(worktree), base], cwd=repo)
    except subprocess.CalledProcessError as e:
        if "invalid reference" in e.stderr:
            ui.error(f"base ref {base!r} does not exist in {repo}. Use --from <branch> to specify a valid ref.")
            sys.exit(1)
        raise
    logger.debug("Worktree created at %s", worktree)


def remove_worktree(repo: Path, worktree: Path, force: bool = False) -> None:
    """Remove a git worktree, falling back to manual cleanup if git can't."""
    flags = ["git", "worktree", "remove"]
    if force:
        flags.append("--force")
    result = sessions.run(flags + [str(worktree)], cwd=repo, check=False)
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
            sessions.run(["chmod", "-RN", str(worktree)], check=False)
            shutil.rmtree(worktree, ignore_errors=True)
    sessions.run(["git", "worktree", "prune"], cwd=repo, check=False)


def has_uncommitted_changes(cwd: Path) -> bool:
    """Return True if the working tree has any uncommitted changes."""
    result = sessions.run(["git", "status", "--porcelain"], cwd=cwd, check=False)
    return bool(result.stdout.strip())


def uncommitted_changes_summary(cwd: Path) -> str:
    """Return a short display of uncommitted changes: file list + diff-stat summary."""
    status = sessions.run(["git", "status", "--short"], cwd=cwd, check=False).stdout.strip()
    diff = sessions.run(["git", "diff", "HEAD", "--stat"], cwd=cwd, check=False).stdout.strip()
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
    result = sessions.run(["git", "branch", "-D", branch], cwd=repo, check=False)
    return result.returncode == 0


def _fetch_if_remote(ref: str, cwd: Path) -> None:
    """Fetch the remote that owns *ref*, if it resolves to a remote-tracking branch.

    Uses ``git rev-parse --symbolic-full-name`` to ask git whether *ref* is a
    remote-tracking branch (``refs/remotes/<remote>/...``).  If the ref is not
    yet known to git (e.g. ``origin/main`` before the first fetch), falls back
    to splitting on ``/`` and confirming the left-hand side is a configured
    remote via ``git remote get-url``.

    Non-remote refs (local branches, HEAD, bare SHAs) are silently skipped.
    """
    result = sessions.run(["git", "rev-parse", "--symbolic-full-name", ref], cwd=cwd, check=False)
    if result.returncode == 0:
        full_name = result.stdout.strip()
        if not full_name.startswith("refs/remotes/"):
            return
        remote = full_name.split("/")[2]
    else:
        # Ref not in the repo yet — fall back to name heuristic.
        if "/" not in ref:
            return
        remote = ref.split("/", 1)[0]
        r = sessions.run(["git", "remote", "get-url", remote], cwd=cwd, check=False)
        if r.returncode != 0:
            return
    fetch_result = sessions.run(["git", "fetch", remote], cwd=cwd, check=False)
    if fetch_result.returncode != 0:
        logger.warning("git fetch %s failed for %s", remote, cwd)


def create_include_worktrees(includes: list[IncludeEntry], name: str, base: str | None = None) -> None:
    """Create a hatchery/<name> worktree inside each included git repo with mode="worktree".

    Entries with mode="ro" or mode="rw" and non-git directories are silently skipped.

    When *base* is omitted, each repo's default branch is resolved and origin is
    fetched so the worktree starts from the latest upstream commit.  When *base*
    is supplied, ``_fetch_if_remote`` fetches the owning remote first if the ref
    is a remote-tracking branch (e.g. ``origin/main``).
    """
    branch = f"hatchery/{name}"
    for entry in includes:
        if entry.mode != "worktree":
            continue
        path = entry.path
        if (path / ".git").exists():
            worktree = path / sessions.WORKTREES_SUBDIR / name
            if base is not None:
                repo_base = base
                _fetch_if_remote(repo_base, path)
            else:
                default = get_default_branch(path)
                # Fetch so the worktree starts from the latest upstream commit.
                fetch_result = sessions.run(["git", "fetch", "origin"], cwd=path, check=False)
                if fetch_result.returncode != 0:
                    logger.warning("git fetch origin failed for %s; using local %s", path, default)
                    repo_base = default
                else:
                    repo_base = f"origin/{default}"
            create_worktree(path, branch, worktree, repo_base)
            logger.debug("Include worktree created at %s", worktree)


def remove_include_worktrees(includes: list[IncludeEntry], name: str) -> None:
    """Remove the hatchery/<name> worktree from included git repos with mode="worktree".

    Reference-mode entries (ro/rw), non-git directories, and missing worktrees
    are silently skipped.
    """
    for entry in includes:
        if entry.mode != "worktree":
            continue
        path = entry.path
        if (path / ".git").exists():
            worktree = path / sessions.WORKTREES_SUBDIR / name
            remove_worktree(path, worktree, force=True)


def delete_include_branches(includes: list[IncludeEntry], name: str) -> None:
    """Delete the hatchery/<name> branch from included git repos with mode="worktree".

    Reference-mode entries (ro/rw) and non-git directories are silently skipped.
    """
    branch = f"hatchery/{name}"
    for entry in includes:
        if entry.mode != "worktree":
            continue
        path = entry.path
        if (path / ".git").exists():
            delete_branch(path, branch)


def get_default_branch(repo: Path) -> str:
    """Return the repo's default branch name (main / master / etc.)."""
    # Prefer the remote's HEAD pointer — reliable on repos with a remote.
    result = sessions.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=repo, check=False)
    if result.returncode == 0:
        ref = result.stdout.strip()
        if "/" in ref:
            return ref.rsplit("/", 1)[-1]
    # Fallback: check whether common names exist as local branches.
    for candidate in ("main", "master", "develop"):
        r = sessions.run(["git", "rev-parse", "--verify", candidate], cwd=repo, check=False)
        if r.returncode == 0:
            return candidate
    return "main"  # safe last-resort default
