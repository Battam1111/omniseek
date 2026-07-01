"""豆瓣小组 (Douban groups): a grassroots overseas-study / immigration / life discussion graph.

Douban (douban.com) hosts China's densest grassroots-community graph: 小组 (groups) are
member-run forums where overseas-study, immigration, and diaspora-life experience is shared
candidly (the lived, unofficial counterpart to the institutional sources the eye already
covers). It fills a COMMUNITY gap: the first-person "我在德国/加拿大/新加坡的真实生活" thread,
the group that organizes around a destination, the answer to "哪个组在讨论 X".

Douban is server-rendered HTML and keyless-ish, but anti-bot: it gates by IP risk + UA. We
hit the public group-search endpoint with browser-ish headers and parse the SSR HTML with bs4.
Two facets of the same query, combined into one result set:

  * GROUPS  (GET /group/search?cat=1019&q=<q>): the community STRUCTURE (a group + member
              count + blurb). Parsed from ``div.groups div.result`` blocks.
  * TOPICS  (GET /group/search?cat=1013&q=<q>): the discussion content UNWALLed (a thread
              title + which group + when + reply count). Parsed from ``table.olt tr.pl`` rows.

Per the http module's contract, anti-bot sources do NOT route through ``penumbra.core.http``
(the shared client carries one fixed UA + no per-site headers); like the bilibili adapter we
own a bespoke ``httpx.Client`` with a real-browser UA + Referer + Accept-Language. Verified
2026-06-17 from the mini (US-LA egress): both endpoints return HTTP 200 SSR HTML, not a 403 /
captcha / risk page, so the US IP is currently NOT gated for group search.

BaseScrapeAdapter (template method): the cache check / atomic set_docs / self-registration
ritual lives in the base; this adapter owns the two-endpoint anti-bot fetch (``_raw_fetch``)
and the dual HTML→doc parse (``_to_documents``). ``rank`` stays default-False: groups come back
in Douban's own relevance order and topics newest-first, and the eye's ranked search re-scores
across sources when it needs a unified relevance order.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Optional

import httpx
from bs4 import BeautifulSoup

from penumbra.core.normalize import PolarisDocument, jsonsafe, mk_signal
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

BASE = "https://www.douban.com"
SEARCH_URL = f"{BASE}/group/search"
CAT_GROUPS = "1019"  # the 小组 (groups) tab: community structure
CAT_TOPICS = "1013"  # the 讨论 (discussions) tab: thread content
DEFAULT_TIMEOUT = 15

# Douban gates by IP risk + UA; a real-browser UA + Referer + zh Accept-Language passes the
# current keyless gate (anti-bot adapters keep bespoke headers, NOT the shared http client).
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.douban.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# A risk / captcha interstitial (rather than the search page) is short and screams "异常"/"sec".
# If a response is BOTH tiny AND carries one of these markers, treat it as a block, not results.
_BLOCK_MARKERS = ("有异常请求", "验证码", "sec.douban.com", "异常操作")


def _fetch_html(params: dict, timeout: int = DEFAULT_TIMEOUT) -> Optional[str]:
    """GET the group-search page with browser-ish headers; return HTML text (None on failure
    or on a detected anti-bot interstitial). One client per call mirrors the bilibili adapter."""
    try:
        with httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True) as client:
            resp = client.get(SEARCH_URL, params=params)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 (failure -> None is the adapter contract)
        logger.warning("Douban search failed (%s): %s", params, exc)
        return None
    html = resp.text or ""
    if len(html) < 4000 and any(m in html for m in _BLOCK_MARKERS):
        logger.warning("Douban returned an anti-bot interstitial for %s (len=%d)", params, len(html))
        return None
    return html


def _member_count(info_text: str) -> Optional[int]:
    """Pull the leading member count out of a group's info line, e.g. '92280个组员在此聚集' → 92280."""
    m = re.search(r"([\d,]+)\s*个", info_text or "")
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _reply_count(text: str) -> Optional[int]:
    """Pull the reply count out of a topic row's reply cell, e.g. '6回复' → 6."""
    m = re.search(r"(\d+)", text or "")
    return int(m.group(1)) if m else None


