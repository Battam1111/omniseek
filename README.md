<!-- mcp-name: io.github.Battam1111/omniseek -->

<p align="center">
  <img src="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/logo-lockup.png?v=2026-10-11b" width="320" alt="OmniSeek">
</p>

**Give your AI agent the parts of the internet web search doesn't reach.**

OmniSeek is an add-on for AI assistants and agents like Claude Code, Cursor, and Codex that lets them search where web search stops.

- Ask in English, get what people actually said on Bilibili, and Zhihu when you ask for it, with links to the original posts.
- Get the papers a research field is built on, and the recent ones building on them, in one answer.
- Have your agent watch a topic and tell you only what is new since last time.

```bash
uv tool install omniseek
claude mcp add omniseek -- omniseek
```

Runs on your own computer. It plugs in through MCP, the standard way AI apps like Claude Code and Cursor add tools. Cursor and Codex: one line each, see [Install](https://github.com/Battam1111/omniseek/blob/main/docs/install.md#connect-your-client).

[![CI](https://github.com/Battam1111/omniseek/actions/workflows/ci.yml/badge.svg)](https://github.com/Battam1111/omniseek/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/omniseek?color=3B82F6&style=flat-square)](https://pypi.org/project/omniseek/) [![License](https://img.shields.io/badge/License-Apache_2.0-3B82F6?style=flat-square)](https://github.com/Battam1111/omniseek/blob/main/LICENSE) ![Python](https://img.shields.io/badge/Python_3.11+-3B82F6?style=flat-square) ![Built for MCP](https://img.shields.io/badge/built_for-MCP-3B82F6?style=flat-square)
**Languages:** English · [中文](https://github.com/Battam1111/omniseek/blob/main/docs/i18n/README_zh.md) · [日本語](https://github.com/Battam1111/omniseek/blob/main/docs/i18n/README_ja.md)

---

## See the difference

We asked two agents for Mu Li's exact words on the Transformer paper's writing, and the minute, in his 87-minute Bilibili walkthrough: web search returned a paraphrase and "around minutes 1 to 8", while the agent with OmniSeek transcribed the video and found the line at 1:21:51.

<p align="center">
  <a href="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-en-light.gif?v=2026-10-11d#gh-light-mode-only"><picture>
    <source media="(max-width: 480px)" srcset="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-en-light-narrow.gif?v=2026-10-11d#gh-light-mode-only">
    <img src="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-en-light.gif?v=2026-10-11d#gh-light-mode-only" width="720" alt="A real run, side by side. Left, an agent with web search only answers with a paraphrase and says around minutes 1 to 8. Right, an agent with OmniSeek transcribes the 87-minute video and quotes Mu Li at 1:21:51, with a link that opens there.">
  </picture></a>
  <a href="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-en-dark.gif?v=2026-10-11d#gh-dark-mode-only"><picture>
    <source media="(max-width: 480px)" srcset="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-en-dark-narrow.gif?v=2026-10-11d#gh-dark-mode-only">
    <img src="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-en-dark.gif?v=2026-10-11d#gh-dark-mode-only" width="720" alt="A real run, side by side. Left, an agent with web search only answers with a paraphrase and says around minutes 1 to 8. Right, an agent with OmniSeek transcribes the 87-minute video and quotes Mu Li at 1:21:51, with a link that opens there.">
  </picture></a>
</p>

[The full comparison on three questions](https://github.com/Battam1111/omniseek/blob/main/docs/compare.md), with sources, dates, and a short video.

---

## What your agent can reach

- **Chinese communities**: Bilibili videos and comments, Zhihu answers when you ask for them, recent V2EX threads, all with links to the posts. More: [Bilibili](https://battam1111.github.io/omniseek/bilibili-mcp.html), [Zhihu](https://battam1111.github.io/omniseek/zhihu-mcp.html)
- **Across languages**: ask in English, search in Chinese, answer in English. And the reverse. [More](https://battam1111.github.io/omniseek/cross-lingual-search-mcp.html)
- **Podcasts and videos**: turns speech into text on your own computer and quotes it with the minute (optional add-on). [More](https://battam1111.github.io/omniseek/podcast-transcription-mcp.html)
- **Research papers**: which papers a field is built on and which recent ones cite them. [More](https://battam1111.github.io/omniseek/citation-graph-mcp.html)
- **New since last time**: save a search once; each rerun shows only what is new. [More](https://battam1111.github.io/omniseek/research-monitoring-mcp.html)
- **Sites you log in to**: if you switch it on, your agent reads them through your own browser login. Off by default. [More](https://github.com/Battam1111/omniseek/blob/main/docs/walled-sources.md)

It also keeps what it finds in a local index on your computer, so later searches start from what it already knows.

---

## Install

The two commands above are all you need, with Python 3.11 or newer ([uv](https://docs.astral.sh/uv/) installs it if you don't have it). Your AI app starts OmniSeek when it needs it. Nothing to keep running, no port to open.

Cursor, Codex, pipx, Docker, and the optional add-ons (podcast transcription, PDF reading, sites you log in to) are in **[Install](https://github.com/Battam1111/omniseek/blob/main/docs/install.md)**.

---

## Your data, your logins

OmniSeek runs on your computer. There is no OmniSeek cloud, no account, no telemetry: your agent's searches go straight to the sites you turned on. Public sites are on by default. Sites that need a login are off until you switch them on, and then they use the login already in your own browser; OmniSeek never sees your password. What it builds up as you use it stays in local files you own.

OmniSeek has no AI model of its own and sends nothing to a server of ours. Your agent does the thinking; OmniSeek goes and gets things. Settings: [Configuration](https://github.com/Battam1111/omniseek/blob/main/docs/configuration.md).

---

## Tools

Your agent starts with `omniseek_search`. The rest, from paper maps to saved searches and the notes your agent keeps (`omniseek_ruling`, `omniseek_statement`), are in **[tools.md](https://github.com/Battam1111/omniseek/blob/main/docs/tools.md)**.

---

## More

[Worked examples, real outputs](https://github.com/Battam1111/omniseek/blob/main/docs/examples.md) · [A full case study](https://github.com/Battam1111/omniseek/blob/main/docs/case-study.md) · [FAQ](https://github.com/Battam1111/omniseek/blob/main/docs/faq.md), including how it differs from Exa and Tavily · [All sources](https://github.com/Battam1111/omniseek/blob/main/docs/sources.md) · [Source health, updated weekly](https://github.com/Battam1111/omniseek/blob/health-data/README.md)

**[How we test these claims](https://github.com/Battam1111/omniseek/blob/main/bench/DESIGN.md)**: a test only counts if we first checked that plain web search could not find its answer on the first page ([latest results](https://github.com/Battam1111/omniseek/blob/health-data/bench/RESULTS.md)). Using Claude Code? [`skills/omniseek-investigate`](https://github.com/Battam1111/omniseek/blob/main/skills/omniseek-investigate/SKILL.md) is a ready-made research method: search wide, narrow in, add structure.

---

## Contributing

See [CONTRIBUTING.md](https://github.com/Battam1111/omniseek/blob/main/.github/CONTRIBUTING.md): a new source has to find something plain web search doesn't. Fixing a source that stopped working is always welcome. Run `python tests/smoke.py` before you push.

By participating you agree to the [Code of Conduct](https://github.com/Battam1111/omniseek/blob/main/.github/CODE_OF_CONDUCT.md).

<div align="center">

---

**Give your AI agent the parts of the internet web search doesn't reach.**

[Apache-2.0](https://github.com/Battam1111/omniseek/blob/main/LICENSE) · [NOTICE](https://github.com/Battam1111/omniseek/blob/main/NOTICE) · [Security](https://github.com/Battam1111/omniseek/blob/main/.github/SECURITY.md) · [Cite](https://github.com/Battam1111/omniseek/blob/main/CITATION.cff)

</div>
