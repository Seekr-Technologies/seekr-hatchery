"""Tests for the spawn module (process_spawn public API)."""

import json
from unittest.mock import patch

import seekr_hatchery.tasks as tasks
from seekr_hatchery.spawn import SpawnRequest, _launch_background, process_spawn

# ---------------------------------------------------------------------------
# process_spawn
# ---------------------------------------------------------------------------


class TestProcessSpawn:
    def _make_request(self, name="new-task"):
        return SpawnRequest(name=name, objective="Do the thing", base="hatchery/parent")

    @patch("seekr_hatchery.spawn._launch_background")
    @patch("seekr_hatchery.spawn.tasks.save_task")
    @patch("seekr_hatchery.spawn.tasks.run")
    @patch("seekr_hatchery.spawn.tasks.write_task_file")
    @patch("seekr_hatchery.spawn.git.create_worktree")
    def test_happy_path(self, mock_wt, mock_write, mock_run, mock_save, mock_bg, fake_repo):
        req = self._make_request()
        result = process_spawn(req, fake_repo, "parent-task")

        assert result is True
        mock_wt.assert_called_once()
        mock_write.assert_called_once()
        assert mock_run.call_count == 2  # git add + git commit
        mock_save.assert_called_once()

    @patch("seekr_hatchery.spawn._launch_background")
    @patch("seekr_hatchery.spawn.tasks.save_task")
    @patch("seekr_hatchery.spawn.tasks.run")
    @patch("seekr_hatchery.spawn.tasks.write_task_file")
    @patch("seekr_hatchery.spawn.git.create_worktree")
    def test_metadata_has_spawned_from(self, mock_wt, mock_write, mock_run, mock_save, mock_bg, fake_repo):
        req = self._make_request()
        process_spawn(req, fake_repo, "parent-task")

        saved_meta = mock_save.call_args[0][0]
        assert saved_meta["spawned_from"] == "parent-task"
        assert saved_meta["name"] == "new-task"
        assert saved_meta["branch"] == "hatchery/new-task"
        assert saved_meta["status"] == "in-progress"

    @patch("seekr_hatchery.spawn._launch_background")
    @patch("seekr_hatchery.spawn.tasks.save_task")
    @patch("seekr_hatchery.spawn.tasks.run")
    @patch("seekr_hatchery.spawn.tasks.write_task_file")
    @patch("seekr_hatchery.spawn.git.create_worktree")
    def test_name_conflict_skips(self, mock_wt, mock_write, mock_run, mock_save, mock_bg, fake_repo):
        # Create existing active task
        db_path = tasks.task_db_path(fake_repo, "new-task")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_text(json.dumps({"status": "in-progress"}))

        req = self._make_request()
        result = process_spawn(req, fake_repo, "parent-task")

        assert result is False
        mock_wt.assert_not_called()

    @patch("seekr_hatchery.spawn._launch_background")
    @patch("seekr_hatchery.spawn.tasks.save_task")
    @patch("seekr_hatchery.spawn.tasks.run")
    @patch("seekr_hatchery.spawn.tasks.write_task_file")
    @patch("seekr_hatchery.spawn.git.create_worktree")
    def test_completed_task_allows_overwrite(self, mock_wt, mock_write, mock_run, mock_save, mock_bg, fake_repo):
        # Existing completed task should not block
        db_path = tasks.task_db_path(fake_repo, "new-task")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_text(json.dumps({"status": "complete"}))

        req = self._make_request()
        result = process_spawn(req, fake_repo, "parent-task")

        assert result is True
        mock_wt.assert_called_once()

    @patch("seekr_hatchery.spawn.tasks.run")
    @patch("seekr_hatchery.spawn.tasks.write_task_file")
    @patch("seekr_hatchery.spawn.git.create_worktree", side_effect=Exception("git failed"))
    def test_git_failure_returns_false(self, mock_wt, mock_write, mock_run, fake_repo):
        req = self._make_request()
        result = process_spawn(req, fake_repo, "parent-task")

        assert result is False

    @patch("seekr_hatchery.spawn._launch_background")
    @patch("seekr_hatchery.spawn.tasks.save_task")
    @patch("seekr_hatchery.spawn.tasks.run")
    @patch("seekr_hatchery.spawn.tasks.write_task_file")
    @patch("seekr_hatchery.spawn.git.create_worktree")
    def test_source_file_deleted_on_success(
        self, mock_wt, mock_write, mock_run, mock_save, mock_bg, tmp_path, fake_repo
    ):
        """When a source_file is provided (legacy), it is deleted on success."""
        f = tmp_path / "req.md"
        f.write_text("objective")
        req = SpawnRequest(name="new-task", objective="Do the thing", base="hatchery/parent", source_file=f)
        assert f.exists()
        process_spawn(req, fake_repo, "parent-task")
        assert not f.exists()

    @patch("seekr_hatchery.spawn.tasks.run")
    @patch("seekr_hatchery.spawn.tasks.write_task_file")
    @patch("seekr_hatchery.spawn.git.create_worktree", side_effect=Exception("boom"))
    def test_source_file_not_deleted_on_failure(self, mock_wt, mock_write, mock_run, tmp_path, fake_repo):
        """When processing fails, the source_file is not deleted."""
        f = tmp_path / "req.md"
        f.write_text("objective")
        req = SpawnRequest(name="new-task", objective="Do the thing", base="hatchery/parent", source_file=f)
        process_spawn(req, fake_repo, "parent-task")
        assert f.exists()

    @patch("seekr_hatchery.spawn._launch_background")
    @patch("seekr_hatchery.spawn.tasks.save_task")
    @patch("seekr_hatchery.spawn.tasks.run")
    @patch("seekr_hatchery.spawn.tasks.write_task_file")
    @patch("seekr_hatchery.spawn.git.create_worktree")
    def test_no_source_file_is_fine(self, mock_wt, mock_write, mock_run, mock_save, mock_bg, fake_repo):
        """When source_file is None (MCP path), processing succeeds without file deletion."""
        req = self._make_request()
        assert req.source_file is None
        result = process_spawn(req, fake_repo, "parent-task")
        assert result is True

    @patch("seekr_hatchery.spawn._launch_background")
    @patch("seekr_hatchery.spawn.tasks.save_task")
    @patch("seekr_hatchery.spawn.tasks.run")
    @patch("seekr_hatchery.spawn.tasks.write_task_file")
    @patch("seekr_hatchery.spawn.git.create_worktree")
    def test_launch_background_called_on_success(self, mock_wt, mock_write, mock_run, mock_save, mock_bg, fake_repo):
        """_launch_background is called after successful provisioning."""
        req = self._make_request()
        process_spawn(req, fake_repo, "parent-task")
        mock_bg.assert_called_once_with(fake_repo, "new-task", "hatchery/new-task")

    @patch("seekr_hatchery.spawn._launch_background", side_effect=Exception("popen failed"))
    @patch("seekr_hatchery.spawn.tasks.save_task")
    @patch("seekr_hatchery.spawn.tasks.run")
    @patch("seekr_hatchery.spawn.tasks.write_task_file")
    @patch("seekr_hatchery.spawn.git.create_worktree")
    def test_launch_background_failure_does_not_fail_spawn(
        self, mock_wt, mock_write, mock_run, mock_save, mock_bg, fake_repo
    ):
        """If _launch_background raises, process_spawn still returns True."""
        req = self._make_request()
        result = process_spawn(req, fake_repo, "parent-task")
        assert result is True


