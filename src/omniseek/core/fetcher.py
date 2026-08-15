"""Unified fetcher — the entry point for OmniSeek eye operations.

All source adapters register themselves with this module via
register_adapter(). The fetcher then routes queries to the appropriate
adapter and aggregates results.

The MCP server (omniseek.server) wraps these functions as tools.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

import anyio

from omniseek.core.normalize import Document

logger = logging.getLogger(__name__)

# search_many() fans out across sources in parallel, then returns after a per-source DEADLINE: a
# slow/hung source is dropped from THIS search but keeps running in the background, warming its
# cache for the next call. This bounds a broad search to ~the deadline, not the slowest source's tail.
#
# WHY 64 AND NOT >= THE LIVE COUNT (measured 2026-07-25; do not "optimize" this without re-measuring).
# A broad search submits ~93 sources here, so 64 does NOT start them all at once and the tail queues
# for a thread. That queueing is REAL, not theoretical: instrumenting _egress start times showed 16
# sources waiting >1s, worst 3.59s of the 16s window. Raising the pool removes it completely (at 100:
# zero waiters, worst 0.11s). It looks like a free win, and it is not:
#   single broad search    64 -> 100 workers:  277 -> 289 docs, 61 -> 64 sources  (INSIDE the measured
#                                              +/-25% doc noise band, so NO proven yield gain)
#   5 CONCURRENT searches  64 ->  96 workers:  peak threads 635 -> 1125, RSS 880 -> 976MB,
#                                              wall 16.6s -> 24.3s  (WORSE by 46%)
# The stacked case is the one that decides it: omniseek_gather really does run several broad searches at
# once, each with its OWN pool, so the pool size multiplies. Past the point where threads outnumber
# the _EGRESS_SEM slots they contend for, extra threads buy nothing and cost GIL + memory + spawn time.
# The elapsed baseline's variance is only +/-2%, so that 46% is far outside noise.
# So the thread pool is deliberately NOT sized to "everything concurrent": _EGRESS_SEM below is the
# real concurrency control, and 64 is where a single search's queueing stays small while a stacked
# gather stays bounded. The cost is that ~16 sources start up to ~3.6s late in a single broad search.
_SEARCH_WORKERS = 64
# PROCESS-GLOBAL egress bound (loop-agnostic). omniseek_gather runs each batched call on its own thread
# and drives an async tool body (omniseek_search) to completion on a FRESH event loop per call
# (server.omniseek_gather). anyio's default thread limiter is PER-LOOP, so N stacked gather calls get N
# independent limiters, making process-wide egress concurrency N times the per-loop cap (unbounded
# in N). That exhausted the fd budget under the 5-way domain sweep (macOS "Too many open files").
# This ONE threading.BoundedSemaphore is shared across every loop / pool / thread, so ALL source
# egress (the sync search_many leaf and the async _dispatch_search leaf) can never exceed it, no
# matter how callers stack. Sized to server._THREAD_TOKENS, i.e. generously above one broad search's
# ~93 sources so this semaphore never throttles a SINGLE search; only cross-call stacking meets it.
# (It used to claim a single broad search therefore "runs its ~95 sources fully concurrent". Measured
# 2026-07-25: it does NOT, because _SEARCH_WORKERS=64 queues the tail well before this gate. That is a
# deliberate trade, justified where _SEARCH_WORKERS is defined; this cap is simply not the binder.)
# serve_http raises the soft fd limit far above this, so the ceiling sits well under the real budget.
_EGRESS_CAP = 256
_EGRESS_SEM = threading.BoundedSemaphore(_EGRESS_CAP)

# Async searches can run on several event loops at once (the main MCP loop plus one fresh loop per
# omniseek_gather child). AnyIO's worker limiter is loop-local, so acquiring _EGRESS_SEM *inside* a
# worker lets every loop create its own waiting workers before the process-global permit is won.
# Under stacked gathers that amplified a 256-request egress ceiling into 800+ process threads and
# starved even omniseek_sources. Poll the non-blocking, process-global semaphore while still a coroutine,
# then create the worker only after admission. The 20ms handoff quantum is below 0.2% of the
# shortest 11s broad-search deadline while bounding a fully queued 256-waiter case to 12,800 cheap
# semaphore checks/s.
_EGRESS_ADMISSION_POLL_S = 0.02


def _egress(adapter, query: str, limit: int) -> list:
    """Run one adapter's blocking .search under the process-global concurrent-egress bound
    (_EGRESS_SEM). Every fan-out routes its leaf network call through here, so the eye's total
    in-flight source connections stay bounded no matter how many event loops or pools stack."""
    with _EGRESS_SEM:
        return adapter.search(query, limit)


async def _aegress(adapter, query: str, limit: int) -> list:
    """Acquire process-global egress capacity before occupying an AnyIO worker thread."""
    while not _EGRESS_SEM.acquire(blocking=False):
        await anyio.sleep(_EGRESS_ADMISSION_POLL_S)
    try:
        return await anyio.to_thread.run_sync(
            functools.partial(adapter.search, query, limit),
            abandon_on_cancel=False,
        )
    finally:
        _EGRESS_SEM.release()


# BROAD (sources=None) cap. Two values, by freshness:
#  - default (cache-allowed) path: 11s. MEASURED (2026-06-13, after the L1 slow-source
#    parallelization + the eye-prewarm cron + the S2 API key): a broad on a NEW query
#    completes EVERY source except reddit within ~10s — reddit (~25s, host-bound on the
#    Arctic mirror) is the only straggler, and it is dropped at 16s too, then self-warms.
#    So 11s drops nothing 16s kept. (The old 16s was set to wait for "semantic_scholar ~12-15s";
#    that comment is now stale — with the API key S2 is ~4s.)
#  - fresh=True path: 16s. A caller asking for LIVE data opted out of the cache, so the
#    pre-warmed sources are forced cold + contended; give that explicit-completeness path
#    the original budget (it can still raise deadline_s). Keeps broad breadth fully intact
#    for the one path where the 11s default could otherwise drop a contended cold source.
_SOURCE_DEADLINE_S = 11
_SOURCE_DEADLINE_FRESH_S = 16
_EXPLICIT_DEADLINE_S = 45  # When the caller NAMES sources, they chose them → wait for them;
                          # this is only a backstop against a genuinely hung source.
# EXCLUDED from the broad (sources=None) fan-out: slow and/or account/credential/
# quota-sensitive sources. Each adapter DECLARES itself via an ``explicit_only``
# attribute (True, or better a short reason string, surfaced in the omniseek_sources roster's
# explicit_only_reason and in _meta.excluded_relevant when query-relevant);
# config-driven sources carry it on their row. This dict remains ONLY as an
# emergency override (name -> reason) for an adapter you cannot edit right now;
# it is normally EMPTY. The smoke gate freezes the effective set, so it can only
# change deliberately (edit the adapter AND the frozen list in tests/smoke.py).
_EXPLICIT_ONLY_SOURCES: dict[str, str] = {}

# A REVERSIBLE runtime retire overlay (the curator one-tap omniseek_curator_retire_live): name -> a
# "retired:<reason> <date>" reason string. Lives OUTSIDE the deploy tree (rides the state backup,
# pristine tree), so a live retire takes effect with NO restart and NO git. CACHED in-process (the
# broad-search routing loop reads _explicit_only_reason once per source per query, a disk read
# there would be O(sources x queries)); the cache is invalidated on every write to the overlay
# (apply_live.invalidate_explicit_only_overrides), so a live retire/rollback shows up at once. The
# DURABLE half (the in-tree explicit_only edit + the smoke frozen-list line) is staged for the operator.
_EXPLICIT_ONLY_OVERRIDES_PATH = (
    Path.home() / ".omniseek" / "state" / "curator" / "explicit_only_overrides.json")
_explicit_only_overrides_cache: "Optional[dict[str, str]]" = None
_overrides_lock = threading.Lock()


def _explicit_only_overrides() -> "dict[str, str]":
    """The runtime retire overlay (cached). Tolerant: missing/corrupt/not-a-dict -> {} (logged),
    NEVER raises into the broad-search routing path."""
    global _explicit_only_overrides_cache
    if _explicit_only_overrides_cache is None:
        with _overrides_lock:
            if _explicit_only_overrides_cache is None:
                data: dict = {}
                try:
                    if _EXPLICIT_ONLY_OVERRIDES_PATH.exists():
                        raw = json.loads(
                            _EXPLICIT_ONLY_OVERRIDES_PATH.read_text(encoding="utf-8"))
                        if isinstance(raw, dict):
                            data = {str(k): str(v) for k, v in raw.items() if k and v}
                        else:
                            logger.warning(
                                "explicit_only_overrides.json is not a dict -> {}")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("explicit_only_overrides.json unreadable (%s) -> {}", exc)
                _explicit_only_overrides_cache = data
    return _explicit_only_overrides_cache


def invalidate_explicit_only_overrides() -> None:
    """Drop the cached retire overlay so the next _explicit_only_reason reads the fresh file.
    Called after a live retire/rollback writes the overlay (the broad fan-out picks it up at once)."""
    global _explicit_only_overrides_cache
    with _overrides_lock:
        _explicit_only_overrides_cache = None


def retired_reason(adapter: "SourceAdapter") -> str:
    """The reversible retire-overlay reason for this adapter ('' = not retired). The ONE place that
    answers 'is this source retired, and why': the curator one-tap parks a source by writing a
    'retired:<reason> <date>' overlay row (apply_live / source_audit own the WRITE side). Every
    consumer (the discovery grid, the P4 placement view, the health sweep) reads THIS instead of
    each re-parsing a 'retired:' prefix off explicit_only. A retire is the STRONGEST exclusion fact:
    it takes precedence over a static explicit_only (see _explicit_only_reason), so retiring an
    already explicit_only source (a walled one, say) stays observable, not masked by the class reason.
    """
    ov = _explicit_only_overrides().get(adapter.name, "")
    return ov if ov.lower().startswith("retired") else ""


def _explicit_only_reason(adapter: "SourceAdapter") -> str:
    """Why this adapter is excluded from the broad fan-out ('' = included).

    A reversible retire wins first (the strongest, operator-applied exclusion, always observable);
    then the adapter's own ``explicit_only`` attribute (True or a reason string); then any non-retire
    runtime overlay entry; then the emergency override dict.
    """
    rr = retired_reason(adapter)
    if rr:
        return rr
    v = getattr(adapter, "explicit_only", False)
    if v:
        return v if isinstance(v, str) else "explicit-only"
    ov = _explicit_only_overrides().get(adapter.name)
    if ov:
        return ov
    return _EXPLICIT_ONLY_SOURCES.get(adapter.name, "")


# A NEUTRAL descriptive FACT (like the routing facets), not a verdict: how fragile a source's
# reach is, so the agent / curator can prioritise repairs (the brittle first) and a consumer can
# set expectations. Four ordered classes, least-to-most fragile:
#   stable  — a public, no-key API or RSS feed (an HTTP contract; breaks only on a real API change)
#   keyed   — needs a free key / credential (rate-limited, can lock out, but a documented contract)
#   scrape  — parses a site's HTML/JSON; a site redesign silently breaks the selectors
#   walled  — CDP + a logged-in session (the most fragile + the highest maintenance cost)
_STABILITY_VALUES = ("stable", "keyed", "scrape", "walled")


def _derive_stability(adapter: "SourceAdapter") -> str:
    """Infer a source's stability class from WHERE its adapter lives + WHAT it needs.

    Centralised so we never touch the ~190 adapters one by one (mirrors how facets fall back to
    a derivation): the module path is the physical organising axis (sources/walled, sources/api,
    sources/scrape), credentials + the RSS/declarative bases fill the rest. An adapter may set a
    class/instance attribute ``stability`` (one of the four values) to OVERRIDE the default, the
    same escape hatch facets give — anything else falls through to the derivation.
    """
    override = getattr(adapter, "stability", None)
    if override in _STABILITY_VALUES:
        return override
    module = type(adapter).__module__ or ""
    # walled FIRST: a CDP source carries a logged-in session even if it also needs_credentials
    # (xiaohongshu is needs_credentials=False yet fully walled) — the wall is the fragile part.
    if ".sources.walled." in module or module.endswith(".sources.walled"):
        return "walled"
    # RSS feeds are an HTTP contract → stable, even though RSSAdapterBase physically lives under
    # scrape/ (catch it by base class BEFORE the scrape-module fallthrough).
    from omniseek.core.sources.scrape._rss import RSSAdapterBase
    if isinstance(adapter, RSSAdapterBase):
        return "stable"
    # keyed: a documented API behind a free key / credential.
    if getattr(adapter, "needs_credentials", False):
        return "keyed"
    # stable: a public no-key API — the declarative REST/JSON table rows + the hand-written
    # api/ adapters (arxiv / dblp / openalex / crossref / hackernews …).
    from omniseek.core.sources._declarative import DeclarativeAPIAdapter
    if isinstance(adapter, DeclarativeAPIAdapter) or ".sources.api." in module:
        return "stable"
    # scrape: the remaining HTML/JSON scrape majority under scrape/ (and any uncategorised
    # adapter) — a site redesign breaks it, so it is the default-fragile bucket short of walled.
    return "scrape"


# Legal-facing ACCESS TIER (free < keyed < walled < circumvention) — the catalog's self-describing
# legal posture (see docs/LEGAL-POSTURE.md), distinct from the `stability` fragility class. Its one
# unique signal over stability is `circumvention`: a source that defeats an access control (decrypts
# an encrypted response, etc.), which is NEVER in the default pack. An adapter may set an
# ``access_tier`` attr to OVERRIDE; otherwise derived from the explicit_only reason + stability.
_ACCESS_TIERS = frozenset({"free", "keyed", "walled", "circumvention"})
_CIRCUMVENTION_RE = re.compile(r"circumvention|§?\s*1201|解密|decrypt|defeat", re.I)


def _derive_access_tier(adapter: "SourceAdapter") -> str:
    override = getattr(adapter, "access_tier", None)
    if override in _ACCESS_TIERS:
        return override
    # circumvention is a STATIC legal posture (the source's OWN explicit_only declaration), not the
    # runtime state: read the class attr, NOT _explicit_only_reason. Otherwise a retire overlay
    # ("retired:...") would win over the class reason and silently drop the circumvention tier of a
    # retired source (mokahr_ats is the one source that declares circumvention via its reason string).
    _static_eo = getattr(adapter, "explicit_only", "")
    if isinstance(_static_eo, str) and _CIRCUMVENTION_RE.search(_static_eo):
        return "circumvention"
    stab = _derive_stability(adapter)
    # WALLED is the login/CDP wall (a real access barrier); a source that merely needs a FREE key is
    # KEYED, not walled. Deriving walled from needs_credentials FIRST made every keyed public API
    # (Exa, Podcast Index, …) mislabel as walled and drop out of thin memory (is_walled_source). Check
    # the walled STABILITY first, THEN map credentialed/keyed sources to keyed.
    if stab == "walled":
        return "walled"
    if stab == "keyed" or getattr(adapter, "needs_credentials", False):
        return "keyed"
    return "free"  # public, no key (stable / scrape)


def is_walled_source(name: str) -> bool:
    """Whether source ``name`` is WALLED or circumvention tier — the operator-privacy boundary the
    thin-memory tap keys off (what a logged-in account has retrieved is operator privacy, so those
    docs become perception history only on an explicit profile opt-in). Keyed by NAME so callers
    without the adapter (the recall writer hook) can classify. Fail-open to False (treat as public)
    when the adapter is unknown or classification errors: walled adapters live under
    ``sources/walled/`` (a stable module-path derivation), so this only ever mislabels the truly
    non-walled, never the reverse."""
    try:
        a = get_adapter(name)
        if a is None:
            return False
        return _derive_access_tier(a) in ("walled", "circumvention")
    except Exception:  # noqa: BLE001 — classification must never break the caller
        return False


from omniseek.core import profile as _profile  # noqa: E402  — stdlib-only module, no import cycle


def _profile_enabled(name: str, adapter: "SourceAdapter") -> bool:
    """Whether the deployment PROFILE exposes this source (broad fan-out + named omniseek_fetch). No
    profile -> True (pre-profile behavior; an existing host is unaffected). Derives the facets that
    profile.is_source_enabled needs (stability/domains/regions/kind) from the adapter + facets.json,
    so a deployer's group/region/walled rules apply across ~190 adapters without per-adapter edits.
    The walled-tier gate here is the ROBUST version of the explicit_only hotfix: it keys off the
    DERIVED stability, so a new walled source can't leak into broad by forgetting an attribute."""
    fb = _FACETS.get(name) or {}
    return _profile.is_source_enabled(
        name,
        stability=_derive_stability(adapter),
        domains=getattr(adapter, "domains", None) or fb.get("domains") or [],
        regions=getattr(adapter, "regions", None) or fb.get("regions") or [],
        kind=getattr(adapter, "kind", "") or fb.get("kind") or "",
    )


def is_enabled_by_profile(name: str) -> bool:
    """Public predicate for the named path (omniseek_fetch): True if the profile exposes ``name`` (or the
    adapter is unknown — let the normal 'unknown source' path handle that)."""
    a = get_adapter(name)
    return a is None or _profile_enabled(name, a)


# P19 health-watchdog state — surfaced (advisory) in list_sources so the agent can
# route around currently-dead sources. Recent, not instant (watchdog runs 6-hourly).
_WATCHDOG_STATE = Path.home() / ".omniseek" / "state" / "health-watchdog-state.json"

# Routing-facet fallback table: adapters / config rows may declare kind / domains /
# regions on themselves; facets.json decorates the rest (one data file, not code).
_FACETS_PATH = Path(__file__).with_name("facets.json")
try:
    _FACETS: dict = json.loads(_FACETS_PATH.read_text(encoding="utf-8"))
except Exception:  # noqa: BLE001 — facets are optional decoration, never fatal
    _FACETS = {}


@runtime_checkable
class SourceAdapter(Protocol):
    """Protocol that every source adapter implements."""

    name: str
    needs_credentials: bool
    description: str  # Brief description shown in list_sources()

    def search(self, query: str, limit: int = 10) -> list[Document]:
        """Search the source for the query, return up to `limit` documents."""
        ...

    def fetch_url(self, url: str) -> Optional[Document]:
        """Fetch a single URL from this source. Return None if URL doesn't belong."""
        ...

    def health_check(self) -> tuple[bool, str]:
        """Return (is_healthy, status_message)."""
        ...


# -----------------------------------------------------------------------------
# Registry
# -----------------------------------------------------------------------------

_adapters: dict[str, SourceAdapter] = {}
# Names registered MORE THAN ONCE this process. A collision means a config row and a
# coded adapter (or two rows) share a name and one silently replaced the other; the
# smoke gate (tests/smoke.py) asserts this list is empty before any deploy goes live.
_collisions: list[str] = []

# Guards STRUCTURAL mutation of _adapters (the live-apply / rollback path) and the snapshot
# reads that iterate it (all_adapter_names / list_sources / fetch_url / _probe_all_health). The
# import-time register_adapter runs single-threaded at module import (before any worker thread
# exists), so it does NOT take the lock; only the LIVE path (register_adapter_live /
# unregister_adapter, run from a worker thread while broad searches iterate _adapters) does. An
# RLock so a future caller that already holds it can re-enter without deadlock. get_adapter stays
# LOCK-FREE (the 64-worker hot path): a plain dict.get is atomic, and a concurrent insert is either
# visible or not to a given get, both correct; search_many._one + fetch_one already tolerate None.
_registry_lock = threading.RLock()


