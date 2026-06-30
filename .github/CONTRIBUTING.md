# Contributing to Penumbra

Thanks for helping grow Penumbra. It is a self-hosted, general deep-retrieval MCP: a catalog of
sources an agent can search for depth the open web can't give. The bar for a new source is high on
purpose (see below); the bar for fixing a decayed one is low (please do).

## Setup

```bash
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
./bootstrap.sh                                   # install + chromium + token + profile
python tests/smoke.py                            # must pass before you push
python -m penumbra.serve_http                      # run it; curl http://127.0.0.1:8765/healthz
```

Core install is Apache-clean. Optional extras carry their own licenses (see [NOTICE](../NOTICE)):
`pip install -e '.[pdf,asr,walled]'`.

## The admission razor (for a NEW source)

A source earns a slot only if its acquisition MODE beats plain web search, not by duplicating prose
the open web already returns. The modes:

- STRUCTURE: structured data web search can't cleanly return (citation graph, filings, an answer body).
- UNWALL: content behind a login the operator has a right to (conservative; their own account).
- TRANSCRIBE: speech/audio the agent can't otherwise read.
- RECALL: a longitudinal/monitored stream the open web forgets.
- MONITOR: a named, watchable per-entity feed.

If the candidate is "another general web page" with no mode edge, it will be rejected no matter how
easy it is to build. Fewer, sharper sources over a long tail of redundant ones.

## Adding a source (simplest shape wins)

1. Flat JSON API: a row in `sources.json` via the declarative adapter (zero `.py`).
2. RSS/Atom: the RSS base + a thin `*_source.py`.
3. Coded `BaseScrapeAdapter` when the flat boundary is crossed (signed requests, pagination, fan-out).
4. Walled (login/anti-bot): `sources/walled/<name>_source.py` via the shared CDP Chrome. Mark it
   `explicit_only` so the broad fan-out skips it, and `needs_credentials` if it needs a logged-in session.

Declare facets as CLASS ATTRS (`kind`, `domains`, `regions`, `modes`); never hand-edit `facets.json`.
Add an offline golden fixture to `tests/smoke.py` (recorded raw payload -> expected doc shape). Never
fabricate fields or signals: a field the source doesn't provide is absent, not guessed.

## Legal + safety (read before adding a walled or scrape source)

- Respect each site's Terms of Service and robots. A source that requires defeating access controls
  (decrypting an encrypted response, breaking a paywall you have no right to) is NOT accepted into the
  default pack: keep such adapters `explicit_only` and document the legal posture. See
  [SECURITY.md](SECURITY.md).
- Penumbra fetches as the OPERATOR's own agent. The deployer is responsible for using it within the law
  and the sites' terms in their jurisdiction.
- Never commit credentials, tokens, cookies, or personal data. State lives in `~/.penumbra`, never in-tree.

## Pull requests

- `python tests/smoke.py` passes; `python -m compileall src` is clean.
- No em-dash in human-facing text (use colon, comma, period, or parens).
- One source (or one fix) per PR where practical; explain the mode edge in the description.

## Layout

```
src/penumbra/
  server.py            MCP tool surface (penumbra_* tools)
  serve_http.py        HTTP transport (token-gated, loopback-default)
  core/                retrieval engine: fetcher · rank · normalize · cache
                       profile · _netguard (SSRF) · enrich · asr · curator
  core/sources/        adapters: api/ · scrape/ · walled/
tests/smoke.py         offline invariants + golden fixtures (CI gate)
```
