"""Tests for the Click CLI entry point and cmd_list/cmd_status."""

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

import seekr_hatchery.docker as docker
import seekr_hatchery.tasks as tasks
from seekr_hatchery.cli import _WRAP_UP_PROMPT, _launch_finalize, _launch_new, _launch_resume, cli

# ---------------------------------------------------------------------------
# --version
# ---------------------------------------------------------------------------


class TestVersion:
    def test_version_flag(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "hatchery" in result.output
        # Version string should be present (any non-empty version)
        assert len(result.output.strip()) > 0

    def test_version_contains_version_string(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--version"])
        # Should output something like "hatchery, version X.Y.Z"
        assert result.exit_code == 0
        assert "version" in result.output.lower()


# ---------------------------------------------------------------------------
# --help
# ---------------------------------------------------------------------------


class TestHelp:
    def test_help_lists_commands(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        for cmd in ("new", "resume", "done", "sandbox", "archive", "delete", "list", "status", "config"):
            assert cmd in result.output

    def test_new_help_shows_from_option(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "--help"])
        assert result.exit_code == 0
        assert "--from" in result.output

    def test_new_help_shows_no_docker_option(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "--help"])
        assert result.exit_code == 0
        assert "--no-docker" in result.output

    def test_new_help_shows_no_worktree_option(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "--help"])
        assert result.exit_code == 0
        assert "--no-worktree" in result.output

    def test_new_help_shows_editor_options(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "--help"])
        assert result.exit_code == 0
        assert "--editor" in result.output
        assert "--no-editor" in result.output

    def test_missing_subcommand_shows_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, [])
        # Click exits 0 with --help, but with no args may show help or exit 0
        assert "Usage" in result.output or result.exit_code == 0


# ---------------------------------------------------------------------------
# CLI dispatch — new
# ---------------------------------------------------------------------------


def _new_patches():
    """Common context managers for cmd_new tests."""
    from unittest.mock import patch

    return [
        patch("seekr_hatchery.cli.git.git_root_or_cwd"),
        patch("seekr_hatchery.cli.tasks.ensure_gitignore"),
        patch("seekr_hatchery.cli.tasks.ensure_tasks_dir"),
        patch("seekr_hatchery.cli.docker.ensure_dockerfile"),
        patch("seekr_hatchery.cli.docker.ensure_docker_config"),
        patch("seekr_hatchery.cli.tasks.task_db_path"),
        patch("seekr_hatchery.cli.tasks.worktrees_dir"),
        patch("seekr_hatchery.cli.git.create_worktree"),
        patch("seekr_hatchery.cli.tasks.run"),  # git add / git commit in cmd_new
        patch("seekr_hatchery.cli.tasks.write_task_file"),
        patch("seekr_hatchery.cli.tasks.open_for_editing"),
        patch("seekr_hatchery.cli.tasks.save_task"),
        patch("seekr_hatchery.cli.docker.resolve_runtime"),
        patch("seekr_hatchery.cli._launch_new"),
        patch("seekr_hatchery.cli._prompt_objective", return_value="Default objective"),
    ]


