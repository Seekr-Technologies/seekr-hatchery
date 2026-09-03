"""Unit tests for PiBackend."""

import base64
import json
import subprocess
from pathlib import Path

import pytest

import seekr_hatchery.agents as agent
import seekr_hatchery.agents.pi as pi_backend
import seekr_hatchery.mount as mount

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The providers pi's models-store.json carries for a typical user.
_STORE = {
    "acme": ("openai-completions", "https://api.acme.com"),
    "openai": ("openai-responses", "https://api.openai.com/v1"),
    "huggingface": ("openai-completions", "https://router.huggingface.co/v1"),
}


def _write_store(home: Path, providers: dict[str, tuple[str, str]]) -> None:
    agent_dir = home / ".pi" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    store = {pid: {"models": [{"api": api, "baseUrl": base}]} for pid, (api, base) in providers.items()}
    (agent_dir / "models-store.json").write_text(json.dumps(store))


def _stub_checks(monkeypatch: pytest.MonkeyPatch, table: dict[str, tuple[str, str]]) -> None:
    """Replace ``pi auth check`` with a table of ready providers.

    *table* maps provider id → (authType, credential); providers absent from
    the table resolve to None (unconfigured).
    """

    def fake(provider_id: str) -> dict | None:
        if provider_id not in table:
            return None
        auth_type, credential = table[provider_id]
        return {"status": "ready", "provider": provider_id, "authType": auth_type, "credentials": credential}

    monkeypatch.setattr(pi_backend, "_run_pi_auth_check", fake)


def _fake_jwt(payload: dict) -> str:
    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestPiBackendConstants:
    def test_constants(self):
        assert agent.PI.kind == "PI"
        assert agent.PI.binary == "pi"
        assert agent.PI.supports_sessions is True
        assert agent.PI.session_id_pre_generated is True


# ---------------------------------------------------------------------------
# build_new_command / build_resume_command / build_finalize_command
# ---------------------------------------------------------------------------


class TestBuildNewCommand:
    def test_native(self):
        cmd = agent.PI.build_new_command("sid", "sys", "initial")
        assert cmd == ["pi", "--session-id", "sid", "--append-system-prompt", "sys", "initial"]

    def test_docker(self):
        cmd = agent.PI.build_new_command("sid", "sys", "initial", docker=True)
        assert cmd == [
            "sh",
            "-c",
            pi_backend.PiBackend._DOCKER_WRAPPER,
            "sh",
            "--session-id",
            "sid",
            "--append-system-prompt",
            "sys",
            "initial",
        ]


class TestBuildResumeCommand:
    def test_native(self):
        cmd = agent.PI.build_resume_command("sid", "sys")
        assert cmd == ["pi", "--session-id", "sid", "--append-system-prompt", "sys"]

    def test_docker(self):
        cmd = agent.PI.build_resume_command("sid", "sys", docker=True)
        assert cmd == [
            "sh",
            "-c",
            pi_backend.PiBackend._DOCKER_WRAPPER,
            "sh",
            "--session-id",
            "sid",
            "--append-system-prompt",
            "sys",
        ]


class TestBuildFinalizeCommand:
    def test_native(self):
        cmd = agent.PI.build_finalize_command("sid", "sys", "wrap up")
        assert cmd == [
            "pi",
            "--print",
            "--session-id",
            "sid",
            "--append-system-prompt",
            "sys",
            "wrap up",
        ]

    def test_docker(self):
        cmd = agent.PI.build_finalize_command("sid", "sys", "wrap up", docker=True)
        assert cmd == [
            "sh",
            "-c",
            pi_backend.PiBackend._DOCKER_WRAPPER,
            "sh",
            "--print",
            "--session-id",
            "sid",
            "--append-system-prompt",
            "sys",
            "wrap up",
        ]


# ---------------------------------------------------------------------------
# _run_pi_auth_check — the subprocess seam over `pi auth check`
# ---------------------------------------------------------------------------


