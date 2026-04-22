"""Click CLI entry point — group + all 7 commands."""

import importlib.metadata
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import click
from click.shell_completion import CompletionItem

import seekr_hatchery.agents as agent
import seekr_hatchery.docker as docker
import seekr_hatchery.git as git
import seekr_hatchery.tasks as tasks
import seekr_hatchery.ui as ui
import seekr_hatchery.user_config as user_config

logger = logging.getLogger("hatchery")


def configure_logging(level: str, log_file: str | None = None) -> None:
    numeric = getattr(logging, level.upper(), logging.WARNING)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    handler: logging.Handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ui.ColorFormatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)

    if log_file:
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    logger.setLevel(numeric)


try:
    _version = importlib.metadata.version("seekr-hatchery")
except importlib.metadata.PackageNotFoundError:
    _version = "0.0.0+dev"


# ---------------------------------------------------------------------------
# Update check
# ---------------------------------------------------------------------------

_UPDATE_CHECK_CACHE = Path.home() / ".hatchery" / "update-check.json"
_UPDATE_CHECK_TTL_SECONDS = 86400  # 24 hours (successful fetch)
_UPDATE_CHECK_RETRY_SECONDS = 3600  # 1 hour (failed fetch / server unreachable)
_SIMPLE_URL = "https://pypi.org/simple/seekr-hatchery/"


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse 'X.Y.Z' (or 'X.Y.Z.devN') into a sortable integer tuple."""
    return tuple(int(x) for x in v.split(".")[:3] if x.isdigit())


def _fetch_latest_pypi_version() -> str | None:
    """Return the latest seekr-hatchery version from the PyPI simple index."""
    try:
        with urllib.request.urlopen(_SIMPLE_URL, timeout=3) as resp:  # noqa: S310
            content = resp.read().decode()
        versions = re.findall(r"seekr.hatchery-([0-9]+\.[0-9]+\.[0-9]+)", content)
        if not versions:
            return None
        return max(set(versions), key=_parse_version)
    except Exception:
        return None


def _check_for_update() -> tuple[str, str] | None:
    """Return (latest, current) if an update is available, else None.

    Uses a 24-hour on-disk cache to avoid hitting the registry on every
    invocation. Failed fetches (e.g. off VPN) are also cached so that a
    single unreachable server doesn't add a timeout delay to every command.
    All errors are silently swallowed so the CLI never fails due to this check.
    """
    try:
        now = datetime.now(tz=timezone.utc)

        # Read cache — a fresh entry suppresses the network fetch even if
        # latest_version is None (meaning the last fetch failed).
        needs_fetch = True
        latest: str | None = None
        if _UPDATE_CHECK_CACHE.exists():
            try:
                cached = json.loads(_UPDATE_CHECK_CACHE.read_text())
                checked_at = datetime.fromisoformat(cached["checked_at"])
                latest = cached.get("latest_version")
                ttl = _UPDATE_CHECK_TTL_SECONDS if latest else _UPDATE_CHECK_RETRY_SECONDS
                if (now - checked_at).total_seconds() < ttl:
                    needs_fetch = False
            except Exception:
                pass  # corrupt cache — fall through to a fresh fetch

        if needs_fetch:
            latest = _fetch_latest_pypi_version()
            try:
                _UPDATE_CHECK_CACHE.parent.mkdir(parents=True, exist_ok=True)
                _UPDATE_CHECK_CACHE.write_text(json.dumps({"checked_at": now.isoformat(), "latest_version": latest}))
            except Exception:
                pass  # non-fatal if we can't write the cache

        if latest and _parse_version(latest) > _parse_version(_version):
            return (latest, _version)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Launch helpers
# ---------------------------------------------------------------------------


def _is_task_complete(content: str) -> bool:
    return bool(re.search(r"^\*\*Status\*\*:\s*complete", content, re.MULTILINE))


def _set_task_status(repo: Path, name: str, status: str) -> None:
    meta = tasks.load_task(repo, name)
    meta["status"] = status
    tasks.save_task(meta)


def _do_mark_done(name: str, repo: Path, worktree: Path) -> None:
    meta = tasks.load_task(repo, name)
    no_worktree = meta.get("no_worktree", False)

    if not no_worktree:
        if worktree.exists():
            task_path = tasks.find_task_file(worktree, name)
            if task_path and task_path.exists():
                content = task_path.read_text()
                if "## Summary" not in content:
                    ui.warn("task file has no ## Summary section — agent may not have completed cleanly.")

            if git.has_uncommitted_changes(worktree):
                ui.warn("worktree has uncommitted changes.")
                ui.info(git.uncommitted_changes_summary(worktree))
                answer = input("Commit them as a final checkpoint before removing? [Y/n] ").strip().lower()
                if answer != "n":
                    tasks.run(["git", "add", "-A"], cwd=worktree)
                    tasks.run(["git", "commit", "-m", f"task({name}): final checkpoint"], cwd=worktree)

            git.remove_worktree(repo, worktree)
            ui.info(f"Worktree removed: {worktree}")
        else:
            ui.info(f"Worktree already gone: {worktree}")

    meta["status"] = "complete"
    meta["completed"] = datetime.now().isoformat()
    tasks.save_task(meta)

    if not no_worktree:
        ui.info(f"Branch retained: {meta['branch']}")
    ui.success(f"Task '{name}' marked complete.")


_WRAP_UP_PROMPT = (
    "The user has indicated they believe this task is complete. "
    "Please review the task file and assess whether the work is actually done. "
    "If it is complete, update the task file: set **Status** to `complete`, "
    "fill in the `## Summary` section documenting key decisions, patterns "
    "established, gotchas, and anything a future agent should know, then remove "
    "the `## Agreed Plan` and `## Progress Log` sections — they are working "
    "scaffolding, not permanent record. The final file should read as a clean "
    "ADR: Status/Branch/Created → Objective → Context → Summary. "
    "If work remains unfinished, tell the user what is still outstanding and ask "
    "whether they want to continue working or close the task out anyway."
)


def _do_delete(name: str, repo: Path, worktree: Path, meta: dict, *, confirmed: bool = False) -> None:
    branch = meta["branch"]
    no_worktree = meta.get("no_worktree", False)

    if not no_worktree:
        if worktree.exists():
            if git.has_uncommitted_changes(worktree):
                ui.warn(f"Task '{name}': there are uncommitted changes in the worktree.")
            if not confirmed:
                answer = input(f"Delete task '{name}' (worktree + branch + metadata)? [y/N] ").strip().lower()
                if answer != "y":
                    ui.info("Aborted.")
                    return
            git.remove_worktree(repo, worktree, force=True)
        else:
            if not confirmed:
                answer = input(f"Delete task '{name}' (branch + metadata)? [y/N] ").strip().lower()
                if answer != "y":
                    ui.info("Aborted.")
                    return
        if git.delete_branch(repo, branch):
            ui.info(f"Branch deleted: {branch}")
        else:
            ui.info(f"Could not delete branch {branch} (may already be gone)")
    else:
        if not confirmed:
            answer = input(f"Delete task '{name}' (metadata only)? [y/N] ").strip().lower()
            if answer != "y":
                ui.info("Aborted.")
                return

    tasks.task_db_path(repo, name).unlink(missing_ok=True)
    ui.success(f"Task '{name}' deleted.")


def _docker_context(
    runtime: docker.Runtime | None,
    worktree: Path | None,
    repo: Path,
) -> tuple[docker.DockerConfig | None, list[str], str]:
    """Return (config, features, container_workdir) for the three launch functions.

    Pass worktree=None for no-worktree mode; the container path becomes /workspace.
    """
    config = docker.load_docker_config(worktree or repo) if runtime else None
    features = docker.docker_features(config) if config else []
    if runtime:
        container_workdir = (
            "/workspace" if worktree is None else f"{tasks.CONTAINER_REPO_ROOT}/{worktree.relative_to(repo)}"
        )
    else:
        container_workdir = ""
    return config, features, container_workdir


def _launch_finalize(
    repo: Path,
    worktree: Path,
    name: str,
    session_id: str,
    backend: agent.AgentBackend,
    runtime: docker.Runtime | None,
    branch: str,
    main_branch: str,
    no_worktree: bool = False,
) -> None:
    env_ctx = tasks.sandbox_context(name, branch, worktree, repo, main_branch, bool(runtime), no_worktree)
    system_prompt = tasks.SESSION_SYSTEM + "\n" + env_ctx
    config, features, container_workdir = _docker_context(runtime, None if no_worktree else worktree, repo)
    agent_cmd = backend.build_finalize_command(
        session_id, system_prompt, _WRAP_UP_PROMPT, docker=bool(runtime), workdir=container_workdir
    )
    ui.banner(name, repo, branch=branch, sandbox=bool(runtime), worktree=not no_worktree, features=features)
    _set_task_status(repo, name, "running")
    try:
        if runtime:
            if no_worktree:
                docker.launch_docker_no_worktree(worktree, name, backend, agent_cmd, config, runtime)
            else:
                docker.launch_docker(repo, worktree, name, backend, agent_cmd, config, runtime)
        else:
            os.chdir(worktree)
            subprocess.run(agent_cmd, env=_session_env(name, repo))
    finally:
        _set_task_status(repo, name, "in-progress")
    _post_exit_check(
        name, repo, worktree, branch=branch, sandbox=bool(runtime), no_worktree=no_worktree, features=features
    )


def _post_exit_check(
    name: str,
    repo: Path,
    worktree: Path,
    branch: str = "",
    sandbox: bool = False,
    no_worktree: bool = False,
    features: list[str] | None = None,
) -> None:
    task_path = tasks.find_task_file(worktree, name)
    if task_path and task_path.exists():
        content = task_path.read_text()
        if _is_task_complete(content):
            click.echo()
            ui.banner(name, repo, branch=branch, sandbox=sandbox, worktree=not no_worktree, features=features)
            ui.success("  Task appears complete.")
            answer = input(f"Mark task '{name}' as done? [Y/n] ").strip().lower()
            if answer != "n":
                _do_mark_done(name, repo, worktree)
            else:
                ui.info(f"Use `hatchery done {name}` to mark complete later.")
            return
    # Not complete (or file not found)
    click.echo()
    ui.banner(name, repo, branch=branch, sandbox=sandbox, worktree=not no_worktree, features=features)
    ui.warn("  Task is not complete.")
    click.echo()
    ui.info("  w) Wrap up         — relaunch agent to finalize the task file")
    ui.info("  x) Delete          — remove this task permanently")
    ui.info(f"  l) Leave for later — exit now; resume with `hatchery resume {name}`")
    click.echo()
    choice = input("Choice [w/x/l, Enter = leave for later]: ").strip().lower()
    if choice == "w":
        meta = tasks.load_task(repo, name)
        session_id = meta.get("session_id")
        if not session_id:
            ui.error("no session ID found. Cannot relaunch.")
            return
        backend = agent.from_kind(meta.get("agent", "CODEX"))
        runtime = docker.resolve_runtime(repo, worktree, no_docker=not sandbox, backend=backend)
        main_branch = git.get_default_branch(repo)
        _launch_finalize(repo, worktree, name, session_id, backend, runtime, meta["branch"], main_branch, no_worktree)
    elif choice == "x":
        meta = tasks.load_task(repo, name)
        _do_delete(name, repo, worktree, meta)
    else:
        ui.info(f"Use `hatchery resume {name}` to continue.")


def _prompt_objective() -> str:
    """Rich multi-line prompt for the task description (prompt_toolkit)."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys

    # Shift+Enter sequences are in the ANSI table but mapped to ControlM (same
    # as plain Enter), making them indistinguishable.  Remap them to ControlJ
    # so we can bind them to "insert newline" instead of "submit".
    #   \x1b[27;2;13~  — xterm modifyOtherKeys (iTerm2, xterm, etc.)
    #   \x1b[13;2u     — kitty keyboard protocol
    ANSI_SEQUENCES["\x1b[27;2;13~"] = Keys.ControlJ
    ANSI_SEQUENCES["\x1b[13;2u"] = Keys.ControlJ

    kb = KeyBindings()

    @kb.add("c-j")  # shift+enter (remapped above)
    @kb.add("escape", "enter")  # alt+enter fallback
    def _(event) -> None:
        event.current_buffer.insert_text("\n")

    @kb.add("enter")
    def _(event) -> None:
        event.current_buffer.validate_and_handle()

    click.echo("\nDescribe the task:\n")
    session: PromptSession[str] = PromptSession(key_bindings=kb, multiline=True)
    return session.prompt("> ").strip()


