"""Shared fixtures for seekr-hatchery tests."""

import json
from pathlib import Path

import pytest

import seekr_hatchery.agents as agent
import seekr_hatchery.tasks as tasks
import seekr_hatchery.user_config as user_config

# ---------------------------------------------------------------------------
# SpyBackend — records every lifecycle call for assertion in test_cli.py
# ---------------------------------------------------------------------------


class SpyBackend(agent.AgentBackend):
    """Test double for AgentBackend.

    Implements all abstract methods as no-ops and records every call in
    ``self.calls`` as ``(method_name, *positional_args_and_kwargs_values)``.
    The abstract methods are declared ``@staticmethod`` on the base class;
    implementing them as regular instance methods here is intentional —
    Python's ABC machinery only checks for method presence, so binding
    ``self`` lets us accumulate the call log without shared mutable state.
    """

    kind = "SPY"
    binary = "spy"
    supports_sessions = True

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    # ── Command construction ───────────────────────────────────────────────

    def build_new_command(
        self,
        session_id: str,
        system_prompt: str,
        initial_prompt: str,
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        self.calls.append(("build_new_command", session_id, system_prompt, initial_prompt, docker, workdir))
        return ["spy-new"]

    def build_resume_command(
        self,
        session_id: str,
        system_prompt: str,
        initial_prompt: str = "",
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        self.calls.append(("build_resume_command", session_id, system_prompt, initial_prompt, docker, workdir))
        return ["spy-resume"]

    def build_finalize_command(
        self,
        session_id: str,
        system_prompt: str,
        wrap_up_prompt: str,
        *,
        docker: bool = False,
        workdir: str = "",
    ) -> list[str]:
        self.calls.append(("build_finalize_command", session_id, system_prompt, wrap_up_prompt, docker, workdir))
        return ["spy-finalize"]

    # ── Docker infrastructure ─────────────────────────────────────────────

    def make_header_mutator(self):
        def _mutate(headers):
            out = {k: v for k, v in headers.items() if k.lower() not in ("x-api-key", "authorization")}
            out["x-api-key"] = "spy-key"
            return out

        return _mutate

    def home_mounts(self, session_dir: Path | None) -> list[str]:
        return []

    def tmpfs_paths(self) -> list[str]:
        return []

    def proxy_kwargs(self) -> dict:
        return {}

    def container_env(self, proxy_token: str, proxy_port: int) -> dict[str, str]:
        return {}

    # ── Lifecycle hooks ───────────────────────────────────────────────────

    def on_new_task(self, session_dir: Path) -> None:
        self.calls.append(("on_new_task", session_dir))

    def on_before_launch(self, worktree: Path) -> None:
        self.calls.append(("on_before_launch", worktree))

    def on_before_container_start(
        self,
        session_dir: Path | None,
        proxy_token: str,
        workdir: str,
    ) -> None:
        self.calls.append(("on_before_container_start", session_dir, proxy_token, workdir))

    # ── Class-level constant properties ───────────────────────────────────

    @property
    def dockerfile_install(self) -> str:
        return ""


@pytest.fixture()
def spy_backend() -> SpyBackend:
    """Fresh SpyBackend instance with empty call log."""
    return SpyBackend()


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every home-directory reference to an isolated temp dir.

    Applied automatically to every test so that no test can accidentally
    read from or write to real dotfiles (~/.hatchery, ~/.codex, etc.).

    Patches:
      - pathlib.Path.home()       — Python's canonical home lookup
      - HOME env var              — covers os.path.expanduser, git, subprocesses
      - tasks.HATCHERY_DIR / TASKS_DB_DIR  — module-level constants (import-time)
      - user_config.UserConfig.CONFIG_PATH — class-level constant (import-time)
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(tasks, "HATCHERY_DIR", fake_home / ".hatchery")
    monkeypatch.setattr(tasks, "TASKS_DB_DIR", fake_home / ".hatchery" / "tasks")
    monkeypatch.setattr(user_config.UserConfig, "CONFIG_PATH", fake_home / ".hatchery" / "config.json")
    return fake_home


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--integration", action="store_true", default=False, help="run integration tests")


def pytest_runtest_setup(item: pytest.Item) -> None:
    if "integration" in item.keywords:
        if not item.config.getoption("--integration"):
            pytest.skip("pass --integration to run integration tests")


@pytest.fixture()
def fake_tasks_db(home: Path) -> Path:
    """Return TASKS_DB_DIR (already home-redirected by the autouse fixture), creating it eagerly."""
    db = home / ".hatchery" / "tasks"
    db.mkdir(parents=True, exist_ok=True)
    return db


@pytest.fixture()
def sample_meta(fake_tasks_db: Path) -> dict:
    """A valid task metadata dict (already saved to fake_tasks_db)."""
    meta = {
        "name": "my-task",
        "branch": "hatchery/my-task",
        "worktree": "/some/repo/.hatchery/worktrees/my-task",
        "repo": "/some/repo",
        "status": "in-progress",
        "created": "2026-01-15T10:00:00",
        "session_id": "abc-123",
        "schema_version": 1,
    }
    # Write to the unified dir path matching repo="/some/repo"
    task_dir = fake_tasks_db / tasks.repo_id(Path("/some/repo")) / "my-task"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "meta.json").write_text(json.dumps(meta))
    return meta


@pytest.fixture()
def fake_repo(tmp_path: Path) -> Path:
    """A temp directory with a .git subdirectory (no real git init needed for most tests)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo
