"""levels.fyi — tech total-compensation (keyless), two query shapes.

Phase 4 P15 (2026-05-30): originally the no-login `/salaries.md` LLM endpoint.
P41 REBUILD (2026-06-11): that endpoint died; switched to the company page
`/companies/{slug}/salaries` and its Next.js `__NEXT_DATA__` JSON (per-job-family
per-level breakdown + median/highest/lowest). That path still works for a COMPANY
name (US/USD benchmark).

P?? FIX (2026-06-21): the adapter ONLY handled company-name queries, so a natural
ROLE/LOCATION query ("Singapore machine learning engineer total compensation") was
slugged into a bogus company and 404'd to a SILENT EMPTY. levels.fyi now also masks
the on-page percentile table with **** for logged-out users. BUT the public role /
location summary pages still SERVER-RENDER the median + range in their
``og:description`` meta (location-aware, real local currency, exactly what a web
search indexes), and ``__NEXT_DATA__`` still carries percentile JSON. So we add a
role path that parses that meta. Two shapes now:
  - ROLE [+ country]  -> /t/{cat}/locations/{country}  (range) or
                         /t/{cat}/title/{subtitle}/locations/{country}  (median)
                         parsed from og:description (no login, location-accurate).
  - COMPANY name      -> /companies/{slug}/salaries __NEXT_DATA__ (US/USD benchmark).
A role query that can't be resolved / 404s now records a diag note (legible), never
a silent empty.

Taxonomy note: only top-level title CATEGORIES resolve at /t/{cat}; ML-engineer /
research-scientist are SUBTITLES under software-engineer; locations are COUNTRY-level,
cities 404.
"""

from __future__ import annotations

import functools
import html as _html
import json
import logging
import re
import threading
from typing import Optional

import anyio
import httpx

from penumbra.core import cache, diag
from penumbra.core.normalize import Document, jsonsafe, mk_signal

logger = logging.getLogger(__name__)

PAGE_URL = "https://www.levels.fyi/companies/{slug}/salaries"   # company path (US/USD)
TITLE_URL = "https://www.levels.fyi/t/{cat}/locations/{loc}"     # role category path (range)
SUBTITLE_URL = "https://www.levels.fyi/t/{cat}/title/{sub}/locations/{loc}"  # role subtitle (median)
TIMEOUT = 20
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
}
CACHE_TTL = 86400  # 24h — comp data moves slowly

# Role lexicon -> levels.fyi (category, subtitle). Ordered most-specific FIRST. Only verified-or-safe
# mappings: ML-engineer + research-scientist are confirmed SUBTITLES under the software-engineer
# category; the rest are top-level categories (which reliably resolve). An unmapped role -> company path.
_ROLE_RULES = [
    (("machine learning engineer", "ml engineer", "mle", "machine learning", "nlp engineer",
      "deep learning engineer", "ai engineer"), "software-engineer", "machine-learning-engineer"),
    (("research scientist", "research engineer", "applied scientist"), "software-engineer", "research-scientist"),
    (("data scientist", "data science"), "data-scientist", None),
    (("data analyst", "data analytics"), "data-scientist", None),
    (("product manager", "product management", "pm"), "product-manager", None),
    (("hardware engineer",), "hardware-engineer", None),
    (("engineering manager", "eng manager"), "engineering-manager", None),
    (("software engineer", "swe", "backend engineer", "frontend engineer", "full stack",
      "fullstack", "software developer", "data engineer", "devops", "security engineer"),
     "software-engineer", None),
]

# Location phrase -> levels.fyi COUNTRY slug (the /t/ pages are country-level; cities map up to country).
_LOC_RULES = [
    (("singapore", "新加坡"), "singapore"),
    (("toronto", "vancouver", "montreal", "waterloo", "ottawa", "ontario", "quebec",
      "british columbia", "canada", "加拿大"), "canada"),
    (("united states", "usa", "u.s.", " us ", "america", "bay area", "seattle", "new york"), "united-states"),
    (("united kingdom", "london", " uk ", "英国"), "united-kingdom"),
    (("india", "bangalore", "印度"), "india"),
    (("germany", "berlin", "munich"), "germany"),
    (("japan", "tokyo", "日本"), "japan"),
    (("hong kong", "香港"), "hong-kong"),
]

