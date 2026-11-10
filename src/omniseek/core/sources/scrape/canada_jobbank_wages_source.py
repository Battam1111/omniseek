"""Job Bank Canada: prevailing hourly wages by occupation (EI/Labour Force Survey).

The Government of Canada's Job Bank publishes the official prevailing wage (low /
median / high hourly) for every occupation, at NATIONAL, PROVINCIAL, and economic
REGION granularity. This is the number employers and immigration streams (LMIA,
Express Entry) anchor to, so a "what does a <role> earn in Canada" query wants THIS,
not a crowd-sourced US benchmark (that is levels.fyi's job). Region ca, compensation.

Two-step, keyless:
  (1) RESOLVE a free-text job title -> the Job Bank occupation id via the site's own
      Solr typeahead: /core/ta-jobtitle_en/select?q=<query>&wt=json&rows=25 . Each doc
      carries noc_code (NOC 2011), noc21_code (NOC 2021), and
      noc_job_title_concordance_id -- the id the wage page URL is keyed on (verified
      against the page's og:url template
      "/wagereport/occupation/#{...jobTitleConcordanceIdParam}").
  (2) FETCH /wagereport/occupation/<concordance_id> and parse its single wage table
      (rows classed wage-national / wage-province / wage-region, cells headers=
      header_min|header_avg|header_max) into ONE Document: national low/median/
      high + a per-province breakdown in content, every region row in metadata.

One doc per query (the resolved occupation), so no local ranking. Wage data refreshes
roughly yearly (LFS 2023-2024 / Small Area Estimation 2024 at time of build), so cache
hard. Faithful to Job Bank's own autocomplete: the top typeahead hit is what a user
picking from the site's search box would land on (a short exact token like "nurse" can
outrank a longer phrase -- that is the site's ranking, not ours to second-guess).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional
from urllib.parse import urlencode

from omniseek.core import http
from omniseek.core.normalize import Document, html_fragment_text, jsonsafe, mk_signal
from omniseek.core.sources._base import aparse
from omniseek.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

# Site's own Solr typeahead (English titles). Resolves a job title -> occupation ids.
RESOLVE_URL = "https://www.jobbank.gc.ca/core/ta-jobtitle_en/select"
# Wage page, keyed on the job-title concordance id (see module docstring / og:url proof).
PAGE_URL = "https://www.jobbank.gc.ca/wagereport/occupation/{cid}"
TIMEOUT = 20
# Both steps go straight to the libcurl tier (http.aget_impersonated, Chrome's handshake), the way the
# higheredjobs feed does: this site leaves about half of the handshakes that offer ALPN "http/1.1"
# alone (what httpx offers without h2) unanswered until the timeout, while curl's and a browser's
# handshake go through (brain finding-eye-handshake-hangs-when-alpn-offers-only-http11-jobbank-
# 2026-11-10). Through the shared httpx helper 8 of 10 queries first waited out the openssl timeout
# before its libcurl fallback served them, 12 to 16 s a query. Only Accept is sent: Chrome's own
# User-Agent and header order come with the impersonation, and overriding them would desync it.
HEADERS = {"Accept": "text/html,application/xhtml+xml"}
_JSON_HEADERS = {"Accept": "application/json"}
CACHE_TTL = 604800  # 7 days: the wage tables refresh about yearly, cache hard.

_WS_RE = re.compile(r"\s+")
# Noise words that hurt the title match (Solr searches occupation titles, not wages/geos).
_NOISE = re.compile(r"\b(prevailing|wages?|salary|salaries|pay|hourly|rate|earnings?|"
                    r"in\s+canada|canada|canadian)\b", re.IGNORECASE)


def _clean_query(query: str) -> str:
    """Strip wage/geo noise so the free-text title matches the Job Bank occupation lexicon."""
    q = _NOISE.sub(" ", query or "")
    return _WS_RE.sub(" ", q).strip() or (query or "").strip()


def _text(html_fragment: str) -> str:
    """A fragment to plain text (tags out, entities decoded, whitespace collapsed)."""
    return html_fragment_text(html_fragment)


def _wage(cell_html: str) -> Optional[float]:
    """A wage cell -> float dollars/hour. 'N/A' (data-suppressed regions) / blank -> None."""
    txt = _text(cell_html).replace("$", "").replace(",", "")
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*$", txt)
    return float(m.group(1)) if m else None


def _occ_title(html: str) -> str:
    """The occupation name from the page H1 ('Wages for Software engineers and designers')."""
    m = re.search(r'<h1[^>]*id="wb-cont"[^>]*>(.*?)</h1>', html, re.S)
    title = _text(m.group(1)) if m else ""
    return re.sub(r"^\s*Wages\s+for\s+", "", title, flags=re.IGNORECASE).strip()


def _reference_period(html: str) -> Optional[str]:
    """The national reference period ('2023-2024') from the first note block, if present."""
    m = re.search(r"Reference period:\s*([^<]+)", html)
    return m.group(1).strip() if m else None


def parse_wage_table(html: str) -> list[dict]:
    """Parse the single wage table into rows: {level, area, low, median, high}. Pure, total.

    level is 'national' | 'province' | 'region' (from the tr class wage-national /
    wage-province / wage-region). Suppressed cells (N/A) become None. A row with no
    parseable area name is dropped.
    """
    tm = re.search(r"<table.*?</table>", html, re.S)
    if not tm:
        return []
    table = tm.group(0)
    out: list[dict] = []
    for m in re.finditer(r'<tr class="(areaGroup[^"]*)">(.*?)</tr>', table, re.S):
        cls, body = m.group(1), m.group(2)
        level = ("national" if "wage-national" in cls
                 else "province" if "wage-province" in cls
                 else "region")
        # The area name lives in the row's <th> (province: a <span>; region: an <a>); the
        # first th, text-stripped, is the name regardless of that inner markup.
        thm = re.search(r"<th[^>]*>(.*?)</th>", body, re.S)
        area = _text(thm.group(1)) if thm else ""
        if not area:
            continue

        def _cellval(suf: str) -> Optional[float]:
            cm = re.search(r'headers="[^"]*header_' + suf + r'"[^>]*>(.*?)</td>', body, re.S)
            return _wage(cm.group(1)) if cm else None

        out.append({
            "level": level,
            "area": area,
            "low": _cellval("min"),
            "median": _cellval("avg"),
            "high": _cellval("max"),
        })
    return out


def _fmt(v: Optional[float]) -> str:
    return f"${v:,.2f}" if isinstance(v, (int, float)) else "N/A"


def build_document(payload: dict) -> Optional[Document]:
    """Resolved-occupation payload (html + ids) -> one Document. Pure, fixture-testable."""
    html = payload.get("html") or ""
    rows = parse_wage_table(html)
    national = next((r for r in rows if r["level"] == "national"), None)
    if not national or national.get("median") is None:
        return None  # no usable national wage -> unusable doc (contract: caller drops)

    occ_title = _occ_title(html) or payload.get("matched_title") or "occupation"
    cid = payload.get("concordance_id")
    noc21 = payload.get("noc21_code") or ""
    noc11 = payload.get("noc_code") or ""
    ref = _reference_period(html)
    url = PAGE_URL.format(cid=cid)

    provinces = [r for r in rows if r["level"] == "province"]
    regions = [r for r in rows if r["level"] == "region"]

    lines = [f"{occ_title}: 加拿大现行时薪 (Job Bank / EI 就业保险工资调查)"]
    lines.append(f"全国 (Canada): 低 {_fmt(national['low'])} · 中位 {_fmt(national['median'])} "
                 f"· 高 {_fmt(national['high'])} (每小时)")
    if ref:
        lines.append(f"参考期: {ref}")
    if noc21:
        lines.append(f"NOC 2021: {noc21}" + (f" (NOC 2011: {noc11})" if noc11 else ""))
    prov_named = [r for r in provinces if r.get("median") is not None]
    if prov_named:
        lines.append("\n各省/地区中位时薪:")
        for r in prov_named:
            lines.append(f"  - {r['area']}: 低 {_fmt(r['low'])} / 中 {_fmt(r['median'])} "
                         f"/ 高 {_fmt(r['high'])}")
    content = "\n".join(lines).strip()

    med = national["median"]
    sig = mk_signal("median_hourly_wage", med, kind="other",
                    by="jobbank/ei-wage-survey", unit="CAD/hour") if med else {}

    return Document(
        source="canada_jobbank_wages",
        source_id=str(cid),
        url=url,
        title=f"Job Bank 加拿大时薪: {occ_title}" + (f"（NOC {noc21}）" if noc21 else ""),
        content=content,
        author="Government of Canada (Job Bank)",
        signals=sig,
        tags=[t for t in ["compensation", "wage", "canada",
                          f"noc21:{noc21}" if noc21 else ""] if t],
        metadata={
            "occupation": occ_title,
            "concordance_id": cid,
            "noc_2021": noc21,
            "noc_2011": noc11,
            "reference_period": ref,
            "currency": "CAD",
            "unit": "hourly",
            "national": jsonsafe(national),
            "provinces": jsonsafe(provinces),
            "regions": jsonsafe(regions),
            "matched_title": payload.get("matched_title"),
        },
    )


class CanadaJobBankWagesAdapter(BaseScrapeAdapter):
    name = "canada_jobbank_wages"
    needs_credentials = False
    description = (
        "Job Bank 加拿大: 官方岗位现行时薪 (低/中位/高, 每小时; 全国 + 各省 + 经济区), 数据源"
        "EI 就业保险工资调查 / 加拿大统计局劳动力调查 (LFS). keyless. 传一个职业名 (\"software "
        "engineer\" / \"registered nurse\" / \"electrician\" / \"data scientist\") → 先经站内 Solr "
        "typeahead 解析成 Job Bank 职业 id, 再抓该职业的官方工资表. 用于加拿大薪酬参考、LMIA / "
        "Express Entry 现行工资锚点, 与 levels.fyi (美国众包总包) 互补. STRUCTURE, region ca."
    )
    cache_ttl = CACHE_TTL
    kind = "lookup"
    domains = ["compensation", "jobs"]
    regions = ["ca"]
    modes = ["STRUCTURE"]
    # The site Solr typeahead fuzzy-matches ANY text to SOME occupation, so in a broad
    # sweep this would emit a confident-looking but irrelevant Canadian wage doc for an
    # unrelated query. Drill it by name, or via compensation + ca routing, not fan-out
    # (the remotive precedent: a curated lookup, never broad-fan-out fodder).
    explicit_only = ("canada_jobbank_wages: a targeted CA-wage lookup; the site typeahead "
                     "fuzzy-matches any text to an occupation, so broad fan-out would return "
                     "irrelevant wage docs. Name it, or route via compensation+ca.")

    async def _aresolve(self, query: str) -> Optional[dict]:
        """Title -> concordance id through the site's Solr typeahead (libcurl tier, see HEADERS). The
        Solr doc walk (pick the first concordance id) is pure CPU. None when the fetch failed (noted
        under http.impersonated_get) or nothing matched."""
        qs = urlencode({"q": _clean_query(query), "wt": "json", "rows": 25})
        body = await http.aget_impersonated(f"{RESOLVE_URL}?{qs}", timeout=TIMEOUT,
                                            headers=_JSON_HEADERS)
        try:
            data = json.loads(body) if body is not None else None
        except ValueError as exc:
            logger.warning("canada_jobbank_wages: typeahead answer is not JSON: %s", exc)
            data = None
        docs = (((data or {}).get("response") or {}).get("docs")) or []
        for d in docs:
            cid = d.get("noc_job_title_concordance_id")
            if cid:
                return {"concordance_id": str(cid), "noc_code": d.get("noc_code"),
                        "noc21_code": d.get("noc21_code"), "matched_title": d.get("title")}
        return None

    async def _araw_fetch(self, query: str, limit: int) -> Optional[dict]:
        """The two-step pipeline: (1) title -> concordance id (``_aresolve``); (2) the wage page, both on
        the libcurl tier (``http.aget_impersonated``: SSRF check and declared gates on every hop, 30MB
        cap; see HEADERS). On any fetch failure it returns None (already logged + ``diag.note``'d as
        "http.impersonated_get"), so the search returns []."""
        if not (query or "").strip():
            return None
        resolved = await self._aresolve(query)
        if not resolved:
            logger.info("canada_jobbank_wages: no occupation match for %r", query)
            return None
        url = PAGE_URL.format(cid=resolved["concordance_id"])
        page = await http.aget_impersonated(url, headers=HEADERS, timeout=TIMEOUT)
        if page is None:
            return None  # wage page fetch failed (logged + diag.note'd as http.impersonated_get) -> []
        html = page.decode("utf-8", "replace")
        resolved["html"] = html
        return resolved

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, dict):
            return []
        doc = build_document(raw)
        return [doc] if doc else []

    async def afetch_url(self, url: str) -> Optional[Document]:
        return None  # search-only

    async def ahealth_check(self) -> tuple[bool, str]:
        # Prove BOTH steps: Solr resolves a common title, and its wage page parses a national row.
        try:
            resolved = await self._aresolve("software engineer")
            if not resolved:
                return False, "typeahead gave no concordance id: the fetch failed or the Solr answer changed (see the http.impersonated_get diag note)"
            page = await http.aget_impersonated(PAGE_URL.format(cid=resolved["concordance_id"]),
                                                headers=HEADERS, timeout=12)
            if page is None:
                return False, "wage page fetch failed (see the http.impersonated_get diag note)"
            html = page.decode("utf-8", "replace")
            rows = await aparse(len(html), parse_wage_table, html)  # here or process lane
            nat = next((x for x in rows if x["level"] == "national"), None)
            if nat and nat.get("median") is not None:
                return True, "OK (typeahead resolve + national wage row)"
            return False, f"national_ok={bool(nat)} rows={len(rows)} (page structure changed?)"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
