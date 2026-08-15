"""Discourse ML forums: keyless JSON search across the major Discourse instances.

Discourse (the open-source forum engine) ships a public, keyless JSON API on
every instance: ``GET {base}/search.json?q=<query>`` returns ``{topics:[...],
posts:[...]}`` (the two are parallel lists joined by ``topic_id``), and
``GET {base}/t/{id}.json`` returns one topic with its posts. We ride that to
cover three high-signal ML practitioner forums that web search reaches only
shallowly:

  * discuss.huggingface.co : Hugging Face Transformers / Datasets / Hub Q&A
  * discuss.pytorch.org     : PyTorch core usage, training, CUDA, autograd
  * forums.fast.ai          : fast.ai course + deep-learning practitioner threads

This is a single adapter that fans the query out over the instances (gently:
one search GET each, capped pagesize), merges the per-instance hits, and emits
one doc per matched topic. The matching post supplies the content blurb +
author + like_count (the topic itself carries title / slug / posts_count /
reply_count / created_at). URL is the canonical topic permalink
``{base}/t/{slug}/{id}``.

Built on BaseScrapeAdapter (template method): the cache check / atomic
set_docs / self-registration ritual lives in the base. We override the two
hooks (_raw_fetch = the per-instance search fan-out; _to_documents = the
topic+post → Document merge) and fetch_url (claim ``/t/{slug}/{id}`` on
a known instance host). We keep ``rank=False`` (the base default) and instead
rank-then-slice INSIDE _to_documents over the FULL cross-instance pool, using
the same shared BM25 scorer the base would use: a merged multi-instance result
has no single server-relevance order, and pre-capping per the base's no-reslice
contract would silently starve the later instances (the first instance fills
``limit`` before the ranker ever sees the rest). Ranking the whole pool first
gives a fair, coherent best-first ordering across all three forums.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from omniseek.core import http
from omniseek.core.normalize import (
    Document,
    jsonsafe,
    keyword_score_filter,
    mk_signal,
)
from omniseek.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

TIMEOUT = 15

# The Discourse instances we cover. Keyed by host (matched in fetch_url); the
# value is the scheme+host base used to build search/topic URLs.
INSTANCES: dict[str, str] = {
    "discuss.huggingface.co": "https://discuss.huggingface.co",
    "discuss.pytorch.org": "https://discuss.pytorch.org",
    "forums.fast.ai": "https://forums.fast.ai",
}


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    """Discourse timestamps are ISO-8601 UTC with a trailing 'Z'."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