def _session_env(name: str, repo: Path) -> dict[str, str]:
    """Env vars that identify the hatchery session to child processes (e.g. statusline scripts)."""
    return {**os.environ, "HATCHERY_TASK": name, "HATCHERY_REPO": str(repo)}


def _launch_new(
    repo: Path,
    worktree: Path,
    name: str,
    session_id: str,
    backend: agent.AgentBackend,
    runtime: docker.Runtime | None,
    branch: str,
    main_branch: str,
    no_worktree: bool = False,
    is_chat: bool = False,
    no_cache: bool = False,
) -> None:
    session_dir = tasks.task_session_dir(repo, name)
    backend.on_new_task(session_dir)
    backend.on_before_launch(worktree)
    if is_chat:
        system_prompt = ""
        initial_prompt = ""
    else:
        env_ctx = tasks.sandbox_context(name, branch, worktree, repo, main_branch, bool(runtime), no_worktree)
        system_prompt = tasks.SESSION_SYSTEM + "\n" + env_ctx
        initial_prompt = tasks.session_prompt(name, worktree)
    config, features, container_workdir = _docker_context(runtime, None if no_worktree else worktree, repo)
    agent_cmd = backend.build_new_command(
        session_id, system_prompt, initial_prompt, docker=bool(runtime), workdir=container_workdir
    )
    if is_chat:
        ui.chat_banner(name, repo, features=features)
    else:
        ui.banner(name, repo, branch=branch, sandbox=bool(runtime), worktree=not no_worktree, features=features)
    _set_task_status(repo, name, "running")
    try:
        if runtime:
            if no_worktree:
                docker.launch_docker_no_worktree(worktree, name, backend, agent_cmd, config, runtime, no_cache=no_cache)
            else:
                docker.launch_docker(repo, worktree, name, backend, agent_cmd, config, runtime, no_cache=no_cache)
        else:
            os.chdir(worktree)
            subprocess.run(agent_cmd, env=_session_env(name, repo))
    finally:
        _set_task_status(repo, name, "in-progress")
    if is_chat:
        _chat_post_exit(name, repo)
    else:
        _post_exit_check(
            name, repo, worktree, branch=branch, sandbox=bool(runtime), no_worktree=no_worktree, features=features
        )


