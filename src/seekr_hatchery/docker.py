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
    CONTAINER_INCLUDES_ROOT,
    CONTAINER_REPO_ROOT,
    DOCKER_CONFIG,
    WORKTREES_SUBDIR,
)
from seekr_hatchery.includes import IncludeEntry, IncludeItem
from seekr_hatchery.kubectl_proxy import KubectlConfig
from seekr_hatchery.models import SessionMeta
from seekr_hatchery.utils import open_for_editing, run, unique_basename

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
    with proxy.api_server(mutator, proxy_token or "", **backend.proxy_kwargs()) as server:
        yield server


@contextmanager
def _kubectl_context(
    config: DockerConfig,
    session_dir: Path,
    kubectl_proxy_token: str,
) -> Generator[list[str], None, None]:
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
    if source is not None:
        source_config = source / DOCKER_CONFIG
        if source_config.exists():
            shutil.copy2(source_config, config_file)
            ui.warn(
                f"  Copied {DOCKER_CONFIG} from repo root "
                "(uncommitted — will not be committed to this worktree branch)"
            )
            return False
    config_file.parent.mkdir(parents=True, exist_ok=True)
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


def _construct_symlink_mounts(scan_root: Path, existing_mounts: list[str]) -> list[str]:
    """Walk *scan_root* for symlinks; return -v flags for external targets.

    For each symlink whose fully-resolved target lives outside the already-mounted
    area (and outside the system blocklist), emit a single ``target:target:rw``
    bind-mount so the symlink's stored host path resolves identically inside the
    container. Deduplicates by unique resolved target.

    Two link shapes do not survive the host→container path remap and are rejected
    at launch with a clear error rather than silently dangling:
      - absolute link whose target is inside *scan_root* (the host path doesn't
        exist in the container; *scan_root* is mounted at a different location)
      - relative link whose resolved target is outside *scan_root* (the relative
        climb anchors at the remapped container path and lands elsewhere)

    Limitation: chains that traverse multiple external symlink files only have
    their final target mounted, not intermediate hops — those chains may still
    dangle inside the container.
    """
    scan_root_resolved = scan_root.resolve()

    existing: set[Path] = set()
    for m in existing_mounts:
        host = Path(m.split(":", 2)[0]).expanduser()
        try:
            existing.add(host.resolve())
        except OSError:
            continue

    def _on_err(exc: OSError) -> None:
        logger.debug("follow_symlinks: walk error: %s", exc)

    seen: set[Path] = set()
    mounts: list[str] = []
    bad_abs_internal: list[tuple[Path, str]] = []
    bad_rel_external: list[tuple[Path, str]] = []

    for dirpath, dirnames, filenames in os.walk(scan_root, followlinks=False, onerror=_on_err):
        dirnames[:] = [d for d in dirnames if d not in _SYMLINK_SKIP_DIRS]
        for entry in list(dirnames) + filenames:
            p = Path(dirpath) / entry
            if not p.is_symlink():
                continue
            try:
                link_str = os.readlink(p)
            except OSError:
                continue
            try:
                target = p.resolve(strict=True)
            except (OSError, RuntimeError):
                logger.debug("follow_symlinks: skipping unresolvable %s", p)
                continue
            is_absolute = os.path.isabs(link_str)
            target_in_scan = target == scan_root_resolved or scan_root_resolved in target.parents

            if is_absolute and target_in_scan:
                bad_abs_internal.append((p, link_str))
                continue
            if not is_absolute and not target_in_scan:
                bad_rel_external.append((p, link_str))
                continue
            if not is_absolute and target_in_scan:
                # Relative link staying inside scan_root resolves correctly inside
                # the container — no mount needed.
                continue
            # Absolute link, target outside scan_root: the happy path.
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
            mounts.append(f"{target}:{target}:rw")

    if bad_abs_internal or bad_rel_external:
        lines = ["follow_symlinks: found symlinks that won't resolve inside the container:"]
        if bad_abs_internal:
            lines.append("")
            lines.append(
                f"  Absolute links pointing inside {scan_root} "
                "(the container mounts this directory at a different absolute "
                "path; rewrite each as a relative link):"
            )
            for p, link in bad_abs_internal:
                lines.append(f"    {p} -> {link}")
        if bad_rel_external:
            lines.append("")
            lines.append(
                f"  Relative links escaping {scan_root} "
                "(the relative climb resolves to a different path inside the "
                "container; rewrite each as an absolute link):"
            )
            for p, link in bad_rel_external:
                lines.append(f"    {p} -> {link}")
        lines.append("")
        lines.append("Fix the offending links, or set follow_symlinks: false in .hatchery/docker.yaml.")
        ui.error("\n".join(lines))
        sys.exit(1)

    return mounts


