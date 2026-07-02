"""SEC financials: official structured fundamentals + latest filings, keyless (UA-gated).

Where ``sec_edgar`` does full-text search across ALL filings, this source answers the
other question: "give me THIS company's current numbers and its most recent filings."
For a named company or ticker it returns one document with (a) the latest values of a
handful of key us-gaap financial concepts (revenue, net income, assets, equity, deferred
revenue) straight from the structured XBRL the SEC publishes, and (b) the most recent
filings (form + date + a direct link). This is clean, official, machine-read structure a
web search cannot return: it would hand you an article ABOUT the 10-K, not the numbers.

Three keyless SEC endpoints, probed live from the mini US egress (2026-06-17):
  - Ticker -> CIK map: ``https://www.sec.gov/files/company_tickers.json`` ->
    ``{idx: {cik_str (int), ticker, title}}``. Loaded once per process and cached. CIK is
    zero-padded to 10 digits for the data.sec.gov endpoints. (Oracle's live CIK is
    1341439; the old 777676 404s — so we always resolve through this map, never guess.)
  - Recent filings: ``https://data.sec.gov/submissions/CIK##########.json`` ->
    ``.filings.recent`` holds PARALLEL arrays (form / filingDate / accessionNumber /
    primaryDocument / primaryDocDescription / reportDate), already newest-first.
  - Structured fundamentals: ``https://data.sec.gov/api/xbrl/companyfacts/CIK#####.json``
    -> ``.facts["us-gaap"][concept].units[unit] = [{end, val, fy, fp, form, filed, ...}]``.
    The payload is large (~4MB), so we read ONLY a few named concepts and take the latest
    entry per concept (max by end-date, then by filed-date). A concept a company does not
    report is simply skipped — there is no fallback number.

SEC fair-access: every request carries a descriptive contact User-Agent (or SEC 403s),
sent via the http helper's headers= merge. A single lookup makes at most 3 requests
(map is cached after the first), so this stays well within polite use without bespoke pacing.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Optional

from penumbra.core import auth, http
from penumbra.core.normalize import Document, mk_signal

logger = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
# SEC requires a descriptive contact UA or it 403s (same contract as sec_edgar). Contact is
# host-injected, never a hardcoded personal address (see auth.contact_email).
SEC_UA = f"penumbra research {auth.contact_email()}"
SEC_HEADERS = {"User-Agent": SEC_UA, "Accept": "application/json"}
TIMEOUT = 20
FACTS_TIMEOUT = 30  # companyfacts is ~4MB

# Key us-gaap concepts, in report order. Revenue has two common spellings (the older
# ``Revenues`` and the newer ASC-606 ``RevenueFromContractWithCustomer...``); we read both
# and surface whichever is most recent under one "Revenue" line. A concept a filer does not
# report is skipped (no fabricated value).
_REVENUE_CONCEPTS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"]
_CONCEPTS = [
    ("Net income", ["NetIncomeLoss"]),
    ("Total assets", ["Assets"]),
    # ``Liabilities`` ONLY: never fall back to LiabilitiesAndStockholdersEquity — that equals
    # total assets (the balance-sheet identity), so substituting it would report a wrong,
    # mislabeled number. A filer that omits Liabilities simply has no liabilities line here.
    ("Total liabilities", ["Liabilities"]),
    ("Stockholders' equity", ["StockholdersEquity"]),
    ("Deferred revenue (contract liability)", ["ContractWithCustomerLiability", "DeferredRevenueCurrent"]),
]
_N_FILINGS = 10

_TICKER_MAP: Optional[dict[str, dict]] = None  # ticker -> {cik (10-digit str), title}
_TICKER_BY_TITLE: Optional[list[tuple[str, str, str]]] = None  # (lower_title, ticker, cik)
_MAP_LOCK = threading.Lock()

_TICKER_RE = re.compile(r"^[A-Za-z][A-Za-z.\-]{0,5}$")


def _pad_cik(cik: Any) -> str:
    """Zero-pad a CIK (int or str) to the 10 digits data.sec.gov wants."""
    return str(cik).strip().lstrip("0").zfill(10) if str(cik).strip() else ""


def _load_ticker_map() -> tuple[dict[str, dict], list[tuple[str, str, str]]]:
    """Load + cache the SEC ticker->CIK map once per process. Returns
    (by_ticker, by_title) where by_ticker maps UPPER ticker -> {cik, title} and by_title is
    a list of (lower_title, ticker, cik) for company-name matching. Empty on fetch failure."""
    global _TICKER_MAP, _TICKER_BY_TITLE
    if _TICKER_MAP is not None and _TICKER_BY_TITLE is not None:
        return _TICKER_MAP, _TICKER_BY_TITLE
    with _MAP_LOCK:
        if _TICKER_MAP is not None and _TICKER_BY_TITLE is not None:
            return _TICKER_MAP, _TICKER_BY_TITLE
        payload = http.get_json(TICKERS_URL, headers=SEC_HEADERS, timeout=TIMEOUT)
        by_ticker: dict[str, dict] = {}
        by_title: list[tuple[str, str, str]] = []
        if isinstance(payload, dict):
            for row in payload.values():
                if not isinstance(row, dict):
                    continue
                ticker = str(row.get("ticker") or "").strip().upper()
                title = str(row.get("title") or "").strip()
                cik = _pad_cik(row.get("cik_str"))
                if not ticker or not cik:
                    continue
                by_ticker[ticker] = {"cik": cik, "title": title}
                if title:
                    by_title.append((title.lower(), ticker, cik))
            logger.info("sec_financials: loaded %d tickers", len(by_ticker))
        else:
            # Do NOT pin an empty map permanently: leave the cache unset so a later call retries.
            logger.warning("sec_financials: ticker map fetch failed; will retry next call")
            return {}, []
        _TICKER_MAP, _TICKER_BY_TITLE = by_ticker, by_title
        return _TICKER_MAP, _TICKER_BY_TITLE


def _resolve(query: str) -> Optional[dict]:
    """Resolve a free-text query (a ticker or a company name) to {cik, ticker, title}.

    Order: exact ticker hit (when the query looks like a single ticker) -> exact
    case-insensitive title match -> title substring match (shortest title wins, so
    "Oracle" prefers "ORACLE CORP" over a longer subsidiary). None if nothing matches."""
    q = (query or "").strip()
    if not q:
        return None
    by_ticker, by_title = _load_ticker_map()
    if not by_ticker:
        return None

    if _TICKER_RE.match(q):
        hit = by_ticker.get(q.upper())
        if hit:
            return {"cik": hit["cik"], "ticker": q.upper(), "title": hit["title"]}

    ql = q.lower()
    exact = [(t, tk, c) for (t, tk, c) in by_title if t == ql]
    if exact:
        t, tk, c = exact[0]
        return {"cik": c, "ticker": tk, "title": t}

    subs = [(t, tk, c) for (t, tk, c) in by_title if ql in t]
    if subs:
        subs.sort(key=lambda x: len(x[0]))
        t, tk, c = subs[0]
        return {"cik": c, "ticker": tk, "title": t}
    return None


def _latest_fact(facts_gaap: dict, concepts: list[str]) -> Optional[dict]:
    """Return the most recent entry across the given concept spellings, or None if none are
    reported. 'Most recent' = max by end-date, then by filed-date. A flow concept (revenue,
    net income) reports BOTH a quarterly and a cumulative-YTD entry at the same period end;
    among those we keep the SHORTER span (the quarterly figure a reader expects), because
    silently surfacing the YTD total mislabeled as 'the quarter' is the misleading-number
    trap. The ``start`` date is carried out so the caller can show the exact period covered."""
    best: Optional[dict] = None
    for concept in concepts:
        node = facts_gaap.get(concept)
        if not isinstance(node, dict):
            continue
        units = node.get("units")
        if not isinstance(units, dict):
            continue
        for unit, arr in units.items():
            if not isinstance(arr, list):
                continue
            for e in arr:
                if not isinstance(e, dict) or e.get("val") is None or not e.get("end"):
                    continue
                cand = {"end": e.get("end"), "start": e.get("start"), "val": e.get("val"),
                        "fy": e.get("fy"), "fp": e.get("fp"), "form": e.get("form"),
                        "filed": e.get("filed"), "unit": unit, "concept": concept}
                if best is None or _fact_rank(cand) > _fact_rank(best):
                    best = cand
    return best


def _fact_rank(e: dict) -> tuple:
    """Sort key for picking the entry to surface: newest end-date wins; at the same end,
    prefer the SHORTER reporting span (quarterly over YTD); then the later filed-date (a
    restatement). A missing start sorts as a zero-length span (a point-in-time balance)."""
    start, end = e.get("start"), e.get("end") or ""
    span = 0
    if start and end:
        try:
            from datetime import date
            span = (date.fromisoformat(end) - date.fromisoformat(start)).days
        except ValueError:
            span = 0
    return (end, -span, e.get("filed") or "")


def _fmt_usd(val: Any, unit: str) -> str:
    """Render a financial magnitude compactly (245240000000 USD -> "$245.24B")."""
    try:
        v = float(val)
    except (ValueError, TypeError):
        return f"{val} {unit}"
    sign = "-" if v < 0 else ""
    a = abs(v)
    if unit == "USD":
        for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
            if a >= div:
                return f"{sign}${a / div:.2f}{suf}"
        return f"{sign}${a:,.0f}"
    return f"{val} {unit}"


class SECFinancialsAdapter:
    name = "sec_financials"
    needs_credentials = False
    kind = "lookup"
    domains = ["finance"]
    modes = ["STRUCTURE"]
    explicit_only = "company fundamentals lookup (named lookup only — ticker or company name)"
    cache_ttl = 3600
    description = (
        "SEC 结构化财务 — 公司官方基本面 + 最新备案 (keyless, data.sec.gov XBRL). "
        "query 给 ticker 或公司名 → 一条: 最新营收/净利/总资产/股东权益/递延收入 (us-gaap XBRL) "
        "+ 最近 10 条备案 (form/日期/直达链接). 命名查询, 不进广搜. web search 给不了干净的结构化数字."
    )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        ident = _resolve(query)
        if not ident:
            return []
        doc = self._build_doc(ident)
        return [doc] if doc is not None else []

    def fetch_url(self, url: str) -> Optional[Document]:
        return None

    def health_check(self) -> tuple[bool, str]:
        by_ticker, _ = _load_ticker_map()
        if not by_ticker:
            return False, "ticker map fetch failed (UA gating or endpoint change?)"
        return True, f"OK ({len(by_ticker)} tickers)"

    def _build_doc(self, ident: dict) -> Optional[Document]:
        cik = ident["cik"]
        ticker = ident.get("ticker") or ""
        title = ident.get("title") or ticker or cik

        sub = http.get_json(SUBMISSIONS_URL.format(cik=cik), headers=SEC_HEADERS, timeout=TIMEOUT)
        facts = http.get_json(COMPANYFACTS_URL.format(cik=cik), headers=SEC_HEADERS, timeout=FACTS_TIMEOUT)
        if sub is None and facts is None:
            return None  # both endpoints failed → nothing to report, do not fabricate

        company = (sub.get("name") if isinstance(sub, dict) else None) or title
        cik_num = cik.lstrip("0") or cik
        edgar_url = (
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&CIK={cik_num}&type=&dateb=&owner=include&count=40"
        )

        content_lines: list[str] = [f"Company: {company}  ·  CIK: {cik_num}"]
        if ticker:
            content_lines[0] += f"  ·  Ticker: {ticker}"
        if isinstance(sub, dict) and sub.get("sicDescription"):
            content_lines.append(f"Industry (SIC): {sub.get('sicDescription')}")

        signals: dict = {}

        # --- Key fundamentals (latest value per concept; absent concepts skipped) -----
        gaap = {}
        if isinstance(facts, dict):
            gaap = (facts.get("facts") or {}).get("us-gaap") or {}
        if isinstance(gaap, dict) and gaap:
            content_lines.append("")
            content_lines.append("Latest reported fundamentals (us-gaap XBRL):")
            rev = _latest_fact(gaap, _REVENUE_CONCEPTS)
            ordered = ([("Revenue", rev)] if rev else []) + [
                (label, _latest_fact(gaap, concepts)) for label, concepts in _CONCEPTS
            ]
            for label, fact in ordered:
                if not fact:
                    continue
                period = f"{fact.get('fp') or ''} {fact.get('fy') or ''}".strip()
                # Show the exact span for a flow figure (start..end), just the as-of date for a
                # point-in-time balance (no start) — so a YTD total is never read as a quarter.
                when = (f"{fact['start']}..{fact['end']}" if fact.get("start")
                        else f"as of {fact['end']}")
                content_lines.append(
                    f"  {label}: {_fmt_usd(fact['val'], fact['unit'])} "
                    f"({when}, {period}, {fact.get('form') or ''})".rstrip()
                )
            if rev:
                signals.update(mk_signal(
                    "latest_revenue", rev.get("val"), kind="other",
                    by="sec/companyfacts", unit=rev.get("unit"),
                ))

        # --- Most recent filings (parallel arrays, already newest-first) ---------------
        filings = self._recent_filings(sub, cik_num)
        if filings:
            content_lines.append("")
            content_lines.append(f"Most recent filings (newest first, top {len(filings)}):")
            for f in filings:
                content_lines.append(
                    f"  {f['date']}  {f['form']:<8} {f.get('desc') or ''}".rstrip()
                )
                content_lines.append(f"      {f['url']}")
        content_lines.append("")
        content_lines.append(f"EDGAR company page: {edgar_url}")

        latest_date = None
        if filings:
            from datetime import datetime
            try:
                latest_date = datetime.fromisoformat(filings[0]["date"])
            except (ValueError, KeyError):
                latest_date = None

        return Document(
            source="sec_financials",
            source_id=cik,
            url=edgar_url,
            title=f"SEC: {company} (CIK {cik_num})",
            content="\n".join(content_lines),
            author=company,
            date=latest_date,
            signals=signals,
            tags=[t for t in (ticker, "sec-financials") if t],
            metadata={
                "cik": cik_num,
                "ticker": ticker or None,
                "company": company,
                "n_filings": len(filings),
                "raw_facts_available": bool(gaap),
            },
        )

    @staticmethod
    def _recent_filings(sub: Any, cik_num: str) -> list[dict]:
        """Pull the top-N recent filings out of submissions' parallel arrays. Each ->
        {form, date, desc, url} where url is the direct filing-index page. The arrays are
        already newest-first; a missing primaryDocument falls back to the filing index."""
        if not isinstance(sub, dict):
            return []
        recent = (sub.get("filings") or {}).get("recent") or {}
        if not isinstance(recent, dict):
            return []
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        accns = recent.get("accessionNumber") or []
        primdocs = recent.get("primaryDocument") or []
        descs = recent.get("primaryDocDescription") or []
        out: list[dict] = []
        for i in range(min(_N_FILINGS, len(forms), len(dates), len(accns))):
            accn = str(accns[i] or "")
            accn_nodash = accn.replace("-", "")
            primdoc = primdocs[i] if i < len(primdocs) else ""
            if accn_nodash and primdoc:
                url = f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{accn_nodash}/{primdoc}"
            elif accn_nodash:
                url = f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{accn_nodash}/{accn}-index.htm"
            else:
                url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_num}"
            out.append({
                "form": str(forms[i] or ""),
                "date": str(dates[i] or ""),
                "desc": str(descs[i]) if i < len(descs) and descs[i] else "",
                "url": url,
            })
        return out


from penumbra.core.fetcher import register_adapter

register_adapter(SECFinancialsAdapter())
