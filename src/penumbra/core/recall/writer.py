"""Perception-memory index — the WRITE half: a single serialized writer (the ONLY writer) + the
ingest hooks.

SQLite is single-writer; the eye is highly concurrent (one uvicorn worker, a 64-wide fetch pool,
256 anyio threads) and runs SEPARATE cron processes (watchtower / digest / health). So ALL writes
funnel through ONE daemon thread owning ONE WAL connection, and writes are GATED to the eye-http
process via ``WRITES_ENABLED`` (set once in ``serve_http.main``). A cron process imports this module
fresh -> ``WRITES_ENABLED`` stays False -> the same ingest hook is a silent no-op there: no second
writer, no cross-process write contention (crons may only ever READ).

``maybe_ingest`` is the hook spliced into the fetcher return path (the true chokepoint — every
adapter's ``search`` returns ``list[Document]`` there, regardless of its internal cache
shape). It is ENQUEUE-ONLY and NEVER raises: a hook exception would break every search.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Optional

from penumbra.core.normalize import Document
from penumbra.core.recall import store

logger = logging.getLogger(__name__)

WRITES_ENABLED = False          # flipped True ONCE in serve_http.main(); cron processes leave it False
RETAIN_DAYS = 365               # roll-off age cap (immutable sources are never swept) — operator knob
_QUEUE_MAX = 4000
_DRAIN = 200                    # max items merged into one write transaction

_queue: "queue.Queue" = queue.Queue(maxsize=_QUEUE_MAX)
_writer_started = False
_start_lock = threading.Lock()
_last_write_ts: float = 0.0
_IMMUTABLE = frozenset({"acl_anthology", "openreview", "ircc_ee_rounds"})

# The P2.0 retrieval-anchored thin-memory lane IS a write tap, so it declares its vocabulary from day
# one like every other tap (vocabulary-by-minting, design section 3): it mints ``document`` nodes
# ONLY — no edge types (doc-doc same_as is a DERIVED view over docs.fp + external_ids, never a stored
# edge), no methods (nodes carry no edge method). Registered at import so ``declared_vocabulary``
# includes it whenever writer is loaded; guarded fail-open (a registration hiccup must never break
# the import of the writer that search depends on).
try:
    from penumbra.core.recall import graph as _graph
    _graph.register_mints("thin_memory", kinds=["document"], edge_types=[], methods=[])
except Exception as _exc:  # noqa: BLE001 — declaration is best-effort; never break writer import
    logger.debug("thin_memory register_mints skipped: %s", _exc)


def last_write_ts() -> float:
    return _last_write_ts


def maybe_ingest(docs) -> None:
    """Classify EVERY retrieved doc and enqueue it for the writer. NO-OP unless WRITES_ENABLED (so
    cron processes that hit the same fetcher chokepoint write nothing). ENQUEUE-ONLY and NEVER
    raises into the caller (the fetcher hot path) — a raised exception here would break every search.

    Two lanes, per the P2.0 retrieval-anchored thin-memory design:
      - ``indexable(source)`` -> the FULL row (title + content + vector), the existing path, enqueued
        as one batch (unchanged).
      - otherwise (query-keyed / walled sources recall deliberately excludes) -> a THIN document node
        (title + url + fp + external_ids ONLY, never content), so the graph's perception history is
        COMPLETE, not just the ~40 enumerable sources. Walled / circumvention-tier docs are SKIPPED
        by default (what an operator's logged-in account retrieved is operator privacy) unless the
        deployment profile opts in via ``walled.remember_retrievals``."""
    if not WRITES_ENABLED or store._disabled or not docs:
        return
    try:
        from penumbra.core import fetcher, profile
        from penumbra.core.recall import indexable
        remember_walled = profile.remember_walled_retrievals()
        full: list = []
        thin: list = []
        for d in docs:
            source = getattr(d, "source", None)
            if not source:
                continue
            if indexable(source):
                full.append(d)          # full-memory lane (existing path, unchanged)
            elif remember_walled or not fetcher.is_walled_source(source):
                thin.append(d)          # thin document-node lane (retrieval-anchored perception)
        if full:
            _enqueue(full)
        for d in thin:
            _enqueue(("__thin__", d))
    except Exception as exc:  # noqa: BLE001 — never propagate into the fetcher
        logger.debug("maybe_ingest swallowed: %s", exc)


def _enqueue(item) -> None:
    """Put one item (a full-doc batch, or a ``('__thin__', doc)`` marker) on the writer queue.
    Best-effort: on a full queue drop the OLDEST (Path C re-covers full rows; a missed thin row is
    re-observed on the next search) — search correctness is sacred, the index is not."""
    try:
        _queue.put_nowait(item)
    except queue.Full:
        try:
            _queue.get_nowait()
            _queue.put_nowait(item)
        except Exception:  # noqa: BLE001
            pass


def mark_run(source: str, count: int) -> None:
    """Stamp a source's ingest watermark (the still_live reference). Routed through the writer
    queue so the single-writer invariant holds. No-op unless writes are enabled."""
    if not WRITES_ENABLED or store._disabled:
        return
    try:
        _queue.put_nowait(("__mark__", source, count, time.time()))
    except Exception:  # noqa: BLE001
        pass


def enqueue_graph(nodes: list[dict], edges: list[dict]) -> None:
    """The GENERAL graph write verb (design section 6): the write taps (cartographer, enrich, ...)
    hand mechanical FACTS + labeled ALIGNMENT candidates here as node + edge dicts. Generalizes the
    P2.0 thin-document lane to entity nodes + M/A edges over the SAME single-writer daemon (same WAL
    connection, same ``WRITES_ENABLED`` gate) — zero new concurrency surface. ENQUEUE-ONLY and NEVER
    raises into the caller (a tap sits on an EXISTING data path; a raised exception here would break
    field_skeleton / enrich / search — the fail-open razor). NO-OP unless writes are enabled (a cron
    process leaves ``WRITES_ENABLED`` False, so its tap writes nothing: no second writer). The heavy
    work (validation, symmetric normalization, upserts) all happens on the writer thread in
    ``_apply_graph``; this side only classifies + enqueues. An empty batch is a cheap no-op."""
    if not WRITES_ENABLED or store._disabled:
        return
    if not nodes and not edges:
        return
    try:
        _enqueue(("__graph__", list(nodes or []), list(edges or [])))
    except Exception as exc:  # noqa: BLE001 — never propagate into the tap's data path
        logger.debug("enqueue_graph swallowed: %s", exc)


def start_writer() -> None:
    """Start the single writer daemon (idempotent). Called once from serve_http.main()."""
    global _writer_started
    with _start_lock:
        if _writer_started:
            return
        _writer_started = True
    threading.Thread(target=_writer_loop, name="recall-writer", daemon=True).start()


def _writer_loop() -> None:
    try:
        con = store.connect()
    except Exception as exc:  # noqa: BLE001
        logger.warning("recall writer connect failed -> no writes this run: %s", exc)
        return
    last_sweep = 0.0
    while True:
        try:
            item = _queue.get()
            items = [item]
            try:
                for _ in range(_DRAIN):
                    items.append(_queue.get_nowait())
            except queue.Empty:
                pass
            _apply(con, items)
            # periodic roll-off sweep (cheap; once/hour of wall-time between batches)
            if time.time() - last_sweep > 3600:
                _sweep(con)
                last_sweep = time.time()
            # P7 thin-title catch-up: ONLY at the idle point (queue drained), so a queued write is
            # never delayed. One bounded page per idle cycle; monotone convergence to zero backlog,
            # then a cheap empty query. Fail-open (never raises into the loop).
            if _queue.empty():
                try:
                    _thin_catchup(con)
                except Exception as exc:  # noqa: BLE001 — a catch-up failure never breaks the writer
                    logger.debug("recall thin catch-up cycle errored: %s", exc)
        except Exception as exc:  # noqa: BLE001 — never let the writer thread die
            logger.warning("recall writer cycle errored: %s", exc)
            try:
                con.rollback()
            except Exception:  # noqa: BLE001
                pass


_vec_fail = 0                  # persistent batch-embed failures (surfaced in health — a fail-open
                               # that's also fail-SILENT-forever is how the vector layer dies unseen)
_BACKFILL_PAGE = 64


def vec_embed_failures() -> int:
    return _vec_fail


def _apply(con, items) -> None:
    global _last_write_ts
    from penumbra.core import rank
    now = time.time()
    marks: list[tuple] = []
    doc_batches: list[list] = []
    thin_docs: list = []
    graph_batches: list[tuple] = []   # ('__graph__', nodes, edges) from enqueue_graph (the taps)
    do_backfill = False
    for it in items:
        if isinstance(it, tuple) and it:
            if it[0] == "__mark__":
                marks.append(it)
            elif it[0] == "__backfill__":
                do_backfill = True
            elif it[0] == "__thin__":
                thin_docs.append(it[1])   # a thin document-node upsert (title/url/fp only)
            elif it[0] == "__graph__":
                graph_batches.append(it)  # entity nodes + M/A edges (cartographer / enrich taps)
            else:
                doc_batches.append(it)
        else:
            doc_batches.append(it)
    con.execute("BEGIN")
    staged: list = []   # (rowid, raw_text) for docs that need (re-)embedding
    for batch in doc_batches:
        for d in batch:
            try:
                r = _upsert(con, rank, d, now)
                if r:
                    staged.append(r)
            except Exception as exc:  # noqa: BLE001 — one bad doc never aborts the batch
                logger.debug("recall upsert skipped one doc: %s", exc)
    thin_staged: list = []   # (node_id, title) for thin rows whose title needs embedding (P7)
    for d in thin_docs:
        try:
            r = _upsert_thin(con, rank, d, now)   # graph_nodes document node, NEVER content
            if r:
                thin_staged.append(r)
        except Exception as exc:  # noqa: BLE001 — one bad thin doc never aborts the batch
            logger.debug("recall thin upsert skipped one doc: %s", exc)
    for _tag, gnodes, gedges in graph_batches:
        try:
            _apply_graph(con, gnodes, gedges, now)   # entity nodes + M/A edges, same transaction
        except Exception as exc:  # noqa: BLE001 — one bad graph batch never aborts the write
            logger.debug("recall graph apply skipped one batch: %s", exc)
    for _tag, source, count, ts in marks:
        try:
            con.execute("INSERT OR REPLACE INTO ingest_runs(source, ran_at, doc_count) VALUES(?,?,?)",
                        (source, ts, count))
        except Exception:  # noqa: BLE001
            pass
    if staged:
        _embed_and_store(con, staged)   # ONE batched embed → vec rows, in this same transaction
    if thin_staged:
        _embed_and_store_thin(con, thin_staged)   # P7: thin TITLE vectors → vec_thin, same txn
    con.commit()
    if doc_batches:
        _last_write_ts = now
    if do_backfill:
        _backfill_page(con)             # one bounded page (own txn), re-enqueues itself if more remain


def _embed_and_store(con, staged) -> None:
    """Embed the staged ``(rowid, text)`` batch ONCE and write the vectors in the CURRENT transaction
    (atomic with the doc rows). FAIL-OPEN: embedder disabled or a forward failure → those docs stay
    lexical-only (a first-class state, never an error), the failure is counted for health."""
    global _vec_fail
    from penumbra.core.recall import embed
    if not embed.available():
        return
    vecs = embed.embed_passage([t for (_rid, t) in staged])
    if vecs is None or len(vecs) != len(staged):
        _vec_fail += 1
        logger.debug("recall: batch embed failed/short → %d docs lexical-only", len(staged))
        return
    mv, dim = embed.MODEL_VERSION, embed.DIM
    for (rid, _t), v in zip(staged, vecs):
        try:
            con.execute("INSERT OR REPLACE INTO vec(rowid, model_version, dim, v) VALUES(?,?,?,?)",
                        (rid, mv, dim, v.astype("float32").tobytes()))
        except Exception as exc:  # noqa: BLE001
            logger.debug("recall vec write skipped: %s", exc)


def _embed_and_store_thin(con, staged) -> None:
    """P7: embed a staged ``(node_id, title)`` batch of THIN rows ONCE and write the vectors into
    ``vec_thin`` in the CURRENT transaction (atomic with the thin node upserts). Mirrors
    ``_embed_and_store`` for the thin lane: FAIL-OPEN at the ROW level (embedder disabled or a forward
    failure -> those thin rows stay un-embedded, a first-class state; ``similar`` just does not see
    them until the catch-up embeds them; the coverage gauge is the visibility, not per-row logs). The
    TITLE is the passage (a thin row has no content); same MODEL_VERSION + embedder as ``vec`` so both
    live in ONE cosine space."""
    global _vec_fail
    from penumbra.core.recall import embed
    if not embed.available():
        return
    titles = [t for (_nid, t) in staged]
    vecs = embed.embed_passage(titles)
    if vecs is None or len(vecs) != len(staged):
        _vec_fail += 1
        logger.debug("recall: thin batch embed failed/short → %d thin rows un-embedded", len(staged))
        return
    mv, dim = embed.MODEL_VERSION, embed.DIM
    for (nid, _t), v in zip(staged, vecs):
        try:
            con.execute(
                "INSERT OR REPLACE INTO vec_thin(node_id, model_version, dim, v) VALUES(?,?,?,?)",
                (nid, mv, dim, v.astype("float32").tobytes()))
        except Exception as exc:  # noqa: BLE001
            logger.debug("recall vec_thin write skipped: %s", exc)


_THIN_CATCHUP_PAGE = 50   # thin rows the idle catch-up embeds per cycle (bounded; monotone convergence)


def _thin_catchup(con) -> int:
    """P7 self-converging catch-up (no script, no cron): embed up to ``_THIN_CATCHUP_PAGE`` thin rows
    that HAVE a title (graph_nodes.label) but NO ``vec_thin`` row for the current model_version, in one
    bounded page + own transaction. Called at the writer's IDLE point (after the queue drains — see
    ``_writer_loop``), so it NEVER delays a queued write (drain first, then catch up). Monotone
    convergence: committed rows drop out of the WHERE, so each cycle shrinks the backlog until it hits
    zero and then costs one cheap empty query. Returns the number embedded this cycle (0 when caught
    up or the embedder is unavailable). FAIL-OPEN — a catch-up failure never touches search."""
    from penumbra.core.recall import embed
    if not embed.available():
        return 0
    mv = embed.MODEL_VERSION
    try:
        rows = con.execute(
            "SELECT g.id, g.label FROM graph_nodes g "
            "LEFT JOIN vec_thin t ON t.node_id = g.id AND t.model_version = ? "
            "WHERE g.kind = 'document' AND g.label IS NOT NULL AND TRIM(g.label) != '' "
            "AND t.node_id IS NULL LIMIT ?", (mv, _THIN_CATCHUP_PAGE),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug("recall thin catch-up query failed: %s", exc)
        return 0
    if not rows:
        return 0   # caught up: every titled thin row has a current-model vec_thin vector
    staged = [(r[0], (r[1] or "").strip()) for r in rows if (r[1] or "").strip()]
    # LEGACY whitespace-only labels (a pre-fix nbsp title survives SQLite's ASCII-only TRIM but
    # Python-strips to empty): normalize them to NULL so they leave the WHERE clause permanently.
    # Without this the same unembeddable page is re-fetched every idle cycle and can starve real
    # titled rows out of the LIMIT. New rows store Python-stripped labels, so this arm only ever
    # touches legacy data, then goes quiet (true convergence, not a filtered spin).
    ghost = [r[0] for r in rows if not (r[1] or "").strip()]
    if ghost:
        try:
            marks = ",".join("?" * len(ghost))
            con.execute(f"UPDATE graph_nodes SET label = NULL WHERE id IN ({marks})", ghost)
            con.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("recall thin catch-up label normalization failed: %s", exc)
    if not staged:
        return 0
    try:
        con.execute("BEGIN")
        _embed_and_store_thin(con, staged)
        con.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("recall thin catch-up write failed: %s", exc)
        try:
            con.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0
    logger.info("recall thin catch-up: embedded a page of %d thin titles into vec_thin", len(staged))
    return len(staged)


def _backfill_page(con) -> None:
    """Embed ONE bounded page of docs missing a current-model_version vector, commit, then re-enqueue
    itself if more remain — so backfill interleaves with live ingest (one page per writer turn, never
    starves it) and is restart-safe (committed rows drop out of the WHERE, so re-running resumes)."""
    global _last_write_ts
    from penumbra.core.recall import embed
    if not embed.available():
        return
    mv = embed.MODEL_VERSION
    try:
        rows = con.execute(
            "SELECT d.rowid, d.title, d.content FROM docs d "
            "LEFT JOIN vec v ON v.rowid = d.rowid AND v.model_version = ? "
            "WHERE v.rowid IS NULL LIMIT ?", (mv, _BACKFILL_PAGE),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug("recall backfill query failed: %s", exc)
        return
    if not rows:
        logger.info("recall backfill complete (every doc has a %s vector)", mv)
        return
    staged = [(r[0], ((r[1] or "") + "\n" + (r[2] or "")).strip()[:2000]) for r in rows]
    con.execute("BEGIN")
    _embed_and_store(con, staged)
    con.commit()
    _last_write_ts = time.time()
    logger.info("recall backfill: embedded a page of %d docs; re-enqueueing", len(staged))
    try:
        _queue.put_nowait(("__backfill__",))
    except Exception:  # noqa: BLE001
        pass


def start_backfill() -> None:
    """Kick off the page-at-a-time backfill of vectors for the existing corpus (and after a model
    swap). No-op unless writes are enabled (eye-http process only)."""
    if not WRITES_ENABLED or store._disabled:
        return
    try:
        _queue.put_nowait(("__backfill__",))
    except Exception:  # noqa: BLE001
        pass


def _doc_json(d: Document) -> str:
    data = d.model_dump(mode="json")
    md = data.get("metadata")
    if isinstance(md, dict) and "raw" in md:  # metadata['raw'] is write-only (to_tool_dict drops it)
        data["metadata"] = {k: v for k, v in md.items() if k != "raw"}
    return json.dumps(data, ensure_ascii=False, default=str)


def _dt(date) -> Optional[str]:
    if date is None:
        return None
    try:
        return date.isoformat()
    except Exception:  # noqa: BLE001
        return str(date)


def _embed_text(d: Document) -> str:
    """RAW title+content for the SEMANTIC embedder (NOT docs.seg, which is the lexical bigram
    shadow — feeding that to a semantic model is garbage). Capped so a giant doc can't stall a batch."""
    return ((d.title or "") + "\n" + (d.content or "")).strip()[:2000]


