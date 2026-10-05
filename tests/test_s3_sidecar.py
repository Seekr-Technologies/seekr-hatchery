"""Contracts for the built-in S3 sandbox sidecar and its host proxy."""

from __future__ import annotations

import configparser
import http.client
import io
import logging
import socket
import time
from email.parser import BytesParser
from pathlib import Path

import pytest
from botocore.credentials import Credentials
from pydantic import ValidationError

from seekr_hatchery.agents import CONTAINER_HOME
from seekr_hatchery.mount import BindMount
from seekr_hatchery.sidecars import SidecarConfig, base
from seekr_hatchery.sidecars.s3_sidecar import S3Config, S3Sidecar, proxy


class _Response:
    def __init__(self, status: int = 200, body: bytes = b"ok") -> None:
        self.status = status
        self.headers = {"content-type": "text/plain", "content-length": str(len(body))}
        self._body = io.BytesIO(body)
        self.drained = False

    def read(self, size: int) -> bytes:
        return self._body.read(size)

    def drain_conn(self) -> None:
        self.drained = True


class _Pool:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.response = _Response()

    def urlopen(self, method: str, url: str, **kwargs: object) -> _Response:
        self.response = _Response()
        body = kwargs["body"]
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": kwargs["headers"],
                "body": body.read(),
                "chunked": kwargs["chunked"],
                "timeout": kwargs["timeout"],
            }
        )
        return self.response


def _profile(region: str = "us-west-2", access_key: str = "AKIAHOSTCREDENTIAL") -> proxy.ResolvedAwsProfile:
    return proxy.ResolvedAwsProfile(
        Credentials(access_key, "host-secret", "host-session-token"),
        region,
        f"https://s3.{region}.amazonaws.com",
    )


def _resolved_profiles() -> dict[str, tuple[proxy.ResolvedAwsProfile, None]]:
    return {"development": (_profile(), None)}


def _wait_for_port(port: int) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port)
            connection.connect()
            connection.close()
            return
        except OSError:
            time.sleep(0.01)
    raise TimeoutError("S3 proxy did not start")


def _auth_headers(profile: proxy.ProxyProfile) -> dict[str, str]:
    return {
        "Authorization": (
            "AWS4-HMAC-SHA256 "
            f"Credential={profile.synthetic.access_key}/20260101/us-west-2/s3/aws4_request, SignedHeaders=host"
        ),
        "X-Amz-Security-Token": profile.synthetic.session_token,
    }


class TestS3ProfileValidation:
    def test_sidecar_validates_its_config(self, tmp_path: Path, monkeypatch) -> None:
        config = S3Config(profiles={"development": {}})
        validated: list[S3Config] = []
        monkeypatch.setattr(proxy, "validate_host_profiles_exist", validated.append)

        S3Sidecar(config, tmp_path).validate()

        assert validated == [config]

    def test_missing_profile_lists_existing_profiles(self, monkeypatch) -> None:
        class _Session:
            available_profiles = ["production", "development"]

        monkeypatch.setattr(proxy.botocore.session, "Session", _Session)
        config = S3Config(profiles={"not-local": {}})
        with pytest.raises(
            RuntimeError,
            match="AWS profile 'not-local' not found; existing profiles: development, production",
        ):
            proxy.validate_host_profiles_exist(config)


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


