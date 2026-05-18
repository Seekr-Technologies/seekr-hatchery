"""Tests for the stdlib PTY pump in ``pty_proxy``.

We test ``_pump`` against ``os.pipe()`` pairs rather than a real PTY so
the tests don't depend on TTY semantics that vary across CI envs.  Two
unidirectional pipes stand in for "stdin" (user → pump) and "master_fd"
(child → pump and pump → child).  A third pair captures everything the
pump would write to stdout.
"""

import os
import threading
import time
from dataclasses import dataclass, field

import seekr_hatchery.pty_proxy as pty_proxy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeResult:
    to_agent: bytes = b""


@dataclass
class _FakeInterceptor:
    """Minimal PasteInputSink stand-in."""

    feeds: list[bytes] = field(default_factory=list)
    next_results: list[_FakeResult] = field(default_factory=list)

    def feed_stdin(self, chunk: bytes) -> _FakeResult:
        self.feeds.append(chunk)
        if self.next_results:
            return self.next_results.pop(0)
        return _FakeResult(to_agent=chunk)


def _pump_in_thread(pump_kwargs: dict) -> threading.Thread:
    t = threading.Thread(target=pty_proxy._pump, kwargs=pump_kwargs, daemon=True)
    t.start()
    return t


def _read_with_timeout(fd: int, n: int, timeout: float = 1.0) -> bytes:
    """Read up to *n* bytes from *fd*, polling until *timeout*."""
    deadline = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < deadline:
        import select as _sel

        r, _, _ = _sel.select([fd], [], [], 0.05)
        if r:
            chunk = os.read(fd, n - len(buf))
            if not chunk:
                break
            buf += chunk
            if len(buf) >= n:
                return buf
    return buf


# ---------------------------------------------------------------------------
# _pump
# ---------------------------------------------------------------------------


class TestPumpForwarding:
    def test_stdin_forwarded_to_agent_by_default(self):
        stdin_r, stdin_w = os.pipe()
        master_r, master_w = os.pipe()  # parent writes here; pump reads master_r
        stdout_r, stdout_w = os.pipe()

        # The pump treats master_fd as bidirectional, so we feed agent output
        # by writing to master_w and capture pump→agent writes by … well, we
        # can't, because _pump writes to the same fd it reads from.  Skip the
        # back-channel for this test and assert only that stdin reaches the
        # forward path by inspecting the interceptor.
        interceptor = _FakeInterceptor()
        running = [True]
        thread = _pump_in_thread(
            dict(
                stdin_fd=stdin_r,
                master_fd=master_r,
                stdout_fd=stdout_w,
                is_running=lambda: running[0],
                interceptor=interceptor,
            )
        )

        os.write(stdin_w, b"hello")
        time.sleep(0.05)
        running[0] = False
        # Trigger select to wake by closing stdin.
        os.close(stdin_w)
        thread.join(timeout=1.0)

        for fd in (stdin_r, master_r, master_w, stdout_r, stdout_w):
            try:
                os.close(fd)
            except OSError:
                pass

        assert interceptor.feeds == [b"hello"]

    def test_master_output_forwarded_to_stdout(self):
        stdin_r, stdin_w = os.pipe()
        master_r, master_w = os.pipe()
        stdout_r, stdout_w = os.pipe()

        interceptor = _FakeInterceptor()
        running = [True]
        thread = _pump_in_thread(
            dict(
                stdin_fd=stdin_r,
                master_fd=master_r,
                stdout_fd=stdout_w,
                is_running=lambda: running[0],
                interceptor=interceptor,
            )
        )

        os.write(master_w, b"output-from-agent")
        out = _read_with_timeout(stdout_r, len(b"output-from-agent"))

        running[0] = False
        os.close(stdin_w)
        thread.join(timeout=1.0)
        for fd in (stdin_r, master_r, master_w, stdout_r, stdout_w):
            try:
                os.close(fd)
            except OSError:
                pass

        assert out == b"output-from-agent"

    def test_master_eof_ends_pump(self):
        stdin_r, stdin_w = os.pipe()
        master_r, master_w = os.pipe()
        stdout_r, stdout_w = os.pipe()

        interceptor = _FakeInterceptor()
        thread = _pump_in_thread(
            dict(
                stdin_fd=stdin_r,
                master_fd=master_r,
                stdout_fd=stdout_w,
                is_running=lambda: True,
                interceptor=interceptor,
            )
        )

        # Close the writer end of the master pipe — pump should see EOF and exit.
        os.close(master_w)
        thread.join(timeout=1.0)
        assert not thread.is_alive()

        for fd in (stdin_r, stdin_w, master_r, stdout_r, stdout_w):
            try:
                os.close(fd)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# _write_all
# ---------------------------------------------------------------------------


class TestWriteAll:
    def test_writes_full_payload(self):
        r, w = os.pipe()
        pty_proxy._write_all(w, b"abcdefg")
        os.close(w)
        assert os.read(r, 32) == b"abcdefg"
        os.close(r)

    def test_drops_on_broken_pipe(self):
        r, w = os.pipe()
        os.close(r)
        # No exception: silently drops.
        pty_proxy._write_all(w, b"junk")
        os.close(w)
