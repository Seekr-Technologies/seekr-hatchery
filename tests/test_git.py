"""Tests for git helper functions, especially worktree detection."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import seekr_hatchery.git as git


class TestResolveMainRepo:
    def test_normal_repo_unchanged(self, tmp_path):
        """When .git is a directory (normal checkout), repo is returned unchanged."""
        (tmp_path / ".git").mkdir()
        result = git._resolve_main_repo(tmp_path)
        assert result == tmp_path

    def test_worktree_resolves_to_main_repo(self, tmp_path):
        """When .git is a file (linked worktree), resolve to main repo root."""
        # Simulate a linked worktree layout:
        #   tmp_path/         ← worktree root
        #   tmp_path/.git     ← pointer file (content doesn't matter for this test)
        #   main_repo/        ← main repo
        #   main_repo/.git/   ← real git dir
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()
        main_git = main_repo / ".git"
        main_git.mkdir()

        worktree = tmp_path / "my_worktree"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: ../main_repo/.git/worktrees/my_worktree\n")

        # git rev-parse --git-common-dir returns the main .git dir
        fake_result = SimpleNamespace(returncode=0, stdout=str(main_git) + "\n")
        with patch("seekr_hatchery.git.tasks.run", return_value=fake_result) as mock_run:
            result = git._resolve_main_repo(worktree)

        mock_run.assert_called_once_with(["git", "rev-parse", "--git-common-dir"], cwd=worktree, check=False)
        assert result == main_repo

    def test_worktree_with_relative_common_dir(self, tmp_path):
        """Handles relative --git-common-dir output (resolves against worktree)."""
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()
        (main_repo / ".git").mkdir()

        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: ../.git/worktrees/wt\n")

        # Simulate git returning a relative path like "../main_repo/.git"
        rel_common_dir = "../main_repo/.git"
        fake_result = SimpleNamespace(returncode=0, stdout=rel_common_dir + "\n")
        with patch("seekr_hatchery.git.tasks.run", return_value=fake_result):
            result = git._resolve_main_repo(worktree)

        expected = (worktree / rel_common_dir).resolve().parent
        assert result == expected

    def test_exits_on_git_command_failure(self, tmp_path):
        """.git is a file but --git-common-dir fails — exits with an error."""
        (tmp_path / ".git").write_text("gitdir: /some/path\n")
        fake_result = SimpleNamespace(returncode=1, stdout="")
        with patch("seekr_hatchery.git.tasks.run", return_value=fake_result):
            with pytest.raises(SystemExit):
                git._resolve_main_repo(tmp_path)


class TestGitRootOrCwdWorktree:
    def test_normal_repo_returns_toplevel(self, tmp_path):
        (tmp_path / ".git").mkdir()
        show_toplevel = SimpleNamespace(returncode=0, stdout=str(tmp_path) + "\n")
        with patch("seekr_hatchery.git.tasks.run", return_value=show_toplevel):
            path, in_repo = git.git_root_or_cwd()
        assert in_repo is True
        assert path == tmp_path

    def test_worktree_resolves_to_main_repo(self, tmp_path):
        main_repo = tmp_path / "main"
        main_repo.mkdir()
        (main_repo / ".git").mkdir()

        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: ../main/.git/worktrees/wt\n")

        show_toplevel = SimpleNamespace(returncode=0, stdout=str(worktree) + "\n")
        common_dir_result = SimpleNamespace(returncode=0, stdout=str(main_repo / ".git") + "\n")

        def fake_run(cmd, **kwargs):
            if "--show-toplevel" in cmd:
                return show_toplevel
            if "--git-common-dir" in cmd:
                return common_dir_result
            raise AssertionError(f"Unexpected git call: {cmd}")

        with patch("seekr_hatchery.git.tasks.run", side_effect=fake_run):
            path, in_repo = git.git_root_or_cwd()

        assert in_repo is True
        assert path == main_repo


# ---------------------------------------------------------------------------
# create_include_worktrees / remove_include_worktrees / delete_include_branches
# ---------------------------------------------------------------------------


class TestIncludeWorktreeHelpers:
    """Tests for create/remove/delete_include_worktrees using mocked git calls."""

    def _fake_run(self, returncode: int = 0):
        return SimpleNamespace(returncode=returncode, stdout="", stderr="")

    def test_create_skips_non_git_dir(self, tmp_path):
        """A plain directory with no .git is silently skipped."""
        plain = tmp_path / "plain"
        plain.mkdir()
        with patch("seekr_hatchery.git.create_worktree") as mock_cw:
            git.create_include_worktrees([plain], "my-task", "HEAD")
        mock_cw.assert_not_called()

    def test_create_calls_create_worktree_for_git_repo(self, tmp_path):
        """A directory with .git triggers create_worktree with the right args."""
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()
        import seekr_hatchery.tasks as tasks_mod

        with patch("seekr_hatchery.git.create_worktree") as mock_cw:
            git.create_include_worktrees([repo_b], "my-task", "HEAD")

        expected_worktree = repo_b / tasks_mod.WORKTREES_SUBDIR / "my-task"
        mock_cw.assert_called_once_with(repo_b, "hatchery/my-task", expected_worktree, "HEAD")

    def test_create_skips_non_git_passes_git(self, tmp_path):
        """Mixed list: git repo gets a worktree, plain dir is skipped."""
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()
        plain = tmp_path / "data"
        plain.mkdir()

        with patch("seekr_hatchery.git.create_worktree") as mock_cw:
            git.create_include_worktrees([repo_b, plain], "t", "HEAD")

        assert mock_cw.call_count == 1

    def test_remove_skips_non_git_dir(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        with patch("seekr_hatchery.git.remove_worktree") as mock_rw:
            git.remove_include_worktrees([plain], "my-task")
        mock_rw.assert_not_called()

    def test_remove_calls_remove_worktree_for_git_repo(self, tmp_path):
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()
        import seekr_hatchery.tasks as tasks_mod

        with patch("seekr_hatchery.git.remove_worktree") as mock_rw:
            git.remove_include_worktrees([repo_b], "my-task")

        expected_worktree = repo_b / tasks_mod.WORKTREES_SUBDIR / "my-task"
        mock_rw.assert_called_once_with(repo_b, expected_worktree, force=True)

    def test_delete_branches_skips_non_git_dir(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        with patch("seekr_hatchery.git.delete_branch") as mock_db:
            git.delete_include_branches([plain], "my-task")
        mock_db.assert_not_called()

    def test_delete_branches_calls_delete_branch_for_git_repo(self, tmp_path):
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()

        with patch("seekr_hatchery.git.delete_branch") as mock_db:
            git.delete_include_branches([repo_b], "my-task")

        mock_db.assert_called_once_with(repo_b, "hatchery/my-task")
