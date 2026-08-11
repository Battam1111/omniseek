"""Perception-memory index — the READ + schema half of the eye's ``recall`` sub-layer.

The eye is otherwise STATELESS (fetch-live, TTL-cache, forget). This sub-layer makes the
ENUMERABLE sources STATEFUL: their docs are continuously ingested into a local SQLite FTS5
index so a query can recall them sub-second, OFFLINE, cross-source — including items the live
feeds have since rolled off. It is HYBRID with the live query-keyed sources, never a replacement.

THE RAZOR holds: this layer is pure RECALL. FTS5 only bounds the candidate pool (its global-IDF
``bm25`` is never surfaced); the agent-facing SCORE stays in ``rank.merge_rank`` /
``relevance.doc_scores``, identical for index- and live-sourced docs. Nothing here judges.

CJK (verified on the mini): the FTS ``seg`` column stores ``relevance.tokenize`` output (ASCII
words + OVERLAPPING CJK bigrams) and queries segment IDENTICALLY via ``relevance.query_terms`` —
so index tokenization is provably the same as the live BM25 tokenizer (the codebase's anti-drift
invariant), giving exact Chinese recall with ZERO new dependency (模型 21/21 vs trigram 0/21).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from penumbra.core import relevance
from penumbra.core.normalize import Document

logger = logging.getLogger(__name__)

DB_PATH = Path.home() / ".penumbra" / "state" / "index.db"
_SCHEMA_VERSION = "2"  # 2 = + vec table (Phase-2 vector layer; additive, CREATE IF NOT EXISTS)
# Tokenizer/segment version: bumped when the seg-column DERIVATION changes (NOT the tokenizer
# itself), so the writer RE-SEGMENTS an existing doc whose stored seg_version != this on the next
# ingest. 1 = title+content; 2 = title+content+tags (a doc's tags, e.g. ircc's "express-entry",
# now enter the FTS so "express entry" recalls ircc); 3 = tokenize gains Hangul bigrams + non-Latin
# letter-run tokens (Korean was invisible to the lexical layer; café-class words were mangled).
SEG_VERSION = 3

# Fail-OPEN switch: if schema init or a connection ever fails, the whole layer becomes a no-op and
# the eye runs exactly as it did before (stateless). A bad index file must NEVER take the eye down.
_disabled = False
_local = threading.local()  # per-thread read connection (sqlite connections aren't thread-shareable)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs(
  rowid        INTEGER PRIMARY KEY,
  source       TEXT NOT NULL,
  source_id    TEXT NOT NULL,
  fp           TEXT,                      -- rank.fingerprint (read-time cross-source dedup aid)
  url TEXT, title TEXT, content TEXT, author TEXT, date TEXT, score INTEGER,
  doc_json     TEXT NOT NULL,            -- Document model_dump(json) MINUS metadata['raw']
  seg          TEXT NOT NULL,            -- relevance.tokenize(title+content+tags) joined (CJK shadow)
  seg_version  INTEGER DEFAULT 1,        -- SEG_VERSION at last write; a mismatch forces re-segment
  content_hash TEXT,                     -- change detection (sha256 of title+content+tags)
  first_seen   REAL, last_seen REAL, version INTEGER DEFAULT 1, immutable INTEGER DEFAULT 0,
  UNIQUE(source, source_id)             -- the logical identity; upsert key
);
CREATE INDEX IF NOT EXISTS docs_fp  ON docs(fp);
CREATE INDEX IF NOT EXISTS docs_src ON docs(source, last_seen);
-- external-content FTS5 over a SINGLE searchable column (seg). title/content live in docs only.
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
  seg, content='docs', content_rowid='rowid', tokenize='unicode61'
);
CREATE TABLE IF NOT EXISTS ingest_runs(source TEXT PRIMARY KEY, ran_at REAL, doc_count INTEGER);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
-- Phase-2 vector layer: one float32 L2-normalized embedding per doc, stored as a BLOB in the SAME
-- DB (commits atomically with the doc row — a crash never leaves a doc with a stale/missing vector,
-- which a sidecar .npy two-phase write would). model_version gates the live matrix (cross-space cosine
-- is mechanically impossible); rowid == docs.rowid is the alignment key.
CREATE TABLE IF NOT EXISTS vec(
  rowid         INTEGER PRIMARY KEY,
  model_version TEXT NOT NULL,
  dim           INTEGER NOT NULL,
  v             BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS vec_mv ON vec(model_version);
-- P7 thin-row title embeddings: one float32 L2-normalized embedding per THIN document node (a doc
-- from a NON-indexed source, which has a title but no content row in docs). Keyed by the graph
-- node_id (``doc:{source}:{source_id}``), NOT a docs.rowid (a thin row has none). Mirrors ``vec``'s
-- format EXACTLY (model_version gates the live matrix; a model swap drops the old space out; the
-- SAME embedder + MODEL_VERSION produce both), so ``similar`` can rank a thin title against an
-- indexed doc in ONE cosine space. This lets ``similar`` (P5) see the WHOLE perception history, not
-- just the indexed subset. DELIBERATE NON-GOAL: vec_thin does NOT feed penumbra_search's recall arm (see
-- the writer thin lane + the search-path tripwire); similar (and future P5 consumers) only.
CREATE TABLE IF NOT EXISTS vec_thin(
  node_id       TEXT PRIMARY KEY,
  model_version TEXT NOT NULL,
  dim           INTEGER NOT NULL,
  v             BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS vec_thin_mv ON vec_thin(model_version);
-- Chunk embeddings: ADDITIONAL passage-level vectors for a LONG doc's TAIL (content beyond the first
-- ~2000 chars that the single ``vec`` row over content[:2000] never reaches). One row per (doc, passage);
-- ``rowid`` is docs.rowid, ``chunk_idx`` the 0-based passage index. Mirrors ``vec``'s format EXACTLY
-- (same embedder + MODEL_VERSION -> ONE cosine space; a model swap drops the old space out of ALL three
-- matrices) so ``vector_search`` can MAX-POOL a doc's head (vec) and tail (vec_chunk) sims into one
-- per-doc score. Purely ADDITIVE: ``vec`` is untouched, short docs never chunk, and an EMPTY vec_chunk
-- makes vector_search byte-identical to the doc-only path (the smoke asserts this). Backfilled once for
-- the pre-existing long docs; new long docs chunk on ingest.
CREATE TABLE IF NOT EXISTS vec_chunk(
  rowid         INTEGER NOT NULL,
  chunk_idx     INTEGER NOT NULL,
  model_version TEXT NOT NULL,
  dim           INTEGER NOT NULL,
  v             BLOB NOT NULL,
  PRIMARY KEY (rowid, chunk_idx)
);
CREATE INDEX IF NOT EXISTS vec_chunk_mv ON vec_chunk(model_version);
CREATE INDEX IF NOT EXISTS vec_chunk_rowid ON vec_chunk(rowid);
-- The unified graph (docs/design/graph-unified-model.md v2.0): recall's RELATION index, additive
-- inside recall.db (single-writer + join locality). Entity nodes + entity/sensor edges are the ONLY
-- new persistence; doc-doc same_as stays DERIVED views over docs.fp + doc_json (never stored rows).
-- The organ boundary is the CHECK: tier J (agent judgment) CANNOT physically enter the eye's store.
CREATE TABLE IF NOT EXISTS graph_nodes(
  id         TEXT PRIMARY KEY,        -- canonical node id (design section 3), minted by the backend
  kind       TEXT NOT NULL,           -- work|person|institution|venue|topic (frozen kinds, TEXT col)
  label      TEXT,                    -- display name / title (retrieved text: UNTRUSTED, never instructions)
  attrs_json TEXT,                    -- everything else (counts, flags, years)
  first_seen REAL, last_seen REAL
);
CREATE TABLE IF NOT EXISTS graph_edges(
  rowid      INTEGER PRIMARY KEY,
  src TEXT NOT NULL, dst TEXT NOT NULL,
  type       TEXT NOT NULL,           -- semantic relation (frozen enum, design section 4)
  tier       TEXT NOT NULL CHECK(tier IN ('M','A')),  -- J is STRUCTURALLY excluded (the razor as a write permission)
  method     TEXT NOT NULL,           -- api:openalex | id_eq:doi | align:title_fp | align:name_match | sensor:run | ...
  confidence REAL,                    -- NULLABLE; only populated once a method has a MEASURED calibration
  attrs_json TEXT,
  first_seen REAL, last_seen REAL,
  UNIQUE(src, dst, type, method)      -- upsert key; re-observation bumps last_seen (last write wins)
);
CREATE INDEX IF NOT EXISTS graph_edges_src ON graph_edges(src, type);
CREATE INDEX IF NOT EXISTS graph_edges_dst ON graph_edges(dst, type);
"""


