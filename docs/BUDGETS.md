# OmniSeek time budgets (the four clocks)

The load-bearing wall-clock budgets that bound every OmniSeek operation, with file provenance.

SINGLE SOURCE OF TRUTH for every number with a symbol is the symbol itself in the named file. The
tables below name each clock, its constant, and its provenance but DO NOT restate its value (so a
number lives in exactly one place and cannot drift). Only budgets with NO importable symbol carry an
inline value (they are documented, verify-by-provenance).

Provenance is cited by symbol name + file (grep-able, rot-free).

## The four clocks

### 1. Search caller-patience (the fan-out deadline)
How long a `search_many` / `search_ranked` fan-out waits for sources before returning what responded
(stragglers keep running detached and warm the cache).

| Scope | Constant | Provenance |
| --- | --- | --- |
| broad, cache-allowed (default) | `fetcher._SOURCE_DEADLINE_S` | `src/omniseek/core/fetcher.py` |
| broad, `fresh=True` (cold + contended) | `fetcher._SOURCE_DEADLINE_FRESH_S` | `src/omniseek/core/fetcher.py` |
| named / scoped (`sources=[...]`) | `fetcher._EXPLICIT_DEADLINE_S` | `src/omniseek/core/fetcher.py` |

An explicit `deadline_s=` argument overrides all three; `deadline_s=None` selects the scope default
above (it does NOT mean unbounded here; that override belongs to `fetch_one`, clock 4).

### 2. cache_only ranked pickup
A defensive ceiling on a cache-only ranked collect (egresses short-circuit anyway, so this only
bounds the assembly, not real network). No importable symbol -> value inline.

| Budget | Value | Provenance |
| --- | --- | --- |
| cache_only ranked ceiling | 8 s | `src/omniseek/server.py` (literal `_deadline_s = 8`) |

### 3. omniseek_gather batch
The parallel read-only batch primitive: a per-call `wait_s` default with a hard ceiling so a hung
batch can never stall the worker.

| Budget | Constant | Provenance |
| --- | --- | --- |
| gather `wait_s` default (60 s, no symbol) | (function default) | `src/omniseek/server.py` |
| gather ceiling | `server._GATHER_TIMEOUT` | `src/omniseek/server.py` |
| gather max calls | `server._GATHER_MAX` | `src/omniseek/server.py` |

### 4. fetch_one single-source backstop
The backstop for one named-source fetch (`afetch_one` and its siblings: past it the caller gets `[]`
and the fetch task finishes detached, `_arun_bounded`); `deadline_s=None` is the deliberate UNBOUNDED
override (a slow source the caller truly wants complete; guards the prewarm contract).

| Budget | Constant | Provenance |
| --- | --- | --- |
| fetch_one default backstop | `fetcher._FETCH_ONE_DEADLINE_S` | `src/omniseek/core/fetcher.py` |
| fetch_one unbounded override | `deadline_s=None` (no clock) | `src/omniseek/core/fetcher.py` `_aderive_outcome` |

## Adjacent clocks (health + fetch_url)

| Budget | Constant | Provenance |
| --- | --- | --- |
| health probe, per-source hard cap (default; an adapter's `health_timeout_s` replaces it for every caller, resolved in `ahealth_check_outcome`) | `fetcher._HEALTH_TIMEOUT_S` | `src/omniseek/core/fetcher.py` |
| health probe, worker fan-out width | `fetcher._HEALTH_WORKERS` | `src/omniseek/core/fetcher.py` |
| health probe, aggregate backstop (~35 s, no symbol) | `_HEALTH_TIMEOUT_S + 10` | `src/omniseek/core/fetcher.py` |
| fetch_url per-adapter cap (no aggregate) | `fetcher._FETCH_URL_TIMEOUT_S` | `src/omniseek/core/fetcher.py` |

