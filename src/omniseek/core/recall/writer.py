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
shape). It durably journals eligible observations before enqueueing a materializer wake, and it
NEVER raises: a hook exception would break every search.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Optional

from omniseek.core.normalize import Document
from omniseek.core.recall import store
from omniseek.core.recall.journal import JournalCorrupt, JournaledObservation, ObservationJournal, ObservationReceipt

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
_observation_journal: "ObservationJournal | None" = None
_journal_lock = threading.Lock()
_journal_failures = 0
_last_journal_failure: "str | None" = None
_materialization_failures = 0
_last_materialization_failure: "str | None" = None
_JOURNAL_WAKE = threading.Event()

# S2 graceful-shutdown stop Event for the single-writer loop: set on ASGI-lifespan shutdown so the
# writer stops polling and runs its FINAL FLUSH, draining the queue BEST-EFFORT within the shutdown
# budget (committing incrementally so partial progress survives). Strictly better than the SIGKILL it
# replaces, which lost the whole queue; a backlog that overruns the budget loses only its un-flushed
# tail. See omniseek.core.lifecycle. _STOP_POLL_S bounds the idle queue-get so a stop is noticed within
# it; a real enqueue still wakes the loop instantly (queue.get returns the moment an item lands).
# Additive: behavior is identical until _STOP is set, which only happens on shutdown.
_STOP = threading.Event()
_STOP_POLL_S = 0.5

# The P2.0 retrieval-anchored thin-memory lane IS a write tap, so it declares its vocabulary from day
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


def journal_failures() -> int:
    return _journal_failures


def last_journal_failure() -> "str | None":
    return _last_journal_failure


def materialization_failures() -> int:
    return _materialization_failures


def last_materialization_failure() -> "str | None":
    return _last_materialization_failure


