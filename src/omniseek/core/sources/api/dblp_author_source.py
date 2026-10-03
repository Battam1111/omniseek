"""DBLP Authors: CS researcher profiles (name -> canonical PID + affiliation) via the dblp SPARQL endpoint.

Resolves a researcher NAME to a stable DBLP PID (the gateway to that author's full, already-ingested
publication record) plus publication count, affiliation and award notes. OmniSeek's
people-STRUCTURE reinforcement, CS-native and high-precision: a third researcher-identity source
behind ORCID (self-asserted CV) and s2_authors (citation metrics), so the people domain is no longer
a single point.

Access: the keyless dblp SPARQL endpoint (https://sparql.dblp.org/sparql, QLever), never dblp.org
itself: dblp.org is ``Disallow: /`` for every agent and Anubis-walled since 2026-09 (see
``dblp_source`` for the full why and the shared egress helpers). The name search uses QLever's text
index on ``dblp:primaryCreatorName`` (one ``ql:contains-word`` per name word), counts each person's
``dblp:authoredBy`` publications, and keeps the most prolific matches first:
  -> rows {person: "https://dblp.org/pid/<pid>", name, n, primary_affiliation?, affiliations?
     (tab-joined dblp:affiliation), notes? (tab-joined dblp:note, e.g. "award (2018): Turing Award")}
The person URI IS the canonical PID URL (extraction, no construction); affiliation and award come
only from what the endpoint returns (absent when it has none).

Drill-in: ``fetch_url`` answers a dblp person URL (dblp.org/pid/<pid>[.html|.xml|.bib], same mirror
hosts as dblp's record URLs) with ONE gated SPARQL request, a UNION of single-purpose branches about
that one person IRI (name, publication count, affiliations, notes, the 20 most recent publications
with venue), and builds one plain-text profile doc on the canonical PID URL. dblp.org itself is never
fetched; a pid with no ``dblp:primaryCreatorName`` is not found (None).

backend="dblp": shares the dblp upstream with the `dblp` publication source (honest backend count:
same upstream, a people facet). explicit_only: a named researcher drill.
"""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlparse

from omniseek.core import diag
from omniseek.core.normalize import Document, jsonsafe
from omniseek.core.sources.api import dblp_source as _dblp
from omniseek.core.sources.scrape._base import BaseScrapeAdapter

PID_PREFIX = "https://dblp.org/pid/"
HEALTH_PID = "56/953"   # Yoshua Bengio, the long-standing known entity of this probe
HEALTH_QUERY = (_dblp.PREFIX + "SELECT ?name WHERE {\n"
                f"  <{PID_PREFIX}{HEALTH_PID}> dblp:primaryCreatorName ?name .\n"
                "}")
# "award (2018): Turing Award" -> type "award", qualifier "2018", text "Turing Award"
_NOTE_RE = re.compile(r"^\s*([A-Za-z]+)\s*(?:\(([^)]*)\))?\s*:\s*(.+?)\s*$")
# A dblp person page: /pid/<pid> with an optional .html / .xml / .bib suffix. A pid is two segments
# ("56/953", "56/953-1", "l/YannLeCun"); the charset keeps it safe inside <...>.
_PID_PATH_RE = re.compile(r"^/pid/(.+?)(?:\.(?:html|xml|bib))?/?$")
_PID_RE = re.compile(r"[A-Za-z0-9]+/[A-Za-z0-9_=-]+")
RECENT = 20   # the most recent publications a person drill lists


def author_search_query(words: list[str], limit: int) -> str:
    """Persons whose primary name contains every word, most publications first, with their
    affiliations and notes."""
    return (_dblp.PREFIX
            + "SELECT ?person ?name ?n (SAMPLE(?pa) AS ?primary_affiliation) "
              '(GROUP_CONCAT(DISTINCT ?aff; separator="\\t") AS ?affiliations) '
              '(GROUP_CONCAT(DISTINCT ?note; separator="\\t") AS ?notes) WHERE {\n'
            + "  {\n    SELECT ?person ?name (COUNT(?pub) AS ?n) WHERE {\n"
            + _dblp.contains_lines("name", words, indent="      ")
            + "      ?person dblp:primaryCreatorName ?name .\n"
            + "      ?pub dblp:authoredBy ?person .\n"
            + f"    }} GROUP BY ?person ?name ORDER BY DESC(?n) LIMIT {int(limit)}\n  }}\n"
            + "  OPTIONAL { ?person dblp:primaryAffiliation ?pa }\n"
            + "  OPTIONAL { ?person dblp:affiliation ?aff }\n"
            + "  OPTIONAL { ?person dblp:note ?note }\n"
            + "} GROUP BY ?person ?name ?n ORDER BY DESC(?n)")


