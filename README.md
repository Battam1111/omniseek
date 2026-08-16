<div align="center">

<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/logo-icon.png">
  <img src="assets/logo-hero-dark.png" width="320" alt="OmniSeek">
</picture>

# OmniSeek

**Your agent seeks what search can't find.**

Give your agent ears, eyes, languages, logins, and a memory.

<sub>Self-hosted perception MCP server · one connection</sub>

[![CI](https://github.com/Battam1111/omniseek/actions/workflows/ci.yml/badge.svg)](https://github.com/Battam1111/omniseek/actions/workflows/ci.yml)
&nbsp;[![License](https://img.shields.io/badge/License-Apache_2.0-3B82F6?style=flat-square)](./LICENSE)
&nbsp;![Python](https://img.shields.io/badge/Python_3.11+-3B82F6?style=flat-square)
&nbsp;![Built for MCP](https://img.shields.io/badge/built_for-MCP-3B82F6?style=flat-square)
&nbsp;![Self-hosted](https://img.shields.io/badge/self--hosted-3B82F6?style=flat-square)

[Quick start](#quick-start) · [Tools](#tools) · [Configure](#configure) · [Contributing](#contributing)

**Languages:** English · [中文](docs/i18n/README_zh.md) · [日本語](docs/i18n/README_ja.md)

</div>

---

Search gives your agent indexed pages, in one language, in text. That's the surface.

OmniSeek reaches underneath: the podcast someone spoke into but nobody transcribed, the video frame that flashed a real number for two seconds, the Chinese forum post the English web hasn't caught up to, the comment three levels deep where someone corrected the headline, and the login-walled thread your browser can see but search can't. All on your machine.

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/demo-en-dark.png">
    <img src="assets/demo-en-light.png" alt="One real investigation: an English question about a changed rule. The surface quotes the rule; OmniSeek comes back with a Chinese explainer transcribed, login-walled first-person timelines, the workaround buried in a comment thread, and the dissenting video note, each line attributed.">
  </picture>
</div>

It hears (local bilingual ASR, no cloud), sees (images and video frames, in-band), crosses languages (a Chinese query finds English results and vice versa), reads behind login walls (your credentials, your machine, off by default), and remembers (a persistent relation graph that gets richer with every query).

Every source in the catalog earned its place by beating plain search at something, from citation graphs and regulatory filings to login-walled forums and Chinese-language video. And the catalog is built to grow: a curator pipeline probes, judges, and admits new sources, and retires the ones that decay.

**[Worked examples, real outputs](docs/examples.md)** · **[A full case study](docs/case-study.md)**

---

## Quick start

### Docker (recommended)

```bash
git clone https://github.com/Battam1111/omniseek.git && cd omniseek
docker compose up -d
docker compose logs omniseek        # bearer token printed on first start
curl -s http://127.0.0.1:8765/healthz
```

Point your MCP client at `http://127.0.0.1:8765/mcp` with `Authorization: Bearer <token>`. The token is generated on first start and stored in `~/.omniseek/credentials/omniseek_http.json`.

The first `up` builds locally (dependencies + headless Chromium); later starts are instant. Optional extras: set `EXTRAS="[pdf,asr,walled]"` as a build arg. Optional but recommended: set `OMNISEEK_CONTACT_EMAIL` for a faster lane with Crossref, SEC, and Unpaywall.

### Without Docker

```bash
python -m venv .venv && . .venv/bin/activate
scripts/bootstrap.sh
python -m omniseek.serve_http
```

On Windows, run `bootstrap.sh` under Git Bash or WSL; Docker is the simplest path. For an always-on Linux service, see [`deploy/omniseek.service`](deploy/omniseek.service).

OmniSeek binds `127.0.0.1` and requires the bearer token on every request. Do not expose without a reverse proxy ([SECURITY.md](.github/SECURITY.md)).

---

## Tools

One MCP connection. Start with `omniseek_search`; explore what's available with `omniseek_sources`.

| Tool | What it does |
|------|-------------|
| `omniseek_search` | Fan out across the whole catalog, deduplicate, rank. Cross-lingual (semantic + lexical). |
| `omniseek_read` | Normalize any URL or document (web page, PDF, arXiv) into clean text. |
| `omniseek_view` | Read images, document figures, video frames with vision. |
| `omniseek_transcribe` | Transcribe audio/video locally. Bilingual ASR, sliceable by timestamp. |
| `omniseek_field_skeleton` | Map a research field's citation neighborhood: foundational core vs. frontier. |
| `omniseek_resolve_identity` | Resolve a person's name to candidate author IDs across databases. |
| `omniseek_coauthors` | Map a researcher's collaboration network by joint-paper count. |
| `omniseek_institution_cohort` | List who actively publishes at a lab, scoped to a field. |
| `omniseek_paper_enrich` | Open-access PDF, retraction/integrity status, citation count for a paper. |
| `omniseek_paper_recommend` | Semantically similar papers (SPECTER embeddings) that keyword search misses. |
| `omniseek_graph` | Query the accumulated relation graph: find, neighborhood, between, since, similar. |
| `omniseek_sensor` | Standing queries with novelty detection. Only tells you what is new. |
| `omniseek_ruling` | Record identity judgments (same/not-same) the graph applies at read time. |
| `omniseek_statement` | Record directed relations the graph carries forward. |
| `omniseek_curator_act` | Source lifecycle: submit, probe, judge, admit, retire. |
| `omniseek_curator_view` | Read the source-admission queue or a per-source audit dossier. |
| `omniseek_gather` | Run multiple tools in parallel, one response. |
| `omniseek_sources` | List and route: domains, regions, capabilities, health. |

Full reference in **[tools.md](docs/tools.md)** · **[FAQ](docs/faq.md)**

Using Claude Code? [`skills/omniseek-investigate`](skills/omniseek-investigate/SKILL.md) ships the investigation methodology (sweep, zoom, structure) as a ready-made skill.

---

## Configure

OmniSeek is **catalog-first**: with no config, every benign source is on and login-walled sources are off. Tune in one file, `~/.omniseek/profile.json` ([example](deploy/profile.example.json)):

| Tier | Default |
|------|---------|
| **free** (public, no key) | **on** |
| **keyed** (a free or paid API key you supply) | on once the key is set |
| **walled** (a login you hold) | **off**; you bring your own browser |
| **circumvention** | **off**; none in the default pack |

Full reference: **[configuration](docs/configuration.md)** · **[walled sources](docs/walled-sources.md)** · **[legal posture](docs/LEGAL-POSTURE.md)**

---

## Why self-hosted

Every query you run, every connection your agent finds, every credential you use stays on your machine. The relation graph it builds over months is yours; if you stop running OmniSeek, you keep everything. Not a feature toggle. The architecture.

---

## Contributing

See [CONTRIBUTING.md](.github/CONTRIBUTING.md). The bar for a new source: it must beat plain web search via a mode (structure / unwall / transcribe / recall / monitor). The bar for fixing a decayed source: low, please do. `python tests/smoke.py` before you push.

By participating you agree to the [Code of Conduct](.github/CODE_OF_CONDUCT.md).

<div align="center">

---

**Your agent seeks what search can't find.**

[Apache-2.0](./LICENSE) · [NOTICE](./NOTICE) · [Security](.github/SECURITY.md) · [Cite](./CITATION.cff)

</div>
