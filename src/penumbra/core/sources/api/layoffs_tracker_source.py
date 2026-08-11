"""Layoffs tracker: layoffs.fyi (Roger Lee's Airtable), the tech-layoffs pulse.

layoffs.fyi is the canonical running ledger of tech-sector layoffs: ~4500 events
(2020-03 to present) with company, HQ location, # laid off, % of headcount, date,
industry, funding stage, total $ raised, country, and a source news link. The site
is a thin shell around a public Airtable shared view (base app1PaujS9zxVGUZ4, the
FULL ledger view shroKsHx3SdYYOzeh; note a sibling view shrFrEtQb57krTYGz on the
same base is a stale 1500-row 2023-2025 slice missing big-tech rows, so we pin the
full view). Data is free WITH ATTRIBUTION to layoffs.fyi.

MODE: STRUCTURE (queryable current ledger: filter by company / industry / country
/ stage) + MONITOR (source_id = the Airtable row id, so every newly logged layoff
surfaces as a new item in a watchtower). No web-search substitute gives this as
one clean structured feed.

ACCESS (verified live 2026-07-10, no credentials): the shared view walls the raw
readSharedViewData JSON behind a two-step handshake. Step 1: GET the embed page
airtable.com/embed/app1PaujS9zxVGUZ4/shroKsHx3SdYYOzeh, which bakes in urlWithParams
(the signed readSharedViewData path carrying accessPolicy + signature), the
applicationId, and a pageLoadId. Step 2: GET airtable.com + that path with the
x-airtable-application-id / x-airtable-page-load-id / x-time-zone headers. Hitting
readSharedViewData without the handshake 302s to /login; the signature self-refreshes
on every embed load, so we redo the handshake each cache miss rather than pinning a
stale token. The full-ledger pull is ~2.8MB / ~4500 rows, cached 6h.

explicit_only: heavy (one ~2.8MB full-ledger pull, cached 6h) niche source; a
named lookup / watchtower, not something to fan into every broad sweep. It stays
directly callable (sources=["layoffs_tracker"]) and sensor-drivable.
"""

from __future__ import annotations

import functools
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import anyio

from penumbra.core import cache, diag, http
from penumbra.core.normalize import Document

logger = logging.getLogger(__name__)

APP_ID = "app1PaujS9zxVGUZ4"
SHARE_ID = "shroKsHx3SdYYOzeh"  # the FULL ledger view (2020 to present, ~4500 rows)
EMBED_URL = f"https://airtable.com/embed/{APP_ID}/{SHARE_ID}"
API_ORIGIN = "https://airtable.com"
TIMEOUT = 30
CACHE_TTL = 21600  # 6h: the ledger grows a few rows a day; a ~2.8MB pull, so cache hard

# Field names are matched by their human column NAME (stable across Airtable field-id
# churn), never by cell index. These are the columns the shared view exposes.
_COL_COMPANY = "Company"
_COL_LOCATION = "Location HQ"
_COL_NUM = "# Laid Off"
_COL_DATE = "Date"
_COL_PCT = "%"
_COL_INDUSTRY = "Industry"
_COL_SOURCE = "Source"
_COL_RAISED = "$ Raised (mm)"
_COL_STAGE = "Stage"
_COL_COUNTRY = "Country"
_COL_DATE_ADDED = "Date Added"


def _js_string_after(text: str, marker: str) -> Optional[str]:
    """Read the JS/JSON string literal that follows ``marker`` in ``text``.

    The embed page stashes ``urlWithParams: "\\u002Fv0.3\\u002F..."`` as a
    backslash-escaped JS string. Scan from the marker to the next UNescaped double
    quote, then resolve the \\uXXXX / \\/ escapes. Returns None if the marker or a
    closing quote is missing.
    """
    i = text.find(marker)
    if i < 0:
        return None
    i += len(marker)
    out: list[str] = []
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            out.append(text[i:i + 2])
            i += 2
            continue
        if c == '"':
            break
        out.append(c)
        i += 1
    else:
        return None
    try:
        return "".join(out).encode("utf-8").decode("unicode_escape")
    except (UnicodeDecodeError, ValueError):
        return None


