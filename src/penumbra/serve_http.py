"""Penumbra eye as a shared HTTP MCP service (streamable-http + bearer-token auth).

ONE always-on process (launchd com.penumbra.organ.eye-http) that every agent / window connects to
over the network, instead of each Claude window spawning its own stdio-over-ssh server (N
heavy processes, a cold 86-adapter load each). Token-gated, because the eye drives
credentialed + logged-in-browser tools: it must NEVER serve open.

Run:    python -m penumbra.serve_http      (env: PENUMBRA_HTTP_HOST / PENUMBRA_HTTP_PORT)
Token:  ~/.penumbra/credentials/penumbra_http.json  ->  {"token": "..."}  (mode 600)
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

from penumbra.server import mcp

logger = logging.getLogger("penumbra.serve_http")

# Bind LOOPBACK by default: the eye drives credentialed + logged-in-browser tools, so a stranger's
# fresh deploy must NOT be reachable off-box. A deployer who wants LAN/tailnet access sets
# PENUMBRA_HTTP_HOST=0.0.0.0 explicitly (and owns putting it behind a firewall / reverse proxy).
HOST = os.environ.get("PENUMBRA_HTTP_HOST", "127.0.0.1")
_IS_LOOPBACK = HOST in ("127.0.0.1", "::1", "localhost")
PORT = int(os.environ.get("PENUMBRA_HTTP_PORT", "8765"))
_TOKEN_PATH = Path.home() / ".penumbra" / "credentials" / "penumbra_http.json"


def _load_token() -> str:
    try:
        tok = (json.loads(_TOKEN_PATH.read_text()) or {}).get("token")
    except Exception:  # noqa: BLE001
        tok = None
    if not tok:
        # fail closed — never serve the credentialed eye without a token
        raise SystemExit(f"refusing to start: no token at {_TOKEN_PATH}")
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
        "PENUMBRA_HTTP_HOST=%s binds NON-loopback: the eye is reachable off-box. Ensure a "
        "firewall / reverse proxy + keep the bearer token secret (it drives credentialed tools).",
        HOST)

app = mcp.streamable_http_app()  # Starlette ASGI app; streamable-http MCP served at /mcp
app.add_middleware(TokenAuth)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from penumbra.core import cache
    pruned = cache.clear_expired()  # TTL only gates reads; sweep dead files each (re)start
    logger.info("cache: pruned %d expired entries", pruned)
    # Background cache warmer, tied to THIS always-on service (replaces the standalone launchd
    # cron, which was observed to fire only once — see penumbra.core.prewarm): warm the
    # query-independent slow sources on startup + every 30min so Lever B's caches stay hot.
    import threading
    from penumbra.core import prewarm
    threading.Thread(target=prewarm.warm_loop, name="cache-warmer", daemon=True).start()
    # Perception-memory index (eye.recall): make the enumerable sources STATEFUL. init() is
    # fail-open (a bad index never crashes boot); ENABLE writes for THIS process only (cron
    # processes that hit the same ingest hook leave WRITES_ENABLED False → no cross-process writer);
    # start the single serialized writer + a Path-C completeness ingest loop beside the warmer.
    try:
        from penumbra.core import recall
        from penumbra.core.recall import writer as _recall_writer
        if recall.init():
            _recall_writer.WRITES_ENABLED = True
            recall.start_writer()
            threading.Thread(target=recall.ingest_loop, name="recall-ingest", daemon=True).start()
            # Curator P2 yield tap: start its single drain daemon beside the recall writer, under
            # the SAME WRITES_ENABLED guard (idempotent). Cron/smoke processes leave WRITES_ENABLED
            # False → no drain thread runs there → no cross-process pollution of the yield statistic.
            from penumbra.core.curator import yield_tap as _yield_tap
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
    # In-process sensor scheduler (P6): a sensor run is an act of PERCEPTION and must land on the
    # wall, so it executes in the ONE process that writes memory. Start it HERE (the writer process),
    # AFTER writes are enabled above, so its WRITES_ENABLED guard passes; a cron / smoke / CLI import
    # never reaches this call site and its guard refuses anyway. Fail-open: a scheduler hiccup must
    # never block boot. (This replaces the deleted launchd cron runner, which wrote no memory.)
    try:
        from penumbra.core import sensor as _sensor
        if _sensor.start_scheduler() is not None:
            logger.info("sensor scheduler started (tick 900s)")
    except Exception as exc:  # noqa: BLE001 — the scheduler is best-effort; never block boot
        logger.warning("sensor scheduler not started (%s)", exc)
    logger.info("Penumbra eye HTTP service on %s:%s (token-gated; MCP at /mcp)", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
