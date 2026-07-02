"""A-share / 港股 / 美股 real-time (delayed) stock quotes (keyless, no login).

WHY (STRUCTURE): the eye's market_quote covers US stocks only; A-share / 港股 quotes were a gap
(雪球 is now login-walled). This source gives name/code → a structured quote (price / 涨跌 / 市值 /
高低 / PE) for CN + HK + US, no login — the Chinese analog of market_quote. Google can't return
clean structured quote numbers.

SHAPE: 2-step (BaseScrapeAdapter + bespoke curl_cffi). Step 1 — EastMoney suggest
(searchapi.eastmoney.com) resolves a NAME or CODE → QuoteID + Classify (the market). Step 2 — the
QUOTE comes from Tencent qt.gtimg.cn (one BATCHED `q=sym,sym,...` call, GBK, `~`-delimited fields).

WHY Tencent for the quote (2026-06-20): the original backend was EastMoney push2.eastmoney.com, but
push2 has aggressive per-IP anti-abuse — under the eye's MULTI-AGENT burst load (a research workflow
fired many parallel eastmoney calls) push2 began dropping the mini's connections (accept-then-close,
curl 56/52) for hours, while searchapi stayed fine. Tencent qt.gtimg.cn is the burst-tolerant,
keyless, industry-standard A-share/HK/US quote endpoint (verified 2026-06-20: 5 rapid hits all 200,
~45ms, no throttle), so the QUOTE moved there. Name resolution stays on EastMoney suggest (it works);
the agent-facing `url` stays an EastMoney quote page; provenance is stamped tencent/qt.gtimg.cn.

Recon trail: brain notes eye-recon-eastmoney + eye-maint-reddit-breaker-logrotate-eastmoney-route-2026-06-20.
"""

from __future__ import annotations

import logging
from typing import Optional

from penumbra.core.normalize import Document, mk_signal
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

_SUGGEST = "https://searchapi.eastmoney.com/api/suggest/get"
_SUGGEST_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"  # public web token (fixed)
_TENCENT_QUOTE = "https://qt.gtimg.cn/q="  # keyless, GBK, burst-tolerant; q=sh600519,r_hk00700,usAAPL
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

try:
    from curl_cffi import requests as _creq
    _DEPS_OK = True
except Exception as exc:  # noqa: BLE001
    logger.warning("eastmoney: curl_cffi unavailable (%s) — adapter inert", exc)
    _DEPS_OK = False


def _num(v) -> Optional[float]:
    """Tencent quote field → float (REAL values, NOT ×100 like EastMoney's f-codes); ''/'-'/None → None."""
    try:
        if v in (None, "", "-"):
            return None
        return round(float(v), 3)
    except (ValueError, TypeError):
        return None


# Market resolution keys off MktNum (the numeric market id), NOT the Classify string: Classify is
# "AStock" for the main boards but "23" for STAR/科创板 (and other values elsewhere), so a Classify
# whitelist silently DROPPED every STAR stock (688xxx → empty). MktNum is the stable key — probed
# live 2026-06-23: 0 = 深证(sz) · 1 = 上证 incl. STAR(sh) · 116 = 港股(r_hk) · 105/106/107/… = 美股(us).
def _tencent_symbol(hit: dict) -> Optional[str]:
    """EastMoney suggest hit → Tencent qt.gtimg.cn symbol (None for funds/bonds/indices)."""
    code = (hit.get("Code") or "").strip()
    if not code:
        return None
    mkt = str(hit.get("MktNum") or "")
    classify = hit.get("Classify") or ""
    if mkt == "0":
        return f"sz{code}"
    if mkt == "1":
        return f"sh{code}"
    if mkt == "116" or classify == "HK":
        return f"r_hk{code}"
    if classify == "UsStock" or mkt in ("100", "105", "106", "107", "153", "155", "156"):
        return f"us{code}"
    return None  # funds / bonds / indices / unknown markets — not quoted here


