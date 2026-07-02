"""Market quote: delayed/real-time US-equity quotes, keyless (browser-UA gated).

For a query that names stock tickers ("$NVDA $AAPL", "ORCL TSLA"), return a clean
per-ticker quote the open web cannot hand back structured on demand: last price,
change + percent, day range, 52-week range, volume, market cap, P/E, EPS, dividend,
and the pre/post-market quote when the regular session is closed. This is the
STRUCTURE the open web only shows you inside a chart widget, never as data.

Source: CNBC's keyless quote backend (the JSON the cnbc.com quote pages call):

    GET https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol
        ?symbols=ORCL|NVDA&requestMethod=itv&fund=1&exthrs=1&output=json

Probed live from the mini US egress (2026-06-17). Hard facts that shaped this code:
  - Multi-symbol uses a PIPE separator (``ORCL|NVDA``). A comma (``ORCL,NVDA``) is
    NOT split: the backend treats it as one bogus symbol and returns a ``code:1``
    stub. So we join requested tickers with ``|``.
  - Response: ``{"FormattedQuoteResult": {"FormattedQuote": [<quote>, ...]}}``. With a
    single requested symbol the inner value can come back as one dict instead of a
    list, so we coerce to a list.
  - A good quote has ``code == 0`` and a populated ``last``; an unknown ticker comes
    back as ``{"symbol": "...", "code": 1}`` with ``last`` null. We drop those (no
    fabrication: an unknown ticker simply yields no document).
  - Yahoo's finance endpoints 429 from this same egress, so CNBC is the path.

This source is ``explicit_only`` (a named lookup, like gpu_pricing): it is reached
when an agent routes a quote query to it, not on every broad search. A query with no
ticker returns nothing (we do not guess a ticker from a company name in v1).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

from penumbra.core import http
from penumbra.core.normalize import Document, jsonsafe, mk_signal

logger = logging.getLogger(__name__)

API_URL = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
TIMEOUT = 15
# CNBC serves this backend to browsers; the shared PenumbraEye UA gets thinner data,
# so we send a browser UA via the http helper's headers= merge (same pattern sec_edgar
# uses to send its contact UA).
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)

# A cashtag (``$NVDA``) or a bare ALL-CAPS ticker. Tickers are 1 to 5 chars, letters
# with an optional dot class (BRK.B). The cashtag form is unambiguous; the bare-uppercase
# form is how people write tickers in a plain query ("ORCL NVDA earnings").
_CASHTAG = re.compile(r"\$([A-Za-z][A-Za-z.]{0,5})\b")
_BARE_TICKER = re.compile(r"\b([A-Z][A-Z.]{0,4})\b")
# Bare-uppercase tokens that are common English words / query noise, NOT tickers. Without
# this guard "PE", "EPS", "USD", "AI" etc. would be probed as symbols and waste a request.
_NOT_TICKERS = frozenset({
    "A", "I", "AI", "AN", "AS", "AT", "BE", "BY", "DO", "GO", "IF", "IN", "IS", "IT",
    "ME", "MY", "NO", "OF", "ON", "OR", "SO", "TO", "UP", "US", "WE",
    "ALL", "AND", "ANY", "ARE", "BUT", "CAN", "CEO", "CFO", "EPS", "ESG", "ETF", "FED",
    "FOR", "GDP", "HOW", "IPO", "NOT", "NOW", "OUT", "PER", "ROE", "SEC", "THE", "USD",
    "WHO", "WHY", "YOY", "QOQ",
})


def _extract_tickers(query: str) -> list[str]:
    """Pull stock tickers out of a free-text query: cashtags (``$NVDA``) always count;
    bare ALL-CAPS tokens count unless they are a common-word false positive. Upper-cased,
    de-duplicated, order preserved. A query with no tickers returns []."""
    if not query:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _CASHTAG.finditer(query):
        t = m.group(1).upper()
        if t not in seen:
            seen.add(t)
            out.append(t)
    for m in _BARE_TICKER.finditer(query):
        t = m.group(1).upper()
        if t in seen or t in _NOT_TICKERS:
            continue
        seen.add(t)
        out.append(t)
    return out


def _quotes(payload) -> list[dict]:
    """Pull the quote list out of the CNBC envelope, coercing the single-symbol dict form
    to a list. Returns [] for any unexpected shape (no raise)."""
    if not isinstance(payload, dict):
        return []
    fqr = payload.get("FormattedQuoteResult")
    if not isinstance(fqr, dict):
        return []
    fq = fqr.get("FormattedQuote")
    if isinstance(fq, dict):
        return [fq]
    if isinstance(fq, list):
        return [q for q in fq if isinstance(q, dict)]
    return []


def _num(s) -> Optional[float]:
    """Parse a CNBC string number ("187.30", "3,789,726", "-0.55%") to float, or None."""
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").replace("%", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


def _dt(s) -> Optional[datetime]:
    """Parse CNBC's ISO-8601 as-of timestamp (last_time, e.g. "2026-06-17T14:57:46.975-0400")
    to an aware datetime, or None. The prose uses last_timedate ("2:57 PM EDT") which is dateless;
    last_time is the machine field with a full date + offset."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except (ValueError, TypeError):
        return None


