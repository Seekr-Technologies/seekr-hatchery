"""Tests for pure functions — no mocking required."""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import seekr_hatchery.agents as agent
import seekr_hatchery.docker as docker
import seekr_hatchery.tasks as tasks


def _make_mutator(key: str = "test-key"):
    """Return a simple header mutator for tests."""
    def _mutate(headers):
        out = {k: v for k, v in headers.items() if k.lower() not in ("x-api-key", "authorization")}
        out["Authorization"] = f"Bearer {key}"
        return out
    return _mutate

# ---------------------------------------------------------------------------
# find_task_file
# ---------------------------------------------------------------------------


class TestFindTaskFile:
    def test_finds_matching_file(self, tmp_path):
        task_file = tmp_path / ".hatchery" / "tasks" / "2026-01-15-my-task.md"
        task_file.parent.mkdir(parents=True)
        task_file.write_text("contents")
        assert tasks.find_task_file(tmp_path, "my-task") == task_file

    def test_returns_none_when_no_match(self, tmp_path):
        (tmp_path / ".hatchery" / "tasks").mkdir(parents=True)
        assert tasks.find_task_file(tmp_path, "nonexistent") is None

    def test_returns_latest_when_multiple(self, tmp_path):
        tasks_dir = tmp_path / ".hatchery" / "tasks"
        tasks_dir.mkdir(parents=True)
        (tasks_dir / "2026-01-10-my-task.md").write_text("old")
        (tasks_dir / "2026-03-04-my-task.md").write_text("new")
        result = tasks.find_task_file(tmp_path, "my-task")
        assert result == tasks_dir / "2026-03-04-my-task.md"


# ---------------------------------------------------------------------------
# to_name
# ---------------------------------------------------------------------------


class TestToName:
    def test_lowercases(self):
        assert tasks.to_name("MyTask") == "mytask"

    def test_replaces_spaces_with_hyphens(self):
        assert tasks.to_name("add auth") == "add-auth"

    def test_replaces_special_chars_with_hyphens(self):
        assert tasks.to_name("fix: bug #42") == "fix-bug-42"

    def test_collapses_consecutive_separators(self):
        assert tasks.to_name("a  b   c") == "a-b-c"

    def test_strips_leading_hyphens(self):
        assert tasks.to_name("--task") == "task"

    def test_strips_trailing_hyphens(self):
        assert tasks.to_name("task--") == "task"

    def test_strips_both_ends(self):
        assert tasks.to_name("  task  ") == "task"

    def test_truncates_to_50_chars(self):
        long_name = "a" * 60
        result = tasks.to_name(long_name)
        assert len(result) == 50

    def test_truncation_at_boundary(self):
        name = "a" * 50
        assert tasks.to_name(name) == name

    def test_handles_empty_string(self):
        assert tasks.to_name("") == ""

    def test_handles_all_special_chars(self):
        assert tasks.to_name("!!!") == ""

    def test_alphanumeric_unchanged(self):
        assert tasks.to_name("abc123") == "abc123"

    def test_hyphens_in_input_preserved(self):
        assert tasks.to_name("my-task-name") == "my-task-name"

    def test_underscores_replaced(self):
        assert tasks.to_name("my_task") == "my-task"

    def test_mixed_case_with_symbols(self):
        assert tasks.to_name("Add-Authentication") == "add-authentication"


# ---------------------------------------------------------------------------
# task_file_name
# ---------------------------------------------------------------------------


