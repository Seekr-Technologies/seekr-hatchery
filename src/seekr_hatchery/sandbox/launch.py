"""Sandbox launch policy and task/shell coordination."""

import logging
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import seekr_hatchery.agents as agent
import seekr_hatchery.constants as constants
import seekr_hatchery.includes as includes
import seekr_hatchery.models as models
import seekr_hatchery.sandbox.config as sandbox_config
import seekr_hatchery.sandbox.images as sandbox_images
import seekr_hatchery.sandbox.mounts as sandbox_mounts
import seekr_hatchery.sandbox.runtime as sandbox_runtime
import seekr_hatchery.sandbox.scaffold as sandbox_scaffold
import seekr_hatchery.sandbox.spec as sandbox_spec
import seekr_hatchery.sandbox.volumes as sandbox_volumes
import seekr_hatchery.sidecars as sidecars
import seekr_hatchery.stream_interceptor.interceptors.clipboard_image as clipboard_image
import seekr_hatchery.ui as ui

# Keep diagnostic output and logger-level selection stable across relocation.
logger = logging.getLogger("seekr_hatchery.docker")


def launch_context(
    meta: models.SessionMeta,
    runtime: "sandbox_runtime.ContainerRuntime | None",
) -> tuple[sandbox_config.SandboxConfig | None, list[str], str]:
    """Return (config, features, container_workdir) for the launch path.

    Composes :func:`sandbox_config.load_config` + :func:`sandbox_config.features` with the
    container-workdir derivation that sessions.launch needs to set up the
    agent command. ``runtime=None`` → no docker (native mode); the workdir
    field is empty in that case and the caller doesn't pass it to the
    agent's command builder.

    Container paths mirror host paths so any path-keyed state — agent
    per-project directories under ``~/.<agent>/``, lockfiles with absolute
    paths, symlink targets that point outside the worktree — lands in the
    same location whether the session runs natively or in the sandbox.
    The container WORKDIR is therefore the host worktree path (worktree
    mode) or the host cwd (no-worktree mode).
    """
    if runtime is None:
        return None, [], ""
    config = sandbox_config.load_config(meta.hatchery_dir, local_hatchery_dir=meta.repo_path / ".hatchery")
    features = sandbox_config.features(config)
    sandbox_mounts.check_host_path_safe_for_mount(meta.repo_path)
    container_workdir = str(meta.worktree_path)
    return config, features, container_workdir


def remove_clipboard_dir(session_dir: Path) -> None:
    """Delete the per-task clipboard images directory (idempotent, swallows errors).

    Pasted screenshots are typically 100 KB – 5 MB; removing them when a task
    is marked complete or deleted keeps `~/.hatchery/tasks/` from growing
    unbounded. Resume flows (archive) intentionally do NOT call this — the
    files may be referenced by saved conversation history.
    """
    d = sandbox_mounts.clipboard_image_dir(session_dir)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def _make_paste_interceptor(
    backend: agent.AgentBackend,
    session_dir: Path,
    config: sandbox_config.SandboxConfig,
) -> clipboard_image.PasteInterceptor | None:
    """Build the paste interceptor for an agent launch, or ``None`` when disabled."""
    if not config.clipboard_images:
        return None
    return clipboard_image.PasteInterceptor(
        sandbox_mounts.clipboard_image_dir(session_dir),
        backend.format_image_reference,
    )


def _prepare_sidecars(
    backend: agent.AgentBackend,
    config: sandbox_config.SandboxConfig,
    session_dir: Path,
    *,
    proxy_token: str,
    kubectl_proxy_token: str | None,
) -> list[sidecars.SandboxSidecar]:
    """Construct and validate the sidecars for one container launch."""
    try:
        endpoints = backend.proxy_endpoints()
        active_sidecars: list[sidecars.SandboxSidecar] = [
            *(sidecars.ApiProxySidecar(endpoint, proxy_token, backend) for endpoint in endpoints),
            sidecars.S3Sidecar(config.s3, session_dir),
            sidecars.OciSidecar(config.oci, session_dir),
            sidecars.KubectlSidecar(config.kubernetes, session_dir, kubectl_proxy_token),
        ]
        sidecars.validate_sidecars(active_sidecars)
    except RuntimeError as exc:
        ui.error(str(exc))
        sys.exit(1)
    return active_sidecars


