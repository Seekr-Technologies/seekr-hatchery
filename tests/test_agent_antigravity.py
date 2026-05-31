"""Unit tests for AntigravityBackend."""

import json

import pytest

import seekr_hatchery.agents as agent


class TestAntigravityBackendConstants:
    def test_constants(self):
        assert agent.ANTIGRAVITY.kind == "ANTIGRAVITY"
        assert agent.ANTIGRAVITY.binary == "agy"
        assert agent.ANTIGRAVITY.supports_sessions is True
        assert agent.ANTIGRAVITY.needs_tls_proxy is True


# ---------------------------------------------------------------------------
# build_new_command
# ---------------------------------------------------------------------------


class TestBuildNewCommand:
    def test_combines_prompts(self):
        cmd = agent.ANTIGRAVITY.build_new_command("sid", "sys", "initial")
        assert cmd == ["agy", "--dangerously-skip-permissions", "-i", "sys\n\ninitial"]

    def test_docker_uses_shell_wrapper(self):
        cmd = agent.ANTIGRAVITY.build_new_command("sid", "sys", "initial", docker=True)
        assert cmd[0] == "sh"
        assert cmd[1] == "-c"
        assert "exec 'agy' '--dangerously-skip-permissions' '-i' 'sys\n\ninitial'" in cmd[2]

    def test_empty_prompt_omits_i_flag(self):
        # hatchery chat passes empty system_prompt and initial_prompt.
        # Passing -i '' causes a 400 INVALID_ARGUMENT from the Google API
        # (empty user message is not allowed). When both prompts are empty,
        # agy must be started in interactive mode without -i.
        cmd = agent.ANTIGRAVITY.build_new_command("sid", "", "")
        assert cmd == ["agy", "--dangerously-skip-permissions"]

    def test_empty_prompt_docker_omits_i_flag(self):
        cmd = agent.ANTIGRAVITY.build_new_command("sid", "", "", docker=True)
        assert cmd[0] == "sh"
        assert cmd[1] == "-c"
        shell_cmd = cmd[2]
        assert "exec 'agy' '--dangerously-skip-permissions'" in shell_cmd
        assert "-i" not in shell_cmd.split("exec 'agy'")[1]

    def test_docker_wrapper_writes_mcp_config(self):
        # _wrap_docker_command must write a valid mcp_config.json so agy does
        # not log "unexpected end of JSON input" on startup.
        cmd = agent.ANTIGRAVITY.build_new_command("sid", "sys", "initial", docker=True)
        assert "mcp_config.json" in cmd[2]
        assert 'mcpServers' in cmd[2]


# ---------------------------------------------------------------------------
# build_resume_command
# ---------------------------------------------------------------------------


class TestBuildResumeCommand:
    def test_resumes_session_with_initial_prompt(self):
        cmd = agent.ANTIGRAVITY.build_resume_command("sid-123", "sys", "resume prompt")
        assert cmd == ["agy", "--dangerously-skip-permissions", "--conversation", "sid-123", "-i", "resume prompt"]

    def test_resumes_session_with_default_prompt(self):
        cmd = agent.ANTIGRAVITY.build_resume_command("sid-123", "sys", "")
        assert cmd == [
            "agy",
            "--dangerously-skip-permissions",
            "--conversation",
            "sid-123",
            "-i",
            "Please continue the task.",
        ]


# ---------------------------------------------------------------------------
# build_finalize_command
# ---------------------------------------------------------------------------


class TestBuildFinalizeCommand:
    def test_uses_wrap_up_prompt(self):
        cmd = agent.ANTIGRAVITY.build_finalize_command("sid-123", "sys", "wrap up")
        assert cmd == ["agy", "--dangerously-skip-permissions", "--conversation", "sid-123", "-i", "wrap up"]


# ---------------------------------------------------------------------------
# proxy_kwargs
# ---------------------------------------------------------------------------


class TestProxyKwargs:
    def test_proxy_kwargs(self):
        assert agent.ANTIGRAVITY.proxy_kwargs() == {
            "target_host": "daily-cloudcode-pa.googleapis.com",
        }


# ---------------------------------------------------------------------------
# extra_hosts
# ---------------------------------------------------------------------------


class TestExtraHosts:
    def test_extra_hosts(self):
        assert agent.ANTIGRAVITY.extra_hosts(9999) == {
            "daily-cloudcode-pa.googleapis.com": "127.0.0.1",
            "people.googleapis.com": "127.0.0.1",
            "www.googleapis.com": "127.0.0.1",
            "play.googleapis.com": "127.0.0.1",
        }


# ---------------------------------------------------------------------------
# make_header_mutator
# ---------------------------------------------------------------------------


class TestMakeHeaderMutator:
    def test_injects_bearer(self, home):
        # Seed fake OAuth token in the expected home directory
        cli_dir = home / ".gemini" / "antigravity-cli"
        cli_dir.mkdir(parents=True)
        (cli_dir / "antigravity-oauth-token").write_text(
            json.dumps({"token": {"access_token": "google-oauth-tok-123"}})
        )

        mutator = agent.ANTIGRAVITY.make_header_mutator()
        result = mutator({"authorization": "Bearer proxy-tok", "x-api-key": "proxy-tok", "content-type": "json"})
        assert result.get("Authorization") == "Bearer google-oauth-tok-123"
        assert result.get("content-type") == "json"
        assert "x-api-key" not in {k.lower() for k in result}

    def test_raises_when_no_credentials(self, home):
        with pytest.raises(RuntimeError, match="No Antigravity OAuth token found"):
            agent.ANTIGRAVITY.make_header_mutator()


# ---------------------------------------------------------------------------
# container_env
# ---------------------------------------------------------------------------


class TestContainerEnv:
    def test_container_env(self, monkeypatch):
        # Mock ca_cert_pem on AntigravityBackend class
        monkeypatch.setattr(agent.AntigravityBackend, "ca_cert_pem", b"fake-ca-pem")
        env = agent.ANTIGRAVITY.container_env("tok", 9999)
        ca_path = "/home/hatchery/.gemini/antigravity-cli/hatchery-agy-ca.crt"
        assert env.get("SSL_CERT_FILE") == ca_path
        assert env.get("CURL_CA_BUNDLE") == ca_path
        assert env.get("REQUESTS_CA_BUNDLE") == ca_path
        assert "fake-ca-pem" in env.get("HATCHERY_AGY_CA", "")
        assert "tok" in env.get("HATCHERY_AGY_TOKEN", "")


# ---------------------------------------------------------------------------
# on_before_container_start
# ---------------------------------------------------------------------------


class TestOnBeforeContainerStart:
    def test_writes_expected_ephemeral_assets(self, tmp_path):
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        agent.ANTIGRAVITY.on_before_container_start(session_dir, "proxy-tok", "/workdir")

        assert (session_dir / "agy_ca.crt").exists()
        assert (session_dir / "agy_leaf.crt").exists()
        assert (session_dir / "agy_leaf.key").exists()

        token_data = json.loads((session_dir / "agy_token.json").read_text())
        assert token_data == {
            "token": {
                "access_token": "proxy-tok",
                "token_type": "Bearer",
                "refresh_token": None,
                "expiry": "2038-01-19T03:14:07Z",
            }
        }


# ---------------------------------------------------------------------------
# construct_mounts
# ---------------------------------------------------------------------------


class TestConstructMounts:
    def test_returns_empty_mounts(self, tmp_path):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        assert agent.ANTIGRAVITY.construct_mounts(session_dir) == []
