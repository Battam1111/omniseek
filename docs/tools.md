# Tools

<sub>[penumbra](../README.md)&nbsp;·&nbsp;[Configuration](configuration.md)&nbsp;·&nbsp;**Tools**&nbsp;·&nbsp;[Patterns](patterns.md)&nbsp;·&nbsp;[Walled sources](walled-sources.md)&nbsp;·&nbsp;[Brand](BRAND.md)</sub>

Penumbra exposes its capability as a small family of MCP verbs: one verb per irreducible
intent, parameters instead of tool sprawl. The authoritative list is whatever the server
registers; `penumbra_sources()` returns the live capability index at runtime. Start with
**`penumbra_search`** for almost everything.

Two axes are shared by the retrieval verbs: `wait_s` (the patience budget) and `staleness`
(`fresh` | `cached_ok` | `cache_only`).

---

## The verbs

| Tool | What it does |
|------|--------------|
| `penumbra_sources` | Orient FIRST: the live catalog with each source's domain, region, and access tier, plus the capability index. `check_health=True` adds per-source liveness and a system block. |
| `penumbra_search` | **The default.** Dedup + rank across sources into one cross-lingual list. `raw=True` returns per-source buckets; `raw=True` with exactly ONE named source is the **drill idiom**: fetch that source unbounded with `full=True` whole content (the escape hatch for slow or walled sources a broad sweep would drop). |
| `penumbra_read` | Text from anything: a URL (adapter-normalized) or a document file (pptx / docx / xlsx / pdf / txt / md / csv). Auto-routes. |
| `penumbra_view` | SEE with your own vision: a document's embedded images, arbitrary image URLs, or video frames. Auto-routes on the target (`kind=` overrides). |
| `penumbra_transcribe` | Transcribe the spoken content of a video, podcast, or audio URL. |
| `penumbra_gather` | Run several read-only calls in ONE parallel round-trip; stragglers keep warming past `wait_s` for a later `staleness="cache_only"` pickup. |
| `penumbra_graph` | The memory of relations: `find` (name → node ids) → `stats` → `neighborhood` → `between` (bounded connection paths between two anchors) → `voices` (collapse a doc set to distinct upstream voices: the independence counter; docs with zero evidence land in `unresolved`, never counted) → `since` (the accretion log: what accrued around a node after a date, tier + method shown, no collapsing) → `similar` (vector-nearest doc candidates for an anchor doc, by rank, no scores: proposals only, never collapsed by any policy; ratify with `penumbra_ruling`). Identity is an evidence-carrying edge; collapse policies are `conservative` \| `working` \| `exploratory`. |
| `penumbra_ruling` | Record / list / retract your identity rulings (`same` \| `not_same` on a node pair): the one judgment channel the graph's `working` policy applies. The pair is the key; re-creating a pair replaces the prior verdict. Penumbra stores your judgment, it never makes one. |
| `penumbra_sensor` | Standing queries with novelty detection: `action=create/list/delete/run`, optional `notify` for push on new results. |

## Scholarly depth

| Tool | What it does |
|------|--------------|
| `penumbra_field_skeleton` | Assemble a research field's citation neighborhood for your agent to map. |
| `penumbra_paper_recommend` | Semantically similar papers to a seed, beyond keyword search. |
| `penumbra_paper_enrich` | Open-access PDF, retraction flags, and citation counts for specific papers. |
| `penumbra_resolve_identity` | Resolve a person's name to candidate author ids (the front door for the relations tools). |
| `penumbra_coauthors` | Reconstruct the co-authorship layer of a network from public records. |
| `penumbra_institution_cohort` | Reconstruct who actively publishes at a lab, department, or institution. |

## The curator

The curator turns source acquisition into a reviewable loop: it gathers neutral evidence
mechanically and never renders a verdict; the agent judges, and only the operator sanctions an
irreversible change. Two dispatchers carry the whole protocol:

| Tool | What it does |
|------|--------------|
| `penumbra_curator_view` | Read-only: `what="queue"` (the candidate backlog), `"packet"` (a candidate's evidence), `"audit"` (the per-source dossier: yield, ingest, coverage, safety flags). |
| `penumbra_curator_act` | The verbs: `submit`, `probe`, `decide`, `apply_live`, `rollback_live`, `stage_commit`, `retire_live`, `rollback_retire`, `source_verdict`. Every safety gate lives inside the verb (a hard red-line refuses an admit; a protected source refuses a prune). |

## The prompt

| Prompt | What it does |
|--------|--------------|
| `investigate(target, shape)` | A parameterized investigation recipe; `shape` = `person` \| `lab` \| `field` \| `product` \| `chase`. |

---

<div align="center"><sub><a href="../README.md">← back to the README</a></sub></div>
