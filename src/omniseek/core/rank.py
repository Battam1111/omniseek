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
from typing import Callable, Optional

from omniseek.core import relevance
from omniseek.core.normalize import Document

logger = logging.getLogger(__name__)

from omniseek.core.recall import graph as _graph  # noqa: E402 — the graph write verb + mint registry

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
    # keep EVERY Unicode letter + digit, drop the rest; lowercase. Superset of relevance.tokenize's
    # letter coverage (anti-drift: no script the tokenizer can see may be fingerprint-blind). The old
    # class kept only ascii+CJK, so a Korean / Cyrillic title normalized to "" and could never act
    # as a merge key (guarded from wrong-merging only by _TITLE_MIN).
    return re.sub(r"[\W_]+", "", (t or "").lower())


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


def _strong_ids(doc: Document) -> set:
    """The STRONG work ids (DOI / arXiv only) a doc exposes, for id-reconciliation. A DOI or arXiv id is
    a UNIQUE work identity, so two docs sharing one ARE the same work even when their long titles differ
    (a bilingual CN/EN rename, a preprint-vs-published retitle) -- exactly the case title-first
    fingerprinting structurally misses. Restricted to doi/arxiv (never a generic external_ids key). Pure."""
    ids: set = set()
    meta = doc.metadata or {}
    ext = meta.get("external_ids")
    ext = ext if isinstance(ext, dict) else {}
    for _d in (meta.get("doi"), ext.get("doi")):
        if _d:
            ids.add("doi:" + _norm_doi(_d))
    if ext.get("arxiv"):
        ids.add("arxiv:" + str(ext["arxiv"]).strip())
    m = _ARXIV_RE.search(doc.url or "")
    if m:
        ids.add("arxiv:" + m.group(1))
    m = _DOI_RE.search(doc.url or "")
    if m:
        ids.add("doi:" + _norm_doi(m.group(0)))
    return ids


def _pick_best(group: list[Document]) -> Document:
    """Richest representative of a duplicate group: most content, then score."""
    return max(group, key=lambda d: (len(d.content or ""), d.attention_value() or 0))


def _enrich_handle(doc: Document):
    """The id omniseek_paper_enrich needs (OA PDF / retraction / citation count), in ANY form it may be
    carried. S2 buries doi + arxiv under metadata.external_ids and fetcher._place_scholarly_fields
    flattens them AFTER ranking, so a check that looks only at metadata.doi reports a handle missing
    when it is merely not yet flattened (that false reading measured 25% of groups 'losing' a handle;
    the true figure, counting every form, is 2%). Returns None only when the doc really has none."""
    m = doc.metadata or {}
    ext = m.get("external_ids")
    ext = ext if isinstance(ext, dict) else {}
    return (m.get("doi") or m.get("arxiv_id") or m.get("paper_id")
            or ext.get("DOI") or ext.get("ArXiv") or None)


