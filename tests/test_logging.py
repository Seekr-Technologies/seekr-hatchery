"""Tests for the always-on file logging and expanded proxy logging."""

import logging
import logging.handlers
from pathlib import Path

import pytest

import seekr_hatchery.cli as cli
import seekr_hatchery.constants as constants
import seekr_hatchery.logging_ as logging_

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def clean_logger(monkeypatch: pytest.MonkeyPatch):
    """Reset the seekr_hatchery logger to a pristine state before each test."""
    logger = logging.getLogger("seekr_hatchery")
    saved_handlers = logger.handlers[:]
    saved_level = logger.level
    logger.handlers = []
    yield logger
    logger.handlers = saved_handlers
    logger.setLevel(saved_level)


@pytest.fixture()
def hatchery_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch constants.HATCHERY_DIR to a temp dir and return it."""
    d = tmp_path / ".hatchery"
    monkeypatch.setattr(constants, "HATCHERY_DIR", d)
    return d


# ---------------------------------------------------------------------------
# configure_logging
# ---------------------------------------------------------------------------


class TestConfigureLogging:
    """Tests for the always-on file handler and console level behavior."""

    def test_file_handler_always_created(self, clean_logger, hatchery_dir):
        """configure_logging always adds a RotatingFileHandler, even at WARNING."""
        logging_.configure_logging("WARNING")
        logger = logging.getLogger("seekr_hatchery")
        file_handlers = [h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(file_handlers) == 1

    def test_file_handler_captures_info_at_warning_console(self, clean_logger, hatchery_dir):
        """File handler captures INFO even when console is at WARNING."""
        logging_.configure_logging("WARNING")
        logger = logging.getLogger("seekr_hatchery")
        logger.info("test-info-message")

        log_file = hatchery_dir / "hatchery.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "test-info-message" in content

    def test_console_does_not_show_info_at_warning(self, clean_logger, hatchery_dir, capsys):
        """Console handler at WARNING does not emit INFO to stderr."""
        logging_.configure_logging("WARNING")
        logger = logging.getLogger("seekr_hatchery")
        logger.info("should-not-appear-on-console")
        captured = capsys.readouterr()
        assert "should-not-appear-on-console" not in captured.err

    def test_debug_level_enables_debug_in_file(self, clean_logger, hatchery_dir):
        """At --log-level DEBUG, the file handler captures DEBUG messages."""
        logging_.configure_logging("DEBUG")
        logger = logging.getLogger("seekr_hatchery")
        logger.debug("test-debug-message")

        log_file = hatchery_dir / "hatchery.log"
        content = log_file.read_text()
        assert "test-debug-message" in content

    def test_log_file_location(self, clean_logger, hatchery_dir):
        """Log file lives at ~/.hatchery/hatchery.log."""
        logging_.configure_logging("INFO")
        logger = logging.getLogger("seekr_hatchery")
        logger.info("trigger")

        assert (hatchery_dir / "hatchery.log").exists()

    def test_file_handler_appends_across_calls(self, clean_logger, hatchery_dir):
        """Successive log writes append to the same file."""
        logging_.configure_logging("INFO")
        logger = logging.getLogger("seekr_hatchery")
        logger.info("first-message")
        logger.info("second-message")

        content = (hatchery_dir / "hatchery.log").read_text()
        assert "first-message" in content
        assert "second-message" in content

    def test_no_log_file_flag_removed_from_cli(self):
        """The --log-file flag should no longer exist on the cli group."""
        runner_opts = [p for p in cli.cli.params if p.name == "log_file"]
        assert runner_opts == []


# ---------------------------------------------------------------------------
# hatchery logs command (CliRunner smoke test)
# ---------------------------------------------------------------------------


class TestLogsCommand:
    """Smoke test that 'hatchery logs' actually runs without crashing."""

    def test_logs_with_no_log_file(self, monkeypatch, tmp_path):
        """hatchery logs exits cleanly when no log file exists yet.

        The cli group callback calls configure_logging which creates the
        log file, so we patch _log_file_path itself to point somewhere
        that doesn't exist.
        """
        from click.testing import CliRunner

        missing = tmp_path / "nonexistent" / "hatchery.log"
        monkeypatch.setattr(logging_, "log_file_path", lambda: missing)
        runner = CliRunner()
        result = runner.invoke(cli.cli, ["logs"])
        assert result.exit_code == 0
        assert "No log file" in result.output

    def test_logs_shows_existing_log_content(self, monkeypatch, tmp_path):
        """hatchery logs displays the contents of the log file."""
        from click.testing import CliRunner

        log_dir = tmp_path / ".hatchery"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "hatchery.log"
        log_file.write_text("2026-07-06 12:00:00  INFO     test-message-1\n")
        monkeypatch.setattr(logging_, "log_file_path", lambda: log_file)

        runner = CliRunner()
        result = runner.invoke(cli.cli, ["logs"])
        assert result.exit_code == 0
        assert "test-message-1" in result.output


# ---------------------------------------------------------------------------
# Per-task file logging (task_log context manager)
# ---------------------------------------------------------------------------


class TestTaskLog:
    """task_log context manager routes file logs to the task's session dir."""

    def test_task_log_writes_to_session_dir(self, clean_logger, hatchery_dir, tmp_path):
        """During task_log, file output goes to session_dir/hatchery.log, not the global file."""
        logging_.configure_logging("INFO")
        logger = logging.getLogger("seekr_hatchery.proxy")

        session_dir = tmp_path / "session"
        with logging_.task_log(session_dir):
            logger.info("task-log-message")

        task_log = session_dir / "hatchery.log"
        assert task_log.exists()
        assert "task-log-message" in task_log.read_text()

    def test_task_log_restores_global_handler(self, clean_logger, hatchery_dir, tmp_path):
        """After task_log exits, the task handler is removed and the global handler remains."""
        logging_.configure_logging("INFO")
        logger = logging.getLogger("seekr_hatchery.proxy")

        global_handlers_before = logging_.get_file_handlers()
        session_dir = tmp_path / "session"
        with logging_.task_log(session_dir):
            logger.info("during-task")
            task_handlers = [
                h
                for h in logging_.get_file_handlers()
                if str(getattr(h, "baseFilename", "")) == str(session_dir / "hatchery.log")
            ]
            assert len(task_handlers) == 1

        # After exit, task handler is gone, global handler still there
        global_handlers_after = logging_.get_file_handlers()
        assert global_handlers_before == global_handlers_after
        # Global handler still works
        logger.info("after-task")
        global_log = hatchery_dir / "hatchery.log"
        assert "after-task" in global_log.read_text()

    def test_task_log_writes_to_both_global_and_task(self, clean_logger, hatchery_dir, tmp_path):
        """During task_log, logs go to both the global file and the per-task file."""
        logging_.configure_logging("INFO")
        logger = logging.getLogger("seekr_hatchery.proxy")

        session_dir = tmp_path / "session"
        with logging_.task_log(session_dir):
            logger.info("dual-write-message")

        task_log_file = session_dir / "hatchery.log"
        global_log = hatchery_dir / "hatchery.log"
        assert "dual-write-message" in task_log_file.read_text()
        assert "dual-write-message" in global_log.read_text()