class TestS3Policy:
    @staticmethod
    def _headers(**headers: str):
        payload = "".join(f"{name}: {value}\n" for name, value in headers.items()).encode() + b"\n"
        return BytesParser().parsebytes(payload)

    @pytest.mark.parametrize(
        ("method", "target", "permission", "bucket", "key"),
        [
            ("GET", "/artifacts/team-a/file.txt", "READ", "artifacts", "team-a/file.txt"),
            ("GET", "/artifacts/team-a/file.txt?x-id=GetObject", "READ", "artifacts", "team-a/file.txt"),
            ("PUT", "/artifacts/team-a/file.txt", "WRITE", "artifacts", "team-a/file.txt"),
            ("PUT", "/artifacts/team-a/file.txt?x-id=PutObject", "WRITE", "artifacts", "team-a/file.txt"),
            ("DELETE", "/artifacts/team-a/file.txt?versionId=1", "DELETE", "artifacts", "team-a/file.txt"),
            ("GET", "/artifacts?list-type=2&prefix=team-a%2F", "LIST", "artifacts", "team-a/"),
            ("POST", "/artifacts/team-a/file?uploads", "WRITE", "artifacts", "team-a/file"),
            ("PUT", "/artifacts/team-a/file?uploadId=u&partNumber=1", "WRITE", "artifacts", "team-a/file"),
            ("GET", "/artifacts/team-a/file?uploadId=u", "WRITE", "artifacts", "team-a/file"),
            ("DELETE", "/artifacts/team-a/file?uploadId=u", "WRITE", "artifacts", "team-a/file"),
        ],
    )
    def test_classifies_supported_data_operations(
        self,
        method: str,
        target: str,
        permission: str,
        bucket: str,
        key: str,
    ) -> None:
        assert proxy._policy_targets(method, target, self._headers()) == [proxy.PolicyTarget(permission, bucket, key)]

    @pytest.mark.parametrize(
        ("method", "target"),
        [
            ("GET", "/"),
            ("GET", "/artifacts?acl"),
            ("PUT", "/artifacts/team-a/file?tagging"),
            ("POST", "/artifacts?delete"),
            ("DELETE", "/artifacts"),
            ("GET", "/artifacts?list-type=2&prefix=team-a%2F&prefix=team-b%2F"),
        ],
    )
    def test_rejects_administrative_bulk_and_ambiguous_operations(self, method: str, target: str) -> None:
        assert proxy._policy_targets(method, target, self._headers()) is None

    def test_rules_require_bucket_prefix_and_permission(self) -> None:
        rules = (
            S3Config(
                profiles={
                    "development": {
                        "rules": [
                            {
                                "path": "s3://artifacts/team-a/",
                                "permissions": ["READ", "LIST", "WRITE"],
                            }
                        ],
                    }
                }
            )
            .profiles["development"]
            .rules
        )
        assert proxy._rules_allow(rules, [proxy.PolicyTarget("READ", "artifacts", "team-a/file")]) is True
        assert proxy._rules_allow(rules, [proxy.PolicyTarget("READ", "artifacts", "team-b/file")]) is False
        assert proxy._rules_allow(rules, [proxy.PolicyTarget("DELETE", "artifacts", "team-a/file")]) is False
        assert proxy._rules_allow([], [proxy.PolicyTarget("READ", "artifacts", "team-a/file")]) is False
        assert proxy._rules_allow(None, None) is True

    def test_bucket_path_explicitly_allows_the_whole_bucket(self) -> None:
        config = S3Config(
            profiles={
                "company-dev": {
                    "rules": [{"path": "s3://artifacts", "permissions": ["READ"]}],
                }
            }
        )
        assert config.profiles["company-dev"].rules is not None
        assert config.profiles["company-dev"].rules[0].bucket == "artifacts"
        assert config.profiles["company-dev"].rules[0].prefix == ""

    def test_copy_requires_destination_write_and_source_read(self) -> None:
        targets = proxy._policy_targets(
            "PUT",
            "/dest/out/copied.txt",
            self._headers(**{"X-Amz-Copy-Source": "/source/in/original.txt?versionId=1"}),
        )
        assert targets == [
            proxy.PolicyTarget("WRITE", "dest", "out/copied.txt"),
            proxy.PolicyTarget("READ", "source", "in/original.txt"),
        ]
        rules = (
            S3Config(
                profiles={
                    "copy": {
                        "rules": [
                            {"path": "s3://dest/out/", "permissions": ["WRITE"]},
                            {"path": "s3://source/in/", "permissions": ["READ"]},
                        ],
                    }
                }
            )
            .profiles["copy"]
            .rules
        )
        assert proxy._rules_allow(rules, targets) is True
        assert proxy._rules_allow(rules[:1], targets) is False


