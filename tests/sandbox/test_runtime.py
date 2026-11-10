import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import seekr_hatchery.agents as agent
import seekr_hatchery.branches as branches
import seekr_hatchery.sandbox.config as sandbox_config
import seekr_hatchery.sandbox.mount_links as mount_links
import seekr_hatchery.sandbox.mount_types as mount
import seekr_hatchery.sandbox.mount_types as mount_types
import seekr_hatchery.sandbox.mounts as sandbox_mounts
import seekr_hatchery.sandbox.runtime as runtime
import seekr_hatchery.sandbox.runtime as sandbox_runtime
import seekr_hatchery.sandbox.spec as sandbox_spec
import seekr_hatchery.sandbox.spec as spec
import seekr_hatchery.utils as utils
from seekr_hatchery.models import SessionMeta
from seekr_hatchery.sandbox.mount_types import (
    BindMount,
    TmpfsMount,
    VolumeMount,
)


@pytest.mark.parametrize("interactive", [False, True])
def test_command_override_file_volume_execution(monkeypatch: pytest.MonkeyPatch, interactive: bool) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, "output", "")

    monkeypatch.setattr(runtime.subprocess, "run", run)
    container = spec.ContainerSpec(
        image="image",
        command=["unused"],
        workdir="/work",
        name="task",
        container_name=None,
        mounts=[mount_types.VolumeMount(name="auth", dst="/home/token", is_file=True)],
        env={"KEY": "value"},
        interactive=interactive,
        command_override=["cat", "argument with spaces"],
        capture_output=True,
    )
    result = runtime.DockerRuntime().run(container)
    argv = ["docker", "run", "--rm", "--init"]
    if interactive:
        argv += ["-it"]
    argv += [
        "-v",
        "auth:/home/token.vol:rw",
        "-e",
        "KEY=value",
        "-w",
        "/work",
        "image",
        "sh",
        "-c",
        "ln -sf /home/token.vol/token /home/token && exec cat 'argument with spaces'",
    ]
    assert calls == [
        (["docker", "volume", "inspect", "auth"], {"cwd": None, "check": False, "capture_output": True, "text": True}),
        (argv, {} if interactive else {"capture_output": True, "text": True}),
    ]
    assert (None if result is None else vars(result)) == (
        None if interactive else {"args": argv, "returncode": 0, "stdout": "output", "stderr": ""}
    )


