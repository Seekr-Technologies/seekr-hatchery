from pathlib import Path

import pytest
from pydantic import ValidationError

import seekr_hatchery.branches as branches
import seekr_hatchery.sandbox.config as sandbox_config
from seekr_hatchery.models import KubectlConfig, KubectlContext, SessionMeta

# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestDockerFeatures:
    def test_lists_s3_proxy_when_configured(self) -> None:
        config = sandbox_config.SandboxConfig(s3={"profiles": {"development": {}}})
        assert sandbox_config.features(config) == ["S3 proxy"]

    def test_lists_oci_proxy_when_configured(self) -> None:
        config = sandbox_config.SandboxConfig(oci={"profiles": {"DEFAULT": {}}})
        assert sandbox_config.features(config) == ["OCI proxy"]

    def test_lists_context_names_when_multiple_kubernetes_contexts_are_configured(self) -> None:
        config = sandbox_config.SandboxConfig(
            kubernetes=KubectlConfig(contexts=[KubectlContext(context="dev"), KubectlContext(context="prd")])
        )
        assert sandbox_config.features(config) == ["kubectl (dev, prd)"]

    def test_single_kubernetes_context_keeps_existing_feature_label(self) -> None:
        assert sandbox_config.features(sandbox_config.SandboxConfig(kubernetes=KubectlConfig(context="dev"))) == [
            "kubectl"
        ]


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestValidateDockerConfigFile:
    def test_valid_returns_none(self, tmp_path):
        path = tmp_path / "docker.yaml"
        path.write_text("schema_version: '1'\nmounts: []\n")
        assert sandbox_config.validate_config_file(path) is None

    def test_empty_file_returns_none(self, tmp_path):
        path = tmp_path / "docker.yaml"
        path.write_text("")
        assert sandbox_config.validate_config_file(path) is None

    def test_unknown_key_returns_error(self, tmp_path):
        path = tmp_path / "docker.yaml"
        path.write_text("bogus_key: 1\n")
        assert sandbox_config.validate_config_file(path) is not None

    def test_bad_capability_returns_error(self, tmp_path):
        path = tmp_path / "docker.yaml"
        path.write_text("cap_add: [BOGUS_CAP]\n")
        assert sandbox_config.validate_config_file(path) is not None

    def test_invalid_yaml_returns_error(self, tmp_path):
        path = tmp_path / "docker.yaml"
        path.write_text("mounts: [unterminated\n")
        assert "Invalid YAML" in sandbox_config.validate_config_file(path)


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestLoadConfigSourceErrors:
    def test_invalid_config_reports_config_path(self, tmp_path, capsys):
        hatchery_dir = tmp_path / ".hatchery"
        hatchery_dir.mkdir()
        config_file = hatchery_dir / "docker.yaml"
        config_file.write_text("unknown: value\n")

        with pytest.raises(SystemExit):
            sandbox_config.load_config(hatchery_dir)

        first_line = capsys.readouterr().err.splitlines()[0]
        assert first_line == f"Error: invalid docker config {config_file}: 1 validation error for DockerConfig"


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestDockerConfigClipboardImages:
    def test_defaults_to_true(self):
        assert sandbox_config.SandboxConfig().clipboard_images is True

    def test_parses_false(self):
        assert sandbox_config.SandboxConfig(clipboard_images=False).clipboard_images is False


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestDockerConfigFollowSymlinks:
    def test_defaults_to_false(self):
        assert sandbox_config.SandboxConfig().follow_symlinks is False

    def test_parses_true(self):
        assert sandbox_config.SandboxConfig(follow_symlinks=True).follow_symlinks is True


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestParseDockerIncludeEntry:
    def test_string_gives_worktree_mode(self):
        assert sandbox_config.parse_include_entry("../repo") == ("../repo", "worktree")

    def test_item_with_mode_ro(self):
        from seekr_hatchery.includes import IncludeItem

        assert sandbox_config.parse_include_entry(IncludeItem(path="../docs", mode="ro")) == ("../docs", "ro")

    def test_item_with_mode_rw(self):
        from seekr_hatchery.includes import IncludeItem

        assert sandbox_config.parse_include_entry(IncludeItem(path="../shared", mode="rw")) == ("../shared", "rw")

    def test_item_with_mode_worktree(self):
        from seekr_hatchery.includes import IncludeItem

        assert sandbox_config.parse_include_entry(IncludeItem(path="../repo", mode="worktree")) == (
            "../repo",
            "worktree",
        )

    def test_item_without_mode_defaults_to_worktree(self):
        from seekr_hatchery.includes import IncludeItem

        assert sandbox_config.parse_include_entry(IncludeItem(path="../repo")) == ("../repo", "worktree")


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestDockerConfigVolumes:
    def test_parses_volume_entry(self):
        config = sandbox_config.SandboxConfig(volumes=[{"name": "uv-cache", "path": "/home/hatchery/.cache/uv"}])
        assert config.volumes == [sandbox_config.CacheVolume(name="uv-cache", path="/home/hatchery/.cache/uv")]

    def test_name_with_colon_is_invalid(self):

        with pytest.raises(ValidationError):
            sandbox_config.SandboxConfig(volumes=[{"name": "bad:name", "path": "/cache"}])

    def test_name_with_slash_is_invalid(self):

        with pytest.raises(ValidationError):
            sandbox_config.SandboxConfig(volumes=[{"name": "bad/name", "path": "/cache"}])

    def test_relative_path_is_invalid(self):

        with pytest.raises(ValidationError):
            sandbox_config.SandboxConfig(volumes=[{"name": "uv-cache", "path": "relative/cache"}])

    def test_none_coerced_to_empty(self):
        # `volumes:` in YAML with all-commented entries parses to None;
        # match the `mounts:` behavior and treat that as an empty list.
        assert sandbox_config.SandboxConfig(volumes=None).volumes == []