def _install_pi_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    bin_dir = tmp_path / "pibin"
    bin_dir.mkdir()
    shim = bin_dir / "pi"
    shim.write_text(f"#!/bin/sh\n{body}\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{tmp_path}")


class TestRunPiAuthCheck:
    def test_ready_returns_parsed_json(self, tmp_path, monkeypatch):
        _install_pi_shim(
            tmp_path,
            monkeypatch,
            'echo \'{"status":"ready","provider":"acme","authType":"api_key","credentials":"sk-real"}\'',
        )
        assert pi_backend._run_pi_auth_check("acme") == {
            "status": "ready",
            "provider": "acme",
            "authType": "api_key",
            "credentials": "sk-real",
        }

    def test_not_ready_exit_code_returns_none(self, tmp_path, monkeypatch):
        _install_pi_shim(tmp_path, monkeypatch, 'echo \'{"status":"not_ready"}\'; exit 1')
        assert pi_backend._run_pi_auth_check("acme") is None

    def test_malformed_output_returns_none(self, tmp_path, monkeypatch):
        _install_pi_shim(tmp_path, monkeypatch, "echo not-json")
        assert pi_backend._run_pi_auth_check("acme") is None

    def test_pi_not_on_path_returns_none(self, tmp_path, monkeypatch):
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        assert pi_backend._run_pi_auth_check("acme") is None


# ---------------------------------------------------------------------------
# proxy_endpoints
# ---------------------------------------------------------------------------


class TestProxyEndpoints:
    def test_raises_when_nothing_resolves(self, home, monkeypatch):
        _write_store(home, _STORE)
        _stub_checks(monkeypatch, {})
        with pytest.raises(RuntimeError, match="no pi provider credentials resolved"):
            agent.PI.proxy_endpoints()

    def test_discovers_only_configured_store_providers(self, home, monkeypatch):
        _write_store(home, _STORE)
        _stub_checks(monkeypatch, {"acme": ("api_key", "sk-acme"), "huggingface": ("api_key", "sk-hf")})
        endpoints = {e.key: e for e in agent.PI.proxy_endpoints()}
        assert set(endpoints) == {"acme", "huggingface"}

    def test_target_host_derived_from_store_base_url(self, home, monkeypatch):
        _write_store(home, _STORE)
        _stub_checks(monkeypatch, {"openai": ("api_key", "sk-openai"), "huggingface": ("api_key", "sk-hf")})
        endpoints = {e.key: e for e in agent.PI.proxy_endpoints()}
        assert endpoints["openai"].target_host == "api.openai.com"
        assert endpoints["huggingface"].target_host == "router.huggingface.co"
        assert endpoints["openai"].path_prefix == ""

    def test_openai_codex_probed_even_though_absent_from_store(self, home, monkeypatch):
        _write_store(home, _STORE)
        _stub_checks(monkeypatch, {"openai-codex": ("oauth", _fake_jwt({"sub": "u"}))})
        (endpoint,) = agent.PI.proxy_endpoints()
        assert endpoint.key == "openai-codex"
        assert endpoint.target_host == "chatgpt.com"


# ---------------------------------------------------------------------------
# Header mutators (driven through proxy_endpoints)
# ---------------------------------------------------------------------------


def _mutator_for(home, monkeypatch, provider_id, auth_type, credential):
    _write_store(home, _STORE)
    _stub_checks(monkeypatch, {provider_id: (auth_type, credential)})
    endpoints = {e.key: e for e in agent.PI.proxy_endpoints()}
    return endpoints[provider_id].header_mutator