_THIN_LABEL_CAP = 200               # title cap for a thin document node's label (design section 3)
# The external-id keys lifted into a thin node's attrs_json — the SAME names graph.py's id_eq
# derivation (``_derived_id_eq_edges`` / the graph_nodes id_eq arm) reads via json_extract, so a
# thin row's ids join to work entities identically to a full docs-table row.
_THIN_ID_KEYS = ("doi", "arxiv_id", "openalex_id")


def _upsert_node(con, nid: str, kind: str, label: Optional[str], attrs_json: Optional[str],
                 now: float) -> None:
    """The ONE graph_nodes upsert (extracted from the P2.0 thin lane so the thin-document lane and the
    generalized entity lane share ONE code path, not two). ``first_seen`` is IMMUTABLE on conflict:
    the INSERT-supplied first_seen is ``excluded.first_seen`` = the new ``now``, and the DO UPDATE
    clause simply does NOT touch first_seen, so the original survives (docs' immutable-first_seen /
    bumped-last_seen DNA). ``last_seen`` is bumped; label + attrs are refreshed last-write-wins. A
    re-observation (cache-hit re-mint) is an HONEST last_seen bump, never a duplicate row."""
    con.execute(
        "INSERT INTO graph_nodes(id, kind, label, attrs_json, first_seen, last_seen) "
        "VALUES(?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET label=excluded.label, attrs_json=excluded.attrs_json, "
        "last_seen=excluded.last_seen",
        (nid, kind, label, attrs_json, now, now),
    )


