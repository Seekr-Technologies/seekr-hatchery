"""Tests for the always-on file logging and expanded proxy logging."""

import logging
import logging.handlers

import pytest

import seekr_hatchery.cli as cli
import seekr_hatchery.constants as constants

# ---------------------------------------------------------------------------
# configure_logging
# ---------------------------------------------------------------------------


class TestConfigureLogging:
    """Tests for the always-on file handler and console level behavior."""

    @pytest.fixture()
    def clean_logger(self, monkeypatch: pytest.MonkeyPatch):
        """Reset the hatchery logger to a pristine state before each test."""
        logger = logging.getLogger("seekr_hatchery")
        saved_handlers = logger.handlers[:]
        saved_level = logger.level
        logger.handlers = []
        yield logger
        logger.handlers = saved_handlers
        logger.setLevel(saved_level)

    def test_file_handler_always_created(self, clean_logger, tmp_path, monkeypatch):
        """configure_logging always adds a RotatingFileHandler, even at WARNING."""
        monkeypatch.setattr(constants, "HATCHERY_DIR", tmp_path / ".hatchery")
        cli.configure_logging("WARNING")
        logger = logging.getLogger("seekr_hatchery")
        file_handlers = [h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(file_handlers) == 1

    def test_file_handler_captures_info_at_warning_console(self, clean_logger, tmp_path, monkeypatch):
        """File handler captures INFO even when console is at WARNING."""
        monkeypatch.setattr(constants, "HATCHERY_DIR", tmp_path / ".hatchery")
        cli.configure_logging("WARNING")
        logger = logging.getLogger("seekr_hatchery")
        logger.info("test-info-message")

        log_file = tmp_path / ".hatchery" / "hatchery.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "test-info-message" in content

    def test_console_does_not_show_info_at_warning(self, clean_logger, tmp_path, monkeypatch, capsys):
        """Console handler at WARNING does not emit INFO to stderr."""
        monkeypatch.setattr(constants, "HATCHERY_DIR", tmp_path / ".hatchery")
        cli.configure_logging("WARNING")
        logger = logging.getLogger("seekr_hatchery")
        logger.info("should-not-appear-on-console")
        captured = capsys.readouterr()
        assert "should-not-appear-on-console" not in captured.err

    def test_debug_level_enables_debug_in_file(self, clean_logger, tmp_path, monkeypatch):
        """At --log-level DEBUG, the file handler captures DEBUG messages."""
        monkeypatch.setattr(constants, "HATCHERY_DIR", tmp_path / ".hatchery")
        cli.configure_logging("DEBUG")
        logger = logging.getLogger("seekr_hatchery")
        logger.debug("test-debug-message")

        log_file = tmp_path / ".hatchery" / "hatchery.log"
        content = log_file.read_text()
        assert "test-debug-message" in content

    def test_log_file_location(self, clean_logger, tmp_path, monkeypatch):
        """Log file lives at ~/.hatchery/hatchery.log."""
        monkeypatch.setattr(constants, "HATCHERY_DIR", tmp_path / ".hatchery")
        cli.configure_logging("INFO")
        logger = logging.getLogger("seekr_hatchery")
        logger.info("trigger")

        assert (tmp_path / ".hatchery" / "hatchery.log").exists()

    def test_file_handler_appends_across_calls(self, clean_logger, tmp_path, monkeypatch):
        """Successive log writes append to the same file."""
        monkeypatch.setattr(constants, "HATCHERY_DIR", tmp_path / ".hatchery")
        cli.configure_logging("INFO")
        logger = logging.getLogger("seekr_hatchery")
        logger.info("first-message")
        logger.info("second-message")

        content = (tmp_path / ".hatchery" / "hatchery.log").read_text()
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
        monkeypatch.setattr(cli, "_log_file_path", lambda: missing)
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
        monkeypatch.setattr(cli, "_log_file_path", lambda: log_file)

        runner = CliRunner()
        result = runner.invoke(cli.cli, ["logs"])
        assert result.exit_code == 0
        assert "test-message-1" in result.output


# ---------------------------------------------------------------------------
# Per-task file logging (task_log context manager)
# ---------------------------------------------------------------------------


class TestTaskLog:
    """task_log context manager routes file logs to the task's session dir."""

    @pytest.fixture()
    def clean_logger(self, monkeypatch: pytest.MonkeyPatch):
        """Reset the hatchery logger to a pristine state before each test."""
        logger = logging.getLogger("seekr_hatchery")
        saved_handlers = logger.handlers[:]
        saved_level = logger.level
        logger.handlers = []
        yield logger
        logger.handlers = saved_handlers
        logger.setLevel(saved_level)

    def test_task_log_writes_to_session_dir(self, clean_logger, tmp_path, monkeypatch):
        """During task_log, file output goes to session_dir/hatchery.log, not the global file."""
        monkeypatch.setattr(constants, "HATCHERY_DIR", tmp_path / ".hatchery")
        cli.configure_logging("INFO")
        logger = logging.getLogger("seekr_hatchery.proxy")

        session_dir = tmp_path / "session"
        with cli.task_log(session_dir):
            logger.info("task-log-message")

        task_log = session_dir / "hatchery.log"
        assert task_log.exists()
        assert "task-log-message" in task_log.read_text()

    def test_task_log_restores_global_handler(self, clean_logger, tmp_path, monkeypatch):
        """After task_log exits, the global file handler is restored."""
        monkeypatch.setattr(constants, "HATCHERY_DIR", tmp_path / ".hatchery")
        cli.configure_logging("INFO")
        logger = logging.getLogger("seekr_hatchery.proxy")

        global_handlers_before = cli._get_file_handlers()
        session_dir = tmp_path / "session"
        with cli.task_log(session_dir):
            logger.info("during-task")

        global_handlers_after = cli._get_file_handlers()
        assert global_handlers_before == global_handlers_after
        # Global handler should be functional again
        logger.info("after-task")
        global_log = tmp_path / ".hatchery" / "hatchery.log"
        assert "after-task" in global_log.read_text()

    def test_task_log_does_not_write_to_global(self, clean_logger, tmp_path, monkeypatch):
        """Logs emitted inside task_log don't appear in the global file."""
        monkeypatch.setattr(constants, "HATCHERY_DIR", tmp_path / ".hatchery")
        cli.configure_logging("INFO")
        logger = logging.getLogger("seekr_hatchery.proxy")

        session_dir = tmp_path / "session"
        with cli.task_log(session_dir):
            logger.info("should-not-be-global")

        global_log = tmp_path / ".hatchery" / "hatchery.log"
        global_content = global_log.read_text()
        assert "should-not-be-global" not in global_content