def person_uri(url: str) -> Optional[str]:
    """``https://dblp.org/pid/<pid>`` for a dblp person URL (``.html`` / ``.xml`` / ``.bib`` suffix
    dropped, mirror hosts accepted), else None. Pure; the pid charset keeps it safe inside ``<...>``."""
    try:
        parts = urlparse(url)
    except Exception:  # noqa: BLE001
        return None
    if (parts.hostname or "").lower() not in _dblp.DBLP_HOSTS:
        return None
    m = _PID_PATH_RE.match(parts.path or "")
    if not m or not _PID_RE.fullmatch(m.group(1)):
        return None
    return PID_PREFIX + m.group(1)


def person_query(uri: str, recent: int = RECENT) -> str:
    """One person's profile in one request: a UNION of branches about the one bound person IRI, so a
    row carries the variables of exactly one branch (name / count / an affiliation / a note / a recent
    publication). Every join is on the bound IRI or the bound ?pub, and the one OPTIONAL is a single
    triple (the QLever cost rule in ``dblp_source``). The COUNT branch always answers one row, so a
    pid dblp does not know comes back as a lone ``n = 0`` row with no name."""
    return (_dblp.PREFIX
            + "SELECT ?name ?n ?pa ?aff ?note ?pub ?title ?y ?v WHERE {\n"
            + f"  {{ <{uri}> dblp:primaryCreatorName ?name }}\n"
            + f"  UNION {{ SELECT (COUNT(?p) AS ?n) WHERE {{ ?p dblp:authoredBy <{uri}> }} }}\n"
            + f"  UNION {{ <{uri}> dblp:primaryAffiliation ?pa }}\n"
            + f"  UNION {{ <{uri}> dblp:affiliation ?aff }}\n"
            + f"  UNION {{ <{uri}> dblp:note ?note }}\n"
            + "  UNION {\n    {\n      SELECT ?pub ?title ?y WHERE {\n"
            + f"        ?pub dblp:authoredBy <{uri}> .\n"
            + "        ?pub dblp:title ?title .\n"
            + "        OPTIONAL { ?pub dblp:yearOfPublication ?y }\n"
            + f"      }} ORDER BY DESC(?y) ASC(?pub) LIMIT {int(recent)}\n    }}\n"
            + "    OPTIONAL { ?pub dblp:publishedIn ?v }\n  }\n"
            + "}")


def _split_tabbed(value: Any) -> list[str]:
    return [p.strip() for p in value.split("\t") if p.strip()] if isinstance(value, str) else []


