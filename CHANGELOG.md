# Changelog

All notable changes to OmniSeek are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is SemVer.

Entries below the rename note predate it and use the project's former name, penumbra; they are
history and are kept as written.

## [0.1.3] - 2026-08-16

### Changed

- **Runtime strings no longer speak the private-era operator.** Source descriptions, drill-only
  rationales, and page-watch notes are strings every client reads; nine of them still carried
  the original operator's name or deployment geography. They are re-authored in a neutral
  curation voice at the engine, the mirror's rename table now neutralizes the whole class, and
  a new residue gate makes it unshippable.
- `docs/sources.md`: the source catalog as a generated document, rebuilt by the sync pipeline
  on every engine update so it cannot rot; linked from the README in all three languages.
- `docs/case-study.md` rebuilt as "The second investigation": verbatim outputs from one real
  session (memory stamps, dedup and citation-conflict surfacing, router coverage honesty, a
  live identity ruling, a standing watch), one diagram, and no claim without evidence shown.

## [0.1.2] - 2026-08-16

### Fixed

- **First search on a fresh install could hang forever.** The ranked-search path imported the
  ranking/recall chain (numpy and the vector stack) lazily, on the event-loop thread, during
  the first call, racing the startup shadow probe's own first imports on a worker thread. On
  Windows under MCP-over-stdio that race deadlocked inside numpy's extension init: the call
  never returned, no deadline applied, nothing was logged. Both entrypoints now warm the
  import chain at boot, so no call path ever pays or races a first import. Surfaced by a
  Cline install test; reproduced on a clean venv (faulthandler stack pinned the exact frames)
  and verified fixed with the same probe (endless hang before, a budgeted return after).

## [0.1.1] - 2026-08-16

### Added

- Official MCP Registry ownership markers, so the server can be published as
  `io.github.Battam1111/omniseek`: an `mcp-name` marker in the README (which is the PyPI long
  description) and the `io.modelcontextprotocol.server.name` OCI label on the GHCR image.
- `server.json` for the registry: PyPI + GHCR packages, streamable HTTP transport at
  `http://localhost:8765/mcp`.

### Changed

- The package root (`src/omniseek/__init__.py`) now carries the product's own description (the
  raw sync shipped the pre-brand internal framing), and the sync script re-authors it from
  `pyproject.toml` on every run, so its `__version__` can never drift from the released version.

## [0.1.0] - 2026-08-16

The first public release.

### Fixed

- **The job scheduler now starts in docker and any plain checkout.** Two deploy-shaped
  assumptions silently killed it for public users: the heartbeat identity insisted on a
  git-archive-substituted commit hash, and the host boot id shelled out to macOS `sysctl`.
  A stranger's `docker compose up` therefore ran with no sensor schedules, no source-health
  lane, no curator tick, announced only by one boot warning. Identity now falls back to a
  deterministic 40-hex fingerprint of the package sources (the strict validator stays for
  the release path), and the boot id falls back to Linux `/proc/sys/kernel/random/boot_id`.
  Same frozen heartbeat schema, no contract bump.
- Pinned `mcp[cli]>=1.0.0,<2`. mcp 2.0 (2026-08) removed `mcp.server.fastmcp`, so a FRESH
  install (docker build, new venv) died at import while every existing venv kept working.
  CI's docker-boot job caught it on the rename push; reproduced red and verified green in a
  clean venv before pinning.

### Changed (brand on runtime surfaces)

- No runtime-visible string speaks the project's pre-brand internal vocabulary anymore:
  server instructions and tool descriptions (strings every MCP client reads) now say
  OmniSeek, the boot log line says OmniSeek, identity errors dropped their old prefix, and
  the outward User-Agent token is `OmniSeek/1.0`. The old wording survives only in code
  comments that never render, and the mirror's residue gate rejects it on any surface a
  reader sees, so it cannot come back.

### Changed

- README and `docs/examples.md` rebuilt around one real investigation (five walls: language,
  login, comment depth, audio, pixels; a policy-change question with no company in the blast
  radius) with verbatim outputs, mermaid diagrams for the chase and the coauthor graph, and a
  full-column demo figure localized per README language (en/zh/ja), light and dark.
