"""MCP (Model Context Protocol) server for the spawn_task tool.

The server runs on the **host** (not inside the container) so it can call
``git.create_worktree()`` and ``tasks.save_task()`` against the real repo.

In Docker mode the server listens on an ephemeral HTTP port; the container
connects via ``host.docker.internal``.  In native mode agents connect via
stdio (the agent launches the server as a subprocess).

Each hatchery task starts its own MCP server so there is zero cross-talk
between concurrent tasks.
"""

import logging
import socket
import subprocess
import sys
import time
from pathlib import Path

import seekr_hatchery.tasks as tasks
from seekr_hatchery.spawn import SpawnRequest, process_spawn

logger = logging.getLogger("hatchery")


def mcp_available() -> bool:
    """Return True if the ``mcp`` package is installed."""
    try:
        import mcp  # noqa: F401

        return True
    except ImportError:
        return False


def create_app(repo: Path, parent_name: str, parent_branch: str):
    """Build a FastMCP app with the ``spawn_task`` tool registered.

    Returns a ``FastMCP`` instance.  Raises ``ImportError`` if the ``mcp``
    package is not installed.
    """
    from mcp.server.fastmcp import FastMCP

    mcp_app = FastMCP("hatchery")

    @mcp_app.tool()
    def spawn_task(name: str, objective: str) -> str:
        """Spawn a new hatchery task.

        Creates a git worktree, branch, task file, and metadata entry for the
        new task.  The user can then attach with ``hatchery resume <name>``.

        Args:
            name: Short task name (will be slugified).
            objective: Plain-text description of what the new task should accomplish.
        """
        req = SpawnRequest(
            name=tasks.to_name(name),
            objective=objective,
            base=parent_branch,
        )
        ok = process_spawn(req, repo, parent_name)
        if ok:
            return f"Spawned task '{req.name}'. The user can attach with: hatchery resume {req.name}"
        return f"Failed to spawn task '{req.name}'. Check the hatchery logs for details."

    return mcp_app


def _find_ephemeral_port() -> int:
    """Bind to port 0, learn the assigned port, close the socket."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 5.0) -> bool:
    """Poll until *port* is accepting connections (or *timeout* expires)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def start_mcp_http(
    repo: Path,
    parent_name: str,
    parent_branch: str,
) -> tuple[subprocess.Popen, int]:
    """Start an MCP HTTP server as a subprocess on an ephemeral port.

    Returns ``(process, port)``.  The caller **must** call
    :func:`stop_mcp_http` in a ``finally`` block.
    """
    port = _find_ephemeral_port()
    cmd = [
        sys.executable,
        "-m",
        "seekr_hatchery.cli",
        "mcp-serve",
        "--repo",
        str(repo),
        "--name",
        parent_name,
        "--branch",
        parent_branch,
        "--port",
        str(port),
    ]
    logger.debug("Starting MCP server: %s", cmd)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if not _wait_for_port(port):
        proc.kill()
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        raise RuntimeError(f"MCP server failed to start on port {port}: {stderr}")

    logger.info("MCP server started on port %d (pid=%d)", port, proc.pid)
    return proc, port


def stop_mcp_http(proc: subprocess.Popen) -> None:
    """Stop a running MCP HTTP server process."""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    logger.debug("MCP server stopped (pid=%d)", proc.pid)
