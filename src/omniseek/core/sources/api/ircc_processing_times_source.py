"""IRCC processing times: Canada's official per-country / per-service table, STRUCTURED.

IRCC publishes the live processing-time estimate for every application type from
every country as JSON (the data that powers the official "Check processing
times" tool). Web search only narrates it and the tool itself is a form you must
click through country by country; this exposes it as a queryable lookup so
"study permit China" or "work permit India" returns the current estimate
directly. Mode: STRUCTURE (a per-route lookup table, refreshed monthly by IRCC).

Four payloads make one table:
  - data-ptime-en.json          : outside-Canada services keyed by application
                                  type then ISO-2 country code -> time string.
  - data-ptime-non-country-en.json : in-Canada services keyed by category then
                                  sub-service -> time string (+ a lastupdated).
  - data-country-name-en.json   : ISO-2 -> country display name.
  - flpt-en.json                : forward-looking estimates for permanent residence
                                  programs (spousal / partner sponsorship, parents
                                  and grandparents, economic classes, refugees,
                                  citizenship), keyed by program -> time string,
                                  plus people waiting per program. Spousal
                                  sponsorship lives ONLY here (verified 2026-10-11
                                  against the 2026-10-09 copy of the file); before
                                  this payload was read, a spouse query matched no
                                  row and fell through to unrelated refugee rows.

Query matching is stricter than the shared keyword filter: words every row carries
("processing", "time", "IRCC") and question filler are dropped, then only the rows
matching the MOST query words are kept, so "spouse sponsorship" returns the spousal
rows and not every row that shares one word with it.

Access (verified 2026-07-10): canada.ca walls the large data-ptime-en.json from
plain HTTP at the transport level (curl times out / HTTP 000, WebFetch 403),
exactly like ircc_ee_rounds; the CDP real browser reads it fine. So this fetches
all three via the shared CDP Chrome (lazy import) and is explicit_only: named
calls only, never the broad fan-out.
"""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from omniseek.core import cache, relevance
from omniseek.core.normalize import Document, keyword_score_filter

logger = logging.getLogger(__name__)

PTIME_JSON = "https://www.canada.ca/content/dam/ircc/documents/json/data-ptime-en.json"
NONCOUNTRY_JSON = "https://www.canada.ca/content/dam/ircc/documents/json/data-ptime-non-country-en.json"
COUNTRY_NAME_JSON = "https://www.canada.ca/content/dam/ircc/documents/json/data-country-name-en.json"
FLPT_JSON = "https://www.canada.ca/content/dam/ircc/documents/json/flpt-en.json"
CHECK_PAGE = ("https://www.canada.ca/en/immigration-refugees-citizenship/services/"
              "application/check-processing-times.html")
CACHE_TTL = 86400  # 24h: IRCC refreshes the table roughly monthly, so daily is ample
# A table built while the flpt payload failed is kept only briefly, so the spousal and other
# permanent residence rows come back on the next call instead of a day later.
PARTIAL_CACHE_TTL = 900
TABLES_CACHE_KEY = ("ircc_processing_times", "tables", "v2")  # v2: adds the flpt payload

# Human labels for the outside-Canada application-type keys (data-ptime-en.json).
APP_TYPES = {
    "visitor-outside-canada": "Visitor visa (from outside Canada)",
    "supervisa": "Super visa (parents and grandparents)",
    "study": "Study permit (from outside Canada)",
    "work": "Work permit (from outside Canada)",
    "child_dependent": "Sponsor a dependent child",
    "child_adopted": "Sponsor an adopted child",
    "refugees_gov": "Government-assisted refugee",
    "refugees_private": "Privately sponsored refugee",
}

# Human labels for the in-Canada sub-service keys (data-ptime-non-country-en.json).
# Unknown keys fall back to a prettified form, so a new IRCC key still yields a doc.
IN_CANADA = {
    "visitor_inside_canada": "Visitor record (in Canada)",
    "visitor_extension": "Visitor visa / status extension (in Canada)",
    "study_extension": "Study permit extension (in Canada)",
    "work_extension": "Work permit extension (in Canada)",
    "iec": "International Experience Canada (current season)",
    "iec_past": "International Experience Canada (past season)",
    "eta": "Electronic Travel Authorization (eTA)",
    "skilled_trades_ee": "Federal Skilled Trades (Express Entry)",
    "cit_resumption": "Citizenship: resumption",
    "cit_renunciation": "Citizenship: renunciation",
    "cit_search": "Citizenship: search / proof of records",
    "cit_adoption_part1": "Citizenship for adopted persons (Part 1)",
    "new_pr": "New permanent resident (PR) card",
    "existing_pr": "PR card renewal",
    "vos": "Verification of status (VOS)",
    "replacement": "Replacement of an immigration document",
    "amend_imm": "Amend an immigration document",
    "amend_tr": "Amend a temporary resident document",
    "sawp_current": "Seasonal Agricultural Worker Program (SAWP)",
}

