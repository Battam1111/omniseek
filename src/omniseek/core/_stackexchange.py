"""Shared Stack Exchange machinery for the keyless SE-API-backed scrape adapters.

Two things live here, both shared by ALL Stack Exchange sources (academia_se,
stackoverflow, crossvalidated, cs_se, datascience_se, ai_se — distinct ``site=``
params over the SAME api.stackexchange.com endpoint + keyless per-IP quota):

1. ``ahealth()`` — ONE single-flight liveness probe.
   Before this, EACH adapter's health_check fired its own live /questions GET on the 6-hourly
   health sweep, spending the shared 10k/day quota once per source per sweep (the OpenAlex
   N-share-one-upstream probe anti-pattern, at small scale). Now every SE source delegates here:
   ONE minimal /questions GET, 60s single-flight cached, surfacing quota_remaining. Mirrors the
   _openalex.ahealth single-flight idiom.

2. ``build_documents()`` + helpers — the QUESTION + ANSWERS → docs map.
   The gold of a Stack Exchange question is its votes-ranked ACCEPTED answer, not the question
   body. So for every question on the returned page we ALSO fetch its top answers (keyless GET
   /questions/{id}/answers?filter=withbody&sort=votes&order=desc&pagesize=3) and emit EACH answer
   as its own Document (source_id "{qid}a{aid}", title "A: <question title>", content =
   answer body markdown, signals = answer score, metadata.is_accepted). The question doc is kept
   too. Capped at 3 answers/question and only for the returned page, mindful of the shared per-IP
   quota. All six SE adapters reuse this — they are thin subclasses that only declare site/name/
   description/domains and call ``build_documents`` from ``_to_documents``.

The DATA path rides the shared pooled http client (http.get_json); the per-IP quota is generous
(10k/day, not a self-DOS).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

from markdownify import markdownify as html_to_md

from omniseek.core import _probe, auth, cache, diag, http, upstreams
from omniseek.core._guard import GateBusy, deadline_refusal
from omniseek.core.normalize import Document, html_fragment_text, jsonsafe, mk_signal

logger = logging.getLogger(__name__)

API_BASE = "https://api.stackexchange.com/2.3"
TIMEOUT = 15
ANSWERS_PER_QUESTION = 3  # cap: only the top-3 votes-ranked answers, mindful of the shared per-IP quota

# A FREE registered Stack Apps key raises the per-IP quota 300/day → 10,000/day (33x). It is NOT a
# secret (per SE docs) but lives host-only at <credentials>/stackexchange.json {"key": "..."}
# (never committed), injected on every SE GET when present. Absent → the cluster runs keyless (300/day)
# and the guard's breaker below absorbs the exhaustion. Register: https://stackapps.com/apps/oauth/register
auth.register_template("stackexchange", {
    "_comment": "FREE key from https://stackapps.com/apps/oauth/register (raises quota 300->10000/day "
                "per IP). Register an app (any name + description; OAuth Domain can be 'stackexchange.com'); "
                "paste the 'Key' field here. Not a secret, but host-only — never committed.",
    "key": "",
})
_SE_KEY = (auth.load("stackexchange") or {}).get("key") or ""

# ── the shared Stack Exchange guard: in-flight cap, start windows, breaker ─────────────────────
# All six SE sources share api.stackexchange.com's per-IP quota (keyless ~300/day, 10,000 with the key
# above). ONE broad search fires 6 SE searches + per-question answer fetches (abuild_documents), so the
# quota empties fast and the API then answers throttle_violation or carries a `backoff`; retried
# per-source-per-call that became a storm (measured: 51+ log lines + ~20s latency on every later broad
# search + a health flap). So the six sources share ONE guard, the registry's (upstreams.json
# "stackexchange"): at most 4 requests in flight and 30 starts in any second (the throttle page: more
# than 30 a second from one IP are dropped), taken permit-first; a `backoff` in a response defers its
# start slot for EVERY caller (the docs: the app MUST wait that many seconds); and its breaker (3 failures
# in a row that say the API is unhealthy, then 300 s, both declared there) makes the cluster fast-skip a
# spent quota. Until EYE-BREAKER-ONE (2026-11-07) the breaker was a count and cooldown of this module's
# own, which also counted a bad_parameter answer; it now goes through the guard's one rule
# (_guard.counts_against_upstream), see _se_failure.
_se_guard = upstreams.guard("stackexchange", log=logger)
_SE_MAX_INFLIGHT = _se_guard.max_inflight
_se_sema = _se_guard.sema

# The API answers EVERY error with HTTP 400 (api.stackexchange.com/docs/error-handling, read
# 2026-11-07): what went wrong is the wrapper's error_id, numbered like HTTP codes (400 bad_parameter,
# 404 no_method, 500 internal_error, 502 throttle_violation, 503 temporarily_unavailable). So the
# breaker judges the error_id, not the bare 400, and a throttle_violation is the API's own rate limit.
_SE_THROTTLE_VIOLATION = 502
_ERROR_ID = re.compile(r'"error_id"\s*:\s*(\d+)')


def _se_failure(seen: list) -> tuple[Optional[int], bool]:
    """(status, throttled) of the last failed request, from the record http noted: the body's
    error_id when the API sent one, else the HTTP status (None when no answer came)."""
    for rec in reversed(seen or ()):
        if not isinstance(rec, dict):
            continue
        m = _ERROR_ID.search(str(rec.get("body") or ""))
        status = int(m.group(1)) if m else rec.get("status")
        if isinstance(status, int):
            return status, status in (429, _SE_THROTTLE_VIOLATION)
    return None, False


def _se_refuse(waited: float) -> GateBusy:
    """The error for a request the gate could not start before the caller's deadline (its permit, or a
    start slot pushed past it by a pending `backoff` or the rate)."""
    return GateBusy(deadline_refusal("Stack Exchange gate", waited))


def _se_observe(data) -> float:
    """Record the quota the API reported in its response wrapper and honour its `backoff`: the start
    slot of every caller is deferred that many seconds. Returns the backoff (0.0 when none)."""
    if not isinstance(data, dict):
        return 0.0
    upstreams.observe("stackexchange", {k: data.get(k) for k in ("quota_max", "quota_remaining")
                                        if data.get(k) is not None}, 200)
    try:
        bo = float(data.get("backoff") or 0)
    except (TypeError, ValueError):
        bo = 0.0
    if bo > 0:
        _se_guard.defer(bo)
    return max(bo, 0.0)


def _se_open_s() -> float:
    """Seconds the guard's breaker stays open (0.0 when closed)."""
    return max(0.0, _se_guard.state["open_until"] - time.time()) if _se_guard.is_open() else 0.0