class TestHeaderMutators:
    """The injection mutator only swaps the fake token for the real secret in
    whichever auth header pi sent; pi shapes every other header itself, and the
    mutator leaves those untouched. One mutator serves every provider."""

    def test_swaps_x_api_key_value(self, home, monkeypatch):
        mutate = _mutator_for(home, monkeypatch, "acme", "api_key", "sk-real")
        result = mutate({"x-api-key": "proxy-tok", "content-type": "application/json"})
        assert result == {"content-type": "application/json", "x-api-key": "sk-real"}

    def test_swaps_bearer_token_value(self, home, monkeypatch):
        mutate = _mutator_for(home, monkeypatch, "openai", "api_key", "sk-real")
        result = mutate({"authorization": "Bearer proxy-tok"})
        assert result == {"authorization": "Bearer sk-real"}

    def test_same_injector_for_every_provider(self, home, monkeypatch):
        # huggingface is openai-completions — an arbitrary store provider we
        # wrote no code for. It goes through the identical swap.
        mutate = _mutator_for(home, monkeypatch, "huggingface", "api_key", "hf-real")
        assert mutate({"authorization": "Bearer proxy-tok"}) == {"authorization": "Bearer hf-real"}

    def test_preserves_headers_pi_already_shaped(self, home, monkeypatch):
        # pi sets any provider-specific headers itself (oauth betas,
        # chatgpt-account-id + originator, SDK markers). The mutator swaps only
        # the auth value and must pass everything else through untouched.
        mutate = _mutator_for(home, monkeypatch, "acme", "oauth", "oauth-real")
        result = mutate(
            {
                "authorization": "Bearer proxy-tok",
                "x-app": "cli",
                "x-stainless-lang": "js",
            }
        )
        assert result == {
            "authorization": "Bearer oauth-real",
            "x-app": "cli",
            "x-stainless-lang": "js",
        }

    def test_leaves_non_bearer_authorization_untouched(self, home, monkeypatch):
        mutate = _mutator_for(home, monkeypatch, "openai", "api_key", "sk-real")
        assert mutate({"authorization": "Basic abc"}) == {"authorization": "Basic abc"}


# ---------------------------------------------------------------------------
# openai-codex sentinel — the fake token pi decodes for the account id
# ---------------------------------------------------------------------------


class TestOpenAiCodexSentinel:
    """openai-codex reads its ChatGPT account id from its *local* token, so its
    endpoint hands the container a fake JWT carrying the real id (rather than
    the shared proxy token) — letting pi emit the header itself."""

    def _codex_endpoint(self, home, monkeypatch, token):
        _write_store(home, _STORE)
        _stub_checks(monkeypatch, {"openai-codex": ("oauth", token)})
        (endpoint,) = agent.PI.proxy_endpoints()
        return endpoint

    def test_container_token_embeds_real_account_id(self, home, monkeypatch):
        token = _fake_jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-123"}})
        endpoint = self._codex_endpoint(home, monkeypatch, token)
        # pi will decode endpoint.container_token client-side; it must yield the
        # real id, not the shared token's dummy.
        assert pi_backend._chatgpt_account_id(endpoint.container_token) == "acct-123"

    def test_no_account_id_claim_leaves_container_token_none(self, home, monkeypatch):
        endpoint = self._codex_endpoint(home, monkeypatch, _fake_jwt({"sub": "user-1"}))
        assert endpoint.container_token is None

    def test_malformed_token_leaves_container_token_none(self, home, monkeypatch):
        endpoint = self._codex_endpoint(home, monkeypatch, "not-a-jwt")
        assert endpoint.container_token is None

    def test_store_provider_has_no_container_token(self, home, monkeypatch):
        _write_store(home, _STORE)
        _stub_checks(monkeypatch, {"acme": ("api_key", "sk-acme")})
        (endpoint,) = agent.PI.proxy_endpoints()
        assert endpoint.container_token is None


# ---------------------------------------------------------------------------
# _refresh — re-runs `pi auth check` and adopts the fresh token
# ---------------------------------------------------------------------------


class TestRefresh:
    def test_oauth_mutator_refresh_adopts_new_token(self, home, monkeypatch):
        tokens = iter(["stale", "fresh"])

        def fake(provider_id):
            if provider_id != "acme":
                return None
            return {"status": "ready", "provider": "acme", "authType": "oauth", "credentials": next(tokens)}

        monkeypatch.setattr(pi_backend, "_run_pi_auth_check", fake)
        _write_store(home, _STORE)
        (endpoint,) = agent.PI.proxy_endpoints()
        assert endpoint.key == "acme"
        inbound = {"authorization": "Bearer proxy-tok"}
        assert endpoint.header_mutator(inbound)["authorization"] == "Bearer stale"
        assert endpoint.header_mutator(inbound, refresh=True)["authorization"] == "Bearer fresh"


