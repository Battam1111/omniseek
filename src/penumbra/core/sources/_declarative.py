"""Declarative REST/JSON sources — a standard search-API source is now ONE table row.

Many of the eye's open-API sources are mechanically identical: GET a JSON endpoint
with the query interpolated into a param template, walk a list of result objects out
of the response, pluck a handful of fields (title / url / content / date / author /
score / id) by name, wrap each in a ``PolarisDocument``, and keyword-filter. The only
things that differ per source are the *endpoint*, the *param shape*, and *where the
fields live in the JSON*. Everything else (shared pooled HTTP, cache round-trip, the
ONE BM25 scorer, health probe, registration) is identical boilerplate.

This module turns that boilerplate into a single ``DeclarativeAPIAdapter`` driven by a
data table (``sources.json``). Adding such a source = one JSON row, zero ``.py``.

What a row declares (see ``sources.json`` for the live rows + per-key notes)::

    {
      "name":            "hackernews",
      "description":     "... shown in eye_list_sources, the agent's router ...",
      "endpoint":        "https://hn.algolia.com/api/v1/search",
      "method":          "GET",               # or "POST" (body = rendered params)
      "params_template": {"query": "{query}", "tags": "story",
                          "hitsPerPage": "{limit}"},
      "results_path":    "hits",              # dot path to the list of result objects
      "field_map": {                          # PolarisDocument field <- dot path in a result
          "title":   "title",
          "url":     "url",
          "content": "story_text",
          "author":  "author",
          "date":    "created_at",
          "score":   "points",
          "id":      "objectID"
      },
      "cache_ttl":         900,
      "url_host":          "ycombinator.com", # substring test for fetch_url ownership
      "facets":            {"kind": "stream", "domains": ["news"]},
      "explicit_only":     false,             # True | reason-string | absent
      "needs_credentials": false
    }

Boundary (when to NOT use this — fall back to a coded adapter / a base class):
  - the param shape is not a flat dict of interpolated strings (signed requests,
    pagination cursors, multi-call fan-out, GraphQL) -> coded adapter;
  - results are not one flat JSON list reachable by a dot path (deeply nested,
    must be merged across pages/calls, or non-JSON) -> coded adapter;
  - a field needs real transformation, not just extraction (inverted-index abstract
    reconstruction, JATS-XML stripping, date math) -> coded adapter / a base class
    whose ``_to_doc`` does the work (e.g. ``_openalex.parse_work``, RSSAdapterBase);
  - the source is walled / anti-bot (needs bespoke headers + signing) -> walled/.
The Protocol back door stays fully legal: a special source is always free to be its
own ``*_source.py`` class. This base is a convenience for the regular case, never a
cage.

The adapter is INERT until imported: this file does not end in ``_source`` so the
``walk_packages`` discovery in ``penumbra.server`` skips it. ``declarative_source.py``
(a thin ``*_source.py``) imports + runs ``load_declarative_sources()`` to register
every row — same two-part shape as ``rss_bundles_source.py`` + ``scrape/_rss.py``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from penumbra.core import cache, http
from penumbra.core.normalize import PolarisDocument, jsonsafe, keyword_score_filter, mk_signal

logger = logging.getLogger(__name__)

_DATA = Path(__file__).with_name("sources.json")
_DEFAULT_TTL = 1800
_DEFAULT_LIMIT_CAP = 30  # cap interpolated into {limit} so a row can't ask for a huge page


# -----------------------------------------------------------------------------
# Field extraction — dot paths over the response JSON
# -----------------------------------------------------------------------------


def _dig(obj: Any, path: str) -> Any:
    """Follow a dot path through nested dicts/lists; return None if any hop misses.

    Each segment indexes a dict by key, OR a list by integer (``authors.0.name``).
    Mechanical only — no judgement, no transformation; just "where the value lives".
    A leading ``$`` (JSONPath-style root marker) is ignored, so ``$.hits`` == ``hits``.
    An empty path returns ``obj`` unchanged (lets ``results_path: ""`` mean "the
    response itself is already the list").
    """
    if path is None:
        return None
    path = path.strip()
    if path.startswith("$"):
        path = path[1:].lstrip(".")
    if not path:
        return obj
    cur = obj
    for seg in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _dig_any(obj: Any, spec: Any) -> Any:
    """Extract by a single dot path (str) OR the FIRST non-None of a fallback list.

    ``"url"`` -> the value at ``url``. ``["url", "external_url"]`` -> ``url`` if present
    else ``external_url``. Still pure extraction (coalesce of existing fields) — never
    synthesis. A field that needs a CONSTRUCTED value (e.g. an item-id stitched into a
    URL template) is past the boundary -> coded adapter."""
    if spec is None:
        return None
    if isinstance(spec, str):
        return _dig(obj, spec)
    if isinstance(spec, list):
        for p in spec:
            v = _dig(obj, p) if isinstance(p, str) else None
            if v is not None and v != "":
                return v
        return None
    return None


def _as_str(v: Any) -> Optional[str]:
    """Coerce an extracted value to a clean string, or None.

    Lists are joined (an API may return ``title: ["..."]`` — Crossref does), so the
    common "first/only element of a singleton list" case Just Works without a coded
    adapter. dict/other -> JSON-ish str as a last resort (never silently dropped)."""
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, (int, float, bool)):
        return str(v)
    if isinstance(v, list):
        parts = [p for p in (_as_str(x) for x in v) if p]
        return ", ".join(parts) or None
    return str(v)


def _as_int(v: Any) -> Optional[int]:
    """Coerce to int (score/citation counts), else None — never raises."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        try:
            return int(float(v.strip()))
        except (ValueError, TypeError):
            return None
    return None


_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%Y/%m/%d",
)


