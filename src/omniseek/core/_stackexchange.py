"""Shared Stack Exchange machinery for the keyless SE-API-backed scrape adapters.

Two things live here, both shared by ALL Stack Exchange sources (academia_se,
stackoverflow, crossvalidated, cs_se, datascience_se, ai_se — distinct ``site=``
params over the SAME api.stackexchange.com endpoint + keyless per-IP quota):

1. ``health()`` — ONE single-flight liveness probe.
   Before this, EACH adapter's health_check fired its own live /questions GET on the 6-hourly
   health sweep, spending the shared 10k/day quota once per source per sweep (the OpenAlex
   N-share-one-upstream probe anti-pattern, at small scale). Now every SE source delegates here:
   ONE minimal /questions GET, 60s single-flight cached, surfacing quota_remaining. Mirrors the
   _openalex.health single-flight idiom.

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
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from markdownify import markdownify as html_to_md

from omniseek.core import auth, diag, http
from omniseek.core._guard import BackendGuard, GateBusy, bounded_async_slot, bounded_slot
from omniseek.core.normalize import Document, jsonsafe, mk_signal

logger = logging.getLogger(__name__)

API_BASE = "https://api.stackexchange.com/2.3"
TIMEOUT = 15
ANSWERS_PER_QUESTION = 3  # cap: only the top-3 votes-ranked answers, mindful of the shared per-IP quota

# A FREE registered Stack Apps key raises the per-IP quota 300/day → 10,000/day (33x). It is NOT a
# secret (per SE docs) but lives host-only at ~/.omniseek/credentials/stackexchange.json {"key": "..."}
# (never committed), injected on every SE GET when present. Absent → the cluster runs keyless (300/day)
# and the breaker below absorbs the exhaustion. Register: https://stackapps.com/apps/oauth/register
auth.write_template("stackexchange", {
    "_comment": "FREE key from https://stackapps.com/apps/oauth/register (raises quota 300->10000/day "
                "per IP). Register an app (any name + description; OAuth Domain can be 'stackexchange.com'); "
                "paste the 'Key' field here. Not a secret, but host-only — never committed.",
    "key": "",
})
_SE_KEY = (auth.load("stackexchange") or {}).get("key") or ""

# ── shared quota / backoff circuit breaker ────────────────────────────────────
# All six SE sources share api.stackexchange.com's KEYLESS per-IP quota (small: ~300/day, NOT the
# 10k a registered app key gets). Under OmniSeek's multi-agent broad fan-out, ONE broad search fires
# 6 SE searches + per-question answer fetches (build_documents), so the quota empties fast and the
# API then returns HTTP 429 / a `backoff` throttle. Retried per-source-per-call that became a 429
# STORM (measured: 51+ log lines + ~20s latency on every later broad search + a health flap). A
# SHARED breaker trips a cooldown on the first quota-429/backoff so the whole SE cluster fast-skips
# the spent API until it heals — same shape as the reddit Arctic / sogou guards.
_se_lock = threading.Lock()
_se_cooldown_until = 0.0
_se_fail_streak = 0
_SE_TRIP_AFTER = 3     # a couple of quota-429s/backoffs ⇒ the shared per-IP quota is spent, not a blip
_SE_COOLDOWN = 300.0   # seconds to skip the shared API once tripped (de-storms; then it re-probes)

# Global in-flight cap on the shared SE host (api.stackexchange.com). All six SE sources + every agent
# share its keyless per-IP quota; the breaker above stops hammering a SPENT quota, but nothing bounded
# the CONCURRENT request storm that spends it. This semaphore (held only around the egress in _se_get)
# caps in-flight SE requests so a broad-fan-out burst paces through instead of cascading into 429s,
# mirroring _s2 / _openalex / _github / reddit. 4 = low end of the proven-safe band (the quota is tight).
# Only the in-flight cap is shared machinery (via BackendGuard.sema); SE's quota/backoff breaker above
# is a genuinely different shape (streak + cooldown, not fails + open_until) and stays module-local.
_SE_MAX_INFLIGHT = 4
_se_sema = BackendGuard("stackexchange", _SE_MAX_INFLIGHT).sema


def _se_cooling() -> bool:
    return time.monotonic() < _se_cooldown_until


def _se_record(ok: bool, backoff: float = 0.0) -> None:
    """Feed one SE-API outcome to the shared breaker: a clean response resets the streak; a 429/None
    or a `backoff` throttle extends it and trips a cooldown once the streak shows the quota is spent."""
    global _se_fail_streak, _se_cooldown_until
    with _se_lock:
        if ok and not backoff:
            _se_fail_streak = 0
            return
        _se_fail_streak += 1
        if _se_fail_streak >= _SE_TRIP_AFTER and not _se_cooling():
            cd = max(_SE_COOLDOWN, backoff)
            _se_cooldown_until = time.monotonic() + cd
            logger.warning("Stack Exchange quota/backoff hit (%d consecutive); skipping the shared "
                           "per-IP API %ds (keyless ~300/day quota spent — a free app key would 33x it)",
                           _se_fail_streak, int(cd))


def _se_get(url: str, params: dict, timeout: float = TIMEOUT) -> Optional[dict]:
    """Shared SE API GET behind the quota breaker: skip instantly while cooling; trip on a 429/backoff
    so all six SE sources stop hammering a spent per-IP quota (no 429 storm, no per-search latency)."""
    if _se_cooling():
        return None
    try:
        # The gate wait shares the request's existing wire-time budget. A saturated local queue is
        # self-load, not an upstream failure, so shed it without feeding the quota breaker.
        with bounded_slot(
            _se_sema,
            timeout,
            lambda waited: GateBusy(f"Stack Exchange gate busy after {waited:.1f}s"),
        ):
            data = http.get_json(
                url,
                params={**params, "key": _SE_KEY} if _SE_KEY else params,
                timeout=timeout,
            )
    except GateBusy as exc:
        diag.note("stackexchange.gate", url=url, exc=exc)
        return None
    if data is None:  # HTTP failure (429 quota / 5xx / timeout) — http.get_json already logged it
        _se_record(False)
        return None
    _se_record(True, backoff=float((isinstance(data, dict) and data.get("backoff")) or 0))
    return data


# ── health probe ────────────────────────────────────────────────────────────
_HEALTH_TTL_S = 60.0
_health: dict = {"at": 0.0, "result": None}
_health_lock = threading.Lock()


def health(timeout: float = 10.0) -> tuple[bool, str]:
    """One shared, 60s single-flight cached liveness probe for all Stack Exchange-backed sources.

    A minimal /questions GET through the shared pooled http client; every SE adapter delegates here
    so the health sweep makes ONE probe (not one-per-source) against the shared per-IP quota, and
    surfaces quota_remaining."""
    now = time.monotonic()
    with _health_lock:
        if _health["result"] is not None and now - _health["at"] < _HEALTH_TTL_S:
            return _health["result"]
        if _se_cooling():  # keyless per-IP quota spent (resets daily): the API is UP, we are just
            # out of free quota for now, a budget state, NOT an outage. Report DEGRADED so all 6 SE
            # sources don't flip down on the shared daily-quota cooldown (they self-heal at reset;
            # 2026-07-23 watchdog false-mass-down fix). Do not spend a probe.
            _health["at"], _health["result"] = now, (True, "degraded (keyless per-IP quota spent; resets daily; upstream up)")
            return _health["result"]
        try:
            data = _se_get(
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
        _health["at"] = time.monotonic()
        _health["result"] = (ok, msg)
        return _health["result"]


# ── document mapping (question + answers → Documents) ──────────────────
def _body_md(body_html: str) -> str:
    """Body HTML → Markdown (markdownify), falling back to a crude tag-strip on failure."""
    if not body_html:
        return ""
    try:
        return html_to_md(body_html, heading_style="ATX").strip()
    except Exception:  # noqa: BLE001
        return re.sub(r"<[^>]+>", "", body_html).strip()


def _ts(creation_ts: Optional[int]) -> Optional[datetime]:
    return datetime.fromtimestamp(creation_ts, tz=timezone.utc) if creation_ts else None


def search(query: str, limit: int, site: str) -> Optional[dict]:
    """The shared /search/advanced GET. Returns parsed JSON or None on failure (the adapter
    contract — None ⇒ search ⇒ []). ``sort=relevance`` so the page is already server-ranked."""
    params = {
        "order": "desc",
        "sort": "relevance",
        "q": query,
        "site": site,
        "pagesize": min(limit, 30),
        "filter": "withbody",  # include body text in the response
    }
    url = f"{API_BASE}/search/advanced"
    data = _se_get(url, params, timeout=TIMEOUT)
    # An HTTP failure already noted itself in http.get_json. The SE-specific empty (a 200 that
    # carried no items: a backoff throttle body, a moved/renamed site param, a contract change)
    # would otherwise be an invisible [] → surface it for the fixing agent.
    if isinstance(data, dict) and not data.get("items"):
        diag.note("stackexchange.search", url=url,
                  body=f"site={site!r}: response carried no 'items' "
                       f"(quota_remaining={data.get('quota_remaining')}, "
                       f"backoff={data.get('backoff')}, error={data.get('error_message')})")
    return data


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


def fetch_answer_documents(question_item: dict, source: str, site: str, site_host: str) -> list[Document]:
    """Fetch the top (votes-ranked) answers for ONE question and map each to its own doc.

    Keyless GET /questions/{id}/answers?filter=withbody&sort=votes&order=desc&pagesize=3. Returns
    [] on any failure (a question with no answers, an API hiccup) so the question doc still ships."""
    question_id = question_item.get("question_id")
    if not question_id:
        return []
    data = _se_get(
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


def build_documents(raw: dict, limit: int, source: str, site: str, site_host: str) -> list[Document]:
    """The shared ``_to_documents`` body for every SE adapter: for each of the first ``limit``
    questions emit the question doc AND its top answer docs (the gold). The page is capped at
    ``limit`` QUESTIONS; answers are an extra (bounded) burst per kept question."""
    items = (raw.get("items") or [])[:limit]
    docs: list[Document] = []
    for item in items:
        try:
            docs.append(question_to_document(item, source, site_host))
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s: skipping malformed question: %s", source, exc)
            continue
        docs.extend(fetch_answer_documents(item, source, site, site_host))
    return docs


def fetch_question_document(url: str, source: str, site: str, site_host: str) -> Optional[Document]:
    """Single-URL drill-down (the academia_se / stackoverflow ``fetch_url`` body): claim a
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
    data = _se_get(f"{API_BASE}/questions/{q_id}", {"site": site, "filter": "withbody"}, timeout=TIMEOUT)
    if data is None:
        return None
    items = data.get("items") or []
    if not items:
        return None
    return question_to_document(items[0], source, site_host)


