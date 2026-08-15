"""UK Companies House — the official company register: name search + officer / PSC drill.

Companies House is the UK's statutory registrar; its free Public Data API is the authoritative
source for every UK-incorporated company (name, number, status, incorporation date, registered
office) plus the people behind it (directors/officers and persons-with-significant-control, i.e.
beneficial owners). OmniSeek's UK filings-STRUCTURE source: resolve a company by name, then
drill its officers or its PSC ownership, which open web search cannot assemble structurally. The
UK sibling of sec_edgar (US filers) and uk_companies_house's own registry-of-record.

Auth: a FREE registered API key (https://developer.company-information.service.gov.uk/, no cost).
It rides as HTTP Basic with the key as the username and an EMPTY password (Basic base64(key + ':')).
The key lives ONLY on the host at ~/.omniseek/credentials/uk_companies_house.json ->
{"api_key": "..."}. needs_credentials=True; no key -> _raw_fetch returns None -> [] (the podcast_index
precedent: a missing credential degrades to empty, never a crash).

Query convention (a leading verb picks the endpoint; a bare query is a name search):
  "deepmind"            -> search/companies by name (the default)
  "officers:12345678"   -> list the officers of company 12345678
  "psc:12345678"        -> list the persons-with-significant-control of company 12345678
The company number is the 8-char CRN shown on every search result (its metadata['company_number']).

explicit_only: a named UK-registry drill (needs a company name or a CRN, is keyed, and the
officer / PSC endpoints return name-level PII) — not broad-fan-out fodder. STRUCTURE mode.

This is a coded BaseAPIAdapter subclass: the base supplies search / fetch_url caching + registration;
this file supplies the two hooks (verb-routed _raw_fetch, kind-branched _to_document), rank_locally
=False (the search endpoint ranks server-side and the drills have no query terms to score against),
and a credential-aware health_check. No module-tail register call (BaseAPIAdapter auto-registers).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from omniseek.core import auth, http
from omniseek.core.normalize import Document, jsonsafe
from omniseek.core.sources.api._base import BaseAPIAdapter

logger = logging.getLogger(__name__)

API = "https://api.company-information.service.gov.uk"
# The human-browsable front end (find-and-update) mirrors the API by CRN, so a doc's url is a
# real page a person / a vision agent can open, not the JSON endpoint.
WEB = "https://find-and-update.company-information.service.gov.uk"

# Drop a credential template on first import (free key from the developer portal).
auth.write_template(
    "uk_companies_house",
    {"_comment": "FREE API key from https://developer.company-information.service.gov.uk/ "
                 "(register an application, create a 'REST' / Live key). Sent as HTTP Basic "
                 "with the key as username and an empty password.",
     "api_key": ""},
)


def _parse_date(raw: Any) -> Optional[datetime]:
    """Companies House dates are 'YYYY-MM-DD' (e.g. date_of_creation, appointed_on, notified_on).
    None on anything else (never a fabricated date)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.strptime(raw.strip()[:10], "%Y-%m-%d")
    except ValueError:
        return None


