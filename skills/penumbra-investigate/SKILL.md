# /penumbra-investigate

Deep investigation METHODOLOGY for Penumbra. Teaches judgment patterns for composing
retrieval primitives into multi-step evidence gathering. Tool facts (signals, handles,
_meta, evidence graph schema, walled source mechanics) are in the server instructions,
not here. This skill teaches HOW to use those facts well.

## 1. The 3-turn rhythm (sweep, zoom, structure)

Every deep investigation follows the same beat:

**WAVE 1 (sweep)**: fire several independent tools in parallel via `penumbra_gather`.
A broad `penumbra_search` plus any tools whose results you need to make your
first judgment call (e.g. `penumbra_resolve_identity` for a person question,
`penumbra_field_skeleton` for a field question). One round-trip, all results at once.

**Judge**: read Phase A signals, handles, and _meta (per server instructions sections 3-5).
Decide what to zoom on: which walled sources from excluded_relevant to chase, which
identities to map, which papers to enrich, which talks to transcribe.

**WAVE 2 (zoom)**: fire follow-ups via `penumbra_gather`, informed by WAVE 1 signals.

**Structure**: build an EvidenceGraph from your findings (per server instructions section 6).

## 2. Investigation starting points

These are STARTING POINTS. Adapt, skip, extend based on what you find.

### Person due-diligence
WAVE 1: `penumbra_search` + `penumbra_resolve_identity`.
WAVE 2: `penumbra_coauthors` + `penumbra_paper_enrich` + walled chase + `penumbra_transcribe`.
Key judgment: coauthor network reveals structural position; walled sources reveal candid views.

### Lab / research group evaluation
WAVE 1: `penumbra_search` + `penumbra_institution_cohort`.
WAVE 2: `penumbra_field_skeleton` + `penumbra_coauthors(top PI)` + `penumbra_paper_enrich` + walled chase.
Key judgment: cohort shows who publishes there; citation neighborhood shows where they sit.

### Field / topic mapping
WAVE 1: `penumbra_search` + `penumbra_field_skeleton`.
WAVE 2: `penumbra_paper_recommend` + `penumbra_paper_enrich` + `penumbra_transcribe`.
Key judgment: identify consensus core (high in_degree), frontier (recent, citing core), controversy.

### Product / tool / company assessment
WAVE 1: `penumbra_search(query="<product> review")` + `penumbra_search(query="<product> alternative")`.
WAVE 2: walled community sources + `penumbra_read` for official + critical pages.
Key judgment: corroboration + also_in, conflicts, source_diversity (vendor-only = one-sided).

## 3. Saturation chase (walled-source depth pursuit)

After any broad search, read `_meta.excluded_relevant`. Each entry has overlap (query-token
match count) and a sources=[...] re-run hint. Chase when: domain matches + overlap >= 2 +
budget allows. Top 2-3, not all.

Fire-then-collect (per server instructions section 7) for parallel walled retrieval.
If a xiaohongshu note URL appears, `penumbra_read(url)` retrieves full note + comment thread.

## 4. Budget discipline

- Surface (fast): 1 gather with 2-3 tools
- Standard: 2 gathers (sweep + zoom)
- Deep: 2-3 gathers + transcription
- Max 3 `penumbra_search` per gather (each fans out to many sources)
- Failed/empty/timed-out calls still count; do NOT infinite-retry

## 5. Triangulation and disconfirmation

- **Count INDEPENDENT corroboration**: corroboration counts source NAMES, not backends.
  Cross-reference also_in to dedup mirrors.
- **Flag conflicts, don't average**: when _meta.conflicts appears, present the divergence,
  don't pick a winner.
- **Stamp freshness**: carry each key fact's freshness_days.
- **Reverse the query**: before acting on a claim, search the INVERSE.
- **Reference-class base rate**: for "join this lab / take this path", estimate the denominator
  with `penumbra_institution_cohort` before trusting anecdote.

## 6. Evidence graph construction

Build an EvidenceGraph (schema in server instructions section 6):
- Extract claims conservatively (distinct, testable assertions with scope).
- Require sourced_from edges for every Claim.
- Use Phase A signals to inform edge weights and Gap identification.
- Check graph quality: orphan Documents, unsupported Claims, open contradicts, critical Gaps.

## 7. Close with a gap-ledger

End every investigation with an explicit accounting of what you did NOT get:
sources used, queries sent, and the gap-ledger (zero-hit facets, unchased excluded_relevant,
timed-out sources). These are VISIBLE gaps, never silently backfilled.
