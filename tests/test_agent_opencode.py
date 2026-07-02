"""Unit tests for OpenCodeBackend."""

import json
import subprocess
from pathlib import Path

import pytest

import seekr_hatchery.agents as agent
import seekr_hatchery.mount as mount
from seekr_hatchery.agents.agent_backend import CONTAINER_HOME

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ONPREM_CONFIG = {
    "provider": {
        "my-provider": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "My On-Prem Provider",
            "options": {
                "baseURL": "http://on-prem.example.com/v1/inference",
                "apiKey": "{file:~/.config/opencode/api-key}",
            },
            "models": {
                "vendor-a/model-one": {"name": "Model One (On-Prem)"},
                "vendor-b/model-two": {"name": "Model Two (On-Prem)"},
            },
        }
    }
}

_BUILTIN_ONLY_CONFIG = {
    "provider": {
        "openai": {"options": {"apiKey": "sk-oai"}},
    }
}


def _write_opencode_config(home: Path, config: dict) -> None:
    cfg_dir = home / ".config" / "opencode"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "opencode.json").write_text(json.dumps(config))


def _write_api_key_file(home: Path, key: str = "real-token-xyz") -> None:
    key_dir = home / ".config" / "opencode"
    key_dir.mkdir(parents=True, exist_ok=True)
    (key_dir / "api-key").write_text(key)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestOpenCodeBackendConstants:
    def test_constants(self):
        assert agent.OPENCODE.kind == "OPENCODE"
        assert agent.OPENCODE.binary == "opencode"
        # Resumption works via `opencode --continue` + the persistent opencode-state
        # named volume — hatchery doesn't need to track the opencode session ID.
        assert agent.OPENCODE.supports_sessions is True


# ---------------------------------------------------------------------------
# build_new_command
# ---------------------------------------------------------------------------


class TestBuildNewCommand:
    def test_native_command_uses_tui_with_prompt(self, home):
        # new/resume use the interactive TUI (--prompt) so the session stays open
        # for plan approval + follow-up, mirroring codex's interactive session.
        cmd = agent.OPENCODE.build_new_command("sid", "sys", "initial")
        assert cmd == ["opencode", "--prompt", "sys\n\ninitial"]

    def test_docker_uses_sh_wrapper(self, home):
        # The sh wrapper writes OPENCODE_CONFIG_CONTENT to the global config path
        # so the TUI picks up provider/model/permission settings.
        cmd = agent.OPENCODE.build_new_command("sid", "sys", "initial", docker=True)
        assert cmd[0] == "sh"
        assert cmd[1] == "-c"
        assert "OPENCODE_CONFIG_CONTENT" in cmd[2]
        assert "opencode.json" in cmd[2]
        assert "--prompt" in cmd
        assert "sys\n\ninitial" in cmd

    def test_combines_system_and_initial_prompts(self, home):
        cmd = agent.OPENCODE.build_new_command("sid", "system", "task", docker=True)
        assert "system\n\ntask" in cmd

    def test_strips_whitespace_from_combined_prompt(self, home):
        cmd = agent.OPENCODE.build_new_command("sid", "", "just initial")
        assert "just initial" in cmd

    def test_workdir_has_no_effect(self, home):
        cmd1 = agent.OPENCODE.build_new_command("sid", "sys", "p", docker=True)
        cmd2 = agent.OPENCODE.build_new_command("sid", "sys", "p", docker=True, workdir="/w")
        assert cmd1 == cmd2

    def test_session_id_unused(self, home):
        cmd1 = agent.OPENCODE.build_new_command("id-a", "sys", "p")
        cmd2 = agent.OPENCODE.build_new_command("id-b", "sys", "p")
        assert cmd1 == cmd2

    def test_empty_prompt_launches_tui(self, home):
        # hatchery chat passes empty prompts — launch the interactive TUI with no
        # initial message.
        cmd = agent.OPENCODE.build_new_command("sid", "", "")
        assert cmd == ["opencode"]

    def test_empty_prompt_tui_in_docker(self, home):
        cmd = agent.OPENCODE.build_new_command("sid", "", "", docker=True)
        assert cmd[0] == "sh"
        assert cmd[1] == "-c"
        assert "OPENCODE_CONFIG_CONTENT" in cmd[2]

    def test_no_run_subcommand_for_interactive_new(self, home):
        # new/resume must NOT use `opencode run` — that's one-shot and exits
        # immediately after responding, leaving the task in an incomplete state.
        cmd = agent.OPENCODE.build_new_command("sid", "sys", "task")
        assert "run" not in cmd

    def test_no_model_flag_for_tui(self, home):
        # The TUI honours the `model` field from config, so no -m flag is needed.
        # (Only `opencode run` ignores config model and requires -m.)
        _write_opencode_config(home, _ONPREM_CONFIG)
        cmd = agent.OPENCODE.build_new_command("sid", "sys", "task")
        assert "-m" not in cmd


# ---------------------------------------------------------------------------
# build_resume_command
# ---------------------------------------------------------------------------


