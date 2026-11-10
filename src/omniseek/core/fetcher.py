"""Unified fetcher — the entry point for OmniSeek operations.

All source adapters register themselves with this module via
register_adapter(). The fetcher then routes queries to the appropriate
adapter and aggregates results.

The MCP server (omniseek.server) wraps these functions as tools.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable
from urllib.parse import urlparse

import anyio

from omniseek import redact as _redact
from omniseek.core import _guard, _probe, portal
from omniseek.core.normalize import Document
from omniseek import where

logger = logging.getLogger(__name__)

# asearch_many() fans out across sources in parallel, then returns after a per-source DEADLINE: a
# slow/hung source is dropped from THIS search but keeps running in the background, warming its
# cache for the next call. This bounds a broad search to ~the deadline, not the slowest source's tail.
#
# A source whose search can only block (a browser, a library with no async client: the walled / CDP
# residue) runs its one sync .search on a worker thread admitted by this ONE limiter on the service's
# one event loop. A dedicated limiter, neither anyio's default one nor portal.IO_LANE (tool bodies), so
# blocking sources take no other offload's tokens; a base source's blocking hook passes it too
# (sources._base.ablocking; ASYNC-CORE-DESIGN.md D14). Sized like portal.IO_LANE_TOKENS, generously
# above one broad search's ~90 sources, so it never throttles a SINGLE search; serve_http raises the
# soft fd limit far above this. (The process-global threading semaphore and the private 64-thread pool
# of the deleted sync fan-out, search_many, are gone with it: 11-04, S9.)
_EGRESS_CAP = 256
_LEGACY_LIMITER = anyio.CapacityLimiter(_EGRESS_CAP)


async def _aegress(adapter, query: str, limit: int) -> list:
    """Run one blocking source's .search on a worker thread admitted by _LEGACY_LIMITER, one fetch per
    cache key at a time (cache.flight_scope, inside the thread)."""
    from omniseek.core import cache  # local import: avoid package-init cycle

    def _run() -> list:
        with cache.flight_scope():
            return adapter.search(query, limit)

    return await anyio.to_thread.run_sync(_run, abandon_on_cancel=False, limiter=_LEGACY_LIMITER)


# BROAD (sources=None) cap. Two values, by freshness:
#  - default (cache-allowed) path: 11s. MEASURED (2026-06-13, after the L1 slow-source
#    parallelization + OmniSeek-prewarm cron + the S2 API key): a broad on a NEW query
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
# The paper sources of a broad sweep (domains facet "papers") are waited for as if the caller had
# named them (EYE-2, 2026-10-20). Measured: with the host machine at load 40 to 50 on 10 cores, the 11 s
# broad deadline dropped arxiv / dblp / core (or their gates ran out of time), cryptography title
# queries came back with no paper, and the workers' working remedy was to NAME the paper sources,
# which gets the named budget. So the broad sweep gives them that same budget, no new number:
# provisional, to be recalibrated from _meta.paper_wait (how long the late paper sources took).
# Under normal load they answer inside 11 s and nothing waits longer. A caller's own deadline_s wins.
_PAPER_DOMAIN = "papers"
_PAPER_DEADLINE_S = _EXPLICIT_DEADLINE_S


def _paper_sources(names) -> set:
    """The sources among ``names`` whose domains facet holds "papers" (the adapter's own attr wins,
    facets.json fills, as for every routing facet)."""
    out = set()
    for n in names:
        a = get_adapter(n)
        doms = (getattr(a, "domains", None) if a else None) or (_FACETS.get(n) or {}).get("domains") or []
        if _PAPER_DOMAIN in doms:
            out.add(n)
    return out


def _paper_wait_meta(t0: float, paper_deadline: float, late: list) -> dict:
    """_meta.paper_wait: the paper sources that were still out at the broad deadline, which of them
    answered within the paper budget, and when the wait ended (seconds from the fan-out's start)."""
    return {"deadline_s": paper_deadline, "ended_s": round(time.monotonic() - t0, 1),
            "arrived": sorted(n for n, ok in late if ok), "still_out": sorted(n for n, ok in late if not ok)}
# EXCLUDED from the broad (sources=None) fan-out: slow and/or account/credential/
# quota-sensitive sources. Each adapter DECLARES itself via an ``explicit_only``
# attribute (True, or better a short reason string, surfaced in the omniseek_sources roster's
# explicit_only_reason and in _meta.excluded_relevant when query-relevant);
# config-driven sources carry it on their row. This dict remains ONLY as an
# emergency override (name -> reason) for an adapter you cannot edit right now;
# it is normally EMPTY. The effective set changes only by a deliberate edit (the
# adapter's attribute, a config row, or the retire overlay below).
_EXPLICIT_ONLY_SOURCES: dict[str, str] = {}

# A REVERSIBLE runtime retire overlay (the curator one-tap omniseek_curator_retire_live): name -> a
# "retired:<reason> <date>" reason string. Lives OUTSIDE the deploy tree (in the state tree, which no
# backup carries; pristine tree), so a live retire takes effect with NO restart and NO git. CACHED in-process (the
# broad-search routing loop reads _explicit_only_reason once per source per query, a disk read
# there would be O(sources x queries)); the cache is invalidated on every write to the overlay
# (apply_live.invalidate_explicit_only_overrides), so a live retire/rollback shows up at once. The
# DURABLE half (the in-tree explicit_only edit) is staged for the operator.
_EXPLICIT_ONLY_OVERRIDES_PATH = (
    where.state() / "curator" / "explicit_only_overrides.json")
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


def sealed_reason(adapter: "SourceAdapter") -> str:
    """The seal reason for this adapter ('' = not sealed). The ONE place that answers 'is this source
    sealed': an adapter that its owner has switched off on purpose declares a ``sealed`` attribute
    (a reason string; the adapter's own flag decides it, so restoring is one edit in that adapter).
    A sealed source is out of the broad fan-out, never suggested as excluded_relevant, answers a
    named call with an empty result plus the reason, reads health "sealed" (not a failure) and is
    skipped by the health sweep and the session warmer. First user: xiaohongshu_cn (2026-10-07)."""
    try:
        v = getattr(adapter, "sealed", "")
    except Exception:  # noqa: BLE001 (a broken property must never break routing)
        return ""
    return v if isinstance(v, str) else ("sealed" if v else "")


def _explicit_only_reason(adapter: "SourceAdapter") -> str:
    """Why this adapter is excluded from the broad fan-out ('' = included).

    A seal wins first (the source is switched off on purpose, see sealed_reason); then a reversible
    retire (the strongest, operator-applied exclusion, always observable);
    then the adapter's own ``explicit_only`` attribute (True or a reason string); then any non-retire
    runtime overlay entry; then the emergency override dict.
    """
    sr = sealed_reason(adapter)
    if sr:
        return f"sealed: {sr}"
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
_WATCHDOG_STATE = where.state() / "health-watchdog-state.json"

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

    async def afetch_url(self, url: str) -> Optional[Document]:
        """Read a single URL from this source. Return None if the URL doesn't belong.

        The ONE URL reader of a source (async migration step six): there is no sync ``fetch_url`` to
        write. It runs on the service's event loop (``afetch_url_with_reason``); a blocking part (a
        library client, a CDP page, a helper still shared with a sync ``search``) goes to a worker
        thread inside it (``anyio.to_thread.run_sync``, ``limiter=_LEGACY_LIMITER`` for work that
        waits and parses, ``portal.IO_LANE`` for a pure wait). A thread reads a URL through the one
        sync entry, ``fetch_url_with_reason``."""
        ...

    async def ahealth_check(self) -> tuple[Optional[bool], str]:
        """The ONE health check of a source (async migration step seven): there is no sync
        ``health_check`` to call. It runs on the service's event loop (``ahealth_check_outcome``) and
        reads through the async helpers. A source whose check can only block (a CDP page, a library
        with no async client) writes a plain ``def health_check`` instead and inherits this method from
        ``sources._base.SourceBase`` makes it the check, run on a thread of the legacy lane, as the
        fan-out runs such a source's ``search``.

        Returns ``(healthy, status_message)``, where ``healthy`` has THREE values:

        - ``True``: verified working. This check asked the upstream and the answer shows it serves.
        - ``False``: verified broken (or unusable here: no credential, a missing dependency).
        - ``None``: NOT verified. This check sent the upstream no request whose answer could tell
          good from bad: the answer came from a cache or a stored state, a metered quota is too
          scarce to spend on a probe, or a declared gate / breaker / back-off held the probe back.
          The message says which. ``None`` is neither healthy nor failing, so a reader must never
          collapse it with ``bool()`` or a bare ``if ok:`` (``None`` is falsy and reads as a failure).

        Three cases are always ``None`` (2026-10-04, tests/test_health_guard_sweep.py holds every
        registered source to them):

        - the upstream answered HTTP 429, or an error the adapter knows as its rate limit (a
          dedicated exception, a body saying "rate limit"): it answered, but not whether the path
          serves data. The message says so (``_probe.rate_limited``), with Retry-After when given.
          The health funnel (``_asafe_health``) already re-reads a False that came only from a 429
          or a refused gate on the shared egress; an adapter that reads the status itself, or keeps
          its own client, must return None for it;
        - a circuit breaker / back-off of OmniSeek is open and the check therefore sends nothing
          (``_probe.breaker_open``, with the reopening time when known);
        - a source that reuses a login must still ask the upstream: reusing a session or token is
          not evidence. Probe the search path itself with the smallest request instead.
        """
        ...


# -----------------------------------------------------------------------------
# Registry
# -----------------------------------------------------------------------------

_adapters: dict[str, SourceAdapter] = {}
# Names registered MORE THAN ONCE this process. A collision means a config row and a
# coded adapter (or two rows) share a name and one silently replaced the other; this
# list must stay empty in a deployed eye.
_collisions: list[str] = []

# Guards STRUCTURAL mutation of _adapters (the live-apply / rollback path) and the snapshot
# reads that iterate it (all_adapter_names / list_sources / fetch_url_route / aprobe_health). The
# import-time register_adapter runs single-threaded at module import (before any worker thread
# exists), so it does NOT take the lock; only the LIVE path (register_adapter_live /
# unregister_adapter, run from a worker thread while broad searches iterate _adapters) does. An
# RLock so a future caller that already holds it can re-enter without deadlock. get_adapter stays
# LOCK-FREE (the 64-worker hot path): a plain dict.get is atomic, and a concurrent insert is either
# visible or not to a given get, both correct; the fan-out + fetch_one already tolerate None.
_registry_lock = threading.RLock()


class AdapterCollision(ValueError):
    """A live re-register hit a name already registered. The LIVE path ABORTS on this (never the
    import-path's log-and-replace) so a one-tap apply can never silently shadow a running source."""


def register_adapter(adapter: SourceAdapter) -> None:
    """Register a source adapter. Called by each source's module on IMPORT (single-threaded).

    Keeps the historical log-and-REPLACE + _collisions semantics (_collisions must be empty in a
    deployed eye; the import-time loaders de-dup base-wins so they never create a collision). The
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
    get_adapter(name) returns None; fetch_one / the fan-out / fetch_url all tolerate that."""
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
_FETCH_URL_CLASS_PRIORITY = {
    "fulltext": 0,
    "default": 10,
    "search-index": 20,
}


def _fetch_url_adapter_class(adapter: "SourceAdapter") -> str:
    """Read the adapter-declared fetch depth, defaulting unknown classes to the middle tier."""
    return getattr(adapter, "fetch_url_class", "default")


def _fetch_url_adapter_order(adapters: list["SourceAdapter"]) -> list["SourceAdapter"]:
    """Order URL readers by depth, with registration order as the stable tie-breaker.

    Full-text and walled adapters must get the first opportunity to read a URL. Search-index
    adapters are snapshots, so they run after a deep route has either produced content or
    honestly missed. The rule is class-based and applies to every adapter that declares a
    fetch_url_class, not to a named venue.
    """
    return [
        adapter
        for _, adapter in sorted(
            enumerate(adapters),
            key=lambda pair: (
                _FETCH_URL_CLASS_PRIORITY.get(_fetch_url_adapter_class(pair[1]), 10),
                pair[0],
            ),
        )
    ]


def _adapter_declares_fetch_url_host(adapter: "SourceAdapter", url: str) -> bool:
    """Whether an adapter explicitly owns the URL host for generic-fallback exclusion. A declared value
    with a path ("arxiv.org/pdf/") owns only the URLs on that host whose path starts with it (EYE-RSS-3:
    the arXiv adapter owns the full-text links, not every arxiv.org page)."""
    declared = getattr(adapter, "fetch_url_hosts", ()) or ()
    if isinstance(declared, str):
        declared = (declared,)
    parts = urlparse(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return False
    for value in declared:
        needle, _, path = str(value).lower().strip().partition("/")
        needle = needle.rstrip(".")
        if needle and (host == needle or host.endswith("." + needle)):
            if not path or (parts.path or "").lower().startswith("/" + path):
                return True
    return False


def _has_declared_fetch_url_owner(url: str, adapters: list["SourceAdapter"]) -> bool:
    """Whether any registered adapter has declared ownership of this URL host."""
    return any(_adapter_declares_fetch_url_host(adapter, url) for adapter in adapters)


async def _arun_bounded(coro, timeout: Optional[float]) -> tuple[bool, object]:
    """OmniSeek's one bounded wait: run ``coro`` as its OWN task (so it gets its own copy of the caller's
    context: a contextvar it sets never leaks back) and wait at most ``timeout`` seconds (None = no
    limit). Returns ``(True, result)`` or ``(False, None)`` past the timeout; re-raises the coroutine's
    exception. A task still running past the timeout, or when the caller itself is cancelled, is NOT
    cancelled: it is DETACHED (``_DETACHED`` + ``_reap``, as asearch_many does with a straggler) and
    finishes on its own, warming the cache (a blocking call it awaits on a worker thread could not be
    interrupted anyway). The URL read, the health probe (ahealth_check_outcome) and the curator tool's
    bounded steps (server._abounded) all wait through here."""
    task = asyncio.ensure_future(coro)
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout)
    finally:
        if not task.done():
            _DETACHED.add(task)
            task.add_done_callback(_reap)
    if not done:
        return False, None
    return True, task.result()


@dataclass(frozen=True)
class AdapterOutcome:
    """The first-class OUTCOME of one bounded source fetch (Wave 3): what HAPPENED, not just what
    came back. Makes the three conflations the old docs-only return hid impossible to miss: a
    timeout, an adapter error, and a full page (possibly truncated) are all distinct states here.

    Fields:
      ``docs``     the documents retrieved (``[]`` on timeout / error).
      ``state``    "completed" (the adapter returned), "timed_out" (exceeded ``deadline_s``; the
                   fetch task was detached and finishes on its own), or "errored" (the adapter raised;
                   NOT re-raised by ``afetch_outcome``).
      ``complete`` ``state == "completed"`` AND ``len(docs) < limit``: the enumeration FIT inside the
                   page, so nothing was truncated. A full page (``len(docs) == limit``) reads
                   ``complete=False`` (possibly more behind it).
      ``started``  the work began. With the current ``_arun_bounded`` the fetch task always
                   starts, so this is True on every real path (including a timeout: started but did
                   not finish). The field exists so a future runner that can DECLINE a task under
                   load can honestly report False.
      ``reason``   "" on success; a one-line timeout / exception summary otherwise (evidence text,
                   deliberately NOT the live exception object).
      ``captures`` the drained ``diag`` evidence, exactly what ``afetch_one_with_diag`` drains.
      ``held``     None, or ``{why, detail, n}`` when one of OmniSeek's gates refused a request of this
                   run or a host breaker was open (``_probe.summarize_held``; EYE-GATE-FIX D): the
                   run may have returned fewer documents than the source has.
    """
    docs: list
    state: str
    complete: bool
    started: bool
    reason: str
    captures: list
    held: Optional[dict] = None


async def _aderive_outcome(source: str, query: str, limit: int, fresh: bool,
                           deadline_s: Optional[float], cache_only: bool = False,
                           filters: Optional[list] = None, as_of=None,
                           ) -> "tuple[SourceAdapter, AdapterOutcome, Optional[BaseException]]":
    """The ONE derivation behind ``afetch_outcome`` / ``afetch_one`` / ``afetch_one_with_diag`` /
    ``afetch_one_as_of`` (Wave 3; async since step three of the async migration).

    Runs the adapter EXACTLY once, through the same capability dispatch the fan-out uses
    (``_dispatch_search``: a native ``asearch`` on the loop, a legacy ``search`` on a worker thread
    under ``_LEGACY_LIMITER``), under the shared ``diag`` capture and the ``_arun_bounded`` backstop,
    and returns ``(adapter, outcome, raised)``. ``raised`` is the adapter's exception when it raised (so
    a VIEW can re-raise it and keep the historical error contract byte-for-byte); it is None otherwise.

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
    from omniseek.core import filters as _flt
    # Typed filters: validated + translated BEFORE the run (a refusal raises FilterError, nothing is
    # sent); a date range wholly outside the source's coverage is answered empty with its reason.
    _fplan = _plan_filters(filters, [source], query, True, as_of)
    if _fplan is not None and source in _fplan.out_of_coverage:
        _why = _fplan.out_of_coverage[source]
        return adapter, AdapterOutcome(
            docs=[], state="completed", complete=True, started=False, reason="",
            captures=[{"helper": f"{source}.out_of_coverage", "body": _why}]), None
    _q, _up = (_fplan.run[source] if _fplan is not None else (query, None))
    _until = None if deadline_s is None else time.monotonic() + max(0.0, float(deadline_s))

    async def _work() -> tuple[list[Document], list, Optional[dict]]:
        # Runs as its own task (_arun_bounded), i.e. in a COPY of the caller's context: the caller's
        # initiator / refresh margin / probe ledger arrive, and what is set below stays in this task.
        cache.set_fresh(fresh)
        cache.set_cache_only(cache_only)  # cache-only (cache_only=True): egresses short-circuit at the funnels (mirrors asearch_many)
        _flt.set_upstream(_up)
        diag.enable()  # arm capture in THIS task's context
        try:
            with _guard.deadline_until(_until), _probe.capture_held() as _held:
                docs = await _dispatch_search(adapter, _q, limit)
            if not cache_only:  # Path A: index enumerable docs (skip on a cache-only collect, mirroring
                from omniseek.core import recall  # asearch_many; a cache_only read has NO side effects)
                await anyio.to_thread.run_sync(recall.maybe_ingest, docs, limiter=portal.IO_LANE)  # its file writes stay off the loop
            _stamp_license(docs, source)
            return docs, diag.drain(), _probe.summarize_held(_held)
        except BaseException as exc:  # the adapter raised: note it, attach the trace, RE-RAISE
            try:
                diag.note(f"{source}.search", exc=exc)  # so the contract is byte-identical to before
                exc._eye_diag = diag.drain()  # type: ignore[attr-defined] (read by the caller below)
            except Exception:  # noqa: BLE001 (capture must never mask the real error)
                pass
            raise

    docs: list[Document] = []
    captures: list = []
    held: Optional[dict] = None
    state = "completed"
    reason = ""
    raised: Optional[BaseException] = None
    try:
        ok, r = await _arun_bounded(_work(), deadline_s)
        if not ok:
            # The task is still running (detached; it finishes on its own and warms the cache); its
            # captures never drained. Report the timeout itself as the evidence and the historical [].
            logger.warning("fetch_one(%s) exceeded %.0fs deadline, returning []",
                           source, deadline_s)
            state = "timed_out"
            reason = f"exceeded the {deadline_s:.0f}s deadline (source hung or very slow)"
        else:
            docs, captures, held = r
    except Exception as exc:  # noqa: BLE001 (an adapter fault is an OUTCOME here, never a raise)
        state = "errored"
        raised = exc
        captures = getattr(exc, "_eye_diag", []) or []
        reason = f"{type(exc).__name__}: {exc}"[:300]
    complete = (state == "completed") and (len(docs) < limit)
    outcome = AdapterOutcome(docs=docs, state=state, complete=complete,
                             started=True, reason=reason, captures=captures, held=held)
    return adapter, outcome, raised


async def afetch_outcome(source: str, query: str, limit: int = 10, fresh: bool = False,
                         deadline_s: Optional[float] = _FETCH_ONE_DEADLINE_S,
                         cache_only: bool = False) -> AdapterOutcome:
    """Fetch from ONE named source and return the first-class :class:`AdapterOutcome` (Wave 3).

    The ONE derivation; ``afetch_one`` and ``afetch_one_with_diag`` are thin VIEWS over the same run.
    Unlike the views, this NEVER raises for an adapter fault: a timeout is ``state="timed_out"``, an
    adapter exception is ``state="errored"`` with a reason and captures, because its consumer is a
    sweep loop (``recall.ingest_loop``) that must never die on one bad source. An UNKNOWN source
    still raises ``ValueError`` (a caller bug, not an outcome). Same bounded / ``fresh`` semantics as
    ``afetch_one``. ``cache_only=True`` (default False) runs the fetch with the cache-only contextvar
    set in its task, so every guarded egress short-circuits (zero live work).
    """
    _adapter, outcome, _raised = await _aderive_outcome(source, query, limit, fresh, deadline_s,
                                                        cache_only)
    return outcome


async def afetch_one(source: str, query: str, limit: int = 10, fresh: bool = False,
                     deadline_s: Optional[float] = _FETCH_ONE_DEADLINE_S,
                     cache_only: bool = False) -> list[Document]:
    """Fetch from ONE named source, BOUNDED by ``deadline_s`` (default 90s).

    Past the deadline the caller gets ``[]`` and the fetch finishes detached (it never stalls the
    caller). Pass ``deadline_s=None`` to opt OUT (deliberate unbounded: a slow source you truly want
    complete, e.g. the cache warmer). ``fresh=True`` bypasses the cache for live data.

    Returns the documents only (the historical contract); ``afetch_one_with_diag`` is the
    sibling that also returns the failure-evidence trace for the /eye-fix repair loop.
    ``cache_only=True`` (default False) collects only warm cache (zero live egress).
    """
    return (await afetch_one_with_diag(source, query, limit, fresh=fresh, deadline_s=deadline_s,
                                       cache_only=cache_only))[0]


async def afetch_one_with_diag(source: str, query: str, limit: int = 10, fresh: bool = False,
                               deadline_s: Optional[float] = _FETCH_ONE_DEADLINE_S,
                               cache_only: bool = False, filters: Optional[list] = None,
                               ) -> tuple[list[Document], Optional[dict]]:
    """``afetch_one`` plus a diagnostic for a FAILED/EMPTY fetch (the /eye-fix evidence tap).

    Arms the opt-in ``diag`` capture for THIS one run only (the broad fan-out never arms it, so it
    stays zero-cost + zero cross-source pollution there), runs the adapter exactly as ``afetch_one``
    does (SAME deadline + SAME error contract, an adapter exception still PROPAGATES), then drains the
    captured egress failures into a diagnostic.

    Returns ``(docs, diagnostic)``. ``diagnostic`` is ``None`` when the fetch returned docs
    without a captured failure (the no-noise success case); otherwise a dict::

        {"adapter_path": <the adapter's source file, for the fixing agent to open>,
         "returned": <doc count>,
         "captures": [ {helper, url?, status?, body?, exc?}, ... ],
         "note": <one human-readable line>}

    On a propagating adapter error the captured egress failures are stashed on the exception
    (``exc._eye_diag``, and the assembled diagnostic as ``exc._eye_diagnostic``) so the drill can
    still surface them; the historical contract that the error propagates is preserved. fail-open:
    the diagnostic machinery is wrapped so it can NEVER turn a working retrieval into a broken one.

    ``cache_only=True`` (default False) sets the cache-only contextvar in the fetch's task so every
    guarded egress short-circuits: the raw=True drill path passes it through so a cache_only drill is
    genuinely zero-egress, exactly like a ranked cache_only search.

    ``filters`` (parsed by omniseek.core.filters.parse): validated against the source's spec before the
    run (FilterError, nothing sent) and translated by the source; out of coverage -> [] + diagnostic.
    """
    return await _afetch_one_core(source, query, limit, fresh, deadline_s, cache_only, filters, None)


async def afetch_one_as_of(source: str, query: str, limit: int = 10, *, as_of, fresh: bool = False,
                           deadline_s: Optional[float] = _FETCH_ONE_DEADLINE_S,
                           cache_only: bool = False, filters: Optional[list] = None,
                           ) -> tuple[list[Document], Optional[dict], dict]:
    """``afetch_one_with_diag`` replayed at ``as_of`` (a filters.AsOf): the bound is pushed down when the
    source declares ``as_of_field``, a source whose coverage starts after as_of sends nothing, and the
    documents pass the exit cut (filters.as_of_cut). Returns ``(docs, diagnostic, as_of_meta)``; the
    diagnostic reads the uncut page, so a page the cut emptied does not read as a broken source."""
    docs, diagnostic = await _afetch_one_core(source, query, limit, fresh, deadline_s, cache_only,
                                              filters, as_of)
    fplan = _plan_filters(filters, [source], query, True, as_of)
    tally = _as_of_tally()
    docs = _as_of_cut_into(docs, as_of, tally)
    meta: dict = {}
    _stamp_as_of_meta(meta, fplan, tally)
    return docs, diagnostic, meta["as_of"]


async def _afetch_one_core(source, query, limit, fresh, deadline_s, cache_only, filters, as_of):
    adapter, outcome, raised = await _aderive_outcome(source, query, limit, fresh, deadline_s,
                                                      cache_only, filters, as_of)
    if raised is not None:
        # Preserve the historical error contract: build the diagnostic, stash it on the exception,
        # and RE-RAISE the original exception. afetch_outcome swallowed the fault into state="errored";
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
    if outcome.held and diagnostic is not None:
        # what OmniSeek's own gates held back rides along, so a drill can tell "not sent" from "nothing
        # there", as the fan-out does in _meta.held (EYE-NOT-SENT-EMPTY, 2026-11-03)
        diagnostic["held"] = outcome.held
    return outcome.docs, diagnostic


def _build_diagnostic(adapter: "SourceAdapter", *, docs: list, captures: list,
                      timed_out: bool, raised: Optional[BaseException],
                      deadline_s: Optional[float]) -> Optional[dict]:
    """Assemble the /eye-fix diagnostic, or None for a clean success (docs + no captured failure).

    fail-open: any assembly error degrades to None (a diagnostic bug never breaks retrieval)."""
    try:
        sr = sealed_reason(adapter)
        if sr and not docs:
            # Sealed on purpose: the empty is the answer, not a fault. Say so plainly (and flag it) so
            # neither the agent nor the /eye-fix loop treats it as a broken source.
            return {"adapter_path": _adapter_source_path(adapter), "returned": 0,
                    "captures": [], "sealed": True, "note": sr}
        if docs and not captures and not timed_out and raised is None:
            return None  # the no-noise success case: results came back, nothing failed → no diagnostic
        # Captures whose helper ALREADY explains the empty (a search-index venue's engine_empty /
        # filtered_all / backend, nowcoder's no_match, a filtered search's out_of_coverage). The generic line below reads as "no evidence
        # was captured", so writing it OVER a captured explanation is what sent one WORKING source to
        # the repair loop twice. When such a capture is present, point AT it.
        explained = sorted({h for h in ((c or {}).get("helper") for c in (captures or [])
                                        if isinstance(c, dict))
                            if h and h.rsplit(".", 1)[-1] in
                            ("engine_empty", "filtered_all", "backend", "no_match",
                             "out_of_coverage")})
        if docs:
            note = "fetch returned documents but an egress failure was captured (partial degrade)"
        else:
            note = "fetch returned nothing"
        if timed_out:
            note = (f"fetch exceeded the {deadline_s:.0f}s deadline (source hung or very slow); "
                    "captures could not be drained from the abandoned thread")
        elif raised is not None:
            note = f"adapter raised: {type(raised).__name__}: {raised}"[:300]
        elif explained and not docs:
            note = f"empty, and the adapter said WHY — see captures: {', '.join(explained)}"
        elif not captures and not docs:
            note = ("empty with no captured egress failure: the source likely returned a "
                    "well-formed response with zero items (query miss, or a parser/selector "
                    "that silently matched nothing: open the adapter to confirm)")
        # The one place every diagnostic is assembled: mask what an exception or capture carried.
        return _redact.redact_obj({
            "adapter_path": _adapter_source_path(adapter),
            "returned": len(docs),
            "captures": captures,
            "note": note,
        })
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
# instead of the agent guessing and re-firing. This is OmniSeek's answer to a typed per-vertical param
# schema, in OmniSeek's idiom: a short imperative string DERIVED FROM the source's own query contract
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


# ── the source spec (filters / coverage / license / samples; see omniseek.core.filters) ──────────────
_license_memo: dict[str, Optional[str]] = {}


def license_of(name: str, adapter: "SourceAdapter" = None) -> Optional[str]:
    """The license a source declares (class attr / sources.json row), else ``upstreams:<uid>`` for the
    upstream that lists it in upstreams.json, else None. Memoized per name (both inputs are static)."""
    if name in _license_memo:
        return _license_memo[name]
    if adapter is None:
        with _registry_lock:
            adapter = _adapters.get(name)
    lic = getattr(adapter, "license", None) if adapter is not None else None
    if not lic:
        try:
            from omniseek.core import upstreams as _ups
            uid = _ups.uid_for_source(name, backend_of(name))
            lic = f"upstreams:{uid}" if uid else None
        except Exception:  # noqa: BLE001 (a spec read must never break a search)
            lic = None
    if adapter is not None:
        _license_memo[name] = lic
    return lic


def spec_of(name: str, adapter: "SourceAdapter" = None) -> dict:
    """A source's machine-readable spec: {filters, coverage, license, samples, as_of_field}. The one
    reader; the omniseek_sources output and the generated docs both come from here, never from a copy."""
    from omniseek.core import filters as _flt
    if adapter is None:
        with _registry_lock:
            adapter = _adapters.get(name)
    return {
        "filters": _flt.describe_fields(_flt.declared_filters(adapter)),
        "coverage": _flt.coverage_of(adapter),
        "license": license_of(name, adapter),
        "samples": _flt.samples_of(adapter),
        "as_of_field": _flt.as_of_field_of(adapter),
    }


def _stamp_license(docs, source: Optional[str] = None) -> None:
    """Put ``metadata.license`` on each doc (the producing source's declared license, "unknown" when
    none). An existing value is kept. Fail-open: a stamp failure never breaks a search."""
    from omniseek.core import filters as _flt
    for d in docs or []:
        try:
            md = d.metadata if isinstance(d.metadata, dict) else {}
            if "license" in md:
                continue
            md = dict(md)
            md["license"] = _flt.license_text(license_of(source or d.source)
                                              or (license_of(d.source) if source else None))
            d.metadata = md
        except Exception:  # noqa: BLE001
            continue


def _plan_filters(raw_filters, target_sources: list, query: str, named: bool, as_of=None):
    """Validate + translate ``raw_filters`` (and push down ``as_of``, a filters.AsOf) for the target
    sources (omniseek.core.filters.plan). None when neither was given. Raises filters.FilterError
    before anything is sent."""
    if not raw_filters and as_of is None:
        return None
    from omniseek.core import filters as _flt
    adapters = {}
    for s in target_sources:
        a = get_adapter(s)
        if a is not None:
            adapters[s] = a
    return _flt.plan(raw_filters or [], adapters, query, named, as_of)


def check_filters(query: str, sources: Optional[list], raw_filters, as_of=None) -> None:
    """Run the filter plan a search with these arguments would run (named: the named sources; broad:
    the broad sweep's live targets) and raise filters.FilterError on a refusal. No egress, no side
    effect: omniseek_search calls it first so a refusal from any shape comes back as one error payload."""
    if not raw_filters and as_of is None:
        return
    if sources is not None:
        _plan_filters(raw_filters, list(sources), query, True, as_of)
        return
    catalog = get_catalog_snapshot()
    plan = build_search_plan(catalog, _build_policy_snapshot(catalog), query, sources=None)
    _plan_filters(raw_filters, plan.broad_live, query, False, as_of)


def _apply_filter_plan(fplan, target_sources: list) -> list:
    """The target list a filtered search actually runs: the sources the plan translated for, plus any
    unknown name (so it still errors exactly as an unfiltered named search would)."""
    if fplan is None:
        return target_sources
    return [s for s in target_sources if s in fplan.run or get_adapter(s) is None]


def _stamp_filter_meta(meta: dict, fplan, results: dict, empty: list, named: bool,
                       diagnostics: dict, deadline: Optional[float]) -> None:
    """Fold a filter plan into a fan-out's _meta: ``filters`` (applied / skipped_unsupported /
    skipped_invalid / out_of_coverage names) and ``out_of_coverage`` {source: reason}; each
    out-of-coverage source also reads as an empty result (and, on a named search, carries a
    diagnostic whose capture says why, the fifth explained empty beside engine_empty / filtered_all
    / backend / no_match)."""
    if fplan is None:
        return
    if fplan.filtered:
        meta["filters"] = fplan.meta()
    if fplan.out_of_coverage:
        meta["out_of_coverage"] = dict(sorted(fplan.out_of_coverage.items()))
        for src, reason in fplan.out_of_coverage.items():
            results.setdefault(src, [])
            if src not in empty:
                empty.append(src)
            if named:
                d = _build_diagnostic(get_adapter(src), docs=[], captures=[
                    {"helper": f"{src}.out_of_coverage", "body": reason}],
                    timed_out=False, raised=None, deadline_s=deadline)
                if d:
                    diagnostics[src] = d
        meta["empty"] = sorted(empty)
    if diagnostics:
        meta["diagnostics"] = diagnostics


def _as_of_tally() -> dict:
    return {"dropped_after": 0, "dropped_undated": 0, "dropped_unseen": 0}


def _as_of_cut_into(docs: list, as_of, tally: dict) -> list:
    """The exit cut (filters.as_of_cut) on one batch of documents, counted into ``tally``."""
    from omniseek.core import filters as _flt
    kept, later, undated = _flt.as_of_cut(docs, as_of)
    tally["dropped_after"] += later
    tally["dropped_undated"] += undated
    return kept


def _as_of_cut_index(idx: list, as_of, tally: dict) -> list:
    """The local-memory half of as_of: keep a recalled document only when OmniSeek had first seen it
    (first_seen) no later than as_of AND its own date is not after as_of. A document with no first_seen,
    or a memory that cannot be read, is dropped (``dropped_unseen``): it cannot be shown to have been
    known then. Contains a SQLite read (the async caller runs it off the loop)."""
    if not idx:
        return idx
    cutoff = as_of.at.timestamp()
    seen = _seen_before_lookup(idx)
    kept = []
    for d in idx:
        fs = None if seen is None else seen.get((getattr(d, "source", None), _doc_sid(d)))
        if fs is None or fs > cutoff:
            tally["dropped_unseen"] += 1
            continue
        kept.append(d)
    return _as_of_cut_into(kept, as_of, tally)


def _as_of_cut_recall(idx: list, as_of, meta: dict) -> list:
    """Cut the joined recall (_as_of_cut_index) and add its counts to the fan-out's ``_meta.as_of``."""
    tally = _as_of_tally()
    kept = _as_of_cut_index(idx, as_of, tally)
    m = meta.get("as_of")
    if isinstance(m, dict):
        for k, v in tally.items():
            m[k] = m.get(k, 0) + v
    return kept


def _stamp_as_of_meta(meta: dict, fplan, tally: dict) -> None:
    """``_meta.as_of``: the cut moment, which sources got the bound pushed down, which were
    out of coverage, how many results the exit cut removed (after as_of / undated / not yet seen by
    the local memory), and that signals are current values."""
    if fplan is None or fplan.as_of is None:
        return
    from omniseek.core import filters as _flt
    meta["as_of"] = {
        "at": fplan.as_of.at.isoformat(),
        "pushed_down": sorted(fplan.as_of_pushed),
        "exit_cut_only": sorted(s for s in fplan.run if s not in fplan.as_of_pushed),
        "out_of_coverage": sorted(fplan.out_of_coverage),
        "dropped_after": tally["dropped_after"],
        "dropped_undated": tally["dropped_undated"],
        "dropped_unseen": tally["dropped_unseen"],
        "signals": _flt.AS_OF_SIGNALS,
    }


def _field_doc_key(name: str, field_name: str) -> Optional[str]:
    from omniseek.core import filters as _flt
    with _registry_lock:
        adapter = _adapters.get(name)
    spec = _flt.declared_filters(adapter).get(field_name) or {}
    return spec.get("doc_key")


def field_values(name: str, field_name: str, limit: int = 30) -> dict:
    """Known values of one declared filter field of one source, with counts, for omniseek_sources(field=).
    ``from`` says where they came from: ``local_index`` (value counts over this source's documents
    in the local index, via the field's ``doc_key``) or ``declared`` (the source's closed ``values``
    list; no counts) or ``none``."""
    from omniseek.core import filters as _flt
    with _registry_lock:
        adapter = _adapters.get(name)
    if adapter is None:
        return {"source": name, "field": field_name, "error": f"unknown source: {name}"}
    spec_all = _flt.declared_filters(adapter)
    if field_name not in spec_all:
        return {"source": name, "field": field_name,
                "error": f"{name} does not declare a filter field {field_name!r}",
                "available_filters": _flt.describe_fields(spec_all)}
    spec = spec_all[field_name]
    out: dict = {"source": name, "field": field_name, "type": spec.get("type"),
                 "ops": list(spec.get("ops") or [])}
    declared = list(spec.get("values") or [])
    counts = None
    if spec.get("doc_key"):
        try:
            from omniseek.core.recall import store as _store
            counts = _store.field_value_counts(name, spec["doc_key"], limit)
        except Exception:  # noqa: BLE001 (an index read must never break omniseek_sources)
            counts = None
    if counts:
        out["from"] = "local_index"
        out["values"] = [{"value": v, "count": n} for v, n in counts]
        if declared:
            out["declared_values"] = declared
    elif declared:
        out["from"] = "declared"
        out["values"] = [{"value": v, "count": None} for v in declared]
    else:
        out["from"] = "none"
        out["values"] = []
        out["note"] = ("no closed value list is declared and the local index holds no values for "
                       "this field yet")
    return out


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
# prove PARITY against the legacy sync fan-out broad-branch selection; Wave 2 is the cutover: the plan
# is now the SINGLE choke point that the fan-out routes through (both the broad sweep and a named
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
    sealed: dict = field(default_factory=dict)  # name -> seal reason (sealed_reason); wins over all


def _build_policy_snapshot(catalog: dict) -> PolicySnapshot:
    """Assemble Layer 2 fresh from the live policy sources, REUSING each derivation: _profile_enabled
    per source for `enabled`, retired_reason per source for `retired`, the cached runtime overlay +
    the emergency dict verbatim, and _watchdog_down_set / _watchdog_health for the quarantine set +
    as_of. Reads globals HERE (the impure assembly step); the plan it feeds stays pure."""
    enabled = set()  # type: set
    retired = {}  # type: dict
    sealed = {}  # type: dict
    for name in catalog:
        adapter = get_adapter(name)
        if adapter is None:  # a live unregister between catalog build and now: legacy skips it too
            continue
        if _profile_enabled(name, adapter):
            enabled.add(name)
        sr = sealed_reason(adapter)
        if sr:
            sealed[name] = sr
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
        sealed=sealed,
    )


