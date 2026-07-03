"""Cache pre-warmer for the QUERY-INDEPENDENT slow sources — driven by the in-service warmer
thread (penumbra.serve_http) and, for a manual one-shot, by ``python -m penumbra.core.prewarm``.

Keeps sources whose EXPENSIVE fetch is cached independently of the query/limit hot, so a broad
search (for ANY query) hits warm cache instead of paying their live cost under the 78-source
fan-out. Only sources whose cache key excludes the query qualify (warming once helps every query):
  - researcher_watch / ai_residencies / overseas_ai_jobs  (the original three)
  - all org_watch labs  (key = org name + affiliations, 6h TTL — discovered by isinstance, so
    adding a lab to org_watch.json is auto-warmed)
  - scrape_js_sites / gter  (key = site count; they were the two slowest sources online, 44s/33s)
  - ircc_ee_rounds  (key = "rounds|v1", query-independent; a CDP-nav source whose ONLY warmable
    form this is — it leaves the cold walled critical path; canada.ca, no account)
  - zhihu_users  (after the per-handle refactor its expensive CDP nav is cached per-handle,
    query-independent, so the warmer keeps each tracked researcher's posts hot — note: this is a
    LOGGED-IN zhihu access on the warm cadence, low-rate + human-plausible, same kind as the
    watchtower's periodic CDP probes)

REFRESH-IF-NEAR-EXPIRY (2026-06-16): each warm uses a cache REFRESH MARGIN, not a blanket
fresh=True. fresh=True FORCE-bypasses the cache, so it re-paid the full live cost EVERY 30-min
cycle on data whose TTL is 2h to 6h: researcher_watch + the ~39 org_watch labs are OpenAlex-
backed, so the warmer self-generated ~72 OpenAlex calls/cycle x 48 cycles/day ~= 3,456 calls/day
(~35% of the free daily budget) to re-warm data that changes every 6h. A ~12x over-refresh. Now
the warm sets cache.set_refresh_margin(REFRESH_MARGIN_S): the adapter's OWN cache.get(key) calls
return the still-warm value untouched and refetch (rewriting the cache) ONLY a key that is missing
or within the margin of expiry. The margin >= the warm interval, so a key that would lapse before
the next cycle is refreshed THIS cycle (caches stay hot), but a comfortably-warm key costs zero
live work. So a 6h-TTL source is refetched roughly once per TTL (~11 cycles) instead of every
cycle: ~11x fewer warm OpenAlex calls, same hot caches. The check targets the EXACT keys the warm
fetch would write (per-PI for researcher_watch, per-affiliation for org_watch, per-source for the
rest), because it happens inside the same get() the adapter already calls. Still purely additive:
it changes no result, only whether a later broad pays the live cost.

WHY deadline_s=None on the warm fetch: the refresh margin is a contextvar, and fetch_one's default
deadline runs the adapter in a fresh backstop thread that does NOT inherit contextvars (a plain
threading.Thread resets them), which would silently drop the margin. deadline_s=None runs the
adapter inline on THIS thread, so the margin (and the adapter's copy_context() workers) see it. The
warmer is a background daemon whose job is to pay slow costs off the critical path, so trading the
per-call backstop for correct margin propagation is right here; each source keeps its own internal
timeouts + the per-source try/except below, and warm_loop wraps the whole cycle.

WHY in-service (2026-06-14): the standalone launchd cron (com.penumbra.core-prewarm) was observed
to fire only ONCE at load and never on its 30-min StartInterval (runs=1 after 14.5h), so the
warmed caches silently expired (6h TTL) and Lever B's speedup evaporated in production. Tying the
warmer to the always-on eye-http service (a daemon thread) makes it fire reliably. (That dead plist
com.penumbra.core-prewarm was never installed on the mini and has since been REMOVED from the repo
2026-06-16; the in-service daemon is the sole AUTOMATIC warmer. The standalone warm CLI moved INTO
this module's ``__main__`` in the P9 rebuild -- ``python -m penumbra.core.prewarm`` -- so the manual
one-shot lives beside the loop it warms, with no extra script to keep in sync.)
"""

from __future__ import annotations

import contextvars
import logging
import time

log = logging.getLogger(__name__)

# ── prewarm is best-effort: its fetch failures are NON-EVENTS ─────────────────────────────────
# A warm that fails just leaves that cache cold; the live query path then handles the source
# exactly as if it had never warmed. So a source-adapter WARNING logged DURING a warm pass is
# noise, not signal -- most visibly on a FRESH install, where the OpenAlex-backed warmed sources
# (researcher_watch + the org_watch labs) hit the polite pool with no contact email, get rate-
# limited, and would otherwise greet a first-time deployer with a screenful of "OpenAlex fetch
# failed". Fixed structurally in ONE place: warm_sources sets the _PREWARMING contextvar (which
# propagates into the adapters' copy_context() per-item workers), and a filter on the root handlers
# drops WARNING records from the source tree WHILE that contextvar is set. Thread-scoped via the
# contextvar, so a concurrent LIVE query (no _PREWARMING in its context) still warns, and prewarm's
# OWN per-source summary (this module's logger, not under `.sources`) is untouched.
_PREWARMING: "contextvars.ContextVar[bool]" = contextvars.ContextVar("penumbra_prewarming", default=False)
_SRC_LOGGER_PREFIX = __name__.rsplit(".", 1)[0] + ".sources"   # rename-safe (penumbra.core / penumbra.core)


