# Configuration

<sub>[penumbra](../README.md)&nbsp;·&nbsp;**Configuration**&nbsp;·&nbsp;[Tools](tools.md)&nbsp;·&nbsp;[Patterns](patterns.md)&nbsp;·&nbsp;[Walled sources](walled-sources.md)&nbsp;·&nbsp;[Brand](BRAND.md)</sub>

Penumbra is **catalog-first**: it ships a classified set of sources and you choose what to enable.
With no configuration at all, every benign default source is on and every login-walled source is
off. None of this page is needed for a first run; it is the full reference for when you want to tune.

---

## The profile

All configuration lives in one file, `~/.penumbra/profile.json`, seeded from
[`profile.example.json`](../profile.example.json). It is a **sparse delta** over the shipped pack:
write only what you change. Narrow or widen by source name, domain, region, and access tier.

```jsonc
{
  "sources": {
    "default_enabled": true,      // every benign source on unless disabled below
    "disable": ["glassdoor"],     // drop specific sources by name
    "enable":  []                 // force-on a source the defaults skip
  },
  "walled": { "enabled": false, "bring_your_own": {} }   // see Walled sources
}
```

Point `PENUMBRA_PROFILE_PATH` at another location to relocate the file.

## Access tiers

Every source declares an `access_tier`. It is the legal-posture axis, and it decides what runs by
default.

| Tier | What it is | Default | You provide |
|------|------------|---------|-------------|
| **free** | public, no key | **on** | nothing |
| **keyed** | a free or paid API key | on once a key is present | the key |
| **walled** | content behind a login you are entitled to | **off** | your own logged-in browser |
| **circumvention** | requires defeating an access control | **off**; none in the default pack | your own adapter + legal judgment |

Route at call time with `penumbra_sources(domain=..., query=...)`; each source reports its
`access_tier`, so an agent can filter by legal posture instead of memorizing the set.

## Keyed sources

A keyed adapter stays **silent** until you give it a key, so an empty result usually just means
"no key set." Keys live in `~/.penumbra/credentials/<source>.json`, outside the project tree so they
are never committed. On its first import each keyed adapter drops a `<source>.json.template` next to
it with the sign-up URL inline: copy it to `<source>.json` and fill in the values.

```bash
python scripts/creds_doctor.py     # which keyed sources are configured vs missing (presence only, never secrets)
```

<details>
<summary><b>Where to get a key</b> (most are free)</summary>

<br>

| Source | Fields | Sign up |
|--------|--------|---------|
| CORE (full-text papers) | `api_key` | <https://core.ac.uk/services/api> |
| Adzuna (jobs) | `app_id`, `app_key` | <https://developer.adzuna.com/signup> |
| Podcast Index (`podcastindex`) | `key`, `secret` | <https://api.podcastindex.org/signup> |
| Bluesky | `handle`, `app_password` | <https://bsky.app/settings/app-passwords> |

Every keyed adapter drops its own template with the get-key URL inline, so `creds_doctor.py` and
the templates together are always the authoritative, up-to-date list.

</details>

### Polite-pool contact

OpenAlex, Crossref, SEC, and Unpaywall give a faster lane to requests that carry a contact email.
Set `PENUMBRA_CONTACT_EMAIL`, or write `~/.penumbra/credentials/contact.json` as `{"email": "..."}`.
Left unset, Penumbra falls back to a reserved placeholder, so a cold checkout still forms a valid
request.

## Wrapping an MCP server as a source

Any external MCP server (streamable HTTP) can become an ordinary source with **one declarative
row** in `src/penumbra/core/sources/sources.json`: set `"transport": "mcp"`, the server's
`endpoint`, the retrieval `tool` to call, a `params_template` for its arguments (`{query}` /
`{limit}` slots; a value that is exactly `"{limit}"` is passed as an integer), and the usual
`results_path` + `field_map` over the tool's result. Everything else (caching, ranking, facets,
routing, the memory that accretes from results) works exactly as for an http row. If the server
needs auth, put its headers in `~/.penumbra/credentials/mcp_<name>.json`
(`{"headers": {...}}`); a `needs_credentials` row without the file stays silently inert.
A wrapped server is admitted like any source: it must beat plain search per the contributing
razor, judged per server. The row schema is documented in `core/sources/_declarative.py`.

## Walled sources

Xiaohongshu, Zhihu, Douyin, and other login-only platforms are read through a browser **you** run
and log into; Penumbra never sees your password. They are off until you opt in, per source. See
**[walled sources](walled-sources.md)** for the full browser-and-CDP setup.

## Advanced

<details>
<summary><b>Semantic recall</b> (optional vector layer)</summary>

<br>

Cross-lingual, paraphrase-tolerant recall uses a local embedding model. Install the `recall` extra
and place the model at `~/.penumbra/models/qwen3-embedding-0.6b`. Without it, recall fail-opens to
the lexical FTS5 index, so search still works, just without the vector layer.

</details>

<details>
<summary><b>Audio transcription</b> (optional ASR)</summary>

<br>

Install the `asr` extra and `penumbra_transcribe` is available; nothing to download by hand. On its
**first call**, it pulls the SenseVoice-Small + VAD model weights from ModelScope automatically (a
one-time download; the wait depends on your connection). The weights land in `~/.penumbra/models`
(the same persisted root as credentials and the recall index), so a container rebuild does not
trigger it again.

</details>

<details>
<summary><b>Circumvention tier</b> (legal posture)</summary>

<br>

The access-tier system classifies every source by legal posture: `free`, `keyed`, `walled`, and
`circumvention`. The first three ship in the default pack; circumvention does not.

A circumvention source is one that **defeats an access control** (decrypts an encrypted response,
circumvents a paywall, bypasses a rate-limit enforcement). The engine's tier-detection logic
(`_CIRCUMVENTION_RE`) auto-classifies any adapter whose `explicit_only` reason mentions these
patterns. No circumvention-class sources currently ship in the public catalog.

**Why it exists as a tier**: the framework is designed so that deployers who choose to build or
contribute circumvention-class adapters can do so as opt-in `explicit_only` sources, gated behind
the same profile consent mechanism as walled sources. The project does not ship them by default,
but it does not reject the category: the deployer's jurisdiction, legal counsel, and Terms of
Service obligations determine what is appropriate for their deployment.

**Legal responsibility**: if you build, install, or enable a circumvention-class source, you are
responsible for compliance with the laws and platform terms in your jurisdiction. Penumbra provides
the classification framework and the opt-in gate; the legal judgment is yours. See
[SECURITY.md](../.github/SECURITY.md).

</details>

<details>
<summary><b>Server environment</b> (variables)</summary>

<br>

| Variable | Default | Purpose |
|----------|---------|---------|
| `PENUMBRA_HTTP_HOST` | `127.0.0.1` | bind address; the container sets `0.0.0.0` behind a loopback port-map |
| `PENUMBRA_HTTP_PORT` | `8765` | HTTP port |
| `PENUMBRA_PROFILE_PATH` | `~/.penumbra/profile.json` | relocate the profile |
| `PENUMBRA_CONTACT_EMAIL` | (placeholder) | polite-pool contact for OpenAlex / Crossref / SEC / Unpaywall |
| `PENUMBRA_CDP_POOL` | off | keep a persistent CDP connection per walled browser |
| `PENUMBRA_ALLOW_NETS` | (none) | extra CIDRs the SSRF guard may reach (advanced) |
| `PENUMBRA_DOC_ROOTS` | inbox only | additional roots `penumbra_read` (document branch) may read |

</details>

---

<div align="center"><sub><a href="../README.md">← back to the README</a></sub></div>
