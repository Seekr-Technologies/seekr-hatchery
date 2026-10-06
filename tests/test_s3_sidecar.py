"""S3 sidecar configuration and lifecycle contracts."""

from pathlib import Path

import pytest
from botocore.credentials import Credentials
from pydantic import ValidationError

from seekr_hatchery.agents import CONTAINER_HOME
from seekr_hatchery.mount import BindMount
from seekr_hatchery.sidecars import SidecarConfig, base
from seekr_hatchery.sidecars.s3_sidecar import S3Config, S3Sidecar, credentials, proxy


def _profile() -> credentials.ResolvedAwsProfile:
    return credentials.ResolvedAwsProfile(
        Credentials("AKIAHOSTCREDENTIAL", "host-secret", "host-session-token"),
        "us-west-2",
        "https://s3.us-west-2.amazonaws.com",
    )


class TestS3Config:
    def test_sidecar_config_parses_profile_and_infers_default(self) -> None:
        config = SidecarConfig(
            s3={
                "profiles": {
                    "development": {
                        "region": "us-west-2",
                        "rules": [
                            {
                                "path": "s3://project-artifacts/team-a/",
                                "permissions": ["READ", "LIST", "WRITE"],
                            }
                        ],
                    }
                }
            }
        )
        assert config.s3 == S3Config(
            profiles={
                "development": {
                    "region": "us-west-2",
                    "rules": [
                        {
                            "path": "s3://project-artifacts/team-a/",
                            "permissions": ["READ", "LIST", "WRITE"],
                        }
                    ],
                }
            },
            default="development",
        )

    def test_requires_default_for_multiple_profiles(self) -> None:
        with pytest.raises(ValidationError, match="default is required"):
            S3Config(profiles={"development": {}, "production": {}})

    def test_rejects_unknown_default_profile(self) -> None:
        with pytest.raises(ValidationError, match="not present"):
            S3Config(default="missing", profiles={"development": {}})

    def test_rejects_unknown_profile_setting(self) -> None:
        with pytest.raises(ValidationError):
            S3Config(profiles={"work": {"endpoint": "https://example.test"}})

    def test_rejects_invalid_profile_name(self) -> None:
        with pytest.raises(ValidationError, match="invalid profile"):
            S3Config(profiles={"not a profile": {}})


class TestS3RuleConfig:
    @pytest.mark.parametrize(
        "path",
        [
            "https://bucket/prefix",
            "s3:///prefix",
            "s3://bucket/prefix?version=1",
            "s3://bucket/prefix#fragment",
            "s3://user@bucket/prefix",
            "s3://bucket:9000/prefix",
        ],
    )
    def test_rejects_invalid_or_ambiguous_paths(self, path: str) -> None:
        with pytest.raises(ValidationError):
            S3Config(
                profiles={
                    "company-dev": {
                        "rules": [{"path": path, "permissions": ["READ"]}],
                    }
                }
            )

    def test_path_prefix_is_literal_and_decoded(self) -> None:
        config = S3Config(
            profiles={
                "company-dev": {
                    "rules": [{"path": "s3://bucket/some%20prefix/", "permissions": ["READ"]}],
                }
            }
        )
        assert config.profiles["company-dev"].rules is not None
        rule = config.profiles["company-dev"].rules[0]
        assert (rule.bucket, rule.prefix) == ("bucket", "some prefix/")

    def test_path_preserves_a_leading_slash_in_the_object_key(self) -> None:
        config = S3Config(
            profiles={
                "company-dev": {
                    "rules": [{"path": "s3://bucket//private/", "permissions": ["READ"]}],
                }
            }
        )
        assert config.profiles["company-dev"].rules is not None
        assert config.profiles["company-dev"].rules[0].prefix == "/private/"


class TestS3Sidecar:
    def test_start_mounts_only_synthetic_named_profiles(self, monkeypatch, tmp_path: Path) -> None:
        class _Server:
            def write_client_files(self, directory: Path) -> Path:
                directory.mkdir(parents=True)
                return directory

            def container_env(self, default_alias: str) -> dict[str, str]:
                return {"AWS_PROFILE": default_alias, "AWS_ENDPOINT_URL_S3": "http://proxy"}

        entered: list[str] = []
        resolved: list[str] = []

        class _Context:
            def __enter__(self) -> _Server:
                entered.append("start")
                return _Server()

            def __exit__(self, *args: object) -> None:
                entered.append("stop")

        def resolve(profile_name, profile) -> credentials.ResolvedAwsProfile:
            resolved.append(profile_name)
            return _profile()

        monkeypatch.setattr(credentials, "resolve_aws_profile", resolve)
        monkeypatch.setattr(proxy, "s3_server", lambda profiles: _Context())
        sidecar = S3Sidecar(
            S3Config(
                default="development",
                profiles={"development": {}, "production": {}},
            ),
            tmp_path,
        )
        assert sidecar.start() == base.SidecarContribution(
            mounts=[BindMount(src=tmp_path / "aws", dst=f"{CONTAINER_HOME}/.aws", mode="RO")],
            env={"AWS_PROFILE": "development", "AWS_ENDPOINT_URL_S3": "http://proxy"},
            needs_host_gateway=True,
        )
        sidecar.stop()
        assert resolved == ["development", "production"]
        assert entered == ["start", "stop"]

    def test_disabled_config_is_a_noop(self, tmp_path: Path) -> None:
        sidecar = S3Sidecar(None, tmp_path)
        assert sidecar.start() is None
        sidecar.stop()
