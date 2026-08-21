"""Host-side ``kubectl proxy`` subprocess launcher.

``start_kubectl_proxy_proc()`` launches ``kubectl proxy --port=0
--address=127.0.0.1`` on the host, bound to loopback only, using the host's
active kubeconfig for credentials.  The port it binds to is parsed from its
startup output.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time

logger = logging.getLogger(__name__)


def start_kubectl_proxy_proc(
    context: str | None = None,
    timeout: float = 10.0,
) -> tuple[subprocess.Popen[str], int]:
    """Launch ``kubectl proxy --port=0 --address=127.0.0.1`` and return ``(proc, port)``.

    Args:
        context: Kubeconfig context to pass via ``--context``.  ``None`` uses
            the host's currently active context.
        timeout: Seconds to wait for the startup banner before giving up.

    Reads stdout until the startup banner ``"Starting to serve on 127.0.0.1:{port}"``
    is seen, then returns.  Raises :class:`RuntimeError` if kubectl is not found,
    the process exits early, or the port cannot be determined within *timeout* seconds.
    """
    if not shutil.which("kubectl"):
        raise RuntimeError("kubectl not found on PATH — install kubectl on the host to use the kubectl feature")

    cmd = ["kubectl", "proxy", "--port=0", "--address=127.0.0.1"]
    if context:
        cmd += ["--context", context]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    deadline = time.monotonic() + timeout
    port_re = re.compile(r"Starting to serve on 127\.0\.0\.1:(\d+)")

    assert proc.stdout is not None  # guaranteed by PIPE
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            # Process exited unexpectedly.
            stderr_out = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"kubectl proxy exited unexpectedly.  stderr: {stderr_out.strip()}")
        m = port_re.search(line)
        if m:
            port = int(m.group(1))
            logger.debug("kubectl proxy started on port %d", port)
            return proc, port

    proc.terminate()
    raise RuntimeError(f"kubectl proxy did not report its port within {timeout}s")


def stop_kubectl_proxy_proc(proc: subprocess.Popen[str]) -> None:
    """Terminate the kubectl proxy subprocess."""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    logger.debug("kubectl proxy stopped")