- Literal source counts removed from all docs and assets. The catalog grows (and prunes)
  through the curator pipeline, so any hard number is born stale; the server already computes
  live counts at startup, and prose now sells the mechanism instead.
- Root decluttered: `bootstrap.sh` -> `scripts/bootstrap.sh` (now runnable from any CWD),
  `profile.example.json` -> `deploy/profile.example.json`, every reference updated. The
  shipped Claude Code skill is linked from the README instead of sitting undiscovered.

### Changed (BREAKING: the project is renamed penumbra -> OmniSeek)

- Every namespace follows the name: the package is `omniseek`, the tools are `omniseek_*`, the
  env vars are `OMNISEEK_*`, runtime state lives in `~/.omniseek`, the service and compose files
  say `omniseek`. No compatibility aliases: the project has never been public, so there is no one
  to break.
- New visual identity (the deep O; see `docs/BRAND.md`), new README, and a new real-output
  examples page (`docs/examples.md`).

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
- **Retrieval-anchored thin memory (P2.0)**: every retrieved doc from a non-indexed source
  leaves a thin graph row (title + url + fingerprint + external ids, never content), so the
  retrieval history is complete; every search stamps per-doc `seen_before` / `first_seen_at`.
  Walled sources stay out by default (`walled.remember_retrievals`, an operator-privacy line).
- **Academic entities on the wall (P2)**: the citation mapper and paper enrichment become graph
  write taps minting work / person / topic / venue nodes and cites / authored / about /
  published_in edges. Vocabulary is declared per tap (vocabulary-by-minting) and a test
  tripwire bounds actual data to the declared union.
- **Relations taps + the judgment channel (P3)**: the people tools mint person / institution
  nodes and coauthored / affiliated edges (plus same-name split candidates as alignment
  edges); `penumbra_graph` gains `between` (bounded connection paths) and `voices` (the
  independence counter, with an honest `unresolved` bucket); new `penumbra_ruling` tool
  records / lists / retracts identity rulings the `working` policy applies at read time.
- **The event layer (P4)**: sensor runs mint `observed` edges from the run diff (a no-news run
  mints nothing); the dedup conflict detector mints `conflicts` edges carrying the signal name
  and kind (so engagement-count noise is filterable); `penumbra_graph` gains `since`, the
  accretion log around a node (stored edges only, tier and method visible, no collapsing).
- **Alignment candidates as a view, not writes (P5)**: `penumbra_graph` gains `similar`, the
  vector-nearest doc candidates for an anchor doc, derived at query time from the live vector
  index (nothing stored, so an embedding-model upgrade upgrades every answer). Top-k by rank,
  no scores, and no collapse policy ever includes embedding proximity: candidates are
  proposals for your judgment, recorded via `penumbra_ruling`.
- **Sensors run in-process (P6)**: the HTTP service ticks its own scheduler (15 min; `hourly` |
  `daily` | `weekly`, unknown = daily), so sensors run because they exist: no cron to install,
  no separate runner script (the old standalone cron path is deleted; scheduled runs now
  accrete memory exactly like manual ones). Optional `notify` pushes on new results.
