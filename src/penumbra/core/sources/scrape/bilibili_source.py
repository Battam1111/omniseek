"""B站 (Bilibili) adapter.

Uses Bilibili's public search API directly via httpx. No auth required
for basic video search. Subtitle extraction is a separate concern (would
need to fetch the subtitle file URL from the video page and parse it).
For now we expose video metadata + descriptions; subtitle support is
Phase 2.5 (see ROADMAP).

API endpoint: https://api.bilibili.com/x/web-interface/wbi/search/type

Migrated to ``BaseScrapeAdapter`` (template method): the base owns the
cache-check / cache-set / auto-register mechanism. The source-specific facts live
in the hooks — ``_raw_fetch`` (the anti-bot ``_bili_request``: a fresh session that
FIRST bootstraps the buvid cookies, plus the ``data.code != 0`` guard) and
``_to_documents`` (the per-video raw→doc mapping). Bilibili returns server order
(``order=totalrank``), so ``rank = False`` keeps that order verbatim (byte-identical
to the hand-written form). ``fetch_url`` + ``health_check`` are overridden because
they do real API I/O (the BV-id /view lookup and a live search probe).

COMMENT layer (mirrors the YouTube adapter's two-shape design): bilibili's top
comments come from the LEGACY keyless reply endpoint
``GET /x/v2/reply?type=1&oid=<aid>&sort=2`` (sort=2 = by likes; the newer
``x/v2/reply/wbi/main`` 403s without a WBI signature + buvid activation, so we stay
on the legacy one, which the buvid-bootstrapped session reaches). Comments key on
the video's ``aid`` (numeric), resolved from the BV-id via ``/x/web-interface/view``.
They surface in TWO shapes, both bounded to the top ~_MAX_COMMENTS by likes:
  * ``search(<BV-id or video URL>)`` returns the top comments as SEPARATE docs
    (one Document per comment: content=message, author=uname, signals=like,
    date from ctime, url=the video url). This mirrors the arXiv / YouTube
    "query is itself a video ref → by-ref lookup" convenience: a free-text search
    query never matches the strict BV-ref detector, so ordinary keyword video
    search (and broad fan-out) is byte-identical to before.
  * ``fetch_url(<video>)`` folds a bounded top-comments preview onto the single
    video doc, alongside the existing transcribe hint (preserved verbatim).
"""

from __future__ import annotations

import functools
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import anyio
import httpx

from penumbra.core import cache
from penumbra.core.normalize import Document, jsonsafe, mk_signal
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

SEARCH_API = "https://api.bilibili.com/x/web-interface/search/type"
VIEW_API = "https://api.bilibili.com/x/web-interface/view"
# Legacy keyless reply endpoint. type=1 = video reply, sort=2 = by likes. The newer
# x/v2/reply/wbi/main 403s without a WBI signature + buvid activation, so we stay on
# the legacy one (still public; reachable from the buvid-bootstrapped session).
REPLY_API = "https://api.bilibili.com/x/v2/reply"
DEFAULT_TIMEOUT = 15
# Top-N comments cap — keep comment extraction bounded. The legacy reply endpoint's
# first page already returns the highest-liked comments (sort=2), so one page is plenty.
_MAX_COMMENTS = 30
# B站 expects browser-ish UA + a referer
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}


def _bili_request(url: str, params: dict, timeout: int = DEFAULT_TIMEOUT) -> httpx.Response:
    """GET a bilibili web API in a fresh session that FIRST bootstraps the buvid
    cookies by visiting bilibili.com — the search API returns 412 Precondition Failed
    without them (their anti-crawler gate; verified 2026-06). One extra GET, and search
    results are cached, so the cost is amortized."""
    with httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True) as client:
        try:
            client.get("https://www.bilibili.com/")  # sets buvid3 / buvid4 cookies
        except Exception as exc:  # noqa: BLE001
            logger.debug("bilibili cookie bootstrap failed (continuing): %s", exc)
        return client.get(url, params=params)


