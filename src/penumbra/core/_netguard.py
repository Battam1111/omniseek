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
import threading
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

# Cloud-provider metadata endpoints a bare is_private/is_link_local check MISSES. 169.254.169.254
# (AWS/GCP) + 169.254.170.2 (AWS ECS) ARE link-local (already blocked), but Alibaba 100.100.100.200
# sits in RFC-6598 CGNAT which Python 3.13 DROPPED from is_private, and Azure 168.63.129.16 is
# globally-routable Microsoft space, so both leak past _ip_is_blocked; fd00:ec2::254 (AWS v6 IMDS)
# is fc00::/7 (already blocked, listed for completeness + drift-lock). Denied UNCONDITIONALLY (before
# the ALLOW_NETS carve-out) so an operator escape hatch can never re-open an exact metadata target.
_METADATA_IPS = frozenset(
    ipaddress.ip_address(s) for s in (
        "169.254.169.254", "169.254.170.2", "100.100.100.200", "168.63.129.16", "fd00:ec2::254",
    )
)
# RFC-6598 CGNAT: Python 3.13 removed 100.64.0.0/10 from is_private. Chinese ISP home links (the
# eye's real environment) are frequently CGNAT'd, so a 100.64.x.x SSRF target is carrier-internal,
# not unreachable. Blocked AFTER the ALLOW_NETS carve-out so a proxy pool resident on 100.64/10 can
# still opt in via PENUMBRA_ALLOW_NETS.
_BLOCKED_NETS = (ipaddress.ip_network("100.64.0.0/10"),)

# socket.getaddrinfo is a BLOCKING C syscall that socket.setdefaulttimeout does NOT cover, so a slow
# / blackholed resolver would pin the calling thread (on the async path, a scarce anyio worker) for
# the full OS resolver duration (~10-30s). Bound it (PENUMBRA_DNS_TIMEOUT_S, default 5s).
try:
    _DNS_TIMEOUT_S = float(os.environ.get("PENUMBRA_DNS_TIMEOUT_S", "5") or 5)
except ValueError:
    _DNS_TIMEOUT_S = 5.0


def _getaddrinfo_bounded(host: str) -> list:
    """socket.getaddrinfo with an app-level timeout. Runs the blocking resolve in a daemon thread and
    joins with a bound; on timeout returns [] so the caller fails CLOSED to the existing 'dns' branch
    (no IP obtained -> not a security block, the real fetch surfaces its own error). The abandoned
    daemon thread cannot be interrupted mid-syscall, but it is OFF the scarce anyio pool and
    single-driver volume caps accumulation; the common OS-cached path returns immediately."""
    result: list = []
    error: list = []

    def _run() -> None:
        try:
            result.append(socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP))
        except Exception as exc:  # noqa: BLE001 -- gaierror / OSError / UnicodeError, re-raised below
            error.append(exc)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(_DNS_TIMEOUT_S)
    if t.is_alive():
        return []  # timed out: treat as no-resolution -> 'dns'
    if error:
        raise error[0]
    return result[0] if result else []


