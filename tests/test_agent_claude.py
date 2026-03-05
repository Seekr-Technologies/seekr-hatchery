"""Unit tests for ClaudeBackend."""

import json

import pytest

import seekr_hatchery.agent as agent

_SETTINGS = json.dumps({"skipDangerousModePermissionPrompt": True, "trustedFolders": ["/w"]})


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestClaudeBackendConstants:
    def test_constants(self):
        assert agent.CLAUDE.kind == "CLAUDE"
        assert agent.CLAUDE.binary == "claude"
        assert agent.CLAUDE.supports_sessions is True


# ---------------------------------------------------------------------------
# build_new_command
# ---------------------------------------------------------------------------


class TestBuildNewCommand:
    def test_native(self):
        cmd = agent.CLAUDE.build_new_command("sid", "sys", "initial")
        assert cmd == [
            "claude",
            "--permission-mode=plan",
            "--append-system-prompt=sys",
            "--session-id=sid",
            "initial",
        ]

    def test_docker(self):
        cmd = agent.CLAUDE.build_new_command("sid", "sys", "initial", docker=True, workdir="/w")
        assert cmd == [
            "claude",
            "--allow-dangerously-skip-permissions",
            "--settings",
            _SETTINGS,
            "--permission-mode=plan",
            "--append-system-prompt=sys",
            "--session-id=sid",
            "initial",
        ]


# ---------------------------------------------------------------------------
# build_resume_command
# ---------------------------------------------------------------------------


class TestBuildResumeCommand:
    def test_native(self):
        # Pass initial_prompt to confirm session-based Claude ignores it
        cmd = agent.CLAUDE.build_resume_command("sid", "sys", "ignored")
        assert cmd == [
            "claude",
            "--permission-mode=plan",
            "--append-system-prompt=sys",
            "--resume=sid",
        ]

    def test_docker(self):
        cmd = agent.CLAUDE.build_resume_command("sid", "sys", docker=True, workdir="/w")
        assert cmd == [
            "claude",
            "--allow-dangerously-skip-permissions",
            "--settings",
            _SETTINGS,
            "--permission-mode=plan",
            "--append-system-prompt=sys",
            "--resume=sid",
        ]


# ---------------------------------------------------------------------------
# build_finalize_command
# ---------------------------------------------------------------------------


class TestBuildFinalizeCommand:
    def test_native(self):
        cmd = agent.CLAUDE.build_finalize_command("sid", "sys", "wrap up")
        assert cmd == [
            "claude",
            "--append-system-prompt=sys",
            "--resume=sid",
            "wrap up",
        ]

    def test_docker(self):
        cmd = agent.CLAUDE.build_finalize_command("sid", "sys", "wrap up", docker=True, workdir="/w")
        assert cmd == [
            "claude",
            "--allow-dangerously-skip-permissions",
            "--settings",
            _SETTINGS,
            "--append-system-prompt=sys",
            "--resume=sid",
            "wrap up",
        ]


# ---------------------------------------------------------------------------
# get_api_key
# ---------------------------------------------------------------------------


