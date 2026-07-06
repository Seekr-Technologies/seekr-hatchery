"""Docker sandbox helpers."""

import logging
import os
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
from pydantic import ValidationError as _PydanticValidationError

import seekr_hatchery.agents as agent
import seekr_hatchery.clipboard_image as clipboard_image
import seekr_hatchery.constants as constants
import seekr_hatchery.kubectl_proxy as _kubectl_proxy
import seekr_hatchery.proxy as proxy
import seekr_hatchery.pty_proxy as pty_proxy
import seekr_hatchery.ui as ui
from seekr_hatchery.constants import (
    DOCKER_CONFIG,
    WORKTREES_SUBDIR,
)
from seekr_hatchery.includes import IncludeEntry, IncludeItem
from seekr_hatchery.kubectl_proxy import KubectlConfig
from seekr_hatchery.models import SessionMeta
from seekr_hatchery.mount import BindMount, Mount, VolumeMount, mount_to_docker_args, wrap_cmd_for_file_mounts
from seekr_hatchery.seeded_volumes import prepare_volume_mounts
from seekr_hatchery.utils import open_for_editing, run

logger = logging.getLogger(__name__)


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


class CacheVolume(BaseModel):
    """A persistent named docker/podman volume mounted into every sandbox.

    Lives in the container engine's storage (not on the host filesystem), so
    it avoids virtiofs traffic and survives across ``--rm`` containers.
    The actual volume name on the host runtime is ``hatchery-<name>``.
    """

    model_config = ConfigDict(extra="forbid")
    name: str
    path: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not v:
            raise ValueError("volume name must not be empty")
        # docker/podman volume names allow [a-zA-Z0-9][a-zA-Z0-9_.-]*; we
        # reject ':' and '/' explicitly because they'd corrupt the -v
        # syntax or get mistaken for a bind-mount source.
        if ":" in v or "/" in v:
            raise ValueError(f"volume name {v!r} must not contain ':' or '/'")
        return v

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(f"volume path {v!r} must be an absolute container path")
        return v


