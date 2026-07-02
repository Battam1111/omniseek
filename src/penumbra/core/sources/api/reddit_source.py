"""Reddit adapter — via the Arctic Shift mirror (Reddit's own .json is WAF-blocked).

2026-05-30: Reddit's public ``.json`` endpoints began returning IP-level WAF
blocks (403) for our server, and self-service OAuth was closed in 2025-11. We
migrated to the **Arctic Shift API** (https://github.com/ArthurHeitmann/arctic_shift),
a community Reddit mirror with near-real-time ingestion (measured lag ~6–46 min,
fine for monitoring). No auth / API key / User-Agent required.

Endpoints:
    GET /api/posts/search?subreddit=<sub>&query=<q>&sort=desc&limit=<=100
        Full-text (title+selftext) search within ONE subreddit. Multi-subreddit
        is NOT supported, so we fan out over DEFAULT_SUBREDDITS serially (gentle
        pacing — bursts trigger a soft "slow down" error) and merge by recency.
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
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from penumbra.core import cache, http
from penumbra.core.fetcher import register_adapter
from penumbra.core.normalize import Document, mk_signal

logger = logging.getLogger(__name__)

API = "https://arctic-shift.photon-reddit.com/api"
_SEARCH_TTL = 900       # 15 min
# Concurrency over the per-sub fan-out. Arctic Shift can only search ONE sub per
# request, so 23 default subs are pushed in waves of this width. KEPT AT 5: a measured
# bump to 10 made reddit SLOWER (34s vs 25s) — higher concurrency against the single
# Arctic host raises its soft-throttle (HTTP200+data:null) rate, pushing more subs
# through the full retries=2 jittered backoff. reddit's latency is HOST-bound, not
# fan-out-bound; its breadth-safe fix is background pre-warm (keep the cache hot so a
# broad search never pays the live Arctic cost), NOT more concurrency. backoff +
# retries + relaxation ladder + the full 23-sub set + per-sub limit are all unchanged.
_FANOUT_WORKERS = 5

# Highest-value subs: PhD methodology + ML/AI + 海外长期落地 (SG/Canada).
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
# regression to submissions or the auto-route). The comment path itself still uses _parse_subreddits
# + _discover_subreddits for its subreddit set, so `comments: subreddit:LocalLLaMA rlhf` works and a
# bare `comments: pour over coffee` still discovers r/Coffee.
_COMMENT_QUALIFIER_RE = re.compile(r"(?:^|\s)comments?\s*:", re.IGNORECASE)
# Arctic's comments endpoint takes full-text via `body` (NOT `query`, which it 400s) and supports a
# `link_id=t3_<id>` thread filter; `sort` accepts ONLY asc/desc (recency) — there is no server-side
# score sort, so the comment path ranks by `score` CLIENT-SIDE to surface the high-signal answers.
_COMMENT_TTL = 900           # 15 min, same as submissions
_COMMENT_THREADS = 3         # top submission threads (by num_comments) to pull comment trees from
_COMMENT_PER_THREAD = 60     # comments fetched per thread before client-side score-ranking
_COMMENT_PER_SUB = 40        # comments per sub for the direct full-text (body=) comment search
# Cap the comment-path fan-out width. reddit's latency is HOST-bound (one Arctic host); the
# submission path already pays a ~23-sub fan-out, and the comment path would otherwise DOUBLE that
# host pressure (a direct comment search PLUS a thread-harvest search). A query rarely needs more
# than its few most-relevant communities for COMMENT depth (vs the submission path's monitor breadth),
# so cap to the top-N subs — explicit/discovered subs lead, the curated core backstops. Keeps the
# comment path a good neighbor to the submission cache-warm instead of triggering the soft-throttle.
_COMMENT_MAX_SUBS = 6
# Reddit's sentinel non-content comment bodies — dropped before ranking (they carry no answer).
_DEAD_BODIES = frozenset({"", "[removed]", "[deleted]", "[ Removed by Reddit ]"})

# Arctic Shift ANDs every token; past this many terms the co-occurrence requirement
# (and the server-side cost) makes 0-hit the norm, so cap the verbatim tier.
_MAX_AND_TERMS = 4

# ── Query-driven topical-subreddit discovery (the "general engine" route) ────────────────────
# DEFAULT_SUBREDDITS is a curated research/career/immigration MONITOR core — perfect for "phd
# advice" or "express entry", useless for "pour over coffee" (which used to be forced through
# r/PhD and came back as ML noise). This route ADDS the subreddit(s) that actually match the
# query's topic, so reddit is a GENERAL source: any topic reaches its real community while the
# curated core still backstops research/career intent (a short topical term like "phd" lives in
# the core, not in discovery, so research queries never regress). The core is always searched;
# discovery only widens. Looks up the 2 longest Latin query tokens (length = specificity proxy,
# same heuristic as _relax_tiers) via Arctic's subreddit_prefix endpoint, keeps public non-NSFW
# subs above a subscriber floor, ranks by size, adds the top few not already in the core.
_DISCOVER_TOP = 4            # max topical subs appended per query
_DISCOVER_MIN_SUBS = 5000    # skip tiny/dead/squatted subs
_DISCOVER_TTL = 86400        # sub metadata is stable day-to-day → cache discovery a full day

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
    list of requested subreddits (overriding ``DEFAULT_SUBREDDITS``) or ``None``
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


def _discover_subreddits(query: str) -> list[str]:
    """Topical subreddits matching ``query``, for the query-driven route (best-effort → [] on any
    failure, so the caller always still has DEFAULT_SUBREDDITS).

    Probes the 2 longest Latin tokens via Arctic's ``subreddit_prefix`` endpoint, keeps public,
    non-NSFW subs above the subscriber floor, ranks the union by subscriber count, and returns the
    top names NOT already in the curated core (de-duped case-insensitively). Per-term lookups are
    cached (_DISCOVER_TTL) so a repeated topic costs no live discovery GETs. Non-Latin queries
    (e.g. "手冲咖啡") yield no tokens → [] → reddit honestly returns ~nothing for content it can't
    serve, instead of forcing it through the research core (those belong on zhihu/bilibili)."""
    # Probe the query's CONTENT terms ranked by SPECIFICITY (acronyms/proper nouns first), NOT raw
    # length — the length proxy probed glue words ("without"/"foreign") and discovered GARBAGE subs
    # (r/ForeignMovies, r/WithoutATrace) that then 422-stormed the mirror (2026-06-21 defect).
    toks = [t for t in _content_terms(query) if re.fullmatch(r"[A-Za-z0-9]+", t) and len(t) >= 3]
    if not toks:
        return []
    probes = sorted(set(toks), key=_term_rank, reverse=True)[:2]
    core_lower = {s.lower() for s in DEFAULT_SUBREDDITS}
    found: dict[str, tuple[str, int]] = {}  # lower-name -> (display_name, subscribers)
    for tok in probes:
        ck = cache.make_key("reddit_arctic", "discover", tok.lower())
        subs = cache.get(ck)
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
            if key in core_lower:
                continue
            prev = found.get(key)
            if prev is None or nsubs > prev[1]:
                found[key] = (name, nsubs)
    ranked = sorted(found.values(), key=lambda x: x[1], reverse=True)
    return [name for name, _ in ranked[:_DISCOVER_TOP]]


# ── Finance routing (the curated core is research/career/immigration; it knows nothing about
#    markets) ───────────────────────────────────────────────────────────────────────────────
# A finance query ("$NVDA earnings", "Oracle stock buyback") used to land in the career core
# (r/cscareerquestions / r/csMajors) because _discover_subreddits keys on the longest token and
# "Oracle"/"earnings" look like generic words, never reaching r/stocks. So when a query carries a
# FINANCE SIGNAL we ADD a small curated finance set on top of the existing subs (additive, exactly
# like _discover_subreddits: the research core is never replaced, and a non-financial query is
# byte-for-byte unchanged: no signal -> no finance subs -> identical fan-out). Kept to 5: reddit is
# HOST-bound (one Arctic host, see _FANOUT_WORKERS) so fan-out width is the cost, and these 5 are the
# highest-signal markets/equity-analysis communities.
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
    case-insensitively with the finance subs added LAST (the existing core/discovered/explicit subs
    keep their lead position and priority). Not financial → returns ``subreddits`` unchanged (same
    list identity contract the caller relies on for the no-regression path)."""
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


