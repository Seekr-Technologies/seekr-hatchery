"""Tests for the Click CLI entry point and cmd_list/cmd_status."""

import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

import seekr_hatchery.constants as constants
import seekr_hatchery.docker as docker
import seekr_hatchery.sessions as sessions
import seekr_hatchery.utils as utils
from seekr_hatchery.cli import (
    TaskNameType,
    cli,
)
from seekr_hatchery.includes import IncludeEntry
from seekr_hatchery.sessions import _WRAP_UP_PROMPT, next_chat_name


def _launch_meta(
    name="t", repo="/repo", worktree="/worktree", branch="b", is_chat=False, no_worktree=False, agent_kind="CODEX"
):
    """Build a SessionMeta for sessions.launch hook/chat/status tests."""
    return sessions.SessionMeta(
        name=name,
        repo=repo,
        worktree=worktree,
        branch=branch,
        type="chat" if is_chat else "task",
        no_worktree=no_worktree,
        agent=agent_kind,
    )


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

        expected_commands = {
            "archive",
            "chat",
            "config",
            "delete",
            "done",
            "exec",
            "ls | list",
            "logs",
            "new",
            "resume",
            "sandbox",
            "self",
            "shell",
            "st | status",
        }

        commands = result.output.split("Commands:\n")[-1]
        commands = commands.split("\n")
        # Each line may show "alias | command  help text"; collect all pipe-separated names.
        actual_commands: set[str] = set()
        for line in commands:
            if not line:
                continue
            name_part = line.split("  ")[1]  # strip help text
            actual_commands.update({name_part})

        assert expected_commands == actual_commands

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
    """Common context managers for cmd_new tests.

    ``run`` is patched on every module that has a local ``from utils import run``
    binding (sessions, git). Both patches share a single MagicMock so tests can
    read all git invocations from one call_args_list — including the bare
    ``run([\"git\", \"add\", ...])`` issued indirectly by ``git.add_and_commit``.
    """
    shared_run = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
    return [
        patch("seekr_hatchery.cli.git.git_root_or_cwd"),
        patch("seekr_hatchery.cli.sessions.ensure_gitignore"),
        patch("seekr_hatchery.cli.sessions.ensure_tasks_dir"),
        patch("seekr_hatchery.cli.sessions.ensure_repo_store"),
        patch("seekr_hatchery.cli.sessions.ensure_git_exclude"),
        patch("seekr_hatchery.cli.docker.ensure_dockerfile"),
        patch("seekr_hatchery.cli.docker.ensure_docker_config"),
        patch("seekr_hatchery.cli.sessions.task_db_path"),
        patch("seekr_hatchery.cli.sessions.worktrees_dir"),
        patch("seekr_hatchery.cli.git.create_worktree"),
        patch("seekr_hatchery.sessions.run", shared_run),  # mock_run — both sessions and git share
        patch("seekr_hatchery.cli.sessions.write_task_file"),
        patch("seekr_hatchery.sessions.open_for_editing"),
        patch("seekr_hatchery.cli.sessions.save_task"),
        patch("seekr_hatchery.cli.docker.resolve_runtime"),
        patch("seekr_hatchery.cli._launch"),
        patch("seekr_hatchery.cli._prompt_objective", return_value="Default objective"),
        patch("seekr_hatchery.git.run", shared_run),  # same mock; git.add_and_commit flows through
    ]


class TestCliNew:
    def _setup_mocks(self, mocks):

        (
            mock_root,
            _,
            _,
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

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            _, _, _, mock_launch, _ = self._setup_mocks(mocks)
            result = runner.invoke(cli, ["new", "my-task"])

        assert result.exit_code == 0
        assert mock_launch.called

    def test_new_default_base_fetches_and_uses_origin(self):
        """Without --from, cmd_new fetches origin and bases the branch on origin/<default>."""
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            mock_create_wt, mock_run, _, _, _ = self._setup_mocks(mocks)
            # Simulate successful fetch; get_default_branch returns "main".
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            stack.enter_context(patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"))
            runner.invoke(cli, ["new", "my-task"])

        assert mock_create_wt.call_args[0][3] == "origin/main"

    def test_new_default_base_falls_back_to_head_when_fetch_fails(self):
        """If git fetch fails (offline/no remote), HEAD is used as base."""
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            mock_create_wt, mock_run, _, _, _ = self._setup_mocks(mocks)
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            stack.enter_context(patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"))
            runner.invoke(cli, ["new", "my-task"])

        assert mock_create_wt.call_args[0][3] == constants.DEFAULT_BASE

    def test_new_with_from_flag(self):
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            mock_create_wt, _, _, _, _ = self._setup_mocks(mocks)
            result = runner.invoke(cli, ["new", "my-task", "--from", "main"])

        assert result.exit_code == 0
        assert mock_create_wt.call_args[0][3] == "main"

    def test_new_with_no_docker(self):
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            _, _, _, _, mock_docker = self._setup_mocks(mocks)
            result = runner.invoke(cli, ["new", "my-task", "--no-docker"])

        assert result.exit_code == 0
        call_args = mock_docker.call_args
        assert call_args[0][1] is True or call_args[1].get("no_docker") is True

    def test_no_editor_skips_open_for_editing(self):
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
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
                _,
            ) = mocks

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

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
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
                _,
            ) = mocks

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

    def test_dockerfile_checked_against_worktree_not_repo(self):
        """ensure_dockerfile/config are called with the worktree path, not repo root."""
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                mock_ensure_df,
                mock_ensure_dc,
                mock_db_path,
                mock_wt_dir,
                _,
                _,
                mock_write,
                _,
                _,
                mock_docker,
                _,
                _,
                _,
            ) = mocks
            mock_root.return_value = (Path("/repo"), True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = Path("/repo/.hatchery/worktrees")
            mock_write.return_value = Path("/repo/.hatchery/tasks/task.md")
            mock_docker.return_value = None
            mock_ensure_df.return_value = False
            mock_ensure_dc.return_value = False
            runner.invoke(cli, ["new", "my-task"])

        worktree = Path("/repo/.hatchery/worktrees/my-task")
        assert mock_ensure_df.call_args[0][0] == worktree
        assert mock_ensure_dc.call_args[0][0] == worktree

    def test_keyboard_interrupt_cleans_up_worktree(self):
        """Ctrl-C after worktree creation removes the worktree and branch."""
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                mock_ensure_df,
                _,
                mock_db_path,
                mock_wt_dir,
                _,
                _,
                mock_write,
                _,
                _,
                mock_docker,
                _,
                _,
                _,
            ) = mocks
            mock_remove_wt = stack.enter_context(patch("seekr_hatchery.cli.git.remove_worktree"))
            mock_delete_br = stack.enter_context(patch("seekr_hatchery.cli.git.delete_branch"))
            mock_root.return_value = (Path("/repo"), True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = Path("/repo/.hatchery/worktrees")
            mock_write.return_value = Path("/repo/.hatchery/tasks/task.md")
            mock_docker.return_value = None
            mock_ensure_df.side_effect = KeyboardInterrupt
            result = runner.invoke(cli, ["new", "my-task"])

        assert result.exit_code == 1
        assert mock_remove_wt.called
        assert mock_delete_br.called

    def test_keyboard_interrupt_no_worktree_skips_cleanup(self):
        """Ctrl-C with --no-worktree does not attempt to remove any worktree."""
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                mock_ensure_df,
                _,
                mock_db_path,
                mock_wt_dir,
                _,
                _,
                mock_write,
                _,
                _,
                mock_docker,
                _,
                _,
                _,
            ) = mocks
            mock_remove_wt = stack.enter_context(patch("seekr_hatchery.cli.git.remove_worktree"))
            mock_delete_br = stack.enter_context(patch("seekr_hatchery.cli.git.delete_branch"))
            mock_root.return_value = (Path("/repo"), True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = Path("/repo/.hatchery/worktrees")
            mock_write.return_value = Path("/repo/.hatchery/tasks/task.md")
            mock_docker.return_value = None
            mock_ensure_df.side_effect = KeyboardInterrupt
            result = runner.invoke(cli, ["new", "my-task", "--no-worktree"])

        assert result.exit_code == 1
        assert not mock_remove_wt.called
        assert not mock_delete_br.called

    def test_committed_docker_commits_when_created(self):
        """Without the flag, a newly generated Dockerfile is committed as normal."""
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                mock_ensure_df,
                mock_ensure_dc,
                mock_db_path,
                mock_wt_dir,
                _,
                mock_run,
                mock_write,
                _,
                _,
                mock_docker,
                _,
                _,
                _,
            ) = mocks
            repo = Path("/repo")
            mock_root.return_value = (repo, True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = repo / ".hatchery/worktrees"
            mock_write.return_value = repo / ".hatchery/tasks/task.md"
            mock_docker.return_value = None
            mock_ensure_df.return_value = True
            mock_ensure_dc.return_value = False
            result = runner.invoke(cli, ["new", "my-task"])

        assert result.exit_code == 0
        # ensure_dockerfile called exactly once
        assert mock_ensure_df.call_count == 1
        dockerfile_commits = [
            c
            for c in mock_run.call_args_list
            if c[0][0] == ["git", "commit", "-m", "chore: add hatchery Docker configuration"]
        ]
        assert len(dockerfile_commits) == 1

    def test_no_flags_dockerfile_exists_at_root_not_committed(self):
        """Without flags, a Dockerfile copied from the repo root (ensure returns False)
        is not committed — only the task file is."""
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                mock_ensure_df,
                mock_ensure_dc,
                mock_db_path,
                mock_wt_dir,
                _,
                mock_run,
                mock_write,
                _,
                _,
                mock_docker,
                _,
                _,
                _,
            ) = mocks
            repo = Path("/repo")
            mock_root.return_value = (repo, True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = repo / ".hatchery/worktrees"
            mock_write.return_value = repo / ".hatchery/tasks/task.md"
            mock_docker.return_value = None
            # ensure_dockerfile returns False: file copied from repo root, not newly created
            mock_ensure_df.return_value = False
            mock_ensure_dc.return_value = False
            result = runner.invoke(cli, ["new", "my-task"])

        assert result.exit_code == 0
        dockerfile_commits = [
            c
            for c in mock_run.call_args_list
            if c[0][0] == ["git", "commit", "-m", "chore: add hatchery Docker configuration"]
        ]
        assert len(dockerfile_commits) == 0
        task_file_commits = [
            c
            for c in mock_run.call_args_list
            if len(c[0][0]) >= 4 and c[0][0][:3] == ["git", "commit", "-m"] and "add task file" in c[0][0][3]
        ]
        assert len(task_file_commits) == 1

    def test_no_commit_skips_all_commits_dockerfile_new(self):
        """--no-commit skips docker and task-file commits even when Dockerfile is brand-new."""
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                mock_ensure_df,
                mock_ensure_dc,
                mock_db_path,
                mock_wt_dir,
                _,
                mock_run,
                mock_write,
                _,
                mock_save,
                mock_docker,
                _,
                _,
                _,
            ) = mocks
            repo = Path("/repo")
            mock_root.return_value = (repo, True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = repo / ".hatchery/worktrees"
            mock_write.return_value = repo / ".hatchery/tasks/task.md"
            mock_docker.return_value = None
            # Single call to the store
            mock_ensure_df.return_value = True
            mock_ensure_dc.return_value = True
            result = runner.invoke(cli, ["new", "my-task", "--no-commit"])

        assert result.exit_code == 0
        all_commits = [c for c in mock_run.call_args_list if "commit" in c[0][0]]
        assert len(all_commits) == 0

    def test_no_commit_skips_all_commits_dockerfile_exists(self):
        """--no-commit skips all commits when Dockerfile already exists at repo root."""
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                mock_ensure_df,
                mock_ensure_dc,
                mock_db_path,
                mock_wt_dir,
                _,
                mock_run,
                mock_write,
                _,
                mock_save,
                mock_docker,
                _,
                _,
                _,
            ) = mocks
            repo = Path("/repo")
            mock_root.return_value = (repo, True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = repo / ".hatchery/worktrees"
            mock_write.return_value = repo / ".hatchery/tasks/task.md"
            mock_docker.return_value = None
            mock_ensure_df.return_value = False
            mock_ensure_dc.return_value = False
            result = runner.invoke(cli, ["new", "my-task", "--no-commit"])

        assert result.exit_code == 0
        all_commits = [c for c in mock_run.call_args_list if "commit" in c[0][0]]
        assert len(all_commits) == 0

    def test_no_commit_saves_metadata_flag(self):
        """--no-commit persists no_commit=True in task metadata."""
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                mock_ensure_df,
                mock_ensure_dc,
                mock_db_path,
                mock_wt_dir,
                _,
                _,
                mock_write,
                _,
                mock_save,
                mock_docker,
                _,
                _,
                _,
            ) = mocks
            repo = Path("/repo")
            mock_root.return_value = (repo, True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = repo / ".hatchery/worktrees"
            mock_write.return_value = repo / ".hatchery/tasks/task.md"
            mock_docker.return_value = None
            mock_ensure_df.return_value = False
            mock_ensure_dc.return_value = False
            result = runner.invoke(cli, ["new", "my-task", "--no-commit"])

        assert result.exit_code == 0
        saved_meta = mock_save.call_args[0][0]
        assert saved_meta.get("no_commit") is True

    def test_no_commit_help_text(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "--help"])
        assert result.exit_code == 0
        assert "--no-commit" in result.output