def _cache_only_skip(url: str) -> bool:
    """True on a cache-only collect (omniseek_search staleness=cache_only): the Stack Exchange answer is
    cached per query by the caller, so a miss reaching here has nothing to read and must not touch
    the network. http.get_json would return None without egress, but until 2026-10-08 that None went
    through the gate (a permit and a pacing slot for a request that never happened) and was then
    counted as a quota failure, so three cache-only misses opened the breaker and the next real
    searches of all six Stack Exchange sources were skipped for the breaker's whole window."""
    if not cache.cache_only():
        return False
    diag.note("stackexchange.cache_only", url=url,
              body="cache-only collect: no cached Stack Exchange answer, no live request made")
    return True


# ── health probe ────────────────────────────────────────────────────────────
_HEALTH_TTL_S = 60.0
_health: dict = {"at": 0.0, "result": None}


async def ahealth(timeout: float = 10.0) -> tuple[Optional[bool], str]:
    """One shared, 60s single-flight cached liveness probe for all Stack Exchange-backed sources.

    A minimal /questions GET through the shared pooled http client; every SE adapter delegates here
    so the health sweep makes ONE probe (not one-per-source) against the shared per-IP quota, and
    surfaces quota_remaining."""
    return await _probe.shared_verdict(_health, _HEALTH_TTL_S, lambda: _aprobe(timeout))


async def _aprobe(timeout: float) -> tuple[Optional[bool], str]:
    """The probe behind ``ahealth`` (run once per TTL by ``_probe.shared_verdict``)."""
    if _se_guard.is_open():  # the breaker is open, mostly a spent per-IP quota (resets daily): the
        # API is UP, we are just out of quota for now, a budget state, NOT an outage, so all 6 SE
        # sources must not flip down on it (they self-heal; 2026-07-23 watchdog false-mass-down fix).
        # Do not spend a probe; and since nothing is sent, nothing is verified: None (the
        # watchdog's `unverified`), neither healthy nor failing.
        return None, _probe.breaker_open(
            "Stack Exchange quota or throttle, consecutive failures", _se_open_s())
    # Cached for all six SE sources, so re-read HERE: a probe that failed only on a 429 or a
    # throttle body ("throttle_violation") is "not verified" for all of them, not "down".
    with _probe.watching() as led:
        try:
            data = await _ase_get(
                f"{API_BASE}/questions",
                {"site": "stackoverflow", "pagesize": 1, "order": "desc", "sort": "activity"},
                timeout=timeout,
            )
            if data is None:
                ok, msg = False, "no response (pooled GET returned None)"
            elif not data.get("items"):
                ok, msg = False, "no items returned"
            else:
                ok, msg = True, f"OK (quota={data.get('quota_remaining', '?')})"
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"{type(exc).__name__}: {exc}"
    ok, msg = _probe.reread(ok, msg, led)
    return ok, msg


