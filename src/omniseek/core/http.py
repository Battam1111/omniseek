"""Shared HTTP helpers — one User-Agent, one timeout policy, one error contract.

~20 adapters each re-implemented the same wrapper: ``httpx.get`` + a UA header +
``follow_redirects=True`` + ``raise_for_status()`` + ``try/except → None`` (with
4+ divergent UA strings). These helpers centralize that. They return ``None`` on
*any* failure (logged), so adapters keep their "failure → empty result" contract
without per-file boilerplate.

Open-API adapters SHOULD use these: routing through the shared client is what earns the
diag.note evidence tap (a failure branch here records the status + body for /eye-fix), the
pooling, the 30MB cap, and the SSRF guard. Some open-API adapters still fetch with a bare
``httpx`` call and only log on failure, so they are INVISIBLE to a drill's diagnostic; those
that cannot route through here (a genuinely oversized download, a non-JSON transport) must at
least add a ``diag.note(...)`` in their own failure branch (the levels_fyi / rss precedent).
Anti-bot / walled adapters (mokahr / feishu / xiaohongshu / bytedance) keep their own bespoke
headers + signing and diag.note by hand — do NOT route those through here.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

import anyio
import httpx

from omniseek.core import _netguard, cache, diag

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; omniseek/0.1)"
DEFAULT_TIMEOUT = 20
MAX_BYTES = 30 * 1024 * 1024  # 30MB hard cap on a single response body — a feed/JSON
                              # bigger than this is almost certainly a hijack/misconfig;
                              # stream + abort rather than buffer it all and OOM the daemon.

# Process-wide pooled client: every open-API helper call reuses ONE httpx.Client, so
# repeated requests to the same host skip the TCP+TLS handshake (a real cost when the
# 64-worker search fan-out + per-source internal fan-out hammer S2/OpenAlex/Arctic/…).
# httpx.Client is documented thread-safe for concurrent requests, which matches that
# fan-out. HTTP/2 multiplexing is enabled only if ``h2`` is importable (no new hard dep:
# keep-alive reuse — the bulk of the win — works on HTTP/1.1 too). Walled / anti-bot
# adapters do NOT use these helpers (they keep bespoke headers/signing — see module
# docstring), so the shared client only ever serves open-API sources.
_client: Optional[httpx.Client] = None
_client_lock = threading.Lock()


def _http2_ok() -> bool:
    try:
        import h2  # noqa: F401  — optional; absence just means HTTP/1.1 keep-alive
        return True
    except Exception:  # noqa: BLE001
        return False


def _get_client() -> httpx.Client:
    """Lazily build (once) the shared pooled client. Double-checked lock so the 64-worker
    fan-out's first concurrent callers create exactly one."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                # Lazy import to break the http <-> safeurl cycle: safeurl imports http (for
                # USER_AGENT etc.), so http importing safeurl at module load would cycle. By the
                # time the first request builds the client, safeurl is fully loaded, so a lazy
                # import here is safe and keeps SSRFGuardTransport single-homed in safeurl.
                from omniseek.core import safeurl  # noqa: PLC0415 (lazy: breaks the import cycle)
                # Build the real HTTP transport explicitly (same http2 + limits as before), then wrap
                # it so EVERY request AND every redirect hop is _netguard-validated at the connection
                # layer (S1-C3). headers / follow_redirects / timeout stay Client-level so httpx still
                # owns redirect semantics; the wrapper only refuses an SSRF-class hop's connection.
                _wrapped = httpx.HTTPTransport(
                    http2=_http2_ok(),
                    limits=httpx.Limits(max_keepalive_connections=64,
                                        max_connections=128,
                                        keepalive_expiry=30.0),
                )
                # PARITY NOTE: passing an explicit transport= makes httpx skip env/system-proxy
                # auto-detection (allow_env_proxies = trust_env AND transport is None). This is
                # HARMLESS for the current deployment, which egresses via transparent fake-IP TUN
                # (verified 2026-07: no *_proxy env on OmniSeek-http process, empty scutil --proxies,
                # utun interfaces present), so httpx never used a proxy anyway. If this eye is ever
                # moved to a PROXY-based egress (HTTP_PROXY / system proxy), restore proxy support by
                # passing proxy= to the wrapped HTTPTransport (or mounts= of SSRFGuardTransport-wrapped
                # proxied transports) so the SSRF guard still wraps the proxied connection.
                _client = httpx.Client(
                    transport=safeurl.SSRFGuardTransport(_wrapped),
                    headers={"User-Agent": USER_AGENT},
                    follow_redirects=True,
                    timeout=DEFAULT_TIMEOUT,
                )
    return _client


