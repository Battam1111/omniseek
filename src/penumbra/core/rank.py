"""Cross-source dedup + unified ranking.

``search_many`` returns one bucket per source, so the same paper shows up 4×
(arxiv + openalex + semantic_scholar + crossref), and there is no order across
sources. This module collapses duplicates and ranks the survivors by a
TRANSPARENT blend of keyword relevance, recency, and engagement — turning N raw
per-source streams into one "here's what actually matters" list.

Dedup fingerprint (first that applies), chosen so the SAME item from different
sources lands on the SAME key:
  1. explicit DOI in metadata
  2. normalized title, if substantial (≥20 alnum chars) — the universal
     cross-source key for papers/articles (arxiv & openalex share a title, not a URL)
  3. arXiv id / DOI parsed from the URL
  4. normalized URL
  5. source:source_id (last resort — never merges across sources)

Ranking is intentionally simple + explainable (no opaque learned weights):
  query present →  0.60·relevance + 0.25·recency + 0.15·engagement
  query absent  →  0.70·recency + 0.30·engagement      (browse/digest mode)
then a light cross-source corroboration nudge (×up to ~1.15 when several sources
independently surface the same work) — a SIGNAL, not truth; the agent re-judges.
The chosen item's score is stamped into ``metadata._rank``; the corroborating
sources into ``metadata.also_in`` + their count into ``metadata.corroboration`` —
so the ranking is auditable, never a black box.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone

from penumbra.core import relevance
from penumbra.core.normalize import Document

logger = logging.getLogger(__name__)

from penumbra.core.recall import graph as _graph  # noqa: E402 — the graph write verb + mint registry

# Vocabulary this tap MINTS (vocabulary-by-minting, design section 3): declared on the tap itself,
# registered at import, folded into ``_graph.declared_vocabulary`` as the computed union; the smoke
# tripwire bounds ACTUAL graph data to that union. The conflicts tap is the P4 event layer's second
# half: it mints ``conflicts`` A-tier edges (doc <-> doc) where dedup already found same-work signal
# DIVERGENCE across sources. NO node kinds (doc endpoints are virtual/thin), NO new methods beyond
# ``signal:divergence``. The edge attrs carry the signal NAME and its KIND (e.g. "engagement"), so
# views/agents can mechanically filter the cross-platform engagement-count noise class the GRPO
# dogfood exposed. THE DETECTION lives in ``dedup`` (the only place a group's collapsed members are
# still visible); the HOOK is fail-open on the ranked-search path (fetcher.py).
GRAPH_MINTS = {
    "kinds": [],
    "edge_types": ["conflicts"],
    "methods": ["signal:divergence"],
}
_graph.register_mints("conflicts", kinds=GRAPH_MINTS["kinds"],
                      edge_types=GRAPH_MINTS["edge_types"], methods=GRAPH_MINTS["methods"])

_ARXIV_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", re.I)
_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.I)
_TITLE_MIN = 20  # min normalized-title length to use it as a merge key


def _norm_title(t: str) -> str:
    # keep alnum + CJK ideographs, drop everything else; lowercase
    return re.sub(r"[^a-z0-9㐀-䶿一-鿿぀-ヿ]+", "", (t or "").lower())  # CJK class == relevance._CJK (anti-drift)


def _norm_url(u: str) -> str:
    u = (u or "").split("?")[0].split("#")[0].rstrip("/").lower()
    return re.sub(r"^https?://(www\.)?", "", u)


def _norm_doi(s: str) -> str:
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", str(s).lower().strip())


def _divergence_ratio(v1: float, v2: float):
    """The same-work divergence ratio for two numeric signal values: ``max/min`` of their
    MAGNITUDES, or the string ``"inf"`` for the two unbounded classes (one side 0 and the other
    not; opposite SIGNS, where no finite magnitude ratio is meaningful and the disagreement is
    categorical, e.g. a downvoted vs upvoted score). Returns ``None`` when the two values are
    EQUAL (equal is not a divergence, so it never ranks). Signals CAN be negative (a reddit
    score, a net loss), so the ratio is defined over |v|; a bare signed max/min would mis-rank
    a real 5x divergence like -10 vs -2 as 0.2 and a sign flip as negative. Pure. The P7
    detector RANKS by this ratio and no longer gates on it (rank, never a score gate)."""
    if v1 == v2:
        return None            # equal values are not a divergence
    if v1 * v2 < 0:
        return "inf"           # opposite signs: categorical disagreement (ranks first)
    hi, lo = max(abs(v1), abs(v2)), min(abs(v1), abs(v2))
    if lo == 0:
        return "inf"           # one side 0, the other nonzero -> unbounded ratio (ranks first)
    return hi / lo


def _round_sig(x: float, sig: int = 3):
    """Round ``x`` to ``sig`` significant figures (the stamp's ratio precision). ``"inf"`` and any
    non-finite/non-numeric value passes through unchanged (the divergence ratio serializes "inf" as a
    string). Pure."""
    if not isinstance(x, (int, float)) or x == 0 or not math.isfinite(x):
        return x
    from math import floor, log10
    return round(x, -int(floor(log10(abs(x)))) + (sig - 1))


def fingerprint(doc: Document) -> str:
    """A key that lands the SAME item on the SAME string across sources.

    TITLE FIRST (≥20 alnum chars): it's the only field all paper/article sources
    expose consistently — arxiv keys a paper by its URL/id, openalex by a DOI-URL,
    semantic_scholar by its own id, but they ALL carry the same title. Identifier
    keys (DOI/arxiv-id) are unreliable for cross-source merge because each source
    exposes a *different* identifier for the same work, so they're used only as a
    fallback for short/empty titles. (Caveat: two distinct items sharing a long
    title — e.g. an identically-named job at two companies — will merge; the
    survivor's ``also_in`` makes that visible/auditable.)
    """
    nt = _norm_title(doc.title)
    if len(nt) >= _TITLE_MIN:
        return f"title:{nt}"
    doi = (doc.metadata or {}).get("doi")
    if doi:
        return f"doi:{_norm_doi(doi)}"
    m = _ARXIV_RE.search(doc.url or "")
    if m:
        return f"arxiv:{m.group(1)}"
    m = _DOI_RE.search(doc.url or "")
    if m:
        return f"doi:{_norm_doi(m.group(0))}"
    nu = _norm_url(doc.url)
    if nu:
        return f"url:{nu}"
    return f"id:{doc.source}:{doc.source_id}"


def _pick_best(group: list[Document]) -> Document:
    """Richest representative of a duplicate group: most content, then score."""
    return max(group, key=lambda d: (len(d.content or ""), d.attention_value() or 0))


def dedup(docs: list[Document]) -> list[Document]:
    """Collapse cross-source duplicates; annotate survivors with ``also_in``."""
    groups: dict[str, list[Document]] = {}
    order: list[str] = []
    for d in docs:
        fp = fingerprint(d)
        if fp not in groups:
            groups[fp] = []
            order.append(fp)
        groups[fp].append(d)

    out: list[Document] = []
    for fp in order:
        grp = groups[fp]
        best = _pick_best(grp)
        add: dict = {}
        if len(grp) > 1:
            srcs = {d.source for d in grp}
            others = sorted(srcs - {best.source})
            if others:
                add["also_in"] = others
            if len(srcs) > 1:  # corroboration = how many DISTINCT sources surfaced this work
                add["corroboration"] = len(srcs)
            # --- #11 signal_conflicts: same-named NUMERIC Signal values that DIVERGE across the
            #     group's different-source members. Detected HERE because the merge is the only
            #     place the collapsed members are still visible (post-dedup one survivor per group
            #     remains, so any later cross-doc comparison compares DIFFERENT works — the
            #     2026-07-01 dogfood noise). Mechanical flag; never fed to ranking (the razor);
            #     fail-open.
            #     P7 (2026-07-03): the 1.5x ratio GATE is gone. "How much divergence is worth
            #     flagging" was the last judgment-shaped constant in the eye; the detector now
            #     MEASURES every same-work numeric divergence (its max/min ratio), RANKS by that
            #     ratio DESC, and keeps the top-3 per doc — a RESOURCE cap, not an epistemic line
            #     (the RRF discipline: rank, never a score gate). Equal values are not a divergence
            #     (skipped); a zero-vs-nonzero pair is an unbounded "inf" ratio and ranks first.
            #     Materiality is the reader's judgment; the stamp carries the number.
            try:
                # Collect ALL cross-source same-name numeric divergences first, each with its
                # measured ratio, THEN rank by ratio DESC and cap — so the survivors are "the most
                # divergent, with numbers", never "whichever the iteration order reached before a
                # cap". Each candidate carries both the agent-visible stamp fields AND the private
                # tap record (full identities + the signal's KIND + the raw values), built in one
                # pass so the two never drift; the fetcher POPS the private _conflict_pairs key
                # before returning (the STABILITY contract: signal_conflicts is additive-only).
                _cands: list[tuple] = []   # (rank_key, seq, conf_entry, pair_record)
                _seq = 0
                for _i, _da in enumerate(grp):
                    for _db in grp[_i + 1:]:
                        if _da.source == _db.source:
                            continue
                        # sorted() over the shared signal names so equal-ratio ties (same ratio,
                        # different signal) fall back to a DETERMINISTIC ``_seq`` (set order is not).
                        for _nm in sorted(set(_da.signals or {}) & set(_db.signals or {})):
                            _v1, _v2 = _da.signals[_nm].value, _db.signals[_nm].value
                            if _v1 is None or _v2 is None:
                                continue
                            _ratio = _divergence_ratio(_v1, _v2)
                            if _ratio is None:      # equal values are not a divergence
                                continue
                            _r_stamp = "inf" if _ratio == "inf" else _round_sig(_ratio, 3)
                            # rank key: "inf" sorts above every finite ratio (unbounded divergence).
                            _rank_key = math.inf if _ratio == "inf" else _ratio
                            _cands.append((_rank_key, _seq, {
                                "topic": _nm,
                                "source_a": _da.source,
                                "claim_a": f"{_nm}={_v1} ({_da.signals[_nm].unit or ''})",
                                "source_b": _db.source,
                                "claim_b": f"{_nm}={_v2} ({_db.signals[_nm].unit or ''})",
                                "ratio": _r_stamp,
                            }, {
                                "a": (_da.source, _da.source_id),
                                "b": (_db.source, _db.source_id),
                                "signal": _nm,
                                "kind": getattr(_da.signals[_nm], "kind", None),
                                "values": {_da.source: _v1, _db.source: _v2},
                                "ratio": _r_stamp,
                            }))
                            _seq += 1
                # rank by ratio DESC; ``_seq`` (insertion order) is a stable, deterministic
                # tie-break so equal-ratio divergences keep a fixed order across runs.
                _cands.sort(key=lambda c: (-c[0], c[1]))
                _top = _cands[:3]              # the per-doc RESOURCE cap (unchanged), applied by RANK
                if _top:
                    add["signal_conflicts"] = [c[2] for c in _top]
                    add["_conflict_pairs"] = [c[3] for c in _top]   # PRIVATE: fetcher pops it
            except Exception:  # noqa: BLE001 — one signal's failure never corrupts the merge
                pass
        # Curator P2 attribution stamps (pure facts, no judgment; stamped on EVERY survivor,
        # singletons included, so the yield tap can TRUST field presence rather than infer absence).
        # live_sources: group members that were LIVE this run (a recall-index rehydration carries
        #   metadata.from_index=True and is NOT live: the live feed went quiet, recall carries it).
        # merge_basis: did this group collapse on a STRONG id (doi/arxiv/url) or only a shared long
        #   TITLE? A title-only merge across sources is WEAK corroboration (two distinct same-titled
        #   jobs merge; see fingerprint caveat) and must NOT strip a sole-contributor credit.
        live_srcs = sorted({d.source for d in grp if not (d.metadata or {}).get("from_index")})
        add["live_sources"] = live_srcs
        add["merge_basis"] = "title" if fp.startswith("title:") else "id"
        # Preserve the recall RRF prior + via across the collapsed group: _pick_best may keep a
        # member WITHOUT the stamp (a richer LIVE doc collapsing a vector-found index doc), and that
        # stamp is exactly what lifts a semantic-only hit in merge_rank. Take the MAX rrf; via=both
        # if both arms (or a prior 'both') are present.
        rrfs = [(d.metadata or {}).get("recall_rrf") for d in grp]
        rrfs = [r for r in rrfs if isinstance(r, (int, float))]
        if rrfs:
            vias = {(d.metadata or {}).get("recall_via") for d in grp}
            vias.discard(None)
            add["recall_rrf"] = max(rrfs)
            add["recall_via"] = "both" if (len(vias) > 1 or "both" in vias) else next(iter(vias), None)
        if add:
            best.metadata = {**(best.metadata or {}), **add}
        out.append(best)
    return out


# ── graph write tap (design section 6 + P4 taps row): mint conflicts edges from dedup divergence ──
# THE MINT RULE (design "Mint the product"): the tap mints what dedup RETURNS as a divergence
# record, not its internal comparison material. Each record carries both members' full identities +
# the signal's name + KIND + the two raw values (collected in ``dedup`` under the private
# ``_conflict_pairs`` stamp the fetcher pops). The builder is PURE (records -> (nodes, edges)) so
# the smoke can golden-test it with zero network; ``_conflict_tap`` wraps enqueue_graph fail-open (a
# tap failure must NEVER break the search the agent gets). NO nodes are minted — the doc endpoints
# are virtual/thin document rows, and a stored edge does not require a node row for its endpoints.

def _conflict_mints(records: list) -> tuple[list[dict], list[dict]]:
    """From dedup's conflict RECORDS (each ``{a: (source, source_id), b: (source, source_id),
    signal: str, kind: str|None, values: {...}, ratio: float|"inf"}``): one ``conflicts`` A-tier edge
    doc <-> doc per record, method ``signal:divergence``, attrs {signal, kind, values, ratio}. ``kind``
    is the SIGNAL's kind field (e.g. "engagement") lifted from the diverging signals, so views/agents
    can mechanically filter the engagement-count noise class; ``ratio`` is the measured max/min
    divergence (P7: the edge carries the number, materiality is the reader's). No nodes (doc endpoints
    are virtual/thin). The writer normalizes the symmetric pair to src < dst, so do NOT pre-sort here.
    A record with a missing endpoint is skipped (fail-open). Pure."""
    edges: list[dict] = []
    for rec in (records or []):
        if not isinstance(rec, dict):
            continue
        a = rec.get("a") or ()
        b = rec.get("b") or ()
        if len(a) != 2 or len(b) != 2 or not a[0] or not a[1] or not b[0] or not b[1]:
            continue
        a_nid = _graph.doc_node_id(a[0], a[1])
        b_nid = _graph.doc_node_id(b[0], b[1])
        if a_nid == b_nid:
            continue
        edges.append({"src": a_nid, "dst": b_nid, "type": "conflicts", "tier": "A",
                      "method": "signal:divergence",
                      "attrs": {"signal": rec.get("signal"), "kind": rec.get("kind"),
                                "values": rec.get("values"), "ratio": rec.get("ratio")}})
    return [], edges


def _conflict_tap(records: list) -> None:
    """FAIL-OPEN wrapper (the relations.py idiom): build the conflicts edges from dedup's records and
    enqueue them through the single-writer queue. Never raises (a tap failure must NEVER break the
    search); NO-OP when writes are disabled (cron) or there are no records. Import the writer INSIDE
    the try so an import hiccup degrades to a swallow, never a broken search."""
    try:
        _nodes, edges = _conflict_mints(records)
        if not edges:
            return
        from penumbra.core.recall import writer
        writer.enqueue_graph([], edges)
    except Exception as exc:  # noqa: BLE001 — a tap failure must NEVER break the search
        logger.debug("conflicts graph tap swallowed: %s", exc)


def _recency(doc: Document, now: datetime) -> float:
    dt = doc.date
    if not dt:
        return 0.3  # unknown date → neutral-low
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - dt).total_seconds() / 86400.0)
    return 1.0 / (1.0 + age_days / 30.0)  # 1.0 today, 0.5 at ~30d


def _engagement(doc: Document) -> float:
    s = doc.attention_value() or 0
    if s <= 0:
        return 0.0
    return min(1.0, math.log10(1 + s) / 4.0)  # ~1.0 at score≈10k


def _extract_hook(doc: Document, terms: list[str], cap: int = 120) -> str:
    """Extractive one-liner: the sentence from the doc's own text with the highest
    query-term overlap. Pure substring selection, NOT generative (the eye does not
    editorialize) — the agent reads the hook to decide relevance at a glance."""
    if not terms:
        return ''
    text = (doc.title or '') + '. ' + (doc.content or '')[:500]
    # Split into sentences (period+space, newline, Chinese period)
    sents = [s.strip() for s in re.split(r'(?<=[.!?])\s+|\n|。', text) if s.strip()]
    if not sents:
        return ''
    from penumbra.core.relevance import tokenize  # local import: keep the helper self-contained
    term_set = set(terms)
    best, best_n = '', 0
    for s in sents:
        n = len(term_set & set(tokenize(s)))
        if n > best_n:
            best, best_n = s, n
    if best_n == 0:
        return ''
    return best[:cap]


def merge_rank(results, query: str, limit: int = 15) -> list[Document]:
    """Flatten → dedup → rank. ``results`` is a search_many dict or a flat list."""
    if isinstance(results, dict):
        docs: list[Document] = []
        for lst in results.values():
            docs.extend(lst)
    else:
        docs = list(results)

    docs = dedup(docs)
    # Lexical relevance comes from the shared engine (penumbra.core.relevance:
    # BM25-shaped, CJK-bigram-aware) so ranking and adapter-side filtering can
    # never drift apart; the transparent blend below is unchanged.
    terms = relevance.query_terms(query or "")
    now = datetime.now(timezone.utc)

    rels = relevance.doc_scores(docs, query or "")
    scored = [[d, rel, _recency(d, now), _engagement(d),
               int((d.metadata or {}).get("corroboration", 1)),
               float((d.metadata or {}).get("recall_rrf") or 0.0)]
              for d, rel in zip(docs, rels)]
    max_rel = max((s[1] for s in scored), default=0.0) or 1.0
    # The Phase-2 vector-recall prior (RRF). Only the perception-memory hybrid path stamps it; live
    # docs never carry it, so max_rrf=0 ⇒ rel_term below is EXACTLY today's rel/max_rel (Phase-1
    # byte-identical). It exists to stop merge_rank from burying a doc whose relevance is SEMANTIC
    # (cross-lingual / paraphrase) rather than LEXICAL — the only thing the vector layer must earn.
    max_rrf = max((s[5] for s in scored), default=0.0) or 1.0

    def composite(rel: float, rec: float, eng: float, corrob: int, rrf: float) -> float:
        # A vector-only hit has rel≈0 (that's WHY lexical missed it); lift it by its normalized RRF
        # rank-prior instead of flooring it. A strong lexical doc (rel/max_rel≈1) is unchanged — the
        # max() picks lexical. Mechanical (a max of two 0-1 normalized values); coefficients untouched.
        rel_term = max(rel / max_rel, rrf / max_rrf) if rrf > 0.0 else (rel / max_rel)
        base = (0.60 * rel_term + 0.25 * rec + 0.15 * eng) if terms else (0.70 * rec + 0.30 * eng)
        # Cross-source corroboration is a SIGNAL, not truth: a light multiplicative nudge (max
        # ~+15% at 4+ distinct sources) so a work several sources independently surface floats up,
        # while the transparent base blend still decides. The agent re-judges via .corroboration.
        return base * (1.0 + 0.15 * min(1.0, (corrob - 1) / 3.0))

    scored.sort(key=lambda s: composite(s[1], s[2], s[3], s[4], s[5]), reverse=True)
    out: list[Document] = []
    for d, rel, rec, eng, corrob, rrf in scored[:limit]:
        d.metadata = {**(d.metadata or {}), "_rank": round(composite(rel, rec, eng, corrob, rrf), 3)}
        # Passive metadata stamps (Phase-A). MECHANICAL measurements the agent interprets;
        # NONE feed composite()/the ranking blend (the razor: measure, don't rank by them).
        # Each is fail-open so one signal's failure never corrupts the return for the others.
        # --- #10 freshness_days + freshness_class: mechanical age bucketing (agent interprets).
        # Already feeds ranking via _recency; these are pure metadata. Timezone-naive dates
        # handled the same way as _recency (line ~147-148).
        try:
            _dd = d.date
            if _dd is not None:
                if _dd.tzinfo is None:
                    _dd = _dd.replace(tzinfo=timezone.utc)
                _fd = round((now - _dd).total_seconds() / 86400.0, 1)
            else:
                _fd = None
            _fc = ('breaking' if _fd is not None and _fd <= 1 else
                   'recent' if _fd is not None and _fd <= 7 else
                   'current' if _fd is not None and _fd <= 30 else
                   'dated' if _fd is not None and _fd <= 365 else
                   'archival' if _fd is not None else None)
            d.metadata["freshness_days"] = _fd
            d.metadata["freshness_class"] = _fc
        except Exception:
            pass
        # --- #14 relevance_hook: EXTRACTIVE substring from the doc's own text matching
        # the query terms (empty in browse mode). Not generative — the eye does not editorialize.
        try:
            d.metadata["relevance_hook"] = _extract_hook(d, terms)
        except Exception:
            pass
        # --- handles: per-doc affordance detection (pure pattern match, never a suggestion).
        # "There IS a door" (measurement), not "you SHOULD open it" (judgment).
        try:
            _h: dict[str, list[str] | bool] = {}
            _urls = [u for u in (d.media or []) + ([d.url] if d.url else []) if u]
            _trans = [u for u in _urls if any(dom in u for dom in
                      ('xiaoyuzhou.com', 'typlog.com', 'bilibili.com', 'b23.tv',
                       'podcasts.apple.com'))
                      or u.rsplit('.', 1)[-1].lower() in
                      ('mp3', 'm4a', 'wav', 'aac', 'ogg', 'flac', 'opus')]
            _capt = [u for u in _urls if 'youtube.com' in u or 'youtu.be' in u]
            if _trans:
                _h['transcribable'] = _trans
            if _capt:
                _h['captioned'] = _capt
            _eids = (d.metadata or {}).get('external_ids', {})
            _enrich = [v for k, v in _eids.items()
                       if k.lower() in ('doi', 'arxiv') and v]
            if _enrich:
                _h['enrichable'] = _enrich
            _h['has_comments'] = bool((d.metadata or {}).get('comments'))
            if _h.get('transcribable') or _h.get('captioned') or _h.get('enrichable') or _h['has_comments']:
                d.metadata['handles'] = _h
        except Exception:
            pass
        out.append(d)
    return out
