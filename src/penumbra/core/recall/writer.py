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
    do_backfill = False
    for it in items:
        if isinstance(it, tuple) and it:
            if it[0] == "__mark__":
                marks.append(it)
            elif it[0] == "__backfill__":
                do_backfill = True
            elif it[0] == "__thin__":
                thin_docs.append(it[1])   # a thin document-node upsert (title/url/fp only)
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
    for d in thin_docs:
        try:
            _upsert_thin(con, rank, d, now)   # graph_nodes document node, NEVER content
        except Exception as exc:  # noqa: BLE001 — one bad thin doc never aborts the batch
            logger.debug("recall thin upsert skipped one doc: %s", exc)
    for _tag, source, count, ts in marks:
        try:
            con.execute("INSERT OR REPLACE INTO ingest_runs(source, ran_at, doc_count) VALUES(?,?,?)",
                        (source, ts, count))
        except Exception:  # noqa: BLE001
            pass
    if staged:
        _embed_and_store(con, staged)   # ONE batched embed → vec rows, in this same transaction
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


def _upsert_thin(con, rank, d: Document, now: float) -> None:
    """Upsert one THIN document node into ``graph_nodes`` (P2.0 retrieval-anchored perception).

    A thin row anchors a doc from a NON-indexed source in the graph with title + url + fp +
    external_ids ONLY — NEVER content (the graph never stores content). id = ``doc:{source}:{sid}``
    (graph.doc_node_id, the SAME scheme the views resolve), kind = ``document``, label = title
    (capped). ``first_seen`` is immutable on conflict (INSERT-supplied first_seen is ignored when the
    row exists); ``last_seen`` is bumped. An indexable doc NEVER reaches here (it gets a full docs
    row, which the union view already surfaces — no double node)."""
    source = getattr(d, "source", None)
    sid = str(getattr(d, "source_id", None) or getattr(d, "url", None) or "")
    if not source or not sid:
        return
    from penumbra.core.recall.graph import doc_node_id
    nid = doc_node_id(source, sid)
    label = (d.title or "")[:_THIN_LABEL_CAP] or None
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
    # first_seen preserved on conflict, last_seen bumped; label/attrs refreshed (last write wins).
    # The COALESCE keeps the ORIGINAL first_seen (excluded.first_seen is the new `now`, ignored when
    # a row already exists), mirroring docs' immutable-first_seen / bumped-last_seen semantics.
    con.execute(
        "INSERT INTO graph_nodes(id, kind, label, attrs_json, first_seen, last_seen) "
        "VALUES(?, 'document', ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET label=excluded.label, attrs_json=excluded.attrs_json, "
        "last_seen=excluded.last_seen",
        (nid, label, attrs_json, now, now),
    )


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
