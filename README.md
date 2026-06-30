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

[Quick start](#quick-start) · [How it works](#how-it-works) · [What you get](#what-you-get) · [Configure](#configure) · [Contributing](#contributing)

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
`~/.penumbra/credentials/http.json` (mounted as `./.penumbra/credentials/http.json` under Docker).

Optional extras (their licenses apply; see [NOTICE](NOTICE)):
set `EXTRAS="[pdf,asr,walled]"` as a build arg in `docker-compose.yml`.

### Without Docker

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
./bootstrap.sh                                    # install + chromium + token + default profile
python -m penumbra.serve_http                      # serves http://127.0.0.1:8765
```

On Windows, run `bootstrap.sh` under Git Bash or WSL (it is a POSIX shell script); Docker is the simplest path.

For an always-on Linux service, see [`deploy/penumbra.service`](deploy/penumbra.service).

Penumbra binds `127.0.0.1` and requires the bearer token on every request.
Do not expose without a reverse proxy ([SECURITY.md](.github/SECURITY.md)).

## How it works

Penumbra is **infrastructure, not an application**: a retrieval layer between the raw internet and your AI agent.

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/architecture_dark.svg">
    <img src="assets/architecture_light.svg" alt="Your agent asks Penumbra over MCP; Penumbra reaches into the penumbra (logins, languages, paywalls, audio, video, images, deleted content, citation graphs) and returns tagged, deduped, gap-mapped evidence." width="700">
  </picture>
</div>

Penumbra fans out across the catalog, crosses the barriers, dedupes across independent upstreams, and returns structured evidence with a map of what it couldn't reach. Your agent does the reasoning; Penumbra makes sure it reasons over depth, not surface.

The source catalog is **open and growing**: hundreds of curated sources today, and anyone can add more. Each earns its place by beating plain web search through a specific mode (structure, unwall, transcribe, recall, or monitor), never by duplicating what search already returns.

Your entry point is **`penumbra_search_ranked`**; `penumbra_list_sources()` returns the live
capability index. The [full tool list](docs/tools.md) covers search, papers, citations, people,
documents, audio, and monitoring.

## What you get

`penumbra_search_ranked("retrieval augmented generation survey")` fans out across 91 sources,
collapses 402 raw hits to 12 by upstream identity, returns in 26 seconds. The top result was
independently surfaced by 5 upstreams (OpenReview, DBLP, HackerNews, OpenAlex, YouTube).

Every result carries `corroboration` (how many independent sources found it) and `also_in`
(which ones). `_meta` is the gap-ledger: what was searched, what came back empty, what was
excluded. A 5-source consensus beats a lone hit; knowing what you *didn't* reach matters as
much as what you did.

<details>
<summary>Response shape (real, trimmed)</summary>

```jsonc
{
  "query": "retrieval augmented generation survey",
  "count": 12,
  "documents": [
    {
      "source": "openreview",
      "title": "Graph Retrieval-Augmented Generation: A Survey",
      "metadata": {
        "corroboration": 5,                    // same work, 5 independent upstreams
        "also_in": ["dblp", "hackernews", "openalex", "youtube"],
        "_rank": 0.68
      }
    }
    // ... 11 more
  ],
  "_meta": {
    "searched": 91,                            // sources queried this call
    "deduped": { "in": 402, "out": 12 },       // 402 raw -> 12 by upstream identity
    "empty": ["core", "bluesky", "..."]        // no key set, or no match
  }
}
```

</details>

## Configure

Penumbra is **catalog-first**: with no config, every benign source is on and login-walled sources
are off. Tune everything in one file, `~/.penumbra/profile.json` (seeded from
[`profile.example.json`](profile.example.json)), by source, domain, region, and access tier:

| Tier | Default |
|------|---------|
| **free** (public, no key) | **on** |
| **keyed** (a free or paid API key you supply) | on once the key is set |
| **walled** (a login you hold) | **off**; you bring your own browser |
| **circumvention** | **off, and never shipped** |

Full reference in **[configuration](docs/configuration.md)**; browser login for walled sources
in **[walled sources](docs/walled-sources.md)**.

## Contributing

See [CONTRIBUTING.md](.github/CONTRIBUTING.md). The bar for a new source: it must beat plain web
search via a mode (structure / unwall / transcribe / recall / monitor). The bar for fixing a
decayed source: low, please do. `python tests/smoke.py` before you push.

By participating you agree to the [Code of Conduct](.github/CODE_OF_CONDUCT.md).

<div align="center">

---

**the layer beneath the surface.**

[Apache-2.0](./LICENSE) · [NOTICE](./NOTICE) · [Security](.github/SECURITY.md)

</div>