async def _abili_request(url: str, params: dict, timeout: int = DEFAULT_TIMEOUT) -> httpx.Response:
    """Async twin of ``_bili_request``: byte-faithful mirror of the two-GET buvid handshake in a FRESH
    per-call ``httpx.AsyncClient`` (fresh client = fresh cookie jar per call, exactly like the sync
    ``with httpx.Client(...)``). ONLY the two ``client.get`` egresses go async; the bootstrap-then-fetch
    order, the browser UA + Referer HEADERS, follow_redirects, timeout, and the best-effort bootstrap
    ``try/except`` are identical. This source keeps its OWN async client (not the shared ``http.aget*``
    pool) because the buvid cookie bootstrap + browser headers are precisely what the shared pool cannot
    provide, and bilibili's buvid cookies must not leak into the pool every other source shares."""
    async with httpx.AsyncClient(headers=HEADERS, timeout=timeout, follow_redirects=True) as client:
        try:
            await client.get("https://www.bilibili.com/")  # sets buvid3 / buvid4 cookies
        except Exception as exc:  # noqa: BLE001
            logger.debug("bilibili cookie bootstrap failed (continuing): %s", exc)
        return await client.get(url, params=params)


# ── BV-ref detection + comment fetch (the comment layer; see module docstring) ──────────
_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")


def _looks_like_video_ref(s: str) -> Optional[str]:
    """Return a BV-id IFF ``s`` is, on its own, a bilibili video URL or a bare BV-id
    (a single-token reference, not a free-text search query). Used to route
    ``search(<video ref>)`` to comment extraction without ever catching an ordinary
    keyword query: a query with whitespace is never a ref, and a bare token must match
    the ``BV`` + 10-char shape, or the URL must be on a bilibili host."""
    s = (s or "").strip()
    if not s or any(c.isspace() for c in s):
        return None  # multi-word query → ordinary video search, never comments
    host = urlparse(s).hostname or ""
    if "bilibili.com" in host or "b23.tv" in host:
        m = _BV_RE.search(s)
        return m.group(1) if m else None
    if re.fullmatch(r"BV[0-9A-Za-z]{10}", s):
        return s
    return None


def _bvid_to_aid(bvid: str) -> Optional[int]:
    """Resolve a BV-id → its numeric ``aid`` via /x/web-interface/view (the reply
    endpoint keys on aid, not BV-id). 24h-cached: a BV→aid mapping is permanent.
    None on any failure / non-zero API code."""
    key = cache.make_key("bilibili", "bvid_aid", bvid)
    cached = cache.get(key)
    if cached is not None:
        return cached or None  # cached 0/None memo means "could not resolve"
    try:
        resp = _bili_request(VIEW_API, {"bvid": bvid})
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("bilibili bvid→aid view fetch failed for %s: %s", bvid, exc)
        return None
    if data.get("code") != 0:
        logger.debug("bilibili view code %s for %s: %s", data.get("code"), bvid, data.get("message"))
        return None
    aid = (data.get("data") or {}).get("aid")
    if isinstance(aid, int) and aid > 0:
        cache.set(key, aid, ttl=86400)
        return aid
    return None


async def _abvid_to_aid(bvid: str) -> Optional[int]:
    """Async twin of ``_bvid_to_aid``: byte-faithful mirror (SAME cache key + 24h ttl, the ``code != 0``
    guard, the ``aid > 0`` check, the failure -> None contract). The disk cache get/set go OFF the loop
    (``anyio.to_thread.run_sync``); the egress swaps to ``_abili_request``; ``resp.json()`` /
    ``raise_for_status`` are pure CPU on an already-read Response, so they stay on the loop."""
    key = cache.make_key("bilibili", "bvid_aid", bvid)
    cached = await anyio.to_thread.run_sync(cache.get, key)
    if cached is not None:
        return cached or None  # cached 0/None memo means "could not resolve"
    try:
        resp = await _abili_request(VIEW_API, {"bvid": bvid})
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("bilibili bvid→aid view fetch failed for %s: %s", bvid, exc)
        return None
    if data.get("code") != 0:
        logger.debug("bilibili view code %s for %s: %s", data.get("code"), bvid, data.get("message"))
        return None
    aid = (data.get("data") or {}).get("aid")
    if isinstance(aid, int) and aid > 0:
        await anyio.to_thread.run_sync(functools.partial(cache.set, key, aid, ttl=86400))
        return aid
    return None


