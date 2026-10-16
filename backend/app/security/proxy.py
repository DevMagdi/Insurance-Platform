"""Trusted-proxy aware client IP resolution.

Behind Nginx / a cloud load balancer every request arrives from a proxy address,
so naive ``request.client.host`` collapses all users into one rate-limit bucket
(an attacker could lock everyone out, or one shared bucket hides abuse). The
``X-Forwarded-For`` chain is honoured ONLY when the TCP peer is a configured
trusted proxy, and it is walked right-to-left so a client cannot spoof its
address by sending its own header.
"""

from __future__ import annotations

from collections.abc import Iterable
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network

UNKNOWN_IP = "0.0.0.0"  # noqa: S104 - sentinel, never bound
DEFAULT_TRUSTED_PROXIES = "127.0.0.1/32,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
_MAX_HOPS = 10


def _parse(value: str | None) -> IPv4Address | IPv6Address | None:
    if not value:
        return None
    candidate = value.strip()
    if candidate.startswith("[") and "]" in candidate:  # [v6]:port
        candidate = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1 and "." in candidate:  # v4:port
        candidate = candidate.split(":", 1)[0]
    try:
        return ip_address(candidate)
    except ValueError:
        return None


class ClientIPResolver:
    def __init__(self, trusted: Iterable[str]) -> None:
        self.networks = tuple(ip_network(item.strip(), strict=False) for item in trusted if item)

    @classmethod
    def from_csv(cls, value: str) -> ClientIPResolver:
        return cls(part for part in value.split(",") if part.strip())

    def trusted(self, address: IPv4Address | IPv6Address) -> bool:
        return any(address in network for network in self.networks)

    def resolve(self, peer: str | None, forwarded_for: str | None) -> str:
        peer_ip = _parse(peer)
        if peer_ip is None:
            return UNKNOWN_IP
        if not forwarded_for or not self.trusted(peer_ip):
            return str(peer_ip)
        hops = [hop for hop in forwarded_for.split(",") if hop.strip()][-_MAX_HOPS:]
        for hop in reversed(hops):
            parsed = _parse(hop)
            if parsed is None:
                # A malformed hop means the chain is not trustworthy past this point.
                return str(peer_ip)
            if not self.trusted(parsed):
                return str(parsed)
        first = _parse(hops[0]) if hops else None
        return str(first or peer_ip)


def is_private_address(value: str) -> bool:
    parsed = _parse(value)
    return bool(parsed is not None and (parsed.is_private or parsed.is_loopback))
