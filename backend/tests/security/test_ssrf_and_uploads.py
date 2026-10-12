"""The data-fetch boundary must not become a way into the internal network."""

from __future__ import annotations

import socket
import ssl
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from iluvtrade.common import storage
from iluvtrade.config import Settings
from iluvtrade.data import fetch as fetch_module
from iluvtrade.data.fetch import FetchError, fetch_csv, validate_url
from tests.support import session_date_ist

pytestmark = pytest.mark.security


def _settings(**overrides) -> Settings:
    base = {
        "fetch_allowed_hosts": ("data.example.com",),
        "fetch_allow_loopback": False,
        "max_fetch_bytes": 1024 * 1024,
        "fetch_timeout_seconds": 5.0,
        "fetch_max_redirects": 2,
    }
    return Settings(**{**base, **overrides})


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://evil.example.com/",
        "ftp://data.example.com/x.csv",
        "jar:http://data.example.com/x.csv",
    ],
)
def test_non_http_schemes_are_refused(url: str) -> None:
    with pytest.raises(FetchError, match=r"not fetchable|no host"):
        validate_url(url, _settings())


def test_an_empty_allowlist_permits_nothing() -> None:
    """An allowlist that defaults to 'everything' is not an allowlist."""

    with pytest.raises(FetchError, match="No data-source hosts are allowed"):
        validate_url("https://anything.example.com/x.csv", _settings(fetch_allowed_hosts=()))


def test_a_host_outside_the_allowlist_is_refused() -> None:
    with pytest.raises(FetchError, match="not in this deployment's data-source allowlist"):
        validate_url("https://evil.example.com/x.csv", _settings())


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "127.0.0.1",
        "127.0.0.2",  # the whole loopback /8, not just .1
        "0.0.0.0",  # "this host"
        "169.254.169.254",  # AWS / GCP / Azure instance metadata
        "169.254.170.2",  # ECS task metadata
        "10.0.0.1",  # RFC1918
        "172.16.0.1",  # RFC1918
        "192.168.1.1",  # RFC1918
        "100.64.0.1",  # carrier-grade NAT, shared address space
        "192.0.0.1",  # IETF protocol assignments
        "240.0.0.1",  # reserved
    ],
)
def test_private_and_metadata_addresses_are_refused(host: str) -> None:
    """The cloud metadata service and every non-public range.

    ``169.254.169.254`` is the one that matters most: reaching it from a server
    yields instance credentials on every major cloud.
    """

    settings = _settings(fetch_allowed_hosts=(host,))
    with pytest.raises(FetchError):
        validate_url(f"https://{host}/x.csv", settings)


@pytest.mark.parametrize(
    "host",
    [
        "[::1]",  # IPv6 loopback
        "[::]",  # unspecified
        "[fe80::1]",  # link-local
        "[fc00::1]",  # unique local
        "[fd00::1]",  # unique local
        "[::ffff:127.0.0.1]",  # IPv4-mapped loopback
        "[::ffff:169.254.169.254]",  # IPv4-mapped metadata
    ],
)
def test_ipv6_private_and_mapped_addresses_are_refused(host: str) -> None:
    """IPv6 has its own private ranges, and can wrap an IPv4 address.

    ``::ffff:169.254.169.254`` is the metadata service written as IPv6; a check
    that only understood dotted-quad notation would let it through.
    """

    settings = _settings(fetch_allowed_hosts=(host.strip("[]"),))
    with pytest.raises(FetchError):
        validate_url(f"https://{host}/x.csv", settings)


def test_a_hostname_resolving_to_a_private_address_is_refused() -> None:
    """The allowlist is not enough on its own.

    An attacker who controls a DNS record for an allowlisted host — or a host
    that legitimately resolves inward — would otherwise reach the internal
    network. The check is on the *resolved address*, which is why
    ``localhost.localdomain`` and friends do not help.
    """

    settings = _settings(fetch_allowed_hosts=("localhost",))
    with pytest.raises(FetchError, match="not a public address"):
        validate_url("https://localhost/x.csv", settings)


