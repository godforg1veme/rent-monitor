"""Validate encrypted HTTPS or private Tailscale IPv4 browser access."""

from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit


def is_private_browser_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return False
        if parsed.scheme == "https":
            return parsed.port in (None, 443, 10000)
        return (
            parsed.scheme == "http"
            and parsed.port == 10001
            and ip_address(parsed.hostname) in ip_network("100.64.0.0/10")
        )
    except ValueError:
        return False
