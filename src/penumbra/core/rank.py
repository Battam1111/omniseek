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

import math
import re
from datetime import datetime, timezone

from penumbra.core import relevance
from penumbra.core.normalize import Document

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
            # --- #11 signal_conflicts: same-named Signal values diverging >50% across the
            #     group's different-source members. Detected HERE because the merge is the
            #     only place the collapsed members are still visible (post-dedup one survivor
            #     per group remains, so any later cross-doc comparison compares DIFFERENT
            #     works — the 2026-07-01 dogfood noise). Mechanical flag; never fed to
            #     ranking (the razor); fail-open.
            try:
                _confs: list[dict] = []
                for _i, _da in enumerate(grp):
                    for _db in grp[_i + 1:]:
                        if _da.source == _db.source or len(_confs) >= 3:
                            continue
                        for _nm in set(_da.signals or {}) & set(_db.signals or {}):
                            _v1, _v2 = _da.signals[_nm].value, _db.signals[_nm].value
                            if (_v1 is not None and _v2 is not None and _v1 > 0 and _v2 > 0
                                    and max(_v1, _v2) / min(_v1, _v2) > 1.5):
                                _confs.append({
                                    "topic": _nm,
                                    "source_a": _da.source,
                                    "claim_a": f"{_nm}={_v1} ({_da.signals[_nm].unit or ''})",
                                    "source_b": _db.source,
                                    "claim_b": f"{_nm}={_v2} ({_db.signals[_nm].unit or ''})",
                                })
                if _confs:
                    add["signal_conflicts"] = _confs[:3]
            except Exception:  # noqa: BLE001 — one signal's failure never corrupts the merge
                pass
        # Curator P2 attribution stamps (pure facts, no judgment; stamped on EVERY survivor,
        # singletons included, so the yield tap can TRUST field presence rather than infer absence).
        # live_sources: group members that were LIVE this run (a recall-index rehydration carries
        #   metadata.from_index=True and is NOT live: the live feed went quiet, recall carries it).
        # index_only: the WHOLE group came from recall (no live member this run).
        # merge_basis: did this group collapse on a STRONG id (doi/arxiv/url) or only a shared long
        #   TITLE? A title-only merge across sources is WEAK corroboration (two distinct same-titled
        #   jobs merge; see fingerprint caveat) and must NOT strip a sole-contributor credit.
        live_srcs = sorted({d.source for d in grp if not (d.metadata or {}).get("from_index")})
        add["live_sources"] = live_srcs
        add["index_only"] = (len(live_srcs) == 0)
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
