import threading
import time
import unittest
from pathlib import Path

import anyio

from penumbra.core._guard import GateBusy, bounded_async_slot


class AsyncSlotTests(unittest.TestCase):
    def test_async_gate_acquires_are_centralized_in_shared_helper(self):
        penumbra_root = Path(__file__).parents[1] / "src" / "penumbra" / "core"
        offenders = []
        for path in penumbra_root.rglob("*.py"):
            if path.name == "_guard.py":
                continue
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "to_thread.run_sync" in line and ".acquire" in line:
                    offenders.append(f"{path.relative_to(penumbra_root)}:{line_no}")
        self.assertEqual([], offenders)

    def test_saturated_slot_fails_within_bound_without_taking_permit(self):
        async def scenario():
            sema = threading.BoundedSemaphore(1)
            self.assertTrue(sema.acquire())
            try:
                started = time.monotonic()
                with self.assertRaises(GateBusy):
                    async with bounded_async_slot(
                        sema,
                        0.03,
                        lambda waited: GateBusy(f"busy after {waited:.2f}s"),
                    ):
                        self.fail("a saturated slot must not enter its body")
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertFalse(sema.acquire(blocking=False))
            finally:
                sema.release()

        anyio.run(scenario)

    def test_cancel_during_acquire_does_not_leak_a_permit(self):
        async def scenario():
            sema = threading.BoundedSemaphore(1)
            self.assertTrue(sema.acquire())
            async def release_initial_permit():
                await anyio.sleep(0.06)
                sema.release()

            async with anyio.create_task_group() as tg:
                tg.start_soon(release_initial_permit)
                with anyio.move_on_after(0.03):
                    async with bounded_async_slot(
                        sema,
                        0.3,
                        lambda waited: GateBusy(f"busy after {waited:.2f}s"),
                    ):
                        await anyio.sleep(0.1)
                tg.cancel_scope.cancel()

            self.assertTrue(sema.acquire(blocking=False))
            try:
                pass
            finally:
                sema.release()

            with anyio.fail_after(0.5):
                async with bounded_async_slot(
                    sema,
                    0.1,
                    lambda waited: GateBusy(f"busy after {waited:.2f}s"),
                ):
                    return

        anyio.run(scenario)


if __name__ == "__main__":
    unittest.main(verbosity=2)
