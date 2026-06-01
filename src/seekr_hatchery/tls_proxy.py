"""Host-side TLS/HTTPS reverse proxy for secure API key injection.

This proxy intercepts HTTPS traffic inside the container by terminating TLS
using an ephemeral self-signed leaf certificate signed by a custom ephemeral CA.
The decrypted request is then validated, mutated to inject real credentials,
and forwarded to the upstream Google APIs securely via HTTPS.
"""

import datetime
import http.server
import logging
import ssl
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import urllib3
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import seekr_hatchery.proxy as proxy

logger = logging.getLogger("hatchery")


def generate_ca() -> tuple[bytes, bytes]:
    """Generate an ephemeral self-signed CA certificate and private key.

    Returns:
        tuple[bytes, bytes]: (ca_cert_pem, ca_key_pem)
    """
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "Seekr Hatchery Ephemeral CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Seekr Technologies"),
        ]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None),
            critical=True,
        )
        .sign(private_key, hashes.SHA256())
    )
    cert_bytes = ca_cert.public_bytes(serialization.Encoding.PEM)
    key_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_bytes, key_bytes


def generate_leaf_cert(hostname: str, ca_cert_pem: bytes, ca_key_pem: bytes) -> tuple[bytes, bytes]:
    """Generate a leaf certificate for the given hostname, signed by the CA.

    Returns:
        tuple[bytes, bytes]: (leaf_cert_pem, leaf_key_pem)
    """
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
    ca_key = serialization.load_pem_private_key(ca_key_pem, password=None)

    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        ]
    )
    now = datetime.datetime.now(datetime.timezone.utc)

    dns_names = [x509.DNSName(hostname)]
    if hostname == "googleapis.com" or hostname.endswith(".googleapis.com"):
        if "*.googleapis.com" not in hostname:
            dns_names.append(x509.DNSName("*.googleapis.com"))
        if hostname != "googleapis.com":
            dns_names.append(x509.DNSName("googleapis.com"))

    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName(dns_names),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    cert_bytes = leaf_cert.public_bytes(serialization.Encoding.PEM)
    key_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_bytes, key_bytes


class _ThreadingHTTPSServer(proxy._ThreadingHTTPServer):
    """Internal threading HTTPS server — one thread per request."""

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[http.server.BaseHTTPRequestHandler],
        ssl_context: ssl.SSLContext,
        bind_and_activate: bool = True,
    ) -> None:
        self.ssl_context = ssl_context
        super().__init__(server_address, RequestHandlerClass, bind_and_activate)

    def get_request(self) -> tuple[Any, Any]:
        sock, addr = super().get_request()
        try:
            ssl_sock = self.ssl_context.wrap_socket(sock, server_side=True)
            return ssl_sock, addr
        except Exception as e:
            logger.debug("TLS handshake failed from %s: %s", addr, e)
            try:
                sock.close()
            except Exception:
                pass
            raise


@contextmanager
def tls_api_server(
    header_mutator: Callable[..., dict[str, str]],
    proxy_token: str,
    cert_file: Path,
    key_file: Path,
    target_host: str = "daily-cloudcode-pa.googleapis.com",
    path_prefix: str = "",
    *,
    _pool: urllib3.PoolManager | None = None,
) -> Generator[proxy.APIServer, None, None]:
    """Start the TLS-terminating reverse proxy, yield APIServer, and shut down on exit."""

    class _BoundHandler(proxy._ProxyHandler):
        pass

    _BoundHandler.header_mutator = staticmethod(header_mutator)
    _BoundHandler.proxy_token = proxy_token
    _BoundHandler.target_host = target_host
    _BoundHandler.path_prefix = path_prefix
    _BoundHandler.pool = _pool if _pool is not None else urllib3.PoolManager(maxsize=16)

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))

    server = _ThreadingHTTPSServer(("0.0.0.0", 0), _BoundHandler, ssl_context)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.debug(
        "TLS Proxy started on port %d (target=%s prefix=%r)",
        server.server_address[1],
        target_host,
        path_prefix,
    )
    try:
        yield proxy.APIServer(server)
    finally:
        server.shutdown()
        server.server_close()
        logger.debug("TLS Proxy stopped")
