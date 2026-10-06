"""HTTP transport and lifecycle for the host-side S3 credential proxy."""

from __future__ import annotations

import configparser
import contextlib
import hmac
import http.server
import logging
import re
import socket
import ssl
import threading
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import truststore
import urllib3

import seekr_hatchery.sidecars.s3_sidecar.credentials as credentials
import seekr_hatchery.sidecars.s3_sidecar.policy as policy
from seekr_hatchery.sidecars.http_server import ThreadingHTTPServer
from seekr_hatchery.sidecars.s3_sidecar.config import S3Rule

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
class ProxyProfile:
    """One sandbox alias bound to host credentials and optional proxy rules."""

    alias: str
    resolved: credentials.ResolvedAwsProfile
    rules: list[S3Rule] | None
    synthetic: credentials.SyntheticCredentials


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
    result: list[tuple[str, str]] = []
    for name, value in headers.items():
        if name.lower() in _HOP_BY_HOP_HEADERS or "\r" in name or "\n" in name or "\r" in value or "\n" in value:
            continue
        result.append((name, value))
    return result


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
        logger.debug("s3 proxy request completed: %s", self.command)

    def _log_path(self) -> str:
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
        try:
            length = int(lengths[0])
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
        targets = policy.policy_targets(self.command, self.path, self.headers)
        if not policy.rules_allow(profile.rules, targets):
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
            signed_headers = credentials.sign_request(self.command, url, headers, body, profile.resolved)
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
    """Running S3 proxy and isolated credentials exposed to a container."""

    _server: _S3HTTPServer
    _thread: threading.Thread
    profiles: dict[str, ProxyProfile]

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def container_env(self, default_alias: str) -> dict[str, str]:
        return {
            "AWS_PROFILE": default_alias,
            "AWS_CONFIG_FILE": "/home/hatchery/.aws/config",
            "AWS_SHARED_CREDENTIALS_FILE": "/home/hatchery/.aws/credentials",
            "AWS_ENDPOINT_URL_S3": f"http://host.docker.internal:{self.port}",
            "AWS_EC2_METADATA_DISABLED": "true",
        }

    def write_client_files(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        config = configparser.RawConfigParser()
        client_credentials = configparser.RawConfigParser()
        for alias, profile in self.profiles.items():
            config[f"profile {alias}"] = {
                "region": profile.resolved.region,
                "s3": "\naddressing_style = path",
            }
            client_credentials[alias] = {
                "aws_access_key_id": profile.synthetic.access_key,
                "aws_secret_access_key": profile.synthetic.secret_key,
                "aws_session_token": profile.synthetic.session_token,
            }
        config_path = directory / "config"
        credentials_path = directory / "credentials"
        with config_path.open("w") as file:
            config.write(file)
        with credentials_path.open("w") as file:
            client_credentials.write(file)
        config_path.chmod(0o600)
        credentials_path.chmod(0o600)
        return directory

    def close(self) -> None:
        self._server.shutdown()
        self._server.close_active_requests()
        self._server.server_close()
        self._thread.join(timeout=5)
        clear = getattr(self._server.RequestHandlerClass.pool, "clear", None)
        if clear is not None:
            clear()


@contextlib.contextmanager
def s3_server(
    resolved_profiles: dict[str, tuple[credentials.ResolvedAwsProfile, list[S3Rule] | None]],
    *,
    _pool: Any | None = None,
) -> Generator[S3Server, None, None]:
    """Start a container-reachable S3 proxy and stop it after launch."""
    profiles = {
        alias: ProxyProfile(alias, resolved, rules, credentials.SyntheticCredentials.create())
        for alias, (resolved, rules) in resolved_profiles.items()
    }
    profiles_by_access_key = {profile.synthetic.access_key: profile for profile in profiles.values()}
    pool = _pool or urllib3.PoolManager(maxsize=16, ssl_context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT))

    class Handler(_S3ProxyHandler):
        pass

    Handler.profiles_by_access_key = profiles_by_access_key
    Handler.pool = pool
    server = _S3HTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="hatchery-s3-proxy")
    thread.start()
    running = S3Server(server, thread, profiles)
    try:
        yield running
    finally:
        running.close()