class TestCliNew:
    def _setup_mocks(self, mocks):
        from unittest.mock import MagicMock

        (
            mock_root,
            _,
            _,
            _,
            _,
            mock_db_path,
            mock_wt_dir,
            mock_create_wt,
            mock_run,
            mock_write,
            _,
            mock_save,
            mock_docker,
            mock_launch,
            _,
        ) = mocks
        mock_root.return_value = (Path("/repo"), True)
        mock_db_path.return_value = MagicMock(exists=lambda: False)
        mock_wt_dir.return_value = Path("/repo/.hatchery/worktrees")
        mock_write.return_value = Path("/repo/.hatchery/tasks/task.md")
        mock_docker.return_value = None
        return mock_create_wt, mock_run, mock_save, mock_launch, mock_docker

    def test_new_dispatches_with_name(self):
        runner = CliRunner()
        from contextlib import ExitStack

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            _, _, _, mock_launch, _ = self._setup_mocks(mocks)
            result = runner.invoke(cli, ["new", "my-task"])

        assert result.exit_code == 0
        assert mock_launch.called

    def test_new_default_base_is_head(self):
        runner = CliRunner()
        from contextlib import ExitStack

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            mock_create_wt, _, mock_save, _, _ = self._setup_mocks(mocks)
            saved_meta = {}
            mock_save.side_effect = saved_meta.update
            runner.invoke(cli, ["new", "my-task"])

        # create_worktree called with base=DEFAULT_BASE ("HEAD")
        assert mock_create_wt.call_args[0][3] == tasks.DEFAULT_BASE

    def test_new_with_from_flag(self):
        runner = CliRunner()
        from contextlib import ExitStack

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            mock_create_wt, _, _, _, _ = self._setup_mocks(mocks)
            result = runner.invoke(cli, ["new", "my-task", "--from", "main"])

        assert result.exit_code == 0
        assert mock_create_wt.call_args[0][3] == "main"

    def test_new_with_no_docker(self):
        runner = CliRunner()
        from contextlib import ExitStack

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            _, _, _, _, mock_docker = self._setup_mocks(mocks)
            result = runner.invoke(cli, ["new", "my-task", "--no-docker"])

        assert result.exit_code == 0
        call_args = mock_docker.call_args
        assert call_args[0][2] is True or call_args[1].get("no_docker") is True

    def test_no_editor_skips_open_for_editing(self):
        runner = CliRunner()
        from contextlib import ExitStack

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                mock_db_path,
                mock_wt_dir,
                _,
                _,
                mock_write,
                mock_open_for_editing,
                _,
                mock_docker,
                _,
                mock_prompt,
            ) = mocks
            from unittest.mock import MagicMock

            mock_root.return_value = (Path("/repo"), True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = Path("/repo/.hatchery/worktrees")
            mock_write.return_value = Path("/repo/.hatchery/tasks/task.md")
            mock_docker.return_value = None
            mock_prompt.return_value = "Add a login page"
            result = runner.invoke(cli, ["new", "my-task", "--no-editor"])

        assert result.exit_code == 0
        assert not mock_open_for_editing.called
        # write_task_file should have been called with the user's description
        call_kwargs = mock_write.call_args
        assert call_kwargs[1].get("objective") == "Add a login page" or (
            len(call_kwargs[0]) > 3 and call_kwargs[0][3] == "Add a login page"
        )

    def test_default_uses_prompt_not_editor(self):
        """With no --editor/--no-editor flag, default config (open_editor=False) uses prompt."""
        runner = CliRunner()
        from contextlib import ExitStack

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                mock_db_path,
                mock_wt_dir,
                _,
                _,
                mock_write,
                mock_open_for_editing,
                _,
                mock_docker,
                _,
                mock_prompt,
            ) = mocks
            from unittest.mock import MagicMock

            mock_root.return_value = (Path("/repo"), True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = Path("/repo/.hatchery/worktrees")
            mock_write.return_value = Path("/repo/.hatchery/tasks/task.md")
            mock_docker.return_value = None
            result = runner.invoke(cli, ["new", "my-task"])

        assert result.exit_code == 0
        assert not mock_open_for_editing.called
        assert mock_prompt.called

    def test_editor_flag_opens_editor(self, tmp_path):
        """--editor flag explicitly opens the editor."""
        runner = CliRunner()
        from contextlib import ExitStack
        from unittest.mock import MagicMock

        # Create a real task file; open_for_editing mock will modify it
        task_file = tmp_path / "task.md"
        task_file.write_text("template content")

        def fake_open_for_editing(path):
            path.write_text("template content\n\nuser edits here")

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                mock_db_path,
                mock_wt_dir,
                _,
                _,
                mock_write,
                mock_open_for_editing,
                _,
                mock_docker,
                _,
                _,
            ) = mocks
            mock_root.return_value = (Path("/repo"), True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = Path("/repo/.hatchery/worktrees")
            mock_write.return_value = task_file
            mock_docker.return_value = None
            mock_open_for_editing.side_effect = fake_open_for_editing
            result = runner.invoke(cli, ["new", "my-task", "--editor"])

        assert result.exit_code == 0
        assert mock_open_for_editing.called

    def test_editor_unchanged_cancels(self, tmp_path):
        """When editor mode produces no changes, task is cancelled."""
        runner = CliRunner()
        from contextlib import ExitStack
        from unittest.mock import MagicMock

        # Create a real task file so read_text() returns consistent content
        task_file = tmp_path / "task.md"
        task_file.write_text("template content")

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                mock_db_path,
                mock_wt_dir,
                _,
                _,
                mock_write,
                mock_open_for_editing,
                _,
                mock_docker,
                mock_launch,
                _,
            ) = mocks
            mock_remove_wt = stack.enter_context(patch("seekr_hatchery.cli.git.remove_worktree"))
            mock_delete_br = stack.enter_context(patch("seekr_hatchery.cli.git.delete_branch"))
            mock_root.return_value = (Path("/repo"), True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = Path("/repo/.hatchery/worktrees")
            mock_write.return_value = task_file
            mock_docker.return_value = None
            # open_for_editing is a no-op, so file stays unchanged
            result = runner.invoke(cli, ["new", "my-task", "--editor"])

        assert result.exit_code == 1
        assert "unchanged" in result.output.lower()
        assert not mock_launch.called
        assert mock_remove_wt.called
        assert mock_delete_br.called


# ---------------------------------------------------------------------------
# config edit
# ---------------------------------------------------------------------------


class TestCmdConfigEdit:
    def test_happy_path(self, home):
        """Editor writes valid JSON → exit 0, 'Config updated' in output."""
        runner = CliRunner()

        def fake_editor(path):
            path.write_text('{"schema_version": "1", "default_agent": "CODEX", "open_editor": true}')

        with patch("seekr_hatchery.cli.tasks.open_for_editing", side_effect=fake_editor):
            result = runner.invoke(cli, ["config", "edit"])

        assert result.exit_code == 0
        assert "Config updated" in result.output

    def test_invalid_then_decline_restores_original(self, home):
        """Editor corrupts file, user declines → exit 1, original file restored."""
        runner = CliRunner()
        config_path = home / ".hatchery" / "config.json"
        # Write a v0 config (missing schema_version and open_editor)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        original = '{"default_agent": "CODEX"}'
        config_path.write_text(original)

        def fake_editor(path):
            path.write_text("not valid json {{")

        with patch("seekr_hatchery.cli.tasks.open_for_editing", side_effect=fake_editor):
            result = runner.invoke(cli, ["config", "edit"], input="n\n")

        assert result.exit_code == 1
        assert "Invalid config" in result.output
        assert "Restored" in result.output
        # Should restore the *original* file, not the migrated version
        assert config_path.read_text() == original
        assert not config_path.with_suffix(".json.bak").exists()

    def test_invalid_then_decline_no_prior_file(self, home):
        """No config existed before → decline removes the file entirely."""
        runner = CliRunner()
        config_path = home / ".hatchery" / "config.json"
        assert not config_path.exists()

        def fake_editor(path):
            path.write_text("not valid json {{")

        with patch("seekr_hatchery.cli.tasks.open_for_editing", side_effect=fake_editor):
            result = runner.invoke(cli, ["config", "edit"], input="n\n")

        assert result.exit_code == 1
        assert not config_path.exists()

    def test_invalid_then_fix_succeeds(self, home):
        """Editor writes bad JSON, user retries and fixes it → exit 0."""
        runner = CliRunner()
        call_count = 0

        def fake_editor(path):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                path.write_text("not valid json {{")
            else:
                path.write_text('{"schema_version": "1", "open_editor": true}')

        with patch("seekr_hatchery.cli.tasks.open_for_editing", side_effect=fake_editor):
            result = runner.invoke(cli, ["config", "edit"], input="\n")  # default=Y

        assert result.exit_code == 0
        assert "Invalid config" in result.output
        assert "Config updated" in result.output
        assert call_count == 2

    def test_missing_config_creates_defaults(self, home):
        """No existing file → defaults created, editor opens."""
        runner = CliRunner()
        config_path = home / ".hatchery" / "config.json"
        assert not config_path.exists()

        def fake_editor(path):
            pass  # leave defaults in place

        with patch("seekr_hatchery.cli.tasks.open_for_editing", side_effect=fake_editor):
            result = runner.invoke(cli, ["config", "edit"])

        assert result.exit_code == 0
        assert config_path.exists()

    def test_backup_cleaned_up_on_success(self, home):
        """On success, the .bak file should be removed."""
        runner = CliRunner()
        config_path = home / ".hatchery" / "config.json"

        def fake_editor(path):
            pass  # leave valid defaults

        with patch("seekr_hatchery.cli.tasks.open_for_editing", side_effect=fake_editor):
            result = runner.invoke(cli, ["config", "edit"])

        assert result.exit_code == 0
        assert not config_path.with_suffix(".json.bak").exists()


# ---------------------------------------------------------------------------
# CLI dispatch — resume
# ---------------------------------------------------------------------------


class TestCliResume:
    def test_resume_dispatched(self, fake_tasks_db):
        runner = CliRunner()

        with (
            patch("seekr_hatchery.cli.tasks.load_task") as mock_load,
            patch("seekr_hatchery.cli.docker.resolve_runtime") as mock_docker,
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli._launch_resume") as mock_launch,
        ):
            from unittest.mock import MagicMock

            worktree = MagicMock(spec=Path)
            worktree.exists.return_value = True
            mock_load.return_value = {
                "name": "my-task",
                "branch": "hatchery/my-task",
                "worktree": "/some/worktree",
                "repo": "/some/repo",
                "session_id": "sid-123",
            }
            mock_docker.return_value = None

            with patch("seekr_hatchery.cli.Path") as mock_path_cls:
                mock_path_cls.return_value = worktree
                runner.invoke(cli, ["resume", "my-task"])

        assert mock_launch.called

    def test_resume_with_no_docker(self, fake_tasks_db):
        runner = CliRunner()

        with (
            patch("seekr_hatchery.cli.tasks.load_task") as mock_load,
            patch("seekr_hatchery.cli.docker.resolve_runtime") as mock_docker,
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli._launch_resume"),
        ):
            from unittest.mock import MagicMock

            worktree = MagicMock(spec=Path)
            worktree.exists.return_value = True
            mock_load.return_value = {
                "name": "my-task",
                "branch": "hatchery/my-task",
                "worktree": "/some/worktree",
                "repo": "/some/repo",
                "session_id": "sid-123",
            }
            mock_docker.return_value = None

            with patch("seekr_hatchery.cli.Path") as mock_path_cls:
                mock_path_cls.return_value = worktree
                runner.invoke(cli, ["resume", "my-task", "--no-docker"])

        call_args = mock_docker.call_args
        assert call_args[0][2] is True or call_args[1].get("no_docker") is True


# ---------------------------------------------------------------------------
# CLI dispatch — sandbox
# ---------------------------------------------------------------------------


class TestSandbox:
    def test_sandbox_dispatches_to_launch_sandbox_shell(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        (repo / ".hatchery").mkdir(parents=True)

        runner = CliRunner()
        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(repo, True)),
            patch("seekr_hatchery.cli.docker.ensure_dockerfile", return_value=False),
            patch("seekr_hatchery.cli.docker.ensure_docker_config", return_value=False),
            patch("seekr_hatchery.cli.docker.detect_runtime", return_value=docker.Runtime.DOCKER),
            patch("seekr_hatchery.cli.docker.launch_sandbox_shell") as mock_launch,
        ):
            result = runner.invoke(cli, ["sandbox"])
            assert result.exit_code == 0, result.output
            assert mock_launch.called
            call_kwargs = mock_launch.call_args
            assert call_kwargs[0][0] == repo  # repo arg
            assert call_kwargs[1]["shell"] == "/bin/bash"  # default shell

    def test_sandbox_custom_shell(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        (repo / ".hatchery").mkdir(parents=True)

        runner = CliRunner()
        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(repo, True)),
            patch("seekr_hatchery.cli.docker.ensure_dockerfile", return_value=False),
            patch("seekr_hatchery.cli.docker.ensure_docker_config", return_value=False),
            patch("seekr_hatchery.cli.docker.detect_runtime", return_value=docker.Runtime.DOCKER),
            patch("seekr_hatchery.cli.docker.launch_sandbox_shell") as mock_launch,
        ):
            result = runner.invoke(cli, ["sandbox", "--shell", "/bin/sh"])
            assert result.exit_code == 0, result.output
            assert mock_launch.call_args[1]["shell"] == "/bin/sh"

    def test_sandbox_creates_dockerfile_when_missing(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        (repo / ".hatchery").mkdir(parents=True)

        runner = CliRunner()
        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(repo, True)),
            patch("seekr_hatchery.cli.docker.ensure_dockerfile", return_value=True) as mock_df,
            patch("seekr_hatchery.cli.docker.ensure_docker_config", return_value=False),
            patch("seekr_hatchery.cli.tasks.run"),
            patch("seekr_hatchery.cli.docker.detect_runtime", return_value=docker.Runtime.DOCKER),
            patch("seekr_hatchery.cli.docker.launch_sandbox_shell"),
        ):
            result = runner.invoke(cli, ["sandbox"])
            assert result.exit_code == 0, result.output
            assert mock_df.called


