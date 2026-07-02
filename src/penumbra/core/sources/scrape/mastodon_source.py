"""Mastodon / the fediverse: keyless public hashtag timelines across a few high-signal instances.

The fediverse is a population distinct from Bluesky and X: a federated network of
independent Mastodon servers whose public hashtag timelines are readable WITHOUT auth.
Every Mastodon instance exposes ``GET {base}/api/v1/timelines/tag/<tag>?limit=N`` (no
token needed for the public surface), returning a JSON array of Status objects. We ride
that to cover three high-signal instances whose hashtag streams web search reaches only
shallowly:

  * mastodon.social : the flagship general-purpose instance (broadest population)
  * sigmoid.social  : the ML / AI research community instance
  * fosstodon.org   : the FOSS / tech / open-source community instance

A query has no hashtag concept, so we DERIVE one from the query's top token (first
alphanumeric word, '#' and punctuation stripped, lowercased): the hashtag timeline is a
tag lookup, not full-text search. The query "diffusion models" hits the #diffusion stream;
a single-word query hits that word's stream directly.

This is ONE adapter that fans the derived tag out over each instance (gently: one GET
each, capped limit), merges the per-instance Status hits, and emits one doc per status:
content = the status text stripped of HTML to Markdown, author = ``account.acct`` (the
fully-qualified fediverse handle), url = ``status.url`` (the canonical permalink; falls
back to ``uri`` for bridged/remote statuses that report a null url), date = ``created_at``,
signals = favourites_count + reblogs_count via mk_signal.

Built on BaseScrapeAdapter (template method): the cache check / atomic set_docs /
self-registration ritual lives in the base. We override the two hooks (_raw_fetch = the
per-instance tag-timeline fan-out; _to_documents = the status -> Document merge).
We keep ``rank=False`` (the base default) and instead rank-then-slice INSIDE
_to_documents over the FULL cross-instance pool with the same shared BM25 scorer the base
would use: a merged multi-instance result has no single server order, and pre-capping per
the base's no-reslice contract would starve the later instances (the first instance fills
``limit`` before the ranker ever sees the rest). Ranking the whole pool first gives a
fair best-first ordering across all three instances.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from penumbra.core import http
from penumbra.core.normalize import (
    Document,
    jsonsafe,
    keyword_score_filter,
    mk_signal,
)
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

TIMEOUT = 15

# The Mastodon instances we cover (a few high-signal populations). Keyed by host; the
# value is the scheme+host base used to build the tag-timeline URL.
INSTANCES: dict[str, str] = {
    "mastodon.social": "https://mastodon.social",  # flagship general-purpose
    "sigmoid.social": "https://sigmoid.social",    # ML / AI research
    "fosstodon.org": "https://fosstodon.org",       # FOSS / tech
}


def _derive_tag(query: str) -> str:
    """Derive a hashtag from the query's top token: the first alphanumeric word with '#'
    and surrounding punctuation stripped, lowercased. A hashtag timeline is a tag lookup
    (not full-text), so we collapse a multi-word query to its leading token. Empty if the
    query has no usable token (the fetch then degrades to no results)."""
    for tok in (query or "").split():
        cleaned = re.sub(r"[^0-9A-Za-z_]", "", tok)  # tags are alnum/underscore; drop #, punct
        if cleaned:
            return cleaned.lower()
    return ""


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    """Mastodon ``created_at`` is ISO-8601 UTC with a trailing 'Z'."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _strip_html(html: Optional[str]) -> str:
    """Status ``content`` is server-rendered HTML (``<p>``/``<a>``/``<br>``); convert it to
    clean Markdown (the same markdownify the RSS / Zenodo / Discourse sources use). Falls
    back to a crude tag-strip on the rare payload markdownify chokes on."""
    if not isinstance(html, str) or not html.strip():
        return ""
    try:
        from markdownify import markdownify as html_to_md
        return html_to_md(html, heading_style="ATX").strip()
    except Exception:  # noqa: BLE001 (markdownify can be picky; degrade to a tag-strip)
        return re.sub(r"<[^>]+>", "", html).strip()


