"""Perception-memory index — the EMBEDDER (Phase-2 vector/semantic layer).

A lazy MPS singleton mirroring ``asr._get_model``: load once on first use, try ``mps`` then ``cpu``,
keep warm. This is the ONLY place the model + its asymmetric prefix scheme live, keyed to
``MODEL_VERSION`` — a model / dim / prefix change bumps the version, the old vectors simply fall out
of the live matrix (lexical-only until backfilled), and cross-space cosine is mechanically
impossible. Import-guarded + FAIL-OPEN at every layer: if sentence-transformers is absent or the
weights won't load, the whole vector layer disables itself and OmniSeek degrades to Phase-1 lexical
(never an error, never blocks boot). One forward ``Lock`` so a query-embed and an ingest-embed never
run two concurrent MPS forwards (the real 16GB peak).

Model chosen by an on-host bake-off on REAL eye content (test on real data, never a benchmark):
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

# Token-length LADDER every forward is padded up to (2026-10-08). On MPS, torch
# compiles and caches one MPSGraph per distinct input SHAPE and never evicts it; the cache lives in
# CPU malloc (footprint category MALLOC_SMALL), and empty_cache() does not touch it. Measured on
# this machine with batch-1 forwards: 150 calls at ONE length grew MALLOC_SMALL by 34 MB, 150 calls
# at 150 distinct lengths grew it by 1160 MB (about 7.7 MB per new shape) and ran 4x slower
# (28.0 s vs 6.9 s, the per-shape compile). Real eye text spans 538 distinct head lengths in a
# 3000-doc sample (4 to 1389 tokens), so the unpadded process gains a graph for almost every new
# length it meets, without bound; the live process grew from 2232 to 2255 MB on searches alone.
# Padding to a fixed ladder caps the shape set at (batch sizes 1.._PASSAGE_LOCK_CHUNK) x
# len(ladder), plus one 1024-step bucket per extra 1024 tokens for rare inputs past the top rung.
# Rung choice (PROVISIONAL): powers of two to 256, then ratio 1.5 (384, 768, 1536) so the padding
# waste stays under half a forward where the long (expensive) documents live; the top rung covers
# the 2000-char cap of _embed_text plus a title (sample max 1427 tokens). Measured agreement with
# the unpadded vectors: cosine min 0.99989 over 40 real docs, inside the 0.99981 the current code
# already shows between embedding a doc alone and in a batch of four. Calibration plan: re-run the
# token-length sample and the shape probe behind this ladder if
# the model, the _embed_text cap, or the chunking changes; drop a rung only if its share of
# forwards is below one percent.
_LEN_LADDER = (16, 32, 64, 128, 256, 384, 512, 768, 1024, 1536, 2048)
_LEN_STEP_PAST_LADDER = 1024
# None until the first padded call decides it: False on a sentence-transformers too old to accept
# per-call processing_kwargs (pre 5.x), which then runs unpadded exactly as before.
_PAD_SUPPORTED: Optional[bool] = None


def _bucket_len(n: int, cap: Optional[int] = None) -> int:
    """Smallest ladder rung >= n (or the next 1024 multiple past the ladder), never above ``cap``
    (the model's max_seq_length; inputs past it are truncated there anyway). Pure."""
    n = max(int(n), 1)
    for rung in _LEN_LADDER:
        if n <= rung:
            out = rung
            break
    else:
        out = -(-n // _LEN_STEP_PAST_LADDER) * _LEN_STEP_PAST_LADDER
    if cap:
        out = min(out, int(cap))
    return out


def _pad_kwargs(m, texts: list[str], longest: Optional[int] = None) -> dict:
    """Per-call processor kwargs that pad this batch to its ladder rung, or {} when padding is
    unavailable (no tokenizer, an old sentence-transformers). ``longest`` skips the tokenizer when
    the caller already counted. Never raises."""
    if _PAD_SUPPORTED is False:
        return {}
    try:
        cap = getattr(m, "max_seq_length", None)
        if longest is None:
            longest = max(_token_lens(m, texts))
        if cap:
            longest = min(longest, int(cap))
        return {"processing_kwargs": {"text": {"padding": "max_length",
                                               "max_length": _bucket_len(longest, cap)}}}
    except Exception as exc:  # noqa: BLE001 — padding is a memory bound, never a reason to fail
        logger.debug("recall embed: length padding skipped (%s)", exc)
        return {}


# ONE FORWARD'S BUFFERS STAY UNDER 8 MiB (2026-10-09). torch's MPS allocator
# packs requests under 10 MiB into 8 or 32 MiB heaps, but opens a fresh 1 GiB heap for any request
# of 10 MiB or more on a unified-memory Mac (allocator trace, PYTORCH_DEBUG_MPS_ALLOCATOR: the
# first 16 MiB request of a forward allocated "shared heap of size 1024.00 MiB", every 8 MiB one
# went into a 32 MiB heap). An unbounded forward requests far larger buffers: the attention score
# tensor of a padded batch is batch x heads x L x L, the MLP holds tokens x intermediate. Measured
# on this machine, footprint above the pre-forward level, first forward at each shape: batch 1 at
# 2048 tokens +1.6 GB, batch 4 at 1536 +3.3 GB, batch 4 at 2048 +5.2 GB; every spike of the load
# bench (peak 6.6 GB) sat inside this forward. So every tensor of a forward is kept under the cap:
#   tokens per forward  <= cap / widest per-token row of the layer stack (the fp32 RMSNorm row,
#                          hidden x 4 bytes, or the query/key/value row after GQA repeat, heads x
#                          head_dim x 2): 2048 here
#   per-row steps       the modules that work row by row and hold a wider row than that run in
#                          steps of at most cap / row bytes rows, which changes no result: the MLP
#                          (intermediate x 2 bytes: 1365 tokens a step) and Qwen3's per-head q/k
#                          RMSNorm (it raises the query to fp32, heads x head_dim x 4 bytes a token:
#                          16 MiB at 2048 tokens before this, 1024 tokens a step after)
#   attention rows      <= cap / (batch x heads x L x element size) per block (each query row's
#                          attention reads only its own mask row and all of K and V)
# Texts past the token budget are truncated there; _embed_text already caps text at 2000 chars,
# which stayed under 1430 tokens on a 3000-doc sample. The cap is derived, not tuned: 8 MiB is the
# largest power of two under the allocator's 10 MiB line. Re-derive it if torch changes its heap
# sizes (re-run the forward-peak probe with the allocator trace on).
# Measured with the bound, same probe: batch 1 at 2048 +0.53 GB, batch 4 at 1536 +0.40 GB, batch
# 4 at 2048 +0.52 GB, no forward slower; the trace shows no 1 GiB heap beyond the weights' own. On
# 96 real docs (the 48 longest plus 48 spread) every vector matched the unbounded path to cosine
# 0.9999999, the same agreement as the bounded path against its own re-run.
_MPS_BUF_CAP = 8 << 20
# Set from the model config at load (MPS only); None means one forward per call as before.
_FWD_TOKENS: Optional[int] = None
_ATTN_IMPL = "omniseek_bounded_sdpa"


def _token_lens(m, texts: list[str]) -> list[int]:
    return [len(ids) for ids in m.tokenizer(texts, add_special_tokens=True)["input_ids"]]


def _token_groups(lens: list[int], budget: Optional[int], cap: Optional[int] = None) -> list[list[int]]:
    """Split text indices into forwards whose padded size (count x ladder rung of the longest)
    stays within ``budget`` tokens. Shortest first, so similar lengths share a rung; a single text
    is always its own forward even past the budget. Pure."""
    order = sorted(range(len(lens)), key=lambda i: lens[i])
    if not budget:
        return [order] if order else []
    groups: list[list[int]] = []
    cur: list[int] = []
    for i in order:
        rung = _bucket_len(min(lens[i], cap) if cap else lens[i], cap)
        if cur and (len(cur) + 1) * rung > budget:
            groups.append(cur)
            cur = []
        cur.append(i)
    if cur:
        groups.append(cur)
    return groups


def _forward_one(m, texts: list[str], longest: Optional[int] = None):
    """One encode under the caller's forward lock, padded to the length ladder when possible."""
    global _PAD_SUPPORTED
    kw = _pad_kwargs(m, texts, longest)
    if kw:
        try:
            v = m.encode(texts, batch_size=8, normalize_embeddings=True, show_progress_bar=False, **kw)
            _PAD_SUPPORTED = True
            return v
        except TypeError as exc:
            if _PAD_SUPPORTED or "keyword" not in str(exc):
                raise
            _PAD_SUPPORTED = False
            logger.warning("recall embed: this sentence-transformers rejects processing_kwargs "
                           "(%s); running unpadded, the MPS graph cache is then unbounded", exc)
    return m.encode(texts, batch_size=8, normalize_embeddings=True, show_progress_bar=False)


def _forward(m, texts: list[str]):
    """Encode ``texts`` (caller holds the forward lock) as one forward, or, once the token budget is
    set, as several forwards of at most _FWD_TOKENS padded tokens each, in the caller's order."""
    if not _FWD_TOKENS or len(texts) < 2:
        return _forward_one(m, texts)
    try:
        lens = _token_lens(m, texts)
    except Exception as exc:  # noqa: BLE001 — no tokenizer: one forward, exactly as before
        logger.debug("recall embed: token count skipped (%s)", exc)
        return _forward_one(m, texts)
    cap = getattr(m, "max_seq_length", None)
    groups = _token_groups(lens, _FWD_TOKENS, cap)
    if len(groups) == 1:
        return _forward_one(m, texts, max(lens))
    out = None
    for g in groups:
        v = np.asarray(_forward_one(m, [texts[i] for i in g], max(lens[i] for i in g)))
        if out is None:
            out = np.empty((len(texts),) + v.shape[1:], dtype=v.dtype)
        out[g] = v
    return out


def _row_steps(module, rows: int) -> None:
    """Wrap a module that works row by row (over its last dimension) so it runs at most ``rows``
    rows at a time; the result is the same tensor."""
    inner = module.forward

    def forward(x):
        if x.dim() < 2 or x.numel() // x.shape[-1] <= rows:
            return inner(x)
        import torch
        flat = x.reshape(-1, x.shape[-1])
        out = torch.cat([inner(flat[s:s + rows]) for s in range(0, flat.shape[0], rows)])
        return out.reshape(*x.shape[:-1], out.shape[-1])

    module.forward = forward


def _bounded_attention(module, query, key, value, attention_mask, dropout: float = 0.0,
                       scaling: Optional[float] = None, is_causal: Optional[bool] = None, **kwargs):
    """transformers attention function: SDPA over blocks of query rows sized so the score block
    stays under _MPS_BUF_CAP. Same signature and return as transformers' sdpa_attention_forward,
    which it calls unchanged when the whole score tensor already fits."""
    from transformers.integrations.sdpa_attention import repeat_kv, sdpa_attention_forward
    import torch

    bsz, heads, q_len = query.shape[0], query.shape[1], query.shape[2]
    k_len = key.shape[2]
    rows = max(1, _MPS_BUF_CAP // max(1, bsz * heads * k_len * query.element_size()))
    if q_len <= rows:
        return sdpa_attention_forward(module, query, key, value, attention_mask, dropout=dropout,
                                      scaling=scaling, is_causal=is_causal, **kwargs)
    groups = getattr(module, "num_key_value_groups", 1)
    if groups > 1:
        key = repeat_kv(key, groups)
        value = repeat_kv(value, groups)
    causal = is_causal if is_causal is not None else getattr(module, "is_causal", True)
    outs = []
    for s in range(0, q_len, rows):
        e = min(s + rows, q_len)
        if attention_mask is not None:
            mask = attention_mask if attention_mask.shape[-2] == 1 else attention_mask[:, :, s:e, :]
        elif causal:
            # SDPA's is_causal is the top-left triangle of the tensor it is given, here the BLOCK, so
            # a block that does not start at row 0 needs that same triangle written out at its rows.
            at = torch.arange(s, e, device=query.device)[:, None]
            mask = torch.arange(k_len, device=query.device)[None, :] <= at
        else:
            mask = None
        outs.append(torch.nn.functional.scaled_dot_product_attention(
            query[:, :, s:e], key, value, attn_mask=mask, dropout_p=dropout, scale=scaling,
            is_causal=False))
    return torch.cat(outs, dim=2).transpose(1, 2).contiguous(), None


def _bound_forward_buffers(m) -> None:
    """Apply the _MPS_BUF_CAP limits to a model loaded on MPS: token budget per forward, MLP in
    steps, attention in row blocks (masks built exactly as for sdpa). Only a qwen-style model on
    sdpa is changed; on any failure it stays as loaded. Never raises."""
    global _FWD_TOKENS
    try:
        if getattr(getattr(m, "device", None), "type", None) != "mps":
            return
        from transformers import AttentionInterface
        from transformers.masking_utils import ALL_MASK_ATTENTION_FUNCTIONS, sdpa_mask
        inner = m[0].auto_model
        cfg = inner.config
        if getattr(cfg, "_attn_implementation", None) != "sdpa":
            return
        elem = next(inner.parameters()).element_size()
        heads = cfg.num_attention_heads
        head_dim = getattr(cfg, "head_dim", None) or cfg.hidden_size // heads
        width = max(cfg.hidden_size * 4, heads * head_dim * elem)
        tokens = max(1, _MPS_BUF_CAP // width)
        mlp_rows = max(1, _MPS_BUF_CAP // (cfg.intermediate_size * elem))
        norm_rows = max(1, _MPS_BUF_CAP // (head_dim * 4))
        layers = inner.layers
        AttentionInterface.register(_ATTN_IMPL, _bounded_attention)
        ALL_MASK_ATTENTION_FUNCTIONS.register(_ATTN_IMPL, sdpa_mask)
        inner.set_attn_implementation(_ATTN_IMPL)
        for layer in layers:
            _row_steps(layer.mlp, mlp_rows)
            for name in ("q_norm", "k_norm"):
                norm = getattr(layer.self_attn, name, None)
                if norm is not None:
                    _row_steps(norm, norm_rows)
        if not m.max_seq_length or m.max_seq_length > tokens:
            m.max_seq_length = tokens
        _FWD_TOKENS = tokens
        logger.info("recall embed: forward buffers bounded (%d tokens per forward, MLP %d)",
                    tokens, mlp_rows)
    except Exception as exc:  # noqa: BLE001 — a memory bound, never a reason to fail
        logger.warning("recall embed: forward buffer bound not installed (%s)", exc)


def _release_device_cache(m) -> None:
    """Hand the MPS allocator's cached buffers back to the driver after a forward (caller holds the
    forward lock, so no forward is mid-flight). Measured 2026-10-08: torch 2.12's
    MPS allocator only trims its cache when the driver's allocation passes the LOW watermark, which
    defaults to 1.4 x recommendedMaxWorkingSetSize = 1.4 x 12124 MB = 16974 MB on this 16 GB
    machine, i.e. never before the machine is out of memory. Over 400 real docs the cache grew the
    driver allocation from 1160 MB (weights) to 3817 MB, length padding alone to 3635 MB; with this
    call it ends at about 1.5 GB, and the bound becomes weights plus one forward's own peak. CUDA
    and CPU keep their own allocator behaviour, so this is MPS only. Never raises."""
    try:
        if getattr(getattr(m, "device", None), "type", None) != "mps":
            return
        import torch
        torch.mps.empty_cache()
    except Exception as exc:  # noqa: BLE001 — a cache release is a memory bound, never a failure
        logger.debug("recall embed: mps empty_cache skipped (%s)", exc)

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
            _bound_forward_buffers(m)
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
            v = _forward(m, [prefix + (t or "") for t in texts])
            _release_device_cache(m)
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
