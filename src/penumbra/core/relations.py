"""Relationship reconstruction — typed, public-artifact-backed EDGE FACTS. No judgment.

A connection between two people is not a scalar; it is a typed, directed, weighted,
time-stamped, evidence-backed edge, and co-authorship is only ONE type. The eye
supplies the mechanical edge FACTS per layer; the AGENT overlays the layers and
decides what a connection MEANS (advisor vs peer, real tie vs list-mate). This
module is the channel; the agent is the network cartographer (same split as
cartographer.py for citation graphs).

Built from live probes against the real host (2026-06-10):
  • Identity is the cross-cutting hard problem: every layer starts with name -> WHICH
    person. Senior authors resolve cleanly in OpenAlex; juniors/arXiv-frontier are
    ambiguous or missing there and need Semantic Scholar. So resolution returns
    CANDIDATES ranked by a transparent signal, NEVER a silent guess (the Sea-AI-Lab
    disambiguation lesson, re-confirmed: "Zhennan Shen" -> 3 OpenAlex fragments, none
    at the right institution).
  • A brand-new arXiv paper is NOT in the graph yet, so connections are reconstructed
    from each author's PRIOR work, not the paper's own (yet-missing) edges.
  • The co-authorship neighborhood surfaces the advisor automatically by frequency
    (Yi R. Fung -> Heng Ji ~51x across two split ids).

THE THREE CLEAN-STRUCTURED LAYERS BUILT HERE (mechanical, keyless, read-only):
  resolve_identity   name -> ranked candidate ids (the shared front door for ALL layers)
  coauthors          academic collaboration: per-author neighborhood + pairwise joint-work
  institution_cohort organizational: who is at a lab/dept (optionally in a field, a window)

Fuzzy / behavioral layers (media co-mention, social proximity, business ties,
advisor-as-meaning, academic siblings, code collaboration) are deliberately NOT
primitives — there the "edge" is a judgment, so the agent assembles them from a
dossier + existing eye tools (field_skeleton, github, bluesky, exa, cdp_fulltext,
penumbra_add_url). See the capability doc.

Read-only over public scholarly data (OpenAlex / Semantic Scholar), the same
standard bibliometric substrate the rest of the eye already maps (field_skeleton,
researcher_watch, csrankings, org_watch).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from penumbra.core import _openalex as oa
from penumbra.core import _s2
from penumbra.core import cache

logger = logging.getLogger(__name__)

_AUTHOR_SELECT = "id,display_name,works_count,cited_by_count,last_known_institutions,affiliations"
_WORKS_SELECT = "id,title,publication_year,authorships"
_MAX_WORKS = 200  # OpenAlex per-page hard max; one author's neighborhood in one call

# Cache TTLs (the same disk cache + idiom the sibling cartographer.py uses for identically-shaped
# graph work). Relations re-fetches OpenAlex/S2 LIVE against the shared metered key on every call —
# coauthors alone pulls up to _MAX_WORKS works PER author — so an un-cached repeat burns the budget
# for an answer that did not move. Caching keys are (source, id) for works and the resolved query
# tuple for the lookups, so a repeated identical call hits disk, not the wire.
_WORKS_TTL = 6 * 3600     # per-author work lists: the heavy fetch; a person's recent papers land slowly
_RESOLVE_TTL = 3600       # name -> id map: stable, but short so a fresh ingestion is picked up within the hour
_COHORT_TTL = 6 * 3600    # institution roster: an org's publishing cohort shifts slowly

from penumbra.core.recall import graph  # noqa: E402 — the graph write verb + mint registry

# Vocabulary this tap MINTS (vocabulary-by-minting, design section 3): declared on the tap itself,
# registered at import, folded into ``graph.declared_vocabulary`` as the computed union; the smoke
# tripwire bounds ACTUAL graph data to that union. relations owns the PERSON layer the cartographer
# deferred (P3 taps table row): person/institution nodes, and the coauthored / affiliated / same_as
# edges from the three clean-structured layers. THE MINT RULE (design "Mint the product"): a tap
# mints what its tool RETURNS, never its internal fetch material — coauthors pulls up to 200 works
# per author as COUNTING material and mints none of it; the returned top_coauthors / bridges /
# pairwise edges (the product) are what mint. same_as here is the A-tier name_match candidate from
# resolve_identity's likely_same_person groups (the agent ratifies via penumbra_ruling); institution
# affiliation is api:openalex; the align:name_match method is declared here.
GRAPH_MINTS = {
    "kinds": ["person", "institution"],
    "edge_types": ["coauthored", "affiliated", "same_as"],
    "methods": ["api:openalex", "api:s2", "align:name_match"],
}
graph.register_mints("relations", kinds=GRAPH_MINTS["kinds"],
                     edge_types=GRAPH_MINTS["edge_types"], methods=GRAPH_MINTS["methods"])


# ── name matching (the disambiguation gate) ──────────────────────────────────
# OpenAlex /authors?search is FUZZY: "Zhennan Shen" returns a prolific "Zhi-Qiang
# Shen" near-match. Picking the top by works_count then silently resolves to the
# WRONG PERSON (the Sea-AI-Lab disaster). So every candidate carries name_match,
# and resolution NEVER auto-picks a non-matching name — it surfaces it, flagged.
def _name_tokens(name: str) -> set[str]:
    """Lowercase alpha tokens of length >= 2 (drops initials/punctuation)."""
    return {t for t in re.split(r"[^a-z]+", (name or "").lower()) if len(t) >= 2}


def _name_matches(query: str, candidate: str) -> bool:
    """True iff every significant query token appears in the candidate name (set
    subset, order-independent). '{wenjie,li} <= {maggie,wenjie,li}' accepts the
    advisor 'Maggie Wenjie Li'; '{zhennan,shen}' rejects 'Zhi-Qiang Shen'."""
    q = _name_tokens(query)
    return bool(q) and q <= _name_tokens(candidate)


# ── identity resolution (the shared front door) ──────────────────────────────
def _oa_candidates(name: str, limit: int) -> list[dict]:
    data = oa.get_json("/authors", {"search": name, "per-page": max(limit, 10),
                                    "select": _AUTHOR_SELECT})
    out = []
    for a in (data.get("results") or []):
        insts = [(i.get("display_name") or "") for i in (a.get("last_known_institutions") or [])]
        # OpenAlex over-collects institutions on merged-garbage profiles; cap the noise.
        inst = "; ".join(x for x in insts if x)[:120] or None
        dn = a.get("display_name")
        out.append({
            "id": (a.get("id") or "").rsplit("/", 1)[-1],
            "source": "openalex",
            "name": dn,
            "works_count": a.get("works_count") or 0,
            "cited_by": a.get("cited_by_count") or 0,
            "institution": inst,
            "name_match": _name_matches(name, dn or ""),
        })
    return out


def _s2_candidates(name: str, limit: int) -> list[dict]:
    out = []
    for a in _s2.search_author(name, limit):  # bounded + breaker + degrade-to-[] inside _s2
        dn = getattr(a, "name", None)
        out.append({
            "id": getattr(a, "authorId", None),
            "source": "s2",
            "name": dn,
            "works_count": getattr(a, "paperCount", None) or 0,
            "cited_by": getattr(a, "citationCount", None) or 0,
            "institution": "; ".join(getattr(a, "affiliations", None) or []) or None,
            "name_match": _name_matches(name, dn or ""),
        })
    return out


def _rank(cands: list[dict], hint: str) -> list[dict]:
    """Order: NAME-MATCHING candidates first (a non-matching name is never a real
    resolution), then within each block a hint-institution match floats up, then
    works_count. Mechanical, transparent."""
    h = (hint or "").strip().lower()
    def key(c):
        nm = 1 if c.get("name_match") else 0
        hit = 1 if (h and h in (c.get("institution") or "").lower()) else 0
        return (nm, hit, c.get("works_count") or 0)
    return sorted(cands, key=key, reverse=True)


def _resolve_by_paper(name: str, paper: str) -> list[dict]:
    """Disambiguate by a KNOWN paper: return the author(s) matching ``name`` in the author
    list of ``paper`` (an arXiv id, a DOI, or a title searched on S2). The reliable way to
    pin a common-name junior — a distinctive paper fixes the exact id where a bare name
    search cannot. Mechanical: the paper's author list is a fact, not a guess."""
    s = (paper or "").strip()
    if not s:
        return []
    # _s2.get_paper normalizes a bare arXiv id / DOI internally (single source of truth); a
    # title goes through search. Both wrappers carry the breaker/semaphore + degrade to None/[].
    if re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", s) or s.lower().startswith("10.") or "doi.org/" in s.lower():
        p = _s2.get_paper(s)
    else:
        best = None
        for hit in _s2.search_paper(s, limit=3):
            best = best or hit
            if s.lower()[:24] in (getattr(hit, "title", "") or "").lower():
                best = hit
                break
        p = best
    out = []
    for a in (getattr(p, "authors", None) or []):
        dn = getattr(a, "name", None)
        if getattr(a, "authorId", None) and _name_matches(name, dn or ""):
            out.append({"id": a.authorId, "source": "s2", "name": dn,
                        "works_count": getattr(a, "paperCount", None) or 0,
                        "cited_by": getattr(a, "citationCount", None) or 0,
                        "institution": None, "name_match": True,
                        "via_paper": (getattr(p, "title", "") or s)[:60]})
    return out


