"""The pooled async HTTP client survives being reached from a different event loop (2026-08-30).

httpx.AsyncClient's connection pool is bound to the loop that created it. The shared client here
is a module global built once under a double-checked lock, so reaching it from a second loop used
to hand back a pool bound to a loop that may already be closed. The symptom is either a
dead-connection error or a bare "Event loop is closed", neither of which points at the cause.

Nothing in today's call graph creates a second loop (the service runs one), so this never fired
in production. It is guarded anyway because the failure is expensive to diagnose and the check is
one identity comparison. The guard REBUILDS rather than raises: raising would push an internal
lifecycle problem onto every caller.
"""
import asyncio
import unittest

from omniseek.core import http


class AsyncClientLoopGuardTests(unittest.TestCase):
    def setUp(self):
        # The client is module-global state. Save and restore it, the way the job-registry tests
        # do, so this file cannot leak a client (or a loop binding) into whatever runs next.
        self._saved_client = http._aclient
        self._saved_loop = http._aclient_loop
        http._aclient = None
        http._aclient_loop = None

    def tearDown(self):
        http._aclient = self._saved_client
        http._aclient_loop = self._saved_loop

    def test_same_loop_reuses_one_client(self):
        """The pooling behaviour that existed before the guard must be untouched."""
        async def twice():
            return http._aget_client(), http._aget_client()

        a, b = asyncio.run(twice())
        self.assertIs(a, b, "the shared client should be built once per loop")

    def test_a_second_loop_gets_a_rebuilt_client(self):
        """The whole point: a client bound to a finished loop must not be handed out again."""
        async def grab():
            return http._aget_client()

        first = asyncio.run(grab())
        second = asyncio.run(grab())
        self.assertIsNot(second, first,
                         "a client bound to the previous (now closed) loop was reused")

    def test_the_loop_binding_is_recorded(self):
        async def grab():
            return http._aget_client(), asyncio.get_running_loop()

        _, loop = asyncio.run(grab())
        self.assertIs(http._aclient_loop, loop,
                      "the guard must remember which loop the pool belongs to")

    def test_an_injected_client_is_left_alone(self):
        """The regression this guard caused on its first day.

        The smoke checks and several tests install a stub straight into this global to keep
        requests inside the process. Such a client carries no loop binding, and the first version
        of the guard treated 'no binding' as 'wrong binding' and replaced it: the stub vanished,
        a real client took over, and six checks failed at once. Not knowing which loop a client
        belongs to is a reason to leave it alone."""
        sentinel = object()
        http._aclient = sentinel          # injected the way a test would: no binding recorded
        http._aclient_loop = None

        async def grab():
            return http._aget_client()

        self.assertIs(asyncio.run(grab()), sentinel,
                      "the guard replaced an injected client instead of leaving it alone")

    def test_calling_outside_a_loop_does_not_clobber_the_binding(self):
        """Construction can happen off-loop. That must not rebind the pool to None and force a
        rebuild on the next real call."""
        async def grab():
            return http._aget_client(), asyncio.get_running_loop()

        built, loop = asyncio.run(grab())
        off_loop = http._aget_client()          # no running loop here
        self.assertIs(off_loop, built, "an off-loop call should reuse, not rebuild")
        self.assertIs(http._aclient_loop, loop, "an off-loop call must leave the binding alone")


if __name__ == "__main__":
    unittest.main()
