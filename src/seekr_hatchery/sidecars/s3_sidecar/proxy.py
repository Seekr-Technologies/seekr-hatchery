"""Host-side HTTP proxy that signs S3 requests with host AWS credentials.

The sandbox is given random, proxy-only credentials and an HTTP endpoint.  Its
SigV4 signature is used only to authenticate it to this process; the proxy
removes it and signs the identical S3 request with credentials resolved on the
host.  Request and response bodies are streamed rather than buffered.
"""

from __future__ import annotations

import configparser
import contextlib
import hmac
import http.server
import logging
import re
import secrets
import socket
import ssl
import threading
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import botocore.session
import truststore
import urllib3
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.credentials import Credentials, ReadOnlyCredentials
from botocore.exceptions import BotoCoreError, ProfileNotFound

from seekr_hatchery.sidecars.http_server import ThreadingHTTPServer
from seekr_hatchery.sidecars.s3_sidecar.config import S3Config, S3Permission, S3ProfileConfig, S3Rule

logger = logging.getLogger(__name__)

_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_AUTH_CREDENTIAL_RE = re.compile(r"^AWS4-HMAC-SHA256\s+Credential=([^/,\s]+)/")
_STREAM_CHUNK_SIZE = 64 * 1024
_CLIENT_TIMEOUT_SECONDS = 60
_UPSTREAM_TIMEOUT = urllib3.Timeout(connect=10, read=60)


@dataclass(frozen=True)
class ResolvedAwsProfile:
    """Host credential source and resolved S3 endpoint for one proxy lifetime."""

    credentials: Credentials | ReadOnlyCredentials
    region: str
    endpoint_url: str

    def frozen_credentials(self) -> ReadOnlyCredentials:
        """Return current credentials, refreshing botocore providers when needed."""
        if isinstance(self.credentials, ReadOnlyCredentials):
            return self.credentials
        return self.credentials.get_frozen_credentials()


@dataclass(frozen=True)
class SyntheticCredentials:
    """Random credentials accepted only by this proxy instance."""

    access_key: str
    secret_key: str
    session_token: str

    @classmethod
    def create(cls) -> "SyntheticCredentials":
        # This is structurally an AWS access key so SDK credential validation
        # accepts it, but it is random and has no AWS account meaning.
        return cls(
            access_key=f"HATCHERYS3{secrets.token_hex(12).upper()}",
            secret_key=secrets.token_urlsafe(48),
            session_token=secrets.token_urlsafe(48),
        )


@dataclass(frozen=True)
class ProxyProfile:
    """One sandbox alias bound to host credentials and optional proxy rules."""

    alias: str
    resolved: ResolvedAwsProfile
    rules: list[S3Rule] | None
    synthetic: SyntheticCredentials


@dataclass(frozen=True)
class PolicyTarget:
    """One permission check derived from an S3 HTTP request."""

    permission: S3Permission
    bucket: str
    key: str


def _bucket_and_key(path: str) -> tuple[str, str] | None:
    parts = path.lstrip("/").split("/", 1)
    if not parts[0]:
        return None
    bucket = unquote(parts[0])
    key = unquote(parts[1]) if len(parts) == 2 else ""
    return bucket, key


def _copy_source(value: str) -> tuple[str, str] | None:
    source = urlsplit(value.lstrip("/"))
    return _bucket_and_key(source.path)