def _id_merge_token(ids: list[str]) -> str:
    """The '+'-joined merge token coauthors/_resolve_one accept for ONE split person ('A1+A2')."""
    return "+".join(ids)


def _likely_same_person(candidates: list[dict]) -> list[dict]:
    """Group MATCHED candidates that share a name-token key AND the same backend into likely-split
    fragments of ONE person, emitting the ready-to-paste '+'-merge token. Purely mechanical (name
    tokens + source, already on each candidate); NEVER auto-merges, it surfaces the merge the agent
    would otherwise hand-build. OpenAlex 'A…' ids and S2 numeric ids don't '+'-merge across backends
    (the coauthors contract), so the group key includes source. Affiliation may refine but is not
    the floor; name_key + source keeps it judgment-free."""
    groups: dict[tuple, list[dict]] = {}
    for c in candidates:
        if not c.get("name_match") or not c.get("id"):
            continue
        key = (frozenset(_name_tokens(c.get("name") or "")), c.get("source"))
        if not key[0]:
            continue
        groups.setdefault(key, []).append(c)
    out = []
    for (_, src), members in groups.items():
        if len(members) < 2:
            continue
        ids = [m["id"] for m in members]
        out.append({
            "source": src,
            "ids": ids,
            "name": members[0].get("name"),
            "merge_token": _id_merge_token(ids),
            "note": "same name, same backend → consider +-merging into one penumbra_coauthors input",
        })
    return out


