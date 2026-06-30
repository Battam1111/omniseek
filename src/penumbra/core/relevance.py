"""Lexical relevance: Penumbra's ONE mechanical text-match scorer.

Replaces raw term-counting (3*title.count(t) + body.count(t)), whose measured
failure modes were:
  - no term-frequency saturation: a page repeating one query word 50x outranked
    a true hit that matched every term once (the "swamped by popular-but-off-
    target" USAGE#2 bug);
  - no document-length normalization: long pages accumulated counts;
  - ASCII terms matched inside unrelated words ("ai" inside "maintain");
  - CJK matched only as one exact substring of the whole run ("大模型推理" could
    not match "大模型的推理").

This is BM25-shaped math over weighted fields, computed within the candidate set:
  tokens     ASCII word tokens + CJK bigrams (mechanical, language-blind)
  tf         saturated: tf*(k1+1) / (tf + k1*norm), k1=1.2
  length     norm = 1-b + b*(doclen/avg-doclen-of-candidates), b=0.75
  idf        within the candidate set: ln(1 + (N-df+0.5)/(df+0.5))
  fields     each caller passes (text, weight) pairs, e.g. title 3x + body 1x

It is judgment-free: no owner preferences, no learned weights, nothing tunable
per source. The agent still re-ranks; this layer only stops the funnel from
drowning true hits before the agent ever sees them.
"""

from __future__ import annotations

import math
import re

_K1 = 1.2
_B = 0.75

_ASCII = re.compile(r"[a-z0-9]+")
# CJK unified ideographs (+ ext A) and kana. Hangul intentionally out of scope
# until a real query needs it.
_CJK = re.compile(r"[㐀-䶿一-鿿぀-ヿ]+")


def tokenize(text: str) -> list[str]:
    """ASCII word tokens + CJK bigrams (a lone CJK char stands alone)."""
    text = (text or "").lower()
    toks = _ASCII.findall(text)
    for run in _CJK.findall(text):
        if len(run) == 1:
            toks.append(run)
        else:
            toks.extend(run[i:i + 2] for i in range(len(run) - 1))
    return toks


def query_terms(query: str) -> list[str]:
    """Unique query tokens worth scoring: 1-char ASCII tokens are dropped (an 'a'
    matches everything and means nothing); 1-char CJK tokens are kept (real words)."""
    out = []
    for t in dict.fromkeys(tokenize(query or "")):
        if len(t) > 1 or not t.isascii():
            out.append(t)
    return out


def field_scores(items: list[list[tuple[str, float]]], query: str) -> list[float]:
    """BM25-lite score of each item against ``query``.

    Each item is a list of (text, weight) fields, e.g. [(title, 3.0), (body, 1.0)].
    Returns one score per item; 0.0 = no query term matched. A term-less query
    returns all zeros so callers keep their own pre-sort order.
    """
    terms = query_terms(query)
    n = len(items)
    if not terms or n == 0:
        return [0.0] * n

    tfs: list[dict[str, float]] = []
    lens: list[float] = []
    for fields in items:
        tf: dict[str, float] = {}
        dl = 0.0
        for text, w in fields:
            toks = tokenize(text)
            dl += w * len(toks)
            for t in toks:
                tf[t] = tf.get(t, 0.0) + w
        tfs.append(tf)
        lens.append(dl)
    avgdl = (sum(lens) / n) or 1.0

    idf = {t: math.log(1.0 + (n - sum(1 for tf in tfs if t in tf) + 0.5)
                       / (sum(1 for tf in tfs if t in tf) + 0.5))
           for t in terms}

    out: list[float] = []
    for tf, dl in zip(tfs, lens):
        norm = 1.0 - _B + _B * (dl / avgdl)
        s = 0.0
        for t in terms:
            f = tf.get(t, 0.0)
            if f > 0.0:
                s += idf[t] * (f * (_K1 + 1.0)) / (f + _K1 * norm)
        out.append(s)
    return out


def doc_scores(docs, query: str) -> list[float]:
    """Score Documents: title weighted 3x over content. The one entry point
    shared by rank.merge_rank and keyword_score_filter, so search ranking and
    adapter-side filtering can never drift apart again."""
    return field_scores([[(d.title or "", 3.0), (d.content or "", 1.0)] for d in docs], query)


def filter_rank(docs, query: str):
    """keyword_score_filter semantics on this engine: a term-less query returns
    ``docs`` unchanged; otherwise only docs matching >=1 term, best first
    (stable order on ties)."""
    if not query_terms(query or ""):
        return docs
    scores = doc_scores(docs, query)
    scored = [(s, i, d) for i, (s, d) in enumerate(zip(scores, docs)) if s > 0.0]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [d for _, _, d in scored]
