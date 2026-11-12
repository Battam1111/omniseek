"""Reddit adapter — via the Arctic Shift mirror (Reddit's own .json is WAF-blocked).

2026-05-30: Reddit's public ``.json`` endpoints began returning IP-level WAF
blocks (403) for our server, and self-service OAuth was closed in 2025-11. We
migrated to the **Arctic Shift API** (https://github.com/ArthurHeitmann/arctic_shift),
a community Reddit mirror with near-real-time ingestion (measured lag ~6–46 min,
fine for monitoring). No auth / API key / User-Agent required.

Endpoints:
    GET /api/posts/search?subreddit=<sub>&title=<q>&sort=desc&limit=<=100
        Title keyword search within ONE subreddit (since 2026-10-04; the full-text
        ``query`` parameter is refused, see below). Multi-subreddit
        is NOT supported, so we fan out over the subreddits the query routes to
        (topic groups, see _topic_subreddits) with gentle pacing (bursts trigger a soft
        "slow down" error) and merge by recency.
    GET /api/posts/ids?ids=<id[,id...]>
        Fetch posts by base36 id — used by fetch_url.

Response shape: ``{"data": [<native Reddit submission JSON>]}`` — every field we
need carries its original Reddit name, so the document mapping is unchanged from
the old .json adapter. Throttle/errors come back as ``{"data": null, "error": …}``
with HTTP 200; we retry once.

Behaviour note vs the old adapter: Arctic Shift has no relevance ranking, so we
sort keyword matches by recency (newest first) — which is what a monitor wants.

Query semantics (probed live 2026-06-10): ``query`` is a STRICT AND of every
whitespace token over title+selftext. No OR, no operators — "compass OR approval"
matches the literal token "OR" and returns 0 where "compass" alone returns plenty.
Long ANDs are also server-side EXPENSIVE (a 5-term query took >25s before returning
0). Natural multi-term queries therefore systematically false-negative. Fix is a
mechanical relaxation ladder (`_relax_tiers`): cap the AND at the _MAX_AND_TERMS
longest tokens, and on a 0-hit round retry with the 2 longest, then the single
longest token (length ≈ specificity proxy). Every doc from a non-verbatim round
carries ``metadata.query_sent`` + a ``relaxed`` tag, so the agent SEES the recall
widen and stays the judge of relevance.

Title, not query (measured 2026-10-04 from both machines, so not our egress): the
full-text ``query`` parameter answers HTTP 422 ``{"error": "Timeout. Maybe slow down
a bit"}`` in about 1.2 s every time (r/PhD ``query=advisor``), while ``title=advisor``
answers 200 with 25 items in 8 to 9 s on r/PhD, r/MachineLearning and r/GradSchool.
Every keyword call therefore sends the same terms (same tiers, same relaxation) as
``title``. Recall narrows to post titles (selftext is no longer searched), and the
docs say title search is not supported for very active subreddits (r/cscareerquestions
timed out with 422 after about 6 s). Since 2026-10-05 a sub refused that way while its siblings in
the same round answered goes on a 24 h cannot-search list and is searched through the browser
instead (see _settle_round); the CDP fallback still covers an all-empty fan-out. A Latin query that
routes to no subreddit at all gets one reddit-wide search, marked ``sitewide``; since 2026-11-09 so
does one whose routed subreddits all refused or matched nothing (those hits also carry
``metadata.sitewide_reason`` and ``routed_subreddits``). Since 2026-11-10 the reddit-wide search is
``site:reddit.com <query>`` through OmniSeek's shared web-search backend, the posts it names read from
Arctic's ``/posts/ids`` (reddit 403s its own search.json in the browser); see _SITEWIDE_SITE. A
search that was blocked or failed is noted as such (``reddit.cdp_search_blocked`` / ``_error``,
``reddit.sitewide_web_search`` with when the backend opens again, ``reddit.sitewide_arctic_failed``)
and its empty answer is not cached; only a search that was answered can say "nothing found". A hit counts only when it carries one of the
query's content terms that name no routed subreddit, if the query has any (_on_topic): a word naming a
sub ("teacher" -> r/Teachers) matches that sub's every post, which says nothing about the topic.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from datetime import datetime, timezone
from typing import NamedTuple, Optional
from urllib.parse import quote, urlparse

import anyio
from tenacity import AsyncRetrying, RetryCallState, retry_if_result, stop_after_attempt

from omniseek.core import _probe, cache, diag, fetcher, http, portal, upstreams
from omniseek.core._guard import GateBusy, answered, bounded_async_slot
from omniseek.core.fetcher import register_adapter
from omniseek.core.normalize import Document, mk_signal
from omniseek.core.sources._base import SourceBase

logger = logging.getLogger(__name__)

API = "https://arctic-shift.photon-reddit.com/api"
_SEARCH_TTL = 900       # 15 min
# The submission-search fan-out's per-request timeout. LOWER than the 20s default on purpose: a healthy
# Arctic answers a (term-capped) search in ~1-2s, so a 20s ceiling only ever helps a THROTTLED / hung
# mirror waste the caller's whole deadline. Observed 2026-07-17: the host's IP is rate-limited by Arctic,
# so a lone reddit drill blew the 90s eye deadline — the 23-sub first wave burned 3x20s of timeouts before
# the breaker tripped and the reddit-own CDP fallback could run. 10s bounds that first wave (~3x10s+backoff
# ~= 33s) so a throttled Arctic fails FAST to the working CDP search WITHIN the deadline; a legit slow
# 4-term query relaxes to faster fewer-term tiers (Arctic long-ANDs are server-side expensive) or is caught
# by the CDP fallback, so recall is not lost. ONLY the submission search path uses it; the ids / health /
# comment paths keep the 20s default (they are not the deadline-eating fan-out).
# Raised to 18s on 2026-10-04: the search now sends `title`, and a healthy title search measured 8 to 9 s
# (r/PhD, r/MachineLearning, r/GradSchool, limit=25), so 10s cut real answers off. 18s is about 2x the
# measured 9 s. The fast failure the 10s bound bought now comes mostly from the refusal path: a throttled
# Arctic answers its "slow down" 422 in about 1.5 s, that is not retried, and five refused subs trip the
# breaker. A hung mirror (no answer at all) still costs one sub at most 3x18s plus backoff, about 58s.
_SEARCH_TIMEOUT = 18
# Concurrency over the per-sub fan-out. Arctic Shift can only search ONE sub per
# request, so the routed subs are pushed in waves of this width. KEPT AT 5: a measured
# bump to 10 made reddit SLOWER (34s vs 25s) — higher concurrency against the single
# Arctic host raises its soft-throttle (HTTP200+data:null) rate, pushing more subs
# through the full retries=2 jittered backoff. reddit's latency is HOST-bound, not
# fan-out-bound; its breadth-safe fix is background pre-warm (keep the cache hot so a
# broad search never pays the live Arctic cost), NOT more concurrency. backoff +
# retries + relaxation ladder + the full 23-sub set + per-sub limit are all unchanged.
_FANOUT_WORKERS = 5

# Highest-value subs: PhD methodology + ML/AI + 海外长期落地 (SG/Canada). Since 2026-10-05 search
# routing does not read this list (it uses the topic groups below); it stays for the other users of a
# fixed curated set, and the subs added for routing are not appended here.
DEFAULT_SUBREDDITS = [
    # PhD methodology (English-world)
    "PhD",
    "AskAcademia",
    "GradSchool",
    # ML / AI technical
    "MachineLearning",
    "compsci",
    "ArtificialIntelligence",
    "labrats",
    # 海外长期落地 (移民/落地子版)
    "IWantOut",
    "CanadaImmigration",
    "ImmigrationCanada",  # the major (~300k) Canada-immigration sub; CanadaImmigration is the small twin
    "cscareerquestionsCAD",
    "AskCanada",
    "singapore",
    "SingaporePR",
    # 内幕 / 职场 / 签证真话 (insider-layer frontier, 2026-06-03)
    "askSingapore",      # SG 生活/工作/EP 真实问答 (比 r/singapore 更实)
    "singaporefi",       # SG 薪资/总包/职业财务真话
    "cscareerquestions", # 全球 CS 职场 (面试/offer/谈判 主版)
    "cscareerquestionsEU",
    "ExperiencedDevs",   # 资深工程师职场 (高信号)
    "csMajors",          # 应届/实习/offer
    "ExpressEntry",      # 加拿大 Express Entry 专版
]


# `subreddit:NAME` / `sub:NAME` qualifier — comma-separated names allowed
# (e.g. `subreddit:LocalLLaMA,MachineLearning rlhf`). Matched case-insensitively,
# stripped from the keyword part of the query before full-text search.
_SUB_QUALIFIER_RE = re.compile(r"(?:^|\s)(?:subreddit|sub)\s*:\s*([A-Za-z0-9_,]+)", re.IGNORECASE)

# ── Comment path (the answers live in comments, not titles) ──────────────────────────────────
# A bare `comments:`/`comment:` token (or `comments` / `comment` as the LAST token) flips reddit
# from the submission path to the COMMENT path: the substantive Reddit answer is almost always in
# the comment tree, not the thread title/selftext. The token is stripped from the keyword part so
# the rest is a normal full-text query. Absent → submission path is byte-identical to before (no
# regression to submissions or the auto-route). The comment path resolves its subreddits with
# _parse_subreddits + the same _routed_subreddits rule as submissions, so `comments: subreddit:LocalLLaMA
# rlhf` works and a bare `comments: pour over coffee` still discovers r/Coffee.
_COMMENT_QUALIFIER_RE = re.compile(r"(?:^|\s)comments?\s*:", re.IGNORECASE)
# Arctic's comments endpoint takes full-text via `body` (NOT `query`, which it 400s) and supports a
# `link_id=t3_<id>` thread filter; `sort` accepts ONLY asc/desc (recency) — there is no server-side
# score sort, so the comment path ranks by `score` CLIENT-SIDE to surface the high-signal answers.
_COMMENT_TTL = 900           # 15 min, same as submissions
_COMMENT_THREADS = 3         # top submission threads (by num_comments) to pull comment trees from
_COMMENT_PER_THREAD = 60     # comments fetched per thread before client-side score-ranking
_COMMENT_PER_SUB = 40        # comments per sub for the direct full-text (body=) comment search
# Cap the comment-path fan-out width. reddit's latency is HOST-bound (one Arctic host); the
# submission path already pays a fan-out of up to the request cap, and the comment path would otherwise DOUBLE that
# host pressure (a direct comment search PLUS a thread-harvest search). A query rarely needs more
# than its few most-relevant communities for COMMENT depth (vs the submission path's monitor breadth),
# so cap to the top-N subs of the shared route (explicit subs, else the topic groups in priority order). Keeps the
# comment path a good neighbor to the submission cache-warm instead of triggering the soft-throttle.
_COMMENT_MAX_SUBS = 6
# Reddit's sentinel non-content comment bodies — dropped before ranking (they carry no answer).
_DEAD_BODIES = frozenset({"", "[removed]", "[deleted]", "[ Removed by Reddit ]"})

# Arctic Shift ANDs every token; past this many terms the co-occurrence requirement
# (and the server-side cost) makes 0-hit the norm, so cap the verbatim tier.
_MAX_AND_TERMS = 4

# ── Same-name subreddit discovery (the "general engine" route) ──────────────────────────────
# For a query no topic group matched ("pour over coffee"), look up subs named after the query's own
# words via Arctic's subreddit_prefix endpoint, keep public non-NSFW subs above a subscriber floor
# whose name equals a content word or two adjacent ones run together (plus or minus a final s),
# rank by size and take the top few. Prefix matches alone brought in noise: "rent" found a German
# r/rentnerzeigenaufdinge and "draw" sent "express entry draw" to r/drawing (2026-10-05).
_DISCOVER_TOP = 4            # max topical subs appended per query
_DISCOVER_MIN_SUBS = 5000    # skip tiny/dead/squatted subs
_DISCOVER_TTL = 86400        # sub metadata is stable day-to-day → cache discovery a full day

# A live Arctic posts probe on 2026-08-22 took 7.89s; the title search measured on 2026-10-04
# took 8 to 9 s (limit=25, three subs), so the measured call is now 9.0s. With the 2s pace the
# request budget treats 11.0s as one conservative paced slot: (90 - 15) // 11 = 6 slots x 5
# workers = 30 requests (was 7 x 5 = 35 at 7.9s). Reserve time for discovery, cache IO, response
# mapping, and the Reddit CDP fallback. The resulting cap is a hard upper bound on
# `subreddits x relaxation tiers` for one search.
_ARCTIC_SEARCH_DEADLINE_S = 90.0
_ARCTIC_MEASURED_CALL_S = 9.0
_ARCTIC_PACE_S = 2.0
_ARCTIC_SEARCH_RESERVE_S = 15.0
_ARCTIC_MAX_REQUESTS = (
    max(1, int((_ARCTIC_SEARCH_DEADLINE_S - _ARCTIC_SEARCH_RESERVE_S)
               // (_ARCTIC_MEASURED_CALL_S + _ARCTIC_PACE_S)))
    * _FANOUT_WORKERS
)

# ── Topic-group routing (2026-10-05) ─────────────────────────────────────────────────────────
# Each group has its trigger words and its subreddits. A query searches only the groups it hits,
# in the fixed priority below, and the request cap then trims from the end. Before this the
# query searched the whole 23-sub DEFAULT_SUBREDDITS list (research first) whenever any core word
# appeared, and the cap cut the career, Singapore and Canada subs it actually needed.
# Triggers are matched case-insensitively on whole words; a two-word trigger matches two adjacent
# query words. Display names of the subs added on 2026-10-05 (HongKong, canada,
# PersonalFinanceCanada and the seven cities) were checked against Arctic's subreddit lookup that
# day: each is public, not adult, and above the discovery subscriber floor.
def _words(text: str) -> frozenset:
    return frozenset(" ".join(line.split()) for line in text.strip().splitlines() if line.strip())


_CITY_SUBS = [  # (trigger, display name); a city also counts as a hit on the Canada group
    ("toronto", "toronto"),
    ("vancouver", "vancouver"),
    ("montreal", "montreal"),
    ("edmonton", "Edmonton"),
    ("ottawa", "ottawa"),
    ("calgary", "Calgary"),
    ("waterloo", "waterloo"),
]
_SINGAPORE_SUBS = ["askSingapore", "singapore", "singaporefi"]
_SINGAPORE_TERMS = _words("""
singapore
singaporean
singaporeans
sg
sentosa
hdb
cpf
changi
nus
employment pass
""")
_HONGKONG_SUBS = ["HongKong"]
_HONGKONG_TERMS = _words("""
hong kong
hongkong
hk
kowloon
mtr
polyu
hku
cuhk
hkust
""")
_CANADA_SUBS = ["AskCanada", "canada", "PersonalFinanceCanada"]
_CANADA_TERMS = _words("""
canada
canadian
canadians
ontario
quebec
alberta
british columbia
manitoba
saskatchewan
""")
# (place name, subs, triggers) in priority order; the city group is handled before these.
_PLACE_GROUPS = [
    ("singapore", _SINGAPORE_SUBS, _SINGAPORE_TERMS),
    ("hongkong", _HONGKONG_SUBS, _HONGKONG_TERMS),
    ("canada", _CANADA_SUBS, _CANADA_TERMS),
]
_IMMIGRATION_SUBS = ["IWantOut", "ImmigrationCanada", "CanadaImmigration", "ExpressEntry", "SingaporePR"]
_IMMIGRATION_TERMS = _words("""
visa
visas
immigration
immigrant
immigrants
expressentry
express entry
workpermit
work permit
study permit
pgwp
cec
lmia
pr
singaporepr
permanent resident
permanent residency
citizenship
employment pass
aaip
oinp
crs
""")
_CAREER_SUBS = ["cscareerquestions", "cscareerquestionsEU", "ExperiencedDevs", "csMajors",
                "cscareerquestionsCAD"]
_CAREER_TERMS = _words("""
career
careers
job
jobs
employment
interview
interviews
offer
offers
salary
salaries
hiring
workplace
software
developer
programming
resume
internship
intern
layoff
layoffs
negotiation
""")
_ML_SUBS = ["MachineLearning", "compsci", "ArtificialIntelligence"]
_ML_TERMS = _words("""
ai
ml
machinelearning
machine learning
artificialintelligence
artificial intelligence
compsci
computer science
deep learning
llm
llms
nlp
""")
_ACADEMIC_SUBS = ["PhD", "AskAcademia", "GradSchool", "labrats"]
_ACADEMIC_TERMS = _words("""
phd
academia
academic
advisor
admission
admissions
graduate
gradschool
grad school
university
college
professor
faculty
postdoc
thesis
dissertation
lab
laboratory
research
researcher
supervisor
""")
# Subs in the immigration and career groups that belong to one place. When the query names any
# place, those two groups keep only the subs of the named places (first) and the subs with no
# place. cscareerquestionsEU belongs to Europe, which has no group, so a named place drops it.
_SUB_PLACE = {
    "immigrationcanada": "canada",
    "canadaimmigration": "canada",
    "expressentry": "canada",
    "cscareerquestionscad": "canada",
    "singaporepr": "singapore",
    "cscareerquestionseu": "europe",
}
# Immigration terms that only exist in one country's system. They name that place for the filter
# above without adding its local-life group: "express entry draw" wants r/ExpressEntry ahead of
# r/IWantOut and no r/SingaporePR, but not r/canada.
_PLACE_HINT_TERMS = {
    "expressentry": "canada",
    "express entry": "canada",
    "pgwp": "canada",
    "cec": "canada",
    "lmia": "canada",
    "crs": "canada",
    "aaip": "canada",
    "oinp": "canada",
    "singaporepr": "singapore",
}
_MAX_TRIGGER_WORDS = 2

# Measured storage route: exact Arctic lookups confirmed these communities exist, and a
# DataHoarder `helium drives` post search returned enterprise and used-drive evidence. This
# local route is necessary because prefix discovery cannot find DataHoarder from `helium`.
_STORAGE_SUBS = ["DataHoarder", "homelab", "sysadmin"]
_STORAGE_INTENT_TERMS = frozenset("""
helium drive drives hdd hdds harddrive harddrives storage nas sas sata smart badblocks
cmr smr burnin
""".split())
_STORAGE_STRONG_TERMS = frozenset({
    "hdd", "hdds", "harddrive", "harddrives", "badblocks", "cmr", "smr",
})

# (A `fields=` payload trim was tried 2026-06-14 to cut host load, but Arctic Shift REJECTS our
# field list — every request -> HTTP error -> retry-walk -> 90s timeout -> 0 docs. Isolated live:
# any fields= with our keys 400s while no-fields works; the README example only covers id/title.
# Not worth bisecting the valid set — a likely-rejected key is `preview`, which we need for media,
# so trimming it would cost image breadth anyway. Left OUT: reddit keeps the full payload.)


# Stopwords (English glue + query connectives): they are generic, so they must NEVER win an AND
# slot — the 2026-06-21 defect was a length-ranked cap keeping "without"/"foreign" while dropping
# the SHORT acronyms (PGWP/CEC/LMIA/PR) that carry the query's meaning. Lowercased compare.
_STOPWORDS = frozenset("""
a an the to of in on at for and or but with without within into onto from by as is are was were be been
being do does did how what when where why who whom which that this these those i you he she it we they
my your his her its our their me him us them not no yes can could should would may might will shall must
about over under via per vs than then so if else any all some each more most less few many much get got
getting need needs want wants like out up down off no
""".split())


def _content_terms(q: str) -> list[str]:
    """Query tokens with stopwords removed (case-insensitive); original order + case preserved."""
    return [t for t in (q or "").split() if t.lower() not in _STOPWORDS]


def _latin_content_terms(q: str) -> list[str]:
    """Return distinct Latin content tokens that Arctic can search without translation."""
    out: list[str] = []
    seen: set[str] = set()
    for token in _content_terms(q):
        if not re.fullmatch(r"[A-Za-z0-9]+", token):
            continue
        key = token.lower()
        if key not in seen:
            seen.add(key)
            out.append(token)
    return out


def _query_grams(q: str) -> set[str]:
    """Lowercased query words plus every run of up to _MAX_TRIGGER_WORDS adjacent words."""
    toks = re.findall(r"[a-z0-9]+", (q or "").lower())
    grams: set[str] = set()
    for n in range(1, _MAX_TRIGGER_WORDS + 1):
        for i in range(len(toks) - n + 1):
            grams.add(" ".join(toks[i:i + n]))
    return grams


def _keep_place_subs(subs: list[str], places: set[str]) -> list[str]:
    """With no named place, the group as listed. With named places, the subs of those places first,
    then the subs that belong to no place; subs of any other place are dropped."""
    if not places:
        return list(subs)
    own = [s for s in subs if _SUB_PLACE.get(s.lower()) in places]
    neutral = [s for s in subs if s.lower() not in _SUB_PLACE]
    return own + neutral


def _topic_subreddits(q: str) -> list[str]:
    """The topic-group route: the subs of every group ``q`` hits, in the fixed priority order
    (cities, places, immigration, career, finance, machine learning, academic), de-duplicated
    case-insensitively with the first occurrence kept. [] when no group and no finance signal."""
    grams = _query_grams(q)
    out = [sub for city, sub in _CITY_SUBS if city in grams]
    city_hit = bool(out)
    places: set[str] = set()
    for place, subs, terms in _PLACE_GROUPS:
        if grams & terms or (place == "canada" and city_hit):
            places.add(place)
            out.extend(subs)
    places.update(place for term, place in _PLACE_HINT_TERMS.items() if term in grams)
    for subs, terms in ((_IMMIGRATION_SUBS, _IMMIGRATION_TERMS), (_CAREER_SUBS, _CAREER_TERMS)):
        if grams & terms:
            out.extend(_keep_place_subs(subs, places))
    out = _with_finance_subs(out, q)
    for subs, terms in ((_ML_SUBS, _ML_TERMS), (_ACADEMIC_SUBS, _ACADEMIC_TERMS)):
        if grams & terms:
            out.extend(subs)
    seen: set[str] = set()
    deduped: list[str] = []
    for sub in out:
        if sub.lower() not in seen:
            seen.add(sub.lower())
            deduped.append(sub)
    return deduped


def _looks_storage_intent(q: str) -> bool:
    """Whether the query carries the measured enterprise-drive/storage topic."""
    terms = {t.lower() for t in _latin_content_terms(q)}
    hits = terms & _STORAGE_INTENT_TERMS
    return bool(hits & _STORAGE_STRONG_TERMS) or len(hits) >= 3


def _storage_search_terms(q: str) -> list[str]:
    """Keep the first two storage terms for a focused Arctic AND tier."""
    out = []
    seen = set()
    for token in _latin_content_terms(q):
        key = token.lower()
        if key in _STORAGE_INTENT_TERMS and key not in seen:
            seen.add(key)
            out.append(token)
    return out[:2]


def _search_request_cap(q: str) -> int:
    """Hard total for Arctic `/posts/search` calls in one search invocation."""
    return _ARCTIC_MAX_REQUESTS


def _discovery_anchor_terms(q: str, subreddits: list[str]) -> list[str]:
    """Terms that directly name a discovered subreddit, ordered by resolved subreddit priority."""
    terms = _latin_content_terms(q)
    out: list[str] = []
    seen: set[str] = set()
    for subreddit in subreddits:
        name = subreddit.lower()
        for term in terms:
            key = term.lower()
            if key in seen or term.isupper():
                continue
            if name.startswith(key):
                seen.add(key)
                out.append(term)
    return out


def _topic_terms(q: str, subreddits: list[str]) -> list[str]:
    """The query's content terms that name none of ``subreddits`` (lowercased), by the rule
    ``_discovery_anchor_terms`` uses to call a term a sub's name. [] when every term names one
    ("pour over coffee" -> r/pourover, r/Coffee)."""
    names = [s.lower() for s in subreddits]
    out: list[str] = []
    for term in _latin_content_terms(q):
        key = term.lower()
        if term.isupper() or not any(n.startswith(key) for n in names):
            out.append(key)
    return out


def _on_topic(items: list[dict], terms: list[str]) -> list[dict]:
    """The submissions whose title or selftext carries at least one of ``terms`` (at a word start, any
    case); all of them when ``terms`` is empty. A tier made only of a word naming a routed sub
    ("teacher" in r/Teachers) matches that sub's every recent post, so without this the 11-09 query
    "teacher student distillation sim2real" came back as five r/Teachers posts and never reached the
    reddit-wide search."""
    if not terms or not items:
        return items
    pats = [re.compile(r"(?<![a-z0-9])" + re.escape(t)) for t in terms]
    kept = [it for it in items
            if any(p.search(f"{it.get('title') or ''}\n{it.get('selftext') or ''}".lower()) for p in pats)]
    if len(kept) < len(items):
        diag.note("reddit.off_topic",
                  body=f"dropped {len(items) - len(kept)} of {len(items)} hits carrying none of {terms}")
    return kept


def _search_tiers(q: str, subreddits: list[str]) -> list[str]:
    """Try discovered topical anchors before broad single-token relaxation tiers."""
    base = _relax_tiers(q) if q else [""]
    if not q:
        return base
    storage_terms = _storage_search_terms(q) if _looks_storage_intent(q) else []
    if storage_terms:
        storage_tier = " ".join(storage_terms)
        out = [base[0], storage_tier]
        out.extend(tier for tier in base[1:] if tier not in out)
        return out
    anchors = _discovery_anchor_terms(q, subreddits)
    if not anchors:
        return base
    out = [base[0]]
    out.extend(anchor for anchor in anchors if anchor not in out)
    out.extend(tier for tier in base[1:] if tier not in out)
    return out


def _cap_search_subreddits(subreddits: list[str], q: str) -> list[str]:
    """Keep `len(subreddits) x len(relaxation_tiers)` within the measured hard budget."""
    tiers = _search_tiers(q, subreddits)
    max_subs = max(1, _search_request_cap(q) // max(len(tiers), 1))
    return list(subreddits[:max_subs])


def _note_non_latin_query(q: str) -> None:
    diag.note(
        "reddit.non_latin_query",
        body="reddit covers almost no Chinese text; query has no Latin content terms",
    )


def _term_rank(t: str) -> tuple:
    """Specificity sort key (sorted DESC = most specific first). An ALL-CAPS acronym (PGWP, CEC,
    LMIA, PR, EE) or a token with a digit is HIGH specificity EVEN WHEN SHORT; a Capitalized proper
    noun next; generic lowercase words rank only by length. This replaces the length-only proxy that
    silently dropped the acronyms carrying an immigration/tech query's actual meaning."""
    is_acronym = (t.isupper() and 2 <= len(t) <= 6) or any(c.isdigit() for c in t)
    is_capitalized = t[:1].isupper() and not t.isupper()
    return (is_acronym, is_capitalized, len(t))


