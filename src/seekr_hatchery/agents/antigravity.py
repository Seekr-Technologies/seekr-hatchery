"""Google Antigravity CLI agent (`agy`) backend."""

import json
import logging
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from seekr_hatchery.agents.agent_backend import CONTAINER_HOME, AgentBackend
from seekr_hatchery.locks import hatchery_lock
from seekr_hatchery.mount import Mount

logger = logging.getLogger("hatchery")


# Default model to use inside the hatchery sandbox — can be overridden by
# setting HATCHERY_AGY_MODEL environment variable on the host before running
# hatchery. Use a cheap/fast model to keep sandbox costs low.
_DEFAULT_SANDBOX_MODEL = "Gemini 3.5 Flash"


class AntigravityBackend(AgentBackend):
    kind = "ANTIGRAVITY"
    binary = "agy"
    supports_sessions = True
    needs_tls_proxy = True
    ca_cert_pem: bytes | None = None

    @staticmethod
    def _read_host_file(filename: str) -> str | None:
        """Read a file from the host's ~/.gemini/antigravity-cli/ directory."""
        p = Path.home() / ".gemini" / "antigravity-cli" / filename
        if p.exists():
            try:
                return p.read_text()
            except Exception as e:
                logger.debug("Failed to read host file %s: %s", filename, e)
        return None

    @staticmethod
    def _wrap_docker_command(cmd: list[str]) -> list[str]:
        """Wrap a command inside a shell wrapper that initializes all environment assets in the container."""
        cmd_str = (
            "mkdir -p /home/hatchery/.gemini/antigravity-cli/brain "
            "/home/hatchery/.gemini/antigravity-cli/cache "
            "/home/hatchery/.gemini/antigravity-cli/knowledge "
            "/home/hatchery/.gemini/antigravity-cli/log "
            "/home/hatchery/.gemini/config && "
            "printf '%s' \"$HATCHERY_AGY_TOKEN\" > /home/hatchery/.gemini/antigravity-cli/antigravity-oauth-token && "
            "printf '%s' \"$HATCHERY_AGY_CA\" > /home/hatchery/.gemini/antigravity-cli/hatchery-agy-ca.crt && "
            "printf '%s' \"$HATCHERY_AGY_SETTINGS\" > /home/hatchery/.gemini/antigravity-cli/settings.json && "
            "printf '%s' \"$HATCHERY_AGY_KEYBINDINGS\" > /home/hatchery/.gemini/antigravity-cli/keybindings.json && "
            "printf '%s' \"$HATCHERY_AGY_INSTALL_ID\" > /home/hatchery/.gemini/antigravity-cli/installation_id && "
            "printf '%s' \"$HATCHERY_AGY_ONBOARDING\" > /home/hatchery/.gemini/antigravity-cli/cache/onboarding.json && "
            # Write a valid empty MCP config so agy doesn't fail to parse it
            "printf '%s' '{\"mcpServers\": {}}' > /home/hatchery/.gemini/config/mcp_config.json && "
            'socat TCP-LISTEN:443,fork TCP:host.docker.internal:"$HATCHERY_AGY_PROXY_PORT" & '
            "exec " + " ".join(f"'{c}'" for c in cmd)
        )
        return ["sh", "-c", cmd_str]

    # ── Command construction ───────────────────────────────────────────────────

    @staticmethod
    def build_new_command(
        session_id: str,
        system_prompt: str,
        initial_prompt: str,
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        # Combine system prompt and initial prompt; strip whitespace.
        prompt = f"{system_prompt}\n\n{initial_prompt}".strip()
        if prompt:
            # Non-empty prompt: start agy with an initial prompt (-i) so it
            # immediately processes the task. The model gets the prompt as the
            # first user message, which is required to be non-empty.
            cmd = ["agy", "--dangerously-skip-permissions", "-i", prompt]
        else:
            # Empty prompt (hatchery chat): start agy in interactive mode
            # without -i. Passing -i with an empty string causes the API to
            # return 400 INVALID_ARGUMENT (empty user message not allowed).
            cmd = ["agy", "--dangerously-skip-permissions"]
        if docker:
            return AntigravityBackend._wrap_docker_command(cmd)
        return cmd

    @staticmethod
    def build_resume_command(
        session_id: str,
        system_prompt: str,
        initial_prompt: str = "",
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        cmd = ["agy", "--dangerously-skip-permissions", "--conversation", session_id]
        cmd += ["-i", initial_prompt or "Please continue the task."]
        if docker:
            return AntigravityBackend._wrap_docker_command(cmd)
        return cmd

    @staticmethod
    def build_finalize_command(
        session_id: str,
        system_prompt: str,
        wrap_up_prompt: str,
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        cmd = [
            "agy",
            "--dangerously-skip-permissions",
            "--conversation",
            session_id,
            "-i",
            wrap_up_prompt,
        ]
        if docker:
            return AntigravityBackend._wrap_docker_command(cmd)
        return cmd

    # ── Docker infrastructure ─────────────────────────────────────────────────

    @staticmethod
    def _read_agy_token() -> str | None:
        """Read and parse the Google OAuth access token from the host file."""
        token_file = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        if not token_file.exists():
            logger.debug("Antigravity OAuth token file not found: %s", token_file)
            return None
        try:
            data = json.loads(token_file.read_text())
            access_token = data.get("token", {}).get("access_token")
            if access_token:
                logger.debug("Successfully read Antigravity OAuth access token")
                return access_token
        except Exception as e:
            logger.debug("Failed to read/parse Antigravity OAuth token: %s", e)
        return None

    @staticmethod
    def construct_mounts(session_dir: Path) -> list[Mount]:
        # Return empty mounts list because mounting outside repo root leads to empty root-owned
        # directories in WSL2/Docker-in-Docker. Instead, all credentials, config, and settings
        # are written dynamically in the container at startup using environment variables.
        return []

    @staticmethod
    def proxy_kwargs() -> dict:
        return {"target_host": "daily-cloudcode-pa.googleapis.com"}

    @staticmethod
    def extra_hosts(proxy_port: int) -> dict[str, str]:
        # Redirect all standard Google API hosts that agy uses to localhost inside
        # the container, where a background socat instance forwards traffic to the host.
        return {
            "daily-cloudcode-pa.googleapis.com": "127.0.0.1",
            "people.googleapis.com": "127.0.0.1",
            "www.googleapis.com": "127.0.0.1",
            "play.googleapis.com": "127.0.0.1",
        }

    @staticmethod
    def make_header_mutator() -> Callable[..., dict[str, str]]:
        token = AntigravityBackend._read_agy_token()
        if not token:
            raise RuntimeError("No Antigravity OAuth token found. Please run `agy` first to authenticate.")

        state = {"token": token}

        def _refresh() -> None:
            """Briefly invoke `agy` to trigger its internal OAuth token refresh logic."""
            with hatchery_lock("refresh.antigravity"):
                # Another process may have already refreshed
                new_token = AntigravityBackend._read_agy_token()
                if new_token and new_token != state["token"]:
                    state["token"] = new_token
                    return

                token_file = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
                old_mtime = token_file.stat().st_mtime if token_file.exists() else 0
                old_token = state["token"]

                proc = subprocess.Popen(
                    ["agy", "-p", "hello"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    current_mtime = token_file.stat().st_mtime if token_file.exists() else 0
                    if current_mtime > old_mtime:
                        new_token = AntigravityBackend._read_agy_token()
                        if new_token:
                            state["token"] = new_token
                        break
                    if proc.poll() is not None:
                        new_token = AntigravityBackend._read_agy_token()
                        if new_token and new_token != old_token:
                            state["token"] = new_token
                        break
                    time.sleep(0.5)
                else:
                    proc.kill()
                    proc.wait()

        def _mutate(headers: dict[str, str], *, refresh: bool = False) -> dict[str, str]:
            if refresh:
                _refresh()
            out = {k: v for k, v in headers.items() if k.lower() not in ("x-api-key", "authorization")}
            out["Authorization"] = f"Bearer {state['token']}"
            return out

        return _mutate

    @staticmethod
    def container_env(proxy_token: str, proxy_port: int) -> dict[str, str]:
        import json

        fake_token_data = {
            "token": {
                "access_token": proxy_token,
                "token_type": "Bearer",
                "refresh_token": None,
                "expiry": "2038-01-19T03:14:07Z",
            },
            "auth_method": "consumer",
        }
        fake_token_json = json.dumps(fake_token_data)

        ca_path = "/home/hatchery/.gemini/antigravity-cli/hatchery-agy-ca.crt"
        ca_pem = AntigravityBackend.ca_cert_pem.decode("utf-8") if AntigravityBackend.ca_cert_pem else ""

        # Load host settings/config files
        settings_content = AntigravityBackend._read_host_file("settings.json") or "{}"
        try:
            settings_dict = json.loads(settings_content) if settings_content.strip() else {}
            if "theme" not in settings_dict:
                settings_dict["theme"] = "terminal"
            if "colorScheme" not in settings_dict:
                settings_dict["colorScheme"] = "terminal"
            if "color_scheme" not in settings_dict:
                settings_dict["color_scheme"] = "terminal"

            # Override model to a cheap/fast one for sandbox use. Respects the
            # HATCHERY_AGY_MODEL env var on the host if set explicitly.
            sandbox_model = os.environ.get("HATCHERY_AGY_MODEL", _DEFAULT_SANDBOX_MODEL)
            settings_dict["model"] = sandbox_model
            logger.debug("Setting sandbox model to: %s", sandbox_model)

            # Auto-trust the container workspace directories to prevent interactive prompt
            if "trustedWorkspaces" not in settings_dict:
                settings_dict["trustedWorkspaces"] = []
            for path in ("/workspace", "/repo"):
                if path not in settings_dict["trustedWorkspaces"]:
                    settings_dict["trustedWorkspaces"].append(path)

            # Redirect API traffic directly to the host's TLS Proxy port
            settings_dict["cloudCodeServerUrl"] = f"https://host.docker.internal:{proxy_port}"
            settings_dict["cloud_code_server_url"] = f"https://host.docker.internal:{proxy_port}"
            settings_dict["useCloudCodeApi"] = True
            settings_dict["use_cloud_code_api"] = True

            settings_content = json.dumps(settings_dict)
        except Exception:
            settings_content = (
                f'{{"enableTelemetry": false, "theme": "terminal", '
                f'"colorScheme": "terminal", "color_scheme": "terminal", '
                f'"trustedWorkspaces": ["/workspace", "/repo"], '
                f'"cloud_code_server_url": "https://host.docker.internal:{proxy_port}", '
                f'"use_cloud_code_api": true}}'
            )

        keybindings_content = AntigravityBackend._read_host_file("keybindings.json") or "[]"
        installation_id = AntigravityBackend._read_host_file("installation_id") or ""

        # Read onboarding state from host or fallback
        onboarding_content = AntigravityBackend._read_host_file("cache/onboarding.json") or "{}"
        try:
            onboarding_dict = json.loads(onboarding_content) if onboarding_content.strip() else {}
            onboarding_dict["onboardingComplete"] = True
            onboarding_dict["consumerOnboardingComplete"] = True
            onboarding_content = json.dumps(onboarding_dict)
        except Exception:
            onboarding_content = '{"onboardingComplete": true, "consumerOnboardingComplete": true}'

        return {
            "SSL_CERT_FILE": ca_path,
            "CURL_CA_BUNDLE": ca_path,
            "REQUESTS_CA_BUNDLE": ca_path,
            "HATCHERY_AGY_TOKEN": fake_token_json,
            "HATCHERY_AGY_CA": ca_pem,
            "HATCHERY_AGY_SETTINGS": settings_content,
            "HATCHERY_AGY_KEYBINDINGS": keybindings_content,
            "HATCHERY_AGY_INSTALL_ID": installation_id,
            "HATCHERY_AGY_ONBOARDING": onboarding_content,
            "HATCHERY_AGY_PROXY_PORT": str(proxy_port),
            "ST_NETWORK_ENDPOINT": f"https://host.docker.internal:{proxy_port}",
            "ST_FIFE_URL": f"https://host.docker.internal:{proxy_port}",
            "DF_URL": f"https://host.docker.internal:{proxy_port}",
        }

    # ── Lifecycle hooks ───────────────────────────────────────────────────────

    @staticmethod
    def on_new_task(session_dir: Path) -> None:
        pass

    @staticmethod
    def on_before_launch(worktree: Path) -> None:
        pass

    @staticmethod
    def on_before_container_start(
        session_dir: Path,
        proxy_token: str,
        workdir: str,
    ) -> None:
        import seekr_hatchery.tls_proxy as tls_proxy

        # Generate ephemeral CA cert & key
        ca_cert_pem, ca_key_pem = tls_proxy.generate_ca()
        AntigravityBackend.ca_cert_pem = ca_cert_pem

        # Generate leaf cert for daily-cloudcode-pa.googleapis.com signed by CA
        hostname = "daily-cloudcode-pa.googleapis.com"
        leaf_cert_pem, leaf_key_pem = tls_proxy.generate_leaf_cert(hostname, ca_cert_pem, ca_key_pem)

        # Write certs and keys to session_dir
        (session_dir / "agy_ca.crt").write_bytes(ca_cert_pem)
        (session_dir / "agy_leaf.crt").write_bytes(leaf_cert_pem)
        (session_dir / "agy_leaf.key").write_bytes(leaf_key_pem)

        # Write a fake OAuth token file containing the proxy_token
        fake_token = {
            "token": {
                "access_token": proxy_token,
                "token_type": "Bearer",
                "refresh_token": None,
                "expiry": "2038-01-19T03:14:07Z",
            }
        }
        (session_dir / "agy_token.json").write_text(json.dumps(fake_token))

    dockerfile_install = f"""\
# ── Google Antigravity CLI ────────────────────────────────────────────────────
COPY agy {CONTAINER_HOME}/.local/bin/agy
USER root
RUN apt-get update && apt-get install -y --no-install-recommends socat libcap2-bin && rm -rf /var/lib/apt/lists/*
RUN setcap 'cap_net_bind_service=+ep' $(readlink -f /usr/bin/socat)
RUN chmod +x {CONTAINER_HOME}/.local/bin/agy \
    && mkdir -p {CONTAINER_HOME}/.gemini/antigravity-cli \
    && chown -R hatchery:hatchery {CONTAINER_HOME}/.gemini
USER hatchery"""
