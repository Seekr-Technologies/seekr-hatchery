"""Contracts for the OCI Object Storage sandbox sidecar and host proxy."""

from __future__ import annotations

import configparser
import http.client
import io
import socket
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from seekr_hatchery.agents import CONTAINER_HOME
from seekr_hatchery.mount import BindMount
from seekr_hatchery.sidecars import SidecarConfig, base
from seekr_hatchery.sidecars.oci_sidecar import OciConfig, OciSidecar, proxy


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


def _resolved_profile(tmp_path: Path) -> proxy.ResolvedOciProfile:
    key_file = tmp_path / "oci-api.pem"
    key_file.write_text("private key")
    return proxy.ResolvedOciProfile(
        profile_name="DEFAULT",
        key_id="ocid1.tenancy/test-user/aa:bb",
        key_file=key_file,
        pass_phrase=None,
        region="us-ashburn-1",
        endpoint_url="https://objectstorage.us-ashburn-1.oraclecloud.com",
    )


def _auth_header(profile: proxy.ProxyProfile) -> str:
    return (
        'Signature algorithm="rsa-sha256",headers="date (request-target) host",'
        f'keyId="{profile.synthetic.key_id}",signature="ignored",version="1"'
    )


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


class TestOciPolicy:
    @pytest.mark.parametrize(
        ("method", "target", "expected"),
        [
            (
                "GET",
                "/n/my-ns/b/artifacts/o/team-a/file.txt",
                [proxy.PolicyTarget("READ", "my-ns", "artifacts", "team-a/file.txt")],
            ),
            (
                "PUT",
                "/n/my-ns/b/artifacts/o/team-a/file.txt",
                [proxy.PolicyTarget("WRITE", "my-ns", "artifacts", "team-a/file.txt")],
            ),
            (
                "DELETE",
                "/n/my-ns/b/artifacts/o/team-a/file.txt",
                [proxy.PolicyTarget("DELETE", "my-ns", "artifacts", "team-a/file.txt")],
            ),
            (
                "GET",
                "/n/my-ns/b/artifacts/o?prefix=team-a%2F",
                [proxy.PolicyTarget("LIST", "my-ns", "artifacts", "team-a/")],
            ),
            (
                "PUT",
                "/n/my-ns/b/artifacts/u/team-a/file.txt?uploadId=one&uploadPartNum=1",
                [proxy.PolicyTarget("WRITE", "my-ns", "artifacts", "team-a/file.txt")],
            ),
        ],
    )
    def test_classifies_supported_requests(
        self,
        method: str,
        target: str,
        expected: list[proxy.PolicyTarget],
    ) -> None:
        assert proxy._policy_targets(method, target) == expected

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
        assert proxy._policy_targets(method, target) is None

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
        assert proxy._rules_allow(
            rules,
            [proxy.PolicyTarget("READ", "my-ns", "artifacts", "team-a/file")],
        )
        assert not proxy._rules_allow(
            rules,
            [proxy.PolicyTarget("READ", "other-ns", "artifacts", "team-a/file")],
        )
        assert not proxy._rules_allow(
            rules,
            [proxy.PolicyTarget("DELETE", "my-ns", "artifacts", "team-a/file")],
        )
        assert not proxy._rules_allow([], [proxy.PolicyTarget("READ", "my-ns", "artifacts", "team-a/file")])
        assert proxy._rules_allow(None, None)


