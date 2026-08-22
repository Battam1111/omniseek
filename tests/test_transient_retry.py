import contextlib
import httpx
import anyio

import omniseek.server
from omniseek.core import diag
from omniseek.core import http
from omniseek.core.sources.api import arxiv_source


def _clear_diag():
    diag.drain()


def test_connect_error_retries_once_and_records_diag():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("TLS blip", request=request)
        return httpx.Response(200, stream=httpx.ByteStream(b"ok"), request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    old_client = http._get_client
    old_block = http._netguard.security_block_reason
    http._get_client = lambda: client
    http._netguard.security_block_reason = lambda url: None
    _clear_diag()
    diag.enable()
    try:
        result = http.get("https://example.com/retry")
        captures = diag.drain()
    finally:
        http._get_client = old_client
        http._netguard.security_block_reason = old_block
        client.close()
    assert result is not None and result.text == "ok"
    assert len(calls) == 2
    assert any(c["helper"] == "http.retry_transient"
               and c["url"] == "https://example.com/retry"
               and "ConnectError" in c.get("exc", "")
               for c in captures)


def test_http_status_error_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, stream=httpx.ByteStream(b"slow down"), request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    old_client = http._get_client
    old_block = http._netguard.security_block_reason
    http._get_client = lambda: client
    http._netguard.security_block_reason = lambda url: None
    _clear_diag()
    diag.enable()
    try:
        result = http.get("https://example.com/429")
        captures = diag.drain()
    finally:
        http._get_client = old_client
        http._netguard.security_block_reason = old_block
        client.close()
    assert result is None
    assert len(calls) == 1
    assert not any(c["helper"] == "http.retry_transient" for c in captures)


def test_ssrf_guard_connect_error_is_not_retried():
    calls = []

    class RejectingStream:
        def __enter__(self):
            calls.append(1)
            raise httpx.ConnectError("refused SSRF-class url (private_ip)", request=None)

        def __exit__(self, exc_type, exc, tb):
            return False

    class RejectingClient:
        def stream(self, *args, **kwargs):
            return RejectingStream()

    old_client = http._get_client
    old_block = http._netguard.security_block_reason
    http._get_client = lambda: RejectingClient()
    http._netguard.security_block_reason = lambda url: None
    _clear_diag()
    diag.enable()
    try:
        result = http.get("https://example.com/ssrf")
        captures = diag.drain()
    finally:
        http._get_client = old_client
        http._netguard.security_block_reason = old_block
    assert result is None
    assert len(calls) == 1
    assert not any(c["helper"] == "http.retry_transient" for c in captures)


def test_async_connect_error_retries_once():
    calls = []

    async def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("TLS blip", request=request)
        return httpx.Response(200, stream=httpx.ByteStream(b"ok"), request=request)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        old_client = http._aget_client
        old_block = http._netguard.security_block_reason
        http._aget_client = lambda: client
        http._netguard.security_block_reason = lambda url: None
        try:
            result = await http.aget("https://example.com/async-retry")
        finally:
            http._aget_client = old_client
            http._netguard.security_block_reason = old_block
            await client.aclose()
        return result

    result = anyio.run(run)
    assert result is not None and result.text == "ok"
    assert len(calls) == 2


def test_arxiv_guard_disables_shared_transient_retry():
    calls = []
    old_get_text = arxiv_source.http.get_text
    old_is_open = arxiv_source._guard.is_open
    old_pace = arxiv_source._guard.pace

    def fail_once(*args, **kwargs):
        calls.append(kwargs)
        raise httpx.ConnectError("penalty-box connect failure")

    arxiv_source.http.get_text = fail_once
    arxiv_source._guard.is_open = lambda: False
    arxiv_source._guard.pace = lambda **kwargs: None
    try:
        with contextlib.suppress(httpx.ConnectError):
            arxiv_source._arxiv_get_text("https://export.arxiv.org/api/query")
    finally:
        arxiv_source.http.get_text = old_get_text
        arxiv_source._guard.is_open = old_is_open
        arxiv_source._guard.pace = old_pace
    assert len(calls) == 1
    assert calls[0]["retry_transient"] is False


if __name__ == "__main__":
    test_connect_error_retries_once_and_records_diag()
    test_http_status_error_is_not_retried()
    test_ssrf_guard_connect_error_is_not_retried()
    test_async_connect_error_retries_once()
    test_arxiv_guard_disables_shared_transient_retry()
    print("part1 transient retry tests passed")
