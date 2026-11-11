"""Shared GitHub plumbing: one keyed client, one pacer, one breaker, one health probe.

Three eye adapters egress to api.github.com: github (code/issues/discussions + tree
browse), github_trending (repo discovery by stars) and, by URL, both. Before this
module each carried its own bare ``http.get_json`` call, and github_trending sent NO
token at all (stuck on the unauth ~10/min Search ceiling + 60/hr core) while sitting
in the broad fan-out; nothing paced the GitHub Search API (30/min authenticated, far
stricter than the 5000/hr core bucket) and nothing read Retry-After on a 429 / 403
secondary-rate response. So a workflow's fan-out across the GitHub-backed sources could
burst the Search quota into a 429 storm with no backoff, wasting the whole quota. This
is the symmetric twin of ``_openalex`` for the GitHub backend:

  get_json()      one keyed pooled client (Authorization on EVERY call when a token
                  exists), one gentle retry honoring Retry-After / X-RateLimit-Reset
  pacing          the declared gate's start slot (``_guard.ahold``) spaces request
                  STARTS so a fan-out can't burst the Search ceiling
  circuit breaker consecutive failures (and a 429 / 403 secondary-rate hit) open the
                  circuit briefly, so a throttled GitHub fails FAST instead of stacking
                  retries across the three sources (same idea as the OpenAlex breaker);
                  a 404 / 410 / 400 / 422 is GitHub answering and is not counted
                  (_guard.counts_against_upstream)
  ahealth()       ONE single-flight 60s-cached probe of the quota-free GET /rate_limit
                  that all three adapters delegate to

Judgment-free plumbing only; callers keep their own caching + doc assembly.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import anyio
import httpx
from tenacity import (AsyncRetrying, RetryCallState, retry_if_exception, retry_if_result,
                      stop_after_attempt)

from omniseek.core import _guard as _guard_mod
from omniseek.core import _probe, cache, diag, http, portal, upstreams
from omniseek import where

logger = logging.getLogger(__name__)

BASE = "https://api.github.com"
_BASE_HOST = "api.github.com"
USER_AGENT = "omniseek/0.1 (automated retrieval)"
TIMEOUT = 20
CRED = where.credentials() / "github.json"

_BREAK_AFTER = 5      # consecutive failures that open the circuit
_BREAK_FOR_S = 120.0  # seconds the circuit stays open

# GitHub's Search API is the binding constraint: 30 req/min AUTHENTICATED (10/min unauth),
# far stricter than the 5000/hr (~83/min) core bucket, and a 403 secondary-rate limit triggers
# on concurrency. OmniSeek egresses ALL GitHub traffic from one host IP across three sources
# (github + github_trending in the broad fan-out + by-URL fetch); a workflow's fan-out can spike
# the Search rate and 429 the shared token, which then trips the breaker below and degrades every
# GitHub-backed source at once. _load_token authenticates every request (get_json injects the
# Authorization header) to move github_trending off the unauth ceiling onto the shared token; the
# pacer + concurrency cap are the load bounds, independent of the token.
# (4 in flight: declared in upstreams.json "github" gate.max_inflight; bound below from the guard.)

# Rate cap: space GitHub request STARTS at least _MIN_INTERVAL_S apart so a fan-out across the three
# GitHub-backed sources (the health sweep, the watchtower org poll, a workflow's burst) can never
# spike the per-minute rate and 429 the shared token. The semaphore bounds CONCURRENCY; this bounds
# RATE; together a burst is impossible by construction. ~1 req / 2s = 30/min, sitting right at the
# Search ceiling, so the stricter Search bucket (not the laxer core bucket) is what we pace to.
# (2.0 s: declared in upstreams.json "github" gate.min_interval_s; bound below from the guard.) The
# Search-code endpoint has its OWN stricter published limit (10 per minute, docs.github.com
# rest/search), which the 2 s spacing alone let OmniSeek exceed whenever code searches were a large
# share of calls: it gets a second declared gate, "github_code_search" (starts >= 6 s apart), taken
# before this one for paths under /search/code (_hold_for).

# The shared load-guard (concurrency cap + rate pacer + circuit breaker): the registry's ONE GitHub
# BackendGuard, built from the declared gate (upstreams.json "github"). The breaker state dict + its
# lock, the semaphore and the pace lock/state live on the guard; the module reaches them by name below
# so every threshold, sleep, log message and error path is unchanged.
_guard = upstreams.guard("github", log=logger)
_code_guard = upstreams.guard("github_code_search", log=logger)
_MAX_CONCURRENCY = _guard.max_inflight
_MIN_INTERVAL_S = _guard.min_interval_s
_state = _guard.state   # health probe reads fails / open_until / last_429
_lock = _guard.lock
_sema = _guard.sema
_pace_state = _guard.pace_state   # the guard's start-slot state (an alias)
_pace_lock = _guard.pace_lock


def _load_token() -> Optional[str]:
    """The GitHub token from <credentials>/github.json (``{"token": "..."}``).

    Classic OR fine-grained PAT (both accept ``Authorization: Bearer``). None when the
    file is absent / unreadable, so the Authorization injection is a no-op and behavior
    is the prior anonymous behavior (committing this before the key exists is safe). This
    is the ONE place the loader lives (github_source previously carried its own copy)."""
    try:
        return json.loads(CRED.read_text(encoding="utf-8")).get("token") or None
    except (OSError, json.JSONDecodeError):
        return None


# Loaded once at import (mirrors _openalex's keyed-client pattern): None when no key file
# exists, so the Authorization injection is a no-op and behavior is unchanged until
# <credentials>/github.json is dropped on the host.
_token = _load_token()


def _http2_ok() -> bool:
    try:
        import h2  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


class GitHubDown(RuntimeError):
    """Raised internally while the circuit is open (recent consecutive failures / a throttle)."""


_RETRY_STATUS = (500, 502, 503)


def _wait_before_retry(state: RetryCallState) -> float:
    """Seconds before the one retry: 1s after an exception; after a throttle / 5xx, what the answer
    asked for (Retry-After, then X-RateLimit-Reset, upstreams.retry_wait), else 1.5s. Capped to 5s:
    the breaker, not a long sleep, is what protects a fan-out from a sustained throttle (a long sleep
    here would just stack across callers)."""
    if state.outcome.failed:
        return 1.0
    resp, _throttled = state.outcome.result()
    return upstreams.retry_wait(resp.headers, cap=5.0, default=1.5)


# One retry (two attempts) on a 429 / secondary-rate 403 / 500 / 502 / 503 or on any exception except a
# shed (nothing was sent) and an answer the breaker does not count (404 / 410 / 400 / 422: GitHub said
# it is not there or the request is wrong, and says so again; _guard.counts_against_upstream). After
# the second attempt, or at once for those, the last outcome comes back as it is (a response, or its
# exception raised) and get_json / aget_json decide: breaker, diag note, None.
_RETRY = dict(
    stop=stop_after_attempt(2),
    wait=_wait_before_retry,
    retry=(retry_if_exception(lambda e: isinstance(e, Exception) and not isinstance(e, _GitHubShed)
                              and _guard_mod.counts_against_upstream(e))
           | retry_if_result(lambda r: r[1] or r[0].status_code in _RETRY_STATUS)),
    retry_error_callback=lambda state: state.outcome.result(),
)


def _is_secondary_rate(resp: "httpx.Response") -> bool:
    """A 403 that is GitHub's SECONDARY rate limit (concurrency / abuse) rather than a plain auth
    denial. GitHub signals it via Retry-After or a body message; treat it like a 429 (back off +
    trip the breaker) so a concurrency burst fails fast instead of hammering on."""
    if resp.status_code != 403:
        return False
    if resp.headers.get("retry-after"):
        return True
    if resp.headers.get("x-ratelimit-remaining") == "0":
        return True
    try:
        msg = (resp.json() or {}).get("message") or ""
    except Exception:  # noqa: BLE001
        msg = ""
    return "secondary rate limit" in msg.lower() or "abuse" in msg.lower()


def get_json(path: str, params: Optional[dict] = None, headers: Optional[dict] = None,
             timeout: float = TIMEOUT) -> Optional[Any]:
    """Sync entry for a caller on a thread: ``aget_json`` run on the event loop (portal.call), so
    there is one implementation of the GitHub request (EYE-TWINS-1)."""
    return portal.call(aget_json, path, params=params, headers=headers, timeout=timeout)


def _trip_breaker(exc: Optional[BaseException] = None) -> None:
    """Record one failure; open the circuit once _BREAK_AFTER consecutive failures pile up. Whether
    it counts is _guard's one rule (``counts_against_upstream``): with no exception (a throttle that
    survived the retry) it counts; an exception carrying 404 / 410 / 400 / 422 does not."""
    _guard.record_fail(exc)


# --- the GitHub request (one implementation; get_json above is its sync entry) --------------------
# Pooled client: reuse one keep-alive connection to api.github.com across the three GitHub-backed
# sources instead of a fresh TCP+TLS handshake per call. HTTP/2 multiplexing if h2 is importable,
# else HTTP/1.1 keep-alive.
_aclient: Optional["httpx.AsyncClient"] = None
_aclient_lock = threading.Lock()


def _aget_client() -> "httpx.AsyncClient":
    global _aclient
    if _aclient is None:
        with _aclient_lock:
            if _aclient is None:
                _aclient = httpx.AsyncClient(
                    headers={"User-Agent": USER_AGENT},
                    timeout=TIMEOUT,
                    http2=_http2_ok(),
                    # follow_redirects=False (SSRF hardening): aget_json is reached with a
                    # candidate-page-parsed owner/repo/path (attacker-influenceable). The API
                    # answers 200 JSON; a 3xx is the off-host redirect attack -> refuse it
                    # explicitly. With the host pin in aget_json, two independent constraints close
                    # the redirect/host injection.
                    follow_redirects=False,
                    limits=httpx.Limits(max_keepalive_connections=8, max_connections=16,
                                        keepalive_expiry=30.0),
                    event_hooks=http.aprogress_hooks(),
                )
    return _aclient


class _GitHubShed(RuntimeError):
    """A declared gate could not start the request before the caller's deadline: nothing was sent. It
    is self-load, not a GitHub failure, so it is neither retried nor counted by the breaker."""

    def __init__(self, *args) -> None:
        super().__init__(*args)
        _probe.note_held(str(self))   # a running health check records that nothing was sent


# Nothing was sent, so a gate taken before the one that refused (the code-search gate, when the
# GitHub-wide gate turns a code search away) hands back its reserved start (review N5).
_guard_mod.register_unsent(_GitHubShed)


def _refuse_for(name: str):
    """The error for a request the gate ``name`` could not start before the caller's deadline: degrade
    to None (no retry, no breaker). Handed to the guards' ahold."""
    return lambda waited: _GitHubShed(_guard_mod.deadline_refusal(name, waited))


