"""Unit tests for CodexBackend."""

import json
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import seekr_hatchery.agents as agent
import seekr_hatchery.agents.codex as codex_backend
import seekr_hatchery.mount as mount
from seekr_hatchery.models import SessionMeta

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestCodexBackendConstants:
    def test_constants(self):
        assert agent.CODEX.kind == "CODEX"
        assert agent.CODEX.binary == "codex"
        assert agent.CODEX.supports_sessions is True


# ---------------------------------------------------------------------------
# build_new_command
# ---------------------------------------------------------------------------


class TestBuildNewCommand:
    def test_combines_prompts_native(self):
        # Non-docker: direct codex invocation
        cmd = agent.CODEX.build_new_command("sid", "sys", "initial")
        assert cmd == ["codex", "--dangerously-bypass-approvals-and-sandbox", "sys\n\ninitial"]

    def test_docker_uses_proxy_wrapper(self):
        # Docker: sh -c wrapper injects openai_base_url via --config so the proxy
        # is used (codex ignores the OPENAI_BASE_URL env var directly).
        cmd = agent.CODEX.build_new_command("sid", "sys", "initial", docker=True)
        assert cmd[0] == "sh"
        assert cmd[1] == "-c"
        # Wrapper script must inject openai_base_url and call codex
        assert "openai_base_url" in cmd[2]
        assert "--config" in cmd[2]
        assert "codex" in cmd[2]
        # Prompt is passed as a positional arg to sh -c ("$@")
        assert cmd[-1] == "sys\n\ninitial"

    def test_workdir_has_no_effect(self):
        cmd1 = agent.CODEX.build_new_command("sid", "sys", "initial", docker=True)
        cmd2 = agent.CODEX.build_new_command("sid", "sys", "initial", docker=True, workdir="/w")
        assert cmd1 == cmd2

    def test_docker_disables_update_check(self):
        # The interactive "Update available" prompt would otherwise block
        # resume launches while codex waits for the user to press enter.
        cmd = agent.CODEX.build_new_command("sid", "sys", "initial", docker=True)
        assert "check_for_update_on_startup=false" in cmd[2]


# ---------------------------------------------------------------------------
# build_resume_command
# ---------------------------------------------------------------------------


class TestBuildResumeCommand:
    def test_native_with_session_id_resumes(self):
        cmd = agent.CODEX.build_resume_command("sid-123", "sys", "ctx")
        assert cmd == ["codex", "--dangerously-bypass-approvals-and-sandbox", "resume", "sid-123"]

    def test_native_without_session_id_falls_back_to_fresh_prompt(self):
        # Native + no sid is the defensive path (cli.py bails first).
        cmd = agent.CODEX.build_resume_command("", "sys", "ctx")
        assert cmd == ["codex", "--dangerously-bypass-approvals-and-sandbox", "sys\n\nctx"]

    def test_docker_with_session_id_resumes(self):
        cmd = agent.CODEX.build_resume_command("sid-123", "sys", "ctx", docker=True)
        assert cmd[0] == "sh"
        assert "openai_base_url" in cmd[2]
        assert cmd[-2:] == ["resume", "sid-123"]

    def test_docker_without_session_id_uses_last(self):
        cmd = agent.CODEX.build_resume_command("", "sys", "ctx", docker=True)
        assert cmd[0] == "sh"
        assert cmd[-2:] == ["resume", "--last"]


# ---------------------------------------------------------------------------
# build_finalize_command
# ---------------------------------------------------------------------------


class TestBuildFinalizeCommand:
    def test_native_with_session_id_execs_resume(self):
        cmd = agent.CODEX.build_finalize_command("sid-123", "sys", "wrap up")
        assert cmd == [
            "codex",
            "--dangerously-bypass-approvals-and-sandbox",
            "exec",
            "resume",
            "sid-123",
            "wrap up",
        ]

    def test_native_without_session_id_execs_fresh(self):
        cmd = agent.CODEX.build_finalize_command("", "sys", "wrap up")
        assert cmd == ["codex", "--dangerously-bypass-approvals-and-sandbox", "exec", "wrap up"]

    def test_docker_with_session_id_execs_resume(self):
        cmd = agent.CODEX.build_finalize_command("sid-123", "sys", "wrap up", docker=True)
        assert cmd[0] == "sh"
        assert "openai_base_url" in cmd[2]
        assert cmd[-4:] == ["exec", "resume", "sid-123", "wrap up"]

    def test_docker_without_session_id_uses_last(self):
        cmd = agent.CODEX.build_finalize_command("", "sys", "wrap up", docker=True)
        assert cmd[0] == "sh"
        assert cmd[-4:] == ["exec", "resume", "--last", "wrap up"]


