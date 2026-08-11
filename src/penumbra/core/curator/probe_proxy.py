"""SSRF-pin forward proxy for the curator wall-aware probe (P2).

A localhost forward HTTP proxy the JAILED Probe Browser routes through
(``chromium --proxy-server=http://HOST:PORT``). Every CONNECT / plain-HTTP request is
validated + IP-PINNED via ``penumbra.core._netguard`` (the ONE shared SSRF guard, so the
decision never forks) BEFORE a single byte leaves: resolve the target host, reject if ANY
resolved IP is private / loopback / link-local / reserved / an IPv6-embedded-v4 form (the
SSRF-to-internal + cloud-metadata class), then CONNECT TO THE PINNED IP literal (defeating
DNS-rebind), under a per-probe host allowlist + a request / byte rate cap. Fail-closed: any
doubt refuses.

Topology (why the proxy runs on the trusted host, not inside the jail):
  [jail VM: Probe Browser runs hostile candidate JS]
        | its ONLY egress route (jail iptables) -> this proxy's port
        v
  [host: this proxy]  --resolve+check+PIN-->  (optional upstream mihomo -> dedicated node)  --> internet

The BROWSER (which executes attacker-controlled JS/WASM) is contained in the colima VM whose
egress firewall permits only this proxy. The PROXY is trusted eye code that reuses _netguard,
so it is safe to run host-side; that keeps the eye package out of the jail and the SSRF
decision single-homed. A blocked request is LOGGED (the curator sees what the candidate tried).

Run: ``python -m penumbra.core.curator.probe_proxy --port 8899 [--allow-hosts a.com,b.com]
      [--upstream 127.0.0.1:7899] [--max-requests 400] [--max-mib 64]``
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import socket
from typing import Optional

try:
    from penumbra.core import _netguard  # normal: imported as part of the eye package (host side)
except ImportError:  # minimal jail container: _netguard.py is mounted flat beside this file
    import _netguard  # type: ignore  # noqa: F401

logger = logging.getLogger("penumbra.core.curator.probe_proxy")

_CONNECT_OK = b"HTTP/1.1 200 Connection Established\r\n\r\n"
_ALLOWED_METHODS_HTTP = frozenset({"GET", "HEAD"})  # plain-HTTP: read-only only (POST etc. refused)


def _refuse(reason: str, code: int = 403) -> bytes:
    return (f"HTTP/1.1 {code} Forbidden\r\n"
            f"X-Probe-Refused: {reason}\r\n"
            f"Content-Length: 0\r\nConnection: close\r\n\r\n").encode("latin-1")


class _Caps:
    """Per-proxy-lifetime request + byte budget. A wall-probe renders ONE candidate; a runaway
    page (redirect storm, flood, decompression relay) is bounded here even though the CONNECT
    tunnel is otherwise a blind relay."""

    def __init__(self, max_requests: int, max_bytes: int) -> None:
        self.max_requests = max_requests
        self.max_bytes = max_bytes
        self.requests = 0
        self.bytes = 0

    def take_request(self) -> bool:
        self.requests += 1
        return self.requests <= self.max_requests

    def take_bytes(self, n: int) -> bool:
        self.bytes += n
        return self.bytes <= self.max_bytes


def _set_nodelay(writer: asyncio.StreamWriter) -> None:
    """Disable Nagle on a relay endpoint: a proxy tunnel carries interactive, bursty traffic, so
    TCP_NODELAY cuts round-trip latency on the bidirectional relay (small TLS records must not wait
    to coalesce)."""
    sock = writer.get_extra_info("socket")
    if sock is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass


def _host_allowed(host: str, allow: "frozenset[str]") -> bool:
    """A host passes the per-probe allowlist iff it equals, or is a subdomain of, an allowed
    registrable domain. An EMPTY allowlist means 'no host restriction' (the SSRF-pin + rate cap +
    dedicated egress IP are then the bounds): rendering a real page pulls subresources from many
    CDNs that cannot be enumerated a priori, so a strict allowlist is opt-in per probe, not the
    default."""
    if not allow:
        return True
    h = (host or "").lower().rstrip(".")
    return any(h == d or h.endswith("." + d) for d in allow)


def _pin(host: str, port: int) -> "tuple[Optional[str], Optional[str]]":
    """Validate + resolve + pin a target host. Returns (pinned_ip_literal, None) or
    (None, reason). Delegates the whole SSRF decision to _netguard so it never forks."""
    if port not in (80, 443):
        return None, "bad_port"
    ip, _family, reason = _netguard._resolve_safe_ip(host)
    if reason is not None:
        return None, reason  # private_ip | dns
    return ip, None


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, caps: "_Caps") -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            if not caps.take_bytes(len(data)):
                logger.warning("probe-proxy: byte cap hit (%d) -> tearing down", caps.bytes)
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def _open_target(ip: str, port: int, upstream: "Optional[tuple[str, int]]") -> "tuple[asyncio.StreamReader, asyncio.StreamWriter]":
    """Open a stream to the PINNED target ip:port. With ``upstream`` set (a dedicated mihomo
    host:port), CONNECT-chain through it so egress exits via the dedicated node's IP (distinct from
    the credentialed cluster). The upstream is handed the PINNED IP literal, so the pin holds
    through the chain (no re-resolution downstream); when the dedicated mihomo is a single-node
    'global' config it routes any target via that one node regardless of the literal."""
    if upstream is not None:
        uhost, uport = upstream
        r, w = await asyncio.open_connection(uhost, uport)
        authority = f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"
        w.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode("latin-1"))
        await w.drain()
        status = await r.readline()
        if b" 200 " not in status and not status.startswith(b"HTTP/1.1 200"):
            w.close()
            raise ConnectionError(f"upstream CONNECT refused: {status!r}")
        while True:  # drain the upstream response headers up to the blank line
            h = await r.readline()
            if h in (b"\r\n", b"\n", b""):
                break
        return r, w
    return await asyncio.open_connection(ip, port)


async def _handle_connect(host: str, port: int, client_r: asyncio.StreamReader,
                          client_w: asyncio.StreamWriter, cfg: "ProxyConfig") -> None:
    """HTTPS CONNECT tunnel. The proxy can see + enforce the HOST (allowlist) + PIN the IP, then
    relays the opaque TLS bytes (it cannot see the method/path inside TLS; the host allowlist +
    rate cap + dedicated egress IP bound the residual)."""
    if not _host_allowed(host, cfg.allow_hosts):
        client_w.write(_refuse("host_not_allowed")); await client_w.drain(); client_w.close(); return
    ip, reason = _pin(host, port)
    if reason is not None:
        logger.warning("probe-proxy: REFUSE CONNECT %s:%d (%s)", host, port, reason)
        client_w.write(_refuse(reason, 502 if reason == "dns" else 403)); await client_w.drain(); client_w.close(); return
    try:
        target_r, target_w = await _open_target(ip, port, cfg.upstream)
    except (ConnectionError, OSError) as exc:
        logger.warning("probe-proxy: CONNECT %s:%d -> %s open failed: %s", host, port, ip, exc)
        client_w.write(_refuse("connect_failed", 502)); await client_w.drain(); client_w.close(); return
    logger.info("probe-proxy: CONNECT %s:%d -> pinned %s%s", host, port, ip,
                f" via {cfg.upstream[0]}:{cfg.upstream[1]}" if cfg.upstream else "")
    _set_nodelay(client_w)
    _set_nodelay(target_w)
    client_w.write(_CONNECT_OK)
    await client_w.drain()
    await asyncio.gather(_pipe(client_r, target_w, cfg.caps), _pipe(target_r, client_w, cfg.caps))


async def _handle_plain(method: str, url: str, headers: "list[bytes]", client_r: asyncio.StreamReader,
                        client_w: asyncio.StreamWriter, cfg: "ProxyConfig") -> None:
    """Plain HTTP (absolute-form request). Enforces GET/HEAD (read-only), validates + pins, and
    forwards origin-form to the pinned IP with the real Host header."""
    if method.upper() not in _ALLOWED_METHODS_HTTP:
        client_w.write(_refuse("method_not_allowed", 405)); await client_w.drain(); client_w.close(); return
    parts, reason = _netguard.validate_url_shape(url)
    if reason is not None:
        client_w.write(_refuse(reason)); await client_w.drain(); client_w.close(); return
    host, port, path, query = parts["host"], parts["port"], parts["path"], parts["query"]
    if not _host_allowed(host, cfg.allow_hosts):
        client_w.write(_refuse("host_not_allowed")); await client_w.drain(); client_w.close(); return
    ip, reason = _pin(host, port)
    if reason is not None:
        logger.warning("probe-proxy: REFUSE %s %s (%s)", method, url, reason)
        client_w.write(_refuse(reason, 502 if reason == "dns" else 403)); await client_w.drain(); client_w.close(); return
    try:
        target_r, target_w = await _open_target(ip, port, cfg.upstream)
    except (ConnectionError, OSError):
        client_w.write(_refuse("connect_failed", 502)); await client_w.drain(); client_w.close(); return
    origin = path or "/"
    if query:
        origin += "?" + query
    # rebuild the request in origin-form; keep the client headers but force a clean Host + close.
    out = [f"{method} {origin} HTTP/1.1".encode("latin-1"), f"Host: {host}".encode("latin-1")]
    for h in headers:
        low = h.lower()
        if low.startswith(b"host:") or low.startswith(b"proxy-") or low.startswith(b"connection:"):
            continue
        out.append(h)
    out.append(b"Connection: close")
    target_w.write(b"\r\n".join(out) + b"\r\n\r\n")
    await target_w.drain()
    logger.info("probe-proxy: %s %s -> pinned %s", method, url, ip)
    _set_nodelay(client_w)
    _set_nodelay(target_w)
    await asyncio.gather(_pipe(client_r, target_w, cfg.caps), _pipe(target_r, client_w, cfg.caps))


async def _handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter, cfg: "ProxyConfig") -> None:
    try:
        request_line = await client_r.readline()
        if not request_line:
            client_w.close(); return
        if not cfg.caps.take_request():
            client_w.write(_refuse("request_cap", 429)); await client_w.drain(); client_w.close(); return
        try:
            method, target, _version = request_line.decode("latin-1", "replace").split()[:3]
        except ValueError:
            client_w.write(_refuse("bad_request", 400)); await client_w.drain(); client_w.close(); return
        headers: "list[bytes]" = []
        while True:  # read request headers up to the blank line
            line = await client_r.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            headers.append(line.rstrip(b"\r\n"))
        if method.upper() == "CONNECT":
            host, _, port_s = target.rpartition(":")
            host = host.strip("[]")
            try:
                port = int(port_s)
            except ValueError:
                client_w.write(_refuse("bad_request", 400)); await client_w.drain(); client_w.close(); return
            await _handle_connect(host, port, client_r, client_w, cfg)
        elif target.lower().startswith(("http://", "https://")):
            await _handle_plain(method, target, headers, client_r, client_w, cfg)
        else:
            client_w.write(_refuse("non_absolute", 400)); await client_w.drain(); client_w.close()
    except (ConnectionError, asyncio.IncompleteReadError, OSError) as exc:
        logger.debug("probe-proxy: client error: %s", exc)
        try:
            client_w.close()
        except OSError:
            pass


class ProxyConfig:
    def __init__(self, allow_hosts: "frozenset[str]", upstream: "Optional[tuple[str, int]]",
                 caps: "_Caps") -> None:
        self.allow_hosts = allow_hosts
        self.upstream = upstream
        self.caps = caps


async def serve(host: str, port: int, cfg: "ProxyConfig") -> None:
    server = await asyncio.start_server(lambda r, w: _handle(r, w, cfg), host, port)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    logger.info("probe-proxy listening on %s (upstream=%s, allow=%s, caps=%d req / %d MiB)",
                addrs, cfg.upstream, sorted(cfg.allow_hosts) or "*",
                cfg.caps.max_requests, cfg.caps.max_bytes // (1024 * 1024))
    async with server:
        await server.serve_forever()


def _parse_upstream(s: "Optional[str]") -> "Optional[tuple[str, int]]":
    if not s:
        return None
    h, _, p = s.rpartition(":")
    return (h.strip("[]"), int(p))


def main(argv: "Optional[list[str]]" = None) -> None:
    ap = argparse.ArgumentParser(description="SSRF-pin forward proxy for the wall-aware probe")
    ap.add_argument("--host", default="127.0.0.1", help="listen host (use the host IP the jail reaches)")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--allow-hosts", default="", help="comma-separated registrable domains (empty = any public host)")
    ap.add_argument("--upstream", default="", help="dedicated mihomo host:port to CONNECT-chain through (empty = direct)")
    ap.add_argument("--max-requests", type=int, default=400)
    ap.add_argument("--max-mib", type=int, default=64)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    allow = frozenset(d.strip().lower() for d in args.allow_hosts.split(",") if d.strip())
    cfg = ProxyConfig(allow, _parse_upstream(args.upstream),
                      _Caps(args.max_requests, args.max_mib * 1024 * 1024))
    try:
        asyncio.run(serve(args.host, args.port, cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