def test_the_address_check_runs_on_every_redirect_hop(local_server: int) -> None:
    """Stated separately from the redirect test because it is the bypass.

    An allowlisted, genuinely public host answering 302 to the metadata service
    is the standard way past a host-only allowlist.
    """

    from iluvtrade.data.fetch import fetch_csv

    with pytest.raises(FetchError):
        fetch_csv(f"http://127.0.0.1:{local_server}/redirect", _loopback_settings(local_server))


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@data.example.com/x.csv",  # credentials in the URL
        "https://data.example.com@evil.example.com/x.csv",  # userinfo confusion
        "https:///x.csv",  # no host
        "https://#@data.example.com/x.csv",  # fragment confusion
    ],
)
def test_malformed_and_confusing_urls_are_refused(url: str) -> None:
    """A URL whose real host is not the one a reader would guess.

    ``https://data.example.com@evil.example.com/`` points at *evil*, and a
    human skim-reading it sees the allowlisted host.
    """

    with pytest.raises(FetchError):
        validate_url(url, _settings())


def test_an_unusual_port_is_refused() -> None:
    """An allowlisted host must not become a route to SSH on that host."""

    with pytest.raises(FetchError, match="not permitted"):
        validate_url("https://data.example.com:22/x.csv", _settings())


class _Server(BaseHTTPRequestHandler):
    """A local server that answers each path with a different hazard."""

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/huge":
            body = b"a,b\n" + b"1,2\n" * 500_000
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/html":
            body = b"<html>login page</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
        elif self.path == "/ok":
            body = b"Date,Symbol,Close\n2024-01-01,ACME,100.5\n2024-01-02,ACME,101.5\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/identity":
            body = f"{self.server.server_address[0]} {self.headers['Host']}\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def local_server():
    server = HTTPServer(("127.0.0.1", 0), _Server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


def _loopback_settings(port: int, **overrides) -> Settings:
    """Settings that reach the local test server, and nothing else.

    The loopback and port allowances are granted explicitly, one port at a time,
    so a test cannot accidentally pass because the controls were disabled
    wholesale.
    """

    return _settings(
        fetch_allowed_hosts=("127.0.0.1",),
        fetch_allow_loopback=True,
        fetch_allowed_ports=(port,),
        **overrides,
    )


def test_an_allowed_source_is_fetched(local_server: int) -> None:
    result = fetch_csv(f"http://127.0.0.1:{local_server}/ok", _loopback_settings(local_server))
    assert b"ACME" in result.content
    assert result.content_type.startswith("text/csv")


def test_dns_rebinding_cannot_change_the_connected_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changing hostname remains pinned to the address checked for this hop."""

    server = HTTPServer(("127.0.0.1", 0), _Server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    original_getaddrinfo = socket.getaddrinfo
    lookups = 0

    def changing_dns(host: str, port: int, *args, **kwargs):
        nonlocal lookups
        if host == "data.example.com":
            lookups += 1
            resolved = "127.0.0.1" if lookups == 1 else "127.0.0.2"
            return original_getaddrinfo(resolved, port, *args, **kwargs)
        return original_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(fetch_module.socket, "getaddrinfo", changing_dns)
    try:
        result = fetch_csv(
            f"http://data.example.com:{server.server_port}/identity",
            _settings(
                fetch_allowed_hosts=("data.example.com",),
                fetch_allow_loopback=True,
                fetch_allowed_ports=(server.server_port,),
            ),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert lookups == 1
    assert result.content == f"127.0.0.1 data.example.com:{server.server_port}\n".encode()


@pytest.mark.parametrize("address", ["93.184.216.34", "2001:4860:4860::8888"])
def test_transport_passes_the_validated_ipv4_or_ipv6_literal(
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    backend = fetch_module._PinnedSyncBackend()
    connected: list[tuple[str, int]] = []
    stream = object()

    def connect_tcp(_backend, host, port, **_kwargs):
        connected.append((host, port))
        return stream

    monkeypatch.setattr(fetch_module.httpcore.SyncBackend, "connect_tcp", connect_tcp)
    backend.pin("data.example.com", 443, (address,))

    assert backend.connect_tcp("data.example.com", 443) is stream
    assert connected == [(address, 443)]


def test_pinned_https_keeps_hostname_for_sni_and_certificate_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The socket is pinned while HTTPX still validates the original hostname."""

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "data.example.com")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("data.example.com")]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    certificate_path = tmp_path / "server.pem"
    key_path = tmp_path / "server-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )

    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate_path, key_path)
    received_sni: list[str | None] = []
    server_context.set_servername_callback(
        lambda _socket, hostname, _context: received_sni.append(hostname)
    )
    server = HTTPServer(("127.0.0.1", 0), _Server)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    original_getaddrinfo = socket.getaddrinfo

    def resolve_to_localhost(host: str, port: int, *args, **kwargs):
        if host in {"data.example.com", "other.example.com"}:
            host = "127.0.0.1"
        return original_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(fetch_module.socket, "getaddrinfo", resolve_to_localhost)
    monkeypatch.setattr(
        "httpx.create_ssl_context",
        lambda: ssl.create_default_context(cafile=str(certificate_path)),
    )
    try:
        result = fetch_csv(
            f"https://data.example.com:{server.server_port}/identity",
            _settings(
                fetch_allowed_hosts=("data.example.com",),
                fetch_allow_loopback=True,
                fetch_allowed_ports=(server.server_port,),
            ),
        )
        with pytest.raises(FetchError, match="could not be reached"):
            fetch_csv(
                f"https://other.example.com:{server.server_port}/identity",
                _settings(
                    fetch_allowed_hosts=("other.example.com",),
                    fetch_allow_loopback=True,
                    fetch_allowed_ports=(server.server_port,),
                ),
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.content == f"127.0.0.1 data.example.com:{server.server_port}\n".encode()
    assert received_sni == ["data.example.com", "other.example.com"]


def test_an_oversized_response_is_refused(local_server: int) -> None:
    with pytest.raises(FetchError, match="exceeds"):
        fetch_csv(
            f"http://127.0.0.1:{local_server}/huge",
            _loopback_settings(local_server, max_fetch_bytes=4096),
        )


def test_an_html_response_is_refused(local_server: int) -> None:
    """A login page is not data, and importing it would produce nonsense."""

    with pytest.raises(FetchError, match="not CSV or text"):
        fetch_csv(f"http://127.0.0.1:{local_server}/html", _loopback_settings(local_server))


def test_a_redirect_to_a_private_address_is_refused(local_server: int) -> None:
    """Every hop is re-validated. This is the classic allowlist bypass."""

    with pytest.raises(FetchError):
        fetch_csv(f"http://127.0.0.1:{local_server}/redirect", _loopback_settings(local_server))


# --- storage path traversal ------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["../escape.txt", "/etc/passwd", "org/../../escape", "a/../../../../etc/hosts"],
)
def test_storage_refuses_keys_that_escape_the_root(key: str) -> None:
    with pytest.raises(storage.StorageError):
        storage.resolve(key)


def test_storage_accepts_a_normal_key() -> None:
    assert storage.resolve("org/abc/datasets/x.jsonl").is_absolute()


def test_an_upload_filename_cannot_traverse(db) -> None:
    """A crafted filename must not decide where bytes land."""

    from iluvtrade.data import ingest
    from tests.conftest import make_csv, make_principal

    principal = make_principal(db)
    outcome = ingest.ingest_upload(
        db,
        principal,
        filename="../../../../etc/passwd",
        payload=make_csv(),
        dataset_name="Traversal attempt",
        temporal=session_date_ist(),
    )
    assert outcome.source.origin == "passwd"
    assert storage.resolve(outcome.source.raw_path).is_relative_to(storage.root().resolve())