def _git_worktree_mounts(repo: Path, name: str, container_root: str) -> list[str]:
    """Return the layered -v flags for one repo + worktree pair (pre-worktree portion).

    Produces the read-only repo root + targeted read-write .git sub-mounts that
    protect the main branch while allowing the hatchery/<name> worktree's git
    metadata to be modified.  The worktree directory itself and any git-pointer
    shadow file are NOT included — callers append those afterwards (so sentinel
    files can be inserted between the .git layers and the worktree mount if needed).

    This function is intentionally repo-agnostic: pass ``CONTAINER_REPO_ROOT``
    for the primary repo or ``/includes/<basename>`` for an included secondary repo.
    """
    git_dir = repo / ".git"
    mounts = [
        f"{repo}:{container_root}:ro",  # repo root ro; .git overridden below
        f"{git_dir}:{container_root}/.git:rw",  # unlock .git/ root for lock files
        f"{git_dir / 'objects'}:{container_root}/.git/objects:rw",
    ]
    # Mount the entire hatchery/ ref directory rw so git can create .lock sidecar
    # files alongside the branch ref during commits.
    hatchery_refs = git_dir / "refs" / "heads" / "hatchery"
    if hatchery_refs.exists():
        mounts.append(f"{hatchery_refs}:{container_root}/.git/refs/heads/hatchery:rw")
    logs_dir = git_dir / "logs"
    if logs_dir.exists():
        mounts.append(f"{logs_dir}:{container_root}/.git/logs:rw")
    # Only this task's worktree git metadata is writable; other worktrees' metadata
    # is protected by the ro parent mount (prevents `git worktree prune` damage).
    worktree_meta = git_dir / "worktrees" / name
    if worktree_meta.exists():
        mounts.append(f"{worktree_meta}:{container_root}/.git/worktrees/{name}:rw")
    return mounts


