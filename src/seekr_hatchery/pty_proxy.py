"""Stdlib PTY proxy that interposes a chance to inspect TTY bytes.

``docker._run_container`` wraps the interactive ``docker run -it ...``
invocation in :func:`run_with_pty` so that ``clipboard_image``'s OSC 5522
paste interceptor can see and react to bytes flowing between the user's
terminal and the agent inside the container.

The protocol layer is intentionally *not* in this module — this is the
plumbing:

  user terminal               (writes from stdout reach the terminal)
   stdin ──▶ run_with_pty ──▶ master_fd ──▶ child
   stdout ◀──────────────────── master_fd ◀── child

Anything that wants to interpose itself on the stream implements the
:class:`PasteInputSink` protocol; this module reads, dispatches, writes.

Stdlib only.  No threads — a single ``select`` loop drives both fds.
"""

import atexit
import errno
import fcntl
import logging
import os
import pty
import select
import signal
import subprocess
import sys
import termios
import time
import tty
from typing import Callable, Protocol, runtime_checkable

logger = logging.getLogger("hatchery")

# Default chunk size for os.read calls.  Big enough that OSC 5522 frames
# (≤4096 bytes pre-base64) usually arrive whole, small enough that the
# pump stays responsive on slow terminals.
_READ_CHUNK: int = 65536


# ── Protocol ──────────────────────────────────────────────────────────────────


@runtime_checkable
class PasteInputSink(Protocol):
    """The slice of ``PasteInterceptor`` that ``_pump`` cares about.

    Defined as a Protocol so tests can hand in a fake without inheriting
    from the real class.  See ``clipboard_image.PasteInterceptor``.
    """

    def feed_stdin(self, chunk: bytes) -> object:
        """Process *chunk* from the user's stdin and return a result object.

        The returned object must expose ``to_agent: bytes``,
        ``to_terminal: bytes``, and ``capture_started: bool`` attributes.
        """

    def capture_deadline_at(self) -> float | None:
        """``time.monotonic()`` deadline for an in-flight capture, or ``None``."""

    def abort_capture(self) -> None:
        """Drop an in-flight capture (e.g. on timeout)."""


# ── PTY pump (testable in isolation) ──────────────────────────────────────────


def _write_all(fd: int, data: bytes) -> None:
    """``os.write`` with partial-write retry. Silently drops on EPIPE."""
    while data:
        try:
            n = os.write(fd, data)
        except BlockingIOError:
            time.sleep(0.001)
            continue
        except OSError as exc:
            if exc.errno in (errno.EPIPE, errno.EBADF):
                return
            raise
        data = data[n:]


def _pump(
    stdin_fd: int,
    master_fd: int,
    stdout_fd: int,
    is_running: Callable[[], bool],
    interceptor: PasteInputSink,
) -> None:
    """Run the byte pump until *is_running()* returns False or master closes.

    Reads from ``stdin_fd`` and ``master_fd``; writes user-facing output
    to ``stdout_fd``, the OSC query side-channel to ``stdout_fd`` as
    well, and agent-bound bytes to ``master_fd``.

    *interceptor* sees every stdin chunk and decides what gets forwarded.
    The pump owns timing: each iteration computes the select timeout
    from ``interceptor.capture_deadline_at()`` so an idle capture
    eventually fires :meth:`abort_capture`.
    """
    master_open = True
    while is_running() and master_open:
        deadline = interceptor.capture_deadline_at()
        if deadline is None:
            timeout: float | None = None
        else:
            timeout = max(0.0, deadline - time.monotonic())

        try:
            rlist, _, _ = select.select([stdin_fd, master_fd], [], [], timeout)
        except InterruptedError:
            continue
        except OSError as exc:
            if exc.errno == errno.EBADF:
                return
            raise

        # Timeout fired with nothing to read → capture deadline reached.
        if not rlist and deadline is not None and time.monotonic() >= deadline:
            interceptor.abort_capture()
            continue

        if stdin_fd in rlist:
            try:
                chunk = os.read(stdin_fd, _READ_CHUNK)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    chunk = b""
                else:
                    raise
            if not chunk:
                # User's stdin closed (e.g. EOF from a piped session).
                return
            result = interceptor.feed_stdin(chunk)
            to_terminal: bytes = getattr(result, "to_terminal", b"")
            to_agent: bytes = getattr(result, "to_agent", b"")
            if to_terminal:
                _write_all(stdout_fd, to_terminal)
            if to_agent:
                _write_all(master_fd, to_agent)

        if master_fd in rlist:
            try:
                chunk = os.read(master_fd, _READ_CHUNK)
            except OSError as exc:
                # EIO on master_fd means the child closed its slave.
                if exc.errno == errno.EIO:
                    chunk = b""
                else:
                    raise
            if not chunk:
                master_open = False
                continue
            _write_all(stdout_fd, chunk)


# ── Top-level entrypoint ──────────────────────────────────────────────────────


def _initial_winsize(tty_fd: int) -> bytes | None:
    """Best-effort read of the user's TTY size for the new PTY."""
    try:
        return fcntl.ioctl(tty_fd, termios.TIOCGWINSZ, b"\x00" * 8)
    except OSError:
        return None


def _set_winsize(master_fd: int, size: bytes | None) -> None:
    if size is None:
        return
    try:
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def run_with_pty(cmd: list[str], interceptor: PasteInputSink) -> int:
    """Run *cmd* under a fresh PTY with stdin/stdout interposed.

    Returns the child's exit code.  Caller must already have confirmed
    that ``sys.stdin`` is a TTY — running this when it isn't would put
    the terminal into raw mode pointlessly.
    """
    # 1. Allocate PTY pair.
    master_fd, slave_fd = pty.openpty()
    initial_size = _initial_winsize(sys.stdin.fileno())
    _set_winsize(master_fd, initial_size)

    # 2. Save termios state and switch to raw mode.  The atexit hook is a
    #    last-ditch defence; the explicit finally below covers the common
    #    case.  Either restore is idempotent.
    old_attrs = termios.tcgetattr(sys.stdin.fileno())
    restored = [False]

    def _restore_termios() -> None:
        if restored[0]:
            return
        restored[0] = True
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_attrs)
        except (OSError, termios.error):
            pass

    atexit.register(_restore_termios)
    tty.setraw(sys.stdin.fileno())

    # 3. Forward window-size changes.
    def _on_winch(_signum: int, _frame: object) -> None:
        size = _initial_winsize(sys.stdin.fileno())
        _set_winsize(master_fd, size)

    prev_winch = signal.signal(signal.SIGWINCH, _on_winch)

    try:
        child = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        os.close(master_fd)
        os.close(slave_fd)
        signal.signal(signal.SIGWINCH, prev_winch)
        _restore_termios()
        raise

    # Drop the slave end in the parent — the child owns it now.
    os.close(slave_fd)

    try:
        _pump(
            sys.stdin.fileno(),
            master_fd,
            sys.stdout.fileno(),
            is_running=lambda: child.poll() is None,
            interceptor=interceptor,
        )
        return child.wait()
    except KeyboardInterrupt:
        # In raw mode Ctrl+C arrives as 0x03 on stdin and is forwarded to
        # the child via master_fd, not as a SIGINT to us.  If we *do* see
        # one (e.g. a kill -INT from elsewhere), pass it along.
        try:
            child.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass
        return child.wait()
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        signal.signal(signal.SIGWINCH, prev_winch)
        _restore_termios()
