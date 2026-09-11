"""Repo-local hatchery configuration — <repo>/.hatchery/config.yaml.

Layers on top of the global ``~/.hatchery/config.yaml`` (see
:mod:`seekr_hatchery.user_config`): any field left unset here (``None``)
inherits the global value. Lives inside ``.hatchery/`` alongside the other
hatchery artifacts — in commit mode it's tracked and shared; in no-commit
mode it's git-excluded with the rest of ``.hatchery/`` (force-add it if you
want a shared per-repo default).
"""

import sys
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

import seekr_hatchery.constants as constants
import seekr_hatchery.ui as ui
import seekr_hatchery.user_config as user_config


class RepoConfigModel(BaseModel):
    default_agent: str | None = None
    open_editor: bool | None = None
    auto_commit: bool | None = None


# The fields a repo config may override on the global config. None = inherit.
_OVERRIDE_FIELDS = tuple(RepoConfigModel.model_fields)

_TEMPLATE = Path(__file__).parent / "resources" / "repo.yaml.template"


def _migrate(data: dict) -> dict:
    """Normalise a raw config dict in place.

    Drops the legacy ``schema_version`` key (see :mod:`seekr_hatchery.user_config`);
    repo configs are unversioned.
    """
    data.pop("schema_version", None)
    return data


def validate_config_file(path: Path) -> str | None:
    """Validate a repo config file against the current schema.

    Uses ``extra = "forbid"`` so typos / unknown keys are caught here.
    Returns ``None`` on success or an error message string on failure.
    """
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        return f"Invalid YAML: {exc}"
    data = _migrate(data)

    class StrictRepoConfigModel(RepoConfigModel):
        model_config = {"extra": "forbid"}

    try:
        StrictRepoConfigModel(**data)
    except ValidationError as exc:
        return str(exc)
    return None


def create_repo_config(repo: Path) -> Path:
    """Write a starter .hatchery/config.yaml from the template, overwriting any existing one.

    The template documents the overridable fields with everything commented
    out — a repo config exists to override a specific value, not to restate the
    global defaults.
    """
    config_file = repo / constants.REPO_CONFIG
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(_TEMPLATE.read_text())
    return config_file


def load_repo_config(repo: Path) -> RepoConfigModel:
    """Read repo/.hatchery/config.yaml. Returns defaults (no overrides) if absent.

    Exits with an error message on invalid YAML/schema — a broken repo
    config is always a user mistake that must be fixed before continuing.
    """
    config_file = repo / constants.REPO_CONFIG
    if not config_file.exists():
        return RepoConfigModel()
    try:
        raw = yaml.safe_load(config_file.read_text()) or {}
        raw = _migrate(raw)
        return RepoConfigModel.model_validate(raw)
    except Exception as exc:
        ui.error(f"invalid {constants.REPO_CONFIG}: {exc}")
        sys.exit(1)


def load_effective_config(repo: Path) -> "user_config.UserConfig":
    """Global config with this repo's overrides layered on top.

    Loads ``~/.hatchery/config.yaml`` and overlays any field the repo's
    ``.hatchery/config.yaml`` sets (non-``None``), so callers can read a single
    resolved config for agent/editor/commit decisions.
    """
    cfg = user_config.UserConfig.load()
    repo_cfg = load_repo_config(repo)
    overrides = {f: v for f in _OVERRIDE_FIELDS if (v := getattr(repo_cfg, f)) is not None}
    return cfg.with_overrides(overrides) if overrides else cfg


def resolve_no_commit(cfg: "user_config.UserConfig", commit: bool | None) -> bool:
    """Resolve the effective no_commit value: flag > (repo-merged) config.

    *cfg* is expected to be the effective config from :func:`load_effective_config`.
    """
    return (not commit) if commit is not None else (not cfg.auto_commit)
