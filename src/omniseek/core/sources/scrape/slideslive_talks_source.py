"""SlidesLive: recorded conference TALKS (NeurIPS / ICML / ICLR / ACL / ...).

SlidesLive is the de-facto video host for the big ML/NLP conferences' VIRTUAL
content. Most of these talks are NOT on YouTube and ship NO machine transcript,
so they are invisible to ordinary web search and to caption-based tooling: a
paper's authors explain the work in a 5 to 15 minute talk that exists only as a
SlidesLive embed behind a conference virtual portal. The eye's TRANSCRIBE mode
unlocks them: yt-dlp (already a dep, with a maintained ``slideslive`` extractor)
resolves a talk's audio stream, which ``omniseek_transcribe`` then feeds to local
SenseVoice ASR. This adapter is the discovery + resolve layer in front of that.

SCOPE: two entry points, one for discovery and one for depth.
  * KEYWORD SEARCH (discovery): a free-text query hits SlidesLive's PUBLIC, unauthenticated
    global library search (``/search/presentations``, server-rendered HTML, 20 results/page,
    ``&page=N`` pagination; verified 2026-07-14) and returns LIGHTWEIGHT hits (talk id / url /
    title / thumbnail / duration badge) in server-relevance order. The agent then drills a
    specific hit for full metadata (omniseek_read / the URL-resolve path below) or transcribes it
    (omniseek_transcribe). This indexes the SlidesLive LIBRARY; talks that live only behind a
    conference virtual-portal and never publish to that library still need a URL handed in.
  * URL / ID RESOLVE (depth): given a SlidesLive talk URL or a bare numeric talk id, it
    returns that talk's full metadata (title / duration / upload date / per-slide chapters /
    thumbnail) and, crucially, surfaces the audio-stream handle so the agent can TRANSCRIBE it.
    If the talk ships SlidesLive captions (often ``en``), they are folded inline, so STRUCTURE
    mode already has text without paying for ASR.

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

from omniseek.core.normalize import Document, mk_signal
from omniseek.core.sources.scrape._base import BaseScrapeAdapter, _SCRAPE_UA

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

# Public, unauthenticated global LIBRARY search (verified 2026-07-14): a server-rendered HTML
# page whose <turbo-frame id="search_presentations_results"> holds up to 20 result cards, with
# ``&page=N`` pagination. Each card carries a canonical talk anchor (``?ref=search-presentations``),
# an <img> thumbnail (+ alt title), an <h4> title anchor, and a MM:SS duration badge beside the thumb.
_SEARCH_URL = "https://slideslive.com/search/presentations?query={query}&page={page}"
_RESULTS_PER_PAGE = 20
# A result-card talk link: slideslive.com/<numeric-id>/<slug>?ref=search-presentations.
_SEARCH_HREF_RE = re.compile(r"slideslive\.com/(\d+)/[^\"?]+\?ref=search-presentations")
_DURATION_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")  # a talk-length badge (MM:SS or H:MM:SS)


def _talk_id(s: str) -> Optional[str]:
    """Return a SlidesLive numeric talk id IFF ``s`` is a SlidesLive talk URL or a
    bare numeric id on its own (a single-token reference, never a keyword query).
    A query with whitespace, or any non-SlidesLive token, returns None: that routes
    to the keyword LIBRARY search instead of the URL-resolve path (see _raw_fetch)."""
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


def _parse_search_fragment(html: str) -> list[dict]:
    """Pure parse (NO network, golden-testable) of a SlidesLive library-search page/fragment
    into result dicts ``{id, url, title, thumbnail?, duration?}`` in server-relevance order,
    deduped by id. Each talk surfaces TWICE per card (a thumbnail anchor + an <h4> title anchor
    sharing one ``?ref=search-presentations`` href); we merge them by id, taking the title from
    whichever anchor carries text (or the thumbnail's alt), the thumbnail from the card's <img>,
    and the MM:SS badge sitting beside the thumbnail. An empty page (no result cards) yields
    ``[]`` -- the pagination stop signal."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    # Narrow to the results turbo-frame when present so unrelated links can never leak in; fall
    # back to the whole document (a bare inner fragment has no wrapping <turbo-frame>).
    root = soup.find("turbo-frame", id="search_presentations_results") or soup
    results: dict[str, dict] = {}
    order: list[str] = []
    for a in root.find_all("a", href=_SEARCH_HREF_RE):
        m = _SEARCH_HREF_RE.search(a.get("href") or "")
        if not m:
            continue
        tid = m.group(1)
        rec = results.get(tid)
        if rec is None:
            rec = {"id": tid, "url": (a.get("href") or "").split("?", 1)[0]}
            results[tid] = rec
            order.append(tid)
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        if text and not rec.get("title"):
            rec["title"] = text
        img = a.find("img")
        if img is not None:
            src = img.get("src")
            if src and not rec.get("thumbnail"):
                rec["thumbnail"] = src
            alt = re.sub(r"\s+", " ", img.get("alt") or "").strip()
            if alt and not rec.get("title"):
                rec["title"] = alt
            parent = a.parent  # the thumbnail anchor's box also holds the duration badge
            if parent is not None and not rec.get("duration"):
                badge = parent.find(string=_DURATION_RE)
                if badge:
                    rec["duration"] = badge.strip()
    return [results[t] for t in order]


class SlidesLiveTalksAdapter(BaseScrapeAdapter):
    name = "slideslive_talks"
    needs_credentials = False
    description = (
        "SlidesLive: recorded conference talks (NeurIPS/ICML/ICLR/ACL) absent from "
        "YouTube and untranscribed. Keyword-search the SlidesLive library for talks, or "
        "pass a talk URL / numeric id to resolve its audio for TRANSCRIBE (local ASR)"
    )
    cache_ttl = 6 * 3600  # talk metadata is static; cache 6h (yt-dlp resolve is not free)

    # routing facets (the router reads these class attrs; do NOT touch facets.json)
    kind = "lookup"
    domains = ["papers", "video"]
    modes = ["TRANSCRIBE", "STRUCTURE"]

    # --------------------------------------------------------------- hooks
    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """Route the query. A SlidesLive URL / bare numeric id -> the yt-dlp resolve path (full
        metadata for that one talk, a dict). Any other non-empty keyword -> the PUBLIC library
        search (a list of lightweight hit dicts). Empty query -> None. Returning None ⇒ [] (the
        adapter contract); an empty search list is collapsed to None so no miss is cached."""
        talk_id = _talk_id(query)
        if talk_id:
            return self._extract_info(_canonical_url(talk_id))
        if query and query.strip():
            return self._search_presentations(query, limit) or None
        return None

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if isinstance(raw, list):  # keyword-search hits -> lightweight docs (drill the URL for depth)
            docs = [self._search_result_to_document(r) for r in raw[:limit]]
            return [d for d in docs if d is not None]
        if not isinstance(raw, dict):
            return []
        doc = self._info_to_document(raw)
        return [doc] if doc is not None else []

    def fetch_url(self, url: str) -> Optional[Document]:
        """Claim a slideslive.com talk URL: resolve its metadata + audio handle into
        one doc (the omniseek_read / omniseek_search 单源钻取 drill-down path). Returns None for any
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
        import yt_dlp  # lazy: heavy lib off the startup import path (this adapter self-registers at boot)
        try:
            with yt_dlp.YoutubeDL(_YDL_OPTS) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001 (failure -> None -> [], the adapter contract)
            logger.warning("slideslive resolve failed for %s: %s", url, exc)
            return None

    @staticmethod
    def _search_presentations(query: str, limit: int) -> list[dict]:
        """Page through the PUBLIC SlidesLive library search (``/search/presentations``), parse
        each 20-result page, and return up to ``limit`` result dicts (deduped by id, in
        server-relevance order). Stops at ``limit`` or the first empty page; a page fetch failure
        just ends the walk (returning what we have). Routes through the shared ``http`` helper so
        the search inherits the SSRF guard + pooled client + /eye-fix diagnostic tap; a browser UA
        is sent because SlidesLive gates the omniseek default UA."""
        from urllib.parse import quote
        from omniseek.core import http
        pages = max(1, (limit + _RESULTS_PER_PAGE - 1) // _RESULTS_PER_PAGE)
        results: list[dict] = []
        seen: set[str] = set()
        for page in range(1, pages + 1):
            url = _SEARCH_URL.format(query=quote(query), page=page)
            html = http.get_text(url, timeout=20,
                                  headers={"User-Agent": _SCRAPE_UA, "Accept": "text/html"})
            if not html:
                break  # fetch failed (None) or empty body -> stop; return what we have so far
            page_items = _parse_search_fragment(html)
            if not page_items:
                break  # first empty page -> no more library results
            for item in page_items:
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
                results.append(item)
                if len(results) >= limit:
                    return results
        return results

    @staticmethod
    def _search_result_to_document(r: dict) -> Optional[Document]:
        """Map ONE lightweight search hit ``{id, url, title, thumbnail?, duration?}`` to a
        Document. Deliberately shallow (no yt-dlp resolve, so no audio/caption/chapter
        metadata): the agent drills a specific hit via omniseek_read / the URL-resolve path for full
        metadata, or omniseek_transcribe for a transcript. ``None`` if the hit carries no id."""
        talk_id = str(r.get("id") or "")
        if not talk_id:
            return None
        url = r.get("url") or _canonical_url(talk_id)
        title = r.get("title") or "(untitled talk)"
        duration = r.get("duration")

        media: list[str] = []
        thumb = r.get("thumbnail")
        if isinstance(thumb, str) and thumb.startswith("http"):
            media.append(thumb)

        lines = [f"Recorded conference talk ({duration})." if duration
                 else "Recorded conference talk."]
        lines.append(
            "SlidesLive library search hit (lightweight). Pass this talk's URL to omniseek_read for "
            "full metadata (slide outline / captions / audio), or to omniseek_transcribe to resolve "
            "its audio and transcribe it with local ASR (the TRANSCRIBE unlock)."
        )
        lines.append(f"Talk page: {url}")

        return Document(
            source="slideslive_talks",
            source_id=talk_id,
            url=url,
            title=title,
            content="\n\n".join(lines),
            tags=["talk", "conference"],
            media=media,
            metadata={
                "talk_id": talk_id,
                "duration": duration,   # the search badge string (MM:SS); resolve the URL for seconds
                "thumbnail": thumb,
                "search_result": True,  # lightweight hit: drill the URL for full metadata
                "transcribe_url": url,  # the durable handle to pass to omniseek_transcribe
            },
        )

    @staticmethod
    def _info_to_document(info: dict) -> Optional[Document]:
        talk_id = str(info.get("id") or "")
        if not talk_id:
            return None
        url = info.get("webpage_url") or _canonical_url(talk_id)
        title = info.get("title") or "(untitled talk)"

        date = _parse_date(info)
        duration = info.get("duration")

        # The audio stream is the TRANSCRIBE handle. We surface its presence (the agent
        # transcribes by passing this doc's url to omniseek_transcribe, which runs the same
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

        return Document(
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
                "transcribe_url": url,  # the durable handle to pass to omniseek_transcribe
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
            "Use omniseek_transcribe on this talk's URL for a clean full transcript via local ASR."
        )
    elif audio_present:
        lines.append(
            "No transcript on SlidesLive: pass this talk's URL to omniseek_transcribe to "
            "resolve its audio and transcribe it with local ASR (the TRANSCRIBE unlock)."
        )
    else:
        lines.append("No resolvable audio stream found for this talk.")

    lines.append(f"Talk page: {url}")
    return "\n\n".join(lines)


# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