def run_session(
    meta: models.SessionMeta,
    backend: agent.AgentBackend,
    agent_cmd: list[str],
    config: sandbox_config.SandboxConfig,
    *,
    proxy_token: str,
    kubectl_proxy_token: str | None = None,
    runtime: sandbox_runtime.ContainerRuntime | None = None,
    no_cache: bool = False,
    include_entries: list[includes.IncludeEntry] | None = None,
) -> None:
    """Replace the current process with a containerised agent session.

    Branches on ``meta.no_worktree``: worktree mode pre-seeds git sentinel
    files, then mounts the worktree at its host path; no-worktree mode
    mounts the cwd at its host path and skips all git metadata. Container
    paths mirror host paths so every path-keyed thing inside the container
    (agent state dirs, lockfiles, symlinks) sees the same absolute paths a
    native run would.

    *agent_cmd* must be the full command already built for Docker mode
    (``backend.build_*_command(docker=True, workdir=...)``). Image is
    built before launch; the layer cache makes this near-instant when
    nothing changed.

    *include_entries* — additional paths to mount at /includes/<basename>/.

    Identifiers like ``image_name``, ``container_name`` and ``session_dir``
    are read from *meta* (the models.SessionMeta properties resolve them via a
    function-level import of sessions, which is safe at call time even
    though launch doesn't import sessions at module load). Tokens are
    explicit kwargs because they involve filesystem side-effects (writing
    a stable per-session secret) that the caller — sessions.launch —
    owns.
    """
    runtime = runtime or sandbox_runtime.DockerRuntime()
    session_dir = meta.session_dir
    session_dir.mkdir(parents=True, exist_ok=True)

    sandbox_mounts.check_host_path_safe_for_mount(meta.repo_path)

    git_sentinels: list[tuple[Path, str]] | None = None
    if meta.no_worktree:
        container_workdir = str(meta.worktree_path)
        container_repo = str(meta.worktree_path)
        build_root = meta.hatchery_dir
    else:
        # Pre-seed writable sentinel files for git's .git/-root writes.
        git_sentinels = []
        for fname in ("COMMIT_EDITMSG", "ORIG_HEAD"):
            if not (meta.repo_path / ".git" / fname).exists():
                continue
            p = session_dir / fname
            if not p.exists():
                p.touch()
            git_sentinels.append((p, fname))

        # The worktree's .git pointer on the host already reads
        # `gitdir: <host_repo>/.git/worktrees/<name>`, which is also the
        # container path under host-path mirroring. No rewrite needed.

        container_workdir = str(meta.worktree_path)
        container_repo = str(meta.repo_path)
        build_root = meta.hatchery_dir

    active_sidecars = _prepare_sidecars(
        backend,
        config,
        session_dir,
        proxy_token=proxy_token,
        kubectl_proxy_token=kubectl_proxy_token,
    )

    if config.dind and not sandbox_scaffold.supports_dind(build_root, backend):
        ui.warn("dind: true is set but the Dockerfile doesn't appear to install Podman.")
        ui.info(
            f"  Uncomment the '── Podman-in-Podman (DinD)' block in {sandbox_scaffold.dockerfile_path(build_root, backend).name}."
        )

    backend.on_before_container_start(session_dir, proxy_token, container_workdir)

    sandbox_images.build_image(meta.repo_path, build_root, meta.image_name, backend, runtime=runtime, no_cache=no_cache)
    mounts = sandbox_mounts.build_mounts(
        meta,
        backend,
        session_dir,
        config,
        git_sentinel_files=git_sentinels,
        include_entries=include_entries,
    )

    # Materialise per-task seeded VolumeMounts: resolve names, ensure
    # the runtime volumes exist, seed on first launch. Bind and tmpfs
    # mounts pass through unchanged.
    mounts = sandbox_volumes.prepare_volume_mounts(
        runtime.binary,
        mounts,
        meta,
        session_dir,
        proxy_token,
        container_workdir,
    )

    mode_label = "no-worktree mode" if meta.no_worktree else "worktree mode"
    logger.debug(f"Launching {runtime.binary} container for session '{meta.name}' ({mode_label})")
    with sidecars.run_sidecars(active_sidecars) as contrib:
        mounts.extend(contrib.mounts)
        spec = sandbox_spec.build_spec(
            image=meta.image_name,
            mounts=mounts,
            workdir=container_workdir,
            name=meta.name,
            hatchery_repo=container_repo,
            container_name=meta.container_name,
            agent_cmd=agent_cmd,
            user_env=sandbox_config.resolve_environment(config),
            extra_env=contrib.env,
            needs_host_gateway=contrib.needs_host_gateway,
            dind=config.dind,
            cap_add=config.cap_add,
        )
        runtime.run(spec, paste_interceptor=_make_paste_interceptor(backend, session_dir, config))


