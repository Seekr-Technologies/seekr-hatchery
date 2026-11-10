import logging
import subprocess
import sys as _sys
from pathlib import Path

import pytest

import seekr_hatchery.agents as agent
import seekr_hatchery.branches as branches
import seekr_hatchery.sandbox.images as sandbox_images
import seekr_hatchery.sandbox.runtime as sandbox_runtime
import seekr_hatchery.sandbox.scaffold as sandbox_scaffold
from seekr_hatchery.models import SessionMeta


@pytest.mark.parametrize(
    "runtime", [sandbox_runtime.DockerRuntime(), sandbox_runtime.PodmanRuntime()], ids=["docker", "podman"]
)
@pytest.mark.parametrize("debug", [False, True], ids=["normal", "debug"])
@pytest.mark.parametrize("no_cache", [False, True], ids=["cached", "no-cache"])
def test_always_builds_without_inspection_or_reading_dockerfile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    runtime: sandbox_runtime.ContainerRuntime,
    debug: bool,
    no_cache: bool,
) -> None:
    """Repeated calls delegate to the builder, even when the Dockerfile is absent."""
    commands: list[tuple[list[str], dict[str, object]]] = []
    dockerfile = sandbox_scaffold.dockerfile_path(tmp_path, agent.CODEX)
    assert not dockerfile.exists()
    caplog.set_level(logging.DEBUG if debug else logging.WARNING, logger="seekr_hatchery.docker")
    monkeypatch.setattr(_sys.stdout, "isatty", lambda: False)

    def run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert list(Path(cmd[-1]).iterdir()) == []
        commands.append(([*cmd[:-1], "<empty-context>"], kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sandbox_images.subprocess, "run", run)
    for _ in range(2):
        assert (
            sandbox_images.build_image(tmp_path, tmp_path, "test-task", agent.CODEX, runtime=runtime, no_cache=no_cache)
            is True
        )

    expected_command = [
        runtime.binary,
        "build",
        "-f",
        str(dockerfile),
        "-t",
        "test-task",
        *(["--no-cache"] if no_cache else []),
        "<empty-context>",
    ]
    expected_kwargs: dict[str, object] = {"cwd": tmp_path, "stdin": subprocess.DEVNULL}
    if not debug:
        expected_kwargs |= {"capture_output": True, "text": True}
    assert commands == [(expected_command, expected_kwargs), (expected_command, expected_kwargs)]


class TestStreamBuild:
    def test_non_tty_passes_stdin_devnull(self, monkeypatch, tmp_path):
        """The non-TTY path must pass stdin=DEVNULL to subprocess.run."""
        monkeypatch.setattr(_sys.stdout, "isatty", lambda: False)

        captured_kwargs: list[dict] = []

        def _mock_run(cmd, **kw):
            captured_kwargs.append(kw)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(sandbox_runtime.subprocess, "run", _mock_run)
        sandbox_scaffold.dockerfile_path(tmp_path, agent.CODEX).write_text("FROM debian\n")
        monkeypatch.setattr(sandbox_images.logger, "isEnabledFor", lambda level: False)
        sandbox_images.build_image(
            tmp_path, tmp_path, "test", agent.CODEX, runtime=sandbox_runtime.DockerRuntime(), no_cache=True
        )
        assert captured_kwargs[0].get("stdin") is subprocess.DEVNULL


def _task_branch(name: str = "my-task", prefix: str = branches.DEFAULT_BRANCH_PREFIX) -> branches.BranchName:
    return branches.BranchPrefix(prefix).task_branch(name)


def _no_wt_meta(cwd):
    """Synthetic SessionMeta for no-worktree mount tests."""
    return SessionMeta(name="-", repo=str(cwd), worktree=str(cwd), no_worktree=True)


# ---------------------------------------------------------------------------
# DockerRuntime.available()
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

        sandbox_scaffold.dockerfile_path(worktree, agent.CODEX).write_text("FROM debian\n")

        captured: list[list[str]] = []
        captured_kwargs: list[dict] = []

        def _mock_run(cmd, **kw):
            captured.append(cmd)
            captured_kwargs.append(kw)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(sandbox_runtime.subprocess, "run", _mock_run)

        if debug:
            monkeypatch.setattr(sandbox_images.logger, "isEnabledFor", lambda _lvl: True)
        else:
            monkeypatch.setattr(sandbox_images.logger, "isEnabledFor", lambda _lvl: False)
            # _stream_build is used in non-debug mode; stub it out
            monkeypatch.setattr(sandbox_images, "_stream_build", lambda cmd, cwd: (0, []))

        sandbox_images.build_image(repo, worktree, "test-task", agent.CODEX, runtime=sandbox_runtime.PodmanRuntime())
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

    def test_failed_build_reports_dockerfile_path(self, monkeypatch, tmp_path, capsys):
        repo = tmp_path / "repo"
        hatchery_dir = repo / ".hatchery"
        hatchery_dir.mkdir(parents=True)
        dockerfile = sandbox_scaffold.dockerfile_path(hatchery_dir, agent.CODEX)
        dockerfile.write_text("FROM debian\n")
        monkeypatch.setattr(sandbox_images.logger, "isEnabledFor", lambda _lvl: False)
        monkeypatch.setattr(sandbox_images, "_stream_build", lambda cmd, cwd: (1, ["build error"]))

        result = sandbox_images.build_image(
            repo,
            hatchery_dir,
            "test-task",
            agent.CODEX,
            runtime=sandbox_runtime.PodmanRuntime(),
            exit_on_error=False,
        )

        assert result is False
        assert capsys.readouterr().err == f"Error: podman build failed.\n  Dockerfile: {dockerfile}\n"