def dedup(docs: list[Document],
          backend_of: Optional[Callable[[str], str]] = None) -> list[Document]:
    """Collapse cross-source duplicates; annotate survivors with ``also_in``.

    ``backend_of`` (injected by the caller to avoid a rank->fetcher import cycle) maps a source name
    to its upstream BACKEND, so ``corroboration`` counts DISTINCT backends rather than source names —
    the OpenAlex family (openalex + openalex_cn + org_watch slices) is ONE independent voice, not
    four. Absent → the historical source-name count (byte-identical for callers that do not inject it)."""
    groups: dict[str, list[Document]] = {}
    order: list[str] = []
    for d in docs:
        fp = fingerprint(d)
        if fp not in groups:
            groups[fp] = []
            order.append(fp)
        groups[fp].append(d)

    # ID-RECONCILIATION (litstudy DocumentIdentifier.matches): TITLE-first fingerprinting never reaches
    # the DOI/arXiv id for a long title, so two docs with the SAME exact DOI but DIFFERENT long titles (a
    # bilingual CN/EN rename, a preprint-vs-published retitle) land on distinct 'title:' keys and never
    # merge. Union any fingerprint GROUPS that share a strong id. ADDITIVE: it can only MERGE, never split
    # (title-first is preserved), and an exact-id merge is SAFER than a title merge (a DOI is a unique work
    # id; it cannot false-merge two distinct same-titled items the way a shared long title can).
    _fp_ids = {fp: set().union(*(_strong_ids(d) for d in grp)) for fp, grp in groups.items()}
    _parent = {fp: fp for fp in order}

    def _find(x: str) -> str:
        while _parent[x] != x:
            _parent[x] = _parent[_parent[x]]
            x = _parent[x]
        return x

    _seen_id: dict[str, str] = {}
    for fp in order:
        for _sid in _fp_ids[fp]:
            if _sid in _seen_id:
                _a, _b = _find(_seen_id[_sid]), _find(fp)
                if _a != _b:
                    _parent[_b] = _a
            else:
                _seen_id[_sid] = fp
    id_union_fps: set = set()
    if any(_find(fp) != fp for fp in order):  # at least one id-union happened -> rebuild groups
        _rep_members: dict[str, list[Document]] = {}
        _merged_order: list[str] = []
        _rep_count: dict[str, int] = {}
        for fp in order:  # first-seen order preserved; the union anchor is the earliest fp in each union
            root = _find(fp)
            _rep_count[root] = _rep_count.get(root, 0) + 1
            if root not in _rep_members:
                _rep_members[root] = []
                _merged_order.append(root)
            _rep_members[root].extend(groups[fp])
        id_union_fps = {root for root, c in _rep_count.items() if c > 1}  # reps that absorbed >1 fp
        groups, order = _rep_members, _merged_order

    out: list[Document] = []
    for fp in order:
        grp = groups[fp]
        best = _pick_best(grp)
        add: dict = {}
        if len(grp) > 1:
            srcs = {d.source for d in grp}
            others = sorted(srcs - {best.source})
            if others:
                add["also_in"] = others  # provenance stays SOURCE names (the agent reads these)
            # Corroboration = distinct independent BACKENDS, not source names: the OpenAlex family
            # (openalex + openalex_cn + org_watch slices) shares one corpus + budget + breaker, so a
            # work surfaced by 4 of its slices is ONE independent voice, not four — counting names
            # inflates the independence signal the agent triangulates on. Absent backend_of → the
            # historical source-name count.
            backends = {backend_of(d.source) for d in grp} if backend_of else srcs
            if len(backends) > 1:
                add["corroboration"] = len(backends)
            # ① carry the RICHEST scholarly IDENTITY across the merge (2026-07-15): if the survivor is NOT
            #    the OpenAlex member but a collapsed OpenAlex duplicate carries structured authorships (exact
            #    person ids + institutions), preserve them so the placement lift
            #    (fetcher._place_scholarly_fields) surfaces the identity layer on the survivor -- else an S2 /
            #    other-source survivor loses the OpenAlex identity at dedup (its doi often differs from
            #    OpenAlex's, so the collapse is TITLE-based). Safe on a title merge HERE: only an OpenAlex doc
            #    carries raw.authorships, so this is always a SCHOLARLY merge on a LONG paper title (distinct
            #    papers do not share a long title -- unlike short generic JOB titles), AND the merge already
            #    treats the group as ONE work (also_in / corroboration / citation-conflict); the carry only
            #    rides that existing same-work judgment, adding no new merge risk.
            _best_raw = (best.metadata or {}).get("raw")
            if not (isinstance(_best_raw, dict) and _best_raw.get("authorships")):
                for _m in grp:
                    if _m is best:
                        continue
                    _mm = _m.metadata or {}
                    _mraw = _mm.get("raw")
                    if isinstance(_mraw, dict) and isinstance(_mraw.get("authorships"), list) and _mraw["authorships"]:
                        add["_merged_authorships"] = _mraw["authorships"]
                        if not (best.metadata or {}).get("openalex_id") and _mm.get("openalex_id"):
                            add["openalex_id"] = _mm.get("openalex_id")
                        break
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
        add["merge_basis"] = "id" if (fp in id_union_fps or not fp.startswith("title:")) else "title"
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
        # Same reason the rrf stamp is preserved just above: _pick_best optimizes for CONTENT LENGTH,
        # so the richest-TEXT member can be the very one missing a field a sibling carried. Measured
        # 2026-07-25 over real broad-search corpora (46 duplicate groups): the survivor was UNDATED
        # while a sibling had a real date in 7% of groups, and carried no enrich handle at all in 2%.
        # Both are strict information LOSS, not a trade: the eye held the fact one document over and
        # dropped it. Losing the date now costs twice, since merge_rank scores recency off doc.date,
        # so a paper published days ago gets ranked as median-aged. The group is the SAME WORK by
        # construction (that is what this merge already asserts when it carries authorships and mints
        # conflict edges), so adopting a sibling's value adds no merge risk that is not already taken.
        # Provenance is stamped rather than implied: a borrowed fact is never presented as native.
        if best.date is None:
            for _m in grp:
                if _m.date is not None:
                    best.date = _m.date
                    add["date_from"] = _m.source
                    break
        if _enrich_handle(best) is None:
            for _m in grp:
                _mh = _enrich_handle(_m)
                if _mh is not None:
                    _mmeta = _m.metadata or {}
                    for _k in ("doi", "arxiv_id"):
                        if _mmeta.get(_k) and not (best.metadata or {}).get(_k):
                            add[_k] = _mmeta[_k]
                    add["ids_from"] = _m.source
                    break
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
        from omniseek.core.recall import writer
        writer.enqueue_graph([], edges)
    except Exception as exc:  # noqa: BLE001 — a tap failure must NEVER break the search
        logger.debug("conflicts graph tap swallowed: %s", exc)


