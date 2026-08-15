"""Hacker News — tech news + community discussion via Algolia search API.

Sub-agent's P0 reflection flagged this as missing from OmniSeek 的 25 个
精选 — HN is the highest-density tech news + threaded community discussion
aggregator and crucially independent of social media platforms.

Algolia hosts HN's official search endpoint (well-documented, no auth):
- /search?query=... &tags=story | comment&hitsPerPage=N
- /search_by_date?query=... — chronological
- /items/{id} — individual item with thread

Docs: https://hn.algolia.com/api
Story hits are dicts with: objectID, title, url, points, author, num_comments,
created_at, story_text, _highlightResult.
Comment hits are dicts with: objectID, comment_text (HTML), story_title,
story_url, story_id, parent_id, author, created_at, _tags. (The Algolia index
does NOT expose per-comment points, so a comment's engagement signal is empty.)

Two layers are retrieved per query and merged into one ranked result set:
  * STORIES (tags=story) — the headline submission + its score/comment counts.
  * COMMENTS (tags=comment) — full-text matches inside threads, the actual
    discussion (often the highest-signal part of HN). Each comment doc carries
    the parent story's title, links to the comment on news.ycombinator.com, and
    is tagged "comment" so it is unmistakable in a mixed result list.
"""

from __future__ import annotations

import functools
import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, urlparse

import anyio
import httpx
from markdownify import markdownify as html_to_md

from omniseek.core import cache, http
from omniseek.core.normalize import Document, jsonsafe, mk_signal

logger = logging.getLogger(__name__)

ALGOLIA_BASE = "https://hn.algolia.com/api/v1"
TIMEOUT = 15
USER_AGENT = "omniseek/0.1 (automated retrieval)"


