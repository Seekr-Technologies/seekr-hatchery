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

3. ``make_kubeconfig(contexts, proxy_token, ca_cert_pem)`` produces a kubeconfig
   YAML that points at the RBAC proxies and embeds the bearer token.  This file
   is mounted into the container at ``~/.kube/config``.

The real kubeconfig / credentials never leave the host process.  The container
talks HTTP to ``host.docker.internal:{rbac_port}`` and the RBAC proxy forwards
only permitted requests to ``127.0.0.1:{kubectl_proxy_port}``.

Subresources exec / attach / portforward / proxy are always blocked regardless
of rules.

Several contexts can be exposed at once (e.g. dev and prd), each with its own
rules.  Every context gets its own pair of the two stages above, on its own
ephemeral port, and appears in the container kubeconfig under the host context
name — so the agent switches clusters with ``kubectl --context prd get pods``.
``start_context_proxies`` / ``stop_context_proxies`` wrap that whole fan-out and
are what callers normally use::

    proxies, failures, ca_cert = start_context_proxies(config.resolved_contexts(), token)
    kubeconfig_yaml = make_kubeconfig([(p.name, p.rbac_port) for p in proxies], token, ca_cert)
    # ... run container ...
    stop_context_proxies(proxies)
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
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 8192

# Container-side context name used when the host context is not pinned by name.
DEFAULT_CONTEXT_NAME = "hatchery-proxy"

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


class KubectlContext(BaseModel):
    """One cluster the agent can reach, with the rules that apply to it."""

    model_config = ConfigDict(extra="forbid")

    context: str | None = None
    """Host kubeconfig context to proxy.  ``None`` uses the host's active context."""

    rules: list[KubectlRBACRule] = []
    """Allowlist rules for this context.  Empty list means deny everything."""

    @property
    def display_name(self) -> str:
        """Container-side context name.

        Host context names are reused verbatim so the agent can switch clusters
        with the ordinary ``kubectl --context <name>``.  An unpinned context has
        no name to reuse, so it falls back to the historical ``hatchery-proxy``.
        """
        return self.context or DEFAULT_CONTEXT_NAME


class KubectlConfig(BaseModel):
    """Top-level kubectl proxy configuration loaded from docker.yaml."""

    model_config = ConfigDict(extra="forbid")

    contexts: list[KubectlContext] = []
    """Clusters the agent can reach, each with its own rules.  The first entry
    becomes the container's ``current-context``.  Mutually exclusive with the
    single-context ``context`` / ``rules`` shorthand below."""

    context: str | None = None
    """Single-context shorthand: kubeconfig context to use.  Defaults to the
    host's active context.  Set this when you have multiple contexts and want to
    pin which cluster the agent can reach (e.g. ``context: my-dev-cluster``)."""

    rules: list[KubectlRBACRule] = []
    """Single-context shorthand: allowlist rules.  Empty list means deny
    everything (fail-closed)."""

    @model_validator(mode="after")
    def _check_exclusive_forms(self) -> KubectlConfig:
        if self.contexts and (self.context is not None or self.rules):
            raise ValueError(
                "kubernetes: 'contexts' cannot be combined with the single-context "
                "'context'/'rules' shorthand — move those under a 'contexts' entry"
            )
        names = [c.display_name for c in self.contexts]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"kubernetes: duplicate context name(s) {duplicates} in 'contexts'")
        return self

    def resolved_contexts(self) -> list[KubectlContext]:
        """Return the configured contexts, normalizing the shorthand form.

        Always returns at least one entry: with neither ``contexts`` nor the
        shorthand set, that entry is the host's active context with no rules
        (i.e. deny everything, matching the fail-closed default).
        """
        if self.contexts:
            return list(self.contexts)
        return [KubectlContext(context=self.context, rules=self.rules)]


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


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


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
    cert: tuple[bytes, bytes] | None = None,
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
        cert: Optional ``(cert_pem, key_pem)`` pair to serve instead of a
            freshly generated one.  Multiple proxies in the same session share
            one cert so the kubeconfig needs only a single CA entry — the cert's
            only SAN is ``host.docker.internal``, which is correct for any port.
    """

    class _BoundHandler(_RBACProxyHandler):
        pass

    _BoundHandler.proxy_token = proxy_token
    _BoundHandler.kubectl_proxy_port = kubectl_proxy_port
    _BoundHandler.rules = rules

    cert_pem, key_pem = cert if cert is not None else _generate_self_signed_cert()

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


def _require_kubectl() -> None:
    """Raise :class:`RuntimeError` unless ``kubectl`` is on the host's PATH."""
    import shutil

    if not shutil.which("kubectl"):
        raise RuntimeError("kubectl not found on PATH — install kubectl on the host to use the kubectl feature")


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
    import time

    _require_kubectl()

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