# ---------------------------------------------------------------------------
# container_env
# ---------------------------------------------------------------------------


class TestContainerEnv:
    def test_no_base_path(self, home):
        _write_store(home, _STORE)
        assert agent.PI.container_env("acme", "proxy-tok", 9999) == {
            "HATCHERY_PI_ACME_ID": "acme",
            "HATCHERY_PI_ACME_BASEURL": "http://host.docker.internal:9999",
            "HATCHERY_PI_ACME_KEY": "proxy-tok",
            "HATCHERY_PI_ACME_SHAPE": "api_key",
        }

    def test_openai_carries_base_path(self, home):
        _write_store(home, _STORE)
        assert agent.PI.container_env("openai", "proxy-tok", 9999) == {
            "HATCHERY_PI_OPENAI_ID": "openai",
            "HATCHERY_PI_OPENAI_BASEURL": "http://host.docker.internal:9999/v1",
            "HATCHERY_PI_OPENAI_KEY": "proxy-tok",
            "HATCHERY_PI_OPENAI_SHAPE": "api_key",
        }

    def test_openai_codex_is_oauth_shaped_and_hardcoded_path(self):
        assert agent.PI.container_env("openai-codex", "proxy-tok", 9999) == {
            "HATCHERY_PI_OPENAI_CODEX_ID": "openai-codex",
            "HATCHERY_PI_OPENAI_CODEX_BASEURL": "http://host.docker.internal:9999/backend-api",
            "HATCHERY_PI_OPENAI_CODEX_KEY": "proxy-tok",
            "HATCHERY_PI_OPENAI_CODEX_SHAPE": "oauth",
        }


# ---------------------------------------------------------------------------
# construct_mounts
# ---------------------------------------------------------------------------


class TestConstructMounts:
    def test_returns_only_volume_when_no_host_config(self, tmp_path):
        # autouse ``home`` fixture points Path.home() at an empty temp dir,
        # so neither settings.json nor models-store.json exists.
        mounts = agent.PI.construct_mounts(tmp_path)
        assert len(mounts) == 1
        (m,) = mounts
        assert isinstance(m, mount.VolumeMount)
        assert m.dst == f"{agent.CONTAINER_HOME}/.pi/agent"
        assert m.seed is None

    def test_layers_host_config_binds_over_volume(self, home, tmp_path):
        agent_dir = home / ".pi" / "agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "settings.json").write_text("{}")
        (agent_dir / "models-store.json").write_text("{}")

        vol, settings, store = agent.PI.construct_mounts(tmp_path)

        assert isinstance(vol, mount.VolumeMount)
        assert vol.dst == f"{agent.CONTAINER_HOME}/.pi/agent"
        assert (settings.src, settings.dst, settings.mode) == (
            agent_dir / "settings.json",
            f"{agent.CONTAINER_HOME}/.pi/agent/settings.json",
            "RW",
        )
        assert (store.src, store.dst, store.mode) == (
            agent_dir / "models-store.json",
            f"{agent.CONTAINER_HOME}/.pi/agent/models-store.json",
            "RO",
        )

    def test_never_binds_the_real_auth_json(self, home, tmp_path):
        agent_dir = home / ".pi" / "agent"
        agent_dir.mkdir(parents=True)
        for name in ("auth.json", "settings.json", "models-store.json"):
            (agent_dir / name).write_text("{}")

        mounts = agent.PI.construct_mounts(tmp_path)

        assert not any(isinstance(m, mount.BindMount) and Path(m.src).name == "auth.json" for m in mounts)


# ---------------------------------------------------------------------------
# on_new_task / on_before_launch / on_before_container_start
# ---------------------------------------------------------------------------