def _as_date(v: Any) -> Optional[datetime]:
    """Parse a common date string (ISO-8601 and a few near-ISO shapes). None on
    miss — declarative sources only carry plainly-formatted dates; anything exotic
    (epoch math, date-parts arrays) is the boundary signal to use a coded adapter."""
    if v is None:
        return None
    if isinstance(v, (int, float)):  # epoch seconds
        try:
            return datetime.fromtimestamp(float(v), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    s = _as_str(v)
    if not s:
        return None
    iso = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


# -----------------------------------------------------------------------------
# The adapter
# -----------------------------------------------------------------------------


class DeclarativeAPIAdapter:
    """A standard REST/JSON search source, fully described by a data row.

    Mechanism only (the eye's "code is dumb, agent is smart" rule): fetch via the
    shared pooled client, extract fields by dot path, rank with the ONE shared BM25
    scorer (``keyword_score_filter`` -> ``relevance.doc_scores``). It makes NO
    business judgement — no custom sort, no relevance heuristic, no field synthesis.
    A source that needs any of those is past this base's boundary (see module docstring).
    """

    kind = "stream"

    def __init__(
        self,
        *,
        name: str,
        description: str,
        endpoint: str,
        field_map: dict,
        params_template: Optional[dict] = None,
        method: str = "GET",
        results_path: str = "",
        cache_ttl: int = _DEFAULT_TTL,
        url_host: Optional[str] = None,
        facets: Optional[dict] = None,
        explicit_only: Any = False,
        needs_credentials: bool = False,
        limit_cap: int = _DEFAULT_LIMIT_CAP,
        timeout: int = http.DEFAULT_TIMEOUT,
        post_filter: bool = True,
    ) -> None:
        if "title" not in field_map or "url" not in field_map:
            raise ValueError(
                f"declarative source {name!r}: field_map must map at least 'title' and 'url'"
            )
        self.name = name
        self.description = description
        self.endpoint = endpoint
        self.field_map = field_map
        self.params_template = params_template or {}
        self.method = (method or "GET").upper()
        self.results_path = results_path or ""
        self.cache_ttl = cache_ttl
        self.url_host = (url_host or "").lower()
        self.explicit_only = explicit_only  # True | reason string | False/absent
        self.needs_credentials = bool(needs_credentials)
        self.limit_cap = limit_cap
        self.timeout = timeout
        # post_filter=True (default): re-rank+drop via the shared BM25 scorer (the eye
        # canon, like RSSAdapterBase). post_filter=False: the ENDPOINT already ranked
        # server-side for this query (Algolia, Elastic, a relevance API) — keep its
        # order verbatim, just truncate to limit; this is what makes such a source
        # byte-equivalent to its former coded adapter (no client-side re-rank that could
        # drop a fuzzy server-side hit).
        self.post_filter = bool(post_filter)
        # Optional routing facets surfaced through list_sources (adapter attr wins over
        # facets.json) — exactly the kind/domains/regions the fetcher reads off an adapter.
        facets = facets or {}
        for facet in ("kind", "domains", "regions"):
            if facets.get(facet):
                setattr(self, facet, facets[facet])

    # -- request shaping -------------------------------------------------------

    def _render_params(self, query: str, limit: int) -> dict:
        """Interpolate ``{query}`` / ``{limit}`` into the param template.

        Only these two placeholders exist (a flat string template is the whole point;
        anything fancier is the boundary). A value is rendered only if it is a string
        CONTAINING a placeholder, so literal params (``"tags": "story"``) pass through
        untouched and numeric/bool literals are preserved verbatim."""
        capped = min(int(limit), self.limit_cap)
        out: dict = {}
        for k, v in self.params_template.items():
            if isinstance(v, str) and ("{query}" in v or "{limit}" in v):
                out[k] = v.replace("{query}", query or "").replace("{limit}", str(capped))
            else:
                out[k] = v
        return out

    def _fetch_results(self, params: dict) -> list[dict]:
        """One HTTP call -> the list of raw result objects (via ``results_path``)."""
        if self.method == "POST":
            data = http.post_json(self.endpoint, json=params, timeout=self.timeout)
        else:
            data = http.get_json(self.endpoint, params=params, timeout=self.timeout)
        if data is None:
            return []
        results = _dig(data, self.results_path)
        if results is None:
            return []
        if isinstance(results, dict):  # tolerate {"id": {...}, ...} maps -> values
            results = list(results.values())
        if not isinstance(results, list):
            return []
        return [r for r in results if isinstance(r, dict)]

    # -- mapping ---------------------------------------------------------------

    def _to_doc(self, item: dict) -> Optional[PolarisDocument]:
        """Map one raw result dict to a PolarisDocument via ``field_map`` dot paths.

        Each field's spec is a dot path (str) or a fallback list (first non-None wins,
        e.g. ``"url": ["url", "external_url"]``). ``metadata['raw']`` keeps the original
        item (the lossless escape hatch the whole eye relies on); ``to_tool_dict`` drops
        it from the agent projection."""
        fm = self.field_map
        title = _as_str(_dig_any(item, fm["title"])) or "(no title)"
        url = _as_str(_dig_any(item, fm["url"])) or ""
        if not url:
            return None  # a doc with no canonical URL is unusable downstream
        content = _as_str(_dig_any(item, fm["content"])) if fm.get("content") else None
        source_id = _as_str(_dig_any(item, fm["id"])) if fm.get("id") else None
        return PolarisDocument(
            source=self.name,
            source_id=source_id or url,
            url=url,
            title=title,
            content=content or title,
            author=_as_str(_dig_any(item, fm["author"])) if fm.get("author") else None,
            date=_as_date(_dig_any(item, fm["date"])) if fm.get("date") else None,
            signals=(mk_signal('score', _as_int(_dig_any(item, fm['score'])),
                               kind='engagement', by=f"{self.name}/{fm['score']}")
                     if fm.get('score') else {}),
            tags=self._tags(item),
            metadata={"raw": jsonsafe(item)},
        )

    def _tags(self, item: dict) -> list[str]:
        """Optional ``tags`` field map -> list[str] (extraction only)."""
        spec = self.field_map.get("tags")
        if not spec:
            return []
        v = _dig_any(item, spec)
        if isinstance(v, list):
            return [t for t in (_as_str(x) for x in v) if t]
        s = _as_str(v)
        return [s] if s else []

    # -- SourceAdapter protocol ------------------------------------------------

    def search(self, query: str, limit: int = 10) -> list[PolarisDocument]:
        key = cache.make_key(self.name, "search", query, limit)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached

        items = self._fetch_results(self._render_params(query or "", limit))
        if not self.post_filter:
            # Server already ranked for this query → keep API order, just truncate
            # (and truncate BEFORE mapping, exactly as the former coded adapters did).
            items = items[:limit]
        docs: list[PolarisDocument] = []
        for item in items:
            try:
                doc = self._to_doc(item)
                if doc:
                    docs.append(doc)
            except Exception as exc:  # noqa: BLE001 — one bad row never sinks the batch
                logger.debug("declarative[%s]: skipping malformed item: %s", self.name, exc)

        if self.post_filter:
            # The ONE shared scorer: filter + rank (title 3x), term-less query keeps order.
            docs = keyword_score_filter(docs, (query or "").strip())[:limit]
        cache.set_docs(key, docs, ttl=self.cache_ttl)
        return docs

    def fetch_url(self, url: str) -> Optional[PolarisDocument]:
        """Declarative sources are SEARCH-ONLY: they declare a search endpoint + param
        template, not a per-item GET-by-id endpoint, so there is no mechanical way to
        turn an arbitrary URL into one result object. Returns None (does not claim the
        URL), letting the fetcher try the next adapter / the generic web reader.

        This is a deliberate boundary, NOT a gap: a source whose value includes
        by-URL drill-down (e.g. Hacker News' ``/items/{id}`` thread fetch) keeps a
        coded ``*_source.py`` adapter with a real ``fetch_url`` — the Protocol back
        door is always open. ``url_host`` is still declared so facets/routing know which
        host a row's docs come from."""
        return None

    def health_check(self) -> tuple[bool, str]:
        """Probe with a tiny live request; healthy if the results list is reachable."""
        try:
            items = self._fetch_results(self._render_params("test", 1))
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        # An empty list for a junk query is still "endpoint + shape OK".
        return True, f"OK (endpoint reachable; {len(items)} result(s) for probe)"


# -----------------------------------------------------------------------------
# Registration loop — reads the table, registers every row
# -----------------------------------------------------------------------------


def _row_to_adapter(row: dict) -> DeclarativeAPIAdapter:
    return DeclarativeAPIAdapter(
        name=row["name"],
        description=row["description"],
        endpoint=row["endpoint"],
        field_map=row["field_map"],
        params_template=row.get("params_template"),
        method=row.get("method", "GET"),
        results_path=row.get("results_path", ""),
        cache_ttl=row.get("cache_ttl", _DEFAULT_TTL),
        url_host=row.get("url_host"),
        facets=row.get("facets"),
        explicit_only=row.get("explicit_only", False),
        needs_credentials=row.get("needs_credentials", False),
        limit_cap=row.get("limit_cap", _DEFAULT_LIMIT_CAP),
        timeout=row.get("timeout", http.DEFAULT_TIMEOUT),
        post_filter=row.get("post_filter", True),
    )


def load_declarative_sources() -> list[str]:
    """Read ``sources.json`` and register every row. Returns the names registered.

    Per-row failures are logged + skipped (one bad row never sinks the table) —
    mirrors the server's per-module import isolation. Called by the thin
    ``declarative_source.py`` so ``walk_packages`` discovery triggers it."""
    from penumbra.core.fetcher import register_adapter

    if not _DATA.exists():
        logger.warning("declarative: %s missing — no declarative sources loaded", _DATA)
        return []
    try:
        rows = json.loads(_DATA.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("declarative: failed to read %s: %s", _DATA, exc)
        return []

    registered: list[str] = []
    for row in rows:
        try:
            register_adapter(_row_to_adapter(row))
            registered.append(row["name"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("declarative: skipping bad row %r: %s", row.get("name"), exc)
    return registered
