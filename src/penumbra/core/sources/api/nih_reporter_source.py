"""NIH RePORTER — US biomedical research grants via the keyless v2 API (POST).

NIH RePORTER is the authoritative record of NIH-funded biomedical research: the
project, the contact PI, the awardee organization, the fiscal-year award amount,
and the full project abstract + MeSH-ish terms. Penumbra's US biomedical
funding STRUCTURE source — filterable grant records (by topic / PI / organization)
that the open web cannot hand back as data, and the biomedical sibling of
nsf_awards (which covers NSF, not NIH).

Access via the public v2 API (no auth, no key) — note it is a POST, not a GET:
  POST https://api.reporter.nih.gov/v2/projects/search
  body = {"criteria": {"advanced_text_search": {"operator": "and",
            "search_field": "projecttitle,abstracttext,terms",
            "search_text": <query>}},
          "include_fields": [...], "offset": 0, "limit": <limit, capped 500>}
Response: {"results": [{project_num, project_title, fiscal_year, award_amount,
  organization: {org_name, org_country}, principal_investigators: [{full_name,
  first_name, last_name, is_contact_pi, profile_id}], agency_ic_admin: {name},
  abstract_text, terms, project_start_date, project_end_date, appl_id}, ...],
  "meta": {...}}
The project page URL is CONSTRUCTED from appl_id (no url field in the payload),
which is why this is a thin coded adapter rather than a declarative config row.

A thin BaseScrapeAdapter subclass: the cache check / atomic set_docs / self-
registration ritual lives in the base; this declares its facets and fills the two
hooks. rank stays default-False (the endpoint returns its own relevance order for
advanced_text_search). explicit_only: US-only federal biomedical grants are a
named, deliberate drill (by topic / PI / organization), not broad-fan-out fodder.

Recon trail: the sandbox DNS-blackholes api.reporter.nih.gov, so field names are
from the spec/probe handed to the source-builder; the eye host live-verifies the
POST after deploy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from penumbra.core import http
from penumbra.core.normalize import Document, jsonsafe, mk_signal
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

API_URL = "https://api.reporter.nih.gov/v2/projects/search"
PROJECT_URL = "https://reporter.nih.gov/project-details/{appl_id}"
PROJNUM_URL = "https://reporter.nih.gov/search/?projnum={project_num}"
_MAX_LIMIT = 500  # the API caps `limit` at 500; asking for more is rejected
class NIHReporterAdapter(BaseScrapeAdapter):
    name = "nih_reporter"
    needs_credentials = False
    description = ("NIH RePORTER — US NIH biomedical research grants (contact PI / award "
                   "amount / awardee organization / full abstract + terms); name it to drill "
                   "US federal biomedical funding by topic / PI / organization. STRUCTURE, "
                   "keyless POST api.reporter.nih.gov; the biomedical sibling of nsf_awards.")
    cache_ttl = 21600  # 6h: grant records change slowly
    kind = "lookup"
    domains = ["funding"]
    regions = ["us"]
    modes = ["STRUCTURE"]
    explicit_only = ("nih_reporter: US-only federal biomedical grants — a named drill "
                     "(by topic / PI / organization), not broad-fan-out fodder")

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        # limit is clamped to the API's 500 ceiling (>=1); offset fixed at the first page.
        # No include_fields: NIH returns the full project record by default. Requesting an
        # explicit field set risks a name mismatch that silently yields 0 results (live-caught
        # 2026-06-21: snake_case include_fields → 200 with empty results); the working public
        # examples all omit it. search_field names are lowercase (projecttitle/abstracttext/terms).
        body = {
            "criteria": {
                "advanced_text_search": {
                    "operator": "and",
                    "search_field": "projecttitle,abstracttext,terms",
                    "search_text": query,
                },
            },
            "offset": 0,
            "limit": max(1, min(int(limit), _MAX_LIMIT)),
        }
        return http.post_json(API_URL, json=body, timeout=20)

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, dict):
            return []
        results = raw.get("results") or []
        docs: list[Document] = []
        for proj in results[:limit]:
            doc = self._project_to_doc(proj)
            if doc is not None:
                docs.append(doc)
        return docs

    def _project_to_doc(self, proj: Any) -> Optional[Document]:
        if not isinstance(proj, dict):
            return None
        appl_id = proj.get("appl_id")
        project_num = proj.get("project_num")
        # Need a stable id: appl_id is the canonical detail-page key; project_num is the fallback.
        sid = appl_id or project_num
        if not sid:
            return None  # no stable id ⇒ no doc

        title = proj.get("project_title") or project_num or str(sid)
        url = (PROJECT_URL.format(appl_id=appl_id) if appl_id
               else PROJNUM_URL.format(project_num=project_num))

        org = proj.get("organization") if isinstance(proj.get("organization"), dict) else {}
        org_name = org.get("org_name")
        agency = proj.get("agency_ic_admin") if isinstance(proj.get("agency_ic_admin"), dict) else {}

        return Document(
            source=self.name,
            source_id=str(sid),
            url=url,
            title=title,
            content=proj.get("abstract_text") or title,
            author=self._contact_pi(proj.get("principal_investigators")),
            date=self._parse_date(proj.get("project_start_date")),
            signals=self._amount_signal(proj.get("award_amount")),
            tags=[t for t in (org_name,) if t],
            metadata={
                "project_num": project_num,
                "fiscal_year": proj.get("fiscal_year"),
                "org_name": org_name,
                "org_country": org.get("org_country"),
                "agency": agency.get("name"),
                "terms": proj.get("terms"),
                "project_end_date": proj.get("project_end_date"),
                "raw": jsonsafe(proj),
            },
        )

    @staticmethod
    def _contact_pi(pis: Any) -> Optional[str]:
        """Pick the contact PI's full_name (is_contact_pi True); else the first PI's name.
        Tolerates a missing / non-list / malformed PI array ⇒ None (never raises)."""
        if not isinstance(pis, list) or not pis:
            return None
        first_name: Optional[str] = None
        for pi in pis:
            if not isinstance(pi, dict):
                continue
            name = pi.get("full_name") or _join_name(pi)
            if first_name is None and name:
                first_name = name
            if pi.get("is_contact_pi") and name:
                return name
        return first_name

    @staticmethod
    def _amount_signal(amount: Any) -> dict:
        """Award dollar amount → an engagement-class signal (the grant's scale).
        Absent / non-numeric ⇒ {} (never raises)."""
        try:
            val = float(amount)
        except (TypeError, ValueError):
            return {}
        return mk_signal("award_amount", val, kind="engagement",
                         by="nih_reporter/award_amount", unit="USD")

    @staticmethod
    def _parse_date(raw: Any) -> Optional[datetime]:
        """NIH RePORTER dates are ISO-8601, sometimes with a time/zone tail
        (e.g. '2024-09-01' or '2024-09-01T00:00:00Z'). None on anything else."""
        if not isinstance(raw, str) or not raw.strip():
            return None
        s = raw.strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(s.replace("Z", "+0000"), fmt)
            except ValueError:
                continue
        # Last resort: take the leading YYYY-MM-DD prefix if present.
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None


def _join_name(pi: dict) -> Optional[str]:
    """Build a name from first_name + last_name when full_name is absent."""
    parts = [str(pi.get(k)).strip() for k in ("first_name", "last_name") if pi.get(k)]
    return " ".join(parts) if parts else None

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
