"""Unit tests for the host-side TLS reverse proxy."""

import http.client
import ssl
import time
from pathlib import Path

from cryptography import x509

import seekr_hatchery.tls_proxy as tls_proxy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TOKEN = "test-tls-proxy-token"


def _make_bearer_mutator(key: str):
    """Return a header mutator that injects Authorization: Bearer."""

    def _mutate(headers, **kwargs):
        out = {k: v for k, v in headers.items() if k.lower() not in ("x-api-key", "authorization")}
        out["Authorization"] = f"Bearer {key}"
        return out

    return _mutate


class _MockPoolResp:
    """Mock urllib3 response returned by _MockPool.urlopen()."""

    def __init__(self, status: int = 200, headers=None, body: bytes = b"{}") -> None:
        self.status = status
        self.headers = headers if headers is not None else {"content-type": "application/json"}
        self._body = body

    def read(self, n: int) -> bytes:
        chunk, self._body = self._body[:n], self._body[n:]
        return chunk

    def drain_conn(self) -> None:
        pass


class _MockPool:
    """Injectable mock for urllib3.PoolManager."""

    def __init__(self, responses=None) -> None:
        self._responses: list = list(responses) if responses else [_MockPoolResp()]
        self.calls: list[dict] = []

    def urlopen(self, method, url, body=None, headers=None, **kwargs):
        self.calls.append({"method": method, "url": url, "body": body, "headers": dict(headers or {})})
        resp = self._responses.pop(0) if self._responses else _MockPoolResp()
        return _MockPoolResp(status=resp) if isinstance(resp, int) else resp


def _wait_for_port(port: int, ca_file: Path, timeout: float = 2.0) -> None:
    """Poll until the TLS proxy is accepting connections."""
    deadline = time.monotonic() + timeout
    ssl_context = ssl.create_default_context(cafile=str(ca_file))
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    while time.monotonic() < deadline:
        try:
            conn = http.client.HTTPSConnection("localhost", port, context=ssl_context)
            conn.connect()
            conn.close()
            return
        except OSError:
            time.sleep(0.01)
    raise TimeoutError(f"TLS Proxy did not come up on port {port}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCertificateGeneration:
    def test_ca_generation(self):
        ca_cert_pem, ca_key_pem = tls_proxy.generate_ca()
        assert ca_cert_pem.startswith(b"-----BEGIN CERTIFICATE-----")
        assert ca_key_pem.startswith(b"-----BEGIN RSA PRIVATE KEY-----")

        # Parse certificate to ensure validity
        cert = x509.load_pem_x509_certificate(ca_cert_pem)
        rfc_str = cert.subject.rfc4514_string()
        assert "CN=Seekr Hatchery Ephemeral CA" in rfc_str
        assert "O=Seekr Technologies" in rfc_str

    def test_leaf_generation(self):
        ca_cert_pem, ca_key_pem = tls_proxy.generate_ca()
        hostname = "daily-cloudcode-pa.googleapis.com"
        leaf_cert_pem, leaf_key_pem = tls_proxy.generate_leaf_cert(hostname, ca_cert_pem, ca_key_pem)

        assert leaf_cert_pem.startswith(b"-----BEGIN CERTIFICATE-----")
        assert leaf_key_pem.startswith(b"-----BEGIN RSA PRIVATE KEY-----")

        cert = x509.load_pem_x509_certificate(leaf_cert_pem)
        assert cert.subject.rfc4514_string() == f"CN={hostname}"


class TestTLSProxyEndToEnd:
    def test_starts_and_stops_and_authenticates(self, tmp_path):
        ca_cert_pem, ca_key_pem = tls_proxy.generate_ca()
        hostname = "daily-cloudcode-pa.googleapis.com"
        leaf_cert_pem, leaf_key_pem = tls_proxy.generate_leaf_cert(hostname, ca_cert_pem, ca_key_pem)

        ca_file = tmp_path / "ca.crt"
        leaf_file = tmp_path / "leaf.crt"
        key_file = tmp_path / "leaf.key"

        ca_file.write_bytes(ca_cert_pem)
        leaf_file.write_bytes(leaf_cert_pem)
        key_file.write_bytes(leaf_key_pem)

        pool = _MockPool(responses=[_MockPoolResp(status=200, body=b"Hello from Google mock")])

        with tls_proxy.tls_api_server(
            _make_bearer_mutator("real-google-token"),
            _TOKEN,
            leaf_file,
            key_file,
            target_host=hostname,
            _pool=pool,
        ) as server:
            port = server.port
            assert port > 0
            _wait_for_port(port, ca_file)

            # Connect using HTTPS and verify certificate signature against our CA
            ssl_context = ssl.create_default_context(cafile=str(ca_file))
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_REQUIRED
            conn = http.client.HTTPSConnection(
                "localhost",
                port,
                context=ssl_context,
            )
            conn.request(
                "POST",
                "/v1/models",
                body=b"payload",
                headers={"Authorization": f"Bearer {_TOKEN}"},
            )
            resp = conn.getresponse()
            assert resp.status == 200
            assert resp.read() == b"Hello from Google mock"

        # Verify proxy mutation logic ran
        assert len(pool.calls) == 1
        call = pool.calls[0]
        assert call["method"] == "POST"
        assert call["headers"].get("Authorization") == "Bearer real-google-token"
        assert "x-api-key" not in {k.lower() for k in call["headers"]}
