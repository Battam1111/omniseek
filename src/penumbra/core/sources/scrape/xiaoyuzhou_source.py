"""小宇宙 (xiaoyuzhou) podcast adapter — native, no RSSHub.

The ``feed.xyzfm.space`` shortcut host (the RSS several 小宇宙 shows relied on)
went dead, killing those shows' standard feeds. Rather than stand up a RSSHub
server on the bare Mac mini (no Docker/Node), this fetches each podcast's recent
episodes straight from xiaoyuzhoufm.com — the Next.js page embeds the full
episode list (title, shownotes, audio enclosure, cover, pubDate) in its
``__NEXT_DATA__`` JSON, so no API auth is needed.

Configure via ``~/.penumbra/credentials/xiaoyuzhou.json``:
    {"podcasts": [{"id": "<24-hex>", "name": "..."}]}   (id = the /podcast/<id> id)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

from penumbra.core import auth, cache
from penumbra.core.normalize import Document, jsonsafe, keyword_score_filter

logger = logging.getLogger(__name__)

BASE = "https://www.xiaoyuzhoufm.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"
CACHE_TTL = 6 * 3600
_NEXT = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# Recovered shows whose xyzfm RSS died (high-signal CN AI/tech/创投 deep-dives).
DEFAULT_PODCASTS = [
    {"id": "626b46ea9cbbf0451cf5a962", "name": "张小珺商业访谈录"},
    {"id": "61cbaac48bb4cd867fcabe22", "name": "OnBoard!"},
    {"id": "648b0b641c48983391a63f98", "name": "42章经"},
    {"id": "6507bc165c88d2412626b401", "name": "屠龙之术"},
    {"id": "61358d971c5d56efe5bcb5d2", "name": "乱翻书"},
    {"id": "670f3da40d2f24f28978736f", "name": "跨国串门儿计划"},
]

auth.write_template("xiaoyuzhou", {
    "_comment": "小宇宙播客跟踪. id = xiaoyuzhoufm.com/podcast/<id> 的 24-hex id.",
    "podcasts": DEFAULT_PODCASTS,
})


def _img(v) -> Optional[str]:
    if isinstance(v, dict):
        return v.get("picUrl") or v.get("largePicUrl") or v.get("middlePicUrl")
    return v if isinstance(v, str) and v.startswith("http") else None


def _date(s) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _next_data(html: str) -> dict:
    m = _NEXT.search(html)
    if not m:
        return {}
    try:
        return json.loads(m.group(1)).get("props", {}).get("pageProps", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


class XiaoyuzhouAdapter:
    name = "xiaoyuzhou"
    needs_credentials = False
    description = (
        "小宇宙播客 — 张小珺商业访谈录 / OnBoard! / 42章经 等中文 AI/科技/创投深度访谈 "
        "(原生抓 xiaoyuzhoufm __NEXT_DATA__, 无需 RSSHub; 可配 ~/.penumbra/credentials/xiaoyuzhou.json)"
    )

    def _podcasts(self) -> list[dict]:
        creds = auth.load("xiaoyuzhou")
        if creds and isinstance(creds.get("podcasts"), list) and creds["podcasts"]:
            return creds["podcasts"]
        return DEFAULT_PODCASTS

    def _fetch_podcast(self, pid: str, name: str) -> list[Document]:
        key = cache.make_key("xiaoyuzhou", "podcast", pid)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached
        try:
            r = httpx.get(f"{BASE}/podcast/{pid}", headers={"User-Agent": UA}, timeout=20, follow_redirects=True)
            pod = _next_data(r.text).get("podcast", {}) or {}
            title = pod.get("title") or name
            eps = pod.get("episodes") or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("xiaoyuzhou fetch failed for %s (%s): %s", name, pid, exc)
            return []
        docs = [self._ep_to_doc(e, title) for e in eps if isinstance(e, dict) and e.get("eid")]
        if docs:
            cache.set_docs(key, docs, ttl=CACHE_TTL)
        return docs

    def _ep_to_doc(self, e: dict, podcast_title: str) -> Document:
        eid = e.get("eid")
        body = e.get("shownotes") or e.get("description") or ""
        content = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip() or "(no shownotes)"
        content += "\n\n[完整口语内容在音频里(shownotes 仅摘要)→ penumbra_transcribe 取本地 Whisper 全文转写]"
        img = _img(e.get("image"))
        enc = e.get("enclosure")
        return Document(
            source="xiaoyuzhou",
            source_id=str(eid),
            url=f"{BASE}/episode/{eid}",
            title=(e.get("title") or "(untitled)").strip(),
            content=content,
            author=podcast_title,
            date=_date(e.get("pubDate")),
            tags=["播客", podcast_title],
            media=[img] if img else [],
            metadata={
                "podcast": podcast_title,
                "duration_s": e.get("duration"),
                "play_count": e.get("playCount"),
                "audio": enc.get("url") if isinstance(enc, dict) else None,
                "raw": jsonsafe(e),
            },
        )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        docs: list[Document] = []
        for p in self._podcasts():
            pid = p.get("id")
            if pid:
                docs.extend(self._fetch_podcast(pid, p.get("name", pid)))
        q = (query or "").strip()
        if q:
            return keyword_score_filter(docs, q)[:limit]
        docs.sort(key=lambda d: d.date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return docs[:limit]

    def fetch_url(self, url: str) -> Optional[Document]:
        host = (urlparse(url).hostname or "").lower()
        if "xiaoyuzhoufm.com" not in host:
            return None
        parts = urlparse(url).path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "episode":
            try:
                r = httpx.get(url, headers={"User-Agent": UA}, timeout=20, follow_redirects=True)
                ep = _next_data(r.text).get("episode", {}) or {}
                if ep.get("eid"):
                    pod = ep.get("podcast") or {}
                    return self._ep_to_doc(ep, pod.get("title") if isinstance(pod, dict) else "")
            except Exception as exc:  # noqa: BLE001
                logger.warning("xiaoyuzhou fetch_url failed for %s: %s", url, exc)
        return None

    def health_check(self) -> tuple[bool, str]:
        pods = self._podcasts()
        if not pods:
            return False, "no podcasts configured"
        first = pods[0]
        try:
            docs = self._fetch_podcast(first.get("id"), first.get("name", "?"))
            if docs:
                return True, f"OK ({len(pods)} podcasts; first returned {len(docs)} episodes)"
            return False, f"0 episodes for {first.get('name')} — page structure changed?"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"


from penumbra.core.fetcher import register_adapter

register_adapter(XiaoyuzhouAdapter())
