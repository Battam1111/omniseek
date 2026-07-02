# Changelog

All notable changes to Penumbra are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is SemVer.

## [Unreleased]

### Changed (BREAKING: the tool surface was re-derived from first principles)

- 34 tools + 5 prompts fused into **15 tools + 1 prompt** (one verb per irreducible intent):
  `penumbra_sources` (absorbs `penumbra_list_sources` + `penumbra_health_check`),
  `penumbra_search` (absorbs `penumbra_search_ranked`, the old raw `penumbra_search`, and
  `penumbra_fetch` as the drill idiom `sources=["<one>"], raw=True, full=True`),
  `penumbra_read` (absorbs `penumbra_add_url` + `penumbra_read_document`),
  `penumbra_view` (absorbs the three vision tools), `penumbra_sensor` (absorbs the four
  sensor CRUD tools), `penumbra_curator_view` + `penumbra_curator_act` (absorb the twelve
  curator tools; every safety gate preserved), and `investigate(target, shape)` (absorbs the
  five investigation prompts).
- Time semantics unified to two axes on the retrieval verbs: `wait_s` (patience budget,
  replaces `deadline_s` / `timeout_s` / `return_after_s`) and `staleness`
  (`fresh` | `cached_ok` | `cache_only`, replaces the `fresh` / `cache_only` booleans).
- `independence_score` removed: read the raw dedup facts (`corroboration`, `also_in`,
  `merge_basis`) and judge; signal conflicts now detected inside the dedup merge (same work,
  different sources) instead of across unrelated results.

### Added

- **The unified graph (P1)**: `penumbra_graph` with `find` / `stats` / `neighborhood` views
  over the relation memory that rides the recall index; document nodes and same-work identity
  edges are live from day one, entity kinds fill as later phases ship. Identity is an
  evidence-carrying edge under `conservative` | `working` | `exploratory` collapse policies.
- Sensors: optional `notify` flag pushes on new results (runner-side).

- PyPI publish (`pip install penumbra-mcp`) planned.

## [0.1.0]

### Added

- Initial public release: a self-hosted deep-retrieval MCP server.
- Around 200 curated sources across 157 independent upstreams, classified by access tier (free / keyed / walled / circumvention).
- MCP tool surface: `penumbra_search`, `penumbra_search_ranked`, `penumbra_fetch`, `penumbra_list_sources`, `penumbra_add_url`, paper + citation tools, people + organization tools, document + vision reading, audio transcription, health check, and a self-iterating source curator.
- Token-gated loopback HTTP transport; SSRF guard; sandboxed document inbox.
- Apache-clean core install; opt-in extras (`pdf` / `asr` / `recall` / `ocr` / `walled`).
- One-command Docker self-host and a non-Docker bootstrap path.
