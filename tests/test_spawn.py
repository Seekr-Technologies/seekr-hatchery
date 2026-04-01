"""Tests for the spawn module (process_spawn public API)."""

import json
from unittest.mock import patch

import seekr_hatchery.tasks as tasks
from seekr_hatchery.spawn import SpawnRequest, process_spawn

# ---------------------------------------------------------------------------
# process_spawn
# ---------------------------------------------------------------------------


class TestProcessSpawn:
    def _make_request(self, name="new-task"):
        return SpawnRequest(name=name, objective="Do the thing", base="hatchery/parent")

    @patch("seekr_hatchery.spawn.tasks.save_task")
    @patch("seekr_hatchery.spawn.tasks.run")
    @patch("seekr_hatchery.spawn.tasks.write_task_file")
    @patch("seekr_hatchery.spawn.git.create_worktree")
    def test_happy_path(self, mock_wt, mock_write, mock_run, mock_save, fake_repo):
        req = self._make_request()
        result = process_spawn(req, fake_repo, "parent-task")

        assert result is True
        mock_wt.assert_called_once()
        mock_write.assert_called_once()
        assert mock_run.call_count == 2  # git add + git commit
        mock_save.assert_called_once()

    @patch("seekr_hatchery.spawn.tasks.save_task")
    @patch("seekr_hatchery.spawn.tasks.run")
    @patch("seekr_hatchery.spawn.tasks.write_task_file")
    @patch("seekr_hatchery.spawn.git.create_worktree")
    def test_metadata_has_spawned_from(self, mock_wt, mock_write, mock_run, mock_save, fake_repo):
        req = self._make_request()
        process_spawn(req, fake_repo, "parent-task")

        saved_meta = mock_save.call_args[0][0]
        assert saved_meta["spawned_from"] == "parent-task"
        assert saved_meta["name"] == "new-task"
        assert saved_meta["branch"] == "hatchery/new-task"
        assert saved_meta["status"] == "in-progress"

    @patch("seekr_hatchery.spawn.tasks.save_task")
    @patch("seekr_hatchery.spawn.tasks.run")
    @patch("seekr_hatchery.spawn.tasks.write_task_file")
    @patch("seekr_hatchery.spawn.git.create_worktree")
    def test_name_conflict_skips(self, mock_wt, mock_write, mock_run, mock_save, fake_repo):
        # Create existing active task
        db_path = tasks.task_db_path(fake_repo, "new-task")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_text(json.dumps({"status": "in-progress"}))

        req = self._make_request()
        result = process_spawn(req, fake_repo, "parent-task")

        assert result is False
        mock_wt.assert_not_called()

    @patch("seekr_hatchery.spawn.tasks.save_task")
    @patch("seekr_hatchery.spawn.tasks.run")
    @patch("seekr_hatchery.spawn.tasks.write_task_file")
    @patch("seekr_hatchery.spawn.git.create_worktree")
    def test_completed_task_allows_overwrite(self, mock_wt, mock_write, mock_run, mock_save, fake_repo):
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

    @patch("seekr_hatchery.spawn.tasks.save_task")
    @patch("seekr_hatchery.spawn.tasks.run")
    @patch("seekr_hatchery.spawn.tasks.write_task_file")
    @patch("seekr_hatchery.spawn.git.create_worktree")
    def test_source_file_deleted_on_success(self, mock_wt, mock_write, mock_run, mock_save, tmp_path, fake_repo):
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

    @patch("seekr_hatchery.spawn.tasks.save_task")
    @patch("seekr_hatchery.spawn.tasks.run")
    @patch("seekr_hatchery.spawn.tasks.write_task_file")
    @patch("seekr_hatchery.spawn.git.create_worktree")
    def test_no_source_file_is_fine(self, mock_wt, mock_write, mock_run, mock_save, fake_repo):
        """When source_file is None (MCP path), processing succeeds without file deletion."""
        req = self._make_request()
        assert req.source_file is None
        result = process_spawn(req, fake_repo, "parent-task")
        assert result is True


class TestSpawnRequest:
    def test_defaults(self):
        req = SpawnRequest(name="test", objective="obj", base="main")
        assert req.source_file is None

    def test_with_source_file(self, tmp_path):
        f = tmp_path / "test.md"
        req = SpawnRequest(name="test", objective="obj", base="main", source_file=f)
        assert req.source_file == f
