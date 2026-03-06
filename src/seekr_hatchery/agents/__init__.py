"""Agent backend definitions.

Each concrete ``AgentBackend`` encodes everything hatchery needs to know about
one AI coding agent: how to invoke it (command construction), what it needs
mounted in the sandbox (home mounts, tmpfs), how to authenticate (API key
retrieval, proxy configuration, container env vars), and how to prepare
per-task state before the container starts.

Module-level singleton ``CODEX`` is the only instance callers should use.
``from_kind()`` resolves a serialised string (e.g. ``"codex"``) back to
the appropriate singleton.
"""

from .agent_backend import CONTAINER_HOME, AgentBackend
from .codex import CodexBackend

__all__ = [
    "AgentBackend",
    "CONTAINER_HOME",
    "CodexBackend",
    "CODEX",
    "from_kind",
]

# ── Module-level singletons ────────────────────────────────────────────────────

CODEX: AgentBackend = CodexBackend()

_REGISTRY: dict[str, AgentBackend] = {b.kind: b for b in [CODEX]}


def from_kind(kind: str) -> AgentBackend:
    """Return the AgentBackend for *kind*, raising ValueError for unknown values."""
    try:
        return _REGISTRY[kind.upper()]
    except KeyError:
        valid = ", ".join(_REGISTRY)
        raise ValueError(f"unknown agent {kind!r}; valid choices: {valid}") from None
