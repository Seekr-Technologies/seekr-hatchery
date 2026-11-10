"""Container runtime adapters, command rendering, and process execution."""

import logging
import shlex
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path

import seekr_hatchery.pty_proxy as pty_proxy
import seekr_hatchery.sandbox.mount_types as mount_types
import seekr_hatchery.sandbox.spec as spec
import seekr_hatchery.stream_interceptor as stream_interceptor
import seekr_hatchery.stream_interceptor.interceptors.clipboard_image as clipboard_image
import seekr_hatchery.ui as ui
import seekr_hatchery.utils as utils

# Keep diagnostic output and logger-level selection stable across relocation.
logger = logging.getLogger("seekr_hatchery.docker")


def _sidecar_dir(dst: str) -> str:
    """Return the container-side sidecar directory path for a file-shaped mount_types.VolumeMount.

    Named volumes always present as directories to the kernel, so a file-shaped
    mount (``is_file=True``) cannot be mounted directly at ``dst``.  Instead we
    mount the volume at ``dst + ".vol"`` and create a symlink from ``dst`` into
    that directory before the agent starts (see ``_file_mount_prestart_cmds``).
    """
    return dst + ".vol"


def _mount_to_args(m: mount_types.BindMount | mount_types.VolumeMount | mount_types.TmpfsMount) -> list[str]:
    """Convert a mount_types.Mount into Docker/Podman CLI flag(s) for ``run``.

    For ``mount_types.VolumeMount``, ``m.name`` is used verbatim as the runtime
    volume name — the launch path resolves the per-task name before
    calling this function (via ``model_copy(update={"name": ...})``).

    File-shaped volume mounts (``is_file=True``) mount the volume at a sidecar
    directory (``dst + ".vol"``) rather than at ``dst`` directly.  Docker and
    Podman both treat named-volume mount points as directories, so mounting at
    the file path itself would shadow any parent directory with an empty dir.
    A symlink from ``dst`` into the sidecar is injected at container startup via
    ``_file_mount_prestart_cmds``.
    """
    if isinstance(m, mount_types.TmpfsMount):
        return ["--tmpfs", m.dst]
    if isinstance(m, mount_types.BindMount):
        return ["-v", f"{m.src}:{m.dst}:{m.mode.lower()}"]
    if isinstance(m, mount_types.VolumeMount):
        if m.is_file:
            sidecar = _sidecar_dir(m.dst)
            return ["-v", f"{m.name}:{sidecar}:{m.mode.lower()}"]
        return ["-v", f"{m.name}:{m.dst}:{m.mode.lower()}"]
    raise TypeError(f"unknown mount type: {type(m).__name__}")


def _file_mount_prestart_cmds(mounts: list[mount_types.Mount]) -> list[str]:
    """Return shell commands that wire up symlinks for ``is_file=True`` VolumeMounts.

    For each file-shaped volume mount, the volume is mounted at a sidecar
    directory (``dst + ".vol"``).  The agent expects the file at ``dst``, so we
    emit a ``ln -sf`` command that creates (or replaces) that symlink before the
    agent process starts.  These commands must run inside the container before
    the agent binary is exec'd.
    """
    cmds = []
    for m in mounts:
        if isinstance(m, mount_types.VolumeMount) and m.is_file:
            sidecar = _sidecar_dir(m.dst)
            filename = Path(m.dst).name
            cmds.append(f"ln -sf {sidecar}/{filename} {m.dst}")
    return cmds


def _wrap_cmd_for_file_mounts(cmd: list[str], mounts: list[mount_types.Mount]) -> list[str]:
    """Wrap *cmd* with the symlink setup needed by any file-shaped VolumeMounts.

    Returns *cmd* unchanged when no file-shaped mounts are present. Otherwise
    returns ``["sh", "-c", "<symlink-setup> && exec <cmd>"]`` so the symlinks
    are in place before the agent process starts. Used by the launch path for
    both the agent command and any command-override path.
    """
    prestart = _file_mount_prestart_cmds(mounts)
    if not prestart:
        return cmd
    setup = " && ".join(prestart)
    return ["sh", "-c", f"{setup} && exec {shlex.join(cmd)}"]