def _upsert_thin(con, rank, d: Document, now: float):
    """Upsert one THIN document node into ``graph_nodes`` (P2.0 retrieval-anchored perception).

    A thin row anchors a doc from a NON-indexed source in the graph with title + url + fp +
    external_ids ONLY — NEVER content (the graph never stores content). id = ``doc:{source}:{sid}``
    (graph.doc_node_id, the SAME scheme the views resolve), kind = ``document``, label = title
    (capped). ``first_seen`` is immutable on conflict (INSERT-supplied first_seen is ignored when the
    row exists); ``last_seen`` is bumped. An indexable doc NEVER reaches here (it gets a full docs
    row, which the union view already surfaces — no double node).

    P7: returns ``(node_id, title)`` when the thin row HAS a title (so the caller can batch-embed the
    title into vec_thin, letting ``similar`` see the whole perception history), else ``None`` (no
    title -> nothing to embed). The embed itself is off the hot path (writer daemon thread, batched in
    ``_apply``) and fail-open, so a thin row is upserted whether or not its title ever embeds."""
    source = getattr(d, "source", None)
    sid = str(getattr(d, "source_id", None) or getattr(d, "url", None) or "")
    if not source or not sid:
        return None
    from penumbra.core.recall.graph import doc_node_id
    nid = doc_node_id(source, sid)
    # STRIPPED before the cap: the embed staging + the catch-up SQL both key on "has a real
    # title", and an unstripped whitespace-only label (e.g. an nbsp from scraped HTML) would
    # pass the SQL's label != '' forever while the staging strip drops it -> a catch-up page
    # that never converges. One strip here keeps every downstream predicate in agreement.
    label = (d.title or "").strip()[:_THIN_LABEL_CAP] or None
    try:
        fp = rank.fingerprint(d)
    except Exception:  # noqa: BLE001
        fp = f"id:{source}:{sid}"
    attrs: dict = {"url": d.url, "fp": fp}
    md = d.metadata or {}
    for key in _THIN_ID_KEYS:                 # lift external ids under the SAME names id_eq reads
        val = md.get(key)
        if val:
            attrs[key] = val
    attrs_json = json.dumps(attrs, ensure_ascii=False, default=str)
    # The shared node upsert: first_seen preserved on conflict, last_seen bumped, label/attrs
    # refreshed (last write wins) — the P2.0 semantics, now one code path for thin + entity lanes.
    _upsert_node(con, nid, "document", label, attrs_json, now)
    # P7 title-embed staging: only when the row carries a title (the TITLE is embedded, not the url/fp).
    title = (d.title or "").strip()
    return (nid, title) if title else None