def launch_shell(
    repo: Path,
    backend: agent.AgentBackend,
    config: sandbox_config.SandboxConfig,
    runtime: sandbox_runtime.ContainerRuntime,
    image_name: str,
    *,
    kubectl_proxy_token: str = "",
    shell: str = "/bin/bash",
    no_cache: bool = False,
    hatchery_dir: Path | None = None,
) -> None:
    """Drop the user into an interactive shell inside the sandbox container.

    Builds the same image agents use and starts its configured sidecars so
    their capabilities can be inspected interactively. The repo is mounted RW
    at its host path (so the sandbox shell sees the same paths a native shell
    would).

    *hatchery_dir* is the directory holding the Dockerfile
    — defaults to *repo* / ``.hatchery`` (committed mode). In not-committed
    mode the caller passes the out-of-tree store so the image builds from the
    store's Dockerfile, not the repo root.

    Caller resolves *image_name* — typically ``sessions.image_name(repo, "sandbox")``.
    """
    if hatchery_dir is None:
        hatchery_dir = repo / ".hatchery"
    sandbox_mounts.check_host_path_safe_for_mount(repo)

    # Use a short-lived session dir under ~/.hatchery/ for generated sidecar
    # files. tempfile.TemporaryDirectory() is not reliable on macOS because
    # /var/folders is outside Podman Machine's default shared roots.
    sandbox_session_dir = constants.HATCHERY_DIR / "sandbox-sessions" / str(uuid.uuid4())
    sandbox_session_dir.mkdir(parents=True, exist_ok=True)
    try:
        active_sidecars = _prepare_sidecars(
            backend,
            config,
            sandbox_session_dir,
            proxy_token=str(uuid.uuid4()),
            kubectl_proxy_token=kubectl_proxy_token or None,
        )
        sandbox_images.build_image(repo, hatchery_dir, image_name, backend, runtime=runtime, no_cache=no_cache)
        mounts = sandbox_mounts.build_shell_mounts(repo, config)
        with sidecars.run_sidecars(active_sidecars) as contrib:
            mounts = list(mounts) + contrib.mounts
            spec = sandbox_spec.build_spec(
                image=image_name,
                mounts=mounts,
                workdir=str(repo),
                name="sandbox",
                hatchery_repo=str(repo),
                container_name=None,
                agent_cmd=[],
                user_env=sandbox_config.resolve_environment(config),
                extra_env=contrib.env,
                needs_host_gateway=contrib.needs_host_gateway,
                command_override=[shell],
                interactive=True,
            )
            runtime.run(spec)
    finally:
        shutil.rmtree(sandbox_session_dir, ignore_errors=True)


def exec_task_shell(container_name: str, runtime: sandbox_runtime.ContainerRuntime, shell: str = "/bin/bash") -> None:
    """Exec an interactive shell into the running container *container_name*.

    Caller resolves *container_name* — typically ``sessions.container_name(repo, name)``.
    The container name is deterministic (it's the same value assigned at launch)
    so no ``docker ps`` lookup is needed. Docker/Podman returns a clear error
    if the container is not running.
    """
    subprocess.run([runtime.binary, "exec", "-it", container_name, shell])


def resolve_runtime(
    hatchery_dir: Path, no_docker: bool, backend: agent.AgentBackend = agent.CODEX
) -> sandbox_runtime.ContainerRuntime | None:
    """Return the runtime to use for this session, or None to run natively.

    Returns None only when --no-docker is explicitly set.  When Docker mode is
    active (the default), a Dockerfile **must** exist — if none is found the
    function prints an error and exits so the user is never silently placed in
    an unsandboxed environment.

    *hatchery_dir* is the directory holding the Dockerfile
    — either the worktree's ``.hatchery`` (commit mode), the repo's ``.hatchery``
    (no-worktree), or the out-of-tree store (no-commit mode). Use
    ``models.SessionMeta.hatchery_dir`` to get the right one.

    Checks for the agent-specific Dockerfile (e.g. ``Dockerfile.codex``).
    """
    if no_docker:
        logger.debug("--no-docker set, running natively")
        return None
    agent_df = sandbox_scaffold.dockerfile_path(hatchery_dir, backend)
    if not agent_df.exists():
        ui.error(
            f"No Dockerfile found for '{backend.kind.lower()}' in {hatchery_dir}.\n"
            "A Dockerfile is required for sandbox mode. "
            "Run `hatchery new` to create one, or pass --no-docker to run without a sandbox."
        )
        sys.exit(1)
    logger.debug("Dockerfile found, detecting container runtime")
    return sandbox_runtime.detect_runtime()
