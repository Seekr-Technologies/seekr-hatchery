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
from datetime import datetime, timezone
from pathlib import Path

import click
from click.shell_completion import CompletionItem

import seekr_hatchery.agents as agent
import seekr_hatchery.docker as docker
import seekr_hatchery.git as git
import seekr_hatchery.sessions as sessions
import seekr_hatchery.ui as ui
import seekr_hatchery.user_config as user_config
from seekr_hatchery.constants import DEFAULT_BASE, DOCKER_CONFIG
from seekr_hatchery.includes import IncludeEntry, load_include_entries
from seekr_hatchery.utils import open_for_editing, run

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


def _cli_includes_to_entries(
    cli_worktree: tuple[Path, ...],
    cli_rw: tuple[Path, ...],
    cli_ro: tuple[Path, ...],
) -> list[IncludeEntry]:
    """Convert click's tuple-shaped --include flags into IncludeEntry objects.

    Click hands cmd_new/cmd_resume three separate tuples (one per mode);
    this helper grants them a single ordered list (worktree → rw → ro)
    with duplicates dropped. Sessions then merges this with docker.yaml
    entries via ``sessions.merge_includes_with_config``.
    """
    seen: set[Path] = set()
    result: list[IncludeEntry] = []
    for p, mode in (
        *((p, "worktree") for p in cli_worktree),
        *((p, "rw") for p in cli_rw),
        *((p, "ro") for p in cli_ro),
    ):
        resolved = p.resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(IncludeEntry(path=resolved, mode=mode))
    return result


def _do_mark_done(name: str, repo: Path, worktree: Path) -> None:
    """CLI-facing mark-done: handles interactive prompts, then delegates."""
    meta = sessions.load(repo, name)
    commit_changes = False

    if not meta.no_worktree and worktree.exists():
        task_path = sessions.find_task_file(worktree, name)
        if task_path and task_path.exists():
            content = task_path.read_text()
            if "## Summary" not in content:
                ui.warn("task file has no ## Summary section — agent may not have completed cleanly.")

        if not meta.no_commit and git.has_uncommitted_changes(worktree):
            ui.warn("worktree has uncommitted changes.")
            ui.info(git.uncommitted_changes_summary(worktree))
            answer = input("Commit them as a final checkpoint before removing? [Y/n] ").strip().lower()
            commit_changes = answer != "n"

    sessions.mark_done(meta, commit_changes=commit_changes)
    _cleanup_task(repo, name)


def _cleanup_task(repo: Path, name: str) -> None:
    """Remove per-task ephemeral state on terminal lifecycle transitions.

    Called from `_do_mark_done`, `_chat_post_exit`, and `_do_delete`.
    Currently just the clipboard image cache; future per-task ephemeral
    state (e.g. scratch dirs, agent caches) should be wired in here.
    """
    docker.remove_clipboard_dir(sessions.task_session_dir(repo, name))


def _do_delete(meta: sessions.SessionMeta, *, confirmed: bool = False) -> None:
    """CLI-facing delete: handles confirmation prompt, then delegates."""
    if not confirmed:
        if meta.no_worktree:
            prompt = f"Delete task '{meta.name}' (metadata only)? [y/N] "
        elif meta.worktree_path.exists():
            if git.has_uncommitted_changes(meta.worktree_path):
                ui.warn(f"Task '{meta.name}': there are uncommitted changes in the worktree.")
            prompt = f"Delete task '{meta.name}' (worktree + branch + metadata)? [y/N] "
        else:
            prompt = f"Delete task '{meta.name}' (branch + metadata)? [y/N] "
        answer = input(prompt).strip().lower()
        if answer != "y":
            ui.info("Aborted.")
            return

    sessions.delete(meta)
    _cleanup_task(meta.repo_path, meta.name)


