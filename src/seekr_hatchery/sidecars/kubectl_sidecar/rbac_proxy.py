"""TLS RBAC-filtering proxy in front of a local ``kubectl proxy``.

``start_rbac_proxy(rules, proxy_token, kubectl_proxy_port)`` starts an HTTP
server on an ephemeral ``0.0.0.0`` port.  Requests from the container must
carry the per-task bearer token.  The proxy parses the Kubernetes API URL,
applies the configured RBAC allowlist, and forwards permitted requests to the
kubectl proxy.  Denied requests receive 403.

The real kubeconfig / credentials never leave the host process.  The container
talks HTTPS to ``host.docker.internal:{rbac_port}`` and this proxy forwards
only permitted requests to ``127.0.0.1:{kubectl_proxy_port}``.

Subresources exec / attach / portforward / proxy are always blocked regardless
of rules.

Public interface::

    server, rbac_port, ca_cert_pem = start_rbac_proxy(rules, proxy_token, kube_port)
    # ... run container ...
    stop_rbac_proxy(server)
"""

from __future__ import annotations

import http.client
import http.server
import logging
import os
import re
import ssl
import tempfile
import threading
from typing import Any
from urllib.parse import parse_qs

from seekr_hatchery.models import KubectlRBACRule
from seekr_hatchery.sidecars.http_server import ThreadingHTTPServer

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 8192