def _handshake_fetch() -> Optional[dict]:
    """Two-step Airtable shared-view handshake -> the raw readSharedViewData dict.

    None on any failure (the failure -> empty, do-not-cache contract). Never raises.
    """
    html = http.get_text(EMBED_URL, timeout=TIMEOUT)
    if not html:
        logger.info("layoffs_tracker: embed page fetch failed")
        return None
    path = _js_string_after(html, 'urlWithParams: "')
    app_m = re.search(r'"applicationId":"(app[A-Za-z0-9]+)"', html)
    pll_m = re.search(r'"pageLoadId":"([^"]+)"', html)
    if not (path and app_m and pll_m):
        logger.warning("layoffs_tracker: handshake tokens not found in embed page")
        diag.note("layoffs_tracker.handshake", url=EMBED_URL,
                  body="missing urlWithParams / applicationId / pageLoadId")
        return None
    headers = {
        "x-airtable-application-id": app_m.group(1),
        "x-airtable-page-load-id": pll_m.group(1),
        "x-requested-with": "XMLHttpRequest",
        "x-time-zone": "America/Toronto",
        "x-user-locale": "en",
        "Accept": "application/json",
        "Referer": EMBED_URL,
    }
    raw = http.get_json(API_ORIGIN + path, timeout=TIMEOUT, headers=headers)
    if not isinstance(raw, dict) or raw.get("msg") != "SUCCESS":
        logger.warning("layoffs_tracker: readSharedViewData not SUCCESS (%s)",
                       (raw or {}).get("msg") if isinstance(raw, dict) else type(raw))
        return None
    return raw


async def _ahandshake_fetch() -> Optional[dict]:
    """Async twin of _handshake_fetch: SAME 2-step Airtable handshake (same EMBED_URL, same token regex,
    same headers, same SUCCESS guard, same None-on-failure contract); only the two shared-http egresses
    go async: http.get_text -> await http.aget_text (the embed page), http.get_json -> await
    http.aget_json (readSharedViewData). The token regex + build are pure CPU, on the loop."""
    html = await http.aget_text(EMBED_URL, timeout=TIMEOUT)
    if not html:
        logger.info("layoffs_tracker: embed page fetch failed")
        return None
    path = _js_string_after(html, 'urlWithParams: "')
    app_m = re.search(r'"applicationId":"(app[A-Za-z0-9]+)"', html)
    pll_m = re.search(r'"pageLoadId":"([^"]+)"', html)
    if not (path and app_m and pll_m):
        logger.warning("layoffs_tracker: handshake tokens not found in embed page")
        diag.note("layoffs_tracker.handshake", url=EMBED_URL,
                  body="missing urlWithParams / applicationId / pageLoadId")
        return None
    headers = {
        "x-airtable-application-id": app_m.group(1),
        "x-airtable-page-load-id": pll_m.group(1),
        "x-requested-with": "XMLHttpRequest",
        "x-time-zone": "America/Toronto",
        "x-user-locale": "en",
        "Accept": "application/json",
        "Referer": EMBED_URL,
    }
    raw = await http.aget_json(API_ORIGIN + path, timeout=TIMEOUT, headers=headers)
    if not isinstance(raw, dict) or raw.get("msg") != "SUCCESS":
        logger.warning("layoffs_tracker: readSharedViewData not SUCCESS (%s)",
                       (raw or {}).get("msg") if isinstance(raw, dict) else type(raw))
        return None
    return raw