def _normalize_query(q: str) -> tuple[str, Optional[str]]:
    """Accept yfinance-style market suffixes (0700.HK / 600519.SS / 000001.SZ) that the EastMoney
    suggest API rejects outright (`0700.HK` → 0 hits). Returns (suggest_input, market_filter|None):
    HK codes are zero-padded to 5 digits (0700 → 00700), and the market_filter pins the result to
    that market so `0700.HK` cannot silently drift to the A-share 000700."""
    q = (q or "").strip()
    u = q.upper()
    if u.endswith(".HK"):
        d = u[:-3].strip()
        return (d.zfill(5) if d.isdigit() else d, "116")
    if u.endswith((".SS", ".SH")):
        return (u[:-3].strip(), "1")
    if u.endswith(".SZ"):
        return (u[:-3].strip(), "0")
    return (q, None)


def _parse_tencent_quotes(body: str) -> dict:
    """Tencent qt.gtimg.cn body (GBK-decoded) → {symbol: [~-split fields]} (pure fn → fixture-testable).
    Each line: ``v_sh600519="1~贵州茅台~600519~1215.00~1240.00~...";`` → key 'sh600519'."""
    out: dict = {}
    for line in body.split("\n"):
        line = line.strip()
        if not line.startswith("v_") or "=" not in line:
            continue
        var, _, val = line.partition("=")
        fields = val.strip().strip(";").strip('"').split("~")
        if len(fields) > 3 and fields[3]:  # has a current price
            out[var.strip()[2:]] = fields  # strip the 'v_' prefix → the tencent symbol
    return out


# Tencent `~`-field indices (probed live 2026-06-20, consistent across A-share/HK/US):
#   1 name · 2 code · 3 price · 4 prev_close · 5 open · 31 change · 32 change% · 33 high · 34 low
#   · 39 PE · 45 total_market_cap(亿). ([46] is PB for A-share but the English name for HK/US, so unused.)
def _quote_to_doc(f: list, quoteid: str, code: str) -> Optional[Document]:
    """One Tencent quote field-list → Document (pure fn → golden-fixture testable)."""
    if len(f) < 40:
        return None
    name = (f[1] or "").strip()
    price = _num(f[3])
    if not name or price is None:
        return None
    code = code or (f[2] if len(f) > 2 else "")
    chg, pct = _num(f[31]), _num(f[32])
    openp, prev, high, low = _num(f[5]), _num(f[4]), _num(f[33]), _num(f[34])
    # HK quotes carry 78 fields vs A-share's 88; PE(39) + 总市值亿(45) verified to hold for BOTH
    # (probed 2026-06-23), but read defensively so a shorter US/odd response can't IndexError.
    pe = _num(f[39]) if len(f) > 39 else None
    mktcap = _num(f[45]) if len(f) > 45 else None
    summary = (f"{name}（{code}）现价 {price} 涨跌 {chg} ({pct}%) | 今开 {openp} 最高 {high} "
               f"最低 {low} 昨收 {prev} | 总市值 {mktcap}亿 市盈率 {pe}")
    return Document(
        source="eastmoney",
        source_id=quoteid or code,
        url=_quote_url(quoteid, code),
        title=f"{name}（{code}） {price} {pct}%",
        content=summary,
        signals=mk_signal("change_pct", pct, kind="quote", by="tencent/qt.gtimg.cn"),
        metadata={"code": code, "quote_id": quoteid, "price": price, "change": chg,
                  "change_pct": pct, "open": openp, "high": high, "low": low,
                  "prev_close": prev, "market_cap_yi": mktcap, "pe": pe,
                  "provider": "tencent_qt.gtimg.cn"},
    )


def _quote_url(quoteid: str, code: str) -> str:
    """Agent-facing EastMoney quote page (nicer than Tencent's) keyed off the QuoteID market prefix."""
    m = (quoteid or "").split(".")[0]
    if m == "1":
        return f"https://quote.eastmoney.com/sh{code}.html"
    if m == "0":
        return f"https://quote.eastmoney.com/sz{code}.html"
    if m == "116":
        return f"https://quote.eastmoney.com/hk/{code}.html"
    return f"https://quote.eastmoney.com/unify/r/{quoteid}"


