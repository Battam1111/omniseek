"""Grants.gov — open / forecasted US federal funding OPPORTUNITIES via the keyless Search2 API.

The complement to nsf_awards / nih_reporter: those return grants already AWARDED (retrospective,
who got the money); Grants.gov returns the OPPORTUNITIES you can still apply to (prospective:
posted + forecasted solicitations) across ALL ~26 federal grant-making agencies (NSF CISE/IIS,
DOE, DARPA, IARPA, NIST, NIH, ...), not just one. Penumbra's "find funding to apply for"
STRUCTURE source: a filterable, all-agency federal opportunity feed web search cannot assemble.

Access via the public Search2 API (no auth, no key; POST-only):
  POST https://api.grants.gov/v1/api/search2
  body {"keyword": "<q>", "rows": <n>, "oppStatuses": "posted|forecasted", "startRecordNum": 0}
  -> {"data": {"hitCount", "oppHits": [{id, number, title, agency, agencyCode,
       openDate, closeDate, oppStatus, cfdaList: [...]}, ...]}}
The detail-page URL is CONSTRUCTED from the opportunity id (no url field), so this is a coded
adapter (POST body + nested data.oppHits + URL construction), modelled on nih_reporter.

backend="grants_gov" (its own upstream, distinct from api.nsf.gov / api.reporter.nih.gov).
explicit_only: a named funding-opportunity drill. closeDate is the application DEADLINE (the
field an applicant cares about most), so it is used as the doc date.

ENV note: the Claude sandbox DNS-blackholes api.grants.gov (198.18.x); the real adapter runs on
the Mac eye host, which reaches it. Live-verify the oppHits field names from the eye host
(penumbra_search 单源钻取) before trusting them. Recon trail: brain note eye-free-api-probe-round2-2026-06-21.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from penumbra.core import http
from penumbra.core.normalize import Document, jsonsafe
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

API_URL = "https://api.grants.gov/v1/api/search2"
DETAIL_URL = "https://www.grants.gov/search-results-detail/{id}"
_MAX_ROWS = 100


class GrantsGovAdapter(BaseScrapeAdapter):
    name = "grants_gov"
    needs_credentials = False
    description = ("Grants.gov — OPEN + forecasted US federal funding OPPORTUNITIES you can apply "
                   "to, across all ~26 grant-making agencies (NSF / DOE / DARPA / NIH / ...); name "
                   "it to find applyable funding by topic. The prospective complement to "
                   "nsf_awards / nih_reporter (which show awards already granted). STRUCTURE, "
                   "keyless POST api.grants.gov.")
    cache_ttl = 21600  # 6h: opportunity listings change slowly
    kind = "lookup"
    domains = ["funding"]
    regions = ["us"]
    modes = ["STRUCTURE"]
    explicit_only = ("grants_gov: a named US federal funding-opportunity drill (by topic / agency), "
                     "not broad-fan-out fodder")

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        body = {
            "keyword": query or "",
            "rows": max(1, min(int(limit), _MAX_ROWS)),
            "oppStatuses": "posted|forecasted",  # applyable now + upcoming
            "startRecordNum": 0,
        }
        return http.post_json(API_URL, json=body, timeout=20)

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, dict):
            return []
        hits = ((raw.get("data") or {}).get("oppHits")) or []
        docs: list[Document] = []
        for opp in hits[:limit]:
            doc = self._opp_to_doc(opp)
            if doc is not None:
                docs.append(doc)
        return docs

    def _opp_to_doc(self, opp: Any) -> Optional[Document]:
        if not isinstance(opp, dict):
            return None
        oid = opp.get("id")
        title = opp.get("title")
        if not oid or not title:
            return None  # no stable id / title -> no doc
        agency = opp.get("agency")
        number = opp.get("number")
        status = opp.get("oppStatus")
        cfda = opp.get("cfdaList") if isinstance(opp.get("cfdaList"), list) else []
        # closeDate is the application deadline (what an applicant needs); fall back to openDate.
        date = self._parse_date(opp.get("closeDate")) or self._parse_date(opp.get("openDate"))
        content_bits = [title]
        if agency:
            content_bits.append(f"Agency: {agency}")
        if number:
            content_bits.append(f"Opportunity number: {number}")
        if opp.get("closeDate"):
            content_bits.append(f"Closes: {opp.get('closeDate')}")
        return Document(
            source=self.name,
            source_id=str(oid),
            url=DETAIL_URL.format(id=oid),
            title=title,
            content=". ".join(content_bits),
            author=agency,
            date=date,
            signals={},
            tags=[t for t in ([opp.get("agencyCode"), status] + list(cfda)) if t],
            metadata={
                "number": number,
                "agency": agency,
                "agency_code": opp.get("agencyCode"),
                "opp_status": status,
                "open_date": opp.get("openDate"),
                "close_date": opp.get("closeDate"),
                "cfda_list": cfda,
                "raw": jsonsafe(opp),
            },
        )

    @staticmethod
    def _parse_date(raw: Any) -> Optional[datetime]:
        """Grants.gov dates are 'MM/DD/YYYY' (e.g. '06/30/2026'); tolerate ISO too. None else."""
        if not isinstance(raw, str) or not raw.strip():
            return None
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(raw.strip()[:19] if "T" in raw else raw.strip(), fmt)
            except ValueError:
                continue
        return None

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
