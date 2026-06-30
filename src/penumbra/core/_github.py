"""Shared GitHub plumbing: one keyed client, one pacer, one breaker, one health probe.

Three adapters egress to api.github.com: github (code/issues/discussions + tree
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
  _pace()         space request STARTS so a fan-out can't burst the Search ceiling
  circuit breaker consecutive failures (and a 429 / 403 secondary-rate hit) open the
                  circuit briefly, so a throttled GitHub fails FAST instead of stacking
                  retries across the three sources (same idea as the OpenAlex breaker)
  health()        ONE single-flight 60s-cached probe of the quota-free GET /rate_limit
                  that all three adapters delegate to

Judgment-free plumbing only; callers keep their own caching + doc assembly.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from penumbra.core import cache, diag

logger = logging.getLogger(__name__)

BASE = "https://api.github.com"
_BASE_HOST = "api.github.com"
USER_AGENT = "penumbra/0.1 (automated retrieval)"
TIMEOUT = 20
CRED = Path.home() / ".penumbra" / "credentials" / "github.json"

_BREAK_AFTER = 5      # consecutive failures that open the circuit
_BREAK_FOR_S = 120.0  # seconds the circuit stays open
_state = {"fails": 0, "open_until": 0.0, "last_429": 0.0}
_lock = threading.Lock()

# GitHub's Search API is the binding constraint: 30 req/min AUTHENTICATED (10/min unauth),
# far stricter than the 5000/hr (~83/min) core bucket, and a 403 secondary-rate limit triggers
# on concurrency. Penumbra egresses ALL GitHub traffic from one host IP across three sources
# (github + github_trending in the broad fan-out + by-URL fetch); a workflow's fan-out can spike
# the Search rate and 429 the shared token, which then trips the breaker below and degrades every
# GitHub-backed source at once. _load_token authenticates every request (get_json injects the
# Authorization header) to move github_trending off the unauth ceiling onto the shared token; the
# pacer + concurrency cap are the load bounds, independent of the token.
_MAX_CONCURRENCY = 4
_sema = threading.BoundedSemaphore(_MAX_CONCURRENCY)

# Rate cap: space GitHub request STARTS at least _MIN_INTERVAL_S apart so a fan-out across the three
# GitHub-backed sources (the health sweep, the watchtower org poll, a workflow's burst) can never
# spike the per-minute rate and 429 the shared token. The semaphore bounds CONCURRENCY; this bounds
# RATE; together a burst is impossible by construction. ~1 req / 2s = 30/min, sitting right at the
# Search ceiling, so the stricter Search bucket (not the laxer core bucket) is what we pace to.
_MIN_INTERVAL_S = 2.0
_pace_state = {"next_at": 0.0}
_pace_lock = threading.Lock()


def _load_token() -> Optional[str]:
    """The GitHub token from ~/.penumbra/credentials/github.json (``{"token": "..."}``).

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
# ~/.penumbra/credentials/github.json is dropped on the host.
_token = _load_token()

# Pooled client: reuse one keep-alive connection to api.github.com across the three GitHub-backed
# sources instead of a fresh TCP+TLS handshake per call. httpx.Client is thread-safe; the global
# _sema still bounds in-flight concurrency. HTTP/2 multiplexing if h2 is importable, else HTTP/1.1
# keep-alive.
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
                    # follow_redirects=False (SSRF hardening): get_json is reached with a
                    # candidate-page-parsed owner/repo/path (attacker-influenceable). The API
                    # answers 200 JSON; a 3xx is the off-host redirect attack -> refuse it
                    # explicitly. With the host pin in get_json, two independent constraints close
                    # the redirect/host injection.
                    follow_redirects=False,
                    limits=httpx.Limits(max_keepalive_connections=8, max_connections=16,
                                        keepalive_expiry=30.0),
                )
    return _client


class GitHubDown(RuntimeError):
    """Raised internally while the circuit is open (recent consecutive failures / a throttle)."""


def _pace() -> None:
    """Reserve the next request-start slot (>= _MIN_INTERVAL_S after the previous), then wait for it.
    The slot reservation is under the lock; the wait is NOT, so callers do not serialize on the lock
    itself, only on the wire-rate. Bounds GitHub requests/minute across all callers + threads."""
    with _pace_lock:
        start = max(time.monotonic(), _pace_state["next_at"])
        _pace_state["next_at"] = start + _MIN_INTERVAL_S
    delay = start - time.monotonic()
    if delay > 0:
        time.sleep(delay)


