"""S3 host credential and signing contracts."""

from pathlib import Path

import pytest
from botocore.credentials import Credentials

from seekr_hatchery.sidecars.s3_sidecar import S3Config, S3Sidecar, credentials


def _profile(region: str = "us-west-2", access_key: str = "AKIAHOSTCREDENTIAL") -> credentials.ResolvedAwsProfile:
    return credentials.ResolvedAwsProfile(
        Credentials(access_key, "host-secret", "host-session-token"),
        region,
        f"https://s3.{region}.amazonaws.com",
    )


class TestS3ProfileValidation:
    def test_sidecar_validates_its_config(self, tmp_path: Path, monkeypatch) -> None:
        config = S3Config(profiles={"development": {}})
        validated: list[S3Config] = []
        monkeypatch.setattr(credentials, "validate_host_profiles_exist", validated.append)

        S3Sidecar(config, tmp_path).validate()

        assert validated == [config]

    def test_missing_profile_lists_existing_profiles(self, monkeypatch) -> None:
        class _Session:
            available_profiles = ["production", "development"]

        monkeypatch.setattr(credentials.botocore.session, "Session", _Session)
        config = S3Config(profiles={"not-local": {}})
        with pytest.raises(
            RuntimeError,
            match="AWS profile 'not-local' not found; existing profiles: development, production",
        ):
            credentials.validate_host_profiles_exist(config)


class TestS3Signing:
    def test_sigv4_canonicalizes_query_order(self) -> None:
        profile = _profile()
        headers = {"Host": "s3.us-west-2.amazonaws.com", "X-Amz-Date": "20260101T000000Z"}
        first = credentials.sign_request(
            "GET",
            "https://s3.us-west-2.amazonaws.com/bucket/key?z=last&a=second&a=first",
            headers,
            None,
            profile,
        )
        second = credentials.sign_request(
            "GET",
            "https://s3.us-west-2.amazonaws.com/bucket/key?a=first&z=last&a=second",
            headers,
            None,
            profile,
        )
        assert first["Authorization"] == second["Authorization"]
