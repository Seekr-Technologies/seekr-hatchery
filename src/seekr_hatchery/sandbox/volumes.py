"""Per-task volume lifecycle for ``VolumeMount`` entries with a seed.

Backends declare per-task state via ``VolumeMount(name="…", seed=…)``.
At launch time the orchestrator here:

1. Resolves the logical ``name`` to a per-task volume name:
   ``{meta.container_name}-vol-{name}``.
2. Ensures the volume exists on the runtime (creates it if not).
3. If newly created, invokes the seed callable and streams its
   output into the volume via a short-lived helper container that
   has the volume mounted.
4. Returns the mounts list with each ``VolumeMount.name`` replaced by
   the resolved runtime name so ``mount_to_docker_args`` produces the
   right ``-v`` / ``--mount`` flag.

The helper container uses ``meta.image_name`` — the already-built
sandbox image — so there's no second image dependency. It runs as
uid 0 (root inside the user-namespace) to write into the empty
volume, then chowns to uid 1000 so the in-container ``hatchery`` user
can read what we wrote.

Lifecycle:
- Created on first launch.
- Seeded once at creation. On resume (volume already exists) the seed
  callable is NOT re-invoked — the container's accumulated writes are
  preserved.
- Removed on ``hatchery rm`` (a separate concern handled in cli.py).
"""

import io
import logging
import subprocess
import tarfile
from collections.abc import Mapping
from pathlib import Path

import seekr_hatchery.models as models
import seekr_hatchery.sandbox.mount_types as mount_types
import seekr_hatchery.sandbox.runtime as runtime
import seekr_hatchery.utils as utils

# Keep diagnostic output and logger-level selection stable across relocation.
logger = logging.getLogger("seekr_hatchery.seeded_volumes")


def task_volume_prefix(repo: Path, name: str) -> str:
    """Common runtime-name prefix of every per-task seeded volume.

    Derived from ``container_name(repo, name) + "-vol-"`` so a single
    ``volume ls`` enumeration finds them all.
    """
    # Session identity remains owned by sessions; resolve it only at call time.
    import seekr_hatchery.sessions as sessions

    return f"{sessions.container_name(repo, name)}-vol-"


def volume_name(meta: models.SessionMeta, logical_name: str) -> str:
    """Per-task runtime volume name for a backend-declared logical name."""
    return f"{meta.container_name}-vol-{logical_name}"


def cleanup_task_volumes(repo: Path, name: str) -> None:
    """Remove every per-task seeded volume for a task.

    Wired into ``cli._do_delete`` only — mark-done and chat-exit
    leave volumes alone so that ``hatchery resume <task>`` keeps the
    accumulated agent state (sessions, history, backups, projects'
    per-project flags, etc.).

    Best-effort: silently no-ops if no container runtime is available,
    if the runtime binary is missing, or if ``volume ls`` fails. Takes
    ``(repo, name)`` rather than ``models.SessionMeta`` because by the time
    this runs the meta may already be unlinked.
    """
    if runtime.PodmanRuntime.available():
        selected_runtime = runtime.PodmanRuntime()
    elif runtime.DockerRuntime.available():
        selected_runtime = runtime.DockerRuntime()
    else:
        return  # no runtime — nothing to clean up

    prefix = task_volume_prefix(repo, name)
    try:
        result = utils.run(
            [selected_runtime.binary, "volume", "ls", "-q", "--filter", f"name={prefix}"],
            check=False,
        )
    except FileNotFoundError:
        return  # runtime binary missing
    if result.returncode != 0:
        logger.debug("%s volume ls failed (rc=%d) — skipping cleanup", selected_runtime.binary, result.returncode)
        return
    # Docker treats ``--filter name=`` as a substring match; podman as a
    # regex anchored at any position. Post-filter in Python with
    # ``startswith`` so anchoring is uniform across runtimes.
    for volname in result.stdout.splitlines():
        volname = volname.strip()
        if not volname or not volname.startswith(prefix):
            continue
        try:
            utils.run([selected_runtime.binary, "volume", "rm", "--force", volname], check=False)
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            logger.warning("Failed to remove task volume %s: %s", volname, exc)


def _resolved_name(m: mount_types.VolumeMount, meta: models.SessionMeta) -> str:
    """Return the runtime volume name for *m*, applying per-task scoping if
    declared via ``task_scoped=True``."""
    if m.task_scoped:
        return volume_name(meta, m.name)
    return m.name


