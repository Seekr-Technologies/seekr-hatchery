"""Tests for task I/O: save_task, load_task, repo_tasks_for_current_repo."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import seekr_hatchery.sessions as sessions

# ---------------------------------------------------------------------------
# save_task
# ---------------------------------------------------------------------------


_REPO = Path("/my/repo")


class TestSaveTask:
    def test_creates_json_file(self, fake_tasks_db):
        meta = {"name": "my-task", "repo": str(_REPO), "status": "in-progress"}
        sessions.save_task(meta)
        path = fake_tasks_db / sessions.repo_id(_REPO) / "my-task" / "meta.json"
        assert path.exists()

    def test_stamps_schema_version(self, fake_tasks_db):
        meta = {"name": "my-task", "repo": str(_REPO), "status": "in-progress"}
        sessions.save_task(meta)
        path = fake_tasks_db / sessions.repo_id(_REPO) / "my-task" / "meta.json"
        saved = json.loads(path.read_text())
        assert saved["schema_version"] == sessions.SCHEMA_VERSION

    def test_creates_parent_dirs(self, tmp_path, monkeypatch):
        db = tmp_path / "deep" / "nested" / "tasks"
        monkeypatch.setattr(sessions, "TASKS_DB_DIR", db)
        meta = {"name": "task1", "repo": str(_REPO), "status": "in-progress"}
        sessions.save_task(meta)
        assert (db / sessions.repo_id(_REPO) / "task1" / "meta.json").exists()

    def test_file_is_valid_json(self, fake_tasks_db):
        meta = {"name": "test", "repo": str(_REPO), "status": "in-progress", "branch": "hatchery/test"}
        sessions.save_task(meta)
        path = fake_tasks_db / sessions.repo_id(_REPO) / "test" / "meta.json"
        loaded = json.loads(path.read_text())
        assert isinstance(loaded, dict)

    def test_overwrites_on_second_save(self, fake_tasks_db):
        meta = {"name": "task", "repo": str(_REPO), "status": "in-progress"}
        sessions.save_task(meta)
        meta["status"] = "complete"
        sessions.save_task(meta)
        path = fake_tasks_db / sessions.repo_id(_REPO) / "task" / "meta.json"
        saved = json.loads(path.read_text())
        assert saved["status"] == "complete"

    def test_all_fields_preserved(self, fake_tasks_db):
        meta = {
            "name": "full-task",
            "branch": "hatchery/full-task",
            "worktree": "/some/path",
            "repo": "/my/repo",
            "status": "in-progress",
            "created": "2026-01-01T00:00:00",
            "session_id": "uuid-1234",
        }
        sessions.save_task(meta)
        path = fake_tasks_db / sessions.repo_id(_REPO) / "full-task" / "meta.json"
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
            "status": "in-progress",
            "branch": "hatchery/round-trip",
        }
        sessions.save_task(meta)
        loaded = sessions.load_task(_REPO, "round-trip")
        assert loaded["name"] == "round-trip"
        assert loaded["status"] == "in-progress"

    def test_exits_when_not_found(self, fake_tasks_db):
        with pytest.raises(SystemExit) as exc_info:
            sessions.load_task(_REPO, "nonexistent")
        assert exc_info.value.code == 1

    def test_stderr_message_on_missing(self, fake_tasks_db, capsys):
        with pytest.raises(SystemExit):
            sessions.load_task(_REPO, "missing-task")
        captured = capsys.readouterr()
        assert "missing-task" in captured.err

    def test_returns_dict(self, sample_meta):
        loaded = sessions.load_task(Path("/some/repo"), "my-task")
        assert isinstance(loaded, dict)

    def test_loads_all_fields(self, sample_meta):
        loaded = sessions.load_task(Path("/some/repo"), "my-task")
        assert loaded["name"] == "my-task"
        assert loaded["branch"] == "hatchery/my-task"
        assert loaded["status"] == "in-progress"


# ---------------------------------------------------------------------------
# repo_tasks_for_current_repo
# ---------------------------------------------------------------------------


def _write_scoped(fake_tasks_db: Path, task: dict) -> None:
    """Write a task JSON to the unified dir path for its repo."""
    repo = Path(task["repo"])
    task_dir = fake_tasks_db / sessions.repo_id(repo) / task["name"]
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "meta.json").write_text(json.dumps(task))


class TestRepoTasksForCurrentRepo:
    def test_returns_empty_when_dir_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sessions, "TASKS_DB_DIR", tmp_path / "nonexistent")
        result = sessions.repo_tasks_for_current_repo(Path("/my/repo"))
        assert result == []

    def test_filters_by_repo(self, fake_tasks_db):
        # Task for this repo (scoped)
        task1 = {
            "name": "task1",
            "repo": "/my/repo",
            "status": "in-progress",
            "created": "2026-01-01T10:00:00",
        }
        # Task for a different repo (scoped)
        task2 = {
            "name": "task2",
            "repo": "/other/repo",
            "status": "in-progress",
            "created": "2026-01-01T09:00:00",
        }
        _write_scoped(fake_tasks_db, task1)
        _write_scoped(fake_tasks_db, task2)
        result = sessions.repo_tasks_for_current_repo(Path("/my/repo"))
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
        result = sessions.repo_tasks_for_current_repo(Path("/my/repo"))
        assert result[0]["name"] == "newer"
        assert result[1]["name"] == "middle"
        assert result[2]["name"] == "older"

    def test_skips_malformed_json_in_flat_dir(self, fake_tasks_db):
        (fake_tasks_db / "bad.json").write_text("not json {{{")
        task = {"name": "good", "repo": "/my/repo", "created": "2026-01-01", "status": "done"}
        _write_scoped(fake_tasks_db, task)
        result = sessions.repo_tasks_for_current_repo(Path("/my/repo"))
        assert len(result) == 1
        assert result[0]["name"] == "good"

    def test_returns_list(self, fake_tasks_db):
        result = sessions.repo_tasks_for_current_repo(Path("/my/repo"))
        assert isinstance(result, list)

    def test_empty_when_no_matching_tasks(self, fake_tasks_db):
        task = {"name": "other", "repo": "/different/repo", "created": "2026-01-01", "status": "done"}
        _write_scoped(fake_tasks_db, task)
        result = sessions.repo_tasks_for_current_repo(Path("/my/repo"))
        assert result == []

    def test_flat_fallback_includes_matching_tasks(self, fake_tasks_db):
        # Simulate a pre-migration flat file for this repo
        task = {"name": "legacy", "repo": "/my/repo", "created": "2026-01-01", "status": "done"}
        (fake_tasks_db / "legacy.json").write_text(json.dumps(task))
        result = sessions.repo_tasks_for_current_repo(Path("/my/repo"))
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
        monkeypatch.setattr(sessions, "HATCHERY_DIR", hatchery)
        monkeypatch.setattr(sessions, "TASKS_DB_DIR", hatchery / "tasks")  # does not exist

        sessions.migrate_db()

        meta_path = hatchery / "meta.json"
        assert meta_path.exists()
        assert json.loads(meta_path.read_text())["schema_version"] == 1

    def test_v0_scoped_json_promoted(self, fake_hatchery_dir: Path) -> None:
        """Scoped <name>.json is promoted to <name>/meta.json after migrate_db()."""
        db = fake_hatchery_dir / "tasks"
        repo_subdir = db / "my-repo-abcd1234"
        repo_subdir.mkdir()
        task_data = {"name": "my-task", "repo": "/my/repo", "status": "in-progress"}
        scoped_file = repo_subdir / "my-task.json"
        scoped_file.write_text(json.dumps(task_data))

        sessions.migrate_db()

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

        sessions.migrate_db()

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

        sessions.migrate_db()

        # Scoped file is untouched (migration did not run)
        assert scoped_file.exists()
        # meta.json still says version 1
        assert json.loads(meta_path.read_text())["schema_version"] == 1


# ---------------------------------------------------------------------------
# sandbox_context — chat mode (no_worktree + no branch)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# include field round-trip
# ---------------------------------------------------------------------------


class TestIncludeRoundTrip:
    def test_include_field_persists(self, fake_tasks_db):
        """The 'include' list round-trips through save_task / load_task."""
        meta = {
            "name": "my-task",
            "repo": str(_REPO),
            "status": "in-progress",
            "include": ["/path/to/repo-b", "/shared/data"],
        }
        sessions.save_task(meta)
        loaded = sessions.load_task(_REPO, "my-task")
        assert loaded["include"] == ["/path/to/repo-b", "/shared/data"]

    def test_missing_include_defaults_to_empty(self, fake_tasks_db):
        """Older task metadata without 'include' loads without error."""
        meta = {
            "name": "my-task",
            "repo": str(_REPO),
            "status": "in-progress",
        }
        sessions.save_task(meta)
        loaded = sessions.load_task(_REPO, "my-task")
        assert loaded.get("include", []) == []


# ---------------------------------------------------------------------------


class TestSandboxContextChat:
    def test_no_worktree_docker_no_branch(self):
        """sandbox_context with no_worktree=True, branch='', use_docker=True
        should describe an isolated Docker container without branch references."""
        result = sessions.sandbox_context(
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


# ---------------------------------------------------------------------------
# SessionMeta model: load/save round-trip + property coverage
# ---------------------------------------------------------------------------


class TestSessionMetaRoundTrip:
    def test_load_returns_session_meta(self, fake_tasks_db, sample_meta):
        meta = sessions.load(Path(sample_meta["repo"]), sample_meta["name"])
        assert isinstance(meta, sessions.SessionMeta)
        assert meta.name == sample_meta["name"]
        assert meta.repo == sample_meta["repo"]
        assert meta.branch == sample_meta["branch"]
        assert meta.status == sample_meta["status"]
        assert meta.session_id == sample_meta["session_id"]

    def test_save_round_trips(self, fake_tasks_db):
        meta = sessions.SessionMeta(
            name="rt",
            repo="/some/repo",
            worktree="/some/repo/.hatchery/worktrees/rt",
            branch="hatchery/rt",
            session_id="sid",
        )
        sessions.save(meta)
        loaded = sessions.load(Path(meta.repo), meta.name)
        assert loaded == meta

    def test_save_writes_schema_version(self, fake_tasks_db):
        meta = sessions.SessionMeta(name="v", repo="/r", worktree="/r/w")
        sessions.save(meta)
        path = sessions.task_db_path(Path("/r"), "v")
        assert json.loads(path.read_text())["schema_version"] == sessions.SCHEMA_VERSION

    def test_save_excludes_none_completed(self, fake_tasks_db):
        """A freshly created session has completed=None — must not write null."""
        meta = sessions.SessionMeta(name="e", repo="/r", worktree="/r/w")
        sessions.save(meta)
        on_disk = json.loads(sessions.task_db_path(Path("/r"), "e").read_text())
        assert "completed" not in on_disk

    def test_save_writes_completed_when_set(self, fake_tasks_db):
        meta = sessions.SessionMeta(
            name="e", repo="/r", worktree="/r/w", completed="2026-01-01T00:00:00"
        )
        sessions.save(meta)
        on_disk = json.loads(sessions.task_db_path(Path("/r"), "e").read_text())
        assert on_disk["completed"] == "2026-01-01T00:00:00"

    def test_save_excludes_none_session_id(self, fake_tasks_db):
        """Like ``completed``, ``session_id=None`` is omitted from the JSON
        (matches the dict-based path, which never wrote keys whose values
        weren't explicitly assigned)."""
        meta = sessions.SessionMeta(name="s", repo="/r", worktree="/r/w")
        sessions.save(meta)
        on_disk = json.loads(sessions.task_db_path(Path("/r"), "s").read_text())
        assert "session_id" not in on_disk

    def test_legacy_dict_with_missing_fields_loads(self, fake_tasks_db):
        """Older meta.json files (lacking fields added in later versions)
        still load — the missing fields take Pydantic defaults. The
        specific default values are an implementation detail; this test
        just pins the backward-compatibility contract."""
        legacy = {
            "name": "legacy",
            "repo": "/some/repo",
            "worktree": "/some/repo/.hatchery/worktrees/legacy",
            "branch": "hatchery/legacy",
            "status": "in-progress",
            "created": "2026-01-15T10:00:00",
            "session_id": "abc",
            "schema_version": 1,
        }
        sessions.save_task(dict(legacy))  # raw dict write, no validation
        sessions.load(Path(legacy["repo"]), legacy["name"])  # shouldn't raise

    def test_chat_type_round_trips(self, fake_tasks_db):
        meta = sessions.SessionMeta(
            name="chat-1",
            repo="/some/repo",
            worktree="/some/repo",
            type="chat",
            no_worktree=True,
        )
        sessions.save(meta)
        loaded = sessions.load(Path(meta.repo), meta.name)
        assert loaded.type == "chat"
        assert loaded.is_chat is True

    def test_extra_field_in_meta_json_raises(self, fake_tasks_db):
        """extra='forbid' is the deliberate choice: migrate() must normalise legacy
        fields, the model must catch typos/ghost fields."""
        bad = {
            "name": "x",
            "repo": "/r",
            "worktree": "/r/w",
            "statuz": "in-progress",  # typo'd field
            "schema_version": 1,
        }
        sessions.save_task(dict(bad))
        with pytest.raises(ValidationError):
            sessions.load(Path("/r"), "x")

    def test_missing_required_field_raises(self, fake_tasks_db):
        """``name`` / ``repo`` / ``worktree`` have no defaults — omitting any
        of them at validation time raises ValidationError."""
        for missing in ("name", "repo", "worktree"):
            fields = {"name": "x", "repo": "/r", "worktree": "/r/w", "schema_version": 1}
            del fields[missing]
            # Write directly to disk; save_task itself requires name+repo to
            # compute the path, so we can't go through it here.
            path = sessions.TASKS_DB_DIR / sessions.repo_id(Path("/r")) / "x" / "meta.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(fields))
            with pytest.raises(ValidationError):
                sessions.load(Path("/r"), "x")

    def test_invalid_literal_status_raises(self, fake_tasks_db):
        bad = {
            "name": "ls",
            "repo": "/r",
            "worktree": "/r/w",
            "status": "bogus",  # not a valid SessionStatus literal
            "schema_version": 1,
        }
        sessions.save_task(dict(bad))
        with pytest.raises(ValidationError):
            sessions.load(Path("/r"), "ls")

    def test_invalid_literal_type_raises(self, fake_tasks_db):
        bad = {
            "name": "lt",
            "repo": "/r",
            "worktree": "/r/w",
            "type": "zombie",  # not a valid SessionType literal
            "schema_version": 1,
        }
        sessions.save_task(dict(bad))
        with pytest.raises(ValidationError):
            sessions.load(Path("/r"), "lt")

    def test_forward_schema_version_exits_with_clear_error(self, fake_tasks_db, capsys):
        """If meta.json was written by a newer hatchery (schema_version > ours),
        migrate() exits with an actionable error before Pydantic sees the dict."""
        future = {
            "name": "fv",
            "repo": "/r",
            "worktree": "/r/w",
            "schema_version": sessions.SCHEMA_VERSION + 1,
        }
        # Write directly — save_task would stamp schema_version back to the
        # current value, defeating the test.
        path = sessions.TASKS_DB_DIR / sessions.repo_id(Path("/r")) / "fv" / "meta.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(future))
        with pytest.raises(SystemExit) as exc_info:
            sessions.load(Path("/r"), "fv")
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "newer hatchery" in err
        assert "upgrade" in err.lower()

    def test_include_string_list_validates(self, fake_tasks_db):
        """The 'include' field accepts both old list[str] and new list[dict] formats."""
        meta_dict = {
            "name": "inc",
            "repo": "/r",
            "worktree": "/r/w",
            "include": ["/path/a", "/path/b"],
            "schema_version": 1,
        }
        sessions.save_task(dict(meta_dict))
        loaded = sessions.load(Path("/r"), "inc")
        assert loaded.include == ["/path/a", "/path/b"]
        # include_entries property parses raw form into IncludeEntry objects
        entries = loaded.include_entries
        assert len(entries) == 2
        assert all(e.mode == "worktree" for e in entries)


class TestSessionMetaProperties:
    def test_is_chat(self):
        assert sessions.SessionMeta(name="c", repo="/r", worktree="/r", type="chat").is_chat
        assert not sessions.SessionMeta(name="t", repo="/r", worktree="/r/w").is_chat

    def test_is_complete(self):
        m = sessions.SessionMeta(name="x", repo="/r", worktree="/r/w", status="complete")
        assert m.is_complete

    def test_repo_path_and_worktree_path(self):
        m = sessions.SessionMeta(name="x", repo="/a/b", worktree="/a/b/wt")
        assert m.repo_path == Path("/a/b")
        assert m.worktree_path == Path("/a/b/wt")

    def test_session_dir_delegates(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sessions, "TASKS_DB_DIR", tmp_path)
        m = sessions.SessionMeta(name="x", repo="/r", worktree="/r/w")
        assert m.session_dir == sessions.task_session_dir(Path("/r"), "x")

    def test_image_and_container_name_delegate(self):
        m = sessions.SessionMeta(name="x", repo="/a/repo", worktree="/a/repo/w")
        assert m.image_name == sessions.image_name(Path("/a/repo"), "x")
        assert m.container_name == sessions.container_name(Path("/a/repo"), "x")
