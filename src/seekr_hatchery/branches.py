"""Branch-domain values and policy for task worktrees."""

from dataclasses import dataclass
from pathlib import Path

DEFAULT_BRANCH_PREFIX = "hatchery/"


@dataclass(frozen=True)
class BranchName:
    """An exact Git branch identity."""

    value: str

    @property
    def ref_dir(self) -> Path:
        """Return the relative ``refs/heads`` directory containing this branch."""
        parent = Path(self.value).parent
        return Path("refs/heads") if parent == Path(".") else Path("refs/heads") / parent

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class BranchPrefix:
    """A validated literal prefix used to derive task branch identities."""

    value: str = DEFAULT_BRANCH_PREFIX

    def __post_init__(self) -> None:
        validate_branch_prefix(self.value)

    def task_branch(self, task_name: str) -> BranchName:
        """Return the exact branch identity for *task_name*."""
        return BranchName(f"{self.value}{task_name}")


def validate_branch_prefix(value: str) -> str:
    """Validate and return a literal prefix that produces safe task branches."""
    candidate = f"{value}task"
    if candidate.startswith(("/", "-")) or "//" in candidate:
        raise ValueError("branch_prefix must produce a relative Git branch name")
    if any(ord(char) < 32 or ord(char) == 127 or char.isspace() or char in "~^:?*[\\" for char in candidate):
        raise ValueError("branch_prefix contains characters that are invalid in Git branch names")
    if ".." in candidate or "@{" in candidate:
        raise ValueError("branch_prefix contains a sequence that is invalid in Git branch names")

    parts = candidate.split("/")
    if any(part in ("", ".", "..") or part.startswith(".") or part.endswith((".", ".lock")) for part in parts):
        raise ValueError("branch_prefix produces an invalid Git branch path component")
    return value