def _launch_resume(
    repo: Path,
    worktree: Path,
    name: str,
    session_id: str,
    backend: agent.AgentBackend,
    runtime: docker.Runtime | None,
    branch: str,
    main_branch: str,
    no_worktree: bool = False,
    is_chat: bool = False,
    no_cache: bool = False,
) -> None:
    backend.on_before_launch(worktree)
    if is_chat:
        system_prompt = ""
        initial_prompt = ""
    else:
        env_ctx = tasks.sandbox_context(name, branch, worktree, repo, main_branch, bool(runtime), no_worktree)
        system_prompt = tasks.SESSION_SYSTEM + "\n" + env_ctx
        initial_prompt = tasks.session_prompt(name, worktree)
    config, features, container_workdir = _docker_context(runtime, None if no_worktree else worktree, repo)
    agent_cmd = backend.build_resume_command(
        session_id, system_prompt, initial_prompt, docker=bool(runtime), workdir=container_workdir
    )
    if is_chat:
        ui.chat_banner(name, repo, features=features)
    else:
        ui.banner(name, repo, branch=branch, sandbox=bool(runtime), worktree=not no_worktree, features=features)
    _set_task_status(repo, name, "running")
    try:
        if runtime:
            if no_worktree:
                docker.launch_docker_no_worktree(worktree, name, backend, agent_cmd, config, runtime, no_cache=no_cache)
            else:
                docker.launch_docker(repo, worktree, name, backend, agent_cmd, config, runtime, no_cache=no_cache)
        else:
            os.chdir(worktree)
            subprocess.run(agent_cmd, env=_session_env(name, repo))
    finally:
        _set_task_status(repo, name, "in-progress")
    if is_chat:
        _chat_post_exit(name, repo)
    else:
        _post_exit_check(
            name, repo, worktree, branch=branch, sandbox=bool(runtime), no_worktree=no_worktree, features=features
        )