class AdapterCollision(ValueError):
    """A live re-register hit a name already registered. The LIVE path ABORTS on this (never the
    import-path's log-and-replace) so a one-tap apply can never silently shadow a running source."""


def register_adapter(adapter: SourceAdapter) -> None:
    """Register a source adapter. Called by each source's module on IMPORT (single-threaded).

    Keeps the historical log-and-REPLACE + _collisions semantics (smoke §2 asserts _collisions is
    empty at deploy; the import-time loaders de-dup base-wins so they never create a collision). The
    LIVE re-register path is register_adapter_live (collision-ABORT + lock-guarded), NOT this."""
    if adapter.name in _adapters:
        _collisions.append(adapter.name)
        logger.warning("Adapter %s already registered, replacing.", adapter.name)
    _adapters[adapter.name] = adapter
    logger.info("Registered adapter: %s", adapter.name)


def register_adapter_live(adapter: SourceAdapter) -> None:
    """LIVE re-register into the RUNNING worker's registry (the curator one-tap overlay lane).

    Distinct from the import-path register_adapter: this ABORTS on a name collision (raises
    AdapterCollision) instead of log-and-replace, so a live apply can never silently shadow a
    running source; and it mutates _adapters UNDER _registry_lock so a concurrent broad-search
    iteration (all_adapter_names / list_sources / fetch_url) can never raise 'dict changed size
    during iteration'. The caller (apply_live.apply_overlay_row) registers BEFORE writing the
    overlay row, so a collision aborts with nothing persisted."""
    with _registry_lock:
        if adapter.name in _adapters:
            raise AdapterCollision(
                f"refuse live re-register: {adapter.name!r} already registered")
        _adapters[adapter.name] = adapter
        _catalog_note_registered(adapter.name)  # rebuild ONLY this entry into the Layer-1 snapshot
        logger.info("live-registered adapter: %s", adapter.name)


def unregister_adapter(name: str) -> None:
    """Remove an adapter from the RUNNING registry (the curator rollback primitive). Under
    _registry_lock + idempotent: a double-rollback on an already-gone name is a no-op. After this
    get_adapter(name) returns None; fetch_one / search_many._one / fetch_url all tolerate that."""
    with _registry_lock:
        if _adapters.pop(name, None) is not None:
            _catalog_note_unregistered(name)  # drop this entry from the Layer-1 snapshot
            logger.info("live-unregistered adapter: %s", name)


def get_adapter(name: str) -> Optional[SourceAdapter]:
    return _adapters.get(name)  # LOCK-FREE hot path: dict.get is atomic; see _registry_lock note.


def all_adapter_names() -> list[str]:
    with _registry_lock:  # snapshot the keys under the lock, return the materialized copy
        return list(_adapters.keys())


def backend_of(name: str) -> str:
    """The upstream BACKEND a source maps to — the ONE de-dup key behind both the HONEST coverage
    count AND the corroboration-INDEPENDENCE count. The adapter's own ``backend`` wins, facets.json
    fills, else the source IS its own backend. A single derivation so list_sources,
    distinct_backend_count, and rank's corroboration collapse the SAME families identically (the
    OpenAlex slices → one backend) instead of re-deriving the formula inline in three places."""
    a = get_adapter(name)
    fb = _FACETS.get(name) or {}
    return (getattr(a, "backend", None) if a else None) or fb.get("backend") or name


# -----------------------------------------------------------------------------
# Public API (used by omniseek.server MCP tools)
# -----------------------------------------------------------------------------


_FETCH_ONE_DEADLINE_S = 90.0   # default backstop for a single-source fetch — generous for
                               # slow CDP/reddit/RSS yet bounds a genuinely hung source.
_FETCH_URL_TIMEOUT_S = 30.0    # default per-adapter cap when probing who-claims-this-URL.
                               # A genuinely slow adapter (e.g. a CDP source that scrolls a
                               # full comment thread) may declare a larger budget via a
                               # ``fetch_timeout`` attribute — the default stays tight so a
                               # STALLED adapter still can't hang omniseek_add_url.


def _run_bounded(fn, timeout: float):
    """Run ``fn()`` in a daemon thread; return ``(True, result)`` if it finished within
    ``timeout``, else ``(False, None)``. Re-raises whatever ``fn`` raised. A still-blocked
    thread is a daemon → it dies with the process and never holds the caller — the same
    primitive as ``health_check_bounded`` generalized to any callable."""
    box: dict = {}

    def _run() -> None:
        try:
            box["r"] = fn()
        except BaseException as exc:  # noqa: BLE001 — propagate to the caller thread
            if isinstance(exc, asyncio.CancelledError): raise  # D11: never eat a cancellation
            box["e"] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return False, None
    if "e" in box:
        raise box["e"]
    return True, box.get("r")


@dataclass(frozen=True)
class AdapterOutcome:
    """The first-class OUTCOME of one bounded source fetch (Wave 3): what HAPPENED, not just what
    came back. Makes the three conflations the old docs-only return hid impossible to miss: a
    timeout, an adapter error, and a full page (possibly truncated) are all distinct states here.

    Fields:
      ``docs``     the documents retrieved (``[]`` on timeout / error).
      ``state``    "completed" (the adapter returned), "timed_out" (exceeded ``deadline_s``; the
                   daemon thread was abandoned), or "errored" (the adapter raised; NOT re-raised by
                   ``fetch_outcome``).
      ``complete`` ``state == "completed"`` AND ``len(docs) < limit``: the enumeration FIT inside the
                   page, so nothing was truncated. A full page (``len(docs) == limit``) reads
                   ``complete=False`` (possibly more behind it).
      ``started``  the work began. With the current ``_run_bounded`` the dispatched thread always
                   starts, so this is True on every real path (including a timeout: started but did
                   not finish). The field exists so a future runner that can DECLINE a task under
                   load can honestly report False.
      ``reason``   "" on success; a one-line timeout / exception summary otherwise (evidence text,
                   deliberately NOT the live exception object).
      ``captures`` the drained ``diag`` evidence, exactly what ``fetch_one_with_diag`` drains today.
    """
    docs: list
    state: str
    complete: bool
    started: bool
    reason: str
    captures: list


def _derive_outcome(source: str, query: str, limit: int, fresh: bool,
                    deadline_s: Optional[float], cache_only: bool = False
                    ) -> "tuple[SourceAdapter, AdapterOutcome, Optional[BaseException]]":
    """The ONE derivation behind ``fetch_outcome`` / ``fetch_one`` / ``fetch_one_with_diag`` (Wave 3).

    Runs the adapter EXACTLY once under the shared ``diag`` capture and the shared ``_run_bounded``
    backstop (no second bounded-runner, no second diagnostics channel), and returns
    ``(adapter, outcome, raised)``. ``raised`` is the adapter's exception when it raised (so a VIEW
    can re-raise it and keep the historical error contract byte-for-byte); it is None otherwise.

    An UNKNOWN source raises ``ValueError`` HERE (a caller bug, before any dispatch): it is not an
    outcome, so every view propagates it. This function itself never raises for an ADAPTER fault:
    that becomes ``state="errored"`` with the reason and captures attached.
    """
    adapter = get_adapter(source)
    if adapter is None:
        raise ValueError(
            f"Unknown source: {source!r}. Known: {sorted(_adapters.keys())}"
        )
    from omniseek.core import cache, diag  # local import: avoid package-init import cycle

    def _work() -> tuple[list[Document], list]:
        cache.set_fresh(fresh)
        cache.set_cache_only(cache_only)  # cache-only (cache_only=True): egresses short-circuit at the funnels (mirrors search_many._one)
        diag.enable()  # arm capture in THIS worker thread (the contextvar is thread/context-local)
        try:
            docs = adapter.search(query, limit)
            if not cache_only:  # Path A: index enumerable docs (skip on a cache-only collect, mirroring
                from omniseek.core import recall  # search_many._one; a cache_only read has NO side effects)
                recall.maybe_ingest(docs)  # local import: avoid package-init cycle; no-op off eye-http
            return docs, diag.drain()
        except BaseException as exc:  # the adapter raised: note it, attach the trace, RE-RAISE
            try:
                diag.note(f"{source}.search", exc=exc)  # so the contract is byte-identical to before
                exc._eye_diag = diag.drain()  # type: ignore[attr-defined] (read by the caller below)
            except Exception:  # noqa: BLE001 (capture must never mask the real error)
                pass
            raise
        finally:
            cache.set_fresh(False)  # never leak `fresh` into the next request on a reused thread
            cache.set_cache_only(False)  # nor `cache_only` (same reset discipline as fresh)

    docs: list[Document] = []
    captures: list = []
    state = "completed"
    reason = ""
    raised: Optional[BaseException] = None
    try:
        if deadline_s is None:
            docs, captures = _work()
        else:
            ok, r = _run_bounded(_work, deadline_s)
            if not ok:
                # The bounded thread is still blocked (daemon, so it dies with the process); its
                # captures never drained. Report the timeout itself as the evidence and the historical [].
                logger.warning("fetch_one(%s) exceeded %.0fs deadline, returning []",
                               source, deadline_s)
                state = "timed_out"
                reason = f"exceeded the {deadline_s:.0f}s deadline (source hung or very slow)"
            else:
                docs, captures = r
    except BaseException as exc:  # noqa: BLE001 (an adapter fault is an OUTCOME here, never a raise)
        if isinstance(exc, asyncio.CancelledError): raise  # D11: cancellation is not an adapter fault
        state = "errored"
        raised = exc
        captures = getattr(exc, "_eye_diag", []) or []
        reason = f"{type(exc).__name__}: {exc}"[:300]
    complete = (state == "completed") and (len(docs) < limit)
    outcome = AdapterOutcome(docs=docs, state=state, complete=complete,
                             started=True, reason=reason, captures=captures)
    return adapter, outcome, raised


def fetch_outcome(source: str, query: str, limit: int = 10, fresh: bool = False,
                  deadline_s: Optional[float] = _FETCH_ONE_DEADLINE_S,
                  cache_only: bool = False) -> AdapterOutcome:
    """Fetch from ONE named source and return the first-class :class:`AdapterOutcome` (Wave 3).

    The ONE derivation; ``fetch_one`` and ``fetch_one_with_diag`` are thin VIEWS over the same run.
    Unlike the views, this NEVER raises for an adapter fault: a timeout is ``state="timed_out"``, an
    adapter exception is ``state="errored"`` with a reason and captures, because its consumer is a
    sweep loop (``recall.ingest_loop``) that must never die on one bad source. An UNKNOWN source
    still raises ``ValueError`` (a caller bug, not an outcome). Same bounded / ``fresh`` semantics as
    ``fetch_one``. ``cache_only=True`` (default False = byte-identical) runs the fetch with the cache-only
    contextvar set inside the worker, so every guarded egress short-circuits (zero live work).
    """
    _adapter, outcome, _raised = _derive_outcome(source, query, limit, fresh, deadline_s, cache_only)
    return outcome


def fetch_one(source: str, query: str, limit: int = 10, fresh: bool = False,
              deadline_s: Optional[float] = _FETCH_ONE_DEADLINE_S,
              cache_only: bool = False) -> list[Document]:
    """Fetch from ONE named source, BOUNDED by ``deadline_s`` (default 90s).

    A daemon-thread backstop means a hung source can never stall the caller (the
    watchtower daemon, ``omniseek_fetch``, ``omniseek_add_url``); on timeout it returns ``[]``.
    Pass ``deadline_s=None`` to opt OUT (deliberate unbounded: a slow source you truly
    want complete). ``fresh=True`` bypasses the cache for live data.

    Returns the documents only (the historical contract); ``fetch_one_with_diag`` is the
    sibling that also returns the failure-evidence trace for the /eye-fix repair loop.
    ``cache_only=True`` (default False = byte-identical) collects only warm cache (zero live egress).
    """
    return fetch_one_with_diag(source, query, limit, fresh=fresh, deadline_s=deadline_s,
                               cache_only=cache_only)[0]


def fetch_one_with_diag(source: str, query: str, limit: int = 10, fresh: bool = False,
                        deadline_s: Optional[float] = _FETCH_ONE_DEADLINE_S,
                        cache_only: bool = False
                        ) -> tuple[list[Document], Optional[dict]]:
    """``fetch_one`` plus a diagnostic for a FAILED/EMPTY fetch (the /eye-fix evidence tap).

    Arms the opt-in ``diag`` capture for THIS one run only (the broad search_many fan-out
    never arms it, so it stays zero-cost + zero cross-source pollution there), runs the
    adapter exactly as ``fetch_one`` did (SAME deadline + SAME error contract, an adapter
    exception still PROPAGATES), then drains the captured egress failures into a diagnostic.

    Returns ``(docs, diagnostic)``. ``diagnostic`` is ``None`` when the fetch returned docs
    without a captured failure (the no-noise success case); otherwise a dict::

        {"adapter_path": <the adapter's source file, for the fixing agent to open>,
         "returned": <doc count>,
         "captures": [ {helper, url?, status?, body?, exc?}, ... ],
         "note": <one human-readable line>}

    On a propagating adapter error the captured egress failures are stashed on the exception
    (``exc._eye_diag``) so ``omniseek_fetch`` can still surface them; the historical contract that
    the error propagates is preserved. fail-open: the diagnostic machinery is wrapped so it
    can NEVER turn a working retrieval into a broken one.

    ``cache_only=True`` (default False = byte-identical) sets the cache-only contextvar inside the
    worker so every guarded egress short-circuits: the raw=True drill path passes it through so a
    cache_only drill is genuinely zero-egress, exactly like a ranked cache_only search.
    """
    adapter, outcome, raised = _derive_outcome(source, query, limit, fresh, deadline_s, cache_only)
    if raised is not None:
        # Preserve the historical error contract: build the diagnostic, stash it on the exception,
        # and RE-RAISE the original exception. fetch_outcome swallowed the fault into state="errored";
        # this VIEW is the one that still raises exactly as before (same type, message, _eye_diag).
        diagnostic = _build_diagnostic(
            adapter, docs=[], captures=outcome.captures,
            timed_out=False, raised=raised, deadline_s=deadline_s)
        try:
            raised._eye_diagnostic = diagnostic  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        raise raised

    diagnostic = _build_diagnostic(adapter, docs=outcome.docs, captures=outcome.captures,
                                   timed_out=(outcome.state == "timed_out"), raised=None,
                                   deadline_s=deadline_s)
    return outcome.docs, diagnostic


def _build_diagnostic(adapter: "SourceAdapter", *, docs: list, captures: list,
                      timed_out: bool, raised: Optional[BaseException],
                      deadline_s: Optional[float]) -> Optional[dict]:
    """Assemble the /eye-fix diagnostic, or None for a clean success (docs + no captured failure).

    fail-open: any assembly error degrades to None (a diagnostic bug never breaks retrieval)."""
    try:
        if docs and not captures and not timed_out and raised is None:
            return None  # the no-noise success case: results came back, nothing failed → no diagnostic
        if docs:
            note = "fetch returned documents but an egress failure was captured (partial degrade)"
        else:
            note = "fetch returned nothing"
        if timed_out:
            note = (f"fetch exceeded the {deadline_s:.0f}s deadline (source hung or very slow); "
                    "captures could not be drained from the abandoned thread")
        elif raised is not None:
            note = f"adapter raised: {type(raised).__name__}: {raised}"[:300]
        elif not captures and not docs:
            note = ("empty with no captured egress failure: the source likely returned a "
                    "well-formed response with zero items (query miss, or a parser/selector "
                    "that silently matched nothing: open the adapter to confirm)")
        return {
            "adapter_path": _adapter_source_path(adapter),
            "returned": len(docs),
            "captures": captures,
            "note": note,
        }
    except Exception as exc:  # noqa: BLE001 (diagnostic assembly must never break the retrieval)
        logger.debug("_build_diagnostic failed: %s", exc)
        return None


def _adapter_source_path(adapter: "SourceAdapter") -> Optional[str]:
    """The adapter's defining source file, for the fixing agent to open. Best-effort; None on any
    failure (a config-row adapter built dynamically may have no clean file)."""
    try:
        import inspect
        return inspect.getfile(type(adapter))
    except Exception:  # noqa: BLE001
        return None


# Cross-lingual English routing aliases for the Chinese-described walled sources. Their value is
# exactly the depth web search lacks, but a Chinese-only description carries NO English token an
# English query (the default driving language) can overlap, so they never surfaced as
# excluded_relevant / in list_sources(query=). Each alias line is DERIVED FROM the source's own
# description (not invented), giving it an English surface WITHOUT rewriting the prose. facets.json
# ``keywords`` (if any) is merged on top, so either place can hold them.
_ROUTING_KEYWORDS: dict[str, str] = {
    "blind": "employment pass EP work visa nationality firm salary compensation hiring freeze referral offer Singapore Hong Kong anonymous insider tech",
    "maimai": "China big tech ByteDance Sea Shopee Singapore overseas staffing referral salary layoff hiring freeze insider workplace",
    "zhihu_search": "Chinese first-hand experience PhD jobhunt company insider visa city comparison question answer",
    "xiaohongshu_search": "study abroad PhD jobhunt city company experience notes interview guide lifestyle review",
    "xiaohongshu_cn": "study abroad experience notes lifestyle review comments little red book",
    "xiaohongshu": "study abroad experience notes lifestyle review little red book",
    "hardwarezone": "Singapore local forum EDMW employment pass foreign talent salary company immigration ground truth",
    "sogou_weixin": "WeChat official account article keyword search Chinese industry specialist first-hand writing",
    "gter": "study abroad application visa admission report Singapore Canada Hong Kong graduate",
    "nowcoder": "China AI ML interview experience bar referral algorithm engineer new grad recruiting",
    "yipin_search": "North America US CS PhD application admission outcome visa Chinese community",
    "quora": "English question answer immigration visa EP PR Express Entry PhD company culture city offer comparison",
    "linkedin_posts": "LinkedIn public hiring posts job opening hiring manager recruiting referral intent",
    "market_quote": "US stock quote ticker price market cap PE EPS dividend realtime company shares",
    "sec_financials": "SEC filing fundamentals revenue net income XBRL annual report company financial statements",
    "cninfo": "China A-share listed company filing disclosure annual report prospectus announcement regulator",
    "eastmoney": "China A-share Hong Kong US stock quote price market cap PE company shares",
    "dblp_author": "computer science researcher author name resolve publication record affiliation",
}


# The STRUCTURED-QUERY hint for a vertical source: what a NAMED call should put in ``query`` to hit
# the source's structured lookup on the FIRST try (a stock code, a ticker, an author's full name),
# instead of the agent guessing and re-firing. This is the eye's answer to a typed per-vertical param
# schema, in the eye's idiom: a short imperative string DERIVED FROM the source's own query contract
# (its description / explicit_only reason), never invented. Empty for a free-text source. Kept beside
# _ROUTING_KEYWORDS (the sibling vertical-routing aid) so the two maps live together; a source may
# instead declare a ``param_hint`` class attr (co-located, wins) or a facets.json ``param_hint``.
_PARAM_HINTS: dict[str, str] = {
    "eastmoney": "a stock name or code: 贵州茅台 / 600519.SH (A-share) / 00700.HK / AAPL (US)",
    "market_quote": "a US ticker: AAPL / MSFT / NVDA",
    "sec_financials": "a US ticker or company name: AAPL / Apple",
    "cninfo": "an A-share company name or code: 比亚迪 / 002594.SZ",
    "dblp_author": "a computer-science author's full name: Yoshua Bengio",
}