def _parse(raw: dict) -> list[dict]:
    """Raw readSharedViewData dict -> list of flat, label-resolved layoff dicts.

    Pure (no I/O): the smoke golden fixture feeds a real captured payload straight
    in. Select / multiSelect cells arrive as choice ids (sel...); we resolve them to
    their human labels via each column's typeOptions.choices map. Rows missing a
    company are dropped.
    """
    tbl = ((raw or {}).get("data") or {}).get("table") or {}
    cols = tbl.get("columns") or []
    by_name = {c.get("name"): c for c in cols}
    id_by_name = {name: c.get("id") for name, c in by_name.items()}
    choices: dict[str, dict] = {}
    for c in cols:
        if c.get("type") in ("select", "multiSelect"):
            ch = (c.get("typeOptions") or {}).get("choices") or {}
            choices[c.get("name")] = {k: (v or {}).get("name", k) for k, v in ch.items()}

    def cell(cv: dict, name: str):
        cid = id_by_name.get(name)
        return cv.get(cid) if cid else None

    def label(cv: dict, name: str):
        v = cell(cv, name)
        cm = choices.get(name)
        if cm is None or v is None:
            return v
        if isinstance(v, list):
            return [cm.get(x, x) for x in v]
        return cm.get(v, v)

    out: list[dict] = []
    for row in tbl.get("rows") or []:
        cv = row.get("cellValuesByColumnId") or {}
        company = cell(cv, _COL_COMPANY)
        if not company:
            continue
        loc = label(cv, _COL_LOCATION)
        out.append({
            "id": row.get("id"),
            "company": company,
            "locations": loc if isinstance(loc, list) else ([loc] if loc else []),
            "num": cell(cv, _COL_NUM),
            "pct": cell(cv, _COL_PCT),
            "date": cell(cv, _COL_DATE),
            "industry": label(cv, _COL_INDUSTRY),
            "source": cell(cv, _COL_SOURCE),
            "raised_mm": cell(cv, _COL_RAISED),
            "stage": label(cv, _COL_STAGE),
            "country": label(cv, _COL_COUNTRY),
            "date_added": cell(cv, _COL_DATE_ADDED),
        })
    return out


def _parse_date(s) -> Optional[datetime]:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


