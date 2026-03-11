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

    server, proxy_token = start_proxy(api_key, proxy_token)
    # ... run container ...
    stop_proxy(server)

For OpenAI (codex)::

    server, proxy_token = start_proxy(
        api_key, proxy_token,
        target_host="api.openai.com",
        inject_header="authorization",
    )
"""

import http.client
import http.server
import logging
import socketserver
import threading
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
    }
)


def _sanitize_header(value: str) -> str:
    """Strip CR/LF to prevent HTTP response splitting."""
    return value.replace("\r", "").replace("\n", "")


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    """Validates the proxy token, strips inbound auth, injects the real API key, and forwards."""

    # Overridden per-server-instance by start_proxy via a fresh subclass.
    api_key: str = ""
    proxy_token: str = ""
    target_host: str = "api.anthropic.com"
    inject_header: str = "x-api-key"  # "x-api-key" or "authorization"
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

    def _handle_request(self) -> None:
        # ── 0. Validate proxy token ───────────────────────────────────────────
        # Reject requests whose token doesn't match the stable per-task proxy
        # token.  This prevents other containers (which share the host gateway
        # and can discover open ports) from routing through this proxy.
        if not self._validate_token():
            self._send_simple(401, "Unauthorized")
            return

        # ── 1. Build outgoing headers ─────────────────────────────────────────
        # Strip the inbound proxy token and inject the real API key.
        out_headers: dict[str, str] = {}
        for key, value in self.headers.items():
            lower = key.lower()
            if lower in ("x-api-key", "authorization", "host") or lower in _HOP_BY_HOP:
                continue
            out_headers[key] = value

        # Inject the real API key in the format the target API expects.
        if self.inject_header == "authorization":
            out_headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            out_headers["x-api-key"] = self.api_key
        out_headers["host"] = self.target_host

        # ── 2. Read request body ──────────────────────────────────────────────
        content_length = int(self.headers.get("content-length", 0) or 0)
        body = self.rfile.read(content_length) if content_length else None

        # ── 3. Forward to target host ─────────────────────────────────────────
        conn = http.client.HTTPSConnection(self.target_host)
        try:
            conn.request(self.command, self.path_prefix + self.path, body=body, headers=out_headers)
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
    api_key: str,
    proxy_token: str,
    target_host: str = "api.anthropic.com",
    inject_header: str = "x-api-key",
    path_prefix: str = "",
) -> tuple[http.server.HTTPServer, str]:
    """Start the reverse proxy and return ``(server, proxy_token)``.

    The server binds to ``0.0.0.0:0`` so the OS picks an ephemeral port.
    Inbound requests must present *proxy_token* as ``x-api-key`` or
    ``Authorization: Bearer <token>``; others are rejected with 401.
    This isolates concurrent proxy instances so that containers from different
    sessions cannot route through each other's proxy.
    The real *api_key* never leaves the host process.

    Args:
        api_key: The real API key to inject into outbound requests.
        proxy_token: The stable per-task token the container uses.
        target_host: Upstream API hostname (default: ``api.anthropic.com``).
            Use ``"api.openai.com"`` for OpenAI/codex.
        inject_header: How to inject the real key outbound.
            ``"x-api-key"`` (default) for Anthropic;
            ``"authorization"`` for OpenAI (sends ``Authorization: Bearer <key>``).
        path_prefix: String prepended to every forwarded path (default: ``""``).
            Use ``"/backend-api/codex"`` for ChatGPT OAuth mode.
    """

    # Bind all settings to a fresh handler class so multiple concurrent proxy
    # instances don't share state.
    class _BoundHandler(_ProxyHandler):
        pass

    _BoundHandler.api_key = api_key
    _BoundHandler.proxy_token = proxy_token
    _BoundHandler.target_host = target_host
    _BoundHandler.inject_header = inject_header
    _BoundHandler.path_prefix = path_prefix

    server = _ThreadingHTTPServer(("0.0.0.0", 0), _BoundHandler)
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    logger.debug(
        "Proxy started on port %d (target=%s inject=%s prefix=%r)",
        port,
        target_host,
        inject_header,
        path_prefix,
    )
    return server, proxy_token


def stop_proxy(server: http.server.HTTPServer) -> None:
    """Gracefully shut down the proxy server."""
    server.shutdown()
    server.server_close()
    logger.debug("Proxy stopped")
