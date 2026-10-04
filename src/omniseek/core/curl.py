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
"""

from __future__ import annotations

from typing import Any

from curl_cffi import requests as _real

from omniseek.core import _probe, upstreams

_BODY_PEEK = 2000   # characters of an error body read for the rate-limit words


def _record(url: str, resp: Any) -> Any:
    """Record one response (never raises; a stub without the attributes records nothing)."""
    try:
        where = str(getattr(resp, "url", "") or url)
        upstreams.observe_response(where, resp)
        status = getattr(resp, "status_code", None)
        if isinstance(status, int) and status >= 400 and status != 429:
            _probe.note_error_body(status, str(getattr(resp, "text", "") or "")[:_BODY_PEEK], where=where)
    except Exception:  # noqa: BLE001 (recording must never break an egress)
        pass
    return resp


class Session(_real.Session):
    """``curl_cffi.requests.Session`` whose every response is recorded (see the module docstring)."""

    def request(self, method, url, *args, **kwargs):  # type: ignore[override]
        return _record(url, super().request(method, url, *args, **kwargs))


def request(method: str, url: str, **kwargs: Any):
    """``curl_cffi.requests.request`` with the response recorded."""
    return _record(url, _real.request(method, url, **kwargs))


def get(url: str, **kwargs: Any):
    return request("GET", url, **kwargs)


def post(url: str, **kwargs: Any):
    return request("POST", url, **kwargs)