class MarketQuoteAdapter:
    name = "market_quote"
    needs_credentials = False
    kind = "lookup"
    # Free-vocab domain facet (read off this class attr by the router; facets.json untouched).
    domains = ["finance"]
    modes = ["STRUCTURE"]
    explicit_only = "stock quote lookup (named lookup only — query must name tickers)"
    # Quotes move intraday; a short TTL keeps a repeated lookup cheap without going stale.
    cache_ttl = 300
    description = (
        "美股行情 — 实时(延迟)股票报价 (keyless, CNBC quote 后端). query 里点名 ticker "
        "($NVDA / ORCL) → 每个 ticker 一条: 现价/涨跌/涨跌幅/成交量/市值/PE/EPS/股息/日内区间/"
        "52周区间 + 盘前盘后. 多 symbol 一次. 命名查询, 不进广搜; query 无 ticker 返空."
    )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        tickers = _extract_tickers(query)
        if not tickers:
            return []
        tickers = tickers[:limit]
        payload = http.get_json(
            API_URL,
            params={
                "symbols": "|".join(tickers),
                "requestMethod": "itv",
                "fund": "1",
                "exthrs": "1",
                "output": "json",
            },
            headers={"User-Agent": BROWSER_UA},
            timeout=TIMEOUT,
        )
        docs: list[Document] = []
        for q in _quotes(payload):
            try:
                doc = self._to_doc(q)
            except Exception as exc:  # noqa: BLE001 — one bad quote can't sink the rest
                logger.debug("market_quote: skipping malformed quote: %s", exc)
                continue
            if doc is not None:
                docs.append(doc)
        return docs

    def fetch_url(self, url: str) -> Optional[Document]:
        return None

    def health_check(self) -> tuple[bool, str]:
        payload = http.get_json(
            API_URL,
            params={"symbols": "AAPL", "requestMethod": "itv", "output": "json"},
            headers={"User-Agent": BROWSER_UA},
            timeout=10,
        )
        quotes = _quotes(payload)
        if quotes and quotes[0].get("code") == 0:
            return True, "OK"
        return False, "no good quote (UA gating or endpoint change?)"

    @staticmethod
    def _to_doc(q: dict) -> Optional[Document]:
        symbol = (q.get("symbol") or "").strip().upper()
        # code != 0 (or no last) is CNBC's "unknown / no quote" stub: drop it, do not invent.
        if not symbol or q.get("code") != 0 or not q.get("last"):
            return None

        name = (q.get("name") or q.get("altName") or symbol).strip()
        last = q.get("last")
        change = q.get("change")
        change_pct = q.get("change_pct")

        title = f"{symbol} — {name} ${last} ({change_pct or change or 'n/a'})"

        lines = [
            f"{name} ({symbol}) on {q.get('exchange', '?')} — {q.get('currencyCode', 'USD')}",
            f"Last: {last}  ·  Change: {change} ({change_pct})  ·  as of {q.get('last_timedate', '?')}",
            f"Open: {q.get('open', '?')}  ·  Day range: {q.get('low', '?')} – {q.get('high', '?')}",
            f"Prev close: {q.get('previous_day_closing', '?')}  ·  Volume: {q.get('volume', '?')}",
            f"52-week range: {q.get('yrloprice', '?')} ({q.get('yrlodate', '?')}) – "
            f"{q.get('yrhiprice', '?')} ({q.get('yrhidate', '?')})",
            f"Market cap: {q.get('mktcapView', '?')}  ·  P/E: {q.get('pe', '?')}  ·  EPS: {q.get('eps', '?')}",
            f"Dividend: {q.get('dividend', '?')} (yield {q.get('dividendyield', '?')})  ·  Beta: {q.get('beta', '?')}",
        ]
        # Pre/post-market quote, present only when the regular session is closed.
        ext = q.get("ExtendedMktQuote")
        if isinstance(ext, dict) and ext.get("last"):
            lines.append(
                f"{ext.get('type', 'EXT')}: {ext.get('last')} "
                f"({ext.get('change_pct', ext.get('change', '?'))}) as of {ext.get('last_timedate', '?')}"
            )

        # Source-reported facts (not judgments, not engagement): record price + percent move
        # as 'other'-kind signals for transparency, same spirit as sec_edgar's relevance_score.
        signals = {}
        signals.update(mk_signal("last_price", _num(last), kind="other", by="cnbc/last", unit="USD"))
        signals.update(mk_signal("change_pct", _num(change_pct), kind="other", by="cnbc/change_pct", unit="%"))

        return Document(
            source="market_quote",
            source_id=symbol,
            url=f"https://www.cnbc.com/quotes/{symbol}",
            title=title,
            content="\n".join(lines),
            author=name,
            date=_dt(q.get("last_time")),
            signals=signals,
            tags=[symbol, "stock-quote"],
            metadata={
                "symbol": symbol,
                "name": name,
                "exchange": q.get("exchange"),
                "last": last,
                "change": change,
                "change_pct": change_pct,
                "volume": q.get("volume"),
                "market_cap": q.get("mktcapView"),
                "pe": q.get("pe"),
                "raw": jsonsafe(q),
            },
        )


from penumbra.core.fetcher import register_adapter

register_adapter(MarketQuoteAdapter())
