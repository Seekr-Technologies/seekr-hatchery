# Task: image-support

**Status**: in-progress
**Branch**: hatchery/image-support
**Created**: 2026-05-15 13:54

## Objective

Coding tools can often accept images copy/pasted directly into the terminal. Can we support that through hatchery?

## Agreed Plan

Support **raw clipboard image paste** (e.g. `Cmd+Shift+4` → `Ctrl+V`) by
intercepting the agent's TTY with a stdlib-only PTY proxy that issues an
OSC 5522 query to the user's terminal on bracketed paste, decodes the
binary clipboard data, saves it to a per-task `clipboard/` directory, and
injects the file path into the agent's stdin. Falls back silently on
terminals that don't speak OSC 5522 (e.g. xterm, gnome-terminal). Codex
sees an ordinary file path; future agents can override formatting.

Locked decisions:

| Decision | Choice |
|---|---|
| Mechanism | OSC 5522 escape-sequence interception (Kitty/Ghostty) |
| Image storage | Per-task `clipboard/` under `session_dir`, bind-mounted at identical host path |
| Trigger | Bracketed paste boundaries (`\e[200~ … \e[201~`) — no new hotkey |
| Default | **On** — silent no-op on unsupported terminals (250 ms timeout, cached per-session) |
| Cleanup | Per-task; removed by `hatchery delete <task>` |
| Format hook | `AgentBackend.format_image_reference(path)` (Codex default: raw path) |
| Drag-drop path rewriting | Out of scope (blast radius rejected by user) |
| Native (`--no-docker`) | Out of scope for v1 |
| Sandbox shell | Skip PTY wrap |

## Progress Log

- [x] 1. Add `AgentBackend.format_image_reference` default + Codex inherits
- [x] 2. Add `DockerConfig.clipboard_images` + mount helper; wire into `docker_mounts*`
- [x] 3. Implement `clipboard_image.py` — OSC 5522 parser + `PasteInterceptor`
- [ ] 4. Implement `pty_proxy.py` — stdlib PTY pump with input/output hooks
- [ ] 5. Wire `pty_proxy` into `_run_container` (gated on TTY + config)
- [ ] 6. Tests for all of the above
- [ ] 7. Docs: `docker.yaml.template` + README "Docker sandbox" section

## Summary

*(Fill in on completion — then remove Agreed Plan and Progress Log above.
Cover: key decisions made, patterns established, files changed, gotchas,
and anything a future agent working in this repo should know.)*
