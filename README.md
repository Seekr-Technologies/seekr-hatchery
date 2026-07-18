# seekr-hatchery

[![PyPI](https://img.shields.io/pypi/v/seekr-hatchery)](https://pypi.org/project/seekr-hatchery/)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://pypi.org/project/seekr-hatchery/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/Seekr-Technologies/seekr-hatchery/badge)](https://securityscorecards.dev/viewer/?uri=github.com/Seekr-Technologies/seekr-hatchery)
<!-- TODO: switch to dynamic badges once PyPI classifiers propagate and GitHub license detection stabilises:
[![Python](https://img.shields.io/pypi/pyversions/seekr-hatchery)](https://pypi.org/project/seekr-hatchery/)
[![License](https://img.shields.io/github/license/Seekr-Technologies/seekr-hatchery)](LICENSE)
-->

Task orchestration CLI for AI coding agents. Each task gets an isolated git worktree and its own agent session, sandboxed by default inside Docker.

**Sandboxing** *(on by default)* — each task runs in full isolation:
- 🐳 **Docker sandbox**: the agent runs inside a container with carefully scoped filesystem access — read-only repo, write-access only to its own worktree
- 🌿 **Isolated worktree**: each task gets its own `hatchery/<name>` git branch and worktree, so parallel work never conflicts

**Task management** — structured workflow with persistent records:
- 📋 **Plan-first workflow**: plan → approval → implement → commit; enforced by task files the agent must follow
- 🔄 **Resumable sessions**: interrupted sessions pick up exactly where they left off via preserved session state
- 📄 **Task files as records**: each task file becomes a permanent ADR in the repo after completion

<!-- Regenerate SVGs: ./docs/resources/render-svg.sh -->
<p align="center">
<img src="docs/resources/demo-codex.svg" alt="Codex demo" width="680">
</p>

## Installation

```bash
uv tool install seekr-hatchery
```

To upgrade to the latest release:

```bash
uv tool upgrade seekr-hatchery
```

> **Note:** Do not pin a version on install (e.g. `==0.3.0`) — uv stores the constraint and will refuse to upgrade past it.

Requires Python 3.12+ and at least one agent:
- **OpenAI Codex**: `npm install -g @openai/codex` — `codex` on `$PATH`, `OPENAI_API_KEY`

## Quick start

```bash
# Start a new task
hatchery new add-auth

# Start a new task using OpenAI Codex
hatchery new add-auth --agent codex

# Resume an interrupted session
hatchery resume add-auth

# Mark complete and remove the worktree
hatchery done add-auth

# See all tasks for this repo
hatchery list
```

## How it works

`hatchery new <name>` creates a git worktree on a `hatchery/<name>` branch, drops a task file there for you to fill in, commits it, then launches an agent session pointed at that worktree. The agent runs inside a Docker sandbox by default — a starter Dockerfile is created automatically on first use. The agent plans, implements, commits, and marks the task complete — all inside the isolated branch. When you're satisfied, `hatchery done <name>` cleans up the worktree and leaves the branch ready to merge.

## Task workflow

When the agent starts a new task it is given a task file at `.hatchery/tasks/YYYY-MM-DD-<name>.md`. The expected workflow:

1. **Plan first** — read the task file, ask clarifying questions, propose a numbered implementation plan. No code until the plan is approved.
2. **On approval** — update the "Agreed Plan" section, then implement step by step.
3. **While executing** — tick checkboxes in the Progress Log after each step, make a descriptive git commit.
4. **If blocked** — stop and discuss before proceeding.
5. **On completion** — mark Status as "complete", add a `## Summary` section. The task file is merged into main as the permanent record.

## Commands

| Command | Description |
|---|---|
| `new <name>` | Create worktree + branch, open task file, launch agent |
| `resume <name>` | Reattach to the existing session exactly where it left off |
| `done <name>` | Remove worktree, retain branch, mark task complete |
| `abort <name>` | Remove worktree without marking complete (branch kept) |
| `delete <name>` | Remove worktree, delete branch, erase all metadata |
| `list` | List all tasks for the current repo |
| `status <name>` | Show task metadata and the full task file |
| `self update` | Upgrade hatchery to the latest release |
| `config edit` | Open `~/.hatchery/config.json` in `$EDITOR` with validation |
| `logs` | View or follow the hatchery log file (`~/.hatchery/hatchery.log`) |

All `new` / `resume` commands accept:
- `--no-docker` — skip the container even if a Dockerfile is present
- `--no-worktree` — reuse the current directory instead of creating a new worktree

`new` also accepts:
- `--from <ref>` — fork from a specific branch or commit (default: `HEAD`)
- `--editor / --no-editor` — force editor or prompt mode for the task objective. By default, hatchery prompts in the terminal; set `"open_editor": true` in `~/.hatchery/config.json` to default to `$EDITOR`. If the editor is opened and the file is unchanged on close, the task is cancelled.
- `--commit / --no-commit` — control whether hatchery auto-commits its scaffolding (task file, Docker configuration). Default: from config (`auto_commit: true`). Use `--no-commit` to keep all hatchery files out of the tracked repo — task records and Docker files are stored under `~/.hatchery/repos/<repo-id>/` instead. Set `"auto_commit": false` in `~/.hatchery/config.json` to make no-commit the default for every repo.
- `--agent [codex]` — choose the AI agent (auto-detected from installed agents)

The chosen agent is stored in task metadata and re-used automatically on `resume`.

## Docker sandbox

By default, `new` and `resume` build a Docker image from `.hatchery/Dockerfile` and run the agent inside it. On first `new`, if no Dockerfile exists, a starter is created for the selected agent and opened for editing.

The container receives:

- Full repo mounted read-only (for context)
- `.git/objects` and `.git/logs` read-write (so commits work)
- `.git/refs/heads/hatchery/` read-write (own branch ref)
- The task worktree read-write (the only place edits land)
- `~/.codex` and a per-task auth config — Codex only
- `~/.gitconfig` read-only (commit identity)

A `.hatchery/docker.yaml` config file is also created alongside the Dockerfile.

### Custom mounts (`docker.yaml`)

`.hatchery/docker.yaml` controls extra host→container bind-mounts injected on every launch. The file is pre-populated with commented examples — uncomment what you need:

```yaml
schema_version: 1
mounts:
  # - "~/.kube:/home/hatchery/.kube:ro"
  # - "~/.aws:/home/hatchery/.aws:ro"
  # - "~/.config/gcloud:/home/hatchery/.config/gcloud:ro"
  # - "~/.oci:/home/hatchery/.oci:ro"
```

Mount format: `"host_path:container_path[:mode]"` — identical to Docker's own `-v` syntax.

- `~` is expanded to your home directory.
- `mode` defaults to `ro` (read-only) if omitted.
- Invalid entries are a hard error. Paths that do not exist on the host are silently skipped.

The file is tracked in git so every developer on the project gets the same mount configuration. Changes take effect on the next `new` or `resume`.

### Persistent cache volumes (`docker.yaml`)

For package-manager caches (uv, pip, npm, …) a host bind-mount routes every cache read/write through virtiofs on macOS, which is slow for many-small-files patterns. Use a named docker/podman volume instead — it lives inside the container engine's storage, persists across `--rm` containers, and is shared by every sandbox that mounts it:

```yaml
volumes:
  - name: uv-cache
    path: /home/hatchery/.cache/uv
```

The volume is auto-created on first launch as `hatchery-<name>` and re-used afterwards. A bare name like `uv-cache` is shared across tasks and repos; suffix it (e.g. `uv-cache-myrepo`) to scope a cache to one repo. To free disk space later: `docker volume rm hatchery-uv-cache` (or `podman volume rm`).

### Clipboard image paste

Press **Ctrl-V** in the agent's TUI to attach an image from your host clipboard to the next prompt. Works on macOS, and on Linux with `wl-paste` or `xclip` installed — terminal-agnostic. Enabled by default; set `clipboard_images: false` in `.hatchery/docker.yaml` to disable.

### API key security

The real API key never enters the container. Hatchery starts a lightweight **host-side HTTP reverse proxy** on an ephemeral port immediately before launching the container.

**Codex (OpenAI):**
- `OPENAI_API_KEY` — a random per-task proxy token
- `OPENAI_BASE_URL` — pointing to the host proxy (`http://host.docker.internal:<port>`)

The SDK inside the container uses these transparently. The proxy validates the inbound token, strips whatever credentials the container sends, injects the real API key in the correct format (`Authorization: Bearer` for OpenAI), and forwards the request over HTTPS. The real key never leaves the host process.

This means a jailbroken or adversarially-prompted agent that reads its API key env var or attempts to exfiltrate it gets only the proxy token — which is worthless outside the session.

The proxy token is stable per-task (persisted across container restarts) so cached credentials stay valid on subsequent `resume` launches.

### Custom Codex providers

If `~/.codex/config.toml` configures a custom provider via
`experimental_bearer_token` (any non-OpenAI provider with a static
bearer), hatchery routes the host-side proxy at that provider instead of
OpenAI. Detection is automatic — there is no flag to set. The bearer
token stays on the host: the container only sees a per-task proxy token,
and a sanitized `config.toml` is mounted RO at `~/.codex/config.toml`
inside the sandbox (the host file is **not** bind-mounted in this mode).

TLS verification uses the OS native trust store via
[`truststore`](https://truststore.readthedocs.io/) — macOS Keychain,
Linux `/etc/ssl/certs`, Windows cert store. Any CA already installed
system-wide (public or corporate) is trusted automatically. If the
upstream presents a certificate signed by a private CA that's not yet
in your OS trust store, install it there (the same way you'd install
it for `curl`, your browser, or any other tool) — no hatchery-specific
config required.

There is no automatic token refresh — when the host bearer rotates,
update `config.toml` on the host through whatever workflow your setup
uses.

### Container runtime auto-detection

Hatchery prefers **Podman** as the sandbox runtime when it is installed, falling back to Docker otherwise. Podman is rootless-native: UID 0 inside the sandbox maps to the calling user on the host — not real root. No daemon required. If you have both installed, `podman info` is checked first.

### Podman-in-Podman (DinD)

DinD enables the agent to run a nested container engine inside its Docker sandbox — useful when your tasks involve building container images, running integration tests with Docker Compose, or any workflow that itself needs a container runtime.

**To enable:**

1. Uncomment the `── Podman-in-Podman (DinD)` block in `.hatchery/Dockerfile`. This installs `podman`, `fuse-overlayfs`, and `uidmap`, and wires up a passwordless `sudo` wrapper so the `hatchery` user can invoke Podman.

2. Set `dind: true` in `.hatchery/docker.yaml`:

   ```yaml
   schema_version: 1
   dind: true
   mounts: []
   ```

3. Run `hatchery new <name>` or `resume <name>` — the image rebuild is only slow the first time after the Dockerfile change; subsequent runs hit the layer cache.

**What you can do inside the container:**

```bash
# Run as the `hatchery` user inside the sandbox
podman run --rm hello-world
podman build -t my-image .
podman compose up
```

hatchery automatically provisions `.hatchery/seccomp.json` the first time DinD is enabled. This seccomp profile allows the extra syscalls required by Podman's user-namespace networking stack.

## Session environment

Every agent session launched by `new` or `resume` receives two environment variables:

| Variable | Value |
|---|---|
| `HATCHERY_TASK` | The task name (e.g. `add-auth`) |
| `HATCHERY_REPO` | Absolute path to the repo root |

### Statusline integration

You can show the active task and its branch in your terminal statusline. Example script:

```bash
hatchery_line=""
if [ -n "$HATCHERY_TASK" ]; then
    hatchery_branch=$(git -C "$HATCHERY_REPO" --no-optional-locks \
        rev-parse --abbrev-ref HEAD 2>/dev/null)
    cyan=$(printf '\033[0;36m'); yellow=$(printf '\033[0;33m'); reset=$(printf '\033[0m')
    hatchery_line="├ ${cyan}⬡ ${HATCHERY_TASK}${reset}  ${yellow}${hatchery_branch}${reset}"
fi
```

Then output `"$top\n$hatchery_line\n$bottom"` when `$hatchery_line` is non-empty, otherwise `"$top\n$bottom"`. This renders as:

```
┌ user@host  ~/path/to/repo  (hatchery/add-auth ●)  Sonnet
├ ⬡ add-auth  hatchery/add-auth
└ [14:32:01]  [████████░░░░░░░░░░░░] 38%
```

## Storage layout

```
<repo>/
  .hatchery/               # in commit mode: Dockerfile, docker.yaml, tasks/ are committed
    Dockerfile             # optional sandbox definition
    docker.yaml            # optional Docker config (custom mounts, etc.)
    tasks/                 # permanent task records (tracked in git)
    worktrees/             # active worktrees (gitignored via .gitignore or .git/info/exclude)
                           # in no-commit mode: only worktrees/ exists; everything else
                           # is stored under ~/.hatchery/repos/<repo-id>/

~/.hatchery/
  config.json               # user config (default_agent, open_editor, auto_commit)
  meta.json                # DB schema version
  hatchery.log             # always-on rotating log file (5 MB × 3 backups)
  tasks/                   # all per-task state, namespaced by repository
    <repo-id>/             # stable hash of the repo path
      <task-name>/         # one directory per task
        hatchery.log        # per-task log file (during runs)
        meta.json          # task metadata
        codex_auth.json    # Docker session: proxy-token auth config
        proxy_token        # Docker session: stable API proxy UUID
        COMMIT_EDITMSG     # Docker session: git sentinel file
        ORIG_HEAD          # Docker session: git sentinel file
        git_ptr            # Docker session: container-path .git pointer
  repos/                   # out-of-tree store (no-commit mode only)
    <repo-id>/
      repo.json            # { "path": "...", "name": "..." }
      docker/              # Dockerfile.<agent> + docker.yaml (when no-commit)
      records/             # task-file ADR store (when no-commit)
        YYYY-MM-DD-<name>.md
```

## Logging

Hatchery always writes logs to disk — no flags needed. The file handler captures
**INFO** level by default, so proxy requests, RBAC decisions, and session lifecycle
events are on disk even when the console is quiet.

Console output (stderr) is shown during startup (Docker build, volume creation,
proxy start) and automatically detached before the agent sandbox launches so it
doesn't corrupt the agent's TUI.

**Two-tier file logging:**

- **Global** — `~/.hatchery/hatchery.log` (rotating, 5 MB × 3 backups).
  Accumulates everything across all commands and tasks.
- **Per-task** — when a task launches, a per-task handler is added alongside
  the global one at `~/.hatchery/tasks/<repo-id>/<name>/hatchery.log`. Both files
  receive all messages during the run. The per-task file is a clean, complete
  record for that task alone — no cross-task interleaving even if two hatchery
  spawns run concurrently.

Use `--log-level DEBUG` to see verbose output on the console (pre-launch) **and**
capture DEBUG in the log file:

```
hatchery --log-level DEBUG new my-task
```

Available levels: `DEBUG`, `INFO` (default), `WARNING`, `ERROR`.

### Viewing logs

```
hatchery logs              # global log (last 50 lines)
hatchery logs my-task      # per-task log
hatchery logs -n 100       # last 100 lines
hatchery logs my-task -f   # follow a task's log (tail -f)
```

## Development

```bash
uv sync          # install deps and editable package
uv run hatchery --help

uv run ruff format .
uv run ruff check --fix .
uv run pytest tests
```

Version is derived from git tags via `uv-dynamic-versioning`. Without a matching `v*.*.*` tag it resolves to `0.0.0.dev0`.

## Contributing

### PR title format

The PR title must follow [Conventional Commits](https://www.conventionalcommits.org/). The CI validates this on every PR.

```
<type>(<optional scope>)<!>: <description>
```

Allowed types: `feat`, `fix`, `docs`, `chore`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `revert`, `no-bump`

Individual commits on your branch are **not** validated — use whatever messages work for you while developing.

### Merging

All PRs are merged via **squash commit**. The squash commit message is set to the PR title, which is the only commit that lands on `main`.

### Version bumps

On every push to `main` the CI computes the next version from the squash commit message and creates an annotated git tag. `uv-dynamic-versioning` derives the package version from that tag.

| PR title prefix | Version bump |
|---|---|
| `no-bump:` | none — skips tag and release entirely |
| `feat!:` / any type with `!` | major — `x.0.0` |
| `feat:` | minor — `0.x.0` |
| `fix:`, `perf:` | patch — `0.0.x` |
| everything else | patch — `0.0.x` |

Most merges produce a release. Types like `chore`, `docs`, `refactor` etc. result in a patch bump. Use `no-bump:` to land a commit on `main` without cutting a release (e.g. for CI tweaks or documentation-only changes that do not warrant a version increment).

### Examples

```
feat(cli): add --dry-run flag
fix: handle missing config file gracefully
chore: update ruff to 0.16
refactor(worktree): extract branch-name validation
feat!: rename `new` command to `start`
no-bump: update CI workflow variables
```

### GitHub repository settings (for maintainers)

- **Settings > General > Pull Requests**: enable "Allow squash merging", set default commit message to "Pull request title"
