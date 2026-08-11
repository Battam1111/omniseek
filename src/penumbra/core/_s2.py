"""Shared Semantic Scholar plumbing: one client, one breaker, one id-normalizer.

Semantic Scholar backs the cartographer (field_skeleton source="s2" + recommend)
and the relationship layer (resolve_identity, coauthors, the paper-anchored
disambiguator). Before this module, FIVE call sites each constructed their own
``SemanticScholar(api_key=...)`` client from ``auth.load`` and TWO of them carried
their own copy of the bare-arXiv-id / DOI → S2-prefix normalizer; nothing shared
the polite-pool rate limit or protected the eye from S2's frequent 429 storms.
This is the symmetric twin of ``_openalex`` for the S2 backend:

  get_client()              one shared keyed client (singleton, lazy, thread-safe)
  norm_s2_id()              the bare arXiv id / DOI → "ArXiv:" / "DOI:" prefix, the
                            ONE place that truth lives (a bare id is SILENTLY rejected
                            by S2's paper/recommend endpoints — returns nothing)
  BoundedSemaphore          global politeness cap (S2 free tier = 5000 req/5min shared;
                            with the key 1 RPS guaranteed) so a parallel fan-out can't
                            429-storm itself and trip the breaker — same idea as _openalex
  circuit breaker (S2Down)  consecutive failures open the circuit briefly so a dead /
                            rate-limited S2 fails FAST instead of stacking the lib's
                            tenacity backoff across callers (mirror of OpenAlexDown)
  retry=False + _retry_rl   the lib's OWN tenacity retry (default retry=True does
                            stop_after_attempt(10) + wait_exponential(5,60) ~= 250s of hidden
                            backoff on a 429) is turned OFF at construction so it can no longer
                            pre-empt the breaker with a ~260s fake-hang (the field_skeleton cold-
                            seed symptom); a SHORT eye-owned bounded retry (a few seconds) rides a
                            brief throttle, then degrades fast and lets the breaker take over
  thin wrappers             get_paper / get_paper_references / get_paper_citations /
                            search_paper / get_recommended_papers / search_author /
                            get_author_papers — each with the semaphore + breaker, the
                            bounded-iteration page cap (the "limit == page size, iterating
                            a PaginatedResults pages through EVERY page" trap the
                            cartographer comment warns about), and exception → degrade.

Judgment-free plumbing only; callers keep their own caching + doc assembly + the
agent-facing shaping. No ranking, no relevance, no doc building here.
"""

from __future__ import annotations

import itertools
import logging
import re
import threading
import time
from typing import Iterable, Optional

from penumbra.core import diag
from penumbra.core._guard import BackendGuard

logger = logging.getLogger(__name__)

TIMEOUT = 15  # per-call ceiling. S2 single-call responses are normally <5s; a call past 15s is a
              # throttle/stall, so 15s (was 25) fails it sooner — tightens the overhang past an
              # assemble's overall deadline and lets the breaker trip faster. Safe for the single-call
              # callers (relations, recommend) and moot for the broad-search source (11s deadline drops
              # it first). The aggregate hang fix is the cartographer assemble deadline, not this.

_BREAK_AFTER = 5      # consecutive failures that open the circuit
_BREAK_FOR_S = 120.0  # seconds the circuit stays open

# Global politeness cap: at most _MAX_CONCURRENCY in-flight S2 calls across ALL callers
# (cartographer's per-seed reference/citation fan-out + relations' parallel per-author
# work fetch). S2's keyless pool is a SHARED 5000 req/5min bucket and aggressively 429s;
# even with the API key the guarantee is ~1 RPS. Bounding concurrent in-flight keeps a
# parallel fan-out from tripping a 429 storm → which would open the shared breaker and
# degrade every S2-backed capability at once. Lower than _openalex's 8 because S2 throttles
# far harder than OpenAlex's polite pool.
_MAX_CONCURRENCY = 4