# ---------------------------------------------------------------------------
# cmd_list()
# ---------------------------------------------------------------------------


class TestCmdList:
    def test_no_tasks_message_default(self, monkeypatch, fake_tasks_db):
        runner = CliRunner()
        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd") as mock_root,
            patch("seekr_hatchery.cli.tasks.repo_tasks_for_current_repo") as mock_tasks,
        ):
            mock_root.return_value = (Path("/my/repo"), True)
            mock_tasks.return_value = []
            result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "No active tasks found" in result.output
        assert "--all" in result.output

    def test_no_tasks_message_all_flag(self, monkeypatch, fake_tasks_db):
        runner = CliRunner()
        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd") as mock_root,
            patch("seekr_hatchery.cli.tasks.repo_tasks_for_current_repo") as mock_tasks,
        ):
            mock_root.return_value = (Path("/my/repo"), True)
            mock_tasks.return_value = []
            result = runner.invoke(cli, ["list", "--all"])
        assert result.exit_code == 0
        assert "No tasks found for this repository." in result.output

    def test_single_task_shows_header_and_row(self, monkeypatch, fake_tasks_db):
        runner = CliRunner()
        task_list = [
            {
                "name": "my-task",
                "status": "in-progress",
                "created": "2026-01-15T10:00:00",
            }
        ]
        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd") as mock_root,
            patch("seekr_hatchery.cli.tasks.repo_tasks_for_current_repo") as mock_tasks,
        ):
            mock_root.return_value = (Path("/my/repo"), True)
            mock_tasks.return_value = task_list
            result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "my-task" in result.output
        assert "in-progress" in result.output
        assert "NAME" in result.output

    def test_created_truncated_to_10_chars(self, monkeypatch, fake_tasks_db):
        runner = CliRunner()
        task_list = [
            {
                "name": "my-task",
                "status": "in-progress",
                "created": "2026-01-15T10:00:00",
            }
        ]
        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd") as mock_root,
            patch("seekr_hatchery.cli.tasks.repo_tasks_for_current_repo") as mock_tasks,
        ):
            mock_root.return_value = (Path("/my/repo"), True)
            mock_tasks.return_value = task_list
            result = runner.invoke(cli, ["list"])
        assert "2026-01-15" in result.output
        assert "T10:00:00" not in result.output

    def test_multiple_tasks_all_flag(self, monkeypatch, fake_tasks_db):
        runner = CliRunner()
        task_list = [
            {"name": "task-one", "status": "in-progress", "created": "2026-01-02"},
            {"name": "task-two", "status": "complete", "created": "2026-01-01"},
        ]
        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd") as mock_root,
            patch("seekr_hatchery.cli.tasks.repo_tasks_for_current_repo") as mock_tasks,
        ):
            mock_root.return_value = (Path("/my/repo"), True)
            mock_tasks.return_value = task_list
            result = runner.invoke(cli, ["list", "--all"])
        assert "task-one" in result.output
        assert "task-two" in result.output

    def test_default_filters_to_in_progress(self, monkeypatch, fake_tasks_db):
        runner = CliRunner()
        task_list = [
            {"name": "task-one", "status": "in-progress", "created": "2026-01-02"},
            {"name": "task-two", "status": "complete", "created": "2026-01-01"},
        ]
        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd") as mock_root,
            patch("seekr_hatchery.cli.tasks.repo_tasks_for_current_repo") as mock_tasks,
        ):
            mock_root.return_value = (Path("/my/repo"), True)
            mock_tasks.return_value = task_list
            result = runner.invoke(cli, ["list"])
        assert "task-one" in result.output
        assert "task-two" not in result.output

    def test_columns_present_in_header(self, monkeypatch, fake_tasks_db):
        runner = CliRunner()
        task_list = [{"name": "t", "status": "in-progress", "created": "2026-01-01"}]
        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd") as mock_root,
            patch("seekr_hatchery.cli.tasks.repo_tasks_for_current_repo") as mock_tasks,
        ):
            mock_root.return_value = (Path("/my/repo"), True)
            mock_tasks.return_value = task_list
            result = runner.invoke(cli, ["list"])
        assert "STATUS" in result.output
        assert "CREATED" in result.output