# ── document mapping (question + answers → Documents) ──────────────────
def _body_md(body_html: str) -> str:
    """Body HTML → Markdown (markdownify), falling back to plain text (html_fragment_text) on failure."""
    if not body_html:
        return ""
    try:
        return html_to_md(body_html, heading_style="ATX").strip()
    except Exception:  # noqa: BLE001
        return html_fragment_text(body_html)


def _ts(creation_ts: Optional[int]) -> Optional[datetime]:
    return datetime.fromtimestamp(creation_ts, tz=timezone.utc) if creation_ts else None


def question_to_document(item: dict, source: str, site_host: str) -> Document:
    """One question item → its Document (the question body). ``site_host`` is the public
    web host (e.g. ``stats.stackexchange.com``) used to synthesize a URL when ``link`` is absent."""
    question_id = item.get("question_id") or 0
    url = item.get("link") or f"https://{site_host}/questions/{question_id}"
    title = item.get("title") or "(no title)"
    owner = (item.get("owner") or {}).get("display_name")
    return Document(
        source=source,
        source_id=str(question_id),
        url=url,
        title=title,
        content=_body_md(item.get("body") or "") or "(empty body)",
        author=owner,
        date=_ts(item.get("creation_date")),
        signals=mk_signal("votes", item.get("score"), kind="engagement", by=f"{source}/score"),
        tags=item.get("tags") or [],
        metadata={
            "answer_count": item.get("answer_count"),
            "view_count": item.get("view_count"),
            "is_answered": item.get("is_answered"),
            "accepted_answer_id": item.get("accepted_answer_id"),
            "raw": jsonsafe(item),
        },
    )


def _answer_to_document(ans: dict, question_item: dict, source: str, site_host: str) -> Document:
    """One answer item → its own Document. The actual gold: the votes-ranked answer body,
    carried under the question's title (``A: <title>``) so triage still reads as a Q&A pair."""
    question_id = question_item.get("question_id") or ans.get("question_id") or 0
    answer_id = ans.get("answer_id") or 0
    q_title = question_item.get("title") or "(no title)"
    # SE answers don't have their own permalink field; the canonical anchor URL is #<answer_id>.
    url = f"https://{site_host}/a/{answer_id}" if answer_id else (
        question_item.get("link") or f"https://{site_host}/questions/{question_id}"
    )
    owner = (ans.get("owner") or {}).get("display_name")
    is_accepted = bool(ans.get("is_accepted"))
    return Document(
        source=source,
        source_id=f"{question_id}a{answer_id}",
        url=url,
        title=f"A: {q_title}",
        content=_body_md(ans.get("body") or "") or "(empty body)",
        author=owner,
        date=_ts(ans.get("creation_date")),
        signals=mk_signal("votes", ans.get("score"), kind="engagement", by=f"{source}/answer_score"),
        tags=question_item.get("tags") or [],
        metadata={
            "is_accepted": is_accepted,
            "accepted": is_accepted,  # convenience alias (the brief's "accepted flag")
            "question_id": question_id,
            "answer_id": answer_id,
            "question_title": q_title,
            "raw": jsonsafe(ans),
        },
    )


async def afetch_question_document(url: str, source: str, site: str, site_host: str) -> Optional[Document]:
    """Single-URL drill-down (the academia_se / stackoverflow ``afetch_url`` body): claim a
    ``questions/<id>`` URL on ``site_host`` and build the question doc by id. None if not ours."""
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if site_host not in host:
        return None
    path = urlparse(url).path.strip("/").split("/")
    if len(path) < 2 or path[0] != "questions":
        return None
    try:
        q_id = int(path[1])
    except ValueError:
        return None
    data = await _ase_get(f"{API_BASE}/questions/{q_id}", {"site": site, "filter": "withbody"}, timeout=TIMEOUT)
    if data is None:
        return None
    items = data.get("items") or []
    if not items:
        return None
    return question_to_document(items[0], source, site_host)


