"""Unified fetcher — the entry point for Penumbra eye operations.

All source adapters register themselves with this module via
register_adapter(). The fetcher then routes queries to the appropriate
adapter and aggregates results.

The MCP server (penumbra.server) wraps these functions as tools.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from penumbra.core.normalize import Document

logger = logging.getLogger(__name__)

# search_many() fans out to EVERY source at once (network-bound threads are cheap),
# then returns after a per-source DEADLINE — a slow/hung source is dropped from THIS
# search but keeps running in the background, warming its cache for the next call.
# This bounds a broad search to ~the deadline instead of the slowest source's tail.
_SEARCH_WORKERS = 64
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
# attribute (True, or better a short reason string, surfaced in the penumbra_sources roster's
# explicit_only_reason and in _meta.excluded_relevant when query-relevant);
# config-driven sources carry it on their row. This dict remains ONLY as an
# emergency override (name -> reason) for an adapter you cannot edit right now;
# it is normally EMPTY. The smoke gate freezes the effective set, so it can only
# change deliberately (edit the adapter AND the frozen list in tests/smoke.py).
_EXPLICIT_ONLY_SOURCES: dict[str, str] = {}

# A REVERSIBLE runtime retire overlay (the curator one-tap penumbra_curator_retire_live): name -> a
# "retired:<reason> <date>" reason string. Lives OUTSIDE the deploy tree (rides the state backup,
# pristine tree), so a live retire takes effect with NO restart and NO git. CACHED in-process (the
# broad-search routing loop reads _explicit_only_reason once per source per query, a disk read
# there would be O(sources x queries)); the cache is invalidated on every write to the overlay
# (apply_live.invalidate_explicit_only_overrides), so a live retire/rollback shows up at once. The
# DURABLE half (the in-tree explicit_only edit + the smoke frozen-list line) is staged for the operator.
_EXPLICIT_ONLY_OVERRIDES_PATH = (
    Path.home() / ".penumbra" / "state" / "curator" / "explicit_only_overrides.json")
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


def _explicit_only_reason(adapter: "SourceAdapter") -> str:
    """Why this adapter is excluded from the broad fan-out ('' = included).

    The adapter's own ``explicit_only`` attribute wins (True or a reason string); the runtime retire
    overlay supplements (a reversible live retire); then the emergency override dict.
    """
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
    from penumbra.core.sources.scrape._rss import RSSAdapterBase
    if isinstance(adapter, RSSAdapterBase):
        return "stable"
    # keyed: a documented API behind a free key / credential.
    if getattr(adapter, "needs_credentials", False):
        return "keyed"
    # stable: a public no-key API — the declarative REST/JSON table rows + the hand-written
    # api/ adapters (arxiv / dblp / openalex / crossref / hackernews …).
    from penumbra.core.sources._declarative import DeclarativeAPIAdapter
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
    if _CIRCUMVENTION_RE.search(_explicit_only_reason(adapter) or ""):
        return "circumvention"
    stab = _derive_stability(adapter)
    if stab == "walled" or getattr(adapter, "needs_credentials", False):
        return "walled"
    if stab == "keyed":
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


from penumbra.core import profile as _profile  # noqa: E402  — stdlib-only module, no import cycle


def _profile_enabled(name: str, adapter: "SourceAdapter") -> bool:
    """Whether the deployment PROFILE exposes this source (broad fan-out + named penumbra_fetch). No
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
    """Public predicate for the named path (penumbra_fetch): True if the profile exposes ``name`` (or the
    adapter is unknown — let the normal 'unknown source' path handle that)."""
    a = get_adapter(name)
    return a is None or _profile_enabled(name, a)


# P19 health-watchdog state — surfaced (advisory) in list_sources so the agent can
# route around currently-dead sources. Recent, not instant (watchdog runs 6-hourly).
_WATCHDOG_STATE = Path.home() / ".penumbra" / "state" / "health-watchdog-state.json"

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
        logger.info("live-registered adapter: %s", adapter.name)


