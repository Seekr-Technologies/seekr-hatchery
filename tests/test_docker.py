"""Tests for docker.py functions (runtime detection, container execution)."""

import subprocess
import sys as _sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import seekr_hatchery.agents as agent
import seekr_hatchery.constants as constants
import seekr_hatchery.docker as docker
import seekr_hatchery.mount as mount
import seekr_hatchery.mount_links as mount_links
from seekr_hatchery.models import SessionMeta


def _no_wt_meta(cwd):
    """Synthetic SessionMeta for no-worktree mount tests."""
    return SessionMeta(name="-", repo=str(cwd), worktree=str(cwd), no_worktree=True)


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestDockerAvailable:
    def test_returns_true_when_rc_zero(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 0
        monkeypatch.setattr(docker, "run", lambda *a, **kw: mock_result)
        assert docker.DockerRuntime.available() is True

    def test_returns_false_when_rc_nonzero(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 1
        monkeypatch.setattr(docker, "run", lambda *a, **kw: mock_result)
        assert docker.DockerRuntime.available() is False

    def test_returns_false_when_binary_not_found(self, monkeypatch):
        def _raise(*a, **kw):
            raise FileNotFoundError("No such file or directory: 'docker'")

        monkeypatch.setattr(docker, "run", _raise)
        assert docker.DockerRuntime.available() is False


# ---------------------------------------------------------------------------
# PodmanRuntime.available()
# ---------------------------------------------------------------------------


class TestPodmanAvailable:
    def test_returns_true_when_rc_zero(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 0
        monkeypatch.setattr(docker, "run", lambda *a, **kw: mock_result)
        assert docker.PodmanRuntime.available() is True

    def test_returns_false_when_rc_nonzero(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 1
        monkeypatch.setattr(docker, "run", lambda *a, **kw: mock_result)
        assert docker.PodmanRuntime.available() is False

    def test_returns_false_when_binary_not_found(self, monkeypatch):
        def _raise(*a, **kw):
            raise FileNotFoundError("No such file or directory: 'podman'")

        monkeypatch.setattr(docker, "run", _raise)
        assert docker.PodmanRuntime.available() is False


# ---------------------------------------------------------------------------
# detect_runtime()
# ---------------------------------------------------------------------------


class TestDetectRuntime:
    def test_returns_podman_when_podman_available(self, monkeypatch):
        monkeypatch.setattr(docker.PodmanRuntime, "available", staticmethod(lambda: True))
        monkeypatch.setattr(docker.DockerRuntime, "available", staticmethod(lambda: True))
        result = docker.detect_runtime()
        assert isinstance(result, docker.PodmanRuntime)

    def test_prefers_podman_over_docker(self, monkeypatch):
        monkeypatch.setattr(docker.PodmanRuntime, "available", staticmethod(lambda: True))
        monkeypatch.setattr(docker.DockerRuntime, "available", staticmethod(lambda: False))
        result = docker.detect_runtime()
        assert isinstance(result, docker.PodmanRuntime)

    def test_falls_back_to_docker_when_podman_not_installed(self, monkeypatch):
        monkeypatch.setattr(docker.PodmanRuntime, "available", staticmethod(lambda: False))
        monkeypatch.setattr(docker.shutil, "which", lambda _: None)
        monkeypatch.setattr(docker.DockerRuntime, "available", staticmethod(lambda: True))
        result = docker.detect_runtime()
        assert isinstance(result, docker.DockerRuntime)

    def test_exits_when_neither_available(self, monkeypatch):
        monkeypatch.setattr(docker.PodmanRuntime, "available", staticmethod(lambda: False))
        monkeypatch.setattr(docker.shutil, "which", lambda _: None)
        monkeypatch.setattr(docker.DockerRuntime, "available", staticmethod(lambda: False))
        with pytest.raises(SystemExit) as exc_info:
            docker.detect_runtime()
        assert exc_info.value.code == 1

    def test_exits_when_podman_installed_but_not_running(self, monkeypatch):
        monkeypatch.setattr(docker.PodmanRuntime, "available", staticmethod(lambda: False))
        monkeypatch.setattr(docker.shutil, "which", lambda _: "/usr/local/bin/podman")
        with pytest.raises(SystemExit) as exc_info:
            docker.detect_runtime()
        assert exc_info.value.code == 1

    def test_installed_not_running_error_message(self, monkeypatch, capsys):
        monkeypatch.setattr(docker.PodmanRuntime, "available", staticmethod(lambda: False))
        monkeypatch.setattr(docker.shutil, "which", lambda _: "/usr/local/bin/podman")
        with pytest.raises(SystemExit):
            docker.detect_runtime()
        assert "not running" in capsys.readouterr().err

    def test_installed_not_running_macos_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(docker.PodmanRuntime, "available", staticmethod(lambda: False))
        monkeypatch.setattr(docker.shutil, "which", lambda _: "/usr/local/bin/podman")
        monkeypatch.setattr(docker.sys, "platform", "darwin")
        with pytest.raises(SystemExit):
            docker.detect_runtime()
        err = capsys.readouterr().err
        assert "podman machine start" in err
        assert "podman machine init" in err

    def test_neither_available_error_message(self, monkeypatch, capsys):
        monkeypatch.setattr(docker.PodmanRuntime, "available", staticmethod(lambda: False))
        monkeypatch.setattr(docker.shutil, "which", lambda _: None)
        monkeypatch.setattr(docker.DockerRuntime, "available", staticmethod(lambda: False))
        with pytest.raises(SystemExit):
            docker.detect_runtime()
        err = capsys.readouterr().err
        assert "Podman" in err or "Docker" in err


# ---------------------------------------------------------------------------
# resolve_runtime()
# ---------------------------------------------------------------------------


class TestResolveRuntime:
    def test_returns_none_when_no_docker_flag(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        result = docker.resolve_runtime(worktree, no_docker=True)
        assert result is None

    def test_exits_when_no_dockerfile_and_docker_not_disabled(self, tmp_path, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        # No Dockerfile in worktree — should error, not silently run natively
        with pytest.raises(SystemExit) as exc_info:
            docker.resolve_runtime(worktree, no_docker=False)
        assert exc_info.value.code == 1
        assert "No Dockerfile found" in capsys.readouterr().err

    def test_returns_podman_when_dockerfile_and_podman_available(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "Dockerfile.codex").write_text("FROM debian\n")
        monkeypatch.setattr(docker, "detect_runtime", lambda: docker.PodmanRuntime())
        result = docker.resolve_runtime(worktree, no_docker=False)
        assert isinstance(result, docker.PodmanRuntime)

    def test_returns_docker_when_dockerfile_and_docker_available(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "Dockerfile.codex").write_text("FROM debian\n")
        monkeypatch.setattr(docker, "detect_runtime", lambda: docker.DockerRuntime())
        result = docker.resolve_runtime(worktree, no_docker=False)
        assert isinstance(result, docker.DockerRuntime)

    def test_exits_when_dockerfile_present_but_no_runtime(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        monkeypatch.setattr(docker, "detect_runtime", lambda: (_ for _ in ()).throw(SystemExit(1)))
        with pytest.raises(SystemExit) as exc_info:
            docker.resolve_runtime(worktree, no_docker=False)
        assert exc_info.value.code == 1

    def test_stderr_message_when_no_runtime(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        def _exit():
            print("Error: neither Podman nor Docker is running.", file=_sys.stderr)
            raise SystemExit(1)

        monkeypatch.setattr(docker, "detect_runtime", _exit)
        with pytest.raises(SystemExit):
            docker.resolve_runtime(worktree, no_docker=False)
        captured = capsys.readouterr()
        assert "Podman" in captured.err or "Docker" in captured.err

    def test_no_docker_flag_skips_dockerfile_check(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        # Even with Dockerfile present, no_docker=True returns None
        result = docker.resolve_runtime(worktree, no_docker=True)
        assert result is None

    def test_agent_specific_dockerfile_detected(self, tmp_path, monkeypatch):
        worktree = tmp_path / "worktree"
        (worktree / ".hatchery").mkdir(parents=True)
        docker.dockerfile_path(worktree, agent.CODEX).write_text("FROM debian\n")
        monkeypatch.setattr(docker, "detect_runtime", lambda: docker.DockerRuntime())
        assert isinstance(docker.resolve_runtime(worktree, no_docker=False, backend=agent.CODEX), docker.DockerRuntime)


# ---------------------------------------------------------------------------
# build_spec() + ContainerRuntime.render_run_argv() — runtime flag injection
# ---------------------------------------------------------------------------


def _make_mutator(key: str = "real-secret-key"):
    """Return a simple header mutator for tests."""

    def _mutate(headers):
        out = {k: v for k, v in headers.items() if k.lower() not in ("x-api-key", "authorization")}
        out["Authorization"] = f"Bearer {key}"
        return out

    return _mutate


class TestRenderRunArgv:
    """Verify render_run_argv injects correct flags for each runtime."""

    def _build_and_render(
        self,
        monkeypatch,
        runtime: docker.ContainerRuntime | None = None,
        mutator=None,
        proxy_token: str = "proxy-uuid-token",
        proxy_port: int = 9999,
        **spec_kwargs,
    ) -> list[str]:
        if runtime is None:
            runtime = docker.DockerRuntime()
        spec = docker.build_spec(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            name="test-task",
            hatchery_repo="/repo",
            container_name=None,
            agent_cmd=["codex"],
            extra_env=agent.CODEX.container_env(proxy_token, proxy_port),
            needs_host_gateway=True,
            **spec_kwargs,
        )
        return runtime.render_run_argv(spec)

    # --- runtime binary ---

    def test_docker_runtime_uses_docker_binary(self, monkeypatch):
        cmd = self._build_and_render(monkeypatch, runtime=docker.DockerRuntime())
        assert cmd[0] == "docker"

    def test_podman_runtime_uses_podman_binary(self, monkeypatch):
        cmd = self._build_and_render(monkeypatch, runtime=docker.PodmanRuntime())
        assert cmd[0] == "podman"

    # --- --init (PID 1 zombie reaping) ---

    def test_docker_runtime_adds_init(self, monkeypatch):
        cmd = self._build_and_render(monkeypatch, runtime=docker.DockerRuntime())
        assert "--init" in cmd

    def test_podman_runtime_adds_init(self, monkeypatch):
        cmd = self._build_and_render(monkeypatch, runtime=docker.PodmanRuntime())
        assert "--init" in cmd

    # --- Podman outer-container flags ---

    def test_podman_userns_keep_id_on_linux(self, monkeypatch):
        monkeypatch.setattr(docker.sys, "platform", "linux")
        cmd = self._build_and_render(monkeypatch, runtime=docker.PodmanRuntime())
        assert "--userns=keep-id" in cmd

    def test_podman_no_userns_keep_id_on_macos(self, monkeypatch):
        monkeypatch.setattr(docker.sys, "platform", "darwin")
        cmd = self._build_and_render(monkeypatch, runtime=docker.PodmanRuntime())
        assert "--userns=keep-id" not in cmd

    def test_podman_runtime_adds_label_disable(self, monkeypatch):
        cmd = self._build_and_render(monkeypatch, runtime=docker.PodmanRuntime())
        assert "label=disable" in " ".join(cmd)

    def test_docker_runtime_no_userns(self, monkeypatch):
        cmd = self._build_and_render(monkeypatch, runtime=docker.DockerRuntime())
        assert "--userns=keep-id" not in cmd

    def test_docker_runtime_no_label_disable(self, monkeypatch):
        cmd = self._build_and_render(monkeypatch, runtime=docker.DockerRuntime())
        assert "label=disable" not in " ".join(cmd)

    # --- Security regression guards ---

    def test_podman_no_privileged(self, monkeypatch):
        cmd = self._build_and_render(monkeypatch, runtime=docker.PodmanRuntime())
        assert "--privileged" not in cmd

    def test_podman_no_seccomp_unconfined(self, monkeypatch):
        cmd = self._build_and_render(monkeypatch, runtime=docker.PodmanRuntime())
        assert "seccomp=unconfined" not in " ".join(cmd)

    def test_docker_no_privileged(self, monkeypatch):
        cmd = self._build_and_render(monkeypatch, runtime=docker.DockerRuntime())
        assert "--privileged" not in cmd

    # --- API key security guards ---

    def test_real_api_key_absent_from_cmd(self, monkeypatch):
        """The real API key must never appear in the docker command."""
        mutator = _make_mutator("real-secret-key")
        cmd = self._build_and_render(monkeypatch, mutator=mutator, proxy_token="proxy-uuid-token")
        assert "real-secret-key" not in " ".join(cmd)

    def test_proxy_token_present_as_api_key(self, monkeypatch):
        """The container's API key env var must be the proxy token, not the real key."""
        cmd = self._build_and_render(monkeypatch, proxy_token="proxy-uuid-token")
        cmd_str = " ".join(cmd)
        assert "OPENAI_API_KEY=proxy-uuid-token" in cmd_str

    def test_base_url_points_to_proxy(self, monkeypatch):
        """OPENAI_BASE_URL must point to the host proxy port."""
        cmd = self._build_and_render(monkeypatch, proxy_port=12345)
        cmd_str = " ".join(cmd)
        assert "OPENAI_BASE_URL" in cmd_str
        assert "host.docker.internal:12345" in cmd_str

    def test_add_host_flag_on_linux(self, monkeypatch):
        """On Linux, --add-host=host.docker.internal:host-gateway must be present."""
        monkeypatch.setattr(docker.sys, "platform", "linux")
        cmd = self._build_and_render(monkeypatch)
        assert "--add-host=host.docker.internal:host-gateway" in cmd

    def test_no_add_host_flag_on_macos(self, monkeypatch):
        """On macOS, Docker Desktop exposes host.docker.internal natively."""
        monkeypatch.setattr(docker.sys, "platform", "darwin")
        cmd = self._build_and_render(monkeypatch)
        assert "--add-host=host.docker.internal:host-gateway" not in cmd

    def test_proxy_token_always_set(self, monkeypatch):
        """The container API key env var must always be set to the stable proxy token."""
        cmd = self._build_and_render(monkeypatch, proxy_token="stable-token")
        cmd_str = " ".join(cmd)
        assert "OPENAI_API_KEY=stable-token" in cmd_str

    def test_no_api_key_env_when_mutator_is_none(self, monkeypatch):
        """When mutator is None, no API key or base URL env vars should appear."""
        spec = docker.build_spec(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            name="test-task",
            hatchery_repo="/repo",
            container_name=None,
            agent_cmd=["codex"],
        )
        cmd = docker.DockerRuntime().render_run_argv(spec)
        cmd_str = " ".join(cmd)
        assert "OPENAI_API_KEY" not in cmd_str
        assert "OPENAI_BASE_URL" not in cmd_str


# ---------------------------------------------------------------------------
# ContainerRuntime.run() — interactive / command_override modes
# ---------------------------------------------------------------------------


class TestRunInteractive:
    """Verify runtime.run with command_override + interactive flags."""

    def test_interactive_override_adds_it_flags(self, monkeypatch):
        """interactive=True + command_override should add -it to the command."""
        captured: list[list[str]] = []

        def _mock_run(cmd, **kw):
            captured.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(docker.subprocess, "run", _mock_run)
        runtime = docker.DockerRuntime()
        monkeypatch.setattr(runtime, "_ensure_volumes", lambda _mounts: None)
        spec = docker.build_spec(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            name="test-task",
            hatchery_repo="/repo",
            container_name=None,
            agent_cmd=[],
            command_override=["/bin/bash"],
            interactive=True,
        )
        runtime.run(spec)
        cmd = captured[0]
        assert "-it" in cmd
        assert "/bin/bash" in cmd

    def test_interactive_override_does_not_capture(self, monkeypatch):
        """interactive=True should call subprocess.run without capture_output."""
        captured_kwargs: list[dict] = []

        def _mock_run(cmd, **kw):
            captured_kwargs.append(kw)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(docker.subprocess, "run", _mock_run)
        runtime = docker.DockerRuntime()
        monkeypatch.setattr(runtime, "_ensure_volumes", lambda _mounts: None)
        spec = docker.build_spec(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            name="test-task",
            hatchery_repo="/repo",
            container_name=None,
            agent_cmd=[],
            command_override=["/bin/bash"],
            interactive=True,
        )
        runtime.run(spec)
        assert "capture_output" not in captured_kwargs[0]

    def test_interactive_override_returns_none(self, monkeypatch):
        """interactive=True should return None (output not captured)."""
        monkeypatch.setattr(docker.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0))
        runtime = docker.DockerRuntime()
        monkeypatch.setattr(runtime, "_ensure_volumes", lambda _mounts: None)
        spec = docker.build_spec(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            name="test-task",
            hatchery_repo="/repo",
            container_name=None,
            agent_cmd=[],
            command_override=["/bin/bash"],
            interactive=True,
        )
        assert runtime.run(spec) is None

    def test_non_interactive_override_captures_output(self, monkeypatch):
        """Default interactive=False + command_override should capture output."""
        captured_kwargs: list[dict] = []

        def _mock_run(cmd, **kw):
            captured_kwargs.append(kw)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(docker.subprocess, "run", _mock_run)
        runtime = docker.DockerRuntime()
        monkeypatch.setattr(runtime, "_ensure_volumes", lambda _mounts: None)
        spec = docker.build_spec(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            name="test-task",
            hatchery_repo="/repo",
            container_name=None,
            agent_cmd=[],
            command_override=["echo", "hello"],
        )
        runtime.run(spec)
        assert captured_kwargs[0].get("capture_output") is True

    def test_non_interactive_override_no_it_flags(self, monkeypatch):
        """Default interactive=False + command_override should NOT add -it."""
        captured: list[list[str]] = []

        def _mock_run(cmd, **kw):
            captured.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(docker.subprocess, "run", _mock_run)
        runtime = docker.DockerRuntime()
        monkeypatch.setattr(runtime, "_ensure_volumes", lambda _mounts: None)
        spec = docker.build_spec(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            name="test-task",
            hatchery_repo="/repo",
            container_name=None,
            agent_cmd=[],
            command_override=["echo", "hello"],
        )
        runtime.run(spec)
        assert "-it" not in captured[0]


# ---------------------------------------------------------------------------
# Golden argv / ContainerSpec assertions (full-output)
# ---------------------------------------------------------------------------


class TestGoldenSpecAndArgv:
    """Full-output golden tests for build_spec + render_run_argv.

    These lock in the exact ContainerSpec fields and rendered argv so that
    argv-order or field-name changes are caught immediately.
    """

    def test_spec_non_dind_with_proxy(self, monkeypatch):
        """build_spec for a standard agent launch with proxy (non-DinD)."""
        monkeypatch.setattr(docker.sys, "platform", "linux")
        spec = docker.build_spec(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            name="test-task",
            hatchery_repo="/repo",
            container_name="hatchery-ctr",
            agent_cmd=["codex"],
            extra_env={
                "OPENAI_API_KEY": "proxy-uuid-token",
                "OPENAI_BASE_URL": "http://host.docker.internal:9999",
            },
            needs_host_gateway=True,
        )
        assert spec == docker.ContainerSpec(
            image="test-image",
            command=["codex"],
            workdir="/workspace",
            name="test-task",
            container_name="hatchery-ctr",
            mounts=[],
            env={
                "HATCHERY_TASK": "test-task",
                "HATCHERY_REPO": "/repo",
                "OPENAI_API_KEY": "proxy-uuid-token",
                "OPENAI_BASE_URL": "http://host.docker.internal:9999",
            },
            cap_add=[],
            cap_drop=[],
            devices=[],
            security_opt=[],
            add_hosts=["host.docker.internal:host-gateway"],
            interactive=True,
            rm=True,
            init=True,
            command_override=None,
            capture_output=True,
        )

    def test_spec_dind(self, monkeypatch):
        """build_spec for a DinD launch (no proxy)."""
        spec = docker.build_spec(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            name="test-task",
            hatchery_repo="/repo",
            container_name=None,
            agent_cmd=["codex"],
            dind=True,
            cap_add=["NET_BIND_SERVICE"],
        )
        assert spec.cap_drop == ["ALL"]
        assert spec.devices == ["/dev/fuse"]
        assert spec.security_opt == ["label=disable", f"seccomp={docker._SECCOMP}"]
        assert "NET_BIND_SERVICE" in spec.cap_add
        assert "SYS_ADMIN" in spec.cap_add
        assert spec.add_hosts == []

    def test_docker_argv_non_dind(self, monkeypatch):
        """Full rendered argv for Docker, non-DinD, with proxy on Linux."""
        monkeypatch.setattr(docker.sys, "platform", "linux")
        spec = docker.build_spec(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            name="test-task",
            hatchery_repo="/repo",
            container_name="hatchery-ctr",
            agent_cmd=["codex"],
            extra_env={
                "OPENAI_API_KEY": "proxy-uuid-token",
                "OPENAI_BASE_URL": "http://host.docker.internal:9999",
            },
            needs_host_gateway=True,
        )
        assert docker.DockerRuntime().render_run_argv(spec) == [
            "docker",
            "run",
            "--rm",
            "--init",
            "-it",
            "-e",
            "HATCHERY_TASK=test-task",
            "-e",
            "HATCHERY_REPO=/repo",
            "-e",
            "OPENAI_API_KEY=proxy-uuid-token",
            "-e",
            "OPENAI_BASE_URL=http://host.docker.internal:9999",
            "--add-host=host.docker.internal:host-gateway",
            "--name",
            "hatchery-ctr",
            "-w",
            "/workspace",
            "test-image",
        ]

    def test_podman_argv_non_dind(self, monkeypatch):
        """Full rendered argv for Podman, non-DinD, with proxy on Linux."""
        monkeypatch.setattr(docker.sys, "platform", "linux")
        spec = docker.build_spec(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            name="test-task",
            hatchery_repo="/repo",
            container_name="hatchery-ctr",
            agent_cmd=["codex"],
            extra_env={
                "OPENAI_API_KEY": "proxy-uuid-token",
                "OPENAI_BASE_URL": "http://host.docker.internal:9999",
            },
            needs_host_gateway=True,
        )
        assert docker.PodmanRuntime().render_run_argv(spec) == [
            "podman",
            "run",
            "--rm",
            "--init",
            "-it",
            "-e",
            "HATCHERY_TASK=test-task",
            "-e",
            "HATCHERY_REPO=/repo",
            "-e",
            "OPENAI_API_KEY=proxy-uuid-token",
            "-e",
            "OPENAI_BASE_URL=http://host.docker.internal:9999",
            "--add-host=host.docker.internal:host-gateway",
            "--name",
            "hatchery-ctr",
            "--userns=keep-id",
            "--security-opt",
            "label=disable",
            "-w",
            "/workspace",
            "test-image",
        ]

    def test_docker_argv_dind(self, monkeypatch):
        """Full rendered argv for Docker, DinD, no proxy."""
        spec = docker.build_spec(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            name="test-task",
            hatchery_repo="/repo",
            container_name=None,
            agent_cmd=["codex"],
            dind=True,
            cap_add=["NET_BIND_SERVICE"],
        )
        assert docker.DockerRuntime().render_run_argv(spec) == [
            "docker",
            "run",
            "--rm",
            "--init",
            "-it",
            "-e",
            "HATCHERY_TASK=test-task",
            "-e",
            "HATCHERY_REPO=/repo",
            "-w",
            "/workspace",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "AUDIT_WRITE",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "DAC_OVERRIDE",
            "--cap-add",
            "FOWNER",
            "--cap-add",
            "FSETID",
            "--cap-add",
            "KILL",
            "--cap-add",
            "MKNOD",
            "--cap-add",
            "NET_ADMIN",
            "--cap-add",
            "NET_BIND_SERVICE",
            "--cap-add",
            "NET_RAW",
            "--cap-add",
            "SETFCAP",
            "--cap-add",
            "SETGID",
            "--cap-add",
            "SETPCAP",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SYS_ADMIN",
            "--cap-add",
            "SYS_CHROOT",
            "--security-opt",
            "label=disable",
            "--security-opt",
            f"seccomp={docker._SECCOMP}",
            "--device",
            "/dev/fuse",
            "test-image",
        ]


# ---------------------------------------------------------------------------
# oom_hint()
# ---------------------------------------------------------------------------


class TestOomHint:
    def test_podman_137_returns_hint(self):
        hint = docker.PodmanRuntime().oom_hint(137)
        assert hint is not None
        assert "memory" in hint

    def test_podman_0_returns_none(self):
        assert docker.PodmanRuntime().oom_hint(0) is None

    def test_docker_137_returns_none(self):
        assert docker.DockerRuntime().oom_hint(137) is None


# ---------------------------------------------------------------------------
# build_docker_image() — build context and stdin
# ---------------------------------------------------------------------------


class TestBuildDockerImage:
    """Verify build_docker_image uses a temp empty dir as build context."""

    def _capture_build(
        self,
        monkeypatch,
        tmp_path,
        *,
        debug: bool = False,
    ) -> tuple[list[str], dict]:
        """Set up a fake repo/worktree, call build_docker_image, return the captured command and kwargs."""
        repo = tmp_path / "repo"
        worktree = tmp_path / "worktree"
        hatchery_dir = worktree / ".hatchery"
        hatchery_dir.mkdir(parents=True)

        docker.dockerfile_path(worktree, agent.CODEX).write_text("FROM debian\n")

        captured: list[list[str]] = []
        captured_kwargs: list[dict] = []

        def _mock_run(cmd, **kw):
            captured.append(cmd)
            captured_kwargs.append(kw)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(docker.subprocess, "run", _mock_run)

        if debug:
            monkeypatch.setattr(docker.logger, "isEnabledFor", lambda _lvl: True)
        else:
            monkeypatch.setattr(docker.logger, "isEnabledFor", lambda _lvl: False)
            # _stream_build is used in non-debug mode; stub it out
            monkeypatch.setattr(docker, "_stream_build", lambda cmd, cwd: (0, []))

        docker.build_docker_image(repo, worktree, "test-task", agent.CODEX, runtime=docker.PodmanRuntime())
        return captured[0], captured_kwargs[0]

    def test_build_context_is_not_repo_root(self, monkeypatch, tmp_path):
        """The last arg (build context) must NOT be the repo root."""
        cmd, _kw = self._capture_build(monkeypatch, tmp_path, debug=True)
        context_arg = cmd[-1]
        # Must be a temp dir, not the repo root
        assert "repo" not in context_arg
        assert "hatchery-build-" in context_arg

    def test_build_context_is_empty_temp_dir(self, monkeypatch, tmp_path):
        """The build context must be a temporary empty directory."""
        cmd, _kw = self._capture_build(monkeypatch, tmp_path, debug=True)
        context_arg = cmd[-1]
        # The temp dir is created by tempfile.TemporaryDirectory with our prefix
        assert "hatchery-build-" in context_arg

    def test_debug_path_passes_stdin_devnull(self, monkeypatch, tmp_path):
        """The DEBUG subprocess.run call must pass stdin=DEVNULL to avoid hangs."""
        _cmd, kw = self._capture_build(monkeypatch, tmp_path, debug=True)
        assert kw.get("stdin") is subprocess.DEVNULL


# ---------------------------------------------------------------------------
# _stream_build() — stdin handling
# ---------------------------------------------------------------------------


class TestStreamBuild:
    def test_non_tty_passes_stdin_devnull(self, monkeypatch):
        """The non-TTY path must pass stdin=DEVNULL to subprocess.run."""
        monkeypatch.setattr(_sys.stdout, "isatty", lambda: False)

        captured_kwargs: list[dict] = []

        def _mock_run(cmd, **kw):
            captured_kwargs.append(kw)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(docker.subprocess, "run", _mock_run)
        docker._stream_build(["echo", "hello"], cwd=_sys.modules["pathlib"].Path("."))
        assert captured_kwargs[0].get("stdin") is subprocess.DEVNULL


# ---------------------------------------------------------------------------
# _docker_mounts_includes()
# ---------------------------------------------------------------------------


class TestDockerMountsIncludes:
    def _entry(self, path, mode="worktree"):
        from seekr_hatchery.includes import IncludeEntry

        return IncludeEntry(path=path, mode=mode)

    def test_plain_dir_gets_rw_mount(self, tmp_path):
        """A plain (non-git) directory in worktree mode is mounted rw at its host path."""
        plain = tmp_path / "shared-data"
        plain.mkdir()
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mounts = docker._docker_mounts_includes([self._entry(plain)], "my-task", session_dir, no_worktree=False)

        assert mount.BindMount(src=str(plain), dst=str(plain), mode="RW") in mounts

    def test_git_repo_without_worktree_gets_rw_mount(self, tmp_path):
        """A git repo in worktree mode with no worktree for the task falls back to rw mount."""
        repo = tmp_path / "repo-b"
        repo.mkdir()
        (repo / ".git").mkdir()
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mounts = docker._docker_mounts_includes([self._entry(repo)], "my-task", session_dir, no_worktree=False)

        assert mount.BindMount(src=str(repo), dst=str(repo), mode="RW") in mounts
        assert not any("git_ptr" in str(m.src or "") for m in mounts)

    def test_git_repo_with_worktree_gets_layered_mounts(self, tmp_path):
        """A git repo in worktree mode with a task worktree gets layered mounts at host paths."""

        repo = tmp_path / "repo-b"
        repo.mkdir()
        git_dir = repo / ".git"
        git_dir.mkdir()
        (git_dir / "objects").mkdir()
        worktree = repo / constants.WORKTREES_SUBDIR / "my-task"
        worktree.mkdir(parents=True)
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mounts = docker._docker_mounts_includes([self._entry(repo)], "my-task", session_dir, no_worktree=False)

        assert mount.BindMount(src=str(repo), dst=str(repo), mode="RO") in mounts
        assert mount.BindMount(src=str(git_dir), dst=f"{repo}/.git", mode="RW") in mounts
        assert mount.BindMount(src=str(git_dir / "objects"), dst=f"{repo}/.git/objects", mode="RW") in mounts
        assert mount.BindMount(src=str(worktree), dst=str(worktree), mode="RW") in mounts
        # No .git pointer rewrite — under host-path mirroring, the worktree's
        # existing .git file already resolves correctly inside the container.
        assert not any("git_ptr" in str(m.src or "") for m in mounts)
        assert mount.BindMount(src=str(repo), dst=str(repo), mode="RW") not in mounts

    def test_no_worktree_skips_layered_mounts(self, tmp_path):
        """In no-worktree mode, worktree-mode git repos get a simple rw mount."""
        repo = tmp_path / "repo-b"
        repo.mkdir()
        (repo / ".git").mkdir()

        worktree = repo / constants.WORKTREES_SUBDIR / "my-task"
        worktree.mkdir(parents=True)
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mounts = docker._docker_mounts_includes([self._entry(repo)], "my-task", session_dir, no_worktree=True)

        assert mount.BindMount(src=str(repo), dst=str(repo), mode="RW") in mounts
        assert not any("git_ptr" in str(m.src or "") for m in mounts)

    def test_empty_list_returns_empty(self, tmp_path):
        mounts = docker._docker_mounts_includes([], "task", tmp_path, no_worktree=False)
        assert mounts == []

    # ── reference mode tests ─────────────────────────────────────────────────

    def test_reference_rw_plain_dir(self, tmp_path):
        """mode='rw' gives a simple rw mount, no worktree logic."""
        plain = tmp_path / "shared-data"
        plain.mkdir()
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mounts = docker._docker_mounts_includes(
            [self._entry(plain, mode="rw")], "my-task", session_dir, no_worktree=False
        )

        assert mount.BindMount(src=str(plain), dst=str(plain), mode="RW") in mounts

    def test_reference_ro_plain_dir(self, tmp_path):
        """mode='ro' gives a simple ro mount."""
        plain = tmp_path / "docs"
        plain.mkdir()
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mounts = docker._docker_mounts_includes(
            [self._entry(plain, mode="ro")], "my-task", session_dir, no_worktree=False
        )

        assert mount.BindMount(src=str(plain), dst=str(plain), mode="RO") in mounts
        assert mount.BindMount(src=str(plain), dst=str(plain), mode="RW") not in mounts

    def test_reference_mode_git_repo_no_layered_mounts(self, tmp_path):
        """mode='ro' on a git repo with a worktree still just does a simple ro mount."""

        repo = tmp_path / "repo-b"
        repo.mkdir()
        git_dir = repo / ".git"
        git_dir.mkdir()
        (git_dir / "objects").mkdir()
        # Create a worktree — it should be ignored in reference mode
        worktree = repo / constants.WORKTREES_SUBDIR / "my-task"
        worktree.mkdir(parents=True)
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mounts = docker._docker_mounts_includes(
            [self._entry(repo, mode="ro")], "my-task", session_dir, no_worktree=False
        )

        assert mount.BindMount(src=str(repo), dst=str(repo), mode="RO") in mounts
        # No layered mounts
        assert mount.BindMount(src=str(repo), dst=str(repo), mode="RW") not in mounts
        assert not any("git_ptr" in str(m.src or "") for m in mounts)
        assert not any("worktrees" in str(m.dst or "") for m in mounts)

    def test_reference_rw_git_repo_no_layered_mounts(self, tmp_path):
        """mode='rw' on a git repo with a worktree still just does a simple rw reference mount."""

        repo = tmp_path / "repo-c"
        repo.mkdir()
        (repo / ".git").mkdir()
        worktree = repo / constants.WORKTREES_SUBDIR / "my-task"
        worktree.mkdir(parents=True)
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mounts = docker._docker_mounts_includes(
            [self._entry(repo, mode="rw")], "my-task", session_dir, no_worktree=False
        )

        assert mount.BindMount(src=str(repo), dst=str(repo), mode="RW") in mounts
        assert not any("git_ptr" in str(m.src or "") for m in mounts)
        assert not any("worktrees" in str(m.dst or "") for m in mounts)

    def test_mixed_modes(self, tmp_path):
        """Mixed worktree and reference entries produce correct mounts each."""

        wt_repo = tmp_path / "wt-repo"
        wt_repo.mkdir()
        (wt_repo / ".git").mkdir()
        ro_dir = tmp_path / "docs"
        ro_dir.mkdir()
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        from seekr_hatchery.includes import IncludeEntry

        entries = [
            IncludeEntry(path=wt_repo, mode="worktree"),
            IncludeEntry(path=ro_dir, mode="ro"),
        ]
        mounts = docker._docker_mounts_includes(entries, "my-task", session_dir, no_worktree=False)

        # worktree entry without an actual worktree → rw fallback
        assert mount.BindMount(src=str(wt_repo), dst=str(wt_repo), mode="RW") in mounts
        # ro reference entry
        assert mount.BindMount(src=str(ro_dir), dst=str(ro_dir), mode="RO") in mounts


# ---------------------------------------------------------------------------
# DockerConfig.include field
# ---------------------------------------------------------------------------


class TestDockerConfigInclude:
    def test_defaults_to_empty(self):
        config = docker.DockerConfig()
        assert config.include == []

    def test_parses_string_include_list(self):
        config = docker.DockerConfig(include=["../repo-b", "/abs/path"])
        assert config.include == ["../repo-b", "/abs/path"]

    def test_parses_dict_include_entry(self):
        from seekr_hatchery.includes import IncludeItem

        config = docker.DockerConfig(include=[{"path": "../ref", "mode": "ro"}])
        assert config.include == [IncludeItem(path="../ref", mode="ro")]

    def test_parses_mixed_include_list(self):
        from seekr_hatchery.includes import IncludeItem

        config = docker.DockerConfig(include=["../wt-repo", {"path": "../ref", "mode": "rw"}])
        assert config.include[0] == "../wt-repo"
        assert config.include[1] == IncludeItem(path="../ref", mode="rw")

    def test_dict_without_path_is_invalid(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            docker.DockerConfig(include=[{"mode": "ro"}])

    def test_dict_invalid_mode_is_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            docker.DockerConfig(include=[{"path": "../foo", "mode": "readwrite"}])

    def test_dict_extra_keys_are_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            docker.DockerConfig(include=[{"path": "../foo", "mode": "ro", "extra": "oops"}])

    def test_extra_fields_still_forbidden(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            docker.DockerConfig(unknown_field="oops")


# ---------------------------------------------------------------------------
# DockerConfig.volumes field
# ---------------------------------------------------------------------------


class TestDockerConfigVolumes:
    def test_parses_volume_entry(self):
        config = docker.DockerConfig(volumes=[{"name": "uv-cache", "path": "/home/hatchery/.cache/uv"}])
        assert config.volumes == [docker.CacheVolume(name="uv-cache", path="/home/hatchery/.cache/uv")]

    def test_name_with_colon_is_invalid(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            docker.DockerConfig(volumes=[{"name": "bad:name", "path": "/cache"}])

    def test_name_with_slash_is_invalid(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            docker.DockerConfig(volumes=[{"name": "bad/name", "path": "/cache"}])

    def test_relative_path_is_invalid(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            docker.DockerConfig(volumes=[{"name": "uv-cache", "path": "relative/cache"}])

    def test_none_coerced_to_empty(self):
        # `volumes:` in YAML with all-commented entries parses to None;
        # match the `mounts:` behavior and treat that as an empty list.
        assert docker.DockerConfig(volumes=None).volumes == []


class TestConstructVolumeMounts:
    def test_empty(self):
        assert docker._construct_volume_mounts(docker.DockerConfig()) == []

    def test_prefixes_name_and_emits_volume_mount(self):
        cfg = docker.DockerConfig(
            volumes=[
                {"name": "uv-cache", "path": "/home/hatchery/.cache/uv"},
                {"name": "pip-cache", "path": "/home/hatchery/.cache/pip"},
            ]
        )
        assert docker._construct_volume_mounts(cfg) == [
            mount.VolumeMount(name="hatchery-uv-cache", dst="/home/hatchery/.cache/uv", mode="RW", task_scoped=False),
            mount.VolumeMount(name="hatchery-pip-cache", dst="/home/hatchery/.cache/pip", mode="RW", task_scoped=False),
        ]


class TestEnsureVolumes:
    def _record_run(self, returncodes_by_cmd):
        """Build a fake `run` that records calls and returns rc per arg-tuple key.

        *returncodes_by_cmd* maps a tuple like ("volume", "inspect", "name") to
        the returncode that `run` should report.  Unknown calls default to 0.
        """
        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(list(cmd))
            key = tuple(cmd[1:])  # strip runtime binary
            rc = returncodes_by_cmd.get(key, 0)
            result = MagicMock()
            result.returncode = rc
            return result

        return calls, fake_run

    def test_skips_non_volume_mounts(self, monkeypatch):
        calls, fake_run = self._record_run({})
        monkeypatch.setattr(docker, "run", fake_run)

        mounts = [mount.BindMount(src="/host/x", dst="/cont/x", mode="RW")]
        docker.DockerRuntime()._ensure_volumes(mounts)

        assert calls == []

    def test_creates_when_inspect_fails(self, monkeypatch):
        calls, fake_run = self._record_run({("volume", "inspect", "hatchery-uv"): 1})
        monkeypatch.setattr(docker, "run", fake_run)

        mounts = [mount.VolumeMount(name="hatchery-uv", dst="/cache", mode="RW", task_scoped=False)]
        docker.DockerRuntime()._ensure_volumes(mounts)

        assert calls == [
            ["docker", "volume", "inspect", "hatchery-uv"],
            ["docker", "volume", "create", "hatchery-uv"],
        ]

    def test_skips_create_when_inspect_succeeds(self, monkeypatch):
        calls, fake_run = self._record_run({("volume", "inspect", "hatchery-uv"): 0})
        monkeypatch.setattr(docker, "run", fake_run)

        mounts = [mount.VolumeMount(name="hatchery-uv", dst="/cache", mode="RW", task_scoped=False)]
        docker.PodmanRuntime()._ensure_volumes(mounts)

        assert calls == [["podman", "volume", "inspect", "hatchery-uv"]]

    def test_dedupes_repeated_names(self, monkeypatch):
        calls, fake_run = self._record_run({("volume", "inspect", "hatchery-uv"): 0})
        monkeypatch.setattr(docker, "run", fake_run)

        mounts = [
            mount.VolumeMount(name="hatchery-uv", dst="/cache/a", mode="RW", task_scoped=False),
            mount.VolumeMount(name="hatchery-uv", dst="/cache/b", mode="RW", task_scoped=False),
        ]
        docker.DockerRuntime()._ensure_volumes(mounts)

        assert calls == [["docker", "volume", "inspect", "hatchery-uv"]]


class TestDefaultHomeMounts:
    def test_default_home_mounts(self, tmp_path, monkeypatch):
        # Canary: assert the exact set of default home mounts so any
        # accidental change to the defaults shows up loudly in tests.
        home = tmp_path / "home"
        (home / ".cache" / "uv").mkdir(parents=True)
        (home / ".gitconfig").write_text("[user]\n")
        monkeypatch.setattr(docker.Path, "home", lambda: home)

        assert docker._default_home_mounts() == [
            mount.BindMount(src=str(home / ".gitconfig"), dst=f"{agent.CONTAINER_HOME}/.gitconfig", mode="RO"),
        ]


class TestBuildMountsIncludesVolumes:
    def _make_backend(self):
        b = MagicMock()
        b.construct_mounts = MagicMock(return_value=[])
        return b

    def test_no_worktree_appends_volume_mount(self, tmp_path, monkeypatch):
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.setattr(docker, "_default_home_mounts", lambda: [])

        cfg = docker.DockerConfig(volumes=[{"name": "uv-cache", "path": "/home/hatchery/.cache/uv"}])
        mounts = docker.build_mounts(_no_wt_meta(cwd), self._make_backend(), tmp_path, cfg)

        expected = mount.VolumeMount(
            name="hatchery-uv-cache", dst="/home/hatchery/.cache/uv", mode="RW", task_scoped=False
        )
        assert expected in mounts


# ---------------------------------------------------------------------------
# I9: build_mounts — record store RW mount in no-commit mode
# ---------------------------------------------------------------------------


class TestBuildMountsNoCommit:
    def _make_backend(self):
        b = MagicMock()
        b.construct_mounts = MagicMock(return_value=[])
        return b

    def test_no_commit_task_has_ro_store_and_rw_task_file(self, tmp_path, monkeypatch):
        """No-commit task: RO store mount + RW own-task-file mount."""
        monkeypatch.setattr(docker, "_default_home_mounts", lambda: [])
        monkeypatch.setattr(constants, "HATCHERY_DIR", tmp_path)
        import seekr_hatchery.sessions as sessions

        monkeypatch.setattr(sessions, "_TASKS_DB_DIR", tmp_path / "tasks")

        meta = SessionMeta(
            name="t",
            repo=str(tmp_path),
            worktree=str(tmp_path / "wt"),
            no_commit=True,
            type="task",
        )
        # Create the task file (in its per-task subdirectory) so meta.task_file resolves
        hdir = meta.hatchery_dir
        (hdir / "tasks" / "2026-01-01-t").mkdir(parents=True)
        task_file = hdir / "tasks" / "2026-01-01-t" / "task.md"
        task_file.write_text("# task\n")

        cfg = docker.DockerConfig()
        mounts = docker.build_mounts(meta, self._make_backend(), tmp_path / "sd", cfg)

        # 1. RO mount of the whole hatchery_dir
        expected_ro = mount.BindMount(src=str(hdir), dst=str(hdir), mode="RO")
        assert expected_ro in mounts

        # 2. RW mount of the task's subdirectory (not the file itself — a
        #    directory-level mount is required for atomic tmpfile+rename saves)
        expected_rw = mount.BindMount(src=str(task_file.parent), dst=str(task_file.parent), mode="RW")
        assert expected_rw in mounts

        # 3. No other RW mount under hdir (no sibling task or Dockerfile RW)
        rw_under_hdir = [
            m for m in mounts if isinstance(m, mount.BindMount) and m.mode == "RW" and str(hdir) in str(m.src)
        ]
        assert len(rw_under_hdir) == 1
        assert str(rw_under_hdir[0].src) == str(task_file.parent)

    def test_commit_mode_no_store_mount(self, tmp_path, monkeypatch):
        """Commit mode: no hatchery_dir mount at all."""
        monkeypatch.setattr(docker, "_default_home_mounts", lambda: [])
        meta = SessionMeta(
            name="t",
            repo=str(tmp_path),
            worktree=str(tmp_path / "wt"),
            no_commit=False,
            type="task",
        )
        cfg = docker.DockerConfig()
        mounts = docker.build_mounts(meta, self._make_backend(), tmp_path / "sd", cfg)
        for m in mounts:
            if hasattr(m, "src") and "repos" in str(m.src):
                assert False, "hatchery_dir mount should not be present in commit mode"

    def test_no_commit_chat_no_store_mount(self, tmp_path, monkeypatch):
        """Chat type: no store mount even in no-commit mode."""
        monkeypatch.setattr(docker, "_default_home_mounts", lambda: [])
        monkeypatch.setattr(constants, "HATCHERY_DIR", tmp_path)
        import seekr_hatchery.sessions as sessions

        monkeypatch.setattr(sessions, "_TASKS_DB_DIR", tmp_path / "tasks")

        meta = SessionMeta(
            name="t",
            repo=str(tmp_path),
            worktree=str(tmp_path),
            no_commit=True,
            type="chat",
            no_worktree=True,
        )
        cfg = docker.DockerConfig()
        mounts = docker.build_mounts(meta, self._make_backend(), tmp_path / "sd", cfg)
        for m in mounts:
            if hasattr(m, "src") and "repos" in str(m.src):
                assert False, "hatchery_dir mount should not be present for chat type"

    def test_no_commit_task_file_missing_ro_only(self, tmp_path, monkeypatch):
        """If find_task_file returns None, only the RO store mount is present."""
        monkeypatch.setattr(docker, "_default_home_mounts", lambda: [])
        monkeypatch.setattr(constants, "HATCHERY_DIR", tmp_path)
        import seekr_hatchery.sessions as sessions

        monkeypatch.setattr(sessions, "_TASKS_DB_DIR", tmp_path / "tasks")

        meta = SessionMeta(
            name="t",
            repo=str(tmp_path),
            worktree=str(tmp_path / "wt"),
            no_commit=True,
            type="task",
        )
        # No task file created — meta.task_file will be None
        hdir = meta.hatchery_dir
        hdir.mkdir(parents=True, exist_ok=True)

        cfg = docker.DockerConfig()
        mounts = docker.build_mounts(meta, self._make_backend(), tmp_path / "sd", cfg)

        # RO mount present
        expected_ro = mount.BindMount(src=str(hdir), dst=str(hdir), mode="RO")
        assert expected_ro in mounts

        # No RW mount under hdir
        rw_under_hdir = [
            m for m in mounts if isinstance(m, mount.BindMount) and m.mode == "RW" and str(hdir) in str(m.src)
        ]
        assert len(rw_under_hdir) == 0


# ensure_dockerfile / ensure_docker_config
# ---------------------------------------------------------------------------


class TestEnsureDockerfileGenerate:
    def test_generates_when_parent_dir_missing(self, tmp_path, monkeypatch):
        """``ensure_dockerfile(target)`` must create target/.hatchery/
        before writing the Dockerfile, even if .hatchery/ doesn't exist yet.
        """
        target = tmp_path / "target"
        target.mkdir()  # target exists but has NO .hatchery/ subdir

        monkeypatch.setattr("builtins.input", lambda _: "n")

        created = docker.ensure_dockerfile(target, agent.CODEX)

        assert created is True
        assert (target / "Dockerfile.codex").exists()

    def test_returns_false_when_already_exists(self, tmp_path, monkeypatch):
        target = tmp_path / "target"
        target.mkdir()
        (target / "Dockerfile.codex").write_text("FROM debian\n")

        monkeypatch.setattr("builtins.input", lambda _: "n")

        created = docker.ensure_dockerfile(target, agent.CODEX)
        assert created is False


# ---------------------------------------------------------------------------
# parse_docker_include_entry()
# ---------------------------------------------------------------------------


class TestParseDockerIncludeEntry:
    def test_string_gives_worktree_mode(self):
        assert docker.parse_docker_include_entry("../repo") == ("../repo", "worktree")

    def test_item_with_mode_ro(self):
        from seekr_hatchery.includes import IncludeItem

        assert docker.parse_docker_include_entry(IncludeItem(path="../docs", mode="ro")) == ("../docs", "ro")

    def test_item_with_mode_rw(self):
        from seekr_hatchery.includes import IncludeItem

        assert docker.parse_docker_include_entry(IncludeItem(path="../shared", mode="rw")) == ("../shared", "rw")

    def test_item_with_mode_worktree(self):
        from seekr_hatchery.includes import IncludeItem

        assert docker.parse_docker_include_entry(IncludeItem(path="../repo", mode="worktree")) == (
            "../repo",
            "worktree",
        )

    def test_item_without_mode_defaults_to_worktree(self):
        from seekr_hatchery.includes import IncludeItem

        assert docker.parse_docker_include_entry(IncludeItem(path="../repo")) == ("../repo", "worktree")


# ---------------------------------------------------------------------------
# DockerConfig.follow_symlinks field
# ---------------------------------------------------------------------------


class TestDockerConfigFollowSymlinks:
    def test_defaults_to_false(self):
        assert docker.DockerConfig().follow_symlinks is False

    def test_parses_true(self):
        assert docker.DockerConfig(follow_symlinks=True).follow_symlinks is True


# ---------------------------------------------------------------------------
# follow_symlinks sets follow_links="DEEP" on the tree mount
#
# The scan itself lives in mount_links now; see tests/test_mount_links.py.
# What's tested here is only the wiring: that the config field reaches the
# right mount, and that the resulting mounts are what a launch needs.
# ---------------------------------------------------------------------------


class TestNoWorktreeFollowSymlinks:
    def _make_backend(self):
        b = MagicMock()
        b.construct_mounts = MagicMock(return_value=[])
        return b

    def test_disabled_skips_symlink_scan(self, tmp_path, monkeypatch):
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        (cwd / "link").symlink_to(external)
        # Avoid coupling to the user's real home mounts (e.g. uv cache).
        monkeypatch.setattr(docker, "_default_home_mounts", lambda: [])

        cfg = docker.DockerConfig(follow_symlinks=False)
        mounts = docker.build_mounts(_no_wt_meta(cwd), self._make_backend(), tmp_path, cfg)

        target = external.resolve()
        assert mount.BindMount(src=str(target), dst=str(target), mode="RW") not in mounts

    def test_enabled_adds_symlink_mounts(self, tmp_path, monkeypatch):
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        (cwd / "link").symlink_to(external)
        monkeypatch.setattr(docker, "_default_home_mounts", lambda: [])

        cfg = docker.DockerConfig(follow_symlinks=True)
        mounts = docker.build_mounts(_no_wt_meta(cwd), self._make_backend(), tmp_path, cfg)

        target = external.resolve()
        assert mount.BindMount(src=str(target), dst=str(target), mode="RW") in mounts

    def test_enabled_declares_the_flag_on_the_tree_mount(self, tmp_path, monkeypatch):
        """The config field is per-mount state, not a separate scan pass —
        which is what lets one expansion cover the worktree, the docker.yaml
        mounts and the agent config dirs alike."""
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.setattr(docker, "_default_home_mounts", lambda: [])
        monkeypatch.setattr(mount_links, "expand_link_mounts", lambda mounts: mounts)

        cfg = docker.DockerConfig(follow_symlinks=True)
        mounts = docker.build_mounts(_no_wt_meta(cwd), self._make_backend(), tmp_path, cfg)

        assert [m.follow_links for m in mounts if isinstance(m, mount.BindMount) and m.dst == str(cwd)] == [True]

    def test_enabled_tolerates_backend_volume_and_tmpfs_mounts(self, tmp_path, monkeypatch):
        """Volume and tmpfs mounts have no host path at all — reading one
        raised AttributeError and took down every launch that had
        follow_symlinks on, since both backends contribute volumes."""
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        (cwd / "link").symlink_to(external)
        monkeypatch.setattr(docker, "_default_home_mounts", lambda: [])

        backend = MagicMock()
        backend.construct_mounts = MagicMock(
            return_value=[
                mount.VolumeMount(name="claude-dir", dst="/home/hatchery/.claude"),
                mount.TmpfsMount(dst="/tmp/scratch"),
            ]
        )
        cfg = docker.DockerConfig(follow_symlinks=True)
        mounts = docker.build_mounts(_no_wt_meta(cwd), backend, tmp_path, cfg)

        target = external.resolve()
        assert mount.BindMount(src=str(target), dst=str(target), mode="RW") in mounts


# ---------------------------------------------------------------------------
# _validate_mounts
# ---------------------------------------------------------------------------


class TestValidateMounts:
    """The one place mount safety is decided, for every source of mounts.

    A dangerous mount can be asked for directly (``docker.yaml mounts:``) or
    arrive from a resolved symlink, so the check runs over the finished list
    rather than inside each producer.
    """

    def test_ordinary_mounts_pass_through_unchanged(self, tmp_path):
        mounts = [
            mount.BindMount(src=tmp_path, dst="/home/hatchery/x", mode="RW"),
            mount.VolumeMount(name="v", dst="/home/hatchery/v"),
            mount.TmpfsMount(dst="/tmp/scratch"),
        ]
        assert docker._validate_mounts(mounts) == mounts

    @pytest.mark.parametrize("dst", ["/usr", "/usr/lib/foo", "/etc/ssl", "/proc", "/dev/shm"])
    def test_system_destinations_dropped(self, tmp_path, dst, capsys):
        keep = mount.BindMount(src=tmp_path, dst="/home/hatchery/keep", mode="RW")
        blocked = mount.BindMount(src=tmp_path, dst=dst, mode="RW")

        assert docker._validate_mounts([keep, blocked]) == [keep]
        # Dropped, but never silently.
        out = capsys.readouterr().out
        assert dst.split("/")[1] in out
        assert str(tmp_path) in out

    def test_subpaths_of_unblocked_roots_are_kept(self, tmp_path):
        # /tmp, /var, /home and /opt are deliberately not blocked.
        mounts = [mount.BindMount(src=tmp_path, dst=d, mode="RW") for d in ("/tmp/x", "/var/x", "/opt/x", "/home/x")]
        assert docker._validate_mounts(mounts) == mounts

    def test_non_bind_mounts_are_never_dropped(self):
        # Volume/tmpfs mounts have no host source to judge; /dev/shm as a
        # tmpfs destination is the container's business, not ours.
        mounts = [mount.TmpfsMount(dst="/dev/shm"), mount.VolumeMount(name="v", dst="/usr/local")]
        assert docker._validate_mounts(mounts) == mounts

    def test_macos_unshared_source_warns_but_is_kept(self, tmp_path, monkeypatch, capsys):
        """A podman machine shares only /Users and /private/tmp.

        Anything else binds as an empty directory with no error at all, so the
        warning is the only signal the user ever gets.
        """
        monkeypatch.setattr(docker.sys, "platform", "darwin")
        m = mount.BindMount(src=tmp_path, dst="/home/hatchery/x", mode="RW")

        assert docker._validate_mounts([m]) == [m]
        assert "may appear empty" in capsys.readouterr().out

    def test_macos_warns_for_a_single_file_bind(self, tmp_path, monkeypatch, capsys):
        """The gap this closes: a symlinked config *file*.

        It needs no follow_links — `-v` resolves the source — but it can still
        resolve somewhere the VM cannot see, and nothing used to say so.
        """
        target = tmp_path / "dotfiles" / "AGENTS.md"
        target.parent.mkdir()
        target.write_text("# global\n")
        link = tmp_path / "home" / ".codex" / "AGENTS.md"
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        monkeypatch.setattr(docker.sys, "platform", "darwin")

        m = mount.BindMount(src=link, dst="/home/hatchery/.codex/AGENTS.md", mode="RW")
        assert docker._validate_mounts([m]) == [m]
        out = capsys.readouterr().out
        assert str(target) in out  # names the resolved target, not just the link

    def test_macos_shared_source_is_silent(self, monkeypatch, capsys):
        monkeypatch.setattr(docker.sys, "platform", "darwin")
        m = mount.BindMount(src=Path("/Users/someone/x"), dst="/home/hatchery/x", mode="RW")

        assert docker._validate_mounts([m]) == [m]
        assert capsys.readouterr().out == ""

    def test_non_darwin_does_not_warn(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(docker.sys, "platform", "linux")
        m = mount.BindMount(src=tmp_path, dst="/home/hatchery/x", mode="RW")

        assert docker._validate_mounts([m]) == [m]
        assert capsys.readouterr().out == ""


class TestBuildMountsDropsExpandedSystemMounts:
    def test_symlink_into_a_system_path_never_reaches_the_runtime(self, tmp_path, monkeypatch, capsys):
        """The seam, end to end.

        Expansion emits a mount at /usr/share because that is where the link
        points; validation then drops it. Asserted through build_mounts so the
        two halves stay pinned together.

        An absolute link deliberately, not the relative-offset case: that one
        only lands on a system path when the container directory is shallower
        than the host one, which under a pytest tmp_path means the *host*
        target has to sit outside tmp_path. Not worth writing outside the
        fixture for — ``test_mount_links.py`` covers that arithmetic at the
        unit level, where the target stays inside the pytest root.
        """
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.setattr(docker, "_default_home_mounts", lambda: [])

        skills = tmp_path / "host-claude" / "skills"
        skills.mkdir(parents=True)
        dst = "/home/hatchery/.claude/skills"
        (skills / "sys").symlink_to("/usr/share", target_is_directory=True)

        backend = MagicMock()
        backend.construct_mounts = MagicMock(
            return_value=[mount.BindMount(src=skills, dst=dst, mode="RW", follow_links=True)]
        )
        mounts = docker.build_mounts(_no_wt_meta(cwd), backend, tmp_path, docker.DockerConfig())

        assert "/usr/share" not in [m.dst for m in mounts]
        assert "shadow the sandbox" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# follow_links expansion
# ---------------------------------------------------------------------------


class TestBuildMountsFollowLinks:
    def test_symlinked_skill_target_is_mounted_at_its_own_path(self, tmp_path, monkeypatch):
        """End-to-end: a backend declaring follow_links on a directory that
        mixes a real skill with an absolute-symlinked one keeps the directory
        bound whole and gains one mount making the link's target exist."""
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.setattr(docker, "_default_home_mounts", lambda: [])

        skills = tmp_path / "host-claude" / "skills"
        real = skills / "agent-comm"
        real.mkdir(parents=True)
        (real / "SKILL.md").write_text("# real\n")
        linked = tmp_path / "dotfiles" / "panel-review"
        linked.mkdir(parents=True)
        (linked / "SKILL.md").write_text("# linked\n")
        (skills / "panel-review").symlink_to(linked)

        dst = "/home/hatchery/.claude/skills"
        backend = MagicMock()
        backend.construct_mounts = MagicMock(
            return_value=[mount.BindMount(src=skills, dst=dst, mode="RW", follow_links=True)]
        )
        mounts = docker.build_mounts(_no_wt_meta(cwd), backend, tmp_path, docker.DockerConfig())

        binds = {m.dst: m.src for m in mounts if isinstance(m, mount.BindMount)}
        # The directory is still one mount; the real skill needs nothing.
        assert binds[dst] == skills
        assert f"{dst}/agent-comm" not in binds
        assert f"{dst}/panel-review" not in binds
        # ...and the link's stored host path now exists in the container.
        assert binds[str(linked)] == linked
        assert not any(getattr(m, "follow_links", False) for m in mounts)


# ---------------------------------------------------------------------------
# clipboard_images
# ---------------------------------------------------------------------------


class TestDockerConfigClipboardImages:
    def test_defaults_to_true(self):
        assert docker.DockerConfig().clipboard_images is True

    def test_parses_false(self):
        assert docker.DockerConfig(clipboard_images=False).clipboard_images is False


class TestClipboardImageMount:
    def _make_backend(self):
        b = MagicMock()
        b.construct_mounts = MagicMock(return_value=[])
        return b

    def test_no_worktree_enabled_adds_identical_mount(self, tmp_path, monkeypatch):
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        monkeypatch.setattr(docker, "_default_home_mounts", lambda: [])

        cfg = docker.DockerConfig(clipboard_images=True)
        mounts = docker.build_mounts(_no_wt_meta(cwd), self._make_backend(), session_dir, cfg)

        clip = session_dir / "clipboard"
        assert mount.BindMount(src=str(clip), dst=str(clip), mode="RW") in mounts
        # And the directory was actually created on the host.
        assert clip.is_dir()

    def test_no_worktree_disabled_omits_mount(self, tmp_path, monkeypatch):
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        monkeypatch.setattr(docker, "_default_home_mounts", lambda: [])

        cfg = docker.DockerConfig(clipboard_images=False)
        mounts = docker.build_mounts(_no_wt_meta(cwd), self._make_backend(), session_dir, cfg)

        clip = session_dir / "clipboard"
        assert not any(str(clip) == str(m.src) for m in mounts)
        # And we did not create the directory.
        assert not clip.exists()


class TestMakePasteInterceptor:
    def test_enabled_returns_interceptor_wired_to_session_dir(self, tmp_path):
        backend = MagicMock()
        backend.format_image_reference = MagicMock(side_effect=lambda p: str(p))
        cfg = docker.DockerConfig(clipboard_images=True)
        pi = docker._make_paste_interceptor(backend, tmp_path, cfg)
        assert pi is not None
        # And the interceptor writes to the per-task clipboard dir.
        assert pi._target_dir == docker.clipboard_image_dir(tmp_path)

    def test_disabled_returns_none(self, tmp_path):
        backend = MagicMock()
        cfg = docker.DockerConfig(clipboard_images=False)
        assert docker._make_paste_interceptor(backend, tmp_path, cfg) is None


class TestRemoveClipboardDir:
    def test_removes_existing_directory(self, tmp_path):
        clip = docker.clipboard_image_dir(tmp_path)
        clip.mkdir(parents=True)
        (clip / "paste-1.png").write_bytes(b"x")
        (clip / "paste-2.png").write_bytes(b"y")

        docker.remove_clipboard_dir(tmp_path)

        assert not clip.exists()
        # session_dir itself is preserved — only the clipboard subdir was cleaned.
        assert tmp_path.exists()

    def test_idempotent_when_dir_absent(self, tmp_path):
        # No clipboard subdir was ever created.
        docker.remove_clipboard_dir(tmp_path)  # must not raise
        assert not docker.clipboard_image_dir(tmp_path).exists()