# ---------------------------------------------------------------------------
# cmd_status()
# ---------------------------------------------------------------------------


_STATUS_REPO = Path("/my/repo")


class TestCmdStatus:
    def _make_task_meta(self, fake_tasks_db, worktree_path=None):
        """Save a task and return its meta."""
        meta = {
            "name": "test-task",
            "branch": "hatchery/test-task",
            "worktree": str(worktree_path or "/nonexistent/worktree"),
            "repo": str(_STATUS_REPO),
            "status": "in-progress",
            "created": "2026-01-15T10:30:00",
            "session_id": "session-uuid-abc",
        }
        tasks.save_task(meta)
        return meta

    def _invoke(self, runner, args):
        """Invoke CLI with git_root_or_cwd mocked to _STATUS_REPO."""
        with patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(_STATUS_REPO, True)):
            return runner.invoke(cli, args)

    def test_shows_name(self, fake_tasks_db):
        runner = CliRunner()
        self._make_task_meta(fake_tasks_db)
        result = self._invoke(runner, ["status", "test-task"])
        assert result.exit_code == 0
        assert "test-task" in result.output

    def test_shows_status(self, fake_tasks_db):
        runner = CliRunner()
        self._make_task_meta(fake_tasks_db)
        result = self._invoke(runner, ["status", "test-task"])
        assert "in-progress" in result.output

    def test_shows_branch(self, fake_tasks_db):
        runner = CliRunner()
        self._make_task_meta(fake_tasks_db)
        result = self._invoke(runner, ["status", "test-task"])
        assert "hatchery/test-task" in result.output

    def test_shows_worktree(self, fake_tasks_db):
        runner = CliRunner()
        self._make_task_meta(fake_tasks_db)
        result = self._invoke(runner, ["status", "test-task"])
        assert "worktree" in result.output.lower()

    def test_shows_session(self, fake_tasks_db):
        runner = CliRunner()
        self._make_task_meta(fake_tasks_db)
        result = self._invoke(runner, ["status", "test-task"])
        assert "session-uuid-abc" in result.output

    def test_task_file_not_accessible_message(self, fake_tasks_db):
        runner = CliRunner()
        self._make_task_meta(fake_tasks_db, worktree_path="/nonexistent/path")
        result = self._invoke(runner, ["status", "test-task"])
        assert "Task file not accessible" in result.output

    def test_shows_task_file_content_when_present(self, tmp_path, fake_tasks_db):
        runner = CliRunner()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        self._make_task_meta(fake_tasks_db, worktree_path=worktree)
        task_dir = worktree / ".hatchery" / "tasks"
        task_dir.mkdir(parents=True)
        real_name = tasks.task_file_name("test-task")
        real_file = task_dir / real_name
        real_file.write_text("# My Task File Content\nSome details here\n")
        result = self._invoke(runner, ["status", "test-task"])
        assert "My Task File Content" in result.output

    def test_created_truncated(self, fake_tasks_db):
        runner = CliRunner()
        self._make_task_meta(fake_tasks_db)
        result = self._invoke(runner, ["status", "test-task"])
        assert "2026-01-15T10:30" in result.output

    def test_completed_shown_when_present(self, fake_tasks_db):
        runner = CliRunner()
        meta = {
            "name": "done-task",
            "branch": "hatchery/done-task",
            "worktree": "/nonexistent",
            "repo": str(_STATUS_REPO),
            "status": "complete",
            "created": "2026-01-15T10:00:00",
            "completed": "2026-01-16T14:00:00",
            "session_id": "sid",
        }
        tasks.save_task(meta)
        result = self._invoke(runner, ["status", "done-task"])
        assert "2026-01-16T14:00" in result.output


