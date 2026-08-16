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

import functools
import logging
import re
from typing import Optional

import anyio

from omniseek.core import cache, http
from omniseek.core.normalize import Document, jsonsafe

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


async def _aget_json_retry(url: str, params: dict, *, timeout: int = 20, tries: int = 2):
    """Async twin of ``_get_json_retry`` (S4b): SAME bounded retry, the egress swaps
    ``http.get_json`` -> ``await http.aget_json``. Same None-on-failure contract (aget_json already
    returns None on any failure + honors cache_only + the 30MB cap). Stays ON the loop (epoll wait)."""
    for _ in range(tries):
        data = await http.aget_json(url, params=params, timeout=timeout)
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


async def _aresolve_latest_resource() -> Optional[tuple[str, int]]:
    """Async twin of ``_resolve_latest_resource`` (S4b): mirrors it LINE-FOR-LINE with the two blocking
    syscalls moved OFF the loop and the CKAN package_search egress swapped to its async twin.
      - the disk cache read/write -> ``await anyio.to_thread.run_sync`` (cache.get/set do file IO);
      - the package_search egress -> ``await _aget_json_retry`` (-> http.aget_json), ON the loop;
      - the PURE-CPU max-year + 'All sectors' resource walk stays ON the loop, UNCHANGED.
    SAME cache key ("latest_resource","v1") + SAME RESOURCE_TTL as sync, so async and sync share it."""
    ck = cache.make_key("ontario_sunshine", "latest_resource", "v1")
    hit = await anyio.to_thread.run_sync(cache.get, ck)  # disk read OFF loop
    if hit and isinstance(hit, list) and len(hit) == 2:
        return (hit[0], int(hit[1]))
    data = await _aget_json_retry(PKG_SEARCH, {"q": PKG_QUERY, "rows": 40})  # async network
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
    await anyio.to_thread.run_sync(  # disk write OFF loop
        functools.partial(cache.set, ck, [rid, best_year], ttl=RESOURCE_TTL))
    return (rid, best_year)


def _build_doc(rec: dict, year: int) -> Optional[Document]:
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
    return Document(
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

    def search(self, query: str, limit: int = 10) -> list[Document]:
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

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` (S4b): mirrors it LINE-FOR-LINE with three changes only, so
        this standalone lookup adapter routes to the fetcher's native async dispatch branch.
          1. the disk CACHE read/write hop OFF the loop (``await anyio.to_thread.run_sync``);
          2. the two CKAN egresses swap to their async twins (``_aresolve_latest_resource`` for the
             resource resolve, ``_aget_json_retry`` -> http.aget_json for the datastore_search);
          3. the PURE-CPU query scrub (_DROP strip) + ``_build_doc`` map stay ON the loop, UNCHANGED.
        SAME cache key ("q", q, lim) + SAME CACHE_TTL as ``search``, so async and sync share the cache;
        the empty-query / all-region-stripped guards short-circuit BEFORE any egress, exactly as sync."""
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
        cached = await anyio.to_thread.run_sync(cache.get_docs, key)  # disk read OFF loop
        if cached is not None:
            return cached
        rr = await _aresolve_latest_resource()  # async resolve (own cache round-trip + egress)
        if not rr:
            return []
        rid, year = rr
        data = await _aget_json_retry(DATASTORE, {"resource_id": rid, "q": q, "limit": min(lim * 2, 50)})
        if not data or not data.get("success"):
            return []
        records = (data.get("result") or {}).get("records", []) or []
        docs = [d for d in (_build_doc(r, year) for r in records) if d][:lim]
        await anyio.to_thread.run_sync(  # disk write OFF loop
            functools.partial(cache.set_docs, key, docs, ttl=CACHE_TTL))
        return docs

    def fetch_url(self, url: str) -> Optional[Document]:
        return None  # structured lookup; reach via search (no arbitrary-URL fan-in)

    def health_check(self) -> tuple[bool, str]:
        """Report from CACHE; never spend requests on a host that rate-limits by IP.

        MEASURED 2026-07-25: data.ontario.ca returns 429 to this IP for the WHOLE domain (even the
        site root, not just the API), and this source sat in watchdog_down because of it. Meanwhile
        the probe itself was OmniSeek's most frequent caller of that host: it resolved the package
        (package_search) AND read the datastore, each through _get_json_retry, i.e. up to ~4-6
        requests per probe, on the daily lane plus the 6h fast lane (~150 probe cycles a month).
        Whatever Ontario's real budget is, that is the one part of the pressure we control, and
        spending it to ask "are you up?" is backwards for a source used only by an occasional named
        drill. So: answer from the already-cached resource when we have one, and otherwise say plainly
        that nothing was verified. We do NOT reroute egress to dodge the 429; a stated rate limit is
        respected, not evaded. Breakage still surfaces at USE time via the /eye-fix diagnostic."""
        ck = cache.make_key("ontario_sunshine", "latest_resource", "v1")
        hit = cache.get(ck)
        if hit and isinstance(hit, list) and len(hit) == 2:
            return True, f"OK from cache (resource {hit[0]}, year {hit[1]}); host not probed live"
        return True, ("not probed (data.ontario.ca rate-limits by IP; a probe costs several requests "
                      "of the budget the named drill needs). Verified at use time instead.")


from omniseek.core.fetcher import register_adapter

register_adapter(OntarioSunshineAdapter())