def unregister_adapter(name: str) -> None:
    """Remove an adapter from the RUNNING registry (the curator rollback primitive). Under
    _registry_lock + idempotent: a double-rollback on an already-gone name is a no-op. After this
    get_adapter(name) returns None; fetch_one / search_many._one / fetch_url all tolerate that."""
    with _registry_lock:
        if _adapters.pop(name, None) is not None:
            logger.info("live-unregistered adapter: %s", name)


def get_adapter(name: str) -> Optional[SourceAdapter]:
    return _adapters.get(name)  # LOCK-FREE hot path: dict.get is atomic; see _registry_lock note.


def all_adapter_names() -> list[str]:
    with _registry_lock:  # snapshot the keys under the lock, return the materialized copy
        return list(_adapters.keys())


# -----------------------------------------------------------------------------
# Public API (used by penumbra.server MCP tools)
# -----------------------------------------------------------------------------


_FETCH_ONE_DEADLINE_S = 90.0   # default backstop for a single-source fetch — generous for
                               # slow CDP/reddit/RSS yet bounds a genuinely hung source.
_FETCH_URL_TIMEOUT_S = 30.0    # default per-adapter cap when probing who-claims-this-URL.
                               # A genuinely slow adapter (e.g. a CDP source that scrolls a
                               # full comment thread) may declare a larger budget via a
                               # ``fetch_timeout`` attribute — the default stays tight so a
                               # STALLED adapter still can't hang penumbra_add_url.


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
            box["e"] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return False, None
    if "e" in box:
        raise box["e"]
    return True, box.get("r")


def fetch_one(source: str, query: str, limit: int = 10, fresh: bool = False,
              deadline_s: Optional[float] = _FETCH_ONE_DEADLINE_S) -> list[Document]:
    """Fetch from ONE named source, BOUNDED by ``deadline_s`` (default 90s).

    A daemon-thread backstop means a hung source can never stall the caller (the
    watchtower daemon, ``penumbra_fetch``, ``penumbra_add_url``); on timeout it returns ``[]``.
    Pass ``deadline_s=None`` to opt OUT (deliberate unbounded — a slow source you truly
    want complete). ``fresh=True`` bypasses the cache for live data.

    Returns the documents only (the historical contract); ``fetch_one_with_diag`` is the
    sibling that also returns the failure-evidence trace for the /eye-fix repair loop.
    """
    return fetch_one_with_diag(source, query, limit, fresh=fresh, deadline_s=deadline_s)[0]


