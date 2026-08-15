"""ClinicalTrials.gov — registered clinical studies via the keyless v2 API.

clinicaltrials.gov is the US NIH registry of clinical studies worldwide: the
authoritative source for trial protocols, recruitment status, phase, sponsor,
and the conditions/interventions under study. OmniSeek's clinical/health
depth source — full-text trial summaries the open web does not surface in a
structured form.

Access via the public v2 REST API (no auth, no key):
  GET https://clinicaltrials.gov/api/v2/studies?query.term=<q>&pageSize=<limit>
Response: {"studies": [{"protocolSection": {
    identificationModule: {nctId, briefTitle},
    statusModule: {overallStatus, startDateStruct: {date}},
    designModule: {phases: [...]},
    sponsorCollaboratorsModule: {leadSponsor: {name}},
    conditionsModule: {conditions: [...]},
    descriptionModule: {briefSummary},
  }}, ...], "nextPageToken": ...}

A thin BaseScrapeAdapter subclass: the cache check / atomic set_docs /
self-registration ritual lives in the base; this declares its facets and fills
the two hooks. rank stays default-False because the endpoint returns server
relevance order for query.term.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from omniseek.core import http
from omniseek.core.normalize import Document, jsonsafe, mk_signal
from omniseek.core.sources.scrape._base import BaseScrapeAdapter

API_URL = "https://clinicaltrials.gov/api/v2/studies"
STUDY_URL = "https://clinicaltrials.gov/study/{nct}"


class ClinicalTrialsAdapter(BaseScrapeAdapter):
    name = "clinicaltrials"
    needs_credentials = False
    description = "ClinicalTrials.gov — registered clinical trials (status/phase/condition/sponsor) via the keyless NIH v2 API"
    cache_ttl = 900
    kind = "lookup"
    domains = ["clinical", "health"]
    explicit_only = (
        "clinicaltrials: named drill only. MEASURED 2026-07-25 over 1986 recorded searches: timed out "
        "776 times (39% of all searches) and reached the ranked top-k ZERO times, sole-contributed "
        "ZERO times. It is not broken (a named drill returns real results), it is simply slower than "
        "the broad deadline while its domain (clinical trials) sits outside the queries this eye "
        "actually serves. Kept fully reachable by name, and excluded_relevant still recommends it when "
        "a query genuinely matches. Captain's call 2026-07-25.")
    modes = ["STRUCTURE"]

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        # pageSize is clamped by the API; ask for the requested count (>=1).
        return http.get_json(
            API_URL,
            params={"query.term": query, "pageSize": max(1, int(limit))},
            timeout=15,
        )

    async def _araw_fetch(self, query: str, limit: int) -> Optional[Any]:
        # Async twin of _raw_fetch: byte-faithful mirror, only http.get_json → await http.aget_json.
        return await http.aget_json(
            API_URL,
            params={"query.term": query, "pageSize": max(1, int(limit))},
            timeout=15,
        )

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, dict):
            return []
        studies = raw.get("studies") or []
        docs: list[Document] = []
        for study in studies[:limit]:
            doc = self._study_to_doc(study)
            if doc is not None:
                docs.append(doc)
        return docs

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of search -> AsyncSearchCapable. Shares the base async cache round-trip;
        egress via _araw_fetch; mapping via the SAME pure-CPU _to_documents (byte-identical to search)."""
        return await self._asearch_via(
            query, limit,
            afetch=lambda: self._araw_fetch(query, limit),
            abuild=lambda raw: self._to_documents(raw, query, limit))

    def _study_to_doc(self, study: Any) -> Optional[Document]:
        if not isinstance(study, dict):
            return None
        ps = study.get("protocolSection") or {}
        idm = ps.get("identificationModule") or {}
        nct = idm.get("nctId")
        if not nct:
            return None  # no stable id ⇒ no doc

        stm = ps.get("statusModule") or {}
        dgm = ps.get("designModule") or {}
        cnm = ps.get("conditionsModule") or {}
        dcm = ps.get("descriptionModule") or {}
        spm = ps.get("sponsorCollaboratorsModule") or {}

        title = idm.get("briefTitle") or idm.get("officialTitle") or nct
        content = dcm.get("briefSummary") or dcm.get("detailedDescription") or ""
        status = stm.get("overallStatus")
        phases = dgm.get("phases") or []
        conditions = cnm.get("conditions") or []
        lead = (spm.get("leadSponsor") or {}).get("name")
        date = self._parse_date(stm.get("startDateStruct"))

        signals = self._enrollment_signal(dgm)

        return Document(
            source=self.name,
            source_id=nct,
            url=STUDY_URL.format(nct=nct),
            title=title,
            content=content,
            author=lead,
            date=date,
            signals=signals,
            tags=list(conditions),
            metadata={
                "status": status,
                "phases": phases,
                "conditions": conditions,
                "raw": jsonsafe(study),
            },
        )

    @staticmethod
    def _enrollment_signal(design_module: dict) -> dict:
        """Trial enrollment count → an engagement-class signal (the trial's scale).
        Absent on many records, so guarded; returns {} when there is no number."""
        enroll = (design_module.get("enrollmentInfo") or {}).get("count")
        if isinstance(enroll, (int, float)) and not isinstance(enroll, bool):
            return mk_signal(
                "enrollment", enroll, kind="engagement",
                by="clinicaltrials/enrollment", unit="participants",
            )
        return {}

    @staticmethod
    def _parse_date(date_struct: Any) -> Optional[datetime]:
        """Parse a v2 *DateStruct {"date": "YYYY", "YYYY-MM", or "YYYY-MM-DD"}.
        Pads partial dates to the first of the month/year. None on anything else."""
        if not isinstance(date_struct, dict):
            return None
        raw = date_struct.get("date")
        if not isinstance(raw, str) or not raw:
            return None
        for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        return None

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