# ── graph write tap (design section 6 + P3 taps row): mint the PRODUCT the three layers return ──
# THE MINT RULE (design "Mint the product, not the intermediate"): a tap mints what its tool RETURNS,
# never its internal fetch material. coauthors pulls up to _MAX_WORKS works PER author purely as
# counting material for the ranking — none of that mints; the returned top_coauthors / bridges /
# pairwise edges (the product) do. resolve candidates' institution STRINGS mint nothing ("no external
# id, no node"); cohort's id-bearing institution does. The three builders below are PURE (take the
# tool's OUT dict, return (nodes, edges) in the writer's dict shapes) so the smoke can golden-test
# them with zero network; _tap wraps enqueue_graph fail-open (a tap failure must NEVER break the
# read the agent gets). person node ids namespace by the candidate/result's own backend
# (person:openalex:A… / person:s2:…); institution is inst:openalex:I….

def _person_nid(source: Optional[str], pid: Optional[str]) -> Optional[str]:
    """``person:{backend}:{native_id}`` — None when either half is missing (a person with no id
    mints no node, the S2-display-name-only lesson generalized). ``source`` is the candidate's own
    backend field (``openalex`` / ``s2``)."""
    src = (source or "").strip()
    pid = (pid or "").strip()
    if not src or not pid:
        return None
    return f"person:{src}:{pid}"


def _resolve_mints(out: dict) -> tuple[list[dict], list[dict]]:
    """From a resolve_identity RESULT dict: person nodes for every returned candidate carrying an id
    (they are real persons OpenAlex/S2 retrieved), plus A-tier same_as candidate edges for every PAIR
    within each ``likely_same_person`` group (one real person split across same-backend ids; the agent
    ratifies via penumbra_ruling). Institution STRINGS on candidates mint nothing (no id). Pure."""
    nodes: list[dict] = []
    edges: list[dict] = []
    for c in (out.get("candidates") or []):
        if not isinstance(c, dict):
            continue
        nid = _person_nid(c.get("source"), c.get("id"))
        if not nid:
            continue
        # attrs carry the two numeric facts the candidate returned (as returned — 0 is a real value
        # the tool emits; the reader distinguishes 0 from absent). Institution string is deliberately
        # NOT an attr edge target (no id → the mint rule forbids a node), but is harmless as prose.
        nodes.append({"id": nid, "kind": "person", "label": c.get("name") or None,
                      "attrs": {"works_count": c.get("works_count"), "cited_by": c.get("cited_by")}})
    # same_as A candidates: ALL PAIRS inside each likely_same_person group (2-4 same-backend ids of
    # ONE person). The writer normalizes symmetric src < dst, so do NOT pre-sort here.
    for group in (out.get("likely_same_person") or []):
        if not isinstance(group, dict):
            continue
        gsrc = group.get("source")
        gids = [i for i in (group.get("ids") or []) if i]
        for i in range(len(gids)):
            for j in range(i + 1, len(gids)):
                a_nid = _person_nid(gsrc, gids[i])
                b_nid = _person_nid(gsrc, gids[j])
                if a_nid and b_nid and a_nid != b_nid:
                    edges.append({"src": a_nid, "dst": b_nid, "type": "same_as",
                                  "tier": "A", "method": "align:name_match"})
    return nodes, edges


def _coauthors_mints(out: dict) -> tuple[list[dict], list[dict]]:
    """From a coauthors RESULT dict: person nodes for every RESOLVED input (one node per id in its
    ``ids``), every ``top_coauthors`` entry, and every ``bridges`` entry; coauthored M-edges input ->
    top_coauthor (joint/papers attrs), input -> input per pairwise ``edges`` entry (joint_count attr,
    endpoints = each input's PRIMARY id ids[0]), and input -> bridge (total_joint attr) for each input
    in the bridge's ``shared_by``. The per-author works pool + ``cooc`` (name-collapsed, no stable
    ids) mint NOTHING; unresolved inputs mint nothing. Pure. Namespace by ``out['source']``."""
    source = (out.get("source") or "").strip()
    ns = source or "openalex"
    method = f"api:{ns}"
    nodes: list[dict] = []
    edges: list[dict] = []
    # Map each input node's query string -> its PRIMARY resolved id (ids[0]); the pairwise edges and
    # bridge.shared_by reference inputs by their query string, so this resolves them back to a node id.
    query_to_primary: dict[str, str] = {}
    input_nodes = out.get("nodes") or []
    for n in input_nodes:
        if not isinstance(n, dict):
            continue
        resolved = n.get("resolved")
        if not isinstance(resolved, dict):
            continue  # unresolved input -> mints nothing
        rname = resolved.get("name")
        ids = [i for i in (resolved.get("ids") or ([resolved.get("id")] if resolved.get("id") else [])) if i]
        if not ids:
            continue
        # a node per id in the input's id set (split ids of ONE person), label = the resolved name.
        for pid in ids:
            nid = _person_nid(ns, pid)
            if nid:
                nodes.append({"id": nid, "kind": "person", "label": rname or None, "attrs": None})
        primary = _person_nid(ns, ids[0])
        if primary and n.get("query"):
            query_to_primary[str(n.get("query"))] = primary
        # coauthored: input's PRIMARY id -> each top_coauthor (their own id + name).
        if primary:
            for tc in (n.get("top_coauthors") or []):
                if not isinstance(tc, dict):
                    continue
                co_nid = _person_nid(ns, tc.get("id"))
                if not co_nid or co_nid == primary:
                    continue
                nodes.append({"id": co_nid, "kind": "person", "label": tc.get("name") or None,
                              "attrs": None})
                edges.append({"src": primary, "dst": co_nid, "type": "coauthored", "tier": "M",
                              "method": method,
                              "attrs": {"joint": tc.get("joint"), "papers": tc.get("papers")}})
    # input -> input pairwise edges (endpoints resolved from the query strings to primary ids).
    for e in (out.get("edges") or []):
        if not isinstance(e, dict):
            continue
        a_nid = query_to_primary.get(str(e.get("a")))
        b_nid = query_to_primary.get(str(e.get("b")))
        if a_nid and b_nid and a_nid != b_nid:
            edges.append({"src": a_nid, "dst": b_nid, "type": "coauthored", "tier": "M",
                          "method": method, "attrs": {"joint_count": e.get("joint_count")}})
    # input -> bridge for every input the bridge is shared_by (bridge is an external person with an id).
    for b in (out.get("bridges") or []):
        if not isinstance(b, dict):
            continue
        br_nid = _person_nid(ns, b.get("id"))
        if not br_nid:
            continue
        nodes.append({"id": br_nid, "kind": "person", "label": b.get("name") or None, "attrs": None})
        for q in (b.get("shared_by") or []):
            in_nid = query_to_primary.get(str(q))
            if in_nid and in_nid != br_nid:
                edges.append({"src": in_nid, "dst": br_nid, "type": "coauthored", "tier": "M",
                              "method": method, "attrs": {"total_joint": b.get("total_joint")}})
    return nodes, edges