def build_mounts(
    meta: SessionMeta,
    backend: agent.AgentBackend,
    session_dir: Path,
    config: DockerConfig,
    *,
    git_sentinel_files: list[tuple[Path, str]] | None = None,
    worktree_git_ptr: Path | None = None,
    include_entries: list[IncludeEntry] | None = None,
) -> list[str]:
    """Return the -v flags for a session's container.

    Branches on ``meta.no_worktree``:
      * False (default): layered git-worktree mounts — repo ro, targeted .git
        sub-dirs rw, sentinel files rw, worktree rw, container-relative .git
        pointer.
      * True: ``meta.worktree`` mounted at ``/workspace``, no git metadata.

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
    if meta.no_worktree:
        cwd = meta.worktree_path
        mounts = (
            [f"{meta.worktree}:/workspace:rw"]
            + _default_home_mounts()
            + backend.home_mounts(session_dir)
            + _construct_docker_mounts(config)
        )
        if config.clipboard_images:
            mounts.append(_clipboard_image_mount(session_dir))
        if config.follow_symlinks:
            mounts.extend(_construct_symlink_mounts(cwd, mounts))
    else:
        worktree_rel = meta.worktree_path.relative_to(meta.repo_path)
        container_worktree = f"{CONTAINER_REPO_ROOT}/{worktree_rel}"

        mounts = _git_worktree_mounts(meta.repo_path, meta.name, CONTAINER_REPO_ROOT)

        # git writes these into .git/ root during normal commits; use per-task
        # sentinel files so .git/ root stays ro.
        for host_file, git_filename in git_sentinel_files or []:
            mounts.append(f"{host_file}:{CONTAINER_REPO_ROOT}/.git/{git_filename}:rw")

        mounts.append(f"{meta.worktree}:{container_worktree}:rw")

        # Shadow the worktree's .git pointer file with a container-path-aware
        # copy. Must come after the worktree:rw mount so Linux VFS lets the
        # file mount win.
        if worktree_git_ptr is not None:
            mounts.append(f"{worktree_git_ptr}:{container_worktree}/.git:rw")
        mounts.extend(_default_home_mounts())
        mounts.extend(backend.home_mounts(session_dir))
        mounts.extend(_construct_docker_mounts(config))
        if config.clipboard_images:
            mounts.append(_clipboard_image_mount(session_dir))
        if config.follow_symlinks:
            mounts.extend(_construct_symlink_mounts(meta.worktree_path, mounts))

    if include_entries:
        mounts.extend(_docker_mounts_includes(include_entries, meta.name, session_dir, no_worktree=meta.no_worktree))
    return mounts


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
    """Shim: delegate to ``build_mounts`` for the worktree case.

    Kept for callers (and tests) that still construct mounts from positional
    repo / worktree / name. Will be removed once all callers migrate to
    build_mounts(meta, ...).
    """
    meta = SessionMeta(name=name, repo=str(repo), worktree=str(worktree), no_worktree=False)
    return build_mounts(
        meta,
        backend,
        session_dir,
        config,
        git_sentinel_files=git_sentinel_files,
        worktree_git_ptr=worktree_git_ptr,
    )


def docker_mounts_no_worktree(
    cwd: Path,
    backend: agent.AgentBackend,
    session_dir: Path,
    config: DockerConfig,
) -> list[str]:
    """Shim: delegate to ``build_mounts`` for the no-worktree case.

    The placeholder name doesn't appear in the returned mounts because
    no_worktree=True skips the worktree-specific paths.
    """
    meta = SessionMeta(name="-", repo=str(cwd), worktree=str(cwd), no_worktree=True)
    return build_mounts(meta, backend, session_dir, config)


def _docker_mounts_includes(
    include_entries: list[IncludeEntry],
    name: str,
    session_dir: Path,
    no_worktree: bool,
) -> list[str]:
    """Return -v flags for paths included via --include / --include-rw / --include-ro.

    Each path is mounted at /includes/<basename>/.  If two included paths share
    a basename the second gets a numeric suffix (e.g. api-1).

    mode="worktree": For git repos with a hatchery/<name> worktree the same
    layered mount strategy as the primary repo is applied (root:ro, targeted
    .git sub-dirs:rw, worktree:rw) and a corrected .git pointer file is
    written to *session_dir* and bind-mounted over the worktree's .git file.

    mode="rw" or mode="ro": Simple bind-mount with the corresponding access
    mode.  No worktree is expected or created.

    In no-worktree mode all entries fall back to a simple mount using their
    access mode (worktree entries are treated as rw).
    """
    mounts: list[str] = []
    used_basenames: set[str] = set()

    for entry in include_entries:
        path = entry.path
        basename = unique_basename(path.name, used_basenames)
        used_basenames.add(basename)
        container_path = f"{CONTAINER_INCLUDES_ROOT}/{basename}"

        if entry.mode == "worktree" and not no_worktree:
            is_git = (path / ".git").exists()
            if is_git:
                worktree = path / WORKTREES_SUBDIR / name
                if worktree.exists():
                    # Layered mounts: root ro + targeted .git rw (same as primary repo)
                    mounts.extend(_git_worktree_mounts(path, name, container_path))
                    container_worktree = f"{container_path}/.hatchery/worktrees/{name}"
                    mounts.append(f"{worktree}:{container_worktree}:rw")
                    # Rewrite .git pointer to use container-relative path
                    git_ptr_file = session_dir / f"git_ptr_include_{basename}"
                    git_ptr_file.write_text(f"gitdir: {container_path}/.git/worktrees/{name}\n")
                    mounts.append(f"{git_ptr_file}:{container_worktree}/.git:rw")
                    continue
                logger.warning(
                    "include worktree not found for %s (expected %s); "
                    "falling back to plain rw mount — branch isolation unavailable.",
                    path,
                    worktree,
                )
            # Git repo without worktree, or plain dir in worktree mode → rw
            mounts.append(f"{path}:{container_path}:rw")
        else:
            # reference mode (ro/rw), or no_worktree fallback
            access = entry.mode if entry.is_reference() else "rw"
            mounts.append(f"{path}:{container_path}:{access}")

    return mounts


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


def clipboard_image_dir(session_dir: Path) -> Path:
    """Per-task host directory where pasted clipboard images are saved.

    Bind-mounted at the same absolute path inside the container so a file
    written here by the host-side PTY proxy resolves identically when the
    agent reads it.
    """
    return session_dir / "clipboard"


def _clipboard_image_mount(session_dir: Path) -> str:
    """Return the -v flag for the per-task clipboard images directory.

    Mounts at the identical host path inside the container so the file path
    typed into the agent's stdin (a host path) resolves byte-for-byte.
    """
    d = clipboard_image_dir(session_dir)
    d.mkdir(parents=True, exist_ok=True)
    return f"{d}:{d}:rw"


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
        if _interactive:
            subprocess.run(cmd)
            return None
        return subprocess.run(cmd, capture_output=True, text=True)

    # Append the full agent command (binary + args, docker-mode already applied).
    cmd += agent_cmd

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
    files + a container-relative .git pointer, then mounts the worktree
    under /repo/.hatchery/worktrees/<name>; no-worktree mode mounts the
    cwd as /workspace and skips all git metadata.

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

    git_sentinels: list[tuple[Path, str]] | None = None
    git_ptr: Path | None = None
    if meta.no_worktree:
        container_workdir = "/workspace"
        container_repo = "/workspace"
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

        # Rewrite the worktree .git pointer to use the container-relative path.
        git_ptr = session_dir / "git_ptr"
        git_ptr.write_text(f"gitdir: {CONTAINER_REPO_ROOT}/.git/worktrees/{meta.name}\n")

        worktree_rel = meta.worktree_path.relative_to(meta.repo_path)
        container_workdir = f"{CONTAINER_REPO_ROOT}/{worktree_rel}"
        container_repo = CONTAINER_REPO_ROOT
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
        worktree_git_ptr=git_ptr,
        include_entries=include_entries,
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
    The repo is mounted read-only at /repo.

    Caller resolves *image_name* — typically ``sessions.image_name(repo, "sandbox")``.
    """
    build_docker_image(repo, repo, image_name, backend, runtime=runtime, no_cache=no_cache)
    mounts = [f"{repo}:{CONTAINER_REPO_ROOT}:rw"] + _default_home_mounts() + _construct_docker_mounts(config)

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
                workdir=CONTAINER_REPO_ROOT,
                hatchery_repo=CONTAINER_REPO_ROOT,
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
