"""The unified graph — recall's RELATION index ("one memory, N indexes").

recall is the eye's perception memory over ONE substrate (the docs table). It already carries a
TEXT index (FTS5) and a VECTOR index (the vec matrix); this module adds the third arm: a RELATION
index answering "what connects to X" where the other two answer "find docs like / about X". Two
indexes over one substrate, joined on (source, source_id). Design of record:
``docs/design/graph-unified-model.md`` (v2.0).

THE RAZOR holds structurally, not by convention: the graph stores mechanical FACTS (tier M) and
labeled ALIGNMENT CANDIDATES (tier A) — NEVER verdicts. Tier J (agent judgment: claims, gaps,
identity rulings) is excluded by a SQL CHECK on graph_edges (``store``), so it cannot physically
enter the eye's store. Identity is an EDGE (same_as with tier + method), never a destructive merge;
query-time collapse POLICIES (named method-sets, not numeric thresholds) choose how much to trust.
Views project; the agent judges. No inference rules live here.

Everything is fail-OPEN: every read degrades to empty on any failure, so a graph bug NEVER breaks
search or recall (it shares recall.db, hence recall's blast radius and recall's mitigation). The
doc-doc same_as edge set is DERIVED (docs.fp self-join + doc_json external_ids), never stored: the
wall is born pre-populated by construction, everything recall ever indexed is already in the views.
"""

from __future__ import annotations

import inspect
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TypedDict

from penumbra.core import relevance
from penumbra.core.recall import store

logger = logging.getLogger(__name__)

# Identity rulings live beside sensors.json (the same precedent: agent JUDGMENT persisted as
# declarative STATE the eye executes mechanically). The eye APPLIES rulings; it never MAKES one — the
# agent hands one in via penumbra_ruling(action=create), which calls save_ruling below. The read half
# (load_rulings) stays fail-open; the WRITE half is serialized under _RULINGS_LOCK + atomic
# tmp/replace (the SensorStore idiom), so a concurrent create/delete never corrupts the file.
RULINGS_PATH = Path.home() / ".penumbra" / "state" / "graph_rulings.json"
_RULINGS_LOCK = threading.Lock()
_RULING_VERDICTS = frozenset({"same", "not_same"})

# Typed agent STATEMENTS generalize the rulings idiom (P8): where a ruling is a pair-keyed, symmetric
# IDENTITY judgment (consumed by the collapse machinery), a statement is a DIRECTED triple
# (src, dst, type) with FREE agent vocabulary — "acquired_by", "advises", "refutes", anything the
# judge names. Same store shape as rulings (a JSON list beside sensors.json / graph_rulings.json,
# serialized under _STATEMENTS_LOCK + atomic tmp/replace, fail-open load), same rule that the eye
# never MAKES one — the agent hands it in via penumbra_statement(action=create), and views PROJECT it at
# read time under the working/exploratory policies (never conservative). The directed triple is the
# KEY: re-stating replaces (declarative state, not a log; git history is the audit trail), and the
# direction is the agent's assertion, NEVER normalized (unlike a ruling's sorted pair). Two types are
# REFUSED — same_as / not_same_as — so identity keeps exactly ONE judgment source (penumbra_ruling).
STATEMENTS_PATH = Path.home() / ".penumbra" / "state" / "graph_statements.json"
_STATEMENTS_LOCK = threading.Lock()
_STATEMENT_MAX_TYPE_LEN = 40
_STATEMENT_REFUSED_TYPES = frozenset({"same_as", "not_same_as"})


# ── J-tier overlay (absorbs evidence.py under the unified names; design section 9) ──────────────
# Pure TypedDicts, zero logic. The agent's session overlay uses this SAME vocabulary with tier='J';
# the eye NEVER constructs a J node/edge (that is the agent's act). evidence.py's Document/Claim/Gap
# /EvidenceEdge/ManifestEntry retire INTO these shapes: same fields, one name.


class GraphNode(TypedDict, total=False):
    """A node in the unified graph. The per-kind optional fields are the union of what evidence.py
    carried, absorbed under one name (design section 9). Mechanical kinds (document/work/person/...)
    are created from eye output; J kinds (claim/gap) are built by the agent, never invented here."""
    id: str                          # canonical node id (design section 3)
    kind: str                        # document|work|person|institution|venue|topic|source|sensor|investigation|claim|gap
    label: Optional[str]             # display name / title
    # claim fields (evidence.py ClaimNode)
    statement: str                   # natural language assertion
    confidence: str                  # HIGH / MED / LOW / VERY_LOW / UNKNOWN
    scope: str                       # under what conditions this holds
    source_count: int                # independent sources supporting this
    as_of: Optional[str]             # date of the evidence
    # gap fields (evidence.py GapNode)
    description: str
    dimension: str                   # which perspective / aspect is absent
    severity: str                    # critical / important / minor
    suggested_queries: list[str]
    # document fields (evidence.py DocumentNode)
    source: str
    source_id: str
    url: str
    title: str
    date: Optional[str]
    freshness_class: Optional[str]
    relevance_hook: Optional[str]
    handles: Optional[dict]          # transcribable / captioned / enrichable / has_comments


class GraphEdge(TypedDict, total=False):
    """A directed relationship between two nodes. type = semantics (cites, same_as, supports...);
    tier = epistemics (M mechanical / A alignment / J judgment). Stored edges are M/A only (the
    CHECK); J edges exist only in the agent's session overlay."""
    src: str                         # source node id
    dst: str                         # target node id
    type: str                        # cites | authored | same_as | sourced_from | supports | ...
    tier: str                        # M | A | J
    method: str                      # api:openalex | id_eq:doi | align:title_fp | ...
    weight: Optional[float]          # 0.0 to 1.0, strength (evidence.py EvidenceEdge.weight)
    note: Optional[str]              # why this relationship exists


class ManifestEntry(TypedDict, total=False):
    """One tool call in the investigation provenance trail (lifted as-is from evidence.py)."""
    tool: str
    query: str
    source_name: Optional[str]
    status: str                      # ok | empty | timed_out | errored | excluded
    doc_count: int
    elapsed_s: float


# ── Vocabulary-by-minting (design section 3, "Vocabulary-by-minting") ────────────────────────────
# The FIRST exercise of the rule that replaces the old central kind/edge-type enum + the grant
# ceremony + the gap-ledger-as-debt. NOTHING mechanical depends on a central enum: kind and type are
# TEXT columns, views filter by string. A kind/edge-type is not a central decision — it is a property
# of the MINTING act (OpenAlex mints person/work ids; HF would mint model ids with lineage edges).
# So each WRITE TAP declares, on itself, the kinds + edge types + methods it mints (the same pattern
# as adapters declaring facets as class attrs); the GLOBAL vocabulary is the COMPUTED UNION of those
# declarations (mechanism demoted to data). The ONLY governance gate is the one that already exists:
# tap admission (shipping the tap IS the grant). A smoke tripwire then bounds ACTUAL data: the
# kinds/edge-types PRESENT in graph_nodes / graph_edges must be a subset of ``declared_vocabulary``
# (no silent vocabulary). J-tier vocabulary is the AGENT'S — fixed by the GraphNode/GraphEdge schema
# and versioned with it, NEVER tap-minted — so it lives in those TypedDicts above, not here.

_MINT_REGISTRY: dict[str, dict] = {}


def register_mints(tap: str, kinds: list[str], edge_types: list[str], methods: list[str]) -> None:
    """Declare, on a WRITE TAP, the node kinds / edge types / methods it mints. Idempotent UPSERT
    keyed by ``tap`` name: re-registering the same tap (e.g. a module re-imported in a test, or a
    declaration edited) REPLACES that tap's entry rather than accreting duplicates. Called at each
    tap module's import time (``cartographer``, ``enrich``, ``thin_memory``, ... register from day
    one). This is data, not ceremony: the union of all registered taps IS the graph's vocabulary."""
    _MINT_REGISTRY[tap] = {
        "kinds": list(kinds or []),
        "edge_types": list(edge_types or []),
        "methods": list(methods or []),
    }


def declared_vocabulary() -> dict:
    """The COMPUTED UNION of every registered tap's declaration: ``{"kinds": set, "edge_types": set,
    "methods": set}``. This replaces any central enum — there is no canonical list to lag reality,
    only the sum of what the shipped taps actually mint. The smoke tripwire asserts the vocabulary
    ACTUALLY present in graph_nodes / graph_edges is a SUBSET of this union (a tap writing an
    undeclared kind/type is the bug the tripwire catches). Empty until the first tap registers."""
    kinds: set = set()
    edge_types: set = set()
    methods: set = set()
    for decl in _MINT_REGISTRY.values():
        kinds.update(decl.get("kinds") or [])
        edge_types.update(decl.get("edge_types") or [])
        methods.update(decl.get("methods") or [])
    return {"kinds": kinds, "edge_types": edge_types, "methods": methods}


# ── Node-id helpers (design section 3) ──────────────────────────────────────────────────────────

def doc_node_id(source: str, source_id: str) -> str:
    """The canonical document node id. A document is a retrieval artifact (has content, lives in
    recall); its node id namespaces the source so ``doc:openalex:W123`` (a search RESULT) stays
    distinct from the world entity ``work:openalex:W123``, joined only by a minted same_as."""
    return f"doc:{source}:{source_id}"


def canon_label(label: str) -> str:
    """The ONE shared canonicalizer for ``topic:label:{norm}`` / ``inst:label:{norm}`` ids: casefold
    + strip, routed through the existing relevance tokenizer so ``topic:label:GRPO`` and
    ``topic:label:grpo`` collapse to the same node (else duplicate nodes accrete silently). Tokens
    are space-joined so multi-word labels ("mila quebec") normalize deterministically; falls back to
    the bare casefold+strip when the tokenizer yields nothing (e.g. punctuation-only)."""
    toks = relevance.tokenize(label or "")
    if toks:
        return " ".join(toks)
    return (label or "").strip().casefold()


def _id_self_label(node_id: str) -> Optional[str]:
    """The SELF-DESCRIBING label carried IN a ``{kind}:label:{x}`` id (label = x), else None. These
    label-keyed ids (``inst:label:{norm}``, ``topic:label:{norm}``, ...) are the idiom for entities the
    wall has never minted a structured row for — the id IS the display name. Used by ``find`` (a
    statement endpoint matches on its self-described label) and ``_hydrate_nodes`` (a label-keyed id
    with no graph_nodes row reads its label straight out of the id). A non-label id (``work:openalex:W1``,
    ``doc:arxiv:2401``) has no self-description here -> None (find substring-matches its raw id instead)."""
    parts = node_id.split(":", 2)
    if len(parts) == 3 and parts[1] == "label" and parts[2]:
        return parts[2]
    return None


