"""Sandbox integration tests — verify container behaviour from the inside.

Skipped by default; opt in with:
    uv run pytest tests/test_sandbox.py --integration -v
"""

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

import seekr_hatchery.agents as agent
import seekr_hatchery.docker as docker
import seekr_hatchery.proxy as proxy_mod
import seekr_hatchery.tasks as tasks

pytestmark = pytest.mark.integration

# Captured at import time, before any fixture patches HOME.
_REAL_HOME = os.environ.get("HOME", "")

CONTAINER_WORKTREE = f"{tasks.CONTAINER_REPO_ROOT}/.hatchery/worktrees/test-wt"


@pytest.fixture(autouse=True)
def _runtime_real_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject the real HOME only into podman/docker subprocess calls.

    conftest.home patches os.environ["HOME"] so tests cannot accidentally write
    to real dotfiles (~/.codex, ~/.hatchery, etc.).  That patch breaks Podman on
    macOS: it reads its VM socket path from $HOME/.config/containers/podman/machine/.
    This fixture restores HOME for container-runtime calls only, leaving all other
    subprocess calls (and Python-level code) with the fake home.
    """
    orig = subprocess.run

    def _run(args, *pargs, **kwargs):
        if isinstance(args, (list, tuple)) and args and args[0] in ("podman", "docker"):
            env = {**(kwargs.pop("env", None) or os.environ), "HOME": _REAL_HOME}
            return orig(args, *pargs, **kwargs, env=env)
        return orig(args, *pargs, **kwargs)

    monkeypatch.setattr(subprocess, "run", _run)


@pytest.fixture(scope="session")
def runtime() -> docker.Runtime:
    if docker.podman_available():
        return docker.Runtime.PODMAN
    if docker.docker_available():
        return docker.Runtime.DOCKER
    pytest.skip("no container runtime available")


@pytest.fixture(scope="session", autouse=True)
def _prepull_images(runtime: docker.Runtime) -> None:
    """Best-effort pre-pull of base images with retries for Docker Hub rate limits."""
    for img in (
        "docker.io/library/alpine:latest",
        "docker.io/alpine/git:latest",
        "docker.io/library/debian:trixie-slim",
    ):
        if subprocess.run([runtime.binary, "image", "exists", img], capture_output=True).returncode == 0:
            continue
        for attempt in range(1, 6):
            result = subprocess.run([runtime.binary, "pull", img], capture_output=True, text=True)
            if result.returncode == 0:
                break
            time.sleep(attempt * 15)


# ---------------------------------------------------------------------------
# No-worktree sandbox fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def no_wt_cwd(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Minimal cwd with a hatchery Dockerfile; no git repo, no worktree."""
    cwd = tmp_path_factory.mktemp("no_wt")
    hatchery_dir = cwd / ".hatchery"
    hatchery_dir.mkdir()
    (hatchery_dir / "Dockerfile.codex").write_text("FROM alpine\n")
    (hatchery_dir / "docker.yaml").write_text("schema_version: 1\n")
    return cwd


@pytest.fixture(scope="module")
def no_wt_image(no_wt_cwd: Path, runtime: docker.Runtime) -> str:
    """Build the no-worktree sandbox image once; remove it after the module."""
    docker.build_docker_image(no_wt_cwd, no_wt_cwd, "test-no-wt", agent.CODEX, runtime=runtime)
    image = docker.docker_image_name(no_wt_cwd, "test-no-wt")
    yield image
    subprocess.run([runtime.binary, "rmi", "-f", image], capture_output=True)


