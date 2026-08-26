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
import seekr_hatchery.ui as ui


class RepoConfigModel(BaseModel):
    schema_version: Literal["1"] = "1"
    auto_commit: bool | None = None


def _migrate(data: dict) -> dict:
    """Bring a raw config dict up to the current schema version in place."""
    v = str(data.get("schema_version", "0"))

    # "0" → "1": initial versioned schema (just stamp the version)
    if v == "0":
        v = "1"

    data["schema_version"] = v
    return data


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