def _cohort_mints(out: dict) -> tuple[list[dict], list[dict]]:
    """From an institution_cohort RESULT dict: the institution node (``inst:openalex:I…``, when
    ``out['institution']`` is real), a person node per ``people`` entry, and affiliated M-edges
    person -> institution (attrs works + concept, the concept key only when the filter had one).
    The no-match miss dict (institution None) mints nothing. Pure. Cohort people ids are OpenAlex
    author ids -> ``person:openalex:…``; institutions are OpenAlex -> api:openalex."""
    inst = out.get("institution")
    if not isinstance(inst, dict) or not inst.get("id"):
        return [], []
    inst_nid = f"inst:openalex:{inst['id']}"
    concept_id = ((out.get("filters") or {}).get("concept_id"))
    nodes: list[dict] = [{"id": inst_nid, "kind": "institution",
                          "label": inst.get("name") or None, "attrs": None}]
    edges: list[dict] = []
    for p in (out.get("people") or []):
        if not isinstance(p, dict):
            continue
        p_nid = _person_nid("openalex", p.get("id"))
        if not p_nid:
            continue
        nodes.append({"id": p_nid, "kind": "person", "label": p.get("name") or None, "attrs": None})
        attrs: dict = {"works": p.get("works_at_institution_in_field")}
        if concept_id:
            attrs["concept"] = concept_id
        edges.append({"src": p_nid, "dst": inst_nid, "type": "affiliated", "tier": "M",
                      "method": "api:openalex", "attrs": attrs})
    return nodes, edges


def _tap(builder, out: dict) -> None:
    """FAIL-OPEN wrapper (the cartographer idiom): run one PURE builder on the tool's result, enqueue
    the (nodes, edges) through the single-writer queue. Never raises (a tap failure must NEVER break
    the read the agent gets); NO-OP when writes are disabled (cron) or the batch is empty. Import the
    writer INSIDE the try so an import hiccup degrades to a swallow, never a broken relations call."""
    try:
        nodes, edges = builder(out)
        if not nodes and not edges:
            return
        from penumbra.core.recall import writer
        writer.enqueue_graph(nodes, edges)
    except Exception as exc:  # noqa: BLE001 — a tap failure must NEVER break a relations call
        logger.debug("relations graph tap swallowed: %s", exc)