def _task_branch(name: str = "my-task", prefix: str = branches.DEFAULT_BRANCH_PREFIX) -> branches.BranchName:
    return branches.BranchPrefix(prefix).task_branch(name)


def _no_wt_meta(cwd):
    """Synthetic SessionMeta for no-worktree mount tests."""
    return SessionMeta(name="-", repo=str(cwd), worktree=str(cwd), no_worktree=True)


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestDockerConfigInclude:
    def test_defaults_to_empty(self):
        config = sandbox_config.SandboxConfig()
        assert config.include == []

    def test_parses_string_include_list(self):
        config = sandbox_config.SandboxConfig(include=["../repo-b", "/abs/path"])
        assert config.include == ["../repo-b", "/abs/path"]

    def test_parses_dict_include_entry(self):
        from seekr_hatchery.includes import IncludeItem

        config = sandbox_config.SandboxConfig(include=[{"path": "../ref", "mode": "ro"}])
        assert config.include == [IncludeItem(path="../ref", mode="ro")]

    def test_parses_mixed_include_list(self):
        from seekr_hatchery.includes import IncludeItem

        config = sandbox_config.SandboxConfig(include=["../wt-repo", {"path": "../ref", "mode": "rw"}])
        assert config.include[0] == "../wt-repo"
        assert config.include[1] == IncludeItem(path="../ref", mode="rw")

    def test_dict_without_path_is_invalid(self):

        with pytest.raises(ValidationError):
            sandbox_config.SandboxConfig(include=[{"mode": "ro"}])

    def test_dict_invalid_mode_is_rejected(self):

        with pytest.raises(ValidationError):
            sandbox_config.SandboxConfig(include=[{"path": "../foo", "mode": "readwrite"}])

    def test_dict_extra_keys_are_rejected(self):

        with pytest.raises(ValidationError):
            sandbox_config.SandboxConfig(include=[{"path": "../foo", "mode": "ro", "extra": "oops"}])

    def test_extra_fields_still_forbidden(self):

        with pytest.raises(ValidationError):
            sandbox_config.SandboxConfig(unknown_field="oops")


# ---------------------------------------------------------------------------
# find_task_file
# ---------------------------------------------------------------------------


class TestMigrateDockerConfig:
    def test_v0_stamped_to_v1(self):
        data = {"mounts": []}
        result = _loaded_config_data(data)
        assert result["schema_version"] == "1"

    def test_v1_idempotent(self):
        data = {"schema_version": "1", "mounts": []}
        result = _loaded_config_data(data)
        assert result["schema_version"] == "1"

    def test_other_fields_preserved(self):
        data = {"mounts": ["/host:/container:ro"]}
        result = _loaded_config_data(data)
        assert result["mounts"] == ["/host:/container:ro"]

    def test_missing_schema_version_treated_as_v0(self):
        data = {"mounts": []}
        assert "schema_version" not in data
        result = _loaded_config_data(data)
        assert result["schema_version"] == "1"

    def test_legacy_int_schema_version_coerced_to_str(self):
        # Existing docker.yaml files written before this change have schema_version: 1 (int)
        data = {"schema_version": 1, "mounts": []}
        result = _loaded_config_data(data)
        assert result["schema_version"] == "1"


# ---------------------------------------------------------------------------
# find_task_file
# ---------------------------------------------------------------------------


