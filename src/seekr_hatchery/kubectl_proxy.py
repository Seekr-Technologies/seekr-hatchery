"""Host-side kubectl RBAC proxy for hatchery sandboxes.

Architecture:

1. ``start_kubectl_proxy_proc()`` launches ``kubectl proxy --port=0 --address=127.0.0.1``
   on the host, bound to loopback only, using the host's active kubeconfig for
   credentials.  The port it binds to is parsed from its startup output.

2. ``start_rbac_proxy(rules, proxy_token, kubectl_proxy_port)`` starts a second
   HTTP server on an ephemeral 0.0.0.0 port.  Requests from the container must
   carry the per-task bearer token.  The proxy parses the Kubernetes API URL,
   applies the configured RBAC allowlist, and forwards permitted requests to the
   kubectl proxy.  Denied requests receive 403.

3. ``make_kubeconfig(rbac_port, proxy_token)`` produces a kubeconfig YAML that
   points at the RBAC proxy and embeds the bearer token.  This file is mounted
   into the container at ``~/.kube/config``.

The real kubeconfig / credentials never leave the host process.  The container
talks HTTP to ``host.docker.internal:{rbac_port}`` and the RBAC proxy forwards
only permitted requests to ``127.0.0.1:{kubectl_proxy_port}``.

Subresources exec / attach / portforward / proxy are always blocked regardless
of rules.

Public interface::

    proc, kube_port = start_kubectl_proxy_proc()
    server, rbac_port = start_rbac_proxy(rules, proxy_token, kube_port)
    kubeconfig_yaml = make_kubeconfig(rbac_port, proxy_token)
    # ... run container ...
    stop_rbac_proxy(server)
    stop_kubectl_proxy_proc(proc)
"""

from __future__ import annotations

import base64
import http.client
import http.server
import logging
import os
import re
import socketserver
import ssl
import subprocess
import tempfile
import textwrap
import threading
from typing import Any

from pydantic import BaseModel, field_validator

logger = logging.getLogger("hatchery")

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

# ── Models ────────────────────────────────────────────────────────────────────


_KNOWN_VERBS: frozenset[str] = frozenset(
    {"get", "list", "watch", "create", "update", "patch", "delete", "deletecollection", "*"}
)


class KubectlRBACRule(BaseModel):
    """Single allowlist rule for the kubectl RBAC proxy.

    A request is allowed if it matches all three fields of at least one rule.
    ``"*"`` acts as a wildcard for that field.

    ``namespaces`` uses ``""`` (empty string) to match cluster-scoped requests
    (those without a ``/namespaces/{name}/`` segment in the URL, e.g.
    ``kubectl get pods -A`` or ``kubectl get nodes``).
    """

    verbs: list[str]
    """k8s verbs: get, list, watch, create, update, patch, delete, or ``*``.

    Client-side kubectl commands like ``describe``, ``logs``, ``exec`` are NOT
    valid RBAC verbs — they resolve to HTTP methods (``describe`` → ``GET``,
    ``exec`` → blocked subresource).  Unknown verbs are warned at load time and
    will never match any request.
    """

    resources: list[str]
    """Resource kinds: pods, services, deployments, etc., or ``*``."""

    namespaces: list[str] = ["*"]
    """Namespace names.  ``*`` matches everything.  ``""`` matches cluster-scoped
    (all-namespace / non-namespaced) requests."""

    @field_validator("verbs")
    @classmethod
    def _warn_unknown_verbs(cls, verbs: list[str]) -> list[str]:
        unknown = [v for v in verbs if v not in _KNOWN_VERBS]
        if unknown:
            logger.warning(
                "kubectl RBAC rules contain unrecognized verb(s) %s — "
                "these will never match any request. "
                "Valid verbs: %s. "
                "Note: 'describe' is a kubectl client command, not a k8s verb "
                "(it issues GET requests, which 'get' already covers).",
                unknown,
                sorted(_KNOWN_VERBS - {"*"}),
            )
        return verbs


class KubectlConfig(BaseModel):
    """Top-level kubectl proxy configuration loaded from docker.yaml."""

    context: str | None = None
    """Kubeconfig context to use.  Defaults to the host's active context.
    Set this when you have multiple contexts and want to pin which cluster
    the agent can reach (e.g. ``context: my-dev-cluster``)."""

    rules: list[KubectlRBACRule] = []
    """Allowlist rules.  Empty list means deny everything (fail-closed)."""


# ── URL parsing ───────────────────────────────────────────────────────────────