def _policy_targets(method: str, request_target: str, headers: http.client.HTTPMessage) -> list[PolicyTarget] | None:
    """Classify supported S3 data-plane requests; return None when ambiguous."""
    parsed = urlsplit(request_target)
    resource = _bucket_and_key(parsed.path)
    if resource is None:
        return None  # ListBuckets is intentionally unsupported under proxy policy.
    bucket, key = resource
    query = parse_qs(parsed.query, keep_blank_values=True)
    # Some SDKs add x-id as an informational operation label. It does not
    # change S3 routing or authorization semantics.
    query.pop("x-id", None)
    query_keys = set(query)

    if not key:
        list_keys = {
            "list-type",
            "prefix",
            "delimiter",
            "continuation-token",
            "start-after",
            "max-keys",
            "encoding-type",
            "marker",
        }
        prefixes = query.get("prefix", [""])
        if len(prefixes) != 1:
            return None
        if method in {"GET", "HEAD"} and query_keys <= list_keys:
            return [PolicyTarget("LIST", bucket, prefixes[0])]
        if (
            method == "GET"
            and "uploads" in query
            and query_keys <= (list_keys | {"uploads", "key-marker", "upload-id-marker", "max-uploads"})
        ):
            return [PolicyTarget("WRITE", bucket, prefixes[0])]
        return None

    response_keys = {key for key in query_keys if key.startswith("response-")}
    ordinary_read_keys = {"versionId", "partNumber"} | response_keys
    if method in {"GET", "HEAD"}:
        if "uploadId" in query and query_keys <= {"uploadId", "max-parts", "part-number-marker"}:
            return [PolicyTarget("WRITE", bucket, key)]
        if query_keys <= ordinary_read_keys:
            return [PolicyTarget("READ", bucket, key)]
        return None

    if method == "PUT":
        multipart_keys = {"uploadId", "partNumber"}
        if query_keys and not (multipart_keys <= query_keys and query_keys <= multipart_keys):
            return None
        targets = [PolicyTarget("WRITE", bucket, key)]
        copy_source = headers.get("X-Amz-Copy-Source")
        if copy_source:
            source = _copy_source(copy_source)
            if source is None:
                return None
            targets.append(PolicyTarget("READ", source[0], source[1]))
        return targets

    if method == "POST":
        if query_keys == {"uploads"} or ("uploadId" in query and query_keys == {"uploadId"}):
            return [PolicyTarget("WRITE", bucket, key)]
        return None

    if method == "DELETE":
        if "uploadId" in query and query_keys == {"uploadId"}:
            return [PolicyTarget("WRITE", bucket, key)]
        if query_keys <= {"versionId"}:
            return [PolicyTarget("DELETE", bucket, key)]
        return None

    return None


def _rules_allow(rules: list[S3Rule] | None, targets: list[PolicyTarget] | None) -> bool:
    """Return whether every target is covered; absent rules preserve unrestricted access."""
    if rules is None:
        return True
    if targets is None:
        return False
    return all(
        any(
            target.permission in rule.permissions
            and target.bucket == rule.bucket
            and target.key.startswith(rule.prefix)
            for rule in rules
        )
        for target in targets
    )


class _LimitedBody:
    """A non-seekable view of exactly one inbound HTTP request body."""

    def __init__(self, source: Any, length: int) -> None:
        self._source = source
        self._remaining = length

    def read(self, amount: int = -1) -> bytes:
        if self._remaining == 0:
            return b""
        if amount < 0 or amount > self._remaining:
            amount = self._remaining
        data = self._source.read(amount)
        self._remaining -= len(data)
        return data


def _header_pairs(message: http.client.HTTPMessage) -> list[tuple[str, str]]:
    """Return incoming header fields, retaining duplicate values where possible."""
    raw_items = getattr(message, "raw_items", None)
    return list(raw_items() if raw_items is not None else message.items())


def _forward_headers(message: http.client.HTTPMessage, endpoint_host: str) -> dict[str, str]:
    """Build one safe, canonicalizable header map for the upstream request."""
    connection_tokens = {
        token.strip().lower()
        for value in message.get_all("Connection", [])
        for token in value.split(",")
        if token.strip()
    }
    grouped: dict[str, tuple[str, list[str]]] = {}
    for name, value in _header_pairs(message):
        lower = name.lower()
        if lower in _HOP_BY_HOP_HEADERS or lower in connection_tokens:
            continue
        # The incoming signature identifies only synthetic credentials.  The
        # host signature below supplies its own date, payload hash and auth.
        if lower in {"authorization", "host", "x-amz-date", "x-amz-content-sha256", "x-amz-security-token"}:
            continue
        if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
            continue
        original, values = grouped.setdefault(lower, (name, []))
        values.append(value.strip())
    headers = {original: ",".join(values) for original, values in grouped.values()}
    headers["Host"] = endpoint_host
    return headers


