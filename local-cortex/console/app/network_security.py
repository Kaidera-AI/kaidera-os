"""Small network-safety helpers shared by console features."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


def ssrf_block_reason(url: str) -> str | None:
    """Return a reason when a URL resolves to a non-public address."""
    try:
        host = (urlparse(url).hostname or "").rstrip(".").lower()
    except ValueError:
        return "an unparseable URL"
    if not host:
        return "a URL with no host"
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return f"a host that does not resolve ({host})"
    for info in infos:
        raw = info[4][0].split("%", 1)[0]
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            return f"a non-public address ({host} -> {raw})"
    return None


__all__ = ["ssrf_block_reason"]
