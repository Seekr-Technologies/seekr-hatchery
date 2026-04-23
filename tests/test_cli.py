"""Tests for the Click CLI entry point and cmd_list/cmd_status."""

import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

import seekr_hatchery.docker as docker
import seekr_hatchery.tasks as tasks
from seekr_hatchery.cli import (
    _WRAP_UP_PROMPT,
    TaskNameType,
    _launch_finalize,
    _launch_new,
    _launch_resume,
    _next_chat_name,
    cli,
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
    """Common context managers for cmd_new tests."""

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

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            _, _, _, mock_launch, _ = self._setup_mocks(mocks)
            result = runner.invoke(cli, ["new", "my-task"])

        assert result.exit_code == 0
        assert mock_launch.called

    def test_new_default_base_is_head(self):
        runner = CliRunner()

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
        assert call_args[0][2] is True or call_args[1].get("no_docker") is True

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

    def test_dockerfile_checked_against_worktree_not_repo(self):
        """ensure_dockerfile/config are called with the worktree path, not repo root."""
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
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

    def test_new_help_shows_no_commit_docker_option(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["new", "--help"])
        assert result.exit_code == 0
        assert "--no-commit-docker" in result.output

    def test_no_commit_docker_generates_to_repo_root_first(self):
        """--no-commit-docker calls ensure_dockerfile with repo path before worktree path."""
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
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
            ) = mocks
            repo = Path("/repo")
            mock_root.return_value = (repo, True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = repo / ".hatchery/worktrees"
            mock_write.return_value = repo / ".hatchery/tasks/task.md"
            mock_docker.return_value = None
            mock_ensure_df.return_value = False
            mock_ensure_dc.return_value = False
            result = runner.invoke(cli, ["new", "my-task", "--no-commit-docker"])

        assert result.exit_code == 0
        # Called twice: once for repo root (generate), once for worktree (copy via source=repo)
        assert mock_ensure_df.call_count == 2
        assert mock_ensure_dc.call_count == 2
        # First call targets repo root
        assert mock_ensure_df.call_args_list[0][0][0] == repo
        assert mock_ensure_dc.call_args_list[0][0][0] == repo
        # Second call targets worktree
        worktree = repo / ".hatchery/worktrees/my-task"
        assert mock_ensure_df.call_args_list[1][0][0] == worktree
        assert mock_ensure_dc.call_args_list[1][0][0] == worktree

    def test_no_commit_docker_skips_dockerfile_commit(self):
        """--no-commit-docker never git-commits the Dockerfile."""
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
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
            ) = mocks
            repo = Path("/repo")
            mock_root.return_value = (repo, True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = repo / ".hatchery/worktrees"
            mock_write.return_value = repo / ".hatchery/tasks/task.md"
            mock_docker.return_value = None
            # Repo-root call creates the file (True); worktree copy returns False
            mock_ensure_df.side_effect = [True, False]
            mock_ensure_dc.side_effect = [True, False]
            result = runner.invoke(cli, ["new", "my-task", "--no-commit-docker"])

        assert result.exit_code == 0
        dockerfile_commits = [
            c
            for c in mock_run.call_args_list
            if c[0][0] == ["git", "commit", "-m", "chore: add hatchery Docker configuration"]
        ]
        assert len(dockerfile_commits) == 0
        # The task-file commit must use .hatchery/tasks/ (not .hatchery/) so the
        # copied-but-uncommitted Docker files are not swept into the staging area.
        task_file_adds = [c for c in mock_run.call_args_list if c[0][0] == ["git", "add", ".hatchery/tasks/"]]
        assert len(task_file_adds) == 1
        full_hatchery_adds = [c for c in mock_run.call_args_list if c[0][0] == ["git", "add", ".hatchery/"]]
        assert len(full_hatchery_adds) == 0

    def test_no_commit_docker_false_default_commits_when_created(self):
        """Without the flag, a newly generated Dockerfile is committed as normal."""
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
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
        # ensure_dockerfile called exactly once (no --no-commit-docker)
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

    def test_no_commit_docker_existing_dockerfile_not_committed(self):
        """--no-commit-docker with a pre-existing repo-root Dockerfile never commits it."""
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
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
            ) = mocks
            repo = Path("/repo")
            mock_root.return_value = (repo, True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = repo / ".hatchery/worktrees"
            mock_write.return_value = repo / ".hatchery/tasks/task.md"
            mock_docker.return_value = None
            # Both calls return False: file already exists at root, copied to worktree
            mock_ensure_df.return_value = False
            mock_ensure_dc.return_value = False
            result = runner.invoke(cli, ["new", "my-task", "--no-commit-docker"])

        assert result.exit_code == 0
        dockerfile_commits = [
            c
            for c in mock_run.call_args_list
            if c[0][0] == ["git", "commit", "-m", "chore: add hatchery Docker configuration"]
        ]
        assert len(dockerfile_commits) == 0

    def test_no_commit_skips_all_commits_dockerfile_new(self):
        """--no-commit skips docker and task-file commits even when Dockerfile is brand-new."""
        runner = CliRunner()

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in _new_patches()]
            (
                mock_root,
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
            ) = mocks
            repo = Path("/repo")
            mock_root.return_value = (repo, True)
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            mock_wt_dir.return_value = repo / ".hatchery/worktrees"
            mock_write.return_value = repo / ".hatchery/tasks/task.md"
            mock_docker.return_value = None
            # Repo-root call creates (True); worktree copy returns False
            mock_ensure_df.side_effect = [True, False]
            mock_ensure_dc.side_effect = [True, False]
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
            "status": "paused",
            "created": "2026-01-15T10:30:00",
            "session_id": "session-uuid-xyz",
        }
        tasks.save_task(meta)
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

    def test_no_worktree_with_dockerfile_uses_launch_docker_no_worktree(self):
        runner = CliRunner()

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
        with patch("seekr_hatchery.cli.tasks.repo_tasks_for_current_repo", return_value=[]):
            result = _next_chat_name(Path("/my/repo"))
        assert result == "chat-1"

    def test_one_existing_chat(self, fake_tasks_db):
        existing = [{"name": "chat-1", "type": "chat"}]
        with patch("seekr_hatchery.cli.tasks.repo_tasks_for_current_repo", return_value=existing):
            result = _next_chat_name(Path("/my/repo"))
        assert result == "chat-2"

    def test_reuses_gap(self, fake_tasks_db):
        """If chat-1 is gone but chat-2 and chat-3 exist, reuse chat-1."""
        existing = [
            {"name": "chat-2", "type": "chat"},
            {"name": "chat-3", "type": "chat"},
        ]
        with patch("seekr_hatchery.cli.tasks.repo_tasks_for_current_repo", return_value=existing):
            result = _next_chat_name(Path("/my/repo"))
        assert result == "chat-1"

    def test_reuses_middle_gap(self, fake_tasks_db):
        existing = [
            {"name": "chat-1", "type": "chat"},
            {"name": "chat-3", "type": "chat"},
        ]
        with patch("seekr_hatchery.cli.tasks.repo_tasks_for_current_repo", return_value=existing):
            result = _next_chat_name(Path("/my/repo"))
        assert result == "chat-2"

    def test_no_gap_uses_next(self, fake_tasks_db):
        existing = [
            {"name": "chat-1", "type": "chat"},
            {"name": "chat-2", "type": "chat"},
            {"name": "chat-3", "type": "chat"},
        ]
        with patch("seekr_hatchery.cli.tasks.repo_tasks_for_current_repo", return_value=existing):
            result = _next_chat_name(Path("/my/repo"))
        assert result == "chat-4"

    def test_ignores_non_chat_tasks(self, fake_tasks_db):
        existing = [
            {"name": "my-task", "type": "task"},
            {"name": "chat-1", "type": "chat"},
        ]
        with patch("seekr_hatchery.cli.tasks.repo_tasks_for_current_repo", return_value=existing):
            result = _next_chat_name(Path("/my/repo"))
        assert result == "chat-2"

    def test_ignores_non_numeric_chat_names(self, fake_tasks_db):
        existing = [
            {"name": "chat-foo", "type": "chat"},
            {"name": "chat-1", "type": "chat"},
        ]
        with patch("seekr_hatchery.cli.tasks.repo_tasks_for_current_repo", return_value=existing):
            result = _next_chat_name(Path("/my/repo"))
        assert result == "chat-2"


