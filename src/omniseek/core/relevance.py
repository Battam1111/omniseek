"""Lexical relevance: OmniSeek's ONE mechanical text-match scorer.

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
  match      a doc scores > 0 only if it matches >= 1 query UNIT (see query_units): a word
             token matches by itself; a CJK run matches when the doc holds MORE THAN A THIRD of
             the run's distinct bigrams. Without this, one 2-char fragment of a typed phrase
             ("模型" of "奖励模型", the cross-word "化学" of "强化学习") admitted any doc.

It is judgment-free: no owner preferences, no learned weights, nothing tunable
per source. The agent still re-ranks; this layer only stops the funnel from
drowning true hits before the agent ever sees them.
"""

from __future__ import annotations

import math
import re

_K1 = 1.2
_B = 0.75

# Scriptio-continua scripts, sliced into OVERLAPPING BIGRAMS (no word spaces to split on):
# CJK unified ideographs (+ ext A), kana, and Hangul syllables. Hangul joined 2026-07-05: a live
# ko probe showed Korean text was fully INVISIBLE to the lexical layer (lexical=0, so the ranker
# lost its relevance anchor and freshness/engagement pushed noise to the top) while the ASR can
# transcribe Korean audio into exactly such text — perception could produce text memory could not find.
_CJK = re.compile(r"[㐀-䶿一-鿿぀-ヿ가-힣]+")
# One linear scan, three classes, first match at each position wins:
#   continua (above) → bigrams; Latin letters (incl. diacritics) + digits → one word token ("café"
#   was mangled to "caf", "über" to "ber"); ANY other Unicode letter run (Cyrillic, Greek, Arabic,
#   Thai, ...) → one word token (previously dropped entirely). Space-separated scripts match their
#   own word forms; Thai-class continua degrade to run-tokens (findable, coarser) — accepted.
_TOKEN = re.compile(
    r"([㐀-䶿一-鿿぀-ヿ가-힣]+)"
    r"|([a-z0-9À-ÖØ-öø-ɏ]+)"
    r"|([^\W\d_]+)")


def tokenize(text: str) -> list[str]:
    """Word tokens for every letter script + overlapping bigrams for scriptio-continua runs
    (a lone continua char stands alone). Tokens return in TEXT ORDER (one linear scan)."""
    text = (text or "").lower()
    toks: list[str] = []
    for cjk, word, other in _TOKEN.findall(text):
        if cjk:
            if len(cjk) == 1:
                toks.append(cjk)
            else:
                toks.extend(cjk[i:i + 2] for i in range(len(cjk) - 1))
        else:
            toks.append(word or other)
    return toks


def query_terms(query: str) -> list[str]:
    """Unique query tokens worth scoring: 1-char ASCII tokens are dropped (an 'a'
    matches everything and means nothing); 1-char CJK tokens are kept for scoring (whether
    they can admit a doc on their own is query_units' call)."""
    out = []
    for t in dict.fromkeys(tokenize(query or "")):
        if len(t) > 1 or not t.isascii():
            out.append(t)
    return out


def query_units(query: str) -> list[tuple[str, ...]]:
    """The query split into the units a doc must match, each a tuple of distinct tokens.

    A word token (what query_terms keeps outside continua runs) is one unit of itself. A
    scriptio-continua run the user typed without spaces is ONE unit holding its bigrams: the
    bigrams are mechanical slices, not words the user chose, so no single one may stand for the
    whole run. A lone continua char between other tokens ("GPT和Claude") is a unit only when the
    query has no other unit; otherwise it still scores but never admits a doc by itself."""
    units: list[tuple[str, ...]] = []
    lone: list[tuple[str, ...]] = []
    for cjk, word, other in _TOKEN.findall((query or "").lower()):
        if cjk:
            if len(cjk) == 1:
                lone.append((cjk,))
            else:
                units.append(tuple(dict.fromkeys(cjk[i:i + 2] for i in range(len(cjk) - 1))))
        else:
            t = word or other
            if len(t) > 1 or not t.isascii():
                units.append((t,))
    units = units or lone
    return list(dict.fromkeys(units))


def _unit_hit(unit: tuple[str, ...], present) -> bool:
    # 3*hits > n: the strict one-third bound. For a run of 2-char words joined by 1-char particles
    # (the sparsest shape) a doc holding every word covers w/(3w-2) > 1/3 of the bigrams, so it is
    # admitted; one 2-char word of a two-word run covers exactly 1/3, so it is not. Single-token
    # units reduce to "the token is present" (ASCII-only queries keep any-term semantics).
    return 3 * sum(1 for t in unit if t in present) > len(unit)


