"""Tests for the MCP server module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import seekr_hatchery.mcp as mcp_mod

# ---------------------------------------------------------------------------
# mcp_available()
# ---------------------------------------------------------------------------


class TestMcpAvailable:
    def test_returns_true_when_mcp_installed(self, monkeypatch):
        import types

        fake_mcp = types.ModuleType("mcp")
        monkeypatch.setitem(__import__("sys").modules, "mcp", fake_mcp)
        assert mcp_mod.mcp_available() is True

    def test_returns_false_when_mcp_not_installed(self, monkeypatch):
        monkeypatch.setitem(__import__("sys").modules, "mcp", None)
        # When sys.modules["mcp"] is None, import will raise ImportError
        assert mcp_mod.mcp_available() is False


# ---------------------------------------------------------------------------
# create_app()
# ---------------------------------------------------------------------------


class TestCreateApp:
    @patch("seekr_hatchery.mcp.process_spawn")
    def test_spawn_task_tool_success(self, mock_spawn):
        mock_spawn.return_value = True
        try:
            app = mcp_mod.create_app(Path("/repo"), "parent-task", "hatchery/parent")
        except ImportError:
            import pytest

            pytest.skip("mcp package not installed")

        # The app should have a tool registered
        assert app is not None

    @patch("seekr_hatchery.mcp.process_spawn")
    def test_spawn_task_tool_failure(self, mock_spawn):
        mock_spawn.return_value = False
        try:
            app = mcp_mod.create_app(Path("/repo"), "parent-task", "hatchery/parent")
        except ImportError:
            import pytest

            pytest.skip("mcp package not installed")

        assert app is not None


# ---------------------------------------------------------------------------
# start_mcp_http / stop_mcp_http
# ---------------------------------------------------------------------------


class TestMcpHttpLifecycle:
    @patch("seekr_hatchery.mcp._wait_for_port", return_value=True)
    @patch("seekr_hatchery.mcp.subprocess.Popen")
    @patch("seekr_hatchery.mcp._find_ephemeral_port", return_value=9876)
    def test_start_returns_proc_and_port(self, mock_port, mock_popen, mock_wait):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc

        proc, port = mcp_mod.start_mcp_http(Path("/repo"), "test-task", "hatchery/test")

        assert proc is mock_proc
        assert port == 9876
        mock_popen.assert_called_once()
        # Verify the command includes mcp-serve
        cmd = mock_popen.call_args[0][0]
        assert "mcp-serve" in cmd
        assert "--port" in cmd
        assert "9876" in cmd

    @patch("seekr_hatchery.mcp._wait_for_port", return_value=False)
    @patch("seekr_hatchery.mcp.subprocess.Popen")
    @patch("seekr_hatchery.mcp._find_ephemeral_port", return_value=9876)
    def test_start_raises_when_port_not_ready(self, mock_port, mock_popen, mock_wait):
        mock_proc = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read.return_value = b"startup error"
        mock_popen.return_value = mock_proc

        import pytest

        with pytest.raises(RuntimeError, match="failed to start"):
            mcp_mod.start_mcp_http(Path("/repo"), "test-task", "hatchery/test")

        mock_proc.kill.assert_called_once()

    def test_stop_terminates_process(self):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.wait.return_value = 0

        mcp_mod.stop_mcp_http(mock_proc)

        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called_once()

    def test_stop_kills_if_terminate_times_out(self):
        import subprocess

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="mcp", timeout=5), None]

        mcp_mod.stop_mcp_http(mock_proc)

        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# _find_ephemeral_port
# ---------------------------------------------------------------------------


class TestFindEphemeralPort:
    def test_returns_positive_int(self):
        port = mcp_mod._find_ephemeral_port()
        assert isinstance(port, int)
        assert port > 0
