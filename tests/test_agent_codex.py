"""Unit tests for CodexBackend."""

import json

import pytest

import seekr_hatchery.agent as agent

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestCodexBackendConstants:
    def test_constants(self):
        assert agent.CODEX.kind == "CODEX"
        assert agent.CODEX.binary == "codex"
        assert agent.CODEX.supports_sessions is False


# ---------------------------------------------------------------------------
# build_new_command
# ---------------------------------------------------------------------------


class TestBuildNewCommand:
    def test_combines_prompts(self):
        cmd = agent.CODEX.build_new_command("sid", "sys", "initial")
        assert cmd == ["codex", "--dangerously-bypass-approvals-and-sandbox", "sys\n\ninitial"]

    def test_docker_flag_has_no_effect(self):
        native = agent.CODEX.build_new_command("sid", "sys", "initial")
        docker = agent.CODEX.build_new_command("sid", "sys", "initial", docker=True, workdir="/w")
        assert native == docker


# ---------------------------------------------------------------------------
# build_resume_command
# ---------------------------------------------------------------------------


class TestBuildResumeCommand:
    def test_uses_initial_prompt_as_context(self):
        # session_id is unused; initial_prompt provides task context
        cmd = agent.CODEX.build_resume_command("sid-ignored", "sys", "ctx")
        assert cmd == ["codex", "--dangerously-bypass-approvals-and-sandbox", "sys\n\nctx"]


# ---------------------------------------------------------------------------
# build_finalize_command
# ---------------------------------------------------------------------------


class TestBuildFinalizeCommand:
    def test_uses_wrap_up_prompt(self):
        cmd = agent.CODEX.build_finalize_command("sid-ignored", "sys", "wrap up")
        assert cmd == ["codex", "--dangerously-bypass-approvals-and-sandbox", "wrap up"]


# ---------------------------------------------------------------------------
# get_api_key
# ---------------------------------------------------------------------------


