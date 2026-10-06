"""Host profile resolution, synthetic credentials, and SigV4 signing for S3."""

import secrets
from dataclasses import dataclass
from typing import Any

import botocore.session
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.credentials import Credentials, ReadOnlyCredentials
from botocore.exceptions import BotoCoreError, ProfileNotFound

from seekr_hatchery.sidecars.s3_sidecar.config import S3Config, S3ProfileConfig


@dataclass(frozen=True)
class ResolvedAwsProfile:
    """Host credential source and resolved S3 endpoint for one proxy lifetime."""

    credentials: Credentials | ReadOnlyCredentials
    region: str
    endpoint_url: str

    def frozen_credentials(self) -> ReadOnlyCredentials:
        """Return current credentials, refreshing botocore providers when needed."""
        if isinstance(self.credentials, ReadOnlyCredentials):
            return self.credentials
        return self.credentials.get_frozen_credentials()


@dataclass(frozen=True)
class SyntheticCredentials:
    """Random credentials accepted only by one proxy instance."""

    access_key: str
    secret_key: str
    session_token: str

    @classmethod
    def create(cls) -> "SyntheticCredentials":
        return cls(
            access_key=f"HATCHERYS3{secrets.token_hex(12).upper()}",
            secret_key=secrets.token_urlsafe(48),
            session_token=secrets.token_urlsafe(48),
        )


def sign_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: Any,
    profile: ResolvedAwsProfile,
) -> dict[str, str]:
    """Return SigV4 headers for *url* without consuming a streaming body."""
    request = AWSRequest(method=method, url=url, data=body, headers=headers)
    request.context["client_config"] = Config(s3={"payload_signing_enabled": False})
    S3SigV4Auth(profile.frozen_credentials(), "s3", profile.region).add_auth(request)
    return dict(request.headers.items())


def validate_host_profiles_exist(config: S3Config) -> None:
    """Verify that every syntactically valid profile exists in the host AWS config."""
    available = sorted(botocore.session.Session().available_profiles)
    available_set = set(available)
    for profile_name in config.profiles:
        if profile_name not in available_set:
            existing = ", ".join(available) if available else "(none)"
            raise RuntimeError(f"s3: AWS profile {profile_name!r} not found; existing profiles: {existing}")


def resolve_aws_profile(profile_name: str, config: S3ProfileConfig) -> ResolvedAwsProfile:
    """Resolve the configured host profile and its regional public S3 endpoint."""
    try:
        session = botocore.session.Session(profile=profile_name)
        credentials = session.get_credentials()
        if credentials is None:
            raise RuntimeError(f"s3: no AWS credentials resolved for profile {profile_name!r}")
        credentials.get_frozen_credentials()
        region = config.region or session.get_config_variable("region") or "us-east-1"
        endpoint = session.get_component("endpoint_resolver").construct_endpoint("s3", region)
        if endpoint is None or "hostname" not in endpoint:
            raise RuntimeError(f"s3: no AWS S3 endpoint is available for region {region!r}")
        return ResolvedAwsProfile(credentials, region, f"https://{endpoint['hostname']}")
    except ProfileNotFound as exc:
        raise RuntimeError(f"s3: AWS profile {profile_name!r} was not found") from exc
    except BotoCoreError as exc:
        raise RuntimeError(f"s3: could not resolve AWS credentials from {profile_name}: {exc}") from exc
