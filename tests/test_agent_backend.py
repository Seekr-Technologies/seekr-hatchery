"""Unit tests for the AgentBackend base seam."""

from pathlib import Path

import seekr_hatchery.agents as agent
import seekr_hatchery.docker as docker
import seekr_hatchery.models as models
from seekr_hatchery.agents.agent_backend import AgentBackend


def test_default_container_migration_is_noop(tmp_path: Path) -> None:
    meta = models.SessionMeta(name="task", repo=str(tmp_path), worktree=str(tmp_path))
    before = sorted(tmp_path.rglob("*"))
    assert agent.CODEX.migrate_container_state(meta, runtime=docker.DockerRuntime()) is None
    assert sorted(tmp_path.rglob("*")) == before


class TestUpdateSeamDefault:
    def test_base_update_returns_none_for_unpinned_harness(self):
        """A backend that doesn't override update() reports 'no pin to bump'.

        This is the path claude takes — its CLI installs via install.sh with
        no version pin, so it inherits the base default.
        """
        assert AgentBackend.update(None, "anything") is None