class ContainerRuntime(ABC):
    """Abstract container runtime — mirrors ``AgentBackend``.

    Each concrete subclass owns the engine divergence (binary name, userns
    flags, OOM messaging).  ``render_run_argv(spec)`` turns a
    ``spec.ContainerSpec`` into the final ``docker``/``podman run`` argv.

    The only divergence point is :meth:`_engine_flags`, which subclasses
    override to inject engine-specific flags (e.g. Podman's ``--userns=keep-id``
    and ``--security-opt label=disable``).  ``render_run_argv`` and ``run``
    are concrete on the base.
    """

    @property
    @abstractmethod
    def binary(self) -> str:
        """CLI binary name for this runtime (e.g. ``"docker"``, ``"podman"``)."""

    @staticmethod
    @abstractmethod
    def available() -> bool:
        """Return True if this runtime's daemon/CLI is reachable."""

    @abstractmethod
    def oom_hint(self, returncode: int) -> str | None:
        """Return a human-readable OOM hint for *returncode*, or ``None``."""

    # ── Engine divergence seam ───────────────────────────────────────────

    def _engine_flags(self, spec: spec.ContainerSpec) -> list[str]:
        """Return engine-specific flags to inject before ``-w`` in the argv.

        Default: no extra flags (Docker).  Subclasses override to add
        engine-specific flags like ``--userns=keep-id`` or
        ``--security-opt label=disable``.

        Tests patch this method on a runtime instance (or the class) to
        suppress userns in nested-container scenarios without globally
        patching ``sys.platform``.
        """
        return []

    # ── Shared helpers ───────────────────────────────────────────────────

    def _ensure_volumes(self, mounts: list[mount_types.Mount]) -> None:
        """Create any named volumes referenced by *mounts* if they don't exist."""
        seen: set[str] = set()
        for m in mounts:
            if not isinstance(m, mount_types.VolumeMount) or m.name in seen:
                continue
            seen.add(m.name)
            if utils.run([self.binary, "volume", "inspect", m.name], check=False).returncode == 0:
                continue
            logger.debug("creating %s volume: %s", self.binary, m.name)
            utils.run([self.binary, "volume", "create", m.name])

    # ── Concrete rendering + execution (shared by all runtimes) ──────────

    def render_run_argv(self, spec: spec.ContainerSpec) -> list[str]:
        """Assemble the full ``<binary> run ...`` argv from *spec*."""
        cmd: list[str] = [self.binary, "run"]
        if spec.rm:
            cmd.append("--rm")
        if spec.init:
            cmd.append("--init")
        if spec.interactive:
            cmd += ["-it"]
        for m in spec.mounts:
            cmd += _mount_to_args(m)
        for key, val in spec.env.items():
            cmd += ["-e", f"{key}={val}"]
        for host in spec.add_hosts:
            cmd += [f"--add-host={host}"]
        if spec.container_name is not None:
            cmd += ["--name", spec.container_name]
        cmd += self._engine_flags(spec)
        cmd += ["-w", spec.workdir]
        cmd += _render_caps(spec)
        cmd += _render_security_opt(spec)
        cmd += _render_devices(spec)
        cmd += [spec.image]
        return cmd

    def run(
        self,
        spec: spec.ContainerSpec,
        *,
        paste_interceptor: clipboard_image.PasteInterceptor | None = None,
    ) -> subprocess.CompletedProcess[str] | None:
        """Execute the container described by *spec*.

        For interactive agent launches, runs via ``_exec_agent`` (PTY proxy
        when a paste interceptor is present).  For command-override mode,
        runs via ``subprocess.run`` (with or without capture).
        """
        self._ensure_volumes(spec.mounts)
        cmd = self.render_run_argv(spec)

        if spec.command_override is not None:
            cmd += _wrap_cmd_for_file_mounts(spec.command_override, spec.mounts)
            logger.debug(
                "Launching %s container image=%r name=%r (command override)",
                self.binary,
                spec.image,
                spec.name,
            )
            if spec.interactive:
                subprocess.run(cmd)
                return None
            return subprocess.run(cmd, capture_output=spec.capture_output, text=True)

        cmd += _wrap_cmd_for_file_mounts(spec.command, spec.mounts)
        logger.debug(
            "Launching %s container image=%r name=%r workdir=%r",
            self.binary,
            spec.image,
            spec.name,
            spec.workdir,
        )
        returncode = _exec_agent(cmd, paste_interceptor)
        if returncode != 0:
            ui.warn(f"{self.binary} container exited with code {returncode}")
            hint = self.oom_hint(returncode)
            if hint:
                ui.info(hint)
        return None