# ---------------------------------------------------------------------------
# _launch_background
# ---------------------------------------------------------------------------


class TestLaunchBackground:
    def test_calls_popen_with_spawn_launch(self, fake_repo):
        """_launch_background fires a subprocess with _spawn-launch subcommand."""
        from unittest.mock import patch as _patch

        with _patch("seekr_hatchery.spawn.subprocess.Popen") as mock_popen:
            _launch_background(fake_repo, "my-task", "hatchery/my-task")

        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]
        assert "_spawn-launch" in cmd
        assert "--name" in cmd
        assert "my-task" in cmd
        assert "--branch" in cmd
        assert "hatchery/my-task" in cmd

    def test_uses_start_new_session(self, fake_repo):
        """start_new_session=True ensures the daemon is decoupled from the parent."""
        from unittest.mock import patch as _patch

        with _patch("seekr_hatchery.spawn.subprocess.Popen") as mock_popen:
            _launch_background(fake_repo, "my-task", "hatchery/my-task")

        kwargs = mock_popen.call_args[1]
        assert kwargs.get("start_new_session") is True

    def test_devnull_output(self, fake_repo):
        """stdout and stderr must be DEVNULL so the parent terminal isn't polluted."""
        import subprocess
        from unittest.mock import patch as _patch

        with _patch("seekr_hatchery.spawn.subprocess.Popen") as mock_popen:
            _launch_background(fake_repo, "my-task", "hatchery/my-task")

        kwargs = mock_popen.call_args[1]
        assert kwargs.get("stdout") is subprocess.DEVNULL
        assert kwargs.get("stderr") is subprocess.DEVNULL


class TestSpawnRequest:
    def test_defaults(self):
        req = SpawnRequest(name="test", objective="obj", base="main")
        assert req.source_file is None

    def test_with_source_file(self, tmp_path):
        f = tmp_path / "test.md"
        req = SpawnRequest(name="test", objective="obj", base="main", source_file=f)
        assert req.source_file == f