# ---------------------------------------------------------------------------
# --no-worktree
# ---------------------------------------------------------------------------


class TestCliNoWorktree:
    """Tests for the --no-worktree flag and auto-enable in non-repo directories."""

    def _setup_no_worktree_mocks(self, mocks, in_repo: bool = True):
        from unittest.mock import MagicMock

        (
            mock_root,
            _,
            _,
            _,
            _,
            mock_db_path,
            mock_wt_dir,
            mock_create_wt,
            mock_run,
            mock_write,
            _,
            mock_save,
            mock_docker,
            mock_launch,
            _,
        ) = mocks
        mock_root.return_value = (Path("/repo"), in_repo)
        mock_db_path.return_value = MagicMock(exists=lambda: False)
        mock_wt_dir.return_value = Path("/repo/.hatchery/worktrees")
        mock_write.return_value = Path("/repo/.hatchery/tasks/task.md")
        mock_docker.return_value = None
        return mock_create_wt, mock_run, mock_save, mock_launch, mock_docker

    def test_no_worktree_flag_skips_create_worktree(self):
        runner = CliRunner()
        from contextlib import ExitStack

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            mock_create_wt, _, _, _, _ = self._setup_no_worktree_mocks(mocks)
            result = runner.invoke(cli, ["new", "my-task", "--no-worktree"])

        assert result.exit_code == 0
        assert not mock_create_wt.called

    def test_no_worktree_flag_stores_no_worktree_in_metadata(self):
        runner = CliRunner()
        from contextlib import ExitStack

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            _, _, mock_save, _, _ = self._setup_no_worktree_mocks(mocks)
            saved_meta = {}
            mock_save.side_effect = lambda m: saved_meta.update(m)
            runner.invoke(cli, ["new", "my-task", "--no-worktree"])

        assert saved_meta.get("no_worktree") is True

    def test_no_worktree_with_dockerfile_uses_launch_docker_no_worktree(self):
        runner = CliRunner()
        from contextlib import ExitStack
        from unittest.mock import patch

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            _, _, _, mock_launch, mock_docker = self._setup_no_worktree_mocks(mocks)
            mock_docker.return_value = docker.Runtime.DOCKER  # Dockerfile present → Docker
            stack.enter_context(patch("seekr_hatchery.cli.docker.launch_docker_no_worktree"))
            # Override _launch_new to NOT be mocked so Docker path is exercised
            mock_launch.side_effect = None  # still mocked; just verify docker call
            result = runner.invoke(cli, ["new", "my-task", "--no-worktree"])

        # _launch_new is still mocked, so we just verify the flag propagates
        assert result.exit_code == 0
        call_kwargs = mock_launch.call_args
        # The last positional arg or kwarg should be no_worktree=True
        args = call_kwargs[0] if call_kwargs[0] else []
        kwargs = call_kwargs[1] if call_kwargs[1] else {}
        assert True in args or kwargs.get("no_worktree") is True

    def test_auto_enable_when_not_in_repo(self):
        """When git_root_or_cwd returns in_repo=False, no_worktree is auto-enabled."""
        runner = CliRunner()
        from contextlib import ExitStack

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            mock_create_wt, _, mock_save, _, _ = self._setup_no_worktree_mocks(mocks, in_repo=False)
            saved_meta = {}
            mock_save.side_effect = lambda m: saved_meta.update(m)
            result = runner.invoke(cli, ["new", "my-task"])

        assert result.exit_code == 0
        # Worktree should NOT be created
        assert not mock_create_wt.called
        # Metadata should record no_worktree=True
        assert saved_meta.get("no_worktree") is True

    def test_auto_enable_prints_note_when_not_in_repo(self):
        runner = CliRunner()
        from contextlib import ExitStack

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            self._setup_no_worktree_mocks(mocks, in_repo=False)
            result = runner.invoke(cli, ["new", "my-task"])

        assert "not in a git repository" in result.output

    def test_resume_skips_worktree_check_in_no_worktree_mode(self, fake_tasks_db):
        """cmd_resume should not error on missing worktree when no_worktree=True."""
        runner = CliRunner()

        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(Path("/some/dir"), False)),
            patch("seekr_hatchery.cli.tasks.load_task") as mock_load,
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli._launch_resume") as mock_launch,
        ):
            mock_load.return_value = {
                "name": "my-task",
                "branch": "",
                "worktree": "/some/dir",
                "repo": "/some/dir",
                "session_id": "sid-456",
                "no_worktree": True,
            }
            # Even though Path("/some/dir") may not exist, no error should occur
            with patch("seekr_hatchery.cli.Path") as mock_path_cls:
                from unittest.mock import MagicMock

                mock_wt = MagicMock(spec=Path)
                mock_wt.exists.return_value = False  # worktree doesn't exist
                mock_path_cls.return_value = mock_wt
                runner.invoke(cli, ["resume", "my-task"])

        assert mock_launch.called