def _relax_tiers(q: str) -> list[str]:
    """Mechanical relaxation ladder for Arctic Shift's strict-AND full-text query.

    Tier 0: the query's CONTENT terms (stopwords stripped), capped to the _MAX_AND_TERMS most
    SPECIFIC tokens (original order kept). Then, tried only while the previous tier matched NOTHING:
    the 2 most-specific tokens, then the single most-specific. Specificity = acronym/has-digit >
    Capitalized > longer (was length-only, which dropped short acronyms like PGWP/CEC/LMIA — the
    bug). Deciding which hits matter stays with the caller — every non-verbatim doc is labeled.
    """
    toks = _content_terms(q)
    if not toks:                       # an all-stopword query: fall back to the raw tokens
        toks = (q or "").split()
    if len(toks) <= 1:
        return [" ".join(toks)] if toks else [q]
    ranked = sorted(toks, key=_term_rank, reverse=True)   # most-specific first
    keep = set(ranked[:_MAX_AND_TERMS])
    tiers = [" ".join(t for t in toks if t in keep)]      # the specific subset, in ORIGINAL order
    if len(toks) > 2:
        tiers.append(" ".join(ranked[:2]))                # the 2 most specific
    tiers.append(ranked[0])                               # the single most specific
    out: list[str] = []
    for t in tiers:
        if t and (not out or t != out[-1]):
            out.append(t)
    return out or [q]