class _PrewarmNoiseFilter(logging.Filter):
    """Drop a source-tree WARNING record while a prewarm pass owns this context (see above). Only
    exactly WARNING is dropped -- a rarer ERROR/CRITICAL from a source is left visible."""
    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.levelno == logging.WARNING
                    and _PREWARMING.get()
                    and record.name.startswith(_SRC_LOGGER_PREFIX))


def _ensure_prewarm_log_filter() -> None:
    """Install the noise filter on the root handlers ONCE (idempotent; handlers exist after the
    service's logging.basicConfig). A filter on the HANDLERS (not a logger) is what sees the
    propagated child-logger records from every source."""
    for h in logging.getLogger().handlers:
        if not any(getattr(f, "_penumbra_prewarm", False) for f in h.filters):
            filt = _PrewarmNoiseFilter()
            filt._penumbra_prewarm = True   # idempotence marker
            h.addFilter(filt)

_BASE = ["researcher_watch", "ai_residencies", "overseas_ai_jobs", "scrape_js_sites", "gter",
         "ircc_ee_rounds", "zhihu_users"]
WARM_INTERVAL_S = 1800  # 30 min — well under every warmed source's TTL (2h/3h/6h)

# Cache refresh margin: a warm refetches a source's cache key ONLY when it is missing or expires
# within this many seconds (else it is served from cache, ZERO live cost). Set >= WARM_INTERVAL_S
# (plus slack for the warm pass's own duration + scheduling jitter) so any key that would lapse
# before the NEXT cycle is refreshed THIS cycle: caches never go cold, yet a comfortably-warm key
# is left untouched. With a 6h TTL this turns ~every-cycle refetch into ~once-per-TTL (~11 cycles),
# i.e. ~11x fewer warm OpenAlex calls. The old fresh=True (full bypass every cycle) was a ~12x
# over-refresh of the OpenAlex-backed sources (researcher_watch + the org_watch labs).
REFRESH_MARGIN_S = WARM_INTERVAL_S + 300


def warm_list() -> list[str]:
    """Static base + every org_watch lab (isinstance-discovered → zero maintenance on add)."""
    from penumbra.core import fetcher
    from penumbra.core.sources.api.org_watch_source import _OrgWatchAdapter
    org_watch = sorted(n for n in fetcher.all_adapter_names()
                       if isinstance(fetcher.get_adapter(n), _OrgWatchAdapter))
    return _BASE + org_watch


def warm_sources() -> tuple[int, int]:
    """Warm every source in warm_list() (empty query → the query-independent fetch). Refresh-if-
    near-expiry: a cache.set_refresh_margin(REFRESH_MARGIN_S) makes each adapter's own cache.get
    refetch (and rewrite) ONLY a key that is missing or within the margin of expiry, while a still-
    warm key is served from cache at zero live cost. Run with deadline_s=None so the adapter runs
    inline on this thread and the margin contextvar (and its copy_context() workers) sees it (a
    backstop thread would reset the contextvar and silently drop the margin). Returns (warmed,
    total). One bad source never blocks the rest."""
    from penumbra.core import cache, fetcher
    srcs = warm_list()
    failures = 0
    _ensure_prewarm_log_filter()
    cache.set_refresh_margin(REFRESH_MARGIN_S)
    _prewarm_tok = _PREWARMING.set(True)  # best-effort context: source WARNINGs are non-events now
    try:
        for src in srcs:
            t0 = time.monotonic()
            try:
                docs = fetcher.fetch_one(src, "", limit=50, deadline_s=None)
                log.info("prewarm %s: %d docs in %.1fs", src, len(docs), time.monotonic() - t0)
            except Exception as exc:  # noqa: BLE001 — one bad source never blocks the rest
                failures += 1
                log.warning("prewarm %s FAILED in %.1fs: %s", src, time.monotonic() - t0, exc)
    finally:
        cache.set_refresh_margin(0)  # never leak the margin into a later request on this thread
        _PREWARMING.reset(_prewarm_tok)
    return len(srcs) - failures, len(srcs)


def warm_loop(interval_s: float = WARM_INTERVAL_S) -> None:
    """Forever: warm on entry (kills the cold-herd right after a service (re)start) then every
    ``interval_s``. Designed to run as a daemon thread inside the always-on eye-http service."""
    while True:
        try:
            ok, total = warm_sources()
            log.info("prewarm cycle: %d/%d warmed", ok, total)
        except Exception as exc:  # noqa: BLE001 — never let the warmer thread die
            log.warning("prewarm cycle errored: %s", exc)
        time.sleep(interval_s)


def _main() -> int:
    """Manual one-shot warm (``python -m penumbra.core.prewarm``) — the manual warm CLI, folded in
    here in the P9 rebuild. The PRODUCTION warmer is the in-service daemon (serve_http starts
    warm_loop); this is for a MANUAL warm right after a deploy, before the service's first cycle
    completes. Boots the registry the same way the service does (adapters self-register on import),
    warms once, and reports how many sources warmed. Exit: 0 all warmed / 1 some failed / 2 import."""
    import logging
    from datetime import datetime
    logging.basicConfig(level=logging.INFO, format="  %(message)s")
    print(f"[{datetime.now().isoformat(timespec='seconds')}] eye prewarm (manual) starting")
    try:
        from penumbra.server import load_sources
        load_sources()
    except Exception as exc:  # noqa: BLE001
        print(f"  FATAL import: {exc}")
        return 2
    ok, total = warm_sources()
    print(f"  done. {ok}/{total} warmed")
    return 0 if ok == total else 1


if __name__ == "__main__":
    import sys
    sys.exit(_main())