class TestDockerConfigEnvironment:
    def test_defaults_empty(self):
        assert sandbox_config.SandboxConfig().environment == []

    def test_none_treated_as_empty(self):
        assert sandbox_config.SandboxConfig(environment=None).environment == []

    def test_mixed_literal_and_passthrough_entries(self):
        config = sandbox_config.SandboxConfig(environment=["VAR_A=x", "VAR_B", "VAR_C="])
        assert config.environment == ["VAR_A=x", "VAR_B", "VAR_C="]

    def test_empty_name_rejected(self):
        with pytest.raises(Exception):
            sandbox_config.SandboxConfig(environment=["=value"])

    def test_non_string_entry_rejected(self):
        with pytest.raises(Exception):
            sandbox_config.SandboxConfig(environment=[123])

    def test_resolve_environment(self, monkeypatch):
        monkeypatch.setenv("VAR_B", "from-host")
        monkeypatch.delenv("VAR_MISSING", raising=False)
        config = sandbox_config.SandboxConfig(environment=["VAR_A=x", "VAR_B", "VAR_C=", "VAR_MISSING"])
        assert sandbox_config.resolve_environment(config) == {"VAR_A": "x", "VAR_B": "from-host", "VAR_C": ""}


# ---------------------------------------------------------------------------
# find_task_file
# ---------------------------------------------------------------------------


class TestDockerConfigCapAdd:
    def test_defaults_empty(self):
        assert sandbox_config.SandboxConfig().cap_add == []

    def test_uppercase_normalization(self):
        config = sandbox_config.SandboxConfig(cap_add=["net_admin"])
        assert config.cap_add == ["NET_ADMIN"]

    def test_invalid_cap_rejected(self):
        with pytest.raises(Exception):
            sandbox_config.SandboxConfig(cap_add=["INVALID"])

    def test_none_treated_as_empty(self):
        config = sandbox_config.SandboxConfig(cap_add=None)
        assert config.cap_add == []


# ---------------------------------------------------------------------------
# find_task_file
# ---------------------------------------------------------------------------


class TestDockerConfig:
    def test_valid_empty_mounts(self):
        config = sandbox_config.SandboxConfig.model_validate({"schema_version": "1", "mounts": []})
        assert config.mounts == []

    def test_null_mounts_treated_as_empty(self):
        config = sandbox_config.SandboxConfig.model_validate({"schema_version": "1", "mounts": None})
        assert config.mounts == []

    def test_default_mounts_is_empty_list(self):
        config = sandbox_config.SandboxConfig.model_validate({"schema_version": "1"})
        assert config.mounts == []

    def test_valid_mount_ro(self):
        config = sandbox_config.SandboxConfig.model_validate({"schema_version": "1", "mounts": ["/host:/container:ro"]})
        assert config.mounts == ["/host:/container:ro"]

    def test_valid_mount_rw(self):
        config = sandbox_config.SandboxConfig.model_validate({"schema_version": "1", "mounts": ["/host:/container:rw"]})
        assert config.mounts == ["/host:/container:rw"]

    def test_valid_mount_without_mode(self):
        config = sandbox_config.SandboxConfig.model_validate({"schema_version": "1", "mounts": ["/host:/container"]})
        assert config.mounts == ["/host:/container"]

    def test_invalid_mount_no_colon(self):
        with pytest.raises(Exception):
            sandbox_config.SandboxConfig.model_validate({"schema_version": "1", "mounts": ["/hostonly"]})

    def test_invalid_mount_empty_host(self):
        with pytest.raises(Exception):
            sandbox_config.SandboxConfig.model_validate({"schema_version": "1", "mounts": [":/container:ro"]})

    def test_invalid_mount_empty_container(self):
        with pytest.raises(Exception):
            sandbox_config.SandboxConfig.model_validate({"schema_version": "1", "mounts": ["/host::ro"]})

    def test_invalid_mode(self):
        with pytest.raises(Exception):
            sandbox_config.SandboxConfig.model_validate({"schema_version": "1", "mounts": ["/host:/container:rx"]})

    def test_non_string_mount_entry(self):
        with pytest.raises(Exception):
            sandbox_config.SandboxConfig.model_validate({"schema_version": "1", "mounts": [123]})

    def test_extra_keys_forbidden(self):
        with pytest.raises(Exception):
            sandbox_config.SandboxConfig.model_validate({"schema_version": "1", "mounts": [], "unknown_key": "oops"})

    def test_dind_defaults_to_false(self):
        config = sandbox_config.SandboxConfig.model_validate({"schema_version": "1"})
        assert config.dind is False

    def test_dind_true_accepted(self):
        config = sandbox_config.SandboxConfig.model_validate({"schema_version": "1", "dind": True})
        assert config.dind is True

    def test_dind_false_accepted(self):
        config = sandbox_config.SandboxConfig.model_validate({"schema_version": "1", "dind": False})
        assert config.dind is False


# ---------------------------------------------------------------------------
# write_task_file
# ---------------------------------------------------------------------------


