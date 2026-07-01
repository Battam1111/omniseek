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

from penumbra.core import _openalex as oa
from penumbra.core import _s2
from penumbra.core import cache

logger = logging.getLogger(__name__)

_TIMEOUT = 25
_SELECT = "id,doi,title,publication_year,publication_date,cited_by_count,referenced_works,concepts,authorships"
_S2_REF_CAP = 100  # hard cap on S2 references fetched per seed (page-bounded; refs are naturally small)
# OVERALL wall-clock budget for one assemble. Each S2/OA call is individually bounded (timeout +
# breaker), but the ~13 SERIAL calls of an assemble have no aggregate cap, so a slow/throttling
# upstream could grind for MINUTES before the 5-consecutive-failure breaker trips (the field_skeleton
# "hung for minutes" symptom). This caps the whole operation: the assemble loop checks it between
# calls and bails early with whatever it has (a partial map, flagged), never hanging the agent. The
# happy path (~12s) is well under it; an agent mapping a huge field can raise deadline_s.
_ASSEMBLE_DEADLINE_S = 25.0

# _norm_s2_id now lives in _s2.norm_s2_id (single source of truth, shared with relations).
# Kept as a module-level alias so the smoke gate + any external caller keep working.
_norm_s2_id = _s2.norm_s2_id


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


def _oa_fetch(ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 50):
        chunk = [c for c in ids[i:i + 50] if c]
        if not chunk:
            continue
        data = _get("/works", {"filter": "openalex_id:" + "|".join(chunk),
                               "per-page": 50, "select": _SELECT})
        for w in ((data or {}).get("results") or []):
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
def _build(seed_ids: list[str], works: dict, max_nodes: int) -> dict:
    seed_set = set(seed_ids)
    indeg: Counter = Counter()  # in-field in-degree (only where referenced_works are present)
    for w in works.values():
        for r in (w.get("referenced_works") or []):
            t = _wid(r)
            if t in works:
                indeg[t] += 1
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
    return {"seeds": seed_ids, "n_nodes": len(nodes), "n_edges": sum(indeg.values()),
            "nodes": nodes[:max_nodes]}


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
    key = cache.make_key("cartographer", "nbhd", source, query or "",
                         ",".join(seeds or []), n_seeds, citers_per_seed)
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
        return {"seeds": [], "n_nodes": 0, "n_edges": 0, "nodes": [], "source": source,
                "note": note,
                "_meta": {"backend": source, "elapsed_s": round(time.monotonic() - t0, 1),
                          "seeds_requested": seeds_requested, "seeds_resolved": 0,
                          "nodes_capped": False, "degraded": bool(degraded or throttled),
                          "partial": True}}
    result = _build(seed_ids, works, max_nodes)
    result["source"] = source
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
    # Do NOT cache a time-truncated map (the throttle is transient): a retry when the upstream is
    # healthy should be able to build the FULL neighborhood, not get served this partial for 6h.
    if not deadline_hit:
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
    key = cache.make_key("cartographer", "recommend", ",".join(seeds), limit)
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
            # doi is the directly-passable handle for eye_paper_enrich; for an arXiv-only paper fall
            # back to the arXiv DOI form (same shape eye_field_skeleton emits) so the chain
            # recommend→enrich never needs the agent to string-parse the id out of url.
            "doi": (("https://doi.org/" + doi) if doi
                    else (("https://doi.org/10.48550/arXiv." + arx) if arx else None)),
            "url": (("https://doi.org/10.48550/arXiv." + arx) if arx
                    else (("https://doi.org/" + doi) if doi else f"https://www.semanticscholar.org/paper/{pid}")),
        })
    result = {"seeds": seeds, "n": len(papers), "papers": papers}
    # A silent n:0 is indistinguishable from 'genuinely no recs'. The common cause is the wrong
    # handle: an OpenAlex W-id (a paper's source_id from an openalex eye_search result) is NOT a
    # DOI/arXiv/S2 id, so S2's recommender returns empty. Name that, instead of a silent dead-end.
    _bad = [s for s in seeds if s.startswith("W") and s[1:].isdigit()]
    if _bad and not papers:
        result["_meta"] = {"diagnostic": (
            f"{_bad} look like OpenAlex work-ids — the paper tools do NOT accept them. For an "
            "openalex eye_search result pass metadata.paper_id (or metadata.doi), not source_id.")}
    cache.set(key, result, ttl=6 * 3600)
    return result