def fetch_one_with_diag(source: str, query: str, limit: int = 10, fresh: bool = False,
                        deadline_s: Optional[float] = _FETCH_ONE_DEADLINE_S
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
    (``exc._eye_diag``) so ``penumbra_fetch`` can still surface them; the historical contract that
    the error propagates is preserved. fail-open: the diagnostic machinery is wrapped so it
    can NEVER turn a working retrieval into a broken one.
    """
    adapter = get_adapter(source)
    if adapter is None:
        raise ValueError(
            f"Unknown source: {source!r}. Known: {sorted(_adapters.keys())}"
        )
    from penumbra.core import cache, diag  # local import: avoid package-init import cycle

    def _work() -> tuple[list[Document], list]:
        cache.set_fresh(fresh)
        diag.enable()  # arm capture in THIS worker thread (the contextvar is thread/context-local)
        try:
            docs = adapter.search(query, limit)
            from penumbra.core import recall  # local import: avoid package-init cycle
            recall.maybe_ingest(docs)  # Path A: index enumerable docs (no-op off the eye-http process)
            return docs, diag.drain()
        except BaseException as exc:  # the adapter raised: note it, attach the trace, RE-RAISE
            try:
                diag.note(f"{source}.search", exc=exc)  # so the contract is byte-identical to before
                exc._eye_diag = diag.drain()  # type: ignore[attr-defined] (read by the except below)
            except Exception:  # noqa: BLE001 (capture must never mask the real error)
                pass
            raise
        finally:
            cache.set_fresh(False)  # never leak `fresh` into the next request on a reused thread

    docs: list[Document] = []
    captures: list = []
    timed_out = False
    try:
        if deadline_s is None:
            docs, captures = _work()
        else:
            ok, r = _run_bounded(_work, deadline_s)
            if not ok:
                # The bounded thread is still blocked (daemon → dies with the process); its captures
                # never drained. Report the timeout itself as the evidence + the historical [].
                logger.warning("fetch_one(%s) exceeded %.0fs deadline, returning []",
                               source, deadline_s)
                timed_out = True
            else:
                docs, captures = r
    except BaseException as exc:  # noqa: BLE001 (preserve the contract: re-raise after stashing the
        diagnostic = _build_diagnostic(  # diagnostic so penumbra_fetch can surface it even on a hard error)
            adapter, docs=[], captures=getattr(exc, "_eye_diag", []) or [],
            timed_out=False, raised=exc, deadline_s=deadline_s)
        try:
            exc._eye_diagnostic = diagnostic  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        raise

    diagnostic = _build_diagnostic(adapter, docs=docs, captures=captures,
                                   timed_out=timed_out, raised=None, deadline_s=deadline_s)
    return docs, diagnostic


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


def _facet_keywords(name: str) -> str:
    """Cross-lingual English routing aliases for a source: the built-in _ROUTING_KEYWORDS map plus
    any facets.json ``keywords`` (a string or list). Chinese-described walled sources carry NO
    English token an English query can overlap, so they never surfaced for an English task despite
    being exactly the depth web search lacks; these aliases give them an English surface WITHOUT
    rewriting the description. Empty when none declared."""
    fb_kw = (_FACETS.get(name) or {}).get("keywords") or ""
    fb_str = " ".join(fb_kw) if isinstance(fb_kw, list) else str(fb_kw)
    return (_ROUTING_KEYWORDS.get(name, "") + " " + fb_str).strip()


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
    from penumbra.core import relevance  # local: leaf module, avoid any package-init cycle
    q_tokens = set(relevance.query_terms(query or ""))
    if not q_tokens:
        return 0
    domains, regions, desc = _adapter_match_surface(adapter)
    surface = set(relevance.tokenize(" ".join([*domains, *regions, desc])))
    return len(q_tokens & surface)


def _query_overlaps_source(query: str, adapter: "SourceAdapter") -> bool:
    """True iff the query thematically matches an excluded source's facets/description (any token
    overlap). Thin bool over _query_overlap_count for callers that only need presence."""
    return _query_overlap_count(query, adapter) > 0


def search_many(
    query: str,
    sources: Optional[list[str]] = None,
    limit_per_source: int = 5,
    deadline_s: Optional[float] = None,
    fresh: bool = False,
    cache_only: bool = False,
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
    full name->reason MAP lives in penumbra_sources, one call away, never re-shipped per query).
    ``excluded_relevant`` is the query-AWARE slice the agent acts on: walled/slow sources whose
    facets/description thematically match THIS query, each with a copy-paste ``sources=[...]``
    re-run hint (empty when no excluded source matches). ``progressive`` = ``{fast, slow, timed_out}``:
    the fast/slow fan-out partition as COUNTS (non-actionable) plus the timed_out NAMES (actionable).
    A NAMED search (sources=[...]) also carries
    ``diagnostics`` when present: the same per-source /eye-fix evidence penumbra_fetch emits, for each
    named source that came back empty / errored / timed out (a broad sweep never arms it).
    """
    excluded_relevant: list[dict] = []
    _er_scored: list[tuple[int, dict]] = []  # (overlap_count, hint) → ranked + capped after the loop
    # A NAMED search (sources=[...] explicit) arms the per-source /eye-fix diagnostic; a broad
    # sweep (sources=None) deliberately does NOT, so the 64-worker fan-out stays zero-cost and
    # never cross-pollutes one source's trace with another's (see diag.py).
    _named = sources is not None
    if sources is None:
        target_sources, excluded, disabled = [], {}, []
        for s in all_adapter_names():
            adapter = get_adapter(s)
            if adapter is None:
                continue
            if not _profile_enabled(s, adapter):
                disabled.append(s)  # turned off by the deployment profile: a true off (not broad, not nameable)
                continue
            reason = _explicit_only_reason(adapter)
            if reason:
                excluded[s] = reason
                # Query-AWARE absence explanation: when an excluded (walled/slow) source thematically
                # matches THIS query, surface it with a copy-paste re-run hint, instead of leaving the
                # agent to read an opaque blanket exclusion. Additive (no auto-inclusion of the source).
                # SKIP org_watch lab feeds: their own reason says their papers already reach broad
                # search via arxiv/s2, so they are redundant here and (being ~41 of the excluded set
                # with ML-token descriptions) flood the channel on any ML query, burying the real hit.
                if not reason.startswith("org_watch"):
                    _n = _query_overlap_count(query, adapter)
                    if _n:
                        _er_scored.append((_n, {
                            "name": s, "reason": reason,
                            "why": f"relevant but excluded; re-run naming it: sources=['{s}']",
                            "overlap": _n}))
            else:
                target_sources.append(s)
        # Rank the matched walled sources best-first (most query-token overlap) and cap: the agent
        # gets the few sharpest hits to name, not a 40-50 item wall it rationally ignores.
        _er_scored.sort(key=lambda t: (-t[0], t[1]["name"]))
        excluded_relevant = [d for _, d in _er_scored[:6]]
        # Fresh broad keeps the wider 16s budget (cold + contended, wants completeness);
        # the cache-allowed default uses 11s (measured: only reddit, dropped at 16s anyway).
        _broad_default = _SOURCE_DEADLINE_FRESH_S if fresh else _SOURCE_DEADLINE_S
        deadline = deadline_s if deadline_s is not None else _broad_default
    else:
        target_sources = list(sources)
        excluded = {}
        disabled = []
        deadline = deadline_s if deadline_s is not None else _EXPLICIT_DEADLINE_S

    if not target_sources:
        return {}, {"searched": 0, "elapsed_s": 0.0, "empty": [], "timed_out": [],
                    "errored": {}, "excluded_count": len(excluded), "disabled": sorted(disabled),
                    "excluded_relevant": excluded_relevant, "truncated": [],
                    "progressive": {"fast": 0, "slow": 0, "timed_out": []}}

    # Progressive-return timing (#6): source -> completion monotonic time. Populated inside
    # _one() (closure capture, same pattern as fresh/cache_only/query). Concurrent writes go to
    # DISTINCT keys (one per source) which are atomic in CPython — no lock needed. Advisory only:
    # feeds the fast/slow/pending _meta facets after wait(), NEVER any control flow (wait() is
    # kept exactly as load-tested; the razor: this does not touch ranking).
    _result_times: dict[str, float] = {}

    def _one(source: str) -> tuple[list[Document], list]:
        from penumbra.core import cache, diag  # local import: avoid package-init cycle
        cache.set_fresh(fresh)  # set in the worker thread → adapter's cache calls honor it
        cache.set_cache_only(cache_only)  # cache-only (cache_only=True): egresses short-circuit
        adapter = get_adapter(source)
        if adapter is None:
            raise ValueError(f"unknown source: {source!r}")
        if _named:
            diag.enable()  # arm per-source capture in THIS worker thread (named search only)
        try:
            docs = adapter.search(query, limit_per_source)
            if not cache_only:  # Path A: index enumerable docs (skip during a cache-only collect)
                from penumbra.core import recall
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
            if _named:  # the same /eye-fix diagnostic penumbra_fetch emits (None on a clean success)
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
        # THIS query), so it is not per-query _meta: only its COUNT rides here, and penumbra_sources carries
        # every source's explicit_only reason so the full catalog stays one call away. The query-AWARE
        # slice the agent DOES act on is excluded_relevant (name + reason + overlap + re-run hint).
        "excluded_count": len(excluded),
        "disabled": sorted(disabled),
        "excluded_relevant": excluded_relevant,
        "truncated": sorted(truncated),
        # fast/slow collapsed to counts; timed_out NAMES kept (actionable) inside the one block.
        "progressive": {"fast": fast_count, "slow": slow_count, "timed_out": sorted(timed_out)},
    }
    if diagnostics:  # named search only, and only when a named source was empty / errored / slow
        meta["diagnostics"] = diagnostics
    return results, meta


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