class TestLoadDockerConfig:
    def _write_config(self, repo: Path, content: str) -> None:
        (repo / "docker.yaml").write_text(content)

    def test_returns_empty_config_when_no_file(self, fake_repo):
        result = sandbox_config.load_config(fake_repo)
        assert isinstance(result, sandbox_config.SandboxConfig)
        assert result.mounts == []

    def test_returns_config_when_mounts_empty(self, fake_repo):
        self._write_config(fake_repo, "schema_version: 1\nmounts: []\n")
        result = sandbox_config.load_config(fake_repo)
        assert isinstance(result, sandbox_config.SandboxConfig)
        assert result.mounts == []

    def test_returns_config_when_mounts_null(self, fake_repo):
        # All entries commented out → YAML parses mounts as null
        self._write_config(fake_repo, "schema_version: 1\nmounts:\n")
        result = sandbox_config.load_config(fake_repo)
        assert result.mounts == []

    def test_returns_config_with_mount_entries(self, fake_repo):
        self._write_config(fake_repo, 'schema_version: 1\nmounts:\n  - "/host:/container:ro"\n')
        result = sandbox_config.load_config(fake_repo)
        assert result.mounts == ["/host:/container:ro"]

    def test_merges_local_lists_mappings_and_scalars(self, fake_repo):
        self._write_config(
            fake_repo,
            "schema_version: 1\n"
            "mounts: ['/shared:/shared:ro']\n"
            "environment: ['MODE=shared']\n"
            "follow_symlinks: false\n"
            "s3:\n  default: development\n  profiles:\n    development: {}\n",
        )
        (fake_repo / "docker.local.yaml").write_text(
            "schema_version: 1\n"
            "mounts: ['/local:/local:rw']\n"
            "environment: ['MODE=local', 'TOKEN']\n"
            "follow_symlinks: true\n"
            "s3:\n  profiles:\n    production: {}\n"
        )

        result = sandbox_config.load_config(fake_repo)

        assert (
            result.model_dump()
            == sandbox_config.SandboxConfig(
                mounts=["/shared:/shared:ro", "/local:/local:rw"],
                environment=["MODE=shared", "MODE=local", "TOKEN"],
                follow_symlinks=True,
                s3={
                    "default": "development",
                    "profiles": {"development": {}, "production": {}},
                },
            ).model_dump()
        )

    def test_loads_local_overlay_from_main_checkout(self, fake_repo, tmp_path):
        self._write_config(fake_repo, "schema_version: 1\nmounts: ['/shared:/shared']\n")
        main_hatchery = tmp_path / "main-hatchery"
        main_hatchery.mkdir()
        (main_hatchery / "docker.local.yaml").write_text("schema_version: 1\nmounts: ['/local:/local']\n")

        result = sandbox_config.load_config(fake_repo, local_hatchery_dir=main_hatchery)

        assert result.mounts == ["/shared:/shared", "/local:/local"]

    def test_invalid_local_overlay_reports_local_path(self, fake_repo, capsys):
        self._write_config(fake_repo, "schema_version: 1\n")
        local = fake_repo / "docker.local.yaml"
        local.write_text("unknown: true\n")

        with pytest.raises(SystemExit):
            sandbox_config.load_config(fake_repo)

        assert str(local) in capsys.readouterr().err

    def test_exits_on_invalid_yaml(self, fake_repo):
        self._write_config(fake_repo, "[unclosed\n")
        with pytest.raises(SystemExit):
            sandbox_config.load_config(fake_repo)

    def test_exits_on_invalid_mount_format(self, fake_repo):
        self._write_config(fake_repo, 'schema_version: 1\nmounts:\n  - "badformat"\n')
        with pytest.raises(SystemExit):
            sandbox_config.load_config(fake_repo)

    def test_exits_on_invalid_mode(self, fake_repo):
        self._write_config(fake_repo, 'schema_version: 1\nmounts:\n  - "/host:/container:rx"\n')
        with pytest.raises(SystemExit):
            sandbox_config.load_config(fake_repo)

    def test_exits_on_extra_keys(self, fake_repo):
        self._write_config(fake_repo, "schema_version: 1\nmounts: []\nextra_key: oops\n")
        with pytest.raises(SystemExit):
            sandbox_config.load_config(fake_repo)

    def test_error_message_mentions_config_file(self, fake_repo, capsys):
        self._write_config(fake_repo, 'schema_version: 1\nmounts:\n  - "badformat"\n')
        with pytest.raises(SystemExit):
            sandbox_config.load_config(fake_repo)
        assert "docker.yaml" in capsys.readouterr().err


def _loaded_config_data(data: dict) -> dict:
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "docker.yaml").write_text(json.dumps(data))
        return sandbox_config.load_config(root).model_dump()