def journal_health(
    *,
    journal: "ObservationJournal | None" = None,
    con=None,
) -> dict:
    """Return observable Journal and SQLite materialization state without mutating either."""
    active_journal = journal or _journal()
    own_con = con is None
    connection = con or store.connect()
    try:
        head_seq = active_journal.head_seq
        head_hash = active_journal.head_hash
        last_seq = _materialization_cursor(connection, active_journal)
        last_event = active_journal.event(last_seq) if last_seq else None
        return {
            "journal_head_seq": head_seq,
            "journal_head_hash": head_hash,
            "pending_materializations": max(0, head_seq - last_seq),
            "last_materialized_seq": last_seq,
            "last_materialized_hash": last_event["event_hash"] if last_event else None,
            "journal_append_failures": _journal_failures,
            "materialization_failures": _materialization_failures,
            "failed_receipts": _journal_failures + _materialization_failures,
            "last_failure": _last_materialization_failure or _last_journal_failure,
        }
    except Exception as exc:  # noqa: BLE001 -- health reports corruption instead of hiding it
        return {
            "journal_head_seq": active_journal.head_seq,
            "journal_head_hash": active_journal.head_hash,
            "pending_materializations": None,
            "last_materialized_seq": None,
            "last_materialized_hash": None,
            "journal_append_failures": _journal_failures,
            "materialization_failures": _materialization_failures,
            "failed_receipts": _journal_failures + _materialization_failures,
            "last_failure": _last_materialization_failure or _last_journal_failure,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if own_con:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass


def _journal() -> ObservationJournal:
    global _observation_journal
    if _observation_journal is None:
        with _journal_lock:
            if _observation_journal is None:
                _observation_journal = ObservationJournal()
    return _observation_journal


def _journal_payload(d: Document, lane: str) -> dict:
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


def _record_observation(
    d,
    *,
    lane: str,
    provenance: str,
    privacy_namespace: str,
) -> "ObservationReceipt | None":
    source = getattr(d, "source", None)
    source_id = str(getattr(d, "source_id", None) or getattr(d, "url", None) or "")
    if not source or not source_id or not hasattr(d, "model_dump"):
        return None
    try:
        return _journal().append_payload(
            _journal_payload(d, lane),
            source=source,
            source_id=source_id,
            observed_at=time.time(),
            provenance=provenance,
            privacy_namespace=privacy_namespace,
            lane=lane,
        )
    except Exception as exc:  # noqa: BLE001 -- journal failure must not break perception
        _record_journal_failure("observation append", exc)
        return None


def _record_journal_failure(context: str, exc: Exception) -> None:
    global _journal_failures, _last_journal_failure
    _journal_failures += 1
    _last_journal_failure = f"{context}: {type(exc).__name__}: {exc}"
    logger.warning("observation journal %s failed: %s", context, exc)


def _materialization_cursor(con, journal: ObservationJournal) -> int:
    rows = dict(con.execute(
        "SELECT k, v FROM meta WHERE k IN ('journal_materialized_seq', 'journal_materialized_hash')"
    ).fetchall())
    raw_seq = rows.get("journal_materialized_seq", "0")
    try:
        seq = int(raw_seq)
    except (TypeError, ValueError) as exc:
        raise JournalCorrupt(f"invalid SQLite materialization cursor: {raw_seq!r}") from exc
    if seq == 0:
        if rows.get("journal_materialized_hash") not in (None, ""):
            raise JournalCorrupt("SQLite materialization hash exists without a sequence")
        return 0
    event = journal.event(seq)
    if event is None:
        raise JournalCorrupt(f"SQLite materialization cursor {seq} is past the journal head")
    if rows.get("journal_materialized_hash") != event["event_hash"]:
        raise JournalCorrupt(f"SQLite materialization hash mismatch at seq {seq}")
    return seq


def _materialization_item(observation: JournaledObservation):
    if observation.kind == "tombstone":
        return ("__tombstone__", observation)
    if observation.payload is None:
        raise JournalCorrupt(f"observation {observation.journal_seq} has no payload")
    doc = Document.model_validate(observation.payload)
    if observation.lane == "full":
        return [doc]
    if observation.lane == "thin":
        return ("__thin__", doc)
    raise JournalCorrupt(f"unknown materialization lane {observation.lane!r}")


def _materialize_pending(
    con,
    journal: "ObservationJournal | None" = None,
    *,
    limit: "int | None" = None,
) -> int:
    """Replay journal events in order and commit each SQLite cursor atomically with its event."""
    global _materialization_failures, _last_materialization_failure
    active_journal = journal or _journal()
    cursor = _materialization_cursor(con, active_journal)
    pending = active_journal.pending(after_seq=cursor, limit=limit)
    applied = 0
    for observation in pending:
        try:
            _apply(
                con,
                [_materialization_item(observation)],
                strict=True,
                journal_checkpoint=(observation.journal_seq, observation.event_hash),
            )
            applied += 1
        except Exception as exc:  # noqa: BLE001 -- stop at the first unmaterialized sequence
            try:
                con.rollback()
            except Exception:  # noqa: BLE001
                pass
            _materialization_failures += 1
            _last_materialization_failure = (
                f"seq={observation.journal_seq} {type(exc).__name__}: {exc}"
            )
            logger.warning("observation materialization stopped at seq %d: %s",
                           observation.journal_seq, exc)
            break
    return applied


def _process_writer_items(
    con,
    items,
    *,
    journal: "ObservationJournal | None" = None,
    force_journal: bool = False,
) -> int:
    """Treat journal queue entries as wake signals and replay the durable sequence in order."""
    journal_markers = [
        item for item in items
        if isinstance(item, tuple) and item and item[0] == "__journal__"
    ]
    passthrough = [
        item for item in items
        if not (isinstance(item, tuple) and item and item[0] == "__journal__")
    ]
    applied = 0
    if force_journal or journal_markers or _JOURNAL_WAKE.is_set():
        active_journal = journal or _journal()
        applied = _materialize_pending(con, active_journal, limit=_DRAIN)
        cursor = _materialization_cursor(con, active_journal)
        if cursor < active_journal.head_seq:
            _JOURNAL_WAKE.set()
        else:
            _JOURNAL_WAKE.clear()
    if passthrough:
        _apply(con, passthrough)
    return applied


def maybe_ingest(docs) -> None:
    """Classify EVERY retrieved doc and enqueue it for the writer. NO-OP unless WRITES_ENABLED (so
    cron processes that hit the same fetcher chokepoint write nothing). JOURNAL-FIRST and NEVER
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
        from omniseek.core import fetcher, profile
        from omniseek.core.recall import indexable
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
            journaled = []
            for d in full:
                receipt = _record_observation(
                    d, lane="full", provenance="retrieved", privacy_namespace="public",
                )
                if receipt is not None:
                    journaled.append((receipt.journal_seq, receipt.observation_id))
            if journaled:
                _JOURNAL_WAKE.set()
                _enqueue(("__journal__", journaled))
        for d in thin:
            is_walled = fetcher.is_walled_source(getattr(d, "source", ""))
            receipt = _record_observation(
                d,
                lane="thin",
                provenance="retrieved",
                privacy_namespace="walled" if is_walled else "public",
            )
            if receipt is not None:
                _JOURNAL_WAKE.set()
                _enqueue(("__journal__", [(receipt.journal_seq, receipt.observation_id)]))
    except Exception as exc:  # noqa: BLE001 — never propagate into the fetcher
        logger.debug("maybe_ingest swallowed: %s", exc)


def _enqueue(item) -> None:
    """Put one wake or legacy item on the writer queue.

    Journal wakes are disposable because the durable journal, not this queue, owns every observation.
    On overflow the oldest wake may be dropped; the next retained wake replays every pending seq.
    """
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
    indexable() classification — the memory contract is: ALL text the eye itself produces must be
    findable later ("which podcast said X"). Same single-writer queue, same upsert (idempotent on
    (source, source_id)), same gates (WRITES_ENABLED / fail-open); journal-first, never raises."""
    if not WRITES_ENABLED or store._disabled or not docs:
        return
    try:
        journaled = []
        for d in docs:
            receipt = _record_observation(
                d, lane="full", provenance="produced", privacy_namespace="public",
            )
            if receipt is not None:
                journaled.append((receipt.journal_seq, receipt.observation_id))
        if journaled:
            _JOURNAL_WAKE.set()
            _enqueue(("__journal__", journaled))
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
    # S2: register so a graceful shutdown can drain THIS loop (the one that owns unflushed writes).
    # drain_last=True: drain_all joins the writer AFTER every producer loop, so no producer can
    # enqueue past the final flush below.
    from omniseek.core import lifecycle
    lifecycle.register_loop("recall-writer", _STOP, threading.current_thread(), drain_last=True)
    last_sweep = 0.0
    try:
        _process_writer_items(con, [], force_journal=True)
    except Exception as exc:  # noqa: BLE001 -- corruption is visible but never kills the writer thread
        logger.warning("recall journal startup replay failed closed: %s", exc)
    while not _STOP.is_set():
        try:
            try:
                item = _queue.get(timeout=_STOP_POLL_S)  # bounded so a stop wakes the idle loop;
                                                         # a real enqueue still returns instantly
            except queue.Empty:
                items = []
            else:
                items = [item]
                try:
                    for _ in range(_DRAIN):
                        items.append(_queue.get_nowait())
                except queue.Empty:
                    pass
            if not items and not _JOURNAL_WAKE.is_set():
                continue
            _process_writer_items(con, items)
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
                    # DRAIN the chunk backfill backlog while idle: keep chunking pages until caught up
                    # (returns 0) OR real work arrives (queue non-empty), re-checking BETWEEN pages so a
                    # queued write interrupts within ONE page (~a page's embed, ~1-2s). A big one-time
                    # backlog thus clears in the first long idle window instead of one page every few
                    # minutes over days; the per-page queue check keeps it responsive to live ingest.
                    while _queue.empty() and _chunk_catchup(con) > 0:
                        pass
                except Exception as exc:  # noqa: BLE001 — a catch-up failure never breaks the writer
                    logger.debug("recall thin/chunk catch-up cycle errored: %s", exc)
        except Exception as exc:  # noqa: BLE001 — never let the writer thread die
            logger.warning("recall writer cycle errored: %s", exc)
            try:
                con.rollback()
            except Exception:  # noqa: BLE001
                pass
    # STOP requested (graceful shutdown): FINAL FLUSH of everything still queued. Best-effort within
    # the shutdown drain budget: _final_flush commits INCREMENTALLY in batches, so partial progress
    # survives even if a pathological backlog overruns the budget and the daemon is abandoned
    # mid-drain. Strictly better than the SIGKILL it replaces (which lost the whole queue); a backlog
    # that exceeds the budget loses only its un-flushed tail.
    _final_flush(con)


def _final_flush(con) -> None:
    """On STOP, drain and apply the remaining queued items in BATCHES, each batch its OWN _apply (its
    own commit). Best-effort within the shutdown drain budget: committing incrementally means a budget
    overrun (the daemon abandoned mid-drain) loses only the un-drained TAIL, never the batches already
    committed. Reuses the normal loop's _apply + _DRAIN batch size (no new write logic). FAIL-OPEN per
    batch: a batch error is logged + rolled back, then the drain continues to the next batch (one bad
    batch never strands the rest). Nothing is raised out of the daemon (shutdown is already underway)."""
    total = 0
    batches = 0
    while True:
        batch: list = []
        try:
            for _ in range(_DRAIN):
                batch.append(_queue.get_nowait())
        except queue.Empty:
            pass
        if not batch:
            break
        try:
            _process_writer_items(con, batch)
            total += len(batch)
            batches += 1
        except Exception as exc:  # noqa: BLE001 -- a batch error must not crash shutdown or strand the tail
            logger.warning("recall writer final-flush batch errored (%d item(s) already committed): %s",
                           total, exc)
            try:
                con.rollback()
            except Exception:  # noqa: BLE001
                pass
    while _JOURNAL_WAKE.is_set():
        try:
            applied = _process_writer_items(con, [], force_journal=True)
            if applied == 0:
                break
        except Exception as exc:  # noqa: BLE001 -- shutdown must not crash on a corrupt journal
            logger.warning("recall writer final journal replay failed closed: %s", exc)
            break
    if total:
        logger.info("recall writer final flush on stop: applied %d queued item(s) in %d batch(es)",
                    total, batches)


_vec_fail = 0                  # persistent batch-embed failures (surfaced in health — a fail-open
                               # that's also fail-SILENT-forever is how the vector layer dies unseen)
_BACKFILL_PAGE = 64


def vec_embed_failures() -> int:
    return _vec_fail


def _delete_observation(con, source: str, source_id: str) -> None:
    """Materialize a tombstone across both full and thin recall projections."""
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
        store.note_vec_write()
        store.note_chunk_write()
    from omniseek.core.recall.graph import doc_node_id
    node_id = doc_node_id(source, source_id)
    thin_row = con.execute("SELECT 1 FROM graph_nodes WHERE id = ?", (node_id,)).fetchone()
    if thin_row is not None:
        con.execute("DELETE FROM graph_edges WHERE src = ? OR dst = ?", (node_id, node_id))
        con.execute("DELETE FROM vec_thin WHERE node_id = ?", (node_id,))
        con.execute("DELETE FROM graph_nodes WHERE id = ?", (node_id,))
        store.note_thin_write()


def _apply(
    con,
    items,
    *,
    strict: bool = False,
    journal_checkpoint: "tuple[int, str] | None" = None,
) -> None:
    global _last_write_ts
    from omniseek.core import rank
    now = time.time()
    marks: list[tuple] = []
    doc_batches: list[list] = []
    thin_docs: list = []
    graph_batches: list[tuple] = []   # ('__graph__', nodes, edges) from enqueue_graph (the taps)
    tombstones: list[JournaledObservation] = []
    do_backfill = False
    for it in items:
        if isinstance(it, tuple) and it:
            if it[0] == "__mark__":
                marks.append(it)
            elif it[0] == "__backfill__":
                do_backfill = True
            elif it[0] == "__thin__":
                thin_docs.append(it[-1])   # journal metadata precedes the thin document
            elif it[0] == "__full__":
                doc_batches.append([entry[-1] for entry in it[1]])
            elif it[0] == "__tombstone__":
                tombstones.append(it[1])
            elif it[0] == "__graph__":
                graph_batches.append(it)  # entity nodes + M/A edges (cartographer / enrich taps)
            else:
                doc_batches.append(it)
        else:
            doc_batches.append(it)
    con.execute("BEGIN")
    staged: list = []   # (rowid, raw_text) for docs that need (re-)embedding
    chunk_staged: list = []   # (rowid, [tail passages]) for LONG docs that need (re-)embedding
    for batch in doc_batches:
        for d in batch:
            try:
                r = _upsert(con, rank, d, now)
                if r:
                    staged.append(r)
                    _p = _chunk_passages(getattr(d, "title", "") or "", getattr(d, "content", "") or "")
                    if _p:
                        chunk_staged.append((r[0], _p))  # only (re-)embedded long docs chunk; backfill covers the rest
            except Exception as exc:  # noqa: BLE001 — one bad doc never aborts the batch
                if strict:
                    raise
                logger.debug("recall upsert skipped one doc: %s", exc)
    thin_staged: list = []   # (node_id, title) for thin rows whose title needs embedding (P7)
    for d in thin_docs:
        try:
            r = _upsert_thin(con, rank, d, now)   # graph_nodes document node, NEVER content
            if r:
                thin_staged.append(r)
        except Exception as exc:  # noqa: BLE001 — one bad thin doc never aborts the batch
            if strict:
                raise
            logger.debug("recall thin upsert skipped one doc: %s", exc)
    for observation in tombstones:
        try:
            _delete_observation(con, observation.source, observation.source_id)
        except Exception as exc:  # noqa: BLE001 -- legacy queue writes remain fail-open
            if strict:
                raise
            logger.debug("recall tombstone skipped: %s", exc)
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
    if chunk_staged:
        _embed_and_store_chunk(con, chunk_staged)   # long-doc TAIL passages → vec_chunk, same txn
    if thin_staged:
        _embed_and_store_thin(con, thin_staged)   # P7: thin TITLE vectors → vec_thin, same txn
    if journal_checkpoint is not None:
        seq, event_hash = journal_checkpoint
        con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('journal_materialized_seq', ?)",
                    (str(seq),))
        con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('journal_materialized_hash', ?)",
                    (event_hash,))
    con.commit()
    # Warm the vector matrices HERE, on the writer thread, so the rebuild (a full reload + renormalize,
    # ~131MB at 32k rows) stops landing on a search READ thread. Because ingest writes on essentially
    # every search, the read path used to eat one rebuild per ~20s debounce window; the writer holding
    # this WAL connection refreshes them off that path. Best-effort + fail-open: the read path still
    # rebuilds on demand if this is skipped, so correctness never depends on it.
    if staged or thin_staged or chunk_staged:
        try:
            if staged:
                store._ensure_matrix(con)
            if chunk_staged:
                store._ensure_chunk_matrix(con)
            if thin_staged:
                store._ensure_thin_matrix(con)
        except Exception as exc:  # noqa: BLE001 — a warm failure must never break the write path
            logger.debug("recall matrix warm skipped: %s", exc)
    if doc_batches:
        _last_write_ts = now
    if do_backfill:
        _backfill_page(con)             # one bounded page (own txn), re-enqueues itself if more remain


