"""DBLP: CS bibliography database (free, no auth), read through the dblp SPARQL endpoint.

DBLP indexes ~7M CS publications + ~3M authors with rich metadata (venue, year, DOI,
co-authorship graph). For ML/AI PhDs it's the canonical lens onto venue + author trajectory.

WHY SPARQL (driver check of 2026-10-03): dblp.org answers every endpoint (search/publ/api,
search/author/api, pid/*.xml) with an Anubis proof-of-work page (HTTP 200, text/html), and
https://dblp.org/robots.txt says ``User-agent: *`` / ``Disallow: /``. OmniSeek therefore sends NO
request to dblp.org at all and never tries to solve or route around the Anubis page. The sanctioned
machine entry is https://sparql.dblp.org/sparql (its robots.txt: ``Allow: /sparql``,
``Crawl-delay: 10``); the declared ``dblp`` upstream gate keeps one request at a time, starts at
least 10 s apart. dblp record URIs (https://dblp.org/rec/<key>) and person URIs
(https://dblp.org/pid/<pid>) are identifiers in the returned data, never fetched.

Endpoint: GET https://sparql.dblp.org/sparql?query=<SPARQL> with
``Accept: application/sparql-results+json`` (QLever). Title search uses QLever's text index: one
``ql:contains-word`` line per query word on the title's text record (``?t ql:contains-entity ?title``;
the same test directly on the literal returns 0 rows). The text index returns matches unranked
(roughly alphabetical), so the query takes the ``POOL`` SHORTEST matching titles (newest first on a
tie) and ``_raw_fetch`` ranks that pool locally with the shared BM25 scorer before the base caps it to
``limit``. Why shortest: every title in the match set contains every query word, mostly once, and with
term frequency fixed BM25 falls monotonically as the title gets longer, so the shortest titles are the
BM25 top of the whole match set. The first version took the most recent matches instead, and a known
older paper (a 2017 title against 2026 to 2027 matches for a common word pair) never reached the pool.

The helpers here (query builders, the SPARQL JSON reader, the gated GET and the health probe) are
shared with ``dblp_author_source``: one upstream, one egress path. Every request goes through the
shared ``http.get`` / ``http.aget`` helpers (SSRF guard, declared gate, diag notes, libcurl tier).

Migrated to ``BaseAPIAdapter`` (template method): the base owns the cache-check / map / cache-set /
auto-register mechanism; ``_raw_fetch`` (the SPARQL search + local ranking of the pool) and
``_to_document`` (one binding row to a doc) carry the source-specific facts. ``fetch_url`` +
``health_check`` are overridden because they do real SPARQL I/O the base defaults can't supply.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from omniseek.core import diag, http, upstreams
from omniseek.core.normalize import Document, jsonsafe, keyword_score_filter
from omniseek.core.sources.api._base import BaseAPIAdapter

logger = logging.getLogger(__name__)

SPARQL_URL = "https://sparql.dblp.org/sparql"
SPARQL_ACCEPT = "application/sparql-results+json"
TIMEOUT = 20
USER_AGENT = "omniseek/0.1 (automated retrieval)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": SPARQL_ACCEPT}
REC_PREFIX = "https://dblp.org/rec/"
# The candidate pool one title search ranks locally (the text index itself does not rank).
POOL = 100
# More words only narrow an AND match further; past this the query is long and the pool tiny.
MAX_WORDS = 8

PREFIX = "PREFIX dblp: <https://dblp.org/rdf/schema#>\n"
# English function words that carry no title signal; dropped from a PUBLICATION search only (a
# person's name keeps every word: "An", "He", "Le" are surnames).
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into", "is", "it",
    "its", "of", "on", "or", "the", "their", "this", "that", "to", "via", "was", "were", "with",
})
# Letters and digits only, so a word can never close the SPARQL string literal it is placed in.
_WORD_RE = re.compile(r"[^\W_]+")
_REC_KEY_RE = re.compile(r"[A-Za-z0-9_./-]+")
_REC_PATH_RE = re.compile(r"^/rec/(.+?)(?:\.(?:html|xml|bib|ris|nt|ttl|rdf))?/?$")
# The dblp web hosts whose record (and, in dblp_author, person) URLs are answered from SPARQL.
DBLP_HOSTS = frozenset({"dblp.org", "www.dblp.org", "dblp.uni-trier.de", "dblp.dagstuhl.de"})
_ORDINAL_RE = re.compile(r"^(\d+) (.+)$")
_DOI_PREFIX_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/", re.I)
_BOT_WALL_MARKERS = ("not a bot", "anubis")

# One row per publication: the fields every publication query returns. Authors come from the
# signatures (ordinal + the name as printed on the paper) so their order can be restored locally.
# QLever cost rule (read in its OptionalJoin source, 2026-10-03, after a first query timed out at
# 20 s): an OPTIONAL holding ONE triple pattern with one join column is joined against the small left
# side by a prefiltered index scan, while an OPTIONAL holding several triples, a FILTER or a BIND is
# evaluated over the whole dataset first. So every OPTIONAL below is a single triple; the signature
# link is a required triple (an OPTIONAL keyed on an unbound ?s would match every signature), which
# drops the rare record with no signature at all; CONCAT runs after the joins, on the small table.
_PUB_SELECT = ('SELECT ?pub ?title (SAMPLE(?y) AS ?year) (SAMPLE(?v) AS ?venue) (SAMPLE(?d) AS ?doi) '
               '(SAMPLE(?bt) AS ?type) (GROUP_CONCAT(DISTINCT ?au; separator="\\t") AS ?authors) WHERE {\n')
_PUB_FIELDS = (
    "  ?pub dblp:hasSignature ?s .\n"
    "  OPTIONAL { ?pub dblp:publishedIn ?v }\n"
    "  OPTIONAL { ?pub dblp:doi ?d }\n"
    "  OPTIONAL { ?pub dblp:bibtexType ?bt }\n"
    "  OPTIONAL { ?s dblp:signatureOrdinal ?o }\n"
    "  OPTIONAL { ?s dblp:signatureDblpName ?sn }\n"
    "  BIND(CONCAT(STR(?o), \" \", ?sn) AS ?au)\n"
)
HEALTH_QUERY = (PREFIX + "SELECT ?pub ?title WHERE {\n"
                "  ?t ql:contains-entity ?title .\n"
                '  ?t ql:contains-word "attention" .\n'
                '  ?t ql:contains-word "transformer" .\n'
                "  ?pub dblp:title ?title .\n"
                "} LIMIT 1")


# ── query building (pure) ────────────────────────────────────────────────────────────────────
def query_words(query: str, *, drop_stopwords: bool = True) -> list[str]:
    """Lowercased words of ``query`` usable as ``ql:contains-word`` terms: letters/digits only, at
    least 2 characters, stopwords dropped unless ``drop_stopwords`` is False, de-duplicated in order,
    at most ``MAX_WORDS``. An empty list means there is nothing to send."""
    words: list[str] = []
    for w in _WORD_RE.findall((query or "").lower()):
        if len(w) < 2 or (drop_stopwords and w in _STOPWORDS) or w in words:
            continue
        words.append(w)
    return words[:MAX_WORDS]


def contains_lines(var: str, words: list[str], indent: str = "  ") -> str:
    """The QLever text-index pattern: ``?var``'s text record must contain every word."""
    lines = [f"?t ql:contains-entity ?{var} ."] + [f'?t ql:contains-word "{w}" .' for w in words]
    return "".join(f"{indent}{line}\n" for line in lines)


