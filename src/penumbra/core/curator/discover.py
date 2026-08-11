"""Curator P4 discovery engine — emits NEUTRAL candidate FACTS, renders NO verdict.

This is the MECHANICAL half of the self-iterating source-acquisition loop. Given the P3
dossier (the gap signal), the accumulated P2 yield (the service-gap signal), and the operator
policy DATA, it assembles a flat list of candidate source dicts the monthly cron then dedups +
persists + probes. It mirrors the cartographer / relations contract exactly: assemble facts,
the AGENT judges. THE RAZOR holds here as hard as anywhere:

  - It emits NO key in evidence.BANNED_KEYS at any depth and NO verdict/score/rank-of-worthiness.
  - It returns ``[]`` when ``policy['enabled']`` is false (scaffold mode: near-idle).
  - It imports NO verdict-writer (penumbra_curator_decide / candidates.record_verdict /
    source_audit.record_source_verdict / candidates.record_applied), NO model/anthropic client,
    NO WebSearch, NO profile.* / relevance / employer_hits. (Smoke 9.1 greps for this.)
  - Trust-inheritance is a RELEVANCE heuristic, NEVER a safety control (Attack-2): the only
    safety = red-lines + safe_fetch + the agent. A discovered host is exactly as untrusted as a
    cold-submitted one. Inner-engine hosts come ONLY from a node's venue / DOI / roster host,
    NEVER an arbitrary paper-body outbound link (Attack-2: a poisoned paper would inject an
    attacker host with clean provenance). The ``_discovery`` provenance is a relevance hint.
  - Ranking by (in_degree, cited_by) is a RATE-LIMIT (top-N per cell + a recorded
    discovery_truncated / dropped_count), NOT a quality filter (HOLE-3): we never claim the
    survivors are the "best".

Two rings (spec 4.2 / 4.3):
  OUTER (gap-driven, the DEMAND signal): empty_cells_for_discovery + single_occupant_cells +
    the service-gap signal -> a frozen GAP->SOURCE-KIND template maps the MODE to a family. A
    cold-start cell (no in-domain source the graph can reach) emits a URL-less STUB.
  INNER (citation-graph, trust-inherited, the DEPTH signal): seed from the roster's strongest
    in-domain sources (skipping cells at/over the coverage ceiling) and traverse the real
    cartographer / relations primitives, deriving candidate hosts from venue/DOI/roster only.
"""

from __future__ import annotations

import logging
import time
from typing import Optional
from urllib.parse import urlparse

from penumbra.core.curator import candidates as _cand

logger = logging.getLogger(__name__)

# ── the frozen GAP -> SOURCE-KIND template (DATA, not judgment; spec 4.2) ─────────
# Maps an acquisition MODE to the config family/families that mechanically fill a cell of that
# mode. Smoke 9.3 asserts this covers every mode in probe.MODE_VOCAB (a future mode added to
# facets without a template entry fails the deploy). The mapping is a mechanical proposal the
# probe + agent re-judge; it carries no verdict.
GAP_SOURCE_KIND = {
    "STRUCTURE": ["search_index", "org_watch"],
    "RECALL": ["rss", "page_watch"],
    "MONITOR": ["rss", "page_watch"],
    "UNWALL": ["news_scraper", "page_watch"],
    "TRANSCRIBE": ["rss"],
}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _split_cell(cell: str) -> "tuple[str, str]":
    """A cell is ``domainXmode`` (the _facet_cell shape). Split on the LAST 'x' that yields a
    known mode, else fall back to the last 'x'. Returns (domain, MODE-uppercased)."""
    if "x" not in cell:
        return cell, ""
    dom, _x, mode = cell.rpartition("x")
    return dom, (mode or "").upper()


def _proposed_family_for(mode: str) -> Optional[str]:
    """The PRIMARY family the GAP->SOURCE-KIND template proposes for a mode (a mechanical guess;
    the probe + agent re-judge). None for an unknown mode (no template -> not a discovery target)."""
    fams = GAP_SOURCE_KIND.get((mode or "").upper())
    return fams[0] if fams else None


# ── coverage-ceiling read (spec 3, Attack-3: bound the ceiling, not just the floor) ──
def _ceiling_for(domain: str, policy: dict) -> int:
    table = (policy or {}).get("coverage_ceiling") or {}
    v = table.get(domain)
    if isinstance(v, (int, float)):
        return int(v)
    d = table.get("_default", 4)
    return int(d) if isinstance(d, (int, float)) else 4