def parse_k8s_url(path: str) -> tuple[str, str, str]:
    """Parse a Kubernetes API URL into ``(namespace, resource, subresource)``.

    Returns ``("", "", "")`` for discovery / non-resource endpoints
    (``/api``, ``/apis``, ``/healthz``, ``/version``, etc.).

    Examples::

        parse_k8s_url("/api/v1/namespaces/default/pods")
        # → ("default", "pods", "")

        parse_k8s_url("/api/v1/namespaces/default/pods/foo/exec")
        # → ("default", "pods", "exec")

        parse_k8s_url("/api/v1/nodes")
        # → ("", "nodes", "")

        parse_k8s_url("/apis/apps/v1/namespaces/staging/deployments/my-dep")
        # → ("staging", "deployments", "")

        parse_k8s_url("/apis/apps/v1/deployments")
        # → ("", "deployments", "")
    """
    # Strip query string and trailing slash.
    path = path.split("?", 1)[0].rstrip("/")

    # ── Core API: /api/v1/... ─────────────────────────────────────────────────
    # Namespaced: /api/v1/namespaces/{ns}/{resource}[/{name}[/{sub}]]
    m = re.match(
        r"^/api/[^/]+/namespaces/([^/]+)/([^/]+)(?:/[^/]+(?:/([^/]+))?)?$",
        path,
    )
    if m:
        return m.group(1), m.group(2), m.group(3) or ""

    # Cluster-scoped: /api/v1/{resource}[/{name}]
    m = re.match(r"^/api/[^/]+/([^/]+)(?:/[^/]+)?$", path)
    if m:
        return "", m.group(1), ""

    # ── Group API: /apis/{group}/{version}/... ────────────────────────────────
    # Namespaced: /apis/{group}/{version}/namespaces/{ns}/{resource}[/{name}[/{sub}]]
    m = re.match(
        r"^/apis/[^/]+/[^/]+/namespaces/([^/]+)/([^/]+)(?:/[^/]+(?:/([^/]+))?)?$",
        path,
    )
    if m:
        return m.group(1), m.group(2), m.group(3) or ""

    # Cluster-scoped: /apis/{group}/{version}/{resource}[/{name}]
    m = re.match(r"^/apis/[^/]+/[^/]+/([^/]+)(?:/[^/]+)?$", path)
    if m:
        return "", m.group(1), ""

    # Discovery / non-resource endpoints (/api, /apis, /healthz, /version, ...)
    return "", "", ""


# ── Verb mapping ──────────────────────────────────────────────────────────────


