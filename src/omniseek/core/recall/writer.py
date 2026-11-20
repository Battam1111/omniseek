"""Perception-memory index — the WRITE half: a single serialized writer (the ONLY writer) + the
ingest hooks.

SQLite is single-writer; OmniSeek is highly concurrent (one uvicorn worker, a 64-wide fetch pool,
256 anyio threads) and runs SEPARATE cron processes (watchtower / digest / health). So ALL writes
funnel through ONE daemon thread owning ONE WAL connection, and writes are GATED to OmniSeek-http
process via ``WRITES_ENABLED`` (set once in ``serve_http.main``). A cron process imports this module
fresh -> ``WRITES_ENABLED`` stays False -> the same ingest hook is a silent no-op there: no second
writer, no cross-process write contention (crons may only ever READ).

``maybe_ingest`` is the hook spliced into the fetcher return path (the true chokepoint — every
adapter's ``search`` returns ``list[Document]`` there, regardless of its internal cache
shape). It queues eligible observations for the writer, which commits them to SQLite in batches; the
vectors are computed afterwards by the embed worker (``_embed_loop``) from the rows the database lists
as not embedded yet (``vec_todo``). It NEVER raises: a hook exception would break every search.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from typing import Any, Optional

from omniseek.core.normalize import Document
from omniseek.core.recall import store

logger = logging.getLogger(__name__)

WRITES_ENABLED = False          # flipped True ONCE in serve_http.main(); cron processes leave it False
RETAIN_DAYS = 365               # roll-off age cap (immutable sources are never swept) — operator knob
_DRAIN = 200                    # max queue items (observations) merged into one write transaction

# ── Observations go straight into SQLite ─────────────────────────────────
# No observation journal in front of SQLite: embedding is slow (2.9 to 4.6
# observations a second with it on the host machine, against 1,104 a second for the SQLite writes alone in
# transactions of _DRAIN, and 18 to 37 a second at the peak of the 11-17 replay), so a backlog waiting
# for its vectors would sit only in memory and a hard restart would lose it. SQLite's WAL is that durable layer: the
# writer commits observations as they arrive, at most _DRAIN to a transaction, and the vectors are
# computed afterwards (the embed worker below) from the rows the database lists as not embedded yet. A
# hard restart loses at most what the writer had queued and not committed.
# The caller dumps each document HERE, on its own thread (ranking stamps it later), filtered by
# _filter_private, and the writer validates it back into an Document.
# _QUEUE_MAX is PROVISIONAL: one broad
# search hands at most about 90 sources x 15 documents = 1350 observations, a payload averages 5.6 KB
# (the journal's blob store: 2.0 GB in 357,095 blobs), so 10,000 queued observations hold about 56 MB
# and cover seven such searches while the writer is busy elsewhere (the hourly roll-off, a matrix
# rebuild). Past it an observation is dropped and counted (``observations_dropped``, in the health
# reading), never waited for: a caller must not block on the index. Calibrate: a non-zero count means
# the writer fell behind; read what it was doing at that time first.
_QUEUE_MAX = 10000

_queue: "queue.Queue" = queue.Queue(maxsize=_QUEUE_MAX)
_writer_started = False
_start_lock = threading.Lock()
_last_write_ts: float = 0.0
_IMMUTABLE = frozenset({"acl_anthology", "openreview", "ircc_ee_rounds"})
_dropped = 0

# Graceful-shutdown stop Event for the single-writer loop: set on ASGI-lifespan shutdown so the
# writer stops polling and runs its FINAL FLUSH, draining the queue BEST-EFFORT within the shutdown
# budget (committing incrementally so partial progress survives). Strictly better than the SIGKILL it
# replaces, which lost the whole queue; a backlog that overruns the budget loses only its un-flushed
# tail. See omniseek.core.lifecycle. _STOP_POLL_S bounds the idle queue-get so a stop is noticed within
# it; a real enqueue still wakes the loop instantly (queue.get returns the moment an item lands).
# Additive: behavior is identical until _STOP is set, which only happens on shutdown.
_STOP = threading.Event()
_STOP_POLL_S = 0.5

# The retrieval-anchored thin-memory lane IS a write tap, so it declares its vocabulary from day
# one like every other tap (vocabulary-by-minting, design section 3): it mints ``document`` nodes
# ONLY — no edge types (doc-doc same_as is a DERIVED view over docs.fp + external_ids, never a stored
# edge), no methods (nodes carry no edge method). Registered at import so ``declared_vocabulary``
# includes it whenever writer is loaded; guarded fail-open (a registration hiccup must never break
# the import of the writer that search depends on).
try:
    from omniseek.core.recall import graph as _graph
    _graph.register_mints("thin_memory", kinds=["document"], edge_types=[], methods=[])
except Exception as _exc:  # noqa: BLE001 — declaration is best-effort; never break writer import
    logger.debug("thin_memory register_mints skipped: %s", _exc)


def last_write_ts() -> float:
    return _last_write_ts


def observations_dropped() -> int:
    """Observations dropped because the writer queue was full (see _QUEUE_MAX)."""
    return _dropped


def vec_pending() -> "int | None":
    """Rows the database lists as not embedded yet (vec_todo: documents and thin titles), or None when
    the index cannot be read."""
    try:
        con = store._read_con()
        return int(con.execute("SELECT COUNT(*) FROM vec_todo").fetchone()[0]) if con is not None else None
    except Exception as exc:  # noqa: BLE001 -- a health read never raises
        logger.debug("recall vec_pending unreadable: %s", exc)
        return None


# Metadata keys never stored (the journal's privacy filter, kept as it was): an exact name or a suffix,
# compared after lowercasing and folding every run of other characters to "_".
_SENSITIVE_EXACT = frozenset({
    "raw", "cookie", "cookies", "token", "password", "secret", "authorization", "credential",
    "credentials", "api_key", "apikey", "set_cookie",
})
_SENSITIVE_SUFFIXES = ("_token", "_password", "_secret", "_authorization", "_credential")
_KEY_NORMALIZE = re.compile(r"[^a-z0-9]+")


def _is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = _KEY_NORMALIZE.sub("_", key.lower()).strip("_")
    return normalized in _SENSITIVE_EXACT or normalized.endswith(_SENSITIVE_SUFFIXES)


def _filter_private(value: Any) -> Any:
    """Remove sensitive keys recursively without changing safe values."""
    if isinstance(value, dict):
        return {key: _filter_private(item) for key, item in value.items() if not _is_sensitive_key(key)}
    if isinstance(value, (list, tuple)):
        return [_filter_private(item) for item in value]
    return value


def _payload(d: Document, lane: str) -> dict:
    data = d.model_dump(mode="json")
    if lane != "thin":
        return data
    metadata = data.get("metadata") or {}
    return {
        "source": data.get("source"),
        "source_id": data.get("source_id"),
        "url": data.get("url"),
        "title": data.get("title"),
        "content": "",
        "metadata": {key: metadata[key] for key in _THIN_ID_KEYS if metadata.get(key)},
    }


def _observation(d, lane: str) -> "tuple | None":
    """One queue item for the writer, the document dumped HERE (on the caller's thread), so what is
    stored is the document as it was retrieved. None when the document has no identity."""
    source = getattr(d, "source", None)
    source_id = str(getattr(d, "source_id", None) or getattr(d, "url", None) or "")
    if not source or not source_id or not hasattr(d, "model_dump"):
        return None
    return ("__obs__", lane, _filter_private(_payload(d, lane)))


def _hand_off(items: list) -> None:
    """Queue observations for the writer and return at once; a full queue drops (and counts) the
    observation, it never makes the caller wait."""
    global _dropped
    for item in items:
        if item is None:
            continue
        try:
            _queue.put_nowait(item)
        except queue.Full:
            _dropped += 1
            if _dropped == 1 or _dropped % 1000 == 0:
                logger.warning("recall writer queue full (%d items): %d observation(s) dropped so far",
                               _QUEUE_MAX, _dropped)


def maybe_ingest(docs) -> None:
    """Classify EVERY retrieved doc and queue it for the writer. NO-OP unless WRITES_ENABLED (so
    cron processes that hit the same fetcher chokepoint write nothing). NEVER raises into the caller
    (the fetcher hot path) — a raised exception here would break every search.

    Two lanes, per the retrieval-anchored thin-memory design:
      - ``indexable(source)`` -> the FULL row (title + content + vector).
      - otherwise (query-keyed / walled sources recall deliberately excludes) -> a THIN document node
        (title + url + fp + external_ids ONLY, never content), so the graph's perception history is
        COMPLETE, not just the ~40 enumerable sources. Walled / circumvention-tier docs are SKIPPED
        by default (what an operator's logged-in account retrieved is operator privacy) unless the
        deployment profile opts in via ``walled.remember_retrievals``."""
    if not WRITES_ENABLED or store._disabled or not docs:
        return
    try:
        from omniseek.core import fetcher, profile
        from omniseek.core.recall import indexable
        remember_walled = profile.remember_walled_retrievals()
        items = []
        for d in docs:
            source = getattr(d, "source", None)
            if not source:
                continue
            if indexable(source):
                items.append(_observation(d, "full"))
            elif remember_walled or not fetcher.is_walled_source(source):
                items.append(_observation(d, "thin"))
        _hand_off(items)
    except Exception as exc:  # noqa: BLE001 — never propagate into the fetcher
        logger.debug("maybe_ingest swallowed: %s", exc)


def _enqueue(item) -> None:
    """Put one graph batch on the writer queue. Best-effort: on a full queue drop the OLDEST item
    (search correctness is sacred, the index is not)."""
    try:
        _queue.put_nowait(item)
    except queue.Full:
        try:
            _queue.get_nowait()
            _queue.put_nowait(item)
        except Exception:  # noqa: BLE001
            pass


def ingest_produced(docs) -> None:
    """PERCEPTION-PRODUCED documents (ASR transcripts today) enter the FULL memory lane directly.
    They are not fetchable sources (no adapter; Path C never sweeps them), so they bypass the
    indexable() classification — the memory contract is: ALL text OmniSeek itself produces must be
    findable later ("which podcast said X"). Same single-writer queue, same upsert (idempotent on
    (source, source_id)), same gates (WRITES_ENABLED / fail-open); never raises."""
    if not WRITES_ENABLED or store._disabled or not docs:
        return
    try:
        _hand_off([_observation(d, "full") for d in docs])
    except Exception as exc:  # noqa: BLE001 — memory must never break perception
        logger.debug("ingest_produced swallowed: %s", exc)


def mark_run(source: str, count: int) -> None:
    """Stamp a source's ingest watermark (the still_live reference). Routed through the writer
    queue so the single-writer invariant holds. No-op unless writes are enabled."""
    if not WRITES_ENABLED or store._disabled:
        return
    try:
        _queue.put_nowait(("__mark__", source, count, time.time()))
    except Exception:  # noqa: BLE001
        pass


def mark_swept(source: str) -> None:
    """Stamp when the ingest loop last swept ``source`` (any outcome; the still_live watermark is
    ``mark_run``'s and stays conditional). Read back by ``store.swept_at`` so a restarted process does
    not sweep again what the one before it swept less than an interval ago.
    Routed through the writer queue (single writer). No-op unless writes are enabled."""
    if not WRITES_ENABLED or store._disabled:
        return
    try:
        _queue.put_nowait(("__swept__", source, 0, time.time()))
    except Exception:  # noqa: BLE001
        pass


def enqueue_graph(nodes: list[dict], edges: list[dict]) -> None:
    """The GENERAL graph write verb (design section 6): the write taps (cartographer, enrich, ...)
    hand mechanical FACTS + labeled ALIGNMENT candidates here as node + edge dicts. Generalizes the
    phase 2.0 thin-document lane to entity nodes + M/A edges over the SAME single-writer daemon (same WAL
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
    """Start the single writer daemon and the embed worker (idempotent). Called once from
    serve_http.main()."""
    global _writer_started
    with _start_lock:
        if _writer_started:
            return
        _writer_started = True
    threading.Thread(target=_writer_loop, name="recall-writer", daemon=True).start()
    threading.Thread(target=_embed_loop, name="recall-embed", daemon=True).start()


def _writer_loop() -> None:
    try:
        con = store.connect()
    except Exception as exc:  # noqa: BLE001
        logger.warning("recall writer connect failed -> no writes this run: %s", exc)
        return
    # S2: register so a graceful shutdown can drain THIS loop (the one that owns unflushed writes).
    # drain_last=True: drain_all joins the writer AFTER every producer loop, so no producer can
    # enqueue past the final flush below.
    from omniseek.core import lifecycle
    lifecycle.register_loop("recall-writer", _STOP, threading.current_thread(), drain_last=True)
    _BOOTED["event"].set()
    last_sweep = 0.0
    while not _STOP.is_set():
        try:
            try:
                item = _queue.get(timeout=_STOP_POLL_S)  # bounded so a stop wakes the idle loop;
                                                         # a real enqueue still returns instantly
            except queue.Empty:
                continue
            items = [item]
            try:
                while len(items) < _DRAIN:
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
                _rollback(con)
            except Exception:  # noqa: BLE001
                pass
    # STOP requested (graceful shutdown): FINAL FLUSH of everything still queued. Best-effort within
    # the shutdown drain budget: _final_flush commits INCREMENTALLY in batches, so partial progress
    # survives even if a pathological backlog overruns the budget and the daemon is abandoned
    # mid-drain. Strictly better than the SIGKILL it replaces (which lost the whole queue); a backlog
    # that exceeds the budget loses only its un-flushed tail. The embed worker stops first (its vectors
    # not stored yet stay listed for the next process).
    _EMBED_STOP.set()
    _final_flush(con)


def _seed_vec_todo(con) -> None:
    """List every row with no vector for the current model in vec_todo: once per model version (the
    meta key ``vec_todo_model``). Runs on the embed worker: the three scans read the whole docs and graph_nodes tables
    (2.7, 9.3 and 5.4 s cold on the 3 GB index of 2026-11-18, 45 s in all on a busy disk), so they run
    on its read connection and the writer only inserts what they found (``__todo__``)."""
    from omniseek.core.recall import embed
    mv = embed.MODEL_VERSION
    row = con.execute("SELECT v FROM meta WHERE k = 'vec_todo_model'").fetchone()
    if row is not None and row[0] == mv:
        return
    t0 = time.monotonic()
    heads = con.execute("SELECT d.rowid FROM docs d LEFT JOIN vec v ON v.rowid = d.rowid AND "
                        "v.model_version = ? WHERE v.rowid IS NULL", (mv,)).fetchall()
    tails = con.execute("SELECT d.rowid FROM docs d WHERE length(d.content) > ? AND NOT EXISTS "
                        "(SELECT 1 FROM vec_chunk c WHERE c.rowid = d.rowid AND c.model_version = ?)",
                        (_CHUNK_SIZE, mv)).fetchall()
    titles = con.execute("SELECT g.id FROM graph_nodes g LEFT JOIN vec_thin t ON t.node_id = g.id AND "
                         "t.model_version = ? WHERE g.kind = 'document' AND g.label IS NOT NULL AND "
                         "TRIM(g.label) != '' AND t.node_id IS NULL", (mv,)).fetchall()
    rows = ([("doc", str(r[0])) for r in heads] + [("doc", str(r[0])) for r in tails]
            + [("thin", r[0]) for r in titles])
    done = threading.Event()
    _queue.put(("__todo__", rows, mv, done), timeout=_HANDOFF_WAIT_S)
    done.wait(_HANDOFF_WAIT_S)
    logger.info("recall vec_todo listed %d doc(s) without a head vector, %d long doc(s) without passage "
                "vectors, %d thin title(s) without a vector (%s, %.1fs)",
                len(heads), len(tails), len(titles), mv, time.monotonic() - t0)


def _apply_todo(con, item) -> None:
    """Writer side of ``_seed_vec_todo``: insert the found rows and note the model, one transaction."""
    _tag, rows, mv, done = item
    try:
        now = time.time()
        con.execute("BEGIN")
        con.executemany("INSERT OR IGNORE INTO vec_todo(lane, key, queued_at) VALUES(?, ?, ?)",
                        [(lane, key, now) for lane, key in rows])
        con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('vec_todo_model', ?)", (mv,))
        _commit(con)
    except Exception as exc:  # noqa: BLE001 -- the next start lists them again
        logger.warning("recall vec_todo listing not stored: %s", exc)
        _rollback(con)
    finally:
        done.set()


def _final_flush(con) -> None:
    """On STOP, drain and apply the remaining queued items in BATCHES, each batch its OWN _apply (its
    own commit). Best-effort within the shutdown drain budget: committing incrementally means a budget
    overrun (the daemon abandoned mid-drain) loses only the un-drained TAIL, never the batches already
    committed. FAIL-OPEN per batch: a batch error is logged + rolled back, then the drain continues to
    the next batch (one bad batch never strands the rest). Nothing is raised out of the daemon."""
    total = 0
    batches = 0
    while True:
        batch: list = []
        try:
            while len(batch) < _DRAIN:
                batch.append(_queue.get_nowait())
        except queue.Empty:
            pass
        if not batch:
            break
        try:
            _apply(con, batch)
            total += len(batch)
            batches += 1
        except Exception as exc:  # noqa: BLE001 -- a batch error must not crash shutdown or strand the tail
            logger.warning("recall writer final-flush batch errored (%d item(s) already committed): %s",
                           total, exc)
            try:
                _rollback(con)
            except Exception:  # noqa: BLE001
                pass
    if total:
        logger.info("recall writer final flush on stop: applied %d queued item(s) in %d batch(es)",
                    total, batches)



_vec_fail = 0                  # persistent batch-embed failures (surfaced in health — a fail-open
                               # that's also fail-SILENT-forever is how the vector layer dies unseen)


def vec_embed_failures() -> int:
    return _vec_fail


def _commit(con) -> None:
    """Commit, then hand the vector changes this transaction staged to the cached matrices."""
    con.commit()
    store.publish_staged()


def _rollback(con) -> None:
    """Roll back, and forget the vector changes this transaction staged."""
    try:
        con.rollback()
    finally:
        store.discard_staged()


def _todo(con, lane: str, key) -> None:
    """List one row as not embedded yet (the embed worker takes it from here)."""
    con.execute("INSERT OR IGNORE INTO vec_todo(lane, key, queued_at) VALUES(?, ?, ?)",
                (lane, str(key), time.time()))


def _delete_observation(con, source: str, source_id: str) -> None:
    """Delete one identity from both the full and the thin recall projections (the roll-off)."""
    row = con.execute(
        "SELECT rowid, seg FROM docs WHERE source = ? AND source_id = ?",
        (source, source_id),
    ).fetchone()
    if row is not None:
        rowid, seg = row
        con.execute("INSERT INTO fts(fts, rowid, seg) VALUES('delete', ?, ?)", (rowid, seg))
        con.execute("DELETE FROM vec_chunk WHERE rowid = ?", (rowid,))
        con.execute("DELETE FROM vec WHERE rowid = ?", (rowid,))
        con.execute("DELETE FROM docs WHERE rowid = ?", (rowid,))
        con.execute("DELETE FROM vec_todo WHERE lane = 'doc' AND key = ?", (str(rowid),))
        store.stage_vec(rowid)
        store.stage_chunks(rowid)
    from omniseek.core.recall.graph import doc_node_id
    node_id = doc_node_id(source, source_id)
    thin_row = con.execute("SELECT 1 FROM graph_nodes WHERE id = ?", (node_id,)).fetchone()
    if thin_row is not None:
        con.execute("DELETE FROM graph_edges WHERE src = ? OR dst = ?", (node_id, node_id))
        con.execute("DELETE FROM vec_thin WHERE node_id = ?", (node_id,))
        con.execute("DELETE FROM graph_nodes WHERE id = ?", (node_id,))
        con.execute("DELETE FROM vec_todo WHERE lane = 'thin' AND key = ?", (node_id,))
        store.stage_thin(node_id)


def _apply(con, items) -> None:
    """Apply one batch of queue items in ONE transaction (observations, roll-off deletes, graph
    batches, watermarks), then the vectors the embed worker handed in (``__vecs__``, their own
    transaction)."""
    global _last_write_ts
    from omniseek.core import rank
    now = time.time()
    marks: list[tuple] = []
    docs: list = []
    thin_docs: list = []
    graph_batches: list[tuple] = []   # ('__graph__', nodes, edges) from enqueue_graph (the taps)
    tombstones: list[tuple] = []
    vec_items: list[tuple] = []
    for it in items:
        tag = it[0] if isinstance(it, tuple) and it else None
        if tag in ("__mark__", "__swept__"):
            marks.append(it)
        elif tag == "__obs__":
            try:
                d = Document.model_validate(it[2])
            except Exception as exc:  # noqa: BLE001 — one bad observation never aborts the batch
                logger.debug("recall observation skipped (invalid payload): %s", exc)
                continue
            (thin_docs if it[1] == "thin" else docs).append(d)
        elif tag == "__tombstone__":
            tombstones.append((it[1], it[2]))
        elif tag == "__graph__":
            graph_batches.append(it)  # entity nodes + M/A edges (cartographer / enrich taps)
        elif tag in ("__vecs__", "__todo__"):
            vec_items.append(it)
    if docs or thin_docs or tombstones or graph_batches or marks:
        con.execute("BEGIN")
        queued = False
        for d in docs:
            try:
                queued = _upsert(con, rank, d, now) or queued
            except Exception as exc:  # noqa: BLE001 — one bad doc never aborts the batch
                logger.debug("recall upsert skipped one doc: %s", exc)
        for d in thin_docs:
            try:
                queued = _upsert_thin(con, rank, d, now) or queued   # graph_nodes document node, NEVER content
            except Exception as exc:  # noqa: BLE001 — one bad thin doc never aborts the batch
                logger.debug("recall thin upsert skipped one doc: %s", exc)
        for source, source_id in tombstones:
            try:
                _delete_observation(con, source, source_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("recall tombstone skipped: %s", exc)
        for _tag, gnodes, gedges in graph_batches:
            try:
                _apply_graph(con, gnodes, gedges, now)   # entity nodes + M/A edges, same transaction
            except Exception as exc:  # noqa: BLE001 — one bad graph batch never aborts the write
                logger.debug("recall graph apply skipped one batch: %s", exc)
        for _tag, source, count, ts in marks:
            try:
                if _tag == "__swept__":   # when the ingest loop last swept it, whatever came of the sweep
                    con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)",
                                (store.SWEPT_KEY + source, repr(float(ts))))
                    continue
                con.execute("INSERT OR REPLACE INTO ingest_runs(source, ran_at, doc_count) VALUES(?,?,?)",
                            (source, ts, count))
            except Exception:  # noqa: BLE001
                pass
        _commit(con)
        if docs:
            _last_write_ts = now
        if queued:
            _VEC_WAKE.set()
    for it in vec_items:
        (_apply_todo if it[0] == "__todo__" else _apply_vecs)(con, it)


# ── Embedding, after the write ──────────────────────────────────────────────
# A row that needs a vector is listed in vec_todo when it is written: lane 'doc' (key docs.rowid: its
# head vector over title + content[:_CHUNK_SIZE], and its tail passages when it is long) and lane 'thin'
# (key graph_nodes.id: its title). Until its vectors are written the row is found by the lexical recall
# only. ONE embed worker (``_embed_loop``, thread recall-embed) reads a page of listed rows on its own
# read connection, computes the vectors outside any transaction, and hands them to the writer
# (``__vecs__``), which stores them only if the row is still what was embedded (docs.version, the thin
# label) and takes the row off the list, in one transaction; so there is still one writer, and the
# observation writes never wait on the model. Documents go before thin titles (the documents feed
# omniseek_search's vector arm, the titles only ``similar``); within a lane the oldest listed goes first.
# A page whose forward fails is set aside until the next start (the rows stay listed and are found
# lexically).
# _EMBED_PASSAGES (texts per page) is PROVISIONAL. The MPS memory does not depend on it: embed_passage
# runs forwards of at most embed._PASSAGE_LOCK_CHUNK (4) texts, each forward's buffers stay under
# embed._MPS_BUF_CAP (8 MiB) and the device cache is released after every forward, so the MPS peak is
# the weights plus one forward of 4 whatever the page holds. What the page sets is how long a row waits
# between its first forward and being stored, which is also the computed work a hard restart throws
# away: at the calibrated 111 ms a text in forwards of 4 (embed.py), 64 texts is about 7 s. The writer
# transaction that stores a page is 64 rows of 4 KB, milliseconds. A long document is up to
# 1 + _CHUNK_MAX = 17 texts, so a page holds at least three of them; a document is never split across
# pages. Calibrate from the replay readings (texts a second, the backlog curve): a larger page only
# pays when the per-call cost dominates, which it does not past 4 texts a forward.
_EMBED_PASSAGES = 64
_VEC_WAKE = threading.Event()
_EMBED_STOP = threading.Event()
_HANDOFF_WAIT_S = 60.0
_BOOTED = {"event": threading.Event()}   # set by the writer once it has its connection
_vec_skip: set = set()          # (lane, key) whose forward failed in this process: retried after a restart


def _embed_loop() -> None:
    """The embed worker. Not registered with eye.lifecycle: it has nothing to flush (whatever it has
    not stored stays listed in vec_todo for the next process), and a forward in progress must not
    spend the shared shutdown budget the writer's final flush needs. The writer stops it."""
    from omniseek.core.recall import embed
    con = None
    seeded = False
    while not _EMBED_STOP.is_set():
        if not seeded:
            if not _BOOTED["event"].wait(_STOP_POLL_S):
                continue
            try:
                con = store.connect()
                _seed_vec_todo(con)
            except Exception as exc:  # noqa: BLE001 -- the next start lists them; new rows are listed anyway
                logger.warning("recall vec_todo seeding failed: %s", exc)
            seeded = True
        did = 0
        try:
            from omniseek.core import recall
            if embed.available() and not recall.warming():   # the boot warmup loads the model first
                if con is None:
                    con = store.connect()
                did = _embed_page(con, embed)
        except Exception as exc:  # noqa: BLE001 — never let the worker die
            logger.warning("recall embed cycle errored: %s", exc)
        if not did:
            _VEC_WAKE.wait(_STOP_POLL_S)
            _VEC_WAKE.clear()


def _embed_page(con, embed) -> int:
    """Embed one page of listed rows and hand the vectors to the writer; return how many rows it
    settled (embedded, set aside or found gone), 0 when there was nothing to do."""
    global _vec_fail
    skip = _vec_skip
    texts: list = []
    plan_docs: list = []     # (rowid, version, first text index, passages)
    plan_thin: list = []     # (node id, label, text index)
    gone: list = []          # (lane, key) whose row is no longer there (or has no title)
    rows = con.execute(
        "SELECT t.key, d.rowid, d.version, d.title, d.content FROM vec_todo t "
        "LEFT JOIN docs d ON d.rowid = CAST(t.key AS INTEGER) WHERE t.lane = 'doc' "
        "ORDER BY t.queued_at LIMIT ?", (_EMBED_PASSAGES + len(skip),)).fetchall()
    for key, rid, version, title, content in rows:
        if ("doc", key) in skip:
            continue
        if rid is None:
            gone.append(("doc", key))
            continue
        head = _embed_text(title, content)
        passages = _chunk_passages(title or "", content or "")
        if texts and len(texts) + 1 + len(passages) > _EMBED_PASSAGES:
            break
        plan_docs.append((rid, version, len(texts), len(passages)))
        texts.append(head)
        texts.extend(passages)
    if len(texts) < _EMBED_PASSAGES:
        rows = con.execute(
            "SELECT t.key, g.label FROM vec_todo t LEFT JOIN graph_nodes g ON g.id = t.key "
            "WHERE t.lane = 'thin' ORDER BY t.queued_at LIMIT ?",
            (_EMBED_PASSAGES - len(texts) + len(skip),)).fetchall()
        for key, label in rows:
            if ("thin", key) in skip:
                continue
            label = (label or "").strip()
            if not label:
                gone.append(("thin", key))
                continue
            if len(texts) >= _EMBED_PASSAGES:
                break
            plan_thin.append((key, label, len(texts)))
            texts.append(label)
    if not texts and not gone:
        return 0
    keys = [("doc", str(p[0])) for p in plan_docs] + [("thin", p[0]) for p in plan_thin]
    docs_out: list = []
    thin_out: list = []
    if texts:
        vecs = embed.embed_passage(texts)
        if vecs is None or len(vecs) != len(texts):
            _vec_fail += 1
            skip.update(keys)
            logger.warning("recall embed: a page of %d text(s) failed; its %d row(s) stay lexical-only "
                           "until the next start", len(texts), len(keys))
            if not gone:
                return len(keys)
            keys = []
            texts = []
        else:
            docs_out = [(rid, version, vecs[i], list(vecs[i + 1:i + 1 + n])) for rid, version, i, n in plan_docs]
            thin_out = [(key, label, vecs[i]) for key, label, i in plan_thin]
    done = threading.Event()
    box: dict = {}
    try:
        _queue.put(("__vecs__", docs_out, thin_out, gone, done, box), timeout=_HANDOFF_WAIT_S)
    except queue.Full:
        return 0
    deadline = time.monotonic() + _HANDOFF_WAIT_S
    while not done.wait(_STOP_POLL_S):
        if time.monotonic() > deadline:
            return 0   # the writer is stuck or gone: the rows stay listed
    if not box.get("ok"):
        skip.update(keys)
    return len(texts) + len(gone) or 1


def _apply_vecs(con, item) -> None:
    """Store the vectors the embed worker computed (writer thread, one transaction): only for rows
    still as they were embedded; each stored row leaves vec_todo. Tells the worker when done."""
    _tag, docs_out, thin_out, gone, done, box = item
    from omniseek.core.recall import embed
    mv, dim = embed.MODEL_VERSION, embed.DIM
    wrote_doc = wrote_chunk = wrote_thin = False
    try:
        con.execute("BEGIN")
        for rid, version, head, chunks in docs_out:
            row = con.execute("SELECT version, source FROM docs WHERE rowid = ?", (rid,)).fetchone()
            if row is None or row[0] != version:
                continue   # deleted or rewritten since it was read: a rewrite listed it again
            v32 = head.astype("float32")
            con.execute("INSERT OR REPLACE INTO vec(rowid, model_version, dim, v) VALUES(?,?,?,?)",
                        (rid, mv, dim, v32.tobytes()))
            store.stage_vec(rid, v32, row[1], mv)
            cleared = con.execute("DELETE FROM vec_chunk WHERE rowid = ?", (rid,)).rowcount
            cvs = [c.astype("float32") for c in chunks]
            for idx, c in enumerate(cvs):
                con.execute("INSERT OR REPLACE INTO vec_chunk(rowid, chunk_idx, model_version, dim, v) "
                            "VALUES(?,?,?,?,?)", (rid, idx, mv, dim, c.tobytes()))
            if cvs or cleared:
                store.stage_chunks(rid, cvs, row[1], mv)
                wrote_chunk = True
            con.execute("DELETE FROM vec_todo WHERE lane = 'doc' AND key = ?", (str(rid),))
            wrote_doc = True
        for key, label, v in thin_out:
            row = con.execute("SELECT label FROM graph_nodes WHERE id = ?", (key,)).fetchone()
            if row is None or (row[0] or "").strip() != label:
                continue
            v32 = v.astype("float32")
            con.execute("INSERT OR REPLACE INTO vec_thin(node_id, model_version, dim, v) VALUES(?,?,?,?)",
                        (key, mv, dim, v32.tobytes()))
            store.stage_thin(key, v32, mv)
            con.execute("DELETE FROM vec_todo WHERE lane = 'thin' AND key = ?", (key,))
            wrote_thin = True
        for lane, key in gone:
            if lane == "doc":
                if con.execute("SELECT 1 FROM docs WHERE rowid = ?", (int(key),)).fetchone() is None:
                    con.execute("DELETE FROM vec_todo WHERE lane = 'doc' AND key = ?", (key,))
            else:
                row = con.execute("SELECT label FROM graph_nodes WHERE id = ?", (key,)).fetchone()
                if row is None or not (row[0] or "").strip():
                    con.execute("DELETE FROM vec_todo WHERE lane = 'thin' AND key = ?", (key,))
        _commit(con)
        box["ok"] = True
    except Exception as exc:  # noqa: BLE001 — the rows stay listed
        logger.warning("recall vector write failed: %s", exc)
        try:
            _rollback(con)
        except Exception:  # noqa: BLE001
            pass
    finally:
        done.set()
    # Bring the vector matrices up to date HERE, on the writer thread, so the work stops landing on a
    # search READ thread: the changes the commit just published are applied to the cached matrices in
    # place (store._apply_deltas). Best-effort + fail-open: the read path applies the same pending
    # changes on demand if this is skipped, so correctness never depends on it.
    try:
        if wrote_doc:
            store._ensure_matrix(con)
        if wrote_chunk:
            store._ensure_chunk_matrix(con)
        if wrote_thin:
            store._ensure_thin_matrix(con)
    except Exception as exc:  # noqa: BLE001 — a warm failure must never break the write path
        logger.debug("recall matrix warm skipped: %s", exc)


# ── chunk embeddings: a LONG doc's TAIL passages (content beyond the head vec's first _CHUNK_SIZE
# chars). Each passage carries the title as a BREADCRUMB header so a page-40 passage still knows its
# doc's topic (contextual-chunk-header idea, borrowed from tldw/SurfSense, MINUS their heavier machinery).
# Bounded to _CHUNK_MAX passages/doc so one 650k-char doc cannot balloon the matrix. ────────────────
_CHUNK_SIZE = 2000   # matches _embed_text's head cap; each tail passage is one head-sized slice
_CHUNK_MAX = 16      # cap passages/doc (~34k chars of tail indexed semantically; the rest stays FTS-only)


def _chunk_passages(title: str, content: str) -> list:
    """A long doc's TAIL passages (content beyond the first _CHUNK_SIZE chars, which the head ``vec``
    already covers), each a ~_CHUNK_SIZE slice prefixed with the title breadcrumb. [] for a short doc.
    Pure."""
    content = content or ""
    if len(content) <= _CHUNK_SIZE:
        return []
    head = (title or "").strip()
    tail = content[_CHUNK_SIZE:]
    out = []
    for i in range(0, len(tail), _CHUNK_SIZE):
        piece = tail[i:i + _CHUNK_SIZE].strip()
        if not piece:
            continue
        out.append((head + "\n" + piece).strip() if head else piece)
        if len(out) >= _CHUNK_MAX:
            break
    return out


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


def _embed_text(title: "str | None", content: "str | None") -> str:
    """RAW title+content for the SEMANTIC embedder (NOT docs.seg, which is the lexical bigram
    shadow — feeding that to a semantic model is garbage). Capped so a giant doc can't stall a batch."""
    return ((title or "") + "\n" + (content or "")).strip()[:2000]


_THIN_LABEL_CAP = 200               # title cap for a thin document node's label (design section 3)
# The external-id keys lifted into a thin node's attrs_json — the SAME names graph.py's id_eq
# derivation (``_derived_id_eq_edges`` / the graph_nodes id_eq arm) reads via json_extract, so a
# thin row's ids join to work entities identically to a full docs-table row.
_THIN_ID_KEYS = ("doi", "arxiv_id", "openalex_id")


def _upsert_node(con, nid: str, kind: str, label: Optional[str], attrs_json: Optional[str],
                 now: float) -> None:
    """The ONE graph_nodes upsert (extracted from the thin lane so the thin-document lane and the
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
    """Upsert one THIN document node into ``graph_nodes`` (phase 2.0 retrieval-anchored perception).

    A thin row anchors a doc from a NON-indexed source in the graph with title + url + fp +
    external_ids ONLY — NEVER content (the graph never stores content). id = ``doc:{source}:{sid}``
    (graph.doc_node_id, the SAME scheme the views resolve), kind = ``document``, label = title
    (capped). ``first_seen`` is immutable on conflict (INSERT-supplied first_seen is ignored when the
    row exists); ``last_seen`` is bumped. An indexable doc NEVER reaches here (it gets a full docs
    row, which the union view already surfaces — no double node).

    its title (the label) is embedded into vec_thin, letting ``similar`` see the whole perception
    history. Returns True when the row was listed for that (vec_todo, lane 'thin'): a new node with a
    title, or a title that changed (whose old vector is dropped here). The embed itself is the embed
    worker's, after the commit, and fail-open, so a thin row is upserted whether or not its title ever
    embeds."""
    source = getattr(d, "source", None)
    sid = str(getattr(d, "source_id", None) or getattr(d, "url", None) or "")
    if not source or not sid:
        return False
    from omniseek.core.recall.graph import doc_node_id
    nid = doc_node_id(source, sid)
    old = con.execute("SELECT label FROM graph_nodes WHERE id = ?", (nid,)).fetchone()
    # STRIPPED before the cap: the vec_todo seeding SQL and the embed worker both key on "has a real
    # title", and an unstripped whitespace-only label (e.g. an nbsp from scraped HTML) would pass the
    # SQL's label != '' while the worker's strip drops it. One strip here keeps them in agreement.
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
    # refreshed (last write wins) — the semantics, now one code path for thin + entity lanes.
    _upsert_node(con, nid, "document", label, attrs_json, now)
    # phase 7 title vector: listed only for a new title (the TITLE is embedded, not the url/fp); a
    # re-observation with the same title keeps its vector (or its place on the list).
    if old is not None and old[0] == label:
        return False
    if old is not None and con.execute("DELETE FROM vec_thin WHERE node_id = ?", (nid,)).rowcount:
        store.stage_thin(nid)
    if label:
        _todo(con, "thin", nid)
        return True
    return False


# ── Graph lanes (design sections 4-6): generalize the thin lane to entity nodes + M/A edges ──
# The symmetric-relation set (design section 4): these types express an UNORDERED pair, so the writer
# stores each ONCE with src < dst (lexicographic). Otherwise (A,B) and (B,A) are distinct rows under
# the UNIQUE(src,dst,type,method) key and duplicates accrete. phase 2's minted types (cites/authored/
# about/published_in) are all DIRECTED, so this fires on none of them yet — but phase 3's ``coauthored``
# will, so the helper lands NOW (built with the model, not retrofitted).
_SYMMETRIC_EDGE_TYPES = frozenset({"same_as", "not_same_as", "coauthored", "conflicts"})
_LEGAL_TIERS = frozenset({"M", "A"})   # J is STRUCTURALLY excluded (the SQL CHECK; validate early too)


def _normalize_edge_endpoints(src: str, dst: str, etype: str) -> tuple[str, str]:
    """Symmetric-edge normalization (design section 4): for a SYMMETRIC type, return the endpoints
    reordered so ``src < dst`` (lexicographic) — so a reversed write (B,A) lands on the SAME row as
    (A,B) under the UNIQUE key instead of accreting a duplicate. DIRECTED types pass through as-is.
    phase 3's ``coauthored`` is the first real consumer; implemented now per the spec (the model is
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
        logger.debug("graph edge dropped (illegal tier %r; only M/A may enter OmniSeek's store): %r",
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
    """Upsert one doc. Returns True when the doc NEEDS (re-)embedding (new doc, or content changed)
    and was listed in vec_todo for the embed worker, else False (unchanged → just a last_seen bump,
    no re-embed)."""
    source = getattr(d, "source", None)
    sid = str(getattr(d, "source_id", None) or getattr(d, "url", None) or "")
    if not source or not sid:
        return False
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
            return False
        # content (or seg derivation) changed -> version bump + rewrite row, the external-content FTS
        # entry, AND drop the now-stale vectors (head and passages): the row is lexical-only until the
        # embed worker writes the new ones.
        con.execute("INSERT INTO fts(fts, rowid, seg) VALUES('delete', ?, ?)", (rowid, old_seg))
        con.execute(
            "UPDATE docs SET fp=?, url=?, title=?, content=?, author=?, date=?, score=?, doc_json=?, "
            "seg=?, seg_version=?, content_hash=?, last_seen=?, version=? WHERE rowid=?",
            (fp, d.url, d.title, d.content, d.author, _dt(d.date), d.attention_value(), djson, seg, sver, chash,
             now, (ver or 1) + 1, rowid),
        )
        con.execute("INSERT INTO fts(rowid, seg) VALUES(?, ?)", (rowid, seg))
        con.execute("DELETE FROM vec WHERE rowid = ?", (rowid,))
        store.stage_vec(rowid)  # the stale vector leaves the matrix too (a re-embed stages the new one)
        if con.execute("DELETE FROM vec_chunk WHERE rowid = ?", (rowid,)).rowcount:
            store.stage_chunks(rowid)
        _todo(con, "doc", rowid)
        return True
    cur = con.execute(
        "INSERT INTO docs(source, source_id, fp, url, title, content, author, date, score, "
        "doc_json, seg, seg_version, content_hash, first_seen, last_seen, version, immutable) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
        (source, sid, fp, d.url, d.title, d.content, d.author, _dt(d.date), d.attention_value(), djson, seg,
         sver, chash, now, now, 1 if source in _IMMUTABLE else 0),
    )
    con.execute("INSERT INTO fts(rowid, seg) VALUES(?, ?)", (cur.lastrowid, seg))
    _todo(con, "doc", cur.lastrowid)
    return True


def _sweep(con) -> None:
    """Mechanical roll-off: delete what was last seen more than RETAIN_DAYS ago, in one transaction.

    Two lanes with the SAME cutoff: (1) full docs rows (never immutable ones); (2) THIN document
    nodes in graph_nodes (``kind='document'``) — a thin doc's perception memory rolls off on the
    same clock as content memory. NON-document graph_nodes kinds (entities: work / person / ...) are
    EXEMPT: entities persist indefinitely (design's lifecycle section — small rows, relation memory
    may outlive content memory). Each identity goes from both projections (``_delete_observation``)."""
    cutoff = time.time() - RETAIN_DAYS * 86400
    try:
        stale = con.execute(
            "SELECT source, source_id FROM docs WHERE immutable = 0 AND last_seen < ?", (cutoff,)
        ).fetchall()
        stale_thin = con.execute(
            "SELECT id FROM graph_nodes WHERE kind = 'document' AND last_seen < ?", (cutoff,)
        ).fetchall()
        if not stale and not stale_thin:
            return
        identities = {(source, source_id) for source, source_id in stale}
        for (nid,) in stale_thin:
            parts = nid.split(":", 2)
            if len(parts) != 3 or parts[0] != "doc" or not parts[1] or not parts[2]:
                logger.warning("recall roll-off kept malformed document node id %r", nid)
                continue
            identities.add((parts[1], parts[2]))
        con.execute("BEGIN")
        for source, source_id in sorted(identities):
            _delete_observation(con, source, source_id)
        _commit(con)
        logger.info("recall roll-off deleted %d identities older than %dd", len(identities), RETAIN_DAYS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("recall sweep skipped: %s", exc)
        try:
            _rollback(con)
        except Exception:  # noqa: BLE001
            pass