class EastMoneyAdapter(BaseScrapeAdapter):
    name = "eastmoney"
    description = (
        "A股 / 港股 / 美股 实时(延迟)行情 (keyless, no login). query = a stock NAME or CODE "
        "(贵州茅台 / 600519 / 0700.HK / AAPL) → structured quote: price / 涨跌 / 今开高低昨收 / 总市值 / "
        "市盈率. The Chinese analog of market_quote (US-only). Name resolution via EastMoney suggest; "
        "quote numbers via Tencent qt.gtimg.cn (burst-tolerant, after EastMoney push2 proved fragile)."
    )
    explicit_only = "A-share/HK/US quotes; name it with a stock name/code (贵州茅台 / 600519 / AAPL)"
    kind = "lookup"
    domains = ["finance"]
    regions = ["cn"]
    modes = ["STRUCTURE"]
    cache_ttl = 120  # quotes are time-sensitive
    rank = False

    def _suggest(self, sess, name: str) -> list[dict]:
        r = sess.get(_SUGGEST, params={"input": name, "type": "14", "token": _SUGGEST_TOKEN, "count": "8"},
                     headers={"user-agent": _UA, "referer": "https://www.eastmoney.com/"}, timeout=15)
        return ((r.json().get("QuotationCodeTable") or {}).get("Data")) or []

    def _raw_fetch(self, query: str, limit: int):
        if not _DEPS_OK:
            return None
        try:
            sess = _creq.Session(impersonate="chrome")
            inp, mkt = _normalize_query(query)
            hits = self._suggest(sess, inp)
            if mkt:  # a .HK/.SS/.SZ suffix pins the market (so 0700.HK can't drift to A-share 000700)
                hits = [h for h in hits if str(h.get("MktNum") or "") == mkt] or hits
            # exact-code match first: bare '00700' resolves to HK 00700 ahead of A-share 000700
            hits.sort(key=lambda h: 0 if (h.get("Code") or "").strip() == inp else 1)
            plan, seen = [], set()  # [(tencent_symbol, quoteid, code)], deduped, suggest order
            for h in hits:
                sym = _tencent_symbol(h)
                if sym and sym not in seen:
                    seen.add(sym)
                    plan.append((sym, h.get("QuoteID"), h.get("Code")))
                if len(plan) >= max(limit, 1):
                    break
            if not plan:
                return []
            # ONE batched Tencent call for all symbols (fast + gentle vs a per-symbol fan-out)
            r = sess.get(_TENCENT_QUOTE + ",".join(s for s, _, _ in plan),
                         headers={"user-agent": _UA}, timeout=15)
            quotes = _parse_tencent_quotes(r.content.decode("gbk", "replace"))
            return [(quotes[sym], qid, code) for (sym, qid, code) in plan if sym in quotes]
        except Exception as exc:  # noqa: BLE001 — failure → None → [] (adapter contract)
            logger.warning("eastmoney (tencent quote) fetch failed: %s", exc)
            return None

    def _to_documents(self, raw, query, limit) -> list[Document]:
        return [doc for (f, qid, code) in raw[:limit] if (doc := _quote_to_doc(f, qid, code))]

    def health_check(self) -> tuple[bool, str]:
        """Probe BOTH steps so a failure self-diagnoses: suggest (searchapi.eastmoney.com, name→code)
        and quote (Tencent qt.gtimg.cn) are different hosts that can fail independently."""
        if not _DEPS_OK:
            return False, "curl_cffi not installed"
        try:
            sess = _creq.Session(impersonate="chrome")
            hits = self._suggest(sess, "贵州茅台")
        except Exception as exc:  # noqa: BLE001
            return False, f"suggest host (searchapi) unreachable: {type(exc).__name__}"
        if not hits:
            return False, "suggest returned 0 (token rotated? — re-grab from eastmoney.com network tab)"
        try:
            r = sess.get(_TENCENT_QUOTE + (_tencent_symbol(hits[0]) or "sh600519"),
                         headers={"user-agent": _UA}, timeout=15)
            quotes = _parse_tencent_quotes(r.content.decode("gbk", "replace"))
        except Exception as exc:  # noqa: BLE001
            return False, f"quote host (Tencent qt.gtimg.cn) unreachable: {type(exc).__name__}"
        if not quotes:
            return False, "quote host reachable but returned no parseable quote"
        return True, "OK (suggest + Tencent quote)"