class TestLaunchNewChat:
    """Assert that _launch_new with is_chat=True passes no system/initial prompt."""

    def _patches(self):
        return [
            patch("seekr_hatchery.cli.tasks.task_session_dir", return_value=Path("/session")),
            patch("seekr_hatchery.cli.subprocess.run"),
            patch(
                "seekr_hatchery.cli.tasks.load_task",
                return_value={"name": "t", "status": "in-progress", "branch": ""},
            ),
            patch("seekr_hatchery.cli.tasks.save_task"),
            patch("seekr_hatchery.cli._post_exit_check"),
            patch("seekr_hatchery.cli._chat_post_exit"),
            patch("seekr_hatchery.cli.os.chdir"),
        ]

    def test_launch_new_chat_empty_system_prompt(self, spy_backend):

        with ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)
            _launch_new(
                repo=Path("/repo"),
                worktree=Path("/repo"),
                name="chat-1",
                session_id="sid",
                backend=spy_backend,
                runtime=None,
                branch="",
                main_branch="main",
                no_worktree=True,
                is_chat=True,
            )

        build_call = [c for c in spy_backend.calls if c[0] == "build_new_command"][0]
        system_prompt = build_call[2]
        assert system_prompt == ""

    def test_launch_new_chat_empty_initial_prompt(self, spy_backend):

        with ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)
            _launch_new(
                repo=Path("/repo"),
                worktree=Path("/repo"),
                name="chat-1",
                session_id="sid",
                backend=spy_backend,
                runtime=None,
                branch="",
                main_branch="main",
                no_worktree=True,
                is_chat=True,
            )

        build_call = [c for c in spy_backend.calls if c[0] == "build_new_command"][0]
        initial_prompt = build_call[3]
        assert initial_prompt == ""

    def test_launch_new_chat_calls_chat_post_exit(self, spy_backend):

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in self._patches()]
            mock_post_exit = mocks[4]  # _post_exit_check
            mock_chat_post_exit = mocks[5]  # _chat_post_exit
            _launch_new(
                repo=Path("/repo"),
                worktree=Path("/repo"),
                name="chat-1",
                session_id="sid",
                backend=spy_backend,
                runtime=None,
                branch="",
                main_branch="main",
                no_worktree=True,
                is_chat=True,
            )

        assert mock_chat_post_exit.called
        assert not mock_post_exit.called

    def test_launch_new_task_calls_post_exit_check(self, spy_backend):
        """Non-chat launch should still use _post_exit_check."""

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in self._patches()]
            mock_post_exit = mocks[4]
            mock_chat_post_exit = mocks[5]
            # Need sandbox_context, session_prompt, SESSION_SYSTEM for task mode
            stack.enter_context(patch("seekr_hatchery.cli.tasks.sandbox_context", return_value="ctx"))
            stack.enter_context(patch("seekr_hatchery.cli.tasks.session_prompt", return_value="prompt"))
            stack.enter_context(patch("seekr_hatchery.cli.tasks.SESSION_SYSTEM", "sys"))
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

        assert mock_post_exit.called
        assert not mock_chat_post_exit.called


