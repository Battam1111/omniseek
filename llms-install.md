# Installing OmniSeek (a guide written for AI agents)

OmniSeek is a self-hosted perception MCP server. There are two ways to run it; pick ONE based
on what the machine has.

## Option A: Docker (recommended when Docker is available)

```bash
git clone https://github.com/Battam1111/omniseek.git && cd omniseek
docker compose up -d
```

1. The first start generates a bearer token. Read it from the logs
   (`docker compose logs omniseek`, look for "generated a new bearer token") or from the file
   `./.omniseek/credentials/omniseek_http.json` on the host.
2. Health check: `curl http://127.0.0.1:8765/healthz` should answer.
3. Register the server with the MCP client:
   - transport: streamable HTTP
   - endpoint: `http://127.0.0.1:8765/mcp`
   - header: `Authorization: Bearer <token>`

## Option B: pip + stdio (no Docker; Python 3.11+)

```bash
pip install omniseek
python -m playwright install chromium
```

(The second command installs the headless browser the render-path sources use; skip it and
those sources degrade gracefully while everything else works.)

Then configure the client to launch the server itself over stdio. No port, no token:

```json
{
  "mcpServers": {
    "omniseek": {
      "command": "omniseek"
    }
  }
}
```

Optional extras (each pulls dependencies under their own licenses; see NOTICE):
`pip install "omniseek[pdf,asr,walled]"`.

## Verify the install

Call the `omniseek_sources` tool: it returns the source catalog and capability index. Then try
`omniseek_search` with any query. If both answer, the install is complete.

## Notes for the agent

- HTTP mode binds `127.0.0.1` only. Do not expose it without a reverse proxy.
- Login-walled sources ship OFF. They activate only when the human configures their own
  credentials in `~/.omniseek/profile.json` (see `deploy/profile.example.json`).
- Optional but recommended: set `OMNISEEK_CONTACT_EMAIL` so Crossref, SEC, and Unpaywall give
  the polite-contact fast lane.
