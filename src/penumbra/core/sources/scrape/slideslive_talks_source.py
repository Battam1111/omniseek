"""SlidesLive: recorded conference TALKS (NeurIPS / ICML / ICLR / ACL / ...).

SlidesLive is the de-facto video host for the big ML/NLP conferences' VIRTUAL
content. Most of these talks are NOT on YouTube and ship NO machine transcript,
so they are invisible to ordinary web search and to caption-based tooling: a
paper's authors explain the work in a 5 to 15 minute talk that exists only as a
SlidesLive embed behind a conference virtual portal. The eye's TRANSCRIBE mode
unlocks them: yt-dlp (already a dep, with a maintained ``slideslive`` extractor)
resolves a talk's audio stream, which ``eye_transcribe`` then feeds to local
SenseVoice ASR. This adapter is the discovery + resolve layer in front of that.

SCOPE (be honest): this is a URL-RESOLVE adapter, not a full search engine.
  * Given a SlidesLive talk URL or a bare numeric talk id, it returns that talk's
    metadata (title / duration / upload date / per-slide chapters / thumbnail) and,
    crucially, surfaces the audio-stream handle so the agent can TRANSCRIBE it.
  * If the talk ships SlidesLive captions (often ``en``), they are folded inline,
    so STRUCTURE mode already has text without paying for ASR.
  * A free-text keyword query returns ``[]``. A real search-discovery layer
    (conference virtual-portal scrape -> embed ids) is a deliberate FOLLOW-UP, not
    shipped here: the portals are JS-rendered, per-conference, and frequently
    login-gated, which is its own unit. The honest contract: the agent finds the
    SlidesLive URL (from a paper page, a conference schedule, or web search) and
    hands it here; the eye resolves + transcribes it.

yt-dlp valid-url shape (verified 2026-06-17, yt-dlp 2026.06.09):
    https?://slideslive.com/(embed/(presentation/)?)?<numeric-id>
``extract_info`` (no download) yields ``id`` / ``title`` / ``duration`` /
``timestamp`` + ``upload_date`` / ``thumbnail`` / ``chapters`` (one per slide) /
``subtitles`` (sometimes ``en``) / ``formats`` (a single ~128k m4a audio stream).
A small fraction of ids 412 with "Unable to extract player token" (a SlidesLive
player edge case yt-dlp can't always satisfy); we degrade that to None -> [].

This source does NOT subclass BaseScrapeAdapter's default ``_raw_fetch`` (it is a
yt-dlp extractor call, not an ``http.get_json``), so it overrides the two hooks and
``fetch_url`` like the ``youtube`` adapter, while still inheriting the base's
cache / atomic set_docs / self-registration ritual (the doc shape and registration
discipline match the rest of the scrape sources).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

import yt_dlp

from penumbra.core.normalize import PolarisDocument, mk_signal
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

# yt-dlp options for a metadata-only resolve: no download, lean, single talk.
_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "socket_timeout": 20,
}

# SlidesLive talk url/id shapes (mirrors yt-dlp's SlidesLiveIE._VALID_URL, plus a
# bare numeric id as a convenience the agent can pass directly).
_URL_RE = re.compile(
    r"^https?://(?:www\.)?slideslive\.com/(?:embed/(?:presentation/)?)?(\d+)", re.I
)
_BARE_ID_RE = re.compile(r"^\d{4,}$")  # >=4 digits: a SlidesLive id, not a stray small int


def _talk_id(s: str) -> Optional[str]:
    """Return a SlidesLive numeric talk id IFF ``s`` is a SlidesLive talk URL or a
    bare numeric id on its own (a single-token reference, never a keyword query).
    A query with whitespace, or any non-SlidesLive token, returns None -> [] (the
    "search-discovery is a follow-up" contract; see module docstring)."""
    s = (s or "").strip()
    if not s or any(c.isspace() for c in s):
        return None
    m = _URL_RE.match(s)
    if m:
        return m.group(1)
    if _BARE_ID_RE.match(s):
        return s
    return None


def _canonical_url(talk_id: str) -> str:
    return f"https://slideslive.com/{talk_id}"


class SlidesLiveTalksAdapter(BaseScrapeAdapter):
    name = "slideslive_talks"
    needs_credentials = False
    description = (
        "SlidesLive: recorded conference talks (NeurIPS/ICML/ICLR/ACL) absent from "
        "YouTube and untranscribed; pass a slideslive.com talk URL or numeric id to "
        "resolve its audio for TRANSCRIBE (keyword search-discovery is a follow-up)"
    )
    cache_ttl = 6 * 3600  # talk metadata is static; cache 6h (yt-dlp resolve is not free)

    # routing facets (the router reads these class attrs; do NOT touch facets.json)
    kind = "lookup"
    domains = ["papers", "video"]
    modes = ["TRANSCRIBE", "STRUCTURE"]

    # --------------------------------------------------------------- hooks
    def _raw_fetch(self, query: str, limit: int) -> Optional[dict]:
        """Resolve query -> talk id -> yt-dlp info dict. A non-URL/non-id keyword
        query returns None (search-discovery is a deliberate follow-up, see module
        docstring), which the base turns into []."""
        talk_id = _talk_id(query)
        if not talk_id:
            return None
        return self._extract_info(_canonical_url(talk_id))

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[PolarisDocument]:
        if not isinstance(raw, dict):
            return []
        doc = self._info_to_document(raw)
        return [doc] if doc is not None else []

    def fetch_url(self, url: str) -> Optional[PolarisDocument]:
        """Claim a slideslive.com talk URL: resolve its metadata + audio handle into
        one doc (the eye_add_url / eye_fetch drill-down path). Returns None for any
        non-SlidesLive url so the fetcher's host routing skips this source."""
        talk_id = _talk_id(url)
        if not talk_id:
            return None
        info = self._extract_info(_canonical_url(talk_id))
        if info is None:
            return None
        return self._info_to_document(info)

    def health_check(self) -> tuple[bool, str]:
        """A keyword probe (the base default) would always return [] here, so probe a
        cheap metadata-only resolve of one known-stable talk id instead."""
        info = self._extract_info(_canonical_url("38968057"))
        if info and info.get("title"):
            return True, "OK (yt-dlp slideslive resolve)"
        return False, "yt-dlp could not resolve the probe talk"

    # --------------------------------------------------------------- internals
    @staticmethod
    def _extract_info(url: str) -> Optional[dict]:
        """yt-dlp metadata-only resolve of one SlidesLive talk. None on any failure
        (network, the occasional 'Unable to extract player token' edge case, etc.) ->
        the base turns it into [] (the adapter contract)."""
        try:
            with yt_dlp.YoutubeDL(_YDL_OPTS) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001 (failure -> None -> [], the adapter contract)
            logger.warning("slideslive resolve failed for %s: %s", url, exc)
            return None

    @staticmethod
    def _info_to_document(info: dict) -> Optional[PolarisDocument]:
        talk_id = str(info.get("id") or "")
        if not talk_id:
            return None
        url = info.get("webpage_url") or _canonical_url(talk_id)
        title = info.get("title") or "(untitled talk)"

        date = _parse_date(info)
        duration = info.get("duration")

        # The audio stream is the TRANSCRIBE handle. We surface its presence (the agent
        # transcribes by passing this doc's url to eye_transcribe, which runs the same
        # yt-dlp bestaudio resolve under the hood; we don't ship the signed CDN url,
        # which expires; the talk url is the durable handle).
        audio_fmt = _best_audio_format(info.get("formats") or [])

        # SlidesLive sometimes ships captions (often `en`); fold them inline so STRUCTURE
        # mode has real text without paying for ASR.
        caption_langs = sorted((info.get("subtitles") or {}).keys())

        # Per-slide chapter titles are a free, lightweight outline of the talk.
        chapters = info.get("chapters") or []
        chapter_titles = [
            c.get("title") for c in chapters
            if isinstance(c, dict) and c.get("title")
        ]

        content = _build_content(
            description=info.get("description") or "",
            duration=duration,
            audio_present=audio_fmt is not None,
            caption_langs=caption_langs,
            chapter_titles=chapter_titles,
            url=url,
        )

        media: list[str] = []
        thumb = info.get("thumbnail")
        if isinstance(thumb, str) and thumb.startswith("http"):
            media.append(thumb)

        return PolarisDocument(
            source="slideslive_talks",
            source_id=talk_id,
            url=url,
            title=title,
            content=content,
            author=info.get("uploader") or None,
            date=date,
            signals=mk_signal("views", info.get("view_count"),
                              kind="engagement", by="slideslive/view_count"),
            tags=["talk", "conference"],
            media=media,
            metadata={
                "talk_id": talk_id,
                "duration_seconds": duration,
                "has_audio": audio_fmt is not None,
                "audio_format": _slim_audio(audio_fmt),  # compact: NO 200+ DASH-fragment dump
                "caption_languages": caption_langs,
                "n_slides": len(chapter_titles) or None,
                "transcribe_url": url,  # the durable handle to pass to eye_transcribe
            },
        )