class TestLaunchResumeChat:
    """Assert that _launch_resume with is_chat=True passes no system/initial prompt."""

    def _patches(self):
        return [
            patch("seekr_hatchery.cli.subprocess.run"),
            patch(
                "seekr_hatchery.cli.tasks.load_task",
                return_value={"name": "t", "status": "in-progress", "branch": ""},
            ),
            patch("seekr_hatchery.cli.tasks.save_task"),
            patch("seekr_hatchery.cli._post_exit_check"),
            patch("seekr_hatchery.cli._chat_post_exit"),
            patch("seekr_hatchery.cli.os.chdir"),
        ]

    def test_launch_resume_chat_empty_system_prompt(self, spy_backend):

        with ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)
            _launch_resume(
                repo=Path("/repo"),
                worktree=Path("/repo"),
                name="chat-1",
                session_id="sid",
                backend=spy_backend,
                runtime=None,
                branch="",
                main_branch="main",
                no_worktree=True,
                is_chat=True,
            )

        build_call = [c for c in spy_backend.calls if c[0] == "build_resume_command"][0]
        system_prompt = build_call[2]
        assert system_prompt == ""

    def test_launch_resume_chat_calls_chat_post_exit(self, spy_backend):

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in self._patches()]
            mock_post_exit = mocks[3]
            mock_chat_post_exit = mocks[4]
            _launch_resume(
                repo=Path("/repo"),
                worktree=Path("/repo"),
                name="chat-1",
                session_id="sid",
                backend=spy_backend,
                runtime=None,
                branch="",
                main_branch="main",
                no_worktree=True,
                is_chat=True,
            )

        assert mock_chat_post_exit.called
        assert not mock_post_exit.called


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
            patch("seekr_hatchery.cli._next_chat_name", return_value="chat-1"),
            patch("seekr_hatchery.cli.docker.ensure_dockerfile", return_value=False),
            patch("seekr_hatchery.cli.docker.ensure_docker_config", return_value=False),
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli.tasks.save_task", side_effect=lambda m: saved_meta.update(m)),
            patch("seekr_hatchery.cli._launch_new"),
            patch("seekr_hatchery.cli.tasks.task_db_path") as mock_db_path,
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
            patch("seekr_hatchery.cli.tasks.save_task", side_effect=lambda m: saved_meta.update(m)),
            patch("seekr_hatchery.cli._launch_new"),
            patch("seekr_hatchery.cli.tasks.task_db_path") as mock_db_path,
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
            patch("seekr_hatchery.cli._next_chat_name", return_value="chat-1"),
            patch("seekr_hatchery.cli.docker.ensure_dockerfile", return_value=False),
            patch("seekr_hatchery.cli.docker.ensure_docker_config", return_value=False),
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli.tasks.save_task"),
            patch("seekr_hatchery.cli._launch_new") as mock_launch,
            patch("seekr_hatchery.cli.tasks.task_db_path") as mock_db_path,
        ):
            mock_db_path.return_value = MagicMock(exists=lambda: False)
            runner.invoke(cli, ["chat"])

        assert mock_launch.called
        call_kwargs = mock_launch.call_args
        args = call_kwargs[0] if call_kwargs[0] else []
        kwargs = call_kwargs[1] if call_kwargs[1] else {}
        # is_chat should be True (either as positional or keyword arg)
        assert kwargs.get("is_chat") is True or (len(args) > 9 and args[9] is True)


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
        tasks.save_task(meta)
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
        tasks.save_task(meta)
        with patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(Path("/my/repo"), True)):
            result = runner.invoke(cli, ["status", "my-task"])
        assert result.exit_code == 0
        assert "task" in result.output


