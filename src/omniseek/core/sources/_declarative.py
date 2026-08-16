"""Declarative REST/JSON sources — a standard search-API source is now ONE table row.

Many of OmniSeek's open-API sources are mechanically identical: GET a JSON endpoint
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
      "description":     "... shown in omniseek_list_sources, the agent's router ...",
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
      "auth_prefix":       "",                # prepended to the key, e.g. "Bearer " (RFC-style auth)
      "auth_header":       "",                # header NAME to send the key under; the VALUE is read
                                             #   from ~/.omniseek/credentials/<name>.json -> api_key.
                                             #   Absent file = keyless, unchanged behaviour.
      "no_live_probe":     false,             # True | reason-string | absent: skip the health
                                             #   probe when probing SPENDS the quota it checks
      "needs_credentials": false
    }

TRANSPORT (the meta-source, P10): a row also declares HOW to move bytes. ``transport`` is
``"http"`` (absent == "http", full back-compat: GET/POST the ``endpoint``, above) OR ``"mcp"``
(wrap an external MCP server: the SAME row table, a different transport). A wrapped MCP server
is NOT a new adapter species; it is this same declarative row with ``tools/call`` instead of a
GET, so every memory/dedup/ranking mechanism OmniSeek has applies to it unchanged, and each
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
for omniseek_read routing when set. Credentials for an ``mcp`` row live at
``~/.omniseek/credentials/mcp_<name>.json`` (``{"headers": {...}}``); a ``needs_credentials: true``
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
``walk_packages`` discovery in ``omniseek.server`` skips it. ``declarative_source.py``
(a thin ``*_source.py``) imports + runs ``load_declarative_sources()`` to register
every row — same two-part shape as ``rss_bundles_source.py`` + ``scrape/_rss.py``.
"""

from __future__ import annotations

import functools
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import anyio

