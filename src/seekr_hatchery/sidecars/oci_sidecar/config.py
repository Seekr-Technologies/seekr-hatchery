"""Configuration for the built-in OCI Object Storage sandbox sidecar."""

from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

OciPermission = Literal["READ", "LIST", "WRITE", "DELETE"]


class OciRule(BaseModel):
    """One additive OCI Object Storage path rule enforced by the proxy."""

    model_config = ConfigDict(extra="forbid")

    path: str
    permissions: list[OciPermission]

    @field_validator("path")
    @classmethod
    def _valid_path(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "oci" or not parsed.netloc:
            raise ValueError("must be an oci://namespace/bucket or oci://namespace/bucket/prefix path")
        if parsed.query or parsed.fragment or "@" in parsed.netloc or ":" in parsed.netloc:
            raise ValueError("must not contain credentials, a port, query parameters, or a fragment")
        parts = parsed.path.lstrip("/").split("/", 1)
        if not parts[0]:
            raise ValueError("must include a bucket after the namespace")
        return value

    @field_validator("permissions")
    @classmethod
    def _nonempty_permissions(cls, value: list[OciPermission]) -> list[OciPermission]:
        if not value:
            raise ValueError("must not be empty")
        return value

    @property
    def namespace(self) -> str:
        """Return the namespace selected by this rule."""
        return urlsplit(self.path).netloc

    @property
    def bucket(self) -> str:
        """Return the bucket selected by this rule."""
        return unquote(urlsplit(self.path).path.lstrip("/").split("/", 1)[0])

    @property
    def prefix(self) -> str:
        """Return the decoded literal object-name prefix selected by this rule."""
        parts = urlsplit(self.path).path.lstrip("/").split("/", 1)
        return unquote(parts[1]) if len(parts) == 2 else ""


class OciProfileConfig(BaseModel):
    """Host OCI profile and optional proxy policy."""

    model_config = ConfigDict(extra="forbid")

    rules: list[OciRule] | None = None
    endpoint: str | None = None

    @field_validator("endpoint")
    @classmethod
    def _valid_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
            raise ValueError("must be an HTTPS origin without a path, query, or fragment")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("must be an HTTPS origin without credentials, a query, or a fragment")
        return value.rstrip("/")


class OciConfig(BaseModel):
    """Configured host OCI profiles exposed through the sandbox proxy."""

    model_config = ConfigDict(extra="forbid")

    profiles: dict[str, OciProfileConfig]
    default: str | None = None
    config_file: str = "~/.oci/config"

    @field_validator("profiles")
    @classmethod
    def _validate_profiles(cls, profiles: dict[str, OciProfileConfig]) -> dict[str, OciProfileConfig]:
        if not profiles:
            raise ValueError("must configure at least one profile")
        for profile_name in profiles:
            if not profile_name or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
                for character in profile_name
            ):
                raise ValueError(f"invalid profile name {profile_name!r}; use letters, digits, '.', '_' or '-'")
        return profiles

    @field_validator("config_file")
    @classmethod
    def _nonempty_config_file(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @model_validator(mode="after")
    def _validate_default(self) -> "OciConfig":
        if self.default is None:
            if len(self.profiles) != 1:
                raise ValueError("default is required when more than one OCI profile is configured")
            self.default = next(iter(self.profiles))
        elif self.default not in self.profiles:
            raise ValueError(f"default profile {self.default!r} is not present in profiles")
        return self

    @property
    def default_profile(self) -> str:
        """Return the validated default profile name."""
        assert self.default is not None
        return self.default