# Rate cap: space S2 request STARTS at least _MIN_INTERVAL_S apart so a fan-out across the S2-backed
# capabilities (the field_skeleton/recommend cartographer burst + the relations per-author fetch + the
# semantic_scholar source) can never spike the per-second rate and 429 the whole shared key. The
# semaphore bounds CONCURRENCY; this bounds RATE; together a burst is impossible by construction (the
# root cause of the 429 storms that trip the shared breaker and degrade every S2 capability at once).
# 1.0s honors S2's ~1 RPS keyed guarantee exactly (the docstring's "even with the key 1 RPS"); S2
# throttles far harder than OpenAlex's polite pool, hence the much wider interval than _openalex's 0.2.
_MIN_INTERVAL_S = 1.0
# Hard cap on how long ONE caller may wait on the rate gate. Without it, a 429 storm (each failed
# call retries, and every attempt reserves another 1s-apart slot) grows the backlog unboundedly, and
# a fresh caller inherits the WHOLE queue: an observed field_skeleton sat 886s on the gate. Past this
# cap the queue is pathological (S2 storming) → fail fast (raise S2Down → the wrapper degrades to
# []/None) instead of hanging, AND do not reserve a slot so the backlog drains rather than growing.
_PACE_MAX_WAIT_S = 15.0
# Hard cap on how long ONE caller may wait for a CONCURRENCY permit (the sema), the sibling of the
# rate gate's _PACE_MAX_WAIT_S. A raw unbounded acquire hangs a caller for the whole MCP idle window
# when the pool is saturated or a permit leaked (the 300s resolve_identity outage, 2026-07-18); past
# this the pool is treated as unavailable -> raise S2Down -> the wrapper degrades to []/None.
_ACQUIRE_MAX_WAIT_S = 20.0

# The shared load-guard (concurrency cap + rate pacer + circuit breaker): the byte-identical machinery
# _openalex / _s2 / _github each carried, extracted to _guard (2026-07-01 parsimony audit P1). The
# breaker state dict + its lock, the semaphore and the pace lock/state now live on the guard; the
# module reaches them by name below so every threshold, sleep, log message and error path is unchanged.
_guard = BackendGuard("s2", _MAX_CONCURRENCY, break_after=_BREAK_AFTER,
                      break_for_s=_BREAK_FOR_S, min_interval_s=_MIN_INTERVAL_S, log=logger)
_state = _guard.state   # health / recently_throttled read fails / open_until / last_429
_lock = _guard.lock
_sema = _guard.sema
_pace_state = _guard.pace_state   # the slot reservation _pace() reads/reserves (aliases the guard)
_pace_lock = _guard.pace_lock


def _pace() -> None:
    """Reserve the next request-start slot (>= _MIN_INTERVAL_S after the previous), then wait for it.
    The slot reservation is under the lock; the wait is NOT, so callers do not serialize on the lock
    itself, only on the wire-rate. Bounds S2 requests/second across all callers + threads. Raises
    ``S2Down`` (without reserving) when the backlog would make this caller wait > _PACE_MAX_WAIT_S, so
    a 429-storm sheds load + fails fast instead of stacking an unbounded multi-minute gate wait."""
    # on_backlog runs under the guard's pace lock with this caller's would-be wait: over the cap it
    # raises S2Down WITHOUT reserving a slot (the backlog drains), exactly as the inline check did.
    _guard.pace(on_backlog=lambda wait: (
        S2Down(f"rate-gate backlog {wait:.0f}s > {_PACE_MAX_WAIT_S:.0f}s; S2 storming, degrade")
        if wait > _PACE_MAX_WAIT_S else None))


# Eye-owned bounded retry on a 429, REPLACING the lib's (now-disabled) 10x/250s tenacity backoff.
# A 429 surfaces from the lib as ConnectionRefusedError (see _is_rate_limit); with the lib's own retry
# off, this SHORT loop rides out a brief throttle (the common case: clears in seconds) while a sustained
# throttle fails fast (a few seconds, not 260s) and lets the breaker take over. Bound, not abandon: it
# only shortens the doomed churn, never the still-productive retrying.
_RL_RETRIES = 2              # retries AFTER the first attempt (3 attempts total)
_RL_BACKOFF_S = (1.5, 3.0)   # waits between attempts (rate-limit only); added budget <= ~4.5s


class S2Down(RuntimeError):
    """Raised immediately while the circuit is open (recent consecutive failures)."""


def _slot_busy(wait: float) -> S2Down:
    """Concurrency-permit exhaustion -> the same degrade-to-[]/None path as breaker-open / a
    pathological rate backlog. Handed to _guard.slot as its on_busy factory."""
    return S2Down(f"concurrency pool saturated (no slot in {wait:.0f}s); degrade")


def breaker_open() -> bool:
    """True iff the shared S2 circuit is currently open (recent consecutive failures). A non-probing
    read of the breaker state, mirror of _openalex.breaker_open, so callers can stamp a degraded
    flag without an upstream probe."""
    with _lock:
        return time.time() < _state["open_until"]