# ---------------------------------------------------------------------------
# DockerRuntime.available()
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
        monkeypatch.setattr(sandbox_mounts, "_default_home_mounts", lambda: [])

        cfg = sandbox_config.SandboxConfig(follow_symlinks=False)
        mounts = sandbox_mounts.build_mounts(_no_wt_meta(cwd), self._make_backend(), tmp_path, cfg)

        target = external.resolve()
        assert mount.BindMount(src=str(target), dst=str(target), mode="RW") not in mounts

    def test_enabled_adds_symlink_mounts(self, tmp_path, monkeypatch):
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        (cwd / "link").symlink_to(external)
        monkeypatch.setattr(sandbox_mounts, "_default_home_mounts", lambda: [])

        cfg = sandbox_config.SandboxConfig(follow_symlinks=True)
        mounts = sandbox_mounts.build_mounts(_no_wt_meta(cwd), self._make_backend(), tmp_path, cfg)

        target = external.resolve()
        assert mount.BindMount(src=str(target), dst=str(target), mode="RW") in mounts

    def test_enabled_declares_the_flag_on_the_tree_mount(self, tmp_path, monkeypatch):
        """The config field is per-mount state, not a separate scan pass —
        which is what lets one expansion cover the worktree, the docker.yaml
        mounts and the agent config dirs alike."""
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.setattr(sandbox_mounts, "_default_home_mounts", lambda: [])
        monkeypatch.setattr(mount_links, "expand_link_mounts", lambda mounts: mounts)

        cfg = sandbox_config.SandboxConfig(follow_symlinks=True)
        mounts = sandbox_mounts.build_mounts(_no_wt_meta(cwd), self._make_backend(), tmp_path, cfg)

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
        monkeypatch.setattr(sandbox_mounts, "_default_home_mounts", lambda: [])

        backend = MagicMock()
        backend.construct_mounts = MagicMock(
            return_value=[
                mount.VolumeMount(name="claude-dir", dst="/home/hatchery/.claude"),
                mount.TmpfsMount(dst="/tmp/scratch"),
            ]
        )
        cfg = sandbox_config.SandboxConfig(follow_symlinks=True)
        mounts = sandbox_mounts.build_mounts(_no_wt_meta(cwd), backend, tmp_path, cfg)

        target = external.resolve()
        assert mount.BindMount(src=str(target), dst=str(target), mode="RW") in mounts


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


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
        monkeypatch.setattr(utils, "run", fake_run)

        mounts = [mount.BindMount(src="/host/x", dst="/cont/x", mode="RW")]
        _run_with_mounts(sandbox_runtime.DockerRuntime(), mounts, monkeypatch)

        assert calls == []

    def test_creates_when_inspect_fails(self, monkeypatch):
        calls, fake_run = self._record_run({("volume", "inspect", "hatchery-uv"): 1})
        monkeypatch.setattr(utils, "run", fake_run)

        mounts = [mount.VolumeMount(name="hatchery-uv", dst="/cache", mode="RW", task_scoped=False)]
        _run_with_mounts(sandbox_runtime.DockerRuntime(), mounts, monkeypatch)

        assert calls == [
            ["docker", "volume", "inspect", "hatchery-uv"],
            ["docker", "volume", "create", "hatchery-uv"],
        ]

    def test_skips_create_when_inspect_succeeds(self, monkeypatch):
        calls, fake_run = self._record_run({("volume", "inspect", "hatchery-uv"): 0})
        monkeypatch.setattr(utils, "run", fake_run)

        mounts = [mount.VolumeMount(name="hatchery-uv", dst="/cache", mode="RW", task_scoped=False)]
        _run_with_mounts(sandbox_runtime.PodmanRuntime(), mounts, monkeypatch)

        assert calls == [["podman", "volume", "inspect", "hatchery-uv"]]

    def test_dedupes_repeated_names(self, monkeypatch):
        calls, fake_run = self._record_run({("volume", "inspect", "hatchery-uv"): 0})
        monkeypatch.setattr(utils, "run", fake_run)

        mounts = [
            mount.VolumeMount(name="hatchery-uv", dst="/cache/a", mode="RW", task_scoped=False),
            mount.VolumeMount(name="hatchery-uv", dst="/cache/b", mode="RW", task_scoped=False),
        ]
        _run_with_mounts(sandbox_runtime.DockerRuntime(), mounts, monkeypatch)

        assert calls == [["docker", "volume", "inspect", "hatchery-uv"]]


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestOomHint:
    def test_podman_137_returns_hint(self):
        hint = sandbox_runtime.PodmanRuntime().oom_hint(137)
        assert hint is not None
        assert "memory" in hint

    def test_podman_0_returns_none(self):
        assert sandbox_runtime.PodmanRuntime().oom_hint(0) is None

    def test_docker_137_returns_none(self):
        assert sandbox_runtime.DockerRuntime().oom_hint(137) is None


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestGoldenSpecAndArgv:
    """Full-output golden tests for build_spec + render_run_argv.

    These lock in the exact ContainerSpec fields and rendered argv so that
    argv-order or field-name changes are caught immediately.
    """

    def test_spec_non_dind_with_proxy(self, monkeypatch):
        """build_spec for a standard agent launch with proxy (non-DinD)."""
        monkeypatch.setattr(sandbox_runtime.sys, "platform", "linux")
        spec = sandbox_spec.build_spec(
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
        assert spec == sandbox_spec.ContainerSpec(
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
        spec = sandbox_spec.build_spec(
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
        assert spec.security_opt == ["label=disable", f"seccomp={sandbox_spec._SECCOMP}"]
        assert "NET_BIND_SERVICE" in spec.cap_add
        assert "SYS_ADMIN" in spec.cap_add
        assert spec.add_hosts == []

    def test_user_env_renders_and_yields_to_extra_env(self):
        """user_env lands after HATCHERY_* but extra_env (sidecar) wins on conflict."""
        spec = sandbox_spec.build_spec(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            name="test-task",
            hatchery_repo="/repo",
            container_name=None,
            agent_cmd=["codex"],
            user_env={"MY_FLAG": "1", "OPENAI_API_KEY": "user-clobber"},
            extra_env={"OPENAI_API_KEY": "sidecar-wins"},
        )
        assert spec.env == {
            "HATCHERY_TASK": "test-task",
            "HATCHERY_REPO": "/repo",
            "MY_FLAG": "1",
            "OPENAI_API_KEY": "sidecar-wins",
        }
        assert "-e" in sandbox_runtime.DockerRuntime().render_run_argv(spec)
        assert "MY_FLAG=1" in sandbox_runtime.DockerRuntime().render_run_argv(spec)

    def test_docker_argv_non_dind(self, monkeypatch):
        """Full rendered argv for Docker, non-DinD, with proxy on Linux."""
        monkeypatch.setattr(sandbox_runtime.sys, "platform", "linux")
        spec = sandbox_spec.build_spec(
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
        assert sandbox_runtime.DockerRuntime().render_run_argv(spec) == [
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
        monkeypatch.setattr(sandbox_runtime.sys, "platform", "linux")
        spec = sandbox_spec.build_spec(
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
        assert sandbox_runtime.PodmanRuntime().render_run_argv(spec) == [
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
        spec = sandbox_spec.build_spec(
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
        assert sandbox_runtime.DockerRuntime().render_run_argv(spec) == [
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
            f"seccomp={sandbox_spec._SECCOMP}",
            "--device",
            "/dev/fuse",
            "test-image",
        ]


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestRunInteractive:
    """Verify runtime.run with command_override + interactive flags."""

    def test_interactive_override_adds_it_flags(self, monkeypatch):
        """interactive=True + command_override should add -it to the command."""
        captured: list[list[str]] = []

        def _mock_run(cmd, **kw):
            captured.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(sandbox_runtime.subprocess, "run", _mock_run)
        runtime = sandbox_runtime.DockerRuntime()
        monkeypatch.setattr(runtime, "_ensure_volumes", lambda _mounts: None)
        spec = sandbox_spec.build_spec(
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

        monkeypatch.setattr(sandbox_runtime.subprocess, "run", _mock_run)
        runtime = sandbox_runtime.DockerRuntime()
        monkeypatch.setattr(runtime, "_ensure_volumes", lambda _mounts: None)
        spec = sandbox_spec.build_spec(
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
        monkeypatch.setattr(sandbox_runtime.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0))
        runtime = sandbox_runtime.DockerRuntime()
        monkeypatch.setattr(runtime, "_ensure_volumes", lambda _mounts: None)
        spec = sandbox_spec.build_spec(
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

        monkeypatch.setattr(sandbox_runtime.subprocess, "run", _mock_run)
        runtime = sandbox_runtime.DockerRuntime()
        monkeypatch.setattr(runtime, "_ensure_volumes", lambda _mounts: None)
        spec = sandbox_spec.build_spec(
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

        monkeypatch.setattr(sandbox_runtime.subprocess, "run", _mock_run)
        runtime = sandbox_runtime.DockerRuntime()
        monkeypatch.setattr(runtime, "_ensure_volumes", lambda _mounts: None)
        spec = sandbox_spec.build_spec(
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
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestRenderRunArgv:
    """Verify render_run_argv injects correct flags for each runtime."""

    def _build_and_render(
        self,
        monkeypatch,
        runtime: sandbox_runtime.ContainerRuntime | None = None,
        mutator=None,
        proxy_token: str = "proxy-uuid-token",
        proxy_port: int = 9999,
        **spec_kwargs,
    ) -> list[str]:
        if runtime is None:
            runtime = sandbox_runtime.DockerRuntime()
        spec = sandbox_spec.build_spec(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            name="test-task",
            hatchery_repo="/repo",
            container_name=None,
            agent_cmd=["codex"],
            extra_env=agent.CODEX.container_env(
                agent.ProxyEndpoint(key="default", header_mutator=lambda h: h, target_host="x"),
                proxy_token,
                proxy_port,
            ),
            needs_host_gateway=True,
            **spec_kwargs,
        )
        return runtime.render_run_argv(spec)

    # --- runtime binary ---

    def test_docker_runtime_uses_docker_binary(self, monkeypatch):
        cmd = self._build_and_render(monkeypatch, runtime=sandbox_runtime.DockerRuntime())
        assert cmd[0] == "docker"

    def test_podman_runtime_uses_podman_binary(self, monkeypatch):
        cmd = self._build_and_render(monkeypatch, runtime=sandbox_runtime.PodmanRuntime())
        assert cmd[0] == "podman"

    # --- --init (PID 1 zombie reaping) ---

    def test_docker_runtime_adds_init(self, monkeypatch):
        cmd = self._build_and_render(monkeypatch, runtime=sandbox_runtime.DockerRuntime())
        assert "--init" in cmd

    def test_podman_runtime_adds_init(self, monkeypatch):
        cmd = self._build_and_render(monkeypatch, runtime=sandbox_runtime.PodmanRuntime())
        assert "--init" in cmd

    # --- Podman outer-container flags ---

    def test_podman_userns_keep_id_on_linux(self, monkeypatch):
        monkeypatch.setattr(sandbox_runtime.sys, "platform", "linux")
        cmd = self._build_and_render(monkeypatch, runtime=sandbox_runtime.PodmanRuntime())
        assert "--userns=keep-id" in cmd

    def test_podman_no_userns_keep_id_on_macos(self, monkeypatch):
        monkeypatch.setattr(sandbox_runtime.sys, "platform", "darwin")
        cmd = self._build_and_render(monkeypatch, runtime=sandbox_runtime.PodmanRuntime())
        assert "--userns=keep-id" not in cmd

    def test_podman_runtime_adds_label_disable(self, monkeypatch):
        cmd = self._build_and_render(monkeypatch, runtime=sandbox_runtime.PodmanRuntime())
        assert "label=disable" in " ".join(cmd)

    def test_docker_runtime_no_userns(self, monkeypatch):
        cmd = self._build_and_render(monkeypatch, runtime=sandbox_runtime.DockerRuntime())
        assert "--userns=keep-id" not in cmd

    def test_docker_runtime_no_label_disable(self, monkeypatch):
        cmd = self._build_and_render(monkeypatch, runtime=sandbox_runtime.DockerRuntime())
        assert "label=disable" not in " ".join(cmd)

    # --- Security regression guards ---

    def test_podman_no_privileged(self, monkeypatch):
        cmd = self._build_and_render(monkeypatch, runtime=sandbox_runtime.PodmanRuntime())
        assert "--privileged" not in cmd

    def test_podman_no_seccomp_unconfined(self, monkeypatch):
        cmd = self._build_and_render(monkeypatch, runtime=sandbox_runtime.PodmanRuntime())
        assert "seccomp=unconfined" not in " ".join(cmd)

    def test_docker_no_privileged(self, monkeypatch):
        cmd = self._build_and_render(monkeypatch, runtime=sandbox_runtime.DockerRuntime())
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
        monkeypatch.setattr(sandbox_runtime.sys, "platform", "linux")
        cmd = self._build_and_render(monkeypatch)
        assert "--add-host=host.docker.internal:host-gateway" in cmd

    def test_no_add_host_flag_on_macos(self, monkeypatch):
        """On macOS, Docker Desktop exposes host.docker.internal natively."""
        monkeypatch.setattr(sandbox_runtime.sys, "platform", "darwin")
        cmd = self._build_and_render(monkeypatch)
        assert "--add-host=host.docker.internal:host-gateway" not in cmd

    def test_proxy_token_always_set(self, monkeypatch):
        """The container API key env var must always be set to the stable proxy token."""
        cmd = self._build_and_render(monkeypatch, proxy_token="stable-token")
        cmd_str = " ".join(cmd)
        assert "OPENAI_API_KEY=stable-token" in cmd_str

    def test_no_api_key_env_when_mutator_is_none(self, monkeypatch):
        """When mutator is None, no API key or base URL env vars should appear."""
        spec = sandbox_spec.build_spec(
            image="test-image",
            mounts=[],
            workdir="/workspace",
            name="test-task",
            hatchery_repo="/repo",
            container_name=None,
            agent_cmd=["codex"],
        )
        cmd = sandbox_runtime.DockerRuntime().render_run_argv(spec)
        cmd_str = " ".join(cmd)
        assert "OPENAI_API_KEY" not in cmd_str
        assert "OPENAI_BASE_URL" not in cmd_str


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestDetectRuntime:
    def test_returns_podman_when_podman_available(self, monkeypatch):
        monkeypatch.setattr(sandbox_runtime.PodmanRuntime, "available", staticmethod(lambda: True))
        monkeypatch.setattr(sandbox_runtime.DockerRuntime, "available", staticmethod(lambda: True))
        result = sandbox_runtime.detect_runtime()
        assert isinstance(result, sandbox_runtime.PodmanRuntime)

    def test_prefers_podman_over_docker(self, monkeypatch):
        monkeypatch.setattr(sandbox_runtime.PodmanRuntime, "available", staticmethod(lambda: True))
        monkeypatch.setattr(sandbox_runtime.DockerRuntime, "available", staticmethod(lambda: False))
        result = sandbox_runtime.detect_runtime()
        assert isinstance(result, sandbox_runtime.PodmanRuntime)

    def test_falls_back_to_docker_when_podman_not_installed(self, monkeypatch):
        monkeypatch.setattr(sandbox_runtime.PodmanRuntime, "available", staticmethod(lambda: False))
        monkeypatch.setattr(sandbox_runtime.shutil, "which", lambda _: None)
        monkeypatch.setattr(sandbox_runtime.DockerRuntime, "available", staticmethod(lambda: True))
        result = sandbox_runtime.detect_runtime()
        assert isinstance(result, sandbox_runtime.DockerRuntime)

    def test_exits_when_neither_available(self, monkeypatch):
        monkeypatch.setattr(sandbox_runtime.PodmanRuntime, "available", staticmethod(lambda: False))
        monkeypatch.setattr(sandbox_runtime.shutil, "which", lambda _: None)
        monkeypatch.setattr(sandbox_runtime.DockerRuntime, "available", staticmethod(lambda: False))
        with pytest.raises(SystemExit) as exc_info:
            sandbox_runtime.detect_runtime()
        assert exc_info.value.code == 1

    def test_exits_when_podman_installed_but_not_running(self, monkeypatch):
        monkeypatch.setattr(sandbox_runtime.PodmanRuntime, "available", staticmethod(lambda: False))
        monkeypatch.setattr(sandbox_runtime.shutil, "which", lambda _: "/usr/local/bin/podman")
        with pytest.raises(SystemExit) as exc_info:
            sandbox_runtime.detect_runtime()
        assert exc_info.value.code == 1

    def test_installed_not_running_error_message(self, monkeypatch, capsys):
        monkeypatch.setattr(sandbox_runtime.PodmanRuntime, "available", staticmethod(lambda: False))
        monkeypatch.setattr(sandbox_runtime.shutil, "which", lambda _: "/usr/local/bin/podman")
        with pytest.raises(SystemExit):
            sandbox_runtime.detect_runtime()
        assert "not running" in capsys.readouterr().err

    def test_installed_not_running_macos_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(sandbox_runtime.PodmanRuntime, "available", staticmethod(lambda: False))
        monkeypatch.setattr(sandbox_runtime.shutil, "which", lambda _: "/usr/local/bin/podman")
        monkeypatch.setattr(sandbox_runtime.sys, "platform", "darwin")
        with pytest.raises(SystemExit):
            sandbox_runtime.detect_runtime()
        err = capsys.readouterr().err
        assert "podman machine start" in err
        assert "podman machine init" in err

    def test_neither_available_error_message(self, monkeypatch, capsys):
        monkeypatch.setattr(sandbox_runtime.PodmanRuntime, "available", staticmethod(lambda: False))
        monkeypatch.setattr(sandbox_runtime.shutil, "which", lambda _: None)
        monkeypatch.setattr(sandbox_runtime.DockerRuntime, "available", staticmethod(lambda: False))
        with pytest.raises(SystemExit):
            sandbox_runtime.detect_runtime()
        err = capsys.readouterr().err
        assert "Podman" in err or "Docker" in err


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestPodmanAvailable:
    def test_returns_true_when_rc_zero(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 0
        monkeypatch.setattr(utils, "run", lambda *a, **kw: mock_result)
        assert sandbox_runtime.PodmanRuntime.available() is True

    def test_returns_false_when_rc_nonzero(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 1
        monkeypatch.setattr(utils, "run", lambda *a, **kw: mock_result)
        assert sandbox_runtime.PodmanRuntime.available() is False

    def test_returns_false_when_binary_not_found(self, monkeypatch):
        def _raise(*a, **kw):
            raise FileNotFoundError("No such file or directory: 'podman'")

        monkeypatch.setattr(utils, "run", _raise)
        assert sandbox_runtime.PodmanRuntime.available() is False


def _task_branch(name: str = "my-task", prefix: str = branches.DEFAULT_BRANCH_PREFIX) -> branches.BranchName:
    return branches.BranchPrefix(prefix).task_branch(name)


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
        monkeypatch.setattr(utils, "run", lambda *a, **kw: mock_result)
        assert sandbox_runtime.DockerRuntime.available() is True

    def test_returns_false_when_rc_nonzero(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 1
        monkeypatch.setattr(utils, "run", lambda *a, **kw: mock_result)
        assert sandbox_runtime.DockerRuntime.available() is False

    def test_returns_false_when_binary_not_found(self, monkeypatch):
        def _raise(*a, **kw):
            raise FileNotFoundError("No such file or directory: 'docker'")

        monkeypatch.setattr(utils, "run", _raise)
        assert sandbox_runtime.DockerRuntime.available() is False


# ── Construction ──────────────────────────────────────────────────────────────


class TestWrapCmdForFileMounts:
    def test_passthrough_when_no_file_mounts(self):
        """No file-shaped mounts → cmd is returned unchanged. No sh -c wrap,
        no symlink setup, no shell evaluation surface added to the command."""
        mounts = [
            BindMount(src=Path("/host"), dst="/cont"),
            VolumeMount(name="vol-state", dst="/home/h/.state"),
        ]
        cmd = ["claude", "--resume=abc", "go fix the bug"]
        assert wrap_cmd_for_file_mounts(cmd, mounts) == cmd

    def test_wraps_with_sh_when_file_mount_present(self):
        """File-shaped mount present → cmd is wrapped in ``sh -c '<ln> && exec <cmd>'``
        so the symlink is in place before the agent process is exec'd. The
        original cmd is shlex-joined so args with spaces/quotes round-trip."""
        mounts = [VolumeMount(name="vol-cfg", dst="/home/h/.cfg.json", is_file=True)]
        cmd = ["claude", "--prompt", "hello world"]
        out = wrap_cmd_for_file_mounts(cmd, mounts)
        assert out == [
            "sh",
            "-c",
            "ln -sf /home/h/.cfg.json.vol/.cfg.json /home/h/.cfg.json && exec claude --prompt 'hello world'",
        ]

    def test_multiple_file_mounts_chain_with_and(self):
        mounts = [
            VolumeMount(name="vol-a", dst="/home/h/.a.json", is_file=True),
            VolumeMount(name="vol-b", dst="/home/h/.b.json", is_file=True),
        ]
        out = wrap_cmd_for_file_mounts(["agent"], mounts)
        assert out == [
            "sh",
            "-c",
            "ln -sf /home/h/.a.json.vol/.a.json /home/h/.a.json"
            " && ln -sf /home/h/.b.json.vol/.b.json /home/h/.b.json"
            " && exec agent",
        ]


# ── Construction ──────────────────────────────────────────────────────────────


class TestFileMountPrestart:
    def test_empty_when_no_file_mounts(self):
        mounts = [
            BindMount(src=Path("/host"), dst="/cont"),
            VolumeMount(name="vol-state", dst="/home/h/.state"),
            TmpfsMount(dst="/tmp"),
        ]
        assert file_mount_prestart_cmds(mounts) == []

    def test_single_file_mount(self):
        mounts = [VolumeMount(name="vol-cfg", dst="/home/h/.cfg.json", is_file=True)]
        cmds = file_mount_prestart_cmds(mounts)
        assert cmds == ["ln -sf /home/h/.cfg.json.vol/.cfg.json /home/h/.cfg.json"]

    def test_multiple_file_mounts(self):
        mounts = [
            VolumeMount(name="vol-a", dst="/home/h/.a.json", is_file=True),
            VolumeMount(name="vol-b", dst="/home/h/.b.json", is_file=True),
        ]
        cmds = file_mount_prestart_cmds(mounts)
        assert cmds == [
            "ln -sf /home/h/.a.json.vol/.a.json /home/h/.a.json",
            "ln -sf /home/h/.b.json.vol/.b.json /home/h/.b.json",
        ]

    def test_mixed_mounts(self):
        mounts = [
            BindMount(src=Path("/host"), dst="/cont"),
            VolumeMount(name="vol-cfg", dst="/home/h/.cfg.json", is_file=True),
            VolumeMount(name="vol-state", dst="/home/h/.state"),
        ]
        cmds = file_mount_prestart_cmds(mounts)
        assert cmds == ["ln -sf /home/h/.cfg.json.vol/.cfg.json /home/h/.cfg.json"]


# ── Construction ──────────────────────────────────────────────────────────────


class TestMountToDockerArgs:
    def test_bind_rw(self):
        m = BindMount(src=Path("/host/a"), dst="/cont/b", mode="RW")
        assert mount_to_docker_args(m) == ["-v", "/host/a:/cont/b:rw"]

    def test_bind_ro(self):
        m = BindMount(src=Path("/host/a"), dst="/cont/b", mode="RO")
        assert mount_to_docker_args(m) == ["-v", "/host/a:/cont/b:ro"]

    def test_tmpfs(self):
        m = TmpfsMount(dst="/cont/tmp")
        assert mount_to_docker_args(m) == ["--tmpfs", "/cont/tmp"]

    def test_volume_dir_shape(self):
        # Directory-shaped: plain -v with the runtime volume name.
        m = VolumeMount(name="hatchery-x-vol-state", dst="/home/h/.state", mode="RW")
        assert mount_to_docker_args(m) == ["-v", "hatchery-x-vol-state:/home/h/.state:rw"]

    def test_volume_dir_shape_ro(self):
        m = VolumeMount(name="vol-a", dst="/d", mode="RO")
        assert mount_to_docker_args(m) == ["-v", "vol-a:/d:ro"]

    def test_volume_file_shape_uses_sidecar(self):
        """File-shaped volume mounts use a sidecar directory (dst + ".vol") so
        the volume (which the kernel always presents as a directory) doesn't
        shadow the parent directory.  A symlink from dst into the sidecar is
        created at container startup via file_mount_prestart_cmds."""
        m = VolumeMount(
            name="hatchery-x-vol-cfg",
            dst="/home/h/.cfg.json",
            is_file=True,
            mode="RW",
        )
        assert mount_to_docker_args(m) == ["-v", "hatchery-x-vol-cfg:/home/h/.cfg.json.vol:rw"]

    def test_volume_file_shape_ro(self):
        m = VolumeMount(name="v", dst="/home/h/.cfg.json", is_file=True, mode="RO")
        assert mount_to_docker_args(m) == ["-v", "v:/home/h/.cfg.json.vol:ro"]


# ---------------------------------------------------------------------------
# find_task_file
# ---------------------------------------------------------------------------


class TestRunContainerName:
    """Verify build_spec + render_run_argv passes --name when container_name is provided."""

    _COMMON = dict(
        image="test-image:task",
        mounts=[],
        workdir="/repo/.hatchery/worktrees/task",
        hatchery_repo="/repo",
        name="my-task",
        agent_cmd=["codex"],
    )

    def _run(self, **kwargs) -> list[str]:
        args = {**self._COMMON, **kwargs}
        container_name = args.pop("container_name", None)
        spec = sandbox_spec.build_spec(**args, container_name=container_name)
        return sandbox_runtime.DockerRuntime().render_run_argv(spec)

    def test_name_injected_when_provided(self):
        cmd = self._run(container_name="hatchery-myrepo-my-task")
        name_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--name"]
        assert name_values == ["hatchery-myrepo-my-task"]

    def test_name_absent_when_not_provided(self):
        cmd = self._run()
        assert "--name" not in cmd


# ---------------------------------------------------------------------------
# find_task_file
# ---------------------------------------------------------------------------


class TestRunContainerDindFlags:
    """Verify build_spec + render_run_argv injects the correct flags depending on dind=."""

    _COMMON = dict(
        image="test-image:task",
        mounts=[],
        workdir="/repo/.hatchery/worktrees/task",
        hatchery_repo="/repo",
        name="task",
        agent_cmd=["codex"],
    )

    def _run(self, **kwargs) -> list[str]:
        """Build spec and render argv (no subprocess call needed)."""
        args = {**self._COMMON, **kwargs}
        spec = sandbox_spec.build_spec(**args, container_name=None)
        return sandbox_runtime.DockerRuntime().render_run_argv(spec)

    def test_dind_false_no_extra_flags(self):
        cmd = self._run(dind=False)
        assert "--cap-drop" not in cmd
        assert "--device" not in cmd
        assert "--security-opt" not in cmd

    def test_dind_true_all_flags(self):
        cmd = self._run(dind=True)
        # --cap-drop ALL
        cap_drop_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--cap-drop"]
        assert cap_drop_values == ["ALL"]
        # exact set of capabilities re-added
        cap_add_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--cap-add"]
        assert set(cap_add_values) == {
            "SYS_ADMIN",
            "MKNOD",
            "SETUID",
            "SETGID",
            "CHOWN",
            "DAC_OVERRIDE",
            "FOWNER",
            "SETFCAP",
            "SYS_CHROOT",
            "SETPCAP",
            "AUDIT_WRITE",
            "FSETID",
            "KILL",
            "NET_BIND_SERVICE",
            "NET_ADMIN",
            "NET_RAW",
        }
        # /dev/fuse device
        device_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--device"]
        assert device_values == ["/dev/fuse"]
        # security-opt: label=disable + seccomp profile
        security_opt_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--security-opt"]
        assert "label=disable" in security_opt_values
        assert any(v.startswith("seccomp=") and "seccomp.json" in v for v in security_opt_values)


# ---------------------------------------------------------------------------
# find_task_file
# ---------------------------------------------------------------------------


class TestContainerRuntime:
    def test_podman_binary(self):
        assert sandbox_runtime.PodmanRuntime().binary == "podman"

    def test_docker_binary(self):
        assert sandbox_runtime.DockerRuntime().binary == "docker"


def _make_mutator(key: str = "real-secret-key"):
    """Return a simple header mutator for tests."""

    def _mutate(headers):
        out = {k: v for k, v in headers.items() if k.lower() not in ("x-api-key", "authorization")}
        out["Authorization"] = f"Bearer {key}"
        return out

    return _mutate


def _render_command(cmd: list[str], mounts: list[mount.Mount]) -> list[str]:
    container_spec = sandbox_spec.build_spec(
        image="contract-image",
        mounts=mounts,
        workdir="/work",
        name="task",
        hatchery_repo="/work",
        container_name=None,
        agent_cmd=cmd,
        command_override=cmd,
    )
    from unittest.mock import patch

    captured: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    with patch.object(utils, "run", run), patch.object(subprocess, "run", run):
        sandbox_runtime.DockerRuntime().run(container_spec)
    argv = captured[-1]
    return argv[argv.index("contract-image") + 1 :]


def wrap_cmd_for_file_mounts(cmd: list[str], mounts: list[mount.Mount]) -> list[str]:
    return _render_command(cmd, mounts)


def file_mount_prestart_cmds(mounts: list[mount.Mount]) -> list[str]:
    command = _render_command(["agent"], mounts)
    return command[-1].split(" && ")[:-1] if command[:2] == ["sh", "-c"] else []


def mount_to_docker_args(m: mount.Mount) -> list[str]:
    container_spec = sandbox_spec.build_spec(
        image="contract-image",
        mounts=[m],
        workdir="/work",
        name="task",
        hatchery_repo="/work",
        container_name=None,
        agent_cmd=["agent"],
    )
    argv = sandbox_runtime.DockerRuntime().render_run_argv(container_spec)
    start = argv.index("--tmpfs" if isinstance(m, mount.TmpfsMount) else "-v")
    return argv[start : start + 2]


def _run_with_mounts(
    runtime: sandbox_runtime.ContainerRuntime, mounts: list[mount.Mount], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox_runtime.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0))
    container_spec = sandbox_spec.build_spec(
        image="contract-image",
        mounts=mounts,
        workdir="/work",
        name="task",
        hatchery_repo="/work",
        container_name=None,
        agent_cmd=["true"],
        command_override=["true"],
    )
    runtime.run(container_spec)


def test_follow_links_does_not_change_rendered_bind_args() -> None:
    m = BindMount(src=Path("/h"), dst="/c", follow_links=True)
    assert mount_to_docker_args(m) == ["-v", "/h:/c:rw"]
