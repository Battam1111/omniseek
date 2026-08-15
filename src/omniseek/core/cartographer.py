"""Field neighborhood — a THIN, MULTI-SOURCE graph-access primitive. No judgment.

Design principle: minimal code, maximal agent intelligence + multi-channel, agent routes.
This assembles the COMPLETE citation neighborhood of a field (seeds → references + citers),
dedup, raw metadata, one cheap signal (in-field in-degree) — over a CHOSEN graph source:

  • source="openalex" (default): the 250M OpenAlex graph — rich for established fields, but
    sparse/laggy for recent arXiv-heavy work (missing reference lists, undercounted citers).
  • source="s2": Semantic Scholar — far better arXiv citation coverage + accurate citation
    counts; the right channel for bleeding-edge fields (needs the S2 API key for reliable,
    un-throttled graph access).

It does NOT judge — no survey filtering, no relevance gating, no title repair, no clustering.
The AGENT routes (pick the source whose graph has THIS field's data; for a young field, often
the best "graph" is a human-curated survey/awesome-list it fetches itself) and does ALL the
cartography. The eye is the channel; the agent is the cartographer.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from typing import Optional

from omniseek.core import _openalex as oa
from omniseek.core import _s2
from omniseek.core import cache

logger = logging.getLogger(__name__)

_TIMEOUT = 25
# primary_location added (design section 11 open item): it carries the venue {id, display_name} the
# graph's ``published_in`` edge needs (work -> venue:openalex:S…). The other fields are unchanged.
_SELECT = ("id,doi,title,publication_year,publication_date,cited_by_count,referenced_works,"
           "concepts,authorships,primary_location")
_S2_REF_CAP = 100  # hard cap on S2 references fetched per seed (page-bounded; refs are naturally small)
# OVERALL wall-clock budget for one assemble. Each S2/OA call is individually bounded (timeout +
# breaker), but the ~13 SERIAL calls of an assemble have no aggregate cap, so a slow/throttling
# upstream could grind for MINUTES before the 5-consecutive-failure breaker trips (the field_skeleton
# "hung for minutes" symptom). This caps the whole operation: the assemble loop checks it between
# calls and bails early with whatever it has (a partial map, flagged), never hanging the agent. The
# happy path (~12s) is well under it; an agent mapping a huge field can raise deadline_s. Set to 45s
# (was 25s): a genuinely broad field (4 seeds x 25 citers) does ~13 calls paced at S2's ~1 RPS, so
# 25s could bail a NORMAL broad map partial before its edges finished; 45s covers the paced serial
# cost with margin while still bounding a truly-stuck upstream (the bail is a safety net, not a target).
_ASSEMBLE_DEADLINE_S = 45.0

# _norm_s2_id now lives in _s2.norm_s2_id (single source of truth, shared with relations).
# Kept as a module-level alias so the smoke gate + any external caller keep working.
_norm_s2_id = _s2.norm_s2_id

from omniseek.core.recall import graph  # noqa: E402 — the graph write verb + mint registry

# Vocabulary this tap MINTS (vocabulary-by-minting, design section 3): declared on the tap itself,
# registered at import, and folded into ``graph.declared_vocabulary`` as the computed union. The
# smoke tripwire bounds ACTUAL graph data to that union. cartographer mints the CITATION-graph core:
# work/person/topic/venue nodes and cites/authored/about/published_in edges, over the two academic
# backends (api:openalex, api:s2). authored/person and about/topic are OpenAlex-only in the tap (the
# S2 path has display-name-only authorships and id-less fields → mints NO person/topic there; the
# relations tap owns persons properly at P3), but the DECLARATION is the tap's full vocabulary.
GRAPH_MINTS = {
    "kinds": ["work", "person", "topic", "venue"],
    "edge_types": ["cites", "authored", "about", "published_in"],
    "methods": ["api:openalex", "api:s2"],
}
graph.register_mints("cartographer", kinds=GRAPH_MINTS["kinds"],
                     edge_types=GRAPH_MINTS["edge_types"], methods=GRAPH_MINTS["methods"])


def _wid(full: str) -> str:
    return (full or "").rsplit("/", 1)[-1]


# ── OpenAlex backend ──────────────────────────────────────────────────────────
def _get(path: str, params: dict) -> Optional[dict]:
    # Route through the shared _openalex client so cartographer / field_skeleton sit BEHIND the
    # same circuit breaker + politeness semaphore + pooled connection as the 40+ other OpenAlex
    # sources. This was the ONE path bypassing them with a bare httpx.get, so a dead/over-quota
    # OpenAlex would have degraded cartographer unprotected (and not contributed to tripping the
    # shared breaker). Preserve cartographer's None-on-failure contract: OpenAlexDown (breaker
    # open) and any error degrade to None, exactly as the old bare GET did.
    try:
        return oa.get_json(path, params)
    except Exception as exc:  # noqa: BLE001 — incl. OpenAlexDown: degrade to None
        logger.warning("cartographer OpenAlex GET %s failed: %s", path, exc)
        return None


def _ids(data: Optional[dict]) -> list[str]:
    return [_wid(w.get("id", "")) for w in ((data or {}).get("results") or []) if w.get("id")]


def _oa_cited_by(work_id: str, cap: int, sort: str = "cited_by_count:desc") -> list[str]:
    return _ids(_get("/works", {"filter": f"cites:{work_id}", "sort": sort,
                                "per-page": min(cap, 200), "select": "id"}))


def _oa_venue(work: dict) -> Optional[dict]:
    """The venue {id, display_name} off an OpenAlex work's ``primary_location.source`` (added to
    _SELECT), with the source id reduced to the bare ``S…`` (``_wid``) so the tap mints
    ``venue:openalex:S…`` directly. None when the work has no primary_location source (e.g. a
    preprint with no indexed venue) — ``published_in`` simply isn't minted for it. No judgment: this
    only reshapes what OpenAlex already returned."""
    loc = work.get("primary_location") or {}
    src = loc.get("source") if isinstance(loc, dict) else None
    if not isinstance(src, dict) or not src.get("id"):
        return None
    return {"id": _wid(src.get("id", "")), "display_name": src.get("display_name")}


def _oa_fetch(ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 50):
        chunk = [c for c in ids[i:i + 50] if c]
        if not chunk:
            continue
        data = _get("/works", {"filter": "openalex_id:" + "|".join(chunk),
                               "per-page": 50, "select": _SELECT})
        for w in ((data or {}).get("results") or []):
            # Carry the venue {id, display_name} through the works dict when present (the SAME shape
            # the tap reads for published_in). The S2 path has no venue id, so it stays absent there.
            venue = _oa_venue(w)
            if venue:
                w["venue"] = venue
            out[_wid(w.get("id", ""))] = w
    return out


def _oa_assemble(query: Optional[str], seeds: Optional[list[str]],
                 n_seeds: int, citers_per_seed: int,
                 deadline_at: Optional[float] = None) -> tuple[list[str], dict, bool]:
    seed_ids = [_wid(s) for s in seeds] if seeds else _ids(_get(
        "/works", {"search": query or "", "per-page": n_seeds,
                   "sort": "relevance_score:desc", "select": "id"}))
    if not seed_ids:
        return [], {}, False
    seed_works = _oa_fetch(seed_ids)
    refs = {_wid(r) for w in seed_works.values() for r in (w.get("referenced_works") or [])}
    citers: set[str] = set()
    deadline_hit = False
    for s in seed_ids:
        if deadline_at and time.monotonic() >= deadline_at:  # overall budget blown -> stop, return partial
            deadline_hit = True
            break
        citers |= set(_oa_cited_by(s, citers_per_seed))
        citers |= set(_oa_cited_by(s, citers_per_seed, "publication_date:desc"))
    return seed_ids, _oa_fetch(list(set(seed_ids) | refs | citers)), deadline_hit


# ── Semantic Scholar backend (better arXiv coverage; normalized to the same shape) ──
def _s2_assemble(query: Optional[str], seeds: Optional[list[str]],
                 n_seeds: int, citers_per_seed: int,
                 deadline_at: Optional[float] = None) -> tuple[list[str], dict, bool]:
    # Shared S2 client + breaker + bounded wrappers (_s2) — no per-call SemanticScholar().
    F = ["paperId", "title", "year", "publicationDate", "citationCount", "authors",
         "externalIds", "fieldsOfStudy"]
    # edge-level fields, valid ONLY on citation/reference calls. contextsWithIntent carries the
    # RAW citing SENTENCE (+ S2's own per-sentence intents) so the AGENT can judge a citation's
    # POLARITY (supporting / contrasting / mentioning): a JUDGMENT the eye must NOT make. We fetch
    # contexts too as a fallback for older S2 lib versions that lack the paired field.
    F_EDGE = F + ["intents", "isInfluential", "contextsWithIntent", "contexts"]
    works: dict[str, dict] = {}
    _CTX_CAP = 8  # bound the raw citing-sentence list per node (payload guard; agent reads what's there)

    def add(p, seed: bool = False, intents=None, influential=None,
            contexts=None, cites: Optional[str] = None) -> None:
        pid = getattr(p, "paperId", None)
        if not pid:
            return
        ext = getattr(p, "externalIds", None) or {}
        doi, arx = ext.get("DOI"), ext.get("ArXiv")
        prev = works.get(pid, {})
        # Merge the raw citing-sentence facts, deduped by sentence text, surviving re-add() (a node
        # may cite multiple seeds → multiple edges). Each entry is {snippet, intents}: the SENTENCE
        # (the polarity evidence the agent reads) + S2's own intents for that sentence (a FACT, not
        # our verdict). No classification here; we only carry what S2 already returned.
        _ctx = list(prev.get("_contexts") or [])
        _seen = {c.get("snippet") for c in _ctx}
        for c in (contexts or []):
            snip = c.get("snippet")
            if snip and snip not in _seen and len(_ctx) < _CTX_CAP:
                _ctx.append(c)
                _seen.add(snip)
        works[pid] = {
            "title": getattr(p, "title", None) or prev.get("title"),
            "publication_year": getattr(p, "year", None) or prev.get("publication_year"),
            "publication_date": getattr(p, "publicationDate", None) or prev.get("publication_date"),
            "cited_by_count": getattr(p, "citationCount", None) or prev.get("cited_by_count") or 0,
            "doi": ("https://doi.org/" + doi) if doi else prev.get("doi"),
            "_url": (("https://doi.org/10.48550/arXiv." + arx) if arx
                     else f"https://www.semanticscholar.org/paper/{pid}"),
            "authorships": [{"author": {"display_name": getattr(a, "name", None)}}
                            for a in (getattr(p, "authors", None) or [])],
            "concepts": [{"display_name": f, "level": 1} for f in (getattr(p, "fieldsOfStudy", None) or [])],
            # Accumulate the fetched (citing → cited) edges, surviving re-add() merges (a wipe on
            # re-add discarded every edge → n_edges/in_degree stayed 0). The seed loop records both
            # the seed→reference and citer→seed directions here; _build intersects against the
            # corpus, so out-of-corpus refs drop and only in-field edges count.
            "referenced_works": list(prev.get("referenced_works") or []),
            "_seed": seed or prev.get("_seed", False),
            # S2-only edge semantics (the one open source with citation intents): how this node
            # and the seeds cite each other. isInfluential = S2 flags the link as substantive.
            "_intents": sorted(set((prev.get("_intents") or []) + list(intents or []))),
            "_influential": bool(prev.get("_influential")) or bool(influential),
            # The RAW citing sentences (capped, deduped): the agent's evidence for polarity.
            "_contexts": _ctx,
        }
        if cites and cites != pid:
            works[pid]["referenced_works"].append(cites)

    def _edge_contexts(edge) -> list[dict]:
        """Extract S2's raw citing sentences off ONE edge as [{snippet, intents}] FACTS. Prefer
        contextsWithIntent (sentence + its S2 intents); fall back to bare contexts (sentence only)
        for older lib versions. No judgment; just normalize what S2 returned. Empty when S2 never
        parsed the citing PDF (a fact, not a bug)."""
        cwi = getattr(edge, "contextsWithIntent", None) or []
        out = []
        for c in cwi:
            # lib elements are dict-like {"context": str, "intents": [...]}; tolerate attr-style too
            snip = (c.get("context") if isinstance(c, dict) else getattr(c, "context", None))
            ints = (c.get("intents") if isinstance(c, dict) else getattr(c, "intents", None)) or []
            if snip:
                out.append({"snippet": snip, "intents": list(ints)})
        if not out:
            for snip in (getattr(edge, "contexts", None) or []):
                if snip:
                    out.append({"snippet": snip, "intents": []})
        return out

    seed_ids = list(seeds) if seeds else []
    if not seed_ids:
        # _s2.search_paper is hard-bounded (page size = n_seeds, stop at n_seeds) + degrades to [].
        res = _s2.search_paper(query or "", limit=n_seeds, fields=F)
        for p in res:
            add(p, seed=True)
        seed_ids = [getattr(p, "paperId", None) for p in res if getattr(p, "paperId", None)]
    if not seed_ids:
        return [], {}, False
    deadline_hit = False

    def _expired() -> bool:
        return bool(deadline_at and time.monotonic() >= deadline_at)

    for sid in seed_ids:
        # Overall budget guard between every network call: the ~13 serial calls otherwise have no
        # aggregate cap, so a slow/throttling S2 grinds for minutes before the breaker trips. On the
        # budget we stop and return the PARTIAL map gathered so far (flagged), never hanging the agent.
        if _expired():
            deadline_hit = True
            break
        # _s2 wrappers normalize the id + bound the iteration (the "limit == page size, paginate
        # forever" trap) + carry the breaker/semaphore + degrade to None/[] — so no try/except here.
        add(_s2.get_paper(sid, fields=F), seed=True)
        if _expired():
            deadline_hit = True
            break
        for r in _s2.get_paper_references(sid, _S2_REF_CAP, fields=F_EDGE):
            # edge seed → r.paper (the seed CITES this reference): add the neighbor, then record
            # the edge on the SEED (the "this work cites these ids" direction _build expects).
            rp = getattr(r, "paper", None)
            add(rp, intents=getattr(r, "intents", None),
                influential=getattr(r, "isInfluential", None),
                contexts=_edge_contexts(r))
            rpid = getattr(rp, "paperId", None)
            if rpid and sid in works and rpid != sid:
                works[sid]["referenced_works"].append(rpid)
        if _expired():
            deadline_hit = True
            break
        for c in _s2.get_paper_citations(sid, citers_per_seed, fields=F_EDGE):
            # edge c.paper → seed (the CITER cites the seed): record the edge on the citer via
            # cites=sid, so c.paper's referenced_works carries sid.
            add(getattr(c, "paper", None), intents=getattr(c, "intents", None),
                influential=getattr(c, "isInfluential", None),
                contexts=_edge_contexts(c), cites=sid)
    seed_pids = [pid for pid, w in works.items() if w.get("_seed")] or seed_ids
    return seed_pids, works, deadline_hit


# ── shared node-building (source-agnostic; reads the normalized work shape) ──────
# Budgeted-projection caps for the S2 citing-sentence evidence (`contexts`), so the field-map stays within
# the tool channel instead of dumping every node's full sentences (dogfood friction #11).
_CTX_TOP_NODES = 15          # only the top nodes (by in_degree order) keep their citing sentences
_CTX_SNIPPETS_PER_NODE = 2   # citing sentences kept per such node
_CTX_SNIPPET_CHARS = 240     # per-sentence character cap


_EDGES_CAP = 2000  # cap the in-corpus edge list _build returns (2-elem short-id pairs; ~120KB worst case)


def _build(seed_ids: list[str], works: dict, max_nodes: int, query: Optional[str] = None) -> dict:
    seed_set = set(seed_ids)
    indeg: Counter = Counter()  # in-field in-degree (only where referenced_works are present)
    edges: list[list[str]] = []          # in-corpus [citer_id, cited_id] pairs (the citation DAG)
    seed_ref_freq: Counter = Counter()   # non-seed work -> # SEEDS that reference it (missing foundational refs)
    seed_cite_freq: Counter = Counter()  # non-seed work -> # SEEDS it cites (missing frontier citers)
    for wid_, w in works.items():
        raw_refs = w.get("referenced_works") or []
        for r in raw_refs:
            t = _wid(r)
            if t in works:
                indeg[t] += 1              # in_degree semantics UNCHANGED (self-cite still counted as before)
                if t != wid_:
                    edges.append([wid_, t])  # no self-loop in the exposed edge list
        # Seed-relative gap frequencies (LCN Top-Cited / Top-Citing, ranked by # DISTINCT seeds).
        refs = {_wid(r) for r in raw_refs}
        if wid_ in seed_set:
            for t in refs:
                if t in works and t not in seed_set:
                    seed_ref_freq[t] += 1    # a seed references t -> t is a candidate MISSING foundational ref
        else:
            seed_cite_freq[wid_] = len(refs & seed_set)  # a non-seed work citing N seeds -> a MISSING frontier citer
    nodes = []
    for wid_, w in works.items():
        cs = [c for c in (w.get("concepts") or []) if isinstance(c, dict) and c.get("display_name")]
        mid = [c for c in cs if (c.get("level") or 0) >= 1]
        auths = w.get("authorships") or []
        doi = w.get("doi")
        node = {
            "id": wid_,
            "title": (w.get("title") or "(untitled)")[:200],
            "year": w.get("publication_year"),
            "date": w.get("publication_date"),
            "cited_by": w.get("cited_by_count") or 0,
            "in_degree": indeg.get(wid_, 0),
            "concept": ((mid or cs or [{}])[0]).get("display_name") or None,
            "first_author": ((auths[0].get("author") or {}).get("display_name")
                             if auths and isinstance(auths[0], dict) else None),
            "doi": doi,
            "url": doi or w.get("_url") or f"https://openalex.org/{wid_}",
            "is_seed": wid_ in seed_set,
        }
        if wid_ not in seed_set:  # seed-relative gap stamps (a seed is never "missing"); passive, no re-sort
            node["seed_ref_freq"] = seed_ref_freq.get(wid_, 0)    # # seeds referencing this (missing foundational ref)
            node["seed_cite_freq"] = seed_cite_freq.get(wid_, 0)  # # seeds this cites (missing frontier citer)
        if "_influential" in w:  # S2 channel only: surface the citation-edge semantics
            node["influential"] = bool(w.get("_influential"))
            if w.get("_intents"):
                node["intent"] = w["_intents"]
            # The RAW citing sentence(s): the EVIDENCE for citation POLARITY (supporting /
            # contrasting / mentioning). The eye exposes the FACT; the AGENT reads the snippet and
            # judges polarity itself (no classifier in the eye). intents is S2's own per-sentence
            # label (background/methodology/result), a FACT, NOT a polarity verdict. Often empty
            # when S2 never parsed the citing PDF.
            if w.get("_contexts"):
                node["contexts"] = w["_contexts"]
        nodes.append(node)
    nodes.sort(key=lambda x: (x["in_degree"], x["cited_by"]), reverse=True)  # default view; agent re-judges
    nodes = nodes[:max_nodes]
    # Expose the in-corpus citation DAG (the edges _build already walked to derive in_degree) so the agent
    # can build the citation / co-citation / bibliographic-coupling networks itself (measure, don't rank).
    # Filter to SURVIVING nodes (no dangling endpoint after truncation) + cap the payload.
    _kept = {n["id"] for n in nodes}
    edges = [e for e in edges if e[0] in _kept and e[1] in _kept]
    edges_capped = len(edges) > _EDGES_CAP
    edges = edges[:_EDGES_CAP]
    # BUDGETED PROJECTION (dogfood friction #11): the RAW citing sentences (`contexts`) are heavy polarity
    # evidence -- field_skeleton used to inline EVERY node's full contexts, a ~150k-char dump that overflowed
    # the tool channel, violating the eye's "budgeted projections, never dump" discipline. Keep the citing
    # sentences ONLY for the top nodes worth reading (by in_degree order, seeds first), capped in count + length;
    # for the rest, drop them for a lean `has_contexts` flag (drill THAT paper for its full citing sentences).
    for _i, _n in enumerate(nodes):
        _ctx = _n.get("contexts")
        if not _ctx:
            continue
        if _i < _CTX_TOP_NODES:
            _n["contexts"] = [{"snippet": str(_c.get("snippet") or "")[:_CTX_SNIPPET_CHARS],
                               "intents": _c.get("intents")}
                              for _c in _ctx[:_CTX_SNIPPETS_PER_NODE] if isinstance(_c, dict)]
        else:
            _n.pop("contexts", None)
            _n["has_contexts"] = True
    # PASSIVE query-relevance stamp (mirrors rank.py's "measure, don't rank by them"): when a query
    # is given, score each node's lexical relevance to it and attach as metadata WITHOUT touching the
    # (in_degree, cited_by) sort above. field_skeleton computes relevance only to pick seeds, then
    # discards it for the neighborhood; this hands back that one thrown-away signal so the agent can
    # re-sort a hub-heavy field's frontier by on-query relevance. Titles-only (thinner than merge_rank),
    # in-process + CJK-aware, zero new dependency. Fail-open: a scoring hiccup never breaks the map.
    if query:
        try:
            from omniseek.core import relevance
            _scores = relevance.field_scores(
                [[(_n["title"] or "", 1.0), (_n.get("concept") or "", 0.3)] for _n in nodes], query)
            for _n, _s in zip(nodes, _scores):
                _n["query_relevance"] = round(_s, 3)
        except Exception:  # noqa: BLE001 -- a relevance hiccup must never break the neighborhood map
            pass
    return {"seeds": seed_ids, "n_nodes": len(nodes), "n_edges": sum(indeg.values()),
            "edges": edges, "edges_capped": edges_capped, "nodes": nodes}


def _graph_tap(source: str, works: dict) -> None:
    """FAIL-OPEN graph write tap (design section 6): mint the citation-graph facts from the assembled
    ``works`` dict into the graph via the single-writer queue. Runs AFTER ``_build`` at the one exit
    point both backends share; a failure here NEVER touches the field_skeleton result the agent gets
    (the whole body is wrapped + ``enqueue_graph`` never raises). Volume is bounded by the assemble's
    existing max_nodes/caps (we mint only from what ``works`` already holds); upserts are idempotent
    so a cache-hit re-call re-mints = an honest last_seen bump.

    What it mints (per the P2 row of the taps table + the vocabulary this tap declared):
      • work nodes   — ``work:openalex:W…`` / ``work:s2:{id}`` per backend; label=title; attrs
                       {doi, year, cited_by}.
      • cites edges  — work→work for the IN-CORPUS referenced_works ``_build`` already intersects
                       (``_wid(r) in works``); tier M, method ``api:{backend}``; attrs
                       intents/influential/contexts ONLY as the existing extraction already capped
                       them (node-level S2 edge semantics; caps never expanded).
      • authored     — OpenAlex authorships that carry an author id → ``person:openalex:A…`` nodes
                       (label=display_name) + work→person edges (M, api:openalex). The S2 path has
                       display-name-only authorships: mint NOTHING for those (the relations tap owns
                       persons properly at P3; no label-persons here).
      • about        — concepts WITH an OpenAlex id → ``topic:openalex:C…`` + work→topic (M). Concepts
                       without an id are SKIPPED (no label-topic pollution from this tap).
      • published_in — work→``venue:openalex:S…`` when primary_location carried a source id.
    """
    try:
        from omniseek.core.recall import writer
        ns = "s2" if source == "s2" else "openalex"
        method = "api:s2" if source == "s2" else "api:openalex"
        is_oa = ns == "openalex"

        def work_nid(wid_: str) -> str:
            return f"work:{ns}:{wid_}"

        nodes: list[dict] = []
        edges: list[dict] = []
        for wid_, w in works.items():
            if not wid_:
                continue
            src_nid = work_nid(wid_)
            # work node: label=title, attrs {doi (when present), year, cited_by}. doi is stored as
            # the work dict already carries it (the canonical https://doi.org/… form, matching _build).
            attrs: dict = {"year": w.get("publication_year"), "cited_by": w.get("cited_by_count") or 0}
            if w.get("doi"):
                attrs["doi"] = w.get("doi")
            nodes.append({"id": src_nid, "kind": "work",
                          "label": (w.get("title") or None), "attrs": attrs})

            # cites edges: work -> in-corpus referenced work. The intents/influential/contexts are the
            # CITING node's S2 edge semantics (present on S2 works only), carried as-is (already capped
            # by add()'s _CTX_CAP / the deduped intents set — never expanded here).
            cite_attrs: dict = {}
            if w.get("_intents"):
                cite_attrs["intents"] = w["_intents"]
            if "_influential" in w:
                cite_attrs["influential"] = bool(w.get("_influential"))
            if w.get("_contexts"):
                cite_attrs["contexts"] = w["_contexts"]
            for r in (w.get("referenced_works") or []):
                t = _wid(r)
                if t and t in works and t != wid_:
                    edge = {"src": src_nid, "dst": work_nid(t), "type": "cites",
                            "tier": "M", "method": method}
                    if cite_attrs:
                        edge["attrs"] = dict(cite_attrs)
                    edges.append(edge)

            if is_oa:
                # authored: OpenAlex authorships WITH an author id (S2 has display-name-only → skipped).
                for a in (w.get("authorships") or []):
                    if not isinstance(a, dict):
                        continue
                    au = a.get("author")
                    if not isinstance(au, dict) or not au.get("id"):
                        continue
                    pid = _wid(au.get("id", ""))
                    if not pid:
                        continue
                    person_nid = f"person:openalex:{pid}"
                    nodes.append({"id": person_nid, "kind": "person",
                                  "label": au.get("display_name") or None, "attrs": None})
                    edges.append({"src": src_nid, "dst": person_nid, "type": "authored",
                                  "tier": "M", "method": "api:openalex"})

                # about: concepts WITH an OpenAlex id (id-less concepts skipped → no label-topics).
                for c in (w.get("concepts") or []):
                    if not isinstance(c, dict) or not c.get("id"):
                        continue
                    cid = _wid(c.get("id", ""))
                    if not cid:
                        continue
                    topic_nid = f"topic:openalex:{cid}"
                    nodes.append({"id": topic_nid, "kind": "topic",
                                  "label": c.get("display_name") or None, "attrs": None})
                    edges.append({"src": src_nid, "dst": topic_nid, "type": "about",
                                  "tier": "M", "method": "api:openalex"})

                # published_in: work -> venue when primary_location carried a source id (see _oa_venue).
                venue = w.get("venue")
                if isinstance(venue, dict) and venue.get("id"):
                    venue_nid = f"venue:openalex:{venue['id']}"
                    nodes.append({"id": venue_nid, "kind": "venue",
                                  "label": venue.get("display_name") or None, "attrs": None})
                    edges.append({"src": src_nid, "dst": venue_nid, "type": "published_in",
                                  "tier": "M", "method": "api:openalex"})

        writer.enqueue_graph(nodes, edges)
    except Exception as exc:  # noqa: BLE001 — a tap failure must NEVER break field_skeleton
        logger.debug("cartographer graph tap swallowed: %s", exc)


def field_skeleton(query: Optional[str] = None, seeds: Optional[list[str]] = None,
                   n_seeds: int = 4, citers_per_seed: int = 30, source: str = "openalex",
                   max_nodes: int = 250, fresh: bool = False,
                   deadline_s: Optional[float] = None) -> dict:
    """Assemble the COMPLETE raw citation neighborhood of a field over a CHOSEN graph source
    (``openalex`` default / ``s2``). Mechanical, no judgment — the AGENT routes + maps. Pass
    ``seeds`` (work/paper ids you chose) or ``query`` (auto-resolves top-relevance seeds).
    ``deadline_s`` (default ~25s) is the OVERALL wall-clock budget: on a slow/throttling upstream
    the assemble bails early with a PARTIAL map (``_meta.deadline_hit``) instead of hanging — raise
    it to map a large field more completely."""
    # A-class canonical key: normalize + sort the seeds so every id-FORM (bare arXiv / ArXiv: / DOI:)
    # and ORDERING of one seed SET collapses to a single cache row (a set defines the neighborhood;
    # order does not). Reuses the s2 normalizer _s2_assemble applies downstream; oa seeds are already
    # canonical W-ids, so just sort them for order-stability.
    _seed_norm = _s2.norm_s2_id if source == "s2" else (lambda s: s)
    seed_key = ",".join(sorted(_seed_norm(s) for s in (seeds or [])))
    key = cache.make_key("cartographer", "nbhd", source, query or "",
                         seed_key, n_seeds, citers_per_seed)
    if not fresh:
        cached = cache.get(key)
        if cached is not None:
            return cached
    t0 = time.monotonic()
    deadline_at = t0 + (deadline_s if deadline_s is not None else _ASSEMBLE_DEADLINE_S)
    assemble = _s2_assemble if source == "s2" else _oa_assemble
    seed_ids, works, deadline_hit = assemble(query, seeds, n_seeds, citers_per_seed, deadline_at)
    seeds_requested = len(seeds) if seeds else n_seeds
    degraded = (_s2.breaker_open() if source == "s2" else oa.breaker_open())
    if not seed_ids:
        # Tell a THROTTLE-induced empty (S2 429'ing) apart from a genuine no-match, so the note is
        # actionable and _meta.degraded is honest even before the 5-consecutive breaker has opened.
        throttled = source == "s2" and _s2.recently_throttled()
        note = f"no seeds resolved (source={source})"
        if throttled:
            note = ("no seeds resolved: Semantic Scholar is rate-limiting (HTTP 429). Retry shortly, "
                    "or pass source=openalex for the same citation structure.")
        return {"seeds": [], "n_nodes": 0, "n_edges": 0, "edges": [], "nodes": [], "source": source,
                "note": note,
                "_meta": {"backend": source, "elapsed_s": round(time.monotonic() - t0, 1),
                          "seeds_requested": seeds_requested, "seeds_resolved": 0,
                          "nodes_capped": False, "degraded": bool(degraded or throttled),
                          "partial": True}}
    result = _build(seed_ids, works, max_nodes, query=query)
    result["source"] = source
    # FAIL-OPEN graph tap: the single exit point both backends share. Mints work/person/topic/venue
    # nodes + cites/authored/about/published_in edges from the assembled works dict through the
    # single-writer queue. Never raises, never changes the result (additive; the STABILITY contract).
    _graph_tap(source, works)
    nodes_capped = len(works) > max_nodes  # _build truncated the assembled corpus to max_nodes
    # The seed TITLES drive the whole map; when seeds were AUTO-picked from a query they can drift
    # off-field (a 'mechanistic interpretability' query once auto-seeded protein-LM SAE papers,
    # pulling the neighborhood into bioinformatics). Surface them prominently + a verify nudge, so an
    # agent that does NOT already know the field's canon can catch the drift instead of trusting a
    # confidently-wrong map. Only nudge when the seeds were auto-derived (query given, no explicit seeds).
    seed_titles = [n.get("title") for n in result.get("nodes", []) if n.get("is_seed")][:max(n_seeds, 8)]
    auto_seeded = bool(query and not seeds)
    result["_meta"] = {
        "backend": source,
        "elapsed_s": round(time.monotonic() - t0, 1),
        "seeds_requested": seeds_requested,
        "seeds_resolved": len(seed_ids),
        "seed_titles": seed_titles,
        "seed_note": ("auto-seeds chosen by relevance — VERIFY these titles match your field before "
                      "trusting the map; re-call with seeds=[...] to re-anchor if off") if auto_seeded else None,
        "nodes_capped": nodes_capped,
        "degraded": degraded,
        # deadline_hit: the overall wall-clock budget blew before every seed's edges were fetched, so
        # the map is time-truncated (a slow/throttling upstream). A transient FACT, not a quality call.
        "deadline_hit": deadline_hit,
        # partial iff the graph is provably incomplete: backend circuit was open this run (some
        # assemble calls silently degraded to None/[]), OR the node corpus was truncated to
        # max_nodes, OR fewer seeds resolved than requested, OR the deadline bailed it early.
        "partial": bool(degraded or nodes_capped or len(seed_ids) < seeds_requested or deadline_hit),
    }
    if deadline_hit:
        result["note"] = (f"partial map: the {round(deadline_at - t0)}s budget blew before all seeds' "
                          "edges were fetched (slow/throttling upstream). Retry shortly, raise "
                          "deadline_s, or pass source=openalex.")
    # Do NOT cache a TRANSIENT-partial map: time-truncated (deadline_hit) OR breaker-degraded (degraded:
    # some assemble calls silently fell to []/None while S2's circuit was open). Both are incomplete
    # because the UPSTREAM was unhealthy, not because the field is small, so a retry when it recovers
    # should build the FULL neighborhood, not get served this partial for 6h. The result is a truthy
    # dict, so the cache.set empty-FLOOR cannot catch it — this is the local guard for that truthy-
    # partial case, using the health signal _s2/oa already expose. nodes_capped / fewer-seeds are
    # LEGITIMATE partials (the map is as complete as the field allows) and DO cache.
    if not deadline_hit and not degraded:
        cache.set(key, result, ttl=6 * 3600)
    return result


def recommend(seeds: Optional[list[str]] = None, limit: int = 20, fresh: bool = False) -> dict:
    """Semantically-similar papers to the seed(s) via Semantic Scholar's recommendation model
    (SPECTER embeddings + co-citation) — discovery BEYOND keyword search + the citation graph,
    including recent work the graph hasn't caught up to. A THIN channel: we build no embeddings;
    S2 already did. The AGENT re-judges the flat list. (The eye-way of "semantic search": route
    to a source that already does it, don't reinvent Exa.)"""
    seeds = [s for s in (seeds or []) if s]
    if not seeds:
        return {"seeds": [], "n": 0, "papers": []}
    # A-class canonical key: normalize + sort seeds (positives; order is not meaningful) so every
    # id-form / ordering collapses to one row. Reuses _s2.norm_s2_id (recommend is s2-only).
    key = cache.make_key("cartographer", "recommend",
                         ",".join(sorted(_s2.norm_s2_id(s) for s in seeds)), limit)
    if not fresh:
        cached = cache.get(key)
        if cached is not None:
            return cached
    F = ["paperId", "title", "year", "publicationDate", "citationCount", "authors", "externalIds"]
    # _s2 wrappers normalize seeds + carry the breaker/semaphore + degrade to [] on failure.
    if len(seeds) == 1:
        raw = _s2.get_recommended_papers(seeds[0], fields=F, limit=limit)
    else:
        raw = _s2.get_recommended_papers_from_lists(positive_paper_ids=seeds, fields=F)

    papers = []
    for i, p in enumerate(raw or []):
        if i >= limit:  # get_recommended_papers returns a list, but cap defensively regardless
            break
        pid = getattr(p, "paperId", None)
        if not pid:
            continue
        ext = getattr(p, "externalIds", None) or {}
        doi, arx = ext.get("DOI"), ext.get("ArXiv")
        auths = [getattr(a, "name", None) for a in (getattr(p, "authors", None) or []) if getattr(a, "name", None)]
        papers.append({
            "id": pid,
            "title": (getattr(p, "title", None) or "(untitled)")[:200],
            "year": getattr(p, "year", None),
            "date": getattr(p, "publicationDate", None),
            "cited_by": getattr(p, "citationCount", None) or 0,
            "first_author": auths[0] if auths else None,
            # doi is the directly-passable handle for omniseek_paper_enrich; for an arXiv-only paper fall
            # back to the arXiv DOI form (same shape omniseek_field_skeleton emits) so the chain
            # recommend→enrich never needs the agent to string-parse the id out of url.
            "doi": (("https://doi.org/" + doi) if doi
                    else (("https://doi.org/10.48550/arXiv." + arx) if arx else None)),
            "url": (("https://doi.org/10.48550/arXiv." + arx) if arx
                    else (("https://doi.org/" + doi) if doi else f"https://www.semanticscholar.org/paper/{pid}")),
        })
    result = {"seeds": seeds, "n": len(papers), "papers": papers}
    # A silent n:0 is indistinguishable from 'genuinely no recs'. The common cause is the wrong
    # handle: an OpenAlex W-id (a paper's source_id from an openalex omniseek_search result) is NOT a
    # DOI/arXiv/S2 id, so S2's recommender returns empty. Name that, instead of a silent dead-end.
    _bad = [s for s in seeds if s.startswith("W") and s[1:].isdigit()]
    if _bad and not papers:
        result["_meta"] = {"diagnostic": (
            f"{_bad} look like OpenAlex work-ids — the paper tools do NOT accept them. For an "
            "openalex omniseek_search result pass metadata.paper_id (or metadata.doi), not source_id.")}
    # Truthy-partial guard (the empty-FLOOR can't see an empty list INSIDE a truthy dict): when there
    # are NO recs AND S2 was unreachable (breaker-open / throttled), the empty is a FAILURE artifact,
    # not a genuine 'no recs' — cache it briefly so it self-heals instead of pinning 6h. A non-empty
    # result, or a genuine empty from a HEALTHY S2, keeps the 6h TTL.
    if papers or not (_s2.breaker_open() or _s2.recently_throttled()):
        cache.set(key, result, ttl=6 * 3600)
    else:
        cache.set(key, result, ttl=cache.EMPTY_TTL_CAP)
    return result
