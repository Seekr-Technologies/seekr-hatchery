"""Unit tests for the host-side API key proxy."""

import http.client
import logging
import threading
import time
from urllib.parse import urlparse

import pytest

import seekr_hatchery.proxy as proxy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TOKEN = "test-proxy-token"


def _make_api_key_mutator(key: str):
    """Return a header mutator that injects x-api-key."""

    def _mutate(headers, **kwargs):
        out = {k: v for k, v in headers.items() if k.lower() not in ("x-api-key", "authorization")}
        out["x-api-key"] = key
        return out

    return _mutate


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
        _h = headers if headers is not None else {"content-type": "application/json"}
        # Accept either a plain dict or a list of (name, value) pairs.
        if isinstance(_h, dict):
            self.headers = _h
        else:

            class _Headers:
                def __init__(self, items):
                    self._items = items

                def items(self):
                    return self._items

            self.headers = _Headers(_h)
        self._body = body

    def read(self, n: int) -> bytes:
        chunk, self._body = self._body[:n], self._body[n:]
        return chunk

    def drain_conn(self) -> None:
        pass


class _MockPool:
    """Injectable mock for urllib3.PoolManager.

    Pass a list of _MockPoolResp (or plain int status codes) as *responses*;
    each urlopen() call pops one.  Calls are recorded in self.calls.
    """

    def __init__(self, responses=None) -> None:
        self._responses: list = list(responses) if responses else [_MockPoolResp()]
        self.calls: list[dict] = []

    def urlopen(self, method, url, body=None, headers=None, **kwargs):
        self.calls.append({"method": method, "url": url, "body": body, "headers": dict(headers or {})})
        resp = self._responses.pop(0) if self._responses else _MockPoolResp()
        return _MockPoolResp(status=resp) if isinstance(resp, int) else resp


# Keep _MockHTTPSResp for the WebSocket test, which still goes through
# http.client (urllib3 can't handle 101 Switching Protocols).


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
        with proxy.api_server(_make_api_key_mutator("dummy-key"), _TOKEN) as server:
            assert server.port > 0

    def test_pool_uses_truststore_ssl_context(self):
        """The proxy's outbound PoolManager is built with a
        ``truststore.SSLContext`` so TLS verification uses the OS
        native trust store.  Asserts via ``connection_pool_kw`` rather
        than performing a real handshake."""
        import truststore

        with proxy.api_server(_make_api_key_mutator("dummy-key"), _TOKEN) as server:
            handler_pool = server._server.RequestHandlerClass.pool  # type: ignore[attr-defined]
            ssl_ctx = handler_pool.connection_pool_kw.get("ssl_context")
            assert isinstance(ssl_ctx, truststore.SSLContext)

    def test_stops_cleanly(self):
        with proxy.api_server(_make_api_key_mutator("dummy-key"), _TOKEN) as server:
            port = server.port
        # After exiting the context, the port should no longer be accepting connections.
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
        with proxy.api_server(_make_api_key_mutator("real-key"), _TOKEN) as server:
            port = server.port
            _wait_for_port(port)
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models", headers={"x-api-key": "wrong-token"})
            resp = conn.getresponse()
            assert resp.status == 401

    def test_rejects_missing_token(self):
        with proxy.api_server(_make_api_key_mutator("real-key"), _TOKEN) as server:
            port = server.port
            _wait_for_port(port)
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models")
            resp = conn.getresponse()
            assert resp.status == 401

    def test_accepts_correct_token(self):
        pool = _MockPool()
        with proxy.api_server(_make_api_key_mutator("real-key"), _TOKEN, _pool=pool) as server:
            port = server.port
            _wait_for_port(port)
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models", headers={"x-api-key": _TOKEN})
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 200


# ---------------------------------------------------------------------------
# test_proxy_injects_api_key
# ---------------------------------------------------------------------------


class TestProxyInjectsApiKey:
    def test_real_key_injected_on_outbound(self):
        """Proxy must replace the proxy token with the real API key on the outbound leg."""
        pool = _MockPool()
        with proxy.api_server(_make_api_key_mutator("real-api-key-xyz"), _TOKEN, _pool=pool) as server:
            port = server.port
            _wait_for_port(port)
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models", headers={"x-api-key": _TOKEN})
            conn.getresponse().read()

        assert len(pool.calls) == 1
        assert pool.calls[0]["headers"].get("x-api-key") == "real-api-key-xyz"

    def test_inbound_auth_headers_stripped(self):
        """Neither the proxy token nor any authorization header must reach upstream."""
        pool = _MockPool()
        with proxy.api_server(_make_api_key_mutator("real-key"), _TOKEN, _pool=pool) as server:
            port = server.port
            _wait_for_port(port)
            conn = http.client.HTTPConnection("localhost", port)
            conn.request(
                "GET",
                "/v1/models",
                headers={"x-api-key": _TOKEN, "authorization": "Bearer should-be-stripped"},
            )
            conn.getresponse().read()

        h = pool.calls[0]["headers"]
        assert h.get("x-api-key") == "real-key"
        assert "authorization" not in {k.lower() for k in h}


