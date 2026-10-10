<p align="center">
  <img src="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/logo-icon.png" width="88" alt="OmniSeek 标志">
</p>

# OmniSeek

**给你的 AI Agent 接上网页搜索够不到的那部分互联网。**

OmniSeek 是一个自己部署的 MCP 服务。接上一次，你的 Agent 就能搜播客和视频里说的话、B 站和 V2EX 这类中文社区、论文引用图。你打开后，它还能用你自己的登录态读要登录才能看的网站（默认关）。覆盖 200 多个源，Claude Code、Cursor 等支持 MCP 的客户端都能用。

试着问你的 Agent：

> B 站上有哪些在自己电脑上部署大模型的实测视频？给出链接，说说各自用的什么硬件。

安装（需要 Python 3.11 以上；没有的话 [uv](https://docs.astral.sh/uv/) 会自己下载）：

```bash
uv tool install omniseek
claude mcp add omniseek -- omniseek
```

播客转写、PDF 阅读、要登录的网站是可选功能；Cursor、pipx、pip、HTTP、Docker 的装法也在那里，见[安装选项](../install.zh.md)。
完整文档：[文档](../) | [全部数据源](../sources.md) | [工具说明](../tools.md)

[![CI](https://github.com/Battam1111/omniseek/actions/workflows/ci.yml/badge.svg)](https://github.com/Battam1111/omniseek/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/omniseek?color=3B82F6&style=flat-square)](https://pypi.org/project/omniseek/) [![License](https://img.shields.io/badge/License-Apache_2.0-3B82F6?style=flat-square)](../../LICENSE) ![Python](https://img.shields.io/badge/Python_3.11+-3B82F6?style=flat-square) ![Built for MCP](https://img.shields.io/badge/built_for-MCP-3B82F6?style=flat-square)

**Languages:** [English](../../README.md) · 中文 · [日本語](README_ja.md)

---

## 网页搜索搜不到、它能搜到的

答案就躺在某期播客的第 47 分钟、某条评论的第三层回复里、要登录才能看的页面上、另一种语言里。网页搜索给的是已被收录的网页：单一语言、纯文本，到此为止。OmniSeek 让你的 Agent 接着往下找，全程都在你自己的机器上。

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo-zh-dark.png">
    <img src="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo-zh-light.png" alt="一次真实调查，画成三层。第一层「搜得到」：普通搜索引到规则就停了。第二层「写下来了，但要登录或埋得太深」：你登录的论坛上第一手的时间线，以及埋在评论区的办法。第三层「压根不是文字」：中文讲解视频经本地转写，以及只在画面上的视频笔记。每一层各有一条通向答案的路。">
  </picture>
</div>

每一层带回了什么，逐字引用：

- **搜得到。** 新闻头条、官方 FAQ、热门博客，口径完全一致：*「2026 年起，F-1 入境限四年初始期；第三国续签仍被允许。」* 引的都是同一条规则，谁都没真的办过。
- **写下来了，但要登录。** 一亩三分地上三份第一手时间线，用你自己的登录态读到：**曼谷**，预约到拿护照 25 天，面签到通过 30 分钟；**米兰**，抢号一个月，签出 5 年；**东京**，*「丝滑」*。米兰帖下，楼主回到评论区补充：*「先约一个靠后的日期，再发邮件给领事馆申请加急。有一位 F-1 申请人真的成功了。」* 个人经验，非官方指引。
- **压根不是文字。** bilibili 上一段中文解读视频，本地转写：头条说的*「最多待 4 年」*其实是初始停留期，延期只是换了审批部门，并没有消失。小红书一条视频笔记，正文只有四个话题标签，画面与语音在本地读取：第三国遇上 212(a)(6)(C) 拒签（虚假陈述认定），F-1 之路几乎断送。

普通搜索引到规则就停了。真正办过的人手里有时间线、办法和风险。OmniSeek 还列出了它没去搜的源，每个都附上去搜它的那条调用。

它能做的：在本地转写中英文音频（不走云）；看图片和视频帧；跨语言（中文查询能找到英文结果，反过来也行）；用你自己的账号读要登录的网站（在你的机器上，默认关）；还会记住（一个随使用增长的本地搜索索引，外加一张它找到的论文、人、网页之间的关系图，每条关系都能追到出处）。

跨语言要靠 OmniSeek 在使用中积累的索引，所以新装时起点很低。公开的验证测试正是在这样一台新装实例上跑的，它的跨语言数字是最冷的情况，不是常态。

[源目录](../sources.md)里的每个源，都是在五件事之一上打赢了普通搜索才收进来的：结构化（引用图、监管文件）、读要登录的内容、转写、召回、监测。目录会一直增长：新的候选源先测试再收录，失效的源会退下来。

**[真实例子、真实输出](../examples.md)** · **[一次完整调查](../case-study.md)** · **[上面的每条说法都有对应测试](../../bench/DESIGN.md)**（[最新结果](https://github.com/Battam1111/omniseek/blob/health-data/bench/RESULTS.md)）· **[源健康，每周更新](https://github.com/Battam1111/omniseek/blob/health-data/README.md)**

---

## 安装选项

顶部那两条命令是主路径：客户端自己经 stdio 启动 `omniseek`，没有端口，也不要 token。其余的都在 **[安装选项](../install.zh.md)**：

- Cursor 的 `mcp.json` 配置，以及想用 `pipx` 或普通 `pip` 的装法（在虚拟环境里用 `pip` 装时，要登记 `omniseek` 的完整路径，因为客户端不会激活那个环境）；
- 可选功能：`pdf`、`asr`（转写，要先装 PyTorch，各平台命令和占盘大小都写了）、`recall`、`ocr`、`walled`（要登录的网站）；
- 带 bearer token 的共享 HTTP 服务、一行 `docker run`、`docker compose`。

OmniSeek 的 HTTP 服务只绑定 `127.0.0.1`，每个请求都要 token。没有反向代理不要对外暴露（[SECURITY.md](../../.github/SECURITY.md)）。

---

## 工具

一条 MCP 连接；里面没有模型，也没有 Agent 循环。模型思考，客户端跑循环，OmniSeek 去取。从 `omniseek_search` 开始；用 `omniseek_sources` 看有哪些源。

| 工具 | 干什么 |
|------|--------|
| `omniseek_search` | 一次搜整个目录，去重、排序。能跨语言。 |
| `omniseek_read` | 把任意网址或文档（网页、PDF、arXiv）转成干净文本。 |
| `omniseek_view` | 看图片、文档插图、视频帧。 |
| `omniseek_transcribe` | 本地转写音视频，中英文，可从任意时间点开始。 |
| `omniseek_field_skeleton` | 画一个研究领域的引用关系：奠基的论文和最近的论文。 |
| `omniseek_resolve_identity` | 把人名对到各个数据库里的候选作者 ID。 |
| `omniseek_coauthors` | 按合著篇数列出一位研究者的合作者。 |
| `omniseek_institution_cohort` | 列出某个实验室里在某领域活跃发表的人。 |
| `omniseek_paper_enrich` | 一篇论文的开放获取 PDF、撤稿状态、被引数。 |
| `omniseek_paper_recommend` | 关键词搜不到的相似论文（SPECTER 向量）。 |
| `omniseek_graph` | 查 OmniSeek 找到的东西组成的本地关系图：find、neighborhood、between、since、similar。 |
| `omniseek_sensor` | 保存的搜索按计划重跑，只报新出现的结果。 |
| `omniseek_ruling` | 记下两个条目是（或不是）同一个人或同一件事。 |
| `omniseek_statement` | 记下两个条目之间的一条有向关系。 |
| `omniseek_curator_act` | 提议、测试、收录或退下一个源。 |
| `omniseek_curator_view` | 看候选源的队列，或某个源的报告。 |
| `omniseek_gather` | 几个工具并行跑，一次返回。 |
| `omniseek_sources` | 按领域、地区、能力、健康状况列出源。 |

要登录的网站没有专门的工具：某个源打开后，同一个 `omniseek_search(..., sources=["xiaohongshu"], raw=True)` 会经你自己已登录的浏览器去跑。见[要登录的网站（英文）](../walled-sources.md)。

完整参考见 **[tools.md](../tools.md)** · **[FAQ](../faq.md)**

在用 Claude Code？[`skills/omniseek-investigate`](../../skills/omniseek-investigate/SKILL.md) 把一套调研方法（先广搜、再收窄、再理结构）打包成了现成的 skill。

---

## 配置

不做任何配置时，公开的源都开着，要登录的网站都关着。全部调节集中在一个文件 `~/.omniseek/profile.json`（[示例](../../deploy/profile.example.json)）：

| 源的类型 | 默认 |
|------|------|
| 公开，不要 key | **开** |
| 要你提供的 API key（免费或付费） | 配好 key 就开 |
| 要你的登录 | **关**；用你自己的浏览器 |
| 要绕过访问控制 | **关**；默认一个也没有 |

完整参考：**[配置（英文）](../configuration.md)** · **[要登录的网站（英文）](../walled-sources.md)** · **[法律立场（英文）](../LEGAL-POSTURE.md)**

---

## 为什么自己部署

没有 OmniSeek 云端：无遥测、无账号、无中转。查询离开你的机器时，只以直连请求发往你启用的那些源，OmniSeek 不往这条路径里加任何第三方。登录态只留在你自己的浏览器里，只出示给它所属的网站；OmniSeek 不存储、不上传，也根本看不到你的密码。日积月累的搜索索引和关系图是你机器上的本地文件：哪天停用 OmniSeek，一切仍归你。

---

## 参与

见 [CONTRIBUTING.md](../../.github/CONTRIBUTING.md)。新源的门槛：必须在上面五件事之一上打赢普通网页搜索。修一个失效源的门槛：很低，欢迎来修。push 前跑 `python tests/smoke.py`。

参与即同意[行为准则](../../.github/CODE_OF_CONDUCT.md)。

<div align="center">

---

**给你的 AI Agent 接上网页搜索够不到的那部分互联网。**

[Apache-2.0](../../LICENSE) · [NOTICE](../../NOTICE) · [Security](../../.github/SECURITY.md) · [引用本项目](../../CITATION.cff)

</div>