def resolve_identity(name: str, hint: str = "", source: str = "auto", paper: str = "",
                     limit: int = 5, fresh: bool = False) -> dict:
    """name -> ranked candidate author ids. NEVER silently picks, and NEVER promotes a
    name that does not actually match the query: returns CANDIDATES, name-matches first.

    source: 'auto' (OpenAlex first; also query S2 when OpenAlex has no strong NAME-MATCH,
    i.e. a likely junior OpenAlex hasn't indexed), 'openalex', or 's2'. hint (e.g. an
    institution) only re-orders candidates, never filters them out.

    paper: a KNOWN paper of this person (arXiv id / DOI / title). The reliable disambiguator
    for a common-name junior — resolves straight from the paper's author list; when it hits,
    the paper-anchored id(s) are returned FIRST as high-confidence.

    fresh=True bypasses the cache (the same fresh idiom cartographer/field_skeleton uses).
    """
    name = (name or "").strip()
    if not name:
        return {"query": name, "candidates": [], "ambiguous": False, "note": "empty name"}
    # Cache the name -> ranked-candidate map: it is stable (an author's id does not move) and the same
    # name resolves repeatedly across a session (resolve_identity is the shared front door EVERY layer
    # calls). limit is in the key so a wider request is a distinct entry, never a truncated hit. Short
    # TTL (~1h) so a brand-new ingestion is still picked up promptly. A DEGRADED lookup is NOT cached
    # (see below): caching a transport failure would freeze a recoverable 429 for the whole TTL.
    key = cache.make_key("relations", "resolve", name, hint, source, paper, limit)
    if not fresh:
        cached = cache.get(key)
        if cached is not None:
            _tap(_resolve_mints, cached)   # a cache hit is still a real resolution → honest re-mint
            return cached
    anchored = _resolve_by_paper(name, paper) if paper else []
    cands: list[dict] = []
    oa_failed: Optional[str] = None
    if source in ("auto", "openalex"):
        try:
            cands += _oa_candidates(name, limit)
        except Exception as exc:  # noqa: BLE001
            # A TRANSPORT failure (429 / breaker-open / network) means we have NO OpenAlex data — it
            # is NOT evidence the person is absent. Record it so the verdict below never reports a
            # degraded lookup as a confident "not in the graph". That conflation is the false
            # "no NAME-MATCH" for famous authors: the API key removes the COMMON trigger (credit-
            # exhaustion 429), but the breaker still trips on any upstream hiccup, so distinguishing
            # "lookup failed" from "genuinely absent" is the actual fix, not a swallow.
            oa_failed = str(exc) or exc.__class__.__name__
            logger.warning("relations openalex resolve failed for %r: %s", name, exc)
    best_match = max((c["works_count"] for c in cands if c.get("name_match")), default=0)
    if source == "s2" or (source == "auto" and best_match < 8) or anchored:
        cands += _s2_candidates(name, limit)
    cands = _rank(cands, hint)
    matched = [c for c in cands if c.get("name_match")]
    if anchored:
        aid = {a["id"] for a in anchored}
        rest = [c for c in (matched + [c for c in cands if not c.get("name_match")])
                if c.get("id") not in aid]
        anchored_out = {"query": name, "hint": hint or None, "source": source,
                        "candidates": anchored + rest[: max(limit, 5)], "ambiguous": len(anchored) > 1,
                        "note": "resolved via paper anchor"
                                + (" (multiple same-name authors on the paper)"
                                   if len(anchored) > 1 else "")}
        # Cache the paper-anchored resolution only when the OpenAlex half did NOT fail: the anchor
        # itself is a fact from the paper's author list, but a failed OpenAlex call means the `rest`
        # tail is blind, so do not freeze a partially-degraded view for the whole TTL.
        if not oa_failed:
            cache.set(key, anchored_out, ttl=_RESOLVE_TTL)
        _tap(_resolve_mints, anchored_out)   # mint the returned candidates + any same_as group
        return anchored_out
    strong = [c for c in matched if c["works_count"] >= 2]
    ambiguous = len(strong) >= 2 and strong[1]["works_count"] * 2 >= strong[0]["works_count"]
    note = ""
    if not matched:
        if oa_failed:
            # Honest degraded verdict: we could NOT query OpenAlex, so "no match" is unconfirmed.
            # Never let a 429 / breaker masquerade as "person not in the graph".
            note = ("lookup DEGRADED — the OpenAlex query failed (" + oa_failed + "); "
                    + ("S2 found no match either" if source == "auto"
                       else "S2 was not queried for source='openalex'")
                    + ". This is NOT a confirmed 'not in graph' — retry shortly, or pass "
                      "source='s2' / paper= / an explicit id.")
        else:
            note = ("no candidate's NAME matches the query (only fuzzy near-misses) — this person "
                    "is likely not yet in the graph; pass paper= / an id, or verify before trusting")
    elif ambiguous:
        note = "multiple same-name candidates — disambiguate with paper= / a hint / a known co-author"
    returned = (matched + [c for c in cands if not c.get("name_match")])[: max(limit, 5)]
    out = {"query": name, "hint": hint or None, "source": source,
           "candidates": returned, "ambiguous": ambiguous, "note": note}
    if oa_failed:
        # Surface the degradation even when S2 DID match: the OpenAlex half was blind, so an agent
        # reads absent OpenAlex candidates/counts as missing-data, not as signal.
        out["degraded"] = {"openalex": oa_failed}
    # Additive merge hint: when one real person is SPLIT across ≥2 same-name same-backend ids,
    # surface the ready '+'-merge token so the agent need not reconstruct it by hand. Never auto-picks.
    same = _likely_same_person(returned)
    if same:
        out["likely_same_person"] = same
    # Cache the resolution UNLESS it was degraded: a 429 / breaker-open lookup is recoverable, and
    # freezing it for the whole TTL would turn a transient outage into a stale "no match" all hour.
    if not oa_failed:
        cache.set(key, out, ttl=_RESOLVE_TTL)
    _tap(_resolve_mints, out)   # mint person nodes + likely_same_person same_as candidates (fail-open)
    return out


# ── co-authorship layer ──────────────────────────────────────────────────────
def _looks_like_id(s: str) -> Optional[str]:
    s = (s or "").strip()
    if s[:1] in ("A",) and s[1:].isdigit():
        return "openalex"
    if s.isdigit() and len(s) >= 6:
        return "s2"
    return None


def _oa_author_works(author_id: str, fresh: bool = False) -> list[dict]:
    # Cache the heavy per-author OpenAlex pull (up to _MAX_WORKS works) keyed by the author id, so a
    # repeated penumbra_coauthors over the same people reads disk instead of re-spending the shared key. The
    # normalized work shape below is what gets cached, identical on a hit. (mirrors researcher_watch's
    # _fetch_pi_works + cartographer's fresh-bypass idiom.)
    key = cache.make_key("relations", "works", "openalex", author_id)
    if not fresh:
        cached = cache.get(key)
        if cached is not None:
            return cached
    data = oa.get_json("/works", {"filter": f"author.id:{author_id}",
                                  "sort": "publication_date:desc",
                                  "per-page": _MAX_WORKS, "select": _WORKS_SELECT})
    works = []
    for w in (data.get("results") or []):
        ids, names = [], {}
        for a in (w.get("authorships") or []):
            au = a.get("author") or {}
            aid = (au.get("id") or "").rsplit("/", 1)[-1]
            if aid:
                ids.append(aid)
                names[aid] = au.get("display_name")
        works.append({"id": (w.get("id") or "").rsplit("/", 1)[-1],
                      "title": w.get("title") or "(untitled)",
                      "year": w.get("publication_year"),
                      "author_ids": ids, "author_names": names})
    cache.set(key, works, ttl=_WORKS_TTL)
    return works