# ---------------------------------------------------------------------------
# test_proxy_openai_format
# ---------------------------------------------------------------------------


class TestProxyOpenAIFormat:
    def test_bearer_token_accepted(self):
        """Proxy must accept Authorization: Bearer <token> in addition to x-api-key."""
        pool = _MockPool()
        with proxy.api_server(
            _make_bearer_mutator("real-key"), _TOKEN, target_host="api.openai.com", _pool=pool
        ) as server:
            port = server.port
            _wait_for_port(port)
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models", headers={"Authorization": f"Bearer {_TOKEN}"})
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 200

    def test_wrong_bearer_rejected(self):
        """Wrong Bearer token must return 401."""
        with proxy.api_server(_make_bearer_mutator("real-key"), _TOKEN, target_host="api.openai.com") as server:
            port = server.port
            _wait_for_port(port)
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models", headers={"Authorization": "Bearer wrong-token"})
            resp = conn.getresponse()
            assert resp.status == 401

    def test_inject_authorization_header(self):
        """Bearer mutator outbound must use Authorization: Bearer <real_key>."""
        pool = _MockPool()
        with proxy.api_server(
            _make_bearer_mutator("my-openai-key"), _TOKEN, target_host="api.openai.com", _pool=pool
        ) as server:
            port = server.port
            _wait_for_port(port)
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models", headers={"Authorization": f"Bearer {_TOKEN}"})
            conn.getresponse().read()

        assert len(pool.calls) == 1
        h = pool.calls[0]["headers"]
        assert h.get("Authorization") == "Bearer my-openai-key"
        assert "x-api-key" not in {k.lower() for k in h}

    def test_openai_target_host_used(self):
        """Proxy must connect to api.openai.com when configured."""
        pool = _MockPool()
        with proxy.api_server(
            _make_bearer_mutator("my-openai-key"), _TOKEN, target_host="api.openai.com", _pool=pool
        ) as server:
            port = server.port
            _wait_for_port(port)
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models", headers={"Authorization": f"Bearer {_TOKEN}"})
            conn.getresponse().read()

        assert len(pool.calls) == 1
        assert urlparse(pool.calls[0]["url"]).hostname == "api.openai.com"

    def test_xapikey_still_accepted_for_anthropic_proxy(self):
        """Default Anthropic proxy still accepts x-api-key tokens."""
        pool = _MockPool()
        with proxy.api_server(_make_api_key_mutator("real-key"), _TOKEN, _pool=pool) as server:
            port = server.port
            _wait_for_port(port)
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models", headers={"x-api-key": _TOKEN})
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 200


class TestPathPrefix:
    def test_path_prefix_prepended_to_forwarded_path(self):
        """Proxy must prepend path_prefix to the path sent to the upstream."""
        pool = _MockPool()
        with proxy.api_server(
            _make_bearer_mutator("api-key"),
            _TOKEN,
            target_host="chatgpt.com",
            path_prefix="/backend-api/codex",
            _pool=pool,
        ) as server:
            port = server.port
            _wait_for_port(port)
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("POST", "/responses", headers={"Authorization": f"Bearer {_TOKEN}"})
            conn.getresponse().read()

        assert len(pool.calls) == 1
        assert pool.calls[0]["url"].endswith("/backend-api/codex/responses")


class TestHeaderSanitization:
    def test_crlf_stripped_from_response_headers(self):
        """Headers containing \\r\\n must be sanitized to prevent HTTP response splitting."""
        malicious_headers = [
            ("content-type", "text/plain"),
            ("x-evil", "value\r\nInjected-Header: pwned"),
            ("x-bad\r\nAnother: oops", "clean-value"),
        ]
        pool = _MockPool(responses=[_MockPoolResp(headers=malicious_headers, body=b"ok")])
        with proxy.api_server(_make_api_key_mutator("real-key"), _TOKEN, _pool=pool) as server:
            port = server.port
            _wait_for_port(port)
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/test", headers={"x-api-key": _TOKEN})
            resp = conn.getresponse()
            resp.read()

            # Verify no header name or value contains CR or LF.
            for name, value in resp.getheaders():
                assert "\r" not in name and "\n" not in name, f"Header name contains CRLF: {name!r}"
                assert "\r" not in value and "\n" not in value, f"Header value contains CRLF: {value!r}"