_IMAGE_EXT_RE = re.compile(r"\.(?:png|jpe?g|gif|webp)(?:\?|$)", re.IGNORECASE)


def _parse_subreddits(query: str) -> tuple[str, Optional[list[str]]]:
    """Split a `subreddit:`/`sub:` qualifier out of the query.

    Returns ``(clean_query, subs)`` where ``subs`` is the ordered, de-duplicated
    list of requested subreddits (overriding the topic route) or ``None``
    when no qualifier is present — in which case the caller's default behaviour
    is completely unchanged.
    """
    subs: list[str] = []
    seen: set[str] = set()
    for m in _SUB_QUALIFIER_RE.finditer(query or ""):
        for name in m.group(1).split(","):
            name = name.strip()
            key = name.lower()
            if name and key not in seen:
                seen.add(key)
                subs.append(name)
    if not subs:
        return (query or "").strip(), None
    clean = _SUB_QUALIFIER_RE.sub(" ", query).strip()
    return clean, subs


def _parse_comment_mode(query: str) -> tuple[str, bool]:
    """Split a `comments:`/`comment:` qualifier out of the query.

    Returns ``(clean_query, want_comments)``. ``want_comments`` is True when the
    query carries a `comments:`/`comment:` token, in which case it is stripped from
    the keyword part (the rest is a normal full-text query). Absent → False and the
    query is returned untouched, so the submission path is completely unchanged.
    """
    q = query or ""
    if not _COMMENT_QUALIFIER_RE.search(q):
        return q.strip(), False
    return _COMMENT_QUALIFIER_RE.sub(" ", q).strip(), True


def _name_forms(word: str) -> set[str]:
    forms = {word, word + "s"}
    if len(word) > 1 and word.endswith("s"):
        forms.add(word[:-1])
    return forms


def _discovery_names(query: str) -> set[str]:
    """Lowercased subreddit names discovery may take for ``query``: each content word, the word plus
    or minus a final s, and each pair of adjacent content words run together, plus or minus s."""
    terms = [t.lower() for t in _content_terms(query) if re.fullmatch(r"[A-Za-z0-9]+", t)]
    names: set[str] = set()
    for t in terms:
        names |= _name_forms(t)
    for a, b in zip(terms, terms[1:]):
        names |= _name_forms(a + b)
    return names


def _discover_subreddits(query: str) -> list[str]:
    """Subreddits named after ``query``'s own words, for a query no topic group matched
    (best effort: [] on any failure).

    Probes every distinct Latin content term of 3+ characters via Arctic's ``subreddit_prefix``
    endpoint, keeps public, non-NSFW subs above the subscriber floor, then keeps only the subs
    whose name equals a content word or an adjacent pair of them (``_discovery_names``), ranks by
    subscriber count, and returns the top few. Per-term lookups are cached (_DISCOVER_TTL) so a
    repeated topic costs no live discovery GETs. Non-Latin queries (e.g. "手冲咖啡") yield no
    tokens and so []."""
    # Probe every distinct Latin CONTENT term ranked by SPECIFICITY (acronyms/proper nouns first),
    # NOT raw length. Prefix is the only search primitive Arctic exposes, so probing each content
    # term is the broadest translation-free lookup available. The prefix answer is wide (r/drawing
    # for "draw", a German r/rentnerzeigenaufdinge for "rent"), so only same-name subs are kept.
    toks = [t for t in _content_terms(query) if re.fullmatch(r"[A-Za-z0-9]+", t) and len(t) >= 3]
    if not toks:
        return []
    probes = sorted(set(toks), key=_term_rank, reverse=True)
    allowed = _discovery_names(query)
    found: dict[str, tuple[str, int]] = {}  # lower-name -> (display_name, subscribers)
    for tok in probes:
        ck = cache.make_key("reddit_arctic", "discover", tok.lower())
        subs = cache.get_once(ck)
        if subs is None and cache.cache_only():
            continue  # cache-only: no probe, and no empty list written under this term
        if subs is None:
            raw = _arctic_get("/subreddits/search", {"subreddit_prefix": tok, "limit": 12})
            subs = []
            for s in raw or []:
                if not isinstance(s, dict):
                    continue
                name = s.get("display_name")
                if (name and s.get("subreddit_type") == "public" and not s.get("over18")
                        and (s.get("subscribers") or 0) >= _DISCOVER_MIN_SUBS):
                    subs.append([name, int(s.get("subscribers") or 0)])
            cache.set(ck, subs, ttl=_DISCOVER_TTL)
        for name, nsubs in subs:
            key = name.lower()
            if key not in allowed:
                continue
            prev = found.get(key)
            if prev is None or nsubs > prev[1]:
                found[key] = (name, nsubs)
    ranked = sorted(found.values(), key=lambda x: x[1], reverse=True)
    return [name for name, _ in ranked[:_DISCOVER_TOP]]


def _routed_subreddits(q: str) -> list[str]:
    """The one routing rule shared by the submission and comment paths, before any width cap:
    topic groups (finance included), else the measured storage subs, else same-name discovery."""
    topical = _topic_subreddits(q)
    if topical:
        return topical
    if _looks_storage_intent(q):
        return list(_STORAGE_SUBS)
    return _discover_subreddits(q)


def _resolve_subreddits(query: str, override_subs: Optional[list[str]]) -> list[str]:
    """Resolve and budget the subreddit set shared by sync and async search."""
    q = (query or "").strip()
    if q and not _latin_content_terms(q):
        _note_non_latin_query(q)
        return []
    if override_subs:
        return _cap_search_subreddits(override_subs, q)

    routed = _routed_subreddits(q)
    if not routed:
        if cache.cache_only():
            return []  # the reddit-wide branch notes the cache-only skip itself
        diag.note(
            "reddit.discovery_empty",
            body=("no topic group, finance signal or same-name subreddit matched this Latin query, "
                  "so reddit searches all of reddit through the shared web search instead (site:reddit.com)"
                  if q else "an empty query matched no subreddit"),
        )
        return []
    return _cap_search_subreddits(routed, q)


def _sitewide_eligible(q: str, override_subs: Optional[list[str]]) -> bool:
    """A reddit-wide search is for a Latin query with no `subreddit:` qualifier that resolved to no
    subreddit at all."""
    return override_subs is None and bool(_latin_content_terms(q))


# ── Reddit-wide search: the shared web-search backend finds the posts, Arctic reads them ─────────
# Until 2026-11-10 the reddit-wide step loaded www.reddit.com/search.json in the shared browser;
# reddit answered it 403 (live, 11-09 23:53, "sim2real"). Now OmniSeek's shared web-search backend
# (_search_backend.asearch_web: its pacing, breakers and cooldowns, no request path of our own) runs
# `site:reddit.com <query>`, the post ids come out of the result URLs, and Arctic's /posts/ids (the
# lookup that keeps working while its search sheds load) returns the posts themselves.
_SITEWIDE_SITE = "reddit.com"
# /r/<sub>/comments/<id>/<slug>/[<comment id>/] or /comments/<id>: the post id is the base36 segment.
_POST_PATH_RE = re.compile(r"^/(?:r/[^/]+/)?comments/([a-z0-9]{3,12})(?:/|$)", re.IGNORECASE)