def publ_search_query(words: list[str], pool: int = POOL) -> str:
    """Title search: the ``pool`` shortest titles that contain every word (newest first on a tie;
    the module docstring says why shortest), with year, venue, DOI, kind and ordered author names."""
    return (PREFIX + _PUB_SELECT
            + "  {\n    SELECT ?pub ?title ?y ?len WHERE {\n"
            + contains_lines("title", words, indent="      ")
            + "      ?pub dblp:title ?title .\n"
            + "      BIND(STRLEN(?title) AS ?len)\n"
            + "      OPTIONAL { ?pub dblp:yearOfPublication ?y }\n"
            + f"    }} ORDER BY ASC(?len) DESC(?y) LIMIT {int(pool)}\n  }}\n"
            + _PUB_FIELDS
            + "} GROUP BY ?pub ?title ORDER BY DESC(?year)")


def record_query(rec_uri: str) -> str:
    """One publication by its record URI (``record_uri`` has already validated it)."""
    return (PREFIX + _PUB_SELECT
            + f"  VALUES ?pub {{ <{rec_uri}> }}\n"
            + "  ?pub dblp:title ?title .\n"
            + _PUB_FIELDS
            + "  OPTIONAL { ?pub dblp:yearOfPublication ?y }\n"
            + "} GROUP BY ?pub ?title")


