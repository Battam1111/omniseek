from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import patch

from omniseek.core import fetcher, recall
from omniseek.core.normalize import Document


class _AsyncAdapter:
    name = "_test_async_ingest"
    needs_credentials = False
    description = "async ingest thread-boundary fixture"

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        return [
            Document(
                source=self.name,
                source_id="item-1",
                url="https://example.test/item-1",
                title="Async journal boundary",
                content="durable observation",
            )
        ]


class AsyncIngestTests(unittest.TestCase):
    def test_durable_ingest_runs_off_the_event_loop_thread(self):
        adapter = _AsyncAdapter()
        ingest_threads: list[int] = []

        def record_ingest(docs) -> None:
            self.assertEqual(len(docs), 1)
            ingest_threads.append(threading.get_ident())

        async def run_search() -> tuple[int, dict]:
            loop_thread = threading.get_ident()
            results, _meta = await fetcher.asearch_many(
                "journal boundary",
                sources=[adapter.name],
                limit_per_source=1,
                deadline_s=1.0,
            )
            return loop_thread, results

        fetcher.register_adapter_live(adapter)
        try:
            with patch.object(recall, "maybe_ingest", side_effect=record_ingest):
                loop_thread, results = asyncio.run(run_search())
        finally:
            fetcher.unregister_adapter(adapter.name)

        self.assertEqual(len(results[adapter.name]), 1)
        self.assertEqual(len(ingest_threads), 1)
        self.assertNotEqual(ingest_threads[0], loop_thread)


if __name__ == "__main__":
    unittest.main()