@dataclass(frozen=True)
class SearchPlan:
    """The five broad-branch projections, reproduced PURELY from a catalog + policy. Wave 2: this IS
    the fan-out's selection (the plan routes both branches). Projection contract (the fan-out's
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
    True -> 'explicit-only' coercion as the legacy function. A seal (policy.sealed) precedes all four,
    as in _explicit_only_reason."""
    sr = policy.sealed.get(rec.name, "")
    if sr:
        return f"sealed: {sr}"
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
    registry/policy globals read (all state arrives via the two snapshots). Reproduces the fan-out's
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
            # via arxiv/s2, so they flood any ML query) exactly as the legacy loop does. A SEALED source
            # is never suggested either: re-running it by name only returns the seal note.
            if not reason.startswith("org_watch") and name not in policy.sealed:
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


def _note_held(held: dict, src: str, cap: list, docs: list) -> bool:
    """Record ``src`` in ``_meta.held`` when a gate or an open host breaker refused any of its requests
    (EYE-GATE-FIX C): ``{why: 闸满 | 时限不够 | 主机断路, detail, n, partial}``, ``partial`` when documents
    came back anyway. Returns True when the source came back EMPTY because of it: such a source is not
    also listed in ``_meta.empty`` (it was not asked, which is not "nothing found")."""
    summary = _probe.summarize_held(cap)
    if summary is None:
        return False
    held[src] = dict(summary, partial=bool(docs))
    return not docs