class TestS3Proxy:
    def test_listens_on_container_reachable_host_interfaces(self) -> None:
        with proxy.s3_server(_resolved_profiles()) as server:
            assert server._server.server_address[0] == "0.0.0.0"

    def test_writes_named_synthetic_profiles_and_container_environment(self, tmp_path: Path) -> None:
        profiles = {
            "development": (_profile(), None),
            "production-readonly": (_profile("eu-west-1", "AKIAPROD"), None),
        }
        with proxy.s3_server(profiles) as server:
            directory = server.write_client_files(tmp_path / "aws")
            assert server.container_env("development") == {
                "AWS_PROFILE": "development",
                "AWS_CONFIG_FILE": "/home/hatchery/.aws/config",
                "AWS_SHARED_CREDENTIALS_FILE": "/home/hatchery/.aws/credentials",
                "AWS_ENDPOINT_URL_S3": f"http://host.docker.internal:{server.port}",
                "AWS_EC2_METADATA_DISABLED": "true",
            }
            config = configparser.RawConfigParser()
            config.read(directory / "config")
            credentials = configparser.RawConfigParser()
            credentials.read(directory / "credentials")

        assert config.sections() == ["profile development", "profile production-readonly"]
        assert config["profile development"]["region"] == "us-west-2"
        assert config["profile production-readonly"]["region"] == "eu-west-1"
        assert credentials.sections() == ["development", "production-readonly"]
        assert credentials["development"]["aws_access_key_id"].startswith("HATCHERYS3")
        assert credentials["production-readonly"]["aws_access_key_id"].startswith("HATCHERYS3")
        assert "AKIAHOSTCREDENTIAL" not in (directory / "credentials").read_text()
        assert (directory / "config").stat().st_mode & 0o777 == 0o600
        assert (directory / "credentials").stat().st_mode & 0o777 == 0o600

    def test_rejects_missing_or_wrong_synthetic_identity(self) -> None:
        with proxy.s3_server(_resolved_profiles()) as server:
            _wait_for_port(server.port)
            connection = http.client.HTTPConnection("127.0.0.1", server.port)
            connection.request("GET", "/bucket/key")
            response = connection.getresponse()
            assert (response.status, response.read()) == (403, b'{"error":"invalid synthetic S3 credentials"}')

    def test_routes_each_synthetic_profile_to_its_host_profile(self) -> None:
        pool = _Pool()
        profiles = {
            "development": (_profile(access_key="AKIADEV"), None),
            "production": (_profile("eu-west-1", "AKIAPROD"), None),
        }
        with proxy.s3_server(profiles, _pool=pool) as server:
            _wait_for_port(server.port)
            for alias in ("development", "production"):
                connection = http.client.HTTPConnection("127.0.0.1", server.port)
                connection.request("GET", f"/bucket/{alias}", headers=_auth_headers(server.profiles[alias]))
                response = connection.getresponse()
                assert response.status == 200
                response.read()

        first_headers = pool.calls[0]["headers"]
        second_headers = pool.calls[1]["headers"]
        assert isinstance(first_headers, dict)
        assert isinstance(second_headers, dict)
        assert "AKIADEV" in first_headers["Authorization"]
        assert "AKIAPROD" in second_headers["Authorization"]
        assert pool.calls[0]["url"] == "https://s3.us-west-2.amazonaws.com/bucket/development"
        assert pool.calls[1]["url"] == "https://s3.eu-west-1.amazonaws.com/bucket/production"

    def test_denies_unmatched_policy_before_contacting_upstream(self) -> None:
        pool = _Pool()
        rules = (
            S3Config(
                profiles={
                    "readonly": {
                        "rules": [{"path": "s3://artifacts/published/", "permissions": ["READ"]}],
                    }
                }
            )
            .profiles["readonly"]
            .rules
        )
        with proxy.s3_server({"readonly": (_profile(), rules)}, _pool=pool) as server:
            _wait_for_port(server.port)
            connection = http.client.HTTPConnection("127.0.0.1", server.port)
            connection.request(
                "PUT",
                "/artifacts/published/file.txt",
                body=b"blocked",
                headers=_auth_headers(server.profiles["readonly"]),
            )
            response = connection.getresponse()
            assert (response.status, response.read()) == (403, b'{"error":"S3 proxy policy denied request"}')

        assert pool.calls == []

    def test_streams_body_and_re_signs_encoded_path_and_query_upstream(self) -> None:
        pool = _Pool()
        with proxy.s3_server(_resolved_profiles(), _pool=pool) as server:
            _wait_for_port(server.port)
            headers = {**_auth_headers(server.profiles["development"]), "X-Test": "preserved"}
            connection = http.client.HTTPConnection("127.0.0.1", server.port)
            connection.request("PUT", "/bucket/a%2Fb?versionId=v1&partNumber=2", b"large-body", headers)
            response = connection.getresponse()
            assert (response.status, response.read()) == (200, b"ok")

        assert pool.calls == [
            {
                "method": "PUT",
                "url": "https://s3.us-west-2.amazonaws.com/bucket/a%2Fb?versionId=v1&partNumber=2",
                "headers": pool.calls[0]["headers"],
                "body": b"large-body",
                "chunked": False,
                "timeout": proxy._UPSTREAM_TIMEOUT,
            }
        ]
        forwarded = pool.calls[0]["headers"]
        assert isinstance(forwarded, dict)
        assert forwarded["Host"] == "s3.us-west-2.amazonaws.com"
        assert forwarded["X-Test"] == "preserved"
        assert forwarded["X-Amz-Content-SHA256"] == "UNSIGNED-PAYLOAD"
        assert "AKIAHOSTCREDENTIAL" in forwarded["Authorization"]

    def test_rejects_ambiguous_duplicate_content_length(self) -> None:
        with proxy.s3_server(_resolved_profiles()) as server:
            _wait_for_port(server.port)
            synthetic = server.profiles["development"].synthetic
            request = (
                "PUT /bucket/key HTTP/1.1\r\n"
                "Host: localhost\r\n"
                f"Authorization: AWS4-HMAC-SHA256 Credential={synthetic.access_key}/date/region/s3/aws4_request\r\n"
                f"X-Amz-Security-Token: {synthetic.session_token}\r\n"
                "Content-Length: 4\r\n"
                "Content-Length: 4\r\n\r\nbody"
            ).encode()
            with socket.create_connection(("127.0.0.1", server.port)) as connection:
                connection.sendall(request)
                assert connection.recv(1024).startswith(b"HTTP/1.1 400")

    def test_combines_duplicate_headers_and_removes_synthetic_auth_headers(self) -> None:
        headers = BytesParser().parsebytes(
            b"X-Meta: first\n"
            b"X-Meta: second\n"
            b"Authorization: AWS4-HMAC-SHA256 Credential=synthetic/date/region/s3/aws4_request\n"
            b"X-Amz-Security-Token: synthetic-token\n"
            b"Connection: keep-alive, X-Remove\n"
            b"X-Remove: no\n\n"
        )
        assert proxy._forward_headers(headers, "s3.us-west-2.amazonaws.com") == {
            "X-Meta": "first,second",
            "Host": "s3.us-west-2.amazonaws.com",
        }

    def test_sigv4_canonicalizes_query_order(self) -> None:
        profile = _profile()
        headers = {"Host": "s3.us-west-2.amazonaws.com", "X-Amz-Date": "20260101T000000Z"}
        first = proxy._sign_request(
            "GET",
            "https://s3.us-west-2.amazonaws.com/bucket/key?z=last&a=second&a=first",
            headers,
            None,
            profile,
        )
        second = proxy._sign_request(
            "GET",
            "https://s3.us-west-2.amazonaws.com/bucket/key?a=first&z=last&a=second",
            headers,
            None,
            profile,
        )
        assert first["Authorization"] == second["Authorization"]

    def test_query_credentials_are_not_written_to_logs(self, caplog) -> None:
        secret = "must-not-appear"
        with caplog.at_level(logging.INFO, logger=proxy.__name__):
            with proxy.s3_server(_resolved_profiles()) as server:
                _wait_for_port(server.port)
                connection = http.client.HTTPConnection("127.0.0.1", server.port)
                connection.request("GET", f"/bucket/key?X-Amz-Security-Token={secret}")
                response = connection.getresponse()
                response.read()

        assert secret not in caplog.text
        assert "/bucket/key" in caplog.text

    def test_rejects_chunked_uploads_instead_of_misreading_the_next_request(self) -> None:
        with proxy.s3_server(_resolved_profiles()) as server:
            _wait_for_port(server.port)
            headers = {**_auth_headers(server.profiles["development"]), "Transfer-Encoding": "chunked"}
            connection = http.client.HTTPConnection("127.0.0.1", server.port)
            connection.request("PUT", "/bucket/key", headers=headers)
            response = connection.getresponse()
            assert response.status == 400


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

        def resolve(profile_name, profile) -> proxy.ResolvedAwsProfile:
            resolved.append(profile_name)
            return _profile()

        monkeypatch.setattr(proxy, "resolve_aws_profile", resolve)
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
