"""OCI sidecar configuration, files, and lifecycle contracts."""

from __future__ import annotations

import configparser
import io
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from seekr_hatchery.agents import CONTAINER_HOME
from seekr_hatchery.mount import BindMount
from seekr_hatchery.sidecars import SidecarConfig, base
from seekr_hatchery.sidecars.oci_sidecar import OciConfig, OciSidecar, credentials, proxy


def _resolved_profile(tmp_path: Path) -> credentials.ResolvedOciProfile:
    key_file = tmp_path / "oci-api.pem"
    key_file.write_text("private key")
    return credentials.ResolvedOciProfile(
        profile_name="DEFAULT",
        key_id="ocid1.tenancy/test-user/aa:bb",
        key_file=key_file,
        pass_phrase=None,
        region="us-ashburn-1",
        endpoint_url="https://objectstorage.us-ashburn-1.oraclecloud.com",
    )


class _Response:
    def __init__(self, status: int = 200, body: bytes = b"ok") -> None:
        self.status = status
        self.headers = {"content-type": "text/plain", "content-length": str(len(body))}
        self._body = io.BytesIO(body)

    def read(self, size: int) -> bytes:
        return self._body.read(size)

    def drain_conn(self) -> None:
        pass


class _Pool:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.response = _Response()

    def urlopen(self, method: str, url: str, **kwargs: object) -> _Response:
        body = kwargs["body"]
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": kwargs["headers"],
                "body": body.read(),
                "chunked": kwargs["chunked"],
            }
        )
        return self.response


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


class TestOciServerFiles:
    def test_writes_synthetic_profiles_and_environment(self, tmp_path: Path, monkeypatch) -> None:
        def fake_run(command, **kwargs):
            Path(command[3]).write_text("synthetic private key")
            return SimpleNamespace(returncode=0, stdout=b"")

        monkeypatch.setattr(proxy.subprocess, "run", fake_run)
        resolved = _resolved_profile(tmp_path)
        with proxy.oci_server({"DEFAULT": (resolved, None)}, _pool=_Pool()) as server:
            directory = server.write_client_files(tmp_path / "client")
            profile = server.profiles["DEFAULT"]
            parser = configparser.ConfigParser(interpolation=None)
            parser.read(directory / "config")
            assert parser["DEFAULT"]["region"] == "us-ashburn-1"
            assert parser["DEFAULT"]["key_file"] == (f"{CONTAINER_HOME}/.oci/{profile.synthetic.key_filename}")
            assert (directory / profile.synthetic.key_filename).read_text() == ("synthetic private key\nOCI_API_KEY\n")
            assert server.container_env("DEFAULT") == {
                "OCI_CONFIG_FILE": "/home/hatchery/.oci/config",
                "OCI_CLI_PROFILE": "DEFAULT",
                "OCI_CLI_ENDPOINT": f"http://host.docker.internal:{server.port}",
                "OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING": "True",
            }


class TestOciSidecar:
    def test_validates_host_profiles(self, tmp_path: Path, monkeypatch) -> None:
        config = OciConfig(profiles={"DEFAULT": {}})
        validated: list[OciConfig] = []
        monkeypatch.setattr(credentials, "validate_host_profiles_exist", validated.append)

        OciSidecar(config, tmp_path).validate()

        assert validated == [config]

    def test_starts_proxy_and_mounts_isolated_config(self, tmp_path: Path, monkeypatch) -> None:
        entered: list[str] = []

        class _Server:
            def write_client_files(self, directory: Path) -> Path:
                directory.mkdir()
                return directory

            def container_env(self, default_profile: str) -> dict[str, str]:
                return {"OCI_CLI_PROFILE": default_profile, "OCI_CLI_ENDPOINT": "http://proxy"}

        @contextmanager
        def fake_server(resolved):
            entered.append("start")
            try:
                yield _Server()
            finally:
                entered.append("stop")

        monkeypatch.setattr(
            credentials,
            "resolve_oci_profile",
            lambda config, name, profile: _resolved_profile(tmp_path),
        )
        monkeypatch.setattr(proxy, "oci_server", fake_server)
        config = OciConfig(profiles={"DEFAULT": {}})
        sidecar = OciSidecar(config, tmp_path)
        contribution = sidecar.start()
        assert contribution == base.SidecarContribution(
            mounts=[
                BindMount(
                    src=tmp_path / "oci",
                    dst=f"{CONTAINER_HOME}/.oci",
                    mode="RO",
                )
            ],
            env={"OCI_CLI_PROFILE": "DEFAULT", "OCI_CLI_ENDPOINT": "http://proxy"},
            needs_host_gateway=True,
        )
        sidecar.stop()
        assert entered == ["start", "stop"]

    def test_disabled_config_is_a_noop(self, tmp_path: Path) -> None:
        sidecar = OciSidecar(None, tmp_path)
        assert sidecar.start() is None
        sidecar.stop()
