"""arXiv adapter — queries the arXiv Atom API directly through the shared http helpers.

arXiv has a free public API (``export.arxiv.org/api/query``) with no auth. We hit it via
``eye.http`` (shared UA / timeout / 30MB size-cap / streaming) and parse the Atom feed with
``feedparser`` — the SAME parser the official ``arxiv`` package uses internally, so the
field mapping is well-understood.

**2026-06-04 (P29):** migrated OFF the ``arxiv`` library. That library fetches via
``urllib`` with NO request timeout (it blocked all the way to our outer ``fetch_one``
deadline under arXiv's throttling) and uses its own dedicated User-Agent that arXiv
rate-limits independently. Routing through ``http.py`` gives a real per-request timeout,
our own UA, and the body-size cap — and degrades to ``[]`` on failure instead of blocking.

Native query syntax passes straight through: the API's ``search_query`` forwards the
string verbatim, so field prefixes + booleans work as-is — ``cat:cs.LG``, ``au:bengio``,
``ti:transformer``, ``abs:diffusion``, ``ti:llm AND cat:cs.CL``.

A PLAIN SENTENCE IS NOT SENT VERBATIM (EYE-PAPERS, 2026-10-18): arXiv reads bare words as OR
(its echo of "threshold verifiable random function post-quantum lattice" was ``all:threshold OR
all:verifiable OR ...``), and its relevance ranked 2007 lattice-QCD physics first. A query with no
field prefix, no boolean and no time window is sent as its content words ANDed under ``all:``
(stopwords out: arXiv treats ``all:the`` as a filter). When that match is short, the shared plan in
``_relax`` asks how many records each word matches on its own and drops the least distinctive word,
one at a time, until the match is long enough; the order stays arXiv's relevance. (Matching a
relaxed set in the title, ``ti:``, was measured on 2026-10-18 and was not better: see
out/EYE-PAPERS/report.md.) Each result carries
the word set it matched (``metadata.matched_words``); a legal empty answer leaves an
``arxiv.no_match`` diagnostic naming the word sets tried, and an answer that is not an Atom feed an
``arxiv.unparseable`` one.

**B2 migration:** now rides ``BaseAPIAdapter`` (template-method base). The two hooks
``_raw_fetch`` / ``_to_document`` carry the verbatim arXiv I/O + field mapping; the base
supplies the cache-checked ``search`` skeleton + auto-registration. ``rank_locally=False``
preserves the server's order byte-for-byte (no local re-rank): ``sortBy=relevance`` by default,
``sortBy=submittedDate`` descending for a recency query (``_search_params``, 2026-10-11).
``fetch_url`` (id_list by-id lookup) and ``health_check``
(treats HTTP 429 as alive) are overridden because they differ from the base defaults.

FULL TEXT (EYE-RSS-3, 2026-10-24): an arxiv.org ``/pdf/<id>`` or ``/html/<id>`` link is the paper's
whole text, read from export.arxiv.org (``_arxiv_fulltext``) under that host's own full-text gate, not
through the API above and not under arxiv.org's 15 s Crawl-delay. ``/html/`` is arXiv's LaTeXML page
as text with ``#`` section lines (omniseek_read level="head" cuts it, appendices included); a paper with no
HTML version, or whose render has no heading under the title (a source that only includes a PDF), gives
the abstract with ``metadata.fulltext`` false and the reason. ``/pdf/`` that cannot
be had (no turn at the gate by the caller's deadline, download failed, no text) falls back to the html of the same id
(``metadata.fulltext_from`` "html", ``metadata.pdf_reason``); both failing returns None, and the
diagnostic names both reasons. Other arXiv links (``/abs/``) stay the API lookup by id.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

import anyio
import feedparser

from omniseek.core import _guard as _guard_mod
from omniseek.core import _probe, diag, fetcher, http, upstreams
from omniseek.core.normalize import Document
from omniseek.core.sources.api import _arxiv_fulltext, _relax
from omniseek.core.sources.api._base import BaseAPIAdapter

logger = logging.getLogger(__name__)

_API = "https://export.arxiv.org/api/query"
# The entry key carrying the word set a search matched (set by the adapter, never by arXiv).
_MATCHED_KEY = "omniseek_matched_words"

# arXiv load-guard: the shared BackendGuard (the concurrency cap + rate pacer + circuit breaker that
# _s2 / _openalex / _github already ride). arXiv rate-limits the export host aggressively (this adapter
# migrated off the arxiv lib for exactly that). The OLD sema bounded CONCURRENCY but NOT req/s, so 4
# concurrent workers could still burst past arXiv's ~3/s politeness line into a 429, and a throttled/dead
# host made every request eat the full timeout. The guard adds a min-interval RATE pacer (spaces request
# STARTS so a fan-out can't spike the rate), a breaker (consecutive failures -> fail fast + serve cache
# instead of hammering), and backlog fail-fast. SHARED sync<->async so the async path cannot double
# the concurrency OR the rate. Order: breaker -> permit -> start slot -> record ok/fail (the
# permit-first hold, see _guard.BackendGuard.ahold). The in-flight cap was 4 until 2026-09-28 and is
# now the declared 1 (see below).
#
# THE RATE IS arXiv's NUMBER, NOT A TUNABLE. arXiv's API terms say ONE request every THREE seconds.
# This constant read 0.35 until 2026-08-19, with a comment calling that "arXiv's politeness rate":
# the 3 was read as 3 req/s instead of 1 req/3s, so we ran ~9x over the line. The comment also
# recorded the reason it was set that wide, "too wide would get arXiv deadline-dropped in the
# ~95-source sweep", which is the actual root cause: a published rate limit was traded away for
# sweep coverage. arXiv collected on 2026-08-19 by escalating our egress from slow (17s) to
# HTTP 429 "Rate exceeded." to no response at all, and because every failure path below degraded
# to [], every arxiv query returned an EMPTY RESULT instead of an alarm.
# So: being deadline-dropped in a wide sweep costs the queued callers; being rate-limited costs
# 100% of arxiv for everyone, for as long as the penalty lasts. Do NOT lower this again.
#
# THE NUMBERS NOW LIVE IN THE DECLARATION (upstreams.json "arxiv", checked 2026-09-28 against
# info.arxiv.org/help/api/tou.html): "make no more than one request every three seconds, and limit
# requests to a single connection at a time". Until 2026-09-28 this module allowed 4 requests in
# flight, which the 3 s start spacing does not cover: a request slower than 3 s overlaps the next.
# The guard is the registry's ONE arXiv guard, so arxiv search, fetch_url, the health probe and
# enrich's integrity lookup (through the shared http client's host gate) all queue on it.
_guard = upstreams.guard("arxiv", log=logger)
_ARXIV_MAX_INFLIGHT = _guard.max_inflight        # 1: the declared single connection
_ARXIV_MIN_INTERVAL_S = _guard.min_interval_s    # 3.0 s between request starts, all callers + threads
# ONE CONNECTION, not just one request in flight (design decision, 2026-09-29): every arXiv request says
# "Connection: close". The shared http clients also route export.arxiv.org to a no-keep-alive HTTP/1.1
# transport (http._one_connection_transport, because the declared terms say max_concurrency 1), so no
# idle connection is left open after a response in either the sync or the async pool.
_CONNECTION_CLOSE = {"Connection": "close"}
# HOW LONG A CALLER MAY WAIT (EYE-GATE-QUEUE, 2026-11-11): the gate is a line; the permit wait and the
# 3 s start wait together run until the caller's own deadline. The module keeps no wait constant of its
# own (it had 20 s for the permit plus 12 s for the start, review F2; then the declared max_wait_s of
# 12 s, which fit four requests and turned away the rest of an ordinary 8-question fan-out).


class _ArxivBusy(upstreams.UpstreamBusy):
    """Internal signal: the caller's deadline came before the gate could start the request, so degrade
    to None (the caller returns []). An
    UpstreamBusy, so the refusal is recorded like every other gate's (a search's _meta.held, a drill's
    top-level held) and an empty arxiv answer reads "not sent", not "nothing there"."""


def _refuse(waited: float) -> "_ArxivBusy":
    return _ArxivBusy(_guard_mod.deadline_refusal("arXiv gate", waited))


def _answered_status(seen: list) -> Optional[int]:
    """The status arXiv answered a failed request with, from the failure http noted (None when no
    answer came: a timeout or no connection). The breaker's one rule (_guard.counts_against_upstream)
    then leaves a 404 / 410 / 400 / 422 uncounted: arXiv answering "no such paper" is not arXiv down."""
    for rec in reversed(seen):
        if isinstance(rec, dict) and isinstance(rec.get("status"), int):
            return rec["status"]
    return None


async def _arxiv_aget_text(url: str, **kwargs):
    """Single arXiv egress chokepoint: pass through the shared guard so the throttled arXiv host sees
    bounded concurrency + a bounded rate + a breaker (breaker -> permit -> start slot -> record). Returns
    None on breaker-open or a pathological rate-gate backlog (callers degrade to []); a falsy http
    result feeds the breaker as a failure (an answered 404 / 410 / 400 / 422 is not counted, see
    _answered_status). No wait blocks the loop: ``_guard.ahold`` waits for the permit in the gate's one
    line on a future of the loop, then for the start slot via ``await anyio.sleep``.

    EVERY path that returns None also diag.note's WHY: without these notes the caller cannot tell
    "arXiv has no such paper" from "we never asked", which is exactly how the 2026-08-19 rate-limit
    outage stayed invisible while health kept reporting ok."""
    if _guard.is_open():
        diag.note("arxiv.breaker_open", url=url, body=(
            "arXiv circuit breaker is OPEN (consecutive failures, typically HTTP 429 rate limiting), "
            "so NO request was sent. An empty arxiv result right now means we did not ask."))
        _probe.note_host_down("arXiv circuit breaker open (consecutive failures, typically HTTP 429); not sent")
        return None
    try:
        # Permit FIRST, then the 3 s start slot while holding it: with the declared single connection,
        # pacing before the permit let two queued callers start back to back the moment a slow request
        # finished. The permit is waited for in the gate's line on the loop (no thread; until the
        # caller's deadline; a cancel cannot keep it).
        async with _guard.ahold(_refuse, request_s=kwargs.get("timeout", http.DEFAULT_TIMEOUT)):
            # A penalty-boxed arXiv host manifests as connect failures (measured as http=000), so an in-slot retry would send two requests through one pace slot.
            with diag.tap() as seen:
                r = await http.aget_text(url, retry_transient=False,
                                         headers={**(kwargs.pop("headers", None) or {}),
                                                  **_CONNECTION_CLOSE},
                                         **kwargs)
    except _ArxivBusy:
        diag.note("arxiv.rate_gate_shed", url=url, body=(
            "declared upstream gate, request not sent: the caller's deadline came before its turn at "
            f"the arXiv gate (published limit: one connection, 1 request / "
            f"{_ARXIV_MIN_INTERVAL_S}s). We did not ask."))
        return None
    if not r:
        diag.note("arxiv.egress_failed", url=url, body=(
            "arXiv returned nothing usable: HTTP 429 'Rate exceeded.', another non-2xx, or a timeout. "
            "An empty arxiv result here means the request FAILED, not that arXiv has no match."))
    _guard.record_ok() if r else _guard.record_fail(status=_answered_status(seen))
    return r


# RECENCY (2026-10-11, OmniSeek showcase R12): the API sorts by relevance, which for a plain topic
# query mixes every year (a 2022 paper led "speculative decoding"). Relevance stays the default, the
# same as openalex / core / dblp and the base's server-side-relevance contract. A query that asks for
# a time window is the case relevance cannot serve, so it is rewritten into arXiv's native form: the
# window becomes a submittedDate range ANDed onto the topic, and the sort becomes submittedDate,
# descending. The topic's bare words are ANDed (all:a AND all:b): arXiv joins bare words loosely
# (50k hits for "speculative decoding"), and a date sort over a loose match is the newest papers of
# the whole archive, not of the topic.
_FIELD_PREFIX_RE = re.compile(r"\b(?:ti|au|abs|co|jr|cat|rn|id|all|submittedDate|lastUpdatedDate):",
                              re.IGNORECASE)
_BOOL_RE = re.compile(r"\b(?:AND|OR|ANDNOT)\b")
_NATIVE_RANGE_RE = re.compile(r"\b(submittedDate|lastUpdatedDate):\s*\[", re.IGNORECASE)
_DAYS_TOKEN_RE = re.compile(r"(?<!\S)days:(\d{1,4})(?!\S)", re.IGNORECASE)
_SINCE_TOKEN_RE = re.compile(r"(?<!\S)since:(\d{4})-?(\d{2})-?(\d{2})(?!\S)", re.IGNORECASE)
_EN_WINDOW_RE = re.compile(
    r"(?:\b(?:in|over|from|during)\s+)?(?:\bthe\s+)?\b(?:past|last)\s+(\d{1,3})\s+(day|week|month)s?\b",
    re.IGNORECASE)
_ZH_WINDOW_RE = re.compile(r"(?:最近|近|过去)\s*(\d{1,3})\s*(天|日|周|个月|月)")
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "天": 1, "日": 1, "周": 7, "个月": 30, "月": 30}


def _recency_window(query: str, today=None):
    """``(topic, start_date)`` when ``query`` asks for a time window, else ``(query, None)``. The
    window marker is cut out of the topic; ``start_date`` is a ``date`` in UTC."""
    today = today or datetime.now(timezone.utc).date()
    m = _SINCE_TOKEN_RE.search(query)
    if m:
        try:
            start = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            return query, None
        return (query[:m.start()] + query[m.end():]), start
    for rx in (_DAYS_TOKEN_RE, _EN_WINDOW_RE, _ZH_WINDOW_RE):
        m = rx.search(query)
        if not m:
            continue
        n = int(m.group(1))
        unit = m.group(2).lower() if rx is not _DAYS_TOKEN_RE else "day"
        days = n * _UNIT_DAYS[unit]
        if days <= 0:
            return query, None
        return (query[:m.start()] + query[m.end():]), today - timedelta(days=days)
    return query, None


def _topic_clause(topic: str) -> str:
    """The topic part of a windowed query: a native query (field prefix or boolean) is grouped as
    is; bare words and quoted phrases are each put under ``all:`` and ANDed."""
    topic = " ".join(topic.split())
    if not topic:
        return ""
    if _FIELD_PREFIX_RE.search(topic) or _BOOL_RE.search(topic):
        return f"({topic})"
    terms = re.findall(r'"[^"]+"|\S+', topic)
    return "(" + " AND ".join(f"all:{t}" for t in terms) + ")"


# Any "word:" token glued to a value (since:2026-13-45 that _recency_window could not read, or a
# prefix arXiv may add later) marks query syntax the user wrote: such a query is sent verbatim.
_ANY_PREFIX_RE = re.compile(r"\b[A-Za-z]+:\S")
# A plain sentence's terms: a quoted phrase stays one term, a hyphenated word stays one word.
_TERM_RE = re.compile(r'"[^"]+"|[^\W_]+(?:-[^\W_]+)*')


def _plain_terms(query: str) -> Optional[list]:
    """The content terms of a plain sentence (lowercased, stopwords and one-letter words out,
    de-duplicated, at most ``_relax.MAX_WORDS``), or None for a native query (a field prefix, any
    ``word:value`` token, or a boolean) or one with no content word."""
    if _FIELD_PREFIX_RE.search(query) or _BOOL_RE.search(query) or _ANY_PREFIX_RE.search(query):
        return None
    terms: list = []
    for t in _TERM_RE.findall(query):
        t = t.lower()
        if t.startswith('"'):
            t = '"' + " ".join(t.strip('"').split()) + '"'
            if t == '""':
                continue
        elif len(t) < 2 or t in _relax.STOPWORDS:
            continue
        if t not in terms:
            terms.append(t)
    return terms[:_relax.MAX_WORDS] or None


def _and_query(terms: list) -> str:
    return " AND ".join(f"all:{t}" for t in terms)


def _relaxable_terms(query: str, today=None) -> Optional[list]:
    """The terms of a query that goes through the relaxed AND search: a plain sentence with no
    native date range and no time window. None: the query is sent as ``_search_params`` builds it."""
    if _NATIVE_RANGE_RE.search(query) or _recency_window(query, today)[1] is not None:
        return None
    return _plain_terms(query)


def _search_params(query: str, limit: int, today=None, terms: Optional[list] = None) -> dict:
    """The arXiv API parameters for a search: relevance order by default, newest first for a
    recency query (see the RECENCY note above). A plain sentence goes out as its terms ANDed under
    ``all:``; ``terms`` overrides them (a relaxed word set)."""
    params = {"search_query": query, "max_results": max(1, min(limit, 100)),
              "sortBy": "relevance", "sortOrder": "descending"}
    native = _NATIVE_RANGE_RE.search(query)
    if native:
        params["sortBy"] = ("lastUpdatedDate" if native.group(1).lower() == "lastupdateddate"
                            else "submittedDate")
        return params
    topic, start = _recency_window(query, today)
    if start is None:
        terms = _plain_terms(query) if terms is None else terms
        if terms:
            params["search_query"] = _and_query(terms)
        return params
    end = today or datetime.now(timezone.utc).date()
    window = f"submittedDate:[{start:%Y%m%d}0000 TO {end:%Y%m%d}2359]"
    clause = _topic_clause(topic)
    params["search_query"] = f"{clause} AND {window}" if clause else window
    params["sortBy"] = "submittedDate"
    return params


def _read_feed(xml, url: str = _API) -> Optional[list]:
    """The entries of an Atom answer ([] for a legal empty one), or None when there was no answer
    (the egress already noted why) or it is not an arXiv Atom feed (noted here)."""
    if not xml:
        return None
    feed = feedparser.parse(xml)
    if feed.entries:
        return feed.entries
    if (feed.get("feed") or {}).get("opensearch_totalresults") is None:
        diag.note("arxiv.unparseable", url=url, body=(
            "arXiv answered, but not with an Atom feed carrying opensearch:totalResults: "
            f"{str(xml)[:300]}"))
        return None
    return []


def _total(xml) -> Optional[int]:
    """``opensearch:totalResults`` of an Atom answer, None when absent."""
    if not xml:
        return None
    try:
        return int(feedparser.parse(xml).feed.get("opensearch_totalresults"))
    except (TypeError, ValueError):
        return None


def _count_params(term: str) -> dict:
    # max_results=0 is refused with HTTP 500 (checked 2026-10-18), so one entry rides along.
    return {"search_query": f"all:{term}", "max_results": 1}


def _as_sent(entries: Optional[list], query: str) -> "_relax.Outcome":
    """A native or windowed query's one answer, in the shape ``_finish`` reads."""
    if entries is None:
        return _relax.Outcome([], [], [], "search failed", failed=True)
    return _relax.Outcome(entries, [query], [[query]])