from omniseek.core import cache, diag, http
from omniseek.core.normalize import Document, jsonsafe, keyword_score_filter, mk_signal

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

    Mechanism only (OmniSeek's "code is dumb, agent is smart" rule): fetch via the
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
        no_live_probe: Any = False,
        auth_header: str = "",
        auth_prefix: str = "",
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
        self.no_live_probe = no_live_probe  # True | reason string | False/absent
        self.auth_header = auth_header or ""  # header NAME; value comes from the credentials file
        self.auth_prefix = auth_prefix or ""  # e.g. "Bearer " for an Authorization header
        self._auth_hdrs: Optional[dict] = None   # lazily resolved once per process
        self.needs_credentials = bool(needs_credentials)
        self.limit_cap = limit_cap
        self.timeout = timeout
        # post_filter=True (default): re-rank+drop via the shared BM25 scorer (OmniSeek
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
        hdrs = self._auth_headers()
        if self.method == "POST":
            return http.post_json(self.endpoint, json=params, timeout=self.timeout,
                                  **({"headers": hdrs} if hdrs else {}))
        return http.get_json(self.endpoint, params=params, timeout=self.timeout,
                             **({"headers": hdrs} if hdrs else {}))

    def _auth_headers(self) -> Optional[dict]:
        """{header_name: key} from ~/.omniseek/credentials/<name>.json, or None when keyless.

        The row names the HEADER (``auth_header``); the SECRET stays in the credentials file, which is
        the house pattern for every keyed source (adzuna / core / semantic_scholar / ... all ship a
        <name>.json.template beside it) and keeps the key out of sources.json, a file OmniSeek's
        generalization path publishes. Absent / unreadable / empty file = keyless, i.e. EXACTLY the
        previous behaviour, so a row can carry auth_header before any key exists and simply upgrades
        itself the moment the file appears. Resolved once per process (a restart picks up a new key)."""
        if not self.auth_header:
            return None
        if self._auth_hdrs is None:
            key = ""
            try:
                path = Path.home() / ".omniseek" / "credentials" / f"{self.name}.json"
                key = str((json.loads(path.read_text(encoding="utf-8")) or {}).get("api_key") or "")
            except Exception:  # noqa: BLE001 — no key is a normal state, never an error
                key = ""
            self._auth_hdrs = {self.auth_header: f"{self.auth_prefix}{key}"} if key else {}
        return self._auth_hdrs or None

    def _mcp_fetch(self, arguments: dict) -> Any:
        """One ``tools/call`` on the wrapped MCP server -> the parsed tool result object. The
        rendered params ARE the tool arguments (same {query}/{limit} interpolation as http). A row
        with ``needs_credentials`` and no credentials file degrades to None BEFORE any network
        (the podcast_index precedent). Any transport/protocol error (MCPTransportError) -> None so
        the source contract (failure -> []) holds; isError tool results log at debug and -> None."""
        from omniseek.core.sources._mcp import MCPTransportError, get_client, load_mcp_headers

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

    async def _afetch_response(self, params: dict) -> Any:
        """Async egress twin of ``_fetch_response`` -> the raw response OBJECT (an http JSON body, or an
        mcp tool result), None on any failure. Off-loop discipline: the async NETWORK wait stays ON the
        loop (await http.aget_json / apost_json use epoll, no held thread); the SYNC mcp transport is
        pushed OFF the loop via to_thread (a native-async mcp client is a later slice). The SSRF
        getaddrinfo inside the async leaf (http._arequest_capped + AsyncSSRFGuardTransport) is moved off
        the loop by S4b Part 1, so this whole path holds no blocking syscall on the loop."""
        if self.transport == "mcp":
            return await anyio.to_thread.run_sync(self._mcp_fetch, params)  # sync MCP off-loop
        if self.method == "POST":
            return await http.apost_json(self.endpoint, json=params, timeout=self.timeout)
        return await http.aget_json(self.endpoint, params=params, timeout=self.timeout)

    def _results_from(self, data: Any, *, classify: bool = False) -> list[dict]:
        """Walk ``results_path`` out of a response object -> the list of raw item dicts.

        ``classify=True`` (the ``search`` parse path, Wave 3 / 1.16) adds FOUR-STATE drift detection
        so a schema drift stops masquerading as an authoritative empty day. It emits ONE ``diag.note``
        (a no-op unless an /eye-fix drill armed capture, so the broad fan-out pays nothing) when:
          - the ``results_path`` is ABSENT (``_dig`` returned None, whether a segment was missing or a
            JSON null sat at the path: both are shape drift) -> "results_path <p> absent"; or
          - the value at the path is a NON-list, NON-dict scalar -> "resolved to <type>, expected list".
        A reached-but-empty LIST stays SILENT (authoritative empty is a legitimate quiet day), and a
        dict is still tolerated as a ``{id: {...}}`` map -> values (the historical behavior). The hot
        success path is unchanged either way: the note f-strings build only on the drift branches.
        The default (``classify=False``: the health probe and ``_fetch_results``) stays byte-silent."""
        if data is None:
            return []
        results = _dig(data, self.results_path)
        if results is None:
            if classify:
                diag.note(f"{self.name}.parse",
                          body=f"results_path {self.results_path!r} absent from response (schema drift?)")
            return []
        if isinstance(results, dict):  # tolerate {"id": {...}, ...} maps -> values
            results = list(results.values())
        if not isinstance(results, list):
            if classify:
                diag.note(f"{self.name}.parse",
                          body=(f"results_path {self.results_path!r} resolved to "
                                f"{type(results).__name__}, expected list"))
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

    def _assemble_docs(self, response: Any, query: str, limit: int) -> list[Document]:
        """PURE assembly: a raw response -> the ranked ``list[Document]``. No cache, no egress.

        Extracted VERBATIM from ``search``'s former body so BOTH the sync ``search`` and the native
        async ``asearch`` share it: a duplicated parse/map/rank would DRIFT (the parity golden guards
        this). Runs entirely on the CPU (safe to keep on the event loop in the async path)."""
        # classify=True: the parse step notes a missing / wrong-shape results_path (1.16) so a schema
        # drift is not silently indistinguishable from an authoritative empty day (no-op unless a drill
        # armed diag capture).
        items = self._results_from(response, classify=True)
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
            # Item-level fourth state (1.16): the list had items but the field_map extracted NONE (no
            # title/url from any row = field_map drift). The docs that DID parse still return above;
            # only an ALL-fail is noteworthy. Silent when at least one parsed. No-op unless armed.
            if not docs:
                diag.note(f"{self.name}.parse",
                          body=f"0/{len(items)} items parsed (field_map drift? no title/url extractable)")
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
        return docs

    def _cache_decision(self, response: Any, docs: list[Document]) -> tuple[int, bool]:
        """The ``(ttl, authoritative_empty)`` the cache write uses, computed EXACTLY as ``search``
        did (shared by search + asearch so the write policy can never drift between the two paths).

        A failure-empty (egress returned None) must NOT pin [] for the full cache_ttl: that blinds
        a later /eye-fix drill (cache HIT -> zero egress -> empty diag) and masks the wall from real
        queries, exactly the RSS all-feeds-failed case fixed in _rss.py. Only an egress FAILURE gets
        the short floor; a reached-but-empty response (genuine miss / unmappable mcp result) keeps
        the full TTL because it is authoritative, not transient. context7's 24h TTL made this acute.
        authoritative_empty vouches the reached-but-empty (not failed) case so the cache.set FLOOR
        keeps its full cache_ttl (a genuine miss is authoritative, not transient); a failure-empty
        (egress None) gets the short floor. Without this, the FLOOR would cap even the genuine miss."""
        failed = response is None
        ttl = self.cache_ttl if (docs or not failed) else min(300, self.cache_ttl)
        return ttl, (not failed)

    def search(self, query: str, limit: int = 10) -> list[Document]:
        key = cache.make_key(self.name, "search", query, limit)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached

        response = self._fetch_response(self._render_params(query or "", limit))
        docs = self._assemble_docs(response, query, limit)
        ttl, auth = self._cache_decision(response, docs)
        cache.set_docs(key, docs, ttl=ttl, authoritative_empty=auth)
        return docs

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` (S4b): the S4a fan-out awaits this DIRECTLY (no thread), so a
        declarative source's dominant NETWORK wait costs a COROUTINE, not a held pool thread. This makes
        EVERY declarative row AsyncSearchCapable, routing it to the fetcher's native dispatch branch.

        OFF-LOOP DISCIPLINE (the load-bearing rule): a native async method runs ON the loop, so every
        BLOCKING syscall must go OFF it or one slow call freezes every coroutine.
          - the disk cache read + write -> anyio.to_thread.run_sync (get_docs / set_docs do file IO);
          - the SSRF getaddrinfo inside the async egress leaf -> moved off-loop by S4b Part 1;
          - the async NETWORK egress stays ON the loop (await _afetch_response, epoll not a thread);
          - PURE CPU (_assemble_docs: parse/map/BM25) + the (ttl, auth) decision stay ON the loop (fast).
        The fresh / cache_only / refresh_margin contextvars propagate into the worker thread via anyio.
        BEHAVIOR-IDENTICAL to search: the SHARED _assemble_docs + _cache_decision guarantee no drift."""
        key = cache.make_key(self.name, "search", query, limit)
        cached = await anyio.to_thread.run_sync(cache.get_docs, key)  # disk read OFF loop
        if cached is not None:
            return cached

        response = await self._afetch_response(self._render_params(query or "", limit))  # async network
        docs = self._assemble_docs(response, query, limit)  # pure, on loop
        ttl, auth = self._cache_decision(response, docs)
        await anyio.to_thread.run_sync(  # disk write OFF loop
            functools.partial(cache.set_docs, key, docs, ttl=ttl, authoritative_empty=auth))
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
        """Probe with a tiny live request; healthy if the results list is reachable.

        Probes ``_fetch_response`` (not ``_fetch_results``) so a DOWN upstream is caught: the egress
        contract is failure->None WITHOUT raising, so the old ``_fetch_results`` path turned a 403 /
        404 / 500 / timeout into an empty list and reported GREEN, making the entire declarative family
        (the largest source family) invisible to the watchdog. None response = down; a reached
        response with an empty result list for a junk query is still "endpoint + shape OK"."""
        # A probe that SPENDS the thing it is checking must not run. Some rows are metered so tightly
        # that health-probing them is self-destructive: context7 allows 200 requests per calendar MONTH
        # per IP, while the watchdog probes daily (~30/mo) plus every 6h (~120/mo), i.e. ~150 of the 200
        # burned answering "are you up?". The source then reports DOWN for a quota exhaustion OmniSeek
        # itself caused. Same reasoning the health run already uses to skip RETIRED sources ("the retire
        # IS the decision; probing it is noise"), except here it is worse than noise.
        # Reporting True keeps the source USABLE (a False would park it in watchdog_down and hide it);
        # the message is explicit that nothing was verified, mirroring the "degraded, not probed this
        # cycle" semantics the shared-upstream probes adopted 2026-07-25. Breakage still surfaces at USE
        # time: a named drill emits the per-source /eye-fix diagnostic, and these rows are named-only.
        if getattr(self, "no_live_probe", False):
            _why = self.no_live_probe if isinstance(self.no_live_probe, str) else "quota too scarce to probe"
            return True, f"not probed (would spend the metered quota): {_why}"
        try:
            response = self._fetch_response(self._render_params("test", 1))
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        if response is None:
            return False, "endpoint unreachable / non-2xx (egress returned None)"
        items = self._results_from(response)
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
        no_live_probe=row.get("no_live_probe", False),
        auth_header=row.get("auth_header", ""),
        auth_prefix=row.get("auth_prefix", ""),
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
    from omniseek.core.fetcher import register_adapter

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
