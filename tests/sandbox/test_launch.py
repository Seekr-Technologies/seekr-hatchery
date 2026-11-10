import contextlib
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

import seekr_hatchery.agents as agents
import seekr_hatchery.branches as branches
import seekr_hatchery.constants as constants
import seekr_hatchery.models as models
import seekr_hatchery.sandbox.config as config
import seekr_hatchery.sandbox.images as images
import seekr_hatchery.sandbox.launch as launch
import seekr_hatchery.sandbox.launch as sandbox_launch
import seekr_hatchery.sandbox.mount_types as mount_types
import seekr_hatchery.sandbox.mounts as mounts
import seekr_hatchery.sandbox.mounts as sandbox_mounts
import seekr_hatchery.sandbox.runtime as runtime
import seekr_hatchery.sandbox.runtime as sandbox_runtime
import seekr_hatchery.sandbox.scaffold as scaffold
import seekr_hatchery.sandbox.spec as spec
import seekr_hatchery.sandbox.volumes as volumes
import seekr_hatchery.sessions as sessions
import seekr_hatchery.sidecars as sidecars
import seekr_hatchery.utils as utils
from seekr_hatchery.models import SessionMeta


@pytest.mark.parametrize("adapter", [runtime.DockerRuntime, runtime.PodmanRuntime])
def test_resolve_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter: type[runtime.ContainerRuntime]
) -> None:
    scaffold.dockerfile_path(tmp_path, agents.CODEX).write_text("FROM debian\n")
    selected = adapter()
    monkeypatch.setattr(runtime, "detect_runtime", lambda: selected)
    assert launch.resolve_runtime(tmp_path, False) is selected
    assert launch.resolve_runtime(tmp_path, True) is None


def test_resolve_runtime_requires_dockerfile(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert launch.resolve_runtime(tmp_path, True) is None
    with pytest.raises(SystemExit) as exc:
        launch.resolve_runtime(tmp_path, False)
    assert exc.value.code == 1
    assert "No Dockerfile found for 'codex'" in capsys.readouterr().err


def test_launch_context(tmp_path: Path) -> None:
    meta = models.SessionMeta(name="task", repo=str(tmp_path), worktree=str(tmp_path / "worktree"))
    assert launch.launch_context(meta, None) == (None, [], "")
    assert launch.launch_context(meta, runtime.DockerRuntime()) == (config.SandboxConfig(), [], str(meta.worktree_path))


@pytest.mark.parametrize("no_worktree,no_commit", [(False, False), (True, False), (False, True)])
@pytest.mark.parametrize("fail", [False, True])
def test_run_session_order_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spy_backend: agents.AgentBackend,
    no_worktree: bool,
    no_commit: bool,
    fail: bool,
) -> None:
    monkeypatch.setattr(constants, "HATCHERY_DIR", tmp_path / "state")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "ORIG_HEAD").write_text("original")
    meta = models.SessionMeta(
        name="task",
        repo=str(repo),
        worktree=str(repo if no_worktree else tmp_path / "wt"),
        no_worktree=no_worktree,
        no_commit=no_commit,
    )
    events: list[str] = []
    captured: list[spec.ContainerSpec] = []

    def build_image(*args: object, **kwargs: object) -> None:
        events.append("image")
        assert ("on_before_container_start", meta.session_dir, "token", str(meta.worktree_path)) in spy_backend.calls
        assert args[:3] == (repo, meta.hatchery_dir, meta.image_name)
        assert kwargs["no_cache"] is False

    def build_mounts(*args: object, **kwargs: object) -> list[mount_types.Mount]:
        events.append("mounts")
        assert kwargs["git_sentinel_files"] == (
            None if no_worktree else [(meta.session_dir / "ORIG_HEAD", "ORIG_HEAD")]
        )
        return []

    def prepare(*args: object) -> list[mount_types.Mount]:
        events.append("volumes")
        assert args[2:] == (meta, meta.session_dir, "token", str(meta.worktree_path))
        return []

    @contextlib.contextmanager
    def run_sidecars(active: list[sidecars.SandboxSidecar]) -> Iterator[sidecars.SidecarContribution]:
        events.append("sidecars")
        try:
            yield sidecars.SidecarContribution(env={"SHARED": "sidecar"})
        finally:
            events.append("cleanup")

    class RecordingRuntime(runtime.DockerRuntime):
        def run(self, container_spec: spec.ContainerSpec, **kwargs: object) -> None:
            events.append("run")
            captured.append(container_spec)
            assert kwargs["paste_interceptor"] is not None
            if fail:
                raise RuntimeError("launch failed")

    monkeypatch.setattr(images, "build_image", build_image)
    monkeypatch.setattr(mounts, "build_mounts", build_mounts)
    monkeypatch.setattr(volumes, "prepare_volume_mounts", prepare)
    monkeypatch.setattr(sidecars, "run_sidecars", run_sidecars)
    settings = config.SandboxConfig(environment=["SHARED=user"])
    with pytest.raises(RuntimeError, match="launch failed") if fail else contextlib.nullcontext():
        launch.run_session(meta, spy_backend, ["agent"], settings, proxy_token="token", runtime=RecordingRuntime())
    assert events == ["image", "mounts", "volumes", "sidecars", "run", "cleanup"]
    assert captured == [
        spec.build_spec(
            image=meta.image_name,
            mounts=[],
            workdir=str(meta.worktree_path),
            name="task",
            hatchery_repo=str(meta.worktree_path if no_worktree else repo),
            container_name=meta.container_name,
            agent_cmd=["agent"],
            user_env={"SHARED": "user"},
            extra_env={"SHARED": "sidecar"},
        )
    ]
    assert (repo / ".git" / "ORIG_HEAD").read_text() == "original"


