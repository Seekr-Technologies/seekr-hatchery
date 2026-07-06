"""Tests for git helper functions, especially worktree detection."""

from pathlib import Path

import pytest

import seekr_hatchery.constants as constants
import seekr_hatchery.git as git
import seekr_hatchery.utils as utils
from seekr_hatchery.includes import IncludeEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git_repo(path: Path) -> Path:
    """Init a real git repo with one commit at path. Returns path."""
    path.mkdir(exist_ok=True)
    utils.run(["git", "init", "--initial-branch=main", str(path)], check=False)
    utils.run(["git", "init", str(path)], check=False)
    (path / "README").write_text("hi")
    utils.run(["git", "add", "README"], cwd=path)
    utils.run(["git", "-c", "user.email=t@t.com", "-c", "user.name=T", "commit", "-m", "init"], cwd=path)
    return path


def _bare_remote(path: Path) -> Path:
    """Init a bare git repo suitable for use as a remote. Returns path."""
    path.mkdir(exist_ok=True)
    utils.run(["git", "init", "--bare", str(path)])
    # Ensure HEAD points to main regardless of git version's default.
    (path / "HEAD").write_text("ref: refs/heads/main\n")
    return path


def _entry(path: Path, mode: str = "worktree") -> IncludeEntry:
    return IncludeEntry(path=path, mode=mode)


# ---------------------------------------------------------------------------
# _resolve_main_repo
# ---------------------------------------------------------------------------


class TestResolveMainRepo:
    def test_normal_repo_unchanged(self, tmp_path):
        """When .git is a directory (normal checkout), repo is returned unchanged."""
        (tmp_path / ".git").mkdir()
        result = git._resolve_main_repo(tmp_path)
        assert result == tmp_path

    def test_worktree_resolves_to_main_repo(self, tmp_path):
        """When .git is a file (linked worktree), resolve to main repo root."""
        main_repo = tmp_path / "main_repo"
        _git_repo(main_repo)
        worktree = tmp_path / "my_worktree"
        utils.run(["git", "worktree", "add", "--detach", str(worktree)], cwd=main_repo)

        result = git._resolve_main_repo(worktree)
        assert result == main_repo

    def test_exits_on_git_command_failure(self, tmp_path):
        """.git file with a nonexistent gitdir — git fails, code exits."""
        (tmp_path / ".git").write_text("gitdir: /nonexistent/path/.git/worktrees/fake\n")
        with pytest.raises(SystemExit):
            git._resolve_main_repo(tmp_path)


# ---------------------------------------------------------------------------
# git_root_or_cwd
# ---------------------------------------------------------------------------


class TestGitRootOrCwdWorktree:
    def test_normal_repo_returns_toplevel(self, tmp_path, monkeypatch):
        _git_repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        path, in_repo = git.git_root_or_cwd()
        assert in_repo is True
        assert path == tmp_path

    def test_worktree_resolves_to_main_repo(self, tmp_path, monkeypatch):
        main_repo = tmp_path / "main"
        _git_repo(main_repo)
        worktree = tmp_path / "wt"
        utils.run(["git", "worktree", "add", "--detach", str(worktree)], cwd=main_repo)

        monkeypatch.chdir(worktree)
        path, in_repo = git.git_root_or_cwd()

        assert in_repo is True
        assert path == main_repo


# ---------------------------------------------------------------------------
# create_include_worktrees / remove_include_worktrees / delete_include_branches
# ---------------------------------------------------------------------------


