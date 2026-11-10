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
def test_forced_builds_skip_image_inspection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    runtime: sandbox_runtime.ContainerRuntime,
    debug: bool,
) -> None:
    """Forced rebuilds bypass inspection and the layer cache on every call."""
    commands: list[tuple[list[str], dict[str, object]]] = []
    dockerfile = sandbox_scaffold.dockerfile_path(tmp_path, agent.CODEX)
    dockerfile.write_text("FROM scratch\n")
    caplog.set_level(logging.DEBUG if debug else logging.WARNING, logger="seekr_hatchery.docker")
    monkeypatch.setattr(_sys.stdout, "isatty", lambda: False)

    def run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert list(Path(cmd[-1]).iterdir()) == []
        commands.append(([*cmd[:-1], "<empty-context>"], kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sandbox_images.subprocess, "run", run)
    for _ in range(2):
        assert (
            sandbox_images.build_image(tmp_path, tmp_path, "test-task", agent.CODEX, runtime=runtime, no_cache=True)
            is True
        )

    expected_command = [
        runtime.binary,
        "build",
        "-f",
        str(dockerfile),
        "-t",
        "test-task",
        "-t",
        "hatchery/cache:61d3e0bf42967724d2fa8a0d51928e51ae09853898c1c4fd08e12e118f08c66f",
        "--label",
        "io.seekr-hatchery.build-fingerprint=61d3e0bf42967724d2fa8a0d51928e51ae09853898c1c4fd08e12e118f08c66f",
        "--no-cache",
        "<empty-context>",
    ]
    expected_kwargs: dict[str, object] = {"cwd": tmp_path, "stdin": subprocess.DEVNULL}
    if not debug:
        expected_kwargs |= {"capture_output": True, "text": True}
    assert commands == [(expected_command, expected_kwargs), (expected_command, expected_kwargs)]


@pytest.mark.parametrize(
    "runtime", [sandbox_runtime.DockerRuntime(), sandbox_runtime.PodmanRuntime()], ids=["docker", "podman"]
)
@pytest.mark.parametrize("debug", [False, True], ids=["normal", "debug"])
@pytest.mark.parametrize(
    "scenario",
    ["matching", "missing", "unlabeled", "stale", "edited", "agent", "forced", "inspect-error"],
)
def test_image_fingerprint_reuse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runtime: sandbox_runtime.ContainerRuntime,
    debug: bool,
    scenario: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hatchery_dir = tmp_path / ".hatchery"
    hatchery_dir.mkdir()
    dockerfile = sandbox_scaffold.dockerfile_path(hatchery_dir, agent.CODEX)
    dockerfile.write_bytes(b"FROM debian\n")
    sandbox_scaffold.dockerfile_path(hatchery_dir, agent.PI).write_bytes(b"FROM debian\n")
    commands: list[list[str]] = []
    labels: list[str] = []
    inspect_code = 1

    def run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal inspect_code
        assert kwargs["stdin"] == subprocess.DEVNULL
        if cmd[1] == "build":
            assert list(Path(cmd[-1]).iterdir()) == []
            commands.append([*cmd[:-1], "<empty-context>"])
            labels[:] = [cmd[cmd.index("--label") + 1].split("=", 1)[1]]
            inspect_code = 0
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        commands.append(cmd)
        assert kwargs == {
            "cwd": tmp_path,
            "stdin": subprocess.DEVNULL,
            "capture_output": True,
            "text": True,
        }
        return subprocess.CompletedProcess(cmd, inspect_code, stdout=(labels[0] if labels else "") + "\n", stderr="")

    monkeypatch.setattr(sandbox_images.subprocess, "run", run)
    monkeypatch.setattr(sandbox_images.logger, "isEnabledFor", lambda level: debug)
    monkeypatch.setattr(_sys.stdout, "isatty", lambda: False)
    assert sandbox_images.build_image(tmp_path, hatchery_dir, "test-task", agent.CODEX, runtime=runtime) is True
    original_label = labels[0]
    assert len(original_label) == 64
    commands.clear()
    capsys.readouterr()

    backend = agent.CODEX
    if scenario in {"missing", "inspect-error"}:
        inspect_code = 1 if scenario == "missing" else 125
    elif scenario == "unlabeled":
        labels.clear()
    elif scenario == "stale":
        labels[:] = ["old-fingerprint"]
    elif scenario == "edited":
        dockerfile.write_bytes(b"FROM debian\nRUN echo changed\n")
    elif scenario == "agent":
        backend = agent.PI

    assert (
        sandbox_images.build_image(
            tmp_path, hatchery_dir, "new-task", backend, runtime=runtime, no_cache=scenario == "forced"
        )
        is True
    )
    cache_image = f"hatchery/cache:{labels[0]}"
    expected = []
    if scenario != "forced":
        expected.append(
            [
                runtime.binary,
                "image",
                "inspect",
                "--format",
                '{{ index .Config.Labels "io.seekr-hatchery.build-fingerprint" }}',
                cache_image,
            ]
        )
    if scenario != "matching":
        expected.append(
            [
                runtime.binary,
                "build",
                "-f",
                str(sandbox_scaffold.dockerfile_path(hatchery_dir, backend)),
                "-t",
                "new-task",
                "-t",
                cache_image,
                "--label",
                f"io.seekr-hatchery.build-fingerprint={labels[0]}",
                *(["--no-cache"] if scenario == "forced" else []),
                "<empty-context>",
            ]
        )
    else:
        expected.append([runtime.binary, "tag", cache_image, "new-task"])
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == (
            "Using cached sandbox image 'new-task'\n  Sandbox image ready.\n",
            "",
        )
    assert commands == expected
    assert (labels[0] != original_label) == (scenario in {"edited", "agent"})


@pytest.mark.parametrize(
    "runtime", [sandbox_runtime.DockerRuntime(), sandbox_runtime.PodmanRuntime()], ids=["docker", "podman"]
)
@pytest.mark.parametrize("exit_on_error", [False, True], ids=["return-false", "exit"])
def test_cached_image_tag_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    runtime: sandbox_runtime.ContainerRuntime,
    exit_on_error: bool,
) -> None:
    sandbox_scaffold.dockerfile_path(tmp_path, agent.CODEX).write_text("FROM scratch\n")
    fingerprint = "61d3e0bf42967724d2fa8a0d51928e51ae09853898c1c4fd08e12e118f08c66f"
    cache_image = f"hatchery/cache:{fingerprint}"
    calls: list[list[str]] = []

    def run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        assert kwargs == {
            "cwd": tmp_path,
            "stdin": subprocess.DEVNULL,
            "capture_output": True,
            "text": True,
        }
        if cmd[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=fingerprint + "\n", stderr="")
        assert cmd[1] == "tag"
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="tag denied\n")

    monkeypatch.setattr(sandbox_images.subprocess, "run", run)
    if exit_on_error:
        with pytest.raises(SystemExit) as exc:
            sandbox_images.build_image(tmp_path, tmp_path, "new-task", agent.CODEX, runtime=runtime)
        assert exc.value.code == 1
    else:
        assert (
            sandbox_images.build_image(
                tmp_path, tmp_path, "new-task", agent.CODEX, runtime=runtime, exit_on_error=False
            )
            is False
        )
    assert calls == [
        [
            runtime.binary,
            "image",
            "inspect",
            "--format",
            '{{ index .Config.Labels "io.seekr-hatchery.build-fingerprint" }}',
            cache_image,
        ],
        [runtime.binary, "tag", cache_image, "new-task"],
    ]
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (
        "tag denied\n",
        f"Error: {runtime.binary} tag failed.\n  Source: {cache_image}\n  Target: new-task\n",
    )


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
            if cmd[1:3] == ["image", "inspect"]:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not found")
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
            no_cache=True,
            exit_on_error=False,
        )

        assert result is False
        assert capsys.readouterr().err == f"Error: podman build failed.\n  Dockerfile: {dockerfile}\n"
