"""V2EX (www.v2ex.com): the CN tech / 润学 forum, via its keyless per-node Atom feeds.

V2EX is the Chinese-language forum where developers, overseas-job seekers and would-be
emigrants talk shop: 职场 grievances, 外企 remote gigs, 移民 (润) logistics, 海外留学. It
is exactly the 润学 / 外企-remote / 润→加拿大 demographic OmniSeek otherwise misses (Reddit
is EN, the CN paper/patent sources are formal, tieba is mass-consumer). This adapter taps
the community's own words.

V2EX has NO keyless per-QUERY search API (site search is walled behind SoV2EX / login). Each node
publishes an Atom feed of its latest topics, answered keylessly (measured 2026-11-09, ~50 entries
per node):

  * Per-node latest topics: GET https://www.v2ex.com/feed/<slug>.xml
        -> <feed><title>node title</title><entry><title/><link href=".../t/<id>#reply<n>"/>
           <published/><updated/><author><name/></author><content type="html"/></entry>...</feed>

The legacy topics JSON (/api/topics/show.json?node_name=<slug>, used until 2026-11-09) and every
page of the site now answer our exit with Cloudflare's managed challenge (HTTP 403 "Just a
moment...", since 2026-11-04); the feeds are served from Cloudflare's cache and are not challenged.
A feed entry carries no node size and no reply field: the reply count is the ``#reply<n>`` anchor of
its link.

So there is no {query} to substitute: the endpoint is per-NODE, not per-query. We therefore
fan out over a curated set of on-telos nodes (职场 / 求职 / 酷工作 / 远程工作 / 移民 /
海外留学), merge their latest topics, and let the shared BM25 scorer (``rank = True``) order
and filter them by the caller's query. A term-less query keeps the merged latest order (a
MONITOR-style community pulse across the 润学 nodes); a query with terms returns only the
matching topics, best-first. This is the tieba multi-GET pattern (fan out, merge, emit), not
a single-endpoint declarative row, which is why it is coded.

BaseScrapeAdapter (template method): the cache check / atomic set_docs / shared BM25 rank /
self-registration ritual lives in the base; this adapter fills the two hooks (_raw_fetch =
the per-node feed GETs, _to_documents = feed entries -> Document, parsed in the process lane
for a large answer). Canonical topic page is https://www.v2ex.com/t/<id>; node page is
https://www.v2ex.com/go/<slug>.

These are public endpoints reachable with a plain Chrome UA, so this adapter uses the shared
http egress directly (no credentials, no CDP).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import feedparser

from omniseek.core import diag, http
from omniseek.core.normalize import Document, html_fragment_text, jsonsafe, mk_signal
from omniseek.core.sources._base import aparse
from omniseek.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

FEED_URL = "https://www.v2ex.com/feed/{slug}.xml"
NODE_PAGE = "https://www.v2ex.com/go/"                    # +<slug>
TIMEOUT = 15
_ACCEPT = "application/atom+xml, application/xml;q=0.9, */*;q=0.8"
_TOPIC_RE = re.compile(r"/t/(\d+)")
_REPLY_RE = re.compile(r"#reply(\d+)$")

# The curated on-telos node set (slug -> a short label for logging only; the live node title
# comes from the feed). Chosen for the 润学 / 外企-remote / 润→加拿大 demographic, tightest
# first: 职场 grievances, 求职 hunting, 酷工作 postings (incl. overseas/remote), 远程工作,
# 移民 (润) logistics, 海外留学. Broad tech nodes (programmer / qna / invest) are left out to
# keep the source's identity on-telos; the BM25 filter already handles query relevance.
NODES: tuple[str, ...] = ("career", "cv", "jobs", "remote", "immigration", "global")

# A plain desktop Chrome UA: the feeds answer keylessly with this (the shared omniseek UA is
# not needed here, but V2EX does gate obviously-empty UAs, so we send a real browser one).
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class V2exAdapter(BaseScrapeAdapter):
    name = "v2ex"
    needs_credentials = False
    description = "V2EX (www.v2ex.com): the CN tech / 润学 forum; 职场 / 求职 / 远程工作 / 移民 / 海外留学 community threads via the keyless per-node Atom feeds"
    cache_ttl = 900

    # OPT-IN BM25: the endpoint is per-node (no server-side query relevance), so we score the
    # merged multi-node feed locally against the query (term-less query keeps latest order).
    rank = True
    # The six node feeds are the same whatever the query: cached once by URL and node list, every
    # query scores that one fetch (keyed by query it re-fetched them for every new
    # query and used up the host's 120-per-hour gate window in minutes of a batch search).
    query_independent = True

    def _fetch_identity(self) -> tuple:
        return (FEED_URL, ",".join(NODES))

    # routing facets (the router reads these class attrs; do NOT touch facets.json)
    kind = "lookup"
    domains = ["community", "career"]
    modes = ["UNWALL", "MONITOR"]
    regions = ["cn"]

    # -- native async -----------------------------------------------------
    @staticmethod
    async def _aget_feed(slug: str) -> Optional[bytes]:
        """One node's Atom feed body, or None. The shared async leaf ``http.aget`` (pool, SSRF guard,
        cache_only, size cap, the diag.note of a non-2xx). A 2xx whose body is not a feed (a challenge
        or error page served with 200) is noted here with its address, so the failure records of a
        health probe always carry the URL the exit-node sweep refetches."""
        url = FEED_URL.format(slug=quote(slug))
        resp = await http.aget(url, headers={"User-Agent": _UA, "Accept": _ACCEPT}, timeout=TIMEOUT)
        if resp is None:
            return None  # request failure (http owns the log + diag.note with url and status)
        body = resp.content or b""
        if b"<feed" not in body[:2048]:
            logger.warning("v2ex: node=%s answered HTTP %s with a body that is not an Atom feed",
                           slug, resp.status_code)
            diag.note("v2ex.feed", url=url, status=resp.status_code,
                      body=body[:400].decode("utf-8", "replace"))
            return None
        return body

    async def _araw_fetch(self, query: str, limit: int) -> Optional[list]:
        """The per-node fan-out, fetched CONCURRENTLY: one GET per node in NODES, the six in flight
        together (independent listings; the host's declared gate in upstreams.json paces them).
        Returns ``[[slug, feed bytes], ...]`` in NODES order for the nodes that answered (plain data,
        so the parse can run in the process lane), None when none did. One node failing does not cost
        the other five."""
        settled = await asyncio.gather(
            *(self._aget_feed(slug) for slug in NODES),
            return_exceptions=True,
        )
        out: list = []
        for slug, body in zip(NODES, settled):
            if isinstance(body, BaseException):
                logger.warning("v2ex: node=%s raised: %s", slug, body)
                continue
            if body is not None:
                out.append([slug, body])
        return out or None

    # -- parse -----------------------------------------------------------------
    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        """Every entry of every fetched node feed, merged in NODES order and deduplicated by topic id
        (a topic cross-listed in two nodes stays with the earlier node). Not sliced: the base applies
        BM25 (rank=True) over the full merged set, then the caller's top-k takes limit."""
        if not isinstance(raw, list):
            return []
        docs: list[Document] = []
        seen: set[str] = set()
        for item in raw:
            if not (isinstance(item, (list, tuple)) and len(item) == 2):
                continue
            slug, body = item
            try:
                parsed = feedparser.parse(body)
            except Exception as exc:  # noqa: BLE001
                logger.warning("v2ex: node=%s feed parse failed: %s", slug, exc)
                continue
            node_title = _clean_text((parsed.get("feed") or {}).get("title")) or None
            for entry in parsed.entries or ():
                doc = self._entry_doc(entry, str(slug), node_title)
                if doc is None or doc.source_id in seen:
                    continue
                seen.add(doc.source_id)
                docs.append(doc)
        return docs

    def _entry_doc(self, entry: Any, slug: str, node_title: Optional[str]) -> Optional[Document]:
        """One feed entry -> a UNWALL doc (title, opening post as plain text, node context, replies)."""
        link = str(entry.get("link") or "")
        m = _TOPIC_RE.search(link) or _TOPIC_RE.search(str(entry.get("id") or ""))
        if not m:
            return None
        tid = m.group(1)
        title = _clean_text(entry.get("title")) or "(无标题)"
        body_html = ""
        if entry.get("content"):
            body_html = entry["content"][0].get("value", "") or ""
        content = html_fragment_text(body_html or entry.get("summary") or "")

        rm = _REPLY_RE.search(link)
        replies = int(rm.group(1)) if rm else None
        signals = mk_signal("replies", replies, kind="engagement", by="v2ex/feed/link#reply")

        tags = ["topic", "v2ex"]
        if node_title:
            tags.append(node_title)

        return Document(
            source=self.name,
            source_id=tid,
            url=f"https://www.v2ex.com/t/{tid}",
            title=title,
            content=content,
            author=_clean_text(entry.get("author")) or None,
            date=_struct_dt(entry.get("published_parsed") or entry.get("updated_parsed")),
            signals=signals,
            tags=tags,
            metadata={
                "doc_type": "topic",
                "tid": tid,
                "node_name": slug,
                "node_title": node_title,
                "node_url": NODE_PAGE + slug,
                "last_touched": entry.get("updated"),
                "raw": jsonsafe(dict(entry)),
            },
        )

    async def afetch_url(self, url: str) -> Optional[Document]:
        """This source does not claim arbitrary topic URLs (the node feeds are the entry
        point; a single topic's full replies are out of scope here)."""
        return None

    async def ahealth_check(self) -> tuple[bool, str]:
        """Light probe: ONE node's feed (career) rather than the full 6-node fan-out, so a health poll
        stays cheap. Live if it parses with at least one topic."""
        body = await self._aget_feed("career")
        if body is None:
            return False, "career node feed fetch returned nothing"
        n = await aparse(len(body), _entry_count, body)
        if not n:
            return False, "career node feed has no entries"
        return True, f"OK ({n} topics)"


# -- helpers -------------------------------------------------------------------
def _entry_count(body: bytes) -> int:
    return len(feedparser.parse(body).entries or ())


def _clean_text(s: Any) -> str:
    """Trim a string field; non-strings (incl. None) become ''."""
    if not isinstance(s, str):
        return ""
    return s.strip()


def _struct_dt(struct: Any) -> Optional[datetime]:
    """feedparser's UTC time struct -> a tz-aware UTC datetime, None if absent or malformed."""
    if not struct:
        return None
    try:
        return datetime(*struct[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