def _seen_before_lookup(ranked: list[Document]) -> dict[tuple[str, str], float]:
    """The batched first_seen lookup half of the seen_before stamp: for each ranked doc's
    (source, source_id), the earliest persisted first_seen across the perception memory (docs
    UNION thin document nodes, graph_nodes kind='document'). A doc the deployment has NEVER
    retrieved is simply absent from the returned map (the stamp pass reads that as first-time-seen).
    Fail-open at every step: recall disabled / no connection / a bad row → the (possibly partial)
    map so far, NEVER an exception into the caller."""
    first_seen: dict[tuple[str, str], float] = {}
    try:
        from penumbra.core.recall import store
        if store._disabled:
            return first_seen
        con = store._read_con()
        if con is None:
            return first_seen
    except Exception as exc:  # noqa: BLE001
        logger.debug("seen_before store unavailable: %s", exc)
        return first_seen
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
    from penumbra.core.recall.graph import doc_node_id
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
    """Stamp ``metadata['seen_before']`` (true|false) + ``first_seen_at`` (value|null) on EVERY ranked
    doc: whether THIS deployment had retrieved the doc BEFORE this search (the wall's novelty stamp).

    COMPLETENESS CONTRACT (P11 W2): every doc in a ranked response carries BOTH keys, NEVER absent. A
    doc the deployment has retrieved before → ``seen_before=True`` + the ISO first_seen_at; a doc it
    is seeing for the FIRST time (or recall is disabled, or a synthetic doc with no persisted row) →
    the HONEST ``seen_before=False`` + ``first_seen_at=None`` (null = never-seen, the true novelty
    state), not a MISSING key. The bug this fixes: the stamp used to be presence-gated (only docs that
    already had a persisted first_seen got stamped at all), so a freshly live-fetched doc came back
    with no seen_before while its previously-seen siblings carried one: a silent, uneven contract.

    ``seen_before`` is True iff a first_seen exists AND is strictly earlier than ``t0_wall`` (this
    search's start), so a doc THIS search is the first to see is False (its first_seen >= t0_wall, or
    absent). The lookup is one BATCHED first_seen probe across docs UNION thin graph_nodes; it is
    fail-open (a lookup failure leaves the map empty, so every doc reads as False, still stamped)."""
    if not ranked:
        return
    from datetime import datetime, timezone
    first_seen = _seen_before_lookup(ranked)
    for d in ranked:
        fs = None
        source = getattr(d, "source", None)
        if source:
            fs = first_seen.get((source, _doc_sid(d)))
        d.metadata = dict(d.metadata or {})
        # The contract: BOTH keys, on EVERY doc. Absent first_seen (or first_seen >= t0_wall = this
        # search's own write) → the honest never-seen-before state, not a missing stamp.
        if fs is not None and fs < t0_wall:
            d.metadata["seen_before"] = True
            try:
                d.metadata["first_seen_at"] = datetime.fromtimestamp(fs, timezone.utc).isoformat()
            except Exception:  # noqa: BLE001 (the boolean stamp still stands without the ISO string)
                d.metadata["first_seen_at"] = None
        else:
            d.metadata["seen_before"] = False
            d.metadata["first_seen_at"] = None


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
    from penumbra.core import rank  # local import keeps the module graph acyclic

    # WALL-CLOCK at THIS search's start (search_many's own t0 is monotonic; the DB stores epoch
    # seconds). The seen_before stamp compares each doc's persisted first_seen against this instant.
    # Race-proof BY CONSTRUCTION: this search's own async ingest writes carry first_seen >= t0_wall
    # (they are stamped with time.time() only AFTER this line runs), so they can never flip the stamp
    # to "seen before" for a doc this very search is the first to retrieve.
    t0_wall = time.time()
    per_source = min(max(limit, 5), 15)
    results, meta = search_many(query, sources, per_source, deadline_s=deadline_s, fresh=fresh,
                                cache_only=cache_only)
    total_in = sum(len(v) for v in results.values())  # LIVE bucket count (before index injection)
    # Hybrid: fold the perception-memory index's recall into the SAME merge_rank/dedup the live
    # path uses — index docs collapse against live twins by fingerprint, rolled-off docs the feeds
    # forgot resurface, and the eye answers even network-down. The index is pure RECALL: merge_rank
    # re-scores it identically to live docs (THE RAZOR). A cache_only pickup stays cache-only.
    if not cache_only:
        from penumbra.core import recall
        k = max(50, 4 * limit)
        # semantic=False = the exact-token escape hatch (lexical only, ranking byte-identical to
        # Phase 1); None/True = the hybrid (lexical + vector RRF-fused → cross-lingual + paraphrase
        # recall lexical can't). merge_rank re-scores BOTH index + live docs identically (the razor).
        if semantic is False:
            idx = recall.search(query, k=k)
            info = {"lexical": len(idx), "vector": 0, "mode": "lexical"}
        else:
            idx, info = recall.hybrid(query, k=k)
        if idx:
            results["_index"] = idx
            meta["index"] = {**info, "candidates": len(idx), "as_of": recall.as_of()}
    ranked = rank.merge_rank(results, query, limit)
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
    # ── Passive enrichments (#8 source_diversity, #11 conflicts) ── computed AFTER merge_rank on
    # the already-ranked+deduped list. Each is a MECHANICAL measurement stamped as _meta for the
    # agent to interpret; NEITHER is fed to composite()/ranking (the razor — order is unchanged).
    # Each is wrapped fail-open so one signal's failure never corrupts `ranked` or the other _meta.
    try:
        meta["source_diversity"] = _compute_source_diversity(ranked)
    except Exception:  # noqa: BLE001 — an enrichment failure must never break the search
        pass
    try:
        # #11 conflicts: rank.dedup stamps same-group cross-source Signal divergence on each
        # survivor at MERGE time (the only place the collapsed members are still visible);
        # collect the survivors' stamps here. Key ABSENT when no conflict → zero noise.
        _cf = [c for d in ranked for c in (d.metadata or {}).get("signal_conflicts", [])][:5]
        if _cf:
            meta["conflicts"] = _cf
    except Exception:  # noqa: BLE001 — an enrichment failure must never break the search
        pass
    # ── P4 conflicts graph tap (fail-open) ── dedup also stamped each survivor with a PRIVATE
    # _conflict_pairs record (full identities + signal kind + values) beside the agent-visible
    # signal_conflicts. Collect those records, mint the doc<->doc conflicts edges (enqueue-only,
    # after dedup, NEVER blocking), then POP the private key so it never reaches the agent (the
    # STABILITY contract: the doc shape the agent sees is byte-identical to pre-P4). The tap is a
    # no-op off the eye-http process (WRITES_ENABLED off) exactly like every other tap.
    try:
        _conf_pairs = [c for d in ranked for c in (d.metadata or {}).get("_conflict_pairs", [])]
        if _conf_pairs:
            rank._conflict_tap(_conf_pairs)
        for _d in ranked:
            if _d.metadata and "_conflict_pairs" in _d.metadata:
                _d.metadata.pop("_conflict_pairs", None)   # private: never agent-facing
    except Exception:  # noqa: BLE001 — the tap NEVER touches the search result
        pass
    # ── Curator P2 yield tap (fail-open) ── records each source's marginal contribution to this
    # top-K. Skipped for synthetic/in-process searches (record_yield=False) and cache-only pickups
    # so they never pollute the real-traffic statistic. The tap NEVER touches `ranked`
    # and NEVER raises into search.
    if record_yield and not cache_only:
        try:
            from penumbra.core.curator import yield_tap
            yield_tap.record_search(query, ranked, results, meta)
        except BaseException:  # noqa: BLE001 the tap NEVER touches search
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


