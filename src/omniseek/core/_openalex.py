"""Shared OpenAlex plumbing: one client, one breaker, one work-parser.

OpenAlex backs 40+ named sources (openalex, researcher_watch, every org_watch
row) plus the cartographer. Before this module, three source files each carried
their own copy of the HTTP call, the inverted-index abstract reconstruction and
the work-to-fields parsing; and a dead upstream degraded a third of OmniSeek with
no shared protection. Here:

  get_json()             keyed client (api_key → the raised per-key credit budget),
                         one gentle retry honoring Retry-After
  circuit breaker        consecutive failures open the circuit briefly, so a dead
                         upstream fails FAST instead of stacking 20s timeouts
                         across 40 sources (same idea as the Brave breaker)
  reconstruct_abstract() the {word: [positions]} inverted index back to prose
  parse_work()           the common fields of a work record

Judgment-free plumbing only; callers keep their own caching and doc assembly.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

import anyio
import httpx

from omniseek.core import auth, diag, http, upstreams

logger = logging.getLogger(__name__)

BASE = "https://api.openalex.org"
_BASE_HOST = "api.openalex.org"
# Contact is host-injected, never a hardcoded personal address (see auth.contact_email).
USER_AGENT = f"omniseek/0.1 (mailto:{auth.contact_email()}; automated retrieval)"
TIMEOUT = 20

_BREAK_AFTER = 5      # consecutive failures that open the circuit
_BREAK_FOR_S = 120.0  # seconds the circuit stays open

# OpenAlex's usage model, CURRENT terms (read 2026-09-28; the declaration "openalex" in upstreams.json
# carries the sources): help.openalex.org/api-reference/authentication (updated 2026-08-19) and
# /access/example-costs/ (2026-08-09) say a free api_key gets $1.00 of usage per day and a caller WITHOUT
# a key gets $0.10 per day ("a free key raises your daily budget 10x"), both resetting at midnight UTC,
# counted separately. Cost per call: get-by-id free, list+filter $0.0001, search $0.001, content $0.01.
# A keyless call on 2026-09-28 answered x-ratelimit-limit 1000 / x-ratelimit-limit-usd 0.1, i.e. the
# keyless bucket is ONE TENTH of the keyed one. (The comment here until 2026-09-28 said "each bucket
# $1/day, ~2x daily capacity": that was the 2026-06-17 reading and is no longer true.)
#
# So the keyless lane is a small overflow, not a second budget: get_json tries the keyed lane first and
# spills to the keyless lane on a budget-429, and it decides from READINGS, not from an assumed size:
# every response's x-ratelimit-* headers are recorded per lane (_note_remaining -> _usage["lanes"]),
# a lane whose last reading shows nothing left before its reported reset is skipped, and a budget-429
# marks the lane dry until the reset the headers report (x-ratelimit-reset seconds), not a guessed hour.
# usage_stats() shows both lanes' reported limit and remaining, so the real capacity is visible. The
# mailto in USER_AGENT is a courtesy contact only (the polite pool ended 2026-02-13). The concurrency
# cap + rate pacer + breaker remain the load bounds, independent of the credit budget.
#
# Load bounds: the declared gate (8 in flight, request starts >= 0.2 s apart, i.e. <= 5/s against the
# published 100/s) so a fan-out across the 40+ OpenAlex-backed sources (the health sweep, the org_watch
# cron, a workflow's cohort burst) can never spike the rate and 429 the shared key. The semaphore bounds
# CONCURRENCY, the pacer bounds RATE; together a burst is impossible by construction (the root cause of
# the self-DOS the all-at-once health probe used to cause). The numbers live in upstreams.json.
# Hard cap on how long ONE caller may wait on the rate gate. Without it, an OA 429 / budget-exhaustion
# storm (40+ OA sources fanning out, each retry re-reserving a slot) grows the backlog unboundedly and
# a fresh caller inherits the WHOLE queue — the same unbounded-pace-wait bug that made an S2
# field_skeleton sit 886s on its gate (brain: eye-s2-rate-gate-hang-2026-06-21). Past this, fail fast
# (raise OpenAlexDown → caller degrades to cache/empty) instead of hanging for minutes.
# The value is the declared gate.max_wait_s of "openalex" (driver ruling 2, 2026-09-29): ONE budget for
# the start wait and the permit wait of an attempt together, cut to the caller's own deadline.
_PACE_MAX_WAIT_S = upstreams.max_wait("openalex")

# The shared load-guard (concurrency cap + rate pacer + circuit breaker): the registry's ONE OpenAlex
# BackendGuard, built from the declared gate (upstreams.json "openalex"), so any other egress to
# api.openalex.org (the shared http client's host gate) queues on the same object. The breaker state
# dict + its lock, the semaphore and the pace lock/state live on the guard; the module reaches them by
# name below so every threshold, sleep, log message and error path is unchanged. extra_state carries the
# per-lane budget-exhaustion reset (monotonic deadline): the keyed and keyless budgets are SEPARATE
# ($1.00 and $0.10 a day); when one 429s "insufficient budget" it is marked dry until its reported reset.
_guard = upstreams.guard("openalex", extra_state={"dry_until": {"keyed": 0.0, "anon": 0.0}}, log=logger)
_MAX_CONCURRENCY = _guard.max_inflight
_MIN_INTERVAL_S = _guard.min_interval_s
_state = _guard.state   # health probes + lane selection read fails / open_until / last_429 / dry_until
_lock = _guard.lock
_sema = _guard.sema
_pace_state = _guard.pace_state   # read-only backlog probe; aliases the guard's slot reservation
_pace_lock = _guard.pace_lock


def _load_api_key() -> Optional[str]:
    from omniseek.core import auth  # local import: keep module import cheap + acyclic
    import os

    creds = auth.load("openalex") or {}
    return creds.get("api_key") or os.environ.get("OPENALEX_API_KEY") or None


# Loaded once at import (mirrors _s2's keyed-client pattern): None when no key file exists, so
# get_json's injection is a no-op and behavior is unchanged until ~/.omniseek/credentials/openalex.json
# is dropped on the host. Committing the code before the key exists is therefore safe.
_api_key = _load_api_key()

# Pooled client: reuse one keep-alive connection to api.openalex.org across the 40+ OpenAlex-
# backed sources (researcher_watch fan-out + 39 org_watch + openalex + cartographer/field_skeleton)
# instead of a fresh TCP+TLS handshake per call (~0.5-1.5s to the overseas endpoint). httpx.Client
# is thread-safe; the global _sema still bounds in-flight concurrency. HTTP/2 multiplexing if h2
# is importable, else HTTP/1.1 keep-alive.
_client: Optional["httpx.Client"] = None
_client_lock = threading.Lock()


def _http2_ok() -> bool:
    try:
        import h2  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _get_client() -> "httpx.Client":
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = httpx.Client(
                    headers={"User-Agent": USER_AGENT},
                    timeout=TIMEOUT,
                    http2=_http2_ok(),
                    # follow_redirects=False (the SSRF hardening, attack-3): get_json is reached
                    # with a candidate-page-parsed OpenAlex work-id (attacker-influenceable). The
                    # API answers 200 JSON; a 3xx is the off-host redirect attack -> refuse it
                    # explicitly. With the host assert in get_json + the _OA_ID_RE path constraint,
                    # three independent constraints close the redirect/host/path injection.
                    follow_redirects=False,
                    limits=httpx.Limits(max_keepalive_connections=16, max_connections=32,
                                        keepalive_expiry=30.0),
                    event_hooks=http.progress_hooks(),   # the body's progress renews the lease
                )
    return _client


class OpenAlexDown(RuntimeError):
    """Raised immediately while the circuit is open (recent consecutive failures)."""


def _slot_busy(wait: float) -> OpenAlexDown:
    """Concurrency-permit exhaustion -> the same degrade-to-cache/empty path as breaker-open / budget
    dry / a pathological rate backlog. Handed to _guard.slot / _guard.aslot as their on_busy factory."""
    return OpenAlexDown(f"concurrency pool saturated (no slot in {wait:.0f}s); degrade")


def breaker_open() -> bool:
    """True iff the shared OpenAlex circuit is currently open (recent consecutive failures). A
    non-probing read of the breaker state, so callers can stamp a degraded flag without an
    upstream probe (cheap; never spends budget)."""
    with _lock:
        return time.time() < _state["open_until"]


def unavailable() -> bool:
    """True iff OpenAlex cannot serve a request right now WITHOUT a live probe: the circuit is open
    (recent consecutive failures) OR every budget lane is dry (keyed and keyless, each until the reset
    it reported). A non-probing read (no upstream call, no budget spend), using the same lane
    selection as get_json, so a caller can choose to serve a stale last-good fallback instead of a blind
    empty when the only reason for [] is OpenAlex being down (vs a genuine no-match)."""
    with _lock:
        if time.time() < _state["open_until"]:
            return True
    return not _open_lanes({})


def _late(wait: float) -> OpenAlexDown:
    return OpenAlexDown(f"rate-gate slot {wait:.0f}s away, past this caller's budget; degrade")


def _pace(until: Optional[float] = None):
    """Reserve the next request-start slot (>= _MIN_INTERVAL_S after the previous), then wait for it.
    The slot reservation is under the lock; the wait is NOT, so callers do not serialize on the lock
    itself, only on the wire-rate. Bounds OpenAlex requests/second across all callers + threads. A slot
    past ``until`` (the attempt's budget) is not reserved: OpenAlexDown. Returns the reservation."""
    return _guard.pace(on_backlog=_late, until=until)


def _pace_backlog_s() -> float:
    """How long the NEXT request would wait on the rate gate, read-only (no reservation). >
    _PACE_MAX_WAIT_S means the backlog is pathological (a 429 storm) and get_json fails fast instead
    of inheriting the whole queue. The fast-fail lives at get_json's top (beside the breaker check),
    not in _pace(), so it never tangles with the in-loop retry/fails bookkeeping."""
    return _guard.pace_backlog_s()


def _retry_after(resp, default: float = 0.0) -> float:
    """The Retry-After header in seconds (OpenAlex sends seconds-until-reset), else ``default``."""
    try:
        return float(resp.headers.get("retry-after") or default)
    except (ValueError, TypeError):
        return default


def _is_budget_429(resp) -> bool:
    """True iff this 429 is daily-CREDIT exhaustion (this bucket is spent), not a transient rate blip.
    OpenAlex 2026 stamps ``x-ratelimit-remaining: 0`` + a JSON body {"dailyRemainingUsd": 0, ...}."""
    if getattr(resp, "status_code", None) != 429:
        return False
    if (resp.headers.get("x-ratelimit-remaining") or "").strip() == "0":
        return True
    try:
        b = resp.json()
        return (b.get("dailyRemainingUsd") == 0 or b.get("creditsRemaining") == 0
                or "insufficient budget" in (b.get("message") or "").lower())
    except Exception:  # noqa: BLE001
        return False


# --- per-caller OpenAlex usage attribution (lightweight: surface a hidden over-consumer) ---------
# Every budget-spending success is tallied by OmniSeek component that drove it (cartographer /
# relations / org_watch / the openalex search source / researcher_watch / enrich), with the live
# per-bucket remaining. In-memory (resets on restart; `window_hours` reports the span). Exposed via
# omniseek_health_check so any day's OpenAlex breakdown is INSPECTABLE, not inferred.
_usage_lock = threading.Lock()
_usage: dict = {"since": None, "by_caller": {}, "spilled_to_anon": 0,
                "remaining": {"keyed": None, "anon": None},
                # per lane, the last x-ratelimit-* reading (see _note_remaining)
                "lanes": {}}


def _caller_tag() -> str:
    """The nearest stack frame OUTSIDE this module = OmniSeek component that drove the call."""
    import sys
    f = sys._getframe(2)  # 0=_caller_tag, 1=get_json, 2=the immediate caller
    for _ in range(15):
        if f is None:
            break
        mod = f.f_globals.get("__name__", "")
        if mod != __name__:
            return f"{mod.rsplit('.', 1)[-1]}:{f.f_code.co_name}"
        f = f.f_back
    return "unknown"


_READING_KEYS = ("limit", "remaining", "limit-usd", "remaining-usd", "reset", "cost-usd")


def _hdr_num(resp, name: str) -> Optional[float]:
    """A header's number, or None when it has none: missing, empty, not a number, or not finite
    ("nan" / "inf" would break the int() of a reading and turn a good response into a failure)."""
    try:
        v = resp.headers.get(name)
        if v is None or str(v).strip() == "":
            return None
        x = float(v)
    except (AttributeError, TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _note_remaining(bucket: str, resp) -> None:
    """Record this lane's budget as OpenAlex reports it on any response (200 or 429): the
    x-ratelimit-* headers (credits and USD, limit and remaining, seconds to the midnight-UTC reset,
    this call's cost). The readings drive the lane choice (_lane_known_dry), usage_stats() and the
    upstream health block (upstreams.observe), so capacity is what OpenAlex says, not an assumption."""
    if resp is None:
        return
    # per-lane budgets: the module honours a budget 429 itself (upstreams.SELF_BACKOFF)
    upstreams.observe("openalex", getattr(resp, "headers", {}) or {},
                      getattr(resp, "status_code", None), lane=bucket, defer_on_429=False)
    reading = {k: _hdr_num(resp, "x-ratelimit-" + k) for k in _READING_KEYS}
    if all(v is None for v in reading.values()):
        return
    reading["observed_at"] = time.time()
    if reading.get("reset") is not None:
        reading["resets_at"] = reading["observed_at"] + reading["reset"]
    with _usage_lock:
        _usage.setdefault("lanes", {})[bucket] = reading
        if reading.get("remaining") is not None:
            _usage.setdefault("remaining", {})[bucket] = int(reading["remaining"])


def _lane_known_dry(bucket: str, billable: bool = True) -> bool:
    """True iff this lane's LAST reading showed nothing left and the reset it reported has not
    passed, for a BILLABLE call: skip the lane without spending a request that would 429 (a
    budget-429 also marks it dry, via dry_until). A get-by-id call costs nothing, so an empty lane
    is still tried for it."""
    if not billable:
        return False
    with _usage_lock:
        r = (_usage.get("lanes") or {}).get(bucket) or {}
    rem, rem_usd, resets_at = r.get("remaining"), r.get("remaining-usd"), r.get("resets_at")
    empty = (rem is not None and rem <= 0) or (rem_usd is not None and rem_usd <= 0)
    return bool(empty and resets_at and time.time() < resets_at)


# get-by-id ("singleton") calls are free under the usage pricing; lists, filters and searches are not.
_SINGLETON_RE = re.compile(r"^/(works|authors|sources|institutions|topics|publishers|funders|"
                           r"concepts|keywords|awards)/[^/?]+$")


def _billable(path: str, params: Optional[dict]) -> bool:
    p = params or {}
    return not (_SINGLETON_RE.match(path or "") and not (p.get("filter") or p.get("search")))


def _open_lanes(base: dict, billable: bool = True) -> list:
    """The budget lanes to try, in order (keyed first, then the keyless overflow), leaving out a lane
    that is dry: marked by a budget-429 until its reported reset, or read empty before its reset."""
    now = time.monotonic()
    with _lock:
        dry = dict(_state["dry_until"])
    lanes = []
    if _api_key and now >= dry.get("keyed", 0.0) and not _lane_known_dry("keyed", billable):
        lanes.append(("keyed", {**base, "api_key": _api_key}))
    if now >= dry.get("anon", 0.0) and not _lane_known_dry("anon", billable):
        lanes.append(("anon", dict(base)))
    return lanes


def _seconds_to_reset(resp) -> float:
    """How long a budget-exhausted lane stays dry: the x-ratelimit-reset OpenAlex reports (seconds
    to midnight UTC), else Retry-After, else the actual seconds to the next midnight UTC. Capped a day."""
    for secs in (_hdr_num(resp, "x-ratelimit-reset"), _retry_after(resp) or None):
        if secs and secs > 0:
            return min(secs, 86400.0)
    now = datetime.now(timezone.utc)
    return float(86400 - (now.hour * 3600 + now.minute * 60 + now.second)) or 86400.0


def _record_ok(caller: str, bucket: str) -> None:
    """Tally one budget-spending success against its caller (+ a spill-to-anon counter)."""
    with _usage_lock:
        if _usage["since"] is None:
            _usage["since"] = time.time()
        _usage["by_caller"][caller] = _usage["by_caller"].get(caller, 0) + 1
        if bucket == "anon" and _api_key:
            _usage["spilled_to_anon"] += 1


def usage_stats() -> dict:
    """Snapshot of OpenAlex usage by caller since process start (surfaced by omniseek_sources
    check_health), plus each budget lane as OpenAlex last REPORTED it: limit and remaining in credits
    and USD, seconds to reset, age of the reading. ``keyless_share_of_keyed`` is the keyless lane's
    reported daily limit over the keyed lane's, i.e. how much the spill adds (0.1 on 2026-09-28)."""
    with _usage_lock:
        by = dict(_usage["by_caller"])
        since = _usage["since"]
        lanes = {}
        now = time.time()
        for name, r in (_usage.get("lanes") or {}).items():
            lanes[name] = {k: r.get(k) for k in _READING_KEYS}
            lanes[name]["age_s"] = round(now - r.get("observed_at", now), 1)
        share = None
        ku, au = (lanes.get("keyed") or {}).get("limit-usd"), (lanes.get("anon") or {}).get("limit-usd")
        if ku and au is not None:
            share = round(au / ku, 3)
        return {
            "since_epoch": round(since, 1) if since else None,
            "window_hours": round((time.time() - since) / 3600, 2) if since else 0.0,
            "total_ok_calls": sum(by.values()),
            "by_caller": dict(sorted(by.items(), key=lambda kv: kv[1], reverse=True)[:25]),
            "spilled_to_anon": _usage["spilled_to_anon"],
            "remaining": dict(_usage["remaining"]),
            "lanes": lanes,
            "keyless_share_of_keyed": share,
        }


def get_json(path: str, params: Optional[dict] = None, timeout: float = TIMEOUT) -> dict:
    """GET api.openalex.org``path`` and return parsed JSON.

    Budget lanes (see the usage-model note above): the keyed lane FIRST, spilling to the keyless lane
    on a budget-429. The keyless lane is a tenth of the keyed budget, so the spill is a small overflow,
    and which lanes are tried comes from what OpenAlex last reported for each (_open_lanes). One gentle
    retry on a TRANSIENT 429/5xx (honoring Retry-After, capped 5s). While the breaker is open this
    raises ``OpenAlexDown`` at once; any final failure propagates so each caller degrades exactly
    as it did before (log + empty).
    """
    # Host pin (the SSRF hardening, attack-3): the resolved request host MUST be api.openalex.org.
    # path comes from a candidate-page-parsed work-id (_OA_ID_RE = W\d{6,}); assert the assembled
    # URL never resolves off-host (a crafted path with an authority or scheme can't redirect us).
    if (urlsplit(f"{BASE}{path}").hostname or "").lower() != _BASE_HOST:
        raise ValueError(f"openalex get_json refused: path {path!r} resolves off {_BASE_HOST}")

    with _lock:
        if time.time() < _state["open_until"]:
            _down = OpenAlexDown(f"circuit open {_state['open_until'] - time.time():.0f}s more")
            diag.note("openalex.get_json", url=f"{BASE}{path}", exc=_down)
            raise _down

    # Rate-gate backlog fast-fail (beside the breaker check): if the pacer would make this caller wait
    # absurdly long (a 429/budget storm piled the queue up), fail fast like an open circuit instead of
    # inheriting a multi-minute wait. Does NOT count as a transient fail (it is shed load, not an
    # upstream error), mirroring the circuit-open early raise above.
    _backlog = _pace_backlog_s()
    if time.monotonic() + _backlog > upstreams.wait_until(_PACE_MAX_WAIT_S):
        _down = OpenAlexDown(f"rate-gate backlog {_backlog:.0f}s, past this caller's budget "
                             f"(declared {_PACE_MAX_WAIT_S:.0f}s or the caller's deadline); OA storming, degrade")
        diag.note("openalex.get_json", url=f"{BASE}{path}", exc=_down)
        raise _down

    base = dict(params or {})
    # Budget lanes in preference order (keyed, then the keyless overflow), minus any lane that is dry
    # by its own last report (a budget-429 until the reset it reported, or read empty for a billable
    # call). Chosen from readings, not from an assumed size.
    lanes = _open_lanes(base, _billable(path, base))
    if not lanes:  # every lane's daily budget is spent → fail fast; the caller degrades to cached/empty
        _dry = OpenAlexDown(
            "OpenAlex daily budgets exhausted on every lane (keyed + keyless), per their last "
            "reported x-ratelimit readings; reset midnight UTC")
        diag.note("openalex.get_json", url=f"{BASE}{path}", exc=_dry)
        raise _dry

    caller = _caller_tag()  # attribute this call's budget spend to OmniSeek component that drove it
    last_exc: Optional[Exception] = None
    for lane_name, p in lanes:
        for attempt in (1, 2):
            try:
                # ONE budget per attempt for the start wait and the permit wait (driver ruling 2)
                until = upstreams.wait_until(_PACE_MAX_WAIT_S)
                res = _pace(until)  # rate cap: bounds req/s so a fan-out across 40+ sources can't burst a bucket
                # global concurrency cap, BOUNDED: a saturated/leaked pool degrades instead of hanging
                try:
                    # released before the retry sleep below
                    with _guard.slot(_PACE_MAX_WAIT_S, _slot_busy, until=until, request_s=timeout):
                        resp = _get_client().get(f"{BASE}{path}", params=p, timeout=timeout)
                except OpenAlexDown:  # only the slot's refusal raises it here: never sent (review F8)
                    _guard.refund(res)
                    raise
                _note_remaining(lane_name, resp)  # record this lane's reported budget (200 or 429)
                if resp.status_code == 429:  # stamp it so health() can surface it honestly
                    with _lock:
                        _state["last_429"] = time.time()
                    if _is_budget_429(resp):  # THIS lane's daily budget is spent → mark dry + spill
                        reset = _seconds_to_reset(resp)  # the reset OpenAlex reported, not a guess
                        with _lock:
                            _state["dry_until"][lane_name] = time.monotonic() + reset
                        last_exc = RuntimeError(
                            f"OpenAlex {lane_name} daily budget exhausted (429); resets in ~{int(reset)}s")
                        break  # spill to the next lane (no sleep: this one won't recover for hours)
                if resp.status_code in (429, 500, 502, 503) and attempt == 1:
                    time.sleep(min(_retry_after(resp, 1.5), 5.0))
                    continue
                resp.raise_for_status()
                _guard.record_ok()  # reset the consecutive-failure streak
                _record_ok(caller, lane_name)  # budget-spending success → tally to its caller
                return resp.json()
            except OpenAlexDown:  # the gate turned it away (we did not ask): no retry, no breaker
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt == 1:
                    time.sleep(1.0)
        # this bucket failed (budget-429 spill, or two transient failures) → try the next bucket
    _guard.record_fail()  # one failure; opens the circuit at _BREAK_AFTER consecutive (logs on open)
    # Every lane failed (budget-429 spill or two transient failures each). Surface the last
    # exception + the path so the fixing agent sees WHY OpenAlex gave nothing (429 budget, 5xx,
    # timeout): only on this real-failure exit, never the success return above.
    diag.note("openalex.get_json", url=f"{BASE}{path}", exc=last_exc)
    raise last_exc  # type: ignore[misc]


# --- native-async twin of get_json (the 40+ OpenAlex sources go native async) --------------------
# Byte-faithful mirror of get_json sharing the SAME _guard (breaker + rate pacer + concurrency sema)
# and the SAME 2-lane budget state, so async and sync egress are ONE load guard + ONE budget ledger.
# Only the BLOCKING waits go async (no thread held during them). No operation converted here changes
# get_json; this is a pure addition callers opt into via aget_json.
_aclient: Optional["httpx.AsyncClient"] = None
_aclient_lock = threading.Lock()  # construction is sync (no await), double-check like _get_client


def _aget_client() -> "httpx.AsyncClient":
    global _aclient
    if _aclient is None:
        with _aclient_lock:
            if _aclient is None:
                _aclient = httpx.AsyncClient(
                    headers={"User-Agent": USER_AGENT},
                    timeout=TIMEOUT,
                    http2=_http2_ok(),
                    follow_redirects=False,  # same SSRF hardening as _get_client (attack-3)
                    limits=httpx.Limits(max_keepalive_connections=16, max_connections=32,
                                        keepalive_expiry=30.0),
                    event_hooks=http.aprogress_hooks(),
                )
    return _aclient


async def aget_json(path: str, params: Optional[dict] = None, timeout: float = TIMEOUT) -> dict:
    """Native-async twin of ``get_json`` (byte-faithful): SAME host-pin, SAME shared breaker / rate
    pacer / concurrency cap / 2-lane budget / retry, so async and sync egress share ONE load guard and
    ONE budget ledger. Only the BLOCKING waits go async so no thread is held during them:
      - the rate-gate wait -> ``_guard.reserve_pace_slot()`` (reserve the slot, sync + brief) then
        ``await anyio.sleep`` (NOT time.sleep on the loop; the SAME shared pace state, NOT a new primitive);
      - the concurrency cap -> the SAME ``_sema``, waited for in its one line on the loop (``aslot``:
        no thread, bounded, leased), held only around the async network call (mirror get_json's slot);
      - the network -> ``await _aget_client().get`` (epoll, no held thread);
      - the retry backoffs -> ``await anyio.sleep`` (a time.sleep on the loop would freeze every coroutine).
    Everything else (host-pin, breaker check, lane selection, budget-429 dry/spill, record_ok/fail,
    _note_remaining, _record_ok, diag labels) is brief-lock / pure CPU, byte-identical to get_json. The
    ``aslot`` takes and returns the permit inside this coroutine, so no cancellation can leak it."""
    if (urlsplit(f"{BASE}{path}").hostname or "").lower() != _BASE_HOST:
        raise ValueError(f"openalex aget_json refused: path {path!r} resolves off {_BASE_HOST}")

    with _lock:
        if time.time() < _state["open_until"]:
            _down = OpenAlexDown(f"circuit open {_state['open_until'] - time.time():.0f}s more")
            diag.note("openalex.get_json", url=f"{BASE}{path}", exc=_down)
            raise _down

    _backlog = _pace_backlog_s()
    if time.monotonic() + _backlog > upstreams.wait_until(_PACE_MAX_WAIT_S):
        _down = OpenAlexDown(f"rate-gate backlog {_backlog:.0f}s, past this caller's budget "
                             f"(declared {_PACE_MAX_WAIT_S:.0f}s or the caller's deadline); OA storming, degrade")
        diag.note("openalex.get_json", url=f"{BASE}{path}", exc=_down)
        raise _down

    base = dict(params or {})
    lanes = _open_lanes(base, _billable(path, base))  # same reading-based lane choice as get_json
    if not lanes:
        _dry = OpenAlexDown(
            "OpenAlex daily budgets exhausted on every lane (keyed + keyless), per their last "
            "reported x-ratelimit readings; reset midnight UTC")
        diag.note("openalex.get_json", url=f"{BASE}{path}", exc=_dry)
        raise _dry

    caller = _caller_tag()
    last_exc: Optional[Exception] = None
    for lane_name, p in lanes:
        for attempt in (1, 2):
            try:
                # ONE budget per attempt for the start wait and the permit wait (driver ruling 2)
                until = upstreams.wait_until(_PACE_MAX_WAIT_S)
                res = _guard.reserve(_late, until=until)  # rate cap: reserve the slot (sync, brief)...
                sent = False
                try:
                    if res.wait > 0:
                        await anyio.sleep(res.wait)     # ...then wait WITHOUT holding a thread
                    # concurrency cap, polled in this coroutine (no worker thread, and no cancel can
                    # strand the permit) + BOUNDED; released before the retry sleep below (mirror get_json).
                    async with _guard.aslot(_PACE_MAX_WAIT_S, _slot_busy, until=until,
                                            request_s=timeout):
                        sent = True
                        resp = await _aget_client().get(f"{BASE}{path}", params=p, timeout=timeout)
                except BaseException:
                    if not sent:  # refused or cancelled before sending: hand the slot back (review F8)
                        _guard.refund(res)
                    raise
                _note_remaining(lane_name, resp)
                if resp.status_code == 429:
                    with _lock:
                        _state["last_429"] = time.time()
                    if _is_budget_429(resp):
                        reset = _seconds_to_reset(resp)  # the reset OpenAlex reported, not a guess
                        with _lock:
                            _state["dry_until"][lane_name] = time.monotonic() + reset
                        last_exc = RuntimeError(
                            f"OpenAlex {lane_name} daily budget exhausted (429); resets in ~{int(reset)}s")
                        break  # spill to the next lane (no sleep: this one won't recover for hours)
                if resp.status_code in (429, 500, 502, 503) and attempt == 1:
                    await anyio.sleep(min(_retry_after(resp, 1.5), 5.0))
                    continue
                resp.raise_for_status()
                _guard.record_ok()
                _record_ok(caller, lane_name)
                return resp.json()
            except OpenAlexDown:  # the gate turned it away (we did not ask): no retry, no breaker
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt == 1:
                    await anyio.sleep(1.0)
        # this bucket failed (budget-429 spill, or two transient failures) → try the next bucket
    _guard.record_fail()
    diag.note("openalex.get_json", url=f"{BASE}{path}", exc=last_exc)
    raise last_exc  # type: ignore[misc]


_HEALTH_TTL_S = 60.0
_health: dict = {"at": 0.0, "result": None}
_health_lock = threading.Lock()


def health(timeout: float = 8.0) -> tuple[Optional[bool], str]:
    """ONE shared upstream probe for all 40+ OpenAlex-backed sources (single-flight + 60s cache).

    Before this, openalex + researcher_watch + every org_watch row each probed OpenAlex in its own
    health_check; the health sweep fired all 40 at once, bursting the shared key into 429 and tripping
    the breaker, so one transient probe storm read as "40 sources down" and degraded them all. Now
    they delegate here: one minimal filter call (per-page=1, select=id) tests connectivity + key
    validity + the breaker/rate state, cached 60s and single-flighted (the probe runs under the lock)
    so 40 concurrent callers cause exactly ONE upstream call. A recent 429 is surfaced even when this
    probe succeeds, because get_json may have reached OpenAlex via the keyless lane while the keyed
    lane is exhausted (the two budgets are SEPARATE, $1.00 and $0.10 a day), so a recent budget-429
    stays legible even on an OK verdict, with each lane's last reported remaining."""
    now = time.monotonic()
    with _health_lock:
        if _health["result"] is not None and now - _health["at"] < _HEALTH_TTL_S:
            return _health["result"]
        try:
            get_json("/works", {"per-page": 1, "select": "id"}, timeout=timeout)
            ok, msg = True, "OK (shared OpenAlex upstream reachable, key valid)"
        except OpenAlexDown as exc:
            # Self-shed, NOT upstream-down: get_json raises OpenAlexDown ONLY for OmniSeek's own
            # protective states (breaker open / concurrency pool saturated / rate-gate backlog /
            # daily budget dry) and raises the RAW exception for a genuine upstream failure (caught
            # below). Reporting DOWN here flipped all 40+ OpenAlex-backed sources down on a single
            # transient breaker-open (the false mass outage the source-health watchdog surfaced
            # 2026-07-23). Nothing was sent, so nothing was verified: None (the watchdog's
            # `unverified`), neither healthy nor failing; it self-heals when the breaker closes / the
            # pool frees. A genuine outage still surfaces as ok=False via the raw branch.
            ok, msg = None, f"degraded (eye backing off, upstream not probed this cycle): {exc}"
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"{type(exc).__name__}: {exc}"
        with _lock:
            last = _state.get("last_429", 0.0)
        if ok and last and (time.time() - last) < 1800:
            with _usage_lock:
                rep = {k: (v or {}).get("remaining-usd") for k, v in (_usage.get("lanes") or {}).items()}
            msg = (f"OK (OpenAlex reachable), but a 429 hit {int(time.time() - last)}s ago: a daily "
                   f"budget lane (keyed $1.00 / keyless $0.10) ran out; last reported remaining USD "
                   f"{rep or 'unknown'}; resets at midnight UTC")
        _health["at"] = time.monotonic()
        _health["result"] = (ok, msg)
        return _health["result"]


def reconstruct_abstract(inv: Optional[dict]) -> str:
    """OpenAlex stores abstracts as an inverted index {word: [positions]}."""
    if not inv or not isinstance(inv, dict):
        return ""
    pos_word: dict[int, str] = {}
    for word, positions in inv.items():
        if not isinstance(positions, list):
            continue
        for p in positions:
            try:
                pos_word[int(p)] = str(word)
            except (ValueError, TypeError):
                continue
    return " ".join(pos_word[i] for i in sorted(pos_word))


def parse_work(work: dict) -> dict:
    """The common fields of an OpenAlex work record (no judgment, no doc assembly).

    Returns: {work_id, doi, url (doi > landing page > openalex id), title, date
    (tz-aware datetime | None), pub_date (raw str), venue, authors (display
    names), abstract, cited_by}.
    """
    work_id = (work.get("id") or "").split("/")[-1]
    doi = work.get("doi")
    loc = work.get("primary_location") or {}
    landing = loc.get("landing_page_url") if isinstance(loc, dict) else None
    url = doi or landing or work.get("id") or ""

    date = None
    pub = work.get("publication_date")
    if pub:
        try:
            date = datetime.fromisoformat(pub).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass

    venue = ""
    if isinstance(loc, dict):
        src = loc.get("source")
        if isinstance(src, dict):
            venue = src.get("display_name") or ""

    authors: list[str] = []
    for a in (work.get("authorships") or []):
        if isinstance(a, dict):
            au = a.get("author")
            if isinstance(au, dict) and au.get("display_name"):
                authors.append(au["display_name"])

    return {
        "work_id": work_id,
        "doi": doi,
        "url": url,
        "title": (work.get("title") or work.get("display_name") or "").strip(),
        "date": date,
        "pub_date": pub,
        "venue": venue,
        "authors": authors,
        "abstract": reconstruct_abstract(work.get("abstract_inverted_index")),
        "cited_by": work.get("cited_by_count"),
    }