# ---------------------------------------------------------------------------
# config edit
# ---------------------------------------------------------------------------


class TestCmdConfigEdit:
    def test_happy_path(self, home):
        """Editor writes valid JSON → exit 0, 'Config updated' in output."""
        runner = CliRunner()

        def fake_editor(path):
            path.write_text('{"schema_version": "1", "default_agent": "CODEX", "open_editor": true}')

        with patch("seekr_hatchery.cli.open_for_editing", side_effect=fake_editor):
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

        with patch("seekr_hatchery.cli.open_for_editing", side_effect=fake_editor):
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

        with patch("seekr_hatchery.cli.open_for_editing", side_effect=fake_editor):
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

        with patch("seekr_hatchery.cli.open_for_editing", side_effect=fake_editor):
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

        with patch("seekr_hatchery.cli.open_for_editing", side_effect=fake_editor):
            result = runner.invoke(cli, ["config", "edit"])

        assert result.exit_code == 0
        assert config_path.exists()

    def test_backup_cleaned_up_on_success(self, home):
        """On success, the .bak file should be removed."""
        runner = CliRunner()
        config_path = home / ".hatchery" / "config.json"

        def fake_editor(path):
            pass  # leave valid defaults

        with patch("seekr_hatchery.cli.open_for_editing", side_effect=fake_editor):
            result = runner.invoke(cli, ["config", "edit"])

        assert result.exit_code == 0
        assert not config_path.with_suffix(".json.bak").exists()


# ---------------------------------------------------------------------------
# CLI dispatch — resume
# ---------------------------------------------------------------------------


def _resume_meta(worktree: Path, **overrides) -> sessions.SessionMeta:
    """Build a SessionMeta for cmd_resume tests against a real tmpdir worktree."""
    kwargs = {
        "name": "my-task",
        "branch": "hatchery/my-task",
        "worktree": str(worktree),
        "repo": str(worktree.parent),
        "session_id": "sid-123",
        "no_worktree": False,
    }
    kwargs.update(overrides)
    return sessions.SessionMeta(**kwargs)


class TestCliResume:
    def test_resume_dispatched(self, tmp_path, fake_tasks_db):
        runner = CliRunner()
        wt = tmp_path / "wt"
        wt.mkdir()
        meta = _resume_meta(wt)

        with (
            patch("seekr_hatchery.cli.sessions.load", return_value=meta),
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.sessions.docker.ensure_dockerfile"),
            patch("seekr_hatchery.cli.sessions.docker.ensure_docker_config"),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli._launch") as mock_launch,
        ):
            runner.invoke(cli, ["resume", "my-task"])

        assert mock_launch.called

    def test_resume_with_no_docker(self, tmp_path, fake_tasks_db):
        runner = CliRunner()
        wt = tmp_path / "wt"
        wt.mkdir()
        meta = _resume_meta(wt)

        with (
            patch("seekr_hatchery.cli.sessions.load", return_value=meta),
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None) as mock_docker,
            patch("seekr_hatchery.cli.sessions.docker.ensure_dockerfile"),
            patch("seekr_hatchery.cli.sessions.docker.ensure_docker_config"),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli._launch"),
        ):
            runner.invoke(cli, ["resume", "my-task", "--no-docker"])

        call_args = mock_docker.call_args
        assert call_args[0][1] is True or call_args[1].get("no_docker") is True

    def test_resume_restores_missing_dockerfile(self, tmp_path, fake_tasks_db):
        """cmd_resume restores Docker files when Dockerfile is missing from worktree."""
        runner = CliRunner()
        wt = tmp_path / "wt"
        wt.mkdir()
        meta = _resume_meta(wt)

        missing_df = MagicMock()
        missing_df.exists.return_value = False

        with (
            patch("seekr_hatchery.cli.sessions.load", return_value=meta),
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.docker.dockerfile_path", return_value=missing_df),
            patch("seekr_hatchery.cli.sessions.docker.ensure_dockerfile") as mock_ensure,
            patch("seekr_hatchery.cli.sessions.docker.ensure_docker_config"),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli._launch"),
        ):
            result = runner.invoke(cli, ["resume", "my-task"])

        assert mock_ensure.called
        assert result.exit_code == 0

    def test_resume_skips_restore_when_dockerfile_present(self, tmp_path, fake_tasks_db):
        """cmd_resume does not call ensure_docker_files_uncommitted when Dockerfile exists."""
        runner = CliRunner()
        wt = tmp_path / "wt"
        wt.mkdir()
        meta = _resume_meta(wt)
        (wt / ".hatchery").mkdir()
        (wt / ".hatchery" / "Dockerfile.codex").write_text("FROM scratch\n")

        with (
            patch("seekr_hatchery.cli.sessions.load", return_value=meta),
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.sessions.docker.ensure_dockerfile") as mock_ensure,
            patch("seekr_hatchery.cli.sessions.docker.ensure_docker_config"),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli._launch"),
        ):
            runner.invoke(cli, ["resume", "my-task"])

        assert not mock_ensure.called

    def test_resume_skips_restore_with_no_docker_flag(self, tmp_path, fake_tasks_db):
        """--no-docker skips the Dockerfile restoration entirely."""
        runner = CliRunner()
        wt = tmp_path / "wt"
        wt.mkdir()
        meta = _resume_meta(wt)

        with (
            patch("seekr_hatchery.cli.sessions.load", return_value=meta),
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.sessions.docker.ensure_dockerfile") as mock_ensure,
            patch("seekr_hatchery.cli.sessions.docker.ensure_docker_config"),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli._launch"),
        ):
            runner.invoke(cli, ["resume", "my-task", "--no-docker"])

        assert not mock_ensure.called

    # -- Degraded-state resume scenarios --------------------------------------
    #
    # These exercise CLI-only behavior (the y/N prompt, EOF/cancel handling,
    # exit codes) against a *real* git repo via the `git_repo` fixture —
    # base-ref resolution correctness (branch missing locally/on origin,
    # falling back to the default branch, etc.) is unit-tested directly in
    # test_session_io.py's TestResolveRecreateBase / TestRestoreWorktreeIfNeeded
    # and isn't re-asserted here.

    def test_resume_missing_worktree_confirm_yes_recreates(self, git_repo, fake_tasks_db):
        """In-progress + missing worktree + 'y' at the prompt → worktree
        is recreated (via real git) and the launch proceeds."""
        runner = CliRunner()
        wt = git_repo / ".hatchery" / "worktrees" / "my-task"
        utils.run(["git", "branch", "hatchery/my-task"], cwd=git_repo)
        meta = _resume_meta(wt, repo=str(git_repo), branch="hatchery/my-task", status="in-progress")
        sessions.save(meta)

        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(git_repo, True)),
            patch("seekr_hatchery.cli._launch") as mock_launch,
        ):
            result = runner.invoke(cli, ["resume", "my-task", "--no-docker"], input="y\n")

        assert result.exit_code == 0, result.output
        assert wt.exists()
        assert mock_launch.called

    def test_resume_missing_worktree_eof_aborts_cleanly(self, git_repo, fake_tasks_db):
        """Non-interactive stdin (CI / nohup): input() raises EOFError; cmd_resume
        must convert that to SessionCancelled → exit 1, not a Python traceback."""
        runner = CliRunner()
        wt = git_repo / ".hatchery" / "worktrees" / "my-task"
        utils.run(["git", "branch", "hatchery/my-task"], cwd=git_repo)
        meta = _resume_meta(wt, repo=str(git_repo), branch="hatchery/my-task", status="in-progress")
        sessions.save(meta)

        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(git_repo, True)),
            patch("seekr_hatchery.cli._launch") as mock_launch,
        ):
            # No input= argument → CliRunner closes stdin → input() raises EOFError.
            result = runner.invoke(cli, ["resume", "my-task"])

        assert result.exit_code == 1, result.output
        assert not wt.exists()
        assert not mock_launch.called
        # No Python traceback should appear.
        assert "Traceback" not in result.output

    def test_resume_missing_worktree_confirm_no_aborts(self, git_repo, fake_tasks_db):
        """In-progress + missing worktree + 'n' → no recreate, no launch,
        exit 1 so shell chains stop on user-cancelled resume."""
        runner = CliRunner()
        wt = git_repo / ".hatchery" / "worktrees" / "my-task"
        utils.run(["git", "branch", "hatchery/my-task"], cwd=git_repo)
        meta = _resume_meta(wt, repo=str(git_repo), branch="hatchery/my-task", status="in-progress")
        sessions.save(meta)

        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(git_repo, True)),
            patch("seekr_hatchery.cli._launch") as mock_launch,
        ):
            result = runner.invoke(cli, ["resume", "my-task"], input="n\n")

        assert result.exit_code == 1, result.output
        assert not wt.exists()
        assert not mock_launch.called
        assert "Aborted" in result.output

    def test_resume_missing_session_id_falls_back_to_new(self, tmp_path, fake_tasks_db):
        """Worktree present but session_id is empty → launch with kind='new'
        and a freshly-generated session_id that gets persisted to meta."""
        runner = CliRunner()
        wt = tmp_path / "wt"
        wt.mkdir()
        meta = _resume_meta(wt, session_id="")

        with (
            patch("seekr_hatchery.cli.sessions.load", return_value=meta),
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.sessions.docker.ensure_dockerfile"),
            patch("seekr_hatchery.cli.sessions.docker.ensure_docker_config"),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli._launch") as mock_launch,
        ):
            result = runner.invoke(cli, ["resume", "my-task"])

        assert result.exit_code == 0, result.output
        assert mock_launch.called
        assert mock_launch.call_args.kwargs["kind"] == "new"
        # A fresh uuid was generated and threaded into _launch.
        sid = mock_launch.call_args.kwargs["session_id"]
        assert sid and sid != ""
        # And the same id is now on meta (so next resume is idempotent).
        assert meta.session_id == sid


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
            patch("seekr_hatchery.cli.docker.detect_runtime", return_value=docker.DockerRuntime()),
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
            patch("seekr_hatchery.cli.docker.detect_runtime", return_value=docker.DockerRuntime()),
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
            patch("seekr_hatchery.cli.git.add_and_commit"),
            patch("seekr_hatchery.cli.docker.detect_runtime", return_value=docker.DockerRuntime()),
            patch("seekr_hatchery.cli.docker.launch_sandbox_shell"),
        ):
            result = runner.invoke(cli, ["sandbox"])
            assert result.exit_code == 0, result.output
            assert mock_df.called


    def test_sandbox_no_commit_writes_docker_to_store_not_repo(self, tmp_path, monkeypatch):
        """sandbox --no-commit writes docker files to the store, not the repo."""
        repo = tmp_path / "repo"
        (repo / ".hatchery").mkdir(parents=True)
        monkeypatch.setattr(constants, "HATCHERY_DIR", tmp_path / "hatchery")
        monkeypatch.setattr(sessions, "_TASKS_DB_DIR", tmp_path / "hatchery" / "tasks")

        runner = CliRunner()
        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(repo, True)),
            patch("seekr_hatchery.cli.docker.ensure_dockerfile", return_value=True) as mock_df,
            patch("seekr_hatchery.cli.docker.ensure_docker_config", return_value=True) as mock_dc,
            patch("seekr_hatchery.cli.git.add_and_commit") as mock_commit,
            patch("seekr_hatchery.cli.sessions.ensure_tasks_dir") as mock_tasks_dir,
            patch("seekr_hatchery.cli.sessions.ensure_repo_store") as mock_store,
            patch("seekr_hatchery.cli.sessions.docker_store_dir", return_value=tmp_path / "store"),
            patch("seekr_hatchery.cli.docker.detect_runtime", return_value=docker.DockerRuntime()),
            patch("seekr_hatchery.cli.docker.load_docker_config", return_value=docker.DockerConfig()),
            patch("seekr_hatchery.cli.docker.docker_features", return_value={}),
            patch("seekr_hatchery.cli.docker.launch_sandbox_shell") as mock_launch,
        ):
            result = runner.invoke(cli, ["sandbox", "--no-commit"])
            assert result.exit_code == 0, result.output
            # ensure_tasks_dir not called (no in-tree .hatchery/tasks)
            assert not mock_tasks_dir.called
            # git.add_and_commit not called
            assert not mock_commit.called
            # ensure_repo_store called
            assert mock_store.called
            # docker files written to store dir, not repo
            assert mock_df.call_args[0][0] == tmp_path / "store"
            assert mock_dc.call_args[0][0] == tmp_path / "store"
            # launch_sandbox_shell gets docker_root=store
            assert mock_launch.call_args[1]["docker_root"] == tmp_path / "store"