class HackerNewsAdapter:
    name = "hackernews"
    needs_credentials = False
    description = (
        "Hacker News — tech news (story submissions) + threaded community discussion "
        "(full-text comment search), both via the Algolia HN API"
    )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        key = cache.make_key("hackernews", "search", query, limit)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached

        # Two layers, ONE merged result set: stories (the submission) AND comments
        # (the actual thread discussion, often HN's highest-signal content). Split the
        # budget so a small ``limit`` still surfaces both; over-fetch is capped at 30
        # per layer (Algolia's practical page size) before slicing back to ``limit``.
        story_budget = max(1, (limit + 1) // 2)   # ceil(limit/2) → stories get the tie
        comment_budget = max(1, limit - story_budget)

        docs: list[Document] = []

        story_data = http.get_json(
            f"{ALGOLIA_BASE}/search",
            params={"query": query, "tags": "story", "hitsPerPage": min(story_budget, 30)},
            timeout=TIMEOUT,
        )
        for hit in (story_data or {}).get("hits", [])[:story_budget]:
            try:
                docs.append(self._hit_to_document(hit))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping malformed HN story hit: %s", exc)

        comment_data = http.get_json(
            f"{ALGOLIA_BASE}/search",
            params={"query": query, "tags": "comment", "hitsPerPage": min(comment_budget, 30)},
            timeout=TIMEOUT,
        )
        for hit in (comment_data or {}).get("hits", [])[:comment_budget]:
            try:
                docs.append(self._comment_to_document(hit))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping malformed HN comment hit: %s", exc)

        # Both layers failed (e.g. network) → honest empty, don't cache the miss.
        if story_data is None and comment_data is None:
            return []

        cache.set_docs(key, docs, ttl=900)
        return docs

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` (S4b): mirrors ``search`` line-for-line so the fetcher's
        native async dispatch (``AsyncSearchCapable``) awaits it DIRECTLY, spending a coroutine on the
        two Algolia waits instead of a held pool thread. Three changes only vs ``search``:
          - the disk CACHE read/write go OFF the loop (anyio.to_thread.run_sync: get_docs / set_docs do
            file IO), keyed IDENTICALLY so async and sync share the cache;
          - the two Algolia GETs swap to ``await http.aget_json`` (both fan-out calls; the async NETWORK
            wait stays ON the loop via epoll, no held thread — the SSRF getaddrinfo inside the async leaf
            is moved off-loop by the http layer);
          - the PURE-CPU budget math + hit→doc mapping (_hit_to_document / _comment_to_document) stay ON
            the loop, byte-identical to ``search`` (no drift)."""
        key = cache.make_key("hackernews", "search", query, limit)
        cached = await anyio.to_thread.run_sync(cache.get_docs, key)  # disk read OFF loop
        if cached is not None:
            return cached

        # Two layers, ONE merged result set: stories (the submission) AND comments
        # (the actual thread discussion, often HN's highest-signal content). Split the
        # budget so a small ``limit`` still surfaces both; over-fetch is capped at 30
        # per layer (Algolia's practical page size) before slicing back to ``limit``.
        story_budget = max(1, (limit + 1) // 2)   # ceil(limit/2) → stories get the tie
        comment_budget = max(1, limit - story_budget)

        docs: list[Document] = []

        story_data = await http.aget_json(
            f"{ALGOLIA_BASE}/search",
            params={"query": query, "tags": "story", "hitsPerPage": min(story_budget, 30)},
            timeout=TIMEOUT,
        )
        for hit in (story_data or {}).get("hits", [])[:story_budget]:
            try:
                docs.append(self._hit_to_document(hit))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping malformed HN story hit: %s", exc)

        comment_data = await http.aget_json(
            f"{ALGOLIA_BASE}/search",
            params={"query": query, "tags": "comment", "hitsPerPage": min(comment_budget, 30)},
            timeout=TIMEOUT,
        )
        for hit in (comment_data or {}).get("hits", [])[:comment_budget]:
            try:
                docs.append(self._comment_to_document(hit))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping malformed HN comment hit: %s", exc)

        # Both layers failed (e.g. network) → honest empty, don't cache the miss.
        if story_data is None and comment_data is None:
            return []

        await anyio.to_thread.run_sync(  # disk write OFF loop
            functools.partial(cache.set_docs, key, docs, ttl=900))
        return docs

    def fetch_url(self, url: str) -> Optional[Document]:
        host = (urlparse(url).hostname or "").lower()
        if "ycombinator.com" not in host:
            return None
        qs = parse_qs(urlparse(url).query)
        item_id = (qs.get("id") or [None])[0]
        if not item_id:
            return None
        item = http.get_json(f"{ALGOLIA_BASE}/items/{item_id}", timeout=TIMEOUT)
        if item is None:
            return None
        return self._item_to_document(item)

    def health_check(self) -> tuple[bool, str]:
        try:
            resp = httpx.get(
                f"{ALGOLIA_BASE}/search",
                params={"query": "test", "hitsPerPage": 1},
                headers={"User-Agent": USER_AGENT},
                timeout=8,
            )
            if resp.status_code == 200 and resp.json().get("hits") is not None:
                return True, "OK"
            return False, f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _hit_to_document(hit: dict) -> Document:
        story_id = hit.get("objectID")
        title = hit.get("title") or "(no title)"
        external_url = hit.get("url")
        comments_url = f"https://news.ycombinator.com/item?id={story_id}"
        url = external_url or comments_url
        comment_count = hit.get("num_comments") or 0
        score = hit.get("points") or 0
        created_at = hit.get("created_at")
        date = None
        if created_at:
            try:
                date = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass
        return Document(
            source="hackernews",
            source_id=str(story_id),
            url=url,
            title=title,
            content=hit.get("story_text") or f"HN story • {comment_count} comments • {score} points",
            author=hit.get("author"),
            date=date,
            # num_comments was already parsed into metadata; promote it to a SIGNAL so it is
            # sortable beside points, like every other thread source now (2026-07-25).
            signals={
                **mk_signal("points", score, kind="engagement", by="hackernews/score"),
                **mk_signal("comments", hit.get("num_comments"),
                            kind="engagement", by="hackernews/num_comments"),
            },
            tags=hit.get("_tags") or [],
            metadata={
                "story_id": story_id,
                "comments_url": comments_url,
                "external_url": external_url,
                "num_comments": comment_count,
                "points": score,
                "raw": jsonsafe(hit),
            },
        )

    @staticmethod
    def _comment_to_document(hit: dict) -> Document:
        """A full-text comment match → a doc. Title is the parent story's title (so the
        comment is anchored to what it's about), content is the comment body (HTML→Markdown),
        url points at the comment on news.ycombinator.com. The Algolia comment index exposes
        no per-comment points, so the engagement signal is honestly empty (None)."""
        comment_id = hit.get("objectID")
        story_title = hit.get("story_title") or "(no story title)"
        comments_url = f"https://news.ycombinator.com/item?id={comment_id}"

        body_html = hit.get("comment_text") or ""
        try:
            body = html_to_md(body_html, heading_style="ATX").strip() if body_html else ""
        except Exception:  # noqa: BLE001
            body = re.sub(r"<[^>]+>", "", body_html).strip()

        created_at = hit.get("created_at")
        date = None
        if created_at:
            try:
                date = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        return Document(
            source="hackernews",
            source_id=str(comment_id),
            url=comments_url,
            title=story_title,
            content=body or "(empty comment)",
            author=hit.get("author"),
            date=date,
            # Comment hits carry a `points` key but the HN Algolia index leaves it None;
            # mk_signal coerces that to a None-valued engagement signal (honest: no count).
            signals=mk_signal("points", hit.get("points"),
                              kind="engagement", by="hackernews/comment_points"),
            tags=hit.get("_tags") or ["comment"],
            metadata={
                "is_comment": True,
                "comment_id": comment_id,
                "story_id": hit.get("story_id"),
                "story_title": story_title,
                "story_url": hit.get("story_url"),
                "parent_id": hit.get("parent_id"),
                "comments_url": comments_url,
                "raw": jsonsafe(hit),
            },
        )

    @staticmethod
    def _item_to_document(item: dict) -> Document:
        item_id = item.get("id")
        title = item.get("title") or "(no title)"
        external_url = item.get("url")
        comments_url = f"https://news.ycombinator.com/item?id={item_id}"
        return Document(
            source="hackernews",
            source_id=str(item_id),
            url=external_url or comments_url,
            title=title,
            content=item.get("text") or item.get("title") or "(no text)",
            author=item.get("author"),
            signals=mk_signal("points", item.get("points"),
                              kind="engagement", by="hackernews/points"),
            metadata={
                "item_id": item_id,
                "comments_url": comments_url,
                "external_url": external_url,
                "type": item.get("type"),
                "raw": jsonsafe(item),
            },
        )


from omniseek.core.fetcher import register_adapter

register_adapter(HackerNewsAdapter())
