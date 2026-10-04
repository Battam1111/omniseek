"""The shared yt-dlp entry point for the sources that extract through yt-dlp.

yt-dlp sends through its own HTTP stack, which reaches nothing of OmniSeek. Before 2026-10-04 a health
check that yt-dlp's extraction failed on with HTTP 429 could only report the exception ("down"), and
the guard sweep could not drive these sources at all. They build their extractor from this module
instead (``from omniseek.core import ytdlp`` then ``ytdlp.YoutubeDL(opts)``): the same class, and every
HTTP error answer yt-dlp gets (its ``HTTPError``, raised by ``YoutubeDL.urlopen`` for every request an
extractor makes) is noted for a running health check's ledger (``_probe``: a 429 is "not verified",
another error answer is evidence), with its host and Retry-After. Nothing else changes: same options,
same results, same exceptions. Outside a health check the note is a no-op.

Importing this module imports yt_dlp (a base dependency), so the sources keep importing it lazily,
inside the call, as they did yt_dlp.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from yt_dlp import YoutubeDL as _YoutubeDL
from yt_dlp.networking.exceptions import HTTPError as _HTTPError

from omniseek.core import _probe


def _note(exc: BaseException, req: Any) -> None:
    """Note one HTTP error answer for a running health check (never raises)."""
    try:
        resp = getattr(exc, "response", None)
        url = str(getattr(resp, "url", "") or getattr(req, "url", "") or req or "")
        _probe.note_response(getattr(exc, "status", None), where=urlsplit(url).hostname or url,
                             retry_after=_probe.retry_after_s(getattr(resp, "headers", None)))
    except Exception:  # noqa: BLE001 (recording must never break an extraction)
        pass


class YoutubeDL(_YoutubeDL):
    """``yt_dlp.YoutubeDL`` whose HTTP error answers are noted (see the module docstring)."""

    def urlopen(self, req):
        try:
            return super().urlopen(req)
        except _HTTPError as exc:
            _note(exc, req)
            raise
