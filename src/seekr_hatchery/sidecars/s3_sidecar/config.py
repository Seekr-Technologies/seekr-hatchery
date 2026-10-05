"""Configuration for the built-in S3 sandbox sidecar."""

from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

S3Permission = Literal["READ", "LIST", "WRITE", "DELETE"]


class S3Rule(BaseModel):
    """One additive S3 path permission rule enforced by the proxy."""

    model_config = ConfigDict(extra="forbid")

    path: str
    permissions: list[S3Permission]

    @field_validator("path")
    @classmethod
    def _valid_path(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "s3" or not parsed.netloc:
            raise ValueError("must be an s3://bucket or s3://bucket/prefix path")
        if parsed.query or parsed.fragment or "@" in parsed.netloc or ":" in parsed.netloc:
            raise ValueError("must not contain credentials, a port, query parameters, or a fragment")
        return value

    @field_validator("permissions")
    @classmethod
    def _nonempty_permissions(cls, value: list[S3Permission]) -> list[S3Permission]:
        if not value:
            raise ValueError("must not be empty")
        return value

    @property
    def bucket(self) -> str:
        """Return the bucket selected by this rule."""
        return urlsplit(self.path).netloc

    @property
    def prefix(self) -> str:
        """Return the decoded literal key prefix selected by this rule."""
        path = urlsplit(self.path).path
        return unquote(path[1:] if path.startswith("/") else path)


class S3ProfileConfig(BaseModel):
    """Host AWS profile backing one sandbox-visible profile alias."""

    model_config = ConfigDict(extra="forbid")

    region: str | None = None
    rules: list[S3Rule] | None = None

    @field_validator("region")
    @classmethod
    def _nonempty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be empty")
        return value


class S3Config(BaseModel):
    """Configured sandbox aliases and their host AWS credential sources."""

    model_config = ConfigDict(extra="forbid")

    profiles: dict[str, S3ProfileConfig]
    default: str | None = None

    @field_validator("profiles")
    @classmethod
    def _validate_profiles(cls, profiles: dict[str, S3ProfileConfig]) -> dict[str, S3ProfileConfig]:
        if not profiles:
            raise ValueError("must configure at least one profile")
        for alias in profiles:
            if not alias or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
                for character in alias
            ):
                raise ValueError(f"invalid profile alias {alias!r}; use letters, digits, '.', '_' or '-'")
        return profiles

    @model_validator(mode="after")
    def _validate_default(self) -> "S3Config":
        if self.default is None:
            if len(self.profiles) != 1:
                raise ValueError("default is required when more than one S3 profile is configured")
            self.default = next(iter(self.profiles))
        elif self.default not in self.profiles:
            raise ValueError(f"default profile alias {self.default!r} is not present in profiles")
        return self

    @property
    def default_alias(self) -> str:
        """Return the validated default sandbox profile alias."""
        assert self.default is not None
        return self.default
