"""AI Incident Database (AIID) — catalogued real-world AI harms, via the keyless GraphQL API.

The AIID (responsible-ai-collaborative) is the canonical catalog of AI systems causing real harm:
1500+ curated incidents (wrongful arrests from facial-recognition, chatbot harms, autonomous-
vehicle deaths, biased deployments), each with the alleged developer/deployer, harmed parties, and
the source-report URLs. Penumbra's safety-STRUCTURE reinforcement: safety previously rested ONLY
on cset (Georgetown research/forecasts) — a DIFFERENT facet. AIID is the empirical harm ledger
(what went wrong, to whom, deployed by whom), structured data web search cannot return cleanly
(it returns news prose, never the cross-incident ledger with developer/deployer entities).

Access via the public read-only GraphQL endpoint (no key). It is ORIGIN-GATED: the site's own SPA
reads it with an ``Origin: https://incidentdatabase.ai`` header, so this adapter sends that header
to read the same public catalog (a public read endpoint, not an auth/credential bypass). A query
runs a case-insensitive REGEX over title+description; incidents are returned newest-first.

  POST https://incidentdatabase.ai/api/graphql   (header Origin: https://incidentdatabase.ai)
  query($f: IncidentFilterType){ incidents(filter:$f, sort:{incident_id:DESC},
        pagination:{limit:N}){ incident_id title date description
        AllegedDeveloperOfAISystem{name} AllegedDeployerOfAISystem{name}
        reports{report_number title url source_domain} } }
  filter = {OR: [{title:{REGEX:q, OPTIONS:"i"}}, {description:{REGEX:q, OPTIONS:"i"}}]}

The incident page is https://incidentdatabase.ai/cite/<incident_id>; the per-incident report URLs
are the drill-in handles (penumbra_add_url them for the underlying news source). backend="aiid".


"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from penumbra.core import http
from penumbra.core.normalize import Document, jsonsafe, mk_signal
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

API_URL = "https://incidentdatabase.ai/api/graphql"
CITE_URL = "https://incidentdatabase.ai/cite/{iid}"
# The site's own frontend origin; the read-only catalog API is gated to it. Sending it reads the
# same public data the SPA shows (no key, no login). If the gate ever tightens -> 403 -> [] (contract).
ORIGIN = "https://incidentdatabase.ai"

_GQL = ("query($f: IncidentFilterType){ incidents(filter: $f, sort: {incident_id: DESC}, "
        "pagination: {limit: %d}){ incident_id title date description "
        "AllegedDeveloperOfAISystem{ name } AllegedDeployerOfAISystem{ name } "
        "AllegedHarmedOrNearlyHarmedParties{ name } "
        "reports{ report_number title url source_domain } } }")


class AIIncidentsAdapter(BaseScrapeAdapter):
    name = "ai_incidents"
    backend = "aiid"
    needs_credentials = False
    description = ("AI Incident Database (AIID) — the curated ledger of real-world AI HARMS: search "
                   "incidents by keyword (facial recognition, chatbot, autonomous vehicle, biased "
                   "hiring) → each with the alleged developer + deployer, harmed parties, date, and "
                   "the source-report URLs to drill into. STRUCTURE, keyless; the empirical harm "
                   "record cset's research/forecasts don't give. Reach for AI-safety / "
                   "responsible-AI / deployment-risk questions.")
    cache_ttl = 43200  # 12h: the catalog accrues slowly (human-curated incidents)
    kind = "lookup"
    domains = ["safety"]
    modes = ["STRUCTURE"]

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        n = max(1, min(int(limit), 30))
        flt = self._filter(query)
        body = {"query": _GQL % n, "variables": {"f": flt}}
        return http.post_json(API_URL, json=body,
                              headers={"Origin": ORIGIN, "Referer": ORIGIN + "/"}, timeout=25)

    @staticmethod
    def _filter(query: str) -> dict:
        """Tokenize the query and AND the words: each (>1-char) word must appear (case-insensitive)
        in title OR description. Regexing the WHOLE query as one phrase requires it verbatim and
        almost always misses (the words are scattered across the title) — tokenized-AND is the
        keyword-search semantics. Mongo $regex specials are escaped so each word is literal text.
        A bare query -> {} (newest incidents)."""
        words = [re.escape(w) for w in (query or "").split() if len(w) > 1]
        if not words:
            return {}
        clauses = [{"OR": [{"title": {"REGEX": w, "OPTIONS": "i"}},
                           {"description": {"REGEX": w, "OPTIONS": "i"}}]} for w in words]
        return clauses[0] if len(clauses) == 1 else {"AND": clauses}

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, dict):
            return []
        incidents = ((raw.get("data") or {}).get("incidents")) or []
        docs: list[Document] = []
        for inc in incidents[:limit]:
            doc = self._incident_to_doc(inc)
            if doc is not None:
                docs.append(doc)
        return docs

    def _incident_to_doc(self, inc: Any) -> Optional[Document]:
        if not isinstance(inc, dict):
            return None
        iid = inc.get("incident_id")
        title = inc.get("title")
        if iid is None or not title:
            return None  # no id/title -> no canonical incident page
        devs = self._entities(inc.get("AllegedDeveloperOfAISystem"))
        deployers = self._entities(inc.get("AllegedDeployerOfAISystem"))
        harmed = self._entities(inc.get("AllegedHarmedOrNearlyHarmedParties"))
        reports = self._reports(inc.get("reports"))
        bits = [inc.get("description") or ""]
        if devs:
            bits.append("Developer: " + ", ".join(devs))
        if deployers:
            bits.append("Deployer: " + ", ".join(deployers))
        if harmed:
            bits.append("Harmed: " + ", ".join(harmed))
        content = "\n".join(b for b in bits if b).strip()
        return Document(
            source=self.name,
            source_id=str(iid),
            url=CITE_URL.format(iid=iid),
            title=title,
            content=content or title,
            author=", ".join(deployers or devs) or None,
            date=self._parse_date(inc.get("date")),
            signals=self._reports_signal(len(reports)),
            tags=[t for t in (devs + deployers) if t][:8] + ["ai-incident"],
            metadata={
                "incident_id": iid,
                "developers": devs,
                "deployers": deployers,
                "harmed_parties": harmed,
                # drill-in handles: the underlying source articles (penumbra_add_url them)
                "reports": reports,
                "raw": jsonsafe(inc),
            },
        )

    @staticmethod
    def _entities(ents: Any) -> list[str]:
        """A list of {name} Entity objects -> their names. Non-list / nameless -> dropped."""
        out: list[str] = []
        if not isinstance(ents, list):
            return out
        for e in ents:
            if isinstance(e, dict):
                nm = e.get("name")
                if isinstance(nm, str) and nm.strip():
                    out.append(nm.strip())
        return out

    @staticmethod
    def _reports(reports: Any) -> list[dict]:
        """Each report -> a compact {url, title, source_domain, report_number} drill-in handle."""
        out: list[dict] = []
        if not isinstance(reports, list):
            return out
        for r in reports:
            if isinstance(r, dict) and r.get("url"):
                out.append({"url": r.get("url"), "title": r.get("title"),
                            "source_domain": r.get("source_domain"),
                            "report_number": r.get("report_number")})
        return out

    @staticmethod
    def _reports_signal(n: int) -> dict:
        """Report count -> an engagement-class signal (corroboration weight). 0 -> {}."""
        if n > 0:
            return mk_signal("reports", n, kind="engagement", by="aiid/reports")
        return {}

    @staticmethod
    def _parse_date(ds: Any) -> Optional[datetime]:
        """date is an ISO 'YYYY-MM-DD' string. None on anything else."""
        if not isinstance(ds, str) or not ds.strip():
            return None
        try:
            return datetime.strptime(ds.strip()[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def health_check(self) -> tuple[bool, str]:
        try:
            raw = http.post_json(API_URL, json={"query": _GQL % 1, "variables": {"f": {}}},
                                 headers={"Origin": ORIGIN, "Referer": ORIGIN + "/"}, timeout=15)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"
        ok = isinstance(raw, dict) and isinstance(raw.get("data"), dict)
        return ok, "OK" if ok else "no data envelope"

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
