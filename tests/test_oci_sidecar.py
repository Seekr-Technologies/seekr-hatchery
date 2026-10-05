"""Contracts for the OCI Object Storage sandbox sidecar and host proxy."""

import pytest
from pydantic import ValidationError

from seekr_hatchery.sidecars import SidecarConfig
from seekr_hatchery.sidecars.oci_sidecar import OciConfig


class TestOciConfig:
    def test_sidecar_config_parses_profile_and_infers_default(self) -> None:
        config = SidecarConfig(
            oci={
                "profiles": {
                    "DEFAULT": {
                        "rules": [
                            {
                                "path": "oci://my-namespace/project-artifacts/team-a/",
                                "permissions": ["READ", "LIST", "WRITE"],
                            }
                        ]
                    }
                }
            }
        )
        assert config.oci == OciConfig(
            profiles={
                "DEFAULT": {
                    "rules": [
                        {
                            "path": "oci://my-namespace/project-artifacts/team-a/",
                            "permissions": ["READ", "LIST", "WRITE"],
                        }
                    ]
                }
            },
            default="DEFAULT",
        )

    def test_requires_default_for_multiple_profiles(self) -> None:
        with pytest.raises(ValidationError, match="default is required"):
            OciConfig(profiles={"DEFAULT": {}, "PRODUCTION": {}})

    def test_rejects_unknown_default_profile(self) -> None:
        with pytest.raises(ValidationError, match="not present"):
            OciConfig(default="MISSING", profiles={"DEFAULT": {}})

    def test_rejects_invalid_profile_name(self) -> None:
        with pytest.raises(ValidationError, match="invalid profile"):
            OciConfig(profiles={"not a profile": {}})

    @pytest.mark.parametrize(
        "path",
        [
            "https://namespace/bucket/prefix",
            "oci://namespace",
            "oci:///bucket/prefix",
            "oci://namespace/bucket?version=1",
            "oci://namespace/bucket#fragment",
            "oci://user@namespace/bucket",
            "oci://namespace:443/bucket",
        ],
    )
    def test_rejects_invalid_or_ambiguous_rule_paths(self, path: str) -> None:
        with pytest.raises(ValidationError):
            OciConfig(
                profiles={
                    "DEFAULT": {
                        "rules": [{"path": path, "permissions": ["READ"]}],
                    }
                }
            )

    def test_rule_path_is_literal_and_decoded(self) -> None:
        config = OciConfig(
            profiles={
                "DEFAULT": {
                    "rules": [
                        {
                            "path": "oci://my-namespace/project-artifacts/some%20prefix/",
                            "permissions": ["READ"],
                        }
                    ]
                }
            }
        )
        assert config.profiles["DEFAULT"].rules is not None
        rule = config.profiles["DEFAULT"].rules[0]
        assert (rule.namespace, rule.bucket, rule.prefix) == (
            "my-namespace",
            "project-artifacts",
            "some prefix/",
        )
