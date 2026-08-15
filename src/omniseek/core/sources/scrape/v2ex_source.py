"""V2EX (www.v2ex.com): the CN tech / 润学 forum, via its keyless public topics JSON.

V2EX is the Chinese-language forum where developers, overseas-job seekers and would-be
emigrants talk shop: 职场 grievances, 外企 remote gigs, 移民 (润) logistics, 海外留学. It
is exactly the 润学 / 外企-remote / 润→加拿大 demographic the eye otherwise misses (Reddit
is EN, the CN paper/patent sources are formal, tieba is mass-consumer). This adapter taps
the community's own words.

V2EX has NO keyless per-QUERY search API (site search is walled behind SoV2EX / login), but
its legacy topics API answers keylessly with a plain browser UA (probed working 2026-07-10):

  * Per-node latest topics: GET https://www.v2ex.com/api/topics/show.json?node_name=<slug>
        -> [ {id, title, url, content, content_rendered, created, last_touched, replies,
              member:{username,...}, node:{name,title,topics,...}}, ... ]  (the latest ~10)

So there is no {query} to substitute: the endpoint is per-NODE, not per-query. We therefore
fan out over a curated set of on-telos nodes (职场 / 求职 / 酷工作 / 远程工作 / 移民 /
海外留学), merge their latest topics, and let the shared BM25 scorer (``rank = True``) order
and filter them by the caller's query. A term-less query keeps the merged latest order (a
MONITOR-style community pulse across the 润学 nodes); a query with terms returns only the
matching topics, best-first. This is the tieba multi-GET pattern (fan out, merge, emit), not
a single-endpoint declarative row, which is why it is coded.

BaseScrapeAdapter (template method): the cache check / atomic set_docs / shared BM25 rank /
self-registration ritual lives in the base; this adapter fills the two hooks (_raw_fetch =
the per-node GETs merged, _to_documents = topic -> Document). Canonical topic page is
the API's own ``url`` (https://www.v2ex.com/t/<id>); node page is https://www.v2ex.com/go/<slug>.

These are public endpoints reachable with a plain Chrome UA, so this adapter uses httpx
directly (no credentials, no CDP).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import httpx

from omniseek.core import http
from omniseek.core.normalize import Document, jsonsafe, mk_signal
from omniseek.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

SHOW_URL = "https://www.v2ex.com/api/topics/show.json"   # ?node_name=<slug>
NODE_PAGE = "https://www.v2ex.com/go/"                    # +<slug>
TIMEOUT = 15

# The curated on-telos node set (slug -> a short label for logging only; the live node title
# comes from the payload). Chosen for the 润学 / 外企-remote / 润→加拿大 demographic, tightest
# first: 职场 grievances, 求职 hunting, 酷工作 postings (incl. overseas/remote), 远程工作,
# 移民 (润) logistics, 海外留学. Broad tech nodes (programmer / qna / invest) are left out to
# keep the source's identity on-telos; the BM25 filter already handles query relevance.
NODES: tuple[str, ...] = ("career", "cv", "jobs", "remote", "immigration", "global")

# A plain desktop Chrome UA: the API answers keylessly with this (the shared OmniSeekEye UA is
# not needed here, but V2EX does gate obviously-empty UAs, so we send a real browser one).
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class V2exAdapter(BaseScrapeAdapter):
    name = "v2ex"
    needs_credentials = False
    description = "V2EX (www.v2ex.com): the CN tech / 润学 forum; 职场 / 求职 / 远程工作 / 移民 / 海外留学 community threads via the keyless topics JSON"
    cache_ttl = 900

    # OPT-IN BM25: the endpoint is per-node (no server-side query relevance), so we score the
    # merged multi-node feed locally against the query (term-less query keeps latest order).
    rank = True

    # routing facets (the router reads these class attrs; do NOT touch facets.json)
    kind = "lookup"
    domains = ["community", "career"]
    modes = ["UNWALL", "MONITOR"]
    regions = ["cn"]

    # -- network ---------------------------------------------------------------
    def _raw_fetch(self, query: str, limit: int) -> Optional[list]:
        """Fan out over the curated nodes, merge their latest topics into one list (deduped by
        topic id, order-preserving). Returns None only if EVERY node fetch failed (that is the
        real miss); a single node 404/timeout is tolerated and just contributes nothing."""
        merged: list[dict] = []
        seen: set[Any] = set()
        any_ok = False
        for slug in NODES:
            topics = self._get_topics(slug)
            if topics is None:
                continue
            any_ok = True
            for t in topics:
                if not isinstance(t, dict):
                    continue
                tid = t.get("id")
                if tid in seen:
                    continue
                seen.add(tid)
                merged.append(t)
        if not any_ok:
            return None
        return merged

    @staticmethod
    def _get_topics(slug: str) -> Optional[list]:
        """GET one node's latest topics. None on any failure (the adapter contract). V2EX
        returns a JSON list on success, or a dict error envelope for an unknown node."""
        url = f"{SHOW_URL}?node_name={quote(slug)}"
        try:
            resp = httpx.get(
                url,
                headers={"User-Agent": _UA},
                follow_redirects=True,
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 (failure -> None is the adapter contract)
            logger.warning("v2ex: GET node=%s failed: %s", slug, exc)
            return None
        if not isinstance(data, list):
            # unknown node / rate-limit envelope comes back as a dict, not a topic list.
            logger.warning("v2ex: node=%s returned non-list payload", slug)
            return None
        return data

    # -- native async (S4) -----------------------------------------------------
    @staticmethod
    async def _aget_topics(slug: str) -> Optional[list]:
        """Async twin of ``_get_topics``: byte-faithful mirror (same URL, Chrome-UA header,
        timeout, and the non-list -> None contract). ONLY the raw ``httpx.get(...).json()``
        egress swaps to the shared async leaf ``await http.aget_json`` (this is a standard
        keyless JSON GET, so it needs nothing httpx.aget cannot do; the leaf adds the shared
        pool + SSRF guard + cache_only + 30MB cap + diag.note evidence tap for free, and the
        pooled async client already carries follow_redirects=True). The Chrome UA overrides the
        shared OmniSeekEye UA via the headers kwarg (V2EX gates obviously-empty UAs)."""
        url = f"{SHOW_URL}?node_name={quote(slug)}"
        data = await http.aget_json(url, headers={"User-Agent": _UA}, timeout=TIMEOUT)
        if data is None:
            return None  # request/parse failure (http owns the log + diag.note evidence tap)
        if not isinstance(data, list):
            # unknown node / rate-limit envelope comes back as a dict, not a topic list.
            logger.warning("v2ex: node=%s returned non-list payload", slug)
            return None
        return data

    async def _araw_fetch(self, query: str, limit: int) -> Optional[list]:
        """Async twin of ``_raw_fetch``: byte-faithful mirror of the per-node fan-out (same
        NODES order, id-dedup, order-preserving merge, and any_ok / None-return contract);
        each node's egress swaps to ``_aget_topics``. Sequential awaits mirror the sync loop
        (there is no ThreadPoolExecutor here to convert to gather) — one GET per node, gently."""
        merged: list[dict] = []
        seen: set[Any] = set()
        any_ok = False
        for slug in NODES:
            topics = await self._aget_topics(slug)
            if topics is None:
                continue
            any_ok = True
            for t in topics:
                if not isinstance(t, dict):
                    continue
                tid = t.get("id")
                if tid in seen:
                    continue
                seen.add(tid)
                merged.append(t)
        if not any_ok:
            return None
        return merged

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` -> AsyncSearchCapable. Shares the base async cache
        round-trip (``_asearch_via``: cache get/set off the loop); egress via ``_araw_fetch``;
        mapping via the SAME pure-CPU ``_to_documents`` (byte-identical results to ``search``)."""
        return await self._asearch_via(
            query, limit,
            afetch=lambda: self._araw_fetch(query, limit),
            abuild=lambda raw: self._to_documents(raw, query, limit))

    # -- parse -----------------------------------------------------------------
    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, list):
            return []
        docs: list[Document] = []
        for t in raw:
            if not isinstance(t, dict):
                continue
            doc = self._topic_doc(t)
            if doc is not None:
                docs.append(doc)
        # do NOT slice here: the base applies BM25 rank (rank=True) over the full merged set,
        # then the caller's own top-k slicing takes limit. Returning all lets the scorer choose.
        return docs

    def _topic_doc(self, t: dict) -> Optional[Document]:
        """One V2EX topic -> a UNWALL doc (title, opening post, node context, reply count)."""
        tid = t.get("id")
        if not tid:
            return None
        title = _clean_text(t.get("title")) or "(无标题)"
        content = _clean_text(t.get("content"))

        member = t.get("member") or {}
        author = member.get("username") if isinstance(member, dict) else None

        date = _epoch_dt(t.get("created"))
        url = t.get("url") or f"https://www.v2ex.com/t/{tid}"

        node = t.get("node") or {}
        node_name = node.get("name") if isinstance(node, dict) else None
        node_title = node.get("title") if isinstance(node, dict) else None
        node_topics = _as_int(node.get("topics")) if isinstance(node, dict) else None

        # the discussion's engagement: reply count is the primary signal.
        signals = mk_signal("replies", t.get("replies"), kind="engagement", by="v2ex/topic/replies")
        # node size (total topics in the node) as a light STRUCTURE signal on the community.
        if node_topics is not None:
            signals.update(mk_signal("node_topics", node_topics, kind="structure", by="v2ex/node/topics"))

        tags = ["topic", "v2ex"]
        if node_title:
            tags.append(str(node_title))

        return Document(
            source=self.name,
            source_id=str(tid),
            url=str(url),
            title=title,
            content=content,
            author=author,
            date=date,
            signals=signals,
            tags=tags,
            metadata={
                "doc_type": "topic",
                "tid": str(tid),
                "node_name": node_name,
                "node_title": node_title,
                "last_touched": t.get("last_touched"),
                "raw": jsonsafe(t),
            },
        )

    def fetch_url(self, url: str) -> Optional[Document]:
        """This source does not claim arbitrary topic URLs (the node feeds are the entry
        point; a single topic's full replies need the /replies API, out of scope here)."""
        return None

    def health_check(self) -> tuple[bool, str]:
        """Light probe: hit ONE node (career) rather than the full 6-node fan-out, so a
        health poll stays cheap. Live if it returns a topic list."""
        topics = self._get_topics("career")
        if topics is None:
            return False, "career node fetch returned nothing"
        return True, f"OK ({len(topics)} topics)"


# -- helpers -------------------------------------------------------------------
def _as_int(v: Any) -> Optional[int]:
    """Coerce to int; None if not a clean number. (V2EX counts are ints already.)"""
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
    """Trim a string field; non-strings (incl. None) become ''. V2EX ``content`` is already
    plain text (``content_rendered`` is the HTML twin, which we do not use)."""
    if not isinstance(s, str):
        return ""
    return s.strip()


def _epoch_dt(v: Any) -> Optional[datetime]:
    """V2EX times are unix epoch seconds. Return a tz-aware UTC datetime, None if absent."""
    iv = _as_int(v)
    if not iv or iv <= 0:
        return None
    try:
        return datetime.fromtimestamp(iv, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
