"""Mount construction and host-path safety policy for sandbox launches."""

import logging
import sys
from pathlib import Path

import seekr_hatchery.agents as agent
import seekr_hatchery.branches as branches
import seekr_hatchery.constants as constants
import seekr_hatchery.includes as includes
import seekr_hatchery.models as models
import seekr_hatchery.sandbox.config as sandbox_config
import seekr_hatchery.sandbox.mount_links as mount_links
import seekr_hatchery.sandbox.mount_types as mount_types
import seekr_hatchery.ui as ui

# Keep diagnostic output and logger-level selection stable across relocation.
logger = logging.getLogger("seekr_hatchery.docker")


def _construct_config_mounts(config: sandbox_config.SandboxConfig) -> list[mount_types.Mount]:
    """Resolve a SandboxConfig into Mount objects.

    Expands ~ in host paths and silently skips entries whose host path does
    not exist on this machine (allows a shared config to list paths that are
    only present on some developers' machines).

    ``follow_symlinks`` applies to these the same as it does to the worktree:
    they are all directories the user asked for, so a symlink inside one
    should resolve in the sandbox for the same reason.
    """
    result: list[mount_types.Mount] = []
    for entry in config.mounts:
        parts = entry.split(":", 2)
        host = Path(parts[0]).expanduser()
        mode = parts[2] if len(parts) == 3 else "ro"
        if not host.exists():
            logger.debug("Custom mount host path does not exist, skipping: %s", host)
            continue
        result.append(
            mount_types.BindMount(
                src=str(host),
                dst=parts[1],
                mode=mode.upper(),
                follow_links=config.follow_symlinks,
            )
        )
    return result


# Prefix applied to user-declared volume names when forming the actual
# docker/podman volume name. Mirrors the namespacing already used elsewhere
# (e.g. prefixed task branch refs) and keeps these volumes visually distinct
# from any unrelated volumes the user may have on the host runtime.
_VOLUME_NAME_PREFIX = "hatchery-"


def _construct_volume_mounts(config: sandbox_config.SandboxConfig) -> list[mount_types.Mount]:
    """Resolve declared cache volumes into named-volume Mount objects.

    Each entry produces a Mount with ``volume=True`` so the runtime treats
    *src* as a named docker/podman volume rather than a host path. The
    actual volume is named ``hatchery-<name>``; ``_ensure_volumes`` creates
    it on the runtime before the container starts.
    """
    return [
        mount_types.VolumeMount(name=f"{_VOLUME_NAME_PREFIX}{v.name}", dst=v.path, mode="RW", task_scoped=False)
        for v in config.volumes
    ]


