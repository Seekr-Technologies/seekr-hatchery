"""Tests for task I/O: save_task, load_task, repo_tasks_for_current_repo."""

import json
from pathlib import Path

import pytest

import seekr_hatchery.tasks as tasks

# ---------------------------------------------------------------------------
# save_task
# ---------------------------------------------------------------------------


_REPO = Path("/my/repo")


class TestSaveTask:
    def test_creates_json_file(self, fake_tasks_db):
        meta = {"name": "my-task", "repo": str(_REPO), "status": "paused"}
        tasks.save_task(meta)
        path = fake_tasks_db / tasks.repo_id(_REPO) / "my-task" / "meta.json"
        assert path.exists()

    def test_stamps_schema_version(self, fake_tasks_db):
        meta = {"name": "my-task", "repo": str(_REPO), "status": "paused"}
        tasks.save_task(meta)
        path = fake_tasks_db / tasks.repo_id(_REPO) / "my-task" / "meta.json"
        saved = json.loads(path.read_text())
        assert saved["schema_version"] == tasks.SCHEMA_VERSION

    def test_creates_parent_dirs(self, tmp_path, monkeypatch):
        db = tmp_path / "deep" / "nested" / "tasks"
        monkeypatch.setattr(tasks, "TASKS_DB_DIR", db)
        meta = {"name": "task1", "repo": str(_REPO), "status": "paused"}
        tasks.save_task(meta)
        assert (db / tasks.repo_id(_REPO) / "task1" / "meta.json").exists()

    def test_file_is_valid_json(self, fake_tasks_db):
        meta = {"name": "test", "repo": str(_REPO), "status": "paused", "branch": "hatchery/test"}
        tasks.save_task(meta)
        path = fake_tasks_db / tasks.repo_id(_REPO) / "test" / "meta.json"
        loaded = json.loads(path.read_text())
        assert isinstance(loaded, dict)

    def test_overwrites_on_second_save(self, fake_tasks_db):
        meta = {"name": "task", "repo": str(_REPO), "status": "paused"}
        tasks.save_task(meta)
        meta["status"] = "complete"
        tasks.save_task(meta)
        path = fake_tasks_db / tasks.repo_id(_REPO) / "task" / "meta.json"
        saved = json.loads(path.read_text())
        assert saved["status"] == "complete"

    def test_all_fields_preserved(self, fake_tasks_db):
        meta = {
            "name": "full-task",
            "branch": "hatchery/full-task",
            "worktree": "/some/path",
            "repo": "/my/repo",
            "status": "paused",
            "created": "2026-01-01T00:00:00",
            "session_id": "uuid-1234",
        }
        tasks.save_task(meta)
        path = fake_tasks_db / tasks.repo_id(_REPO) / "full-task" / "meta.json"
        saved = json.loads(path.read_text())
        for key in meta:
            assert saved[key] == meta[key]


# ---------------------------------------------------------------------------
# load_task
# ---------------------------------------------------------------------------


class TestLoadTask:
    def test_round_trip(self, fake_tasks_db):
        meta = {
            "name": "round-trip",
            "repo": str(_REPO),
            "status": "paused",
            "branch": "hatchery/round-trip",
        }
        tasks.save_task(meta)
        loaded = tasks.load_task(_REPO, "round-trip")
        assert loaded["name"] == "round-trip"
        assert loaded["status"] == "paused"

    def test_exits_when_not_found(self, fake_tasks_db):
        with pytest.raises(SystemExit) as exc_info:
            tasks.load_task(_REPO, "nonexistent")
        assert exc_info.value.code == 1

    def test_stderr_message_on_missing(self, fake_tasks_db, capsys):
        with pytest.raises(SystemExit):
            tasks.load_task(_REPO, "missing-task")
        captured = capsys.readouterr()
        assert "missing-task" in captured.err

    def test_returns_dict(self, sample_meta):
        loaded = tasks.load_task(Path("/some/repo"), "my-task")
        assert isinstance(loaded, dict)

    def test_loads_all_fields(self, sample_meta):
        loaded = tasks.load_task(Path("/some/repo"), "my-task")
        assert loaded["name"] == "my-task"
        assert loaded["branch"] == "hatchery/my-task"
        assert loaded["status"] == "paused"