def _next_chat_name(repo: Path) -> str:
    """Return the lowest available chat-N name for this repo."""
    existing = tasks.repo_tasks_for_current_repo(repo)
    used: set[int] = set()
    for meta in existing:
        if meta.get("type") == "chat":
            name = meta.get("name", "")
            if name.startswith("chat-") and name[5:].isdigit():
                used.add(int(name[5:]))
    n = 1
    while n in used:
        n += 1
    return f"chat-{n}"


def _chat_post_exit(name: str, repo: Path) -> None:
    """Simple post-exit for chat: offer to mark complete."""
    answer = input(f"\nMark chat '{name}' as complete? [Y/n] ").strip().lower()
    if answer != "n":
        meta = tasks.load_task(repo, name)
        meta["status"] = "complete"
        meta["completed"] = datetime.now().isoformat()
        tasks.save_task(meta)
        ui.success(f"Chat '{name}' marked complete.")
    else:
        ui.info(f"Chat '{name}' left in-progress. Resume with: hatchery resume {name}")


# ---------------------------------------------------------------------------
# Shell completion
# ---------------------------------------------------------------------------


class TaskNameType(click.ParamType):
    """Click parameter type that tab-completes existing task names."""

    name = "name"

    def convert(self, value: str, param: click.Parameter | None, ctx: click.Context | None) -> str:
        return value

    def shell_complete(
        self,
        ctx: click.Context,
        param: click.Parameter,
        incomplete: str,
    ) -> list:
        try:
            repo, _ = git.git_root_or_cwd()
            all_tasks = tasks.repo_tasks_for_current_repo(repo)
            return [CompletionItem(t["name"]) for t in all_tasks if t.get("name", "").startswith(incomplete)]
        except Exception:
            return []


TASK_NAME = TaskNameType()


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


