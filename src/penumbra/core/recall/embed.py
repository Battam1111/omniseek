"""Perception-memory index — the EMBEDDER (Phase-2 vector/semantic layer).

A lazy MPS singleton mirroring ``asr._get_model``: load once on first use, try ``mps`` then ``cpu``,
keep warm. This is the ONLY place the model + its asymmetric prefix scheme live, keyed to
``MODEL_VERSION`` — a model / dim / prefix change bumps the version, the old vectors simply fall out
of the live matrix (lexical-only until backfilled), and cross-space cosine is mechanically
impossible. Import-guarded + FAIL-OPEN at every layer: if sentence-transformers is absent or the
weights won't load, the whole vector layer disables itself and the eye degrades to Phase-1 lexical
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
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

MODEL_PATH = str(Path.home() / ".polaris" / "models" / "qwen3-embedding-0.6b")
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


def _encode(texts: list[str], prefix: str) -> Optional[np.ndarray]:
    m = _get_model()
    if m is None:
        return None
    try:
        with _fwd_lock:   # one MPS forward at a time (peak-memory safety on 16GB unified)
            v = m.encode([prefix + (t or "") for t in texts], batch_size=8,
                         normalize_embeddings=True, show_progress_bar=False)
        return np.asarray(v, dtype=np.float32)
    except Exception as exc:  # noqa: BLE001 — a forward failure degrades to lexical, never raises up
        logger.warning("recall embed failed: %s", exc)
        return None


def embed_passage(texts: list[str]) -> Optional[np.ndarray]:
    """Embed documents (NO prefix). Returns (N, DIM) float32 L2-normalized, or None on failure."""
    return _encode(texts, prefix="") if texts else None


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