# ── Graph lanes (design sections 4-6): generalize the P2.0 thin lane to entity nodes + M/A edges ──
# The symmetric-relation set (design section 4): these types express an UNORDERED pair, so the writer
# stores each ONCE with src < dst (lexicographic). Otherwise (A,B) and (B,A) are distinct rows under
# the UNIQUE(src,dst,type,method) key and duplicates accrete. P2's minted types (cites/authored/
# about/published_in) are all DIRECTED, so this fires on none of them yet — but P3's ``coauthored``
# will, so the helper lands NOW (built with the model, not retrofitted).
_SYMMETRIC_EDGE_TYPES = frozenset({"same_as", "not_same_as", "coauthored", "conflicts"})
_LEGAL_TIERS = frozenset({"M", "A"})   # J is STRUCTURALLY excluded (the SQL CHECK; validate early too)


def _normalize_edge_endpoints(src: str, dst: str, etype: str) -> tuple[str, str]:
    """Symmetric-edge normalization (design section 4): for a SYMMETRIC type, return the endpoints
    reordered so ``src < dst`` (lexicographic) — so a reversed write (B,A) lands on the SAME row as
    (A,B) under the UNIQUE key instead of accreting a duplicate. DIRECTED types pass through as-is.
    P3's ``coauthored`` is the first real consumer; implemented now per the spec (the model is
    complete now even though assembly is incremental)."""
    if etype in _SYMMETRIC_EDGE_TYPES and dst < src:
        return dst, src
    return src, dst