class TestHostProfiles:
    def _write_config(self, tmp_path: Path) -> tuple[Path, Path]:
        key_file = tmp_path / "oci-api.pem"
        key_file.write_text("private key")
        config_file = tmp_path / "config"
        config_file.write_text(
            "[DEFAULT]\n"
            "tenancy=ocid1.tenancy.oc1..example\n"
            "user=ocid1.user.oc1..example\n"
            "fingerprint=00:11\n"
            f"key_file={key_file}\n"
            "region=us-ashburn-1\n"
            "\n[PRODUCTION]\n"
            "region=us-phoenix-1\n"
        )
        return config_file, key_file

    def test_validation_accepts_existing_api_key_profile(self, tmp_path: Path, monkeypatch) -> None:
        config_file, _ = self._write_config(tmp_path)
        monkeypatch.setattr(proxy.shutil, "which", lambda name: "/usr/bin/openssl")
        proxy.validate_host_profiles_exist(OciConfig(config_file=str(config_file), profiles={"DEFAULT": {}}))

    def test_missing_profile_lists_existing_profiles(self, tmp_path: Path, monkeypatch) -> None:
        config_file, _ = self._write_config(tmp_path)
        monkeypatch.setattr(proxy.shutil, "which", lambda name: "/usr/bin/openssl")
        with pytest.raises(RuntimeError, match="existing profiles: DEFAULT, PRODUCTION"):
            proxy.validate_host_profiles_exist(OciConfig(config_file=str(config_file), profiles={"MISSING": {}}))

    def test_missing_key_file_fails_before_build(self, tmp_path: Path, monkeypatch) -> None:
        config_file, key_file = self._write_config(tmp_path)
        key_file.unlink()
        monkeypatch.setattr(proxy.shutil, "which", lambda name: "/usr/bin/openssl")
        with pytest.raises(RuntimeError, match="key file"):
            proxy.validate_host_profiles_exist(OciConfig(config_file=str(config_file), profiles={"DEFAULT": {}}))

    def test_resolves_standard_profile_and_endpoint(self, tmp_path: Path) -> None:
        config_file, key_file = self._write_config(tmp_path)
        config = OciConfig(config_file=str(config_file), profiles={"DEFAULT": {}})
        resolved = proxy.resolve_oci_profile(config, "DEFAULT", config.profiles["DEFAULT"])
        assert resolved == proxy.ResolvedOciProfile(
            profile_name="DEFAULT",
            key_id="ocid1.tenancy.oc1..example/ocid1.user.oc1..example/00:11",
            key_file=key_file,
            pass_phrase=None,
            region="us-ashburn-1",
            endpoint_url="https://objectstorage.us-ashburn-1.oraclecloud.com",
        )


class TestOciSigning:
    def test_signs_expected_headers_with_host_key(self, tmp_path: Path, monkeypatch) -> None:
        captured: dict[str, object] = {}

        def fake_run(command, **kwargs):
            captured.update({"command": command, **kwargs})
            return SimpleNamespace(returncode=0, stdout=b"signature")

        monkeypatch.setattr(proxy.subprocess, "run", fake_run)
        profile = _resolved_profile(tmp_path)
        headers = {
            "host": "objectstorage.us-ashburn-1.oraclecloud.com",
            "date": "Mon, 05 Oct 2026 12:00:00 GMT",
            "content-length": "3",
            "content-type": "application/octet-stream",
            "x-content-sha256": "hash",
        }
        signed = proxy._sign_request(
            "PUT",
            "/n/ns/b/bucket/o/object",
            headers,
            profile,
        )
        assert captured["command"] == [
            "openssl",
            "dgst",
            "-sha256",
            "-sign",
            str(profile.key_file),
        ]
        assert captured["input"] == (
            b"date: Mon, 05 Oct 2026 12:00:00 GMT\n"
            b"(request-target): put /n/ns/b/bucket/o/object\n"
            b"host: objectstorage.us-ashburn-1.oraclecloud.com\n"
            b"content-length: 3\n"
            b"content-type: application/octet-stream\n"
            b"x-content-sha256: hash"
        )
        assert signed["Authorization"].startswith('Signature algorithm="rsa-sha256",')


