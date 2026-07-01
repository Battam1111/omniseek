"""Ontario Sunshine List: public-sector salary disclosure (>= CAD 100k), STRUCTURED.

Faculty/PI comp at Ontario universities = a real Canada-path signal. The official open
data is on data.ontario.ca, a CKAN portal, KEYLESS for public datasets. Reachable via
plain HTTP from the mini (PROVINCIAL host, NOT canada.ca, so no transport wall, no CDP;
verified 2026-06). The ontario.ca web front-end parses 0 docs; the CKAN datastore IS the
structured interface.

Access (verified 2026-06): the list is ONE CKAN package PER YEAR
(`public-sector-salary-disclosure-YYYY`); open data currently ends at 2020 (2021-2024 are
HTML-front-end-only, 404 as CKAN packages). package_search resolves the latest per-year
package; datastore_search on its 'All sectors' resource returns records with full-text q=.
Column names VARY BY YEAR (human-typed: 'Salary' vs 'Salary Paid', 'Last name' vs
'Last Name', etc.), so read every field defensively via _f(). Mode: STRUCTURE. lookup +
explicit_only: named calls only (each call fires an external CKAN query; name-level PII
must never enter the broad fan-out or the recall corpus).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from penumbra.core import cache, http
from penumbra.core.normalize import PolarisDocument, jsonsafe

logger = logging.getLogger(__name__)

CKAN = "https://data.ontario.ca/api/3/action"
PKG_SEARCH = f"{CKAN}/package_search"
DATASTORE = f"{CKAN}/datastore_search"
PKG_QUERY = "public sector salary disclosure"
_YEAR_RE = re.compile(r"public-sector-salary-disclosure-(\d{4})")
CACHE_TTL = 86400          # 24h: per-query record cache (annual data, moves slowly)
RESOURCE_TTL = 86400 * 7   # 7d: latest resource id moves once a year
_MAX_LIMIT = 25            # hard cap: name-level PII + external query => small top-N only
_DROP = ("ontario", "canada", "加拿大", "安大略")  # region words stripped from the query


def _f(rec: dict, *aliases: str) -> str:
    """First present alias value (handles per-year column renames), else ''. Never raises."""
    for a in aliases:
        v = rec.get(a)
        if v:
            return str(v)
    return ""


def _get_json_retry(url: str, params: dict, *, timeout: int = 20, tries: int = 2):
    """http.get_json with a small bounded retry (the one-off TLS UNEXPECTED_EOF seen in probing).
    http.get_json already returns None on any failure + honors cache_only + the 30MB cap."""
    for _ in range(tries):
        data = http.get_json(url, params=params, timeout=timeout)
        if data is not None:
            return data
    return None


def _resolve_latest_resource() -> Optional[tuple[str, int]]:
    """(resource_id, year) of the most-recent 'All sectors' datastore-active resource.
    package_search -> max year whose name matches salary-disclosure-YYYY -> its 'All sectors'
    resource with datastore_active (dedup resources by id). Cached (RESOURCE_TTL). None on failure.
    Do NOT hardcode a resource_id (they rot per year; open data currently ends at 2020)."""
    ck = cache.make_key("ontario_sunshine", "latest_resource", "v1")
    hit = cache.get(ck)
    if hit and isinstance(hit, list) and len(hit) == 2:
        return (hit[0], int(hit[1]))
    data = _get_json_retry(PKG_SEARCH, {"q": PKG_QUERY, "rows": 40})
    if not data or not data.get("success"):
        return None
    best_year, best_pkg = -1, None
    for pkg in (data.get("result") or {}).get("results", []) or []:
        m = _YEAR_RE.match(pkg.get("name", "") or "")
        if m and int(m.group(1)) > best_year:
            best_year, best_pkg = int(m.group(1)), pkg
    if not best_pkg:
        return None
    seen, rid = set(), None
    for r in best_pkg.get("resources", []) or []:
        if r.get("id") in seen:
            continue
        seen.add(r.get("id"))
        if (r.get("name", "") or "").startswith("All sectors") and r.get("datastore_active"):
            rid = r.get("id")
            break
    if not rid:
        return None
    cache.set(ck, [rid, best_year], ttl=RESOURCE_TTL)
    return (rid, best_year)


def _build_doc(rec: dict, year: int) -> Optional[PolarisDocument]:
    last = _f(rec, "Last name", "Last Name")
    first = _f(rec, "First name", "First Name")
    name = f"{first} {last}".strip()
    if not name:
        return None
    employer = _f(rec, "Employer")
    sector = _f(rec, "Sector")
    position = _f(rec, "Job title", "Job Title")
    salary = _f(rec, "Salary", "Salary Paid")
    benefits = _f(rec, "Benefits", "Taxable Benefits")
    yr = _f(rec, "Year", "Calendar Year") or str(year)
    return PolarisDocument(
        source="ontario_sunshine",
        source_id=f"{year}|{rec.get('_id')}",
        url="https://www.ontario.ca/page/public-sector-salary-disclosure",
        title=f"{name} - {salary} @ {employer} ({yr})",
        content="\n".join([
            f"Name: {name}",
            f"Employer: {employer}  ·  Sector: {sector}",
            f"Job title: {position}",
            f"Salary: {salary}  ·  Benefits: {benefits}  ·  Year: {yr}",
        ]),
        tags=["compensation", "salary", "ca", sector],
        metadata={"employer": employer, "sector": sector, "position": position,
                  "name": name, "salary": salary, "benefits": benefits, "year": yr,
                  "raw": jsonsafe(rec)},
    )


class OntarioSunshineAdapter:
    name = "ontario_sunshine"
    needs_credentials = False
    kind = "lookup"
    domains = ["compensation"]
    regions = ["ca"]
    modes = ["STRUCTURE"]
    explicit_only = "Ontario salary-disclosure lookup (CKAN query per call; name-level PII; named-only)"
    description = (
        "安大略 Sunshine List: 公共部门薪酬披露 (>= CAD 10万, 含大学/教职, data.ontario.ca "
        "官方 CKAN, keyless). 查人名/雇主/职称 → 该人薪资+福利+部门+年份. 安大略大学 PI/教职 "
        "薪酬的公开参考. 开放数据目前止于 2020 (2021+ 仅前端). 空 query 无意义 → "
        "传人名或机构名 (如 'University of Toronto')."
    )

    def search(self, query: str, limit: int = 10) -> list[PolarisDocument]:
        q = (query or "").strip()
        if not q:
            return []  # targeted lookup: empty query must NEVER trigger a bulk pull
        low = q.lower()
        for kw in _DROP:
            if kw in low:
                q = re.sub(re.escape(kw), "", q, flags=re.IGNORECASE).strip(" ,")
        if not q:
            return []
        lim = max(1, min(limit, _MAX_LIMIT))
        key = cache.make_key("ontario_sunshine", "q", q, lim)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached
        rr = _resolve_latest_resource()
        if not rr:
            return []
        rid, year = rr
        data = _get_json_retry(DATASTORE, {"resource_id": rid, "q": q, "limit": min(lim * 2, 50)})
        if not data or not data.get("success"):
            return []
        records = (data.get("result") or {}).get("records", []) or []
        docs = [d for d in (_build_doc(r, year) for r in records) if d][:lim]
        cache.set_docs(key, docs, ttl=CACHE_TTL)
        return docs

    def fetch_url(self, url: str) -> Optional[PolarisDocument]:
        return None  # structured lookup; reach via search (no arbitrary-URL fan-in)

    def health_check(self) -> tuple[bool, str]:
        rr = _resolve_latest_resource()
        if not rr:
            return False, "could not resolve latest salary-disclosure resource"
        rid, year = rr
        data = _get_json_retry(DATASTORE, {"resource_id": rid, "limit": 1})
        recs = ((data or {}).get("result") or {}).get("records", []) if data else []
        if not recs:
            return False, f"resolved resource {rid} (year {year}) but datastore returned no record"
        return True, f"OK (latest resource {rid}, year {year})"


from penumbra.core.fetcher import register_adapter

register_adapter(OntarioSunshineAdapter())