def _fetch_comments(aid: int, limit: int = _MAX_COMMENTS) -> list[dict]:
    """Pull up to ``limit`` (capped at _MAX_COMMENTS) top comments for a video via the
    LEGACY reply endpoint (sort=2 = by likes). Returns the raw reply dicts
    (content.message / like / member.uname / ctime / rpid). Empty list on any failure /
    non-zero code / comments disabled. 6h-cached (like counts drift but slowly)."""
    cap = max(1, min(limit, _MAX_COMMENTS))
    key = cache.make_key("bilibili", "comments", str(aid), cap)
    cached = cache.get(key)
    if cached is not None:
        return cached  # may be [] — a known "no comments / disabled" memo
    try:
        resp = _bili_request(REPLY_API, {"type": 1, "oid": aid, "sort": 2})
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("bilibili comment fetch failed for aid %s: %s", aid, exc)
        return []
    if data.get("code") != 0:
        # code 12022 = comments closed; -404 = not found; etc. Treat as "no comments".
        logger.debug("bilibili reply code %s for aid %s: %s",
                     data.get("code"), aid, data.get("message"))
        return []
    replies = (data.get("data") or {}).get("replies") or []
    if not isinstance(replies, list):
        replies = []
    # The endpoint already returns highest-liked first (sort=2); just take the cap.
    ranked = [r for r in replies if isinstance(r, dict)][:cap]
    cache.set(key, ranked, ttl=6 * 3600)
    return ranked


async def _afetch_comments(aid: int, limit: int = _MAX_COMMENTS) -> list[dict]:
    """Async twin of ``_fetch_comments``: byte-faithful mirror (SAME cap, SAME cache key + 6h ttl, the
    ``code != 0`` guard, the ``replies`` list-guard, the ``[:cap]`` slice, and the ``[]``-on-failure
    memo). The disk cache get/set go OFF the loop; the legacy reply egress swaps to ``_abili_request``."""
    cap = max(1, min(limit, _MAX_COMMENTS))
    key = cache.make_key("bilibili", "comments", str(aid), cap)
    cached = await anyio.to_thread.run_sync(cache.get, key)
    if cached is not None:
        return cached  # may be [] — a known "no comments / disabled" memo
    try:
        resp = await _abili_request(REPLY_API, {"type": 1, "oid": aid, "sort": 2})
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("bilibili comment fetch failed for aid %s: %s", aid, exc)
        return []
    if data.get("code") != 0:
        # code 12022 = comments closed; -404 = not found; etc. Treat as "no comments".
        logger.debug("bilibili reply code %s for aid %s: %s",
                     data.get("code"), aid, data.get("message"))
        return []
    replies = (data.get("data") or {}).get("replies") or []
    if not isinstance(replies, list):
        replies = []
    # The endpoint already returns highest-liked first (sort=2); just take the cap.
    ranked = [r for r in replies if isinstance(r, dict)][:cap]
    await anyio.to_thread.run_sync(functools.partial(cache.set, key, ranked, ttl=6 * 3600))
    return ranked