def record_uri(url: str) -> Optional[str]:
    """``https://dblp.org/rec/<key>`` for a dblp record URL (``.html`` / ``.xml`` / ``.bib`` ...
    suffix dropped, mirror hosts accepted), else None. The key is restricted to the characters dblp
    keys use, so it is safe inside ``<...>``."""
    try:
        parts = urlparse(url)
    except Exception:  # noqa: BLE001
        return None
    if (parts.hostname or "").lower() not in DBLP_HOSTS:
        return None
    m = _REC_PATH_RE.match(parts.path or "")
    if not m or not _REC_KEY_RE.fullmatch(m.group(1)):
        return None
    return REC_PREFIX + m.group(1)


# ── response reading (pure) ──────────────────────────────────────────────────────────────────
def sparql_rows(payload: Any) -> Optional[list[dict]]:
    """SPARQL 1.1 JSON results -> one ``{variable: value}`` dict per binding (unbound variables
    absent). None when ``payload`` is not SPARQL JSON (no ``results.bindings`` list)."""
    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    bindings = results.get("bindings") if isinstance(results, dict) else None
    if not isinstance(bindings, list):
        return None
    rows: list[dict] = []
    for b in bindings:
        if isinstance(b, dict):
            rows.append({k: v["value"] for k, v in b.items()
                         if isinstance(v, dict) and isinstance(v.get("value"), str)})
    return rows


def read_response(resp: Any) -> tuple[Optional[list[dict]], str]:
    """(rows, "") for a SPARQL JSON answer, else (None, the specific reason): the Anubis page, any
    other non-JSON body, or JSON without a results.bindings list."""
    if resp is None:
        return None, "request failed (non-2xx, timeout, network, or gate busy: see the http.get diag note)"
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        body = (getattr(resp, "text", "") or "").lower()
        if any(m in body for m in _BOT_WALL_MARKERS):
            return None, "bot wall (Anubis challenge page, not JSON)"
        ctype = resp.headers.get("content-type", "?") if getattr(resp, "headers", None) else "?"
        return None, f"not SPARQL JSON (HTTP {resp.status_code}, content-type {ctype})"
    rows = sparql_rows(payload)
    if rows is None:
        return None, f"JSON without a SPARQL results.bindings list (HTTP {resp.status_code})"
    return rows, ""


def _note_unreadable(resp: Any, why: str) -> None:
    if resp is not None:   # a failed request already left its own http.get note
        diag.note("dblp.sparql", url=SPARQL_URL, status=getattr(resp, "status_code", None),
                  body=f"{why}: {(getattr(resp, 'text', '') or '')[:500]}")


# ── egress (the one path to sparql.dblp.org) ─────────────────────────────────────────────────
def sparql_select(query: str, *, timeout: int = TIMEOUT) -> Optional[list[dict]]:
    """Run one SELECT through the shared http helper (declared dblp gate, SSRF guard, diag notes).
    Rows (possibly empty) on a SPARQL JSON answer; None on any failure or a non-JSON body."""
    resp = http.get(SPARQL_URL, params={"query": query}, headers=HEADERS, timeout=timeout)
    rows, why = read_response(resp)
    if rows is None:
        _note_unreadable(resp, why)
    return rows


