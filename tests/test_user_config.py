"""Unit tests for user_config.py — UserConfigModel, migration, UserConfig."""

import pytest
import yaml

import seekr_hatchery.agents as agent
import seekr_hatchery.user_config as user_config

# ---------------------------------------------------------------------------
# UserConfigModel defaults
# ---------------------------------------------------------------------------


class TestUserConfigModelDefaults:
    def test_defaults(self):
        assert user_config.UserConfigModel().model_dump() == {
            "schema_version": "1",
            "default_agent": None,
            "open_editor": False,
            "auto_commit": True,
        }

    def test_invalid_schema_version_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            user_config.UserConfigModel(schema_version="42")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


class TestMigrate:
    def test_v0_migrates_to_v1(self):
        assert user_config._migrate({"default_agent": "CODEX"}) == {
            "schema_version": "1",
            "default_agent": "CODEX",
        }

    def test_v1_is_idempotent(self):
        data = {"schema_version": "1", "default_agent": "CODEX"}
        assert user_config._migrate(data) == {"schema_version": "1", "default_agent": "CODEX"}


# ---------------------------------------------------------------------------
# UserConfig.load
# ---------------------------------------------------------------------------


class TestUserConfigLoad:
    def test_missing_file_returns_defaults(self, tmp_path):
        cfg = user_config.UserConfig.load(tmp_path / "config.yaml")
        assert cfg.schema_version == "1"
        assert cfg.default_agent is None

    def test_valid_file_is_loaded(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({"schema_version": "1", "default_agent": "CODEX"}))
        cfg = user_config.UserConfig.load(path)
        assert cfg.schema_version == "1"
        assert cfg.default_agent == "CODEX"

    def test_corrupt_yaml_returns_defaults(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("not valid yaml: [{{")
        cfg = user_config.UserConfig.load(path)
        assert cfg.schema_version == "1"
        assert cfg.default_agent is None

    def test_v0_file_is_migrated(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({"default_agent": "CODEX"}))
        cfg = user_config.UserConfig.load(path)
        assert cfg.schema_version == "1"
        assert cfg.default_agent == "CODEX"

    def test_legacy_json_config_is_migrated(self, tmp_path, monkeypatch):
        legacy_path = tmp_path / "config.json"
        new_path = tmp_path / "config.yaml"
        legacy_path.write_text(yaml.dump({"schema_version": "1", "default_agent": "CODEX"}))
        monkeypatch.setattr(user_config.UserConfig, "CONFIG_PATH", new_path)
        monkeypatch.setattr(user_config.UserConfig, "_LEGACY_CONFIG_PATH", legacy_path)

        cfg = user_config.UserConfig.load()

        assert cfg.default_agent == "CODEX"
        assert new_path.exists()
        assert not legacy_path.exists()
        assert yaml.safe_load(new_path.read_text())["default_agent"] == "CODEX"


# ---------------------------------------------------------------------------
# UserConfig.save
# ---------------------------------------------------------------------------


class TestUserConfigSave:
    def test_creates_file_and_parent_dirs(self, tmp_path):
        path = tmp_path / "a" / "b" / "c" / "config.yaml"
        user_config.UserConfig.load(path).save()
        assert path.exists()

    def test_round_trip(self, tmp_path):
        path = tmp_path / "config.yaml"
        cfg = user_config.UserConfig.load(path)
        cfg.set_default_agent("CODEX")
        cfg.save()
        reloaded = user_config.UserConfig.load(path)
        assert reloaded.default_agent == "CODEX"
        assert reloaded.schema_version == "1"


# ---------------------------------------------------------------------------
# set_default_agent — mutates in memory only
# ---------------------------------------------------------------------------


class TestSetDefaultAgent:
    def test_sets_value_in_memory(self, tmp_path):
        cfg = user_config.UserConfig.load(tmp_path / "config.yaml")
        cfg.set_default_agent("CODEX")
        assert cfg.default_agent == "CODEX"


# ---------------------------------------------------------------------------
# set_open_editor — mutates in memory only
# ---------------------------------------------------------------------------


class TestSetOpenEditor:
    def test_sets_value_in_memory(self, tmp_path):
        cfg = user_config.UserConfig.load(tmp_path / "config.yaml")
        assert cfg.open_editor is False
        cfg.set_open_editor(True)
        assert cfg.open_editor is True

    def test_round_trip(self, tmp_path):
        path = tmp_path / "config.yaml"
        cfg = user_config.UserConfig.load(path)
        cfg.set_open_editor(True)
        cfg.save()
        reloaded = user_config.UserConfig.load(path)
        assert reloaded.open_editor is True

    def test_load_from_file(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({"schema_version": "1", "open_editor": True}))
        cfg = user_config.UserConfig.load(path)
        assert cfg.open_editor is True


# ---------------------------------------------------------------------------
# validate_config_file
# ---------------------------------------------------------------------------


class TestValidateConfigFile:
    def test_valid_file(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("schema_version: '1'\ndefault_agent: null\n")
        assert user_config.validate_config_file(path) is None

    def test_invalid_yaml(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("not yaml: [{{")
        result = user_config.validate_config_file(path)
        assert result is not None
        assert "Invalid YAML" in result

    def test_invalid_schema_version(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("schema_version: '99'\n")
        result = user_config.validate_config_file(path)
        assert result is not None

    def test_unknown_key_rejected(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("schema_version: '1'\ntypo_key: true\n")
        result = user_config.validate_config_file(path)
        assert result is not None
        assert "typo_key" in result

    def test_v0_file_passes_after_migration(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("default_agent: CODEX\n")
        assert user_config.validate_config_file(path) is None

    def test_load_ignores_unknown_keys(self, tmp_path):
        """Normal load stays permissive for forward compatibility."""
        path = tmp_path / "config.yaml"
        path.write_text("schema_version: '1'\nfuture_field: 42\n")
        cfg = user_config.UserConfig.load(path)
        assert cfg.schema_version == "1"


# ---------------------------------------------------------------------------
# resolve_backend — explicit agent_name
# ---------------------------------------------------------------------------


class TestResolveBackendExplicit:
    def test_known_backends(self, tmp_path):
        cfg = user_config.UserConfig.load(tmp_path / "config.yaml")
        assert cfg.resolve_backend("codex") is agent.CODEX
        assert cfg.resolve_backend("CODEX") is agent.CODEX  # case-insensitive

    def test_unknown_raises(self, tmp_path):
        cfg = user_config.UserConfig.load(tmp_path / "config.yaml")
        with pytest.raises(ValueError, match="unknown agent"):
            cfg.resolve_backend("gpt-engineer")


# ---------------------------------------------------------------------------
# resolve_backend — auto-detection
# ---------------------------------------------------------------------------


class TestResolveBackendAutoDetect:
    def test_single_detected_returns_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr("seekr_hatchery.user_config._detect_installed", lambda _: [agent.CODEX])
        cfg = user_config.UserConfig.load(tmp_path / "config.yaml")
        assert cfg.resolve_backend(None) is agent.CODEX

    def test_zero_detected_returns_codex_without_saving(self, tmp_path, monkeypatch):
        path = tmp_path / "config.yaml"
        monkeypatch.setattr("seekr_hatchery.user_config._detect_installed", lambda _: [])
        result = user_config.UserConfig.load(path).resolve_backend(None)
        assert result is agent.CODEX
        assert not path.exists()


# ---------------------------------------------------------------------------
# set_auto_commit — mutates in memory only
# ---------------------------------------------------------------------------


class TestSetAutoCommit:
    def test_defaults_to_true(self, tmp_path):
        cfg = user_config.UserConfig.load(tmp_path / "config.yaml")
        assert cfg.auto_commit is True

    def test_sets_value_in_memory(self, tmp_path):
        cfg = user_config.UserConfig.load(tmp_path / "config.yaml")
        assert cfg.auto_commit is True
        cfg.set_auto_commit(False)
        assert cfg.auto_commit is False

    def test_round_trip(self, tmp_path):
        path = tmp_path / "config.yaml"
        cfg = user_config.UserConfig.load(path)
        cfg.set_auto_commit(False)
        cfg.save()
        reloaded = user_config.UserConfig.load(path)
        assert reloaded.auto_commit is False

    def test_load_from_file(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({"schema_version": "1", "auto_commit": False}))
        cfg = user_config.UserConfig.load(path)
        assert cfg.auto_commit is False

    def test_validate_accepts_auto_commit(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("schema_version: '1'\nauto_commit: false\n")
        assert user_config.validate_config_file(path) is None