# Results asked of the engine: the most one Brave call returns. It bills per query, not per result,
# and subreddit, user and wiki pages in the results carry no post id.
_SITEWIDE_ENGINE_N = 20


def _post_ids_from_urls(urls) -> list[str]:
    """Distinct reddit post ids in engine order (pure). A comment permalink names its post."""
    ids: list[str] = []
    for u in urls:
        try:
            p = urlparse(u or "")
        except ValueError:
            continue
        host = (p.hostname or "").lower()
        if host != _SITEWIDE_SITE and not host.endswith("." + _SITEWIDE_SITE):
            continue
        m = _POST_PATH_RE.match(p.path or "")
        if m and m.group(1).lower() not in ids:
            ids.append(m.group(1).lower())
    return ids


def _sitewide_docs(items: list[dict], limit: int, sent: str) -> list[Document]:
    docs = []
    for it in items[:limit]:
        doc = RedditAdapter._submission_to_document(it)
        doc.metadata["search_via"] = "web_search_arctic_ids"
        doc.metadata["search_scope"] = "sitewide"
        doc.metadata["query_sent"] = sent
        doc.tags.append("sitewide")
        docs.append(doc)
    return docs


def _note_sitewide_empty(sent: str, engine_n: int, ids: list[str]) -> None:
    """The engine answered and nothing came of it: no result was a reddit post, or Arctic holds none of
    the posts it named. An index miss, not a failure."""
    if not ids:
        body = (f"web search for {sent!r} returned {engine_n} results, none of them a reddit post; "
                "the reddit-wide search found nothing for this query either")
    else:
        body = (f"web search for {sent!r} named {len(ids)} reddit posts ({', '.join(ids)}); "
                "Arctic Shift returned none of them (deleted, or not mirrored)")
    diag.note("reddit.sitewide_empty", body=body)


def _web_search_reopens() -> str:
    """When the shared web-search backend serves again, read off its ledger (sends nothing)."""
    from omniseek.core.sources.api._search_backend import backend_state
    state = backend_state()
    if state["active"] != "none":
        return f"the backend serves again now ({state['active']})"
    halves = []
    if state["brave"]["keyed"] and state["brave"]["cooling_until"]:
        halves.append((state["brave"]["cooling_s"], "brave", state["brave"]["cooling_until"]))
    if not state["ddg"]["disabled"] and state["ddg"]["cooling_until"]:
        halves.append((state["ddg"]["cooling_s"], "ddg", state["ddg"]["cooling_until"]))
    if not halves:
        return "no half of the backend reopens by itself (Brave unkeyed and the fallback disabled)"
    secs, half, until = min(halves)
    return f"it reopens at {until} ({half}, in {secs}s)"


def _note_sitewide_unsent(sent: str, exc: BaseException) -> None:
    """The web-search backend refused (cooling, disabled) or failed: reddit was not searched. Its own
    message says whether a request went out; this adds when the backend opens again."""
    diag.note("reddit.sitewide_web_search", url=sent, exc=exc,
              body=f"the reddit-wide search did not run: {exc}; {_web_search_reopens()}; "
                   "this says nothing about whether reddit has posts")


def _note_sitewide_arctic_failed(ids: list[str]) -> None:
    """Web search named posts but Arctic's id lookup failed (breaker open, refused, gate busy)."""
    why = (f"the Arctic breaker is open for {_arctic_open_s():.0f}s more" if _arctic_guard.is_open()
           else "the Arctic Shift /posts/ids lookup failed")
    diag.note("reddit.sitewide_arctic_failed",
              body=f"web search named {len(ids)} reddit posts but {why}, so none were read; "
                   "post URLs: " + " ".join(f"https://www.reddit.com/comments/{i}/" for i in ids))


def _cache_only_skip(where: str) -> bool:
    """True on a cache-only collect (omniseek_search staleness=cache_only) that missed the cache: reddit
    then has nothing to read and must do no live work. Until 2026-10-08 such a miss ran the whole live
    path: the reddit-wide branch reported a misleading ``reddit.sitewide_empty`` ("found nothing")
    for a search that never ran; the Arctic fan-out slept through its retry backoffs and fed the
    Arctic breaker with failures that never happened (enough of them tripped it for everyone); the
    empty answer was then written under the query key (and an empty discovery list under each probed
    term), which the next live search served as reddit's answer. Callers check this right after
    their cache miss; it writes nothing and records nothing."""
    if not cache.cache_only():
        return False
    diag.note("reddit.cache_only",
              body=f"cache-only collect: no cached reddit {where} for this query, no live request made")
    return True


def _sitewide_key(q: str, limit: int) -> str:
    # "web-index": the entries kept while this step went through the browser (until 11-10) are not read back
    return cache.make_key("reddit_arctic", "search_sitewide", q, limit, "web-index")


# ── Finance routing (the finance topic group) ────────────────────────────────────────────────
# A finance query ("$NVDA earnings", "Oracle stock buyback") used to land in the career subs
# (r/cscareerquestions / r/csMajors) because discovery keyed on generic-looking words and never
# reached r/stocks. So when a query carries a FINANCE SIGNAL the topic route adds this small
# curated finance set, after the place, immigration and career groups and before the machine
# learning and academic groups (_topic_subreddits). Kept to 5: reddit is HOST-bound (one Arctic
# host, see _FANOUT_WORKERS) so fan-out width is the cost, and these 5 are the highest-signal
# markets/equity-analysis communities.
_FINANCE_SUBS = [
    "stocks",
    "investing",
    "wallstreetbets",
    "StockMarket",
    "SecurityAnalysis",
]

# A cashtag like $NVDA / $brk.b: 1-5 letters after a literal '$', on a word boundary.
_TICKER_RE = re.compile(r"\$[A-Za-z]{1,5}\b")
# Finance keywords (case-insensitive, whole-word). A small high-signal set: a query carrying any of
# these is about markets/filings, not careers. Intentionally NOT generic business words (no
# "company"/"market" alone) so a non-finance query ("graduate market for PhDs") does not false-trip.
_FINANCE_KEYWORDS = frozenset({
    "earnings", "stock", "stocks", "shares", "valuation", "capex", "bull", "bear",
    "dividend", "guidance", "buyback", "ticker", "ipo", "10-k", "10-q", "8-k",
    "sec", "filing", "filings", "nasdaq", "nyse", "bullish", "bearish",
})
_FINANCE_WORD_RE = re.compile(
    r"(?<![\w$])(?:" + "|".join(re.escape(k) for k in sorted(_FINANCE_KEYWORDS, key=len, reverse=True)) + r")(?![\w])",
    re.IGNORECASE,
)


def _looks_financial(query: str) -> bool:
    """True when ``query`` carries a finance signal: a cashtag ($NVDA) OR a whole-word finance
    keyword (earnings/stock/buyback/SEC filing/…). Whole-word so "stock" hits but "Woodstock" does
    not, and "$NVDA" matches before its letters are mistaken for a keyword. Used only to WIDEN the
    sub set; absent → reddit's routing is completely unchanged."""
    q = query or ""
    return bool(_TICKER_RE.search(q) or _FINANCE_WORD_RE.search(q))


def _with_finance_subs(subreddits: list[str], query: str) -> list[str]:
    """Append the curated finance subs to ``subreddits`` IFF ``query`` looks financial, de-duped
    case-insensitively with the finance subs added LAST (the groups already in ``subreddits`` keep
    their lead position). Not financial → returns ``subreddits`` unchanged (same list identity)."""
    if not _looks_financial(query):
        return subreddits
    seen = {s.lower() for s in subreddits}
    return subreddits + [s for s in _FINANCE_SUBS if s.lower() not in seen]


