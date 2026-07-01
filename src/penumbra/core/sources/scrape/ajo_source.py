"""AcademicJobsOnline (AJO) — the North-American faculty / postdoc application board.

The generic index scraper failed here (verified 2026-06-10): AJO's listing puts
an internal CODE in the anchor text and the REAL position title in a sibling
``<span id="j{ID}">``, with the institution in the nearest preceding
``<h3 class="x1">``. This small bespoke parser reads exactly that structure.
Permanent per the openness principle (academic track stays on the books).

Listing: https://academicjobsonline.org/ajo/jobs (the old ajo?joblist URL is 410).
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from penumbra.core import cache
from penumbra.core.normalize import PolarisDocument, keyword_score_filter

logger = logging.getLogger(__name__)

LIST_URL = "https://academicjobsonline.org/ajo/jobs"
BASE = "https://academicjobsonline.org"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"
TIMEOUT = 25
CACHE_TTL = 10800  # 3h, matches the other job scrapes

_JOB_HREF = re.compile(r"^/ajo/jobs/(\d+)")


class AJOAdapter:
    name = "ajo"
    needs_credentials = False
    kind = "stream"
    domains = ["jobs", "career"]
    description = (
        "AcademicJobsOnline (AJO) — 北美教职/博后申请主板的最新职位列表 "
        "(专用解析器: 真标题在 span#j{ID}, 机构在前置 h3; 通用 scraper 在此只会抽出代码碎片). "
        "开放性准则: 学术 track 永久在册. 补 academic_job_boards(jobs.ac.uk) / "
        "higheredjobs_cs / academic_jobs(Nature Careers)"
    )

    def _positions(self) -> list[dict]:
        key = cache.make_key("ajo", "positions", "v1")
        cached = cache.get(key)
        if cached is not None:
            return cached
        try:
            resp = httpx.get(LIST_URL, headers={"User-Agent": UA}, timeout=TIMEOUT,
                             follow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ajo: listing fetch failed: %s", exc)
            return []
        soup = BeautifulSoup(resp.text, "lxml")
        out: list[dict] = []
        for a in soup.find_all("a", href=_JOB_HREF):
            jid = _JOB_HREF.match(a["href"]).group(1)
            title_el = soup.find("span", id=f"j{jid}")
            title = title_el.get_text(" ", strip=True) if title_el else ""
            if not title or len(title) < 8:
                continue  # no real title rendered for this row
            inst = ""
            h3 = a.find_previous("h3", class_="x1")
            if h3 is not None:
                inst = h3.get_text(" ", strip=True)
            li = a.find_parent("li")
            extra = li.get_text(" ", strip=True)[:400] if li is not None else ""
            out.append({"id": jid, "title": title, "institution": inst, "extra": extra,
                        "url": f"{BASE}/ajo/jobs/{jid}"})
        cache.set(key, out, ttl=CACHE_TTL)
        return out

    def search(self, query: str, limit: int = 10) -> list[PolarisDocument]:
        docs = [self._to_doc(p) for p in self._positions()]
        docs = keyword_score_filter(docs, (query or "").strip())
        return docs[:limit]

    def fetch_url(self, url: str) -> Optional[PolarisDocument]:
        host = (urlparse(url).hostname or "").lower()
        if "academicjobsonline.org" not in host:
            return None
        for p in self._positions():
            if p["url"] == url or url.startswith(p["url"]):
                return self._to_doc(p)
        return None

    def health_check(self) -> tuple[bool, str]:
        n = len(self._positions())
        if n:
            return True, f"OK ({n} positions parsed)"
        return False, "0 positions parsed (layout drift or fetch failure)"

    @staticmethod
    def _to_doc(p: dict) -> PolarisDocument:
        title = f"{p['title']}" + (f" ({p['institution']})" if p["institution"] else "")
        return PolarisDocument(
            source="ajo",
            source_id=p["url"],
            url=p["url"],
            title=title,
            content=(f"Institution: {p['institution'] or '?'}\n{p['extra']}\n"
                     "(列表项; 打开 URL 看完整职位描述与截止日期)"),
            author=p["institution"] or None,
            tags=["ajo", "academic-job"],
            metadata={"institution": p["institution"], "ajo_id": p["id"]},
        )


from penumbra.core.fetcher import register_adapter

register_adapter(AJOAdapter())