# ---------------------------------------------------------------------------
# cmd_list()
# ---------------------------------------------------------------------------


class TestCmdList:
    def test_no_tasks_message_default(self, monkeypatch, fake_tasks_db):
        runner = CliRunner()
        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd") as mock_root,
            patch("seekr_hatchery.cli.sessions.repo_tasks_for_current_repo") as mock_tasks,
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
            patch("seekr_hatchery.cli.sessions.repo_tasks_for_current_repo") as mock_tasks,
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
            patch("seekr_hatchery.cli.sessions.repo_tasks_for_current_repo") as mock_tasks,
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
            patch("seekr_hatchery.cli.sessions.repo_tasks_for_current_repo") as mock_tasks,
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
            patch("seekr_hatchery.cli.sessions.repo_tasks_for_current_repo") as mock_tasks,
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
            patch("seekr_hatchery.cli.sessions.repo_tasks_for_current_repo") as mock_tasks,
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
            patch("seekr_hatchery.cli.sessions.repo_tasks_for_current_repo") as mock_tasks,
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
        sessions.save_task(meta)
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
        real_name = sessions.task_file_name("test-task")
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
        sessions.save_task(meta)
        result = self._invoke(runner, ["status", "done-task"])
        assert "2026-01-16T14:00" in result.output


# ---------------------------------------------------------------------------
# shell command
# ---------------------------------------------------------------------------

_SHELL_REPO = Path("/my/repo")


class TestCmdShell:
    def _make_task_meta(self, fake_tasks_db, worktree_path):
        meta = {
            "name": "my-task",
            "branch": "hatchery/my-task",
            "worktree": str(worktree_path),
            "repo": str(_SHELL_REPO),
            "status": "in-progress",
            "created": "2026-01-15T10:30:00",
            "session_id": "session-uuid-xyz",
        }
        sessions.save_task(meta)
        return meta

    def _invoke(self, runner, args):
        with patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(_SHELL_REPO, True)):
            return runner.invoke(cli, args)

    def test_shell_in_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["shell", "--help"])
        assert result.exit_code == 0
        assert "worktree" in result.output.lower()

    def test_spawns_shell_in_worktree(self, tmp_path, fake_tasks_db):
        runner = CliRunner()
        worktree = tmp_path / "my-task"
        worktree.mkdir()
        self._make_task_meta(fake_tasks_db, worktree)
        with (
            patch("seekr_hatchery.cli.subprocess.run") as mock_run,
            patch.dict("os.environ", {"SHELL": "/bin/zsh"}),
        ):
            result = self._invoke(runner, ["shell", "my-task"])
        assert result.exit_code == 0
        mock_run.assert_called_once_with(["/bin/zsh"], cwd=worktree)

    def test_falls_back_to_bash_when_shell_unset(self, tmp_path, fake_tasks_db):
        runner = CliRunner()
        worktree = tmp_path / "my-task"
        worktree.mkdir()
        self._make_task_meta(fake_tasks_db, worktree)
        with (
            patch("seekr_hatchery.cli.subprocess.run") as mock_run,
            patch.dict("os.environ", {}, clear=True),
        ):
            result = self._invoke(runner, ["shell", "my-task"])
        assert result.exit_code == 0
        shell_used = mock_run.call_args[0][0][0]
        assert shell_used == "bash"

    def test_error_when_worktree_missing(self, tmp_path, fake_tasks_db):
        runner = CliRunner()
        self._make_task_meta(fake_tasks_db, tmp_path / "nonexistent")
        result = self._invoke(runner, ["shell", "my-task"])
        assert result.exit_code == 1
        assert "Worktree not found" in result.output


# ---------------------------------------------------------------------------
# --no-worktree
# ---------------------------------------------------------------------------


class TestCliNoWorktree:
    """Tests for the --no-worktree flag and auto-enable in non-repo directories."""

    def _setup_no_worktree_mocks(self, mocks, in_repo: bool = True):

        (
            mock_root,
            _,
            _,
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

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            mock_create_wt, _, _, _, _ = self._setup_no_worktree_mocks(mocks)
            result = runner.invoke(cli, ["new", "my-task", "--no-worktree"])

        assert result.exit_code == 0
        assert not mock_create_wt.called

    def test_no_worktree_flag_stores_no_worktree_in_metadata(self):
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            _, _, mock_save, _, _ = self._setup_no_worktree_mocks(mocks)
            saved_meta = {}
            mock_save.side_effect = lambda m: saved_meta.update(m)
            runner.invoke(cli, ["new", "my-task", "--no-worktree"])

        assert saved_meta.get("no_worktree") is True

    def test_no_worktree_with_dockerfile_propagates_flag_to_launch(self):
        """--no-worktree flag must reach cli._launch (the docker dispatch is mocked)."""
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            _, _, _, mock_launch, mock_docker = self._setup_no_worktree_mocks(mocks)
            mock_docker.return_value = docker.DockerRuntime()  # Dockerfile present → Docker
            mock_launch.side_effect = None  # still mocked; just verify the flag propagates
            result = runner.invoke(cli, ["new", "my-task", "--no-worktree"])

        assert result.exit_code == 0
        # cli._launch is called with a SessionMeta whose no_worktree=True
        meta_arg = mock_launch.call_args[0][0]
        assert meta_arg.no_worktree is True

    def test_auto_enable_when_not_in_repo(self):
        """When git_root_or_cwd returns in_repo=False, no_worktree is auto-enabled."""
        runner = CliRunner()

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

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            self._setup_no_worktree_mocks(mocks, in_repo=False)
            result = runner.invoke(cli, ["new", "my-task"])

        assert "not in a git repository" in result.output
        assert "Docker sandbox" not in result.output

    def test_not_in_repo_docker_files_are_created(self):
        """When not in a git repo, Docker scaffold files are still created."""
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                mock_ensure_df,
                mock_ensure_dc,
                mock_db_path,
                mock_wt_dir,
                _,
                _,
                mock_write,
                _,
                _,
                mock_docker,
                _,
                _,
                _,
            ) = mocks
            mock_root.return_value = (Path("/some/dir"), False)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = Path("/some/dir/.hatchery/worktrees")
            mock_write.return_value = Path("/some/dir/.hatchery/tasks/task.md")
            mock_docker.return_value = None
            result = runner.invoke(cli, ["new", "my-task"])

        assert result.exit_code == 0
        assert mock_ensure_df.called
        assert mock_ensure_dc.called

    def test_not_in_repo_no_docker_flag_skips_docker_files(self):
        """When not in a git repo and --no-docker is passed, Docker files are not created."""
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                mock_ensure_df,
                mock_ensure_dc,
                mock_db_path,
                mock_wt_dir,
                _,
                _,
                mock_write,
                _,
                _,
                mock_docker,
                _,
                _,
                _,
            ) = mocks
            mock_root.return_value = (Path("/some/dir"), False)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = Path("/some/dir/.hatchery/worktrees")
            mock_write.return_value = Path("/some/dir/.hatchery/tasks/task.md")
            mock_docker.return_value = None
            result = runner.invoke(cli, ["new", "my-task", "--no-docker"])

        assert result.exit_code == 0
        assert not mock_ensure_df.called
        assert not mock_ensure_dc.called

    def test_resume_skips_worktree_check_in_no_worktree_mode(self, tmp_path, fake_tasks_db):
        """cmd_resume should not error on missing worktree when no_worktree=True."""
        runner = CliRunner()
        # no_worktree=True means meta.worktree_path doesn't need to exist
        nonexistent = tmp_path / "does-not-exist"
        meta = sessions.SessionMeta(
            name="my-task",
            branch="",
            worktree=str(nonexistent),
            repo=str(tmp_path),
            session_id="sid-456",
            no_worktree=True,
        )

        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(tmp_path, False)),
            patch("seekr_hatchery.cli.sessions.load", return_value=meta),
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.sessions.restore_dockerfile_if_needed"),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli._launch") as mock_launch,
        ):
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
            patch("seekr_hatchery.sessions.task_session_dir", return_value=Path("/session")),
            patch("seekr_hatchery.sessions.sandbox_context", return_value="ctx"),
            patch("seekr_hatchery.sessions._SESSION_SYSTEM", "sys"),
            patch("seekr_hatchery.sessions.session_prompt", return_value="prompt"),
            patch("seekr_hatchery.sessions.subprocess.run"),
            patch(
                "seekr_hatchery.sessions.load_task",
                return_value={"name": "t", "status": "in-progress", "branch": "b"},
            ),
            patch("seekr_hatchery.sessions.save_task"),
            patch("seekr_hatchery.sessions.os.chdir"),
        ]

    def test_launch_new_hook_order(self, spy_backend):

        with ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)
            sessions.launch(
                _launch_meta(),
                kind="new",
                backend=spy_backend,
                runtime=None,
                main_branch="main",
                session_id="sid",
            )

        call_names = [c[0] for c in spy_backend.calls]
        assert call_names == ["on_new_task", "on_before_launch", "build_new_command", "background_threads"]
        _, session_dir = spy_backend.calls[0]
        assert session_dir == Path("/session")
        _, worktree = spy_backend.calls[1]
        assert worktree == Path("/worktree")
        _, sid, _sys, _init, docker, workdir = spy_backend.calls[2]
        assert sid == "sid"
        assert docker is False
        assert workdir == ""

    def test_launch_resume_hook_order(self, spy_backend):

        with ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)
            sessions.launch(
                _launch_meta(),
                kind="resume",
                backend=spy_backend,
                runtime=None,
                main_branch="main",
                session_id="sid",
            )

        call_names = [c[0] for c in spy_backend.calls]
        assert call_names == ["on_before_launch", "build_resume_command", "background_threads"]
        _, worktree = spy_backend.calls[0]
        assert worktree == Path("/worktree")
        _, sid, _sys, _init, docker, workdir = spy_backend.calls[1]
        assert sid == "sid"
        assert docker is False
        assert workdir == ""

    def test_launch_finalize_no_hooks(self, spy_backend):

        with ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)
            sessions.launch(
                _launch_meta(),
                kind="finalize",
                backend=spy_backend,
                runtime=None,
                main_branch="main",
                session_id="sid",
            )

        call_names = [c[0] for c in spy_backend.calls]
        assert call_names == ["build_finalize_command", "background_threads"]
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
            patch("seekr_hatchery.cli.sessions.repo_tasks_for_current_repo") as mock_tasks,
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
            patch("seekr_hatchery.cli.sessions.repo_tasks_for_current_repo") as mock_tasks,
        ):
            mock_root.return_value = (Path("/my/repo"), True)
            mock_tasks.return_value = task_list
            result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "paused-task" in result.output

    def test_running_state_blocked_by_new_duplicate_check(self):
        runner = CliRunner()

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
        """After sessions.launch exits, status should go running then in-progress."""
        statuses_saved: list[str] = []

        def fake_load_task(repo: Path, name: str) -> dict:
            return {"name": name, "status": "in-progress", "branch": "hatchery/x"}

        def fake_save_task(meta: dict) -> None:
            statuses_saved.append(meta["status"])

        with (
            patch("seekr_hatchery.sessions.task_session_dir", return_value=Path("/session")),
            patch("seekr_hatchery.sessions.sandbox_context", return_value="ctx"),
            patch("seekr_hatchery.sessions._SESSION_SYSTEM", "sys"),
            patch("seekr_hatchery.sessions.session_prompt", return_value="prompt"),
            patch("seekr_hatchery.sessions.subprocess.run"),
            patch("seekr_hatchery.sessions.load_task", side_effect=fake_load_task),
            patch("seekr_hatchery.sessions.save_task", side_effect=fake_save_task),
            patch("seekr_hatchery.sessions.os.chdir"),
        ):
            sessions.launch(
                _launch_meta(name="my-task", branch="hatchery/my-task"),
                kind="new",
                backend=spy_backend,
                runtime=None,
                main_branch="main",
                session_id="sid-123",
            )

        assert statuses_saved == ["running", "in-progress"]