@pytest.fixture()
def no_wt_run(
    no_wt_cwd: Path,
    no_wt_image: str,
    runtime: docker.Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], list[str]]:
    """No-worktree container runner.  Mirrors launch_docker_no_worktree without
    requiring a real API key.

    Returns ``(run_fn, mounts)`` where ``run_fn(command, *, api_key, proxy_token)``
    executes a command override in the production-configured sandbox container.
    """
    # --userns=keep-id fails in nested Podman (DinD); drop it for all sandbox tests.
    monkeypatch.setattr(docker, "_userns_flags", lambda _r: [])

    session_dir = tasks.task_session_dir(no_wt_cwd, "test-no-wt")
    session_dir.mkdir(parents=True, exist_ok=True)
    agent.CODEX.on_new_task(session_dir)
    # Codex's home_mounts requires codex_auth.json (created by on_before_container_start)
    # and ~/.codex to exist.
    agent.CODEX.on_before_container_start(session_dir, "test-proxy-token", "/workspace")
    (Path.home() / ".codex").mkdir(parents=True, exist_ok=True)
    mounts = docker.docker_mounts_no_worktree(no_wt_cwd, agent.CODEX, session_dir, docker.DockerConfig())

    def run(
        command: list[str],
        *,
        mutator: Callable[[dict], dict] | None = None,
        proxy_token: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = docker._run_container(
            image=no_wt_image,
            mounts=mounts,
            workdir="/workspace",
            hatchery_repo="/workspace",
            name="test-no-wt",
            mutator=mutator,
            proxy_token=proxy_token,
            agent_cmd=[],
            runtime=runtime,
            _command_override=command,
        )
        assert result is not None
        return result

    return run, mounts


# ---------------------------------------------------------------------------
# Worktree sandbox fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def wt_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Git repo with Dockerfile (git included) and an initial commit."""
    repo = tmp_path_factory.mktemp("wt_repo")
    hatchery_dir = repo / ".hatchery"
    hatchery_dir.mkdir()
    # Use COPY --from to avoid RUN apk add, which calls capset() and fails in DinD.
    (hatchery_dir / "Dockerfile.codex").write_text(
        "FROM docker.io/alpine/git AS git-src\n"
        "FROM alpine\n"
        "COPY --from=git-src /usr/bin/git /usr/bin/git\n"
        "COPY --from=git-src /usr/lib/libpcre2-8.so.0 /usr/lib/libpcre2-8.so.0\n"
    )
    (hatchery_dir / "docker.yaml").write_text("schema_version: 1\n")
    (repo / "README.md").write_text("test repo\n")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture(scope="module")
def wt_worktree(wt_repo: Path) -> Path:
    """Git worktree at .hatchery/worktrees/test-wt on branch hatchery/test-wt."""
    worktrees_dir = wt_repo / ".hatchery" / "worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    worktree = worktrees_dir / "test-wt"
    subprocess.run(
        ["git", "worktree", "add", str(worktree), "-b", "hatchery/test-wt"],
        cwd=wt_repo,
        check=True,
        capture_output=True,
    )
    return worktree


@pytest.fixture(scope="module")
def wt_image(wt_repo: Path, wt_worktree: Path, runtime: docker.Runtime) -> str:
    """Build the worktree sandbox image (alpine+git) once; remove it after the module."""
    docker.build_docker_image(wt_repo, wt_worktree, "test-wt", agent.CODEX, runtime=runtime)
    image = docker.docker_image_name(wt_repo, "test-wt")
    yield image
    subprocess.run([runtime.binary, "rmi", "-f", image], capture_output=True)


@pytest.fixture()
def wt_run(
    wt_repo: Path,
    wt_worktree: Path,
    wt_image: str,
    runtime: docker.Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Callable[[list[str]], subprocess.CompletedProcess[str]], list[str]]:
    """Worktree container runner.  Mirrors the pre-flight setup in launch_docker
    (sentinel files, git_ptr rewrite, mount construction) without requiring a
    real API key.

    Returns ``(run_fn, mounts)`` where ``run_fn(command)`` executes a command
    override inside the production-configured worktree sandbox container.

    Note: intentionally replicates the sentinel / git_ptr logic from
    launch_docker so that changes to that logic must be mirrored here.
    """
    # --userns=keep-id fails in nested Podman (DinD); drop it for all sandbox tests.
    monkeypatch.setattr(docker, "_userns_flags", lambda _r: [])

    task_name = "test-wt"
    session_dir = tasks.task_session_dir(wt_repo, task_name)
    session_dir.mkdir(parents=True, exist_ok=True)
    agent.CODEX.on_new_task(session_dir)
    agent.CODEX.on_before_container_start(session_dir, "test-proxy-token", CONTAINER_WORKTREE)
    (Path.home() / ".codex").mkdir(parents=True, exist_ok=True)

    # Mirror launch_docker: create sentinel files for any .git-root writes.
    git_sentinels: list[tuple[Path, str]] = []
    for fname in ("COMMIT_EDITMSG", "ORIG_HEAD"):
        if not (wt_repo / ".git" / fname).exists():
            continue
        p = session_dir / fname
        if not p.exists():
            p.touch()
        git_sentinels.append((p, fname))

    # Rewrite the worktree .git pointer to use the container-relative path.
    git_ptr = session_dir / "git_ptr"
    git_ptr.write_text(f"gitdir: {tasks.CONTAINER_REPO_ROOT}/.git/worktrees/{task_name}\n")

    mounts = docker.docker_mounts(
        wt_repo,
        wt_worktree,
        task_name,
        agent.CODEX,
        session_dir,
        docker.DockerConfig(),
        git_sentinels,
        worktree_git_ptr=git_ptr,
    )

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        result = docker._run_container(
            image=wt_image,
            mounts=mounts,
            workdir=CONTAINER_WORKTREE,
            hatchery_repo=tasks.CONTAINER_REPO_ROOT,
            name=task_name,
            mutator=None,
            proxy_token=None,
            agent_cmd=[],
            runtime=runtime,
            _command_override=command,
        )
        assert result is not None
        return result

    return run, mounts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mount_access_script(mounts: list[str]) -> str:
    """Build a sh script that probes each container mount path for actual RW/RO access.

    For directory mounts: attempts to create (then delete) a probe file.
    For file mounts: opens in append mode (writes 0 bytes — no content change).
    Emits one line per path: "rw:<path>" or "ro:<path>".
    """
    checks = []
    for m in mounts:
        parts = m.split(":")
        path = parts[1]
        checks.append(
            f'if [ -d "{path}" ]; then '
            f'  if touch "{path}/.rw_probe" 2>/dev/null; then '
            f'    rm -f "{path}/.rw_probe"; printf "rw:{path}\\n"; '
            f'  else printf "ro:{path}\\n"; fi; '
            f"else "
            f'  if (: >> "{path}") 2>/dev/null; then printf "rw:{path}\\n"; '
            f'  else printf "ro:{path}\\n"; fi; '
            f"fi"
        )
    return "; ".join(checks)


def _assert_mounts(result: subprocess.CompletedProcess[str], mounts: list[str]) -> None:
    """Assert every mount in *mounts* reports the declared RW/RO access."""
    assert result.returncode == 0, f"Mount probe script failed:\n{result.stderr}"
    for m in mounts:
        parts = m.split(":")
        container_path = parts[1]
        declared_mode = parts[2] if len(parts) > 2 else "rw"
        if declared_mode == "rw":
            assert f"rw:{container_path}" in result.stdout, (
                f"{container_path}: declared RW but container reports RO\n{result.stdout}"
            )
        else:
            assert f"ro:{container_path}" in result.stdout, (
                f"{container_path}: declared RO but container reports RW\n{result.stdout}"
            )


# ---------------------------------------------------------------------------
# TestNoWorktreeMounts
# ---------------------------------------------------------------------------


class TestNoWorktreeMounts:
    """Every path in the no-worktree mount layout has its declared RW/RO access
    when probed from inside the container."""

    def test_mount_access(
        self,
        no_wt_run: tuple[Callable[..., subprocess.CompletedProcess[str]], list[str]],
    ) -> None:
        run, mounts = no_wt_run
        _assert_mounts(run(["sh", "-c", _mount_access_script(mounts)]), mounts)


# ---------------------------------------------------------------------------
# TestContainerEnv
# ---------------------------------------------------------------------------


class TestSandboxShell:
    """Verify _run_container with _interactive=True executes the command."""

    def test_interactive_command_runs(
        self,
        no_wt_image: str,
        runtime: docker.Runtime,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_interactive=True runs the command and returns None (output inherited)."""
        monkeypatch.setattr(docker, "_userns_flags", lambda _r: [])
        result = docker._run_container(
            image=no_wt_image,
            mounts=[],
            workdir="/",
            hatchery_repo="/",
            name="test-sandbox",
            mutator=None,
            proxy_token=None,
            agent_cmd=[],
            runtime=runtime,
            _command_override=["echo", "sandbox-ok"],
            _interactive=True,
        )
        # _interactive=True returns None (output goes to inherited stdout)
        assert result is None


class TestContainerEnv:
    """Container injects HATCHERY_TASK, HATCHERY_REPO, sets workdir correctly,
    and sets workdir correctly."""

    def test_env_and_workdir(
        self,
        no_wt_run: tuple[Callable[..., subprocess.CompletedProcess[str]], list[str]],
    ) -> None:
        run, _ = no_wt_run
        script = (
            'printf "TASK=%s\\n" "$HATCHERY_TASK"; printf "REPO=%s\\n" "$HATCHERY_REPO"; printf "WD=%s\\n" "$(pwd)"'
        )
        result = run(["sh", "-c", script])
        assert result.returncode == 0, f"Script failed:\n{result.stderr}"
        assert "TASK=test-no-wt" in result.stdout
        assert "REPO=/workspace" in result.stdout
        assert "WD=/workspace" in result.stdout


# ---------------------------------------------------------------------------
# TestContainerProxy
# ---------------------------------------------------------------------------


class TestContainerProxy:
    """Container can reach the host proxy and requests are forwarded to the upstream API."""

    def test_proxy_reachable_from_container(self, runtime: docker.Runtime) -> None:
        """TCP connection from container to the proxy succeeds via host.docker.internal.

        Uses a bare subprocess.run (not _run_container) to test the underlying
        host-gateway networking independently of our mount/env setup.
        """
        server, _ = proxy_mod.start_proxy("fake-real-key", "correct-token")
        port = server.server_address[1]
        # --add-host is what _run_container injects on Linux when api_key is set.
        extra_args = ["--add-host=host.docker.internal:host-gateway"] if sys.platform == "linux" else []
        try:
            result = subprocess.run(
                [
                    runtime.binary,
                    "run",
                    "--rm",
                    *extra_args,
                    "alpine",
                    "sh",
                    "-c",
                    f"nc -zw3 host.docker.internal {port}; echo nc_exit:$?",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            combined = result.stdout + result.stderr
            assert "nc_exit:0" in combined, f"TCP connection to proxy failed: {combined}"
        finally:
            proxy_mod.stop_proxy(server)

    def test_request_is_proxied(
        self,
        no_wt_run: tuple[Callable[..., subprocess.CompletedProcess[str]], list[str]],
    ) -> None:
        """_run_container routes container requests through the proxy to the upstream API.

        Verifies the container→proxy channel: a correctly-tokened HTTP request
        reaches the proxy and receives an HTTP reply (forwarded upstream response
        or proxy error), confirming the full wiring is in place.

        Uses wget rather than nc: busybox nc closes the TCP connection immediately
        on stdin EOF (after the request is sent), so the proxy's response never
        arrives.  wget keeps the connection open until the response is complete.
        """
        run, _ = no_wt_run
        mutator = lambda headers: {k: v for k, v in headers.items()
                                   if k.lower() not in ("x-api-key", "authorization")} \
                                  | {"Authorization": "Bearer fake-real-key"}
        result = run(
            [
                "sh",
                "-c",
                'PORT=$(echo "$OPENAI_BASE_URL" | sed "s|.*:\\([0-9]*\\).*|\\1|"); '
                'wget -qO - -T 10 --header "Authorization: Bearer $OPENAI_API_KEY" '
                '"http://host.docker.internal:$PORT/v1/responses" 2>&1 || true',
            ],
            mutator=mutator,
            proxy_token="test-proxy-token",
        )
        assert result.returncode == 0, f"Container command failed:\n{result.stderr}"
        # An HTTP error line in the output confirms the proxy received and processed
        # the request (either upstream returned 4xx or proxy returned 502).
        assert "HTTP" in result.stdout, f"Proxy did not respond with HTTP:\n{result.stdout}"


# ---------------------------------------------------------------------------
# TestWorktreeMounts
# ---------------------------------------------------------------------------


class TestWorktreeMounts:
    """Every path in the full worktree mount layout has its declared RW/RO access
    when probed from inside the container."""

    def test_mount_access(
        self,
        wt_run: tuple[Callable[[list[str]], subprocess.CompletedProcess[str]], list[str]],
    ) -> None:
        run, mounts = wt_run
        _assert_mounts(run(["sh", "-c", _mount_access_script(mounts)]), mounts)


# ---------------------------------------------------------------------------
# TestGitInWorktree
# ---------------------------------------------------------------------------


class TestGitInWorktree:
    """Git read and write operations work correctly from inside the container."""

    def test_git_log_reads_history(
        self,
        wt_run: tuple[Callable[[list[str]], subprocess.CompletedProcess[str]], list[str]],
    ) -> None:
        """git log can read the repo's history from inside the container."""
        run, _ = wt_run
        # -c safe.directory=* bypasses the ownership check: tests skip --userns=keep-id
        # so the container runs as root while files are owned by the host UID.
        result = run(["git", "-c", "safe.directory=*", "-C", CONTAINER_WORKTREE, "log", "--oneline"])
        assert result.returncode == 0, f"git log failed:\n{result.stderr}"
        assert "init" in result.stdout

    def test_git_commit_visible_on_host(
        self,
        wt_run: tuple[Callable[[list[str]], subprocess.CompletedProcess[str]], list[str]],
        wt_repo: Path,
    ) -> None:
        """A commit written inside the container is visible in git log on the host.

        This is the core guarantee of the .git/objects:rw + refs/heads/hatchery:rw
        mount layout: objects written inside the container land in the real object
        store and the branch ref is updated, so the host sees them immediately.
        """
        run, _ = wt_run
        result = run(
            [
                "sh",
                "-c",
                f"git -c safe.directory='*' -C {CONTAINER_WORKTREE} commit --allow-empty -m host-visible-commit",
            ]
        )
        assert result.returncode == 0, f"git commit failed:\n{result.stderr}"

        # Verify the commit landed in the HOST's git repo — not just inside the container.
        host_log = subprocess.run(
            ["git", "log", "--oneline", "hatchery/test-wt"],
            cwd=wt_repo,
            capture_output=True,
            text=True,
        )
        assert host_log.returncode == 0, f"host git log failed:\n{host_log.stderr}"
        assert "host-visible-commit" in host_log.stdout, (
            f"commit not visible on host after container exit:\n{host_log.stdout}"
        )


# ---------------------------------------------------------------------------
# TestDinD
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="DinD image builds fail in hatchery sandbox: capset: Operation not permitted")
class TestDinD:
    """DinD (Docker-in-Docker / Podman-in-Podman): nested container execution works.

    Builds a DinD-capable image using ``docker.DIND_DOCKERFILE_LINES`` (the
    shared constant that also renders into Dockerfile.template), then runs
    nested Podman commands inside it with production DinD flags.
    """

    @pytest.fixture(scope="class")
    def dind_image(self, tmp_path_factory: pytest.TempPathFactory, runtime: docker.Runtime) -> str:
        """Build a DinD-capable image once per class; remove after.

        The Dockerfile is generated from the production template pipeline:
        ``_DOCKERFILE_TEMPLATE`` → substitute ``{{AGENT_INSTALL}}`` (empty,
        no agent needed) + ``{{DIND}}`` (uncommented, simulating a user who
        has enabled DinD).  This exercises the full template-to-image path.

        Test-env patches are appended: switch to root + add root subuid/subgid
        entries because our sandbox host lacks KILL in its bounding set, so
        crun can't start containers as non-root users.
        """
        build_dir = tmp_path_factory.mktemp("dind")
        # Generate the Dockerfile the same way production does
        text = docker._DOCKERFILE_TEMPLATE.read_text()
        text = text.replace("{{AGENT_INSTALL}}", "")
        text = text.replace("{{DIND}}", docker.DIND_DOCKERFILE_LINES)
        # Test-env patches: switch to root + add root subuid (our sandbox lacks KILL cap)
        text += "\nUSER root\n"
        text += "RUN printf 'root:1:65535\\n' >> /etc/subuid && printf 'root:1:65535\\n' >> /etc/subgid\n"
        (build_dir / "Dockerfile").write_text(text)

        # Verify _dind_dockerfile_ok passes on the generated+uncommented Dockerfile
        assert docker._dind_dockerfile_ok(build_dir, agent.CODEX) is False, (
            "_dind_dockerfile_ok should be False — Dockerfile is not at the agent-specific path"
        )
        # Write at the agent-specific path so _dind_dockerfile_ok can find it
        hatchery_dir = build_dir / ".hatchery"
        hatchery_dir.mkdir()
        (hatchery_dir / f"Dockerfile.{agent.CODEX.kind.lower()}").write_text(text)
        assert docker._dind_dockerfile_ok(build_dir, agent.CODEX) is True, (
            "_dind_dockerfile_ok should be True on an uncommented DinD Dockerfile"
        )
        image = "hatchery-test:dind"
        # When running inside a hatchery sandbox the bounding capability set
        # may not include all OCI defaults.  Pass --cap-drop/--cap-add so crun
        # only requests capabilities that are actually available.
        build_cmd = [runtime.binary, "build", "--cap-drop=ALL"]
        for cap in (
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
            "NET_ADMIN",
            "NET_RAW",
        ):
            build_cmd += [f"--cap-add={cap}"]
        build_cmd += ["-t", image, str(build_dir)]
        result = subprocess.run(build_cmd, capture_output=True, text=True)
        assert result.returncode == 0, f"DinD image build failed:\n{result.stderr}"
        yield image
        subprocess.run([runtime.binary, "rmi", "-f", image], capture_output=True)

    def _dind_run(
        self,
        dind_image: str,
        runtime: docker.Runtime,
        monkeypatch: pytest.MonkeyPatch,
        command: list[str],
    ) -> subprocess.CompletedProcess[str]:
        """Run *command* inside the DinD container with production flags."""
        monkeypatch.setattr(docker, "_userns_flags", lambda _r: [])
        result = docker._run_container(
            image=dind_image,
            mounts=[],
            workdir="/",
            hatchery_repo="/",
            name="test-dind",
            mutator=None,
            proxy_token=None,
            agent_cmd=[],
            dind=True,
            runtime=runtime,
            _command_override=command,
        )
        assert result is not None
        return result

    def test_podman_info(self, dind_image: str, runtime: docker.Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        result = self._dind_run(dind_image, runtime, monkeypatch, ["podman", "info"])
        assert result.returncode == 0, f"podman info failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        assert "host" in result.stdout

    def test_podman_pull(self, dind_image: str, runtime: docker.Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        result = self._dind_run(
            dind_image,
            runtime,
            monkeypatch,
            ["podman", "pull", "docker.io/library/alpine:latest"],
        )
        assert result.returncode == 0, f"podman pull failed:\nstdout={result.stdout}\nstderr={result.stderr}"

    def test_podman_run(self, dind_image: str, runtime: docker.Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        result = self._dind_run(
            dind_image,
            runtime,
            monkeypatch,
            ["podman", "run", "--rm", "docker.io/library/alpine", "echo", "hello-from-dind"],
        )
        assert result.returncode == 0, f"podman run failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        assert "hello-from-dind" in result.stdout

    def test_podman_build(self, dind_image: str, runtime: docker.Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        # --isolation=chroot avoids cgroup subtree_control writes.
        # --cap-drop/--cap-add restricts to caps in the bounding set so
        # buildah's capset() for the RUN step process succeeds.
        caps = "CHOWN DAC_OVERRIDE FOWNER MKNOD NET_ADMIN NET_RAW SETFCAP SETGID SETPCAP SETUID SYS_ADMIN SYS_CHROOT"
        cap_flags = " ".join(f"--cap-add={c}" for c in caps.split())
        result = self._dind_run(
            dind_image,
            runtime,
            monkeypatch,
            [
                "sh",
                "-c",
                f"printf 'FROM alpine\\nRUN echo built-ok\\n' | podman build "
                f"--isolation=chroot --cap-drop=ALL {cap_flags} "
                f"-t hatchery-test:inner -f - .",
            ],
        )
        assert result.returncode == 0, f"podman build failed:\nstdout={result.stdout}\nstderr={result.stderr}"

    def test_cap_net_admin_granted(
        self,
        no_wt_image: str,
        runtime: docker.Runtime,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DinD containers have CAP_NET_ADMIN (bit 12) and CAP_NET_RAW (bit 13) in CapEff."""
        monkeypatch.setattr(docker, "_userns_flags", lambda _r: [])
        result = docker._run_container(
            image=no_wt_image,
            mounts=[],
            workdir="/",
            hatchery_repo="/",
            name="test-cap",
            mutator=None,
            proxy_token=None,
            agent_cmd=[],
            dind=True,
            runtime=runtime,
            _command_override=["grep", "CapEff", "/proc/self/status"],
        )
        assert result is not None
        assert result.returncode == 0, f"grep CapEff failed:\n{result.stderr}"
        # Parse hex capability mask from "CapEff:\t00000000a80435fb" format
        for line in result.stdout.splitlines():
            if line.startswith("CapEff:"):
                cap_hex = line.split()[-1]
                cap_mask = int(cap_hex, 16)
                assert cap_mask & (1 << 12), f"CAP_NET_ADMIN (bit 12) not set in CapEff={cap_hex}"
                assert cap_mask & (1 << 13), f"CAP_NET_RAW (bit 13) not set in CapEff={cap_hex}"
                break
        else:
            pytest.fail(f"CapEff line not found in output: {result.stdout}")