class DiscourseForumsAdapter(BaseScrapeAdapter):
    name = "discourse_forums"
    needs_credentials = False
    description = (
        "Discourse ML forums (Hugging Face / PyTorch / fast.ai): practitioner Q&A threads on "
        "transformers, training, CUDA, fine-tuning, as STRUCTURED docs with sortable engagement "
        "fields (reply/like/post counts) + verbatim threads web search does not rank (keyless API)"
    )
    cache_ttl = 900

    # Routing facets (mirrors RSS / config rows; the router reads these class attrs).
    # modes = STRUCTURE only: the win is the queryable engagement fields + verbatim threads, NOT
    # access. These forums are PUBLIC + Google-indexable (no login/JS/anti-bot wall), so an UNWALL
    # claim would be false (verified in a WebSearch head-to-head, 2026-06-17).
    kind = "lookup"
    domains = ["community", "methodology"]
    modes = ["STRUCTURE"]

    # Stay at the base default (rank=False): a per-instance cap under the base's
    # no-reslice contract would starve the later forums. We rank-then-slice the FULL
    # cross-instance pool ourselves inside _to_documents (same shared BM25 scorer).
    rank = False

    # ── hooks ───────────────────────────────────────────────────────────────
    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """Fan the query out over each instance's keyless ``/search.json`` (one GET
        each, gently). Returns a list of (host, payload) for instances that answered;
        None only if EVERY instance failed (so the base degrades to [])."""
        results: list[tuple[str, dict]] = []
        for host, base in INSTANCES.items():
            data = http.get_json(
                f"{base}/search.json",
                params={"q": query},
                timeout=TIMEOUT,
            )
            if isinstance(data, dict):
                results.append((host, data))
            else:
                logger.debug("discourse_forums: %s returned no JSON", host)
        return results or None

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        docs: list[Document] = []
        for host, payload in raw:
            base = INSTANCES.get(host, f"https://{host}")
            topics = payload.get("topics") or []
            posts = payload.get("posts") or []

            # Posts reference topics via topic_id; index the FIRST (best-ranked)
            # matching post per topic to supply content/author/likes.
            post_by_topic: dict[int, dict] = {}
            for post in posts:
                tid = post.get("topic_id")
                if tid is not None and tid not in post_by_topic:
                    post_by_topic[tid] = post

            for topic in topics:
                try:
                    doc = self._topic_to_document(topic, post_by_topic.get(topic.get("id")), base)
                    if doc is not None:
                        docs.append(doc)
                except Exception as exc:  # noqa: BLE001 (one malformed item degrades to skip)
                    logger.debug("discourse_forums: skipping malformed topic on %s: %s", host, exc)
        # Rank the FULL cross-instance pool best-first (shared BM25 scorer; drops
        # non-matches, term-less query passes through), THEN slice to limit, so the
        # best topics across ALL forums survive, not just the first instance's.
        docs = keyword_score_filter(docs, query)
        return docs[:limit] if limit and limit > 0 else docs

    async def _araw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """Async twin of `_raw_fetch`: byte-faithful mirror of the per-instance
        ``/search.json`` fan-out (same URL, params, timeout, control flow, and
        None-return contract); ONLY the shared-http egress swaps to its async twin
        (``http.get_json`` -> ``await http.aget_json``). One GET per instance, gently."""
        results: list[tuple[str, dict]] = []
        for host, base in INSTANCES.items():
            data = await http.aget_json(
                f"{base}/search.json",
                params={"q": query},
                timeout=TIMEOUT,
            )
            if isinstance(data, dict):
                results.append((host, data))
            else:
                logger.debug("discourse_forums: %s returned no JSON", host)
        return results or None

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of `search` -> AsyncSearchCapable. Shares the base async
        cache round-trip; egress via `_araw_fetch`; mapping via the SAME pure-CPU
        `_to_documents` (byte-identical to `search`)."""
        return await self._asearch_via(
            query, limit,
            afetch=lambda: self._araw_fetch(query, limit),
            abuild=lambda raw: self._to_documents(raw, query, limit))

    def fetch_url(self, url: str) -> Optional[Document]:
        """Claim a topic permalink ``{base}/t/{slug}/{id}`` (or ``/t/{id}``) on a
        known instance: fetch ``{base}/t/{id}.json`` and build a doc from the
        topic + its first post."""
        parsed = urlparse(url)
        host = parsed.hostname or ""
        base = INSTANCES.get(host)
        if base is None:
            return None
        parts = parsed.path.strip("/").split("/")
        # Expected: t/<slug>/<id> or t/<id>
        if len(parts) < 2 or parts[0] != "t":
            return None
        topic_id: Optional[int] = None
        for seg in reversed(parts[1:]):
            try:
                topic_id = int(seg)
                break
            except ValueError:
                continue
        if topic_id is None:
            return None

        data = http.get_json(f"{base}/t/{topic_id}.json", timeout=TIMEOUT)
        if not isinstance(data, dict):
            return None

        post_stream = (data.get("post_stream") or {}).get("posts") or []
        first_post = post_stream[0] if post_stream else None
        topic = {
            "id": data.get("id") or topic_id,
            "title": data.get("title") or data.get("fancy_title"),
            "slug": data.get("slug"),
            "posts_count": data.get("posts_count"),
            "reply_count": data.get("reply_count"),
            "like_count": data.get("like_count"),
            "created_at": data.get("created_at"),
            "category_id": data.get("category_id"),
            "tags": data.get("tags"),
        }
        # Topic-endpoint posts carry full HTML in `cooked`; surface a plain blurb-ish view.
        post_blurb: Optional[dict] = None
        if first_post is not None:
            post_blurb = {
                "id": first_post.get("id"),
                "topic_id": topic_id,
                "username": first_post.get("username"),
                "name": first_post.get("name"),
                "created_at": first_post.get("created_at"),
                "blurb": _strip_html(first_post.get("cooked") or ""),
                "like_count": _post_like_count(first_post),
                "post_number": first_post.get("post_number"),
            }
        return self._topic_to_document(topic, post_blurb, base)

    # ── build ───────────────────────────────────────────────────────────────
    @staticmethod
    def _topic_to_document(
        topic: dict, post: Optional[dict], base: str
    ) -> Optional[Document]:
        topic_id = topic.get("id")
        if topic_id is None:
            return None
        title = topic.get("title") or topic.get("fancy_title") or "(no title)"
        slug = topic.get("slug") or "topic"
        url = f"{base}/t/{slug}/{topic_id}"

        post = post or {}
        content = (post.get("blurb") or "").strip() or "(no preview)"
        author = post.get("name") or post.get("username")
        date = _parse_date(post.get("created_at") or topic.get("created_at"))

        # Signals: prefer the matched post's like_count, else the topic's; pair with posts_count.
        like_count = post.get("like_count")
        if like_count is None:
            like_count = topic.get("like_count")
        signals = mk_signal(
            "likes", like_count, kind="engagement", by=f"discourse/{_short_host(base)}/like_count"
        )
        signals.update(
            mk_signal(
                "posts", topic.get("posts_count"), kind="engagement",
                by=f"discourse/{_short_host(base)}/posts_count",
            )
        )

        tags = topic.get("tags") or []
        if not isinstance(tags, list):
            tags = []

        return Document(
            source="discourse_forums",
            source_id=f"{_short_host(base)}:{topic_id}",
            url=url,
            title=title,
            content=content,
            author=author,
            date=date,
            signals=signals,
            tags=tags,
            metadata={
                "instance": _short_host(base),
                "category_id": topic.get("category_id"),
                "posts_count": topic.get("posts_count"),
                "reply_count": topic.get("reply_count"),
                "post_number": post.get("post_number"),
                "raw": jsonsafe({"topic": topic, "post": post}),
            },
        )


def _short_host(base: str) -> str:
    return urlparse(base).hostname or base


def _post_like_count(post: dict) -> Optional[int]:
    """Topic-endpoint posts report likes inside `actions_summary` (action id 2 = like)."""
    raw = post.get("like_count")
    if isinstance(raw, (int, float)):
        return int(raw)
    for action in post.get("actions_summary") or []:
        if action.get("id") == 2:
            return action.get("count")
    return None


def _strip_html(html: str) -> str:
    """Lossy HTML→text for the topic-endpoint `cooked` body (search blurbs are already plain)."""
    if not html:
        return ""
    try:
        from markdownify import markdownify as html_to_md
        return html_to_md(html, heading_style="ATX").strip()
    except Exception:  # noqa: BLE001
        import re
        return re.sub(r"<[^>]+>", "", html).strip()

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
