"""Curator P2, the yield tap: a FAIL-OPEN, single-writer, WRITES_ENABLED-gated mechanical hook at
the return of ``search_ranked`` that records, per source, its MARGINAL contribution to real top-K
results. It records FACTS ONLY (integer counters + timestamps), NEVER a verdict. A later audit AGENT
(P3) reads the accumulated facts and decides KEEP / WATCH / PRUNE; this module decides nothing.

THE RAZOR holds here exactly as in recall/writer.py and curator/candidates.py: the eye records /
counts / measures; every judgment is the agent's. There is NO field named score / verdict /
redundant / dead / low_yield / passes / recommend anywhere in the durable store; no ratio, no
threshold. The ratio sole/appeared and any "redundant"/"low-yield" call is the audit agent's at P3.

Concurrency (mirrors recall/writer.py, the proven primitive): ``search_ranked`` runs across
uvicorn's anyio threads + the 64-wide fetch pool. ``record_search`` mutates NOTHING shared inline:
it builds an immutable fact bundle (pure reads over ``ranked``, O(K)) and ``put_nowait``s it. ONE
daemon drain thread (``yield-tap-writer``) owns the counters dict + ``yield.json`` exclusively (the
only mutator and the only writer) so the folds need no lock (one mutator); ``_LOCK`` guards only
the atomic write against a future P3 reader. Drop-oldest on ``queue.Full``. Batched debounced flush.

Gating (REUSES recall.writer.WRITES_ENABLED, one source of truth, never a second "am I live"
flag): ``record_search`` no-ops when WRITES_ENABLED is False, and the drain thread is started ONLY
from ``serve_http.main`` under that guard. A cron/smoke process (watchtower / digest / health,
digest DOES call search_ranked) imports fresh -> WRITES_ENABLED False -> the tap is a silent no-op
AND no drain thread runs, so even a stray enqueue is drained by nobody and fills+drops. The
``record_yield=False`` kwarg on search_ranked closes the in-process synthetic-search hole
(cache-only pickups via cache_only=True; any future prewarm-driven search): process-gating catches cron,
intent-gating catches in-process synthetic traffic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
from pathlib import Path

from penumbra.core import cache

logger = logging.getLogger(__name__)

# Runtime state lives under ~/.polaris/state/curator/ (same tree as candidates.json + the
# health-watchdog state; survives redeploys, rides the weekly state-backup launchd, keeps the
# read-only deploy tree pristine). Created on first flush.
STATE_DIR = Path.home() / ".polaris" / "state" / "curator"
YIELD_PATH = STATE_DIR / "yield.json"

_VERSION = 1
_QUEUE_MAX = 4000
_FLUSH_ITEMS = 50          # flush when >= this many bundles are queued ...
_FLUSH_SECONDS = 30.0      # ... OR this much wall-time elapsed since the last flush
_RANK_BUCKETS = ("0-2", "3-5", "6-9", "10+")

_queue: "queue.Queue" = queue.Queue(maxsize=_QUEUE_MAX)
_writer_started = False
_start_lock = threading.Lock()
# Guards ONLY the atomic write of yield.json (so a future P3/curator reader can never read a torn
# file). The counters dict needs no lock: the single drain thread is its only mutator.
_LOCK = threading.Lock()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _rank_bucket(i: int) -> str:
    if i <= 2:
        return "0-2"
    if i <= 5:
        return "3-5"
    if i <= 9:
        return "6-9"
    return "10+"


def _build_bundle(query: str, ranked, results, meta) -> dict:
    """Pure, O(K) read over ``ranked`` on the SEARCH thread → an immutable fact bundle. Touches
    NOTHING shared, performs no I/O, no network, no embed, and never mutates ``ranked`` or its docs.
    Returns per-source integer deltas + the per-search facts the drain folds into the counters."""
    timed_out = set(meta.get("timed_out") or []) if isinstance(meta, dict) else set()
    errored = set(meta.get("errored") or []) if isinstance(meta, dict) else set()

    # Per-source per-search accumulators (deltas to fold). Defaults via plain dict.get in the drain.
    topk: dict[str, int] = {}
    from_index_only: dict[str, int] = {}
    sole: dict[str, int] = {}
    title_soft: dict[str, int] = {}
    best_rank: dict[str, int] = {}                 # smallest live rank index seen this search
    hist: dict[str, dict[str, int]] = {}           # source -> {bucket: count}
    present_live: set[str] = set()                 # sources with >=1 LIVE survivor this search

    def _bump(d: dict, k: str, n: int = 1) -> None:
        d[k] = d.get(k, 0) + n

    for i, d in enumerate(ranked or []):
        # Defensive reads: a malformed doc (None / str / int / garbage metadata) must never raise;
        # the tap is on the search hot path. Skip anything without a usable .source.
        source = getattr(d, "source", None)
        if not isinstance(source, str) or not source:
            continue
        md = getattr(d, "metadata", None)
        if not isinstance(md, dict):
            md = {}
        also = md.get("also_in")
        also_set = {s for s in also if isinstance(s, str)} if isinstance(also, list) else set()
        present = {source} | also_set                       # everyone who surfaced this survivor
        live = md.get("live_sources")
        if isinstance(live, list):
            # rank.dedup ALWAYS stamps live_sources (a list) on every survivor. An EXPLICIT empty
            # list means index-only this run (the feed went quiet, recall carried it); it must NOT
            # fall back to live, or a silent feed launders itself alive (Attack 1).
            live_present = {s for s in live if isinstance(s, str)}
        else:
            # field absent/garbage (a pre-P2 doc or a malformed fixture) → fall back to the
            # survivor's own source (prune-safe: treats it as live rather than inventing index-only).
            live_present = {source}
        merge_basis = md.get("merge_basis")
        if merge_basis not in ("title", "id"):
            merge_basis = "id"
        # _index is the memory layer, not a feed: it gets NO per-source credit (drop it everywhere).
        present.discard("_index")
        live_present.discard("_index")

        for s in present:
            if s in live_present:
                _bump(topk, s)
                present_live.add(s)
                b = hist.setdefault(s, {})
                _bump(b, _rank_bucket(i))
                if s not in best_rank or i < best_rank[s]:
                    best_rank[s] = i
            else:
                # present but NOT live this run → recall carried it; the feed was quiet (Attack 1).
                _bump(from_index_only, s)

        # sole_contributions: unique-AND-live AND id-grade (a title-only merge is WEAK corroboration
        # and must NEVER strip a sole credit: record title_soft_coappearances instead; error forced
        # toward KEEP, the prune-safe direction).
        if merge_basis == "title" and also_set:
            for s in present:
                if s in live_present:
                    _bump(title_soft, s)
        elif len(present) == 1:
            (only_src,) = tuple(present)
            if only_src in live_present:
                _bump(sole, only_src)

    return {
        "as_of": time.time(),
        "qfp": hashlib.sha1((query or "").encode("utf-8")).hexdigest()[:12],
        "k": len(ranked or []),
        "topk": topk,
        "from_index_only": from_index_only,
        "sole": sole,
        "title_soft": title_soft,
        "present_live": sorted(present_live),
        "best_rank": best_rank,
        "hist": hist,
        "timed_out": sorted(s for s in timed_out if isinstance(s, str)),
        "errored": sorted(s for s in errored if isinstance(s, str)),
    }


def record_search(query: str, ranked, results, meta) -> None:
    """Hot-path hook spliced at the return of ``search_ranked``. NO-OP unless WRITES_ENABLED (so
    cron/smoke processes that hit the same chokepoint write nothing). ENQUEUE-ONLY: it builds an
    immutable fact bundle and ``put_nowait``s it: it mutates nothing shared inline, performs no
    I/O, and NEVER raises into the caller (an exception here would break every search)."""
    try:
        from penumbra.core.recall import writer as _recall_writer
        if not _recall_writer.WRITES_ENABLED:
            return
        bundle = _build_bundle(query, ranked, results, meta)
        try:
            _queue.put_nowait(bundle)
        except queue.Full:
            # yield facts are best-effort statistics; search correctness is sacred. Drop oldest.
            try:
                _queue.get_nowait()
                _queue.put_nowait(bundle)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 never propagate into the fetcher hot path
        logger.debug("yield_tap.record_search swallowed: %s", exc)


# ── durable store (atomic, lock-guarded, corrupt-tolerant): mirrors candidates.py ──────────────
def _load_all() -> dict:
    """Read yield.json. Tolerates missing/corrupt -> fresh {} (mirroring candidates._load_all),
    logged, never raised."""
    if not YIELD_PATH.exists():
        return {"version": _VERSION, "total_searches_observed": 0, "updated_at": None, "sources": {}}
    try:
        data = json.loads(YIELD_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("curator yield.json unreadable (%s) -> treating as empty {}", exc)
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        return {"version": _VERSION, "total_searches_observed": 0, "updated_at": None, "sources": {}}
    data.setdefault("version", _VERSION)
    data.setdefault("total_searches_observed", 0)
    data.setdefault("updated_at", None)
    return data


def _save_all(state: dict) -> None:
    """Atomic write of the WHOLE counter state via cache._atomic_write_text (tmp-in-same-dir +
    os.replace). MUST be called under _LOCK. Bounded by source cardinality (~143 rows), never grows
    with traffic (only counters increment); no raw query strings, no event log."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cache._atomic_write_text(
        YIELD_PATH, json.dumps(state, default=str, ensure_ascii=False, indent=1))