# =============================================================================================
# The fan-out (asearch_many / asearch_ranked): one asyncio.Task per source (create_task COPIES the
# contextvars.Context, so each source's fresh / cache_only / diag is ISOLATED). A native source's
# asearch is awaited on the loop; a source whose search can only block runs its one sync .search on a
# worker thread admitted by _LEGACY_LIMITER (_aegress). The charter's D3 permit / D4 reservation
# machinery is DEFERRED until a MEASURED need appears against it. (D14 met its measured need: the IO
# lane and the process lane in portal.py.) There is no second, synchronous fan-out (11-04, S9): a
# caller on a thread reaches this one through portal.run.
# =============================================================================================

# Strong refs to timed_out straggler tasks DETACHED past the deadline: a straggler keeps running +
# warms the cache, it is NOT cancelled.
# A module-level set keeps each task alive (else the loop could GC it: "Task was destroyed but it is
# pending"); _reap drops the ref AND consumes the outcome when the task finally settles.
_DETACHED: "set[asyncio.Task]" = set()


def _reap(t: "asyncio.Task") -> None:
    """Done-callback for a DETACHED straggler: drop the strong ref, then CONSUME the outcome so a
    straggler that RAISES after the deadline (a common late network failure via the legacy runner:
    HTTP 500 / connection reset) does NOT emit asyncio's 'Task exception was never retrieved' ERROR
    into OmniSeek logs. The sync executor.shutdown(wait=False) path swallows it silently; the async
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
    256-token _LEGACY_LIMITER, so shared-pool backpressure replaces the per-search private 64-thread pool.

    Every source call of OmniSeek passes here (the fan-out, the one-source fetch, the recall ingest, the
    prewarmer), so this is where a live fetch that worked is recorded (``note_fetched_ok``)."""
    with _probe.capture_answers() as heard:
        if isinstance(adapter, AsyncSearchCapable):        # native async adapter (S4b onward)
            from omniseek.core import cache  # local import: avoid package-init cycle
            with cache.flight_scope():                     # one fetch per cache key at a time (cache.py)
                docs = await adapter.asearch(query, limit)
        else:
            # LEGACY RUNNER (D3, simplified): the existing sync .search on the ONE bounded shared pool,
            # its leaf network call gated by the process-global egress semaphore it shares with the sync
            # fan-out's own thread pools.
            docs = await _aegress(adapter, query, limit)
    if docs and heard.ok and not heard.failed:
        await anyio.to_thread.run_sync(note_fetched_ok, getattr(adapter, "name", ""), limiter=portal.IO_LANE)
    return docs


