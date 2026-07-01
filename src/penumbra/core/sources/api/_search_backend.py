"""Shared web-search backend for search-index sources (Blind/Glassdoor/X/脉脉/LinkedIn-A).

Reaches walled venues WITHOUT scraping them: queries a SEARCH ENGINE with a
``site:``-scoped query and returns the engine's indexed title+url+snippet. ToS-clean
— reads the engine, never hits the walled site with our UA, so it sidesteps every
robots.txt block on the target.

Backend is pluggable + keyless-by-default:
  * Brave Search API (robust) when ``~/.polaris/credentials/brave.json`` has ``api_key``;
  * else DuckDuckGo HTML (keyless, fragile — soft-rate-limits with HTTP 202, so we
    pace + retry). Drop a Brave key any time to upgrade with zero code change.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"
_BRAVE_CRED = Path.home() / ".polaris" / "credentials" / "brave.json"

# Brave resilience: a circuit-breaker + a ~1-req/s rate gate (free tier ≈ 1 qps). Without
# these, a dead/over-quota key is re-hit every call (wasted round-trip that can keep a
# rate-limited key pinned at 429), and a concurrent fan-out trips 429 instantly. State is
# module-global, guarded by _brave_lock.
_brave_lock = threading.Lock()
_brave_cooldown_until = 0.0   # skip Brave entirely until this wall-clock time
_brave_last_call = 0.0        # last Brave request time (min-interval pacing)
_BRAVE_MIN_INTERVAL = 1.1     # seconds between Brave calls
_ping_lock = threading.Lock()

# Pooled client: reuse keep-alive connections to the Brave API / DDG endpoints across the 10
# search-index venues instead of a fresh TCP+TLS handshake per call (~0.5-1.5s to the overseas
# Brave endpoint). Thread-safe; the 1qps Brave gate + circuit breaker are untouched.
_client: Optional[httpx.Client] = None
_client_init_lock = threading.Lock()


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        with _client_init_lock:
            if _client is None:
                try:
                    import h2  # noqa: F401
                    _h2 = True
                except Exception:  # noqa: BLE001
                    _h2 = False
                _client = httpx.Client(timeout=15, http2=_h2, follow_redirects=True,
                                       limits=httpx.Limits(max_keepalive_connections=8,
                                                           max_connections=16, keepalive_expiry=30.0))
    return _client


class _BraveUnavailable(Exception):
    """Brave key broken (401/403) or rate-limited (429) — fall back to DDG + back off."""

    def __init__(self, msg: str, cooldown: float) -> None:
        super().__init__(msg)
        self.cooldown = cooldown


def _brave_key():
    try:
        return (json.loads(_BRAVE_CRED.read_text(encoding="utf-8")) or {}).get("api_key") or None
    except Exception:  # noqa: BLE001
        return None


def _brave(query: str, n: int, key: str) -> list[dict]:
    global _brave_last_call
    # Rate gate: serialize Brave calls ≥ _BRAVE_MIN_INTERVAL apart (free tier ~1 qps), so a
    # concurrent venue fan-out can't trip 429.
    with _brave_lock:
        wait = _BRAVE_MIN_INTERVAL - (time.time() - _brave_last_call)
        if wait > 0:
            time.sleep(wait)
        _brave_last_call = time.time()
    r = _get_client().get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": min(n, 20)},
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
        timeout=15,
    )
    if r.status_code in (401, 403):
        raise _BraveUnavailable(f"brave key rejected ({r.status_code})", 3600)  # broken → 1h
    if r.status_code == 429:
        ra = r.headers.get("Retry-After", "")
        raise _BraveUnavailable("brave rate-limited (429)", int(ra) if ra.isdigit() else 60)
    r.raise_for_status()
    web = (r.json().get("web") or {}).get("results") or []
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("description", "")} for x in web[:n]]


def _ddg(query: str, n: int) -> list[dict]:
    for attempt in range(3):
        try:
            r = _get_client().post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                headers={"User-Agent": UA},
                timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ddg request failed: %s", exc)
            return []
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "lxml")
            out: list[dict] = []
            for res in soup.select(".result, .web-result"):
                a = res.select_one(".result__a")
                sn = res.select_one(".result__snippet")
                href = a.get("href") if a else None
                if a and href:
                    out.append({"title": a.get_text(strip=True), "url": href,
                                "snippet": sn.get_text(strip=True) if sn else ""})
                if len(out) >= n:
                    break
            return out
        if r.status_code == 202:  # DDG soft rate-limit — pace and retry
            time.sleep(2.0 + attempt * 2.0)
            continue
        return []
    return []


def search_web(query: str, n: int = 8) -> list[dict]:
    """``site:``-scoped web search → ``[{title, url, snippet}]``. Brave if keyed, else DDG.

    (CN-engine backends rejected 2026-06-03: cn.bing.com IGNORES the ``site:`` operator
    → returns general web junk; Baidu blocks the bot; Sogou's xhs index is sparse. Brave
    honors ``site:`` and is the ceiling for safe engine-read CN-walled content.)"""
    global _brave_cooldown_until
    key = _brave_key()
    if key and time.time() >= _brave_cooldown_until:
        try:
            return _brave(query, n, key)
        except _BraveUnavailable as exc:
            _brave_cooldown_until = time.time() + exc.cooldown  # circuit-breaker: stop hitting it
            logger.warning("brave unavailable (%s) → DDG for %.0fs", exc, exc.cooldown)
        except Exception as exc:  # noqa: BLE001
            logger.warning("brave search failed, falling back to ddg: %s", exc)
    return _ddg(query, n)


_ping = {"t": 0.0, "ok": None, "msg": ""}


def backend_ping() -> tuple[bool, str]:
    """Cached (10 min) backend reachability — so N venues' health checks cost ~1 real hit
    (avoids a rate-limit storm when the watchdog probes every search-index venue)."""
    now = time.time()
    if _ping["ok"] is not None and now - _ping["t"] < 600:
        return _ping["ok"], _ping["msg"]
    with _ping_lock:  # double-checked: cold/expired cache under concurrent probes → ONE real hit
        now = time.time()
        if _ping["ok"] is not None and now - _ping["t"] < 600:
            return _ping["ok"], _ping["msg"]
        backend = "brave" if _brave_key() else "ddg"
        try:
            search_web("site:example.com test", n=1)  # reachable if no exception (empty is fine)
            ok, msg = True, f"OK ({backend} backend)"
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"{backend}: {type(exc).__name__}: {exc}"
        _ping.update(t=now, ok=ok, msg=msg)
        return ok, msg