# ── Collapse policies as METHOD-SETS (design section 2) ─────────────────────────────────────────
# Policies are NAMED METHOD-SETS, not numeric thresholds: the METHOD is the honest epistemic unit
# (a hand-picked 0.8 constant is pseudo-precision), exactly as recall's RRF fuses by rank only.
# ``conservative`` trusts only exact-ID equality; ``working`` adds the agent's rulings; ``exploratory``
# adds the fuzzy alignment candidates.
#
# THE LADDER, in one line (P8, now that the working tier carries BOTH judgment channels):
#   conservative = the mechanical world (M facts + exact-id same_as);
#   working      = + the agent's OWN judgments (rulings AND statements);
#   exploratory  = + machine candidates (title_fp / name_match alignment).
# The method-sets below gate IDENTITY (same_as) collapse only. Statements are NOT a same_as method
# and so are absent from these frozensets: they are DIRECTED, typed FACTS the agent asserts, appended
# in ``_policy_hop_edges`` on the SAME ``use_rulings`` gate the rulings overlay rides (working /
# exploratory), never gated by the collapse method-set. conservative NEVER shows a statement (a pure
# mechanical world); a statement is a judgment, and the working tier is where "add MY OWN judgments"
# begins.

CONSERVATIVE: frozenset = frozenset({
    "id_eq:doi", "id_eq:arxiv", "id_eq:openalex", "id_eq:orcid",
})
WORKING: frozenset = CONSERVATIVE | {"ruling"}
EXPLORATORY: frozenset = WORKING | {"align:title_fp", "align:name_match"}

_POLICIES: dict[str, frozenset] = {
    "conservative": CONSERVATIVE,
    "working": WORKING,
    "exploratory": EXPLORATORY,
}


def _policy_methods(policy: str) -> frozenset:
    """The method-set for a policy name; unknown names degrade to the safe ``conservative`` set."""
    return _POLICIES.get((policy or "").strip().lower(), CONSERVATIVE)


# ── View registry (design P6: the open family gets an open ABI) ──────────────────────────────────
# penumbra_graph is the eye's ONE open-family verb: its intents (views) grow with the model and their
# parameters are DISJOINT per view, so it gets an open ABI ``(view, args)``, frozen forever, with the
# views as a REGISTRY (the same mechanism-demoted-to-data move as _GATHER_TOOLS / register_mints).
# Each view function's OWN python signature is its per-view contract; the dispatcher, the valid-view
# list, per-view argument validation, and the runtime self-description all DERIVE from the registry
# via ``inspect`` (adding a view is dropping a decorated function, nothing else to touch).

_VIEWS: dict[str, object] = {}


def graph_view(fn):
    """Register a view function under its own name (so the registry IS the valid-view list). The
    function's signature is the per-view arg contract; its docstring's first line is its blurb."""
    _VIEWS[fn.__name__] = fn
    return fn


# ── Rulings store (the one J exception; sensors.json precedent) ─────────────────────────────────