class TestResumeChat:
    """Test that cmd_resume passes is_chat for chat metadata."""

    def test_resume_passes_is_chat_for_chat(self, fake_tasks_db):
        runner = CliRunner()

        with (
            patch("seekr_hatchery.cli.tasks.load_task") as mock_load,
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli._launch_resume") as mock_launch,
        ):
            worktree = MagicMock(spec=Path)
            worktree.exists.return_value = True
            mock_load.return_value = {
                "name": "chat-1",
                "type": "chat",
                "branch": "",
                "worktree": "/some/repo",
                "repo": "/some/repo",
                "session_id": "sid-123",
                "no_worktree": True,
            }

            with patch("seekr_hatchery.cli.Path") as mock_path_cls:
                mock_path_cls.return_value = worktree
                runner.invoke(cli, ["resume", "chat-1"])

        assert mock_launch.called
        call_kwargs = mock_launch.call_args
        args = call_kwargs[0] if call_kwargs[0] else []
        kwargs = call_kwargs[1] if call_kwargs[1] else {}
        # is_chat should be True
        assert kwargs.get("is_chat") is True or (len(args) > 9 and args[9] is True)

    def test_resume_passes_is_chat_false_for_task(self, fake_tasks_db):
        runner = CliRunner()

        with (
            patch("seekr_hatchery.cli.tasks.load_task") as mock_load,
            patch("seekr_hatchery.cli.docker.resolve_runtime", return_value=None),
            patch("seekr_hatchery.cli.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.cli._launch_resume") as mock_launch,
        ):
            worktree = MagicMock(spec=Path)
            worktree.exists.return_value = True
            mock_load.return_value = {
                "name": "my-task",
                "branch": "hatchery/my-task",
                "worktree": "/some/worktree",
                "repo": "/some/repo",
                "session_id": "sid-123",
            }

            with patch("seekr_hatchery.cli.Path") as mock_path_cls:
                mock_path_cls.return_value = worktree
                runner.invoke(cli, ["resume", "my-task"])

        assert mock_launch.called
        call_kwargs = mock_launch.call_args
        args = call_kwargs[0] if call_kwargs[0] else []
        kwargs = call_kwargs[1] if call_kwargs[1] else {}
        # is_chat should be False (default)
        is_chat = kwargs.get("is_chat", False) if kwargs else (args[9] if len(args) > 9 else False)
        assert is_chat is False


