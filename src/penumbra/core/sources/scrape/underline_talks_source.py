"""Underline: recorded conference TALKS + video for the ACL-family venues.

Underline.io is the video host for EMNLP / NAACL / EACL / AACL / *SEM (and SIGIR /
KDD / AAAI in other years). It is the sibling gap of ``slideslive_talks``: those
NLP venues moved their virtual content to Underline, so a paper's 5 to 15 minute
author talk exists ONLY as an Underline lecture, invisible to ordinary web search
and to SlidesLive tooling. The eye's TRANSCRIBE mode unlocks it.

WHAT IS PUBLIC vs WALLED (verified live 2026-07-11, event EMNLP 2023):
  * The lecture METADATA is a public JSON:API (app.underline.io/api/v1): title,
    abstract, venue, dates, DOI, and the video ``playlist`` come back with NO auth.
    This is real STRUCTURE text (the paper abstract) without paying for ASR.
  * The video is a PUBLIC HLS master playlist on assets.underline.io (unsigned,
    HTTP 200 to a plain Chrome UA). That ``.m3u8`` is the durable TRANSCRIBE handle:
    ``penumbra_transcribe`` ffmpeg-decodes an https .m3u8 directly (its protocol whitelist
    already covers https,http,tcp,tls,crypto), so no yt-dlp extractor is needed.
  * The transcript PDF and the paper/slides downloads are LOGIN-WALLED (the
    ``downloadable_materials`` route 302s to /log-in). We do NOT claim those: the
    recon lead's "transcript fetchable without auth" was wrong (the LINK renders on
    the page, but the asset redirects to login). The paper itself lives on the ACL
    Anthology (the eye's ``acl_anthology`` source), so we only surface the DOI.

SCOPE (be honest, same contract as ``slideslive_talks``): this is a URL-RESOLVE
adapter, not a search engine. Underline exposes NO public full-text search (the
``filter[query]`` / ``filter[title]`` params are ignored, they return the unfiltered
firehose), so a free-text keyword query returns ``[]``. The agent finds the lecture
URL (from a paper page, the ACL Anthology, a conference schedule, or web search) and
hands it here; the eye resolves its metadata + video and TRANSCRIBEs it. Conference
enumeration (all talks for one event via ``filter[event_id]``) is a deliberate
FOLLOW-UP, not shipped here.

Underline lecture url/id shapes (verified 2026-07-11):
    https://underline.io/lecture/<numeric-id>-<slug>
    https://ai.underline.io/lecture/<numeric-id>-<slug>-video
    <numeric-id>            (a bare Underline lecture id the agent passes directly)
The resolve calls ``GET app.underline.io/api/v1/thin_lectures/<id>?include=tag,event``
(Accept: application/vnd.api+json) and reads ``data.attributes`` + the ``included``
event/tag. We do NOT include ``sorted_profiles`` (that relationship leaks author
emails); author is left unset rather than ingest PII for a name.

This source overrides the two BaseScrapeAdapter hooks plus ``fetch_url`` (like the
``slideslive_talks`` / ``youtube`` adapters) because the resolve is a JSON:API call,
not the base's templated ``http.get_json``; it still inherits the base's cache /
atomic set_docs / self-registration ritual.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from penumbra.core.normalize import Document
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

_API = "https://app.underline.io/api/v1/thin_lectures/{id}?include=tag,event"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/121.0 Safari/537.36")

# Underline lecture url / id shapes. The path segment after /lecture/ is
# "<id>-<slug>"; we capture the leading numeric id. Accept the www and the ai.
# subdomain (both serve the same lecture ids).
_URL_RE = re.compile(
    r"^https?://(?:www\.|ai\.)?underline\.io/lecture/(\d+)", re.I
)
_BARE_ID_RE = re.compile(r"^\d{2,}$")  # a single numeric token = an Underline lecture id


def _lecture_id(s: str) -> Optional[str]:
    """Return an Underline numeric lecture id IFF ``s`` is an Underline lecture URL or
    a bare numeric id on its own (a single-token reference, never a keyword query).
    A query with whitespace, or any non-Underline token, returns None -> [] (the
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


class UnderlineTalksAdapter(BaseScrapeAdapter):
    name = "underline_talks"
    needs_credentials = False
    description = (
        "Underline: recorded conference talks + video for the ACL-family venues "
        "(EMNLP/NAACL/EACL/AACL, also SIGIR/KDD/AAAI) that SlidesLive does not host; "
        "pass an underline.io/lecture URL or numeric id to resolve its abstract and "
        "public video for TRANSCRIBE (no public keyword search: that is a follow-up)"
    )
    cache_ttl = 6 * 3600  # lecture metadata is static; cache 6h

    # routing facets (the router reads these class attrs; do NOT touch facets.json)
    kind = "lookup"
    domains = ["papers", "video"]
    regions = ["global"]
    modes = ["TRANSCRIBE", "STRUCTURE"]

    # --------------------------------------------------------------- hooks
    def _raw_fetch(self, query: str, limit: int) -> Optional[dict]:
        """Resolve query -> lecture id -> JSON:API lecture payload. A non-URL/non-id
        keyword query returns None (Underline has no public full-text search, see
        module docstring), which the base turns into []."""
        lecture_id = _lecture_id(query)
        if not lecture_id:
            return None
        return self._resolve(lecture_id)

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, dict):
            return []
        doc = self._payload_to_document(raw)
        return [doc] if doc is not None else []

    async def _araw_fetch(self, query: str, limit: int) -> Optional[dict]:
        """Async twin of ``_raw_fetch``: resolve query -> lecture id (pure CPU) -> the JSON:API lecture
        payload via the async resolve. A non-URL/non-id keyword query (or a failed resolve) -> None,
        which the base turns into [] (the same contract as the sync path)."""
        lecture_id = _lecture_id(query)
        if not lecture_id:
            return None
        return await self._aresolve(lecture_id)

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of BaseScrapeAdapter.search -> AsyncSearchCapable (the S4a fan-out awaits
        this directly; the JSON:API resolve costs a COROUTINE, not a held pool thread). Shares the base
        async cache round-trip (``_asearch_via``: SAME cache key + 6h ttl, off-loop disk IO, rank off);
        egress via ``_araw_fetch``; mapping via the SAME pure-CPU ``_to_documents`` -> byte-identical to
        ``search`` (a keyword query still returns [], a lecture URL/id still resolves one doc)."""
        return await self._asearch_via(
            query, limit,
            afetch=lambda: self._araw_fetch(query, limit),
            abuild=lambda raw: self._to_documents(raw, query, limit))

    def fetch_url(self, url: str) -> Optional[Document]:
        """Claim an underline.io lecture URL: resolve its metadata + video handle into
        one doc (the penumbra_read / penumbra_search 单源钻取 drill-down path). Returns None for any
        non-Underline url so the fetcher's host routing skips this source."""
        lecture_id = _lecture_id(url)
        if not lecture_id:
            return None
        raw = self._resolve(lecture_id)
        if raw is None:
            return None
        return self._payload_to_document(raw)

    def health_check(self) -> tuple[bool, str]:
        """A keyword probe (the base default) would always return [] here, so probe a
        cheap resolve of one known-stable lecture id (EMNLP 2023 FActScore) instead."""
        raw = self._resolve("88705")
        if raw and _attrs(raw).get("title"):
            return True, "OK (underline JSON:API resolve)"
        return False, "underline API could not resolve the probe lecture"

    # --------------------------------------------------------------- internals
    @staticmethod
    def _resolve(lecture_id: str) -> Optional[dict]:
        """GET the JSON:API lecture payload. None on any failure (network, 404, a
        non-JSON body) -> the base turns it into [] (the adapter contract)."""
        import httpx  # lazy: off the boot import path (this adapter self-registers at import)
        try:
            r = httpx.get(
                _API.format(id=lecture_id),
                timeout=20,
                follow_redirects=True,
                headers={"User-Agent": _UA, "Accept": "application/vnd.api+json"},
            )
            r.raise_for_status()
            data = r.json()
        except Exception as exc:  # noqa: BLE001 (failure -> None -> [], the adapter contract)
            logger.warning("underline resolve failed for %s: %s", lecture_id, exc)
            return None
        return data if isinstance(data, dict) and isinstance(data.get("data"), dict) else None

    @staticmethod
    async def _aresolve(lecture_id: str) -> Optional[dict]:
        """Async twin of ``_resolve``: GET the JSON:API lecture payload via the shared async leaf
        (``http.aget_json``: pooled AsyncClient + per-hop SSRF guard + 30MB cap + the /eye-fix diag tap).
        Same url / UA / Accept header / timeout / redirect-follow (Client-level ``follow_redirects=True``)
        as the sync client; None on any failure (network, non-2xx, non-JSON body, or a payload without a
        ``data`` object) -> the base turns it into [] (the adapter contract)."""
        from penumbra.core import http  # lazy: mirror _resolve's off-boot discipline (this adapter self-registers at import)
        data = await http.aget_json(
            _API.format(id=lecture_id),
            timeout=20,
            headers={"User-Agent": _UA, "Accept": "application/vnd.api+json"},
        )
        return data if isinstance(data, dict) and isinstance(data.get("data"), dict) else None

    @staticmethod
    def _payload_to_document(payload: dict) -> Optional[Document]:
        data = payload.get("data") or {}
        lecture_id = str(data.get("id") or "")
        if not lecture_id:
            return None
        attrs = data.get("attributes") or {}

        slug = attrs.get("slug") or ""
        url = _canonical_url(lecture_id, slug)
        title = attrs.get("title") or "(untitled talk)"
        date = _parse_date(attrs)

        # The video HLS master playlist is a PUBLIC, unsigned CDN url and the durable
        # TRANSCRIBE handle: penumbra_transcribe ffmpeg-decodes an https .m3u8 directly.
        playlist = attrs.get("playlist")
        has_video = isinstance(playlist, str) and playlist.startswith("http")

        event_name = _included_event_name(payload)
        subject_tag = _included_tag_name(payload)

        content = _build_content(
            abstract=attrs.get("abstract") or "",
            event_name=event_name,
            poster=bool(attrs.get("poster_lecture")),
            has_video=has_video,
            underline_doi=attrs.get("underline_doi"),
            url=url,
        )

        tags = ["talk", "conference"]
        if subject_tag:
            tags.append(subject_tag)

        return Document(
            source="underline_talks",
            source_id=lecture_id,
            url=url,
            title=title,
            content=content,
            author=None,  # authors live in sorted_profiles, which leaks emails; we do not ingest it
            date=date,
            # no numeric engagement signal: Underline's public API reports no view/like count
            tags=tags,
            media=[],
            metadata={
                "lecture_id": lecture_id,
                "event": event_name,
                "event_id": attrs.get("event_id"),
                "subject_tag": subject_tag,
                "underline_doi": attrs.get("underline_doi"),
                "package": attrs.get("package"),  # "free" vs a paid tier
                "poster_lecture": bool(attrs.get("poster_lecture")),
                "has_video": has_video,
                # the durable TRANSCRIBE handle (public .m3u8; pass to penumbra_transcribe):
                "transcribe_url": playlist if has_video else None,
                # paper/slides/transcript downloads are login-walled; the paper is on the
                # ACL Anthology (penumbra_anthology). We surface the DOI, not a walled url.
                "paper_walled": bool(attrs.get("paper_url")),
            },
        )


