"""World Bank Indicators — cross-country economic / labor / education statistics, STRUCTURED.

The World Bank open data API exposes thousands of indicators (GDP, unemployment,
population, school enrollment, …) for every country as a clean time series, no
auth and no key. Polaris-eye's macro-statistics STRUCTURE source: a structured
point-lookup the open web can only narrate, not hand back as queryable data
(one indicator's full year-by-year series for a country, as numbers).

This is NOT a free-text search. It is a STRUCTURED point lookup, so the query is
a small convention rather than a sentence:

    "<COUNTRY> <INDICATOR>"

where COUNTRY is an ISO2 or ISO3 country code (CN, USA, CA, DEU, …) and
INDICATOR is either a known keyword (unemployment, gdp, gdp per capita,
population, inflation, enrollment, …) mapped to its World Bank code, or a raw
World Bank indicator code (e.g. SL.UEM.TOTL.ZS) used verbatim. Examples:

    "CN unemployment"           -> China, SL.UEM.TOTL.ZS
    "USA NY.GDP.MKTP.CD"        -> United States, raw GDP code
    "CA gdp per capita"         -> Canada, NY.GDP.PCAP.CD

One document = one country × one indicator: the full year-by-year series, with
the latest year as the doc date and every {year: value} pair in metadata (so the
agent gets the trend, not a single number). The latest non-null value is exposed
as an engagement-class signal (the headline figure, with provenance).

Access (keyless v2 REST API):
  GET https://api.worldbank.org/v2/country/<code>/indicator/<indicator>
      ?format=json&date=<start>:<end>&per_page=<n>
Response: a 2-element array [meta, rows] where rows[i] = {
    indicator: {id, value}, country: {id, value}, countryiso3code, date, value, unit}.
A null `value` is normal (no data that year) and is kept in the series but never
fed to the headline signal.

Recon trail: the v2 [meta, rows] envelope + per-row field names (indicator.value,
country.value, countryiso3code, date, value) are the documented, long-stable
shape of api.worldbank.org/v2; the Claude sandbox DNS-blackholes the host, so the
field decode is written to that shape and live-verified from the eye host.

Eurostat (the JSON-stat cube sibling) is intentionally NOT in this round: its
response is a flat value map indexed by zipped size/dimension.category.index that
must be reconstructed per-dataset (and each dimension code validated, an empty
value meaning a wrong code), a materially higher decode complexity. World Bank
alone is the clean win here; Eurostat is a deliberate follow-up.

A thin BaseAPIAdapter subclass: the cache / map / register ritual lives in the
base. ``rank_locally`` stays default-True (harmless: at most one doc per call, so
the BM25 funnel is a no-op pass-through that can never reorder a single result).
explicit_only: a structured point lookup by (country, indicator), never broad
fan-out fodder.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from penumbra.core import http
from penumbra.core.normalize import PolarisDocument, jsonsafe, mk_signal
from penumbra.core.sources.api._base import BaseAPIAdapter

API_URL = "https://api.worldbank.org/v2/country/{country}/indicator/{indicator}"
# The human-facing indicator page (canonical, clickable). locations= takes the
# country code the user supplied (ISO2 or ISO3 both resolve on data.worldbank.org).
PAGE_URL = "https://data.worldbank.org/indicator/{indicator}?locations={country}"

# Default observation window: a wide span so the series shows the trend, while
# per_page caps the rows actually returned. Both are query-able overrides later.
_DATE_RANGE = "1990:2025"
_PER_PAGE = 100

# Curated keyword -> World Bank indicator code map. Deliberately SMALL and honest:
# these are the common macro indicators; ANYTHING not here falls through to "treat
# the second query token as a raw indicator code" (so the full WB catalog stays
# reachable without pretending this map is exhaustive). Keys are matched as
# whole-phrase substrings of the lower-cased indicator part, longest first.
_INDICATOR_MAP: dict[str, str] = {
    "gdp per capita": "NY.GDP.PCAP.CD",
    "gdp growth": "NY.GDP.MKTP.KD.ZG",
    "gdp": "NY.GDP.MKTP.CD",
    "unemployment": "SL.UEM.TOTL.ZS",
    "youth unemployment": "SL.UEM.1524.ZS",
    "labor force": "SL.TLF.TOTL.IN",
    "population": "SP.POP.TOTL",
    "population growth": "SP.POP.GROW",
    "life expectancy": "SP.DYN.LE00.IN",
    "inflation": "FP.CPI.TOTL.ZG",
    "tertiary enrollment": "SE.TER.ENRR",
    "education enrollment": "SE.TER.ENRR",
    "enrollment": "SE.TER.ENRR",
    "literacy": "SE.ADT.LITR.ZS",
    "rd expenditure": "GB.XPD.RSDV.GD.ZS",
    "research spending": "GB.XPD.RSDV.GD.ZS",
    "internet users": "IT.NET.USER.ZS",
}
# Longest phrases first so "gdp per capita" wins over "gdp".
_MAP_PHRASES = sorted(_INDICATOR_MAP, key=len, reverse=True)

# A raw World Bank indicator code looks like SL.UEM.TOTL.ZS — uppercase segments
# joined by dots. Used to decide whether the second token is already a code.
_CODE_RE = re.compile(r"^[A-Za-z0-9]+(\.[A-Za-z0-9]+)+$")


def _parse_query(query: str) -> Optional[tuple[str, str]]:
    """Parse "<COUNTRY> <INDICATOR-or-code>" into (country_code, indicator_code).

    First whitespace token is the country code; the remainder is the indicator
    part. The indicator part is resolved via the keyword map (whole-phrase
    substring, longest first); if nothing matches, a single remaining token that
    LOOKS like a WB code is used verbatim. Returns None when the query is empty,
    has no indicator part, or the indicator can be resolved to nothing (so the
    adapter never invents a code)."""
    if not query or not query.strip():
        return None
    parts = query.strip().split(None, 1)
    if len(parts) < 2:
        return None  # need both a country and an indicator
    country = parts[0].strip()
    indicator_part = parts[1].strip()
    if not country or not indicator_part:
        return None

    low = indicator_part.lower()
    for phrase in _MAP_PHRASES:
        if phrase in low:
            return country, _INDICATOR_MAP[phrase]

    # No keyword hit: accept a raw code (single token shaped like SL.UEM.TOTL.ZS).
    token = indicator_part.split()[0]
    if _CODE_RE.match(token):
        return country, token.upper()
    return None  # unrecognized indicator, no guess


class WorldBankStatsAdapter(BaseAPIAdapter):
    name = "worldbank_stats"
    needs_credentials = False
    description = (
        "World Bank Indicators: cross-country economic / labor / education statistics "
        "(GDP, unemployment, population, enrollment, ...) as a year-by-year time series, "
        "keyless. STRUCTURED point lookup, NOT free-text search: query is "
        "'<COUNTRY> <INDICATOR>' where COUNTRY is an ISO2/ISO3 code (CN, USA, CA) and "
        "INDICATOR is a keyword (unemployment / gdp / gdp per capita / population / "
        "inflation / enrollment ...) or a raw WB code (SL.UEM.TOTL.ZS). One doc = one "
        "country x one indicator, full series. Examples: 'CN unemployment', "
        "'USA NY.GDP.MKTP.CD', 'CA gdp per capita'."
    )
    cache_ttl = 86400  # 24h: macro indicators update at most a few times a year
    kind = "lookup"
    domains = ["data"]
    modes = ["STRUCTURE"]
    explicit_only = ("worldbank_stats: a structured point lookup by (country, indicator), "
                     "named on demand, not broad-fan-out fodder")
    health_probe_url = (
        "https://api.worldbank.org/v2/country/USA/indicator/SP.POP.TOTL"
        "?format=json&per_page=1"
    )

    def _raw_fetch(self, query: str, limit: int) -> list:
        """Resolve the (country, indicator) convention, GET the series, and return
        it as a SINGLE aggregation unit [(country, indicator, rows)] so the base's
        per-record loop yields exactly one time-series doc. Any failure (bad query,
        network, malformed envelope) -> [] (the adapter contract)."""
        parsed = _parse_query(query)
        if parsed is None:
            return []
        country, indicator = parsed
        raw = http.get_json(
            API_URL.format(country=country, indicator=indicator),
            params={"format": "json", "date": _DATE_RANGE, "per_page": _PER_PAGE},
            timeout=15,
        )
        rows = self._rows(raw)
        if not rows:
            return []
        return [(country, indicator, rows)]

    def _to_document(self, raw) -> Optional[PolarisDocument]:
        """One aggregation unit (country, indicator, rows) -> one time-series doc."""
        if not isinstance(raw, tuple) or len(raw) != 3:
            return None
        country, indicator, rows = raw
        return self._series_to_doc(country, indicator, rows)

    # ---------------------------------------------------------------- pure helpers
    @staticmethod
    def _rows(raw: Any) -> list:
        """The v2 envelope is [meta, rows]; pull rows defensively. Anything else
        (None on a failed fetch, an error-object dict, a short array) -> []."""
        if not isinstance(raw, list) or len(raw) < 2:
            return []
        rows = raw[1]
        return rows if isinstance(rows, list) else []

    @classmethod
    def _series_to_doc(cls, country: str, indicator: str,
                       rows: list) -> Optional[PolarisDocument]:
        """Build ONE doc for a country x indicator from its row list. Per-row
        decode is guarded so a single malformed row can't sink the series; an
        empty / all-malformed series returns None (no doc on nothing)."""
        if not rows:
            return None

        # {year(int): value(float)} for the numeric points, kept newest-first when listed.
        series: dict[int, float] = {}
        indicator_name = indicator
        country_name = country
        country_iso3 = ""
        unit = ""
        for row in rows:
            if not isinstance(row, dict):
                continue
            year = cls._as_int(row.get("date"))
            if year is None:
                continue
            # Carry the human labels off the FIRST row that supplies each (don't let a
            # later junk-label row clobber a good earlier one). indicator_name/country_name
            # start as the codes, so "still equals the code" means "not yet filled".
            ind = row.get("indicator")
            if indicator_name == indicator and isinstance(ind, dict) and ind.get("value"):
                indicator_name = ind["value"]
            ctry = row.get("country")
            if country_name == country and isinstance(ctry, dict) and ctry.get("value"):
                country_name = ctry["value"]
            if not country_iso3 and row.get("countryiso3code"):
                country_iso3 = row["countryiso3code"]
            if not unit and row.get("unit"):
                unit = row["unit"]
            val = cls._as_float(row.get("value"))
            if val is not None:
                series[year] = val

        if not series:
            return None  # the country/indicator pair exists but has no numeric data

        years_desc = sorted(series, reverse=True)
        latest_year = years_desc[0]
        latest_val = series[latest_year]

        # content: a compact year-by-year listing, newest first.
        lines = [f"{country_name}: {indicator_name}",
                 f"Indicator: {indicator}  ·  Country: {country_iso3 or country}",
                 ""]
        for y in years_desc:
            v = series[y]
            lines.append(f"{y}: {cls._fmt_num(v)}" + (f" {unit}" if unit else ""))

        return PolarisDocument(
            source="worldbank_stats",
            source_id=f"{country}/{indicator}",
            url=PAGE_URL.format(indicator=indicator, country=country),
            title=f"{indicator_name} ({country_name})",
            content="\n".join(lines),
            date=datetime(latest_year, 1, 1),
            signals=mk_signal(
                "latest_value", latest_val, kind="engagement",
                by="worldbank_stats/value", unit=unit or None,
            ),
            tags=[t for t in (country_iso3 or country, indicator) if t],
            metadata={
                "country": country_name,
                "country_code": country,
                "country_iso3": country_iso3,
                "indicator_code": indicator,
                "indicator_name": indicator_name,
                "latest_year": latest_year,
                "latest_value": latest_val,
                "unit": unit,
                "series": {str(y): series[y] for y in years_desc},
                "raw": jsonsafe(rows),
            },
        )

    @staticmethod
    def _as_int(v: Any) -> Optional[int]:
        """A 'date' field like '2024' -> 2024; anything non-integer -> None."""
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_float(v: Any) -> Optional[float]:
        """A numeric 'value' -> float; None / null / non-numeric -> None (a null
        observation year is normal and must not become a 0)."""
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
        """Render a value without a trailing '.0' on whole numbers (population is
        an integer count; a rate keeps its decimals)."""
        if v == int(v):
            return f"{int(v):,}"
        return f"{v:,.4g}" if abs(v) < 1 else f"{v:,.2f}"

# Registration is automatic via BaseAPIAdapter.__init_subclass__ (no module-tail ceremony).
