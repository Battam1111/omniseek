# /penumbra-investigate

Deep investigation patterns for Penumbra: how to compose the retrieval primitives
into a multi-step evidence-gathering workflow. The patterns here are INTELLIGENCE
(your judgment, applied through Penumbra's tools), not hardcoded pipelines.

Penumbra is a RETRIEVAL LAYER: it hands you raw, provenance-stamped EVIDENCE. You
judge. These patterns teach you how to gather that evidence efficiently.

## 1. The 3-turn rhythm (sweep, zoom, structure)

Every deep investigation follows the same beat:

**WAVE 1 (sweep)**: fire several independent tools in parallel via `penumbra_gather`.
A broad `penumbra_search_ranked` plus any tools whose results you need to make your
first judgment call (e.g. `penumbra_resolve_identity` for a person question,
`penumbra_field_skeleton` for a field question). One round-trip, all results at once.

**Judge**: read the results. Key signals to scan on each document:
- `metadata.independence_score`: 0 = singleton, 0.3+ = corroborated by multiple
  independent sources. Title-merge results get a 0.7x discount (may be coincidental).
- `metadata.freshness_days` / `freshness_class`: how old. breaking (<=1d), recent
  (<=7d), current (<=30d), dated (<=1y), archival (>1y). null = no date.
- `metadata.relevance_hook`: one extracted sentence from the doc's own text showing
  why it matched your query. Scan this instead of reading full content.
- `metadata.handles`: affordance detection (pure pattern match, not suggestions):
  - `transcribable`: URLs Penumbra can transcribe (bilibili, xiaoyuzhou, podcasts, audio files).
  - `captioned`: YouTube URLs (captions available, no ASR needed).
  - `enrichable`: DOI / arXiv IDs that `penumbra_paper_enrich` can drill into.
  - `has_comments`: the document carries a comment thread with per-comment IDs for
    provenance citation (e.g. `xiaohongshu_cn:note123#comment-456`).
- `_meta.source_diversity`: which perspective types are present/absent
  (academic / social / audio / walled / news). If a type is absent, consider whether
  it matters for your question.
- `_meta.conflicts`: mechanical divergence flags where the same signal name carries
  different values across sources (e.g. two sources disagree on citation count).
- `_meta.excluded_relevant`: walled/slow sources that thematically match your query
  but were excluded from the broad sweep. Each entry has an `overlap` score (higher
  = more query tokens matched) and a copy-paste `sources=[...]` re-run hint.

**WAVE 2 (zoom)**: based on your judgment, fire follow-ups via `penumbra_gather`:
- Chase the top-overlap excluded_relevant walled sources.
- Run `penumbra_coauthors` on a resolved identity.
- Run `penumbra_paper_enrich` on DOIs/arXiv IDs from handles.enrichable.
- Run `penumbra_transcribe` on a podcast/talk URL from handles.transcribable.
All in parallel. One round-trip.

**Structure**: organize your findings as an EvidencePackage:
```
{
  question: "...",
  surface_findings: [WAVE 1 search results],
  deep_findings: [WAVE 2 walled/enriched results],
  structural_data: {coauthors, field_skeleton, identity, cohort},
  audio_findings: [transcription results],
  gaps: [what you did NOT get + why],
  source_manifest: [every source touched + status],
  confidence_notes: ["all results share one backend", "no social voice", ...]
}
```

## 2. Investigation patterns

These are STARTING POINTS. Adapt, skip, extend based on what you find. The tools
are the same; the patterns differ in which ones to reach for first.

### Person due-diligence (advisor, collaborator, hire)

WAVE 1: `penumbra_gather([`
- `penumbra_search_ranked(query="<name> <field>")`
- `penumbra_resolve_identity(name="<name>")`
`])`

Judge: pick the right identity candidate. Read source_diversity (social voice
present?). Scan excluded_relevant for community sources (zhihu, xiaohongshu,
blind, glassdoor) where practitioners share candid views.

WAVE 2: `penumbra_gather([`
- `penumbra_coauthors(authors=[<resolved_id>])`
- `penumbra_paper_enrich(ids=[<top DOIs from handles.enrichable>])`
- `penumbra_search_ranked(query="<name>", sources=[<top excluded_relevant>], deadline_s=30)`
- `penumbra_transcribe(url=<talk/podcast URL from handles>)` (if found)
`])`

Key judgment: coauthor network reveals advisor, frequent collaborators, and the
person's structural position. Paper enrichment reveals retraction/integrity flags.
Walled social sources reveal what students/employees actually say. The podcast/talk
reveals the person's own voice on their research.

### Lab / research group evaluation

WAVE 1: `penumbra_gather([`
- `penumbra_search_ranked(query="<lab/institution> <field>")`
- `penumbra_institution_cohort(institution="<name>")`
`])`

Judge: who publishes there (cohort). What the lab claims vs what outsiders say
(source_diversity). Spot the top PI(s) for WAVE 2.

WAVE 2: `penumbra_gather([`
- `penumbra_field_skeleton(query="<lab's main topic>")` for citation neighborhood
- `penumbra_coauthors(authors=[<top PI id>])`
- Chase walled sources for student perspectives
`])`

### Field / topic mapping

WAVE 1: `penumbra_gather([`
- `penumbra_search_ranked(query="<field/topic>")`
- `penumbra_field_skeleton(query="<field/topic>")`
`])`

Judge: identify the consensus core (high in_degree nodes), the frontier (recent,
citing core), and any controversy. Scan handles.enrichable for key papers.

WAVE 2: `penumbra_gather([`
- `penumbra_paper_recommend(ids=[<seed papers from skeleton>])`
- `penumbra_paper_enrich(ids=[<frontier papers>])`
- `penumbra_transcribe(url=<conference talk>)` (if found)
`])`

### Product / tool / company assessment

WAVE 1: `penumbra_gather([`
- `penumbra_search_ranked(query="<product> review")`
- `penumbra_search_ranked(query="<product> alternative comparison")`
`])`

Judge: check independence_score (how many independent sources agree), conflicts
(where sources disagree on facts), source_diversity (all vendor pages? no user
voice?). Identify community sources in excluded_relevant.

WAVE 2: `penumbra_gather([`
- Chase excluded_relevant community/walled sources
- `penumbra_add_url(url=<official page>)` for vendor claims
- `penumbra_add_url(url=<critical review>)` for counterpoint
`])`

## 3. Saturation chase (walled-source depth pursuit)

After any broad search, read `_meta.excluded_relevant`. Each entry has:
- `overlap`: how many query tokens matched (higher = more relevant)
- `reason`: why it was excluded (rate-limited, walled, etc.)

**When to chase**: the query's domain matches a walled source AND overlap >= 2
AND your budget allows it. Chase the top 2-3, not all.

**How**:
```
penumbra_gather([
  penumbra_search_ranked(query="...", sources=["zhihu", "xiaohongshu_cn"], deadline_s=30),
])
```
Or use `penumbra_fetch(source="<name>", query="...")` for unbounded single-source drill.

If a xiaohongshu note URL appears in results, `penumbra_add_url(url)` retrieves the
full note body + comment thread. Each comment carries an `id` for provenance citation:
`xiaohongshu_cn:<note_id>#comment-<comment_id>`.

## 4. Budget discipline

Each `penumbra_gather` call runs N tools; each tool internally fans out to many sources.
A reasonable investigation budget:
- Surface (fast, ~15s): 1 gather with 2-3 tools
- Standard (~30s): 2 gathers (sweep + zoom)
- Deep (~60-90s): 2-3 gathers + transcription

Do not call gather with more than 3 `penumbra_search_ranked` calls in one batch
(each fans out to ~90 sources; 3 concurrent = ~270 network requests from one host).

## 5. Triangulation and disconfirmation

Retrieving 10 sources is not knowing; the advantage is in CONNECTING them.

- **Count INDEPENDENT corroboration**: `independence_score` counts source names,
  not backends. ~42 org_watch sources share the OpenAlex backend, so corroboration=5
  from OpenAlex slices is NOT 5 independent upstreams. Cross-reference `also_in`.
- **Flag conflicts, don't average**: when `_meta.conflicts` appears, present the
  divergence (who said what, who is first-party vs reblog), don't pick a winner.
- **Stamp freshness**: carry each key fact's `freshness_days`. "The newest source
  for this claim is 14 months old" is a first-class calibration signal.
- **Reverse the query**: before acting on a claim, search the INVERSE ("risks of X",
  "who regretted X", "X debunked"). Especially for important decisions.
- **Reference-class base rate**: for "join this lab / take this path", use
  `penumbra_institution_cohort` + alumni destinations to estimate the denominator.

## 6. Close with a gap-ledger

End every investigation with an explicit accounting of what you did NOT get:
- Which sources you used, with the query each got
- The gap-ledger: every zero-hit facet, every excluded_relevant source you did NOT
  chase, every timed-out source. These are VISIBLE gaps, never silently backfilled.
- "These dimensions I systematically did not get" is the honest complement to
  "here is what I found."