def _src_row(sources: dict, name: str) -> dict:
    row = sources.get(name)
    if row is None:
        row = {
            "topk_appearances": 0,
            "from_index_only_appearances": 0,
            "sole_contributions": 0,
            "title_soft_coappearances": 0,
            "searches_present": 0,
            "searches_timed_out": 0,
            "searches_errored": 0,
            "best_rank_seen": None,
            "rank_histogram": {b: 0 for b in _RANK_BUCKETS},
            "first_recorded_at": _now_iso(),
            "last_topk_at": None,
        }
        sources[name] = row
    # tolerate a hand-edited / older row missing the histogram shape
    rh = row.get("rank_histogram")
    if not isinstance(rh, dict):
        row["rank_histogram"] = {b: 0 for b in _RANK_BUCKETS}
    else:
        for b in _RANK_BUCKETS:
            rh.setdefault(b, 0)
    return row


def _fold(state: dict, bundle: dict) -> None:
    """Fold ONE fact bundle into the in-memory counter state. Runs ONLY on the drain thread (single
    mutator → no lock needed here). Pure integer accumulation: no ratio, no threshold, no verdict."""
    sources = state["sources"]
    state["total_searches_observed"] = int(state.get("total_searches_observed", 0)) + 1
    now_iso = _now_iso()

    for name, n in (bundle.get("topk") or {}).items():
        row = _src_row(sources, name)
        row["topk_appearances"] += int(n)
        row["last_topk_at"] = now_iso
    for name, n in (bundle.get("from_index_only") or {}).items():
        _src_row(sources, name)["from_index_only_appearances"] += int(n)
    for name, n in (bundle.get("sole") or {}).items():
        _src_row(sources, name)["sole_contributions"] += int(n)
    for name, n in (bundle.get("title_soft") or {}).items():
        _src_row(sources, name)["title_soft_coappearances"] += int(n)
    for name in (bundle.get("present_live") or []):
        _src_row(sources, name)["searches_present"] += 1
    for name in (bundle.get("timed_out") or []):
        _src_row(sources, name)["searches_timed_out"] += 1
    for name in (bundle.get("errored") or []):
        _src_row(sources, name)["searches_errored"] += 1
    for name, rank_i in (bundle.get("best_rank") or {}).items():
        row = _src_row(sources, name)
        cur = row.get("best_rank_seen")
        if cur is None or int(rank_i) < int(cur):
            row["best_rank_seen"] = int(rank_i)
    for name, buckets in (bundle.get("hist") or {}).items():
        rh = _src_row(sources, name)["rank_histogram"]
        for b, c in (buckets or {}).items():
            if b in rh:
                rh[b] += int(c)