@pytest.mark.parametrize("failure", ["none", "build", "run"])
def test_shell_cleans_session_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spy_backend: agents.AgentBackend,
    failure: str,
) -> None:
    monkeypatch.setattr(constants, "HATCHERY_DIR", tmp_path / "state")
    calls: list[tuple[object, ...]] = []

    def build_image(*args: object, **kwargs: object) -> None:
        calls.append(args[:3])
        if failure == "build":
            raise RuntimeError("build failed")

    class RecordingRuntime(runtime.DockerRuntime):
        def run(self, container_spec: spec.ContainerSpec, **kwargs: object) -> None:
            calls.append((container_spec.command_override, container_spec.interactive))
            if failure == "run":
                raise RuntimeError("run failed")

    monkeypatch.setattr(images, "build_image", build_image)
    with pytest.raises(RuntimeError, match=f"{failure} failed") if failure != "none" else contextlib.nullcontext():
        launch.launch_shell(
            tmp_path,
            spy_backend,
            config.SandboxConfig(),
            RecordingRuntime(),
            "existing-image",
            hatchery_dir=tmp_path / "store",
            shell="/bin/sh",
        )
    assert calls == (
        [(tmp_path, tmp_path / "store", "existing-image")]
        if failure == "build"
        else [(tmp_path, tmp_path / "store", "existing-image"), (["/bin/sh"], True)]
    )
    assert list((tmp_path / "state" / "sandbox-sessions").iterdir()) == []


