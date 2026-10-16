"""Server-side request forgery guard for tenant-configured outbound URLs (OWASP A10).

Webhook and LLM endpoints are chosen by administrators, so a malicious or
compromised admin account could aim them at cloud metadata (169.254.169.254),
the database, Redis or other cluster-internal services. Validation at creation
time is not enough (DNS can change - "DNS rebinding"), therefore every delivery:

1. resolves the hostname *at send time*;
2. rejects the request if ANY resolved address is not globally routable;
3. pins the connection to the vetted IP while keeping the original ``Host``
   header and TLS SNI/certificate verification for the real hostname.

Redirects stay disabled on every outbound client.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address
from urllib.parse import urlsplit, urlunsplit


class UnsafeDestination(ValueError):
    """The URL is malformed or resolves to a private/reserved address."""


@dataclass(frozen=True)
class PinnedTarget:
    url: str  # with the vetted IP literal as host
    host_header: str
    sni_hostname: str
    address: str

    @property
    def extensions(self) -> dict[str, str]:
        return {"sni_hostname": self.sni_hostname}


async def resolve_host(host: str, port: int) -> list[str]:
    """DNS lookup (patched in unit tests, which never touch the network)."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def is_public(address: IPv4Address | IPv6Address) -> bool:
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return bool(address.is_global and not address.is_multicast)


async def pin_public_url(url: str, *, require_https: bool = True) -> PinnedTarget:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeDestination("invalid_url") from exc
    host = parsed.hostname
    if parsed.scheme not in (("https",) if require_https else ("https", "http")):
        raise UnsafeDestination("https_required")
    if not host or parsed.username or parsed.password:
        raise UnsafeDestination("invalid_host")
    effective_port = port or (443 if parsed.scheme == "https" else 80)
    try:
        literal: IPv4Address | IPv6Address | None = ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        candidates = [str(literal)]
    else:
        try:
            candidates = await resolve_host(host, effective_port)
        except OSError as exc:
            raise UnsafeDestination("dns_resolution_failed") from exc
    addresses = []
    for raw in candidates:
        try:
            addresses.append(ip_address(raw.split("%", 1)[0]))
        except ValueError as exc:
            raise UnsafeDestination("invalid_address") from exc
    if not addresses or not all(is_public(address) for address in addresses):
        raise UnsafeDestination("non_public_address")
    chosen = addresses[0]
    ip_host = f"[{chosen}]" if chosen.version == 6 else str(chosen)
    netloc = f"{ip_host}:{port}" if port else ip_host
    host_header = f"{host}:{port}" if port else host
    pinned = urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))
    return PinnedTarget(pinned, host_header, host, str(chosen))