class MastodonAdapter(BaseScrapeAdapter):
    name = "mastodon"
    needs_credentials = False
    description = (
        "Mastodon / the fediverse: public HASHTAG timelines across high-signal instances "
        "(mastodon.social / sigmoid.social ML / fosstodon.org FOSS), keyless. Reaches walled "
        "same-day fediverse posts web search barely indexes; best for BROAD / trending topics "
        "(retrieval is hashtag-level, not precise keyword search), not narrow exact-term lookups"
    )
    cache_ttl = 900

    # Routing facets (mirrors RSS / config rows; the router reads these class attrs).
    kind = "lookup"
    domains = ["social", "community"]
    modes = ["UNWALL", "STRUCTURE", "MONITOR"]

    # Stay at the base default (rank=False): a per-instance cap under the base's
    # no-reslice contract would starve the later instances. We rank-then-slice the FULL
    # cross-instance pool ourselves inside _to_documents (same shared BM25 scorer).
    rank = False

    # ── hooks ───────────────────────────────────────────────────────────────
    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """Fan the derived hashtag out over each instance's keyless tag timeline (one GET
        each, gently). Returns a list of (host, statuses) for instances that answered;
        None if no tag could be derived OR every instance failed (base degrades to [])."""
        tag = _derive_tag(query)
        if not tag:
            return None
        # Ask each instance for up to `limit` statuses; Mastodon caps tag-timeline limit at
        # 40, so clamp into [1, 40] to stay within the documented bound.
        per = max(1, min(limit or 10, 40))
        results: list[tuple[str, list]] = []
        for host, base in INSTANCES.items():
            data = http.get_json(
                f"{base}/api/v1/timelines/tag/{tag}",
                params={"limit": per},
                timeout=TIMEOUT,
            )
            if isinstance(data, list):
                results.append((host, data))
            else:
                logger.debug("mastodon: %s returned no list for #%s", host, tag)
        return results or None

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        docs: list[Document] = []
        seen: set[str] = set()  # dedupe a status federated to multiple instances (shared uri)
        for host, statuses in raw:
            base = INSTANCES.get(host, f"https://{host}")
            for status in statuses:
                if not isinstance(status, dict):
                    continue
                try:
                    doc = self._status_to_document(status, base)
                except Exception as exc:  # noqa: BLE001 (one malformed status degrades to skip)
                    logger.debug("mastodon: skipping malformed status on %s: %s", host, exc)
                    continue
                if doc is None:
                    continue
                if doc.source_id in seen:
                    continue
                seen.add(doc.source_id)
                docs.append(doc)
        # Rank the FULL cross-instance pool best-first (shared BM25 scorer; drops non-matches,
        # term-less query passes through), THEN slice to limit, so the best statuses across
        # ALL instances survive, not just the first instance's.
        docs = keyword_score_filter(docs, query)
        return docs[:limit] if limit and limit > 0 else docs

    @staticmethod
    def _status_to_document(status: dict, base: str) -> Optional[Document]:
        # A reblog (boost) carries the real content under `reblog`; follow it so we index
        # the boosted status, not an empty wrapper.
        reblog = status.get("reblog")
        src = reblog if isinstance(reblog, dict) else status

        # Canonical permalink: prefer the federated `url`; remote/bridged statuses report a
        # null url, so fall back to `uri` (the ActivityPub id is also a fetchable URL).
        url = (src.get("url") or src.get("uri") or "").strip()
        status_id = str(src.get("id") or "")
        if not url and not status_id:
            return None

        account = src.get("account") or {}
        acct = (account.get("acct") or "").strip() if isinstance(account, dict) else ""
        display = (account.get("display_name") or "").strip() if isinstance(account, dict) else ""

        content_md = _strip_html(src.get("content"))
        # The status text is short and has no native title; build a compact one from the
        # author + the first line of the content (Mastodon statuses are titleless).
        first_line = next((ln.strip() for ln in content_md.splitlines() if ln.strip()), "")
        snippet = (first_line[:80] + "...") if len(first_line) > 80 else first_line
        handle = acct or display or "unknown"
        title = f"@{handle}: {snippet}" if snippet else f"@{handle} (status)"

        # tags: the status's own hashtags (each is {name, url}); lowercase names.
        raw_tags = src.get("tags") or []
        tags = [
            t["name"].lower()
            for t in raw_tags
            if isinstance(t, dict) and isinstance(t.get("name"), str) and t.get("name")
        ]

        # media: attached image/video preview URLs (a vision-capable agent can view these).
        media = [
            m["url"]
            for m in (src.get("media_attachments") or [])
            if isinstance(m, dict) and isinstance(m.get("url"), str) and m.get("url")
        ]

        # signals: source-reported engagement counts (None-safe via mk_signal).
        signals: dict = {}
        signals.update(mk_signal(
            "favourites", src.get("favourites_count"),
            kind="engagement", by="mastodon/favourites_count",
        ))
        signals.update(mk_signal(
            "reblogs", src.get("reblogs_count"),
            kind="engagement", by="mastodon/reblogs_count",
        ))

        return Document(
            source="mastodon",
            source_id=src.get("uri") or url or status_id,
            url=url,
            title=title,
            content=content_md,
            author=acct or display or None,
            date=_parse_date(src.get("created_at")),
            signals=signals,
            tags=tags,
            media=media,
            metadata={
                "instance": base,
                "language": src.get("language"),
                "replies_count": src.get("replies_count"),
                "boosted": reblog is not None,
                "raw": jsonsafe(status),
            },
        )


# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
