from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

import pytest

import seekr_hatchery.agents as agent
import seekr_hatchery.agents as agents
import seekr_hatchery.branches as branches
import seekr_hatchery.constants as constants
import seekr_hatchery.sandbox.config as sandbox_config
import seekr_hatchery.sandbox.scaffold as sandbox_scaffold
import seekr_hatchery.sandbox.scaffold as scaffold
from seekr_hatchery.models import SessionMeta


class OfflineCodexBackend(agents.CodexBackend):
    """Skip the registry boundary while exercising real scaffold rendering."""

    def update(self, dockerfile_text: str) -> tuple[str, str | None, str] | None:
        return None


def test_scaffold_resources(tmp_path: Path) -> None:
    tmp_path = tmp_path / "scaffold"
    backend = OfflineCodexBackend()
    resources = files("seekr_hatchery").joinpath("resources")
    assert scaffold.ensure_config(tmp_path, prompt=False)
    assert scaffold.ensure_local_config(tmp_path)
    assert scaffold.ensure_dockerfile(tmp_path, backend, prompt=False)
    expected_dockerfile = (
        resources.joinpath("Dockerfile.template")
        .read_text()
        .replace("{{AGENT_INSTALL}}", backend.dockerfile_install)
        .replace("{{DIND}}", "\n".join(f"# {line}" for line in scaffold.DIND_DOCKERFILE_LINES.splitlines()))
    )
    assert {path.name: path.read_text() for path in tmp_path.iterdir()} == {
        "docker.yaml": resources.joinpath("docker.yaml.template").read_text(),
        "docker.local.yaml": resources.joinpath("docker.local.yaml.template").read_text(),
        "Dockerfile.codex": expected_dockerfile,
    }
    assert not scaffold.ensure_config(tmp_path, prompt=False)
    assert not scaffold.ensure_local_config(tmp_path)
    assert not scaffold.ensure_dockerfile(tmp_path, backend, prompt=False)


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestEnsureHelpersNoPrompt:
    def test_ensure_docker_config_writes_valid_template(self, tmp_path):
        created = sandbox_scaffold.ensure_config(tmp_path, prompt=False)
        path = tmp_path / constants.DOCKER_CONFIG
        assert created is True
        assert path.exists()
        assert sandbox_config.validate_config_file(path) is None

    def test_ensure_dockerfile_writes_populated_template(self, tmp_path):
        created = sandbox_scaffold.ensure_dockerfile(tmp_path, agent.CODEX, prompt=False)
        path = tmp_path / "Dockerfile.codex"
        assert created is True
        assert path.exists()
        assert "{{AGENT_INSTALL}}" not in path.read_text()


def _task_branch(name: str = "my-task", prefix: str = branches.DEFAULT_BRANCH_PREFIX) -> branches.BranchName:
    return branches.BranchPrefix(prefix).task_branch(name)


def _no_wt_meta(cwd):
    """Synthetic SessionMeta for no-worktree mount tests."""
    return SessionMeta(name="-", repo=str(cwd), worktree=str(cwd), no_worktree=True)


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestEnsureDockerfileGenerate:
    def test_generates_when_parent_dir_missing(self, tmp_path, monkeypatch):
        """``ensure_dockerfile(target)`` must create target/.hatchery/
        before writing the Dockerfile, even if .hatchery/ doesn't exist yet.
        """
        target = tmp_path / "target"
        target.mkdir()  # target exists but has NO .hatchery/ subdir

        monkeypatch.setattr("builtins.input", lambda _: "n")

        created = sandbox_scaffold.ensure_dockerfile(target, agent.CODEX)

        assert created is True
        assert (target / "Dockerfile.codex").exists()

    def test_returns_false_when_already_exists(self, tmp_path, monkeypatch):
        target = tmp_path / "target"
        target.mkdir()
        (target / "Dockerfile.codex").write_text("FROM debian\n")

        monkeypatch.setattr("builtins.input", lambda _: "n")

        created = sandbox_scaffold.ensure_dockerfile(target, agent.CODEX)
        assert created is False


# ---------------------------------------------------------------------------
# find_task_file
# ---------------------------------------------------------------------------