def _image_media(payload: dict) -> list[str]:
    """Collect image URLs from a submission (thumbnail / preview / direct link)."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(u: Optional[str]) -> None:
        if isinstance(u, str) and u.startswith("http") and u not in seen:
            seen.add(u)
            out.append(u)

    # Direct image link post (url points straight at an image)
    url = payload.get("url") or ""
    if _IMAGE_EXT_RE.search(url):
        _add(url)
    # Resolved preview (highest-res source image), if Arctic carries it
    preview = payload.get("preview")
    if isinstance(preview, dict):
        for img in preview.get("images") or []:
            if isinstance(img, dict):
                src = (img.get("source") or {}).get("url")
                # Reddit HTML-escapes preview URLs (&amp;) — unescape for direct use
                if isinstance(src, str):
                    _add(src.replace("&amp;", "&"))
    # Thumbnail (skip Reddit's sentinel non-URL values)
    thumb = payload.get("thumbnail")
    if isinstance(thumb, str) and thumb not in ("self", "default", "nsfw", "spoiler", "image", ""):
        _add(thumb)
    return out


# ── Arctic Shift's guard: in-flight cap + circuit breaker (the registry's, upstreams.json "arctic_shift")
# The mirror rate-limits the 23-sub fan-out HARD (HTTP 429) under burst load. A per-sub retry is the
# WRONG response to a GLOBAL 429: it amplifies the storm (23 subs x up-to-3 attempts ≈ 69 requests,
# nearly all 429), adds ~20s of jittered backoff to every broad search, AND floods the log (measured:
# 117 "Too Many Requests" lines + 123 "http.get failed" in one window). So the guard's breaker opens
# after a run of failed subs: while open, _arctic_get skips the network entirely (instant None), so the
# fan-out is instant + silent and reddit degrades to cached/empty until the mirror's rate window passes.
# Self-heals: the breaker closes after its window and the first real response resets the count. The
# count follows the guard's one rule (_guard.counts_against_upstream, see _arctic_outcome): a 404 / 410
# / 400 or a 422 that is not the slow-down refusal is Arctic answering, not counted; a 429, a 5xx, a
# timeout, the slow-down 422 and the soft throttle (200 with an ``error``) are. Until
# 2026-11-07 this was a count and cooldown of this module's own, which counted every failure.
#
# The guard's permits are also the global in-flight cap on Arctic egress. Arctic is HOST-bound (ONE
# mirror host) and 429s when hit too hard. A single search's 23-sub fan-out is already bounded to
# _FANOUT_WORKERS, but NOTHING bounded the load ACROSS concurrent searches: under an N-agent burst,
# N x _FANOUT_WORKERS requests storm the one host into a 429 cascade that trips the breaker and empties
# reddit for the whole burst (observed in the 2026-06-20 18-agent stress test). The cap keeps TOTAL
# concurrent Arctic requests near the proven-safe single-search width, so a burst PACES THROUGH (slower)
# instead of STORMING (empty); it is >= _FANOUT_WORKERS so a lone search is never throttled below its
# own fan-out. (Caching cannot fix this: reddit's cache key includes the query, and one host cannot
# serve a burst of NOVEL queries no matter how warm.) The cap, the breaker's count and its window are
# declared in upstreams.json (gate.http_gate false: this module takes the permits itself, around each
# request, and the shared http layer does not take them again).
_arctic_guard = upstreams.guard("arctic_shift", log=logger)
_ARCTIC_MAX_INFLIGHT = _arctic_guard.max_inflight
_arctic_sema = _arctic_guard.sema

# Arctic's load refusal (measured 2026-10-04): under load, or for a request it judges too
# expensive, Arctic answers HTTP 422 with the body {"data":null,"error":"Timeout. Maybe slow down
# a bit"} in about 1.2 to 1.5 s. The docs say its rate limits are computed dynamically from server
# load and request complexity, and a burst of about 15 of our requests made every request come back
# this way. That is the far end refusing us, like a 429: retrying the same request only adds load to
# a host that just asked us to slow down. So such a 422 is not retried and counts toward the breaker
# exactly like a fully retried 429 (one step per sub). A 422 with any other body, like a 404 / 410 /
# 400, is an answer: not retried (_guard.answered) and not counted by the breaker.
# http.get_json returns None for every non-2xx, so the status and body are read from the
# failure record the http layer writes through diag.note, captured privately around each request.
# Until 2026-10-04 (spec 5) that record carried NO body: http read the error body of a streamed
# response that was never read and got nothing. http now keeps a bounded copy of the error body, so
# the refusal is read from what Arctic actually said. A 422 whose body still could not be read is
# read as the refusal too, as the fallback: every 422 Arctic has sent us so far (the 2026-10-04 live
# checks and manual probes, the 2026-06-25 comment endpoint probe) carried the slow-down body. Messages
# quote Arctic's words only when they were observed.
_ARCTIC_REFUSAL_MARKERS = ("slow down", "timeout")
_ARCTIC_REFUSED_HELPER = "reddit.arctic_refused"


@contextmanager
def _egress_trace():
    """Collect the failure records of one Arctic request even when no diag capture is armed.

    Sets a private list on the diag contextvar for the duration (the portal.py precedent for
    reading that var directly), yields it, then restores the caller's trace and copies the
    records into it when the caller had capture armed, so a named drill still sees them."""
    outer = diag._trace_var.get()
    mine: list = []
    token = diag._trace_var.set(mine)
    try:
        yield mine
    finally:
        diag._trace_var.reset(token)
        if outer is not None:
            room = max(0, diag._MAX_CAPTURES - len(outer))
            outer.extend(mine[:room])


def _arctic_refused(captures: list) -> bool:
    """True when a captured failure is Arctic's load refusal: HTTP 422 with its slow-down body, or
    HTTP 422 whose body the http layer could not read (see _ARCTIC_REFUSAL_MARKERS)."""
    for rec in captures or ():
        if (not isinstance(rec, dict) or rec.get("status") != 422
                or rec.get("helper") == _ARCTIC_REFUSED_HELPER):
            continue
        body = str(rec.get("body") or "").lower()
        if not body or any(m in body for m in _ARCTIC_REFUSAL_MARKERS):
            return True
    return False


def _arctic_observed_message(captures: list) -> Optional[str]:
    """Arctic's own words from the first captured 422 whose body was kept: the JSON ``error`` field
    when the body parses, else the body as kept. None when no 422 body was observed."""
    for rec in captures or ():
        if (not isinstance(rec, dict) or rec.get("status") != 422
                or rec.get("helper") == _ARCTIC_REFUSED_HELPER):
            continue
        body = str(rec.get("body") or "").strip()
        if not body:
            continue
        try:
            said = json.loads(body).get("error")
        except Exception:  # noqa: BLE001 (not JSON, or a JSON body that is not an object)
            said = None
        return str(said).strip() if said else body
    return None


def _arctic_refusal_words(observed: Optional[str]) -> str:
    if observed:
        return f"HTTP 422, its \"{observed}\" refusal"
    return "HTTP 422 whose response body was not captured, read as its load refusal"


def _note_arctic_refused(path: str, trace: list) -> None:
    words = _arctic_refusal_words(_arctic_observed_message(trace))
    diag.note(_ARCTIC_REFUSED_HELPER, url=f"{API}{path}", status=422,
              body=(f"Arctic Shift refused the request under load ({words}): the far end's load-based "
                    "rate limit, so it is not retried and counts toward the throttle breaker like a 429"))
    logger.info("Arctic Shift refused %s under load (%s); not retried, counted toward the throttle "
                "breaker", path, words)


def _arctic_outcome(path: str, data: Optional[dict], trace: list,
                    outcome: Optional[list]) -> Optional[list]:
    """One Arctic request's last answer, after its retries: hand the outcome to the guard's breaker and
    return the ``data`` list (None on failure). Shared by ``_arctic_get`` and ``_aarctic_get``."""
    if data is None and _arctic_refused(trace):  # the far end refused us under load: no retry
        _note_arctic_refused(path, trace)
        _arctic_guard.record_fail(status=422, throttled=True)
        if outcome is not None:
            outcome.append("refused")
        return None
    if data is None:  # HTTP-level failure that survived the retries (429 / 5xx / timeout / an answer)
        # a 404 / 410 / 400 / other 422 is Arctic answering: not counted (_guard.answered)
        _arctic_guard.record_fail(settled=answered(trace))
        return None
    if data.get("error"):
        _arctic_guard.record_fail(throttled=True)  # the soft throttle: 200 with an ``error``
        logger.warning("Arctic Shift error: %s", data.get("error"))
        return None
    _arctic_guard.record_ok()  # a real response (even 0 hits) clears the failure count
    result = data.get("data")
    return result if isinstance(result, list) else []


def _arctic_open_s() -> float:
    """Seconds the guard's breaker stays open (0.0 when closed)."""
    return max(0.0, _arctic_guard.state["open_until"] - time.time()) if _arctic_guard.is_open() else 0.0


# ── Subs Arctic cannot title-search (2026-10-05) ─────────────────────────────────────────────
# Arctic's docs say title search is not supported for very active subreddits, and it answers them
# with the same 422 "Timeout. Maybe slow down a bit" body it uses for a load refusal (measured
# 2026-10-05: r/singapore 422 in about 1 s while r/askSingapore and r/canada answered 200 in about
# 8 s; r/cscareerquestions did the same on 2026-10-04). When one fan-out round has a sub answering
# 200 and another sub refused that way, the refusal is about that sub, not about our load: the
# sub goes on this list for a day, the refusal is taken back out of the breaker's count, and the
# list's subs are searched through the browser instead. A round in which nothing answered 200 is
# the far end throttling us, and counts toward the breaker as before.
_UNSEARCHABLE_TTL = 86400


def _unsearchable_key(sub: str) -> str:
    return cache.make_key("reddit_arctic", "unsearchable", sub.lower())


def _arctic_unsearchable(subreddits: list[str]) -> set[str]:
    """Lowercased names of the ``subreddits`` currently on the cannot-search list."""
    return {s.lower() for s in subreddits if cache.get(_unsearchable_key(s))}


def _mark_arctic_unsearchable(subreddits: list[str]) -> None:
    for sub in subreddits:
        cache.set(_unsearchable_key(sub), True, ttl=_UNSEARCHABLE_TTL)


def _settle_round(events: list[tuple[str, str]]) -> list[str]:
    """Read one fan-out round's per-sub outcomes, in completion order ("ok" for any 200 answer,
    "refused" for the slow-down 422, "failed" otherwise). When some sub answered and some were
    refused, return the refused subs for the cannot-search list and take back the breaker steps those
    refusals added after the last answer (an answer already reset the count before them): the round's
    answer is recorded again, after them, which resets the count. Otherwise return [] and leave the
    breaker as the requests left it."""
    oks = [i for i, (_, o) in enumerate(events) if o == "ok"]
    refused = [s for s, o in events if o == "refused"]
    if not oks or not refused:
        return []
    if any(o == "refused" for _, o in events[oks[-1] + 1:]):
        _arctic_guard.record_ok()
    diag.note("reddit.arctic_unsearchable",
              body=(f"Arctic Shift refused a title search on {', '.join('r/' + s for s in refused)} while "
                    "other subs in the same round answered, so the refusal is about the sub, not our "
                    "load: not counted toward the throttle breaker, and the sub goes through the "
                    "browser search for the next 24 hours"))
    return refused

def _arctic_wait(state: RetryCallState) -> float:
    """The jittered backoff before Arctic try n+1: 0.8s + 0.9s for each try before try n + up to 0.8s,
    which de-syncs concurrent per-sub retries (a naive same-instant retry just re-collides with the
    same burst wave)."""
    return 0.8 + (state.attempt_number - 1) * 0.9 + random.uniform(0.0, 0.8)


def _arctic_again(answer: tuple) -> bool:
    """Retry an Arctic answer that is a transient failure while the breaker is closed: an HTTP-level
    failure that is neither Arctic's load refusal nor an answer OmniSeek does not retry (404 / 410 /
    400 / 422, _guard.answered), or the soft throttle (200 with an ``error``)."""
    data, trace = answer
    if data is None:
        transient = not _arctic_refused(trace) and not answered(trace)
    else:
        transient = bool(data.get("error"))
    return transient and not _arctic_guard.is_open()


def _arctic_note_retry(state: RetryCallState) -> None:
    data, _trace = state.outcome.result()
    if data is not None:
        logger.info("Arctic Shift throttled (%s); retrying", data.get("error"))


# _aarctic_get: the retries go through tenacity; ``retries`` (after the first try) is the
# caller's. A GateBusy is not retried; the last answer comes back as it is for the caller's verdict.
_ARCTIC_RETRY = dict(
    wait=_arctic_wait,
    retry=retry_if_result(_arctic_again),
    before_sleep=_arctic_note_retry,
    retry_error_callback=lambda state: state.outcome.result(),
)


def _arctic_get(path: str, params: dict, **kwargs) -> Optional[list]:
    """Sync entry for a caller on a thread: ``_aarctic_get`` on the event loop (portal.call)."""
    return portal.call(_aarctic_get, path, params, **kwargs)


async def _aarctic_get(path: str, params: dict, *, retries: int = 1, timeout: int = 20,
                       outcome: Optional[list] = None) -> Optional[list]:
    """GET an Arctic Shift endpoint → its ``data`` list (or None on failure).

    The mirror throttles the per-sub fan-out under burst load TWO ways, both transient:
      • soft: HTTP 200 + ``{"data": null, "error": "Timeout…"}``
      • hard: HTTP 429/5xx → ``http.aget_json`` returns None
    Retry both, with a jittered backoff that de-syncs concurrent per-sub retries
    (a naive same-instant retry just re-collides with the same burst wave). Arctic's load
    refusal (HTTP 422 with its "slow down" body) is not retried, one breaker step, see
    ``_ARCTIC_REFUSAL_MARKERS``; nor is any other 422 or a 404 / 410 / 400 (_guard.answered).
    The outcome goes to the guard's breaker (``_arctic_outcome``).

    The global in-flight cap is ``_arctic_sema`` (the guard's permits); ``bounded_async_slot`` takes it
    inside this coroutine, in the gate's one line on the loop (no thread waits), with the request
    timeout as the finite queue budget, so no cancellation can separate the acquire from the release.
    """
    if _arctic_guard.is_open():
        return None  # breaker open: skip the throttled mirror entirely (instant, silent, no retry)
    if cache.cache_only():
        return None  # no egress, so no retry sleep and nothing for the breaker (see _cache_only_skip)

    async def _attempt() -> tuple:
        # Hold the global in-flight cap ONLY around the egress (not the backoff sleep between tries), so a
        # burst of concurrent searches/agents paces through the one Arctic host instead of storming it.
        with _egress_trace() as trace:
            async with bounded_async_slot(
                _arctic_sema,
                timeout,
                lambda waited: GateBusy(f"Arctic Shift gate busy after {waited:.1f}s"),
            ):
                # A penalty-boxed Arctic host can manifest as connect failures, so its own paced retry must not double one slot.
                data = await http.aget_json(f"{API}{path}", params=params, timeout=timeout,
                                             retry_transient=False)
        return data, trace

    try:
        data, trace = await AsyncRetrying(sleep=anyio.sleep, stop=stop_after_attempt(retries + 1), **_ARCTIC_RETRY)(_attempt)
    except GateBusy as exc:
        diag.note("reddit.arctic_gate", url=f"{API}{path}", exc=exc)
        return None
    return _arctic_outcome(path, data, trace, outcome)


