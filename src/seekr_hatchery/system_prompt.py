"""Global and repository-local Hatchery system prompts."""

from pathlib import Path

import seekr_hatchery.constants as constants
from seekr_hatchery.utils import open_for_editing

_PROMPT_FILENAME = "system-prompt.md"
_DEFAULT_PROMPT_PATH = Path(__file__).parent / "resources" / _PROMPT_FILENAME

_COMMIT_PREFIX = """\
You are working on a task. The task file is at `.hatchery/tasks/<date>-<name>/task.md`
(the exact path is in your session prompt). This single file serves as brief,
plan, progress log, and final notes — update it in place as you work.

"""

_NO_COMMIT_PREFIX = """\
You are working on a task. The task file is at the path given in your session
prompt. This single file serves as brief, plan, progress log, and final notes —
update it in place as you work.

"""

_COMMIT_SUFFIX = "   This file will be merged into main as the permanent record of this task.\n"

_NO_COMMIT_SUFFIX = """\
   This file is your local hatchery record (stored outside the repo, not
   committed) — future sessions can read sibling records in the same directory.
"""


def global_path() -> Path:
    """Return the user-wide system-prompt path."""
    return constants.HATCHERY_DIR / _PROMPT_FILENAME


def local_path(repo: Path) -> Path:
    """Return the repository-local system-prompt path."""
    return repo / ".hatchery" / _PROMPT_FILENAME


def default_text() -> str:
    """Return the packaged default system prompt."""
    return _DEFAULT_PROMPT_PATH.read_text()


def seed(path: Path, content: str | None = None) -> bool:
    """Create *path* when absent and return whether it was created."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(default_text() if content is None else content)
    return True


def reset(path: Path) -> None:
    """Overwrite *path* with the current packaged default prompt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(default_text())


def load(repo: Path) -> str:
    """Load the effective prompt, preferring a repository-local override."""
    global_ = global_path()
    seed(global_)
    local = local_path(repo)
    if local.exists():
        return local.read_text()
    return global_.read_text()


def render(repo: Path, *, no_commit: bool) -> str:
    """Render the effective prompt with the original mode-specific framing."""
    prefix = _NO_COMMIT_PREFIX if no_commit else _COMMIT_PREFIX
    suffix = _NO_COMMIT_SUFFIX if no_commit else _COMMIT_SUFFIX
    return prefix + load(repo).rstrip() + "\n" + suffix


def edit(repo: Path, *, global_: bool) -> Path:
    """Seed when needed, open the selected prompt in $EDITOR, and return its path."""
    path = global_path() if global_ else local_path(repo)
    if global_:
        seed(path)
    else:
        seed(path, load(repo))
    open_for_editing(path)
    return path
