"""Tests for task I/O: save_task, load_task, repo_tasks_for_current_repo,
plus real-fs lifecycle tests for sessions.create / mark_done / archive /
delete / launch / merge_include_updates."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

import seekr_hatchery.agents as agent
import seekr_hatchery.constants as constants
import seekr_hatchery.git as git
import seekr_hatchery.sessions as sessions
import seekr_hatchery.utils as utils
from seekr_hatchery.includes import IncludeEntry, IncludeItem


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Small helper to run git in *repo* during the real-fs tests."""
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# save_task
# ---------------------------------------------------------------------------


_REPO = Path("/my/repo")


class TestSaveTask:
    def test_creates_json_file(self, fake_tasks_db):
        meta = {"name": "my-task", "repo": str(_REPO), "status": "in-progress"}
        sessions.save_task(meta)
        path = fake_tasks_db / utils.repo_id(_REPO) / "my-task" / "meta.json"
        assert path.exists()

    def test_stamps_schema_version(self, fake_tasks_db):
        meta = {"name": "my-task", "repo": str(_REPO), "status": "in-progress"}
        sessions.save_task(meta)
        path = fake_tasks_db / utils.repo_id(_REPO) / "my-task" / "meta.json"
        saved = json.loads(path.read_text())
        assert saved["schema_version"] == sessions.SCHEMA_VERSION

    def test_creates_parent_dirs(self, tmp_path, monkeypatch):
        db = tmp_path / "deep" / "nested" / "tasks"
        monkeypatch.setattr(sessions, "_TASKS_DB_DIR", db)
        meta = {"name": "task1", "repo": str(_REPO), "status": "in-progress"}
        sessions.save_task(meta)
        assert (db / utils.repo_id(_REPO) / "task1" / "meta.json").exists()

    def test_file_is_valid_json(self, fake_tasks_db):
        meta = {"name": "test", "repo": str(_REPO), "status": "in-progress", "branch": "hatchery/test"}
        sessions.save_task(meta)
        path = fake_tasks_db / utils.repo_id(_REPO) / "test" / "meta.json"
        loaded = json.loads(path.read_text())
        assert isinstance(loaded, dict)

    def test_overwrites_on_second_save(self, fake_tasks_db):
        meta = {"name": "task", "repo": str(_REPO), "status": "in-progress"}
        sessions.save_task(meta)
        meta["status"] = "complete"
        sessions.save_task(meta)
        path = fake_tasks_db / utils.repo_id(_REPO) / "task" / "meta.json"
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
        path = fake_tasks_db / utils.repo_id(_REPO) / "full-task" / "meta.json"
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
    task_dir = fake_tasks_db / utils.repo_id(repo) / task["name"]
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "meta.json").write_text(json.dumps(task))


class TestRepoTasksForCurrentRepo:
    def test_returns_empty_when_dir_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sessions, "_TASKS_DB_DIR", tmp_path / "nonexistent")
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
        monkeypatch.setattr(constants, "HATCHERY_DIR", hatchery)
        monkeypatch.setattr(sessions, "_TASKS_DB_DIR", hatchery / "tasks")  # does not exist

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
            worktree=Path("/host/repo"),
            repo=Path("/host/repo"),
            main_branch="main",
            use_docker=True,
            no_worktree=True,
        )
        assert "Docker container" in result
        # The working dir is now the host cwd path (mirroring), not /workspace.
        assert "/host/repo/" in result
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
        meta = sessions.SessionMeta(name="e", repo="/r", worktree="/r/w", completed="2026-01-01T00:00:00")
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
            path = sessions._TASKS_DB_DIR / utils.repo_id(Path("/r")) / "x" / "meta.json"
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
        path = sessions._TASKS_DB_DIR / utils.repo_id(Path("/r")) / "fv" / "meta.json"
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
        monkeypatch.setattr(sessions, "_TASKS_DB_DIR", tmp_path)
        m = sessions.SessionMeta(name="x", repo="/r", worktree="/r/w")
        assert m.session_dir == sessions.task_session_dir(Path("/r"), "x")

    def test_image_and_container_name_delegate(self):
        m = sessions.SessionMeta(name="x", repo="/a/repo", worktree="/a/repo/w")
        assert m.image_name == sessions.image_name(Path("/a/repo"), "x")
        assert m.container_name == sessions.container_name(Path("/a/repo"), "x")


# ---------------------------------------------------------------------------
# Real-fs lifecycle tests — exercise sessions.create / mark_done / archive
# / delete / launch / merge_include_updates against a real git repo.
# Only docker container operations are mocked; the rest is real filesystem.
# ---------------------------------------------------------------------------


class TestSessionCreateTask:
    """sessions.create(type='task') — real worktree creation, real docker scaffolding."""

    def test_creates_worktree_branch_and_meta(self, git_repo, fake_tasks_db, no_input):
        meta = sessions.create(
            name="my-task",
            repo=git_repo,
            type="task",
            backend=agent.CODEX,
            objective="Test objective",
        )
        expected_wt = git_repo / ".hatchery" / "worktrees" / "my-task"
        assert expected_wt.exists()
        assert meta.worktree_path == expected_wt
        assert _git(git_repo, "rev-parse", "--verify", "hatchery/my-task", check=False).returncode == 0
        assert sessions.task_db_path(git_repo, "my-task").exists()
        assert meta.status == "in-progress"
        assert meta.branch == "hatchery/my-task"

    def test_task_file_contains_objective(self, git_repo, fake_tasks_db, no_input):
        sessions.create(
            name="t",
            repo=git_repo,
            type="task",
            backend=agent.CODEX,
            objective="The task is to do X.",
        )
        task_file = sessions.find_task_file(git_repo / ".hatchery" / "worktrees" / "t" / ".hatchery" / "tasks", "t")
        assert task_file is not None
        assert "The task is to do X." in task_file.read_text()

    def test_no_worktree_skips_branch_and_worktree(self, git_repo, fake_tasks_db, no_input):
        meta = sessions.create(
            name="t",
            repo=git_repo,
            type="task",
            backend=agent.CODEX,
            no_worktree=True,
            objective="x",
        )
        assert _git(git_repo, "rev-parse", "--verify", "hatchery/t", check=False).returncode != 0
        assert meta.worktree_path == git_repo
        assert meta.no_worktree is True
        assert meta.branch == ""

    def test_in_progress_collision_exits(self, git_repo, fake_tasks_db, no_input):
        sessions.create(name="t", repo=git_repo, type="task", backend=agent.CODEX, objective="x")
        with pytest.raises(SystemExit):
            sessions.create(name="t", repo=git_repo, type="task", backend=agent.CODEX, objective="x")

    def test_includes_passed_through_to_meta(self, git_repo, fake_tasks_db, no_input, tmp_path):
        ref = tmp_path / "external"
        ref.mkdir()
        entries = [IncludeEntry(path=ref, mode="ro")]
        meta = sessions.create(
            name="t",
            repo=git_repo,
            type="task",
            backend=agent.CODEX,
            include_entries=entries,
            objective="x",
        )
        assert any(e["mode"] == "ro" and Path(e["path"]) == ref for e in meta.include)

    def test_dockerfile_is_committed(self, git_repo, fake_tasks_db, no_input):
        sessions.create(name="t", repo=git_repo, type="task", backend=agent.CODEX, objective="x")
        worktree = git_repo / ".hatchery" / "worktrees" / "t"
        assert (worktree / ".hatchery" / "Dockerfile.codex").exists()
        log = _git(worktree, "log", "--oneline").stdout
        assert "hatchery Docker configuration" in log

    def test_not_committed_skips_task_file_commit(self, git_repo, fake_tasks_db, no_input):
        sessions.create(
            name="t",
            repo=git_repo,
            type="task",
            backend=agent.CODEX,
            no_commit=True,
            objective="x",
        )
        worktree = git_repo / ".hatchery" / "worktrees" / "t"
        log = _git(worktree, "log", "--oneline").stdout
        assert "add task file" not in log