def _facet_keywords(name: str) -> str:
    """Cross-lingual English routing aliases for a source: the built-in _ROUTING_KEYWORDS map plus
    any facets.json ``keywords`` (a string or list). Chinese-described walled sources carry NO
    English token an English query can overlap, so they never surfaced for an English task despite
    being exactly the depth web search lacks; these aliases give them an English surface WITHOUT
    rewriting the description. Empty when none declared."""
    fb_kw = (_FACETS.get(name) or {}).get("keywords") or ""
    fb_str = " ".join(fb_kw) if isinstance(fb_kw, list) else str(fb_kw)
    return (_ROUTING_KEYWORDS.get(name, "") + " " + fb_str).strip()


def _param_hint(name: str, adapter: "SourceAdapter" = None) -> str:
    """The structured-query hint for a vertical source (see _PARAM_HINTS): what a NAMED call should
    put in ``query``. Precedence mirrors the facet merge (the adapter's own declaration wins): a
    ``param_hint`` class attr, then the central _PARAM_HINTS map, then facets.json. '' when the
    source takes free text."""
    if adapter is not None:
        own = getattr(adapter, "param_hint", "") or ""
        if own:
            return str(own)
    if name in _PARAM_HINTS:
        return _PARAM_HINTS[name]
    return str((_FACETS.get(name) or {}).get("param_hint") or "")


def param_hint(name: str) -> str:
    """Public by-name accessor for _param_hint: resolves the adapter from the registry so a caller
    (server.omniseek_search's routing_hint) needs only the source name. '' for an unknown / free-text
    source."""
    with _registry_lock:
        adapter = _adapters.get(name)
    return _param_hint(name, adapter)


def _adapter_match_surface(adapter: "SourceAdapter") -> tuple[set[str], set[str], str]:
    """An excluded source's routing surface for query-overlap: (domains, regions, description +
    cross-lingual keywords), merged the SAME way list_sources merges them (the adapter's own
    declaration wins, facets.json fills the rest). Lets the absence-explanation be query-AWARE
    without a second source of truth."""
    fb = _FACETS.get(adapter.name) or {}
    domains = set(getattr(adapter, "domains", None) or fb.get("domains") or [])
    regions = set(getattr(adapter, "regions", None) or fb.get("regions") or [])
    desc = (adapter.description or "") + " " + _facet_keywords(adapter.name)
    return domains, regions, desc.strip()


def _query_overlap_count(query: str, adapter: "SourceAdapter") -> int:
    """How many distinct query tokens appear in an excluded source's surface (domains ∪ regions ∪
    description ∪ cross-lingual keywords). Tokens are ASCII words + CJK BIGRAMS (via
    relevance.tokenize) so a CHINESE query matches a CHINESE source — the old ASCII-only split
    silently dropped every CJK char, so a 中文 query never surfaced any 中文 walled source. The COUNT
    (not just a bool) lets excluded_relevant rank the best-matched walled sources first instead of
    dumping the whole roster in registry order. Mechanical token overlap, no semantic ranking."""
    from omniseek.core import relevance  # local: leaf module, avoid any package-init cycle
    # Function words dropped, same as the live routing path in build_search_plan: the two
    # expressions of "routing overlap" must not drift apart (see _ROUTE_STOPWORDS).
    q_tokens = set(relevance.query_terms(query or "")) - _ROUTE_STOPWORDS
    if not q_tokens:
        return 0
    domains, regions, desc = _adapter_match_surface(adapter)
    surface = set(relevance.tokenize(" ".join([*domains, *regions, desc])))
    return len(q_tokens & surface)


def _query_overlaps_source(query: str, adapter: "SourceAdapter") -> bool:
    """True iff the query thematically matches an excluded source's facets/description (any token
    overlap). Thin bool over _query_overlap_count for callers that only need presence."""
    return _query_overlap_count(query, adapter) > 0


# Function words carry ZERO routing signal, and unlike the document ranker's BM25 (which scores
# within a candidate set and drowns them naturally) the routing hint scores against 220 short
# descriptions, where one shared "of" is enough to earn a slot. MEASURED 2026-07-25: the query
# 'chain of thought faithfulness evaluation' filled ALL SIX hint slots with sources matched purely on
# "of" (eastmoney 股票行情, mpnp_draws 曼省抽签, nih_reporter US biomedical grants).
# WHY A LIST AND NOT A FREQUENCY CUTOFF (the cutoff was tried first and provably cannot work): in this
# roster the two classes INTERLEAVE across the whole range, so no threshold separates them:
#     of(15) > singapore(14)   |   in(9) == pass(9)   |   from(5) == salary(5) == employment(5)
# Any cutoff that drops "of" also drops "singapore" and "canada"(17). Function words are instead a
# CLOSED linguistic class, identified by what they ARE, so this is a fixed vocabulary, not a tuned
# parameter. Scope is deliberately narrow: the ROUTING HINT only, never relevance.query_terms (the
# document ranker keeps every token, where phrase evidence still matters). English only: the CJK path
# tokenizes to bigrams and showed no such failure in the measured set, so nothing is invented for it.
_ROUTE_STOPWORDS = frozenset("""
about above after all also an and any are as at be been before being below between both but by can
did do does doing during each few for from further had has have having he her here hers him his how
if in into is it its me more most my no nor not of off on once only or other our out over own same
per she should so some such than that the their them then there these they this those through to
too under until up upon versus very via vs was we were what when where whether which while who whom
why will with within without you your
""".split())


def _route_idf(catalog: dict, q_tokens: set) -> dict:
    """How much is a match on each query token WORTH, given this roster's own vocabulary?

    WHY (measured 2026-07-25). excluded_relevant ranked candidates by RAW overlap count, which treats
    every matched token as equal evidence. It is not: route_tokens are tokenized from each source's
    domains + regions + DESCRIPTION, and descriptions share heavy boilerplate, so the roster's most
    common tokens are exactly the ones that mean nothing for routing:
        ai(76/220)  a(74)  papers(72)  keyless(52)  search(52)  ml(50)  the(47)  论文(47)  llm(44)
    The failure that exposed it: 'chain of thought faithfulness evaluation' filled ALL SIX hint slots
    with sources matched purely on the stopword "of" (eastmoney 股票行情, mpnp_draws 曼省抽签,
    nih_reporter US biomedical grants), crowding out anything real. Meanwhile a genuine hit like
    'canada express entry CEC cutoff' -> ircc_ee_rounds matched cec + entry + express, which the raw
    count could not distinguish from three boilerplate matches.

    So weight each token by RARITY IN THIS ROSTER, using the SAME BM25 idf relevance.field_scores
    already uses (one formula for both, so document ranking and source routing cannot drift apart).
    No new tunable: df comes from the catalog itself and re-derives whenever the roster changes.

    Computed over the query's tokens only (|q_tokens| x len(catalog) set lookups, sub-millisecond),
    and read ONLY from the catalog argument, so build_search_plan stays PURE as documented."""
    n = len(catalog) or 1
    out = {}
    for t in q_tokens:
        df = sum(1 for rec in catalog.values() if t in rec.route_tokens)
        out[t] = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
    return out


# -----------------------------------------------------------------------------
# Routing SearchPlan (Wave 2: the AUTHORITATIVE routing selection)
# -----------------------------------------------------------------------------
# A UNIFIED, PURE routing-selection over two materialized input layers. Wave 1 ran this in shadow to
# prove PARITY against the legacy search_many broad-branch selection; Wave 2 is the cutover: the plan
# is now the SINGLE choke point that search_many routes through (both the broad sweep and a named
# search), and the legacy in-line re-derivation plus the shadow drift-check are gone (a plan comparing
# to itself is meaningless once it IS the selection). The W1 REAL-registry parity golden keeps guarding
# the semantics: it independently recomputes the legacy selection from fetcher's own derivations and
# asserts the plan reproduces it, so it is now the frozen spec of record for this path.
# Three layers:
#   Layer 1 CatalogRecord / _catalog_snapshot: frozen per-source query-INDEPENDENT facts, MATERIALIZED
#           (cached), rebuilt per-entry under _registry_lock on a live register/unregister.
#   Layer 2 PolicySnapshot: the dynamic per-request overlay (enabled / retired / overlay / emergency /
#           watchdog-down), assembled FRESH each plan-build, never cached across requests.
#   Layer 3 build_search_plan: a PURE function reproducing the five broad-branch projections.
# The absolute discipline: every derivation is REUSED (backend_of / _derive_stability /
# _derive_access_tier / _adapter_match_surface / _profile_enabled / retired_reason /
# _explicit_only_overrides / _watchdog_down_set / _watchdog_health / relevance.tokenize), never
# reimplemented (a second source of truth is exactly what this refactor forbids).


@dataclass(frozen=True)
class CatalogRecord:
    """One source's query-INDEPENDENT, registry-state-dependent facts, materialized once (the
    _derive_* results are CACHED here, not re-derived in the plan). `static_explicit_only` is the RAW
    class value (getattr(adapter, 'explicit_only', False)), NOT the retire/overlay-resolved reason:
    the plan combines it with the policy overlay to mirror _explicit_only_reason's precedence exactly.
    `route_tokens` is the query-independent HALF of _query_overlap_count (its surface token set), so
    the plan needs only the query tokens to reproduce the overlap. `generation` bumps when THIS entry
    is live re-registered (the minimal mutable-family staleness signal); a static coded adapter keeps
    its build-time generation."""
    name: str
    registration_index: int
    backend: str
    stability: str
    access_tier: str
    kind: str
    domains: tuple
    regions: tuple
    modes: tuple
    static_explicit_only: object  # False | True | reason str: the raw getattr, NOT the overlay result
    route_tokens: frozenset
    generation: int


# Materialized Layer 1. Built once lazily (after import completes), then rebuilt PER-ENTRY on a live
# register/unregister via copy-on-write (a NEW dict swapped in atomically), so a concurrent reader
# iterating the snapshot never sees a mutating dict. Insertion order == all_adapter_names() by
# construction (registration order). None until first access.
_catalog_snapshot = None  # type: Optional[dict]
_catalog_generation = 0   # bumps on each live register/unregister; stamped onto rebuilt records


def _build_catalog_record(name: str, adapter: "SourceAdapter", index: int,
                          generation: int) -> CatalogRecord:
    """Materialize ONE source's query-independent facts by CALLING the existing derivations (never
    reimplementing them): backend_of / _derive_stability / _derive_access_tier for the classes; the
    same facet-merge list_sources uses (the adapter's own declaration wins, facets.json fills; regions
    normalized to a list) for kind/domains/regions/modes; and _adapter_match_surface + relevance.tokenize
    for the route_tokens surface _query_overlap_count consumes. `static_explicit_only` is the RAW attr."""
    from omniseek.core import relevance  # local: leaf module, avoid any package-init cycle
    fb = _FACETS.get(name) or {}

    def _facet_tuple(key: str) -> tuple:
        v = getattr(adapter, key, None) or fb.get(key)
        if key == "regions" and isinstance(v, str):  # org_watch stores a bare string; facets use a list
            v = [v]
        return tuple(v) if v else ()

    # route_tokens: the SAME surface _query_overlap_count builds, tokenized once (query-independent).
    domains_s, regions_s, desc = _adapter_match_surface(adapter)
    route_tokens = frozenset(relevance.tokenize(" ".join([*domains_s, *regions_s, desc])))
    return CatalogRecord(
        name=name,
        registration_index=index,
        backend=backend_of(name),
        stability=_derive_stability(adapter),
        access_tier=_derive_access_tier(adapter),
        kind=(getattr(adapter, "kind", None) or fb.get("kind") or ""),
        domains=_facet_tuple("domains"),
        regions=_facet_tuple("regions"),
        modes=_facet_tuple("modes"),
        static_explicit_only=getattr(adapter, "explicit_only", False),
        route_tokens=route_tokens,
        generation=generation,
    )


def _rebuild_catalog_snapshot() -> dict:
    """Full materialization under _registry_lock: iterate the registry in registration order and build
    one record per source. The lock makes the (name, adapter) pairs and the ORDER coherent (no
    half-built record, no dict-changed-size race), and the new dict's insertion order ==
    all_adapter_names() by construction. Swapped in atomically (copy-on-write)."""
    global _catalog_snapshot
    with _registry_lock:
        pairs = list(_adapters.items())  # ordered snapshot under the lock (== all_adapter_names order)
        snap = {}  # type: dict
        for i, (name, adapter) in enumerate(pairs):
            snap[name] = _build_catalog_record(name, adapter, i, _catalog_generation)
        _catalog_snapshot = snap
        return snap


def get_catalog_snapshot() -> dict:
    """The materialized Layer-1 catalog (built once lazily, then kept current per-entry on live
    changes). Returns the current immutable snapshot dict (do NOT mutate); iteration order ==
    all_adapter_names(). Callers hold the reference and iterate freely: a concurrent live change swaps
    in a NEW dict, it never mutates the one a caller is reading."""
    snap = _catalog_snapshot
    if snap is None:
        snap = _rebuild_catalog_snapshot()
    return snap


def _catalog_note_registered(name: str) -> None:
    """Live register hook (called UNDER _registry_lock from register_adapter_live): rebuild ONLY the
    affected entry into the materialized snapshot via copy-on-write (the name is NEW, so it appends,
    matching _adapters' order), bumping the generation so a mutable-family re-register is observable.
    No-op if the snapshot was never built (the lazy first access materializes the full set)."""
    global _catalog_snapshot, _catalog_generation
    snap = _catalog_snapshot
    if snap is None:
        return
    a = _adapters.get(name)
    if a is None:
        return
    _catalog_generation += 1
    new = dict(snap)  # copy-on-write: readers keep iterating the old snapshot
    new[name] = _build_catalog_record(name, a, len(new), _catalog_generation)
    _catalog_snapshot = new  # atomic reference swap (CPython): no torn read for a concurrent reader


def _catalog_note_unregistered(name: str) -> None:
    """Live unregister hook (called UNDER _registry_lock from unregister_adapter): drop the affected
    entry from the materialized snapshot via copy-on-write, bumping the generation. No-op if the
    snapshot was never built or the name is absent."""
    global _catalog_snapshot, _catalog_generation
    snap = _catalog_snapshot
    if snap is None or name not in snap:
        return
    _catalog_generation += 1
    new = dict(snap)
    new.pop(name, None)
    _catalog_snapshot = new


@dataclass(frozen=True)
class PolicySnapshot:
    """The dynamic per-request policy overlay, assembled FRESH at each plan-build from the live policy
    sources (never cached across requests). `enabled` is the profile predicate materialized to a name
    set (via _profile_enabled). `retired` / `overlay` / `emergency` are the three non-static tiers of
    _explicit_only_reason (the retire overlay via retired_reason; the RAW runtime overlay via
    _explicit_only_overrides; the _EXPLICIT_ONLY_SOURCES emergency dict), captured so the pure plan can
    reproduce that function's precedence exactly. `watchdog_down` is the fresh quarantine set."""
    enabled: frozenset
    retired: dict
    overlay: dict
    emergency: dict
    watchdog_down: frozenset
    watchdog_as_of: object


def _build_policy_snapshot(catalog: dict) -> PolicySnapshot:
    """Assemble Layer 2 fresh from the live policy sources, REUSING each derivation: _profile_enabled
    per source for `enabled`, retired_reason per source for `retired`, the cached runtime overlay +
    the emergency dict verbatim, and _watchdog_down_set / _watchdog_health for the quarantine set +
    as_of. Reads globals HERE (the impure assembly step); the plan it feeds stays pure."""
    enabled = set()  # type: set
    retired = {}  # type: dict
    for name in catalog:
        adapter = get_adapter(name)
        if adapter is None:  # a live unregister between catalog build and now: legacy skips it too
            continue
        if _profile_enabled(name, adapter):
            enabled.add(name)
        rr = retired_reason(adapter)
        if rr:
            retired[name] = rr
    _, _, as_of = _watchdog_health()
    return PolicySnapshot(
        enabled=frozenset(enabled),
        retired=retired,
        overlay=dict(_explicit_only_overrides()),
        emergency=dict(_EXPLICIT_ONLY_SOURCES),
        watchdog_down=frozenset(_watchdog_down_set()),
        watchdog_as_of=as_of,
    )


@dataclass(frozen=True)
class SearchPlan:
    """The five broad-branch projections, reproduced PURELY from a catalog + policy. Wave 2: this IS
    search_many's selection (the plan routes both branches). Projection contract (search_many's
    sources-is-None branch): broad_live == target_sources (registration order, minus watchdog-down);
    excluded == excluded; disabled == disabled; excluded_relevant == excluded_relevant (rank + cap +
    org_watch skip); skipped_down == skipped_down."""
    broad_live: list
    excluded: dict
    disabled: list
    excluded_relevant: list
    skipped_down: list


def _plan_excluded_reason(rec: CatalogRecord, policy: PolicySnapshot) -> str:
    """Mirror _explicit_only_reason EXACTLY from materialized inputs ('' = included): a reversible
    retire wins first (policy.retired), then the adapter's own static explicit_only
    (rec.static_explicit_only: True or a reason str), then any NON-retire runtime overlay entry
    (policy.overlay), then the emergency dict (policy.emergency). Same four-tier precedence, same
    True -> 'explicit-only' coercion as the legacy function."""
    rr = policy.retired.get(rec.name, "")
    if rr:
        return rr
    v = rec.static_explicit_only
    if v:
        return v if isinstance(v, str) else "explicit-only"
    ov = policy.overlay.get(rec.name)
    if ov:
        return ov
    return policy.emergency.get(rec.name, "")


def build_search_plan(catalog: dict, policy: PolicySnapshot, query: str,
                      sources: Optional[list] = None) -> SearchPlan:
    """PURE routing selection: deterministic given (catalog, policy, query, sources); no I/O, no
    registry/policy globals read (all state arrives via the two snapshots). Reproduces search_many's
    sources-is-None broad branch EXACTLY; for a NAMED search it mirrors the legacy no-selection path
    (target = list(sources); excluded/disabled empty; no quarantine)."""
    if sources is not None:
        return SearchPlan(broad_live=list(sources), excluded={}, disabled=[],
                          excluded_relevant=[], skipped_down=[])
    from omniseek.core import relevance  # local: leaf module (a pure tokenizer over the query arg)
    # Routing-hint tokens ONLY (broad_live below is query-independent): function words are dropped
    # because a shared "of" is not evidence that a source is relevant. See _ROUTE_STOPWORDS.
    q_tokens = set(relevance.query_terms(query or "")) - _ROUTE_STOPWORDS
    er_idf = _route_idf(catalog, q_tokens)
    broad_live = []  # type: list
    excluded = {}  # type: dict
    disabled = []  # type: list
    _er_scored = []  # type: list  # (overlap, hint) -> ranked + capped after the loop, exactly like legacy
    for name, rec in catalog.items():  # dict order == registration order (== all_adapter_names())
        if name not in policy.enabled:
            disabled.append(name)  # turned off by the deployment profile (a true off)
            continue
        reason = _plan_excluded_reason(rec, policy)
        if reason:
            excluded[name] = reason
            # Query-AWARE absence hint, SKIPPING org_watch lab feeds (their papers already reach broad
            # via arxiv/s2, so they flood any ML query) exactly as the legacy loop does.
            if not reason.startswith("org_watch"):
                hit = (q_tokens & rec.route_tokens) if q_tokens else set()
                if hit:
                    _er_scored.append((sum(er_idf[t] for t in hit), {
                        "name": name, "reason": reason,
                        "why": f"relevant but excluded; re-run naming it: sources=['{name}']",
                        "overlap": len(hit),
                        # The EVIDENCE for the claim, not just its size: which query tokens actually
                        # matched. A bare overlap=1 is unjudgeable (see _route_idf), so the agent gets
                        # the tokens and decides. matched=['cec'] is a hit; matched=['of'] is noise.
                        "matched": sorted(hit)}))
        else:
            broad_live.append(name)
    # Rank by RARITY-weighted score, not raw count, so one distinctive match outranks several
    # boilerplate ones. Ties break by name (determinism, as before).
    _er_scored.sort(key=lambda t: (-t[0], t[1]["name"]))
    excluded_relevant = [d for _, d in _er_scored[:6]]
    skipped_down = []  # type: list
    if policy.watchdog_down:
        skipped_down = sorted(s for s in broad_live if s in policy.watchdog_down)
        broad_live = [s for s in broad_live if s not in policy.watchdog_down]
    return SearchPlan(broad_live=broad_live, excluded=excluded, disabled=disabled,
                      excluded_relevant=excluded_relevant, skipped_down=skipped_down)


