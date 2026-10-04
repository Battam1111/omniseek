"""What held a health check back: the ledger a check's verdict is re-read against.

A health check answers True (verified working), False (verified broken) or None (not verified: this
check sent the upstream no request whose answer could tell good from bad; see
``fetcher.SourceAdapter.health_check``). Two things make a False wrong without the adapter knowing:

- the upstream answered HTTP 429, or an error whose body says it is a rate limit. It answered, but
  the answer says nothing about whether the path serves data: not verified;
- one of OmniSeek's own gates (a declared upstream gate, a shared permit pool) turned the request
  away before it was sent: nothing was asked, so nothing was verified.

Most adapters cannot tell either case from an outage: the shared http helpers return None on any
failure. So the egress records them here instead, at the places every request already passes:
``upstreams.observe`` sees every response's status, ``upstreams.UpstreamBusy`` and
``_guard.GateBusy`` are built only when a gate refuses, and ``http._request_capped`` reads the error
body. Three egress paths that never reach ``upstreams.observe`` record here too (2026-10-04): the
curl_cffi entry point (``omniseek.core.curl``), the browser helper (``_cdp.cdp_call``: the page's
main-document answers and its in-page fetches' 429s, kept only while a ledger is open, see
``active``) and the yt-dlp entry point (``omniseek.core.ytdlp``: every HTTP error answer). A ledger is
open only while a health check runs (``watching``, opened by
``fetcher._safe_health`` and by the shared probes that cache one verdict for many sources); outside
one, recording is a no-op.

``reread`` turns a False into None when the ledger holds a 429, a rate-limit body or a gate refusal
AND no other error answer (a 4xx / 5xx that is not a rate limit): that answer is real evidence, so
the False stands. A True is never touched: an adapter that saw a served answer keeps it.

Context: the ledger rides a ContextVar, so a request made on another thread is recorded only when
that thread runs in a copy of the check's context (``contextvars.copy_context()``, as the RSS bundle
probe does); asyncio tasks inherit it on their own.
"""

from __future__ import annotations

import contextlib
import contextvars
import re
from typing import Optional

# Words an error body uses for a rate limit (GitHub's "API rate limit exceeded" / "secondary rate
# limit", Stack Exchange's "throttle_violation", the generic "Too Many Requests").
_RATE_WORDS = re.compile(r"rate[ _-]?limit|too many requests|throttl", re.I)


class Ledger:
    """One health check's record: ``held`` (what held it back), ``errors`` (other error answers)."""

    __slots__ = ("held", "errors", "answers")

    def __init__(self) -> None:
        self.held: list[tuple] = []     # ("429", where, retry_after) | ("body", where, status) | ("gate", detail)
        self.errors: list[tuple] = []   # (status, where) for every 4xx / 5xx answer that is not a rate limit
        self.answers = 0                # responses observed, any status


_current: contextvars.ContextVar[Optional[Ledger]] = contextvars.ContextVar(
    "omniseek_eye_probe_ledger", default=None)


@contextlib.contextmanager
def watching():
    """Open a ledger for one health check; what it records also reaches an enclosing ledger."""
    outer = _current.get()
    led = Ledger()
    token = _current.set(led)
    try:
        yield led
    finally:
        _current.reset(token)
        if outer is not None:
            outer.held.extend(led.held)
            outer.errors.extend(led.errors)
            outer.answers += led.answers


def says_rate_limited(text: Optional[str]) -> bool:
    """True when an error text names a rate limit (for an adapter's own exception messages)."""
    return bool(text) and bool(_RATE_WORDS.search(text))


def active() -> bool:
    """Whether a health check's ledger is open in this context (a recorder that has to set something
    up first, such as the browser's response listener, does so only then)."""
    return _current.get() is not None


def note_response(status: Optional[int], *, where: str = "", retry_after: Optional[float] = None) -> None:
    """Record one response (called by ``upstreams.observe`` for every response). No-op outside a check."""
    led = _current.get()
    if led is None or not isinstance(status, int):
        return
    led.answers += 1
    if status == 429:
        led.held.append(("429", where, retry_after))
    elif status >= 400:
        led.errors.append((status, where))


def note_error_body(status: Optional[int], body: Optional[str], *, where: str = "") -> None:
    """An error answer whose body says it is a rate limit counts as one (moved from ``errors`` to
    ``held``). Called by the shared http helper, which has the body; a 429 is already held."""
    led = _current.get()
    if led is None or not isinstance(status, int) or status == 429 or not body:
        return
    if not _RATE_WORDS.search(body):
        return
    for i in range(len(led.errors) - 1, -1, -1):
        if led.errors[i][0] == status:
            del led.errors[i]
            break
    led.held.append(("body", where, status))


def note_held(detail: str) -> None:
    """A gate of OmniSeek refused a request before it was sent (``UpstreamBusy`` / ``GateBusy``)."""
    led = _current.get()
    if led is not None:
        led.held.append(("gate", str(detail)[:200]))


def rate_limited(where: str = "", retry_after: Optional[float] = None, status: int = 429) -> str:
    """The one wording for "the upstream rate-limited this check" (adapters that read the status
    themselves use it too, so every such message reads alike)."""
    at = f" from {where}" if where else ""
    ra = f", Retry-After {retry_after:.0f}s" if retry_after else ""
    return f"not verified: rate-limited (HTTP {status}{at}{ra})"


def breaker_open(what: str, seconds_left: Optional[float] = None) -> str:
    """The one wording for "a circuit breaker / back-off of OmniSeek is open, nothing was sent"."""
    left = f", reopens in {max(0.0, seconds_left):.0f}s" if seconds_left is not None else ""
    return f"not verified: circuit breaker open ({what}{left}); no request sent"


def http_429(exc: BaseException) -> Optional[str]:
    """The ``rate_limited`` message when ``exc`` carries an HTTP 429 response (an
    ``httpx.HTTPStatusError`` from ``raise_for_status``), else None. For adapters whose own client
    raises instead of returning the status."""
    resp = getattr(exc, "response", None)
    if getattr(resp, "status_code", None) != 429:
        return None
    try:
        host = resp.request.url.host
    except Exception:  # noqa: BLE001
        host = ""
    return rate_limited(host, retry_after_s(getattr(resp, "headers", None)))


def _describe(item: tuple) -> str:
    kind = item[0]
    if kind == "429":
        return rate_limited(item[1], item[2])
    if kind == "body":
        return f"{rate_limited(item[1], status=item[2])}; its body says it is a rate limit"
    return f"not verified: an eye gate held the request back, nothing sent ({item[1]})"


def reread(ok: Optional[bool], msg: str, led: Optional[Ledger]) -> tuple[Optional[bool], str]:
    """``(ok, msg)`` re-read against what the check's ledger recorded (rules in the module docstring)."""
    if ok is not False or led is None or not led.held or led.errors:
        return ok, msg
    return None, f"{_describe(led.held[-1])}; the check said: {msg}"


def retry_after_s(headers) -> Optional[float]:
    """Seconds a response's Retry-After asks for (either RFC form), or None."""
    try:
        from omniseek.core.upstreams import parse_retry_after
        h = headers or {}
        return parse_retry_after(h.get("retry-after") or h.get("Retry-After"))
    except Exception:  # noqa: BLE001
        return None
