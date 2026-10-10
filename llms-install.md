# Installing OmniSeek (a guide written for AI agents)

OmniSeek is a self-hosted MCP server that lets AI agents search podcasts and videos, Chinese
communities, citation graphs, and sites behind your own login (off by default). The default
install runs it over stdio: the MCP client starts the `omniseek` command itself. No port, no
token, no Docker. Python 3.11+ is required; `uv` fetches it if the machine only has an older one.

## Step 1: install the command

```bash
uv tool install omniseek
```

If `uv` is missing, install it first (`curl -LsSf https://astral.sh/uv/install.sh | sh` on
macOS/Linux, `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
on Windows). If `uv tool install` warns that its bin folder is not on `PATH`, run
`uv tool update-shell`; the client will not find a command that is off `PATH`.

Do NOT use a plain `pip install omniseek` inside a virtual environment and then register the bare
name `omniseek`: the client does not activate that environment and fails with
`Executable not found in $PATH`. If you must use a virtual environment, register the absolute path
of its `omniseek` executable instead.

Recommended: download the headless browser some sources use (they degrade gracefully without it):

```bash
uvx --from omniseek playwright install chromium
```

## Step 2: register it with the client

Claude Code:

```bash
claude mcp add omniseek -- omniseek
```

Cursor (`~/.cursor/mcp.json`) and other clients that take a JSON config:

```json
{
  "mcpServers": {
    "omniseek": {
      "command": "omniseek"
    }
  }
}
```

Desktop apps may not inherit the shell `PATH`; if the server fails to start there, use the full
path printed by `which omniseek` as `command`.

## Verify the install

`claude mcp list` should show `omniseek` as `✔ Connected`. Then call the `omniseek_sources` tool
(it lists the sources) and `omniseek_search` with any query. If both answer, the install is
complete.

## Optional extras

Each pulls libraries under their own licenses (see NOTICE): `pdf` (PDF reading, AGPL PyMuPDF),
`asr` (podcast and video transcription), `recall`, `ocr`, `walled` (sites behind a login). Example:
`uv tool install "omniseek[pdf]"`. Transcription also needs PyTorch, installed per platform; the
exact commands and disk cost are in `docs/install.md`.

## Advanced: HTTP or Docker

Only when several clients share one server. Both need a bearer token; follow `docs/install.md`
(sections "Advanced: HTTP service" and "Advanced: Docker"). The Docker image is
`ghcr.io/battam1111/omniseek` and serves `http://127.0.0.1:8765/mcp`.

## Notes for the agent

- Sites behind a login ship OFF. They turn on only when the human configures their own account in
  `~/.omniseek/profile.json` (see `docs/walled-sources.md`). Do not turn them on for the human.
- Almost everything on by default works without an API key. Bluesky and CORE return nothing until
  the human adds credentials; GitHub without a token uses the anonymous rate limit; OpenReview needs
  an account login for most forums. See `docs/configuration.md`. Do not ask for keys up front.
- Optional: set `OMNISEEK_CONTACT_EMAIL` so Crossref, SEC, and Unpaywall serve requests in their
  faster lane.
