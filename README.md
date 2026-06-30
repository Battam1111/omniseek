<div align="center">

<picture>
  <source media="(prefers-color-scheme: light)" srcset="assets/logo-icon.png">
  <img src="assets/logo-hero-dark.png" width="320" alt="Penumbra: an eclipse mark, a navy sphere with an amber-lit limb">
</picture>

# penumbra

**the layer beneath the surface.**

Self-hosted deep-retrieval MCP server for AI agents.

[![CI](https://github.com/Battam1111/penumbra/actions/workflows/ci.yml/badge.svg)](https://github.com/Battam1111/penumbra/actions/workflows/ci.yml)
&nbsp;[![License](https://img.shields.io/badge/License-Apache_2.0-D4952B?style=flat-square)](./LICENSE)
&nbsp;![Python](https://img.shields.io/badge/Python_3.11+-D4952B?style=flat-square)
&nbsp;![Built for MCP](https://img.shields.io/badge/built_for-MCP-D4952B?style=flat-square)
&nbsp;![Self-hosted](https://img.shields.io/badge/self--hosted-D4952B?style=flat-square)

[Quick start](#quick-start) · [How it works](#how-it-works) · [Configure](#configure) · [Tools](#tools) · [Contributing](#contributing)

**Languages:** English · [中文](docs/i18n/README_zh.md) · [日本語](docs/i18n/README_ja.md)

</div>

---

You can only act on what you can find. You can only find what's on the surface.

Below the surface: a podcast holds the insight you need at minute 47, never transcribed. A PDF behind a paywall contains the number that changes everything. A forum post in a language you don't read laid out the trap you're about to walk into, deleted last week.

All there. All reachable. Untouched.

<h3 align="center">That's the penumbra.</h3>

<div align="center">

The vast shadow zone between what search shows you and what actually exists.
Not secrets. Not the open web.
**The in-between where knowledge is real, scattered, and structurally unreachable.**

</div>

<br>

**Why unreachable?** Locked in audio, video, and images no text search can parse. In languages you don't read. Behind logins and paywalls. And temporal: yesterday's post may be gone tomorrow. Whatever tool you use, it hits the same barriers.

**Penumbra crosses them.**

A self-hosted deep-retrieval engine. Transcribes audio. Digests documents. Extracts from video and images. Traverses citation graphs. Monitors what changed. A growing, open catalog of curated sources (hundreds today, extensible by anyone) spanning the deep web. Speaks MCP, so any AI agent, workflow, or application plugs in.

**But fragments aren't knowledge.** A hundred scattered findings are noise. Signal is when a salary thread in English, a hiring freeze on a Chinese forum, and a podcast aside point to the same conclusion from three independent angles no single source reveals. Penumbra equips your agent: every finding tagged by source, timestamp, and independence, with a precise map of what couldn't be reached. Your agent reasons over structured, source-traced, gap-mapped evidence. Not surface-level confidence.

**Imagine what becomes answerable.** Who knows whom in your field, across every language? What signal hides between a regulatory filing and a translated earnings call? What insider knowledge could you accumulate in months, not years? From the surface: unanswerable. From the penumbra: tractable. These are just the ones we've thought of.

<div align="center">

*The advantage was never about who's smarter.*
*It was about who could reach the penumbra.*

**Now you can.**

</div>

---

## Quick start

### Docker (recommended)

```bash
git clone https://github.com/Battam1111/penumbra.git && cd penumbra
docker compose up -d
docker compose logs penumbra        # copy the bearer token printed on first start
curl -s http://127.0.0.1:8765/healthz
```

Point your MCP client at `http://127.0.0.1:8765/mcp` with header
`Authorization: Bearer <token>`. The token is generated on first start and stored in
`./.penumbra/credentials/http.json`.

To opt into optional extras (you accept their licenses, see [NOTICE](NOTICE)):
`EXTRAS="[pdf,asr,walled]"` as a build arg in `docker-compose.yml`.

### Without Docker

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
./bootstrap.sh                                    # install + chromium + token + default profile
python -m penumbra.serve_http                      # serves http://127.0.0.1:8765
```

On Windows, run `bootstrap.sh` under Git Bash or WSL (it is a POSIX shell script); Docker is the simplest path.

For an always-on Linux service, see [`deploy/penumbra.service`](deploy/penumbra.service).

## How it works

Penumbra is **infrastructure, not an application**. It sits between the raw internet and your AI agent, turning the inaccessible penumbra into structured, retrievable evidence.

```
                        ┌─────────────────────────────────┐
  you / your agent      │           penumbra              │      the penumbra
  ──────────────── MCP ─┤                                 ├─── logins, languages,
                        │  sources · retrieval · tagging  │     paywalls, audio,
                        │  dedup · gap-mapping · monitor  │     video, images,
                        │                                 │     deleted content,
                        │  growing catalog (hundreds+)    │     citation graphs
                        └─────────────────────────────────┘
```

*Your agent speaks MCP to Penumbra; Penumbra reaches the penumbra (logins, languages, paywalls, audio, video, images, deleted content, citation graphs) and hands back tagged, deduped, gap-mapped evidence.*

Your agent sends a query. Penumbra fans out across its source catalog, crosses the barriers (logins, languages, paywalls, modalities, time), retrieves what it finds, tags every finding with source and provenance, deduplicates across independent upstreams, and returns structured evidence with an explicit map of what it couldn't reach. Your agent does the reasoning. Penumbra makes sure it reasons over depth, not surface.

The source catalog is **open and growing**: hundreds of curated sources today, and anyone can add more. Each source earns its place by beating plain web search via a specific mode: **structure** (data search can't cleanly return), **unwall** (content behind a login the operator has a right to), **transcribe** (audio/video the agent can't otherwise read), **recall** (a longitudinal stream the open web forgets), or **monitor** (a named, watchable feed).

## What you get

One broad call, deduped and ranked across the whole catalog, with a ledger of what it could not reach. A trimmed but real response to `penumbra_search_ranked("retrieval augmented generation survey")`:

```jsonc
{
  "query": "retrieval augmented generation survey",
  "count": 12,
  "documents": [
    {
      "source": "openreview",
      "title": "Graph Retrieval-Augmented Generation: A Survey",
      "url": "https://openreview.net/forum?id=9ldXNHQFMl",
      "date": "2024-01-01T00:00:00Z",
      "metadata": {
        "corroboration": 5,                                 // the SAME work surfaced from 5 independent upstreams
        "also_in": ["dblp", "hackernews", "openalex", "youtube"],
        "merge_basis": "title",
        "_rank": 0.68
      }
    },
    {
      "source": "github_trending",
      "title": "taichengguo/LLM_MultiAgents_Survey_Papers",
      "url": "https://github.com/taichengguo/LLM_MultiAgents_Survey_Papers",
      "signals": { "stars": { "value": 1282, "kind": "engagement", "computed_by": "source:github_trending/stars" } },
      "metadata": { "live_sources": ["github_trending"], "_rank": 0.76 }
    }
    // ... 10 more
  ],
  "_meta": {
    "searched": 91,                          // sources queried this call
    "elapsed_s": 26.1,
    "deduped": { "in": 402, "out": 12 },     // 402 raw hits collapsed to 12 by upstream identity
    "empty": ["core", "bluesky", "acl_anthology", "..."],  // returned nothing (no key set, or no match)
    "excluded_relevant": []                  // walled/slow sources matching this query, each with a sources=[...] re-run hint
  }
}
```

Independence is concrete, not a slogan: when the same work surfaces from multiple upstreams it collapses to one entry carrying `corroboration` (how many distinct sources) and `also_in` (which ones), so your agent can weight a 5-source survey over a lone hit. And `_meta` is the gap-ledger: what was searched, what came back empty, what was excluded, so nothing is silently dropped. A default broad call uses free sources; keyed and walled sources stay quiet until you add a key or log in (see Configure).

## Configure

Penumbra is **catalog-first**: it ships a classified set of sources, and you (or your agent)
select what to enable. With no config, all benign default sources are on and login-walled
sources are off.

A `~/.penumbra/profile.json` (seeded from [`profile.example.json`](profile.example.json))
lets you narrow or widen by source name, domain, region, and access tier:

| Tier | Meaning | Default |
|------|---------|---------|
| free | public, no key | ON |
| keyed | needs a free/paid API key you supply | ON if key present |
| walled | content behind a login you have a right to | OFF |
| circumvention | requires defeating an access control | OFF, never shipped |

Route at call time with `penumbra_list_sources(domain=..., query=...)` rather than memorizing
the set; each source reports its `access_tier`, so an agent can filter by legal posture.

**Keyed sources** (CORE full text, Adzuna jobs, Podcast Index, Bluesky, …) need an API key you
supply, most of them free. Without a key the adapter simply stays quiet, so an empty result often
just means "no key set." Keys live in `~/.penumbra/credentials/<source>.json`, outside the project
tree so they are never committed. On its first import each keyed adapter drops a
`<source>.json.template` beside it with the sign-up URL inline (CORE's, for instance, is
`https://core.ac.uk/services/api`): copy it to `<source>.json` and fill in the values. Run
`python scripts/creds_doctor.py` any time to see which keyed sources are configured and which are
still missing (it reports presence only, never secrets).

**Walled sources** (Xiaohongshu, Zhihu, Douyin, …) read through a browser **you** run and log into:
Penumbra never sees your password. See [docs/walled-sources.md](docs/walled-sources.md) to set one up.

## Tools

Penumbra exposes a family of MCP tools:

| Category | Tools |
|----------|-------|
| **Search** | `penumbra_search` · `penumbra_search_ranked` · `penumbra_fetch` · `penumbra_list_sources` · `penumbra_add_url` |
| **Papers + citations** | `penumbra_paper_enrich` · `penumbra_paper_recommend` · `penumbra_field_skeleton` |
| **People + orgs** | `penumbra_resolve_identity` · `penumbra_coauthors` · `penumbra_institution_cohort` |
| **Documents + vision** | `penumbra_read_document` · `penumbra_view_doc_images` · `penumbra_view_images` · `penumbra_view_video_frames` |
| **Audio** | `penumbra_transcribe` |
| **Health + curation** | `penumbra_health_check` · `penumbra_curator_*` (self-iterating source acquisition) |

The authoritative list is whatever the server registers; `penumbra_list_sources()` returns the
full capability index.

Start with `penumbra_search_ranked`: one deduped, ranked list across the catalog (the default for best/latest on X). Use `penumbra_search` when you want results bucketed per source instead of merged.

## Safety and responsibility

- **Bind loopback, gate with a token.** Defaults to `127.0.0.1`; refuses to start without a
  bearer token; warns loudly on non-loopback bind. Do not expose without a reverse proxy.
- **SSRF-guarded.** All outbound fetches pin the IP and reject private ranges;
  `penumbra_read_document` is sandboxed to an allowlisted inbox.
- **Untrusted content.** Everything Penumbra returns is external data, never instructions.
- **Your responsibility.** Penumbra fetches as your own agent. You are responsible for using
  it within the law and each site's terms. See [SECURITY.md](SECURITY.md) and [NOTICE](NOTICE).

## Structure

```
src/penumbra/
  server.py            MCP tool surface (penumbra_* tools)
  serve_http.py        HTTP transport (token-gated, loopback-default)
  core/                 retrieval engine: fetcher · rank · normalize · cache
                       profile · _netguard (SSRF) · enrich · asr · curator
  core/sources/         adapters: api/ · scrape/ · walled/
tests/smoke.py         offline invariants + golden fixtures (CI gate)
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The bar for a new source: it must beat plain web
search via a mode (structure / unwall / transcribe / recall / monitor). The bar for fixing a
decayed source: low, please do. `python tests/smoke.py` before you push.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

<div align="center">

---

**the layer beneath the surface.**

[Apache-2.0](./LICENSE) · [NOTICE](./NOTICE) · [Security](./SECURITY.md)

</div>