class TestBuildResumeCommand:
    def test_uses_tui_with_prompt_and_continue(self, home):
        # resume uses the interactive TUI with --continue --prompt so the session
        # resumes and stays open for follow-up conversation.
        cmd = agent.OPENCODE.build_resume_command("sid-ignored", "sys", "ctx")
        assert cmd == ["opencode", "--continue", "--prompt", "sys\n\nctx"]

    def test_docker_uses_sh_wrapper(self, home):
        cmd = agent.OPENCODE.build_resume_command("sid-ignored", "sys", "ctx", docker=True)
        assert cmd[0] == "sh"
        assert "OPENCODE_CONFIG" in cmd[2]
        assert "--continue" in cmd
        assert "--prompt" in cmd
        assert "sys\n\nctx" in cmd

    def test_empty_prompt_resumes_last_session(self, home):
        cmd = agent.OPENCODE.build_resume_command("sid-ignored", "", "")
        assert cmd == ["opencode", "--continue"]

    def test_nonempty_prompt_resumes_with_continue(self, home):
        cmd = agent.OPENCODE.build_resume_command("sid-ignored", "sys", "ctx")
        assert "--continue" in cmd
        assert "--prompt" in cmd

    def test_no_run_subcommand_for_interactive_resume(self, home):
        # resume must NOT use `opencode run` — that's one-shot and exits
        # immediately, defeating the purpose of resuming an interactive session.
        cmd = agent.OPENCODE.build_resume_command("sid-ignored", "sys", "ctx")
        assert "run" not in cmd

    def test_no_model_flag_for_tui(self, home):
        _write_opencode_config(home, _ONPREM_CONFIG)
        cmd = agent.OPENCODE.build_resume_command("sid-ignored", "sys", "ctx")
        assert "-m" not in cmd


# ---------------------------------------------------------------------------
# build_finalize_command
# ---------------------------------------------------------------------------


class TestBuildFinalizeCommand:
    def test_uses_wrap_up_prompt(self, home):
        cmd = agent.OPENCODE.build_finalize_command("sid-ignored", "sys", "wrap up now")
        assert cmd == ["opencode", "run", "wrap up now"]

    def test_docker_uses_sh_wrapper(self, home):
        cmd = agent.OPENCODE.build_finalize_command("sid-ignored", "sys", "wrap up", docker=True)
        assert cmd[0] == "sh"
        assert "OPENCODE_CONFIG" in cmd[2]
        assert cmd[-1] == "wrap up"

    def test_passes_model_flag_when_config_present(self, home, monkeypatch):
        _write_opencode_config(home, _ONPREM_CONFIG)
        cmd = agent.OPENCODE.build_finalize_command("sid-ignored", "sys", "wrap up")
        assert "-m" in cmd
        assert "my-provider/vendor-a/model-one" in cmd

    def test_no_print_logs_even_at_info_level(self, home, monkeypatch):
        # --print-logs dumps raw timestamp=... level=INFO lines to stderr that the
        # user sees as noise during the wrap-up step. Must never be added.
        import logging

        monkeypatch.setattr(logging, "root", logging.getLogger("hatchery"))
        logging.getLogger("hatchery").setLevel(logging.INFO)
        cmd = agent.OPENCODE.build_finalize_command("sid-ignored", "sys", "wrap up")
        assert "--print-logs" not in cmd

    def test_raises_when_custom_provider_has_no_models(self, home):
        # A custom provider with a baseURL but no `models` is unusable: opencode
        # run would fall back to its default model while enabled_providers
        # restricts the sandbox to the custom provider alone — no model works.
        # Must fail loudly instead of emitting a silently-broken command.
        config = {
            "provider": {
                "my-provider": {"options": {"baseURL": "https://api.example.com/v1"}},
            }
        }
        _write_opencode_config(home, config)
        with pytest.raises(RuntimeError, match="no models configured"):
            agent.OPENCODE.build_finalize_command("sid", "sys", "wrap up")


# ---------------------------------------------------------------------------
# _resolve_env_ref
# ---------------------------------------------------------------------------


class TestResolveEnvRef:
    def test_resolves_set_var(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "hello")
        assert agent.OpenCodeBackend._resolve_env_ref("{env:TEST_VAR}") == "hello"

    def test_unset_var_becomes_empty_string(self, monkeypatch):
        monkeypatch.delenv("OPENCODE_MISSING_VAR", raising=False)
        assert agent.OpenCodeBackend._resolve_env_ref("{env:OPENCODE_MISSING_VAR}") == ""

    def test_no_ref_unchanged(self):
        assert agent.OpenCodeBackend._resolve_env_ref("https://example.com/v1") == "https://example.com/v1"

    def test_partial_substitution(self, monkeypatch):
        monkeypatch.setenv("MY_HOST", "example.com")
        result = agent.OpenCodeBackend._resolve_env_ref("https://{env:MY_HOST}/v1")
        assert result == "https://example.com/v1"


# ---------------------------------------------------------------------------
# _resolve_file_ref
# ---------------------------------------------------------------------------


class TestResolveFileRef:
    def test_reads_file_contents(self, home, tmp_path):
        key_file = tmp_path / "mykey"
        key_file.write_text("secret-value")
        result = agent.OpenCodeBackend._resolve_file_ref(f"{{file:{key_file}}}")
        assert result == "secret-value"

    def test_strips_trailing_newline(self, home, tmp_path):
        key_file = tmp_path / "mykey"
        key_file.write_text("secret-value\n")
        result = agent.OpenCodeBackend._resolve_file_ref(f"{{file:{key_file}}}")
        assert result == "secret-value"

    def test_expands_tilde(self, home):
        key_file = home / ".config" / "opencode" / "api-key"
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text("tilde-key")
        result = agent.OpenCodeBackend._resolve_file_ref("{file:~/.config/opencode/api-key}")
        assert result == "tilde-key"

    def test_missing_file_becomes_empty_string(self, home):
        result = agent.OpenCodeBackend._resolve_file_ref("{file:/nonexistent/path/key}")
        assert result == ""

    def test_no_ref_unchanged(self):
        assert agent.OpenCodeBackend._resolve_file_ref("literal-key") == "literal-key"

    def test_does_not_alter_env_refs(self, monkeypatch):
        monkeypatch.setenv("SOME_VAR", "val")
        # _resolve_file_ref should not touch {env:} patterns
        result = agent.OpenCodeBackend._resolve_file_ref("{env:SOME_VAR}")
        assert result == "{env:SOME_VAR}"


