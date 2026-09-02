"""Unit tests for repo_config.py — RepoConfigModel, migration, load_repo_config."""

import pytest

import seekr_hatchery.repo_config as repo_config


class TestRepoConfigModelDefaults:
    def test_defaults(self):
        assert repo_config.RepoConfigModel().model_dump() == {
            "schema_version": "1",
            "auto_commit": None,
        }


class TestMigrate:
    def test_v0_migrates_to_v1(self):
        assert repo_config._migrate({"auto_commit": True}) == {
            "schema_version": "1",
            "auto_commit": True,
        }

    def test_v1_is_idempotent(self):
        data = {"schema_version": "1", "auto_commit": False}
        assert repo_config._migrate(data) == {"schema_version": "1", "auto_commit": False}


class TestLoadRepoConfig:
    def test_missing_file_returns_defaults(self, tmp_path):
        cfg = repo_config.load_repo_config(tmp_path)
        assert cfg.auto_commit is None

    def test_valid_file_with_auto_commit_true(self, tmp_path):
        (tmp_path / ".hatchery.yaml").write_text("auto_commit: true\n")
        cfg = repo_config.load_repo_config(tmp_path)
        assert cfg.auto_commit is True

    def test_valid_file_with_auto_commit_false(self, tmp_path):
        (tmp_path / ".hatchery.yaml").write_text("auto_commit: false\n")
        cfg = repo_config.load_repo_config(tmp_path)
        assert cfg.auto_commit is False

    def test_v0_file_is_migrated(self, tmp_path):
        (tmp_path / ".hatchery.yaml").write_text("auto_commit: false\n")
        cfg = repo_config.load_repo_config(tmp_path)
        assert cfg.schema_version == "1"

    def test_invalid_yaml_exits(self, tmp_path, capsys):
        (tmp_path / ".hatchery.yaml").write_text("auto_commit: [unterminated\n")
        with pytest.raises(SystemExit) as exc_info:
            repo_config.load_repo_config(tmp_path)
        assert exc_info.value.code == 1
        assert ".hatchery.yaml" in capsys.readouterr().err

    def test_invalid_schema_exits(self, tmp_path, capsys):
        (tmp_path / ".hatchery.yaml").write_text("auto_commit: not-a-bool\n")
        with pytest.raises(SystemExit) as exc_info:
            repo_config.load_repo_config(tmp_path)
        assert exc_info.value.code == 1
        assert ".hatchery.yaml" in capsys.readouterr().err
