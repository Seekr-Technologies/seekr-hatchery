# Task: feat-spawn-task

**Status**: complete
**Branch**: hatchery/feat-spawn-task
**Created**: 2026-03-12 14:50

## Objective

Allow agents to split off sub-tasks from within a running session.

Phase 1 implemented a filesystem-based spawn protocol (agent writes
`.hatchery/spawn/<name>.md`, host polls with a watcher thread). During review,
we concluded that an MCP (Model Context Protocol) server is strictly better:
agents already support MCP natively, so the spawn tool appears alongside their
standard toolbox with no custom skill/file conventions needed.

## Context

The MCP server runs on the **host** (not inside the container) so it can call
`git.create_worktree()` and `tasks.save_task()` against the real repo. In
Docker mode the container connects via `host.docker.internal`; in native mode
agents connect via stdio.

Each hatchery task starts its own MCP server on its own ephemeral port, mirroring
the existing per-task API proxy pattern.

## Summary

### What changed

Replaced the file-watcher spawn protocol with an MCP server that exposes
`spawn_task` as a standard tool.

### Architecture

1. **`spawn.py`** — stripped to a thin library with `SpawnRequest` dataclass
   and `process_spawn()` function (the core provisioning logic). All watcher
   code removed.

2. **`mcp.py`** (new) — MCP server module:
   - `mcp_available()` — checks if `mcp` package is installed
   - `create_app()` — builds FastMCP app with `spawn_task` tool
   - `start_mcp_http()` / `stop_mcp_http()` — subprocess lifecycle for Docker

3. **`cli.py`**:
   - Hidden `mcp-serve` command (internal entry point for subprocess/stdio)
   - `_resolve_mcp()` helper checks config + package availability
   - All three launch functions (`_launch_new`, `_launch_resume`,
     `_launch_finalize`) accept `enable_mcp` and wire up Docker/native config
   - Spawn watcher imports and calls removed entirely

4. **`docker.py`**:
   - `launch_docker()` and `launch_docker_no_worktree()` start/stop MCP HTTP
     server around container execution (in `finally` block)
   - `.hatchery/spawn/` mount removed from `docker_mounts()`

5. **`agents/agent_backend.py`** — new non-abstract method:
   - `write_mcp_config()` — writes agent-specific MCP config, returns extra
     mount strings (Docker) or empty list (native)

6. **`agents/codex.py`** — overrides `write_mcp_config()`:
   - Writes TOML `mcp.json` to session dir with URL-based config
   - In Docker mode, the returned mount string shadow-mounts it into the container

7. **`user_config.py`** — `enable_mcp: bool = True` setting

8. **`tasks.py`** — `SPAWN_SUBDIR` removed; gitignore simplified; system prompt
   hint updated to mention MCP tool

9. **`pyproject.toml`** — `mcp = ["mcp>=1.0"]` optional dependency

### Files changed

| File | Action |
|------|--------|
| `src/seekr_hatchery/spawn.py` | Refactored — watcher removed, `process_spawn()` public API |
| `src/seekr_hatchery/mcp.py` | Created — MCP server module |
| `src/seekr_hatchery/cli.py` | Modified — `mcp-serve` command, MCP wiring, spawn watcher removed |
| `src/seekr_hatchery/docker.py` | Modified — MCP lifecycle, spawn mount removed |
| `src/seekr_hatchery/agents/agent_backend.py` | Modified — MCP config methods |
| `src/seekr_hatchery/agents/codex.py` | Modified — MCP config overrides |
| `src/seekr_hatchery/user_config.py` | Modified — `enable_mcp` setting |
| `src/seekr_hatchery/tasks.py` | Modified — spawn remnants removed, MCP hint |
| `pyproject.toml` | Modified — `mcp` optional dependency |
| `tests/test_spawn.py` | Refactored — watcher tests removed |
| `tests/test_mcp.py` | Created — MCP module tests |
| `tests/test_cli.py` | Modified — spawn watcher mocks removed |
| `tests/test_user_config.py` | Modified — enable_mcp in defaults |
| `.agents/skills/hatchery-spawn/SKILL.md` | Deleted — replaced by MCP tool |

### Key decisions

- **MCP over file-watcher**: Agents support MCP natively; no custom skill file
  or filesystem convention needed. The tool shows up in the agent's toolbox
  automatically.
- **HTTP everywhere**: Both Docker and native modes use the same HTTP transport.
  Hatchery always owns the MCP server lifecycle (start before agent, stop in
  `finally`). Docker URL is `host.docker.internal:{port}`, native is
  `127.0.0.1:{port}`. This avoids clobbering the user's global agent config.
- **Subprocess for HTTP mode**: `start_mcp_http()` launches `hatchery mcp-serve`
  via `subprocess.Popen`. Cleaner isolation than threading + asyncio.
- **Ephemeral port binding**: `_find_ephemeral_port()` binds port 0 then closes;
  `_wait_for_port()` polls until the subprocess is listening.
- **Per-task MCP server**: Mirrors the proxy pattern — each task gets its own
  server on its own port. Zero cross-talk between concurrent tasks.
- **Backend-owned config**: `write_mcp_config()` lives on `AgentBackend` so
  each backend fully owns its config format.
- **Optional dependency**: `mcp` package is optional; `_resolve_mcp()` checks
  availability and prints a one-time install hint if missing.
- **Graceful degradation**: If MCP is disabled or unavailable, everything works
  exactly as before — agents just don't see the `spawn_task` tool.

### Gotchas

- The `mcp` package must be installed separately (`pip install seekr-hatchery[mcp]`).
  The `mcp_available()` check prevents import errors.
- Codex uses TOML format for `mcp.json` despite the `.json` extension — this is
  the Codex CLI's convention.
- The `mcp-serve` CLI command is hidden (`hidden=True`) — it's internal
  infrastructure, not user-facing.
- `spawn.py` still has `source_file` on `SpawnRequest` for potential future use;
  `process_spawn()` guards the `.unlink()` call.
- In native mode, the MCP config is written to the session dir (not the user's
  global config). The Codex backend's `write_mcp_config()` returns mount strings
  for Docker but in native mode the caller just needs the config file written.
  Currently native mode writes to session_dir which Codex won't read unless
  mounted — this needs the agent to be pointed at it (future work for native
  Codex support).