# Human labels for the forward-looking program keys (flpt-en.json "current-flpt"). The labels
# carry the words people search with (spouse / spousal / partner / common-law / conjugal,
# inland / outland), since the filter is lexical. Unknown keys fall back to a prettified form.
_ROC = "outside Quebec"
FLPT_PROGRAMS = {
    "spousal-canada-roc": ("Spousal sponsorship (spouse, common-law or conjugal partner), "
                           f"partner already in Canada (inland), {_ROC}"),
    "spousal-canada-quebec": ("Spousal sponsorship (spouse, common-law or conjugal partner), "
                              "partner already in Canada (inland), Quebec"),
    "spousal-abroad-roc": ("Spousal sponsorship (spouse, common-law or conjugal partner), "
                           f"partner outside Canada (outland), {_ROC}"),
    "spousal-abroad-quebec": ("Spousal sponsorship (spouse, common-law or conjugal partner), "
                              "partner outside Canada (outland), Quebec"),
    "dependants-abroad-roc": f"Dependent child sponsorship, child outside Canada, {_ROC}",
    "dependants-abroad-quebec": "Dependent child sponsorship, child outside Canada, Quebec",
    "pgp-roc": f"Parents and grandparents sponsorship (PGP), {_ROC}",
    "pgp-quebec": "Parents and grandparents sponsorship (PGP), Quebec",
    "cec": "Canadian Experience Class (CEC, Express Entry)",
    "fsw": "Federal Skilled Worker (FSW, Express Entry)",
    "pnp-ee": "Provincial Nominee Program (PNP, Express Entry)",
    "pnp-base": "Provincial Nominee Program (PNP, base, paper)",
    "aip": "Atlantic Immigration Program (AIP)",
    "qsw": "Quebec Skilled Worker (QSW)",
    "quebec-business": "Quebec business immigration",
    "caregiver": "Home care worker / caregiver permanent residence",
    "self-employed": "Self-employed persons program",
    "startup-visa": "Start-up Visa program",
    "government-refugees-roc": f"Government-assisted refugees, {_ROC}",
    "government-refugees-quebec": "Government-assisted refugees, Quebec",
    "private-refugees-roc": f"Privately sponsored refugees, {_ROC}",
    "private-refugees-quebec": "Privately sponsored refugees, Quebec",
    "protected-persons-in-canada-roc": f"Protected persons in Canada (refugee claimants), {_ROC}",
    "protected-persons-in-canada-quebec": "Protected persons in Canada (refugee claimants), Quebec",
    "humanitarian-roc": f"Humanitarian and compassionate (H&C) permanent residence, {_ROC}",
    "humanitarian-quebec": "Humanitarian and compassionate (H&C) permanent residence, Quebec",
    "citizen-grants": "Citizenship grant (become a Canadian citizen)",
    "citizen-proofs": "Citizenship certificate (proof of citizenship)",
}

# Query words that carry no routing intent here: every row says "Current IRCC processing time",
# so these matched the whole table and let any row through (the R09 refugee / renunciation rows
# for "spousal sponsorship outland processing time"), plus common question filler. "in" is noise
# too: "Processed in Canada" is on every in-Canada row; the inland / outland labels carry that intent.
_QUERY_NOISE = frozenset({
    "processing", "process", "processed", "time", "times", "ircc", "current", "currently",
    "estimate", "estimated", "how", "long", "does", "do", "is", "are", "it", "the", "for",
    "of", "to", "in", "my", "what", "take", "takes", "wait", "waiting", "application",
    "applications",
})

_HAS_DIGIT = re.compile(r"\d")


def _has_time(value) -> bool:
    """A real estimate (not 'No processing time available' / 'Not enough data')."""
    if isinstance(value, dict):
        return any(_has_time(v) for v in value.values())
    return bool(_HAS_DIGIT.search(str(value)))


def _fetch_json(url: str) -> dict:
    """One JSON payload via the CDP real browser (lazy import: never pull
    playwright in when the source is untouched)."""
    from omniseek.core.sources.walled._cdp import cdp_call

    def _nav(page):
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(1000)
        return page.inner_text("body")

    body = cdp_call(_nav, initial_url=url, timeout=60)
    return json.loads(body)