def list_sources(check_health: bool = False, domain: Optional[str] = None,
                 query: Optional[str] = None, verbose: bool = False,
                 region: Optional[str] = None) -> list[dict]:
    """List all registered sources with routing-relevant facts.

    Each entry carries: name, the routing FACETS (kind / domains / regions / modes),
    needs_credentials, ``explicit_only`` (excluded from broad search, name it to include) plus
    ``explicit_only_reason`` (the WHY string, present only when excluded; the full catalog of
    exclusion reasons search's _meta.excluded_count no longer re-ships per query),
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
            # any excluded source stays exactly one penumbra_sources call away, never lost.
            **({"explicit_only_reason": _eo_reason} if _eo_reason else {}),
            # NEUTRAL fragility class (stable < keyed < scrape < walled): a routing/expectations
            # signal + the curator's repair-priority fact, NOT a verdict. Derived centrally.
            "stability": _derive_stability(adapter),
            # Legal-facing tier (free / keyed / walled / circumvention) — see _derive_access_tier.
            "access_tier": _derive_access_tier(adapter),
            "health": health,
            "health_as_of": as_of,
        }
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
        entry["backend"] = getattr(adapter, "backend", None) or fb.get("backend") or name
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
        from penumbra.core import relevance
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
    registry. Handed to the agent by penumbra_list_sources so domain= becomes a DISCOVERABLE router
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
    so the headline number matches what penumbra_list_sources reports."""
    with _registry_lock:
        adapters = list(_adapters.values())
    backends = set()
    for a in adapters:
        fb = _FACETS.get(a.name) or {}
        backends.add(getattr(a, "backend", None) or fb.get("backend") or a.name)
    return len(backends)