# ---------------------------------------------------------------------------
# cmd_self_update()
# ---------------------------------------------------------------------------


class TestCmdSelfUpdate:
    def test_runs_uv_upgrade_when_receipt_exists(self, tmp_path):
        receipt = tmp_path / ".local/share/uv/tools/seekr-hatchery/uv-receipt.toml"
        receipt.parent.mkdir(parents=True)
        receipt.touch()

        runner = CliRunner()
        with (
            patch("seekr_hatchery.cli.Path.home", return_value=tmp_path),
            patch("seekr_hatchery.cli.shutil.which", return_value="/usr/bin/uv"),
            patch("seekr_hatchery.cli.subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            result = runner.invoke(cli, ["self", "update"])

        mock_run.assert_called_once_with(["uv", "tool", "upgrade", "seekr-hatchery"])
        assert result.exit_code == 0

    def test_exits_with_uv_return_code(self, tmp_path):
        receipt = tmp_path / ".local/share/uv/tools/seekr-hatchery/uv-receipt.toml"
        receipt.parent.mkdir(parents=True)
        receipt.touch()

        runner = CliRunner()
        with (
            patch("seekr_hatchery.cli.Path.home", return_value=tmp_path),
            patch("seekr_hatchery.cli.shutil.which", return_value="/usr/bin/uv"),
            patch("seekr_hatchery.cli.subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 1
            result = runner.invoke(cli, ["self", "update"])

        assert result.exit_code == 1

    def test_error_when_receipt_missing(self, tmp_path):
        runner = CliRunner()
        with (
            patch("seekr_hatchery.cli.Path.home", return_value=tmp_path),
            patch("seekr_hatchery.cli.shutil.which", return_value="/usr/bin/uv"),
        ):
            result = runner.invoke(cli, ["self", "update"])

        assert result.exit_code == 1
        assert "uv tool upgrade seekr-hatchery" in result.output

    def test_error_when_uv_not_on_path(self, tmp_path):
        receipt = tmp_path / ".local/share/uv/tools/seekr-hatchery/uv-receipt.toml"
        receipt.parent.mkdir(parents=True)
        receipt.touch()

        runner = CliRunner()
        with (
            patch("seekr_hatchery.cli.Path.home", return_value=tmp_path),
            patch("seekr_hatchery.cli.shutil.which", return_value=None),
        ):
            result = runner.invoke(cli, ["self", "update"])

        assert result.exit_code == 1
        assert "uv tool upgrade seekr-hatchery" in result.output


# ---------------------------------------------------------------------------
# Backend lifecycle dispatch
# ---------------------------------------------------------------------------


class TestLaunchHooks:
    """Assert that the CLI calls the correct backend hooks and command builders."""

    def _patches(self) -> list:
        return [
            patch("seekr_hatchery.cli.tasks.task_session_dir", return_value=Path("/session")),
            patch("seekr_hatchery.cli.tasks.sandbox_context", return_value="ctx"),
            patch("seekr_hatchery.cli.tasks.SESSION_SYSTEM", "sys"),
            patch("seekr_hatchery.cli.tasks.session_prompt", return_value="prompt"),
            patch("seekr_hatchery.cli.subprocess.run"),
            patch(
                "seekr_hatchery.cli.tasks.load_task",
                return_value={"name": "t", "status": "in-progress", "branch": "b"},
            ),
            patch("seekr_hatchery.cli.tasks.save_task"),
            patch("seekr_hatchery.cli._post_exit_check"),
            patch("seekr_hatchery.cli.os.chdir"),
        ]

    def test_launch_new_hook_order(self, spy_backend):
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)
            _launch_new(
                repo=Path("/repo"),
                worktree=Path("/worktree"),
                name="t",
                session_id="sid",
                backend=spy_backend,
                runtime=None,
                branch="b",
                main_branch="main",
            )

        call_names = [c[0] for c in spy_backend.calls]
        assert call_names == ["on_new_task", "on_before_launch", "build_new_command"]
        _, session_dir = spy_backend.calls[0]
        assert session_dir == Path("/session")
        _, worktree = spy_backend.calls[1]
        assert worktree == Path("/worktree")
        _, sid, _sys, _init, docker, workdir = spy_backend.calls[2]
        assert sid == "sid"
        assert docker is False
        assert workdir == ""

    def test_launch_resume_hook_order(self, spy_backend):
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)
            _launch_resume(
                repo=Path("/repo"),
                worktree=Path("/worktree"),
                name="t",
                session_id="sid",
                backend=spy_backend,
                runtime=None,
                branch="b",
                main_branch="main",
            )

        call_names = [c[0] for c in spy_backend.calls]
        assert call_names == ["on_before_launch", "build_resume_command"]
        _, worktree = spy_backend.calls[0]
        assert worktree == Path("/worktree")
        _, sid, _sys, _init, docker, workdir = spy_backend.calls[1]
        assert sid == "sid"
        assert docker is False
        assert workdir == ""

    def test_launch_finalize_no_hooks(self, spy_backend):
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)
            _launch_finalize(
                repo=Path("/repo"),
                worktree=Path("/worktree"),
                name="t",
                session_id="sid",
                backend=spy_backend,
                runtime=None,
                branch="b",
                main_branch="main",
            )

        call_names = [c[0] for c in spy_backend.calls]
        assert call_names == ["build_finalize_command"]
        _, sid, _sys, wrap_up, docker, workdir = spy_backend.calls[0]
        assert sid == "sid"
        assert wrap_up == _WRAP_UP_PROMPT
        assert docker is False
        assert workdir == ""


# ---------------------------------------------------------------------------
# running state
# ---------------------------------------------------------------------------


class TestRunningState:
    def test_running_task_appears_in_default_list(self, monkeypatch, fake_tasks_db):
        runner = CliRunner()
        task_list = [{"name": "active-task", "status": "running", "created": "2026-01-15T10:00:00"}]
        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd") as mock_root,
            patch("seekr_hatchery.cli.tasks.repo_tasks_for_current_repo") as mock_tasks,
        ):
            mock_root.return_value = (Path("/my/repo"), True)
            mock_tasks.return_value = task_list
            result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "active-task" in result.output
        assert "running" in result.output

    def test_in_progress_still_appears_in_default_list(self, monkeypatch, fake_tasks_db):
        runner = CliRunner()
        task_list = [{"name": "paused-task", "status": "in-progress", "created": "2026-01-15T10:00:00"}]
        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd") as mock_root,
            patch("seekr_hatchery.cli.tasks.repo_tasks_for_current_repo") as mock_tasks,
        ):
            mock_root.return_value = (Path("/my/repo"), True)
            mock_tasks.return_value = task_list
            result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "paused-task" in result.output

    def test_running_state_blocked_by_new_duplicate_check(self):
        runner = CliRunner()
        from contextlib import ExitStack
        from unittest.mock import MagicMock

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            mock_root = mocks[0]
            mock_db_path = mocks[5]
            mock_root.return_value = (Path("/repo"), True)
            db_mock = MagicMock()
            db_mock.exists.return_value = True
            db_mock.read_text.return_value = json.dumps({"status": "running"})
            mock_db_path.return_value = db_mock
            result = runner.invoke(cli, ["new", "my-task"])

        assert result.exit_code == 1

    def test_launch_new_sets_running_then_restores_in_progress(self, spy_backend):
        """After _launch_new exits, status should go running then in-progress."""
        statuses_saved: list[str] = []

        def fake_load_task(repo: Path, name: str) -> dict:
            return {"name": name, "status": "in-progress", "branch": "hatchery/x"}

        def fake_save_task(meta: dict) -> None:
            statuses_saved.append(meta["status"])

        with (
            patch("seekr_hatchery.cli.tasks.task_session_dir", return_value=Path("/session")),
            patch("seekr_hatchery.cli.tasks.sandbox_context", return_value="ctx"),
            patch("seekr_hatchery.cli.tasks.SESSION_SYSTEM", "sys"),
            patch("seekr_hatchery.cli.tasks.session_prompt", return_value="prompt"),
            patch("seekr_hatchery.cli.subprocess.run"),
            patch("seekr_hatchery.cli.tasks.load_task", side_effect=fake_load_task),
            patch("seekr_hatchery.cli.tasks.save_task", side_effect=fake_save_task),
            patch("seekr_hatchery.cli._post_exit_check"),
            patch("seekr_hatchery.cli.os.chdir"),
        ):
            _launch_new(
                repo=Path("/repo"),
                worktree=Path("/worktree"),
                name="my-task",
                session_id="sid-123",
                backend=spy_backend,
                runtime=None,
                branch="hatchery/my-task",
                main_branch="main",
            )

        assert statuses_saved == ["running", "in-progress"]
