"""Container mount descriptors shared by ``agents`` and ``docker``.

Lives at the top of the package — not inside ``agents/`` — because both
``agents/*`` (backends that return a list of mounts) and ``docker.py``
(internal mount construction for the repo/worktree, kubeconfig, includes,
etc.) traffic in the same ``Mount`` type.  Keeping it agent-neutral
avoids the awkward upward dependency ``docker.py → agents.Mount``.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MountMode = Literal["ro", "rw", "tmpfs"]


@dataclass(frozen=True)
class Mount:
    """A mount specification for the sandbox container.

    Converted into the appropriate Docker CLI flag(s) (``-v`` for bind
    mounts and named volumes, ``--tmpfs`` for tmpfs) by
    :func:`mount_to_docker_args` just before the container starts.

    Attributes:
        src: Host filesystem path for bind mounts; named-volume name when
            ``volume=True``; ``None`` for tmpfs.
        mode: ``"ro"`` or ``"rw"`` for bind/volume mounts; ``"tmpfs"`` for
            an in-memory tmpfs at *dst*.  No default — callers must state
            intent explicitly so a missed argument can't silently promote
            a mount to read-write.
        dst: Container target path.  Defaults to ``None`` which means
            "mirror src" (same path on both sides) for bind mounts.
            Required for tmpfs and named volumes.
        volume: When ``True``, *src* is treated as a docker/podman named
            volume rather than a host path. The runtime is responsible for
            creating the volume before the container starts.
    """

    src: str | Path | None
    mode: MountMode
    dst: str | None = None
    volume: bool = False


def mount_to_docker_args(m: Mount) -> list[str]:
    """Convert a Mount into Docker CLI flag(s) suitable for ``docker run``."""
    if m.mode == "tmpfs":
        if m.dst is None:
            raise ValueError(f"tmpfs Mount requires dst: {m!r}")
        return ["--tmpfs", m.dst]
    if m.src is None:
        raise ValueError(f"bind Mount requires src: {m!r}")
    if m.volume:
        if m.dst is None:
            raise ValueError(f"volume Mount requires dst: {m!r}")
        return ["-v", f"{m.src}:{m.dst}:{m.mode}"]
    dst = m.dst if m.dst is not None else str(m.src)
    return ["-v", f"{m.src}:{dst}:{m.mode}"]
