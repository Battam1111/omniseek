"""The shared curl_cffi entry point for modules that send through libcurl themselves.

Some sources need curl_cffi's browser TLS fingerprint (anti-bot hosts) and keep their own request or
session calls instead of the shared tier (``http.get_impersonated``). Before 2026-10-04 they imported
``curl_cffi.requests`` directly, so their responses reached nothing of OmniSeek: no rate-limit readings
for the upstream health block, no Retry-After deferral, and a health check that got HTTP 429 could
only report "fetch failed" (down) instead of "not verified".

They import this module instead (``from omniseek.core import curl as _creq``): the same ``get`` /
``post`` / ``request`` / ``Session`` they used, and every response is recorded once, on its own host,
through ``upstreams.observe_response`` (which also feeds a running health check's ledger, see
``_probe``), and an error body that says it is a rate limit is noted for that ledger too. Nothing else
changes: same arguments, same return values, same exceptions; redirects are still followed inside
libcurl. Importing this module raises ImportError when curl_cffi is missing, exactly like the import
it replaces, so each module's own fallback stays as it was.

A failed request (an error answer, 4xx / 5xx, or an exception) is also noted for an armed failure
capture (``diag``), with its HTTP status, so a health check or a search drill can say what the upstream
answered ("HTTP 504", not "fetch failed"); ``failure(exc)`` words an exception the same way for a
module that reads the status through ``raise_for_status``.
"""

from __future__ import annotations

from typing import Any

from curl_cffi import requests as _real

from omniseek.core import _probe, diag, upstreams

_BODY_PEEK = 2000   # characters of an error body read for the rate-limit words


def _record(method: str, url: str, resp: Any) -> Any:
    """Record one response (never raises; a stub without the attributes records nothing)."""
    try:
        where = str(getattr(resp, "url", "") or url)
        upstreams.observe_response(where, resp)
        status = getattr(resp, "status_code", None)
        if isinstance(status, int) and status >= 400:
            text = str(getattr(resp, "text", "") or "")[:_BODY_PEEK]
            if status != 429:
                _probe.note_error_body(status, text, where=where)
            diag.note(f"curl.{str(method).lower()}", url=where, status=status, body=text or None)
    except Exception:  # noqa: BLE001 (recording must never break an egress)
        pass
    return resp


def _failed(method: str, url: str, exc: BaseException) -> None:
    diag.note(f"curl.{str(method).lower()}", url=url, exc=exc)


def failure(exc: BaseException) -> str:
    """One failed curl_cffi call, worded with its HTTP status when it carries a response (the
    ``HTTPError`` of ``raise_for_status``): "HTTP 504 Gateway Timeout", else "<type>: <message>"."""
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    if isinstance(status, int) and status > 0:
        reason = str(getattr(resp, "reason", "") or "").strip()
        return f"HTTP {status}" + (f" {reason}" if reason else "")
    return f"{type(exc).__name__}: {str(exc)[:80]}"


class Session(_real.Session):
    """``curl_cffi.requests.Session`` whose every response is recorded (see the module docstring)."""

    def request(self, method, url, *args, **kwargs):  # type: ignore[override]
        try:
            resp = super().request(method, url, *args, **kwargs)
        except Exception as exc:
            _failed(method, url, exc)
            raise
        return _record(method, url, resp)


def request(method: str, url: str, **kwargs: Any):
    """``curl_cffi.requests.request`` with the response recorded."""
    try:
        resp = _real.request(method, url, **kwargs)
    except Exception as exc:
        _failed(method, url, exc)
        raise
    return _record(method, url, resp)


def get(url: str, **kwargs: Any):
    return request("GET", url, **kwargs)


def post(url: str, **kwargs: Any):
    return request("POST", url, **kwargs)
