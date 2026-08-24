"""Tests for pydantic models in seekr_hatchery.models."""

from __future__ import annotations

import logging

import pytest

from seekr_hatchery.models import KubectlConfig, KubectlRBACRule


class TestKubectlConfig:
    def test_default_empty_rules(self) -> None:
        cfg = KubectlConfig()
        assert cfg.rules == []

    def test_default_context_is_none(self) -> None:
        cfg = KubectlConfig()
        assert cfg.context is None

    def test_context_field(self) -> None:
        cfg = KubectlConfig(context="my-dev-cluster", rules=[])
        assert cfg.context == "my-dev-cluster"

    def test_parse_from_dict(self) -> None:
        cfg = KubectlConfig(
            rules=[
                {"verbs": ["get", "list"], "resources": ["pods"], "namespaces": ["default"]},
            ]
        )
        assert len(cfg.rules) == 1
        assert cfg.rules[0].verbs == ["get", "list"]

    def test_default_namespaces_is_wildcard(self) -> None:
        rule = KubectlRBACRule(verbs=["get"], resources=["pods"])
        assert rule.namespaces == ["*"]

    def test_unknown_verb_emits_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """'describe' and other client-side commands are not real k8s verbs."""
        with caplog.at_level(logging.WARNING, logger="hatchery"):
            KubectlRBACRule(verbs=["get", "describe"], resources=["pods"])
        assert any("describe" in r.message for r in caplog.records)

    def test_known_verbs_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="hatchery"):
            KubectlRBACRule(verbs=["get", "list", "watch", "create", "update", "patch", "delete"], resources=["pods"])
        assert not any("unrecognized" in r.message for r in caplog.records)
