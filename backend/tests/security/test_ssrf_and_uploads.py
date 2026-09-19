"""The data-fetch boundary must not become a way into the internal network."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from iluvtrade.common import storage
from iluvtrade.config import Settings
from iluvtrade.data.fetch import FetchError, fetch_csv, validate_url

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
    )
    assert outcome.source.origin == "passwd"
    assert storage.resolve(outcome.source.raw_path).is_relative_to(storage.root().resolve())