# ---------------------------------------------------------------------------
# CLI dispatch — exec
# ---------------------------------------------------------------------------


class TestExec:
    def test_exec_dispatches_to_exec_task_shell(self, tmp_path):
        runner = CliRunner()
        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(tmp_path, True)),
            patch("seekr_hatchery.cli.docker.detect_runtime", return_value=docker.Runtime.DOCKER),
            patch("seekr_hatchery.cli.docker.exec_task_shell") as mock_exec,
        ):
            result = runner.invoke(cli, ["exec", "my-task"])
        assert result.exit_code == 0, result.output
        assert mock_exec.called
        mock_exec.assert_called_once_with("my-task", docker.Runtime.DOCKER, tmp_path, shell="/bin/bash")

    def test_exec_custom_shell(self, tmp_path):
        runner = CliRunner()
        with (
            patch("seekr_hatchery.cli.git.git_root_or_cwd", return_value=(tmp_path, True)),
            patch("seekr_hatchery.cli.docker.detect_runtime", return_value=docker.Runtime.DOCKER),
            patch("seekr_hatchery.cli.docker.exec_task_shell") as mock_exec,
        ):
            result = runner.invoke(cli, ["exec", "my-task", "--shell", "/bin/sh"])
        assert result.exit_code == 0, result.output
        mock_exec.assert_called_once_with("my-task", docker.Runtime.DOCKER, tmp_path, shell="/bin/sh")


# ---------------------------------------------------------------------------
# Shell completion
# ---------------------------------------------------------------------------


class TestCompletion:
    def test_task_name_type_empty_on_no_tasks(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        t = TaskNameType()
        with patch("seekr_hatchery.tasks.TASKS_DB_DIR", tmp_path / "nonexistent"):
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
            patch("seekr_hatchery.tasks.repo_tasks_for_current_repo", return_value=fake_tasks),
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
