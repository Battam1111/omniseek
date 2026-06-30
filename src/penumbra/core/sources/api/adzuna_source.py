"""Adzuna — multi-country job listings + employer salary ranges via the keyed public API.

Adzuna aggregates job postings across 20+ countries and is one of the few sources that
returns a structured employer-side salary range per listing (salary_min / salary_max).
Penumbra's careers-STRUCTURE source: a filterable, salaried job feed across many markets
(the default is the deployer-configured country; any supported country code can be passed),
which web search cannot assemble. Fills the "cross-country jobs + salary" gap (MyCareersFuture
is SG-only, levels.fyi is self-reported tech comp).

Auth: a FREE registered app_id + app_key (https://developer.adzuna.com/signup, no cost;
free tier ~250 calls/day). They live ONLY on the host at ~/.penumbra/credentials/adzuna.json
-> {"app_id": "...", "app_key": "..."} and ride as URL params (no auth header), so this uses
the shared http helper. needs_credentials=True; no key -> _raw_fetch returns None -> [].

Query convention: an optional leading 2-letter Adzuna country code picks the market, e.g.
"ca machine learning" (Canada), "sg data scientist" (Singapore); a bare query defaults to
Canada. Supported: gb us ca au sg de fr in it nl es pl nz br mx at za.

A thin BaseScrapeAdapter subclass. explicit_only: a named market drill (country + role), not
broad-fan-out fodder (it needs a country and burns the keyed daily quota).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from penumbra.core import auth, http
from penumbra.core.normalize import Document, jsonsafe, mk_signal
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

API_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"
_COUNTRIES = {"gb", "us", "ca", "au", "sg", "de", "fr", "in", "it", "nl",
              "es", "pl", "nz", "br", "mx", "at", "za"}
_DEFAULT_COUNTRY = "ca"  # the deployer's default job market (configurable)

# Drop a credential template on first import (free key from developer.adzuna.com/signup).
auth.write_template(
    "adzuna",
    {"_comment": "FREE app_id + app_key from https://developer.adzuna.com/signup (no cost, "
                 "~250 calls/day). BOTH are required; sent as URL params.",
     "app_id": "", "app_key": ""},
)


class AdzunaAdapter(BaseScrapeAdapter):
    name = "adzuna"
    needs_credentials = True
    description = ("Adzuna — multi-country job listings with employer SALARY ranges "
                   "(salary_min/max + company / location / contract); name it to drill a job "
                   "market. Query = optional 2-letter country code + role, e.g. 'ca machine "
                   "learning' / 'sg data scientist' (defaults to Canada). STRUCTURE, keyed.")
    cache_ttl = 3600
    kind = "lookup"
    domains = ["jobs"]
    modes = ["STRUCTURE"]
    explicit_only = ("adzuna: a named job-market drill (country + role); not broad-fan-out "
                     "fodder (it needs a country and burns the keyed daily quota)")

    @staticmethod
    def _creds() -> tuple[Optional[str], Optional[str]]:
        c = auth.load("adzuna") or {}
        return (c.get("app_id") or None), (c.get("app_key") or None)

    @staticmethod
    def _split_country(query: str) -> tuple[str, str]:
        """Optional leading 2-letter Adzuna country code -> (country, role); a bare query
        defaults to Canada."""
        parts = (query or "").strip().split(None, 1)
        if parts and parts[0].lower() in _COUNTRIES:
            return parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        return _DEFAULT_COUNTRY, (query or "").strip()

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        app_id, app_key = self._creds()
        if not app_id or not app_key:
            return None  # no key -> [] (the contract); the template names where to put it
        country, what = self._split_country(query)
        return http.get_json(
            API_URL.format(country=country),
            params={"app_id": app_id, "app_key": app_key, "what": what,
                    "results_per_page": max(1, min(int(limit), 50))},
            timeout=20,
        )

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, dict):
            return []
        results = raw.get("results") or []
        docs: list[Document] = []
        for job in results[:limit]:
            doc = self._job_to_doc(job)
            if doc is not None:
                docs.append(doc)
        return docs

    def _job_to_doc(self, job: Any) -> Optional[Document]:
        if not isinstance(job, dict):
            return None
        url = job.get("redirect_url")
        title = job.get("title")
        if not url or not title:
            return None  # no canonical URL / title -> unusable
        jid = job.get("id")
        company = (job.get("company") or {}).get("display_name") if isinstance(job.get("company"), dict) else None
        loc = (job.get("location") or {}).get("display_name") if isinstance(job.get("location"), dict) else None
        cat = (job.get("category") or {}).get("label") if isinstance(job.get("category"), dict) else None
        return Document(
            source=self.name,
            source_id=str(jid) if jid else url,
            url=url,
            title=title,
            content=job.get("description") or title,
            author=company,
            date=self._parse_date(job.get("created")),
            signals=self._salary_signal(job),
            tags=[t for t in (loc, cat) if t],
            metadata={
                "company": company,
                "location": loc,
                "salary_min": job.get("salary_min"),
                "salary_max": job.get("salary_max"),
                "salary_is_predicted": job.get("salary_is_predicted"),
                "contract_type": job.get("contract_type"),
                "contract_time": job.get("contract_time"),
                "raw": jsonsafe(job),
            },
        )

    @staticmethod
    def _salary_signal(job: dict) -> dict:
        """Employer salary midpoint -> an engagement-class signal (the listing's pay scale).
        Absent / non-numeric -> {} (never a fabricated zero)."""
        vals = [v for v in (job.get("salary_min"), job.get("salary_max"))
                if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if not vals:
            return {}
        return mk_signal("salary", sum(vals) / len(vals), kind="engagement",
                         by="adzuna/salary", unit=None)

    @staticmethod
    def _parse_date(raw: Any) -> Optional[datetime]:
        """Adzuna 'created' is ISO-8601 (e.g. 2026-06-01T12:00:00Z). None on anything else."""
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            return datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            try:
                return datetime.strptime(raw.strip()[:19], "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                return None

    def health_check(self) -> tuple[bool, str]:
        """Liveness: a tiny keyed query. Without a key, report unconfigured (the endpoint
        itself is fine, the host just has no credential yet)."""
        app_id, app_key = self._creds()
        if not app_id or not app_key:
            return False, "no app_id/app_key (set ~/.penumbra/credentials/adzuna.json)"
        try:
            raw = http.get_json(API_URL.format(country=_DEFAULT_COUNTRY),
                                params={"app_id": app_id, "app_key": app_key,
                                        "what": "engineer", "results_per_page": 1}, timeout=15)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"
        ok = isinstance(raw, dict) and "results" in raw
        return ok, "OK" if ok else "no results envelope"

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
