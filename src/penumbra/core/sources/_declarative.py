"""Declarative REST/JSON sources — a standard search-API source is now ONE table row.

Many of the eye's open-API sources are mechanically identical: GET a JSON endpoint
with the query interpolated into a param template, walk a list of result objects out
of the response, pluck a handful of fields (title / url / content / date / author /
score / id) by name, wrap each in a ``Document``, and keyword-filter. The only
things that differ per source are the *endpoint*, the *param shape*, and *where the
fields live in the JSON*. Everything else (shared pooled HTTP, cache round-trip, the
ONE BM25 scorer, health probe, registration) is identical boilerplate.

This module turns that boilerplate into a single ``DeclarativeAPIAdapter`` driven by a
data table (``sources.json``). Adding such a source = one JSON row, zero ``.py``.

What a row declares (see ``sources.json`` for the live rows + per-key notes)::

    {
      "name":            "hackernews",
      "description":     "... shown in penumbra_list_sources, the agent's router ...",
      "endpoint":        "https://hn.algolia.com/api/v1/search",
      "method":          "GET",               # or "POST" (body = rendered params)
      "params_template": {"query": "{query}", "tags": "story",
                          "hitsPerPage": "{limit}"},
      "results_path":    "hits",              # dot path to the list of result objects
      "field_map": {                          # Document field <- dot path in a result
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

TRANSPORT (the meta-source, P10): a row also declares HOW to move bytes. ``transport`` is
``"http"`` (absent == "http", full back-compat: GET/POST the ``endpoint``, above) OR ``"mcp"``
(wrap an external MCP server: the SAME row table, a different transport). A wrapped MCP server
is NOT a new adapter species; it is this same declarative row with ``tools/call`` instead of a
GET, so every memory/dedup/ranking mechanism the eye has applies to it unchanged, and each
wrapped server still earns its slot through the curator razor PER SERVER. An ``mcp`` row adds::

      "transport":  "mcp",
      "endpoint":   "https://host/mcp",       # the streamable-HTTP MCP endpoint
      "tool":       "search",                 # REQUIRED: the tool name to tools/call
      "params_template": {"query": "{query}", # becomes the tool ARGUMENTS template (same
                          "limit": "{limit}"},#   {query}/{limit} interpolation as http)
      "results_path": "results",              # dot path INTO the tool result object
      "field_map":  { ... },                  # walks the tool result (structuredContent, or the
                                              #   parsed JSON of a single text block)
      "text_fallback": false                  # OPT-IN. When true AND the tool result is only
                                              #   prose text blocks (no structured object the
                                              #   field_map can walk), each block -> one doc
                                              #   (title = first line, content = the block, url =
                                              #   endpoint#tool). WITHOUT this opt-in a mapping
                                              #   that stops matching FAILS VISIBLY to [] + a
                                              #   debug log, never degrades into prose soup.

Netguard posture: an ``mcp`` row's ``endpoint`` is OPERATOR-CONFIG data (the same trust class
as every ``sources.json`` endpoint) and rides the same pooled client + SSRF guard + deadline
machinery (``_mcp._post`` re-applies ``http._request_capped``'s exact guards inline: SSRF
pre-flight, MAX_BYTES cap, response-rebuild decoding). Retrieved tool RESULTS stay
UNTRUSTED data (instructions §9), exactly like every other source. ``url_host`` works as usual
for penumbra_read routing when set. Credentials for an ``mcp`` row live at
``~/.penumbra/credentials/mcp_<name>.json`` (``{"headers": {...}}``); a ``needs_credentials: true``
row with no file degrades to [] silently at fetch (the podcast_index precedent).

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
from penumbra.core.normalize import Document, jsonsafe, keyword_score_filter, mk_signal

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
        transport: str = "http",
        tool: Optional[str] = None,
        text_fallback: bool = False,
    ) -> None:
        if "title" not in field_map or "url" not in field_map:
            raise ValueError(
                f"declarative source {name!r}: field_map must map at least 'title' and 'url'"
            )
        # Transport slot (P10): "http" (default, full back-compat) or "mcp" (wrap an external MCP
        # server via tools/call). Only the FETCH differs; the field walk / BM25 / cache are shared.
        self.transport = (transport or "http").lower()
        if self.transport not in ("http", "mcp"):
            raise ValueError(
                f"declarative source {name!r}: transport must be 'http' or 'mcp', got {transport!r}")
        self.tool = tool
        if self.transport == "mcp" and not self.tool:
            raise ValueError(
                f"declarative source {name!r}: transport 'mcp' requires a 'tool' name")
        # text_fallback (mcp only): opt-in to synthesize one doc per prose text block when the tool
        # result carries NO structured object the field_map can walk. Never silent: without this a
        # prose-only result fails VISIBLY to [] (see _fetch_results / _docs_from_text_blocks).
        self.text_fallback = bool(text_fallback)
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
        untouched and numeric/bool literals are preserved verbatim. A value that is
        EXACTLY ``"{limit}"`` renders as the INT, not its string form: http query params
        encode either identically, but an mcp tools/call argument is TYPED, and a wrapped
        tool whose inputSchema says integer would reject "5" (P10 gate catch)."""
        capped = min(int(limit), self.limit_cap)
        out: dict = {}
        for k, v in self.params_template.items():
            if v == "{limit}":
                out[k] = capped
            elif isinstance(v, str) and ("{query}" in v or "{limit}" in v):
                out[k] = v.replace("{query}", query or "").replace("{limit}", str(capped))
            else:
                out[k] = v
        return out

    def _fetch_results(self, params: dict) -> list[dict]:
        """Fetch by the row's transport -> the list of raw result objects (via ``results_path``).

        transport "http" (default): one GET/POST. transport "mcp": one ``tools/call`` on the
        wrapped server. Both then walk ``results_path`` out of the response the SAME way. The mcp
        text_fallback (prose-only result -> synthesized docs) is handled in ``search`` (which reads
        the raw response via ``_fetch_response``); this returns only the mappable ITEM dicts, so it
        stays the drop-in ``health_check`` probe path too."""
        data = self._fetch_response(params)
        return self._results_from(data)

    def _fetch_response(self, params: dict) -> Any:
        """Transport-specific fetch -> the raw response OBJECT (an http JSON body, or an mcp tool
        result). None on any failure (the source contract; mcp transport errors are caught here)."""
        if self.transport == "mcp":
            return self._mcp_fetch(params)
        if self.method == "POST":
            return http.post_json(self.endpoint, json=params, timeout=self.timeout)
        return http.get_json(self.endpoint, params=params, timeout=self.timeout)

    def _mcp_fetch(self, arguments: dict) -> Any:
        """One ``tools/call`` on the wrapped MCP server -> the parsed tool result object. The
        rendered params ARE the tool arguments (same {query}/{limit} interpolation as http). A row
        with ``needs_credentials`` and no credentials file degrades to None BEFORE any network
        (the podcast_index precedent). Any transport/protocol error (MCPTransportError) -> None so
        the source contract (failure -> []) holds; isError tool results log at debug and -> None."""
        from penumbra.core.sources._mcp import MCPTransportError, get_client, load_mcp_headers

        headers, configured = load_mcp_headers(self.name)
        if self.needs_credentials and not configured:
            logger.debug("declarative[%s]: mcp row needs credentials (mcp_%s.json); degrading to []",
                         self.name, self.name)
            return None
        try:
            client = get_client(self.endpoint, headers=headers or None, timeout_s=self.timeout)
            return client.call_tool(self.tool, arguments)
        except MCPTransportError as exc:
            logger.debug("declarative[%s]: mcp transport error on tool %r: %s",
                         self.name, self.tool, exc)
            return None

    def _results_from(self, data: Any) -> list[dict]:
        """Walk ``results_path`` out of a response object -> the list of raw item dicts."""
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

    def _to_doc(self, item: dict) -> Optional[Document]:
        """Map one raw result dict to a Document via ``field_map`` dot paths.

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
        return Document(
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

    def _docs_from_text_blocks(self, blocks: list) -> list[Document]:
        """mcp text_fallback (opt-in): one Document per prose text block. title = the first
        non-empty line trimmed, content = the whole block, url = ``endpoint#tool`` (a synthetic but
        stable, source-owned handle; the wrapped server exposed no per-item URL). Only reached when
        the row set ``text_fallback: true`` AND the field walk found no structured items, so a
        mapping that stopped matching still fails visibly to [] unless the row consciously opted in."""
        docs: list[Document] = []
        base_url = f"{self.endpoint}#{self.tool}"
        for i, block in enumerate(blocks):
            text = _as_str(block)
            if not text:
                continue
            first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), text[:80])
            docs.append(Document(
                source=self.name,
                source_id=f"{base_url}/{i}",
                url=base_url,
                title=first_line[:200] or "(no title)",
                content=text,
                metadata={"raw": jsonsafe({"_text_block": text})},
            ))
        return docs

    # -- SourceAdapter protocol ------------------------------------------------

    def search(self, query: str, limit: int = 10) -> list[Document]:
        key = cache.make_key(self.name, "search", query, limit)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached

        response = self._fetch_response(self._render_params(query or "", limit))
        items = self._results_from(response)
        docs: list[Document] = []
        if items:
            if not self.post_filter:
                # Server already ranked for this query → keep API order, just truncate
                # (and truncate BEFORE mapping, exactly as the former coded adapters did).
                items = items[:limit]
            for item in items:
                try:
                    doc = self._to_doc(item)
                    if doc:
                        docs.append(doc)
                except Exception as exc:  # noqa: BLE001: one bad row never sinks the batch
                    logger.debug("declarative[%s]: skipping malformed item: %s", self.name, exc)
        elif (self.transport == "mcp" and self.text_fallback
              and isinstance(response, dict) and isinstance(response.get("_text_blocks"), list)):
            # Prose-only tool result + the row opted into text_fallback: synthesize docs. This is
            # the ONLY silent-fallback path, and it is opt-in; a structured result whose field walk
            # simply missed yields [] (fail-visible) instead of falling through to here.
            docs = self._docs_from_text_blocks(response["_text_blocks"])
        elif self.transport == "mcp" and response is not None and not items:
            # A reached-but-unmappable mcp result with NO opt-in: fail VISIBLY (empty + a loud debug
            # log), never degrade a structured miss into prose soup.
            logger.debug("declarative[%s]: mcp tool %r returned no mappable items via results_path "
                         "%r (text_fallback off) -> []", self.name, self.tool, self.results_path)

        if self.post_filter:
            # The ONE shared scorer: filter + rank (title 3x), term-less query keeps order.
            docs = keyword_score_filter(docs, (query or "").strip())[:limit]
        cache.set_docs(key, docs, ttl=self.cache_ttl)
        return docs

    def fetch_url(self, url: str) -> Optional[Document]:
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
        transport=row.get("transport", "http"),
        tool=row.get("tool"),
        text_fallback=row.get("text_fallback", False),
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
