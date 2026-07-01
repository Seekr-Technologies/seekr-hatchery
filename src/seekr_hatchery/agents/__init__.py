"""Agent backend definitions.

Each concrete ``AgentBackend`` encodes everything hatchery needs to know about
one AI coding agent: how to invoke it (command construction), what it needs
mounted in the sandbox (via ``construct_mounts``), how to authenticate (API key
retrieval, proxy configuration, container env vars), and how to prepare
per-task state before the container starts.

Module-level singleton ``CODEX`` is the only instance callers should use.
``from_kind()`` resolves a serialised string (e.g. ``"codex"``) back to
the appropriate singleton.
"""

from seekr_hatchery.agents.agent_backend import CONTAINER_HOME, AgentBackend
from seekr_hatchery.agents.codex import CodexBackend
from seekr_hatchery.agents.opencode import OpenCodeBackend

__all__ = [
    "AgentBackend",
    "CONTAINER_HOME",
    "CodexBackend",
    "OpenCodeBackend",
    "CODEX",
    "OPENCODE",
    "from_kind",
]

# ── Module-level singletons ────────────────────────────────────────────────────

CODEX: AgentBackend = CodexBackend()
OPENCODE: AgentBackend = OpenCodeBackend()

ALL_BACKENDS = [
    CODEX,
    OPENCODE,
]

_REGISTRY: dict[str, AgentBackend] = {b.kind: b for b in ALL_BACKENDS}


def from_kind(kind: str) -> AgentBackend:
    """Return the AgentBackend for *kind*, raising ValueError for unknown values."""
    try:
        return _REGISTRY[kind.upper()]
    except KeyError:
        valid = ", ".join(_REGISTRY)
        raise ValueError(f"unknown agent {kind!r}; valid choices: {valid}") from None