# ─────────────────────────────────────────────────────────────── module helpers

def _slim_audio(fmt: Optional[dict]) -> Optional[dict]:
    """A COMPACT audio-handle summary: identifying fields only. The full yt-dlp format dict
    carries a 200+ entry DASH ``fragments`` list (plus signed urls + http headers) that would
    bloat every SlidesLive doc; keep just what names the stream. The durable transcribe handle is
    the talk URL (transcribe_url), not this dict."""
    if not isinstance(fmt, dict):
        return None
    keep = ("format_id", "ext", "abr", "tbr", "acodec", "container", "protocol")
    return {k: fmt.get(k) for k in keep if fmt.get(k) is not None} or None


def _best_audio_format(formats: list) -> Optional[dict]:
    """The audio-only format with the highest bitrate (the cleanest transcribe source),
    or any audio-bearing format if none is audio-only. None if no audio at all."""
    if not isinstance(formats, list):
        return None
    audio = [
        f for f in formats
        if isinstance(f, dict) and f.get("acodec") not in (None, "none")
    ]
    if not audio:
        return None
    audio_only = [f for f in audio if f.get("vcodec") in (None, "none")]
    pool = audio_only or audio
    return max(pool, key=lambda f: f.get("abr") or f.get("tbr") or 0)


def _parse_date(info: dict) -> Optional[datetime]:
    """Prefer the unix ``timestamp``; fall back to the ``YYYYMMDD`` ``upload_date``."""
    ts = info.get("timestamp")
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            pass
    ud = info.get("upload_date")
    if isinstance(ud, str) and len(ud) == 8 and ud.isdigit():
        try:
            return datetime.strptime(ud, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _build_content(*, description: str, duration: Optional[float], audio_present: bool,
                   caption_langs: list[str], chapter_titles: list[str], url: str) -> str:
    """Assemble the doc body: the talk's own description (often empty on SlidesLive),
    a duration line, the slide outline, captions-inline (if any) or the TRANSCRIBE
    pointer (if not)."""
    lines: list[str] = []
    desc = (description or "").strip()
    if desc:
        lines.append(desc)

    if isinstance(duration, (int, float)) and duration > 0:
        mins = int(duration) // 60
        secs = int(duration) % 60
        lines.append(f"Recorded conference talk, {mins}m{secs:02d}s.")
    else:
        lines.append("Recorded conference talk.")

    if chapter_titles:
        # Per-slide chapter titles are usually placeholders ("Slide 001"); only surface
        # them when at least one looks like real text (a titled slide), capped.
        meaningful = [t for t in chapter_titles
                      if not re.fullmatch(r"(?i)slide\s*\d+", t.strip())]
        outline = meaningful or chapter_titles
        if meaningful:
            lines.append("Slide outline: " + " | ".join(outline[:40]))

    if caption_langs:
        lines.append(
            "SlidesLive captions available (" + ", ".join(caption_langs) + "). "
            "Use eye_transcribe on this talk's URL for a clean full transcript via local ASR."
        )
    elif audio_present:
        lines.append(
            "No transcript on SlidesLive: pass this talk's URL to eye_transcribe to "
            "resolve its audio and transcribe it with local ASR (the TRANSCRIBE unlock)."
        )
    else:
        lines.append("No resolvable audio stream found for this talk.")

    lines.append(f"Talk page: {url}")
    return "\n\n".join(lines)


# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