class LayoffsTrackerAdapter:
    name = "layoffs_tracker"
    needs_credentials = False
    kind = "stream"
    domains = ["jobs", "finance"]
    regions = ["global", "us", "ca"]
    modes = ["STRUCTURE", "MONITOR"]
    explicit_only = (
        "layoffs.fyi Airtable shared view (free, attribution); one ~2.8MB full-ledger "
        "handshake cached 6h; a named lookup / watchtower, not a broad-sweep source"
    )
    description = (
        "科技裁员追踪: layoffs.fyi (Roger Lee 的 Airtable, ~4500 起裁员事件, 2020 至今: 公司/总部/裁员人数/"
        "占比/日期/行业/融资阶段/累计融资/国家/新闻源). STRUCTURE: 空 query=按日期倒序; "
        "关键词过滤公司/行业/国家/阶段 ('google' / 'crypto' / 'india' / 'Series C'). "
        "MONITOR: 新裁员事件=watchtower 新条目. Data by layoffs.fyi (attribution required)"
    )

    def _rows(self) -> list[dict]:
        key = cache.make_key(self.name, "dataset", "v1")
        cached = cache.get(key)
        if cached is not None:
            return cached
        raw = _handshake_fetch()
        if not raw:
            return []
        rows = _parse(raw)
        if rows:
            cache.set(key, rows, ttl=CACHE_TTL)
        return rows

    def search(self, query: str, limit: int = 10) -> list[Document]:
        rows = self._rows()
        if not rows:
            return []
        terms = [t for t in (query or "").lower().split() if t]
        if terms:
            def hay(r: dict) -> str:
                return " ".join(str(x) for x in (
                    r.get("company"), r.get("industry"), r.get("country"),
                    r.get("stage"), " ".join(r.get("locations") or []))).lower()
            rows = [r for r in rows if all(t in hay(r) for t in terms)]
        rows.sort(key=lambda r: r.get("date") or "", reverse=True)
        return [self._to_doc(r) for r in rows[:limit]]

    async def _arows(self) -> list[dict]:
        """Async twin of _rows: off-loop cache get/set (SAME 'dataset','v1' key so async + sync share the
        6h dataset cache), await _ahandshake_fetch (the 2-step handshake), pure-CPU _parse on the loop."""
        key = cache.make_key(self.name, "dataset", "v1")
        cached = await anyio.to_thread.run_sync(cache.get, key)  # disk read OFF loop
        if cached is not None:
            return cached
        raw = await _ahandshake_fetch()
        if not raw:
            return []
        rows = _parse(raw)  # pure CPU, on loop
        if rows:
            await anyio.to_thread.run_sync(functools.partial(cache.set, key, rows, ttl=CACHE_TTL))
        return rows

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of search -> AsyncSearchCapable (the ~2.8MB handshake fetch costs a
        COROUTINE, not a held pool thread). Awaits _arows (handshake + off-loop cache), then the SAME
        pure-CPU term filter + date sort + _to_doc map -> byte-identical to search."""
        rows = await self._arows()
        if not rows:
            return []
        terms = [t for t in (query or "").lower().split() if t]
        if terms:
            def hay(r: dict) -> str:
                return " ".join(str(x) for x in (
                    r.get("company"), r.get("industry"), r.get("country"),
                    r.get("stage"), " ".join(r.get("locations") or []))).lower()
            rows = [r for r in rows if all(t in hay(r) for t in terms)]
        rows.sort(key=lambda r: r.get("date") or "", reverse=True)
        return [self._to_doc(r) for r in rows[:limit]]

    def fetch_url(self, url: str) -> Optional[Document]:
        """Claim by the news-source URL cached in the ledger (no by-id endpoint on the
        shared view). Cheap scan of already-cached rows; None if not ours / not cached."""
        host = (urlparse(url).hostname or "").lower()
        if "layoffs.fyi" not in host and "airtable.com" not in host:
            return None
        for r in self._rows():
            if r.get("source") == url:
                return self._to_doc(r)
        return None

    def health_check(self) -> tuple[bool, str]:
        n = len(self._rows())
        if n:
            return True, f"OK ({n} layoff events)"
        return False, "0 rows (handshake failed / view moved / Airtable down)"

    @staticmethod
    def _to_doc(r: dict) -> Document:
        company = r["company"]
        num = r.get("num")
        pct = r.get("pct")
        country = r.get("country")
        industry = r.get("industry")
        date = _parse_date(r.get("date"))
        pct_str = f"{round(pct * 100)}%" if isinstance(pct, (int, float)) else None

        head_bits = []
        if isinstance(num, (int, float)):
            head_bits.append(f"{int(num)} laid off")
        if pct_str:
            head_bits.append(f"({pct_str})")
        title = f"{company}: " + " ".join(head_bits) if head_bits else f"{company}: layoff"
        tail = ", ".join(b for b in (industry, country) if b)
        if tail:
            title += f" · {tail}"

        lines = [f"Company: {company}"]
        if date:
            lines.append(f"Date: {date.date().isoformat()}")
        if isinstance(num, (int, float)):
            lines.append(f"# Laid off: {int(num)}" + (f" ({pct_str} of headcount)" if pct_str else ""))
        if industry:
            lines.append(f"Industry: {industry}")
        if r.get("stage"):
            lines.append(f"Stage: {r['stage']}")
        if isinstance(r.get("raised_mm"), (int, float)):
            lines.append(f"$ Raised: {r['raised_mm']}mm")
        if country:
            lines.append(f"Country: {country}")
        if r.get("locations"):
            lines.append(f"HQ: {', '.join(r['locations'])}")
        if r.get("source"):
            lines.append(f"Source: {r['source']}")
        lines.append("Data by layoffs.fyi")

        return Document(
            source="layoffs_tracker",
            source_id=r.get("id") or f"{company}-{r.get('date')}",
            url=r.get("source") or "https://layoffs.fyi",
            title=title,
            content="\n".join(lines),
            author=None,
            date=date,
            tags=["layoffs", "jobs"] + ([industry.lower()] if isinstance(industry, str) else []),
            metadata={
                "company": company,
                "num_laid_off": int(num) if isinstance(num, (int, float)) else None,
                "percent": pct,
                "industry": industry,
                "stage": r.get("stage"),
                "raised_mm": r.get("raised_mm"),
                "country": country,
                "locations": r.get("locations"),
                "layoff_date": r.get("date"),
                "date_added": r.get("date_added"),
                "attribution": "layoffs.fyi",
            },
        )


from penumbra.core.fetcher import register_adapter

register_adapter(LayoffsTrackerAdapter())