class AliasedGroup(click.Group):
    """Click Group subclass that supports short aliases shown in --help."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        # alias -> primary command name
        self._aliases: dict[str, str] = {}

    def add_alias(self, alias: str, target: str) -> None:
        self._aliases[alias] = target

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        return super().get_command(ctx, self._aliases.get(cmd_name, cmd_name))

    def list_commands(self, ctx: click.Context) -> list[str]:
        # Include aliases in tab-completion
        return sorted(list(self.commands) + list(self._aliases))

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        # Build reverse map: primary -> sorted aliases
        primary_to_aliases: dict[str, list[str]] = {}
        for alias, primary in self._aliases.items():
            primary_to_aliases.setdefault(primary, []).append(alias)

        commands = []
        for name in self.commands:
            cmd = self.commands[name]
            if cmd.hidden:
                continue
            aliases = sorted(primary_to_aliases.get(name, []))
            parts = aliases + [name] if aliases else [name]
            display = " | ".join(parts)
            commands.append((display, cmd.get_short_help_str(limit=formatter.width)))

        if commands:
            with formatter.section("Commands"):
                formatter.write_dl(commands)


@click.group(cls=AliasedGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(version=_version, prog_name="hatchery")
@click.option(
    "--log-level",
    default="WARNING",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]),
    help="Set log verbosity (default: WARNING)",
)
@click.option("--log-file", type=click.Path(), default=None, help="Also write logs to this file")
def cli(log_level: str, log_file: str | None) -> None:
    """AI coding agent task orchestration."""
    configure_logging(log_level, log_file)
    tasks.migrate_db()
    if not os.environ.get("_HATCHERY_COMPLETE"):
        update = _check_for_update()
        if update:
            latest, current = update
            ui.warn(
                f"hatchery {latest} is available (you have {current}). Run: hatchery self update",
            )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@cli.command("new")
@click.argument("name")
@click.option(
    "--from",
    "base",
    default=tasks.DEFAULT_BASE,
    metavar="REF",
    help=f"Branch or commit to fork from (default: {tasks.DEFAULT_BASE})",
)
@click.option("--no-docker", is_flag=True, help="Run agent directly, even if a Dockerfile is present")
@click.option(
    "--no-worktree",
    is_flag=True,
    help="Work directly in the current directory; skip worktree and branch creation",
)
@click.option("--editor/--no-editor", default=None, help="Open $EDITOR for the task file (default: from config)")
@click.option(
    "--agent",
    "agent_name",
    default=None,
    type=click.Choice(["codex"], case_sensitive=False),
    help="Agent to use (auto-detected if not specified)",
)
@click.option(
    "--rebuild-sandbox",
    "rebuild_sandbox",
    is_flag=True,
    help="Rebuild the sandbox image from scratch, ignoring the layer cache",
)
@click.option(
    "--no-commit-docker",
    "no_commit_docker",
    is_flag=True,
    help=(
        "Write the generated Dockerfile and docker.yaml to the repo root instead of "
        "the worktree branch, and skip the automatic commit. "
        "Useful for keeping Docker config out of version control. "
        "If you forgot this flag on your first run, use "
        "`git rm --cached .hatchery/Dockerfile.<agent> .hatchery/docker.yaml` to undo."
    ),
)
def cmd_new(
    name: str,
    base: str,
    no_docker: bool,
    no_worktree: bool,
    editor: bool | None,
    agent_name: str,
    rebuild_sandbox: bool,
    no_commit_docker: bool,
) -> None:
    """Start a new task."""
    ui.hatchery_header(_version)
    repo, in_repo = git.git_root_or_cwd()
    if not in_repo:
        no_worktree = True
        no_docker = True
        ui.note("not in a git repository — running without worktree isolation or Docker sandbox.")

    cfg = user_config.UserConfig.load()
    backend = cfg.resolve_backend(agent_name)
    use_editor = editor if editor is not None else cfg.open_editor

    tasks.ensure_tasks_dir(repo)
    if in_repo:
        tasks.ensure_gitignore(repo)

    name = tasks.to_name(name)
    db_path = tasks.task_db_path(repo, name)
    if db_path.exists():
        existing = json.loads(db_path.read_text())
        if existing.get("status") in ("in-progress", "running"):
            ui.error(f"task '{name}' is already in-progress. Choose a different name.")
            sys.exit(1)
        # completed/aborted → allow overwrite (git task file is permanent record)

    session_id = str(uuid.uuid4())

    if no_worktree:
        worktree = repo
        branch = ""
        ui.info(f"Creating task: {name}")
    else:
        branch = f"hatchery/{name}"
        worktree = tasks.worktrees_dir(repo) / name
        ui.info(f"Creating task: {name}")
        git.create_worktree(repo, branch, worktree, base)

    try:
        if in_repo:
            if no_commit_docker:
                # Generate files to the repo root so they stay uncommitted.
                # The source=repo calls below will then copy them into the worktree
                # for this session without committing.
                docker.ensure_dockerfile(repo, backend)
                docker.ensure_docker_config(repo)
            df_created = docker.ensure_dockerfile(worktree, backend, source=repo)
            dc_created = docker.ensure_docker_config(worktree, source=repo)
            if df_created or dc_created:
                ui.info("  Committing...")
                tasks.run(
                    [
                        "git",
                        "add",
                        str(docker.dockerfile_path(worktree, backend).relative_to(worktree)),
                        str(tasks.DOCKER_CONFIG),
                    ],
                    cwd=worktree,
                )
                tasks.run(
                    ["git", "commit", "-m", "chore: add hatchery Docker configuration"],
                    cwd=worktree,
                )

        if use_editor:
            task_path = tasks.write_task_file(worktree, name, branch)
            content_before = task_path.read_text()
            click.echo("\nOpening task file for editing...")
            tasks.open_for_editing(task_path)
            if task_path.read_text() == content_before:
                ui.warn("Task file unchanged — cancelled.")
                if not no_worktree:
                    git.remove_worktree(repo, worktree)
                    git.delete_branch(repo, branch)
                sys.exit(1)
        else:
            objective = _prompt_objective()
            task_path = tasks.write_task_file(worktree, name, branch, objective=objective)

        if in_repo:
            add_path = ".hatchery/tasks/" if no_commit_docker else ".hatchery/"
            tasks.run(["git", "add", add_path], cwd=worktree)
            tasks.run(["git", "commit", "-m", f"task({name}): add task file"], cwd=worktree)

        meta = {
            "name": name,
            "branch": branch,
            "worktree": str(worktree),
            "repo": str(repo),
            "status": "in-progress",
            "created": datetime.now().isoformat(),
            "session_id": session_id,  # internal only, not shown in normal output
            "no_worktree": no_worktree,
            "agent": backend.kind,
        }
        tasks.save_task(meta)

        runtime = docker.resolve_runtime(repo, worktree, no_docker, backend=backend)
        main_branch = git.get_default_branch(repo)
        _launch_new(
            repo,
            worktree,
            name,
            session_id,
            backend,
            runtime,
            branch,
            main_branch,
            no_worktree,
            no_cache=rebuild_sandbox,
        )
    except KeyboardInterrupt:
        if not no_worktree:
            git.remove_worktree(repo, worktree)
            git.delete_branch(repo, branch)
        ui.warn("Cancelled.")
        sys.exit(1)


@cli.command("chat")
@click.argument("name", required=False, default=None, type=TASK_NAME)
@click.option(
    "--agent",
    "agent_name",
    default=None,
    type=click.Choice(["codex"], case_sensitive=False),
    help="Agent to use (auto-detected if not specified)",
)
def cmd_chat(name: str | None, agent_name: str) -> None:
    """Start a free-form chat session in a sandbox."""
    ui.hatchery_header(_version)
    repo, in_repo = git.git_root_or_cwd()
    if not in_repo:
        ui.error("chat requires a git repository (needed for Docker scaffolding).")
        sys.exit(1)

    cfg = user_config.UserConfig.load()
    backend = cfg.resolve_backend(agent_name)

    # Auto-generate name if not provided
    if name is None:
        name = _next_chat_name(repo)
    else:
        name = tasks.to_name(name)

    db_path = tasks.task_db_path(repo, name)
    if db_path.exists():
        existing = json.loads(db_path.read_text())
        if existing.get("status") in ("in-progress", "running"):
            ui.error(f"chat '{name}' is already in-progress. Choose a different name or resume it.")
            sys.exit(1)

    # Ensure Docker scaffolding
    df_created = docker.ensure_dockerfile(repo, backend)
    dc_created = docker.ensure_docker_config(repo)
    if df_created or dc_created:
        ui.info("  Committing...")
        tasks.run(
            ["git", "add", str(docker.dockerfile_path(repo, backend).relative_to(repo)), str(tasks.DOCKER_CONFIG)],
            cwd=repo,
        )
        tasks.run(
            ["git", "commit", "-m", "chore: add hatchery Docker configuration"],
            cwd=repo,
        )

    runtime = docker.resolve_runtime(repo, repo, no_docker=False, backend=backend)

    session_id = str(uuid.uuid4())
    meta = {
        "name": name,
        "type": "chat",
        "branch": "",
        "worktree": str(repo),
        "repo": str(repo),
        "status": "in-progress",
        "created": datetime.now().isoformat(),
        "session_id": session_id,
        "no_worktree": True,
        "agent": backend.kind,
    }
    tasks.save_task(meta)

    main_branch = git.get_default_branch(repo)
    _launch_new(repo, repo, name, session_id, backend, runtime, "", main_branch, no_worktree=True, is_chat=True)


@cli.command("resume")
@click.argument("name", type=TASK_NAME)
@click.option("--no-docker", is_flag=True, help="Run agent directly, even if a Dockerfile is present")
@click.option(
    "--rebuild-sandbox",
    "rebuild_sandbox",
    is_flag=True,
    help="Rebuild the sandbox image from scratch, ignoring the layer cache",
)
def cmd_resume(name: str, no_docker: bool, rebuild_sandbox: bool) -> None:
    """Resume exactly where you left off."""
    ui.hatchery_header(_version)
    repo, _ = git.git_root_or_cwd()
    meta = tasks.load_task(repo, name)
    worktree = Path(meta["worktree"])
    repo = Path(meta["repo"])
    no_worktree = meta.get("no_worktree", False)

    backend = agent.from_kind(meta.get("agent", "CODEX"))

    if not no_worktree and not worktree.exists():
        if meta.get("status") == "archived":
            ui.info(f"Re-creating worktree for archived task '{name}'...")
            git.create_worktree(repo, meta["branch"], worktree, meta["branch"])
            meta["status"] = "in-progress"
            tasks.save_task(meta)
            ui.success(f"Worktree restored: {worktree}")
        else:
            ui.error(f"worktree {worktree} does not exist. Has this task been completed?")
            sys.exit(1)

    session_id = meta.get("session_id")
    if not session_id:
        ui.error("no session ID found for this task. Cannot resume.")
        sys.exit(1)

    if not backend.supports_sessions:
        ui.note(
            f"'{backend.kind}' does not support session resumption; "
            "starting a fresh session with the current task file as context."
        )

    if meta.get("status") == "running":
        ui.note(f"task '{name}' was marked as running — a previous session may have exited unexpectedly.")

    is_chat = meta.get("type") == "chat"
    runtime = docker.resolve_runtime(repo, worktree, no_docker, backend=backend)
    main_branch = git.get_default_branch(repo)
    _launch_resume(
        repo,
        worktree,
        name,
        session_id,
        backend,
        runtime,
        meta["branch"],
        main_branch,
        no_worktree,
        is_chat=is_chat,
        no_cache=rebuild_sandbox,
    )


@cli.command("sandbox")
@click.option("--shell", default="/bin/bash", help="Shell to launch (default: /bin/bash)")
@click.option(
    "--rebuild-sandbox",
    "rebuild_sandbox",
    is_flag=True,
    help="Rebuild the sandbox image from scratch, ignoring the layer cache",
)
def cmd_sandbox(shell: str, rebuild_sandbox: bool) -> None:
    """Drop into an interactive shell inside the Docker sandbox."""
    repo, in_repo = git.git_root_or_cwd()
    cfg = user_config.UserConfig.load()
    backend = cfg.resolve_backend(None)
    tasks.ensure_tasks_dir(repo)
    df_created = docker.ensure_dockerfile(repo, backend)
    dc_created = docker.ensure_docker_config(repo)
    if in_repo and (df_created or dc_created):
        ui.info("  Committing...")
        tasks.run(
            ["git", "add", str(docker.dockerfile_path(repo, backend).relative_to(repo)), str(tasks.DOCKER_CONFIG)],
            cwd=repo,
        )
        tasks.run(
            ["git", "commit", "-m", "chore: add hatchery Docker configuration"],
            cwd=repo,
        )
    runtime = docker.detect_runtime()
    config = docker.load_docker_config(repo)
    features = docker.docker_features(config)
    ui.banner("sandbox", repo, sandbox=True, worktree=False, features=features)
    docker.launch_sandbox_shell(repo, backend, config, runtime, shell=shell, no_cache=rebuild_sandbox)


@cli.command("exec")
@click.argument("name", type=TASK_NAME)
@click.option("--shell", default="/bin/bash", help="Shell to launch (default: /bin/bash)")
def cmd_exec(name: str, shell: str) -> None:
    """Exec an interactive shell into a running task's container."""
    repo, _ = git.git_root_or_cwd()
    runtime = docker.detect_runtime()
    docker.exec_task_shell(name, runtime, repo, shell=shell)