# NB: no trailing \s? in the number group (it would eat the space before ' to ' and drop the range
# high); decimals require digits after the dot (so a sentence-ending '.' is not captured).
_MONEY = re.compile(r"(SGD|CAD|USD|GBP|EUR|INR|AUD|HKD|JPY|CA\$|US\$|S\$|A\$|HK\$|\$|£|€)\s?"
                    r"([0-9][0-9,]*(?:\.[0-9]+)?[KkMm]?)")

# Module-level async client for the native-async twin (asearch). This source does NOT route through
# the shared http.aget* leaves: those call raise_for_status() internally, collapsing every non-2xx to
# None — but BOTH sync egresses INSPECT r.status_code WITHOUT raising to emit a status-DIFFERENTIATED
# legible diag.note (a role-page taxonomy miss vs a company 404 vs a 200-but-structure-changed page;
# see the module docstring's "never a silent empty" contract, and http.py's docstring naming this the
# "levels_fyi precedent" for sources that cannot route through the shared leaf). So the async twin
# keeps its OWN client — lazy, double-checked-lock like _openalex._aget_client / nsfc_awards._aget_client
# — with the SAME follow_redirects / timeout / browser HEADERS as the sync httpx.get it mirrors, so it
# returns the raw Response (non-2xx included) for that same status inspection. Only asearch awaits it;
# the sync search is byte-identical. Posture: a FIXED public host (www.levels.fyi) with the query in
# the URL PATH (a slugged company / a mapped role+country from a closed lexicon), so no SSRF surface is
# opened (same as nsfc_awards / nserc_awards keeping their own client).
_aclient: Optional["httpx.AsyncClient"] = None
_aclient_lock = threading.Lock()  # construction is sync (no await); double-check like http._aget_client


def _aget_client() -> "httpx.AsyncClient":
    global _aclient
    if _aclient is None:
        with _aclient_lock:
            if _aclient is None:
                _aclient = httpx.AsyncClient(
                    headers=HEADERS,
                    timeout=TIMEOUT,
                    follow_redirects=True,
                )
    return _aclient


