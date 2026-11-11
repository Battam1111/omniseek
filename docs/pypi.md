<!-- mcp-name: io.github.Battam1111/omniseek -->
<!-- Generated from README.md by scripts/gen_pypi_readme.py: edit README.md, then run it. -->

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

One real question, asked in Chinese: in Mu Li's 87-minute Bilibili walkthrough of the Transformer paper, what does he say about the paper's writing, and at what minute?

<p align="center">
  <img src="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/seek-en-light.gif?v=2026-11-11" width="720" alt="The 87-minute video as a timeline. Web search's two answers point near the start: minutes 1 to 8, then 1:50 to 2:30. OmniSeek transcribes all 87 minutes and finds the line at 1:21:51.">
</p>

- **Web search** answered from other people's notes: "around minutes 1 to 8", and asked again, "between about 1:50 and 2:30". A search for his exact words found nothing.
- **With OmniSeek**, the agent transcribed all 87 minutes locally and found the line at [1:21:51](https://www.bilibili.com/video/BV1pu411o7BE?t=4911): 「这篇文章的写作是非常简洁的，因为就是说他每一句话就基本上在讲一件事情。」 "The writing of this paper is very concise: basically, every sentence says one thing." Asked again, it gave the same line from cache in 0.1 s.

[The full comparison on three questions](https://github.com/Battam1111/omniseek/blob/main/docs/compare.md), with sources, dates, and a short video.