# ─────────────────────────────────────────────────────────────── module helpers

def _canonical_url(lecture_id: str, slug: str) -> str:
    if slug:
        return f"https://underline.io/lecture/{lecture_id}-{slug}"
    return f"https://underline.io/lecture/{lecture_id}"


def _attrs(payload: dict) -> dict:
    return (payload.get("data") or {}).get("attributes") or {}


def _included_event_name(payload: dict) -> Optional[str]:
    """The venue name (e.g. "EMNLP 2023") from the JSON:API ``included`` thin_events."""
    for inc in payload.get("included") or []:
        if isinstance(inc, dict) and inc.get("type") == "thin_events":
            name = (inc.get("attributes") or {}).get("name")
            if name:
                return name
    return None


def _included_tag_name(payload: dict) -> Optional[str]:
    """The subject tag (e.g. "technical paper") from the ``included`` tags."""
    for inc in payload.get("included") or []:
        if isinstance(inc, dict) and inc.get("type") == "tags":
            name = (inc.get("attributes") or {}).get("name")
            if name:
                return name
    return None


def _parse_date(attrs: dict) -> Optional[datetime]:
    """Prefer ``held_at`` (when the talk was given); fall back to ``published_at``.
    Both are ISO 8601 with a trailing Z (e.g. 2023-12-08T08:00:00.000Z)."""
    for key in ("held_at", "published_at"):
        raw = attrs.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _build_content(*, abstract: str, event_name: Optional[str], poster: bool,
                   has_video: bool, underline_doi: Optional[str], url: str) -> str:
    """Assemble the doc body: the paper abstract (real STRUCTURE text), a venue line,
    the DOI, and the TRANSCRIBE pointer (or a note if no resolvable video)."""
    lines: list[str] = []
    abstract = (abstract or "").strip()
    if abstract:
        lines.append(abstract)

    kind = "poster" if poster else "oral"
    venue = f" at {event_name}" if event_name else ""
    lines.append(f"Recorded conference talk ({kind}){venue}.")

    if underline_doi:
        lines.append(f"DOI: {underline_doi}. The paper is on the ACL Anthology.")

    if has_video:
        lines.append(
            "No transcript on Underline without login, but the talk video is public: "
            "pass this lecture's video URL to penumbra_transcribe to transcribe it with local "
            "ASR (the TRANSCRIBE unlock). See metadata.transcribe_url."
        )
    else:
        lines.append("No resolvable public video stream found for this lecture.")

    lines.append(f"Lecture page: {url}")
    return "\n\n".join(lines)


# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
