"""Baidu Tieba (百度贴吧): China's largest topical BBS, via the mobile JSON endpoints.

Tieba is the biggest interest-based forum in the Chinese internet: every topic gets a
"吧" (a forum), and the popular ones are enormous (考研 / postgrad-exam alone is ~43M
posts, 5.7M followers). It is the long-tail community voice OmniSeek otherwise misses for
CN-language topics: exam prep, schools, games, fandoms, regional life, niche hobbies.

The DESKTOP site 403s automation, but the MOBILE JSON endpoints answer keylessly with a
plain mobile User-Agent (probed working from the US-LA mini egress, 2026-06-17):

  * Threads (the gold): GET https://tieba.baidu.com/mo/q/search/thread?word=<q>&pn=1
        -> {"no":0,"data":{"post_list":[{tid,title,content,time,user,post_num,
            like_num,share_num,media,forum_name,forum_id,pb_url}, ...]}}
        post_num is the reply count; content is the thread's opening abstract.
  * Forums (the STRUCTURE): GET https://tieba.baidu.com/mo/q/search/forum?word=<q>
        -> {"no":0,"data":{"exactMatch":{forum_id,forum_name,post_num_ori,
            concern_num_ori,intro,...}, "fuzzyMatch":[<forum>, ...]}}
        post_num_ori / concern_num_ori are the forum's total-post / follower counts.

We emit BOTH: a forum doc for the exact-name match (so the agent sees the community's
size + blurb: the STRUCTURE), then a thread doc per search-matched thread (the UNWALL of
the actual discussion). Forum doc first, then threads, sliced to ``limit`` total.

Canonical thread page: https://tieba.baidu.com/p/<tid> (the pb_url carries an extra
jump_tieba_native native-app param we strip). Canonical forum page:
https://tieba.baidu.com/f?kw=<forum_name>.

BaseScrapeAdapter (template method): the cache check / atomic set_docs / self-registration
ritual lives in the base; this adapter fills the two hooks (_raw_fetch = the two mobile GETs,
_to_documents = the forum/thread -> Document maps). ``rank`` stays default-False:
search/thread returns server-relevance order for the query, which we keep byte-faithful.

These are public mobile endpoints with a mobile UA, NOT the open-API ``http`` helper's UA
contract (Baidu gates the OmniSeek UA), so this adapter uses httpx directly with a mobile
User-Agent. No new dependency: httpx is already a core dep.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from omniseek.core import http
from omniseek.core.normalize import Document, jsonsafe, mk_signal
from omniseek.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

THREAD_URL = "https://tieba.baidu.com/mo/q/search/thread"
FORUM_URL = "https://tieba.baidu.com/mo/q/search/forum"
THREAD_PAGE = "https://tieba.baidu.com/p"          # /<tid>
FORUM_PAGE = "https://tieba.baidu.com/f?kw="       # +<forum_name>
TIMEOUT = 15

# A plain mobile User-Agent: the desktop site 403s, the mobile JSON endpoints answer with
# this. The shared http.USER_AGENT (OmniSeek/0.1) is gated by Baidu, so we cannot route
# through the open-API helper here (per its module docstring: walled/anti-bot sources keep
# their own headers).
_MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)


class TiebaAdapter(BaseScrapeAdapter):
    name = "tieba"
    needs_credentials = False
    description = "Baidu Tieba (百度贴吧): China's largest topical BBS; CN-community threads + forum structure via the keyless mobile JSON endpoints"
    cache_ttl = 900

    # routing facets (the router reads these class attrs; do NOT touch facets.json)
    kind = "lookup"
    domains = ["community"]
    modes = ["UNWALL", "STRUCTURE"]
    regions = ["cn"]

    # ── network ─────────────────────────────────────────────────────────────
    def _raw_fetch(self, query: str, limit: int) -> Optional[dict]:
        """Two mobile GETs: the thread search (the gold) plus the forum search (the
        community-size STRUCTURE). Returns a bundle dict; None only if BOTH fail (the
        thread search failing alone is the real miss, but we still emit a forum doc if
        that one answered). httpx directly with a mobile UA (Baidu gates the shared UA)."""
        threads = self._get_json(THREAD_URL, {"word": query, "pn": 1})
        forums = self._get_json(FORUM_URL, {"word": query})
        if threads is None and forums is None:
            return None
        return {"threads": threads, "forums": forums}

    @staticmethod
    def _get_json(url: str, params: dict) -> Optional[dict]:
        """GET + parse JSON with the mobile UA. None on any failure (the adapter contract).
        Builds the query string with quote() so a CN-character query is encoded correctly."""
        qs = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
        full = f"{url}?{qs}"
        try:
            resp = httpx.get(
                full,
                headers={"User-Agent": _MOBILE_UA},
                follow_redirects=True,
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 (failure -> None is the adapter contract)
            logger.warning("tieba: GET %s failed: %s", url, exc)
            return None
        # Tieba envelope: {"no":0,"error":"success","data":{...}}; no != 0 is an API error.
        if not isinstance(data, dict) or data.get("no") not in (0, "0", None):
            logger.warning("tieba: %s returned error envelope no=%s", url, data.get("no") if isinstance(data, dict) else "?")
            return None
        return data

    # ── native async (S4) ────────────────────────────────────────────────────
    @staticmethod
    async def _aget_json(url: str, params: dict) -> Optional[dict]:
        """Async twin of ``_get_json``: byte-faithful mirror (SAME quote()-built query string, mobile-UA
        header, timeout, the ``no != 0`` envelope check, and the failure -> None contract). ONLY the raw
        ``httpx.get(...).json()`` egress swaps to the shared async leaf ``await http.aget_json`` (this is a
        standard keyless JSON GET, so it needs nothing httpx.aget cannot do; the leaf adds the shared pool +
        SSRF guard + cache_only + 30MB cap + diag.note evidence tap for free, and its pooled async client
        already carries follow_redirects=True). The mobile UA overrides the shared OmniSeek UA via the
        headers kwarg (Baidu gates the shared UA)."""
        qs = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
        full = f"{url}?{qs}"
        data = await http.aget_json(full, headers={"User-Agent": _MOBILE_UA}, timeout=TIMEOUT)
        if data is None:
            return None  # request/parse failure (http owns the log + diag.note evidence tap)
        # Tieba envelope: {"no":0,"error":"success","data":{...}}; no != 0 is an API error.
        if not isinstance(data, dict) or data.get("no") not in (0, "0", None):
            logger.warning("tieba: %s returned error envelope no=%s", url, data.get("no") if isinstance(data, dict) else "?")
            return None
        return data

    async def _araw_fetch(self, query: str, limit: int) -> Optional[dict]:
        """Async twin of ``_raw_fetch``: byte-faithful mirror of the two mobile GETs (thread search then
        forum search) and the both-failed -> None contract; each egress swaps to ``_aget_json``. Sequential
        awaits mirror the sync order (there is no ThreadPoolExecutor here to convert to gather): the same
        two GETs the sync path issues, gently."""
        threads = await self._aget_json(THREAD_URL, {"word": query, "pn": 1})
        forums = await self._aget_json(FORUM_URL, {"word": query})
        if threads is None and forums is None:
            return None
        return {"threads": threads, "forums": forums}

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` -> AsyncSearchCapable. Shares the base async cache round-trip
        (``_asearch_via``: cache get/set off the loop, SAME cache key as ``search``); egress via
        ``_araw_fetch``; mapping via the SAME pure-CPU ``_to_documents`` (byte-identical to ``search``)."""
        return await self._asearch_via(
            query, limit,
            afetch=lambda: self._araw_fetch(query, limit),
            abuild=lambda raw: self._to_documents(raw, query, limit))

    # ── parse ───────────────────────────────────────────────────────────────
    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, dict):
            return []
        docs: list[Document] = []

        # 1) the forum doc (community structure) for the exact-name match, if any.
        forum_doc = self._forum_doc(raw.get("forums"))
        if forum_doc is not None:
            docs.append(forum_doc)

        # 2) the thread docs (the actual discussions), filling the rest of the budget.
        threads = (((raw.get("threads") or {}).get("data") or {}).get("post_list")) or []
        for t in threads:
            if len(docs) >= limit:
                break
            if not isinstance(t, dict):
                continue
            doc = self._thread_doc(t)
            if doc is not None:
                docs.append(doc)
        return docs[:limit]

    def _forum_doc(self, forums_payload: Any) -> Optional[Document]:
        """The exactMatch forum -> a STRUCTURE doc (size + follower count + blurb)."""
        if not isinstance(forums_payload, dict):
            return None
        data = forums_payload.get("data") or {}
        fm = data.get("exactMatch")
        if not isinstance(fm, dict) or not fm.get("forum_name"):
            return None

        name = str(fm.get("forum_name_show") or fm.get("forum_name"))
        fid = fm.get("forum_id")
        post_num_ori = _as_int(fm.get("post_num_ori"))
        concern = _as_int(fm.get("concern_num_ori"))
        url = FORUM_PAGE + quote(str(fm.get("forum_name")))

        intro = _clean_text(fm.get("intro"))
        slogan = _clean_text(fm.get("slogan"))
        lines = [f"百度贴吧「{name}吧」: 一个话题社区 (forum)."]
        if slogan:
            lines.append(f"标语: {slogan}")
        if intro:
            lines.append(intro)
        if post_num_ori is not None:
            lines.append(f"累计帖子数 (posts): {post_num_ori}")
        if concern is not None:
            lines.append(f"关注人数 (followers): {concern}")
        content = "\n\n".join(lines)

        # the forum's own engagement signals: total post count + follower count.
        signals = mk_signal("posts", post_num_ori, kind="engagement", by="tieba/forum/post_num")
        signals.update(mk_signal("followers", concern, kind="engagement", by="tieba/forum/concern_num"))

        return Document(
            source=self.name,
            source_id=f"forum/{fid}" if fid is not None else f"forum/{name}",
            url=url,
            title=f"{name}吧 (百度贴吧)",
            content=content,
            signals=signals,
            tags=["forum", "贴吧"],
            metadata={
                "doc_type": "forum",
                "forum_id": fid,
                "forum_name": fm.get("forum_name"),
                "raw": jsonsafe(fm),
            },
        )

    def _thread_doc(self, t: dict) -> Optional[Document]:
        """One search-matched thread -> a UNWALL doc (title, opening abstract, replies)."""
        tid = t.get("tid")
        if not tid:
            return None
        title = _clean_text(t.get("title")) or "(无标题)"
        content = _clean_text(t.get("content"))

        user = t.get("user") or {}
        author = None
        if isinstance(user, dict):
            author = user.get("show_nickname") or user.get("user_name") or None

        date = _epoch_dt(t.get("create_time") or t.get("time"))
        url = _canon_thread_url(t.get("pb_url"), tid)

        # the discussion's engagement: reply count is the primary signal; likes/shares too.
        signals = mk_signal("replies", t.get("post_num"), kind="engagement", by="tieba/thread/post_num")
        signals.update(mk_signal("likes", t.get("like_num"), kind="engagement", by="tieba/thread/like_num"))
        signals.update(mk_signal("shares", t.get("share_num"), kind="engagement", by="tieba/thread/share_num"))
        # video threads carry a play_count; only record it when it is a real number.
        play = _as_int(t.get("play_count"))
        if play:
            signals.update(mk_signal("plays", play, kind="engagement", by="tieba/thread/play_count"))

        forum_name = t.get("forum_name")
        tags = ["thread"]
        if forum_name:
            tags.append(str(forum_name))

        return Document(
            source=self.name,
            source_id=str(tid),
            url=url,
            title=title,
            content=content,
            author=author,
            date=date,
            signals=signals,
            media=_thread_media(t.get("media")),
            tags=tags,
            metadata={
                "doc_type": "thread",
                "tid": str(tid),
                "forum_id": t.get("forum_id"),
                "forum_name": forum_name,
                "raw": jsonsafe(t),
            },
        )

    def fetch_url(self, url: str) -> Optional[Document]:
        """This source does not claim arbitrary thread URLs (the search endpoint is the
        entry point; a single thread's full replies need the pb page, out of scope here)."""
        return None


