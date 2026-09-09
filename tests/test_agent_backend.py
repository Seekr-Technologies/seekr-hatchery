"""Unit tests for the AgentBackend base seam."""

from seekr_hatchery.agents.agent_backend import AgentBackend


class TestUpdateSeamDefault:
    def test_base_update_returns_none_for_unpinned_harness(self):
        """A backend that doesn't override update() reports 'no pin to bump'.

        This is the path claude takes — its CLI installs via install.sh with
        no version pin, so it inherits the base default.
        """
        assert AgentBackend.update(None, "anything") is None