def _meta_for_launch(
    repo: Path,
    worktree: Path,
    name: str,
    branch: str,
    no_worktree: bool,
    backend: agent.AgentBackend,
    is_chat: bool = False,
) -> sessions.SessionMeta:
    """PR2 test-seam shim — to be deleted in PR3 with the _launch_* wrappers.

    Synthesises a SessionMeta from the positional args that
    cli._launch_new/_resume/_finalize still accept. Production callers
    (cmd_new/cmd_chat/cmd_resume) already hold a full SessionMeta and
    only unpack to positional args because ~30 existing test patches in
    test_cli.py expect those wrapper signatures. Once PR3 rewrites
    those tests to drive sessions.launch(meta, ...) directly, both this
    helper and the wrappers go away.

    Drops status/created/completed/session_id/no_commit/include from
    the synthetic meta: sessions.launch reads ``meta.name``,
    ``meta.repo_path``, ``meta.worktree_path``, ``meta.branch``,
    ``meta.is_chat``, ``meta.no_worktree``, ``meta.session_dir`` — and
    ``session_id`` enters launch as a separate kwarg, not via meta.
    """
    return sessions.SessionMeta(
        name=name,
        repo=str(repo),
        worktree=str(worktree),
        branch=branch,
        type="chat" if is_chat else "task",
        no_worktree=no_worktree,
        agent=backend.kind,
    )


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
    include_repos: list[IncludeEntry] | None = None,
) -> None:
    meta = _meta_for_launch(repo, worktree, name, branch, no_worktree, backend)
    features = sessions.launch(
        meta, kind="finalize", backend=backend, runtime=runtime,
        main_branch=main_branch, session_id=session_id,
        include_repos=include_repos,
    )
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
    task_path = sessions.find_task_file(worktree, name)
    if task_path and task_path.exists():
        content = task_path.read_text()
        if sessions.is_task_complete(content):
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
        meta = sessions.load(repo, name)
        if not meta.session_id:
            ui.error("no session ID found. Cannot relaunch.")
            return
        backend = agent.from_kind(meta.agent)
        runtime = docker.resolve_runtime(repo, worktree, no_docker=not sandbox, backend=backend)
        main_branch = git.get_default_branch(repo)
        include_repos = load_include_entries({"include": meta.include})
        _launch_finalize(
            repo,
            worktree,
            name,
            meta.session_id,
            backend,
            runtime,
            meta.branch,
            main_branch,
            no_worktree,
            include_repos=include_repos,
        )
    elif choice == "x":
        _do_delete(sessions.load(repo, name))
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
    include_repos: list[IncludeEntry] | None = None,
) -> None:
    meta = _meta_for_launch(repo, worktree, name, branch, no_worktree, backend, is_chat=is_chat)
    features = sessions.launch(
        meta, kind="new", backend=backend, runtime=runtime,
        main_branch=main_branch, session_id=session_id,
        no_cache=no_cache, include_repos=include_repos,
    )
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
    include_repos: list[IncludeEntry] | None = None,
) -> None:
    meta = _meta_for_launch(repo, worktree, name, branch, no_worktree, backend, is_chat=is_chat)
    features = sessions.launch(
        meta, kind="resume", backend=backend, runtime=runtime,
        main_branch=main_branch, session_id=session_id,
        no_cache=no_cache, include_repos=include_repos,
    )
    if is_chat:
        _chat_post_exit(name, repo)
    else:
        _post_exit_check(
            name, repo, worktree, branch=branch, sandbox=bool(runtime), no_worktree=no_worktree, features=features
        )


