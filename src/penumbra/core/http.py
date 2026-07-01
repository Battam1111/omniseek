"""Shared HTTP helpers — one User-Agent, one timeout policy, one error contract.

~20 adapters each re-implemented the same wrapper: ``httpx.get`` + a UA header +
``follow_redirects=True`` + ``raise_for_status()`` + ``try/except → None`` (with
4+ divergent UA strings). These helpers centralize that. They return ``None`` on
*any* failure (logged), so adapters keep their "failure → empty result" contract
without per-file boilerplate.

Open-API adapters (arXiv / OpenAlex / Crossref / DBLP / Hacker News / …) should
use these. Anti-bot / walled adapters (mokahr / feishu / xiaohongshu / bytedance)
keep their own bespoke headers + signing — do NOT route those through here.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

import httpx

from penumbra.core import _netguard, cache, diag

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; polaris-eye/0.1)"
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
                _client = httpx.Client(
                    headers={"User-Agent": USER_AGENT},
                    follow_redirects=True,
                    timeout=DEFAULT_TIMEOUT,
                    http2=_http2_ok(),
                    limits=httpx.Limits(max_keepalive_connections=64,
                                        max_connections=128,
                                        keepalive_expiry=30.0),
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
    # SSRF pre-flight: refuse a URL whose host resolves to a private/loopback/link-local/reserved
    # IP (169.254.169.254 cloud-metadata, 127/10/192.168, ...). Closes the direct eye_add_url ->
    # web_fallback -> http.get attacker path; a 'dns' miss is NOT blocked (the fetch fails on its
    # own). (Redirect-to-private is a residual: the pooled client follows redirects without per-hop
    # re-validation; the attacker-URL drill path eye_add_url should additionally route through
    # curator.probe.safe_fetch, which IP-pins + re-validates every hop.)
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

    NOTE: we do NOT inject our PolarisEye UA here — ``impersonate='chrome'`` sets a Chrome-consistent
    UA + header order, and overriding the UA would desync the very fingerprint we are matching."""
    try:
        from curl_cffi import requests as _creq  # lazy: keep curl_cffi off the hot import path
    except Exception as exc:  # noqa: BLE001 — missing/broken dep -> degrade, never crash
        logger.warning("http.get_impersonated unavailable (curl_cffi import failed): %s", exc)
        return None
    try:
        r = _creq.get(url, impersonate="chrome", timeout=timeout,
                      headers=dict(headers) if headers else None, allow_redirects=True)
        r.raise_for_status()
        return r.content
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
