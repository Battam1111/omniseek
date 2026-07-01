"""Shared OpenAlex plumbing: one client, one breaker, one work-parser.

OpenAlex backs 40+ named sources (openalex, researcher_watch, every org_watch
row) plus the cartographer. Before this module, three source files each carried
their own copy of the HTTP call, the inverted-index abstract reconstruction and
the work-to-fields parsing; and a dead upstream degraded a third of the eye with
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
import threading
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

import httpx

from penumbra.core import auth, diag

logger = logging.getLogger(__name__)

BASE = "https://api.openalex.org"
_BASE_HOST = "api.openalex.org"
# Contact is host-injected, never a hardcoded personal address (see auth.contact_email).
USER_AGENT = f"polaris-eye/0.1 (mailto:{auth.contact_email()}; automated retrieval)"
TIMEOUT = 20

_BREAK_AFTER = 5      # consecutive failures that open the circuit
_BREAK_FOR_S = 120.0  # seconds the circuit stays open
_state = {"fails": 0, "open_until": 0.0, "last_429": 0.0,
          # Per-bucket budget-exhaustion reset (monotonic deadline). OpenAlex 2026 gives the api_key
          # and the anonymous per-IP path SEPARATE $1/day credit buckets; when one 429s "insufficient
          # budget" we mark it dry until its reset and route to the other (get_json's two-lane spill).
          "dry_until": {"keyed": 0.0, "anon": 0.0}}
_lock = threading.Lock()

# OpenAlex's 2026 credit model (verified 2026-06-17 from the live 429 body + rate headers): every
# call costs $0.0001 and each IDENTITY gets a $1/day budget = 10,000 calls/day, resetting at midnight
# UTC. CRUCIALLY the api_key and the anonymous per-IP path are SEPARATE buckets, each $1/day (an
# api_key does NOT raise the allotment, contra the old belief) -- so the eye's one host has TWO free
# daily budgets. The eye egresses ALL OpenAlex traffic across 40+ sources (researcher_watch + 39
# org_watch + openalex + cartographer/field_skeleton + relations) from that host, and an active day's
# legitimate fan-out (a single field_skeleton is 100+ calls) exhausts one $1 bucket. get_json
# therefore uses BOTH buckets: the api_key bucket first, spilling to the anonymous per-IP bucket on a
# budget-429 (~2x daily capacity) before degrading; a bucket that 429s "insufficient budget" is marked
# dry until its reset so we stop hammering it. The mailto in USER_AGENT is a courtesy contact only
# (the polite-pool fast-lane is dead). The concurrency cap + rate pacer + breaker remain the load
# bounds, independent of the credit budget.
_MAX_CONCURRENCY = 8
_sema = threading.BoundedSemaphore(_MAX_CONCURRENCY)

# Rate cap: space OpenAlex request STARTS at least _MIN_INTERVAL_S apart so a fan-out across the 40+
# OpenAlex-backed sources (the health sweep, the org_watch cron, a workflow's cohort burst) can never
# spike the per-second rate and 429 the whole shared key. The semaphore bounds CONCURRENCY; this bounds
# RATE; together a burst is impossible by construction (the root cause of the self-DOS the all-at-once
# health probe used to cause). ~5 req/s sits well under OpenAlex's ceiling and is invisible interactively.
_MIN_INTERVAL_S = 0.2
# Hard cap on how long ONE caller may wait on the rate gate. Without it, an OA 429 / budget-exhaustion
# storm (40+ OA sources fanning out, each retry re-reserving a slot) grows the backlog unboundedly and
# a fresh caller inherits the WHOLE queue — the same unbounded-pace-wait bug that made an S2
# field_skeleton sit 886s on its gate (brain: eye-s2-rate-gate-hang-2026-06-21). Past this, fail fast
# (raise OpenAlexDown → caller degrades to cache/empty) instead of hanging for minutes.
_PACE_MAX_WAIT_S = 15.0
_pace_state = {"next_at": 0.0}
_pace_lock = threading.Lock()


def _load_api_key() -> Optional[str]:
    from penumbra.core import auth  # local import: keep module import cheap + acyclic
    import os

    creds = auth.load("openalex") or {}
    return creds.get("api_key") or os.environ.get("OPENALEX_API_KEY") or None


# Loaded once at import (mirrors _s2's keyed-client pattern): None when no key file exists, so
# get_json's injection is a no-op and behavior is unchanged until ~/.polaris/credentials/openalex.json
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
                )
    return _client


class OpenAlexDown(RuntimeError):
    """Raised immediately while the circuit is open (recent consecutive failures)."""


def breaker_open() -> bool:
    """True iff the shared OpenAlex circuit is currently open (recent consecutive failures). A
    non-probing read of the breaker state, so callers can stamp a degraded flag without an
    upstream probe (cheap; never spends budget)."""
    with _lock:
        return time.time() < _state["open_until"]


def unavailable() -> bool:
    """True iff OpenAlex cannot serve a request right now WITHOUT a live probe: the circuit is open
    (recent consecutive failures) OR BOTH daily credit buckets are dry (key + anon per-IP, until their
    midnight-UTC reset). A non-probing read (no upstream call, no budget spend) — mirrors get_json's
    lane selection — so a caller can choose to serve a stale last-good fallback instead of a blind
    empty when the only reason for [] is OpenAlex being down (vs a genuine no-match)."""
    with _lock:
        if time.time() < _state["open_until"]:
            return True
        now = time.monotonic()
        dry = _state["dry_until"]
        keyed_avail = bool(_api_key) and now >= dry.get("keyed", 0.0)
        anon_avail = now >= dry.get("anon", 0.0)
        return not (keyed_avail or anon_avail)


def _pace() -> None:
    """Reserve the next request-start slot (>= _MIN_INTERVAL_S after the previous), then wait for it.
    The slot reservation is under the lock; the wait is NOT, so callers do not serialize on the lock
    itself, only on the wire-rate. Bounds OpenAlex requests/second across all callers + threads."""
    with _pace_lock:
        start = max(time.monotonic(), _pace_state["next_at"])
        _pace_state["next_at"] = start + _MIN_INTERVAL_S
    delay = start - time.monotonic()
    if delay > 0:
        time.sleep(delay)


def _pace_backlog_s() -> float:
    """How long the NEXT request would wait on the rate gate, read-only (no reservation). >
    _PACE_MAX_WAIT_S means the backlog is pathological (a 429 storm) and get_json fails fast instead
    of inheriting the whole queue. The fast-fail lives at get_json's top (beside the breaker check),
    not in _pace(), so it never tangles with the in-loop retry/fails bookkeeping."""
    return max(0.0, _pace_state["next_at"] - time.monotonic())


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
# Every budget-spending success is tallied by the eye component that drove it (cartographer /
# relations / org_watch / the openalex search source / researcher_watch / enrich), with the live
# per-bucket remaining. In-memory (resets on restart; `window_hours` reports the span). Exposed via
# eye_health_check so any day's OpenAlex breakdown is INSPECTABLE, not inferred.
_usage_lock = threading.Lock()
_usage: dict = {"since": None, "by_caller": {}, "spilled_to_anon": 0,
                "remaining": {"keyed": None, "anon": None}}


def _caller_tag() -> str:
    """The nearest stack frame OUTSIDE this module = the eye component that drove the call."""
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


def _note_remaining(bucket: str, resp) -> None:
    """Capture the live x-ratelimit-remaining for a bucket from any response (200 or 429)."""
    rem = resp.headers.get("x-ratelimit-remaining") if resp is not None else None
    if rem is None:
        return
    try:
        rem_i = int(rem)
    except (ValueError, TypeError):
        return
    with _usage_lock:
        _usage["remaining"][bucket] = rem_i


def _record_ok(caller: str, bucket: str) -> None:
    """Tally one budget-spending success against its caller (+ a spill-to-anon counter)."""
    with _usage_lock:
        if _usage["since"] is None:
            _usage["since"] = time.time()
        _usage["by_caller"][caller] = _usage["by_caller"].get(caller, 0) + 1
        if bucket == "anon" and _api_key:
            _usage["spilled_to_anon"] += 1


def usage_stats() -> dict:
    """Snapshot of OpenAlex usage by caller since process start (surfaced by eye_health_check)."""
    with _usage_lock:
        by = dict(_usage["by_caller"])
        since = _usage["since"]
        return {
            "since_epoch": round(since, 1) if since else None,
            "window_hours": round((time.time() - since) / 3600, 2) if since else 0.0,
            "total_ok_calls": sum(by.values()),
            "by_caller": dict(sorted(by.items(), key=lambda kv: kv[1], reverse=True)[:25]),
            "spilled_to_anon": _usage["spilled_to_anon"],
            "remaining": dict(_usage["remaining"]),
        }


def get_json(path: str, params: Optional[dict] = None, timeout: float = TIMEOUT) -> dict:
    """GET api.openalex.org``path`` and return parsed JSON.

    Uses BOTH free daily budgets (see the credit-model note above): the api_key bucket FIRST,
    spilling to the anonymous per-IP bucket on a budget-429, for ~2x daily capacity. One gentle
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
    if _backlog > _PACE_MAX_WAIT_S:
        _down = OpenAlexDown(f"rate-gate backlog {_backlog:.0f}s > {_PACE_MAX_WAIT_S:.0f}s; OA storming, degrade")
        diag.note("openalex.get_json", url=f"{BASE}{path}", exc=_down)
        raise _down

    base = dict(params or {})
    # Auth lanes in preference order: the api_key bucket first, then the anonymous per-IP bucket (a
    # SECOND independent $1/day budget). Skip a bucket still marked dry from a recent budget-429.
    now = time.monotonic()
    with _lock:
        dry = _state["dry_until"]
        lanes = []
        if _api_key and now >= dry.get("keyed", 0.0):
            lanes.append(("keyed", {**base, "api_key": _api_key}))
        if now >= dry.get("anon", 0.0):
            lanes.append(("anon", dict(base)))
    if not lanes:  # both daily budgets exhausted → fail fast; the caller degrades to cached/empty
        _dry = OpenAlexDown(
            "both OpenAlex daily budgets exhausted (key + anon per-IP); reset midnight UTC")
        diag.note("openalex.get_json", url=f"{BASE}{path}", exc=_dry)
        raise _dry

    caller = _caller_tag()  # attribute this call's budget spend to the eye component that drove it
    last_exc: Optional[Exception] = None
    for lane_name, p in lanes:
        for attempt in (1, 2):
            try:
                _pace()  # rate cap: bounds req/s so a fan-out across 40+ sources can't burst a bucket
                with _sema:  # global concurrency cap; released during the retry sleep below
                    resp = _get_client().get(f"{BASE}{path}", params=p, timeout=timeout)
                _note_remaining(lane_name, resp)  # capture this bucket's live remaining (200 or 429)
                if resp.status_code == 429:  # stamp it so health() can surface it honestly
                    with _lock:
                        _state["last_429"] = time.time()
                    if _is_budget_429(resp):  # THIS bucket's daily credit is spent → mark dry + spill
                        reset = min(_retry_after(resp) or 3600.0, 86400.0)
                        with _lock:
                            _state["dry_until"][lane_name] = time.monotonic() + reset
                        last_exc = RuntimeError(
                            f"OpenAlex {lane_name} daily budget exhausted (429); resets in ~{int(reset)}s")
                        break  # spill to the next bucket (no sleep: this one won't recover for hours)
                if resp.status_code in (429, 500, 502, 503) and attempt == 1:
                    time.sleep(min(_retry_after(resp, 1.5), 5.0))
                    continue
                resp.raise_for_status()
                with _lock:
                    _state["fails"] = 0
                _record_ok(caller, lane_name)  # budget-spending success → tally to its caller
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt == 1:
                    time.sleep(1.0)
        # this bucket failed (budget-429 spill, or two transient failures) → try the next bucket
    with _lock:
        _state["fails"] += 1
        if _state["fails"] >= _BREAK_AFTER:
            _state["open_until"] = time.time() + _BREAK_FOR_S
            _state["fails"] = 0
            logger.warning("openalex circuit OPEN for %.0fs (consecutive failures)", _BREAK_FOR_S)
    # Every lane failed (budget-429 spill or two transient failures each). Surface the last
    # exception + the path so the fixing agent sees WHY OpenAlex gave nothing (429 budget, 5xx,
    # timeout): only on this real-failure exit, never the success return above.
    diag.note("openalex.get_json", url=f"{BASE}{path}", exc=last_exc)
    raise last_exc  # type: ignore[misc]