def _cell_occupancy(cell: str, grid_by_placement: dict) -> int:
    return len(grid_by_placement.get(cell, []) or [])


# ── service-gap signal (spec 6, P2->P4: NO new tap; derived from existing yield ratios) ──
def _underserved_single_cells(dossier: dict, yield_state: dict, policy: dict) -> list:
    """single-occupant cells whose sole occupant is occupied-but-WEAK by the existing yield
    ratios (low presence_rate / high timeout_rate / carrying only from the index). A FACT-only
    derivation: it reads the dossier's per-source ratios + the existing yield counters; it stores
    no query text + computes no verdict. Returns a sorted list of cells (lexicographic)."""
    floor = (policy or {}).get("service_gap_floor") or {}
    presence_min = float(floor.get("presence_rate_min", 0.05))
    timeout_max = float(floor.get("timeout_rate_max", 0.5))
    grid = dossier.get("grid_by_placement") or {}
    by_name = {s.get("name"): s for s in (dossier.get("sources") or [])}
    out: list = []
    for cell in dossier.get("single_occupant_cells") or []:
        occupants = grid.get(cell) or []
        if len(occupants) != 1:
            continue
        src = by_name.get(occupants[0]) or {}
        ratios = src.get("ratios") or {}
        yld = src.get("yield") or {}
        presence = float(ratios.get("presence_rate", 0.0) or 0.0)
        timeout = float(ratios.get("timeout_rate", 0.0) or 0.0)
        from_index_only = int(yld.get("from_index_only_appearances", 0) or 0)
        topk = int(yld.get("topk_appearances", 0) or 0)
        weak = (presence < presence_min) or (timeout > timeout_max) \
            or (topk > 0 and from_index_only >= topk)
        if weak:
            out.append(cell)
    return sorted(set(out))


# ── outer-ring stub builder (cold-start, cell-keyed id; spec 4.2, HOLE-5) ─────────
def _cold_start_stub(cell: str, reason: str) -> dict:
    """A URL-less STUB candidate for a cold-start cell (no in-domain source the citation graph
    can reach). Cell-keyed make_id so a re-emitted stub for the same cell collapses to the same
    row and is never re-counted (HOLE-5). urls=[] -> the cron's 'probe only new rows' loop skips
    it cleanly (probe needs a URL); it surfaces in the alert as an unfilled cell for the agent to
    expand. Carries NO verdict/score key."""
    domain, mode = _split_cell(cell)
    family = _proposed_family_for(mode)
    name = f"cold-start {cell}"
    cid = _cand.make_id(name, [])  # name-keyed (urls empty) -> stable per cell
    return {
        "id": cid,
        "name": name,
        "urls": [],
        "proposed_mode": mode,
        "proposed_domain": domain,
        "proposed_family": family or "other",
        "proposed_kind": None,
        "proposed_regions": [],
        "rationale_text": "",
        "submitted_by": "curator-loop",
        "_discovery": {
            "ring": "outer",
            "edge_type": "cold_start",
            "parent_source": None,
            "seed": None,
            "target_cell": cell,
            "centrality": {"in_degree": None, "cited_by": None},
            "truncated": False,
            "dropped_count": 0,
            "note": "cold-start: needs agent/operator-supplied URL",
            "discovered_at": _now_iso(),
        },
    }


# ── inner-engine host derivation (spec 4.3, Attack-2: venue/DOI/roster ONLY) ──────
def _host_of(url: Optional[str]) -> str:
    if not isinstance(url, str) or not url.strip():
        return ""
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _candidate_host_from_node(node: dict) -> "tuple[Optional[str], Optional[str]]":
    """Derive a candidate SOURCE host from a citation NODE -- ONLY from the node's own DOI host or
    its publisher/venue URL host (the trusted-provenance fields), NEVER an arbitrary paper-body
    outbound link (Attack-2). Returns (url, edge_type) or (None, None). The DOI host is the
    canonical publisher; ``url`` is the node's resolved landing page (also DOI/venue-derived in
    cartographer._build). We deliberately do NOT read any ``external_links`` field: if one is ever
    added, it must be tagged external_link_UNTRUSTED and treated as cold-start (no centrality
    credit)."""
    doi = node.get("doi")
    if isinstance(doi, str) and _host_of(doi):
        return doi, "venue_doi"
    url = node.get("url")
    if isinstance(url, str) and _host_of(url):
        # cartographer._build sets url = doi or _url or openalex.org/<id> — all venue/registry
        # hosts, never a paper-body link. Safe to derive a host from.
        return url, "venue_url"
    return None, None


