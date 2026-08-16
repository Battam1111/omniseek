"""OmniSeek eye as a shared HTTP MCP service (streamable-http + bearer-token auth).

ONE always-on process (launchd com.omniseek.organ.eye-http) that every agent / window connects to
over the network, instead of each Claude window spawning its own stdio-over-ssh server (N
heavy processes, a cold 86-adapter load each). Token-gated, because OmniSeek drives
credentialed + logged-in-browser tools: it must NEVER serve open.

Run:    python -m omniseek.serve_http      (env: OMNISEEK_HTTP_HOST / OMNISEEK_HTTP_PORT)
Token:  ~/.omniseek/credentials/omniseek_http.json  ->  {"token": "..."}  (mode 600)
Client: send  Authorization: Bearer <token>  on every request. Unauthed -> 401.
        /healthz is open (liveness only, returns no data).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from pathlib import Path

import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from omniseek.server import mcp

logger = logging.getLogger("omniseek.serve_http")

# Bind LOOPBACK by default: OmniSeek drives credentialed + logged-in-browser tools, so a stranger's
# fresh deploy must NOT be reachable off-box. A deployer who wants LAN/tailnet access sets
# OMNISEEK_HTTP_HOST=0.0.0.0 explicitly (and owns putting it behind a firewall / reverse proxy).
HOST = os.environ.get("OMNISEEK_HTTP_HOST", "127.0.0.1")
_IS_LOOPBACK = HOST in ("127.0.0.1", "::1", "localhost")
PORT = int(os.environ.get("OMNISEEK_HTTP_PORT", "8765"))
_TOKEN_PATH = Path.home() / ".omniseek" / "credentials" / "omniseek_http.json"


def _load_token() -> str:
    # The bearer secret is the SOLE access boundary to the credentialed eye, so its confidentiality
    # and strength are correctness properties. Fail closed on a weak or leaky credential:
    #   - refuse a token FILE that group/other could read on POSIX (a real local leak);
    #   - refuse a missing / non-string / too-short token (a truthy "x" is not a secret).
    try:
        st = _TOKEN_PATH.stat()
    except OSError as exc:
        raise SystemExit(f"refusing to start: cannot stat token file {_TOKEN_PATH}: {exc}")
    if os.name == "posix" and (st.st_mode & 0o077):
        raise SystemExit(
            f"refusing to start: token file {_TOKEN_PATH} is group/other-accessible "
            f"(mode {oct(st.st_mode & 0o777)}); run: chmod 600 {_TOKEN_PATH}")
    try:
        tok = (json.loads(_TOKEN_PATH.read_text()) or {}).get("token")
    except Exception:  # noqa: BLE001
        tok = None
    if not isinstance(tok, str) or len(tok) < 16:
        raise SystemExit(
            f"refusing to start: token at {_TOKEN_PATH} is missing or too weak "
            "(need a string of at least 16 chars).")
    return tok


_TOKEN = _load_token()


class TokenAuth(BaseHTTPMiddleware):
    """Bearer-token gate on every request except the open /healthz liveness ping."""

    async def dispatch(self, request, call_next):
        if request.url.path == "/healthz":
            return JSONResponse({"ok": True})
        # Constant-time compare: a plain != leaks the token byte-by-byte via timing.
        presented = request.headers.get("authorization") or ""
        if not hmac.compare_digest(presented, f"Bearer {_TOKEN}"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


# MCP streamable-http does DNS-rebinding Host-header validation (defaults to localhost-only). It
# guards a browser the operator visits from reaching a loopback service via a rebound DNS name.
# Keep it ON for the safe loopback default; only DISABLE it when the operator explicitly binds
# non-loopback (LAN/tailnet), where it would otherwise 421 those clients and the bearer token is
# the real auth. So: protection tracks the bind, instead of being unconditionally off.
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402

mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=_IS_LOOPBACK)
if not _IS_LOOPBACK:
    logger.warning(
        "OMNISEEK_HTTP_HOST=%s binds NON-loopback: OmniSeek is reachable off-box. Ensure a "
        "firewall / reverse proxy + keep the bearer token secret (it drives credentialed tools).",
        HOST)

# STATELESS streamable-http: the session was pure liability, so remove the session.
# A stateful transport keeps per-connection state IN THIS PROCESS, so every deploy restart
# invalidates the live agent's session id and the client dies with "Session terminated" for the
# REST of its window (an agent has no reconnect verb; only the human can re-attach). That is the
# eye punishing its own improvement: each fix costs the driver the tool.
# The session bought OmniSeek nothing to begin with. Sessions exist for server-initiated traffic
# (sampling, elicitation, progress notifications, SSE resumability); every eye tool is a pure
# request -> response over PROCESS-global state (registry, caches, guards), and not one takes a
# Context or pushes to the client. So statelessness loses no capability, it only drops the handle
# that was breaking.
# Measured 2026-07-25 (real SDK client, same code, one flag): stateful call after a restart ->
# McpError "Session terminated"; stateless -> succeeds unchanged. Cost of a fresh transport per
# request: median 2.9ms -> 3.1ms on a CPU-only tool, against a ~16s broad search.
# To revert, drop this line (the SDK default is stateful).
mcp.settings.stateless_http = True

app = mcp.streamable_http_app()  # Starlette ASGI app; streamable-http MCP served at /mcp
app.add_middleware(TokenAuth)

# S2 graceful shutdown: COMPOSE the ASGI lifespan so a deploy restart (SIGTERM -> graceful uvicorn
# shutdown) DRAINS the background loops instead of SIGKILLing the recall single-writer queue mid-
# flush. app.router.lifespan_context is ALREADY FastMCP's own session lifespan; compose_lifespan
# delegates its startup + shutdown UNCHANGED (an inner `async with`) and only adds a FAIL-SAFE
# drain_all() on the shutdown side (any drain error is logged, never fatal). Confirmed shape
# (starlette 1.0.0 / mcp streamable-http): lifespan_context is a callable `app -> async context
# manager` and Starlette copies the yielded state into the lifespan scope, so compose_lifespan
# re-yields FastMCP's state. Must run after `app = ...` and before uvicorn.run (below, in main()).
from omniseek.core import lifecycle  # noqa: E402
app.router.lifespan_context = lifecycle.compose_lifespan(app.router.lifespan_context)


def _raise_fd_limit(want: int = 16384) -> None:
    """Raise THIS process's soft open-file limit toward `want`. macOS launchd hands a service a 256
    soft NOFILE by default (launchctl limit maxfiles), which a 95-source fan-out under nested
    omniseek_gather blows through with "Too many open files". The process-global egress semaphore in
    fetcher caps concurrent egress below this ceiling; raising the ceiling gives that cap real
    headroom (256 concurrent sockets plus the DB / logs / CDP / listener fds must all fit). Fail-open:
    a platform that refuses the raise keeps the old limit, and egress stays bounded, so the worst
    case is queueing, never a wrong result."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = want if hard == resource.RLIM_INFINITY else min(want, hard)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            logger.info("fd limit raised: soft %s to %s (hard %s)", soft, target, hard)
    except Exception as exc:  # noqa: BLE001: never block boot on an rlimit tweak
        logger.warning("fd limit raise skipped (%s)", exc)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from omniseek.core import _lograte
    _lograte.install_on_root()  # no single logger may flood the boot log (fresh-install OpenAlex storm)
    _raise_fd_limit()  # a 95-source fan-out under nested omniseek_gather needs headroom over macOS's 256 default
    from omniseek.core import cache
    pruned = cache.clear_expired()  # TTL only gates reads; sweep dead files each (re)start
    logger.info("cache: pruned %d expired entries", pruned)
    # Background cache warmer, tied to THIS always-on service (replaces the standalone launchd
    # cron, which was observed to fire only once — see omniseek.core.prewarm): warm the
    # query-independent slow sources on startup + every 30min so Lever B's caches stay hot.
    # P9 JUDGMENT (prewarm stays its OWN daemon, NOT folded into the jobs scheduler): the warmer is a
    # continuous WARM LOOP with two properties the calendar/interval job model would break — it warms
    # ON ENTRY (kills the cold-herd the instant the service (re)starts, before any tick would fire),
    # and it carries a refresh-margin contextvar that must run on its own thread inline. Folding it
    # into the 900s serial job tick would delay the boot warm and change its 1800s cadence, so it
    # stays a dedicated thread; the jobs scheduler owns the CALENDAR/interval self-maintenance instead.
    #
    # S2 NOTE (deferred to S3): the background services below still START here as pre-uvicorn daemon
    # threads (unchanged). S2 only makes them STOPPABLE + drains them on the ASGI-lifespan shutdown
    # (see the compose_lifespan wiring after `app = ...`). Moving these STARTS into the lifespan
    # startup is an S3 change, needed only when a service must reach the async core via the sync->
    # async PORTAL (also S3). No double-writer today: the stdio path (omniseek.server.main -> mcp.run)
    # never starts these services (they start ONLY here in serve_http.main), so only this process
    # ever writes -- the lifespan ROLES split (stdio must not start the WRITER services) is likewise
    # an S3 concern with no live exposure now.
    import threading
    from omniseek.core import prewarm
    threading.Thread(target=prewarm.warm_loop, name="cache-warmer", daemon=True).start()
    # Perception-memory index (eye.recall): make the enumerable sources STATEFUL. init() is
    # fail-open (a bad index never crashes boot); ENABLE writes for THIS process only (cron
    # processes that hit the same ingest hook leave WRITES_ENABLED False → no cross-process writer);
    # start the single serialized writer + a Path-C completeness ingest loop beside the warmer.
    try:
        from omniseek.core import recall
        from omniseek.core.recall import writer as _recall_writer
        if recall.init():
            _recall_writer.WRITES_ENABLED = True
            recall.start_writer()
            threading.Thread(target=recall.ingest_loop, name="recall-ingest", daemon=True).start()
            # Curator P2 yield tap: start its single drain daemon beside the recall writer, under
            # the SAME WRITES_ENABLED guard (idempotent). Cron/smoke processes leave WRITES_ENABLED
            # False → no drain thread runs there → no cross-process pollution of the yield statistic.
            from omniseek.core.curator import yield_tap as _yield_tap
            _yield_tap.start_writer()
            logger.info("curator yield tap: drain thread started")
            # Phase 2 (vector): preload the embedder off the hot path, then page-backfill vectors for
            # the existing corpus (interleaves with live ingest; fully fail-open if the embedder /
            # sentence-transformers is absent → the index stays lexical-only).
            threading.Thread(target=recall.embed.warm, name="recall-embed-warm", daemon=True).start()
            recall.start_backfill()
            logger.info("recall index: writes enabled + ingest loop + vector backfill started")
    except Exception as exc:  # noqa: BLE001 — the index is best-effort; never block boot
        logger.warning("recall index disabled (init failed): %s", exc)
    # In-process JOB scheduler (P9, generalizing the P6 sensor scheduler): every scheduled piece of
    # OmniSeek's self-maintenance (the sensor tick + the transplanted watchdogs / curator / audit /
    # digest) runs as a declarative row on ONE daemon loop in the process that writes memory. Start it
    # HERE (the writer process), AFTER writes are enabled above, so its WRITES_ENABLED guard passes; a
    # cron / smoke / CLI import never reaches this call site and its guard refuses anyway. Fail-open: a
    # scheduler hiccup must never block boot. (This absorbs the deleted launchd crons + the P6 loop.)
    try:
        from omniseek.core import jobs as _jobs
        if _jobs.start_scheduler() is not None:
            logger.info("job scheduler started (tick %ds, %d rows)",
                        _jobs.TICK_SECONDS, len(_jobs.registry()))
    except Exception as exc:  # noqa: BLE001 — the scheduler is best-effort; never block boot
        logger.warning("job scheduler not started (%s)", exc)
    from omniseek.server import _senses_report, _warm_heavy_imports
    _warm_heavy_imports()  # same first-import race exists under HTTP; see the helper's docstring
    logger.info(_senses_report())
    logger.info("OmniSeek HTTP service on %s:%s (token-gated; MCP at /mcp)", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
