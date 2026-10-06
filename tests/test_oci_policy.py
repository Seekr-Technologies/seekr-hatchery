"""OCI Object Storage path-policy contracts."""

from __future__ import annotations

import pytest

from seekr_hatchery.sidecars.oci_sidecar import OciConfig, policy


class TestOciPolicy:
    @pytest.mark.parametrize(
        ("method", "target", "expected"),
        [
            (
                "GET",
                "/n/my-ns/b/artifacts/o/team-a/file.txt",
                [policy.PolicyTarget("READ", "my-ns", "artifacts", "team-a/file.txt")],
            ),
            (
                "PUT",
                "/n/my-ns/b/artifacts/o/team-a/file.txt",
                [policy.PolicyTarget("WRITE", "my-ns", "artifacts", "team-a/file.txt")],
            ),
            (
                "DELETE",
                "/n/my-ns/b/artifacts/o/team-a/file.txt",
                [policy.PolicyTarget("DELETE", "my-ns", "artifacts", "team-a/file.txt")],
            ),
            (
                "GET",
                "/n/my-ns/b/artifacts/o?prefix=team-a%2F",
                [policy.PolicyTarget("LIST", "my-ns", "artifacts", "team-a/")],
            ),
            (
                "PUT",
                "/n/my-ns/b/artifacts/u/team-a/file.txt?uploadId=one&uploadPartNum=1",
                [policy.PolicyTarget("WRITE", "my-ns", "artifacts", "team-a/file.txt")],
            ),
        ],
    )
    def test_classifies_supported_requests(
        self,
        method: str,
        target: str,
        expected: list[policy.PolicyTarget],
    ) -> None:
        assert policy.policy_targets(method, target) == expected

    @pytest.mark.parametrize(
        ("method", "target"),
        [
            ("GET", "/n/"),
            ("GET", "/n/my-ns/b"),
            ("POST", "/n/my-ns/b/artifacts/actions/copyObject"),
            ("POST", "/n/my-ns/b/artifacts/o"),
            ("PATCH", "/n/my-ns/b/artifacts/o/object"),
        ],
    )
    def test_unknown_or_administrative_requests_are_ambiguous(self, method: str, target: str) -> None:
        assert policy.policy_targets(method, target) is None

    def test_rules_require_matching_namespace_bucket_prefix_and_permission(self) -> None:
        config = OciConfig(
            profiles={
                "DEFAULT": {
                    "rules": [
                        {
                            "path": "oci://my-ns/artifacts/team-a/",
                            "permissions": ["READ", "LIST", "WRITE"],
                        }
                    ]
                }
            }
        )
        rules = config.profiles["DEFAULT"].rules
        assert rules is not None
        assert policy.rules_allow(
            rules,
            [policy.PolicyTarget("READ", "my-ns", "artifacts", "team-a/file")],
        )
        assert not policy.rules_allow(
            rules,
            [policy.PolicyTarget("READ", "other-ns", "artifacts", "team-a/file")],
        )
        assert not policy.rules_allow(
            rules,
            [policy.PolicyTarget("DELETE", "my-ns", "artifacts", "team-a/file")],
        )
        assert not policy.rules_allow([], [policy.PolicyTarget("READ", "my-ns", "artifacts", "team-a/file")])
        assert policy.rules_allow(None, None)