def _parse_dt(s: str) -> Optional[datetime]:
    """Douban topic td-time title is a full local timestamp 'YYYY-MM-DD HH:MM:SS'."""
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _group_id(url: str) -> Optional[str]:
    m = re.search(r"/group/(\d+)/?", url or "")
    return m.group(1) if m else None


def _topic_id(url: str) -> Optional[str]:
    m = re.search(r"/group/topic/(\d+)", url or "")
    return m.group(1) if m else None


class DoubanGroupsAdapter(BaseScrapeAdapter):
    name = "douban_groups"
    needs_credentials = False
    description = "豆瓣小组: China grassroots community graph for overseas-study/immigration/diaspora life (groups + discussion threads, SSR HTML)"
    cache_ttl = 1800  # community pages drift slowly; a 30-min cache spares the anti-bot gate

    # routing facets (the router reads these class attrs; do NOT touch facets.json)
    kind = "lookup"
    domains = ["community", "immigration"]
    modes = ["UNWALL", "STRUCTURE"]
    regions = ["cn"]

    # ------------------------------------------------------------------ hooks
    def _raw_fetch(self, query: str, limit: int) -> Optional[dict]:
        """Hit BOTH tabs (groups = STRUCTURE, topics = UNWALL) and hand the HTML to the parser.
        Returns None only if BOTH endpoints fail (so the base degrades to []); a single-tab
        failure still yields the other tab's docs."""
        params_groups = {"cat": CAT_GROUPS, "q": query}
        params_topics = {"cat": CAT_TOPICS, "q": query, "sort": "relevance"}
        groups_html = _fetch_html(params_groups)
        topics_html = _fetch_html(params_topics)
        if groups_html is None and topics_html is None:
            return None
        return {"groups": groups_html, "topics": topics_html}

    def _to_documents(self, raw: dict, query: str, limit: int) -> list[PolarisDocument]:
        """Parse both tabs into docs. Groups first (the community structure the agent can join),
        then topics (the discussion content); each side gets roughly half the limit so neither
        starves the other, and the combined list is sliced to ``limit``."""
        if not isinstance(raw, dict):
            return []
        half = max(1, limit // 2)

        group_docs: list[PolarisDocument] = []
        if raw.get("groups"):
            group_docs = self._parse_groups(raw["groups"], half + (limit - 2 * half))

        topic_docs: list[PolarisDocument] = []
        if raw.get("topics"):
            topic_docs = self._parse_topics(raw["topics"], limit)  # may backfill if few groups

        combined = group_docs + topic_docs
        return combined[:limit]

    # ------------------------------------------------------------------ parsers
    def _parse_groups(self, html: str, limit: int) -> list[PolarisDocument]:
        soup = BeautifulSoup(html, "lxml")
        docs: list[PolarisDocument] = []
        for result in soup.select("div.groups div.result"):
            try:
                doc = self._group_to_doc(result)
            except Exception as exc:  # noqa: BLE001 (one malformed block is skipped, not fatal)
                logger.debug("Skipping malformed Douban group block: %s", exc)
                continue
            if doc is not None:
                docs.append(doc)
            if len(docs) >= limit:
                break
        return docs

    def _parse_topics(self, html: str, limit: int) -> list[PolarisDocument]:
        soup = BeautifulSoup(html, "lxml")
        docs: list[PolarisDocument] = []
        for row in soup.select("table.olt tr.pl"):
            try:
                doc = self._topic_to_doc(row)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping malformed Douban topic row: %s", exc)
                continue
            if doc is not None:
                docs.append(doc)
            if len(docs) >= limit:
                break
        return docs

    # ------------------------------------------------------------------ field mapping
    def _group_to_doc(self, result) -> Optional[PolarisDocument]:
        link = result.select_one("div.title h3 a") or result.select_one("div.pic a")
        if link is None:
            return None
        url = (link.get("href") or "").strip()
        gid = _group_id(url)
        if not gid:
            return None
        name = link.get_text(strip=True) or link.get("title") or "(unnamed group)"

        info_el = result.select_one("div.info")
        info_text = info_el.get_text(strip=True) if info_el else ""
        members = _member_count(info_text)

        desc_el = result.select_one("div.content p") or result.select_one("div.content > div")
        desc = desc_el.get_text(" ", strip=True) if desc_el else ""

        content_lines = ["豆瓣小组 (Douban group)."]
        if info_text:
            content_lines.append(info_text)
        if desc:
            content_lines.append(desc)
        content_lines.append(f"Group page: {url}")
        content = "\n\n".join(content_lines)

        img_el = result.select_one("div.pic img")
        media = []
        img_src = (img_el.get("src") if img_el else "") or ""
        if img_src.startswith("http"):
            media.append(img_src)

        return PolarisDocument(
            source=self.name,
            source_id=f"g{gid}",
            url=url,
            title=name,
            content=content,
            signals=mk_signal("members", members, kind="engagement", by="douban/group_members"),
            tags=["group"],
            media=media,
            metadata={
                "kind": "group",
                "group_id": gid,
                "member_count": members,
                "raw": jsonsafe({"name": name, "url": url, "info": info_text, "desc": desc}),
            },
        )

    def _topic_to_doc(self, row) -> Optional[PolarisDocument]:
        subj = row.select_one("td.td-subject a")
        if subj is None:
            return None
        url = (subj.get("href") or "").strip()
        tid = _topic_id(url)
        if not tid:
            return None
        # Strip the analytics query string (?_spm_id=...) off the canonical topic URL.
        clean_url = url.split("?", 1)[0]
        title = subj.get_text(strip=True) or subj.get("title") or "(untitled topic)"

        time_el = row.select_one("td.td-time")
        date = _parse_dt(time_el.get("title") if time_el else "")

        reply_el = row.select_one("td.td-reply")
        replies = _reply_count(reply_el.get_text(strip=True) if reply_el else "")

        # The last td holds the GROUP the topic was posted in (a link: name + group URL).
        tds = row.find_all("td")
        group_link = tds[-1].find("a") if tds else None
        group_name = group_link.get_text(strip=True) if group_link else None
        group_url = (group_link.get("href") or "").strip() if group_link else None

        content_lines = ["豆瓣小组讨论 (Douban group discussion thread)."]
        if group_name:
            content_lines.append(f"发布于小组：{group_name}" + (f" ({group_url})" if group_url else ""))
        content_lines.append("Open the URL for the full thread + replies (Douban shows only the title in search).")
        content_lines.append(f"Topic page: {clean_url}")
        content = "\n\n".join(content_lines)

        return PolarisDocument(
            source=self.name,
            source_id=f"t{tid}",
            url=clean_url,
            title=title,
            content=content,
            author=group_name,  # the originating group is the closest "author"/venue Douban exposes here
            date=date,
            signals=mk_signal("replies", replies, kind="engagement", by="douban/topic_replies"),
            tags=["topic"],
            metadata={
                "kind": "topic",
                "topic_id": tid,
                "group_name": group_name,
                "group_url": group_url,
                "reply_count": replies,
                "raw": jsonsafe({"title": title, "url": clean_url, "group": group_name,
                                 "group_url": group_url, "replies": replies}),
            },
        )

    # ------------------------------------------------------------------ health
    def health_check(self) -> tuple[bool, str]:
        """Liveness probe: a trivial group-search GET must come back as parseable SSR HTML
        (not an anti-bot interstitial). Confirms both the egress and the gate are open."""
        html = _fetch_html({"cat": CAT_GROUPS, "q": "test"}, timeout=10)
        if html is None:
            return False, "fetch returned nothing (HTTP error or anti-bot interstitial)"
        if "豆瓣" not in html:
            return False, "unexpected page (no 豆瓣 marker): possible block/redirect"
        return True, "OK"


# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