class DockerRuntime(ContainerRuntime):
    """Docker engine backend."""

    @property
    def binary(self) -> str:
        return "docker"

    @staticmethod
    def available() -> bool:
        """Return True if the Docker daemon is reachable."""
        logger.debug("Checking Docker availability")
        try:
            result = utils.run(["docker", "info"], check=False)
        except FileNotFoundError:
            return False
        return result.returncode == 0

    def oom_hint(self, returncode: int) -> str | None:
        return None


class PodmanRuntime(ContainerRuntime):
    """Podman engine backend."""

    @property
    def binary(self) -> str:
        return "podman"

    @staticmethod
    def available() -> bool:
        """Return True if the Podman CLI is reachable."""
        logger.debug("Checking Podman availability")
        try:
            result = utils.run(["podman", "info"], check=False)
        except FileNotFoundError:
            return False
        return result.returncode == 0

    def _engine_flags(self, spec: spec.ContainerSpec) -> list[str]:
        # Podman-specific outer-container flags for rootless mount permissions.
        # --userns=keep-id maps the calling user to the same UID inside the
        # container so bind-mounted host files are writable by the hatchery user.
        # --security-opt label=disable suppresses SELinux/AppArmor label
        # confinement on mounts.
        flags: list[str] = []
        if sys.platform == "linux":
            flags += ["--userns=keep-id"]
        flags += ["--security-opt", "label=disable"]
        return flags

    def oom_hint(self, returncode: int) -> str | None:
        if returncode == 137:
            return (
                "Hint: the container was killed (OOM). Try increasing the Podman machine memory:\n"
                "  podman machine stop\n"
                "  podman machine set --memory 8192\n"
                "  podman machine start"
            )
        return None


# ── Spec rendering helpers (shared by all runtimes) ────────────────────────


def _render_caps(spec: spec.ContainerSpec) -> list[str]:
    """Render --cap-drop / --cap-add flags from *spec*."""
    cmd: list[str] = []
    for cap in spec.cap_drop:
        cmd += ["--cap-drop", cap]
    for cap in spec.cap_add:
        cmd += ["--cap-add", cap]
    return cmd


def _render_security_opt(spec: spec.ContainerSpec) -> list[str]:
    """Render --security-opt flags from *spec*."""
    cmd: list[str] = []
    for opt in spec.security_opt:
        cmd += ["--security-opt", opt]
    return cmd


def _render_devices(spec: spec.ContainerSpec) -> list[str]:
    """Render --device flags from *spec*."""
    cmd: list[str] = []
    for dev in spec.devices:
        cmd += ["--device", dev]
    return cmd


def detect_runtime() -> ContainerRuntime:
    """Return the preferred container runtime, or exit if none is available.

    Podman is preferred because it is rootless-native and its default seccomp
    profile allows the user-namespace syscalls needed for nested containers.

    If the Podman binary is installed but 'podman info' fails (e.g. the machine
    is not running on macOS), this is treated as an error rather than a silent
    fallback to Docker — the user installed Podman intentionally and should not
    be silently downgraded to the less-secure Docker runtime.
    """
    if PodmanRuntime.available():
        logger.debug("Using Podman as container runtime")
        return PodmanRuntime()
    if shutil.which("podman") is not None:
        msg = "Podman is installed but not running."
        if sys.platform == "darwin":
            msg += " Start it with: podman machine start"
            msg += "\n(or: podman machine init && podman machine start on first use)"
        ui.error(msg)
        sys.exit(1)
    if DockerRuntime.available():
        logger.debug("Using Docker as container runtime")
        return DockerRuntime()
    ui.error(".hatchery/dockerfile exists but neither Podman nor Docker is running.")
    ui.info("Start Podman/Docker or pass --no-docker to run without the sandbox.")
    sys.exit(1)


def _exec_agent(cmd: list[str], paste_interceptor: clipboard_image.PasteInterceptor | None) -> int:
    """Run the agent's ``docker run`` command, optionally under the PTY proxy.

    The PTY-proxy path activates only when a paste interceptor was provided
    AND stdin is a real TTY.  Non-TTY callers (CI, captured stdin) get the
    plain ``subprocess.run`` path so output behaviour stays unchanged.
    """
    if paste_interceptor is not None and sys.stdin.isatty():
        return pty_proxy.run_with_pty(cmd, stream_interceptor.InterceptorChain([paste_interceptor]))
    return subprocess.run(cmd).returncode
