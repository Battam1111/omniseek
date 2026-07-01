"""The eye's perception-memory index (``recall``): makes the ENUMERABLE sources STATEFUL.

Public surface for the rest of the eye. See ``store`` (read/schema) and ``writer`` (write/ingest).

Design of record: [[eye-recall-index-blueprint-2026-06-14]]. The eye stays a perception organ —
this sub-layer stores + retrieves + scores MECHANICALLY (FTS5 recall feeds the unchanged
``rank.merge_rank``); it never judges. It is an EYE sub-layer, not a 4th organ.
"""

from __future__ import annotations

import logging
import time

from penumbra.core.recall import embed, store, writer  # noqa: F401
from penumbra.core.recall.store import as_of, doc_count, init, search, segment, vector_search  # noqa: F401
from penumbra.core.recall.writer import (  # noqa: F401
    last_write_ts, mark_run, maybe_ingest, start_backfill, start_writer, vec_embed_failures,
)

logger = logging.getLogger(__name__)

# ── what enters the corpus — an OPT-IN allow-list (smoke-frozen) ──────────────────────────────
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
        ("penumbra.core.sources.scrape._rss", "RSSAdapterBase"),
        ("penumbra.core.sources.api.org_watch_source", "_OrgWatchAdapter"),
        ("penumbra.core.sources.scrape.page_watch_source", "PageWatchAdapter"),
        ("penumbra.core.sources.scrape.news_scraper_source", "_ScrapeSite"),
    ):
        try:
            classes.append(getattr(__import__(mod, fromlist=[name]), name))
        except Exception as exc:  # noqa: BLE001
            logger.debug("recall: family class %s.%s unavailable: %s", mod, name, exc)
    return tuple(classes)


def indexable_set() -> "frozenset[str]":
    """The full set of indexable source NAMES (singletons + every config-family instance),
    computed once and cached. Smoke freezes this so what enters the corpus is deliberate."""
    global _indexable_cache
    if _indexable_cache is None:
        names = set(_SINGLETONS)
        try:
            from penumbra.core import fetcher
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
    from penumbra.core import cache
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


def _rrf_fuse(lex: list, vec: list, c: int = 60) -> list:
    """Reciprocal-rank fusion of the lexical + vector recall lists — RANK-only (no score calibration,
    no learned weights → preserves THE RAZOR). Stamps metadata.recall_rrf (the fused prior) +
    recall_via ∈ {lexical, vector, both}. A vector-only doc still gets a real non-zero RRF — that
    prior is what lifts it in merge_rank instead of being floored to rel≈0 (the buried-hit fix)."""
    from penumbra.core import rank
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


def hybrid(query: str, k: int = 60) -> tuple:
    """Lexical + vector recall, RRF-fused. Returns ``(docs, info)``. When the vector arm yields
    nothing (embedder disabled / empty query), returns the PLAIN lexical recall with NO rrf stamp,
    so downstream merge_rank is byte-identical to Phase 1. ``info`` = {lexical, vector, mode}."""
    lex = search(query, k)
    qv = _query_vector(query)
    vec = vector_search(qv, k) if qv is not None else []
    if not vec:
        return lex, {"lexical": len(lex), "vector": 0, "mode": "lexical"}
    return _rrf_fuse(lex, vec), {"lexical": len(lex), "vector": len(vec), "mode": "hybrid"}


def ingest_loop(interval_s: float = 21600.0) -> None:
    """Phase-C completeness daemon: on entry then every ``interval_s`` (default 6h — growth is
    ~hundreds/day, not a firehose), drive an empty-query high-limit fetch of every ingest_list()
    source. The Path-A hook in the fetcher absorbs the returned docs into the index; we additionally
    stamp each source's ingest watermark (the still_live reference). Mirrors prewarm.warm_loop."""
    from penumbra.core import fetcher
    while True:
        try:
            for src in ingest_list():
                try:
                    docs = fetcher.fetch_one(src, "", limit=50, fresh=True, deadline_s=90)
                    mark_run(src, len(docs))
                except Exception as exc:  # noqa: BLE001 — one bad source never blocks the rest
                    logger.debug("recall ingest_loop %s: %s", src, exc)
            logger.info("recall ingest_loop cycle done (%d sources, %d docs indexed)",
                        len(ingest_list()), doc_count())
        except Exception as exc:  # noqa: BLE001 — never let the loop die
            logger.warning("recall ingest_loop cycle errored: %s", exc)
        time.sleep(interval_s)