def _request_capped(method: str, url: str, *, timeout: int, headers: dict,
                    **kwargs: Any) -> Optional[httpx.Response]:
    """Stream a request, aborting if the body exceeds ``MAX_BYTES`` — the OOM guard shared
    by all helpers. Returns a fully-read ``httpx.Response`` (so ``.text``/``.json()`` work
    exactly as before) or ``None`` on any failure / oversize. Uses the pooled client so the
    connection is reused (keep-alive) and returned to the pool when the stream context exits."""
    if cache.cache_only():
        return None  # cache-only mode (cache_only=True): do NO live HTTP, the single egress guard
    # SSRF pre-flight (belt-and-suspenders): refuse a URL whose host resolves to a private/loopback/
    # link-local/reserved IP (169.254.169.254 cloud-metadata, 127/10/192.168, ...). Closes the direct
    # omniseek_add_url -> web_fallback -> http.get attacker path; a 'dns' miss is NOT blocked (the fetch
    # fails on its own). This initial check is now REDUNDANT with the per-hop SSRFGuardTransport on the
    # pooled client (S1-C3): that transport revalidates EVERY hop, so redirect-to-private is CLOSED
    # there (the residual this comment used to admit). We keep this fast-fail for the clear initial
    # block message + diag.note evidence tap; the extra getaddrinfo is OS-cached, so negligible.
    # Residual: a DNS-rebind TOCTOU (host resolves public here, private at connect time) is NOT closed
    # by non-pinning revalidation; the IP-pinning lane stays safe_fetch (pins + revalidates each hop).
    _blk = _netguard.security_block_reason(url)
    if _blk is not None:
        logger.warning("http.%s blocked SSRF-class target (%s): %s", method.lower(), url, _blk)
        diag.note(f"http.{method.lower()}", url=url, status=None, body=f"blocked SSRF-class target: {_blk}")
        return None
    try:
        with _get_client().stream(method, url, timeout=timeout, headers=headers, **kwargs) as r:
            r.raise_for_status()
            raw = bytearray()
            for chunk in r.iter_raw():
                raw += chunk
                if len(raw) > MAX_BYTES:
                    logger.warning("http.%s refused oversized response (%s): >%d bytes",
                                   method.lower(), url, MAX_BYTES)
                    diag.note(f"http.{method.lower()}", url=url, status=r.status_code,
                              body=f"refused oversized response (>{MAX_BYTES} bytes)")
                    return None
        # Rebuild a normal already-read Response from the raw body + original headers, so
        # content-encoding / charset decoding happens exactly as the buffered path did.
        return httpx.Response(r.status_code, headers=r.headers, content=bytes(raw),
                              request=r.request)
    except Exception as exc:  # noqa: BLE001 — failure → None is the adapter contract
        logger.warning("http.%s failed (%s): %s", method.lower(), url, exc)
        # A non-2xx surfaces here as httpx.HTTPStatusError → surface its status + body snippet so
        # the fixing agent sees the wall (403/412 anti-bot, 404 moved endpoint), not just a string.
        st = getattr(getattr(exc, "response", None), "status_code", None)
        bd = None
        if isinstance(exc, httpx.HTTPStatusError):
            try:
                bd = exc.response.text
            except Exception:  # noqa: BLE001
                bd = None
        diag.note(f"http.{method.lower()}", url=url, status=st, body=bd, exc=exc)
        return None


def get(url: str, *, timeout: int = DEFAULT_TIMEOUT, headers: Optional[dict] = None,
        params: Optional[dict] = None, **kwargs: Any) -> Optional[httpx.Response]:
    """GET with the shared UA + redirects + raise_for_status, size-capped. None on failure."""
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    return _request_capped("GET", url, timeout=timeout, headers=hdrs, params=params, **kwargs)


def get_json(url: str, **kwargs: Any) -> Optional[Any]:
    """GET and parse JSON. None on request failure OR unparseable body."""
    resp = get(url, **kwargs)
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("http.get_json parse failed (%s): %s", url, exc)
        diag.note("http.get_json", url=url, status=resp.status_code, body=resp.text, exc=exc)
        return None


