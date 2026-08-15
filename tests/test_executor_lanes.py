import asyncio
import threading
import unittest
from unittest.mock import patch

import anyio

from omniseek import server
from omniseek.core import fetcher


class _SyncAdapter:
    def search(self, query: str, limit: int) -> list[str]:
        return [f"{query}:{limit}"]


class ExecutorLaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_dispatch_waits_for_egress_admission_before_starting_worker(self):
        semaphore = threading.BoundedSemaphore(1)
        semaphore.acquire()
        worker_started = asyncio.Event()

        async def fake_run_sync(fn, *args, **kwargs):
            worker_started.set()
            return ["dispatched"]

        released = False
        with patch.object(fetcher, "_EGRESS_SEM", semaphore), \
             patch.object(fetcher.anyio.to_thread, "run_sync", side_effect=fake_run_sync):
            task = asyncio.create_task(fetcher._dispatch_search(_SyncAdapter(), "q", 1))
            try:
                await asyncio.sleep(0.03)
                self.assertFalse(
                    worker_started.is_set(),
                    "a request waiting for the global egress permit must stay a coroutine, not occupy a worker",
                )
                semaphore.release()
                released = True
                self.assertEqual(await asyncio.wait_for(task, timeout=1), ["dispatched"])
            finally:
                if not released:
                    semaphore.release()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_plain_eye_sources_completes_while_default_worker_lane_is_saturated(self):
        limiter = anyio.to_thread.current_default_thread_limiter()
        old_tokens = limiter.total_tokens
        old_limiter_set = server._limiter_set
        old_portal_bound = server._portal_bound_once
        blocker_started = threading.Event()
        blocker_release = threading.Event()

        def block_default_worker() -> None:
            blocker_started.set()
            blocker_release.wait()

        limiter.total_tokens = 1
        server._limiter_set = True
        server._portal_bound_once = True
        blocker = asyncio.create_task(anyio.to_thread.run_sync(block_default_worker))
        while not blocker_started.is_set():
            await asyncio.sleep(0)

        orient = asyncio.create_task(server.omniseek_sources())
        try:
            await asyncio.sleep(0.05)
            self.assertTrue(
                orient.done(),
                "the lightweight orient path must not queue behind saturated data-plane workers",
            )
            result = orient.result()
            self.assertGreater(result["count"], 0)
            self.assertGreater(result["backend_count"], 0)
        finally:
            if not orient.done():
                orient.cancel()
            blocker_release.set()
            await asyncio.gather(blocker, orient, return_exceptions=True)
            limiter.total_tokens = old_tokens
            server._limiter_set = old_limiter_set
            server._portal_bound_once = old_portal_bound


if __name__ == "__main__":
    unittest.main()