# ---------------------------------------------------------------------------
# _resolve_config_ref
# ---------------------------------------------------------------------------


class TestResolveConfigRef:
    def test_resolves_env_ref(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "hello")
        assert agent.OpenCodeBackend._resolve_config_ref("{env:TEST_VAR}") == "hello"

    def test_resolves_file_ref(self, home):
        key_file = home / ".config" / "opencode" / "api-key"
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text("file-key")
        result = agent.OpenCodeBackend._resolve_config_ref("{file:~/.config/opencode/api-key}")
        assert result == "file-key"

    def test_literal_unchanged(self):
        assert agent.OpenCodeBackend._resolve_config_ref("literal-key") == "literal-key"

    def test_missing_file_returns_empty(self):
        assert agent.OpenCodeBackend._resolve_config_ref("{file:/no/such/file}") == ""

    def test_unset_env_returns_empty(self, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        assert agent.OpenCodeBackend._resolve_config_ref("{env:MISSING_VAR}") == ""


# ---------------------------------------------------------------------------
# _find_primary_provider
# ---------------------------------------------------------------------------


class TestFindPrimaryProvider:
    def test_returns_first_custom_provider(self):
        result = agent.OpenCodeBackend._find_primary_provider(_ONPREM_CONFIG)
        assert result is not None
        pid, pdata = result
        assert pid == "my-provider"
        assert pdata["name"] == "My On-Prem Provider"

    def test_none_when_only_builtins(self):
        assert agent.OpenCodeBackend._find_primary_provider(_BUILTIN_ONLY_CONFIG) is None

    def test_none_when_no_provider_key(self):
        assert agent.OpenCodeBackend._find_primary_provider({}) is None

    def test_skips_builtin_ids(self):
        config = {
            "provider": {
                "openai": {"options": {}},
                "my-custom": {"options": {"baseURL": "https://custom.example.com/v1"}},
            }
        }
        result = agent.OpenCodeBackend._find_primary_provider(config)
        assert result is not None
        assert result[0] == "my-custom"


# ---------------------------------------------------------------------------
# _resolve_model
# ---------------------------------------------------------------------------


class TestResolveModel:
    def test_prefers_explicit_host_model_when_matching_provider(self, home):
        config = {**_ONPREM_CONFIG, "model": "my-provider/vendor-b/model-two"}
        assert agent.OpenCodeBackend._resolve_model(config) == "my-provider/vendor-b/model-two"

    def test_falls_back_to_first_model_when_no_explicit_model(self, home):
        assert agent.OpenCodeBackend._resolve_model(_ONPREM_CONFIG) == "my-provider/vendor-a/model-one"

    def test_ignores_stale_model_pointing_at_other_provider(self, home):
        # A stale `model` referencing a builtin provider must not propagate —
        # enabled_providers restricts the sandbox to the custom provider alone.
        config = {**_ONPREM_CONFIG, "model": "openai/gpt-5.2-codex"}
        assert agent.OpenCodeBackend._resolve_model(config) == "my-provider/vendor-a/model-one"

    def test_returns_host_model_when_no_primary_provider(self, home):
        config = {"model": "openai/gpt-5.2-codex"}
        assert agent.OpenCodeBackend._resolve_model(config) == "openai/gpt-5.2-codex"

    def test_raises_when_custom_provider_has_no_models(self, home):
        # A custom provider without models is unusable in the sandbox: the only
        # selectable model would be a builtin (openai/...) which enabled_providers
        # disables. Raise instead of silently returning None (which would make
        # opencode run fall back to the openai default and then crash).
        config = {"provider": {"my-custom": {"options": {"baseURL": "https://x.example.com/v1"}}}}
        with pytest.raises(RuntimeError, match="no models configured"):
            agent.OpenCodeBackend._resolve_model(config)

    def test_returns_none_when_no_config(self, home):
        assert agent.OpenCodeBackend._resolve_model({}) is None

    def test_validates_model_is_in_provider_models(self, home):
        # host_model prefix matches provider but the model id isn't listed → fall back
        config = {**_ONPREM_CONFIG, "model": "my-provider/nonexistent-model"}
        assert agent.OpenCodeBackend._resolve_model(config) == "my-provider/vendor-a/model-one"


# ---------------------------------------------------------------------------
# _read_credentials
# ---------------------------------------------------------------------------


class TestReadCredentials:
    def test_resolves_api_key_from_file(self, home):
        _write_opencode_config(home, _ONPREM_CONFIG)
        _write_api_key_file(home, "real-token-xyz")
        assert agent.OpenCodeBackend._read_credentials() == "real-token-xyz"

    def test_resolves_api_key_from_env(self, home, monkeypatch):
        config = {
            "provider": {
                "my-provider": {
                    "options": {
                        "baseURL": "http://example.com/v1",
                        "apiKey": "{env:MY_API_KEY}",
                    }
                }
            }
        }
        monkeypatch.setenv("MY_API_KEY", "env-token-xyz")
        _write_opencode_config(home, config)
        assert agent.OpenCodeBackend._read_credentials() == "env-token-xyz"

    def test_returns_none_when_config_missing(self, home):
        assert agent.OpenCodeBackend._read_credentials() is None

    def test_returns_none_when_only_builtins(self, home):
        _write_opencode_config(home, _BUILTIN_ONLY_CONFIG)
        assert agent.OpenCodeBackend._read_credentials() is None

    def test_api_key_none_when_file_missing(self, home):
        _write_opencode_config(home, _ONPREM_CONFIG)
        # api-key file not written — should return None
        assert agent.OpenCodeBackend._read_credentials() is None

    def test_literal_api_key(self, home):
        config = {
            "provider": {
                "my-provider": {
                    "options": {
                        "baseURL": "https://api.example.com/v1",
                        "apiKey": "literal-key-abc",
                    }
                }
            }
        }
        _write_opencode_config(home, config)
        assert agent.OpenCodeBackend._read_credentials() == "literal-key-abc"

    def test_returns_only_api_key(self, home):
        # _read_credentials must return a single value (the api key), not a
        # (api_key, target_host) tuple — target_host was unused at the call site
        # and was resolved inconsistently (_resolve_env_ref vs _resolve_config_ref).
        _write_opencode_config(home, _ONPREM_CONFIG)
        _write_api_key_file(home, "real-token-xyz")
        result = agent.OpenCodeBackend._read_credentials()
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _build_inline_config
# ---------------------------------------------------------------------------


class TestBuildInlineConfig:
    def test_replaces_base_url(self, home):
        inline = agent.OpenCodeBackend._build_inline_config("http://proxy:9999/v1", "tok", _ONPREM_CONFIG)
        opts = inline["provider"]["my-provider"]["options"]
        assert opts["baseURL"] == "http://proxy:9999/v1"

    def test_replaces_api_key(self, home):
        inline = agent.OpenCodeBackend._build_inline_config("http://proxy:9999/v1", "my-token", _ONPREM_CONFIG)
        opts = inline["provider"]["my-provider"]["options"]
        assert opts["apiKey"] == "my-token"

    def test_preserves_provider_id(self, home):
        inline = agent.OpenCodeBackend._build_inline_config("http://proxy/v1", "tok", _ONPREM_CONFIG)
        assert "my-provider" in inline["provider"]

    def test_preserves_npm_and_name(self, home):
        inline = agent.OpenCodeBackend._build_inline_config("http://proxy/v1", "tok", _ONPREM_CONFIG)
        provider = inline["provider"]["my-provider"]
        assert provider.get("npm") == "@ai-sdk/openai-compatible"
        assert provider.get("name") == "My On-Prem Provider"

    def test_preserves_models(self, home):
        inline = agent.OpenCodeBackend._build_inline_config("http://proxy/v1", "tok", _ONPREM_CONFIG)
        provider = inline["provider"]["my-provider"]
        assert "models" in provider
        assert "vendor-a/model-one" in provider["models"]

    def test_real_api_key_not_in_inline_config(self, home, monkeypatch):
        monkeypatch.setenv("MY_API_KEY", "super-secret-real-key")
        inline = agent.OpenCodeBackend._build_inline_config("http://proxy/v1", "proxy-tok", _ONPREM_CONFIG)
        serialised = json.dumps(inline)
        assert "super-secret-real-key" not in serialised
        assert "{env:MY_API_KEY}" not in serialised

    def test_enabled_providers_restricts_to_custom_provider(self, home):
        inline = agent.OpenCodeBackend._build_inline_config("http://proxy/v1", "tok", _ONPREM_CONFIG)
        assert inline.get("enabled_providers") == ["my-provider"]

    def test_permission_allow_set(self, home):
        inline = agent.OpenCodeBackend._build_inline_config("http://proxy/v1", "tok", _ONPREM_CONFIG)
        assert inline.get("permission") == "allow"

    def test_auto_defaults_to_first_model(self, home):
        inline = agent.OpenCodeBackend._build_inline_config("http://proxy/v1", "tok", _ONPREM_CONFIG)
        assert inline.get("model") == "my-provider/vendor-a/model-one"

    def test_host_model_override_propagated(self, home):
        config_with_model = {**_ONPREM_CONFIG, "model": "my-provider/vendor-b/model-two"}
        inline = agent.OpenCodeBackend._build_inline_config("http://proxy/v1", "tok", config_with_model)
        assert inline.get("model") == "my-provider/vendor-b/model-two"

    def test_fallback_when_no_provider(self, home):
        inline = agent.OpenCodeBackend._build_inline_config("http://proxy/v1", "tok", {})
        assert "provider" in inline
        assert len(inline["provider"]) == 1
        pid = next(iter(inline["provider"]))
        opts = inline["provider"][pid]["options"]
        assert opts["baseURL"] == "http://proxy/v1"
        assert opts["apiKey"] == "tok"

    def test_raises_when_custom_provider_has_no_models(self, home):
        # enabled_providers restricts the sandbox to the custom provider, but
        # with no models listed there is no selectable model — building the
        # inline config must fail loudly rather than produce a broken sandbox.
        config = {
            "provider": {
                "my-provider": {"options": {"baseURL": "https://api.example.com/v1"}},
            }
        }
        with pytest.raises(RuntimeError, match="no models configured"):
            agent.OpenCodeBackend._build_inline_config("http://proxy/v1", "tok", config)


# ---------------------------------------------------------------------------
# proxy_kwargs
# ---------------------------------------------------------------------------


class TestProxyKwargs:
    def test_returns_target_host_and_scheme(self, home):
        _write_opencode_config(home, _ONPREM_CONFIG)
        _write_api_key_file(home)
        kwargs = agent.OPENCODE.proxy_kwargs()
        assert kwargs["target_host"] == "on-prem.example.com"
        assert kwargs["target_scheme"] == "http"

    def test_https_scheme_for_https_endpoint(self, home):
        config = {
            "provider": {
                "my-provider": {
                    "options": {
                        "baseURL": "https://api.example.com/v1",
                        "apiKey": "key",
                    }
                }
            }
        }
        _write_opencode_config(home, config)
        assert agent.OPENCODE.proxy_kwargs()["target_scheme"] == "https"

    def test_no_path_prefix(self, home):
        _write_opencode_config(home, _ONPREM_CONFIG)
        _write_api_key_file(home)
        assert "path_prefix" not in agent.OPENCODE.proxy_kwargs()

    def test_raises_when_no_config(self, home):
        with pytest.raises(RuntimeError, match="no opencode provider configured"):
            agent.OPENCODE.proxy_kwargs()

    def test_raises_when_only_builtins(self, home):
        _write_opencode_config(home, _BUILTIN_ONLY_CONFIG)
        with pytest.raises(RuntimeError, match="no opencode provider configured"):
            agent.OPENCODE.proxy_kwargs()


# ---------------------------------------------------------------------------
# make_header_mutator
# ---------------------------------------------------------------------------


class TestMakeHeaderMutator:
    def test_injects_bearer_token(self, home):
        _write_opencode_config(home, _ONPREM_CONFIG)
        _write_api_key_file(home, "real-api-key")
        mutator = agent.OPENCODE.make_header_mutator()
        result = mutator({})
        assert result.get("Authorization") == "Bearer real-api-key"

    def test_strips_inbound_auth_headers(self, home):
        _write_opencode_config(home, _ONPREM_CONFIG)
        _write_api_key_file(home, "real-key")
        mutator = agent.OPENCODE.make_header_mutator()
        result = mutator(
            {
                "x-api-key": "proxy-tok",
                "authorization": "Bearer proxy-tok",
                "content-type": "application/json",
            }
        )
        assert result.get("Authorization") == "Bearer real-key"
        assert result.get("content-type") == "application/json"
        lower_keys = {k.lower() for k in result}
        assert "x-api-key" not in lower_keys

    def test_refresh_is_noop(self, home):
        _write_opencode_config(home, _ONPREM_CONFIG)
        _write_api_key_file(home, "real-key")
        mutator = agent.OPENCODE.make_header_mutator()
        result1 = mutator({})
        result2 = mutator({}, refresh=True)
        assert result1 == result2

    def test_raises_when_no_api_key(self, home):
        _write_opencode_config(home, _ONPREM_CONFIG)
        # api-key file not written — should raise
        with pytest.raises(RuntimeError, match="no API key found"):
            agent.OPENCODE.make_header_mutator()

    def test_raises_when_no_config(self, home):
        with pytest.raises(RuntimeError, match="no API key found"):
            agent.OPENCODE.make_header_mutator()


# ---------------------------------------------------------------------------
# container_env
# ---------------------------------------------------------------------------


class TestContainerEnv:
    def test_sets_dangerously_skip_permissions_env(self, home):
        _write_opencode_config(home, _ONPREM_CONFIG)
        _write_api_key_file(home)
        env = agent.OPENCODE.container_env("proxy-tok", 9999)
        assert env.get("OPENCODE_DANGEROUSLY_SKIP_PERMISSIONS") == "true"

    def test_sets_config_content(self, home):
        _write_opencode_config(home, _ONPREM_CONFIG)
        _write_api_key_file(home)
        env = agent.OPENCODE.container_env("proxy-tok", 9999)
        assert "OPENCODE_CONFIG_CONTENT" in env

    def test_config_content_is_valid_json(self, home):
        _write_opencode_config(home, _ONPREM_CONFIG)
        _write_api_key_file(home)
        env = agent.OPENCODE.container_env("proxy-tok", 9999)
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        assert "provider" in config

    def test_proxy_url_uses_correct_port(self, home):
        _write_opencode_config(home, _ONPREM_CONFIG)
        _write_api_key_file(home)
        env = agent.OPENCODE.container_env("proxy-tok", 12345)
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        provider = next(iter(config["provider"].values()))
        assert "12345" in provider["options"]["baseURL"]
        assert "host.docker.internal" in provider["options"]["baseURL"]

    def test_proxy_url_preserves_path_from_real_base_url(self, home):
        _write_opencode_config(home, _ONPREM_CONFIG)
        _write_api_key_file(home)
        env = agent.OPENCODE.container_env("proxy-tok", 9999)
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        provider = next(iter(config["provider"].values()))
        assert provider["options"]["baseURL"].endswith("/v1/inference")

    def test_proxy_token_used_as_api_key(self, home):
        _write_opencode_config(home, _ONPREM_CONFIG)
        _write_api_key_file(home)
        env = agent.OPENCODE.container_env("my-proxy-uuid", 9999)
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        provider = next(iter(config["provider"].values()))
        assert provider["options"]["apiKey"] == "my-proxy-uuid"

    def test_real_api_key_not_in_container_env(self, home):
        """SECURITY: the real API key must never appear in any container env var."""
        _write_opencode_config(home, _ONPREM_CONFIG)
        _write_api_key_file(home, "super-secret-real-key")
        env = agent.OPENCODE.container_env("proxy-tok", 9999)
        serialised = json.dumps(env)
        assert "super-secret-real-key" not in serialised, "real API key leaked into container env!"

    def test_real_base_url_not_in_container_env(self, home):
        """SECURITY: the real upstream URL must not appear in the container env."""
        _write_opencode_config(home, _ONPREM_CONFIG)
        _write_api_key_file(home)
        env = agent.OPENCODE.container_env("proxy-tok", 9999)
        serialised = json.dumps(env)
        assert "on-prem.example.com" not in serialised, "real endpoint URL leaked into container env!"

    def test_default_path_v1_when_no_config(self, home):
        env = agent.OPENCODE.container_env("proxy-tok", 9999)
        assert "OPENCODE_CONFIG_CONTENT" in env
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        provider = next(iter(config["provider"].values()))
        assert provider["options"]["baseURL"] == "http://host.docker.internal:9999/v1"


# ---------------------------------------------------------------------------
# construct_mounts
# ---------------------------------------------------------------------------


class TestConstructMounts:
    def test_returns_volume_mounts_for_config_and_data(self, tmp_path):
        mounts = agent.OPENCODE.construct_mounts(tmp_path)
        # When no host dirs exist, only the two per-task volumes are returned.
        assert len(mounts) == 2
        by_dst = {m.dst: m for m in mounts}
        config_vol = by_dst[f"{CONTAINER_HOME}/.config/opencode"]
        data_vol = by_dst[f"{CONTAINER_HOME}/.local/share/opencode"]
        assert isinstance(config_vol, mount.VolumeMount)
        assert config_vol.name == "opencode-state"
        assert isinstance(data_vol, mount.VolumeMount)
        assert data_vol.name == "opencode-data"

    def test_volumes_are_task_scoped(self, tmp_path):
        mounts = agent.OPENCODE.construct_mounts(tmp_path)
        for m in mounts:
            assert m.task_scoped is True

    def test_no_seed_needed(self, tmp_path):
        # Config comes from OPENCODE_CONFIG_CONTENT; volumes start empty.
        mounts = agent.OPENCODE.construct_mounts(tmp_path)
        for m in mounts:
            assert m.seed is None

    def test_no_binds_when_host_missing(self, home, tmp_path):
        mounts = agent.OPENCODE.construct_mounts(tmp_path)
        assert all(isinstance(m, mount.VolumeMount) for m in mounts)

    def test_cross_task_dirs_bind_rw_when_present(self, home, tmp_path):
        """skills/agents/commands/plugins are user-owned state that persists
        across tasks — RW so in-container edits propagate to the host."""
        host_opencode = home / ".config" / "opencode"
        for name in ("skills", "agents", "commands", "plugins"):
            (host_opencode / name).mkdir(parents=True)
        mounts = agent.OPENCODE.construct_mounts(tmp_path)
        by_dst = {m.dst: m for m in mounts}
        for name in ("skills", "agents", "commands", "plugins"):
            m = by_dst[f"{CONTAINER_HOME}/.config/opencode/{name}"]
            assert isinstance(m, mount.BindMount)
            assert m.mode == "RW"
            assert m.src == host_opencode / name

    def test_only_existing_cross_task_dirs_bound(self, home, tmp_path):
        """Only dirs that exist on the host are bound — missing dirs are skipped."""
        (home / ".config" / "opencode" / "skills").mkdir(parents=True)
        mounts = agent.OPENCODE.construct_mounts(tmp_path)
        binds = [m for m in mounts if isinstance(m, mount.BindMount)]
        assert len(binds) == 1
        assert binds[0].dst == f"{CONTAINER_HOME}/.config/opencode/skills"

    def test_opencode_json_not_bound(self, home, tmp_path):
        """The host opencode.json must never be bound — it contains the real
        API key and would shadow the proxy config."""
        (home / ".config" / "opencode").mkdir(parents=True)
        (home / ".config" / "opencode" / "opencode.json").write_text('{"provider": {}}')
        mounts = agent.OPENCODE.construct_mounts(tmp_path)
        by_dst = {m.dst: m for m in mounts}
        assert f"{CONTAINER_HOME}/.config/opencode/opencode.json" not in by_dst

    def test_claude_skills_dir_bound_when_present(self, home, tmp_path):
        """opencode searches ~/.claude/skills/ for Claude-compatible skills."""
        (home / ".claude" / "skills").mkdir(parents=True)
        mounts = agent.OPENCODE.construct_mounts(tmp_path)
        by_dst = {m.dst: m for m in mounts}
        m = by_dst[f"{CONTAINER_HOME}/.claude/skills"]
        assert isinstance(m, mount.BindMount)
        assert m.mode == "RW"
        assert m.src == home / ".claude" / "skills"

    def test_agents_skills_dir_bound_when_present(self, home, tmp_path):
        """opencode searches ~/.agents/skills/ for agent-compatible skills."""
        (home / ".agents" / "skills").mkdir(parents=True)
        mounts = agent.OPENCODE.construct_mounts(tmp_path)
        by_dst = {m.dst: m for m in mounts}
        m = by_dst[f"{CONTAINER_HOME}/.agents/skills"]
        assert isinstance(m, mount.BindMount)
        assert m.mode == "RW"
        assert m.src == home / ".agents" / "skills"

    def test_all_dirs_bound_when_all_present(self, home, tmp_path):
        """When all host dirs exist, the full set of mounts is returned."""
        host_opencode = home / ".config" / "opencode"
        for name in ("skills", "agents", "commands", "plugins"):
            (host_opencode / name).mkdir(parents=True)
        (home / ".claude" / "skills").mkdir(parents=True)
        (home / ".agents" / "skills").mkdir(parents=True)
        mounts = agent.OPENCODE.construct_mounts(tmp_path)
        # 2 volumes + 4 opencode dirs + 2 compat skills dirs = 8
        assert len(mounts) == 8
        assert sum(1 for m in mounts if isinstance(m, mount.VolumeMount)) == 2
        assert sum(1 for m in mounts if isinstance(m, mount.BindMount)) == 6


# ---------------------------------------------------------------------------
# on_new_task / on_before_launch / on_before_container_start
# ---------------------------------------------------------------------------


class TestOnNewTask:
    def test_is_noop(self, tmp_path):
        session_dir = tmp_path / "session"
        agent.OPENCODE.on_new_task(session_dir)
        assert not session_dir.exists()


class TestOnBeforeLaunch:
    def test_is_noop(self, tmp_path):
        agent.OPENCODE.on_before_launch(tmp_path)


class TestOnBeforeContainerStart:
    def test_is_noop(self, tmp_path):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        agent.OPENCODE.on_before_container_start(session_dir, "proxy-tok", "/workdir")
        assert list(session_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# dockerfile_install
# ---------------------------------------------------------------------------


class TestDockerfileInstall:
    def test_contains_opencode_ai_package(self):
        snippet = agent.OPENCODE.dockerfile_install
        assert "opencode-ai" in snippet

    def test_contains_npm(self):
        snippet = agent.OPENCODE.dockerfile_install
        assert "npm" in snippet

    def test_contains_nodejs(self):
        snippet = agent.OPENCODE.dockerfile_install
        assert "nodejs" in snippet

    def test_pins_opencode_to_exact_version(self):
        # `npm install -g opencode-ai` (unpinned) lets the registry float to
        # whatever latest is at build time — non-reproducible and flagged by
        # Scorecard's Pinned-Dependencies check. Must pin to an exact version.
        import re

        snippet = agent.OPENCODE.dockerfile_install
        assert "opencode-ai@" in snippet
        # No bare unpinned install of the package.
        assert not re.search(r"npm install -g opencode-ai\b(?!@)", snippet)
        # The pin is an exact x.y.z version, not a range/tag.
        match = re.search(r"opencode-ai@(\d+\.\d+\.\d+)", snippet)
        assert match, f"opencode-ai is not pinned to an exact x.y.z version in:\n{snippet}"


# ---------------------------------------------------------------------------
# format_image_reference (inherited default)
# ---------------------------------------------------------------------------


class TestFormatImageReference:
    def test_returns_raw_absolute_path(self):
        assert agent.OPENCODE.format_image_reference(Path("/tmp/clip.png")) == "/tmp/clip.png"


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_from_kind_resolves_opencode(self):
        backend = agent.from_kind("OPENCODE")
        assert backend is agent.OPENCODE

    def test_from_kind_case_insensitive(self):
        backend = agent.from_kind("opencode")
        assert backend is agent.OPENCODE

    def test_opencode_in_all_backends(self):
        assert agent.OPENCODE in agent.ALL_BACKENDS


# ---------------------------------------------------------------------------
# Config delivery integration tests
#
# These tests simulate the bug chain discovered in production:
#   1. OPENCODE_CONFIG_CONTENT env var is ignored by `opencode run`
#   2. OPENCODE_CONFIG file-path env var is read but provider/model fields ignored
#   3. Only the global ~/.config/opencode/opencode.json is fully honoured
#
# The sh wrapper must write to the GLOBAL path and contain the correct provider,
# model, and permission fields.  If any of these fail, opencode falls back to
# `openai/gpt-5.2-codex` (its built-in default) and crashes with
# ProviderModelNotFoundError.
# ---------------------------------------------------------------------------


class TestConfigDelivery:
    """Verify that the sh wrapper delivers config to the correct path and that
    the generated JSON contains all fields opencode needs to start cleanly."""

    def _get_wrapper_and_env(self, home):
        _write_opencode_config(home, _ONPREM_CONFIG)
        _write_api_key_file(home, "real-key")
        cmd = agent.OPENCODE.build_new_command("sid", "sys", "task", docker=True)
        env = agent.OPENCODE.container_env("proxy-tok", 9999)
        return cmd, env

    def test_wrapper_writes_to_global_config_path(self, home):
        """The global path ~/.config/opencode/opencode.json must be the target.
        opencode only fully applies provider/model/permission from that path."""
        cmd, _ = self._get_wrapper_and_env(home)
        wrapper_script = cmd[2]
        assert f"{CONTAINER_HOME}/.config/opencode/opencode.json" in wrapper_script

    def test_wrapper_does_not_use_tmp_or_env_config(self, home):
        """OPENCODE_CONFIG (pointing to /tmp) was tried and failed — model fields
        are silently ignored.  Guard against regression to that approach."""
        cmd, _ = self._get_wrapper_and_env(home)
        wrapper_script = cmd[2]
        assert "OPENCODE_CONFIG=" not in wrapper_script
        assert "/tmp/oc-config" not in wrapper_script

    def test_config_content_has_provider(self, home):
        """The custom provider must be in the config so opencode can route
        requests.  Missing this causes ProviderModelNotFoundError on startup."""
        _, env = self._get_wrapper_and_env(home)
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        assert "my-provider" in config.get("provider", {})

    def test_config_content_has_model(self, home):
        """Without an explicit model, opencode falls back to openai/gpt-5.2-codex
        which fails because the openai provider has no API key."""
        _, env = self._get_wrapper_and_env(home)
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        assert "model" in config
        assert config["model"].startswith("my-provider/")

    def test_config_content_has_enabled_providers(self, home):
        """Without enabled_providers restriction, opencode may attempt builtin
        providers and fail on missing API keys."""
        _, env = self._get_wrapper_and_env(home)
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        assert config.get("enabled_providers") == ["my-provider"]

    def test_config_content_has_permission_allow(self, home):
        """opencode run sets permission=deny for question/plan_enter/plan_exit
        by default.  The config must override this to allow autonomous operation."""
        _, env = self._get_wrapper_and_env(home)
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        assert config.get("permission") == "allow"

    def test_wrapper_script_is_valid_shell(self, home):
        """The sh wrapper must be parseable by /bin/sh — syntax errors silently
        skip the config write and leave opencode with no provider config."""
        cmd, _ = self._get_wrapper_and_env(home)
        wrapper_script = cmd[2]
        result = subprocess.run(
            ["sh", "-n", "-c", wrapper_script],
            capture_output=True,
        )
        assert result.returncode == 0, f"wrapper syntax error: {result.stderr.decode()}"

    def test_proxy_url_in_config_uses_correct_port(self, home):
        """The proxy URL written into the config must match the port that was
        started.  A stale port means all LLM requests hit the wrong target."""
        _write_opencode_config(home, _ONPREM_CONFIG)
        _write_api_key_file(home, "real-key")
        env = agent.OPENCODE.container_env("proxy-tok", 54321)
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        provider = config["provider"]["my-provider"]
        assert "54321" in provider["options"]["baseURL"]


# ---------------------------------------------------------------------------
# Session persistence integration tests
#
# opencode stores session history in a SQLite DB at ~/.local/share/opencode/
# (NOT ~/.config/opencode/ as one might expect).  Without persisting that
# directory across container runs, `opencode --continue` on resume finds no
# prior session and starts fresh — losing all conversation history.
# ---------------------------------------------------------------------------


class TestSessionDataPersistence:
    """Verify that the mount structure preserves session data across runs."""

    def test_data_volume_mounted_for_session_db(self, tmp_path):
        """The SQLite DB at ~/.local/share/opencode/opencode.db must survive
        container exit so ``opencode --continue`` on resume finds prior sessions."""
        mounts = agent.OPENCODE.construct_mounts(tmp_path)
        data_volumes = [
            m for m in mounts if isinstance(m, mount.VolumeMount) and m.dst == f"{CONTAINER_HOME}/.local/share/opencode"
        ]
        assert len(data_volumes) == 1, (
            "construct_mounts must include a VolumeMount for "
            "~/.local/share/opencode/ — opencode stores session history in a "
            "SQLite DB there, not in ~/.config/opencode/"
        )

    def test_data_volume_is_task_scoped(self, tmp_path):
        """The data volume must be task-scoped so concurrent tasks don't share
        the same SQLite DB (which would cause lock contention and cross-task
        session leakage)."""
        mounts = agent.OPENCODE.construct_mounts(tmp_path)
        data_vol = next(
            m for m in mounts if isinstance(m, mount.VolumeMount) and m.dst == f"{CONTAINER_HOME}/.local/share/opencode"
        )
        assert data_vol.task_scoped is True

    def test_build_mounts_includes_data_volume(self, tmp_path, monkeypatch):
        """End-to-end: docker.build_mounts must surface the data volume so
        the container actually gets it at launch time."""
        import seekr_hatchery.docker as docker
        from seekr_hatchery.models import SessionMeta

        repo = tmp_path / "repo"
        worktree = tmp_path / "repo" / ".hatchery" / "worktrees" / "test"
        worktree.mkdir(parents=True)
        (repo / ".git").mkdir(parents=True)
        (worktree / ".git").write_text(f"gitdir: {repo}/.git/worktrees/test")

        meta = SessionMeta(
            name="test",
            repo=str(repo),
            worktree=str(worktree),
        )
        cfg = docker.DockerConfig()
        monkeypatch.setattr(docker, "_default_home_mounts", lambda: [])
        mounts = docker.build_mounts(meta, agent.OPENCODE, tmp_path / "session", cfg)

        data_volumes = [
            m for m in mounts if isinstance(m, mount.VolumeMount) and m.dst == f"{CONTAINER_HOME}/.local/share/opencode"
        ]
        assert len(data_volumes) == 1

    def test_resume_command_uses_continue_flag(self, home):
        """``opencode --continue`` is the only way to resume the last session.
        Without the data volume, this flag finds nothing and starts fresh."""
        cmd = agent.OPENCODE.build_resume_command("sid", "sys", "ctx")
        assert "--continue" in cmd