def _embed_and_store(con, staged) -> None:
    """Embed the staged ``(rowid, text)`` batch ONCE and write the vectors in the CURRENT transaction
    (atomic with the doc rows). FAIL-OPEN: embedder disabled or a forward failure → those docs stay
    lexical-only (a first-class state, never an error), the failure is counted for health."""
    global _vec_fail
    from omniseek.core.recall import embed
    if not embed.available():
        return
    vecs = embed.embed_passage([t for (_rid, t) in staged])
    if vecs is None or len(vecs) != len(staged):
        _vec_fail += 1
        logger.debug("recall: batch embed failed/short → %d docs lexical-only", len(staged))
        return
    mv, dim = embed.MODEL_VERSION, embed.DIM
    wrote = False
    for (rid, _t), v in zip(staged, vecs):
        try:
            con.execute("INSERT OR REPLACE INTO vec(rowid, model_version, dim, v) VALUES(?,?,?,?)",
                        (rid, mv, dim, v.astype("float32").tobytes()))
            wrote = True
        except Exception as exc:  # noqa: BLE001
            logger.debug("recall vec write skipped: %s", exc)
    if wrote:
        store.note_vec_write()  # invalidate the docs matrix (catches re-embeds the row-count missed)


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


def _embed_and_store_chunk(con, doc_chunks) -> None:
    """Embed a batch of long docs' TAIL passages ONCE and write them to ``vec_chunk`` in the CURRENT
    transaction (atomic with the doc rows). ``doc_chunks`` = ``[(rowid, [passage, ...]), ...]``. Clears a
    doc's OLD chunk rows first (a content change / re-embed) so chunk_idx stays contiguous and no stale
    passages linger, then inserts the fresh set. FAIL-OPEN: an embed failure leaves those docs with just
    their head vec (a first-class state), mirroring ``_embed_and_store``."""
    global _vec_fail
    from omniseek.core.recall import embed
    if not embed.available():
        return
    flat = [(rid, idx, txt) for (rid, passages) in doc_chunks for idx, txt in enumerate(passages)]
    if not flat:
        return
    vecs = embed.embed_passage([txt for (_r, _i, txt) in flat])
    if vecs is None or len(vecs) != len(flat):
        _vec_fail += 1
        logger.debug("recall: chunk batch embed failed/short → %d passages skipped", len(flat))
        return
    mv, dim = embed.MODEL_VERSION, embed.DIM
    for (rid, _passages) in doc_chunks:
        try:
            con.execute("DELETE FROM vec_chunk WHERE rowid = ?", (rid,))  # drop stale passages first
        except Exception as exc:  # noqa: BLE001
            logger.debug("recall vec_chunk clear skipped: %s", exc)
    wrote = False
    for (rid, idx, _txt), v in zip(flat, vecs):
        try:
            con.execute("INSERT OR REPLACE INTO vec_chunk(rowid, chunk_idx, model_version, dim, v) "
                        "VALUES(?,?,?,?,?)", (rid, idx, mv, dim, v.astype("float32").tobytes()))
            wrote = True
        except Exception as exc:  # noqa: BLE001
            logger.debug("recall vec_chunk write skipped: %s", exc)
    if wrote:
        store.note_chunk_write()  # invalidate the chunk matrix


