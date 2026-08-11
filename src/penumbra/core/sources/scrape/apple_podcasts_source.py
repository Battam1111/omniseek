"""Apple Podcasts: podcast discovery via the keyless iTunes Search API.

iTunes Search (https://itunes.apple.com/search) is Apple's public, no-auth
catalogue lookup. With ``media=podcast`` it returns the show-level records for
podcasts matching a term: collection (show) name, the artist/host, genres, the
public Apple URL, and crucially the **feedUrl** (the show's RSS).

The VALUE here is TRANSCRIBE, not text. A podcast doc carries almost no body
the agent can read. The payoff is surfacing the ``feedUrl`` so the agent can
pull a real episode .mp3 from that feed and run ``penumbra_transcribe`` on it to get
the SPOKEN content. To make that one step shorter, for the TOP result we also
fetch the feed once and surface the latest episode's enclosure .mp3 URL
(``metadata.latest_episode_mp3``) so the agent can ASR it immediately without a
second discovery hop.

Docs: https://performance-partners.apple.com/search-api
Endpoint:
  GET https://itunes.apple.com/search?media=podcast&term=<query>&limit=<limit>
  → {"resultCount": N, "results": [{collectionName, artistName, feedUrl,
       trackCount, genres, collectionViewUrl, releaseDate, ...}, ...]}

Keyless / read-only. rank stays default-False: iTunes Search already returns
results in catalogue-relevance order for the term, so we keep that server order
(byte-identical-to-hand-form policy of BaseScrapeAdapter).
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Optional

from penumbra.core import http
from penumbra.core.normalize import Document, jsonsafe, mk_signal
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

SEARCH_URL = "https://itunes.apple.com/search"


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    """iTunes ``releaseDate`` is ISO-8601 with a trailing ``Z`` (e.g.
    ``2008-07-23T19:12:00Z``). Coerce to a tz-aware UTC datetime; None on miss."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _latest_episode_mp3(feed_url: str) -> Optional[str]:
    """Fetch a podcast RSS feed once and return the FIRST item's audio enclosure
    URL (feeds list newest-first), so the agent is one step from ASR. Best-effort:
    any failure (no feed, network, malformed XML, no enclosure) returns None."""
    text = http.get_text(feed_url, timeout=15)
    if not text:
        return None
    try:
        root = ET.fromstring(text)
    except Exception:  # noqa: BLE001 (malformed feed -> no episode link, not a crash)
        return None
    # RSS shape: <rss><channel><item><enclosure url="..." type="audio/..."/></item>...
    channel = root.find("channel")
    if channel is None:
        return None
    for item in channel.findall("item"):
        enc = item.find("enclosure")
        if enc is None:
            continue
        url = (enc.get("url") or "").strip()
        if not url:
            continue
        etype = (enc.get("type") or "").lower()
        # Prefer an explicit audio enclosure; if type is absent, accept a URL that
        # looks like an audio file (some feeds omit the type attribute).
        if etype.startswith("audio/") or url.lower().split("?")[0].endswith(
            (".mp3", ".m4a", ".aac", ".wav", ".ogg")
        ):
            return url
    return None


async def _alatest_episode_mp3(feed_url: str) -> Optional[str]:
    """Async twin of ``_latest_episode_mp3``: SAME best-effort RSS fetch (now
    ``await http.aget_text``, a COROUTINE not a held pool thread) + the SAME pure-CPU XML parse /
    enclosure pick (on loop). Byte-identical logic; any failure returns None."""
    text = await http.aget_text(feed_url, timeout=15)
    if not text:
        return None
    try:
        root = ET.fromstring(text)
    except Exception:  # noqa: BLE001 (malformed feed -> no episode link, not a crash)
        return None
    # RSS shape: <rss><channel><item><enclosure url="..." type="audio/..."/></item>...
    channel = root.find("channel")
    if channel is None:
        return None
    for item in channel.findall("item"):
        enc = item.find("enclosure")
        if enc is None:
            continue
        url = (enc.get("url") or "").strip()
        if not url:
            continue
        etype = (enc.get("type") or "").lower()
        # Prefer an explicit audio enclosure; if type is absent, accept a URL that
        # looks like an audio file (some feeds omit the type attribute).
        if etype.startswith("audio/") or url.lower().split("?")[0].endswith(
            (".mp3", ".m4a", ".aac", ".wav", ".ogg")
        ):
            return url
    return None