def load_rulings() -> list[dict]:
    """Read the agent's identity rulings from ``~/.penumbra/state/graph_rulings.json`` — a list of
    ``{src, dst, verdict: "same"|"not_same", note, ruled_at}``. The sensors.json pattern: fail-OPEN
    to ``[]`` on absent/unreadable/malformed. There is deliberately NO writer here: the agent writes
    rulings via its own session tools; the eye only APPLIES them (it stores judgment as config, it
    never makes a judgment)."""
    try:
        if not RULINGS_PATH.exists():
            return []
        raw = json.loads(RULINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        return [r for r in raw if isinstance(r, dict)]
    except Exception as exc:  # noqa: BLE001 — a bad rulings file must never break a graph read
        logger.debug("graph_rulings.json unreadable (%s) -> []", exc)
        return []


def _atomic_write_rulings(rulings: list[dict]) -> None:
    """Write the whole rulings list atomically (the SensorStore idiom: build the list, write a
    sibling ``.tmp``, ``tmp.replace(path)`` so a reader never sees a half-written file). Parent dir
    is created if absent. Caller holds ``_RULINGS_LOCK``."""
    RULINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(rulings, ensure_ascii=False, indent=1)
    tmp = RULINGS_PATH.with_suffix(".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(RULINGS_PATH)


def save_ruling(src: str, dst: str, verdict: str, note: str = "") -> dict:
    """Record ONE identity ruling as declarative STATE (the pair is the KEY, not a log entry).

    The eye never MAKES a ruling; this stores the one the AGENT decided (the sensors.json precedent:
    judgment persisted as config the eye applies mechanically at read time). Normalizes the pair to
    ``src < dst`` (a ruling is symmetric — the same judgment regardless of direction), validates, then
    REPLACES any existing entry for that sorted pair (declarative state: re-create overwrites, git
    history is the audit trail) and atomic-writes the whole list.

    verdict must be ``same`` or ``not_same``; src/dst must be non-empty and distinct. A bad value
    raises ``ValueError`` (the tool layer maps it to an error dict). Returns
    ``{"ruling": <entry>, "replaced": <bool>}`` where the entry carries a UTC ISO ``ruled_at``."""
    src = (src or "").strip()
    dst = (dst or "").strip()
    if not src or not dst:
        raise ValueError("ruling requires non-empty src and dst")
    src, dst = sorted((src, dst))
    if src == dst:
        raise ValueError("ruling src and dst must be different nodes")
    verdict = (verdict or "").strip().lower()
    if verdict not in _RULING_VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(_RULING_VERDICTS)}; got {verdict!r}")
    entry = {"src": src, "dst": dst, "verdict": verdict, "note": (note or "").strip(),
             "ruled_at": datetime.now(timezone.utc).isoformat()}
    with _RULINGS_LOCK:
        rulings = load_rulings()
        kept = [r for r in rulings
                if sorted(((r.get("src") or "").strip(), (r.get("dst") or "").strip())) != [src, dst]]
        replaced = len(kept) != len(rulings)
        kept.append(entry)
        _atomic_write_rulings(kept)
    return {"ruling": entry, "replaced": replaced}


def delete_ruling(src: str, dst: str) -> bool:
    """Retract the ruling for a pair (the same normalization as ``save_ruling``: the sorted pair is
    the key). Atomic-writes the surviving list; returns True iff an entry was removed."""
    src = (src or "").strip()
    dst = (dst or "").strip()
    if not src or not dst:
        return False
    src, dst = sorted((src, dst))
    with _RULINGS_LOCK:
        rulings = load_rulings()
        kept = [r for r in rulings
                if sorted(((r.get("src") or "").strip(), (r.get("dst") or "").strip())) != [src, dst]]
        removed = len(kept) != len(rulings)
        if removed:
            _atomic_write_rulings(kept)
    return removed


def _ruling_index(rulings: list[dict]) -> tuple[set, dict]:
    """Fold rulings into ``(not_same_pairs, same_pairs)`` keyed by the UNORDERED node-id pair
    (frozenset), so a ruling applies regardless of which direction an edge is traversed.
    ``not_same_as`` (J) beats ``same_as`` (J): a not_same verdict wins its pair."""
    not_same: set = set()
    same: dict = {}
    for r in rulings:
        src, dst = r.get("src"), r.get("dst")
        if not src or not dst:
            continue
        key = frozenset((src, dst))
        verdict = (r.get("verdict") or "").strip().lower()
        if verdict == "not_same":
            not_same.add(key)
        elif verdict == "same":
            same[key] = r
    return not_same, same


# ── Statements store (the rulings idiom generalized; P8) ────────────────────────────────────────
# The DIRECTED, free-vocabulary sibling of the rulings store above. Every function mirrors its
# rulings twin's shape and voice; the differences are the design's essence: the KEY is the directed
# triple (src, dst, type) NOT a sorted pair (direction is the assertion, never normalized), endpoints
# are ANY node id (existing or not — the rulings precedent), and two types are refused so identity
# stays penumbra_ruling's alone.

def load_statements() -> list[dict]:
    """Read the agent's typed statements from ``~/.penumbra/state/graph_statements.json`` — a list of
    ``{src, dst, type, note, doc, stated_at}``. The sensors.json / rulings pattern: fail-OPEN to ``[]``
    on absent/unreadable/malformed. The writer is ``save_statement``; the eye only APPLIES statements
    (it stores the judgment as config, it never makes one)."""
    try:
        if not STATEMENTS_PATH.exists():
            return []
        raw = json.loads(STATEMENTS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        return [s for s in raw if isinstance(s, dict)]
    except Exception as exc:  # noqa: BLE001 — a bad statements file must never break a graph read
        logger.debug("graph_statements.json unreadable (%s) -> []", exc)
        return []


def _atomic_write_statements(statements: list[dict]) -> None:
    """Write the whole statements list atomically (the SensorStore idiom: build the list, write a
    sibling ``.tmp``, ``tmp.replace(path)`` so a reader never sees a half-written file). Parent dir is
    created if absent. Caller holds ``_STATEMENTS_LOCK``."""
    STATEMENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(statements, ensure_ascii=False, indent=1)
    tmp = STATEMENTS_PATH.with_suffix(".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(STATEMENTS_PATH)


def _slug_statement_type(type: str) -> str:
    """Mechanically slug a free-form statement type: casefold + strip, spaces -> underscores, then
    keep only ``[a-z0-9_]`` runs collapsed to single underscores, trimmed of edge underscores. The
    result must be non-empty, <= 40 chars, and not one of the refused identity types; else ValueError
    (the tool layer maps it to an error dict). This is the ONLY place a type is normalized — the views
    are OPAQUE to type (no code branches on the vocabulary), so the slug carries all the discipline."""
    raw = (type or "").strip().casefold().replace(" ", "_")
    slug = "".join(ch if (ch.isascii() and (ch.isalnum() or ch == "_")) else "_" for ch in raw)
    while "__" in slug:
        slug = slug.replace("__", "_")
    slug = slug.strip("_")
    if not slug:
        raise ValueError(f"statement type must slug to non-empty [a-z0-9_]; got {type!r}")
    if len(slug) > _STATEMENT_MAX_TYPE_LEN:
        raise ValueError(f"statement type too long ({len(slug)} > {_STATEMENT_MAX_TYPE_LEN}): {slug!r}")
    if slug in _STATEMENT_REFUSED_TYPES:
        raise ValueError(f"type {slug!r} is an IDENTITY judgment; record it via penumbra_ruling "
                         f"(same_as / not_same_as belong to the collapse machinery, not statements)")
    return slug


def save_statement(src: str, dst: str, type: str, note: str, doc: str = "") -> dict:
    """Record ONE typed, DIRECTED statement as declarative STATE (the directed triple is the KEY, not a
    log entry).

    The eye never MAKES a statement; this stores the one the AGENT decided (the rulings / sensors.json
    precedent: judgment persisted as config the eye applies mechanically at read time). The type is
    mechanically slugged (see ``_slug_statement_type``); direction is the agent's assertion and is
    NEVER normalized (unlike a ruling's sorted pair — ``(A, B, t)`` and ``(B, A, t)`` are DISTINCT).
    REPLACES any existing statement for the same directed triple (declarative state: re-create
    overwrites, git history is the audit trail) and atomic-writes the whole list.

    src/dst must be non-empty (any node id string, existing or not — the rulings precedent: a statement
    may pre-date the wall); ``note`` is REQUIRED non-empty (the judgment's reasoning); ``doc`` is the
    optional provenance node id (usually a ``doc:{source}:{sid}``; strongly encouraged, never validated
    for existence). A bad value raises ``ValueError``. Returns ``{"statement": <entry>, "replaced":
    <bool>}`` where the entry carries a UTC ISO ``stated_at``."""
    src = (src or "").strip()
    dst = (dst or "").strip()
    if not src or not dst:
        raise ValueError("statement requires non-empty src and dst")
    slug = _slug_statement_type(type)
    note = (note or "").strip()
    if not note:
        raise ValueError("statement requires a non-empty note (the judgment's reasoning)")
    doc = (doc or "").strip()
    entry = {"src": src, "dst": dst, "type": slug, "note": note, "doc": doc,
             "stated_at": datetime.now(timezone.utc).isoformat()}
    with _STATEMENTS_LOCK:
        statements = load_statements()
        kept = [s for s in statements
                if ((s.get("src") or "").strip(), (s.get("dst") or "").strip(),
                    (s.get("type") or "").strip()) != (src, dst, slug)]
        replaced = len(kept) != len(statements)
        kept.append(entry)
        _atomic_write_statements(kept)
    return {"statement": entry, "replaced": replaced}


def delete_statement(src: str, dst: str, type: str) -> bool:
    """Retract the statement for a directed triple (the SAME key as ``save_statement``: src, dst, and
    the slugged type, direction preserved). A bad/empty type slugs the same way it was stored so the
    key matches; a slug failure (e.g. empty) simply finds nothing. Atomic-writes the surviving list;
    returns True iff an entry was removed."""
    src = (src or "").strip()
    dst = (dst or "").strip()
    if not src or not dst:
        return False
    try:
        slug = _slug_statement_type(type)
    except ValueError:
        return False
    with _STATEMENTS_LOCK:
        statements = load_statements()
        kept = [s for s in statements
                if ((s.get("src") or "").strip(), (s.get("dst") or "").strip(),
                    (s.get("type") or "").strip()) != (src, dst, slug)]
        removed = len(kept) != len(statements)
        if removed:
            _atomic_write_statements(kept)
    return removed


def _statement_index(statements: list[dict]) -> dict:
    """Fold statements into an adjacency ``{node_id: [edge_dict, ...]}`` indexed by BOTH endpoints, so
    a directed statement edge is queryable from either end (like the stored graph_edges UNION). Each
    edge dict is ``{src, dst, type, method: "statement", tier: "J", note?, doc?}`` — the same edge
    shape the views already carry, tier J (the agent's judgment), method "statement" (its provenance
    channel). note / doc are attached only when present. Direction is preserved as stated."""
    index: dict[str, list[dict]] = {}
    for s in statements:
        src = (s.get("src") or "").strip()
        dst = (s.get("dst") or "").strip()
        typ = (s.get("type") or "").strip()
        if not src or not dst or not typ:
            continue
        edge: dict = {"src": src, "dst": dst, "type": typ, "method": "statement", "tier": "J"}
        if s.get("note"):
            edge["note"] = s["note"]
        if s.get("doc"):
            edge["doc"] = s["doc"]
        index.setdefault(src, []).append(edge)
        index.setdefault(dst, []).append(edge)
    return index


def _is_hand_minted(node_id: str) -> bool:
    """True for a HAND-MINTED anchor id (one the driver typed, which can fragment across sessions): a short
    synthetic id (``{kind}:{x}`` with no backend namespace, e.g. ``claim:c3_wedge`` / ``org:acme``) or a
    label-keyed id (``{kind}:label:{x}``). False for a DETERMINISTIC backend id (``work:openalex:W1``,
    ``doc:arxiv:2401``): those never fragment, so they never need the near-duplicate echo."""
    parts = (node_id or "").split(":")
    if len(parts) < 2 or not parts[0]:
        return False
    if len(parts) == 2:
        return True                      # claim:foo, org:acme -> no backend namespace
    return parts[1] == "label"           # topic:label:x hand-minted; work:openalex:W1 deterministic


def _anchor_tokens(node_id: str) -> set:
    """The significant tokens of an id's LOCAL part (after the kind[:label] prefix), for near-duplicate
    detection: split on whitespace / _ - /, casefold, drop <3-char noise. ``claim:c3_exact_credit_wedge``
    -> {exact, credit, wedge} (``c3`` dropped as 2-char)."""
    import re
    local = (node_id or "").split(":")[-1]
    return {t for t in re.split(r"[\s_\-/]+", local.casefold()) if len(t) >= 3}


def _similar_anchor_ids(target: str, statements: list[dict], limit: int = 5) -> list[str]:
    """Existing statement-endpoint ids that MAY be the SAME hand-minted anchor as ``target``: same kind
    prefix, and one token-set CONTAINED in the other (a near-duplicate is one id minus/plus a few tokens,
    ``claim:c3_wedge`` vs ``claim:c3_exact_credit_wedge`` -> {wedge} ⊂ {exact,credit,wedge}). Containment,
    not bare overlap, so a DIFFERENT claim merely sharing one word (``claim:turn_level_credit``) is not
    surfaced. Ranked by shared-token count. The mechanical half of anti-fragmentation: a create surfaces
    these so the driver REUSES an id instead of silently orphaning the edge. Only hand-minted targets fire
    (deterministic backend ids never fragment). NEVER an identity verdict (that is penumbra_ruling's): the eye
    ranks by overlap, the driver decides."""
    t = (target or "").strip()
    if not _is_hand_minted(t):
        return []
    ttoks = _anchor_tokens(t)
    if not ttoks:
        return []
    kind = t.split(":", 1)[0]
    scored: dict = {}
    for s in statements:
        for ep in ((s.get("src") or "").strip(), (s.get("dst") or "").strip()):
            if not ep or ep == t or ep in scored or ep.split(":", 1)[0] != kind:
                continue
            ctoks = _anchor_tokens(ep)
            shared = len(ttoks & ctoks)
            if shared and shared >= min(len(ttoks), len(ctoks)):   # smaller set contained in the larger
                scored[ep] = shared
    return [ep for ep, _ in sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def similar_anchors(src: str, dst: str, statements: Optional[list] = None) -> dict:
    """The anti-fragmentation echo for a create: ``{role: [near-match hand-minted ids]}`` for whichever of
    src / dst is a hand-minted id with an existing near-duplicate endpoint (empty when neither fragments).
    The tool surfaces this so the driver REUSES an anchor id instead of orphaning the edge on a
    slightly-different mint; it is mechanical overlap, NEVER an identity verdict (that is penumbra_ruling's)."""
    if statements is None:
        statements = load_statements()
    out: dict = {}
    for role, nid in (("src", src), ("dst", dst)):
        near = _similar_anchor_ids(nid, statements)
        if near:
            out[role] = near
    return out


# ── The three P1 read functions (read-only over store's DB, fail-open to empty, budgeted) ───────

def _con() -> Optional["object"]:
    """The shared read connection from ``store`` (per-thread WAL, fail-open to None when disabled)."""
    return store._read_con()


@graph_view
def find(label_query: str, kind: str = "", limit: int = 20) -> dict:
    """The graph's ENTRY POINT: turn a name into candidate node ids (every other view takes an
    anchor id and nothing else). Mechanical token/substring match over ``graph_nodes.label`` UNIONed
    with the VIRTUAL document nodes (the docs table: id=``doc:{source}:{source_id}``, label=title;
    matched via LIKE on title). Returns ``{nodes: [{id, kind, label}], capped}``, capped to ``limit``
    with ``capped: true`` when truncated (the no-silent-caps discipline). Fail-open to empty."""
    empty = {"nodes": [], "capped": False}
    q = (label_query or "").strip()
    if not q:
        return empty
    con = _con()
    if con is None:
        return empty
    # Tokenized AND match (order-free): "GRPO reinforcement" must hit a title carrying both
    # tokens anywhere, and "Siva Reddy" must survive name-order variance. Each token becomes one
    # LIKE clause; all must hold (dogfood fix 2026-07-01: a whole-phrase LIKE made multi-word
    # entry queries return zero on the live corpus).
    toks = [t for t in q.split() if t]
    likes = [f"%{t}%" for t in toks]
    ent_where = " AND ".join(["label LIKE ?"] * len(likes))
    doc_where = " AND ".join(["title LIKE ?"] * len(likes))
    # +1 so we can detect truncation (asked for limit, fetched limit+1 -> capped).
    over = max(1, limit) + 1
    out: list[dict] = []
    seen: set = set()
    try:
        # (i) materialized entity nodes. kind filter is optional (empty -> all kinds).
        if kind:
            rows = con.execute(
                f"SELECT id, kind, label FROM graph_nodes "
                f"WHERE {ent_where} AND kind = ? ORDER BY last_seen DESC LIMIT ?",
                (*likes, kind, over),
            ).fetchall()
        else:
            rows = con.execute(
                f"SELECT id, kind, label FROM graph_nodes "
                f"WHERE {ent_where} ORDER BY last_seen DESC LIMIT ?",
                (*likes, over),
            ).fetchall()
        for nid, nkind, nlabel in rows:
            if nid in seen:
                continue
            seen.add(nid)
            out.append({"id": nid, "kind": nkind, "label": nlabel})
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph.find entity scan failed: %s", exc)
    # (ii) virtual document nodes (only when the caller wants documents or all kinds).
    if not kind or kind == "document":
        try:
            rows = con.execute(
                f"SELECT source, source_id, title FROM docs "
                f"WHERE {doc_where} ORDER BY last_seen DESC LIMIT ?",
                (*likes, over),
            ).fetchall()
            for source, source_id, title in rows:
                nid = doc_node_id(source, source_id)
                if nid in seen:
                    continue
                seen.add(nid)
                out.append({"id": nid, "kind": "document", "label": title})
        except Exception as exc:  # noqa: BLE001
            logger.debug("graph.find doc scan failed: %s", exc)
    # (iii) STATEMENT endpoints (P8): a typed statement may name an id no tap ever minted (a
    # label-keyed entity, or any id string). Scan both endpoints of every statement; match the query
    # tokens against the SELF-DESCRIBING part (a ``{kind}:label:{x}`` id matches on its label segment
    # x; any other id matches on its raw string). Deduped against the node/doc hits above, kind filter
    # respected (kind from the id prefix), same limit/capped discipline. Fail-open (statements are
    # judgment config; a bad file must never break find).
    try:
        stmt_ids: set = set()
        for s in load_statements():
            for endpoint in ((s.get("src") or "").strip(), (s.get("dst") or "").strip()):
                if endpoint:
                    stmt_ids.add(endpoint)
        low_toks = [t.casefold() for t in toks]
        for nid in sorted(stmt_ids):   # sorted -> deterministic ordering
            if nid in seen:
                continue
            self_label = _id_self_label(nid)
            hay = (self_label if self_label is not None else nid).casefold()
            if not all(t in hay for t in low_toks):
                continue
            nkind = _node_kind(nid)
            if kind and nkind != kind:
                continue
            seen.add(nid)
            out.append({"id": nid, "kind": nkind, "label": self_label, "via": "statement"})
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph.find statement scan failed: %s", exc)
    capped = len(out) > max(1, limit)
    return {"nodes": out[: max(1, limit)], "capped": capped}


@graph_view
def stats() -> dict:
    """The cheap orientation call: counts by node kind (incl. the virtual docs count), edge counts
    by type and by tier, the rulings count, and the statements count. Fail-open — any sub-count that
    fails is simply omitted / zero. NOTE the cold-start expectation (design section 10): at P1 the
    entity kinds are EMPTY and only document same_as edges exist (derived, so they do not appear in
    graph_edges either); empty entity kinds here are CORRECT, not broken. ``document`` counts the
    virtual docs (indexed sources); ``document_thin`` counts the retrieval-anchored thin rows (P2.0 —
    docs from non-indexed sources, title + url only) SEPARATELY so the cold-start story stays honest.
    ``document_thin_embedded`` counts the vec_thin rows (P7 — thin titles that have an embedding, the
    mechanical coverage gauge for how much of the thin perception history ``similar`` can see).
    ``statements`` counts the agent's typed statements (P8 — the volume gauge beside ``rulings``, so a
    migration off the JSON file stays a measured decision, not a pre-emptive one)."""
    result: dict = {"node_kinds": {}, "edge_types": {}, "edge_tiers": {}, "rulings": 0,
                    "statements": 0}
    con = _con()
    if con is None:
        result["rulings"] = len(load_rulings())
        result["statements"] = len(load_statements())
        return result
    try:
        for kind, n in con.execute("SELECT kind, count(*) FROM graph_nodes GROUP BY kind").fetchall():
            # Thin document nodes (kind='document' in graph_nodes) count SEPARATELY from the virtual
            # docs-table documents; true entity kinds pass through under their own name.
            result["node_kinds"]["document_thin" if kind == "document" else kind] = n
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph.stats node_kinds failed: %s", exc)
    try:  # virtual document nodes are the docs table, not graph_nodes rows.
        result["node_kinds"]["document"] = con.execute("SELECT count(*) FROM docs").fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph.stats doc count failed: %s", exc)
    try:  # P7 thin-title embedding coverage gauge: vec_thin rows (a mechanical count, no ratio, no
        # judgment). Beside document_thin so "how much of the thin history similar can see" is visible.
        result["node_kinds"]["document_thin_embedded"] = con.execute(
            "SELECT count(*) FROM vec_thin").fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph.stats vec_thin count failed: %s", exc)
    try:
        for typ, n in con.execute("SELECT type, count(*) FROM graph_edges GROUP BY type").fetchall():
            result["edge_types"][typ] = n
        for tier, n in con.execute("SELECT tier, count(*) FROM graph_edges GROUP BY tier").fetchall():
            result["edge_tiers"][tier] = n
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph.stats edge counts failed: %s", exc)
    result["rulings"] = len(load_rulings())
    result["statements"] = len(load_statements())
    return result


def _stored_edges(con, frontier: list[str], types: Optional[list[str]]) -> list[dict]:
    """Stored graph_edges rows touching the frontier (either endpoint), one hop. Scoped to the
    frontier ids (never a full-table scan); optional type filter. Direction preserved as stored."""
    if not frontier:
        return []
    marks = ",".join("?" * len(frontier))
    type_clause = ""
    params: list = list(frontier) + list(frontier)
    if types:
        tmarks = ",".join("?" * len(types))
        type_clause = f" AND type IN ({tmarks})"
        params = list(frontier) + list(types) + list(frontier) + list(types)
    sql = (
        f"SELECT src, dst, type, method FROM graph_edges WHERE src IN ({marks}){type_clause} "
        f"UNION SELECT src, dst, type, method FROM graph_edges WHERE dst IN ({marks}){type_clause}"
    )
    try:
        rows = con.execute(sql, params).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph._stored_edges failed: %s", exc)
        return []
    return [{"src": s, "dst": d, "type": t, "method": m} for (s, d, t, m) in rows]


def _stored_edges_since(con, anchor: str, types: Optional[list[str]], cutoff: float) -> list[dict]:
    """STORED graph_edges rows touching ``anchor`` (either endpoint, one hop) whose ``first_seen`` is
    >= ``cutoff`` — the accretion-log SELECT the ``since`` view needs. Unlike the shared
    ``_stored_edges`` (which returns only src/dst/type/method), this returns ``tier`` + ``first_seen``
    too: since projects accretion HISTORY with honest epistemics (every row shows its tier + method;
    the reader judges), and it orders by recency, so both columns are load-bearing. Scoped to the one
    anchor (never a full-table scan); optional type filter. Fail-open to []."""
    if not anchor:
        return []
    type_clause = ""
    params: list = [anchor, cutoff]
    if types:
        tmarks = ",".join("?" * len(types))
        type_clause = f" AND type IN ({tmarks})"
        params = [anchor, *types, cutoff, anchor, *types, cutoff]
    else:
        params = [anchor, cutoff, anchor, cutoff]
    sql = (
        f"SELECT src, dst, type, tier, method, first_seen FROM graph_edges "
        f"WHERE src = ?{type_clause} AND first_seen >= ? "
        f"UNION SELECT src, dst, type, tier, method, first_seen FROM graph_edges "
        f"WHERE dst = ?{type_clause} AND first_seen >= ?"
    )
    try:
        rows = con.execute(sql, params).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph._stored_edges_since failed: %s", exc)
        return []
    return [{"src": s, "dst": d, "type": t, "tier": ti, "method": m, "first_seen": fs}
            for (s, d, t, ti, m, fs) in rows]


def _stated_at_epoch(stated_at: str) -> Optional[float]:
    """Parse a statement's ``stated_at`` (a full UTC ISO stamp from ``datetime.isoformat``) to an
    epoch-UTC float for the ``since`` cutoff comparison + ordering. A naive stamp is read as UTC.
    None on empty/unparseable (that statement is then simply not surfaced — fail-open)."""
    s = (stated_at or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception as exc:  # noqa: BLE001 — an unparseable stamp is skipped, never a crash
        logger.debug("graph._stated_at_epoch failed for %r: %s", stated_at, exc)
        return None


def _statements_since(anchor: str, types: Optional[list[str]], cutoff: float) -> list[dict]:
    """The agent's STATEMENTS touching ``anchor`` (either endpoint, one hop) whose ``stated_at`` is
    >= ``cutoff`` — the statement arm of the ``since`` accretion log (P8: a judgment accreting is an
    EVENT). Returns the SAME edge-row shape as ``_stored_edges_since`` but with ``stated_at`` surfaced
    in place of ``first_seen`` (tier "J", method "statement"), respecting the optional ``types`` filter.
    UNLIKE the collapsing views, since threads NO policy here: a statement is surfaced with its honest
    tier + method and the reader judges (since never collapses identity). Fail-open to []."""
    if not anchor:
        return []
    type_set = set(types) if types else None
    out: list[dict] = []
    for s in load_statements():
        src = (s.get("src") or "").strip()
        dst = (s.get("dst") or "").strip()
        typ = (s.get("type") or "").strip()
        if not src or not dst or not typ:
            continue
        if anchor not in (src, dst):
            continue
        if type_set is not None and typ not in type_set:
            continue
        epoch = _stated_at_epoch(s.get("stated_at") or "")
        if epoch is None or epoch < cutoff:
            continue
        row: dict = {"src": src, "dst": dst, "type": typ, "tier": "J", "method": "statement",
                     "stated_at": s.get("stated_at")}
        if s.get("doc"):
            row["doc"] = s["doc"]
        out.append(row)
    return out


def _anchor_fp(con, source: str, source_id: str) -> Optional[str]:
    """The title fingerprint of a frontier doc node — docs.fp if indexed, else the thin row's
    ``attrs_json.fp`` (P2.0 thin-memory coverage). Returns the fp ONLY when it is a ``title:``
    alignment (id-fallback fps never align cross-source); else None."""
    fp: Optional[str] = None
    try:
        row = con.execute(
            "SELECT fp FROM docs WHERE source = ? AND source_id = ?", (source, source_id)
        ).fetchone()
        if row is not None:
            fp = row[0]
        else:
            row = con.execute(
                "SELECT json_extract(attrs_json, '$.fp') FROM graph_nodes "
                "WHERE id = ? AND kind = 'document'", (doc_node_id(source, source_id),)
            ).fetchone()
            if row is not None:
                fp = row[0]
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph._anchor_fp failed: %s", exc)
        return None
    if fp and str(fp).startswith("title:"):
        return fp
    return None


def _derived_title_fp_edges(con, frontier: list[str]) -> list[dict]:
    """DERIVED same_as edges (design v2.0 + P2.0): a shared title fingerprint IS the title_fp same_as
    edge set, live and always current (no stored rows). Scoped to frontier DOC nodes each hop and
    keyed on the anchor's exact fp value (never a full-table cross join). method ``align:title_fp``,
    tier A (exploratory-only). Only ``title:`` fingerprints align (id-fallback fps are not). Coverage
    spans BOTH stores: a thin row's attrs fp can match a docs.fp AND another thin row's fp, so a
    non-indexed arXiv original and its indexed mirror unify identically to two docs-table rows."""
    doc_ids = [f for f in frontier if f.startswith("doc:")]
    if not doc_ids:
        return []
    edges: list[dict] = []
    for nid in doc_ids:
        rest = nid[len("doc:"):]
        source, _, source_id = rest.partition(":")
        if not source or not source_id:
            continue
        fp = _anchor_fp(con, source, source_id)
        if not fp:
            continue
        anchor = doc_node_id(source, source_id)
        others: list[str] = []
        try:
            # (a) indexed docs sharing this fp (docs.fp is indexed → an equality probe, not a scan).
            for b_source, b_source_id in con.execute(
                "SELECT source, source_id FROM docs WHERE fp = ?", (fp,)
            ).fetchall():
                others.append(doc_node_id(b_source, b_source_id))
            # (b) thin document nodes whose attrs fp matches (bounded to document rows).
            for (other_id,) in con.execute(
                "SELECT id FROM graph_nodes WHERE kind = 'document' "
                "AND json_extract(attrs_json, '$.fp') = ?", (fp,)
            ).fetchall():
                others.append(other_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("graph._derived_title_fp_edges match failed: %s", exc)
            continue
        for other in others:
            if other == anchor:
                continue
            lo, hi = sorted((anchor, other))  # symmetric: store once with src < dst
            edges.append({"src": lo, "dst": hi, "type": "same_as", "method": "align:title_fp"})
    return edges


def _id_eq_triple(con, source: str, source_id: str) -> Optional[tuple]:
    """The ``(openalex_id, doi, arxiv_id)`` for a frontier doc node — from the docs table if it is an
    indexed doc, ELSE from its thin graph_nodes row's attrs_json (the P2.0 thin-memory coverage).
    A given ``doc:{source}:{sid}`` is in exactly ONE store (indexable -> docs; non-indexed -> thin),
    so docs-first-then-thin never double-reads. The attrs_json keys are the SAME names writer.py's
    thin upsert lifts (``openalex_id`` / ``doi`` / ``arxiv_id``), read here at the FLAT attrs root
    (``$.openalex_id``) — mirroring the docs arm's ``$.metadata.openalex_id``. None -> no row found."""
    try:
        row = con.execute(
            "SELECT json_extract(doc_json, '$.metadata.openalex_id'), "
            "json_extract(doc_json, '$.metadata.doi'), "
            "json_extract(doc_json, '$.metadata.arxiv_id') "
            "FROM docs WHERE source = ? AND source_id = ?",
            (source, source_id),
        ).fetchone()
        if row is not None:
            return row
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph._id_eq_triple docs json_extract failed: %s", exc)
    # Thin document node: same keys, at the flat attrs_json root.
    try:
        row = con.execute(
            "SELECT json_extract(attrs_json, '$.openalex_id'), "
            "json_extract(attrs_json, '$.doi'), "
            "json_extract(attrs_json, '$.arxiv_id') "
            "FROM graph_nodes WHERE id = ? AND kind = 'document'",
            (doc_node_id(source, source_id),),
        ).fetchone()
        return row
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph._id_eq_triple thin json_extract failed: %s", exc)
        return None


def _derived_id_eq_edges(con, frontier: list[str]) -> list[dict]:
    """DERIVED doc->work id_eq same_as (design v2.0): a document's external ids equal a world
    entity's id. Query-time via json_extract, scoped to frontier DOC nodes, covering BOTH indexed
    docs (docs.doc_json) and thin document nodes (graph_nodes.attrs_json — the P2.0 thin-memory arm).
    methods ``id_eq:openalex`` / ``id_eq:doi`` / ``id_eq:arxiv`` (conservative). This joins the
    perception atom (doc) to the bibliographic entity (work) WITHOUT collapsing them into one node."""
    doc_ids = [f for f in frontier if f.startswith("doc:")]
    if not doc_ids:
        return []
    edges: list[dict] = []
    for nid in doc_ids:
        rest = nid[len("doc:"):]
        source, _, source_id = rest.partition(":")
        if not source or not source_id:
            continue
        row = _id_eq_triple(con, source, source_id)
        if not row:
            continue
        openalex_id, doi, arxiv_id = row
        if openalex_id:
            edges.append({"src": nid, "dst": f"work:openalex:{openalex_id}",
                          "type": "same_as", "method": "id_eq:openalex"})
        if doi:
            edges.append({"src": nid, "dst": f"work:doi:{str(doi).strip().lower()}",
                          "type": "same_as", "method": "id_eq:doi"})
        if arxiv_id:
            edges.append({"src": nid, "dst": f"work:arxiv:{arxiv_id}",
                          "type": "same_as", "method": "id_eq:arxiv"})
    return edges


def _apply_policy_and_rulings(edges: list[dict], methods: frozenset,
                              not_same: set, same: dict, use_rulings: bool) -> list[dict]:
    """Apply the collapse POLICY + the agent's rulings. A policy governs IDENTITY only (design
    section 2: policies "choose how much to trust" a ``same_as`` collapse) — it is NOT a fact filter:
    mechanical relation edges (cites, authored, affiliated, coauthored...) are FACTS and always pass,
    only ``same_as`` candidates are gated by the method-set. Then the rulings overlay
    (working / exploratory only): a ``not_same`` ruling REMOVES a same_as candidate for that pair; a
    ``same`` ruling ADDS a traversal edge {method "ruling", tier-label "J-ruling"}. ``not_same_as``
    beats ``same_as`` beats M beats A (design section 2): the not_same removal is applied last so it
    wins even over a same-ruling add."""
    kept: list[dict] = []
    for e in edges:
        # Identity edges collapse ONLY when their method is in the policy set; facts always pass.
        if e.get("type") == "same_as" and e.get("method") not in methods:
            continue
        if use_rulings and e.get("type") == "same_as" \
                and frozenset((e["src"], e["dst"])) in not_same:
            continue  # a not_same ruling rejects this same_as candidate
        kept.append(e)
    if use_rulings:
        # ADD same-ruling edges for traversal (deduped against what we already have).
        have = {(e["src"], e["dst"], e["type"], e.get("method")) for e in kept}
        for key in same:
            pair = tuple(key)
            if len(pair) != 2:
                continue
            a, b = sorted(pair)
            if frozenset((a, b)) in not_same:
                continue  # not_same beats same
            edge = {"src": a, "dst": b, "type": "same_as", "method": "ruling", "tier": "J-ruling"}
            if (a, b, "same_as", "ruling") not in have:
                kept.append(edge)
    return kept


def _statement_hop_edges(stmt_index: dict, frontier: list[str],
                         types: Optional[list[str]]) -> list[dict]:
    """The statement edges touching ``frontier`` (either endpoint), from a PRE-LOADED index (loaded
    ONCE per view call and threaded through, the way rulings are — never re-read per hop). Respects the
    ``types`` filter (a statement's free type is matched against the same ``types`` list stored edges
    honor). Deduped on the directed (src, dst, type) key so a frontier carrying both endpoints of one
    statement yields it once. Statement edges are DIRECTED, typed FACTS (tier J, method "statement");
    they are never ``same_as`` (those types are refused at write), so ``_apply_policy_and_rulings``
    passes them through unchanged — the collapse method-set gates identity only."""
    if not stmt_index:
        return []
    type_set = set(types) if types else None
    out: list[dict] = []
    seen: set = set()
    for node in frontier:
        for e in stmt_index.get(node, ()):
            if type_set is not None and e.get("type") not in type_set:
                continue
            key = (e["src"], e["dst"], e["type"])
            if key in seen:
                continue
            seen.add(key)
            out.append(e)
    return out


def _policy_hop_edges(con, frontier: list[str], types: Optional[list[str]], methods: frozenset,
                      not_same: set, same: dict, use_rulings: bool,
                      stmt_index: Optional[dict] = None) -> list[dict]:
    """ONE hop of edges off ``frontier``: the UNION of (i) stored graph_edges rows, (ii) the two
    DERIVED same_as sets (title_fp when the policy admits it; doc-work id_eq when any id_eq method is
    in the policy), and (iii) the agent's STATEMENTS touching the frontier when ``use_rulings`` is on
    (working / exploratory: "add MY OWN judgments" — the SAME gate the rulings overlay rides), then
    filtered by ``_apply_policy_and_rulings``. This is the SINGLE per-hop machinery ``neighborhood`` /
    ``between`` / ``voices`` all share (extracted so the three views can never drift apart), each
    scoped to the frontier (never a full-table scan). ``stmt_index`` is the pre-loaded statement
    adjacency (loaded ONCE per view call and threaded through, never re-read per hop); it is None /
    empty under conservative, so conservative NEVER shows a statement."""
    hop_edges: list[dict] = []
    hop_edges += _stored_edges(con, frontier, types)
    if "align:title_fp" in methods:  # exploratory-only (title_fp is in EXPLORATORY alone)
        hop_edges += _derived_title_fp_edges(con, frontier)
    if ("id_eq:doi" in methods) or ("id_eq:arxiv" in methods) or ("id_eq:openalex" in methods):
        hop_edges += _derived_id_eq_edges(con, frontier)
    if use_rulings and stmt_index:  # the agent's typed judgments (working / exploratory only)
        hop_edges += _statement_hop_edges(stmt_index, frontier, types)
    return _apply_policy_and_rulings(hop_edges, methods, not_same, same, use_rulings)


@graph_view
def neighborhood(anchor: str, depth: int = 1, types: Optional[list[str]] = None,
                 policy: str = "conservative", max_nodes: int = 40) -> dict:
    """The bounded subgraph around a node: BFS over the UNION of (i) stored graph_edges rows,
    (ii) the two DERIVED same_as edge sets (title_fp + doc-work id_eq), and (iii) the agent's typed
    STATEMENTS touching the frontier (working / exploratory only — conservative stays the pure
    mechanical world), each scoped to the frontier per hop (never a full-table cross join). The policy
    method-set filters which same_as candidates count, then the rulings overlay applies
    (working/exploratory). Returns ``{nodes: [{id, kind, label}], edges: [{src, dst, type, method,
    ...}], capped, truncation}`` (a statement edge additionally carries tier "J" + its note/doc)
    respecting ``max_nodes`` with a MECHANICAL order (recency then degree). depth is capped at 2,
    max_nodes floored at 1. Fail-open to just the anchor node on any error."""
    depth = max(0, min(int(depth), 2))
    max_nodes = max(1, int(max_nodes))
    methods = _policy_methods(policy)
    use_rulings = policy in ("working", "exploratory")
    result: dict = {"nodes": [], "edges": [], "capped": False,
                    "truncation": "recency-then-degree"}
    anchor = (anchor or "").strip()
    if not anchor:
        return result
    con = _con()
    if con is None:
        return result

    rulings = load_rulings() if use_rulings else []
    not_same, same = _ruling_index(rulings)
    # The agent's typed statements, loaded ONCE per call and threaded through every hop (never
    # re-read per hop, exactly like the rulings overlay). Empty under conservative -> no statements.
    stmt_index = _statement_index(load_statements()) if use_rulings else None

    visited: set = {anchor}
    frontier: list[str] = [anchor]
    all_edges: list[dict] = []
    edge_seen: set = set()

    try:
        for _hop in range(depth):
            if not frontier:
                break
            # The shared per-hop machinery (stored + derived same_as + statements, policy-filtered).
            hop_edges = _policy_hop_edges(con, frontier, types, methods, not_same, same, use_rulings,
                                          stmt_index)
            next_frontier: list[str] = []
            for e in hop_edges:
                ekey = (e["src"], e["dst"], e["type"], e.get("method"))
                if ekey not in edge_seen:
                    edge_seen.add(ekey)
                    all_edges.append(e)
                for endpoint in (e["src"], e["dst"]):
                    if endpoint not in visited:
                        visited.add(endpoint)
                        next_frontier.append(endpoint)
            frontier = next_frontier
    except Exception as exc:  # noqa: BLE001 — a broken hop degrades to what we gathered so far
        logger.debug("graph.neighborhood BFS failed: %s", exc)

    # Hydrate node labels + kinds, then apply the mechanical max_nodes truncation.
    nodes = _hydrate_nodes(con, visited, anchor)
    capped = len(nodes) > max_nodes
    if capped:
        nodes = _truncate_nodes(con, nodes, max_nodes, anchor)
        kept_ids = {n["id"] for n in nodes}
        all_edges = [e for e in all_edges if e["src"] in kept_ids and e["dst"] in kept_ids]
    result["nodes"] = nodes
    result["edges"] = all_edges
    result["capped"] = capped
    return result


# ── voices: the independence projection (design section 7 + the P3 amendment) ────────────────────
# THE ONE independence mechanism (the parsimony audit deleted the derived independence_score that
# counted source NAMES, not upstream voices). voices collapses a DOC SET to distinct upstream VOICES
# via same_as + authored, so "N sources agree" becomes "N INDEPENDENT voices agree" (or fewer). It
# COUNTS EVIDENCE, NEVER ABSENCE: a component with resolution evidence is a voice; a doc with zero
# connecting evidence lands in ``unresolved`` and is NEVER counted as a voice (counting unknowns as
# distinct voices would mechanically FABRICATE independence). Two voices sharing a person MERGE
# (shared speaker = not independent) — which falls out of union-find automatically since persons are
# in the union. The projection is mechanical; reading it is judgment.

_VOICES_MAX = 64   # explicit input cap: split a larger set, never silently truncate (no-silent-caps)


class _UnionFind:
    """A tiny union-find over string node ids (no external dep; the graph adds zero new tech)."""
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, x: str) -> None:
        self.parent.setdefault(x, x)

    def find(self, x: str) -> str:
        self.add(x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:   # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # deterministic root (lexicographically smaller) so component grouping is stable.
            lo, hi = sorted((ra, rb))
            self.parent[hi] = lo


@graph_view
def voices(doc_ids: list[str], policy: str = "conservative") -> dict:
    """Collapse a DOC SET to distinct upstream VOICES via same_as + authored — the independence
    counter. Input: graph doc ids (``doc:{source}:{source_id}``). NON-``doc:``-prefixed entries are
    collected into ``skipped`` (not an error — the agent may pass a mixed handful). More than 64 ids
    returns an explicit error (split the set; never a silent truncation).

    Builds the identity/authorship evidence pool SCOPED to the input docs (never a full-table scan):
    (a) doc->work id_eq same_as, (b) doc<->doc title_fp same_as when the policy admits it, (c) the
    rulings overlay (working/exploratory: a ``same`` ruling between input docs ADDS a link, a
    ``not_same`` ruling REMOVES a candidate), (d) work->person ``authored`` edges for the works
    reached in (a). Union-find over docs ∪ works ∪ persons; each component restricted to the input
    docs is either a VOICE (>= 1 work link, or >= 2 docs joined by same_as; ``speaker_known`` flags
    >= 1 person) or, if a lone doc with ZERO evidence, an ``unresolved`` entry. Returns
    ``{voices, n_voices, unresolved, n_unresolved, policy, skipped?}`` with deterministic ordering.
    Fail-open to all-unresolved on a connection failure."""
    ids = [str(d) for d in (doc_ids or [])]
    if len(ids) > _VOICES_MAX:
        return {"error": f"voices takes at most {_VOICES_MAX} doc ids; split the set"}
    docs = [d for d in ids if d.startswith("doc:")]
    skipped = [d for d in ids if not d.startswith("doc:")]
    fail_open = {"voices": [], "n_voices": 0, "unresolved": docs,
                 "n_unresolved": len(docs), "policy": policy}
    if skipped:
        fail_open["skipped"] = skipped
    if not docs:
        return fail_open
    con = _con()
    if con is None:
        return fail_open

    methods = _policy_methods(policy)
    use_rulings = policy in ("working", "exploratory")
    rulings = load_rulings() if use_rulings else []
    not_same, same = _ruling_index(rulings)
    doc_set = set(docs)

    try:
        # (a)+(b)+(c): the same_as evidence among the input docs (id_eq doc->work always; title_fp
        # doc<->doc only when the policy admits it), policy-filtered + rulings overlaid. Reuse the
        # SAME per-hop machinery the other views use (scoped to the input docs as the frontier).
        # NO stmt_index is threaded here: voices deliberately IGNORES statements — an arbitrary typed
        # judgment ("advises", "refutes") is not identity evidence, and independence counting must stay
        # same_as / authored only (the ["same_as"] filter would drop non-same_as statements anyway).
        same_as_edges = _policy_hop_edges(con, docs, ["same_as"], methods, not_same, same, use_rulings)
        # A same-ruling ADD is only relevant here when it joins two INPUT docs (the overlay is scoped
        # to the doc set); id_eq / title_fp edges already have an input doc as one endpoint by
        # construction (they are derived off the input frontier).
        uf = _UnionFind()
        for d in docs:
            uf.add(d)
        works: set = set()
        for e in same_as_edges:
            s, t = e.get("src"), e.get("dst")
            if not s or not t:
                continue
            # a ruling-added same_as must have BOTH endpoints among the input docs to count (scope);
            # id_eq / title_fp always carry an input doc endpoint already.
            if e.get("method") == "ruling" and not ({s, t} <= doc_set):
                continue
            uf.add(s)
            uf.add(t)
            uf.union(s, t)
            for endpoint in (s, t):
                if endpoint.startswith("work:"):
                    works.add(endpoint)
        # (d): work->person authored edges for the works reached in (a), endpoints IN the work set.
        persons: set = set()
        if works:
            for e in _stored_edges(con, list(works), ["authored"]):
                if e.get("type") != "authored":
                    continue
                s, t = e.get("src"), e.get("dst")
                if not s or not t:
                    continue
                # keep only edges whose work endpoint is one we reached (never pull in foreign works).
                if s not in works and t not in works:
                    continue
                uf.add(s)
                uf.add(t)
                uf.union(s, t)
                for endpoint in (s, t):
                    if endpoint.startswith("person:"):
                        persons.add(endpoint)

        # Group the union-find components, then classify each by its INPUT docs' evidence.
        comps: dict[str, dict] = {}
        # seed a bucket per input doc's root so a lone doc still forms its own component.
        for node in list(uf.parent.keys()):
            root = uf.find(node)
            b = comps.setdefault(root, {"docs": set(), "works": set(), "persons": set()})
            if node in doc_set:
                b["docs"].add(node)
            elif node.startswith("work:"):
                b["works"].add(node)
            elif node.startswith("person:"):
                b["persons"].add(node)

        voice_list: list[dict] = []
        unresolved: list[str] = []
        for b in comps.values():
            cdocs = b["docs"]
            if not cdocs:
                continue  # a component with no input doc (a foreign mirror) is not counted
            # EVIDENCE (never absence): >= 1 work link OR >= 2 input docs joined by same_as.
            if b["works"] or len(cdocs) >= 2:
                voice_list.append({
                    "docs": sorted(cdocs),
                    "works": sorted(b["works"]),
                    "persons": sorted(b["persons"]),
                    "speaker_known": len(b["persons"]) >= 1,
                })
            else:
                # a lone doc with zero evidence — NEVER a voice (would fabricate independence).
                unresolved.extend(sorted(cdocs))
        # Deterministic ordering: voices by their first doc, unresolved sorted.
        voice_list.sort(key=lambda v: v["docs"][0] if v["docs"] else "")
        unresolved.sort()
        result: dict = {"voices": voice_list, "n_voices": len(voice_list),
                        "unresolved": unresolved, "n_unresolved": len(unresolved),
                        "policy": policy}
        if skipped:
            result["skipped"] = skipped
        return result
    except Exception as exc:  # noqa: BLE001 — fail-open: a voices failure never breaks the caller
        logger.debug("graph.voices failed: %s", exc)
        return fail_open


@graph_view
def between(a: str, b: str, types: Optional[list[str]] = None, policy: str = "conservative",
            max_nodes: int = 40) -> dict:
    """Bounded connection PATHS between two anchors — the "how do these relate" question. Bidirectional
    BFS, at most 2 hops per side (max path length 4), over the SAME per-hop edge machinery as
    ``neighborhood`` (stored + the two derived same_as arms per the policy + the rulings overlay + the
    agent's typed statements under working / exploratory), so a path may walk THROUGH a statement edge.
    Edges are traversed UNDIRECTED for pathfinding but returned as stored/derived (direction
    preserved). Collects up to 8 SHORTEST paths, ordered (length, path tuple) deterministically.
    Returns ``{paths: [[node ids]], nodes: [{id, kind, label}] (only nodes on returned paths), edges:
    [edges on returned paths], capped, truncation}``. ``capped`` is true when more than 8 paths
    existed or the node budget trimmed paths. ``a == b`` or an empty anchor -> an empty result with a
    note; no path -> ``{paths: []}`` (an honest empty, not an error). Fail-open like the other views."""
    max_nodes = max(1, int(max_nodes))
    methods = _policy_methods(policy)
    use_rulings = policy in ("working", "exploratory")
    result: dict = {"paths": [], "nodes": [], "edges": [], "capped": False,
                    "truncation": "shortest-then-lexicographic"}
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        result["note"] = "between needs two anchors"
        return result
    if a == b:
        result["note"] = "a and b are the same node"
        return result
    con = _con()
    if con is None:
        return result

    rulings = load_rulings() if use_rulings else []
    not_same, same = _ruling_index(rulings)
    # The agent's typed statements, loaded ONCE and threaded through both sides' hops (never re-read
    # per hop). Empty under conservative, so conservative paths stay the pure mechanical world.
    stmt_index = _statement_index(load_statements()) if use_rulings else None

    # Build an UNDIRECTED adjacency out to <=2 hops from EACH side (bidirectional BFS meets in the
    # middle, so a length-<=4 path is covered), recording the directed edge on each traversed pair so
    # the returned edges keep their stored/derived direction. Each hop reuses _policy_hop_edges.
    adj: dict[str, set] = {}
    edge_by_pair: dict[frozenset, dict] = {}

    def _grow(seed: str) -> None:
        visited: set = {seed}
        frontier = [seed]
        for _hop in range(2):   # <=2 hops per side
            if not frontier:
                break
            hop_edges = _policy_hop_edges(con, frontier, types, methods, not_same, same, use_rulings,
                                          stmt_index)
            nxt: list[str] = []
            for e in hop_edges:
                s, t = e.get("src"), e.get("dst")
                if not s or not t:
                    continue
                adj.setdefault(s, set()).add(t)
                adj.setdefault(t, set()).add(s)   # undirected for pathfinding
                edge_by_pair.setdefault(frozenset((s, t)), e)   # keep the directed edge as stored
                for endpoint in (s, t):
                    if endpoint not in visited:
                        visited.add(endpoint)
                        nxt.append(endpoint)
            frontier = nxt

    try:
        _grow(a)
        _grow(b)
    except Exception as exc:  # noqa: BLE001 — a broken expansion degrades to no paths
        logger.debug("graph.between expansion failed: %s", exc)
        return result

    # Enumerate simple paths a..b of length <= 4 over the undirected adjacency, via bounded DFS.
    all_paths: list[list[str]] = []
    _PATH_CAP_SCAN = 4000   # a hard ceiling on DFS steps (the adjacency is already hop-bounded)
    steps = [0]

    def _dfs(node: str, target: str, path: list[str], seen: set) -> None:
        if steps[0] > _PATH_CAP_SCAN or len(all_paths) > 64:
            return
        if len(path) - 1 > 4:   # path length (edge count) cap
            return
        if node == target:
            all_paths.append(list(path))
            return
        for nb in sorted(adj.get(node, ())):   # sorted -> deterministic enumeration
            steps[0] += 1
            if nb in seen:
                continue
            if len(path) - 1 >= 4:
                continue
            seen.add(nb)
            path.append(nb)
            _dfs(nb, target, path, seen)
            path.pop()
            seen.discard(nb)

    try:
        _dfs(a, b, [a], {a})
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph.between DFS failed: %s", exc)

    if not all_paths:
        return result   # honest empty: no path within the bound (not an error)

    # Deterministic order: shortest first, then lexicographically by the path tuple. Keep up to 8.
    all_paths.sort(key=lambda p: (len(p), tuple(p)))
    capped = len(all_paths) > 8
    kept_paths = all_paths[:8]

    # Collect the nodes + edges that lie on the kept paths; honor the node budget (trim paths that
    # would blow it, stamping capped).
    on_path_nodes: list[str] = []
    seen_nodes: set = set()
    final_paths: list[list[str]] = []
    for p in kept_paths:
        new_nodes = [n for n in p if n not in seen_nodes]
        if len(seen_nodes) + len(new_nodes) > max_nodes and final_paths:
            capped = True
            break   # this path would exceed the node budget; stop (we already have >=1 path)
        for n in p:
            if n not in seen_nodes:
                seen_nodes.add(n)
                on_path_nodes.append(n)
        final_paths.append(p)

    # Edges on the final paths (consecutive pairs), returned as the stored/derived directed edge.
    edges_out: list[dict] = []
    edge_seen: set = set()
    for p in final_paths:
        for i in range(len(p) - 1):
            key = frozenset((p[i], p[i + 1]))
            e = edge_by_pair.get(key)
            if e is None:
                continue
            ekey = (e["src"], e["dst"], e.get("type"), e.get("method"))
            if ekey not in edge_seen:
                edge_seen.add(ekey)
                edges_out.append(e)

    nodes_out = _hydrate_nodes(con, set(on_path_nodes), a)
    result["paths"] = final_paths
    result["nodes"] = nodes_out
    result["edges"] = edges_out
    result["capped"] = capped
    return result


# ── since: the accretion log (design section 7 + the P4 sensor consumer) ─────────────────────────
# "What accreted around X after T." STORED edges only: derived edges (title_fp / id_eq same_as) are
# computed live and carry NO timestamps, so they are structurally absent here — since projects a
# fact STREAM (what changed), not an identity question, and it does NOT collapse identity (no policy,
# no rulings overlay). Every row returns WITH its tier + method visible: honest epistemics, the
# reader judges. The natural consumer is a sensor's observed edges ("what did this standing query
# newly surface after T"), but any stored edge touching the anchor qualifies.

def _parse_since_cutoff(date: str) -> Optional[float]:
    """Parse ``date`` to an epoch-UTC float. Accepts a bare ``YYYY-MM-DD`` (= midnight UTC that day)
    or a full ISO timestamp (``2026-07-03T12:00:00+00:00``; a naive timestamp is read as UTC). Returns
    None when empty/unparseable (the caller maps that to an error). Mechanical, no guessing."""
    s = (date or "").strip()
    if not s:
        return None
    try:
        if len(s) == 10 and s[4] == "-" and s[7] == "-":   # bare YYYY-MM-DD -> midnight UTC
            dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception as exc:  # noqa: BLE001 — an unparseable date is a caller error, not a crash
        logger.debug("graph._parse_since_cutoff failed for %r: %s", date, exc)
        return None


@graph_view
def since(anchor: str, date: str, types: Optional[list[str]] = None, max_nodes: int = 40) -> dict:
    """The ACCRETION LOG around a node: the STORED edges touching ``anchor`` (one hop, both
    directions, optional ``types`` filter) whose ``first_seen`` is >= the parsed cutoff, PLUS the
    agent's typed STATEMENTS touching the anchor whose ``stated_at`` is >= the cutoff (P8: a judgment
    accreting is an event, surfaced with ``stated_at`` in place of ``first_seen``). DERIVED same_as
    edges (title_fp / doc-work id_eq) are computed live and carry NO timestamps, so they stay
    structurally absent. There is NO policy and NO rulings/statement COLLAPSE overlay: since projects
    accretion HISTORY, it does not collapse identity; every row returns WITH its ``tier`` and
    ``method`` visible (honest epistemics — the reader judges), including the statement rows (tier
    "J", method "statement").

    ``date`` accepts ``YYYY-MM-DD`` (= midnight UTC) or a full ISO timestamp; empty/unparseable ->
    ``{"error": ...}``. Returns ``{edges: [{src, dst, type, tier, method, first_seen | stated_at}],
    nodes: [hydrated endpoints], since: <parsed cutoff, ISO>, capped, truncation}`` with the anchor's
    neighborhood capped at ``max_nodes`` by the same MECHANICAL truncation as neighborhood (recency
    then degree; edges whose endpoints were dropped are dropped too). Deterministic ordering (recency
    DESC over both first_seen and stated_at, then the edge tuple). Fail-open to empty on any error."""
    max_nodes = max(1, int(max_nodes))
    anchor = (anchor or "").strip()
    cutoff = _parse_since_cutoff(date)
    if cutoff is None:
        return {"error": "since requires date=YYYY-MM-DD (or full ISO)"}
    since_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
    result: dict = {"edges": [], "nodes": [], "since": since_iso, "capped": False,
                    "truncation": "recency-then-degree"}
    if not anchor:
        return result
    con = _con()
    if con is None:
        return result
    try:
        rows = _stored_edges_since(con, anchor, types, cutoff)
        # The statement arm (P8): the agent's typed statements touching the anchor with stated_at >=
        # cutoff, surfaced with stated_at in place of first_seen (a judgment accreting is an event).
        rows += _statements_since(anchor, types, cutoff)

        def _row_epoch(e: dict) -> float:
            # a stored row carries first_seen (epoch); a statement row carries stated_at (ISO).
            if e.get("first_seen") is not None:
                return float(e.get("first_seen") or 0.0)
            return _stated_at_epoch(e.get("stated_at") or "") or 0.0

        # Deterministic order: newest accretion first, then the edge tuple for a stable tie-break.
        rows.sort(key=lambda e: (-_row_epoch(e),
                                 e.get("src") or "", e.get("dst") or "",
                                 e.get("type") or "", e.get("method") or ""))
        # Collect the endpoint node set (anchor + every edge endpoint), hydrate, then apply the
        # SAME mechanical max_nodes truncation neighborhood uses; drop edges whose endpoints were cut.
        node_ids: set = {anchor}
        for e in rows:
            node_ids.add(e["src"])
            node_ids.add(e["dst"])
        nodes = _hydrate_nodes(con, node_ids, anchor)
        capped = len(nodes) > max_nodes
        if capped:
            nodes = _truncate_nodes(con, nodes, max_nodes, anchor)
            kept_ids = {n["id"] for n in nodes}
            rows = [e for e in rows if e["src"] in kept_ids and e["dst"] in kept_ids]
        result["edges"] = rows
        result["nodes"] = nodes
        result["capped"] = capped
    except Exception as exc:  # noqa: BLE001 — fail-open: a since failure never breaks the caller
        logger.debug("graph.since failed: %s", exc)
        return {"edges": [], "nodes": [], "since": since_iso, "capped": False,
                "truncation": "recency-then-degree"}
    return result


# ── similar: the alignment CANDIDATES view (design "P5 shipped", the P5 sketch overturned) ────────
# The earlier P5 sketch (a tap MINTING align:embed same_as candidate edges, exploratory-visible) is
# overturned by two of this design's own principles, and the shipped form is a ZERO-WRITE view:
#   (1) Durability: storing embedding neighbors freezes ONE model's judgment into the wall (the store
#       must stay non-thinking so it appreciates as models improve), so candidates are DERIVED at
#       query time from the live vec index, never stored — a model upgrade upgrades every future
#       answer; nothing rots.
#   (2) The razor: "similar" vs "same" always admits a reasoned objection (it is a JUDGMENT), so NO
#       collapse policy may include align:embed, not even exploratory (embedding proximity in a
#       collapse would fabricate identity out of topicality, corrupting voices). The ladder is:
#       similar PROPOSES (top-k by RANK) -> the agent verifies -> penumbra_ruling records -> the working
#       policy collapses. The method is the epistemic unit, carried to its end: align:embed exists
#       ONLY as a proposal label, never as a stored edge method.

@graph_view
def similar(anchor: str, k: int = 10) -> dict:
    """Vector-NEAREST doc CANDIDATES for an anchor doc — PROPOSALS from embedding proximity, never
    collapsed by any policy. ``anchor`` must be a ``doc:{source}:{source_id}`` id of a doc that has an
    EMBEDDED vector in EITHER store: an INDEXED doc (the recall ``vec`` matrix) OR a THIN doc whose
    title was embedded as the writer caught up (``vec_thin``, P7). Reuses the anchor's OWN stored
    vector by its identity (never re-embeds), ranks all other vectors across the UNION of both stores
    by cosine, and takes top-k BY RANK (k is a resource budget like max_nodes, NEVER a similarity
    threshold: rank, never a score cutoff — the RRF discipline).

    Returns ``{anchor, candidates: [{id, kind: "document", label: title, rank: 1..k}], method:
    "align:embed", note, coverage, capped}``. A candidate may be an indexed doc OR a thin doc (its
    ``id`` is the thin node id, its label the thin row's title) — so similar now covers the WHOLE
    perception history, exactly the cross-boundary pairs it exists for (an indexed Chinese post vs a
    thin arXiv original). Deliberately NO cosine scores (rank is the honest unit; a score invites
    pseudo-precision) and NO edges (candidates are a listing, not graph structure). An anchor that is
    NOT a ``doc:`` id, or has NO vector in EITHER store (un-embedded yet), returns ``{"error": ...}``
    naming the real condition. Fail-open to an empty candidate list on store failure. This view WRITES
    NOTHING (the P1 zero-new-writes move, repeated).

    DELIBERATE NON-GOAL (P7): vec_thin does NOT feed penumbra_search's recall arm — that fold is a separate
    decision with its own dogfood. similar (and future P5 consumers) read vec_thin; search does not."""
    coverage = "docs with an embedded title (indexed vec OR thin vec_thin)"
    anchor = (anchor or "").strip()
    if not anchor.startswith("doc:"):
        return {"error": f"similar needs a doc:{{source}}:{{source_id}} anchor ({coverage}); "
                         f"got {anchor!r}"}
    rest = anchor[len("doc:"):]
    source, _, source_id = rest.partition(":")
    if not source or not source_id:
        return {"error": f"malformed doc anchor {anchor!r} (expected doc:{{source}}:{{source_id}})"}
    k = max(1, int(k))
    try:
        # Resolve the anchor vector from EITHER store (docs vec by rowid, else vec_thin by node id).
        resolved = store.similar_anchor(source, source_id)
        if resolved is None:
            return {"error": f"no embedding for this doc yet ({anchor}): thin rows embed as the "
                             f"writer catches up, so a just-seen doc may not be embedded yet; "
                             f"coverage is {coverage}"}
        anchor_vec, anchor_nid = resolved
        # +1 over-fetch so capped is a STRICT truncation test (the find idiom): a full page whose
        # (k+1)th neighbor does not exist is COMPLETE, not capped (no false "more exist" flag).
        hits = store.similar_neighbors(anchor_vec, anchor_nid, k + 1)
    except Exception as exc:  # noqa: BLE001 — fail-open: a similar failure never breaks the caller
        logger.debug("graph.similar failed: %s", exc)
        hits = []
    capped = len(hits or []) > k
    candidates: list[dict] = []
    for i, (c_nid, c_title) in enumerate((hits or [])[:k]):
        candidates.append({"id": c_nid, "kind": "document", "label": c_title, "rank": i + 1})
    return {
        "anchor": anchor,
        "candidates": candidates,
        "method": "align:embed",
        "note": ("candidates are PROPOSALS from embedding proximity, never collapsed by any policy; "
                 "verify, then record a ruling via penumbra_ruling"),
        "coverage": coverage,
        "capped": capped,
    }


def _node_kind(node_id: str) -> str:
    """Best-effort kind from an id's namespace prefix (docs are virtual, so never in graph_nodes)."""
    prefix = node_id.split(":", 1)[0] if ":" in node_id else ""
    known = {"doc": "document", "work": "work", "person": "person", "inst": "institution",
             "venue": "venue", "topic": "topic", "source": "source", "sensor": "sensor",
             "inv": "investigation", "claim": "claim", "gap": "gap"}
    return known.get(prefix, prefix or "unknown")


def _hydrate_nodes(con, ids: set, anchor: str) -> list[dict]:
    """Attach {id, kind, label} for a set of node ids: entity labels from graph_nodes, document
    labels (titles) from the docs table, a ``{kind}:label:{x}`` id's SELF-DESCRIBED label read out of
    the id when no row exists (P8), everything else labelled None. Fail-open per source."""
    labels: dict = {}
    kinds: dict = {}
    id_list = list(ids)
    # Entity nodes.
    try:
        marks = ",".join("?" * len(id_list)) if id_list else ""
        if marks:
            for nid, nkind, nlabel in con.execute(
                f"SELECT id, kind, label FROM graph_nodes WHERE id IN ({marks})", id_list
            ).fetchall():
                labels[nid] = nlabel
                kinds[nid] = nkind
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph._hydrate_nodes entity failed: %s", exc)
    # Virtual document nodes (title as label).
    for nid in id_list:
        if nid in labels or not nid.startswith("doc:"):
            continue
        rest = nid[len("doc:"):]
        source, _, source_id = rest.partition(":")
        if not source or not source_id:
            continue
        try:
            row = con.execute(
                "SELECT title FROM docs WHERE source = ? AND source_id = ?",
                (source, source_id),
            ).fetchone()
            if row:
                labels[nid] = row[0]
        except Exception as exc:  # noqa: BLE001
            logger.debug("graph._hydrate_nodes doc failed: %s", exc)
    out: list[dict] = []
    for nid in id_list:
        # A ``{kind}:label:{x}`` id with no graph_nodes row is SELF-DESCRIBING (label = x): read the
        # label straight out of the id (P8 — statement endpoints are often these never-minted ids).
        label = labels.get(nid)
        if label is None:
            label = _id_self_label(nid)
        out.append({"id": nid, "kind": kinds.get(nid) or _node_kind(nid), "label": label})
    return out


def _node_recency(con, node_id: str) -> float:
    """last_seen for a node (indexed doc from docs, thin doc / entity from graph_nodes); 0.0 when
    unknown. Used as the PRIMARY truncation key so a capped view drops the least-recently-seen first.
    An indexed doc lives in docs; a thin doc node lives in graph_nodes — so a doc: id falls back to
    graph_nodes when it is not an indexed row (else thin nodes would always read as recency 0.0)."""
    try:
        if node_id.startswith("doc:"):
            rest = node_id[len("doc:"):]
            source, _, source_id = rest.partition(":")
            row = con.execute(
                "SELECT last_seen FROM docs WHERE source = ? AND source_id = ?",
                (source, source_id),
            ).fetchone()
            if row is None:  # not indexed -> a thin document node in graph_nodes
                row = con.execute(
                    "SELECT last_seen FROM graph_nodes WHERE id = ?", (node_id,)
                ).fetchone()
        else:
            row = con.execute(
                "SELECT last_seen FROM graph_nodes WHERE id = ?", (node_id,)
            ).fetchone()
        if row and row[0] is not None:
            return float(row[0])
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph._node_recency failed: %s", exc)
    return 0.0


def _truncate_nodes(con, nodes: list[dict], max_nodes: int, anchor: str) -> list[dict]:
    """Mechanical ``recency-then-degree`` truncation: keep the anchor, then rank the rest by
    last_seen DESC, breaking ties by degree (how many kept edges touch a node is approximated by
    frequency in the node list order). This makes a capped view honest — it never reads as complete
    and the drop order is deterministic, not arbitrary."""
    if len(nodes) <= max_nodes:
        return nodes
    anchor_nodes = [n for n in nodes if n["id"] == anchor]
    rest = [n for n in nodes if n["id"] != anchor]
    # recency primary; the list already carries discovery order which stands in for degree tie-break.
    order_index = {n["id"]: i for i, n in enumerate(rest)}
    rest.sort(key=lambda n: (_node_recency(con, n["id"]), -order_index[n["id"]]), reverse=True)
    keep = anchor_nodes + rest
    return keep[:max_nodes]


# ── The open ABI: describe + dispatch, both DERIVED from the registry (design P6) ─────────────────
# describe_views is the runtime self-description (a no-view call returns it, so the surface is
# self-describing and future views appear WITHOUT a client restart). dispatch_view is the ONE
# mechanical gate: unknown view, per-view arg validation (against the view's OWN signature), lenient
# coercion mirroring the server's Lenient* types, then a fail-open call. The tool schema upstream is
# now (view, args) and FROZEN: adding a view is data growth here, never schema growth there.


def describe_views() -> dict:
    """The live view catalog, DERIVED from the registry via ``inspect``: per view, its parameters
    (name, whether required, default) plus its docstring's FIRST LINE (the same first-line convention
    _PENUMBRA_VERBS uses). This is what a no-view dispatch returns, so the surface self-describes."""
    out: dict = {}
    for name, fn in _VIEWS.items():
        sig = inspect.signature(fn)
        params: list[dict] = []
        for pname, p in sig.parameters.items():
            required = p.default is inspect.Parameter.empty
            params.append({
                "name": pname,
                "required": required,
                "default": None if required else p.default,
            })
        first_line = ((getattr(fn, "__doc__", "") or "").strip().splitlines() or [""])[0]
        out[name] = {"params": params, "doc": first_line}
    return out


def _coerce_arg(value, param: "inspect.Parameter"):
    """Lenient coercion for ONE arg, mirroring the server's Lenient* types (it reuses the very
    functions behind LenientInt / LenientBool): a str that parses as int for an int-typed/int-defaulted
    param becomes int; a str/0/1 for a bool-typed/bool-defaulted param becomes bool; everything else
    (lists, strings, already-correct values) passes through unchanged. bool is checked BEFORE int
    because ``bool`` is a subclass of ``int`` in Python (a bool default must not read as an int)."""
    from penumbra.core.normalize import _coerce_bool, _coerce_int
    ann = param.annotation
    default = param.default
    is_bool = ann is bool or isinstance(default, bool)
    is_int = (ann is int) or (isinstance(default, int) and not isinstance(default, bool))
    if is_bool:
        return _coerce_bool(value)
    if is_int:
        return _coerce_int(value)
    return value


def dispatch_view(view: str, args: Optional[dict] = None) -> dict:
    """The ONE mechanical gate behind penumbra_graph's frozen ``(view, args)`` ABI.

    - EMPTY view -> the live view catalog (``{"views": describe_views(), "note": ...}``): a no-view
      call is self-describing, not an error.
    - UNKNOWN view -> ``{"error": "unknown view X; valid: ...", "views": describe_views()}`` (the
      error names every valid view; the catalog rides along so one round-trip teaches the surface).
    - ARG VALIDATION against the view function's OWN signature: an unexpected key -> an error naming
      the view's real params and the unexpected ones; a missing REQUIRED param -> an error naming it.
      Values are leniently coerced (int / bool mirroring the server's Lenient* types; lists and
      strings pass through). Depth / max_nodes caps STAY inside the view functions where they live.
    - Then ``fn(**args)``; any exception -> a fail-open error dict (the same contract the tool had).
    """
    v = (view or "").strip().lower()
    if not v:
        return {"views": describe_views(),
                "note": "pass view=<name> with args={...}; this is the live view catalog"}
    fn = _VIEWS.get(v)
    if fn is None:
        valid = " | ".join(sorted(_VIEWS.keys()))
        return {"error": f"unknown view {view!r}; valid: {valid}", "views": describe_views()}

    call_args = dict(args or {})
    sig = inspect.signature(fn)
    real_params = list(sig.parameters.keys())
    unexpected = [k for k in call_args if k not in sig.parameters]
    if unexpected:
        return {"error": f"view {v} takes {real_params}; got unexpected {unexpected}"}
    missing = [pname for pname, p in sig.parameters.items()
               if p.default is inspect.Parameter.empty and pname not in call_args]
    if missing:
        return {"error": f"view {v} requires {missing} (takes {real_params})"}

    coerced: dict = {}
    for pname, val in call_args.items():
        coerced[pname] = _coerce_arg(val, sig.parameters[pname])

    try:
        return fn(**coerced)
    except Exception as exc:  # noqa: BLE001 — a graph failure NEVER breaks the caller (fail-open)
        return {"error": f"graph {v} failed: {str(exc)[:300]}"}