# ── Arctic Shift global-throttle circuit breaker ──────────────────────────────────────────────
# The mirror now rate-limits the 23-sub fan-out HARD (HTTP 429) under burst load. A per-sub retry
# is the WRONG response to a GLOBAL 429: it amplifies the storm (23 subs x up-to-3 attempts ≈ 69
# requests, nearly all 429), adds ~20s of jittered backoff to every broad search, AND floods the
# log (measured: 117 "Too Many Requests" lines + 123 "http.get failed" in one window). So a
# sustained per-sub failure streak trips a SHORT global cooldown: while open, _arctic_get skips the
# network entirely (instant None), so the fan-out is instant + silent and reddit degrades to
# cached/empty until the mirror's rate window passes. Self-heals: the cooldown expires and the
# first real response resets the streak. Same circuit-breaker shape as the sogou/xhs anti-bot guards.
_arctic_lock = threading.Lock()
_arctic_fail_streak = 0
_arctic_cooldown_until = 0.0
_ARCTIC_TRIP_AFTER = 5     # failed SUBS in quick succession ⇒ a global throttle, not a few flaky subs
_ARCTIC_COOLDOWN = 120.0   # seconds to stop hitting the mirror entirely once tripped

# Global in-flight cap on Arctic egress (the s2/openalex pattern reddit was missing). Arctic is
# HOST-bound (ONE mirror host) and 429s when hit too hard. A single search's 23-sub fan-out is
# already bounded to _FANOUT_WORKERS, but NOTHING bounded the load ACROSS concurrent searches: under
# an N-agent burst, N x _FANOUT_WORKERS requests storm the one host into a 429 cascade that trips the
# breaker and empties reddit for the whole burst (observed in the 2026-06-20 18-agent stress test).
# This process-global semaphore caps TOTAL concurrent Arctic requests near the proven-safe single-
# search width, so a burst PACES THROUGH (slower) instead of STORMING (empty). Set >= _FANOUT_WORKERS
# so a lone search is never throttled below its own fan-out; the breaker still backstops a real outage,
# this just stops self-inflicted bursts from ever reaching it. (Caching cannot fix this: reddit's
# cache key includes the query, and one host cannot serve a burst of NOVEL queries no matter how warm.)
_ARCTIC_MAX_INFLIGHT = 6
_arctic_sema = threading.BoundedSemaphore(_ARCTIC_MAX_INFLIGHT)