def search_many(
    query: str,
    sources: Optional[list[str]] = None,
    limit_per_source: int = 5,
    deadline_s: Optional[float] = None,
    fresh: bool = False,
    cache_only: bool = False,
    _catalog=None,
    _policy=None,
) -> tuple[dict[str, list[Document]], dict]:
    """Fan out across sources in parallel; return ``(results, _meta)``.

    Every source starts at once. Returns after ``deadline_s`` with whatever responded;
    slower sources are dropped from THIS result but keep running in the background and
    warm their cache. ``deadline_s=None`` uses a smart default (broad ~11s, or 16s when
    fresh=True; scoped ~45s); pass a number to override (a large value ≈ wait for all, bounded by each
    source's own internal timeout). ``sources=None`` searches all NON-explicit-only
    sources (browser/CDP + twitter_x excluded, being slow + account-rate-sensitive; counted in
    ``_meta.excluded_count``; name them to include). ``fresh=True`` bypasses cache.

    ``_meta`` explains the ABSENCES (presences are visible in the documents):
    ``{searched, elapsed_s, empty, timed_out, errored, excluded_count, excluded_relevant, truncated,
    progressive}``. ``excluded_count`` is just the SIZE of the deployment-static excluded set (the
    full name->reason MAP lives in omniseek_sources, one call away, never re-shipped per query).
    ``excluded_relevant`` is the query-AWARE slice the agent acts on: walled/slow sources whose
    facets/description thematically match THIS query, each with a copy-paste ``sources=[...]``
    re-run hint (empty when no excluded source matches). ``progressive`` = ``{fast, slow, timed_out}``:
    the fast/slow fan-out partition as COUNTS (non-actionable) plus the timed_out NAMES (actionable).
    A NAMED search (sources=[...]) also carries
    ``diagnostics`` when present: the same per-source /eye-fix evidence omniseek_fetch emits, for each
    named source that came back empty / errored / timed out (a broad sweep never arms it).
    """
    # A NAMED search (sources=[...] explicit) arms the per-source /eye-fix diagnostic; a broad
    # sweep (sources=None) deliberately does NOT, so the 64-worker fan-out stays zero-cost and
    # never cross-pollutes one source's trace with another's (see diag.py).
    _named = sources is not None
    # Wave 2 cutover: routing selection now flows through ONE choke point, the pure build_search_plan
    # over the materialized catalog + policy snapshots. It is AUTHORITATIVE for both branches: the
    # broad sweep gets the five projections (broad_live minus watchdog-down, excluded, disabled,
    # excluded_relevant, skipped_down); a named search gets the trivial no-selection projection
    # (broad_live == list(sources), everything else empty). The legacy in-line re-derivation and the
    # Wave-1 shadow drift-check are gone; the W1 REAL-registry parity golden guards this path.
    # S0.3 (one request snapshot): routing normally materializes its OWN catalog + policy here, but a
    # caller (search_ranked) that already built a snapshot for its recall scope threads that SAME pair
    # in via ``_catalog`` / ``_policy`` so routing and recall observe one consistent generation (no
    # register/retire/profile drift between the two former build points). Both default None -> build
    # internally, so EVERY other caller is byte-identical to before.
    catalog = get_catalog_snapshot() if _catalog is None else _catalog
    policy = _build_policy_snapshot(catalog) if _policy is None else _policy
    plan = build_search_plan(catalog, policy, query, sources=sources)
    target_sources = plan.broad_live
    excluded = plan.excluded
    disabled = plan.disabled
    excluded_relevant = plan.excluded_relevant
    skipped_down = plan.skipped_down  # broad-only: watchdog-down sources quarantined from this sweep
    if sources is None:
        # Fresh broad keeps the wider 16s budget (cold + contended, wants completeness);
        # the cache-allowed default uses 11s (measured: only reddit, dropped at 16s anyway).
        _broad_default = _SOURCE_DEADLINE_FRESH_S if fresh else _SOURCE_DEADLINE_S
        deadline = deadline_s if deadline_s is not None else _broad_default
    else:
        deadline = deadline_s if deadline_s is not None else _EXPLICIT_DEADLINE_S

    if not target_sources:
        return {}, {"searched": 0, "elapsed_s": 0.0, "empty": [], "timed_out": [],
                    "errored": {}, "excluded_count": len(excluded), "disabled": sorted(disabled),
                    "excluded_relevant": excluded_relevant, "truncated": [],
                    "skipped_down": skipped_down,
                    "progressive": {"fast": 0, "slow": 0, "timed_out": []}}

    # Progressive-return timing (#6): source -> completion monotonic time. Populated inside
    # _one() (closure capture, same pattern as fresh/cache_only/query). Concurrent writes go to
    # DISTINCT keys (one per source) which are atomic in CPython — no lock needed. Advisory only:
    # feeds the fast/slow/pending _meta facets after wait(), NEVER any control flow (wait() is
    # kept exactly as load-tested; the razor: this does not touch ranking).
    _result_times: dict[str, float] = {}

    def _one(source: str) -> tuple[list[Document], list]:
        from omniseek.core import cache, diag  # local import: avoid package-init cycle
        cache.set_fresh(fresh)  # set in the worker thread → adapter's cache calls honor it
        cache.set_cache_only(cache_only)  # cache-only (cache_only=True): egresses short-circuit
        adapter = get_adapter(source)
        if adapter is None:
            raise ValueError(f"unknown source: {source!r}")
        if _named:
            diag.enable()  # arm per-source capture in THIS worker thread (named search only)
        try:
            docs = _egress(adapter, query, limit_per_source)  # process-global egress bound
            if not cache_only:  # Path A: index enumerable docs (skip during a cache-only collect)
                from omniseek.core import recall
                recall.maybe_ingest(docs)
            _result_times[source] = time.monotonic()  # #6: stamp completion (advisory timing)
            return docs, (diag.drain() if _named else [])
        except Exception as exc:  # noqa: BLE001 — stash captures so the assembly can diagnose
            if _named:
                exc._eye_diag = diag.drain()  # type: ignore[attr-defined]
            raise
        finally:
            cache.set_fresh(False)  # don't leak `fresh` into a reused pool thread's next task
            cache.set_cache_only(False)  # nor `cache_only`

    results: dict[str, list[Document]] = {s: [] for s in target_sources}
    empty: list[str] = []
    timed_out: list[str] = []
    errored: dict[str, str] = {}
    truncated: list[str] = []
    diagnostics: dict[str, dict] = {}  # named-search only: per-source /eye-fix evidence

    workers = min(_SEARCH_WORKERS, len(target_sources))
    executor = ThreadPoolExecutor(max_workers=workers)
    t0 = time.monotonic()
    try:
        future_to_source = {executor.submit(_one, s): s for s in target_sources}
        done, not_done = wait(future_to_source, timeout=deadline)
        for fut, src in future_to_source.items():
            if fut in not_done:
                timed_out.append(src)
                if _named:  # the thread is abandoned, so captures can't be drained → timed-out note
                    d = _build_diagnostic(get_adapter(src), docs=[], captures=[],
                                          timed_out=True, raised=None, deadline_s=deadline)
                    if d:
                        diagnostics[src] = d
                continue
            caps: list = []
            raised_exc: Optional[BaseException] = None
            try:
                r, caps = fut.result()
            except Exception as exc:  # noqa: BLE001 — record it, don't kill the search
                errored[src] = f"{type(exc).__name__}: {exc}"[:80]
                r = []
                caps = list(getattr(exc, "_eye_diag", None) or [])
                raised_exc = exc
            results[src] = r
            if not r:
                empty.append(src)
            elif len(r) >= limit_per_source:
                truncated.append(src)  # returned == limit → likely more exists
            if _named:  # the same /eye-fix diagnostic omniseek_fetch emits (None on a clean success)
                d = _build_diagnostic(get_adapter(src), docs=r, captures=caps,
                                      timed_out=False, raised=raised_exc, deadline_s=deadline)
                if d:
                    diagnostics[src] = d
    finally:
        executor.shutdown(wait=False)  # stragglers finish detached + warm the cache

    # Progressive-return facets (#6, fail-open): partition the sources that responded within the
    # deadline into fast (< 3s) vs slow (>= 3s) by their stamped completion time. This is a
    # NON-ACTIONABLE diagnostic (which sources happened to be quick THIS run), so it carries only
    # COUNTS, not the 70+-name lists it used to: the agent does not act on the fast/slow NAMES for a
    # query, and the deployment-static / logs channels own those. The timed_out NAMES stay (the
    # agent may re-fire or cache_only-collect exactly those); they also remain a top-level key that
    # the curator yield tap reads. Advisory metadata only, NO control-flow / ranking impact (the razor).
    fast_count = 0
    slow_count = 0
    try:
        fast_count = sum(1 for _, t in _result_times.items() if t - t0 < 3.0)
        slow_count = sum(1 for _, t in _result_times.items() if t - t0 >= 3.0)
    except Exception:  # noqa: BLE001 — a timing-facet failure must never corrupt the search return
        fast_count, slow_count = 0, 0

    meta = {
        "searched": len(target_sources),
        "elapsed_s": round(time.monotonic() - t0, 1),
        "empty": sorted(empty),
        "timed_out": sorted(timed_out),
        "errored": errored,
        # The full ~100-entry excluded name->reason MAP is deployment-static (it does not change with
        # THIS query), so it is not per-query _meta: only its COUNT rides here, and omniseek_sources carries
        # every source's explicit_only reason so the full catalog stays one call away. The query-AWARE
        # slice the agent DOES act on is excluded_relevant (name + reason + overlap + re-run hint).
        "excluded_count": len(excluded),
        "disabled": sorted(disabled),
        "excluded_relevant": excluded_relevant,
        "truncated": sorted(truncated),
        # broad-only: sources the watchdog has fresh-flagged as down, soft-skipped this sweep (still
        # nameable via sources=[s]); empty for a named search and when the watchdog data is stale.
        "skipped_down": skipped_down,
        # fast/slow collapsed to counts; timed_out NAMES kept (actionable) inside the one block.
        "progressive": {"fast": fast_count, "slow": slow_count, "timed_out": sorted(timed_out)},
    }
    if diagnostics:  # named search only, and only when a named source was empty / errored / slow
        meta["diagnostics"] = diagnostics
    return results, meta


# =============================================================================================
# S4a: the ASYNC FAN-OUT (dormant behind a flag; NO adapter converted).
#
# asearch_many is the ASYNC TWIN of search_many: same signature, same _meta contract, same routing.
# It REUSES the pure sync helpers (build_search_plan / get_catalog_snapshot / _build_policy_snapshot
# / _build_diagnostic) UNCHANGED. The ONLY behavioral difference is the fan-out mechanism: one
# asyncio.Task per source (create_task COPIES the contextvars.Context, so each source's fresh /
# cache_only / diag is ISOLATED) whose leaf work runs the EXISTING sync .search on the ONE bounded
# shared thread pool (the S3a 256-token anyio limiter) via anyio.to_thread.run_sync, instead of
# search_many's per-search PRIVATE 64-thread ThreadPoolExecutor. That collapses the unbounded
# per-search amplification (searches x 64) into one bounded shared pool with natural backpressure:
# the structural win the charter's D3 permit / D4 reservation / D14 lanes machinery is DEFERRED until
# a MEASURED need appears against it.
#
# _ASYNC_FANOUT was a PLACEHOLDER flag: NO code branches on it (it is never read). The async twins
# asearch_many / asearch_ranked are reached by the offline goldens + the live shadow probe, and the
# REAL S4c-2 go/no-go flip is the omniseek_search TOOL-BODY change in server.py (omniseek_search is now an
# `async def` that dropped @_threaded and awaits asearch_ranked / asearch_many on the loop), NOT this
# flag. It is kept False so the S4a/S4c dormancy goldens that pin it stay green; do NOT wire it -- the
# tool-body flip supersedes it. search_many / search_ranked stay SYNC for the other callers (omniseek_gather
# / sensors / curator). Converting a source to native async (adapter.asearch) is S4b; no adapter has
# asearch here yet, so every source takes the legacy-runner branch of _dispatch_search.
# =============================================================================================
_ASYNC_FANOUT = False  # PLACEHOLDER, never read; superseded by the omniseek_search async tool-body flip (S4c-2, server.py).

# Strong refs to timed_out straggler tasks DETACHED past the deadline (mirror search_many's
# executor.shutdown(wait=False): a straggler keeps running + warms the cache, it is NOT cancelled).
# A module-level set keeps each task alive (else the loop could GC it: "Task was destroyed but it is
# pending"); _reap drops the ref AND consumes the outcome when the task finally settles.
_DETACHED: "set[asyncio.Task]" = set()


def _reap(t: "asyncio.Task") -> None:
    """Done-callback for a DETACHED straggler: drop the strong ref, then CONSUME the outcome so a
    straggler that RAISES after the deadline (a common late network failure via the legacy runner:
    HTTP 500 / connection reset) does NOT emit asyncio's 'Task exception was never retrieved' ERROR
    into the eye logs. The sync executor.shutdown(wait=False) path swallows it silently; the async
    twin must too."""
    _DETACHED.discard(t)              # drop the strong ref (else "Task was destroyed but pending")
    if not t.cancelled():
        t.exception()                 # mark the exception retrieved -> suppress the ERROR log


@runtime_checkable
class AsyncSearchCapable(Protocol):
    """A source adapter that has gone NATIVE async (S4b onward): it exposes an awaitable ``asearch``
    that egresses off the loop by itself, so _dispatch_search awaits it directly instead of pushing
    the sync .search onto the shared thread pool. In S4a NO adapter implements this, so every source
    takes the legacy-runner branch; the native branch is built + goldened now (a stub adapter) so the
    dispatch is proven both ways before any real adapter converts."""

    async def asearch(self, query: str, limit: int) -> list: ...


async def _dispatch_search(adapter: "SourceAdapter", query: str, limit: int) -> list:
    """D1 capability dispatch: prefer an adapter's NATIVE async asearch; else run its EXISTING sync
    .search on the bounded shared thread pool. anyio.to_thread.run_sync PROPAGATES the current
    contextvars.Context into the worker thread, so the sync adapter's cache.fresh() / cache_only() +
    diag reads observe THIS source's isolated context (golden pins that propagation). The pool is the
    S3a 256-token limiter, so shared-pool backpressure replaces the per-search private 64-thread pool."""
    if isinstance(adapter, AsyncSearchCapable):        # native async adapter (S4b onward)
        return await adapter.asearch(query, limit)
    # LEGACY RUNNER (D3, simplified): the existing sync .search on the ONE bounded shared pool,
    # its leaf network call gated by the process-global egress semaphore (loop-agnostic; the anyio
    # pool limiter is per-loop and omniseek_gather spins a fresh loop per call, so it alone cannot bound
    # cross-loop concurrency).
    return await _aegress(adapter, query, limit)