def get_text(url: str, **kwargs: Any) -> Optional[str]:
    """GET and return response text. None on failure."""
    resp = get(url, **kwargs)
    return resp.text if resp is not None else None


def download_to_file(url: str, dest: str, *, max_bytes: int,
                     timeout: int = 600, headers: Optional[dict] = None) -> int:
    """Stream a URL to ``dest`` via curl_cffi (libcurl + Chrome TLS fingerprint), capping at
    ``max_bytes``. Returns bytes written; RAISES on cache-only / SSRF block / missing dep / oversize /
    transport error (unlike get()'s None: a fallback caller wants the reason). curl_cffi NOT httpx:
    this deployment's egress mangles openssl TLS to some CDNs (httpx -> 'UNEXPECTED_EOF_WHILE_READING'
    on cloudfront) while libcurl's handshake gets through -- the same tier get_impersonated uses. The
    robust-fetch path for LARGE binaries (audio) that the size-capped get() refuses AND that the
    bundled ffmpeg's own TLS cannot fetch. (Egress to some CDNs can be slow, so timeout defaults high.)"""
    if cache.cache_only():
        raise RuntimeError("cache-only mode: no live download")
    _blk = _netguard.security_block_reason(url)
    if _blk is not None:
        raise RuntimeError(f"blocked SSRF-class target: {_blk}")
    try:
        from curl_cffi import requests as _creq  # lazy: keep curl_cffi off the hot import path
    except Exception as exc:  # noqa: BLE001 — missing/broken dep surfaces as a clear raise
        raise RuntimeError(f"curl_cffi unavailable: {exc}") from exc
    total = 0
    r = _creq.get(url, impersonate="chrome", timeout=timeout, stream=True,
                  headers=dict(headers) if headers else None, allow_redirects=True)
    try:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=262144):
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError(f"download exceeded {max_bytes} bytes")
                f.write(chunk)
    finally:
        r.close()
    if total == 0:
        raise RuntimeError("download returned 0 bytes")
    return total


