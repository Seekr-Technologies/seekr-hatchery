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
    def test_no_base_path(self, home):
        _write_store(home, _STORE)
        assert agent.PI.container_env(_ep("acme"), "proxy-tok", 9999) == {
            "HATCHERY_PI_ACME_ID": "acme",
            "HATCHERY_PI_ACME_BASEURL": "http://host.docker.internal:9999",
            "HATCHERY_PI_ACME_KEY": "proxy-tok",
            "HATCHERY_PI_ACME_SHAPE": "api_key",
        }

    def test_openai_carries_base_path(self, home):
        _write_store(home, _STORE)
        assert agent.PI.container_env(_ep("openai"), "proxy-tok", 9999) == {
            "HATCHERY_PI_OPENAI_ID": "openai",
            "HATCHERY_PI_OPENAI_BASEURL": "http://host.docker.internal:9999/v1",
            "HATCHERY_PI_OPENAI_KEY": "proxy-tok",
            "HATCHERY_PI_OPENAI_SHAPE": "api_key",
        }

    def test_openai_codex_is_oauth_shaped_with_store_base_path(self, home):
        _write_store(home, {"openai-codex": ("openai-codex-responses", "https://chatgpt.com/backend-api")})
        assert agent.PI.container_env(_ep("openai-codex", oauth=True), "proxy-tok", 9999) == {
            "HATCHERY_PI_OPENAI_CODEX_ID": "openai-codex",
            "HATCHERY_PI_OPENAI_CODEX_BASEURL": "http://host.docker.internal:9999/backend-api",
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
            mount.VolumeMount(name="pi-bin", dst=f"{agent_dst}/bin", seed=pi_backend._seed_empty_state),
            mount.VolumeMount(name="pi-sessions", dst=f"{agent_dst}/sessions", seed=pi_backend._seed_empty_state),
        ]
        assert agent_dir.is_dir()
        assert {path.name: json.loads(path.read_text()) for path in (tmp_path / "pi-config").iterdir()} == {
            "auth.json": {},
            "models.json": {},
        }
        assert list(agent_dir.iterdir()) == []

    def test_reconstruction_preserves_task_files_and_host_credentials(self, home, tmp_path):
        agent_dir = home / ".pi" / "agent"
        agent_dir.mkdir(parents=True)
        host_files = {"auth.json": "host-auth-secret", "models.json": "host-model-secret"}
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
        }

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
    for name in ("auth.json", "models.json"):
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
    for name in ("auth.json", "models.json"):
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
            "PATH": _path_with_pi_shim(bin_dir),
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
                            "baseUrl": "https://models.example.com/v1",
                            "api": "openai-completions",
                            "models": [{"id": "coder", "contextWindow": 12345}],
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
            "HATCHERY_PI_CUSTOM_BASEURL": "http://host.docker.internal:12345/v1",
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
                    "models": [{"id": "coder", "contextWindow": 12345}],
                }
            },
        }
        assert json.loads((agent_dir / "auth.json").read_text()) == {
            "custom": {"type": "api_key", "key": "proxy-token"}
        }


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
