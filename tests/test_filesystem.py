"""Tests for filesystem operations."""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import seekr_hatchery.agent as agent
import seekr_hatchery.docker as docker
import seekr_hatchery.git as git
import seekr_hatchery.tasks as tasks

# ---------------------------------------------------------------------------
# write_task_file
# ---------------------------------------------------------------------------


class TestWriteTaskFile:
    def test_creates_file(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        with patch("seekr_hatchery.tasks.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 15, 10, 30)
            path = tasks.write_task_file(worktree, "my-task", "hatchery/my-task")
        assert path.exists()

    def test_correct_path(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        with patch("seekr_hatchery.tasks.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 15, 10, 30)
            path = tasks.write_task_file(worktree, "my-task", "hatchery/my-task")
        expected = worktree / ".hatchery" / "tasks" / "2026-01-15-my-task.md"
        assert path == expected

    def test_contains_task_name(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        path = tasks.write_task_file(worktree, "my-task", "hatchery/my-task")
        content = path.read_text()
        assert "my-task" in content

    def test_contains_branch(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        path = tasks.write_task_file(worktree, "my-task", "hatchery/my-task")
        content = path.read_text()
        assert "hatchery/my-task" in content

    def test_contains_status_in_progress(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        path = tasks.write_task_file(worktree, "my-task", "hatchery/my-task")
        content = path.read_text()
        assert "in-progress" in content

    def test_contains_task_heading(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        path = tasks.write_task_file(worktree, "my-task", "hatchery/my-task")
        content = path.read_text()
        assert "# Task:" in content

    def test_contains_section_headings(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        path = tasks.write_task_file(worktree, "my-task", "hatchery/my-task")
        content = path.read_text()
        assert "## Objective" in content
        assert "## Context" in content
        assert "## Agreed Plan" in content
        assert "## Progress Log" in content

    def test_contains_branch_label(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        path = tasks.write_task_file(worktree, "my-task", "hatchery/my-task")
        content = path.read_text()
        assert "**Branch**:" in content

    def test_contains_status_label(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        path = tasks.write_task_file(worktree, "my-task", "hatchery/my-task")
        content = path.read_text()
        assert "**Status**:" in content

    def test_objective_param_injects_text(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        path = tasks.write_task_file(worktree, "my-task", "hatchery/my-task", objective="Add a login page")
        content = path.read_text()
        assert "Add a login page" in content

    def test_objective_param_omits_context_section(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        path = tasks.write_task_file(worktree, "my-task", "hatchery/my-task", objective="Add a login page")
        content = path.read_text()
        assert "## Context" not in content

    def test_objective_param_omits_todo_placeholder(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        path = tasks.write_task_file(worktree, "my-task", "hatchery/my-task", objective="Add a login page")
        content = path.read_text()
        assert "TODO" not in content


# ---------------------------------------------------------------------------
# ensure_tasks_dir
# ---------------------------------------------------------------------------


class TestEnsureTasksDir:
    def test_creates_hatchery_tasks_dir(self, fake_repo):
        tasks.ensure_tasks_dir(fake_repo)
        assert (fake_repo / ".hatchery" / "tasks").is_dir()

    def test_creates_readme(self, fake_repo):
        tasks.ensure_tasks_dir(fake_repo)
        readme = fake_repo / ".hatchery" / "README.md"
        assert readme.exists()

    def test_idempotent_second_call(self, fake_repo):
        tasks.ensure_tasks_dir(fake_repo)
        tasks.ensure_tasks_dir(fake_repo)
        assert (fake_repo / ".hatchery" / "tasks").is_dir()

    def test_preserves_existing_readme(self, fake_repo):
        hatchery = fake_repo / ".hatchery"
        hatchery.mkdir(exist_ok=True)
        readme = hatchery / "README.md"
        readme.write_text("existing content")
        tasks.ensure_tasks_dir(fake_repo)
        assert readme.read_text() == "existing content"

    def test_readme_has_content(self, fake_repo):
        tasks.ensure_tasks_dir(fake_repo)
        readme = fake_repo / ".hatchery" / "README.md"
        assert len(readme.read_text()) > 0


# ---------------------------------------------------------------------------
# ensure_gitignore
# ---------------------------------------------------------------------------


class TestEnsureGitignore:
    def test_creates_gitignore_when_absent(self, fake_repo):
        tasks.ensure_gitignore(fake_repo)
        assert (fake_repo / ".gitignore").exists()

    def test_created_contains_worktrees_entry(self, fake_repo):
        tasks.ensure_gitignore(fake_repo)
        content = (fake_repo / ".gitignore").read_text()
        assert ".hatchery/worktrees/" in content

    def test_appends_to_existing_gitignore(self, fake_repo):
        gitignore = fake_repo / ".gitignore"
        gitignore.write_text("*.pyc\n")
        tasks.ensure_gitignore(fake_repo)
        content = gitignore.read_text()
        assert "*.pyc" in content
        assert ".hatchery/worktrees/" in content

    def test_idempotent_no_duplicate_entry(self, fake_repo):
        tasks.ensure_gitignore(fake_repo)
        tasks.ensure_gitignore(fake_repo)
        content = (fake_repo / ".gitignore").read_text()
        assert content.count(".hatchery/worktrees/") == 1

    def test_handles_missing_trailing_newline(self, fake_repo):
        gitignore = fake_repo / ".gitignore"
        gitignore.write_text("*.pyc")  # no trailing newline
        tasks.ensure_gitignore(fake_repo)
        content = gitignore.read_text()
        assert ".hatchery/worktrees/" in content
        # Verify the entries are on separate lines
        lines = content.splitlines()
        assert "*.pyc" in lines
        assert ".hatchery/worktrees/" in lines

    def test_idempotent_with_existing_entry(self, fake_repo):
        gitignore = fake_repo / ".gitignore"
        gitignore.write_text(".hatchery/worktrees/\n")
        tasks.ensure_gitignore(fake_repo)
        content = gitignore.read_text()
        assert content.count(".hatchery/worktrees/") == 1


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

        monkeypatch.setattr(tasks, "run", fake_run)
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

        monkeypatch.setattr(tasks, "run", fake_run)
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

        monkeypatch.setattr(tasks, "run", fake_run)
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

        monkeypatch.setattr(tasks, "run", fake_run)
        git.remove_worktree(repo, worktree)

        assert any("prune" in " ".join(c) for c in run_calls)


# ---------------------------------------------------------------------------
# _load_docker_config
# ---------------------------------------------------------------------------


class TestLoadDockerConfig:
    def _write_config(self, repo: Path, content: str) -> None:
        config_dir = repo / ".hatchery"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "docker.yaml").write_text(content)

    def test_returns_empty_config_when_no_file(self, fake_repo):
        result = docker.load_docker_config(fake_repo)
        assert isinstance(result, docker.DockerConfig)
        assert result.mounts == []

    def test_returns_config_when_mounts_empty(self, fake_repo):
        self._write_config(fake_repo, "schema_version: 1\nmounts: []\n")
        result = docker.load_docker_config(fake_repo)
        assert isinstance(result, docker.DockerConfig)
        assert result.mounts == []

    def test_returns_config_when_mounts_null(self, fake_repo):
        # All entries commented out → YAML parses mounts as null
        self._write_config(fake_repo, "schema_version: 1\nmounts:\n")
        result = docker.load_docker_config(fake_repo)
        assert result.mounts == []

    def test_returns_config_with_mount_entries(self, fake_repo):
        self._write_config(fake_repo, 'schema_version: 1\nmounts:\n  - "/host:/container:ro"\n')
        result = docker.load_docker_config(fake_repo)
        assert result.mounts == ["/host:/container:ro"]

    def test_exits_on_invalid_yaml(self, fake_repo):
        self._write_config(fake_repo, "[unclosed\n")
        with pytest.raises(SystemExit):
            docker.load_docker_config(fake_repo)

    def test_exits_on_invalid_mount_format(self, fake_repo):
        self._write_config(fake_repo, 'schema_version: 1\nmounts:\n  - "badformat"\n')
        with pytest.raises(SystemExit):
            docker.load_docker_config(fake_repo)

    def test_exits_on_invalid_mode(self, fake_repo):
        self._write_config(fake_repo, 'schema_version: 1\nmounts:\n  - "/host:/container:rx"\n')
        with pytest.raises(SystemExit):
            docker.load_docker_config(fake_repo)

    def test_exits_on_extra_keys(self, fake_repo):
        self._write_config(fake_repo, "schema_version: 1\nmounts: []\nextra_key: oops\n")
        with pytest.raises(SystemExit):
            docker.load_docker_config(fake_repo)

    def test_error_message_mentions_config_file(self, fake_repo, capsys):
        self._write_config(fake_repo, 'schema_version: 1\nmounts:\n  - "badformat"\n')
        with pytest.raises(SystemExit):
            docker.load_docker_config(fake_repo)
        assert "docker.yaml" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _construct_docker_mounts
# ---------------------------------------------------------------------------


class TestConstructDockerMounts:
    def test_returns_empty_for_empty_config(self):
        config = docker.DockerConfig()
        assert docker._construct_docker_mounts(config) == []

    def test_resolves_existing_host_path(self, tmp_path):
        host_dir = tmp_path / "mydir"
        host_dir.mkdir()
        config = docker.DockerConfig(mounts=[f"{host_dir}:/container:ro"])
        result = docker._construct_docker_mounts(config)
        assert result == [f"{host_dir}:/container:ro"]

    def test_skips_nonexistent_host_path(self):
        config = docker.DockerConfig(mounts=["/no/such/path:/container:ro"])
        assert docker._construct_docker_mounts(config) == []

    def test_defaults_mode_to_ro(self, tmp_path):
        host_dir = tmp_path / "mydir"
        host_dir.mkdir()
        config = docker.DockerConfig(mounts=[f"{host_dir}:/container"])
        result = docker._construct_docker_mounts(config)
        assert result == [f"{host_dir}:/container:ro"]

    def test_expands_tilde_in_host_path(self, home):
        (home / "mydir").mkdir()
        config = docker.DockerConfig(mounts=["~/mydir:/container:ro"])
        result = docker._construct_docker_mounts(config)
        assert result == [f"{home}/mydir:/container:ro"]

    def test_rw_mode_preserved(self, tmp_path):
        host_dir = tmp_path / "mydir"
        host_dir.mkdir()
        config = docker.DockerConfig(mounts=[f"{host_dir}:/container:rw"])
        result = docker._construct_docker_mounts(config)
        assert result == [f"{host_dir}:/container:rw"]


# ---------------------------------------------------------------------------
# ensure_docker_config
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ensure_dockerfile
# ---------------------------------------------------------------------------


class TestEnsureDockerfile:
    def _prep(self, repo: Path) -> None:
        (repo / ".hatchery").mkdir(exist_ok=True)

    def test_creates_agent_specific_dockerfile(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        docker.ensure_dockerfile(fake_repo, agent.CLAUDE)
        assert docker.dockerfile_path(fake_repo, agent.CLAUDE).exists()

    def test_returns_true_when_created(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        assert docker.ensure_dockerfile(fake_repo, agent.CLAUDE) is True

    def test_returns_false_when_already_exists(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        docker.dockerfile_path(fake_repo, agent.CLAUDE).write_text("FROM debian\n")
        monkeypatch.setattr("builtins.input", lambda _: "n")
        assert docker.ensure_dockerfile(fake_repo, agent.CLAUDE) is False

    def test_skips_if_already_exists(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        df = docker.dockerfile_path(fake_repo, agent.CLAUDE)
        df.write_text("existing content")
        monkeypatch.setattr("builtins.input", lambda _: "n")
        with patch("seekr_hatchery.docker.tasks.open_for_editing") as mock_edit:
            docker.ensure_dockerfile(fake_repo, agent.CLAUDE)
        assert df.read_text() == "existing content"
        mock_edit.assert_not_called()

    def test_yes_opens_editor(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        with patch("seekr_hatchery.docker.tasks.open_for_editing") as mock_edit:
            docker.ensure_dockerfile(fake_repo)
        mock_edit.assert_called_once()

    def test_enter_opens_editor(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "")  # pressing Enter = default yes
        with patch("seekr_hatchery.docker.tasks.open_for_editing") as mock_edit:
            docker.ensure_dockerfile(fake_repo)
        mock_edit.assert_called_once()

    def test_no_skips_editor(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        with patch("seekr_hatchery.docker.tasks.open_for_editing") as mock_edit:
            docker.ensure_dockerfile(fake_repo)
        mock_edit.assert_not_called()

    def test_does_not_commit(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        with patch("seekr_hatchery.docker.tasks.run") as mock_run:
            docker.ensure_dockerfile(fake_repo)
        mock_run.assert_not_called()

    def test_placeholders_resolved(self, fake_repo, monkeypatch):
        """Rendered Dockerfile contains no raw placeholders and includes the
        DinD lines in commented-out form."""
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        docker.ensure_dockerfile(fake_repo, agent.CLAUDE)
        content = docker.dockerfile_path(fake_repo, agent.CLAUDE).read_text()
        assert "{{AGENT_INSTALL}}" not in content
        assert "{{DIND}}" not in content
        assert "# USER root" in content
        assert "fuse-overlayfs" in content


# ---------------------------------------------------------------------------
# ensure_docker_config
# ---------------------------------------------------------------------------


class TestEnsureDockerConfig:
    def _prep(self, repo: Path) -> None:
        """Create .hatchery/ — normally done by ensure_tasks_dir before this runs."""
        (repo / ".hatchery").mkdir(exist_ok=True)

    def test_creates_config_file(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        docker.ensure_docker_config(fake_repo)
        assert (fake_repo / tasks.DOCKER_CONFIG).exists()

    def test_returns_true_when_created(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        assert docker.ensure_docker_config(fake_repo) is True

    def test_returns_false_when_already_exists(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        (fake_repo / tasks.DOCKER_CONFIG).write_text("existing")
        monkeypatch.setattr("builtins.input", lambda _: "n")
        assert docker.ensure_docker_config(fake_repo) is False

    def test_skips_if_already_exists(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        config = fake_repo / tasks.DOCKER_CONFIG
        config.write_text("existing content")
        monkeypatch.setattr("builtins.input", lambda _: "n")
        with patch("seekr_hatchery.docker.tasks.open_for_editing") as mock_edit:
            docker.ensure_docker_config(fake_repo)
        assert config.read_text() == "existing content"
        mock_edit.assert_not_called()

    def test_yes_opens_editor(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        with patch("seekr_hatchery.docker.tasks.open_for_editing") as mock_edit:
            docker.ensure_docker_config(fake_repo)
        mock_edit.assert_called_once()

    def test_enter_opens_editor(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "")  # pressing Enter = default yes
        with patch("seekr_hatchery.docker.tasks.open_for_editing") as mock_edit:
            docker.ensure_docker_config(fake_repo)
        mock_edit.assert_called_once()

    def test_no_skips_editor(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        with patch("seekr_hatchery.docker.tasks.open_for_editing") as mock_edit:
            docker.ensure_docker_config(fake_repo)
        mock_edit.assert_not_called()

    def test_does_not_commit(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        with patch("seekr_hatchery.docker.tasks.run") as mock_run:
            docker.ensure_docker_config(fake_repo)
        mock_run.assert_not_called()

    def test_created_file_is_valid_yaml(self, fake_repo, monkeypatch):
        import yaml

        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        docker.ensure_docker_config(fake_repo)
        content = (fake_repo / tasks.DOCKER_CONFIG).read_text()
        parsed = yaml.safe_load(content)
        assert parsed["schema_version"] == "1"
