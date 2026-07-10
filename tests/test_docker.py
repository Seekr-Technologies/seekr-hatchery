"""Tests for docker.py functions (runtime detection, container execution)."""

import subprocess
import sys as _sys
from unittest.mock import MagicMock

import pytest

import seekr_hatchery.agents as agent
import seekr_hatchery.constants as constants
import seekr_hatchery.docker as docker
import seekr_hatchery.mount as mount
from seekr_hatchery.models import SessionMeta


def _no_wt_meta(cwd):
    """Synthetic SessionMeta for no-worktree mount tests."""
    return SessionMeta(name="-", repo=str(cwd), worktree=str(cwd), no_worktree=True)


# ---------------------------------------------------------------------------
# docker_available()
# ---------------------------------------------------------------------------


class TestDockerAvailable:
    def test_returns_true_when_rc_zero(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 0
        monkeypatch.setattr(docker, "run", lambda *a, **kw: mock_result)
        assert docker.docker_available() is True

    def test_returns_false_when_rc_nonzero(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 1
        monkeypatch.setattr(docker, "run", lambda *a, **kw: mock_result)
        assert docker.docker_available() is False

    def test_returns_false_when_binary_not_found(self, monkeypatch):
        def _raise(*a, **kw):
            raise FileNotFoundError("No such file or directory: 'docker'")

        monkeypatch.setattr(docker, "run", _raise)
        assert docker.docker_available() is False


# ---------------------------------------------------------------------------
# podman_available()
# ---------------------------------------------------------------------------


class TestPodmanAvailable:
    def test_returns_true_when_rc_zero(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 0
        monkeypatch.setattr(docker, "run", lambda *a, **kw: mock_result)
        assert docker.podman_available() is True

    def test_returns_false_when_rc_nonzero(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 1
        monkeypatch.setattr(docker, "run", lambda *a, **kw: mock_result)
        assert docker.podman_available() is False

    def test_returns_false_when_binary_not_found(self, monkeypatch):
        def _raise(*a, **kw):
            raise FileNotFoundError("No such file or directory: 'podman'")

        monkeypatch.setattr(docker, "run", _raise)
        assert docker.podman_available() is False


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
        result = docker.resolve_runtime(repo, worktree, no_docker=True)
        assert result is None

    def test_exits_when_no_dockerfile_and_docker_not_disabled(self, tmp_path, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        # No Dockerfile in worktree — should error, not silently run natively
        with pytest.raises(SystemExit) as exc_info:
            docker.resolve_runtime(repo, worktree, no_docker=False)
        assert exc_info.value.code == 1
        assert "No Dockerfile found" in capsys.readouterr().err

    def test_returns_podman_when_dockerfile_and_podman_available(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        dockerfile_dir = worktree / ".hatchery"
        dockerfile_dir.mkdir()
        (dockerfile_dir / "Dockerfile.codex").write_text("FROM debian\n")
        monkeypatch.setattr(docker, "detect_runtime", lambda: docker.PodmanRuntime())
        result = docker.resolve_runtime(repo, worktree, no_docker=False)
        assert isinstance(result, docker.PodmanRuntime)

    def test_returns_docker_when_dockerfile_and_docker_available(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        dockerfile_dir = worktree / ".hatchery"
        dockerfile_dir.mkdir()
        (dockerfile_dir / "Dockerfile.codex").write_text("FROM debian\n")
        monkeypatch.setattr(docker, "detect_runtime", lambda: docker.DockerRuntime())
        result = docker.resolve_runtime(repo, worktree, no_docker=False)
        assert isinstance(result, docker.DockerRuntime)

    def test_exits_when_dockerfile_present_but_no_runtime(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        dockerfile_dir = worktree / ".hatchery"
        dockerfile_dir.mkdir()
        (dockerfile_dir / "Dockerfile.codex").write_text("FROM debian\n")
        monkeypatch.setattr(docker, "detect_runtime", lambda: (_ for _ in ()).throw(SystemExit(1)))
        with pytest.raises(SystemExit) as exc_info:
            docker.resolve_runtime(repo, worktree, no_docker=False)
        assert exc_info.value.code == 1

    def test_stderr_message_when_no_runtime(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        dockerfile_dir = worktree / ".hatchery"
        dockerfile_dir.mkdir()
        (dockerfile_dir / "Dockerfile.codex").write_text("FROM debian\n")

        def _exit():
            print("Error: neither Podman nor Docker is running.", file=_sys.stderr)
            raise SystemExit(1)

        monkeypatch.setattr(docker, "detect_runtime", _exit)
        with pytest.raises(SystemExit):
            docker.resolve_runtime(repo, worktree, no_docker=False)
        captured = capsys.readouterr()
        assert "Podman" in captured.err or "Docker" in captured.err

    def test_no_docker_flag_skips_dockerfile_check(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        # Even with Dockerfile present, no_docker=True returns None
        dockerfile_dir = worktree / ".hatchery"
        dockerfile_dir.mkdir()
        (dockerfile_dir / "Dockerfile.codex").write_text("FROM debian\n")
        result = docker.resolve_runtime(repo, worktree, no_docker=True)
        assert result is None

    def test_agent_specific_dockerfile_detected(self, tmp_path, monkeypatch):
        worktree = tmp_path / "worktree"
        (worktree / ".hatchery").mkdir(parents=True)
        docker.dockerfile_path(worktree, agent.CODEX).write_text("FROM debian\n")
        monkeypatch.setattr(docker, "detect_runtime", lambda: docker.DockerRuntime())
        assert isinstance(
            docker.resolve_runtime(tmp_path, worktree, no_docker=False, backend=agent.CODEX), docker.DockerRuntime
        )


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
        runtime: docker.ContainerRuntime = None,
        mutator=None,
        proxy_token: str = "proxy-uuid-token",
        proxy_port: int = 9999,
        **spec_kwargs,
    ) -> list[str]:
        if runtime is None:
            runtime = docker.DockerRuntime()
        if mutator is None:
            mutator = _make_mutator()
        # Stub _ensure_volumes so no real subprocess calls happen.
        monkeypatch.setattr(runtime, "_ensure_volumes", lambda _mounts: None)
        spec = docker.build_spec(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            name="test-task",
            hatchery_repo="/repo",
            container_name=None,
            mutator=mutator,
            proxy_token=proxy_token,
            proxy_port=proxy_port,
            agent_cmd=["codex"],
            backend=agent.CODEX,
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
            mutator=None,
            proxy_token=None,
            proxy_port=None,
            agent_cmd=["codex"],
            backend=agent.CODEX,
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
            mutator=None,
            proxy_token=None,
            proxy_port=None,
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
            mutator=None,
            proxy_token=None,
            proxy_port=None,
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
            mutator=None,
            proxy_token=None,
            proxy_port=None,
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
            mutator=None,
            proxy_token=None,
            proxy_port=None,
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
            mutator=None,
            proxy_token=None,
            proxy_port=None,
            agent_cmd=[],
            command_override=["echo", "hello"],
        )
        runtime.run(spec)
        assert "-it" not in captured[0]


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

        docker.build_docker_image(repo, worktree, "test-task", agent.CODEX, runtime=docker.Runtime.PODMAN)
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


# ensure_docker_files_uncommitted
# ---------------------------------------------------------------------------


class TestEnsureDockerFilesUncommitted:
    def test_copies_from_repo_root_when_worktree_missing(self, tmp_path, monkeypatch):
        """When files exist in repo root but not in worktree, they are copied."""
        repo = tmp_path / "repo"
        worktree = tmp_path / "worktree"
        for d in (repo / ".hatchery", worktree / ".hatchery"):
            d.mkdir(parents=True)

        # Place files only in repo root
        (repo / ".hatchery" / "Dockerfile.codex").write_text("FROM debian\n")
        (repo / constants.DOCKER_CONFIG).write_text("schema_version: '1'\n")

        # suppress interactive prompts (shouldn't be hit, but be safe)
        monkeypatch.setattr("builtins.input", lambda _: "n")

        docker.ensure_docker_files_uncommitted(repo, worktree, agent.CODEX)

        assert (worktree / ".hatchery" / "Dockerfile.codex").exists()
        assert (worktree / constants.DOCKER_CONFIG).exists()

    def test_copies_from_source_when_target_parent_dir_missing(self, tmp_path, monkeypatch):
        """``ensure_dockerfile(target, source=...)`` must create target/.hatchery/
        before shutil.copy2 lands. Regression for the bug where the mkdir ran
        only on the generate-from-template path and the source-copy path failed
        with FileNotFoundError if .hatchery/ didn't exist in the target.
        """
        source = tmp_path / "source"
        target = tmp_path / "target"
        (source / ".hatchery").mkdir(parents=True)
        target.mkdir()  # target exists but has NO .hatchery/ subdir
        (source / ".hatchery" / "Dockerfile.codex").write_text("FROM debian\n")
        (source / constants.DOCKER_CONFIG).write_text("schema_version: '1'\n")

        monkeypatch.setattr("builtins.input", lambda _: "n")

        # Should not raise FileNotFoundError; should copy both files.
        docker.ensure_dockerfile(target, agent.CODEX, source=source)
        docker.ensure_docker_config(target, source=source)

        assert (target / ".hatchery" / "Dockerfile.codex").exists()
        assert (target / constants.DOCKER_CONFIG).exists()
        # Content matches the source (we copied, not generated from template).
        assert (target / ".hatchery" / "Dockerfile.codex").read_text() == "FROM debian\n"

    def test_generates_when_repo_root_also_missing(self, tmp_path, monkeypatch):
        """When neither repo root nor worktree has files, generates from template."""
        repo = tmp_path / "repo"
        worktree = tmp_path / "worktree"
        for d in (repo / ".hatchery", worktree / ".hatchery"):
            d.mkdir(parents=True)

        monkeypatch.setattr("builtins.input", lambda _: "n")

        docker.ensure_docker_files_uncommitted(repo, worktree, agent.CODEX)

        assert (repo / ".hatchery" / "Dockerfile.codex").exists()
        assert (worktree / ".hatchery" / "Dockerfile.codex").exists()
        assert (repo / constants.DOCKER_CONFIG).exists()
        assert (worktree / constants.DOCKER_CONFIG).exists()

    def test_worktree_files_unchanged_when_already_present(self, tmp_path, monkeypatch):
        """When worktree already has files, they are not overwritten."""
        repo = tmp_path / "repo"
        worktree = tmp_path / "worktree"
        for d in (repo / ".hatchery", worktree / ".hatchery"):
            d.mkdir(parents=True)

        original_df = "FROM custom-image\n"
        original_cfg = "schema_version: '1'\nmounts: []\n"
        (worktree / ".hatchery" / "Dockerfile.codex").write_text(original_df)
        (worktree / constants.DOCKER_CONFIG).write_text(original_cfg)

        monkeypatch.setattr("builtins.input", lambda _: "n")

        docker.ensure_docker_files_uncommitted(repo, worktree, agent.CODEX)

        # Worktree files should be untouched
        assert (worktree / ".hatchery" / "Dockerfile.codex").read_text() == original_df
        assert (worktree / constants.DOCKER_CONFIG).read_text() == original_cfg


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
# _construct_symlink_mounts()
# ---------------------------------------------------------------------------


class TestConstructSymlinkMounts:
    def _scan_root(self, tmp_path):
        """Build an isolated worktree-like directory under tmp_path."""
        root = tmp_path / "worktree"
        root.mkdir()
        return root

    def test_external_file_symlink_emits_mount(self, tmp_path):
        scan = self._scan_root(tmp_path)
        external = tmp_path / "external" / "file.txt"
        external.parent.mkdir()
        external.write_text("hello")
        (scan / "link").symlink_to(external)

        mounts = docker._construct_symlink_mounts(scan, [])

        target = external.resolve()
        assert mounts == [mount.BindMount(src=str(target), dst=str(target), mode="RW")]

    def test_external_dir_symlink_emits_mount(self, tmp_path):
        scan = self._scan_root(tmp_path)
        external = tmp_path / "external" / "dir"
        external.mkdir(parents=True)
        (external / "child").write_text("x")
        (scan / "linkdir").symlink_to(external)

        mounts = docker._construct_symlink_mounts(scan, [])

        target = external.resolve()
        assert mount.BindMount(src=str(target), dst=str(target), mode="RW") in mounts

    def test_relative_internal_symlink_skipped(self, tmp_path):
        """Relative links staying inside scan_root resolve correctly in the
        container and need no extra mount."""
        scan = self._scan_root(tmp_path)
        (scan / "inner.txt").write_text("x")
        (scan / "link").symlink_to("inner.txt")

        mounts = docker._construct_symlink_mounts(scan, [])

        assert mounts == []

    def test_dedupes_same_target(self, tmp_path):
        scan = self._scan_root(tmp_path)
        external = tmp_path / "external" / "file.txt"
        external.parent.mkdir()
        external.write_text("hello")
        (scan / "a").symlink_to(external)
        (scan / "b").symlink_to(external)

        mounts = docker._construct_symlink_mounts(scan, [])

        assert len(mounts) == 1

    def test_already_covered_by_existing_mount(self, tmp_path):
        scan = self._scan_root(tmp_path)
        external_root = tmp_path / "external"
        external_root.mkdir()
        external_file = external_root / "file.txt"
        external_file.write_text("x")
        (scan / "link").symlink_to(external_file)

        # external_root is already a mount; its child should be skipped
        existing = [mount.BindMount(src=str(external_root), dst="/mounted/external", mode="RO")]
        mounts = docker._construct_symlink_mounts(scan, existing)

        assert mounts == []

    def test_broken_symlink_skipped(self, tmp_path):
        scan = self._scan_root(tmp_path)
        (scan / "broken").symlink_to(tmp_path / "does-not-exist")

        mounts = docker._construct_symlink_mounts(scan, [])

        assert mounts == []

    def test_system_path_target_skipped(self, tmp_path):
        scan = self._scan_root(tmp_path)
        # Use /usr/bin/env which exists on all Linux/macOS test runners
        (scan / "syslink").symlink_to("/usr/bin/env")

        mounts = docker._construct_symlink_mounts(scan, [])

        assert mounts == []

    def test_heavyweight_dir_pruned(self, tmp_path):
        scan = self._scan_root(tmp_path)
        external = tmp_path / "external" / "file.txt"
        external.parent.mkdir()
        external.write_text("x")
        node_modules = scan / "node_modules"
        node_modules.mkdir()
        (node_modules / "link").symlink_to(external)

        mounts = docker._construct_symlink_mounts(scan, [])

        # The symlink inside node_modules is never visited.
        assert mounts == []

    def test_nested_relative_internal_symlink_skipped(self, tmp_path):
        """Relative links climbing within scan_root (but not escaping) are fine."""
        scan = self._scan_root(tmp_path)
        (scan / "a").mkdir()
        (scan / "b").mkdir()
        (scan / "b" / "file.txt").write_text("x")
        (scan / "a" / "link").symlink_to("../b/file.txt")

        mounts = docker._construct_symlink_mounts(scan, [])

        assert mounts == []

    def test_nested_external_target(self, tmp_path):
        """Symlinks discovered in nested (non-skipped) subdirs still emit mounts."""
        scan = self._scan_root(tmp_path)
        nested = scan / "a" / "b"
        nested.mkdir(parents=True)
        external = tmp_path / "external" / "data"
        external.mkdir(parents=True)
        (nested / "link").symlink_to(external)

        mounts = docker._construct_symlink_mounts(scan, [])

        target = external.resolve()
        assert mounts == [mount.BindMount(src=str(target), dst=str(target), mode="RW")]

    def test_absolute_internal_link_skipped(self, tmp_path):
        """Absolute link pointing inside scan_root needs no extra mount: under
        host-path mirroring, the scan_root mount makes the absolute host path
        resolve identically inside the container."""
        scan = self._scan_root(tmp_path)
        (scan / "inner.txt").write_text("x")
        (scan / "link").symlink_to(scan / "inner.txt")  # absolute target

        mounts = docker._construct_symlink_mounts(scan, [])

        assert mounts == []

    def test_relative_external_link_emits_mount(self, tmp_path):
        """Relative link escaping scan_root needs its target mounted at the
        host path — under host-path mirroring the relative climb lands at the
        same absolute path on both sides, so a target:target bind-mount
        suffices."""
        scan = self._scan_root(tmp_path)
        external = tmp_path / "external"
        external.mkdir()
        (scan / "link").symlink_to("../external")

        mounts = docker._construct_symlink_mounts(scan, [])

        target = external.resolve()
        assert mounts == [mount.BindMount(src=str(target), dst=str(target), mode="RW")]


# ---------------------------------------------------------------------------
# docker_mounts_no_worktree honors follow_symlinks
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


class TestMaybeApiServerErrorPath:
    """``_maybe_api_server`` must surface ``backend.proxy_kwargs()`` errors
    as ``ui.error`` + ``sys.exit(1)`` so users see a clean message rather
    than a stack trace.  Mirrors the existing handling for
    ``make_header_mutator``.
    """

    def test_runtime_error_from_proxy_kwargs_exits_cleanly(self, monkeypatch, capsys):
        class BadBackend:
            def proxy_kwargs(self):
                raise RuntimeError("clean message for the user")

        with pytest.raises(SystemExit) as excinfo:
            with docker._maybe_api_server(lambda h: h, "tok", BadBackend()):
                pass
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        # ui.error writes to stderr by default.
        assert "clean message for the user" in (captured.err + captured.out)
