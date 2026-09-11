"""Unit tests for repo_config.py — RepoConfigModel, migration, load_repo_config."""

from pathlib import Path

import pytest

import seekr_hatchery.constants as constants
import seekr_hatchery.repo_config as repo_config
import seekr_hatchery.user_config as user_config


def _write_config(repo: Path, text: str) -> Path:
    """Write repo config at its canonical path, creating .hatchery/ as needed."""
    path = repo / constants.REPO_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class TestRepoConfigModelDefaults:
    def test_defaults(self):
        assert repo_config.RepoConfigModel().model_dump() == {
            "default_agent": None,
            "open_editor": None,
            "auto_commit": None,
        }


class TestMigrate:
    def test_strips_legacy_schema_version(self):
        assert repo_config._migrate({"schema_version": "1", "auto_commit": True}) == {
            "auto_commit": True,
        }

    def test_no_op_without_schema_version(self):
        assert repo_config._migrate({"auto_commit": False}) == {"auto_commit": False}


class TestLoadRepoConfig:
    def test_missing_file_returns_defaults(self, tmp_path):
        cfg = repo_config.load_repo_config(tmp_path)
        assert cfg.auto_commit is None

    def test_valid_file_with_auto_commit_true(self, tmp_path):
        _write_config(tmp_path, "auto_commit: true\n")
        cfg = repo_config.load_repo_config(tmp_path)
        assert cfg.auto_commit is True

    def test_valid_file_with_auto_commit_false(self, tmp_path):
        _write_config(tmp_path, "auto_commit: false\n")
        cfg = repo_config.load_repo_config(tmp_path)
        assert cfg.auto_commit is False

    def test_legacy_schema_version_is_stripped(self, tmp_path):
        _write_config(tmp_path, "schema_version: '1'\nauto_commit: false\n")
        cfg = repo_config.load_repo_config(tmp_path)
        assert cfg.auto_commit is False

    def test_invalid_yaml_exits(self, tmp_path, capsys):
        _write_config(tmp_path, "auto_commit: [unterminated\n")
        with pytest.raises(SystemExit) as exc_info:
            repo_config.load_repo_config(tmp_path)
        assert exc_info.value.code == 1
        assert constants.REPO_CONFIG in capsys.readouterr().err

    def test_invalid_schema_exits(self, tmp_path, capsys):
        _write_config(tmp_path, "auto_commit: not-a-bool\n")
        with pytest.raises(SystemExit) as exc_info:
            repo_config.load_repo_config(tmp_path)
        assert exc_info.value.code == 1
        assert constants.REPO_CONFIG in capsys.readouterr().err


class TestValidateConfigFile:
    def test_valid_returns_none(self, tmp_path):
        path = _write_config(tmp_path, "auto_commit: true\n")
        assert repo_config.validate_config_file(path) is None

    def test_legacy_schema_version_still_valid(self, tmp_path):
        """Pre-removal files carrying schema_version pass — migration strips it."""
        path = _write_config(tmp_path, "schema_version: '1'\nauto_commit: true\n")
        assert repo_config.validate_config_file(path) is None

    def test_unknown_key_returns_error(self, tmp_path):
        path = _write_config(tmp_path, "bogus_key: 1\n")
        assert repo_config.validate_config_file(path) is not None

    def test_wrong_type_returns_error(self, tmp_path):
        path = _write_config(tmp_path, "auto_commit: not-a-bool\n")
        assert repo_config.validate_config_file(path) is not None

    def test_invalid_yaml_returns_error(self, tmp_path):
        path = _write_config(tmp_path, "auto_commit: [unterminated\n")
        assert "Invalid YAML" in repo_config.validate_config_file(path)


class TestCreateRepoConfig:
    def test_creates_valid_commented_template(self, tmp_path):
        path = repo_config.create_repo_config(tmp_path)
        assert path == tmp_path / constants.REPO_CONFIG
        # Template is valid and leaves every override unset (commented out).
        assert repo_config.validate_config_file(path) is None
        assert repo_config.load_repo_config(tmp_path).model_dump() == {f: None for f in repo_config._OVERRIDE_FIELDS}

    def test_template_lists_every_overridable_field(self, tmp_path):
        """Guard against the template drifting out of sync with the model."""
        text = repo_config.create_repo_config(tmp_path).read_text()
        for field in repo_config._OVERRIDE_FIELDS:
            assert f"# {field}:" in text


class TestLoadEffectiveConfig:
    def test_no_repo_config_uses_global(self, tmp_path, monkeypatch):
        monkeypatch.setattr(user_config.UserConfig, "CONFIG_PATH", tmp_path / "global.yaml")
        user_config.UserConfig.load().save()  # global defaults on disk
        cfg = repo_config.load_effective_config(tmp_path)
        assert cfg.auto_commit is True  # global default
        assert cfg.open_editor is False

    def test_repo_overrides_layer_over_global(self, tmp_path, monkeypatch):
        monkeypatch.setattr(user_config.UserConfig, "CONFIG_PATH", tmp_path / "global.yaml")
        user_config.UserConfig.load().save()
        _write_config(tmp_path, "default_agent: CLAUDE\nopen_editor: true\nauto_commit: false\n")
        cfg = repo_config.load_effective_config(tmp_path)
        assert cfg.default_agent == "CLAUDE"
        assert cfg.open_editor is True
        assert cfg.auto_commit is False

    def test_unset_repo_fields_inherit_global(self, tmp_path, monkeypatch):
        monkeypatch.setattr(user_config.UserConfig, "CONFIG_PATH", tmp_path / "global.yaml")
        user_config.UserConfig.load().save()
        _write_config(tmp_path, "auto_commit: false\n")
        cfg = repo_config.load_effective_config(tmp_path)
        assert cfg.auto_commit is False  # overridden
        assert cfg.open_editor is False  # inherited global default


class TestResolveNoCommit:
    def _cfg(self, tmp_path, monkeypatch, auto_commit):
        monkeypatch.setattr(user_config.UserConfig, "CONFIG_PATH", tmp_path / "global.yaml")
        cfg = user_config.UserConfig.load()
        cfg.set_auto_commit(auto_commit)
        return cfg

    def test_flag_wins_over_config(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch, auto_commit=True)
        assert repo_config.resolve_no_commit(cfg, commit=False) is True

    def test_falls_back_to_config_when_no_flag(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch, auto_commit=False)
        assert repo_config.resolve_no_commit(cfg, commit=None) is True