@cli.command("done")
@click.argument("names", nargs=-1, required=True, type=TASK_NAME)
def cmd_done(names: tuple[str, ...]) -> None:
    """Mark complete and remove worktree."""
    repo, _ = git.git_root_or_cwd()
    for name in names:
        meta = tasks.load_task(repo, name)
        _do_mark_done(meta["name"], Path(meta["repo"]), Path(meta["worktree"]))


@cli.command("abort", hidden=True)
@click.argument("name")
def cmd_abort(name: str) -> None:
    """Removed: use `archive` instead."""
    ui.error("'abort' has been replaced by 'archive'. Use: hatchery archive <name>")
    sys.exit(1)


@cli.command("archive")
@click.argument("names", nargs=-1, required=True, type=TASK_NAME)
def cmd_archive(names: tuple[str, ...]) -> None:
    """Park a task: remove worktree, keep branch for later resumption."""
    repo, _ = git.git_root_or_cwd()
    for name in names:
        meta = tasks.load_task(repo, name)
        worktree = Path(meta["worktree"])
        task_repo = Path(meta["repo"])
        no_worktree = meta.get("no_worktree", False)

        if no_worktree:
            ui.note(f"Task '{name}': no-worktree task — no worktree or branch to remove.")
        else:
            if worktree.exists():
                if git.has_uncommitted_changes(worktree):
                    ui.warn(f"Task '{name}': there are uncommitted changes in the worktree.")
                    ui.info(git.uncommitted_changes_summary(worktree))
                    answer = input("Commit them as a checkpoint before archiving? [Y/n] ").strip().lower()
                    if answer != "n":
                        tasks.run(["git", "add", "-A"], cwd=worktree)
                        tasks.run(["git", "commit", "-m", f"task({name}): checkpoint before archive"], cwd=worktree)
                git.remove_worktree(task_repo, worktree)
                ui.info(f"Worktree removed: {worktree}")
            else:
                ui.info(f"Task '{name}': worktree not found (may already be removed).")
            ui.info(f"Branch retained: {meta['branch']}")

        meta["status"] = "archived"
        tasks.save_task(meta)
        ui.success(f"Task '{name}' archived. Resume with: hatchery resume {name}")