def _filter_rows(docs: list[Document], query: str) -> list[Document]:
    """Keyword filter for this table: drop noise words, keep only the best-covered rows.

    The shared keyword_score_filter admits a row on ONE matching word. Over a table whose
    every row shares its boilerplate, that let unrelated rows through. Here the query loses
    the words in _QUERY_NOISE first (a query of only such words is treated as empty: the
    whole table, as an empty query gives), then rows are ranked by the shared scorer and only
    the rows that match the largest number of the remaining query words are kept."""
    words = [w for w in re.split(r"\s+", (query or "").strip()) if w]
    kept = [w for w in words if w.lower().strip(".,;:?!\"'()") not in _QUERY_NOISE]
    q = " ".join(kept)
    if not q:
        return docs
    ranked = keyword_score_filter(docs, q)
    if not ranked:
        return []
    units = relevance.query_units(q)

    def coverage(d: Document) -> int:
        present = set(relevance.tokenize(f"{d.title or ''} {d.content or ''}"))
        return sum(1 for u in units if relevance._unit_hit(u, present))

    covers = [coverage(d) for d in ranked]
    best = max(covers)
    return [d for d, c in zip(ranked, covers) if c == best]


class IRCCProcessingTimesAdapter:
    name = "ircc_processing_times"
    needs_credentials = False
    kind = "lookup"
    domains = ["immigration"]
    regions = ["ca"]
    modes = ["STRUCTURE"]
    explicit_only = "CDP fetch of canada.ca (data-ptime-en.json transport-walled to plain HTTP); named only"
    description = (
        "IRCC 处理时长表 (加拿大官方 JSON, 经 CDP 真浏览器取): 每种申请类型 x 每个国家的当前处理时长估计, "
        "结构化可查, 即官方 Check processing times 工具背后的数据. 覆盖境外服务 (访客 / 学签 / 工签 / 超级签证 / "
        "团聚 / 难民) 与境内服务 (延期 / PR 卡 / 公民 / eTA / IEC...), 外加永居类项目的前瞻估计 (配偶 / 伴侣担保 "
        "inland / outland, 父母祖父母, CEC / FSW / PNP, 难民, 入籍) 与排队人数. 例: 'study permit China' / "
        "'work permit India' / 'PR card' / 'spouse sponsorship outland'. 空 query=各路由一批; 关键词按申请类型 + "
        "国家过滤, 只回命中查询词最多的行 (processing / time 这类每行都有的词不算)."
    )

    # table cache (raw JSON payloads, one combined blob)
    def _tables(self) -> Optional[dict]:
        key = cache.make_key(*TABLES_CACHE_KEY)
        cached = cache.get(key)
        if cached is not None:
            return cached
        try:
            tables = {
                "country": _fetch_json(PTIME_JSON),
                "noncountry": _fetch_json(NONCOUNTRY_JSON),
                "names": _fetch_json(COUNTRY_NAME_JSON).get("country-name", {}),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("ircc_processing_times: fetch failed: %s", exc)
            return None
        # The flpt payload is fetched on its own: its failure must not cost the per-country table.
        try:
            tables["flpt"] = _fetch_json(FLPT_JSON)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ircc_processing_times: flpt fetch failed: %s", exc)
            tables["flpt"] = None
        cache.set(key, tables, ttl=CACHE_TTL if tables["flpt"] else PARTIAL_CACHE_TTL)
        return tables

    @staticmethod
    def _updated(noncountry: dict) -> Optional[datetime]:
        raw = ((noncountry.get("default-update") or {}).get("lastupdated") or "").strip()
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%B %d, %Y").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None

    def _build_docs(self, tables: dict) -> list[Document]:
        names = tables.get("names") or {}
        updated = self._updated(tables.get("noncountry") or {})
        updated_str = updated.date().isoformat() if updated else None
        docs: list[Document] = []

        # Outside-Canada: application type x country code.
        for app_key, countries in (tables.get("country") or {}).items():
            label = APP_TYPES.get(app_key, app_key.replace("-", " ").replace("_", " ").title())
            if not isinstance(countries, dict):
                continue
            for cc, value in countries.items():
                if not _has_time(value):
                    continue
                country = html.unescape(names.get(cc, cc))
                if isinstance(value, dict):  # refugees_private -> {sponsor, refugee}
                    value_str = "  ·  ".join(f"{k}: {v}" for k, v in value.items())
                else:
                    value_str = str(value)
                docs.append(self._doc(
                    source_id=f"{app_key}:{cc}",
                    title=f"{label}, {country}: {value_str}",
                    body=f"{label}\nApply from: {country} ({cc})\n"
                         f"Current IRCC processing time: {value_str}",
                    updated=updated, updated_str=updated_str,
                    tags=["processing-time", "immigration", "ca", app_key],
                    meta={"application_type": app_key, "country_code": cc,
                          "country": country, "processing_time": value_str,
                          "in_canada": False, "last_updated": updated_str, "raw": value},
                ))

        # In-Canada: category x sub-service.
        for cat, subs in (tables.get("noncountry") or {}).items():
            if cat == "default-update" or not isinstance(subs, dict):
                continue
            for sub_key, value in subs.items():
                if not _has_time(value):
                    continue
                label = IN_CANADA.get(sub_key, sub_key.replace("_", " ").title())
                docs.append(self._doc(
                    source_id=f"incanada:{cat}:{sub_key}",
                    title=f"{label} (in Canada): {value}",
                    body=f"{label}\nProcessed in Canada.\n"
                         f"Current IRCC processing time: {value}",
                    updated=updated, updated_str=updated_str,
                    tags=["processing-time", "immigration", "ca", "in-canada", cat],
                    meta={"application_type": cat, "service": sub_key,
                          "processing_time": str(value), "in_canada": True,
                          "last_updated": updated_str, "raw": value},
                ))

        docs.extend(self._flpt_docs(tables.get("flpt") or {}))
        return docs

    def _flpt_docs(self, flpt: dict) -> list[Document]:
        """Permanent residence / sponsorship / citizenship rows from flpt-en.json."""
        current = flpt.get("current-flpt") if isinstance(flpt, dict) else None
        if not isinstance(current, dict):
            return []
        people = flpt.get("total-people") if isinstance(flpt.get("total-people"), dict) else {}
        raw_upd = ((flpt.get("default-update") or {}).get("flpt_lastupdated") or "").strip()
        try:
            updated = datetime.strptime(raw_upd, "%B %d, %Y").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            updated = None
        updated_str = updated.date().isoformat() if updated else None
        docs: list[Document] = []
        for key, value in current.items():
            value_str = str(value or "").strip()
            if not value_str:
                continue
            label = FLPT_PROGRAMS.get(key, key.replace("-", " ").replace("_", " ").title())
            waiting = str(people.get(key) or "").strip()
            body = (f"{label}\nPermanent residence / sponsorship / citizenship program.\n"
                    f"Current IRCC processing time (forward-looking estimate for a new "
                    f"application): {value_str}")
            if waiting:
                body += f"\n{waiting}"
            docs.append(self._doc(
                source_id=f"flpt:{key}",
                title=f"{label}: {value_str}",
                body=body,
                updated=updated, updated_str=updated_str,
                tags=["processing-time", "immigration", "ca", "flpt", key],
                meta={"application_type": key, "processing_time": value_str,
                      "people_waiting": waiting or None, "forward_looking": True,
                      "in_canada": "-canada-" in key or "-in-canada-" in key,
                      "last_updated": updated_str, "raw": value},
            ))
        return docs

    def _doc(self, *, source_id, title, body, updated, updated_str, tags, meta) -> Document:
        if updated_str:
            body = f"{body}\n(IRCC estimate as of {updated_str})"
        return Document(
            source=self.name,
            source_id=source_id,
            url=CHECK_PAGE,
            title=title,
            content=body,
            date=updated,
            tags=tags,
            metadata=meta,
        )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        tables = self._tables()
        if not tables:
            return []
        docs = self._build_docs(tables)
        return _filter_rows(docs, query)[:limit]

    def fetch_url(self, url: str) -> Optional[Document]:
        return None  # structured lookup source; reach it via search

    def health_check(self) -> tuple[Optional[bool], str]:
        # LIGHT: never a full CDP fetch in a health probe (the P19 lesson). Browser liveness is OUR
        # side and the cache is a past answer: neither asks the IRCC site anything, so a live browser
        # is None (not verified), not True. A dead browser stays False (the source cannot serve).
        try:
            from omniseek.core.sources.walled._cdp import cdp_health
            alive, msg = cdp_health(ensure=True)
        except Exception as exc:  # noqa: BLE001
            return False, f"CDP unavailable: {exc}"
        if not alive:
            return False, f"CDP down: {msg}"
        cached = cache.get(cache.make_key(*TABLES_CACHE_KEY))
        if cached:
            n = sum(len(v) for v in (cached.get("country") or {}).values() if isinstance(v, dict))
            return None, f"not probed (CDP up; ~{n} country rows cached; the IRCC site is not asked)"
        return None, "not probed (CDP up; no cache yet; the IRCC site is not asked)"


from omniseek.core.fetcher import register_adapter

register_adapter(IRCCProcessingTimesAdapter())
