"""OCI Object Storage HTTP proxy transport contracts."""

from __future__ import annotations

import http.client
import io
import socket
from pathlib import Path

from seekr_hatchery.sidecars.oci_sidecar import OciConfig, credentials, proxy


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


def _auth_header(profile: proxy.ProxyProfile) -> str:
    return (
        'Signature algorithm="rsa-sha256",headers="date (request-target) host",'
        f'keyId="{profile.synthetic.key_id}",signature="ignored",version="1"'
    )


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
        monkeypatch.setattr(credentials, "sign_request", lambda method, target, headers, profile: headers)

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
        monkeypatch.setattr(credentials, "sign_request", lambda method, target, headers, profile: headers)

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

    def test_reports_signing_phase_without_leaking_credentials(
        self,
        tmp_path: Path,
        monkeypatch,
        caplog,
    ) -> None:
        resolved = _resolved_profile(tmp_path)
        monkeypatch.setattr(
            credentials,
            "sign_request",
            lambda *args: (_ for _ in ()).throw(RuntimeError("missing body header token=secret-value")),
        )
        caplog.set_level("WARNING", logger=proxy.__name__)

        with proxy.oci_server({"DEFAULT": (resolved, None)}, _pool=_Pool()) as server:
            connection = http.client.HTTPConnection("127.0.0.1", server.port)
            connection.request(
                "GET",
                "/n/my-ns/b/artifacts/o/file.txt",
                headers={"Authorization": _auth_header(server.profiles["DEFAULT"])},
            )
            response = connection.getresponse()
            assert (response.status, response.read()) == (
                502,
                b'{"error":"OCI upstream signing failed"}',
            )
            connection.close()

        assert "phase=sign" in caplog.text
        assert "RuntimeError" in caplog.text
        assert "token=<redacted>" in caplog.text
        assert "secret-value" not in caplog.text

    def test_reports_upstream_phase_with_body_progress_and_redacted_query(
        self,
        tmp_path: Path,
        monkeypatch,
        caplog,
    ) -> None:
        class _FailingPool:
            def urlopen(self, *args, **kwargs):
                body = kwargs["body"]
                body.read()
                raise RuntimeError(
                    "failed https://objectstorage.example/object?token=secret-value Authorization=hidden"
                )

            def clear(self) -> None:
                pass

        resolved = _resolved_profile(tmp_path)
        monkeypatch.setattr(
            credentials,
            "sign_request",
            lambda method, target, headers, profile: headers,
        )
        caplog.set_level("WARNING", logger=proxy.__name__)

        with proxy.oci_server({"DEFAULT": (resolved, None)}, _pool=_FailingPool()) as server:
            connection = http.client.HTTPConnection("127.0.0.1", server.port)
            connection.request(
                "PUT",
                "/n/my-ns/b/artifacts/o/temp/file.txt",
                body=b"test",
                headers={
                    "Authorization": _auth_header(server.profiles["DEFAULT"]),
                    "Content-Type": "application/octet-stream",
                    "x-content-sha256": "synthetic-hash",
                },
            )
            response = connection.getresponse()
            assert (response.status, response.read()) == (
                502,
                b'{"error":"OCI upstream request failed"}',
            )
            connection.close()

        assert "phase=upstream" in caplog.text
        assert "body_bytes=4/4" in caplog.text
        assert "https://objectstorage.example/object?<redacted>" in caplog.text
        assert "authorization=<redacted>" in caplog.text.lower()
        assert "secret-value" not in caplog.text
        assert "hidden" not in caplog.text

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
