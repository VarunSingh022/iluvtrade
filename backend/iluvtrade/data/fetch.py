"""Retrieving a CSV from a URL, safely.

This is the module PHASE 4 warns about: *do NOT implement arbitrary unsafe URL
fetching blindly*. The controls, and what each actually stops:

============================  ===============================================
Scheme allowlist              ``file://``, ``gopher://``, ``ftp://`` and the
                              rest cannot be reached at all.
Host allowlist                An empty allowlist permits nothing. A deployment
                              names the hosts it trusts; "any host" is not a
                              value this accepts.
DNS-resolution address check  Every resolved address is checked against the
                              private, loopback, link-local, multicast and
                              reserved ranges *before* connecting, which is
                              what stops a public hostname pointing at
                              ``169.254.169.254`` or ``10.0.0.1``. The checked
                              addresses are pinned into the transport so the
                              connection cannot resolve the hostname again.
Redirect policy               Redirects are followed manually, at most
                              ``fetch_max_redirects`` times, and **every hop is
                              re-validated**. A permitted host redirecting to
                              the metadata service is the classic bypass.
Size ceiling                  The body is read in chunks and abandoned the
                              moment it exceeds the limit, so a declared
                              ``Content-Length`` cannot lie its way past.
Decompression ceiling         Gzip and deflate are expanded against the same
                              ceiling, so a zip bomb is refused rather than
                              inflated into memory.
Timeout                       Applied to connect and to read.
Content-type check            A text or CSV type, or no type at all. An
                              ``text/html`` response is a login page, not data.
============================  ===============================================

What this module deliberately does not do is authenticate. Credentialed sources
need per-source secrets and a place to keep them; that is recorded in
``docs/SECURITY.md`` as deferred rather than half-built here.
"""

from __future__ import annotations

import gzip
import ipaddress
import socket
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn
from urllib.parse import urlparse

import httpcore
import httpx

from iluvtrade.config import Settings
from iluvtrade.db.base import utcnow

__all__ = ["FetchError", "FetchResult", "fetch_csv", "validate_url"]

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_ALLOWED_CONTENT_TYPES = (
    "text/csv",
    "text/plain",
    "application/csv",
    "application/octet-stream",
    "application/x-gzip",
    "application/gzip",
)
_CHUNK = 64 * 1024


