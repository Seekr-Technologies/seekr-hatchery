"""Pi container storage upgrades (not task/config schema migrations).

Pathway: missing marker = V1 -> V2 (CURRENT).
V1: all agent state, including sessions/, in the task's pi-dir volume.
V2: shared host agent directory plus task-local pi-sessions and pi-bin volumes.

State lives at <task-dir>/pi-container-state/<runtime-and-task-id>.json.
A persisted IN_PROGRESS transition belongs to its source version until committed.
Fresh tasks follow the same V1->V2 step, without a copy when no legacy volume exists.
Unmarked destination collisions are errors, not evidence of a completed upgrade.

To add V2->V3: document the layouts, implement a named upgrade with recovery,
append its sequential dispatch below, advance CURRENT_VERSION, and test the full
pathway and interruption boundaries. Keep versions independent of package releases.
V1->V2 was introduced by expand-pi-mounts. Retire it only when support for V1 tasks
is explicitly dropped; initialize fresh tasks at the supported version then.
"""

import hashlib
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

import seekr_hatchery.seeded_volumes as volumes
import seekr_hatchery.utils as utils
from seekr_hatchery.locks import hatchery_lock

if TYPE_CHECKING:
    from seekr_hatchery.docker import ContainerRuntime
    from seekr_hatchery.models import SessionMeta

logger = logging.getLogger(__name__)
CURRENT_VERSION = 2
MINIMUM_SUPPORTED_VERSION = 1


class _Transition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    from_version: int = Field(ge=1)
    to_version: int = Field(ge=1)
    status: Literal["IN_PROGRESS"]


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    layout_version: int = Field(ge=1)
    migration: _Transition | None


def upgrade_container_state(meta: "SessionMeta", *, runtime: "ContainerRuntime") -> None:
    """Upgrade Pi storage under a runtime/immutable-task-scoped lock."""
    identity = hashlib.sha256(f"{runtime.binary}:{meta.container_name}".encode()).hexdigest()
    path = meta.session_dir / "pi-container-state" / f"{identity}.json"
    with hatchery_lock(f"pi-container-state.{identity}"):
        state = _load_state(path)
        if state.layout_version == 1:
            _upgrade_v1_to_v2(meta, runtime, path, recovering=state.migration is not None)
            state = _State(layout_version=2, migration=None)
            _save_state(path, state)
        # Append subsequent N->N+1 upgrades here, updating and saving state
        # after each successful step. Do not skip intermediate versions.
        if state.layout_version != CURRENT_VERSION:
            raise RuntimeError(f"No Pi upgrade pathway from V{state.layout_version} to V{CURRENT_VERSION}.")


def _load_state(path: Path) -> _State:
    if not path.exists():
        return _State(layout_version=1, migration=None)
    try:
        state = _State.model_validate_json(path.read_text())
    except (OSError, ValidationError) as exc:
        raise RuntimeError(f"Cannot read Pi container state at {path}: {exc}") from exc
    if not MINIMUM_SUPPORTED_VERSION <= state.layout_version <= CURRENT_VERSION:
        raise RuntimeError(
            f"Unsupported Pi container state V{state.layout_version}; "
            f"supported V{MINIMUM_SUPPORTED_VERSION}..V{CURRENT_VERSION}."
        )
    transition = state.migration
    if transition is not None and (
        transition.from_version != state.layout_version
        or transition.to_version != state.layout_version + 1
        or transition.to_version > CURRENT_VERSION
    ):
        raise RuntimeError(f"Invalid Pi upgrade transition at {path}.")
    return state


def _save_state(path: Path, state: _State) -> None:
    """Atomically persist progress before side effects, or completion before use."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w") as stream:
            stream.write(state.model_dump_json(indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _upgrade_v1_to_v2(
    meta: "SessionMeta",
    runtime: "ContainerRuntime",
    path: Path,
    *,
    recovering: bool,
) -> None:
    """Import pi-dir/sessions/ once; never mutate the legacy source."""
    source = volumes.volume_name(meta, "pi-dir")
    destination = volumes.volume_name(meta, "pi-sessions")
    helper = f"hatchery-pi-v1-v2-{path.stem[:32]}"
    existing = utils.run([runtime.binary, "volume", "ls", "-q"]).stdout.splitlines()
    if recovering:
        if source not in existing:
            raise RuntimeError("Cannot recover Pi V1->V2: legacy history volume is missing.")
        _discard_v1_to_v2_import(runtime, helper, destination)
    elif destination in existing:
        raise RuntimeError("Unversioned Pi V1 state already has pi-sessions; refusing to overwrite it.")
    elif source not in existing:
        return  # Fresh task: no legacy state to copy; caller commits V2.

    _save_state(
        path, _State(layout_version=1, migration=_Transition(from_version=1, to_version=2, status="IN_PROGRESS"))
    )
    logger.info("Upgrading Pi storage V1->V2: %s -> %s", source, destination)
    try:
        utils.run([runtime.binary, "volume", "create", destination])
        utils.run(
            [
                runtime.binary,
                "run",
                "--rm",
                "--name",
                helper,
                "--user",
                "0:0",
                "-v",
                f"{source}:/legacy:ro",
                "-v",
                f"{destination}:/sessions",
                meta.image_name,
                "sh",
                "-ec",
                "if [ -d /legacy/sessions ]; then cp -a /legacy/sessions/. /sessions/; fi; "
                "chown -R 1000:1000 /sessions",
            ]
        )
    except BaseException:
        # Progress remains IN_PROGRESS even if cleanup succeeds. Process death
        # bypasses this block, so the next locked upgrade performs the same recovery.
        try:
            _discard_v1_to_v2_import(runtime, helper, destination)
        except Exception as exc:
            logger.warning("Pi V1->V2 cleanup deferred until next launch: %s", exc)
        raise


def _discard_v1_to_v2_import(runtime: "ContainerRuntime", helper: str, destination: str) -> None:
    """Stop an orphan writer before deleting an uncommitted V2 destination."""
    containers = utils.run([runtime.binary, "ps", "-a", "--format", "{{.Names}}"]).stdout.splitlines()
    if helper in containers:
        utils.run([runtime.binary, "rm", "--force", helper])
    existing = utils.run([runtime.binary, "volume", "ls", "-q"]).stdout.splitlines()
    if destination in existing:
        # No forced volume removal: unexpectedly in-use history blocks recovery.
        utils.run([runtime.binary, "volume", "rm", destination])
