# Tools

<sub>[penumbra](../README.md)&nbsp;·&nbsp;[Configuration](configuration.md)&nbsp;·&nbsp;**Tools**&nbsp;·&nbsp;[Patterns](patterns.md)&nbsp;·&nbsp;[Walled sources](walled-sources.md)&nbsp;·&nbsp;[Brand](BRAND.md)</sub>

Penumbra exposes its capability as a family of MCP tools. The authoritative list is whatever the
server registers; `penumbra_list_sources()` returns the live capability index at runtime. Start with
**`penumbra_search_ranked`** for almost everything, and reach for the rest when you have a specific
target.

---

## Search and routing

| Tool | What it does |
|------|--------------|
| `penumbra_search_ranked` | Search across sources, deduped and ranked into one list. **The default** for best / latest on X. |
| `penumbra_search` | Search many sources in parallel, returned as per-source buckets (uncollapsed). |
| `penumbra_fetch` | Drill ONE named source, unbounded (it waits for slow or walled sources). |
| `penumbra_list_sources` | List and route: the live catalog with each source's domain, region, and access tier. |
| `penumbra_add_url` | Fetch and normalize a single URL into a document. |

## Papers and citations

| Tool | What it does |
|------|--------------|
| `penumbra_field_skeleton` | Assemble a research field's citation neighborhood for your agent to map. |
| `penumbra_paper_recommend` | Semantically similar papers to a seed, beyond keyword search. |
| `penumbra_paper_enrich` | Open-access PDF, retraction flags, and citation counts for specific papers. |

## People and organizations

| Tool | What it does |
|------|--------------|
| `penumbra_resolve_identity` | Resolve a person's name to candidate author ids (the front door for the relations tools). |
| `penumbra_coauthors` | Reconstruct the co-authorship layer of a network from public records. |
| `penumbra_institution_cohort` | Reconstruct who actively publishes at a lab, department, or institution. |

## Documents and vision

| Tool | What it does |
|------|--------------|
| `penumbra_read_document` | Read one document file (pptx / docx / xlsx / pdf / txt / md / csv) as text. |
| `penumbra_view_doc_images` | See a document's embedded images with your own vision. |
| `penumbra_view_images` | See arbitrary image URLs in-band. |
| `penumbra_view_video_frames` | See the frames inside a video. |

## Audio

| Tool | What it does |
|------|--------------|
| `penumbra_transcribe` | Transcribe the spoken content of a video, podcast, or audio URL. |

## Health and curation

| Tool | What it does |
|------|--------------|
| `penumbra_health_check` | Probe connectivity against every registered source. |
| `penumbra_curator_*` | The self-iterating source-acquisition subsystem (below). |

<details>
<summary><b>The curator subsystem</b> (advanced: how the catalog grows itself)</summary>

<br>

The curator turns source acquisition into a reviewable loop: it gathers neutral evidence
mechanically and never renders a verdict; the agent judges, and only the operator sanctions an
irreversible change.

| Tool | What it does |
|------|--------------|
| `penumbra_curator_submit` | Submit a candidate source for admission review (durable backlog). |
| `penumbra_curator_probe` | Run the mechanical evidence-gatherers and persist the packet. |
| `penumbra_curator_packet` | Return a candidate's evidence packet (a fresh agent picks it up cold). |
| `penumbra_curator_list` | List the candidate backlog, optionally filtered by state. |
| `penumbra_curator_decide` | Record the agent's admit / watch / reject verdict. |
| `penumbra_curator_apply_live` | One-tap live admit for the reversible rss-safe subclass. |
| `penumbra_curator_rollback_live` | Full revert of a live-applied source. |
| `penumbra_curator_stage_commit` | Prepare a ready-to-commit row for the non-auto subclass. |
| `penumbra_curator_audit` | Read-only per-source audit dossier (yield, ingest, coverage, safety flags). |
| `penumbra_curator_source_verdict` | Record the agent's keep / watch / prune for an existing source. |
| `penumbra_curator_retire_live` | One-tap prune (the live half reversible, the durable half staged). |
| `penumbra_curator_rollback_retire` | Roll back a runtime retire. |

</details>

---

<div align="center"><sub><a href="../README.md">← back to the README</a></sub></div>