async def asearch_many(
    query: str,
    sources: Optional[list[str]] = None,
    limit_per_source: int = 5,
    deadline_s: Optional[float] = None,
    fresh: bool = False,
    cache_only: bool = False,
    _catalog=None,
    _policy=None,
) -> tuple[dict[str, list[Document]], dict]:
    """Async twin of search_many (S4a): SAME signature / routing / _meta contract; the ONLY
    behavioral difference is the fan-out mechanism (one asyncio.Task per source on the bounded shared
    pool, not a private 64-thread ThreadPoolExecutor). See the S4a region header. DORMANT: reached
    only by the offline goldens + the live shadow probe while _ASYNC_FANOUT is False."""
    _named = sources is not None
    # Routing: byte-identical to search_many (the SAME pure helpers, unchanged). A caller that already
    # built a snapshot threads it in via _catalog / _policy; otherwise build internally -- but OFF the
    # loop: _build_policy_snapshot reads the watchdog-state JSON off disk (a blocking syscall), which on
    # a native async caller (e.g. a cache_only asearch_ranked, which threads in no snapshot) would stall
    # the loop. get_catalog_snapshot is in-memory but rides the same hop for simplicity.
    if _catalog is not None and _policy is not None:
        catalog, policy = _catalog, _policy
    else:
        def _build_snap():
            _c = get_catalog_snapshot() if _catalog is None else _catalog
            return _c, (_build_policy_snapshot(_c) if _policy is None else _policy)
        catalog, policy = await anyio.to_thread.run_sync(_build_snap)
    plan = build_search_plan(catalog, policy, query, sources=sources)
    target_sources = plan.broad_live
    excluded = plan.excluded
    disabled = plan.disabled
    excluded_relevant = plan.excluded_relevant
    skipped_down = plan.skipped_down  # broad-only: watchdog-down sources quarantined from this sweep
    if sources is None:
        _broad_default = _SOURCE_DEADLINE_FRESH_S if fresh else _SOURCE_DEADLINE_S
        deadline = deadline_s if deadline_s is not None else _broad_default
    else:
        deadline = deadline_s if deadline_s is not None else _EXPLICIT_DEADLINE_S

    if not target_sources:
        # EMPTY-TARGET GUARD (mirror search_many:1043-1048): asyncio.wait(set()) RAISES ValueError
        # (unlike concurrent.futures.wait([])), so return the SAME clean early {} + _meta BEFORE any
        # task is built. Byte-identical to search_many's zero-source branch.
        return {}, {"searched": 0, "elapsed_s": 0.0, "empty": [], "timed_out": [],
                    "errored": {}, "excluded_count": len(excluded), "disabled": sorted(disabled),
                    "excluded_relevant": excluded_relevant, "truncated": [],
                    "skipped_down": skipped_down,
                    "progressive": {"fast": 0, "slow": 0, "timed_out": []}}

    # Progressive-return timing (#6): source -> completion monotonic time. Populated inside _aone in
    # THIS task's copied context; each task writes a DISTINCT key on the single loop thread (no lock).
    # Advisory only: feeds the fast/slow _meta counts after wait(), NEVER any control flow.
    _result_times: dict[str, float] = {}

    async def _aone(source: str) -> tuple[list[Document], list]:
        # Async + context-ISOLATED twin of _one. Runs in THIS task's COPIED context (create_task), so
        # the set_fresh / set_cache_only / diag.enable below touch only this source's context and never
        # cross-pollinate another source's (the S3b no-leak contract, now at the fan-out level).
        from omniseek.core import cache, diag  # local import: avoid package-init cycle
        cache.set_fresh(fresh)  # in THIS task's copied context -> adapter's cache calls honor it
        cache.set_cache_only(cache_only)  # cache-only (cache_only=True): egresses short-circuit
        adapter = get_adapter(source)
        if adapter is None:
            raise ValueError(f"unknown source: {source!r}")
        if _named:
            diag.enable()  # arm per-source capture in THIS task's context (named search only)
        try:
            docs = await _dispatch_search(adapter, query, limit_per_source)  # D1 dispatch
            if not cache_only:  # Path A: index enumerable docs (skip during a cache-only collect).
                # maybe_ingest durably appends observations before it enqueues the materializer wake.
                # Keep that durability boundary awaited, but move its file writes and fsync calls off
                # the event loop. The writer daemon still owns every SQLite, vector, and graph write.
                from omniseek.core import recall
                await anyio.to_thread.run_sync(recall.maybe_ingest, docs)
            _result_times[source] = time.monotonic()  # #6: stamp completion (advisory timing)
            return docs, (diag.drain() if _named else [])
        except Exception as exc:  # noqa: BLE001 -- stash captures so the assembly can diagnose
            if _named:
                exc._eye_diag = diag.drain()  # type: ignore[attr-defined]
            raise
        finally:
            cache.set_fresh(False)  # mirror _one; harmless here (this task's copied context dies after)
            cache.set_cache_only(False)

    results: dict[str, list[Document]] = {s: [] for s in target_sources}
    empty: list[str] = []
    timed_out: list[str] = []
    errored: dict[str, str] = {}
    truncated: list[str] = []
    diagnostics: dict[str, dict] = {}  # named-search only: per-source /eye-fix evidence

    # ORDERED source -> task mapping (upholds the SETTLED I2 invariant: results / errored /
    # diagnostics follow CATALOG order, NOT the hash-unordered asyncio.wait done/pending SETS). Insertion
    # order == target/catalog order; create_task COPIES the context per source (per-source isolation).
    t0 = time.monotonic()
    src_to_task: "dict[str, asyncio.Task]" = {
        s: asyncio.create_task(_aone(s)) for s in target_sources}
    # DEADLINE + PARTIAL-RETURN (mirror search_many's wait(timeout=deadline) -> done/not_done). Use
    # done / pending ONLY for the membership test below; ASSEMBLE by the ordered src_to_task walk.
    done, pending = await asyncio.wait(src_to_task.values(), timeout=deadline)
    for src, task in src_to_task.items():  # ORDERED walk (mirror search_many's future_to_source walk)
        if task in pending:
            timed_out.append(src)
            # DETACH SAFELY + REAP: do NOT cancel (mirror executor.shutdown(wait=False); the straggler
            # finishes detached + warms the cache). _reap drops the strong ref + consumes the outcome
            # so a straggler that RAISES past the deadline emits no "exception never retrieved" ERROR.
            _DETACHED.add(task)
            task.add_done_callback(_reap)
            if _named:  # the task is abandoned, so captures can't be drained -> timed-out note
                d = _build_diagnostic(get_adapter(src), docs=[], captures=[],
                                      timed_out=True, raised=None, deadline_s=deadline)
                if d:
                    diagnostics[src] = d
            continue
        caps: list = []
        raised_exc: Optional[BaseException] = None
        try:
            r, caps = task.result()
        except Exception as exc:  # noqa: BLE001 -- record it, don't kill the search
            errored[src] = f"{type(exc).__name__}: {exc}"[:80]
            r = []
            caps = list(getattr(exc, "_eye_diag", None) or [])
            raised_exc = exc
        results[src] = r
        if not r:
            empty.append(src)
        elif len(r) >= limit_per_source:
            truncated.append(src)  # returned == limit -> likely more exists
        if _named:  # the same /eye-fix diagnostic omniseek_fetch emits (None on a clean success)
            d = _build_diagnostic(get_adapter(src), docs=r, captures=caps,
                                  timed_out=False, raised=raised_exc, deadline_s=deadline)
            if d:
                diagnostics[src] = d

    # Progressive-return facets (#6, fail-open): same fast (< 3s) vs slow (>= 3s) partition as
    # search_many. The async model cannot double-count a timed_out straggler that stamps during the
    # count window (a pending task only resumes when the loop runs it, and we do not await here),
    # which is acceptable, arguably cleaner. Advisory metadata only, NO control-flow / ranking impact.
    fast_count = 0
    slow_count = 0
    try:
        fast_count = sum(1 for _, t in _result_times.items() if t - t0 < 3.0)
        slow_count = sum(1 for _, t in _result_times.items() if t - t0 >= 3.0)
    except Exception:  # noqa: BLE001 -- a timing-facet failure must never corrupt the search return
        fast_count, slow_count = 0, 0

    meta = {
        "searched": len(target_sources),
        "elapsed_s": round(time.monotonic() - t0, 1),
        "empty": sorted(empty),
        "timed_out": sorted(timed_out),
        "errored": errored,
        "excluded_count": len(excluded),
        "disabled": sorted(disabled),
        "excluded_relevant": excluded_relevant,
        "truncated": sorted(truncated),
        "skipped_down": skipped_down,
        "progressive": {"fast": fast_count, "slow": slow_count, "timed_out": sorted(timed_out)},
    }
    if diagnostics:  # named search only, and only when a named source was empty / errored / slow
        meta["diagnostics"] = diagnostics
    return results, meta


# S4a LIVE SHADOW PROBE (the production proof; the dormant fan-out's real result is never touched).
# Run ONCE, on the first tool call after deploy (server schedules it in the BACKGROUND, OFF the hot
# path), from a WORKER thread: run BOTH search_many AND asearch_many over the SAME scoped stable query
# and compare the source -> doc source_id SETS, logging parity. asearch_many runs via portal.submit
# (this executes on a NON-loop worker thread; 1 parent + 2 children, trivial scale). Fully fail-safe:
# it can NEVER affect a real result or break a tool call. This proves asearch_many produces the SAME
# sources + docs as search_many against REAL adapters on the REAL loop, before any flip (S4c). It is
# the ONLY live async-fan-out exposure.
#
# S4b: the source list now spans BOTH dispatch paths so the live parity covers each. federal_register
# is a DECLARATIVE row (DeclarativeAPIAdapter.asearch -> the NATIVE dispatch branch); openalex is a
# CODED adapter (no asearch -> the legacy runner on the shared pool). So the post-deploy "async fan-out
# shadow OK: ... parity=True" now proves a native-async declarative source AND a legacy-runner coded
# source both match sync. NOTE: hackernews (the spec's declarative pick) is a CODED adapter in this
# repo (api/hackernews_source.py; its 2-layer stories+comments merge is past the declarative boundary),
# so it would route to the legacy runner and cover NO native path; federal_register is the repo's real
# keyless/public/no-quota declarative row (context7, the only other, is monthly-quota explicit_only).
_ASYNC_FANOUT_SHADOW_DONE = False
_SHADOW_QUERY = "artificial intelligence"        # a fixed query BOTH probe sources answer with stable
                                                 # results. BOTH runs pass fresh=True so each EGRESSES
                                                 # independently: the async run genuinely exercises the NATIVE
                                                 # asearch network path (off-loop discipline) on a real
                                                 # endpoint, not a hit on the sync run's warm cache.
_SHADOW_SOURCES = ["federal_register", "hackernews"]  # native declarative (federal_register) + legacy coded
                                                      # (hackernews); both are FAST HTTP APIs returning real
                                                      # docs for the query within the deadline (openalex fresh
                                                      # can exceed 12s -> a 0-doc probe).
_SHADOW_DEADLINE_S = 12.0