def _s2_author_works(author_id: str, cap: int = _MAX_WORKS, fresh: bool = False) -> list[dict]:
    """S2 author's papers as the SAME normalized shape. S2 consolidates a junior's
    profile far better than OpenAlex (which splits arXiv-recent papers across ids), so
    this is the right backend for junior / arXiv-frontier co-authorship edges. Uses the
    eye's S2 API key (not the rate-limited keyless tier)."""
    # Same cache as the OpenAlex path: a repeated S2 co-authorship pull reads disk, not the metered S2
    # key. The cap is part of the key so a wider re-request (more works) is a distinct entry, never a
    # silently-truncated hit. (mirrors researcher_watch's _fetch_pi_works + cartographer's fresh bypass.)
    key = cache.make_key("relations", "works", "s2", author_id, cap)
    if not fresh:
        cached = cache.get(key)
        if cached is not None:
            return cached
    works = []
    # _s2.get_author_papers is hard-bounded (page size = cap, stop at cap) + breaker + degrade-to-[].
    for p in _s2.get_author_papers(author_id, cap, fields=["title", "year", "authors"]):
        ids, names = [], {}
        for a in (getattr(p, "authors", None) or []):
            aid = getattr(a, "authorId", None)
            if aid:
                ids.append(aid)
                names[aid] = getattr(a, "name", None)
        works.append({"id": getattr(p, "paperId", None),
                      "title": getattr(p, "title", None) or "(untitled)",
                      "year": getattr(p, "year", None),
                      "author_ids": ids, "author_names": names})
    cache.set(key, works, ttl=_WORKS_TTL)
    return works


def _name_key(name: str) -> str:
    """Order-independent identity key for collapsing an author's SPLIT ids: same display
    name = same key. Used only for EXTERNAL coauthors within a single graph (where two
    distinct people sharing a full name is rare); input authors keep explicit id sets."""
    return " ".join(sorted(_name_tokens(name)))


def _resolve_one(token: str, hint: str, source: str, paper: str = "", fresh: bool = False) -> dict:
    """Resolve one input token for the graph. A token may be:
      • a NAME (-> top NAME-MATCHING candidate; paper= pins a common-name junior),
      • a single id, or
      • '+'-joined ids ("id1+id2") = ONE person SPLIT across ids, to be merged.
    Never picks a non-matching name: if none matches, resolved=None + near-misses surfaced."""
    token = (token or "").strip()
    parts = [p.strip() for p in token.split("+")] if "+" in token else [token]
    if parts and all(_looks_like_id(p) for p in parts):
        ns = _looks_like_id(parts[0])
        return {"query": token, "resolved": {"id": parts[0], "ids": parts, "source": ns, "name": None},
                "ambiguous": False, "alternatives": []}
    r = resolve_identity(token, hint=hint, source=source, paper=paper, fresh=fresh)
    want = "s2" if (source == "s2" or paper) else "openalex"
    matched = [c for c in r["candidates"] if c.get("name_match") and c["source"] == want]
    if not matched and paper:
        matched = [c for c in r["candidates"] if c.get("name_match")]
    if not matched:
        degraded = r.get("degraded")
        out = {"query": token, "resolved": None, "ambiguous": r["ambiguous"],
               "alternatives": r["candidates"][:3],
               # Propagate the honest degraded verdict: a failed lookup is NOT a confirmed absence,
               # so it must never come back as a flat "no NAME-MATCH".
               "note": (r.get("note") if degraded
                        else "no NAME-MATCH — try paper=, source='s2', a hint, or an explicit id")}
        if degraded:
            out["degraded"] = degraded
        return out
    top = matched[0]
    return {"query": token, "resolved": {**top, "ids": [top["id"]]},
            "ambiguous": r["ambiguous"], "alternatives": matched[1:4]}


