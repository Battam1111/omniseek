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
# now enter the FTS so "express entry" recalls ircc).
SEG_VERSION = 2

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


def search(query: str, k: int = 60) -> list[Document]:
    """Recall up to ``k`` candidate docs whose seg matches any query term. PURE RECALL — ``bm25``
    only bounds the pool (never surfaced); the caller re-scores via ``rank.merge_rank``. NEVER
    raises: any failure (disabled / missing db / bad row) degrades to ``[]``."""
    if _disabled:
        return []
    expr = _match_expr(query)
    if not expr:
        return []
    con = _read_con()
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT d.doc_json, d.last_seen, d.source FROM fts "
            "JOIN docs d ON fts.rowid = d.rowid WHERE fts MATCH ? ORDER BY bm25(fts) LIMIT ?",
            (expr, k),
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
_vec_built_ts = 0.0
_vec_built_gen = -1
_vec_built_mv = ""
_vec_lock = threading.Lock()
_VEC_DEBOUNCE = 20.0   # under a burst of ingest, rebuild the matrix at most every 20s and serve
                       # slightly-stale otherwise (it is RECALL; merge_rank re-scores anyway)


def _model_version() -> str:
    from penumbra.core.recall import embed  # local: vector path only
    return embed.MODEL_VERSION


def _vec_count(con, mv: str) -> int:
    try:
        r = con.execute("SELECT count(*) FROM vec WHERE model_version = ?", (mv,)).fetchone()
        return r[0] if r else 0
    except Exception:  # noqa: BLE001
        return -1


def _ensure_matrix(con):
    """Build/refresh the cached vector matrix for the CURRENT model_version (debounced). A model
    change forces a full rebuild and the old space drops out. Returns (M, ids) or (None, None)."""
    global _vec_M, _vec_ids, _vec_built_ts, _vec_built_gen, _vec_built_mv
    if _np is None:
        return None, None
    mv = _model_version()
    gen = _vec_count(con, mv)
    now = time.time()
    with _vec_lock:
        same_space = (_vec_M is not None and _vec_built_mv == mv)
        if same_space and _vec_built_gen == gen:
            return _vec_M, _vec_ids
        if same_space and (now - _vec_built_ts) < _VEC_DEBOUNCE:
            return _vec_M, _vec_ids  # serve slightly-stale during an ingest burst
        try:
            rows = con.execute("SELECT rowid, v FROM vec WHERE model_version = ?", (mv,)).fetchall()
        except Exception:  # noqa: BLE001
            return None, None
        if not rows:
            _vec_M, _vec_ids, _vec_built_ts, _vec_built_gen, _vec_built_mv = None, None, now, gen, mv
            return None, None
        try:
            ids = _np.fromiter((r[0] for r in rows), dtype=_np.int64, count=len(rows))
            M = _np.frombuffer(b"".join(r[1] for r in rows), dtype=_np.float32).reshape(len(rows), -1)
            nrm = _np.linalg.norm(M, axis=1, keepdims=True)
            M = (M / _np.where(nrm > 0, nrm, 1.0)).astype(_np.float32)
        except Exception as exc:  # noqa: BLE001
            logger.debug("recall matrix build failed: %s", exc)
            return None, None
        _vec_M, _vec_ids, _vec_built_ts, _vec_built_gen, _vec_built_mv = M, ids, now, gen, mv
        return M, ids


def vector_search(qvec, k: int = 60) -> list:
    """Cosine top-k over the cached matrix. PURE RECALL — the cosine is never surfaced; the caller
    re-scores via rank.merge_rank. NEVER raises: disabled / no numpy / no matrix / dim-mismatch
    (model swap mid-flight) / any failure → []."""
    if _disabled or qvec is None or _np is None:
        return []
    con = _read_con()
    if con is None:
        return []
    M, ids = _ensure_matrix(con)
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
        kk = min(k, len(ids))
        top = _np.argpartition(-sims, kk - 1)[:kk]
        top = top[_np.argsort(-sims[top])]
        rowids = [int(ids[i]) for i in top]
    except Exception as exc:  # noqa: BLE001
        logger.debug("vector_search failed: %s", exc)
        return []
    return _hydrate_rowids(con, rowids, recall_via="vector")


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