class TestProxyConcurrentRequests:
    def test_two_concurrent_requests_both_succeed(self):
        """ThreadingMixIn must handle simultaneous requests without blocking."""
        barrier = threading.Barrier(2)

        class _BarrierPool(_MockPool):
            def urlopen(self, method, url, body=None, headers=None, **kwargs):
                # Both threads arrive here, ensuring genuine concurrency.
                barrier.wait(timeout=5)
                return super().urlopen(method, url, body=body, headers=headers, **kwargs)

        pool = _BarrierPool(responses=[_MockPoolResp(), _MockPoolResp()])
        with proxy.api_server(_make_api_key_mutator("api-key"), _TOKEN, _pool=pool) as server:
            port = server.port
            _wait_for_port(port)

            results: list[int] = []

            def _make_request() -> None:
                conn = http.client.HTTPConnection("localhost", port)
                conn.request("GET", "/v1/models", headers={"x-api-key": _TOKEN})
                resp = conn.getresponse()
                resp.read()
                results.append(resp.status)

            t1 = threading.Thread(target=_make_request)
            t2 = threading.Thread(target=_make_request)
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

        assert len(results) == 2
        assert all(s == 200 for s in results)


class TestHeaderMutatorIntegration:
    def test_oauth_beta_header_prepended(self):
        """OAuth mutator must prepend oauth-2025-04-20 to existing anthropic-beta."""
        pool = _MockPool()

        def _oauth_mutate(headers, **kwargs):
            out = {k: v for k, v in headers.items() if k.lower() not in ("x-api-key", "authorization")}
            out["Authorization"] = "Bearer oauth-token"
            existing = out.get("anthropic-beta", "")
            out["anthropic-beta"] = ("oauth-2025-04-20," + existing) if existing else "oauth-2025-04-20"
            return out

        with proxy.api_server(_oauth_mutate, _TOKEN, _pool=pool) as server:
            port = server.port
            _wait_for_port(port)
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("POST", "/v1/messages", headers={"x-api-key": _TOKEN, "anthropic-beta": "existing-beta"})
            conn.getresponse().read()

        assert len(pool.calls) == 1
        h = pool.calls[0]["headers"]
        assert h.get("anthropic-beta") == "oauth-2025-04-20,existing-beta"
        assert h.get("Authorization") == "Bearer oauth-token"
        assert "x-api-key" not in {k.lower() for k in h}


# ---------------------------------------------------------------------------
# test_proxy_reauth_on_401
# ---------------------------------------------------------------------------


class TestProxyReauthOn401:
    def test_upstream_401_triggers_refresh_and_retry(self):
        """When upstream returns 401, proxy must call mutator with refresh=True and retry."""
        refresh_calls: list[bool] = []

        def _mutator(headers, *, refresh: bool = False):
            refresh_calls.append(refresh)
            out = {k: v for k, v in headers.items() if k.lower() not in ("x-api-key", "authorization")}
            out["x-api-key"] = "refreshed-key" if refresh else "original-key"
            return out

        pool = _MockPool(responses=[_MockPoolResp(status=401), _MockPoolResp(status=200)])
        with proxy.api_server(_mutator, _TOKEN, _pool=pool) as server:
            port = server.port
            _wait_for_port(port)
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models", headers={"x-api-key": _TOKEN})
            resp = conn.getresponse()
            resp.read()
            final_status = resp.status

        # Two upstream requests: first got 401, second is the retry.
        assert len(pool.calls) == 2
        # First call used original key, second used refreshed key.
        assert pool.calls[0]["headers"].get("x-api-key") == "original-key"
        assert pool.calls[1]["headers"].get("x-api-key") == "refreshed-key"
        # Mutator was called with refresh=False then refresh=True.
        assert refresh_calls == [False, True]
        # Client sees the 200 from the retry.
        assert final_status == 200

    def test_retry_also_401_forwarded_without_further_retry(self):
        """If the retry also returns 401, it is forwarded to the client with no further attempt."""
        pool = _MockPool(responses=[_MockPoolResp(status=401), _MockPoolResp(status=401)])
        with proxy.api_server(_make_api_key_mutator("key"), _TOKEN, _pool=pool) as server:
            port = server.port
            _wait_for_port(port)
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models", headers={"x-api-key": _TOKEN})
            resp = conn.getresponse()
            resp.read()
            final_status = resp.status

        # Exactly two upstream attempts (original + one retry).
        assert len(pool.calls) == 2
        assert final_status == 401

    def test_non_401_error_forwarded_without_retry(self):
        """A 500 from upstream must be forwarded immediately without any retry."""
        pool = _MockPool(responses=[_MockPoolResp(status=500)])
        with proxy.api_server(_make_api_key_mutator("key"), _TOKEN, _pool=pool) as server:
            port = server.port
            _wait_for_port(port)
            conn = http.client.HTTPConnection("localhost", port)
            conn.request("GET", "/v1/models", headers={"x-api-key": _TOKEN})
            resp = conn.getresponse()
            resp.read()
            final_status = resp.status

        assert len(pool.calls) == 1
        assert final_status == 500


