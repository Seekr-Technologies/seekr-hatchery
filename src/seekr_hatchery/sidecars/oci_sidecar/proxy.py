"""Host-side OCI Object Storage proxy using isolated sandbox credentials."""

from __future__ import annotations

import base64
import configparser
import contextlib
import email.utils
import hmac
import http.server
import logging
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import truststore
import urllib3

from seekr_hatchery.sidecars.http_server import ThreadingHTTPServer
from seekr_hatchery.sidecars.oci_sidecar.config import OciConfig, OciPermission, OciProfileConfig, OciRule

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
        # This proxy completes the client-facing 100-continue handshake. The
        # upstream connection must negotiate its own body transfer.
        "expect",
    }
)
_SIGNATURE_PARAMETER_RE = re.compile(r'(\w+)="([^"]*)"')
_STREAM_CHUNK_SIZE = 64 * 1024
_CLIENT_TIMEOUT_SECONDS = 60
_UPSTREAM_TIMEOUT = urllib3.Timeout(connect=10, read=60)
_BODY_METHODS = frozenset({"PUT", "POST", "PATCH"})
_REQUIRED_PROFILE_FIELDS = ("tenancy", "user", "fingerprint", "key_file", "region")


@dataclass(frozen=True)
class ResolvedOciProfile:
    """Host API-key identity and regional Object Storage endpoint."""

    profile_name: str
    key_id: str
    key_file: Path
    pass_phrase: str | None
    region: str
    endpoint_url: str


@dataclass(frozen=True)
class SyntheticIdentity:
    """Random OCI identity accepted only by one proxy instance."""

    key_id: str
    key_filename: str

    @classmethod
    def create(cls, index: int) -> "SyntheticIdentity":
        tenancy = f"ocid1.tenancy.oc1..hatchery{secrets.token_hex(24)}"
        user = f"ocid1.user.oc1..hatchery{secrets.token_hex(24)}"
        fingerprint = ":".join(f"{byte:02x}" for byte in secrets.token_bytes(16))
        return cls(f"{tenancy}/{user}/{fingerprint}", f"profile-{index}.pem")


@dataclass(frozen=True)
class ProxyProfile:
    """One sandbox profile bound to a host identity and optional rules."""

    name: str
    resolved: ResolvedOciProfile
    rules: list[OciRule] | None
    synthetic: SyntheticIdentity


@dataclass(frozen=True)
class PolicyTarget:
    """One permission check derived from an Object Storage request."""

    permission: OciPermission
    namespace: str
    bucket: str
    object_name: str


class _LimitedBody:
    """A non-seekable view of exactly one inbound request body."""

    def __init__(self, source: Any, length: int) -> None:
        self._source = source
        self._length = length
        self._remaining = length

    @property
    def length(self) -> int:
        return self._length

    @property
    def bytes_read(self) -> int:
        return self._length - self._remaining

    def read(self, amount: int = -1) -> bytes:
        if self._remaining == 0:
            return b""
        if amount < 0 or amount > self._remaining:
            amount = self._remaining
        data = self._source.read(amount)
        self._remaining -= len(data)
        return data