def test_resolve_runtime_exits_when_no_engine_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scaffold.dockerfile_path(tmp_path, agents.CODEX).write_text("FROM debian\n")

    def unavailable(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("engine not installed")

    monkeypatch.setattr(utils, "run", unavailable)
    monkeypatch.setattr(runtime.shutil, "which", lambda _: None)
    with pytest.raises(SystemExit) as exc:
        launch.resolve_runtime(tmp_path, False)
    assert exc.value.code == 1
    assert "neither Podman nor Docker" in capsys.readouterr().err


def test_remove_clipboard_dir(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    clipboard = mounts.clipboard_image_dir(session_dir)
    clipboard.mkdir(parents=True)
    (clipboard / "paste.png").write_bytes(b"image")
    launch.remove_clipboard_dir(session_dir)
    launch.remove_clipboard_dir(session_dir)
    assert list(session_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestRemoveClipboardDir:
    def test_removes_existing_directory(self, tmp_path):
        clip = sandbox_mounts.clipboard_image_dir(tmp_path)
        clip.mkdir(parents=True)
        (clip / "paste-1.png").write_bytes(b"x")
        (clip / "paste-2.png").write_bytes(b"y")

        sandbox_launch.remove_clipboard_dir(tmp_path)

        assert not clip.exists()
        # session_dir itself is preserved — only the clipboard subdir was cleaned.
        assert tmp_path.exists()

    def test_idempotent_when_dir_absent(self, tmp_path):
        # No clipboard subdir was ever created.
        sandbox_launch.remove_clipboard_dir(tmp_path)  # must not raise
        assert not sandbox_mounts.clipboard_image_dir(tmp_path).exists()


def _task_branch(name: str = "my-task", prefix: str = branches.DEFAULT_BRANCH_PREFIX) -> branches.BranchName:
    return branches.BranchPrefix(prefix).task_branch(name)


def _no_wt_meta(cwd):
    """Synthetic SessionMeta for no-worktree mount tests."""
    return SessionMeta(name="-", repo=str(cwd), worktree=str(cwd), no_worktree=True)


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# find_task_file
# ---------------------------------------------------------------------------


class TestExecTaskShell:
    """Verify exec_task_shell execs directly into the named container."""

    def test_calls_docker_exec_with_container_name(self):
        with patch("seekr_hatchery.sandbox.runtime.subprocess.run") as mock_run:
            sandbox_launch.exec_task_shell("hatchery-myrepo-deadbeef-my-task", sandbox_runtime.DockerRuntime())
        cmd = mock_run.call_args[0][0]
        assert cmd == ["docker", "exec", "-it", "hatchery-myrepo-deadbeef-my-task", "/bin/bash"]

    def test_custom_shell_passed_to_exec(self):
        with patch("seekr_hatchery.sandbox.runtime.subprocess.run") as mock_run:
            sandbox_launch.exec_task_shell(
                "hatchery-myrepo-deadbeef-my-task", sandbox_runtime.DockerRuntime(), shell="/bin/sh"
            )
        cmd = mock_run.call_args[0][0]
        assert cmd[-1] == "/bin/sh"

    def test_uses_podman_binary_for_podman_runtime(self):
        with patch("seekr_hatchery.sandbox.runtime.subprocess.run") as mock_run:
            sandbox_launch.exec_task_shell("hatchery-myrepo-deadbeef-my-task", sandbox_runtime.PodmanRuntime())
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "podman"


# ---------------------------------------------------------------------------
# find_task_file
# ---------------------------------------------------------------------------


class TestSandboxContextIncludePaths:
    """Verify that include_paths appear in the sandbox context output."""

    _BASE = dict(
        name="my-task",
        branch="hatchery/my-task",
        worktree=Path("/repo/.hatchery/worktrees/my-task"),
        repo=Path("/repo"),
        main_branch="main",
    )

    def _ctx(self, use_docker: bool, no_worktree: bool, include_paths=None) -> str:
        return sessions.sandbox_context(
            **self._BASE,
            use_docker=use_docker,
            no_worktree=no_worktree,
            include_paths=include_paths,
        )

    def _entry(self, path, mode="worktree"):
        from seekr_hatchery.includes import IncludeEntry

        return IncludeEntry(path=path, mode=mode)

    def test_no_includes_produces_no_includes_section(self):
        result = self._ctx(use_docker=True, no_worktree=False)
        assert "Included paths" not in result

    def test_empty_list_produces_no_includes_section(self):
        result = self._ctx(use_docker=True, no_worktree=False, include_paths=[])
        assert "Included paths" not in result

    def test_docker_plain_dir_shows_host_path(self, tmp_path):
        plain = tmp_path / "shared-data"
        plain.mkdir()
        result = self._ctx(use_docker=True, no_worktree=False, include_paths=[self._entry(plain)])
        assert "Included paths" in result
        assert str(plain) in result

    def test_docker_git_repo_with_worktree_shows_worktree_host_path(self, tmp_path):
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()

        wt = repo_b / constants.WORKTREES_SUBDIR / "my-task"
        wt.mkdir(parents=True)
        result = self._ctx(use_docker=True, no_worktree=False, include_paths=[self._entry(repo_b)])
        assert str(wt) in result

    def test_docker_ro_reference_shows_read_only_label(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        result = self._ctx(use_docker=True, no_worktree=False, include_paths=[self._entry(docs, mode="ro")])
        assert "Included paths" in result
        assert str(docs) in result
        assert "read-only" in result

    def test_docker_rw_reference_shows_read_write_label(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        result = self._ctx(use_docker=True, no_worktree=False, include_paths=[self._entry(shared, mode="rw")])
        assert str(shared) in result
        assert "read-write" in result


# ---------------------------------------------------------------------------
# find_task_file
# ---------------------------------------------------------------------------


class TestSandboxContextNoWorktree:
    def _no_worktree_native(self, branch: str = "", **kwargs) -> str:
        defaults = dict(
            name="my-task",
            branch=branch,
            worktree=Path("/projects/my-workspace"),
            repo=Path("/projects/my-workspace"),
            main_branch="main",
            use_docker=False,
            no_worktree=True,
        )
        defaults.update(kwargs)
        return sessions.sandbox_context(**defaults)

    def _no_worktree_docker(self, branch: str = "", **kwargs) -> str:
        defaults = dict(
            name="my-task",
            branch=branch,
            worktree=Path("/projects/my-workspace"),
            repo=Path("/projects/my-workspace"),
            main_branch="main",
            use_docker=True,
            no_worktree=True,
        )
        defaults.update(kwargs)
        return sessions.sandbox_context(**defaults)

    # --- native no-worktree ---

    def test_native_no_worktree_mentions_directly(self):
        result = self._no_worktree_native()
        assert "directly in the working directory" in result

    def test_native_no_worktree_does_not_claim_docker(self):
        result = self._no_worktree_native()
        assert "Docker container" not in result

    def test_native_no_worktree_does_not_claim_native_worktree(self):
        result = self._no_worktree_native()
        assert "native git worktree" not in result

    def test_native_no_worktree_shows_working_directory(self):
        result = self._no_worktree_native(worktree=Path("/some/dir"))
        assert "/some/dir" in result

    def test_native_no_worktree_branch_shown_when_nonempty(self):
        result = self._no_worktree_native(branch="hatchery/my-task")
        assert "hatchery/my-task" in result

    def test_native_no_worktree_branch_omitted_when_empty(self):
        result = self._no_worktree_native(branch="")
        assert "Your branch" not in result

    def test_native_no_worktree_pr_target_omitted_when_no_branch(self):
        result = self._no_worktree_native(branch="")
        assert "Target branch" not in result

    # --- docker no-worktree ---

    def test_docker_no_worktree_mentions_docker_container(self):
        result = self._no_worktree_docker()
        assert "Docker container" in result

    def test_docker_no_worktree_mentions_working_directory(self):
        result = self._no_worktree_docker(worktree=Path("/host/some/cwd"))
        assert "/host/some/cwd" in result

    def test_docker_no_worktree_does_not_claim_native_worktree(self):
        result = self._no_worktree_docker()
        assert "native git worktree" not in result

    def test_docker_no_worktree_branch_shown_when_nonempty(self):
        result = self._no_worktree_docker(branch="hatchery/my-task")
        assert "hatchery/my-task" in result

    def test_docker_no_worktree_branch_omitted_when_empty(self):
        result = self._no_worktree_docker(branch="")
        assert "Your branch" not in result

    def test_docker_no_worktree_pr_target_omitted_when_no_branch(self):
        result = self._no_worktree_docker(branch="")
        assert "Target branch" not in result

    def test_docker_no_worktree_pr_target_shown_when_branch_present(self):
        result = self._no_worktree_docker(branch="hatchery/my-task", main_branch="develop")
        assert "develop" in result


# ---------------------------------------------------------------------------
# find_task_file
# ---------------------------------------------------------------------------


class TestSandboxContext:
    def _native(self, **kwargs):
        defaults = dict(
            name="my-task",
            branch="hatchery/my-task",
            worktree=Path("/repo/.hatchery/worktrees/my-task"),
            repo=Path("/repo"),
            main_branch="main",
            use_docker=False,
        )
        defaults.update(kwargs)
        return sessions.sandbox_context(**defaults)

    def _docker(self, **kwargs):
        defaults = dict(
            name="my-task",
            branch="hatchery/my-task",
            worktree=Path("/repo/.hatchery/worktrees/my-task"),
            repo=Path("/repo"),
            main_branch="main",
            use_docker=True,
        )
        defaults.update(kwargs)
        return sessions.sandbox_context(**defaults)

    # --- return type ---

    def test_returns_string(self):
        assert isinstance(self._native(), str)

    # --- native mode ---

    def test_native_mentions_native_worktree(self):
        result = self._native()
        assert "native git worktree" in result

    def test_native_does_not_claim_docker_container(self):
        result = self._native()
        assert "Docker container" not in result

    def test_native_contains_working_directory(self):
        result = self._native(worktree=Path("/repo/.hatchery/worktrees/my-task"))
        assert "/repo/.hatchery/worktrees/my-task" in result

    def test_native_contains_repo_root(self):
        result = self._native(repo=Path("/some/repo"))
        assert "/some/repo" in result

    def test_native_contains_branch(self):
        result = self._native(branch="hatchery/my-task")
        assert "hatchery/my-task" in result

    def test_native_contains_main_branch(self):
        result = self._native(main_branch="develop")
        assert "develop" in result

    def test_native_pr_target_instruction(self):
        result = self._native(main_branch="main")
        assert "main" in result
        assert "pull request" in result.lower() or "PR" in result

    # --- docker mode ---

    def test_docker_mentions_docker_container(self):
        result = self._docker()
        assert "Docker container" in result

    def test_docker_does_not_mention_native(self):
        result = self._docker()
        assert "native git worktree" not in result

    def test_docker_contains_container_worktree_path(self):
        result = self._docker(name="my-task")
        assert "/repo/.hatchery/worktrees/my-task" in result

    def test_docker_contains_repo_root(self):
        result = self._docker(repo=Path("/some/repo"))
        assert "/some/repo" in result

    def test_docker_mentions_read_write(self):
        result = self._docker()
        assert "read-write" in result

    def test_docker_mentions_main_branch_inspection(self):
        # After the host-path-mirroring switch the RO main-branch file view
        # at /repo is gone — the system prompt should tell agents to use git.
        result = self._docker()
        assert "git show" in result or "git diff" in result

    def test_docker_contains_branch(self):
        result = self._docker(branch="hatchery/my-task")
        assert "hatchery/my-task" in result

    def test_docker_contains_main_branch(self):
        result = self._docker(main_branch="master")
        assert "master" in result

    def test_docker_pr_target_instruction(self):
        result = self._docker(main_branch="main")
        assert "main" in result
        assert "pull request" in result.lower() or "PR" in result


@pytest.mark.parametrize("enabled", [False, True])
def test_session_paste_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_backend: agents.AgentBackend, enabled: bool
) -> None:
    import subprocess

    import seekr_hatchery.stream_interceptor.interceptors.clipboard_image as clipboard_image

    monkeypatch.setattr(constants, "HATCHERY_DIR", tmp_path / "state")
    monkeypatch.setattr(images, "build_image", lambda *args, **kwargs: True)
    monkeypatch.setattr(clipboard_image.sys, "platform", "linux")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=b"\x89PNG\r\n\x1a\nimage"),
    )
    meta = models.SessionMeta(name="task", repo=str(tmp_path), worktree=str(tmp_path), no_worktree=True)
    observed: list[bool] = []

    class RecordingRuntime(runtime.DockerRuntime):
        def run(self, container_spec: spec.ContainerSpec, **kwargs: object) -> None:
            interceptor = kwargs["paste_interceptor"]
            observed.append(interceptor is not None)
            if interceptor is not None:
                assert interceptor.on_stdin(b"\x16") != b"\x16"
                assert [p.read_bytes() for p in mounts.clipboard_image_dir(meta.session_dir).iterdir()] == [
                    b"\x89PNG\r\n\x1a\nimage"
                ]

    launch.run_session(
        meta,
        spy_backend,
        ["agent"],
        config.SandboxConfig(clipboard_images=enabled),
        proxy_token="token",
        runtime=RecordingRuntime(),
    )
    assert observed == [enabled]


def test_explicit_native_never_discovers_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected() -> runtime.ContainerRuntime:
        pytest.fail("native mode must not discover a runtime")

    monkeypatch.setattr(runtime, "detect_runtime", unexpected)
    assert launch.resolve_runtime(tmp_path, True) is None