class TestTaskFileName:
    def test_format(self):
        with patch("seekr_hatchery.tasks.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 15, 10, 30)
            result = tasks.task_file_name("my-task")
        assert result == "2026-01-15-my-task.md"

    def test_uses_current_date(self):
        # Without mocking — just check format shape
        result = tasks.task_file_name("test")
        parts = result.split("-")
        assert len(parts) >= 4
        assert result.endswith(".md")
        assert parts[0].isdigit() and len(parts[0]) == 4  # year


# ---------------------------------------------------------------------------
# session_prompt
# ---------------------------------------------------------------------------


class TestSessionPrompt:
    def test_contains_task_file_path(self, tmp_path):
        task_file = tmp_path / ".hatchery" / "tasks" / "2026-01-15-my-task.md"
        task_file.parent.mkdir(parents=True)
        task_file.write_text("task contents")
        result = tasks.session_prompt("my-task", tmp_path)
        assert ".hatchery/tasks/2026-01-15-my-task.md" in result

    def test_is_string(self, tmp_path):
        task_file = tmp_path / ".hatchery" / "tasks" / "2026-01-15-foo.md"
        task_file.parent.mkdir(parents=True)
        task_file.write_text("contents")
        result = tasks.session_prompt("foo", tmp_path)
        assert isinstance(result, str)

    def test_file_not_found_exits(self, tmp_path):
        (tmp_path / ".hatchery" / "tasks").mkdir(parents=True)
        with pytest.raises(SystemExit, match="1"):
            tasks.session_prompt("nonexistent", tmp_path)

    def test_finds_file_from_different_date(self, tmp_path):
        """Regression: resuming a task created on a different day must work."""
        task_file = tmp_path / ".hatchery" / "tasks" / "2026-01-01-old-task.md"
        task_file.parent.mkdir(parents=True)
        task_file.write_text("created yesterday")
        result = tasks.session_prompt("old-task", tmp_path)
        assert "2026-01-01-old-task.md" in result
        assert "created yesterday" in result


# ---------------------------------------------------------------------------
# repo_id
# ---------------------------------------------------------------------------


class TestRepoId:
    def test_contains_repo_basename(self):
        repo = Path("/home/user/my-project")
        result = tasks.repo_id(repo)
        assert result.startswith("my-project-")

    def test_contains_hash_suffix(self):
        repo = Path("/some/repo")
        result = tasks.repo_id(repo)
        parts = result.rsplit("-", 1)
        assert len(parts) == 2
        assert len(parts[1]) == 8

    def test_stable_for_same_path(self):
        repo = Path("/some/repo")
        assert tasks.repo_id(repo) == tasks.repo_id(repo)

    def test_different_for_different_paths(self):
        repo_a = Path("/repos/project-a")
        repo_b = Path("/repos/project-b")
        assert tasks.repo_id(repo_a) != tasks.repo_id(repo_b)

    def test_same_basename_different_path_differs(self):
        repo_a = Path("/alice/myapp")
        repo_b = Path("/bob/myapp")
        # Same basename but different full paths → different IDs
        assert tasks.repo_id(repo_a) != tasks.repo_id(repo_b)

    def test_returns_string(self):
        assert isinstance(tasks.repo_id(Path("/some/repo")), str)


# ---------------------------------------------------------------------------
# task_db_path
# ---------------------------------------------------------------------------


class TestTaskDbPath:
    def test_returns_unified_dir_path_in_tasks_db_dir(self, fake_tasks_db):
        repo = Path("/some/repo")
        result = tasks.task_db_path(repo, "my-task")
        expected = fake_tasks_db / tasks.repo_id(repo) / "my-task" / "meta.json"
        assert result == expected

    def test_extension_is_json(self, fake_tasks_db):
        result = tasks.task_db_path(Path("/some/repo"), "foo")
        assert result.suffix == ".json"

    def test_name_in_parent_dir(self, fake_tasks_db):
        result = tasks.task_db_path(Path("/some/repo"), "special-task")
        assert "special-task" in result.parent.name

    def test_different_repos_different_dirs(self, fake_tasks_db):
        repo_a = Path("/repos/alpha")
        repo_b = Path("/repos/beta")
        path_a = tasks.task_db_path(repo_a, "my-task")
        path_b = tasks.task_db_path(repo_b, "my-task")
        assert path_a.parent != path_b.parent


# ---------------------------------------------------------------------------
# docker_image_name
# ---------------------------------------------------------------------------


class TestDockerImageName:
    def test_format(self):
        repo = Path("/home/user/my-project")
        result = docker.docker_image_name(repo, "my-task")
        assert result == "hatchery/my-project:my-task"

    def test_lowercases_repo_name(self):
        repo = Path("/home/user/MyProject")
        result = docker.docker_image_name(repo, "task")
        assert "myproject" in result.lower()

    def test_includes_task_name(self):
        repo = Path("/some/repo")
        result = docker.docker_image_name(repo, "fix-bug")
        assert "fix-bug" in result

    def test_normalizes_dots(self):
        repo = Path("/home/user/my.project")
        result = docker.docker_image_name(repo, "task")
        assert result == "hatchery/my-project:task"

    def test_normalizes_plus(self):
        repo = Path("/home/user/foo+bar")
        result = docker.docker_image_name(repo, "task")
        assert result == "hatchery/foo-bar:task"

    def test_normalizes_spaces(self):
        repo = Path("/home/user/my project")
        result = docker.docker_image_name(repo, "task")
        assert result == "hatchery/my-project:task"

    def test_normalizes_leading_trailing_special(self):
        repo = Path("/home/user/.hidden-repo")
        result = docker.docker_image_name(repo, "task")
        assert result == "hatchery/hidden-repo:task"


# ---------------------------------------------------------------------------
# dockerfile_path
# ---------------------------------------------------------------------------


class TestDockerfilePath:
    def test_returns_agent_specific_path(self):
        repo = Path("/some/repo")
        assert docker.dockerfile_path(repo, agent.CODEX) == Path("/some/repo/.hatchery/Dockerfile.codex")


# ---------------------------------------------------------------------------
# worktrees_dir
# ---------------------------------------------------------------------------


class TestWorktreesDir:
    def test_returns_path_inside_repo(self):
        repo = Path("/some/repo")
        result = tasks.worktrees_dir(repo)
        assert result == Path("/some/repo/.hatchery/worktrees")

    def test_is_under_hatchery(self):
        repo = Path("/my/repo")
        result = tasks.worktrees_dir(repo)
        assert ".hatchery" in str(result)
        assert "worktrees" in str(result)


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------


class TestMigrate:
    def test_v0_migrates_to_v1(self):
        meta = {"name": "test"}
        result = tasks.migrate(meta)
        assert result["schema_version"] == 1

    def test_v1_idempotent(self):
        meta = {"name": "test", "schema_version": 1}
        result = tasks.migrate(meta)
        assert result["schema_version"] == 1

    def test_v0_does_not_add_agent_field(self):
        meta = {"name": "test"}
        result = tasks.migrate(meta)
        assert "agent" not in result

    def test_other_fields_preserved(self):
        meta = {"name": "test", "status": "paused", "branch": "hatchery/test"}
        result = tasks.migrate(meta)
        assert result["name"] == "test"
        assert result["status"] == "paused"
        assert result["branch"] == "hatchery/test"

    def test_v0_without_schema_version_key(self):
        meta = {"name": "test", "status": "paused"}
        assert "schema_version" not in meta
        result = tasks.migrate(meta)
        assert result["schema_version"] == 1

    def test_current_schema_version_is_1(self):
        assert tasks.SCHEMA_VERSION == 1


# ---------------------------------------------------------------------------
# sandbox_context
# ---------------------------------------------------------------------------


class TestSandboxContext:
    def _native(self, **kwargs):
        defaults = dict(
            name="my-task",
            branch="hatchery/my-task",
            worktree=Path("/repo/.hatchery/worktrees/my-task"),
            repo=Path("/repo"),
            main_branch="main",
            use_docker=False,
        )
        defaults.update(kwargs)
        return tasks.sandbox_context(**defaults)

    def _docker(self, **kwargs):
        defaults = dict(
            name="my-task",
            branch="hatchery/my-task",
            worktree=Path("/repo/.hatchery/worktrees/my-task"),
            repo=Path("/repo"),
            main_branch="main",
            use_docker=True,
        )
        defaults.update(kwargs)
        return tasks.sandbox_context(**defaults)

    # --- return type ---

    def test_returns_string(self):
        assert isinstance(self._native(), str)

    # --- native mode ---

    def test_native_mentions_native_worktree(self):
        result = self._native()
        assert "native git worktree" in result

    def test_native_does_not_claim_docker_container(self):
        result = self._native()
        assert "Docker container" not in result

    def test_native_contains_working_directory(self):
        result = self._native(worktree=Path("/repo/.hatchery/worktrees/my-task"))
        assert "/repo/.hatchery/worktrees/my-task" in result

    def test_native_contains_repo_root(self):
        result = self._native(repo=Path("/some/repo"))
        assert "/some/repo" in result

    def test_native_contains_branch(self):
        result = self._native(branch="hatchery/my-task")
        assert "hatchery/my-task" in result

    def test_native_contains_main_branch(self):
        result = self._native(main_branch="develop")
        assert "develop" in result

    def test_native_pr_target_instruction(self):
        result = self._native(main_branch="main")
        assert "main" in result
        assert "pull request" in result.lower() or "PR" in result

    # --- docker mode ---

    def test_docker_mentions_docker_container(self):
        result = self._docker()
        assert "Docker container" in result

    def test_docker_does_not_mention_native(self):
        result = self._docker()
        assert "native git worktree" not in result

    def test_docker_contains_container_worktree_path(self):
        result = self._docker(name="my-task")
        assert "/repo/.hatchery/worktrees/my-task" in result

    def test_docker_contains_repo_root(self):
        result = self._docker()
        assert tasks.CONTAINER_REPO_ROOT in result

    def test_docker_mentions_read_write(self):
        result = self._docker()
        assert "read-write" in result

    def test_docker_mentions_read_only(self):
        result = self._docker()
        assert "read-only" in result

    def test_docker_contains_branch(self):
        result = self._docker(branch="hatchery/my-task")
        assert "hatchery/my-task" in result

    def test_docker_contains_main_branch(self):
        result = self._docker(main_branch="master")
        assert "master" in result

    def test_docker_pr_target_instruction(self):
        result = self._docker(main_branch="main")
        assert "main" in result
        assert "pull request" in result.lower() or "PR" in result


# ---------------------------------------------------------------------------
# sandbox_context — no-worktree mode
# ---------------------------------------------------------------------------


class TestSandboxContextNoWorktree:
    def _no_worktree_native(self, branch: str = "", **kwargs) -> str:
        defaults = dict(
            name="my-task",
            branch=branch,
            worktree=Path("/projects/my-workspace"),
            repo=Path("/projects/my-workspace"),
            main_branch="main",
            use_docker=False,
            no_worktree=True,
        )
        defaults.update(kwargs)
        return tasks.sandbox_context(**defaults)

    def _no_worktree_docker(self, branch: str = "", **kwargs) -> str:
        defaults = dict(
            name="my-task",
            branch=branch,
            worktree=Path("/projects/my-workspace"),
            repo=Path("/projects/my-workspace"),
            main_branch="main",
            use_docker=True,
            no_worktree=True,
        )
        defaults.update(kwargs)
        return tasks.sandbox_context(**defaults)

    # --- native no-worktree ---

    def test_native_no_worktree_mentions_directly(self):
        result = self._no_worktree_native()
        assert "directly in the working directory" in result

    def test_native_no_worktree_does_not_claim_docker(self):
        result = self._no_worktree_native()
        assert "Docker container" not in result

    def test_native_no_worktree_does_not_claim_native_worktree(self):
        result = self._no_worktree_native()
        assert "native git worktree" not in result

    def test_native_no_worktree_shows_working_directory(self):
        result = self._no_worktree_native(worktree=Path("/some/dir"))
        assert "/some/dir" in result

    def test_native_no_worktree_branch_shown_when_nonempty(self):
        result = self._no_worktree_native(branch="hatchery/my-task")
        assert "hatchery/my-task" in result

    def test_native_no_worktree_branch_omitted_when_empty(self):
        result = self._no_worktree_native(branch="")
        assert "Your branch" not in result

    def test_native_no_worktree_pr_target_omitted_when_no_branch(self):
        result = self._no_worktree_native(branch="")
        assert "Target branch" not in result

    # --- docker no-worktree ---

    def test_docker_no_worktree_mentions_docker_container(self):
        result = self._no_worktree_docker()
        assert "Docker container" in result

    def test_docker_no_worktree_mentions_workspace(self):
        result = self._no_worktree_docker()
        assert "/workspace" in result

    def test_docker_no_worktree_does_not_claim_native_worktree(self):
        result = self._no_worktree_docker()
        assert "native git worktree" not in result

    def test_docker_no_worktree_branch_shown_when_nonempty(self):
        result = self._no_worktree_docker(branch="hatchery/my-task")
        assert "hatchery/my-task" in result

    def test_docker_no_worktree_branch_omitted_when_empty(self):
        result = self._no_worktree_docker(branch="")
        assert "Your branch" not in result

    def test_docker_no_worktree_pr_target_omitted_when_no_branch(self):
        result = self._no_worktree_docker(branch="")
        assert "Target branch" not in result

    def test_docker_no_worktree_pr_target_shown_when_branch_present(self):
        result = self._no_worktree_docker(branch="hatchery/my-task", main_branch="develop")
        assert "develop" in result


# ---------------------------------------------------------------------------
# Runtime enum
# ---------------------------------------------------------------------------


class TestRuntime:
    def test_podman_binary(self):
        assert docker.Runtime.PODMAN.binary == "podman"

    def test_docker_binary(self):
        assert docker.Runtime.DOCKER.binary == "docker"

    def test_values_are_uppercase(self):
        assert docker.Runtime.PODMAN.value == "PODMAN"
        assert docker.Runtime.DOCKER.value == "DOCKER"

    def test_binary_differs_from_value(self):
        # binary is lowercase; value is UPPERCASE — they must differ
        assert docker.Runtime.PODMAN.binary != docker.Runtime.PODMAN.value
        assert docker.Runtime.DOCKER.binary != docker.Runtime.DOCKER.value


# ---------------------------------------------------------------------------
# DockerConfig validation
# ---------------------------------------------------------------------------


class TestDockerConfig:
    def test_valid_empty_mounts(self):
        config = docker.DockerConfig.model_validate({"schema_version": "1", "mounts": []})
        assert config.mounts == []

    def test_null_mounts_treated_as_empty(self):
        config = docker.DockerConfig.model_validate({"schema_version": "1", "mounts": None})
        assert config.mounts == []

    def test_default_mounts_is_empty_list(self):
        config = docker.DockerConfig.model_validate({"schema_version": "1"})
        assert config.mounts == []

    def test_valid_mount_ro(self):
        config = docker.DockerConfig.model_validate({"schema_version": "1", "mounts": ["/host:/container:ro"]})
        assert config.mounts == ["/host:/container:ro"]

    def test_valid_mount_rw(self):
        config = docker.DockerConfig.model_validate({"schema_version": "1", "mounts": ["/host:/container:rw"]})
        assert config.mounts == ["/host:/container:rw"]

    def test_valid_mount_without_mode(self):
        config = docker.DockerConfig.model_validate({"schema_version": "1", "mounts": ["/host:/container"]})
        assert config.mounts == ["/host:/container"]

    def test_invalid_mount_no_colon(self):
        with pytest.raises(Exception):
            docker.DockerConfig.model_validate({"schema_version": "1", "mounts": ["/hostonly"]})

    def test_invalid_mount_empty_host(self):
        with pytest.raises(Exception):
            docker.DockerConfig.model_validate({"schema_version": "1", "mounts": [":/container:ro"]})

    def test_invalid_mount_empty_container(self):
        with pytest.raises(Exception):
            docker.DockerConfig.model_validate({"schema_version": "1", "mounts": ["/host::ro"]})

    def test_invalid_mode(self):
        with pytest.raises(Exception):
            docker.DockerConfig.model_validate({"schema_version": "1", "mounts": ["/host:/container:rx"]})

    def test_non_string_mount_entry(self):
        with pytest.raises(Exception):
            docker.DockerConfig.model_validate({"schema_version": "1", "mounts": [123]})

    def test_extra_keys_forbidden(self):
        with pytest.raises(Exception):
            docker.DockerConfig.model_validate({"schema_version": "1", "mounts": [], "unknown_key": "oops"})

    def test_dind_defaults_to_false(self):
        config = docker.DockerConfig.model_validate({"schema_version": "1"})
        assert config.dind is False

    def test_dind_true_accepted(self):
        config = docker.DockerConfig.model_validate({"schema_version": "1", "dind": True})
        assert config.dind is True

    def test_dind_false_accepted(self):
        config = docker.DockerConfig.model_validate({"schema_version": "1", "dind": False})
        assert config.dind is False


# ---------------------------------------------------------------------------
# DockerConfig.cap_add validation
# ---------------------------------------------------------------------------


class TestDockerConfigCapAdd:
    def test_defaults_empty(self):
        assert docker.DockerConfig().cap_add == []

    def test_uppercase_normalization(self):
        config = docker.DockerConfig(cap_add=["net_admin"])
        assert config.cap_add == ["NET_ADMIN"]

    def test_invalid_cap_rejected(self):
        with pytest.raises(Exception):
            docker.DockerConfig(cap_add=["INVALID"])

    def test_none_treated_as_empty(self):
        config = docker.DockerConfig(cap_add=None)
        assert config.cap_add == []


# ---------------------------------------------------------------------------
# DinD cap_add merging
# ---------------------------------------------------------------------------


class TestDindCapMerge:
    """Verify _run_container merges user cap_add with DinD defaults."""

    _COMMON = dict(
        image="test-image:task",
        mounts=[],
        workdir="/repo/.hatchery/worktrees/task",
        hatchery_repo="/repo",
        name="task",
        mutator=_make_mutator("test-key"),
        proxy_token="test-proxy-token",
        agent_cmd=["codex"],
        backend=agent.CODEX,
    )

    def _run(self, **kwargs) -> list[str]:
        args = {**self._COMMON, **kwargs}
        mock_server = MagicMock()
        mock_server.server_address = ("0.0.0.0", 9999)
        with patch("seekr_hatchery.docker.subprocess.run") as mock_run, \
             patch("seekr_hatchery.proxy.start_proxy", return_value=(mock_server, "tok")), \
             patch("seekr_hatchery.proxy.stop_proxy"):
            docker._run_container(**args)
        return mock_run.call_args[0][0]

    def test_user_caps_merged(self):
        cmd = self._run(dind=True, cap_add=["NET_BIND_SERVICE"])
        cap_add_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--cap-add"]
        assert "NET_BIND_SERVICE" in cap_add_values

    def test_duplicates_eliminated(self):
        cmd = self._run(dind=True, cap_add=["SYS_ADMIN"])
        cap_add_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--cap-add"]
        assert cap_add_values.count("SYS_ADMIN") == 1

    def test_caps_ignored_when_dind_false(self):
        cmd = self._run(dind=False, cap_add=["NET_ADMIN"])
        assert "--cap-add" not in cmd


# ---------------------------------------------------------------------------
# _migrate_docker_config
# ---------------------------------------------------------------------------


class TestMigrateDockerConfig:
    def test_v0_stamped_to_v1(self):
        data = {"mounts": []}
        result = docker._migrate_docker_config(data)
        assert result["schema_version"] == "1"

    def test_v1_idempotent(self):
        data = {"schema_version": "1", "mounts": []}
        result = docker._migrate_docker_config(data)
        assert result["schema_version"] == "1"

    def test_other_fields_preserved(self):
        data = {"mounts": ["/host:/container:ro"]}
        result = docker._migrate_docker_config(data)
        assert result["mounts"] == ["/host:/container:ro"]

    def test_missing_schema_version_treated_as_v0(self):
        data = {"mounts": []}
        assert "schema_version" not in data
        result = docker._migrate_docker_config(data)
        assert result["schema_version"] == "1"

    def test_legacy_int_schema_version_coerced_to_str(self):
        # Existing docker.yaml files written before this change have schema_version: 1 (int)
        data = {"schema_version": 1, "mounts": []}
        result = docker._migrate_docker_config(data)
        assert result["schema_version"] == "1"


# ---------------------------------------------------------------------------
# _run_container DinD flags
# ---------------------------------------------------------------------------


class TestRunContainerDindFlags:
    """Verify _run_container injects the correct flags depending on dind=."""

    _COMMON = dict(
        image="test-image:task",
        mounts=[],
        workdir="/repo/.hatchery/worktrees/task",
        hatchery_repo="/repo",
        name="task",
        mutator=_make_mutator("test-key"),
        proxy_token="test-proxy-token",
        agent_cmd=["codex"],
        backend=agent.CODEX,
    )

    def _run(self, **kwargs) -> list[str]:
        """Call _run_container with mock subprocess and return the captured cmd."""
        args = {**self._COMMON, **kwargs}
        mock_server = MagicMock()
        mock_server.server_address = ("0.0.0.0", 9999)
        with patch("seekr_hatchery.docker.subprocess.run") as mock_run, \
             patch("seekr_hatchery.proxy.start_proxy", return_value=(mock_server, "tok")), \
             patch("seekr_hatchery.proxy.stop_proxy"):
            docker._run_container(**args)
        return mock_run.call_args[0][0]

    def test_dind_false_no_extra_flags(self):
        cmd = self._run(dind=False)
        assert "--cap-drop" not in cmd
        assert "--device" not in cmd
        assert "--security-opt" not in cmd

    def test_dind_true_all_flags(self):
        cmd = self._run(dind=True)
        # --cap-drop ALL
        cap_drop_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--cap-drop"]
        assert cap_drop_values == ["ALL"]
        # exact set of capabilities re-added
        cap_add_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--cap-add"]
        assert set(cap_add_values) == {
            "SYS_ADMIN",
            "MKNOD",
            "SETUID",
            "SETGID",
            "CHOWN",
            "DAC_OVERRIDE",
            "FOWNER",
            "SETFCAP",
            "SYS_CHROOT",
            "SETPCAP",
            "AUDIT_WRITE",
            "FSETID",
            "KILL",
            "NET_BIND_SERVICE",
            "NET_ADMIN",
            "NET_RAW",
        }
        # /dev/fuse device
        device_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--device"]
        assert device_values == ["/dev/fuse"]
        # security-opt: label=disable + seccomp profile
        security_opt_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--security-opt"]
        assert "label=disable" in security_opt_values
        assert any(v.startswith("seccomp=") and "seccomp.json" in v for v in security_opt_values)


# ---------------------------------------------------------------------------
# _SECCOMP bundled resource
# ---------------------------------------------------------------------------


class TestSeccompResource:
    def test_is_absolute_path(self):
        assert docker._SECCOMP.is_absolute()

    def test_file_exists_and_is_nonempty(self):
        assert docker._SECCOMP.exists()
        assert docker._SECCOMP.stat().st_size > 0

    def test_is_valid_json(self):
        import json

        data = json.loads(docker._SECCOMP.read_text())
        assert "syscalls" in data
