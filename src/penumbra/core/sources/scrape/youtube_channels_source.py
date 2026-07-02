"""Curated YouTube channels — latest uploads via yt-dlp (innertube), NOT RSS.

YouTube's ``/feeds/videos.xml?channel_id=`` RSS endpoint started returning 404 to
this host (verified 2026-06 across all four channels, both UAs, and consent
cookies — a host/endpoint-level denial, not a bad channel id). yt-dlp's
flat-playlist extraction hits the innertube API instead — the SAME mechanism the
healthy ``youtube`` search adapter uses — which works. We pull each channel's
recent uploads, normalize to Document (thumbnail ``media`` + ``raw``
escape hatch), and keyword-filter.

URL fetch is intentionally deferred to the dedicated ``youtube`` adapter (it adds
transcripts); this adapter's value is the latest-from-curated-channels stream.
"""

from __future__ import annotations

import logging
from typing import Optional

import yt_dlp

from penumbra.core import cache
from penumbra.core.normalize import Document, jsonsafe, keyword_score_filter, mk_signal

logger = logging.getLogger(__name__)

# (channel_id, display_name) — MLST / Yannic Kilcher / 3Blue1Brown / Dwarkesh.
CHANNELS = [
    ("UCMLtBahI5DMrt0NPvDSoIRQ", "Machine Learning Street Talk"),
    ("UCZHmQk67mSJgfCCTn7xBfew", "Yannic Kilcher"),
    ("UCYO_jab_esuFRV4b17AJtAw", "3Blue1Brown"),
    ("UCXl4i9dYBrFOabk0xGmbkRA", "Dwarkesh Patel"),
    ("UCJgIbYl6C5no72a0NUAPcTA", "GPU MODE"),
]
PER_CHANNEL = 15
CACHE_TTL = 3600

_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,   # list uploads without per-video extraction (fast)
    "skip_download": True,
    "playlistend": PER_CHANNEL,
    "socket_timeout": 10,
}


class YoutubeChannelsAdapter:
    name = "youtube_channels"
    needs_credentials = False
    description = (
        "Curated YouTube channels — MLST / Yannic Kilcher / 3Blue1Brown / Dwarkesh / GPU MODE "
        "(latest uploads via yt-dlp; the RSS endpoint is IP-blocked for this host)"
    )

    def _fetch_channel(self, channel_id: str, display: str) -> list[dict]:
        key = cache.make_key("youtube_channels", "chan", channel_id, PER_CHANNEL)
        cached = cache.get(key)
        if cached is not None:
            return cached
        url = f"https://www.youtube.com/channel/{channel_id}/videos"
        out: list[dict] = []
        try:
            with yt_dlp.YoutubeDL(_YDL_OPTS) as ydl:
                info = ydl.extract_info(url, download=False)
            for e in (info.get("entries") or []):
                if not e or not e.get("id"):
                    continue
                out.append({
                    "id": e.get("id"),
                    "title": e.get("title") or "(untitled)",
                    "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
                    "channel": display,
                    "channel_id": channel_id,
                    "duration": e.get("duration"),
                    "view_count": e.get("view_count"),
                })
        except Exception as exc:  # noqa: BLE001 — one bad channel must not kill the rest
            logger.warning("youtube_channels: yt-dlp failed for %s (%s): %s", display, channel_id, exc)
            return []
        cache.set(key, out, ttl=CACHE_TTL)
        return out

    def _to_doc(self, e: dict) -> Document:
        vid = e["id"]
        # YouTube's per-video thumbnail URL is stable and always valid.
        thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        dur = e.get("duration")
        content = f"{e.get('channel', '')} — YouTube video" + (f" ({int(dur)}s)" if dur else "")
        return Document(
            source="youtube_channels",
            source_id=str(vid),
            url=e.get("url") or f"https://www.youtube.com/watch?v={vid}",
            title=e["title"],
            content=content,
            author=e.get("channel"),
            signals=mk_signal("views", e.get("view_count"),
                              kind="engagement", by="youtube_channels/view_count"),
            tags=["youtube", e.get("channel", "")],
            media=[thumb],
            metadata={
                "channel": e.get("channel"),
                "channel_id": e.get("channel_id"),
                "video_id": vid,
                "view_count": e.get("view_count"),
                "raw": jsonsafe(e),
            },
        )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        docs: list[Document] = []
        for cid, disp in CHANNELS:
            for e in self._fetch_channel(cid, disp):
                docs.append(self._to_doc(e))
        q = (query or "").strip()
        if q:
            return keyword_score_filter(docs, q)[:limit]
        return docs[:limit]

    def fetch_url(self, url: str) -> Optional[Document]:
        # Defer YouTube URL fetches to the dedicated `youtube` adapter (it adds
        # transcripts). This source only provides the curated-channel stream.
        return None

    def health_check(self) -> tuple[bool, str]:
        # Probe ONE channel (3Blue1Brown) — fast yt-dlp flat extract; the outer
        # bounded-probe primitive caps it regardless.
        cid, disp = CHANNELS[2]
        try:
            entries = self._fetch_channel(cid, disp)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        if entries:
            return True, f"OK (yt-dlp; {len(entries)} videos from {disp})"
        return False, f"yt-dlp returned 0 videos for {disp}"


from penumbra.core.fetcher import register_adapter

register_adapter(YoutubeChannelsAdapter())
