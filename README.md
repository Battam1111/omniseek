<!-- mcp-name: io.github.Battam1111/omniseek -->

<p align="center">
  <img src="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/logo-icon.png" width="88" alt="OmniSeek logo">
</p>

# OmniSeek

**Give your AI agent the parts of the internet web search doesn't reach.**

OmniSeek is a self-hosted MCP server. Through one connection, your agent can search what people said in podcasts and videos, Chinese communities such as Bilibili and V2EX, and paper citation graphs. If you turn it on, it can also read sites behind your own login (off by default). It covers 200+ sources and works with Claude Code, Cursor, and other MCP clients.

Ask your agent:

> What are the foundational papers on speculative decoding, and which recent papers build on them?

Install (Python 3.11+; [uv](https://docs.astral.sh/uv/) fetches it if you don't have it):

```bash
uv tool install omniseek
claude mcp add omniseek -- omniseek
```

Podcast transcription, PDF reading, and logged-in sites are optional extras; Cursor, pipx, pip, HTTP, and Docker setups are there too: see [Install options](https://github.com/Battam1111/omniseek/blob/main/docs/install.md).
Full docs: [Docs](https://github.com/Battam1111/omniseek/tree/main/docs) | [All sources](https://github.com/Battam1111/omniseek/blob/main/docs/sources.md) | [Tools](https://github.com/Battam1111/omniseek/blob/main/docs/tools.md)

[![CI](https://github.com/Battam1111/omniseek/actions/workflows/ci.yml/badge.svg)](https://github.com/Battam1111/omniseek/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/omniseek?color=3B82F6&style=flat-square)](https://pypi.org/project/omniseek/) [![License](https://img.shields.io/badge/License-Apache_2.0-3B82F6?style=flat-square)](https://github.com/Battam1111/omniseek/blob/main/LICENSE) ![Python](https://img.shields.io/badge/Python_3.11+-3B82F6?style=flat-square) ![Built for MCP](https://img.shields.io/badge/built_for-MCP-3B82F6?style=flat-square)

**Languages:** English · [中文](https://github.com/Battam1111/omniseek/blob/main/docs/i18n/README_zh.md) · [日本語](https://github.com/Battam1111/omniseek/blob/main/docs/i18n/README_ja.md)

---

## What it finds that web search doesn't

The answer is sitting in minute 47 of a podcast, three replies deep in a comment thread, behind a login, in another language. Web search returns indexed pages, in one language, as text, and stops there. OmniSeek lets your agent keep going, all on your own machine.

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo-en-dark.png">
    <img src="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo-en-light.png" alt="One real investigation, drawn as three layers. Layer one, written down and easy to find: plain search quotes the rule and stops. Layer two, written down but behind a login or buried: first-person timelines on a forum you are logged in to, and a workaround in a comment thread. Layer three, never written down: a Chinese explainer video transcribed from audio, and a video note read from its frames. Each layer opens a different way to the answer.">
  </picture>
</div>

What each layer gave back, verbatim:

- **Written down, and easy to find.** Headlines, official FAQ, top blogs, all one voice: *"From 2026, F-1 admission is limited to a 4-year initial period; renewal in a third country remains possible."* All quote the same rule. None of them have done it.
- **Written down, but behind a login.** Three first-person threads on 1point3acres, read through your own login: **Bangkok**, booked to passport in 25 days, interview to approval in 30 minutes; **Milan**, a month-long fight for a slot, visa issued for 5 years; **Tokyo**, *"silky-smooth"*. Under the Milan post the author comes back in the comments: *"Book any late slot first, then email the consulate to expedite. For one F-1 applicant it worked."* One person's experience, not official guidance.
- **Never written down.** A Chinese explainer video on bilibili, transcribed locally: the *"4-year cap"* in the headlines is the initial period, extensions moved desks rather than vanishing. A rednote video note whose caption is four hashtags, frames and speech read locally: a 212(a)(6)(C) refusal abroad, a misrepresentation finding, can nearly close the F-1 road.

Plain search quoted the rule and stopped. The people who had lived it held the timelines, the workaround, and the risk. OmniSeek also named the sources it had not searched, each with the exact call that would search it.

What it can do: transcribe audio locally in English and Chinese (no cloud), look at images and video frames, cross languages (a Chinese query finds English results and the reverse), read sites behind your own login (your accounts, your machine, off by default), and remember (a local search index that grows as you use it, plus a graph of the papers, people, and pages it has found, each link traced to its source).

Crossing languages draws on the index OmniSeek builds as you use it, so a fresh install starts low. The published claim tests run on exactly that fresh install, so their cross-lingual number is the coldest case, not the typical one.

Every source in [the catalog](https://github.com/Battam1111/omniseek/blob/main/docs/sources.md) earned its place by beating plain search at one of five jobs: structure (citation graphs, regulatory filings), access behind a login, transcription, recall, or monitoring. The catalog keeps growing: new candidate sources are tested before they are added, and sources that stop working are retired.

**[Worked examples, real outputs](https://github.com/Battam1111/omniseek/blob/main/docs/examples.md)** · **[A full case study](https://github.com/Battam1111/omniseek/blob/main/docs/case-study.md)** · **[Every claim above is a test](https://github.com/Battam1111/omniseek/blob/main/bench/DESIGN.md)** ([latest results](https://github.com/Battam1111/omniseek/blob/health-data/bench/RESULTS.md)) · **[Source health, updated weekly](https://github.com/Battam1111/omniseek/blob/health-data/README.md)**

---

## Install options

The two commands at the top are the main path: the client starts `omniseek` itself over stdio, so there is no port and no token. Everything else is in **[Install options](https://github.com/Battam1111/omniseek/blob/main/docs/install.md)**:

- a Cursor `mcp.json` block, and `pipx` or plain `pip` if you prefer them (with `pip` in a virtual environment, register the full path to `omniseek`, because the client does not activate the environment);
- the optional extras: `pdf`, `asr` (transcription; needs PyTorch, with per-platform commands and disk sizes), `recall`, `ocr`, and `walled` (sites behind a login);
- a shared HTTP service with a bearer token, a one-line `docker run`, and `docker compose`.

OmniSeek's HTTP service binds `127.0.0.1` and requires the token on every request. Do not expose it without a reverse proxy ([SECURITY.md](https://github.com/Battam1111/omniseek/blob/main/.github/SECURITY.md)).

---

## Tools

One MCP connection; no model and no agent loop inside. Your model thinks, your client runs the loop, OmniSeek fetches. Start with `omniseek_search`; see what is available with `omniseek_sources`.

| Tool | What it does |
|------|-------------|
| `omniseek_search` | Search the whole catalog at once, remove duplicates, rank. Works across languages. |
| `omniseek_read` | Turn any URL or document (web page, PDF, arXiv) into clean text. |
| `omniseek_view` | Look at images, document figures, and video frames. |
| `omniseek_transcribe` | Transcribe audio or video locally, English and Chinese, from any timestamp. |
| `omniseek_field_skeleton` | Map a research field's citations: the papers it is built on and the recent ones. |
| `omniseek_resolve_identity` | Match a person's name to candidate author IDs across databases. |
| `omniseek_coauthors` | Map a researcher's collaborators by number of joint papers. |
| `omniseek_institution_cohort` | List who actively publishes at a lab, within a field. |
| `omniseek_paper_enrich` | Open-access PDF, retraction status, and citation count for a paper. |
| `omniseek_paper_recommend` | Similar papers (SPECTER embeddings) that keyword search misses. |
| `omniseek_graph` | Query the local graph of what OmniSeek has found: find, neighborhood, between, since, similar. |
| `omniseek_sensor` | Saved searches that rerun on a schedule and report only what is new. |
| `omniseek_ruling` | Record that two entries are (or are not) the same person or thing. |
| `omniseek_statement` | Record a directed relation between two entries. |
| `omniseek_curator_act` | Propose, test, add, or retire a source. |
| `omniseek_curator_view` | Read the queue of proposed sources or one source's report. |
| `omniseek_gather` | Run several tools in parallel, one response. |
| `omniseek_sources` | List sources by domain, region, capability, and health. |

Sites behind a login have no tool of their own: once you turn one on, the same `omniseek_search(..., sources=["xiaohongshu"], raw=True)` runs through your own logged-in browser. See [Sites behind a login](https://github.com/Battam1111/omniseek/blob/main/docs/walled-sources.md).

Full reference in **[tools.md](https://github.com/Battam1111/omniseek/blob/main/docs/tools.md)** · **[FAQ](https://github.com/Battam1111/omniseek/blob/main/docs/faq.md)**

Using Claude Code? [`skills/omniseek-investigate`](https://github.com/Battam1111/omniseek/blob/main/skills/omniseek-investigate/SKILL.md) packages a research method (search wide, narrow in, add structure) as a ready-made skill.

---

## Configure

With no config, every public source is on and every site behind a login is off. Change it in one file, `~/.omniseek/profile.json` ([example](https://github.com/Battam1111/omniseek/blob/main/deploy/profile.example.json)):

| Kind of source | Default |
|------|---------|
| Public, no key | **on** |
| Needs an API key you supply (free or paid) | on once the key is set |
| Behind a login you hold | **off**; uses your own browser |
| Needs an access control bypassed | **off**; none in the default set |

Full reference: **[Configuration](https://github.com/Battam1111/omniseek/blob/main/docs/configuration.md)** · **[Sites behind a login](https://github.com/Battam1111/omniseek/blob/main/docs/walled-sources.md)** · **[Legal posture](https://github.com/Battam1111/omniseek/blob/main/docs/LEGAL-POSTURE.md)**

---

## Why self-hosted

There is no OmniSeek cloud. No telemetry, no accounts, no relay: a query leaves your machine only as direct requests to the sources you enabled, and OmniSeek adds no other party to that path. Logins stay in your own browser and are shown only to the site they belong to; OmniSeek never stores, uploads, or even sees your passwords. The search index and graph it builds up over months are local files you own: stop running OmniSeek and you keep everything.

---

## Contributing

See [CONTRIBUTING.md](https://github.com/Battam1111/omniseek/blob/main/.github/CONTRIBUTING.md). The bar for a new source: it must beat plain web search at one of the five jobs above. The bar for fixing a source that stopped working: low, please do. Run `python tests/smoke.py` before you push.

By participating you agree to the [Code of Conduct](https://github.com/Battam1111/omniseek/blob/main/.github/CODE_OF_CONDUCT.md).

<div align="center">

---

**Give your AI agent the parts of the internet web search doesn't reach.**

[Apache-2.0](https://github.com/Battam1111/omniseek/blob/main/LICENSE) · [NOTICE](https://github.com/Battam1111/omniseek/blob/main/NOTICE) · [Security](https://github.com/Battam1111/omniseek/blob/main/.github/SECURITY.md) · [Cite](https://github.com/Battam1111/omniseek/blob/main/CITATION.cff)

</div>