def _upsert_edge(con, edge: dict, now: float) -> None:
    """Upsert one M/A edge into ``graph_edges`` on the UNIQUE(src, dst, type, method) key. ``tier``
    must be 'M' or 'A' (validated here AND enforced by the SQL CHECK); a bad/absent tier or a missing
    endpoint/type/method DROPS the item with a debug log (fail-open — a bad edge never aborts the
    batch). ``first_seen`` is immutable on conflict (the DO UPDATE omits it); ``last_seen`` is bumped;
    ``attrs`` is last-write-wins. Symmetric types are normalized to src < dst before the write."""
    src = (edge.get("src") or "").strip()
    dst = (edge.get("dst") or "").strip()
    etype = (edge.get("type") or "").strip()
    method = (edge.get("method") or "").strip()
    tier = (edge.get("tier") or "").strip()
    if not src or not dst or not etype or not method:
        logger.debug("graph edge dropped (missing src/dst/type/method): %r", edge)
        return
    if tier not in _LEGAL_TIERS:
        logger.debug("graph edge dropped (illegal tier %r; only M/A may enter the eye's store): %r",
                     tier, edge)
        return
    src, dst = _normalize_edge_endpoints(src, dst, etype)
    attrs = edge.get("attrs")
    attrs_json = json.dumps(attrs, ensure_ascii=False, default=str) if attrs else None
    con.execute(
        "INSERT INTO graph_edges(src, dst, type, tier, method, attrs_json, first_seen, last_seen) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(src, dst, type, method) DO UPDATE SET attrs_json=excluded.attrs_json, "
        "last_seen=excluded.last_seen",
        (src, dst, etype, tier, method, attrs_json, now, now),
    )


