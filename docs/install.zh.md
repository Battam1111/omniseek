# 安装方式

<sub>[OmniSeek](i18n/README_zh.md)&nbsp;·&nbsp;**安装**&nbsp;·&nbsp;[配置（英文）](configuration.md)&nbsp;·&nbsp;[工具（英文）](tools.md)&nbsp;·&nbsp;[常见问题（英文）](faq.md)&nbsp;·&nbsp;[English](install.md)</sub>

OmniSeek 跑在你自己的机器上。MCP 客户端（Claude Code、Cursor 等）把它当作本机命令启动，经 stdio
（标准输入输出）和它通信：不开端口、不要 token、不用 Docker。第一节以外的内容都是可选的。

**目录：** [快速安装](#快速安装) · [其他安装方式](#其他安装方式) · [接到客户端](#接到客户端) ·
[可选功能](#可选功能) · [播客与视频转写](#播客与视频转写) · [进阶：HTTP 服务](#进阶http-服务) ·
[进阶：Docker](#进阶docker) · [常见问题排查](#常见问题排查)

---

## 快速安装

需要 Python 3.11 或更新的版本。系统里只有旧版 Python 也没关系，[uv](https://docs.astral.sh/uv/getting-started/installation/)
会自动下载合适的版本。

```bash
uv tool install omniseek
claude mcp add omniseek -- omniseek
```

然后运行 `claude mcp list`，`omniseek` 那一行末尾应是 `✔ Connected`。

`uv tool install` 把 `omniseek` 命令放在 `~/.local/bin`（Windows 是 `%USERPROFILE%\.local\bin`）。
如果 uv 提示这个目录不在 `PATH` 里，运行 `uv tool update-shell` 再开一个新终端。不做这一步，
Claude Code 找不到命令，会报 `Executable not found in $PATH: "omniseek"`。

还没装 uv：macOS 和 Linux 用 `curl -LsSf https://astral.sh/uv/install.sh | sh`，Windows 用
`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`，也可以 `brew install uv`。

建议装完做一次：有些源要用无界面浏览器打开网页，先把浏览器下载好：

```bash
uvx --from omniseek playwright install chromium
```

（uvx 会提示可以改用 `--from playwright`，不用管。）以后升级用 `uv tool upgrade omniseek`。

## 其他安装方式

**pipx**（pipx 本身要用 Python 3.11 以上；macOS 自带的是 3.9，要指定新版本）：

```bash
pipx install --python python3.12 omniseek
claude mcp add omniseek -- omniseek
```

**在虚拟环境里用 pip。** 能用，但 `omniseek` 命令只在这个虚拟环境里，Claude Code 不会替你激活它。
所以登记时写绝对路径，不写裸命令名：

```bash
python3.12 -m venv ~/.omniseek-venv
~/.omniseek-venv/bin/pip install omniseek
claude mcp add omniseek -- ~/.omniseek-venv/bin/omniseek
```

（Windows 是 `~\.omniseek-venv\Scripts\omniseek.exe`。）如果在虚拟环境里登记裸命令名 `omniseek`，
`claude mcp list` 会显示 `✘ Failed to connect` 和 `ENOENT: Executable not found`。

**从源码装**（想改代码时）：`git clone https://github.com/Battam1111/omniseek && cd omniseek`，然后
`uv tool install --editable .`，或在 Python 3.11 以上的虚拟环境里 `pip install -e .`。

## 接到客户端

**Claude Code：** `claude mcp add omniseek -- omniseek`（加 `--scope user` 就在所有项目里都能用）。

**Cursor：** 把下面这段加进 `~/.cursor/mcp.json`（所有项目）或 `.cursor/mcp.json`（单个项目）：

```json
{
  "mcpServers": {
    "omniseek": {
      "command": "omniseek"
    }
  }
}
```

从程序坞或开始菜单打开的应用不一定能读到终端里的 `PATH`。Cursor 里显示连接失败时，把 `"omniseek"`
换成 `which omniseek`（Windows 用 `where omniseek`）打印的完整路径，例如 `"/Users/you/.local/bin/omniseek"`。

**其他 MCP 客户端**（Claude Desktop、VS Code、Windsurf、Cline 等）：只要客户端能用 `command` 启动 stdio
服务，同一段配置就能用。

## 可选功能

默认安装就覆盖 200 多个源。下面这些功能要用体积大或许可证不同的库，用方括号加上，例如
`uv tool install "omniseek[pdf,ocr]"`。

| 名字 | 加了什么 | 说明 |
|---|---|---|
| `pdf` | 完整读 PDF 文件和论文 | 用 PyMuPDF（AGPL-3.0 许可），装了即表示你接受它的许可 |
| `asr` | 转写播客和视频 | 还要装 PyTorch，见下一节 |
| `recall` | 跨语言搜索你的 Agent 之前找到过的内容 | 经 sentence-transformers 带进 PyTorch |
| `ocr` | 读图片和扫描页里的文字 | 带进 onnxruntime |
| `walled` | 用你自己的登录态读要登录才能看的网站 | 你打开之前一直是关的，见 [要登录的网站（英文）](walled-sources.md) |

默认开着的源几乎都不需要 API key。有四个默认源填了凭据会更好用，见
[配置（英文）](configuration.md)：Bluesky 和 CORE 在你填之前不出结果；GitHub 没有 token 时按匿名
限额查，并跳过代码搜索；OpenReview 大多数论坛要账号登录才读得到。要付费或注册 key、又没有免费
退路的源（Adzuna、Podcast Index、Exa 等）默认是关的。

## 播客与视频转写

转写用的是 FunASR，它需要 PyTorch。`asr` 有意不自动装 PyTorch：Linux 上默认的 PyTorch 安装包带着
NVIDIA CUDA 库，CPU 版下载约 200 MB，默认版连同 CUDA 库要下载 1.5 GB 以上。按你的机器选一行：

| 机器 | 命令 |
|---|---|
| macOS | `uv tool install "omniseek[asr]" --with torch --with torchaudio` |
| Windows | `uv tool install "omniseek[asr]" --with torch --with torchaudio` |
| Linux，没有 NVIDIA 显卡 | `uv tool install "omniseek[asr]" --with torch --with torchaudio --torch-backend cpu` |
| Linux，有 NVIDIA 显卡 | `uv tool install "omniseek[asr]" --with torch --with torchaudio --torch-backend auto` |

在虚拟环境里用 pip 时，先装 PyTorch 再装 `asr`。没有显卡的 Linux：
`pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu`，然后
`pip install "omniseek[asr]"`。macOS 和 Windows 直接 `pip install torch torchaudio` 就行。

磁盘：在 Apple 芯片的 Mac 上，装好的工具从约 270 MB 涨到约 1.3 GB。第一次转写还要下载语音模型
（约 1 GB，下载后缓存），所以第一次调用较慢（我们实测约两分钟）。总共按 2.5 GB 左右准备。

## 进阶：HTTP 服务

几个客户端或几台机器共用一个 OmniSeek 时用 HTTP。HTTP 服务必须有 bearer token，从
`~/.omniseek/credentials/omniseek_http.json` 读取（文件权限 600）。

```bash
# 1. uv 装 OmniSeek 用的那个 Python
PY="$(uv tool dir)/omniseek/bin/python"
# 2. 生成一次 token
"$PY" -c "import json,secrets,pathlib; p=pathlib.Path.home()/'.omniseek/credentials/omniseek_http.json'; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps({'token': secrets.token_urlsafe(32)})); p.chmod(0o600); print(p)"
# 3. 在 127.0.0.1:8765 上启动服务
"$PY" -m omniseek.serve_http
```

（用虚拟环境的，把 `$PY` 换成那个环境里的 `python`。从源码装的，`scripts/bootstrap.sh` 会生成 token、
默认配置并下载浏览器，之后运行第 3 步。）

用那个文件里的 token 接 Claude Code：

```bash
claude mcp add --transport http omniseek http://127.0.0.1:8765/mcp --header "Authorization: Bearer <token>"
```

`OMNISEEK_HTTP_HOST` 与 `OMNISEEK_HTTP_PORT` 改地址和端口。绑定回环地址以外的地址，别的机器就能连上：
要放在防火墙或反向代理后面，并保管好 token，因为它能驱动用你登录态的工具。

## 进阶：Docker

预构建镜像 `ghcr.io/battam1111/omniseek` 支持 amd64 与 arm64（解压后约 2.7 GB，内含 Chromium），
在 8765 端口跑 HTTP 服务。

```bash
docker run -d --name omniseek -p 127.0.0.1:8765:8765 -v "$HOME/.omniseek-docker:/root/.omniseek" -v "$HOME/omniseek-inbox:/root/omniseek-inbox" ghcr.io/battam1111/omniseek
```

第一次启动时它会生成 token，打印在 `docker logs omniseek` 里，并存到本机的
`~/.omniseek-docker/credentials/omniseek_http.json`。配置、缓存和下载的模型也在这个目录里，
`docker rm` 之后还在；要 OmniSeek 读的文件放进 `~/omniseek-inbox`。连接用上面那条
`claude mcp add --transport http`。用 `curl http://127.0.0.1:8765/healthz` 看它起来没有。

**docker compose。** 仓里的 `docker-compose.yml` 跑的是同一个预构建镜像，状态存在文件旁边的
`./.omniseek`（token 在 `./.omniseek/credentials/omniseek_http.json`）：

```bash
git clone https://github.com/Battam1111/omniseek && cd omniseek
docker compose up -d
```

想自己构建镜像（例如把可选功能装进去），用 `build` 这个 profile：

```bash
EXTRAS="[pdf]" docker compose --profile build up -d --build omniseek-build
```

两个服务同时只跑一个，它们都用 8765 端口。

**经 stdio 用 Docker。** `Dockerfile.stdio` 构建一个走 stdio 而不是 HTTP 的版本，给自己启动容器的客户端用。

## 常见问题排查

| 看到的 | 原因与解决 |
|---|---|
| `ERROR: Could not find a version that satisfies the requirement omniseek` | 你的 `pip` 属于 Python 3.10 或更旧的版本。改用 `uv tool install omniseek`，或 Python 3.11 以上的虚拟环境。 |
| `claude mcp list` 显示 `✘ Failed to connect` 和 `Executable not found in $PATH` | 命令不在 `PATH` 里。运行 `uv tool update-shell` 再开新终端，或者登记绝对路径。 |
| `this feature needs the optional 'asr' dependencies` | 照 [播客与视频转写](#播客与视频转写) 装 `asr` 和 PyTorch。 |
| `refusing to start: cannot stat token file` | HTTP 服务还没有 token。照 [进阶：HTTP 服务](#进阶http-服务) 第 2 步生成。stdio 不需要 token。 |
| 要打开网页的源什么都没返回 | 下载浏览器：`uvx --from omniseek playwright install chromium`。 |

更多回答见 [常见问题（英文）](faq.md)。