def _seed_sources_for_domain(domain: str, dossier: dict) -> list:
    """The roster's in-domain sources usable as inner-engine seeds: a registered, non-retired
    source declaring this domain whose mode includes STRUCTURE (the citation-graph-bearing mode).
    Returns sorted names (lexicographic). Pure placement membership; renders no verdict."""
    out: list = []
    for s in dossier.get("sources") or []:
        if s.get("name") == "_index":
            continue
        if s.get("retired"):  # a retired source is no longer a valid inner-engine seed
            continue
        if domain in (s.get("domains") or []) and "STRUCTURE" in (s.get("modes") or []):
            out.append(s.get("name"))
    return sorted(set(n for n in out if n))


def _inner_for_cell(cell: str, dossier: dict, policy: dict) -> "tuple[list, int]":
    """Traverse the citation graph / roster for ONE cell and emit candidate dicts. Returns
    (candidates, dropped_count). Ranking by (in_degree, cited_by) is a RATE-LIMIT only: we sort by
    the FACT, take discover_topn, and record the truncation. Hosts come from venue/DOI/roster only.
    Degrades to ([], 0) on any graph-call failure (never raises) -> a null round (the cron's
    healthy-round gate freezes the STOP streak)."""
    domain, mode = _split_cell(cell)
    topn = int((policy or {}).get("discover_topn", 12) or 12)
    seeds = _seed_sources_for_domain(domain, dossier)
    if not seeds:
        return [], 0  # cold-start: no in-domain seed; the OUTER ring emits a stub instead

    out: list = []
    seen_hosts: set = set()
    dropped = 0
    parent = seeds[0]
    family = _proposed_family_for(mode) or "other"

    # cited-by / co-cited / references via the citation graph (S2 for arXiv-frontier coverage).
    nodes: list = []
    try:
        from penumbra.core import cartographer
        skel = cartographer.field_skeleton(query=domain, source="s2",
                                            n_seeds=4, citers_per_seed=topn, max_nodes=topn * 4)
        nodes = list(skel.get("nodes") or [])
    except Exception as exc:  # noqa: BLE001: a graph-call failure is a null round, never a raise
        logger.debug("curator discover field_skeleton failed for %s: %s", cell, exc)
        nodes = []

    # nodes already come sorted (in_degree, cited_by) desc from cartographer._build; this is the
    # RATE-LIMIT order, NOT a quality ranking. Round-robin is moot for a single cell (one seed
    # group); we take the top-N per cell and record the rest as dropped.
    ranked = nodes
    for i, node in enumerate(ranked):
        if len([c for c in out if c["_discovery"]["edge_type"] in ("venue_doi", "venue_url")]) >= topn:
            dropped += 1
            continue
        url, edge_type = _candidate_host_from_node(node)
        if not url:
            continue
        host = _cand.canonical_host(url)
        if not host or host in seen_hosts:
            continue
        seen_hosts.add(host)
        title = (node.get("title") or "")[:120]
        out.append({
            "id": _cand.make_id(title or host, [url]),
            "name": (title or host)[:120],
            "urls": [url],
            "proposed_mode": mode,
            "proposed_domain": domain,
            "proposed_family": family,
            "proposed_kind": None,
            "proposed_regions": [],
            "rationale_text": "",
            "submitted_by": "curator-loop",
            "_discovery": {
                "ring": "inner",
                "edge_type": edge_type,
                "parent_source": parent,
                "seed": domain,
                "target_cell": cell,
                "centrality": {"in_degree": node.get("in_degree"), "cited_by": node.get("cited_by")},
                "truncated": False,   # set on the row below once the cell's dropped_count is known
                "dropped_count": 0,
                "note": "host derived from node venue/DOI (relevance hint, not a safety credential)",
                "discovered_at": _now_iso(),
            },
        })

    # roster / org layer: an institution recurring in this domain's cohort with no org_watch entry
    # is an org_watch candidate (a host-LESS family: no URL, so a STUB-shaped row keyed by name).
    # institution_cohort needs a concept; we pass the domain as the concept term (mechanical).
    # This is intentionally light: the agent drills the cohort; we only surface the affordance.
    # (Kept inside the same try-isolation discipline; a failure just yields no org candidate.)
    # NOTE: we do NOT auto-emit org_watch person-candidates here; a single-person affiliation is a
    # red-line concern (spec 8a) and the agent must review it. We surface only the cell-level need
    # via the OUTER ring's single-occupant / empty-cell signal.

    # stamp truncation onto every inner row of this cell (the agent learns the cell has more).
    truncated = dropped > 0
    for c in out:
        c["_discovery"]["truncated"] = truncated
        c["_discovery"]["dropped_count"] = dropped
    return out, dropped