def _embed_and_store_thin(con, staged) -> None:
    """P7: embed a staged ``(node_id, title)`` batch of THIN rows ONCE and write the vectors into
    ``vec_thin`` in the CURRENT transaction (atomic with the thin node upserts). Mirrors
    ``_embed_and_store`` for the thin lane: FAIL-OPEN at the ROW level (embedder disabled or a forward
    failure -> those thin rows stay un-embedded, a first-class state; ``similar`` just does not see
    them until the catch-up embeds them; the coverage gauge is the visibility, not per-row logs). The
    TITLE is the passage (a thin row has no content); same MODEL_VERSION + embedder as ``vec`` so both
    live in ONE cosine space."""
    global _vec_fail
    from omniseek.core.recall import embed
    if not embed.available():
        return
    titles = [t for (_nid, t) in staged]
    vecs = embed.embed_passage(titles)
    if vecs is None or len(vecs) != len(staged):
        _vec_fail += 1
        logger.debug("recall: thin batch embed failed/short → %d thin rows un-embedded", len(staged))
        return
    mv, dim = embed.MODEL_VERSION, embed.DIM
    wrote = False
    for (nid, _t), v in zip(staged, vecs):
        try:
            con.execute(
                "INSERT OR REPLACE INTO vec_thin(node_id, model_version, dim, v) VALUES(?,?,?,?)",
                (nid, mv, dim, v.astype("float32").tobytes()))
            wrote = True
        except Exception as exc:  # noqa: BLE001
            logger.debug("recall vec_thin write skipped: %s", exc)
    if wrote:
        store.note_thin_write()  # invalidate the thin matrix


