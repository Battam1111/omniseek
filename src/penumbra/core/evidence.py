"""EvidencePackage schema: the canonical output structure for a multi-step investigation.

A pure TypedDict (zero runtime logic, zero functions). The agent structures its gathered
evidence into this shape; the eye never constructs it (the razor: the eye measures,
the agent packages). Importable by programmatic consumers for type-checking / IDE support:
``from penumbra.core.evidence import EvidencePackage``.
"""

from __future__ import annotations

from typing import TypedDict


class GapEntry(TypedDict, total=False):
    source: str
    reason: str   # excluded | timed_out | errored | empty | not_attempted
    action: str   # what the agent COULD do, e.g. "name it: sources=['zhihu']"


class ManifestEntry(TypedDict, total=False):
    source: str
    query: str
    status: str   # ok | empty | timed_out | errored | excluded
    doc_count: int
    elapsed_s: float


class EvidencePackage(TypedDict, total=False):
    question: str
    surface_findings: list[dict]
    deep_findings: list[dict]
    structural_data: dict
    audio_findings: list[dict]
    gaps: list[GapEntry]
    source_manifest: list[ManifestEntry]
    confidence_notes: list[str]
