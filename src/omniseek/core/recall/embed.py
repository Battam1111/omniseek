"""Perception-memory index — the EMBEDDER (Phase-2 vector/semantic layer).

A lazy MPS singleton mirroring ``asr._get_model``: load once on first use, try ``mps`` then ``cpu``,
keep warm. This is the ONLY place the model + its asymmetric prefix scheme live, keyed to
``MODEL_VERSION`` — a model / dim / prefix change bumps the version, the old vectors simply fall out
of the live matrix (lexical-only until backfilled), and cross-space cosine is mechanically
impossible. Import-guarded + FAIL-OPEN at every layer: if sentence-transformers is absent or the
weights won't load, the whole vector layer disables itself and OmniSeek degrades to Phase-1 lexical
(never an error, never blocks boot). One forward ``Lock`` so a query-embed and an ingest-embed never
run two concurrent MPS forwards (the real 16GB peak).

Model chosen by an on-mini bake-off on REAL eye content (test on real data, never a benchmark):
Qwen3-Embedding-0.6B won on cross-lingual RELIABILITY — zero whiffs on 12 real code-switched queries
vs bge-m3's two, plus the widest related/unrelated cosine contrast. Local, ~0.6B, dim 1024.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

MODEL_PATH = str(Path.home() / ".omniseek" / "models" / "qwen3-embedding-0.6b")
DIM = 1024
# Qwen3-Embedding: the instruct prefix goes on the QUERY side only; documents embed raw.
_QUERY_PREFIX = "Instruct: Given a query, retrieve relevant documents.\nQuery: "
# Stamps every stored vector AND gates the live matrix. Bump on ANY model/dim/prefix change → the
# old-version vectors drop out of the matrix until re-embedded (fail-open), never a mixed space.
MODEL_VERSION = "qwen3-emb-0.6b/d1024/qprefix-v1"

_model = None
_disabled = False
_load_lock = threading.Lock()
_fwd_lock = threading.Lock()   # serialize MPS forwards (query + ingest never concurrent)
# CALIBRATED 2026-08-17 against warm forwards over real indexed text on this deployment, with the
# query embed at a 28 ms median for reference. Lock held per chunk, and cost per document:
#   chunk  1 -> 677 ms held, 677 ms/doc   fixed per-call overhead dominates; never go below 2
#   chunk  2 -> 342 ms held, 171 ms/doc
#   chunk  4 -> 445 ms held, 111 ms/doc   the knee, and what this is set to
#   chunk  8 -> 862 ms held, 108 ms/doc
#   chunk 16 -> 1489 ms held, 93 ms/doc
# Four halves the worst case a waiting query can sit behind an absorb batch, for three percent more
# cost per document. The wait is what was silently killing cross-lingual recall under concurrency;
# absorb throughput was never the constraint, so the trade goes to the wait. Re-run the calibration
# if the embedding model, its device, or the typical document length changes.
_PASSAGE_LOCK_CHUNK = 4

# Diagnostic breadcrumb for the LAST forward through _encode, whichever thread it belonged to.
# Not a per-call return value and not thread-local on purpose: the question it answers is
# "is this machine's embed path queueing right now", which is a property of the process.
LAST_TIMING: dict = {}


def available() -> bool:
    """True if the embedder is usable (not disabled). Does NOT force a load."""
    return not _disabled


def _get_model():
    """Lazy singleton. Returns the model, or None on any failure (fail-open: absent dep / missing
    weights / load error → disable the vector layer for this process, never raise)."""
    global _model, _disabled
    if _disabled:
        return None
    if _model is not None:
        return _model
    with _load_lock:
        if _model is not None:
            return _model
        if _disabled:
            return None
        try:
            if not os.path.isdir(MODEL_PATH):
                raise FileNotFoundError(f"embedder weights not at {MODEL_PATH}")
            os.environ.setdefault("HF_HUB_OFFLINE", "1")   # never hang on a network revision-check
            from sentence_transformers import SentenceTransformer  # optional dep (import-guarded)
            try:
                m = SentenceTransformer(MODEL_PATH, device="mps")
            except Exception as exc:  # noqa: BLE001 — MPS absent / OOM → CPU (the asr.py pattern)
                logger.warning("recall embedder: mps load failed (%s) → trying cpu", exc)
                m = SentenceTransformer(MODEL_PATH, device="cpu")
            _model = m
            logger.info("recall embedder ready (%s, dim=%d)", MODEL_VERSION, DIM)
            return _model
        except Exception as exc:  # noqa: BLE001 — fail-open: no vector layer, eye stays lexical
            logger.warning("recall embedder DISABLED (load failed): %s", exc)
            _disabled = True
            return None


def _encode(texts: list[str], prefix: str, chunk: Optional[int] = None) -> Optional[np.ndarray]:
    m = _get_model()
    if m is None:
        return None
    if chunk and len(texts) > chunk:
        # Take the lock PER CHUNK instead of once for the whole batch. The lock still serializes
        # forwards exactly as before; what changes is that a bulk absorb can no longer hold it for
        # tens of seconds while a query embed that needs 25 ms waits behind it. Measured holds for a
        # single un-chunked call on this machine: 2.2 s at 16 docs, 6.8 s at 64, 41.5 s at 256.
        parts = []
        for i in range(0, len(texts), chunk):
            got = _encode(texts[i:i + chunk], prefix)
            if got is None:
                return None
            parts.append(got)
        return np.concatenate(parts) if len(parts) > 1 else parts[0]
    try:
        _t0 = time.perf_counter()
        with _fwd_lock:   # one MPS forward at a time (peak-memory safety on 16GB unified)
            # INSTRUMENTED 2026-08-17: the wait and the forward are recorded separately. One query
            # embed measures 25 to 49 ms in isolation, so a multi-second embed reported by a live
            # search is a QUEUE, not a slow model, and the two are fixed in completely different
            # places. LAST_TIMING is a diagnostic breadcrumb, deliberately not a return value: it is
            # last-writer-wins across threads and must never be read as this call's own number.
            _t1 = time.perf_counter()
            v = m.encode([prefix + (t or "") for t in texts], batch_size=8,
                         normalize_embeddings=True, show_progress_bar=False)
        _t2 = time.perf_counter()
        LAST_TIMING.update(wait_ms=round((_t1 - _t0) * 1000, 1),
                           fwd_ms=round((_t2 - _t1) * 1000, 1), n=len(texts))
        return np.asarray(v, dtype=np.float32)
    except Exception as exc:  # noqa: BLE001 — a forward failure degrades to lexical, never raises up
        logger.warning("recall embed failed: %s", exc)
        return None


def embed_passage(texts: list[str]) -> Optional[np.ndarray]:
    """Embed documents (NO prefix). Returns (N, DIM) float32 L2-normalized, or None on failure.

    Chunked so the absorb path cannot hold the forward lock for the length of a whole batch. A
    query embed costs 25 to 49 ms here and must not queue behind tens of seconds of document
    work; searches feed the absorb path themselves, so an unbounded hold made heavy search
    degrade its own recall."""
    return _encode(texts, prefix="", chunk=_PASSAGE_LOCK_CHUNK) if texts else None


def embed_query(text: str) -> Optional[np.ndarray]:
    """Embed ONE query (with the instruct prefix). Returns (DIM,) float32 or None."""
    v = _encode([text or ""], prefix=_QUERY_PREFIX)
    return None if v is None else v[0]


def warm() -> None:
    """Preload the model in the background so the first real query isn't cold. Never raises."""
    try:
        _get_model()
    except Exception:  # noqa: BLE001
        pass