_THIN_CATCHUP_PAGE = 50   # thin rows the idle catch-up embeds per cycle (bounded; monotone convergence)


def _thin_catchup(con) -> int:
    """P7 self-converging catch-up (no script, no cron): embed up to ``_THIN_CATCHUP_PAGE`` thin rows
    that HAVE a title (graph_nodes.label) but NO ``vec_thin`` row for the current model_version, in one
    bounded page + own transaction. Called at the writer's IDLE point (after the queue drains — see
    ``_writer_loop``), so it NEVER delays a queued write (drain first, then catch up). Monotone
    convergence: committed rows drop out of the WHERE, so each cycle shrinks the backlog until it hits
    zero and then costs one cheap empty query. Returns the number embedded this cycle (0 when caught
    up or the embedder is unavailable). FAIL-OPEN — a catch-up failure never touches search."""
    from omniseek.core.recall import embed
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


_CHUNK_CATCHUP_PAGE = 40   # long docs the idle catch-up chunk-embeds per cycle (bounded; converges to 0)


def _chunk_catchup(con) -> int:
    """Self-converging catch-up (no script, no cron, SINGLE-WRITER safe): chunk-embed up to
    ``_CHUNK_CATCHUP_PAGE`` LONG docs (content > _CHUNK_SIZE) that have NO ``vec_chunk`` row for the
    current model_version, one bounded page + own transaction. Called at the writer's IDLE point (after
    the queue drains + the thin catch-up), so it NEVER delays a queued write. This is how the PRE-EXISTING
    long docs get their tail passages (new long docs chunk on ingest via _embed_and_store_chunk).
    Monotone convergence: a chunked doc gains vec_chunk rows and drops out of the WHERE, so each cycle
    shrinks the backlog to zero, then a cheap empty query. FAIL-OPEN — a catch-up failure never touches
    search."""
    from omniseek.core.recall import embed
    if not embed.available():
        return 0
    mv = embed.MODEL_VERSION
    try:
        rows = con.execute(
            "SELECT d.rowid, d.title, d.content FROM docs d "
            "LEFT JOIN vec_chunk c ON c.rowid = d.rowid AND c.model_version = ? "
            "WHERE c.rowid IS NULL AND length(d.content) > ? LIMIT ?",
            (mv, _CHUNK_SIZE, _CHUNK_CATCHUP_PAGE),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug("recall chunk catch-up query failed: %s", exc)
        return 0
    if not rows:
        return 0   # caught up: every long doc has current-model chunk rows
    doc_chunks = [(rid, p) for (rid, title, content) in rows
                  if (p := _chunk_passages(title or "", content or ""))]
    if not doc_chunks:
        return 0   # a rare all-whitespace tail: nothing to embed this page (benign; re-checked next idle)
    try:
        con.execute("BEGIN")
        _embed_and_store_chunk(con, doc_chunks)
        con.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("recall chunk catch-up write failed: %s", exc)
        try:
            con.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0
    logger.info("recall chunk catch-up: chunk-embedded a page of %d long docs into vec_chunk", len(doc_chunks))
    return len(doc_chunks)


def _backfill_page(con) -> None:
    """Embed ONE bounded page of docs missing a current-model_version vector, commit, then re-enqueue
    itself if more remain — so backfill interleaves with live ingest (one page per writer turn, never
    starves it) and is restart-safe (committed rows drop out of the WHERE, so re-running resumes)."""
    global _last_write_ts
    from omniseek.core.recall import embed
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
    from omniseek.core.recall.graph import doc_node_id
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
    """Journal mechanical roll-off before deleting either SQLite projection.

    Two lanes with the SAME cutoff: (1) full docs rows (never immutable ones); (2) THIN document
    nodes in graph_nodes (``kind='document'``) — a thin doc's perception memory rolls off on the
    same clock as content memory. NON-document graph_nodes kinds (entities: work / person / ...) are
    EXEMPT: entities persist indefinitely (design's lifecycle section — small rows, relation memory
    may outlive content memory)."""
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
        identities = {(source, source_id): "full" for source, source_id in stale}
        for (nid,) in stale_thin:
            parts = nid.split(":", 2)
            if len(parts) != 3 or parts[0] != "doc" or not parts[1] or not parts[2]:
                logger.warning("recall roll-off kept malformed document node id %r", nid)
                continue
            identities.setdefault((parts[1], parts[2]), "thin")

        journal = _journal()
        materialized_through = _materialization_cursor(con, journal)
        appended = 0
        pending = 0
        failed = 0
        from omniseek.core import fetcher
        for (source, source_id), lane in sorted(identities.items()):
            privacy_namespace = "walled" if fetcher.is_walled_source(source) else "public"
            try:
                receipt = journal.append_tombstone(
                    source=source,
                    source_id=source_id,
                    observed_at=time.time(),
                    provenance="sweep",
                    privacy_namespace=privacy_namespace,
                    reason="expired",
                    lane=lane,
                    materialized_through=materialized_through,
                )
            except Exception as exc:  # noqa: BLE001 -- unjournaled rows must remain materialized
                failed += 1
                _record_journal_failure(f"sweep tombstone {source}:{source_id}", exc)
                continue
            if receipt is None:
                pending += 1
            else:
                appended += 1
                _JOURNAL_WAKE.set()

        materialized = _materialize_pending(con, journal)
        if _materialization_cursor(con, journal) < journal.head_seq:
            _JOURNAL_WAKE.set()
        else:
            _JOURNAL_WAKE.clear()
        logger.info(
            "recall roll-off considered %d identities older than %dd: "
            "journaled=%d pending=%d failed=%d materialized=%d",
            len(identities), RETAIN_DAYS, appended, pending, failed, materialized,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("recall sweep skipped: %s", exc)
        try:
            con.rollback()
        except Exception:  # noqa: BLE001
            pass
