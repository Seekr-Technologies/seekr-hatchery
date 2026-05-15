# Task: image-support

**Status**: complete
**Branch**: hatchery/image-support
**Created**: 2026-05-15 13:54

## Objective

Coding tools can often accept images copy/pasted directly into the terminal. Can we support that through hatchery?

## Context

Codex's TUI composer can attach images via drag-drop (which inserts a host
file path) or, for raw clipboard paste, via the terminal's bracketed-paste
mechanism. Inside hatchery's Docker sandbox both flows fail:

- Drag-drop inserts a *host* filesystem path that doesn't exist in the
  container.
- Raw clipboard image paste needs binary clipboard access, which a regular
  TTY (and Codex itself) cannot do — it requires the terminal to volunteer
  the image via a protocol like Kitty's OSC 5522.

The user picked **raw clipboard paste over OSC 5522** as the headline,
explicitly rejected drag-drop directory mounts (blast radius), and chose
the per-task host temp dir for image storage.

## Summary

### Architecture

```
user terminal      hatchery host                 docker container
  stdin ─────▶ pty_proxy._pump ─────▶ master_fd ───▶ codex
  stdout ◀───────────────────────── master_fd ◀── codex
                       │ writes (OSC 5522 query)
                       ▼
            ~/.hatchery/tasks/<repo>/<task>/clipboard/
            (bind-mounted at identical host path inside container)
```

Three layers, kept independent so Claude Code drops in cleanly later:

1. **`pty_proxy.py`** — file-descriptor plumbing. Allocates the PTY, sets
   raw mode (atexit + finally restore), forwards SIGWINCH to the slave,
   and drives a single `select` loop. Exposes `run_with_pty(cmd,
   interceptor)`. The byte pump itself is extracted as
   `_pump(stdin_fd, master_fd, stdout_fd, is_running, interceptor)` so it
   can be unit-tested with `os.pipe()` pairs without a real PTY.
2. **`clipboard_image.py`** — protocol layer. Pure byte streams in/out.
   - `Osc5522Parser` handles streaming base64 decode with BEL/ST
     terminators, mid-prefix splits, and interleaved residue.
   - `BracketedPasteSplitter` separates pass-through bytes from paste
     payloads at `\x1b[200~ ... \x1b[201~` boundaries.
   - `PasteInterceptor` orchestrates: on a bracketed paste that looks
     binary, emits an OSC 5522 query out to the terminal, swallows the
     paste, accumulates the response, saves the image, and injects the
     file path into the agent's stdin. Plain text pastes are re-wrapped
     so the agent still sees a bracketed-paste boundary.
3. **`docker.py` integration** — `_run_container` gained a
   `paste_interceptor` parameter. When provided AND `sys.stdin.isatty()`,
   the final `docker run -it` invocation routes through
   `pty_proxy.run_with_pty`; otherwise the old `subprocess.run(cmd)` path
   stands. `_make_paste_interceptor()` builds the interceptor in
   `launch_docker` / `launch_docker_no_worktree`, threading in the
   backend's `format_image_reference` hook. `launch_sandbox_shell`
   doesn't pass an interceptor — the bash shell doesn't need this.

### Key decisions

