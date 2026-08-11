"""Statistics Canada WDS (Web Data Service) — Canadian official statistics, STRUCTURED.

StatCan's Web Data Service exposes every CANSIM/table time series as clean JSON, no
auth and no key. Penumbra's Canada-specific macro-statistics STRUCTURE source: the
labour-force, price, population, and output series the open web can only narrate, handed
back as queryable numbers. Complements worldbank_stats (cross-country) with the higher
geographic / characteristic resolution of the Canadian national statistics office, which
matters for the immigration + labour-market telos (unemployment, employment, wages, CPI).

This is NOT a free-text search. It is a STRUCTURED point lookup keyed on a small curated map
of telos-relevant CANSIM vectors, or a raw vector id used verbatim:

    "unemployment"    -> Canada unemployment rate (vector 2062815)
    "employment"      -> employment + participation + labour force (shared keywords)
    "cpi" / "inflation" -> CPI all-items (vector 41690973)
    "v41690973" / "41690973" -> that vector verbatim (bare-id passthrough)

One document = one vector: the latest-N-period series, latest period as the doc date and
every {refPer: value} pair in metadata (the trend, not one number). The latest value is the
engagement-class headline signal.

Access (keyless REST, POST-only):
  POST https://www150.statcan.gc.ca/t1/wds/rest/getDataFromVectorsAndLatestNPeriods
       body = [{"vectorId": <int>, "latestN": <n>}, ...]
Response: a LIST of {"status": "SUCCESS", "object": {responseStatusCode, productId, coordinate,
  vectorId, vectorDataPoint: [{refPer, value, ...}]}}. A bad vector answers status SUCCESS with
  object.responseStatusCode == 4 and an empty vectorDataPoint (gated out to no doc).

Titles/units are NOT in the data response (getSeriesInfoFromVector returns thin labels like just
"Canada"), so they come from the curated map: a hand-written geo+characteristic label per vector,
which saves a second call and reads better. rank_locally=False: resolution IS the selection, so the
BM25 funnel must not drop the bare-vector-id case. explicit_only: a named point lookup, not broad
fan-out fodder (same posture as worldbank_stats).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from penumbra.core import http
from penumbra.core.normalize import Document, jsonsafe, mk_signal
from penumbra.core.sources.api._base import BaseAPIAdapter

REST = "https://www150.statcan.gc.ca/t1/wds/rest"
DATA_URL = f"{REST}/getDataFromVectorsAndLatestNPeriods"
# The human-facing table page (canonical, clickable): productId zero-padded to 8 + "01".
TABLE_URL = "https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid={pid}"
_LATEST_N = 120  # a wide recent window so the series shows the trend; latest period is the headline

# Curated telos-relevant CANSIM vectors, each verified live 2026-07-10. Order is the reply order
# (rank_locally=False keeps it). Each: vector id, hand-written label, unit, and the whole-word
# keywords that select it. Shared keywords (employment / labour) intentionally select several.
_CURATED: list[dict] = [
    {"vector": 2062815, "unit": "Percent", "keywords": ["unemployment", "jobless", "labour", "labor", "lfs"],
     "label": "Canada: Unemployment rate (15 years and over, seasonally adjusted)"},
    {"vector": 2062816, "unit": "Percent", "keywords": ["participation", "participation rate"],
     "label": "Canada: Labour force participation rate (15 years and over, seasonally adjusted)"},
    {"vector": 2062811, "unit": "Thousands", "keywords": ["employment", "employed", "jobs"],
     "label": "Canada: Employment (15 years and over, seasonally adjusted)"},
    {"vector": 2062810, "unit": "Thousands", "keywords": ["labour force", "labor force"],
     "label": "Canada: Labour force (15 years and over, seasonally adjusted)"},
    {"vector": 41690973, "unit": "Index (2002=100)", "keywords": ["cpi", "inflation", "consumer price", "prices"],
     "label": "Canada: Consumer Price Index (CPI), all-items (2002=100)"},
    {"vector": 1, "unit": "Persons", "keywords": ["population", "demographics", "residents"],
     "label": "Canada: Population estimate (quarterly)"},
    {"vector": 65201210, "unit": "Millions of chained 2017 dollars", "keywords": ["gdp", "output", "economy", "growth"],
     "label": "Canada: Real gross domestic product (GDP), chained 2017 dollars"},
]

_BARE_ID_RE = re.compile(r"^v?(\d+)$", re.IGNORECASE)


def _matches(keyword: str, ql: str) -> bool:
    """Whole-word (or whole-phrase) match of a keyword inside the lower-cased query."""
    return re.search(r"\b" + re.escape(keyword) + r"\b", ql) is not None


def _resolve(query: str) -> list[dict]:
    """Resolve the query to a list of curated vector entries (curated order, deduped).

    A bare vector id ("v2062815" / "2062815") is a passthrough: it returns a synthetic entry
    for that vector even when it is not in the curated map (the full StatCan catalog stays
    reachable). Otherwise every curated entry whose keywords appear as whole words in the query
    is selected. Empty / unmatched -> [] (the adapter never invents a vector)."""
    q = (query or "").strip()
    if not q:
        return []
    m = _BARE_ID_RE.match(q)
    if m:
        vid = int(m.group(1))
        curated = next((e for e in _CURATED if e["vector"] == vid), None)
        return [curated] if curated else [{"vector": vid, "unit": "", "keywords": [],
                                           "label": f"StatCan vector v{vid}"}]
    ql = q.lower()
    return [e for e in _CURATED if any(_matches(k, ql) for k in e["keywords"])]


def _parse_date(s: Any) -> Optional[datetime]:
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _table_parts(product_id: Any) -> tuple[str, str]:
    """(pid_for_url, human_table_code) from a numeric productId. 14100287 -> ('1410028701', '14-10-0287')."""
    pid8 = str(product_id or "").strip()
    if not pid8.isdigit():
        return "", ""
    pid8 = pid8.zfill(8)
    return pid8 + "01", f"{pid8[0:2]}-{pid8[2:4]}-{pid8[4:8]}"


class StatCanWdsAdapter(BaseAPIAdapter):
    name = "statcan_wds"
    needs_credentials = False
    description = (
        "Statistics Canada WDS: official Canadian statistics (labour force, CPI/inflation, "
        "population, real GDP) as a latest-N-period time series, keyless. STRUCTURED point "
        "lookup, NOT free-text search: query a keyword (unemployment / employment / participation / "
        "'labour force' / cpi / inflation / population / gdp) or a raw CANSIM vector id "
        "('v41690973'). One doc = one vector, full recent series with the latest value as the "
        "headline. Canada-specific complement to worldbank_stats. Telos: labour-market + immigration."
    )
    cache_ttl = 43200  # 12h: LFS/CPI release monthly, population/GDP quarterly
    rank_locally = False  # resolution IS the selection; the BM25 funnel must not drop the bare-id case
    kind = "lookup"
    domains = ["data"]
    regions = ["ca"]
    modes = ["STRUCTURE"]
    url_host = "statcan.gc.ca"
    explicit_only = ("statcan_wds: a structured Canadian-statistics point lookup by keyword / vector id, "
                     "named on demand, not broad-fan-out fodder")

    def _raw_fetch(self, query: str, limit: int) -> list:
        """Resolve to curated vectors, POST them all in one batch, and pair each returned object
        with its entry -> [(entry, obj), ...] (one time-series doc per pair). Any failure -> []."""
        entries = _resolve(query)
        if not entries:
            return []
        body = [{"vectorId": int(e["vector"]), "latestN": _LATEST_N} for e in entries]
        raw = http.post_json(DATA_URL, json=body, timeout=20)
        if not isinstance(raw, list):
            return []
        objs: dict[int, dict] = {}
        for item in raw:
            if isinstance(item, dict) and item.get("status") == "SUCCESS" and isinstance(item.get("object"), dict):
                obj = item["object"]
                vid = obj.get("vectorId")
                if isinstance(vid, int):
                    objs[vid] = obj
        pairs = [(e, objs.get(int(e["vector"]))) for e in entries]
        return [(e, o) for e, o in pairs if o is not None]

    async def _araw_fetch(self, query: str, limit: int) -> list:
        """Async twin of _raw_fetch: byte-faithful mirror (same URL / body / timeout, same SUCCESS
        gate + pair-up control flow, same None/[] contract), only the shared-http egress swapped for
        its async twin (http.post_json -> await http.apost_json)."""
        entries = _resolve(query)
        if not entries:
            return []
        body = [{"vectorId": int(e["vector"]), "latestN": _LATEST_N} for e in entries]
        raw = await http.apost_json(DATA_URL, json=body, timeout=20)
        if not isinstance(raw, list):
            return []
        objs: dict[int, dict] = {}
        for item in raw:
            if isinstance(item, dict) and item.get("status") == "SUCCESS" and isinstance(item.get("object"), dict):
                obj = item["object"]
                vid = obj.get("vectorId")
                if isinstance(vid, int):
                    objs[vid] = obj
        pairs = [(e, objs.get(int(e["vector"]))) for e in entries]
        return [(e, o) for e, o in pairs if o is not None]

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of search -> AsyncSearchCapable. Shares the base async cache round-trip
        (_aapi_search); egress via _araw_fetch; per-record mapping via the SAME pure-CPU _to_document."""
        return await self._aapi_search(query, limit, araw_fetch=lambda: self._araw_fetch(query, limit))

    def _to_document(self, raw) -> Optional[Document]:
        if not isinstance(raw, tuple) or len(raw) != 2:
            return None
        entry, obj = raw
        return self._series_to_doc(entry, obj)

    @classmethod
    def _series_to_doc(cls, entry: dict, obj: dict) -> Optional[Document]:
        """One curated entry + its WDS object -> a time-series doc. Gated on responseStatusCode
        in (0, None): a bad vector (code 4, empty points) returns None."""
        if not isinstance(obj, dict):
            return None
        if obj.get("responseStatusCode") not in (0, None):
            return None
        points = obj.get("vectorDataPoint") or []
        series: dict[str, float] = {}
        latest_release = ""
        for p in points:
            if not isinstance(p, dict):
                continue
            ref = p.get("refPer")
            val = cls._as_float(p.get("value"))
            if ref and val is not None:
                series[str(ref)] = val
                if not latest_release and p.get("releaseTime"):
                    latest_release = p["releaseTime"]
        if not series:
            return None

        periods_desc = sorted(series, reverse=True)
        latest_period = periods_desc[0]
        latest_value = series[latest_period]
        vid = int(entry["vector"])
        unit = entry.get("unit") or ""
        pid_url, table_code = _table_parts(obj.get("productId"))
        url = TABLE_URL.format(pid=pid_url) if pid_url else "https://www150.statcan.gc.ca/t1/wds/rest"

        lines = [entry.get("label") or f"StatCan vector v{vid}",
                 f"Vector: v{vid}  ·  Table: {table_code or '?'}",
                 ""]
        for period in periods_desc:
            lines.append(f"{period}: {cls._fmt_num(series[period])}" + (f" {unit}" if unit else ""))

        return Document(
            source="statcan_wds",
            source_id=f"v{vid}",
            url=url,
            title=entry.get("label") or f"StatCan vector v{vid}",
            content="\n".join(lines),
            date=_parse_date(latest_period),
            signals=mk_signal("latest_value", latest_value, kind="engagement",
                              by="statcan_wds/value", unit=unit or None),
            tags=["canada", "statistics"],
            metadata={
                "vector_id": vid,
                "product_id": obj.get("productId"),
                "table_code": table_code,
                "coordinate": obj.get("coordinate"),
                "unit": unit,
                "latest_release": latest_release,
                "latest_period": latest_period,
                "latest_value": latest_value,
                "series": {p: series[p] for p in periods_desc},
                "raw": jsonsafe(obj),
            },
        )

    def health_check(self) -> tuple[bool, str]:
        """POST-only endpoint: probe vector 1 (population) for the latest period; SUCCESS = alive."""
        try:
            raw = http.post_json(DATA_URL, json=[{"vectorId": 1, "latestN": 1}], timeout=10)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"
        if isinstance(raw, list) and raw and isinstance(raw[0], dict) and raw[0].get("status") == "SUCCESS":
            return True, "WDS status SUCCESS"
        return False, "WDS unreachable / non-SUCCESS"

    @staticmethod
    def _as_float(v: Any) -> Optional[float]:
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        try:
            return float(str(v).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _fmt_num(v: float) -> str:
        if v == int(v):
            return f"{int(v):,}"
        return f"{v:,.4g}" if abs(v) < 1 else f"{v:,.2f}"

# Registration is automatic via BaseAPIAdapter.__init_subclass__ (no module-tail ceremony).