class TestGetApiKey:
    def test_returns_env_var(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "openai-key-123")
        assert agent.CODEX.get_api_key() == "openai-key-123"

    def test_returns_none_when_not_set(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert agent.CODEX.get_api_key() is None

    def test_reads_api_key_from_auth_json(self, home, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        (home / ".codex").mkdir()
        (home / ".codex" / "auth.json").write_text(
            json.dumps({"auth_mode": "api_key", "OPENAI_API_KEY": "key-from-file", "tokens": None})
        )
        assert agent.CODEX.get_api_key() == "key-from-file"

    def test_falls_back_to_oauth_access_token(self, home, monkeypatch):
        # OAuth tokens are routed to chatgpt.com/backend-api/codex by the proxy.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        (home / ".codex").mkdir()
        (home / ".codex" / "auth.json").write_text(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "OPENAI_API_KEY": None,
                    "tokens": {"access_token": "oauth-access-token", "refresh_token": "r"},
                }
            )
        )
        assert agent.CODEX.get_api_key() == "oauth-access-token"

    def test_env_var_takes_priority_over_auth_json(self, home, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        (home / ".codex").mkdir()
        (home / ".codex" / "auth.json").write_text(
            json.dumps({"auth_mode": "api_key", "OPENAI_API_KEY": "file-key", "tokens": None})
        )
        assert agent.CODEX.get_api_key() == "env-key"


# ---------------------------------------------------------------------------
# proxy_kwargs
# ---------------------------------------------------------------------------


class TestProxyKwargs:
    def test_apikey_mode(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert agent.CODEX.proxy_kwargs() == {
            "target_host": "api.openai.com",
            "inject_header": "authorization",
        }

    def test_oauth_mode(self, home, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        (home / ".codex").mkdir()
        (home / ".codex" / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": None, "tokens": {"access_token": "oauth-tok", "refresh_token": "r"}})
        )
        assert agent.CODEX.proxy_kwargs() == {
            "target_host": "chatgpt.com",
            "inject_header": "authorization",
            "path_prefix": "/backend-api/codex",
        }


# ---------------------------------------------------------------------------
# container_env
# ---------------------------------------------------------------------------


class TestContainerEnv:
    def test_apikey_mode(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert agent.CODEX.container_env("tok", 9999) == {
            "OPENAI_API_KEY": "tok",
            "OPENAI_BASE_URL": "http://host.docker.internal:9999/v1",
        }

    def test_oauth_mode(self, home, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        (home / ".codex").mkdir()
        (home / ".codex" / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": None, "tokens": {"access_token": "oauth-tok", "refresh_token": "r"}})
        )
        assert agent.CODEX.container_env("tok", 9999) == {
            "OPENAI_API_KEY": "tok",
            "OPENAI_BASE_URL": "http://host.docker.internal:9999",
        }


# ---------------------------------------------------------------------------
# _detect_auth_source
# ---------------------------------------------------------------------------


class TestDetectAuthSource:
    def test_env_var_returns_api_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert agent.CodexBackend._detect_auth_source() == "API_KEY"

    def test_auth_json_api_key_returns_api_key(self, home, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        (home / ".codex").mkdir()
        (home / ".codex" / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "sk-file", "tokens": None}))
        assert agent.CodexBackend._detect_auth_source() == "API_KEY"

    def test_auth_json_oauth_returns_oauth(self, home, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        (home / ".codex").mkdir()
        (home / ".codex" / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": None, "tokens": {"access_token": "oauth-tok", "refresh_token": "r"}})
        )
        assert agent.CodexBackend._detect_auth_source() == "OAUTH"

    def test_no_credentials_returns_none(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert agent.CodexBackend._detect_auth_source() is None

    def test_env_var_takes_priority_over_oauth(self, home, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        (home / ".codex").mkdir()
        (home / ".codex" / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": None, "tokens": {"access_token": "oauth-tok", "refresh_token": "r"}})
        )
        assert agent.CodexBackend._detect_auth_source() == "API_KEY"


# ---------------------------------------------------------------------------
# on_new_task
# ---------------------------------------------------------------------------


class TestOnNewTask:
    def test_is_noop(self, tmp_path):
        session_dir = tmp_path / "session"
        agent.CODEX.on_new_task(session_dir)  # should not raise or create files
        assert not session_dir.exists()


# ---------------------------------------------------------------------------
# on_before_container_start
# ---------------------------------------------------------------------------


class TestOnBeforeContainerStart:
    def test_writes_fake_auth_json(self, tmp_path):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        agent.CODEX.on_before_container_start(session_dir, "proxy-tok", "/workdir")
        data = json.loads((session_dir / "codex_auth.json").read_text())
        assert data == {"auth_mode": "apikey", "OPENAI_API_KEY": "proxy-tok", "tokens": None}

    def test_overwrites_on_subsequent_calls(self, tmp_path):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        agent.CODEX.on_before_container_start(session_dir, "token-1", "/workdir")
        agent.CODEX.on_before_container_start(session_dir, "token-2", "/workdir")
        data = json.loads((session_dir / "codex_auth.json").read_text())
        assert data["OPENAI_API_KEY"] == "token-2"


# ---------------------------------------------------------------------------
# home_mounts
# ---------------------------------------------------------------------------


class TestHomeMounts:
    def test_returns_expected_mounts(self, home, tmp_path):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        fake_auth = session_dir / "codex_auth.json"
        fake_auth.write_text("{}")
        assert agent.CODEX.home_mounts(session_dir) == [
            f"{home / '.codex'}:{agent.CONTAINER_HOME}/.codex:rw",
            f"{fake_auth}:{agent.CONTAINER_HOME}/.codex/auth.json:rw",
        ]

    def test_raises_if_fake_auth_missing(self, tmp_path):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        with pytest.raises(RuntimeError, match="codex_auth.json not found"):
            agent.CODEX.home_mounts(session_dir)


# ---------------------------------------------------------------------------
# on_before_launch
# ---------------------------------------------------------------------------


class TestOnBeforeLaunch:
    def test_is_noop(self, tmp_path):
        agent.CODEX.on_before_launch(tmp_path)  # should not raise


# ---------------------------------------------------------------------------
# tmpfs_paths
# ---------------------------------------------------------------------------


class TestTmpfsPaths:
    def test_returns_empty_list(self):
        assert agent.CODEX.tmpfs_paths() == []


# ---------------------------------------------------------------------------
# dockerfile_install
# ---------------------------------------------------------------------------


class TestDockerfileInstall:
    def test_dockerfile_install(self):
        snippet = agent.CODEX.dockerfile_install
        assert "npm" in snippet
        assert "@openai/codex" in snippet


# ---------------------------------------------------------------------------
# api_key_missing_hint
# ---------------------------------------------------------------------------


class TestApiKeyMissingHint:
    def test_api_key_missing_hint(self):
        hint = agent.CODEX.api_key_missing_hint
        assert "OPENAI_API_KEY" in hint
        assert "codex login" in hint
