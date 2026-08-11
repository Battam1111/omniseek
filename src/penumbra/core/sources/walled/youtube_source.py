"""YouTube adapter — yt-dlp for search + comments, youtube-transcript-api for captions.

We treat YouTube as a "walled-garden-lite": no login required, but the
search, comment, and transcript extraction need specialized tooling. yt-dlp is
the de-facto standard for extracting YouTube metadata and comments;
youtube-transcript-api extracts auto-generated or manual captions.

Search uses yt-dlp's `ytsearch<N>:<query>` URL form. This is slower than
the YouTube Data API but needs no key. If the user wants higher throughput,
they can drop a YouTube Data API v3 key at ~/.penumbra/credentials/youtube.json
— Phase 3.5 enhancement, not done yet.

Transcripts are pulled lazily — only when a doc's `fetch_url()` is called
or when explicitly requested. Pulling transcripts for every search result
would 5-10x the search latency.

Comments are pulled via yt-dlp `getcomments=True` (it scrapes the YouTube
comment thread, no key needed) and surface in TWO shapes, both bounded to the
top ~50:
  * fetch_url(<video>) folds a short comment preview onto the single video doc
    (alongside the transcript — the transcript behaviour is preserved verbatim).
  * search(<video url or 11-char id>) returns the top comments as SEPARATE docs
    (one Document per comment, content=text, author, signals=like_count,
    url=the video url). This mirrors the arXiv "query is an id → by-id lookup"
    convenience: a plain free-text query never matches the strict id/URL detector,
    so ordinary video search (and broad fan-out) is byte-identical to before.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse, parse_qs

from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)

from penumbra.core import cache
from penumbra.core.normalize import Document, jsonsafe, mk_signal

# Single API instance reused across calls
_TRANSCRIPT_API = YouTubeTranscriptApi()

logger = logging.getLogger(__name__)


def _ytdlp():
    """Lazy yt_dlp handle: keeps the heavy lib off the startup import path (this adapter
    self-registers at boot, so a module-level import loaded yt_dlp on every service start)."""
    import yt_dlp
    return yt_dlp

# Top-N comments cap — keep comment extraction bounded (yt-dlp would otherwise
# page the WHOLE thread, which on a popular video is thousands of round trips).
_MAX_COMMENTS = 50

# yt-dlp options for search: lean, fast, no download
_YDL_SEARCH_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "extract_flat": True,  # Just metadata, don't dive into each video
    "default_search": "ytsearch",
    "noplaylist": True,
}

# yt-dlp options for single-video info fetch
_YDL_INFO_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
}

# yt-dlp options for comment extraction: like _YDL_INFO_OPTS, plus getcomments
# and a hard max_comments cap so we never page the entire thread.
_YDL_COMMENTS_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "getcomments": True,
    "extractor_args": {"youtube": {"max_comments": [str(_MAX_COMMENTS)]}},
}


def _extract_video_id(url_or_id: str) -> Optional[str]:
    """Extract YouTube video ID from a URL or accept a raw 11-char ID."""
    if re.match(r"^[A-Za-z0-9_-]{11}$", url_or_id):
        return url_or_id
    parsed = urlparse(url_or_id)
    host = parsed.hostname or ""
    if "youtu.be" in host:
        return parsed.path.lstrip("/")
    if "youtube.com" in host:
        qs = parse_qs(parsed.query)
        v = qs.get("v")
        if v:
            return v[0]
        # /shorts/<id> or /embed/<id>
        m = re.match(r"^/(shorts|embed)/([A-Za-z0-9_-]{11})", parsed.path)
        if m:
            return m.group(2)
    return None


def _looks_like_video_ref(s: str) -> Optional[str]:
    """Return a video id IFF ``s`` is, on its own, a YouTube video URL or a bare
    11-char id (i.e. a single-token reference, not a free-text search query).

    Used to route ``search(<video ref>)`` to comment extraction without ever
    catching an ordinary keyword query: a query with whitespace is never a ref,
    and a bare token must match the 11-char id shape or be a youtube host URL."""
    s = (s or "").strip()
    if not s or any(c.isspace() for c in s):
        return None  # multi-word query → ordinary video search, never comments
    host = (urlparse(s).hostname or "")
    if "youtube.com" in host or "youtu.be" in host:
        return _extract_video_id(s)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    return None


def _fetch_comments(video_id: str, limit: int = _MAX_COMMENTS) -> list[dict]:
    """Pull up to ``limit`` (capped at _MAX_COMMENTS) top comments for a video via
    yt-dlp. Returns the raw comment dicts (text / author / like_count / timestamp /
    parent / is_pinned / ...), top-level first. Empty list on any failure or when
    comments are disabled. 6h-cached (comment counts drift but slowly).

    yt-dlp's ``getcomments`` returns ``info["comments"]`` — a flat list mixing
    top-level (parent == "root") and reply comments. We keep top-level first and
    promote any pinned comment to the front (highest-signal)."""
    cap = max(1, min(limit, _MAX_COMMENTS))
    key = cache.make_key("youtube", "comments", video_id, cap)
    cached = cache.get(key)
    if cached is not None:
        return cached  # may be [] — a known "no comments / disabled" memo

    opts = dict(_YDL_COMMENTS_OPTS)
    opts["extractor_args"] = {"youtube": {"max_comments": [str(cap)]}}
    try:
        with _ytdlp().YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("YouTube comment fetch failed for %s: %s", video_id, exc)
        return []

    comments = (info or {}).get("comments") or []
    if not isinstance(comments, list):
        comments = []
    # Order: pinned first, then top-level (parent == "root"), then the rest —
    # so the bounded slice keeps the highest-signal comments.
    def _rank(c: dict) -> tuple:
        pinned = 0 if c.get("is_pinned") else 1
        toplevel = 0 if (c.get("parent") in (None, "root")) else 1
        likes = c.get("like_count") or 0
        return (pinned, toplevel, -(likes if isinstance(likes, (int, float)) else 0))

    ranked = sorted((c for c in comments if isinstance(c, dict)), key=_rank)[:cap]
    cache.set(key, ranked, ttl=6 * 3600)
    return ranked


def _comment_to_document(comment: dict, video_id: str, video_url: str) -> Document:
    """One yt-dlp comment dict → one Document (content=text, author,
    signals=like_count, url=the video url)."""
    text = (comment.get("text") or "").strip()
    author = comment.get("author") or None
    cid = str(comment.get("id") or "")

    date = None
    ts = comment.get("timestamp")
    if isinstance(ts, (int, float)):
        try:
            date = datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            date = None

    return Document(
        source="youtube",
        source_id=f"{video_id}:comment:{cid}" if cid else f"{video_id}:comment",
        url=video_url,  # a comment has no standalone URL; point at the video
        title=f"Comment by {author or 'unknown'} on {video_id}",
        content=text or "(empty comment)",
        author=author,
        date=date,
        signals=mk_signal("likes", comment.get("like_count"),
                          kind="engagement", by="youtube/comment_like_count"),
        metadata={
            "kind": "comment",
            "video_id": video_id,
            "comment_id": cid or None,
            "parent": comment.get("parent"),
            "is_pinned": bool(comment.get("is_pinned")),
            "is_favorited": bool(comment.get("is_favorited")),
            "author_is_uploader": bool(comment.get("author_is_uploader")),
            "author_is_verified": bool(comment.get("author_is_verified")),
            "author_url": comment.get("author_url"),
            "like_count": comment.get("like_count"),
            "raw": jsonsafe(comment),
        },
    )


def _fetch_transcript(video_id: str, prefer_languages=("en", "zh-CN", "zh-Hans", "zh")) -> Optional[str]:
    """Try to fetch a transcript in any of the preferred languages.

    Returns the transcript as a single newline-joined string, or None if
    no transcript exists / video disabled subtitles / video unavailable.

    Uses youtube-transcript-api 1.x API:
        api = YouTubeTranscriptApi()
        api.list(video_id)  -> TranscriptList
        api.fetch(video_id, languages=[...]) -> FetchedTranscript (iter of FetchedTranscriptSnippet)
    """
    key = cache.make_key("youtube", "transcript", video_id)
    cached = cache.get(key)
    if cached is not None:
        return cached or None  # cached '' means we already know there's no transcript

    # First try preferred languages list directly (api.fetch handles fallback within the list)
    try:
        fetched = _TRANSCRIPT_API.fetch(video_id, languages=list(prefer_languages))
    except (TranscriptsDisabled, VideoUnavailable):
        # AUTHORITATIVE no-transcript (captions genuinely disabled / video unavailable): a durable fact,
        # so keep the 24h '' sentinel past the empty-FLOOR (re-checking every 5min would only hammer the
        # transcript API for something that will not change).
        cache.set(key, "", ttl=86400, authoritative_empty=True)
        return None
    except NoTranscriptFound:
        # None of preferred languages found — try any available
        try:
            tlist = _TRANSCRIPT_API.list(video_id)
            # Iterate to find first available, accept any language
            chosen = None
            for t in tlist:
                chosen = t
                break
            if chosen is None:
                # AUTHORITATIVE: listed transcripts successfully, there are none → durable fact, keep 24h.
                cache.set(key, "", ttl=86400, authoritative_empty=True)
                return None
            fetched = chosen.fetch()
        except Exception as exc:  # noqa: BLE001
            # TRANSIENT: the list()/fetch() fallback RAISED (network / API error), NOT a proven
            # no-transcript. Leave this to the cache.set empty-FLOOR: the '' self-heals in EMPTY_TTL_CAP
            # instead of masking a transient failure as "no transcript" for 24h (the sibling of the
            # enrich-null-cached-24h bug). Do NOT pass authoritative_empty here.
            logger.debug("Transcript fallback failed for %s: %s", video_id, exc)
            cache.set(key, "", ttl=86400)
            return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("Transcript fetch raised for %s: %s", video_id, exc)
        return None

    # fetched is iterable of FetchedTranscriptSnippet(text=..., start=..., duration=...)
    try:
        text = "\n".join(s.text.strip() for s in fetched if s.text and s.text.strip())
    except Exception as exc:  # noqa: BLE001
        logger.debug("Transcript snippet iteration failed for %s: %s", video_id, exc)
        return None

    cache.set(key, text, ttl=7 * 86400)  # transcripts rarely change; 7-day cache
    return text or None


class YouTubeAdapter:
    name = "youtube"
    needs_credentials = False
    description = (
        "YouTube — video search + transcript + top comments (PhD methodology channels, "
        "lectures, talks; pass a video URL/id as the query to get its comments as docs)"
    )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        # Routing convenience (arXiv id_list precedent): if the query is itself a
        # single YouTube video URL or 11-char id, return that video's TOP COMMENTS
        # as separate docs. A multi-word / non-id query never matches, so ordinary
        # keyword video search (and broad fan-out) is byte-identical to before.
        ref_id = _looks_like_video_ref(query)
        if ref_id:
            return self._comments_search(ref_id, limit)

        key = cache.make_key("youtube", "search", query, limit)
        cached = cache.get(key)
        if cached is not None:
            return [Document.model_validate(d) for d in cached]

        # ytsearch<N>:<query> tells yt-dlp to do a YouTube search and return N results
        search_url = f"ytsearch{min(limit, 25)}:{query}"
        try:
            with _ytdlp().YoutubeDL(_YDL_SEARCH_OPTS) as ydl:
                info = ydl.extract_info(search_url, download=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("YouTube search failed: %s", exc)
            return []

        entries = (info or {}).get("entries") or []
        docs: list[Document] = []
        for entry in entries[:limit]:
            if not entry:
                continue
            try:
                docs.append(self._entry_to_document(entry))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping YouTube entry: %s", exc)

        # A yt-dlp/network-failure empty must not pin [] for 30m (masks the outage); a genuine empty
        # re-checks in 5m. (The "" transcript sentinels above are deliberate negative caches, untouched.)
        cache.set(key, [d.model_dump(mode="json") for d in docs], ttl=1800 if docs else 300)
        return docs

    def _comments_search(self, video_id: str, limit: int) -> list[Document]:
        """Return up to ``limit`` top comments for one video, each as a doc."""
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        comments = _fetch_comments(video_id, limit=min(max(limit, 1), _MAX_COMMENTS))
        docs: list[Document] = []
        for c in comments[:limit]:
            try:
                docs.append(_comment_to_document(c, video_id, video_url))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping YouTube comment: %s", exc)
        return docs

    def fetch_url(self, url: str) -> Optional[Document]:
        video_id = _extract_video_id(url)
        if not video_id:
            return None

        # Pull full video info + transcript
        try:
            with _ytdlp().YoutubeDL(_YDL_INFO_OPTS) as ydl:
                info = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={video_id}", download=False
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("YouTube fetch_url failed for %s: %s", url, exc)
            return None

        doc = self._entry_to_document(info, include_full_description=True)
        # Augment with transcript (this is the high-value content) — PRESERVED verbatim.
        transcript = _fetch_transcript(video_id)
        if transcript:
            doc.content = doc.content + "\n\n## Transcript\n\n" + transcript
            doc.metadata["has_transcript"] = True
            doc.metadata["transcript_chars"] = len(transcript)
        else:
            doc.metadata["has_transcript"] = False

        # Augment with a bounded TOP-COMMENTS preview on the single video doc.
        # (The standalone per-comment docs come from search(<video ref>); here we
        # just surface the discussion inline so the drill-down doc carries it too.)
        try:
            comments = _fetch_comments(video_id, limit=_MAX_COMMENTS)
        except Exception:  # noqa: BLE001
            comments = []
        if comments:
            preview_lines = []
            for c in comments[:_MAX_COMMENTS]:
                text = (c.get("text") or "").strip().replace("\n", " ")
                if not text:
                    continue
                author = c.get("author") or "unknown"
                likes = c.get("like_count")
                likes_s = f" ({likes} likes)" if isinstance(likes, (int, float)) else ""
                preview_lines.append(f"- **{author}**{likes_s}: {text}")
            if preview_lines:
                doc.content = (
                    doc.content
                    + "\n\n## Top comments\n\n"
                    + "\n".join(preview_lines)
                )
            doc.metadata["comments_fetched"] = len(comments)
        else:
            doc.metadata["comments_fetched"] = 0
        return doc

    def health_check(self) -> tuple[bool, str]:
        # Probe with a tiny search
        try:
            with _ytdlp().YoutubeDL(_YDL_SEARCH_OPTS) as ydl:
                info = ydl.extract_info("ytsearch1:research methodology", download=False)
            if info and info.get("entries"):
                return True, "OK"
            return False, "empty response"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:100]}"

    @staticmethod
    def _entry_to_document(entry: dict, include_full_description: bool = False) -> Document:
        video_id = entry.get("id") or _extract_video_id(entry.get("url") or "")
        url = entry.get("webpage_url") or (
            f"https://www.youtube.com/watch?v={video_id}" if video_id else entry.get("url", "")
        )

        title = entry.get("title") or "(untitled)"
        description = entry.get("description") or ""

        author = entry.get("uploader") or entry.get("channel") or None

        date = None
        upload_date = entry.get("upload_date")
        if upload_date and len(upload_date) == 8:
            try:
                date = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
            except ValueError:
                date = None
        # release_timestamp is unix epoch
        ts = entry.get("release_timestamp") or entry.get("timestamp")
        if ts and not date:
            try:
                date = datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, OSError):
                pass

        # Thumbnail URL — a vision-capable agent can view the video's cover.
        # yt-dlp gives either a single `thumbnail` or a `thumbnails` list of
        # {url, ...}; take the highest-res available, defensively.
        media: list[str] = []
        thumb = entry.get("thumbnail")
        if isinstance(thumb, str) and thumb.startswith("http"):
            media.append(thumb)
        elif not thumb:
            thumbs = entry.get("thumbnails")
            if isinstance(thumbs, list) and thumbs:
                last = thumbs[-1]
                if isinstance(last, dict):
                    u = last.get("url")
                    if isinstance(u, str) and u.startswith("http"):
                        media.append(u)

        return Document(
            source="youtube",
            source_id=video_id or url,
            url=url,
            title=title,
            content=description or "(no description)",
            author=author,
            date=date,
            signals=mk_signal("views", entry.get("view_count"),
                              kind="engagement", by="youtube/view_count"),
            tags=entry.get("categories") or [],
            media=media,
            metadata={
                "channel": entry.get("channel") or entry.get("uploader"),
                "channel_id": entry.get("channel_id"),
                "duration": entry.get("duration"),
                "view_count": entry.get("view_count"),
                "like_count": entry.get("like_count"),
                "comment_count": entry.get("comment_count"),
                "subtitle_languages": list((entry.get("subtitles") or {}).keys()),
                "automatic_caption_languages": list((entry.get("automatic_captions") or {}).keys()),
                "raw": jsonsafe(entry),
            },
        )


from penumbra.core.fetcher import register_adapter

register_adapter(YouTubeAdapter())
