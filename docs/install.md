# Install options

<sub>[OmniSeek](../README.md)&nbsp;·&nbsp;**Install**&nbsp;·&nbsp;[Configuration](configuration.md)&nbsp;·&nbsp;[Tools](tools.md)&nbsp;·&nbsp;[FAQ](faq.md)&nbsp;·&nbsp;[中文](install.zh.md)</sub>

OmniSeek runs on your own machine. Your MCP client (Claude Code, Cursor, and others) starts it as a
local command and talks to it over stdio: no port, no token, no Docker. Everything below the first
section is optional.

**Contents:** [Quick install](#quick-install) · [Other ways to install](#other-ways-to-install) ·
[Connect your client](#connect-your-client) · [Optional extras](#optional-extras) ·
[Podcast and video transcription](#podcast-and-video-transcription) ·
[Advanced: HTTP service](#advanced-http-service) · [Advanced: Docker](#advanced-docker) ·
[Troubleshooting](#troubleshooting)

---

## Quick install

Needs Python 3.11 or newer. [uv](https://docs.astral.sh/uv/getting-started/installation/) downloads
a suitable Python for you if your system only has an older one.

```bash
uv tool install omniseek
claude mcp add omniseek -- omniseek
```

Then run `claude mcp list`; the `omniseek` line should end in `✔ Connected`.

`uv tool install` puts the `omniseek` command in `~/.local/bin` (Windows: `%USERPROFILE%\.local\bin`).
If uv warns that this folder is not on your `PATH`, run `uv tool update-shell` and open a new
terminal. Without that step Claude Code cannot find the command and reports
`Executable not found in $PATH: "omniseek"`.

Don't have uv yet? `curl -LsSf https://astral.sh/uv/install.sh | sh` on macOS and Linux,
`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"` on Windows, or
`brew install uv`.

Recommended once: some sources render pages in a headless browser. Download it with

```bash
uvx --from omniseek playwright install chromium
```

(uvx prints a hint about `--from playwright`; it is harmless.) Upgrade later with
`uv tool upgrade omniseek`.

## Other ways to install

**pipx** (needs Python 3.11+ itself; on macOS the system Python is 3.9, so point it at a newer one):

```bash
pipx install --python python3.12 omniseek
claude mcp add omniseek -- omniseek
```

**pip in a virtual environment.** This works, but the `omniseek` command then exists only inside that
environment, and Claude Code does not activate it. Register the absolute path instead of the bare name:

```bash
python3.12 -m venv ~/.omniseek-venv
~/.omniseek-venv/bin/pip install omniseek
claude mcp add omniseek -- ~/.omniseek-venv/bin/omniseek
```

(Windows: `~\.omniseek-venv\Scripts\omniseek.exe`.) If you register the bare name `omniseek` from a
virtual environment, `claude mcp list` shows `✘ Failed to connect` with `ENOENT: Executable not found`.

**From a clone** (to hack on it): `git clone https://github.com/Battam1111/omniseek && cd omniseek`,
then `uv tool install --editable .` or `pip install -e .` in a Python 3.11+ virtual environment.

## Connect your client

**Claude Code:** `claude mcp add omniseek -- omniseek` (add `--scope user` to make it available in
every project).

**Cursor:** add this to `~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` (one project):

```json
{
  "mcpServers": {
    "omniseek": {
      "command": "omniseek"
    }
  }
}
```

Apps opened from the Dock or Start menu may not see your shell's `PATH`. If Cursor shows the server as
failed, replace `"omniseek"` with the full path that `which omniseek` (Windows: `where omniseek`)
prints, for example `"/Users/you/.local/bin/omniseek"`.

**Other MCP clients** (Claude Desktop, VS Code, Windsurf, Cline, and so on): the same block works
wherever the client takes a `command` for a stdio server.

## Optional extras

The plain install covers 200+ sources. Extras add abilities that need large or differently licensed
libraries. Add them in brackets, for example `uv tool install "omniseek[pdf,ocr]"`.

| Extra | What it adds | Notes |
|---|---|---|
| `pdf` | Reading PDF files and papers in full | Uses PyMuPDF (AGPL-3.0); you accept its license |
| `asr` | Transcribing podcasts and videos | Also needs PyTorch, see the next section |
| `recall` | Cross-language search over what your agent has already found | Pulls PyTorch through sentence-transformers |
| `ocr` | Reading text inside images and scanned pages | Pulls onnxruntime |
| `walled` | Sites behind your own login | Off until you turn it on; see [Sites behind a login](walled-sources.md) |

Sources that need an API key (CORE, Adzuna, Podcast Index, Bluesky) stay quiet until you add a key;
see [Configuration](configuration.md). Nothing that is on by default needs a key.

## Podcast and video transcription

Transcription uses FunASR, which needs PyTorch. PyTorch is not installed by the `asr` extra on
purpose: on Linux the default PyTorch wheel bundles NVIDIA CUDA libraries, which is about
200 MB for the CPU build versus well over 1.5 GB of downloads for the default build. Pick the line
for your machine:

| Machine | Command |
|---|---|
| macOS | `uv tool install "omniseek[asr]" --with torch --with torchaudio` |
| Windows | `uv tool install "omniseek[asr]" --with torch --with torchaudio` |
| Linux, no NVIDIA GPU | `uv tool install "omniseek[asr]" --with torch --with torchaudio --torch-backend cpu` |
| Linux with an NVIDIA GPU | `uv tool install "omniseek[asr]" --with torch --with torchaudio --torch-backend auto` |

With pip in a virtual environment, install PyTorch first and then the extra. On Linux without a GPU:
`pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu`, then
`pip install "omniseek[asr]"`. On macOS and Windows plain `pip install torch torchaudio` is fine.

Disk: on an Apple silicon Mac the installed tool grows from about 270 MB to about 1.3 GB. The first
transcription also downloads the speech model (about 1 GB, kept in a cache), so that first call is
slow (about two minutes in our test). Plan for about 2.5 GB in total.

## Advanced: HTTP service

Use HTTP when several clients or machines share one OmniSeek. The HTTP service always requires a
bearer token, read from `~/.omniseek/credentials/omniseek_http.json` (file mode 600).

```bash
# 1. the Python that uv installed OmniSeek into
PY="$(uv tool dir)/omniseek/bin/python"
# 2. create a token once
"$PY" -c "import json,secrets,pathlib; p=pathlib.Path.home()/'.omniseek/credentials/omniseek_http.json'; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps({'token': secrets.token_urlsafe(32)})); p.chmod(0o600); print(p)"
# 3. start the service on 127.0.0.1:8765
"$PY" -m omniseek.serve_http
```

(With a virtual environment, use its `python` instead of `$PY`. From a clone, `scripts/bootstrap.sh`
creates the token and a default profile and downloads the browser; then run step 3.)

Connect Claude Code, with the token from that file:

```bash
claude mcp add --transport http omniseek http://127.0.0.1:8765/mcp --header "Authorization: Bearer <token>"
```

`OMNISEEK_HTTP_HOST` and `OMNISEEK_HTTP_PORT` change the address. Binding anything other than the
loopback address makes OmniSeek reachable from other machines: put it behind a firewall or reverse
proxy and keep the token secret, because it drives tools that use your own logins.

## Advanced: Docker

A prebuilt image is published for amd64 and arm64 as `ghcr.io/battam1111/omniseek` (about 2.7 GB
unpacked; it includes Chromium). It runs the HTTP service on port 8765.

```bash
docker run -d --name omniseek -p 127.0.0.1:8765:8765 -v "$HOME/.omniseek-docker:/root/.omniseek" -v "$HOME/omniseek-inbox:/root/omniseek-inbox" ghcr.io/battam1111/omniseek
```

On first start it creates a token, prints it in `docker logs omniseek`, and saves it to
`~/.omniseek-docker/credentials/omniseek_http.json` on your machine. Settings, cache and downloaded
models live in that folder too, so they survive `docker rm`; files you want OmniSeek to read go in
`~/omniseek-inbox`. Connect with the `claude mcp add
--transport http` line above. Check it is up with `curl http://127.0.0.1:8765/healthz`.

**docker compose.** The repo's `docker-compose.yml` runs the same prebuilt image and keeps its state in
`./.omniseek` next to the file (token: `./.omniseek/credentials/omniseek_http.json`):

```bash
git clone https://github.com/Battam1111/omniseek && cd omniseek
docker compose up -d
```

To build the image yourself instead (for example with extras baked in), use the `build` profile:

```bash
EXTRAS="[pdf]" docker compose --profile build up -d --build omniseek-build
```

Only run one of the two services at a time; both use port 8765.

**Docker over stdio.** `Dockerfile.stdio` builds a variant that speaks stdio instead of HTTP, for
clients that start the container themselves.

## Troubleshooting

| What you see | Cause and fix |
|---|---|
| `ERROR: Could not find a version that satisfies the requirement omniseek` | Your `pip` belongs to Python 3.10 or older. Use `uv tool install omniseek`, or a Python 3.11+ virtual environment. |
| `claude mcp list` shows `✘ Failed to connect` and `Executable not found in $PATH` | The command is not on `PATH`. Run `uv tool update-shell` and open a new terminal, or register the absolute path. |
| `this feature needs the optional 'asr' dependencies` | Install the `asr` extra and PyTorch as in [Podcast and video transcription](#podcast-and-video-transcription). |
| `refusing to start: cannot stat token file` | The HTTP service has no token yet. Create it as in step 2 of [Advanced: HTTP service](#advanced-http-service). Stdio needs no token. |
| A source that renders pages returns nothing | Download the browser: `uvx --from omniseek playwright install chromium`. |

More answers in the [FAQ](faq.md).