def async_fanout_shadow_probe() -> None:
    """Run-once, fail-safe shadow parity probe (see the region note above). MUST be called from a
    NON-loop WORKER thread (portal.submit deadlocks on the loop thread). NEVER raises."""
    global _ASYNC_FANOUT_SHADOW_DONE
    if _ASYNC_FANOUT_SHADOW_DONE:
        return
    _ASYNC_FANOUT_SHADOW_DONE = True  # exactly-once ATTEMPT: a stuck probe cannot storm-retry
    try:
        from omniseek.core import portal
        # fresh=True on BOTH: each egresses independently, so the async side actually runs the NATIVE
        # asearch network path (not a hit on the cache the sync side just warmed). parity then compares
        # two independent live fetches (stable sources -> equal).
        sync_res, _ = search_many(_SHADOW_QUERY, sources=_SHADOW_SOURCES,
                                  limit_per_source=3, deadline_s=_SHADOW_DEADLINE_S, fresh=True)
        async_res, _ = portal.submit(  # asearch_many returns (results, meta); UNPACK like search_many above
            asearch_many(_SHADOW_QUERY, sources=_SHADOW_SOURCES,
                         limit_per_source=3, deadline_s=_SHADOW_DEADLINE_S, fresh=True),
            timeout=_SHADOW_DEADLINE_S + 10.0)

        def _idsets(res: dict) -> dict:
            return {s: {getattr(d, "source_id", None) for d in docs} for s, docs in res.items()}

        sync_ids = _idsets(sync_res)
        async_ids = _idsets(async_res)
        parity = sync_ids == async_ids
        n = len(async_res)
        m = sum(len(v) for v in async_res.values())
        if parity:
            logger.info("async fan-out shadow OK: %d sources, %d docs, parity=%s", n, m, parity)
        else:
            logger.warning("async fan-out shadow DIVERGENCE: parity=%s sync=%s async=%s",
                           parity, sync_ids, async_ids)

        # S4c-1: ALSO shadow the RANKED path (omniseek_search's DEFAULT), the most-used/most-complex twin.
        # search_ranked vs asearch_ranked over the SAME scoped stable query, both fresh=True (each
        # egresses independently, so the async side genuinely runs the off-loop recall + fan-out on the
        # real loop, not a hit on the sync run's warm cache). RANKED ORDER MATTERS: compare the ORDERED
        # source_id list (not a set) + _meta.deduped. record_yield=False so this synthetic probe never
        # pollutes the curator yield statistic. Own try so a ranked hiccup can't mask the fan-out result
        # above; still fully fail-safe + inside the run-once latch. This is the ONLY live proof the
        # ranked async path matches sync on real adapters/real loop before the flip (S4c-2).
        try:
            rsync_docs, rsync_meta = search_ranked(
                _SHADOW_QUERY, _SHADOW_SOURCES, deadline_s=_SHADOW_DEADLINE_S, fresh=True,
                record_yield=False)
            rasync_docs, rasync_meta = portal.submit(  # asearch_ranked -> (documents, _meta); UNPACK
                asearch_ranked(_SHADOW_QUERY, _SHADOW_SOURCES, deadline_s=_SHADOW_DEADLINE_S,
                               fresh=True, record_yield=False),
                timeout=_SHADOW_DEADLINE_S + 10.0)
            rsync_order = [getattr(d, "source_id", None) for d in rsync_docs]
            rasync_order = [getattr(d, "source_id", None) for d in rasync_docs]
            rparity = (rsync_order == rasync_order
                       and rsync_meta.get("deduped") == rasync_meta.get("deduped"))
            if rparity:
                logger.info("async ranked shadow OK: %d docs, parity=%s", len(rasync_order), rparity)
            else:
                logger.warning("async ranked shadow DIVERGENCE: parity=%s sync=%s async=%s "
                               "deduped_sync=%s deduped_async=%s", rparity, rsync_order, rasync_order,
                               rsync_meta.get("deduped"), rasync_meta.get("deduped"))
        except Exception as exc:  # noqa: BLE001 -- the ranked shadow must NEVER break a tool call
            logger.warning("async ranked shadow probe failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 -- the shadow probe must NEVER break a tool call
        logger.warning("async fan-out shadow probe failed: %s", exc)


def _compute_source_diversity(ranked: list[Document]) -> dict:
    """PERSPECTIVE distribution of ranked results. Mechanical tally by a FIXED perspective
    taxonomy (academic / social / audio / walled / news) mapped from each source's routing
    FACETS (domains + modes + access_tier, data-driven from the adapter attr / facets.json,
    self-maintaining as sources are added) — the agent judges what a one-sided distribution
    means; the eye only counts. NOTE the ``kind`` facet (stream/lookup/proxy/portal) is HOW a
    source behaves, NOT a perspective, so it is deliberately NOT used here. MULTI-LABEL: a source
    that spans types (e.g. a walled community forum) counts toward EACH perspective it satisfies,
    so ``distribution`` may sum to more than len(ranked). NOT a hardcoded name list. Advisory
    metadata: NEVER fed to ranking (the razor)."""
    perspectives = ('academic', 'social', 'audio', 'walled', 'news')
    dist: dict[str, int] = {}
    sources_seen: set[str] = set()
    for d in ranked:
        sources_seen.add(d.source)
        a = get_adapter(d.source)
        fj = _FACETS.get(d.source, {})
        domains = set((getattr(a, 'domains', None) if a else None) or fj.get('domains', []) or [])
        modes = set((getattr(a, 'modes', None) if a else None) or fj.get('modes', []) or [])
        try:
            tier = _derive_access_tier(a) if a else 'free'
        except Exception:
            tier = 'free'
        hits: set[str] = set()
        if ('TRANSCRIBE' in modes) or (domains & {'podcast', 'video'}):
            hits.add('audio')          # transcribable / audio-visual voice
        if tier in ('walled', 'circumvention'):
            hits.add('walled')         # login-walled depth
        if 'papers' in domains:
            hits.add('academic')       # scholarly literature
        if domains & {'social', 'community', 'insider'}:
            hits.add('social')         # community / gripe / social-platform voice
        if domains & {'news', 'media'}:
            hits.add('news')
        if not hits:
            hits.add('other')
        for h in hits:
            dist[h] = dist.get(h, 0) + 1
    absent = sorted(p for p in perspectives if p not in dist)
    return {'distribution': dist, 'absent_perspectives': absent,
            'unique_sources': len(sources_seen)}


def _doc_sid(d: Document) -> str:
    """The persisted source_id key for a doc — source_id, else url, else '' — matching the recall
    writer's ``sid`` derivation so a stamp lookup keys on the SAME (source, source_id) a doc was
    stored under."""
    return str(getattr(d, "source_id", None) or getattr(d, "url", None) or "")


def _seen_before_lookup(ranked: list[Document]) -> "Optional[dict[tuple[str, str], float]]":
    """The batched first_seen lookup half of the seen_before stamp: for each ranked doc's
    (source, source_id), the earliest persisted first_seen across the perception memory (docs
    UNION thin document nodes, graph_nodes kind='document'). A doc the deployment has NEVER
    retrieved is simply absent from the returned map (the stamp pass reads that as first-time-seen).

    TRI-STATE (Wave 3, 1.14): the return distinguishes "the lookup could not run" from "the lookup
    ran and found nothing":
      - ``None``  the lookup itself was UNAVAILABLE (recall disabled via ``store._disabled``, no read
                  connection, or an exception BEFORE the query). Memory is unavailable, so novelty is
                  UNKNOWN, not verified-new. The stamp pass turns this into ``seen_before=None``.
      - ``{}``    the lookup RAN and simply found no prior first_seen for any ranked doc.
      - a map     the first_seen values that were found.
    A per-chunk row error still fails open to the (possibly partial) map so far, never an exception
    into the caller: a query that STARTED is still an available lookup, just an incomplete one."""
    first_seen: dict[tuple[str, str], float] = {}
    try:
        from omniseek.core.recall import store
        if store._disabled:
            return None  # recall disabled: memory UNAVAILABLE, not an empty verified answer
        con = store._read_con()
        if con is None:
            return None  # no read connection: UNAVAILABLE
    except Exception as exc:  # noqa: BLE001
        logger.debug("seen_before store unavailable: %s", exc)
        return None  # the lookup could not even begin: UNAVAILABLE
    # (source, sid) for every ranked doc, deduped.
    keys: list[tuple[str, str]] = []
    seen_keys: set = set()
    for d in ranked:
        source = getattr(d, "source", None)
        if not source:
            continue
        sid = _doc_sid(d)
        if not sid:
            continue
        k = (source, sid)
        if k not in seen_keys:
            seen_keys.add(k)
            keys.append(k)
    if not keys:
        return first_seen
    _CHUNK = 400  # bound the SQL variable count per statement (row-value IN over pairs)
    from omniseek.core.recall.graph import doc_node_id
    for i in range(0, len(keys), _CHUNK):
        chunk = keys[i:i + _CHUNK]
        # (a) indexed docs (UNIQUE(source, source_id) → an index probe per pair).
        try:
            marks = ",".join(["(?,?)"] * len(chunk))
            params: list = [x for pair in chunk for x in pair]
            for source, sid, fs in con.execute(
                f"SELECT source, source_id, first_seen FROM docs "
                f"WHERE (source, source_id) IN (VALUES {marks})", params,
            ).fetchall():
                if fs is not None:
                    first_seen[(source, sid)] = float(fs)
        except Exception as exc:  # noqa: BLE001
            logger.debug("seen_before docs lookup failed: %s", exc)
        # (b) thin document nodes (graph_nodes id = doc:{source}:{sid}).
        try:
            id_to_key = {doc_node_id(s, sid): (s, sid) for (s, sid) in chunk}
            id_marks = ",".join("?" * len(id_to_key))
            for nid, fs in con.execute(
                f"SELECT id, first_seen FROM graph_nodes "
                f"WHERE kind = 'document' AND id IN ({id_marks})", list(id_to_key.keys()),
            ).fetchall():
                key = id_to_key.get(nid)
                if key is not None and fs is not None:
                    # Mutually exclusive with docs in practice; keep the EARLIEST if ever both.
                    prev = first_seen.get(key)
                    first_seen[key] = float(fs) if prev is None else min(prev, float(fs))
        except Exception as exc:  # noqa: BLE001
            logger.debug("seen_before thin lookup failed: %s", exc)
    return first_seen


def _stamp_seen_before(ranked: list[Document], t0_wall: float) -> None:
    """Stamp ``metadata['seen_before']`` (true|false|null) + ``first_seen_at`` (value|null) on EVERY
    ranked doc: whether THIS deployment had retrieved the doc BEFORE this search (the wall's novelty
    stamp).

    COMPLETENESS CONTRACT (P11 W2): every doc in a ranked response carries BOTH keys, NEVER absent.
    The value set (Wave 3, 1.14) is now three-valued:
      - ``seen_before=True``  + the ISO ``first_seen_at``: the deployment retrieved this doc before.
      - ``seen_before=False`` + ``first_seen_at=None``: the lookup RAN and this is a verified
        never-seen doc (first time, first_seen >= t0_wall = this search's own write, or a synthetic
        doc with no persisted row).
      - ``seen_before=None``  + ``first_seen_at=None``: the memory lookup was UNAVAILABLE (recall
        disabled / store down). JSON null = UNKNOWN, the honest degraded state. This never masquerades
        as verified-new: "memory unavailable" and "verified never-seen" are now distinguishable.
    The bug this fixes: a lookup FAILURE used to fail-open to an empty map, so every doc stamped
    ``seen_before=False`` (memory unavailable masquerading as verified-new); and earlier still, the
    stamp was presence-gated so a freshly live-fetched doc came back with no key at all.

    ``seen_before`` is True iff a first_seen exists AND is strictly earlier than ``t0_wall`` (this
    search's start), so a doc THIS search is the first to see is False (its first_seen >= t0_wall, or
    absent). The lookup is one BATCHED first_seen probe across docs UNION thin graph_nodes; when it
    returns ``None`` (unavailable) every doc gets the honest ``seen_before=None``, still stamped."""
    if not ranked:
        return
    from datetime import datetime, timezone
    first_seen = _seen_before_lookup(ranked)
    unavailable = first_seen is None  # None = the lookup could not run: novelty is UNKNOWN (null)
    for d in ranked:
        d.metadata = dict(d.metadata or {})
        if unavailable:
            # Memory unavailable: stamp the honest UNKNOWN (null), NEVER a False that would read as
            # "verified new". BOTH keys still present, so the completeness contract holds.
            d.metadata["seen_before"] = None
            d.metadata["first_seen_at"] = None
            continue
        fs = None
        source = getattr(d, "source", None)
        if source:
            fs = first_seen.get((source, _doc_sid(d)))
        # The contract: BOTH keys, on EVERY doc. Absent first_seen (or first_seen >= t0_wall = this
        # search's own write) is the honest never-seen-before state, not a missing stamp.
        if fs is not None and fs < t0_wall:
            d.metadata["seen_before"] = True
            try:
                d.metadata["first_seen_at"] = datetime.fromtimestamp(fs, timezone.utc).isoformat()
            except Exception:  # noqa: BLE001 (the boolean stamp still stands without the ISO string)
                d.metadata["first_seen_at"] = None
        else:
            d.metadata["seen_before"] = False
            d.metadata["first_seen_at"] = None


# ── SHARED ranked-path leaf helpers (S4c-1) ── search_ranked (sync) + asearch_ranked (async) call
# these EXACT helpers so the parity-critical steps (recall scope + arm building, the index fold, the
# remaining-budget formula, the seen_before completeness normalize, the passive enrichments + taps)
# cannot drift between the two twins. The ONLY thing the async twin re-implements is the CONCURRENCY
# orchestration (the recall arm runs off-loop via asyncio.create_task over anyio.to_thread instead of a
# private ThreadPoolExecutor; the seen_before DB read + the fold's as_of DB read hop off-loop via
# anyio.to_thread). A parity golden guards that the orchestration divergence stays behavior-identical.
def _build_recall_arm(query: str, sources: Optional[list[str]], limit: int,
                      semantic: Optional[bool], cache_only: bool):
    """Build the perception-index recall ARM (scope + the callable that runs it), SHARED by both twins.

    Returns ``(route_catalog, route_policy, recall_task, index_skipped)``:
      - ``route_catalog`` / ``route_policy``: the S0.3 one-request snapshot (the recall scope + the
        fan-out routing observe the SAME generation). Both None on a cache_only search, so the fan-out
        falls back to building its own (byte-identical there).
      - ``recall_task``: a zero-arg callable that runs the recall (semantic=False -> lexical only;
        None/True -> the hybrid), or None when no arm should run (cache_only, or an empty scope).
      - ``index_skipped``: the honest-absence stamp when the scope is empty (no arm ran), else None.

    The CALLER owns HOW the task runs: sync submits it to a private ThreadPoolExecutor; async kicks it
    off-loop via asyncio.create_task over anyio.to_thread. This helper does the small watchdog-state
    disk read (inside _build_policy_snapshot) + recall.indexable_set() (in-memory cached), so the async
    caller runs the WHOLE helper OFF the loop via anyio.to_thread."""
    if cache_only:
        # cache_only pickup: skip the recall arm entirely (no embed, no future). The fan-out builds its
        # own snapshot (byte-identical), so route_catalog/route_policy stay None -- exactly as before.
        return None, None, None, None
    from omniseek.core import recall
    _k = max(50, 4 * limit)
    # Recall SCOPE (Wave 2, audit 1.1): push the source scope INTO the arm (an unfiltered recall injects
    # off-scope index residue). Computed from the SAME catalog + policy the routing uses. broad =
    # indexable INTERSECT enabled MINUS retired; scoped = requested INTERSECT that same set.
    _cat = get_catalog_snapshot()
    _pol = _build_policy_snapshot(_cat)
    _allowed = (recall.indexable_set() & _pol.enabled) - set(_pol.retired.keys())
    _recall_scope = frozenset(_allowed) if sources is None else (frozenset(sources) & _allowed)
    if not _recall_scope:
        # EMPTY scope means "nothing is allowed", never "everything" (an empty frozenset reaching the
        # store would read as sources=None and UNFILTER the arm -- the 1.1 bypass). Fail CLOSED: SKIP the
        # arm (no embed, no future) + stamp the honest absence.
        index_skipped = {"lexical": 0, "vector": 0, "mode": "skipped",
                         "reason": ("no requested source is indexable" if sources is not None
                                    else "no indexable source is enabled")}
        return _cat, _pol, None, index_skipped

    def _recall_task():
        # semantic=False = the exact-token escape hatch (lexical only, byte-identical ranking);
        # None/True = the hybrid (lexical + vector RRF-fused -> cross-lingual + paraphrase recall).
        if semantic is False:
            _i = recall.search(query, k=_k, sources=_recall_scope)
            return _i, {"lexical": len(_i), "vector": 0, "mode": "lexical"}
        return recall.hybrid(query, k=_k, sources=_recall_scope)

    return _cat, _pol, _recall_task, None


def _recall_remaining_budget(deadline_s: Optional[float], fresh: bool,
                             sources: Optional[list[str]], t0_wall: float) -> float:
    """The remaining-budget the recall JOIN waits (mirrors the fan-out's deadline choice; wave 4 unifies
    this into one end-to-end deadline). SHARED so the sync future.result(timeout) + the async wait both
    use the IDENTICAL budget. Floored at 0.5s: recall is enrichment, never allowed to break OR stall."""
    _rank_budget = deadline_s if (deadline_s is not None and deadline_s > 0) else (
        (_SOURCE_DEADLINE_FRESH_S if fresh else _SOURCE_DEADLINE_S) if sources is None
        else _EXPLICIT_DEADLINE_S)
    return max(0.5, _rank_budget - (time.time() - t0_wall))


def _fold_recall_index(results: dict, meta: dict, idx: list, info: dict) -> None:
    """Fold the joined recall into the SAME merge_rank/dedup the live path uses: index docs collapse
    against live twins by fingerprint, rolled-off docs resurface. SHARED. No-op on an empty idx (a
    recall failure/timeout degrades to no-index: meta gets NO 'index' key, exactly as before). Contains
    the as_of DB read, so the async caller runs this OFF the loop via anyio.to_thread."""
    if not idx:
        return
    from omniseek.core import recall
    results["_index"] = idx
    meta["index"] = {**info, "candidates": len(idx), "as_of": recall.as_of()}


def _normalize_seen_before(ranked: list[Document]) -> None:
    """S0.1 (P11 completeness): a stamp that raised PART-WAY leaves some docs with both keys and some
    with neither. Normalize the SURVIVORS: any doc missing EITHER key gets the honest UNKNOWN
    (seen_before=None + first_seen_at=None), so EVERY ranked doc carries both keys even on a partial
    failure. The happy path is a no-op. SHARED, pure CPU (safe on the loop)."""
    for _d in ranked:
        _md = _d.metadata
        if _md is None or "seen_before" not in _md or "first_seen_at" not in _md:
            _md = dict(_md or {})
            _md["seen_before"] = None
            _md["first_seen_at"] = None
            _d.metadata = _md


def _coauthor_names(meta: dict) -> list:
    """The FULL author-name list from whatever key THIS scholarly source used -- arxiv ``all_authors``,
    acl/cvf ``authors``, s2 ``raw.authors``, an OpenAlex-style ``raw.authorships`` -- normalizing both
    list-of-strings and list-of-dicts ({name | display_name | author:{display_name}}). Returns [] if none.
    This is the ONE place the eye's many author-list shapes collapse to a single representation, so the
    placement can put "who wrote it" under ONE key for every source (dogfood friction #2)."""
    def _names(v) -> list:
        out: list = []
        for a in (v or []):
            if isinstance(a, str) and a.strip():
                out.append(a.strip())
            elif isinstance(a, dict):
                nm = a.get("name") or a.get("display_name") or (a.get("author") or {}).get("display_name")
                if isinstance(nm, str) and nm.strip():
                    out.append(nm.strip())
        return out
    for key in ("all_authors", "authors"):
        v = meta.get(key)
        if isinstance(v, list):
            names = _names(v)
            if names:
                return names
    raw = meta.get("raw")
    if isinstance(raw, dict):
        for key in ("authors", "authorships"):
            names = _names(raw.get(key))
            if names:
                return names
    return []


def _place_scholarly_fields(ranked: list[Document]) -> None:
    """Phase 0 STRUCTURAL PLACEMENT (pure-CPU, fail-open, idempotent): lift the scholarly identity /
    affiliation structure that is ALREADY in each doc's ``metadata['raw']`` (which ``to_tool_dict`` drops
    at projection, normalize.py:235) into named metadata keys that SURVIVE projection, and flatten
    cross-source ids so an S2 result welds to work nodes the SAME way an OpenAlex one does (``id_eq`` reads
    ``$.metadata.doi``). "New information arrives already placed" (PHILOSOPHY 0.1) made literal for the paper
    + its authors / institutions, cold store or warm, with ZERO store read and ZERO live call -- the
    per-search pointer for ids / OA / citations / authors / institutions was always redundant. Additive
    (never clobbers an existing key), so re-ranking a cached doc is a no-op. Design:
    docs/design/ambient-placement-and-selfwarm.md."""
    for d in ranked:
        try:
            meta = d.metadata
            if not isinstance(meta, dict):
                continue
            # cross-source id normalization: S2 buries doi/arxiv in external_ids -> flatten (weld + agent)
            ext = meta.get("external_ids")
            if isinstance(ext, dict):
                if not meta.get("doi") and ext.get("DOI"):
                    meta["doi"] = str(ext["DOI"])
                if not meta.get("arxiv_id") and ext.get("ArXiv"):
                    meta["arxiv_id"] = str(ext["ArXiv"])
            # authorships: lift exact author ids + institutions + the FULL coauthor list from the survivor's
            # OWN raw.authorships, ELSE the OpenAlex authorships CARRIED across a dedup merge (rank.dedup ①)
            # so an S2 / other-source survivor of a merged work still gets the OpenAlex identity layer.
            raw = meta.get("raw")
            _merged = meta.pop("_merged_authorships", None)   # private dedup-carry: consume it, never surface it
            if "author_ids" not in meta:
                ships = raw.get("authorships") if isinstance(raw, dict) else None
                if not (isinstance(ships, list) and ships) and isinstance(_merged, list):
                    ships = _merged            # fall back to the OpenAlex authorships carried across the merge
                if isinstance(ships, list) and ships:
                    aids, coauthors, insts, seen_inst = [], [], [], set()
                    for a in ships:
                        if not isinstance(a, dict):
                            continue
                        au = a.get("author") or {}
                        aid = au.get("id")
                        if aid:
                            aids.append(str(aid).rsplit("/", 1)[-1])  # bare A-id, drop the openalex.org/ prefix
                        nm = au.get("display_name")
                        if nm:
                            coauthors.append(nm)
                        for inst in (a.get("institutions") or []):
                            dn = inst.get("display_name") if isinstance(inst, dict) else None
                            if dn and dn not in seen_inst:
                                seen_inst.add(dn)
                                insts.append(dn)
                    if aids:
                        meta["author_ids"] = aids
                    if coauthors:
                        meta["coauthors"] = coauthors   # the FULL list (doc.author truncates to 5 + "et al.")
                    if insts:
                        meta["institutions"] = insts
            # UNIFY the FULL author list into ONE key across ALL scholarly sources: OpenAlex set `coauthors`
            # above; else pull from whatever key THIS source used (arxiv all_authors, acl/cvf authors, s2
            # raw.authors, ...) so "who wrote it" places the SAME way everywhere (dogfood friction #2). The
            # exact ids / institutions stay OpenAlex-only (only it carries them). Additive, never clobbers.
            if "coauthors" not in meta:
                _names = _coauthor_names(meta)
                if _names:
                    meta["coauthors"] = _names
        except Exception:  # noqa: BLE001 -- placement must never break the search
            continue


_JUDG_MAX = 4               # cap surfaced judgments per hit (hand-recorded statements are rare; 4 signals plenty)
_JUDG_NOTE_CHARS = 200      # truncate each judgment's note snippet (cartographer._CTX_* / the 72KB projection lesson)


def _compact_judgments(node_ids: list[str], stmt_index: dict) -> "tuple[list[dict], bool]":
    """The driver's OWN prior STATEMENTS touching any of ``node_ids`` (a hit's work: ids + its doc: id),
    compacted for the placement stamp: each ``{src, type, dst, note?}`` with self-describing labels
    (``_id_self_label`` for a ``{kind}:label:{x}`` id, else the raw id) and a note snippet capped at
    ``_JUDG_NOTE_CHARS``, deduped on the directed (src, dst, type) key, capped to ``_JUDG_MAX``. Returns
    ``(judgments, capped)``. These are tier J BY CONSTRUCTION (a statement is the agent's judgment); the
    caller keeps them in their OWN key, NEVER merged into the mechanical ``stored_edges`` counts -- that
    tier line is the one way this could violate the razor, so it is held structurally here."""
    if not stmt_index:
        return [], False
    from omniseek.core.recall.graph import _id_self_label
    seen: set = set()
    out: list[dict] = []
    capped = False
    for nid in node_ids:
        for e in stmt_index.get(nid, ()):
            key = (e.get("src"), e.get("dst"), e.get("type"))
            if None in key or key in seen:
                continue
            seen.add(key)
            if len(out) >= _JUDG_MAX:
                capped = True
                continue
            item: dict = {"src": _id_self_label(e["src"]) or e["src"],
                          "type": e.get("type"),
                          "dst": _id_self_label(e["dst"]) or e["dst"]}
            note = str(e.get("note") or "").strip()
            if note:
                item["note"] = (note[:_JUDG_NOTE_CHARS].rstrip() + "...") if len(note) > _JUDG_NOTE_CHARS else note
            out.append(item)
    return out, capped


def _norm_arxiv_id(s: str) -> str:
    """Strip a trailing arXiv version suffix (``2606.15621v1`` -> ``2606.15621``, ``cs/0701001v2`` ->
    ``cs/0701001``) so a version-suffixed HIT matches a version-less statement / edge key. Call ONLY on a
    KNOWN arxiv id (an arxiv source_id or the arxiv_id / external_ids.ArXiv field), never a generic string
    (the ``v\\d+$`` strip is safe only where the string is really an arxiv id). Dogfood 2026-07-15: a real
    statement on ``doc:arxiv:2606.15621`` was invisible on the ``2606.15621v1`` arxiv-source hit."""
    import re
    return re.sub(r"v\d+$", "", (s or "").strip())


def _place_graph_presence(ranked: list[Document]) -> None:
    """Phase 1 STRUCTURAL PLACEMENT (off-loop store read, fail-open): stamp each scholarly result with (a)
    what the graph's STORE-MEMORY holds about its WORK entity -- the count of stored M/A graph_edges by type
    -- AND (b) the driver's OWN prior tier-J STATEMENTS touching this entity (by its work: ids AND its doc:
    id). So both the accreted mechanical relations AND my own recorded judgments arrive ALREADY PLACED in the
    reflexive search, closing the write-side loop with no omniseek_graph verb invoked.

    RAZOR: surfacing my own stored statement is PLACING a recorded judgment, NOT the eye MAKING one -- the
    same operation ``neighborhood(policy=working)`` already performs over the same ``_statement_index`` (which
    ships razor-clean today); the only differences are the trigger (reflexive vs explicit verb) and the budget
    (a capped snippet vs max_nodes). The eye still never judges. The J judgments live in their OWN ``judgments``
    key, NEVER merged into the mechanical ``stored_edges`` counts (the tier line held structurally; a smoke
    golden asserts it). Statements load + fold ONCE per search (``load_statements`` is uncached), then O(1)
    per-hit lookups; J volume stays tiny BY CONSTRUCTION (hand-recorded durable judgments), watched by
    stats.statements.

    Runs on the seen_before hop's warm thread-local connection (store._read_con; off-loop for asearch via the
    same to_thread wrap). Work-node ids are derived PURE-CPU from the doc's ids (openalex_id / doi / arxiv_id,
    with the S2 external_ids fallback since Phase 0's flatten runs later); the doc: id is built the SAME way
    the seen_before stamp builds it (doc_node_id over the persisted source/sid). Stored-edge counts are
    STORE-MEMORY accreted from PAST tool runs, NOT live bibliometrics (labeled as such). An ``in_graph`` hit
    with NO recorded judgment stamps ``judgments: []`` -- the conspicuous blank is the write-reflex cue (the
    gap-ledger's write-side mirror). ABSENT entirely when the store holds nothing about the hit AND no
    statement touches it (routing_hint discipline: silent when nothing clears).
    Design: docs/design/ambient-placement-and-selfwarm.md."""
    try:
        from omniseek.core.recall import store
        from omniseek.core.recall import graph as _g
        if store._disabled:
            return
        con = store._read_con()
        if con is None:
            return
    except Exception:  # noqa: BLE001
        return
    from collections import Counter
    from omniseek.core.recall.graph import doc_node_id
    # The driver's OWN typed statements (tier J): loaded + folded ONCE per search (fail-open to empty), then
    # O(1) per-hit lookups. Empty under any read failure -> the judgments arm silently no-ops.
    stmt_index: dict = {}
    try:
        _stmts = _g.load_statements()
        if _stmts:
            stmt_index = _g._statement_index(_stmts)
    except Exception:  # noqa: BLE001 -- a statements read must never break placement
        stmt_index = {}
    doc_rows: list = []        # (doc, [work_ids], [statement-lookup ids])
    frontier: list[str] = []   # work: ids ONLY -> the stored-edges presence frontier (unchanged)
    seen_f: set = set()
    for d in ranked:
        meta = d.metadata if isinstance(d.metadata, dict) else None
        if not meta:
            continue
        oaid, doi, axid = meta.get("openalex_id"), meta.get("doi"), meta.get("arxiv_id")
        ext = meta.get("external_ids")   # S2 buries ids here; Phase 0's flatten runs after seen_before
        if isinstance(ext, dict):
            doi = doi or ext.get("DOI")
            axid = axid or ext.get("ArXiv")
        src = getattr(d, "source", None)
        sid = _doc_sid(d)
        # a clean (version-stripped) arxiv id: from arxiv_id / external_ids, ELSE an arxiv-source source_id
        # (the arxiv adapter puts the v-suffixed id in source_id + no clean arxiv_id -> the read-back missed
        # it; dogfood 2026-07-15). ``2606.15621v1`` must match a statement/edge keyed on ``2606.15621``.
        clean_ax = _norm_arxiv_id(axid) if axid else (_norm_arxiv_id(sid) if src == "arxiv" else "")
        wids: list[str] = []
        if oaid:
            wids.append(f"work:openalex:{oaid}")
        if doi:
            wids.append(f"work:doi:{str(doi).strip().lower()}")
        if clean_ax:
            wids.append(f"work:arxiv:{clean_ax}")
        # statement-lookup ids: the work: ids + the hit's OWN doc: id (built the SAME way _stamp_seen_before
        # does) + the version-NORMALIZED arxiv doc: id (a statement on doc:arxiv:2606.15621 must surface on a
        # 2606.15621v1 hit). The doc: namespace is where the real hand-recorded statements are keyed.
        stmt_ids: list[str] = list(wids)
        if src and sid:
            stmt_ids.append(doc_node_id(src, sid))
        if clean_ax and f"doc:arxiv:{clean_ax}" not in stmt_ids:
            stmt_ids.append(f"doc:arxiv:{clean_ax}")
        if not stmt_ids:
            continue
        doc_rows.append((d, wids, stmt_ids))
        for w in wids:
            if w not in seen_f:
                seen_f.add(w)
                frontier.append(w)
    if not doc_rows:
        return
    edges_by_node: dict = {}
    if frontier:
        try:
            edges = _g._stored_edges(con, frontier, None)
        except Exception:  # noqa: BLE001
            edges = []
        for e in (edges or []):
            for ep in (e.get("src"), e.get("dst")):
                if ep in seen_f:
                    edges_by_node.setdefault(ep, []).append(e)
    for d, wids, stmt_ids in doc_rows:
        counts: Counter = Counter()
        seen_e: set = set()
        for w in wids:
            for e in edges_by_node.get(w, ()):   # count each distinct edge once, even across a doc's id aliases
                ek = (e.get("src"), e.get("dst"), e.get("type"))
                if ek in seen_e:
                    continue
                seen_e.add(ek)
                counts[e.get("type") or "?"] += 1
        judgments, jcapped = _compact_judgments(stmt_ids, stmt_index)
        if not counts and not judgments:
            continue
        stamp: dict = {}
        if counts:
            stamp["in_graph"] = True
            stamp["stored_edges"] = dict(counts)
            stamp["note"] = ("accreted store-memory from past tool runs, not live bibliometrics; "
                             "refresh the live/deep version via the named drill (omniseek_graph / omniseek_coauthors / ...)")
        if judgments:
            stamp["judgments"] = judgments
            if jcapped:
                stamp["judgments_capped"] = True
            stamp["judgments_note"] = ("your OWN prior recorded statements on this entity (tier J, the "
                                       "driver's judgment, not mechanical facts and not live)")
        elif counts:
            stamp["judgments"] = []   # in_graph but UNJUDGED: the conspicuous blank = the write-reflex cue
        try:
            d.metadata["graph"] = stamp
        except Exception:  # noqa: BLE001
            pass


_WARM_PACE_MAX_S = 0.5   # self-warm ONLY when the shared OpenAlex rate gate is idle (< this) -> yields to real traffic
_WARM_CAP = 2            # at most this many self-warms per search (up to 1 enrich + 1 resolve; bounds the spend)
_SCHOLARLY_FOR_WARM = frozenset(
    {"openalex", "semantic_scholar", "crossref", "arxiv", "core", "openreview", "dblp"})


def _selfwarm_candidates(ranked: list[Document]) -> list:
    """Phase 2 SELF-WARM, the GATED decision half (pure, testable -- no loop, no network): which entities EARN
    a bounded live refresh this search, as ``(kind, arg)`` pairs -- ``("enrich", paper_id)`` for a re-touched
    PAPER's integrity/OA/pdf (highest reflexive value: check before citing) and ``("resolve", author_name)``
    for its author's identity. Returns [] when a SAFETY gate is closed (OpenAlex unavailable = budget dry /
    breaker open, OR the shared rate gate is NOT idle so a warm would contend with real traffic), or when NO
    scholarly result was RE-TOUCHED. The PRIMARY gate is structural: REVISITATION (``seen_before is True`` --
    the FREE signal the hop already stamped), so an entity earns a costly run through the agent's OWN repeated
    attention, not a tuned budget knob. Capped at ``_WARM_CAP`` total; the caller kicks ONLY enrich / resolve
    (never the heavy field_skeleton / coauthors). Design: ambient-placement-and-selfwarm.md."""
    try:
        from omniseek.core import _openalex as _oa
        if _oa.unavailable() or _oa._pace_backlog_s() > _WARM_PACE_MAX_S:
            return []
    except Exception:  # noqa: BLE001 -- gate read failed -> no warm (fail-open)
        return []
    out: list = []       # (kind, arg) pairs, integrity-first
    seen: set = set()
    for d in ranked:
        if len(out) >= _WARM_CAP:
            break
        if getattr(d, "source", None) not in _SCHOLARLY_FOR_WARM:
            continue
        meta = d.metadata if isinstance(d.metadata, dict) else None
        if not meta or meta.get("seen_before") is not True:   # REVISITATION gate (the structural primary)
            continue
        # (a) enrich the re-touched PAPER (integrity / OA / pdf, minted onto the work node)
        pid = meta.get("doi") or meta.get("arxiv_id")
        if not pid:
            ext = meta.get("external_ids")
            if isinstance(ext, dict):
                pid = ext.get("DOI") or ext.get("ArXiv")
        if pid:
            k = ("enrich", str(pid))
            if k not in seen and len(out) < _WARM_CAP:
                seen.add(k)
                out.append(k)
        # (b) resolve the re-touched author's identity (person node + same_as candidates)
        nm = (meta.get("coauthors") or [None])[0]
        if not nm:
            nm = (getattr(d, "author", "") or "").split(",")[0].split(" et al")[0].strip()
        nm = (nm or "").strip()
        if nm:
            k = ("resolve", nm)
            if k not in seen and len(out) < _WARM_CAP:
                seen.add(k)
                out.append(k)
    return out


def _selfwarm_revisited(ranked: list[Document]) -> None:
    """Phase 2 SELF-WARM, the KICK half (async-only): for each gated candidate, fire a FIRE-AND-FORGET
    generator (``resolve_identity`` for an author, ``enrich`` for a paper) DETACHED off the loop (create_task
    over to_thread + _reap, the SAME straggler pattern the recall arm uses), so the store's identity +
    integrity layers (person nodes + same_as via resolve; retracted / is_oa / pdf_url via enrich, minted by
    their taps) grow through the reflexive search itself while the search response stays UNTOUCHED. Only
    reached on asearch_ranked (a running loop); never awaited, never blocks, fail-open. The heavy generators
    (field_skeleton / coauthors) are NEVER auto-warmed -- they heal via the agent's own demand-driven drills."""
    cands = _selfwarm_candidates(ranked)
    if not cands:
        return
    try:
        import asyncio as _aio
        _aio.get_running_loop()                 # async path only; no running loop -> no warm (fail-open)
        from omniseek.core import relations
        from omniseek.core import enrich as _enrich
    except Exception:  # noqa: BLE001
        return
    for kind, arg in cands:
        try:
            if kind == "resolve":
                fut = anyio.to_thread.run_sync(relations.resolve_identity, arg)
            elif kind == "enrich":
                fut = anyio.to_thread.run_sync(_enrich.enrich, [arg])   # enrich takes a LIST of ids
            else:
                continue
            t = _aio.create_task(fut)
            _DETACHED.add(t)                    # strong ref: else the loop could GC the pending task
            t.add_done_callback(_reap)          # drop the ref + consume the outcome (no late-raise ERROR log)
        except Exception:  # noqa: BLE001 -- a warm kick must never break the search
            pass


def _apply_ranked_enrichments(query: str, ranked: list[Document], results: dict, meta: dict,
                              record_yield: bool, cache_only: bool) -> None:
    """The passive enrichments (#8 source_diversity, #11 conflicts), the P4 conflicts graph tap, and the
    curator P2 yield tap -- each computed AFTER merge_rank on the already-ranked+deduped list, each a
    MECHANICAL measurement stamped as _meta for the agent (NEITHER fed to ranking; the razor). SHARED,
    each wrapped fail-open so one signal's failure never corrupts `ranked` or the other _meta. Pure CPU
    + enqueue-only taps (the conflict tap + the yield tap are non-blocking put_nowait / WRITES_ENABLED-
    gated no-ops), so this is SAFE on the loop."""
    from omniseek.core import rank
    try:
        _place_scholarly_fields(ranked)   # Phase 0: pure-CPU structural placement (raw-lift + id normalization)
    except Exception:  # noqa: BLE001 — placement must never break the search
        pass
    try:
        meta["source_diversity"] = _compute_source_diversity(ranked)
    except Exception:  # noqa: BLE001 — an enrichment failure must never break the search
        pass
    try:
        # #11 conflicts: rank.dedup stamps same-group cross-source Signal divergence on each survivor at
        # MERGE time; collect the survivors' stamps here. Key ABSENT when no conflict → zero noise.
        _cf = [c for d in ranked for c in (d.metadata or {}).get("signal_conflicts", [])][:5]
        if _cf:
            meta["conflicts"] = _cf
    except Exception:  # noqa: BLE001 — an enrichment failure must never break the search
        pass
    try:
        # P4 conflicts graph tap (fail-open): mint the doc<->doc conflicts edges from dedup's PRIVATE
        # _conflict_pairs records (enqueue-only, NEVER blocking), then POP the private key so it never
        # reaches the agent (the STABILITY contract: the doc shape is byte-identical to pre-P4).
        _conf_pairs = [c for d in ranked for c in (d.metadata or {}).get("_conflict_pairs", [])]
        if _conf_pairs:
            rank._conflict_tap(_conf_pairs)
        for _d in ranked:
            if _d.metadata and "_conflict_pairs" in _d.metadata:
                _d.metadata.pop("_conflict_pairs", None)   # private: never agent-facing
    except Exception:  # noqa: BLE001 — the tap NEVER touches the search result
        pass
    # Curator P2 yield tap (fail-open): records each source's marginal contribution to this top-K.
    # Skipped for synthetic/in-process searches (record_yield=False) and cache-only pickups.
    if record_yield and not cache_only:
        try:
            from omniseek.core.curator import yield_tap
            yield_tap.record_search(query, ranked, results, meta)
        except BaseException as exc:  # noqa: BLE001 the tap NEVER touches search
            if isinstance(exc, asyncio.CancelledError): raise  # D11: never eat a cancellation
            pass


def search_ranked(
    query: str,
    sources: Optional[list[str]] = None,
    limit: int = 15,
    deadline_s: Optional[float] = None,
    fresh: bool = False,
    cache_only: bool = False,
    semantic: Optional[bool] = None,
    record_yield: bool = True,
) -> tuple[list[Document], dict]:
    """Search, then DEDUP + RANK into one list; return ``(documents, _meta)``.

    Collapses cross-source duplicates (same paper from arxiv+openalex+… → one entry,
    others in ``metadata.also_in``) and orders by a transparent relevance+recency+
    engagement blend (``metadata._rank``) — a convenience the caller may re-sort (each doc's
    named signals map + date are on the doc; use search_many for un-ranked buckets).
    Same routing / deadline / fresh semantics as search_many. ``_meta`` adds ``deduped``.
    """
    from omniseek.core import rank  # local import keeps the module graph acyclic

    # WALL-CLOCK at THIS search's start (search_many's own t0 is monotonic; the DB stores epoch
    # seconds). The seen_before stamp compares each doc's persisted first_seen against this instant.
    # Race-proof BY CONSTRUCTION: this search's own async ingest writes carry first_seen >= t0_wall
    # (they are stamped with time.time() only AFTER this line runs), so they can never flip the stamp
    # to "seen before" for a doc this very search is the first to retrieve.
    t0_wall = time.time()
    per_source = min(max(limit, 5), 15)
    # Kick the perception-index recall off CONCURRENTLY with the live fan-out. It depends ONLY on the
    # query (not the live results), and its query-embed serializes behind the shared _fwd_lock (ingest
    # + backfill), so running it AFTER the ~10s fan-out added that embed serially to every default
    # broad search. Overlapping hides it under the fan-out's wall-clock. Same razor: the index is pure
    # RECALL, merge_rank re-scores it identically to live docs; a cache_only pickup skips it entirely.
    # S0.3 (one request snapshot): the catalog + policy the arm built (for the recall scope) is also
    # threaded into search_many's routing so both observe the same generation (None on cache_only).
    _route_catalog, _route_policy, _recall_task, _index_skipped = _build_recall_arm(
        query, sources, limit, semantic, cache_only)
    _idx_ex = None
    _idx_future = None
    if _recall_task is not None:
        _idx_ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recall-hybrid")
        _idx_future = _idx_ex.submit(_recall_task)

    results, meta = search_many(query, sources, per_source, deadline_s=deadline_s, fresh=fresh,
                                cache_only=cache_only, _catalog=_route_catalog, _policy=_route_policy)
    total_in = sum(len(v) for v in results.values())  # LIVE bucket count (before index injection)
    # Join the overlapped recall and fold it into the SAME merge_rank/dedup the live path uses: index
    # docs collapse against live twins by fingerprint, rolled-off docs the feeds forgot resurface, and
    # the eye answers even network-down. Fail-open: a recall failure degrades to no index, never breaks.
    if _idx_future is not None:
        # Recall runs IN PARALLEL with the fan-out, so it is usually already done — but result() had NO
        # timeout, so a stuck embed / a held _fwd_lock / a vector-matrix rebuild could hang the WHOLE
        # search past its own deadline (fail-open catches exceptions, not a hang). Wait only for the
        # remaining budget (the SHARED formula the async twin also uses), then degrade to no-index —
        # recall is enrichment, never allowed to break OR stall the search.
        _remaining = _recall_remaining_budget(deadline_s, fresh, sources, t0_wall)
        try:
            idx, info = _idx_future.result(timeout=_remaining)
        except FuturesTimeoutError:
            logger.debug("recall arm exceeded remaining budget (%.1fs) -> no-index degrade", _remaining)
            idx, info = [], {"lexical": 0, "vector": 0, "mode": "timeout"}
        except Exception as exc:  # noqa: BLE001 — recall is an enrichment; its failure must not break search
            logger.debug("recall hybrid failed: %s", exc)
            idx, info = [], {"lexical": 0, "vector": 0, "mode": "error"}
        finally:
            _idx_ex.shutdown(wait=False)
        _fold_recall_index(results, meta, idx, info)
    elif _index_skipped is not None:
        # Scoped search whose requested sources are all non-indexable: no recall arm ran, so stamp the
        # honest absence (an explicit "skipped" mode + reason, never a silently missing index key).
        meta["index"] = _index_skipped
    ranked = rank.merge_rank(results, query, limit, backend_of=backend_of)
    meta["deduped"] = {"in": total_in, "out": len(ranked)}
    # ── seen_before stamp (the wall's novelty stamp) ── one BATCHED first_seen lookup across the
    # perception memory (docs UNION thin graph_nodes) marks which ranked results THIS deployment had
    # retrieved before this search. COMPLETENESS CONTRACT (P11 W2): EVERY ranked doc carries
    # seen_before + first_seen_at, never absent (the lookup is fail-open, so a lookup failure just
    # makes every doc read as never-seen, still stamped). The outer guard keeps a stamp failure from
    # ever breaking the search.
    try:
        _stamp_seen_before(ranked, t0_wall)
    except Exception:  # noqa: BLE001 — a stamp failure must never break the search
        pass
    try:
        _place_graph_presence(ranked)   # Phase 1: stamp graph store-memory (relations counts), same warm read
    except Exception:  # noqa: BLE001 — a placement failure must never break the search
        pass
    _normalize_seen_before(ranked)
    _apply_ranked_enrichments(query, ranked, results, meta, record_yield, cache_only)
    return ranked, meta


async def asearch_ranked(
    query: str,
    sources: Optional[list[str]] = None,
    limit: int = 15,
    deadline_s: Optional[float] = None,
    fresh: bool = False,
    cache_only: bool = False,
    semantic: Optional[bool] = None,
    record_yield: bool = True,
) -> tuple[list[Document], dict]:
    """Async twin of search_ranked (S4c-1): SAME (documents, _meta) contract; the ONLY difference is
    that the fan-out + the recall arm run as coroutines / off-loop hops instead of the sync executor.

    OFF-LOOP DISCIPLINE (asearch_ranked runs ON the event loop): every BLOCKING SYSCALL hops OFF the
    loop via ``anyio.to_thread.run_sync``; the async fan-out (``await asearch_many``) + the pure-CPU
    ``merge_rank`` + the enqueue-only enrichment taps stay ON the loop.
      - the recall ARM build (_build_recall_arm: the watchdog-state disk read + indexable_set) -> OFF.
      - the recall ARM run (recall.hybrid / recall.search: SQLite FTS + query-embed + vector matrix) ->
        OFF, kicked as an asyncio.Task over anyio.to_thread BEFORE the fan-out (the SAME overlap the
        sync twin gets from its private ThreadPoolExecutor), joined after with the SAME remaining-budget
        + the SAME fail-open degrade (timeout -> mode "timeout"; exception -> mode "error").
      - the recall FOLD (_fold_recall_index: recall.as_of SQLite read) -> OFF.
      - the seen_before stamp (_stamp_seen_before -> _seen_before_lookup: batched SQLite read) -> OFF.
    Shares the leaf helpers with search_ranked (_build_recall_arm / _recall_remaining_budget /
    _fold_recall_index / merge_rank / _stamp_seen_before / _normalize_seen_before /
    _apply_ranked_enrichments) so ranking / dedup / recall scope / seen_before / _meta cannot drift; a
    parity golden guards the ONE thing it re-implements (the recall-overlap concurrency orchestration).

    DORMANT (S4c-1): NOT wired into omniseek_search or any live path (that flip is S4c-2). Reached only by
    the offline goldens + the live ranked shadow probe while _ASYNC_FANOUT is False."""
    from omniseek.core import rank  # local import keeps the module graph acyclic

    # WALL-CLOCK at THIS search's start (see search_ranked): the seen_before stamp compares each doc's
    # persisted first_seen against this instant; this search's own ingest writes carry first_seen >=
    # t0_wall, so they can never flip a doc this very search is the first to retrieve to "seen before".
    t0_wall = time.time()
    per_source = min(max(limit, 5), 15)
    # Build the recall arm OFF the loop (the watchdog-state disk read inside _build_policy_snapshot +
    # indexable_set) via the SAME shared helper the sync twin uses. Returns the request snapshot (routed
    # into asearch_many so the scope + the fan-out observe one generation) + the arm callable + the
    # honest skip stamp. contextvars (fresh / cache_only / diag) propagate through anyio.to_thread.
    _route_catalog, _route_policy, _recall_task, _index_skipped = await anyio.to_thread.run_sync(
        _build_recall_arm, query, sources, limit, semantic, cache_only)
    # Kick the recall OFF-LOOP concurrently with the fan-out (create_task over to_thread), BEFORE the
    # await, so it overlaps under the fan-out's wall-clock exactly like the sync executor submit.
    _recall_fut = None
    if _recall_task is not None:
        _recall_fut = asyncio.create_task(anyio.to_thread.run_sync(_recall_task))

    results, meta = await asearch_many(query, sources, per_source, deadline_s=deadline_s, fresh=fresh,
                                       cache_only=cache_only, _catalog=_route_catalog,
                                       _policy=_route_policy)
    total_in = sum(len(v) for v in results.values())  # LIVE bucket count (before index injection)
    if _recall_fut is not None:
        # Join the overlapped recall with the SAME remaining-budget the sync twin waits. Mirror the sync
        # executor.shutdown(wait=False): on the budget elapsing, DETACH the task (do NOT cancel) so the
        # recall thread finishes + warms the index; _reap drops the ref + CONSUMES the outcome so a late
        # raise never emits an "exception never retrieved" ERROR. Same fail-open modes as sync (an empty
        # idx -> no meta["index"] key: the no-index degrade).
        _remaining = _recall_remaining_budget(deadline_s, fresh, sources, t0_wall)
        _done, _pending = await asyncio.wait({_recall_fut}, timeout=_remaining)
        if _recall_fut in _pending:
            logger.debug("recall arm exceeded remaining budget (%.1fs) -> no-index degrade", _remaining)
            _DETACHED.add(_recall_fut)
            _recall_fut.add_done_callback(_reap)
            idx, info = [], {"lexical": 0, "vector": 0, "mode": "timeout"}
        else:
            try:
                idx, info = _recall_fut.result()
            except asyncio.CancelledError:  # D11: never eat a cancellation
                raise
            except Exception as exc:  # noqa: BLE001 — recall is an enrichment; its failure must not break search
                logger.debug("recall hybrid failed: %s", exc)
                idx, info = [], {"lexical": 0, "vector": 0, "mode": "error"}
        # The fold does the as_of SQLite read -> OFF the loop (no-op on an empty idx).
        await anyio.to_thread.run_sync(_fold_recall_index, results, meta, idx, info)
    elif _index_skipped is not None:
        meta["index"] = _index_skipped  # pure, on-loop: the honest absence stamp (no arm ran)
    # merge_rank is PURE CPU -> stays ON the loop (fast; not to_thread'd by default).
    ranked = rank.merge_rank(results, query, limit, backend_of=backend_of)
    meta["deduped"] = {"in": total_in, "out": len(ranked)}
    # seen_before stamp: the batched first_seen lookup is a BLOCKING SQLite read -> OFF the loop. Same
    # fail-open outer guard (a stamp failure never breaks the search); the normalize below is pure CPU.
    try:
        await anyio.to_thread.run_sync(_stamp_seen_before, ranked, t0_wall)
    except Exception:  # noqa: BLE001 — a stamp failure must never break the search
        pass
    try:
        await anyio.to_thread.run_sync(_place_graph_presence, ranked)   # Phase 1: OFF-LOOP graph-presence stamp
    except Exception:  # noqa: BLE001 — a placement failure must never break the search
        pass
    _normalize_seen_before(ranked)
    # Passive enrichments + the conflict/yield taps: pure CPU + enqueue-only (non-blocking) -> ON loop.
    _apply_ranked_enrichments(query, ranked, results, meta, record_yield, cache_only)
    try:
        _selfwarm_revisited(ranked)   # Phase 2: revisitation-gated DETACHED identity self-warm (never awaited)
    except Exception:  # noqa: BLE001 — a warm kick must never break the search
        pass
    return ranked, meta


_DOWN_AFTER = 2  # consecutive watchdog failures before reporting "down" — matches the
                 # watchdog's own alert threshold, so a single transient blip stays "ok".


def _watchdog_health() -> tuple[dict, set, Optional[str]]:
    """From the P19 watchdog state: (consecutive-fail counts, tracked-source set, as_of).
    Advisory + ≤6h old. Keying off the CONSECUTIVE-fail count (not the last single run)
    means one bad run doesn't false-flag a healthy source as down."""
    try:
        data = json.loads(_WATCHDOG_STATE.read_text(encoding="utf-8"))
        return (data.get("fails", {}) or {}, set(data.get("last_status", {}) or {}),
                data.get("last_run"))
    except Exception:  # noqa: BLE001
        return {}, set(), None


_WATCHDOG_FRESH_S = 6 * 3600  # only quarantine on watchdog data no older than this


def _watchdog_down_set() -> set:
    """Source names the watchdog saw fail >= _DOWN_AFTER consecutive times, but ONLY when the
    watchdog run is fresh (<= 6h). The BROAD sweep soft-skips these so one hung/walled upstream cannot
    tax every broad search's deadline (wait() returns only when ALL finish OR the deadline elapses).
    Fail-open: an empty set on any read/parse error or stale data. NEVER consulted for a named search,
    so a down source stays fully nameable (sources=[s] still reaches it)."""
    fails, _tracked, as_of = _watchdog_health()
    if not as_of or not fails:
        return set()
    try:
        from datetime import datetime
        age = (datetime.now() - datetime.fromisoformat(str(as_of))).total_seconds()
        if age < 0 or age > _WATCHDOG_FRESH_S:
            return set()
    except Exception:  # noqa: BLE001
        return set()
    return {n for n, c in fails.items() if isinstance(c, int) and c >= _DOWN_AFTER}


def list_sources(check_health: bool = False, domain: Optional[str] = None,
                 query: Optional[str] = None, verbose: bool = False,
                 region: Optional[str] = None) -> list[dict]:
    """List all registered sources with routing-relevant facts.

    Each entry carries: name, the routing FACETS (kind / domains / regions / modes),
    needs_credentials, ``explicit_only`` (excluded from broad search, name it to include) plus
    ``explicit_only_reason`` (the WHY string, present only when excluded; the full catalog of
    exclusion reasons search's _meta.excluded_count no longer re-ships per query),
    an optional ``param_hint`` (the structured query a VERTICAL source wants — a stock code, a
    ticker, an author's full name — so a named call is filled right the first try; absent for a
    free-text source),
    a ``stability`` class (stable < keyed < scrape < walled — a neutral fragility FACT, NOT a
    verdict: set expectations + prioritise repairs), and recent ``health`` from the P19 watchdog
    (advisory — never blocks a source). Pass ``check_health=True`` for a fresh LIVE probe (slow).

    By default the per-source ``description`` prose is OMITTED: the facets are the routing
    signal and the full prose for every source is a large unconditional payload. Pass
    ``domain`` (keep only sources whose ``domains`` facet contains it; general sources lead,
    explicit_only trail), ``region`` (exact-match on the ``regions`` facet, e.g. 'sg'/'ca'/'cn'),
    or ``query`` (token-overlap over name + description + domains + regions + cross-lingual
    keywords, ranked best-first) to get the NARROWED set WITH descriptions; or ``verbose=True`` to
    force descriptions on the full list.
    """
    fails, tracked, as_of = _watchdog_health()
    with _registry_lock:  # snapshot the values under the lock; iterate the copy outside it
        adapters_snapshot = list(_adapters.values())
    live = _probe_all_health(adapters_snapshot) if check_health else {}
    out = []
    for adapter in adapters_snapshot:
        name = adapter.name
        if name not in tracked:
            health = "unknown"  # not probed by the watchdog (e.g. CDP sources)
        elif fails.get(name, 0) >= _DOWN_AFTER:
            health = "down"     # persistently failing across runs
        else:
            health = "ok"       # healthy, or just a single transient blip
        _eo_reason = _explicit_only_reason(adapter)
        entry = {
            "name": name,
            "description": adapter.description,
            "needs_credentials": adapter.needs_credentials,
            "explicit_only": bool(_eo_reason),
            # The explicit_only REASON string, present only when excluded. This is the catalog side of
            # the P11 _meta weight-class rule: search's per-query _meta ships only excluded_COUNT (the
            # ~100-entry name->reason map is deployment-static, not query info), so the full reason for
            # any excluded source stays exactly one omniseek_sources call away, never lost.
            **({"explicit_only_reason": _eo_reason} if _eo_reason else {}),
            # RETIREMENT as a first-class observable fact (the reversible curator retire overlay), so
            # consumers read a boolean instead of re-parsing a 'retired:' prefix off the reason. Its
            # one derivation is retired_reason(); a retire wins over a static explicit_only, so it
            # stays visible even on an already explicit_only (e.g. walled) source. The reason itself
            # rides explicit_only_reason above ("retired:<why> <date>").
            "retired": bool(retired_reason(adapter)),
            # NEUTRAL fragility class (stable < keyed < scrape < walled): a routing/expectations
            # signal + the curator's repair-priority fact, NOT a verdict. Derived centrally.
            "stability": _derive_stability(adapter),
            # Legal-facing tier (free / keyed / walled / circumvention) — see _derive_access_tier.
            "access_tier": _derive_access_tier(adapter),
            "health": health,
            "health_as_of": as_of,
        }
        # Structured-query hint (the vertical param an agent should put in ``query`` on a NAMED call),
        # present only when the source declares one (see _param_hint). The eye's idiom for a typed
        # per-vertical param schema: it teaches a one-shot-correct named call (e.g. eastmoney wants a
        # stock code), so a vertical source is not just discoverable but callable right the first try.
        _ph = _param_hint(name, adapter)
        if _ph:
            entry["param_hint"] = _ph
        # Optional routing facets (kind: stream/lookup/proxy/portal; domains;
        # regions; modes = the P0 acquisition-mode facet the Curator audit reads).
        # The adapter's own declaration wins, facets.json fills the rest.
        fb = _FACETS.get(name) or {}
        for facet in ("kind", "domains", "regions", "modes"):
            v = getattr(adapter, facet, None) or fb.get(facet)
            if v:
                # Normalize regions to a LIST: org_watch rows store a bare string ('cn') while
                # facets.json uses a list (['ca']); a stable shape lets region= filter + the agent
                # treat the facet uniformly (the audit found `region in regions` would behave
                # differently for the two shapes — substring-in-string vs membership-in-list).
                if facet == "regions" and isinstance(v, str):
                    v = [v]
                entry[facet] = v
        # Upstream backend — the de-duplicating key behind the HONEST count. The adapter's own
        # `backend` wins, facets.json fills, else the source IS its own backend. Collapses the
        # OpenAlex family (openalex + openalex_cn + researcher_watch + every org_watch slice =
        # one corpus + one API budget + one breaker) so the raw source count stops over-stating
        # coverage; an independent source keeps its own name as backend.
        entry["backend"] = backend_of(name)
        if check_health:
            healthy, msg = live.get(name, (None, "not probed"))
            entry["healthy"] = bool(healthy)
            entry["status"] = msg
        out.append(entry)

    # Narrowing. domain: closed-vocab exact match (the caller surfaces the vocabulary + a
    # did_you_mean on a near-miss, so a wrong token is not a silent dead end). query: TOKEN-OVERLAP
    # (ASCII words + CJK bigrams via relevance.tokenize), ranked best-first — NOT a contiguous
    # substring. The old substring test returned 0 for almost every multi-word query (incl. this
    # tool's own "singapore visa" example) and an English query could never substring-match a
    # Chinese description; token overlap (+ the facets.json cross-lingual keywords) fixes both.
    if domain:
        out = [e for e in out if domain in (e.get("domains") or [])]
        # General (broad-search-eligible) sources lead; explicit_only lab streams / walled sources
        # trail. domain='papers' is 65 entries, 42 of them org_watch OpenAlex slices — without this
        # the 6 general spine sources an agent wants for a keyword search are buried ~10:1.
        out.sort(key=lambda e: (e.get("explicit_only", False), e["name"]))
    if region:
        out = [e for e in out if region in (e.get("regions") or [])]
    if query:
        from omniseek.core import relevance
        q_tokens = set(relevance.query_terms(query))
        scored: list[tuple[int, dict]] = []
        for e in out:
            regions = e.get("regions") or []
            if isinstance(regions, str):  # org_watch stores a bare string; facets use a list
                regions = [regions]
            surface = set(relevance.tokenize(" ".join([
                e["name"], e["description"] or "",
                *(e.get("domains") or []), *regions, _facet_keywords(e["name"])])))
            n = len(q_tokens & surface)
            if n:
                scored.append((n, e))
        scored.sort(key=lambda t: (-t[0], t[1]["name"]))  # best overlap first, then name
        out = [e for _, e in scored]

    # Compact by default: the description prose is reachable on demand (a domain/query narrows the
    # set first, or verbose forces it on the full list): never truncated (a truncated prose head is
    # mangled), just omitted. The facets remain the routing signal.
    if not (domain or query or region or verbose):
        for e in out:
            e.pop("description", None)
    return out


def facet_vocabulary() -> dict:
    """The closed routing vocabularies (domain / region / kind → source count), from the live
    registry. Handed to the agent by omniseek_list_sources so domain= becomes a DISCOVERABLE router
    instead of a token the agent must already know (a wrong guess used to return a silent empty
    that read as 'the eye has nothing here'). Cheap: facet read only, no health probe."""
    from collections import Counter
    doms, regs, kinds = Counter(), Counter(), Counter()
    with _registry_lock:
        adapters = list(_adapters.values())
    for a in adapters:
        fb = _FACETS.get(a.name) or {}
        for d in (getattr(a, "domains", None) or fb.get("domains") or []):
            doms[d] += 1
        rs = getattr(a, "regions", None) or fb.get("regions") or []
        if isinstance(rs, str):
            rs = [rs]
        for r in rs:
            regs[r] += 1
        k = getattr(a, "kind", None) or fb.get("kind")
        if k:
            kinds[k] += 1
    return {"domains": dict(doms.most_common()),
            "regions": dict(regs.most_common()),
            "kinds": dict(kinds.most_common())}


def distinct_backend_count() -> int:
    """Distinct upstream backends — the HONEST coverage figure (the ~42-slice OpenAlex family
    collapses to 1). Cheap registry walk, no health probe; used in the connect-time instructions
    so the headline number matches what omniseek_list_sources reports."""
    with _registry_lock:
        adapters = list(_adapters.values())
    backends = set()
    for a in adapters:
        fb = _FACETS.get(a.name) or {}
        backends.add(backend_of(a.name))
    return len(backends)


def health_check() -> dict[str, dict]:
    """Run health check on every registered adapter — bounded + concurrent, so a
    source whose own health_check blocks (no internal timeout) can never stall this."""
    with _registry_lock:  # snapshot under the lock; the slow probe runs on the copy outside it
        adapters_snapshot = list(_adapters.values())
    live = _probe_all_health(adapters_snapshot)
    return {name: {"healthy": bool(h), "status": m} for name, (h, m) in live.items()}


def _adapter_failure_reason(name: str, captures: list) -> Optional[str]:
    """Turn one adapter's redacted diagnostic captures into a concise caller-facing reason."""
    for capture in reversed(captures):
        detail = capture.get("body") or capture.get("exc")
        if detail:
            return f"{name}: {detail}"
    return None


def _fetch_url_via_adapters_with_reason(url: str) -> tuple[Optional[Document], Optional[str]]:
    """Try every bounded adapter and retain an adapter-owned failure reason when it declines.

    The diagnostic capture is armed INSIDE each worker because ``_run_bounded`` deliberately uses a
    fresh daemon thread and contextvars do not cross that boundary. Only an adapter that emitted
    evidence for this URL contributes a reason; a plain ``None`` remains an ordinary non-claim.
    """
    with _registry_lock:  # snapshot under the lock; iterate (slow per-adapter fetch) on the copy
        adapters_snapshot = list(_adapters.values())
    adapter_reason: Optional[str] = None
    for adapter in adapters_snapshot:
        def _attempt(a=adapter) -> tuple[Optional[Document], list]:
            from omniseek.core import diag

            diag.enable()
            try:
                result = a.fetch_url(url)
            except BaseException as exc:  # noqa: BLE001 - preserve the adapter's historical error contract
                diag.note(f"{a.name}.fetch_url", url=url, exc=exc)
                exc._eye_url_diag = diag.drain()  # type: ignore[attr-defined]
                raise
            return result, diag.drain()

        try:
            budget = getattr(adapter, "fetch_timeout", _FETCH_URL_TIMEOUT_S)
            ok, payload = _run_bounded(_attempt, budget)
            if not ok:
                logger.warning("fetch_url: adapter %s exceeded %.0fs on %s — skipping",
                               adapter.name, budget, url)
                continue
            result, captures = payload
            adapter_reason = adapter_reason or _adapter_failure_reason(adapter.name, captures)
            if result is not None:
                return result, None
        except Exception as exc:  # noqa: BLE001
            adapter_reason = adapter_reason or _adapter_failure_reason(
                adapter.name, getattr(exc, "_eye_url_diag", [])
            )
            logger.debug("Adapter %s couldn't claim URL %s: %s", adapter.name, url, exc)
    return None, adapter_reason


def _fetch_url_via_adapters(url: str) -> Optional[Document]:
    """Historical adapter claim loop, retaining its document-only return contract."""
    return _fetch_url_via_adapters_with_reason(url)[0]


def fetch_url(url: str) -> Optional[Document]:
    """Try every adapter until one claims this URL, else a generic web read. None if unreachable.
    Thin wrapper over ``fetch_url_with_reason`` (drops the reason) so every existing caller is
    byte-identical."""
    return fetch_url_with_reason(url)[0]


def fetch_url_with_reason(url: str) -> "tuple[Optional[Document], Optional[str]]":
    """Like ``fetch_url`` but ALSO returns WHY a read failed: ``(doc, None)`` on success, else
    ``(None, reason)`` where an adapter-owned diagnostic wins over a generic web-fallback reason.
    This lets a caller act differently on a WALLED challenge (retry via CDP) vs a genuinely empty
    page. The generic web read (plain fetch, then a Jina headless render on a thin JS-wall / SPA shell)
    is the ONLY way the eye reaches a page outside its ~190 adapters, and runs ONLY after every adapter
    declines (zero cost to the happy path). Any failure degrades to ``(None, reason-or-None)``,
    preserving the historical matched=false contract."""
    doc, adapter_reason = _fetch_url_via_adapters_with_reason(url)
    if doc is not None:
        return doc, None
    # No adapter claimed it. LAST RESORT: a generic web read, diag armed to capture its failure reason.
    # Lazy import keeps bs4 off the hot adapter path.
    try:
        from omniseek.core import diag, web_fallback
        diag.enable()
        doc = web_fallback.read_via_fallback(url)
        notes = diag.drain()
        if doc is not None:
            return doc, None
        fallback_reason = notes[-1].get("body") if notes else None
        return None, adapter_reason or fallback_reason
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_url: web_fallback raised on %s: %s", url, exc)
        return None, None


# -----------------------------------------------------------------------------
# Internal
# -----------------------------------------------------------------------------


def _safe_health(adapter: SourceAdapter) -> tuple[bool, str]:
    try:
        return adapter.health_check()
    except Exception as exc:  # noqa: BLE001
        return False, f"health_check raised: {type(exc).__name__}: {exc}"


_HEALTH_TIMEOUT_S = 25   # per-source hard cap for a LIVE health probe — generous enough
                         # for thorough multi-feed RSS checks (slowest legit ~18s) yet
                         # still bounds a genuinely hung source (vs the old ∞ hang).
_HEALTH_WORKERS = 24     # health checks are I/O-bound — probe many at once


def health_check_bounded(adapter: SourceAdapter, timeout: float = _HEALTH_TIMEOUT_S) -> tuple[Optional[bool], str]:
    """``adapter.health_check()`` with a HARD timeout, run in a daemon thread.

    Returns ``(healthy, status)``; ``(None, 'timeout ...')`` if the check does not
    return within ``timeout``. This is the safety net that makes a source whose own
    ``health_check`` blocks (no internal timeout) UNABLE to stall its caller — the
    live ``list_sources`` probe, ``health_check()``, and the health-watchdog daemon
    all go through here. A still-blocked probe thread is a daemon → dies with the
    process; it never holds the caller."""
    box: dict = {}

    def _run() -> None:
        box["r"] = _safe_health(adapter)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, f"timeout (>{int(timeout)}s) — health_check did not return"
    return box.get("r", (False, "no result"))


def _probe_all_health(adapters: list, timeout: float = _HEALTH_TIMEOUT_S) -> dict[str, tuple]:
    """Probe EVERY adapter's health concurrently, each bounded by ``timeout``, with a
    global backstop so the whole call returns in ~``timeout`` even if several block.
    ``shutdown(wait=False)`` means a still-running probe can't hold us either."""
    results: dict[str, tuple] = {}
    ex = ThreadPoolExecutor(max_workers=min(_HEALTH_WORKERS, len(adapters) or 1))
    futs = {ex.submit(health_check_bounded, a, timeout): a.name for a in adapters}
    deadline = time.time() + timeout + 10
    for fut, name in futs.items():
        try:
            results[name] = fut.result(timeout=max(0.1, deadline - time.time()))
        except Exception:  # noqa: BLE001
            results[name] = (None, f"timeout (>{int(timeout)}s)")
    ex.shutdown(wait=False)
    return results