# ---------------------------------------------------------------------------
# Chat command
# ---------------------------------------------------------------------------


class TestChat:
    def test_help_lists_chat_command(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "chat" in result.output

    def test_chat_help_shows_agent_option(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["chat", "--help"])
        assert result.exit_code == 0
        assert "--agent" in result.output

    def test_chat_help_shows_name_argument(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["chat", "--help"])
        assert result.exit_code == 0
        assert "NAME" in result.output


class TestNextChatName:
    def test_no_existing_chats(self, fake_tasks_db):
        with patch("seekr_hatchery.cli.sessions.repo_tasks_for_current_repo", return_value=[]):
            result = next_chat_name(Path("/my/repo"))
        assert result == "chat-1"

    def test_one_existing_chat(self, fake_tasks_db):
        existing = [{"name": "chat-1", "type": "chat"}]
        with patch("seekr_hatchery.cli.sessions.repo_tasks_for_current_repo", return_value=existing):
            result = next_chat_name(Path("/my/repo"))
        assert result == "chat-2"

    def test_reuses_gap(self, fake_tasks_db):
        """If chat-1 is gone but chat-2 and chat-3 exist, reuse chat-1."""
        existing = [
            {"name": "chat-2", "type": "chat"},
            {"name": "chat-3", "type": "chat"},
        ]
        with patch("seekr_hatchery.cli.sessions.repo_tasks_for_current_repo", return_value=existing):
            result = next_chat_name(Path("/my/repo"))
        assert result == "chat-1"

    def test_reuses_middle_gap(self, fake_tasks_db):
        existing = [
            {"name": "chat-1", "type": "chat"},
            {"name": "chat-3", "type": "chat"},
        ]
        with patch("seekr_hatchery.cli.sessions.repo_tasks_for_current_repo", return_value=existing):
            result = next_chat_name(Path("/my/repo"))
        assert result == "chat-2"

    def test_no_gap_uses_next(self, fake_tasks_db):
        existing = [
            {"name": "chat-1", "type": "chat"},
            {"name": "chat-2", "type": "chat"},
            {"name": "chat-3", "type": "chat"},
        ]
        with patch("seekr_hatchery.cli.sessions.repo_tasks_for_current_repo", return_value=existing):
            result = next_chat_name(Path("/my/repo"))
        assert result == "chat-4"

    def test_ignores_non_chat_tasks(self, fake_tasks_db):
        existing = [
            {"name": "my-task", "type": "task"},
            {"name": "chat-1", "type": "chat"},
        ]
        with patch("seekr_hatchery.cli.sessions.repo_tasks_for_current_repo", return_value=existing):
            result = next_chat_name(Path("/my/repo"))
        assert result == "chat-2"

    def test_ignores_non_numeric_chat_names(self, fake_tasks_db):
        existing = [
            {"name": "chat-foo", "type": "chat"},
            {"name": "chat-1", "type": "chat"},
        ]
        with patch("seekr_hatchery.cli.sessions.repo_tasks_for_current_repo", return_value=existing):
            result = next_chat_name(Path("/my/repo"))
        assert result == "chat-2"


class TestLaunchNewChat:
    """sessions.launch with is_chat passes empty system/initial prompts."""

    def _patches(self):
        return [
            patch("seekr_hatchery.sessions.task_session_dir", return_value=Path("/session")),
            patch("seekr_hatchery.sessions.subprocess.run"),
            patch(
                "seekr_hatchery.sessions.load_task",
                return_value={"name": "t", "status": "in-progress", "branch": ""},
            ),
            patch("seekr_hatchery.sessions.save_task"),
            patch("seekr_hatchery.sessions.os.chdir"),
        ]

    def test_launch_new_chat_empty_system_prompt(self, spy_backend):

        with ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)
            sessions.launch(
                _launch_meta(name="chat-1", worktree="/repo", branch="", is_chat=True, no_worktree=True),
                kind="new",
                backend=spy_backend,
                runtime=None,
                main_branch="main",
                session_id="sid",
            )

        build_call = [c for c in spy_backend.calls if c[0] == "build_new_command"][0]
        system_prompt = build_call[2]
        assert system_prompt == ""

    def test_launch_new_chat_empty_initial_prompt(self, spy_backend):

        with ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)
            sessions.launch(
                _launch_meta(name="chat-1", worktree="/repo", branch="", is_chat=True, no_worktree=True),
                kind="new",
                backend=spy_backend,
                runtime=None,
                main_branch="main",
                session_id="sid",
            )

        build_call = [c for c in spy_backend.calls if c[0] == "build_new_command"][0]
        initial_prompt = build_call[3]
        assert initial_prompt == ""


class TestCliLaunchDispatch:
    """cli._launch dispatches to chat or task post-exit based on meta.is_chat."""

    def _patches(self):
        return [
            patch("seekr_hatchery.cli.sessions.launch", return_value=[]),
            patch("seekr_hatchery.cli._post_exit_check"),
            patch("seekr_hatchery.cli._chat_post_exit"),
        ]

    def test_chat_calls_chat_post_exit(self, spy_backend):
        from seekr_hatchery import cli as cli_mod

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in self._patches()]
            _, mock_post_exit, mock_chat_post_exit = mocks
            cli_mod._launch(
                _launch_meta(name="chat-1", worktree="/repo", branch="", is_chat=True, no_worktree=True),
                kind="new",
                backend=spy_backend,
                runtime=None,
                main_branch="main",
                session_id="sid",
            )

        assert mock_chat_post_exit.called
        assert not mock_post_exit.called

    def test_task_calls_post_exit_check(self, spy_backend):
        from seekr_hatchery import cli as cli_mod

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in self._patches()]
            _, mock_post_exit, mock_chat_post_exit = mocks
            cli_mod._launch(
                _launch_meta(),
                kind="new",
                backend=spy_backend,
                runtime=None,
                main_branch="main",
                session_id="sid",
            )

        assert mock_post_exit.called
        assert not mock_chat_post_exit.called


class TestLaunchResumeChat:
    """sessions.launch resume with is_chat passes empty system prompt."""

    def _patches(self):
        return [
            patch("seekr_hatchery.sessions.subprocess.run"),
            patch(
                "seekr_hatchery.sessions.load_task",
                return_value={"name": "t", "status": "in-progress", "branch": ""},
            ),
            patch("seekr_hatchery.sessions.save_task"),
            patch("seekr_hatchery.sessions.os.chdir"),
        ]

    def test_launch_resume_chat_empty_system_prompt(self, spy_backend):

        with ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)
            sessions.launch(
                _launch_meta(name="chat-1", worktree="/repo", branch="", is_chat=True, no_worktree=True),
                kind="resume",
                backend=spy_backend,
                runtime=None,
                main_branch="main",
                session_id="sid",
            )

        build_call = [c for c in spy_backend.calls if c[0] == "build_resume_command"][0]
        system_prompt = build_call[2]
        assert system_prompt == ""


class TestCmdChatDispatch:
    """Test the cmd_chat Click command."""

    def test_chat_requires_git_repo(self):
        runner = CliRunner()
        with patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(Path("/some/dir"), False)):
            result = runner.invoke(cli, ["chat"])
        assert result.exit_code == 1
        assert "git repository" in result.output

    def test_chat_auto_generates_name(self, fake_tasks_db):
        runner = CliRunner()
        saved_meta = {}

        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(Path("/repo"), True)),
            patch("seekr_hatchery.sessions.next_chat_name", return_value="chat-1"),
            patch("seekr_hatchery.cli.docker.ensure_dockerfile", return_value=False),
            patch("seekr_hatchery.cli.docker.ensure_docker_config", return_value=False),
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli.sessions.save_task", side_effect=lambda m: saved_meta.update(m)),
            patch("seekr_hatchery.cli._launch"),
            patch("seekr_hatchery.cli.sessions.task_db_path") as mock_db_path,
        ):
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            result = runner.invoke(cli, ["chat"])

        assert result.exit_code == 0
        assert saved_meta.get("name") == "chat-1"
        assert saved_meta.get("type") == "chat"
        assert saved_meta.get("no_worktree") is True
        assert saved_meta.get("branch") == ""

    def test_chat_with_explicit_name(self, fake_tasks_db):
        runner = CliRunner()
        saved_meta = {}

        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(Path("/repo"), True)),
            patch("seekr_hatchery.cli.docker.ensure_dockerfile", return_value=False),
            patch("seekr_hatchery.cli.docker.ensure_docker_config", return_value=False),
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli.sessions.save_task", side_effect=lambda m: saved_meta.update(m)),
            patch("seekr_hatchery.cli._launch"),
            patch("seekr_hatchery.cli.sessions.task_db_path") as mock_db_path,
        ):
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            result = runner.invoke(cli, ["chat", "my-session"])

        assert result.exit_code == 0
        assert saved_meta.get("name") == "my-session"
        assert saved_meta.get("type") == "chat"

    def test_chat_passes_is_chat_to_launch(self, fake_tasks_db):
        runner = CliRunner()

        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(Path("/repo"), True)),
            patch("seekr_hatchery.sessions.next_chat_name", return_value="chat-1"),
            patch("seekr_hatchery.cli.docker.ensure_dockerfile", return_value=False),
            patch("seekr_hatchery.cli.docker.ensure_docker_config", return_value=False),
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli.sessions.save_task"),
            patch("seekr_hatchery.cli._launch") as mock_launch,
            patch("seekr_hatchery.cli.sessions.task_db_path") as mock_db_path,
        ):
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            runner.invoke(cli, ["chat"])

        assert mock_launch.called
        # cli._launch receives a SessionMeta; chat sessions have meta.is_chat=True.
        meta_arg = mock_launch.call_args[0][0]
        assert meta_arg.is_chat is True


