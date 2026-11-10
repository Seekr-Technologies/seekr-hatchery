"""Sandbox config."""

import os
import sys
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator
from pydantic import ValidationError as _PydanticValidationError

import seekr_hatchery.constants as constants
import seekr_hatchery.includes as includes
import seekr_hatchery.sidecars as sidecars
import seekr_hatchery.ui as ui

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


class SandboxConfig(sidecars.SidecarConfig):
    """Schema for .hatchery/docker.yaml."""

    # Keep validation error headings stable despite the Python API rename.
    model_config = ConfigDict(extra="forbid", title="DockerConfig")
    schema_version: Literal["1"] = "1"
    mounts: list[str] = []
    volumes: list[CacheVolume] = []
    include: list[str | includes.IncludeItem] = []
    dind: bool = False
    follow_symlinks: bool = False
    clipboard_images: bool = True
    cap_add: list[str] = []
    environment: list[str] = []

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

    @field_validator("environment", mode="before")
    @classmethod
    def validate_environment(cls, v: list | None) -> list[str]:
        if v is None:
            return []
        for i, entry in enumerate(v):
            if not isinstance(entry, str):
                raise ValueError(f"environment[{i}]: expected a string, got {type(entry).__name__!r}")
            name = entry.split("=", 1)[0]
            if not name:
                raise ValueError(f'environment[{i}]: invalid entry {entry!r} — expected "VAR=value" or "VAR"')
        return v

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
                    result.append(includes.IncludeItem.model_validate(entry))
                except _PydanticValidationError as exc:
                    raise ValueError(f"include[{i}]: {exc}") from exc
            else:
                raise ValueError(f"include[{i}]: expected a string or dict, got {type(entry).__name__!r}")
        return result


def parse_include_entry(entry: str | includes.IncludeItem) -> tuple[str, str]:
    """Parse a single docker.yaml include entry into (path_str, mode).

    Accepts the legacy string form or a validated includes.IncludeItem::

        "../other-repo"                          → ("../other-repo", "worktree")
        includes.IncludeItem(path="../ref", mode="ro")    → ("../ref", "ro")
    """
    if isinstance(entry, str):
        return entry, "worktree"
    return entry.path, entry.mode


def _migrate_config(data: dict) -> dict:
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


def features(config: SandboxConfig) -> list[str]:
    """Return the list of enabled optional features from a loaded SandboxConfig."""
    features = []
    if config.dind:
        features.append("DinD")
    if config.kubernetes is not None:
        contexts = config.kubernetes.resolved_contexts()
        if len(contexts) > 1:
            features.append(f"kubectl ({', '.join(context.display_name for context in contexts)})")
        else:
            features.append("kubectl")
    if config.s3 is not None:
        features.append("S3 proxy")
    if config.oci is not None:
        features.append("OCI proxy")
    return features


def _read_config(path: Path) -> dict:
    """Parse one Docker config file and return its migrated mapping."""
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError("expected a YAML mapping at the document root")
    return _migrate_config(raw)


def _merge_config(base: object, overlay: object) -> object:
    """Recursively layer raw Docker config values.

    Mappings merge, lists append, and all other overlay values replace their
    base value. A null overlay on a list is treated like an empty list because
    commented-out YAML sections parse as null.
    """
    if isinstance(base, dict) and isinstance(overlay, dict):
        result = dict(base)
        for key, value in overlay.items():
            result[key] = _merge_config(result[key], value) if key in result else value
        return result
    if isinstance(base, list) and overlay is None:
        return list(base)
    if isinstance(base, list) and isinstance(overlay, list):
        return [*base, *overlay]
    return overlay


def validate_config_file(path: Path, *, base_path: Path | None = None) -> str | None:
    """Validate a Docker config, optionally as an overlay on *base_path*.

    Returns ``None`` on success or an error message string on failure — the
    string form (rather than a hard exit) lets the interactive edit loop
    re-prompt the user.
    """
    try:
        raw = _read_config(path)
        if base_path is not None and base_path.exists():
            base = _read_config(base_path)
            SandboxConfig.model_validate(base)
            raw = _merge_config(base, raw)
        SandboxConfig.model_validate(raw)
    except yaml.YAMLError as exc:
        return f"Invalid YAML: {exc}"
    except Exception as exc:
        return str(exc)
    return None


def load_config(hatchery_dir: Path, *, local_hatchery_dir: Path | None = None) -> SandboxConfig:
    """Load ``docker.yaml`` with an optional ``docker.local.yaml`` overlay.

    The local file defaults to *hatchery_dir*. Task worktrees pass the main
    checkout's Hatchery directory explicitly so one user-local file applies to
    every task. Missing files produce an empty config; either invalid file is a
    hard user error naming the file that must be fixed.
    """
    config_file = hatchery_dir / constants.DOCKER_CONFIG
    local_file = (local_hatchery_dir or hatchery_dir) / constants.LOCAL_DOCKER_CONFIG
    raw: dict = {}
    if config_file.exists():
        try:
            raw = _read_config(config_file)
            SandboxConfig.model_validate(raw)
        except Exception as exc:
            ui.error(f"invalid docker config {config_file}: {exc}")
            sys.exit(1)
    if local_file.exists():
        try:
            overlay = _read_config(local_file)
            raw = _merge_config(raw, overlay)
        except Exception as exc:
            ui.error(f"invalid docker config {local_file}: {exc}")
            sys.exit(1)
    try:
        return SandboxConfig.model_validate(raw)
    except Exception as exc:
        source = local_file if local_file.exists() else config_file
        ui.error(f"invalid docker config {source}: {exc}")
        sys.exit(1)


def resolve_environment(config: SandboxConfig) -> dict[str, str]:
    """Resolve declared ``environment`` entries into a name→value dict.

    ``VAR=value`` yields a literal; bare ``VAR`` pulls the host's current
    ``os.environ["VAR"]`` and is skipped when the host has no such variable.
    """
    resolved: dict[str, str] = {}
    for entry in config.environment:
        name, sep, value = entry.partition("=")
        if sep:
            resolved[name] = value
        elif name in os.environ:
            resolved[name] = os.environ[name]
    return resolved