def _comment_to_document(reply: dict, bvid: str, aid: int, video_url: str) -> Document:
    """One legacy-reply dict → one Document (content=message, author=uname,
    signals=like, date from ctime, url=the video url)."""
    message = ((reply.get("content") or {}).get("message") or "").strip()
    member = reply.get("member") or {}
    author = member.get("uname") or None
    rpid = str(reply.get("rpid") or reply.get("rpid_str") or "")

    date = None
    ctime = reply.get("ctime")
    if isinstance(ctime, (int, float)):
        try:
            date = datetime.fromtimestamp(ctime, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            date = None

    return Document(
        source="bilibili",
        source_id=f"{bvid}:comment:{rpid}" if rpid else f"{bvid}:comment",
        url=video_url,  # a comment has no standalone URL; point at the video
        title=f"评论 by {author or '匿名'} on {bvid}",
        content=message or "(空评论)",
        author=author,
        date=date,
        signals=mk_signal("likes", reply.get("like"),
                          kind="engagement", by="bilibili/reply_like"),
        metadata={
            "kind": "comment",
            "bvid": bvid,
            "aid": aid,
            "comment_id": rpid or None,
            "like_count": reply.get("like"),
            "reply_count": reply.get("rcount") or reply.get("count"),
            "raw": jsonsafe(reply),
        },
    )


class BilibiliAdapter(BaseScrapeAdapter):
    name = "bilibili"
    needs_credentials = False
    description = ("Bilibili — Chinese academic video (论文精读, 科研 vlog, 方法论讲解); "
                   "pass a BV-id/video URL as the query to get its top comments as docs")

    cache_ttl = 1800
    # Bilibili returns server order (order=totalrank); keep it verbatim.
    rank = False

    # --------------------------------------------------------------------- search
    def search(self, query: str, limit: int = 10) -> list[Document]:
        """Routing convenience (arXiv id_list / YouTube precedent): if the query is
        itself a single bilibili video URL or bare BV-id, return that video's TOP
        COMMENTS (by likes) as separate docs. A multi-word / non-BV query never
        matches, so ordinary keyword video search (and broad fan-out) is byte-identical
        to the base template-method path."""
        bvid = _looks_like_video_ref(query)
        if bvid:
            return self._comments_search(bvid, limit)
        return super().search(query, limit)

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` -> AsyncSearchCapable. SAME routing: a bare BV-id / bilibili
        video URL query returns that video's top comments as docs (async ``_acomments_search``); any other
        query goes the base async template path (``_asearch_via``: cache round-trip OFF the loop with the
        SAME cache key as ``search``, opt-in rank on the loop). Egress via ``_araw_fetch``; mapping via the
        SAME pure-CPU ``_to_documents``, so this is behavior-identical to ``search`` on both branches."""
        bvid = _looks_like_video_ref(query)
        if bvid:
            return await self._acomments_search(bvid, limit)
        return await self._asearch_via(
            query, limit,
            afetch=lambda: self._araw_fetch(query, limit),
            abuild=lambda raw: self._to_documents(raw, query, limit))

    def _comments_search(self, bvid: str, limit: int) -> list[Document]:
        """Return up to ``limit`` top comments (by likes) for one video, each as a doc."""
        aid = _bvid_to_aid(bvid)
        if not aid:
            return []
        video_url = f"https://www.bilibili.com/video/{bvid}"
        replies = _fetch_comments(aid, limit=min(max(limit, 1), _MAX_COMMENTS))
        docs: list[Document] = []
        for r in replies[:limit]:
            try:
                docs.append(_comment_to_document(r, bvid, aid, video_url))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping bilibili comment: %s", exc)
        return docs

    async def _acomments_search(self, bvid: str, limit: int) -> list[Document]:
        """Async twin of ``_comments_search``: resolve the aid (async ``_abvid_to_aid``) then fetch the top
        comments (async ``_afetch_comments``, off-loop cache); the ``min(max(limit, 1), _MAX_COMMENTS)``
        cap, the ``replies[:limit]`` slice, and the per-comment ``_comment_to_document`` map (pure CPU, on
        the loop) are byte-identical to sync."""
        aid = await _abvid_to_aid(bvid)
        if not aid:
            return []
        video_url = f"https://www.bilibili.com/video/{bvid}"
        replies = await _afetch_comments(aid, limit=min(max(limit, 1), _MAX_COMMENTS))
        docs: list[Document] = []
        for r in replies[:limit]:
            try:
                docs.append(_comment_to_document(r, bvid, aid, video_url))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping bilibili comment: %s", exc)
        return docs

    # ------------------------------------------------------------------ hooks
    def _raw_fetch(self, query: str, limit: int) -> Optional[list]:
        """Anti-bot search call → the result list (None on failure / API error).

        Verbatim port of the old search() fetch block: the buvid-cookie-bootstrapping
        ``_bili_request``, the raise_for_status, and the ``data.code != 0`` guard.
        Returns the (possibly empty) result list on success so the base caches it,
        or None on any failure / non-zero code so the base returns [] without caching."""
        try:
            resp = _bili_request(
                SEARCH_API,
                {
                    "search_type": "video",
                    "keyword": query,
                    "page": 1,
                    "page_size": min(limit, 42),
                    "order": "totalrank",  # relevance + popularity hybrid
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bilibili search failed: %s", exc)
            return None

        if data.get("code") != 0:
            logger.warning("Bilibili API error: %s", data.get("message"))
            return None

        return (data.get("data") or {}).get("result") or []

    async def _araw_fetch(self, query: str, limit: int) -> Optional[list]:
        """Async twin of ``_raw_fetch``: byte-faithful mirror of the anti-bot search call — the SAME params
        (``search_type``/``keyword``/``page``/``page_size=min(limit, 42)``/``order=totalrank``), the
        raise_for_status, the ``code != 0`` guard, and the failure/error -> None contract (so the base
        caches a success list but not a miss). ONLY the ``_bili_request`` egress swaps to ``_abili_request``."""
        try:
            resp = await _abili_request(
                SEARCH_API,
                {
                    "search_type": "video",
                    "keyword": query,
                    "page": 1,
                    "page_size": min(limit, 42),
                    "order": "totalrank",  # relevance + popularity hybrid
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bilibili search failed: %s", exc)
            return None

        if data.get("code") != 0:
            logger.warning("Bilibili API error: %s", data.get("message"))
            return None

        return (data.get("data") or {}).get("result") or []

    def _to_documents(self, raw, query: str, limit: int) -> list[Document]:
        """Result list → Documents (verbatim per-video map, slice to limit).

        The base does NOT re-slice, so the ``results[:limit]`` cut stays here,
        preserving the exact "take first N" semantics; a malformed video is skipped
        per-record (one bad record can't sink the rest)."""
        docs: list[Document] = []
        for v in (raw or [])[:limit]:
            try:
                docs.append(self._video_to_document(v))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping Bilibili video: %s", exc)
        return docs

    # --------------------------------------------------------------- fetch_url
    def fetch_url(self, url: str) -> Optional[Document]:
        import re
        host = urlparse(url).hostname or ""
        if "bilibili.com" not in host:
            return None
        m = re.search(r"(BV[0-9A-Za-z]{10})", url)
        if not m:
            return None
        bvid = m.group(1)
        try:
            resp = _bili_request("https://api.bilibili.com/x/web-interface/view", {"bvid": bvid})
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bilibili view fetch failed: %s", exc)
            return None
        if data.get("code") != 0:
            logger.warning("Bilibili view API code %s: %s", data.get("code"), data.get("message"))
            return None
        d = data.get("data") or {}
        date = None
        if d.get("pubdate"):
            try:
                date = datetime.fromtimestamp(d["pubdate"], tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                date = None
        cover = d.get("pic") or ""
        media = [cover] if isinstance(cover, str) and cover.startswith("http") else []
        hint = ("\n\n[视频的口语干货在音频里;B 站字幕需登录 → 用 penumbra_transcribe 取本地 Whisper "
                "全文转写(免费,首次较慢、之后缓存)]")
        video_url = f"https://www.bilibili.com/video/{bvid}"
        content = (d.get("desc") or "(no description)") + hint
        aid = d.get("aid")

        metadata = {"bvid": bvid, "aid": aid, "duration": d.get("duration"),
                    "view_count": (d.get("stat") or {}).get("view"),
                    "transcribe": "penumbra_transcribe for full spoken transcript"}

        # Fold a bounded TOP-COMMENTS preview onto the single video doc (the standalone
        # per-comment docs come from search(<BV-ref>); here we just surface the discussion
        # inline so the drill-down doc carries it too). Best-effort: a comment failure
        # never sinks the video doc.
        try:
            replies = _fetch_comments(aid, limit=_MAX_COMMENTS) if isinstance(aid, int) and aid > 0 else []
        except Exception:  # noqa: BLE001
            replies = []
        if replies:
            preview_lines = []
            for r in replies:
                msg = ((r.get("content") or {}).get("message") or "").strip().replace("\n", " ")
                if not msg:
                    continue
                uname = (r.get("member") or {}).get("uname") or "匿名"
                like = r.get("like")
                like_s = f" ({like} 赞)" if isinstance(like, (int, float)) else ""
                preview_lines.append(f"- **{uname}**{like_s}: {msg}")
            if preview_lines:
                content = content + "\n\n## 热门评论\n\n" + "\n".join(preview_lines)
            metadata["comments_fetched"] = len(replies)
        else:
            metadata["comments_fetched"] = 0

        return Document(
            source="bilibili",
            source_id=bvid,
            url=video_url,
            title=d.get("title") or "(untitled)",
            content=content,
            author=(d.get("owner") or {}).get("name"),
            date=date,
            # views alone cannot say whether the comment section holds anything; stat.reply can.
            # mk_signal is None-safe, so a payload without it yields an honest None-valued signal
            # rather than a fabricated 0 (2026-07-25).
            signals={
                **mk_signal("views", (d.get("stat") or {}).get("view"),
                            kind="engagement", by="bilibili/view"),
                **mk_signal("comments", (d.get("stat") or {}).get("reply"),
                            kind="engagement", by="bilibili/stat.reply"),
            },
            media=media,
            metadata=metadata,
        )

    # ------------------------------------------------------------- health_check
    def health_check(self) -> tuple[bool, str]:
        try:
            resp = _bili_request(
                SEARCH_API,
                {"search_type": "video", "keyword": "test", "page": 1, "page_size": 1},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    return True, "OK"
                return False, f"API code {data.get('code')}: {data.get('message')}"
            return False, f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------ field mapping
    @staticmethod
    def _video_to_document(v: dict) -> Document:
        # Bilibili returns title with <em> highlight tags around matches
        import re

        title = re.sub(r"</?em[^>]*>", "", v.get("title") or "(untitled)").strip()
        bvid = v.get("bvid") or ""
        # Non-video hits (type=ketang course / 'cheese') have no BV-id; their arcurl is the real
        # link. The API now returns it ABSOLUTE (https://www.bilibili.com/cheese/play/ss...), so only
        # protocol-relative ('//...') arcurls need the https: prefix — same idiom as the cover pic below.
        arcurl = (v.get("arcurl") or "").strip()
        if arcurl.startswith("//"):
            arcurl = "https:" + arcurl
        url = f"https://www.bilibili.com/video/{bvid}" if bvid else arcurl

        # pubdate is unix timestamp
        date = None
        if v.get("pubdate"):
            try:
                date = datetime.fromtimestamp(v["pubdate"], tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                date = None

        # Video cover image (B站 returns it as a protocol-relative or absolute URL).
        media: list[str] = []
        pic = v.get("pic") or v.get("cover") or ""
        if isinstance(pic, str) and pic.strip():
            pic = pic.strip()
            if pic.startswith("//"):
                pic = "https:" + pic
            if pic.startswith("http"):
                media.append(pic)

        return Document(
            source="bilibili",
            source_id=bvid or str(v.get("aid", "")),
            url=url,
            title=title,
            content=v.get("description") or "(no description)",
            author=v.get("author"),
            date=date,
            signals={   # play = views; video_review = the danmaku/comment count on a search hit
                **mk_signal("views", v.get("play"), kind="engagement", by="bilibili/play"),
                **mk_signal("comments", v.get("review") or v.get("video_review"),
                            kind="engagement", by="bilibili/review"),
            },
            tags=(v.get("tag") or "").split(",") if v.get("tag") else [],
            media=media,
            metadata={
                "bvid": bvid,
                "duration": v.get("duration"),
                "play_count": v.get("play"),
                "like_count": v.get("like"),
                "favorite_count": v.get("favorites"),
                "danmaku_count": v.get("video_review"),
                "subtitle": v.get("subtitle"),  # B站 sometimes returns subtitle preview text
                "raw": jsonsafe(v),
            },
        )
