<div align="center">

<picture>
  <source media="(prefers-color-scheme: light)" srcset="../../assets/logo-icon.png">
  <img src="../../assets/logo-hero-dark.png" width="320" alt="Penumbra:日食标记,暗蓝球体与琥珀色受光边缘">
</picture>

# penumbra

**表层之下。**

面向 AI agent 的自托管深度检索 MCP 服务器。

[![CI](https://github.com/Battam1111/penumbra/actions/workflows/ci.yml/badge.svg)](https://github.com/Battam1111/penumbra/actions/workflows/ci.yml)
&nbsp;[![License](https://img.shields.io/badge/License-Apache_2.0-D4952B?style=flat-square)](../../LICENSE)
&nbsp;![Python](https://img.shields.io/badge/Python_3.11+-D4952B?style=flat-square)
&nbsp;![Built for MCP](https://img.shields.io/badge/built_for-MCP-D4952B?style=flat-square)
&nbsp;![Self-hosted](https://img.shields.io/badge/self--hosted-D4952B?style=flat-square)

[工作原理](#工作原理) · [快速开始](#快速开始) · [配置](#配置) · [贡献](#贡献)

**Languages:** [English](../../README.md) · 中文 · [日本語](README_ja.md)

</div>

---

你的 agent 再聪明,也只能基于它够得到的东西行动。现成的搜索够得到的,只是表层:写成文字、你看得懂、还留在原地的那一层。

表层之下:你在犹豫要不要加入一家创业公司,所有公开报道都说前景大好;三个月前一档播客第 52 分钟,创始人随口说了句"跑道大概还剩九个月",从没被转写。你在考虑一款评测全五星的产品;演示视频里,一行真实性能数据在屏幕上闪了不到两秒,和宣传差了十倍,没人念出来。你要决定搬不搬去一个城市,旅游博主都说宜居;当地人早就在一个论坛里写清楚了真相,但那是一种你不懂的语言。

都在。无人碰过。

<h3 align="center">这就是半影区。</h3>

<div align="center">

你的 agent 够得到的,和真实存在的,中间那片巨大的、若隐若现的地带。
不是机密,是盲区:
**知识就在那里,散落各处,只是对表层不可见。**

</div>

<br>

**为什么够不到?** 说出来的,搜索听不见;画面上的,搜索看不懂;另一种语言写的,你读不了;你登录了才看得到的,搜索进不去;昨天还在的,今天就没了。现成的搜索,每一堵都撞。换你自己一件件去够?够得完,可你没有那么多时间。

**Penumbra 让你的 agent 做到这些:** 把说出来的转写成文字,把画面上一闪而过的读出来,把另一种语言翻成你能读的,进到你登录了才看得到的地方,把已经删掉的拉回来,把散在几百条记录里的线索接上。这些原本够不到的碎片,一趟给你收回来。

**但光有一堆碎片,远远不够。** 一百条散落的发现只是噪声,直到它们对上:多个独立来源指向同一个结论,而没有任何一条说得全。Penumbra 给每块碎片标上出处、时间、和它是不是别处的回声,然后把散落在不同来源的线索编织在一起:同一个名字出现在三个不相关的地方,一条时间线只有拼起来才说得通,一段藏在记录缝隙里的关系。剩下的,才轮到你的 agent,拼出那张只属于你的图。

**还有第二种不公平的优势,和触及无关。** 绝大多数"知道"都会蒸发:你去年春天查到的,某个周二偶然注意到的,你曾想明白、并短暂而安静地正确过的那些。你身边所有人都在以同样的速度淡忘,所以没有人感到损失。你的,不必如此。

会议上,有人展示"本周的新发现",而你八个月前就见过它,那时它还只是互联网某个角落里一个工程师的抱怨。谎言换了身衣服第二次登门,满屋只有你认出它。终于有人问你为什么总是知道得早,而你有一个答案,答案上有日期。

所有人的每个早晨都从零开始。你的早晨,从你见过的一切开始。触及让你早知道一次;记忆让"早知道"成为习惯。

**想象一下,什么问题变得可以回答了。** 那款你天天在用的东西,好评如潮,可你查得到有几条是真正独立的?你行业里所有人都在传的那条内幕,到底是真的,还是同一个人在三个平台上编出来的?一个直接关系到你的风险,在另一种语言的世界里已经被讨论了两年,而你连它的存在都不知道?这些,只是先想到的第一批。

<div align="center">

*半影区一直在那里。*

**现在,够得到了。**

</div>

---

## 工作原理

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../../assets/demo-zh-dark.png">
    <img src="../../assets/demo-zh-light.png" alt="一个实例:你的 agent 问这里到底在发生什么。普通搜索只给表层:官方公告、评测、文档,都干净、一致。Penumbra 探到表层之下,带回四块任何单一搜索都浮不出的碎片:一场它转写的演讲,台上两人争执,暴露了官方通稿抹平的裂缝;一段它从画面读出的演示视频,一个数字闪过又从没说出口;上百条它连起来的记录,浮现出任何单页都没写明的关系;还有一个用你读不懂的语言写的论坛,一个知情者说了那句不该说的。单看,每一块都是噪声;拼起来,就成了只有你能拼出的那张图。Penumbra 的触及横跨音频、视频、图像、语言、关系、被删内容、登录源,以及更多。" width="780">
  </picture>
</div>

你的 agent 经 MCP 接入。入口是 **`penumbra_search`**;`penumbra_sources()` 展示可用能力。目录开放且持续生长:每个源靠胜过普通搜索来赢得席位。agent 取回的一切会累积成一张可追问的持久关系图(`penumbra_graph(view, args)`:`find` / `stats` / `neighborhood` / `between` / `voices` / `since` / `similar`,一个冻结的动词,视图作为数据生长);它的身份判断用 `penumbra_ruling` 记录(图在读取时应用你的判断,自己绝不判断)。完整工具清单见 **[tools](../tools.md)**。

## 快速开始

### Docker(推荐)

```bash
git clone https://github.com/Battam1111/penumbra.git && cd penumbra
docker compose up -d
docker compose logs penumbra        # 首次启动时会打印 bearer token,复制它
curl -s http://127.0.0.1:8765/healthz
```

将你的 MCP 客户端指向 `http://127.0.0.1:8765/mcp`,携带请求头
`Authorization: Bearer <token>`。token 在首次启动时自动生成,
存储在 `~/.penumbra/credentials/penumbra_http.json`(Docker 下挂载为 `./.penumbra/credentials/penumbra_http.json`)。

可选扩展(你需接受其许可,参见 [NOTICE](../../NOTICE)):
在 `docker-compose.yml` 中设置 `EXTRAS="[pdf,asr,walled]"`。

### 不使用 Docker

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
./bootstrap.sh                                    # 安装 + chromium + token + 默认 profile
python -m penumbra.serve_http                      # 启动于 http://127.0.0.1:8765
```

Windows 上请在 Git Bash 或 WSL 里运行 `bootstrap.sh`(它是 POSIX shell 脚本);Docker 是最省事的路径。

Linux 常驻服务参见 [`deploy/penumbra.service`](../../deploy/penumbra.service)。

Penumbra 绑定 `127.0.0.1`,每个请求都需要 bearer token。
未经反向代理不要对外暴露([SECURITY.md](../../.github/SECURITY.md))。

## 配置

Penumbra 采用**源目录优先**设计:无配置时,所有良性源开启、登录墙源关闭。全部调节集中在一个文件
`~/.penumbra/profile.json`(从 [`profile.example.json`](../../profile.example.json) 播种),按源名、领域、地区、访问层级缩放:

| 层级 | 默认 |
|------|------|
| **free**(公开,无需密钥) | **开** |
| **keyed**(你提供的免费/付费 API 密钥) | 配好密钥即开 |
| **walled**(你有权访问的登录墙) | **关**;自带浏览器 |
| **circumvention** | **关**;默认包中无此类源 |

完整参考见 **[configuration](../configuration.md)**;墙内源登录见 **[walled sources](../walled-sources.md)**。

## 贡献

参见 [CONTRIBUTING.md](../../.github/CONTRIBUTING.md)。新源的门槛:必须通过一种模式
(structure / unwall / transcribe / recall / monitor)胜过普通搜索。
修复衰变源的门槛:低,欢迎。提交前跑 `python tests/smoke.py`。

参与即表示你同意遵守[行为准则](../../.github/CODE_OF_CONDUCT.md)。

<div align="center">

---

**表层之下。**

[Apache-2.0](../../LICENSE) · [NOTICE](../../NOTICE) · [Security](../../.github/SECURITY.md)

</div>