def _arctic_cooling() -> bool:
    return time.time() < _arctic_cooldown_until


def _arctic_record(ok: bool) -> None:
    """Feed one per-sub outcome to the breaker: a real response resets the streak; a fully-retried
    failure extends it and trips a global cooldown once the streak shows the host is throttling."""
    global _arctic_fail_streak, _arctic_cooldown_until
    with _arctic_lock:
        if ok:
            _arctic_fail_streak = 0
            return
        _arctic_fail_streak += 1
        if _arctic_fail_streak >= _ARCTIC_TRIP_AFTER and not _arctic_cooling():
            _arctic_cooldown_until = time.time() + _ARCTIC_COOLDOWN
            logger.warning("Arctic Shift throttling (%d consecutive sub-failures); backing off %ds — "
                           "reddit serves cached/empty until then (NOT retrying through a global 429)",
                           _arctic_fail_streak, int(_ARCTIC_COOLDOWN))


def _arctic_get(path: str, params: dict, *, retries: int = 1) -> Optional[list]:
    """GET an Arctic Shift endpoint → its ``data`` list (or None on failure).

    The mirror throttles the per-sub fan-out under burst load TWO ways, both transient:
      • soft: HTTP 200 + ``{"data": null, "error": "Timeout…"}``
      • hard: HTTP 422/429/5xx → ``http.get_json`` returns None
    Retry both, with a jittered backoff that de-syncs concurrent per-sub retries
    (a naive same-instant retry just re-collides with the same burst wave).
    """
    if _arctic_cooling():
        return None  # breaker open: skip the throttled mirror entirely (instant, silent, no retry)

    def _backoff(attempt: int) -> None:
        time.sleep(0.8 + attempt * 0.9 + random.uniform(0.0, 0.8))

    for attempt in range(retries + 1):
        # Hold the global in-flight cap ONLY around the egress (not the backoff sleep below), so a
        # burst of concurrent searches/agents paces through the one Arctic host instead of storming it.
        with _arctic_sema:
            data = http.get_json(f"{API}{path}", params=params, timeout=20)
        if data is None:  # HTTP-level failure (422/429/5xx/timeout) — transient under burst
            if attempt < retries and not _arctic_cooling():
                _backoff(attempt)
                continue
            _arctic_record(False)  # this sub fully failed → feed the breaker
            return None
        if data.get("error"):
            if attempt < retries and not _arctic_cooling():
                logger.info("Arctic Shift throttled (%s); retrying", data.get("error"))
                _backoff(attempt)
                continue
            _arctic_record(False)
            logger.warning("Arctic Shift error: %s", data.get("error"))
            return None
        _arctic_record(True)  # a real response (even 0 hits) clears the failure streak
        result = data.get("data")
        return result if isinstance(result, list) else []
    return None


