"""Repo-local hatchery configuration — <repo>/.hatchery.yaml.

Layers on top of the global ``~/.hatchery/config.yaml`` (see
:mod:`seekr_hatchery.user_config`): any field left unset here (``None``)
inherits the global value. Lives at the repo root rather than inside
``.hatchery/`` so it stays trackable even when a repo's no-commit mode
git-excludes the whole ``.hatchery/`` directory.
"""

import sys
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

import seekr_hatchery.constants as constants
import seekr_hatchery.schema_migration as schema_migration
import seekr_hatchery.ui as ui
import seekr_hatchery.user_config as user_config


class RepoConfigModel(BaseModel):
    schema_version: Literal["1"] = "1"
    auto_commit: bool | None = None


def _migrate(data: dict) -> dict:
    """Bring a raw config dict up to the current schema version in place."""
    return schema_migration.stamp_v1(data)


def load_repo_config(repo: Path) -> RepoConfigModel:
    """Read repo/.hatchery.yaml. Returns defaults (no overrides) if absent.

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


def resolve_no_commit(repo: Path, cfg: "user_config.UserConfig", commit: bool | None) -> bool:
    """Resolve the effective no_commit value: flag > repo config > global config."""
    repo_cfg = load_repo_config(repo)
    effective_auto_commit = repo_cfg.auto_commit if repo_cfg.auto_commit is not None else cfg.auto_commit
    return (not commit) if commit is not None else (not effective_auto_commit)