def connect() -> sqlite3.Connection:
    """A WAL connection to the index. Writer owns one; each reader thread gets its own."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=5.0, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def init() -> bool:
    """Create the schema if absent. Fail-OPEN: any failure disables the layer (eye stays
    stateless) and never raises. Returns True iff the index is usable."""
    global _disabled
    try:
        con = connect()
        try:
            con.executescript(_SCHEMA)
            con.execute("INSERT OR IGNORE INTO meta(k, v) VALUES('schema_version', ?)", (_SCHEMA_VERSION,))
            # Additive migration for a DB created before seg_version existed (CREATE TABLE IF NOT
            # EXISTS never alters an extant table). Default 1 so every legacy row reads as seg
            # v1 and re-segments to v2 (tags-in-seg) on its next ingest.
            cols = {r[1] for r in con.execute("PRAGMA table_info(docs)").fetchall()}
            if "seg_version" not in cols:
                con.execute("ALTER TABLE docs ADD COLUMN seg_version INTEGER DEFAULT 1")
            con.commit()
        finally:
            con.close()
        logger.info("recall index ready at %s", DB_PATH)
        return True
    except Exception as exc:  # noqa: BLE001 — never crash boot on a bad index
        logger.warning("recall index init failed -> DISABLED (eye stays stateless): %s", exc)
        _disabled = True
        return False


def _read_con() -> Optional[sqlite3.Connection]:
    if _disabled:
        return None
    con = getattr(_local, "con", None)
    if con is None:
        try:
            con = connect()
            _local.con = con
        except Exception as exc:  # noqa: BLE001
            logger.debug("recall read-conn open failed: %s", exc)
            return None
    return con


def segment(text: str) -> str:
    """The eye's OWN tokenizer (ASCII words + overlapping CJK bigrams), space-joined — so the
    index segments byte-identically to the live BM25 scorer. Stored in docs.seg / fts.seg."""
    return " ".join(relevance.tokenize(text or ""))


def _tags_text(doc: Document) -> str:
    """A doc's tags as one space-joined string (e.g. ircc's 'express-entry'), or '' when none.
    Byte-identical-when-no-tags: an empty/absent tags list contributes nothing."""
    tags = getattr(doc, "tags", None) or []
    return " ".join(str(t) for t in tags if t)


def segment_doc(doc: Document) -> str:
    """Segment a doc's full searchable surface: title + content + TAGS (SEG_VERSION 2). The tags
    join makes a doc recallable by its tag terms (the missing 'express entry' → ircc hit). Falls
    back to title+content alone when there are no tags (so the seg is byte-identical to v1)."""
    return segment(((doc.title or "") + " " + (doc.content or "") + " " + _tags_text(doc)).strip())


def content_hash(doc: Document) -> str:
    """Change-detection hash over the full seg surface (title + content + TAGS): a tags-only edit
    (no title/content change) now counts as a change, so the doc re-indexes its new tag terms."""
    h = hashlib.sha256()
    h.update(((doc.title or "") + "\x00" + (doc.content or "")
              + "\x00" + _tags_text(doc)).encode("utf-8"))
    return h.hexdigest()


def _match_expr(query: str) -> Optional[str]:
    """OR-of-terms (NOT a phrase): verified to equal the live ``relevance.doc_scores>0`` set on
    real bilingual content (a phrase match drops valid hits). Each term is a tokenize() unit, so a
    2-char Chinese term is one bigram token and matches anywhere it occurs."""
    terms = relevance.query_terms(query or "")
    parts = ['"' + t.replace('"', '') + '"' for t in terms if t.strip()]
    return " OR ".join(parts) if parts else None


def search(query: str, k: int = 60, sources: "Optional[frozenset[str]]" = None) -> list[Document]:
    """Recall up to ``k`` candidate docs whose seg matches any query term. PURE RECALL — ``bm25``
    only bounds the pool (never surfaced); the caller re-scores via ``rank.merge_rank``. NEVER
    raises: any failure (disabled / missing db / bad row) degrades to ``[]``.

    ``sources`` (Wave 2, audit 1.1): when a NON-empty set, the source predicate is pushed INTO this
    ONE FTS query (``AND d.source IN (...)`` with PARAMETERIZED placeholders, never interpolated
    values), so the candidate pool is scoped BEFORE bm25 ranks + LIMITs it. ``sources=None`` keeps the
    exact unfiltered SQL (byte-identical for every existing caller)."""
    if _disabled:
        return []
    expr = _match_expr(query)
    if not expr:
        return []
    con = _read_con()
    if con is None:
        return []
    # ONE query, one predicate added: sources=None -> src_pred='' -> the exact pre-Wave-2 SQL.
    src_pred = ""
    params: list = [expr]
    if sources:
        src_list = list(sources)
        src_pred = " AND d.source IN (%s)" % ",".join("?" * len(src_list))
        params.extend(src_list)
    params.append(k)
    try:
        rows = con.execute(
            "SELECT d.doc_json, d.last_seen, d.source FROM fts "
            "JOIN docs d ON fts.rowid = d.rowid WHERE fts MATCH ?" + src_pred
            + " ORDER BY bm25(fts) LIMIT ?",
            params,
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug("recall search failed: %s", exc)
        return []
    out: list[Document] = []
    now = time.time()
    for doc_json, last_seen, source in rows:
        try:
            d = Document.model_validate(json.loads(doc_json))
        except Exception:  # noqa: BLE001 — skip a corrupt row, never fail the whole recall
            continue
        ran = _ran_at(con, source)
        d.metadata = dict(d.metadata or {})
        d.metadata["from_index"] = True
        d.metadata["recall_via"] = "lexical"
        if last_seen:
            d.metadata["index_age_days"] = round((now - last_seen) / 86400, 1)
        # still_live = seen in this source's most recent full ingest (advisory; the agent judges)
        d.metadata["still_live"] = bool(ran and last_seen and last_seen >= ran)
        out.append(d)
    return out


def _ran_at(con: sqlite3.Connection, source: str) -> Optional[float]:
    try:
        r = con.execute("SELECT ran_at FROM ingest_runs WHERE source = ?", (source,)).fetchone()
        return r[0] if r else None
    except Exception:  # noqa: BLE001
        return None


def as_of() -> Optional[float]:
    """Newest ingest watermark across all sources (for _meta.index.as_of)."""
    con = _read_con()
    if con is None:
        return None
    try:
        r = con.execute("SELECT max(ran_at) FROM ingest_runs").fetchone()
        return r[0] if r and r[0] else None
    except Exception:  # noqa: BLE001
        return None


def doc_count() -> int:
    con = _read_con()
    if con is None:
        return 0
    try:
        return con.execute("SELECT count(*) FROM docs").fetchone()[0]
    except Exception:  # noqa: BLE001
        return 0


# ── Phase-2 vector layer: a per-model_version float32 matrix cached in module state ──────────────
try:
    import numpy as _np
except Exception:  # noqa: BLE001 — no numpy → no vector layer (lexical path is unaffected)
    _np = None

_vec_M = None          # (N, DIM) float32, L2-normalized rows
_vec_ids = None        # (N,) int64 docs.rowid aligned to _vec_M rows
_vec_srcs = None       # (N,) object array of docs.source aligned to _vec_M rows (Wave 2 source scope)
_vec_built_ts = 0.0
_vec_built_gen = -1
_vec_built_mv = ""
_vec_lock = threading.Lock()
_VEC_DEBOUNCE = 20.0   # under a burst of ingest, rebuild the matrix at most every 20s and serve
                       # slightly-stale otherwise (it is RECALL; merge_rank re-scores anyway)

# P7 thin matrix: the SAME cache pattern for vec_thin (thin-row title embeddings), a SEPARATE cache
# with its OWN invalidation (keyed on the _thin_write_gen counter), so a vec_thin write refreshes
# this matrix without disturbing the docs vec matrix and vice versa. Same
# model_version gate (a model swap drops the old space out of BOTH). Rows here key on the graph
# node_id string (a thin row has no docs.rowid), so ids are an object array of strings, not int64.
_thin_M = None         # (N, DIM) float32, L2-normalized rows
_thin_ids = None       # (N,) object array of node_id strings aligned to _thin_M rows
_thin_built_ts = 0.0
_thin_built_gen = -1
_thin_built_mv = ""
_thin_lock = threading.Lock()

# Chunk matrix: the SAME cache pattern for vec_chunk (a long doc's TAIL passages), a SEPARATE cache
# with its OWN invalidation (keyed on _chunk_write_gen) so a chunk write refreshes THIS matrix without
# disturbing the docs / thin matrices. ids are int64 docs.rowid WITH DUPLICATES (N passages per doc);
# srcs is the per-chunk doc source (masked in lockstep, like _vec_srcs), so vector_search can max-pool
# a doc's head + tail sims into ONE per-doc score AND source-scope the chunk rows.
_chunk_M = None        # (N, DIM) float32, L2-normalized rows
_chunk_ids = None      # (N,) int64 docs.rowid per chunk row (duplicated across a doc's passages)
_chunk_srcs = None     # (N,) object array of docs.source per chunk row (scope mask)
_chunk_built_ts = 0.0
_chunk_built_gen = -1
_chunk_built_mv = ""
_chunk_lock = threading.Lock()


def _model_version() -> str:
    from penumbra.core.recall import embed  # local: vector path only
    return embed.MODEL_VERSION


# Monotonic write generations, bumped by the SINGLE recall-writer daemon on any vec / vec_thin
# write. This REPLACES row-count as the matrix-invalidation key: a RE-EMBED (delete+reinsert the
# same rowid) leaves count(*) unchanged, so the old count key served a STALE vector until an
# unrelated INSERT drifted the count. The write-gen bumps on every write, so a re-embed invalidates
# correctly. Single writer -> a plain int increment is race-free; readers only compare it.
_vec_write_gen = 0
_thin_write_gen = 0
_chunk_write_gen = 0


def note_vec_write() -> None:
    """Writer daemon: signal that vec rows changed (insert OR re-embed), invalidating the docs matrix."""
    global _vec_write_gen
    _vec_write_gen += 1


def note_thin_write() -> None:
    """Writer daemon: signal that vec_thin rows changed, invalidating the thin matrix."""
    global _thin_write_gen
    _thin_write_gen += 1


def note_chunk_write() -> None:
    """Writer daemon: signal that vec_chunk rows changed, invalidating the chunk matrix."""
    global _chunk_write_gen
    _chunk_write_gen += 1


def _ensure_matrix(con):
    """Build/refresh the cached vector matrix for the CURRENT model_version (debounced). A model
    change forces a full rebuild and the old space drops out. Returns (M, ids) or (None, None).

    Wave 2 (audit 1.1): the same build ALSO caches ``_vec_srcs``, a per-row source array aligned with
    ``ids`` (docs.source for each vec row), so ``vector_search`` can mask off-scope rows BEFORE the
    top-k. It is refreshed under the SAME lock / debounce / write-generation invalidation as the
    matrix (built and swapped in lockstep), and stays a module global so the (M, ids) return signature
    the other consumers (similar_anchor / similar_neighbors) rely on is unchanged."""
    global _vec_M, _vec_ids, _vec_srcs, _vec_built_ts, _vec_built_gen, _vec_built_mv
    if _np is None:
        return None, None
    mv = _model_version()
    gen = _vec_write_gen  # write-gen, not row-count: catches re-embeds the count key missed
    now = time.time()
    with _vec_lock:
        same_space = (_vec_M is not None and _vec_built_mv == mv)
        if same_space and _vec_built_gen == gen:
            return _vec_M, _vec_ids
        if same_space and (now - _vec_built_ts) < _VEC_DEBOUNCE:
            return _vec_M, _vec_ids  # serve slightly-stale during an ingest burst
        try:
            rows = con.execute(
                "SELECT v.rowid, v.v, d.source FROM vec v JOIN docs d ON v.rowid = d.rowid "
                "WHERE v.model_version = ?", (mv,)).fetchall()
        except Exception:  # noqa: BLE001
            return None, None
        if not rows:
            _vec_M, _vec_ids, _vec_srcs, _vec_built_ts, _vec_built_gen, _vec_built_mv = (
                None, None, None, now, gen, mv)
            return None, None
        try:
            ids = _np.fromiter((r[0] for r in rows), dtype=_np.int64, count=len(rows))
            M = _np.frombuffer(b"".join(r[1] for r in rows), dtype=_np.float32).reshape(len(rows), -1)
            srcs = _np.array([r[2] for r in rows], dtype=object)  # aligned per-row source (scope mask)
            nrm = _np.linalg.norm(M, axis=1, keepdims=True)
            M = (M / _np.where(nrm > 0, nrm, 1.0)).astype(_np.float32)
        except Exception as exc:  # noqa: BLE001
            logger.debug("recall matrix build failed: %s", exc)
            return None, None
        _vec_M, _vec_ids, _vec_srcs, _vec_built_ts, _vec_built_gen, _vec_built_mv = (
            M, ids, srcs, now, gen, mv)
        return M, ids


def _ensure_thin_matrix(con):
    """Build/refresh the cached THIN vector matrix for the CURRENT model_version (P7). The exact
    mirror of ``_ensure_matrix`` for ``vec_thin`` — a SEPARATE cache + SEPARATE invalidation (keyed on
    the ``_thin_write_gen`` counter, plus the same debounce) so a thin write refreshes THIS matrix and
    a docs-vec write refreshes the OTHER, neither disturbing the other. Rows key on the
    node_id STRING (a thin row has no docs.rowid), so ``ids`` is an object array of strings. Returns
    (M, ids) or (None, None); fail-open like the docs matrix (a bad row/space -> no thin matrix)."""
    global _thin_M, _thin_ids, _thin_built_ts, _thin_built_gen, _thin_built_mv
    if _np is None:
        return None, None
    mv = _model_version()
    gen = _thin_write_gen  # write-gen, not row-count: catches re-embeds the count key missed
    now = time.time()
    with _thin_lock:
        same_space = (_thin_M is not None and _thin_built_mv == mv)
        if same_space and _thin_built_gen == gen:
            return _thin_M, _thin_ids
        if same_space and (now - _thin_built_ts) < _VEC_DEBOUNCE:
            return _thin_M, _thin_ids  # serve slightly-stale during an ingest burst
        try:
            rows = con.execute(
                "SELECT node_id, v FROM vec_thin WHERE model_version = ?", (mv,)
            ).fetchall()
        except Exception:  # noqa: BLE001
            return None, None
        if not rows:
            _thin_M, _thin_ids, _thin_built_ts, _thin_built_gen, _thin_built_mv = None, None, now, gen, mv
            return None, None
        try:
            ids = _np.array([r[0] for r in rows], dtype=object)
            M = _np.frombuffer(b"".join(r[1] for r in rows), dtype=_np.float32).reshape(len(rows), -1)
            nrm = _np.linalg.norm(M, axis=1, keepdims=True)
            M = (M / _np.where(nrm > 0, nrm, 1.0)).astype(_np.float32)
        except Exception as exc:  # noqa: BLE001
            logger.debug("recall thin matrix build failed: %s", exc)
            return None, None
        _thin_M, _thin_ids, _thin_built_ts, _thin_built_gen, _thin_built_mv = M, ids, now, gen, mv
        return M, ids


def _ensure_chunk_matrix(con):
    """Build/refresh the cached CHUNK vector matrix for the CURRENT model_version. Mirror of
    ``_ensure_matrix`` for ``vec_chunk``: a SEPARATE cache + SEPARATE invalidation (``_chunk_write_gen``
    + the same debounce), the SAME model_version gate. ``ids`` are int64 docs.rowid PER CHUNK ROW
    (duplicated across a doc's passages); ``_chunk_srcs`` is the per-chunk doc source, cached in lockstep
    for the scope mask. Returns (M, ids) or (None, None); fail-open like the docs matrix."""
    global _chunk_M, _chunk_ids, _chunk_srcs, _chunk_built_ts, _chunk_built_gen, _chunk_built_mv
    if _np is None:
        return None, None
    mv = _model_version()
    gen = _chunk_write_gen
    now = time.time()
    with _chunk_lock:
        same_space = (_chunk_M is not None and _chunk_built_mv == mv)
        if same_space and _chunk_built_gen == gen:
            return _chunk_M, _chunk_ids
        if same_space and (now - _chunk_built_ts) < _VEC_DEBOUNCE:
            return _chunk_M, _chunk_ids  # serve slightly-stale during an ingest burst
        try:
            rows = con.execute(
                "SELECT c.rowid, c.v, d.source FROM vec_chunk c JOIN docs d ON c.rowid = d.rowid "
                "WHERE c.model_version = ?", (mv,)).fetchall()
        except Exception:  # noqa: BLE001
            return None, None
        if not rows:
            _chunk_M, _chunk_ids, _chunk_srcs, _chunk_built_ts, _chunk_built_gen, _chunk_built_mv = (
                None, None, None, now, gen, mv)
            return None, None
        try:
            ids = _np.fromiter((r[0] for r in rows), dtype=_np.int64, count=len(rows))
            M = _np.frombuffer(b"".join(r[1] for r in rows), dtype=_np.float32).reshape(len(rows), -1)
            srcs = _np.array([r[2] for r in rows], dtype=object)
            nrm = _np.linalg.norm(M, axis=1, keepdims=True)
            M = (M / _np.where(nrm > 0, nrm, 1.0)).astype(_np.float32)
        except Exception as exc:  # noqa: BLE001
            logger.debug("recall chunk matrix build failed: %s", exc)
            return None, None
        _chunk_M, _chunk_ids, _chunk_srcs, _chunk_built_ts, _chunk_built_gen, _chunk_built_mv = (
            M, ids, srcs, now, gen, mv)
        return M, ids


def vector_search(qvec, k: int = 60, sources: "Optional[frozenset[str]]" = None) -> list:
    """Cosine top-k over the cached matrix. PURE RECALL — the cosine is never surfaced; the caller
    re-scores via rank.merge_rank. NEVER raises: disabled / no numpy / no matrix / dim-mismatch
    (model swap mid-flight) / any failure → [].

    ``sources`` (Wave 2, audit 1.1): when a NON-empty set, off-scope rows are masked to -inf BEFORE
    the argpartition top-k (using ``_vec_srcs``, the source array cached in lockstep with the matrix),
    so an in-scope doc that sits DEEPER in the index than k off-scope docs still surfaces; a filter
    applied AFTER the top-k would miss it. Masked (-inf) survivors are dropped from the final slice, so
    an all-masked matrix returns []. ``sources=None`` is byte-identical to the pre-Wave-2 path.

    P7 DELIBERATE NON-GOAL: this is penumbra_search's recall arm, and it consults ONLY ``vec`` (the docs
    matrix) — NOT ``vec_thin``. Folding thin-title embeddings into search recall is a SEPARATE
    decision with its own dogfood; do not add ``_ensure_thin_matrix`` here (the smoke asserts this
    structurally). vec_thin feeds ``graph.similar`` (and future P5 consumers) only."""
    if _disabled or qvec is None or _np is None:
        return []
    con = _read_con()
    if con is None:
        return []
    _ensure_matrix(con)  # build/refresh; then read the aligned triple as a UNIT below
    # Read (M, ids, srcs) TOGETHER under _vec_lock: the three globals are only ever assigned as one
    # tuple under this lock, so a locked group-read always yields a CONSISTENT triple. Reading srcs
    # separately after the call left a window where a concurrent rebuild (e.g. a same-count re-embed)
    # could swap in a differently-aligned array and the mask would scope the WRONG rows silently.
    with _vec_lock:
        M, ids, srcs = _vec_M, _vec_ids, _vec_srcs
    if M is None or ids is None or len(ids) == 0:
        return []
    try:
        q = _np.asarray(qvec, dtype=_np.float32).ravel()
        if q.shape[0] != M.shape[1]:
            return []
        n = _np.linalg.norm(q)
        if n > 0:
            q = q / n
        sims = M @ q
        if sources:
            # Scope BEFORE the top-k: any row whose source is not requested is pushed to -inf so
            # argpartition can never spend a slot on it (the 1.1 recall-bypass fix). srcs is the
            # per-row source array read in lockstep with M/ids above; a length mismatch (out-of-band
            # corruption) fails CLOSED here rather than mis-scoping.
            if srcs is None or len(srcs) != len(ids):
                return []
            sims[~_np.isin(srcs, list(sources))] = -_np.inf
        # CHUNK max-pool (long-doc tail recall): CONCATENATE the chunk rows so a doc surfaces if its
        # HEAD (vec) OR any TAIL passage (vec_chunk) matches; the per-rowid dedup below keeps the MAX
        # (a doc's best passage), so passages never flood top-k. When vec_chunk is empty (Mc is None)
        # all_sims/all_ids are the doc-only arrays -> byte-identical to the pre-chunk path.
        all_sims, all_ids = sims, ids
        _ensure_chunk_matrix(con)
        with _chunk_lock:
            Mc, ids_c, srcs_c = _chunk_M, _chunk_ids, _chunk_srcs
        if Mc is not None and ids_c is not None and len(ids_c) and Mc.shape[1] == q.shape[0]:
            sims_c = Mc @ q
            if sources and (srcs_c is None or len(srcs_c) != len(ids_c)):
                sims_c = None  # cannot honestly scope the chunk rows -> fall back to doc-only
            elif sources:
                sims_c[~_np.isin(srcs_c, list(sources))] = -_np.inf
            if sims_c is not None:
                all_sims = _np.concatenate([sims, sims_c])
                all_ids = _np.concatenate([ids, ids_c])
        # Over-fetch (chunks compete for slots, then collapse per doc), sort desc, dedup by rowid keeping
        # the highest (max-pool), take k. With no chunks this reduces to the original top-k exactly.
        pool = min(max(k * 4, k), len(all_ids))
        top = _np.argpartition(-all_sims, pool - 1)[:pool]
        top = top[_np.argsort(-all_sims[top])]
        seen: set = set()
        rowids: list = []
        for i in top:
            if all_sims[i] == -_np.inf:  # drop masked-out survivors
                continue
            rid = int(all_ids[i])
            if rid in seen:
                continue
            seen.add(rid)
            rowids.append(rid)
            if len(rowids) >= k:
                break
    except Exception as exc:  # noqa: BLE001
        logger.debug("vector_search failed: %s", exc)
        return []
    return _hydrate_rowids(con, rowids, recall_via="vector")


def anchor_rowid(source: str, source_id: str) -> Optional[int]:
    """The ``docs.rowid`` for one ``(source, source_id)`` — the row identity the vector matrix aligns
    on (``vec.rowid == docs.rowid``). None when the doc is not an indexed row (a thin/non-indexed
    doc has no rowid, hence no vector). Fail-open to None."""
    con = _read_con()
    if con is None:
        return None
    try:
        row = con.execute(
            "SELECT rowid FROM docs WHERE source = ? AND source_id = ?", (source, source_id)
        ).fetchone()
        return int(row[0]) if row else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("anchor_rowid failed: %s", exc)
        return None


# ── P7: the generalized anchor/neighbor engine (similar covers the WHOLE perception history) ──────
# The engine resolves an anchor from EITHER store (docs vec by rowid, vec_thin by node id) and ranks
# across the UNION of both matrices, so an indexed Chinese post and a thin arXiv original — exactly
# the cross-boundary pair similar exists for — land in ONE cosine space. Identity is the graph
# node_id string (``doc:{source}:{source_id}``) in both cases, so the caller never needs to know
# which store a candidate came from. (The interim docs-only similar_by_rowid engine was deleted at
# the P7 gate: no caller survived the migration, and dead code earns no slot.)

def _anchor_node_id(source: str, source_id: str) -> str:
    from penumbra.core.recall.graph import doc_node_id  # local: id scheme lives in graph
    return doc_node_id(source, source_id)


def similar_anchor(source: str, source_id: str):
    """Resolve the anchor VECTOR for a doc from EITHER store (P7): the docs ``vec`` matrix by its
    docs.rowid FIRST (an indexed doc), else the ``vec_thin`` matrix by its graph node_id (a thin doc
    whose title was embedded as the writer caught up). Returns ``(anchor_vec, anchor_node_id)`` with
    the vector as the store's OWN L2-normalized row (never re-embedded — a model upgrade upgrades every
    future answer), or ``None`` when the doc has NO vector in EITHER store (un-embedded: the caller
    maps None to the coverage-line error). Fail-open to None on any failure."""
    if _disabled or _np is None:
        return None
    con = _read_con()
    if con is None:
        return None
    node_id = _anchor_node_id(source, source_id)
    try:
        # (a) docs vec by rowid (an indexed doc). anchor_rowid is None for a non-indexed doc.
        rid = anchor_rowid(source, source_id)
        if rid is not None:
            M, ids = _ensure_matrix(con)
            if M is not None and ids is not None and len(ids) > 0:
                pos = _np.where(ids == int(rid))[0]
                if pos.size > 0:
                    return M[int(pos[0])], node_id
        # (b) vec_thin by node_id (a thin doc whose title is embedded).
        Mt, tids = _ensure_thin_matrix(con)
        if Mt is not None and tids is not None and len(tids) > 0:
            tpos = _np.where(tids == node_id)[0]
            if tpos.size > 0:
                return Mt[int(tpos[0])], node_id
    except Exception as exc:  # noqa: BLE001
        logger.debug("similar_anchor failed: %s", exc)
        return None
    return None


def similar_neighbors(anchor_vec, anchor_node_id: str, k: int = 10):
    """Vector-NEAREST docs to an anchor VECTOR, ranked by cosine across the UNION of BOTH matrices
    (docs ``vec`` + ``vec_thin``), the anchor itself excluded, top-k BY RANK (k is a resource budget,
    never a score threshold — the RRF discipline). Returns ``[(node_id, title)]`` in cosine-rank
    order, each node_id a canonical ``doc:{source}:{source_id}`` and title from the store the row came
    from (docs.title for an indexed hit, graph_nodes.label for a thin hit). Deterministic tie order:
    cosine DESC, then node_id ASC (so equal-cosine neighbors keep a fixed order across runs). Fail-open
    to ``[]`` on any failure (never an exception). The anchor vector must be L2-normalized (both stores
    store normalized rows; similar_anchor returns such a row)."""
    if _disabled or _np is None or anchor_vec is None:
        return []
    con = _read_con()
    if con is None:
        return []
    try:
        q = _np.asarray(anchor_vec, dtype=_np.float32).ravel()
        n = _np.linalg.norm(q)
        if n > 0:
            q = q / n
        scored: list = []   # (cosine, node_id) across both stores, self excluded
        docs_rowids: list = []
        # (a) docs vec matrix: ids are rowids; map to node_id + title AFTER ranking.
        M, ids = _ensure_matrix(con)
        if M is not None and ids is not None and len(ids) > 0 and M.shape[1] == q.shape[0]:
            sims = M @ q
            for i in range(len(ids)):
                docs_rowids.append(int(ids[i]))
        else:
            sims = None
        # (b) thin matrix: ids are node_id strings; self-exclude by node_id here.
        Mt, tids = _ensure_thin_matrix(con)
        thin_pairs: list = []
        if Mt is not None and tids is not None and len(tids) > 0 and Mt.shape[1] == q.shape[0]:
            tsims = Mt @ q
            for i in range(len(tids)):
                nid = str(tids[i])
                if nid == anchor_node_id:
                    continue   # exclude the anchor itself
                thin_pairs.append((float(tsims[i]), nid))
        else:
            tsims = None
    except Exception as exc:  # noqa: BLE001
        logger.debug("similar_neighbors ranking failed: %s", exc)
        return []
    # Resolve docs rowids -> node_id + title in ONE query, self-excluded by node_id.
    docs_titles: dict = {}
    docs_nid: dict = {}
    if docs_rowids:
        qmarks = ",".join("?" * len(docs_rowids))
        try:
            for rid, dsource, dsid, dtitle in con.execute(
                f"SELECT rowid, source, source_id, title FROM docs WHERE rowid IN ({qmarks})",
                docs_rowids,
            ).fetchall():
                nid = _anchor_node_id(dsource, dsid)
                docs_nid[int(rid)] = nid
                docs_titles[nid] = dtitle
        except Exception as exc:  # noqa: BLE001
            logger.debug("similar_neighbors docs hydrate failed: %s", exc)
    if sims is not None:
        for i, rid in enumerate(docs_rowids):
            nid = docs_nid.get(rid)
            if nid is None or nid == anchor_node_id:
                continue
            scored.append((float(sims[i]), nid))
    # thin candidates carry their title from graph_nodes.label.
    thin_titles: dict = {}
    if thin_pairs:
        tmarks = ",".join("?" * len(thin_pairs))
        tnids = [nid for (_c, nid) in thin_pairs]
        try:
            for nid, label in con.execute(
                f"SELECT id, label FROM graph_nodes WHERE id IN ({tmarks})", tnids
            ).fetchall():
                thin_titles[nid] = label
        except Exception as exc:  # noqa: BLE001
            logger.debug("similar_neighbors thin hydrate failed: %s", exc)
        scored.extend(thin_pairs)
    if not scored:
        return []
    # Cross-store dedup by node_id, keeping the best cosine: a doc normally lives in exactly ONE
    # store (the source-level indexable predicate), but a reclassified source can leave a stale
    # vec_thin row beside a fresh docs/vec row until roll-off, and the same id must never appear
    # as two candidates with two ranks.
    best: dict = {}
    for cos, nid in scored:
        if nid not in best or cos > best[nid]:
            best[nid] = cos
    deduped = [(cos, nid) for nid, cos in best.items()]
    # Deterministic UNION rank: cosine DESC, node_id ASC as the stable tie-break.
    deduped.sort(key=lambda s: (-s[0], s[1]))
    kk = max(1, int(k))
    out: list = []
    for _cos, nid in deduped[:kk]:
        title = docs_titles.get(nid) if nid in docs_titles else thin_titles.get(nid)
        out.append((nid, title))
    return out


def _hydrate_one(con, doc_json, last_seen, source, recall_via, now=None):
    try:
        d = Document.model_validate(json.loads(doc_json))
    except Exception:  # noqa: BLE001
        return None
    now = now if now is not None else time.time()
    ran = _ran_at(con, source)
    d.metadata = dict(d.metadata or {})
    d.metadata["from_index"] = True
    d.metadata["recall_via"] = recall_via
    if last_seen:
        d.metadata["index_age_days"] = round((now - last_seen) / 86400, 1)
    d.metadata["still_live"] = bool(ran and last_seen and last_seen >= ran)
    return d


def _hydrate_rowids(con, rowids, recall_via: str) -> list:
    """Reconstruct Documents by rowid, preserving the given (cosine-rank) order."""
    if not rowids:
        return []
    qmarks = ",".join("?" * len(rowids))
    try:
        rows = con.execute(
            f"SELECT rowid, doc_json, last_seen, source FROM docs WHERE rowid IN ({qmarks})",
            rowids,
        ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    by_id = {r[0]: r for r in rows}
    out = []
    now = time.time()
    for rid in rowids:
        r = by_id.get(rid)
        if not r:
            continue
        d = _hydrate_one(con, r[1], r[2], r[3], recall_via, now)
        if d is not None:
            out.append(d)
    return out