- **`penumbra_graph` stable ABI (P6, BREAKING)**: the graph verb is now
  `penumbra_graph(view, args)`: views live in a registry, per-view arguments are validated by
  signature introspection (a wrong argument names the view's real parameters), calling with no
  view returns the live view catalog, and the tool schema never changes again as views grow.
- **Divergence by rank, not gate (P7)**: the same-work signal-conflict detector no longer
  applies a 1.5x threshold; it measures every divergence, ranks by ratio (magnitudes; a sign
  flip or zero-vs-nonzero is unbounded), keeps the top-3 per doc, and carries the ratio in the
  stamp and the `conflicts` edge. What counts as material is the reader's call.
- **Thin rows embed their titles (P7)**: docs from non-indexed sources get title embeddings at
  mint time (plus a bounded self-converging catch-up in the writer's idle cycles), so `similar`
  ranks across the whole retrieval history, not just the indexed subset; `stats` reports the
  coverage gauge (`document_thin_embedded`). The thin vectors never feed search's recall arm.

- **Self-maintenance runs in-process (P9)**: the sensor scheduler generalized into a job
  registry (`every:Ns` | `daily@HH:MM` | `weekly@ddd-HH:MM` | `monthly@D-HH:MM`), and the
  server's own upkeep (source health probing, log rotation, the curator's monthly
  evidence pass, the weekly source audit, an optional digest) now rides it as declarative
  rows with per-job wall-clock budgets and a heartbeat file: no cron to install for any of
  it. Jobs can be toggled per deployment via `profile.json` (`jobs: {"<name>": true|false}`).

- **Any MCP server wraps as a source (P10)**: declarative rows gain `transport: "mcp"` (same
  row table, same field_map / facets / cache / admission vocabulary; `tools/call` instead of
  GET, via a hand-rolled streamable-HTTP client, httpx-only, zero new dependencies). Because a
  wrapped server lands as an ordinary source, every memory mechanism (thin rows, seen_before,
  conflict ratios, similar) applies to it with zero new code, and each wrapped server still
  earns its slot through the admission razor per server. Auth headers, when needed, live in
  `~/.penumbra/credentials/mcp_<name>.json`.
- **Foundry-grade curator packets (P10)**: a candidate submission can carry a working draft
  artifact ({row, fixture, probe_summary}); the packet surfaces it and `stage_commit` renders
  the draft as the ready-to-paste row, so the judge reviews a WORKING source instead of a host
  name. The first row admitted this way ships in the catalog: `context7` (Upstash's live
  library-docs registry; explicit-only, its anonymous quota honestly encoded in the row).

- **Typed relation statements (P8)**: `penumbra_statement` records directed agent judgments
  (`src`, `dst`, free `type`, required `note`, optional provenance `doc`) as declarative state
  beside the identity rulings; they project in `neighborhood` / `between` / `since` under
  `working` and `exploratory`, never `conservative`, and never enter the mechanical store.
  Identity types are refused (that is `penumbra_ruling`'s job), `voices` deliberately ignores
  statements, and endpoints may name entities no source ever minted (label-keyed ids are
  self-describing and findable). Nothing is ever extracted by code: the reading agent judges,
  the graph projects.

- **`_meta` goes lean (P11, shape change)**: search `_meta` now carries only THIS query's
  information. The full excluded-sources reason map (deployment-static, repeated on every
  search) is replaced by `excluded_count`, with the query-aware `excluded_relevant` unchanged
  and the full catalog one call away via the roster's `explicit_only_reason`; the fast/slow
  source name lists become counts inside `progressive` (whose `timed_out` keeps its actionable
  name list, as do `empty` and `truncated`). Also: every ranked result now ALWAYS carries
  `seen_before` (true|false, never absent; a first-time doc reads false with a null
  `first_seen_at`); gather failures caused by a wrong argument now include a hint naming the
  tool's real parameters; walled sources whose payloads carry no timestamp keep `date: null`
  honestly (never approximated).

- PyPI publish planned (it later landed as `pip install omniseek`).

## [0.1.0-alpha] (internal; never published)

### Added

- Initial release: a self-hosted deep-retrieval MCP server.
- A curated source catalog spanning independent upstreams, classified by access tier (free / keyed / walled / circumvention).
- MCP tool surface: `penumbra_search`, `penumbra_search_ranked`, `penumbra_fetch`, `penumbra_list_sources`, `penumbra_add_url`, paper + citation tools, people + organization tools, document + vision reading, audio transcription, health check, and a self-iterating source curator.
- Token-gated loopback HTTP transport; SSRF guard; sandboxed document inbox.
- Apache-clean core install; opt-in extras (`pdf` / `asr` / `recall` / `ocr` / `walled`).
- One-command Docker self-host and a non-Docker bootstrap path.