class TestCmdStatusShowsType:
    """Test that cmd_status shows a Type: line."""

    def test_status_shows_type_chat(self, fake_tasks_db):
        runner = CliRunner()
        meta = {
            "name": "chat-1",
            "type": "chat",
            "branch": "",
            "worktree": "/nonexistent",
            "repo": "/my/repo",
            "status": "in-progress",
            "created": "2026-01-15T10:00:00",
            "session_id": "sid",
            "no_worktree": True,
        }
        sessions.save_task(meta)
        with patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(Path("/my/repo"), True)):
            result = runner.invoke(cli, ["status", "chat-1"])
        assert result.exit_code == 0
        assert "chat" in result.output

    def test_status_shows_type_task_default(self, fake_tasks_db):
        runner = CliRunner()
        meta = {
            "name": "my-task",
            "branch": "hatchery/my-task",
            "worktree": "/nonexistent",
            "repo": "/my/repo",
            "status": "in-progress",
            "created": "2026-01-15T10:00:00",
            "session_id": "sid",
        }
        sessions.save_task(meta)
        with patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(Path("/my/repo"), True)):
            result = runner.invoke(cli, ["status", "my-task"])
        assert result.exit_code == 0
        assert "task" in result.output


class TestResumeChat:
    """Test that cmd_resume passes is_chat for chat metadata."""

    def test_resume_passes_is_chat_for_chat(self, tmp_path, fake_tasks_db):
        runner = CliRunner()
        meta = sessions.SessionMeta(
            name="chat-1",
            type="chat",
            branch="",
            worktree=str(tmp_path),
            repo=str(tmp_path),
            session_id="sid-123",
            no_worktree=True,
        )

        with (
            patch("seekr_hatchery.cli.sessions.load", return_value=meta),
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.sessions.restore_dockerfile_if_needed"),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli._launch") as mock_launch,
        ):
            runner.invoke(cli, ["resume", "chat-1"])

        assert mock_launch.called
        meta_arg = mock_launch.call_args[0][0]
        assert meta_arg.is_chat is True

    def test_resume_passes_is_chat_false_for_task(self, tmp_path, fake_tasks_db):
        runner = CliRunner()
        wt = tmp_path / "wt"
        wt.mkdir()
        meta = _resume_meta(wt)

        with (
            patch("seekr_hatchery.cli.sessions.load", return_value=meta),
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.sessions.docker.ensure_dockerfile"),
            patch("seekr_hatchery.cli.sessions.docker.ensure_docker_config"),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli._launch") as mock_launch,
        ):
            runner.invoke(cli, ["resume", "my-task"])

        assert mock_launch.called
        meta_arg = mock_launch.call_args[0][0]
        assert meta_arg.is_chat is False


# ---------------------------------------------------------------------------
# CLI dispatch — exec
# ---------------------------------------------------------------------------


class TestExec:
    def test_exec_dispatches_to_exec_task_shell(self, tmp_path):
        runner = CliRunner()
        expected_name = sessions.container_name(tmp_path, "my-task")
        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(tmp_path, True)),
            patch("seekr_hatchery.cli.docker.detect_runtime", return_value=docker.DockerRuntime()),
            patch("seekr_hatchery.cli.docker.exec_task_shell") as mock_exec,
        ):
            result = runner.invoke(cli, ["exec", "my-task"])
        assert result.exit_code == 0, result.output
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args
        assert call_args[0][0] == expected_name
        assert isinstance(call_args[0][1], docker.DockerRuntime)
        assert call_args[1] == {"shell": "/bin/bash"}

    def test_exec_custom_shell(self, tmp_path):
        runner = CliRunner()
        expected_name = sessions.container_name(tmp_path, "my-task")
        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(tmp_path, True)),
            patch("seekr_hatchery.cli.docker.detect_runtime", return_value=docker.DockerRuntime()),
            patch("seekr_hatchery.cli.docker.exec_task_shell") as mock_exec,
        ):
            result = runner.invoke(cli, ["exec", "my-task", "--shell", "/bin/sh"])
        assert result.exit_code == 0, result.output
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args
        assert call_args[0][0] == expected_name
        assert isinstance(call_args[0][1], docker.DockerRuntime)
        assert call_args[1] == {"shell": "/bin/sh"}


# ---------------------------------------------------------------------------
# Shell completion
# ---------------------------------------------------------------------------