# ── async egress twins (S4d: the 6 SE sources go NATIVE async) ────────────────
# Pure ADDITIONS mirroring _se_get / search / fetch_answer_documents / build_documents. They REUSE
# every existing pure-CPU mapper (question_to_document / _answer_to_document / _body_md / _ts) and the
# SHARED quota breaker + in-flight cap (_se_cooling / _se_record / _se_sema / _SE_KEY / TIMEOUT). The
# sync fns above are UNTOUCHED (the other callers -- omniseek_gather / sensors / curator via search_many --
# still use them); async and sync share ONE global cap so the migration cannot double the quota storm.
async def _ase_get(url, params, timeout=TIMEOUT):
    """Async twin of `_se_get`: SAME shared quota breaker + SAME `_se_sema` global in-flight cap
    (NOT a new asyncio.Semaphore -- the cap is shared sync<->async across the migration). The
    threading.BoundedSemaphore is acquired OFF the loop (a `with _se_sema:` on the loop would freeze
    the event loop); release on the loop (instant). `_se_record`/`_se_cooling` hold `_se_lock` only for
    microsecond counter math -> fine on loop. cache_only/5xx/429 -> aget_json None -> record fail
    (byte-identical to `_se_get`; the pre-existing cache_only false-record is mirrored, NOT fixed --
    fixing it would diverge from sync)."""
    if _se_cooling():
        return None
    try:
        async with bounded_async_slot(
            _se_sema,
            timeout,
            lambda waited: GateBusy(f"Stack Exchange gate busy after {waited:.1f}s"),
        ):
            data = await http.aget_json(
                url,
                params={**params, "key": _SE_KEY} if _SE_KEY else params,
                timeout=timeout,
            )
    except GateBusy as exc:
        diag.note("stackexchange.gate", url=url, exc=exc)
        return None
    if data is None:
        _se_record(False)
        return None
    _se_record(True, backoff=float((isinstance(data, dict) and data.get("backoff")) or 0))
    return data


async def asearch(query: str, limit: int, site: str) -> Optional[dict]:
    """Async twin of `search` (the /search/advanced GET). Byte-identical params + the SAME no-items
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
    """Async twin of `fetch_answer_documents`: SAME keyless /questions/{id}/answers GET via `_ase_get`,
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
    """Async twin of `build_documents` (R1: the answer layer egresses). For each of the first `limit`
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
