"""Shared web-search backend for search-index sources (Blind/Glassdoor/X/脉脉/LinkedIn-A).

Reaches walled venues WITHOUT scraping them: queries a SEARCH ENGINE with a
``site:``-scoped query and returns the engine's indexed title+url+snippet. ToS-clean
— reads the engine, never hits the walled site with our UA, so it sidesteps every
robots.txt block on the target.

Backend is pluggable + keyless-by-default:
  * Brave Search API (robust) when ``~/.omniseek/credentials/brave.json`` has ``api_key``;
  * else DuckDuckGo HTML (keyless, fragile — soft-rate-limits with HTTP 202, so we
    pace, retry, then break the circuit). Drop a Brave key any time to upgrade with
    zero code change.

Both halves are paced + circuit-broken over ONE module-global ledger shared by the sync
and async twins; ``backend_state()`` is the pure read of it, and when neither half can
serve, ``search_web`` raises instead of sending (a request into a cooling backend buys
no information and only re-arms the limiter).
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import anyio
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"
_BRAVE_CRED = Path.home() / ".omniseek" / "credentials" / "brave.json"

# Brave resilience: a circuit-breaker + a ~1-req/s rate gate (free tier ≈ 1 qps). Without
# these, a dead/over-quota key is re-hit every call (wasted round-trip that can keep a
# rate-limited key pinned at 429), and a concurrent fan-out trips 429 instantly. State is
# module-global, guarded by _brave_lock.
_brave_lock = threading.Lock()
_brave_cooldown_until = 0.0   # skip Brave entirely until this wall-clock time
_brave_last_call = 0.0        # last Brave request time (min-interval pacing)
_BRAVE_MIN_INTERVAL = 1.1     # seconds between Brave calls
_ping_lock = threading.Lock()

# DDG resilience, the SYMMETRIC half (2026-09-10). DDG was the last-resort backend with NO gate and
# NO breaker: five search-index venues named in one gather pushed Brave into cooldown, all five fell
# onto DDG at once, DDG soft-limited (HTTP 202), and every later call re-hit a limiter it had no way
# to know about — ten sources reading as dead with zero information for the caller. Same shape as
# Brave now: a min-interval gate + an exponential circuit-breaker, one ledger shared by sync + async
# under _ddg_lock. 2.5s because DDG's HTML endpoint limits harder than Brave's paid 1 qps.
_ddg_lock = threading.Lock()
_ddg_last_call = 0.0          # last DDG request time (min-interval pacing)
_DDG_MIN_INTERVAL = 2.5       # seconds between DDG calls
_ddg_cooldown_until = 0.0     # skip DDG entirely until this wall-clock time
_ddg_consecutive_trips = 0    # consecutive breaker trips; a 200 clears it
_DDG_COOLDOWN_BASE = 90.0     # first trip cools 90s, then 180s, 360s ... (2 ** trips)
_DDG_COOLDOWN_MAX = 600.0     # ceiling: a longer blackout helps nobody
_DDG_TRANSIENT_RETRIES = 2    # bounded retries for a transient network fault (3 tries total)
_DDG_TRANSIENT_BACKOFF = 1.5  # seconds between those retries
_DDG_SOFT_LIMIT_TRIES = 3     # consecutive HTTP 202s that trip the breaker

# The last backend failure, one line, for backend_state() to hand the caller. A cooling backend is
# only actionable if the caller can see WHY it is cooling.
_last_error: Optional[str] = None

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


def _clock(ts: float) -> str:
    """Wall-clock HH:MM:SS for a cooldown deadline — the form a waiting caller can act on."""
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _fail(msg: str) -> RuntimeError:
    """Record a backend failure for ``backend_state`` and return the error for the caller to raise."""
    global _last_error
    _last_error = msg
    return RuntimeError(msg)


def _ddg_guard() -> None:
    """Refuse to spend a request while DDG is cooling. The breaker only works if a retry inside the
    window sends NOTHING: hitting a soft-limited endpoint again just re-arms its limiter, which is
    exactly how one fan-out kept ten venues dead. Shared by the sync + async twins."""
    remaining = _ddg_cooldown_until - time.time()
    if remaining > 0:
        raise _fail(f"ddg soft rate-limit; cooling {math.ceil(remaining)}s until "
                    f"{_clock(_ddg_cooldown_until)}; no request sent")


def _ddg_trip() -> RuntimeError:
    """Open the DDG breaker after its soft limit exhausted this call's retries: back off
    exponentially (capped), count the trip, and return the error to raise."""
    global _ddg_cooldown_until, _ddg_consecutive_trips
    with _ddg_lock:
        cooldown = min(_DDG_COOLDOWN_BASE * 2 ** _ddg_consecutive_trips, _DDG_COOLDOWN_MAX)
        _ddg_consecutive_trips += 1
        _ddg_cooldown_until = time.time() + cooldown
        until = _ddg_cooldown_until
    logger.warning("ddg soft rate-limited → cooling %.0fs (trip %d)", cooldown, _ddg_consecutive_trips)
    return _fail(f"ddg soft rate-limit; cooling {cooldown:.0f}s until {_clock(until)}")


def _ddg_ok() -> None:
    """A 200 clears the trip count — the backoff escalates on CONSECUTIVE failures only."""
    global _ddg_consecutive_trips
    with _ddg_lock:
        _ddg_consecutive_trips = 0


def _ddg_gate() -> None:
    """Rate gate: serialize DDG calls ≥ _DDG_MIN_INTERVAL apart, so a concurrent venue fan-out
    can't soft-limit it. RESERVE the next slot under the lock (brief, no IO), then sleep OFF the
    lock. Unlike ``_brave``'s gate this must not sleep while holding the lock: ``_ddg_lock`` is also
    taken by ``_ddg_trip`` / ``_ddg_ok``, which the async twin calls ON the event loop, so a sleep
    held under it would stall every coroutine for up to one interval."""
    global _ddg_last_call
    with _ddg_lock:
        now = time.time()
        wait = _DDG_MIN_INTERVAL - (now - _ddg_last_call)
        if wait < 0:
            wait = 0.0
        _ddg_last_call = now + wait  # reserve the slot at the reserved start time
    if wait > 0:
        time.sleep(wait)


def _ddg_parse(html: str, n: int) -> list[dict]:
    """Pure parse of a DDG HTML result page → [{title, url, snippet}] (no IO, no state)."""
    soup = BeautifulSoup(html, "lxml")
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


def _ddg(query: str, n: int) -> list[dict]:
    _ddg_guard()  # cooling → raise WITHOUT sending anything
    soft = 0       # consecutive HTTP 202 soft rate-limits this call
    transient = 0  # transient network faults this call
    while True:
        _ddg_gate()
        try:
            r = _get_client().post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                headers={"User-Agent": UA},
                timeout=15,
            )
        except httpx.TransportError as exc:
            # A transport fault (connect / read / remote-protocol / an SSL EOF) is usually the link
            # blinking, not the engine refusing. Retrying it a bounded number of times is the
            # difference between a blip and a declared TOTAL failure of web search.
            if transient < _DDG_TRANSIENT_RETRIES:
                transient += 1
                logger.warning("ddg transport error (%s) — retry %d/%d", exc, transient,
                               _DDG_TRANSIENT_RETRIES)
                time.sleep(_DDG_TRANSIENT_BACKOFF)
                continue
            raise _fail(f"DDG request failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            logger.warning("ddg request failed: %s", exc)
            # DDG is the LAST-RESORT backend (search_web falls back to it when Brave is unkeyed),
            # so its failure is a TOTAL failure of web search. Returning [] published that as
            # "the web has no such page", which is the one thing a retrieval layer must never say.
            raise _fail(f"DDG request failed: {exc}") from exc
        if r.status_code == 200:
            _ddg_ok()
            return _ddg_parse(r.text, n)
        if r.status_code == 202:  # DDG soft rate-limit — pace and retry, then break the circuit
            soft += 1
            if soft >= _DDG_SOFT_LIMIT_TRIES:
                raise _ddg_trip()
            time.sleep(2.0 + (soft - 1) * 2.0)
            continue
        raise _fail(f"DDG request failed: HTTP {r.status_code}")


def _both_cooling(key: Optional[str]) -> RuntimeError:
    """The BOTH-HALVES-DOWN error: name each half's deadline and say plainly that nothing was sent,
    so the caller waits the stated seconds instead of retrying into two closed circuits."""
    now = time.time()
    brave_part = (f"brave until {_clock(_brave_cooldown_until)} "
                  f"({math.ceil(max(0.0, _brave_cooldown_until - now))}s)") if key else "brave unkeyed"
    return _fail(f"web-search backend cooling: {brave_part}, ddg until {_clock(_ddg_cooldown_until)} "
                 f"({math.ceil(max(0.0, _ddg_cooldown_until - now))}s); no request sent")


def search_web(query: str, n: int = 8) -> list[dict]:
    """``site:``-scoped web search → ``[{title, url, snippet}]``. Brave if keyed, else DDG.

    Both halves are circuit-broken, so the FIRST thing this does is read the two breakers: when
    neither can serve it raises WITHOUT sending anything (a request into a cooling backend buys no
    information and re-arms the limiter). ``backend_state()`` is the pure read of the same ledger.

    (CN-engine backends rejected 2026-06-03: cn.bing.com IGNORES the ``site:`` operator
    → returns general web junk; Baidu blocks the bot; Sogou's xhs index is sparse. Brave
    honors ``site:`` and is the ceiling for safe engine-read CN-walled content.)"""
    global _brave_cooldown_until
    key = _brave_key()
    now = time.time()
    brave_ok = bool(key) and now >= _brave_cooldown_until
    ddg_ok = now >= _ddg_cooldown_until
    if not brave_ok and not ddg_ok:
        raise _both_cooling(key)
    if brave_ok:
        try:
            return _brave(query, n, key)
        except _BraveUnavailable as exc:
            _brave_cooldown_until = time.time() + exc.cooldown  # circuit-breaker: stop hitting it
            _fail(f"brave: {exc}")
            logger.warning("brave unavailable (%s) → DDG for %.0fs", exc, exc.cooldown)
        except Exception as exc:  # noqa: BLE001
            _fail(f"brave: {type(exc).__name__}: {exc}")
            logger.warning("brave search failed, falling back to ddg: %s", exc)
        if not ddg_ok:  # Brave just fell over and DDG is cooling → nothing left to send with
            raise _both_cooling(key)
    return _ddg(query, n)


# --- native-async twins (the search-index venues + nowcoder go native async) ---------------------
# Byte-faithful mirror of search_web / _brave / _ddg. SAME Brave 1qps rate gate (shared _brave_last_call
# + _brave_lock) + SAME cooldown breaker (_brave_cooldown_until) so async and sync share ONE rate ledger
# + ONE circuit. Only the BLOCKING waits go async: the Brave rate gate reserves the slot under the lock
# (brief, no IO) then `await anyio.sleep` OFF the lock (a `time.sleep` holding _brave_lock on the loop
# would freeze every coroutine); the network on a shared httpx.AsyncClient; the DDG 202 backoff via
# anyio.sleep. The BeautifulSoup parse is pure CPU, on the loop. A pure addition; sync is untouched.
_aclient: Optional[httpx.AsyncClient] = None
_aclient_init_lock = threading.Lock()


def _aget_client() -> httpx.AsyncClient:
    global _aclient
    if _aclient is None:
        with _aclient_init_lock:
            if _aclient is None:
                try:
                    import h2  # noqa: F401
                    _h2 = True
                except Exception:  # noqa: BLE001
                    _h2 = False
                _aclient = httpx.AsyncClient(timeout=15, http2=_h2, follow_redirects=True,
                                             limits=httpx.Limits(max_keepalive_connections=8,
                                                                 max_connections=16, keepalive_expiry=30.0))
    return _aclient


async def _abrave(query: str, n: int, key: str) -> list[dict]:
    global _brave_last_call
    # Rate gate: RESERVE the next Brave slot under the lock (brief, no IO), then wait for it OFF the lock
    # via anyio.sleep -- holding _brave_lock across an await would block the loop. Shares _brave_last_call
    # with sync _brave so async + sync Brave calls together stay >= _BRAVE_MIN_INTERVAL apart (~1 qps).
    with _brave_lock:
        now = time.time()
        wait = _BRAVE_MIN_INTERVAL - (now - _brave_last_call)
        if wait < 0:
            wait = 0.0
        _brave_last_call = now + wait  # reserve the slot at the reserved start time
    if wait > 0:
        await anyio.sleep(wait)
    r = await _aget_client().get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": min(n, 20)},
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
        timeout=15,
    )
    if r.status_code in (401, 403):
        raise _BraveUnavailable(f"brave key rejected ({r.status_code})", 3600)
    if r.status_code == 429:
        ra = r.headers.get("Retry-After", "")
        raise _BraveUnavailable("brave rate-limited (429)", int(ra) if ra.isdigit() else 60)
    r.raise_for_status()
    web = (r.json().get("web") or {}).get("results") or []
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("description", "")} for x in web[:n]]


async def _addg_gate() -> None:
    """Async twin of ``_ddg_gate``: RESERVE the next DDG slot under the lock (brief, no IO), then
    wait for it OFF the lock via anyio.sleep — holding _ddg_lock across an await would block the
    loop. Shares _ddg_last_call with sync ``_ddg``, so async + sync stay ONE paced stream."""
    global _ddg_last_call
    with _ddg_lock:
        now = time.time()
        wait = _DDG_MIN_INTERVAL - (now - _ddg_last_call)
        if wait < 0:
            wait = 0.0
        _ddg_last_call = now + wait  # reserve the slot at the reserved start time
    if wait > 0:
        await anyio.sleep(wait)


async def _addg(query: str, n: int) -> list[dict]:
    _ddg_guard()  # cooling → raise WITHOUT sending anything (SAME ledger as the sync twin)
    soft = 0       # consecutive HTTP 202 soft rate-limits this call
    transient = 0  # transient network faults this call
    while True:
        await _addg_gate()
        try:
            r = await _aget_client().post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                headers={"User-Agent": UA},
                timeout=15,
            )
        except httpx.TransportError as exc:
            # A transport fault (connect / read / remote-protocol / an SSL EOF) is usually the link
            # blinking, not the engine refusing. Retrying it a bounded number of times is the
            # difference between a blip and a declared TOTAL failure of web search.
            if transient < _DDG_TRANSIENT_RETRIES:
                transient += 1
                logger.warning("ddg transport error (%s) — retry %d/%d", exc, transient,
                               _DDG_TRANSIENT_RETRIES)
                await anyio.sleep(_DDG_TRANSIENT_BACKOFF)
                continue
            raise _fail(f"DDG request failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            logger.warning("ddg request failed: %s", exc)
            # DDG is the LAST-RESORT backend (search_web falls back to it when Brave is unkeyed),
            # so its failure is a TOTAL failure of web search. Returning [] published that as
            # "the web has no such page", which is the one thing a retrieval layer must never say.
            raise _fail(f"DDG request failed: {exc}") from exc
        if r.status_code == 200:
            _ddg_ok()
            return _ddg_parse(r.text, n)  # pure CPU, on the loop
        if r.status_code == 202:  # DDG soft rate-limit — pace and retry, then break the circuit
            soft += 1
            if soft >= _DDG_SOFT_LIMIT_TRIES:
                raise _ddg_trip()
            await anyio.sleep(2.0 + (soft - 1) * 2.0)
            continue
        raise _fail(f"DDG request failed: HTTP {r.status_code}")


async def asearch_web(query: str, n: int = 8) -> list[dict]:
    """Native-async twin of ``search_web``: Brave (if keyed + not cooling) else DDG, SAME cooldown
    breakers (``_brave_cooldown_until`` / ``_ddg_cooldown_until``) shared with the sync path,
    including the both-halves-down early exit. Byte-identical routing + result shape."""
    global _brave_cooldown_until
    key = _brave_key()
    now = time.time()
    brave_ok = bool(key) and now >= _brave_cooldown_until
    ddg_ok = now >= _ddg_cooldown_until
    if not brave_ok and not ddg_ok:
        raise _both_cooling(key)
    if brave_ok:
        try:
            return await _abrave(query, n, key)
        except _BraveUnavailable as exc:
            _brave_cooldown_until = time.time() + exc.cooldown
            _fail(f"brave: {exc}")
            logger.warning("brave unavailable (%s) → DDG for %.0fs", exc, exc.cooldown)
        except Exception as exc:  # noqa: BLE001
            _fail(f"brave: {type(exc).__name__}: {exc}")
            logger.warning("brave search failed, falling back to ddg: %s", exc)
        if not ddg_ok:  # Brave just fell over and DDG is cooling → nothing left to send with
            raise _both_cooling(key)
    return await _addg(query, n)


def _cooling_until_iso(until: float) -> Optional[str]:
    """ISO-8601 local deadline for a breaker, or None when that half is not cooling."""
    if until <= time.time():
        return None
    try:
        return datetime.fromtimestamp(until).astimezone().isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001 (a formatting failure must never break a state read)
        return None


def backend_state() -> dict:
    """PURE READ of the shared backend's ledger — sends NOTHING, judges nothing.

    The caller's missing half: when ten venues come back empty at once, "the backend is cooling for
    another 84s" and "these venues have no such page" are opposite conclusions, and nothing used to
    tell them apart. omniseek_search stamps this into ``_meta.web_search_backend`` when it is not nominal.

    ``active`` is the half a call would use RIGHT NOW ("none" = both closed, so a call sends
    nothing); ``cooling_s`` is the remaining seconds rounded UP, 0 when that half is open."""
    now = time.time()
    brave_left = max(0.0, _brave_cooldown_until - now)
    ddg_left = max(0.0, _ddg_cooldown_until - now)
    keyed = bool(_brave_key())
    active = "brave" if (keyed and brave_left <= 0) else ("ddg" if ddg_left <= 0 else "none")
    return {
        "nominal": brave_left <= 0 and ddg_left <= 0,
        "active": active,
        "brave": {"keyed": keyed, "cooling_s": math.ceil(brave_left),
                  "cooling_until": _cooling_until_iso(_brave_cooldown_until)},
        "ddg": {"cooling_s": math.ceil(ddg_left),
                "cooling_until": _cooling_until_iso(_ddg_cooldown_until),
                "consecutive_trips": _ddg_consecutive_trips,
                "min_interval_s": _DDG_MIN_INTERVAL},
        "last_error": _last_error,
    }


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
        state = backend_state()
        if state["active"] == "none":
            # A probe must not spend the very thing it is checking: both halves are closed, so send
            # NOTHING. True is deliberate — a cooldown is a transient state that self-heals, and a
            # False here would let the watchdog mark all ten venues down and HIDE them for a blip.
            # Deliberately NOT cached either, so the next probe after the cooldown sees the truth.
            return True, (f"OK (backend cooling: brave {state['brave']['cooling_s']}s / "
                          f"ddg {state['ddg']['cooling_s']}s; will self-heal)")
        backend = "brave" if _brave_key() else "ddg"
        try:
            search_web("site:example.com test", n=1)  # reachable if no exception (empty is fine)
            ok, msg = True, f"OK ({backend} backend)"
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"{backend}: {type(exc).__name__}: {exc}"
        msg = f"{msg} [active: {backend_state()['active']}]"
        _ping.update(t=now, ok=ok, msg=msg)
        return ok, msg
