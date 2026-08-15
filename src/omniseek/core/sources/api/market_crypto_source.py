"""Crypto spot quotes — keyless CoinGecko (the BTC/ETH analog of market_quote / eastmoney).

For a query that names coins ("BTC ETH", "$SOL", "bitcoin"), return a clean per-coin quote the
open web can't hand back structured on demand: USD price, 24h change %, market cap, 24h volume.
STRUCTURE mode. Keyless: CoinGecko's public /simple/price (no API key, browser-UA gated).

Resolution (no fabrication): a static SYMBOL→id map covers the majors with ZERO extra calls; a
lowercase / hyphenated token is passed through as a literal CoinGecko id (bitcoin / avalanche-2);
an UNKNOWN uppercase symbol is simply dropped (we never guess a coin id from an unknown ticker).
A query that names no coin returns []. CoinGecko's /simple/price silently omits unknown ids, so a
bad id yields no document rather than an error.

explicit_only (a named lookup, like market_quote): reached when an agent routes a crypto quote to
it, not on every broad search. Recon trail: brain note eye-recon-market-crypto.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from omniseek.core import http
from omniseek.core.normalize import Document, mk_signal

logger = logging.getLogger(__name__)

API_URL = "https://api.coingecko.com/api/v3/simple/price"
TIMEOUT = 15
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)

# Static SYMBOL → CoinGecko id for the majors (covers a watchlist with no resolver round-trip).
# A coin outside this map is still reachable by passing its CoinGecko id directly (e.g. "solana").
_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "USDT": "tether", "BNB": "binancecoin",
    "SOL": "solana", "XRP": "ripple", "USDC": "usd-coin", "ADA": "cardano",
    "DOGE": "dogecoin", "TRX": "tron", "TON": "the-open-network", "AVAX": "avalanche-2",
    "SHIB": "shiba-inu", "LINK": "chainlink", "DOT": "polkadot", "BCH": "bitcoin-cash",
    "NEAR": "near", "MATIC": "matic-network", "LTC": "litecoin", "UNI": "uniswap",
    "ICP": "internet-computer", "APT": "aptos", "XLM": "stellar", "ATOM": "cosmos",
    "ETC": "ethereum-classic", "FIL": "filecoin", "ARB": "arbitrum", "OP": "optimism",
    "SUI": "sui", "HBAR": "hedera-hashgraph", "INJ": "injective-protocol", "RNDR": "render-token",
}
_SYM_BY_ID = {v: k for k, v in _IDS.items()}


def _resolve(query: str) -> list[tuple[str, str]]:
    """query → [(coingecko_id, display_symbol)], de-duped, order preserved.

    Resolves ONLY recognizable coin forms (never guesses a coin from a bare English word):
      - a known uppercase SYMBOL (BTC) → its id;
      - a hyphenated or known-id token (avalanche-2 / bitcoin / solana) → that id;
      - a $-cashtag ($pepe) → a literal id (an explicit 'this is a coin' signal).
    Anything else (a bare unknown word, or an unknown uppercase ticker) is dropped."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for tok in re.findall(r"(\$?[A-Za-z][A-Za-z0-9-]{1,19})", query or ""):
        cash = tok.startswith("$")
        raw = tok[1:] if cash else tok
        u = raw.upper()
        if u in _IDS:
            cid, sym = _IDS[u], u
        elif "-" in raw or raw.lower() in _SYM_BY_ID:
            cid, sym = raw.lower(), _SYM_BY_ID.get(raw.lower(), u)
        elif cash:
            cid, sym = raw.lower(), u
        else:
            continue
        if cid not in seen:
            seen.add(cid)
            out.append((cid, sym))
    return out