def coauthors(authors: list[str], source: str = "openalex", hints: Optional[list[str]] = None,
              papers: Optional[list[str]] = None, per_author_works: int = 200,
              fresh: bool = False) -> dict:
    """Co-authorship layer. Pass names and/or ids (single source per call — OpenAlex 'A…'
    ids and S2 numeric ids do not mix in one graph).

    Each input may be a NAME, an id, or '+'-joined ids ("id1+id2") for ONE person SPLIT
    across ids — their works are merged (OpenAlex/S2 routinely split a junior's papers).
    N=1 -> that author's coauthor neighborhood, name-collapsed + frequency-ranked (advisor
    + close collaborators surface by count). N>1 -> additionally the pairwise prior
    joint-work edges (with the joint paper titles as evidence) + bridge collaborators
    (people co-authoring with >=2 inputs, not in the set). Plus ``cooc``: which of the
    network's top external coauthors co-appear on the same papers — the SUB-COMMUNITY
    structure (an ego's distinct 'research worlds'). Mechanical throughout; the agent reads
    what the clusters MEAN.

    source: 'openalex' (established authors, clean ids) or 's2' (consolidates JUNIOR /
    arXiv-frontier profiles OpenAlex splits + lags). ``hints`` / ``papers`` are parallel
    lists for per-author disambiguation (an institution hint, or a known paper that pins a
    common-name junior). Unresolved authors come back resolved:null + near-misses + a note.

    fresh=True bypasses the cache for both the per-author work pulls and the identity
    resolutions (the same fresh idiom cartographer/field_skeleton uses).
    """
    authors = [a for a in (authors or []) if a and a.strip()]
    if not authors:
        return {"source": source, "nodes": [], "edges": [], "bridges": [], "cooc": []}
    hints = hints or []
    papers = papers or []

    nodes = [_resolve_one(a, hint=(hints[i] if i < len(hints) else ""), source=source,
                          paper=(papers[i] if i < len(papers) else ""), fresh=fresh)
             for i, a in enumerate(authors)]

    # fetch each resolved author's works, MERGING across their split ids (deduped)
    def fetch(n):
        rid = n.get("resolved")
        if not rid:
            return []
        ids = rid.get("ids") or [rid.get("id")]
        merged: dict = {}
        for aid in ids:
            if not aid:
                continue
            try:
                # fresh is threaded EXPLICITLY (not via cache._fresh_var): these run inside the
                # ThreadPoolExecutor below, and the fresh contextvar does not propagate into raw
                # worker threads (researcher_watch copy_context()s for the same reason).
                ws = (_s2_author_works(aid, per_author_works, fresh=fresh) if rid.get("source") == "s2"
                      else _oa_author_works(aid, fresh=fresh)[:per_author_works])
            except Exception as exc:  # noqa: BLE001
                logger.warning("coauthors fetch failed for %s: %s", aid, exc)
                continue
            for w in ws:
                merged[w.get("id") or _name_key(w["title"])] = w
        return list(merged.values())

    with ThreadPoolExecutor(max_workers=min(len(nodes), 8)) as ex:
        works_by_node = list(ex.map(fetch, nodes))

    node_ids = [set((n.get("resolved") or {}).get("ids")
                    or ([n["resolved"]["id"]] if n.get("resolved") else [])) for n in nodes]
    input_names = {_name_key(n["query"]) for n in nodes}
    input_names |= {_name_key((n.get("resolved") or {}).get("name") or "") for n in nodes}
    input_names.discard("")

    # per-node neighborhood, keyed by NAME (collapse split author ids; count = papers shared).
    # rep_idc keeps a representative id per name so the agent can DRILL a coauthor (harvest an
    # id straight from the neighborhood, then penumbra_coauthors([id])) without re-resolving a
    # common name — the 'anchor + harvest' technique the handbook documents.
    rep: dict[str, str] = {}
    rep_idc: dict[str, Counter] = {}
    neigh: list[Counter] = []          # FRACTIONAL credit (1/n_authors): the ranking signal
    for n, works, own in zip(nodes, works_by_node, node_ids):
        own_names = {_name_key(n["query"]), _name_key((n.get("resolved") or {}).get("name") or "")}
        # Backfill the ego's name when the input was a BARE ID (resolve_identity returns name=None
        # for an id, line 387): the ego is an author on every one of its own works, so its display
        # name is already in author_names; harvest it here at zero extra API cost.
        resolved = n.get("resolved")
        if resolved is not None and not resolved.get("name"):
            for w in works:
                hit = next((w["author_names"].get(aid) for aid in w["author_ids"] if aid in own
                            and w["author_names"].get(aid)), None)
                if hit:
                    resolved["name"] = hit
                    break
        c: Counter = Counter()         # 1/n_authors-weighted: a 22-author survey is worth ~0.045 here
        cp: Counter = Counter()        # raw paper overlap, for the surfaced 'papers' field
        for w in works:
            # Fractional authorship credit: one paper's total collaboration weight is 1, split across
            # its authors, so 50 genuine 3-author papers (~16.7) outrank one 22-author survey (~0.045)
            # instead of a big-list paper minting a whole high-count clique in a single shot.
            weight = 1.0 / max(len(w["author_ids"]), 1)
            seen = set()
            for aid in w["author_ids"]:
                k = _name_key(w["author_names"].get(aid) or "")
                if not k or aid in own or k in own_names or k in seen:
                    continue
                seen.add(k)
                c[k] += weight
                cp[k] += 1
                rep[k] = w["author_names"].get(aid)
                rep_idc.setdefault(k, Counter())[aid] += 1
        n["works_seen"] = len(works)
        # joint = 1/n-weighted collaboration strength (the RANK); papers = raw count of shared works.
        # joint = 1/n-author-weighted collaboration strength (the RANK); papers = raw count of
        # shared works. The two together let the reader SEE a big-list-paper artifact (high papers,
        # low joint) and judge it, so the eye reports the facts and does not editorialize.
        n["top_coauthors"] = [{"id": rep_idc[k].most_common(1)[0][0], "name": rep[k], "joint": round(ct, 2), "papers": cp[k]}
                              for k, ct in c.most_common(12)]
        neigh.append(c)

    # pairwise edges between input authors (by id sets — any of B's ids in A's work)
    edges = []
    for i in range(len(nodes)):
        wi = works_by_node[i]
        if not wi or not node_ids[i]:
            continue
        for j in range(i + 1, len(nodes)):
            if not node_ids[j]:
                continue
            joint = [w for w in wi if node_ids[j] & set(w["author_ids"])]
            if joint:
                edges.append({"a": nodes[i]["query"], "b": nodes[j]["query"],
                              "joint_count": len(joint),
                              "papers": [{"title": w["title"], "year": w["year"], "id": w["id"]}
                                         for w in joint[:8]]})

    # bridges: external coauthor NAMES shared by >= 2 input authors (split ids collapsed)
    bridge_in: dict[str, list[int]] = {}
    for idx, c in enumerate(neigh):
        for k in c:
            if k not in input_names:
                bridge_in.setdefault(k, []).append(idx)
    bridges = sorted(
        ({"id": rep_idc[k].most_common(1)[0][0], "name": rep[k],
          "shared_by": [nodes[i]["query"] for i in idxs],
          "total_joint": round(sum(neigh[i][k] for i in idxs), 2)}
         for k, idxs in bridge_in.items() if len(idxs) >= 2),
        key=lambda x: (len(x["shared_by"]), x["total_joint"]), reverse=True)[:15]

    # cooc: which of the network's TOP external coauthors co-appear on the same papers —
    # the sub-community structure (an ego's distinct 'worlds'). Mechanical co-occurrence.
    totals: Counter = Counter()
    for c in neigh:
        totals.update(c)
    topset = {k for k, _ in totals.most_common(14)}
    allworks: dict = {}
    for works in works_by_node:
        for w in (works or []):
            allworks[w.get("id") or _name_key(w["title"])] = w
    cooc: Counter = Counter()
    for w in allworks.values():
        present = sorted({_name_key(w["author_names"].get(aid) or "") for aid in w["author_ids"]}
                         & topset)
        for i in range(len(present)):
            for j in range(i + 1, len(present)):
                cooc[(present[i], present[j])] += 1
    cooc_out = sorted(({"a": rep.get(a, a), "b": rep.get(b, b), "n": ct}
                       for (a, b), ct in cooc.items() if ct >= 2),
                      key=lambda x: -x["n"])[:30]

    out = {"source": source, "n_authors": len(nodes),
           "nodes": nodes, "edges": edges, "bridges": bridges, "cooc": cooc_out}
    # If any input author's resolution was DEGRADED (an OpenAlex transport failure, not a real
    # absence), surface it at the top level so a thin / empty graph reads as missing-data, not "no ties".
    degraded = {n["query"]: n["degraded"] for n in nodes if n.get("degraded")}
    if degraded:
        out["degraded"] = degraded
    # FAIL-OPEN graph tap: mint the PRODUCT (resolved persons + top_coauthors + bridges + pairwise
    # coauthored edges); the per-author works pool + cooc mint nothing (the mint-the-product rule).
    _tap(_coauthors_mints, out)
    return out