# ---------------------------------------------------------------------------
# detach_console_handler
# ---------------------------------------------------------------------------


class TestDetachConsoleHandler:
    """detach_console_handler removes console handlers but keeps file handlers."""

    def test_removes_console_keeps_file(self, clean_logger, hatchery_dir):
        """After detach, console is gone but file handler remains."""
        logging_.configure_logging("INFO")

        handlers = logging.getLogger("seekr_hatchery").handlers
        # StreamHandler but NOT RotatingFileHandler = console handler
        console_handlers = [
            h
            for h in handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        file_handlers = [h for h in handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(console_handlers) == 1
        assert len(file_handlers) == 1

        logging_.detach_console_handler()

        handlers = logging.getLogger("seekr_hatchery").handlers
        console_handlers = [
            h
            for h in handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        file_handlers = [h for h in handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(console_handlers) == 0
        assert len(file_handlers) == 1

    def test_file_still_works_after_detach(self, clean_logger, hatchery_dir, capsys):
        """File logging continues to work after console is detached."""
        logging_.configure_logging("INFO")
        logger = logging.getLogger("seekr_hatchery.proxy")

        logging_.detach_console_handler()
        logger.info("after-detach")

        captured = capsys.readouterr()
        assert "after-detach" not in captured.err
        assert "after-detach" in (hatchery_dir / "hatchery.log").read_text()
