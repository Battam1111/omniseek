"""Shared HTTP helpers — one User-Agent, one timeout policy, one error contract.

One implementation, async (EYE-TWINS-1, 2026-11-10): every helper here is an ``a``-prefixed
coroutine; the sync names (``get``, ``get_json``, ``get_text``, ``post_json``, ``direct``,
``get_impersonated``, ``download_to_file``) are one-line entries for a caller on a thread, which
run the coroutine on the event loop through ``portal.call``. Never call a sync entry on the event
loop's own thread (portal raises): await the coroutine there. The sync request/response hooks kept
below (``progress_hooks``) are for a third-party sync client OmniSeek does not build itself (atproto).

~20 adapters each re-implemented the same wrapper: ``httpx.get`` + a UA header +
``follow_redirects=True`` + ``raise_for_status()`` + ``try/except → None`` (with
4+ divergent UA strings). These helpers centralize that. They return ``None`` on
*any* failure (logged), so adapters keep their "failure → empty result" contract
without per-file boilerplate.

Open-API adapters SHOULD use these: routing through the shared client is what earns the
diag.note evidence tap (a failure branch here records the status + body for /eye-fix), the
pooling, the 30MB cap, and the SSRF guard. Some open-API adapters still fetch with a bare
``httpx`` call and only log on failure, so they are INVISIBLE to a drill's diagnostic; those
that cannot route through here (a genuinely oversized download, a non-JSON transport) must at
least add a ``diag.note(...)`` in their own failure branch (the levels_fyi / rss precedent).
Anti-bot / walled adapters (mokahr / feishu / xiaohongshu / bytedance) keep their own bespoke
headers + signing and diag.note by hand — do NOT route those through here.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import importlib.util
import logging
import os
import sys
import threading
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

import anyio
import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_fixed, wait_random

from omniseek.core import _guard, _netguard, _probe, cache, diag, portal, upstreams

logger = logging.getLogger(__name__)

# NO SYS.PATH SCAN PER ASYNC LOCK (OmniSeek redo item 4, 2026-10-10). httpcore 1.0.x asks which async
# library is running by doing `import sniffio` inside every lock, event and cancel-shield setup, that
# is several times per request. anyio 4.x no longer installs sniffio, and a FAILED import is not
# cached: each call re-scans sys.path with stat syscalls (measured 48us against 0.4us). On a cold
# broad sweep that is thousands of scans on the event loop, and each stat releases the GIL, so while
# worker threads parse feeds the loop waits up to a thread switch interval per stat to get it back.
# A None entry in sys.modules makes the import fail at once with the same ImportError, so httpcore
# takes the same "asyncio" branch it takes today. Only set when sniffio is genuinely not installed.
if importlib.util.find_spec("sniffio") is None:
    sys.modules.setdefault("sniffio", None)

# DECLARED UPSTREAM GATE (task R, 2026-09-28). Every request through these helpers first takes the
# gate of the upstream its host is declared under in upstreams.json (the SAME BackendGuard the owning
# module uses), per attempt, holding it through the body read; an undeclared or ungated host passes
# straight through, and a caller already inside that upstream's hold (the owning module) is not gated
# twice. Rate-limit headers are recorded for the health block, and a 429 carrying Retry-After defers
# the upstream for every caller. A request the gate cannot admit in time is NOT sent: the helper
# returns None like any other failure, with a diag note naming the gate.


# A connection that was never made is a request that was never sent: the gate hands back its
# reserved start (review F8).
_guard.register_unsent(httpx.ConnectError, httpx.ConnectTimeout)


def _gate_note(method: str, url: str, exc: BaseException) -> None:
    logger.info("http.%s: %s", method.lower(), exc)
    diag.note(f"http.{method.lower()}", url=url, status=None,
              body=f"declared upstream gate, request not sent: {exc}")


# ONE CONNECTION AT A TIME (arXiv's terms: "limit requests to a single connection at a time"). The
# declared gate already lets only one request to such a host be in flight, but a pooled keep-alive
# connection would stay open after the response, and HTTP/2 (when h2 is installed) keeps its
# connection open by design. So those hosts get their own transport, mounted on the shared client: HTTP/1.1 only, at most one connection, no keep-alive (the connection closes
# when the response is done), and every request to them says "Connection: close". Which hosts: the
# declared upstreams whose published terms say max_concurrency 1 (upstreams.single_connection_hosts).
def _one_connection_limits() -> httpx.Limits:
    return httpx.Limits(max_connections=1, max_keepalive_connections=0, keepalive_expiry=0.0)


def _one_connection_transport():
    """The raw transport for a one-connection host (wrapped in the SSRF guard by the client)."""
    return httpx.AsyncHTTPTransport(http2=False, limits=_one_connection_limits())


# DECLARED USER-AGENT (design decision, 2026-09-29): the User-Agent a host's upstream requires lives
# in upstreams.json ("user_agent"), in one place. The initial request gets it in _arequest_capped;
# every HOP (redirects included) gets it from these request hooks on the shared client, and a hop that leaves such a host gets the shared USER_AGENT back.
def _apply_declared_user_agent(request: "httpx.Request") -> None:
    ua = upstreams.user_agent_for(str(request.url))
    if ua:
        request.headers["User-Agent"] = ua
    elif request.headers.get("User-Agent") in upstreams.declared_user_agents():
        request.headers["User-Agent"] = USER_AGENT   # a redirect off a declared host: not theirs


async def _aapply_declared_user_agent(request: "httpx.Request") -> None:
    _apply_declared_user_agent(request)


# THE REDIRECT RULE ON EVERY HOP (design decision 1, 2026-09-29). The layers that send one request set the
# request's upstreams.AsyncHopGates here for as long as it runs; this request hook, installed on the
# shared client and on every client ``adirect`` builds, hands each hop httpx sends (redirects included) to it:
# same host, the same visit; another host, its own gates, taken after the last host's are let go. The
# rule itself lives in HopGates only.
_hops_var: contextvars.ContextVar = contextvars.ContextVar("omniseek_eye_hops", default=None)


async def _aapply_hop_gates(request: "httpx.Request") -> None:
    gates = _hops_var.get()
    if isinstance(gates, upstreams.AsyncHopGates):
        await gates.enter(str(request.url))


# HOW MUCH A FETCH STEP READ (2026-11-04). A source base opens
# ``count_bytes()`` around its fetch step; every answer these helpers return while it is open (the
# capped helpers and ``adirect``, with their sync entries) adds its body length, after content decoding. A task or a
# worker thread started inside copies the context and so adds to the same tally. The parse step then
# knows the answer's size and runs here or in the process lane (sources._base.aparse).
_tally_var: contextvars.ContextVar = contextvars.ContextVar("omniseek_eye_fetched_bytes", default=None)


@contextlib.contextmanager
def count_bytes():
    """Yields a one-item list: the bytes of the answers read in this context while the block runs."""
    tally = [0]
    token = _tally_var.set(tally)
    try:
        yield tally
    finally:
        _tally_var.reset(token)


def _counted(resp: Any) -> Any:
    tally = _tally_var.get()
    if tally is not None and resp is not None:
        try:
            tally[0] += len(resp.content)
        except httpx.ResponseNotRead:  # a streamed answer the caller reads itself is not counted
            pass
    return resp


def _timeout_s(timeout: Any) -> Optional[float]:
    """A request's own timeout in seconds, for the gates' leases (review P1): a number as it is; an
    ``httpx.Timeout`` (or the dict httpx keeps in a request's extensions) its longest phase, which is
    per operation, so a long body can outlive it (the lease then ends early and the gate may admit one
    more request, never fewer); None when unknown (the gate falls back to its declared max_wait_s)."""
    if timeout is None or timeout is _UNSET:
        return None
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
        return float(timeout)
    parts = timeout.values() if isinstance(timeout, dict) else (
        getattr(timeout, k, None) for k in ("connect", "read", "write", "pool"))
    nums = [float(v) for v in parts if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return max(nums) if nums else None


def _resp_url(resp: Any, fallback: str) -> str:
    """The URL a response came from (the last hop), or ``fallback`` when a stub response has none."""
    try:
        return str(resp.url or fallback)
    except Exception:  # noqa: BLE001 (httpx raises when no request is attached)
        return fallback


class _ProgressStream(httpx.SyncByteStream):
    """A response body that renews the leases of its request's holds at every block of data (design
    decision of 2026-09-29 on section 17.5, item 2): a request that is making progress keeps its
    permits; one that is stuck makes none and is reclaimed when its lease runs out. The holds are the
    ones running when the response arrived, so the renewal reaches them wherever the body is read
    (inside httpx for a plain ``get``, or by the caller for a stream)."""

    def __init__(self, inner: Any, handles: tuple) -> None:
        self._inner, self._handles = inner, handles

    def __iter__(self):
        for chunk in self._inner:
            _guard.renew_handles(self._handles)
            yield chunk

    def close(self) -> None:
        self._inner.close()


class _AProgressStream(httpx.AsyncByteStream):
    """Async twin of ``_ProgressStream``."""

    def __init__(self, inner: Any, handles: tuple) -> None:
        self._inner, self._handles = inner, handles

    async def __aiter__(self):
        async for chunk in self._inner:
            _guard.renew_handles(self._handles)
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


# CONNECTED, SENT, AND THE HEADERS (design decisions of 2026-09-29 on review X section 12.8, Q1). Before
# its first byte a request makes progress when its connection is made, when TLS is set up on it, when the
# whole request has been written and when its response headers arrive. Each renews the leases of the
# holds it runs under, so a slow connection or a slow first byte inside the request's own timeout keeps
# the permit. The first three are httpcore's trace events "connection.connect_tcp.complete",
# "connection.start_tls.complete" and "<http11|http2>.send_request_body.complete", reached through the
# request's "trace" extension, which the request hook sets; the headers are the response hook.
_PROGRESS_EVENTS = ("connect_tcp.complete", "start_tls.complete", "send_request_body.complete")


def _sent_trace(handles: tuple, prior: Any):
    def trace(name: str, info: dict) -> None:
        if name.endswith(_PROGRESS_EVENTS):
            _guard.renew_handles(handles)
        if prior is not None:
            prior(name, info)
    trace._omniseek_prior = prior   # a redirect hop carries the extensions on: never chain our own
    return trace


def _asent_trace(handles: tuple, prior: Any):
    async def trace(name: str, info: dict) -> None:
        if name.endswith(_PROGRESS_EVENTS):
            _guard.renew_handles(handles)
        if prior is not None:
            await prior(name, info)
    trace._omniseek_prior = prior
    return trace


def _prior_trace(request: "httpx.Request") -> Any:
    prior = request.extensions.get("trace")
    return getattr(prior, "_omniseek_prior", prior)


def _watch_send(request: "httpx.Request") -> None:
    """Request hook: when this request's connection is made, when TLS is set up and when the request has
    been sent, renew the holds it runs under."""
    handles = _guard.active_handles()
    if handles:
        request.extensions["trace"] = _sent_trace(handles, _prior_trace(request))


async def _awatch_send(request: "httpx.Request") -> None:
    """Async twin of ``_watch_send`` (httpcore awaits an async client's trace callback)."""
    handles = _guard.active_handles()
    if handles:
        request.extensions["trace"] = _asent_trace(handles, _prior_trace(request))


def _watch_progress(response: "httpx.Response") -> None:
    handles = _guard.active_handles()
    if handles:
        _guard.renew_handles(handles)   # the headers arrived
        if isinstance(response.stream, httpx.SyncByteStream):
            response.stream = _ProgressStream(response.stream, handles)


def _awatch_progress(response: "httpx.Response") -> None:
    handles = _guard.active_handles()
    if handles:
        _guard.renew_handles(handles)
        if isinstance(response.stream, httpx.AsyncByteStream):
            response.stream = _AProgressStream(response.stream, handles)


def progress_hooks() -> dict:
    """The event hooks that give a plain sync ``httpx.Client`` (a module's own, one that takes its gates
    with ``hold``/``slot``/``egress`` around the request) the lease renewal by progress: when the
    request has been sent, when its headers arrive, and at every block of its body."""
    return {"request": [_watch_send], "response": [_watch_progress]}


def aprogress_hooks() -> dict:
    """Async twin of ``progress_hooks``."""
    async def hook(response: "httpx.Response") -> None:
        _awatch_progress(response)
    return {"request": [_awatch_send], "response": [hook]}


def _aresponse_hook(defer_on_429: bool = True):
    """The response hook of every hop-aware client: EVERY response it receives (each redirect hop and
    the final one) is recorded on its own host, once (design decision of 2026-09-29 on section 16.5,
    item 2); the layer that returns the final response records it as well, which is then a no-op
    (upstreams.observe_response marks a recorded response); its body renews its holds' leases as it
    arrives. ``defer_on_429=False`` only for the client of a module listed in upstreams.SELF_BACKOFF."""
    async def hook(response: "httpx.Response") -> None:
        upstreams.observe_response(str(response.request.url), response, defer_on_429=defer_on_429)
        _awatch_progress(response)
    return hook


_aobserve_hop = _aresponse_hook()      # the shared client's response hook


def _with_connection_close(url: str, headers: dict) -> dict:
    """Add ``Connection: close`` for a one-connection host; other hosts are untouched."""
    if upstreams.is_single_connection_host(url):
        return {**headers, "Connection": "close"}
    return headers

USER_AGENT = "Mozilla/5.0 (compatible; omniseek/0.1)"
DEFAULT_TIMEOUT = 20
MAX_BYTES = 30 * 1024 * 1024  # 30MB hard cap on a single response body — a feed/JSON
                              # bigger than this is almost certainly a hijack/misconfig;
                              # stream + abort rather than buffer it all and OOM the daemon.
# A DOCUMENT (a paper PDF, an arXiv HTML page, a slide deck) is not an API answer: it streams to a file
# through download_to_file and never sits whole in memory, so it gets its own cap (EYE-RSS-3,
# 2026-10-24: openreview.net/forum?id=jU7AbGq3se is 31,551,840 bytes and the 30MB answer cap above
# refused it). PROVISIONAL: 200MB, below the 500MB the same download path allows for audio
# (asr._AUDIO_DL_MAX_BYTES); calibrated against 20 arXiv and 20 OpenReview paper PDFs, readings in
# out/EYE-RSS-3/report.md. Raise it from those readings, not by guess.
DOC_MAX_BYTES = 200 * 1024 * 1024
# Bytes of a non-2xx body kept for the diag capture: an error message fits many times over, and a
# huge or endless error body costs at most this much read.
_ERROR_BODY_CAP = 64 * 1024
_TRANSIENT_RETRY_DELAY_S = 0.5
_TRANSIENT_RETRY_JITTER_S = 0.1


_TRANSIENT_WAIT = wait_fixed(_TRANSIENT_RETRY_DELAY_S) + wait_random(0.0, _TRANSIENT_RETRY_JITTER_S)


def _transient_retry(retry_transient: bool, url: str) -> dict:
    """The request helpers' tenacity settings: when ``retry_transient``, one retry of a connect-phase
    failure (_is_transient_connect_error) after 0.5s plus up to 0.1s of jitter, noted in diag first;
    anything else, and the second failure, is raised as it is to the helper's own failure handling."""
    return dict(
        stop=stop_after_attempt(2 if retry_transient else 1),
        wait=_TRANSIENT_WAIT,
        retry=retry_if_exception(_is_transient_connect_error),
        before_sleep=lambda state: diag.note("http.retry_transient", url=url, exc=state.outcome.exception()),
        reraise=True,
    )


def _is_transient_connect_error(exc: BaseException) -> bool:
    return isinstance(exc, httpx.ConnectError) and not str(exc).startswith(
        "refused SSRF-class url")


# OPENSSL FAILED, LIBCURL MAY NOT. Some hosts fail openssl's handshake (the one httpx uses) on and
# off while libcurl's handshake to the same host goes through, seconds apart. Either something on
# the way mangles openssl's handshake, or the site (or a machine in front of it) answers one shape of
# handshake only some of the time. download_to_file and get_impersonated run on the libcurl tier for
# this reason, and the request helper (_arequest_capped) falls back to it once when openssl failed
# before the request went out.
#   2026-09-09, canada_jobbank_wages: httpx SSL-EOF, curl 200 in 2.6 s.
#   2026-11-10, same source down again from 03:47 (source fault mail #3097, http.get ConnectTimeout).
#   Measured on the host machine through OmniSeek's own egress, one new process per try, against
#   https://www.jobbank.gc.ca/core/ta-jobtitle_en/select: curl 29/29 fine; a stdlib handshake with no
#   ALPN, "h2,http/1.1" or "http/1.1,h2" 38/38 fine (0.9 to 5.2 s); offering ALPN "http/1.1" alone
#   (what httpx offers without h2, as in OmniSeek's env) the handshake got no answer until the timeout
#   (_ssl.c:993 "The handshake operation timed out", httpx ConnectTimeout): http.get_json 8 of 17,
#   bare httpx.get 4 of 6, stdlib 4 of 12; http.get_impersonated (libcurl) 8/8 fine, 17077 bytes each.
#   So the site is flaky on that handshake shape, the egress is fine, and a handshake TIMEOUT
#   (ConnectTimeout) must reach the fallback too, not only a handshake that broke off.
_TLS_MANGLED_MARKERS = (
    "UNEXPECTED_EOF_WHILE_READING",
    "SSLError",
    "SSLEOFError",
    "[SSL:",
    "Server disconnected without sending a response",
)
_CURL_TIER_METHODS = ("GET", "POST")


def _openssl_failed_unsent(exc: BaseException) -> bool:
    """True when openssl's side (httpx) failed before the request went out, in a way the libcurl tier
    is known to get past: the connect phase timed out (``httpx.ConnectTimeout``: TCP or TLS handshake),
    or the handshake broke off (the markers above).

    Deliberately narrow: an HTTP status, a read / write / pool timeout, or an SSRF refusal is NOT this.
    Those requests were sent (or never reached the wire for our own reasons); sending them again on a
    second transport would spend the upstream's budget twice and could hide a real answer.
    """
    if isinstance(exc, httpx.ConnectTimeout):
        return True
    if isinstance(exc, httpx.HTTPStatusError) or isinstance(exc, httpx.TimeoutException):
        return False
    if str(exc).startswith("refused SSRF-class url"):
        return False
    # The markers are looked for down the exception chain: the async backend (anyio's TLS stream)
    # turns openssl's SSLEOFError into ``raise EndOfStream from None``, so the async helper's
    # ConnectError reads "" and the broken handshake is named only in the chain's __context__ (seen
    # 2026-11-10, httpx 0.28.1, anyio 4.13; the sync backend raises it with the SSL text itself).
    seen: set = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if any(m in str(cur) for m in _TLS_MANGLED_MARKERS):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _curl_tier_may_serve(method: str, exc: BaseException) -> bool:
    """Whether the libcurl tier will try this failed request next. When it will, the request helpers
    let go of the hop's gates without counting the failure against the host yet
    (``upstreams.HopGates.let_go``) and keep it for ``_count_unreached``: a host libcurl reaches is
    not a host that is down."""
    return method.upper() in _CURL_TIER_METHODS and _openssl_failed_unsent(exc)


def _count_unreached(deferred: list) -> None:
    """Count the connections httpx could not make against their host (``upstreams.note_host_failure``,
    what the hop gates do for any other failure), once the request ended without any answer."""
    for host, exc in deferred:
        upstreams.note_host_failure(host, exc)
    deferred.clear()


async def _acurl_tier_retry(method: str, url: str, timeout: int, headers: Optional[dict],
                            json_body: Any = None, params: Any = None) -> Optional[httpx.Response]:
    """One retry through the libcurl tier, rebuilt as the Response the caller expects.

    Returns None for anything it cannot serve (a verb outside GET/POST, a missing dep, a non-2xx),
    so the caller falls through to its normal None. The rebuilt Response carries the body but NOT
    the upstream headers, which is enough for .text / .json() and is why this stays a fallback
    rather than the default tier. libcurl itself blocks: each hop runs on the IO lane (_acurl_hops).

    The same request httpx tried also means its headers: only the TLS handshake is Chrome's here, not
    Chrome's navigation headers (``default_headers=False``), and a caller that gave no Accept gets
    httpx's default ``*/*``. Chrome's own set (Accept ``text/html,...``, ``Sec-Fetch-Dest: document``)
    reads to a site as a page load, and an API can answer that with a web page: api.openreview.net/notes,
    keyless, answers ``*/*`` with 403 JSON (ChallengeRequiredError) but either of those two with a 302 to
    openreview.net/challenge, whose 200 HTML this tier then served as the body (EYE-TWINS-1 live
    evidence; re-measured with curl and curl_cffi 0.15.0, SMALL-BATCH-9).
    """
    if method.upper() not in _CURL_TIER_METHODS:
        return None
    if params:   # the same request httpx tried: its query string goes into the URL here
        url = str(httpx.Request(method, url, params=params).url)
    headers = dict(headers or {})
    if not any(str(k).lower() == "accept" for k in headers):
        headers["Accept"] = "*/*"   # httpx's default, what the first attempt sent
    body = await _aimpersonated_request(method, url, timeout=timeout, headers=headers,
                                        json_body=json_body, check_host=False, browser_headers=False)
    if body is None:
        return None
    diag.note("http.curl_tier_retry", url=url, body="httpx transport failed; libcurl tier served it")
    logger.info("http.%s: httpx transport failed, libcurl tier served %s (%d bytes)",
                method.lower(), url, len(body))
    return httpx.Response(200, content=body, request=httpx.Request(method, url))


def _http2_ok() -> bool:
    try:
        import h2  # noqa: F401  — optional; absence just means HTTP/1.1 keep-alive
        return True
    except Exception:  # noqa: BLE001
        return False


def _decode_error_body(r: httpx.Response, data: bytes) -> str:
    try:
        return data.decode(r.encoding or "utf-8", errors="replace")
    except LookupError:  # a charset Python does not know
        return data.decode("utf-8", errors="replace")


async def _aread_error_body(r: httpx.Response) -> Optional[str]:
    """At most ``_ERROR_BODY_CAP`` bytes of a non-2xx streamed body, decoded, for the diag capture.
    Fail-open: a body that cannot be read is None, and the caller still raises on the status."""
    try:
        buf = bytearray()
        chunks = r.aiter_bytes()
        try:
            async for chunk in chunks:
                buf += chunk
                if len(buf) >= _ERROR_BODY_CAP:
                    break
        finally:
            await chunks.aclose()  # stopping early must not leave the generator for the loop to finalize
        return _decode_error_body(r, bytes(buf[:_ERROR_BODY_CAP]))
    except Exception:  # noqa: BLE001
        return None


def _direct_ua_hook(original_ua: Optional[str]):
    """The declared User-Agent on every hop of an ``adirect`` request; a hop that leaves a declared host
    gets the caller's own User-Agent back (not the shared one)."""
    def hook(request: "httpx.Request") -> None:
        ua = upstreams.user_agent_for(str(request.url))
        if ua:
            request.headers["User-Agent"] = ua
        elif original_ua and request.headers.get("User-Agent") in upstreams.declared_user_agents():
            request.headers["User-Agent"] = original_ua
    return hook


def ahop_hooks(original_ua: Optional[str] = None, *, defer_on_429: bool = True) -> dict:
    """The event hooks that give a caller-built ``httpx.AsyncClient`` the redirect rule, the declared
    User-Agent and the recording of every response on its own host, on every hop (pass as
    ``event_hooks=``; use with ``adirect(..., client=...)``). ``defer_on_429=False`` only for a module
    listed in upstreams.SELF_BACKOFF."""
    ua_hook = _direct_ua_hook(original_ua)

    async def _aua(request: "httpx.Request") -> None:
        ua_hook(request)
    return {"request": [_aapply_hop_gates, _aua, _awatch_send], "response": [_aresponse_hook(defer_on_429)]}


_UNSET: Any = object()

# One TLS context for the one-shot clients of direct / adirect (EYE-ADIRECT-SSL, 2026-11-03). httpx builds a
# fresh context (loading the CA bundle) for every client it constructs: 6.36 ms per client on the host machine against
# 0.05 ms with a shared one (measured 11-03 with OmniSeek's httpx 0.28.1), and a broad search builds about 20 of
# them on the event loop through adirect. A context keeps no per-connection state; each client is still
# one-shot, so no cookie or connection crosses calls.
_SSL_CTX: Any = None
_SSL_LOCK = threading.Lock()


def _shared_ssl() -> Any:
    global _SSL_CTX
    if _SSL_CTX is None:
        with _SSL_LOCK:
            if _SSL_CTX is None:
                _SSL_CTX = httpx.create_ssl_context()
    return _SSL_CTX


def direct(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Sync entry for a caller on a thread: ``adirect`` on the event loop (portal.call), arguments as
    they are (a ``client`` must then be an ``httpx.AsyncClient`` built with ``ahop_hooks``)."""
    return portal.call(adirect, method, url, **kwargs)


async def adirect(method: str, url: str, *, client: Optional[httpx.AsyncClient] = None,
                  follow_redirects: bool = False, timeout: Any = _UNSET,
                  **kwargs: Any) -> httpx.Response:
    """ONE request outside the shared pool, for the modules that keep their own headers or client:
    ``httpx.request`` with the declared gates on every hop (the redirect rule of
    ``upstreams.AsyncHopGates``), the declared User-Agent on every hop, and each hop's rate-limit
    headers recorded (a Retry-After defers the upstream). Returns the read response; raises what httpx
    raises, and ``upstreams.UpstreamBusy`` (nothing sent) when a gate cannot admit a hop in time.
    ``client``: the module's own client, built with ``event_hooks=ahop_hooks(...)``; else a one-shot
    client."""
    headers = kwargs.get("headers") or {}
    ua = next((v for k, v in headers.items() if str(k).lower() == "user-agent"), None)
    own = client is None
    if own:
        c = httpx.AsyncClient(event_hooks=ahop_hooks(ua), verify=_shared_ssl(),
                              timeout=DEFAULT_TIMEOUT if timeout is _UNSET else timeout)
    else:
        c = client
        if timeout is not _UNSET:
            kwargs["timeout"] = timeout
    try:
        async with upstreams.ahop_gates(request_s=_timeout_s(kwargs.get("timeout", c.timeout))) as gates:
            await gates.enter(url)
            token = _hops_var.set(gates)
            try:
                r = await c.request(method, url, follow_redirects=follow_redirects, **kwargs)
            finally:
                _hops_var.reset(token)
        upstreams.observe_response(str(r.url), r)
        return _counted(r)
    finally:
        if own:
            await c.aclose()


# A MODULE'S OWN CLIENT UNDER THE REDIRECT RULE (design decision 1, 2026-09-29). A module that keeps its
# own httpx client (its own headers, cookies, pool or HTTP/2) builds it as AsyncHopClient instead of
# httpx.AsyncClient, and a one-off streamed download uses adirect_stream instead of httpx.stream. Every
# request such a client sends (get, post, stream, ...) then passes the declared gates of each hop
# through upstreams.AsyncHopGates, gets the declared User-Agent on each hop, and has every response
# recorded on its own host, the final one included (design decision of 2026-09-29 on section 16.5,
# item 2; a module listed in upstreams.SELF_BACKOFF builds its client with defer_on_429=False). A
# streamed response keeps its gates until it is closed. Inside ``adirect``, which carries the
# request's gates itself, the client takes no gate of its own.
class _AGatedStream(httpx.AsyncByteStream):
    """A streamed body that lets go of its request's gates when it is closed."""

    def __init__(self, inner: Any, release: Any) -> None:
        self._inner, self._release = inner, release

    async def __aiter__(self):
        async for chunk in self._inner:
            yield chunk

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        finally:
            release, self._release = self._release, None
            if release is not None:
                await release()


def _hooks_with(rule: dict, extra: Optional[dict]) -> dict:
    """The redirect-rule hooks first, then any hooks the module passes itself."""
    out = {k: list(v) for k, v in rule.items()}
    for k, v in (extra or {}).items():
        out.setdefault(k, []).extend(v)
    return out


def _ua_in(headers: Any) -> Optional[str]:
    try:
        return next((v for k, v in dict(headers or {}).items() if str(k).lower() == "user-agent"), None)
    except (TypeError, ValueError):
        return None


class AsyncHopClient(httpx.AsyncClient):
    """``httpx.AsyncClient`` under the one redirect rule (see above); same constructor and methods,
    plus ``defer_on_429`` (False only for a module listed in upstreams.SELF_BACKOFF)."""

    def __init__(self, *args: Any, event_hooks: Optional[dict] = None, defer_on_429: bool = True,
                 **kwargs: Any) -> None:
        self._defer_on_429 = defer_on_429
        hooks = ahop_hooks(_ua_in(kwargs.get("headers")), defer_on_429=defer_on_429)
        super().__init__(*args, event_hooks=_hooks_with(hooks, event_hooks), **kwargs)

    def _record(self, request: httpx.Request, resp: httpx.Response) -> httpx.Response:
        # the final response, on its own host (a no-op when the response hook has recorded it)
        upstreams.observe_response(_resp_url(resp, str(request.url)), resp,
                                   defer_on_429=self._defer_on_429)
        return resp

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        # inside adirect, whose AsyncHopGates carries the gates; any other value is not this
        # request's, and would take no gate (review P3)
        if isinstance(_hops_var.get(), upstreams.AsyncHopGates):
            return self._record(request, await super().send(request, **kwargs))
        gates = upstreams.AsyncHopGates(request_s=_timeout_s(request.extensions.get("timeout")))
        token = _hops_var.set(gates)
        try:
            await gates.enter(str(request.url))
            resp = await super().send(request, **kwargs)
        except BaseException as exc:
            await gates._abort(exc)
            raise
        finally:
            _hops_var.reset(token)
        if kwargs.get("stream"):
            resp.stream = _AGatedStream(resp.stream, gates.close)
        else:
            await gates.close()
        return self._record(request, resp)


@contextlib.asynccontextmanager
async def adirect_stream(method: str, url: str, *, cookies: Any = None, timeout: Any = DEFAULT_TIMEOUT,
                         **kwargs: Any):
    """``httpx.stream`` under the redirect rule: a one-off ``AsyncHopClient`` (``cookies`` and
    ``timeout`` belong to the client, everything else to the request); the response's gates are held
    until the body is read or the block ends."""
    async with AsyncHopClient(cookies=cookies, timeout=timeout) as c:
        async with c.stream(method, url, **kwargs) as resp:
            yield resp


# The sync entries of the shared helpers below (get, get_json, get_text, post_json): for a caller on a
# thread, the async helper run on the event loop (portal.call), arguments as they are (EYE-TWINS-1).
def get(url: str, **kwargs: Any) -> Optional[httpx.Response]:
    """``aget`` from a thread. None on failure."""
    return portal.call(aget, url, **kwargs)


def get_json(url: str, **kwargs: Any) -> Optional[Any]:
    """``aget_json`` from a thread. None on request failure OR unparseable body."""
    return portal.call(aget_json, url, **kwargs)


def get_text(url: str, **kwargs: Any) -> Optional[str]:
    """``aget_text`` from a thread. None on failure."""
    return portal.call(aget_text, url, **kwargs)


def post_json(url: str, **kwargs: Any) -> Optional[Any]:
    """``apost_json`` from a thread. None on any failure."""
    return portal.call(apost_json, url, **kwargs)


_CURL_MAX_REDIRECTS = 20
_REDIRECT_STATUS = (301, 302, 303, 307, 308)


async def _acurl_hops(creq, method: str, url: str, gates: "upstreams.AsyncHopGates", *,
                      headers: Optional[dict], max_redirects: int = _CURL_MAX_REDIRECTS, **kw: Any):
    """Send ``method url`` through the libcurl tier, following redirects HOP BY HOP (curl's own
    following is off), so every hop gets the SSRF check, the redirect rule (``gates``: same host
    continues, another host lets go and takes its own gates) and its host's declared User-Agent.
    301/302/303 turn a non-GET into GET without a body (what browsers do); 307/308 keep both.
    Credentials are not carried to another host. Returns the final response (the caller closes it).
    The gates are held by the calling task; libcurl blocks, so each hop's request (and the SSRF check's
    DNS lookup) runs on the IO lane, in a copy of this context (the holds' progress renewal sees them)."""
    method = method.upper()
    base_headers = dict(headers) if headers else None
    host0 = urlsplit(url).hostname
    for _hop in range(max_redirects + 1):
        blk = await anyio.to_thread.run_sync(_netguard.security_block_reason, url, limiter=portal.IO_LANE)
        if blk is not None:
            raise RuntimeError(f"refused SSRF-class url ({blk}): {url[:120]}")
        await gates.enter(url)
        hop_headers = base_headers
        if upstreams.user_agent_for(url):   # a declared User-Agent replaces Chrome's for that host only
            hop_headers = upstreams.with_declared_user_agent(url, hop_headers)
        holds = _guard.active_handles()
        _guard.renew_handles(holds)   # this hop goes out (libcurl reports no "request written" moment)
        r = await anyio.to_thread.run_sync(
            functools.partial(creq.request, method, url, impersonate="chrome", headers=hop_headers,
                              allow_redirects=False, **kw), limiter=portal.IO_LANE)
        _guard.renew_handles(holds)   # its response is back (for a stream: the headers)
        upstreams.observe(url, r.headers, r.status_code)
        loc = r.headers.get("location") if r.status_code in _REDIRECT_STATUS else None
        if not loc:
            return r
        await anyio.to_thread.run_sync(_close_quietly, r, limiter=portal.IO_LANE)
        nxt = urljoin(url, loc)
        if r.status_code in (301, 302, 303) and method not in ("GET", "HEAD"):
            method = "GET"
            kw.pop("json", None)
            kw.pop("data", None)
        if base_headers and urlsplit(nxt).hostname != host0:
            base_headers = {k: v for k, v in base_headers.items()
                            if str(k).lower() not in ("authorization", "cookie")}
        url = nxt
    raise RuntimeError(f"too many redirects (>{max_redirects}): {url[:120]}")


def _close_quietly(r: Any) -> None:
    try:
        r.close()
    except Exception:  # noqa: BLE001
        pass


class DownloadError(RuntimeError):
    """A download_to_file that got an answer but no file: a non-2xx (``status`` and up to
    ``_ERROR_BODY_CAP`` bytes of its ``body`` as text, for the caller's reason line), or a body past the
    cap it was given. A RuntimeError, so every caller that caught RuntimeError still catches it."""

    def __init__(self, message: str, *, status: Optional[int] = None, body: str = "",
                 content_type: str = "") -> None:
        super().__init__(message)
        self.status, self.body, self.content_type = status, body, content_type


def _unlink_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def download_to_file(url: str, dest: str, **kwargs: Any) -> int:
    """Sync entry for a caller on a thread: ``adownload_to_file`` on the event loop (portal.call)."""
    return portal.call(adownload_to_file, url, dest, **kwargs)


async def adownload_to_file(url: str, dest: str, *, max_bytes: int,
                            timeout: int = 600, headers: Optional[dict] = None,
                            info: Optional[dict] = None) -> int:
    """Stream a URL to ``dest`` via curl_cffi (libcurl + Chrome TLS fingerprint), capping at
    ``max_bytes``. Returns bytes written; RAISES on cache-only / SSRF block / missing dep / oversize /
    transport error (unlike get()'s None: a fallback caller wants the reason). curl_cffi NOT httpx:
    this deployment's egress mangles openssl TLS to some CDNs (httpx -> 'UNEXPECTED_EOF_WHILE_READING'
    on cloudfront) while libcurl's handshake gets through -- the same tier get_impersonated uses. The
    robust-fetch path for LARGE binaries (audio, and since EYE-RSS-3 every paper PDF / arXiv full text
    under ``DOC_MAX_BYTES``) that the size-capped get() refuses AND that the bundled ffmpeg's own TLS
    cannot fetch. (Egress to some CDNs can be slow, so timeout defaults high.)

    A non-2xx answer or a body past ``max_bytes`` raises ``DownloadError`` (the status and the start of
    the error body ride on it); ``dest`` is then removed, so a failed download never leaves a partial
    file. ``info`` (a dict the caller passes) is filled with the final answer's ``status``,
    ``content_type``, ``content_disposition`` and ``final_url`` once the answer arrives."""
    if cache.cache_only():
        raise RuntimeError("cache-only mode: no live download")
    _blk = await anyio.to_thread.run_sync(_netguard.security_block_reason, url, limiter=portal.IO_LANE)
    if _blk is not None:
        raise RuntimeError(f"blocked SSRF-class target: {_blk}")
    try:
        from curl_cffi import requests as _creq  # lazy: keep curl_cffi off the hot import path
    except Exception as exc:  # noqa: BLE001 — missing/broken dep surfaces as a clear raise
        raise RuntimeError(f"curl_cffi unavailable: {exc}") from exc
    # Declared gates of every hop (UpstreamBusy is a RuntimeError, raised before that hop is sent),
    # held by this task until the file is written; the body is read on the IO lane (libcurl blocks).
    async with upstreams.ahop_gates(request_s=_timeout_s(timeout)) as gates:
        r = await _acurl_hops(_creq, "GET", url, gates, headers=headers, timeout=timeout, stream=True)
        return await anyio.to_thread.run_sync(
            functools.partial(_write_download, r, url, dest, max_bytes, info), limiter=portal.IO_LANE)


def _write_download(r: Any, url: str, dest: str, max_bytes: int, info: Optional[dict]) -> int:
    """The body of ``adownload_to_file``'s final answer to ``dest`` (on a worker thread; closes ``r``)."""
    total = 0
    holds = _guard.active_handles()   # every block of the body renews their leases
    opened = False
    try:
        status = getattr(r, "status_code", None)
        rh = getattr(r, "headers", None) or {}
        ctype = str(rh.get("content-type") or "")
        if info is not None:
            info.update({"status": status, "content_type": ctype,
                         "content_disposition": str(rh.get("content-disposition") or ""),
                         "final_url": str(getattr(r, "url", "") or url)})
        if isinstance(status, int) and status >= 400:
            buf = bytearray()
            try:
                for chunk in r.iter_content(chunk_size=65536):
                    buf += chunk
                    if len(buf) >= _ERROR_BODY_CAP:
                        break
            except Exception:  # noqa: BLE001 (the body is only for the reason line)
                pass
            text = bytes(buf[:_ERROR_BODY_CAP]).decode("utf-8", "replace")
            raise DownloadError(f"HTTP {status}", status=status, body=text, content_type=ctype)
        r.raise_for_status()
        opened = True
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=262144):
                _guard.renew_handles(holds)
                total += len(chunk)
                if total > max_bytes:
                    raise DownloadError(f"download exceeded {max_bytes} bytes", status=status,
                                        content_type=ctype)
                f.write(chunk)
    except BaseException:
        if opened:
            _unlink_quietly(dest)
        raise
    finally:
        r.close()
    if total == 0:
        _unlink_quietly(dest)
        raise RuntimeError("download returned 0 bytes")
    return total


async def _aimpersonated_request(method: str, url: str, *, timeout: int = DEFAULT_TIMEOUT,
                                 headers: Optional[dict] = None,
                                 json_body: Any = None, check_host: bool = True,
                                 browser_headers: bool = True) -> Optional[bytes]:
    """Request via curl_cffi with a real-browser TLS/JA3 fingerprint (Chrome impersonation); returns
    the raw response BYTES. None on failure OR if curl_cffi is unavailable.

    Method-aware since 2026-09-09: the egress mangles openssl's handshake on POST endpoints too
    (statcan_wds POSTs its only data endpoint and read as down for it), and the guards below are
    method-independent, so keeping this GET-only would have meant a second copy of them.

    A SEPARATE fetch tier BETWEEN plain httpx (``get``) and the heavy CDP browser: some hosts wall
    httpx by its TLS/JA3 handshake fingerprint (PerimeterX / HUMAN 'Pardon Our Interruption',
    Cloudflare TLS checks) while letting a real browser through. curl_cffi replays Chrome's TLS
    handshake, so the fetch passes WITHOUT spinning a headless browser. OPT-IN only (no default
    caller routes here, so every existing source is byte-identical); the import is LAZY and a missing
    dep degrades to None (the source goes DOWN, never crashes the server). Verified 2026-06-22 on the
    HigherEdJobs PerimeterX-walled RSS feed: httpx -> challenge HTML; curl_cffi(chrome) -> the real
    129-item feed.

    NOTE: we do NOT inject our omniseek UA here — ``impersonate='chrome'`` sets a Chrome-consistent
    UA + header order, and overriding the UA would desync the very fingerprint we are matching. The one
    exception is a host whose upstream DECLARES a User-Agent (upstreams.json): its policy wins.

    ``browser_headers=False`` keeps the Chrome handshake but sends only ``headers`` (curl_cffi's
    ``default_headers=False``): the transport fallback's retry of an httpx request (_acurl_tier_retry)."""
    try:
        from curl_cffi import requests as _creq  # lazy: keep curl_cffi off the hot import path
    except Exception as exc:  # noqa: BLE001 — missing/broken dep -> degrade, never crash
        logger.warning("http impersonated tier unavailable (curl_cffi import failed): %s", exc)
        return None
    # Mirror the _arequest_capped discipline at the curl_cffi tier (S1-C1): the single egress guard, the
    # SSRF pre-flight, and a MAX_BYTES cap. All three were absent here (an unbounded ``return r.content``).
    if cache.cache_only():
        return None  # cache-only mode (cache_only=True): do NO live HTTP, the single egress guard
    # SSRF pre-flight on the INITIAL url (same predicate as _arequest_capped). Per-hop redirect
    # revalidation stays DEFERRED PAST C2 to a curl_cffi-exercisable cohort: get_impersonated is
    # curl_cffi (a DIFFERENT transport from safeurl.safe_fetch's httpx, so it cannot reuse that
    # per-hop walk) and serves only FIXED CONFIGURED public hosts (higheredjobs), whose redirect-SSRF
    # needs a DNS-rebind on a fixed host (lower probability than an arbitrary user URL); and curl_cffi
    # is not importable in this dev env, so a manual redirect-walk cannot be verified here. C2
    # closed the arbitrary-user-URL lane instead (omniseek_add_url -> web_fallback -> safeurl.safe_fetch,
    # IP-pinned per hop); this fixed-host tier keeps allow_redirects=True as a known residual.
    _blk = await anyio.to_thread.run_sync(_netguard.security_block_reason, url, limiter=portal.IO_LANE)
    if _blk is not None:
        logger.warning("http impersonated tier blocked SSRF-class target (%s): %s", url, _blk)
        return None
    try:
        # Redirects are followed hop by hop (_acurl_hops): the SSRF check, the declared gates and the
        # declared User-Agent on every hop; UpstreamBusy -> the except below -> None.
        async with upstreams.ahop_gates(request_s=_timeout_s(timeout), check_host=check_host) as gates:
            r = await _acurl_hops(_creq, method, url, gates, headers=headers, timeout=timeout,
                                  json=json_body, default_headers=browser_headers)
        r.raise_for_status()
        # DECODED-bytes cap (curl_cffi returns already-decoded content). The curl_cffi streaming API
        # (stream=True + iter_content) is not exercisable in this build (curl_cffi is not importable in the
        # dev env, so a streamed accumulate-and-abort mirror of the _arequest_capped aiter_raw loop
        # cannot be verified here), so this takes the documented FALLBACK: reject a declared-oversize
        # Content-Length BEFORE reading, then cap len(r.content) AFTER read. It still allocates the body
        # once (the partial-mitigation caveat), but it never RETURNS an oversize body, closing the
        # unbounded-return hole. A later cohort can swap this for an abort-mid-stream once the tier is
        # exercisable against real curl_cffi.
        try:
            _clen = int(r.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            _clen = 0
        if _clen > MAX_BYTES:
            logger.warning("http impersonated tier refused oversized response (%s): Content-Length %d > %d",
                           url, _clen, MAX_BYTES)
            return None
        body = r.content
        if len(body) > MAX_BYTES:
            logger.warning("http impersonated tier refused oversized response (%s): %d bytes > %d",
                           url, len(body), MAX_BYTES)
            return None
        return body
    except Exception as exc:  # noqa: BLE001 — the failure->None contract (same as http.get)
        logger.warning("http impersonated tier failed (%s %s): %s", method.upper(), url, exc)
        _resp = getattr(exc, "response", None)
        _st = getattr(_resp, "status_code", None)
        diag.note(f"http.impersonated_{method.lower()}", url=url,
                  status=_st if isinstance(_st, int) and _st > 0 else None, exc=exc)
        return None


def get_impersonated(url: str, **kwargs: Any) -> Optional[bytes]:
    """Sync entry for a caller on a thread: ``aget_impersonated`` on the event loop (portal.call)."""
    return portal.call(aget_impersonated, url, **kwargs)


async def aget_impersonated(url: str, *, timeout: int = DEFAULT_TIMEOUT,
                            headers: Optional[dict] = None) -> Optional[bytes]:
    """GET on the curl_cffi tier. The named entry point adapters opt into (higheredjobs,
    canada_jobbank_wages); the method-aware internal is what the transport fallback in
    _arequest_capped uses."""
    return await _aimpersonated_request("GET", url, timeout=timeout, headers=headers)


# ── THE SHARED HELPERS ─────────────────────────────────────────────────────────────────────────
# The ONE choke point ~190 adapters egress through (cache_only->None, the SSRF pre-flight, the
# MAX_BYTES streamed cap, raise_for_status, rebuild-Response-from-raw-bytes, failure->None + diag.note,
# with the diag labels "http.get"/"http.post"/..., NOT "http.aget"). A caller on a thread reaches them
# through the sync entries above (get, get_json, get_text, post_json), which run them on the event
# loop; the contextvars (cache.cache_only/fresh + diag.note/enable/drain, the caller's deadline) arrive
# as the caller set them (portal.py) and propagate through await.
# Process-wide pooled client: every helper call reuses ONE httpx.AsyncClient, so repeated requests to
# the same host skip the TCP+TLS handshake. HTTP/2 multiplexing is enabled only if ``h2`` is importable
# (no new hard dep: keep-alive reuse works on HTTP/1.1 too). Walled / anti-bot adapters do NOT use
# these helpers (they keep bespoke headers/signing, see the module docstring).
_aclient: Optional[httpx.AsyncClient] = None
_aclient_lock = threading.Lock()  # construction is sync (no await): double-checked lock


def _aget_client() -> httpx.AsyncClient:
    """Lazily build (once) the shared pooled async client. Double-checked lock so the first concurrent
    async callers create exactly one.

    httpx.AsyncClient's connection pool is bound to the event loop that first uses it. In the service
    every caller is on its one loop (omniseek_gather's children included), so one client serves them all."""
    global _aclient
    if _aclient is None:
        with _aclient_lock:
            if _aclient is None:
                # Lazy import to break the http <-> safeurl cycle: safeurl imports http (for USER_AGENT
                # etc.), so http importing safeurl at module load would cycle. By the time the first
                # request builds the client, safeurl is fully loaded.
                from omniseek.core import safeurl  # noqa: PLC0415 (lazy: breaks the import cycle)
                # Build the real async HTTP transport explicitly, then wrap it so EVERY request AND every
                # redirect hop is _netguard-validated at the connection layer (the per-hop SSRF guard). headers / follow_redirects / timeout stay
                # Client-level so httpx still owns redirect semantics; the wrapper only refuses an
                # SSRF-class hop's connection.
                _awrapped = httpx.AsyncHTTPTransport(
                    http2=_http2_ok(),
                    limits=httpx.Limits(max_keepalive_connections=64,
                                        max_connections=128,
                                        keepalive_expiry=30.0),
                )
                # PARITY NOTE: passing an explicit transport= makes httpx skip env/system-proxy
                # auto-detection (allow_env_proxies = trust_env AND transport is None). This is HARMLESS
                # for the current deployment, which egresses via transparent fake-IP TUN (verified 2026-07:
                # no *_proxy env on OmniSeek-http process, empty scutil --proxies, utun interfaces present),
                # so httpx never used a proxy anyway. If this eye is ever moved to a PROXY-based egress
                # (HTTP_PROXY / system proxy), restore proxy support by passing proxy= to the wrapped
                # AsyncHTTPTransport (or mounts= of AsyncSSRFGuardTransport-wrapped proxied transports) so
                # the SSRF guard still wraps the proxied connection.
                _aclient = httpx.AsyncClient(
                    transport=safeurl.AsyncSSRFGuardTransport(_awrapped),
                    # one-connection hosts (see _one_connection_transport): their own transport
                    mounts={f"all://{h}": safeurl.AsyncSSRFGuardTransport(_one_connection_transport())
                            for h in upstreams.single_connection_hosts()},
                    # every hop: the redirect rule (declared gates) and the declared User-Agent
                    event_hooks={"request": [_aapply_hop_gates, _aapply_declared_user_agent,
                                             _awatch_send],
                                 "response": [_aobserve_hop]},
                    headers={"User-Agent": USER_AGENT},
                    follow_redirects=True,
                    timeout=DEFAULT_TIMEOUT,
                )
    return _aclient


async def _arequest_capped(method: str, url: str, *, timeout: int, headers: dict,
                           retry_transient: bool = True,
                           **kwargs: Any) -> Optional[httpx.Response]:
    """Stream a request, aborting if the body exceeds ``MAX_BYTES``, the OOM guard shared by all
    helpers. Returns a fully-read ``httpx.Response`` (so ``.text``/``.json()`` work) or ``None`` on any
    failure / oversize. Uses the pooled client so the connection is reused (keep-alive). When enabled,
    only a first-attempt ``httpx.ConnectError`` gets one bounded retry. A GET / POST whose last attempt
    failed before it went out (``_openssl_failed_unsent``) is tried once more on the libcurl tier;
    served there, it counts as reaching the host (see ``_curl_tier_may_serve``)."""
    if cache.cache_only():
        return None  # cache-only mode (cache_only=True): do NO live HTTP, the single egress guard
    # SSRF pre-flight (belt-and-suspenders): refuse a URL whose host resolves to a private/loopback/
    # link-local/reserved IP. Redundant with the per-hop AsyncSSRFGuardTransport on the pooled client,
    # kept for the clear initial block message + diag.note evidence tap; the extra getaddrinfo is
    # OS-cached, so negligible. Residual (DNS-rebind TOCTOU): the IP-pinning lane stays safe_fetch.
    # OFF-LOOP (S4b): security_block_reason resolves getaddrinfo (a BLOCKING syscall). On a native async
    # method it runs ON the loop, so a slow/cold DNS would freeze EVERY coroutine. Push it to a worker
    # thread. IDENTICAL guard DECISION (same _netguard, same block reasons, same diag.note); only moved
    # off the loop.
    _blk = await anyio.to_thread.run_sync(_netguard.security_block_reason, url, limiter=portal.IO_LANE)
    if _blk is not None:
        logger.warning("http.%s blocked SSRF-class target (%s): %s", method.lower(), url, _blk)
        diag.note(f"http.{method.lower()}", url=url, status=None, body=f"blocked SSRF-class target: {_blk}")
        return None
    headers = upstreams.with_declared_user_agent(url, _with_connection_close(url, headers))
    err_body: list = [None]   # the last attempt's bounded error body, for the failure note below
    deferred: list = []       # (host, exc): connections httpx could not make, held for the libcurl tier

    async def _attempt() -> Optional[httpx.Response]:
        err_body[0] = None
        # The client first (building it can take a second the first time), then the gates, so the
        # start slot the gate grants is the moment the request goes on the wire. Declared gates per
        # attempt, held through the body read; every later hop (a redirect) goes through the same
        # AsyncHopGates from the client's request hook, so the rule is the same on every hop.
        client = _aget_client()
        async with upstreams.ahop_gates(request_s=_timeout_s(timeout)) as gates:  # every hop
            await gates.enter(url)
            token = _hops_var.set(gates)
            try:
                async with client.stream(method, url, timeout=timeout, headers=headers,
                                         **kwargs) as r:
                    upstreams.observe_response(_resp_url(r, url), r)
                    if getattr(r, "is_error", False):  # getattr: hand-built stub responses lack it
                        err_body[0] = await _aread_error_body(r)
                    r.raise_for_status()
                    raw = bytearray()
                    async for chunk in r.aiter_raw():
                        raw += chunk
                        if len(raw) > MAX_BYTES:
                            logger.warning("http.%s refused oversized response (%s): >%d bytes",
                                           method.lower(), url, MAX_BYTES)
                            diag.note(f"http.{method.lower()}", url=url, status=r.status_code,
                                      body=f"refused oversized response (>{MAX_BYTES} bytes)")
                            return None
            except Exception as exc:
                if _curl_tier_may_serve(method, exc):
                    host = await gates.let_go(exc)
                    if host:
                        deferred.append((host, exc))
                raise
            finally:
                _hops_var.reset(token)
        # Rebuild a normal already-read Response from the raw body + original headers, so
        # content-encoding / charset decoding happens exactly as the buffered path did.
        return _counted(httpx.Response(r.status_code, headers=r.headers, content=bytes(raw),
                                       request=r.request))

    async def _curl_tier(exc: BaseException) -> Optional[httpx.Response]:
        if not _curl_tier_may_serve(method, exc):
            return None
        served = await _acurl_tier_retry(method, url, timeout, headers,
                                         kwargs.get("json"), kwargs.get("params"))
        if served is not None:
            deferred.clear()   # libcurl reached the host: not a failed connection to it
        return served

    try:
        resp = await AsyncRetrying(sleep=anyio.sleep, **_transient_retry(retry_transient, url))(_attempt)
        deferred.clear()   # a later attempt reached the host
        return resp
    except upstreams.UpstreamBusy as exc:
        _gate_note(method, url, exc)
        return None
    except httpx.ConnectError as exc:
        served = await _curl_tier(exc)
        if served is not None:
            return served
        logger.warning("http.%s failed (%s): %s", method.lower(), url, exc)
        diag.note(f"http.{method.lower()}", url=url, exc=exc)
        return None
    except Exception as exc:  # noqa: BLE001 , failure → None is the adapter contract
        served = await _curl_tier(exc)
        if served is not None:
            return served
        logger.warning("http.%s failed (%s): %s", method.lower(), url, exc)
        # A non-2xx surfaces here as httpx.HTTPStatusError → surface its status + body snippet so
        # the fixing agent sees the wall (403/412 anti-bot, 404 moved endpoint), not just a string.
        # The body is the bounded copy read before raise_for_status (a streamed response's .text
        # raises ResponseNotRead); .text stays as the fallback for a response already read.
        st = getattr(getattr(exc, "response", None), "status_code", None)
        bd = err_body[0]
        if bd is None and isinstance(exc, httpx.HTTPStatusError):
            try:
                bd = exc.response.text
            except Exception:  # noqa: BLE001
                bd = None
        _probe.note_error_body(st, bd, where=url)
        diag.note(f"http.{method.lower()}", url=url, status=st, body=bd, exc=exc)
        return None
    finally:
        _count_unreached(deferred)   # no answer from either tier: counted as the hop gates count it


async def aget(url: str, *, timeout: int = DEFAULT_TIMEOUT, headers: Optional[dict] = None,
               params: Optional[dict] = None, retry_transient: bool = True,
               **kwargs: Any) -> Optional[httpx.Response]:
    """Async GET with the shared UA + redirects + raise_for_status, size-capped. None on failure.
    ``retry_transient`` retries only one first-attempt connect-phase failure."""
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    return await _arequest_capped("GET", url, timeout=timeout, headers=hdrs,
                                  retry_transient=retry_transient, params=params, **kwargs)


async def aget_json(url: str, **kwargs: Any) -> Optional[Any]:
    """Async GET and parse JSON. None on request failure OR unparseable body."""
    resp = await aget(url, **kwargs)
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("http.get_json parse failed (%s): %s", url, exc)
        diag.note("http.get_json", url=url, status=resp.status_code, body=resp.text, exc=exc)
        return None


async def aget_text(url: str, **kwargs: Any) -> Optional[str]:
    """Async GET and return response text. None on failure."""
    resp = await aget(url, **kwargs)
    return resp.text if resp is not None else None


async def apost_json(url: str, *, json: Any = None, timeout: int = DEFAULT_TIMEOUT,
                     headers: Optional[dict] = None, **kwargs: Any) -> Optional[Any]:
    """Async POST a JSON body and parse the JSON response. None on any failure."""
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    resp = await _arequest_capped("POST", url, timeout=timeout, headers=hdrs, json=json, **kwargs)
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("http.post_json parse failed (%s): %s", url, exc)
        diag.note("http.post_json", url=url, status=resp.status_code, body=resp.text, exc=exc)
        return None


async def aput_json(url: str, *, json: Any = None, timeout: int = DEFAULT_TIMEOUT,
                    headers: Optional[dict] = None, **kwargs: Any) -> Optional[Any]:
    """Async PUT a JSON body and parse the JSON response. None on any failure. Same contract as
    apost_json (some search APIs are PUT-shaped)."""
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    resp = await _arequest_capped("PUT", url, timeout=timeout, headers=hdrs, json=json, **kwargs)
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("http.put_json parse failed (%s): %s", url, exc)
        diag.note("http.put_json", url=url, status=resp.status_code, body=resp.text, exc=exc)
        return None


async def aclose_client() -> None:
    """Close the pooled async client (await its aclose) and reset it so a later build is clean. Fail-open
    (never raises). NOTE: S4 must wire this into the ASGI lifespan shutdown when the FIRST async caller
    lands. In S3b nothing builds _aclient, so nothing leaks yet; this is built here but NOT yet wired."""
    global _aclient
    _ac = _aclient
    _aclient = None
    if _ac is not None:
        try:
            await _ac.aclose()
        except Exception as exc:  # noqa: BLE001 , shutdown must never raise
            logger.warning("http.aclose_client failed: %s", exc)