def _apply_graph(con, nodes: list, edges: list, now: float) -> None:
    """Apply a batch of graph nodes + edges in the CALLER's transaction (``_apply`` opened one BEGIN
    for the whole write, so node rows, edge rows, doc rows and vectors all commit atomically). Each
    node/edge is upserted defensively: one bad item is dropped + logged, never aborting the batch
    (the recall-ingest fail-open discipline). Nodes carry ``{id, kind, label, attrs}``; edges carry
    ``{src, dst, type, tier, method, attrs}``."""
    for n in (nodes or []):
        try:
            nid = (n.get("id") or "").strip()
            kind = (n.get("kind") or "").strip()
            if not nid or not kind:
                logger.debug("graph node dropped (missing id/kind): %r", n)
                continue
            label = n.get("label")
            attrs = n.get("attrs")
            attrs_json = json.dumps(attrs, ensure_ascii=False, default=str) if attrs else None
            _upsert_node(con, nid, kind, label, attrs_json, now)
        except Exception as exc:  # noqa: BLE001 — one bad node never aborts the batch
            logger.debug("graph node upsert skipped one: %s", exc)
    for e in (edges or []):
        try:
            _upsert_edge(con, e, now)
        except Exception as exc:  # noqa: BLE001 — one bad edge never aborts the batch
            logger.debug("graph edge upsert skipped one: %s", exc)


