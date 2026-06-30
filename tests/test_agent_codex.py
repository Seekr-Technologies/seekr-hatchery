"""Unit tests for CodexBackend."""

import json
from pathlib import Path

import pytest

import seekr_hatchery.agents as agent
import seekr_hatchery.mount as mount

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


# ---------------------------------------------------------------------------
# build_resume_command
# ---------------------------------------------------------------------------


class TestBuildResumeCommand:
    def test_uses_initial_prompt_as_context_native(self):
        # session_id is unused; initial_prompt provides task context
        cmd = agent.CODEX.build_resume_command("sid-ignored", "sys", "ctx")
        assert cmd == ["codex", "--dangerously-bypass-approvals-and-sandbox", "sys\n\nctx"]

    def test_docker_uses_proxy_wrapper(self):
        cmd = agent.CODEX.build_resume_command("sid-ignored", "sys", "ctx", docker=True)
        assert cmd[0] == "sh"
        assert "openai_base_url" in cmd[2]
        assert cmd[-1] == "sys\n\nctx"


# ---------------------------------------------------------------------------
# build_finalize_command
# ---------------------------------------------------------------------------


class TestBuildFinalizeCommand:
    def test_uses_wrap_up_prompt_native(self):
        cmd = agent.CODEX.build_finalize_command("sid-ignored", "sys", "wrap up")
        assert cmd == ["codex", "--dangerously-bypass-approvals-and-sandbox", "wrap up"]

    def test_docker_uses_proxy_wrapper(self):
        cmd = agent.CODEX.build_finalize_command("sid-ignored", "sys", "wrap up", docker=True)
        assert cmd[0] == "sh"
        assert "openai_base_url" in cmd[2]
        assert cmd[-1] == "wrap up"


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
