"""Tests for git helper functions, especially worktree detection."""

from pathlib import Path

import pytest

import seekr_hatchery.git as git
import seekr_hatchery.tasks as tasks_mod
from seekr_hatchery.includes import IncludeEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git_repo(path: Path) -> Path:
    """Init a real git repo with one commit at path. Returns path."""
    path.mkdir(exist_ok=True)
    tasks_mod.run(["git", "init", "--initial-branch=main", str(path)], check=False)
    tasks_mod.run(["git", "init", str(path)], check=False)
    (path / "README").write_text("hi")
    tasks_mod.run(["git", "add", "README"], cwd=path)
    tasks_mod.run(["git", "-c", "user.email=t@t.com", "-c", "user.name=T",
                   "commit", "-m", "init"], cwd=path)
    return path


def _bare_remote(path: Path) -> Path:
    """Init a bare git repo suitable for use as a remote. Returns path."""
    path.mkdir(exist_ok=True)
    tasks_mod.run(["git", "init", "--bare", str(path)])
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
        tasks_mod.run(["git", "worktree", "add", "--detach", str(worktree)], cwd=main_repo)

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
        tasks_mod.run(["git", "worktree", "add", "--detach", str(worktree)], cwd=main_repo)

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
        assert not (plain / tasks_mod.WORKTREES_SUBDIR).exists()

    def test_create_skips_reference_mode_entries(self, tmp_path):
        """reference mode entries (ro/rw) are skipped even if they are git repos."""
        repo = _git_repo(tmp_path / "repo-b")
        git.create_include_worktrees([_entry(repo, mode="ro")], "my-task", "HEAD")
        git.create_include_worktrees([_entry(repo, mode="rw")], "my-task", "HEAD")
        assert not (repo / tasks_mod.WORKTREES_SUBDIR).exists()

    def test_create_calls_create_worktree_for_git_repo(self, tmp_path):
        """A directory with .git gets a worktree at the expected path."""
        repo = _git_repo(tmp_path / "repo-b")
        git.create_include_worktrees([_entry(repo)], "my-task", "main")
        assert (repo / tasks_mod.WORKTREES_SUBDIR / "my-task").exists()

    def test_create_skips_non_git_passes_git(self, tmp_path):
        """Mixed list: git repo gets a worktree, plain dir is skipped."""
        repo = _git_repo(tmp_path / "repo-b")
        plain = tmp_path / "data"
        plain.mkdir()
        git.create_include_worktrees([_entry(repo), _entry(plain)], "t", "main")
        assert (repo / tasks_mod.WORKTREES_SUBDIR / "t").exists()
        assert not (plain / tasks_mod.WORKTREES_SUBDIR).exists()

    def test_remove_skips_non_git_dir(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        git.remove_include_worktrees([_entry(plain)], "my-task")  # should not raise

    def test_remove_skips_reference_mode_entries(self, tmp_path):
        """reference mode entries are not touched by remove."""
        repo = _git_repo(tmp_path / "repo-b")
        wt = repo / tasks_mod.WORKTREES_SUBDIR / "my-task"
        tasks_mod.run(["git", "worktree", "add", "-b", "hatchery/my-task", str(wt), "main"], cwd=repo)
        git.remove_include_worktrees([_entry(repo, mode="ro")], "my-task")
        assert wt.exists()  # untouched

    def test_remove_calls_remove_worktree_for_git_repo(self, tmp_path):
        repo = _git_repo(tmp_path / "repo-b")
        wt = repo / tasks_mod.WORKTREES_SUBDIR / "my-task"
        tasks_mod.run(["git", "worktree", "add", "-b", "hatchery/my-task", str(wt), "main"], cwd=repo)
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
        tasks_mod.run(["git", "branch", "hatchery/my-task"], cwd=repo)
        git.delete_include_branches([_entry(repo, mode="ro")], "my-task")
        r = tasks_mod.run(["git", "rev-parse", "--verify", "hatchery/my-task"], cwd=repo, check=False)
        assert r.returncode == 0  # branch still exists

    def test_delete_branches_calls_delete_branch_for_git_repo(self, tmp_path):
        repo = _git_repo(tmp_path / "repo-b")
        tasks_mod.run(["git", "branch", "hatchery/my-task"], cwd=repo)
        git.delete_include_branches([_entry(repo)], "my-task")
        r = tasks_mod.run(["git", "rev-parse", "--verify", "hatchery/my-task"], cwd=repo, check=False)
        assert r.returncode != 0  # branch deleted

    def test_create_fetches_and_uses_origin_default(self, tmp_path):
        """When base is None, create_include_worktrees fetches origin and bases the
        new branch on origin/<default> so it starts from the latest upstream commit."""
        remote = _bare_remote(tmp_path / "remote")
        local = tmp_path / "local"
        tasks_mod.run(["git", "clone", str(remote), str(local)])
        (local / "README").write_text("hi")
        tasks_mod.run(["git", "add", "README"], cwd=local)
        tasks_mod.run(["git", "-c", "user.email=t@t.com", "-c", "user.name=T",
                       "commit", "-m", "init"], cwd=local)
        tasks_mod.run(["git", "push", "origin", "main"], cwd=local)

        git.create_include_worktrees([_entry(local)], "my-task")

        worktree = local / tasks_mod.WORKTREES_SUBDIR / "my-task"
        assert worktree.exists()
        result = tasks_mod.run(["git", "rev-parse", "hatchery/my-task"], cwd=local)
        origin_main = tasks_mod.run(["git", "rev-parse", "origin/main"], cwd=local)
        assert result.stdout.strip() == origin_main.stdout.strip()

    def test_create_falls_back_to_local_default_when_no_remote(self, tmp_path):
        """If there's no remote, create_include_worktrees falls back to the local branch."""
        repo = _git_repo(tmp_path / "repo")
        git.create_include_worktrees([_entry(repo)], "my-task")
        assert (repo / tasks_mod.WORKTREES_SUBDIR / "my-task").exists()

    def test_create_uses_explicit_base_without_fetching(self, tmp_path):
        """An explicit base is passed through directly with no fetch."""
        repo = _git_repo(tmp_path / "repo")
        git.create_include_worktrees([_entry(repo)], "my-task", base="main")
        assert (repo / tasks_mod.WORKTREES_SUBDIR / "my-task").exists()


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
