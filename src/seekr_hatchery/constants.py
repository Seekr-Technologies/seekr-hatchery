"""Cross-module constants shared by sessions, docker, git, and cli.

This is a deliberate leaf module: it imports nothing from the project, so
any other module can import from here without creating a cycle. Anything
that ends up referenced from more than one module belongs here; anything
referenced only from inside its home module should stay there (and be
prefixed ``_`` to mark it private).
"""

from pathlib import Path

# Per-user state lives under ~/.hatchery. Cross-module: docker writes the
# sandbox session dir under HATCHERY_DIR.
HATCHERY_DIR = Path.home() / ".hatchery"

# Default branch/commit new tasks fork from when the user doesn't pass --from.
# Read by cli.py to populate the click option default and help text.
DEFAULT_BASE = "HEAD"

# Per-repo worktree home (relative to the repo root). Used by git when
# creating per-task worktrees and by docker when mounting them.
WORKTREES_SUBDIR = Path(".hatchery") / "worktrees"

# Docker config file inside a repo (relative to repo root). Read by both
# cli.py and docker.py when staging Docker scaffolding commits.
DOCKER_CONFIG = Path(".hatchery") / "docker.yaml"

# Persistent out-of-tree store for repos that opt out of committing hatchery
# files. Lives under ~/.hatchery/repos/<repo-id>/ and holds docker/ and
# records/ subdirs. Used by sessions.py path helpers.
REPOS_SUBDIR = "repos"
