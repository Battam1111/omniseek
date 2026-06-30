"""page_watch — change-sentinel for slow-moving POLICY/RULES pages.

Some decision-critical pages (MOM EP/COMPASS eligibility, ONE Pass criteria,
ICA PR application rules) have no feed and change rarely but MATTER when they
do (salary thresholds, criteria). Penumbra's watchtower diffs source_ids, so the
whole "page diff" capability collapses into one dumb trick: each watched page
becomes ONE document whose source_id embeds the normalized-content FINGERPRINT.
Page changes → fingerprint changes → a NEW source_id appears → the existing
watchtower alerts. No new machinery, no judgment.

Caveat (documented, observed-first posture): pages can embed legitimately
volatile fragments, so the watch row starts PASSIVE; promote to active once the
flap rate is seen to be low. Rows live in ``page_watch.json``
{name, label, url, regions?, note?}: adding a watched page is a one-row edit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from penumbra.core import cache
from penumbra.core.normalize import Document, keyword_score_filter

logger = logging.getLogger(__name__)

_DATA = Path(__file__).with_name("page_watch.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"
TIMEOUT = 25
CACHE_TTL = 21600  # 6h, the watchtower cadence


def _strip_to_text(html: str) -> str:
    """SPA-friendly HTML → visible text (borrowed: changedetection.io html_tools.html_to_text).
    Strips the head/script/style/svg/math/canvas/iframe/template bloat an SPA dumps into the page
    (which would otherwise drown the real text) + un-hides a ``display:none`` body, then BS4
    get_text + whitespace-collapse."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["head", "script", "style", "noscript", "svg", "math",
                     "canvas", "iframe", "template", "nav", "header", "footer"]):
        tag.decompose()
    body = soup.find("body")
    if body and body.get("style") and re.search(
            r"display\s*:\s*none|visibility\s*:\s*hidden", body["style"], re.IGNORECASE):
        del body["style"]  # SPAs hide the body until JS loads; un-hide so get_text sees it
    main = soup.find("main") or soup.body or soup
    return re.sub(r"\s+", " ", main.get_text(" ", strip=True)).strip()


def _render_html(url: str) -> str:
    """JS-render a SPA through the shared CDP Chrome (for pages httpx can't read: docs sites /
    changelogs that client-render to an empty shell — e.g. Valyu/Exa changelogs). Returns rendered
    HTML, '' on failure. cdp_call is serialized + gated, so this queues behind other walled work."""
    from penumbra.core.sources.walled._cdp import cdp_call

    def _flow(page):
        try:
            page.wait_for_timeout(2500)  # let the SPA hydrate after domcontentloaded
        except Exception:  # noqa: BLE001
            pass
        return page.content()
    try:
        return cdp_call(_flow, initial_url=url, timeout=45) or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("page_watch: render failed %s: %s", url, exc)
        return ""


def _page_text(url: str, render: bool = False) -> str:
    """Fetch + normalize a page's visible text (whitespace-collapsed). ``render=True`` drives the
    page through the CDP Chrome (JS-rendered) instead of httpx — the only way to fingerprint a
    SPA changelog that client-renders to an empty shell. Empty string on failure."""
    if render:
        html = _render_html(url)
        return _strip_to_text(html) if html else ""
    try:
        resp = httpx.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT,
                         follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("page_watch: fetch failed %s: %s", url, exc)
        return ""
    return _strip_to_text(resp.text)


class PageWatchAdapter:
    name = "page_watch"
    needs_credentials = False
    kind = "stream"
    domains = ["immigration", "policy"]
    explicit_only = "page-change sentinel; watchtower + named calls only"
    description = (
        "规则页变更哨兵 — 盯无 feed 但变了就重要的政策/规则页 (MOM EP/COMPASS 资格、"
        "ONE Pass 标准、ICA PR 申请). 每页一个文档, source_id 内嵌内容指纹: 页面一变, "
        "指纹即变, watchtower 视为新条目自动报. 加一页 = page_watch.json 加一行. "
        "(指纹可能因页面动态碎片偶发翻动, 故盯哨先 passive 观察)"
    )

    def _rows(self) -> list[dict]:
        """Base in-tree rows + the curator live-apply overlay rows (base wins on a name clash;
        each overlay row is typed-validated and a bad one is dropped + logged). page_watch is ONE
        adapter over N rows, so an overlay row is an extra ROW (NOT a new adapter): re-read here,
        every call, means a live overlay append takes effect on the next search with no restart."""
        base = json.loads(_DATA.read_text(encoding="utf-8"))
        rows = list(base)
        seen = {r.get("name") for r in base if isinstance(r, dict)}
        try:
            from penumbra.core.curator import apply as _apply
            from penumbra.core.curator import apply_live as _apply_live
            for r in _apply_live.overlay_rows("page_watch"):
                name = r.get("name")
                if name in seen:
                    continue  # base wins
                problems = _apply.validate_row_typed("page_watch", r)
                if problems:
                    logger.warning("page_watch overlay row %r dropped (invalid): %s", name, problems)
                    continue
                rows.append(r)
                seen.add(name)
        except Exception as exc:  # noqa: BLE001, overlay best-effort; base rows always returned
            logger.warning("page_watch overlay rows skipped: %s", exc)
        return rows

    def _doc_for(self, row: dict) -> Optional[Document]:
        key = cache.make_key("page_watch", "text", row["url"])
        text = cache.get(key)
        if text is None:
            text = _page_text(row["url"], render=bool(row.get("render")))
            if text:
                cache.set(key, text, ttl=CACHE_TTL)
        if not text or len(text) < 200:
            return None  # JS shell or fetch failure: no honest fingerprint possible
        fp = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
        return Document(
            source="page_watch",
            source_id=f"{row['name']}:{fp}",
            url=row["url"],
            title=f"{row['label']} · 内容指纹 {fp}",
            content=(text[:1200] + ("…" if len(text) > 1200 else "")
                     + "\n\n(此文档代表该页当前内容版本; 指纹变化 = 页面已被改动, 打开 URL 看现行规则)"),
            tags=["page-watch"] + (row.get("regions") or []),
            metadata={"page": row["name"], "fingerprint": fp, "chars": len(text),
                      "note": row.get("note", "")},
        )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        docs = [d for d in (self._doc_for(r) for r in self._rows()) if d]
        docs = keyword_score_filter(docs, (query or "").strip())
        return docs[:limit]

    def fetch_url(self, url: str) -> Optional[Document]:
        norm = url.split("?")[0].rstrip("/")
        for row in self._rows():
            if row["url"].split("?")[0].rstrip("/") == norm:
                return self._doc_for(row)
        return None

    def health_check(self) -> tuple[bool, str]:
        rows = self._rows()
        if not rows:
            return False, "no pages configured"
        d = self._doc_for(rows[0])
        if d is None:
            return False, f"first page yielded no text ({rows[0]['url']})"
        return True, f"OK ({len(rows)} pages; first fp={d.metadata['fingerprint']})"


from penumbra.core.fetcher import register_adapter

register_adapter(PageWatchAdapter())
