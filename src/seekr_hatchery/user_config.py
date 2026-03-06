"""User-level configuration — ~/.hatchery/config.json.

Callers interact exclusively with :class:`UserConfig`.  Construct one via
:meth:`UserConfig.load`, which reads and migrates the on-disk file.  Mutating
methods (e.g. :meth:`set_default_agent`) change in-memory state only;
call :meth:`save` explicitly to persist.

Pass an explicit *path* to :meth:`UserConfig.load` for test isolation —
tests always supply a ``tmp_path``-based path rather than relying on the
production default.
"""

import json
import logging
import shutil
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ValidationError

import seekr_hatchery.agents as agent
import seekr_hatchery.ui as ui

logger = logging.getLogger("hatchery")


# ---------------------------------------------------------------------------
# Pydantic model — validation only, no behaviour
# ---------------------------------------------------------------------------


class UserConfigModel(BaseModel):
    schema_version: Literal["1"] = "1"
    default_agent: str | None = None
    open_editor: bool = False


# ---------------------------------------------------------------------------
# Migration — pure transformation, called before model construction
# ---------------------------------------------------------------------------


def _migrate(data: dict) -> dict:
    """Bring a raw config dict up to the current schema version in place."""
    v = str(data.get("schema_version", "0"))

    # "0" → "1": initial versioned schema (just stamp the version)
    if v == "0":
        v = "1"

    data["schema_version"] = v
    return data


# ---------------------------------------------------------------------------
# Validation — standalone file-level check
# ---------------------------------------------------------------------------


def validate_config_file(path: Path) -> str | None:
    """Validate a config file against the current schema.

    Uses ``extra = "forbid"`` so that typos / unknown keys are caught here,
    while the normal ``UserConfigModel`` stays permissive for forward
    compatibility (an older hatchery reading a newer config).

    Returns ``None`` on success or an error message string on failure.
    """
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return f"Invalid JSON: {e}"
    data = _migrate(data)

    class StrictConfigModel(UserConfigModel):
        model_config = {"extra": "forbid"}

    try:
        StrictConfigModel(**data)
    except ValidationError as e:
        return str(e)
    return None


# ---------------------------------------------------------------------------
# Detection helper — stateless, module-level
# ---------------------------------------------------------------------------


def _detect_installed(backends: list[agent.AgentBackend]) -> list[agent.AgentBackend]:
    """Return backends whose binary is present on $PATH."""
    return [b for b in backends if shutil.which(b.binary)]


# ---------------------------------------------------------------------------
# UserConfig — owns state, exposes all config operations
# ---------------------------------------------------------------------------


class UserConfig:
    """Loaded user configuration with save/load, get/set, and domain methods.

    Construct via :meth:`load`; persist changes via :meth:`save`.
    Mutation methods do **not** auto-save — the caller decides when to write.
    """

    CONFIG_PATH: ClassVar[Path] = Path.home() / ".hatchery" / "config.json"

    def __init__(self, model: UserConfigModel, path: Path) -> None:
        self._model = model
        self._path = path

    # ── Construction ──────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path | None = None) -> "UserConfig":
        """Load config from *path*, applying migrations.

        Returns a default-valued instance if the file is absent or corrupt.
        Pass an explicit *path* in tests; omit it in production to use
        :attr:`CONFIG_PATH`.
        """
        if path is None:
            path = cls.CONFIG_PATH
        if not path.exists():
            return cls(UserConfigModel(), path)
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("Could not read config at %s — using defaults", path)
            return cls(UserConfigModel(), path)
        data = _migrate(data)
        return cls(UserConfigModel(**data), path)

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self) -> None:
        """Persist the current state to :attr:`_path`."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(self._model.model_dump_json(indent=2))
        logger.debug("Config saved to %s", self._path)

    # ── Properties / setters ─────────────────────────────────────────────────

    @property
    def schema_version(self) -> str:
        return self._model.schema_version

    @property
    def default_agent(self) -> str | None:
        return self._model.default_agent

    def set_default_agent(self, value: str) -> None:
        """Set the default agent in memory.  Call :meth:`save` to persist."""
        self._model = self._model.model_copy(update={"default_agent": value})

    @property
    def open_editor(self) -> bool:
        return self._model.open_editor

    def set_open_editor(self, value: bool) -> None:
        """Set the open_editor preference in memory.  Call :meth:`save` to persist."""
        self._model = self._model.model_copy(update={"open_editor": value})

    # ── Domain methods ────────────────────────────────────────────────────────

    def resolve_backend(self, agent_name: str | None) -> agent.AgentBackend:
        """Resolve which agent backend to use for a new task.

        Resolution order
        ----------------
        1. *agent_name* given (``--agent`` flag) → use it, no detection.
        2. Exactly one binary on ``$PATH`` → use it silently.
        3. Zero binaries on ``$PATH`` → fall back to :data:`agent.CODEX` silently
           (Docker-only workflow where the agent runs inside the container).
        4. Multiple binaries on ``$PATH``, saved default → use saved default.
        5. Multiple binaries on ``$PATH``, no saved default → prompt and save.
        """
        if agent_name is not None:
            return agent.from_kind(agent_name)

        all_backends: list[agent.AgentBackend] = [agent.CODEX]
        detected = _detect_installed(all_backends)

        if len(detected) == 1:
            return detected[0]

        if len(detected) == 0:
            logger.debug("No agent binary found on $PATH — defaulting to codex (Docker workflow)")
            return agent.CODEX

        # Multiple detected — check saved default first.
        if self._model.default_agent is not None:
            try:
                return agent.from_kind(self._model.default_agent)
            except ValueError:
                logger.warning(
                    "Saved default_agent %r is no longer valid — re-prompting",
                    self._model.default_agent,
                )

        return self._prompt_and_save(detected)

    def _prompt_and_save(self, detected: list[agent.AgentBackend]) -> agent.AgentBackend:
        """Interactively prompt the user to choose a default agent, then save."""
        ui.info("Multiple AI coding agents detected. Choose your default:")
        for i, b in enumerate(detected, 1):
            ui.info(f"  {i}. {b.binary}")
        while True:
            raw = input("Choice [1]: ").strip()
            if raw == "":
                chosen = detected[0]
                break
            if raw.isdigit():
                idx = int(raw) - 1
                if 0 <= idx < len(detected):
                    chosen = detected[idx]
                    break
            ui.warn(f"Please enter a number between 1 and {len(detected)}.")

        self.set_default_agent(chosen.kind)
        self.save()
        ui.success(f"Default agent set to '{chosen.binary}'.")
        ui.info("To change it, edit ~/.hatchery/config.json directly.")
        return chosen
