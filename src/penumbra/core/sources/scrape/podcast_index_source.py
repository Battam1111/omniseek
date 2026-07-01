"""Podcast Index: cross-network podcast catalog + the podcast:transcript router.

Podcast Index (podcastindex.org) is the open, cross-network catalog of >4M
podcast feeds (the index Apple does NOT gatekeep). Its unique value over the
keyless Apple Podcasts adapter is the Podcasting 2.0 namespace data: per EPISODE
it reports whether the feed ALREADY ships a ``podcast:transcript`` file. That is
the routing signal the eye wants: an episode that already publishes a transcript
needs NO ASR (read the transcript directly), while one with only an audio
enclosure is the real candidate for ``eye_transcribe``. So this adapter both
discovers a show AND tells the agent which of its recent episodes are
already-transcribed vs ASR-bound.

Access (REQUIRES a free key+secret from https://api.podcastindex.org/signup):

    GET https://api.podcastindex.org/api/1.0/search/byterm?q=<q>&max=<n>
    headers:
      X-Auth-Key:    <apiKey>
      X-Auth-Date:   <unix-seconds>
      Authorization: sha1(apiKey + apiSecret + unix-seconds)   # hex, lowercase
      User-Agent:    <required>

Response: ``{status, feeds: [<feed>...], count, query, description}`` where each
feed carries ``id``, ``title``, ``url`` (RSS), ``link`` (homepage), ``author``,
``ownerName``, ``description``, ``image``/``artwork``, ``episodeCount``,
``categories`` (``{catId: name}``), ``language``, ``newestItemPubdate`` (unix).

Transcript routing (the payoff): for the TOP result we make ONE enrichment hop to
``GET /episodes/byfeedid?id=<feedId>&max=<n>`` (episodes newest-first). Each episode
item carries ``enclosureUrl`` (the .mp3 for ASR), ``transcriptUrl`` and/or a
``transcripts`` array (``{url, type}``: the ``podcast:transcript`` namespace). We
surface ``metadata.transcript_episodes`` (episodes that already ship a transcript:
read it, skip ASR) and ``metadata.asr_episodes`` (enclosure-only: candidates for
eye_transcribe), mirroring apple_podcasts' one-step-to-ASR enrichment.

Credentials live in ``~/.polaris/credentials/podcastindex.json`` as
``{"key": "...", "secret": "..."}`` (a free key registered at the signup URL).
A ``.json.template`` is dropped on first import for discoverability. Without a key
the adapter returns ``[]`` (the contract) and ``health_check`` reports the gap.

rank stays default-False: byterm returns the index's own term-relevance order, and
the eye's ranked search re-scores across sources when it needs to.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from penumbra.core import auth, http
from penumbra.core.normalize import PolarisDocument, jsonsafe, mk_signal
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

API_BASE = "https://api.podcastindex.org/api/1.0"
SEARCH_URL = f"{API_BASE}/search/byterm"
EPISODES_URL = f"{API_BASE}/episodes/byfeedid"
PODCAST_PAGE = "https://podcastindex.org/podcast"  # human page: /podcast/<feedId>
TIMEOUT = 15

# Drop a credential template on first import so the user knows the file shape.
auth.write_template(
    "podcastindex",
    {
        "key": "YOUR_PODCASTINDEX_API_KEY",
        "secret": "YOUR_PODCASTINDEX_API_SECRET",
        "_help": "Register a free key/secret at https://api.podcastindex.org/signup",
    },
)


def _auth_headers(key: str, secret: str) -> dict:
    """Build the Podcast Index auth headers.

    The scheme (verified against the API's PHP/C#/Swift code samples): a unix-seconds
    timestamp goes in ``X-Auth-Date``, and the SHA1 hex digest of
    ``key + secret + timestamp`` goes in ``Authorization``. A ``User-Agent`` is
    mandatory (the shared http client supplies one). ``X-Auth-Key`` carries the key.
    """
    ts = str(int(time.time()))
    sig = hashlib.sha1(f"{key}{secret}{ts}".encode("utf-8")).hexdigest()
    return {
        "X-Auth-Key": key,
        "X-Auth-Date": ts,
        "Authorization": sig,
    }


class PodcastIndexAdapter(BaseScrapeAdapter):
    name = "podcast_index"
    needs_credentials = True
    description = "Podcast Index: cross-network podcast catalog; flags which episodes ship a podcast:transcript (read it, skip ASR) vs which need eye_transcribe (free key/secret)"
    cache_ttl = 900
    kind = "lookup"
    domains = ["podcast"]
    modes = ["STRUCTURE", "TRANSCRIBE"]

    # ── credentials ─────────────────────────────────────────────────────────
    def _creds(self) -> Optional[tuple[str, str]]:
        """Return (key, secret) from ~/.polaris/credentials/podcastindex.json, or None."""
        creds = auth.load("podcastindex") or {}
        key = (creds.get("key") or "").strip()
        secret = (creds.get("secret") or "").strip()
        if not key or not secret:
            return None
        return key, secret

    # ── hooks ───────────────────────────────────────────────────────────────
    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        creds = self._creds()
        if creds is None:
            logger.info("podcast_index: credentials not configured (see "
                        "~/.polaris/credentials/podcastindex.json.template)")
            return None  # → [] (the contract); no key means no live call
        key, secret = creds
        return http.get_json(
            SEARCH_URL,
            params={"q": query, "max": max(1, min(limit, 1000))},
            headers=_auth_headers(key, secret),
            timeout=TIMEOUT,
        )

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[PolarisDocument]:
        if not isinstance(raw, dict):
            return []
        feeds = raw.get("feeds") or []
        if not isinstance(feeds, list):
            return []
        docs: list[PolarisDocument] = []
        for idx, feed in enumerate(feeds[:limit]):
            if not isinstance(feed, dict):
                continue
            doc = self._feed_to_doc(feed, enrich=(idx == 0), limit=limit)
            if doc is not None:
                docs.append(doc)
        return docs

    # ── feed → doc ────────────────────────────────────────────────────────────
    def _feed_to_doc(self, feed: dict, *, enrich: bool, limit: int) -> Optional[PolarisDocument]:
        title = (feed.get("title") or "").strip()
        feed_id = feed.get("id")
        if not title and feed_id is None:
            return None
        if not title:
            title = str(feed_id)

        rss_url = (feed.get("url") or "").strip()
        homepage = (feed.get("link") or "").strip()
        page_url = f"{PODCAST_PAGE}/{feed_id}" if feed_id is not None else ""
        # Canonical URL: the Podcast Index page if we have an id, else homepage, else RSS.
        url = page_url or homepage or rss_url

        author = (feed.get("author") or feed.get("ownerName") or "").strip() or None
        date = _unix_to_dt(feed.get("newestItemPubdate"))
        categories = _categories(feed.get("categories"))
        episode_count = feed.get("episodeCount")
        description = (feed.get("description") or "").strip()

        # Content is the discovery/TRANSCRIBE breadcrumb (a podcast feed has little
        # readable body): the show blurb + the RSS feed + how transcript routing works.
        content_lines: list[str] = []
        if description:
            content_lines.append(description)
        if categories:
            content_lines.append("Categories: " + ", ".join(categories))
        if isinstance(episode_count, int):
            content_lines.append(f"Episodes: {episode_count}")
        if rss_url:
            content_lines.append(f"RSS feed: {rss_url}")

        metadata: dict[str, Any] = {
            "feedId": feed_id,
            "rssUrl": rss_url or None,
            "homepage": homepage or None,
            "ownerName": feed.get("ownerName") or None,
            "language": feed.get("language") or None,
            "itunesId": feed.get("itunesId"),
            "image": feed.get("image") or feed.get("artwork") or None,
            "raw": jsonsafe(feed),
        }

        # ONE-HOP transcript routing for the TOP show only (cheap, like apple_podcasts'
        # latest-episode enrichment): split recent episodes into already-transcribed
        # (read the transcript, skip ASR) vs enclosure-only (eye_transcribe candidates).
        if enrich and feed_id is not None:
            routed = self._episode_transcript_routing(feed_id, limit)
            if routed is not None:
                with_t, asr = routed
                metadata["transcript_episodes"] = with_t  # already ship podcast:transcript
                metadata["asr_episodes"] = asr            # enclosure-only → eye_transcribe
                content_lines.append(
                    f"Recent episodes: {len(with_t)} already ship a transcript "
                    f"(read it, skip ASR), {len(asr)} are audio-only (eye_transcribe candidates)."
                )
                if with_t:
                    t0 = with_t[0]
                    content_lines.append(
                        f"Transcript example: '{t0.get('title')}' -> {t0.get('transcript_url')}"
                    )
                if asr:
                    a0 = asr[0]
                    content_lines.append(
                        f"ASR candidate: '{a0.get('title')}' (audio for eye_transcribe) -> {a0.get('enclosure_url')}"
                    )

        content = "\n\n".join(content_lines)

        # episode_count is the catalogue size: a mechanical engagement-ish FACT (how much
        # there is to transcribe / read), not a judgment of quality.
        signals = mk_signal(
            "episode_count", episode_count, kind="engagement",
            by="podcast_index/episodeCount", unit="episodes",
        )

        return PolarisDocument(
            source=self.name,
            source_id=str(feed_id) if feed_id is not None else (rss_url or title),
            url=url,
            title=title,
            content=content,
            author=author,
            date=date,
            signals=signals,
            tags=categories,
            metadata=metadata,
        )

    def _episode_transcript_routing(
        self, feed_id: Any, limit: int
    ) -> Optional[tuple[list[dict], list[dict]]]:
        """One hop to /episodes/byfeedid for the top show: classify recent episodes into
        (already-ships-a-transcript, enclosure-only). Best-effort: any failure returns None
        (the show doc still stands without the routing extra)."""
        creds = self._creds()
        if creds is None:
            return None
        key, secret = creds
        raw = http.get_json(
            EPISODES_URL,
            params={"id": feed_id, "max": max(1, min(limit, 50))},
            headers=_auth_headers(key, secret),
            timeout=TIMEOUT,
        )
        if not isinstance(raw, dict):
            return None
        items = raw.get("items") or []
        if not isinstance(items, list):
            return None

        with_transcript: list[dict] = []
        asr_only: list[dict] = []
        for ep in items:
            if not isinstance(ep, dict):
                continue
            ep_title = (ep.get("title") or "").strip()
            enclosure = (ep.get("enclosureUrl") or "").strip()
            t_url = _first_transcript_url(ep)
            if t_url:
                with_transcript.append({
                    "id": ep.get("id"),
                    "title": ep_title,
                    "transcript_url": t_url,
                    "enclosure_url": enclosure or None,
                })
            elif enclosure:
                asr_only.append({
                    "id": ep.get("id"),
                    "title": ep_title,
                    "enclosure_url": enclosure,
                })
        return with_transcript, asr_only

    # ── liveness ────────────────────────────────────────────────────────────
    def health_check(self) -> tuple[bool, str]:
        if not auth.is_configured("podcastindex"):
            return (False,
                    "credentials not configured (register a free key at "
                    "https://api.podcastindex.org/signup -> ~/.polaris/credentials/podcastindex.json)")
        if self._creds() is None:
            return False, "podcastindex.json present but missing key/secret"
        raw = self._raw_fetch("test", 1)
        if raw is None:
            return False, "search call failed (key rejected or network)"
        if isinstance(raw, dict) and str(raw.get("status")).lower() in ("true", "1"):
            return True, "OK"
        return True, "OK (responded)"


def _first_transcript_url(ep: dict) -> Optional[str]:
    """Return the episode's podcast:transcript URL if it ships one. The richer
    ``transcripts`` array (``[{url, type}]``) is preferred per the API docs; fall
    back to the single ``transcriptUrl``. None if the episode publishes neither."""
    transcripts = ep.get("transcripts")
    if isinstance(transcripts, list):
        for t in transcripts:
            if isinstance(t, dict):
                u = (t.get("url") or "").strip()
                if u:
                    return u
    single = ep.get("transcriptUrl")
    if isinstance(single, str) and single.strip():
        return single.strip()
    return None


def _categories(cats: Any) -> list[str]:
    """Podcast Index ``categories`` is an object ``{catId: name}``; pull the names.
    Tolerates a list-of-strings or missing value too."""
    out: list[str] = []
    if isinstance(cats, dict):
        out = [str(v).strip() for v in cats.values() if isinstance(v, str) and v.strip()]
    elif isinstance(cats, list):
        out = [c.strip() for c in cats if isinstance(c, str) and c.strip()]
    return out


def _unix_to_dt(ts: Any) -> Optional[datetime]:
    """Podcast Index timestamps (``newestItemPubdate``) are unix seconds. None on miss."""
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
