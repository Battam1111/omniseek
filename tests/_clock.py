"""The lower bound a test may assert for "at least X seconds passed", on the clock it really reads.

A test that stamps ``time.monotonic()`` before and after something that waits X seconds can read a
little less than X while the code under test did wait X. Two effects, each worth at most one tick:

  1. A reading trails the true moment by up to one resolution, so a span between two readings can
     come out up to one resolution short of a wait done against a finer timer (``time.sleep`` uses a
     high-resolution timer on Windows while ``time.monotonic`` there is GetTickCount64 before Python
     3.13: 15.625 ms; a ``time.sleep(0.1)`` read as about 0.094 s there, and ``elapsed >= 1.1`` in
     test_shutdown_cap failed on CI with 1.094).
  2. A timer can end on that clock up to one tick early: asyncio runs a timer whose deadline is within
     the clock's resolution of now (BaseEventLoop._run_once), and a timed wait for a lock or event on
     Windows ends on the system timer, so it can return up to part of a timer period early (measured
     0.04 ms early on Python 3.13, whose monotonic clock resolves 100 ns).

So the tick is the monotonic clock's declared resolution, but never below 1 ms, the shortest timer
period Windows offers, and the allowance is two ticks: 31.25 ms on Python 3.11 on Windows, 2 ms
elsewhere (``_assert_paced`` in test_upstream_limits allows 2 ms on a fine clock for the same reason).
An upper bound needs none of this and keeps its own margin.

Not named test_*.py on purpose (unittest discover must not collect it). Importers use the two-form
guard of tests/_repo_only.py:

    try:
        from _clock import at_least
    except ImportError:
        from tests._clock import at_least
"""
import time

WAIT_TICK_FLOOR_S = 0.001
TICK_S = max(time.get_clock_info("monotonic").resolution, WAIT_TICK_FLOOR_S)
SLACK_S = 2 * TICK_S


def at_least(seconds: float) -> float:
    """The bound for ``assertGreaterEqual(elapsed, ...)`` when the code under test waited ``seconds``
    measured on ``time.monotonic``: ``seconds`` less two ticks of that clock (see the module text)."""
    return seconds - SLACK_S