# ---------------------------------------------------------------------------
# test_proxy_websocket_relay
# ---------------------------------------------------------------------------


class TestProxyWebSocketRelay:
    def test_101_forwarded_with_websocket_headers(self, monkeypatch):
        """Proxy must forward 101 with Connection/Upgrade headers intact and relay bytes."""
        import socket as _socket

        # Build a real socket pair so we can test actual byte relay.
        client_sock, proxy_side = _socket.socketpair()

        class _WSResp:
            status = 101
            fp = None  # set after construction

            def getheaders(self):
                return [
                    ("upgrade", "websocket"),
                    ("connection", "Upgrade"),
                    ("sec-websocket-accept", "abc123=="),
                ]

            def read(self, n=None):
                return b""

        class _WSConn:
            def __init__(self, host, timeout=None):
                self.host = host
                # upstream_sock is the other end of a second socketpair
                self._upstream, self._downstream = _socket.socketpair()
                self.sock = self._upstream

            def request(self, method, path, body=None, headers=None):
                resp = _WSResp()
                # Give resp.fp a reader that reads from downstream end

                resp.fp = self._downstream.makefile("rb")
                self._resp = resp

            def getresponse(self):
                return self._resp

            def close(self):
                # Only close if sock is still ours (relay detaches it)
                if self.sock is not None:
                    self.sock.close()

        monkeypatch.setattr(http.client, "HTTPSConnection", _WSConn)
        with proxy.api_server(_make_bearer_mutator("real-key"), _TOKEN) as server:
            port = server.port
            _wait_for_port(port)
            conn = http.client.HTTPConnection("localhost", port)
            conn.request(
                "GET",
                "/ws",
                headers={
                    "Authorization": f"Bearer {_TOKEN}",
                    "Upgrade": "websocket",
                    "Connection": "Upgrade",
                    "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
                    "Sec-WebSocket-Version": "13",
                },
            )
            resp = conn.getresponse()
            # Proxy must return 101 with the WebSocket headers
            assert resp.status == 101
            headers_lower = {k.lower(): v for k, v in resp.getheaders()}
            assert headers_lower.get("upgrade", "").lower() == "websocket"
            assert headers_lower.get("sec-websocket-accept") == "abc123=="


# ---------------------------------------------------------------------------
# test_proxy_logging
# ---------------------------------------------------------------------------


class TestProxyLogging:
    """Proxy logs requests, responses, and errors at appropriate levels."""

    def test_401_rejection_logged_at_info(self, caplog):
        """Token mismatch emits an INFO log with method and path."""

        with caplog.at_level(logging.INFO, logger="seekr_hatchery"):
            with proxy.api_server(_make_api_key_mutator("real-key"), _TOKEN) as server:
                port = server.port
                _wait_for_port(port)
                conn = http.client.HTTPConnection("localhost", port)
                conn.request("GET", "/v1/models", headers={"x-api-key": "wrong-token"})
                conn.getresponse().read()

        messages = [r.getMessage() for r in caplog.records]
        assert any("401" in m and "rejected" in m for m in messages)

    def test_request_and_response_logged_at_info(self, caplog):
        """A successful proxied request emits INFO logs for request and response."""

        pool = _MockPool()
        with caplog.at_level(logging.INFO, logger="seekr_hatchery"):
            with proxy.api_server(_make_api_key_mutator("real-key"), _TOKEN, _pool=pool) as server:
                port = server.port
                _wait_for_port(port)
                conn = http.client.HTTPConnection("localhost", port)
                conn.request("GET", "/v1/models", headers={"x-api-key": _TOKEN})
                conn.getresponse().read()

        messages = [r.getMessage() for r in caplog.records]
        # Request log
        assert any("proxy:" in m and "/v1/models" in m and "→" in m for m in messages)
        # Response log (200)
        assert any("proxy:" in m and "200" in m for m in messages)

    def test_upstream_error_logged_at_warning(self, caplog):
        """An upstream connection error emits a WARNING log."""

        # Pool that raises on urlopen
        class _ErrorPool:
            def urlopen(self, *a, **kw):
                raise ConnectionError("upstream unreachable")

        with caplog.at_level(logging.WARNING, logger="seekr_hatchery"):
            with proxy.api_server(_make_api_key_mutator("real-key"), _TOKEN, _pool=_ErrorPool()) as server:
                port = server.port
                _wait_for_port(port)
                conn = http.client.HTTPConnection("localhost", port)
                conn.request("GET", "/v1/models", headers={"x-api-key": _TOKEN})
                conn.getresponse().read()

        messages = [r.getMessage() for r in caplog.records]
        assert any("upstream error" in m for m in messages)
