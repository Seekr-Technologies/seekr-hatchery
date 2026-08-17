"""Make symlinks inside a bind-mounted host directory resolve in the container.

A symlink stores a *path*, and a mount only preserves the meaning of that path
when its container destination equals its host source. Hatchery mirrors repo
paths host→container precisely so that holds for the worktree. It does not hold
for config directories: host ``$HOME`` is wherever the user lives while the
container's is ``/home/hatchery``, so a dotfiles-managed
``~/.claude/skills/my-skill -> ~/.dotfiles/claude/skills/my-skill`` points at a
path with no container-side existence, and the skill is invisible to the agent
even though ``skills/`` itself is mounted.

The rule, which is the whole mechanism:

    Mount the link's resolved host target at whatever path the link
    resolves to *as seen from inside the container*.

Nothing is mounted at the link's own path, so the link stays a link and the
directory being scanned stays bound whole. Both link shapes fall out of the one
computation:

- **Absolute** link — the container-side resolution *is* the stored host path,
  so the target is mounted at its own host path.
- **Relative** link — the resolution is relative to the link's *container*
  directory, so the target is mounted under ``/home/hatchery/...``. This is the
  case that needs the machinery, and it is not exotic: GNU stow and friends
  create relative links by default.

For a directory mounted at its own host path the two collapse into one, because
``normpath(container_dir + raw) == normpath(host_dir + raw)`` when the two dirs
are equal. That is why this single pass replaced the older worktree-only
scanner: mirrored mounts are the degenerate case of the general rule, not a
separate feature.

Note what needs nothing at all: a bind whose ``src`` is *itself* a symlink,
file or directory. ``mount(2)`` follows symlinks in the source path — you
cannot bind-mount a link, only the inode it resolves to — so the container gets
the target directly and no link survives to dangle. Only links found *inside*
the mounted tree survive verbatim, and those are this module's whole concern.

A link whose container-side destination is already provided by some other mount
needs nothing and is skipped. That covers links pointing back into the scanned
directory itself, and it incidentally guarantees this never mounts anything
*inside* a directory being scanned, where a destination component could itself
be a dangling link.

Multi-hop chains resolve in one jump: the final target is mounted at the *first*
hop's destination, so no intermediate hop is ever consulted.

Scope note: the guards here are about *interpreting a link* — unresolvable,
dangling, not a regular file or directory. Whether a resulting mount is safe to
perform at all (system paths, host reachability) is not decided here; a bad
mount can equally come from a user asking for one directly, so that check
belongs to one validation pass over the finished mount list. See
``docker._validate_mounts``.
"""

import logging
import os
import posixpath
from collections.abc import Iterator
from pathlib import Path

from seekr_hatchery.mount import BindMount, Mount

logger = logging.getLogger(__name__)