def start_writer() -> None:
    """Start the single drain daemon (idempotent). Called ONCE from serve_http.main() under the
    WRITES_ENABLED guard, never from a cron/smoke import."""
    global _writer_started
    with _start_lock:
        if _writer_started:
            return
        _writer_started = True
    threading.Thread(target=_drain_loop, name="yield-tap-writer", daemon=True).start()


def _drain_loop() -> None:
    """Forever: own the counter state + yield.json exclusively. Batch-flush on >=_FLUSH_ITEMS
    queued OR >=_FLUSH_SECONDS elapsed, each flush one atomic _save_all. A crash loses at most the
    un-flushed window (acceptable for advisory statistics). Never dies (every cycle guarded)."""
    state = _load_all()
    pending = 0
    last_flush = time.monotonic()
    while True:
        try:
            timeout = max(0.5, _FLUSH_SECONDS - (time.monotonic() - last_flush))
            try:
                bundle = _queue.get(timeout=timeout)
                _fold(state, bundle)
                pending += 1
                # opportunistically drain whatever else is queued right now (one fold per item)
                try:
                    while pending < _FLUSH_ITEMS:
                        _fold(state, _queue.get_nowait())
                        pending += 1
                except queue.Empty:
                    pass
            except queue.Empty:
                pass  # idle window elapsed → fall through to the time-based flush check
            due = pending >= _FLUSH_ITEMS or (pending > 0 and time.monotonic() - last_flush >= _FLUSH_SECONDS)
            if due:
                state["updated_at"] = _now_iso()
                with _LOCK:
                    _save_all(state)
                pending = 0
                last_flush = time.monotonic()
        except Exception as exc:  # noqa: BLE001 never let the drain thread die
            logger.warning("yield_tap drain cycle errored: %s", exc)
            time.sleep(1.0)
