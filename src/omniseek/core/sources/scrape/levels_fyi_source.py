"""levels.fyi — tech total-compensation (keyless), two query shapes.

Phase 4 (2026-05-30): originally the no-login `/salaries.md` LLM endpoint.
REBUILD (2026-06-11): that endpoint died; switched to the company page
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
research-scientist are SUBTITLES under software-engineer; locations are countries plus some metro
slugs of levels.fyi's own (hong-kong-sar, greater-toronto-area; a plain city name 404s). A place the
lexicon does not know is tried as its own slug and, when that 404s, reported as not recognized; it is
never replaced by another country (2026-11-18).

WAF note (2026-11-01): www.levels.fyi is behind AWS WAF (CloudFront). The page
structure did NOT change; what broke was the WAF: a request without browser navigation headers
gets 405 + a "Human Verification" CAPTCHA page, and from a shared proxy-node IP even a
browser-looking request gets 202 + ``x-amzn-waf-action: challenge`` (a JS proof-of-work page)
after a handful of requests in a few minutes. HEADERS carries the navigation headers; the health
check names a WAF block instead of "page structure changed". If the challenge persists on the
eye's egress, the options are an egress the WAF does not challenge, or levels.fyi's official MCP
endpoint https://mcp.levels.fyi/ (listed in /llms.txt; no WAF, anonymous 20 calls per 60 s, but
USD only and no title filter, so "ML engineer @ Singapore" becomes the software-engineer family).

URL read (2026-11-19): ``omniseek_read`` of a levels.fyi page (a city / role page such as
/t/software-engineer/locations/greater-hong-kong-area, a company page, with or without a locale prefix
like /zh-cn/) falling to the generic web read, which sends no navigation headers, gets the WAF's
challenge and the read came back as "plain+jina both thin (plain=0 chars)". ``afetch_url``
now claims those pages and reads them like the search does (HEADERS, the same client; a locale page
is read at its English path, since the WAF challenges /zh-cn/... even with those headers): the page's
description, the percentile JSON of __NEXT_DATA__ converted to the page's currency
(``jobFamilyHistogram`` / ``serverJobTitlePercentiles`` / ``percentiles.tc``, USD per year times
``locationExchangeRate``), the per-level averages, the top-paying companies and the page's FAQ
(``...QuestionsAndAnswers`` or a ``...FaqSchema`` JSON-LD string). A WAF challenge is reported as one
(``levels_fyi.fetch_url``), never as a thin page. Other paths (/jobs/..., /internships/...) are not claimed.
Recon for a separate structured source (11-19): no public JSON API (api.levels.fyi wants a login
token; the page's own data is __NEXT_DATA__, which this source already reads), so none was added.
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

from omniseek.core import cache, diag, http, portal
from omniseek.core.normalize import Document, html_fragment_text, jsonsafe, mk_signal
from omniseek.core.sources._base import SourceBase, aparse

logger = logging.getLogger(__name__)

PAGE_URL = "https://www.levels.fyi/companies/{slug}/salaries"   # company path (US/USD)
TITLE_URL = "https://www.levels.fyi/t/{cat}/locations/{loc}"     # role category path (range)
SUBTITLE_URL = "https://www.levels.fyi/t/{cat}/title/{sub}/locations/{loc}"  # role subtitle (median)
TIMEOUT = 20
# 2026-11-01: levels.fyi sits behind AWS WAF; a request without a browser's top-level-navigation
# headers (Sec-Fetch-Site / Sec-Fetch-Dest / Upgrade-Insecure-Requests) gets a 405/202 "Human
# Verification" CAPTCHA page instead of the salary page. With them the page is served.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Dest": "document",
    "Upgrade-Insecure-Requests": "1",
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

# Location phrase -> levels.fyi location slug. Most /t/ pages are country-level, but levels.fyi also
# keeps metro pages under its own slugs (checked 2026-11-18: china, hong-kong-sar, greater-toronto-area,
# greater-vancouver and san-francisco-bay-area answer 200 with the location's own median;
# new-york-city-area is linked from the US page; greater-montreal per the 11-18 loc1118 worker report).
# "hong-kong" is a 404 (the slug is hong-kong-sar). A city with no metro slug listed here maps up to its
# OWN country. A place not listed is never mapped to some other country: asearch tries it as a slug of
# its own and says so when levels.fyi has no such page (the 11-18 "machine learning engineer china"
# query came back as the United States page, a wrong number with no warning).
_LOC_RULES = [
    (("singapore", "新加坡"), "singapore"),
    (("hong kong", "hongkong", "香港"), "hong-kong-sar"),
    (("china", "中国", "mainland"), "china"),
    (("toronto", "多伦多"), "greater-toronto-area"),
    (("vancouver", "温哥华"), "greater-vancouver"),
    (("montreal", "蒙特利尔"), "greater-montreal"),
    (("waterloo", "ottawa", "ontario", "quebec", "british columbia", "canada", "加拿大"), "canada"),
    (("bay area", "san francisco", "silicon valley"), "san-francisco-bay-area"),
    (("new york", "nyc"), "new-york-city-area"),
    (("united states", "usa", "u.s.", " us ", "america", "seattle"), "united-states"),
    (("united kingdom", "london", " uk ", "英国"), "united-kingdom"),
    (("india", "bangalore", "印度"), "india"),
    (("germany", "berlin", "munich"), "germany"),
    (("japan", "tokyo", "日本"), "japan"),
]

# Words of a role query that name neither the role nor a place: what is left after removing them and the
# role phrases is the place the query asked for (see _place_residue).
_FILLER = frozenset("""salary salaries comp compensation total tc pay pays paid median average avg range
    in at for of the a an and or vs versus how much is what whats package offer offers base stock bonus
    equity level levels senior staff junior entry new grad principal lead annual yearly per year
    薪资 薪酬 工资 年薪 待遇""".split())

# NB: no trailing \s? in the number group (it would eat the space before ' to ' and drop the range
# high); decimals require digits after the dot (so a sentence-ending '.' is not captured).
_MONEY = re.compile(r"(SGD|CAD|USD|GBP|EUR|INR|AUD|HKD|JPY|CNY|CHF|CA\$|US\$|S\$|A\$|HK\$|NT\$|CN¥|JP¥|¥|₹|\$|£|€)\s?"
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
# opened (same as nsfc_awards keeping its own client).
_aclient: Optional["httpx.AsyncClient"] = None
_aclient_lock = threading.Lock()  # construction is sync (no await); double-check like http._aget_client


def _aget_client() -> "httpx.AsyncClient":
    global _aclient
    if _aclient is None:
        with _aclient_lock:
            if _aclient is None:
                _aclient = http.AsyncHopClient(   # the redirect rule on every hop
                    headers=HEADERS,
                    timeout=TIMEOUT,
                    follow_redirects=True,
                )
    return _aclient


def _waf_block(r) -> Optional[str]:
    """What the AWS WAF did to this response, else None: its ``x-amzn-waf-action`` header, or the
    202 / 405 "Human Verification" page it serves without one (11-18: the company page and the .md
    page answered the host machine's exit 202 / 405)."""
    waf = r.headers.get("x-amzn-waf-action")
    if waf:
        return waf
    if r.status_code in (202, 405) or "Human Verification" in r.text[:600]:
        return f"challenge (HTTP {r.status_code})"
    return None


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
    """The levels.fyi location slug of a place in _LOC_RULES named in the query, else None."""
    low = f" {(query or '').lower()} "
    for phrases, slug in _LOC_RULES:
        for p in phrases:
            if p in low:
                return slug
    return None


def _place_residue(query: str) -> str:
    """What a role query says besides its role phrases and _FILLER words, lowercased: the place it asked
    for when _location knows none ("" when it named no place). Pure."""
    low = (query or "").lower()
    phrases = sorted((p for ps, _c, _s in _ROLE_RULES for p in ps), key=len, reverse=True)
    for p in phrases:
        low = re.sub(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", " ", low)
    toks = [t for t in re.split(r"[\s,;:/()|+]+", low) if t]
    return " ".join(t for t in toks if t not in _FILLER and not t.isdigit())


def _page_location(html: str) -> Optional[dict]:
    """The page's own ``locationInfo`` ({slug, displayName, ...}) from __NEXT_DATA__, else None."""
    m = re.search(r'"locationInfo":(\{[^{}]*\})', html)
    if not m:
        return None
    try:
        info = json.loads(m.group(1))
    except Exception:  # noqa: BLE001
        return None
    return info if isinstance(info, dict) else None


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
    # the number and its own K/M suffix only: the K of a currency prefix (HK$) is not a thousand
    m = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s?([KkMm]?)", money or "")
    if not m:
        return None
    s, suf = m.group(1).replace(",", ""), m.group(2).lower()
    mult = {"k": 1000, "m": 1_000_000}.get(suf, 1)
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
    # medianAcrossAllJobFamilies was an object {totalCompensation: ...}; since 2026-11 it is a bare number
    med_tc = med.get("totalCompensation") if isinstance(med, dict) else med
    if med_tc and not isinstance(med_tc, (dict, list)):
        lines.append(f"中位总包(全职位族): {_money(med_tc)}")
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


# A levels.fyi page this source reads by URL: an optional locale segment (/zh-cn/, /en-gb/), then a role /
# location page (/t/...) or a company salary page (/companies/{slug}/salaries...).
_URL_PATH = re.compile(r"^/(?:([a-z]{2}(?:-[a-z]{2,4})?)/)?(t/[^?#]+|companies/[^/?#]+/salaries(?:/[^?#]*)?)/?$", re.I)


def _faq(pp: dict) -> list[tuple[str, str]]:
    """The page's FAQ as (question, answer) pairs: a ``...QuestionsAndAnswers`` list, else the first
    ``...FaqSchema`` JSON-LD string. Pure."""
    for k, v in pp.items():
        if k.endswith("QuestionsAndAnswers") and isinstance(v, list):
            out = [(html_fragment_text(q.get("question", "")), html_fragment_text(q.get("answer", "")))
                   for q in v if isinstance(q, dict)]
            if out:
                return out
    for k, v in pp.items():
        if k.endswith("FaqSchema") and isinstance(v, str):
            try:
                ents = (json.loads(v) or {}).get("mainEntity") or []
            except Exception:  # noqa: BLE001
                continue
            out = [(html_fragment_text(e.get("name", "")), html_fragment_text((e.get("acceptedAnswer") or {}).get("text", "")))
                   for e in ents if isinstance(e, dict)]
            if out:
                return out
    return []


def _page_doc(url: str, html: str) -> Optional[Document]:
    """A levels.fyi role / location / company page as a document: its description, total-compensation
    percentiles and per-level averages in the page's currency (the JSON is USD per year; times
    ``locationExchangeRate``), top-paying companies and FAQ. None when the page carries none of them. Pure."""
    pp = _next_data_pageprops(html) or {}
    path = _URL_PATH.match(httpx.URL(url).path)
    slug_m = re.match(r"companies/([^/]+)/salaries/?$", (path.group(2) if path else ""), re.I)
    if slug_m and pp.get("company") and pp.get("overview") is not None:
        return _build_doc(slug_m.group(1), url, pp)   # the plain company page: the search's own builder
    m = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"', html) or re.search(r"<title>([^<]*)</title>", html)
    title = _html.unescape(m.group(1)).strip() if m else url
    desc = _meta_description(html) or ""
    cur = pp.get("locationCurrency") or "USD"
    try:
        rate = float(pp.get("locationExchangeRate") or 1.0)
    except (TypeError, ValueError):
        rate = 1.0

    def money(v) -> str:
        try:
            return f"{cur} {round(float(v) * rate):,}"
        except (TypeError, ValueError):
            return "?"

    lines: list[str] = [desc] if desc else []
    pct, n = None, None
    for src in (pp.get("jobFamilyHistogram"), pp.get("serverJobTitlePercentiles"), pp.get("percentiles")):
        if isinstance(src, dict):
            tc = src.get("totalCompensation") or src.get("tc")
            if isinstance(tc, dict) and tc.get("p50") is not None:
                pct, n = src, src.get("count")
                break
    if pct:
        for label, key in (("总包", ("totalCompensation", "tc")), ("base", ("baseSalary", "base_salary")),
                           ("股票", ("stockGrant", "stock_grant")), ("奖金", ("bonus",))):
            d = next((pct[k] for k in key if isinstance(pct.get(k), dict)), None)
            if d:
                cells = ", ".join(f"{q} {money(d[q])}" for q in ("p10", "p25", "p50", "p75", "p90") if d.get(q) is not None)
                lines.append(f"{label}分位（年，{cur}{f'，n={n}' if n is not None else ''}）: {cells}")
    avgs = [a for a in (pp.get("averages") or []) if isinstance(a, dict) and a.get("level")]
    if avgs:
        lines.append(f"\n各级别平均（年，{cur}）:")
        for a in avgs[:20]:
            lines.append(f"  - {a.get('level')}: 总包 {money(a.get('total'))}（base {money(a.get('base'))}, "
                         f"股票 {money(a.get('stock'))}, 奖金 {money(a.get('bonus'))}; n={a.get('count', '?')}）")
    tops = [t for t in (pp.get("topPayingCompanies") or []) if isinstance(t, dict) and t.get("name")]
    if tops:
        lines.append("\n薪酬最高的公司（年总包）: " + ", ".join(f"{t['name']} {money(t.get('totalCompensation'))}"
                                                     for t in tops[:10]))
    qa = _faq(pp)
    if qa:
        lines.append("\n常见问题（页面原文）:")
        lines.extend(f"  Q: {q}\n  A: {a}" for q, a in qa[:8] if q and a)
    if not (pct or avgs or tops or qa):
        return None
    comp = _parse_comp_meta(desc) if desc else {}
    med_n = _num(comp.get("median") or "") if comp else None
    if med_n is None and pct:
        tc = pct.get("totalCompensation") or pct.get("tc") or {}
        try:
            med_n = round(float(tc["p50"]) * rate)
        except (KeyError, TypeError, ValueError):
            med_n = None
    return Document(
        source="levels_fyi",
        source_id=httpx.URL(url).path.strip("/"),
        url=url,
        title=f"levels.fyi — {title}",
        content="\n".join(lines).strip(),
        author="levels.fyi",
        signals=(mk_signal("median_total_comp", med_n, kind="other", by="levels_fyi/page", unit=cur)
                 if med_n else {}),
        tags=["compensation", "salary"],
        metadata={"kind": "page", "currency": cur, "exchange_rate_from_usd": rate,
                  "location": pp.get("location") or (pp.get("locationInfo") or pp.get("locationMeta") or {}).get("displayName"),
                  "job_family": pp.get("jobFamily"), "count": n,
                  "percentiles_usd": jsonsafe({k: pct.get(k) for k in ("totalCompensation", "tc", "baseSalary", "base_salary")
                                               if pct and pct.get(k)}),
                  "data_date": pp.get("dataDate")},
    )


class LevelsFyiAdapter(SourceBase):
    name = "levels_fyi"
    needs_credentials = False
    description = (
        "levels.fyi：科技公司/岗位薪酬 (TC 黄金标准, keyless). 两种查法: (1) 岗位[+地点] "
        "(\"machine learning engineer singapore\" / \"data scientist toronto\") → 该岗位在该地的"
        "中位 + 区间 (本币, location-accurate, 取自页面 og:description; 国家或 levels.fyi 自有的都会区页, "
        "如 hong-kong-sar / greater-toronto-area; 认不出的地点照实报认不出, 不换成别国; 没写地点按美国并在标题注明); "
        "(2) 公司名 (\"bytedance\") "
        "→ 该公司各职位族/级别总包 (US/USD 基准). 用于谈 offer / 比较雇主薪酬参考. "
        "omniseek_read 一个 levels.fyi 薪资页 (/t/... 城市或岗位页, /companies/.../salaries...) 取本币分位、各级别平均与页面 FAQ; "
        "在招岗位用 levels_fyi_jobs."
    )

    @staticmethod
    def _role_doc(query, cat, sub, loc, url, desc, comp, loc_name=None,
                  defaulted=False) -> Optional[Document]:
        role_disp = (sub or cat).replace("-", " ").title()
        loc_disp = loc_name or loc.replace("-", " ").title()
        if defaulted:  # the query named no place: say the page is the US one, not "the" answer
            loc_disp += "（查询未指定地点，按美国）"
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
                      "location_name": loc_name, "location_defaulted": defaulted,
                      "currency": cur, "median": comp.get("median"),
                      "range_low": comp.get("low"), "range_high": comp.get("high"),
                      "og_description": desc},
        )

    async def _arole_search(self, query: str, role: tuple, country: Optional[str],
                            asked: str = "") -> list[Document]:
        """Native-async ``_role_search``: BYTE-FAITHFUL mirror, changing ONLY the two blocking
        legs. The URL build, the status-differentiated diag.notes, and the pure-CPU parse
        (``_meta_description`` / ``_parse_comp_meta`` / ``_role_doc``) are identical, on the loop.
          - the disk cache read + write -> ``anyio.to_thread.run_sync`` (get_docs / set_docs do file IO);
          - the raw ``httpx.get`` -> the module-level AsyncClient's ``get`` (SAME headers/timeout, and
            follow_redirects carried on the client). It returns the raw Response WITHOUT raising, so the
            ``r.status_code != 200`` taxonomy-miss branch keeps its exact legible diag.note (a shared
            http.aget* leaf would raise_for_status and lose that status)."""
        cat, sub = role
        # /t/ pages need a location: a query naming no place gets the US page, labelled as such; a place
        # _LOC_RULES does not know comes in as ``asked`` with its own slug as ``country``.
        loc = country or "united-states"
        url = (SUBTITLE_URL.format(cat=cat, sub=sub, loc=loc) if sub
               else TITLE_URL.format(cat=cat, loc=loc))
        key = cache.make_key("levels_fyi", "role", cat, sub or "-", loc, "default" if country is None else "")
        cached = await anyio.to_thread.run_sync(cache.get_docs, key, limiter=portal.IO_LANE)  # disk read OFF loop
        if cached is not None:
            return cached
        try:
            r = await _aget_client().get(url, headers=HEADERS, timeout=TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            diag.note("levels_fyi", url=url, exc=exc)
            return []
        if r.status_code != 200:
            # Legible, not silent: a WAF challenge on our egress is not a taxonomy miss (unmapped
            # subtitle / unknown country); the header or the 202 / 405 status says which one it is.
            waf = _waf_block(r)
            diag.note("levels_fyi", url=url, status=r.status_code,
                      body=(f"AWS WAF {waf} on the role page: our egress is challenged" if waf else
                            f"place not recognized: levels.fyi has no location page {loc!r} for "
                            f"{asked!r} in the query; no other location is substituted" if asked else
                            f"role page not found (cat={cat} sub={sub} loc={loc}); taxonomy miss"))
            return []
        info = _page_location(r.text) or {}
        if info.get("slug") and info["slug"] != loc:  # the page is about another place: its number is not ours
            diag.note("levels_fyi", url=url, status=200,
                      body=f"asked for location {loc!r}, the page is about {info['slug']!r}; not used")
            return []
        desc = _meta_description(r.text)
        comp = _parse_comp_meta(desc) if desc else {}
        if not desc or not comp:
            diag.note("levels_fyi", url=url, status=200,
                      body="200 but no parseable comp in og:description (page structure changed?)")
            return []
        doc = self._role_doc(query, cat, sub, loc, url, desc, comp,
                             loc_name=info.get("displayName") or info.get("name"),
                             defaulted=country is None)
        docs = [doc] if doc else []
        await anyio.to_thread.run_sync(  # disk write OFF loop
            functools.partial(cache.set_docs, key, docs, ttl=CACHE_TTL), limiter=portal.IO_LANE)
        return docs

    async def _acompany_search(self, query: str) -> list[Document]:
        """Native-async ``_company_search``: BYTE-FAITHFUL mirror, changing ONLY the two blocking
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
        cached = await anyio.to_thread.run_sync(cache.get_docs, key, limiter=portal.IO_LANE)  # disk read OFF loop
        if cached is not None:
            return cached
        url = PAGE_URL.format(slug=slug)
        try:
            r = await _aget_client().get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 404:
                diag.note("levels_fyi", url=url, status=404,
                          body=f"no company page for slug={slug!r} (not a company? try a role+country query)")
                return []
            waf = _waf_block(r)
            if waf:  # a 202 challenge is 2xx: without this it read as "200 but no company data"
                diag.note("levels_fyi", url=url, status=r.status_code,
                          body=f"AWS WAF {waf} on the company page: our egress is challenged")
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
            functools.partial(cache.set_docs, key, docs, ttl=CACHE_TTL), limiter=portal.IO_LANE)
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

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async ``search`` -> AsyncSearchCapable, so the omniseek_search fan-out awaits
        this DIRECTLY instead of pushing the sync ``.search`` onto the shared thread pool; the one
        network wait costs a COROUTINE, not a held pool thread. Mirrors ``search`` line-for-line: the
        SAME empty guard + the SAME ``_role`` / ``_location`` routing (pure CPU, on the loop), dispatching
        to the async twin of whichever path ``search`` would take. Behavior-identical to ``search`` given
        identical egress (the two twins share every pure-CPU helper + the SAME cache keys + CACHE_TTL)."""
        if not (query or "").strip():
            return []
        role = _role(query)
        if role:  # role[+place] query -> location-accurate /t/ page (the fix)
            loc = _location(query)
            if loc:
                return await self._arole_search(query, role, loc)
            asked = _place_residue(query)
            if not asked:
                return await self._arole_search(query, role, None)
            slug = _slug(asked)
            if not re.fullmatch(r"[a-z0-9-]+", asked.replace(" ", "-")) or not slug:
                diag.note("levels_fyi", body=(f"place not recognized: {asked!r} in the query is not a "
                                              "levels.fyi location; no other location is substituted"))
                return []
            return await self._arole_search(query, role, slug, asked=asked)
        return await self._acompany_search(query)  # else treat as a company name (US/USD benchmark)

    async def afetch_url(self, url: str) -> Optional[Document]:
        """A levels.fyi role / location / company page (see the module docstring, URL read): read with the
        navigation HEADERS the search uses. None for any other URL (not claimed) and on failure, with a
        note saying which (a WAF challenge, a 404, a page without salary data)."""
        try:
            u = httpx.URL(url)
        except Exception:  # noqa: BLE001
            return None
        m = _URL_PATH.match(u.path)
        if (u.host or "").lower() not in ("www.levels.fyi", "levels.fyi") or not m:
            return None
        asked = url
        if m.group(1):
            # A locale page (/zh-cn/...) sits behind a stricter WAF rule (11-19, the sandbox exit: 405 captcha
            # or 202 challenge with the navigation headers that pass the English page); the English page at
            # the same path carries the same numbers, so that one is read.
            url = str(u.copy_with(path="/" + m.group(2)))
        try:
            r = await _aget_client().get(url, headers=HEADERS, timeout=TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            diag.note("levels_fyi.fetch_url", url=url, exc=exc, headers=HEADERS)
            return None
        waf = _waf_block(r)
        if waf:
            diag.note("levels_fyi.fetch_url", url=url, status=r.status_code, headers=HEADERS,
                      body=(f"blocked: AWS WAF {waf} on our egress, levels.fyi answered its bot check, not "
                            "the page; omniseek_search sources=['levels_fyi'] with a role and place reads the "
                            "same numbers when the WAF lets it through"))
            return None
        if r.status_code == 404:
            diag.note("levels_fyi.fetch_url", url=url, status=404, final=True,
                      body="levels.fyi has no such page (HTTP 404)")
            return None
        if r.status_code != 200:
            diag.note("levels_fyi.fetch_url", url=url, status=r.status_code, body=f"HTTP {r.status_code}")
            return None
        doc = await aparse(len(r.text), _page_doc, url, r.text)
        if doc is not None and url != asked:
            doc.metadata = {**(doc.metadata or {}), "requested_url": asked}
        if doc is None:
            diag.note("levels_fyi.fetch_url", url=url, status=200,
                      body="200 but no salary data in the page (__NEXT_DATA__ / FAQ changed?)")
        return doc

    async def ahealth_check(self) -> tuple[bool, str]:
        # Probe BOTH paths: a role page (the fixed path) + the company page. A leg that fails leaves a
        # failure record with its address, status and the request headers sent (the exit-node sweep
        # refetches it on other nodes with them; the bare eye User-Agent gets 403 on every node).
        legs = (("role", SUBTITLE_URL.format(cat="software-engineer", sub="machine-learning-engineer",
                                             loc="singapore")),
                ("company", PAGE_URL.format(slug="google")))
        res: dict = {}
        for leg, url in legs:
            try:
                r = await http.adirect("GET", url, headers=HEADERS, timeout=12, follow_redirects=True)
            except Exception as exc:  # noqa: BLE001
                diag.note("levels_fyi.health", url=url, exc=exc, headers=HEADERS)
                res[leg] = (None, False, f"{type(exc).__name__}: {exc}")
                continue
            if leg == "role":
                ok = r.status_code == 200 and bool(_parse_comp_meta(_meta_description(r.text) or ""))
            else:
                ok = r.status_code == 200 and bool(
                    (await aparse(len(r.text), _next_data_pageprops, r.text) or {}).get("company"))
            walled = r.status_code in (202, 405) or "Human Verification" in r.text[:600]
            if not ok:
                waf = r.headers.get("x-amzn-waf-action")
                diag.note("levels_fyi.health", url=url, status=r.status_code, headers=HEADERS,
                          body=(f"AWS WAF {waf or 'challenge'} on the {leg} page: our egress is challenged"
                                if walled else f"{leg} page: no parseable comp data"))
            res[leg] = (r.status_code, ok, "walled" if walled else "")
        if all(v[1] for v in res.values()):
            return True, "OK (role og:description + company __NEXT_DATA__)"
        raised = [f"{leg}: {v[2]}" for leg, v in res.items() if v[0] is None]
        if raised:
            return False, "; ".join(raised)
        walled = [leg for leg, v in res.items() if v[2] == "walled"]
        why = (f"AWS WAF challenge on {'+'.join(walled)}; request headers no longer pass?"
               if walled else "page structure changed?")
        return False, (f"role_ok={res['role'][1]}({res['role'][0]}) comp_ok={res['company'][1]}"
                       f"({res['company'][0]}) ({why})")


from omniseek.core.fetcher import register_adapter

register_adapter(LevelsFyiAdapter())