class TestOciProxy:
    def test_forwards_authenticated_allowed_request(self, tmp_path: Path, monkeypatch) -> None:
        pool = _Pool()
        resolved = _resolved_profile(tmp_path)
        rules = (
            OciConfig(
                profiles={
                    "DEFAULT": {
                        "rules": [
                            {
                                "path": "oci://my-ns/artifacts/team-a/",
                                "permissions": ["READ"],
                            }
                        ]
                    }
                }
            )
            .profiles["DEFAULT"]
            .rules
        )
        monkeypatch.setattr(proxy, "_sign_request", lambda method, target, headers, profile: headers)

        with proxy.oci_server({"DEFAULT": (resolved, rules)}, _pool=pool) as server:
            connection = http.client.HTTPConnection("127.0.0.1", server.port)
            connection.request(
                "GET",
                "/n/my-ns/b/artifacts/o/team-a/file.txt",
                headers={"Authorization": _auth_header(server.profiles["DEFAULT"])},
            )
            response = connection.getresponse()
            assert (response.status, response.read()) == (200, b"ok")
            connection.close()

        assert pool.calls == [
            {
                "method": "GET",
                "url": ("https://objectstorage.us-ashburn-1.oraclecloud.com/n/my-ns/b/artifacts/o/team-a/file.txt"),
                "headers": pool.calls[0]["headers"],
                "body": b"",
                "chunked": False,
            }
        ]
        assert "Authorization" not in pool.calls[0]["headers"]

    def test_consumes_expect_header_before_streaming_upload(self, tmp_path: Path, monkeypatch) -> None:
        pool = _Pool()
        resolved = _resolved_profile(tmp_path)
        rules = (
            OciConfig(
                profiles={
                    "DEFAULT": {
                        "rules": [
                            {
                                "path": "oci://my-ns/artifacts/temp/",
                                "permissions": ["WRITE"],
                            }
                        ]
                    }
                }
            )
            .profiles["DEFAULT"]
            .rules
        )
        monkeypatch.setattr(proxy, "_sign_request", lambda method, target, headers, profile: headers)

        with proxy.oci_server({"DEFAULT": (resolved, rules)}, _pool=pool) as server:
            authorization = _auth_header(server.profiles["DEFAULT"])
            connection = socket.create_connection(("127.0.0.1", server.port), timeout=2)
            request_headers = (
                "PUT /n/my-ns/b/artifacts/o/temp/upload.txt HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{server.port}\r\n"
                f"Authorization: {authorization}\r\n"
                "Content-Type: application/octet-stream\r\n"
                "Content-Length: 4\r\n"
                "x-content-sha256: synthetic-hash\r\n"
                "Expect: 100-continue\r\n"
                "\r\n"
            )
            connection.sendall(request_headers.encode())
            interim = connection.recv(4096)
            assert interim == b"HTTP/1.1 100 Continue\r\n\r\n"

            connection.sendall(b"test")
            final = b""
            while chunk := connection.recv(4096):
                final += chunk
            connection.close()

        assert b"HTTP/1.1 200 OK" in final
        assert pool.calls[0]["body"] == b"test"
        assert all(name.lower() != "expect" for name in pool.calls[0]["headers"])

    def test_rejects_request_outside_policy(self, tmp_path: Path) -> None:
        pool = _Pool()
        resolved = _resolved_profile(tmp_path)
        rules = (
            OciConfig(
                profiles={
                    "DEFAULT": {
                        "rules": [
                            {
                                "path": "oci://my-ns/artifacts/team-a/",
                                "permissions": ["READ"],
                            }
                        ]
                    }
                }
            )
            .profiles["DEFAULT"]
            .rules
        )

        with proxy.oci_server({"DEFAULT": (resolved, rules)}, _pool=pool) as server:
            connection = http.client.HTTPConnection("127.0.0.1", server.port)
            connection.request(
                "GET",
                "/n/my-ns/b/artifacts/o/team-b/file.txt",
                headers={"Authorization": _auth_header(server.profiles["DEFAULT"])},
            )
            response = connection.getresponse()
            assert response.status == 403
            response.read()
            connection.close()

        assert pool.calls == []


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
        monkeypatch.setattr(proxy, "validate_host_profiles_exist", validated.append)

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
            proxy,
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
