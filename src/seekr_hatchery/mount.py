"""Container mount descriptors shared by ``agents`` and ``docker``.

Mounts are a tagged union of three Pydantic models — ``BindMount`` /
``VolumeMount`` / ``TmpfsMount``, discriminated on ``kind``. The union
is exposed as the ``Mount`` type alias so callers can declare
``list[Mount]`` and pattern-match on ``isinstance`` or ``.kind``.

Why a tagged union (and why Pydantic):

- The three variants share very little (``BindMount`` has a host path,
  ``VolumeMount`` carries a seed callable, ``TmpfsMount`` carries
  almost nothing). A flat dataclass would collapse them into one
  shape with mutually-exclusive optional fields and runtime asserts —
  workable but easy to get wrong.
- A ``Literal["BIND" | "VOLUME" | "TMPFS"]`` discriminator lets static
  tools narrow the variant; Pydantic's discriminator handles
  round-trips when a Mount comes from YAML.

Mounts are agent-neutral (both ``agents/*`` and ``docker.py`` traffic
in them), so they live at the top of the package, not inside
``agents/``.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

MountMode = Literal["RO", "RW"]


@dataclass(frozen=True)
class SeedContext:
    """Per-launch values passed to a ``VolumeMount.seed`` callable.

    Held intentionally minimal so backends don't reach into half the
    world to synthesise content. Add fields here only when a real seed
    needs them.
    """

    session_dir: Path
    proxy_token: str
    container_workdir: str


class BindMount(BaseModel):
    """Bind a host path into the container at ``dst``."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["BIND"] = "BIND"
    src: Path
    dst: str
    mode: MountMode = "RW"


class VolumeMount(BaseModel):
    """A docker/podman named volume mounted into the container.

    ``name`` is the *logical* spec name (e.g. ``"app-state"``) when
    declared by a backend; the launch path resolves it to a runtime
    volume name (``{meta.container_name}-vol-{name}``) before the mount
    is serialised to CLI args. ``mount_to_docker_args`` uses ``name``
    verbatim — callers are responsible for resolving before serialising.

    ``is_file`` flags single-file shapes (e.g. an agent config JSON the
    agent looks for at a fixed in-container path).
    Named volumes always present as directories on the kernel side, so
    file-shaped mounts get special handling at launch time — either a
    subpath mount (`--mount type=volume,...,subpath=...`) or a symlink
    injected from ``dst`` into the volume's parent mount point. Which
    mechanism wins is a launch-path concern, not a backend concern.

    ``seed`` produces the volume's contents on first launch. For
    ``is_file=True`` mounts return raw ``bytes`` (the file body);
    otherwise return ``{relative_path: bytes}``. The callable runs on
    the host before the agent container starts and has full host
    filesystem access.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    kind: Literal["VOLUME"] = "VOLUME"
    name: str
    dst: str
    mode: MountMode = "RW"
    is_file: bool = False
    seed: Callable[[SeedContext], Mapping[str, bytes] | bytes] | None = None
    # When True (default), ``name`` is the *logical* spec name and the
    # launch path resolves it to ``{meta.container_name}-vol-{name}`` so
    # the volume is per-task. Backends declaring per-task state want
    # this. Set False for cross-task shared volumes (e.g. user-config
    # cache volumes from docker.yaml) where the caller has chosen the
    # full runtime volume name themselves.
    task_scoped: bool = True


class TmpfsMount(BaseModel):
    """An ephemeral tmpfs at ``dst`` (in-memory; container-scoped)."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["TMPFS"] = "TMPFS"
    dst: str


Mount = Annotated[
    BindMount | VolumeMount | TmpfsMount,
    Field(discriminator="kind"),
]


def mount_to_docker_args(m: BindMount | VolumeMount | TmpfsMount) -> list[str]:
    """Convert a Mount into Docker/Podman CLI flag(s) for ``run``.

    For ``VolumeMount``, ``m.name`` is used verbatim as the runtime
    volume name — the launch path resolves the per-task name before
    calling this function (via ``model_copy(update={"name": ...})``).

    File-shaped volume mounts (``is_file=True``) emit ``--mount
    type=volume,subpath=…`` rather than ``-v``: that's the only way to
    surface a single file from a volume at a fixed in-container file
    path. Requires docker ≥ 25 / podman ≥ 4.7. Whether the resulting
    mount survives an agent's atomic-rename write pattern is the open
    question task #53 exists to answer.
    """
    if isinstance(m, TmpfsMount):
        return ["--tmpfs", m.dst]
    if isinstance(m, BindMount):
        return ["-v", f"{m.src}:{m.dst}:{m.mode.lower()}"]
    if isinstance(m, VolumeMount):
        if m.is_file:
            parts = [
                "type=volume",
                f"source={m.name}",
                f"target={m.dst}",
                f"subpath={Path(m.dst).name}",
            ]
            if m.mode == "RO":
                parts.append("readonly")
            return ["--mount", ",".join(parts)]
        return ["-v", f"{m.name}:{m.dst}:{m.mode.lower()}"]
    raise TypeError(f"unknown mount type: {type(m).__name__}")