# ── Reddit-own full-text search via the shared CDP browser (arctic-throttle fallback) ─────────────
# The Arctic mirror periodically 429s ("Too many complex queries") / 500s its /posts/search full-text
# endpoint under load while its cheap /posts/ids lookup keeps working (probed live 2026-07-11). When
# the arctic fan-out comes back EMPTY, fall back to reddit's OWN search. Reddit's public .json is
# 403'd by its datacenter-IP WAF for bare HTTP (curl / httpx / even curl_cffi TLS-impersonation all
# 403 — verified), but a REAL browser session passes it: the shared 9222 Chrome loading
# www.reddit.com/search.json returns HTTP 200 relevance-ranked results. ONE call does a
# relevance-ranked search, and it fires ONLY on an arctic miss, so a healthy mirror never pays the
# browser cost.
# Scoped to the resolved subreddits since 2026-10-04: the global search ignored them, even an explicit
# `subreddit:PhD`, and a PhD-advisor query came back with r/AITAH and r/BestofRedditorUpdates. With a
# sub list it is reddit's multireddit search (/r/A+B/search.json?restrict_sr=1), still ONE request.
# Until 2026-11-09 the tab opened reddit's home page (wait_until="commit") and ran a fetch() in it;
# the home page went on to navigate, which destroyed the execution context under page.evaluate
# ("Execution context was destroyed, most likely because of a navigation"), and that failure, a
# non-200 and a non-JSON wall page all came back as [] and were reported as "nothing found". Now
# the tab loads the search JSON itself and reads that navigation's own response (Chrome's network,
# TLS and cookies, no page script involved), and the outcome says whether reddit answered.
_REDDIT_WEB = "https://www.reddit.com"
_CDP_BODY_HEAD = 300  # characters of a wall page kept in the diagnostic (enough to name the wall)


class _CdpSearch(NamedTuple):
    """One browser search: ``items`` are the native ``t3`` dicts reddit returned (zero hits included);
    ``failure`` is None when reddit answered with a search listing, else ``"blocked"`` (a non-200
    answer, or one that is not a search listing) or ``"error"`` (the browser step raised), and
    ``detail`` says what came back. A failed search says nothing about whether reddit has posts."""
    items: list
    failure: Optional[str] = None
    detail: str = ""


def _cdp_search_path(query: str, limit: int, subreddits: Optional[list[str]] = None) -> str:
    """The same-origin search path: the multireddit search over ``subreddits`` when given, else the
    global search (pure; both shapes are part of the contract)."""
    q = quote(query)
    rest = f"sort=relevance&type=link&limit={min(max(limit, 1), 25)}&raw_json=1"
    subs = [s for s in (subreddits or []) if s]
    if not subs:
        return f"/search.json?q={q}&{rest}"
    multi = "+".join(quote(s, safe="") for s in subs)
    return f"/r/{multi}/search.json?q={q}&restrict_sr=1&{rest}"


def _body_head(text: str) -> str:
    return " ".join((text or "").split())[:_CDP_BODY_HEAD]


def _cdp_search(query: str, limit: int, subreddits: Optional[list[str]] = None) -> _CdpSearch:
    """Reddit full-text search through the shared CDP Chrome (bypasses the datacenter-IP WAF that
    403s bare HTTP), restricted to ``subreddits`` when given. ``items`` are native reddit ``t3``
    submission dicts (the SAME shape ``_submission_to_document`` already consumes). A blocked or
    failed search (cdp_call raises CacheOnlyMiss in a cache-only sweep, so the browser is never
    driven there) is noted here, in the calling thread, as ``reddit.cdp_search_blocked`` /
    ``reddit.cdp_search_error`` and comes back with ``failure`` set, never as a plain empty answer.
    """
    url = _REDDIT_WEB + _cdp_search_path(query, limit, subreddits)

    def _flow(page):
        # Load the JSON itself and read this navigation's response: wait_until="load" is the
        # document settled, and the response object does not depend on the page's script context,
        # so a later navigation by the page cannot break the read.
        resp = page.goto(url, wait_until="load", timeout=20000)
        if resp is None:
            return {"status": None, "text": ""}
        try:
            text = resp.text()
        except Exception as exc:  # noqa: BLE001 — a wall page that navigated away has no body to give
            text = f"<response body unavailable: {type(exc).__name__}: {exc}>"
        return {"status": resp.status, "text": text}

    try:
        from omniseek.core.sources.walled._cdp import cdp_call
        res = cdp_call(_flow, initial_url=None)
    except Exception as exc:  # noqa: BLE001 — CDP / cache-only / network failure: reported, not a miss
        logger.info("reddit CDP search failed: %s", exc)
        diag.note("reddit.cdp_search_error", url=url, exc=exc)
        return _CdpSearch([], "error", f"{type(exc).__name__}: {exc}")
    status = res.get("status") if isinstance(res, dict) else None
    text = (res.get("text") or "") if isinstance(res, dict) else ""
    items = _parse_search_json(text) if status == 200 else None
    if items is None:
        head = _body_head(text)
        diag.note("reddit.cdp_search_blocked", url=url, status=status, body=head)
        return _CdpSearch([], "blocked", f"HTTP {status}, body begins: {head!r}")
    return _CdpSearch(items)


def _parse_search_json(text: str) -> Optional[list[dict]]:
    """Reddit ``/search.json`` body → native ``t3`` submission dicts (the SAME shape
    ``_submission_to_document`` consumes); ``[]`` for a listing with no hits. Pure + offline (this
    shape is the contract); a non-JSON body (a WAF interstitial) or JSON that is not a search listing
    is None: reddit did not answer the search."""
    try:
        data = json.loads(text or "")
    except Exception:  # noqa: BLE001 — a non-JSON body (an interstitial) is not an answer
        return None
    listing = data.get("data") if isinstance(data, dict) else None
    children = listing.get("children") if isinstance(listing, dict) else None
    if not isinstance(children, list):
        return None
    return [c["data"] for c in children
            if isinstance(c, dict) and c.get("kind") == "t3" and isinstance(c.get("data"), dict)]


