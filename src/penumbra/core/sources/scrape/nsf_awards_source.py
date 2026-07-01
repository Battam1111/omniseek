"""NSF Award Search — US National Science Foundation research grants via the keyless v1 API.

The NSF Award Search Web API exposes every NSF-funded award: the PI, the awardee
institution, the obligated/total dollar amounts, and the full project abstract.
Polaris-eye's US-research-funding STRUCTURE source — filterable grant records (by
keyword / institution / PI) the open web cannot hand back as data. Fills the
grants/funding gap (zero coverage before this).

Access via the public v1 API (no auth, no key):
  GET https://api.nsf.gov/services/v1/awards.json?keyword=<q>&rpp=<limit>&printFields=...
Response: {"response": {"award": [{id, title, abstractText, pdPIName, awardeeName,
  startDate, fundsObligatedAmt, estimatedTotalAmt, ...}, ...], "serviceNotification": ...}}
The award page URL is CONSTRUCTED from the id (no url field in the payload), which is
why this is a thin coded adapter rather than a declarative ``sources.json`` row.

A thin BaseScrapeAdapter subclass: the cache check / atomic set_docs / self-registration
ritual lives in the base; this declares its facets and fills the two hooks. rank stays
default-False (the API returns its own order for the keyword). explicit_only: US-only
federal science grants are a named, deliberate drill (by institution / PI / topic), not
broad-fan-out fodder.

Recon trail: brain note eye-free-api-probe-2026-06-21 (live-probed; response field names
confirmed by eye_add_url from the eye host — the Claude sandbox DNS-blackholes api.nsf.gov).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from penumbra.core import http
from penumbra.core.normalize import PolarisDocument, jsonsafe, mk_signal
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

API_URL = "https://api.nsf.gov/services/v1/awards.json"
AWARD_URL = "https://www.nsf.gov/awardsearch/showAward?AWD_ID={id}"
# Only fields confirmed present in the live probe response are requested, so the
# decode never depends on a guessed printField name.
_FIELDS = ("id,title,abstractText,pdPIName,awardeeName,awardeeStateCode,"
           "fundsObligatedAmt,estimatedTotalAmt,startDate,expDate")


class NSFAwardsAdapter(BaseScrapeAdapter):
    name = "nsf_awards"
    needs_credentials = False
    description = ("NSF Award Search — US National Science Foundation research grants "
                   "(PI / award amount / awardee institution / full abstract); name it to "
                   "drill US federal funding by topic / institution / PI. STRUCTURE, keyless "
                   "api.nsf.gov; fills the grants/funding gap.")
    cache_ttl = 21600  # 6h: grant records change slowly
    kind = "lookup"
    domains = ["funding"]
    regions = ["us"]
    modes = ["STRUCTURE"]
    explicit_only = ("nsf_awards: US-only federal science grants — a named drill "
                     "(by topic / institution / PI), not broad-fan-out fodder")

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        # rpp is capped at 25 by the API; ask for the requested count (1..25).
        return http.get_json(
            API_URL,
            params={"keyword": query, "rpp": max(1, min(int(limit), 25)),
                    "printFields": _FIELDS},
            timeout=15,
        )

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[PolarisDocument]:
        if not isinstance(raw, dict):
            return []
        awards = ((raw.get("response") or {}).get("award")) or []
        docs: list[PolarisDocument] = []
        for award in awards[:limit]:
            doc = self._award_to_doc(award)
            if doc is not None:
                docs.append(doc)
        return docs

    def _award_to_doc(self, award: Any) -> Optional[PolarisDocument]:
        if not isinstance(award, dict):
            return None
        aid = award.get("id")
        title = award.get("title")
        if not aid or not title:
            return None  # no stable id / title ⇒ no usable doc
        amt = award.get("fundsObligatedAmt") or award.get("estimatedTotalAmt")
        return PolarisDocument(
            source=self.name,
            source_id=str(aid),
            url=AWARD_URL.format(id=aid),
            title=title,
            content=award.get("abstractText") or title,
            author=award.get("pdPIName"),
            date=self._parse_date(award.get("startDate")),
            signals=self._amount_signal(amt),
            tags=[t for t in (award.get("awardeeStateCode"),) if t],
            metadata={
                "awardee": award.get("awardeeName"),
                "amount_obligated": award.get("fundsObligatedAmt"),
                "amount_total": award.get("estimatedTotalAmt"),
                "expires": award.get("expDate"),
                "raw": jsonsafe(award),
            },
        )

    @staticmethod
    def _amount_signal(amount: Any) -> dict:
        """Award dollar amount → an engagement-class signal (the grant's scale).
        Absent/non-numeric ⇒ {} (never raises)."""
        try:
            val = float(amount)
        except (TypeError, ValueError):
            return {}
        return mk_signal("award_amount", val, kind="engagement",
                         by="nsf_awards/fundsObligatedAmt", unit="USD")

    @staticmethod
    def _parse_date(raw: Any) -> Optional[datetime]:
        """NSF award dates are MM/DD/YYYY (e.g. '01/15/2025'). None on anything else."""
        if not isinstance(raw, str) or not raw.strip():
            return None
        for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw.strip(), fmt)
            except ValueError:
                continue
        return None

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