def matches(tokens, query: str) -> bool:
    """True iff a doc whose token set is ``tokens`` matches >= 1 unit of ``query``. The ONE
    match rule: field_scores applies it, the recall index post-filters with it."""
    present = tokens if isinstance(tokens, (set, frozenset, dict)) else set(tokens)
    return any(_unit_hit(u, present) for u in query_units(query))


_PAIR = "\x00"   # joins two tokens into one pair term (no token can hold it)


def word_pairs(toks: list[str]) -> list[str]:
    """Each two adjacent tokens of ``toks`` as one pair term, for pairs of two WORD tokens only: a
    scriptio-continua run is already sliced into overlapping bigrams, so its word order is in its
    tokens. A pair of a word and a 1-char token ("degree-1" -> degree, 1) is a pair too."""
    return [a + _PAIR + b for a, b in zip(toks, toks[1:])
            if not _CJK.match(a) and not _CJK.match(b)]


def query_pairs(query: str) -> list[str]:
    """The query's adjacent word-token pairs (see word_pairs), each once, in query order."""
    return list(dict.fromkeys(word_pairs(tokenize(query or ""))))


def _weighted_tf(fields: list[tuple], pairs: frozenset = frozenset(),
                 tokenized: bool = False) -> tuple[dict[str, float], float]:
    """Weighted term frequencies and length of one item; with ``pairs`` (query_pairs) also the
    frequency of each of those pairs where its two words stand next to each other in a field.
    ``tokenized``: each field is (tokenize(text), weight) already, so a caller that needs a text's
    tokens several times tokenizes it once (rank.text_scores)."""
    tf: dict[str, float] = {}
    dl = 0.0
    heads = {p.split(_PAIR, 1)[0] for p in pairs}
    for text, w in fields:
        toks = text if tokenized else tokenize(text)
        dl += w * len(toks)
        for t in toks:
            tf[t] = tf.get(t, 0.0) + w
        if heads:
            for a, b in zip(toks, toks[1:]):
                if a in heads and a + _PAIR + b in pairs:
                    t = a + _PAIR + b
                    tf[t] = tf.get(t, 0.0) + w
    return tf, dl


def corpus_stats(items: list[list[tuple[str, float]]]) -> dict:
    """The candidate-set statistics field_scores computes, taken once over a whole corpus a source
    holds locally: ``{"n", "df", "avgdl"}``. Scoring a subset with ``corpus=`` gives each item the
    score it gets when the whole corpus is scored, so a caller may score only the items that match a
    query term (every other item scores 0) without scoring all of them each time."""
    df: dict[str, int] = {}
    total = 0.0
    for fields in items:
        tf, dl = _weighted_tf(fields)
        total += dl
        for t in tf:
            df[t] = df.get(t, 0) + 1
    n = len(items)
    return {"n": n, "df": df, "avgdl": (total / n if n else 0.0) or 1.0}