class TestSessionCreateChat:
    """sessions.create(type='chat') — no worktree, no task file."""

    def test_chat_skips_worktree_and_task_file(self, git_repo, fake_tasks_db, no_input):
        meta = sessions.create(name="chat-1", repo=git_repo, type="chat", backend=agent.CODEX)
        assert meta.is_chat
        assert meta.no_worktree is True
        assert not (git_repo / ".hatchery" / "worktrees" / "chat-1").exists()
        assert sessions.find_task_file(git_repo / ".hatchery" / "tasks", "chat-1") is None
        assert _git(git_repo, "rev-parse", "--verify", "hatchery/chat-1", check=False).returncode != 0


class TestRestoreWorktreeIfNeeded:
    """sessions.restore_worktree_if_needed — degraded-state resume recovery.

    Worktree creation is patched to a stub so tests don't need a real git
    repo; the call args carry the decisions we care about.
    """

    @staticmethod
    def _meta(tmp_path, *, status="in-progress", no_worktree=False, worktree_exists=False):
        wt = tmp_path / "wt"
        if worktree_exists:
            wt.mkdir(exist_ok=True)
        meta = sessions.SessionMeta(
            name="t",
            repo=str(tmp_path),
            worktree=str(wt),
            branch="hatchery/t",
            no_worktree=no_worktree,
            type="task",
            session_id="sid",
            status=status,
        )
        sessions.save(meta)
        return meta

    def _confirm(self, value: bool):
        return lambda base: value

    def test_noop_when_worktree_exists(self, fake_tasks_db, tmp_path):
        meta = self._meta(tmp_path, worktree_exists=True)
        with patch("seekr_hatchery.sessions.git.create_worktree") as mock_create:
            note = sessions.restore_worktree_if_needed(meta, confirm_recreate=self._confirm(True))
        assert note == ""
        assert not mock_create.called

    def test_noop_when_no_worktree_flag_set(self, fake_tasks_db, tmp_path):
        meta = self._meta(tmp_path, no_worktree=True)
        with patch("seekr_hatchery.sessions.git.create_worktree") as mock_create:
            note = sessions.restore_worktree_if_needed(meta, confirm_recreate=self._confirm(True))
        assert note == ""
        assert not mock_create.called

    def test_archived_auto_recreates_without_confirm(self, fake_tasks_db, tmp_path):
        meta = self._meta(tmp_path, status="archived")
        confirm = MagicMock(return_value=False)
        with (
            patch("seekr_hatchery.sessions.git.branch_exists", return_value=True),
            patch("seekr_hatchery.sessions.git.create_worktree") as mock_create,
            patch("seekr_hatchery.sessions.git.create_include_worktrees"),
        ):
            note = sessions.restore_worktree_if_needed(meta, confirm_recreate=confirm)
        assert note == ""
        assert not confirm.called
        assert mock_create.called
        assert sessions.load(meta.repo_path, meta.name).status == "in-progress"

    def test_in_progress_confirm_true_recreates(self, fake_tasks_db, tmp_path):
        meta = self._meta(tmp_path, status="in-progress")
        with (
            patch("seekr_hatchery.sessions.git.branch_exists", return_value=True),
            patch("seekr_hatchery.sessions.git.create_worktree") as mock_create,
            patch("seekr_hatchery.sessions.git.create_include_worktrees"),
        ):
            note = sessions.restore_worktree_if_needed(meta, confirm_recreate=self._confirm(True))
        assert note == ""
        assert mock_create.called

    def test_in_progress_confirm_false_raises_cancelled(self, fake_tasks_db, tmp_path):
        meta = self._meta(tmp_path, status="in-progress")
        with (
            patch("seekr_hatchery.sessions.git.create_worktree") as mock_create,
            pytest.raises(sessions.SessionCancelled),
        ):
            sessions.restore_worktree_if_needed(meta, confirm_recreate=self._confirm(False))
        assert not mock_create.called

    def test_missing_branch_recreates_from_default(self, fake_tasks_db, tmp_path):
        meta = self._meta(tmp_path, status="archived")
        with (
            patch("seekr_hatchery.sessions.git.branch_exists", return_value=False),
            patch("seekr_hatchery.sessions.git.fetch_remote", return_value=True),
            patch("seekr_hatchery.sessions.git.remote_branch_exists", return_value=False),
            patch("seekr_hatchery.sessions.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.sessions.git.create_worktree") as mock_create,
            patch("seekr_hatchery.sessions.git.create_include_worktrees"),
        ):
            note = sessions.restore_worktree_if_needed(meta, confirm_recreate=self._confirm(True))
        args, kwargs = mock_create.call_args
        base = kwargs.get("base") if "base" in kwargs else args[3]
        assert base == "main"
        assert meta.branch in note

    def test_fetch_failure_warns_unconfirmed_instead_of_missing(self, fake_tasks_db, tmp_path):
        """When fetch_remote fails, we can't confirm the branch is actually
        absent from origin — the warning/note must say so instead of
        claiming confirmed absence (which could otherwise mask a silent
        recreate-from-wrong-base that loses prior work)."""
        meta = self._meta(tmp_path, status="archived")
        with (
            patch("seekr_hatchery.sessions.git.branch_exists", return_value=False),
            patch("seekr_hatchery.sessions.git.fetch_remote", return_value=False) as mock_fetch,
            patch("seekr_hatchery.sessions.git.remote_branch_exists") as mock_remote_exists,
            patch("seekr_hatchery.sessions.git.get_default_branch", return_value="main"),
            patch("seekr_hatchery.sessions.git.create_worktree") as mock_create,
            patch("seekr_hatchery.sessions.git.create_include_worktrees"),
            patch("seekr_hatchery.sessions.ui.warn") as mock_warn,
        ):
            note = sessions.restore_worktree_if_needed(meta, confirm_recreate=self._confirm(True))
        assert mock_fetch.called
        # remote_branch_exists must not be trusted once fetch failed.
        assert not mock_remote_exists.called
        args, kwargs = mock_create.call_args
        base = kwargs.get("base") if "base" in kwargs else args[3]
        assert base == "main"
        warn_text = mock_warn.call_args[0][0]
        assert "fetch failed" in warn_text
        assert "couldn't verify" in warn_text
        assert "fetch failed" in note
        assert "was missing (locally and on origin)" not in note  # that's the confirmed-missing wording


