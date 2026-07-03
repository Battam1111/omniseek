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

Your agent is only as good as what it can reach. Plain search only reaches the surface: written down, in a language it reads, still standing where it was.

Below the surface: you're deciding whether to join a startup, and every public report says it's thriving; three months ago on a podcast, at minute 52, the founder let slip "we've got maybe nine months of runway left," and it was never transcribed. You're weighing a product every review gives five stars; in the demo video, a real performance metric flashes on screen for under two seconds, ten times worse than the spec sheet, and no one ever said it aloud. You're thinking about moving to a new city, and every travel blogger calls it livable; locals wrote out the real picture on a forum long ago, in a language you don't read.

All there. Untouched.

<h3 align="center">That's the penumbra.</h3>

<div align="center">

The zone between what your agent can reach and what actually exists: vast, half-lit.
Not secret. A blind spot:
**the knowledge is right there, scattered everywhere, just invisible to the surface.**

</div>

<br>

**Why can't it reach?** What's spoken, search can't hear; what's on screen, search can't read; what's in another language, you can't read; what you can see when logged in, search can't get to; what was there yesterday, gone today. Plain search hits every wall. Reach it yourself, one at a time? You'd get there eventually. You just don't have that much time.

**Penumbra gives your agent that reach:** it transcribes what was spoken into text, reads what flickered across a screen, translates what was written in another language into yours, gets into places you can see when logged in but search can't, pulls back what's been deleted, and threads together what's scattered across hundreds of records. What was out of reach, it brings back in one pass.

**But a pile of fragments still isn't knowledge.** A hundred scattered findings are noise until they line up: independent angles converging on the same conclusion, none of them telling the whole story alone. Penumbra marks where each fragment came from, when, and whether it's an echo of another, then weaves together what's scattered across different sources: the same name surfacing in three unrelated places, a timeline that only makes sense laid end to end, a relationship hiding in the gaps between records. What's left, your agent assembles into the one map that's yours alone.

**And there is a second unfair advantage, and it has nothing to do with reach.** Most knowing
evaporates: what you found last spring, what you noticed on some Tuesday, what you once worked
out and were briefly, quietly right about. Everyone around you fades at the same rate, so
nobody ever feels the loss. Yours doesn't have to.

In a meeting, someone presents this week's discovery; you saw it eight months ago, back when
it was one engineer complaining in a corner of the internet. A lie comes around a second time
wearing new clothes; you're the only one in the room who recognizes it. Someone finally asks
how you always seem to know early, and there is an answer, and the answer has a date on it.

Everyone else starts every morning from zero. You start from everything you've ever seen.
Reach makes you early once; memory makes early a habit.

**Imagine what becomes answerable.** That product you use every day, stellar reviews everywhere: can you tell how many are actually independent? That piece of insider knowledge everyone in your industry passes around: is it firsthand, or did one person make it up on three different platforms? A risk that directly concerns you, already discussed for two years in a language you couldn't read, and you didn't even know it existed? These are only the questions that came to mind first.

<div align="center">

*The penumbra was always there.*

**Now it's within reach.**

</div>

---

## How it works

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/demo-en-dark.png">
    <img src="assets/demo-en-light.png" alt="A worked example. Your agent asks what is really going on. Plain search returns only the surface: the announcement, the reviews, the docs, all clean and agreeing. Penumbra reaches beneath and brings back four fragments no single search surfaces: a talk it transcribed, where two argue on stage and expose the fault line the write-up hides; a demo video it read from the frame, where a number flickers for a second and is never said aloud; a hundred records it connected, revealing a tie no single page states; and a forum in a language you don't read, where an insider says the quiet part. Alone, each is noise. Assembled, they become the map only you can build. Penumbra's reach spans audio, video, images, languages, connections, the deleted, logins, and more." width="780">
  </picture>
</div>

Your agent connects over MCP. Start with **`penumbra_search`**; `penumbra_sources()`
shows what's available. The catalog is open and growing: each source earns its place by beating
plain search. What your agent retrieves accretes into a persistent relation graph it can ask
questions of later (`penumbra_graph(view, args)`: `find` / `stats` / `neighborhood` / `between` /
`voices` / `since` / `similar`, one frozen verb whose views grow as data), and its judgments
are recorded with `penumbra_ruling` (identity) and `penumbra_statement` (typed relations it
judged from content): the graph applies them at read time, it never judges by itself. Full
tool list in **[tools](docs/tools.md)**. For a real investigation written up end to end, see
the **[case study](docs/case-study.md)**.

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
`~/.penumbra/credentials/penumbra_http.json` (mounted as `./.penumbra/credentials/penumbra_http.json` under Docker).

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
| **circumvention** | **off**; none in the default pack |

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