# ---------------------------------------------------------------------------
# proxy_kwargs
# ---------------------------------------------------------------------------


class TestProxyKwargs:
    def test_apikey_mode(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert agent.CODEX.proxy_kwargs() == {
            "target_host": "api.openai.com",
        }
        assert "inject_header" not in agent.CODEX.proxy_kwargs()

    def test_oauth_mode(self, home, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        (home / ".codex").mkdir()
        (home / ".codex" / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": None, "tokens": {"access_token": "oauth-tok", "refresh_token": "r"}})
        )
        assert agent.CODEX.proxy_kwargs() == {
            "target_host": "chatgpt.com",
            "path_prefix": "/backend-api/codex",
        }
        assert "inject_header" not in agent.CODEX.proxy_kwargs()


# ---------------------------------------------------------------------------
# make_header_mutator
# ---------------------------------------------------------------------------


class TestMakeHeaderMutator:
    def test_injects_bearer(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
        mutator = agent.CODEX.make_header_mutator()
        result = mutator({})
        assert result.get("Authorization") == "Bearer sk-test-123"
        assert "x-api-key" not in {k.lower() for k in result}

    def test_raises_when_no_credentials(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="no API token found"):
            agent.CODEX.make_header_mutator()

    def test_strips_inbound_auth_headers(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "real-key")
        mutator = agent.CODEX.make_header_mutator()
        result = mutator(
            {"x-api-key": "proxy-tok", "authorization": "Bearer proxy-tok", "content-type": "application/json"}
        )
        assert result.get("Authorization") == "Bearer real-key"
        assert result.get("content-type") == "application/json"
        lower_keys = {k.lower() for k in result}
        assert lower_keys.issuperset({"authorization"})
        # Only one authorization header (the real one), not the proxy one
        assert result.get("Authorization") == "Bearer real-key"


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

    def test_chatgpt_auth_mode_ignores_env_var(self, home, monkeypatch):
        # auth_mode="chatgpt" means the user explicitly logged in via OAuth —
        # any OPENAI_API_KEY env var is a stale override and must be ignored.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-stale")
        (home / ".codex").mkdir()
        (home / ".codex" / "auth.json").write_text(
            json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": {"access_token": "oauth-tok"}})
        )
        assert agent.CodexBackend._detect_auth_source() == "OAUTH"

    def test_oauth_auth_mode_ignores_env_var(self, home, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-stale")
        (home / ".codex").mkdir()
        (home / ".codex" / "auth.json").write_text(
            json.dumps({"auth_mode": "oauth", "OPENAI_API_KEY": None, "tokens": {"access_token": "oauth-tok"}})
        )
        assert agent.CodexBackend._detect_auth_source() == "OAUTH"

    def test_chatgpt_auth_mode_no_tokens_returns_none(self, home, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        (home / ".codex").mkdir()
        (home / ".codex" / "auth.json").write_text(
            json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": None})
        )
        assert agent.CodexBackend._detect_auth_source() is None


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
    """No longer does anything — auth.json synthesis moved into the
    VolumeMount.seed callable. Method stays as a required abstract
    method on the base."""

    def test_is_noop(self, tmp_path):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        agent.CODEX.on_before_container_start(session_dir, "proxy-tok", "/workdir")
        assert list(session_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# construct_mounts
# ---------------------------------------------------------------------------


def _kinds_by_dst(mounts):
    return {m.dst: m for m in mounts}


class TestConstructMounts:
    def test_volume_always_present(self, home, tmp_path):
        """The ~/.codex/ seeded volume is always returned — independent
        of host file existence."""
        mounts = agent.CODEX.construct_mounts(tmp_path)
        by_dst = _kinds_by_dst(mounts)
        v = by_dst[f"{agent.CONTAINER_HOME}/.codex"]
        assert isinstance(v, mount.VolumeMount)
        assert v.name == "codex-dir"
        assert v.is_file is False
        assert v.seed is agent.CodexBackend._seed_codex_dir

    def test_no_binds_when_host_missing(self, home, tmp_path):
        mounts = agent.CODEX.construct_mounts(tmp_path)
        assert all(isinstance(m, mount.VolumeMount) for m in mounts)

    def test_memories_and_skills_bind_rw_when_present(self, home, tmp_path):
        """memories/ and skills/ are user-owned state that persists
        across tasks — RW so in-container edits propagate to the host."""
        (home / ".codex" / "memories").mkdir(parents=True)
        (home / ".codex" / "skills").mkdir(parents=True)
        mounts = agent.CODEX.construct_mounts(tmp_path)
        by_dst = _kinds_by_dst(mounts)
        for name in ("memories", "skills"):
            m = by_dst[f"{agent.CONTAINER_HOME}/.codex/{name}"]
            assert isinstance(m, mount.BindMount)
            assert m.mode == "RW"
            assert m.src == home / ".codex" / name

    def test_cross_task_files_bind_when_present(self, home, tmp_path):
        (home / ".codex").mkdir()
        (home / ".codex" / "config.toml").write_text("")
        (home / ".codex" / "models_cache.json").write_text("{}")
        mounts = agent.CODEX.construct_mounts(tmp_path)
        by_dst = _kinds_by_dst(mounts)
        for name in ("config.toml", "models_cache.json"):
            m = by_dst[f"{agent.CONTAINER_HOME}/.codex/{name}"]
            assert isinstance(m, mount.BindMount)
            assert m.mode == "RW"
            assert m.src == home / ".codex" / name


# ---------------------------------------------------------------------------
# _seed_codex_dir — only auth.json is synthesised
# ---------------------------------------------------------------------------


class TestSeedCodexDir:
    def _ctx(self, token="proxy-tok"):
        return mount.SeedContext(
            session_dir=Path("/tmp/session"),
            proxy_token=token,
            container_workdir="/workspace",
        )

    def test_returns_only_auth_json(self, home):
        out = agent.CodexBackend._seed_codex_dir(self._ctx())
        assert set(out.keys()) == {"auth.json"}

    def test_auth_json_uses_proxy_token(self, home):
        out = agent.CodexBackend._seed_codex_dir(self._ctx(token="my-proxy-123"))
        data = json.loads(out["auth.json"])
        assert data == {"auth_mode": "apikey", "OPENAI_API_KEY": "my-proxy-123", "tokens": None}

    def test_apikey_mode_even_when_host_uses_oauth(self, home, monkeypatch):
        """In chatgpt mode, codex bypasses OPENAI_BASE_URL and goes
        directly to chatgpt.com — the proxy would never see the request.
        The in-container auth.json must always be ``apikey`` so codex
        routes through the proxy regardless of host config."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        (home / ".codex").mkdir()
        (home / ".codex" / "auth.json").write_text(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "OPENAI_API_KEY": None,
                    "tokens": {"access_token": "real-oauth-tok"},
                }
            )
        )
        out = agent.CodexBackend._seed_codex_dir(self._ctx(token="proxy-tok"))
        data = json.loads(out["auth.json"])
        assert data["auth_mode"] == "apikey"
        assert data["OPENAI_API_KEY"] == "proxy-tok"
        assert data["tokens"] is None


# ---------------------------------------------------------------------------
# on_before_launch
# ---------------------------------------------------------------------------


class TestOnBeforeLaunch:
    def test_is_noop(self, tmp_path):
        agent.CODEX.on_before_launch(tmp_path)  # should not raise


# ---------------------------------------------------------------------------
# dockerfile_install
# ---------------------------------------------------------------------------


class TestDockerfileInstall:
    def test_dockerfile_install(self):
        snippet = agent.CODEX.dockerfile_install
        assert "npm" in snippet
        assert "@openai/codex" in snippet


# ---------------------------------------------------------------------------
# format_image_reference
# ---------------------------------------------------------------------------


class TestFormatImageReference:
    def test_returns_raw_absolute_path(self):
        # Codex's TUI composer accepts a bare absolute path — no markup needed.
        assert agent.CODEX.format_image_reference(Path("/tmp/clip.png")) == "/tmp/clip.png"


# ---------------------------------------------------------------------------
# Custom-provider mode
# ---------------------------------------------------------------------------


def _make_custom_provider_config(
    home,
    *,
    provider: str = "dev-adapter",
    base_url: str = "https://adapter.example.com/v1",
    bearer: str = "real-bearer-1234",
    model: str = "some/Model",
    model_reasoning_effort: str = "high",
    wire_api: str = "responses",
    section_name: str = "Custom adapter",
) -> None:
    """Write a config.toml matching the custom-provider layout."""
    (home / ".codex").mkdir(exist_ok=True)
    (home / ".codex" / "config.toml").write_text(
        f"""\
model = "{model}"
model_provider = "{provider}"
model_reasoning_effort = "{model_reasoning_effort}"

[model_providers.{provider}]
name = "{section_name}"
wire_api = "{wire_api}"
base_url = "{base_url}"
experimental_bearer_token = "{bearer}"
"""
    )


class TestReadCustomProvider:
    def test_none_when_no_config(self, home):
        assert agent.CodexBackend._read_custom_provider() is None

    def test_returns_tuple_when_active_provider_has_bearer(self, home):
        _make_custom_provider_config(home)
        assert agent.CodexBackend._read_custom_provider() == (
            "dev-adapter",
            "https://adapter.example.com/v1",
            "real-bearer-1234",
        )

    def test_none_when_active_provider_missing_bearer(self, home):
        (home / ".codex").mkdir()
        (home / ".codex" / "config.toml").write_text(
            """model_provider = "openai"
[model_providers.openai]
base_url = "https://api.openai.com/v1"
"""
        )
        assert agent.CodexBackend._read_custom_provider() is None

    @pytest.mark.parametrize("bad_name", ["dev adapter", 'dev"adapter', "dev$adapter", ""])
    def test_invalid_provider_name_rejected(self, home, bad_name):
        if bad_name == "":
            (home / ".codex").mkdir()
            (home / ".codex" / "config.toml").write_text(
                """model_provider = ""
"""
            )
        else:
            _make_custom_provider_config(home, provider=bad_name)
        assert agent.CodexBackend._read_custom_provider() is None

    @pytest.mark.parametrize(
        "bad_path",
        [
            '/v1"; echo pwned',  # quote — would break the docker wrapper's shell-quoted --config
            "/v1$(echo pwned)",  # shell substitution
            "/v1 with space",  # space — breaks shell-word boundaries
            "/v1\nNEW",  # newline
        ],
    )
    def test_invalid_base_url_path_rejected(self, home, bad_path):
        """A provider whose base_url path contains characters that would
        break the _DOCKER_WRAPPER shell quoting is treated as
        not-configured (same as a malformed provider name)."""
        _make_custom_provider_config(home, base_url=f"https://adapter.example.com{bad_path}")
        assert agent.CodexBackend._read_custom_provider() is None

    def test_non_utf8_config_does_not_crash(self, home):
        """A non-UTF-8 byte in the host config.toml must not raise an
        uncaught UnicodeDecodeError — the function should fall through
        to None like any other unparseable config."""
        (home / ".codex").mkdir()
        # 0xFF is invalid as a stand-alone byte in UTF-8.
        (home / ".codex" / "config.toml").write_bytes(b'model_provider = "openai"\n# \xff invalid utf-8\n')
        # Must return None, not propagate UnicodeDecodeError.
        assert agent.CodexBackend._read_custom_provider() is None


class TestCustomProviderProxyKwargs:
    def test_returns_target_host(self, home):
        """The provider's URL path lives in OPENAI_BASE_URL (see
        container_env), not in path_prefix — putting it in both would
        double-prepend and 404 against the upstream.

        TLS validation uses the OS trust store (see proxy.api_server),
        so no CA-bundle kwarg is needed here."""
        _make_custom_provider_config(home)
        assert agent.CODEX.proxy_kwargs() == {"target_host": "adapter.example.com"}

    def test_http_base_url_works(self, home):
        """Plain-HTTP providers don't need TLS validation either way —
        same code path, no extra config."""
        _make_custom_provider_config(home, base_url="http://localhost:8000/v1")
        assert agent.CODEX.proxy_kwargs() == {"target_host": "localhost:8000"}

    def test_custom_provider_wins_over_oauth(self, home):
        """A host with both an OAuth auth.json and a custom provider config
        is treated as custom-provider — the explicit provider setup is the
        deliberate signal."""
        _make_custom_provider_config(home)
        (home / ".codex" / "auth.json").write_text(
            json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "oauth-tok"}})
        )
        kwargs = agent.CODEX.proxy_kwargs()
        assert kwargs["target_host"] == "adapter.example.com"


class TestCustomProviderMakeHeaderMutator:
    def test_injects_bearer_from_config(self, home, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        _make_custom_provider_config(home, bearer="real-bearer-1234")
        mutator = agent.CODEX.make_header_mutator()
        result = mutator({})
        assert result.get("Authorization") == "Bearer real-bearer-1234"

    def test_strips_inbound_auth_headers(self, home, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        _make_custom_provider_config(home, bearer="real-bearer-1234")
        mutator = agent.CODEX.make_header_mutator()
        result = mutator(
            {"x-api-key": "proxy-tok", "authorization": "Bearer proxy-tok", "content-type": "application/json"}
        )
        assert result.get("Authorization") == "Bearer real-bearer-1234"
        assert result.get("content-type") == "application/json"
        # Only one Authorization-like header (case-insensitive); never the proxy one
        lower = {k.lower(): v for k, v in result.items()}
        assert lower["authorization"] == "Bearer real-bearer-1234"
        assert "x-api-key" not in lower

    def test_refresh_kwarg_is_noop(self, home, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        _make_custom_provider_config(home, bearer="real-bearer-1234")
        mutator = agent.CODEX.make_header_mutator()
        # refresh=True must not raise (no kubectl, no internal lookup)
        assert mutator({}, refresh=True).get("Authorization") == "Bearer real-bearer-1234"


class TestCustomProviderContainerEnv:
    def test_returns_expected_env(self, home):
        _make_custom_provider_config(home)
        env = agent.CODEX.container_env("proxy-tok", 9999)
        assert env == {
            "OPENAI_API_KEY": "proxy-tok",
            "OPENAI_BASE_URL": "http://host.docker.internal:9999/v1",
            "HATCHERY_CODEX_PROVIDER": "dev-adapter",
        }

    def test_empty_path_when_base_url_has_no_path(self, home):
        _make_custom_provider_config(home, base_url="https://adapter.example.com")
        env = agent.CODEX.container_env("proxy-tok", 9999)
        assert env["OPENAI_BASE_URL"] == "http://host.docker.internal:9999"


class TestCustomProviderDockerWrapper:
    def test_wrapper_contains_both_branches(self):
        wrapper = agent.CodexBackend._DOCKER_WRAPPER
        # Custom-provider branch
        assert "HATCHERY_CODEX_PROVIDER" in wrapper
        assert "model_providers.${HATCHERY_CODEX_PROVIDER}.base_url" in wrapper
        assert "model_providers.${HATCHERY_CODEX_PROVIDER}.experimental_bearer_token" in wrapper
        # Legacy openai_base_url branch must still be present
        assert "openai_base_url" in wrapper


class TestCustomProviderConstructMounts:
    def test_skips_host_config_toml(self, home, tmp_path):
        _make_custom_provider_config(home)
        # Host config.toml exists (written by _make_custom_provider_config), so the
        # non-custom-provider path would have bind-mounted it RW.
        mounts = agent.CODEX.construct_mounts(tmp_path)
        bind_srcs = {str(m.src) for m in mounts if isinstance(m, mount.BindMount) and m.mode == "RW"}
        # No RW bind from the host config.toml — it contains the real bearer.
        assert str(home / ".codex" / "config.toml") not in bind_srcs

    def test_includes_synthesized_config_path(self, home, tmp_path):
        _make_custom_provider_config(home)
        mounts = agent.CODEX.construct_mounts(tmp_path)
        synth = [
            m
            for m in mounts
            if isinstance(m, mount.BindMount) and m.dst == f"{agent.CONTAINER_HOME}/.codex/config.toml"
        ]
        assert len(synth) == 1
        assert synth[0].src == tmp_path / "codex_config.toml"
        # RW so codex can persist project trust / model selection / TUI
        # prefs into its own scratch copy of the file.
        assert synth[0].mode == "RW"

    def test_includes_catalog_when_present(self, home, tmp_path):
        _make_custom_provider_config(home)
        (home / ".codex" / "model-catalog.json").write_text('{"models": []}')
        mounts = agent.CODEX.construct_mounts(tmp_path)
        catalog = [
            m
            for m in mounts
            if isinstance(m, mount.BindMount) and m.dst == f"{agent.CONTAINER_HOME}/.codex/model-catalog.json"
        ]
        assert len(catalog) == 1
        assert catalog[0].mode == "RO"


class TestCustomProviderOnBeforeContainerStart:
    def test_writes_sanitized_config(self, home, tmp_path):
        _make_custom_provider_config(home, bearer="real-bearer-NEVER-IN-CONTAINER")
        session_dir = tmp_path / "session"
        agent.CODEX.on_before_container_start(session_dir, "proxy-tok-XYZ", "/workdir")
        out = session_dir / "codex_config.toml"
        assert out.exists()
        content = out.read_text()
        # Sanity-checks on the synthesised TOML
        assert 'model = "some/Model"' in content
        assert 'model_provider = "dev-adapter"' in content
        assert "[model_providers.dev-adapter]" in content
        assert 'wire_api = "responses"' in content
        assert 'base_url = "http://placeholder/"' in content
        assert 'experimental_bearer_token = "proxy-tok-XYZ"' in content
        # Critical: the real bearer must NEVER appear in the file
        assert "real-bearer-NEVER-IN-CONTAINER" not in content

    def test_noop_when_not_custom_provider(self, home, tmp_path):
        # No config.toml on host → not in custom-provider mode → no file written
        session_dir = tmp_path / "session"
        agent.CODEX.on_before_container_start(session_dir, "proxy-tok", "/workdir")
        assert not session_dir.exists() or not (session_dir / "codex_config.toml").exists()

    def test_includes_catalog_path_only_when_host_catalog_exists(self, home, tmp_path):
        _make_custom_provider_config(home)
        session_dir = tmp_path / "session"
        agent.CODEX.on_before_container_start(session_dir, "proxy-tok", "/workdir")
        content = (session_dir / "codex_config.toml").read_text()
        assert "model_catalog_json" not in content

        # Now add the host catalog and re-run
        (home / ".codex" / "model-catalog.json").write_text('{"models": []}')
        agent.CODEX.on_before_container_start(session_dir, "proxy-tok", "/workdir")
        content = (session_dir / "codex_config.toml").read_text()
        assert f'model_catalog_json = "{agent.CONTAINER_HOME}/.codex/model-catalog.json"' in content

    def test_synthesized_config_pre_trusts_workdir(self, home, tmp_path):
        """The workdir is added as a trusted project so codex doesn't
        prompt on startup (and doesn't try to persist the answer back
        to config.toml)."""
        import tomllib

        _make_custom_provider_config(home)
        session_dir = tmp_path / "session"
        workdir = "/Users/me/code/myrepo/.hatchery/worktrees/feature-x"
        agent.CODEX.on_before_container_start(session_dir, "proxy-tok", workdir)
        data = tomllib.loads((session_dir / "codex_config.toml").read_text())
        assert data["projects"][workdir] == {"trust_level": "trusted"}

    def test_synthesized_config_does_not_carry_host_trust_entries(self, home, tmp_path):
        """Only the workdir is trusted — other host entries don't exist
        inside the container, so copying them would be inert noise."""
        import tomllib

        (home / ".codex").mkdir()
        (home / ".codex" / "config.toml").write_text(
            """\
model = "some/Model"
model_provider = "dev-adapter"

[model_providers.dev-adapter]
wire_api = "responses"
base_url = "https://adapter.example.com/v1"
experimental_bearer_token = "real-bearer"

[projects."/Users/me/other-repo"]
trust_level = "trusted"
"""
        )
        session_dir = tmp_path / "session"
        agent.CODEX.on_before_container_start(session_dir, "proxy-tok", "/workdir")
        data = tomllib.loads((session_dir / "codex_config.toml").read_text())
        assert set(data["projects"]) == {"/workdir"}

    def test_synthesized_config_is_valid_toml(self, home, tmp_path):
        import tomllib

        _make_custom_provider_config(home)
        session_dir = tmp_path / "session"
        agent.CODEX.on_before_container_start(session_dir, "proxy-tok", "/workdir")
        data = tomllib.loads((session_dir / "codex_config.toml").read_text())
        assert data["model_provider"] == "dev-adapter"
        assert data["model_providers"]["dev-adapter"]["experimental_bearer_token"] == "proxy-tok"


# ---------------------------------------------------------------------------
# session-id probe
# ---------------------------------------------------------------------------


def _meta(**overrides) -> SessionMeta:
    """Minimal SessionMeta for probe / poller tests."""
    return SessionMeta(
        name=overrides.pop("name", "t"),
        repo=overrides.pop("repo", "/repo"),
        worktree=overrides.pop("worktree", "/wt"),
        agent="CODEX",
        **overrides,
    )


def _write_rollout(root: Path, uuid: str, *, mtime: float | None = None) -> Path:
    """Create an empty rollout file at ~/.codex/sessions/YYYY/MM/DD/.

    The trailing UUID in the filename is the id that ``codex resume``
    accepts — that's what our probe extracts.
    """
    day = root / ".codex" / "sessions" / "2026" / "07" / "01"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-07-01T12-00-00-{uuid}.jsonl"
    path.write_text("")
    if mtime is not None:
        import os

        os.utime(path, (mtime, mtime))
    return path


def _docker_probe_stdout(
    uuid: str,
    *,
    path_prefix: str = "/home/hatchery/.codex/sessions/2026/07/01/rollout-2026-07-01T12-00-00-",
    mtime: float | None = None,
    raw: str | None = None,
) -> str:
    """Assemble the ``<mtime> <path>`` line the docker probe shell snippet emits.

    ``raw`` bypasses the mtime/uuid formatting for tests that need to
    inject malformed output.
    """
    if raw is not None:
        return raw
    m = mtime if mtime is not None else time.time()
    return f"{m} {path_prefix}{uuid}.jsonl\n"


class TestExtractUuidFromPath:
    def test_extracts_trailing_uuid(self):
        uuid = "019f1e3c-5b9a-76f3-b6f7-e5f967162066"
        got = codex_backend._extract_uuid_from_path(
            f"/home/hatchery/.codex/sessions/2026/07/01/rollout-2026-07-01T12-00-00-{uuid}.jsonl"
        )
        assert got == uuid

    def test_returns_none_when_no_uuid(self):
        assert codex_backend._extract_uuid_from_path("/tmp/rollout-not-a-uuid.jsonl") is None

    def test_returns_none_on_unrelated_path(self):
        assert codex_backend._extract_uuid_from_path("/etc/passwd") is None


class TestProbeSessionIdNative:
    def test_returns_uuid_from_newest_rollout(self, home):
        uuid = "019f1e3c-5b9a-76f3-b6f7-e5f967162066"
        _write_rollout(home, uuid, mtime=time.time())
        got = codex_backend._probe_session_id(_meta(), docker=False, runtime=None, launch_start=time.time() - 10)
        assert got == uuid

    def test_ignores_stale_rollouts(self, home):
        _write_rollout(home, "019f1e3c-5b9a-76f3-b6f7-e5f967162066", mtime=time.time() - 3600)
        assert codex_backend._probe_session_id(_meta(), docker=False, runtime=None, launch_start=time.time()) is None

    def test_prefers_newest_when_multiple_fresh(self, home):
        newer = "019f1e3c-5b9a-76f3-b6f7-000000000002"
        older = "019f1e3c-5b9a-76f3-b6f7-000000000001"
        now = time.time()
        _write_rollout(home, older, mtime=now - 1)
        _write_rollout(home, newer, mtime=now + 1)
        got = codex_backend._probe_session_id(_meta(), docker=False, runtime=None, launch_start=now - 10)
        assert got == newer

    def test_handles_missing_sessions_dir(self, home):
        assert codex_backend._probe_session_id(_meta(), docker=False, runtime=None, launch_start=time.time()) is None

    def test_slight_negative_skew_still_accepted(self, home):
        # Filesystem clock a couple of seconds behind — still accepted
        # thanks to the 5s tolerance window.
        uuid = "019f1e3c-5b9a-76f3-b6f7-e5f967162066"
        launch = time.time()
        _write_rollout(home, uuid, mtime=launch - 2)
        got = codex_backend._probe_session_id(_meta(), docker=False, runtime=None, launch_start=launch)
        assert got == uuid


class TestProbeSessionIdDocker:
    def test_shells_out_to_docker_exec(self):
        from seekr_hatchery.docker import DockerRuntime

        uuid = "019f1e3c-5b9a-76f3-b6f7-e5f967162066"
        launch = time.time()
        completed = MagicMock(returncode=0, stdout=_docker_probe_stdout(uuid, mtime=launch + 1))
        with patch("subprocess.run", return_value=completed) as sp:
            got = codex_backend._probe_session_id(
                _meta(name="my-task"), docker=True, runtime=DockerRuntime(), launch_start=launch
            )
        assert got == uuid
        cmd = sp.call_args[0][0]
        assert cmd[0] == "docker"
        assert cmd[1] == "exec"
        assert any("my-task" in part for part in cmd)

    def test_nonzero_exit_returns_none(self):
        from seekr_hatchery.docker import DockerRuntime

        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")):
            assert (
                codex_backend._probe_session_id(_meta(), docker=True, runtime=DockerRuntime(), launch_start=time.time())
                is None
            )

    def test_empty_stdout_returns_none(self):
        from seekr_hatchery.docker import DockerRuntime

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            assert (
                codex_backend._probe_session_id(_meta(), docker=True, runtime=DockerRuntime(), launch_start=time.time())
                is None
            )

    def test_timeout_returns_none(self):
        from seekr_hatchery.docker import DockerRuntime

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["docker"], timeout=2)):
            assert (
                codex_backend._probe_session_id(_meta(), docker=True, runtime=DockerRuntime(), launch_start=time.time())
                is None
            )

    def test_stale_rollout_returns_none(self):
        # File mtime is well before launch_start (dirty volume from a
        # previous task run) — must be filtered.
        from seekr_hatchery.docker import DockerRuntime

        launch = time.time()
        completed = MagicMock(
            returncode=0,
            stdout=_docker_probe_stdout("019f1e3c-5b9a-76f3-b6f7-e5f967162066", mtime=launch - 3600),
        )
        with patch("subprocess.run", return_value=completed):
            assert (
                codex_backend._probe_session_id(_meta(), docker=True, runtime=DockerRuntime(), launch_start=launch)
                is None
            )

    def test_slight_negative_skew_still_accepted(self):
        # Container clock is a couple of seconds behind host — still
        # accepted thanks to the 5s tolerance window.
        from seekr_hatchery.docker import DockerRuntime

        uuid = "019f1e3c-5b9a-76f3-b6f7-e5f967162066"
        launch = time.time()
        completed = MagicMock(returncode=0, stdout=_docker_probe_stdout(uuid, mtime=launch - 2))
        with patch("subprocess.run", return_value=completed):
            got = codex_backend._probe_session_id(_meta(), docker=True, runtime=DockerRuntime(), launch_start=launch)
        assert got == uuid

    def test_unparseable_stdout_returns_none(self):
        # Missing space between mtime and path, or otherwise garbage.
        from seekr_hatchery.docker import DockerRuntime

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="not-a-stat-line\n")):
            assert (
                codex_backend._probe_session_id(_meta(), docker=True, runtime=DockerRuntime(), launch_start=time.time())
                is None
            )

    def test_non_numeric_mtime_returns_none(self):
        from seekr_hatchery.docker import DockerRuntime

        with patch(
            "subprocess.run",
            return_value=MagicMock(
                returncode=0,
                stdout="notanumber /home/hatchery/.codex/sessions/2026/07/01/rollout-x-abc.jsonl\n",
            ),
        ):
            assert (
                codex_backend._probe_session_id(_meta(), docker=True, runtime=DockerRuntime(), launch_start=time.time())
                is None
            )

    def test_malformed_filename_returns_none(self):
        # File exists and mtime is fresh, but the filename doesn't carry
        # a UUID — we must not persist garbage.
        from seekr_hatchery.docker import DockerRuntime

        launch = time.time()
        stdout = f"{launch + 1} /home/hatchery/.codex/sessions/2026/07/01/rollout-oops.jsonl\n"
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=stdout)):
            assert (
                codex_backend._probe_session_id(_meta(), docker=True, runtime=DockerRuntime(), launch_start=launch)
                is None
            )


# ---------------------------------------------------------------------------
# background_threads
# ---------------------------------------------------------------------------


class TestBackgroundThreads:
    def test_returns_poller_when_no_session_id(self):
        stop = threading.Event()
        workers = agent.CODEX.background_threads(
            _meta(), docker=False, runtime=None, launch_start=time.time(), stop=stop
        )
        assert len(workers) == 1
        assert callable(workers[0])

    def test_returns_poller_even_when_session_id_set(self):
        # Poller runs unconditionally — on resume, codex may write a new
        # rollout for the resumed thread and we need to capture it.
        stop = threading.Event()
        workers = agent.CODEX.background_threads(
            _meta(session_id="019f1e3e-d5ff-7ce0-801b-28a905c7d3ed"),
            docker=False,
            runtime=None,
            launch_start=time.time(),
            stop=stop,
        )
        assert len(workers) == 1

    def test_session_id_pre_generated_flag(self):
        assert agent.CODEX.session_id_pre_generated is False

    def test_poller_persists_on_capture(self, fake_tasks_db, tmp_path):
        # Write a valid meta so sessions.save() works.
        import seekr_hatchery.sessions as sessions

        meta = _meta(repo=str(tmp_path), worktree=str(tmp_path))
        sessions.save(meta)

        uuid = "cafebabe-cafe-babe-cafe-babecafebabe"
        # Probe returns None first call, then the UUID.
        calls = {"n": 0}

        def fake_probe(meta_arg, *, docker, runtime, launch_start):
            calls["n"] += 1
            return uuid if calls["n"] >= 2 else None

        stop = threading.Event()
        with patch.object(codex_backend, "_probe_session_id", side_effect=fake_probe):
            workers = agent.CODEX.background_threads(
                meta, docker=False, runtime=None, launch_start=time.time(), stop=stop
            )
            assert len(workers) == 1
            # Run in a thread so we can time it out.
            t = threading.Thread(target=workers[0])
            t.start()
            t.join(timeout=5)
            assert not t.is_alive(), "poller did not exit after capture"

        assert meta.session_id == uuid
        # Persisted to disk too
        loaded = sessions.load(Path(meta.repo), meta.name)
        assert loaded.session_id == uuid

    def test_poller_exits_promptly_on_stop(self):
        meta = _meta()
        stop = threading.Event()
        with patch.object(codex_backend, "_probe_session_id", return_value=None):
            workers = agent.CODEX.background_threads(
                meta, docker=False, runtime=None, launch_start=time.time(), stop=stop
            )
            t = threading.Thread(target=workers[0])
            t.start()
            time.sleep(0.1)  # let the worker enter its loop
            stop.set()
            t.join(timeout=2)
            assert not t.is_alive()
        assert meta.session_id is None

    def test_poller_swallows_probe_exceptions(self, fake_tasks_db, tmp_path):
        import seekr_hatchery.sessions as sessions

        meta = _meta(repo=str(tmp_path), worktree=str(tmp_path))
        sessions.save(meta)

        uuid = "deadbeef-dead-beef-dead-beefdeadbeef"
        calls = {"n": 0}

        def fake_probe(meta_arg, *, docker, runtime, launch_start):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("container not up yet")
            return uuid

        stop = threading.Event()
        with patch.object(codex_backend, "_probe_session_id", side_effect=fake_probe):
            workers = agent.CODEX.background_threads(
                meta, docker=False, runtime=None, launch_start=time.time(), stop=stop
            )
            t = threading.Thread(target=workers[0])
            t.start()
            t.join(timeout=5)
            assert not t.is_alive()

        assert meta.session_id == uuid