def _upsert(con, rank, d: Document, now: float):
    """Upsert one doc. Returns ``(rowid, raw_text)`` when the doc NEEDS (re-)embedding (new doc, or
    content changed), else ``None`` (unchanged → just a last_seen bump, no re-embed)."""
    source = getattr(d, "source", None)
    sid = str(getattr(d, "source_id", None) or getattr(d, "url", None) or "")
    if not source or not sid:
        return None
    seg = store.segment_doc(d)              # title + content + TAGS (SEG_VERSION 2)
    sver = store.SEG_VERSION
    chash = store.content_hash(d)
    try:
        fp = rank.fingerprint(d)
    except Exception:  # noqa: BLE001
        fp = f"id:{source}:{sid}"
    djson = _doc_json(d)
    row = con.execute(
        "SELECT rowid, content_hash, version, seg, seg_version FROM docs WHERE source = ? AND source_id = ?",
        (source, sid),
    ).fetchone()
    if row:
        rowid, old_hash, ver, old_seg, old_sver = row
        # A tokenizer/segment-version bump forces a RE-SEGMENT of an existing doc even when its
        # content is byte-identical: a stale seg_version means the seg column was derived by an
        # older rule (e.g. without tags), so re-index it. (old_sver may be NULL on a legacy row.)
        if chash == old_hash and (old_sver or 1) >= sver:
            con.execute("UPDATE docs SET last_seen = ? WHERE rowid = ?", (now, rowid))
            return None
        # content (or seg derivation) changed -> version bump + rewrite row, the external-content FTS
        # entry, AND drop the now-stale vector (a failed re-embed leaves NO vector, lexical-only).
        con.execute("INSERT INTO fts(fts, rowid, seg) VALUES('delete', ?, ?)", (rowid, old_seg))
        con.execute(
            "UPDATE docs SET fp=?, url=?, title=?, content=?, author=?, date=?, score=?, doc_json=?, "
            "seg=?, seg_version=?, content_hash=?, last_seen=?, version=? WHERE rowid=?",
            (fp, d.url, d.title, d.content, d.author, _dt(d.date), d.attention_value(), djson, seg, sver, chash,
             now, (ver or 1) + 1, rowid),
        )
        con.execute("INSERT INTO fts(rowid, seg) VALUES(?, ?)", (rowid, seg))
        con.execute("DELETE FROM vec WHERE rowid = ?", (rowid,))
        return (rowid, _embed_text(d))
    cur = con.execute(
        "INSERT INTO docs(source, source_id, fp, url, title, content, author, date, score, "
        "doc_json, seg, seg_version, content_hash, first_seen, last_seen, version, immutable) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
        (source, sid, fp, d.url, d.title, d.content, d.author, _dt(d.date), d.attention_value(), djson, seg,
         sver, chash, now, now, 1 if source in _IMMUTABLE else 0),
    )
    con.execute("INSERT INTO fts(rowid, seg) VALUES(?, ?)", (cur.lastrowid, seg))
    return (cur.lastrowid, _embed_text(d))


