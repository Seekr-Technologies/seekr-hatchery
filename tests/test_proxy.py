"""Unit tests for the host-side API key proxy."""

import http.client
import threading
import time

import pytest

import seekr_hatchery.proxy as proxy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TOKEN = "test-proxy-token"


class _MockHTTPSConn:
    """Minimal stand-in for http.client.HTTPSConnection that records requests."""

    def __init__(self, host: str) -> None:
        self.host = host
        self._recorded: dict = {}

    def request(self, method: str, path: str, body=None, headers=None) -> None:
        self._recorded = {"method": method, "path": path, "headers": dict(headers or {})}

    def getresponse(self) -> "_MockHTTPSResp":
        return _MockHTTPSResp()

    def close(self) -> None:
        pass


class _MockHTTPSResp:
    status = 200
    _data = b"{}"

    def getheaders(self) -> list[tuple[str, str]]:
        return [("content-type", "application/json"), ("content-length", str(len(self._data)))]

    def read(self, n: int) -> bytes:
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk


def _wait_for_port(port: int, timeout: float = 2.0) -> None:
    """Poll until the proxy is accepting connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            http.client.HTTPConnection("localhost", port).connect()
            return
        except OSError:
            time.sleep(0.01)
    raise TimeoutError(f"Proxy did not come up on port {port}")


# ---------------------------------------------------------------------------
# test_proxy_starts_and_stops
# ---------------------------------------------------------------------------


class TestProxyStartsAndStops:
    def test_port_is_positive(self):
        server, _ = proxy.start_proxy("dummy-key", _TOKEN)
        try:
            assert server.server_address[1] > 0
        finally:
            proxy.stop_proxy(server)

    def test_returns_same_token(self):
        server, token = proxy.start_proxy("dummy-key", _TOKEN)
        try:
            assert token == _TOKEN
        finally:
            proxy.stop_proxy(server)

    def test_stops_cleanly(self):
        server, _ = proxy.start_proxy("dummy-key", _TOKEN)
        proxy.stop_proxy(server)
        # After stop, the port should no longer be accepting connections.
        port = server.server_address[1]
        try:
            c = http.client.HTTPConnection("localhost", port, timeout=0.5)
            c.request("GET", "/")
            c.getresponse()
            pytest.fail("Expected connection refused after stop")
        except OSError:
            pass  # expected


# ---------------------------------------------------------------------------
# test_proxy_token_validation
# ---------------------------------------------------------------------------


class TestProxyTokenValidation:
    def test_rejects_wrong_token(self):
        server, _ = proxy.start_proxy("real-key", _TOKEN)
        port = server.server_address[1]
        _wait_for_port(port)
        try:
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models", headers={"x-api-key": "wrong-token"})
            resp = conn.getresponse()
            assert resp.status == 401
        finally:
            proxy.stop_proxy(server)

    def test_rejects_missing_token(self):
        server, _ = proxy.start_proxy("real-key", _TOKEN)
        port = server.server_address[1]
        _wait_for_port(port)
        try:
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models")
            resp = conn.getresponse()
            assert resp.status == 401
        finally:
            proxy.stop_proxy(server)

    def test_accepts_correct_token(self, monkeypatch):
        monkeypatch.setattr(http.client, "HTTPSConnection", _MockHTTPSConn)
        server, _ = proxy.start_proxy("real-key", _TOKEN)
        port = server.server_address[1]
        _wait_for_port(port)
        try:
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models", headers={"x-api-key": _TOKEN})
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 200
        finally:
            proxy.stop_proxy(server)


# ---------------------------------------------------------------------------
# test_proxy_injects_api_key
# ---------------------------------------------------------------------------


class TestProxyInjectsApiKey:
    def test_real_key_injected_on_outbound(self, monkeypatch):
        """Proxy must replace the proxy token with the real API key on the outbound leg."""
        recorded: list[dict] = []

        class _CapturingConn(_MockHTTPSConn):
            def request(self, method, path, body=None, headers=None):
                recorded.append({"method": method, "headers": dict(headers or {})})

        monkeypatch.setattr(http.client, "HTTPSConnection", _CapturingConn)

        server, _ = proxy.start_proxy("real-api-key-xyz", _TOKEN)
        port = server.server_address[1]
        _wait_for_port(port)

        try:
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models", headers={"x-api-key": _TOKEN})
            resp = conn.getresponse()
            resp.read()
        finally:
            proxy.stop_proxy(server)

        assert len(recorded) == 1
        assert recorded[0]["headers"].get("x-api-key") == "real-api-key-xyz"

    def test_inbound_auth_headers_stripped(self, monkeypatch):
        """Neither the proxy token nor any authorization header must reach upstream."""
        recorded: list[dict] = []

        class _CapturingConn(_MockHTTPSConn):
            def request(self, method, path, body=None, headers=None):
                recorded.append({"headers": dict(headers or {})})

        monkeypatch.setattr(http.client, "HTTPSConnection", _CapturingConn)

        server, _ = proxy.start_proxy("real-key", _TOKEN)
        port = server.server_address[1]
        _wait_for_port(port)

        try:
            conn = http.client.HTTPConnection("localhost", port)
            conn.request(
                "GET",
                "/v1/models",
                headers={"x-api-key": _TOKEN, "authorization": "Bearer should-be-stripped"},
            )
            resp = conn.getresponse()
            resp.read()
        finally:
            proxy.stop_proxy(server)

        h = recorded[0]["headers"]
        assert h.get("x-api-key") == "real-key"
        assert "authorization" not in {k.lower() for k in h}


# ---------------------------------------------------------------------------
# test_proxy_concurrent_requests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# test_proxy_openai_format
# ---------------------------------------------------------------------------


class TestProxyOpenAIFormat:
    def test_bearer_token_accepted(self, monkeypatch):
        """Proxy must accept Authorization: Bearer <token> in addition to x-api-key."""
        monkeypatch.setattr(http.client, "HTTPSConnection", _MockHTTPSConn)
        server, _ = proxy.start_proxy("real-key", _TOKEN, target_host="api.openai.com", inject_header="authorization")
        port = server.server_address[1]
        _wait_for_port(port)
        try:
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models", headers={"Authorization": f"Bearer {_TOKEN}"})
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 200
        finally:
            proxy.stop_proxy(server)

    def test_wrong_bearer_rejected(self):
        """Wrong Bearer token must return 401."""
        server, _ = proxy.start_proxy("real-key", _TOKEN, target_host="api.openai.com", inject_header="authorization")
        port = server.server_address[1]
        _wait_for_port(port)
        try:
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models", headers={"Authorization": "Bearer wrong-token"})
            resp = conn.getresponse()
            assert resp.status == 401
        finally:
            proxy.stop_proxy(server)

    def test_inject_authorization_header(self, monkeypatch):
        """When inject_header='authorization', outbound must use Authorization: Bearer <real_key>."""
        recorded: list[dict] = []

        class _CapturingConn:
            def __init__(self, host: str) -> None:
                self.host = host

            def request(self, method, path, body=None, headers=None):
                recorded.append({"headers": dict(headers or {})})

            def getresponse(self):
                return _MockHTTPSResp()

            def close(self):
                pass

        monkeypatch.setattr(http.client, "HTTPSConnection", _CapturingConn)
        server, _ = proxy.start_proxy(
            "my-openai-key", _TOKEN, target_host="api.openai.com", inject_header="authorization"
        )
        port = server.server_address[1]
        _wait_for_port(port)

        try:
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models", headers={"Authorization": f"Bearer {_TOKEN}"})
            resp = conn.getresponse()
            resp.read()
        finally:
            proxy.stop_proxy(server)

        assert len(recorded) == 1
        h = recorded[0]["headers"]
        assert h.get("Authorization") == "Bearer my-openai-key"
        assert "x-api-key" not in {k.lower() for k in h}

    def test_openai_target_host_used(self, monkeypatch):
        """Proxy must connect to api.openai.com when configured."""
        connected_to: list[str] = []

        class _RecordingConn:
            def __init__(self, host: str) -> None:
                connected_to.append(host)

            def request(self, method, path, body=None, headers=None):
                pass

            def getresponse(self):
                return _MockHTTPSResp()

            def close(self):
                pass

        monkeypatch.setattr(http.client, "HTTPSConnection", _RecordingConn)
        server, _ = proxy.start_proxy(
            "my-openai-key", _TOKEN, target_host="api.openai.com", inject_header="authorization"
        )
        port = server.server_address[1]
        _wait_for_port(port)

        try:
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models", headers={"Authorization": f"Bearer {_TOKEN}"})
            resp = conn.getresponse()
            resp.read()
        finally:
            proxy.stop_proxy(server)

        assert connected_to == ["api.openai.com"]

    def test_xapikey_still_accepted_for_anthropic_proxy(self, monkeypatch):
        """Default Anthropic proxy still accepts x-api-key tokens."""
        monkeypatch.setattr(http.client, "HTTPSConnection", _MockHTTPSConn)
        server, _ = proxy.start_proxy("real-key", _TOKEN)
        port = server.server_address[1]
        _wait_for_port(port)
        try:
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models", headers={"x-api-key": _TOKEN})
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 200
        finally:
            proxy.stop_proxy(server)


class TestPathPrefix:
    def test_path_prefix_prepended_to_forwarded_path(self, monkeypatch):
        """Proxy must prepend path_prefix to the path sent to the upstream."""
        recorded: list[dict] = []

        class _CapturingConn(_MockHTTPSConn):
            def request(self, method, path, body=None, headers=None):
                recorded.append({"path": path})

        monkeypatch.setattr(http.client, "HTTPSConnection", _CapturingConn)
        server, _ = proxy.start_proxy(
            "api-key",
            _TOKEN,
            target_host="chatgpt.com",
            inject_header="authorization",
            path_prefix="/backend-api/codex",
        )
        port = server.server_address[1]
        _wait_for_port(port)

        try:
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("POST", "/responses", headers={"Authorization": f"Bearer {_TOKEN}"})
            resp = conn.getresponse()
            resp.read()
        finally:
            proxy.stop_proxy(server)

        assert len(recorded) == 1
        assert recorded[0]["path"] == "/backend-api/codex/responses"


class TestHeaderSanitization:
    def test_crlf_stripped_from_response_headers(self, monkeypatch):
        """Headers containing \\r\\n must be sanitized to prevent HTTP response splitting."""

        class _MaliciousResp:
            status = 200
            _data = b"ok"

            def getheaders(self):
                return [
                    ("content-type", "text/plain"),
                    ("x-evil", "value\r\nInjected-Header: pwned"),
                    ("x-bad\r\nAnother: oops", "clean-value"),
                ]

            def read(self, n):
                chunk, self._data = self._data[:n], self._data[n:]
                return chunk

        class _MaliciousConn(_MockHTTPSConn):
            def getresponse(self):
                return _MaliciousResp()

        monkeypatch.setattr(http.client, "HTTPSConnection", _MaliciousConn)
        server, _ = proxy.start_proxy("real-key", _TOKEN)
        port = server.server_address[1]
        _wait_for_port(port)

        try:
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/test", headers={"x-api-key": _TOKEN})
            resp = conn.getresponse()
            resp.read()

            # Verify no header name or value contains CR or LF.
            for name, value in resp.getheaders():
                assert "\r" not in name and "\n" not in name, f"Header name contains CRLF: {name!r}"
                assert "\r" not in value and "\n" not in value, f"Header value contains CRLF: {value!r}"
        finally:
            proxy.stop_proxy(server)


class TestProxyConcurrentRequests:
    def test_two_concurrent_requests_both_succeed(self, monkeypatch):
        """ThreadingMixIn must handle simultaneous requests without blocking."""
        barrier = threading.Barrier(2)

        class _SlowConn(_MockHTTPSConn):
            def request(self, method, path, body=None, headers=None):
                # Both threads arrive here, ensuring genuine concurrency.
                barrier.wait(timeout=5)

        monkeypatch.setattr(http.client, "HTTPSConnection", _SlowConn)

        server, _ = proxy.start_proxy("api-key", _TOKEN)
        port = server.server_address[1]
        _wait_for_port(port)

        results: list[int] = []

        def _make_request() -> None:
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models", headers={"x-api-key": _TOKEN})
            resp = conn.getresponse()
            resp.read()
            results.append(resp.status)

        try:
            t1 = threading.Thread(target=_make_request)
            t2 = threading.Thread(target=_make_request)
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)
        finally:
            proxy.stop_proxy(server)

        assert len(results) == 2
        assert all(s == 200 for s in results)