def recently_throttled(within_s: float = 120.0) -> bool:
    """True iff S2 rate-limited us (HTTP 429) within ``within_s`` seconds, OR the circuit is open.
    Lets a caller tell a THROTTLE-induced empty result (retry / switch backend) apart from a genuine
    no-match. The 429 is stamped by _record_fail on a SINGLE failure, so this catches a cold-call
    throttle that has not yet tripped the (5-consecutive) breaker."""
    with _lock:
        last = _state.get("last_429", 0.0)
        open_until = _state.get("open_until", 0.0)
    return time.time() < open_until or (last > 0 and time.time() - last < within_s)


# ── shared client (singleton, lazy, thread-safe) ─────────────────────────────
# One ``SemanticScholar`` client reused across the cartographer + relations call sites
# instead of five fresh constructions. The semanticscholar lib's client is a thin httpx
# wrapper and is safe to share for concurrent reads; the global _sema still bounds in-flight
# concurrency. Keyed from auth.load("semantic_scholar") (the eye's API key → un-throttled
# tier), falling back to the S2_API_KEY env var, then the keyless shared pool.
_client = None  # type: ignore[var-annotated]
_client_lock = threading.Lock()


def _load_api_key() -> Optional[str]:
    from penumbra.core import auth  # local import: keep module import cheap + acyclic
    import os

    creds = auth.load("semantic_scholar") or {}
    key = creds.get("api_key") or os.environ.get("S2_API_KEY")
    return key or None


