"""Prefer IPv4 for all outbound connections.

Frozen (PyInstaller) builds intermittently fail HTTPS with
``OSError: [Errno 49] Can't assign requested address`` when DNS / happy-eyeballs
selects an IPv6 address for a Google endpoint (oauth2.googleapis.com,
www.googleapis.com): the bundled runtime can't bind an IPv6 source socket on
some networks. Symptom: uploads/heartbeat fail on an IPv6-preferring network
while the system Python connects fine.

Google's endpoints are all dual-stack, so resolving to IPv4 first avoids the
error with no loss of connectivity. Falls back to the original resolver when a
host has no A record. Import this ONCE, before any network client is built.
"""

import socket

_original_getaddrinfo = socket.getaddrinfo


def _ipv4_first_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        res = _original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
        if res:
            return res
    except OSError:
        pass
    # no IPv4 available (or lookup error) — fall back to the normal resolver
    return _original_getaddrinfo(host, port, family, type, proto, flags)


def install() -> None:
    """Idempotently install the IPv4-first resolver."""
    if getattr(socket.getaddrinfo, "_ipv4_first", False):
        return
    _ipv4_first_getaddrinfo._ipv4_first = True
    socket.getaddrinfo = _ipv4_first_getaddrinfo


# apply on import so it's active before any Google client is constructed
install()
