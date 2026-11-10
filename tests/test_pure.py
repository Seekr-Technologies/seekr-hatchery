"""Tests for pure functions — no mocking required."""

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import seekr_hatchery.sessions as sessions
import seekr_hatchery.utils as utils
from seekr_hatchery.models import SessionMeta

# ---------------------------------------------------------------------------
# find_task_file
# ---------------------------------------------------------------------------


class TestFindTaskFile:
    def test_finds_matching_file(self, tmp_path):
        task_file = tmp_path / ".hatchery" / "tasks" / "2026-01-15-my-task" / "task.md"
        task_file.parent.mkdir(parents=True)
        task_file.write_text("contents")
        assert sessions.find_task_file(tmp_path / ".hatchery" / "tasks", "my-task") == task_file

    def test_returns_none_when_no_match(self, tmp_path):
        (tmp_path / ".hatchery" / "tasks").mkdir(parents=True)
        assert sessions.find_task_file(tmp_path / ".hatchery" / "tasks", "nonexistent") is None

    def test_returns_latest_when_multiple(self, tmp_path):
        tasks_dir = tmp_path / ".hatchery" / "tasks"
        (tasks_dir / "2026-01-10-my-task").mkdir(parents=True)
        (tasks_dir / "2026-01-10-my-task" / "task.md").write_text("old")
        (tasks_dir / "2026-03-04-my-task").mkdir(parents=True)
        (tasks_dir / "2026-03-04-my-task" / "task.md").write_text("new")
        result = sessions.find_task_file(tasks_dir, "my-task")
        assert result == tasks_dir / "2026-03-04-my-task" / "task.md"

    def test_migrates_legacy_flat_file(self, tmp_path):
        tasks_dir = tmp_path / ".hatchery" / "tasks"
        tasks_dir.mkdir(parents=True)
        legacy_file = tasks_dir / "2026-01-15-my-task.md"
        legacy_file.write_text("contents")

        result = sessions.find_task_file(tasks_dir, "my-task")

        assert result == tasks_dir / "2026-01-15-my-task" / "task.md"
        assert result.read_text() == "contents"
        assert not legacy_file.exists()

    def test_migrates_legacy_per_task_subdir(self, tmp_path):
        """Middle layout: tasks_dir/<name>/<date>-<name>.md — pre-unification."""
        tasks_dir = tmp_path / ".hatchery" / "tasks"
        old_subdir = tasks_dir / "my-task"
        old_subdir.mkdir(parents=True)
        old_file = old_subdir / "2026-01-15-my-task.md"
        old_file.write_text("contents")

        result = sessions.find_task_file(tasks_dir, "my-task")

        assert result == tasks_dir / "2026-01-15-my-task" / "task.md"
        assert result.read_text() == "contents"
        assert not old_file.exists()


# ---------------------------------------------------------------------------
# to_name
# ---------------------------------------------------------------------------


class TestToName:
    def test_lowercases(self):
        assert utils.to_name("MyTask") == "mytask"

    def test_replaces_spaces_with_hyphens(self):
        assert utils.to_name("add auth") == "add-auth"

    def test_replaces_special_chars_with_hyphens(self):
        assert utils.to_name("fix: bug #42") == "fix-bug-42"

    def test_collapses_consecutive_separators(self):
        assert utils.to_name("a  b   c") == "a-b-c"

    def test_strips_leading_hyphens(self):
        assert utils.to_name("--task") == "task"

    def test_strips_trailing_hyphens(self):
        assert utils.to_name("task--") == "task"

    def test_strips_both_ends(self):
        assert utils.to_name("  task  ") == "task"

    def test_truncates_to_50_chars(self):
        long_name = "a" * 60
        result = utils.to_name(long_name)
        assert len(result) == 50

    def test_truncation_at_boundary(self):
        name = "a" * 50
        assert utils.to_name(name) == name

    def test_handles_empty_string(self):
        assert utils.to_name("") == ""

    def test_handles_all_special_chars(self):
        assert utils.to_name("!!!") == ""

    def test_alphanumeric_unchanged(self):
        assert utils.to_name("abc123") == "abc123"

    def test_hyphens_in_input_preserved(self):
        assert utils.to_name("my-task-name") == "my-task-name"

    def test_underscores_replaced(self):
        assert utils.to_name("my_task") == "my-task"

    def test_mixed_case_with_symbols(self):
        assert utils.to_name("Add-Authentication") == "add-authentication"