class DBLPAuthorAdapter(BaseScrapeAdapter):
    name = "dblp_author"
    backend = "dblp"  # same dblp upstream as the `dblp` publication source, a people facet
    needs_credentials = False
    description = ("DBLP authors: resolve a CS researcher by NAME to a canonical DBLP PID page "
                   "(gateway to their full publication record) + affiliation + award notes; name a "
                   "researcher to disambiguate them in computer science. STRUCTURE, keyless, "
                   "people-lookup. CS-native; pairs with orcid / s2_authors / omniseek_resolve_identity.")
    cache_ttl = 86400  # 24h: researcher profiles change slowly
    kind = "lookup"
    domains = ["people"]
    modes = ["STRUCTURE"]
    explicit_only = ("dblp_author: a named CS-researcher drill (resolve a person to a DBLP PID); "
                     "not broad-fan-out fodder")
    # Owns every dblp host for omniseek_read, so a dblp URL neither adapter answers gets this adapter's
    # reason instead of the generic web read: that read would go to dblp.org, which is
    # `Disallow: /` for programs and serves only the Anubis page. Declared here, not on `dblp`,
    # because this adapter runs after it and knows both URL kinds it can answer.
    fetch_url_hosts = tuple(sorted(_dblp.DBLP_HOSTS))

    @staticmethod
    def _limit(limit: int) -> int:
        return max(1, min(int(limit), 30))

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """The binding rows (a list), [] without a request when the name has no usable word, None on
        a failed request or a non-JSON answer. Every name word is kept (no stopwords for names)."""
        words = _dblp.query_words(query, drop_stopwords=False)
        if not words:
            return []
        return _dblp.sparql_select(author_search_query(words, self._limit(limit)), timeout=15)

    async def _araw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """Async twin of _raw_fetch: same words, same query, same reader; only the egress is async."""
        words = _dblp.query_words(query, drop_stopwords=False)
        if not words:
            return []
        return await _dblp.asparql_select(author_search_query(words, self._limit(limit)), timeout=15)

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, list):
            return []
        docs: list[Document] = []
        for row in raw:
            doc = self._binding_to_doc(row)
            if doc is not None:
                docs.append(doc)
            if len(docs) >= limit:
                break
        return docs

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of search -> AsyncSearchCapable. Shares the base async cache
        round-trip; egress via _araw_fetch; mapping via the SAME pure-CPU _to_documents."""
        return await self._asearch_via(
            query, limit,
            afetch=lambda: self._araw_fetch(query, limit),
            abuild=lambda raw: self._to_documents(raw, query, limit))

    def _binding_to_doc(self, row: Any) -> Optional[Document]:
        """One SPARQL binding row -> doc on the canonical PID URL. Pure, total: a row without a
        person URI under dblp.org/pid/ or without a name drops."""
        if not isinstance(row, dict):
            return None
        url = row.get("person")
        name = row.get("name")
        if not name or not isinstance(url, str) or not url.startswith(PID_PREFIX):
            return None
        n = row.get("n")
        pubs = int(n) if isinstance(n, str) and n.isdigit() else None
        affils: list[str] = []
        for a in [row.get("primary_affiliation") or ""] + _split_tabbed(row.get("affiliations")):
            a = a.strip()
            if a and a not in affils:
                affils.append(a)
        note_affils, awards = self._split_notes(_split_tabbed(row.get("notes")))
        affils += [a for a in note_affils if a not in affils]
        content = name
        if pubs is not None:
            content += f" ({pubs} dblp publications)"
        if affils:
            content += ": " + "; ".join(affils)
        if awards:
            content += "; awards: " + ", ".join(awards)
        return Document(
            source=self.name,
            source_id=url,  # the PID url is the stable id
            url=url,
            title=name,
            content=content,
            author=name,
            date=None,
            signals={},
            tags=affils + awards,
            metadata={
                "pid_url": url,
                "pid": url[len(PID_PREFIX):],
                "publication_count": pubs,
                "affiliations": affils,
                "awards": awards,
                "raw": jsonsafe(row),
            },
        )

    @staticmethod
    def _split_notes(notes: Any) -> tuple[list[str], list[str]]:
        """dblp:note values look like "award (2018): Turing Award" / "affiliation: MIT". Split into
        affiliations and awards (the qualifier kept in parentheses; other note types and unparsable
        notes are ignored). Pure, total."""
        affils: list[str] = []
        awards: list[str] = []
        if isinstance(notes, str):
            notes = [notes]
        if not isinstance(notes, list):
            return affils, awards
        for note in notes:
            m = _NOTE_RE.match(note) if isinstance(note, str) else None
            if not m:
                continue
            ntype, qual, text = m.group(1).lower(), (m.group(2) or "").strip(), m.group(3)
            text = f"{text} ({qual})" if qual else text
            if ntype == "award" and text not in awards:
                awards.append(text)
            elif ntype == "affiliation" and text not in affils:
                affils.append(text)
        return affils, awards

    # --------------------------------------------------------------- fetch_url
    def fetch_url(self, url: str) -> Optional[Document]:
        """A dblp person URL (dblp.org/pid/<pid>[.html|.xml|.bib], mirror hosts too) answered by one
        gated SPARQL request about that person; dblp.org itself is never fetched. Any other URL, a
        failed request, or a pid with no primary name: None."""
        uri = person_uri(url)
        if uri is None:
            host = (urlparse(url).hostname or "").lower().rstrip(".")
            if host in _dblp.DBLP_HOSTS and _dblp.record_uri(url) is None:
                diag.note("dblp_author.fetch_url", url=url,
                          body="dblp.org pages are not read: its robots.txt disallows programs and it "
                               "serves a bot check; OmniSeek answers only dblp record (/rec/) and person "
                               "(/pid/) URLs, from sparql.dblp.org")
            return None
        rows = _dblp.sparql_select(person_query(uri), timeout=15)
        if not rows:
            return None
        return self._profile_to_doc(uri, rows)

    def _profile_to_doc(self, uri: str, rows: Any) -> Optional[Document]:
        """The UNION rows of ``person_query`` -> one profile doc on the canonical PID URL. Pure,
        total: no ``name`` row (dblp does not know the pid) means None, never a guessed doc."""
        if not isinstance(rows, list) or not uri.startswith(PID_PREFIX):
            return None
        name = next((r["name"] for r in rows if isinstance(r, dict) and r.get("name")), None)
        if not name:
            return None
        pubs_n: Optional[int] = None
        affils: list[str] = []
        notes: list[str] = []
        recent: dict[str, dict] = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            n = r.get("n")
            if isinstance(n, str) and n.isdigit():
                pubs_n = int(n)
            for key in ("pa", "aff"):
                a = (r.get(key) or "").strip()
                if a and a not in affils:
                    affils.append(a)
            note = (r.get("note") or "").strip()
            if note and note not in notes:
                notes.append(note)
            pub, title = r.get("pub"), r.get("title")
            if pub and title:
                p = recent.setdefault(pub, {"url": pub, "title": title.strip(),
                                            "year": r.get("y") or None, "venues": []})
                v = (r.get("v") or "").strip()
                if v and v not in p["venues"]:
                    p["venues"].append(v)
        note_affils, awards = self._split_notes(notes)
        affils += [a for a in note_affils if a not in affils]
        # The server's order is lost in the UNION + OPTIONAL join: restore newest first, then by URI.
        pubs = sorted(recent.values(), key=lambda p: p["url"])
        pubs.sort(key=lambda p: p["year"] or "", reverse=True)
        recent_pubs = [{"url": p["url"], "title": p["title"], "year": p["year"],
                        "venue": "; ".join(p["venues"]) or None} for p in pubs]

        lines = [name, f"dblp person: {uri}",
                 "Affiliations: " + ("; ".join(affils) if affils else "(none in dblp)"),
                 "Awards: " + (", ".join(awards) if awards else "(none in dblp)"),
                 "Publications in dblp: " + (str(pubs_n) if pubs_n is not None else "(unknown)")]
        if recent_pubs:
            lines += ["", f"Most recent publications ({len(recent_pubs)}):"]
            for p in recent_pubs:
                venue = p["venue"]
                if venue and venue.endswith("."):
                    venue = venue[:-1]
                parts = [p["year"], p["title"].rstrip(". "), venue, p["url"]]
                lines.append(". ".join(x for x in parts if x))
        return Document(
            source=self.name,
            source_id=uri,
            url=uri,
            title=name,
            content="\n".join(lines),
            author=name,
            date=None,
            signals={},
            tags=affils + awards,
            metadata={
                "pid_url": uri,
                "pid": uri[len(PID_PREFIX):],
                "name": name,
                "publication_count": pubs_n,
                "affiliations": affils,
                "awards": awards,
                "notes": notes,
                "recent_publications": recent_pubs,
            },
        )

    def health_check(self) -> tuple[bool, str]:
        # One gated SPARQL request for a known person. The Anubis page (or any non-JSON body) and an
        # empty answer are failures with their own reasons; a busy gate stays degraded-True.
        return _dblp.sparql_probe(HEALTH_QUERY, f"person pid {HEALTH_PID}", timeout=15)

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
