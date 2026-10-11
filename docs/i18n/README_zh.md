<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/hero-zh-dark.png?v=2026-10-11">
    <img src="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/hero-zh-light.png?v=2026-10-11" width="560" alt="OmniSeek：给你的 AI Agent 接上网页搜索够不到的那部分互联网。">
  </picture>
</p>

**给你的 AI Agent 接上网页搜索够不到的那部分互联网。**

OmniSeek 是给 Claude Code、Cursor、Codex 这类 AI 编程助手和 Agent 装的一个扩展，让它们能接着搜网页搜索停下的地方。

- B 站视频和评论区里真人的经验，知乎在你点名时也搜，每条附原帖链接。
- 用中文问，也能拿到 Reddit、Hacker News 和英文论文里的答案。
- 一个研究方向的奠基论文，和最近在它们之上做的新论文，一次给全。

```bash
uv tool install omniseek
claude mcp add omniseek -- omniseek
```

在你自己的电脑上运行。它通过 MCP 接进去（MCP 是 Claude Code、Cursor 这类 AI 应用加装工具的通用接口）。Cursor 和 Codex 各一行命令，见[安装说明](../install.zh.md#接到客户端)。

[![CI](https://github.com/Battam1111/omniseek/actions/workflows/ci.yml/badge.svg)](https://github.com/Battam1111/omniseek/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/omniseek?color=3B82F6&style=flat-square)](https://pypi.org/project/omniseek/) [![License](https://img.shields.io/badge/License-Apache_2.0-3B82F6?style=flat-square)](../../LICENSE) ![Python](https://img.shields.io/badge/Python_3.11+-3B82F6?style=flat-square) ![Built for MCP](https://img.shields.io/badge/built_for-MCP-3B82F6?style=flat-square)
**Languages:** [English](../../README.md) · 中文 · [日本語](README_ja.md)

---

## 看看差别

问两个 Agent：李沐在 B 站 87 分钟的 Transformer 论文精读里怎么评价这篇论文的写作，原话是什么、在第几分钟；只有网页搜索的 Agent 给出网友整理的大意和「大约在开头第1~8分钟」，装了 OmniSeek 的 Agent 把视频整段转成文字，找到原话在 1:21:51。

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-zh-dark.gif?v=2026-10-11b">
    <img src="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-zh-light.gif?v=2026-10-11b" width="720" alt="一次真实运行，左右对照。左边只有网页搜索的 Agent 给出网友整理的大意，说大约在开头第 1 到 8 分钟；右边装了 OmniSeek 的 Agent 把 87 分钟的视频整段转成文字，找到李沐的原话在 1:21:51，附一个从这一秒打开的链接。">
  </picture>
</p>

[三个问题的完整对照](../compare.zh.md)，附出处、日期和一段短视频。

---

## 你的 Agent 能去哪

- **中文社区**：B 站视频和评论区、知乎回答（点名才搜）、V2EX 近期帖子，附原帖链接。详见 [B 站](https://battam1111.github.io/omniseek/bilibili-mcp.html)、[知乎](https://battam1111.github.io/omniseek/zhihu-mcp.html)（英文页）
- **跨语言**：用中文问，它去搜英文的；反过来也行，再用你的语言回答。[详见](https://battam1111.github.io/omniseek/cross-lingual-search-mcp.html)
- **播客和视频**：在你自己的电脑上把说的话转成文字，引原话时带上第几分钟（需装一个可选组件）。[详见](https://battam1111.github.io/omniseek/podcast-transcription-mcp.html)
- **论文**：一个方向立在哪几篇论文上，最近又有哪些新论文引了它们。[详见](https://battam1111.github.io/omniseek/citation-graph-mcp.html)
- **只看新的**：一个搜索存一次，以后每次只给你上回没有的。[详见](https://battam1111.github.io/omniseek/research-monitoring-mcp.html)
- **你登录的网站**：你打开后，Agent 用你自己浏览器里的登录态去读。默认关。[详见（英文）](../walled-sources.md)

它还会把找到的东西存进你电脑上的本地索引，下次搜索从已经知道的地方接着找。

---

## 安装

上面两条命令就够了，需要 Python 3.11 以上（没有的话 [uv](https://docs.astral.sh/uv/) 会自己下载）。你的 AI 应用要用时会自己启动 OmniSeek，不用一直开着，也不用开端口。

Cursor、Codex、pipx、Docker 的装法，以及几个可选组件（把播客说的话转成文字、读 PDF、读要登录的网站），都在 **[安装说明](../install.zh.md)**。

---

## 你的数据，你的账号

OmniSeek 在你自己的电脑上跑。没有 OmniSeek 云端，不用注册，不回传任何使用数据：Agent 的搜索直接发给你打开的那些网站。公开网站默认开；要登录的网站默认关，你打开后用的是你自己浏览器里已有的登录，OmniSeek 碰不到你的密码。用久了积累下来的东西，都是你本地的文件。

OmniSeek 自己不带 AI 模型，也不往我们的服务器发任何东西。思考是你的 Agent 的事，OmniSeek 负责去把东西找回来。设置见[配置（英文）](../configuration.md)。

---

## 工具

你的 Agent 从 `omniseek_search` 开始。其余的，从画论文地图、存搜索，到 Agent 记下的判断（`omniseek_ruling`、`omniseek_statement`），都在 **[tools.md（英文）](../tools.md)**。

---

## 更多

[真实例子、真实输出（英文）](../examples.md) · [一次完整调查（英文）](../case-study.md) · [常见问题（英文）](../faq.md)，含「和 Exa、Tavily 有什么不同」 · [全部来源（英文）](../sources.md) · [各来源可用情况，每周更新](https://github.com/Battam1111/omniseek/blob/health-data/README.md)

**[这些说法是怎么测的（英文）](../../bench/DESIGN.md)**：一道题要先确认普通网页搜索第一页找不到答案，才算数（[最新结果](https://github.com/Battam1111/omniseek/blob/health-data/bench/RESULTS.md)）。在用 Claude Code？[`skills/omniseek-investigate`](../../skills/omniseek-investigate/SKILL.md) 是一套现成的调研方法：先广搜，再收窄，再理结构。

---

## 参与

见 [CONTRIBUTING.md](../../.github/CONTRIBUTING.md)：新来源得能找到普通网页搜索找不到的东西。某个来源失效了，欢迎来修。push 前跑 `python tests/smoke.py`。

参与即同意[行为准则](../../.github/CODE_OF_CONDUCT.md)。

<div align="center">

---

**给你的 AI Agent 接上网页搜索够不到的那部分互联网。**

[Apache-2.0](../../LICENSE) · [NOTICE](../../NOTICE) · [Security](../../.github/SECURITY.md) · [引用本项目](../../CITATION.cff)

</div>