class TestResolveRecreateBase:
    """sessions._resolve_recreate_base — base-ref resolution tiers."""

    def test_local_branch_exists_returns_branch(self, tmp_path):
        with patch("seekr_hatchery.sessions.git.branch_exists", return_value=True):
            base, missing, remote_check_failed = sessions._resolve_recreate_base(tmp_path, "hatchery/t")
        assert (base, missing, remote_check_failed) == ("hatchery/t", False, False)

    def test_missing_locally_found_on_origin(self, tmp_path):
        with (
            patch("seekr_hatchery.sessions.git.branch_exists", return_value=False),
            patch("seekr_hatchery.sessions.git.fetch_remote", return_value=True),
            patch("seekr_hatchery.sessions.git.remote_branch_exists", return_value=True),
        ):
            base, missing, remote_check_failed = sessions._resolve_recreate_base(tmp_path, "hatchery/t")
        assert (base, missing, remote_check_failed) == ("origin/hatchery/t", False, False)

    def test_missing_everywhere_falls_back_to_local_default(self, tmp_path):
        with (
            patch("seekr_hatchery.sessions.git.branch_exists", side_effect=[False, True]),
            patch("seekr_hatchery.sessions.git.fetch_remote", return_value=True),
            patch("seekr_hatchery.sessions.git.remote_branch_exists", return_value=False),
            patch("seekr_hatchery.sessions.git.get_default_branch", return_value="main"),
        ):
            base, missing, remote_check_failed = sessions._resolve_recreate_base(tmp_path, "hatchery/t")
        assert (base, missing, remote_check_failed) == ("main", True, False)

    def test_missing_everywhere_falls_back_to_origin_default(self, tmp_path):
        with (
            patch("seekr_hatchery.sessions.git.branch_exists", side_effect=[False, False]),
            patch("seekr_hatchery.sessions.git.fetch_remote", return_value=True),
            patch("seekr_hatchery.sessions.git.remote_branch_exists", side_effect=[False, True]),
            patch("seekr_hatchery.sessions.git.get_default_branch", return_value="main"),
        ):
            base, missing, remote_check_failed = sessions._resolve_recreate_base(tmp_path, "hatchery/t")
        assert (base, missing, remote_check_failed) == ("origin/main", True, False)

    def test_fetch_failure_skips_remote_checks_and_flags_unconfirmed(self, tmp_path):
        with (
            patch("seekr_hatchery.sessions.git.branch_exists", side_effect=[False, False]),
            patch("seekr_hatchery.sessions.git.fetch_remote", return_value=False),
            patch("seekr_hatchery.sessions.git.remote_branch_exists") as mock_remote_exists,
            patch("seekr_hatchery.sessions.git.get_default_branch", return_value="main"),
        ):
            base, missing, remote_check_failed = sessions._resolve_recreate_base(tmp_path, "hatchery/t")
        assert not mock_remote_exists.called
        assert (base, missing, remote_check_failed) == ("main", True, True)


class TestUpdateTaskFileStatus:
    """update_task_file_status rewrites the front-matter Status line."""

    def _write_task(self, tasks_dir, name, status):
        tasks_dir.mkdir(parents=True, exist_ok=True)
        p = tasks_dir / f"2026-01-01-{name}.md"
        p.write_text(f"# Task: {name}\n\n**Status**: {status}\n**Branch**: x\n\nBody\n")
        return p

    def test_replaces_complete_with_in_progress(self, tmp_path):
        tasks_dir = tmp_path / ".hatchery" / "tasks"
        p = self._write_task(tasks_dir, "t", "complete")
        sessions.update_task_file_status(tasks_dir, "t", "in-progress")
        assert "**Status**: in-progress" in p.read_text()
        assert "**Status**: complete" not in p.read_text()

    def test_noop_when_already_matches(self, tmp_path):
        tasks_dir = tmp_path / ".hatchery" / "tasks"
        p = self._write_task(tasks_dir, "t", "in-progress")
        before = p.read_text()
        sessions.update_task_file_status(tasks_dir, "t", "in-progress")
        assert p.read_text() == before

    def test_noop_when_file_missing(self, tmp_path):
        # Doesn't raise.
        sessions.update_task_file_status(tmp_path / ".hatchery" / "tasks", "no-such", "in-progress")

    def test_preserves_other_lines(self, tmp_path):
        tasks_dir = tmp_path / ".hatchery" / "tasks"
        p = self._write_task(tasks_dir, "t", "complete")
        sessions.update_task_file_status(tasks_dir, "t", "in-progress")
        text = p.read_text()
        assert "# Task: t" in text
        assert "**Branch**: x" in text
        assert "Body" in text


class TestResolveResumeKind:
    @staticmethod
    def _meta(tmp_path, session_id):
        meta = sessions.SessionMeta(
            name="t",
            repo=str(tmp_path),
            worktree=str(tmp_path / "wt"),
            branch="hatchery/t",
            type="task",
            session_id=session_id,
        )
        sessions.save(meta)
        return meta

    def test_session_id_present_returns_resume(self, tmp_path, fake_tasks_db):
        meta = self._meta(tmp_path, session_id="sid-xyz")
        assert sessions.resolve_resume_kind(meta) == ("resume", "sid-xyz")

    def test_session_id_empty_generates_and_persists(self, tmp_path, fake_tasks_db):
        meta = self._meta(tmp_path, session_id="")
        kind, sid = sessions.resolve_resume_kind(meta)
        assert kind == "new"
        assert sid  # non-empty uuid
        assert meta.session_id == sid  # mutated in-memory
        # And persisted to disk so subsequent resumes are idempotent.
        reloaded = sessions.load(meta.repo_path, meta.name)
        assert reloaded.session_id == sid


