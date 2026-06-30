"""SEC EDGAR full-text filing search: US public-company disclosures, keyless (UA-gated).

The U.S. SEC's EDGAR system holds every public-company filing: 10-K / 10-Q annual and
quarterly reports, 8-K material events, DEF 14A proxy statements (the canonical source
for executive compensation), S-1 registrations, 6-K foreign reports, and the rest. The
full-text search backend indexes the body text of filings since 2001, so Penumbra can find
WHICH companies disclosed a given term (a risk factor, a product, a person, a phrase) and
hand back the specific filing.

Access via the keyless EDGAR full-text search backend (the JSON API behind efts.sec.gov):

    GET https://efts.sec.gov/LATEST/search-index?q=<query>

SEC REQUIRES a descriptive User-Agent identifying the requester, or it returns 403. We send
one explicitly via the shared http helper's ``headers=`` pass-through (the default Penumbra
UA is fine for most sources, but SEC wants a contact, so we override it here).

Response shape (verified live 2026-06-17): ``{"hits": {"total": {...}, "hits": [<hit>, ...]}}``
where each hit is ``{"_id": "<adsh>:<filename>", "_score": <float>, "_source": {...}}`` and the
``_source`` carries: ``form`` (filing type, e.g. "DEF 14A"), ``display_names`` (list like
``"USA TRUCK INC  (CIK 0000883945)"`` (the filer name with its CIK/ticker in trailing parens),
``ciks`` (list of zero-padded CIK strings), ``adsh`` (the accession number, dashed), ``file_date``
(``YYYY-MM-DD``), plus ``file_type`` / ``file_description`` / ``biz_locations`` / ``sics``.

NOTE: this backend returns NO highlight/snippet field (verified: ``_source`` has no ``highlight``
key, and there is no top-level highlight on the hit). So ``content`` is composed from the rich
hit metadata (company, form, description, location, date) plus the direct filing link. The filing
INDEX url is reconstructed from the CIK + accession number and resolves directly (verified HTTP 200):
``https://www.sec.gov/Archives/edgar/data/<cik>/<adsh-no-dashes>/<adsh>-index.htm``; if the pieces
are missing we fall back to the company's browse-edgar page.

BaseScrapeAdapter (template method): the cache check / atomic set_docs / self-registration ritual
lives in the base; this adapter declares its facets and fills the two hooks. ``rank`` stays
default-False (no BM25 re-score): the backend returns descending ``_score`` and we then reorder
the page by recency in ``_to_documents`` (newest filing first), so the shared lexical scorer is
deliberately NOT in this path. Recency is a deliberate bias here because a SEC full-text match for
a company/term overwhelmingly wants the CURRENT disclosure, not a decades-old exhibit that happens
to score high; see ``_to_documents`` for the live-probed reason it is done client-side.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Optional

from penumbra.core import auth, http
from penumbra.core.normalize import Document, jsonsafe, mk_signal
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

API_URL = "https://efts.sec.gov/LATEST/search-index"
# SEC's fair-access policy: identify the requester with a contact. Without a descriptive
# User-Agent the endpoint 403s (see module docstring). The shared http helper merges this
# over its default UA via the headers= pass-through. Contact is host-injected, never a
# hardcoded personal address (see auth.contact_email).
SEC_UA = f"penumbra research {auth.contact_email()}"
TIMEOUT = 15

# A display_name looks like "USA TRUCK INC  (CIK 0000883945)" or
# "PRICE T ROWE GROUP INC  (TROW)  (CIK 0001113169)": strip the trailing parenthetical(s)
# (the CIK and/or ticker) to recover the clean company name.
_TRAILING_PARENS = re.compile(r"\s*\([^)]*\)\s*$")


class SECEdgarAdapter(BaseScrapeAdapter):
    name = "sec_edgar"
    needs_credentials = False
    description = "SEC EDGAR full-text filing search: US public-company disclosures (10-K/8-K/DEF 14A proxy/etc.), keyless UA-gated"
    cache_ttl = 900

    # routing facets (the router reads these class attrs; do NOT touch facets.json)
    kind = "lookup"
    domains = ["filings", "compensation"]
    modes = ["STRUCTURE"]

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        # The backend ignores a page-size param (returns up to 100 hits per page); we slice
        # to ``limit`` in _to_documents. UA override is mandatory or SEC 403s.
        return http.get_json(
            API_URL,
            params={"q": query},
            headers={"User-Agent": SEC_UA},
            timeout=TIMEOUT,
        )

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, dict):
            return []
        hits = (raw.get("hits") or {}).get("hits") or []
        # RECENCY SORT (client-side, latest-first). The efts backend returns hits in
        # _score (relevance) order, which floats decades-old filings (a 2001 EX-21
        # exhibit) above the current 10-Q/8-K for a company-name query. Probed live
        # 2026-06-17: this backend REJECTS the server-side levers that would fix it at
        # the source -- `forms=` -> HTTP 500, `sort=date` -> HTTP 500; even `startdt`/
        # `enddt` (which it DOES accept) only windows the set, it does not reorder it.
        # So the only mechanism that improves recency WITHOUT an unverified param AND
        # without breaking general full-text search (this is a universal full-text
        # endpoint, not a per-company API) is to reorder the page here: newest
        # file_date first, undated hits last, ties keep the backend's relevance order
        # (Python's sort is stable). Same recency-bias shape as the zenodo fix.
        hits = _sort_by_recency(hits)
        docs: list[Document] = []
        for hit in hits[:limit]:
            try:
                doc = self._hit_to_document(hit)
            except Exception as exc:  # noqa: BLE001 (a malformed hit is skipped, not fatal)
                logger.debug("Skipping malformed SEC EDGAR hit: %s", exc)
                continue
            if doc is not None:
                docs.append(doc)
        return docs

    def _hit_to_document(self, hit: dict) -> Optional[Document]:
        if not isinstance(hit, dict):
            return None
        src = hit.get("_source") or {}
        if not isinstance(src, dict):
            return None

        form = (src.get("form") or "").strip()
        company = _company_name(src.get("display_names"))
        if not form and not company:
            return None  # nothing identifiable

        # _id is "<adsh>:<filename>" (the filename half is the specific document inside the filing).
        hit_id = str(hit.get("_id") or "")
        adsh = (src.get("adsh") or "").strip()
        filename = ""
        if not adsh and ":" in hit_id:
            adsh = hit_id.split(":", 1)[0]
        if ":" in hit_id:
            filename = hit_id.split(":", 1)[1]

        ciks = [c for c in (src.get("ciks") or []) if isinstance(c, str) and c]
        cik = ciks[0] if ciks else ""

        url = _filing_url(cik, adsh)

        date = _parse_date(src.get("file_date"))

        title = ": ".join(p for p in (form, company) if p) or (hit_id or self.name)

        # No highlight/snippet exists on this backend (see module docstring), so compose content
        # from the rich hit metadata: the description, filer location, form type, and the link.
        descr = (src.get("file_description") or "").strip()
        locations = [loc for loc in (src.get("biz_locations") or []) if isinstance(loc, str) and loc]
        all_filers = [_company_name([n]) for n in (src.get("display_names") or []) if isinstance(n, str)]
        all_filers = [n for n in all_filers if n]

        content_lines: list[str] = []
        if form:
            content_lines.append(f"Filing type: {form}")
        if all_filers:
            content_lines.append("Filer(s): " + "; ".join(all_filers))
        if descr:
            content_lines.append(f"Description: {descr}")
        if locations:
            content_lines.append("Business location(s): " + "; ".join(locations))
        if date is not None:
            content_lines.append(f"Filed: {date.date().isoformat()}")
        if cik:
            content_lines.append(f"CIK: {cik}")
        if adsh:
            content_lines.append(f"Accession: {adsh}")
        content_lines.append(
            "SEC EDGAR full-text search match. This backend returns no body snippet; open the "
            "filing index below to read the document text (the specific matched document is "
            + (f"'{filename}'." if filename else "listed on the index.")
        )
        content_lines.append(f"Filing index: {url}")
        content = "\n".join(content_lines)

        # _score is the backend's full-text relevance, a source-reported number (not engagement,
        # not a citation): record it as an 'other'-kind signal for transparency.
        signals = mk_signal(
            "relevance_score", hit.get("_score"),
            kind="other", by="sec_edgar/_score",
        )

        return Document(
            source=self.name,
            source_id=hit_id or adsh or (cik or title),
            url=url,
            title=title,
            content=content,
            author=company or None,
            date=date,
            signals=signals,
            tags=[t for t in (form,) if t],
            metadata={
                "form": form or None,
                "cik": cik or None,
                "ciks": ciks or None,
                "company": company or None,
                "adsh": adsh or None,
                "filename": filename or None,
                "file_type": src.get("file_type"),
                "sics": src.get("sics"),
                "raw": jsonsafe(hit),
            },
        )

    def health_check(self) -> tuple[bool, str]:
        # Override the base probe so the trivial liveness query carries the mandatory SEC UA
        # (the base _raw_fetch already does, but be explicit about the failure contract here).
        try:
            raw = self._raw_fetch("test", 1)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"
        if not isinstance(raw, dict) or "hits" not in raw:
            return False, "no hits envelope (UA gating or endpoint change?)"
        return True, "OK"


def _company_name(display_names: Any) -> str:
    """Recover the clean company name from a display_names list entry like
    ``"USA TRUCK INC  (CIK 0000883945)"`` by stripping trailing parenthetical(s)."""
    if isinstance(display_names, list) and display_names:
        first = display_names[0]
    elif isinstance(display_names, str):
        first = display_names
    else:
        return ""
    if not isinstance(first, str):
        return ""
    name = first.strip()
    # Strip trailing parentheticals repeatedly (some entries have both a ticker and a CIK).
    prev = None
    while name and name != prev:
        prev = name
        name = _TRAILING_PARENS.sub("", name).strip()
    return name


def _filing_url(cik: str, adsh: str) -> str:
    """Build the direct filing-index URL (verified to resolve, HTTP 200):
    ``https://www.sec.gov/Archives/edgar/data/<cik>/<adsh-no-dashes>/<adsh>-index.htm``.
    Falls back to the company's browse-edgar page (by CIK) when the accession is missing,
    or to the generic EDGAR search when even the CIK is unavailable."""
    cik_num = cik.lstrip("0") or cik  # the Archives path wants the un-padded CIK
    if cik_num and adsh:
        adsh_nodash = adsh.replace("-", "")
        return (
            f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{adsh_nodash}/{adsh}-index.htm"
        )
    if cik_num:
        return (
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&CIK={cik_num}&type=&dateb=&owner=include&count=40"
        )
    return "https://efts.sec.gov/LATEST/search-index"


def _parse_date(s: Any) -> Optional[datetime]:
    """EDGAR file_date is ISO ``YYYY-MM-DD``."""
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        return datetime.fromisoformat(s.strip())
    except ValueError:
        return None


def _sort_by_recency(hits: list) -> list:
    """Reorder efts hits newest-first by ``_source.file_date`` (the recency bias the
    backend will not apply server-side: see ``_to_documents``). A hit with no parseable
    file_date sorts LAST (it carries no recency claim), and same-date hits keep their
    incoming relevance order because the sort is stable. Pure / offline / total: a
    non-list or a malformed hit degrades to "no date" rather than raising."""
    if not isinstance(hits, list):
        return []

    def _key(hit) -> str:
        src = hit.get("_source") if isinstance(hit, dict) else None
        fd = (src or {}).get("file_date") if isinstance(src, dict) else None
        # ISO YYYY-MM-DD sorts lexically == chronologically; "" sorts below any real date.
        return fd if (isinstance(fd, str) and _parse_date(fd) is not None) else ""

    return sorted(hits, key=_key, reverse=True)


# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
