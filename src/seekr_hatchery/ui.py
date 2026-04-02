"""User-facing output helpers — all CLI output goes through this module."""

import logging
import sys
from itertools import zip_longest

import click

_BANNER_MIN_INNER = 47  # minimum content width between border characters

# ── ASCII art constants (swap either independently) ───────────────────────────

_EGG = """\
 ▗▟▙▖
▗████▖
██████
██████
▝▜██▛▘"""

_HATCHERY_TEXT = """\
▗▖ ▗▖ ▗▄▖▗▄▄▄▖▗▄▄▖▗▖ ▗▖▗▄▄▄▖▗▄▄▖▗▖  ▗▖
▐▌ ▐▌▐▌ ▐▌ █ ▐▌   ▐▌ ▐▌▐▌   ▐▌ ▐▌▝▚▞▘
▐▛▀▜▌▐▛▀▜▌ █ ▐▌   ▐▛▀▜▌▐▛▀▀▘▐▛▀▚▖ ▐▌
▐▌ ▐▌▐▌ ▐▌ █ ▝▚▄▄▖▐▌ ▐▌▐▙▄▄▖▐▌ ▐▌ ▐▌"""


def hatchery_header(version: str) -> None:
    """Print the ASCII art header: egg on the left, hatchery text + version on the right."""
    egg_lines = _EGG.splitlines()
    text_lines = _HATCHERY_TEXT.splitlines()
    version_str = click.style(f"v{version}", fg="bright_black")
    egg_w = max(len(ln) for ln in egg_lines)
    for i, (egg_ln, text_ln) in enumerate(zip_longest(egg_lines, text_lines, fillvalue="")):
        colored_egg = click.style(egg_ln.ljust(egg_w), fg="yellow")
        # Last egg line gets the version; remaining text lines get cyan
        if i < len(text_lines):
            right = click.style(text_ln, fg="red")
        else:
            right = version_str
        click.echo(colored_egg + "  " + right)


def error(msg: str) -> None:
    """Print a red bold 'Error: {msg}' to stderr."""
    click.echo(click.style(f"Error: {msg}", fg="red", bold=True), err=True)


def warn(msg: str) -> None:
    """Print a yellow 'Warning: {msg}' to stdout."""
    click.echo(click.style(f"Warning: {msg}", fg="yellow"))


def note(msg: str) -> None:
    """Print a cyan 'Note: {msg}' to stdout."""
    click.echo(click.style(f"Note: {msg}", fg="cyan"))


def success(msg: str) -> None:
    """Print a green message to stdout."""
    click.echo(click.style(msg, fg="green"))


def info(msg: str) -> None:
    """Print a plain message to stdout."""
    click.echo(msg)


def _pad_line(s: str, width: int) -> str:
    """Pad `s` to `width` visible characters (ANSI escape codes excluded from width)."""
    return s + " " * (width - len(click.unstyle(s)))


def _banner_box(lines: list[str], inner: int, label: str, double: bool, color: str) -> None:
    """Render a labeled border box around content lines."""
    if double:
        h, v, tl, tr, bl, br = "═", "║", "╔", "╗", "╚", "╝"
        prefix = "══"
    else:
        h, v, tl, tr, bl, br = "─", "│", "┌", "┐", "└", "┘"
        prefix = "──"
    label_styled = click.style(label, fg=color, bold=True)
    fill = inner - len(prefix) - len(label)
    top = click.style(tl + prefix, fg=color) + label_styled + click.style(h * fill + tr, fg=color)
    bot = click.style(bl + h * inner + br, fg=color)
    lborder = click.style(v, fg=color)
    rborder = click.style(v, fg=color)
    click.echo(top)
    for line in lines:
        click.echo(lborder + _pad_line(line, inner) + rborder)
    click.echo(bot)