# ── organizational layer: institution cohort ─────────────────────────────────
def institution_cohort(institution: str, concept: str = "", year_from: Optional[int] = None,
                       limit: int = 40, fresh: bool = False) -> dict:
    """Who actively publishes at an institution (a lab/dept/university), optionally within a
    FIELD and since a year. The organizational network — orthogonal to co-authorship
    ('same lab, never co-authored' is still a tie, and a lab's roster is the SG/Canada
    cohort question). Counts a person by their works AT this institution IN this field
    (so juniors with a few papers surface, unlike a total-output sort).

    WITHOUT a concept this is every field at the institution (very broad). Pass
    concept='natural language processing' / 'machine learning' / 'reinforcement learning'
    to scope to a cohort. ``year_from`` (e.g. 2022) biases toward the CURRENT cohort. The
    roster is a starting point the agent drills (penumbra_coauthors / homepages), not a verified
    lab-member list — OpenAlex has no 'PhD student' flag.

    fresh=True bypasses the cache (the same fresh idiom cartographer/field_skeleton uses).
    """
    # Cache the roster keyed by the full query (institution + concept + window + size): an org's
    # publishing cohort shifts slowly, so a repeated identical cohort call reads disk instead of
    # re-running the institution + concept + group_by works queries against the shared metered key.
    # A transport failure raises out of oa.get_json BEFORE any cache.set, so a degraded lookup is
    # never frozen (same boundary as cartographer: only a real result is cached).
    key = cache.make_key("relations", "cohort", institution, concept, year_from or "", limit)
    if not fresh:
        cached = cache.get(key)
        if cached is not None:
            _tap(_cohort_mints, cached)   # a cache hit is a real cohort → honest re-mint (a miss mints nothing)
            return cached
    inst = oa.get_json("/institutions", {"search": institution, "per-page": 1})
    res = inst.get("results") or []
    if not res:
        miss = {"institution": None, "people": [], "note": f"no institution matched {institution!r}"}
        cache.set(key, miss, ttl=_COHORT_TTL)
        return miss
    iid = (res[0].get("id") or "").rsplit("/", 1)[-1]
    iname = res[0].get("display_name")

    filt = [f"institutions.id:{iid}"]
    cid = None
    if concept:
        cj = oa.get_json("/concepts", {"search": concept, "per-page": 1})
        cres = cj.get("results") or []
        if cres:
            cid = (cres[0].get("id") or "").rsplit("/", 1)[-1]
            filt.append(f"concepts.id:{cid}")
    if year_from:
        filt.append(f"from_publication_date:{int(year_from)}-01-01")
    # group works by author -> "who publishes at this institution in this field" (per-page
    # caps the number of GROUPS returned, hence the cohort size).
    data = oa.get_json("/works", {"filter": ",".join(filt),
                                  "group_by": "authorships.author.id",
                                  "per-page": min(limit, 100)})
    people = [{"id": (g.get("key") or "").rsplit("/", 1)[-1],
               "name": g.get("key_display_name"),
               "works_at_institution_in_field": g.get("count")}
              for g in (data.get("group_by") or []) if g.get("key_display_name")]
    out = {"institution": {"id": iid, "name": iname},
           "filters": {"concept": concept or None, "concept_id": cid, "year_from": year_from},
           "n": len(people), "people": people,
           "note": ("" if concept else
                    "no field filter — all fields at this institution; pass concept= to "
                    "scope to a cohort")}
    cache.set(key, out, ttl=_COHORT_TTL)
    _tap(_cohort_mints, out)   # mint the institution + people nodes + affiliated edges (fail-open)
    return out
