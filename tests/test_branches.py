"""Tests for branch-domain values and task branch policy."""

from pathlib import Path

import pytest

import seekr_hatchery.branches as branches


class TestBranchName:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("hatchery/task", Path("refs/heads/hatchery")),
            ("team/agents/task", Path("refs/heads/team/agents")),
            ("custom-branch", Path("refs/heads")),
        ],
    )
    def test_ref_dir(self, value, expected):
        assert branches.BranchName(value).ref_dir == expected

    def test_string_value(self):
        assert str(branches.BranchName("agents/task")) == "agents/task"


class TestBranchPrefix:
    def test_default_task_branch(self):
        prefix = branches.BranchPrefix()
        assert prefix.task_branch("my-task") == branches.BranchName("hatchery/my-task")

    @pytest.mark.parametrize(
        ("prefix", "expected"),
        [
            ("team/agents/", "team/agents/my-task"),
            ("agents-", "agents-my-task"),
            ("agents", "agentsmy-task"),
            ("", "my-task"),
        ],
    )
    def test_literal_custom_task_branch(self, prefix, expected):
        assert branches.BranchPrefix(prefix).task_branch("my-task") == branches.BranchName(expected)

    @pytest.mark.parametrize(
        "value",
        ["/agents/", "-agents-", "agents//nested/", "../agents/", "agents/.hidden/", "agents branch/"],
    )
    def test_rejects_invalid_prefix(self, value):
        with pytest.raises(ValueError, match="branch_prefix"):
            branches.BranchPrefix(value)