class RedditAdapter(SourceBase):
    name = "reddit"
    needs_credentials = False  # Arctic Shift needs no auth
    description = (
        "Reddit: GENERAL topic search (via Arctic Shift mirror; Reddit's own API is WAF-blocked). "
        "按话题分组路由: 查询命中哪些话题组就只搜那些组的子版 (科研 r/PhD·AskAcademia·GradSchool, "
        "机器学习 r/MachineLearning·compsci, 求职 r/cscareerquestions·ExperiencedDevs·csMajors, "
        "移民 r/IWantOut·ImmigrationCanada·ExpressEntry·SingaporePR, 含金融信号如 '$NVDA earnings' "
        "→ r/stocks·investing·wallstreetbets 等), 含新加坡、香港、加拿大和主要城市的本地生活子版 "
        "(r/askSingapore·singapore·singaporefi, r/HongKong, r/AskCanada·canada·PersonalFinanceCanada, "
        "r/toronto·vancouver·montreal·Edmonton·ottawa·Calgary·waterloo). 本地生活问题请在查询里带上地名 "
        "(如 'Universal Studios Singapore tickets', 'Montreal apartment rent'). "
        "没有话题组对得上时找与查询词同名的子版 (如 'pour over coffee'→r/Coffee); 什么子版都对不上或都没取到时改搜 "
        "reddit 全站 (共用网页搜索 site:reddit.com, 再经 Arctic 按帖子编号取正文), 结果带 'sitewide' tag + "
        "metadata.search_scope='sitewide'; 网页搜索冷却时照实报没发出去与何时再开. "
        "Arctic 搜不动的大子版 (如 r/singapore) 记名 24 小时, 改走浏览器按子版搜索, 结果带 'cdp-fallback' tag. "
        "查询语义=标题全词 AND、无 OR (按帖子标题匹配): 1-3 个词且含一个生僻词最准 (如 'COMPASS rejected'); "
        "多词 0 命中时自动放宽 (最长 2 词→1 词), 放宽结果带 metadata.query_sent + 'relaxed' tag. "
        "subreddit:NAME[,NAME] 显式限定子版 (覆盖自动路由). "
        "comments: 前缀切到评论路径 (实质答案在评论而非标题; 如 'comments: imposter syndrome' → 高赞回答, 按 score 排序; "
        "路由同上, 但不搜全站). "
        "中文/非拉丁查询请用知乎/B站."
    )

    # Source spec (omniseek.core.filters). subreddit translates into the `subreddit:` qualifier the
    # query parser already reads (it overrides the topic routing).
    filters = {
        "subreddit": {"type": "keyword", "ops": ["eq", "any"], "doc_key": "metadata.subreddit",
                      "desc": "subreddit name without r/ (sent as subreddit:A,B; replaces the topic routing)"},
    }
    coverage = {"since": None, "until": None, "gaps": [
                    "title search only (Arctic Shift refuses its full-text query parameter)",
                    "very active subreddits may refuse title search; those go through the browser"],
                "note": "Arctic Shift community mirror of Reddit, near real time (lag measured 6 to 46 min)"}
    license = ("Reddit user content (rights stay with each poster under the Reddit User Agreement), "
               "read through the Arctic Shift mirror: see upstreams:arctic_shift")
    samples = [{"query": "advisor", "filters": {"subreddit": "PhD"}},
               {"query": "rlhf", "filters": {"subreddit": ["LocalLLaMA", "MachineLearning"]}}]

    def translate_filters(self, query: str, clauses: list) -> str:
        """Prepend ``subreddit:A,B`` (the qualifier _SUB_QUALIFIER_RE reads)."""
        from omniseek.core.filters import Unsupported
        toks = []
        for c in clauses:
            if c.field != "subreddit":
                raise Unsupported(c.field)
            subs = [c.value] if c.op == "eq" else list(c.value)
            subs = [str(x).strip().removeprefix("r/") for x in subs]
            if not all(re.fullmatch(r"[A-Za-z0-9_]+", x) for x in subs):
                raise Unsupported("subreddit", "a subreddit name is letters, digits and _")
            toks.append("subreddit:" + ",".join(subs))
        return " ".join([*toks, (query or "").strip()]).strip()

    @classmethod
    def _finish_docs(cls, merged: list[dict], via_cdp: bool, sent: str, q: str,
                     topup: list[dict], limit: int) -> list[Document]:
        """Map the merged submissions to documents with their provenance marks (shared by search and
        asearch): Arctic hits newest first, browser hits in reddit's order, and browser top-up hits
        for the cannot-search subs after Arctic's, de-duplicated by post id, `limit` in total."""
        if not via_cdp:
            # Arctic Shift has no relevance rank → newest matches first.
            merged.sort(key=lambda d: d.get("created_utc") or 0, reverse=True)
        docs = []
        for it in merged[:limit]:
            doc = cls._submission_to_document(it)
            if via_cdp:  # surface that this came via the browser fallback, not the arctic mirror
                doc.metadata["search_via"] = "cdp_www_reddit"
                doc.tags.append("cdp-fallback")
            elif sent != q:  # capped or relaxed: let the agent see what actually matched
                doc.metadata["query_sent"] = sent
                doc.metadata["query_original"] = q
                doc.tags.append("relaxed")
            docs.append(doc)
        seen = {d.source_id for d in docs}
        for it in topup:
            if len(docs) >= limit:
                break
            pid = it.get("id")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            doc = cls._submission_to_document(it)
            doc.metadata["search_via"] = "cdp_www_reddit"
            doc.tags.append("cdp-fallback")
            docs.append(doc)
        return docs

    async def _asearch_sitewide(self, q: str, limit: int) -> list[Document]:
        """All of reddit: ``site:reddit.com <q>`` through the shared web-search backend, then the posts
        it names read from Arctic by id (see _SITEWIDE_SITE). A backend that refused or failed, or an
        Arctic lookup that failed, is noted and its empty answer is not cached; only an answered
        search says "nothing found"."""
        from omniseek.core.sources.api._search_backend import asearch_web
        key = _sitewide_key(q, limit)
        cached = await anyio.to_thread.run_sync(cache.get_docs, key, limiter=portal.IO_LANE)
        if cached is not None:
            return cached
        if _cache_only_skip("reddit-wide search"):
            return []
        sent = f"site:{_SITEWIDE_SITE} {q}"
        try:
            hits = await asearch_web(sent, n=_SITEWIDE_ENGINE_N)
        except RuntimeError as exc:  # WebSearchUnavailable included: cooling, disabled or failed
            _note_sitewide_unsent(sent, exc)
            return []
        ids = _post_ids_from_urls(h.get("url") for h in hits)
        if not ids:
            _note_sitewide_empty(sent, len(hits), ids)
            return []
        items = await _aarctic_get("/posts/ids", {"ids": ",".join(ids)})
        if items is None:
            _note_sitewide_arctic_failed(ids)
            return []
        by_id = {it.get("id"): it for it in items if isinstance(it, dict)}
        docs = _sitewide_docs([by_id[i] for i in ids if i in by_id], limit, sent)
        if docs:
            await anyio.to_thread.run_sync(functools.partial(cache.set_docs, key, docs, ttl=_SEARCH_TTL), limiter=portal.IO_LANE)
        else:
            _note_sitewide_empty(sent, len(hits), ids)
        return docs

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async replacement of the deleted sync ``search`` (11-04, this is the only one now) (reddit's Arctic submission fan-out goes native async), so
        the live async omniseek_search path awaits it DIRECTLY instead of parking a pool thread on the whole
        fan-out. It mirrors ``search`` step-for-step; only the BLOCKING work moves:
          • the disk cache read/write → ``anyio.to_thread.run_sync`` (get_docs / set_docs do file IO);
          • the Arctic ``/posts/search`` fan-out → concurrent coroutines awaiting ``_aarctic_get`` (the
            native egress twin), the per-round WIDTH bounded to ``_FANOUT_WORKERS`` exactly as the sync
            ThreadPoolExecutor(max_workers=…) bounded it;
          • ``_discover_subreddits`` (its own _arctic_get egress + per-token cache IO) and the SYNC CDP
            fallback (curl/cdp browser) each run OFF the loop via to_thread — self-contained blocking
            units the loop must never run inline; the DOMINANT egress (the submission fan-out) goes native.
        The pure-CPU parse/map/sort (_relax_tiers / _submission_to_document / the recency sort) stays ON
        the loop, byte-identical to ``search``. SAME cache KEY as ``search`` (async and sync share the
        cache). A ``comments:`` query is a whole separate double-fan-out egress path (not the primary
        submission target of this conversion), so it runs its unchanged sync ``_search_comments`` OFF the
        loop — faithful + correct, the _rss precedent for a non-primary path.
        """
        # A `comments:`/`comment:` qualifier flips reddit to the COMMENT path — a separate double-fan-out
        # egress path (direct body= search + thread-tree harvest), not the primary submission target here.
        # Run its unchanged sync body OFF the loop (byte-identical result). Absent → the submission path
        # below goes native.
        q_comment, want_comments = _parse_comment_mode(query or "")
        if want_comments:
            return await anyio.to_thread.run_sync(
                functools.partial(self._search_comments, q_comment, limit), limiter=fetcher._LEGACY_LIMITER)

        # Pull any `subreddit:`/`sub:` qualifier out first (mirrors search). When present it OVERRIDES;
        # absent → the topic-group route, and a Latin query with no sub at all searches all of reddit once.
        q, override_subs = _parse_subreddits(query or "")
        if q and not _latin_content_terms(q):
            _note_non_latin_query(q)
            return []
        # _resolve_subreddits does its own discovery egress and cache IO. Run it OFF the loop; the
        # dominant submission fan-out remains native below. Result is shared with the sync path.
        subreddits = await anyio.to_thread.run_sync(_resolve_subreddits, q, override_subs, limiter=fetcher._LEGACY_LIMITER)
        if not subreddits:
            if _sitewide_eligible(q, override_subs):
                return await self._asearch_sitewide(q, limit)
            return []

        # CRITICAL: the resolved sub set is part of the cache identity, and this is the SAME key as
        # search() — async and sync share the cache.
        # "on-topic": hits carrying none of the query's own topic terms stopped counting (_on_topic,
        # 11-09), so the entries kept before that are not read back.
        key = cache.make_key("reddit_arctic", "search", q, ",".join(subreddits), limit, "on-topic")
        topic = _topic_terms(q, subreddits)

        async def _aget() -> Optional[list[Document]]:
            if _cache_only_skip("search"):
                return None  # a failure: not cached

            per_sub = min(max(limit, 1), 100)
            blocked = await anyio.to_thread.run_sync(_arctic_unsearchable, subreddits, limiter=portal.IO_LANE)  # cache IO OFF loop

            async def _aone_round(tq: str) -> list[dict]:
                targets = [s for s in subreddits if s.lower() not in blocked]
                if not targets:
                    return None  # a failure: not cached
                events: list[tuple[str, str]] = []
                # Bound the per-round fan-out WIDTH to _FANOUT_WORKERS, mirroring the sync
                # ThreadPoolExecutor(max_workers=min(len(subreddits), _FANOUT_WORKERS)). This is the fan-out
                # WIDTH limiter (a local asyncio primitive), NOT the egress guard — the egress guard stays the
                # shared _arctic_sema (the arctic_shift guard's permits), taken inside _aarctic_get in the
                # gate's one line (no thread waits). Bounding the width also keeps at most _FANOUT_WORKERS waiting acquires per
                # search.
                width = asyncio.Semaphore(_FANOUT_WORKERS)

                async def _aone(sub: str) -> list:
                    async with width:
                        params = {"subreddit": sub, "sort": "desc", "limit": per_sub}
                        if tq:
                            params["title"] = tq  # title keyword search; `query` is refused (module docstring)
                        # search fan-out hammers the mirror hardest → deeper retry budget here, but a SHORT
                        # per-request timeout so a throttled mirror fails fast to the CDP fallback (see _SEARCH_TIMEOUT)
                        outcome: list = []
                        res = await _aarctic_get("/posts/search", params, retries=2, timeout=_SEARCH_TIMEOUT,
                                                 outcome=outcome)
                        events.append((sub, "refused" if outcome else ("ok" if res is not None else "failed")))
                        return res or []

                # Every coroutine runs on the one loop thread, so (unlike the sync ThreadPoolExecutor) NO
                # copy_context() is needed: the cache `fresh` contextvar propagates naturally. And today
                # _aarctic_get → http.aget_json does not read cache.get anyway (reddit caches only at the
                # asearch top level), so fresh is not at risk here regardless — identical to the sync note.
                batches = await asyncio.gather(*(_aone(s) for s in targets))
                listed = _settle_round(events)
                if listed:
                    await anyio.to_thread.run_sync(_mark_arctic_unsearchable, listed, limiter=portal.IO_LANE)  # disk write OFF loop
                    blocked.update(s.lower() for s in listed)
                seen: set[str] = set()
                out: list[dict] = []
                for items in batches:
                    for it in items:
                        pid = it.get("id")
                        if pid and pid not in seen:
                            seen.add(pid)
                            out.append(it)
                return out

            # Strict-AND ladder: verbatim (capped) first; relax only while EVERYTHING
            # came back empty — the relaxed rounds replace a useless 0, never real hits.
            merged: list[dict] = []
            sent = q
            for tq in _search_tiers(q, subreddits):
                sent = tq
                merged = _on_topic(await _aone_round(tq), topic)
                if merged:
                    break

            # Arctic-throttle fallback: when the arctic /posts/search fan-out came back EMPTY, fall back to
            # reddit's OWN relevance-ranked search via the shared CDP browser, over the same resolved
            # subreddits as search(). The CDP path is SYNC (curl/cdp)
            # — run it OFF the loop, NEVER on it. Fires ONLY on an arctic miss, so a healthy mirror never pays
            # the browser cost. Its results are already relevance-ranked, so they KEEP reddit's order (not the
            # arctic newest-first reorder below). A short Arctic answer with cannot-search subs resolved
            # sends the ONE browser request to just those subs instead (see search()).
            via_cdp = False
            cdp_failed = False
            topup: list[dict] = []
            if not merged and q:
                found = await anyio.to_thread.run_sync(_cdp_search, q, limit, subreddits, limiter=portal.IO_LANE)
                cdp_failed = found.failure is not None
                cdp_items = _on_topic(found.items, topic)
                if cdp_items:
                    merged = cdp_items
                    via_cdp = True
            elif q and len(merged) < limit:
                listed_subs = [s for s in subreddits if s.lower() in blocked]
                if listed_subs:
                    found = await anyio.to_thread.run_sync(_cdp_search, q, limit, listed_subs, limiter=portal.IO_LANE)
                    topup = _on_topic(found.items, topic)

            docs = self._finish_docs(merged, via_cdp, sent, q, topup, limit)
            # Don't cache a breaker-induced empty (transient throttle): a genuine 0-hit (mirror healthy) IS
            # cached; a CDP-fallback hit IS cached; an all-empty with arctic cooling, or with the browser
            # search blocked or failed (noted by _cdp_search), is not.
            if not docs and (_arctic_guard.is_open() or cdp_failed):
                return None
            return docs

        docs = await self._acached(key, _SEARCH_TTL, _aget)
        if docs or not _sitewide_eligible(q, override_subs):
            return docs
        # The routed subs all refused or matched nothing on the query's topic (a same-name discovery can land
        # on an unrelated very active sub: "teacher" -> r/Teachers, whose posts carry "teacher" but none of
        # "distillation sim2real"; see _on_topic): take the same reddit-wide search a query that routes
        # to no sub gets, and say why on each hit.
        routed = ", ".join("r/" + s for s in subreddits)
        diag.note("reddit.routed_empty_sitewide",
                  body=f"the routed subreddits ({routed}) returned nothing; searched all of reddit instead")
        docs = await self._asearch_sitewide(q, limit)
        for doc in docs:
            doc.metadata["sitewide_reason"] = f"routed subreddits returned nothing: {routed}"
            doc.metadata["routed_subreddits"] = list(subreddits)
        return docs

    # ── comment path (the answers live in comments, not titles) ─────────────────────────────
    def _search_comments(self, query: str, limit: int = 10) -> list[Document]:
        """Surface high-signal COMMENTS for ``query`` (the substantive Reddit answer is in the
        comment tree, not the thread title). Reuses the same subreddit resolution + _arctic_get
        retry/backoff as the submission path; ranks the merged comments by score CLIENT-SIDE
        (Arctic's comment endpoint sorts only by time, asc/desc — there is no server score sort).

        HOST-GENTLE design: reddit's latency is HOST-bound (one Arctic host) and the comment path
        could otherwise double the submission path's already-heavy fan-out. So the sub set is CAPPED
        to ``_COMMENT_MAX_SUBS`` (explicit subs, else the shared topic route) and the two
        comment sources are sequenced — the cheaper, more precise one first, the heavier harvest only
        as a backfill — instead of two full parallel fan-outs:
          1. DIRECT full-text comment search per sub (``body=<query>``) — comments whose own text
             matches the query, anywhere in the (capped) subs. Primary recall when a query is given.
          2. THREAD-TREE harvest (backfill, only when (1) is thin or query-less) — find the top
             ``_COMMENT_THREADS`` matching threads by num_comments, pull each thread's comment tree by
             ``link_id``. Surfaces answers under the most-discussed threads even when the query words
              never appear in a reply (the question is in the title, the answer paraphrases it).
        """
        if query and not _latin_content_terms(query):
            _note_non_latin_query(query)
            return []
        q, override_subs = _parse_subreddits(query or "")
        if override_subs:
            subreddits = override_subs[:_COMMENT_MAX_SUBS]  # explicit subreddit: wins outright
        else:
            # The same routing rule as the submission path (_routed_subreddits), capped to the
            # comment width. No reddit-wide search here: nothing resolved means nothing returned.
            subreddits = _routed_subreddits(q)[:_COMMENT_MAX_SUBS]
            if not subreddits:
                if _cache_only_skip("comment search"):
                    return []
                diag.note("reddit.discovery_empty",
                          body=("no topic group, finance signal or same-name subreddit matched this "
                                "comment query; the comment path has no reddit-wide search, so it "
                                "returns nothing"))
                return []

        # Sub set is part of cache identity (mirrors the submission path) + a 'comments' discriminator
        # so a comment search never collides with a submission search over the same clean q / sub set.
        key = cache.make_key("reddit_arctic", "comments", q, ",".join(subreddits), limit)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached
        if _cache_only_skip("comment search"):
            return []

        seen_ids: set[str] = set()
        comments: list[dict] = []
        harvested_threads: list[dict] = []  # captured from step 2 for the degraded-fallback below

        def _absorb(items: list[dict]) -> None:
            for it in items:
                cid = it.get("id")
                body = (it.get("body") or "").strip()
                if cid and cid not in seen_ids and body not in _DEAD_BODIES:
                    seen_ids.add(cid)
                    comments.append(it)

        # 1) DIRECT full-text comment search (only with a query — the body= filter needs terms).
        #    Fan out over the capped subs with the same workers + retry budget as the submission path.
        if q:
            def _one_direct(sub: str) -> list:
                params = {"subreddit": sub, "sort": "desc", "limit": _COMMENT_PER_SUB, "body": q}
                return _arctic_get("/comments/search", params, retries=2) or []

            contexts = [copy_context() for _ in subreddits]
            with ThreadPoolExecutor(max_workers=min(len(subreddits), _FANOUT_WORKERS)) as ex:
                for batch in ex.map(lambda ctx, s: ctx.run(_one_direct, s), contexts, subreddits):
                    _absorb(batch)

        # 2) THREAD-TREE harvest — BACKFILL only: skip the second fan-out entirely when (1) already
        #    filled the page, so a well-matched query pays ONE comment fan-out (host-gentle). Runs when
        #    the direct search is thin (< limit) or query-less (body= needs terms; thread harvest does not).
        if len(comments) < limit:
            tq = (_relax_tiers(q)[0] if q else "")  # verbatim (capped) tier; comments don't need the full ladder

            def _one_threads(sub: str) -> list:
                params = {"subreddit": sub, "sort": "desc", "limit": min(max(limit, 5), 25)}
                if tq:
                    params["title"] = tq  # title keyword search; `query` is refused (module docstring)
                return _arctic_get("/posts/search", params, retries=2) or []

            threads = harvested_threads  # populate the hoisted list (reused by the degraded-fallback)
            contexts = [copy_context() for _ in subreddits]
            with ThreadPoolExecutor(max_workers=min(len(subreddits), _FANOUT_WORKERS)) as ex:
                for batch in ex.map(lambda ctx, s: ctx.run(_one_threads, s), contexts, subreddits):
                    threads.extend(t for t in batch if t.get("id"))
            threads.sort(key=lambda t: t.get("num_comments") or 0, reverse=True)

            for thread in threads[:_COMMENT_THREADS]:
                link_id = f"t3_{thread.get('id')}"
                sub = thread.get("subreddit") or ""
                params = {"link_id": link_id, "sort": "desc", "limit": _COMMENT_PER_THREAD}
                if sub:
                    params["subreddit"] = sub
                _absorb(_arctic_get("/comments/search", params, retries=2) or [])

        # Graceful degradation: arctic's COMMENT endpoint sheds load far harder than its posts
        # endpoint. Probed live 2026-06-25: /api/comments/search returns HTTP 422 "Timeout. Maybe
        # slow down a bit" (and intermittent 500) for EVERY comment query — even the cheapest
        # no-body one — while /api/posts/search stays healthy. That is an UPSTREAM arctic outage of
        # the comment path, not our routing. When the comment pulls all failed but the thread harvest
        # DID find matching submissions (the posts path works), return those threads instead of a
        # silent 0 — the user still gets the relevant discussions, flagged so the agent knows the
        # comment bodies were unavailable. Transient → not cached (re-fetch once arctic heals).
        if not comments and harvested_threads:
            fallback: list[Document] = []
            for t in harvested_threads[:limit]:
                d = self._submission_to_document(t)
                d.metadata["comment_path_degraded"] = (
                    "arctic comments endpoint unavailable (HTTP 422/500); showing the matching "
                    "threads instead of comment bodies")
                d.tags.append("comment-fallback")
                fallback.append(d)
            return fallback

        # Rank by score (the high-signal answer floats up); ties keep recency (newest first).
        comments.sort(key=lambda c: ((c.get("score") or 0), c.get("created_utc") or 0), reverse=True)
        docs = [self._comment_to_document(c) for c in comments[:limit]]
        if docs or not _arctic_guard.is_open():  # don't cache a breaker-induced empty (transient throttle)
            cache.set_docs(key, docs, ttl=_COMMENT_TTL)
        return docs

    async def afetch_url(self, url: str) -> Optional[Document]:
        if "reddit.com" not in (urlparse(url).hostname or ""):
            return None
        parts = urlparse(url).path.strip("/").split("/")
        # /r/<sub>/comments/<ID>/<slug>
        if len(parts) >= 4 and parts[0] == "r" and parts[2] == "comments":
            pid = parts[3]
            items = await _aarctic_get("/posts/ids", {"ids": pid})
            if items:
                return self._submission_to_document(items[0])
        return None

    async def ahealth_check(self) -> tuple[Optional[bool], str]:
        # Breaker open = Arctic is THROTTLING us (429), not down: the host is alive and the data path
        # falls back to cache, so this must not read "unreachable" (mirrors _s2.health's "429 = API
        # alive, rate-limiting us"). Conflating throttle with down would false-alarm the health sweep
        # AND hide WHY reddit returns empty during a burst (the legibility gap the 18-agent concurrency
        # stress test surfaced: reddit was throttled, not broken). While cooling no probe is sent, so
        # nothing is verified: None (the watchdog's `unverified`), neither healthy nor failing.
        if _arctic_guard.is_open():
            return None, _probe.breaker_open(
                "Arctic Shift rate-limited, reddit serves cache until it clears", _arctic_open_s())
        # The probe has the shape the data path uses (title keyword search, 2026-10-04), so health
        # tests what search actually sends.
        diag.enable()
        try:
            items = await _aarctic_get("/posts/search", {
                "subreddit": "PhD",
                "title": "advisor",
                "sort": "desc",
                "limit": 1,
            })
        finally:
            captures = diag.drain()
        if items:
            return True, f"OK (Arctic Shift mirror; {len(items)} probe item)"
        if items == []:
            # r/PhD always has recent posts titled with "advisor", so a well-formed 0-item response
            # means the data path is degraded (e.g. an Arctic API/shape change that returns nothing)
            # even though the host answered. Liveness alone misses this: require actual content, so
            # health is a data-path check not a reachability ping.
            return False, "Arctic Shift returned 0 items for the r/PhD title=advisor probe (data path degraded)"
        if _arctic_refused(captures):
            # The far end refused the probe under load, the same verdict as a 429: it answered, but
            # not whether the mirror serves, so it is not verified (None; until 2026-10-04 this read
            # False). Arctic's message is quoted only when the http layer kept the body.
            words = _arctic_refusal_words(_arctic_observed_message(captures))
            return None, (f"not verified: rate-limited (Arctic Shift: {words}; the far end's load-based "
                          "rate limit refused the probe; not retried)")
        if captures:
            observed = captures[-1]
            status = observed.get("status")
            body = observed.get("body")
            exc = observed.get("exc")
            if status is not None:
                detail = f"HTTP {status}"
                if body:
                    detail += f": {body}"
            elif exc:
                detail = f"request failed ({exc})"
            else:
                detail = "request failed without an observed response"
            return False, f"Arctic Shift {detail}"
        return False, "Arctic Shift request failed without an observed response"

    @staticmethod
    def _submission_to_document(payload: dict) -> Document:
        body = payload.get("selftext") or ""
        url = payload.get("url") or ""
        permalink = payload.get("permalink") or ""
        canonical_url = f"https://www.reddit.com{permalink}" if permalink else url

        # a link post has no body of its own: the link rides in metadata.external_url

        created_utc = payload.get("created_utc")
        date = None
        if created_utc:
            try:
                date = datetime.fromtimestamp(float(created_utc), tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                date = None

        author = payload.get("author") or "[deleted]"
        subreddit = payload.get("subreddit") or ""

        return Document(
            source="reddit",
            source_id=payload.get("id") or "",
            url=canonical_url,
            title=payload.get("title") or "(untitled)",
            content=body,  # full selftext — no truncation
            author=f"u/{author}",
            date=date,
            # A thread's COMMENT COUNT is what says "there is a discussion here"; upvotes alone
            # cannot (2026-07-25). num_comments was already in this payload -- this file even sorts
            # threads by it (see _COMMENT_THREADS) -- it just never reached the card.
            signals={
                **mk_signal('upvotes', payload.get("score"), kind='engagement', by='reddit/score'),
                **mk_signal('comments', payload.get("num_comments"),
                            kind='engagement', by='reddit/num_comments'),
            },
            tags=[f"r/{subreddit}"] if subreddit else [],
            media=_image_media(payload),
            metadata={
                "subreddit": subreddit,
                "num_comments": payload.get("num_comments"),
                "upvote_ratio": payload.get("upvote_ratio"),
                "is_self": payload.get("is_self"),
                "external_url": url if not payload.get("is_self") else None,
                "flair": payload.get("link_flair_text"),
                "raw": payload,  # Arctic Shift's original Reddit submission JSON
            },
        )

    @staticmethod
    def _comment_to_document(payload: dict) -> Document:
        """One Reddit COMMENT → Document. content=body (already Markdown — no HTML
        conversion needed for comments), url from the comment permalink, author=u/…, score
        through mk_signal, tags=[r/sub]. The thread (link_id) + parent are kept in metadata so
        the agent can drill to the full thread (omniseek_read on the parent submission)."""
        body = (payload.get("body") or "").strip()

        permalink = payload.get("permalink") or ""
        if permalink:
            url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else permalink
        else:
            url = "https://www.reddit.com"

        created_utc = payload.get("created_utc")
        date = None
        if created_utc:
            try:
                date = datetime.fromtimestamp(float(created_utc), tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                date = None

        author = payload.get("author") or "[deleted]"
        subreddit = payload.get("subreddit") or ""
        link_id = payload.get("link_id") or ""           # t3_<submission id>
        link36 = link_id[3:] if link_id.startswith("t3_") else link_id

        # A comment has no title of its own — synthesize a compact, list-view-friendly one so
        # omniseek_search summaries read sensibly ("comment by u/X in r/sub" + the body's first line).
        first_line = body.splitlines()[0] if body else ""
        snippet = (first_line[:80] + "…") if len(first_line) > 80 else first_line
        where = f"r/{subreddit}" if subreddit else "reddit"
        title = f"Comment by u/{author} in {where}" + (f": {snippet}" if snippet else "")

        return Document(
            source="reddit",
            source_id=payload.get("id") or "",
            url=url,
            title=title,
            content=body,  # comment body is Reddit Markdown already — no conversion
            author=f"u/{author}",
            date=date,
            signals=mk_signal('upvotes', payload.get("score"),
                              kind='engagement', by='reddit/score'),
            tags=[f"r/{subreddit}"] if subreddit else [],
            metadata={
                "subreddit": subreddit,
                "kind": "comment",
                "link_id": link_id,                       # the thread this comment belongs to
                "parent_id": payload.get("parent_id"),    # t1_<comment> or t3_<submission>
                "thread_url": (f"https://www.reddit.com/r/{subreddit}/comments/{link36}/"
                               if subreddit and link36 else None),
                "is_submitter": payload.get("is_submitter"),
                "controversiality": payload.get("controversiality"),
                "raw": payload,  # Arctic Shift's original Reddit comment JSON
            },
        )


register_adapter(RedditAdapter())
