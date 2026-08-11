"""Eurostat dissemination API: EU official statistics, STRUCTURED.

Eurostat (the EU's statistical office) exposes every dataset as a JSON-stat 2.0 cube,
no auth and no key. Penumbra's EU-specific macro-statistics STRUCTURE source: the
labour-market, price, output, population, and MIGRATION series the open web can only
narrate, handed back as queryable numbers. Complements worldbank_stats (cross-country,
one number per year) and statcan_wds (Canada) with the EU's harmonised, comparable
series at member-state or EU/euro-area resolution, which matters directly for the
immigration + labour-market telos (unemployment, employment, HICP, residence permits,
citizenship acquisitions, asylum applications).

This is NOT a free-text search. It is a STRUCTURED point lookup keyed on a small curated
map of telos-relevant Eurostat datasets, each with its non-geo dimensions PINNED so the
cube collapses to a single time series. The query convention mirrors worldbank_stats:

    "<GEO> <INDICATOR>"

where GEO is a Eurostat geo code (a 2-letter member state DE / FR / EL, or an aggregate
EU27_2020 / EA20) and INDICATOR is a keyword mapped to one curated dataset. GEO is
OPTIONAL: an omitted geo defaults to EU27_2020 (the EU-27 aggregate). Examples:

    "DE unemployment"        -> Germany, harmonised unemployment rate (une_rt_a)
    "FR inflation"           -> France, HICP annual average rate of change (prc_hicp_aind)
    "asylum applications"    -> EU27_2020, monthly asylum applicants (migr_asyappctzm)

KEY DECODE (why pinning matters). JSON-stat 2.0 returns a flat `value` map keyed by the
LINEARIZED cell index over the `id` dimensions (sizes in `size`), plus a per-dimension
`category.index` (code -> position). By pinning EVERY non-time dimension (geo / sex / age /
unit / na_item / ...) to one code, `size` collapses to [1, 1, ..., N_time]; since every
non-time dimension is size 1 and `time` is last, the linear index EQUALS the time position.
So the decode is: invert `dimension.time.category.index` (period -> pos) and read
`value[pos]` for each period. A wrong or absent pin leaves a dimension with size > 1
(a multi-series cube) whose flat index no longer maps to time alone; that is gated out to
no doc (the "empty value / wrong code" guard in _series_to_doc), never mis-decoded.

Access (keyless REST, GET):
  GET https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/<dataset>
      ?format=JSON&geo=<code>&<pinned dims>
Response: a JSON-stat 2.0 object {version, class, value, id, size, dimension, ...}. A cell
with a status flag (":" not available) is simply ABSENT from the sparse `value` map, so a
missing period is skipped, never read as a zero.

One document = one dataset x one geo: the full time series, latest period as the doc date
and the latest value as the engagement-class headline signal. rank_locally=False: resolution
IS the selection (at most one doc per call), so the BM25 funnel is a no-op that must not
reorder. explicit_only: a named point lookup, not broad fan-out fodder (same posture as
worldbank_stats / statcan_wds).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from penumbra.core import http
from penumbra.core.normalize import Document, jsonsafe, mk_signal
from penumbra.core.sources.api._base import BaseAPIAdapter

DATA_URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{dataset}"
# The human-facing Eurostat databrowser page (canonical, clickable) for a dataset.
PAGE_URL = "https://ec.europa.eu/eurostat/databrowser/view/{dataset}/default/table?lang=en"
_DEFAULT_GEO = "EU27_2020"  # EU-27 aggregate: the sensible default when the query omits a geo

# Curated telos-relevant Eurostat datasets, each verified live 2026-07-14. Order is the
# reply order (rank_locally=False keeps it), but selection is longest-keyword-first (see
# _resolve_indicator) so a specific phrase wins over a generic one. Each entry:
#   dataset  the Eurostat dataset code
#   filters  the non-geo / non-time dimension PINS that collapse the cube to one series
#            (every one live-verified to yield size == 1 on that dimension)
#   unit     a hand-written human unit label (JSON-stat unit labels are terse codes)
#   label    the human indicator name (geo is prepended at doc-build time)
#   keywords whole-word / whole-phrase selectors matched against the indicator part
_CURATED: list[dict] = [
    {"dataset": "une_rt_a", "filters": {"sex": "T", "age": "Y15-74", "unit": "PC_ACT"},
     "unit": "% of active population", "keywords": ["unemployment", "jobless", "unemployment rate"],
     "label": "Harmonised unemployment rate, annual (age 15-74)"},
    {"dataset": "une_rt_m", "filters": {"s_adj": "SA", "age": "TOTAL", "sex": "T", "unit": "PC_ACT"},
     "unit": "% of active population, seasonally adjusted",
     "keywords": ["monthly unemployment", "unemployment monthly"],
     "label": "Harmonised unemployment rate, monthly (seasonally adjusted)"},
    {"dataset": "lfsi_emp_a", "filters": {"indic_em": "EMP_LFS", "sex": "T", "age": "Y20-64", "unit": "PC_POP"},
     "unit": "% of population aged 20-64", "keywords": ["employment", "employed", "employment rate", "jobs"],
     "label": "Employment rate, annual (age 20-64)"},
    {"dataset": "nama_10_gdp", "filters": {"na_item": "B1GQ", "unit": "CLV20_MEUR"},
     "unit": "chain linked volumes (2020), million EUR",
     "keywords": ["gdp", "real gdp", "output", "economy"],
     "label": "Real gross domestic product (GDP)"},
    {"dataset": "nama_10_pc", "filters": {"na_item": "B1GQ", "unit": "CP_EUR_HAB"},
     "unit": "EUR per capita (current prices)",
     "keywords": ["gdp per capita", "gdp per head", "income per capita"],
     "label": "GDP per capita (current prices)"},
    {"dataset": "prc_hicp_aind", "filters": {"unit": "RCH_A_AVG", "coicop": "CP00"},
     "unit": "annual average rate of change, %",
     "keywords": ["inflation", "hicp", "consumer price", "prices"],
     "label": "HICP inflation, all-items (annual average rate of change)"},
    {"dataset": "demo_pjan", "filters": {"sex": "T", "age": "TOTAL"},
     "unit": "persons", "keywords": ["population", "demographics", "residents", "inhabitants"],
     "label": "Population on 1 January"},
    {"dataset": "migr_resfirst", "filters": {"reason": "TOTAL", "citizen": "TOTAL", "duration": "TOTAL"},
     "unit": "persons", "keywords": ["residence permits", "first residence permits", "permits"],
     "label": "First residence permits issued (all reasons)"},
    {"dataset": "migr_acq", "filters": {"citizen": "TOTAL", "agedef": "COMPLET", "age": "TOTAL", "sex": "T"},
     "unit": "persons",
     "keywords": ["citizenship", "naturalisation", "naturalization", "citizenship acquisition"],
     "label": "Acquisitions of citizenship"},
    {"dataset": "migr_asyappctzm", "filters": {"citizen": "TOTAL", "sex": "T", "applicant": "TOTAL", "age": "TOTAL"},
     "unit": "persons", "keywords": ["asylum", "asylum applications", "asylum applicants", "asylum seekers"],
     "label": "Asylum applicants, monthly (all applicant types)"},
]

# (keyword, entry) pairs sorted longest-keyword-first, so a specific phrase ("gdp per capita")
# is tested before a generic one ("gdp") and wins. Built once at import.
_KEYWORD_INDEX: list[tuple[str, dict]] = sorted(
    ((kw, e) for e in _CURATED for kw in e["keywords"]),
    key=lambda pair: len(pair[0]), reverse=True,
)

# A Eurostat geo code: a 2-letter member state (DE/FR/EL/...), a euro-area/EU aggregate
# (EA20/EU27_2020/EU28), or EEA/EFTA. Used to decide whether the first token is a geo.
_GEO_RE = re.compile(r"^([A-Z]{2}|E[AU]\d+(?:_\d+)?|EEA|EFTA)$")


def _matches(keyword: str, ql: str) -> bool:
    """Whole-word (or whole-phrase) match of a keyword inside the lower-cased indicator part."""
    return re.search(r"\b" + re.escape(keyword) + r"\b", ql) is not None


def _resolve_indicator(indicator_part: str) -> Optional[dict]:
    """Resolve the indicator part to ONE curated entry, longest matching keyword first.

    Returns the entry whose longest matching keyword is the most specific hit, so
    "gdp per capita" selects nama_10_pc while "gdp" selects nama_10_gdp. No keyword
    match -> None (the adapter never invents a dataset)."""
    ql = (indicator_part or "").strip().lower()
    if not ql:
        return None
    for kw, entry in _KEYWORD_INDEX:
        if _matches(kw, ql):
            return entry
    return None


def _parse_query(query: str) -> Optional[tuple[str, dict]]:
    """Parse "<GEO> <INDICATOR>" into (geo_code, curated_entry).

    The first whitespace token is treated as a geo code when it LOOKS like one
    (_GEO_RE, case-insensitive); otherwise geo defaults to EU27_2020 and the WHOLE
    query is the indicator part. Returns None when the query is empty or the indicator
    resolves to nothing (so the adapter never fabricates a geo/dataset)."""
    q = (query or "").strip()
    if not q:
        return None
    parts = q.split(None, 1)
    first = parts[0].upper()
    if _GEO_RE.match(first):
        geo = first
        indicator_part = parts[1].strip() if len(parts) > 1 else ""
    else:
        geo = _DEFAULT_GEO
        indicator_part = q
    entry = _resolve_indicator(indicator_part)
    if entry is None:
        return None
    return geo, entry


class EurostatStatsAdapter(BaseAPIAdapter):
    name = "eurostat_stats"
    needs_credentials = False
    description = (
        "Eurostat dissemination API: EU official statistics (harmonised unemployment, "
        "employment, real GDP, GDP per capita, HICP inflation, population, and the "
        "migration set: first residence permits, citizenship acquisitions, asylum "
        "applicants) as a JSON-stat time series, keyless. STRUCTURED point lookup, NOT "
        "free-text search: query is '<GEO> <INDICATOR>' where GEO is a Eurostat geo code "
        "(DE / FR / EU27_2020 / EA20; omitted -> EU27_2020) and INDICATOR is a keyword "
        "(unemployment / employment / gdp / gdp per capita / inflation / population / "
        "'residence permits' / citizenship / asylum). One doc = one dataset x one geo, "
        "full series. EU complement to worldbank_stats / statcan_wds. Telos: "
        "labour-market + immigration."
    )
    cache_ttl = 43200  # 12h: Eurostat releases are monthly (LFS/HICP) to annual
    rank_locally = False  # resolution IS the selection; at most one doc, do not reorder
    kind = "lookup"
    domains = ["data"]
    regions = ["eu"]
    modes = ["STRUCTURE"]
    url_host = "ec.europa.eu"
    explicit_only = ("eurostat_stats: a structured EU-statistics point lookup by geo + indicator "
                     "keyword, named on demand, not broad-fan-out fodder")

    def _raw_fetch(self, query: str, limit: int) -> list:
        """Resolve (geo, entry), GET the dataset with format=JSON + the entry's dimension
        pins + geo, and return it as a SINGLE aggregation unit [(entry, geo, jsonstat)] so
        the base's per-record loop yields exactly one time-series doc. Any failure (bad
        query, network, non-dict body) -> [] (the adapter contract)."""
        parsed = _parse_query(query)
        if parsed is None:
            return []
        geo, entry = parsed
        params = {"format": "JSON", "geo": geo, **entry["filters"]}
        raw = http.get_json(DATA_URL.format(dataset=entry["dataset"]), params=params, timeout=20)
        if not isinstance(raw, dict):
            return []
        return [(entry, geo, raw)]

    async def _araw_fetch(self, query: str, limit: int) -> list:
        """Async twin of _raw_fetch: byte-faithful mirror (same query parse, same URL /
        params / timeout, same non-dict -> [] contract). ONLY the shared-http egress is
        swapped for its async twin (http.get_json -> await http.aget_json)."""
        parsed = _parse_query(query)
        if parsed is None:
            return []
        geo, entry = parsed
        params = {"format": "JSON", "geo": geo, **entry["filters"]}
        raw = await http.aget_json(DATA_URL.format(dataset=entry["dataset"]), params=params, timeout=20)
        if not isinstance(raw, dict):
            return []
        return [(entry, geo, raw)]

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of search -> AsyncSearchCapable. Shares the base async cache
        round-trip (_aapi_search); egress via _araw_fetch; per-record mapping via the SAME
        pure-CPU _to_document (byte-identical to search)."""
        return await self._aapi_search(query, limit, araw_fetch=lambda: self._araw_fetch(query, limit))

    def _to_document(self, raw) -> Optional[Document]:
        if not isinstance(raw, tuple) or len(raw) != 3:
            return None
        entry, geo, js = raw
        return self._series_to_doc(entry, geo, js)

    @classmethod
    def _series_to_doc(cls, entry: dict, geo: str, js: dict) -> Optional[Document]:
        """One curated entry + its JSON-stat cube -> a time-series doc.

        GUARDED: every non-time dimension must have size 1 (a wrong / absent pin leaves a
        size > 1 dimension, so the flat value index no longer maps to time alone) -> None.
        The value map is sparse and position-keyed; since all non-time dims are size 1 and
        time is last, the linear index equals the time position, so we invert the time
        category index and read value[pos] per period. A status-flagged ":" cell is simply
        absent from value, so it is skipped, never read as a zero."""
        if not isinstance(js, dict):
            return None
        ids = js.get("id")
        size = js.get("size")
        if not isinstance(ids, list) or not isinstance(size, list) or len(ids) != len(size):
            return None
        if "time" not in ids:
            return None
        # The "empty value / wrong code" gate: any non-time dimension with size != 1 means a
        # pin was wrong or absent (a multi-series cube), so this decode is invalid -> drop.
        for name, sz in zip(ids, size):
            if name != "time" and sz != 1:
                return None

        dim = js.get("dimension") or {}
        time_index = (((dim.get("time") or {}).get("category") or {}).get("index"))
        if not isinstance(time_index, dict) or not time_index:
            return None
        value = js.get("value")

        # Invert period -> position into position -> period, then read the flat value map.
        pos_to_period: dict[int, str] = {}
        for period, pos in time_index.items():
            try:
                pos_to_period[int(pos)] = str(period)
            except (TypeError, ValueError):
                continue
        series: dict[str, float] = {}
        for pos, period in pos_to_period.items():
            val = cls._as_float(cls._cell(value, pos))
            if val is not None:
                series[period] = val
        if not series:
            return None

        periods_desc = sorted(series, reverse=True)
        latest_period = periods_desc[0]
        latest_value = series[latest_period]
        dataset = entry["dataset"]
        unit = entry.get("unit") or ""
        geo_label = cls._geo_label(dim, geo)
        title = f"{geo_label}: {entry.get('label') or dataset}"

        lines = [title,
                 f"Dataset: {dataset}  ·  Geo: {geo}",
                 ""]
        for period in periods_desc:
            lines.append(f"{period}: {cls._fmt_num(series[period])}" + (f" {unit}" if unit else ""))

        return Document(
            source="eurostat_stats",
            source_id=f"{dataset}/{geo}",
            url=PAGE_URL.format(dataset=dataset),
            title=title,
            content="\n".join(lines),
            date=cls._parse_date(latest_period),
            signals=mk_signal("latest_value", latest_value, kind="engagement",
                              by="eurostat_stats/value", unit=unit or None),
            tags=[t for t in ("eu", geo, dataset) if t],
            metadata={
                "dataset": dataset,
                "geo": geo,
                "geo_label": geo_label,
                "filters": dict(entry.get("filters") or {}),
                "unit": unit,
                "label": entry.get("label"),
                "latest_period": latest_period,
                "latest_value": latest_value,
                "n_periods": len(series),
                "series": {p: series[p] for p in periods_desc},
                "raw": jsonsafe(js),
            },
        )

    def health_check(self) -> tuple[bool, str]:
        """Probe une_rt_a for geo=DE (fully pinned): a JSON-stat body with a non-empty value
        map = alive."""
        try:
            raw = http.get_json(
                DATA_URL.format(dataset="une_rt_a"),
                params={"format": "JSON", "geo": "DE", "sex": "T", "age": "Y15-74", "unit": "PC_ACT"},
                timeout=10)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"
        if isinstance(raw, dict) and raw.get("value"):
            return True, "eurostat JSON-stat value present"
        return False, "eurostat unreachable / empty value"

    # ---------------------------------------------------------------- pure helpers
    @staticmethod
    def _cell(value: Any, pos: int) -> Any:
        """Read one cell from the JSON-stat value map, which is EITHER a position-keyed dict
        ({"0": 3.2, ...}) OR a dense list ([3.2, ...]). A missing / out-of-range cell -> None."""
        if isinstance(value, dict):
            return value.get(str(pos))
        if isinstance(value, list):
            return value[pos] if 0 <= pos < len(value) else None
        return None

    @staticmethod
    def _geo_label(dim: dict, geo: str) -> str:
        """Human geo name from the response's geo dimension label map, falling back to the code."""
        labels = (((dim.get("geo") or {}).get("category") or {}).get("label")) or {}
        val = labels.get(geo) if isinstance(labels, dict) else None
        return val or geo

    @staticmethod
    def _parse_date(period: str) -> Optional[datetime]:
        """Parse a Eurostat time key: annual "2024", monthly "2024-06", daily "2024-06-01",
        quarterly "2024-Q2", or SDMX-style "2024M06" / "2024Q2". None if unparseable."""
        p = str(period).strip()
        for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
            try:
                return datetime.strptime(p, fmt)
            except ValueError:
                continue
        m = re.match(r"^(\d{4})-?Q([1-4])$", p)
        if m:
            return datetime(int(m.group(1)), (int(m.group(2)) - 1) * 3 + 1, 1)
        m = re.match(r"^(\d{4})M(\d{2})$", p)
        if m:
            return datetime(int(m.group(1)), int(m.group(2)), 1)
        return None

    @staticmethod
    def _as_float(v: Any) -> Optional[float]:
        """A numeric cell -> float; None / null / a ":" status flag / non-numeric -> None
        (a missing observation must never become a 0)."""
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
        """Render without a trailing '.0' on whole numbers (counts stay integers; rates keep
        their decimals)."""
        if v == int(v):
            return f"{int(v):,}"
        return f"{v:,.4g}" if abs(v) < 1 else f"{v:,.2f}"

# Registration is automatic via BaseAPIAdapter.__init_subclass__ (no module-tail ceremony).