class ApplePodcastsAdapter(BaseScrapeAdapter):
    name = "apple_podcasts"
    needs_credentials = False
    description = "Apple Podcasts: find a podcast show + its RSS feedUrl (iTunes Search, keyless); pull an episode .mp3 for penumbra_transcribe"
    cache_ttl = 900
    kind = "lookup"
    domains = ["podcast"]
    modes = ["STRUCTURE", "TRANSCRIBE"]

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        return http.get_json(
            SEARCH_URL,
            params={"media": "podcast", "term": query, "limit": limit},
            timeout=15,
        )

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        results = (raw or {}).get("results") if isinstance(raw, dict) else None
        if not results:
            return []

        docs: list[Document] = []
        for idx, item in enumerate(results[:limit]):
            if not isinstance(item, dict):
                continue
            title = (item.get("collectionName") or item.get("trackName") or "").strip()
            if not title:
                continue
            author = (item.get("artistName") or "").strip() or None
            feed_url = (item.get("feedUrl") or "").strip()
            view_url = (item.get("collectionViewUrl") or item.get("trackViewUrl") or "").strip()
            url = feed_url or view_url
            if not url:
                continue
            genres = [g for g in (item.get("genres") or []) if isinstance(g, str)]
            track_count = item.get("trackCount")

            # Content is the TRANSCRIBE breadcrumb, not readable body: genres + the
            # feed the agent pulls an episode .mp3 from to run penumbra_transcribe.
            content_lines = []
            if genres:
                content_lines.append("Genres: " + ", ".join(genres))
            if isinstance(track_count, int):
                content_lines.append(f"Episodes: {track_count}")
            if feed_url:
                content_lines.append(f"RSS feed (pull an episode .mp3 for penumbra_transcribe): {feed_url}")
            else:
                content_lines.append("No RSS feedUrl exposed by iTunes for this show.")
            content = "\n".join(content_lines)

            metadata: dict[str, Any] = {
                "feedUrl": feed_url or None,
                "collectionViewUrl": view_url or None,
                "trackCount": track_count,
                "primaryGenre": item.get("primaryGenreName"),
                "collectionId": item.get("collectionId"),
                "raw": jsonsafe(item),
            }

            # CHEAP one-step-to-ASR: only for the TOP show, resolve the latest episode
            # enclosure .mp3 so the agent can penumbra_transcribe it without a second hop.
            if idx == 0 and feed_url:
                mp3 = _latest_episode_mp3(feed_url)
                if mp3:
                    metadata["latest_episode_mp3"] = mp3
                    content += f"\nLatest episode audio (ready for penumbra_transcribe): {mp3}"

            signals = {}
            if isinstance(track_count, int):
                # Episode count is the show's catalogue size, a mechanical engagement-ish
                # FACT (how much there is to transcribe), not a judgment of quality.
                signals = mk_signal(
                    "episode_count", track_count, kind="engagement",
                    by="apple_podcasts/trackCount", unit="episodes",
                )

            docs.append(Document(
                source=self.name,
                source_id=str(item.get("collectionId") or item.get("trackId") or url),
                url=url,
                title=title,
                content=content,
                author=author,
                date=_parse_date(item.get("releaseDate")),
                signals=signals,
                tags=genres,
                metadata=metadata,
            ))
        return docs

    # ── native-async twin (S4d: BOTH layers egress, so BOTH are mirrored) ────
    # This source is TWO-LAYER: `_raw_fetch` egresses (the iTunes Search GET) AND `_to_documents`
    # ITSELF egresses per-record (the TOP result's `_latest_episode_mp3` RSS fetch). So we cannot
    # pass the sync `_to_documents` to `_asearch_via` (its enrichment GET would block the loop);
    # BOTH layers get an async mirror, like `_stackexchange`'s abuild_documents awaiting the answer
    # fetch. The sync `_raw_fetch` / `_to_documents` / `search` are UNTOUCHED.
    async def _araw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """Async twin of `_raw_fetch`: SAME keyless iTunes Search GET, now `await http.aget_json`."""
        return await http.aget_json(
            SEARCH_URL,
            params={"media": "podcast", "term": query, "limit": limit},
            timeout=15,
        )

    async def _ato_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        """Async twin of `_to_documents` (this source's `_to_documents` ITSELF egresses per-record:
        the TOP show's `_latest_episode_mp3` RSS fetch). Reproduced line-for-line, but that one
        enrichment egress is AWAITED (`_alatest_episode_mp3`). SAME top-result-only cap (idx == 0),
        SAME order, SAME per-record skip-on-fail, SAME markdownify-free content build (pure CPU, on
        loop). Only ONE await per page (idx == 0) → order is byte-identical to sync."""
        results = (raw or {}).get("results") if isinstance(raw, dict) else None
        if not results:
            return []

        docs: list[Document] = []
        for idx, item in enumerate(results[:limit]):
            if not isinstance(item, dict):
                continue
            title = (item.get("collectionName") or item.get("trackName") or "").strip()
            if not title:
                continue
            author = (item.get("artistName") or "").strip() or None
            feed_url = (item.get("feedUrl") or "").strip()
            view_url = (item.get("collectionViewUrl") or item.get("trackViewUrl") or "").strip()
            url = feed_url or view_url
            if not url:
                continue
            genres = [g for g in (item.get("genres") or []) if isinstance(g, str)]
            track_count = item.get("trackCount")

            # Content is the TRANSCRIBE breadcrumb, not readable body: genres + the
            # feed the agent pulls an episode .mp3 from to run penumbra_transcribe.
            content_lines = []
            if genres:
                content_lines.append("Genres: " + ", ".join(genres))
            if isinstance(track_count, int):
                content_lines.append(f"Episodes: {track_count}")
            if feed_url:
                content_lines.append(f"RSS feed (pull an episode .mp3 for penumbra_transcribe): {feed_url}")
            else:
                content_lines.append("No RSS feedUrl exposed by iTunes for this show.")
            content = "\n".join(content_lines)

            metadata: dict[str, Any] = {
                "feedUrl": feed_url or None,
                "collectionViewUrl": view_url or None,
                "trackCount": track_count,
                "primaryGenre": item.get("primaryGenreName"),
                "collectionId": item.get("collectionId"),
                "raw": jsonsafe(item),
            }

            # CHEAP one-step-to-ASR: only for the TOP show, resolve the latest episode
            # enclosure .mp3 so the agent can penumbra_transcribe it without a second hop.
            if idx == 0 and feed_url:
                mp3 = await _alatest_episode_mp3(feed_url)
                if mp3:
                    metadata["latest_episode_mp3"] = mp3
                    content += f"\nLatest episode audio (ready for penumbra_transcribe): {mp3}"

            signals = {}
            if isinstance(track_count, int):
                # Episode count is the show's catalogue size, a mechanical engagement-ish
                # FACT (how much there is to transcribe), not a judgment of quality.
                signals = mk_signal(
                    "episode_count", track_count, kind="engagement",
                    by="apple_podcasts/trackCount", unit="episodes",
                )

            docs.append(Document(
                source=self.name,
                source_id=str(item.get("collectionId") or item.get("trackId") or url),
                url=url,
                title=title,
                content=content,
                author=author,
                date=_parse_date(item.get("releaseDate")),
                signals=signals,
                tags=genres,
                metadata=metadata,
            ))
        return docs

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of BaseScrapeAdapter.search -> AsyncSearchCapable (the fan-out awaits
        this directly; the iTunes Search GET AND the top-result RSS enrichment cost COROUTINES, not
        held pool threads). BOTH layers egress, so `_asearch_via` is handed the ASYNC `_ato_documents`
        (it awaits it, being polymorphic). Shares the base async cache round-trip. BEHAVIOR-IDENTICAL
        to `search`: same cache key, same order, same enrichment cap."""
        return await self._asearch_via(
            query, limit,
            afetch=lambda: self._araw_fetch(query, limit),
            abuild=lambda raw: self._ato_documents(raw, query, limit))

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
