"""Minimal streamable-HTTP MCP client: the second TRANSPORT for a declarative row.

STRUCTURAL THESIS: a wrapped MCP server is NOT a new adapter species; it is the SAME
declarative row (``sources.json``) with a different way to move bytes. One row table, one
field_map/facets/cache/admission vocabulary, TWO transports: ``"http"`` (GET/POST a JSON
endpoint, the original ``_declarative`` path) and ``"mcp"`` (JSON-RPC ``tools/call`` over
streamable HTTP, this client). Because a wrapped server lands as an ordinary source, EVERY
memory mechanism OmniSeek already has (thin rows, seen_before, conflicts ratios, similar)
applies to it with ZERO new code; and every wrapped server still earns its slot through the
curator razor PER SERVER (the razor judges the wrapped capability, not the wrapper).

We already SPEAK this protocol server-side (``serve_http`` mounts FastMCP's
``streamable_http_app`` at ``/mcp``); this client pins to the JSON-RPC basics and fails OPEN
on anything exotic. It is httpx-only, ZERO new dependencies. It uses the SHARED pooled client
(``eye.http``) so a wrapped-server endpoint rides the same keep-alive + SSRF guard + size cap
as every other source; row endpoints are OPERATOR-CONFIG data (the same trust class as every
``sources.json`` endpoint). Retrieved tool RESULTS stay UNTRUSTED data (instructions §9),
exactly like every other source.

Protocol shape mirrored (streamable HTTP, one endpoint):
  * POST ``initialize`` -> capture ``Mcp-Session-Id`` (when the server issues one) -> POST the
    ``notifications/initialized`` acknowledgement. The session id rides every later call.
  * POST ``tools/call`` -> parse the result: ``structuredContent`` when present, else the text
    ``content`` blocks (a single block that parses as JSON -> the parsed object; otherwise
    ``{"_text_blocks": [...]}``).
  * A POST response may be ``application/json`` OR ``text/event-stream`` (streamable HTTP allows
    either): SSE is accumulated ``data:`` line by line until the JSON-RPC response with the
    matching id arrives; other events are ignored. Reads are bounded (size + a per-line cap).

FAIL-OPEN contract (the whole point; a wrapped server can never crash OmniSeek or leak):
  * ANY protocol / transport error raises a single ``MCPTransportError``; the adapter layer
    catches it and degrades to ``[]`` (the source contract, like the podcast_index precedent).
  * an ``isError: true`` tool result raises ``MCPTransportError`` carrying the error text; the
    adapter logs it at debug and returns ``[]``.
  * One client per (endpoint, headers) per process; the session is reused, and a 404 / expired
    -session response triggers exactly ONE re-initialize (servers recycle sessions).

Auth: optional per-source credentials at ``~/.omniseek/credentials/mcp_<name>.json``
(``{"headers": {...}}``) read via the ``auth.py`` idiom. A row with ``needs_credentials: true``
and no file degrades to ``[]`` silently at fetch (again the podcast_index precedent); the
adapter checks that BEFORE ever touching the network.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Optional

import httpx

from omniseek.core import _netguard, auth, cache, http

logger = logging.getLogger(__name__)

# The MCP wire protocol version this client OFFERS on initialize. Read from our own MCP
# dependency's constant (the same package FastMCP negotiates against server-side) rather than
# hardcoding a date string that would rot; fall back to a pinned literal only if the import
# shape ever changes. A server that speaks an older version negotiates down in its initialize
# response, which we do not need to inspect (we only send tools/call afterwards).
try:  # cheap import: mcp[cli] is a CORE dependency, already loaded server-side.
    from mcp.types import LATEST_PROTOCOL_VERSION as _PROTOCOL_VERSION
except Exception:  # noqa: BLE001: never let a constant-import shape change break the client
    _PROTOCOL_VERSION = "2025-06-18"  # last-known-good; only reached if the mcp constant moves

# Bounded reads for the SSE accumulation path (the streamable-HTTP resource class). The pooled
# http client already caps a whole body at MAX_BYTES; these bound the line-oriented SSE loop so a
# server that streams forever without ever emitting our response id cannot spin unbounded.
_SSE_MAX_BYTES = 8 * 1024 * 1024   # total SSE bytes accumulated before we give up
_SSE_MAX_EVENTS = 10000            # total SSE events scanned before we give up
_CLIENT_INFO = {"name": "omniseek", "version": "0.1"}

_ACCEPT = "application/json, text/event-stream"


class MCPTransportError(Exception):
    """The ONE error the client raises for any protocol/transport failure OR an isError tool
    result. The adapter layer catches exactly this and degrades to [] (the source contract)."""


# One client per (endpoint, frozenset(headers)) per process; the session is reused across calls.
_CLIENTS: dict[tuple, "MCPClient"] = {}
_CLIENTS_LOCK = threading.Lock()


def get_client(endpoint: str, headers: Optional[dict] = None,
               timeout_s: int = http.DEFAULT_TIMEOUT) -> "MCPClient":
    """Return the process-wide client for (endpoint, headers), building it once. Mirrors the
    shared-pooled-client discipline: repeated tools/call to the same wrapped server reuse ONE
    initialized session instead of re-handshaking every search."""
    key = (endpoint, tuple(sorted((headers or {}).items())))
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(key)
        if client is None:
            client = MCPClient(endpoint, headers=headers, timeout_s=timeout_s)
            _CLIENTS[key] = client
        return client


class MCPClient:
    """A minimal streamable-HTTP MCP client: initialize once, then tools/call. httpx-only,
    fail-open. Not a general MCP SDK: it pins to the exact JSON-RPC subset a retrieval row
    needs (initialize / notifications/initialized / tools/call), and raises MCPTransportError on
    anything it cannot handle so the adapter degrades cleanly."""

    def __init__(self, endpoint: str, headers: Optional[dict] = None,
                 timeout_s: int = http.DEFAULT_TIMEOUT) -> None:
        self.endpoint = endpoint
        self.base_headers = dict(headers or {})
        self.timeout_s = timeout_s
        self.session_id: Optional[str] = None
        self._initialized = False
        self._id = 0
        self._lock = threading.Lock()  # serialize handshake + id issuance for this endpoint

    # -- low-level POST (the ONE network touchpoint; monkeypatched by smoke) ----------------

    def _post(self, payload: dict) -> "tuple[Optional[dict], dict]":
        """POST one JSON-RPC message via the SHARED pooled http client (keep-alive), keeping the
        SAME security guards ``http._request_capped`` applies (SSRF pre-flight + a MAX_BYTES read
        cap + cache-only refusal), but reading the RAW response so we can see the status code the
        MCP session protocol needs: a 404/410 means the server recycled the session (-> re-init
        ONCE), which ``_request_capped`` would otherwise swallow to None via raise_for_status.

        Returns (parsed_response_or_None, response_headers). A NOTIFICATION (no ``id``) or a 202/
        empty body returns (None, headers). RAISES ``_SessionExpired`` on 404/410 and
        ``MCPTransportError`` on any other transport/HTTP failure. Both application/json and
        text/event-stream bodies are handled."""
        if cache.cache_only():
            raise MCPTransportError("cache-only mode: no live MCP call")
        # SSRF pre-flight (the load-bearing guard, identical to http._request_capped): refuse a
        # host that resolves to a private/loopback/link-local/reserved IP before connecting.
        blk = _netguard.security_block_reason(self.endpoint)
        if blk is not None:
            raise MCPTransportError(f"SSRF-class endpoint blocked ({blk})")

        headers = {
            "Content-Type": "application/json",
            "Accept": _ACCEPT,
            **self.base_headers,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        try:
            with http._get_client().stream("POST", self.endpoint, timeout=self.timeout_s,
                                           headers=headers, json=payload) as r:
                status = r.status_code
                resp_headers = dict(r.headers)
                # A 404/410 is the session-recycled signal: surface it to the caller BEFORE reading
                # the body (we re-initialize once and retry).
                if status in (404, 410):
                    raise _SessionExpired(f"session expired (HTTP {status})")
                # Read the body under the SAME MAX_BYTES cap _request_capped enforces.
                raw = bytearray()
                for chunk in r.iter_raw():
                    raw += chunk
                    if len(raw) > http.MAX_BYTES:
                        raise MCPTransportError(f"MCP response exceeded {http.MAX_BYTES} bytes")
        except _SessionExpired:
            raise
        except MCPTransportError:
            raise
        except Exception as exc:  # noqa: BLE001: any httpx raise is a transport error
            raise MCPTransportError(f"POST failed: {type(exc).__name__}: {exc}") from exc

        # A non-2xx that is not a session-expiry: a transport error (the adapter degrades to []).
        if status >= 400:
            raise MCPTransportError(f"MCP endpoint returned HTTP {status}")

        # A notification (no id), or a 202-Accepted / empty body: nothing to parse.
        if payload.get("id") is None:
            return None, resp_headers
        body = bytes(raw)
        if status == 202 or not body.strip():
            return None, resp_headers

        # Rebuild a normal already-read Response from the raw body + original headers (the exact
        # http._request_capped idiom), so content-encoding (gzip/deflate) and charset decoding
        # happen as a buffered read would; iter_raw() gave the UNDECODED transfer bytes, and a
        # naive utf-8 decode of a gzipped body would be garbage that silently degrades to [].
        text = httpx.Response(status, headers=resp_headers, content=body).text
        ctype = (resp_headers.get("content-type") or "").lower()
        if "text/event-stream" in ctype:
            parsed = self._parse_sse(text, payload.get("id"))
        else:
            parsed = self._parse_json_body(text)
        return parsed, resp_headers

    @staticmethod
    def _parse_json_body(text: str) -> dict:
        try:
            obj = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            raise MCPTransportError(f"non-JSON response body: {exc}") from exc
        if not isinstance(obj, dict):
            raise MCPTransportError("JSON-RPC response is not an object")
        return obj

    @staticmethod
    def _parse_sse(text: str, want_id: Any) -> dict:
        """Accumulate an SSE stream: read ``data:`` lines, parse each accumulated event as a
        JSON-RPC message, and return the one whose ``id`` matches ``want_id``. Other events
        (notifications, pings, log messages) are ignored. Bounded by size + event count so a
        never-terminating stream gives up with a MCPTransportError instead of spinning."""
        seen_bytes = 0
        events = 0
        data_lines: list[str] = []

        def _flush(lines: list[str]) -> Optional[dict]:
            if not lines:
                return None
            blob = "\n".join(lines).strip()
            if not blob:
                return None
            try:
                obj = json.loads(blob)
            except Exception:  # noqa: BLE001: a non-JSON event (comment/ping) is simply skipped
                return None
            if isinstance(obj, dict) and obj.get("id") == want_id:
                return obj
            return None

        for raw_line in text.splitlines():
            seen_bytes += len(raw_line) + 1
            if seen_bytes > _SSE_MAX_BYTES:
                raise MCPTransportError("SSE stream exceeded byte cap without a matching response")
            line = raw_line.rstrip("\r")
            if line == "":  # blank line ends an SSE event -> try to parse what we accumulated
                events += 1
                if events > _SSE_MAX_EVENTS:
                    raise MCPTransportError("SSE stream exceeded event cap without a matching response")
                hit = _flush(data_lines)
                data_lines = []
                if hit is not None:
                    return hit
                continue
            if line.startswith(":"):  # an SSE comment / heartbeat
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
            # other SSE fields (event:, id:, retry:) are not needed for JSON-RPC framing
        # stream ended: one last unterminated event may still hold our response
        hit = _flush(data_lines)
        if hit is not None:
            return hit
        raise MCPTransportError("SSE stream ended without a matching JSON-RPC response")

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    @staticmethod
    def _raise_on_jsonrpc_error(msg: dict) -> dict:
        """A JSON-RPC error object -> MCPTransportError. Returns the ``result`` on success."""
        if "error" in msg and msg["error"] is not None:
            err = msg["error"]
            detail = err.get("message") if isinstance(err, dict) else str(err)
            raise MCPTransportError(f"JSON-RPC error: {detail}")
        result = msg.get("result")
        if not isinstance(result, dict):
            raise MCPTransportError("JSON-RPC response has no result object")
        return result

    # -- handshake -------------------------------------------------------------------------

    def initialize(self) -> None:
        """Run the MCP handshake: POST ``initialize`` (capturing ``Mcp-Session-Id``), then POST the
        ``notifications/initialized`` acknowledgement. Idempotent-ish: safe to call again after a
        session expiry (it re-handshakes). RAISES MCPTransportError on failure."""
        with self._lock:
            self._do_initialize()

    def _do_initialize(self) -> None:
        self.session_id = None  # a fresh handshake gets a fresh session id
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        }
        msg, resp_headers = self._post(payload)
        # Capture the session id the server minted (header name is case-insensitive per HTTP).
        sid = resp_headers.get("mcp-session-id") or resp_headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        if msg is None:
            raise MCPTransportError("initialize returned no response body")
        self._raise_on_jsonrpc_error(msg)
        # Acknowledge: notifications/initialized carries no id and expects no response.
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        self._initialized = True

    # -- the one useful call ---------------------------------------------------------------

    def call_tool(self, name: str, arguments: dict) -> dict:
        """JSON-RPC ``tools/call`` -> the parsed result object. Initializes the session on first
        use; re-initializes ONCE on a session-expiry response and retries. Parses the MCP tool
        result: ``structuredContent`` when present, else the text ``content`` blocks (a single
        block parsing as JSON -> that object; otherwise ``{"_text_blocks": [...]}``). An
        ``isError: true`` result RAISES MCPTransportError carrying the error text.

        RAISES MCPTransportError on any transport/protocol failure (the adapter catches it -> [])."""
        try:
            return self._call_tool_once(name, arguments)
        except _SessionExpired:
            # Servers recycle sessions; re-handshake ONCE, then retry the call a single time.
            logger.debug("mcp[%s]: session expired, re-initializing once", self.endpoint)
            self.initialize()
            try:
                return self._call_tool_once(name, arguments)
            except _SessionExpired as exc:
                raise MCPTransportError(f"session expired again after re-initialize: {exc}") from exc

    def _call_tool_once(self, name: str, arguments: dict) -> dict:
        if not self._initialized:
            self.initialize()
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
        msg, _ = self._post(payload)
        if msg is None:
            raise MCPTransportError("tools/call returned no response body")
        result = self._raise_on_jsonrpc_error(msg)
        return self._parse_tool_result(result)

    @staticmethod
    def _parse_tool_result(result: dict) -> dict:
        """Turn an MCP ``CallToolResult`` into a plain result object for the field walk.

        Precedence: an ``isError`` result raises; ``structuredContent`` (the machine-readable
        payload the spec added for exactly this) wins when present; otherwise the text ``content``
        blocks are concatenated: a single block that parses as JSON becomes that object, else the
        blocks are returned under ``_text_blocks`` (the row's ``text_fallback`` opt-in consumes
        that shape; without the opt-in the mapping simply fails visibly to [])."""
        if result.get("isError"):
            blocks = _text_blocks_of(result.get("content"))
            raise MCPTransportError(f"tool returned isError: {' '.join(blocks)[:500] or '(no text)'}")
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        blocks = _text_blocks_of(result.get("content"))
        if len(blocks) == 1:
            try:
                obj = json.loads(blocks[0])
            except Exception:  # noqa: BLE001: not JSON, fall through to the text-block shape
                obj = None
            if isinstance(obj, (dict, list)):
                return {"_json": obj} if isinstance(obj, list) else obj
        return {"_text_blocks": blocks}


def _text_blocks_of(content: Any) -> list[str]:
    """Extract the ``text`` field of every text-type content block; ignore non-text blocks
    (image/audio/resource). Tolerant of a bare string or a malformed list."""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            txt = block.get("text")
            if isinstance(txt, str) and txt:
                out.append(txt)
        elif isinstance(block, str) and block:
            out.append(block)
    return out


class _SessionExpired(Exception):
    """Internal: a 404/410 that means the server recycled the session. Caught inside call_tool to
    trigger exactly ONE re-initialize; never escapes to the adapter (it becomes MCPTransportError
    if the retry also fails)."""


def load_mcp_headers(name: str) -> "tuple[Optional[dict], bool]":
    """Read per-source MCP credentials at ~/.omniseek/credentials/mcp_<name>.json via the auth.py
    idiom. Returns (headers, configured): ({...}, True) when the file supplies a ``headers`` dict,
    ({}, False) when no file exists. A malformed file (present but no headers dict) is treated as
    NOT configured so a needs_credentials row degrades to [] rather than sending junk headers."""
    creds = auth.load(f"mcp_{name}")
    if not isinstance(creds, dict):
        return {}, False
    headers = creds.get("headers")
    if isinstance(headers, dict) and headers:
        return {str(k): str(v) for k, v in headers.items()}, True
    return {}, False
