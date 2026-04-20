"""Host-side HTTP reverse proxy for API key injection.

The container is given ``ANTHROPIC_BASE_URL`` (or ``OPENAI_BASE_URL``) pointing
to this proxy.  All outbound AI API calls from inside the container pass through
here; the proxy validates the inbound proxy token, strips auth credentials the
container sends, injects the real API key, and forwards to the target host.

The real API key never enters the container.

The proxy validates the inbound token using one of two formats:
  - ``x-api-key: <token>``       — Anthropic style
  - ``Authorization: Bearer <token>`` — OpenAI style

Requests with a wrong or missing token are rejected with 401.
This prevents concurrent container sessions from routing through each other's
proxy (all containers can reach ``host.docker.internal``).

Public interface::

    server, proxy_token = start_proxy(header_mutator, proxy_token)
    # ... run container ...
    stop_proxy(server)
"""

import http.client
import http.server
import logging
import socketserver
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("hatchery")

_CHUNK_SIZE = 8192

# Hop-by-hop headers that must not be forwarded between proxy and client/server.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
    }
)


def _sanitize_header(value: str) -> str:
    """Strip CR/LF to prevent HTTP response splitting."""
    return value.replace("\r", "").replace("\n", "")


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    """Validates the proxy token, strips inbound auth, injects the real API key, and forwards."""

    # HTTP/1.1 is required for WebSocket upgrades (RFC 6455 §4.1) and for
    # correct chunked-transfer / keep-alive handling.  Python's BaseHTTPRequestHandler
    # defaults to HTTP/1.0; override it here.
    protocol_version = "HTTP/1.1"

    # Overridden per-server-instance by start_proxy via a fresh subclass.
    # Must be a staticmethod to prevent Python's descriptor protocol from
    # injecting `self` as the first argument when called as self.header_mutator().
    header_mutator = staticmethod(lambda h: h)
    proxy_token: str = ""
    target_host: str = "api.anthropic.com"
    path_prefix: str = ""  # prepended to every forwarded path (e.g. "/backend-api/codex")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # suppress per-request access log lines

    def _send_simple(self, code: int, message: str) -> None:
        body = message.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _validate_token(self) -> bool:
        """Return True if the inbound request carries the correct proxy token.

        Accepts either ``x-api-key: <token>`` (Anthropic style) or
        ``Authorization: Bearer <token>`` (OpenAI style).
        """
        # x-api-key style
        if self.headers.get("x-api-key", "") == self.proxy_token:
            return True
        # Authorization: Bearer <token> style
        auth = self.headers.get("authorization", "")
        if auth.startswith("Bearer ") and auth[len("Bearer ") :] == self.proxy_token:
            return True
        return False

    # Headers that are normally hop-by-hop but are REQUIRED for a WebSocket
    # upgrade response to reach the client (RFC 6455 §4.1).
    _WS_KEEP: frozenset[str] = frozenset(
        {"connection", "upgrade", "sec-websocket-accept", "sec-websocket-protocol", "sec-websocket-extensions"}
    )

    def _relay_websocket(self, upstream_reader, upstream_sock) -> None:
        """Relay WebSocket frames bidirectionally after a 101 upgrade.

        upstream_reader — BufferedReader wrapping the upstream SSL socket;
            may already hold bytes read-ahead from the 101 response.
        upstream_sock   — the underlying SSL socket; used for writes to upstream.
        """
        self.wfile.flush()

        def upstream_to_client() -> None:
            try:
                while True:
                    chunk = upstream_reader.read1(_CHUNK_SIZE)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception:
                pass

        relay_thread = threading.Thread(target=upstream_to_client, daemon=True)
        relay_thread.start()
        try:
            while True:
                chunk = self.rfile.read1(_CHUNK_SIZE)
                if not chunk:
                    break
                upstream_sock.sendall(chunk)
        except Exception:
            pass
        finally:
            relay_thread.join(timeout=30)

    def _handle_request(self, *, _retried: bool = False) -> None:
        # HTTP/1.1 defaults to keep-alive; force close after each request so
        # clients don't wait for a second request that never comes.  WebSocket
        # relay sets this implicitly by returning after the relay completes.
        self.close_connection = True

        # ── 0. Validate proxy token ───────────────────────────────────────────
        # Reject requests whose token doesn't match the stable per-task proxy
        # token.  This prevents other containers (which share the host gateway
        # and can discover open ports) from routing through this proxy.
        if not self._validate_token():
            self._send_simple(401, "Unauthorized")
            return

        # ── 1. Build outgoing headers ─────────────────────────────────────────
        # Strip hop-by-hop headers; delegate auth stripping + key injection to
        # the backend mutator.
        pre_auth_headers: dict[str, str] = {k: v for k, v in self.headers.items() if k.lower() not in _HOP_BY_HOP}

        # Backend owns all auth header logic.
        out_headers = self.header_mutator(pre_auth_headers)

        # Always set host (routing concern owned by proxy).
        out_headers["host"] = self.target_host

        # Preserve Connection/Upgrade on the outbound leg for WebSocket
        # handshakes so the upstream actually performs the upgrade.
        if self.headers.get("upgrade", "").lower() == "websocket":
            out_headers["Connection"] = "Upgrade"
            out_headers["Upgrade"] = "websocket"

        # ── 2. Read request body ──────────────────────────────────────────────
        content_length = int(self.headers.get("content-length", 0) or 0)
        body = self.rfile.read(content_length) if content_length else None

        # ── 3. Forward to target host ─────────────────────────────────────────
        conn = http.client.HTTPSConnection(self.target_host)
        try:
            conn.request(self.command, self.path_prefix + self.path, body=body, headers=out_headers)
            resp = conn.getresponse()

            # ── 3a. 101 Switching Protocols — WebSocket relay ─────────────────
            # Forward the upgrade response (including hop-by-hop headers required
            # by RFC 6455) then relay raw bytes bidirectionally.
            if resp.status == 101:
                logger.debug("proxy: WebSocket upgrade to %s, starting relay", self.target_host)
                self.send_response(101)
                for key, value in resp.getheaders():
                    if key.lower() in _HOP_BY_HOP and key.lower() not in self._WS_KEEP:
                        continue
                    self.send_header(_sanitize_header(key), _sanitize_header(value))
                self.end_headers()
                # Detach conn.sock before conn.close() so the relay keeps the
                # SSL socket open.  resp.fp is the BufferedReader wrapping the
                # same socket and may already hold read-ahead bytes.
                upstream_sock = conn.sock
                conn.sock = None
                self._relay_websocket(resp.fp, upstream_sock)
                return

            # ── 3b. 401 retry — refresh credentials once ──────────────────────
            if resp.status == 401 and not _retried:
                # Drain and discard the 401 body (always small).
                resp.read()
                conn.close()
                # Ask the backend to refresh credentials, then retry.
                refreshed_headers = self.header_mutator(pre_auth_headers, refresh=True)
                refreshed_headers["host"] = self.target_host
                conn = http.client.HTTPSConnection(self.target_host)
                conn.request(self.command, self.path_prefix + self.path, body=body, headers=refreshed_headers)
                resp = conn.getresponse()

            # Forward status + headers (strip hop-by-hop; http.client decodes
            # chunked encoding for us so transfer-encoding is no longer accurate).
            self.send_response(resp.status)
            for key, value in resp.getheaders():
                if key.lower() in _HOP_BY_HOP:
                    continue
                self.send_header(_sanitize_header(key), _sanitize_header(value))
            self.end_headers()

            # Stream response body in chunks (handles SSE correctly).
            while True:
                chunk = resp.read(_CHUNK_SIZE)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()

        except Exception as exc:
            logger.debug("proxy: upstream error: %s", exc)
            try:
                self._send_simple(502, f"Bad Gateway: {exc}")
            except Exception:
                pass  # response headers may already be sent
        finally:
            conn.close()

    def do_GET(self) -> None:  # noqa: N802
        self._handle_request()

    def do_POST(self) -> None:  # noqa: N802
        self._handle_request()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle_request()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle_request()

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle_request()

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle_request()


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """HTTP server that handles each request in a separate thread."""

    daemon_threads = True


