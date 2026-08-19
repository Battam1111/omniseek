"""IRCC Express Entry rounds — Canada's official draw history, STRUCTURED.

Canada's Express Entry draws are an immigration decision signal: each draw's
category / CRS cut-off / invitation count shows which categories are being drawn
and where the cut-off is trending. IRCC publishes the full history as JSON (419
rounds and counting, with per-draw CRS pool distribution buckets), which web
search cannot return as queryable data and the news feed only narrates. Mode:
STRUCTURE + MONITOR (a watchtower row alerts on every new draw; source_id = draw
number, so each new round is exactly one new item).

Access (verified 2026-06-10): canada.ca walls plain HTTP from this host at the
transport level (curl times out, WebFetch 403), but the CDP real browser reads
the JSON fine. So this fetches via the shared CDP Chrome (lazy import, same
pattern as the JS-rendered scrape sites) and is explicit_only: named calls +
the watchtower, never the broad fan-out.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from omniseek.core import cache
from omniseek.core.normalize import Document, keyword_score_filter

logger = logging.getLogger(__name__)

ROUNDS_JSON = "https://www.canada.ca/content/dam/ircc/documents/json/ee_rounds_123_en.json"
ROUNDS_PAGE = ("https://www.canada.ca/en/immigration-refugees-citizenship/services/"
               "immigrate-canada/express-entry/submit-profile/rounds-invitations.html")
CACHE_TTL = 14400  # 4h: draws land roughly biweekly; the watchtower polls 6-hourly
MAX_ROUNDS = 30    # most-recent N exposed as documents (full history in the JSON)

_HREF_RE = re.compile(r"href='([^']+)'")


def _fetch_rounds_json() -> list[dict]:
    """The rounds list via the CDP real browser (lazy import: this module must
    not pull playwright in when the source is never touched)."""
    from omniseek.core.sources.walled._cdp import cdp_call

    def _nav(page):
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        return page.inner_text("body")

    body = cdp_call(_nav, initial_url=ROUNDS_JSON, timeout=60)
    return json.loads(body).get("rounds", [])


class IRCCEERoundsAdapter:
    name = "ircc_ee_rounds"
    needs_credentials = False
    kind = "stream"
    domains = ["immigration"]
    regions = ["ca"]
    explicit_only = "CDP fetch of canada.ca (transport-walled to plain HTTP); named + watchtower only"
    description = (
        "IRCC Express Entry 抽签轮 (加拿大官方 JSON, 全史 400+ 轮, 经 CDP 真浏览器取) — "
        "每轮的类别 / CRS 分数线 / 邀请数 / 池内 CRS 分布, 结构化可查 + watchtower 盯新轮. "
        "加拿大 Express Entry 抽签的核心决策信号. 空 query=最近各轮; "
        "关键词过滤类别 (CEC / French / STEM / PNP...)"
    )

    def _rounds(self) -> list[dict]:
        key = cache.make_key("ircc_ee_rounds", "rounds", "v1")
        cached = cache.get(key)
        if cached is not None:
            return cached
        try:
            rounds = _fetch_rounds_json()[:MAX_ROUNDS]
        except Exception as exc:  # noqa: BLE001
            logger.warning("ircc_ee_rounds: fetch failed: %s", exc)
            return []
        cache.set(key, rounds, ttl=CACHE_TTL)
        return rounds

    def search(self, query: str, limit: int = 10) -> list[Document]:
        docs = [d for d in (self._to_doc(r) for r in self._rounds()) if d]
        docs = keyword_score_filter(docs, (query or "").strip())
        return docs[:limit]

    def fetch_url(self, url: str) -> Optional[Document]:
        return None  # structured lookup source; reach it via search

    def health_check(self) -> tuple[bool, str]:
        # LIGHT: never a full CDP fetch in a health probe (the P19 lesson). Browser
        # liveness + whatever the cache holds is an honest signal.
        try:
            from omniseek.core.sources.walled._cdp import cdp_health
            alive, msg = cdp_health()
        except Exception as exc:  # noqa: BLE001
            return False, f"CDP unavailable: {exc}"
        if not alive:
            return False, f"CDP down: {msg}"
        cached = cache.get(cache.make_key("ircc_ee_rounds", "rounds", "v1"))
        n = len(cached) if cached else 0
        if n:
            return True, f"OK (CDP up; {n} rounds cached)"
        return True, "OK (CDP up; no cache yet)"

    @staticmethod
    def _to_doc(r: dict) -> Optional[Document]:
        num = str(r.get("drawNumber") or "").strip()
        if not num:
            return None
        name = (r.get("drawName") or "").strip()
        crs = str(r.get("drawCRS") or "").strip()
        size = str(r.get("drawSize") or "").strip()
        date = None
        if r.get("drawDate"):
            try:
                date = datetime.fromisoformat(r["drawDate"]).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass
        m = _HREF_RE.search(r.get("drawNumberURL") or "")
        url = ("https://www.canada.ca" + m.group(1)) if m else ROUNDS_PAGE

        parts = [
            f"Category: {name}",
            f"CRS cut-off: {crs}  ·  Invitations: {size}",
            f"Draw date: {r.get('drawDateFull') or r.get('drawDate') or '?'}"
            + (f"  ·  Tie-break: {r['drawCutOff']}" if r.get("drawCutOff") else ""),
        ]
        if r.get("drawText2"):
            parts.append(f"Programs: {r['drawText2']}")
        if r.get("dd18"):
            parts.append(f"Pool total (as of {r.get('drawDistributionAsOn', '?')}): {r['dd18']}")

        return Document(
            source="ircc_ee_rounds",
            source_id=num,  # stable per draw: the watchtower's "new round" key
            url=url,
            title=f"EE Draw #{num}: {name} (CRS {crs}, {size} invitations)",
            content="\n".join(parts),
            date=date,
            tags=["express-entry", "immigration", "ca"],
            metadata={
                "draw_number": num,
                "category": name,
                "crs_cutoff": crs,
                "invitations": size,
                "draw_date": r.get("drawDate"),
                "pool_distribution": {k: r.get(k) for k in
                                      ("dd1", "dd2", "dd3", "dd4", "dd18") if r.get(k)},
                "raw": r,
            },
        )


from omniseek.core.fetcher import register_adapter

register_adapter(IRCCEERoundsAdapter())
