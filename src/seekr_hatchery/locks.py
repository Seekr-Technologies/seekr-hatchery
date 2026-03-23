"""Process-level advisory locks for hatchery.

All lock files live under ``~/.hatchery/locks/`` so multiple concurrent
hatchery processes (one per session) coordinate through the filesystem.

``fcntl.flock`` is used exclusively:
- Blocking: callers wait until the lock is available.
- Death-safe: the kernel releases the lock automatically if the process dies.

Usage::

    from seekr_hatchery.locks import hatchery_lock

    with hatchery_lock("refresh.claude"):
        ...
"""

import fcntl
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def hatchery_lock(name: str):
    """Acquire an exclusive advisory lock at ``~/.hatchery/locks/<name>``.

    Blocks until the lock is available.  Released automatically on context
    exit or process death (kernel guarantee via ``fcntl.flock``).

    Args:
        name: Lock file basename, e.g. ``"refresh.claude"`` or ``"refresh.codex"``.
    """
    lock_dir = Path.home() / ".hatchery" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / name
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
