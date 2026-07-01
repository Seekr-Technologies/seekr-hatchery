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

    with api_server(header_mutator, proxy_token) as server:
        port = server.port
        # ... run container ...
"""

import http.client
import http.server
import logging
import socketserver
import ssl
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any

import truststore
import urllib3

# Patch Python's ssl module to use the OS trust store (macOS Keychain, Linux
# /etc/ssl/certs, Windows cert store).  Must run before any SSL connections are
# made.  Using inject_into_ssl() rather than truststore.SSLContext() preserves
# check_hostname=True so urllib3 does not emit InsecureRequestWarning.
truststore.inject_into_ssl()

_UPSTREAM_TIMEOUT = urllib3.Timeout(connect=10, read=60)

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
    target_scheme: str = "https"  # "http" for plain-HTTP internal endpoints
    path_prefix: str = ""  # prepended to every forwarded path (e.g. "/backend-api/codex")
    pool: urllib3.PoolManager  # set per-instance by api_server()

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
        # WebSocket upgrades need raw socket access for bidirectional relay;
        # urllib3 does not support 101, so keep http.client for that path only.
        if self.headers.get("upgrade", "").lower() == "websocket":
            conn = (
                http.client.HTTPSConnection(self.target_host, timeout=60)
                if self.target_scheme == "https"
                else http.client.HTTPConnection(self.target_host, timeout=60)
            )
            try:
                conn.request(self.command, self.path_prefix + self.path, body=body, headers=out_headers)
                resp = conn.getresponse()
                if resp.status == 101:
                    logger.debug("proxy: WebSocket upgrade to %s, starting relay", self.target_host)
                    self.send_response(101)
                    for key, value in resp.getheaders():
                        if key.lower() in _HOP_BY_HOP and key.lower() not in self._WS_KEEP:
                            continue
                        self.send_header(_sanitize_header(key), _sanitize_header(value))
                    self.end_headers()
                    upstream_sock = conn.sock
                    conn.sock = None
                    self._relay_websocket(resp.fp, upstream_sock)
                    return
                # Upgrade refused — forward the error response.
                self.send_response(resp.status)
                for key, value in resp.getheaders():
                    if key.lower() in _HOP_BY_HOP:
                        continue
                    self.send_header(_sanitize_header(key), _sanitize_header(value))
                self.end_headers()
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
                    pass
            finally:
                conn.close()
            return

        # Normal request — use the shared connection pool.
        url = f"{self.target_scheme}://{self.target_host}{self.path_prefix}{self.path}"
        try:
            resp = self.pool.urlopen(
                self.command,
                url,
                body=body,
                headers=out_headers,
                preload_content=False,
                decode_content=False,
                redirect=False,
                timeout=_UPSTREAM_TIMEOUT,
            )
        except Exception as exc:
            logger.debug("proxy: upstream error: %s", exc)
            try:
                self._send_simple(502, f"Bad Gateway: {exc}")
            except Exception:
                pass
            return

        # ── 3a. 401 retry — refresh credentials once ─────────────────────────
        if resp.status == 401 and not _retried:
            try:
                resp.drain_conn()
            except Exception:
                pass
            refreshed_headers = self.header_mutator(pre_auth_headers, refresh=True)
            refreshed_headers["host"] = self.target_host
            try:
                resp = self.pool.urlopen(
                    self.command,
                    url,
                    body=body,
                    headers=refreshed_headers,
                    preload_content=False,
                    decode_content=False,
                    redirect=False,
                    timeout=_UPSTREAM_TIMEOUT,
                )
            except Exception as exc:
                logger.debug("proxy: upstream error: %s", exc)
                try:
                    self._send_simple(502, f"Bad Gateway: {exc}")
                except Exception:
                    pass
                return

        # Forward status + headers (strip hop-by-hop).
        self.send_response(resp.status)
        for key, value in resp.headers.items():
            if key.lower() in _HOP_BY_HOP:
                continue
            self.send_header(_sanitize_header(key), _sanitize_header(value))
        self.end_headers()

        # Stream response body in chunks (handles SSE correctly).
        try:
            while True:
                chunk = resp.read(_CHUNK_SIZE)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception:
            pass
        finally:
            try:
                resp.drain_conn()
            except Exception:
                pass

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
    """Internal threading server — one thread per request."""

    daemon_threads = True


class APIServer:
    """Public handle for a running API proxy server.

    Intentionally a thin wrapper around :class:`_ThreadingHTTPServer` rather
    than a subclass.  Subclassing would expose the full ``HTTPServer`` /
    ``socketserver`` API (``shutdown()``, ``server_close()``, ``socket``, …)
    on a type that callers receive from :func:`api_server`.  Those methods are
    owned by the context manager and should not be called directly; leaking
    them onto the public type is misleading and a footgun.

    Callers only ever need ``port``.  If more information is needed in the
    future, add explicit properties here rather than inheriting everything.
    """

    def __init__(self, server: _ThreadingHTTPServer) -> None:
        self._server = server

    @property
    def port(self) -> int:
        """Ephemeral port the proxy is listening on."""
        return self._server.server_address[1]


@contextmanager
def api_server(
    header_mutator: Callable[..., dict[str, str]],
    proxy_token: str,
    target_host: str = "api.anthropic.com",
    target_scheme: str = "https",
    path_prefix: str = "",
    *,
    _pool: urllib3.PoolManager | None = None,
) -> Generator[APIServer, None, None]:
    """Start the reverse proxy, yield an :class:`APIServer`, and shut it down on exit.

    The server binds to ``0.0.0.0:0`` so the OS picks an ephemeral port.
    Inbound requests must present *proxy_token* as ``x-api-key`` or
    ``Authorization: Bearer <token>``; others are rejected with 401.
    This isolates concurrent proxy instances so that containers from different
    sessions cannot route through each other's proxy.
    The real credentials never leave the host process.

    The outbound ``urllib3.PoolManager`` is constructed with a
    ``truststore.SSLContext`` so TLS verification uses the OS native
    trust store (macOS Keychain, Linux ``/etc/ssl/certs``, Windows cert
    store) instead of Python's bundled certifi list.  This means any
    non-public CA the user has already installed system-wide — public
    or corporate — is trusted automatically; no hatchery-specific CA
    config is needed.  The patch is scoped to this pool only (the
    process-wide ``ssl`` module is untouched).

    Args:
        header_mutator: Callable that transforms outbound request headers.
            Called for every proxied request with the inbound headers (hop-by-hop
            already stripped). Must strip inbound auth headers, inject the real
            API key in the correct format, and return the modified dict.
        proxy_token: The stable per-task token the container uses.
        target_host: Upstream API hostname (default: ``api.anthropic.com``).
            Use ``"api.openai.com"`` for OpenAI/codex.
        target_scheme: URL scheme for the upstream connection (default: ``"https"``).
            Set to ``"http"`` for plain-HTTP internal/on-prem endpoints.
        path_prefix: String prepended to every forwarded path (default: ``""``).
            Use ``"/backend-api/codex"`` for ChatGPT OAuth mode.
    """

    # Bind all settings to a fresh handler subclass so multiple concurrent
    # proxy instances don't share state via class-level attributes.
    class _BoundHandler(_ProxyHandler):
        pass

    _BoundHandler.header_mutator = staticmethod(header_mutator)
    _BoundHandler.proxy_token = proxy_token
    _BoundHandler.target_host = target_host
    _BoundHandler.target_scheme = target_scheme
    _BoundHandler.path_prefix = path_prefix
    if _pool is not None:
        _BoundHandler.pool = _pool
    else:
        ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        _BoundHandler.pool = urllib3.PoolManager(maxsize=16, ssl_context=ssl_context)

    server = _ThreadingHTTPServer(("0.0.0.0", 0), _BoundHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.debug(
        "Proxy started on port %d (target=%s prefix=%r)",
        server.server_address[1],
        target_host,
        path_prefix,
    )
    try:
        yield APIServer(server)
    finally:
        server.shutdown()
        server.server_close()
        logger.debug("Proxy stopped")