class TestGetApiKey:
    def test_returns_env_var(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")
        assert agent.CLAUDE.get_api_key() == "test-key-123"

    def test_returns_none_when_not_set_and_no_keychain(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(agent.ClaudeBackend, "_get_from_keychain", staticmethod(lambda: None))
        assert agent.CLAUDE.get_api_key() is None

    def test_env_var_takes_priority_over_keychain(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-token")
        monkeypatch.setattr(agent.ClaudeBackend, "_get_from_keychain", staticmethod(lambda: "keychain-token"))
        assert agent.CLAUDE.get_api_key() == "env-token"

    def test_falls_back_to_keychain(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(agent.ClaudeBackend, "_get_from_keychain", staticmethod(lambda: "keychain-token"))
        assert agent.CLAUDE.get_api_key() == "keychain-token"


# ---------------------------------------------------------------------------
# proxy_kwargs
# ---------------------------------------------------------------------------


class TestProxyKwargs:
    def test_proxy_kwargs(self):
        assert agent.CLAUDE.proxy_kwargs() == {"target_host": "api.anthropic.com", "inject_header": "x-api-key"}


# ---------------------------------------------------------------------------
# container_env
# ---------------------------------------------------------------------------


class TestContainerEnv:
    def test_container_env(self):
        assert agent.CLAUDE.container_env("tok", 9999) == {
            "ANTHROPIC_API_KEY": "tok",
            "ANTHROPIC_BASE_URL": "http://host.docker.internal:9999",
        }


# ---------------------------------------------------------------------------
# on_new_task
# ---------------------------------------------------------------------------


class TestOnNewTask:
    def test_noop_when_no_host_file(self, home, tmp_path):
        session_dir = tmp_path / "session"
        agent.CLAUDE.on_new_task(session_dir)
        assert not (session_dir / "claude.json").exists()

    def test_copies_host_file_stripping_auth_fields(self, home, tmp_path):
        (home / ".claude.json").write_text(
            json.dumps({"theme": "dark", "hasCompletedOnboarding": True, "oauthAccount": {"secret": "real-key"}})
        )
        session_dir = tmp_path / "session"
        agent.CLAUDE.on_new_task(session_dir)
        result = json.loads((session_dir / "claude.json").read_text())
        assert result["theme"] == "dark"
        assert result["hasCompletedOnboarding"] is True
        assert "oauthAccount" not in result

    def test_idempotent(self, home, tmp_path):
        (home / ".claude.json").write_text('{"theme": "dark"}')
        session_dir = tmp_path / "session"
        agent.CLAUDE.on_new_task(session_dir)
        (session_dir / "claude.json").write_text('{"theme": "modified"}')
        agent.CLAUDE.on_new_task(session_dir)
        assert (session_dir / "claude.json").read_text() == '{"theme": "modified"}'


# ---------------------------------------------------------------------------
# on_before_launch
# ---------------------------------------------------------------------------


class TestOnBeforeLaunch:
    def test_writes_skill_files_to_worktree(self, tmp_path):
        agent.CLAUDE.on_before_launch(tmp_path)
        skills_dir = tmp_path / ".claude" / "skills"
        assert skills_dir.exists()
        skill_dirs = [d for d in skills_dir.iterdir() if d.is_dir()]
        assert skill_dirs, "expected at least one skill directory"
        assert (skills_dir / "hatchery-done" / "SKILL.md").exists()


# ---------------------------------------------------------------------------
# on_before_container_start
# ---------------------------------------------------------------------------


class TestOnBeforeContainerStart:
    def _setup(self, tmp_path: object) -> tuple:
        session_dir = tmp_path / "session"  # type: ignore[operator]
        session_dir.mkdir()
        claude_json = session_dir / "claude.json"
        return session_dir, claude_json

    def test_seeds_trust_and_token_approval(self, tmp_path):
        token = "12345678-1234-1234-1234-123456789012"
        session_dir, claude_json = self._setup(tmp_path)
        claude_json.write_text("{}")
        agent.CLAUDE.on_before_container_start(session_dir, token, "/workdir")
        data = json.loads(claude_json.read_text())
        assert "/workdir" in data["trustedFolders"]
        assert data["projects"]["/workdir"]["hasTrustDialogAccepted"] is True
        assert token[-20:] in data["customApiKeyResponses"]["approved"]

    def test_clears_old_approved_entries(self, tmp_path):
        """Old real-key suffixes from the host copy must not persist in the container."""
        token = "new-proxy-token-1234-5678-9012-3456"
        session_dir, claude_json = self._setup(tmp_path)
        claude_json.write_text(json.dumps({"customApiKeyResponses": {"approved": ["old-real-key"], "rejected": []}}))
        agent.CLAUDE.on_before_container_start(session_dir, token, "/workdir")
        data = json.loads(claude_json.read_text())
        assert "old-real-key" not in data["customApiKeyResponses"]["approved"]
        assert token[-20:] in data["customApiKeyResponses"]["approved"]

    def test_idempotent(self, tmp_path):
        session_dir, claude_json = self._setup(tmp_path)
        claude_json.write_text("{}")
        agent.CLAUDE.on_before_container_start(session_dir, "token", "/workdir")
        agent.CLAUDE.on_before_container_start(session_dir, "token", "/workdir")
        data = json.loads(claude_json.read_text())
        assert data["trustedFolders"].count("/workdir") == 1

    def test_preserves_existing_projects(self, tmp_path):
        session_dir, claude_json = self._setup(tmp_path)
        claude_json.write_text(json.dumps({"projects": {"/other": {"hasTrustDialogAccepted": True}}}))
        agent.CLAUDE.on_before_container_start(session_dir, "token", "/workdir")
        data = json.loads(claude_json.read_text())
        assert data["projects"]["/other"]["hasTrustDialogAccepted"] is True
        assert data["projects"]["/workdir"]["hasTrustDialogAccepted"] is True


# ---------------------------------------------------------------------------
# home_mounts
# ---------------------------------------------------------------------------


class TestHomeMounts:
    def test_with_task_json(self, home, tmp_path):
        (home / ".claude").mkdir()
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        task_json = session_dir / "claude.json"
        task_json.write_text("{}")
        assert agent.CLAUDE.home_mounts(session_dir) == [
            f"{home / '.claude'}:{agent.CONTAINER_HOME}/.claude:rw",
            f"{task_json}:{agent.CONTAINER_HOME}/.claude.json:rw",
        ]

    def test_without_task_json(self, home, tmp_path):
        (home / ".claude").mkdir()
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        assert agent.CLAUDE.home_mounts(session_dir) == [
            f"{home / '.claude'}:{agent.CONTAINER_HOME}/.claude:rw",
        ]

    def test_missing_claude_dir(self, tmp_path):
        """~/.claude absent (e.g. CI) — mount is silently skipped."""
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        assert agent.CLAUDE.home_mounts(session_dir) == []


# ---------------------------------------------------------------------------
# tmpfs_paths
# ---------------------------------------------------------------------------


class TestTmpfsPaths:
    def test_tmpfs_paths(self):
        assert agent.CLAUDE.tmpfs_paths() == [f"{agent.CONTAINER_HOME}/.claude/backups"]


# ---------------------------------------------------------------------------
# dockerfile_install
# ---------------------------------------------------------------------------


class TestDockerfileInstall:
    def test_dockerfile_install(self):
        snippet = agent.CLAUDE.dockerfile_install
        assert "https://claude.ai/install.sh" in snippet


# ---------------------------------------------------------------------------
# api_key_missing_hint
# ---------------------------------------------------------------------------


class TestApiKeyMissingHint:
    def test_api_key_missing_hint(self):
        assert "ANTHROPIC_API_KEY" in agent.CLAUDE.api_key_missing_hint


# ---------------------------------------------------------------------------
# CONTAINER_HOME constant (module-level, used by all backends)
# ---------------------------------------------------------------------------


class TestContainerHome:
    def test_container_home(self):
        assert isinstance(agent.CONTAINER_HOME, str)
        assert agent.CONTAINER_HOME.startswith("/")


# ---------------------------------------------------------------------------
# from_kind (module-level registry)
# ---------------------------------------------------------------------------


class TestFromKind:
    def test_from_string_claude(self):
        assert agent.from_kind("claude") is agent.CLAUDE

    def test_from_string_codex(self):
        assert agent.from_kind("codex") is agent.CODEX

    def test_string_is_case_insensitive(self):
        assert agent.from_kind("CLAUDE") is agent.CLAUDE
        assert agent.from_kind("Codex") is agent.CODEX

    def test_unknown_strings_raise_value_error(self):
        for kind in ["gpt-engineer", "opencode"]:
            with pytest.raises(ValueError, match="unknown agent"):
                agent.from_kind(kind)

    def test_round_trip_via_kind(self):
        for backend in [agent.CLAUDE, agent.CODEX]:
            assert agent.from_kind(backend.kind) is backend
