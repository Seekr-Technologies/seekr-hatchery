"""Tests for docker.py functions (runtime detection, container execution)."""

import subprocess
import sys as _sys
from unittest.mock import MagicMock

import pytest

import seekr_hatchery.agents as agent
import seekr_hatchery.docker as docker
import seekr_hatchery.proxy as proxy_mod
import seekr_hatchery.tasks as tasks

# ---------------------------------------------------------------------------
# docker_available()
# ---------------------------------------------------------------------------


class TestDockerAvailable:
    def test_returns_true_when_rc_zero(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 0
        monkeypatch.setattr(tasks, "run", lambda *a, **kw: mock_result)
        assert docker.docker_available() is True

    def test_returns_false_when_rc_nonzero(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 1
        monkeypatch.setattr(tasks, "run", lambda *a, **kw: mock_result)
        assert docker.docker_available() is False

    def test_returns_false_when_binary_not_found(self, monkeypatch):
        def _raise(*a, **kw):
            raise FileNotFoundError("No such file or directory: 'docker'")

        monkeypatch.setattr(tasks, "run", _raise)
        assert docker.docker_available() is False


# ---------------------------------------------------------------------------
# podman_available()
# ---------------------------------------------------------------------------


class TestPodmanAvailable:
    def test_returns_true_when_rc_zero(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 0
        monkeypatch.setattr(tasks, "run", lambda *a, **kw: mock_result)
        assert docker.podman_available() is True

    def test_returns_false_when_rc_nonzero(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 1
        monkeypatch.setattr(tasks, "run", lambda *a, **kw: mock_result)
        assert docker.podman_available() is False

    def test_returns_false_when_binary_not_found(self, monkeypatch):
        def _raise(*a, **kw):
            raise FileNotFoundError("No such file or directory: 'podman'")

        monkeypatch.setattr(tasks, "run", _raise)
        assert docker.podman_available() is False


# ---------------------------------------------------------------------------
# detect_runtime()
# ---------------------------------------------------------------------------


class TestDetectRuntime:
    def test_returns_podman_when_podman_available(self, monkeypatch):
        monkeypatch.setattr(docker, "podman_available", lambda: True)
        monkeypatch.setattr(docker, "docker_available", lambda: True)
        assert docker.detect_runtime() == docker.Runtime.PODMAN

    def test_prefers_podman_over_docker(self, monkeypatch):
        monkeypatch.setattr(docker, "podman_available", lambda: True)
        monkeypatch.setattr(docker, "docker_available", lambda: False)
        assert docker.detect_runtime() == docker.Runtime.PODMAN

    def test_falls_back_to_docker_when_podman_not_installed(self, monkeypatch):
        monkeypatch.setattr(docker, "podman_available", lambda: False)
        monkeypatch.setattr(docker.shutil, "which", lambda _: None)
        monkeypatch.setattr(docker, "docker_available", lambda: True)
        assert docker.detect_runtime() == docker.Runtime.DOCKER

    def test_exits_when_neither_available(self, monkeypatch):
        monkeypatch.setattr(docker, "podman_available", lambda: False)
        monkeypatch.setattr(docker.shutil, "which", lambda _: None)
        monkeypatch.setattr(docker, "docker_available", lambda: False)
        with pytest.raises(SystemExit) as exc_info:
            docker.detect_runtime()
        assert exc_info.value.code == 1

    def test_exits_when_podman_installed_but_not_running(self, monkeypatch):
        monkeypatch.setattr(docker, "podman_available", lambda: False)
        monkeypatch.setattr(docker.shutil, "which", lambda _: "/usr/local/bin/podman")
        with pytest.raises(SystemExit) as exc_info:
            docker.detect_runtime()
        assert exc_info.value.code == 1

    def test_installed_not_running_error_message(self, monkeypatch, capsys):
        monkeypatch.setattr(docker, "podman_available", lambda: False)
        monkeypatch.setattr(docker.shutil, "which", lambda _: "/usr/local/bin/podman")
        with pytest.raises(SystemExit):
            docker.detect_runtime()
        assert "not running" in capsys.readouterr().err

    def test_installed_not_running_macos_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(docker, "podman_available", lambda: False)
        monkeypatch.setattr(docker.shutil, "which", lambda _: "/usr/local/bin/podman")
        monkeypatch.setattr(docker.sys, "platform", "darwin")
        with pytest.raises(SystemExit):
            docker.detect_runtime()
        err = capsys.readouterr().err
        assert "podman machine start" in err
        assert "podman machine init" in err

    def test_neither_available_error_message(self, monkeypatch, capsys):
        monkeypatch.setattr(docker, "podman_available", lambda: False)
        monkeypatch.setattr(docker.shutil, "which", lambda _: None)
        monkeypatch.setattr(docker, "docker_available", lambda: False)
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
        monkeypatch.setattr(docker, "detect_runtime", lambda: docker.Runtime.PODMAN)
        result = docker.resolve_runtime(repo, worktree, no_docker=False)
        assert result == docker.Runtime.PODMAN

    def test_returns_docker_when_dockerfile_and_docker_available(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        dockerfile_dir = worktree / ".hatchery"
        dockerfile_dir.mkdir()
        (dockerfile_dir / "Dockerfile.codex").write_text("FROM debian\n")
        monkeypatch.setattr(docker, "detect_runtime", lambda: docker.Runtime.DOCKER)
        result = docker.resolve_runtime(repo, worktree, no_docker=False)
        assert result == docker.Runtime.DOCKER

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
        monkeypatch.setattr(docker, "detect_runtime", lambda: docker.Runtime.DOCKER)
        assert docker.resolve_runtime(tmp_path, worktree, no_docker=False, backend=agent.CODEX) == docker.Runtime.DOCKER


# ---------------------------------------------------------------------------
# _run_container() — runtime flag injection
# ---------------------------------------------------------------------------


def _make_mutator(key: str = "real-secret-key"):
    """Return a simple header mutator for tests."""
    def _mutate(headers):
        out = {k: v for k, v in headers.items() if k.lower() not in ("x-api-key", "authorization")}
        out["Authorization"] = f"Bearer {key}"
        return out
    return _mutate


class TestRunContainerRuntime:
    """Verify _run_container injects correct flags for each runtime."""

    def _capture_cmd(
        self,
        monkeypatch,
        runtime: docker.Runtime = docker.Runtime.DOCKER,
        mutator=None,
        proxy_token: str = "proxy-uuid-token",
        proxy_port: int = 9999,
    ) -> list[str]:
        if mutator is None:
            mutator = _make_mutator()
        captured: list[list[str]] = []

        # Mock the proxy so we don't start a real server; inject predictable values.
        mock_server = MagicMock()
        mock_server.server_address = ("0.0.0.0", proxy_port)
        monkeypatch.setattr(proxy_mod, "start_proxy", lambda _mutator, _token, **kw: (mock_server, "ignored-token"))
        monkeypatch.setattr(proxy_mod, "stop_proxy", lambda _srv: None)

        def _mock_run(cmd, **kw):
            captured.append(cmd)
            return docker.subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(docker.subprocess, "run", _mock_run)
        docker._run_container(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            hatchery_repo="/repo",
            name="test-task",
            mutator=mutator,
            proxy_token=proxy_token,
            agent_cmd=["codex"],
            backend=agent.CODEX,
            runtime=runtime,
        )
        return captured[0]

    # --- runtime binary ---

    def test_docker_runtime_uses_docker_binary(self, monkeypatch):
        cmd = self._capture_cmd(monkeypatch, runtime=docker.Runtime.DOCKER)
        assert cmd[0] == "docker"

    def test_podman_runtime_uses_podman_binary(self, monkeypatch):
        cmd = self._capture_cmd(monkeypatch, runtime=docker.Runtime.PODMAN)
        assert cmd[0] == "podman"

    # --- Podman outer-container flags ---

    def test_podman_userns_keep_id_on_linux(self, monkeypatch):
        monkeypatch.setattr(docker.sys, "platform", "linux")
        cmd = self._capture_cmd(monkeypatch, runtime=docker.Runtime.PODMAN)
        assert "--userns=keep-id" in cmd

    def test_podman_no_userns_keep_id_on_macos(self, monkeypatch):
        monkeypatch.setattr(docker.sys, "platform", "darwin")
        cmd = self._capture_cmd(monkeypatch, runtime=docker.Runtime.PODMAN)
        assert "--userns=keep-id" not in cmd

    def test_podman_runtime_adds_label_disable(self, monkeypatch):
        cmd = self._capture_cmd(monkeypatch, runtime=docker.Runtime.PODMAN)
        assert "label=disable" in " ".join(cmd)

    def test_docker_runtime_no_userns(self, monkeypatch):
        cmd = self._capture_cmd(monkeypatch, runtime=docker.Runtime.DOCKER)
        assert "--userns=keep-id" not in cmd

    def test_docker_runtime_no_label_disable(self, monkeypatch):
        cmd = self._capture_cmd(monkeypatch, runtime=docker.Runtime.DOCKER)
        assert "label=disable" not in " ".join(cmd)

    # --- Security regression guards ---

    def test_podman_no_privileged(self, monkeypatch):
        cmd = self._capture_cmd(monkeypatch, runtime=docker.Runtime.PODMAN)
        assert "--privileged" not in cmd

    def test_podman_no_seccomp_unconfined(self, monkeypatch):
        cmd = self._capture_cmd(monkeypatch, runtime=docker.Runtime.PODMAN)
        assert "seccomp=unconfined" not in " ".join(cmd)

    def test_docker_no_privileged(self, monkeypatch):
        cmd = self._capture_cmd(monkeypatch, runtime=docker.Runtime.DOCKER)
        assert "--privileged" not in cmd

    # --- API key security guards ---

    def test_real_api_key_absent_from_cmd(self, monkeypatch):
        """The real API key must never appear in the docker command."""
        mutator = _make_mutator("real-secret-key")
        cmd = self._capture_cmd(monkeypatch, mutator=mutator, proxy_token="proxy-uuid-token")
        assert "real-secret-key" not in " ".join(cmd)

    def test_proxy_token_present_as_api_key(self, monkeypatch):
        """The container's API key env var must be the proxy token, not the real key."""
        cmd = self._capture_cmd(monkeypatch, proxy_token="proxy-uuid-token")
        cmd_str = " ".join(cmd)
        assert "OPENAI_API_KEY=proxy-uuid-token" in cmd_str

    def test_base_url_points_to_proxy(self, monkeypatch):
        """OPENAI_BASE_URL must point to the host proxy port."""
        cmd = self._capture_cmd(monkeypatch, proxy_port=12345)
        cmd_str = " ".join(cmd)
        assert "OPENAI_BASE_URL" in cmd_str
        assert "host.docker.internal:12345" in cmd_str

    def test_add_host_flag_on_linux(self, monkeypatch):
        """On Linux, --add-host=host.docker.internal:host-gateway must be present."""
        monkeypatch.setattr(docker.sys, "platform", "linux")
        cmd = self._capture_cmd(monkeypatch)
        assert "--add-host=host.docker.internal:host-gateway" in cmd

    def test_no_add_host_flag_on_macos(self, monkeypatch):
        """On macOS, Docker Desktop exposes host.docker.internal natively."""
        monkeypatch.setattr(docker.sys, "platform", "darwin")
        cmd = self._capture_cmd(monkeypatch)
        assert "--add-host=host.docker.internal:host-gateway" not in cmd

    def test_proxy_token_always_set(self, monkeypatch):
        """The container API key env var must always be set to the stable proxy token."""
        cmd = self._capture_cmd(monkeypatch, proxy_token="stable-token")
        cmd_str = " ".join(cmd)
        assert "OPENAI_API_KEY=stable-token" in cmd_str

    def test_no_api_key_env_when_mutator_is_none(self, monkeypatch):
        """When mutator is None, no API key or base URL env vars should appear."""
        monkeypatch.setattr(
            proxy_mod,
            "start_proxy",
            lambda _mutator, _token, **kw: (_ for _ in ()).throw(AssertionError("should not be called")),
        )

        captured: list[list[str]] = []

        def _mock_run(cmd, **kw):
            captured.append(cmd)
            return docker.subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(docker.subprocess, "run", _mock_run)
        docker._run_container(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            hatchery_repo="/repo",
            name="test-task",
            mutator=None,
            proxy_token=None,
            agent_cmd=["codex"],
            backend=agent.CODEX,
        )
        cmd_str = " ".join(captured[0])
        assert "OPENAI_API_KEY" not in cmd_str
        assert "OPENAI_BASE_URL" not in cmd_str


# ---------------------------------------------------------------------------
# _run_container() — _interactive flag
# ---------------------------------------------------------------------------


class TestRunContainerInteractive:
    """Verify _interactive=True adds -it and does not capture output."""

    def test_interactive_override_adds_it_flags(self, monkeypatch):
        """_interactive=True + _command_override should add -it to the command."""
        captured: list[list[str]] = []

        def _mock_run(cmd, **kw):
            captured.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(docker.subprocess, "run", _mock_run)
        docker._run_container(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            hatchery_repo="/repo",
            name="test-task",
            mutator=None,
            proxy_token=None,
            agent_cmd=[],
            runtime=docker.Runtime.DOCKER,
            _command_override=["/bin/bash"],
            _interactive=True,
        )
        cmd = captured[0]
        assert "-it" in cmd
        assert "/bin/bash" in cmd

    def test_interactive_override_does_not_capture(self, monkeypatch):
        """_interactive=True should call subprocess.run without capture_output."""
        captured_kwargs: list[dict] = []

        def _mock_run(cmd, **kw):
            captured_kwargs.append(kw)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(docker.subprocess, "run", _mock_run)
        docker._run_container(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            hatchery_repo="/repo",
            name="test-task",
            mutator=None,
            proxy_token=None,
            agent_cmd=[],
            runtime=docker.Runtime.DOCKER,
            _command_override=["/bin/bash"],
            _interactive=True,
        )
        assert "capture_output" not in captured_kwargs[0]

    def test_interactive_override_returns_none(self, monkeypatch):
        """_interactive=True should return None (output not captured)."""
        monkeypatch.setattr(docker.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0))
        result = docker._run_container(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            hatchery_repo="/repo",
            name="test-task",
            mutator=None,
            proxy_token=None,
            agent_cmd=[],
            runtime=docker.Runtime.DOCKER,
            _command_override=["/bin/bash"],
            _interactive=True,
        )
        assert result is None

    def test_non_interactive_override_captures_output(self, monkeypatch):
        """Default _interactive=False + _command_override should capture output."""
        captured_kwargs: list[dict] = []

        def _mock_run(cmd, **kw):
            captured_kwargs.append(kw)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(docker.subprocess, "run", _mock_run)
        docker._run_container(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            hatchery_repo="/repo",
            name="test-task",
            mutator=None,
            proxy_token=None,
            agent_cmd=[],
            runtime=docker.Runtime.DOCKER,
            _command_override=["echo", "hello"],
        )
        assert captured_kwargs[0].get("capture_output") is True

    def test_non_interactive_override_no_it_flags(self, monkeypatch):
        """Default _interactive=False + _command_override should NOT add -it."""
        captured: list[list[str]] = []

        def _mock_run(cmd, **kw):
            captured.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(docker.subprocess, "run", _mock_run)
        docker._run_container(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            hatchery_repo="/repo",
            name="test-task",
            mutator=None,
            proxy_token=None,
            agent_cmd=[],
            runtime=docker.Runtime.DOCKER,
            _command_override=["echo", "hello"],
        )
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
    def test_plain_dir_gets_rw_mount(self, tmp_path):
        """A plain (non-git) directory is mounted rw at /includes/<basename>/."""
        plain = tmp_path / "shared-data"
        plain.mkdir()
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mounts = docker._docker_mounts_includes([plain], "my-task", session_dir, no_worktree=False)

        assert f"{plain}:/includes/shared-data:rw" in mounts

    def test_git_repo_without_worktree_gets_rw_mount(self, tmp_path):
        """A git repo with no worktree for the task falls back to a simple rw mount."""
        repo = tmp_path / "repo-b"
        repo.mkdir()
        (repo / ".git").mkdir()
        # No worktree created
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mounts = docker._docker_mounts_includes([repo], "my-task", session_dir, no_worktree=False)

        assert f"{repo}:/includes/repo-b:rw" in mounts
        # No git_ptr mount since worktree doesn't exist
        assert not any("git_ptr" in m for m in mounts)

    def test_git_repo_with_worktree_gets_layered_mounts(self, tmp_path):
        """A git repo with a task worktree gets layered mounts (root:ro, .git:rw, worktree:rw)."""
        import seekr_hatchery.tasks as tasks_mod

        repo = tmp_path / "repo-b"
        repo.mkdir()
        git_dir = repo / ".git"
        git_dir.mkdir()
        (git_dir / "objects").mkdir()
        worktree = repo / tasks_mod.WORKTREES_SUBDIR / "my-task"
        worktree.mkdir(parents=True)
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mounts = docker._docker_mounts_includes([repo], "my-task", session_dir, no_worktree=False)

        # Root is read-only
        assert f"{repo}:/includes/repo-b:ro" in mounts
        # .git sub-dirs are rw
        assert f"{git_dir}:/includes/repo-b/.git:rw" in mounts
        assert f"{git_dir / 'objects'}:/includes/repo-b/.git/objects:rw" in mounts
        # Worktree itself is rw
        container_wt = "/includes/repo-b/.hatchery/worktrees/my-task"
        assert f"{worktree}:{container_wt}:rw" in mounts
        # git pointer file is written and mounted
        git_ptr_file = session_dir / "git_ptr_include_repo-b"
        assert git_ptr_file.exists()
        assert "gitdir: /includes/repo-b/.git/worktrees/my-task" in git_ptr_file.read_text()
        assert f"{git_ptr_file}:{container_wt}/.git:rw" in mounts
        # No single full rw mount for root
        assert f"{repo}:/includes/repo-b:rw" not in mounts

    def test_basename_collision_gets_numeric_suffix(self, tmp_path):
        """Two paths sharing the same basename get distinct container paths."""
        a = tmp_path / "a" / "api"
        b = tmp_path / "b" / "api"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mounts = docker._docker_mounts_includes([a, b], "task", session_dir, no_worktree=False)

        assert f"{a}:/includes/api:rw" in mounts
        assert f"{b}:/includes/api-1:rw" in mounts

    def test_no_worktree_skips_layered_mounts(self, tmp_path):
        """In no-worktree mode, git repos get a simple rw mount (no layering, no git_ptr)."""
        repo = tmp_path / "repo-b"
        repo.mkdir()
        (repo / ".git").mkdir()
        import seekr_hatchery.tasks as tasks_mod
        worktree = repo / tasks_mod.WORKTREES_SUBDIR / "my-task"
        worktree.mkdir(parents=True)
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mounts = docker._docker_mounts_includes([repo], "my-task", session_dir, no_worktree=True)

        assert f"{repo}:/includes/repo-b:rw" in mounts
        # No git_ptr pointer file should be written or mounted in no-worktree mode
        git_ptr_file = session_dir / "git_ptr_include_repo-b"
        assert not git_ptr_file.exists()
        assert not any(str(git_ptr_file) in m for m in mounts)

    def test_empty_list_returns_empty(self, tmp_path):
        mounts = docker._docker_mounts_includes([], "task", tmp_path, no_worktree=False)
        assert mounts == []


# ---------------------------------------------------------------------------
# DockerConfig.include field
# ---------------------------------------------------------------------------


class TestDockerConfigInclude:
    def test_defaults_to_empty(self):
        config = docker.DockerConfig()
        assert config.include == []

    def test_parses_include_list(self):
        config = docker.DockerConfig(include=["../repo-b", "/abs/path"])
        assert config.include == ["../repo-b", "/abs/path"]

    def test_extra_fields_still_forbidden(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            docker.DockerConfig(unknown_field="oops")