class TestDockerfilePath:
    def test_returns_agent_specific_path(self):
        repo = Path("/some/repo")
        assert sandbox_scaffold.dockerfile_path(repo, agent.CODEX) == Path("/some/repo/Dockerfile.codex")


# ---------------------------------------------------------------------------
# write_task_file
# ---------------------------------------------------------------------------


class TestDindDockerfileOk:
    def _prep(self, repo: Path) -> None:
        (repo / ".hatchery").mkdir(exist_ok=True)

    def test_commented_section_returns_false(self, fake_repo, monkeypatch):
        """Default ensure_dockerfile() generates DinD commented out → False."""
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        sandbox_scaffold.ensure_dockerfile(fake_repo, agent.CODEX)
        assert sandbox_scaffold.supports_dind(fake_repo, agent.CODEX) is False

    def test_uncommented_section_returns_true(self, fake_repo, monkeypatch):
        """Generate Dockerfile, then uncomment DinD lines → True."""
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        sandbox_scaffold.ensure_dockerfile(fake_repo, agent.CODEX)
        df = sandbox_scaffold.dockerfile_path(fake_repo, agent.CODEX)
        text = df.read_text()
        # Replace commented DinD block with the uncommented production constant
        text = text.replace(
            "\n".join(f"# {line}" for line in sandbox_scaffold.DIND_DOCKERFILE_LINES.splitlines()),
            sandbox_scaffold.DIND_DOCKERFILE_LINES,
        )
        df.write_text(text)
        assert sandbox_scaffold.supports_dind(fake_repo, agent.CODEX) is True

    def test_missing_dockerfile_returns_false(self, fake_repo):
        """No Dockerfile on disk → False."""
        assert sandbox_scaffold.supports_dind(fake_repo, agent.CODEX) is False


# ---------------------------------------------------------------------------
# write_task_file
# ---------------------------------------------------------------------------