class TestIncludeWorktreeHelpers:
    """Tests for create/remove/delete_include_worktrees with real git repos."""

    def test_create_skips_non_git_dir(self, tmp_path):
        """A plain directory with no .git is silently skipped."""
        plain = tmp_path / "plain"
        plain.mkdir()
        git.create_include_worktrees([_entry(plain)], "my-task", "HEAD")
        assert not (plain / constants.WORKTREES_SUBDIR).exists()

    def test_create_skips_reference_mode_entries(self, tmp_path):
        """reference mode entries (ro/rw) are skipped even if they are git repos."""
        repo = _git_repo(tmp_path / "repo-b")
        git.create_include_worktrees([_entry(repo, mode="ro")], "my-task", "HEAD")
        git.create_include_worktrees([_entry(repo, mode="rw")], "my-task", "HEAD")
        assert not (repo / constants.WORKTREES_SUBDIR).exists()

    def test_create_calls_create_worktree_for_git_repo(self, tmp_path):
        """A directory with .git gets a worktree at the expected path."""
        repo = _git_repo(tmp_path / "repo-b")
        git.create_include_worktrees([_entry(repo)], "my-task", "main")
        assert (repo / constants.WORKTREES_SUBDIR / "my-task").exists()

    def test_create_skips_non_git_passes_git(self, tmp_path):
        """Mixed list: git repo gets a worktree, plain dir is skipped."""
        repo = _git_repo(tmp_path / "repo-b")
        plain = tmp_path / "data"
        plain.mkdir()
        git.create_include_worktrees([_entry(repo), _entry(plain)], "t", "main")
        assert (repo / constants.WORKTREES_SUBDIR / "t").exists()
        assert not (plain / constants.WORKTREES_SUBDIR).exists()

    def test_remove_skips_non_git_dir(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        git.remove_include_worktrees([_entry(plain)], "my-task")  # should not raise

    def test_remove_skips_reference_mode_entries(self, tmp_path):
        """reference mode entries are not touched by remove."""
        repo = _git_repo(tmp_path / "repo-b")
        wt = repo / constants.WORKTREES_SUBDIR / "my-task"
        utils.run(["git", "worktree", "add", "-b", "hatchery/my-task", str(wt), "main"], cwd=repo)
        git.remove_include_worktrees([_entry(repo, mode="ro")], "my-task")
        assert wt.exists()  # untouched

    def test_remove_calls_remove_worktree_for_git_repo(self, tmp_path):
        repo = _git_repo(tmp_path / "repo-b")
        wt = repo / constants.WORKTREES_SUBDIR / "my-task"
        utils.run(["git", "worktree", "add", "-b", "hatchery/my-task", str(wt), "main"], cwd=repo)
        assert wt.exists()
        git.remove_include_worktrees([_entry(repo)], "my-task")
        assert not wt.exists()

    def test_delete_branches_skips_non_git_dir(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        git.delete_include_branches([_entry(plain)], "my-task")  # should not raise

    def test_delete_branches_skips_reference_mode_entries(self, tmp_path):
        """reference mode entries don't have branches to delete."""
        repo = _git_repo(tmp_path / "repo-b")
        utils.run(["git", "branch", "hatchery/my-task"], cwd=repo)
        git.delete_include_branches([_entry(repo, mode="ro")], "my-task")
        r = utils.run(["git", "rev-parse", "--verify", "hatchery/my-task"], cwd=repo, check=False)
        assert r.returncode == 0  # branch still exists

    def test_delete_branches_calls_delete_branch_for_git_repo(self, tmp_path):
        repo = _git_repo(tmp_path / "repo-b")
        utils.run(["git", "branch", "hatchery/my-task"], cwd=repo)
        git.delete_include_branches([_entry(repo)], "my-task")
        r = utils.run(["git", "rev-parse", "--verify", "hatchery/my-task"], cwd=repo, check=False)
        assert r.returncode != 0  # branch deleted

    def test_create_fetches_and_uses_origin_default(self, tmp_path):
        """When base is None, create_include_worktrees fetches origin and bases the
        new branch on origin/<default> so it starts from the latest upstream commit."""
        remote = _bare_remote(tmp_path / "remote")
        local = tmp_path / "local"
        utils.run(["git", "clone", str(remote), str(local)])
        (local / "README").write_text("hi")
        utils.run(["git", "add", "README"], cwd=local)
        utils.run(["git", "-c", "user.email=t@t.com", "-c", "user.name=T", "commit", "-m", "init"], cwd=local)
        utils.run(["git", "push", "origin", "main"], cwd=local)

        git.create_include_worktrees([_entry(local)], "my-task")

        worktree = local / constants.WORKTREES_SUBDIR / "my-task"
        assert worktree.exists()
        result = utils.run(["git", "rev-parse", "hatchery/my-task"], cwd=local)
        origin_main = utils.run(["git", "rev-parse", "origin/main"], cwd=local)
        assert result.stdout.strip() == origin_main.stdout.strip()

    def test_create_falls_back_to_local_default_when_no_remote(self, tmp_path):
        """If there's no remote, create_include_worktrees falls back to the local branch."""
        repo = _git_repo(tmp_path / "repo")
        git.create_include_worktrees([_entry(repo)], "my-task")
        assert (repo / constants.WORKTREES_SUBDIR / "my-task").exists()

    def test_create_uses_explicit_base_without_fetching(self, tmp_path):
        """An explicit base is passed through directly with no fetch."""
        repo = _git_repo(tmp_path / "repo")
        git.create_include_worktrees([_entry(repo)], "my-task", base="main")
        assert (repo / constants.WORKTREES_SUBDIR / "my-task").exists()

    def test_create_preserves_existing_include_branch(self, tmp_path):
        """If the include already has a hatchery/<name> branch with commits,
        create_include_worktrees attaches without -B so the prior work is preserved."""
        repo = _git_repo(tmp_path / "repo")
        # Create the include task branch and add a unique commit on it.
        utils.run(["git", "branch", "hatchery/my-task"], cwd=repo)
        utils.run(["git", "checkout", "hatchery/my-task"], cwd=repo)
        (repo / "include-work.txt").write_text("don't lose me")
        utils.run(["git", "add", "include-work.txt"], cwd=repo)
        utils.run(
            ["git", "-c", "user.email=t@t.com", "-c", "user.name=T", "commit", "-m", "include work"],
            cwd=repo,
        )
        task_branch_sha = utils.run(["git", "rev-parse", "hatchery/my-task"], cwd=repo).stdout.strip()
        # Switch back to main so the include task branch isn't checked out.
        utils.run(["git", "checkout", "main"], cwd=repo)

        # Now create the include worktree — the branch should NOT be reset.
        git.create_include_worktrees([_entry(repo)], "my-task", base="main")

        after = utils.run(["git", "rev-parse", "hatchery/my-task"], cwd=repo).stdout.strip()
        assert after == task_branch_sha, "hatchery/<name> branch must not be force-reset"
        worktree = repo / constants.WORKTREES_SUBDIR / "my-task"
        assert (worktree / "include-work.txt").exists(), "worktree must attach to existing branch"

    def test_create_preserves_uncommitted_work_in_existing_worktree(self, tmp_path):
        """If the include worktree already exists (branch present, dir present),
        create_include_worktrees must leave it alone — no remove+readd, or an
        uncommitted edit sitting in that worktree would be silently destroyed."""
        repo = _git_repo(tmp_path / "repo")
        worktree = repo / constants.WORKTREES_SUBDIR / "my-task"
        utils.run(["git", "worktree", "add", "-b", "hatchery/my-task", str(worktree), "main"], cwd=repo)
        (worktree / "scratch.txt").write_text("uncommitted work")

        git.create_include_worktrees([_entry(repo)], "my-task", base="main")

        assert (worktree / "scratch.txt").exists(), "existing worktree (and its uncommitted work) must survive"
        assert (worktree / "scratch.txt").read_text() == "uncommitted work"


# ---------------------------------------------------------------------------
# _fetch_if_remote
# ---------------------------------------------------------------------------


class TestFetchIfRemote:
    """_fetch_if_remote fetches the right remote (or nothing) based on the ref."""

    def _repo_with_remote(self, tmp_path: Path) -> tuple[Path, Path]:
        """Returns (local, remote) — local cloned from bare remote, one commit pushed."""
        remote = _bare_remote(tmp_path / "remote")
        local = tmp_path / "local"
        utils.run(["git", "clone", str(remote), str(local)])
        (local / "README").write_text("hi")
        utils.run(["git", "add", "README"], cwd=local)
        utils.run(["git", "-c", "user.email=t@t.com", "-c", "user.name=T", "commit", "-m", "init"], cwd=local)
        utils.run(["git", "push", "origin", "main"], cwd=local)
        return local, remote

    def test_remote_tracking_branch_fetches(self, tmp_path):
        """origin/main resolves to refs/remotes/origin/main — origin is fetched."""
        local, remote = self._repo_with_remote(tmp_path)
        # Add a new commit to the remote directly so a fetch would advance origin/main.
        utils.run(["git", "clone", str(remote), str(tmp_path / "other")])
        (tmp_path / "other" / "NEW").write_text("new")
        utils.run(["git", "add", "NEW"], cwd=tmp_path / "other")
        utils.run(
            ["git", "-c", "user.email=t@t.com", "-c", "user.name=T", "commit", "-m", "second"], cwd=tmp_path / "other"
        )
        utils.run(["git", "push", "origin", "main"], cwd=tmp_path / "other")

        before = utils.run(["git", "rev-parse", "origin/main"], cwd=local).stdout.strip()
        git._fetch_if_remote("origin/main", local)
        after = utils.run(["git", "rev-parse", "origin/main"], cwd=local).stdout.strip()
        assert before != after  # fetch advanced origin/main

    def test_local_branch_with_slash_not_fetched(self, tmp_path):
        """hatchery/my-task is a local branch — no fetch."""
        local, _ = self._repo_with_remote(tmp_path)
        utils.run(["git", "branch", "hatchery/my-task"], cwd=local)
        # Record origin/main before; if a fetch happened it would be a no-op here,
        # but the key test is no exception and no remote activity.
        before = utils.run(["git", "rev-parse", "origin/main"], cwd=local).stdout.strip()
        git._fetch_if_remote("hatchery/my-task", local)
        after = utils.run(["git", "rev-parse", "origin/main"], cwd=local).stdout.strip()
        assert before == after  # origin/main unchanged (no fetch)

    def test_no_slash_not_fetched(self, tmp_path):
        """HEAD and plain branch names have no slash — nothing happens."""
        local, _ = self._repo_with_remote(tmp_path)
        before = utils.run(["git", "rev-parse", "origin/main"], cwd=local).stdout.strip()
        git._fetch_if_remote("HEAD", local)
        git._fetch_if_remote("main", local)
        after = utils.run(["git", "rev-parse", "origin/main"], cwd=local).stdout.strip()
        assert before == after

    def test_unknown_remote_prefix_not_fetched(self, tmp_path):
        """upstream/main where 'upstream' is not a configured remote — no fetch."""
        local, _ = self._repo_with_remote(tmp_path)
        # Should not raise; 'upstream' is not a real remote.
        git._fetch_if_remote("upstream/main", local)

    def test_origin_ref_before_first_fetch_uses_heuristic(self, tmp_path):
        """origin/main doesn't exist yet (fresh clone, never fetched) — heuristic
        detects 'origin' is a real remote and fetches it."""
        remote = _bare_remote(tmp_path / "remote")
        # Push a commit to the remote via a separate clone.
        seed = tmp_path / "seed"
        utils.run(["git", "clone", str(remote), str(seed)])
        (seed / "README").write_text("hi")
        utils.run(["git", "add", "README"], cwd=seed)
        utils.run(["git", "-c", "user.email=t@t.com", "-c", "user.name=T", "commit", "-m", "init"], cwd=seed)
        utils.run(["git", "push", "origin", "main"], cwd=seed)

        # Clone again but clear the remote-tracking ref to simulate "not yet fetched".
        local = tmp_path / "local"
        utils.run(["git", "clone", str(remote), str(local)])
        utils.run(["git", "update-ref", "-d", "refs/remotes/origin/main"], cwd=local)

        # origin/main no longer resolves — heuristic should kick in and fetch.
        git._fetch_if_remote("origin/main", local)
        result = utils.run(["git", "rev-parse", "origin/main"], cwd=local, check=False)
        assert result.returncode == 0  # fetch restored it


# ---------------------------------------------------------------------------
# create_worktree — invalid ref error handling
# ---------------------------------------------------------------------------


class TestCreateWorktreeErrors:
    def test_invalid_ref_exits_cleanly(self, tmp_path):
        """create_worktree exits cleanly with an informative message when the base
        ref doesn't exist in the repo."""
        repo = _git_repo(tmp_path / "repo")
        with pytest.raises(SystemExit):
            git.create_worktree(repo, "hatchery/t", repo / "wt", "nonexistent-branch")


# ---------------------------------------------------------------------------
# branch_exists
# ---------------------------------------------------------------------------


class TestBranchExists:
    def test_true_for_existing_branch(self, tmp_path):
        repo = _git_repo(tmp_path / "repo")
        utils.run(["git", "branch", "feature/x"], cwd=repo)
        assert git.branch_exists(repo, "feature/x") is True

    def test_false_for_missing_branch(self, tmp_path):
        repo = _git_repo(tmp_path / "repo")
        assert git.branch_exists(repo, "no-such-branch") is False

    def test_false_for_tag_with_same_name(self, tmp_path):
        """A tag named ``v1`` must not satisfy the heads-only lookup."""
        repo = _git_repo(tmp_path / "repo")
        utils.run(["git", "tag", "v1"], cwd=repo)
        assert git.branch_exists(repo, "v1") is False
