# Task: fix-vterm-pty

**Status**: complete
**Branch**: hatchery/fix-vterm-pty
**Created**: 2026-05-19 09:17

## Objective

The PTY proxy seems to be messing up claude in vterm. We are getting aliasing on lines.

We has this problem some time ago, but we had fixed it. I cant remember if that fix was here, or in my .dotfiles, but the pty proxy brought it back.

Can you help figure out how the pty proxy is causing issues with vterm?

## Context

`run_with_pty` (introduced in commits `68b3957` / `8d8299a` to support
host-side Ctrl-V interception for clipboard images) interposes a fresh
`pty.openpty()` pair between the user's terminal and `docker run -it ...`.
Inside emacs vterm this caused a specific symptom: switching emacs frames
away and back left the cursor rendered one row too high, and producing
enough new output would mask the drift until the next frame switch.

## Summary

### Root cause

`pty.openpty()` + `subprocess.Popen(..., start_new_session=True)` is not a
drop-in for `forkpty(3)`. Two independent gaps result, both rooted in "the
worker PTY is left at kernel defaults and the child has no controlling-TTY
relationship to it":

1. **No controlling terminal (primary cause of the symptom).**
   `start_new_session=True` only does `setsid()`. Without
   `ioctl(worker_fd, TIOCSCTTY, 0)`, the new session has no controlling
   TTY, so when the proxy's SIGWINCH handler calls `TIOCSWINSZ` on
   `master_fd` the kernel has no process group to notify — docker (and
   claude inside it) never sees the resize. Claude draws at stale
   dimensions and the cursor lands off by the row delta as soon as emacs
   renders the buffer at a different size (frame switch with different
   geometry).

2. **Worker PTY in cooked mode (secondary corruption).** A fresh
   `pty.openpty()` worker end has `OPOST` / `ONLCR` / `ECHO` / `ICANON` /
   `ICRNL` all set. The proxy raw'd the *host* terminal but left the
   worker end at defaults, so docker's output passed through cooked-mode
   processing on the way to the master: `ONLCR` injected `\r` before every
   `\n`, `ECHO` echoed master-direction writes back into the output
   stream.

### Fix

`src/seekr_hatchery/pty_proxy.py`:

- `tty.setraw(worker_fd)` immediately after `pty.openpty()`. The worker
  end is a byte transport, not a terminal — docker manages the
  container's TTY internally, so cooked-mode processing on the transport
  is pure damage.
- `start_new_session=True` → `preexec_fn=_attach_ctty`, where
  `_attach_ctty` does `os.setsid()` + `fcntl.ioctl(0, termios.TIOCSCTTY, 0)`
  in the child (post-fork, pre-exec). `stdin == worker_fd` after the
  subprocess machinery dups it, so the ioctl targets the right fd.

### Files changed

- `src/seekr_hatchery/pty_proxy.py` — the two fixes plus *why* comments
  (the gap from `forkpty(3)` isn't obvious from the surrounding code).
- `tests/test_pty_proxy.py` — `TestRunWithPty` class with a real-PTY
  integration test. Monkeypatches `sys.stdin`/`sys.stdout` to a host PTY
  worker end, captures the inner `pty.openpty()` pair, runs a Python child
  that reports its termios and waits for SIGWINCH, then asserts
  `OPOST`/`ECHO`/`ICANON` cleared *and* `TIOCSWINSZ` on the inner master
  triggers the child's SIGWINCH handler.

### Gotchas for future work

- **`tty.setraw` clears `OPOST` but not `ONLCR`.** The `ONLCR` bit stays
  set in `oflag`, but with `OPOST` off it has no effect. Tests should
  assert on `OPOST` (the master switch), or do a behavioural check that
  `\n` doesn't become `\r\n` end-to-end. Asserting `oflag & ONLCR == 0`
  will fail even on correctly raw'd PTYs.
- **`signal.signal()` can only be called from the main thread.** The
  integration test runs `run_with_pty` in a worker thread to drive it
  concurrently with reading from the host master, so it monkeypatches
  `pty_proxy.signal.signal` to a no-op. SIGWINCH propagation is exercised
  by calling `TIOCSWINSZ` directly on the inner master rather than relying
  on the proxy's handler.
- **`run_with_pty` registers an `atexit` hook to restore termios.** In
  tests this would leak across runs and persist for the lifetime of the
  pytest process; the integration test neutralises it via monkeypatch.
- **The PTY proxy exists only to satisfy clipboard-image Ctrl-V
  interception** (see `src/seekr_hatchery/clipboard_image.py`). Removing
  the proxy would require either a host-side clipboard transport into the
  container or a sidecar+FIFO+agent-side paste command — both larger
  designs. If terminal plumbing keeps biting, that's a separate task.
- **Pre-existing test crash on aarch64:** `test_kubectl_proxy.py::
  TestRBACProxyIntegration::test_rejects_missing_token` dies with
  "Illegal instruction" in this sandbox. Reproduces on pre-fix code, so
  not related to this work — likely an x86 binary somewhere in the
  kubectl proxy test path.
