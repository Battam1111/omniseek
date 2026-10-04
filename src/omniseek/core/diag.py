"""Opt-in failure-evidence trace: OmniSeek's "what went wrong" capture for a SINGLE source.

When omniseek_fetch(source, query) comes back empty or errored, the agent that has to FIX
the source (the /eye-fix skill) needs evidence: which egress helper failed, the HTTP
status + a body snippet, the exception type. This module is that capture, and nothing
more. The razor: it stores; it never judges (the fixing agent judges the root cause).

Design (deliberately small):
  - A contextvar holds either None (the default: capture OFF) or a list (capture ON).
  - ``enable()`` turns capture on FOR THE CURRENT THREAD/CONTEXT by setting the var to a
    fresh list; ``drain()`` returns that list and resets to None.
  - The shared egress helpers (http / _openalex / _github / _s2 / _stackexchange / _cdp)
    call ``note(...)`` ONLY on their failure branches. When capture is OFF (the var is
    None) ``note`` is a cheap no-op, so the broad search_many fan-out (which never calls
    ``enable()``) pays ZERO cost and never cross-contaminates one source's trace with
    another's. Capture is armed ONLY around a single fetch_one_with_diag run.

fail-open is absolute: every function here swallows its own errors. A bug in the
diagnostic path must NEVER turn a working retrieval into a broken one. note() that raises
internally is caught; a body that will not stringify is dropped; a URL that will not parse
is passed through best-effort. The capture is a luxury; the retrieval is the product.
"""

from __future__ import annotations

import contextvars
from typing import Optional

from omniseek import redact as _redact

# None = capture OFF (the default, and the broad-search state). A list = capture ON; note()
# appends to it. The default is shared, but enable() always sets a FRESH list, so two
# concurrent armed contexts never share a list (contextvars are per-logical-context).
_trace_var: contextvars.ContextVar = contextvars.ContextVar("omniseek_eye_diag", default=None)

_MAX_BODY = 500       # a body snippet beyond this is truncated (a marker is appended)
_MAX_CAPTURES = 50    # an upper bound on captures per run, so a retry storm cannot grow unbounded

# Query-string keys whose VALUE is a credential and must be stripped before a capture is shown
# to the fixing agent (the trace is read by an agent + may be logged). Case-insensitive match.
# ONE list for the whole eye: omniseek.redact owns it (logs, tool results and state files use it too).
_SECRET_KEYS = _redact.SECRET_PARAMS

# The diagnostic's historical marker for a masked value, kept so existing readers see no change.
_DIAG_MASK = "<redacted>"


def enable() -> None:
    """Arm capture for the current context: set the var to a FRESH list. Idempotent-ish (a
    second call just starts a new list, dropping any not-yet-drained captures, which is the
    intended reset). Called by fetch_one_with_diag right before it runs the adapter."""
    try:
        _trace_var.set([])
    except Exception:  # noqa: BLE001 (arming must never raise into the caller)
        pass


def active() -> bool:
    """True iff capture is currently armed (the var holds a list, not None)."""
    try:
        return _trace_var.get() is not None
    except Exception:  # noqa: BLE001
        return False


def _strip_secrets(url: Optional[str]) -> Optional[str]:
    """Return ``url`` with any credential-bearing query-string VALUES replaced by ``<redacted>``,
    keeping every other byte of the query VERBATIM. LOSSLESS by design: the old parse_qsl +
    urlencode round-trip force-decoded percent-escapes as UTF-8 (errors=replace) and re-encoded
    form-style, so a legacy-GBK query (Discuz srchtxt=%B2%A9%BA%F3) was displayed as %EF%BF%BD
    garbage with + spaces: the diagnostic then LIED about the URL actually sent and misled a
    2026-07-09 investigation into a nonexistent adapter "encoding bug". A diagnostic must never
    alter the evidence it reports; only the secret VALUES are substituted. The rule is the shared
    one in omniseek.redact (it rewrites nothing but the masked values). Never raises."""
    if not url:
        return url
    return _redact.redact(url, mask=_DIAG_MASK)


def _redact_text(text: Optional[str]) -> Optional[str]:
    """Scrub credential values from FREE TEXT (exc/body): an httpx exception message is literally
    "... for url 'https://api.adzuna.com/...?app_key=SECRET'", so a body/exc field would leak the
    secret the url field already scrubs. Same shared rule as _strip_secrets. Never raises."""
    if not text:
        return text
    return _redact.redact(text, mask=_DIAG_MASK)


def note(helper: str, *, url: Optional[str] = None, status: Optional[int] = None,
         body: Optional[object] = None, exc: Optional[BaseException] = None) -> None:
    """Append ONE failure record to the active trace (a no-op when capture is OFF).

    Called ONLY from the failure branches of the shared egress helpers (never the success
    path). ``helper`` names the egress (e.g. "http.get", "openalex.get_json", "cdp_call").
    ``url`` has its credential query values stripped; ``body`` is stringified + truncated to
    ``_MAX_BODY``; ``exc`` is reduced to ``type: message``. Fail-open: any internal error is
    swallowed so a diagnostic bug can never break the retrieval it is observing."""
    try:
        trace = _trace_var.get()
        if trace is None or len(trace) >= _MAX_CAPTURES:
            return  # capture OFF (the broad-search path), or this run already hit the cap
        rec: dict = {"helper": helper}
        if url is not None:
            rec["url"] = _strip_secrets(str(url))
        if status is not None:
            rec["status"] = status
        if body is not None:
            try:
                text = body if isinstance(body, str) else str(body)
            except Exception:  # noqa: BLE001 (an object whose __str__ raises is just dropped)
                text = None
            if text:
                text = _redact_text(text)
                rec["body"] = text[:_MAX_BODY] + ("…(truncated)" if len(text) > _MAX_BODY else "")
        if exc is not None:
            rec["exc"] = (_redact_text(f"{type(exc).__name__}: {exc}") or "")[:_MAX_BODY]
        trace.append(rec)
    except Exception:  # noqa: BLE001 (capture must never raise into a live retrieval)
        pass


def drain() -> list:
    """Return the captures collected since ``enable()`` and reset capture to OFF.

    Always returns a list (``[]`` when nothing was captured or capture was never armed), and
    leaves the var at None so a reused pool thread starts clean. Never raises."""
    try:
        trace = _trace_var.get()
        _trace_var.set(None)
        return list(trace) if trace else []
    except Exception:  # noqa: BLE001
        return []


def failure_reason(captures: list, *, fallback: str) -> str:
    """Format the latest captured egress failure for an operator-facing status message.

    A response status is a different fact from no response. The body is already bounded and
    redacted by ``note``; an exception capture already contains its type and message. This helper
    only formats those stored facts and never performs I/O or judgment.
    """
    try:
        if not captures:
            return fallback
        observed = captures[-1] if isinstance(captures[-1], dict) else {}
        status = observed.get("status")
        body = observed.get("body")
        exc = observed.get("exc")
        if status is not None:
            detail = f"HTTP {status}"
            if body:
                detail += f": {body}"
            return detail
        if exc:
            return f"request failed ({exc})"
        return "request failed without an observed response"
    except Exception:  # noqa: BLE001
        return fallback