@contextlib.asynccontextmanager
async def _ahold_for(path: str, request_s: Optional[float] = None):
    """The declared gates for one request to ``path``: the code-search gate first when the path is
    under /search/code (10 per minute), then the GitHub-wide gate. One fixed order, so no deadlock;
    one bound for both, the caller's deadline."""
    async with contextlib.AsyncExitStack() as stack:
        if (path or "").startswith("/search/code"):
            await stack.enter_async_context(
                _code_guard.ahold(_refuse_for("github_code_search"), request_s=request_s))
        await stack.enter_async_context(_guard.ahold(_refuse_for("github"), request_s=request_s))
        yield


async def aget_json(path: str, params: Optional[dict] = None, headers: Optional[dict] = None,
                    timeout: float = TIMEOUT) -> Optional[Any]:
    """GET api.github.com``path`` and return parsed JSON (or None on any failure).

    Authenticates onto the shared token (Authorization on every call when a token exists), takes the
    declared gates (``_ahold_for``: the permit waited for in the gate's line on the loop, then the
    start slot under it), and does ONE gentle retry on 429 / 5xx / a 403 secondary-rate hit (honoring
    Retry-After / X-RateLimit-Reset, capped 5s). While the breaker is open this returns None at once.
    Returning None on failure (not raising) keeps each adapter's "failure -> empty result" contract."""
    if cache.cache_only():
        return None

    if (urlsplit(f"{BASE}{path}").hostname or "").lower() != _BASE_HOST:
        logger.warning("github aget_json refused: path %r resolves off %s", path, _BASE_HOST)
        return None

    with _lock:
        if time.time() < _state["open_until"]:
            logger.debug("github circuit open %.0fs more; skipping %s",
                         _state["open_until"] - time.time(), path)
            return None

    hdrs = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if headers:
        hdrs.update(headers)
    if _token:
        hdrs["Authorization"] = f"Bearer {_token}"

    async def _attempt() -> tuple:
        # Same gates, same order as get_json: the permit waited for in the gate's line on the
        # loop (bounded; a cancel cannot keep it), then the start slot awaited under it. Released
        # before the retry sleep (mirror get_json).
        async with _ahold_for(path, request_s=timeout):
            resp = await _aget_client().get(f"{BASE}{path}", params=params, headers=hdrs, timeout=timeout)
        upstreams.observe_response("github", resp, defer_on_429=False)  # upstreams.SELF_BACKOFF
        throttled = resp.status_code == 429 or _is_secondary_rate(resp)
        if throttled:
            with _lock:
                _state["last_429"] = time.time()
        elif resp.status_code not in _RETRY_STATUS:
            resp.raise_for_status()
        return resp, throttled

    try:
        resp, throttled = await AsyncRetrying(sleep=anyio.sleep, **_RETRY)(_attempt)
        if throttled:
            _trip_breaker()
            diag.note("github.get_json", url=f"{BASE}{path}", status=resp.status_code,
                      body="rate / secondary-rate limited (survived one retry)")
            return None
        resp.raise_for_status()
    except _GitHubShed as exc:  # the gate turned it away: nothing sent, not a GitHub failure
        diag.note("github.get_json", url=f"{BASE}{path}",
                  body=f"declared upstream gate, request not sent: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        _trip_breaker(exc)  # a 404 / 410 / 400 / 422 is GitHub answering, not counted (_guard)
        logger.warning("github aget_json failed (%s): %s", path, exc)
        diag.note("github.get_json", url=f"{BASE}{path}", exc=exc,
                  status=getattr(getattr(exc, "response", None), "status_code", None))
        return None
    _guard.record_ok()
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001 — unparseable body is a failure, degrade to None
        logger.warning("github aget_json parse failed (%s): %s", path, exc)
        diag.note("github.get_json", url=f"{BASE}{path}", status=resp.status_code,
                  body=resp.text, exc=exc)
        return None


_HEALTH_TTL_S = 60.0
_health: dict = {"at": 0.0, "result": None}


async def ahealth(timeout: float = 8.0) -> tuple[Optional[bool], str]:
    """ONE shared upstream probe for all three GitHub-backed sources (single-flight + 60s cache,
    ``_probe.shared_verdict``).

    Before this, github + github_trending each probed GET /rate_limit in its own health_check; the
    health sweep fired both at once, and github_trending probed UNAUTHENTICATED. Now they delegate
    here: one GET /rate_limit (quota-free: it never spends the core / search budget) tests
    connectivity + token validity + the breaker state, cached 60s and single-flighted so concurrent
    callers cause exactly ONE upstream call. The core + search remaining counts and whether a token
    is present are surfaced; a recent 429 / secondary-rate hit is surfaced
    even when the probe itself succeeds, because GET /rate_limit is quota-free and so does not prove
    the Search budget is unexhausted."""
    return await _probe.shared_verdict(_health, _HEALTH_TTL_S, lambda: _aprobe(timeout))


async def _aprobe(timeout: float) -> tuple[Optional[bool], str]:
    """The probe behind ``ahealth`` (run once per TTL by ``_probe.shared_verdict``)."""
    with _lock:
        if time.time() < _state["open_until"]:
            # Self-shed (breaker open after consecutive failures), NOT GitHub-unreachable: a
            # transient breaker-open must not flip github + github_trending down (they self-heal
            # when the breaker closes). Nothing is sent, so nothing is verified: None (the
            # watchdog's `unverified`). A genuine outage surfaces as a failed /rate_limit probe
            # below (2026-07-23 watchdog false-mass-down fix).
            return None, _probe.breaker_open("GitHub, eye backing off",
                                             _state["open_until"] - time.time())
    t0 = time.time()
    # The verdict is cached for every GitHub-backed source, so it is re-read HERE (not only in
    # the per-source health funnel): a 429 or a refused gate is "not verified" for all of them.
    with _probe.watching() as led:
        data = await aget_json("/rate_limit", timeout=timeout)
    if data is None:
        with _lock:
            throttled_at = _state.get("last_429", 0.0)
        if throttled_at >= t0:
            # A 429 or a secondary-rate 403 during THIS probe: GitHub answered, but only that it
            # is throttling us, which says nothing about whether the API serves: not verified.
            ok, msg = None, (f"{_probe.rate_limited('api.github.com')}: GET /rate_limit hit a "
                             "rate or secondary limit")
        else:
            ok, msg = _probe.reread(
                False, "GET /rate_limit failed (timeout / network / breaker / throttle)", led)
    else:
        res = (data or {}).get("resources", {})
        # GET /rate_limit reports EVERY bucket's limit and remaining in one quota-free call: record
        # them as readings (one lane per resource) so the upstream health block compares core /
        # search / code_search against the declared 5000 / 30 / 10.
        for rname in ("core", "search", "code_search"):
            b = res.get(rname) or {}
            if b.get("limit") is not None:
                upstreams.observe("github", {"x-ratelimit-limit": b.get("limit"),
                                             "x-ratelimit-remaining": b.get("remaining"),
                                             "x-ratelimit-reset": b.get("reset")}, 200, lane=rname)
        core = res.get("core", {}).get("remaining", "?")
        cs = res.get("search", {}).get("remaining", "?")
        tok = "token" if _token else "NO token (unauth ceiling)"
        ok, msg = True, f"OK ({tok}; core={core}, search={cs})"
        with _lock:
            last = _state.get("last_429", 0.0)
        if last and (time.time() - last) < 1800:
            msg = (f"OK ({tok}; core={core}, search={cs}), but a rate/secondary-limit hit "
                   f"{int(time.time() - last)}s ago: the Search budget (30/min) may be throttling")
    return ok, msg