class DockerConfig(BaseModel):
    """Schema for .hatchery/docker.yaml."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1"] = "1"
    mounts: list[str] = []
    volumes: list[CacheVolume] = []
    include: list[str | IncludeItem] = []
    dind: bool = False
    follow_symlinks: bool = False
    clipboard_images: bool = True
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

    @field_validator("volumes", mode="before")
    @classmethod
    def validate_volumes(cls, v: list | None) -> list:
        # YAML parses an empty `volumes:` section (e.g. all-commented entries
        # in the template) as None — coerce to [] to match `mounts` semantics.
        if v is None:
            return []
        return v

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

    @field_validator("include", mode="before")
    @classmethod
    def validate_include(cls, v: list | None) -> list:
        if v is None:
            return []
        result = []
        for i, entry in enumerate(v):
            if isinstance(entry, str):
                result.append(entry)
            elif isinstance(entry, dict):
                try:
                    result.append(IncludeItem.model_validate(entry))
                except _PydanticValidationError as exc:
                    raise ValueError(f"include[{i}]: {exc}") from exc
            else:
                raise ValueError(f"include[{i}]: expected a string or dict, got {type(entry).__name__!r}")
        return result


def parse_docker_include_entry(entry: str | IncludeItem) -> tuple[str, str]:
    """Parse a single docker.yaml include entry into (path_str, mode).

    Accepts the legacy string form or a validated IncludeItem::

        "../other-repo"                          → ("../other-repo", "worktree")
        IncludeItem(path="../ref", mode="ro")    → ("../ref", "ro")
    """
    if isinstance(entry, str):
        return entry, "worktree"
    return entry.path, entry.mode


# ── kubectl helpers ───────────────────────────────────────────────────────────


@contextmanager
def _maybe_api_server(
    mutator: Callable[[dict[str, str]], dict[str, str]] | None,
    proxy_token: str | None,
    backend: agent.AgentBackend,
) -> Generator[proxy.APIServer | None, None, None]:
    """Conditionally start the API proxy and yield the server handle (or ``None``).

    *mutator* is the gate: a non-``None`` mutator means the caller has a real
    API key to inject, so a proxy is needed.  When ``None`` (e.g. sandbox shell
    sessions which don't run an agent), no proxy is started and ``None`` is
    yielded so call sites can use this unconditionally with a uniform pattern::

        with _maybe_api_server(mutator, token, backend) as api_proxy, \\
             _kubectl_context(config, session_dir) as kubectl_mounts:
            _run_container(..., proxy_port=api_proxy.port if api_proxy else None)
    """
    if mutator is None:
        yield None
        return
    try:
        kwargs = backend.proxy_kwargs()
    except RuntimeError as exc:
        ui.error(str(exc))
        sys.exit(1)
    with proxy.api_server(mutator, proxy_token or "", **kwargs) as server:
        yield server


@contextmanager
def _kubectl_context(
    config: DockerConfig,
    session_dir: Path,
    kubectl_proxy_token: str,
) -> Generator[list[Mount], None, None]:
    """Context manager that starts the kubectl proxy chain and yields extra mounts.

    Yields an empty list when ``config.kubernetes`` is ``None``.  On exit
    (normal or exceptional) the RBAC proxy and kubectl proxy subprocess are
    stopped.

    Caller is responsible for resolving *kubectl_proxy_token* — it's a
    stable per-session secret that sessions persists under session_dir.
    """
    if config.kubernetes is None:
        yield []
        return

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
        yield [BindMount(src=str(kubeconfig_path), dst=f"{agent.CONTAINER_HOME}/.kube/config", mode="RO")]
    finally:
        _kubectl_proxy.stop_rbac_proxy(rbac_server)
        _kubectl_proxy.stop_kubectl_proxy_proc(kubectl_proc)


# ── Utilities ─────────────────────────────────────────────────────────────────


def dockerfile_path(base: Path, backend: agent.AgentBackend) -> Path:
    """Return the canonical path to this backend's Dockerfile under *base*.

    Files are named ``Dockerfile.<agent>`` (e.g. ``Dockerfile.codex``).
    """
    return base / ".hatchery" / f"Dockerfile.{backend.kind.lower()}"


def docker_available() -> bool:
    """Return True if the Docker daemon is reachable."""
    logger.debug("Checking Docker availability")
    try:
        result = run(["docker", "info"], check=False)
    except FileNotFoundError:
        return False
    return result.returncode == 0


def podman_available() -> bool:
    """Return True if the Podman CLI is reachable."""
    logger.debug("Checking Podman availability")
    try:
        result = run(["podman", "info"], check=False)
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
    df.parent.mkdir(parents=True, exist_ok=True)
    if source is not None:
        source_df = dockerfile_path(source, backend)
        if source_df.exists():
            shutil.copy2(source_df, df)
            ui.warn(
                f"  Copied {df.relative_to(repo)} from repo root "
                "(uncommitted — will not be committed to this worktree branch)"
            )
            return False
    text = _DOCKERFILE_TEMPLATE.read_text()
    text = text.replace("{{AGENT_INSTALL}}", backend.dockerfile_install)
    text = text.replace("{{DIND}}", _comment_out(DIND_DOCKERFILE_LINES))
    df.write_text(text)
    ui.info(f"  Created {df.relative_to(repo)}")
    answer = input("  Would you like to edit the Dockerfile? [Y/n] ").strip().lower()
    if answer != "n":
        open_for_editing(df)
    return True


def _migrate_docker_config(data: dict) -> dict:
    """Bring a docker.yaml config dict up to the current schema version.

    Add a new `if v == N` block here whenever the schema changes.
    Each block should make the minimal edit to reach version N+1,
    then increment data["schema_version"]. The final state will
    always be DOCKER_CONFIG_SCHEMA_VERSION.
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
    config_file = repo / DOCKER_CONFIG
    if config_file.exists():
        return False
    config_file.parent.mkdir(parents=True, exist_ok=True)
    if source is not None:
        source_config = source / DOCKER_CONFIG
        if source_config.exists():
            shutil.copy2(source_config, config_file)
            ui.warn(
                f"  Copied {DOCKER_CONFIG} from repo root (uncommitted — will not be committed to this worktree branch)"
            )
            return False
    config_file.write_text(_DOCKER_CONFIG_TEMPLATE.read_text())
    ui.info(f"  Created {DOCKER_CONFIG}")
    answer = input("  Would you like to edit the docker config? [Y/n] ").strip().lower()
    if answer != "n":
        open_for_editing(config_file)
    return True


def ensure_docker_files_uncommitted(
    repo: Path,
    worktree: Path,
    backend: agent.AgentBackend,
) -> None:
    """Ensure Docker files exist in *worktree* without committing.

    Generates Dockerfile and docker.yaml in the repo root if they don't
    already exist, then copies them into the worktree via the *source*
    parameter so they remain uncommitted on the task branch.
    """
    ensure_dockerfile(repo, backend)
    ensure_docker_config(repo)
    ensure_dockerfile(worktree, backend, source=repo)
    ensure_docker_config(worktree, source=repo)


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
    config_file = root / DOCKER_CONFIG
    if not config_file.exists():
        return DockerConfig()
    try:
        raw = yaml.safe_load(config_file.read_text()) or {}
        raw = _migrate_docker_config(raw)
        return DockerConfig.model_validate(raw)
    except Exception as exc:
        ui.error(f"invalid {DOCKER_CONFIG}: {exc}")
        sys.exit(1)


def launch_context(
    meta: SessionMeta,
    runtime: "Runtime | None",
) -> tuple[DockerConfig | None, list[str], str]:
    """Return (config, features, container_workdir) for the launch path.

    Composes :func:`load_docker_config` + :func:`docker_features` with the
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
    root = meta.repo_path if meta.no_worktree else meta.worktree_path
    config = load_docker_config(root)
    features = docker_features(config)
    _check_host_path_safe_for_mount(meta.repo_path)
    container_workdir = str(meta.worktree_path)
    return config, features, container_workdir


def _construct_docker_mounts(config: DockerConfig) -> list[Mount]:
    """Resolve a DockerConfig into Mount objects.

    Expands ~ in host paths and silently skips entries whose host path does
    not exist on this machine (allows a shared config to list paths that are
    only present on some developers' machines).
    """
    result: list[Mount] = []
    for entry in config.mounts:
        parts = entry.split(":", 2)
        host = Path(parts[0]).expanduser()
        container = parts[1]
        mode = parts[2] if len(parts) == 3 else "ro"
        if not host.exists():
            logger.debug("Custom mount host path does not exist, skipping: %s", host)
            continue
        result.append(BindMount(src=str(host), dst=container, mode=mode.upper()))
    return result


# Prefix applied to user-declared volume names when forming the actual
# docker/podman volume name. Mirrors the namespacing already used elsewhere
# (e.g. hatchery/<task> branch refs) and keeps these volumes visually distinct
# from any unrelated volumes the user may have on the host runtime.
_VOLUME_NAME_PREFIX = "hatchery-"


def _construct_volume_mounts(config: DockerConfig) -> list[Mount]:
    """Resolve declared cache volumes into named-volume Mount objects.

    Each entry produces a Mount with ``volume=True`` so the runtime treats
    *src* as a named docker/podman volume rather than a host path. The
    actual volume is named ``hatchery-<name>``; ``_ensure_volumes`` creates
    it on the runtime before the container starts.
    """
    return [
        VolumeMount(name=f"{_VOLUME_NAME_PREFIX}{v.name}", dst=v.path, mode="RW", task_scoped=False)
        for v in config.volumes
    ]


# Host directories whose contents are provided by the container image or kernel.
# Mounting host equivalents over them would shadow critical binaries/libraries
# or replace special filesystems (/proc, /sys, /dev). /tmp, /var, /home, /opt
# are intentionally NOT blocked — users legitimately keep data there.
_SYMLINK_SYSTEM_BLOCKLIST: tuple[Path, ...] = (
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
    Path("/lib"),
    Path("/lib64"),
    Path("/etc"),
    Path("/proc"),
    Path("/sys"),
    Path("/dev"),
    Path("/run"),
)


def _check_host_path_safe_for_mount(repo: Path) -> None:
    """Reject host repo paths that would shadow load-bearing container paths.

    Container paths now mirror host paths so any path-keyed state inside the
    container (agent project dirs, lockfiles, symlink targets) matches what a
    native run would produce. That breaks if the host repo path collides with
    a directory the container image depends on — e.g., /usr, /etc, or the
    container's home dir itself.

    The blocklist mirrors _SYMLINK_SYSTEM_BLOCKLIST plus the container HOME.
    Subpaths under these (e.g. /home/hatchery/myrepo) are fine — they just add
    a subdir to the container's home.
    """
    blocklist: tuple[Path, ...] = _SYMLINK_SYSTEM_BLOCKLIST + (
        Path("/"),
        Path(agent.CONTAINER_HOME),
    )
    repo_resolved = repo.resolve()
    if repo_resolved in blocklist:
        ui.error(
            f"Host repo path {repo} collides with a load-bearing container path. "
            f"Move the repo to a different location (e.g. under /home, /tmp, or /Users/...)."
        )
        sys.exit(1)


# Directories we don't bother descending into — large, and unlikely to host
# meaningful user-authored symlinks.
_SYMLINK_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hatchery",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
    }
)


def _construct_symlink_mounts(scan_root: Path, existing_mounts: list[Mount]) -> list[Mount]:
    """Walk *scan_root* for symlinks; return Mounts for external targets.

    For each symlink whose fully-resolved target lives outside the already-mounted
    area (and outside the system blocklist), emit a single ``target:target:rw``
    bind-mount so the symlink's stored host path resolves identically inside the
    container. Deduplicates by unique resolved target. Symlinks whose target is
    inside *scan_root* are skipped — the scan_root mount already covers them
    (under host-path mirroring, both absolute and relative internal links
    resolve correctly without any extra wiring).

    Limitation: chains that traverse multiple external symlink files only have
    their final target mounted, not intermediate hops — those chains may still
    dangle inside the container.
    """
    scan_root_resolved = scan_root.resolve()

    existing: set[Path] = set()
    for m in existing_mounts:
        if m.src is None:
            continue
        host = Path(str(m.src)).expanduser()
        try:
            existing.add(host.resolve())
        except OSError:
            continue

    def _on_err(exc: OSError) -> None:
        logger.debug("follow_symlinks: walk error: %s", exc)

    seen: set[Path] = set()
    mounts: list[Mount] = []

    for dirpath, dirnames, filenames in os.walk(scan_root, followlinks=False, onerror=_on_err):
        dirnames[:] = [d for d in dirnames if d not in _SYMLINK_SKIP_DIRS]
        for entry in list(dirnames) + filenames:
            p = Path(dirpath) / entry
            if not p.is_symlink():
                continue
            try:
                target = p.resolve(strict=True)
            except (OSError, RuntimeError):
                logger.debug("follow_symlinks: skipping unresolvable %s", p)
                continue
            target_in_scan = target == scan_root_resolved or scan_root_resolved in target.parents
            if target_in_scan:
                # Internal target — covered by the scan_root mount.
                continue
            if target in seen:
                continue
            if any(target == hp or hp in target.parents for hp in existing):
                continue
            if any(target == sp or sp in target.parents for sp in _SYMLINK_SYSTEM_BLOCKLIST):
                logger.debug("follow_symlinks: skipping system-path target %s", target)
                continue
            if any(target in hp.parents for hp in existing):
                logger.debug("follow_symlinks: skipping parent-of-existing target %s", target)
                continue
            seen.add(target)
            mounts.append(BindMount(src=str(target), dst=str(target), mode="RW"))

    return mounts


def _git_worktree_mounts(repo: Path, name: str, container_root: str) -> list[Mount]:
    """Return the layered Mounts for one repo + worktree pair (pre-worktree portion).

    Produces the read-only repo root + targeted read-write .git sub-mounts that
    protect the main branch while allowing the hatchery/<name> worktree's git
    metadata to be modified.  The worktree directory itself and any git-pointer
    shadow file are NOT included — callers append those afterwards (so sentinel
    files can be inserted between the .git layers and the worktree mount if needed).

    This function is intentionally repo-agnostic: pass ``str(repo)`` for the
    primary repo (container paths mirror host paths) or ``/includes/<basename>``
    for an included secondary repo.
    """
    git_dir = repo / ".git"
    mounts: list[Mount] = [
        BindMount(src=str(repo), dst=container_root, mode="RO"),
        BindMount(src=str(git_dir), dst=f"{container_root}/.git", mode="RW"),
        BindMount(src=str(git_dir / "objects"), dst=f"{container_root}/.git/objects", mode="RW"),
    ]
    # Mount the entire hatchery/ ref directory rw so git can create .lock sidecar
    # files alongside the branch ref during commits.
    hatchery_refs = git_dir / "refs" / "heads" / "hatchery"
    if hatchery_refs.exists():
        mounts.append(BindMount(src=str(hatchery_refs), dst=f"{container_root}/.git/refs/heads/hatchery", mode="RW"))
    logs_dir = git_dir / "logs"
    if logs_dir.exists():
        mounts.append(BindMount(src=str(logs_dir), dst=f"{container_root}/.git/logs", mode="RW"))
    # Only this task's worktree git metadata is writable; other worktrees' metadata
    # is protected by the ro parent mount (prevents `git worktree prune` damage).
    worktree_meta = git_dir / "worktrees" / name
    if worktree_meta.exists():
        mounts.append(BindMount(src=str(worktree_meta), dst=f"{container_root}/.git/worktrees/{name}", mode="RW"))
    return mounts


def build_mounts(
    meta: SessionMeta,
    backend: agent.AgentBackend,
    session_dir: Path,
    config: DockerConfig,
    *,
    git_sentinel_files: list[tuple[Path, str]] | None = None,
    include_entries: list[IncludeEntry] | None = None,
) -> list[Mount]:
    """Return the Mounts for a session's container.

    Container paths mirror host paths (see :func:`launch_context` for why).
    Branches on ``meta.no_worktree``:
      * False (default): layered git-worktree mounts — repo ro, targeted .git
        sub-dirs rw, sentinel files rw, worktree rw (overlaying the repo root
        at the host repo path). The worktree's existing ``.git`` pointer file
        (``gitdir: <host_repo>/.git/worktrees/<name>``) already resolves
        correctly inside the container under host-path mirroring, so no
        shadow rewrite is needed.
      * True: ``meta.worktree`` mounted at its host path, no git metadata.

    Append ``include_entries`` mounts at the end when provided (each include
    is mapped to ``/includes/<basename>/``).

    Known security holes for the worktree case (accepted; fixing them would
    break real-time git visibility):
      LOW-MEDIUM: refs/heads/hatchery/ rw allows creating arbitrary
        hatchery/<anything> branch refs.
      MEDIUM: .git/ root rw allows modifying config/packed-refs/FETCH_HEAD
        etc. Required so git can take rebase/cherry-pick/merge locks.
      HIGH: .git/objects/ rw against the real object store — the container
        can corrupt git history. Fixing requires staging+copy-back, which
        kills real-time host visibility of commits.
    """
    mounts: list[Mount]
    if meta.no_worktree:
        cwd = meta.worktree_path
        mounts = [BindMount(src=str(meta.worktree), dst=str(meta.worktree_path), mode="RW")]
        mounts.extend(_default_home_mounts())
        mounts.extend(backend.construct_mounts(session_dir))
        mounts.extend(_construct_docker_mounts(config))
        mounts.extend(_construct_volume_mounts(config))
        if config.clipboard_images:
            mounts.append(_clipboard_image_mount(session_dir))
        if config.follow_symlinks:
            mounts.extend(_construct_symlink_mounts(cwd, mounts))
    else:
        container_root = str(meta.repo_path)
        container_worktree = str(meta.worktree_path)

        mounts = _git_worktree_mounts(meta.repo_path, meta.name, container_root)

        # git writes these into .git/ root during normal commits; use per-task
        # sentinel files so .git/ root stays ro.
        for host_file, git_filename in git_sentinel_files or []:
            mounts.append(BindMount(src=str(host_file), dst=f"{container_root}/.git/{git_filename}", mode="RW"))

        mounts.append(BindMount(src=str(meta.worktree), dst=container_worktree, mode="RW"))
        mounts.extend(_default_home_mounts())
        mounts.extend(backend.construct_mounts(session_dir))
        mounts.extend(_construct_docker_mounts(config))
        mounts.extend(_construct_volume_mounts(config))
        if config.clipboard_images:
            mounts.append(_clipboard_image_mount(session_dir))
        if config.follow_symlinks:
            mounts.extend(_construct_symlink_mounts(meta.worktree_path, mounts))

    if include_entries:
        mounts.extend(_docker_mounts_includes(include_entries, meta.name, session_dir, no_worktree=meta.no_worktree))
    return mounts


def _docker_mounts_includes(
    include_entries: list[IncludeEntry],
    name: str,
    session_dir: Path,
    no_worktree: bool,
) -> list[Mount]:
    """Return Mounts for paths included via --include / --include-rw / --include-ro.

    Each include is mounted at its host path inside the container, same as
    the primary repo. Distinct host paths are inherently unique, so no
    basename collision logic is needed; the worktree's existing .git
    pointer already resolves correctly under host-path mirroring, so no
    pointer rewrite is needed either.

    mode="worktree": For git repos with a hatchery/<name> worktree the
    same layered mount strategy as the primary repo is applied (root:ro,
    targeted .git sub-dirs:rw, worktree:rw).

    mode="rw" or mode="ro": Simple bind-mount with the corresponding
    access mode. No worktree is expected or created.

    In no-worktree mode all entries fall back to a simple mount using
    their access mode (worktree entries are treated as rw).
    """
    mounts: list[Mount] = []

    for entry in include_entries:
        path = entry.path
        _check_host_path_safe_for_mount(path)
        container_path = str(path)

        if entry.mode == "worktree" and not no_worktree:
            is_git = (path / ".git").exists()
            if is_git:
                worktree = path / WORKTREES_SUBDIR / name
                if worktree.exists():
                    # Layered mounts: root ro + targeted .git rw (same as primary repo)
                    mounts.extend(_git_worktree_mounts(path, name, container_path))
                    mounts.append(BindMount(src=str(worktree), dst=str(worktree), mode="RW"))
                    continue
                logger.warning(
                    "include worktree not found for %s (expected %s); "
                    "falling back to plain rw mount — branch isolation unavailable.",
                    path,
                    worktree,
                )
            # Git repo without worktree, or plain dir in worktree mode → rw
            mounts.append(BindMount(src=str(path), dst=container_path, mode="RW"))
        else:
            # reference mode (ro/rw), or no_worktree fallback
            access = entry.mode if entry.is_reference() else "rw"
            mounts.append(BindMount(src=str(path), dst=container_path, mode=access.upper()))

    return mounts


def _default_home_mounts() -> list[Mount]:
    """Mounts applied to every container regardless of agent: .gitconfig."""
    mounts: list[Mount] = []
    gitconfig = Path.home() / ".gitconfig"
    if gitconfig.exists():
        mounts.append(BindMount(src=str(gitconfig), dst=f"{agent.CONTAINER_HOME}/.gitconfig", mode="RO"))
    return mounts


def clipboard_image_dir(session_dir: Path) -> Path:
    """Per-task host directory where pasted clipboard images are saved.

    Bind-mounted at the same absolute path inside the container so a file
    written here by the host-side PTY proxy resolves identically when the
    agent reads it.
    """
    return session_dir / "clipboard"


def _clipboard_image_mount(session_dir: Path) -> Mount:
    """Return the Mount for the per-task clipboard images directory.

    Mounts at the identical host path inside the container so the file path
    typed into the agent's stdin (a host path) resolves byte-for-byte.
    """
    d = clipboard_image_dir(session_dir)
    d.mkdir(parents=True, exist_ok=True)
    return BindMount(src=str(d), dst=str(d), mode="RW")


def remove_clipboard_dir(session_dir: Path) -> None:
    """Delete the per-task clipboard images directory (idempotent, swallows errors).

    Pasted screenshots are typically 100 KB – 5 MB; removing them when a task
    is marked complete or deleted keeps `~/.hatchery/tasks/` from growing
    unbounded. Resume flows (archive) intentionally do NOT call this — the
    files may be referenced by saved conversation history.
    """
    d = clipboard_image_dir(session_dir)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


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
    image_name: str,
    backend: agent.AgentBackend,
    runtime: Runtime = Runtime.DOCKER,
    no_cache: bool = False,
) -> None:
    """Build the sandbox image from the worktree's .hatchery/Dockerfile.<agent>.

    Using the worktree's copy means Dockerfile changes made as part of a task
    are isolated to that task's image and merge into main with the task.

    Caller resolves *image_name* — typically ``sessions.image_name(repo, name)``.
    """
    image = image_name
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


def _ensure_volumes(runtime: Runtime, mounts: list[Mount]) -> None:
    """Create any named volumes referenced by *mounts* if they don't exist.

    Both Docker and Podman auto-create missing volumes when ``run -v
    name:/path`` is invoked, but doing it explicitly here surfaces
    creation in the debug logs and lets future tooling (e.g. ``hatchery
    volume prune``) recognise volumes hatchery owns.  ``volume inspect``
    returns non-zero when the volume is missing, which is the cheapest
    cross-runtime existence check.

    For seeded VolumeMounts the launch path already handled creation
    (and seeding) via ``prepare_volume_mounts``; this call is then a
    no-op for them. The remaining VolumeMounts are user-config volumes
    from docker.yaml (``task_scoped=False``, no seed) used by the
    sandbox-shell flow.
    """
    seen: set[str] = set()
    for m in mounts:
        if not isinstance(m, VolumeMount) or m.name in seen:
            continue
        seen.add(m.name)
        if run([runtime.binary, "volume", "inspect", m.name], check=False).returncode == 0:
            continue
        logger.debug("creating %s volume: %s", runtime.binary, m.name)
        run([runtime.binary, "volume", "create", m.name])


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
    mounts: list[Mount],
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
    proxy_port: int | None = None,
    add_host_gateway: bool = False,
    paste_interceptor: clipboard_image.PasteInterceptor | None = None,
) -> subprocess.CompletedProcess[str] | None:
    """Assemble and execute the container run command for the given agent session.

    *agent_cmd* is the complete command to run inside the container, including
    the agent binary and all its arguments (as returned by
    ``backend.build_*_command(docker=True, ...)``).

    *proxy_token* is a stable per-task UUID used as the API key env var inside
    the container.  It must be provided whenever *mutator* is set.
    The same token is reused on resume.

    *proxy_port* is the port of the host-side API proxy, managed externally via
    ``_maybe_api_server``.  When ``None`` no API proxy env vars are injected.

    *add_host_gateway* forces the ``--add-host=host.docker.internal:host-gateway``
    flag on Linux even when the API proxy is not active (e.g. when the kubectl
    feature is enabled and the container needs to reach the RBAC proxy).
    """
    # --init injects a minimal init process (tini for docker, catatonit for
    # podman) as PID 1 so SIGCHLD is handled and zombie children of the agent
    # process get reaped. Without it, long-running containers accumulate
    # zombies from tool shell-outs and tool calls eventually hang.
    _ensure_volumes(runtime, mounts)
    cmd = [runtime.binary, "run", "--rm", "--init"]
    if _command_override is None or _interactive:
        cmd += ["-it"]
    for m in mounts:
        cmd += mount_to_docker_args(m)

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
        cmd += wrap_cmd_for_file_mounts(_command_override, mounts)
        logger.debug(f"Launching {runtime.binary} container image={image!r} name={name!r} (command override)")
        if _interactive:
            subprocess.run(cmd)
            return None
        return subprocess.run(cmd, capture_output=True, text=True)

    # Append the full agent command (binary + args, docker-mode already applied).
    cmd += wrap_cmd_for_file_mounts(agent_cmd, mounts)

    logger.debug(f"Launching {runtime.binary} container image={image!r} name={name!r} workdir={workdir!r}")
    returncode = _exec_agent(cmd, paste_interceptor)
    if returncode != 0:
        ui.warn(f"{runtime.binary} container exited with code {returncode}")
        if runtime == Runtime.PODMAN and returncode == 137:
            ui.info(
                "Hint: the container was killed (OOM). Try increasing the Podman machine memory:\n"
                "  podman machine stop\n"
                "  podman machine set --memory 8192\n"
                "  podman machine start"
            )
    return None


def _exec_agent(cmd: list[str], paste_interceptor: clipboard_image.PasteInterceptor | None) -> int:
    """Run the agent's ``docker run`` command, optionally under the PTY proxy.

    The PTY-proxy path activates only when a paste interceptor was provided
    AND stdin is a real TTY.  Non-TTY callers (CI, captured stdin) get the
    plain ``subprocess.run`` path so output behaviour stays unchanged.
    """
    if paste_interceptor is not None and sys.stdin.isatty():
        return pty_proxy.run_with_pty(cmd, paste_interceptor)
    return subprocess.run(cmd).returncode


def _make_paste_interceptor(
    backend: agent.AgentBackend,
    session_dir: Path,
    config: DockerConfig,
) -> clipboard_image.PasteInterceptor | None:
    """Build the paste interceptor for an agent launch, or ``None`` when disabled."""
    if not config.clipboard_images:
        return None
    return clipboard_image.PasteInterceptor(
        clipboard_image_dir(session_dir),
        backend.format_image_reference,
    )


def run_session(
    meta: SessionMeta,
    backend: agent.AgentBackend,
    agent_cmd: list[str],
    config: DockerConfig,
    *,
    proxy_token: str,
    kubectl_proxy_token: str | None = None,
    runtime: Runtime = Runtime.DOCKER,
    no_cache: bool = False,
    include_entries: list[IncludeEntry] | None = None,
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
    are read from *meta* (the SessionMeta properties resolve them via a
    function-level import of sessions, which is safe at call time even
    though docker doesn't import sessions at module load). Tokens are
    explicit kwargs because they involve filesystem side-effects (writing
    a stable per-session secret) that the caller — sessions.launch —
    owns.
    """
    try:
        mutator = backend.make_header_mutator()
    except RuntimeError as e:
        ui.error(str(e))
        sys.exit(1)

    session_dir = meta.session_dir
    session_dir.mkdir(parents=True, exist_ok=True)

    _check_host_path_safe_for_mount(meta.repo_path)

    git_sentinels: list[tuple[Path, str]] | None = None
    if meta.no_worktree:
        container_workdir = str(meta.worktree_path)
        container_repo = str(meta.worktree_path)
        build_root = meta.worktree_path  # cwd serves as build context root
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
        build_root = meta.worktree_path

    if config.dind and not _dind_dockerfile_ok(build_root, backend):
        ui.warn("dind: true is set but the Dockerfile doesn't appear to install Podman.")
        ui.info(f"  Uncomment the '── Podman-in-Podman (DinD)' block in {dockerfile_path(build_root, backend).name}.")

    backend.on_before_container_start(session_dir, proxy_token, container_workdir)

    build_docker_image(meta.repo_path, build_root, meta.image_name, backend, runtime=runtime, no_cache=no_cache)
    mounts = build_mounts(
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
    mounts = prepare_volume_mounts(
        runtime.binary,
        mounts,
        meta,
        session_dir,
        proxy_token,
        container_workdir,
    )

    mode_label = "no-worktree mode" if meta.no_worktree else "worktree mode"
    logger.debug(f"Launching {runtime.binary} container for session '{meta.name}' ({mode_label})")
    with (
        _maybe_api_server(mutator, proxy_token, backend) as api_proxy,
        _kubectl_context(config, session_dir, kubectl_proxy_token or "") as kubectl_mounts,
    ):
        mounts.extend(kubectl_mounts)
        _run_container(
            meta.image_name,
            mounts,
            container_workdir,
            container_repo,
            meta.name,
            mutator,
            proxy_token,
            agent_cmd,
            backend=backend,
            dind=config.dind,
            runtime=runtime,
            cap_add=config.cap_add,
            container_name=meta.container_name,
            proxy_port=api_proxy.port if api_proxy else None,
            add_host_gateway=bool(kubectl_mounts),
            paste_interceptor=_make_paste_interceptor(backend, session_dir, config),
        )


def launch_sandbox_shell(
    repo: Path,
    backend: agent.AgentBackend,
    config: DockerConfig,
    runtime: Runtime,
    image_name: str,
    *,
    kubectl_proxy_token: str = "",
    shell: str = "/bin/bash",
    no_cache: bool = False,
) -> None:
    """Drop the user into an interactive shell inside the sandbox container.

    Builds the same image agents use but skips all agent/proxy/session setup.
    The repo is mounted RW at its host path (so the sandbox shell sees the
    same paths a native shell would).

    Caller resolves *image_name* — typically ``sessions.image_name(repo, "sandbox")``.
    """
    _check_host_path_safe_for_mount(repo)
    build_docker_image(repo, repo, image_name, backend, runtime=runtime, no_cache=no_cache)
    mounts: list[Mount] = [BindMount(src=str(repo), dst=str(repo), mode="RW")]
    mounts.extend(_default_home_mounts())
    mounts.extend(_construct_docker_mounts(config))
    mounts.extend(_construct_volume_mounts(config))

    # Use a short-lived session dir under ~/.hatchery/ for the kubeconfig mount.
    # tempfile.TemporaryDirectory() is not reliable on macOS because Python
    # resolves to /var/folders/… which is outside Podman Machine's default
    # virtio-fs share roots (only /Users/ and /private/tmp are shared).
    sandbox_session_dir = constants.HATCHERY_DIR / "sandbox-sessions" / str(uuid.uuid4())
    sandbox_session_dir.mkdir(parents=True, exist_ok=True)
    try:
        with (
            _maybe_api_server(None, None, backend) as api_proxy,
            _kubectl_context(config, sandbox_session_dir, kubectl_proxy_token) as kubectl_mounts,
        ):
            mounts = list(mounts) + kubectl_mounts
            _run_container(
                image=image_name,
                mounts=mounts,
                workdir=str(repo),
                hatchery_repo=str(repo),
                name="sandbox",
                mutator=None,
                proxy_token=None,
                agent_cmd=[],
                runtime=runtime,
                _command_override=[shell],
                _interactive=True,
                proxy_port=api_proxy.port if api_proxy else None,
                add_host_gateway=bool(kubectl_mounts),
            )
    finally:
        shutil.rmtree(sandbox_session_dir, ignore_errors=True)


def exec_task_shell(container_name: str, runtime: Runtime, shell: str = "/bin/bash") -> None:
    """Exec an interactive shell into the running container *container_name*.

    Caller resolves *container_name* — typically ``sessions.container_name(repo, name)``.
    The container name is deterministic (it's the same value assigned at launch)
    so no ``docker ps`` lookup is needed. Docker/Podman returns a clear error
    if the container is not running.
    """
    subprocess.run([runtime.binary, "exec", "-it", container_name, shell])


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
