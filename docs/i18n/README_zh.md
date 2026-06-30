<div align="center">

<picture>
  <source media="(prefers-color-scheme: light)" srcset="../../assets/logo-icon.png">
  <img src="../../assets/logo-hero-dark.png" width="320" alt="Penumbra">
</picture>

# penumbra

**表层之下。**

[![License](https://img.shields.io/badge/License-Apache_2.0-D4952B?style=flat-square)](../../LICENSE)
&nbsp;![Python](https://img.shields.io/badge/Python_3.11+-D4952B?style=flat-square)
&nbsp;![Built for MCP](https://img.shields.io/badge/built_for-MCP-D4952B?style=flat-square)
&nbsp;![Self-hosted](https://img.shields.io/badge/self--hosted-D4952B?style=flat-square)

[快速开始](#快速开始) · [工作原理](#工作原理) · [配置](#配置) · [工具](#工具) · [贡献](#贡献)

**Languages:** [English](../../README.md) · 中文 · [日本語](README_ja.md)

</div>

---

你只能基于你找到的东西行动。你能找到的,只有表层。

表层之下:一期播客的第 47 分钟有你需要的洞察,从没被转写过。一份付费墙后的 PDF 里有改变一切的数字。一个你不懂的语言写成的论坛帖,上周被删了,里面有人说清了你正要踩的坑。

都在。都可以被找到。无人触及。

<h3 align="center">这就是半影区。</h3>

<div align="center">

任何搜索给你看到的,和实际存在的之间,那片巨大的若隐若现的地带。
不是机密。不是开放网络。
**是中间那片:知识真实存在,散落各处,结构性地碰不到。**

</div>

<br>

**为什么碰不到?** 锁在音频、视频、图片里,文本搜索解析不了。用你不懂的语言写成。藏在登录墙和付费墙后面。而且是流动的:昨天还在的帖子,明天可能就没了。无论你用什么工具,撞上的都是同样的壁垒。

**Penumbra 穿过它们。**

一个自托管的深度检索引擎。转写音频,消化文档,从视频和图像中提取,遍历引用图谱,监控变化。一个不断生长的开源策展源目录(目前数百个,任何人可扩展),横跨深层网络。讲 MCP 协议,任何 AI agent、工作流或应用直接接入。

**但碎片不是知识。** 一百条散落的发现是噪声。信号,是当一条英文薪资帖、一条中文论坛上提到的招聘冻结、一期未转写播客里的一句无心之言,从三个独立角度指向同一个结论,而没有任何单一来源能看到全貌。Penumbra 为你的 agent 提供做到这一点所需的一切:每条发现标注出处、时间戳和独立性,附带对"哪里没能触及"的精确度量。你的 agent 推理的依据,是结构化的、可溯源的、标注了盲区的证据,而非对表层的自信复述。

**想象一下,什么问题变得可以回答了。** 你的领域里谁认识谁,跨越每一种语言追踪? 一份监管文件和一场翻译过的财报会议之间,藏着什么信号? 那些需要数年耳濡目染的内行知识,几个月能不能系统积累? 在表层:无解。在半影区:可解。这些不过是我们想到的第一批。

<div align="center">

*优势从来不在于谁更聪明。*
*在于谁能进入半影区。*

**现在,你能了。**

</div>

---

## 快速开始

### Docker(推荐)

```bash
docker compose up -d
docker compose logs penumbra        # 首次启动时会打印 bearer token,复制它
curl -s http://127.0.0.1:8765/healthz
```

将你的 MCP 客户端指向 `http://127.0.0.1:8765/mcp`,携带请求头
`Authorization: Bearer <token>`。token 在首次启动时自动生成,
存储在 `./.penumbra/credentials/http.json`。

可选扩展(你需接受其许可,参见 [NOTICE](../../NOTICE)):
在 `docker-compose.yml` 中设置 `EXTRAS="[pdf,asr,walled]"`。

### 不使用 Docker

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
./bootstrap.sh                                    # 安装 + chromium + token + 默认 profile
python -m penumbra.serve_http                      # 启动于 http://127.0.0.1:8765
```

Linux 常驻服务参见 [`deploy/penumbra.service`](../../deploy/penumbra.service)。

## 工作原理

Penumbra 是**基础设施,不是应用**。它位于原始互联网和你的 AI agent 之间,将不可触及的半影区转化为结构化的、可检索的证据。

```
                        ┌─────────────────────────────────┐
  你 / 你的 agent       │           penumbra              │       半影区
  ──────────────── MCP ─┤                                 ├─── 登录墙、语言壁、
                        │  源 · 检索 · 标注 · 去重        │     付费墙、音频、
                        │  盲区度量 · 变化监控            │     视频、图像、
                        │                                 │     删帖、引用图谱
                        │  持续生长的目录 (数百+)          │
                        └─────────────────────────────────┘
```

你的 agent 发送查询。Penumbra 在源目录中扇出检索,穿越壁垒(登录墙、语言、付费墙、模态、时间),为每条发现标注来源与出处,跨独立上游去重,返回结构化证据并附带一份精确的"哪里没能触及"的地图。你的 agent 负责推理。Penumbra 确保它推理的依据是深度,而非表层。

源目录**开放且持续生长**:目前已有数百个策展源,任何人都能增加。每个源必须通过一种特定模式胜过普通搜索来赢得一席之地:**structure**(搜索无法干净返回的结构化数据)、**unwall**(运营者有权访问的登录墙后内容)、**transcribe**(agent 无法直接阅读的音视频)、**recall**(开放网络会遗忘的纵向信息流)、或 **monitor**(命名的、可监控的信息源)。

## 配置

Penumbra 采用**源目录优先**设计:预置一套分类好的源,由你(或你的 agent)选择启用哪些。无配置 = 所有良性默认源开启,登录墙源关闭。

`~/.penumbra/profile.json`(从 [`profile.example.json`](../../profile.example.json) 播种)
可按源名、领域、地区和访问层级缩放:

| 层级 | 含义 | 默认 |
|------|------|------|
| free | 公开,无需密钥 | 开 |
| keyed | 需要你提供的免费/付费 API 密钥 | 有密钥则开 |
| walled | 你有权访问的登录墙后内容 | 关 |
| circumvention | 需要突破访问控制 | 关,不随附 |

通过 `penumbra_list_sources(domain=..., query=...)` 按需路由,无需记忆源列表;
每个源报告其 `access_tier`,agent 可按法律合规需求过滤。

**Keyed 源**(CORE 全文、Adzuna 职位、Podcast Index、Bluesky …)需要你自己提供 API 密钥,大多免费。
没有密钥时适配器只会静默返空,所以"查不到结果"往往只是"没配密钥"。密钥放在
`~/.penumbra/credentials/<source>.json`,在项目树之外,不会被提交。每个 keyed 适配器首次导入时会在旁边
落一个 `<source>.json.template`,内联注明申请密钥的网址(如 CORE 的 `https://core.ac.uk/services/api`):
把它复制成 `<source>.json` 填好即可。随时运行 `python scripts/creds_doctor.py` 查看哪些 keyed 源已配置、
哪些还缺(只报有无,绝不打印密钥)。

**墙内源**(小红书、知乎、抖音 …)通过**你自己**运行并登录的浏览器读取,Penumbra 从不接触你的密码。
接入方法见 [docs/walled-sources.md](../walled-sources.md)。

## 工具

Penumbra 通过 MCP 暴露以下工具族:

| 类别 | 工具 |
|------|------|
| **搜索** | `penumbra_search` · `penumbra_search_ranked` · `penumbra_fetch` · `penumbra_list_sources` · `penumbra_add_url` |
| **论文 + 引用** | `penumbra_paper_enrich` · `penumbra_paper_recommend` · `penumbra_field_skeleton` |
| **人物 + 机构** | `penumbra_resolve_identity` · `penumbra_coauthors` · `penumbra_institution_cohort` |
| **文档 + 视觉** | `penumbra_read_document` · `penumbra_view_doc_images` · `penumbra_view_images` · `penumbra_view_video_frames` |
| **音频** | `penumbra_transcribe` |
| **健康 + 策展** | `penumbra_health_check` · `penumbra_curator_*`(自迭代源获取) |

权威列表以服务器实际注册为准;`penumbra_list_sources()` 返回完整的能力索引。

## 安全 + 责任

- **默认绑定回环,token 守门。** 默认 `127.0.0.1`;无 bearer token 拒绝启动;非回环绑定时大声警告。未经反向代理不要对外暴露。
- **SSRF 防护。** 所有出站请求锁定 IP 并拒绝内网地址;`penumbra_read_document` 沙箱化到白名单收件箱。
- **不可信内容。** Penumbra 返回的一切都是外部数据,不是指令。
- **你的责任。** Penumbra 以你自己 agent 的身份进行检索。你有责任在你所在的司法管辖区内合法合规地使用它。参见 [SECURITY.md](../../SECURITY.md) 和 [NOTICE](../../NOTICE)。

## 目录结构

```
src/penumbra/
  server.py            MCP 工具层 (penumbra_* 工具)
  serve_http.py        HTTP 传输层 (token 守门,默认回环)
  core/                 检索引擎: fetcher · rank · normalize · cache
                       profile · _netguard (SSRF) · enrich · asr · curator
  core/sources/         适配器: api/ · scrape/ · walled/
tests/smoke.py         离线不变量 + 黄金 fixture (CI 闸门)
```

## 贡献

参见 [CONTRIBUTING.md](../../CONTRIBUTING.md)。新源的门槛:必须通过一种模式
(structure / unwall / transcribe / recall / monitor)胜过普通搜索。
修复衰变源的门槛:低,欢迎。提交前跑 `python tests/smoke.py`。

<div align="center">

---

**表层之下。**

[Apache-2.0](../../LICENSE) · [NOTICE](../../NOTICE) · [Security](../../SECURITY.md)

</div>