class TestLifecycleHooksAreNoops:
    def test_on_new_task(self, tmp_path):
        session_dir = tmp_path / "session"
        agent.PI.on_new_task(session_dir)
        assert not session_dir.exists()

    def test_on_before_launch(self, tmp_path):
        agent.PI.on_before_launch(tmp_path)  # should not raise

    def test_on_before_container_start(self, tmp_path):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        agent.PI.on_before_container_start(session_dir, "proxy-tok", "/workdir")
        assert list(session_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# _DOCKER_WRAPPER — security invariant: never leaks real credentials
# ---------------------------------------------------------------------------


class TestDockerWrapperSecurity:
    def test_writes_only_fake_proxy_creds_for_n_providers(self, tmp_path):
        """Run the real wrapper under sh with a fake $HOME and a `pi` shim on
        PATH. The written auth.json/models.json must contain only the fake
        proxy token and proxy-port URLs — never a real credential — for
        whatever providers container_env passed, api_key- or oauth-shaped per
        _SHAPE. The real ~/.pi/agent/auth.json is never mounted or read here.
        """
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        pi_shim = bin_dir / "pi"
        pi_shim.write_text("#!/bin/sh\nexit 0\n")
        pi_shim.chmod(0o755)

        env = {
            "HOME": str(fake_home),
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HATCHERY_PI_ACME_ID": "acme",
            "HATCHERY_PI_ACME_BASEURL": "http://host.docker.internal:12345",
            "HATCHERY_PI_ACME_KEY": "proxy-token-aaa",
            "HATCHERY_PI_ACME_SHAPE": "api_key",
            "HATCHERY_PI_HUGGINGFACE_ID": "huggingface",
            "HATCHERY_PI_HUGGINGFACE_BASEURL": "http://host.docker.internal:12345/v1",
            "HATCHERY_PI_HUGGINGFACE_KEY": "proxy-token-aaa",
            "HATCHERY_PI_HUGGINGFACE_SHAPE": "api_key",
            "HATCHERY_PI_OPENAI_CODEX_ID": "openai-codex",
            "HATCHERY_PI_OPENAI_CODEX_BASEURL": "http://host.docker.internal:12345/backend-api",
            "HATCHERY_PI_OPENAI_CODEX_KEY": "proxy-token-aaa",
            "HATCHERY_PI_OPENAI_CODEX_SHAPE": "oauth",
        }
        result = subprocess.run(
            ["sh", "-c", pi_backend.PiBackend._DOCKER_WRAPPER, "sh"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

        auth = json.loads((fake_home / ".pi" / "agent" / "auth.json").read_text())
        models = json.loads((fake_home / ".pi" / "agent" / "models.json").read_text())

        assert auth == {
            "acme": {"type": "api_key", "key": "proxy-token-aaa"},
            "huggingface": {"type": "api_key", "key": "proxy-token-aaa"},
            "openai-codex": {
                "type": "oauth",
                "access": "proxy-token-aaa",
                "refresh": "",
                "expires": 4102444800000,
            },
        }
        assert models == {
            "providers": {
                "acme": {"baseUrl": "http://host.docker.internal:12345", "apiKey": "proxy-token-aaa"},
                "huggingface": {"baseUrl": "http://host.docker.internal:12345/v1", "apiKey": "proxy-token-aaa"},
                "openai-codex": {
                    "baseUrl": "http://host.docker.internal:12345/backend-api",
                    "apiKey": "proxy-token-aaa",
                },
            }
        }
        dumped = json.dumps(auth) + json.dumps(models)
        assert "sk-" not in dumped
        assert "real" not in dumped


# ---------------------------------------------------------------------------
# dockerfile_install
# ---------------------------------------------------------------------------


class TestDockerfileInstall:
    def test_installs_node_and_pi(self):
        install = agent.PI.dockerfile_install
        assert "nodesource" in install.lower()
        assert "@earendil-works/pi-coding-agent" in install

    def test_installs_search_tools_to_avoid_runtime_download(self):
        install = agent.PI.dockerfile_install
        assert "ripgrep" in install
        assert "fd-find" in install