def get_client():
    """The shared, lazily-built Semantic Scholar client (singleton)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                from semanticscholar import SemanticScholar  # local: lib is a soft dep
                # retry=False turns OFF the lib's own tenacity backoff. Its default (retry=True) does
                # stop_after_attempt(10) + wait_exponential(5,60) ~= 250s of HIDDEN backoff on a 429,
                # which pre-empts this module's breaker/pace and surfaces as a ~260s fake-hang (the
                # field_skeleton cold-seed symptom). With it off, a 429 surfaces here in ~1s and the
                # eye owns the short, bounded retry (_retry_rl) instead.
                _client = SemanticScholar(api_key=_load_api_key(), timeout=TIMEOUT, retry=False)
    return _client


# ── id normalization (single source of truth) ────────────────────────────────
_ARXIV_RE = re.compile(r"\d{4}\.\d{4,5}(v\d+)?")


def norm_s2_id(s: str) -> str:
    """Normalize a seed id to a form Semantic Scholar accepts.

    A BARE arXiv id ("2203.02155") or a DOI is SILENTLY rejected by the S2
    paper/recommend endpoints (returns nothing, no error) — they need the
    "ArXiv:" / "DOI:" prefix. S2 paperIds and CorpusIDs already pass through.
    This is the ONE place this truth lives (was duplicated in cartographer +
    relations); it makes the docstrings' "pass arXiv ids / DOIs" actually true.
    """
    s = (s or "").strip()
    if _ARXIV_RE.fullmatch(s):
        return "ArXiv:" + s
    if s.lower().startswith("10.") or "doi.org/" in s.lower():
        return "DOI:" + s.split("doi.org/")[-1].strip("/")
    return s


# ── breaker bookkeeping ──────────────────────────────────────────────────────
def _check_open() -> None:
    with _lock:
        if time.time() < _state["open_until"]:
            raise S2Down(f"circuit open {_state['open_until'] - time.time():.0f}s more")


def _record_ok() -> None:
    _guard.record_ok()


def _is_rate_limit(exc: Exception) -> bool:
    """True when ``exc`` is S2's 429 signature. The semanticscholar lib masks the HTTP
    status behind its tenacity backoff: a 429 surfaces as ConnectionRefusedError or a
    RetryError whose message carries "429" / "Too Many Requests". This is the ONE place
    that detection lives (same pattern the source's hand-rolled loop used to inline)."""
    if isinstance(exc, ConnectionRefusedError):
        return True
    msg = str(exc)
    return "429" in msg or "Too Many Requests" in msg or "ConnectionRefusedError" in msg


def _record_fail(exc: Optional[Exception] = None) -> None:
    if exc is not None and _is_rate_limit(exc):  # stamp it so health() can surface it honestly
        with _lock:
            _state["last_429"] = time.time()
    _guard.record_fail()  # bump the streak; opens the circuit at _BREAK_AFTER consecutive (logs on open)


def _retry_rl(do):
    """Run ``do()``, retrying ONLY on a rate-limit (429 -> ConnectionRefusedError; see _is_rate_limit)
    with the eye's own SHORT bounded backoff. Any other exception propagates on the first occurrence
    exactly as before. This is the retry budget the lib used to own (and overspent at ~250s); here it
    is a handful of seconds, after which the caller degrades + the breaker takes over."""
    for _i in range(_RL_RETRIES + 1):
        try:
            return do()
        except Exception as exc:
            if _i >= _RL_RETRIES or not _is_rate_limit(exc):
                raise
            time.sleep(_RL_BACKOFF_S[min(_i, len(_RL_BACKOFF_S) - 1)])


def _call(label: str, fn):
    """Run one S2 client call behind the semaphore + breaker + eye-owned rate-limit retry.

    Returns ``fn()`` (the raw lib return — a record OR a lazy ``PaginatedResults``;
    NOTE: the lib defers the HTTP call to iteration time for paginated calls, so the
    breaker can only see failures that surface at call time here; iteration failures
    are bounded + degraded by the iterating wrappers below). ``S2Down`` propagates
    immediately while the circuit is open; a 429 is retried a few times (bounded) by
    _retry_rl; any other exception trips the breaker and re-raises so the wrapper degrades.
    """
    _check_open()

    def _attempt():
        _pace()  # rate cap: bounds req/s so a fan-out across the S2-backed sources can't burst the key
        with _guard.slot(_ACQUIRE_MAX_WAIT_S, _slot_busy):  # concurrency cap, BOUNDED (degrade, don't hang)
            return fn()

    try:
        r = _retry_rl(_attempt)
        _record_ok()
        return r
    except S2Down as exc:
        diag.note(f"s2.{label}", exc=exc)
        raise
    except Exception as exc:
        _record_fail(exc)
        diag.note(f"s2.{label}", exc=exc)
        raise


def _bounded(label: str, make_iter, cap: int) -> list:
    """Iterate a lazy ``PaginatedResults`` HARD-BOUNDED to ``cap`` items, behind the
    semaphore + breaker, degrading to ``[]`` on any error.

    CRITICAL (the trap the cartographer comment documents): the semanticscholar lib's
    ``limit`` is the PAGE SIZE, and iterating a PaginatedResults auto-fetches EVERY
    page. A famous seed (thousands of citers) would paginate without end (observed:
    358s / 3750 nodes / 429 storm / dropped HTTP). So ``make_iter`` must request a
    page size of ``cap`` (caller's job) AND we stop the iteration at ``cap`` here, so
    the call is bounded no matter how cited the seed is.
    """
    try:
        _check_open()

        def _attempt():
            _pace()  # rate cap: bounds req/s so a fan-out across the S2-backed sources can't burst the key
            with _guard.slot(_ACQUIRE_MAX_WAIT_S, _slot_busy):  # concurrency cap, BOUNDED (degrade, don't hang)
                it = make_iter()
                # islice pulls EXACTLY ``cap`` items and stops; the prior ``for i,item: if i>=cap: break``
                # pulled one item PAST cap, which advances the lazy PaginatedResults into its next-page
                # branch and fires a needless second HTTP page (offset=cap). At a cap that equals the page
                # size (the ranked path's per_source=15) that doubled every S2 call and the second page hit
                # S2's 429 throttle, so the lib's internal retry backoff blew past the broad deadline and the
                # source was silently dropped. One page, one request.
                return list(itertools.islice(it, cap))

        out = _retry_rl(_attempt)
        _record_ok()
        return out
    except S2Down as exc:
        logger.warning("s2 %s skipped: %s", label, exc)
        diag.note(f"s2.{label}", exc=exc)
        return []
    except Exception as exc:  # noqa: BLE001 — bounded iteration degrades to empty
        _record_fail(exc)
        logger.warning("s2 %s failed: %s", label, exc)
        diag.note(f"s2.{label}", exc=exc)
        return []


# ── thin wrappers (cartographer + relations call these instead of raw client) ─
# Each carries the semaphore + breaker. Single-record calls degrade to None; the
# paginated calls degrade to a bounded list. Callers keep their own cache + assembly.

def get_paper(paper_id: str, fields: Optional[list[str]] = None):
    """One paper record by id (normalized). None on any failure / breaker-open."""
    try:
        kwargs = {"fields": fields} if fields else {}
        return _call("get_paper", lambda: get_client().get_paper(norm_s2_id(paper_id), **kwargs))
    except Exception as exc:  # noqa: BLE001
        logger.warning("s2 get_paper %s failed: %s", paper_id, exc)
        return None


def search_paper(query: str, limit: int = 10, fields: Optional[list[str]] = None,
                 **kwargs) -> list:
    """Search papers, HARD-BOUNDED to ``limit`` (page size = limit so iteration can't
    page through every hit). Extra kwargs (year / venue / min_citation_count) pass
    through to the lib. Returns a list (degrades to [] on error)."""
    page = min(limit, 100)
    fkw = {"fields": fields} if fields else {}
    return _bounded("search_paper",
                    lambda: get_client().search_paper(query or "", limit=page, **fkw, **kwargs),
                    limit)


def snippet_search(query: str, limit: int = 20) -> list:
    """Passage-level full-text retrieval via S2 ``/graph/v1/snippet/search`` — the exact SENTENCES /
    sections across S2's open-access full-text corpus that match ``query`` (not just papers/abstracts).
    The ``semanticscholar`` lib does NOT expose this endpoint, so it is a raw ``http.get_json`` wrapped
    in ``_call`` for the SAME pace/semaphore/breaker guard every S2 caller shares (never hit S2 outside
    ``_call``). Returns the raw ``data`` list of ``{score, paper, snippet}`` records; degrades to [] on
    error (a 429 surfaces as get_json -> None -> [], so the pace+concurrency guard still bounds it even
    though the breaker cannot see the swallowed 429)."""
    def _go():
        from penumbra.core import http
        key = _load_api_key()
        headers = {"x-api-key": key} if key else {}
        # S2's snippet/search default response already carries paper.{corpusId,title,authors,
        # openAccessInfo} + snippet.{text,snippetKind,section,snippetOffset}; a custom ``fields`` param
        # 400s on this endpoint (its field grammar differs from /paper/search), so we take the default
        # shape. publicationDate / citationCount are simply absent (never fabricated).
        params = {"query": query or "", "limit": max(1, min(int(limit or 20), 1000))}
        data = http.get_json("https://api.semanticscholar.org/graph/v1/snippet/search",
                             params=params, headers=headers, timeout=TIMEOUT)
        return (data or {}).get("data", []) or []
    try:
        return _call("snippet_search", _go) or []
    except Exception:  # noqa: BLE001 — degrade to [] like the other wrappers (breaker already tripped upstream)
        return []


def get_paper_references(paper_id: str, limit: int, fields: Optional[list[str]] = None) -> list:
    """A paper's references, HARD-BOUNDED to ``limit`` (page size = limit). Returns the
    edge records (each carries ``.paper`` + ``.intents`` / ``.isInfluential``)."""
    page = min(limit, 100)
    fkw = {"fields": fields} if fields else {}
    return _bounded("get_paper_references",
                    lambda: get_client().get_paper_references(norm_s2_id(paper_id), limit=page, **fkw),
                    limit)


def get_paper_citations(paper_id: str, limit: int, fields: Optional[list[str]] = None) -> list:
    """A paper's citations, HARD-BOUNDED to ``limit`` (page size = limit). Returns the
    edge records (each carries ``.paper`` + ``.intents`` / ``.isInfluential``)."""
    page = min(limit, 100)
    fkw = {"fields": fields} if fields else {}
    return _bounded("get_paper_citations",
                    lambda: get_client().get_paper_citations(norm_s2_id(paper_id), limit=page, **fkw),
                    limit)


def get_recommended_papers(paper_id: str, fields: Optional[list[str]] = None,
                           limit: int = 20) -> list:
    """Recommended papers for ONE seed. The lib returns a plain list, but we cap
    defensively to ``limit`` regardless. Degrades to [] on error / breaker-open."""
    try:
        kwargs = {"fields": fields} if fields else {}
        raw = _call("get_recommended_papers",
                    lambda: get_client().get_recommended_papers(
                        norm_s2_id(paper_id), limit=min(limit, 100), **kwargs))
    except Exception as exc:  # noqa: BLE001
        logger.warning("s2 get_recommended_papers %s failed: %s", paper_id, exc)
        return []
    return list(raw or [])[:limit]


def get_recommended_papers_from_lists(positive_paper_ids: Iterable[str],
                                      fields: Optional[list[str]] = None,
                                      negative_paper_ids: Optional[Iterable[str]] = None,
                                      limit: int = 20) -> list:
    """Recommended papers from MULTIPLE positive seeds (and optional negatives). Seeds are
    normalized. Capped defensively to ``limit`` (mirrors the single-seed sibling). Degrades
    to [] on error / breaker-open."""
    pos = [norm_s2_id(s) for s in (positive_paper_ids or [])]
    neg = [norm_s2_id(s) for s in (negative_paper_ids or [])] if negative_paper_ids else None
    try:
        def _go():
            kwargs = {"positive_paper_ids": pos, "limit": min(limit, 100)}
            if fields:
                kwargs["fields"] = fields
            if neg:
                kwargs["negative_paper_ids"] = neg
            return get_client().get_recommended_papers_from_lists(**kwargs)
        raw = _call("get_recommended_papers_from_lists", _go)
    except Exception as exc:  # noqa: BLE001
        logger.warning("s2 get_recommended_papers_from_lists failed: %s", exc)
        return []
    return list(raw or [])[:limit]


def search_author(name: str, limit: int = 10) -> list:
    """Search authors by name, HARD-BOUNDED to ``limit`` (page size = limit so a
    common name can't page forever). Returns a list (degrades to [])."""
    page = min(limit, 10)
    return _bounded("search_author",
                    lambda: get_client().search_author(name, limit=page),
                    limit)


def get_author_papers(author_id: str, limit: int, fields: Optional[list[str]] = None) -> list:
    """One author's papers, HARD-BOUNDED to ``limit`` (page size = limit so a prolific
    author can't page forever). Returns a list (degrades to [])."""
    page = min(limit, 100)
    fkw = {"fields": fields} if fields else {}
    return _bounded("get_author_papers",
                    lambda: get_client().get_author_papers(author_id, limit=page, **fkw),
                    limit)


# ── shared health probe (single-flight + 60s cache) ──────────────────────────
_HEALTH_TTL_S = 60.0
_health: dict = {"at": 0.0, "result": None}
_health_lock = threading.Lock()


def health(timeout: float = 8.0) -> tuple[bool, str]:
    """ONE shared upstream probe for every S2-backed capability (single-flight + 60s cache).

    Mirror of ``_openalex.health``: the cartographer (field_skeleton/recommend), the relations
    layer (resolve_identity/coauthors) and the semantic_scholar source all delegate here instead
    of each probing S2 in its own health_check; the all-source health sweep otherwise fired them
    all at once, bursting the shared key into a 429 storm and tripping the breaker, so one transient
    probe storm read as "every S2 source down". One minimal search call (limit=1, fields=title) tests
    connectivity + key validity + the breaker/rate state, cached 60s and single-flighted (the probe
    runs under the lock) so N concurrent callers cause exactly ONE upstream call. A 429 means the API
    is UP and merely throttling us, so it reports healthy (the data path falls back to cache anyway);
    a recent-429 stamp is surfaced in the message so an active throttle stays legible.

    NOTE: the ``timeout`` arg is accepted for call-site parity with ``_openalex.health``; the shared
    client's timeout is fixed at construction (TIMEOUT), so it is advisory here, not re-applied.
    """
    now = time.monotonic()
    with _health_lock:
        if _health["result"] is not None and now - _health["at"] < _HEALTH_TTL_S:
            return _health["result"]
        try:
            # ONE minimal call through _call (so it shares the pace + semaphore + breaker and an
            # exception surfaces, unlike the degrading wrappers). search_paper returns a lazy
            # PaginatedResults; touching len()/the first page forces the HTTP call here under the lock.
            _call("health", lambda: get_client().search_paper(
                "machine learning", limit=1, fields=["title"]))
            ok, msg = True, "OK (shared S2 upstream reachable)"
        except S2Down as exc:
            # Self-shed (breaker / pool), NOT upstream-down: a genuine outage raises the raw
            # exception below (a 429 is handled there as UP). Don't flip every S2-backed source down
            # on a transient breaker-open; report DEGRADED (self-heals when the breaker closes).
            ok, msg = True, f"degraded (eye backing off, upstream not probed this cycle): {exc}"
        except Exception as exc:  # noqa: BLE001
            # A 429 means S2 is UP and merely throttling us -> report healthy (cache covers the data
            # path); any other exception is a genuine outage.
            if _is_rate_limit(exc):
                ok, msg = True, "OK (HTTP 429: API alive, rate-limiting us)"
            else:
                ok, msg = False, f"{type(exc).__name__}: {exc}"
        with _lock:
            last = _state.get("last_429", 0.0)
        if ok and last and (time.time() - last) < 1800 and "429" not in msg:
            msg = (f"OK (probe live), but a 429 hit {int(time.time() - last)}s ago: S2 is actively "
                   "throttling the shared key; the data path falls back to cache while it backs off")
        _health["at"] = time.monotonic()
        _health["result"] = (ok, msg)
        return _health["result"]