def _policy_targets(method: str, request_target: str) -> list[PolicyTarget] | None:
    """Classify supported OCI Object Storage data-plane requests."""
    parsed = urlsplit(request_target)
    parts = parsed.path.lstrip("/").split("/")
    if len(parts) < 4 or parts[0] != "n" or parts[2] != "b":
        return None
    namespace = unquote(parts[1])
    bucket = unquote(parts[3])
    if not namespace or not bucket:
        return None

    tail = parts[4:]
    query = parse_qs(parsed.query, keep_blank_values=True)
    if not tail:
        if method in {"GET", "HEAD"}:
            return [PolicyTarget("LIST", namespace, bucket, "")]
        return None

    collection = tail[0]
    object_name = unquote("/".join(tail[1:])) if len(tail) > 1 else ""
    if collection == "o":
        if not object_name:
            if method != "GET":
                return None
            prefixes = query.get("prefix", [""])
            if len(prefixes) != 1:
                return None
            allowed_query = {
                "prefix",
                "start",
                "end",
                "limit",
                "delimiter",
                "fields",
                "startAfter",
            }
            if not set(query) <= allowed_query:
                return None
            return [PolicyTarget("LIST", namespace, bucket, prefixes[0])]
        if method in {"GET", "HEAD"}:
            return [PolicyTarget("READ", namespace, bucket, object_name)]
        if method == "PUT":
            return [PolicyTarget("WRITE", namespace, bucket, object_name)]
        if method == "DELETE":
            return [PolicyTarget("DELETE", namespace, bucket, object_name)]
        return None

    if collection == "u":
        if not object_name:
            if method == "GET":
                prefixes = query.get("prefix", [""])
                if len(prefixes) == 1:
                    return [PolicyTarget("LIST", namespace, bucket, prefixes[0])]
            return None
        if method in {"GET", "PUT", "POST", "DELETE"}:
            return [PolicyTarget("WRITE", namespace, bucket, object_name)]
        return None

    return None


def _rules_allow(rules: list[OciRule] | None, targets: list[PolicyTarget] | None) -> bool:
    """Return whether every target is covered; omitted rules preserve host access."""
    if rules is None:
        return True
    if targets is None:
        return False
    return all(
        any(
            target.permission in rule.permissions
            and target.namespace == rule.namespace
            and target.bucket == rule.bucket
            and target.object_name.startswith(rule.prefix)
            for rule in rules
        )
        for target in targets
    )


def _sanitized_exception_message(exc: Exception) -> str:
    """Return bounded diagnostics without query strings or credential values."""
    message = " ".join(str(exc).split())
    message = re.sub(r"(https?://[^?\s]+)\?[^\s]*", r"\1?<redacted>", message)
    message = re.sub(
        r"(?i)\b(authorization|signature|token|keyid)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^,\s]+)",
        r"\1=<redacted>",
        message,
    )
    return message[:500] or "(no exception message)"


def _safe_request_id(value: str | None) -> str:
    if not value:
        return "-"
    return re.sub(r"[^A-Za-z0-9._:-]", "_", value)[:128]


def _signature_key_id(value: str) -> str | None:
    if not value.startswith("Signature "):
        return None
    parameters = dict(_SIGNATURE_PARAMETER_RE.findall(value[len("Signature ") :]))
    if parameters.get("algorithm") != "rsa-sha256":
        return None
    return parameters.get("keyId")


def _header_pairs(message: http.client.HTTPMessage) -> list[tuple[str, str]]:
    raw_items = getattr(message, "raw_items", None)
    return list(raw_items() if raw_items is not None else message.items())


def _forward_headers(message: http.client.HTTPMessage, endpoint_host: str) -> dict[str, str]:
    """Build a safe upstream header map without the synthetic signature."""
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
        if lower in {"authorization", "host", "date"}:
            continue
        if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
            continue
        original, values = grouped.setdefault(lower, (name, []))
        values.append(value.strip())
    headers = {original: ",".join(values) for original, values in grouped.values()}
    headers["host"] = endpoint_host
    headers["date"] = email.utils.formatdate(usegmt=True)
    return headers


def _safe_response_headers(headers: Any) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for name, value in headers.items():
        if name.lower() in _HOP_BY_HOP_HEADERS or "\r" in name or "\n" in name or "\r" in value or "\n" in value:
            continue
        result.append((name, value))
    return result


def _case_insensitive_value(headers: dict[str, str], name: str) -> str | None:
    return next((value for key, value in headers.items() if key.lower() == name.lower()), None)


