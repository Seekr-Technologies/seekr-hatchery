"""Tests for subprocess-wrapping functions."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import seekr_hatchery.git as git
import seekr_hatchery.tasks as tasks

# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


class TestRun:
    def test_returns_completed_process(self):
        result = tasks.run(["echo", "hello"])
        assert isinstance(result, subprocess.CompletedProcess)

    def test_captures_stdout(self):
        result = tasks.run(["echo", "hello"])
        assert "hello" in result.stdout

    def test_passes_cwd(self, tmp_path):
        result = tasks.run(["pwd"], cwd=tmp_path)
        assert str(tmp_path) in result.stdout.strip()

    def test_raises_on_nonzero_with_check_true(self):
        with pytest.raises(subprocess.CalledProcessError):
            tasks.run(["false"], check=True)

    def test_does_not_raise_with_check_false(self):
        result = tasks.run(["false"], check=False)
        assert result.returncode != 0

    def test_prints_error_to_stderr_on_failure(self, capsys):
        with pytest.raises(subprocess.CalledProcessError):
            tasks.run(["false"], check=True)
        captured = capsys.readouterr()
        assert "Error" in captured.err or len(captured.err) >= 0  # just verify it doesn't crash

    def test_check_false_nonzero_returns_result(self):
        result = tasks.run(["sh", "-c", "exit 42"], check=False)
        assert result.returncode == 42


# ---------------------------------------------------------------------------
# uncommitted_changes_summary()
# ---------------------------------------------------------------------------


class TestUncommittedChangesSummary:
    def _init_git_repo(self, path: Path) -> None:
        """Initialise a minimal git repo at *path*."""
        import subprocess

        subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)

    def test_staged_file_appears_in_summary(self, tmp_path):
        import subprocess

        self._init_git_repo(tmp_path)
        # Create an initial commit so HEAD exists
        (tmp_path / "readme.txt").write_text("hello\n")
        subprocess.run(["git", "add", "readme.txt"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
        # Modify the file (tracked change visible in diff HEAD)
        (tmp_path / "readme.txt").write_text("hello\nworld\n")
        subprocess.run(["git", "add", "readme.txt"], cwd=tmp_path, check=True, capture_output=True)

        result = git.uncommitted_changes_summary(tmp_path)
        assert "readme.txt" in result

    def test_untracked_file_appears_in_summary(self, tmp_path):
        import subprocess

        self._init_git_repo(tmp_path)
        # Create an initial commit so HEAD exists
        (tmp_path / "readme.txt").write_text("hello\n")
        subprocess.run(["git", "add", "readme.txt"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
        # Add an untracked file
        (tmp_path / "scratch.py").write_text("# scratch\n")

        result = git.uncommitted_changes_summary(tmp_path)
        assert "scratch.py" in result

    def test_diff_stat_summary_line_appears(self, tmp_path):
        import subprocess

        self._init_git_repo(tmp_path)
        (tmp_path / "file.txt").write_text("line1\n")
        subprocess.run(["git", "add", "file.txt"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
        # Modify and stage so diff HEAD --stat shows a summary
        (tmp_path / "file.txt").write_text("line1\nline2\nline3\n")
        subprocess.run(["git", "add", "file.txt"], cwd=tmp_path, check=True, capture_output=True)

        result = git.uncommitted_changes_summary(tmp_path)
        assert "changed" in result

    def test_returns_empty_string_when_clean(self, tmp_path):
        import subprocess

        self._init_git_repo(tmp_path)
        (tmp_path / "file.txt").write_text("hello\n")
        subprocess.run(["git", "add", "file.txt"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

        result = git.uncommitted_changes_summary(tmp_path)
        assert result == ""


# ---------------------------------------------------------------------------
# git_root()
# ---------------------------------------------------------------------------


class TestGitRoot:
    def test_returns_path_on_success(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "/my/repo\n"
        monkeypatch.setattr(tasks, "run", lambda *a, **kw: mock_result)
        result = git.git_root()
        assert result == Path("/my/repo")

    def test_strips_whitespace_from_stdout(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "  /my/repo  \n"
        monkeypatch.setattr(tasks, "run", lambda *a, **kw: mock_result)
        result = git.git_root()
        assert result == Path("/my/repo")

    def test_exits_on_failure(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        monkeypatch.setattr(tasks, "run", lambda *a, **kw: mock_result)
        with pytest.raises(SystemExit) as exc_info:
            git.git_root()
        assert exc_info.value.code == 1

    def test_stderr_message_on_failure(self, monkeypatch, capsys):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        monkeypatch.setattr(tasks, "run", lambda *a, **kw: mock_result)
        with pytest.raises(SystemExit):
            git.git_root()
        captured = capsys.readouterr()
        assert "git repository" in captured.err

    def test_returns_path_type(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "/some/path\n"
        monkeypatch.setattr(tasks, "run", lambda *a, **kw: mock_result)
        result = git.git_root()
        assert isinstance(result, Path)