class TestSessionLaunch:
    """sessions.launch — hook order, status transitions, prompt construction.

    All paths exercised; the subprocess-level ``subprocess.run`` is patched so
    no real agent binary or docker container is invoked.
    """

    @staticmethod
    def _meta(tmp_path, *, is_chat=False, no_worktree=False, status="in-progress"):
        wt = tmp_path if no_worktree else tmp_path / "wt"
        if not no_worktree:
            wt.mkdir(exist_ok=True)
        meta = sessions.SessionMeta(
            name="t",
            repo=str(tmp_path),
            worktree=str(wt),
            branch="" if no_worktree else "hatchery/t",
            no_worktree=no_worktree,
            type="chat" if is_chat else "task",
            session_id="sid",
            status=status,
        )
        sessions.save(meta)
        return meta

    @staticmethod
    def _drive(meta, *, kind, backend, prompt_note=""):
        with patch("seekr_hatchery.sessions.subprocess.run"):
            return sessions.launch(
                meta,
                kind=kind,
                backend=backend,
                runtime=None,
                main_branch="main",
                session_id="sid",
                prompt_note=prompt_note,
            )

    def test_new_fires_hooks_in_order(self, spy_backend, fake_tasks_db, tmp_path, no_input):
        meta = self._meta(tmp_path)
        (meta.worktree_path / ".hatchery" / "tasks").mkdir(parents=True, exist_ok=True)
        (meta.worktree_path / ".hatchery" / "tasks" / sessions.task_file_name(meta.name)).write_text("body\n")
        self._drive(meta, kind="new", backend=spy_backend)
        names = [c[0] for c in spy_backend.calls]
        assert names == ["on_new_task", "on_before_launch", "build_new_command", "background_threads"]

    def test_resume_skips_on_new_task(self, spy_backend, fake_tasks_db, tmp_path, no_input):
        meta = self._meta(tmp_path)
        (meta.worktree_path / ".hatchery" / "tasks").mkdir(parents=True, exist_ok=True)
        (meta.worktree_path / ".hatchery" / "tasks" / sessions.task_file_name(meta.name)).write_text("body\n")
        self._drive(meta, kind="resume", backend=spy_backend)
        names = [c[0] for c in spy_backend.calls]
        assert names == ["on_before_launch", "build_resume_command", "background_threads"]

    def test_finalize_skips_on_before_launch(self, spy_backend, fake_tasks_db, tmp_path, no_input):
        meta = self._meta(tmp_path)
        self._drive(meta, kind="finalize", backend=spy_backend)
        names = [c[0] for c in spy_backend.calls]
        assert names == ["build_finalize_command", "background_threads"]
        _, _sid, _sys, wrap_up, _docker, _wd = spy_backend.calls[0]
        assert wrap_up == sessions._WRAP_UP_PROMPT

    def test_finalize_prepends_prompt_note(self, spy_backend, fake_tasks_db, tmp_path, no_input):
        """When the resume produced a prompt_note (e.g. branch recreated), the
        wrap-up agent should also see it — not just the original launch."""
        meta = self._meta(tmp_path)
        self._drive(meta, kind="finalize", backend=spy_backend, prompt_note="NOTE-X")
        _, _sid, _sys, wrap_up, _docker, _wd = spy_backend.calls[0]
        assert wrap_up.startswith("NOTE-X")
        assert sessions._WRAP_UP_PROMPT in wrap_up

    def test_resume_succeeds_when_task_file_missing(self, spy_backend, fake_tasks_db, tmp_path, no_input):
        """Regression: resume must not crash if the task file is absent from
        the worktree (e.g. agent switched branches or deleted it). The fallback
        prompt should mention the task name so the agent knows what's missing.
        """
        meta = self._meta(tmp_path)
        # Intentionally do NOT create .hatchery/tasks/ — file is missing.
        self._drive(meta, kind="resume", backend=spy_backend)
        build = next(c for c in spy_backend.calls if c[0] == "build_resume_command")
        _, _sid, _system, initial, _docker, _wd = build
        assert meta.name in initial
        assert "not present" in initial or "missing" in initial

    def test_resume_prompt_note_prepended(self, spy_backend, fake_tasks_db, tmp_path, no_input):
        """prompt_note threads through launch() and ends up at the start of
        the agent's initial prompt — used to surface branch-recreated etc."""
        meta = self._meta(tmp_path)
        (meta.worktree_path / ".hatchery" / "tasks").mkdir(parents=True, exist_ok=True)
        (meta.worktree_path / ".hatchery" / "tasks" / sessions.task_file_name(meta.name)).write_text("body\n")
        self._drive(meta, kind="resume", backend=spy_backend, prompt_note="HELLO-NOTE")
        build = next(c for c in spy_backend.calls if c[0] == "build_resume_command")
        _, _sid, _system, initial, _docker, _wd = build
        assert initial.startswith("HELLO-NOTE")

    def test_chat_uses_empty_prompts(self, spy_backend, fake_tasks_db, tmp_path, no_input):
        meta = self._meta(tmp_path, is_chat=True, no_worktree=True)
        self._drive(meta, kind="new", backend=spy_backend)
        build = next(c for c in spy_backend.calls if c[0] == "build_new_command")
        _, _sid, system, initial, _d, _wd = build
        assert system == ""
        assert initial == ""

    def test_status_flips_running_then_in_progress(self, spy_backend, fake_tasks_db, tmp_path, no_input):
        meta = self._meta(tmp_path)
        (meta.worktree_path / ".hatchery" / "tasks").mkdir(parents=True, exist_ok=True)
        (meta.worktree_path / ".hatchery" / "tasks" / sessions.task_file_name(meta.name)).write_text("body\n")
        statuses: list[str] = []
        original = sessions.set_status

        def spy(repo, name, status):
            statuses.append(status)
            return original(repo, name, status)

        with (
            patch("seekr_hatchery.sessions.set_status", side_effect=spy),
            patch("seekr_hatchery.sessions.subprocess.run"),
        ):
            sessions.launch(meta, kind="new", backend=spy_backend, runtime=None, main_branch="main", session_id="sid")
        assert statuses == ["running", "in-progress"]
        assert sessions.load(Path(meta.repo), meta.name).status == "in-progress"