def _retry_after(resp: "httpx.Response") -> float:
    """Seconds to wait before a retry, honoring (in order) Retry-After, then X-RateLimit-Reset
    (an absolute epoch). Capped to 5s: the breaker, not a long sleep, is what protects a fan-out
    from a sustained throttle (a long sleep here would just stack across callers)."""
    ra = resp.headers.get("retry-after")
    if ra:
        try:
            return min(float(ra), 5.0)
        except ValueError:
            pass
    reset = resp.headers.get("x-ratelimit-reset")
    if reset:
        try:
            return max(0.0, min(float(reset) - time.time(), 5.0))
        except ValueError:
            pass
    return 1.5


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
    """GET api.github.com``path`` and return parsed JSON (or None on any failure).

    Authenticates onto the shared token (Authorization on every call when a token exists),
    paces request starts, and does ONE gentle retry on 429 / 5xx / a 403 secondary-rate hit
    (honoring Retry-After / X-RateLimit-Reset, capped 5s). While the breaker is open this
    returns None at once. Returning None on failure (not raising) keeps each adapter's
    "failure -> empty result" contract exactly as the prior ``http.get_json`` did."""
    if cache.cache_only():
        return None  # cache-only mode (cache_only=True): do NO live HTTP, the single egress guard

    # Host pin (SSRF hardening): the resolved request host MUST be api.github.com. path comes from a
    # candidate-page-parsed owner/repo/number/branch; assert the assembled URL never resolves off-host
    # (a crafted path with an authority or scheme can't redirect us off the pinned API host).
    if (urlsplit(f"{BASE}{path}").hostname or "").lower() != _BASE_HOST:
        logger.warning("github get_json refused: path %r resolves off %s", path, _BASE_HOST)
        return None

    with _lock:
        if time.time() < _state["open_until"]:
            logger.debug("github circuit open %.0fs more; skipping %s",
                         _state["open_until"] - time.time(), path)
            return None

    # Authenticate onto the shared token (the real remedy for github_trending's unauth ceiling +
    # the per-IP Search 429). The Authorization header is injected here so ONE site covers all three
    # GitHub-backed sources; the caller's Accept / API-version headers ride on top. No token -> no-op.
    hdrs = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if headers:
        hdrs.update(headers)
    if _token:
        hdrs["Authorization"] = f"Bearer {_token}"

    last_exc: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            _pace()  # rate cap: bounds req/min so a fan-out across the 3 sources can't burst the token
            with _sema:  # global concurrency cap; released during the retry sleep below
                resp = _get_client().get(f"{BASE}{path}", params=params, headers=hdrs, timeout=timeout)
            throttled = resp.status_code == 429 or _is_secondary_rate(resp)
            if throttled:  # rate/secondary limit: stamp it so health() can surface it honestly
                with _lock:
                    _state["last_429"] = time.time()
            if (throttled or resp.status_code in (500, 502, 503)) and attempt == 1:
                time.sleep(_retry_after(resp))
                continue
            if throttled:  # a throttle that survived the one retry trips the breaker (fail fast)
                _trip_breaker()
                diag.note("github.get_json", url=f"{BASE}{path}", status=resp.status_code,
                          body="rate / secondary-rate limited (survived one retry)")
                return None
            resp.raise_for_status()
            with _lock:
                _state["fails"] = 0
            try:
                return resp.json()
            except Exception as exc:  # noqa: BLE001 — unparseable body is a failure, degrade to None
                logger.warning("github get_json parse failed (%s): %s", path, exc)
                diag.note("github.get_json", url=f"{BASE}{path}", status=resp.status_code,
                          body=resp.text, exc=exc)
                return None
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt == 1:
                time.sleep(1.0)
    _trip_breaker()
    logger.warning("github get_json failed (%s): %s", path, last_exc)
    # Only on the real-failure exit (both attempts failed): surface the exception + path so the
    # fixing agent sees the network/HTTP cause, not just an empty result.
    diag.note("github.get_json", url=f"{BASE}{path}", exc=last_exc)
    return None


def _trip_breaker() -> None:
    """Record one failure; open the circuit once _BREAK_AFTER consecutive failures pile up."""
    with _lock:
        _state["fails"] += 1
        if _state["fails"] >= _BREAK_AFTER:
            _state["open_until"] = time.time() + _BREAK_FOR_S
            _state["fails"] = 0
            logger.warning("github circuit OPEN for %.0fs (consecutive failures)", _BREAK_FOR_S)


_HEALTH_TTL_S = 60.0
_health: dict = {"at": 0.0, "result": None}
_health_lock = threading.Lock()


def health(timeout: float = 8.0) -> tuple[bool, str]:
    """ONE shared upstream probe for all three GitHub-backed sources (single-flight + 60s cache).

    Before this, github + github_trending each probed GET /rate_limit in its own health_check; the
    health sweep fired both at once, and github_trending probed UNAUTHENTICATED. Now they delegate
    here: one GET /rate_limit (quota-free: it never spends the core / search budget) tests
    connectivity + token validity + the breaker state, cached 60s and single-flighted (the probe runs
    under the lock) so concurrent callers cause exactly ONE upstream call. The core + search remaining
    counts and whether a token is present are surfaced; a recent 429 / secondary-rate hit is surfaced
    even when the probe itself succeeds, because GET /rate_limit is quota-free and so does not prove
    the Search budget is unexhausted."""
    now = time.monotonic()
    with _health_lock:
        if _health["result"] is not None and now - _health["at"] < _HEALTH_TTL_S:
            return _health["result"]
        with _lock:
            if time.time() < _state["open_until"]:
                ok, msg = False, (f"circuit open ({_state['open_until'] - time.time():.0f}s more); "
                                  "recent consecutive failures, backing off")
                _health["at"] = time.monotonic()
                _health["result"] = (ok, msg)
                return _health["result"]
        data = get_json("/rate_limit", timeout=timeout)
        if data is None:
            ok, msg = False, "GET /rate_limit failed (timeout / network / breaker / throttle)"
        else:
            res = (data or {}).get("resources", {})
            core = res.get("core", {}).get("remaining", "?")
            cs = res.get("search", {}).get("remaining", "?")
            tok = "token" if _token else "NO token (unauth ceiling)"
            ok, msg = True, f"OK ({tok}; core={core}, search={cs})"
            with _lock:
                last = _state.get("last_429", 0.0)
            if last and (time.time() - last) < 1800:
                msg = (f"OK ({tok}; core={core}, search={cs}), but a rate/secondary-limit hit "
                       f"{int(time.time() - last)}s ago: the Search budget (30/min) may be throttling")
        _health["at"] = time.monotonic()
        _health["result"] = (ok, msg)
        return _health["result"]
