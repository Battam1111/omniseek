"""Evidence graph: the canonical schema for structured evidential reasoning.

The eye provides ATOMS (documents + Phase A signals + handles). The agent builds
STRUCTURE (claims + evidential links + gaps). This module defines the SHAPE of that
structure as TypedDicts. Zero functions, zero logic. The agent populates it; the
server instructions describe how to read it; the skill teaches how to build it well.

Node types:
  Document  -- an atomic piece of evidence from the eye (mechanical).
  Claim     -- an assertion extracted by the agent (judgment).
  Gap       -- an identified absence in evidence (judgment).

Edge types:
  sourced_from  -- Claim -> Document (provenance).
  supports      -- Doc/Claim -> Claim (positive evidence).
  contradicts   -- Doc/Claim -> Claim (negative evidence).
  depends_on    -- Claim -> Claim (logical dependency).
  addresses     -- Doc/Claim -> Gap (partially fills an absence).
"""

from __future__ import annotations

from typing import TypedDict, Optional, Literal


class DocumentNode(TypedDict, total=False):
    """A retrieved document from the eye. Mechanical: created from eye output, never invented."""
    id: str                          # "doc:{source}:{source_id}"
    type: Literal["document"]
    source: str
    source_id: str
    url: str
    title: str
    date: Optional[str]
    independence_score: Optional[float]
    freshness_class: Optional[str]
    relevance_hook: Optional[str]
    handles: Optional[dict]          # transcribable / captioned / enrichable / has_comments


class ClaimNode(TypedDict, total=False):
    """An assertion extracted by the agent from one or more documents."""
    id: str                          # "claim:{n}"
    type: Literal["claim"]
    statement: str                   # natural language
    confidence: str                  # HIGH / MED / LOW / VERY_LOW / UNKNOWN
    scope: str                       # under what conditions this holds
    source_count: int                # independent sources supporting this
    as_of: Optional[str]             # date of the evidence


class GapNode(TypedDict, total=False):
    """An identified absence in evidence, found by the agent."""
    id: str                          # "gap:{n}"
    type: Literal["gap"]
    description: str
    dimension: str                   # which perspective / aspect is absent
    severity: str                    # critical / important / minor
    suggested_queries: list[str]


EvidenceNode = DocumentNode | ClaimNode | GapNode


class EvidenceEdge(TypedDict, total=False):
    """A directed relationship between two evidence nodes."""
    source: str                      # source node id
    target: str                      # target node id
    type: str                        # sourced_from | supports | contradicts | depends_on | addresses
    weight: Optional[float]          # 0.0 to 1.0, strength of relationship
    note: Optional[str]              # why this relationship exists


class ManifestEntry(TypedDict, total=False):
    """One tool call in the investigation provenance trail."""
    tool: str
    query: str
    source_name: Optional[str]
    status: str                      # ok | empty | timed_out | errored | excluded
    doc_count: int
    elapsed_s: float


class EvidenceGraph(TypedDict, total=False):
    """The top-level evidence graph for one investigation."""
    id: str
    query: str                       # the question being investigated
    nodes: list[EvidenceNode]
    edges: list[EvidenceEdge]
    manifest: list[ManifestEntry]
    created_at: str