class TestSessionLaunchBackgroundThreads:
    """Thread lifecycle: launch() starts backend.background_threads() workers,
    signals stop when the agent exits, and joins them."""

    @staticmethod
    def _meta(tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir(exist_ok=True)
        m = sessions.SessionMeta(
            name="t",
            repo=str(tmp_path),
            worktree=str(wt),
            branch="hatchery/t",
            type="task",
            session_id="sid",
            status="in-progress",
        )
        sessions.save(m)
        return m

    def test_each_worker_runs_in_its_own_thread(self, spy_backend, fake_tasks_db, tmp_path, no_input):
        import threading as _threading

        seen: list[str] = []
        started = _threading.Event()

        def w1() -> None:
            seen.append(_threading.current_thread().name)
            started.set()

        def w2() -> None:
            seen.append(_threading.current_thread().name)

        spy_backend.background_threads = lambda meta, **kw: [w1, w2]

        meta = self._meta(tmp_path)
        (meta.worktree_path / ".hatchery" / "tasks").mkdir(parents=True, exist_ok=True)
        (meta.worktree_path / ".hatchery" / "tasks" / sessions.task_file_name(meta.name)).write_text("body\n")
        with patch("seekr_hatchery.sessions.subprocess.run"):
            sessions.launch(meta, kind="new", backend=spy_backend, runtime=None, main_branch="main", session_id="sid")
        # Both workers must have executed
        assert len(seen) == 2

    def test_stop_event_is_set_after_agent_exit(self, spy_backend, fake_tasks_db, tmp_path, no_input):
        import threading as _threading

        stop_captured: dict[str, _threading.Event] = {}

        # Worker records the stop event so we can inspect it after launch()
        # returns; the event must be set by then.
        def hook(meta, *, docker, runtime, launch_start, stop):
            stop_captured["stop"] = stop

            def _worker() -> None:
                stop.wait(1)

            return [_worker]

        spy_backend.background_threads = hook

        meta = self._meta(tmp_path)
        (meta.worktree_path / ".hatchery" / "tasks").mkdir(parents=True, exist_ok=True)
        (meta.worktree_path / ".hatchery" / "tasks" / sessions.task_file_name(meta.name)).write_text("body\n")
        with patch("seekr_hatchery.sessions.subprocess.run"):
            sessions.launch(meta, kind="new", backend=spy_backend, runtime=None, main_branch="main", session_id="sid")
        assert stop_captured["stop"].is_set()

    def test_worker_exception_does_not_mask_agent_exit(self, spy_backend, fake_tasks_db, tmp_path, no_input):
        # A crashing worker must be logged and swallowed. The launch itself
        # completes normally.
        def crashing_worker() -> None:
            raise RuntimeError("worker exploded")

        spy_backend.background_threads = lambda meta, **kw: [crashing_worker]

        meta = self._meta(tmp_path)
        (meta.worktree_path / ".hatchery" / "tasks").mkdir(parents=True, exist_ok=True)
        (meta.worktree_path / ".hatchery" / "tasks" / sessions.task_file_name(meta.name)).write_text("body\n")
        with patch("seekr_hatchery.sessions.subprocess.run"):
            # Must not raise
            sessions.launch(meta, kind="new", backend=spy_backend, runtime=None, main_branch="main", session_id="sid")
        # Status still flipped back to in-progress cleanly.
        assert sessions.load(Path(meta.repo), meta.name).status == "in-progress"

    def test_stop_and_join_run_even_when_agent_raises(self, spy_backend, fake_tasks_db, tmp_path, no_input):
        import threading as _threading

        stop_captured: dict[str, _threading.Event] = {}

        def hook(meta, *, docker, runtime, launch_start, stop):
            stop_captured["stop"] = stop

            def _worker() -> None:
                stop.wait(1)

            return [_worker]

        spy_backend.background_threads = hook

        meta = self._meta(tmp_path)
        (meta.worktree_path / ".hatchery" / "tasks").mkdir(parents=True, exist_ok=True)
        (meta.worktree_path / ".hatchery" / "tasks" / sessions.task_file_name(meta.name)).write_text("body\n")
        with patch("seekr_hatchery.sessions.subprocess.run", side_effect=RuntimeError("agent boom")):
            with pytest.raises(RuntimeError, match="agent boom"):
                sessions.launch(
                    meta, kind="new", backend=spy_backend, runtime=None, main_branch="main", session_id="sid"
                )
        # Thread lifecycle still happened
        assert stop_captured["stop"].is_set()


class TestSessionMarkDone:
    def test_removes_worktree_and_sets_complete(self, git_repo, fake_tasks_db, no_input):
        meta = sessions.create(name="t", repo=git_repo, type="task", backend=agent.CODEX, objective="x")
        worktree = meta.worktree_path

        sessions.mark_done(meta, commit_changes=False)
        assert not worktree.exists()
        assert _git(git_repo, "rev-parse", "--verify", "hatchery/t", check=False).returncode == 0
        loaded = sessions.load(git_repo, "t")
        assert loaded.status == "complete"
        assert loaded.completed

    def test_commit_changes_creates_final_checkpoint(self, git_repo, fake_tasks_db, no_input):
        meta = sessions.create(name="t", repo=git_repo, type="task", backend=agent.CODEX, objective="x")
        (meta.worktree_path / "new.txt").write_text("work in progress\n")

        sessions.mark_done(meta, commit_changes=True)
        log = _git(git_repo, "log", "hatchery/t", "--oneline").stdout
        assert "final checkpoint" in log

    def test_no_worktree_chat_just_updates_status(self, git_repo, fake_tasks_db, no_input):
        meta = sessions.create(name="chat-1", repo=git_repo, type="chat", backend=agent.CODEX)
        sessions.mark_done(meta)
        loaded = sessions.load(git_repo, "chat-1")
        assert loaded.status == "complete"


class TestSessionArchive:
    def test_archive_keeps_branch_and_sets_archived(self, git_repo, fake_tasks_db, no_input):
        meta = sessions.create(name="t", repo=git_repo, type="task", backend=agent.CODEX, objective="x")
        worktree = meta.worktree_path
        sessions.archive(meta)
        assert not worktree.exists()
        assert _git(git_repo, "rev-parse", "--verify", "hatchery/t", check=False).returncode == 0
        assert sessions.load(git_repo, "t").status == "archived"

    def test_archive_chat_is_a_noop_on_worktree(self, git_repo, fake_tasks_db, no_input):
        meta = sessions.create(name="chat-1", repo=git_repo, type="chat", backend=agent.CODEX)
        sessions.archive(meta)
        assert sessions.load(git_repo, "chat-1").status == "archived"


class TestSessionDelete:
    def test_removes_worktree_branch_and_meta(self, git_repo, fake_tasks_db, no_input):
        meta = sessions.create(name="t", repo=git_repo, type="task", backend=agent.CODEX, objective="x")
        worktree = meta.worktree_path
        assert worktree.exists()
        assert sessions.task_db_path(git_repo, "t").exists()

        sessions.delete(meta)
        assert not worktree.exists()
        assert _git(git_repo, "rev-parse", "--verify", "hatchery/t", check=False).returncode != 0
        assert not sessions.task_db_path(git_repo, "t").exists()

    def test_delete_chat_removes_only_meta(self, git_repo, fake_tasks_db, no_input):
        meta = sessions.create(name="chat-1", repo=git_repo, type="chat", backend=agent.CODEX)
        sessions.delete(meta)
        assert not sessions.task_db_path(git_repo, "chat-1").exists()


# ---------------------------------------------------------------------------
# SessionCancelled rollback (sessions.create with use_editor=True)
# ---------------------------------------------------------------------------


class TestSessionCancelledRollback:
    """When the editor returns the task file unchanged, sessions.create must
    raise SessionCancelled AND undo every side effect it made before reaching
    the editor (worktree, branch, include worktrees, include branches)."""

    def test_unchanged_editor_rolls_back_worktree_and_branch(self, git_repo, fake_tasks_db, no_input, monkeypatch):
        # No-op editor: leaves the task file exactly as written.
        monkeypatch.setattr("seekr_hatchery.sessions.open_for_editing", lambda _p: None)

        with pytest.raises(sessions.SessionCancelled):
            sessions.create(
                name="t",
                repo=git_repo,
                type="task",
                backend=agent.CODEX,
                use_editor=True,
            )

        # Worktree directory is gone.
        assert not (git_repo / ".hatchery" / "worktrees" / "t").exists()
        # Branch is gone.
        assert _git(git_repo, "rev-parse", "--verify", "hatchery/t", check=False).returncode != 0
        # No meta.json on disk (sessions.save_task never reached).
        assert not sessions.task_db_path(git_repo, "t").exists()

    def test_unchanged_editor_rolls_back_include_worktrees(
        self, git_repo, fake_tasks_db, no_input, tmp_path, monkeypatch
    ):
        repo_b = _make_include_repo(tmp_path, "repo-b")

        monkeypatch.setattr("seekr_hatchery.sessions.open_for_editing", lambda _p: None)

        with pytest.raises(sessions.SessionCancelled):
            sessions.create(
                name="t",
                repo=git_repo,
                type="task",
                backend=agent.CODEX,
                use_editor=True,
                include_entries=[IncludeEntry(path=repo_b, mode="worktree")],
            )

        # The include worktree under repo_b/.hatchery/worktrees/t is cleaned up.
        assert not (repo_b / ".hatchery" / "worktrees" / "t").exists()
        # The include branch (hatchery/t in repo_b) is gone.
        assert _git(repo_b, "rev-parse", "--verify", "hatchery/t", check=False).returncode != 0


# ---------------------------------------------------------------------------
# sessions.launch — docker-runtime branch (the no-runtime branch is covered
# above in TestSessionLaunch).
# ---------------------------------------------------------------------------


class TestSessionLaunchDockerBranch:
    def test_runtime_delegates_to_docker_run_session(self, spy_backend, fake_tasks_db, tmp_path, monkeypatch):
        """When runtime is set, sessions.launch routes through docker.run_session
        with the right meta + tokens (the native subprocess.run path is bypassed).
        """
        wt = tmp_path / "wt"
        wt.mkdir()
        meta = sessions.SessionMeta(
            name="t",
            repo=str(tmp_path),
            worktree=str(wt),
            branch="hatchery/t",
            session_id="sid",
            status="in-progress",
        )
        sessions.save(meta)
        (wt / ".hatchery" / "tasks").mkdir(parents=True, exist_ok=True)
        (wt / ".hatchery" / "tasks" / sessions.task_file_name("t")).write_text("body\n")

        # Stub the docker primitives that sessions.launch would otherwise hit.
        from unittest.mock import MagicMock

        fake_config = MagicMock()
        fake_config.kubernetes = None  # short-circuit the kubectl-token write
        runtime_sentinel = MagicMock(name="Runtime")

        with (
            patch(
                "seekr_hatchery.sessions.docker.launch_context",
                return_value=(fake_config, ["docker"], "/repo/.hatchery/worktrees/t"),
            ),
            patch("seekr_hatchery.sessions.docker.run_session") as mock_run_session,
            patch("seekr_hatchery.sessions.get_or_create_proxy_token", return_value="tok"),
        ):
            sessions.launch(
                meta,
                kind="new",
                backend=spy_backend,
                runtime=runtime_sentinel,
                main_branch="main",
                session_id="sid",
            )

        # docker.run_session was called with meta + the proxy token.
        mock_run_session.assert_called_once()
        kwargs = mock_run_session.call_args[1]
        assert kwargs["proxy_token"] == "tok"
        assert kwargs["kubectl_proxy_token"] is None
        # Status flip still happens around the docker call.
        assert sessions.load(meta.repo_path, meta.name).status == "in-progress"


# ---------------------------------------------------------------------------
# merge_include_updates (resume-time --include flag handling)
# ---------------------------------------------------------------------------


def _make_include_repo(parent: Path, name: str) -> Path:
    """Create a real, committed git repo at *parent/name* for use as an include."""
    repo = parent / name
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "T")
    (repo / "README").write_text(f"{name}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


class TestMergeIncludeUpdates:
    """sessions.merge_include_updates — mode transitions, additions, ordering."""

    def _setup(self, git_repo, tmp_path, *, mode="worktree"):
        repo_b = _make_include_repo(tmp_path, "repo-b")
        entry = IncludeEntry(path=repo_b, mode=mode)
        meta = sessions.create(
            name="t",
            repo=git_repo,
            type="task",
            backend=agent.CODEX,
            objective="x",
            include_entries=[entry],
        )
        return meta, repo_b, entry

    def test_empty_updates_returns_current(self, git_repo, fake_tasks_db, no_input, tmp_path):
        meta, _repo_b, entry = self._setup(git_repo, tmp_path)
        result = sessions.merge_include_updates([entry], [], meta)
        assert result == [entry]

    def test_worktree_to_ro_removes_worktree(self, git_repo, fake_tasks_db, no_input, tmp_path):
        meta, repo_b, _entry = self._setup(git_repo, tmp_path, mode="worktree")
        include_wt = repo_b / ".hatchery" / "worktrees" / "t"
        assert include_wt.exists()

        new_entry = IncludeEntry(path=repo_b, mode="ro")
        result = sessions.merge_include_updates(meta.include_entries, [new_entry], meta)

        assert any(e.path == repo_b and e.mode == "ro" for e in result)
        assert not include_wt.exists()

    def test_ro_to_worktree_creates_worktree(self, git_repo, fake_tasks_db, no_input, tmp_path):
        meta, repo_b, _entry = self._setup(git_repo, tmp_path, mode="ro")
        include_wt = repo_b / ".hatchery" / "worktrees" / "t"
        assert not include_wt.exists()  # ro doesn't create a worktree

        new_entry = IncludeEntry(path=repo_b, mode="worktree")
        sessions.merge_include_updates(meta.include_entries, [new_entry], meta)

        assert include_wt.exists()

    def test_new_entry_appended_at_end(self, git_repo, fake_tasks_db, no_input, tmp_path):
        meta, _repo_b, original = self._setup(git_repo, tmp_path)
        extra = _make_include_repo(tmp_path, "extra")

        new_entry = IncludeEntry(path=extra, mode="ro")
        result = sessions.merge_include_updates(meta.include_entries, [new_entry], meta)

        # Original first, new entry second
        assert [e.path for e in result] == [original.path, extra]


# ---------------------------------------------------------------------------
# merge_includes_with_config (CLI-flag + docker.yaml de-dupe)
# ---------------------------------------------------------------------------


class TestMergeIncludesWithConfig:
    """sessions.merge_includes_with_config — CLI entries win on conflict; missing
    paths are skipped with a warning; new config entries get appended."""

    def test_cli_priority_on_mode_conflict(self, tmp_path):
        """When both CLI and docker.yaml list the same path with different modes,
        the CLI mode wins."""
        ref = tmp_path / "ref"
        ref.mkdir()
        cli_entries = [IncludeEntry(path=ref.resolve(), mode="ro")]
        # docker.yaml says worktree; CLI says ro — CLI wins.
        config = [IncludeItem(path=str(ref), mode="worktree")]

        result = sessions.merge_includes_with_config(cli_entries, config, tmp_path)

        assert len(result) == 1
        assert result[0].mode == "ro"

    def test_config_entry_appended_when_not_in_cli(self, tmp_path):
        ref_a = tmp_path / "a"
        ref_a.mkdir()
        ref_b = tmp_path / "b"
        ref_b.mkdir()
        cli_entries = [IncludeEntry(path=ref_a.resolve(), mode="ro")]
        config = [IncludeItem(path=str(ref_b), mode="worktree")]

        result = sessions.merge_includes_with_config(cli_entries, config, tmp_path)

        paths = [e.path for e in result]
        assert ref_a.resolve() in paths
        assert ref_b.resolve() in paths

    def test_missing_config_path_skipped(self, tmp_path):
        """A docker.yaml entry pointing at a non-existent path is dropped (with warning)."""
        config = [IncludeItem(path=str(tmp_path / "does-not-exist"), mode="ro")]
        result = sessions.merge_includes_with_config([], config, tmp_path)
        assert result == []

    def test_relative_path_resolved_against_repo(self, tmp_path):
        ref = tmp_path / "ref"
        ref.mkdir()
        config = [IncludeItem(path="ref", mode="ro")]  # relative to repo
        result = sessions.merge_includes_with_config([], config, tmp_path)
        assert len(result) == 1
        assert result[0].path == ref.resolve()


# ---------------------------------------------------------------------------
# Out-of-tree store path helpers
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# hatchery_dir resolver
# ---------------------------------------------------------------------------


class TestHatcheryDir:
    def test_no_commit_uses_store(self, tmp_path, monkeypatch):
        monkeypatch.setattr(constants, 'HATCHERY_DIR', tmp_path)
        monkeypatch.setattr(sessions, '_TASKS_DB_DIR', tmp_path / 'tasks')
        repo = tmp_path / 'myrepo'
        worktree = repo / '.hatchery' / 'worktrees' / 't'
        result = sessions.hatchery_dir(repo, worktree, no_commit=True, no_worktree=False)
        assert result == sessions.repo_store_dir(repo)

    def test_commit_worktree(self, tmp_path):
        repo = tmp_path / 'myrepo'
        worktree = repo / '.hatchery' / 'worktrees' / 't'
        result = sessions.hatchery_dir(repo, worktree, no_commit=False, no_worktree=False)
        assert result == worktree / '.hatchery'

    def test_commit_no_worktree(self, tmp_path):
        repo = tmp_path / 'myrepo'
        result = sessions.hatchery_dir(repo, repo, no_commit=False, no_worktree=True)
        assert result == repo / '.hatchery'


# ---------------------------------------------------------------------------
# repo_store_dir / ensure_repo_store
# ---------------------------------------------------------------------------


class TestRepoStorePaths:
    def test_repo_store_dir_composes_correctly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(constants, 'HATCHERY_DIR', tmp_path)
        monkeypatch.setattr(sessions, '_TASKS_DB_DIR', tmp_path / 'tasks')
        repo = tmp_path / 'myrepo'
        result = sessions.repo_store_dir(repo)
        assert result == tmp_path / 'repos' / utils.repo_id(repo)


class TestEnsureRepoStore:
    def test_creates_dirs_and_repo_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(constants, 'HATCHERY_DIR', tmp_path)
        monkeypatch.setattr(sessions, '_TASKS_DB_DIR', tmp_path / 'tasks')
        repo = tmp_path / 'myrepo'
        sessions.ensure_repo_store(repo)
        store = sessions.repo_store_dir(repo)
        assert (store / 'tasks').is_dir()
        repo_meta = json.loads((store / 'repo.json').read_text())
        assert repo_meta == {'path': str(repo), 'name': repo.name}

    def test_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(constants, 'HATCHERY_DIR', tmp_path)
        monkeypatch.setattr(sessions, '_TASKS_DB_DIR', tmp_path / 'tasks')
        repo = tmp_path / 'myrepo'
        sessions.ensure_repo_store(repo)
        sessions.ensure_repo_store(repo)
        store = sessions.repo_store_dir(repo)
        assert (store / 'repo.json').exists()


# ---------------------------------------------------------------------------
# SessionMeta derived properties (hatchery_dir, task_dir)
# ---------------------------------------------------------------------------


class TestSessionMetaDerivedPaths:
    def test_hatchery_dir_no_commit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(constants, 'HATCHERY_DIR', tmp_path)
        meta = sessions.SessionMeta(
            name='t', repo=str(tmp_path), worktree=str(tmp_path / 'wt'),
            no_commit=True,
        )
        assert meta.hatchery_dir == sessions.repo_store_dir(tmp_path)

    def test_hatchery_dir_committed_no_worktree(self, tmp_path):
        meta = sessions.SessionMeta(
            name='t', repo=str(tmp_path), worktree=str(tmp_path),
            no_commit=False, no_worktree=True,
        )
        assert meta.hatchery_dir == tmp_path / '.hatchery'

    def test_hatchery_dir_committed_worktree(self, tmp_path):
        wt = tmp_path / 'wt'
        meta = sessions.SessionMeta(
            name='t', repo=str(tmp_path), worktree=str(wt),
            no_commit=False,
        )
        assert meta.hatchery_dir == wt / '.hatchery'

    def test_task_dir_no_commit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(constants, 'HATCHERY_DIR', tmp_path)
        meta = sessions.SessionMeta(
            name='t', repo=str(tmp_path), worktree=str(tmp_path / 'wt'),
            no_commit=True,
        )
        assert meta.task_dir == meta.hatchery_dir / 'tasks'

    def test_task_dir_committed(self, tmp_path):
        meta = sessions.SessionMeta(
            name='t', repo=str(tmp_path), worktree=str(tmp_path / 'wt'),
            no_commit=False,
        )
        assert meta.task_dir == tmp_path / 'wt' / '.hatchery' / 'tasks'


# ---------------------------------------------------------------------------
# No-commit create() — task file and docker files go to the store
# ---------------------------------------------------------------------------


class TestCreateNoCommit:
    def test_task_file_in_store_not_worktree(self, git_repo, fake_tasks_db, no_input, monkeypatch):
        """In no-commit mode, the task file lives in the store, not the worktree."""
        meta = sessions.create(
            name='t', repo=git_repo, type='task', backend=agent.CODEX,
            no_commit=True, objective='do stuff',
        )
        task_file = meta.hatchery_dir / 'tasks' / sessions.task_file_name('t')
        assert task_file.exists()
        wt_tasks = meta.worktree_path / '.hatchery' / 'tasks'
        assert not wt_tasks.exists() or not list(wt_tasks.glob('*.md'))

    def test_no_git_commit_called(self, git_repo, fake_tasks_db, no_input, monkeypatch):
        """In no-commit mode, git.add_and_commit is never called."""
        commit_calls = []
        original = git.add_and_commit

        def spy(repo, msg, **kw):
            commit_calls.append(msg)
            return original(repo, msg, **kw)

        monkeypatch.setattr(git, 'add_and_commit', spy)
        monkeypatch.setattr('seekr_hatchery.sessions.git.add_and_commit', spy)
        sessions.create(
            name='t', repo=git_repo, type='task', backend=agent.CODEX,
            no_commit=True, objective='x',
        )
        assert commit_calls == []

    def test_no_ensure_tasks_dir_called(self, git_repo, fake_tasks_db, no_input, monkeypatch):
        """In no-commit mode, ensure_tasks_dir is not called."""
        called = []
        original = sessions.ensure_tasks_dir

        def spy(repo):
            called.append(repo)
            return original(repo)

        monkeypatch.setattr(sessions, 'ensure_tasks_dir', spy)
        sessions.create(
            name='t', repo=git_repo, type='task', backend=agent.CODEX,
            no_commit=True, objective='x',
        )
        assert called == []

    def test_docker_files_in_store_not_worktree(self, git_repo, fake_tasks_db, no_input, monkeypatch):
        """In no-commit mode, docker files are in the store, not the worktree."""
        meta = sessions.create(
            name='t', repo=git_repo, type='task', backend=agent.CODEX,
            no_commit=True, objective='x',
        )
        hdir = meta.hatchery_dir
        assert (hdir / 'Dockerfile.codex').exists()
        assert (hdir / 'docker.yaml').exists()
        wt_df = meta.worktree_path / '.hatchery' / 'Dockerfile.codex'
        assert not wt_df.exists()

    def test_git_exclude_used(self, git_repo, fake_tasks_db, no_input, monkeypatch):
        """In no-commit mode, ensure_git_exclude is called, not ensure_gitignore."""
        exclude_called = []
        gitignore_called = []
        monkeypatch.setattr(sessions, 'ensure_git_exclude', lambda repo, entry: exclude_called.append((repo, entry)))
        monkeypatch.setattr(sessions, 'ensure_gitignore', lambda repo: gitignore_called.append(repo))
        sessions.create(
            name='t', repo=git_repo, type='task', backend=agent.CODEX,
            no_commit=True, objective='x',
        )
        assert len(exclude_called) == 1
        assert '.hatchery/worktrees/' in exclude_called[0][1]
        assert gitignore_called == []


class TestRecordSurvivesDone:
    def test_record_survives_mark_done(self, git_repo, fake_tasks_db, no_input, monkeypatch):
        """After create(no_commit=True) + mark_done, the record file still exists."""
        meta = sessions.create(
            name='t', repo=git_repo, type='task', backend=agent.CODEX,
            no_commit=True, objective='x',
        )
        task_file = meta.hatchery_dir / 'tasks' / sessions.task_file_name('t')
        assert task_file.exists()
        sessions.mark_done(meta, commit_changes=False)
        assert task_file.exists()


# ---------------------------------------------------------------------------
# sandbox_context no-commit branch
# ---------------------------------------------------------------------------


class TestSandboxContextNoCommit:
    def test_no_commit_emits_record_store_bullet(self, tmp_path):
        """sandbox_context with no_commit=True + hatchery_dir emits the record-store bullet."""
        result = sessions.sandbox_context(
            name='t', branch='hatchery/t', worktree=tmp_path / 'wt', repo=tmp_path,
            main_branch='main', use_docker=True,
            no_commit=True, hatchery_dir=tmp_path / 'store',
        )
        assert 'Record store' in result
        assert 'mounted read-write' in result
        assert str(tmp_path / 'store') in result

    def test_commit_mode_no_record_store_bullet(self, tmp_path):
        """sandbox_context with no_commit=False does not emit the record-store bullet."""
        result = sessions.sandbox_context(
            name='t', branch='hatchery/t', worktree=tmp_path / 'wt', repo=tmp_path,
            main_branch='main', use_docker=True,
            no_commit=False,
        )
        assert 'Record store' not in result

    def test_no_commit_no_hatchery_dir_no_bullet(self, tmp_path):
        """sandbox_context with no_commit=True but no hatchery_dir does not emit the bullet."""
        result = sessions.sandbox_context(
            name='t', branch='hatchery/t', worktree=tmp_path / 'wt', repo=tmp_path,
            main_branch='main', use_docker=True,
            no_commit=True,
        )
        assert 'Record store' not in result


# ---------------------------------------------------------------------------
# not-in-repo + not-committed path agreement
# ---------------------------------------------------------------------------


class TestCreateNoCommitNotInRepo:
    def test_docker_files_go_to_store_when_not_in_repo(self, tmp_path, fake_tasks_db, no_input, monkeypatch):
        """When not in a repo and not committed, docker files go to the store."""
        repo = tmp_path / 'repo'
        repo.mkdir()

        meta = sessions.create(
            name='t', repo=repo, type='task', backend=agent.CODEX,
            no_commit=True, no_worktree=True, in_repo=False, objective='x',
        )
        hdir = meta.hatchery_dir
        assert (hdir / 'Dockerfile.codex').exists()
        assert (hdir / 'docker.yaml').exists()
        assert not (repo / '.hatchery' / 'Dockerfile.codex').exists()