# Hop-by-hop headers that must not be forwarded between proxy hops.
_HOP_BY_HOP: frozenset[str] = frozenset(
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

# Kubernetes API subresources that are always blocked.
# These use streaming/interactive protocols (SPDY/WebSocket) that we don't proxy.
_BLOCKED_SUBRESOURCES: frozenset[str] = frozenset({"exec", "attach", "portforward", "proxy"})

# RFC 7230 header field-name token — rejects anything that could enable header injection.
_HEADER_NAME_RE: re.Pattern[str] = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


# ── URL parsing ───────────────────────────────────────────────────────────────


# Values accepted as "true" by Kubernetes' own bool query-param parsing
# (a subset of Go's strconv.ParseBool truthy forms).
_TRUTHY_QUERY_VALUES: frozenset[str] = frozenset({"1", "t", "true"})


def _is_watch_query(query: str) -> bool:
    """Return True if the query string requests a watch (``?watch=true``)."""
    if not query:
        return False
    values = parse_qs(query).get("watch", [])
    return any(v.lower() in _TRUTHY_QUERY_VALUES for v in values)


def parse_k8s_url(path: str) -> tuple[str, str, str, bool, bool]:
    """Parse a Kubernetes API URL into ``(namespace, resource, subresource, has_name, is_watch)``.

    ``has_name`` is ``True`` when the path names a specific object (e.g.
    ``.../pods/my-pod``) rather than a bare collection (e.g. ``.../pods``) —
    this is what distinguishes a ``get``/``delete`` request from a
    ``list``/``deletecollection`` request on an otherwise-identical HTTP
    method.

    ``is_watch`` is ``True`` when the query string carries ``watch=true``
    (or another truthy form) — this is what distinguishes a ``watch``
    request from ``get``/``list`` on an otherwise-identical HTTP GET.

    Returns ``("", "", "", False, False)`` for discovery / non-resource
    endpoints (``/api``, ``/apis``, ``/healthz``, ``/version``, etc.).

    Examples::

        parse_k8s_url("/api/v1/namespaces/default/pods")
        # → ("default", "pods", "", False, False)

        parse_k8s_url("/api/v1/namespaces/default/pods/foo")
        # → ("default", "pods", "", True, False)

        parse_k8s_url("/api/v1/namespaces/default/pods?watch=true")
        # → ("default", "pods", "", False, True)

        parse_k8s_url("/api/v1/namespaces/default/pods/foo/exec")
        # → ("default", "pods", "exec", True, False)

        parse_k8s_url("/api/v1/nodes")
        # → ("", "nodes", "", False, False)

        parse_k8s_url("/apis/apps/v1/namespaces/staging/deployments/my-dep")
        # → ("staging", "deployments", "", True, False)

        parse_k8s_url("/apis/apps/v1/deployments")
        # → ("", "deployments", "", False, False)
    """
    # Split off the query string (needed for the watch check) and trailing slash.
    path, _, query = path.partition("?")
    path = path.rstrip("/")
    is_watch = _is_watch_query(query)

    # ── Core API: /api/v1/... ─────────────────────────────────────────────────
    # Namespaced: /api/v1/namespaces/{ns}/{resource}[/{name}[/{sub}]]
    m = re.match(
        r"^/api/[^/]+/namespaces/([^/]+)/([^/]+)(?:/([^/]+)(?:/([^/]+))?)?$",
        path,
    )
    if m:
        return m.group(1), m.group(2), m.group(4) or "", bool(m.group(3)), is_watch

    # Cluster-scoped: /api/v1/{resource}[/{name}]
    m = re.match(r"^/api/[^/]+/([^/]+)(?:/([^/]+))?$", path)
    if m:
        return "", m.group(1), "", bool(m.group(2)), is_watch

    # ── Group API: /apis/{group}/{version}/... ────────────────────────────────
    # Namespaced: /apis/{group}/{version}/namespaces/{ns}/{resource}[/{name}[/{sub}]]
    m = re.match(
        r"^/apis/[^/]+/[^/]+/namespaces/([^/]+)/([^/]+)(?:/([^/]+)(?:/([^/]+))?)?$",
        path,
    )
    if m:
        return m.group(1), m.group(2), m.group(4) or "", bool(m.group(3)), is_watch

    # Cluster-scoped: /apis/{group}/{version}/{resource}[/{name}]
    m = re.match(r"^/apis/[^/]+/[^/]+/([^/]+)(?:/([^/]+))?$", path)
    if m:
        return "", m.group(1), "", bool(m.group(2)), is_watch

    # Discovery / non-resource endpoints (/api, /apis, /healthz, /version, ...)
    return "", "", "", False, False


# ── Verb mapping ──────────────────────────────────────────────────────────────


def http_method_to_k8s_verbs(method: str, has_name: bool, is_watch: bool) -> list[str]:
    """Map an HTTP method to the corresponding Kubernetes RBAC verb.

    *has_name* and *is_watch* come from :func:`parse_k8s_url` and are what
    let an otherwise-identical HTTP method resolve to distinct verbs:

    - GET: ``watch`` (if *is_watch*) > ``get`` (if *has_name*) > ``list``.
      Watch takes precedence because ``?watch=true`` on a named-object URL
      still watches that one object rather than fetching it once.
    - DELETE: ``delete`` (if *has_name*) else ``deletecollection``.

    Without these, every GET/DELETE would be indistinguishable and, e.g., a
    ``list``-only rule would also grant ``get``.

    The caller should check whether *any* of the returned verbs is permitted
    by the configured rules.
    """
    method = method.upper()
    if method == "GET":
        if is_watch:
            return ["watch"]
        return ["get"] if has_name else ["list"]
    if method == "DELETE":
        return ["delete"] if has_name else ["deletecollection"]
    return {
        "POST": ["create"],
        "PUT": ["update"],
        "PATCH": ["patch"],
    }.get(method, [method.lower()])


# ── RBAC checking ─────────────────────────────────────────────────────────────


def check_rbac(
    rules: list[KubectlRBACRule],
    verbs: list[str],
    resource: str,
    namespace: str,
) -> bool:
    """Return True if the request is permitted by at least one allowlist rule.

    *verbs* should be the list returned by :func:`http_method_to_k8s_verbs`.
    *namespace* is ``""`` for cluster-scoped requests.

    Empty *rules* list → always deny (fail-closed).
    """
    for rule in rules:
        verb_ok = "*" in rule.verbs or any(v in rule.verbs for v in verbs)
        resource_ok = "*" in rule.resources or resource in rule.resources
        ns_ok = "*" in rule.namespaces or namespace in rule.namespaces
        if verb_ok and resource_ok and ns_ok:
            return True
    return False


# ── HTTP proxy handler ────────────────────────────────────────────────────────


class _RBACProxyHandler(http.server.BaseHTTPRequestHandler):
    """Validates token, enforces RBAC, and forwards to kubectl proxy."""

    # HTTP/1.1 is required for correct chunked-transfer / keep-alive handling.
    # Python's BaseHTTPRequestHandler defaults to HTTP/1.0; override it here.
    protocol_version = "HTTP/1.1"

    # Overridden per-server-instance via fresh subclass in start_rbac_proxy.
    proxy_token: str = ""
    kubectl_proxy_port: int = 0
    rules: list[KubectlRBACRule] = []

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # suppress per-request access log lines

    def _send_json(self, code: int, message: str) -> None:
        import json

        body = json.dumps({"error": message}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _validate_token(self) -> bool:
        auth = self.headers.get("authorization", "")
        if auth.startswith("Bearer ") and auth[len("Bearer ") :] == self.proxy_token:
            return True
        return False

    def _handle_request(self) -> None:
        # HTTP/1.1 defaults to keep-alive; force close after each request so
        # clients don't wait for a second request that never comes.
        self.close_connection = True

        # ── 0. Validate bearer token ──────────────────────────────────────────
        if not self._validate_token():
            received = self.headers.get("authorization", "")[7:]  # strip "Bearer "
            logger.warning(
                "kubectl RBAC proxy: 401 – bearer token mismatch. Expected ...%s, received ...%s. path=%s",
                self.proxy_token[-6:] if len(self.proxy_token) >= 6 else self.proxy_token,
                received[-6:] if len(received) >= 6 else f"(empty or short: {received!r})",
                self.path,
            )
            self._send_json(401, "Unauthorized")
            return

        # ── 1. Parse the k8s API URL ──────────────────────────────────────────
        namespace, resource, subresource, has_name, is_watch = parse_k8s_url(self.path)

        # ── 2. Block dangerous subresources unconditionally ───────────────────
        if subresource in _BLOCKED_SUBRESOURCES:
            self._send_json(
                403,
                f"kubectl: subresource '{subresource}' is always blocked by hatchery",
            )
            return

        # ── 3. Apply RBAC allowlist (skip for discovery endpoints) ────────────
        if resource:  # non-empty resource means it's a resource endpoint
            verbs = http_method_to_k8s_verbs(self.command, has_name, is_watch)
            if not check_rbac(self.rules, verbs, resource, namespace):
                verb_str = "/".join(verbs)
                ns_str = namespace if namespace else "<cluster-scoped>"
                logger.info(
                    "kubectl RBAC: 403 denied — %s %s (%s '%s' in '%s')",
                    self.command,
                    self.path,
                    verb_str,
                    resource,
                    ns_str,
                )
                self._send_json(
                    403,
                    f"kubectl: {verb_str} '{resource}' in namespace '{ns_str}' is not permitted",
                )
                return

        # ── 4. Forward to kubectl proxy ───────────────────────────────────────
        logger.info(
            "kubectl RBAC: %s %s → kubectl-proxy:%d (allowed)",
            self.command,
            self.path,
            self.kubectl_proxy_port,
        )
        content_length = int(self.headers.get("content-length", 0) or 0)
        body = self.rfile.read(content_length) if content_length else None

        # Forward with minimal headers; strip hop-by-hop and our bearer token.
        forward_headers: dict[str, str] = {}
        for key, val in self.headers.items():
            if key.lower() in _HOP_BY_HOP or key.lower() == "authorization":
                continue
            forward_headers[key] = val

        conn = http.client.HTTPConnection("127.0.0.1", self.kubectl_proxy_port)
        try:
            conn.request(self.command, self.path, body=body, headers=forward_headers)
            resp = conn.getresponse()

            logger.info(
                "kubectl RBAC: upstream returned %d for %s %s",
                resp.status,
                self.command,
                self.path,
            )
            self.send_response(resp.status)
            # Forward only a fixed set of safe response headers to avoid
            # reflecting untrusted/dynamic header names to the client.
            _ALLOWED_RESPONSE_HEADERS = {
                "content-type",
                "content-length",
                "content-encoding",
                "content-language",
                "cache-control",
                "pragma",
                "expires",
                "last-modified",
                "etag",
                "vary",
                "date",
            }
            for key, value in resp.getheaders():
                key_lc = key.lower()
                if key_lc in _HOP_BY_HOP:
                    continue
                if key_lc not in _ALLOWED_RESPONSE_HEADERS:
                    logger.debug("kubectl RBAC proxy: dropping non-allowlisted upstream header: %r", key)
                    continue
                self.send_header(key, value.replace("\r", "").replace("\n", ""))
            self.end_headers()

            # Stream response body in chunks (handles watch / log streaming).
            while True:
                chunk = resp.read(_CHUNK_SIZE)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()

        except Exception as exc:
            logger.warning("kubectl RBAC: upstream error: %s", exc)
            try:
                self._send_json(502, f"Bad Gateway: {exc}")
            except Exception:
                pass
        finally:
            conn.close()

    def do_GET(self) -> None:  # noqa: N802
        self._handle_request()

    def do_POST(self) -> None:  # noqa: N802
        self._handle_request()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle_request()

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle_request()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle_request()

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle_request()


# ── TLS cert generation ───────────────────────────────────────────────────────


def _generate_self_signed_cert(validity_days: int = 365) -> tuple[bytes, bytes]:
    """Generate a throwaway self-signed TLS certificate and private key.

    Returns ``(cert_pem, key_pem)`` as bytes.

    Requires the ``cryptography`` package (a declared project dependency).
    The certificate has ``host.docker.internal`` as the only SAN, which is
    sufficient for the container→host RBAC proxy.

    *validity_days* is intentionally long: the cert is meaningless without
    the proxy process that signed it (the kubeconfig pins this cert as CA
    and points at an ephemeral port on ``host.docker.internal`` that's
    only reachable while this process is alive), so cert rotation defends
    against no realistic threat — but a too-short validity silently breaks
    sandboxes that outlive the cert.
    """
    try:
        import datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "The kubectl feature requires the 'cryptography' package. Install it with: pip install cryptography"
        ) from exc

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "hatchery-kubectl-proxy")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=validity_days))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("host.docker.internal")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem


# ── Public API ────────────────────────────────────────────────────────────────


def start_rbac_proxy(
    rules: list[KubectlRBACRule],
    proxy_token: str,
    kubectl_proxy_port: int,
) -> tuple[http.server.HTTPServer, int, bytes]:
    """Start the TLS RBAC filtering proxy and return ``(server, port, cert_pem)``.

    Binds to ``0.0.0.0:0`` (OS picks ephemeral port) so the container can
    reach it via ``host.docker.internal``.  The returned ``cert_pem`` should
    be embedded in the kubeconfig as ``certificate-authority-data`` so that
    kubectl trusts exactly this certificate.

    kubectl refuses to send ``Authorization: Bearer`` headers over plain HTTP
    to non-localhost hosts.  Serving HTTPS here is the correct fix — the same
    pattern used by kind / k3d / minikube for local cluster endpoints.

    Args:
        rules: Allowlist rules from :class:`seekr_hatchery.models.KubectlConfig`.
        proxy_token: Bearer token the container must send.
        kubectl_proxy_port: Local port where ``kubectl proxy`` is listening.
    """

    class _BoundHandler(_RBACProxyHandler):
        pass

    _BoundHandler.proxy_token = proxy_token
    _BoundHandler.kubectl_proxy_port = kubectl_proxy_port
    _BoundHandler.rules = rules

    cert_pem, key_pem = _generate_self_signed_cert()

    server = ThreadingHTTPServer(("0.0.0.0", 0), _BoundHandler)
    port = server.server_address[1]

    # ssl.SSLContext.load_cert_chain() requires file paths; write to temp files
    # and delete them immediately after the context has loaded them into memory.
    cert_fd, cert_path = tempfile.mkstemp(suffix="-rbac-cert.pem")
    key_fd, key_path = tempfile.mkstemp(suffix="-rbac-key.pem")
    try:
        os.write(cert_fd, cert_pem)
        os.close(cert_fd)
        os.write(key_fd, key_pem)
        os.close(key_fd)
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_ctx.load_cert_chain(cert_path, key_path)
    finally:
        for p in (cert_path, key_path):
            try:
                os.unlink(p)
            except OSError:
                pass

    server.socket = ssl_ctx.wrap_socket(server.socket, server_side=True)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    logger.debug("kubectl RBAC proxy (TLS) started on port %d", port)
    return server, port, cert_pem


def stop_rbac_proxy(server: http.server.HTTPServer) -> None:
    """Gracefully shut down the RBAC proxy."""
    server.shutdown()
    server.server_close()
    logger.debug("kubectl RBAC proxy stopped")