def _ensure_volume(runtime_binary: str, name: str) -> bool:
    """Ensure a named volume exists. Return True if we created it now,
    False if it already existed.

    ``volume inspect`` is the cheapest cross-runtime existence check —
    returns non-zero when the volume is missing.
    """
    if utils.run([runtime_binary, "volume", "inspect", name], check=False).returncode == 0:
        return False
    logger.debug("creating %s volume: %s", runtime_binary, name)
    utils.run([runtime_binary, "volume", "create", name])
    return True


def _seed_volume(
    runtime_binary: str,
    image: str,
    name: str,
    files: Mapping[str, bytes],
) -> None:
    """Stream *files* into the volume *name* via a helper container.

    Builds an uncompressed tar stream in memory, pipes it to a one-shot
    container (``run --rm -i``) that mounts the volume at ``/seed``,
    extracts the tar, then chowns to uid/gid 1000 so the in-container
    agent (running as the non-root ``hatchery`` user) can read the
    files.
    """
    buf = io.BytesIO()
    with tarfile.open(mode="w|", fileobj=buf) as tar:
        for rel_path, content in files.items():
            info = tarfile.TarInfo(name=rel_path)
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))
    subprocess.run(
        [
            runtime_binary,
            "run",
            "--rm",
            "-i",
            "--user",
            "0:0",
            "-v",
            f"{name}:/seed",
            image,
            "sh",
            "-c",
            "tar -xf - -C /seed && chown -R 1000:1000 /seed",
        ],
        input=buf.getvalue(),
        check=True,
        capture_output=True,
    )


def _seed_files_for(m: mount_types.VolumeMount, ctx: mount_types.SeedContext) -> Mapping[str, bytes]:
    """Run the seed callable and normalise its return to ``{relpath: bytes}``.

    For ``is_file=True`` the callable returns raw bytes; we wrap them
    under the basename of ``dst`` (e.g. ``app-config.json`` for
    ``dst="/home/hatchery/.app-config.json"``) so the volume contains exactly
    one file at that name — which is what the startup symlink resolves to
    The runtime creates the startup symlink for file-shaped mounts.

    For ``is_file=False`` the callable returns ``{relpath: bytes}``
    directly.
    """
    assert m.seed is not None
    payload = m.seed(ctx)
    if m.is_file:
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError(f"VolumeMount(is_file=True) seed must return bytes; got {type(payload).__name__}")
        return {Path(m.dst).name: bytes(payload)}
    if isinstance(payload, (bytes, bytearray)):
        raise TypeError("VolumeMount(is_file=False) seed must return Mapping[str, bytes]; got bytes")
    return payload


def prepare_volume_mounts(
    runtime_binary: str,
    mounts: list[mount_types.Mount],
    meta: models.SessionMeta,
    session_dir: Path,
    proxy_token: str,
    container_workdir: str,
) -> list[mount_types.Mount]:
    """Materialise every ``VolumeMount`` in *mounts*; return a new list
    with names resolved so ``mount_to_docker_args`` can serialise them.

    Bind mounts and tmpfs mounts pass through unchanged. Volume mounts
    with a seed callable get the create + seed treatment described in
    the module docstring; volume mounts without a seed (e.g. user-config
    cache volumes) are merely ensured-to-exist.
    """
    out: list[mount_types.Mount] = []
    for m in mounts:
        if isinstance(m, (mount_types.BindMount, mount_types.TmpfsMount)):
            out.append(m)
            continue
        if not isinstance(m, mount_types.VolumeMount):  # defensive
            out.append(m)
            continue

        runtime_name = _resolved_name(m, meta)
        created = _ensure_volume(runtime_binary, runtime_name)
        if created and m.seed is not None:
            ctx = mount_types.SeedContext(
                session_dir=session_dir,
                proxy_token=proxy_token,
                container_workdir=container_workdir,
            )
            try:
                files = _seed_files_for(m, ctx)
                _seed_volume(runtime_binary, meta.image_name, runtime_name, files)
            except Exception:
                # Best-effort rollback: drop the half-created volume so
                # the next launch reseeds cleanly rather than mounting
                # empty state. Covers both the seed callable raising
                # and the helper container failing.
                utils.run([runtime_binary, "volume", "rm", "--force", runtime_name], check=False)
                raise

        # Resolve name so CLI serialisation uses the runtime name.
        out.append(m.model_copy(update={"name": runtime_name}))
    return out