async def asparql_select(query: str, *, timeout: int = TIMEOUT) -> Optional[list[dict]]:
    """Async twin of ``sparql_select`` (same URL, params, headers, timeout, same reader)."""
    resp = await http.aget(SPARQL_URL, params={"query": query}, headers=HEADERS, timeout=timeout)
    rows, why = read_response(resp)
    if rows is None:
        _note_unreadable(resp, why)
    return rows


def sparql_probe(query: str, what: str, *, timeout: int = TIMEOUT) -> tuple[Optional[bool], str]:
    """Health probe shared by both dblp adapters: ONE gated request for a known entity. Healthy only
    when the answer is SPARQL JSON with at least one binding. The gate is taken here first, so a busy
    gate surfaces as ``UpstreamBusy`` (None: degraded, not probed, so not verified) instead of a
    failed request."""
    try:
        with upstreams.egress(SPARQL_URL, request_s=timeout):
            resp = http.get(SPARQL_URL, params={"query": query}, headers=HEADERS, timeout=timeout)
    except upstreams.UpstreamBusy as exc:
        return None, f"degraded: declared dblp gate busy, not probed this cycle ({exc})"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:80]}"
    rows, why = read_response(resp)
    if rows is None:
        return False, why
    if not rows:
        return False, f"SPARQL JSON with 0 bindings for the known {what}"
    return True, f"OK ({len(rows)} binding(s) for the known {what})"


# ── field helpers (pure) ─────────────────────────────────────────────────────────────────────
def ordered_authors(field: Optional[str]) -> list[str]:
    """``"2 Noam Shazeer\\t1 Ashish Vaswani"`` -> ``["Ashish Vaswani", "Noam Shazeer"]``: the
    GROUP_CONCAT of "<ordinal> <name>" back in author order; an entry without an ordinal goes last."""
    ranked: list[tuple[int, str]] = []
    for part in (field or "").split("\t"):
        part = part.strip()
        if not part:
            continue
        m = _ORDINAL_RE.match(part)
        ranked.append((int(m.group(1)), m.group(2)) if m else (1 << 30, part))
    out: list[str] = []
    for _, name in sorted(ranked, key=lambda x: x[0]):
        if name not in out:
            out.append(name)
    return out


def bare_doi(value: Optional[str]) -> Optional[str]:
    """``https://doi.org/10.x/y`` -> ``10.x/y`` (the bare form the old JSON API gave)."""
    if not value:
        return None
    return _DOI_PREFIX_RE.sub("", value).strip() or None


def local_name(iri: Optional[str]) -> Optional[str]:
    """``https://dblp.org/rdf/schema#Inproceedings`` -> ``Inproceedings``."""
    if not iri:
        return None
    return iri.rsplit("#", 1)[-1].rsplit("/", 1)[-1] or None