| Decision | Choice | Rationale |
|---|---|---|
| Capture protocol | Kitty OSC 5522 | Only widely-implemented binary-clipboard escape sequence; Ghostty support in progress; degrades silently on unsupported terminals. |
| Trigger | Bracketed paste boundaries | No new hotkey for users to learn; matches the existing paste muscle memory. |
| Image storage | `session_dir/clipboard/` | Per-task lifetime; cleaned by `hatchery delete`. Identical-host-path mount so the file the host writes is the same path the container reads. Avoids `tempfile.mkdtemp()` which lands under `/var/folders/...` on macOS — outside Podman Machine's default shares. |
| Default | `clipboard_images: true` | Silent no-op on terminals that ignore OSC 5522 (250 ms timeout, then a one-shot `ui.warn`). Zero-config for Kitty/Ghostty users. |
| Image-paste detection | NUL byte in payload (or empty) | Plain ASCII / UTF-8 paste content never contains NUL; binary clipboard streams typically do. Far simpler and more robust than a "non-printable ratio" heuristic. |
| Agent-format hook | `AgentBackend.format_image_reference(path) -> str` | Concrete `@staticmethod` returning `str(path)` by default. Future backends with markup syntax (e.g. Claude Code's `[Image: …]`) override it. |
| Drag-drop path rewriting | Rejected | Would require mounting `~/Desktop`, `~/Downloads`, etc. — blast radius the user vetoed. |
| Native `--no-docker` mode | Out of scope | Trivial follow-up (host path == agent path); shipped after the Docker case is proven. |

### Files

| File | Change |
|---|---|
| `src/seekr_hatchery/pty_proxy.py` | New — stdlib PTY pump + `run_with_pty`. |
| `src/seekr_hatchery/clipboard_image.py` | New — OSC 5522 parser + bracketed-paste splitter + `PasteInterceptor`. |
| `src/seekr_hatchery/docker.py` | `clipboard_images` config field, `_clipboard_image_mount`, `clipboard_image_dir`, `_make_paste_interceptor`, `_exec_agent`, wiring in `_run_container`. |
| `src/seekr_hatchery/agents/agent_backend.py` | New `format_image_reference` default (`str(path)`). |
| `src/seekr_hatchery/resources/docker.yaml.template` | Documented `clipboard_images: true`. |
| `README.md` | New "Clipboard image paste" subsection in Docker sandbox. |
| `tests/test_pty_proxy.py` | New — `_pump` exercised with `os.pipe()` pairs. |
| `tests/test_clipboard_image.py` | New — parser, splitter, image-paste heuristic, `save_image`, end-to-end interceptor flow with a fake OSC response. |
| `tests/test_docker.py` | `clipboard_images` defaults, mount inclusion gating, `_make_paste_interceptor` returns interceptor / `None`. |
| `tests/test_agent_codex.py` | Codex inherits the default `format_image_reference`. |

### Gotchas

- **`_pump` writes "to terminal" to `stdout_fd`, not `master_fd`.** The OSC
  query has to reach the user's terminal emulator, not the agent. The
  terminal's response then arrives on stdin like any keypress, so
  `PasteInterceptor.feed_stdin` handles both states (idle and capturing).
- **Two `os.pipe()` pairs are not a PTY.** The pump test code can't
  observe what gets written *to* `master_fd` from the same fd it reads
  from, so the "stdin forwarded to agent" test inspects the interceptor's
  fed-chunks list instead of the wire. A real PTY test exists implicitly
  in `run_with_pty` and is acceptance-tested manually.
- **Don't use `tempfile.mkdtemp()` for the clipboard dir.** It lands under
  `/var/folders/...` on macOS, which Podman Machine doesn't share. We
  already put it under `~/.hatchery/tasks/<repo>/<task>/clipboard/`
  precisely because that's already shared.
- **macOS pasteboard truth.** When the user does `Cmd+Shift+4` and then
  `Ctrl+V`, the clipboard genuinely holds an image. Terminals that don't
  speak OSC 5522 typically deliver an empty bracketed paste or just
  forward textual representations. The interceptor treats an empty paste
  as "probably an image" and queries — this is the right call because if
  it isn't an image, the terminal won't respond and we abort silently.
- **`ruff` orders imports inside `if TYPE_CHECKING` blocks the same as
  module top-level.** I didn't need this here, but watch for it when
  touching the imports.
- **Pre-existing test crash on aarch64.** `tests/test_kubectl_proxy.py`
  segfaults via the cryptography library on the sandbox's aarch64 Python.
  Unrelated to this change; tests pass when that file is excluded.

### Verification

- `uv run pytest tests --ignore=tests/test_kubectl_proxy.py -q` →
  646 passed, 13 skipped.
- `uv run ruff format .` and `uv run ruff check .` → clean.
- Manual smoke test on a Kitty terminal: paste a screenshot inside a Codex
  session and verify the path appears in the composer, the file lives at
  the expected per-task host path, and the same path resolves inside the
  container.
- For unsupported terminals: verify the one-time warning fires and text
  pastes still work.
