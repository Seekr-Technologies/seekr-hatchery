"""Unit tests for PiBackend."""

import base64
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest

import seekr_hatchery.agents as agent
import seekr_hatchery.agents.pi.backend as pi_backend
import seekr_hatchery.docker as docker
import seekr_hatchery.mount as mount
import seekr_hatchery.mount_links as mount_links
import seekr_hatchery.seeded_volumes as seeded_volumes

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


def _write_models(home: Path, text: str) -> None:
    agent_dir = home / ".pi" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "models.json").write_text(text)


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


def _path_with_pi_shim(bin_dir: Path) -> str:
    """Keep the test Pi shim first without hiding Node's host location."""
    if shutil.which("node") is None:
        pytest.skip("Pi's container wrapper requires Node.js")
    return f"{bin_dir}:{os.environ.get('PATH', os.defpath)}"


def _fake_jwt(payload: dict) -> str:
    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


def _ep(key: str, *, oauth: bool = False) -> agent.ProxyEndpoint:
    return agent.ProxyEndpoint(key=key, header_mutator=lambda h: h, target_host="x", container_oauth=oauth)


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
# host models.json
# ---------------------------------------------------------------------------


class TestHostModelsConfig:
    def test_accepts_pi_jsonc_syntax(self, home):
        _write_models(
            home,
            """{
              // Custom local gateway
              "providers": {
                "local": {
                  "baseUrl": "http://localhost:8080/v1", // inline comment
                  "api": "openai-completions",
                  "models": [{"id": "coder"}],
                },
              },
            }""",
        )

        assert pi_backend._read_models_config() == {
            "providers": {
                "local": {
                    "baseUrl": "http://localhost:8080/v1",
                    "api": "openai-completions",
                    "models": [{"id": "coder"}],
                }
            }
        }

    def test_invalid_file_returns_empty_config(self, home):
        _write_models(home, "{not valid")
        assert pi_backend._read_models_config() == {}


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

    def test_openai_codex_discovered_from_store_like_any_provider(self, home, monkeypatch):
        # A ChatGPT login makes pi's catalog refresh add openai-codex to the
        # store; from there it is discovered and routed with no special-casing.
        _write_store(home, {**_STORE, "openai-codex": ("openai-codex-responses", "https://chatgpt.com/backend-api")})
        _stub_checks(monkeypatch, {"openai-codex": ("oauth", _fake_jwt({"sub": "u"}))})
        (endpoint,) = agent.PI.proxy_endpoints()
        assert endpoint.key == "openai-codex"
        assert endpoint.target_host == "chatgpt.com"

    def test_discovers_custom_models_json_provider(self, home, monkeypatch):
        _write_store(home, _STORE)
        _write_models(
            home,
            json.dumps(
                {
                    "providers": {
                        "custom": {
                            "baseUrl": "https://models.example.com/v1",
                            "api": "openai-completions",
                            "apiKey": "sk-host",
                            "models": [{"id": "coder"}],
                        }
                    }
                }
            ),
        )
        _stub_checks(monkeypatch, {"custom": ("api_key", "sk-host")})

        (endpoint,) = agent.PI.proxy_endpoints()

        assert (endpoint.key, endpoint.target_host) == ("custom", "models.example.com")

    def test_models_json_overrides_store_endpoint(self, home, monkeypatch):
        _write_store(home, _STORE)
        _write_models(home, json.dumps({"providers": {"openai": {"baseUrl": "https://gateway.example/v2"}}}))
        _stub_checks(monkeypatch, {"openai": ("api_key", "sk-openai")})

        (endpoint,) = agent.PI.proxy_endpoints()

        assert (endpoint.key, endpoint.target_host) == ("openai", "gateway.example")

    def test_discovers_custom_model_level_base_url(self, home, monkeypatch):
        _write_models(
            home,
            json.dumps(
                {
                    "providers": {
                        "custom": {
                            "models": [
                                {
                                    "id": "coder",
                                    "api": "openai-completions",
                                    "baseUrl": "https://models.example.com/v1",
                                }
                            ]
                        }
                    }
                }
            ),
        )
        _stub_checks(monkeypatch, {"custom": ("api_key", "sk-custom")})

        (endpoint,) = agent.PI.proxy_endpoints()

        assert (endpoint.key, endpoint.target_host) == ("custom", "models.example.com")

    def test_provider_base_url_overrides_catalog_model_origins(self, home, monkeypatch):
        _write_store(home, {"mixed": ("openai-completions", "https://catalog-one.example/v1")})
        store_path = home / ".pi" / "agent" / "models-store.json"
        store = json.loads(store_path.read_text())
        store["mixed"]["models"].append(
            {"id": "second", "api": "openai-completions", "baseUrl": "https://catalog-two.example/v1"}
        )
        store_path.write_text(json.dumps(store))
        _write_models(home, json.dumps({"providers": {"mixed": {"baseUrl": "https://gateway.example/v2"}}}))
        _stub_checks(monkeypatch, {"mixed": ("api_key", "sk-mixed")})

        (endpoint,) = agent.PI.proxy_endpoints()

        assert (endpoint.key, endpoint.target_host) == ("mixed", "gateway.example")

    def test_model_base_url_overrides_provider_and_matching_catalog_defaults(self, home, monkeypatch):
        _write_store(home, {"custom": ("openai-completions", "https://catalog.example/v1")})
        store_path = home / ".pi" / "agent" / "models-store.json"
        store = json.loads(store_path.read_text())
        store["custom"]["models"][0]["id"] = "coder"
        store_path.write_text(json.dumps(store))
        _write_models(
            home,
            json.dumps(
                {
                    "providers": {
                        "custom": {
                            "baseUrl": "https://provider.example/v1",
                            "models": [{"id": "coder", "baseUrl": "https://model.example/v1"}],
                        }
                    }
                }
            ),
        )
        _stub_checks(monkeypatch, {"custom": ("api_key", "sk-custom")})

        (endpoint,) = agent.PI.proxy_endpoints()

        assert (endpoint.key, endpoint.target_host) == ("custom", "model.example")

    @pytest.mark.parametrize(
        "custom_url",
        [
            "https://custom.example/v1?query-secret#fragment-secret",
            "https://catalog.example:8443/v1",
            "http://catalog.example/v1",
        ],
        ids=["host", "port", "scheme"],
    )
    def test_rejects_provider_with_differing_effective_model_origins(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, custom_url: str
    ) -> None:
        _write_store(home, {"mixed": ("openai-completions", "https://catalog.example/v1")})
        _write_models(
            home,
            json.dumps(
                {
                    "providers": {
                        "mixed": {
                            "models": [
                                {
                                    "id": "custom",
                                    "baseUrl": custom_url,
                                }
                            ]
                        }
                    }
                }
            ),
        )
        _stub_checks(monkeypatch, {"mixed": ("api_key", "sk-mixed")})

        with pytest.raises(RuntimeError) as error:
            agent.PI.proxy_endpoints()

        assert str(error.value) == (
            "pi provider 'mixed' uses multiple upstream origins; configure its model and provider base URLs "
            "to use one origin before starting a container"
        )

    @pytest.mark.parametrize("explicit_first", [False, True])
    def test_accepts_equivalent_default_https_ports(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, explicit_first: bool
    ) -> None:
        urls = ["https://gateway.example/api", "https://gateway.example:443/api/v1"]
        if explicit_first:
            urls.reverse()
        _write_store(home, {"custom": ("openai-completions", urls[0])})
        _write_models(home, json.dumps({"providers": {"custom": {"models": [{"id": "second", "baseUrl": urls[1]}]}}}))
        _stub_checks(monkeypatch, {"custom": ("api_key", "sk-custom")})

        endpoints = agent.PI.proxy_endpoints()

        assert [(endpoint.key, endpoint.target_host) for endpoint in endpoints] == [("custom", "gateway.example")]

    def test_accepts_distinct_model_paths_on_one_origin(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_store(home, {"custom": ("openai-completions", "https://gateway.example/api")})
        _write_models(
            home,
            json.dumps(
                {"providers": {"custom": {"models": [{"id": "second", "baseUrl": "https://gateway.example/api/v1"}]}}}
            ),
        )
        _stub_checks(monkeypatch, {"custom": ("api_key", "sk-custom")})

        endpoints = agent.PI.proxy_endpoints()

        assert [(endpoint.key, endpoint.target_host) for endpoint in endpoints] == [("custom", "gateway.example")]

    def test_container_oauth_tracks_resolved_auth_type(self, home, monkeypatch):
        _write_store(home, _STORE)
        _stub_checks(
            monkeypatch,
            {"acme": ("oauth", "oauth-acme"), "huggingface": ("api_key", "sk-hf")},
        )
        endpoints = {e.key: e for e in agent.PI.proxy_endpoints()}
        assert endpoints["acme"].container_oauth is True
        assert endpoints["huggingface"].container_oauth is False


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
        _write_store(home, {"openai-codex": ("openai-codex-responses", "https://chatgpt.com/backend-api")})
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
    def test_uses_proxy_origin_without_provider_base_path(self, home):
        _write_store(home, _STORE)
        assert agent.PI.container_env(_ep("acme"), "proxy-tok", 9999) == {
            "HATCHERY_PI_ACME_ID": "acme",
            "HATCHERY_PI_ACME_PROXY_ORIGIN": "http://host.docker.internal:9999",
            "HATCHERY_PI_ACME_KEY": "proxy-tok",
            "HATCHERY_PI_ACME_SHAPE": "api_key",
        }

    def test_openai_uses_the_same_proxy_origin(self, home):
        _write_store(home, _STORE)
        assert agent.PI.container_env(_ep("openai"), "proxy-tok", 9999) == {
            "HATCHERY_PI_OPENAI_ID": "openai",
            "HATCHERY_PI_OPENAI_PROXY_ORIGIN": "http://host.docker.internal:9999",
            "HATCHERY_PI_OPENAI_KEY": "proxy-tok",
            "HATCHERY_PI_OPENAI_SHAPE": "api_key",
        }

    def test_openai_codex_is_oauth_shaped_without_store_base_path(self, home):
        _write_store(home, {"openai-codex": ("openai-codex-responses", "https://chatgpt.com/backend-api")})
        assert agent.PI.container_env(_ep("openai-codex", oauth=True), "proxy-tok", 9999) == {
            "HATCHERY_PI_OPENAI_CODEX_ID": "openai-codex",
            "HATCHERY_PI_OPENAI_CODEX_PROXY_ORIGIN": "http://host.docker.internal:9999",
            "HATCHERY_PI_OPENAI_CODEX_KEY": "proxy-tok",
            "HATCHERY_PI_OPENAI_CODEX_SHAPE": "oauth",
        }

    def test_shape_follows_container_oauth_not_provider_id(self, home):
        # An OAuth-resolved credential yields an oauth-shaped entry for ANY
        # provider — the shape tracks the endpoint's resolved auth, not a
        # hard-coded provider list.
        _write_store(home, _STORE)
        env = agent.PI.container_env(_ep("acme", oauth=True), "proxy-tok", 9999)
        assert env["HATCHERY_PI_ACME_SHAPE"] == "oauth"


# ---------------------------------------------------------------------------
# construct_mounts
# ---------------------------------------------------------------------------


class TestConstructMounts:
    def test_shares_agent_directory_with_task_local_overlays(self, home, tmp_path):
        agent_dir = home / ".pi" / "agent"
        agent_dst = f"{agent.CONTAINER_HOME}/.pi/agent"

        assert agent.PI.construct_mounts(tmp_path) == [
            mount.BindMount(src=agent_dir, dst=agent_dst, follow_links="RECURSIVE"),
            mount.BindMount(src=tmp_path / "pi-config/auth.json", dst=f"{agent_dst}/auth.json"),
            mount.BindMount(src=tmp_path / "pi-config/models.json", dst=f"{agent_dst}/models.json"),
            mount.BindMount(src=tmp_path / "pi-config/models-store.json", dst=f"{agent_dst}/models-store.json"),
            mount.VolumeMount(name="pi-bin", dst=f"{agent_dst}/bin", seed=pi_backend._seed_empty_state),
            mount.VolumeMount(name="pi-sessions", dst=f"{agent_dst}/sessions", seed=pi_backend._seed_empty_state),
            mount.BindMount(
                src=tmp_path / pi_backend._HOST_MODELS_STORE_FILENAME,
                dst=f"{agent_dst}/{pi_backend._HOST_MODELS_STORE_FILENAME}",
                mode="RO",
            ),
        ]
        assert agent_dir.is_dir()
        assert {path.name: json.loads(path.read_text()) for path in (tmp_path / "pi-config").iterdir()} == {
            "auth.json": {},
            "models.json": {},
            "models-store.json": {},
        }
        assert list(agent_dir.iterdir()) == []

    def test_reconstruction_preserves_task_files_and_host_credentials(self, home, tmp_path):
        agent_dir = home / ".pi" / "agent"
        agent_dir.mkdir(parents=True)
        host_files = {
            "auth.json": "host-auth-secret",
            "models.json": "host-model-secret",
            "models-store.json": "host-catalog",
        }
        for name, content in host_files.items():
            (agent_dir / name).write_text(content)
        mounts = agent.PI.construct_mounts(tmp_path)
        for name in host_files:
            (tmp_path / "pi-config" / name).write_text("task-local")

        assert agent.PI.construct_mounts(tmp_path) == mounts
        assert {name: (agent_dir / name).read_text() for name in host_files} == host_files
        assert {name: (tmp_path / "pi-config" / name).read_text() for name in host_files} == {
            "auth.json": "task-local",
            "models.json": "task-local",
            "models-store.json": "task-local",
        }

    @pytest.mark.parametrize("catalog_shape", ["missing", "regular", "symlink"])
    def test_snapshots_host_catalog_into_isolated_task_input(
        self, home: Path, tmp_path: Path, catalog_shape: str
    ) -> None:
        agent_dir = home / ".pi/agent"
        agent_dir.mkdir(parents=True)
        catalog = {"openai": {"models": [{"id": "coder", "baseUrl": "https://gateway.example/v1"}]}}
        external = tmp_path / "external-catalog.json"
        external.write_text(json.dumps(catalog))
        host_catalog = agent_dir / "models-store.json"
        if catalog_shape == "regular":
            host_catalog.write_text(json.dumps(catalog))
        elif catalog_shape == "symlink":
            host_catalog.symlink_to(external)
        # A similarly named host file must not become the wrapper's input.
        (agent_dir / pi_backend._HOST_MODELS_STORE_FILENAME).write_text("untrusted-host-input")
        session_dir = tmp_path / "session"

        mounts = agent.PI.construct_mounts(session_dir)

        expected = {} if catalog_shape == "missing" else catalog
        snapshot = session_dir / pi_backend._HOST_MODELS_STORE_FILENAME
        assert (
            mount.BindMount(
                src=snapshot,
                dst=f"{agent.CONTAINER_HOME}/.pi/agent/{pi_backend._HOST_MODELS_STORE_FILENAME}",
                mode="RO",
            )
            in mounts
        )
        assert json.loads(snapshot.read_text()) == expected
        (session_dir / "pi-config/models-store.json").write_text("routed-task-catalog")
        assert json.loads(external.read_text()) == catalog
        if catalog_shape == "missing":
            assert not host_catalog.exists()
        else:
            assert json.loads(host_catalog.read_text()) == catalog
        assert (agent_dir / pi_backend._HOST_MODELS_STORE_FILENAME).read_text() == "untrusted-host-input"

    def test_mounts_sanitized_host_models_copy(self, home, tmp_path):
        session_models = tmp_path / pi_backend._HOST_MODELS_FILENAME
        session_models.write_text("{}")

        assert agent.PI.construct_mounts(tmp_path)[-1] == mount.BindMount(
            src=session_models,
            dst=f"{agent.CONTAINER_HOME}/.pi/agent/{pi_backend._HOST_MODELS_FILENAME}",
            mode="RO",
        )


@pytest.mark.integration
@pytest.mark.parametrize("credential_shape", ["missing", "regular", "symlink"])
def test_shared_agent_directory_in_container(home: Path, tmp_path: Path, credential_shape: str) -> None:
    if shutil.which("podman") is None or subprocess.run(["podman", "info"], capture_output=True).returncode:
        pytest.skip("podman unavailable")
    image = "docker.io/library/alpine:3.21"
    subprocess.run(["podman", "pull", "-q", image], check=True, capture_output=True)
    agent_dir = home / ".pi/agent"
    agent_dir.mkdir(parents=True)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name in ("auth.json", "models.json", "models-store.json"):
        (secrets / name).write_text(f"host-secret-{name}")
        if credential_shape == "regular":
            (agent_dir / name).write_text(f"host-secret-{name}")
        elif credential_shape == "symlink":
            (agent_dir / name).symlink_to(secrets / name)
    for name in ("bin", "sessions"):
        (agent_dir / name).mkdir()
        (agent_dir / name / "host-only").write_text("host-state")
    instructions = tmp_path / "instructions.md"
    instructions.write_text("user instructions")
    (agent_dir / "APPEND_SYSTEM.md").symlink_to(instructions)
    resource = tmp_path / "dotfile-custom-resources"
    resource.mkdir()
    dependency = tmp_path / "dependency.txt"
    dependency.write_text("dependency")
    (resource / "dependency.txt").symlink_to(dependency)
    resource_link = os.path.relpath(resource, agent_dir) if credential_shape == "regular" else str(resource)
    (agent_dir / "custom-resources").symlink_to(resource_link)
    (agent_dir / "agent-alias").symlink_to(agent_dir)
    (agent_dir / "parent-alias").symlink_to(agent_dir.parent)
    if credential_shape != "missing":
        (agent_dir / "credential-alias").symlink_to((agent_dir / "auth.json").resolve())
    session_dir = tmp_path / "task"
    prefix = f"pi-mount-test-{uuid.uuid4().hex}"
    meta = SimpleNamespace(container_name=prefix, image_name=image, session_dir=session_dir)
    agent.PI.migrate_container_state(meta, runtime=docker.PodmanRuntime())
    agent.PI.on_before_container_start(meta, "proxy-token", "/work", runtime=docker.PodmanRuntime())
    mounts = mount_links.expand_link_mounts(agent.PI.construct_mounts(session_dir))
    mounts = seeded_volumes.prepare_volume_mounts("podman", mounts, meta, session_dir, "proxy-token", "/work")
    volumes = [item.name for item in mounts if isinstance(item, mount.VolumeMount)]
    args = [arg for item in mounts for arg in mount.mount_to_docker_args(item)]
    dst = f"{agent.CONTAINER_HOME}/.pi/agent"
    try:
        # Fresh state overlays must be writable by the image\'s hatchery UID,
        # independently of the host directory\'s ownership.
        for volume in volumes:
            subprocess.run(
                [
                    "podman",
                    "run",
                    "--rm",
                    "--user",
                    "1000:1000",
                    "-v",
                    f"{volume}:/state",
                    image,
                    "sh",
                    "-ec",
                    "touch /state/nonroot-write",
                ],
                check=True,
                capture_output=True,
            )
        subprocess.run(
            [
                "podman",
                "run",
                "--rm",
                *args,
                image,
                "sh",
                "-ec",
                f"""
                cd {dst}
                test "$(cat auth.json)" = '{{}}'
                test "$(cat models.json)" = '{{}}'
                test "$(cat models-store.json)" = '{{}}'
                test "$(cat APPEND_SYSTEM.md)" = 'user instructions'
                test "$(cat custom-resources/dependency.txt)" = dependency
                test ! -e agent-alias/auth.json
                test ! -e parent-alias/agent/models.json
                if test -e credential-alias; then
                    test "$(cat credential-alias)" = '{{}}'
                    echo task-alias > credential-alias
                    test "$(cat auth.json)" = task-alias
                    echo '{{}}' > auth.json
                fi
                if echo overwritten > agent-alias/auth.json 2>/dev/null; then exit 1; fi
                if echo overwritten > parent-alias/agent/models.json 2>/dev/null; then exit 1; fi
                test ! -e bin/host-only
                test ! -e sessions/host-only
                echo task-auth > auth.json
                echo task-models > models.json
                echo task-catalog > models-store.json
                echo task-bin > bin/task-only
                echo task-session > sessions/task-only
                echo shared > new-user-file
                echo updated > APPEND_SYSTEM.md
            """,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "podman",
                "run",
                "--rm",
                *args,
                image,
                "sh",
                "-ec",
                f"""
                cd {dst}
                test "$(cat auth.json)" = task-auth
                test "$(cat models.json)" = task-models
                test "$(cat models-store.json)" = task-catalog
                test "$(cat bin/task-only)" = task-bin
                test "$(cat sessions/task-only)" = task-session
            """,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        for volume in volumes:
            subprocess.run(["podman", "volume", "rm", "--force", volume], capture_output=True, check=True)
    assert instructions.read_text() == "updated\n"
    assert (agent_dir / "new-user-file").read_text() == "shared\n"
    for name in ("auth.json", "models.json", "models-store.json"):
        assert (secrets / name).read_text() == f"host-secret-{name}"
        if credential_shape != "missing":
            assert (agent_dir / name).read_text() == f"host-secret-{name}"
    for name in ("bin", "sessions"):
        assert {p.name: p.read_text() for p in (agent_dir / name).iterdir()} == {"host-only": "host-state"}


# ---------------------------------------------------------------------------
# on_new_task / on_before_launch / on_before_container_start
# ---------------------------------------------------------------------------


class TestLifecycleHooks:
    def test_on_new_task(self, tmp_path):
        session_dir = tmp_path / "session"
        agent.PI.on_new_task(session_dir)
        assert not session_dir.exists()

    def test_on_before_launch(self, tmp_path):
        agent.PI.on_before_launch(tmp_path)  # should not raise

    def test_on_before_container_start_without_host_models(self, tmp_path, pi_meta):
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        agent.PI.on_before_container_start(pi_meta, "proxy-tok", "/workdir", runtime=docker.PodmanRuntime())
        assert {path.name: path.read_text() for path in session_dir.iterdir()} == {
            pi_backend._HOST_MODELS_FILENAME: "{}"
        }

    def test_on_before_container_start_sanitizes_host_models(self, home, tmp_path, pi_meta):
        _write_models(
            home,
            json.dumps(
                {
                    "$schema": "https://pi.dev/schemas/models.schema.json",
                    "providers": {
                        "custom": {
                            "baseUrl": "https://models.example.com/v1",
                            "api": "openai-completions",
                            "apiKey": "sk-host-secret",
                            "headers": {"Authorization": "Bearer host-secret", "x-feature": "enabled"},
                            "models": [
                                {
                                    "id": "coder",
                                    "headers": {"x-api-key": "model-secret", "x-model-option": "yes"},
                                }
                            ],
                        }
                    },
                }
            ),
        )
        session_dir = tmp_path / "session"

        agent.PI.on_before_container_start(pi_meta, "proxy-tok", "/workdir", runtime=docker.PodmanRuntime())

        written = json.loads((session_dir / pi_backend._HOST_MODELS_FILENAME).read_text())
        assert written == {
            "$schema": "https://pi.dev/schemas/models.schema.json",
            "providers": {
                "custom": {
                    "baseUrl": "https://models.example.com/v1",
                    "api": "openai-completions",
                    "headers": {"Authorization": "Bearer proxy-tok", "x-feature": "enabled"},
                    "models": [
                        {
                            "id": "coder",
                            "headers": {"x-api-key": "proxy-tok", "x-model-option": "yes"},
                        }
                    ],
                }
            },
        }
        assert "host-secret" not in json.dumps(written)
        assert "sk-host-secret" not in json.dumps(written)


# ---------------------------------------------------------------------------
# _DOCKER_WRAPPER — security invariant: never leaks real credentials
# ---------------------------------------------------------------------------


class TestDockerWrapperSecurity:
    @pytest.mark.parametrize("provider_id", ["openai-codex", "openai"])
    @pytest.mark.parametrize("last_modified", [None, 1], ids=["timestamp-free", "stale"])
    def test_routes_models_even_when_pi_ignores_cached_catalog(
        self, tmp_path: Path, provider_id: str, last_modified: int | None
    ) -> None:
        pi = shutil.which("pi")
        if pi is None:
            pytest.skip("Real Pi runtime regression requires an installed Pi CLI")
        runtime_path = next(
            (
                parent / "dist/core/model-runtime.js"
                for parent in Path(pi).resolve().parents
                if (parent / "dist/core/model-runtime.js").is_file()
            ),
            None,
        )
        if runtime_path is None:
            pytest.skip("Installed Pi does not expose ModelRuntime")
        agent_dir = tmp_path / ".pi/agent"
        agent_dir.mkdir(parents=True)
        env = {**os.environ, "HOME": str(tmp_path), "PI_OFFLINE": "1"}
        runtime_script = f"""
import {{ ModelRuntime }} from {json.dumps(runtime_path.as_uri())};
const dir = process.env.HOME + "/.pi/agent";
const runtime = await ModelRuntime.create({{
  modelsPath: dir + "/models.json", authPath: dir + "/auth.json",
  modelsStorePath: dir + "/models-store.json", allowModelNetwork: false,
}});
const models = runtime.getModels({json.dumps(provider_id)});
console.log(JSON.stringify(models));
"""
        baseline = subprocess.run(
            ["node", "--input-type=module", "-e", runtime_script], env=env, capture_output=True, text=True, check=True
        )
        catalog_models = json.loads(baseline.stdout)[:2]
        assert len(catalog_models) == 2
        first, second = catalog_models
        # Distinct paths in a catalog Pi will ignore must still be authoritative.
        first["baseUrl"] = "https://gateway.example/first"
        second["baseUrl"] = "https://gateway.example/second"
        catalog = {"models": catalog_models}
        if last_modified is not None:
            catalog["lastModified"] = last_modified
        (agent_dir / pi_backend._HOST_MODELS_STORE_FILENAME).write_text(json.dumps({provider_id: catalog}))
        (agent_dir / pi_backend._HOST_MODELS_FILENAME).write_text(
            json.dumps(
                {
                    "providers": {
                        provider_id: {"models": [{"id": first["id"], "baseUrl": "https://gateway.example/custom"}]}
                    }
                }
            )
        )
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        pi_shim = bin_dir / "pi"
        pi_shim.write_text("#!/bin/sh\nexit 0\n")
        pi_shim.chmod(0o755)
        prefix = "HATCHERY_PI_" + provider_id.upper().replace("-", "_")
        env.update(
            {
                "PATH": _path_with_pi_shim(bin_dir),
                f"{prefix}_ID": provider_id,
                f"{prefix}_PROXY_ORIGIN": "http://host.docker.internal:12345",
                f"{prefix}_KEY": "proxy-token",
                f"{prefix}_SHAPE": "api_key",
            }
        )
        subprocess.run(
            ["sh", "-c", agent.PI._DOCKER_WRAPPER, "sh"], env=env, capture_output=True, text=True, check=True
        )

        resolved = subprocess.run(
            ["node", "--input-type=module", "-e", runtime_script], env=env, capture_output=True, text=True, check=True
        )

        resolved_models = json.loads(resolved.stdout)
        resolved_urls = [urlsplit(model["baseUrl"]) for model in resolved_models]
        assert [(url.scheme, url.hostname, url.port) for url in resolved_urls] == [
            ("http", "host.docker.internal", 12345)
        ] * len(resolved_models)
        assert [(model["id"], model["baseUrl"]) for model in resolved_models[:2]] == [
            (first["id"], "http://host.docker.internal:12345/custom"),
            (second["id"], "http://host.docker.internal:12345/second"),
        ]

    @pytest.mark.parametrize("existing_mode", [None, 0o600], ids=["new", "resume"])
    def test_replaces_catalog_on_launch_without_changing_session_state(
        self, tmp_path: Path, existing_mode: int | None
    ) -> None:
        agent_dir = tmp_path / ".pi" / "agent"
        agent_dir.mkdir(parents=True)
        catalog = agent_dir / "models-store.json"
        if existing_mode is not None:
            catalog.write_text('{"old": true}')
            catalog.chmod(existing_mode)
        original_inode = catalog.stat().st_ino if catalog.exists() else None
        source = agent_dir / pi_backend._HOST_MODELS_STORE_FILENAME
        source.write_text(
            '{"openrouter":{"models":[{"id":"coder","api":"openai-completions",'
            '"baseUrl":"https://openrouter.ai/api/v1"}]}}'
        )
        session = agent_dir / "sessions" / "existing.jsonl"
        session.parent.mkdir()
        session.write_text("existing session\n")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        pi_shim = bin_dir / "pi"
        pi_shim.write_text("#!/bin/sh\nexit 0\n")
        pi_shim.chmod(0o755)
        env = {
            "HOME": str(tmp_path),
            "PATH": _path_with_pi_shim(bin_dir),
            "HATCHERY_PI_OPENROUTER_ID": "openrouter",
            "HATCHERY_PI_OPENROUTER_PROXY_ORIGIN": "http://host.docker.internal:12345",
            "HATCHERY_PI_OPENROUTER_KEY": "proxy-token",
            "HATCHERY_PI_OPENROUTER_SHAPE": "api_key",
        }

        result = subprocess.run(
            ["sh", "-c", pi_backend.PiBackend._DOCKER_WRAPPER, "sh"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert (result.returncode, result.stderr) == (0, "")
        if original_inode is not None:
            assert catalog.stat().st_ino == original_inode  # Single-file bind mounts require in-place writes.
        assert json.loads(catalog.read_text()) == {
            "openrouter": {
                "models": [
                    {
                        "id": "coder",
                        "api": "openai-completions",
                        "baseUrl": "http://host.docker.internal:12345/api/v1",
                    }
                ]
            }
        }
        assert session.read_text() == "existing session\n"
        assert json.loads(source.read_text()) == {
            "openrouter": {
                "models": [{"id": "coder", "api": "openai-completions", "baseUrl": "https://openrouter.ai/api/v1"}]
            }
        }
        assert sorted(item.name for item in agent_dir.iterdir()) == [
            "auth.json",
            pi_backend._HOST_MODELS_STORE_FILENAME,
            "models-store.json",
            "models.json",
            "sessions",
        ]

    @pytest.mark.parametrize("caller_offline", [None, "0"])
    def test_exports_offline_mode_to_pi_and_its_children(self, tmp_path: Path, caller_offline: str | None) -> None:
        fake_home = tmp_path / "fakehome"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        pi_shim = bin_dir / "pi"
        pi_shim.write_text(
            r"""#!/bin/sh
printf '%s\n' "$PI_OFFLINE"
sh -c 'printf "%s\n" "$PI_OFFLINE"'
"""
        )
        pi_shim.chmod(0o755)
        env = {"HOME": str(fake_home), "PATH": _path_with_pi_shim(bin_dir)}
        if caller_offline is not None:
            env["PI_OFFLINE"] = caller_offline

        result = subprocess.run(
            ["sh", "-c", pi_backend.PiBackend._DOCKER_WRAPPER, "sh"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert (result.returncode, result.stdout, result.stderr) == (0, "1\n1\n", "")

    @pytest.mark.parametrize(
        ("configured_models", "expected_provider"),
        [
            ([], {"apiKey": "proxy-token", "baseUrl": "http://host.docker.internal:12345/backend-api"}),
            (
                [
                    {"id": "gpt-5.3-codex", "baseUrl": "https://chatgpt.com/custom"},
                    {"id": "gpt-5.4", "baseUrl": "https://chatgpt.com/custom"},
                ],
                {
                    "apiKey": "proxy-token",
                    "baseUrl": "http://host.docker.internal:12345/custom",
                    "models": [
                        {"id": "gpt-5.3-codex", "baseUrl": "http://host.docker.internal:12345/custom"},
                        {"id": "gpt-5.4", "baseUrl": "http://host.docker.internal:12345/custom"},
                    ],
                },
            ),
            (
                [{"id": "gpt-5.3-codex", "baseUrl": "https://chatgpt.com/custom"}],
                {
                    "apiKey": "proxy-token",
                    "baseUrl": "http://host.docker.internal:12345/backend-api",
                    "models": [
                        {
                            "id": "gpt-5.3-codex",
                            "api": "openai-codex-responses",
                            "baseUrl": "http://host.docker.internal:12345/custom",
                        },
                        {
                            "id": "gpt-5.4",
                            "api": "openai-codex-responses",
                            "baseUrl": "http://host.docker.internal:12345/backend-api",
                        },
                    ],
                },
            ),
            (
                [{"id": "gpt-5.3-codex", "contextWindow": 12345}],
                {
                    "apiKey": "proxy-token",
                    "baseUrl": "http://host.docker.internal:12345/backend-api",
                    "models": [{"id": "gpt-5.3-codex", "contextWindow": 12345}],
                },
            ),
        ],
        ids=["codex-catalog", "uniform-model-overrides", "mixed-model-overrides", "inherited-model-url"],
    )
    def test_routes_uniform_and_mixed_effective_model_urls(
        self, tmp_path: Path, configured_models: list[dict], expected_provider: dict
    ) -> None:
        agent_dir = tmp_path / ".pi" / "agent"
        agent_dir.mkdir(parents=True)
        catalog_models = [
            {"id": model_id, "api": "openai-codex-responses", "baseUrl": "https://chatgpt.com/backend-api"}
            for model_id in ("gpt-5.3-codex", "gpt-5.4")
        ]
        (agent_dir / pi_backend._HOST_MODELS_STORE_FILENAME).write_text(
            json.dumps({"openai-codex": {"models": catalog_models}})
        )
        host_provider = {"models": configured_models} if configured_models else {}
        (agent_dir / pi_backend._HOST_MODELS_FILENAME).write_text(
            json.dumps({"providers": {"openai-codex": host_provider}})
        )
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        pi_shim = bin_dir / "pi"
        pi_shim.write_text("#!/bin/sh\nexit 0\n")
        pi_shim.chmod(0o755)
        env = {
            "HOME": str(tmp_path),
            "PATH": _path_with_pi_shim(bin_dir),
            "HATCHERY_PI_OPENAI_CODEX_ID": "openai-codex",
            "HATCHERY_PI_OPENAI_CODEX_PROXY_ORIGIN": "http://host.docker.internal:12345",
            "HATCHERY_PI_OPENAI_CODEX_KEY": "proxy-token",
            "HATCHERY_PI_OPENAI_CODEX_SHAPE": "oauth",
        }

        result = subprocess.run(
            ["sh", "-c", pi_backend.PiBackend._DOCKER_WRAPPER, "sh"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert json.loads((agent_dir / "models.json").read_text()) == {"providers": {"openai-codex": expected_provider}}
        assert json.loads((agent_dir / "models-store.json").read_text()) == {
            "openai-codex": {
                "models": [
                    {
                        "id": "gpt-5.3-codex",
                        "api": "openai-codex-responses",
                        "baseUrl": "http://host.docker.internal:12345/backend-api",
                    },
                    {
                        "id": "gpt-5.4",
                        "api": "openai-codex-responses",
                        "baseUrl": "http://host.docker.internal:12345/backend-api",
                    },
                ]
            }
        }

    def test_writes_only_fake_proxy_creds_for_n_providers(self, tmp_path):
        """Run the real wrapper under sh with a fake $HOME and a `pi` shim on
        PATH. The written auth.json/models.json must contain only fake proxy
        credentials — never a real credential — for whatever providers
        container_env passed, api_key- or oauth-shaped per _SHAPE. The real
        ~/.pi/agent/auth.json is never mounted or read here.
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
            "PATH": _path_with_pi_shim(bin_dir),
            "HATCHERY_PI_ACME_ID": "acme",
            "HATCHERY_PI_ACME_PROXY_ORIGIN": "http://host.docker.internal:12345",
            "HATCHERY_PI_ACME_KEY": "proxy-token-aaa",
            "HATCHERY_PI_ACME_SHAPE": "api_key",
            "HATCHERY_PI_HUGGINGFACE_ID": "huggingface",
            "HATCHERY_PI_HUGGINGFACE_PROXY_ORIGIN": "http://host.docker.internal:12345",
            "HATCHERY_PI_HUGGINGFACE_KEY": "proxy-token-aaa",
            "HATCHERY_PI_HUGGINGFACE_SHAPE": "api_key",
            "HATCHERY_PI_OPENAI_CODEX_ID": "openai-codex",
            "HATCHERY_PI_OPENAI_CODEX_PROXY_ORIGIN": "http://host.docker.internal:12345",
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
                "acme": {"apiKey": "proxy-token-aaa"},
                "huggingface": {"apiKey": "proxy-token-aaa"},
                "openai-codex": {"apiKey": "proxy-token-aaa"},
            }
        }
        dumped = json.dumps(auth) + json.dumps(models)
        assert "sk-" not in dumped
        assert "real" not in dumped

    def test_merges_custom_model_metadata_with_proxy_overrides(self, tmp_path):
        fake_home = tmp_path / "fakehome"
        agent_dir = fake_home / ".pi" / "agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / pi_backend._HOST_MODELS_FILENAME).write_text(
            json.dumps(
                {
                    "$schema": "https://pi.dev/schemas/models.schema.json",
                    "providers": {
                        "custom": {
                            "baseUrl": "https://models.example.com/v1?provider-query-secret#provider-fragment-secret",
                            "api": "openai-completions",
                            "models": [
                                {
                                    "id": "coder",
                                    "baseUrl": "https://models.example.com/custom/v1?model-query-secret#model-fragment-secret",
                                    "contextWindow": 12345,
                                }
                            ],
                        }
                    },
                }
            )
        )
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        pi_shim = bin_dir / "pi"
        pi_shim.write_text("#!/bin/sh\nexit 0\n")
        pi_shim.chmod(0o755)
        env = {
            "HOME": str(fake_home),
            "PATH": _path_with_pi_shim(bin_dir),
            "HATCHERY_PI_CUSTOM_ID": "custom",
            "HATCHERY_PI_CUSTOM_PROXY_ORIGIN": "http://host.docker.internal:12345",
            "HATCHERY_PI_CUSTOM_KEY": "proxy-token",
            "HATCHERY_PI_CUSTOM_SHAPE": "api_key",
        }

        result = subprocess.run(
            ["sh", "-c", pi_backend.PiBackend._DOCKER_WRAPPER, "sh"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert json.loads((agent_dir / "models.json").read_text()) == {
            "$schema": "https://pi.dev/schemas/models.schema.json",
            "providers": {
                "custom": {
                    "baseUrl": "http://host.docker.internal:12345/v1",
                    "api": "openai-completions",
                    "apiKey": "proxy-token",
                    "models": [
                        {
                            "id": "coder",
                            "baseUrl": "http://host.docker.internal:12345/custom/v1",
                            "contextWindow": 12345,
                        }
                    ],
                }
            },
        }
        assert json.loads((agent_dir / "auth.json").read_text()) == {
            "custom": {"type": "api_key", "key": "proxy-token"}
        }
        generated = (agent_dir / "models.json").read_text()
        assert "query-secret" not in generated
        assert "fragment-secret" not in generated

    def test_preserves_each_catalog_model_path_through_the_proxy(self, tmp_path):
        fake_home = tmp_path / "fakehome"
        agent_dir = fake_home / ".pi" / "agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / pi_backend._HOST_MODELS_STORE_FILENAME).write_text(
            json.dumps(
                {
                    "openrouter": {
                        "opaqueMetadata": {"source": "catalog", "enabled": True},
                        "models": [
                            {
                                "id": "anthropic/claude",
                                "api": "anthropic-messages",
                                "baseUrl": "https://openrouter.ai/api?catalog-query-secret#catalog-fragment-secret",
                                "contextWindow": 200000,
                            },
                            {
                                "id": "minimax/minimax-m3.1-flash",
                                "api": "openai-completions",
                                "baseUrl": "https://openrouter.ai/api/v1",
                                "contextWindow": 100000,
                            },
                        ],
                    }
                }
            )
        )
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        pi_shim = bin_dir / "pi"
        pi_shim.write_text("#!/bin/sh\nexit 0\n")
        pi_shim.chmod(0o755)
        env = {
            "HOME": str(fake_home),
            "PATH": _path_with_pi_shim(bin_dir),
            "HATCHERY_PI_OPENROUTER_ID": "openrouter",
            "HATCHERY_PI_OPENROUTER_PROXY_ORIGIN": "http://host.docker.internal:12345",
            "HATCHERY_PI_OPENROUTER_KEY": "proxy-token",
            "HATCHERY_PI_OPENROUTER_SHAPE": "api_key",
        }

        result = subprocess.run(
            ["sh", "-c", pi_backend.PiBackend._DOCKER_WRAPPER, "sh"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert json.loads((agent_dir / "models-store.json").read_text()) == {
            "openrouter": {
                "opaqueMetadata": {"source": "catalog", "enabled": True},
                "models": [
                    {
                        "id": "anthropic/claude",
                        "api": "anthropic-messages",
                        "baseUrl": "http://host.docker.internal:12345/api",
                        "contextWindow": 200000,
                    },
                    {
                        "id": "minimax/minimax-m3.1-flash",
                        "api": "openai-completions",
                        "baseUrl": "http://host.docker.internal:12345/api/v1",
                        "contextWindow": 100000,
                    },
                ],
            }
        }
        assert json.loads((agent_dir / "models.json").read_text()) == {
            "providers": {
                "openrouter": {
                    "apiKey": "proxy-token",
                    "baseUrl": "http://host.docker.internal:12345/api",
                    "models": [
                        {
                            "id": "anthropic/claude",
                            "api": "anthropic-messages",
                            "baseUrl": "http://host.docker.internal:12345/api",
                            "contextWindow": 200000,
                        },
                        {
                            "id": "minimax/minimax-m3.1-flash",
                            "api": "openai-completions",
                            "baseUrl": "http://host.docker.internal:12345/api/v1",
                            "contextWindow": 100000,
                        },
                    ],
                }
            }
        }
        generated = (agent_dir / "models-store.json").read_text() + (agent_dir / "models.json").read_text()
        assert "query-secret" not in generated
        assert "fragment-secret" not in generated


# ---------------------------------------------------------------------------
# dockerfile_install
# ---------------------------------------------------------------------------


class TestDockerfileInstall:
    def test_installs_node_and_pi(self):
        install = agent.PI.dockerfile_install
        assert (
            "COPY --from=node:22.23.4-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d /usr/local/ /usr/local/"
            in install
        )
        assert "curl" not in install
        assert "@earendil-works/pi-coding-agent" in install

    def test_installs_search_tools_to_avoid_runtime_download(self):
        install = agent.PI.dockerfile_install
        assert "ripgrep" in install
        assert "fd-find" in install

    def test_dockerfile_install_is_unpinned(self):
        # The version is resolved to latest at Dockerfile-generation time, not
        # baked into the snippet — so the package appears without an @version.
        assert "@earendil-works/pi-coding-agent@" not in agent.PI.dockerfile_install
        assert "@earendil-works/pi-coding-agent" in agent.PI.dockerfile_install


class TestUpdate:
    def test_bumps_pin_to_latest(self):
        text = "RUN npm install -g --ignore-scripts @earendil-works/pi-coding-agent@0.1.0"
        with patch.object(pi_backend.npm, "npm_latest_version", return_value="9.9.9"):
            new_text, old_version, version = agent.PI.update(text)
        assert old_version == "0.1.0"
        assert version == "9.9.9"
        assert new_text == "RUN npm install -g --ignore-scripts @earendil-works/pi-coding-agent@9.9.9"