@cli.command("delete")
@click.argument("names", nargs=-1, required=True, type=TASK_NAME)
@click.option("-f", "--force", is_flag=True, help="Skip confirmation prompt.")
def cmd_delete(names: tuple[str, ...], force: bool) -> None:
    """Delete task, branch, and all metadata."""
    repo, _ = git.git_root_or_cwd()
    if len(names) > 1:
        metas = [tasks.load_task(repo, n) for n in names]
        if not force:
            answer = input(f"Delete {len(names)} tasks ({', '.join(names)})? [y/N] ").strip().lower()
            if answer != "y":
                ui.info("Aborted.")
                return
        for meta in metas:
            _do_delete(meta["name"], repo, Path(meta["worktree"]), meta, confirmed=True)
    else:
        meta = tasks.load_task(repo, names[0])
        _do_delete(meta["name"], repo, Path(meta["worktree"]), meta, confirmed=force)


@cli.command("list")
@click.option("-a", "--all", "show_all", is_flag=True, help="Show all tasks, including completed and aborted")
def cmd_list(show_all: bool) -> None:
    """List tasks for current repo."""
    repo, _ = git.git_root_or_cwd()
    task_list = tasks.repo_tasks_for_current_repo(repo)

    archived_count = 0
    if not show_all:
        archived_count = sum(1 for t in task_list if t.get("status") == "archived")
        task_list = [t for t in task_list if t.get("status") in ("in-progress", "running")]

    ui.task_list_table(task_list, archived_count, show_all)




