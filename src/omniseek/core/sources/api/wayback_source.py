"""Wayback Machine — archived / historical / deleted versions of a URL (keyless, Internet Archive CDX).

OmniSeek's DISCONFIRM + RECALL primitive: when a page changed, vanished, or you need what it said
BEFORE, query its URL → the available snapshots (each a timestamp + an archived web.archive.org URL
you can `omniseek_read` to read the historical content). Web search only ever shows the LIVE page;
this reaches what the open web forgot or deleted. explicit_only named lookup (query = a URL).

Source: the Internet Archive CDX API (keyless):
    GET https://web.archive.org/cdx/search/cdx?url=<URL>&output=json&collapse=digest&limit=-N
        &fl=timestamp,original,statuscode
Response is a JSON array of rows; row[0] is the header. `collapse=digest` drops consecutive
identical-content captures; `limit=-N` returns the N NEWEST. A snapshot is read at
    https://web.archive.org/web/<timestamp>/<original>
Fallback when CDX does not answer (2026-10-04): the availability API (keyless)
    GET https://archive.org/wayback/available?url=<URL>
returns only the CLOSEST snapshot, ``{"archived_snapshots": {"closest": {"url", "timestamp",
"status", "available"}}}``, or ``"archived_snapshots": {}`` when there is none. Measured 2026-10-04
from both machines: CDX answered in 1.5 s to over 40 s (Internet Archive load), the availability API
in about 0.6 s. A read then returns that one snapshot, marked ``partial``.
Recon trail: brain note eye-recon-wayback.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

from omniseek.core import diag, http
from omniseek.core.normalize import Document

logger = logging.getLogger(__name__)

CDX_URL = "https://web.archive.org/cdx/search/cdx"
AVAILABILITY_URL = "https://archive.org/wayback/available"
# CDX is legitimately slow (collapse=digest scans the full capture history; ~15-20s on a big URL)
# and intermittently 503s under Internet Archive load. A generous timeout + the 1h cache (a hit
# caches, repeats are instant) make it usable; a miss falls back to the availability API.
TIMEOUT = 30
# The availability API answered in about 0.6 s (2026-10-04); 15 s bounds it well clear of that while
# keeping a full miss (30 + 15) inside the declared health cap.
AVAILABILITY_TIMEOUT = 15
_UA = "Mozilla/5.0 (compatible; OmniSeek/1.0; +archive lookup)"
# Accept a URL-ish query: starts with http(s), or a bare domain (no spaces, has a dotted host).
_URLISH = re.compile(r"^(https?://|[\w-]+(\.[\w-]+)+(/|$))", re.I)


def _looks_like_url(q: str) -> bool:
    q = (q or "").strip()
    return bool(q) and " " not in q and bool(_URLISH.match(q))


def _snap_to_doc(row: list, idx: dict) -> Optional[Document]:
    """One CDX row → a snapshot Document (pure fn → golden-fixture testable)."""
    ts = row[idx["timestamp"]] if "timestamp" in idx else ""
    orig = row[idx["original"]] if "original" in idx else ""
    if not ts or not orig:
        return None
    status = row[idx["statuscode"]] if "statuscode" in idx and idx["statuscode"] < len(row) else ""
    snap_url = f"https://web.archive.org/web/{ts}/{orig}"
    try:
        dt = datetime.strptime(ts, "%Y%m%d%H%M%S")
        pretty = dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        dt, pretty = None, ts
    return Document(
        source="wayback",
        source_id=f"{ts}:{orig}",
        url=snap_url,
        title=f"{orig} @ {pretty}" + (f" [HTTP {status}]" if status else ""),
        content=(f"Wayback snapshot of {orig}\nCaptured: {pretty}"
                 + (f"  ·  HTTP {status}" if status else "")
                 + f"\nRead the archived page: omniseek_read {snap_url}"),
        date=dt,
        metadata={"timestamp": ts, "original": orig, "status": status, "snapshot_url": snap_url,
                  "provider": "internet_archive_cdx"},
    )


_SNAP_URL = re.compile(r"/web/(\d{4,14})/(.+)$")


def _cdx_params(q: str, limit: int) -> dict:
    return {"url": q, "output": "json", "collapse": "digest",
            "limit": str(-max(limit, 1)), "fl": "timestamp,original,statuscode"}


def _cdx_docs(payload: list, limit: int) -> list[Document]:
    """A CDX answer (header row + rows) → snapshot docs, newest first (pure)."""
    if len(payload) < 2:
        return []
    idx = {name: i for i, name in enumerate(payload[0])}
    docs: list[Document] = []
    for row in payload[1:]:
        try:
            doc = _snap_to_doc(row, idx)
        except Exception:  # noqa: BLE001 (one bad row can't sink the rest)
            continue
        if doc is not None:
            docs.append(doc)
    docs.sort(key=lambda d: d.metadata.get("timestamp", ""), reverse=True)  # newest first
    return docs[:limit]


def _availability_doc(payload: object, q: str) -> Optional[Document]:
    """The availability API's closest snapshot → ONE doc marked partial, or None when it has none (pure).
    The original URL is the part of the closest url after ``/web/<timestamp>/``, else the query."""
    snaps = payload.get("archived_snapshots") if isinstance(payload, dict) else None
    closest = snaps.get("closest") if isinstance(snaps, dict) else None
    if not isinstance(closest, dict) or closest.get("available") is False:
        return None
    url = str(closest.get("url") or "")
    m = _SNAP_URL.search(url)
    ts = str(closest.get("timestamp") or (m.group(1) if m else ""))
    orig = m.group(2) if m else q
    doc = _snap_to_doc([ts, orig, str(closest.get("status") or "")],
                       {"timestamp": 0, "original": 1, "statuscode": 2})
    if doc is None:
        return None
    note = (f"Only the closest snapshot: the full snapshot list (Internet Archive CDX) did not answer "
            f"within {TIMEOUT} s, so this one comes from the availability API. Retry later for the full list.")
    doc.title += " (closest snapshot only; the full snapshot list did not answer)"
    doc.content = note + "\n" + doc.content
    doc.metadata.update({"provider": "internet_archive_availability", "partial": True})
    return doc


def _note_cdx_miss(q: str) -> None:
    diag.note("wayback.cdx", url=CDX_URL,
              body=(f"CDX gave no snapshot list for {q} within {TIMEOUT} s (no answer, an error, or not "
                    "JSON); asked the availability API for the closest snapshot instead"))


class WaybackAdapter:
    name = "wayback"
    needs_credentials = False
    kind = "lookup"
    domains = ["news"]
    modes = ["RECALL", "UNWALL"]
    explicit_only = "archived/historical/deleted versions of a URL (named lookup; query = a URL)"
    cache_ttl = 3600
    # Health probes CDX (TIMEOUT, 30 s) and, on a miss, the availability API (15 s): 45 s plus margin,
    # over the 25 s default cap (fetcher._HEALTH_TIMEOUT_S) that would cut a slow CDX answer off.
    health_timeout_s = 50
    description = (
        "Wayback Machine 时光机：一个 URL 的历史/被删快照 (keyless, Internet Archive CDX). query = "
        "一个 URL → 该页的存档快照列表(时间戳 + web.archive.org 存档链接,再 omniseek_read 读历史正文). "
        "web 搜只给 LIVE 页;这取开放网已遗忘/已删改的旧版本(对抗检索、读历史、读被删)。命名查询;非 URL 返空. "
        "CDX 30 秒内不应答时退到 availability API,只返回最近的一个快照(metadata.partial=true)."
    )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        q = (query or "").strip()
        if not _looks_like_url(q):
            return []  # not a URL, do not guess
        # ONE CDX attempt. The old 3-attempt loop retried only on an exception, but get_json returns
        # None and never raises, so it never ran. No shared connect retry either: a CDX miss falls
        # through to the availability API, which is the cheaper second try.
        payload = http.get_json(CDX_URL, params=_cdx_params(q, limit), headers={"User-Agent": _UA},
                                timeout=TIMEOUT, retry_transient=False)
        if isinstance(payload, list):
            return _cdx_docs(payload, limit)
        _note_cdx_miss(q)
        avail = http.get_json(AVAILABILITY_URL, params={"url": q}, headers={"User-Agent": _UA},
                              timeout=AVAILABILITY_TIMEOUT)
        doc = _availability_doc(avail, q)
        return [doc] if doc is not None else []

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` (S4b): the fan-out awaits this DIRECTLY, so a wayback
        lookup's dominant NETWORK wait (CDX is legitimately slow) costs a COROUTINE, not a held pool
        thread. Mirrors ``search`` line for line; the only swaps are the two egress calls to their
        async twins (``await http.aget_json``). The URL guard and the pure payload-to-doc helpers stay
        on the loop, so async and sync return the same docs. No disk cache round-trip in either."""
        q = (query or "").strip()
        if not _looks_like_url(q):
            return []  # not a URL, do not guess
        payload = await http.aget_json(CDX_URL, params=_cdx_params(q, limit), headers={"User-Agent": _UA},
                                       timeout=TIMEOUT, retry_transient=False)
        if isinstance(payload, list):
            return _cdx_docs(payload, limit)
        _note_cdx_miss(q)
        avail = await http.aget_json(AVAILABILITY_URL, params={"url": q}, headers={"User-Agent": _UA},
                                     timeout=AVAILABILITY_TIMEOUT)
        doc = _availability_doc(avail, q)
        return [doc] if doc is not None else []

    def fetch_url(self, url: str) -> Optional[Document]:
        return None

    def health_check(self) -> tuple[bool, str]:
        # The CDX probe waits as long as the data path does (30 s), so health certifies that path and
        # nothing stricter. A CDX miss is still a usable source when the availability API answers.
        payload = http.get_json(
            CDX_URL, params={"url": "example.com", "output": "json", "limit": "1"},
            headers={"User-Agent": _UA}, timeout=TIMEOUT,
        )
        if isinstance(payload, list):
            return True, "OK (Internet Archive CDX)"
        avail = http.get_json(AVAILABILITY_URL, params={"url": "example.com"},
                              headers={"User-Agent": _UA}, timeout=AVAILABILITY_TIMEOUT)
        if isinstance(avail, dict) and "archived_snapshots" in avail:
            return True, (f"degraded: CDX did not answer within {TIMEOUT} s (Internet Archive load); the "
                          "availability API answered, so reads return the closest snapshot only")
        return False, f"neither CDX ({TIMEOUT} s) nor the availability API answered"

from omniseek.core.fetcher import register_adapter

register_adapter(WaybackAdapter())