def field_scores(items: list[list[tuple]], query: str,
                 corpus: dict | None = None, extra_len: list[float] | None = None,
                 pair_weight: float = 0.0, tokenized: bool = False) -> list[float]:
    """BM25-lite score of each item against ``query``.

    Each item is a list of (text, weight) fields, e.g. [(title, 3.0), (body, 1.0)].
    Returns one score per item; 0.0 = the item matches no query unit (see ``matches``).
    A term-less query returns all zeros so callers keep their own pre-sort order.
    ``corpus`` (from ``corpus_stats``): the items are part of that corpus and are scored against
    its statistics instead of their own. ``extra_len`` (one number per item) is added to each item's
    length before length normalization: the length of a field whose text is unknown (see doc_scores).
    ``pair_weight`` > 0: each adjacent word pair of the query (query_pairs) is one more BM25 term,
    held where the two words stand next to each other in one field, with its own idf over the items
    and its contribution times ``pair_weight`` (see doc_scores). Pairs never decide WHETHER an item
    matches (that is still the word units) and add nothing to an item's length. Not with ``corpus``
    (its statistics hold no pairs). ``tokenized``: each field's text is given as its token list
    (see _weighted_tf); the scores are the same as for the texts.
    """
    terms = query_terms(query)
    n = len(items)
    if not terms or n == 0:
        return [0.0] * n
    units = query_units(query)
    pairs = query_pairs(query) if pair_weight > 0.0 else []
    if pairs and corpus is not None:
        raise ValueError("pair_weight needs the items' own statistics, not a corpus")

    tfs: list[dict[str, float]] = []
    lens: list[float] = []
    for fields in items:
        tf, dl = _weighted_tf(fields, pairs=frozenset(pairs), tokenized=tokenized)
        tfs.append(tf)
        lens.append(dl)
    if extra_len is not None:
        lens = [dl + x for dl, x in zip(lens, extra_len)]
    if corpus is not None:
        n_all, avgdl = corpus["n"], corpus["avgdl"]
        dfs = {t: corpus["df"].get(t, 0) for t in terms}
    else:
        n_all, avgdl = n, (sum(lens) / n) or 1.0
        dfs = {t: sum(1 for tf in tfs if t in tf) for t in terms + pairs}

    weight = {t: 1.0 for t in terms}
    weight.update({p: pair_weight for p in pairs})
    idf = {t: math.log(1.0 + (n_all - dfs[t] + 0.5) / (dfs[t] + 0.5)) for t in weight}

    out: list[float] = []
    for tf, dl in zip(tfs, lens):
        if not any(_unit_hit(u, tf) for u in units):
            out.append(0.0)
            continue
        norm = 1.0 - _B + _B * (dl / avgdl)
        s = 0.0
        for t, wt in weight.items():
            f = tf.get(t, 0.0)
            if f > 0.0:
                s += wt * idf[t] * (f * (_K1 + 1.0)) / (f + _K1 * norm)
        out.append(s)
    return out


def doc_scores(docs, query: str, *, empty_body_unknown: bool = False,
               pair_weight: float = 0.0) -> list[float]:
    """Score Documents: title weighted 3x over content. The one entry point
    shared by rank.merge_rank and keyword_score_filter, so search ranking and
    adapter-side filtering can never drift apart again.

    ``empty_body_unknown`` (merge_rank, 2026-10-20): a document whose body is empty (its
    source had none to give) counts as having a body of the average length of the non-empty bodies
    here. Its length is unknown, not zero: scored as zero length, BM25's length normalization made a
    bare title the strongest match of the batch (a crossref "VRF air-conditioner" book chapter with no
    abstract took relevance 0.98 of the best, measured on a recorded search). Which documents match
    is unchanged, so adapter-side filtering (the default) is unaffected.

    ``pair_weight`` (merge_rank, 2026-10-24): the query's adjacent word pairs count as
    extra BM25 terms at this weight (field_scores). A document that holds the query's words apart
    ("Point-Based Distributed Zeroth-Order Optimization" for "distributed point function") then
    scores below one that holds them as the query's phrase. Default 0: adapter-side filtering and
    every other caller are unchanged."""
    items = [[(d.title or "", 3.0), (d.content or "", 1.0)] for d in docs]
    return _doc_scores(items, query, empty_body_unknown, pair_weight, False)


def doc_scores_tokens(docs: list[tuple[list[str], list[str]]], query: str, *,
                      empty_body_unknown: bool = False, pair_weight: float = 0.0) -> list[float]:
    """doc_scores over documents given as (title tokens, content tokens), i.e. tokenize(title or "")
    and tokenize(content or ""); the same scores, for a caller that already holds the tokens."""
    items = [[(tt, 3.0), (tc, 1.0)] for tt, tc in docs]
    return _doc_scores(items, query, empty_body_unknown, pair_weight, True)


def _doc_scores(items: list, query: str, empty_body_unknown: bool, pair_weight: float,
                tokenized: bool) -> list[float]:
    if not empty_body_unknown:
        return field_scores(items, query, pair_weight=pair_weight, tokenized=tokenized)
    body = [_weighted_tf([it[1]], tokenized=tokenized)[1] for it in items]
    known = [b for b in body if b > 0]
    avg = sum(known) / len(known) if known else 0.0
    return field_scores(items, query, extra_len=[avg if b == 0 else 0.0 for b in body],
                        pair_weight=pair_weight, tokenized=tokenized)


def filter_rank(docs, query: str):
    """keyword_score_filter semantics on this engine: a term-less query returns
    ``docs`` unchanged; otherwise only docs matching >=1 query unit, best first
    (stable order on ties)."""
    if not query_terms(query or ""):
        return docs
    scores = doc_scores(docs, query)
    scored = [(s, i, d) for i, (s, d) in enumerate(zip(scores, docs)) if s > 0.0]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [d for _, _, d in scored]
