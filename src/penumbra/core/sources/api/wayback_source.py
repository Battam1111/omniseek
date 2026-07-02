"""Wayback Machine — archived / historical / deleted versions of a URL (keyless, Internet Archive CDX).

The eye's DISCONFIRM + RECALL primitive: when a page changed, vanished, or you need what it said
BEFORE, query its URL → the available snapshots (each a timestamp + an archived web.archive.org URL
you can `penumbra_read` to read the historical content). Web search only ever shows the LIVE page;
this reaches what the open web forgot or deleted. explicit_only named lookup (query = a URL).

Source: the Internet Archive CDX API (keyless):
    GET https://web.archive.org/cdx/search/cdx?url=<URL>&output=json&collapse=digest&limit=-N
        &fl=timestamp,original,statuscode
Response is a JSON array of rows; row[0] is the header. `collapse=digest` drops consecutive
identical-content captures; `limit=-N` returns the N NEWEST. A snapshot is read at
    https://web.archive.org/web/<timestamp>/<original>
Recon trail: brain note eye-recon-wayback.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Optional

from penumbra.core import http
from penumbra.core.normalize import Document

logger = logging.getLogger(__name__)

CDX_URL = "https://web.archive.org/cdx/search/cdx"
# CDX is legitimately slow (collapse=digest scans the full capture history; ~15-20s on a big URL)
# and intermittently 503s under Internet Archive load. A generous timeout + the 1h cache (a hit
# caches, repeats are instant) make it usable; a 503/slow miss degrades to [] with a diagnostic.
TIMEOUT = 30
_UA = "Mozilla/5.0 (compatible; PenumbraEye/1.0; +archive lookup)"
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
                 + f"\nRead the archived page: penumbra_read {snap_url}"),
        date=dt,
        metadata={"timestamp": ts, "original": orig, "status": status, "snapshot_url": snap_url,
                  "provider": "internet_archive_cdx"},
    )


class WaybackAdapter:
    name = "wayback"
    needs_credentials = False
    kind = "lookup"
    domains = ["news"]
    modes = ["RECALL", "UNWALL"]
    explicit_only = "archived/historical/deleted versions of a URL (named lookup — query = a URL)"
    cache_ttl = 3600
    description = (
        "Wayback Machine 时光机 — 一个 URL 的历史/被删快照 (keyless, Internet Archive CDX). query = "
        "一个 URL → 该页的存档快照列表(时间戳 + web.archive.org 存档链接,再 penumbra_read 读历史正文). "
        "web 搜只给 LIVE 页;这取开放网已遗忘/已删改的旧版本(对抗检索、读历史、读被删)。命名查询;非 URL 返空."
    )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        q = (query or "").strip()
        if not _looks_like_url(q):
            return []  # not a URL — do not guess
        # CDX 503s under Internet Archive load; retry a couple times before degrading to [].
        payload = None
        for attempt in range(3):
            try:
                payload = http.get_json(
                    CDX_URL,
                    params={"url": q, "output": "json", "collapse": "digest",
                            "limit": str(-max(limit, 1)), "fl": "timestamp,original,statuscode"},
                    headers={"User-Agent": _UA},
                    timeout=TIMEOUT,
                )
                break
            except Exception as exc:  # noqa: BLE001 — IA load → retry, then degrade to []
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                logger.warning("wayback CDX failed after retries: %s", exc)
                return []
        if not isinstance(payload, list) or len(payload) < 2:
            return []
        idx = {name: i for i, name in enumerate(payload[0])}
        docs: list[Document] = []
        for row in payload[1:]:
            try:
                doc = _snap_to_doc(row, idx)
            except Exception:  # noqa: BLE001 — one bad row can't sink the rest
                continue
            if doc is not None:
                docs.append(doc)
        docs.sort(key=lambda d: d.metadata.get("timestamp", ""), reverse=True)  # newest first
        return docs[:limit]

    def fetch_url(self, url: str) -> Optional[Document]:
        return None

    def health_check(self) -> tuple[bool, str]:
        payload = http.get_json(
            CDX_URL, params={"url": "example.com", "output": "json", "limit": "1"},
            headers={"User-Agent": _UA}, timeout=15,
        )
        if isinstance(payload, list):
            return True, "OK (Internet Archive CDX)"
        return False, "CDX slow/503 (Internet Archive load) — transient, source still usable"


from penumbra.core.fetcher import register_adapter

register_adapter(WaybackAdapter())
