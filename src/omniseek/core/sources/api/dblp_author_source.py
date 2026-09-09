"""DBLP Authors — CS researcher profiles (name -> canonical PID + affiliation) via the keyless API.

Resolves a researcher NAME to a stable DBLP PID page (the gateway to that author's full,
already-ingested publication record) plus affiliation + award notes. OmniSeek's people-STRUCTURE
reinforcement, CS-native and high-precision: a third researcher-identity source behind ORCID
(self-asserted CV) and s2_authors (citation metrics), so the people domain is no longer a single
point.

Access via the public DBLP author-search API (no auth, no key):
  GET https://dblp.org/search/author/api?q=<name>&format=json
  -> {"result": {"hits": {"hit": [{"@score", "@id", "info": {
        "author": "<name>", "url": "https://dblp.org/pid/<pid>",
        "notes": {"note": [{"@type": "affiliation"|"award", "text": "..."}, ...]}}}, ...]}}}
The PID ``info.url`` IS the canonical URL (extraction, no construction); the affiliation/award
notes need a small filter (their shape is dict | list | absent), so this is a thin coded adapter.

backend="dblp": shares the DBLP host with the existing `dblp` publication source (honest backend
count: same upstream, a people facet). explicit_only: a named researcher drill.
"""

from __future__ import annotations

from typing import Any, Optional

from omniseek.core import http
from omniseek.core.normalize import Document, jsonsafe
from omniseek.core.sources.scrape._base import BaseScrapeAdapter

SEARCH_URL = "https://dblp.org/search/author/api"


class DBLPAuthorAdapter(BaseScrapeAdapter):
    name = "dblp_author"
    backend = "dblp"  # same DBLP host as the `dblp` publication source, a people facet
    needs_credentials = False
    description = ("DBLP authors — resolve a CS researcher by NAME to a canonical DBLP PID page "
                   "(gateway to their full publication record) + affiliation + award notes; name a "
                   "researcher to disambiguate them in computer science. STRUCTURE, keyless, "
                   "people-lookup. CS-native; pairs with orcid / s2_authors / omniseek_resolve_identity.")
    cache_ttl = 86400  # 24h: researcher profiles change slowly
    kind = "lookup"
    domains = ["people"]
    modes = ["STRUCTURE"]
    explicit_only = ("dblp_author: a named CS-researcher drill (resolve a person to a DBLP PID); "
                     "not broad-fan-out fodder")

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        return http.get_json(
            SEARCH_URL,
            params={"q": query, "format": "json", "h": max(1, min(int(limit), 30))},
            timeout=15,
        )

    async def _araw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """Async twin of _raw_fetch: byte-faithful mirror (same URL, params, timeout);
        only the shared-http egress fn is swapped for its async twin."""
        return await http.aget_json(
            SEARCH_URL,
            params={"q": query, "format": "json", "h": max(1, min(int(limit), 30))},
            timeout=15,
        )

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, dict):
            return []
        hits = (((raw.get("result") or {}).get("hits") or {}).get("hit")) or []
        if isinstance(hits, dict):  # a single hit can come back unwrapped
            hits = [hits]
        if not isinstance(hits, list):
            return []
        docs: list[Document] = []
        for hit in hits[:limit]:
            doc = self._hit_to_doc(hit)
            if doc is not None:
                docs.append(doc)
        return docs

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of search -> AsyncSearchCapable. Shares the base async cache
        round-trip; egress via _araw_fetch; mapping via the SAME pure-CPU _to_documents."""
        return await self._asearch_via(
            query, limit,
            afetch=lambda: self._araw_fetch(query, limit),
            abuild=lambda raw: self._to_documents(raw, query, limit))

    def _hit_to_doc(self, hit: Any) -> Optional[Document]:
        if not isinstance(hit, dict):
            return None
        info = hit.get("info") or {}
        if not isinstance(info, dict):
            return None
        name = info.get("author")
        url = info.get("url")
        if not name or not url:
            return None  # no name / canonical PID url -> no doc
        affils, awards = self._notes(info.get("notes"))
        content = name
        if affils:
            content += " — " + "; ".join(affils)
        if awards:
            content += " (" + ", ".join(awards) + ")"
        return Document(
            source=self.name,
            source_id=str(info.get("url")),  # the PID url is the stable id
            url=url,
            title=name,
            content=content,
            author=name,
            date=None,
            signals={},
            tags=affils + awards,
            metadata={
                "pid_url": url,
                "affiliations": affils,
                "awards": awards,
                "raw": jsonsafe(hit),
            },
        )

    @staticmethod
    def _notes(notes_block: Any) -> tuple[list[str], list[str]]:
        """info.notes.note is dict | list | absent; each note is {@type, text}. Split into
        affiliations and awards (other note types are ignored). Pure, total."""
        affils: list[str] = []
        awards: list[str] = []
        if not isinstance(notes_block, dict):
            return affils, awards
        notes = notes_block.get("note")
        if isinstance(notes, dict):
            notes = [notes]
        if not isinstance(notes, list):
            return affils, awards
        for note in notes:
            if not isinstance(note, dict):
                continue
            text = note.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            ntype = note.get("@type")
            if ntype == "award":
                awards.append(text.strip())
            elif ntype == "affiliation":
                affils.append(text.strip())
        return affils, awards

    def health_check(self) -> tuple[bool, str]:
        try:
            raw = http.get_json(SEARCH_URL, params={"q": "Bengio", "format": "json", "h": 1}, timeout=15)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"
        if isinstance(raw, dict) and isinstance(raw.get("result"), dict):
            return True, "OK"
        # "no result envelope" hid WHICH failure this was. dblp fronts the API with an Anubis
        # proof-of-work bot wall that answers 200 with a challenge PAGE, and it trips on request
        # RATE, so the same endpoint serves clean JSON minutes later. Naming it separates a wall
        # (wait, and do not hammer: no TLS tier gets through a JS proof-of-work) from a real schema
        # change (fix the parser). Measured 2026-09-09: challenge for plain httpx, for a browser-UA
        # curl and for the curl_cffi Chrome tier alike, minutes after the same URL returned JSON.
        if raw is None:
            body = http.get_text(SEARCH_URL, params={"q": "Bengio", "format": "json", "h": 1},
                                 timeout=15) or ""
            if "not a bot" in body or "<!doctype html" in body[:200].lower():
                return False, "bot wall (dblp Anubis challenge page, not JSON) -- rate-triggered, retries later"
            return False, "request failed or returned no body (see the http.get diag note)"
        return False, f"unexpected shape: {type(raw).__name__} without a 'result' object"

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
