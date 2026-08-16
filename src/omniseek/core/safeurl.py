"""SSRF-hardened fetch for an ATTACKER-INFLUENCEABLE URL: the core-leaf pinned-per-hop fetcher.

``safe_fetch`` is OmniSeek's ONE hardened fetch primitive for a URL an adversary chose: a
candidate-source URL the curator probes, OR an arbitrary user/page URL no adapter claims that
web_fallback must read. It OWNS its httpx client (cookieless, redirect-disabled, trust_env=False)
instead of wrapping http._request_capped (which forces the shared pool + follow_redirects=True + a
COMPRESSED-bytes cap: all wrong for an attacker host). It scheme-allowlists http/https, rejects
userinfo + non-80/443 ports on the FINAL pinned connection, resolves the host and validates EVERY
resolved IP, then CONNECTS TO THE PINNED IP literal (defeating DNS-rebind), walks redirects manually
re-validating each hop, caps on DECODED bytes (defeating a gzip bomb) AND a raw cap, and honors
cache.cache_only().

This is a CORE LEAF module: it imports ONLY core (_netguard / cache / http / httpx / socket /
ipaddress), never curator, so both the curator probe path (curator.probe re-exports safe_fetch +
its helpers) and the mainline web_fallback path depend on it with no import cycle. Every SSRF
DECISION (URL shape, per-IP block, host-suffix denylist) delegates to _netguard; safeurl carries
only the RESOLUTION + connect mechanics (its own socket.getaddrinfo so it can pin the IP literal it
connects to) plus the thin wrappers that forward to _netguard, so the guard never forks.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Optional
from urllib.parse import urljoin

import anyio
import httpx

from omniseek.core import _netguard, cache, http

# ── SSRF guard: DELEGATED to omniseek.core._netguard (ONE guard, shared with the mainline egress) ──
# The URL-shape (scheme/userinfo/port), IP-block, and host-suffix DECISIONS all live in _netguard;
# this fetcher used to carry its own byte-identical copy. safeurl keeps only the RESOLUTION + connect
# mechanics below (its own socket.getaddrinfo so it can pin the IP literal it connects to), and the
# thin wrappers here forward to _netguard so the two paths can never drift. The 198.18.0.0/15
# fake-IP-proxy allowance + every private/loopback/link-local/reserved block now come from there.

# Caps. max_bytes is a DECODED cap (gzip-bomb defense); a separate raw cap refuses a body
# whose COMPRESSED size already exceeds the budget before we ever decode it.
_DEFAULT_MAX_BYTES = 5 * 1024 * 1024


def _ip_is_blocked(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """True iff this resolved IP is in a range we must never connect to. Delegates to _netguard
    (unwraps IPv4-mapped IPv6, allows the fake-IP proxy pool, blocks private/loopback/...)."""
    return _netguard.ip_is_blocked(ip)


def _host_suffix_blocked(host: str) -> bool:
    """True iff ``host`` is on the literal denylist. Delegates to _netguard."""
    return _netguard.host_suffix_blocked(host)


def _resolve_safe_ip(host: str) -> "tuple[Optional[str], Optional[int], Optional[str]]":
    """Resolve ``host`` and validate EVERY returned IP. Returns (safe_ip_literal, family, None)
    on success, or (None, None, blocked_reason) on any failure / a blocked IP. Resolve-once,
    pin-the-IP: the caller CONNECTS to the returned literal IP (not the hostname), which closes
    the DNS-rebind/TOCTOU window (a 2nd lookup at connect time can't swap in a private IP).

    THIN FORWARD to _netguard._resolve_safe_ip: this used to carry a byte-identical COPY of that
    resolve+validate loop, the ONE helper here that re-implemented instead of forwarding, so a future
    _netguard hardening (a new blocked range, a rebind-form fix) would NOT be inherited. Forwarding
    makes the resolution + per-IP decision live ONCE in the shared guard. It stays a module-level
    name (not an import alias) so safe_fetch calls it by name and curator goldens that monkeypatch
    safeurl/probe._resolve_safe_ip still rebind a live name safe_fetch uses; and it calls
    _netguard._resolve_safe_ip FRESH each time so a patch of THAT name is seen here too. The shared
    socket module (probe/safeurl/_netguard all `import socket`) means a getaddrinfo monkeypatch still
    lands: _netguard.socket IS safeurl.socket, so the pinned-IP contract is byte-for-byte preserved."""
    return _netguard._resolve_safe_ip(host)


def _validate_url_shape(url: str) -> "tuple[Optional[dict], Optional[str]]":
    """Validate scheme / userinfo / port on a single URL. Returns (parsed_parts, None) or
    (None, blocked_reason). Checked on EVERY hop (input + each redirect target). Delegates to
    _netguard so the shape rules match the mainline egress guard exactly."""
    return _netguard.validate_url_shape(url)


def _blocked(reason: str, status: Optional[int] = None, chain: Optional[list] = None) -> dict:
    return {"ok": False, "status": status, "bytes": 0, "text": "", "final_url": "",
            "redirect_chain": chain or [], "content_type": None, "blocked_reason": reason}


def _read_capped(resp: httpx.Response, max_bytes: int) -> "tuple[Optional[bytes], Optional[str]]":
    """Read the body, aborting on EITHER cap: DECODED bytes (iter_bytes handles gzip/deflate/
    br/zstd) > max_bytes (gzip-bomb defense), OR raw compressed bytes > max_bytes (refuse a body
    whose compressed size alone already blows the budget). Returns (decoded_bytes, None) or
    (None, 'oversize')."""
    decoded = bytearray()
    raw_total = 0
    # raw_total tracks compressed wire bytes; if the COMPRESSED stream already exceeds the cap we
    # never finish decoding. httpx exposes the raw stream length via num_bytes_downloaded.
    try:
        for chunk in resp.iter_bytes():
            decoded += chunk
            if len(decoded) > max_bytes:
                return None, "oversize"
            raw_total = resp.num_bytes_downloaded
            if raw_total > max_bytes:
                return None, "oversize"
    except Exception:  # noqa: BLE001: a stream error mid-read is a failed fetch, not a body
        return None, "oversize"
    return bytes(decoded), None


def safe_fetch(url: str, *, method: str = "GET", render: bool = False,
               timeout_total: float = 20.0, max_bytes: int = _DEFAULT_MAX_BYTES,
               max_redirects: int = 5) -> dict:
    """Fetch an ATTACKER-INFLUENCEABLE candidate URL and return FACTS, fail-closed.

    Owns its client (NOT http._request_capped/_get_client). Returns:
        {"ok", "status", "bytes", "text", "final_url", "redirect_chain", "content_type",
         "blocked_reason": one of private_ip|bad_scheme|oversize|timeout|dns|userinfo|
                           bad_port|redirect_loop|cache_only|fetch_error|None}
    A blocked fetch is RECORDED; it never silently judges the candidate. ``render`` is accepted
    for signature parity but P1 NEVER drives a browser here (anonymous stranger only, no CDP).

    SAFE_FETCH BOUNDARY (spec 8c, SSRF pass): safe_fetch hardens the PROBE-TIME fetch ONLY -- the
    one fetch OmniSeek makes at a candidate URL before any verdict. The POST-ADMISSION recurring
    fetch a family adapter makes once a source is live (org_watch / page_watch / news_scraper /
    render) goes through the NORMAL fetcher and is NOT IP-pinned by this guard. That is exactly why
    those families are in apply._NEVER_AUTO_FAMILIES (never auto-applied) and why an admit of one
    must consciously acknowledge the unguarded recurring fetch (server.omniseek_curator_decide requires
    baseline_ref.recurring_fetch_acknowledged for a first-seen host in that subclass). Routing the
    enrich/OpenAlex resolution hosts through a fixed-API host allowlist is a flagged follow-up
    hardening (the id regexes constrain path injection today), not a P4 blocker.
    """
    # cache.cache_only() (cache_only=True): do ZERO live egress.
    if cache.cache_only():
        return _blocked("cache_only")

    parts, reason = _validate_url_shape(url)
    if reason is not None:
        return _blocked(reason)

    chain: list = []
    current_url = url
    seen: set = set()

    client = httpx.Client(
        follow_redirects=False,           # we walk redirects MANUALLY, re-validating each hop
        cookies={},                       # no cookies, ever
        headers={"User-Agent": http.USER_AGENT},
        timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
        trust_env=False,                  # no env proxies / no Authorization from netrc / etc.
        max_redirects=0,
    )
    try:
        for _hop in range(max_redirects + 1):
            parts, reason = _validate_url_shape(current_url)
            if reason is not None:
                return _blocked(reason, chain=chain)
            host = parts["host"]
            port = parts["port"]
            scheme = parts["scheme"]

            safe_ip, _family, reason = _resolve_safe_ip(host)
            if reason is not None:
                return _blocked(reason, chain=chain)

            # Pin the connection to the validated IP literal; carry the Host header + (for TLS)
            # the SNI hostname so the server still routes correctly. This defeats DNS-rebind:
            # the guard validated THIS ip, and THIS ip is exactly what we connect to.
            if ":" in safe_ip:  # IPv6 literal needs brackets in a URL authority
                authority = f"[{safe_ip}]:{port}"
            else:
                authority = f"{safe_ip}:{port}"
            pinned_url = f"{scheme}://{authority}{parts['path'] or '/'}"
            if parts["query"]:
                pinned_url += "?" + parts["query"]

            req_headers = {"Host": host}
            extensions = {}
            if scheme == "https":
                # sni_hostname makes the TLS handshake present the real host while we connect to
                # the pinned IP: required for SNI-based virtual hosts + cert validation.
                extensions = {"sni_hostname": host}

            try:
                with client.stream(method, pinned_url, headers=req_headers,
                                   extensions=extensions, timeout=timeout_total) as resp:
                    status = resp.status_code
                    # Redirect? re-validate the Location target as a brand-new untrusted URL.
                    if status in (301, 302, 303, 307, 308) and "location" in resp.headers:
                        loc = resp.headers["location"]
                        nxt = urljoin(current_url, loc)  # resolve relative against the CURRENT url
                        if nxt in seen:
                            return _blocked("redirect_loop", status=status, chain=chain)
                        seen.add(nxt)
                        chain.append({"from": current_url, "to": nxt, "status": status})
                        current_url = nxt
                        continue  # next hop re-runs the FULL guard at the top of the loop
                    # Terminal response: read the body under both caps.
                    body, oversize = _read_capped(resp, max_bytes)
                    if oversize is not None:
                        return _blocked(oversize, status=status, chain=chain)
                    ctype = resp.headers.get("content-type")
                    text = ""
                    if method.upper() != "HEAD" and body:
                        try:
                            enc = resp.encoding or "utf-8"
                            text = body.decode(enc, errors="replace")
                        except (LookupError, ValueError):
                            text = body.decode("utf-8", errors="replace")
                    return {
                        "ok": True,
                        "status": status,
                        "bytes": len(body or b""),
                        "text": text[:max_bytes],         # length-bounded attacker string
                        "final_url": current_url[:2048],
                        "redirect_chain": chain,
                        "content_type": (ctype[:200] if ctype else None),
                        "blocked_reason": None,
                    }
            except httpx.TimeoutException:
                return _blocked("timeout", chain=chain)
            except httpx.HTTPError:
                return _blocked("fetch_error", chain=chain)
        # Exhausted max_redirects without a terminal response.
        return _blocked("redirect_loop", chain=chain)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def walk_redirects_revalidated(client: "httpx.Client", method: str, url: str, *,
                               max_redirects: int = 10,
                               headers: Optional[dict] = None) -> "httpx.Response":
    """Walk redirects MANUALLY on a follow_redirects=False client, re-validating EVERY hop's target
    via _netguard.security_block_reason BEFORE connecting. Returns the FINAL non-3xx httpx.Response
    with its body still UNREAD (the caller reads/streams then closes it), or RAISES
    RuntimeError("refused SSRF-class url (<reason>): <url>") on a blocked hop (or on redirect
    exhaustion). Reuses _netguard for EVERY decision; this only sequences the hops (no forked guard).

    The shared per-hop guard for the two in-band-bytes callers that use their OWN httpx (NOT the
    IP-pinning safe_fetch): docreader._download (streams a document to a temp file) and
    docreader.view_image_urls (buffers image bytes). Both previously followed redirects BLINDLY
    (follow_redirects=True), so an attacker-influenceable URL that 302s to 169.254.169.254 / 127.0.0.1
    was an SSRF/exfil oracle. This closes that with a _netguard check on each resolved Location.

    It deliberately does NOT IP-pin like safe_fetch: those callers never pinned, and per-hop
    security_block_reason matches their existing initial-URL guard and the mainline http egress; the
    arbitrary-user-URL lane that warrants full pinning already goes through safe_fetch. Streamed
    (H1) vs buffered (H2) final-body handling stays in each caller; only the hop walk is shared."""
    current = url
    for _hop in range(max_redirects + 1):
        blk = _netguard.security_block_reason(current)
        if blk is not None:
            raise RuntimeError(f"refused SSRF-class url ({blk}): {current[:120]}")
        req = client.build_request(method, current, headers=headers)
        # follow_redirects=False EXPLICITLY: the per-hop _netguard check above is the ONLY redirect
        # authority, so the primitive stays safe even if a future caller hands it a client built with
        # follow_redirects=True (never let httpx auto-follow past the guard).
        resp = client.send(req, stream=True, follow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
            loc = resp.headers["location"]
            resp.close()                       # drop the 3xx before connecting to the next hop
            current = urljoin(current, loc)    # resolve relative Location against the CURRENT url
            continue
        return resp                            # final non-3xx: caller owns read + close
    raise RuntimeError(f"refused SSRF-class url (redirect_loop): {url[:120]}")


class SSRFGuardTransport(httpx.BaseTransport):
    """A sync httpx transport wrapper that _netguard-validates EVERY request URL before delegating.

    httpx re-invokes the client's transport ONCE PER REQUEST, including once per redirect hop (the
    redirect loop calls transport.handle_request for each hop). So wrapping the real transport here
    enforces SSRF on EVERY hop while httpx keeps owning redirect method/body/cookie semantics (no
    manual redirect walk, no reimplementing 303->GET / 307-body rules). This is the mainline per-hop
    redirect-SSRF closure for the shared pooled client (http._get_client): it only refuses a blocked
    hop's connection, so a 302 -> 169.254.169.254 / a private IP is never followed and its body is
    never delivered, for this and every other http.get / get_json / post_json caller at once.

    It raises httpx.ConnectError (a RequestError subclass httpx will NOT retry, and that
    http._request_capped's except already turns into None: the adapter failure contract).

    IP-PINNED (the rebind TOCTOU is now closed on the mainline too, not only in safe_fetch): it
    resolves + validates ONCE via _netguard.resolve_pin, then rewrites the request URL to the checked
    IP LITERAL so the wrapped transport connects to exactly that IP (a second getaddrinfo at connect
    can't swap in a private IP). The real host is preserved for correctness: the Host header (httpx
    already set it from the original URL) routes vhosts, and sni_hostname keeps TLS SNI + cert
    validation on the real name; the original URL is RESTORED before returning so a relative-redirect
    Location still resolves against the real host, and each redirect hop re-pins. Every SSRF decision
    delegates to _netguard so the guard never forks; an unresolvable host is NOT pinned (dns is not a
    security block) and passes through so the wrapped transport surfaces its own DNS error."""

    def __init__(self, wrapped: "httpx.BaseTransport") -> None:
        self._wrapped = wrapped

    def handle_request(self, request: "httpx.Request") -> "httpx.Response":
        ip, host, reason = _netguard.resolve_pin(str(request.url))
        if reason is not None and reason != "dns":
            raise httpx.ConnectError(
                f"refused SSRF-class url ({reason}): {str(request.url)[:120]}", request=request)
        if ip is None:  # unresolvable / no host: cannot pin; let the wrapped transport surface the error
            return self._wrapped.handle_request(request)
        original_url = request.url
        request.url = original_url.copy_with(host=ip)     # connect to the validated IP (defeats rebind)
        request.extensions.setdefault("sni_hostname", host)  # TLS SNI + cert stay on the real host
        try:
            return self._wrapped.handle_request(request)
        finally:
            request.url = original_url  # restore: a relative-redirect Location resolves against the real host

    def close(self) -> None:
        self._wrapped.close()


class AsyncSSRFGuardTransport(httpx.AsyncBaseTransport):
    """Async twin of ``SSRFGuardTransport``: the per-hop SSRF guard for the shared pooled
    ``httpx.AsyncClient`` (http._aget_client). Same guard DECISION (delegates to
    _netguard.security_block_reason, single-homed, never forks), same per-hop enforcement
    (httpx re-invokes the transport once per redirect hop), same httpx.ConnectError (a
    non-retried RequestError http._arequest_capped's except turns into None). See the sync
    twin above for the full rationale (not re-transcribed here)."""

    def __init__(self, wrapped: "httpx.AsyncBaseTransport") -> None:
        self._wrapped = wrapped

    async def handle_async_request(self, request: "httpx.Request") -> "httpx.Response":
        # OFF-LOOP (S4b): resolve_pin does getaddrinfo per hop (a BLOCKING syscall). On the async client
        # this would run ON the loop, so a slow/cold DNS would freeze EVERY coroutine. Push it to a worker
        # thread; the pin rewrite itself is pure CPU on the loop. IDENTICAL guard DECISION + pin as the sync
        # twin (SSRFGuardTransport): see it for the full rationale (not re-transcribed).
        ip, host, reason = await anyio.to_thread.run_sync(_netguard.resolve_pin, str(request.url))
        if reason is not None and reason != "dns":
            raise httpx.ConnectError(
                f"refused SSRF-class url ({reason}): {str(request.url)[:120]}", request=request)
        if ip is None:
            return await self._wrapped.handle_async_request(request)
        original_url = request.url
        request.url = original_url.copy_with(host=ip)
        request.extensions.setdefault("sni_hostname", host)
        try:
            return await self._wrapped.handle_async_request(request)
        finally:
            request.url = original_url

    async def aclose(self) -> None:
        await self._wrapped.aclose()


# DEFER (S4-side, not built here): an async safe_fetch / walk_redirects_revalidated (the
# attacker-URL IP-pinning lane serving fetch_url / web_fallback / docreader) is a DIFFERENT lane
# from the adapter mainline and is NOT needed by the S4 async fan-out; it stays sync-on-thread for now.
