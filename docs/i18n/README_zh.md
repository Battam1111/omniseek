<div align="center">

<picture>
  <source media="(prefers-color-scheme: light)" srcset="../../assets/logo-icon.png">
  <img src="../../assets/logo-hero-dark.png" width="320" alt="OmniSeek">
</picture>

# OmniSeek

**你的 agent,找到搜索找不到的东西。**

给你的 agent 装上耳朵、眼睛、语言、登录态和记忆。

<sub>自托管感知 MCP 服务器 · 一条连接</sub>

[![CI](https://github.com/Battam1111/omniseek/actions/workflows/ci.yml/badge.svg)](https://github.com/Battam1111/omniseek/actions/workflows/ci.yml)
&nbsp;[![License](https://img.shields.io/badge/License-Apache_2.0-3B82F6?style=flat-square)](../../LICENSE)
&nbsp;![Python](https://img.shields.io/badge/Python_3.11+-3B82F6?style=flat-square)
&nbsp;![Built for MCP](https://img.shields.io/badge/built_for-MCP-3B82F6?style=flat-square)
&nbsp;![Self-hosted](https://img.shields.io/badge/self--hosted-3B82F6?style=flat-square)

**Languages:** [English](../../README.md) · 中文 · [日本語](README_ja.md)

</div>

---

搜索给你的 agent 的,是已被索引的网页:单一语言,纯文本。那只是表面。

OmniSeek 探到表面之下:有人在播客里说过但从没被转写成文字的那句话,demo 视频里一闪两秒的真实数字,英文互联网还没跟上的中文论坛帖,评论区第三层里纠正标题的那条回复,还有你登录后能看见、搜索引擎却够不到的帖子。全部在你自己的机器上完成。

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../../assets/demo-zh-dark.png">
    <img src="../../assets/demo-zh-light.png" alt="一次真实调查:英文提问一条规则变化。表面只会复述规则;OmniSeek 带回转写的中文解读视频、登录墙内的第一手时间线、埋在评论区的解法、以及持反方观点的视频笔记,每一行都有出处。">
  </picture>
</div>

它能听(本地双语转写,不走云),能看(图片与视频帧,视觉直读),能跨语言(中文查询返回英文结果,反之亦然),能进登录墙(你的凭证,你的机器,默认关闭),还能记住(一张随每次查询增厚的持久关系图谱)。

目录里的每一个源,都是靠在某件事上打赢普通搜索才拿到位置的:从引用图谱、监管文件,到登录墙论坛、中文视频。目录生来就会生长:内置的 curator 流水线负责探测、判决、准入新源,并让退化的源退役。

**[真实例子、真实输出](../examples.md)** · **[一次完整调查](../case-study.md)**

---

## 快速开始

### Docker(推荐)

```bash
git clone https://github.com/Battam1111/omniseek.git && cd omniseek
docker compose up -d
docker compose logs omniseek        # 首次启动时打印 bearer token
curl -s http://127.0.0.1:8765/healthz
```

把你的 MCP 客户端指向 `http://127.0.0.1:8765/mcp`,带上 `Authorization: Bearer <token>`。token 在首次启动时生成,存于 `~/.omniseek/credentials/omniseek_http.json`。

首次 `up` 会本地构建镜像(依赖 + 无头 Chromium),需要几分钟;之后的启动是即时的。可选扩展:构建参数设 `EXTRAS="[pdf,asr,walled]"`。建议同时设置 `OMNISEEK_CONTACT_EMAIL`,Crossref、SEC、Unpaywall 会给礼貌联系方式一条快速通道。

### 不用 Docker

```bash
python -m venv .venv && . .venv/bin/activate
scripts/bootstrap.sh
python -m omniseek.serve_http
```

Windows 上请在 Git Bash 或 WSL 里跑 `bootstrap.sh`;Docker 是最省事的路。要挂常驻 Linux 服务,见 [`deploy/omniseek.service`](../../deploy/omniseek.service)。

OmniSeek 绑定 `127.0.0.1`,每个请求都要 bearer token。没有反向代理不要对外暴露([SECURITY.md](../../.github/SECURITY.md))。

---

## 工具

一条 MCP 连接。从 `omniseek_search` 开始;用 `omniseek_sources` 看有什么可用。

| 工具 | 干什么 |
|------|--------|
| `omniseek_search` | 全目录扇出、去重、排序。跨语言(语义 + 词面)。 |
| `omniseek_read` | 把任意 URL 或文档(网页、PDF、arXiv)归一为干净文本。 |
| `omniseek_view` | 用视觉读图片、文档插图、视频帧。 |
| `omniseek_transcribe` | 本地转写音视频。双语 ASR,可按时间戳切片。 |
| `omniseek_field_skeleton` | 画一个研究领域的引用邻域:根基与前沿。 |
| `omniseek_resolve_identity` | 把人名解析为跨库的候选作者 ID。 |
| `omniseek_coauthors` | 按合著篇数画一位研究者的合作网络。 |
| `omniseek_institution_cohort` | 列出某实验室里活跃发表的人,可按领域收窄。 |
| `omniseek_paper_enrich` | 一篇论文的开放获取 PDF、撤稿状态、被引数。 |
| `omniseek_paper_recommend` | 语义相似论文(SPECTER 向量),关键词搜不到的那种。 |
| `omniseek_graph` | 查累积的关系图谱:find、neighborhood、between、since、similar。 |
| `omniseek_sensor` | 常驻查询 + 新颖性检测。只在有新东西时告诉你。 |
| `omniseek_ruling` | 记录同一性裁决(是/不是同一实体),图谱读取时应用。 |
| `omniseek_statement` | 记录有向关系,图谱带着它走。 |
| `omniseek_curator_act` | 源的生命周期:提交、探测、判决、准入、退役。 |
| `omniseek_curator_view` | 读源准入队列或单源审计档案。 |
| `omniseek_gather` | 多件工具并行跑,一次返回。 |
| `omniseek_sources` | 列出与路由:领域、地区、能力、健康。 |

完整参考见 **[tools.md](../tools.md)** · **[FAQ](../faq.md)**

在用 Claude Code?[`skills/omniseek-investigate`](../../skills/omniseek-investigate/SKILL.md) 把调查方法论(扫、钻、结构化)打包成了现成的 skill。

---

## 配置

OmniSeek 是**目录优先**的:零配置时,每个无害源都开着,登录墙源都关着。全部调节集中在一个文件 `~/.omniseek/profile.json`([示例](../../deploy/profile.example.json)):

| 层级 | 默认 |
|------|------|
| **free**(公开,无需 key) | **开** |
| **keyed**(你提供的免费或付费 API key) | key 配好即开 |
| **walled**(你持有的登录) | **关**;浏览器你自己带 |
| **circumvention**(绕过访问控制) | **关**;默认包里一个也没有 |

完整参考:**[configuration](../configuration.md)** · **[walled sources](../walled-sources.md)** · **[legal posture](../LEGAL-POSTURE.md)**

---

## 为什么自托管

你跑的每条查询、agent 攒出的每条关系、你用的每个凭证,都留在你的机器上。没有任何云服务看得见你在研究什么。图谱是你的资产;哪天你不跑 OmniSeek 了,一切都还在你手里。这不是一个功能开关,这是架构本身。

---

## 参与

见 [CONTRIBUTING.md](../../.github/CONTRIBUTING.md)。新源的门槛:必须以某种模式(structure / unwall / transcribe / recall / monitor)打赢普通网页搜索。修一个退化源的门槛:很低,欢迎来修。push 前跑 `python tests/smoke.py`。

参与即同意[行为准则](../../.github/CODE_OF_CONDUCT.md)。

<div align="center">

---

**你的 agent,找到搜索找不到的东西。**

[Apache-2.0](../../LICENSE) · [NOTICE](../../NOTICE) · [Security](../../.github/SECURITY.md) · [引用本项目](../../CITATION.cff)

</div>