@cli.command("status")
@click.argument("name", type=TASK_NAME)
def cmd_status(name: str) -> None:
    """Show task file and metadata."""
    repo, _ = git.git_root_or_cwd()
    meta = tasks.load_task(repo, name)
    worktree = Path(meta["worktree"])
    task_path = tasks.find_task_file(worktree, name)

    click.echo(click.style("Name:     ", bold=True) + meta["name"])
    click.echo(click.style("Type:     ", bold=True) + meta.get("type", "task"))
    click.echo(click.style("Status:   ", bold=True) + meta["status"])
    click.echo(click.style("Agent:    ", bold=True) + meta.get("agent", "CODEX").lower())
    click.echo(click.style("Branch:   ", bold=True) + meta["branch"])
    click.echo(click.style("Worktree: ", bold=True) + meta["worktree"])
    click.echo(click.style("Created:  ", bold=True) + meta.get("created", "unknown")[:16])
    if meta.get("completed"):
        click.echo(click.style("Completed:", bold=True) + meta["completed"][:16])
    # Session ID shown here for debugging, kept out of normal workflow
    click.echo(click.style("Session:  ", bold=True) + meta.get("session_id", "none"))

    if task_path and task_path.exists():
        click.echo()
        click.echo("─" * 60)
        click.echo(task_path.read_text())
    else:
        click.echo("\n(Task file not accessible — worktree may have been removed)")



@cli.command("shell")
@click.argument("name", type=TASK_NAME)
def cmd_shell(name: str) -> None:
    """Open a native shell in the task's worktree."""
    repo, _ = git.git_root_or_cwd()
    meta = tasks.load_task(repo, name)
    worktree = Path(meta["worktree"])
    if not worktree.exists():
        ui.error(f"Worktree not found: {worktree}")
        sys.exit(1)
    shell = os.environ.get("SHELL", "bash")
    ui.note(f"Opening shell in {worktree}  (exit with Ctrl-D or 'exit')")
    subprocess.run([shell], cwd=worktree)


# ---------------------------------------------------------------------------
# Config command group
# ---------------------------------------------------------------------------


@cli.group("config")
def cmd_config() -> None:
    """View and edit hatchery configuration."""


@cmd_config.command("edit")
def cmd_config_edit() -> None:
    """Open config in $EDITOR with validation."""
    config_path = user_config.UserConfig.CONFIG_PATH
    # Backup the original file before we touch it
    backup_path = config_path.with_suffix(".json.bak")
    if config_path.exists():
        shutil.copy2(config_path, backup_path)
    # Load, migrate, fill defaults, and write back so the user sees all options
    cfg = user_config.UserConfig.load()
    cfg.save()
    # Edit → validate loop
    while True:
        tasks.open_for_editing(config_path)
        error = user_config.validate_config_file(config_path)
        if error is None:
            break
        ui.error("Invalid config:")
        ui.info(error)
        answer = input("Continue editing? [Y/n] ").strip().lower()
        if answer == "n":
            if backup_path.exists():
                shutil.copy2(backup_path, config_path)
                backup_path.unlink()
            else:
                config_path.unlink(missing_ok=True)
            ui.warn("Restored previous config.")
            sys.exit(1)
    backup_path.unlink(missing_ok=True)
    ui.success("Config updated.")


# ---------------------------------------------------------------------------
# Self command group
# ---------------------------------------------------------------------------


@cli.group("self")
def cmd_self() -> None:
    """Manage the hatchery installation."""


@cmd_self.command("update")
def cmd_self_update() -> None:
    """Upgrade hatchery to the latest release."""
    uv_receipt = Path.home() / ".local/share/uv/tools/seekr-hatchery/uv-receipt.toml"
    if uv_receipt.exists() and shutil.which("uv"):
        result = subprocess.run(["uv", "tool", "upgrade", "seekr-hatchery"])
        sys.exit(result.returncode)
    ui.error("Could not detect uv tool installation.")
    ui.info("Run manually: uv tool upgrade seekr-hatchery")
    sys.exit(1)


@cmd_self.command("completions")
def cmd_self_completions() -> None:
    """Install shell tab-completion into your shell's rc file."""
    shell = Path(os.environ.get("SHELL", "")).name

    rc_files = {
        "bash": Path.home() / ".bashrc",
        "zsh": Path.home() / ".zshrc",
        "fish": Path.home() / ".config" / "fish" / "config.fish",
    }
    completion_lines = {
        "bash": 'eval "$(_HATCHERY_COMPLETE=bash_source hatchery)"',
        "zsh": 'eval "$(_HATCHERY_COMPLETE=zsh_source hatchery)"',
        "fish": "_HATCHERY_COMPLETE=fish_source hatchery | source",
    }

    if shell not in rc_files:
        ui.error(f"Unsupported shell: {shell!r}. Run `hatchery completion <shell>` for manual instructions.")
        sys.exit(1)

    rc_file = rc_files[shell]
    line = completion_lines[shell]

    if rc_file.exists() and "_HATCHERY_COMPLETE" in rc_file.read_text():
        ui.success(f"Shell completion is already installed in {rc_file}.")
        return

    rc_file.parent.mkdir(parents=True, exist_ok=True)
    with rc_file.open("a") as f:
        f.write(f"\n# hatchery shell completion\n{line}\n")

    ui.success(f"Completion installed in {rc_file}.")
    ui.note(f"Run `source {rc_file}` (or open a new terminal) to activate.")


# ---------------------------------------------------------------------------
# Aliases (shown as name|alias in --help, resolved in AliasedGroup)
# ---------------------------------------------------------------------------

cli.add_alias("ls", "list")
cli.add_alias("st", "status")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
