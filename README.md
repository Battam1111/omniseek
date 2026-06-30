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

[How it works](#how-it-works) · [Quick start](#quick-start) · [Configure](#configure) · [Contributing](#contributing)

**Languages:** English · [中文](docs/i18n/README_zh.md) · [日本語](docs/i18n/README_ja.md)

</div>

---

You can only act on what you can find. You can only find what's on the surface.

Below the surface: a podcast holds the insight you need at minute 47, never transcribed. The setting that makes it reproduce flashed across a slide for three seconds; the deck was never posted. The method you are about to call new was worked out years ago, in a language you do not read.

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

A self-hosted deep-retrieval engine. Transcribes audio. Digests documents. Extracts from video and images. Reads across languages. Traverses citation graphs. Monitors what changed. A growing, open catalog of curated sources (hundreds today, extensible by anyone) spanning the deep web. Speaks MCP, so any AI agent, workflow, or application plugs in.

**But fragments aren't knowledge.** A hundred scattered findings are noise. Signal is when a salary thread in English, a hiring freeze on a Chinese forum, and a podcast aside point to the same conclusion from three independent angles no single source reveals. Penumbra equips your agent: every finding tagged by source, timestamp, and independence, with a precise map of what couldn't be reached. Your agent reasons over structured, source-traced, gap-mapped evidence. Not surface-level confidence.

**Imagine what becomes answerable.** Who knows whom in your field, across every language? What signal hides between a regulatory filing and a translated earnings call? What insider knowledge could you accumulate in months, not years? From the surface: unanswerable. From the penumbra: tractable. These are just the ones we've thought of.

<div align="center">

*The advantage was never about who's smarter.*
*It was about who could reach the penumbra.*

**Now you can.**

</div>

---

## How it works

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/demo-dark.png">
    <img src="assets/demo-light.png" alt="A worked example. Your agent asks why a result will not reproduce. Plain search (the paper, the README, the blog post) says it works. Penumbra reaches beneath the surface and returns three pieces the surface left out: a caveat the author only spoke in a talk Q&A, the hyperparameters shown on a conference slide and never written down, and a preprocessing step written years ago in another language. What makes it reproduce was spoken, shown, and written in another language; Penumbra brought back all three." width="780">
  </picture>
</div>

Your agent connects over MCP. Start with **`penumbra_search_ranked`**; `penumbra_list_sources()`
shows what's available. The catalog is open and growing: each source earns its place by beating
plain search. Full tool list in **[tools](docs/tools.md)**.

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