# ── async egress twins (S4d: the 6 SE sources go NATIVE async) ────────────────
# Pure ADDITIONS mirroring the (since deleted) sync _se_get / search / fetch_answer_documents / build_documents. They REUSE
# every existing pure-CPU mapper (question_to_document / _answer_to_document / _body_md / _ts) and the
# SHARED guard (_se_guard: breaker + in-flight cap + start windows; _SE_KEY / TIMEOUT). The
# sync fns above are UNTOUCHED (the other callers -- omniseek_gather / sensors / curator via search_many --
# still use them); async and sync share ONE global cap so the migration cannot double the quota storm.
async def _ase_get(url, params, timeout=TIMEOUT):
    """The one Stack Exchange egress: the shared guard's breaker, then its permit and start slot (waited
    for in the gate's one line on the loop, without blocking it: `_se_guard.ahold`), then the GET.
    The outcome goes to the guard's breaker under its one rule (`_guard.counts_against_upstream`): a
    failure counts unless the API's error_id says the request itself was wrong (400 bad_parameter,
    404 no_method; see `_se_failure`); a throttle_violation or a 429 counts as the API's rate limit; a
    response that carries `backoff` defers every caller (`_se_observe`) and counts as throttled too; a
    clean response resets the count. A cache-only collect returns None before the gate and records
    nothing (`_cache_only_skip`)."""
    if _se_guard.is_open():
        diag.note("stackexchange.breaker_open", url=url, body=(
            f"Stack Exchange breaker open for {_se_open_s():.0f}s more (consecutive quota / throttle / "
            "server failures), so NO request was sent: an empty result now means we did not ask"))
        return None
    if _cache_only_skip(url):
        return None
    try:
        async with _se_guard.ahold(_se_refuse, request_s=timeout):
            # A quota-boxed Stack Exchange host can manifest as connect failures, so an in-slot retry would double one paced request.
            with diag.tap() as seen:
                data = await http.aget_json(
                    url,
                    params={**params, "key": _SE_KEY} if _SE_KEY else params,
                    timeout=timeout,
                    retry_transient=False,
                )
    except GateBusy as exc:
        diag.note("stackexchange.gate", url=url, exc=exc)
        return None
    if data is None:
        status, throttled = _se_failure(seen)
        _se_guard.record_fail(status=status, throttled=throttled)
        return None
    if _se_observe(data) > 0:
        _se_guard.record_fail(throttled=True)
    else:
        _se_guard.record_ok()
    return data


async def asearch(query: str, limit: int, site: str) -> Optional[dict]:
    """Async replacement of the deleted sync `search` (11-04, S9; this is the only one now) (the /search/advanced GET). Byte-identical params + the SAME no-items
    diag.note. Returns parsed JSON or None (contract)."""
    params = {
        "order": "desc",
        "sort": "relevance",
        "q": query,
        "site": site,
        "pagesize": min(limit, 30),
        "filter": "withbody",  # include body text in the response
    }
    url = f"{API_BASE}/search/advanced"
    data = await _ase_get(url, params, timeout=TIMEOUT)
    # An HTTP failure already noted itself in http.aget_json. The SE-specific empty (a 200 that
    # carried no items) would otherwise be an invisible [] -> surface it for the fixing agent.
    if isinstance(data, dict) and not data.get("items"):
        diag.note("stackexchange.search", url=url,
                  body=f"site={site!r}: response carried no 'items' "
                       f"(quota_remaining={data.get('quota_remaining')}, "
                       f"backoff={data.get('backoff')}, error={data.get('error_message')})")
    return data


async def afetch_answer_documents(question_item: dict, source: str, site: str,
                                  site_host: str) -> list[Document]:
    """Async replacement of the deleted sync `fetch_answer_documents` (11-04, S9; this is the only one now): SAME keyless /questions/{id}/answers GET via `_ase_get`,
    SAME ANSWERS_PER_QUESTION cap, SAME `_answer_to_document` map (pure CPU, on loop). [] on failure."""
    question_id = question_item.get("question_id")
    if not question_id:
        return []
    data = await _ase_get(
        f"{API_BASE}/questions/{question_id}/answers",
        {
            "site": site,
            "filter": "withbody",
            "sort": "votes",
            "order": "desc",
            "pagesize": ANSWERS_PER_QUESTION,
        },
        timeout=TIMEOUT,
    )
    if data is None:
        return []
    docs: list[Document] = []
    for ans in (data.get("items") or [])[:ANSWERS_PER_QUESTION]:
        try:
            docs.append(_answer_to_document(ans, question_item, source, site_host))
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s: skipping malformed answer: %s", source, exc)
    return docs


async def abuild_documents(raw: dict, limit: int, source: str, site: str,
                           site_host: str) -> list[Document]:
    """Async replacement of the deleted sync `build_documents` (11-04, S9; this is the only one now) (R1: the answer layer egresses). For each of the first `limit`
    questions: build the question doc (`question_to_document`, pure CPU on loop) then AWAIT its answer
    docs (`afetch_answer_documents`). Keep the SAME doc ORDER as sync (q0, *a0, q1, *a1, ...) -> await
    answers SEQUENTIALLY per question (byte-identical order; no asyncio.gather -- ordering-parity over
    a marginal latency win, and the sema would serialize a gather at cap 4 anyway)."""
    items = (raw.get("items") or [])[:limit]
    docs: list[Document] = []
    for item in items:
        try:
            docs.append(question_to_document(item, source, site_host))
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s: skipping malformed question: %s", source, exc)
            continue
        docs.extend(await afetch_answer_documents(item, source, site, site_host))
    return docs