# WHEN A SOURCE LAST FETCHED FOR REAL (EYE-NONE-RESET, 2026-11-07). The watchdog's consecutive-fail
# count moves only on a check that verified something, and about fifteen checks answer "not probed"
# while the source's listing is still cached. The recall ingest refetches that listing on its own
# schedule, so after a fix every check landed on a warm cache and a repaired source read as down for
# good (llm_leaderboard, 11-06: six failed runs, then "not probed" every run). The watchdog now also
# reads this record: a live fetch that worked after the last failed check clears the count
# (infra_jobs._health_track).
# Recorded by _dispatch_search when a source call returned documents, raised nothing, heard at least one
# answer below HTTP 400 from its upstream and no error answer, unreachable connection or open host
# breaker (_probe.capture_answers). An answer read wholly from cache hears nothing and records nothing.
# One file per source under <state>, holding the epoch: each writer replaces its own source's file
# atomically, so OmniSeek's processes and threads never lose each other's writes, and a restart keeps it.
_FETCH_OK_DIRNAME = "source-fetch-ok"
_SAFE_SOURCE_NAME = re.compile(r"[A-Za-z0-9_.-]+")


def _fetch_ok_path(name: str) -> Optional[Path]:
    if not name or not _SAFE_SOURCE_NAME.fullmatch(name) or name.startswith("."):
        return None
    return where.state() / _FETCH_OK_DIRNAME / name