class FetchError(RuntimeError):
    """The URL was refused, or the retrieval failed. The message is shown to the user."""


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Bytes, plus the provenance that must be recorded with them."""

    content: bytes
    final_url: str
    requested_url: str
    content_type: str | None
    retrieved_at: datetime
    redirect_chain: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ValidatedDestination:
    hostname: str
    port: int
    addresses: tuple[str, ...]


class _PinnedSyncBackend(httpcore.SyncBackend):
    """Connect only to the addresses validated for the current request hop."""

    def __init__(self) -> None:
        self._addresses: dict[tuple[str, int], tuple[str, ...]] = {}

    def pin(self, hostname: str, port: int, addresses: tuple[str, ...]) -> None:
        self._addresses[(_host_key(hostname), port)] = addresses

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        addresses = self._addresses.get((_host_key(host), port))
        if not addresses:
            raise httpcore.ConnectError(f"No validated addresses for {host!r}:{port}.")

        last_error: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for address in addresses:
            try:
                return super().connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc

        if last_error is None:
            raise httpcore.ConnectError(f"No validated addresses for {host!r}:{port}.")
        raise last_error


class _PinnedResponseStream(httpx.SyncByteStream):
    def __init__(self, response: httpcore.Response) -> None:
        self._response = response

    def __iter__(self) -> Iterator[bytes]:
        try:
            yield from self._response.iter_stream()
        except Exception as exc:
            _raise_httpx_exception(exc)

    def close(self) -> None:
        try:
            self._response.close()
        except Exception as exc:
            _raise_httpx_exception(exc)


class _PinnedHTTPTransport(httpx.BaseTransport):
    """HTTPX transport extension backed by a pool using validated IPs."""

    def __init__(self, backend: _PinnedSyncBackend) -> None:
        limits = httpx.Limits()
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            network_backend=backend,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        try:
            response = self._pool.handle_request(core_request)
        except Exception as exc:
            _raise_httpx_exception(exc)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_PinnedResponseStream(response),
            extensions=response.extensions,
            request=request,
        )

    def close(self) -> None:
        try:
            self._pool.close()
        except Exception as exc:
            _raise_httpx_exception(exc)


_HTTPCORE_HTTPX_EXCEPTIONS: dict[type[Exception], type[httpx.HTTPError]] = {
    httpcore.ConnectTimeout: httpx.ConnectTimeout,
    httpcore.ReadTimeout: httpx.ReadTimeout,
    httpcore.WriteTimeout: httpx.WriteTimeout,
    httpcore.PoolTimeout: httpx.PoolTimeout,
    httpcore.TimeoutException: httpx.TimeoutException,
    httpcore.ConnectError: httpx.ConnectError,
    httpcore.ReadError: httpx.ReadError,
    httpcore.WriteError: httpx.WriteError,
    httpcore.NetworkError: httpx.NetworkError,
    httpcore.ProxyError: httpx.ProxyError,
    httpcore.UnsupportedProtocol: httpx.UnsupportedProtocol,
    httpcore.LocalProtocolError: httpx.LocalProtocolError,
    httpcore.RemoteProtocolError: httpx.RemoteProtocolError,
    httpcore.ProtocolError: httpx.ProtocolError,
}


def _raise_httpx_exception(error: Exception) -> NoReturn:
    mapped_type: type[httpx.HTTPError] | None = None
    for core_type, httpx_type in _HTTPCORE_HTTPX_EXCEPTIONS.items():
        if isinstance(error, core_type) and (
            mapped_type is None or issubclass(httpx_type, mapped_type)
        ):
            mapped_type = httpx_type
    if mapped_type is None:
        raise error
    raise mapped_type(str(error)) from error


def _host_key(hostname: str) -> str:
    """Canonical key shared by URL parsing and the HTTP transport."""

    hostname = hostname.strip("[]")
    try:
        return ipaddress.ip_address(hostname).compressed
    except ValueError:
        return hostname.encode("idna").decode("ascii").lower()


#: Ranges that must never be reached, stated explicitly.
#:
#: The stdlib's ``is_private`` is **not** sufficient on its own: what it covers
#: changes between Python versions. On 3.12, ``100.64.0.0/10`` (RFC 6598
#: carrier-grade NAT — routable-looking, and real internal infrastructure at
#: many ISPs and clouds) and ``192.88.99.0/24`` (RFC 7526 6to4 relay anycast)
#: are both reported as *public*. Relying on the flags alone means the set of
#: addresses this application will connect to silently changes with an
#: interpreter upgrade.
#:
#: So the flags are used **and** this list, and the union is refused.
_DENIED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    # IPv4
    ipaddress.ip_network("0.0.0.0/8"),  # "this network"
    ipaddress.ip_network("10.0.0.0/8"),  # RFC1918
    ipaddress.ip_network("100.64.0.0/10"),  # RFC6598 shared address space
    ipaddress.ip_network("127.0.0.0/8"),  # loopback
    ipaddress.ip_network("169.254.0.0/16"),  # link-local, incl. cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),  # RFC1918
    ipaddress.ip_network("192.0.0.0/24"),  # IETF protocol assignments
    ipaddress.ip_network("192.0.2.0/24"),  # TEST-NET-1
    ipaddress.ip_network("192.88.99.0/24"),  # RFC7526 6to4 relay anycast
    ipaddress.ip_network("192.168.0.0/16"),  # RFC1918
    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking
    ipaddress.ip_network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),  # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),  # multicast
    ipaddress.ip_network("240.0.0.0/4"),  # reserved
    # IPv6
    ipaddress.ip_network("::/128"),  # unspecified
    ipaddress.ip_network("::1/128"),  # loopback
    ipaddress.ip_network("fc00::/7"),  # unique local
    ipaddress.ip_network("fe80::/10"),  # link-local
    ipaddress.ip_network("ff00::/8"),  # multicast
    ipaddress.ip_network("2001:db8::/32"),  # documentation
    ipaddress.ip_network("64:ff9b::/96"),  # IPv4/IPv6 translation
)


def _is_public_address(address: str) -> bool:
    """Whether an address is one this application may connect to.

    An **IPv4-mapped IPv6 address** (``::ffff:10.0.0.1``) is unwrapped before
    the check. Without that, every private IPv4 range could be reached by
    writing it in IPv6 notation — the check would look at the v6 form, find it
    in none of the v6 ranges, and allow it.
    """

    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False

    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.ipv4_mapped is not None:
            parsed = parsed.ipv4_mapped
        elif parsed.sixtofour is not None:
            parsed = parsed.sixtofour

    if any(parsed in network for network in _DENIED_NETWORKS):
        return False

    return not (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


def _validated_destination(url: str, settings: Settings) -> _ValidatedDestination:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise FetchError(
            f"Scheme {parsed.scheme!r} is not fetchable. Only http and https are allowed."
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise FetchError("The URL has no host.")
    if parsed.username is not None or parsed.password is not None or "@" in (parsed.netloc or ""):
        # ``https://data.example.com@evil.example.com/`` points at *evil*, and a
        # human reviewing an allowlist entry reads the first host. Refusing
        # userinfo outright removes the ambiguity rather than resolving it.
        raise FetchError(
            "A data-source URL may not contain userinfo (a '@' before the host). "
            "Such a URL does not point where it appears to."
        )
    if parsed.port is not None and parsed.port not in settings.fetch_allowed_ports:
        raise FetchError(
            f"Port {parsed.port} is not permitted for data fetching. Allowed: "
            f"{', '.join(str(p) for p in settings.fetch_allowed_ports)}."
        )

    allowed = tuple(h.lower() for h in settings.fetch_allowed_hosts)
    if not allowed:
        raise FetchError(
            "No data-source hosts are allowed in this deployment. An administrator "
            "must add the host to ILUVTRADE_FETCH_ALLOWED_HOSTS before it can be fetched."
        )
    if not any(host == entry or host.endswith("." + entry) for entry in allowed):
        raise FetchError(f"Host {host!r} is not in this deployment's data-source allowlist.")

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        infos = socket.getaddrinfo(host, port)
    except socket.gaierror as exc:
        raise FetchError(f"Host {host!r} could not be resolved.") from exc

    # ``getaddrinfo`` types its sockaddr loosely; only the string forms are
    # addresses, and anything else is not something to connect to anyway.
    addresses = tuple(
        dict.fromkeys(str(info[4][0]) for info in infos if isinstance(info[4][0], str))
    )
    if not addresses:
        raise FetchError(f"Host {host!r} resolved to no addresses.")
    for address in addresses:
        if _is_public_address(address):
            continue
        if settings.fetch_allow_loopback and ipaddress.ip_address(address).is_loopback:
            continue
        raise FetchError(
            f"Host {host!r} resolves to {address}, which is not a public address. "
            "Fetching it could reach an internal service."
        )
    return _ValidatedDestination(host, port, addresses)


def validate_url(url: str, settings: Settings) -> str:
    """Refuse a URL this deployment must not fetch. Returns the hostname."""

    return _validated_destination(url, settings).hostname


def _decompress(body: bytes, encoding: str | None, limit: int) -> bytes:
    """Expand a compressed body against the same ceiling as the raw one."""

    if not encoding:
        return body
    encoding = encoding.lower().strip()
    if encoding == "gzip":
        decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
    elif encoding == "deflate":
        decompressor = zlib.decompressobj()
    else:
        return body
    # ``max_length`` is the whole point: a 4 KB payload that expands to 4 GB
    # stops at the ceiling instead of exhausting memory.
    expanded = decompressor.decompress(body, limit + 1)
    if len(expanded) > limit:
        raise FetchError(
            f"The compressed response expands past the {limit} byte limit and was refused."
        )
    return expanded


def fetch_csv(url: str, settings: Settings) -> FetchResult:
    """Retrieve ``url`` under every control this module documents."""

    requested = url
    chain: list[str] = []
    current = url
    backend = _PinnedSyncBackend()

    with httpx.Client(
        transport=_PinnedHTTPTransport(backend),
        follow_redirects=False,
        timeout=httpx.Timeout(settings.fetch_timeout_seconds),
        headers={"User-Agent": "iluvtrade-data-fetcher/0.1", "Accept-Encoding": "gzip, deflate"},
        # A proxy would resolve the URL hostname outside the pinned backend.
        trust_env=False,
    ) as client:
        for _ in range(settings.fetch_max_redirects + 1):
            destination = _validated_destination(current, settings)
            backend.pin(destination.hostname, destination.port, destination.addresses)
            try:
                with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError("The server returned a redirect with no location.")
                        current = str(response.url.join(location))
                        chain.append(current)
                        continue

                    if response.status_code >= 400:
                        raise FetchError(f"The source returned HTTP {response.status_code}.")

                    content_type = response.headers.get("content-type")
                    if content_type:
                        base = content_type.split(";")[0].strip().lower()
                        if base not in _ALLOWED_CONTENT_TYPES:
                            raise FetchError(
                                f"The source returned {base!r}, which is not CSV or text. "
                                "This is usually a login or error page rather than data."
                            )

                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes(_CHUNK):
                        total += len(chunk)
                        if total > settings.max_fetch_bytes:
                            raise FetchError(
                                f"The response exceeds the {settings.max_fetch_bytes} byte limit."
                            )
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    encoding = response.headers.get("content-encoding")
                    # httpx transparently decodes standard encodings; only expand
                    # when it evidently has not.
                    if encoding and body[:2] == b"\x1f\x8b":
                        body = _decompress(body, encoding, settings.max_fetch_bytes)
                    elif body[:2] == b"\x1f\x8b":
                        body = gzip.decompress(body)
                        if len(body) > settings.max_fetch_bytes:
                            raise FetchError("The decompressed response exceeds the size limit.")

                    return FetchResult(
                        content=body,
                        final_url=str(response.url),
                        requested_url=requested,
                        content_type=content_type,
                        retrieved_at=utcnow(),
                        redirect_chain=tuple(chain),
                    )
            except httpx.TimeoutException as exc:
                raise FetchError("The source timed out.") from exc
            except httpx.HTTPError as exc:
                raise FetchError(f"The source could not be reached: {type(exc).__name__}") from exc

    raise FetchError(f"The source redirected more than {settings.fetch_max_redirects} times.")
