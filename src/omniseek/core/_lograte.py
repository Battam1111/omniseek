"""A per-logger WARNING/ERROR rate-limit filter — no single logger may flood the log.

Installed on the root handler at service startup (serve_http / server main). A shared failing
dependency otherwise makes MANY subsystems each spam near-identical warnings: the canonical case is
a FRESH install with no OMNISEEK_CONTACT_EMAIL, where OpenAlex refuses the polite pool, rate-limits
every affiliation lookup, and the breaker opens — so researcher_watch, the ~39 org_watch labs, the
background recall backfill, and any live query ALL emit "fetch failed: circuit open" across many
threads. It greeted a first-time deployer with a screenful that reads as broken.

Per-subsystem silencing proved FRAGILE: a contextvar reached only the workers spawned via
copy_context (researcher_watch's, not org_watch's plain pool), and a prewarm-scoped hush could not
cover the SAME storm re-emitted by a different subsystem seconds later. The fresh-docker boot log
was the oracle that caught each gap. This filter dissolves the whole class at the ONE place every
warning must pass to be rendered — the handler — so it is thread- AND subsystem-agnostic: the first
``burst`` records per logger per ``window_s`` pass; the rest are dropped and COUNTED, and the next
record after the window carries a "[+N similar suppressed]" note so the cap is never silent (the
same no-silent-truncation discipline the rest of the system holds). Only WARNING/ERROR are limited;
DEBUG/INFO always pass, and CRITICAL is never throttled. The first few of any storm still surface
(you still learn the source is failing — on a fresh install that is even a useful hint to set the
contact email), only the flood is capped.
"""

from __future__ import annotations

import logging
import threading
import time

_INSTALLED_MARK = "_omniseek_ratelimit"


class WarningRateLimit(logging.Filter):
    """Drop-in handler filter: cap each logger to ``burst`` WARNING/ERROR records per ``window_s``.

    Keyed by logger name (the storm is many DISTINCT messages from ONE logger — org_watch[ai2],
    org_watch[amii], ... — so per-logger is the right grain). ``clock`` is injectable for tests.
    """

    def __init__(self, burst: int = 3, window_s: float = 60.0, clock=time.monotonic):
        super().__init__()
        self._burst = burst
        self._window_s = window_s
        self._clock = clock
        self._lock = threading.Lock()
        # logger name -> [window_start, shown_in_window, suppressed_in_window]
        self._by_logger: dict[str, list[float]] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING or record.levelno >= logging.CRITICAL:
            return True
        now = self._clock()
        with self._lock:
            st = self._by_logger.get(record.name)
            if st is None or (now - st[0]) >= self._window_s:
                carried = int(st[2]) if st else 0
                self._by_logger[record.name] = [now, 1.0, 0.0]
                if carried:
                    try:  # annotate this first-of-new-window record; never let it break logging
                        record.msg = "%s  [+%d similar suppressed in the last %ds]" % (
                            record.getMessage(), carried, int(self._window_s))
                        record.args = None
                    except Exception:  # noqa: BLE001
                        pass
                return True
            if st[1] < self._burst:
                st[1] += 1
                return True
            st[2] += 1
            return False


def install_on_root(burst: int = 3, window_s: float = 60.0) -> None:
    """Attach one WarningRateLimit to every root handler, once (idempotent across repeat calls).

    Attached to the HANDLER (not a logger), so it sees every record that PROPAGATES up from the
    ``omniseek.core.*`` / ``omniseek.core.*`` tree — a filter on a logger would miss propagated
    child records. Call right after basicConfig; safe to call again if handlers are added later."""
    for h in logging.getLogger().handlers:
        if not any(getattr(f, _INSTALLED_MARK, False) for f in h.filters):
            filt = WarningRateLimit(burst, window_s)
            setattr(filt, _INSTALLED_MARK, True)
            h.addFilter(filt)