def _sign_request(
    method: str,
    request_target: str,
    headers: dict[str, str],
    profile: ResolvedOciProfile,
) -> dict[str, str]:
    """Sign an upstream request with the host profile's API key."""
    signed_names = ["date", "(request-target)", "host"]
    if method in _BODY_METHODS:
        for required in ("content-length", "content-type", "x-content-sha256"):
            if _case_insensitive_value(headers, required) is None:
                raise RuntimeError(f"oci: request body is missing required signing header {required!r}")
        signed_names.extend(["content-length", "content-type", "x-content-sha256"])

    lines: list[str] = []
    for name in signed_names:
        if name == "(request-target)":
            value = f"{method.lower()} {request_target}"
        else:
            value = _case_insensitive_value(headers, name)
            if value is None:
                raise RuntimeError(f"oci: request is missing signing header {name!r}")
        lines.append(f"{name}: {value}")

    env = os.environ.copy()
    command = ["openssl", "dgst", "-sha256", "-sign", str(profile.key_file)]
    if profile.pass_phrase is not None:
        env["HATCHERY_OCI_KEY_PASSPHRASE"] = profile.pass_phrase
        command.extend(["-passin", "env:HATCHERY_OCI_KEY_PASSPHRASE"])
    result = subprocess.run(
        command,
        input="\n".join(lines).encode(),
        capture_output=True,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError("oci: failed to sign request with the configured API key")
    signature = base64.b64encode(result.stdout).decode()
    headers["Authorization"] = (
        'Signature algorithm="rsa-sha256",'
        f'headers="{" ".join(signed_names)}",'
        f'keyId="{profile.key_id}",'
        f'signature="{signature}",version="1"'
    )
    return headers


class _OciHTTPServer(ThreadingHTTPServer):
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


class _OciProxyHandler(http.server.BaseHTTPRequestHandler):
    """Dynamic handler base; factory subclasses provide proxy state."""

    protocol_version = "HTTP/1.1"
    profiles_by_key_id: dict[str, ProxyProfile]
    pool: Any

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(_CLIENT_TIMEOUT_SECONDS)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug("oci proxy request completed: %s", self.command)

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
        if len(authorizations) != 1:
            return None
        key_id = _signature_key_id(authorizations[0])
        if key_id is None:
            return None
        return next(
            (profile for expected, profile in self.profiles_by_key_id.items() if hmac.compare_digest(key_id, expected)),
            None,
        )

    def _request_body(self) -> _LimitedBody | None:
        if self.headers.get("Transfer-Encoding"):
            self._error(400, "chunked request bodies are not supported by the OCI proxy")
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
            logger.info("oci proxy: %s %s rejected synthetic credentials", self.command, self._log_path())
            self._error(403, "invalid synthetic OCI credentials")
            return
        targets = _policy_targets(self.command, self.path)
        if not _rules_allow(profile.rules, targets):
            logger.info("oci proxy: %s %s denied by profile policy", self.command, self._log_path())
            self._error(403, "OCI proxy policy denied request")
            return
        body = self._request_body()
        if body is None:
            return

        parsed_endpoint = urlsplit(profile.resolved.endpoint_url)
        headers = _forward_headers(self.headers, parsed_endpoint.netloc)
        request_id = _safe_request_id(self.headers.get("opc-client-request-id"))
        logger.info(
            "oci proxy: phase=prepare method=%s path=%s host=%s content_length=%d opc_client_request_id=%s",
            self.command,
            self._log_path(),
            parsed_endpoint.netloc,
            body.length,
            request_id,
        )
        logger.debug(
            "oci proxy: phase=prepare method=%s path=%s upstream_headers=%s",
            self.command,
            self._log_path(),
            sorted(name.lower() for name in headers),
        )

        try:
            signed_headers = _sign_request(self.command, self.path, headers, profile.resolved)
        except Exception as exc:
            logger.warning(
                "oci proxy: phase=sign method=%s path=%s error_type=%s detail=%s "
                "body_bytes=%d/%d opc_client_request_id=%s",
                self.command,
                self._log_path(),
                type(exc).__name__,
                _sanitized_exception_message(exc),
                body.bytes_read,
                body.length,
                request_id,
            )
            logger.debug("oci proxy signing exception", exc_info=True)
            self._error(502, "OCI upstream signing failed")
            return

        logger.debug(
            "oci proxy: phase=signed method=%s path=%s signed_headers=%s",
            self.command,
            self._log_path(),
            sorted(name.lower() for name in signed_headers),
        )
        upstream_started = time.monotonic()
        try:
            response = self.pool.urlopen(
                self.command,
                f"{profile.resolved.endpoint_url}{self.path}",
                body=body,
                headers=signed_headers,
                preload_content=False,
                redirect=False,
                chunked=False,
                timeout=_UPSTREAM_TIMEOUT,
            )
        except Exception as exc:
            logger.warning(
                "oci proxy: phase=upstream method=%s path=%s host=%s elapsed=%.3fs "
                "error_type=%s detail=%s body_bytes=%d/%d opc_client_request_id=%s",
                self.command,
                self._log_path(),
                parsed_endpoint.netloc,
                time.monotonic() - upstream_started,
                type(exc).__name__,
                _sanitized_exception_message(exc),
                body.bytes_read,
                body.length,
                request_id,
            )
            logger.debug("oci proxy upstream exception", exc_info=True)
            self._error(502, "OCI upstream request failed")
            return

        try:
            self.send_response(response.status)
            for name, value in _safe_response_headers(response.headers):
                self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            while chunk := response.read(_STREAM_CHUNK_SIZE):
                self.wfile.write(chunk)
            upstream_request_id = _safe_request_id(
                next(
                    (value for name, value in response.headers.items() if name.lower() == "opc-request-id"),
                    None,
                )
            )
            logger.info(
                "oci proxy: phase=complete method=%s path=%s status=%s elapsed=%.3fs "
                "body_bytes=%d/%d opc_client_request_id=%s opc_request_id=%s",
                self.command,
                self._log_path(),
                response.status,
                time.monotonic() - upstream_started,
                body.bytes_read,
                body.length,
                request_id,
                upstream_request_id,
            )
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            logger.warning(
                "oci proxy: response stream aborted for %s %s (%s)",
                self.command,
                self._log_path(),
                type(exc).__name__,
            )
        finally:
            response.drain_conn()
            self.close_connection = True


@dataclass
class OciServer:
    """Running OCI proxy and isolated identities exposed to the sandbox."""

    _server: _OciHTTPServer
    _thread: threading.Thread
    profiles: dict[str, ProxyProfile]

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def container_env(self, default_profile: str) -> dict[str, str]:
        return {
            "OCI_CONFIG_FILE": "/home/hatchery/.oci/config",
            "OCI_CLI_PROFILE": default_profile,
            "OCI_CLI_ENDPOINT": f"http://host.docker.internal:{self.port}",
            "OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING": "True",
        }

    def write_client_files(self, directory: Path) -> Path:
        """Write sandbox profiles and generate their synthetic API keys."""
        directory.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        for profile in self.profiles.values():
            key_path = directory / profile.synthetic.key_filename
            result = subprocess.run(
                ["openssl", "genrsa", "-out", str(key_path), "2048"],
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError("oci: failed to generate a synthetic sandbox API key")
            key_data = key_path.read_bytes()
            separator = b"" if key_data.endswith(b"\n") else b"\n"
            key_path.write_bytes(key_data + separator + b"OCI_API_KEY\n")
            key_path.chmod(0o600)
            lines.extend(
                [
                    f"[{profile.name}]",
                    f"user={profile.synthetic.key_id.split('/')[1]}",
                    f"fingerprint={profile.synthetic.key_id.split('/')[2]}",
                    f"tenancy={profile.synthetic.key_id.split('/')[0]}",
                    f"region={profile.resolved.region}",
                    f"key_file=/home/hatchery/.oci/{profile.synthetic.key_filename}",
                    "",
                ]
            )
        config_path = directory / "config"
        config_path.write_text("\n".join(lines))
        config_path.chmod(0o600)
        return directory

    def close(self) -> None:
        self._server.shutdown()
        self._server.close_active_requests()
        self._server.server_close()
        self._thread.join(timeout=5)
        clear = getattr(self._server.RequestHandlerClass.pool, "clear", None)
        if clear is not None:
            clear()


def _load_config(config: OciConfig) -> tuple[configparser.ConfigParser, Path]:
    path = Path(os.path.expandvars(os.path.expanduser(config.config_file)))
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(path):
        raise RuntimeError(f"oci: config file not found: {path}")
    return parser, path


def _available_profiles(parser: configparser.ConfigParser) -> list[str]:
    profiles = list(parser.sections())
    if parser.defaults():
        profiles.insert(0, "DEFAULT")
    return profiles


def _profile_values(parser: configparser.ConfigParser, profile_name: str) -> dict[str, str]:
    if profile_name == "DEFAULT":
        return dict(parser.defaults())
    return dict(parser[profile_name])


def validate_host_profiles_exist(config: OciConfig) -> None:
    """Verify profiles, API-key fields, key files, and OpenSSL before build."""
    if shutil.which("openssl") is None:
        raise RuntimeError("oci: openssl is required to isolate and sign API-key credentials")
    parser, _ = _load_config(config)
    available = _available_profiles(parser)
    for profile_name in config.profiles:
        if profile_name not in available:
            existing = ", ".join(sorted(available)) if available else "(none)"
            raise RuntimeError(f"oci: profile {profile_name!r} not found; existing profiles: {existing}")
        values = _profile_values(parser, profile_name)
        missing = [field for field in _REQUIRED_PROFILE_FIELDS if not values.get(field)]
        if missing:
            raise RuntimeError(f"oci: profile {profile_name!r} is missing required fields: {', '.join(missing)}")
        key_file = Path(os.path.expandvars(os.path.expanduser(values["key_file"])))
        if not key_file.is_file():
            raise RuntimeError(f"oci: key file for profile {profile_name!r} not found: {key_file}")


def resolve_oci_profile(
    config: OciConfig,
    profile_name: str,
    profile_config: OciProfileConfig,
) -> ResolvedOciProfile:
    """Resolve one validated standard OCI API-key profile."""
    parser, _ = _load_config(config)
    values = _profile_values(parser, profile_name)
    region = values["region"]
    endpoint = profile_config.endpoint or f"https://objectstorage.{region}.oraclecloud.com"
    key_file = Path(os.path.expandvars(os.path.expanduser(values["key_file"])))
    return ResolvedOciProfile(
        profile_name=profile_name,
        key_id=f"{values['tenancy']}/{values['user']}/{values['fingerprint']}",
        key_file=key_file,
        pass_phrase=values.get("pass_phrase"),
        region=region,
        endpoint_url=endpoint.rstrip("/"),
    )


@contextlib.contextmanager
def oci_server(
    resolved_profiles: dict[str, tuple[ResolvedOciProfile, list[OciRule] | None]],
    *,
    _pool: Any | None = None,
) -> Generator[OciServer, None, None]:
    """Start a container-reachable OCI proxy and stop it after launch."""
    profiles = {
        name: ProxyProfile(name, resolved, rules, SyntheticIdentity.create(index))
        for index, (name, (resolved, rules)) in enumerate(resolved_profiles.items())
    }
    profiles_by_key_id = {profile.synthetic.key_id: profile for profile in profiles.values()}
    pool = _pool or urllib3.PoolManager(maxsize=16, ssl_context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT))

    class Handler(_OciProxyHandler):
        pass

    Handler.profiles_by_key_id = profiles_by_key_id
    Handler.pool = pool
    server = _OciHTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="hatchery-oci-proxy")
    thread.start()
    running = OciServer(server, thread, profiles)
    try:
        yield running
    finally:
        running.close()
