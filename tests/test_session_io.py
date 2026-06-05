"""Tests for task I/O: save_task, load_task, repo_tasks_for_current_repo,
plus real-fs lifecycle tests for sessions.create / mark_done / archive /
delete / launch / merge_include_updates."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

import seekr_hatchery.agents as agent
import seekr_hatchery.constants as constants
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
        task_file = sessions.find_task_file(git_repo / ".hatchery" / "worktrees" / "t", "t")
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

    def test_no_commit_skips_task_file_commit(self, git_repo, fake_tasks_db, no_input):
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
        assert sessions.find_task_file(git_repo, "chat-1") is None
        assert _git(git_repo, "rev-parse", "--verify", "hatchery/chat-1", check=False).returncode != 0


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
    def _drive(meta, *, kind, backend):
        with patch("seekr_hatchery.sessions.subprocess.run"):
            return sessions.launch(meta, kind=kind, backend=backend, runtime=None, main_branch="main", session_id="sid")

    def test_new_fires_hooks_in_order(self, spy_backend, fake_tasks_db, tmp_path, no_input):
        meta = self._meta(tmp_path)
        (meta.worktree_path / ".hatchery" / "tasks").mkdir(parents=True, exist_ok=True)
        (meta.worktree_path / ".hatchery" / "tasks" / sessions.task_file_name(meta.name)).write_text("body\n")
        self._drive(meta, kind="new", backend=spy_backend)
        names = [c[0] for c in spy_backend.calls]
        assert names == ["on_new_task", "on_before_launch", "build_new_command"]

    def test_resume_skips_on_new_task(self, spy_backend, fake_tasks_db, tmp_path, no_input):
        meta = self._meta(tmp_path)
        (meta.worktree_path / ".hatchery" / "tasks").mkdir(parents=True, exist_ok=True)
        (meta.worktree_path / ".hatchery" / "tasks" / sessions.task_file_name(meta.name)).write_text("body\n")
        self._drive(meta, kind="resume", backend=spy_backend)
        names = [c[0] for c in spy_backend.calls]
        assert names == ["on_before_launch", "build_resume_command"]

    def test_finalize_skips_on_before_launch(self, spy_backend, fake_tasks_db, tmp_path, no_input):
        meta = self._meta(tmp_path)
        self._drive(meta, kind="finalize", backend=spy_backend)
        names = [c[0] for c in spy_backend.calls]
        assert names == ["build_finalize_command"]
        _, _sid, _sys, wrap_up, _docker, _wd = spy_backend.calls[0]
        assert wrap_up == sessions._WRAP_UP_PROMPT

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
