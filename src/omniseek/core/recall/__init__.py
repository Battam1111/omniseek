"""OmniSeek's perception-memory index (``recall``): makes the ENUMERABLE sources STATEFUL.

Public surface for the rest of OmniSeek. See ``store`` (read/schema) and ``writer`` (write/ingest).

Design of record: [[eye-recall-index-blueprint-2026-06-14]]. OmniSeek stays a perception organ —
this sub-layer stores + retrieves + scores MECHANICALLY (FTS5 recall feeds the unchanged
``rank.merge_rank``); it never judges. It is an EYE sub-layer, not a 4th organ.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from omniseek.core.recall import embed, graph, store, writer  # noqa: F401
from omniseek.core.recall.store import as_of, doc_count, init, search, segment, vector_search  # noqa: F401
from omniseek.core.recall.writer import (  # noqa: F401
    ingest_produced, journal_health, last_write_ts, mark_run, mark_swept, maybe_ingest, start_backfill,
    start_journal_boot, start_writer,
    vec_embed_failures,
)

if TYPE_CHECKING:   # only for the _ingest_step return annotation; the runtime import stays lazy inside it
    from omniseek.core import fetcher

logger = logging.getLogger(__name__)

# S2 graceful-shutdown stop Event for the Path-C ingest loop: set on ASGI-lifespan shutdown so a
# stop wakes it out of its (6h) interval wait immediately (see omniseek.core.lifecycle). Additive: the
# loop behaves identically until this is set, which only happens on shutdown.
_STOP = threading.Event()


# FOREGROUND FIRST (2026-10-24). While the boot warmup below runs (the model import and
# load under embed._load_lock, then the vec and chunk matrix builds under store._vec_lock and
# _chunk_lock), a search does not run its vector arm: it would queue on those locks, and the search
# joins its recall arm with the WHOLE remaining wait budget, so the first search after every restart
# returned only at its deadline (the deploy check "眼的搜索": 20 s for wait_s=20 with cold model and
# matrices, under 1 s warm). The search answers with the lexical recall plus the live sources and says
# so in _meta.index (vector_skipped: "warming"); once the warmup is done the hybrid runs as before.
# The flag is set BEFORE the warmup thread starts (start_warm), so a search that lands between the
# thread's creation and its first line cannot reach the locks either; warm() itself clears it when it
# returns, whatever happened, so a failed warmup falls back to the lazy path it always had.
_WARMING = threading.Event()


def warming() -> bool:
    """True while the boot vector warmup runs: the vector arm would wait on its locks, so it is skipped."""
    return _WARMING.is_set()


def warm() -> None:
    """Load the vector model and build the matrices used by hybrid search.

    This is a boot-only warmup. It does not write recall state, alter request
    arguments, or participate in request concurrency: while it runs, ``warming()`` is True and
    ``hybrid`` skips its vector arm instead of waiting on the locks this holds. A failed warmup
    leaves the existing lazy, fail-open search behavior in place.
    """
    _WARMING.set()
    t0 = time.monotonic()
    con = None
    try:
        embed.warm()
        if not embed.available() or store._disabled:
            return
        con = store.connect()
        store._ensure_matrix(con)
        store._ensure_chunk_matrix(con)
    except Exception as exc:  # noqa: BLE001
        logger.debug("recall vector warmup skipped: %s", exc)
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        _WARMING.clear()
        logger.info("recall vector warmup done in %.1fs (searches skipped the vector arm until now)",
                    time.monotonic() - t0)


def start_warm() -> threading.Thread:
    """Start the boot warmup on its own (non-resident) thread, the flag already set (see _WARMING)."""
    _WARMING.set()
    t = threading.Thread(target=warm, name="recall-warm", daemon=True)
    t.start()
    return t


# ── what enters the corpus — an OPT-IN allow-list (frozen) ────────────────────────────────────
# Default: a source is NOT indexed. Excluded by design: query-keyed/walled (search-is-crawl, zero
# recall on novel queries), structured-field sources FTS would flatten (conference_deadlines /
# ai_residencies / gpu_pricing stay live), the account-rate-sensitive xiaohongshu, and csrankings
# (~20k thin who/where rows = index bloat for thin value; its in-memory filter is already fast).
_SINGLETONS = frozenset({
    "hf_daily_papers", "researcher_watch", "github_trending", "github_awesome_phd",
    "ml_collective", "transformer_circuits", "acl_anthology", "openreview", "llm_leaderboard",
    "hk_universities", "ajo", "mycareersfuture", "overseas_ai_jobs", "ircc_ee_rounds",
    "zhihu_users", "xiaoyuzhou", "youtube_channels", "feishu_jobs", "mokahr_ats", "bytedance_seed",
})
# Account-sensitive: indexed OPPORTUNISTICALLY via Path A (it rides prewarm's existing fetches —
# zero NEW logged-in traffic) but NEVER actively swept by Path C's ingest_loop.
_PATH_A_ONLY = frozenset({"zhihu_users"})

_indexable_cache: "frozenset[str] | None" = None


def _family_classes() -> tuple:
    """The config-family adapter classes whose instances are all enumerable (imported defensively
    so a future rename degrades gracefully rather than crashing the layer)."""
    classes = []
    for mod, name in (
        ("omniseek.core.sources.scrape._rss", "RSSAdapterBase"),
        ("omniseek.core.sources.api.org_watch_source", "_OrgWatchAdapter"),
        ("omniseek.core.sources.scrape.page_watch_source", "PageWatchAdapter"),
        ("omniseek.core.sources.scrape.news_scraper_source", "_ScrapeSite"),
    ):
        try:
            classes.append(getattr(__import__(mod, fromlist=[name]), name))
        except Exception as exc:  # noqa: BLE001
            logger.debug("recall: family class %s.%s unavailable: %s", mod, name, exc)
    return tuple(classes)


def indexable_set() -> "frozenset[str]":
    """The full set of indexable source NAMES (singletons + every config-family instance),
    computed once and cached. Changing it is a deliberate edit, so what enters the corpus is deliberate."""
    global _indexable_cache
    if _indexable_cache is None:
        names = set(_SINGLETONS)
        try:
            from omniseek.core import fetcher
            fams = _family_classes()
            if fams:
                for n in fetcher.all_adapter_names():
                    try:
                        if isinstance(fetcher.get_adapter(n), fams):
                            names.add(n)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("recall indexable_set family scan failed: %s", exc)
        _indexable_cache = frozenset(names)
    return _indexable_cache


def invalidate_indexable_cache() -> None:
    """Drop the cached indexable set so the NEXT indexable_set() recomputes from the live roster.

    Called by the curator live-apply lane after register_adapter_live of a RECALL/MONITOR-capable
    family row: without this the source is in fetcher._adapters (so search_many fans out to it) but
    indexable_set() returns the STALE frozenset, so recall.maybe_ingest never indexes the new source
    until a restart. Idempotent + fail-open: callers guard it (a recall import error must not fail an
    apply). The recompute is lazy (next indexable_set / indexable call), never eager."""
    global _indexable_cache
    _indexable_cache = None


def indexable(source: str) -> bool:
    return source in indexable_set()


def ingest_list() -> list[str]:
    """Sources Path C actively sweeps for COMPLETENESS — the full indexable set MINUS the
    account-sensitive Path-A-only ones. (NOT prewarm.warm_list, which omits rss/hf/acl/leaderboard/
    scrape families — that omission was the design's verified false premise.)"""
    return sorted(indexable_set() - _PATH_A_ONLY)


def _query_vector(query: str):
    """Embed a query, with a disk qvec cache (same query + model_version → sub-ms reuse, embed off
    the hot path; a model bump rotates MODEL_VERSION so the key rotates — no stale-vector risk).
    Returns a float32 vector or None (embedder disabled / failure)."""
    if not embed.available() or not (query or "").strip():
        return None
    from omniseek.core import cache
    key = cache.make_key("qvec", embed.MODEL_VERSION, query)
    cached = cache.get(key)
    if cached is not None:
        try:
            import numpy as _np
            return _np.asarray(cached, dtype=_np.float32)
        except Exception:  # noqa: BLE001
            pass
    v = embed.embed_query(query)
    if v is None:
        return None
    try:
        cache.set(key, v.tolist(), ttl=86400)
    except Exception:  # noqa: BLE001
        pass
    return v


# The RRF smoothing constant. One stored copy of a work, ranked first by both recall arms, gets
# 2 / (RRF_C + 1); only a work held in several stored copies near the top of an arm gets more (each
# copy adds its own 1 / (RRF_C + rank)). rank.merge_rank reads that bound.
RRF_C = 60


def _rrf_fuse(lex: list, vec: list, c: int = RRF_C) -> list:
    """Reciprocal-rank fusion of the lexical + vector recall lists — RANK-only (no score calibration,
    no learned weights → preserves THE RAZOR). Stamps metadata.recall_rrf (the fused prior) +
    recall_via ∈ {lexical, vector, both}. A vector-only doc still gets a real non-zero RRF — that
    prior is what lifts it in merge_rank instead of being floored to rel≈0 (the buried-hit fix)."""
    from omniseek.core import rank
    fused: dict = {}
    order: list = []

    def _fp(d):
        try:
            return rank.fingerprint(d)
        except Exception:  # noqa: BLE001
            return id(d)

    for i, d in enumerate(lex):
        k = _fp(d)
        if k not in fused:
            d.metadata = dict(d.metadata or {})
            d.metadata["recall_rrf"] = 0.0
            d.metadata["recall_via"] = "lexical"
            fused[k] = d
            order.append(k)
        fused[k].metadata["recall_rrf"] += 1.0 / (c + i + 1)
    for i, d in enumerate(vec):
        k = _fp(d)
        if k not in fused:
            d.metadata = dict(d.metadata or {})
            d.metadata["recall_rrf"] = 0.0
            d.metadata["recall_via"] = "vector"
            fused[k] = d
            order.append(k)
        else:
            fused[k].metadata["recall_via"] = "both"
        fused[k].metadata["recall_rrf"] += 1.0 / (c + i + 1)
    return [fused[k] for k in order]


def hybrid(query: str, k: int = 60, sources: "frozenset[str] | None" = None) -> tuple:
    """Lexical + vector recall, RRF-fused. Returns ``(docs, info)``. When the vector arm yields
    nothing (embedder disabled / empty query), returns the PLAIN lexical recall with NO rrf stamp,
    so downstream merge_rank is byte-identical to Phase 1. ``info`` = {lexical, vector, mode}.

    ``sources`` (Wave 2, audit 1.1): the source scope is passed through to BOTH arms (the FTS predicate
    and the pre-top-k vector mask). ``sources=None`` keeps today's behavior exactly for every caller."""
    # INSTRUMENTED 2026-08-17: under concurrency this arm silently misses its join deadline and
    # the search loses cross-lingual recall. A timeout at the caller cannot say WHERE the time went,
    # so each phase is timed here and reported in ``info``. Measurement only: the control flow and
    # every return value are unchanged.
    _t0 = time.perf_counter()
    lex = search(query, k, sources=sources)
    _t1 = time.perf_counter()
    if warming():   # the boot warmup holds the model / matrix locks: answer now, without the vector arm
        return lex, {"lexical": len(lex), "vector": 0, "mode": "lexical", "vector_skipped": "warming",
                     "lex_ms": round((_t1 - _t0) * 1000, 1)}
    qv = _query_vector(query)
    _t2 = time.perf_counter()
    # embed_ms below is wall time for the whole query-vector step. When it dwarfs the forward, the
    # query was queued behind another forward (the absorb path embeds documents through the same
    # lock), which is a scheduling fix, not a model fix. Absent when the vector came from cache.
    _emb = dict(embed.LAST_TIMING) if getattr(embed, "LAST_TIMING", None) else {}
    vec = vector_search(qv, k, sources=sources) if qv is not None else []
    _t3 = time.perf_counter()
    _phases = {
        "lex_ms": round((_t1 - _t0) * 1000, 1),
        "embed_ms": round((_t2 - _t1) * 1000, 1),
        "ann_ms": round((_t3 - _t2) * 1000, 1),
    }
    if _emb:
        _phases["embed_wait_ms"] = _emb.get("wait_ms")
        _phases["embed_fwd_ms"] = _emb.get("fwd_ms")
        _phases["embed_batch"] = _emb.get("n")
    if not vec:
        return lex, {"lexical": len(lex), "vector": 0, "mode": "lexical", **_phases}
    _fused = _rrf_fuse(lex, vec)
    _phases["fuse_ms"] = round((time.perf_counter() - _t3) * 1000, 1)
    return _fused, {"lexical": len(lex), "vector": len(vec), "mode": "hybrid", **_phases}


def _ingest_step(src: str, fresh: bool = True) -> "fetcher.AdapterOutcome":
    """One source's ingest sweep plus a CONDITIONAL watermark advance (Wave 3, 1.15).

    Fetch the source as a first-class ``AdapterOutcome``, then advance the still_live watermark ONLY on
    a sweep that both COMPLETED and was COMPLETE (the whole enumeration fit inside the page). A
    timed_out sweep, a completed-but-truncated one (a full page: the source has more behind it), or one
    a gate of OmniSeek held back in part (``oc.held``) HOLDS the watermark. Rationale (pinned by the spec): a held watermark makes the source's
    live_feed_silent_days grow so the curator LOOKS at the source (a safe, visible failure), whereas a
    falsely-advanced watermark silently flips every indexed doc not re-seen this sweep to
    still_live=False (store.py: still_live = last_seen >= ran_at), an invisible false roll-off.

    ``afetch_outcome`` never raises on an adapter fault (that is state="errored"), so the only thing that
    can raise here is an unknown-source ValueError or a mark_run fault; the loop still guards per source.
    Returns the outcome (for the loop's log line)."""
    from omniseek.core import _guard, fetcher, portal
    with _guard.initiator("收录"):   # carried into the coroutine with the rest of this thread's context
        oc = portal.run(fetcher.afetch_outcome, src, "", limit=50, fresh=fresh, deadline_s=90)
    # A sweep one of OmniSeek's gates (or an open host breaker) cut short is not complete, however few
    # documents it returned (2026-10-20): advancing the watermark over it would mark the
    # documents it never asked for as no longer live.
    if oc.state == "completed" and oc.complete and not oc.held:
        mark_run(src, len(oc.docs))
    else:
        held = ("held by a gate: " + oc.held["why"] if oc.held
                else oc.state + ("/truncated" if oc.state == "completed" else ""))
        logger.info("recall ingest watermark HELD for %s: %s (no still_live advance; the source will "
                    "look silent to the curator, the safe failure)", src, held)
    return oc


# When this process last swept each source (the persisted copy is store.swept_at, via writer.mark_swept;
# this one covers the moment before the writer has applied it).
_swept_here: dict[str, float] = {}


def _last_swept(src: str) -> "float | None":
    vals = [v for v in (_swept_here.get(src), store.swept_at(src)) if v]
    return max(vals) if vals else None


def _kept_warm() -> "frozenset[str]":
    """The sources the prewarmer keeps cached (prewarm.warm_list); empty when it cannot be read."""
    try:
        from omniseek.core import prewarm
        return frozenset(prewarm.warm_list())
    except Exception as exc:  # noqa: BLE001
        logger.debug("recall ingest: prewarm list unreadable, sweeping everything live: %s", exc)
        return frozenset()


def ingest_pass(interval_s: float, now: "float | None" = None) -> tuple[int, int, float]:
    """One pass over ingest_list(): sweep each source whose last sweep is ``interval_s`` old or more
    (or that was never swept), skip the rest. Returns (swept, skipped, seconds until the next one is
    due). The last sweep is persisted per source, so a restarted eye-http only makes up what is due
    instead of sweeping every source again (2026-10-20: each of the 18-22 restarts a day re-ran a
    full sweep; on 2026-10-18 to 10-20 that spent 86-88% of the keyed OpenAlex daily budget, ~8,700 of
    ~10,000 credits, before any person asked for anything). A source the prewarmer keeps cached is read
    from that cache (it is refreshed before it expires); a cold one is still fetched live."""
    now = time.time() if now is None else now
    warm = _kept_warm()
    swept = skipped = 0
    next_due = interval_s
    for src in ingest_list():
        if _STOP.is_set():
            break   # S2 (F1): stop PROMPTLY mid-sweep so a shutdown drains no late enqueue
                    # into the writer queue, and the sweep ends within the drain budget
                    # instead of running a full (6h) enumeration out.
        last = _last_swept(src)
        if last is not None and now - last < interval_s:
            skipped += 1
            next_due = min(next_due, last + interval_s - now)
            continue
        try:
            _ingest_step(src, fresh=src not in warm)
        except Exception as exc:  # noqa: BLE001 — one bad source never blocks the rest
            logger.debug("recall ingest_loop %s: %s", src, exc)
        _swept_here[src] = time.time()
        mark_swept(src)
        swept += 1
    return swept, skipped, max(0.0, next_due)


def ingest_loop(interval_s: float = 21600.0) -> None:
    """Phase-C completeness daemon: every source of ingest_list() is swept once per ``interval_s``
    (default 6h, growth is ~hundreds/day, not a firehose) by an empty-query high-limit fetch; a
    restart sweeps only the sources that are due (``ingest_pass``). The Path-A hook in the fetcher
    absorbs the returned docs into the index; each source's ingest watermark (the still_live
    reference) is advanced ONLY on a completed+complete sweep (Wave 3, 1.15): a timed-out or truncated
    fetch holds it, so a partial sweep never falsely rolls off still-live docs. See ``_ingest_step``
    for the per-source gate. Mirrors prewarm.warm_loop.

    S2: registers itself so a graceful shutdown can drain it, and waits on _STOP instead of sleeping
    so a stop wakes it out of the interval immediately. Purely additive: identical until _STOP is
    set (only on shutdown)."""
    from omniseek.core import lifecycle
    lifecycle.register_loop("recall-ingest", _STOP, threading.current_thread())
    while not _STOP.is_set():
        wait = interval_s
        try:
            swept, skipped, wait = ingest_pass(interval_s)
            logger.info("recall ingest_loop cycle done (%d sources, %d swept, %d not due yet, "
                        "%d docs indexed)", swept + skipped, swept, skipped, doc_count())
        except Exception as exc:  # noqa: BLE001 — never let the loop die
            logger.warning("recall ingest_loop cycle errored: %s", exc)
        _STOP.wait(max(wait, 1.0))