def _slug(company: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", company.strip().lower()).strip("-")


def _role(query: str) -> Optional[tuple]:
    """(category, subtitle|None) if the query names a known tech role, else None (=> company query)."""
    low = f" {(query or '').lower()} "
    for phrases, cat, sub in _ROLE_RULES:
        for p in phrases:
            if re.search(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", low):
                return (cat, sub)
    return None


def _location(query: str) -> Optional[str]:
    """The levels.fyi COUNTRY slug named in the query, else None."""
    low = f" {(query or '').lower()} "
    for phrases, slug in _LOC_RULES:
        for p in phrases:
            if p in low:
                return slug
    return None


def _money(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        return f"${int(v):,}"
    except (TypeError, ValueError):
        return str(v)


def _num(money: str) -> Optional[int]:
    """'SGD 162,754' / 'CA$157,072' / '167K' / '1.18M' -> int. None if unparseable."""
    s = re.sub(r"[^0-9.KkMm]", "", money or "")
    if not s:
        return None
    mult = 1
    if s[-1] in "Kk":
        mult, s = 1000, s[:-1]
    elif s[-1] in "Mm":
        mult, s = 1_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except (TypeError, ValueError):
        return None


def _parse_comp_meta(desc: str) -> dict:
    """Pull median / low / high / currency out of a levels.fyi og:description (server-rendered,
    location-aware), across its 3 phrasings. Pure (offline golden-fixture-able). {} if nothing parses.
      - subtitle page:  'The median Machine Learning Engineer Salary is SGD 162,754.'
      - category page:  'The average Software Engineer Salary range in Singapore is from SGD 85,107 to SGD 165,133.'
      - company+loc:    'ranges from SGD 114K ... to SGD 477K ... median ... totals SGD 167K.'
    """
    desc = _html.unescape(desc or "")
    monies = list(_MONEY.finditer(desc))
    if not monies:
        return {}
    out: dict = {"currency": monies[0].group(1)}
    low = desc.lower()
    mi = low.find("median")
    if mi >= 0:
        after = [m for m in monies if m.start() > mi]
        if after:
            out["median"] = after[0].group(0).strip()
    fi = low.find("from ")
    if fi >= 0:
        lows = [m for m in monies if m.start() > fi]
        if lows:
            out["low"] = lows[0].group(0).strip()
            ti = low.find(" to ", lows[0].end())
            if ti >= 0:
                highs = [m for m in monies if m.start() > ti]
                if highs:
                    out["high"] = highs[0].group(0).strip()
    return out


def _meta_description(html: str) -> Optional[str]:
    m = re.search(r'<meta\s+name="description"\s+content="([^"]*)"', html)
    if not m:
        m = re.search(r'<meta\s+property="og:description"\s+content="([^"]*)"', html)
    return _html.unescape(m.group(1)) if m else None


def _next_data_pageprops(html: str) -> Optional[dict]:
    """Extract props.pageProps from the page's __NEXT_DATA__ script (string-find, no regex)."""
    i = html.find("__NEXT_DATA__")
    if i < 0:
        return None
    s = html.find(">", i) + 1
    e = html.find("</script>", s)
    if s <= 0 or e < 0:
        return None
    try:
        return (json.loads(html[s:e]).get("props") or {}).get("pageProps") or {}
    except Exception:  # noqa: BLE001
        return None


def _build_doc(slug: str, url: str, pp: dict) -> Optional[Document]:
    """COMPANY path: build a doc from the company page __NEXT_DATA__ pageProps (US/USD benchmark)."""
    co = pp.get("company") or {}
    name = co.get("name") or slug.replace("-", " ").title()
    cur = pp.get("locationCurrency") or "USD"

    def _tc(d) -> str:
        d = d or {}
        return " ".join(x for x in (
            d.get("jobFamily", ""), d.get("level", ""), _money(d.get("totalCompensation")),
            f"({d.get('location')})" if d.get("location") else "") if x).strip()

    lines: list[str] = []
    meta_bits = [b for b in (f"{co.get('year_founded')}创立" if co.get("year_founded") else "",
                             f"{co.get('emp_count')}人" if co.get("emp_count") else "") if b]
    if meta_bits:
        lines.append(" · ".join(meta_bits))
    med = pp.get("medianAcrossAllJobFamilies")
    hi = pp.get("highestPayingJobFamilyAndLevel")
    lo = pp.get("lowestPayingJobFamilyAndLevel")
    if isinstance(med, dict) and med.get("totalCompensation"):
        lines.append(f"中位总包(全职位族): {_money(med.get('totalCompensation'))}")
    if isinstance(hi, dict):
        lines.append(f"最高: {_tc(hi)}")
    if isinstance(lo, dict):
        lines.append(f"最低: {_tc(lo)}")

    msd = pp.get("modalSalariesData") or []
    if isinstance(msd, list) and msd:
        lines.append("\n各职位薪资:")
        for it in msd[:40]:
            if isinstance(it, dict) and it.get("label"):
                lines.append(f"  - {it.get('label')}: {it.get('value', '')}")

    fam_lines: list[str] = []
    for fam in (pp.get("overview") or [])[:20]:
        if not isinstance(fam, dict):
            continue
        bd = fam.get("breakdown")
        if isinstance(bd, str):
            try:
                bd = json.loads(bd)
            except Exception:  # noqa: BLE001
                bd = None
        if isinstance(bd, list) and bd:
            levs = ", ".join(
                f"{b.get('level', '')} {_money(b.get('totalCompensation') or b.get('total') or b.get('avgTotalCompensation'))}"
                for b in bd[:8] if isinstance(b, dict) and b.get("level"))
            if levs.strip():
                fam_lines.append(f"  - {fam.get('name', '')}: {levs}")
    if fam_lines:
        lines.append("\n各职位族级别明细:")
        lines.extend(fam_lines)

    content = "\n".join(lines).strip()
    if not content:
        return None
    return Document(
        source="levels_fyi",
        source_id=slug,
        url=url,
        title=f"levels.fyi 薪酬 — {name}（{cur}, US 基准）",
        content=content,
        author="levels.fyi",
        tags=["compensation", "salary", f"company:{slug}"],
        metadata={"company": name, "slug": slug, "currency": cur, "kind": "company",
                  "median": jsonsafe(med), "highest": jsonsafe(hi), "lowest": jsonsafe(lo),
                  "raw": jsonsafe({"company": co, "modalSalariesData": msd})},
    )


class LevelsFyiAdapter:
    name = "levels_fyi"
    needs_credentials = False
    description = (
        "levels.fyi — 科技公司/岗位薪酬 (TC 黄金标准, keyless). 两种查法: (1) 岗位[+国家] "
        "(\"machine learning engineer singapore\" / \"data scientist canada\") → 该岗位在该国的"
        "中位 + 区间 (本币, location-accurate, 取自页面 og:description); (2) 公司名 (\"bytedance\") "
        "→ 该公司各职位族/级别总包 (US/USD 基准). 用于谈 offer / 比较雇主薪酬参考."
    )

    # ---- ROLE path (the fix): /t/ pages, location-accurate median+range from og:description ----
    def _role_search(self, query: str, role: tuple, country: Optional[str]) -> list[Document]:
        cat, sub = role
        loc = country or "united-states"  # /t/ pages need a location; default to the US benchmark
        url = (SUBTITLE_URL.format(cat=cat, sub=sub, loc=loc) if sub
               else TITLE_URL.format(cat=cat, loc=loc))
        key = cache.make_key("levels_fyi", "role", cat, sub or "-", loc)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached
        try:
            r = httpx.get(url, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            diag.note("levels_fyi", url=url, exc=exc)
            return []
        if r.status_code != 200:
            # Legible, not silent: a taxonomy miss (unmapped subtitle / unknown country) records WHY.
            diag.note("levels_fyi", url=url, status=r.status_code,
                      body=f"role page not found (cat={cat} sub={sub} loc={loc}); taxonomy miss")
            return []
        desc = _meta_description(r.text)
        comp = _parse_comp_meta(desc) if desc else {}
        if not desc or not comp:
            diag.note("levels_fyi", url=url, status=200,
                      body="200 but no parseable comp in og:description (page structure changed?)")
            return []
        doc = self._role_doc(query, cat, sub, loc, url, desc, comp)
        docs = [doc] if doc else []
        cache.set_docs(key, docs, ttl=CACHE_TTL)
        return docs

    @staticmethod
    def _role_doc(query, cat, sub, loc, url, desc, comp) -> Optional[Document]:
        role_disp = (sub or cat).replace("-", " ").title()
        loc_disp = loc.replace("-", " ").title()
        cur = comp.get("currency") or ""
        bits = [f"{role_disp} @ {loc_disp}", desc]
        struct = []
        if comp.get("median"):
            struct.append(f"中位: {comp['median']}")
        if comp.get("low") and comp.get("high"):
            struct.append(f"区间: {comp['low']} – {comp['high']}")
        if struct:
            bits.append(" · ".join(struct))
        med_n = _num(comp.get("median") or comp.get("low") or "")
        sig = (mk_signal("median_total_comp", med_n, kind="other",
                         by="levels_fyi/og:description", unit=cur) if med_n else {})
        return Document(
            source="levels_fyi",
            source_id=f"{cat}/{sub or '-'}/{loc}",
            url=url,
            title=f"levels.fyi 薪酬 — {role_disp} @ {loc_disp}（{cur or '本币'}）",
            content="\n".join(b for b in bits if b).strip(),
            author="levels.fyi",
            signals=sig,
            tags=["compensation", "salary", f"role:{sub or cat}", f"country:{loc}"],
            metadata={"kind": "role", "category": cat, "subtitle": sub, "country": loc,
                      "currency": cur, "median": comp.get("median"),
                      "range_low": comp.get("low"), "range_high": comp.get("high"),
                      "og_description": desc},
        )

    async def _arole_search(self, query: str, role: tuple, country: Optional[str]) -> list[Document]:
        """Native-async twin of ``_role_search``: BYTE-FAITHFUL mirror, changing ONLY the two blocking
        legs. The URL build, the status-differentiated diag.notes, and the pure-CPU parse
        (``_meta_description`` / ``_parse_comp_meta`` / ``_role_doc``) are identical, on the loop.
          - the disk cache read + write -> ``anyio.to_thread.run_sync`` (get_docs / set_docs do file IO);
          - the raw ``httpx.get`` -> the module-level AsyncClient's ``get`` (SAME headers/timeout, and
            follow_redirects carried on the client). It returns the raw Response WITHOUT raising, so the
            ``r.status_code != 200`` taxonomy-miss branch keeps its exact legible diag.note (a shared
            http.aget* leaf would raise_for_status and lose that status)."""
        cat, sub = role
        loc = country or "united-states"  # /t/ pages need a location; default to the US benchmark
        url = (SUBTITLE_URL.format(cat=cat, sub=sub, loc=loc) if sub
               else TITLE_URL.format(cat=cat, loc=loc))
        key = cache.make_key("levels_fyi", "role", cat, sub or "-", loc)
        cached = await anyio.to_thread.run_sync(cache.get_docs, key)  # disk read OFF loop
        if cached is not None:
            return cached
        try:
            r = await _aget_client().get(url, headers=HEADERS, timeout=TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            diag.note("levels_fyi", url=url, exc=exc)
            return []
        if r.status_code != 200:
            # Legible, not silent: a taxonomy miss (unmapped subtitle / unknown country) records WHY.
            diag.note("levels_fyi", url=url, status=r.status_code,
                      body=f"role page not found (cat={cat} sub={sub} loc={loc}); taxonomy miss")
            return []
        desc = _meta_description(r.text)
        comp = _parse_comp_meta(desc) if desc else {}
        if not desc or not comp:
            diag.note("levels_fyi", url=url, status=200,
                      body="200 but no parseable comp in og:description (page structure changed?)")
            return []
        doc = self._role_doc(query, cat, sub, loc, url, desc, comp)
        docs = [doc] if doc else []
        await anyio.to_thread.run_sync(  # disk write OFF loop
            functools.partial(cache.set_docs, key, docs, ttl=CACHE_TTL))
        return docs

    # ---- COMPANY path (unchanged): /companies/{slug}/salaries __NEXT_DATA__ (US/USD) ----
    def _company_search(self, query: str) -> list[Document]:
        slug = self._resolve_company(query)
        key = cache.make_key("levels_fyi", "nextdata", slug)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached
        url = PAGE_URL.format(slug=slug)
        try:
            r = httpx.get(url, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
            if r.status_code == 404:
                diag.note("levels_fyi", url=url, status=404,
                          body=f"no company page for slug={slug!r} (not a company? try a role+country query)")
                return []
            r.raise_for_status()
            pp = _next_data_pageprops(r.text)
        except Exception as exc:  # noqa: BLE001
            diag.note("levels_fyi", url=url, exc=exc)
            return []
        if not pp or not pp.get("company"):
            diag.note("levels_fyi", url=url, status=200, body="200 but no __NEXT_DATA__ company data")
            return []
        doc = _build_doc(slug, url, pp)
        docs = [doc] if doc else []
        cache.set_docs(key, docs, ttl=CACHE_TTL)
        return docs

    async def _acompany_search(self, query: str) -> list[Document]:
        """Native-async twin of ``_company_search``: BYTE-FAITHFUL mirror, changing ONLY the two blocking
        legs. The slug resolve, the ``status_code == 404`` legible note THEN ``raise_for_status`` for other
        non-2xx (caught by the generic except -> its diag.note), and the pure-CPU ``_next_data_pageprops``
        / ``_build_doc`` are identical, on the loop.
          - the disk cache read + write -> ``anyio.to_thread.run_sync`` (get_docs / set_docs do file IO);
          - the raw ``httpx.get`` -> the module-level AsyncClient's ``get`` (SAME headers/timeout, and
            follow_redirects carried on the client), returning the raw Response so the ``== 404`` branch
            keeps its exact legible diag.note before ``raise_for_status`` (a shared http.aget* leaf would
            raise on the 404 and lose that status-specific note)."""
        slug = self._resolve_company(query)
        key = cache.make_key("levels_fyi", "nextdata", slug)
        cached = await anyio.to_thread.run_sync(cache.get_docs, key)  # disk read OFF loop
        if cached is not None:
            return cached
        url = PAGE_URL.format(slug=slug)
        try:
            r = await _aget_client().get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 404:
                diag.note("levels_fyi", url=url, status=404,
                          body=f"no company page for slug={slug!r} (not a company? try a role+country query)")
                return []
            r.raise_for_status()
            pp = _next_data_pageprops(r.text)
        except Exception as exc:  # noqa: BLE001
            diag.note("levels_fyi", url=url, exc=exc)
            return []
        if not pp or not pp.get("company"):
            diag.note("levels_fyi", url=url, status=200, body="200 but no __NEXT_DATA__ company data")
            return []
        doc = _build_doc(slug, url, pp)
        docs = [doc] if doc else []
        await anyio.to_thread.run_sync(  # disk write OFF loop
            functools.partial(cache.set_docs, key, docs, ttl=CACHE_TTL))
        return docs

    @staticmethod
    def _resolve_company(query: str) -> str:
        q = (query or "").strip()
        low = f" {q.lower()} "
        for phrases, _slug_ in _LOC_RULES:  # strip country/city words so "google singapore" -> "google"
            for p in phrases:
                if p in low:
                    q = re.sub(re.escape(p), "", q, flags=re.IGNORECASE).strip(" ,")
        return _slug(q or "google")

    def search(self, query: str, limit: int = 10) -> list[Document]:
        if not (query or "").strip():
            return []
        role = _role(query)
        if role:  # role[+country] query -> location-accurate /t/ page (the fix)
            return self._role_search(query, role, _location(query))
        return self._company_search(query)  # else treat as a company name (US/USD benchmark)

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` (S4b) -> AsyncSearchCapable, so the penumbra_search fan-out awaits
        this DIRECTLY instead of pushing the sync ``.search`` onto the shared thread pool; the one
        network wait costs a COROUTINE, not a held pool thread. Mirrors ``search`` line-for-line: the
        SAME empty guard + the SAME ``_role`` / ``_location`` routing (pure CPU, on the loop), dispatching
        to the async twin of whichever path ``search`` would take. Behavior-identical to ``search`` given
        identical egress (the two twins share every pure-CPU helper + the SAME cache keys + CACHE_TTL)."""
        if not (query or "").strip():
            return []
        role = _role(query)
        if role:  # role[+country] query -> location-accurate /t/ page (the fix)
            return await self._arole_search(query, role, _location(query))
        return await self._acompany_search(query)  # else treat as a company name (US/USD benchmark)

    def fetch_url(self, url: str) -> Optional[Document]:
        return None  # search-only

    def health_check(self) -> tuple[bool, str]:
        # Probe BOTH paths: a role page (the fixed path) + the company page.
        try:
            ru = SUBTITLE_URL.format(cat="software-engineer", sub="machine-learning-engineer", loc="singapore")
            rr = httpx.get(ru, headers=HEADERS, timeout=12, follow_redirects=True)
            role_ok = rr.status_code == 200 and bool(_parse_comp_meta(_meta_description(rr.text) or ""))
            cr = httpx.get(PAGE_URL.format(slug="google"), headers=HEADERS, timeout=12, follow_redirects=True)
            comp_ok = cr.status_code == 200 and bool((_next_data_pageprops(cr.text) or {}).get("company"))
            if role_ok and comp_ok:
                return True, "OK (role og:description + company __NEXT_DATA__)"
            return False, f"role_ok={role_ok} comp_ok={comp_ok} (page structure changed?)"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"


from penumbra.core.fetcher import register_adapter

register_adapter(LevelsFyiAdapter())
