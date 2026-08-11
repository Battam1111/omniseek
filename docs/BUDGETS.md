# Eye time budgets (the four clocks)

The load-bearing wall-clock budgets that bound every eye operation, with file provenance. This doc
is a REGRESSION RAIL: the S0.5 smoke golden (`tests/smoke.py`, prefix `S0:`) imports each live
constant SYMBOL and asserts it equals the value in the DRIFT-GUARD block below.

SINGLE SOURCE OF TRUTH for the guarded numbers is the DRIFT-GUARD block. The prose tables below name
each clock, its constant, and its provenance but DO NOT restate the guarded value (so a number lives
in exactly one place and cannot drift). Only budgets with NO importable symbol carry an inline value
(they are documented, verify-by-provenance, not machine-guarded).

Provenance is cited by symbol name + file (grep-able); line numbers are as of the S0 landing and
may drift, but the symbol binding is what the guard enforces.

## The four clocks

### 1. Search caller-patience (the fan-out deadline)
How long a `search_many` / `search_ranked` fan-out waits for sources before returning what responded
(stragglers keep running detached and warm the cache; see I1 straggler retention).

| Scope | Constant | Provenance |
| --- | --- | --- |
| broad, cache-allowed (default) | `fetcher._SOURCE_DEADLINE_S` | `src/penumbra/eye/fetcher.py:43` |
| broad, `fresh=True` (cold + contended) | `fetcher._SOURCE_DEADLINE_FRESH_S` | `src/penumbra/eye/fetcher.py:44` |
| named / scoped (`sources=[...]`) | `fetcher._EXPLICIT_DEADLINE_S` | `src/penumbra/eye/fetcher.py:45` |

An explicit `deadline_s=` argument overrides all three; `deadline_s=None` selects the scope default
above (it does NOT mean unbounded here; that override belongs to `fetch_one`, clock 4).

### 2. cache_only ranked pickup
A defensive ceiling on a cache-only ranked collect (egresses short-circuit anyway, so this only
bounds the assembly, not real network). No importable symbol -> value inline.

| Budget | Value | Provenance |
| --- | --- | --- |
| cache_only ranked ceiling | 8 s | `src/penumbra/server.py:516` (literal `_deadline_s = 8`) |

### 3. penumbra_gather batch
The parallel read-only batch primitive: a per-call `wait_s` default with a hard ceiling so a hung
batch can never stall the worker.

| Budget | Constant | Provenance |
| --- | --- | --- |
| gather `wait_s` default (60 s, no symbol) | (function default) | `src/penumbra/server.py:1561` |
| gather ceiling | `server._GATHER_TIMEOUT` | `src/penumbra/server.py:1507` |
| gather max calls | `server._GATHER_MAX` | `src/penumbra/server.py:1506` |

### 4. fetch_one single-source backstop
The daemon-thread backstop for one named-source fetch; `deadline_s=None` is the deliberate UNBOUNDED
override (a slow source the caller truly wants complete; guards the prewarm contract).

| Budget | Constant | Provenance |
| --- | --- | --- |
| fetch_one default backstop | `fetcher._FETCH_ONE_DEADLINE_S` | `src/penumbra/eye/fetcher.py:380` |
| fetch_one unbounded override | `deadline_s=None` (no clock) | `src/penumbra/eye/fetcher.py` `_derive_outcome` |

## Adjacent clocks (health + fetch_url)

| Budget | Constant | Provenance |
| --- | --- | --- |
| health probe, per-source hard cap | `fetcher._HEALTH_TIMEOUT_S` | `src/penumbra/eye/fetcher.py:1765` |
| health probe, worker fan-out width | `fetcher._HEALTH_WORKERS` | `src/penumbra/eye/fetcher.py:1768` |
| health probe, aggregate backstop (~35 s, no symbol) | `_HEALTH_TIMEOUT_S + 10` | `src/penumbra/eye/fetcher.py:1800` |
| fetch_url per-adapter cap (no aggregate) | `fetcher._FETCH_URL_TIMEOUT_S` | `src/penumbra/eye/fetcher.py:382` |

## Drift-guard bindings (machine-read by the S0.5 golden; the SINGLE source for these values)

The smoke golden parses the `module.SYMBOL = value` lines between the sentinels below, imports each
symbol live, and asserts equality. These are the only guarded values; the inline literals above
(cache_only 8 s, gather default 60 s, health aggregate +10) have no symbol to bind.

<!-- DRIFT-GUARD:BEGIN -->
```
fetcher._SOURCE_DEADLINE_S = 11
fetcher._SOURCE_DEADLINE_FRESH_S = 16
fetcher._EXPLICIT_DEADLINE_S = 45
fetcher._FETCH_ONE_DEADLINE_S = 90.0
fetcher._FETCH_URL_TIMEOUT_S = 30.0
fetcher._HEALTH_TIMEOUT_S = 25
fetcher._HEALTH_WORKERS = 24
server._GATHER_TIMEOUT = 120
server._GATHER_MAX = 10
```
<!-- DRIFT-GUARD:END -->