class TestEnsureDockerConfig:
    def _prep(self, repo: Path) -> None:
        """Create .hatchery/ — normally done by ensure_tasks_dir before this runs."""
        (repo / ".hatchery").mkdir(exist_ok=True)

    def test_creates_config_file(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        sandbox_scaffold.ensure_config(fake_repo)
        assert (fake_repo / constants.DOCKER_CONFIG).exists()

    def test_returns_true_when_created(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        assert sandbox_scaffold.ensure_config(fake_repo) is True

    def test_returns_false_when_already_exists(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        (fake_repo / constants.DOCKER_CONFIG).write_text("existing")
        monkeypatch.setattr("builtins.input", lambda _: "n")
        assert sandbox_scaffold.ensure_config(fake_repo) is False

    def test_skips_if_already_exists(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        config = fake_repo / constants.DOCKER_CONFIG
        config.write_text("existing content")
        monkeypatch.setattr("builtins.input", lambda _: "n")
        with patch("seekr_hatchery.sandbox.scaffold.utils.open_for_editing") as mock_edit:
            sandbox_scaffold.ensure_config(fake_repo)
        assert config.read_text() == "existing content"
        mock_edit.assert_not_called()

    def test_yes_opens_editor(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        with patch("seekr_hatchery.sandbox.scaffold.utils.open_for_editing") as mock_edit:
            sandbox_scaffold.ensure_config(fake_repo)
        mock_edit.assert_called_once()

    def test_enter_opens_editor(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "")  # pressing Enter = default yes
        with patch("seekr_hatchery.sandbox.scaffold.utils.open_for_editing") as mock_edit:
            sandbox_scaffold.ensure_config(fake_repo)
        mock_edit.assert_called_once()

    def test_no_skips_editor(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        with patch("seekr_hatchery.sandbox.scaffold.utils.open_for_editing") as mock_edit:
            sandbox_scaffold.ensure_config(fake_repo)
        mock_edit.assert_not_called()

    def test_does_not_commit(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        with patch("seekr_hatchery.utils.run") as mock_run:
            sandbox_scaffold.ensure_config(fake_repo)
        mock_run.assert_not_called()

    def test_created_file_is_valid_yaml(self, fake_repo, monkeypatch):
        import yaml

        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        sandbox_scaffold.ensure_config(fake_repo)
        content = (fake_repo / constants.DOCKER_CONFIG).read_text()
        parsed = yaml.safe_load(content)
        assert parsed["schema_version"] == "1"


# ---------------------------------------------------------------------------
# write_task_file
# ---------------------------------------------------------------------------


class TestEnsureDockerfile:
    def _prep(self, repo: Path) -> None:
        (repo / ".hatchery").mkdir(exist_ok=True)

    @pytest.fixture(autouse=True)
    def _stub_npm(self, monkeypatch):
        """Keep generation offline: resolve every harness to a canned version."""
        monkeypatch.setattr("seekr_hatchery.utils.npm.npm_latest_version", lambda _pkg: "1.2.3")

    def test_creates_agent_specific_dockerfile(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        sandbox_scaffold.ensure_dockerfile(fake_repo, agent.CODEX)
        assert sandbox_scaffold.dockerfile_path(fake_repo, agent.CODEX).exists()

    def test_returns_true_when_created(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        assert sandbox_scaffold.ensure_dockerfile(fake_repo, agent.CODEX) is True

    def test_returns_false_when_already_exists(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        sandbox_scaffold.dockerfile_path(fake_repo, agent.CODEX).write_text("FROM debian\n")
        monkeypatch.setattr("builtins.input", lambda _: "n")
        assert sandbox_scaffold.ensure_dockerfile(fake_repo, agent.CODEX) is False

    def test_skips_if_already_exists(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        df = sandbox_scaffold.dockerfile_path(fake_repo, agent.CODEX)
        df.write_text("existing content")
        monkeypatch.setattr("builtins.input", lambda _: "n")
        with patch("seekr_hatchery.sandbox.scaffold.utils.open_for_editing") as mock_edit:
            sandbox_scaffold.ensure_dockerfile(fake_repo, agent.CODEX)
        assert df.read_text() == "existing content"
        mock_edit.assert_not_called()

    def test_yes_opens_editor(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        with patch("seekr_hatchery.sandbox.scaffold.utils.open_for_editing") as mock_edit:
            sandbox_scaffold.ensure_dockerfile(fake_repo)
        mock_edit.assert_called_once()

    def test_enter_opens_editor(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "")  # pressing Enter = default yes
        with patch("seekr_hatchery.sandbox.scaffold.utils.open_for_editing") as mock_edit:
            sandbox_scaffold.ensure_dockerfile(fake_repo)
        mock_edit.assert_called_once()

    def test_no_skips_editor(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        with patch("seekr_hatchery.sandbox.scaffold.utils.open_for_editing") as mock_edit:
            sandbox_scaffold.ensure_dockerfile(fake_repo)
        mock_edit.assert_not_called()

    def test_does_not_commit(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        with patch("seekr_hatchery.utils.run") as mock_run:
            sandbox_scaffold.ensure_dockerfile(fake_repo)
        mock_run.assert_not_called()

    def test_placeholders_resolved(self, fake_repo, monkeypatch):
        """Rendered Dockerfile contains no raw placeholders and includes the
        DinD lines in commented-out form."""
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        sandbox_scaffold.ensure_dockerfile(fake_repo, agent.CODEX)
        content = sandbox_scaffold.dockerfile_path(fake_repo, agent.CODEX).read_text()
        assert "{{AGENT_INSTALL}}" not in content
        assert "{{DIND}}" not in content
        assert "debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad" in content
        assert "# USER root" in content
        assert "fuse-overlayfs" in content
        assert (
            "ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff"
            in content
        )

    def test_pins_harness_to_latest_at_generation(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        sandbox_scaffold.ensure_dockerfile(fake_repo, agent.CODEX)
        content = sandbox_scaffold.dockerfile_path(fake_repo, agent.CODEX).read_text()
        assert "@openai/codex@1.2.3" in content

    def test_leaves_harness_unpinned_when_lookup_fails(self, fake_repo, monkeypatch):
        self._prep(fake_repo)
        monkeypatch.setattr("builtins.input", lambda _: "n")

        def boom(_pkg):
            raise OSError("offline")

        monkeypatch.setattr("seekr_hatchery.utils.npm.npm_latest_version", boom)
        sandbox_scaffold.ensure_dockerfile(fake_repo, agent.CODEX)
        content = sandbox_scaffold.dockerfile_path(fake_repo, agent.CODEX).read_text()
        assert "@openai/codex@" not in content
        assert "@openai/codex" in content
