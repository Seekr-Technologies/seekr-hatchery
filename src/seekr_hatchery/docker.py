"""Docker sandbox helpers."""

import logging
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections import deque
from collections.abc import Callable, Generator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Literal

import click
import yaml
from pydantic import BaseModel, ConfigDict, field_validator

import seekr_hatchery.agents as agent
import seekr_hatchery.kubectl_proxy as _kubectl_proxy
import seekr_hatchery.proxy as proxy
import seekr_hatchery.tasks as tasks
import seekr_hatchery.ui as ui
from seekr_hatchery.kubectl_proxy import KubectlConfig

logger = logging.getLogger("hatchery")


class Runtime(Enum):
    PODMAN = "PODMAN"
    DOCKER = "DOCKER"

    @property
    def binary(self) -> str:
        """CLI binary name for this runtime."""
        return self.value.lower()


_RESOURCES = Path(__file__).parent / "resources"
_DOCKERFILE_TEMPLATE = _RESOURCES / "Dockerfile.template"
_DOCKER_CONFIG_TEMPLATE = _RESOURCES / "docker.yaml.template"
_SECCOMP = _RESOURCES / "seccomp.json"

# The uncommented DinD Dockerfile instructions — single source of truth for the
# Podman-in-Podman section rendered into Dockerfile.template via {{DIND}}.
DIND_DOCKERFILE_LINES = """\
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \\
    podman podman-docker sudo fuse-overlayfs uidmap \\
    && rm -rf /var/lib/apt/lists/*
RUN printf 'hatchery:1001:64535\\n' > /etc/subuid \\
    && printf 'hatchery:1001:64535\\n' > /etc/subgid
RUN mkdir -p /root/.config/containers \\
    && printf '[storage]\\ndriver = "overlay"\\n\\n[storage.options.overlay]\\nmount_program = "/usr/bin/fuse-overlayfs"\\n' \\
        > /root/.config/containers/storage.conf \\
    && printf '[containers]\\nnetns = "host"\\ncgroups = "disabled"\\ndefault_sysctls = []\\n[engine]\\ncgroup_manager = "cgroupfs"\\nevents_logger = "file"\\n' \\
        > /root/.config/containers/containers.conf
RUN echo 'hatchery ALL=(root) NOPASSWD: /usr/bin/podman' \\
        > /etc/sudoers.d/hatchery-podman \\
    && chmod 440 /etc/sudoers.d/hatchery-podman
RUN printf '#!/bin/sh\\nexec sudo -n /usr/bin/podman "$@"\\n' \\
        > /usr/local/bin/podman \\
    && chmod +x /usr/local/bin/podman
USER hatchery"""


def _comment_out(text: str) -> str:
    """Prefix every line of *text* with ``# ``."""
    return "\n".join(f"# {line}" for line in text.splitlines())


# ── Config model ──────────────────────────────────────────────────────────────

# All Linux capabilities from capabilities(7).
_VALID_CAPS: frozenset[str] = frozenset(
    {
        "AUDIT_CONTROL",
        "AUDIT_READ",
        "AUDIT_WRITE",
        "BLOCK_SUSPEND",
        "BPF",
        "CHECKPOINT_RESTORE",
        "CHOWN",
        "DAC_OVERRIDE",
        "DAC_READ_SEARCH",
        "FOWNER",
        "FSETID",
        "IPC_LOCK",
        "IPC_OWNER",
        "KILL",
        "LEASE",
        "LINUX_IMMUTABLE",
        "MAC_ADMIN",
        "MAC_OVERRIDE",
        "MKNOD",
        "NET_ADMIN",
        "NET_BIND_SERVICE",
        "NET_BROADCAST",
        "NET_RAW",
        "PERFMON",
        "SETFCAP",
        "SETGID",
        "SETPCAP",
        "SETUID",
        "SYS_ADMIN",
        "SYS_BOOT",
        "SYS_CHROOT",
        "SYS_MODULE",
        "SYS_NICE",
        "SYS_PACCT",
        "SYS_PTRACE",
        "SYS_RAWIO",
        "SYS_RESOURCE",
        "SYS_TIME",
        "SYS_TTY_CONFIG",
        "SYSLOG",
        "WAKE_ALARM",
    }
)


class DockerConfig(BaseModel):
    """Schema for .hatchery/docker.yaml."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1"] = "1"
    mounts: list[str] = []
    dind: bool = False
    cap_add: list[str] = []
    kubernetes: KubectlConfig | None = None

    @field_validator("cap_add", mode="before")
    @classmethod
    def validate_cap_add(cls, v: list | None) -> list[str]:
        if v is None:
            return []
        result: list[str] = []
        for i, entry in enumerate(v):
            if not isinstance(entry, str):
                raise ValueError(f"cap_add[{i}]: expected a string, got {type(entry).__name__!r}")
            cap = entry.upper()
            if cap not in _VALID_CAPS:
                raise ValueError(f"cap_add[{i}]: unknown capability {entry!r}")
            result.append(cap)
        return result

    @field_validator("mounts", mode="before")
    @classmethod
    def validate_mounts(cls, v: list | None) -> list:
        if v is None:
            return []
        for i, entry in enumerate(v):
            if not isinstance(entry, str):
                raise ValueError(f"mounts[{i}]: expected a string, got {type(entry).__name__!r}")
            parts = entry.split(":", 2)
            if len(parts) < 2 or not parts[0] or not parts[1]:
                raise ValueError(
                    f'mounts[{i}]: invalid mount {entry!r} — expected "host:container" or "host:container:mode"'
                )
            if len(parts) == 3 and parts[2] not in ("ro", "rw"):
                raise ValueError(f'mounts[{i}]: invalid mode {parts[2]!r} in {entry!r} — must be "ro" or "rw"')
        return v


# ── Authentication ────────────────────────────────────────────────────────────


def get_or_create_proxy_token(repo: Path, name: str) -> str:
    """Return the stable proxy token for this task, creating it on first call.

    The token is persisted in the session directory so it stays constant
    across container restarts.  A stable token means the agent's cached
    credential in the per-task config directory continues to match the
    API key env var on subsequent launches — no repeated dialogs.
    """
    session_dir = tasks.task_session_dir(repo, name)
    session_dir.mkdir(parents=True, exist_ok=True)
    token_file = session_dir / "proxy_token"
    if token_file.exists():
        token = token_file.read_text().strip()
        logger.debug("Reusing proxy token for task %r", name)
        return token
    token = str(uuid.uuid4())
    token_file.write_text(token)
    logger.debug("Created proxy token for task %r", name)
    return token


# ── kubectl helpers ───────────────────────────────────────────────────────────


def _get_or_create_kubectl_token(session_dir: Path) -> str:
    """Return the stable kubectl RBAC proxy token, creating it on first call."""
    token_file = session_dir / "kubectl_proxy_token"
    if token_file.exists():
        return token_file.read_text().strip()
    token = str(uuid.uuid4())
    token_file.write_text(token)
    return token


@contextmanager
def _kubectl_context(
    config: DockerConfig,
    session_dir: Path,
) -> Generator[list[str], None, None]:
    """Context manager that starts the kubectl proxy chain and yields extra mounts.

    Yields an empty list when ``config.kubernetes`` is ``None``.  On exit
    (normal or exceptional) the RBAC proxy and kubectl proxy subprocess are
    stopped.
    """
    if config.kubernetes is None:
        yield []
        return

    kubectl_proxy_token = _get_or_create_kubectl_token(session_dir)

    # Start kubectl proxy subprocess (uses host kubeconfig).
    kubectl_proc, kube_port = _kubectl_proxy.start_kubectl_proxy_proc(context=config.kubernetes.context)

    # Start RBAC filtering proxy in front of it (TLS; returns cert_pem for kubeconfig).
    rbac_server, rbac_port, ca_cert_pem = _kubectl_proxy.start_rbac_proxy(
        config.kubernetes.rules, kubectl_proxy_token, kube_port
    )

    # Write a kubeconfig pointing to the RBAC proxy (HTTPS with pinned cert).
    kubeconfig_path = session_dir / "kubeconfig"
    kubeconfig_path.write_text(_kubectl_proxy.make_kubeconfig(rbac_port, kubectl_proxy_token, ca_cert_pem))
    kubeconfig_path.chmod(0o600)

    try:
        yield [f"{kubeconfig_path}:{agent.CONTAINER_HOME}/.kube/config:ro"]
    finally:
        _kubectl_proxy.stop_rbac_proxy(rbac_server)
        _kubectl_proxy.stop_kubectl_proxy_proc(kubectl_proc)


# ── Utilities ─────────────────────────────────────────────────────────────────


def dockerfile_path(base: Path, backend: agent.AgentBackend) -> Path:
    """Return the canonical path to this backend's Dockerfile under *base*.

    Files are named ``Dockerfile.<agent>`` (e.g. ``Dockerfile.codex``).
    """
    return base / ".hatchery" / f"Dockerfile.{backend.kind.lower()}"


def docker_image_name(repo: Path, name: str) -> str:
    """Return the Docker image tag for a given repo and task name."""
    return f"hatchery/{tasks.to_name(repo.name)}:{name}"


def task_container_name(repo: Path, name: str) -> str:
    """Return the deterministic container name for a task.

    Uses repo_id (basename + path hash) rather than bare basename to avoid
    collisions between repos with the same directory name at different paths.
    """
    return f"hatchery-{tasks.repo_id(repo)}-{name}"


def docker_available() -> bool:
    """Return True if the Docker daemon is reachable."""
    logger.debug("Checking Docker availability")
    try:
        result = tasks.run(["docker", "info"], check=False)
    except FileNotFoundError:
        return False
    return result.returncode == 0


def podman_available() -> bool:
    """Return True if the Podman CLI is reachable."""
    logger.debug("Checking Podman availability")
    try:
        result = tasks.run(["podman", "info"], check=False)
    except FileNotFoundError:
        return False
    return result.returncode == 0


def detect_runtime() -> Runtime:
    """Return the preferred container runtime, or exit if none is available.

    Podman is preferred because it is rootless-native and its default seccomp
    profile allows the user-namespace syscalls needed for nested containers.

    If the Podman binary is installed but 'podman info' fails (e.g. the machine
    is not running on macOS), this is treated as an error rather than a silent
    fallback to Docker — the user installed Podman intentionally and should not
    be silently downgraded to the less-secure Docker runtime.
    """
    if podman_available():
        logger.debug("Using Podman as container runtime")
        return Runtime.PODMAN
    if shutil.which("podman") is not None:
        msg = "Podman is installed but not running."
        if sys.platform == "darwin":
            msg += " Start it with: podman machine start"
            msg += "\n(or: podman machine init && podman machine start on first use)"
        ui.error(msg)
        sys.exit(1)
    if docker_available():
        logger.debug("Using Docker as container runtime")
        return Runtime.DOCKER
    ui.error(".hatchery/dockerfile exists but neither Podman nor Docker is running.")
    ui.info("Start Podman/Docker or pass --no-docker to run without the sandbox.")
    sys.exit(1)


# ── Setup ─────────────────────────────────────────────────────────────────────


def ensure_dockerfile(
    repo: Path,
    backend: agent.AgentBackend = agent.CODEX,
    *,
    source: Path | None = None,
) -> bool:
    """Write a starter Dockerfile if none exists. Returns True if created.

    If *source* is given and the Dockerfile exists there but not in *repo*,
    the file is copied from *source* instead of generated from the template.
    Returns False in that case so callers do not auto-commit a file the user
    intentionally left uncommitted.
    """
    df = dockerfile_path(repo, backend)
    if df.exists():
        return False
    if source is not None:
        source_df = dockerfile_path(source, backend)
        if source_df.exists():
            shutil.copy2(source_df, df)
            ui.warn(
                f"  Copied {df.relative_to(repo)} from repo root "
                "(uncommitted — will not be committed to this worktree branch)"
            )
            return False
    df.parent.mkdir(parents=True, exist_ok=True)
    text = _DOCKERFILE_TEMPLATE.read_text()
    text = text.replace("{{AGENT_INSTALL}}", backend.dockerfile_install)
    text = text.replace("{{DIND}}", _comment_out(DIND_DOCKERFILE_LINES))
    df.write_text(text)
    ui.info(f"  Created {df.relative_to(repo)}")
    answer = input("  Would you like to edit the Dockerfile? [Y/n] ").strip().lower()
    if answer != "n":
        tasks.open_for_editing(df)
    return True


def _migrate_docker_config(data: dict) -> dict:
    """Bring a docker.yaml config dict up to the current schema version.

    Add a new `if v == N` block here whenever the schema changes.
    Each block should make the minimal edit to reach version N+1,
    then increment data["schema_version"]. The final state will
    always be tasks.DOCKER_CONFIG_SCHEMA_VERSION.
    """
    v = str(data.get("schema_version", "0"))

    # "0" → "1": initial versioned schema (just stamps the version)
    if v == "0":
        v = "1"

    # Always write back as string to normalise legacy int values (e.g. schema_version: 1 from YAML)
    data["schema_version"] = v

    # Future example:
    # if v == 1:
    #     data["new_field"] = data.pop("old_field", None)
    #     data["schema_version"] = 2
    #     v = 2

    return data


def ensure_docker_config(repo: Path, *, source: Path | None = None) -> bool:
    """Write .hatchery/docker.yaml from template if it does not already exist.

    Returns True if the file was created, False if it already existed.

    If *source* is given and docker.yaml exists there but not in *repo*,
    the file is copied from *source* instead of generated from the template.
    Returns False in that case so callers do not auto-commit a file the user
    intentionally left uncommitted.
    """
    config_file = repo / tasks.DOCKER_CONFIG
    if config_file.exists():
        return False
    if source is not None:
        source_config = source / tasks.DOCKER_CONFIG
        if source_config.exists():
            shutil.copy2(source_config, config_file)
            ui.warn(
                f"  Copied {tasks.DOCKER_CONFIG} from repo root "
                "(uncommitted — will not be committed to this worktree branch)"
            )
            return False
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(_DOCKER_CONFIG_TEMPLATE.read_text())
    ui.info(f"  Created {tasks.DOCKER_CONFIG}")
    answer = input("  Would you like to edit the docker config? [Y/n] ").strip().lower()
    if answer != "n":
        tasks.open_for_editing(config_file)
    return True


# ── DinD helpers ──────────────────────────────────────────────────────────────


def _dind_dockerfile_ok(worktree: Path, backend: agent.AgentBackend) -> bool:
    """Return True if the Dockerfile has an uncommented fuse-overlayfs reference.

    fuse-overlayfs is the storage driver required for rootless nested containers
    and is a reliable smoke-test that the DinD section has been uncommented.
    """
    df = dockerfile_path(worktree, backend)
    if not df.exists():
        return False
    for line in df.read_text().splitlines():
        stripped = line.strip()
        if not stripped.startswith("#") and "fuse-overlayfs" in stripped:
            return True
    return False


def docker_features(config: DockerConfig) -> list[str]:
    """Return the list of enabled optional features from a loaded DockerConfig."""
    features = []
    if config.dind:
        features.append("DinD")
    if config.kubernetes is not None:
        features.append("kubectl")
    return features


# ── Mount construction ────────────────────────────────────────────────────────


def load_docker_config(root: Path) -> DockerConfig:
    """Read and parse root/docker.yaml into a DockerConfig.

    Returns an empty DockerConfig if the file does not exist. Exits with an
    error message if the file cannot be parsed or fails validation — an
    invalid config is always a user mistake that must be fixed before launching.
    """
    config_file = root / tasks.DOCKER_CONFIG
    if not config_file.exists():
        return DockerConfig()
    try:
        raw = yaml.safe_load(config_file.read_text()) or {}
        raw = _migrate_docker_config(raw)
        return DockerConfig.model_validate(raw)
    except Exception as exc:
        ui.error(f"invalid {tasks.DOCKER_CONFIG}: {exc}")
        sys.exit(1)


def _construct_docker_mounts(config: DockerConfig) -> list[str]:
    """Resolve a DockerConfig into docker -v mount strings.

    Expands ~ in host paths and silently skips entries whose host path does
    not exist on this machine (allows a shared config to list paths that are
    only present on some developers' machines).
    """
    result = []
    for entry in config.mounts:
        parts = entry.split(":", 2)
        host = Path(parts[0]).expanduser()
        container = parts[1]
        mode = parts[2] if len(parts) == 3 else "ro"
        if not host.exists():
            logger.debug("Custom mount host path does not exist, skipping: %s", host)
            continue
        result.append(f"{host}:{container}:{mode}")
    return result


def docker_mounts(
    repo: Path,
    worktree: Path,
    name: str,
    backend: agent.AgentBackend,
    session_dir: Path,
    config: DockerConfig,
    git_sentinel_files: list[tuple[Path, str]] | None = None,
    worktree_git_ptr: Path | None = None,
) -> list[str]:
    """Return the -v flags for the container.
    Mount layout:
      /repo                                       ← full repo + .git, read-only
      /repo/.git                                  ← read-write (allows lock files at .git/ root)
      /repo/.git/objects                          ← read-write (new commit objects)
      /repo/.git/refs/heads/hatchery/                  ← read-write (own branch + lock sidecar files)
      /repo/.git/logs                             ← read-write (reflogs, if dir exists)
      /repo/.git/worktrees/<n>                    ← read-write (this task's index + HEAD only)
      /repo/.git/COMMIT_EDITMSG                   ← read-write (per-task sentinel file)
      /repo/.git/ORIG_HEAD                        ← read-write (per-task sentinel file)
      /repo/.hatchery/worktrees/<n>                ← read-write (the ONLY place edits land)
      /repo/.hatchery/worktrees/<n>/.git          ← container-path-aware .git pointer (file)
      {CONTAINER_HOME}/...  ← agent-specific home mounts (see backend.home_mounts())

    Known security holes (accepted; fixing them would break real-time git visibility):
      LOW-MEDIUM: The entire refs/heads/hatchery/ dir is mounted rw, so the container
        can create arbitrary hatchery/<anything> branch refs, not just update its own.
        These would persist to the host repo. Mitigation: only the agent runs in the
        container, so this requires deliberately malicious behaviour from the agent.
      MEDIUM: .git/ root is mounted rw, so the container can modify files at the
        .git/ root level: config, packed-refs, FETCH_HEAD, MERGE_HEAD, description,
        info/, etc. This enables modifying refs for any branch, not just hatchery/.
        Required so that git can create packed-refs.lock during rebase/cherry-pick/merge.
      HIGH: .git/objects/ is mounted rw against the real object store. The container
        can delete or overwrite existing object files, corrupting the entire git
        history and breaking the repo for all branches. Fixing this requires a
        staging temp dir + post-exit copy-back, which means commits made inside the
        container are not visible via `git log` on the host until the session ends.
    """
    git_dir = repo / ".git"
    worktree_rel = worktree.relative_to(repo)
    container_worktree = f"{tasks.CONTAINER_REPO_ROOT}/{worktree_rel}"
    mounts = [
        f"{repo}:{tasks.CONTAINER_REPO_ROOT}:ro",  # .git ro via parent; overridden below
        f"{git_dir}:{tasks.CONTAINER_REPO_ROOT}/.git:rw",  # unlock .git/ root for lock files
        f"{git_dir / 'objects'}:{tasks.CONTAINER_REPO_ROOT}/.git/objects:rw",
    ]

    # Mount the entire task/ ref directory rw so git can create .lock sidecar
    # files alongside the branch ref during commits.  refs/heads/ itself is
    # read-only via the parent repo mount, so main/develop/etc. stay protected.
    hatchery_refs_dir = git_dir / "refs" / "heads" / "hatchery"
    if hatchery_refs_dir.exists():
        mounts.append(f"{hatchery_refs_dir}:{tasks.CONTAINER_REPO_ROOT}/.git/refs/heads/hatchery:rw")

    logs_dir = git_dir / "logs"
    if logs_dir.exists():
        mounts.append(f"{logs_dir}:{tasks.CONTAINER_REPO_ROOT}/.git/logs:rw")

    # Only this task's worktree git metadata is writable; other worktrees' metadata
    # is protected by the ro parent mount (prevents `git worktree prune` damage).
    worktree_meta = git_dir / "worktrees" / name
    if worktree_meta.exists():
        mounts.append(f"{worktree_meta}:{tasks.CONTAINER_REPO_ROOT}/.git/worktrees/{name}:rw")

    # git writes these into .git/ root during normal commits; use per-task sentinel
    # files so .git/ root stays ro.
    for host_file, git_filename in git_sentinel_files or []:
        mounts.append(f"{host_file}:{tasks.CONTAINER_REPO_ROOT}/.git/{git_filename}:rw")

    mounts.append(f"{worktree}:{container_worktree}:rw")

    # Shadow the worktree's .git pointer file with a container-path-aware copy.
    # The host file contains an absolute host path that doesn't resolve inside the
    # container; this mount replaces it with the correct container-relative path.
    # Must come after the worktree:rw mount so Linux VFS lets the file mount win.
    if worktree_git_ptr is not None:
        mounts.append(f"{worktree_git_ptr}:{container_worktree}/.git:rw")
    mounts.extend(_default_home_mounts())
    mounts.extend(backend.home_mounts(session_dir))
    mounts.extend(_construct_docker_mounts(config))
    return mounts


def docker_mounts_no_worktree(
    cwd: Path,
    backend: agent.AgentBackend,
    session_dir: Path,
    config: DockerConfig,
) -> list[str]:
    """Return the -v flags for a no-worktree Docker container.

    Mount layout:
      /workspace        ← cwd, read-write
      {CONTAINER_HOME}/... ← agent-specific home mounts (see backend.home_mounts())
    """
    return (
        [f"{cwd}:/workspace:rw"]
        + _default_home_mounts()
        + backend.home_mounts(session_dir)
        + _construct_docker_mounts(config)
    )


def _default_home_mounts() -> list[str]:
    """Mounts applied to every container regardless of agent: .gitconfig and uv cache."""
    mounts: list[str] = []
    gitconfig = Path.home() / ".gitconfig"
    if gitconfig.exists():
        mounts.append(f"{gitconfig}:{agent.CONTAINER_HOME}/.gitconfig:ro")
    uv_cache = Path.home() / ".cache" / "uv"
    if uv_cache.exists():
        mounts.append(f"{uv_cache}:{agent.CONTAINER_HOME}/.cache/uv:rw")
    return mounts


# ── Container execution ───────────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\033\[[^a-zA-Z]*[a-zA-Z]|\r")
# Whitelist: only STEP/Step lines (both runtimes) and COMMIT lines (Podman).
# Filters out cache hits, layer hashes, "Successfully tagged", etc.
_BUILD_PROGRESS_RE = re.compile(r"^(STEP |COMMIT )", re.IGNORECASE)


def _stream_build(cmd: list[str], cwd: Path, n_lines: int = 4) -> tuple[int, list[str]]:
    """Run build cmd, showing a rolling n_lines buffer on TTY; silent capture otherwise.

    Returns (returncode, all_output_lines) — full output kept for error dumps.
    """
    if not sys.stdout.isatty():
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        return result.returncode, (result.stderr or result.stdout).splitlines()

    term_w = shutil.get_terminal_size((80, 24)).columns
    buf: deque[str] = deque(maxlen=n_lines)
    all_lines: list[str] = []
    prev_n = 0

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for raw in iter(proc.stdout.readline, ""):  # type: ignore[union-attr]
        clean = _ANSI_RE.sub("", raw).strip()
        if not clean:
            continue
        all_lines.append(clean)
        if not _BUILD_PROGRESS_RE.match(clean):
            continue
        buf.append(clean[: term_w - 4])

        if prev_n:
            sys.stdout.write(f"\033[{prev_n}A")
        for line in buf:
            sys.stdout.write(f"\033[2K  {click.style(line, fg='blue')}\n")
        prev_n = len(buf)
        sys.stdout.flush()

    proc.wait()
    return proc.returncode, all_lines


def build_docker_image(
    repo: Path,
    worktree: Path,
    name: str,
    backend: agent.AgentBackend,
    runtime: Runtime = Runtime.DOCKER,
    no_cache: bool = False,
) -> None:
    """Build the sandbox image from the worktree's .hatchery/Dockerfile.<agent>.

    Using the worktree's copy means Dockerfile changes made as part of a task
    are isolated to that task's image and merge into main with the task.
    """
    image = docker_image_name(repo, name)
    worktree_dockerfile = dockerfile_path(worktree, backend)
    # Use a temporary empty directory as the build context — NOT the repo root.
    # The generated Dockerfile has no COPY/ADD from context (only multi-stage
    # COPY --from=), so an empty context is correct and avoids tar-ing the
    # entire repo (or .hatchery/worktrees/) which can hang indefinitely on
    # large repositories.
    with tempfile.TemporaryDirectory(prefix="hatchery-build-") as empty_context:
        build_cmd = [runtime.binary, "build", "-f", str(worktree_dockerfile), "-t", image]
        if no_cache:
            build_cmd.append("--no-cache")
        build_cmd.append(empty_context)
        logger.debug("Building %s image %r (context=%s)", runtime.binary, image, empty_context)
        logger.debug("Build command: %s", build_cmd)

        if logger.isEnabledFor(logging.DEBUG):
            # Let the runtime's own output pass through so build progress is visible.
            result = subprocess.run(build_cmd, cwd=repo, stdin=subprocess.DEVNULL)
            if result.returncode != 0:
                ui.error(f"{runtime.binary} build failed.")
                sys.exit(1)
        else:
            ui.info(click.style(f"Building sandbox image '{image}'", fg="magenta", bold=True))
            returncode, output = _stream_build(build_cmd, cwd=repo)
            if returncode != 0:
                for line in output[-20:]:
                    ui.info(f"  {line}")
                ui.error(f"{runtime.binary} build failed.")
                sys.exit(1)
            ui.success("  Image built.")


def _userns_flags(runtime: Runtime) -> list[str]:
    """Return ``--userns=keep-id`` for Podman on Linux; empty list otherwise.

    Extracted as a named function so tests can monkeypatch it without globally
    changing ``sys.platform``, which would also suppress ``--add-host`` injection.
    """
    if runtime == Runtime.PODMAN and sys.platform == "linux":
        return ["--userns=keep-id"]
    return []


def _run_container(
    image: str,
    mounts: list[str],
    workdir: str,
    hatchery_repo: str,
    name: str,
    mutator: Callable[[dict[str, str]], dict[str, str]] | None,
    proxy_token: str | None,
    agent_cmd: list[str],
    backend: agent.AgentBackend = agent.CODEX,
    dind: bool = False,
    runtime: Runtime = Runtime.DOCKER,
    _command_override: list[str] | None = None,
    _interactive: bool = False,
    cap_add: list[str] | None = None,
    container_name: str | None = None,
    add_host_gateway: bool = False,
) -> subprocess.CompletedProcess[str] | None:
    """Assemble and execute the container run command for the given agent session.

    *agent_cmd* is the complete command to run inside the container, including
    the agent binary and all its arguments (as returned by
    ``backend.build_*_command(docker=True, ...)``).

    *proxy_token* is a stable per-task UUID used as the API key env var inside
    the container.  It must be provided whenever *mutator* is set.
    The same token is reused on resume.

    *add_host_gateway* forces the ``--add-host=host.docker.internal:host-gateway``
    flag on Linux even when the API proxy is not active (e.g. when the kubectl
    feature is enabled and the container needs to reach the RBAC proxy).
    """
    # Start the host-side proxy so the real credentials never enter the container.
    proxy_server = None
    proxy_port = None
    if mutator is not None:
        proxy_server, _ = proxy.start_proxy(mutator, proxy_token, **backend.proxy_kwargs())
        proxy_port = proxy_server.server_address[1]
        if proxy_token is not None:
            logger.debug("Proxy started for task; API key in container is a proxy token")

    cmd = [runtime.binary, "run", "--rm"]
    if _command_override is None or _interactive:
        cmd += ["-it"]
    for mount in mounts:
        cmd += ["-v", mount]

    # Shadow agent-specific tmpfs paths.
    for path in backend.tmpfs_paths():
        cmd += ["--tmpfs", path]

    if mutator is not None and proxy_port is not None:
        for key, val in backend.container_env(proxy_token, proxy_port).items():
            cmd += ["-e", f"{key}={val}"]

    # On Linux, Docker doesn't automatically expose host.docker.internal;
    # --add-host maps it to the host gateway so the container can reach any
    # host-side proxy (API proxy and/or kubectl RBAC proxy).
    if (proxy_port is not None or add_host_gateway) and sys.platform == "linux":
        cmd += ["--add-host=host.docker.internal:host-gateway"]

    cmd += ["-e", f"HATCHERY_TASK={name}"]
    cmd += ["-e", f"HATCHERY_REPO={hatchery_repo}"]
    if container_name is not None:
        cmd += ["--name", container_name]
    # Podman-specific outer-container flags for proper rootless mount permissions.
    # --userns=keep-id maps the calling user to the same UID inside the container
    # so bind-mounted host files (owned by the calling user) are writable by the
    # container's hatchery user (also UID 1000 on most systems).
    # --security-opt label=disable suppresses SELinux/AppArmor label confinement
    # on mounts, which would otherwise block access to host-owned directories.
    match runtime:
        case Runtime.PODMAN:
            cmd += _userns_flags(runtime)
            cmd += ["--security-opt", "label=disable"]
    cmd += ["-w", workdir]
    if dind:
        cmd += ["--cap-drop", "ALL"]
        hardcoded_caps = {
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
            # OCI defaults needed for `podman build` RUN steps (crun capset)
            "AUDIT_WRITE",
            "FSETID",
            "KILL",
            "NET_BIND_SERVICE",
            # Networking caps for bridge networking (k3d, kind, CNI)
            "NET_ADMIN",
            "NET_RAW",
        }
        caps = hardcoded_caps | set(cap_add or [])
        for cap in sorted(caps):
            cmd += ["--cap-add", cap]
        cmd += ["--device", "/dev/fuse"]
        cmd += ["--security-opt", "label=disable"]
        cmd += ["--security-opt", f"seccomp={_SECCOMP}"]
    cmd += [image]
    if _command_override is not None:
        cmd += _command_override
        logger.debug(f"Launching {runtime.binary} container image={image!r} name={name!r} (command override)")
        try:
            if _interactive:
                subprocess.run(cmd)
                return None
            return subprocess.run(cmd, capture_output=True, text=True)
        finally:
            if proxy_server is not None:
                proxy.stop_proxy(proxy_server)

    # Append the full agent command (binary + args, docker-mode already applied).
    cmd += agent_cmd

    logger.debug(f"Launching {runtime.binary} container image={image!r} name={name!r} workdir={workdir!r}")
    try:
        result = subprocess.run(cmd)
    finally:
        if proxy_server is not None:
            proxy.stop_proxy(proxy_server)
    if result.returncode != 0:
        ui.warn(f"{runtime.binary} container exited with code {result.returncode}")
        if runtime == Runtime.PODMAN and result.returncode == 137:
            ui.info(
                "Hint: the container was killed (OOM). Try increasing the Podman machine memory:\n"
                "  podman machine stop\n"
                "  podman machine set --memory 8192\n"
                "  podman machine start"
            )
    return None


def launch_docker(
    repo: Path,
    worktree: Path,
    name: str,
    backend: agent.AgentBackend,
    agent_cmd: list[str],
    config: DockerConfig,
    runtime: Runtime = Runtime.DOCKER,
    no_cache: bool = False,
) -> None:
    """Replace the current process with a Docker-sandboxed agent session.

    Builds the image first — hits the layer cache instantly if nothing changed.
    Auth is provided via the appropriate API key env var or per-task config.
    *agent_cmd* must be the full command already built for Docker mode
    (``backend.build_*_command(docker=True, workdir=container_workdir)``).
    """
    try:
        mutator = backend.make_header_mutator()
    except RuntimeError as e:
        ui.error(str(e))
        sys.exit(1)

    proxy_token = get_or_create_proxy_token(repo, name)

    # Pre-seed writable sentinel files for git's .git/-root writes (COMMIT_EDITMSG etc.).
    session_dir = tasks.task_session_dir(repo, name)
    session_dir.mkdir(parents=True, exist_ok=True)
    git_sentinels: list[tuple[Path, str]] = []
    for fname in ("COMMIT_EDITMSG", "ORIG_HEAD"):
        if not (repo / ".git" / fname).exists():
            continue
        p = session_dir / fname
        if not p.exists():
            p.touch()
        git_sentinels.append((p, fname))

    # Rewrite the worktree .git pointer to use the container-relative path.
    git_ptr = session_dir / "git_ptr"
    git_ptr.write_text(f"gitdir: {tasks.CONTAINER_REPO_ROOT}/.git/worktrees/{name}\n")

    worktree_rel = worktree.relative_to(repo)
    container_worktree = f"{tasks.CONTAINER_REPO_ROOT}/{worktree_rel}"

    if config.dind and not _dind_dockerfile_ok(worktree, backend):
        ui.warn("dind: true is set but the Dockerfile doesn't appear to install Podman.")
        ui.info(f"  Uncomment the '── Podman-in-Podman (DinD)' block in {dockerfile_path(worktree, backend).name}.")

    # Let the backend mutate its per-task config before the container starts
    # (e.g. pre-seed trust or approval in agent-specific config files).
    backend.on_before_container_start(session_dir, proxy_token, container_worktree)

    build_docker_image(repo, worktree, name, backend, runtime=runtime, no_cache=no_cache)
    image = docker_image_name(repo, name)
    mounts = docker_mounts(repo, worktree, name, backend, session_dir, config, git_sentinels, worktree_git_ptr=git_ptr)

    logger.debug(f"Launching {runtime.binary} container for task '{name}'")
    with _kubectl_context(config, session_dir) as kubectl_mounts:
        mounts.extend(kubectl_mounts)
        _run_container(
            image,
            mounts,
            container_worktree,
            tasks.CONTAINER_REPO_ROOT,
            name,
            mutator,
            proxy_token,
            agent_cmd,
            backend=backend,
            dind=config.dind,
            runtime=runtime,
            cap_add=config.cap_add,
            container_name=task_container_name(repo, name),
            add_host_gateway=(config.kubernetes is not None),
        )


def launch_docker_no_worktree(
    cwd: Path,
    name: str,
    backend: agent.AgentBackend,
    agent_cmd: list[str],
    config: DockerConfig,
    runtime: Runtime = Runtime.DOCKER,
    no_cache: bool = False,
) -> None:
    """Launch a Docker-sandboxed agent session with cwd mounted as /workspace.

    Used when --no-worktree is active. No git metadata mounts needed.
    Builds the image from cwd/.hatchery/Dockerfile (same as standard mode).
    *agent_cmd* must be the full command already built for Docker mode
    (``backend.build_*_command(docker=True, workdir="/workspace")``).
    """
    try:
        mutator = backend.make_header_mutator()
    except RuntimeError as e:
        ui.error(str(e))
        sys.exit(1)

    proxy_token = get_or_create_proxy_token(cwd, name)
    session_dir = tasks.task_session_dir(cwd, name)
    session_dir.mkdir(parents=True, exist_ok=True)
    if config.dind and not _dind_dockerfile_ok(cwd, backend):
        ui.warn("dind: true is set but the Dockerfile doesn't appear to install Podman.")
        ui.info(f"  Uncomment the '── Podman-in-Podman (DinD)' block in {dockerfile_path(cwd, backend).name}.")

    backend.on_before_container_start(session_dir, proxy_token, "/workspace")

    build_docker_image(cwd, cwd, name, backend, runtime=runtime, no_cache=no_cache)
    image = docker_image_name(cwd, name)
    mounts = docker_mounts_no_worktree(cwd, backend, session_dir, config=config)

    logger.debug(f"Launching {runtime.binary} container for task '{name}' (no-worktree mode)")
    with _kubectl_context(config, session_dir) as kubectl_mounts:
        mounts.extend(kubectl_mounts)
        _run_container(
            image,
            mounts,
            "/workspace",
            "/workspace",
            name,
            mutator,
            proxy_token,
            agent_cmd,
            backend=backend,
            dind=config.dind,
            runtime=runtime,
            cap_add=config.cap_add,
            container_name=task_container_name(cwd, name),
            add_host_gateway=(config.kubernetes is not None),
        )


def launch_sandbox_shell(
    repo: Path,
    backend: agent.AgentBackend,
    config: DockerConfig,
    runtime: Runtime,
    shell: str = "/bin/bash",
    no_cache: bool = False,
) -> None:
    """Drop the user into an interactive shell inside the sandbox container.

    Builds the same image agents use but skips all agent/proxy/session setup.
    The repo is mounted read-only at /repo.
    """
    build_docker_image(repo, repo, "sandbox", backend, runtime=runtime, no_cache=no_cache)
    image = docker_image_name(repo, "sandbox")
    mounts = [f"{repo}:{tasks.CONTAINER_REPO_ROOT}:rw"] + _default_home_mounts() + _construct_docker_mounts(config)

    # Use a short-lived session dir under ~/.hatchery/ for the kubeconfig mount.
    # tempfile.TemporaryDirectory() is not reliable on macOS because Python
    # resolves to /var/folders/… which is outside Podman Machine's default
    # virtio-fs share roots (only /Users/ and /private/tmp are shared).
    sandbox_session_dir = tasks.HATCHERY_DIR / "sandbox-sessions" / str(uuid.uuid4())
    sandbox_session_dir.mkdir(parents=True, exist_ok=True)
    try:
        with _kubectl_context(config, sandbox_session_dir) as kubectl_mounts:
            mounts = list(mounts) + kubectl_mounts
            _run_container(
                image=image,
                mounts=mounts,
                workdir=tasks.CONTAINER_REPO_ROOT,
                hatchery_repo=tasks.CONTAINER_REPO_ROOT,
                name="sandbox",
                mutator=None,
                proxy_token=None,
                agent_cmd=[],
                runtime=runtime,
                _command_override=[shell],
                _interactive=True,
                add_host_gateway=(config.kubernetes is not None),
            )
    finally:
        shutil.rmtree(sandbox_session_dir, ignore_errors=True)


def exec_task_shell(name: str, runtime: Runtime, repo: Path, shell: str = "/bin/bash") -> None:
    """Exec an interactive shell into the running container for task *name*.

    The container name is derived deterministically via ``task_container_name``
    — the same name assigned at launch — so no ``docker ps`` lookup is needed.
    Docker/Podman will return a clear error if the container is not running.
    """
    subprocess.run([runtime.binary, "exec", "-it", task_container_name(repo, name), shell])


def resolve_runtime(
    repo: Path, worktree: Path, no_docker: bool, backend: agent.AgentBackend = agent.CODEX
) -> Runtime | None:
    """Return the runtime to use for this session, or None to run natively.

    Returns None only when --no-docker is explicitly set.  When Docker mode is
    active (the default), a Dockerfile **must** exist — if none is found the
    function prints an error and exits so the user is never silently placed in
    an unsandboxed environment.

    Checks for the agent-specific Dockerfile (e.g. ``Dockerfile.codex``).
    """
    if no_docker:
        logger.debug("--no-docker set, running natively")
        return None
    agent_df = dockerfile_path(worktree, backend)
    if not agent_df.exists():
        ui.error(
            f"No Dockerfile found for '{backend.kind.lower()}' in {worktree / '.hatchery'}.\n"
            "A Dockerfile is required for sandbox mode. "
            "Run `hatchery new` to create one, or pass --no-docker to run without a sandbox."
        )
        sys.exit(1)
    logger.debug("Dockerfile found, detecting container runtime")
    return detect_runtime()