class DBLPAdapter(BaseAPIAdapter):
    name = "dblp"
    needs_credentials = False
    description = (
        "DBLP: CS bibliography database (~7M publications + 3M authors, "
        "venue + year + DOI metadata; canonical CS publication lens)"
    )

    # Cache identity must stay ("dblp", "publ_search", query, limit): the hand-written form used
    # this middle part, so preserve it exactly.
    search_label = "publ_search"
    cache_ttl = 3600
    # _raw_fetch already ranked the whole pool (the base would only re-rank the first `limit`).
    rank_locally = False
    url_host = "dblp.org"

    # ------------------------------------------------------------------ hooks
    def _raw_fetch(self, query: str, limit: int) -> list:
        """The SPARQL title search, its pool ranked locally. No usable word: [] and no request.
        A failed request or a non-JSON answer raises, so the base records it and caches nothing."""
        words = query_words(query)
        if not words:
            return []
        rows = sparql_select(publ_search_query(words))
        if rows is None:
            raise RuntimeError("dblp SPARQL search failed (see the dblp.sparql / http.get diag note)")
        return self._rank_rows(rows, query)

    async def _araw_fetch(self, query: str, limit: int) -> list:
        """Async twin of ``_raw_fetch``: same words, same query, same reader, same ranking; only the
        egress is ``http.aget``."""
        words = query_words(query)
        if not words:
            return []
        rows = await asparql_select(publ_search_query(words))
        if rows is None:
            raise RuntimeError("dblp SPARQL search failed (see the dblp.sparql / http.get diag note)")
        return self._rank_rows(rows, query)

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` (AsyncSearchCapable): the base async cache round-trip
        ``_aapi_search`` (SAME cache key ``(name, "publ_search", query, limit)``, per-record
        ``_to_document``, cache-only-if-docs) with egress via ``_araw_fetch``."""
        return await self._aapi_search(query, limit, araw_fetch=lambda: self._araw_fetch(query, limit))

    def _to_document(self, raw) -> Optional[Document]:
        """One binding row -> Document."""
        return self._row_to_document(raw)

    def _rank_rows(self, rows: list[dict], query: str) -> list[dict]:
        """Order the pool by the shared BM25 scorer (the same one ``rank_locally`` would use). Rows
        the scorer gives 0 still matched every word on the server (a tokenizer difference), so they
        follow in pool order instead of being dropped."""
        pairs = []
        for row in rows:
            try:
                doc = self._row_to_document(row)
            except Exception as exc:  # noqa: BLE001
                logger.debug("dblp: skipping malformed row: %s", exc)
                continue
            if doc is not None:
                pairs.append((doc, row))
        by_doc = {id(d): r for d, r in pairs}
        ranked = [by_doc[id(d)] for d in keyword_score_filter([d for d, _ in pairs], query)]
        taken = {id(r) for r in ranked}
        return ranked + [r for _, r in pairs if id(r) not in taken]

    # --------------------------------------------------------------- fetch_url
    def fetch_url(self, url: str) -> Optional[Document]:
        """A dblp record URL (dblp.org/rec/<key>[.html|.xml|.bib]) resolved by a SPARQL lookup of
        its record URI; dblp.org itself is never fetched. Any other URL: None (a dblp person URL,
        dblp.org/pid/<pid>, is answered by ``dblp_author.fetch_url``)."""
        rec = record_uri(url)
        if rec is None:
            return None
        rows = sparql_select(record_query(rec))
        if not rows:
            return None
        try:
            return self._row_to_document(rows[0])
        except Exception as exc:  # noqa: BLE001
            logger.debug("DBLP fetch_url mapping failed: %s", exc)
            return None

    # ------------------------------------------------------------- health_check
    def health_check(self) -> tuple[Optional[bool], str]:
        return sparql_probe(HEALTH_QUERY, 'title word pair "attention" + "transformer"')

    # ------------------------------------------------------------ field mapping
    @staticmethod
    def _row_to_document(row: dict) -> Optional[Document]:
        if not isinstance(row, dict):
            return None
        pub = row.get("pub") or ""
        title = row.get("title")
        if not pub or not title:
            return None
        key = pub[len(REC_PREFIX):] if pub.startswith(REC_PREFIX) else pub
        authors = ordered_authors(row.get("authors"))
        author_str = ", ".join(authors[:6]) or None
        venue = row.get("venue") or None
        year = row.get("year") or None
        date = None
        if year:
            try:
                date = datetime(int(year[:4]), 1, 1, tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass
        doi = bare_doi(row.get("doi"))
        pub_type = local_name(row.get("type"))   # the BibTeX type: "Inproceedings", "Article", ...

        return Document(
            source="dblp",
            source_id=key,
            url=pub,   # the record URI: an identifier, never fetched
            title=title,
            content=f"{venue or '(no venue)'} • {year or '?'} • {pub_type or '?'}",
            author=author_str,
            date=date,
            tags=[pub_type] if pub_type else [],
            metadata={
                "publ_id": key,
                "venue": venue,
                "year": year,
                "doi": doi,
                "type": pub_type,
                "authors": authors,
                "raw": jsonsafe(row),
            },
        )