# Directories the walk doesn't descend into — large, and unlikely to hold
# meaningful user-authored symlinks.
SKIP_DIRS: frozenset[str] = frozenset(
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


def expand_link_mounts(mounts: list[Mount]) -> list[Mount]:
    """Add the mounts that make ``follow_links`` directories' symlinks resolve.

    Every input mount is preserved; ``follow_links`` binds simply gain
    companions. Emitted mounts carry ``follow_links=False`` and the flag is
    cleared on the originals, so calling this twice is a no-op.
    """
    # Container paths some mount already provides. A link landing inside one of
    # these resolves as-is, so it needs no companion.
    provided = [m.dst for m in mounts]

    out: list[Mount] = []
    extra: list[BindMount] = []
    for m in mounts:
        if not isinstance(m, BindMount) or not m.follow_links:
            out.append(m)
            continue
        out.append(m.model_copy(update={"follow_links": False}))
        for container_path, target in _link_targets(m, provided):
            extra.append(BindMount(src=target, dst=container_path, mode=m.mode))

    # Two links onto one container path is pathological; first declaration wins.
    seen: set[str] = set()
    for e in extra:
        if e.dst not in seen:
            seen.add(e.dst)
            out.append(e)
    return out


def _link_targets(m: BindMount, provided: list[str]) -> Iterator[tuple[str, Path]]:
    """Yield ``(container_path, host_target)`` for each usable link in *m*."""
    src = m.src.expanduser()
    if not src.is_dir():
        # is_dir() follows links, so a symlinked directory is scanned through.
        # A non-directory needs nothing: `-v` resolves the mount's own source.
        # This is an early-out, not the thing that makes the flag inert on a
        # file — os.walk on a non-directory yields nothing regardless, which is
        # what lets callers declare the flag uniformly over a mixed group.
        return

    for link, container_dir in _walk(src, m.dst):
        container_path = _container_destination(link, container_dir)
        if container_path is None:
            continue
        if any(_within(container_path, d) for d in provided):
            continue  # Already mounted, or inside something already mounted.
        if any(_within(d, container_path) for d in provided):
            # A proper ancestor of an existing mount: over-broad, and it would
            # bury whatever else lives at that path in the image.
            logger.debug("follow_links: skipping %s, a parent of an existing mount", container_path)
            continue
        target = _resolved_target(link)
        if target is None:
            continue
        yield container_path, target


def _walk(src: Path, dst: str) -> Iterator[tuple[Path, str]]:
    """Yield ``(link, container_dir)`` for every symlink under *src*.

    Any depth, so a link nested inside a real subdirectory is found too;
    :data:`SKIP_DIRS` keeps that affordable on a repo-sized tree.
    """

    def _on_err(exc: OSError) -> None:
        logger.debug("follow_links: walk error: %s", exc)

    for dirpath, dirnames, filenames in os.walk(src, followlinks=False, onerror=_on_err):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        # Symlinked directories land in dirnames but are not descended into,
        # since followlinks is False.
        rel = os.path.relpath(dirpath, src)
        container_dir = dst if rel == os.curdir else posixpath.join(dst, *rel.split(os.sep))
        for entry in list(dirnames) + filenames:
            link = Path(dirpath) / entry
            if link.is_symlink():
                yield link, container_dir


def _container_destination(link: Path, container_dir: str) -> str | None:
    """Where *link* points, as resolved from inside the container.

    *container_dir* is the link's parent directory on the container side.
    Only the first hop is read: mounting the final target here collapses any
    chain.
    """
    try:
        raw = os.readlink(link)
    except OSError as exc:
        logger.debug("follow_links: cannot read link %s (%s)", link, exc)
        return None

    # Container paths are always POSIX, regardless of the host.
    joined = raw if posixpath.isabs(raw) else posixpath.join(container_dir, raw)
    dst = posixpath.normpath(joined)

    if ":" in dst:
        # `-v src:dst:mode` is colon-delimited; a colon would corrupt the flag.
        logger.debug("follow_links: skipping colon in container path %s", dst)
        return None
    if dst == "/" or not posixpath.isabs(dst):
        # normpath leaves a leading `..` in place when it escapes the root.
        logger.debug("follow_links: skipping unusable container path %s", dst)
        return None
    return dst


def _resolved_target(link: Path) -> Path | None:
    """Return *link*'s final host target, or ``None`` if it cannot be mounted."""
    try:
        target = link.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        # Broken link, symlink loop (ELOOP / RuntimeError), permission error.
        logger.debug("follow_links: skipping unresolvable %s (%s)", link, exc)
        return None

    if not target.exists():
        # Belt-and-braces after strict resolve. Load-bearing because
        # `-v <missing>:<dst>` makes docker/podman *create* the source as a
        # root-owned directory on the host — a dangling dotfile link would
        # litter the user's filesystem. Kept here rather than in the validator
        # because a mount hand-declared elsewhere may legitimately have a
        # source created later in the launch.
        logger.debug("follow_links: skipping missing target %s", target)
        return None

    if not target.is_file() and not target.is_dir():
        logger.debug("follow_links: skipping non-regular target %s", target)
        return None

    if ":" in str(target):
        logger.debug("follow_links: skipping colon in host path %s", target)
        return None

    return target


def _within(path: str, root: str) -> bool:
    """True when container *path* is *root* or sits underneath it."""
    return path == root or path.startswith(root.rstrip("/") + "/")
