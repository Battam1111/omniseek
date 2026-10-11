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

We asked two agents for Mu Li's exact words on the Transformer paper's writing, and the minute, in his 87-minute Bilibili walkthrough: web search returned a paraphrase and "around minutes 1 to 8", while the agent with OmniSeek transcribed the video and found the line at 1:21:51.

<p align="center">
  <img src="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-en-light.gif?v=2026-10-11c" width="720" alt="A real run, side by side. Left, an agent with web search only answers with a paraphrase and says around minutes 1 to 8. Right, an agent with OmniSeek transcribes the 87-minute video and quotes Mu Li at 1:21:51, with a link that opens there.">
</p>

[The full comparison on three questions](https://github.com/Battam1111/omniseek/blob/main/docs/compare.md), with sources, dates, and a short video.