def _chat_post_exit(name: str, repo: Path) -> None:
    """Simple post-exit for chat: offer to mark complete."""
    answer = input(f"\nMark chat '{name}' as complete? [Y/n] ").strip().lower()
    if answer != "n":
        meta = sessions.load(repo, name)
        meta.status = "complete"
        meta.completed = datetime.now().isoformat()
        sessions.save(meta)
        _cleanup_task(repo, name)
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
            all_tasks = sessions.repo_tasks_for_current_repo(repo)
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
    sessions.migrate_db()
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
    default=DEFAULT_BASE,
    metavar="REF",
    help=f"Branch or commit to fork from (default: {DEFAULT_BASE})",
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
@click.option(
    "--include",
    "include",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    metavar="PATH",
    help=(
        "Mount an additional directory inside the container at /includes/<basename>/. "
        "Git repos get a hatchery/<name> worktree for branch isolation (read-write). "
        "Repeatable; merged with docker.yaml 'include:' list."
    ),
)
@click.option(
    "--include-rw",
    "include_rw",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    metavar="PATH",
    help=(
        "Mount an additional directory read-write inside the container at /includes/<basename>/. "
        "No worktree is created — the directory is mounted as-is. "
        "Repeatable; merged with docker.yaml 'include:' list."
    ),
)
@click.option(
    "--include-ro",
    "include_ro",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    metavar="PATH",
    help=(
        "Mount an additional directory read-only inside the container at /includes/<basename>/. "
        "No worktree is created. "
        "Repeatable; merged with docker.yaml 'include:' list."
    ),
)
@click.option(
    "--no-commit",
    "no_commit",
    is_flag=True,
    help=(
        "Skip all automatic git commits made by hatchery "
        "(task file, Docker configuration, etc.). "
        "Files are still written to the worktree but never staged or committed."
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
    include: tuple[Path, ...],
    include_rw: tuple[Path, ...],
    include_ro: tuple[Path, ...],
    no_commit: bool,
) -> None:
    """Start a new task."""
    ui.hatchery_header(_version)
    repo, in_repo = git.git_root_or_cwd()
    if not in_repo:
        no_worktree = True
        ui.note("not in a git repository — running without worktree isolation.")

    cfg = user_config.UserConfig.load()
    backend = cfg.resolve_backend(agent_name)
    use_editor = editor if editor is not None else cfg.open_editor

    # Resolve --include paths: convert CLI tuples → entries, then sessions
    # merges them with docker.yaml's 'include:' list.
    early_config = docker.load_docker_config(repo)
    include_repos = sessions.merge_includes_with_config(
        _cli_includes_to_entries(include, include_rw, include_ro),
        early_config.include,
        repo,
    )

    name = sessions.to_name(name)
    objective = None if use_editor else _prompt_objective()
    try:
        meta = sessions.create(
            name=name, repo=repo, type="task", backend=backend,
            base=base, no_worktree=no_worktree,
            no_commit=no_commit, no_commit_docker=no_commit_docker,
            no_docker=no_docker, in_repo=in_repo,
            include_entries=include_repos, objective=objective, use_editor=use_editor,
        )
    except sessions.SessionCancelled:
        sys.exit(1)
    except KeyboardInterrupt:
        ui.warn("Cancelled.")
        sys.exit(1)

    runtime = docker.resolve_runtime(meta.repo_path, meta.worktree_path, no_docker, backend=backend)
    main_branch = git.get_default_branch(repo) if in_repo else ""
    include_repos = load_include_entries({"include": meta.include})
    try:
        _launch_new(
            meta.repo_path, meta.worktree_path, meta.name, meta.session_id or "",
            backend, runtime, meta.branch, main_branch, meta.no_worktree,
            no_cache=rebuild_sandbox, include_repos=include_repos,
        )
    except KeyboardInterrupt:
        if not meta.no_worktree:
            git.remove_worktree(repo, meta.worktree_path)
            git.delete_branch(repo, meta.branch)
        if include_repos:
            git.remove_include_worktrees(include_repos, meta.name)
            git.delete_include_branches(include_repos, meta.name)
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
@click.option(
    "--no-commit",
    "no_commit",
    is_flag=True,
    help="Skip all automatic git commits made by hatchery (Docker configuration, etc.).",
)
def cmd_chat(name: str | None, agent_name: str, no_commit: bool) -> None:
    """Start a free-form chat session in a sandbox."""
    ui.hatchery_header(_version)
    repo, in_repo = git.git_root_or_cwd()
    if not in_repo:
        ui.note("not in a git repository — running without worktree isolation.")

    cfg = user_config.UserConfig.load()
    backend = cfg.resolve_backend(agent_name)

    name = sessions.next_chat_name(repo) if name is None else sessions.to_name(name)

    try:
        meta = sessions.create(
            name=name, repo=repo, type="chat", backend=backend,
            no_commit=no_commit, in_repo=in_repo,
        )
    except KeyboardInterrupt:
        ui.warn("Cancelled.")
        sys.exit(1)

    runtime = docker.resolve_runtime(repo, repo, no_docker=False, backend=backend)
    main_branch = git.get_default_branch(repo) if in_repo else ""
    _launch_new(
        repo, repo, meta.name, meta.session_id or "",
        backend, runtime, "", main_branch, no_worktree=True, is_chat=True,
    )


@cli.command("resume")
@click.argument("name", type=TASK_NAME)
@click.option("--no-docker", is_flag=True, help="Run agent directly, even if a Dockerfile is present")
@click.option(
    "--rebuild-sandbox",
    "rebuild_sandbox",
    is_flag=True,
    help="Rebuild the sandbox image from scratch, ignoring the layer cache",
)
@click.option(
    "--include",
    "include",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    metavar="PATH",
    help=(
        "Add or update an include path in worktree mode. "
        "If the path is already included, its mode is updated. "
        "To change modes permanently, edit meta.json or re-run with the desired flag."
    ),
)
@click.option(
    "--include-rw",
    "include_rw",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    metavar="PATH",
    help="Add or update an include path as a read-write reference mount (no worktree).",
)
@click.option(
    "--include-ro",
    "include_ro",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    metavar="PATH",
    help="Add or update an include path as a read-only reference mount (no worktree).",
)
def cmd_resume(
    name: str,
    no_docker: bool,
    rebuild_sandbox: bool,
    include: tuple[Path, ...],
    include_rw: tuple[Path, ...],
    include_ro: tuple[Path, ...],
) -> None:
    """Resume exactly where you left off.

    Reviving a session whose status is ``complete`` or ``archived`` is
    supported on purpose — tasks sometimes get marked complete by accident,
    and archived tasks are explicitly designed to be brought back. In both
    cases the worktree (which was removed at mark-done / archive time) is
    re-created from the saved branch and status flips to ``in-progress``.
    """
    ui.hatchery_header(_version)
    repo, _ = git.git_root_or_cwd()
    meta = sessions.load(repo, name)
    repo = meta.repo_path

    backend = agent.from_kind(meta.agent)

    if not meta.no_worktree and not meta.worktree_path.exists():
        if meta.status in ("archived", "complete"):
            label = "archived" if meta.status == "archived" else "completed"
            ui.info(f"Re-creating worktree for {label} task '{name}'...")
            git.create_worktree(repo, meta.branch, meta.worktree_path, meta.branch)
            archived_includes = load_include_entries({"include": meta.include})
            if archived_includes:
                git.create_include_worktrees(archived_includes, name)
            meta.status = "in-progress"
            sessions.save(meta)
            ui.success(f"Worktree restored: {meta.worktree_path}")
        else:
            ui.error(f"worktree {meta.worktree_path} does not exist.")
            sys.exit(1)

    if not meta.session_id:
        ui.error("no session ID found for this task. Cannot resume.")
        sys.exit(1)

    if not backend.supports_sessions:
        ui.note(
            f"'{backend.kind}' does not support session resumption; "
            "starting a fresh session with the current task file as context."
        )

    if meta.status == "running":
        ui.note(f"task '{name}' was marked as running — a previous session may have exited unexpectedly.")

    # Restore Docker files if they were removed from the task branch (e.g. to
    # keep them out of a PR).  Generate in repo root if needed, then copy into
    # the worktree — neither location is committed.
    if not no_docker and not meta.no_worktree:
        agent_df = docker.dockerfile_path(meta.worktree_path, backend)
        if not agent_df.exists():
            ui.note(
                "Dockerfile missing from worktree — restoring from repo root "
                "(will not be committed to the task branch)."
            )
            docker.ensure_docker_files_uncommitted(repo, meta.worktree_path, backend)

    include_repos = load_include_entries({"include": meta.include})

    # Apply any --include* flags passed at resume time.
    updates = [
        *((IncludeEntry(path=p.resolve(), mode="worktree")) for p in include),
        *((IncludeEntry(path=p.resolve(), mode="rw")) for p in include_rw),
        *((IncludeEntry(path=p.resolve(), mode="ro")) for p in include_ro),
    ]
    include_repos = sessions.merge_include_updates(include_repos, updates, meta)

    runtime = docker.resolve_runtime(repo, meta.worktree_path, no_docker, backend=backend)
    main_branch = git.get_default_branch(repo)
    _launch_resume(
        repo,
        meta.worktree_path,
        name,
        meta.session_id,
        backend,
        runtime,
        meta.branch,
        main_branch,
        meta.no_worktree,
        is_chat=meta.is_chat,
        no_cache=rebuild_sandbox,
        include_repos=include_repos,
    )


@cli.command("sandbox")
@click.option("--shell", default="/bin/bash", help="Shell to launch (default: /bin/bash)")
@click.option(
    "--rebuild-sandbox",
    "rebuild_sandbox",
    is_flag=True,
    help="Rebuild the sandbox image from scratch, ignoring the layer cache",
)
@click.option(
    "--no-commit",
    "no_commit",
    is_flag=True,
    help="Skip all automatic git commits made by hatchery (Docker configuration, etc.).",
)
def cmd_sandbox(shell: str, rebuild_sandbox: bool, no_commit: bool) -> None:
    """Drop into an interactive shell inside the Docker sandbox."""
    repo, in_repo = git.git_root_or_cwd()
    cfg = user_config.UserConfig.load()
    backend = cfg.resolve_backend(None)
    sessions.ensure_tasks_dir(repo)
    df_created = docker.ensure_dockerfile(repo, backend)
    dc_created = docker.ensure_docker_config(repo)
    if in_repo and not no_commit and (df_created or dc_created):
        ui.info("  Committing...")
        run(
            ["git", "add", str(docker.dockerfile_path(repo, backend).relative_to(repo)), str(DOCKER_CONFIG)],
            cwd=repo,
        )
        run(
            ["git", "commit", "-m", "chore: add hatchery Docker configuration"],
            cwd=repo,
        )
    runtime = docker.detect_runtime()
    config = docker.load_docker_config(repo)
    features = docker.docker_features(config)
    ui.banner("sandbox", repo, sandbox=True, worktree=False, features=features)
    docker.launch_sandbox_shell(
        repo, backend, config, runtime,
        image_name=sessions.image_name(repo, "sandbox"),
        shell=shell, no_cache=rebuild_sandbox,
    )


@cli.command("exec")
@click.argument("name", type=TASK_NAME)
@click.option("--shell", default="/bin/bash", help="Shell to launch (default: /bin/bash)")
def cmd_exec(name: str, shell: str) -> None:
    """Exec an interactive shell into a running task's container."""
    repo, _ = git.git_root_or_cwd()
    runtime = docker.detect_runtime()
    docker.exec_task_shell(sessions.container_name(repo, name), runtime, shell=shell)


@cli.command("done")
@click.argument("names", nargs=-1, required=True, type=TASK_NAME)
def cmd_done(names: tuple[str, ...]) -> None:
    """Mark complete and remove worktree."""
    repo, _ = git.git_root_or_cwd()
    for name in names:
        meta = sessions.load(repo, name)
        _do_mark_done(meta.name, meta.repo_path, meta.worktree_path)


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
        meta = sessions.load(repo, name)
        commit_changes = False
        if not meta.no_worktree and meta.worktree_path.exists():
            if not meta.no_commit and git.has_uncommitted_changes(meta.worktree_path):
                ui.warn(f"Task '{name}': there are uncommitted changes in the worktree.")
                ui.info(git.uncommitted_changes_summary(meta.worktree_path))
                answer = input("Commit them as a checkpoint before archiving? [Y/n] ").strip().lower()
                commit_changes = answer != "n"
        sessions.archive(meta, commit_changes=commit_changes)


@cli.command("delete")
@click.argument("names", nargs=-1, required=True, type=TASK_NAME)
@click.option("-f", "--force", is_flag=True, help="Skip confirmation prompt.")
def cmd_delete(names: tuple[str, ...], force: bool) -> None:
    """Delete task, branch, and all metadata."""
    repo, _ = git.git_root_or_cwd()
    metas = [sessions.load(repo, n) for n in names]
    if len(metas) > 1:
        if not force:
            answer = input(f"Delete {len(metas)} tasks ({', '.join(names)})? [y/N] ").strip().lower()
            if answer != "y":
                ui.info("Aborted.")
                return
        for meta in metas:
            _do_delete(meta, confirmed=True)
    else:
        _do_delete(metas[0], confirmed=force)


@cli.command("list")
@click.option("-a", "--all", "show_all", is_flag=True, help="Show all tasks, including completed and aborted")
def cmd_list(show_all: bool) -> None:
    """List tasks for current repo."""
    repo, _ = git.git_root_or_cwd()
    task_list = sessions.repo_tasks_for_current_repo(repo)

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
    meta = sessions.load(repo, name)
    task_path = sessions.find_task_file(meta.worktree_path, name)

    click.echo(click.style("Name:     ", bold=True) + meta.name)
    click.echo(click.style("Type:     ", bold=True) + meta.type)
    click.echo(click.style("Status:   ", bold=True) + meta.status)
    click.echo(click.style("Agent:    ", bold=True) + meta.agent.lower())
    click.echo(click.style("Branch:   ", bold=True) + meta.branch)
    click.echo(click.style("Worktree: ", bold=True) + meta.worktree)
    click.echo(click.style("Created:  ", bold=True) + (meta.created or "unknown")[:16])
    if meta.completed:
        click.echo(click.style("Completed:", bold=True) + meta.completed[:16])
    # Session ID shown here for debugging, kept out of normal workflow
    click.echo(click.style("Session:  ", bold=True) + (meta.session_id or "none"))

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
    meta = sessions.load(repo, name)
    if not meta.worktree_path.exists():
        ui.error(f"Worktree not found: {meta.worktree_path}")
        sys.exit(1)
    shell = os.environ.get("SHELL", "bash")
    ui.note(f"Opening shell in {meta.worktree_path}  (exit with Ctrl-D or 'exit')")
    subprocess.run([shell], cwd=meta.worktree_path)


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
        open_for_editing(config_path)
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
