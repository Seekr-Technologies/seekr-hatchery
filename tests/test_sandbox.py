"""Sandbox integration tests — verify container behaviour from the inside.

Skipped by default; opt in with:
    uv run pytest tests/test_sandbox.py --integration -v
"""

import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

import seekr_hatchery.agent as agent
import seekr_hatchery.docker as docker
import seekr_hatchery.proxy as proxy_mod
import seekr_hatchery.tasks as tasks

pytestmark = pytest.mark.integration

CONTAINER_WORKTREE = f"{tasks.CONTAINER_REPO_ROOT}/.hatchery/worktrees/test-wt"


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
    for img in ("docker.io/library/alpine:latest", "docker.io/alpine/git:latest"):
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
    (hatchery_dir / "Dockerfile.claude").write_text("FROM alpine\n")
    (hatchery_dir / "docker.yaml").write_text("schema_version: 1\n")
    return cwd


@pytest.fixture(scope="module")
def no_wt_image(no_wt_cwd: Path, runtime: docker.Runtime) -> str:
    """Build the no-worktree sandbox image once; remove it after the module."""
    docker.build_docker_image(no_wt_cwd, no_wt_cwd, "test-no-wt", agent.CLAUDE, runtime=runtime)
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
    agent.CLAUDE.on_new_task(session_dir)
    mounts = docker.docker_mounts_no_worktree(no_wt_cwd, agent.CLAUDE, session_dir, docker.DockerConfig())

    def run(
        command: list[str],
        *,
        api_key: str | None = None,
        proxy_token: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = docker._run_container(
            image=no_wt_image,
            mounts=mounts,
            workdir="/workspace",
            hatchery_repo="/workspace",
            name="test-no-wt",
            api_key=api_key,
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
    (hatchery_dir / "Dockerfile.claude").write_text(
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
    docker.build_docker_image(wt_repo, wt_worktree, "test-wt", agent.CLAUDE, runtime=runtime)
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
    agent.CLAUDE.on_new_task(session_dir)

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
        agent.CLAUDE,
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
            api_key=None,
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


class TestContainerEnv:
    """Container injects HATCHERY_TASK, HATCHERY_REPO, sets workdir correctly,
    and shadows ~/.claude/backups with a tmpfs."""

    def test_env_and_workdir(
        self,
        no_wt_run: tuple[Callable[..., subprocess.CompletedProcess[str]], list[str]],
    ) -> None:
        run, _ = no_wt_run
        script = (
            'printf "TASK=%s\\n" "$HATCHERY_TASK"; '
            'printf "REPO=%s\\n" "$HATCHERY_REPO"; '
            'printf "WD=%s\\n" "$(pwd)"; '
            'grep -q "backups" /proc/mounts && printf "BACKUPS=tmpfs\\n" || printf "BACKUPS=missing\\n"'
        )
        result = run(["sh", "-c", script])
        assert result.returncode == 0, f"Script failed:\n{result.stderr}"
        assert "TASK=test-no-wt" in result.stdout
        assert "REPO=/workspace" in result.stdout
        assert "WD=/workspace" in result.stdout
        assert "BACKUPS=tmpfs" in result.stdout


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
        add_host = "--add-host=host.docker.internal:host-gateway"
        try:
            result = subprocess.run(
                [
                    runtime.binary,
                    "run",
                    "--rm",
                    add_host,
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

        The response containing Anthropic CDN headers confirms the full path:
        container → proxy (validates token, injects real key) → api.anthropic.com.
        """
        run, _ = no_wt_run
        result = run(
            [
                "sh",
                "-c",
                'PORT=$(echo "$ANTHROPIC_BASE_URL" | sed "s/.*://"); '
                'printf "GET /v1/messages HTTP/1.0\r\nx-api-key: $ANTHROPIC_API_KEY\r\nContent-Length: 0\r\n\r\n"'
                ' | nc -w5 host.docker.internal "$PORT"',
            ],
            api_key="fake-real-key",
            proxy_token="test-proxy-token",
        )
        assert result.returncode == 0, f"Proxy request failed:\n{result.stderr}"
        # Anthropic CDN headers confirm the request reached the upstream, not just the proxy.
        assert "cloudflare" in result.stdout, f"Expected upstream response, got:\n{result.stdout}"


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


class TestDinD:
    """DinD (Docker-in-Docker / Podman-in-Podman): nested container execution works.

    The full test should:
    1. Build a DinD-capable image using the exact Dockerfile section from the
       hatchery template (the {{DIND}} block rendered by docker.py).
    2. Run ``podman run --rm <dind-image> podman run alpine echo hello-from-dind``
       via ``_run_container`` with the production DinD flags.
    3. Assert the inner container exits 0 and its output contains "hello-from-dind".

    This exercises both that our ``podman run`` args are sufficient for DinD AND
    that the Dockerfile template section is correct — end-to-end, not just flag
    presence.

    SKIPPED: all ``RUN`` instructions in Dockerfile builds currently fail inside
    the hatchery sandbox with ``capset: Operation not permitted``.  ``crun``
    calls ``capset()`` during container setup before the user command runs, and
    the outer container's bounding capability set does not include the required
    capabilities.  Once the sandbox is updated to grant the missing caps (or the
    Dockerfile template is restructured to avoid ``RUN`` for the DinD layer),
    this skip can be removed.  See the "Known limitation" note in the task ADR.
    """

    @pytest.mark.skip(
        reason=(
            "Blocked until DinD image builds work inside the sandbox. "
            "All RUN instructions fail with 'capset: Operation not permitted' — "
            "the outer container's bounding capability set is too restrictive for crun."
        )
    )
    def test_nested_container_runs(self, runtime: docker.Runtime) -> None:
        """A container launched with production DinD flags can spawn a nested container."""
        pytest.fail("implement once DinD image builds are unblocked")