class UkCompaniesHouseAdapter(BaseAPIAdapter):
    name = "uk_companies_house"
    needs_credentials = True
    description = (
        "UK Companies House — the official company register (free key). Name search returns UK "
        "companies (number/status/type/incorporation date/registered office); drill a company by "
        "CRN with 'officers:12345678' (directors/secretaries) or 'psc:12345678' (beneficial owners, "
        "persons with significant control). filings, UK. STRUCTURE, keyed; name it to drill."
    )
    cache_ttl = 21600  # 6h: register data moves slowly; a name/CRN lookup is stable within a day
    rank_locally = False  # search/companies ranks server-side; drills carry no scorable query terms
    url_host = "company-information.service.gov.uk"
    kind = "lookup"
    domains = ["filings"]
    regions = ["uk"]
    modes = ["STRUCTURE"]
    explicit_only = ("uk_companies_house: a named UK-registry drill (needs a company name or CRN, "
                     "keyed, officer / PSC endpoints return name-level PII); not broad-fan-out fodder")

    # ------------------------------------------------------------------ auth
    @staticmethod
    def _api_key() -> Optional[str]:
        return (auth.load("uk_companies_house") or {}).get("api_key") or None

    def _get(self, path: str, params: Optional[dict] = None) -> Optional[Any]:
        """GET an API path with the key as HTTP Basic username (empty password). None on any
        failure (the shared http helper's contract) or if the host has no key."""
        key = self._api_key()
        if not key:
            return None
        return http.get_json(f"{API}{path}", params=params or {}, auth=(key, ""), timeout=20)

    async def _aget(self, path: str, params: Optional[dict] = None) -> Optional[Any]:
        """Async twin of _get: BYTE-FAITHFUL mirror — same URL / params / HTTP-Basic auth / timeout,
        same no-key -> None contract; ONLY the shared-http egress swaps http.get_json -> await
        http.aget_json (same **kwargs, so auth=(key, "") rides identically)."""
        key = self._api_key()
        if not key:
            return None
        return await http.aget_json(f"{API}{path}", params=params or {}, auth=(key, ""), timeout=20)

    # --------------------------------------------------------------- routing
    @staticmethod
    def _route(query: str) -> tuple[str, str]:
        """(verb, arg): 'officers:CRN' / 'psc:CRN' pick a drill; anything else is a name search.
        The verb may use ':' or whitespace ('officers 12345678'); arg is stripped."""
        q = (query or "").strip()
        low = q.lower()
        for verb in ("officers", "psc"):
            if low.startswith(verb + ":") or low.startswith(verb + " "):
                return verb, q[len(verb) + 1:].strip()
        return "search", q

    def _raw_fetch(self, query: str, limit: int) -> Optional[list]:
        """Verb-route to one of three endpoints; tag each record with its kind + parent CRN so the
        single-record _to_document hook can branch. Returns [] on no key / empty result / failure."""
        verb, arg = self._route(query)
        n = max(1, min(int(limit), 50))
        if verb == "officers":
            if not arg:
                return []
            data = self._get(f"/company/{arg}/officers", {"items_per_page": n})
            items = (data or {}).get("items") or []
            return [{"_ch": "officer", "_crn": arg, **it} for it in items if isinstance(it, dict)]
        if verb == "psc":
            if not arg:
                return []
            data = self._get(f"/company/{arg}/persons-with-significant-control", {"items_per_page": n})
            items = (data or {}).get("items") or []
            return [{"_ch": "psc", "_crn": arg, **it} for it in items if isinstance(it, dict)]
        # default: company name search
        if not arg:
            return []
        data = self._get("/search/companies", {"q": arg, "items_per_page": n})
        items = (data or {}).get("items") or []
        return [{"_ch": "company", **it} for it in items if isinstance(it, dict)]

    async def _araw_fetch(self, query: str, limit: int) -> Optional[list]:
        """Async twin of _raw_fetch: BYTE-FAITHFUL mirror — same verb routing, same clamp, same
        endpoints / params, same kind+CRN record tagging, same None/[] contract. ONLY the shared-http
        egress is mirrored (self._get -> await self._aget, i.e. http.get_json -> await http.aget_json);
        control flow is unchanged (exactly one keyed GET per branch, as in the sync path)."""
        verb, arg = self._route(query)
        n = max(1, min(int(limit), 50))
        if verb == "officers":
            if not arg:
                return []
            data = await self._aget(f"/company/{arg}/officers", {"items_per_page": n})
            items = (data or {}).get("items") or []
            return [{"_ch": "officer", "_crn": arg, **it} for it in items if isinstance(it, dict)]
        if verb == "psc":
            if not arg:
                return []
            data = await self._aget(f"/company/{arg}/persons-with-significant-control", {"items_per_page": n})
            items = (data or {}).get("items") or []
            return [{"_ch": "psc", "_crn": arg, **it} for it in items if isinstance(it, dict)]
        # default: company name search
        if not arg:
            return []
        data = await self._aget("/search/companies", {"q": arg, "items_per_page": n})
        items = (data or {}).get("items") or []
        return [{"_ch": "company", **it} for it in items if isinstance(it, dict)]

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of search -> AsyncSearchCapable. Shares the base async cache round-trip
        (_aapi_search: same cache key, per-record _to_document, rank_locally, cache-only-if-docs);
        egress via _araw_fetch; mapping via the SAME pure-CPU _to_document (byte-identical to search)."""
        return await self._aapi_search(query, limit, araw_fetch=lambda: self._araw_fetch(query, limit))

    def _to_document(self, raw: Any) -> Optional[Document]:
        if not isinstance(raw, dict):
            return None
        ch = raw.get("_ch")
        if ch == "officer":
            return self._officer_doc(raw)
        if ch == "psc":
            return self._psc_doc(raw)
        return self._company_doc(raw)

    # --------------------------------------------------------------- mappers
    def _company_doc(self, c: dict) -> Optional[Document]:
        crn = c.get("company_number")
        title = c.get("title")
        if not crn or not title:
            return None  # no CRN / name -> unusable
        status = c.get("company_status")
        ctype = c.get("company_type")
        addr = c.get("address_snippet")
        created = c.get("date_of_creation")
        content = "\n".join(p for p in [
            f"Company: {title}  (no. {crn})",
            f"Status: {status}" if status else "",
            f"Type: {ctype}" if ctype else "",
            f"Incorporated: {created}" if created else "",
            f"Registered office: {addr}" if addr else "",
        ] if p)
        return Document(
            source=self.name,
            source_id=str(crn),
            url=f"{WEB}/company/{crn}",
            title=f"{title} ({crn})",
            content=content,
            date=_parse_date(created),
            tags=[t for t in (status, ctype, "uk") if t],
            metadata={
                "company_number": crn,
                "company_status": status,
                "company_type": ctype,
                "date_of_creation": created,
                "address_snippet": addr,
                "address": c.get("address"),
                "raw": jsonsafe(c),
            },
        )

    def _officer_doc(self, o: dict) -> Optional[Document]:
        crn = o.get("_crn")
        name = o.get("name")
        if not crn or not name:
            return None
        role = o.get("officer_role")
        appointed = o.get("appointed_on")
        resigned = o.get("resigned_on")
        # The officer's stable id is embedded in the appointments link: /officers/{id}/appointments.
        appts = ((o.get("links") or {}).get("officer") or {}).get("appointments") \
            if isinstance(o.get("links"), dict) else None
        oid = appts.strip("/").split("/")[1] if isinstance(appts, str) and "/officers/" in appts else None
        url = f"{WEB}/officers/{oid}/appointments" if oid else f"{WEB}/company/{crn}/officers"
        dob = o.get("date_of_birth") if isinstance(o.get("date_of_birth"), dict) else None
        content = "\n".join(p for p in [
            f"Officer: {name}",
            f"Role: {role}" if role else "",
            f"Company: {crn}",
            f"Appointed: {appointed}" if appointed else "",
            f"Resigned: {resigned}" if resigned else "",
            f"Nationality: {o.get('nationality')}" if o.get("nationality") else "",
            f"Occupation: {o.get('occupation')}" if o.get("occupation") else "",
        ] if p)
        return Document(
            source=self.name,
            source_id=f"{crn}:officer:{oid or name}",
            url=url,
            title=f"{name} - {role or 'officer'} @ {crn}",
            content=content,
            author=name,
            date=_parse_date(appointed),
            tags=[t for t in (role, o.get("nationality"), "uk") if t],
            metadata={
                "company_number": crn,
                "officer_id": oid,
                "officer_role": role,
                "appointed_on": appointed,
                "resigned_on": resigned,
                "nationality": o.get("nationality"),
                "occupation": o.get("occupation"),
                "country_of_residence": o.get("country_of_residence"),
                "date_of_birth": dob,  # month + year only (CH redacts the day)
                "address": o.get("address"),
                "raw": jsonsafe(o),
            },
        )

    def _psc_doc(self, p: dict) -> Optional[Document]:
        crn = p.get("_crn")
        name = p.get("name")
        if not crn or not name:
            return None
        natures = p.get("natures_of_control") if isinstance(p.get("natures_of_control"), list) else []
        notified = p.get("notified_on")
        ceased = p.get("ceased_on")
        pkind = p.get("kind")  # e.g. individual-person-with-significant-control
        self_link = (p.get("links") or {}).get("self") if isinstance(p.get("links"), dict) else None
        url = f"{WEB}{self_link}" if isinstance(self_link, str) and self_link.startswith("/") \
            else f"{WEB}/company/{crn}/persons-with-significant-control"
        dob = p.get("date_of_birth") if isinstance(p.get("date_of_birth"), dict) else None
        content = "\n".join(part for part in [
            f"Person with significant control: {name}",
            f"Of company: {crn}",
            f"Kind: {pkind}" if pkind else "",
            ("Natures of control: " + ", ".join(str(x) for x in natures)) if natures else "",
            f"Notified: {notified}" if notified else "",
            f"Ceased: {ceased}" if ceased else "",
            f"Nationality: {p.get('nationality')}" if p.get("nationality") else "",
        ] if part)
        return Document(
            source=self.name,
            source_id=f"{crn}:psc:{name}:{notified or ''}",
            url=url,
            title=f"{name} - PSC of {crn}",
            content=content,
            author=name,
            date=_parse_date(notified),
            tags=[t for t in ([pkind] + list(natures) + ["uk"]) if t],
            metadata={
                "company_number": crn,
                "psc_kind": pkind,
                "natures_of_control": natures,
                "notified_on": notified,
                "ceased_on": ceased,
                "nationality": p.get("nationality"),
                "country_of_residence": p.get("country_of_residence"),
                "date_of_birth": dob,
                "address": p.get("address"),
                "raw": jsonsafe(p),
            },
        )

    # ------------------------------------------------------------- health
    def health_check(self) -> tuple[bool, str]:
        """Liveness: a tiny keyed name search. Without a key, report unconfigured (the endpoint is
        fine, the host just has no credential yet) rather than failing the source outright."""
        if not self._api_key():
            return False, "no api_key (set ~/.omniseek/credentials/uk_companies_house.json)"
        try:
            data = self._get("/search/companies", {"q": "tesco", "items_per_page": 1})
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"
        ok = isinstance(data, dict) and "items" in data
        return ok, "OK" if ok else "no items envelope"

# Registration is automatic via BaseAPIAdapter.__init_subclass__ (no module-tail ceremony).
