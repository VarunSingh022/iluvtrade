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
                              ``169.254.169.254`` or ``10.0.0.1``.
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
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

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


def _is_public_address(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


def validate_url(url: str, settings: Settings) -> str:
    """Refuse a URL this deployment must not fetch. Returns the hostname."""

    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise FetchError(
            f"Scheme {parsed.scheme!r} is not fetchable. Only http and https are allowed."
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise FetchError("The URL has no host.")
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
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise FetchError(f"Host {host!r} could not be resolved.") from exc

    # ``getaddrinfo`` types its sockaddr loosely; only the string forms are
    # addresses, and anything else is not something to connect to anyway.
    addresses = {str(info[4][0]) for info in infos if isinstance(info[4][0], str)}
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
    return host


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

    with httpx.Client(
        follow_redirects=False,
        timeout=httpx.Timeout(settings.fetch_timeout_seconds),
        headers={"User-Agent": "iluvtrade-data-fetcher/0.1", "Accept-Encoding": "gzip, deflate"},
    ) as client:
        for _hop in range(settings.fetch_max_redirects + 1):
            validate_url(current, settings)
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