def note_fetched_ok(name: str, at: Optional[float] = None) -> None:
    """Record that source ``name`` fetched documents live at ``at`` (default now). Never raises: the
    record must never break the fetch that made it."""
    path = _fetch_ok_path(name)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{name}.{threading.get_ident()}.tmp")
        tmp.write_text(repr(float(time.time() if at is None else at)), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("note_fetched_ok(%s) failed: %s", name, exc)


def fetched_ok_at(name: str) -> Optional[float]:
    """The epoch of source ``name``'s last recorded live fetch that worked (``note_fetched_ok``), or None."""
    path = _fetch_ok_path(name)
    if path is None:
        return None
    try:
        at = float(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return at if math.isfinite(at) else None


async def asearch_many(
    query: str,
    sources: Optional[list[str]] = None,
    limit_per_source: int = 5,
    deadline_s: Optional[float] = None,
    fresh: bool = False,
    cache_only: bool = False,
    _catalog=None,
    _policy=None,
    filters: Optional[list] = None,
    as_of=None,
) -> tuple[dict[str, list[Document]], dict]:
    """The fan-out: one asyncio.Task per source (see the region header), raw per-source buckets plus
    the deterministic ``_meta``. omniseek_search's raw shape and asearch_ranked await it; a caller on a
    thread reaches it through portal.run."""
    _named = sources is not None
    # Routing: the pure helpers (catalog snapshot, policy snapshot, build_search_plan). A caller that already
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
        catalog, policy = await anyio.to_thread.run_sync(_build_snap, limiter=portal.IO_LANE)
    plan = build_search_plan(catalog, policy, query, sources=sources)
    target_sources = plan.broad_live
    excluded = plan.excluded
    disabled = plan.disabled
    excluded_relevant = plan.excluded_relevant
    skipped_down = plan.skipped_down  # broad-only: watchdog-down sources quarantined from this sweep
    # Typed filters (omniseek.core.filters): validated + translated per source HERE, before any egress.
    # A refusal raises FilterError (nothing sent); the plan narrows the targets to the sources that
    # take every filter, and names the skipped / out-of-coverage ones for _meta.
    _fplan = _plan_filters(filters, target_sources, query, _named, as_of)
    target_sources = _apply_filter_plan(_fplan, target_sources)
    _fq = _fplan.run if _fplan is not None else {}
    _tally = _as_of_tally()  # as_of exit-cut counts (stays zero without as_of)
    if sources is None:
        _broad_default = _SOURCE_DEADLINE_FRESH_S if fresh else _SOURCE_DEADLINE_S
        deadline = deadline_s if deadline_s is not None else _broad_default
    else:
        deadline = deadline_s if deadline_s is not None else _EXPLICIT_DEADLINE_S
    _paper_wait = _paper_sources(target_sources) if sources is None and deadline_s is None else set()
    _paper_deadline = max(deadline, _PAPER_DEADLINE_S)

    if not target_sources:
        # EMPTY-TARGET GUARD (as the fan-out always did): asyncio.wait(set()) RAISES ValueError
        # (unlike concurrent.futures.wait([])), so return the SAME clean early {} + _meta BEFORE any
        # task is built. The zero-source branch.
        _r0: dict = {}
        _m0 = {"searched": 0, "elapsed_s": 0.0, "empty": [], "timed_out": [],
               "errored": {}, "excluded_count": len(excluded), "disabled": sorted(disabled),
               "excluded_relevant": excluded_relevant, "truncated": [],
               "skipped_down": skipped_down,
               "progressive": {"fast": 0, "slow": 0, "timed_out": []}}
        _stamp_filter_meta(_m0, _fplan, _r0, [], _named, {}, deadline)
        _stamp_as_of_meta(_m0, _fplan, _tally)
        return _r0, _m0

    # Progressive-return timing (#6): source -> completion monotonic time. Populated inside _aone in
    # THIS task's copied context; each task writes a DISTINCT key on the single loop thread (no lock).
    # Advisory only: feeds the fast/slow _meta counts after wait(), NEVER any control flow.
    _result_times: dict[str, float] = {}

    async def _aone(source: str) -> tuple[list[Document], list, list]:
        # The fan-out's deadline for every gate this source's requests pass, in this task's context
        # (and, copied by AnyIO, in any worker thread it hands a sync adapter to), and who asked.
        with _guard.deadline_until(_deadline_at + (_paper_deadline - deadline if source in _paper_wait else 0)), \
                _guard.initiator(_who):
            return await _aone_body(source)

    async def _aone_body(source: str) -> tuple[list[Document], list]:
        # Async + context-ISOLATED twin of _one. Runs in THIS task's COPIED context (create_task), so
        # the set_fresh / set_cache_only / diag.enable below touch only this source's context and never
        # cross-pollinate another source's (the S3b no-leak contract, now at the fan-out level).
        from omniseek.core import cache, diag  # local import: avoid package-init cycle
        from omniseek.core import filters as _flt
        cache.set_fresh(fresh)  # in THIS task's copied context -> adapter's cache calls honor it
        cache.set_cache_only(cache_only)  # cache-only (cache_only=True): egresses short-circuit
        adapter = get_adapter(source)
        if adapter is None:
            raise ValueError(f"unknown source: {source!r}")
        if _named:
            diag.enable()  # arm per-source capture in THIS task's context (named search only)
        _q, _up = _fq.get(source, (query, None))
        _flt.set_upstream(_up)  # this task's context only (copied into a worker thread by AnyIO)
        try:
            with _probe.capture_held() as _held:   # what the gates refused (_meta.held)
                docs = await _dispatch_search(adapter, _q, limit_per_source)  # D1 dispatch
            if not cache_only:  # Path A: index enumerable docs (skip during a cache-only collect).
                # maybe_ingest durably appends observations before it enqueues the materializer wake.
                # Keep that durability boundary awaited, but move its file writes and fsync calls off
                # the event loop. The writer daemon still owns every SQLite, vector, and graph write.
                from omniseek.core import recall
                await anyio.to_thread.run_sync(recall.maybe_ingest, docs, limiter=portal.IO_LANE)
            _stamp_license(docs, source)
            _result_times[source] = time.monotonic()  # #6: stamp completion (advisory timing)
            return docs, (diag.drain() if _named else []), _held
        except Exception as exc:  # noqa: BLE001 -- stash captures so the assembly can diagnose
            if _named:
                exc._eye_diag = diag.drain()  # type: ignore[attr-defined]
            raise
        finally:
            cache.set_fresh(False)  # mirror _one; harmless here (this task's copied context dies after)
            cache.set_cache_only(False)
            _flt.set_upstream(None)

    results: dict[str, list[Document]] = {s: [] for s in target_sources}
    empty: list[str] = []
    timed_out: list[str] = []
    errored: dict[str, str] = {}
    truncated: list[str] = []
    diagnostics: dict[str, dict] = {}  # named-search only: per-source /eye-fix evidence
    held: dict[str, dict] = {}  # sources a gate (or an open host breaker) held back: _meta.held
    paper_wait: Optional[dict] = None  # _meta.paper_wait: set only when late paper sources were waited for
    _who = _guard.current_initiator() or "检索"

    # ORDERED source -> task mapping (upholds the SETTLED I2 invariant: results / errored /
    # diagnostics follow CATALOG order, NOT the hash-unordered asyncio.wait done/pending SETS). Insertion
    # order == target/catalog order; create_task COPIES the context per source (per-source isolation).
    t0 = time.monotonic()
    _deadline_at = t0 + deadline  # a straggler's gate waits end here: it is not sent after the caller left
    src_to_task: "dict[str, asyncio.Task]" = {
        s: asyncio.create_task(_aone(s)) for s in target_sources}
    # DEADLINE + PARTIAL-RETURN (as before: wait(timeout=deadline) -> done/not_done). Use
    # done / pending ONLY for the membership test below; ASSEMBLE by the ordered src_to_task walk.
    done, pending = await asyncio.wait(src_to_task.values(), timeout=deadline)
    _task_src = {t: s for s, t in src_to_task.items()}
    _late = [t for t in pending if _task_src[t] in _paper_wait]
    if _late:  # as before: the late paper sources get the rest of their budget
        await asyncio.wait(_late, timeout=max(0.0, t0 + _paper_deadline - time.monotonic()))
        paper_wait = _paper_wait_meta(t0, _paper_deadline, [(_task_src[t], t.done()) for t in _late])
        pending = {t for t in pending if not t.done()}
    for src, task in src_to_task.items():  # ORDERED walk (as before: future_to_source walk)
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
        hcap: list = []
        raised_exc: Optional[BaseException] = None
        try:
            r, caps, hcap = task.result()
        except Exception as exc:  # noqa: BLE001 -- record it, don't kill the search
            errored[src] = f"{type(exc).__name__}: {exc}"[:80]
            r = []
            caps = list(getattr(exc, "_eye_diag", None) or [])
            raised_exc = exc
        # as_of: the exit cut, as before (truncated + diagnostic read the uncut page).
        kept = r if _fplan is None or _fplan.as_of is None else _as_of_cut_into(r, _fplan.as_of, _tally)
        results[src] = kept
        if not _note_held(held, src, hcap, r) and not kept:
            empty.append(src)
        if r and len(r) >= limit_per_source:
            truncated.append(src)  # returned == limit -> likely more exists
        if _named:  # the same /eye-fix diagnostic omniseek_fetch emits (None on a clean success)
            d = _build_diagnostic(get_adapter(src), docs=r, captures=caps,
                                  timed_out=False, raised=raised_exc, deadline_s=deadline)
            if d:
                diagnostics[src] = d

    # Progressive-return facets (#6, fail-open): same fast (< 3s) vs slow (>= 3s) partition as
    # the fan-out. The async model cannot double-count a timed_out straggler that stamps during the
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
    if held:  # sources a gate held back (not "empty": they were not asked, or only partly)
        meta["held"] = held
    if paper_wait:
        meta["paper_wait"] = paper_wait
    _stamp_filter_meta(meta, _fplan, results, empty, _named, diagnostics, deadline)
    _stamp_as_of_meta(meta, _fplan, _tally)
    return results, meta


def _compute_source_diversity(ranked: list[Document]) -> dict:
    """PERSPECTIVE distribution of ranked results. Mechanical tally by a FIXED perspective
    taxonomy (academic / social / audio / walled / news) mapped from each source's routing
    FACETS (domains + modes + access_tier, data-driven from the adapter attr / facets.json,
    self-maintaining as sources are added) — the agent judges what a one-sided distribution
    means; OmniSeek only counts. NOTE the ``kind`` facet (stream/lookup/proxy/portal) is HOW a
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


# ── ranked-path leaf helpers (S4c-1) ── asearch_ranked's steps (recall scope + arm building, the index
# fold, the remaining-budget formula, the seen_before completeness normalize, the passive enrichments +
# taps). The recall arm runs off-loop via asyncio.create_task over anyio.to_thread; the seen_before DB
# read + the fold's as_of DB read hop off-loop via anyio.to_thread.
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

    The CALLER (asearch_ranked) owns HOW the task runs: it kicks it
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

    _armed_at = time.time()

    def _recall_task():
        # semantic=False = the exact-token escape hatch (lexical only, byte-identical ranking);
        # None/True = the hybrid (lexical + vector RRF-fused -> cross-lingual + paraphrase recall).
        # INSTRUMENTED 2026-08-17: queued_ms is submit-to-start, so a starved thread is visible as a
        # late start rather than being blamed on a slow arm; run_ms is the arm's own wall time.
        # recall.hybrid supplies the per-phase split inside run_ms.
        _started = time.time()
        if semantic is False:
            _i = recall.search(query, k=_k, sources=_recall_scope)
            _info = {"lexical": len(_i), "vector": 0, "mode": "lexical"}
        else:
            _i, _info = recall.hybrid(query, k=_k, sources=_recall_scope)
        _now = time.time()
        return _i, {**_info,
                    "queued_ms": round((_started - _armed_at) * 1000, 1),
                    "run_ms": round((_now - _started) * 1000, 1)}

    return _cat, _pol, _recall_task, None


# A filtered search runs no local-index recall: the index holds documents of every period, value and
# subset, so a recalled doc could break the filters the caller asked for.
_FILTERED_INDEX_SKIP = {"lexical": 0, "vector": 0, "mode": "skipped",
                        "reason": "filters given: the local index cannot apply source filters"}


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
    recall failure/timeout degrades to no-index, and meta carries an explicit degraded stamp). Contains
    the as_of DB read, so the async caller runs this OFF the loop via anyio.to_thread."""
    if not idx:
        # The caller already built an honest record of WHY there is no index (mode timeout / error,
        # or a hybrid that matched nothing). Discarding it here is what made a DEGRADED search
        # indistinguishable from one that never ran a recall arm at all: the key simply vanished, so
        # a silent quality collapse read as "search was a bit off today". Stamp it, in the same shape
        # the empty-scope path already uses.
        if info:
            meta["index"] = {**info, "candidates": 0}
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
    This is the ONE place OmniSeek's many author-list shapes collapse to a single representation, so the
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
    """The agent's OWN prior STATEMENTS touching any of ``node_ids`` (a hit's work: ids + its doc: id),
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
    -- AND (b) the agent's OWN prior tier-J STATEMENTS touching this entity (by its work: ids AND its doc:
    id). So both the accreted mechanical relations AND my own recorded judgments arrive ALREADY PLACED in the
    reflexive search, closing the write-side loop with no omniseek_graph verb invoked.

    RAZOR: surfacing my own stored statement is PLACING a recorded judgment, NOT OmniSeek MAKING one -- the
    same operation ``neighborhood(policy=working)`` already performs over the same ``_statement_index`` (which
    ships razor-clean today); the only differences are the trigger (reflexive vs explicit verb) and the budget
    (a capped snippet vs max_nodes). OmniSeek still never judges. The J judgments live in their OWN ``judgments``
    key, NEVER merged into the mechanical ``stored_edges`` counts (the tier line held structurally). Statements load + fold ONCE per search (``load_statements`` is uncached), then O(1)
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
    # The agent's OWN typed statements (tier J): loaded + folded ONCE per search (fail-open to empty), then
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
                                       "agent's judgment, not mechanical facts and not live)")
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
    generator (``aresolve_identity`` for an author, ``aenrich`` for a paper) DETACHED (create_task + _reap,
    the SAME straggler pattern the recall arm uses), so the store's identity +
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
                fut = relations.aresolve_identity(arg)
            elif kind == "enrich":
                fut = _enrich.aenrich([arg])   # enrich takes a LIST of ids
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


async def asearch_ranked(
    query: str,
    sources: Optional[list[str]] = None,
    limit: int = 15,
    deadline_s: Optional[float] = None,
    fresh: bool = False,
    cache_only: bool = False,
    semantic: Optional[bool] = None,
    record_yield: bool = True,
    per_source: Optional[int] = None,
    filters: Optional[list] = None,
    as_of=None,
) -> tuple[list[Document], dict]:
    """The ranked search (omniseek_search's default shape): (documents, _meta). The fan-out + the recall arm
    run as coroutines / off-loop hops.

    OFF-LOOP DISCIPLINE (asearch_ranked runs ON the event loop): every BLOCKING SYSCALL hops OFF the
    loop via ``anyio.to_thread.run_sync``; merge_rank's text scoring runs in the process lane
    (``rank.amerge_rank``: ``rank.text_scores`` through ``portal.run_cpu``); the async fan-out
    (``await asearch_many``), dedup + the rank blend and the enqueue-only enrichment taps stay ON the loop.
      - the recall ARM build (_build_recall_arm: the watchdog-state disk read + indexable_set) -> OFF.
      - the recall ARM run (recall.hybrid / recall.search: SQLite FTS + query-embed + vector matrix) ->
        OFF, kicked as an asyncio.Task over anyio.to_thread BEFORE the fan-out (it overlaps the fan-out),
        joined after with the remaining budget + the fail-open degrade (timeout -> mode "timeout";
        exception -> mode "error").
      - the recall FOLD (_fold_recall_index: recall.as_of SQLite read) -> OFF.
      - the seen_before stamp (_stamp_seen_before -> _seen_before_lookup: batched SQLite read) -> OFF.
    Leaf helpers: _build_recall_arm / _recall_remaining_budget / _fold_recall_index / merge_rank /
    _stamp_seen_before / _normalize_seen_before / _apply_ranked_enrichments."""
    from omniseek.core import rank  # local import keeps the module graph acyclic

    # WALL-CLOCK at THIS search's start (see below): the seen_before stamp compares each doc's
    # persisted first_seen against this instant; this search's own ingest writes carry first_seen >=
    # t0_wall, so they can never flip a doc this very search is the first to retrieve to "seen before".
    t0_wall = time.time()
    # per_source given (a sensor over a source that declares sensor_window) overrides the default
    # per-source cap; the default keeps every other caller exactly as before.
    per_source = per_source if per_source else min(max(limit, 5), 15)
    # Build the recall arm OFF the loop (the watchdog-state disk read inside _build_policy_snapshot +
    # indexable_set) via the SAME shared helper the deleted sync twin uses. Returns the request snapshot (routed
    # into asearch_many so the scope + the fan-out observe one generation) + the arm callable + the
    # honest skip stamp. contextvars (fresh / cache_only / diag) propagate through anyio.to_thread.
    _route_catalog, _route_policy, _recall_task, _index_skipped = await anyio.to_thread.run_sync(
        _build_recall_arm, query, sources, limit, semantic, cache_only, limiter=portal.IO_LANE)
    if filters:
        _recall_task, _index_skipped = None, dict(_FILTERED_INDEX_SKIP)
    # Kick the recall OFF-LOOP concurrently with the fan-out (create_task over to_thread), BEFORE the
    # await, so it overlaps under the fan-out's wall-clock exactly like the sync executor submit.
    _recall_fut = None
    if _recall_task is not None:
        _recall_fut = asyncio.create_task(anyio.to_thread.run_sync(_recall_task, limiter=portal.IO_LANE))

    results, meta = await asearch_many(query, sources, per_source, deadline_s=deadline_s, fresh=fresh,
                                       cache_only=cache_only, _catalog=_route_catalog,
                                       _policy=_route_policy, filters=filters,
                                       **({"as_of": as_of} if as_of is not None else {}))
    total_in = sum(len(v) for v in results.values())  # LIVE bucket count (before index injection)
    if _recall_fut is not None:
        # Join the overlapped recall with the SAME remaining-budget the deleted sync twin waits. Mirror the sync
        # executor.shutdown(wait=False): on the budget elapsing, DETACH the task (do NOT cancel) so the
        # recall thread finishes + warms the index; _reap drops the ref + CONSUMES the outcome so a late
        # raise never emits an "exception never retrieved" ERROR. Same fail-open modes as sync (an empty
        # idx -> an explicit degraded stamp, never a missing key).
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
        if as_of is not None:  # the first_seen read is SQLite -> OFF the loop
            idx = await anyio.to_thread.run_sync(_as_of_cut_recall, idx, as_of, meta, limiter=portal.IO_LANE)
        # The fold does the as_of SQLite read -> OFF the loop (no-op on an empty idx).
        await anyio.to_thread.run_sync(_fold_recall_index, results, meta, idx, info, limiter=portal.IO_LANE)
    elif _index_skipped is not None:
        meta["index"] = _index_skipped  # pure, on-loop: the honest absence stamp (no arm ran)
    # merge_rank's text half (tokenize + BM25 over every document's title and content: 0.6 to 0.75 s
    # for a broad search, measured) runs in the process lane; dedup and the
    # blend stay here. A dead worker is recorded in _meta (rank_error) and nothing is ranked: the
    # loop never takes the scoring back.
    try:
        ranked = await rank.amerge_rank(results, query, limit, backend_of=backend_of)
    except anyio.BrokenWorkerProcess as exc:
        meta["rank_error"] = f"BrokenWorkerProcess: {exc!r}"[:300]
        ranked = []
    _stamp_license(ranked)  # recalled index docs carry no stamp yet (live docs keep theirs)
    meta["deduped"] = {"in": total_in, "out": len(ranked)}
    # seen_before stamp: the batched first_seen lookup is a BLOCKING SQLite read -> OFF the loop. Same
    # fail-open outer guard (a stamp failure never breaks the search); the normalize below is pure CPU.
    try:
        await anyio.to_thread.run_sync(_stamp_seen_before, ranked, t0_wall, limiter=portal.IO_LANE)
    except Exception:  # noqa: BLE001 — a stamp failure must never break the search
        pass
    try:
        await anyio.to_thread.run_sync(_place_graph_presence, ranked, limiter=portal.IO_LANE)   # Phase 1: OFF-LOOP graph-presence stamp
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


def _watchdog_side_map(key: str) -> dict:
    """One per-source side map of the P19 watchdog state ({name: reason}). Fail-open: empty on any
    read/parse error, so a missing file never invents a state."""
    try:
        data = json.loads(_WATCHDOG_STATE.read_text(encoding="utf-8"))
        return data.get(key, {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def _watchdog_unmeasured() -> dict:
    """Sources whose LAST watchdog probe did not complete: {name: reason}. A separate map because
    ``last_status`` is consumed as a KEY SET (see _watchdog_health), where a None value is
    indistinguishable from True. Without this, "our probe timed out" would report as ``ok`` — the
    false GREEN that replaces the false RED the None-as-failure bug used to produce. Fail-open:
    empty on any read/parse error, so a missing file never invents an unmeasured state."""
    return _watchdog_side_map("unmeasured")


def _watchdog_unverified() -> dict:
    """Sources whose LAST watchdog probe COMPLETED but verified nothing: {name: reason}. The adapter
    returned ``None`` because it asked the upstream nothing this run (a metered quota it must not
    spend, a declared gate or an open breaker holding the probe back, an answer served from cache or
    from stored state), or the upstream answered only that it is rate-limiting us (HTTP 429). Kept
    apart from ``unmeasured`` (our probe did not finish) and from ``ok``
    (verified working): before 2026-10-04 these sources returned True and read as ``ok``, so a source
    OmniSeek never tests (context7) counted as healthy forever. Fail-open like _watchdog_unmeasured."""
    return _watchdog_side_map("unverified")


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


def list_sources(live: Optional[dict] = None, domain: Optional[str] = None,
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
    (advisory — never blocks a source). Pass ``live``, the readings of a fresh LIVE probe
    (``await aprobe_health()``, slow), to have them on every entry.

    ``health`` takes one of six values:
      - ``ok``: the watchdog's last probe verified the source works (or one transient failure, below
        the down threshold);
      - ``down``: failing across consecutive runs;
      - ``unmeasured``: OUR last probe did not finish (it hit its hard timeout), so nothing is known;
      - ``unverified``: our last probe finished, but by design it asked the upstream nothing, or the
        answer could not tell good from bad (a quota too scarce to spend, a declared gate or an open
        breaker holding the probe back, an HTTP 429 or other rate limit, an answer served from cache
        or stored state). Neither healthy nor failing;
      - ``unknown``: the watchdog has no row for the source;
      - ``sealed``: switched off on purpose (see sealed_reason); not a failure. The entry also carries
        ``sealed: true`` and ``sealed_reason`` (where to go instead).
    With ``live`` each entry also carries ``healthy``: true (verified working), false
    (verified broken) or null (not verified by this probe: the adapter asked the upstream nothing, or
    the probe timed out), plus ``status``, the adapter's own message saying why.

    By default the per-source ``description`` prose is OMITTED: the facets are the routing
    signal and the full prose for every source is a large unconditional payload. Pass
    ``domain`` (keep only sources whose ``domains`` facet contains it; general sources lead,
    explicit_only trail), ``region`` (exact-match on the ``regions`` facet, e.g. 'sg'/'ca'/'cn'),
    or ``query`` (token-overlap over name + description + domains + regions + cross-lingual
    keywords, ranked best-first) to get the NARROWED set WITH descriptions; or ``verbose=True`` to
    force descriptions on the full list.
    """
    fails, tracked, as_of = _watchdog_health()
    unmeasured = _watchdog_unmeasured()
    unverified = _watchdog_unverified()
    with _registry_lock:  # snapshot the values under the lock; iterate the copy outside it
        adapters_snapshot = list(_adapters.values())
    out = []
    for adapter in adapters_snapshot:
        name = adapter.name
        if name not in tracked:
            health = "unknown"      # not probed by the watchdog (e.g. CDP sources)
        elif fails.get(name, 0) >= _DOWN_AFTER:
            health = "down"         # persistently failing across runs
        elif name in unmeasured:
            health = "unmeasured"   # OUR last probe did not complete; we know nothing either way
        elif name in unverified:
            health = "unverified"   # our last probe completed but, by design, asked the upstream nothing
        else:
            health = "ok"           # healthy, or just a single transient blip
        _sealed = sealed_reason(adapter)
        if _sealed:
            health = "sealed"       # switched off on purpose (sealed_reason), not a failure
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
            **({"sealed": True, "sealed_reason": _sealed} if _sealed else {}),
        }
        # Structured-query hint (the vertical param an agent should put in ``query`` on a NAMED call),
        # present only when the source declares one (see _param_hint). OmniSeek's idiom for a typed
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
        # The machine-readable spec (filters / coverage / license / samples; omniseek.core.filters),
        # read from the source's own declaration. Shipped with the descriptions (narrowed / verbose).
        entry.update(spec_of(name, adapter))
        if live is not None:
            healthy, msg = live.get(name, (None, "not probed"))
            # THREE values, never bool(): None (not verified) must not read as a failure.
            entry["healthy"] = None if healthy is None else bool(healthy)
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
            for k in _SPEC_KEYS:
                e.pop(k, None)
    return out


_SPEC_KEYS = ("filters", "coverage", "license", "samples", "as_of_field")


def facet_vocabulary() -> dict:
    """The closed routing vocabularies (domain / region / kind → source count), from the live
    registry. Handed to the agent by omniseek_list_sources so domain= becomes a DISCOVERABLE router
    instead of a token the agent must already know (a wrong guess used to return a silent empty
    that read as 'OmniSeek has nothing here'). Cheap: facet read only, no health probe."""
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


def _adapter_failure_reason(name: str, captures: list) -> Optional[str]:
    """Turn one adapter's redacted diagnostic captures into a concise caller-facing reason."""
    for capture in reversed(captures):
        detail = capture.get("body") or capture.get("exc")
        if detail:
            return f"{name}: {detail}"
    return None


def _adapter_absent_reason(name: str, captures: list) -> Optional[str]:
    """The reason of an adapter's settled "not there" (a ``diag.note(..., final=True)`` record), else
    None. Only the adapter that owns the URL says it, so the read ends with it and nothing else is tried."""
    for capture in reversed(captures):
        if isinstance(capture, dict) and capture.get("final"):
            detail = capture.get("body") or capture.get("exc") or "not found"
            return f"{name}: {detail}"
    return None


def fetch_url_route(url: str) -> list["SourceAdapter"]:
    """The URL readers a read of ``url`` tries, in the order it tries them (each until one claims it,
    then the generic web read). THE one routing step: the read itself (``_afetch_url_via_adapters_with_
    reason``) walks this list and ``fetch_url_upstream`` walks the same list to find the reader whose
    gate comes first, so replacing this function changes both together. Read-only, sends nothing."""
    with _registry_lock:  # snapshot under the lock; iterate (slow per-adapter fetch) on the copy
        adapters_snapshot = list(_adapters.values())
    return _fetch_url_adapter_order(adapters_snapshot)


def fetch_url_upstream(url: str) -> Optional[str]:
    """The upstream id (upstreams.json) whose declared gate a read of ``url`` passes FIRST, or None.

    Derived from upstreams.json alone, no reader declares it (EYE-GATHER-PACE-R2): walk
    ``fetch_url_route(url)`` in order and take the first reader that has a declaration on the URL's site
    (``upstreams.source_on_site``: an entry covering the reader names a host on the same site, so an
    arxiv.org link meets the arxiv reader through export.arxiv.org). An entry covers a reader the way
    ``upstreams.coverage`` counts it: its name in ``sources``, or its backend (``backend_of``) in
    ``backends`` (EYE-REG-CHECK: the 41 OpenAlex slices are under the openalex entry's backends only).
    That reader sends the first request; its gate is the strictest of the gated entries covering it
    (``upstreams.strictest_gate_for_source``: smallest max_inflight, the earlier entry on a tie), or
    None when it is under none. No reader on the site, or a reader the declarations leave out, is None
    too: the caller treats the read as ungated, and a gate that then refuses it says so as always
    ("request not sent"). Never raises (a scheduling hint must never break a read).

    The route itself does not depend on ``url`` (every reader, in one order), and the declarations say
    which hosts a source SENDS to, not which links it READS; the site match stands in for "reads this
    link". Its two costs: a source declared on the link's site that does not read the link still
    answers (the call queues behind a gate it never meets: slower, never refused); a reader of links on
    another site, with no entry listing it on that site, answers nothing (not queued)."""
    try:
        from omniseek.core import upstreams as _ups
        for adapter in fetch_url_route(url):
            name = getattr(adapter, "name", None)
            if not name:
                continue
            backend = backend_of(name)
            if _ups.source_on_site(name, url, backend):
                return _ups.strictest_gate_for_source(name, backend)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fetch_url_upstream: no answer for %s: %s", url, exc)
    return None


async def _afetch_url_via_adapters_with_reason(
        url: str) -> tuple[Optional[Document], Optional[str], bool, Optional[str]]:
    """Try every adapter's ``afetch_url`` in route order, each bounded by its budget, and retain an
    adapter-owned failure reason when it declines.

    Each attempt runs as its own task (``_arun_bounded``), so the diagnostic capture it arms and the
    deadline it sets stay in that attempt. Only an adapter that emitted evidence for this URL
    contributes a reason; a plain ``None`` remains an ordinary non-claim. An attempt past its budget
    is left to finish on its own (as the daemon thread of the sync read was) and the next adapter is
    tried; an attempt that raises is that adapter's failure, never a cue to read the URL another way.

    An adapter that declines with a settled "not there" (``diag.note(..., final=True)``: its API said
    the repository / issue / package does not exist) ends the read here with that reason: no later
    adapter and no generic web read gets the URL, so a site's 404 page never comes back as the content.
    Returns ``(doc, reason, settled, url_reason)``; ``settled`` is True only for that ending.
    ``url_reason`` is the first reason an adapter recorded about THIS url (a record whose url is the
    read's own), i.e. an adapter that took the URL as its own and failed on it; a record about another
    address (an RSS bundle's feed it scans for the link) does not count.
    """
    adapters_snapshot = fetch_url_route(url)
    adapter_reason: Optional[str] = None
    url_reason: Optional[str] = None
    from omniseek.core import diag  # local import: avoid package-init import cycle
    url_key = diag.strip_secrets(url)
    fulltext_missed = False
    for adapter in adapters_snapshot:
        adapter_class = _fetch_url_adapter_class(adapter)
        fulltext_attempt = (
            adapter_class == "fulltext"
            and _adapter_declares_fetch_url_host(adapter, url)
        )
        search_index_fallback = adapter_class == "search-index" and fulltext_missed
        budget = getattr(adapter, "fetch_timeout", _FETCH_URL_TIMEOUT_S)

        async def _attempt(a=adapter, fallback=search_index_fallback,
                           until=time.monotonic() + max(0.0, float(budget))) -> tuple[Optional[Document], list]:
            from omniseek.core import diag

            diag.enable()
            try:
                with _guard.deadline_until(until):
                    result = await a.afetch_url(url)
                if result is not None and fallback:
                    result.metadata = dict(result.metadata or {})
                    result.metadata["body_needs_read"] = True
                    result.metadata["fulltext_fallback"] = True
                    diag.note(
                        "fetch_url.walled_fallback",
                        url=url,
                        body="full text needs the walled route which was unavailable",
                    )
            except Exception as exc:  # noqa: BLE001 - preserve the adapter's historical error contract
                diag.note(f"{a.name}.fetch_url", url=url, exc=exc)
                exc._eye_url_diag = diag.drain()  # type: ignore[attr-defined]
                raise
            return result, diag.drain()

        try:
            ok, payload = await _arun_bounded(_attempt(), budget)
            if not ok:
                logger.warning("fetch_url: adapter %s exceeded %.0fs on %s — skipping",
                               adapter.name, budget, url)
                continue
            result, captures = payload
            if result is None:
                absent = _adapter_absent_reason(adapter.name, captures)
                if absent:
                    return None, absent, True, absent
            adapter_reason = adapter_reason or _adapter_failure_reason(adapter.name, captures)
            url_reason = url_reason or _adapter_failure_reason(
                adapter.name, [c for c in captures if isinstance(c, dict) and c.get("url") == url_key])
            if result is not None:
                if fulltext_missed and not fulltext_attempt:
                    # The adapter that OWNS this host already tried and failed, so whatever we
                    # are about to return came from a generic route and may be a login wall or
                    # a page shell. Returning it bare makes a failure read as a clean fetch;
                    # say so in the metadata instead. Same principle as the walled_fallback
                    # stamp above, widened past the search-index class.
                    result.metadata = dict(result.metadata or {})
                    result.metadata["owner_adapter_missed"] = True
                    if adapter_reason:
                        result.metadata["owner_adapter_reason"] = adapter_reason
                return result, None, False, None
        except Exception as exc:  # noqa: BLE001
            absent = _adapter_absent_reason(adapter.name, getattr(exc, "_eye_url_diag", []))
            if absent:
                return None, absent, True, absent
            adapter_reason = adapter_reason or _adapter_failure_reason(
                adapter.name, getattr(exc, "_eye_url_diag", [])
            )
            url_reason = url_reason or _adapter_failure_reason(
                adapter.name, [c for c in getattr(exc, "_eye_url_diag", [])
                               if isinstance(c, dict) and c.get("url") == url_key])
            logger.debug("Adapter %s couldn't claim URL %s: %s", adapter.name, url, exc)
        finally:
            if fulltext_attempt:
                fulltext_missed = True
    return None, adapter_reason, False, url_reason


# The receipt of the URL read in progress: omniseek_read opens one around its call (url_read_receipt) and
# afetch_url_with_reason fills it. A context variable rather than an argument, so the function keeps its
# one-argument signature and a stand-in for it (a test spy) needs to know nothing about the cache.
_url_receipt_var: "contextvars.ContextVar[Optional[dict]]" = contextvars.ContextVar(
    "omniseek_url_read_receipt", default=None)


@contextlib.contextmanager
def url_read_receipt():
    """Collect what the URL cache did on the afetch_url_with_reason call inside this block: yields a dict
    that ends up with ``cached`` (bool), ``fetched_at`` (ISO time of the network read behind the
    document) and, on cache trouble, ``diagnostic``. A stand-in reader that never fills it leaves it
    empty, which reads as not cached."""
    receipt: dict = {}
    token = _url_receipt_var.set(receipt)
    try:
        yield receipt
    finally:
        _url_receipt_var.reset(token)


def _url_cache_trouble(receipt: dict, url: str, step: str, exc: BaseException) -> None:
    """A URL-cache read or write that raised: note it on the receipt and in the log, never fail the read."""
    msg = f"url cache {step} failed: {type(exc).__name__}: {exc}"
    logger.warning("fetch_url: %s on %s", msg, url)
    prev = receipt.get("diagnostic") or {}
    prev = dict(prev) if isinstance(prev, dict) else {}
    prev["url_cache"] = f"{prev['url_cache']}; {msg}" if prev.get("url_cache") else msg
    receipt["diagnostic"] = prev


async def afetch_url_with_reason(url: str) -> "tuple[Optional[Document], Optional[str]]":
    """Read ONE URL: ``(doc, None)`` on success, else ``(None, reason)`` where an adapter-owned
    diagnostic wins over a generic web-fallback reason. This lets a caller act differently on a
    WALLED challenge (retry via CDP) vs a genuinely empty page. The generic web read (plain fetch,
    then a Jina headless render on a thin JS-wall / SPA shell) is the ONLY way OmniSeek reaches a page
    outside its ~190 adapters, and runs ONLY after every adapter declines (zero cost to the happy
    path). Any failure degrades to ``(None, reason-or-None)``, preserving the historical
    matched=false contract. Runs on the service's event loop (omniseek_read awaits it); a thread reads
    through ``fetch_url_with_reason``.

    URL CACHE: every route passes through here, so this is the one place a successful read is kept
    (``cache.set_url_read``, the whole document, ``cache.URL_READ_TTL`` seconds, keyed by the URL
    without its #fragment). A repeat read within that window (an agent's head-then-section calls)
    comes from the cache and makes no network call. A failed, empty or stand-in read is never kept,
    nor one whose address carries a credential (it would sit on disk in the cached document).
    A cache that raises is passed over: the read goes out as usual and the trouble is noted. What the
    cache did is reported through ``url_read_receipt`` when the caller opened one. The cache's file
    reads and writes run on the IO lane, off the loop."""
    from omniseek.core import cache as _cache  # local import: avoid package-init import cycle

    receipt = _url_receipt_var.get()
    receipt = {} if receipt is None else receipt
    receipt["cached"] = False
    receipt.pop("fetched_at", None)
    try:
        hit = await anyio.to_thread.run_sync(_cache.get_url_read, url, limiter=portal.IO_LANE)
    except Exception as exc:  # noqa: BLE001 - the cache must never break a read
        hit = None
        _url_cache_trouble(receipt, url, "read", exc)
    if hit is not None and hit[0].source == "web":
        from omniseek.core import web_fallback  # local import: keeps bs4 off the adapter path
        marker = web_fallback.challenge_marker(hit[0].title or "", hit[0].content or "")
        if marker:
            # A bot-check page kept before the generic read learned to refuse it (11-09: OpenReview's
            # "Verifying your browser"): never hand it back, read the URL again.
            logger.info("url cache: skipped a kept challenge page (%r) %s", marker, url)
            hit = None
    if hit is not None:
        receipt["cached"] = True
        receipt["fetched_at"] = hit[1]
        # one line per hit and per store: the record URL_READ_TTL is to be recalibrated against
        logger.info("url cache: hit (fetched_at %s) %s", hit[1], url)
        return hit[0], None
    doc, reason = await _afetch_url_live_with_reason(url)
    if doc is not None:
        fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        receipt["fetched_at"] = fetched_at
        try:
            # an address carrying a credential (a omniseek.redact name) is not written to disk: the cached
            # document would keep it verbatim in its url, source_id and text (EYE-CACHE-SECRET, 2026-11-03)
            if _redact.redact(url) == url and await anyio.to_thread.run_sync(
                    _cache.set_url_read, url, doc, fetched_at, limiter=portal.IO_LANE):
                logger.info("url cache: stored %s", url)
        except Exception as exc:  # noqa: BLE001 - the cache must never break a read
            _url_cache_trouble(receipt, url, "write", exc)
    return doc, reason


def fetch_url_with_reason(url: str) -> "tuple[Optional[Document], Optional[str]]":
    """The one SYNC entry to a URL read, generated from ``afetch_url_with_reason``: for a thread (a
    worker thread of the service, a background loop bound to the portal, a script run under
    ``portal.run_standalone``). It hands the read to the event loop through ``portal.run`` and blocks
    for the answer; the read itself is the async one, so there is no second way to read a URL."""
    return portal.run(afetch_url_with_reason, url)


def _web_read_on_thread(url: str) -> "tuple[Optional[Document], list, float]":
    """The generic web read (``web_fallback.read_via_fallback``), for a worker thread: its SSRF-pinned
    per-hop fetcher (``safeurl.safe_fetch``) and the page extraction are blocking code with no async
    form yet. Returns ``(doc, diag notes, seconds)``."""
    from omniseek.core import diag, web_fallback
    diag.enable()
    t0 = time.monotonic()
    # omniseek_read's generic web read waits on declared gates no longer than an adapter attempt may
    with _guard.deadline_after(_FETCH_URL_TIMEOUT_S):
        doc = web_fallback.read_via_fallback(url)
    return doc, diag.drain(), time.monotonic() - t0


async def _afetch_url_live_with_reason(url: str) -> "tuple[Optional[Document], Optional[str]]":
    """The network half of ``afetch_url_with_reason``: adapters in order, then the generic web read."""
    doc, adapter_reason, settled, url_reason = await _afetch_url_via_adapters_with_reason(url)
    if doc is not None:
        return doc, None
    if settled:  # the adapter that owns the URL said the thing is not there: no generic read
        return None, adapter_reason
    with _registry_lock:
        adapters_snapshot = list(_adapters.values())
    if _has_declared_fetch_url_owner(url, adapters_snapshot):
        return None, adapter_reason
    # No adapter claimed it. LAST RESORT: a generic web read, diag armed to capture its failure reason.
    # Lazy import keeps bs4 off the hot adapter path.
    try:
        doc, notes, secs = await anyio.to_thread.run_sync(_web_read_on_thread, url, limiter=_LEGACY_LIMITER)
        fallback_reason = None if doc is not None else (notes[-1].get("body") if notes else None)
        await anyio.to_thread.run_sync(_note_web_read, url, doc, fallback_reason, secs, limiter=portal.IO_LANE)
        if doc is not None:
            if url_reason:
                # An adapter took this URL as its own and failed on it before the generic read got it: the
                # page may be the site's error page or a shell, so say so, as the adapter loop does.
                doc.metadata = dict(doc.metadata or {})
                doc.metadata["owner_adapter_missed"] = True
                doc.metadata["owner_adapter_reason"] = url_reason
            return doc, None
        return None, adapter_reason or fallback_reason
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_url: web_fallback raised on %s: %s", url, exc)
        return None, None


# EYE-READ-LOG (2026-11-03): one JSON line per live generic web read in <logs>/web-reads.log: the
# url (credential values masked by omniseek.redact, OmniSeek's one list), which path answered
# (render:plain / render:jina / format:json), how many characters came back, how long it took, or
# why nothing came back. No body. infra_jobs.rotate_logs caps it like every other eye log. Before
# this, the generic read left no record: sampling pages for the extractor comparison had to dig
# URLs out of worker transcripts, and an Anubis wall read back as content showed nowhere.
_WEB_READS_LOG = "web-reads.log"


def _note_web_read(url: str, doc: "Optional[Document]", reason: Optional[str], secs: float) -> None:
    try:
        from omniseek import where
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "url": _redact.redact(url), "ok": doc is not None,
               "ms": int(secs * 1000)}
        if doc is not None:
            row["via"] = [t for t in (doc.tags or []) if t.startswith(("render:", "format:"))]
            row["chars"] = len(doc.content or "")
        else:
            row["reason"] = (reason or "")[:200]
        with open(where.logs() / _WEB_READS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 - the record must never break a read
        logger.debug("web read record skipped: %s", exc)


# -----------------------------------------------------------------------------
# Internal
# -----------------------------------------------------------------------------


async def _asafe_health(adapter: SourceAdapter) -> tuple[Optional[bool], str]:
    """``await adapter.ahealth_check()``, never raising, re-read against what held it back.

    The ONE funnel every reader goes through (``ahealth_check_outcome``). While the check runs a
    ledger (``_probe.watching``) records what the egress saw: a 429 or rate-limit answer, a gate of
    OmniSeek refusing to send. A False that comes only from those is turned into None (not verified),
    with the reason in front of the adapter's own message; a False backed by any other error answer
    stands, and a True is never touched. So an adapter that reads every failure as "down" (most of
    them: the shared http helpers return None on a 429) still reports a rate limit as not verified.
    The ledger rides a ContextVar: the awaits of the check and the legacy-lane thread of a blocking
    source's sync ``health_check`` (anyio runs it in a copy of this context) all record into it."""
    with _probe.watching() as led, _guard.initiator("健康检查"):
        try:
            ok, msg = await adapter.ahealth_check()
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"health_check raised: {type(exc).__name__}: {exc}"
    return _probe.reread(ok, msg, led)


_HEALTH_TIMEOUT_S = 25   # DEFAULT per-source hard cap for a LIVE health probe. Bounds a genuinely
                         # hung source (vs the old ∞ hang) without inflating the whole sweep: the
                         # daily source-health job has a 900s budget and the fast lane only 300s,
                         # so raising this globally to cover the few slow probes would risk the
                         # budget for all ~220. A source that legitimately needs longer declares
                         # ``health_timeout_s`` on its adapter instead (see _aprobe_all_health).
                         #
                         # This comment used to assert "slowest legit ~18s". MEASURED 2026-08-19,
                         # sequentially and paced: rl_llm_frameworks 65.6s, github_releases 40.6s,
                         # vast_ai 28.3s, all answering healthy. The assertion had rotted, and every
                         # probe over the cap was being recorded as a source failure. Treat any
                         # number here as a measurement with a date, never as a standing bound.
_HEALTH_WORKERS = 24     # health checks are I/O-bound — probe many at once


async def ahealth_check_outcome(adapter: SourceAdapter,
                                timeout: Optional[float] = None) -> tuple[Optional[bool], str, bool]:
    """``await adapter.ahealth_check()`` with a HARD timeout.

    Returns ``(healthy, status, completed)``. ``completed`` is False only when the check did not
    return within ``timeout`` (then ``healthy`` is None and ``status`` says so). It is the one place
    that tells the two kinds of ``None`` apart: OUR probe not finishing (the watchdog's
    ``unmeasured``) versus the adapter finishing and reporting that, by design, it asked the upstream
    nothing this time (the watchdog's ``unverified``). The check runs as its own task
    (``_arun_bounded``); past ``timeout`` the wait ends and the running check is abandoned
    (ASYNC-CORE-DESIGN.md D2: "running work is abandoned at its execution budget"): the task finishes
    on its own, and so does a blocking source's thread inside it (opened by
    ``SourceBase._athreaded_health``), whatever it awaits, so a source whose check hangs is UNABLE to
    stall its caller. No thread is opened here: a native check awaits on the loop, and only the base
    default of a blocking source takes a legacy-lane thread. The live
    ``omniseek_sources`` probe and the health-watchdog (through ``portal.run``) all go through here. The
    probe's declared gates stop waiting at the same cap (``_guard.deadline_until``).

    ``timeout`` None means the adapter's declared ``health_timeout_s``, else ``_HEALTH_TIMEOUT_S``.
    Resolved HERE so every caller honours a declaration: the watchdog's ``_health_probe`` passes no
    timeout, and until 2026-10-04 it cut every declaring source at the 25 s default."""
    if timeout is None:
        timeout = float(getattr(adapter, "health_timeout_s", None) or _HEALTH_TIMEOUT_S)
    until = time.monotonic() + max(0.0, float(timeout))

    async def _probe_once() -> tuple[Optional[bool], str]:
        with _guard.deadline_until(until):  # the probe's gates stop at its cap
            return await _asafe_health(adapter)

    done, r = await _arun_bounded(_probe_once(), timeout)
    if not done:
        return None, f"timeout (>{int(timeout)}s): health_check did not return", False
    ok, msg = r
    # Every health message (the live omniseek_sources probe, the watchdog, its state file and alerts)
    # passes here; an HTTPStatusError's text carries the full request address, key included.
    return ok, _redact.redact(msg), True


async def ahealth_check_bounded(adapter: SourceAdapter, timeout: Optional[float] = None) -> tuple[Optional[bool], str]:
    """``(healthy, status)`` of ``ahealth_check_outcome``: True verified working, False verified
    broken, None not verified (the adapter asked the upstream nothing this time, or the check timed
    out; ``status`` says which). Callers that must tell those two Nones apart use
    ``ahealth_check_outcome``."""
    ok, msg, _completed = await ahealth_check_outcome(adapter, timeout)
    return ok, msg


async def _aprobe_all_health(adapters: list, timeout: float = _HEALTH_TIMEOUT_S) -> dict[str, tuple]:
    """Probe EVERY adapter's health concurrently (at most ``_HEALTH_WORKERS`` at once), each bounded by
    its cap, with a global backstop so the whole call returns in ~the largest cap + 10 s even if
    several block. A probe still queued at the backstop is cancelled before it starts; a running one
    is abandoned by ``ahealth_check_outcome``; both read as a timeout.

    PER-SOURCE cap: an adapter may declare ``health_timeout_s`` when its own probe is legitimately
    slow (a bundle that walks N feeds serially). Measured 2026-08-19, the slowest HEALTHY probe was
    65.6s against this 25s default, so those sources were recorded as failures every run. Only the
    declaring source pays the longer wait; the global backstop follows the largest cap in the batch.
    """
    results: dict[str, tuple] = {}
    caps = {a.name: float(getattr(a, "health_timeout_s", None) or timeout) for a in adapters}
    slots = anyio.CapacityLimiter(min(_HEALTH_WORKERS, len(adapters) or 1))

    async def _one(a) -> None:
        async with slots:
            results[a.name] = await ahealth_check_bounded(a, caps[a.name])

    with anyio.move_on_after((max(caps.values()) if caps else timeout) + 10):
        async with anyio.create_task_group() as tg:
            for a in adapters:
                tg.start_soon(_one, a)
    for name, cap in caps.items():
        results.setdefault(name, (None, f"timeout (>{int(cap)}s)"))
    return results


async def aprobe_health() -> dict[str, tuple]:
    """A fresh LIVE health probe of every registered adapter (``_aprobe_all_health`` over a registry
    snapshot): ``{name: (healthy, status)}``, healthy True / False / None (None = not verified; see
    SourceAdapter.ahealth_check). What ``list_sources(live=...)`` and omniseek_sources(check_health=True) show."""
    with _registry_lock:  # snapshot under the lock; the slow probe runs on the copy outside it
        adapters_snapshot = list(_adapters.values())
    return await _aprobe_all_health(adapters_snapshot)