def _sweep(con) -> None:
    """Mechanical roll-off: drop rows older than RETAIN_DAYS. Keeps the external-content FTS in sync
    via the 'delete' command. This is the only deletion path.

    Two lanes with the SAME cutoff: (1) full docs rows (never immutable ones); (2) THIN document
    nodes in graph_nodes (``kind='document'``) — a thin doc's perception memory rolls off on the
    same clock as content memory. NON-document graph_nodes kinds (entities: work / person / ...) are
    EXEMPT: entities persist indefinitely (design's lifecycle section — small rows, relation memory
    may outlive content memory)."""
    cutoff = time.time() - RETAIN_DAYS * 86400
    try:
        stale = con.execute(
            "SELECT rowid, seg FROM docs WHERE immutable = 0 AND last_seen < ?", (cutoff,)
        ).fetchall()
        stale_thin = con.execute(
            "SELECT id FROM graph_nodes WHERE kind = 'document' AND last_seen < ?", (cutoff,)
        ).fetchall()
        if not stale and not stale_thin:
            return
        con.execute("BEGIN")
        for rowid, seg in stale:
            con.execute("INSERT INTO fts(fts, rowid, seg) VALUES('delete', ?, ?)", (rowid, seg))
            con.execute("DELETE FROM vec WHERE rowid = ?", (rowid,))
            con.execute("DELETE FROM docs WHERE rowid = ?", (rowid,))
        for (nid,) in stale_thin:
            con.execute("DELETE FROM vec_thin WHERE node_id = ?", (nid,))   # P7: drop its title vector
            con.execute("DELETE FROM graph_nodes WHERE id = ?", (nid,))
        con.commit()
        logger.info("recall roll-off swept %d docs + %d thin document nodes older than %dd",
                    len(stale), len(stale_thin), RETAIN_DAYS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("recall sweep skipped: %s", exc)
        try:
            con.rollback()
        except Exception:  # noqa: BLE001
            pass
