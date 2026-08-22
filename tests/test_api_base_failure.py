import anyio

import omniseek.server
from omniseek.core import diag
from omniseek.core.sources.api._base import BaseAPIAdapter


class RaisingAPIAdapter(BaseAPIAdapter, register=False):
    name = "smoke_api_failure"
    description = "minimal adapter for the base failure contract"

    def _raw_fetch(self, query: str, limit: int) -> list:
        raise RuntimeError("upstream connect failed")

    async def _araw_fetch(self, query: str, limit: int) -> list:
        raise RuntimeError("async upstream connect failed")

    async def asearch(self, query: str, limit: int = 10) -> list:
        return await self._aapi_search(
            query,
            limit,
            araw_fetch=lambda: self._araw_fetch(query, limit),
        )

    def _to_document(self, raw):
        return None


def test_sync_search_degrades_and_records_adapter_failure():
    adapter = RaisingAPIAdapter()
    diag.enable()
    result = adapter.search("base-failure-sync-unique", limit=1)
    captures = diag.drain()
    assert result == []
    assert any(c["helper"] == "smoke_api_failure.fetch_failed"
               and "upstream connect failed" in c.get("exc", "")
               for c in captures)


def test_async_search_degrades_and_records_adapter_failure():
    adapter = RaisingAPIAdapter()

    async def run():
        diag.enable()
        result = await adapter.asearch("base-failure-async-unique", limit=1)
        return result, diag.drain()

    result, captures = anyio.run(run)
    assert result == []
    assert any(c["helper"] == "smoke_api_failure.fetch_failed"
               and "async upstream connect failed" in c.get("exc", "")
               for c in captures)


if __name__ == "__main__":
    test_sync_search_degrades_and_records_adapter_failure()
    test_async_search_degrades_and_records_adapter_failure()
    print("part2 api base failure tests passed")
