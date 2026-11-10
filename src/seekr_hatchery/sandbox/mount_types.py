"""Container mount descriptors shared by ``agents`` and ``sandbox.launch``.

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

These dependency-leaf descriptors can be imported by agents and sidecars
without initializing sandbox launch coordination.
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
    """Bind a host path into the container at ``dst``.

    ``follow_links`` asks the launch path to make symlinks *inside* ``src``
    resolve once mounted. A symlink stores a path, and that path only means
    the same thing on both sides of the mount when ``dst`` mirrors ``src``.
    Repo paths are mirrored deliberately; ``$HOME`` is not, since the
    container's is ``/home/hatchery``. So set this on any directory whose
    contents a user might manage with a dotfiles repo — agent config
    directories (``~/.claude/skills``, ``~/.codex/prompts``, ...) and, via
    ``docker.yaml``'s ``follow_symlinks``, the worktree and any extra
    mounts.

    Note what needs *no* flag, because ``-v`` resolves a mount's own source
    path: a bind whose ``src`` is itself a symlink — file or directory —
    already lands on the target's inode, so nothing dangles. The flag is
    only about links found *inside* the mounted tree, which survive
    verbatim.

    Setting it on a non-directory source is therefore harmless and inert —
    the walk finds nothing to do — so callers may declare it uniformly across
    a group of config paths without checking which of them are files.

    Links are looked for at any depth, pruning ``.git``, ``node_modules``
    and similar (see :data:`seekr_hatchery.sandbox.mount_links.SKIP_DIRS`), so a
    link nested inside a real subdirectory resolves too. That costs a walk
    of the tree per launch, which is why it is opt-in rather than always on.

    The mount itself is unaffected: the directory is still bound whole and
    nothing is mounted at a link's own path, so the link stays a link. The
    flag only asks for *additional* mounts that make the links'
    destinations exist. It is purely declarative — the mechanism lives in
    :func:`seekr_hatchery.sandbox.mount_links.expand_link_mounts`, which every
    mount-building path calls, so this module stays free of filesystem
    knowledge and any mount source (agent backends, ``docker.yaml``
    entries, ``--include``) can opt in with one field.

    ``mount_to_docker_args`` ignores the flag, so an unexpanded mount still
    serialises as an ordinary bind: the failure mode of forgetting to
    expand is "symlinks dangle as before", never a malformed flag.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["BIND"] = "BIND"
    src: Path
    dst: str
    mode: MountMode = "RW"
    follow_links: bool = False


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
    file-shaped mounts get special handling at launch time: the volume is
    mounted at a sidecar directory (``dst + ".vol"``) and a symlink is
    injected from ``dst`` into that directory before the agent starts.
    Subpath mounts (``--mount type=volume,...,subpath=...``) would also
    work on Podman but are not supported on Docker, so the symlink is the
    sole mechanism. See ``mount_to_docker_args`` and
    ``file_mount_prestart_cmds`` for the launch-side implementation.

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