# Container directories whose contents are provided by the image or the kernel.
# Mounting over them would shadow critical binaries/libraries or replace special
# filesystems (/proc, /sys, /dev). /tmp, /var, /home, /opt are intentionally NOT
# blocked — users legitimately keep data there.
SYSTEM_MOUNT_BLOCKLIST: tuple[Path, ...] = (
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

# Paths a podman machine shares with the host on macOS. A bind whose source
# lies outside these is invisible to the VM and comes up as an empty directory.
_MACOS_SHARED_ROOTS: tuple[Path, ...] = (Path("/Users"), Path("/private/tmp"))


def check_host_path_safe_for_mount(repo: Path) -> None:
    """Reject host repo paths that would shadow load-bearing container paths.

    Container paths now mirror host paths so any path-keyed state inside the
    container (agent project dirs, lockfiles, symlink targets) matches what a
    native run would produce. That breaks if the host repo path collides with
    a directory the container image depends on — e.g., /usr, /etc, or the
    container's home dir itself.

    A hard error rather than :func:`_validate_mounts`'s drop-and-warn: there is
    no sensible sandbox to launch if the repo itself cannot be mounted, whereas
    dropping one stray mount still leaves a working one.

    Subpaths under these (e.g. /home/hatchery/myrepo) are fine — they just add
    a subdir to the container's home.
    """
    blocklist: tuple[Path, ...] = SYSTEM_MOUNT_BLOCKLIST + (
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


def _validate_mounts(mounts: list[mount_types.Mount]) -> list[mount_types.Mount]:
    """Drop mounts that must not be performed; warn about ones that may not work.

    The single place mount safety is decided, for every mount regardless of
    where it came from. A dangerous mount can be asked for directly
    (``docker.yaml mounts:``) or arrive from a resolved symlink
    (:func:`seekr_hatchery.sandbox.mount_links.expand_link_mounts`) — same hazard, so
    one check, run over the finished list rather than at each source.

    Two rules:

    - A destination inside :data:`SYSTEM_MOUNT_BLOCKLIST` is dropped. It would
      shadow the image's own binaries or a special filesystem.
    - On macOS, a source outside the paths a podman machine shares is kept but
      warned about: the mount succeeds and presents as an empty directory, with
      no error anywhere, which is otherwise near-impossible to diagnose.

    Dropping rather than failing the launch is deliberate. Most offenders come
    from a symlink the user never thought about, and killing the sandbox over a
    stray dotfile link would be hostile — but it is warned about loudly, never
    silent.
    """
    result: list[mount_types.Mount] = []
    for m in mounts:
        if not isinstance(m, mount_types.BindMount):
            result.append(m)
            continue

        dst = Path(m.dst)
        blocked = next((sp for sp in SYSTEM_MOUNT_BLOCKLIST if dst == sp or sp in dst.parents), None)
        if blocked is not None:
            ui.warn(
                f"Skipping mount of {m.src} at {m.dst}: {blocked} is provided by the container "
                f"image, and mounting over it would shadow the sandbox's own files."
            )
            continue

        if sys.platform == "darwin":
            src = m.src.expanduser()
            resolved = src.resolve()
            if not any(resolved == r or r in resolved.parents for r in _MACOS_SHARED_ROOTS):
                shared = ", ".join(str(r) for r in _MACOS_SHARED_ROOTS)
                ui.warn(
                    f"Mounting {src} at {m.dst}, but it resolves to {resolved}, outside the paths "
                    f"a podman machine shares with the host ({shared}) — it may appear empty "
                    f"inside the sandbox."
                )

        result.append(m)
    return result


def _git_worktree_mounts(
    repo: Path, name: str, branch: branches.BranchName, container_root: str
) -> list[mount_types.Mount]:
    """Return the layered Mounts for one repo + worktree pair (pre-worktree portion).

    Produces the read-only repo root + targeted read-write .git sub-mounts that
    protect the main branch while allowing the task branch's git metadata to be
    modified. The worktree directory itself and any git-pointer
    shadow file are NOT included — callers append those afterwards (so sentinel
    files can be inserted between the .git layers and the worktree mount if needed).

    This function is intentionally repo-agnostic: pass ``str(repo)`` for the
    primary repo (container paths mirror host paths) or ``/includes/<basename>``
    for an included secondary repo.
    """
    git_dir = repo / ".git"
    mounts: list[mount_types.Mount] = [
        mount_types.BindMount(src=str(repo), dst=container_root, mode="RO"),
        mount_types.BindMount(src=str(git_dir), dst=f"{container_root}/.git", mode="RW"),
        mount_types.BindMount(src=str(git_dir / "objects"), dst=f"{container_root}/.git/objects", mode="RW"),
    ]
    # mount_types.Mount the branch's namespace directory rw so git can create .lock
    # sidecars alongside the branch ref during commits.
    branch_refs = git_dir / branch.ref_dir
    if branch_refs.exists():
        mounts.append(
            mount_types.BindMount(
                src=str(branch_refs),
                dst=f"{container_root}/.git/{branch.ref_dir}",
                mode="RW",
            )
        )
    logs_dir = git_dir / "logs"
    if logs_dir.exists():
        mounts.append(mount_types.BindMount(src=str(logs_dir), dst=f"{container_root}/.git/logs", mode="RW"))
    # Only this task's worktree git metadata is writable; other worktrees' metadata
    # is protected by the ro parent mount (prevents `git worktree prune` damage).
    worktree_meta = git_dir / "worktrees" / name
    if worktree_meta.exists():
        mounts.append(
            mount_types.BindMount(src=str(worktree_meta), dst=f"{container_root}/.git/worktrees/{name}", mode="RW")
        )
    return mounts


def build_mounts(
    meta: models.SessionMeta,
    backend: agent.AgentBackend,
    session_dir: Path,
    config: sandbox_config.SandboxConfig,
    *,
    git_sentinel_files: list[tuple[Path, str]] | None = None,
    include_entries: list[includes.IncludeEntry] | None = None,
) -> list[mount_types.Mount]:
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
      LOW-MEDIUM: the task branch's refs/heads namespace is rw, allowing the
        container to create arbitrary branches within that namespace.
      MEDIUM: .git/ root rw allows modifying config/packed-refs/FETCH_HEAD
        etc. Required so git can take rebase/cherry-pick/merge locks.
      HIGH: .git/objects/ rw against the real object store — the container
        can corrupt git history. Fixing requires staging+copy-back, which
        kills real-time host visibility of commits.
    """
    # follow_symlinks is a per-mount property, declared on the trees it applies
    # to — the worktree here, and the docker.yaml mounts in
    # _construct_config_mounts. expand_link_mounts acts on it below.
    tree_links: bool = config.follow_symlinks

    mounts: list[mount_types.Mount]
    if meta.no_worktree:
        mounts = [
            mount_types.BindMount(
                src=str(meta.worktree),
                dst=str(meta.worktree_path),
                mode="RW",
                follow_links=tree_links,
            )
        ]
        mounts.extend(_default_home_mounts())
        mounts.extend(backend.construct_mounts(session_dir))
        mounts.extend(_construct_config_mounts(config))
        mounts.extend(_construct_volume_mounts(config))
        if config.clipboard_images:
            mounts.append(_clipboard_image_mount(session_dir))
    else:
        container_root = str(meta.repo_path)
        container_worktree = str(meta.worktree_path)

        mounts = _git_worktree_mounts(meta.repo_path, meta.name, meta.branch_name, container_root)

        # git writes these into .git/ root during normal commits; use per-task
        # sentinel files so .git/ root stays ro.
        for host_file, git_filename in git_sentinel_files or []:
            mounts.append(
                mount_types.BindMount(src=str(host_file), dst=f"{container_root}/.git/{git_filename}", mode="RW")
            )

        mounts.append(
            mount_types.BindMount(src=str(meta.worktree), dst=container_worktree, mode="RW", follow_links=tree_links)
        )
        mounts.extend(_default_home_mounts())
        mounts.extend(backend.construct_mounts(session_dir))
        mounts.extend(_construct_config_mounts(config))
        mounts.extend(_construct_volume_mounts(config))
        if config.clipboard_images:
            mounts.append(_clipboard_image_mount(session_dir))

    # In no-commit mode, layer two mounts: the whole store read-only (sibling
    # task records + Dockerfile/docker.yaml are readable but immutable) and the
    # agent's own task subdirectory read-write on top (more-specific path wins,
    # same trick used for .git/ above).
    #
    # The RW mount must be the task's *directory*, not the file itself: atomic
    # saves (write a tmp file, then rename() over the target — vim and many
    # editors/agents do this) need write permission on the containing
    # directory, since rename()/unlink() are directory operations in POSIX.
    # A single-file RW mount can't satisfy that even though the file's
    # content is writable. Task files live one-per-subdirectory
    # (tasks/<name>/...) precisely so this directory-level RW mount doesn't
    # expose any other task's files.
    if meta.no_commit and meta.type != "chat":
        hdir = meta.hatchery_dir
        hdir.mkdir(parents=True, exist_ok=True)
        mounts.append(mount_types.BindMount(src=str(hdir), dst=str(hdir), mode="RO"))
        task_file = meta.task_file
        if task_file is not None:
            mounts.append(mount_types.BindMount(src=str(task_file.parent), dst=str(task_file.parent), mode="RW"))

    if include_entries:
        mounts.extend(
            _include_mounts(
                include_entries,
                meta.name,
                session_dir,
                no_worktree=meta.no_worktree,
                branch=meta.include_branch_name,
            )
        )

    # Expand then validate, last and only here: every branch above and every
    # mount source (worktree, backend, docker.yaml, includes) is covered by the
    # one pair of calls. Both need the complete list — expansion to tell which
    # container paths another mount already provides, validation because a
    # dangerous mount is dangerous whichever source produced it.
    return _validate_mounts(mount_links.expand_link_mounts(mounts))


def _include_mounts(
    include_entries: list[includes.IncludeEntry],
    name: str,
    session_dir: Path,
    no_worktree: bool,
    branch: branches.BranchName,
) -> list[mount_types.Mount]:
    """Return Mounts for paths included via --include / --include-rw / --include-ro.

    Each include is mounted at its host path inside the container, same as
    the primary repo. Distinct host paths are inherently unique, so no
    basename collision logic is needed; the worktree's existing .git
    pointer already resolves correctly under host-path mirroring, so no
    pointer rewrite is needed either.

    mode="worktree": For git repos with a prefixed task worktree the
    same layered mount strategy as the primary repo is applied (root:ro,
    targeted .git sub-dirs:rw, worktree:rw).

    mode="rw" or mode="ro": Simple bind-mount with the corresponding
    access mode. No worktree is expected or created.

    In no-worktree mode all entries fall back to a simple mount using
    their access mode (worktree entries are treated as rw).
    """
    mounts: list[mount_types.Mount] = []

    for entry in include_entries:
        path = entry.path
        check_host_path_safe_for_mount(path)
        container_path = str(path)

        if entry.mode == "worktree" and not no_worktree:
            is_git = (path / ".git").exists()
            if is_git:
                worktree = path / constants.WORKTREES_SUBDIR / name
                if worktree.exists():
                    # Layered mounts: root ro + targeted .git rw (same as primary repo)
                    mounts.extend(_git_worktree_mounts(path, name, branch, container_path))
                    mounts.append(mount_types.BindMount(src=str(worktree), dst=str(worktree), mode="RW"))
                    continue
                logger.warning(
                    "include worktree not found for %s (expected %s); "
                    "falling back to plain rw mount — branch isolation unavailable.",
                    path,
                    worktree,
                )
            # Git repo without worktree, or plain dir in worktree mode → rw
            mounts.append(mount_types.BindMount(src=str(path), dst=container_path, mode="RW"))
        else:
            # reference mode (ro/rw), or no_worktree fallback
            access = entry.mode if entry.is_reference() else "rw"
            mounts.append(mount_types.BindMount(src=str(path), dst=container_path, mode=access.upper()))

    return mounts


def _default_home_mounts() -> list[mount_types.Mount]:
    """Mounts applied to every container regardless of agent: .gitconfig."""
    mounts: list[mount_types.Mount] = []
    gitconfig = Path.home() / ".gitconfig"
    if gitconfig.exists():
        mounts.append(mount_types.BindMount(src=str(gitconfig), dst=f"{agent.CONTAINER_HOME}/.gitconfig", mode="RO"))
    return mounts


def clipboard_image_dir(session_dir: Path) -> Path:
    """Per-task host directory where pasted clipboard images are saved.

    Bind-mounted at the same absolute path inside the container so a file
    written here by the host-side PTY proxy resolves identically when the
    agent reads it.
    """
    return session_dir / "clipboard"


def _clipboard_image_mount(session_dir: Path) -> mount_types.Mount:
    """Return the Mount for the per-task clipboard images directory.

    Mounts at the identical host path inside the container so the file path
    typed into the agent's stdin (a host path) resolves byte-for-byte.
    """
    d = clipboard_image_dir(session_dir)
    d.mkdir(parents=True, exist_ok=True)
    return mount_types.BindMount(src=str(d), dst=str(d), mode="RW")


def build_shell_mounts(repo: Path, config: sandbox_config.SandboxConfig) -> list[mount_types.Mount]:
    """Return the existing shell mount policy, in launch order."""
    mounts: list[mount_types.Mount] = [mount_types.BindMount(src=str(repo), dst=str(repo), mode="RW")]
    mounts.extend(_default_home_mounts())
    mounts.extend(_construct_config_mounts(config))
    mounts.extend(_construct_volume_mounts(config))
    return _validate_mounts(mount_links.expand_link_mounts(mounts))