# ── helpers ─────────────────────────────────────────────────────────────────
def _as_int(v: Any) -> Optional[int]:
    """Coerce to int (the *_ori counts are ints; post_num strings like '4303W' are NOT
    used here, we only take the numeric originals). None if not a clean number."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _clean_text(s: Any) -> str:
    """Trim a string field; non-strings (incl. None) become ''. Tieba sometimes carries
    raw <br> in intro: soften them to newlines so the prose reads cleanly."""
    if not isinstance(s, str):
        return ""
    return s.replace("&lt;br&gt;", "\n").replace("<br>", "\n").strip()


def _epoch_dt(v: Any) -> Optional[datetime]:
    """Tieba times are unix epoch seconds. Return a tz-aware UTC datetime, None if absent."""
    iv = _as_int(v)
    if not iv or iv <= 0:
        return None
    try:
        return datetime.fromtimestamp(iv, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _canon_thread_url(pb_url: Any, tid: Any) -> str:
    """Canonical thread page is https://tieba.baidu.com/p/<tid>. The API pb_url tacks on a
    jump_tieba_native native-app query we strip; fall back to building from tid."""
    if isinstance(pb_url, str) and pb_url.startswith("http"):
        parts = urlsplit(pb_url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return f"{THREAD_PAGE}/{tid}"


def _thread_media(media: Any) -> list[str]:
    """Pull image/video URLs from the thread's media list (each item is a dict with a
    big_pic / water_pic / small_pic for pics). Best-effort, deduped, order-preserving."""
    out: list[str] = []
    if not isinstance(media, list):
        return out
    seen: set[str] = set()
    for m in media:
        if not isinstance(m, dict):
            continue
        for k in ("big_pic", "water_pic", "small_pic", "vpic", "video_url"):
            u = m.get(k)
            if isinstance(u, str) and u.startswith("http") and u not in seen:
                seen.add(u)
                out.append(u)
                break
    return out


# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