@dataclass
class ContextProxy:
    """A running kubectl-proxy + RBAC-proxy pair for one context."""

    name: str
    """Container-side context name (see :attr:`KubectlContext.display_name`)."""

    kubectl_proc: subprocess.Popen[str]
    rbac_server: http.server.HTTPServer
    rbac_port: int


def start_context_proxies(
    contexts: list[KubectlContext],
    proxy_token: str,
) -> tuple[list[ContextProxy], list[tuple[str, str]], bytes]:
    """Start one kubectl-proxy + RBAC-proxy pair per context.

    Returns ``(started, failures, ca_cert_pem)``.  *failures* is a list of
    ``(context_name, error_message)`` for contexts whose ``kubectl proxy`` did
    not come up (unreachable cluster, expired credentials, unknown context
    name).  Those are reported rather than raised so one stale credential does
    not block a session that has other usable clusters — the caller decides how
    to surface them, and gets an empty *started* list if none came up.

    All proxies share a single TLS cert, returned as *ca_cert_pem* for the
    kubeconfig's ``certificate-authority-data``.

    *started* follows the order of *contexts*, so element 0 is the entry that
    should become ``current-context``.

    Raises:
        RuntimeError: if ``kubectl`` is missing from the host PATH, which dooms
            every context.
    """
    _require_kubectl()
    cert = _generate_self_signed_cert()

    started: list[ContextProxy] = []
    failures: list[tuple[str, str]] = []
    for ctx in contexts:
        try:
            proc, kube_port = start_kubectl_proxy_proc(context=ctx.context)
        except RuntimeError as exc:
            logger.warning("kubectl proxy for context %r failed to start: %s", ctx.display_name, exc)
            failures.append((ctx.display_name, str(exc)))
            continue
        server, rbac_port, _ = start_rbac_proxy(ctx.rules, proxy_token, kube_port, cert=cert)
        started.append(ContextProxy(name=ctx.display_name, kubectl_proc=proc, rbac_server=server, rbac_port=rbac_port))

    return started, failures, cert[0]


def stop_context_proxies(proxies: list[ContextProxy]) -> None:
    """Stop every proxy pair, continuing past individual failures."""
    for p in proxies:
        for stop, arg in ((stop_rbac_proxy, p.rbac_server), (stop_kubectl_proxy_proc, p.kubectl_proc)):
            try:
                stop(arg)  # type: ignore[arg-type]
            except Exception as exc:  # pragma: no cover — best-effort teardown
                logger.warning("kubectl: failed to stop proxy for context %r: %s", p.name, exc)


def make_kubeconfig(contexts: list[tuple[str, int]], proxy_token: str, ca_cert_pem: bytes) -> str:
    """Return a kubeconfig YAML that routes kubectl through the RBAC proxies over TLS.

    kubectl refuses to send ``Authorization: Bearer`` headers over plain HTTP
    to non-localhost hosts.  This kubeconfig uses ``https://`` and pins the
    self-signed certificate via ``certificate-authority-data``, which is the
    same pattern used by kind / k3d / minikube for local cluster endpoints.

    Each context gets its own cluster entry pointing at that context's RBAC
    proxy port; all share one user (the bearer token authenticates the
    container, and per-context isolation comes from the rules each proxy
    enforces).  The first entry becomes ``current-context``.

    Args:
        contexts: ``(context_name, rbac_port)`` pairs, in priority order.
        proxy_token: Bearer token embedded for the container to authenticate.
        ca_cert_pem: PEM-encoded self-signed cert shared by the RBAC proxies.

    Raises:
        ValueError: if *contexts* is empty.
    """
    if not contexts:
        raise ValueError("make_kubeconfig requires at least one context")

    ca_b64 = base64.b64encode(ca_cert_pem).decode()
    user = "hatchery-agent"
    config: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": name,
                "cluster": {
                    "server": f"https://host.docker.internal:{port}",
                    "certificate-authority-data": ca_b64,
                },
            }
            for name, port in contexts
        ],
        "contexts": [{"name": name, "context": {"cluster": name, "user": user}} for name, _ in contexts],
        "current-context": contexts[0][0],
        "users": [{"name": user, "user": {"token": proxy_token}}],
    }

    # Keep the historical fixed name working for a lone unnamed/pinned context.
    if len(contexts) == 1 and contexts[0][0] != DEFAULT_CONTEXT_NAME:
        config["contexts"].append({"name": DEFAULT_CONTEXT_NAME, "context": {"cluster": contexts[0][0], "user": user}})

    # Names come from user config (EKS contexts contain ':', '/'), so dump via
    # yaml rather than interpolating into a template.
    return yaml.safe_dump(config, sort_keys=False, default_flow_style=False)
