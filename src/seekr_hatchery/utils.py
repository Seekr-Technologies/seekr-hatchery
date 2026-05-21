"""Generic utility functions with no project-domain knowledge.

A deliberate leaf module: imports only stdlib + ``ui`` (which is itself a
leaf). Anything that takes raw inputs and returns raw outputs without
knowing about sessions, docker, or git belongs here. Used by every other
module — putting these utilities anywhere else would force callers to
function-level-import to dodge a cycle.
"""

import hashlib
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import seekr_hatchery.ui as ui

logger = logging.getLogger("hatchery")


def run(
    cmd: list[str], cwd: Path | None = None, check: bool = True, sensitive: bool = False
) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing stdout/stderr.

    Logs the command at DEBUG level before running and the result (or
    ``<redacted>`` when *sensitive* is True) afterwards.  On non-zero exit with
    *check=True* the CalledProcessError is re-raised after printing a human-
    readable error to stderr.
    """
    logger.debug("run %s (cwd=%s)", cmd, cwd)
    try:
        result = subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        ui.error(f"command failed (exit {e.returncode}): {' '.join(str(a) for a in e.cmd)}")
        if e.stdout.strip():
            ui.info(f"  stdout: {e.stdout.strip()}")
        if e.stderr.strip():
            ui.info(f"  stderr: {e.stderr.strip()}")
        raise
    if sensitive:
        logger.debug("  -> rc=%d stdout=<redacted> stderr=<redacted>", result.returncode)
    else:
        logger.debug("  -> rc=%d stdout=%r stderr=%r", result.returncode, result.stdout[:200], result.stderr[:200])
    return result


def to_name(raw: str) -> str:
    """Normalise a task name to a filesystem/branch-safe slug.

    Lowercases, replaces runs of non-alphanumeric chars with ``-``, strips
    leading/trailing dashes, and truncates to 50 chars. Pure — no I/O.
    """
    s = raw.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")[:50]


def repo_id(repo: Path) -> str:
    """Stable, human-readable identifier for a repo path.

    Combines ``to_name(repo.name)`` (up to 20 chars) with an 8-char SHA-256
    suffix of the absolute path, so repos with the same basename at
    different paths get different ids.
    """
    short_hash = hashlib.sha256(str(repo).encode()).hexdigest()[:8]
    basename = to_name(repo.name)[:20]
    return f"{basename}-{short_hash}"


def unique_basename(name: str, used: set[str]) -> str:
    """Return *name* if not in *used*, else *name*-1, *name*-2, … — first unused variant.

    Does NOT mutate *used*; callers are responsible for adding the result.
    """
    if name not in used:
        return name
    i = 1
    while f"{name}-{i}" in used:
        i += 1
    return f"{name}-{i}"


def open_for_editing(path: Path) -> None:
    """Open a file for the user to edit, then wait until they are done.

    If $EDITOR is set it is launched directly and expected to block until the
    user is done (e.g. EDITOR=emacsclient, EDITOR=vim, EDITOR=nano).
    If $EDITOR is not set, fall back to the OS default opener (non-blocking)
    and prompt for Enter.
    """
    editor = os.environ.get("EDITOR")
    if editor:
        subprocess.run([editor, str(path)])
    else:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)])
        else:
            subprocess.run(["xdg-open", str(path)])
        input(f"Edit {path.name}, then press Enter to continue...")
