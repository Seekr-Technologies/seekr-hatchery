# Task: fix-img-paste

**Status**: complete
**Branch**: hatchery/fix-img-paste
**Created**: 2026-05-14 10:38

## Objective

Currently a user cannot paste in an image to the chat. This is likely because the interactive shell is within the sandbox and the agent cli does not properly handle this. 

Implement a change in the interactive shell that allows for a user to paste in images to the agent's shell. If its not easy, provide some other way of quickly pasting in an image into the sandbox so the agent can see it

## Summary

**Root cause:** The Codex CLI REPL runs inside a Docker container over a PTY (`-it`). Clipboard data from the host does not cross the container boundary, so direct image paste is not feasible without invasive changes to the Codex binary.

**Solution:** Added `hatchery img [TASK] [--file PATH]` — a host-side command that bridges the gap using the already-shared filesystem (the worktree is mounted read-write into the container).

**How it works:**
1. User copies an image to their system clipboard
2. User runs `hatchery img` (auto-detects the task if only one is running) from a second terminal on the host
3. The command reads the PNG from clipboard and writes it to `<worktree>/paste-<timestamp>.png`
4. It prints the container-accessible path (e.g., `/repo/.hatchery/worktrees/fix-img-paste/paste-20260514-103845.png`)
5. User pastes that path into the agent REPL to reference the image

**Platform support:**
- macOS: `osascript` reads clipboard PNG data (no extra dependencies)
- Linux Wayland: `wl-paste --type image/png`
- Linux X11: `xclip -selection clipboard -t image/png -o`
- `--file PATH`: bypass clipboard entirely (useful for CI or headless environments)

**Key decisions:**
- No new Python dependencies — stdlib + subprocess only
- Auto-infer task when exactly one is in-progress; require name otherwise
- Container path computed from `repo`-relative path + `CONTAINER_REPO_ROOT` prefix, which is always `/repo`; works for both worktree tasks and `--no-worktree` chat sessions

**Files changed:**
- `src/seekr_hatchery/cli.py`: added `_read_clipboard_image()`, `_resolve_img_task()`, and `cmd_img` command (~90 lines after `cmd_shell`)

**Gotchas for future agents:**
- The osascript snippet uses `«class PNGf»` (AppleScript chevron syntax). These are literal Unicode left/right double angle quotation marks (U+00AB / U+00BB), not `<<`/`>>`. Python source stores them as `\u00ab`/`\u00bb` escape sequences to avoid encoding issues.
- The worktree is always at `<repo>/.hatchery/worktrees/<name>/` on the host and at `/repo/.hatchery/worktrees/<name>/` inside the container. The path computation uses `dest.relative_to(repo)` which handles this correctly for both task types.