def _to_doc(cid: str, sym: str, data: dict) -> Optional[Document]:
    """One CoinGecko /simple/price entry → Document (no price → None, no fabrication)."""
    if not isinstance(data, dict):
        return None
    price = data.get("usd")
    if price is None:
        return None
    chg = data.get("usd_24h_change")
    cap = data.get("usd_market_cap")
    vol = data.get("usd_24h_vol")
    chg_str = f"{chg:+.2f}%" if isinstance(chg, (int, float)) else "n/a"
    lines = [
        f"{sym}/USD spot — CoinGecko ({cid})",
        f"Price: ${price:,}  ·  24h change: {chg_str}",
        f"Market cap: ${cap:,.0f}" if isinstance(cap, (int, float)) else "Market cap: n/a",
        f"24h volume: ${vol:,.0f}" if isinstance(vol, (int, float)) else "24h volume: n/a",
    ]
    signals = {}
    signals.update(mk_signal("last_price", float(price), kind="quote", by="coingecko/usd", unit="USD"))
    if isinstance(chg, (int, float)):
        signals.update(mk_signal("change_pct", round(float(chg), 3), kind="quote",
                                 by="coingecko/usd_24h_change", unit="%"))
    return Document(
        source="market_crypto",
        source_id=cid,
        url=f"https://www.coingecko.com/en/coins/{cid}",
        title=f"{sym}/USD ${price:,} ({chg_str} 24h)",
        content="\n".join(lines),
        signals=signals,
        tags=[sym, "crypto-quote"],
        metadata={"coin_id": cid, "symbol": sym, "price_usd": price,
                  "change_pct_24h": chg, "market_cap_usd": cap, "vol_24h_usd": vol,
                  "provider": "coingecko"},
    )


class MarketCryptoAdapter:
    name = "market_crypto"
    needs_credentials = False
    kind = "lookup"
    domains = ["finance"]
    modes = ["STRUCTURE"]
    explicit_only = "crypto spot quote lookup (named lookup only — query must name a coin: BTC / ETH / solana)"
    cache_ttl = 120  # crypto moves fast; short TTL keeps a repeat lookup cheap without staleness
    description = (
        "加密现货行情 — BTC/ETH 等币种的实时(秒级)报价 (keyless, CoinGecko 后端). query 点名币种 "
        "(BTC / ETH / $SOL / bitcoin) → 每币一条: 美元现价 / 24h 涨跌% / 市值 / 24h 成交额. 多币一次. "
        "命名查询, 不进广搜; 主流币静态映射、未知大写符号不臆测 → query 无币种或无法解析返空."
    )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        plan = _resolve(query)[:max(limit, 1)]
        if not plan:
            return []
        payload = http.get_json(
            API_URL,
            params={
                "ids": ",".join(cid for cid, _ in plan),
                "vs_currencies": "usd",
                "include_market_cap": "true",
                "include_24hr_vol": "true",
                "include_24hr_change": "true",
            },
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
            timeout=TIMEOUT,
        )
        if not isinstance(payload, dict):
            return []
        docs: list[Document] = []
        for cid, sym in plan:
            doc = _to_doc(cid, sym, payload.get(cid) or {})
            if doc is not None:
                docs.append(doc)
        return docs[:limit]

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` (S4b): mirrors it line-for-line so the fan-out awaits this
        DIRECTLY instead of pushing the sync ``.search`` onto the shared pool. Only ONE line differs —
        the network egress swaps ``http.get_json`` -> ``await http.aget_json`` (epoll on the loop, no
        held thread). No disk-cache round-trip exists in ``search`` (this source caches at the fan-out
        layer, not internally), so there is nothing to hop OFF the loop; ``_resolve`` + ``_to_doc`` are
        PURE CPU and stay on the loop UNCHANGED, byte-identical to ``search``, so the two can never drift."""
        plan = _resolve(query)[:max(limit, 1)]
        if not plan:
            return []
        payload = await http.aget_json(
            API_URL,
            params={
                "ids": ",".join(cid for cid, _ in plan),
                "vs_currencies": "usd",
                "include_market_cap": "true",
                "include_24hr_vol": "true",
                "include_24hr_change": "true",
            },
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
            timeout=TIMEOUT,
        )
        if not isinstance(payload, dict):
            return []
        docs: list[Document] = []
        for cid, sym in plan:
            doc = _to_doc(cid, sym, payload.get(cid) or {})
            if doc is not None:
                docs.append(doc)
        return docs[:limit]

    def fetch_url(self, url: str) -> Optional[Document]:
        return None

    def health_check(self) -> tuple[bool, str]:
        payload = http.get_json(
            API_URL,
            params={"ids": "bitcoin", "vs_currencies": "usd"},
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
            timeout=10,
        )
        if isinstance(payload, dict) and isinstance(payload.get("bitcoin"), dict) \
                and payload["bitcoin"].get("usd"):
            return True, "OK (CoinGecko /simple/price)"
        return False, "no good quote (CoinGecko rate-limit or endpoint change?)"


from omniseek.core.fetcher import register_adapter

register_adapter(MarketCryptoAdapter())
