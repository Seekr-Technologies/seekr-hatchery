"""OCI profile resolution, synthetic identities, and API-key request signing."""

import base64
import configparser
import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from seekr_hatchery.sidecars.oci_sidecar.config import OciConfig, OciProfileConfig

_BODY_METHODS = frozenset({"PUT", "POST", "PATCH"})
_REQUIRED_PROFILE_FIELDS = ("tenancy", "user", "fingerprint", "key_file", "region")


@dataclass(frozen=True)
class ResolvedOciProfile:
    """Host API-key identity and regional Object Storage endpoint."""

    profile_name: str
    key_id: str
    key_file: Path
    pass_phrase: str | None
    region: str
    endpoint_url: str


@dataclass(frozen=True)
class SyntheticIdentity:
    """Random OCI identity accepted only by one proxy instance."""

    key_id: str
    key_filename: str

    @classmethod
    def create(cls, index: int) -> "SyntheticIdentity":
        tenancy = f"ocid1.tenancy.oc1..hatchery{secrets.token_hex(24)}"
        user = f"ocid1.user.oc1..hatchery{secrets.token_hex(24)}"
        fingerprint = ":".join(f"{byte:02x}" for byte in secrets.token_bytes(16))
        return cls(f"{tenancy}/{user}/{fingerprint}", f"profile-{index}.pem")


def _case_insensitive_value(headers: dict[str, str], name: str) -> str | None:
    return next((value for key, value in headers.items() if key.lower() == name.lower()), None)


def sign_request(
    method: str,
    request_target: str,
    headers: dict[str, str],
    profile: ResolvedOciProfile,
) -> dict[str, str]:
    """Sign an upstream request with the host profile's API key."""
    signed_names = ["date", "(request-target)", "host"]
    if method in _BODY_METHODS and _case_insensitive_value(headers, "x-content-sha256") is not None:
        for required in ("content-length", "content-type"):
            if _case_insensitive_value(headers, required) is None:
                raise RuntimeError(f"oci: hashed request body is missing signing header {required!r}")
        signed_names.extend(["content-length", "content-type", "x-content-sha256"])

    lines: list[str] = []
    for name in signed_names:
        if name == "(request-target)":
            value = f"{method.lower()} {request_target}"
        else:
            value = _case_insensitive_value(headers, name)
            if value is None:
                raise RuntimeError(f"oci: request is missing signing header {name!r}")
        lines.append(f"{name}: {value}")

    env = os.environ.copy()
    command = ["openssl", "dgst", "-sha256", "-sign", str(profile.key_file)]
    if profile.pass_phrase is not None:
        env["HATCHERY_OCI_KEY_PASSPHRASE"] = profile.pass_phrase
        command.extend(["-passin", "env:HATCHERY_OCI_KEY_PASSPHRASE"])
    result = subprocess.run(
        command,
        input="\n".join(lines).encode(),
        capture_output=True,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError("oci: failed to sign request with the configured API key")
    signature = base64.b64encode(result.stdout).decode()
    headers["Authorization"] = (
        'Signature algorithm="rsa-sha256",'
        f'headers="{" ".join(signed_names)}",'
        f'keyId="{profile.key_id}",'
        f'signature="{signature}",version="1"'
    )
    return headers


def _load_config(config: OciConfig) -> tuple[configparser.ConfigParser, Path]:
    path = Path(os.path.expandvars(os.path.expanduser(config.config_file)))
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(path):
        raise RuntimeError(f"oci: config file not found: {path}")
    return parser, path


def _available_profiles(parser: configparser.ConfigParser) -> list[str]:
    profiles = list(parser.sections())
    if parser.defaults():
        profiles.insert(0, "DEFAULT")
    return profiles


def _profile_values(parser: configparser.ConfigParser, profile_name: str) -> dict[str, str]:
    if profile_name == "DEFAULT":
        return dict(parser.defaults())
    return dict(parser[profile_name])


def validate_host_profiles_exist(config: OciConfig) -> None:
    """Verify profiles, API-key fields, key files, and OpenSSL before build."""
    if shutil.which("openssl") is None:
        raise RuntimeError("oci: openssl is required to isolate and sign API-key credentials")
    parser, _ = _load_config(config)
    available = _available_profiles(parser)
    for profile_name in config.profiles:
        if profile_name not in available:
            existing = ", ".join(sorted(available)) if available else "(none)"
            raise RuntimeError(f"oci: profile {profile_name!r} not found; existing profiles: {existing}")
        values = _profile_values(parser, profile_name)
        missing = [field for field in _REQUIRED_PROFILE_FIELDS if not values.get(field)]
        if missing:
            raise RuntimeError(f"oci: profile {profile_name!r} is missing required fields: {', '.join(missing)}")
        key_file = Path(os.path.expandvars(os.path.expanduser(values["key_file"])))
        if not key_file.is_file():
            raise RuntimeError(f"oci: key file for profile {profile_name!r} not found: {key_file}")


def resolve_oci_profile(
    config: OciConfig,
    profile_name: str,
    profile_config: OciProfileConfig,
) -> ResolvedOciProfile:
    """Resolve one validated standard OCI API-key profile."""
    parser, _ = _load_config(config)
    values = _profile_values(parser, profile_name)
    region = values["region"]
    endpoint = profile_config.endpoint or f"https://objectstorage.{region}.oraclecloud.com"
    key_file = Path(os.path.expandvars(os.path.expanduser(values["key_file"])))
    return ResolvedOciProfile(
        profile_name=profile_name,
        key_id=f"{values['tenancy']}/{values['user']}/{values['fingerprint']}",
        key_file=key_file,
        pass_phrase=values.get("pass_phrase"),
        region=region,
        endpoint_url=endpoint.rstrip("/"),
    )