def http_method_to_k8s_verbs(method: str) -> list[str]:
    """Map an HTTP method to the corresponding Kubernetes RBAC verbs.

    The caller should check whether *any* of the returned verbs is permitted
    by the configured rules.
    """
    return {
        "GET": ["get", "list", "watch"],
        "POST": ["create"],
        "PUT": ["update"],
        "PATCH": ["patch"],
        "DELETE": ["delete", "deletecollection"],
    }.get(method.upper(), [method.lower()])


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
        namespace, resource, subresource = parse_k8s_url(self.path)

        # ── 2. Block dangerous subresources unconditionally ───────────────────
        if subresource in _BLOCKED_SUBRESOURCES:
            self._send_json(
                403,
                f"kubectl: subresource '{subresource}' is always blocked by hatchery",
            )
            return

        # ── 3. Apply RBAC allowlist (skip for discovery endpoints) ────────────
        if resource:  # non-empty resource means it's a resource endpoint
            verbs = http_method_to_k8s_verbs(self.command)
            if not check_rbac(self.rules, verbs, resource, namespace):
                verb_str = "/".join(verbs)
                ns_str = namespace if namespace else "<cluster-scoped>"
                self._send_json(
                    403,
                    f"kubectl: {verb_str} '{resource}' in namespace '{ns_str}' is not permitted",
                )
                return

        # ── 4. Forward to kubectl proxy ───────────────────────────────────────
        logger.debug(
            "kubectl RBAC proxy: %s %s → kubectl-proxy:%d",
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

            logger.debug(
                "kubectl RBAC proxy: upstream returned %d for %s %s",
                resp.status,
                self.command,
                self.path,
            )
            self.send_response(resp.status)
            for key, value in resp.getheaders():
                if key.lower() in _HOP_BY_HOP:
                    continue
                # Guard against HTTP response splitting: reject header names that
                # don't match the RFC 7230 token grammar, and strip CR/LF from values.
                if not _HEADER_NAME_RE.match(key):
                    logger.warning("kubectl RBAC proxy: dropping upstream header with unsafe name: %r", key)
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
            logger.debug("kubectl rbac proxy: upstream error: %s", exc)
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


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


# ── TLS cert generation ───────────────────────────────────────────────────────


def _generate_self_signed_cert() -> tuple[bytes, bytes]:
    """Generate a throwaway self-signed TLS certificate and private key.

    Returns ``(cert_pem, key_pem)`` as bytes.

    Requires the ``cryptography`` package (a declared project dependency).
    The certificate is valid for 24 hours and has ``host.docker.internal``
    as the only SAN, which is sufficient for the container→host RBAC proxy.
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
        .not_valid_after(now + datetime.timedelta(days=1))
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
        rules: Allowlist rules from :class:`KubectlConfig`.
        proxy_token: Bearer token the container must send.
        kubectl_proxy_port: Local port where ``kubectl proxy`` is listening.
    """

    class _BoundHandler(_RBACProxyHandler):
        pass

    _BoundHandler.proxy_token = proxy_token
    _BoundHandler.kubectl_proxy_port = kubectl_proxy_port
    _BoundHandler.rules = rules

    cert_pem, key_pem = _generate_self_signed_cert()

    server = _ThreadingHTTPServer(("0.0.0.0", 0), _BoundHandler)
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


def start_kubectl_proxy_proc(
    context: str | None = None,
    timeout: float = 10.0,
) -> tuple[subprocess.Popen[str], int]:
    """Launch ``kubectl proxy --port=0 --address=127.0.0.1`` and return ``(proc, port)``.

    Args:
        context: Kubeconfig context to pass via ``--context``.  ``None`` uses
            the host's currently active context.
        timeout: Seconds to wait for the startup banner before giving up.

    Reads stdout until the startup banner ``"Starting to serve on 127.0.0.1:{port}"``
    is seen, then returns.  Raises :class:`RuntimeError` if kubectl is not found,
    the process exits early, or the port cannot be determined within *timeout* seconds.
    """
    import shutil
    import time

    if not shutil.which("kubectl"):
        raise RuntimeError("kubectl not found on PATH — install kubectl on the host to use the kubectl feature")

    cmd = ["kubectl", "proxy", "--port=0", "--address=127.0.0.1"]
    if context:
        cmd += ["--context", context]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    deadline = time.monotonic() + timeout
    port_re = re.compile(r"Starting to serve on 127\.0\.0\.1:(\d+)")

    assert proc.stdout is not None  # guaranteed by PIPE
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            # Process exited unexpectedly.
            stderr_out = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"kubectl proxy exited unexpectedly.  stderr: {stderr_out.strip()}")
        m = port_re.search(line)
        if m:
            port = int(m.group(1))
            logger.debug("kubectl proxy started on port %d", port)
            return proc, port

    proc.terminate()
    raise RuntimeError(f"kubectl proxy did not report its port within {timeout}s")


def stop_kubectl_proxy_proc(proc: subprocess.Popen[str]) -> None:
    """Terminate the kubectl proxy subprocess."""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    logger.debug("kubectl proxy stopped")


def make_kubeconfig(rbac_port: int, proxy_token: str, ca_cert_pem: bytes) -> str:
    """Return a kubeconfig YAML that routes kubectl through the RBAC proxy over TLS.

    kubectl refuses to send ``Authorization: Bearer`` headers over plain HTTP
    to non-localhost hosts.  This kubeconfig uses ``https://`` and pins the
    self-signed certificate via ``certificate-authority-data``, which is the
    same pattern used by kind / k3d / minikube for local cluster endpoints.

    Args:
        rbac_port: Port where the RBAC proxy is listening (on the host).
        proxy_token: Bearer token embedded for the container to authenticate.
        ca_cert_pem: PEM-encoded self-signed cert returned by :func:`start_rbac_proxy`.
    """
    ca_b64 = base64.b64encode(ca_cert_pem).decode()
    return textwrap.dedent(f"""\
        apiVersion: v1
        kind: Config
        clusters:
          - name: hatchery-proxy
            cluster:
              server: https://host.docker.internal:{rbac_port}
              certificate-authority-data: {ca_b64}
        current-context: hatchery-proxy
        contexts:
          - name: hatchery-proxy
            context:
              cluster: hatchery-proxy
              user: hatchery-agent
        users:
          - name: hatchery-agent
            user:
              token: {proxy_token}
    """)