class RedditAdapter:
    name = "reddit"
    needs_credentials = False  # Arctic Shift needs no auth
    description = (
        "Reddit — GENERAL topic search (via Arctic Shift mirror; Reddit's own API is WAF-blocked). "
        "自动按查询路由到对应话题子版 (如 'pour over coffee'→r/Coffee; 含金融信号如 '$NVDA earnings' "
        "→ 追加 r/stocks·investing·wallstreetbets 等), 同时常驻搜索 "
        "r/PhD·AskAcademia·MachineLearning + 移民/求职 核心子版 (科研/职业意图永不丢失). "
        "查询语义=全词 AND、无 OR: 1-3 个词且含一个生僻词最准 (如 'COMPASS rejected'); "
        "多词 0 命中时自动放宽 (最长 2 词→1 词), 放宽结果带 metadata.query_sent + 'relaxed' tag. "
        "subreddit:NAME[,NAME] 显式限定子版 (覆盖自动路由). "
        "comments: 前缀切到评论路径 (实质答案在评论而非标题; 如 'comments: imposter syndrome' → 高赞回答, 按 score 排序). "
        "中文/非拉丁查询请用知乎/B站."
    )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        # A `comments:`/`comment:` qualifier flips reddit to the COMMENT path (the
        # substantive answer is in the comment tree, not the title). Strip it first;
        # the remaining query is dispatched to _search_comments. Absent → fall through
        # to the unchanged submission path (no regression to submissions / auto-route).
        q_comment, want_comments = _parse_comment_mode(query or "")
        if want_comments:
            return self._search_comments(q_comment, limit)

        # Pull any `subreddit:`/`sub:` qualifier out of the query first. When
        # present it OVERRIDES everything (search only those subs) and is stripped
        # from the keyword part. Absent → search the curated research/career CORE
        # plus any topical subs the query itself discovers (the general-engine route:
        # "pour over coffee" reaches r/Coffee instead of being forced through r/PhD).
        q, override_subs = _parse_subreddits(query or "")
        if override_subs:
            subreddits = override_subs  # explicit subreddit: wins outright (no auto-widening)
        else:
            # Auto-route: curated core + query-discovered topical subs, then a curated finance set
            # appended IFF the query looks financial (so "$NVDA earnings" reaches r/stocks instead of
            # being stranded in the career core). _with_finance_subs is a no-op for any other query.
            subreddits = _with_finance_subs(DEFAULT_SUBREDDITS + _discover_subreddits(q), q)

        # CRITICAL: the resolved sub set is part of the cache identity — otherwise
        # `subreddit:A foo` and `subreddit:B foo` (same clean q) would collide.
        key = cache.make_key("reddit_arctic", "search", q, ",".join(subreddits), limit)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached

        per_sub = min(max(limit, 1), 100)

        def _one_round(tq: str) -> list[dict]:
            def _one(sub: str) -> list:
                params = {"subreddit": sub, "sort": "desc", "limit": per_sub}
                if tq:
                    params["query"] = tq
                # search fan-out hammers the mirror hardest → deeper retry budget here
                return _arctic_get("/posts/search", params, retries=2) or []

            # Capture the cache `fresh` contextvar HERE on the search thread and
            # hand each worker its own private copy via ctx.run — never copy inside
            # the worker (that grabs an empty context and silently defeats fresh=True).
            # Today _arctic_get → http.get_json does not read cache.get (reddit caches
            # only at the search() top level), so fresh is not currently at risk here;
            # propagating the context anyway matches the verified template and is
            # future-proof if a per-sub cache.get is ever added. Zero effect on results.
            contexts = [copy_context() for _ in subreddits]
            with ThreadPoolExecutor(max_workers=min(len(subreddits), _FANOUT_WORKERS)) as ex:
                batches = list(ex.map(lambda ctx, s: ctx.run(_one, s),
                                      contexts, subreddits))
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
        for tq in (_relax_tiers(q) if q else [""]):
            sent = tq
            merged = _one_round(tq)
            if merged:
                break

        # Arctic Shift has no relevance rank → newest matches first.
        merged.sort(key=lambda d: d.get("created_utc") or 0, reverse=True)
        docs = []
        for it in merged[:limit]:
            doc = self._submission_to_document(it)
            if sent != q:  # capped or relaxed — let the agent see what actually matched
                doc.metadata["query_sent"] = sent
                doc.metadata["query_original"] = q
                doc.tags.append("relaxed")
            docs.append(doc)
        # Don't cache a breaker-induced empty (transient throttle): a genuine 0-hit (mirror healthy)
        # IS cached to avoid re-hammering, but a throttled-empty must re-fetch once the mirror heals.
        if docs or not _arctic_cooling():
            cache.set_docs(key, docs, ttl=_SEARCH_TTL)
        return docs

    # ── comment path (the answers live in comments, not titles) ─────────────────────────────
    def _search_comments(self, query: str, limit: int = 10) -> list[Document]:
        """Surface high-signal COMMENTS for ``query`` (the substantive Reddit answer is in the
        comment tree, not the thread title). Reuses the same subreddit resolution + _arctic_get
        retry/backoff as the submission path; ranks the merged comments by score CLIENT-SIDE
        (Arctic's comment endpoint sorts only by time, asc/desc — there is no server score sort).

        HOST-GENTLE design: reddit's latency is HOST-bound (one Arctic host) and the comment path
        could otherwise double the submission path's already-heavy fan-out. So the sub set is CAPPED
        to ``_COMMENT_MAX_SUBS`` (explicit/discovered subs lead, curated core backstops) and the two
        comment sources are sequenced — the cheaper, more precise one first, the heavier harvest only
        as a backfill — instead of two full parallel fan-outs:
          1. DIRECT full-text comment search per sub (``body=<query>``) — comments whose own text
             matches the query, anywhere in the (capped) subs. Primary recall when a query is given.
          2. THREAD-TREE harvest (backfill, only when (1) is thin or query-less) — find the top
             ``_COMMENT_THREADS`` matching threads by num_comments, pull each thread's comment tree by
             ``link_id``. Surfaces answers under the most-discussed threads even when the query words
             never appear in a reply (the question is in the title, the answer paraphrases it).
        """
        q, override_subs = _parse_subreddits(query or "")
        if override_subs:
            subreddits = override_subs[:_COMMENT_MAX_SUBS]  # explicit subreddit: wins outright
        else:
            # Discovered topical subs (most query-specific) lead, then the curated finance set when the
            # query looks financial (so a finance comment search reaches r/stocks et al. before being
            # capped), then the research core backstops; finally cap the width. _with_finance_subs is a
            # no-op for a non-financial query, so its comment routing is unchanged.
            discovered = _discover_subreddits(q)
            lead = _with_finance_subs(discovered, q)  # discovered (+ finance subs if financial)
            lead_lower = {s.lower() for s in lead}
            ranked = lead + [s for s in DEFAULT_SUBREDDITS if s.lower() not in lead_lower]
            subreddits = ranked[:_COMMENT_MAX_SUBS]

        # Sub set is part of cache identity (mirrors the submission path) + a 'comments' discriminator
        # so a comment search never collides with a submission search over the same clean q / sub set.
        key = cache.make_key("reddit_arctic", "comments", q, ",".join(subreddits), limit)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached

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
                    params["query"] = tq
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
        if docs or not _arctic_cooling():  # don't cache a breaker-induced empty (transient throttle)
            cache.set_docs(key, docs, ttl=_COMMENT_TTL)
        return docs

    def fetch_url(self, url: str) -> Optional[Document]:
        if "reddit.com" not in (urlparse(url).hostname or ""):
            return None
        parts = urlparse(url).path.strip("/").split("/")
        # /r/<sub>/comments/<ID>/<slug>
        if len(parts) >= 4 and parts[0] == "r" and parts[2] == "comments":
            pid = parts[3]
            items = _arctic_get("/posts/ids", {"ids": pid})
            if items:
                return self._submission_to_document(items[0])
        return None

    def health_check(self) -> tuple[bool, str]:
        # Breaker open = Arctic is THROTTLING us (429), not down: the host is alive and the data path
        # falls back to cache, so report healthy-with-a-note rather than "unreachable" (mirrors
        # _s2.health's "429 = API alive, rate-limiting us"). Conflating throttle with down would
        # false-alarm the health sweep AND hide WHY reddit returns empty during a burst (the legibility
        # gap the 18-agent concurrency stress test surfaced: reddit was throttled, not broken).
        if _arctic_cooling():
            return True, "OK (Arctic Shift; rate-limited/cooling, reddit serves cache until it clears)"
        items = _arctic_get("/posts/search", {"subreddit": "PhD", "sort": "desc", "limit": 1})
        if items:
            return True, f"OK (Arctic Shift mirror; {len(items)} probe item)"
        if items == []:
            # A bare r/PhD probe (no query) is never legitimately empty (the sub always has fresh
            # posts), so a well-formed 0-item response means the data path is degraded (e.g. an Arctic
            # API/shape change that returns nothing) even though the host answered. Liveness alone
            # misses this: require actual content, so health is a data-path check not a reachability ping.
            return False, "Arctic Shift returned 0 items for a bare r/PhD probe (data path degraded)"
        return False, "Arctic Shift unreachable"

    @staticmethod
    def _submission_to_document(payload: dict) -> Document:
        body = payload.get("selftext") or ""
        url = payload.get("url") or ""
        permalink = payload.get("permalink") or ""
        canonical_url = f"https://www.reddit.com{permalink}" if permalink else url

        if not body and url and url != canonical_url:
            body = f"(Link post)\n{url}"

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
            signals=mk_signal('upvotes', payload.get("score"),
                              kind='engagement', by='reddit/score'),
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
        the agent can drill to the full thread (penumbra_add_url on the parent submission)."""
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
        # penumbra_search summaries read sensibly ("comment by u/X in r/sub" + the body's first line).
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