def banner(
    name: str,
    repo: object,
    branch: str = "",
    sandbox: bool = False,
    worktree: bool = True,
    features: list[str] | None = None,
) -> None:
    """Print the launch frame banner with isolation-level visual encoding.

    Three styles encode the active security features:
      sandbox=True   → double-line box (cyan)   strongest containment
      worktree=True  → single-line box (green)  git isolation
      both False     → flat ━━━ bar             no isolation
    """
    lines: list[str] = [f"  Task:    {name}"]
    if branch:
        lines.append(f"  Branch:  {click.style(branch, fg='green')}")
    lines.append(f"  {'Repo' if worktree else 'Dir'}:     {repo}")
    if features:
        features_str = "  ".join(click.style(f, fg="cyan", bold=True) for f in features)
        lines.append(f"  Features: {features_str}")
    inner = max(max(len(click.unstyle(ln)) for ln in lines) + 2, _BANNER_MIN_INNER)
    if sandbox:
        _banner_box(lines, inner, label=" sandbox ", double=True, color="cyan")
    elif worktree:
        _banner_box(lines, inner, label=" worktree ", double=False, color="green")
    else:
        bar = click.style("━" * (inner + 2), bold=True)
        click.echo(bar)
        for line in lines:
            click.echo(line)
        click.echo(bar)


def chat_banner(
    name: str,
    repo: object,
    features: list[str] | None = None,
) -> None:
    """Print the launch frame banner for a chat session (always sandboxed)."""
    lines: list[str] = [
        f"  Chat:    {name}",
        f"  Dir:     {repo}",
    ]
    if features:
        features_str = "  ".join(click.style(f, fg="cyan", bold=True) for f in features)
        lines.append(f"  Features: {features_str}")
    inner = max(max(len(click.unstyle(ln)) for ln in lines) + 2, _BANNER_MIN_INNER)
    _banner_box(lines, inner, label=" sandbox ", double=True, color="cyan")


def task_list_table(task_list: list[dict], archived_count: int, show_all: bool) -> None:
    """Render a colored task list table to stdout."""
    if not task_list:
        if show_all:
            info("No tasks found for this repository.")
        else:
            info("No active tasks found. Use --all to see all tasks.")
        if archived_count:
            info(f"+ {archived_count} archived task{'s' if archived_count != 1 else ''}.")
        return

    name_w = max(len(t["name"]) for t in task_list)
    st_w = max(len(t["status"]) for t in task_list)
    header = f"{'NAME':<{name_w}}  {'STATUS':<{st_w}}  CREATED"
    click.echo(click.style(header, bold=True))
    click.echo("-" * (len(header) + 10))

    for t in task_list:
        created = t.get("created", "")[:10]
        status = t["status"]
        match status:
            case "attached":
                status_str = click.style(f"{status:<{st_w}}", fg="cyan")
            case "background":
                status_str = click.style(f"{status:<{st_w}}", fg="yellow")
            case "paused":
                status_str = click.style(f"{status:<{st_w}}", fg="green")
            case "archived":
                status_str = click.style(f"{status:<{st_w}}", dim=True)
            case "complete":
                status_str = click.style(f"{status:<{st_w}}", dim=True)
            case _:
                status_str = f"{status:<{st_w}}"
        click.echo(f"{t['name']:<{name_w}}  {status_str}  {created}")

    if archived_count:
        click.echo(
            click.style(
                f"\n+ {archived_count} archived task{'s' if archived_count != 1 else ''}. Use --all to see them.",
                dim=True,
            )
        )


_LEVEL_COLORS: dict[str, dict[str, object]] = {
    "DEBUG": {"fg": "blue"},
    "INFO": {"fg": "cyan"},
    "WARNING": {"fg": "yellow", "bold": True},
    "ERROR": {"fg": "red", "bold": True},
    "CRITICAL": {"fg": "red", "bold": True},
}


class ColorFormatter(logging.Formatter):
    """Colorizes the levelname when stderr is a TTY."""

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        if not sys.stderr.isatty():
            return formatted
        style = _LEVEL_COLORS.get(record.levelname, {})
        if style:
            # Replace just the levelname portion with a colored version.
            colored_level = click.style(record.levelname, **style)  # type: ignore[arg-type]
            return formatted.replace(record.levelname, colored_level, 1)
        return formatted
