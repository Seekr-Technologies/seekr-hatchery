"""Logging infrastructure for hatchery — formatters, handlers, and configuration.

All modules use ``logging.getLogger(__name__)`` which produces hierarchical child
loggers (e.g. ``seekr_hatchery.proxy``) that propagate to the ``seekr_hatchery``
parent logger.  :func:`configure_logging` attaches handlers and sets the level on
the parent so every child is covered.

Two-tier file logging:
  - Global fallback: ``~/.hatchery/hatchery.log`` (always on)
  - Per-task: ``session_dir / "hatchery.log"`` (swapped in by :func:`task_log`)
"""

import logging
import logging.handlers
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import seekr_hatchery.constants as constants
import seekr_hatchery.ui as ui

# Parent logger for the whole package.
_pkg_logger = logging.getLogger("seekr_hatchery")

# Global log file: always-on rotating file handler at ~/.hatchery/hatchery.log.
_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_LOG_BACKUP_COUNT = 3

_LOG_FMT = "%(asctime)s  %(levelname)-8s  %(name)-20s  %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def log_file_path() -> Path:
    """Resolve the global log file path at call time from constants.HATCHERY_DIR.

    Resolved at call time (not import time) so a patched HATCHERY_DIR
    is honored — important for tests.
    """
    return constants.HATCHERY_DIR / "hatchery.log"


class _HatcheryFormatter(logging.Formatter):
    """File handler formatter: strips the ``seekr_hatchery.`` prefix from logger names.

    Uses millisecond-precision timestamps via :func:`ui._format_time`.
    """

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return ui._format_time(record, datefmt)

    def format(self, record: logging.LogRecord) -> str:
        if record.name.startswith("seekr_hatchery."):
            record.name = record.name[len("seekr_hatchery.") :]
        return super().format(record)


def configure_logging(level: str) -> None:
    """Configure the package logger with a console handler and an always-on file handler.

    Console (stderr) handler respects *level* (default WARNING).  The file handler
    always captures at least INFO, so debuggable signal is on disk even when the
    console is quiet.  When ``level="DEBUG"``, both handlers get DEBUG.

    The file is ``~/.hatchery/hatchery.log`` (rotating, 5 MB × 3 backups).
    """
    console_level = getattr(logging, level.upper(), logging.WARNING)

    # File handler always captures at least INFO — this is the whole point.
    file_level = min(logging.INFO, console_level)

    # Console handler (stderr) — colored when TTY.
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(ui.ColorFormatter(_LOG_FMT, datefmt=_LOG_DATEFMT))
    _pkg_logger.addHandler(console)

    # File handler — always on, rotating.
    try:
        constants.HATCHERY_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file_path(), maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUP_COUNT
        )
        file_handler.setLevel(file_level)
        file_handler.setFormatter(_HatcheryFormatter(_LOG_FMT, datefmt=_LOG_DATEFMT))
        _pkg_logger.addHandler(file_handler)
    except OSError:
        # Non-fatal: console logging still works.
        pass

    # Logger level must be the min so both handlers get messages.
    _pkg_logger.setLevel(min(console_level, file_level))


def get_file_handlers() -> list[logging.handlers.RotatingFileHandler]:
    """Return the RotatingFileHandler(s) currently on the package logger."""
    return [h for h in _pkg_logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]


@contextmanager
def task_log(session_dir: Path, level: int = logging.INFO) -> Generator[None, None, None]:
    """Route hatchery file logs to the task's own log for the duration of a run.

    Swaps the global file handler (~/.hatchery/hatchery.log) for a per-task
    handler at ``session_dir / "hatchery.log"``.  Because a hatchery process
    is single-task, the per-task file is clean and complete for that task —
    no cross-task interleaving even if two hatchery spawns run concurrently.

    The global handler is restored on exit so non-task commands continue
    logging to the global file.
    """
    session_dir.mkdir(parents=True, exist_ok=True)
    task_fh = logging.handlers.RotatingFileHandler(
        session_dir / "hatchery.log",
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
    )
    task_fh.setLevel(level)
    task_fh.setFormatter(_HatcheryFormatter(_LOG_FMT, datefmt=_LOG_DATEFMT))

    global_handlers = get_file_handlers()
    for gh in global_handlers:
        _pkg_logger.removeHandler(gh)

    _pkg_logger.addHandler(task_fh)
    try:
        yield
    finally:
        _pkg_logger.removeHandler(task_fh)
        task_fh.close()
        for gh in global_handlers:
            _pkg_logger.addHandler(gh)