# ---------------------------------------------------------------------------
# repo_tasks_for_current_repo
# ---------------------------------------------------------------------------


def _write_scoped(fake_tasks_db: Path, task: dict) -> None:
    """Write a task JSON to the unified dir path for its repo."""
    repo = Path(task["repo"])
    task_dir = fake_tasks_db / tasks.repo_id(repo) / task["name"]
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "meta.json").write_text(json.dumps(task))


class TestRepoTasksForCurrentRepo:
    def test_returns_empty_when_dir_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tasks, "TASKS_DB_DIR", tmp_path / "nonexistent")
        result = tasks.repo_tasks_for_current_repo(Path("/my/repo"))
        assert result == []

    def test_filters_by_repo(self, fake_tasks_db):
        # Task for this repo (scoped)
        task1 = {
            "name": "task1",
            "repo": "/my/repo",
            "status": "paused",
            "created": "2026-01-01T10:00:00",
        }
        # Task for a different repo (scoped)
        task2 = {
            "name": "task2",
            "repo": "/other/repo",
            "status": "paused",
            "created": "2026-01-01T09:00:00",
        }
        _write_scoped(fake_tasks_db, task1)
        _write_scoped(fake_tasks_db, task2)
        result = tasks.repo_tasks_for_current_repo(Path("/my/repo"))
        assert len(result) == 1
        assert result[0]["name"] == "task1"

    def test_sorted_newest_first(self, fake_tasks_db):
        task_list = [
            {"name": "older", "repo": "/my/repo", "created": "2026-01-01T08:00:00", "status": "done"},
            {"name": "newer", "repo": "/my/repo", "created": "2026-01-02T10:00:00", "status": "done"},
            {"name": "middle", "repo": "/my/repo", "created": "2026-01-01T12:00:00", "status": "done"},
        ]
        for t in task_list:
            _write_scoped(fake_tasks_db, t)
        result = tasks.repo_tasks_for_current_repo(Path("/my/repo"))
        assert result[0]["name"] == "newer"
        assert result[1]["name"] == "middle"
        assert result[2]["name"] == "older"

    def test_skips_malformed_json_in_flat_dir(self, fake_tasks_db):
        (fake_tasks_db / "bad.json").write_text("not json {{{")
        task = {"name": "good", "repo": "/my/repo", "created": "2026-01-01", "status": "done"}
        _write_scoped(fake_tasks_db, task)
        result = tasks.repo_tasks_for_current_repo(Path("/my/repo"))
        assert len(result) == 1
        assert result[0]["name"] == "good"

    def test_returns_list(self, fake_tasks_db):
        result = tasks.repo_tasks_for_current_repo(Path("/my/repo"))
        assert isinstance(result, list)

    def test_empty_when_no_matching_tasks(self, fake_tasks_db):
        task = {"name": "other", "repo": "/different/repo", "created": "2026-01-01", "status": "done"}
        _write_scoped(fake_tasks_db, task)
        result = tasks.repo_tasks_for_current_repo(Path("/my/repo"))
        assert result == []

    def test_flat_fallback_includes_matching_tasks(self, fake_tasks_db):
        # Simulate a pre-migration flat file for this repo
        task = {"name": "legacy", "repo": "/my/repo", "created": "2026-01-01", "status": "done"}
        (fake_tasks_db / "legacy.json").write_text(json.dumps(task))
        result = tasks.repo_tasks_for_current_repo(Path("/my/repo"))
        assert any(t["name"] == "legacy" for t in result)


# ---------------------------------------------------------------------------
# migrate_db
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_hatchery_dir(home: Path) -> Path:
    """Return HATCHERY_DIR (already home-redirected by the autouse fixture), creating it eagerly."""
    hatchery = home / ".hatchery"
    hatchery.mkdir(parents=True, exist_ok=True)
    (hatchery / "tasks").mkdir(exist_ok=True)
    return hatchery