def _safe_response_headers(headers: Any) -> list[tuple[str, str]]:
    """Filter upstream hop-by-hop and malformed response headers."""
    result: list[tuple[str, str]] = []
    items = headers.items()
    for name, value in items:
        if name.lower() in _HOP_BY_HOP_HEADERS or "\r" in name or "\n" in name or "\r" in value or "\n" in value:
            continue
        result.append((name, value))
    return result


def _sign_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: _LimitedBody | None,
    profile: ResolvedAwsProfile,
) -> dict[str, str]:
    """Return SigV4 headers for *url* without consuming a streaming body."""
    request = AWSRequest(method=method, url=url, data=body, headers=headers)
    # S3 permits UNSIGNED-PAYLOAD over TLS.  It lets uploads stream directly
    # instead of buffering potentially multi-gigabyte request bodies in RAM.
    request.context["client_config"] = Config(s3={"payload_signing_enabled": False})
    S3SigV4Auth(profile.frozen_credentials(), "s3", profile.region).add_auth(request)
    return dict(request.headers.items())


class _S3HTTPServer(ThreadingHTTPServer):
    """Threaded server that can terminate active requests during teardown."""

    def __init__(self, server_address: tuple[str, int], handler: type[http.server.BaseHTTPRequestHandler]) -> None:
        super().__init__(server_address, handler)
        self._active_sockets: set[socket.socket] = set()
        self._active_lock = threading.Lock()

    def process_request(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        with self._active_lock:
            self._active_sockets.add(request)
        super().process_request(request, client_address)

    def shutdown_request(self, request: socket.socket) -> None:
        with self._active_lock:
            self._active_sockets.discard(request)
        super().shutdown_request(request)

    def close_active_requests(self) -> None:
        """Close accepted sockets so incomplete uploads cannot outlive the sidecar."""
        with self._active_lock:
            requests = list(self._active_sockets)
        for request in requests:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            request.close()


class _S3ProxyHandler(http.server.BaseHTTPRequestHandler):
    """Dynamic handler base; factory subclasses provide proxy instance state."""

    protocol_version = "HTTP/1.1"
    profiles_by_access_key: dict[str, ProxyProfile]
    pool: Any

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(_CLIENT_TIMEOUT_SECONDS)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # BaseHTTPRequestHandler's default arguments can include the complete
        # request target. Never persist query credentials in diagnostic logs.
        logger.debug("s3 proxy request completed: %s", self.command)

    def _log_path(self) -> str:
        """Return the request path without its potentially secret query string."""
        return urlsplit(self.path).path

    def do_GET(self) -> None:  # noqa: N802
        self._proxy()

    def do_HEAD(self) -> None:  # noqa: N802
        self._proxy()

    def do_PUT(self) -> None:  # noqa: N802
        self._proxy()

    def do_POST(self) -> None:  # noqa: N802
        self._proxy()

    def do_DELETE(self) -> None:  # noqa: N802
        self._proxy()

    def do_PATCH(self) -> None:  # noqa: N802
        self._proxy()

    def _error(self, status: int, message: str) -> None:
        payload = f'{{"error":"{message}"}}'.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def _authenticated_profile(self) -> ProxyProfile | None:
        authorizations = self.headers.get_all("Authorization", [])
        tokens = self.headers.get_all("X-Amz-Security-Token", [])
        if len(authorizations) != 1 or len(tokens) != 1:
            return None
        match = _AUTH_CREDENTIAL_RE.match(authorizations[0])
        if match is None:
            return None
        profile = self.profiles_by_access_key.get(match.group(1))
        if profile is None or not hmac.compare_digest(tokens[0], profile.synthetic.session_token):
            return None
        return profile

    def _request_body(self) -> _LimitedBody | None:
        if self.headers.get("Transfer-Encoding"):
            self._error(400, "chunked request bodies are not supported by the S3 proxy")
            return None
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1:
            self._error(400, "ambiguous Content-Length")
            return None
        if not lengths:
            return _LimitedBody(self.rfile, 0)
        raw_length = lengths[0]
        try:
            length = int(raw_length)
        except ValueError:
            self._error(400, "invalid Content-Length")
            return None
        if length < 0:
            self._error(400, "invalid Content-Length")
            return None
        return _LimitedBody(self.rfile, length)

    def _proxy(self) -> None:
        profile = self._authenticated_profile()
        if profile is None:
            logger.info("s3 proxy: %s %s rejected synthetic credentials", self.command, self._log_path())
            self._error(403, "invalid synthetic S3 credentials")
            return
        targets = _policy_targets(self.command, self.path, self.headers)
        if not _rules_allow(profile.rules, targets):
            logger.info("s3 proxy: %s %s denied by profile policy", self.command, self._log_path())
            self._error(403, "S3 proxy policy denied request")
            return
        body = self._request_body()
        if body is None:
            return
        parsed = urlsplit(profile.resolved.endpoint_url)
        url = f"{profile.resolved.endpoint_url}{self.path}"
        headers = _forward_headers(self.headers, parsed.netloc)
        try:
            signed_headers = _sign_request(self.command, url, headers, body, profile.resolved)
            response = self.pool.urlopen(
                self.command,
                url,
                body=body,
                headers=signed_headers,
                preload_content=False,
                redirect=False,
                chunked=False,
                timeout=_UPSTREAM_TIMEOUT,
            )
        except Exception as exc:
            logger.warning(
                "s3 proxy: upstream error for %s %s (%s)",
                self.command,
                self._log_path(),
                type(exc).__name__,
            )
            self._error(502, "S3 upstream request failed")
            return

        try:
            self.send_response(response.status)
            for name, value in _safe_response_headers(response.headers):
                self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            while chunk := response.read(_STREAM_CHUNK_SIZE):
                self.wfile.write(chunk)
            logger.info("s3 proxy: %s %s -> %s", self.command, self._log_path(), response.status)
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            logger.warning(
                "s3 proxy: response stream aborted for %s %s (%s)",
                self.command,
                self._log_path(),
                type(exc).__name__,
            )
        finally:
            response.drain_conn()
            self.close_connection = True


@dataclass
class S3Server:
    """Running S3 proxy and the isolated credentials it exposes to a container."""

    _server: _S3HTTPServer
    _thread: threading.Thread
    profiles: dict[str, ProxyProfile]

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def container_env(self, default_alias: str) -> dict[str, str]:
        """Environment an endpoint-aware S3 client needs inside the sandbox."""
        return {
            "AWS_PROFILE": default_alias,
            "AWS_CONFIG_FILE": "/home/hatchery/.aws/config",
            "AWS_SHARED_CREDENTIALS_FILE": "/home/hatchery/.aws/credentials",
            "AWS_ENDPOINT_URL_S3": f"http://host.docker.internal:{self.port}",
            "AWS_EC2_METADATA_DISABLED": "true",
        }

    def write_client_files(self, directory: Path) -> Path:
        """Write sandbox-only named profiles containing synthetic credentials."""
        directory.mkdir(parents=True, exist_ok=True)
        config = configparser.RawConfigParser()
        credentials = configparser.RawConfigParser()
        for alias, profile in self.profiles.items():
            config[f"profile {alias}"] = {
                "region": profile.resolved.region,
                "s3": "\naddressing_style = path",
            }
            credentials[alias] = {
                "aws_access_key_id": profile.synthetic.access_key,
                "aws_secret_access_key": profile.synthetic.secret_key,
                "aws_session_token": profile.synthetic.session_token,
            }
        config_path = directory / "config"
        credentials_path = directory / "credentials"
        with config_path.open("w") as file:
            config.write(file)
        with credentials_path.open("w") as file:
            credentials.write(file)
        config_path.chmod(0o600)
        credentials_path.chmod(0o600)
        return directory

    def close(self) -> None:
        self._server.shutdown()
        self._server.close_active_requests()
        self._server.server_close()
        self._thread.join(timeout=5)
        pool = self._server.RequestHandlerClass.pool
        clear = getattr(pool, "clear", None)
        if clear is not None:
            clear()


def validate_host_profiles_exist(config: S3Config) -> None:
    """Verify that every syntactically valid profile exists in the host AWS config."""
    available = sorted(botocore.session.Session().available_profiles)
    available_set = set(available)
    for profile_name in config.profiles:
        if profile_name not in available_set:
            existing = ", ".join(available) if available else "(none)"
            raise RuntimeError(f"s3: AWS profile {profile_name!r} not found; existing profiles: {existing}")


def resolve_aws_profile(profile_name: str, config: S3ProfileConfig) -> ResolvedAwsProfile:
    """Resolve the configured host profile and its regional public S3 endpoint.

    Botocore owns the AWS credential chain, including shared profiles,
    credential_process, assume-role and renewable temporary credentials. The
    resolved source stays host-side; only a frozen copy is used for each
    upstream request signature.
    """
    try:
        session = botocore.session.Session(profile=profile_name)
        credentials = session.get_credentials()
        if credentials is None:
            raise RuntimeError(f"s3: no AWS credentials resolved for profile {profile_name!r}")
        # Resolve once during startup so missing/expired credentials fail before
        # a sandbox is launched. Refreshable credentials are fetched again per
        # request by ResolvedAwsProfile.frozen_credentials().
        credentials.get_frozen_credentials()
        region = config.region or session.get_config_variable("region") or "us-east-1"
        endpoint = session.get_component("endpoint_resolver").construct_endpoint("s3", region)
        if endpoint is None or "hostname" not in endpoint:
            raise RuntimeError(f"s3: no AWS S3 endpoint is available for region {region!r}")
        return ResolvedAwsProfile(credentials, region, f"https://{endpoint['hostname']}")
    except ProfileNotFound as exc:
        raise RuntimeError(f"s3: AWS profile {profile_name!r} was not found") from exc
    except BotoCoreError as exc:
        raise RuntimeError(f"s3: could not resolve AWS credentials from {profile_name}: {exc}") from exc


@contextlib.contextmanager
def s3_server(
    resolved_profiles: dict[str, tuple[ResolvedAwsProfile, list[S3Rule] | None]],
    *,
    _pool: Any | None = None,
) -> Generator[S3Server, None, None]:
    """Start a container-reachable S3 proxy and stop it after the launch ends."""
    profiles = {
        alias: ProxyProfile(alias, resolved, rules, SyntheticCredentials.create())
        for alias, (resolved, rules) in resolved_profiles.items()
    }
    profiles_by_access_key = {profile.synthetic.access_key: profile for profile in profiles.values()}
    pool = _pool or urllib3.PoolManager(maxsize=16, ssl_context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT))

    class Handler(_S3ProxyHandler):
        pass

    Handler.profiles_by_access_key = profiles_by_access_key
    Handler.pool = pool
    # Containers reach this process through the host gateway rather than the
    # host loopback interface. Synthetic credentials authenticate every request.
    server = _S3HTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="hatchery-s3-proxy")
    thread.start()
    running = S3Server(server, thread, profiles)
    try:
        yield running
    finally:
        running.close()
