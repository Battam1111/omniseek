"""Books axis: Open Library (bibliographic) + Internet Archive (full-text-inside-books).

Polaris-eye's BOOKS axis, two keyless surfaces merged behind one source so a single
query reaches both the catalog record AND the readable full text:

  (a) Open Library (openlibrary.org): the bibliographic record: title, author,
      first-publish year, subjects, ratings. The canonical "does this book exist,
      who wrote it, what is it about" lookup, on the Internet Archive's open catalog.

  (b) Internet Archive advancedsearch (archive.org): full-text-INSIDE books: the
      ``mediatype:texts`` corpus is millions of scanned / deposited texts whose body
      is searchable, so a query hits books whose CONTENT matches, not just the title.
      Each hit links to ``archive.org/details/<identifier>`` where the full text is
      readable (the UNWALL handle).

Both keyless, no quota auth.

APIs (verified live from the US egress, 2026-06-17):
  (a) GET https://openlibrary.org/search.json?q=<q>&limit=N&fields=<...>
      -> {"numFound": N, "docs": [{key:"/works/OL..W", title, author_name:[str],
          first_publish_year:int, ratings_average:float|None, ratings_count:int|None,
          subject:[str], language:[str], edition_count:int}, ...]}
      NOTE: subject + ratings_* come back null UNLESS requested via ``fields``, so we
      pass an explicit field list. The read link is ``openlibrary.org<key>``.
  (b) GET https://archive.org/advancedsearch.php?q=<q>+AND+mediatype:texts
          &fl[]=identifier&fl[]=title&fl[]=creator&fl[]=year&fl[]=downloads
          &fl[]=description&fl[]=subject&rows=N&output=json
      -> {"response": {"numFound": N, "docs": [{identifier, title, creator(str|list),
          year(str), downloads(int), description(str|list), subject(str|list|None)}, ...]}}
      The read link is ``archive.org/details/<identifier>``.

The two surfaces are fetched and merged: we split ``limit`` across them (so a small
limit still samples both), tag each doc by surface, and emit unified book docs
(title / author / date / content = subjects+description + a note + the read link;
signals = Open Library ``ratings_average`` via mk_signal where present).

Thin subclass over BaseScrapeAdapter: the cache check / atomic set_docs /
self-registration ritual lives in the base; this adapter declares its facets and
fills the two hooks. ``_raw_fetch`` does the dual GET (failure of EITHER surface is
tolerated: a partial payload still yields docs; only a total miss returns None ->
[]). ``rank`` stays default-False: each surface returns its own server order
(Open Library relevance, IA default), and the eye's ranked search re-scores across
sources when it needs a unified relevance order.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from penumbra.core import http
from penumbra.core.normalize import PolarisDocument, jsonsafe, mk_signal
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

OL_URL = "https://openlibrary.org/search.json"
OL_BASE = "https://openlibrary.org"
# Open Library returns subject + ratings only when asked: request them explicitly.
OL_FIELDS = (
    "key,title,author_name,first_publish_year,"
    "ratings_average,ratings_count,subject,language,edition_count"
)

IA_URL = "https://archive.org/advancedsearch.php"
IA_DETAILS = "https://archive.org/details"
IA_FIELDS = ("identifier", "title", "creator", "year", "downloads", "description", "subject")

TIMEOUT = 15


class BooksOpenLibraryIAAdapter(BaseScrapeAdapter):
    name = "books_openlibrary_ia"
    needs_credentials = False
    description = (
        "Books: Open Library bibliographic records + Internet Archive full-text-inside-books "
        "(read links to openlibrary.org / archive.org/details), keyless"
    )
    cache_ttl = 900

    # routing facets (the router reads these class attrs; do NOT touch facets.json)
    kind = "lookup"
    domains = ["books"]
    modes = ["STRUCTURE", "UNWALL"]

    # ── hooks ───────────────────────────────────────────────────────────────
    def _raw_fetch(self, query: str, limit: int) -> Optional[dict]:
        """Fetch BOTH surfaces. Split the limit so a small limit still samples each;
        a failure of either surface is tolerated (partial payload still yields docs).
        Returns None only when BOTH surfaces miss (the failure -> [] contract)."""
        # Give each surface at least 1 slot; bias slightly toward Open Library (the
        # primary bibliographic record) when limit is odd.
        ol_n = max(1, (limit + 1) // 2)
        ia_n = max(1, limit // 2)

        ol = http.get_json(
            OL_URL,
            params={"q": query, "limit": ol_n, "fields": OL_FIELDS},
            timeout=TIMEOUT,
        )
        ia = http.get_json(
            IA_URL,
            params=[
                ("q", f"{query} AND mediatype:texts"),
                *[("fl[]", f) for f in IA_FIELDS],
                ("rows", ia_n),
                ("output", "json"),
            ],
            timeout=TIMEOUT,
        )
        if ol is None and ia is None:
            return None
        return {"ol": ol, "ia": ia, "ol_n": ol_n, "ia_n": ia_n}

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[PolarisDocument]:
        if not isinstance(raw, dict):
            return []
        docs: list[PolarisDocument] = []

        # (a) Open Library bibliographic records
        ol = raw.get("ol")
        if isinstance(ol, dict):
            for rec in (ol.get("docs") or [])[: raw.get("ol_n", limit)]:
                if not isinstance(rec, dict):
                    continue
                try:
                    docs.append(self._ol_to_doc(rec))
                except Exception as exc:  # noqa: BLE001: a malformed record is skipped, not fatal
                    logger.debug("Skipping malformed Open Library record: %s", exc)

        # (b) Internet Archive full-text-inside-books
        ia = raw.get("ia")
        if isinstance(ia, dict):
            for rec in ((ia.get("response") or {}).get("docs") or [])[: raw.get("ia_n", limit)]:
                if not isinstance(rec, dict):
                    continue
                try:
                    docs.append(self._ia_to_doc(rec))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Skipping malformed Internet Archive record: %s", exc)

        return docs

    # ── per-surface mappers ───────────────────────────────────────────────────
    @staticmethod
    def _ol_to_doc(rec: dict) -> PolarisDocument:
        key = rec.get("key") or ""  # e.g. "/works/OL17854085W"
        title = (rec.get("title") or "(untitled)").strip()
        read_url = f"{OL_BASE}{key}" if key else OL_BASE

        author = _join_names(rec.get("author_name"))
        date = _year_to_date(rec.get("first_publish_year"))
        subjects = [s for s in (rec.get("subject") or []) if isinstance(s, str)]
        languages = [s for s in (rec.get("language") or []) if isinstance(s, str)]
        rating = rec.get("ratings_average")

        content_lines: list[str] = []
        if subjects:
            content_lines.append("Subjects: " + "; ".join(subjects[:20]))
        if languages:
            content_lines.append("Language(s): " + ", ".join(languages))
        if isinstance(rating, (int, float)) and not isinstance(rating, bool):
            rc = rec.get("ratings_count")
            content_lines.append(
                f"Open Library rating: {rating:.2f}"
                + (f" ({rc} ratings)" if isinstance(rc, int) else "")
            )
        content_lines.append(
            "Open Library bibliographic record. Open the book page below for editions, "
            "subjects, and (where digitized) a read/borrow link."
        )
        content_lines.append(f"Book page: {read_url}")
        content = "\n\n".join(content_lines)

        return PolarisDocument(
            source="books_openlibrary_ia",
            source_id=key.rsplit("/", 1)[-1] if key else (title or "ol"),
            url=read_url,
            title=title,
            content=content,
            author=author,
            date=date,
            # ratings_average is a source-reported number -> through mk_signal (None-safe).
            signals=mk_signal(
                "rating", rating, kind="engagement", by="openlibrary/ratings_average",
            ),
            tags=["openlibrary", *subjects],
            metadata={
                "surface": "openlibrary",
                "olid": key.rsplit("/", 1)[-1] if key else None,
                "languages": languages,
                "edition_count": rec.get("edition_count"),
                "first_publish_year": rec.get("first_publish_year"),
                "ratings_count": rec.get("ratings_count"),
                "raw": jsonsafe(rec),
            },
        )

    @staticmethod
    def _ia_to_doc(rec: dict) -> PolarisDocument:
        ident = (rec.get("identifier") or "").strip()
        title = _first_str(rec.get("title")) or "(untitled)"
        read_url = f"{IA_DETAILS}/{ident}" if ident else IA_DETAILS

        author = _join_names(rec.get("creator"))
        date = _year_to_date(rec.get("year"))
        # subject can be str | list | None; description can be str | list.
        subjects = _as_str_list(rec.get("subject"))
        description = _first_str(rec.get("description"))

        content_lines: list[str] = []
        if subjects:
            content_lines.append("Subjects: " + "; ".join(subjects[:20]))
        if description:
            content_lines.append(description.strip())
        content_lines.append(
            "Internet Archive full-text book (mediatype:texts). The body is searchable; "
            "open the details page below to read the full scanned/deposited text."
        )
        content_lines.append(f"Read full text: {read_url}")
        content = "\n\n".join(content_lines)

        return PolarisDocument(
            source="books_openlibrary_ia",
            source_id=ident or (title or "ia"),
            url=read_url,
            title=title,
            content=content,
            author=author,
            date=date,
            # IA exposes a downloads count (engagement); rating is Open-Library-only.
            signals=mk_signal(
                "downloads", rec.get("downloads"), kind="engagement", by="archive_org/downloads",
            ),
            tags=["internet_archive", *subjects],
            metadata={
                "surface": "internet_archive",
                "identifier": ident or None,
                "year": rec.get("year"),
                "downloads": rec.get("downloads"),
                "raw": jsonsafe(rec),
            },
        )


# ── helpers ──────────────────────────────────────────────────────────────────
def _join_names(value: Any) -> Optional[str]:
    """Open Library ``author_name`` / IA ``creator`` are a str or a list of str. Join into
    one author string, de-duplicating while preserving order (IA sometimes repeats a name)."""
    names = _as_str_list(value)
    if not names:
        return None
    seen: dict[str, None] = {}
    for n in names:
        n = n.strip()
        if n and n not in seen:
            seen[n] = None
    return "; ".join(seen) if seen else None


def _as_str_list(value: Any) -> list[str]:
    """Coerce a str | list | None field into a clean list[str] (drops non-strings/empties)."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


def _first_str(value: Any) -> Optional[str]:
    """First non-empty string of a str | list | None field (IA description/title can be either)."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        for v in value:
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _year_to_date(value: Any) -> Optional[datetime]:
    """A publish year (int from Open Library, or a str / 'YYYY-..' from IA ``year``) -> a
    January-1 datetime. Best-effort: returns None on anything unparseable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        year = value
    elif isinstance(value, str) and value.strip():
        # IA year is usually "1998"; occasionally "1998-01-01" or "[1998]".
        digits = "".join(c for c in value if c.isdigit())[:4]
        if len(digits) != 4:
            return None
        year = int(digits)
    else:
        return None
    if 1 <= year <= 9999:
        try:
            return datetime(year, 1, 1)
        except ValueError:
            return None
    return None


# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