def _has_embedded_v4(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """True iff this is an IPv6 address that embeds an IPv4 via a transit form: IPv4-mapped
    (``::ffff:a.b.c.d``), 6to4 (``2002:V4::``), Teredo (``2001:0::``), NAT64 (``64:ff9b::/96``), or
    the deprecated IPv4-compatible (``::a.b.c.d``). We refuse the WHOLE class: each can smuggle a
    private / metadata v4 (``::ffff:169.254.169.254``, ``64:ff9b::a9fe:a9fe``, ``2002:a9fe:a9fe::``)
    past a v6-only is_private check (Python reports is_private=False for them), and all are
    deprecated / rare transit mechanisms a real source is essentially never served over. This is the
    DNS-rebind-via-v6 closure (red-team wall-probe H2)."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return False
    if ip.ipv4_mapped is not None or ip.sixtofour is not None or ip.teredo is not None:
        return True
    packed = ip.packed
    if packed[:2] == b"\x00\x64" and packed[2:4] == b"\xff\x9b":  # NAT64 64:ff9b::/96 (well-known)
        return True
    # IPv4-compatible ``::a.b.c.d`` (deprecated), excluding ``::`` (unspecified) and ``::1``
    # (loopback), which the is_unspecified / is_loopback checks below already own.
    if packed[:12] == b"\x00" * 12 and packed[12:16] not in (b"\x00\x00\x00\x00", b"\x00\x00\x00\x01"):
        return True
    return False


def _ip_is_blocked(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """True iff this resolved IP is a range we must never connect to. Refuses every IPv6 form that
    embeds an IPv4 (mapped / 6to4 / Teredo / NAT64 / v4-compatible) FIRST (see _has_embedded_v4:
    those smuggle a private v4 past a v6-only check), then the ordinary private / loopback /
    link-local / reserved / multicast / unspecified block, with the 198.18/15 fake-IP proxy pool
    allowed."""
    if _has_embedded_v4(ip):
        return True
    if ip in _METADATA_IPS:
        return True  # exact cloud-metadata endpoint: unconditional, ALLOW_NETS cannot re-open it
    if any(ip in net for net in _ALLOW_NETS):
        return False
    if any(ip in net for net in _BLOCKED_NETS):
        return True  # CGNAT (after the allow carve-out, so a 100.64/10 proxy pool can opt in)
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
        infos = _getaddrinfo_bounded(host)
    except (socket.gaierror, OSError, UnicodeError):
        return None, None, "dns"
    if not infos:
        return None, None, "dns"  # unresolved OR resolver timed out (fail closed to non-security dns)
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
    # ``.port`` re-parses the authority and RAISES ValueError on a non-numeric / out-of-range port
    # (urlsplit only defers it, does not validate). A malformed port is a bad_port, never a crash:
    # this is the ONE choke point, so every caller (http.get / get_impersonated / safe_fetch) is
    # immune without its own guard.
    try:
        port = sp.port
    except ValueError:
        return None, "bad_port"
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


def resolve_pin(url: str) -> "tuple[Optional[str], Optional[str], Optional[str]]":
    """The PIN lane's twin of ``security_block_reason``: validate + RESOLVE + hand back the pinned IP.
    Returns:
      (safe_ip_literal, host, None)  -- safe AND resolvable: the caller CONNECTS to safe_ip (not the
                                        hostname), closing the DNS-rebind TOCTOU a re-resolve at connect
                                        would reopen (public at check, private at connect).
      (None, host, reason)           -- SSRF-blocked, reason in SECURITY_BLOCK_REASONS: the caller RAISES.
      (None, host, "dns")            -- unresolvable: NOT a security block, so the caller does NOT pin and
                                        lets the real fetch surface its own DNS error (matches
                                        security_block_reason's dns->None).
    Same guard DECISIONS as security_block_reason (delegates to _validate_url_shape + _resolve_safe_ip),
    so the block/pin lanes never fork."""
    parts, reason = _validate_url_shape(url)
    if reason is not None:
        return None, (parts or {}).get("host"), reason  # bad_scheme | userinfo | bad_port | dns (no host)
    host = parts["host"]
    ip, _family, reason = _resolve_safe_ip(host)
    if reason is not None:
        return None, host, reason  # (None, host, "private_ip") | (None, host, "dns")
    # A PROXY FAKE-IP (198.18/15 or PENUMBRA_ALLOW_NETS) must NOT be pinned: the connection goes to the
    # proxy (a Clash/mihomo split-tunnel resolver), not a real target host, so DNS-rebind is moot (the
    # local resolver, not an attacker NS, mints the fake IP) AND pinning it would key the shared pooled
    # client by a fake IP the proxy REUSES across hostnames -> a pooled keep-alive collides onto the
    # wrong host (SNI mismatch -> SSL EOF). Return ip=None so the caller passes through BY HOSTNAME
    # (the proxy routes correctly + the pool stays per-host). Real public IPs still pin (rebind matters).
    try:
        if any(ipaddress.ip_address(ip) in net for net in _ALLOW_NETS):
            return None, host, None  # safe fake-IP: DO NOT pin, pass through by hostname
    except ValueError:
        pass
    return ip, host, None  # real public IP: PIN (rebind TOCTOU closed)