class ArxivAdapter(BaseAPIAdapter):
    name = "arxiv"
    needs_credentials = False
    description = ("arXiv preprints: 3M+ papers across physics, math, CS, biology. Relevance order by "
                   "default; a recency query (\"past 7 days\", \"last 2 weeks\", \"最近 7 天\", days:7, "
                   "since:2026-10-01, or a native submittedDate:[A TO B] range) returns that window "
                   "newest first; omniseek_read on an arxiv.org /pdf/ or /html/ link returns the whole paper "
                   "(from export.arxiv.org)")

    # The server order is kept verbatim (no local re-rank): relevance by default, newest first for a
    # recency query (see _search_params).
    rank_locally = False
    cache_ttl = 3600
    url_host = "arxiv.org"
    # The full-text links are this adapter's own (EYE-RSS-3): when it cannot read one, nothing else
    # may try it through arxiv.org (the generic web read would queue on the 15 s Crawl-delay gate for a
    # copy of what export.arxiv.org just refused). The abstract pages too (EYE-ARXIV-ABS, 2026-11-03): when the
    # API lookup was not sent or failed, an /abs/ link fell to the generic read, which returned the page's
    # chrome as a matched document (title "Computer Science > Software Engineering", the arXivLabs footer as
    # the abstract) and cached it for an hour. Other arxiv.org pages (listings, help) keep the generic read.
    fetch_url_hosts = ("arxiv.org/pdf/", "arxiv.org/html/", "arxiv.org/abs/")
    # A PDF download, then the html of the same id when the PDF fails, each up to
    # _arxiv_fulltext.DOWNLOAD_TIMEOUT_S plus its gate wait.
    fetch_timeout = 90.0

    # Source spec (omniseek.core.filters). Every field translates into arXiv's own search_query grammar
    # (translate_filters), so a filtered query is an ordinary native query to the rest of the adapter.
    filters = {
        "category": {"type": "keyword", "ops": ["eq", "any"], "doc_key": "tags",
                     "desc": "arXiv category such as cs.CL or math.PR (sent as cat:)"},
        "submitted": {"type": "date", "ops": ["eq", "gte", "lte", "range"], "doc_key": "date",
                      "desc": "submission date of the first version (sent as submittedDate:[A TO B]; "
                              "results then come newest first)"},
        "author": {"type": "text", "ops": ["text"], "desc": "author name match (sent as au:)"},
        "title": {"type": "text", "ops": ["text"], "desc": "title words (sent as ti:)"},
    }
    # omniseek_search(as_of=...) pushes "submitted <= as_of" upstream through this field: the first version's submission date.
    as_of_field = "submitted"
    coverage = {"since": "1991-08", "until": None, "gaps": [],
                "note": "every submission since arXiv opened in August 1991"}
    license = ("arXiv metadata (title, authors, abstract) is CC0 1.0; each paper's full text keeps "
               "the license its authors chose (shown on its abs page)")
    samples = [{"query": "speculative decoding", "filters": {"category": "cs.CL"}},
               {"query": "diffusion models", "filters": {"submitted": {"gte": "2024-01-01"}}}]

    def translate_filters(self, query: str, clauses: list) -> str:
        """Rewrite ``query`` plus the typed filters as one arXiv search_query: the topic (plain words
        ANDed under all:, a native query grouped as is) ANDed with cat: / au: / ti: and a
        submittedDate range. A recency phrase in the query becomes the range unless a submitted
        filter is given (the filter wins)."""
        from omniseek.core.filters import Unsupported
        topic, start = _recency_window(query or "")
        parts = []
        clause = _topic_clause(topic)
        if clause:
            parts.append(clause)
        has_submitted = any(c.field == "submitted" for c in clauses)
        if start is not None and not has_submitted:
            end = datetime.now(timezone.utc).date()
            parts.append(f"submittedDate:[{start:%Y%m%d}0000 TO {end:%Y%m%d}2359]")
        for c in clauses:
            if c.field == "category":
                cats = [c.value] if c.op == "eq" else list(c.value)
                expr = " OR ".join(f"cat:{v}" for v in cats)
                parts.append(f"({expr})" if len(cats) > 1 else expr)
            elif c.field == "submitted":
                lo = c.lo or datetime(1991, 8, 1).date()
                hi = c.hi or datetime.now(timezone.utc).date()
                parts.append(f"submittedDate:[{lo:%Y%m%d}0000 TO {hi:%Y%m%d}2359]")
            elif c.field == "author":
                parts.append(f'au:"{c.value}"' if " " in c.value else f"au:{c.value}")
            elif c.field == "title":
                parts.append(f'ti:"{c.value}"' if " " in c.value else f"ti:{c.value}")
            else:
                raise Unsupported(c.field)
        return " AND ".join(parts)

    @staticmethod
    def _finish(outcome: "_relax.Outcome", tag: bool = True) -> list:
        """[] on a failed first search (the egress noted why); a legal empty answer is noted with
        the word sets tried; each entry is tagged with the word set it matched."""
        if outcome.failed:
            return []
        if outcome.stopped:
            diag.note("arxiv.relax_stopped", url=_API,
                      body=f"{outcome.stopped}; word sets tried: {_relax.tried_text(outcome)}")
        if not outcome.rows:
            diag.note("arxiv.no_match", url=_API,
                      body=f"上游回 0 条（试过的词组：{_relax.tried_text(outcome)}）: arXiv answered an "
                           "Atom feed with totalResults 0")
            return []
        if tag:
            for e in outcome.rows:
                e[_MATCHED_KEY] = list(outcome.words)
        return list(outcome.rows)

    def _to_document(self, raw) -> Document:
        return self._entry_to_document(raw)

    # ------------------------------------------------------------- async twins
    async def _araw_fetch(self, query: str, limit: int) -> list:
        """Async replacement of the deleted sync ``_raw_fetch`` (11-04, S9; this is the only one now): BYTE-FAITHFUL mirror — same URL, same params (search_query,
        the max(1,min(limit,100)) clamp and the sort, all built by the one _search_params), the same
        relaxed plan (_relax.arun over the same search and count requests), same reader, same
        _finish. ONLY the shared-http egress is swapped for its async form: _arxiv_get_text -> await
        _arxiv_aget_text (both ride the SAME shared _guard: sema + pace + breaker)."""
        terms = _relaxable_terms(query)
        if terms is None:
            entries = _read_feed(await _arxiv_aget_text(_API, params=_search_params(query, limit)))
            return self._finish(_as_sent(entries, query), tag=False)

        async def search(ts):
            return _read_feed(await _arxiv_aget_text(
                _API, params=_search_params(query, limit, terms=ts)))

        async def counts(ts):
            out = {}
            for t in ts:
                n = _total(await _arxiv_aget_text(_API, params=_count_params(t)))
                if n is None:
                    return None
                out[t] = n
            return out
        return self._finish(await _relax.arun(terms, search, counts,
                                              want=min(_relax.MIN_HITS, limit)))

    # --------------------------------------------------------------- fetch_url
    @staticmethod
    def _url_route(url: str) -> Optional[tuple]:
        """How fetch_url reads ``url``, sending nothing: ("fulltext", kind, id) for a full-text link,
        ("abstract", id) for any other arXiv paper link, None when this adapter does not read it.
        arxiv.org/abs/<id> is the API lookup by id (abstract + metadata); arxiv.org/pdf/<id> and
        arxiv.org/html/<id> are the whole paper (_fetch_fulltext)."""
        host = urlparse(url).hostname or ""
        if "arxiv.org" not in host:
            return None
        full = _arxiv_fulltext.parse(url)
        if full is not None:
            return ("fulltext", *full)
        if (urlparse(url).path or "").lower().startswith(("/pdf/", "/html/")):
            return None   # under a full-text path but not a paper id (a figure, a malformed id)
        path = (urlparse(url).path or "").rstrip("/")
        # An old-style id keeps its archive: /abs/hep-th/9901001 is "hep-th/9901001", not "9901001".
        if path.lower().startswith("/abs/"):
            arxiv_id = path[len("/abs/"):]
        else:
            arxiv_id = path.split("/")[-1].replace(".pdf", "")
        if not arxiv_id:
            return None
        return ("abstract", arxiv_id)

    async def afetch_url(self, url: str) -> Optional[Document]:
        route = self._url_route(url)
        if route is None:
            return None
        if route[0] == "fulltext":
            return await self._fetch_fulltext(url, *route[1:])
        return await self._abstract(route[1])

    async def _abstract(self, arxiv_id: str) -> Optional[Document]:
        """The API's record of one id (title, authors, abstract), or None."""
        xml = await _arxiv_aget_text(_API, params={"id_list": arxiv_id, "max_results": 1})
        if not xml:
            return None
        feed = feedparser.parse(xml)
        if not feed.entries:
            return None
        return self._entry_to_document(feed.entries[0])

    # ------------------------------------------------------------ full text
    # _read_pdf / _read_html download a whole file through the arXiv gate and parse it (a PDF's text, a
    # LaTeXML page): blocking code, so each runs on a worker thread under the legacy limiter.
    async def _fetch_fulltext(self, url: str, kind: str, arxiv_id: str) -> Optional[Document]:
        if kind == "pdf":
            doc, pdf_reason = await anyio.to_thread.run_sync(self._read_pdf, url, arxiv_id,
                                                             limiter=fetcher._LEGACY_LIMITER)
            if doc is not None:
                return doc
            doc, html_reason = await anyio.to_thread.run_sync(self._read_html, url, arxiv_id,
                                                              limiter=fetcher._LEGACY_LIMITER)
            if doc is not None:
                logger.info("arXiv %s: the PDF could not be read (%s); read the html instead", arxiv_id,
                            pdf_reason)
                meta = dict(doc.metadata or {})
                meta.update({"fulltext_from": "html", "pdf_reason": pdf_reason})
                return doc.model_copy(update={"metadata": meta})
            diag.note("arxiv.fulltext", url=url,
                      body=f"no full text for {arxiv_id}: pdf: {pdf_reason}; html: {html_reason}")
            return None
        doc, html_reason = await anyio.to_thread.run_sync(self._read_html, url, arxiv_id,
                                                          limiter=fetcher._LEGACY_LIMITER)
        if doc is not None:
            return doc
        abstract = await self._abstract(arxiv_id)
        if abstract is None:
            diag.note("arxiv.fulltext", url=url,
                      body=f"no full text for {arxiv_id}: html: {html_reason}; the abstract lookup failed too")
            return None
        diag.note("arxiv.fulltext", url=url, body=f"abstract only for {arxiv_id}: {html_reason}")
        meta = dict(abstract.metadata or {})
        meta.update({"fulltext": False, "fulltext_reason": html_reason})
        return abstract.model_copy(update={"metadata": meta})

    def _read_pdf(self, url: str, arxiv_id: str) -> tuple[Optional[Document], str]:
        """``(document, "ok")`` for the PDF of ``arxiv_id`` read from export.arxiv.org, else
        ``(None, reason)``."""
        from omniseek.core.sources.scrape import pdf_source
        missing = pdf_source.reader_missing()
        if missing:
            return None, f"this install cannot read PDFs: {missing}"
        src = _arxiv_fulltext.export_url_for("pdf", arxiv_id)
        with _arxiv_fulltext.temp_dir() as d:
            dest = f"{d}/paper.pdf"
            try:
                size = _arxiv_fulltext.download(src, dest)
            except Exception as exc:  # noqa: BLE001 (every way the download fails is a reason line)
                return None, _arxiv_fulltext.failure_reason(exc)
            with open(dest, "rb") as f:
                head = f.read(5)
            if head != b"%PDF-":
                return None, f"the answer from {src} is not a PDF (it starts {head!r})"
            got = pdf_source.extract_text(dest, label=f"arxiv pdf {arxiv_id}")
        if got is None:
            return None, "the PDF has no extractable text (it did not open, or it has no text layer)"
        text, n_pages, meta_title, truncated, quality = got
        meta = {"fulltext": True, "fulltext_from": "pdf", "export_url": src, "pages": n_pages,
                "extracted_chars": len(text), "truncated": truncated, "pdf_bytes": size}
        if quality:
            meta["text_quality"] = quality
        return Document(
            source="arxiv", source_id=arxiv_id, url=url, title=(meta_title or "").strip() or arxiv_id,
            content=text, metadata=meta,
        ), "ok"

    def _read_html(self, url: str, arxiv_id: str) -> tuple[Optional[Document], str]:
        """``(document, "ok")`` for the LaTeXML page of ``arxiv_id`` read from export.arxiv.org, else
        ``(None, reason)``; the reason says "no HTML version" for a 404 or a page that is not LaTeXML."""
        src = _arxiv_fulltext.export_url_for("html", arxiv_id)
        with _arxiv_fulltext.temp_dir() as d:
            dest = f"{d}/paper.html"
            try:
                size = _arxiv_fulltext.download(src, dest)
            except Exception as exc:  # noqa: BLE001
                if getattr(exc, "status", None) == 404:
                    return None, f"export.arxiv.org has no HTML version of {arxiv_id} (HTTP 404 from {src})"
                return None, f"the HTML read failed: {_arxiv_fulltext.failure_reason(exc)}"
            page = _arxiv_fulltext.read_html_file(dest)
        got = _arxiv_fulltext.html_to_markdown(page)
        if got is None:
            return None, (f"export.arxiv.org has no HTML version of {arxiv_id}: the page is not a LaTeXML "
                          f"render ({_arxiv_fulltext.page_gist(page)!r})")
        title, text = got
        if not text.strip():
            return None, f"the HTML page of {arxiv_id} has no text"
        # A render with no heading under the title is not the paper's text: a source that only includes
        # a PDF renders as one line pointing at it (1412.6980, 2026-10-24: "See pages 1-last of
        # 0_adam_main.pdf"). Reading it as full text claimed fulltext true for that one line.
        if not re.search(r"^#{2,6} ", text, re.M):
            return None, (f"the HTML version of {arxiv_id} holds no paper text (no abstract or section heading; "
                          f"it reads {' '.join(text.split())[:120]!r}); the PDF has it: "
                          f"https://arxiv.org/pdf/{arxiv_id}")
        truncated = len(text) > _arxiv_fulltext.MAX_CHARS
        text = text[:_arxiv_fulltext.MAX_CHARS]
        return Document(
            source="arxiv", source_id=arxiv_id, url=url, title=title or arxiv_id, content=text,
            metadata={"fulltext": True, "fulltext_from": "html", "export_url": src,
                      "extracted_chars": len(text), "truncated": truncated, "html_bytes": size},
        ), "ok"

    async def ahealth_check(self) -> tuple[Optional[bool], str]:
        # Light DIRECT probe with its OWN short timeout.
        #
        # A 429 is NOT health, however alive the host is. Until 2026-08-19 this returned True on
        # 429, reasoning "API alive, merely throttling us -> search falls back to cache / degrades".
        # But a rate-limited arXiv serves NOTHING live: the guard's breaker opens and every search
        # returns []. Calling that ok is precisely what let a 9x-over-rate adapter sit green through
        # a real outage. Same shape as the `blocked` class in the public health sweep: "answered,
        # but refused us" is its OWN state, not healthy and not down.
        #
        # A single probe also cannot see a RATE failure (one request gets through while real traffic
        # is refused), so check the breaker FIRST: it is the only thing here that reflects load. Open,
        # it sends nothing, so nothing is verified: None (until 2026-10-04 this read False), with the
        # reopening time; the searches it holds back return empty meanwhile.
        if _guard.is_open():
            return None, (_probe.breaker_open("arXiv, consecutive failures, typically HTTP 429",
                                              _guard.state["open_until"] - time.time())
                          + "; live searches return empty until it closes")
        try:
            # The probe is a real API request: it takes the same permit AND the same 3 s start slot
            # as a search (it used to take only the in-flight permit, so a health sweep could land
            # inside another request's 3 s). Bounded by the check's deadline: no turn by then reports
            # "not probed" (None, not verified), never down and never healthy.
            async with _guard.ahold(_refuse, request_s=15):
                resp = await http.adirect(  # a one-shot client: its connection closes when it returns
                    "GET", _API,
                    params={"search_query": "all:machine learning", "max_results": 1},
                    headers=_CONNECTION_CLOSE,
                    timeout=15,
                )
        except _ArxivBusy:
            return None, ("degraded: the arXiv gate (one connection, 3 s spacing) was busy, so this "
                          "cycle did not probe; searches are queueing, not failing")
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        upstreams.observe_response("arxiv", resp)
        if resp.status_code == 200:
            return True, "OK"
        if resp.status_code == 429:
            # The host answered, but only that it refuses us now: not verified (None; until
            # 2026-10-04 this read False). The breaker check above is what reports a sustained rate
            # failure as it holds searches back.
            return None, (f"{_probe.rate_limited('export.arxiv.org', upstreams.parse_retry_after(resp.headers))}: "
                          f"live searches return empty while it lasts. Pacing is 1 request / "
                          f"{_ARXIV_MIN_INTERVAL_S}s; if this persists, something is bursting past it")
        return False, f"HTTP {resp.status_code}"

    # ------------------------------------------------------------------ parse
    @staticmethod
    def _dt(parsed) -> Optional[datetime]:
        """feedparser ``*_parsed`` struct_time (UTC) → tz-aware datetime."""
        if not parsed:
            return None
        try:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _entry_to_document(e) -> Document:
        entry_id = e.get("id", "") or ""
        arxiv_id = entry_id.rsplit("/", 1)[-1]
        authors = [a.get("name", "").strip() for a in (e.get("authors") or []) if a.get("name")]
        cats = [t.get("term") for t in (e.get("tags") or []) if t.get("term")]
        primary = (e.get("arxiv_primary_category") or {}).get("term")
        pdf_url = next(
            (l.get("href") for l in (e.get("links") or [])
             if l.get("title") == "pdf" or l.get("type") == "application/pdf"),
            None,
        )
        title = (e.get("title") or "").strip().replace("\n", " ")
        summary = (e.get("summary") or "").strip()
        return Document(
            source="arxiv",
            source_id=arxiv_id,
            url=entry_id,
            title=title or "(untitled)",
            content=summary,  # full abstract — no truncation
            author=", ".join(authors[:5]) + (" et al." if len(authors) > 5 else "") or None,
            date=ArxivAdapter._dt(e.get("published_parsed")),
            tags=cats,
            metadata={
                "pdf_url": pdf_url,
                "doi": e.get("arxiv_doi"),
                "journal_ref": e.get("arxiv_journal_ref"),
                "primary_category": primary,
                "comment": e.get("arxiv_comment"),
                "updated": ArxivAdapter._dt(e.get("updated_parsed")).isoformat()
                if e.get("updated_parsed") else None,
                "all_authors": authors,
                "matched_words": (list(e.get(_MATCHED_KEY)) if isinstance(e.get(_MATCHED_KEY), list)
                                  else None),
            },
        )