class TestCompletion:
    def test_task_name_type_empty_on_no_tasks(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        t = TaskNameType()
        with patch("seekr_hatchery.sessions._TASKS_DB_DIR", tmp_path / "nonexistent"):
            result = t.shell_complete(MagicMock(), MagicMock(), "")
        assert result == []

    def test_task_name_type_silent_on_error(self):

        t = TaskNameType()
        with patch("seekr_hatchery.git.git_root_or_cwd", side_effect=RuntimeError("boom")):
            result = t.shell_complete(MagicMock(), MagicMock(), "")
        assert result == []

    def test_task_name_type_returns_matching(self, tmp_path):

        t = TaskNameType()
        fake_tasks = [
            {"name": "my-feature", "status": "in-progress"},
            {"name": "my-bug", "status": "archived"},
            {"name": "other-task", "status": "in-progress"},
        ]
        with (
            patch("seekr_hatchery.git.git_root_or_cwd", return_value=(tmp_path, True)),
            patch("seekr_hatchery.sessions.repo_tasks_for_current_repo", return_value=fake_tasks),
        ):
            result = t.shell_complete(MagicMock(), MagicMock(), "my")
        assert len(result) == 2
        names = {item.value for item in result}
        assert names == {"my-feature", "my-bug"}


# ---------------------------------------------------------------------------
# hatchery self completions
# ---------------------------------------------------------------------------


class TestSelfCompletions:
    def test_installs_bash_completion(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/bash")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["self", "completions"])
        assert result.exit_code == 0
        rc = (tmp_path / ".bashrc").read_text()
        assert "_HATCHERY_COMPLETE=bash_source hatchery" in rc

    def test_installs_zsh_completion(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/zsh")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["self", "completions"])
        assert result.exit_code == 0
        rc = (tmp_path / ".zshrc").read_text()
        assert "_HATCHERY_COMPLETE=zsh_source hatchery" in rc

    def test_fish_creates_config_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHELL", "/usr/bin/fish")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["self", "completions"])
        assert result.exit_code == 0
        rc = (tmp_path / ".config" / "fish" / "config.fish").read_text()
        assert "_HATCHERY_COMPLETE=fish_source hatchery" in rc

    def test_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/bash")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        runner = CliRunner()
        runner.invoke(cli, ["self", "completions"])
        runner.invoke(cli, ["self", "completions"])
        rc = (tmp_path / ".bashrc").read_text()
        assert rc.count("_HATCHERY_COMPLETE") == 1

    def test_unsupported_shell(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/tcsh")
        runner = CliRunner()
        result = runner.invoke(cli, ["self", "completions"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# cmd_new --include: metadata + worktree creation
# ---------------------------------------------------------------------------


class TestCliNewInclude:
    def test_include_stored_in_metadata(self, tmp_path):
        """--include paths are stored as absolute strings in saved metadata."""
        runner = CliRunner()
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()

        saved_meta = {}

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                _,
                _,
                mock_db_path,
                mock_wt_dir,
                mock_create_wt,
                _,
                mock_write,
                _,
                mock_save,
                mock_docker,
                _,
                _,
                _,
            ) = mocks
            mock_root.return_value = (Path("/repo"), True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = Path("/repo/.hatchery/worktrees")
            mock_write.return_value = Path("/repo/.hatchery/tasks/task.md")
            mock_docker.return_value = None
            mock_save.side_effect = saved_meta.update

            stack.enter_context(patch("seekr_hatchery.cli.git.create_include_worktrees"))
            stack.enter_context(
                patch("seekr_hatchery.cli.docker.load_docker_config", return_value=MagicMock(include=[]))
            )

            result = runner.invoke(cli, ["new", "my-task", "--include", str(repo_b)])

        assert result.exit_code == 0, result.output
        include = saved_meta.get("include", [])
        assert any(isinstance(e, dict) and e.get("path") == str(repo_b.resolve()) for e in include)

    def test_include_creates_secondary_worktrees(self, tmp_path):
        """When --include points at a git repo, create_include_worktrees is called."""
        runner = CliRunner()
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                _,
                _,
                mock_db_path,
                mock_wt_dir,
                _,
                _,
                mock_write,
                _,
                _,
                mock_docker,
                _,
                _,
                _,
            ) = mocks
            mock_root.return_value = (Path("/repo"), True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = Path("/repo/.hatchery/worktrees")
            mock_write.return_value = Path("/repo/.hatchery/tasks/task.md")
            mock_docker.return_value = None

            mock_create_inc = stack.enter_context(patch("seekr_hatchery.cli.git.create_include_worktrees"))
            stack.enter_context(
                patch("seekr_hatchery.cli.docker.load_docker_config", return_value=MagicMock(include=[]))
            )

            runner.invoke(cli, ["new", "my-task", "--include", str(repo_b)])

        mock_create_inc.assert_called_once()
        call_includes = mock_create_inc.call_args[0][0]
        assert any(repo_b.resolve() == e.path for e in call_includes)

    def test_include_passed_to_launch_new(self, tmp_path):
        """include_repos is forwarded to cli._launch."""
        runner = CliRunner()
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                _,
                _,
                mock_db_path,
                mock_wt_dir,
                _,
                _,
                mock_write,
                _,
                _,
                mock_docker,
                mock_launch,
                _,
                _,
            ) = mocks
            mock_root.return_value = (Path("/repo"), True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = Path("/repo/.hatchery/worktrees")
            mock_write.return_value = Path("/repo/.hatchery/tasks/task.md")
            mock_docker.return_value = None

            stack.enter_context(patch("seekr_hatchery.cli.git.create_include_worktrees"))
            stack.enter_context(
                patch("seekr_hatchery.cli.docker.load_docker_config", return_value=MagicMock(include=[]))
            )

            runner.invoke(cli, ["new", "my-task", "--include", str(repo_b)])

        assert mock_launch.called
        kwargs = mock_launch.call_args[1]
        include_repos = kwargs.get("include_repos", [])
        assert any(repo_b.resolve() == e.path for e in include_repos)

    def test_keyboard_interrupt_removes_secondary_worktrees_and_branches(self, tmp_path):
        """Ctrl-C during cmd_new removes include worktrees and deletes branches."""
        runner = CliRunner()
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                _,
                _,
                mock_db_path,
                mock_wt_dir,
                _,
                _,
                mock_write,
                _,
                _,
                mock_docker,
                mock_launch,
                _,
                _,
            ) = mocks
            mock_root.return_value = (Path("/repo"), True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = Path("/repo/.hatchery/worktrees")
            mock_write.return_value = Path("/repo/.hatchery/tasks/task.md")
            mock_docker.return_value = None
            mock_launch.side_effect = KeyboardInterrupt

            mock_remove_inc = stack.enter_context(patch("seekr_hatchery.cli.git.remove_include_worktrees"))
            mock_delete_inc = stack.enter_context(patch("seekr_hatchery.cli.git.delete_include_branches"))
            stack.enter_context(patch("seekr_hatchery.cli.git.create_include_worktrees"))
            stack.enter_context(
                patch("seekr_hatchery.cli.docker.load_docker_config", return_value=MagicMock(include=[]))
            )

            result = runner.invoke(cli, ["new", "my-task", "--include", str(repo_b)])

        assert result.exit_code == 1
        mock_remove_inc.assert_called_once()
        mock_delete_inc.assert_called_once()

    def test_include_ro_stored_in_metadata(self, tmp_path):
        """--include-ro stores mode='ro'; create_include_worktrees is passed a ro entry."""
        runner = CliRunner()
        repo_b = tmp_path / "ref-docs"
        repo_b.mkdir()

        saved_meta = {}

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                _,
                _,
                mock_db_path,
                mock_wt_dir,
                _,
                _,
                mock_write,
                _,
                mock_save,
                mock_docker,
                _,
                _,
                _,
            ) = mocks
            mock_root.return_value = (Path("/repo"), True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = Path("/repo/.hatchery/worktrees")
            mock_write.return_value = Path("/repo/.hatchery/tasks/task.md")
            mock_docker.return_value = None
            mock_save.side_effect = saved_meta.update

            mock_create_inc = stack.enter_context(patch("seekr_hatchery.cli.git.create_include_worktrees"))
            stack.enter_context(
                patch("seekr_hatchery.cli.docker.load_docker_config", return_value=MagicMock(include=[]))
            )

            result = runner.invoke(cli, ["new", "my-task", "--include-ro", str(repo_b)])

        assert result.exit_code == 0, result.output
        # metadata written with mode=ro
        include = saved_meta.get("include", [])
        assert any(e.get("path") == str(repo_b.resolve()) and e.get("mode") == "ro" for e in include)
        # create_include_worktrees called with ro entry (its internal filter handles skipping)
        call_entries = mock_create_inc.call_args[0][0]
        assert any(e.path == repo_b.resolve() and e.mode == "ro" for e in call_entries)

    def test_include_rw_stored_in_metadata(self, tmp_path):
        """--include-rw stores mode='rw'; create_include_worktrees is passed an rw entry."""
        runner = CliRunner()
        repo_b = tmp_path / "shared-lib"
        repo_b.mkdir()

        saved_meta = {}

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                _,
                _,
                mock_db_path,
                mock_wt_dir,
                _,
                _,
                mock_write,
                _,
                mock_save,
                mock_docker,
                _,
                _,
                _,
            ) = mocks
            mock_root.return_value = (Path("/repo"), True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = Path("/repo/.hatchery/worktrees")
            mock_write.return_value = Path("/repo/.hatchery/tasks/task.md")
            mock_docker.return_value = None
            mock_save.side_effect = saved_meta.update

            mock_create_inc = stack.enter_context(patch("seekr_hatchery.cli.git.create_include_worktrees"))
            stack.enter_context(
                patch("seekr_hatchery.cli.docker.load_docker_config", return_value=MagicMock(include=[]))
            )

            result = runner.invoke(cli, ["new", "my-task", "--include-rw", str(repo_b)])

        assert result.exit_code == 0, result.output
        include = saved_meta.get("include", [])
        assert any(e.get("path") == str(repo_b.resolve()) and e.get("mode") == "rw" for e in include)
        call_entries = mock_create_inc.call_args[0][0]
        assert any(e.path == repo_b.resolve() and e.mode == "rw" for e in call_entries)

    def test_no_worktree_with_worktree_mode_include_still_creates_secondary_worktree(self, tmp_path):
        """--no-worktree on the main repo does not prevent worktree creation in included repos."""
        runner = CliRunner()
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (mock_root, _, _, _, _, _, _, mock_db_path, mock_wt_dir, _, _, mock_write, _, _, mock_docker, _, _, _) = (
                mocks
            )
            (_,)
            (_,)
            mock_root.return_value = (Path("/repo"), True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = Path("/repo/.hatchery/worktrees")
            mock_write.return_value = Path("/repo/.hatchery/tasks/task.md")
            mock_docker.return_value = None

            mock_create_inc = stack.enter_context(patch("seekr_hatchery.cli.git.create_include_worktrees"))
            stack.enter_context(
                patch("seekr_hatchery.cli.docker.load_docker_config", return_value=MagicMock(include=[]))
            )

            result = runner.invoke(cli, ["new", "my-task", "--no-worktree", "--include", str(repo_b)])

        assert result.exit_code == 0, result.output
        # create_include_worktrees called with the included repo in worktree mode
        mock_create_inc.assert_called_once()
        call_entries = mock_create_inc.call_args[0][0]
        assert any(e.path == repo_b.resolve() and e.mode == "worktree" for e in call_entries)

    def test_docker_yaml_include_ro_stored_in_metadata(self, tmp_path):
        """docker.yaml 'include:' with mode ro is merged into saved metadata."""
        from seekr_hatchery.includes import IncludeItem

        runner = CliRunner()
        repo_b = tmp_path / "ref"
        repo_b.mkdir()
        saved_meta = {}

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                _,
                _,
                mock_db_path,
                mock_wt_dir,
                _,
                _,
                mock_write,
                _,
                mock_save,
                mock_docker,
                _,
                _,
                _,
            ) = mocks
            mock_root.return_value = (tmp_path, True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = tmp_path / ".hatchery/worktrees"
            mock_write.return_value = tmp_path / ".hatchery/tasks/task.md"
            mock_docker.return_value = None
            mock_save.side_effect = saved_meta.update

            stack.enter_context(patch("seekr_hatchery.cli.git.create_include_worktrees"))
            stack.enter_context(
                patch(
                    "seekr_hatchery.cli.docker.load_docker_config",
                    return_value=MagicMock(include=[IncludeItem(path=str(repo_b), mode="ro")]),
                )
            )

            result = runner.invoke(cli, ["new", "my-task"])

        assert result.exit_code == 0, result.output
        include = saved_meta.get("include", [])
        assert any(e.get("path") == str(repo_b.resolve()) and e.get("mode") == "ro" for e in include)

    def test_includes_added_to_docker_yaml_during_edit_are_respected(self, tmp_path):
        """Include entries added to docker.yaml during the setup prompt are picked up."""
        from seekr_hatchery.includes import IncludeItem

        runner = CliRunner()
        repo_b = tmp_path / "ref"
        repo_b.mkdir()
        saved_meta = {}

        # First load_docker_config (pre-worktree) returns empty includes.
        # Second load (post-edit reconciliation) returns the entry the user added.
        post_edit_config = MagicMock(include=[IncludeItem(path=str(repo_b), mode="ro")])

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                _,
                _,
                mock_db_path,
                mock_wt_dir,
                _,
                _,
                mock_write,
                _,
                mock_save,
                mock_docker,
                _,
                _,
                _,
            ) = mocks
            mock_root.return_value = (tmp_path, True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = tmp_path / ".hatchery/worktrees"
            mock_write.return_value = tmp_path / ".hatchery/tasks/task.md"
            mock_docker.return_value = None
            mock_save.side_effect = saved_meta.update

            mock_create_inc = stack.enter_context(patch("seekr_hatchery.cli.git.create_include_worktrees"))
            # Pre-worktree load: no includes. Post-edit load: one include added.
            stack.enter_context(
                patch(
                    "seekr_hatchery.cli.docker.load_docker_config",
                    side_effect=[MagicMock(include=[]), post_edit_config],
                )
            )

            result = runner.invoke(cli, ["new", "my-task"])

        assert result.exit_code == 0, result.output
        # create_include_worktrees called with the newly-added entry
        mock_create_inc.assert_called_once()
        call_entries = mock_create_inc.call_args[0][0]
        assert any(e.path == repo_b.resolve() and e.mode == "ro" for e in call_entries)
        # entry persisted to meta.json
        include = saved_meta.get("include", [])
        assert any(e.get("path") == str(repo_b.resolve()) and e.get("mode") == "ro" for e in include)


# ---------------------------------------------------------------------------
# cmd_resume — include_repos passed to _launch_resume
# ---------------------------------------------------------------------------


class TestCliResumeInclude:
    def _meta(self, tmp_path, include=None, **overrides) -> sessions.SessionMeta:
        wt = tmp_path / "wt"
        wt.mkdir(exist_ok=True)
        kwargs = {
            "name": "my-task",
            "branch": "hatchery/my-task",
            "worktree": str(wt),
            "repo": str(tmp_path),
            "session_id": "sid-123",
            "no_worktree": False,
            "include": include or [],
        }
        kwargs.update(overrides)
        return sessions.SessionMeta(**kwargs)

    def _invoke_resume(self, runner, meta, extra_args=()):
        """Run cmd_resume with minimal mocks. Returns (result, launch_kwargs, saved_calls)."""
        saved = []
        with (
            patch("seekr_hatchery.cli.sessions.load", return_value=meta),
            patch("seekr_hatchery.cli.sessions.save", side_effect=lambda m: saved.append(m.model_dump())),
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.sessions.docker.ensure_dockerfile"),
            patch("seekr_hatchery.cli.sessions.docker.ensure_docker_config"),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli.git.create_include_worktrees"),
            patch("seekr_hatchery.cli.git.remove_include_worktrees"),
            patch("seekr_hatchery.cli._launch") as mock_launch,
        ):
            result = runner.invoke(cli, ["resume", "my-task", *extra_args])
            launch_kwargs = mock_launch.call_args[1] if mock_launch.called else {}
        return result, launch_kwargs, saved

    def test_metadata_includes_forwarded_to_launch(self, fake_tasks_db, tmp_path):
        """include entries from saved metadata are forwarded to _launch_resume."""
        runner = CliRunner()
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()
        meta = self._meta(tmp_path, include=[{"path": str(repo_b), "mode": "ro"}])

        result, kwargs, _ = self._invoke_resume(runner, meta)

        assert result.exit_code == 0, result.output
        include_repos = kwargs.get("include_repos", [])
        assert any(e.path == repo_b and e.mode == "ro" for e in include_repos)

    def test_missing_include_key_defaults_to_empty(self, fake_tasks_db, tmp_path):
        """Tasks with no include entries resume without error (model default = [])."""
        runner = CliRunner()
        meta = self._meta(tmp_path)

        result, kwargs, _ = self._invoke_resume(runner, meta)

        assert result.exit_code == 0, result.output
        assert kwargs.get("include_repos", []) == []

    def test_resume_include_ro_flag_adds_new_path(self, fake_tasks_db, tmp_path):
        """--include-ro on resume appends a new ro entry to the task."""
        runner = CliRunner()
        new_ref = tmp_path / "docs"
        new_ref.mkdir()
        meta = self._meta(tmp_path)

        result, kwargs, saved = self._invoke_resume(runner, meta, extra_args=["--include-ro", str(new_ref)])

        assert result.exit_code == 0, result.output
        include_repos = kwargs.get("include_repos", [])
        assert any(e.path == new_ref.resolve() and e.mode == "ro" for e in include_repos)
        # meta.json updated with the new entry
        assert saved and any(
            any(e.get("path") == str(new_ref.resolve()) and e.get("mode") == "ro" for e in m.get("include", []))
            for m in saved
        )

    def test_resume_include_rw_flag_adds_new_path(self, fake_tasks_db, tmp_path):
        """--include-rw on resume appends a new rw entry to the task."""
        runner = CliRunner()
        shared = tmp_path / "shared"
        shared.mkdir()
        meta = self._meta(tmp_path)

        result, kwargs, _ = self._invoke_resume(runner, meta, extra_args=["--include-rw", str(shared)])

        assert result.exit_code == 0, result.output
        include_repos = kwargs.get("include_repos", [])
        assert any(e.path == shared.resolve() and e.mode == "rw" for e in include_repos)

    def test_resume_include_flag_mode_transition_worktree_to_ro(self, fake_tasks_db, tmp_path):
        """--include-ro on a path already in worktree mode downgrades and removes the worktree."""
        runner = CliRunner()
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()
        meta = self._meta(tmp_path, include=[{"path": str(repo_b), "mode": "worktree"}])

        with (
            patch("seekr_hatchery.cli.sessions.load", return_value=meta),
            patch("seekr_hatchery.cli.sessions.save"),
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.sessions.docker.ensure_dockerfile"),
            patch("seekr_hatchery.cli.sessions.docker.ensure_docker_config"),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli.git.create_include_worktrees") as mock_create,
            patch("seekr_hatchery.cli.git.remove_include_worktrees") as mock_remove,
            patch("seekr_hatchery.cli._launch") as mock_launch,
        ):
            result = runner.invoke(cli, ["resume", "my-task", "--include-ro", str(repo_b)])

        assert result.exit_code == 0, result.output
        mock_remove.assert_called_once()
        removed = mock_remove.call_args[0][0]
        assert any(e.path == repo_b.resolve() and e.mode == "worktree" for e in removed)
        mock_create.assert_not_called()
        # final launch has ro mode
        launch_kwargs = mock_launch.call_args[1]
        include_repos = launch_kwargs.get("include_repos", [])
        assert any(e.path == repo_b.resolve() and e.mode == "ro" for e in include_repos)

    def test_resume_include_flag_mode_transition_ro_to_worktree(self, fake_tasks_db, tmp_path):
        """--include on a path already in ro mode upgrades it and creates a worktree."""
        runner = CliRunner()
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()
        meta = self._meta(tmp_path, include=[{"path": str(repo_b), "mode": "ro"}])

        with (
            patch("seekr_hatchery.cli.sessions.load", return_value=meta),
            patch("seekr_hatchery.cli.sessions.save"),
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.sessions.docker.ensure_dockerfile"),
            patch("seekr_hatchery.cli.sessions.docker.ensure_docker_config"),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli.git.create_include_worktrees") as mock_create,
            patch("seekr_hatchery.cli.git.remove_include_worktrees") as mock_remove,
            patch("seekr_hatchery.cli._launch") as mock_launch,
        ):
            result = runner.invoke(cli, ["resume", "my-task", "--include", str(repo_b)])

        assert result.exit_code == 0, result.output
        mock_create.assert_called_once()
        created = mock_create.call_args[0][0]
        assert any(e.path == repo_b.resolve() and e.mode == "worktree" for e in created)
        mock_remove.assert_not_called()
        launch_kwargs = mock_launch.call_args[1]
        include_repos = launch_kwargs.get("include_repos", [])
        assert any(e.path == repo_b.resolve() and e.mode == "worktree" for e in include_repos)

    def test_resume_no_worktree_task_with_include_ro(self, fake_tasks_db, tmp_path):
        """no_worktree tasks can have reference includes added on resume."""
        runner = CliRunner()
        ref = tmp_path / "ref"
        ref.mkdir()
        meta = self._meta(tmp_path, no_worktree=True)

        result, kwargs, _ = self._invoke_resume(runner, meta, extra_args=["--include-ro", str(ref)])

        assert result.exit_code == 0, result.output
        include_repos = kwargs.get("include_repos", [])
        assert any(e.path == ref.resolve() and e.mode == "ro" for e in include_repos)

    def test_resume_rw_to_ro_mode_transition_is_a_no_op(self, fake_tasks_db, tmp_path):
        """rw→ro transition updates the mode without any git worktree operations."""
        runner = CliRunner()
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()
        meta = self._meta(tmp_path, include=[{"path": str(repo_b), "mode": "rw"}])

        with (
            patch("seekr_hatchery.cli.sessions.load", return_value=meta),
            patch("seekr_hatchery.cli.sessions.save"),
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.sessions.docker.ensure_dockerfile"),
            patch("seekr_hatchery.cli.sessions.docker.ensure_docker_config"),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli.git.create_include_worktrees") as mock_create,
            patch("seekr_hatchery.cli.git.remove_include_worktrees") as mock_remove,
            patch("seekr_hatchery.cli._launch") as mock_launch,
        ):
            result = runner.invoke(cli, ["resume", "my-task", "--include-ro", str(repo_b)])

        assert result.exit_code == 0, result.output
        # No git worktree operations — both old and new modes are reference mounts.
        mock_create.assert_not_called()
        mock_remove.assert_not_called()
        # Mode updated in the entries forwarded to launch.
        launch_kwargs = mock_launch.call_args[1]
        include_repos = launch_kwargs.get("include_repos", [])
        assert any(e.path == repo_b.resolve() and e.mode == "ro" for e in include_repos)

    def test_resume_after_archive_recreates_include_worktrees(self, fake_tasks_db, tmp_path):
        """Resuming an archived task with worktree-mode includes recreates their worktrees."""
        runner = CliRunner()
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()
        meta = self._meta(
            tmp_path,
            include=[{"path": str(repo_b), "mode": "worktree"}],
            status="archived",
        )
        # Remove the worktree directory so resume triggers the archive restoration path.
        Path(meta.worktree).rmdir()

        with (
            patch("seekr_hatchery.cli.sessions.load", return_value=meta),
            patch("seekr_hatchery.cli.sessions.save") as mock_save,
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.sessions.docker.ensure_dockerfile"),
            patch("seekr_hatchery.cli.sessions.docker.ensure_docker_config"),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli.git.create_worktree"),
            patch("seekr_hatchery.cli.git.create_include_worktrees") as mock_create_inc,
            patch("seekr_hatchery.cli.git.remove_include_worktrees"),
            patch("seekr_hatchery.cli._launch"),
        ):
            result = runner.invoke(cli, ["resume", "my-task"])

        assert result.exit_code == 0, result.output
        # Include worktrees recreated during archive restoration.
        mock_create_inc.assert_called()
        all_calls = mock_create_inc.call_args_list
        assert any(any(e.path == repo_b.resolve() and e.mode == "worktree" for e in call[0][0]) for call in all_calls)
        # Status was flipped to in-progress and persisted.
        mock_save.assert_called_once()
        saved_meta = mock_save.call_args[0][0]
        assert saved_meta.status == "in-progress"

    def test_resume_after_complete_recreates_worktree_and_flips_status(self, fake_tasks_db, tmp_path):
        """Resuming a completed task (worktree removed at mark-done) restores it.

        This is the "tasks sometimes get marked complete by accident" path the
        sessions.launch docstring calls out. Shares ~90% with the archive test
        — the user-facing difference is the "completed" vs "archived" banner
        label printed during restoration.
        """
        runner = CliRunner()
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()
        meta = self._meta(
            tmp_path,
            include=[{"path": str(repo_b), "mode": "worktree"}],
            status="complete",
        )
        # Worktree was removed when mark_done ran; simulate that.
        Path(meta.worktree).rmdir()

        with (
            patch("seekr_hatchery.cli.sessions.load", return_value=meta),
            patch("seekr_hatchery.cli.sessions.save") as mock_save,
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.sessions.docker.ensure_dockerfile"),
            patch("seekr_hatchery.cli.sessions.docker.ensure_docker_config"),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli.git.create_worktree"),
            patch("seekr_hatchery.cli.git.create_include_worktrees") as mock_create_inc,
            patch("seekr_hatchery.cli.git.remove_include_worktrees"),
            patch("seekr_hatchery.cli._launch") as mock_launch,
        ):
            result = runner.invoke(cli, ["resume", "my-task"])

        assert result.exit_code == 0, result.output
        # Same worktree-recreate behaviour as archived.
        mock_create_inc.assert_called()
        all_calls = mock_create_inc.call_args_list
        assert any(any(e.path == repo_b.resolve() and e.mode == "worktree" for e in call[0][0]) for call in all_calls)
        # Status flipped back to in-progress and persisted before launch.
        mock_save.assert_called_once()
        saved_meta = mock_save.call_args[0][0]
        assert saved_meta.status == "in-progress"
        # And launch happened.
        assert mock_launch.called
        assert "completed" in result.output  # banner label distinguishes complete from archived

    def test_cli_include_flag_overrides_yaml_mode_for_same_path(self, fake_tasks_db, tmp_path):
        """CLI --include-ro overrides docker.yaml worktree mode for the same path."""
        from seekr_hatchery.includes import IncludeItem

        runner = CliRunner()
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()
        saved_meta = {}

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
                _,
                _,
                _,
                _,
                _,
                _,
                mock_db_path,
                mock_wt_dir,
                _,
                _,
                mock_write,
                _,
                mock_save,
                mock_docker,
                _,
                _,
                _,
            ) = mocks
            mock_root.return_value = (tmp_path, True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = tmp_path / ".hatchery/worktrees"
            mock_write.return_value = tmp_path / ".hatchery/tasks/task.md"
            mock_docker.return_value = None
            mock_save.side_effect = saved_meta.update

            stack.enter_context(patch("seekr_hatchery.cli.git.create_include_worktrees"))
            # docker.yaml says worktree, CLI flag says ro
            stack.enter_context(
                patch(
                    "seekr_hatchery.cli.docker.load_docker_config",
                    return_value=MagicMock(include=[IncludeItem(path=str(repo_b), mode="worktree")]),
                )
            )

            result = runner.invoke(cli, ["new", "my-task", "--include-ro", str(repo_b)])

        assert result.exit_code == 0, result.output
        # CLI flag wins: stored mode should be ro, not worktree.
        include = saved_meta.get("include", [])
        assert any(e.get("path") == str(repo_b.resolve()) and e.get("mode") == "ro" for e in include)


# ---------------------------------------------------------------------------
# cmd_archive — include worktree removal
# ---------------------------------------------------------------------------


class TestCliArchiveInclude:
    def test_archive_removes_include_worktrees(self, fake_tasks_db, tmp_path):
        """cmd_archive removes include worktrees (branches are retained)."""
        wt = tmp_path / "wt"
        wt.mkdir()
        repo_b = Path("/other/repo-b")

        sessions.save_task(
            {
                "name": "my-task",
                "branch": "hatchery/my-task",
                "worktree": str(wt),
                "repo": str(tmp_path),
                "status": "in-progress",
                "no_worktree": False,
                "no_commit": True,
                "include": [{"path": str(repo_b), "mode": "worktree"}],
            }
        )

        runner = CliRunner()
        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(tmp_path, True)),
            patch("seekr_hatchery.cli.git.has_uncommitted_changes", return_value=False),
            patch("seekr_hatchery.cli.git.remove_worktree"),
            patch("seekr_hatchery.cli.git.remove_include_worktrees") as mock_remove_inc,
        ):
            result = runner.invoke(cli, ["archive", "my-task"])

        assert result.exit_code == 0, result.output
        mock_remove_inc.assert_called_once_with([IncludeEntry(repo_b, "worktree")], "my-task")

    def test_archive_does_not_remove_reference_mode_includes(self, fake_tasks_db, tmp_path):
        """cmd_archive passes all includes to remove_include_worktrees; the function
        itself filters out ro/rw entries — so no worktrees are removed for them."""
        wt = tmp_path / "wt"
        wt.mkdir()
        repo_b = Path("/other/ref-docs")

        sessions.save_task(
            {
                "name": "my-task",
                "branch": "hatchery/my-task",
                "worktree": str(wt),
                "repo": str(tmp_path),
                "status": "in-progress",
                "no_worktree": False,
                "no_commit": True,
                "include": [{"path": str(repo_b), "mode": "ro"}],
            }
        )

        runner = CliRunner()
        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(tmp_path, True)),
            patch("seekr_hatchery.cli.git.has_uncommitted_changes", return_value=False),
            patch("seekr_hatchery.cli.git.remove_worktree"),
            patch("seekr_hatchery.cli.git.remove_include_worktrees") as mock_remove_inc,
        ):
            result = runner.invoke(cli, ["archive", "my-task"])

        assert result.exit_code == 0, result.output
        # Called with the ro entry — but remove_include_worktrees will skip it internally.
        mock_remove_inc.assert_called_once_with([IncludeEntry(repo_b, "ro")], "my-task")


# ---------------------------------------------------------------------------
# _do_mark_done / _do_delete — include cleanup
# ---------------------------------------------------------------------------


class TestDoMarkDoneInclude:
    def test_done_removes_include_worktrees(self, fake_tasks_db):
        """_do_mark_done calls remove_include_worktrees when include paths are present."""
        repo = Path("/my/repo")
        worktree = Path("/my/repo/.hatchery/worktrees/my-task")
        repo_b = Path("/other/repo-b")

        sessions.save_task(
            {
                "name": "my-task",
                "branch": "hatchery/my-task",
                "worktree": str(worktree),
                "repo": str(repo),
                "status": "in-progress",
                "no_worktree": True,
                "include": [str(repo_b)],
            }
        )

        import seekr_hatchery.cli as cli_mod

        with patch("seekr_hatchery.cli.git.remove_include_worktrees") as mock_remove:
            cli_mod._do_mark_done("my-task", repo, worktree)

        mock_remove.assert_not_called()  # no_worktree=True skips the whole block

    def test_done_removes_include_worktrees_with_worktree_mode(self, fake_tasks_db, tmp_path):
        """_do_mark_done with no_worktree=False removes include worktrees."""
        repo = Path("/my/repo")
        wt = tmp_path / "wt"
        wt.mkdir()
        repo_b = Path("/other/repo-b")

        sessions.save_task(
            {
                "name": "my-task",
                "branch": "hatchery/my-task",
                "worktree": str(wt),
                "repo": str(repo),
                "status": "in-progress",
                "no_worktree": False,
                "include": [str(repo_b)],
            }
        )

        import seekr_hatchery.cli as cli_mod

        with (
            patch("seekr_hatchery.cli.git.remove_worktree"),
            patch("seekr_hatchery.cli.git.has_uncommitted_changes", return_value=False),
            patch("seekr_hatchery.cli.git.remove_include_worktrees") as mock_remove,
        ):
            cli_mod._do_mark_done("my-task", repo, wt)

        mock_remove.assert_called_once_with([IncludeEntry(repo_b, "worktree")], "my-task")

    def test_done_no_include_does_not_call_remove(self, fake_tasks_db, tmp_path):
        """_do_mark_done with no include paths does not call remove_include_worktrees."""
        repo = Path("/my/repo")
        wt = tmp_path / "wt"
        wt.mkdir()

        sessions.save_task(
            {
                "name": "my-task",
                "branch": "hatchery/my-task",
                "worktree": str(wt),
                "repo": str(repo),
                "status": "in-progress",
                "no_worktree": False,
            }
        )

        import seekr_hatchery.cli as cli_mod

        with (
            patch("seekr_hatchery.cli.git.remove_worktree"),
            patch("seekr_hatchery.cli.git.has_uncommitted_changes", return_value=False),
            patch("seekr_hatchery.cli.git.remove_include_worktrees") as mock_remove,
        ):
            cli_mod._do_mark_done("my-task", repo, wt)

        mock_remove.assert_not_called()


class TestDoDeleteInclude:
    def test_delete_removes_worktrees_and_branches(self, fake_tasks_db, tmp_path):
        """_do_delete calls remove_include_worktrees and delete_include_branches."""
        repo = Path("/my/repo")
        wt = tmp_path / "wt"
        wt.mkdir()
        repo_b = Path("/other/repo-b")

        meta = {
            "name": "my-task",
            "branch": "hatchery/my-task",
            "worktree": str(wt),
            "repo": str(repo),
            "status": "in-progress",
            "no_worktree": False,
            "include": [str(repo_b)],
        }
        sessions.save_task(meta)
        meta_obj = sessions.load(repo, "my-task")

        import seekr_hatchery.cli as cli_mod

        with (
            patch("seekr_hatchery.cli.git.remove_worktree"),
            patch("seekr_hatchery.cli.git.delete_branch", return_value=True),
            patch("seekr_hatchery.cli.git.remove_include_worktrees") as mock_remove,
            patch("seekr_hatchery.cli.git.delete_include_branches") as mock_delete_br,
        ):
            cli_mod._do_delete(meta_obj, confirmed=True)

        mock_remove.assert_called_once_with([IncludeEntry(repo_b, "worktree")], "my-task")
        mock_delete_br.assert_called_once_with([IncludeEntry(repo_b, "worktree")], "my-task")

    def test_delete_no_include_does_not_call_helpers(self, fake_tasks_db, tmp_path):
        repo = Path("/my/repo")
        wt = tmp_path / "wt"
        wt.mkdir()

        meta = {
            "name": "my-task",
            "branch": "hatchery/my-task",
            "worktree": str(wt),
            "repo": str(repo),
            "status": "in-progress",
            "no_worktree": False,
        }
        sessions.save_task(meta)
        meta_obj = sessions.load(repo, "my-task")

        import seekr_hatchery.cli as cli_mod

        with (
            patch("seekr_hatchery.cli.git.remove_worktree"),
            patch("seekr_hatchery.cli.git.delete_branch", return_value=True),
            patch("seekr_hatchery.cli.git.remove_include_worktrees") as mock_remove,
            patch("seekr_hatchery.cli.git.delete_include_branches") as mock_delete_br,
        ):
            cli_mod._do_delete(meta_obj, confirmed=True)

        mock_remove.assert_not_called()
        mock_delete_br.assert_not_called()


# ---------------------------------------------------------------------------
# _launch_finalize — include_repos
# ---------------------------------------------------------------------------


class TestLaunchFinalizeInclude:
    def test_include_repos_passed_to_sandbox_context(self, tmp_path, spy_backend):
        """sessions.launch include entries appear in the system prompt for finalize."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()
        entry_b = IncludeEntry(path=repo_b, mode="worktree")

        with (
            patch("seekr_hatchery.sessions.set_status"),
            patch("seekr_hatchery.sessions.sandbox_context", wraps=sessions.sandbox_context) as mock_ctx,
            patch("seekr_hatchery.sessions.docker.launch_context", return_value=(MagicMock(), [], "/workspace")),
            patch("seekr_hatchery.sessions.os.chdir"),
            patch("seekr_hatchery.sessions.subprocess.run"),
        ):
            sessions.launch(
                _launch_meta(
                    name="my-task",
                    repo=str(repo),
                    worktree=str(worktree),
                    branch="hatchery/my-task",
                    no_worktree=True,
                    agent_kind=spy_backend.kind,
                ),
                kind="finalize",
                backend=spy_backend,
                runtime=None,
                main_branch="main",
                session_id="sid-1",
                include_repos=[entry_b],
            )

        assert mock_ctx.called
        kwargs = mock_ctx.call_args[1]
        assert entry_b in kwargs.get("include_paths", [])

    def test_include_repos_forwarded_to_launch_docker(self, tmp_path, spy_backend):
        """sessions.launch forwards include_repos to docker.run_session."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()
        entry_b = IncludeEntry(path=repo_b, mode="worktree")

        # docker.launch_context returns a config whose .kubernetes is None so sessions.launch
        # short-circuits the kubectl-token write that would otherwise hit fs.
        config = MagicMock()
        config.kubernetes = None
        with (
            patch("seekr_hatchery.sessions.set_status"),
            patch("seekr_hatchery.sessions.get_or_create_proxy_token", return_value="tok"),
            patch("seekr_hatchery.sessions.docker.run_session") as mock_run_session,
            patch(
                "seekr_hatchery.sessions.docker.launch_context",
                return_value=(config, [], "/repo/.hatchery/worktrees/my-task"),
            ),
        ):
            sessions.launch(
                _launch_meta(
                    name="my-task",
                    repo=str(repo),
                    worktree=str(worktree),
                    branch="hatchery/my-task",
                    agent_kind=spy_backend.kind,
                ),
                kind="finalize",
                backend=spy_backend,
                runtime=MagicMock(),
                main_branch="main",
                session_id="sid-1",
                include_repos=[entry_b],
            )

        assert mock_run_session.called
        kwargs = mock_run_session.call_args[1]
        assert entry_b in kwargs.get("include_entries", [])


# ---------------------------------------------------------------------------
# auto_commit config + --commit/--no-commit flag resolution
# ---------------------------------------------------------------------------


class TestAutoCommitResolution:
    """Test that --commit/--no-commit flag resolves against auto_commit config.

    These tests write a real config file (with auto_commit set) to the
    home-redirected config path, then invoke the CLI. The _new_patches
    mock all filesystem-touching functions, so only the flag resolution
    and the saved meta are exercised.
    """

    @staticmethod
    def _write_config(home, auto_commit):
        import json

        path = home / ".hatchery" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": "1", "auto_commit": auto_commit}))

    def test_no_flag_auto_commit_true(self, home):
        """No flag + auto_commit=True → no_commit=False."""
        self._write_config(home, True)
        runner = CliRunner()
        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            mock_root, _, _, _, _, _, _, mock_db_path, mock_wt_dir, _, _, mock_write, _, mock_save, _, _, _, _ = mocks
            mock_root.return_value = (Path("/repo"), True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = Path("/repo/.hatchery/worktrees")
            mock_write.return_value = Path("/repo/.hatchery/tasks/task.md")
            result = runner.invoke(cli, ["new", "my-task"])
        assert result.exit_code == 0, result.output
        saved_meta = mock_save.call_args[0][0]
        assert saved_meta.get("no_commit") is False

    def test_no_flag_auto_commit_false(self, home):
        """No flag + auto_commit=False → no_commit=True."""
        self._write_config(home, False)
        runner = CliRunner()
        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            mock_root, _, _, _, _, _, _, mock_db_path, mock_wt_dir, _, _, mock_write, _, mock_save, _, _, _, _ = mocks
            mock_root.return_value = (Path("/repo"), True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = Path("/repo/.hatchery/worktrees")
            mock_write.return_value = Path("/repo/.hatchery/tasks/task.md")
            result = runner.invoke(cli, ["new", "my-task"])
        assert result.exit_code == 0, result.output
        saved_meta = mock_save.call_args[0][0]
        assert saved_meta.get("no_commit") is True

    def test_explicit_commit_overrides_false(self, home):
        """--commit + auto_commit=False → no_commit=False."""
        self._write_config(home, False)
        runner = CliRunner()
        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            mock_root, _, _, _, _, _, _, mock_db_path, mock_wt_dir, _, _, mock_write, _, mock_save, _, _, _, _ = mocks
            mock_root.return_value = (Path("/repo"), True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = Path("/repo/.hatchery/worktrees")
            mock_write.return_value = Path("/repo/.hatchery/tasks/task.md")
            result = runner.invoke(cli, ["new", "my-task", "--commit"])
        assert result.exit_code == 0, result.output
        saved_meta = mock_save.call_args[0][0]
        assert saved_meta.get("no_commit") is False

    def test_explicit_no_commit_overrides_true(self, home):
        """--no-commit + auto_commit=True → no_commit=True."""
        self._write_config(home, True)
        runner = CliRunner()
        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            mock_root, _, _, _, _, _, _, mock_db_path, mock_wt_dir, _, _, mock_write, _, mock_save, _, _, _, _ = mocks
            mock_root.return_value = (Path("/repo"), True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = Path("/repo/.hatchery/worktrees")
            mock_write.return_value = Path("/repo/.hatchery/tasks/task.md")
            result = runner.invoke(cli, ["new", "my-task", "--no-commit"])
        assert result.exit_code == 0, result.output
        saved_meta = mock_save.call_args[0][0]
        assert saved_meta.get("no_commit") is True
