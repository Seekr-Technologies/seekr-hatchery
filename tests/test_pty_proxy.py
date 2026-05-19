"""Tests for the stdlib PTY pump in ``pty_proxy``.

We test ``_pump`` against ``os.pipe()`` pairs rather than a real PTY so
the tests don't depend on TTY semantics that vary across CI envs.  Two
unidirectional pipes stand in for "stdin" (user → pump) and "master_fd"
(child → pump and pump → child).  A third pair captures everything the
pump would write to stdout.

``TestRunWithPty`` exercises ``run_with_pty`` end-to-end against a real
PTY because the bugs it guards against — missing controlling-TTY setup
and cooked-mode worker PTY — are invisible to the pump-only tests.
"""

import os
import pty
import sys
import termios
import textwrap
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


# ---------------------------------------------------------------------------
# run_with_pty — real-PTY integration tests
# ---------------------------------------------------------------------------


def _read_until(fd: int, needle: bytes, timeout: float = 5.0) -> bytes:
    """Read from *fd* until *needle* appears or *timeout* elapses."""
    import select as _sel

    deadline = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < deadline and needle not in buf:
        r, _, _ = _sel.select([fd], [], [], 0.05)
        if not r:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    return buf


class _FakeStdio:
    """Minimal sys.stdin/sys.stdout stand-in — only fileno() is used."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd


class TestRunWithPty:
    """End-to-end tests against a real PTY.

    These cover two distinct gaps between ``pty.openpty() + Popen`` and
    ``forkpty(3)`` — both reproduce real bugs that bit users in emacs vterm:

    1. The child must have the worker end as its controlling TTY so that
       ``TIOCSWINSZ`` on the master propagates SIGWINCH to the child.
       Without it, the agent inside the container never learns the window
       resized and keeps drawing at its initial dimensions.
    2. The worker PTY must be in raw mode — it's a byte transport, not a
       terminal, and cooked-mode processing (``ONLCR``, ``ECHO``, …)
       corrupts the byte stream in both directions.
    """

    def test_ctty_propagates_sigwinch_and_worker_is_raw(self, monkeypatch):
        # Host-side PTY so run_with_pty's sys.stdin / sys.stdout look like a
        # real terminal to it without touching pytest's own stdio.
        host_master, host_worker = pty.openpty()

        # Capture the inner PTY pair that run_with_pty allocates so we can
        # drive TIOCSWINSZ on the master directly.
        real_openpty = pty_proxy.pty.openpty
        captured: dict[str, int] = {}

        def fake_openpty():
            m, w = real_openpty()
            captured.setdefault("master", m)
            captured.setdefault("worker", w)
            return m, w

        monkeypatch.setattr(pty_proxy.pty, "openpty", fake_openpty)
        monkeypatch.setattr(sys, "stdin", _FakeStdio(host_worker))
        monkeypatch.setattr(sys, "stdout", _FakeStdio(host_worker))
        # run_with_pty registers an atexit hook to restore termios; a test
        # invocation would leak that hook forever, so neutralise it.
        monkeypatch.setattr(pty_proxy.atexit, "register", lambda _f: None)
        # We drive TIOCSWINSZ directly on the inner master in this test,
        # so we don't need the proxy's SIGWINCH handler — and we can't
        # install it anyway because signal.signal() rejects calls from
        # non-main threads, and run_with_pty runs in a thread here.
        monkeypatch.setattr(pty_proxy.signal, "signal", lambda _sig, _h: None)

        child_src = textwrap.dedent(
            """
            import signal, sys, time, termios
            a = termios.tcgetattr(0)
            sys.stdout.write("TERMIOS=%d,%d,%d,%d\\n" % (a[0], a[1], a[2], a[3]))
            sys.stdout.flush()
            got = []
            signal.signal(signal.SIGWINCH, lambda s, f: got.append(1))
            sys.stdout.write("ready\\n")
            sys.stdout.flush()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not got:
                time.sleep(0.02)
            if got:
                sys.stdout.write("winch\\n")
                sys.stdout.flush()
            sys.exit(0 if got else 2)
            """
        )

        interceptor = _FakeInterceptor()
        result: dict[str, object] = {}

        def run():
            try:
                result["rc"] = pty_proxy.run_with_pty([sys.executable, "-c", child_src], interceptor)
            except BaseException as exc:  # noqa: BLE001
                result["exc"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            # 1. Confirm the worker PTY is in raw mode by inspecting the
            #    termios the child sees on its stdin.
            out = _read_until(host_master, b"ready\n", timeout=5.0)
            assert b"ready" in out, f"child never printed ready: {out!r}"

            termios_line = next(line for line in out.split(b"\n") if line.startswith(b"TERMIOS="))
            iflag, oflag, cflag, lflag = (int(x) for x in termios_line[len(b"TERMIOS=") :].split(b","))
            # ``OPOST`` is the top-level switch for output processing — with
            # it off, ``ONLCR`` has no effect even if its individual bit
            # remains set (``tty.setraw`` clears OPOST but not ONLCR
            # specifically).
            assert oflag & termios.OPOST == 0, f"OPOST set on worker: {oct(oflag)}"
            assert lflag & termios.ECHO == 0, f"ECHO set on worker: {oct(lflag)}"
            assert lflag & termios.ICANON == 0, f"ICANON set on worker: {oct(lflag)}"

            # Behavioural sanity check: a bare LF from the child should not
            # have a phantom CR injected on the way to the master.  Without
            # ``tty.setraw(worker_fd)`` this would arrive as ``b"ready\r\n"``.
            assert b"ready\r\n" not in out, f"ONLCR still active: {out!r}"

            # 2. Resize the inner master and confirm the child gets SIGWINCH.
            #    This only happens if the worker end is the child's
            #    controlling TTY — exactly the relationship the preexec_fn
            #    establishes.
            import fcntl
            import struct

            fcntl.ioctl(
                captured["master"],
                termios.TIOCSWINSZ,
                struct.pack("HHHH", 30, 100, 0, 0),
            )

            more = _read_until(host_master, b"winch\n", timeout=5.0)
            assert b"winch" in more, f"child never got SIGWINCH: {more!r}"
        finally:
            # Make sure the run_with_pty thread exits even on assertion
            # failure.
            try:
                os.close(host_master)
            except OSError:
                pass
            thread.join(timeout=5.0)
            try:
                os.close(host_worker)
            except OSError:
                pass

        assert result.get("rc") == 0, result