def health_check() -> dict[str, dict]:
    """Run health check on every registered adapter — bounded + concurrent, so a
    source whose own health_check blocks (no internal timeout) can never stall this."""
    with _registry_lock:  # snapshot under the lock; the slow probe runs on the copy outside it
        adapters_snapshot = list(_adapters.values())
    live = _probe_all_health(adapters_snapshot)
    return {name: {"healthy": bool(h), "status": m} for name, (h, m) in live.items()}


def fetch_url(url: str) -> Optional[Document]:
    """Try every adapter until one claims this URL — each attempt BOUNDED so no single
    adapter's ``fetch_url`` (e.g. a stalled twscrape / CDP) can hang ``penumbra_add_url``."""
    with _registry_lock:  # snapshot under the lock; iterate (slow per-adapter fetch) on the copy
        adapters_snapshot = list(_adapters.values())
    for adapter in adapters_snapshot:
        try:
            budget = getattr(adapter, "fetch_timeout", _FETCH_URL_TIMEOUT_S)
            ok, result = _run_bounded(lambda a=adapter: a.fetch_url(url), budget)
            if not ok:
                logger.warning("fetch_url: adapter %s exceeded %.0fs on %s — skipping",
                               adapter.name, budget, url)
                continue
            if result is not None:
                return result
        except Exception as exc:  # noqa: BLE001
            logger.debug("Adapter %s couldn't claim URL %s: %s", adapter.name, url, exc)
    # No adapter claimed this URL. LAST RESORT: a generic web read (plain fetch, then a Jina
    # headless render if the page is a thin JS-wall / SPA shell). This is the ONLY way the eye
    # reaches an arbitrary page outside its ~190 source adapters; before it, such a URL returned
    # matched=false. It runs only HERE (after every adapter declined), so the happy path adds
    # zero calls. Best-effort: any failure degrades to None (matched=false), the historical
    # contract. Lazy import keeps bs4 off the hot adapter path.
    try:
        from penumbra.core import web_fallback
        return web_fallback.read_via_fallback(url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_url: web_fallback raised on %s: %s", url, exc)
        return None


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