def _recency(doc: Document, now: datetime, unknown: float = 0.3) -> float:
    """Age score: 1.0 today, 0.5 at ~30d. ``unknown`` is what an UNDATED doc scores.

    The 0.3 default is kept only for direct callers; merge_rank passes the candidate set's MEDIAN
    instead, because a FIXED floor turned out to reward missing metadata. Measured 2026-07-25 over
    real broad searches: 23% of the ranked corpus carries no date at all, and the sources concerned
    are entirely dateless (youtube 15/0, pmlr 10/0, transformer_circuits 10/0, ajo 5/0), while the
    DATED docs competing with them scored 0.04-0.29, i.e. essentially all BELOW the 0.3 floor. So
    "neutral-low" was neutral only against an imagined distribution; against the real one it was
    near the TOP, and an undated video outscored a genuinely 4-month-old paper on recency."""
    dt = doc.date
    if not dt:
        return unknown
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - dt).total_seconds() / 86400.0)
    return 1.0 / (1.0 + age_days / 30.0)  # 1.0 today, 0.5 at ~30d


def _engagement(doc: Document) -> float:
    """Absolute attention on a log scale. Retained for direct callers; merge_rank uses the
    WITHIN-SOURCE version below, because this one compares incomparable things (see _engagement_ranks)."""
    s = doc.attention_value() or 0
    if s <= 0:
        return 0.0
    return min(1.0, math.log10(1 + s) / 4.0)  # ~1.0 at score≈10k


def _engagement_ranks(docs: list[Document]) -> list[float]:
    """Engagement as a percentile WITHIN THE DOC'S OWN SOURCE, not on one absolute scale.

    WHY (measured 2026-07-25). attention_value() is a max over engagement AND citation signals, so
    the same number means YouTube views for one doc and citations for another, and the old
    log10(1+s)/4 put them on ONE curve. 10k views is unremarkable; 10k citations is legendary. The
    result in real searches: youtube docs scored 0.47-0.98 while semantic_scholar / crossref scored
    0.08-0.35, a standing ~0.1 advantage (of a 1.0 scale) for video over research, on top of the
    recency floor above. Three videos with LOWER relevance outranked the best-matching paper.

    Comparing a doc only to its OWN source's docs makes the number mean something again: "more
    watched than the other videos here" / "more cited than the other papers here". Scale-free, so no
    per-source constants are introduced, and it follows the same within-candidate-set normalization
    the relevance term already uses (max_rel).

    No attention signal at all still scores 0.0 (never invent attention), and a source contributing a
    single doc gets 0.5 (neutral, since it has no peers to beat) rather than a free 1.0."""
    vals = [(d.attention_value() or 0.0) for d in docs]
    peers: dict[str, list[float]] = {}
    for d, v in zip(docs, vals):
        if v > 0:
            peers.setdefault(d.source, []).append(v)
    out: list[float] = []
    for d, v in zip(docs, vals):
        if v <= 0:
            out.append(0.0)
            continue
        group = peers.get(d.source) or [v]
        below = sum(1 for p in group if p < v)
        equal = sum(1 for p in group if p == v)
        out.append((below + 0.5 * equal) / len(group))
    return out


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
    from omniseek.core.relevance import tokenize  # local import: keep the helper self-contained
    term_set = set(terms)
    best, best_n = '', 0
    for s in sents:
        n = len(term_set & set(tokenize(s)))
        if n > best_n:
            best, best_n = s, n
    if best_n == 0:
        return ''
    return best[:cap]


def merge_rank(results, query: str, limit: int = 15,
               backend_of: Optional[Callable[[str], str]] = None) -> list[Document]:
    """Flatten → dedup → rank. ``results`` is a search_many dict or a flat list. ``backend_of`` is
    passed through to ``dedup`` so corroboration counts distinct BACKENDS (injected by the caller to
    avoid a rank->fetcher import cycle; absent → the historical source-name count)."""
    if isinstance(results, dict):
        docs: list[Document] = []
        for lst in results.values():
            docs.extend(lst)
    else:
        docs = list(results)

    docs = dedup(docs, backend_of=backend_of)
    # Lexical relevance comes from the shared engine (omniseek.core.relevance:
    # BM25-shaped, CJK-bigram-aware) so ranking and adapter-side filtering can
    # never drift apart; the transparent blend below is unchanged.
    terms = relevance.query_terms(query or "")
    now = datetime.now(timezone.utc)

    rels = relevance.doc_scores(docs, query or "")
    # Both non-relevance terms are normalized WITHIN THE CANDIDATE SET, exactly as max_rel already
    # normalizes relevance. A fixed unknown-date floor and an absolute attention curve both silently
    # favoured whichever sources omit dates and count cheap units (see _recency / _engagement_ranks).
    _dated = [_recency(d, now) for d in docs if d.date]
    _unknown_rec = sorted(_dated)[len(_dated) // 2] if _dated else 0.3
    engs = _engagement_ranks(docs)
    scored = [[d, rel, _recency(d, now, unknown=_unknown_rec), eng,
               int((d.metadata or {}).get("corroboration", 1)),
               float((d.metadata or {}).get("recall_rrf") or 0.0)]
              for d, rel, eng in zip(docs, rels, engs)]
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