def start_proxy(
    header_mutator: Callable[..., dict[str, str]],
    proxy_token: str,
    target_host: str = "api.anthropic.com",
    path_prefix: str = "",
) -> tuple[http.server.HTTPServer, str]:
    """Start the reverse proxy and return ``(server, proxy_token)``.

    The server binds to ``0.0.0.0:0`` so the OS picks an ephemeral port.
    Inbound requests must present *proxy_token* as ``x-api-key`` or
    ``Authorization: Bearer <token>``; others are rejected with 401.
    This isolates concurrent proxy instances so that containers from different
    sessions cannot route through each other's proxy.
    The real credentials never leave the host process.

    Args:
        header_mutator: Callable that transforms outbound request headers.
            Called for every proxied request with the inbound headers (hop-by-hop
            already stripped). Must strip inbound auth headers, inject the real
            API key in the correct format, and return the modified dict.
        proxy_token: The stable per-task token the container uses.
        target_host: Upstream API hostname (default: ``api.anthropic.com``).
            Use ``"api.openai.com"`` for OpenAI/codex.
        path_prefix: String prepended to every forwarded path (default: ``""``).
            Use ``"/backend-api/codex"`` for ChatGPT OAuth mode.
    """

    # Bind all settings to a fresh handler class so multiple concurrent proxy
    # instances don't share state.
    class _BoundHandler(_ProxyHandler):
        pass

    _BoundHandler.header_mutator = staticmethod(header_mutator)
    _BoundHandler.proxy_token = proxy_token
    _BoundHandler.target_host = target_host
    _BoundHandler.path_prefix = path_prefix

    server = _ThreadingHTTPServer(("0.0.0.0", 0), _BoundHandler)
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    logger.debug(
        "Proxy started on port %d (target=%s prefix=%r)",
        port,
        target_host,
        path_prefix,
    )
    return server, proxy_token


def stop_proxy(server: http.server.HTTPServer) -> None:
    """Gracefully shut down the proxy server."""
    server.shutdown()
    server.server_close()
    logger.debug("Proxy stopped")
