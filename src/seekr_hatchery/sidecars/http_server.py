"""Shared threading HTTP server base used by both sidecar proxies."""

import http.server
import socketserver


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Internal threading server — one thread per request."""

    daemon_threads = True