# ---------------------------------------------------------------------------
# task_dir_name
# ---------------------------------------------------------------------------


class TestTaskDirName:
    def test_format(self):
        with patch("seekr_hatchery.sessions.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 15, 10, 30)
            result = sessions.task_dir_name("my-task")
        assert result == "2026-01-15-my-task"

    def test_uses_current_date(self):
        # Without mocking — just check format shape
        result = sessions.task_dir_name("test")
        parts = result.split("-")
        assert len(parts) >= 4
        assert parts[0].isdigit() and len(parts[0]) == 4  # year


# ---------------------------------------------------------------------------
# session_prompt
# ---------------------------------------------------------------------------


class TestSessionPrompt:
    @staticmethod
    def _meta(worktree, name):
        return SessionMeta(name=name, repo=str(worktree), worktree=str(worktree))

    def test_contains_task_file_path(self, tmp_path):
        task_file = tmp_path / ".hatchery" / "tasks" / "2026-01-15-my-task" / "task.md"
        task_file.parent.mkdir(parents=True)
        task_file.write_text("task contents")
        result = sessions.session_prompt(self._meta(tmp_path, "my-task"))
        assert ".hatchery/tasks/2026-01-15-my-task/task.md" in result

    def test_is_string(self, tmp_path):
        task_file = tmp_path / ".hatchery" / "tasks" / "2026-01-15-foo.md"
        task_file.parent.mkdir(parents=True)
        task_file.write_text("contents")
        result = sessions.session_prompt(self._meta(tmp_path, "foo"))
        assert isinstance(result, str)

    def test_file_not_found_returns_fallback_prompt(self, tmp_path):
        """Resume must not crash when the task file is missing — instead the
        prompt explains the situation so the agent can recover.
        """
        (tmp_path / ".hatchery" / "tasks").mkdir(parents=True)
        result = sessions.session_prompt(self._meta(tmp_path, "nonexistent"))
        assert isinstance(result, str)
        assert "nonexistent" in result
        assert "not present" in result or "missing" in result

    def test_file_not_found_when_tasks_dir_absent(self, tmp_path):
        """Even with no .hatchery/tasks/ directory at all, session_prompt
        should return a fallback string rather than raising.
        """
        result = sessions.session_prompt(self._meta(tmp_path, "nonexistent"))
        assert isinstance(result, str)
        assert "nonexistent" in result

    def test_extra_note_prepended_when_file_present(self, tmp_path):
        task_file = tmp_path / ".hatchery" / "tasks" / "2026-01-15-foo.md"
        task_file.parent.mkdir(parents=True)
        task_file.write_text("contents")
        result = sessions.session_prompt(self._meta(tmp_path, "foo"), extra_note="HELLO")
        assert result.startswith("HELLO")
        assert "contents" in result

    def test_extra_note_prepended_when_file_missing(self, tmp_path):
        result = sessions.session_prompt(self._meta(tmp_path, "foo"), extra_note="HELLO")
        assert result.startswith("HELLO")
        assert "foo" in result

    def test_finds_file_from_different_date(self, tmp_path):
        """Regression: resuming a task created on a different day must work."""
        task_file = tmp_path / ".hatchery" / "tasks" / "2026-01-01-old-task" / "task.md"
        task_file.parent.mkdir(parents=True)
        task_file.write_text("created yesterday")
        result = sessions.session_prompt(self._meta(tmp_path, "old-task"))
        assert "2026-01-01-old-task/task.md" in result
        assert "created yesterday" in result


# ---------------------------------------------------------------------------
# repo_id
# ---------------------------------------------------------------------------


class TestRepoId:
    def test_contains_repo_basename(self):
        repo = Path("/home/user/my-project")
        result = utils.repo_id(repo)
        assert result.startswith("my-project-")

    def test_contains_hash_suffix(self):
        repo = Path("/some/repo")
        result = utils.repo_id(repo)
        parts = result.rsplit("-", 1)
        assert len(parts) == 2
        assert len(parts[1]) == 8

    def test_stable_for_same_path(self):
        repo = Path("/some/repo")
        assert utils.repo_id(repo) == utils.repo_id(repo)

    def test_different_for_different_paths(self):
        repo_a = Path("/repos/project-a")
        repo_b = Path("/repos/project-b")
        assert utils.repo_id(repo_a) != utils.repo_id(repo_b)

    def test_same_basename_different_path_differs(self):
        repo_a = Path("/alice/myapp")
        repo_b = Path("/bob/myapp")
        # Same basename but different full paths → different IDs
        assert utils.repo_id(repo_a) != utils.repo_id(repo_b)

    def test_returns_string(self):
        assert isinstance(utils.repo_id(Path("/some/repo")), str)


# ---------------------------------------------------------------------------
# task_db_path
# ---------------------------------------------------------------------------


class TestTaskDbPath:
    def test_returns_unified_dir_path_in_tasks_db_dir(self, fake_tasks_db):
        repo = Path("/some/repo")
        result = sessions.task_db_path(repo, "my-task")
        expected = fake_tasks_db / utils.repo_id(repo) / "my-task" / "meta.json"
        assert result == expected

    def test_extension_is_json(self, fake_tasks_db):
        result = sessions.task_db_path(Path("/some/repo"), "foo")
        assert result.suffix == ".json"

    def test_name_in_parent_dir(self, fake_tasks_db):
        result = sessions.task_db_path(Path("/some/repo"), "special-task")
        assert "special-task" in result.parent.name

    def test_different_repos_different_dirs(self, fake_tasks_db):
        repo_a = Path("/repos/alpha")
        repo_b = Path("/repos/beta")
        path_a = sessions.task_db_path(repo_a, "my-task")
        path_b = sessions.task_db_path(repo_b, "my-task")
        assert path_a.parent != path_b.parent


# ---------------------------------------------------------------------------
# docker_image_name
# ---------------------------------------------------------------------------


class TestDockerImageName:
    def test_format(self):
        repo = Path("/home/user/my-project")
        result = sessions.image_name(repo, "my-task")
        assert result == "hatchery/my-project:my-task"

    def test_lowercases_repo_name(self):
        repo = Path("/home/user/MyProject")
        result = sessions.image_name(repo, "task")
        assert "myproject" in result.lower()

    def test_includes_task_name(self):
        repo = Path("/some/repo")
        result = sessions.image_name(repo, "fix-bug")
        assert "fix-bug" in result

    def test_normalizes_dots(self):
        repo = Path("/home/user/my.project")
        result = sessions.image_name(repo, "task")
        assert result == "hatchery/my-project:task"

    def test_normalizes_plus(self):
        repo = Path("/home/user/foo+bar")
        result = sessions.image_name(repo, "task")
        assert result == "hatchery/foo-bar:task"

    def test_normalizes_spaces(self):
        repo = Path("/home/user/my project")
        result = sessions.image_name(repo, "task")
        assert result == "hatchery/my-project:task"

    def test_normalizes_leading_trailing_special(self):
        repo = Path("/home/user/.hidden-repo")
        result = sessions.image_name(repo, "task")
        assert result == "hatchery/hidden-repo:task"


# ---------------------------------------------------------------------------
# dockerfile_path
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# worktrees_dir
# ---------------------------------------------------------------------------


class TestWorktreesDir:
    def test_returns_path_inside_repo(self):
        repo = Path("/some/repo")
        result = sessions.worktrees_dir(repo)
        assert result == Path("/some/repo/.hatchery/worktrees")

    def test_is_under_hatchery(self):
        repo = Path("/my/repo")
        result = sessions.worktrees_dir(repo)
        assert ".hatchery" in str(result)
        assert "worktrees" in str(result)


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------


class TestMigrate:
    def test_v0_migrates_to_current(self):
        meta = {"name": "test"}
        result = sessions._migrate(meta)
        assert result == {"name": "test", "schema_version": 1}

    def test_v1_is_idempotent(self):
        meta = {"name": "test", "branch_prefix": "agents-", "schema_version": 1}
        result = sessions._migrate(meta)
        assert result == meta

    def test_v0_does_not_add_agent_field(self):
        meta = {"name": "test"}
        result = sessions._migrate(meta)
        assert "agent" not in result

    def test_other_fields_preserved(self):
        meta = {"name": "test", "status": "in-progress", "branch": "hatchery/test"}
        result = sessions._migrate(meta)
        assert result["name"] == "test"
        assert result["status"] == "in-progress"
        assert result["branch"] == "hatchery/test"

    def test_v0_without_schema_version_key(self):
        meta = {"name": "test", "status": "in-progress"}
        assert "schema_version" not in meta
        result = sessions._migrate(meta)
        assert result["schema_version"] == 1

    def test_current_schema_version_is_1(self):
        assert sessions.SCHEMA_VERSION == 1


# ---------------------------------------------------------------------------
# sandbox_context
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# sandbox_context — no-worktree mode
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ContainerRuntime backends
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# DockerConfig validation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# DockerConfig.cap_add validation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# DockerConfig.environment validation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# DinD cap_add merging
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _migrate_docker_config
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# build_spec DinD flags
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _SECCOMP bundled resource
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# build_spec container name
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# task_container_name
# ---------------------------------------------------------------------------


class TestTaskContainerName:
    """Verify task_container_name produces the expected deterministic name."""

    def test_format(self, tmp_path):
        repo = tmp_path / "my-project"
        repo.mkdir()
        expected = f"hatchery-{utils.repo_id(repo)}-my-task"
        assert sessions.container_name(repo, "my-task") == expected

    def test_different_paths_same_name_differ(self, tmp_path):
        repo_a = tmp_path / "a" / "myrepo"
        repo_b = tmp_path / "b" / "myrepo"
        repo_a.mkdir(parents=True)
        repo_b.mkdir(parents=True)
        assert sessions.container_name(repo_a, "t") != sessions.container_name(repo_b, "t")


# ---------------------------------------------------------------------------
# exec_task_shell
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# sandbox_context — include_paths
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# IncludeEntry — serialisation / deserialisation
# ---------------------------------------------------------------------------


class TestIncludeEntry:
    def test_serialize_worktree_entry(self):
        from seekr_hatchery.includes import IncludeEntry, serialize_include_entries

        entries = [IncludeEntry(path=Path("/a/b"), mode="worktree")]
        assert serialize_include_entries(entries) == [{"path": "/a/b", "mode": "worktree"}]

    def test_serialize_reference_entries(self):
        from seekr_hatchery.includes import IncludeEntry, serialize_include_entries

        entries = [
            IncludeEntry(path=Path("/a/b"), mode="ro"),
            IncludeEntry(path=Path("/c/d"), mode="rw"),
        ]
        result = serialize_include_entries(entries)
        assert result == [{"path": "/a/b", "mode": "ro"}, {"path": "/c/d", "mode": "rw"}]

    def test_load_new_format(self):
        from seekr_hatchery.includes import IncludeEntry, load_include_entries

        meta = {"include": [{"path": "/a/b", "mode": "ro"}, {"path": "/c/d", "mode": "worktree"}]}
        entries = load_include_entries(meta)
        assert entries == [IncludeEntry(Path("/a/b"), "ro"), IncludeEntry(Path("/c/d"), "worktree")]

    def test_load_old_format_string_list(self):
        """Old meta.json format (plain strings) is silently upgraded to worktree mode."""
        from seekr_hatchery.includes import IncludeEntry, load_include_entries

        meta = {"include": ["/a/b", "/c/d"]}
        entries = load_include_entries(meta)
        assert entries == [IncludeEntry(Path("/a/b"), "worktree"), IncludeEntry(Path("/c/d"), "worktree")]

    def test_load_missing_key_returns_empty(self):
        from seekr_hatchery.sessions import load_include_entries

        assert load_include_entries({}) == []

    def test_is_reference_true_for_ro_rw(self):
        from seekr_hatchery.includes import IncludeEntry

        assert IncludeEntry(Path("/x"), mode="ro").is_reference() is True
        assert IncludeEntry(Path("/x"), mode="rw").is_reference() is True

    def test_is_reference_false_for_worktree(self):
        from seekr_hatchery.includes import IncludeEntry

        assert IncludeEntry(Path("/x"), mode="worktree").is_reference() is False

    def test_load_unknown_mode_defaults_to_worktree(self):
        from seekr_hatchery.includes import IncludeEntry, load_include_entries

        meta = {"include": [{"path": "/a/b", "mode": "bogus"}]}
        entries = load_include_entries(meta)
        assert entries == [IncludeEntry(Path("/a/b"), "worktree")]
