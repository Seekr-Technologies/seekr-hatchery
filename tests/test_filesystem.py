"""Tests for filesystem operations."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import seekr_hatchery.git as git
import seekr_hatchery.sessions as sessions

# ---------------------------------------------------------------------------
# write_task_file
# ---------------------------------------------------------------------------


class TestWriteTaskFile:
    def test_creates_file(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        with patch("seekr_hatchery.sessions.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 15, 10, 30)
            path = sessions.write_task_file(worktree / ".hatchery" / "tasks", "my-task", "hatchery/my-task")
        assert path.exists()

    def test_correct_path(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        with patch("seekr_hatchery.sessions.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 15, 10, 30)
            path = sessions.write_task_file(worktree / ".hatchery" / "tasks", "my-task", "hatchery/my-task")
        expected = worktree / ".hatchery" / "tasks" / "2026-01-15-my-task" / "task.md"
        assert path == expected

    def test_contains_task_name(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        path = sessions.write_task_file(worktree / ".hatchery" / "tasks", "my-task", "hatchery/my-task")
        content = path.read_text()
        assert "my-task" in content

    def test_contains_branch(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        path = sessions.write_task_file(worktree / ".hatchery" / "tasks", "my-task", "hatchery/my-task")
        content = path.read_text()
        assert "hatchery/my-task" in content

    def test_contains_status_in_progress(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        path = sessions.write_task_file(worktree / ".hatchery" / "tasks", "my-task", "hatchery/my-task")
        content = path.read_text()
        assert "in-progress" in content

    def test_contains_task_heading(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        path = sessions.write_task_file(worktree / ".hatchery" / "tasks", "my-task", "hatchery/my-task")
        content = path.read_text()
        assert "# Task:" in content

    def test_contains_section_headings(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        path = sessions.write_task_file(worktree / ".hatchery" / "tasks", "my-task", "hatchery/my-task")
        content = path.read_text()
        assert "## Objective" in content
        assert "## Context" in content
        assert "## Agreed Plan" in content
        assert "## Progress Log" in content

    def test_contains_branch_label(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        path = sessions.write_task_file(worktree / ".hatchery" / "tasks", "my-task", "hatchery/my-task")
        content = path.read_text()
        assert "**Branch**:" in content

    def test_contains_status_label(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        path = sessions.write_task_file(worktree / ".hatchery" / "tasks", "my-task", "hatchery/my-task")
        content = path.read_text()
        assert "**Status**:" in content

    def test_objective_param_injects_text(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        path = sessions.write_task_file(
            worktree / ".hatchery" / "tasks", "my-task", "hatchery/my-task", objective="Add a login page"
        )
        content = path.read_text()
        assert "Add a login page" in content

    def test_objective_param_omits_context_section(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        path = sessions.write_task_file(
            worktree / ".hatchery" / "tasks", "my-task", "hatchery/my-task", objective="Add a login page"
        )
        content = path.read_text()
        assert "## Context" not in content

    def test_objective_param_omits_todo_placeholder(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        path = sessions.write_task_file(
            worktree / ".hatchery" / "tasks", "my-task", "hatchery/my-task", objective="Add a login page"
        )
        content = path.read_text()
        assert "TODO" not in content


# ---------------------------------------------------------------------------
# ensure_tasks_dir
# ---------------------------------------------------------------------------


class TestEnsureTasksDir:
    def test_creates_hatchery_tasks_dir(self, fake_repo):
        sessions.ensure_tasks_dir(fake_repo)
        assert (fake_repo / ".hatchery" / "tasks").is_dir()

    def test_creates_readme(self, fake_repo):
        sessions.ensure_tasks_dir(fake_repo)
        readme = fake_repo / ".hatchery" / "README.md"
        assert readme.exists()

    def test_idempotent_second_call(self, fake_repo):
        sessions.ensure_tasks_dir(fake_repo)
        sessions.ensure_tasks_dir(fake_repo)
        assert (fake_repo / ".hatchery" / "tasks").is_dir()

    def test_preserves_existing_readme(self, fake_repo):
        hatchery = fake_repo / ".hatchery"
        hatchery.mkdir(exist_ok=True)
        readme = hatchery / "README.md"
        readme.write_text("existing content")
        sessions.ensure_tasks_dir(fake_repo)
        assert readme.read_text() == "existing content"

    def test_readme_has_content(self, fake_repo):
        sessions.ensure_tasks_dir(fake_repo)
        readme = fake_repo / ".hatchery" / "README.md"
        assert len(readme.read_text()) > 0


# ---------------------------------------------------------------------------
# ensure_gitignore
# ---------------------------------------------------------------------------


class TestEnsureGitignore:
    def test_creates_gitignore_when_absent(self, fake_repo):
        sessions.ensure_gitignore(fake_repo)
        assert (fake_repo / ".gitignore").exists()

    def test_created_contains_worktrees_entry(self, fake_repo):
        sessions.ensure_gitignore(fake_repo)
        content = (fake_repo / ".gitignore").read_text()
        assert ".hatchery/worktrees/" in content

    def test_appends_to_existing_gitignore(self, fake_repo):
        gitignore = fake_repo / ".gitignore"
        gitignore.write_text("*.pyc\n")
        sessions.ensure_gitignore(fake_repo)
        content = gitignore.read_text()
        assert "*.pyc" in content
        assert ".hatchery/worktrees/" in content

    def test_idempotent_no_duplicate_entry(self, fake_repo):
        sessions.ensure_gitignore(fake_repo)
        sessions.ensure_gitignore(fake_repo)
        content = (fake_repo / ".gitignore").read_text()
        assert content.count(".hatchery/worktrees/") == 1

    def test_handles_missing_trailing_newline(self, fake_repo):
        gitignore = fake_repo / ".gitignore"
        gitignore.write_text("*.pyc")  # no trailing newline
        sessions.ensure_gitignore(fake_repo)
        content = gitignore.read_text()
        assert ".hatchery/worktrees/" in content
        # Verify the entries are on separate lines
        lines = content.splitlines()
        assert "*.pyc" in lines
        assert ".hatchery/worktrees/" in lines

    def test_idempotent_with_existing_entry(self, fake_repo):
        gitignore = fake_repo / ".gitignore"
        gitignore.write_text(".hatchery/worktrees/\n")
        sessions.ensure_gitignore(fake_repo)
        content = gitignore.read_text()
        assert content.count(".hatchery/worktrees/") == 1


# ---------------------------------------------------------------------------
# I9: ensure_git_exclude — writes to .git/info/exclude (real fs)
# ---------------------------------------------------------------------------


class TestEnsureGitExclude:
    def test_creates_exclude_when_absent(self, fake_repo):
        sessions.ensure_git_exclude(fake_repo, ".hatchery/worktrees/")
        exclude = fake_repo / ".git" / "info" / "exclude"
        assert exclude.exists()
        assert ".hatchery/worktrees/" in exclude.read_text()

    def test_appends_to_existing_exclude(self, fake_repo):
        exclude = fake_repo / ".git" / "info"
        exclude.mkdir(parents=True)
        (exclude / "exclude").write_text("*.pyc\n")
        sessions.ensure_git_exclude(fake_repo, ".hatchery/worktrees/")
        content = (fake_repo / ".git" / "info" / "exclude").read_text()
        assert "*.pyc" in content
        assert ".hatchery/worktrees/" in content

    def test_idempotent_no_duplicate(self, fake_repo):
        sessions.ensure_git_exclude(fake_repo, ".hatchery/worktrees/")
        sessions.ensure_git_exclude(fake_repo, ".hatchery/worktrees/")
        content = (fake_repo / ".git" / "info" / "exclude").read_text()
        assert content.count(".hatchery/worktrees/") == 1

    def test_handles_missing_trailing_newline(self, fake_repo):
        exclude = fake_repo / ".git" / "info"
        exclude.mkdir(parents=True)
        (exclude / "exclude").write_text("*.pyc")  # no trailing newline
        sessions.ensure_git_exclude(fake_repo, ".hatchery/worktrees/")
        content = (fake_repo / ".git" / "info" / "exclude").read_text()
        lines = content.splitlines()
        assert "*.pyc" in lines
        assert ".hatchery/worktrees/" in lines

    def test_custom_entry(self, fake_repo):
        sessions.ensure_git_exclude(fake_repo, "some/custom/path/")
        content = (fake_repo / ".git" / "info" / "exclude").read_text()
        assert "some/custom/path/" in content

    def test_does_not_touch_committed_gitignore(self, fake_repo):
        """ensure_git_exclude must never write to .gitignore (committed)."""
        gitignore = fake_repo / ".gitignore"
        gitignore.write_text("existing\n")
        sessions.ensure_git_exclude(fake_repo, ".hatchery/worktrees/")
        assert gitignore.read_text() == "existing\n"


class TestRemoveWorktree:
    def test_success_calls_git_worktree_remove(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        run_calls = []

        def fake_run(cmd, cwd=None, check=True, sensitive=False):
            run_calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        monkeypatch.setattr(git, "run", fake_run)
        git.remove_worktree(repo, worktree)

        assert any("worktree" in " ".join(c) and "remove" in " ".join(c) for c in run_calls)

    def test_force_flag_included(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        run_calls = []

        def fake_run(cmd, cwd=None, check=True, sensitive=False):
            run_calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        monkeypatch.setattr(git, "run", fake_run)
        git.remove_worktree(repo, worktree, force=True)

        remove_calls = [c for c in run_calls if "remove" in " ".join(c)]
        assert any("--force" in c for c in remove_calls)

    def test_fallback_to_rmtree_on_failure(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "file.txt").write_text("content")

        call_count = [0]

        def fake_run(cmd, cwd=None, check=True, sensitive=False):
            result = MagicMock()
            result.returncode = 1  # always fail
            result.stdout = ""
            result.stderr = "error"
            call_count[0] += 1
            return result

        monkeypatch.setattr(git, "run", fake_run)
        git.remove_worktree(repo, worktree)

        # Worktree should have been removed by shutil.rmtree
        assert not worktree.exists()

    def test_prune_called_after_fallback(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        run_calls = []

        def fake_run(cmd, cwd=None, check=True, sensitive=False):
            run_calls.append(cmd)
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            result.stderr = ""
            return result

        monkeypatch.setattr(git, "run", fake_run)
        git.remove_worktree(repo, worktree)

        assert any("prune" in " ".join(c) for c in run_calls)


# ---------------------------------------------------------------------------
# _load_docker_config
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _construct_docker_mounts
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ensure_docker_config
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ensure_dockerfile
# ---------------------------------------------------------------------------
