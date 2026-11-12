"""Disk-based cache for source responses.

Keyed by SHA-256 of the cache key. Values are JSON. Survives restarts.
Default TTL is 15 minutes — adapters can override per-call.
Inside a source call a miss on a fetch result is single-flight: one caller fetches a key, the others
of that moment wait for it (see "ONE FETCH PER KEY AT A TIME" below).
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import anyio
from platformdirs import user_cache_dir

from omniseek.core import _guard, portal
from omniseek.core import filters as _filters

# OMNISEEK_CACHE_DIR moves the whole cache elsewhere. The service leaves it unset; a test or one-off
# run sets it to a throwaway directory so it never reads the live service's cache entries (state
# such as reddit's cannot-search list, 2026-10-10) and never write into them. The application name is omniseek
# (2026-10-20); the deploy moves the old-named directory to the recycle area.
CACHE_DIR = Path(os.environ.get("OMNISEEK_CACHE_DIR")
                 or Path(user_cache_dir("omniseek", appauthor=False)) / "omniseek_cache")
DEFAULT_TTL = 900  # 15 minutes

# Per-thread "fresh" flag: when set, get()/get_docs() force a cache MISS (live fetch),
# while set()/set_docs() still WRITE — so a `fresh=True` request gets live data AND
# warms the cache for later normal calls. fetcher sets this inside each search worker.
_fresh_var: contextvars.ContextVar = contextvars.ContextVar("omniseek_cache_fresh", default=False)


def set_fresh(value: bool) -> None:
    _fresh_var.set(bool(value))


# Per-thread "refresh margin" (seconds): when > 0, get()/get_docs() treat an entry that
# expires within this many seconds as a MISS (return None → live refetch, which rewrites
# the cache), while a still-comfortably-warm entry is served from cache untouched. This is
# the basis of the prewarmer's refresh-if-near-expiry pass: warm a source's expensive fetch
# ONLY when its cache is missing or about to lapse, instead of re-paying the full live cost
# every cycle on data whose TTL is hours away. It targets the adapter's OWN cache keys (the
# per-PI / per-affiliation / per-source keys the warm fetch would write), because the check
# happens inside the same get() the adapter already calls. `fresh` still wins (it forces a
# miss unconditionally); margin 0 (the default) is a no-op so all normal reads are unchanged.
_refresh_margin_var: contextvars.ContextVar = contextvars.ContextVar(
    "omniseek_cache_refresh_margin", default=0.0)


def set_refresh_margin(seconds: float) -> None:
    _refresh_margin_var.set(max(0.0, float(seconds)))


# Per-thread "cache-only" flag: when set, the live egresses (http._arequest_capped + cdp_call)
# short-circuit (return None / raise), so a search reads ONLY warm cache and does ZERO live work.
# This is the basis of the cache_only=True pickup: pick up walled results that have self-warmed after a fire,
# WITHOUT re-firing a still-cold source (no extra CDP nav / account traffic; poll-safe). The
# adapter always checks the cache BEFORE an egress, so a warm source returns normally; only a
# cache MISS hits the guarded egress and degrades to empty. fetcher sets this in each worker.
_cache_only_var: contextvars.ContextVar = contextvars.ContextVar("omniseek_cache_only", default=False)


def set_cache_only(value: bool) -> None:
    _cache_only_var.set(bool(value))


def cache_only() -> bool:
    return _cache_only_var.get()


def _key_path(key: str) -> Path:
    # A filtered call whose source translated the filters into separate upstream parameters gets its
    # own key space (filters.fingerprint() is '' on every unfiltered call, so those keys are unchanged).
    salt = _filters.fingerprint()
    if salt:
        key = f"{key}\x00filters={salt}"
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{h}.json"


def get(key: str) -> Optional[Any]:
    """Return cached value if present and not expired, else None."""
    if _fresh_var.get():
        return None  # caller requested fresh → force a live fetch
    return _read(key, _refresh_margin_var.get())


def _read(key: str, margin: float = 0.0) -> Optional[Any]:
    path = _key_path(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Corrupt / half-written (e.g. a kill mid-write from before atomic writes):
        # delete it so a stable sha256 key can't decode-fail on every hit until TTL.
        try:
            path.unlink()
        except OSError:
            pass
        return None
    except OSError:
        return None  # transient read error → miss, but don't delete
    # margin > 0 (the prewarmer's refresh-if-near-expiry pass): treat an entry within `margin`
    # seconds of expiry as a miss so the adapter refetches and rewrites it; a comfortably-warm
    # entry is served untouched. margin 0 (the default) collapses this to the plain TTL check.
    if data.get("expires_at", 0) < time.time() + margin:
        return None
    return data.get("value")


def _atomic_write_text(path: Path, text: str) -> None:
    """Write atomically: a unique temp file in the same dir + ``os.replace`` (an atomic
    rename on POSIX & Windows same-volume). Concurrent writers → last rename wins, never
    a half-file; a kill mid-write leaves only a ``.tmp`` (never a corrupt final file)."""
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# The failure-empty FLOOR (structural, 2026-07-10). An EMPTY value ([], '', {}, None) is capped to a
# short TTL BY DEFAULT, because an empty result is almost always a TRANSIENT miss (a down / rate-limited
# / breaker-open / all-rows-failed upstream) that must NOT be pinned for a long ttl: a later drill would
# then get a cache HIT with zero egress and the outage is masked (the enrich-null-cached-24h bug + its 7
# siblings). This lives in the ONE primitive every write funnels through, so no adapter can reintroduce
# the bug BY OMISSION. A caller with a GENUINELY authoritative empty it wants remembered long passes
# authoritative_empty=True: explicit, greppable, reviewable — the shift from patch to structure.
EMPTY_TTL_CAP = 300


def _is_empty(value: Any) -> bool:
    """Values that must not be pinned long unless vouched: None, or an empty list/tuple/dict/str.
    NOT 0 / False / 0.0 — those are MEANINGFUL scalars (a real count of zero, a flag), not 'no result'.
    (set/frozenset are intentionally omitted: they are not JSON-serializable so never reach this cache;
    also the module's own ``set`` name shadows the builtin, so naming it here would be the function.)"""
    if value is None:
        return True
    if isinstance(value, (list, tuple, dict, str)):
        return len(value) == 0
    return False


def set(key: str, value: Any, ttl: Optional[int] = None, *,  # noqa: A001 (shadows built-in)
        authoritative_empty: bool = False) -> None:
    """Store a value with the given TTL (seconds).

    FLOOR: an EMPTY value (see _is_empty) is capped to EMPTY_TTL_CAP unless ``authoritative_empty=True``.
    The safe default is that an un-vouched empty self-heals fast rather than masking a transient outage;
    pass authoritative_empty=True only for a GENUINE no-result answer you want remembered long."""
    eff_ttl = ttl or DEFAULT_TTL
    if _is_empty(value) and not authoritative_empty:
        eff_ttl = min(eff_ttl, EMPTY_TTL_CAP)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _key_path(key)
    payload = {"expires_at": time.time() + eff_ttl, "value": value}
    _atomic_write_text(path, json.dumps(payload, default=str, ensure_ascii=False))
    _end_flight(path, filled=True)   # a caller waiting on this key reads what was just written


def clear_expired() -> int:
    """Delete cache files whose TTL has passed. The TTL only gates READS; expired
    files otherwise sit on disk forever, so a long-lived host slowly accumulates
    dead JSON. Called at service startup (serve_http). Returns count removed."""
    if not CACHE_DIR.exists():
        return 0
    now = time.time()
    removed = 0
    for f in CACHE_DIR.glob("*.json"):
        try:
            expired = json.loads(f.read_text(encoding="utf-8")).get("expires_at", 0) < now
        except (OSError, json.JSONDecodeError):
            expired = True  # unreadable/corrupt: treat as dead
        if expired:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def clear() -> int:
    """Remove all cache entries. Returns count removed."""
    if not CACHE_DIR.exists():
        return 0
    count = 0
    for f in CACHE_DIR.glob("*.json"):
        try:
            f.unlink()
            count += 1
        except OSError:
            pass
    return count


def make_key(*parts: str) -> str:
    """Helper to build a stable cache key from parts."""
    return "|".join(str(p) for p in parts)


def seconds_until_expiry(key: str) -> Optional[float]:
    """Seconds until ``key``'s entry expires, or None if it is missing / corrupt / already
    expired. READ-ONLY (ignores the ``fresh`` and refresh-margin contextvars): it reports the
    on-disk TTL state, it does not fetch. A negative result is never returned (a lapsed entry
    is None, same as absent), so a caller can use ``v is None or v < margin`` as 'needs refresh'."""
    path = _key_path(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    remaining = data.get("expires_at", 0) - time.time()
    return remaining if remaining > 0 else None


# -----------------------------------------------------------------------------
# ONE FETCH PER KEY AT A TIME (2026-10-20). Measured 2026-10-18 16:35 to 16:46: about 40
# queries fanned out at once and 285 requests were turned away by OmniSeek's own gates in 11 minutes,
# much of it the same cache key fetched side by side (v2ex's six node listings, the data.gov.sg pages).
# Inside a ``flight_scope`` (the fetcher opens one around every source call) a cache miss on a fetch
# result (``aget_docs`` / ``aget_once`` and their sync entries) is single-flight: the first caller of a key
# fetches it, every other caller of the same key waits for that fetch and reads what it wrote. The wait
# ends at the waiter's own deadline (``_guard.current_deadline``): past it the waiter goes on as it
# would have before (fetches itself, and the gates refuse it if its time is up), so no number is
# chosen here; a caller with no deadline does not wait. A leader's flight ends when it writes the key
# (``set``) or when its scope ends (the source call returned, written or not); a waiter whose leader
# wrote nothing tries again, leading the next fetch if nobody else does. Outside a scope (health
# checks, tools that read the cache directly) nothing changes.
# -----------------------------------------------------------------------------


class _Flight:
    __slots__ = ("done", "scope", "filled")

    def __init__(self, scope) -> None:
        self.done = threading.Event()
        self.scope = scope
        self.filled = False


class _FlightScope:
    __slots__ = ("led",)

    def __init__(self) -> None:
        self.led: dict = {}   # keys this scope leads (a dict: this module's own ``set`` shadows the builtin)


_flights: dict[str, _Flight] = {}
_flights_lock = threading.Lock()
_scope_var: contextvars.ContextVar = contextvars.ContextVar("omniseek_cache_flight_scope", default=None)


@contextlib.contextmanager
def flight_scope():
    """One source call: cache misses inside it are single-flight (see the section above); the flights
    it leads end when it ends."""
    scope = _FlightScope()
    token = _scope_var.set(scope)
    try:
        yield scope
    finally:
        try:
            _scope_var.reset(token)
        except (ValueError, RuntimeError):  # reset from another context: the value dies with it
            pass
        with _flights_lock:
            keys = list(scope.led)
        for k in keys:
            _end_flight_key(k, scope, filled=False)


def _end_flight(path: Path, filled: bool) -> None:
    _end_flight_key(str(path), None, filled)


def _end_flight_key(k: str, scope, filled: bool) -> None:
    with _flights_lock:
        fl = _flights.get(k)
        if fl is None or (scope is not None and fl.scope is not scope):
            return
        del _flights[k]
        fl.filled = filled
        fl.scope.led.pop(k, None)
    fl.done.set()


def _join(key: str) -> Optional[_Flight]:
    """None: this caller fetches the key (it now leads its flight, or no single flight applies here).
    A flight: another caller is fetching the key; wait for it."""
    scope = _scope_var.get()
    if scope is None or _cache_only_var.get():   # a cache_only collect stays instant: it never waits
        return None
    k = str(_key_path(key))   # the filters salt included: a filtered call is another key
    with _flights_lock:
        fl = _flights.get(k)
        if fl is None:
            _flights[k] = _Flight(scope)
            scope.led[k] = True
            return None
        return None if fl.scope is scope else fl


def _time_left() -> Optional[float]:
    d = _guard.current_deadline()
    return None if d is None else d - time.monotonic()


async def _await_flight(key: str) -> Optional[Any]:
    """After a miss: lead the key's flight (None: fetch it), or wait on the loop for the caller already
    fetching it and return what it wrote."""
    while True:
        fl = _join(key)
        left = _time_left()
        if fl is None or left is None or left <= 0:
            return None
        end = time.monotonic() + left
        while not fl.done.is_set():
            if time.monotonic() >= end:
                return None
            await anyio.sleep(_guard._ADMISSION_POLL_S)
        if fl.filled:
            value = await anyio.to_thread.run_sync(_read, key, limiter=portal.IO_LANE)
            if value is not None:
                return value


# -----------------------------------------------------------------------------
# Document convenience — the list[Document] round-trip that ~38
# adapters were re-implementing inline (cache.get → model_validate / cache.set →
# model_dump(mode="json")). Behavior is identical; this just removes the boilerplate.
# -----------------------------------------------------------------------------


def get_docs(key: str):
    """Sync entry for a caller on a thread: ``aget_docs`` on the event loop (portal.call), so there is
    one implementation of the single-flight read."""
    return portal.call(aget_docs, key)


def _read_docs(key: str):
    cached = get(key)
    return None if cached is None else _to_docs(cached)


async def aget_docs(key: str):
    """Return a cached ``list[Document]`` if present + fresh, else None.

    Inside a ``flight_scope`` a miss is single-flight (see the section above): when another caller
    is already fetching this key, wait for its result (until this caller's own deadline) instead of
    fetching it a second time; otherwise this caller becomes the one that fetches it. The first read
    runs in a worker thread (disk IO off the loop), the wait for another caller's fetch on the loop
    (no worker thread blocks on it)."""
    cached = await anyio.to_thread.run_sync(_read_docs, key, limiter=portal.IO_LANE)
    if cached is not None:
        return cached
    raw = await _await_flight(key)
    return None if raw is None else _to_docs(raw)


def get_once(key: str) -> Optional[Any]:
    """Sync entry for a caller on a thread: ``aget_once`` on the event loop (portal.call)."""
    return portal.call(aget_once, key)


async def aget_once(key: str) -> Optional[Any]:
    """``get`` with the single flight of ``aget_docs``: for a cached fetch result (a raw payload) whose
    key is always written after a successful fetch. Never for a state key that a fetch may leave
    unwritten (a caller would wait for nothing). First read in a worker thread, the wait on the loop."""
    hit = await anyio.to_thread.run_sync(get, key, limiter=portal.IO_LANE)
    if hit is not None:
        return hit
    return await _await_flight(key)


def _to_docs(cached):
    from omniseek.core.normalize import Document  # local import: avoid import cycle
    return [Document.model_validate(d) for d in cached]


def set_docs(key: str, docs, ttl: Optional[int] = None, empty_ttl: Optional[int] = None, *,
             authoritative_empty: bool = False) -> None:
    """Cache a ``list[Document]`` (JSON round-trip), with TTL.

    An empty result is capped by the cache.set FLOOR (EMPTY_TTL_CAP) BY DEFAULT, so a transient upstream
    miss self-heals no matter what — the ``empty_ttl=X if docs else Y`` discipline is now the primitive's
    job, not each adapter's. Two opt-in ways to VOUCH that an empty is a genuine no-result (kept long):
    ``empty_ttl`` names the empty TTL explicitly (honored authoritatively); ``authoritative_empty=True``
    vouches the given ``ttl`` for an empty, for a source that computes ttl itself (e.g. the declarative
    family's reached-vs-failed split). Omit both for the safe default (the floor caps an empty)."""
    payload = [d.model_dump(mode="json") for d in docs]
    if not docs and empty_ttl is not None:
        set(key, payload, ttl=empty_ttl, authoritative_empty=True)  # caller explicitly chose the empty TTL
    else:
        set(key, payload, ttl=ttl, authoritative_empty=authoritative_empty)


# -----------------------------------------------------------------------------
# Whole-URL read cache. The URL branch of omniseek_read (fetcher.fetch_url_with_reason) stores every
# successful read here, so the layered reads an agent makes in a row (head, then a section or two)
# reuse one download instead of fetching the page again each time. The value is the WHOLE document;
# windowing (start_char / max_chars / grep / level / section) is cut after it comes out. Sources that
# keep their own cache are untouched. The generic web read (web_fallback.py) kept a second copy of
# the same page for the same hour; it was removed (2026-11-03): it duplicated
# this one and kept credential-bearing addresses on disk that this cache refuses.
# -----------------------------------------------------------------------------

# One hour, the value the generic web read used for its own page cache before it was folded into
# this one. Provisional: revisit once a month of hit records exists.
URL_READ_TTL = 3600

# A doc carrying one of these flags is a stand-in (a search snippet, a generic read standing in for the
# owning adapter that failed, a partial archive copy): an honest answer for this call, never one to reuse.
_URL_READ_DEGRADED = ("body_needs_read", "fulltext_fallback", "owner_adapter_missed", "partial")


def url_read_key(url: str) -> str:
    """Cache key of a URL read: scheme and host lowercased, the #fragment dropped, the query kept.
    Two links that differ only in their fragment point at the same document, so they share one entry."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(str(url).strip())
    norm = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))
    return make_key("url_read", norm)


def url_read_cacheable(doc) -> bool:
    """A read worth keeping: a document with a non-empty body and no stand-in flag."""
    if doc is None or not (getattr(doc, "content", "") or "").strip():
        return False
    meta = getattr(doc, "metadata", None) or {}
    return not any(meta.get(flag) for flag in _URL_READ_DEGRADED)


def get_url_read(url: str):
    """``(Document, fetched_at)`` for a cached read of ``url``, else None."""
    from omniseek.core.normalize import Document  # local import: avoid import cycle

    value = get(url_read_key(url))
    if not isinstance(value, dict) or not isinstance(value.get("doc"), dict):
        return None
    return Document.model_validate(value["doc"]), value.get("fetched_at")


def set_url_read(url: str, doc, fetched_at: str) -> bool:
    """Keep a successful read of ``url`` for URL_READ_TTL seconds. Returns False (nothing written)
    for a read that is not worth keeping (see url_read_cacheable)."""
    if not url_read_cacheable(doc):
        return False
    set(url_read_key(url), {"doc": doc.model_dump(mode="json"), "fetched_at": fetched_at},
        ttl=URL_READ_TTL)
    return True
