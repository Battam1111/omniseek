# Changelog

All notable changes to Penumbra are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is SemVer.

## [Unreleased]

- PyPI publish (`pip install penumbra-mcp`) planned.

## [0.1.0]

### Added

- Initial public release: a self-hosted deep-retrieval MCP server.
- Around 200 curated sources across 157 independent upstreams, classified by access tier (free / keyed / walled).
- MCP tool surface: `penumbra_search`, `penumbra_search_ranked`, `penumbra_fetch`, `penumbra_list_sources`, `penumbra_add_url`, paper + citation tools, people + organization tools, document + vision reading, audio transcription, health check, and a self-iterating source curator.
- Token-gated loopback HTTP transport; SSRF guard; sandboxed document inbox.
- Apache-clean core install; opt-in extras (`pdf` / `asr` / `recall` / `ocr` / `walled`).
- One-command Docker self-host and a non-Docker bootstrap path.