class TestMigrateDb:
    def test_v0_no_tasks_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """migrate_db() with no tasks dir creates meta.json with schema_version 1, no error."""
        hatchery = tmp_path / "hatchery"
        hatchery.mkdir()
        monkeypatch.setattr(tasks, "HATCHERY_DIR", hatchery)
        monkeypatch.setattr(tasks, "TASKS_DB_DIR", hatchery / "tasks")  # does not exist

        tasks.migrate_db()

        meta_path = hatchery / "meta.json"
        assert meta_path.exists()
        assert json.loads(meta_path.read_text())["schema_version"] == 1

    def test_v0_scoped_json_promoted(self, fake_hatchery_dir: Path) -> None:
        """Scoped <name>.json is promoted to <name>/meta.json after migrate_db()."""
        db = fake_hatchery_dir / "tasks"
        repo_subdir = db / "my-repo-abcd1234"
        repo_subdir.mkdir()
        task_data = {"name": "my-task", "repo": "/my/repo", "status": "paused"}
        scoped_file = repo_subdir / "my-task.json"
        scoped_file.write_text(json.dumps(task_data))

        tasks.migrate_db()

        # Original scoped file is gone
        assert not scoped_file.exists()
        # Unified dir meta.json is present and correct
        dest = repo_subdir / "my-task" / "meta.json"
        assert dest.exists()
        assert json.loads(dest.read_text())["name"] == "my-task"

    def test_v0_scoped_json_skipped_if_unified_exists(self, fake_hatchery_dir: Path) -> None:
        """When unified meta.json already exists, migrate_db() deletes scoped .json and keeps unified."""
        db = fake_hatchery_dir / "tasks"
        repo_subdir = db / "my-repo-abcd1234"
        repo_subdir.mkdir()
        # Pre-existing unified dir
        unified_dir = repo_subdir / "my-task"
        unified_dir.mkdir()
        unified_meta = unified_dir / "meta.json"
        unified_meta.write_text(json.dumps({"name": "my-task", "repo": "/my/repo", "status": "complete"}))
        # Stale scoped file
        scoped_file = repo_subdir / "my-task.json"
        scoped_file.write_text(json.dumps({"name": "my-task", "repo": "/my/repo", "status": "old"}))

        tasks.migrate_db()

        # Scoped file deleted
        assert not scoped_file.exists()
        # Unified meta.json is intact and unchanged
        saved = json.loads(unified_meta.read_text())
        assert saved["status"] == "complete"

    def test_already_at_latest_no_change(self, fake_hatchery_dir: Path) -> None:
        """When meta.json already has schema_version 1, no files are touched."""
        db = fake_hatchery_dir / "tasks"
        meta_path = fake_hatchery_dir / "meta.json"
        meta_path.write_text(json.dumps({"schema_version": 1}))

        # Put a scoped JSON in there that should NOT be touched
        repo_subdir = db / "some-repo-11223344"
        repo_subdir.mkdir()
        scoped_file = repo_subdir / "untouched.json"
        scoped_file.write_text(json.dumps({"name": "untouched", "repo": "/some/repo"}))

        tasks.migrate_db()

        # Scoped file is untouched (migration did not run)
        assert scoped_file.exists()
        # meta.json still says version 1
        assert json.loads(meta_path.read_text())["schema_version"] == 1


# ---------------------------------------------------------------------------
# sandbox_context — chat mode (no_worktree + no branch)
# ---------------------------------------------------------------------------


class TestSandboxContextChat:
    def test_no_worktree_docker_no_branch(self):
        """sandbox_context with no_worktree=True, branch='', use_docker=True
        should describe an isolated Docker container without branch references."""
        result = tasks.sandbox_context(
            name="chat-1",
            branch="",
            worktree=Path("/repo"),
            repo=Path("/repo"),
            main_branch="main",
            use_docker=True,
            no_worktree=True,
        )
        assert "Docker container" in result
        assert "/workspace/" in result
        # Should NOT mention branches or PRs when branch is empty
        assert "branch" not in result.lower()
        assert "pull request" not in result.lower()
        assert "push" not in result.lower()