_HEALTH_TTL_S = 60.0
_health: dict = {"at": 0.0, "result": None}
_health_lock = threading.Lock()


def health(timeout: float = 8.0) -> tuple[bool, str]:
    """ONE shared upstream probe for all 40+ OpenAlex-backed sources (single-flight + 60s cache).

    Before this, openalex + researcher_watch + every org_watch row each probed OpenAlex in its own
    health_check; the health sweep fired all 40 at once, bursting the shared key into 429 and tripping
    the breaker, so one transient probe storm read as "40 sources down" and degraded them all. Now
    they delegate here: one minimal filter call (per-page=1, select=id) tests connectivity + key
    validity + the breaker/rate state, cached 60s and single-flighted (the probe runs under the lock)
    so 40 concurrent callers cause exactly ONE upstream call. A recent 429 is surfaced even when this
    probe succeeds, because get_json may have reached OpenAlex via the anonymous bucket while the
    api_key bucket is exhausted (the two $1/day credit buckets are SEPARATE), so a recent budget-429
    stays legible even on an OK verdict."""
    now = time.monotonic()
    with _health_lock:
        if _health["result"] is not None and now - _health["at"] < _HEALTH_TTL_S:
            return _health["result"]
        try:
            get_json("/works", {"per-page": 1, "select": "id"}, timeout=timeout)
            ok, msg = True, "OK (shared OpenAlex upstream reachable, key valid)"
        except OpenAlexDown as exc:
            ok, msg = False, f"circuit open ({exc}); recent consecutive failures, backing off"
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"{type(exc).__name__}: {exc}"
        with _lock:
            last = _state.get("last_429", 0.0)
        if ok and last and (time.time() - last) < 1800:
            msg = (f"OK (OpenAlex reachable), but a 429 hit {int(time.time() - last)}s ago: one of the two "
                   "daily $1 credit buckets (api_key / anon per-IP) is exhausted; resets at midnight UTC")
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
