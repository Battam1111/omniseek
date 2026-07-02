"""Shared SSRF / egress guard: validate an attacker-influenceable URL before the eye fetches it.

Extracted from curator/probe.py so BOTH the probe-time fetch AND the mainline egress (http.get,
penumbra_add_url / web_fallback, docreader URL download, asr ffmpeg / yt-dlp, view_images) share ONE
guard, instead of probe being the only hardened path. Stdlib-only (no eye imports) so the
low-level http client can import it with no dependency cycle.

Blocks: non-http(s) schemes, userinfo in the URL, non-80/443 ports, and any host that resolves to
a private / loopback / link-local / reserved / multicast IP (the SSRF-to-internal + cloud-metadata
class: 127.0.0.0/8, 10/8, 192.168/16, 169.254.169.254, ::1, metadata.google.internal, ...).

Allows 198.18.0.0/15 by default: the RFC-2544 benchmarking range that Clash-style fake-IP
split-tunnel resolvers use to FRONT public domains (connecting to the fake IP routes to the real
public site). It is almost never a real internal target, so allowing it keeps such deployments
working WITHOUT opening any dangerous range. Extra allowed CIDRs come from PENUMBRA_ALLOW_NETS
(comma/space separated) for an operator whose proxy uses a different pool.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from typing import Optional
from urllib.parse import urlsplit

_BLOCKED_HOST_SUFFIXES = (
    "localhost", ".localhost", ".local", ".internal", ".lan", "metadata.google.internal",
)
_ALLOWED_SCHEMES = ("http", "https")
_ALLOWED_PORTS = (80, 443)

# A security-class block (deliberately fetchable-but-refused). A "dns" miss is NOT here: it means
# the host did not resolve, which the real fetch will surface on its own — never a security signal.
SECURITY_BLOCK_REASONS = frozenset({"private_ip", "bad_scheme", "bad_port", "userinfo"})


def _allow_nets() -> tuple:
    nets = [ipaddress.ip_network("198.18.0.0/15")]
    for tok in os.environ.get("PENUMBRA_ALLOW_NETS", "").replace(",", " ").split():
        try:
            nets.append(ipaddress.ip_network(tok, strict=False))
        except ValueError:
            pass
    return tuple(nets)


_ALLOW_NETS = _allow_nets()


def _ip_is_blocked(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """True iff this resolved IP is a range we must never connect to. Unwraps IPv4-mapped IPv6
    (e.g. ::ffff:169.254.169.254) first — that mapped-address form is a rebind trick, refuse it."""
    if getattr(ip, "ipv4_mapped", None) is not None:
        return True
    if any(ip in net for net in _ALLOW_NETS):
        return False
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    )


def _host_suffix_blocked(host: str) -> bool:
    h = (host or "").lower().rstrip(".")
    if not h:
        return True
    for suf in _BLOCKED_HOST_SUFFIXES:
        if h == suf.lstrip(".") or h.endswith(suf):
            return True
    return False


# ── Public delegation surface ──────────────────────────────────────────────────────
# Thin public aliases so an attacker-URL fetcher that pins the IP itself (curator.probe) can
# reuse the SAME shape / IP / host-suffix decisions instead of re-implementing them. probe keeps
# its own socket.getaddrinfo + connect mechanics; only the DECISIONS live here (one guard).
def ip_is_blocked(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """Public: True iff this resolved IP is a range we must never connect to. See _ip_is_blocked."""
    return _ip_is_blocked(ip)


def host_suffix_blocked(host: str) -> bool:
    """Public: True iff this hostname is on the literal denylist. See _host_suffix_blocked."""
    return _host_suffix_blocked(host)


def validate_url_shape(url: str) -> "tuple[Optional[dict], Optional[str]]":
    """Public: validate scheme / userinfo / port on a single URL (no DNS). See _validate_url_shape.
    (parsed_parts, None) or (None, reason in {bad_scheme, userinfo, bad_port, dns})."""
    return _validate_url_shape(url)


def _resolve_safe_ip(host: str) -> "tuple[Optional[str], Optional[int], Optional[str]]":
    """Resolve ``host`` and validate EVERY returned IP. Returns (safe_ip_literal, family, None) or
    (None, None, blocked_reason). Resolve-once / pin-the-IP: a caller that CONNECTS to the returned
    literal closes the DNS-rebind/TOCTOU window."""
    if _host_suffix_blocked(host):
        return None, None, "private_ip"
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError, UnicodeError):
        return None, None, "dns"
    if not infos:
        return None, None, "dns"
    safe_ip = None
    safe_family = None
    for family, _type, _proto, _canon, sockaddr in infos:
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return None, None, "private_ip"
        if _ip_is_blocked(ip):
            return None, None, "private_ip"  # ANY blocked IP in the set aborts (no cherry-pick)
        if safe_ip is None:
            safe_ip = addr
            safe_family = family
    if safe_ip is None:
        return None, None, "dns"
    return safe_ip, safe_family, None


def _validate_url_shape(url: str) -> "tuple[Optional[dict], Optional[str]]":
    """Validate scheme / userinfo / port on a single URL. (parsed_parts, None) or (None, reason)."""
    try:
        sp = urlsplit(url)
    except ValueError:
        return None, "bad_scheme"
    if sp.scheme.lower() not in _ALLOWED_SCHEMES:
        return None, "bad_scheme"
    if sp.username or sp.password:
        return None, "userinfo"
    host = sp.hostname
    if not host:
        return None, "dns"
    port = sp.port
    if port is None:
        port = 443 if sp.scheme.lower() == "https" else 80
    if port not in _ALLOWED_PORTS:
        return None, "bad_port"
    return {"scheme": sp.scheme.lower(), "host": host, "port": port,
            "path": sp.path, "query": sp.query}, None


def url_is_allowed(url: str) -> "tuple[bool, Optional[str]]":
    """Pre-flight (no fetch): (True, None) if safe to fetch, else (False, reason in
    {bad_scheme, userinfo, bad_port, private_ip, dns})."""
    parts, reason = _validate_url_shape(url)
    if reason is not None:
        return False, reason
    _ip, _fam, reason = _resolve_safe_ip(parts["host"])
    if reason is not None:
        return False, reason
    return True, None


def security_block_reason(url: str) -> "Optional[str]":
    """The SECURITY block reason for ``url`` (private_ip/bad_scheme/bad_port/userinfo), or None.
    A 'dns' non-resolution returns None: not a security block, the real fetch will error itself.
    This is the predicate the mainline http client uses to refuse SSRF without breaking a source
    that is merely momentarily unresolvable."""
    ok, reason = url_is_allowed(url)
    if ok or reason == "dns":
        return None
    return reason