def get_impersonated(url: str, *, timeout: int = DEFAULT_TIMEOUT,
                     headers: Optional[dict] = None) -> Optional[bytes]:
    """GET via curl_cffi with a real-browser TLS/JA3 fingerprint (Chrome impersonation); returns the
    raw response BYTES. None on failure OR if curl_cffi is unavailable.

    A SEPARATE fetch tier BETWEEN plain httpx (``get``) and the heavy CDP browser: some hosts wall
    httpx by its TLS/JA3 handshake fingerprint (PerimeterX / HUMAN 'Pardon Our Interruption',
    Cloudflare TLS checks) while letting a real browser through. curl_cffi replays Chrome's TLS
    handshake, so the fetch passes WITHOUT spinning a headless browser. OPT-IN only (no default
    caller routes here, so every existing source is byte-identical); the import is LAZY and a missing
    dep degrades to None (the source goes DOWN, never crashes the server). Verified 2026-06-22 on the
    HigherEdJobs PerimeterX-walled RSS feed: httpx -> challenge HTML; curl_cffi(chrome) -> the real
    129-item feed.

    NOTE: we do NOT inject our OmniSeek UA here — ``impersonate='chrome'`` sets a Chrome-consistent
    UA + header order, and overriding the UA would desync the very fingerprint we are matching."""
    try:
        from curl_cffi import requests as _creq  # lazy: keep curl_cffi off the hot import path
    except Exception as exc:  # noqa: BLE001 — missing/broken dep -> degrade, never crash
        logger.warning("http.get_impersonated unavailable (curl_cffi import failed): %s", exc)
        return None
    # Mirror the _request_capped discipline at the curl_cffi tier (S1-C1): the single egress guard, the
    # SSRF pre-flight, and a MAX_BYTES cap. All three were absent here (an unbounded ``return r.content``).
    if cache.cache_only():
        return None  # cache-only mode (cache_only=True): do NO live HTTP, the single egress guard
    # SSRF pre-flight on the INITIAL url (same predicate as _request_capped:90). Per-hop redirect
    # revalidation stays DEFERRED PAST C2 to a curl_cffi-exercisable cohort: get_impersonated is
    # curl_cffi (a DIFFERENT transport from safeurl.safe_fetch's httpx, so it cannot reuse that
    # per-hop walk) and serves only FIXED CONFIGURED public hosts (higheredjobs), whose redirect-SSRF
    # needs a DNS-rebind on a fixed host (lower probability than an arbitrary user URL); and curl_cffi
    # is not importable in this dev/smoke env, so a manual redirect-walk cannot be verified here. C2
    # closed the arbitrary-user-URL lane instead (omniseek_add_url -> web_fallback -> safeurl.safe_fetch,
    # IP-pinned per hop); this fixed-host tier keeps allow_redirects=True as a known residual.
    _blk = _netguard.security_block_reason(url)
    if _blk is not None:
        logger.warning("http.get_impersonated blocked SSRF-class target (%s): %s", url, _blk)
        return None
    try:
        r = _creq.get(url, impersonate="chrome", timeout=timeout,
                      headers=dict(headers) if headers else None,
                      allow_redirects=True)  # per-hop redirect revalidation DEFERRED past C2 (curl_cffi tier; see above)
        r.raise_for_status()
        # DECODED-bytes cap (curl_cffi returns already-decoded content). The curl_cffi streaming API
        # (stream=True + iter_content) is not exercisable in this build (curl_cffi is not importable in the
        # smoke/dev env, so a streamed accumulate-and-abort mirror of the _request_capped iter_raw loop
        # cannot be verified here), so this takes the documented FALLBACK: reject a declared-oversize
        # Content-Length BEFORE reading, then cap len(r.content) AFTER read. It still allocates the body
        # once (the partial-mitigation caveat), but it never RETURNS an oversize body, closing the
        # unbounded-return hole. A later cohort can swap this for an abort-mid-stream once the tier is
        # exercisable against real curl_cffi.
        try:
            _clen = int(r.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            _clen = 0
        if _clen > MAX_BYTES:
            logger.warning("http.get_impersonated refused oversized response (%s): Content-Length %d > %d",
                           url, _clen, MAX_BYTES)
            return None
        body = r.content
        if len(body) > MAX_BYTES:
            logger.warning("http.get_impersonated refused oversized response (%s): %d bytes > %d",
                           url, len(body), MAX_BYTES)
            return None
        return body
    except Exception as exc:  # noqa: BLE001 — the failure->None contract (same as http.get)
        logger.warning("http.get_impersonated failed (%s): %s", url, exc)
        return None


def post_json(url: str, *, json: Any = None, timeout: int = DEFAULT_TIMEOUT,
              headers: Optional[dict] = None, **kwargs: Any) -> Optional[Any]:
    """POST a JSON body and parse the JSON response. None on any failure."""
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    resp = _request_capped("POST", url, timeout=timeout, headers=hdrs, json=json, **kwargs)
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("http.post_json parse failed (%s): %s", url, exc)
        diag.note("http.post_json", url=url, status=resp.status_code, body=resp.text, exc=exc)
        return None


def put_json(url: str, *, json: Any = None, timeout: int = DEFAULT_TIMEOUT,
             headers: Optional[dict] = None, **kwargs: Any) -> Optional[Any]:
    """PUT a JSON body and parse the JSON response. None on any failure. Some search APIs
    (e.g. ModelScope's /models listing) are PUT-shaped; same contract as post_json."""
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    resp = _request_capped("PUT", url, timeout=timeout, headers=hdrs, json=json, **kwargs)
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("http.put_json parse failed (%s): %s", url, exc)
        diag.note("http.put_json", url=url, status=resp.status_code, body=resp.text, exc=exc)
        return None


# ── ASYNC SIBLINGS (S3b) ───────────────────────────────────────────────────────────────────────
# The async version of the ONE choke point ~190 adapters egress through. PURE ADDITION: the sync
# helpers above are byte-identical; these async siblings MIRROR every sync guarantee (cache_only->None,
# the SSRF pre-flight, the MAX_BYTES streamed cap, raise_for_status, rebuild-Response-from-raw-bytes,
# failure->None + diag.note) with the SAME diag labels ("http.get"/"http.post"/... NOT "http.aget"), so a
# converted adapter's /eye-fix evidence is identical whether it went sync or async. The contextvars
# (cache.cache_only/fresh + diag.note/enable/drain) propagate NATURALLY through await (the coroutine runs
# in the awaiter's context), so we just CALL those sync fns; the per-thread set/reset was a THREAD-model
# workaround, not needed here. No operation is converted and no adapter awaits these yet (S4 does that).
_aclient: Optional[httpx.AsyncClient] = None
_aclient_lock = threading.Lock()  # construction is sync (no await); double-check like _get_client


def _aget_client() -> httpx.AsyncClient:
    """Lazily build (once) the shared pooled async client. Double-checked lock so the first concurrent
    async callers create exactly one. Async twin of _get_client (same http2 + Limits + UA + timeout)."""
    global _aclient
    if _aclient is None:
        with _aclient_lock:
            if _aclient is None:
                # Lazy import to break the http <-> safeurl cycle (same reason as _get_client): by the
                # time the first request builds the client, safeurl is fully loaded.
                from omniseek.core import safeurl  # noqa: PLC0415 (lazy: breaks the import cycle)
                # Build the real async HTTP transport explicitly (same http2 + limits as the sync client),
                # then wrap it so EVERY request AND every redirect hop is _netguard-validated at the
                # connection layer (S1-C3 async twin). headers / follow_redirects / timeout stay
                # Client-level so httpx still owns redirect semantics; the wrapper only refuses an
                # SSRF-class hop's connection.
                _awrapped = httpx.AsyncHTTPTransport(
                    http2=_http2_ok(),
                    limits=httpx.Limits(max_keepalive_connections=64,
                                        max_connections=128,
                                        keepalive_expiry=30.0),
                )
                # PARITY NOTE: passing an explicit transport= makes httpx skip env/system-proxy
                # auto-detection (allow_env_proxies = trust_env AND transport is None). This is HARMLESS
                # for the current deployment, which egresses via transparent fake-IP TUN (verified 2026-07:
                # no *_proxy env on OmniSeek-http process, empty scutil --proxies, utun interfaces present),
                # so httpx never used a proxy anyway. If this eye is ever moved to a PROXY-based egress
                # (HTTP_PROXY / system proxy), restore proxy support by passing proxy= to the wrapped
                # AsyncHTTPTransport (or mounts= of AsyncSSRFGuardTransport-wrapped proxied transports) so
                # the SSRF guard still wraps the proxied connection.
                _aclient = httpx.AsyncClient(
                    transport=safeurl.AsyncSSRFGuardTransport(_awrapped),
                    headers={"User-Agent": USER_AGENT},
                    follow_redirects=True,
                    timeout=DEFAULT_TIMEOUT,
                )
    return _aclient


async def _arequest_capped(method: str, url: str, *, timeout: int, headers: dict,
                           **kwargs: Any) -> Optional[httpx.Response]:
    """Async twin of _request_capped: stream a request, aborting if the body exceeds ``MAX_BYTES``.
    Returns a fully-read ``httpx.Response`` (so ``.text``/``.json()`` work) or ``None`` on any failure /
    oversize. Byte-identical guarantees + SAME diag labels as the sync path."""
    if cache.cache_only():
        return None  # cache-only mode (cache_only=True): do NO live HTTP, the single egress guard
    # SSRF pre-flight (belt-and-suspenders): same predicate as the sync twin. Redundant with the per-hop
    # AsyncSSRFGuardTransport on the pooled client, kept for the clear initial block message + diag.note
    # evidence tap; the extra getaddrinfo is OS-cached, so negligible. Residual (DNS-rebind TOCTOU) is the
    # same as sync: the IP-pinning lane stays safe_fetch.
    # OFF-LOOP (S4b): security_block_reason resolves getaddrinfo (a BLOCKING syscall). On a native async
    # method it runs ON the loop, so a slow/cold DNS would freeze EVERY coroutine. Push it to a worker
    # thread. IDENTICAL guard DECISION (same _netguard, same block reasons, same diag.note); only moved
    # off the loop. The sync twin (_request_capped) keeps its inline call (it runs on a worker thread).
    _blk = await anyio.to_thread.run_sync(_netguard.security_block_reason, url)
    if _blk is not None:
        logger.warning("http.%s blocked SSRF-class target (%s): %s", method.lower(), url, _blk)
        diag.note(f"http.{method.lower()}", url=url, status=None, body=f"blocked SSRF-class target: {_blk}")
        return None
    try:
        async with _aget_client().stream(method, url, timeout=timeout, headers=headers, **kwargs) as r:
            r.raise_for_status()
            raw = bytearray()
            async for chunk in r.aiter_raw():
                raw += chunk
                if len(raw) > MAX_BYTES:
                    logger.warning("http.%s refused oversized response (%s): >%d bytes",
                                   method.lower(), url, MAX_BYTES)
                    diag.note(f"http.{method.lower()}", url=url, status=r.status_code,
                              body=f"refused oversized response (>{MAX_BYTES} bytes)")
                    return None
        # Rebuild a normal already-read Response from the raw body + original headers, so
        # content-encoding / charset decoding happens exactly as the buffered path did.
        return httpx.Response(r.status_code, headers=r.headers, content=bytes(raw),
                              request=r.request)
    except Exception as exc:  # noqa: BLE001 , failure → None is the adapter contract
        logger.warning("http.%s failed (%s): %s", method.lower(), url, exc)
        # A non-2xx surfaces here as httpx.HTTPStatusError → surface its status + body snippet so
        # the fixing agent sees the wall (403/412 anti-bot, 404 moved endpoint), not just a string.
        st = getattr(getattr(exc, "response", None), "status_code", None)
        bd = None
        if isinstance(exc, httpx.HTTPStatusError):
            try:
                bd = exc.response.text
            except Exception:  # noqa: BLE001
                bd = None
        diag.note(f"http.{method.lower()}", url=url, status=st, body=bd, exc=exc)
        return None


async def aget(url: str, *, timeout: int = DEFAULT_TIMEOUT, headers: Optional[dict] = None,
               params: Optional[dict] = None, **kwargs: Any) -> Optional[httpx.Response]:
    """Async GET with the shared UA + redirects + raise_for_status, size-capped. None on failure."""
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    return await _arequest_capped("GET", url, timeout=timeout, headers=hdrs, params=params, **kwargs)


async def aget_json(url: str, **kwargs: Any) -> Optional[Any]:
    """Async GET and parse JSON. None on request failure OR unparseable body."""
    resp = await aget(url, **kwargs)
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("http.get_json parse failed (%s): %s", url, exc)
        diag.note("http.get_json", url=url, status=resp.status_code, body=resp.text, exc=exc)
        return None


async def aget_text(url: str, **kwargs: Any) -> Optional[str]:
    """Async GET and return response text. None on failure."""
    resp = await aget(url, **kwargs)
    return resp.text if resp is not None else None


async def apost_json(url: str, *, json: Any = None, timeout: int = DEFAULT_TIMEOUT,
                     headers: Optional[dict] = None, **kwargs: Any) -> Optional[Any]:
    """Async POST a JSON body and parse the JSON response. None on any failure."""
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    resp = await _arequest_capped("POST", url, timeout=timeout, headers=hdrs, json=json, **kwargs)
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("http.post_json parse failed (%s): %s", url, exc)
        diag.note("http.post_json", url=url, status=resp.status_code, body=resp.text, exc=exc)
        return None


async def aput_json(url: str, *, json: Any = None, timeout: int = DEFAULT_TIMEOUT,
                    headers: Optional[dict] = None, **kwargs: Any) -> Optional[Any]:
    """Async PUT a JSON body and parse the JSON response. None on any failure. Same contract as
    apost_json (some search APIs are PUT-shaped)."""
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    resp = await _arequest_capped("PUT", url, timeout=timeout, headers=hdrs, json=json, **kwargs)
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("http.put_json parse failed (%s): %s", url, exc)
        diag.note("http.put_json", url=url, status=resp.status_code, body=resp.text, exc=exc)
        return None


async def aclose_client() -> None:
    """Close the pooled async client (await its aclose) and reset it so a later build is clean. Fail-open
    (never raises). NOTE: S4 must wire this into the ASGI lifespan shutdown when the FIRST async caller
    lands. In S3b nothing builds _aclient, so nothing leaks yet; this is built here but NOT yet wired."""
    global _aclient
    _ac = _aclient
    _aclient = None
    if _ac is not None:
        try:
            await _ac.aclose()
        except Exception as exc:  # noqa: BLE001 , shutdown must never raise
            logger.warning("http.aclose_client failed: %s", exc)