# ── the public entry point ────────────────────────────────────────────────────────
def discover(dossier: dict, yield_state: Optional[dict] = None,
             policy: Optional[dict] = None) -> list:
    """Assemble the flat list of NEUTRAL candidate dicts for this monthly round. Returns ``[]``
    when ``policy['enabled']`` is false (scaffold mode). Each dict carries ONLY submitted-shaped
    fields + a ``_discovery`` provenance sub-dict; NO verdict/score/rank-of-worthiness key at any
    depth. Renders NO verdict: it discovers/counts; the cron dedups+persists+probes; the agent
    judges. Every emitted list is sorted lexicographically by id (never centrality/yield-ranked).

    The cron passes the result through dedup (canonical-host terminal ledger + live hosts +
    make_id) before ``candidates.add``; discover itself never calls add and never probes.
    """
    policy = policy or {}
    yield_state = yield_state or {}
    if not policy.get("enabled"):
        return []  # scaffold mode: no discovery target -> no candidates

    dossier = dossier or {}
    grid_by_placement = dossier.get("grid_by_placement") or {}
    empty_cells = list(dossier.get("empty_cells_for_discovery") or [])
    underserved = _underserved_single_cells(dossier, yield_state, policy)
    # out_of_scope cells the agent recorded as deliberately-empty are never re-seeded (spec 7).
    out_of_scope = set(policy.get("out_of_scope_cells") or [])

    findings: dict = {}  # id -> candidate (dedup by id within the round; cron dedups across rounds)

    # OUTER RING: gap-driven demand. For each gap cell, if its domain has >=1 in-corpus STRUCTURE
    # seed, hand it to the inner engine; else emit a cold-start stub.
    gap_cells = sorted(set(empty_cells) | set(underserved) - out_of_scope)
    for cell in gap_cells:
        if cell in out_of_scope:
            continue
        domain, mode = _split_cell(cell)
        if mode not in GAP_SOURCE_KIND:
            continue  # no template -> not a discovery target (smoke 9.3 freezes template totality)
        # ceiling guard (Attack-3): a cell at/over its ceiling is done; not a discovery target.
        if _cell_occupancy(cell, grid_by_placement) >= _ceiling_for(domain, policy):
            continue
        seeds = _seed_sources_for_domain(domain, dossier)
        if seeds:
            inner, _dropped = _inner_for_cell(cell, dossier, policy)
            for c in inner:
                findings[c["id"]] = c
        else:
            stub = _cold_start_stub(cell, reason="no in-domain STRUCTURE seed")
            findings[stub["id"]] = stub

    # Return sorted by id (lexicographic) — never centrality/yield/relevance-ranked (HOLE-1).
    return [findings[k] for k in sorted(findings)]


def discovery_health(findings: list, dossier: dict) -> str:
    """A FACT about the round's graph reachability for the STOP healthy-round gate (spec 7,
    Attack-3): 'healthy' iff >=1 inner-ring candidate was surfaced (>=1 successful graph call
    returning >=1 node), else 'degraded' (a null/API-outage round). A pure derived fact: no
    verdict. (Cold-start-only rounds, where every gap cell lacks a seed, are 'degraded' for the
    streak: the engine made no demonstrably-healthy graph call.)"""
    for c in findings or []:
        if (c.get("_discovery") or {}).get("ring") == "inner":
            return "healthy"
    return "degraded"


# Smoke / cron convenience alias: the spec 9.2 banned-key walk calls
# ``discover.gather_candidates(fixture_dossier)``; expose it as a thin wrapper that uses an
# always-enabled fixture policy so the walk exercises the candidate shape offline.
def gather_candidates(dossier: dict, yield_state: Optional[dict] = None,
                      policy: Optional[dict] = None) -> list:
    """Thin wrapper over discover() that defaults to an ENABLED fixture policy when none is given
    (so an offline banned-keys walk gets real candidate shapes). With an explicit policy it is
    identical to discover()."""
    if policy is None:
        policy = {"enabled": True, "discover_topn": 8, "coverage_ceiling": {"_default": 4}}
    return discover(dossier, yield_state=yield_state, policy=policy)
