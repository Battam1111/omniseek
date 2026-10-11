"""Shared CDP (Chrome DevTools Protocol) helper for walled-garden adapters.

Connects to the persistent Chrome instance launched by
`scripts/launch_cdp_cn_forums.sh` (registered as a launchd service on the
host machine). All adapters that need authenticated browser sessions
(zhihu, yipinsanfendi, xiaohongshu) reuse this connection helper so
they share one browser, one set of logged-in cookies.

The persistent browser advantage:
- Cookies/localStorage persist across reboots
- No login automation needed (the operator logs in once via VNC)
- Same browser handles all platforms — clean architecture

## ⚠️ CDP-in-async fix (2026-05-28 P7)

FastMCP's `@mcp.tool()` decorator may run sync tool functions in a thread
that has an asyncio event loop attached (from anyio/asyncio internals).
Playwright's `sync_playwright()` refuses to start when an asyncio loop
exists on the current thread, raising:
    "Playwright Sync API inside asyncio loop"

**Fix**: `cdp_call(callback)` runs the entire Playwright session inside
a fresh thread (no asyncio loop) and returns the callback's result.
All CDP adapters should use `cdp_call` instead of `cdp_page`.

`cdp_page` is kept for backward compat (legacy direct-Python invocations)
but emits a deprecation warning when used from an async context.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import queue
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, nullcontext
from typing import Any, Callable, ContextManager, Iterator, Optional
from urllib.parse import urlsplit

from omniseek.core import _probe, cache, diag, upstreams

logger = logging.getLogger(__name__)

# Prefer patchright — a stealth-patched, API-identical Playwright drop-in that
# removes the Runtime.enable CDP call + console leaks (P13 anti-detection
# overhaul, 2026-05-29). Falls back to vanilla playwright if patchright is
# absent: both are pinned at 1.60.0 and the Runtime.enable leak was empirically
# absent on our Chrome 148 even with vanilla, so the fallback is safe (it just
# loses patchright's defense-in-depth, not correctness). A silent fallback would
# strip the whole walled cluster's stealth base with no signal, so warn loudly on
# import AND surface the active engine in cdp_health (see below).
try:
    from patchright.sync_api import Browser, BrowserContext, Page, sync_playwright
    _CDP_ENGINE = "patchright"
except ImportError:  # pragma: no cover
    from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright
    _CDP_ENGINE = "playwright"
    logger.debug("patchright unavailable; CDP stealth degraded to vanilla playwright")

DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DEFAULT_THREAD_TIMEOUT = 90  # seconds


# ── on-demand browser lifecycle (2026-08-29) ──────────────────────────────────────────────────
# Until now the four CDP Chromes were launched at boot by launchd and stayed up forever, because
# this module only ever CONNECTS (see cdp_page's "Don't close the browser — it's shared,
# persistent"). Measured cost of that choice on the live host: 3.1 GB of resident memory across 64
# processes, 11.5 GB of profile directories, and 248,762 lines of error log from browsers retrying
# an unreachable Google push transport. Measured benefit: about 24 calls a day, each a few seconds.
#
# The memory mattered because the machine has 16 GB of memory and its swap was 98.3% full, which
# pushed the 1.4 GB recall index onto disk and made a cold vector query take 8.8 s out of an 11 s
# search budget — starving several dozen sources into timeout. Freeing the browsers is the only
# lever that returns GBs (measured alternatives: capping local retrieval returns nothing, and the
# unified memory is soldered so it cannot be upgraded).
#
# Three facts make on-demand safe, all measured 2026-08-29 on com.omniseek.cdp.xhs-cn:
#   1. `launchctl kill TERM` does NOT get auto-restarted: KeepAlive is {Crashed, !SuccessfulExit},
#      so a deliberate stop stays stopped while a crash still self-heals.
#   2. Cold start to a usable browser takes 1 second (profile and cache are already on disk).
#   3. The login session survives: after a restart the page returned to xiaohongshu.com/explore,
#      not the login wall, and the Cookies file kept updating.
_CDP_STATE_DIR = pathlib.Path.home() / ".omniseek" / "state" / "cdp-lastuse"

# port → launchd label. A port that is NOT here (e.g. the jailed 9444 render Chromium, which is a
# colima container and not a launchd service) is left completely alone: ensure_browser returns
# immediately and behaviour is byte-identical to before.
_CDP_SERVICES = {
    "9222": "com.omniseek.cdp.cn-forums",
    "9223": "com.omniseek.cdp.xhs",
    "9224": "com.omniseek.cdp.xhs-cn",
    "9225": "com.omniseek.cdp.douyin",
}

# port → why it is sealed. A sealed port's browser is never started, kickstarted or driven by OmniSeek:
# ensure_browser (the one door every cdp_call and render goes through) refuses it. Filled at import by
# the adapter that owns the port and decides the seal (2026-10-07: xiaohongshu_cn seals 9224 from its
# _SEALED flag), so the restore point stays in that adapter; this table only records it.
_SEALED_PORTS: dict[str, str] = {}


def seal_port(port: str, reason: str) -> None:
    """Mark a CDP port as sealed: ensure_browser raises for it from now on (see _SEALED_PORTS)."""
    _SEALED_PORTS[str(port)] = reason or "sealed"


def sealed_port_reason(cdp_url: str) -> str:
    """The seal reason for the port in ``cdp_url``, or "" when that port is not sealed."""
    port = cdp_port(cdp_url)
    return _SEALED_PORTS.get(port, "") if port else ""


# port → how many connections OmniSeek may hold to that Chrome at once. The ONE place this is decided,
# read by both ways in (the persistent pool, _pool_for, and the per-call path's gate, _gate_for):
#   * 9222, the shared browser (大号 logins: zhihu, 一亩三分地, ...): 3, a few reused connections so
#     concurrent named walled fetches do not queue behind each other (the Chrome's CDP pump
#     serializes commands anyway; page loads and renders still overlap);
#   * 9223 / 9224, the two xiaohongshu 小号, and 9225, the douyin 小号: 1, strictly one flow at a
#     time (anti-ban: an account that must not look automated).
# A port that is not here (the jailed 9444 render Chromium, a new browser nobody listed) gets 1: one
# at a time is the safe side to be wrong on. tests/test_cdp_ondemand.py fails when a port in
# _CDP_SERVICES has no row here, so a new browser's size is a decision, not a default.
_CDP_MAX_CONNECTIONS = {
    "9222": 3,
    "9223": 1,
    "9224": 1,
    "9225": 1,
}
_CDP_DEFAULT_CONNECTIONS = 1

_PORT_RE = re.compile(r":(\d+)")
_START_LOCK = threading.Lock()
_START_TIMEOUT_S = 20  # cold start measured at 1s; 20 is a generous ceiling, not an expectation


def cdp_port(cdp_url: str) -> Optional[str]:
    m = _PORT_RE.search(cdp_url or "")
    return m.group(1) if m else None


def cdp_service_for(cdp_url: str) -> Optional[str]:
    port = cdp_port(cdp_url)
    return _CDP_SERVICES.get(port) if port else None


def max_connections(cdp_url: str) -> int:
    """How many connections OmniSeek may hold to the Chrome at ``cdp_url`` at once (the port's row of
    _CDP_MAX_CONNECTIONS; 1 for a port with no row or a url without a port)."""
    port = cdp_port(cdp_url)
    return _CDP_MAX_CONNECTIONS.get(port, _CDP_DEFAULT_CONNECTIONS) if port else _CDP_DEFAULT_CONNECTIONS


def touch_last_use(cdp_url: str) -> None:
    """Stamp 'this browser was used just now' on DISK.

    On disk rather than in a module global because the reaper that stops idle browsers runs as a
    process-isolated job (its own interpreter, see jobs._run_isolated_with_budget), so it cannot
    see any in-process counter. A failure to stamp must never break the call that is about to
    happen, hence the swallow: the worst case is the reaper stopping a browser one cycle early,
    which costs a 1 second restart."""
    port = cdp_port(cdp_url)
    if not port:
        return
    try:
        _CDP_STATE_DIR.mkdir(parents=True, exist_ok=True)
        (_CDP_STATE_DIR / port).write_text(str(time.time()), encoding="utf-8")
    except OSError:  # noqa: BLE001 — telemetry, never load-bearing
        pass


def read_last_use(port: str) -> Optional[float]:
    """Epoch seconds of the last recorded use of this port, or None if never recorded."""
    try:
        return float((_CDP_STATE_DIR / port).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def ensure_browser(cdp_url: str) -> None:
    """Make sure the CDP Chrome for this url is running, starting it if it is not.

    No-ops on any non-macOS host and on any port outside _CDP_SERVICES, so the only environment
    whose behaviour changes is the deployment's four launchd-managed browsers.

    Raises RuntimeError if the browser cannot be brought up: the caller (cdp_call) already
    propagates exceptions and every walled adapter degrades a raise to an empty result, so a
    failed start surfaces as 'this source returned nothing' plus a logged reason, exactly like a
    dead browser did before this existed.

    A sealed port (_SEALED_PORTS) raises at once, on every platform, before any health probe or
    kickstart: nothing is started and nothing is sent to that browser."""
    sealed = sealed_port_reason(cdp_url)
    if sealed:
        raise RuntimeError(f"CDP port {cdp_port(cdp_url)} sealed: {sealed}")
    if sys.platform != "darwin":
        return
    label = cdp_service_for(cdp_url)
    if label is None:
        return
    if cdp_health(cdp_url)[0]:
        touch_last_use(cdp_url)
        return
    with _START_LOCK:
        # Double-check: a concurrent caller may have started it while we waited for the lock.
        if cdp_health(cdp_url)[0]:
            touch_last_use(cdp_url)
            return
        logger.info("CDP browser %s is down; starting on demand", label)
        try:
            subprocess.run(
                ["launchctl", "kickstart", f"gui/{os.getuid()}/{label}"],
                check=False, timeout=10,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"could not launch CDP browser {label}: {exc}") from exc
        deadline = time.time() + _START_TIMEOUT_S
        while time.time() < deadline:
            if cdp_health(cdp_url)[0]:
                touch_last_use(cdp_url)
                logger.info("CDP browser %s ready", label)
                return
            time.sleep(0.3)
    raise RuntimeError(f"CDP browser {label} did not become ready within {_START_TIMEOUT_S}s")


def close_browser(cdp_url: str, timeout: float = 5.0) -> bool:
    """Ask the Chrome at ``cdp_url`` to shut down the normal way, with the CDP command Browser.close.

    Why not a signal (measured 2026-10-05 on the live host, with a throwaway Chrome run exactly like the
    four services): at startup Chrome clones its own app bundle into a temp directory, and only the
    end of a normal shutdown starts the helper that deletes the clone (see omniseek.core.chrome_clones).
    ``launchctl kill TERM`` left the clone behind 12 times out of 12 (no cleanup helper ever
    started), and still 10 of 10 with AbandonProcessGroup set; after Browser.close the helper ran
    and the clone was gone with the browser 13 times out of 13 (3 of them through the reaper's own
    stop path). Returns True when Chrome took the command (its reply arrived, or it dropped the
    connection while closing); False when nothing answers on the port or the command could not be
    sent. Standard library only: one WebSocket handshake and one text frame."""
    import base64
    import json
    import socket

    ws_url = _browser_instance(cdp_url)
    if not ws_url:
        return False
    parts = urlsplit(ws_url)
    if parts.scheme != "ws" or not parts.hostname or not parts.port:
        return False
    payload = json.dumps({"id": 1, "method": "Browser.close"}).encode()
    mask = os.urandom(4)
    frame = (bytes([0x81, 0x80 | len(payload)]) + mask
             + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
    try:
        with socket.create_connection((parts.hostname, parts.port), timeout=timeout) as s:
            key = base64.b64encode(os.urandom(16)).decode()
            s.sendall((f"GET {parts.path} HTTP/1.1\r\nHost: {parts.hostname}:{parts.port}\r\n"
                       "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 65536:
                chunk = s.recv(4096)
                if not chunk:
                    return False
                head += chunk
            status = head.split(b"\r\n", 1)[0].split(b" ")
            if len(status) < 2 or status[1] != b"101":
                logger.info("Browser.close on %s: handshake refused (%r)", cdp_url, head[:80])
                return False
            s.sendall(frame)
            try:
                s.recv(4096)   # the reply, or b"" when Chrome closes the socket first
            except OSError:
                pass
        return True
    except OSError as exc:
        logger.info("Browser.close on %s failed: %s", cdp_url, exc)
        return False


class CacheOnlyMiss(Exception):
    """Raised by cdp_call when cache-only mode (cache_only=True) is active: a cache miss must NOT
    drive the browser. Every cdp_call caller already degrades an exception to [] (the adapter
    contract), so this short-circuits all CDP sources at the single egress with zero per-source
    edits and zero account traffic from a poll."""

# Live cdp_call count. Each worker holds exactly one tab, and active tabs are always the
# most-recently-opened, so the tab-sweep keeps the newest ``in-flight + margin`` tabs — that
# way it can NEVER reap a tab an in-flight call is using, even under dozens of concurrent
# name-called walled fetches, while still reaping tabs LEAKED by prior timed-out workers (a
# leaked-but-still-blocked worker stays counted until it truly dies, then its stale tab ages
# out). A timed-out worker is a daemon → still running (still counted) until its blocked op
# returns, so its tab is protected exactly while the worker lives, reaped once it's gone.
_inflight_cdp = 0
_inflight_lock = threading.Lock()


# Per-Chrome gate on the per-call path. The default cdp_call path spawns a fresh thread PER call, so
# without a bound two named walled fetches to the SAME Chrome run truly concurrently: two tabs, two
# same-site searches on one shared browser → the site's flood-control throttles one and OmniSeek
# SILENTLY caches the empty as success (the gap-③ false-empty; proven 2026-06-22: two parallel
# 一亩三分地 fetches false-emptied one, while a serial fetch returned 35). The gate admits at most the
# port's row of _CDP_MAX_CONNECTIONS (the same table the pool reads, so both paths hold the same
# number of connections; until 2026-10-04 this gate was 1 for every port, while the pool gave 9222
# three). Sites that must not see two flows at once keep their own serialization on top
# (yipinsanfendi's _run chokepoint). Keyed by cdp_url so different Chromes never block each other.
_cdp_gates: dict[str, "threading.Semaphore"] = {}
_cdp_gates_lock = threading.Lock()


def _gate_for(cdp_url: str) -> "threading.Semaphore":
    g = _cdp_gates.get(cdp_url)
    if g is None:
        with _cdp_gates_lock:
            g = _cdp_gates.get(cdp_url)
            if g is None:
                g = threading.Semaphore(max_connections(cdp_url))
                _cdp_gates[cdp_url] = g
    return g


def _sweep_excess_tabs(ctx, keep_recent: int = 6) -> None:
    """Close the OLDEST excess tabs that prior timed-out calls leaked — WITHOUT ever reaping a
    tab an in-flight call is actively using.

    When ``cdp_call`` hits its thread join-timeout it abandons the worker, whose tab stays
    open until its blocked Playwright op finally times out (usually seconds — but pathological
    timing can let tabs accumulate in the shared Chrome → memory bloat → the 'silent death'
    the watchdog otherwise has to relaunch). We keep the newest ``max(keep_recent, in-flight+2)``
    tabs: active tabs are always the most-recent, so this protects EVERY concurrent live call
    and still reaps clear leaks. (The old fixed keep_recent=6 closed the oldest-beyond-6 by
    creation order even when >6 calls ran at once → it could close an in-flight tab →
    TargetClosedError → 3 consecutive CDP errors → a FALSE 6h backoff on a servable source;
    'dozens of parallel walled' would have made that the norm.)"""
    try:
        pages = ctx.pages
    except Exception:  # noqa: BLE001
        return
    with _inflight_lock:
        active = _inflight_cdp
    keep = max(keep_recent, active + 2)  # +2: tabs created in the window between count and sweep
    if len(pages) <= keep:
        return
    for p in pages[:len(pages) - keep]:  # ctx.pages is creation order → oldest first
        try:
            p.close()
        except Exception:  # noqa: BLE001
            pass


# ── Lever A: persistent CDP connection pool (opt-in via OMNISEEK_CDP_POOL=1) ──────────────────
# The default cdp_call() spawns a fresh thread + sync_playwright() + connect_over_cdp() PER call
# (~1.5-3s of pure driver-startup + CDP-handshake waste, since the browser itself is persistent
# but the connection is rebuilt every time). This pool keeps N persistent worker threads per
# Chrome, each holding ONE long-lived sync_playwright + connection for the process lifetime, and
# runs each callback on a fresh page inside its owning worker thread — which satisfies Playwright
# sync's thread-affinity (a connection is only ever touched by the one thread that created it).
# Its size per Chrome is the port's row of _CDP_MAX_CONNECTIONS (9222 shared 大号: 3; the 小号 on
# 9223 / 9224 / 9225: 1, strictly serial, the pool never widens them; any other port: 1).
# Self-heals after a Chrome restart (the reaper stopped it and ensure_browser started a new one, or
# the sentinel / launchd restarted it): before reusing its connection a worker checks that the
# Chrome on the port is still the one it connected to (_browser_instance) and reconnects if not, so
# the first task after a restart already runs on the new browser; only a task in flight at the
# moment Chrome dies fails, exactly as the old per-call path would. The
# flag defaults OFF → behavior is byte-identical to before unless explicitly enabled, so it ships
# inert and is reversible by unsetting the env var.
_POOL_ENV = "OMNISEEK_CDP_POOL"


def _pool_enabled() -> bool:
    return os.environ.get(_POOL_ENV) == "1"


_pools: dict[str, "_CdpPool"] = {}
_pools_lock = threading.Lock()


def _pool_for(cdp_url: str) -> "_CdpPool":
    p = _pools.get(cdp_url)
    if p is None:
        with _pools_lock:
            p = _pools.get(cdp_url)
            if p is None:
                p = _CdpPool(cdp_url, max_connections(cdp_url))
                _pools[cdp_url] = p
    return p


def _browser_instance(cdp_url: str) -> Optional[str]:
    """Which Chrome process is serving this CDP url right now, or None if nothing answers.

    Chrome mints a new browser id at every launch and publishes it as /json/version's
    webSocketDebuggerUrl, so two reads compare equal only while the same process is up. This is the
    pool's reuse test. Browser.is_connected() cannot be: Playwright's sync API updates that flag
    only while a call is running on the connection's thread, so a worker parked in q.get() keeps
    reading True for a Chrome that exited while it waited, and its next task used to fail with
    TargetClosedError before the one after it reconnected."""
    import httpx

    try:
        resp = httpx.get(f"{cdp_url}/json/version", timeout=3)
        if resp.status_code == 200:
            return resp.json().get("webSocketDebuggerUrl") or None
    except Exception:  # noqa: BLE001: no answer means there is no browser to match
        pass
    return None


def _note_failure(url: Optional[str], exc: BaseException) -> None:
    """The diagnostic of a failed call. A host gate that did not admit the render when its turn came
    (``on_turn`` raised; the turn was given up at once, review P6) reads as what it is."""
    if isinstance(exc, upstreams.UpstreamBusy):
        diag.note("cdp_call", url=url, body=f"declared upstream gate, request not sent: {exc}")
    else:
        diag.note("cdp_call", url=url, exc=exc)


# ── what the page's own requests got, for a running health check (2026-10-04) ──────────────────
# A health check whose page load or in-page fetch got HTTP 429 has not verified anything (see
# omniseek.core._probe): the site answered, but the answer says nothing about whether the path serves.
# The browser's answers never reached OmniSeek, so such a check could only say "down". Now, while a
# health check's ledger is open in the calling thread (and only then), the page cdp_call opens gets one
# listener for its responses: the status of every MAIN-FRAME DOCUMENT (the navigation to initial_url
# and any the callback makes) and every 429 of a FETCH / XHR (an in-page fetch such as douban's rexxar
# call; other fetch / XHR answers are the site's own background traffic, not the check's). Back in the
# calling thread they go into the ledger, so a 429 reads "not verified". Only a listener is added:
# nothing about the navigation, the waits, the concurrency, the connections or the logins changes, and
# no request is added. Outside a health check nothing is attached at all.
#
# One thing a health check gets back differs: when the page's LAST main-frame document answered 429,
# cdp_call raises RateLimitedPage instead of handing back what the callback read off the error page.
# A check that only reads the page (its URL, its title, its HTML) would otherwise call a 429 page
# working (zhihu read the URL, ml_conferences / mpnp_draws the HTML), and True is never re-read.
_watched: dict[int, list] = {}
_watched_lock = threading.Lock()


class RateLimitedPage(RuntimeError):
    """A health check's page: its main document answered HTTP 429 (the message is ``_probe``'s
    wording). Raised by cdp_call only while a health check's ledger is open."""


def _watch(page, seen: Optional[list]) -> None:
    """Attach the response listener to a fresh page (a no-op when ``seen`` is None: no health check)."""
    if seen is None:
        return
    try:
        main = page.main_frame

        def on_response(resp) -> None:
            try:
                kind, status = resp.request.resource_type, resp.status
                if kind == "document":
                    if resp.frame != main:
                        return
                elif kind not in ("fetch", "xhr") or status != 429:
                    return
                seen.append((status, resp.url, _probe.retry_after_s(resp.headers), kind == "document"))
            except Exception:  # noqa: BLE001 (recording must never break a page)
                pass
        page.on("response", on_response)
        with _watched_lock:
            _watched[id(page)] = seen
    except Exception:  # noqa: BLE001
        pass


def _unwatch(page) -> None:
    with _watched_lock:
        _watched.pop(id(page), None)


def _record_seen(seen: Optional[list]) -> None:
    """Into the calling thread's health-check ledger (``_probe``) with what the page's requests got."""
    for status, url, retry_after, _doc in list(seen or ()):
        _probe.note_response(status, where=urlsplit(url).hostname or url, retry_after=retry_after)


def _refuse_rate_limited_page(seen: Optional[list]) -> None:
    """Raise RateLimitedPage when the page's last main-frame document answered 429 (see above)."""
    docs = [row for row in list(seen or ()) if row[3]]
    if docs and docs[-1][0] == 429:
        _status, url, retry_after, _doc = docs[-1]
        raise RateLimitedPage(_probe.rate_limited(urlsplit(url).hostname or url, retry_after))


class _CdpPool:
    """A pool of persistent worker threads for ONE Chrome (see block comment)."""

    def __init__(self, cdp_url: str, size: int) -> None:
        self.cdp_url = cdp_url
        self.size = size
        self._lock = threading.Lock()
        self._q: "queue.Queue" = queue.Queue()
        self._start_workers()

    def _start_workers(self) -> None:
        """Spawn `size` fresh worker threads bound to the CURRENT queue (at init, and again on
        recovery). A fresh worker makes a fresh sync_playwright + connect on its first task."""
        q = self._q
        for i in range(self.size):
            threading.Thread(target=self._worker, args=(q,),
                             name=f"cdp-pool-{self.cdp_url}-{i}", daemon=True).start()

    def submit(self, callback: Callable, initial_url: Optional[str], timeout: int,
               queue_until: Optional[float] = None,
               on_turn: Optional[Callable[[], ContextManager]] = None,
               seen: Optional[list] = None) -> Any:
        with self._lock:
            q = self._q  # snapshot: a concurrent _recover swap must not split put/get across queues
        reply: "queue.Queue" = queue.Queue(maxsize=1)
        q.put((callback, initial_url, reply, queue_until, on_turn, seen))
        try:
            status, payload = reply.get(timeout=timeout)
        except queue.Empty:
            # The worker did not answer in time. On a serial (size-1) pool this usually means it
            # WEDGED mid-op on a half-dead persistent connection: the socket still reports connected,
            # but the CDP command pump stalled, so a no-timeout new_page()/contexts hangs forever. It
            # cannot recover itself (it never returns to the loop top where is_connected() is checked),
            # and every later submit would queue behind it, so the whole source jams until eye-http
            # restarts. Retire the wedged worker + start a fresh one so the NEXT call reconnects.
            self._recover(q)
            to = TimeoutError(f"CDP call exceeded {timeout}s (pool {self.cdp_url})")
            diag.note("cdp_call", url=initial_url, exc=to)
            raise to
        if status == "err":
            _note_failure(initial_url, payload)
            raise payload
        return payload

    def _recover(self, stale_q: "queue.Queue") -> None:
        """A submit timed out: swap in a FRESH queue + fresh workers, once per wedge. The retired
        daemon stays parked on `stale_q` (nothing feeds it now, so it is harmless); it never touches
        the browser again unless it unwedges and finishes its abandoned op, whose leaked tab
        _sweep_excess_tabs reaps. Size is unchanged, so the serial anti-ban invariant holds."""
        with self._lock:
            if self._q is not stale_q:
                return  # another timeout already recovered this pool
            self._q = queue.Queue()
            self._start_workers()

    def _worker(self, q: "queue.Queue") -> None:
        global _inflight_cdp
        pw = None
        browser: Optional[Browser] = None
        instance: Optional[str] = None  # the Chrome `browser` was connected to (_browser_instance)

        def _connect() -> None:
            nonlocal pw, browser, instance
            # Drop the old handle first: if connecting fails below, the next task must reconnect
            # instead of reusing it (after pw.stop() its is_connected() can still read True).
            browser = None
            # Read the id BEFORE connecting: if Chrome is replaced in between, the id kept is the
            # older one, so the next task sees a mismatch and reconnects once more (harmless). The
            # other order could pair a connection to the dead browser with the new id.
            instance = _browser_instance(self.cdp_url)
            pw = sync_playwright().start()
            browser = pw.chromium.connect_over_cdp(self.cdp_url, timeout=10000)

        while True:
            callback, initial_url, reply, queue_until, on_turn, seen = q.get()
            if queue_until is not None and time.monotonic() > queue_until:
                # Its turn came after the caller's budget ran out: do not load the page at all.
                reply.put(("err", TimeoutError("CDP queue: the caller's time ran out before its turn; "
                                               "not rendered")))
                continue
            try:
                # (Re)connect if this is the first task, the connection is known dead, or the Chrome
                # on the port is no longer the one we connected to (restarted since the last task).
                # The last test is the one a restart needs: is_connected() still reads True here.
                if (browser is None or not browser.is_connected()
                        or _browser_instance(self.cdp_url) != instance):
                    try:
                        if pw is not None:
                            pw.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    _connect()
                contexts = browser.contexts
                if not contexts:
                    raise RuntimeError("No browser context in CDP Chrome — freshly launched + empty?")
                ctx = contexts[0]
                with _inflight_lock:
                    _inflight_cdp += 1
                try:
                    _sweep_excess_tabs(ctx)
                    page = ctx.new_page()
                    _watch(page, seen)   # a health check's ledger only (see _watch)
                    try:
                        # the host's gates only now, for the page load itself (review F9), tried
                        # once: refused, this worker drops the task and takes the next (review P6);
                        # leased, so a load stuck here gives the host back in time (review P1)
                        with (on_turn() if on_turn is not None else nullcontext()):
                            if initial_url:
                                page.goto(initial_url, wait_until="domcontentloaded", timeout=30000)
                            reply.put(("ok", callback(page)))
                    finally:
                        _unwatch(page)
                        try:
                            page.close()
                        except Exception:  # noqa: BLE001
                            pass
                finally:
                    with _inflight_lock:
                        _inflight_cdp -= 1
            except Exception as exc:  # noqa: BLE001 — report to the caller, keep the worker alive
                reply.put(("err", exc))
                try:  # if the connection died, drop it so the NEXT task reconnects
                    if browser is None or not browser.is_connected():
                        browser = None
                except Exception:  # noqa: BLE001
                    browser = None


def cdp_call(callback: Callable[[Page], Any], *,
             initial_url: Optional[str] = None,
             timeout: int = DEFAULT_THREAD_TIMEOUT,
             cdp_url: str = DEFAULT_CDP_URL,
             queue_until: Optional[float] = None,
             on_turn: Optional[Callable[[], ContextManager]] = None) -> Any:
    """Run a CDP-driven callback in a fresh thread (no asyncio loop).

    Use this from any adapter `search` / `fetch_url` / `health_check`
    method. The callback receives a fresh Page (already at `initial_url`
    if provided) and should return the desired result (HTML string, dict,
    etc.). Exceptions in the callback are propagated to the caller.

    Threading isolation: each call spawns a new daemon thread that opens
    sync_playwright, connects to CDP, opens a tab, runs the callback,
    closes the tab, and tears down playwright. This avoids the
    "Sync API inside asyncio loop" error that would otherwise occur when
    called from FastMCP's async tool dispatch.

    Args:
        callback: function `(page) -> Any`. Runs inside the worker thread.
        initial_url: optional URL to `page.goto(...)` before invoking callback.
        timeout: max seconds for the entire operation (default 90).
        queue_until: optional monotonic time: the latest this call may wait for its turn at this
            Chrome (the caller's budget); past it, TimeoutError and nothing is loaded.
        on_turn: optional context-manager factory entered only once the call HAS its turn, around the
            page load (the host's declared gates: a render must not hold a host gate while it queues
            behind other browser calls, review F9).

    Returns:
        Whatever the callback returns.

    Raises:
        TimeoutError: if the operation exceeds timeout.
        CacheOnlyMiss: in cache-only mode (cache_only=True): a cache miss must not drive the browser.
        Any exception raised by the callback or Playwright setup.
    """
    if cache.cache_only():
        # cache-only mode (cache_only=True): never drive the browser on a miss. Checked in the
        # CALLING thread (where fetcher set the flag), before the pool/worker thread is touched.
        miss = CacheOnlyMiss("cache-only mode: live CDP suppressed")
        diag.note("cdp_call", url=initial_url, exc=miss)
        raise miss
    # On-demand lifecycle: bring the browser up if the idle reaper stopped it (1s cold start,
    # measured). Deliberately AFTER the cache-only gate — a cache-only poll must not start a
    # browser — and BEFORE the pool branch, so both the pooled and per-call paths are covered.
    ensure_browser(cdp_url)
    # what the page's requests got, kept only while a health check's ledger is open (see _watch)
    seen: Optional[list] = [] if _probe.active() else None
    if _pool_enabled():  # Lever A: route to the persistent connection pool (else per-call below)
        try:
            value = _pool_for(cdp_url).submit(callback, initial_url, timeout, queue_until, on_turn, seen)
        finally:
            _record_seen(seen)
        _refuse_rate_limited_page(seen)
        return value

    result_queue: queue.Queue = queue.Queue(maxsize=1)

    def worker() -> None:
        global _inflight_cdp
        with _inflight_lock:
            _inflight_cdp += 1  # counted BEFORE the tab exists, so a concurrent sweep keeps room for it
        try:
            with sync_playwright() as pw:
                browser: Browser = pw.chromium.connect_over_cdp(cdp_url, timeout=10000)
                contexts = browser.contexts
                if not contexts:
                    raise RuntimeError("No browser context in CDP Chrome — is it freshly launched and empty?")
                ctx: BrowserContext = contexts[0]
                _sweep_excess_tabs(ctx)  # reap tabs leaked by prior timed-out calls (never active ones)
                page = ctx.new_page()
                _watch(page, seen)   # a health check's ledger only (see _watch)
                try:
                    if initial_url:
                        page.goto(initial_url, wait_until="domcontentloaded", timeout=30000)
                    value = callback(page)
                    result_queue.put(("ok", value))
                finally:
                    _unwatch(page)
                    try:
                        page.close()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            result_queue.put(("err", exc))
        finally:
            with _inflight_lock:
                _inflight_cdp -= 1

    # Serialize concurrent calls to the SAME Chrome (see _gate_for): hold the gate across the
    # worker's run so two named walled fetches to one browser QUEUE instead of contending (the
    # gap-③ false-empty). Released on the timeout raise too → one stuck call can't block every
    # walled fetch forever (its leaked tab is reaped by _sweep_excess_tabs on the next call).
    gate = _gate_for(cdp_url)
    if queue_until is None:
        gate.acquire()
    else:
        left = queue_until - time.monotonic()
        if left <= 0 or not gate.acquire(timeout=left):
            to = TimeoutError("CDP queue: no turn at this Chrome within the caller's budget; not rendered")
            diag.note("cdp_call", url=initial_url, exc=to)
            raise to
    try:
        # ``on_turn`` tries the host's gates once (upstreams.browser_turn); when it refuses, the
        # finally below gives the Chrome turn up at once and nothing is loaded (review P6)
        with (on_turn() if on_turn is not None else nullcontext()):
            t = threading.Thread(target=worker, daemon=True)
            t.start()
            t.join(timeout=timeout)
            if t.is_alive():
                to = TimeoutError(f"CDP call exceeded {timeout}s")
                diag.note("cdp_call", url=initial_url, exc=to)
                raise to
            status, payload = result_queue.get_nowait()
    except upstreams.UpstreamBusy as exc:
        _note_failure(initial_url, exc)
        raise
    finally:
        gate.release()
        _record_seen(seen)
    if status == "err":
        # The CDP egress failed (TargetClosedError, a goto timeout, a dead CDP connection, a
        # selector raise inside the callback). Surface it so the fixing agent sees the wall.
        _note_failure(initial_url, payload)
        raise payload
    _refuse_rate_limited_page(seen)
    return payload


def cdp_render(callback: Callable[[Page], Any], *,
               initial_url: str,
               timeout: int = DEFAULT_THREAD_TIMEOUT,
               cdp_url: str = DEFAULT_CDP_URL,
               wait_cloudflare: bool = True) -> Any:
    """Render an ATTACKER-INFLUENCEABLE url in a FRESH, EPHEMERAL incognito context, then run
    ``callback(page)`` on the settled page. The wall-aware probe (P2) lane.

    Unlike ``cdp_call`` (which reuses the persistent logged-in DEFAULT context of the credentialed
    cluster), this opens a NEW context per call and tears it DOWN after, so a hostile candidate page
    leaves no cookie / localStorage bleed into the next probe (the red-team's per-probe ephemerality).
    It targets the JAILED Chromium (a network-isolated colima container whose only egress is the
    SSRF-pin proxy, reached via the socat CDP bridge, e.g. cdp_url=http://127.0.0.1:9444), NEVER the
    credentialed cluster. Same fresh-thread pattern as cdp_call (no asyncio loop on the worker
    thread) + the per-cdp_url serialization gate; honors cache.cache_only(). Returns the callback's
    result; raises TimeoutError / the CDP exception on failure (the caller degrades to a blocked
    fetch)."""
    if cache.cache_only():
        miss = CacheOnlyMiss("cache-only mode: live CDP render suppressed")
        diag.note("cdp_render", url=initial_url, exc=miss)
        raise miss

    result_queue: queue.Queue = queue.Queue(maxsize=1)

    def worker() -> None:
        global _inflight_cdp
        with _inflight_lock:
            _inflight_cdp += 1
        try:
            with sync_playwright() as pw:
                browser: Browser = pw.chromium.connect_over_cdp(cdp_url, timeout=10000)
                ctx: BrowserContext = browser.new_context()  # FRESH incognito; torn down below
                try:
                    page = ctx.new_page()
                    page.goto(initial_url, wait_until="domcontentloaded", timeout=30000)
                    if wait_cloudflare:
                        wait_through_cloudflare(page)
                    result_queue.put(("ok", callback(page)))
                finally:
                    try:
                        ctx.close()  # drops the page + ALL cookies/storage for THIS probe
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            result_queue.put(("err", exc))
        finally:
            with _inflight_lock:
                _inflight_cdp -= 1

    with _gate_for(cdp_url):
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            to = TimeoutError(f"CDP render exceeded {timeout}s")
            diag.note("cdp_render", url=initial_url, exc=to)
            raise to
        status, payload = result_queue.get_nowait()
    if status == "err":
        diag.note("cdp_render", url=initial_url, exc=payload)
        raise payload
    return payload


@contextmanager
def cdp_page(initial_url: Optional[str] = None) -> Iterator[Page]:
    """Yield a Playwright Page connected to the persistent Chrome.

    Opens a fresh tab in the existing browser, navigates to `initial_url`
    if provided, and closes the tab when done (leaving other tabs alone).

    Usage:
        with cdp_page("https://www.zhihu.com/search?q=foo") as page:
            page.wait_for_load_state("networkidle")
            html = page.content()
    """
    with sync_playwright() as pw:
        try:
            browser: Browser = pw.chromium.connect_over_cdp(DEFAULT_CDP_URL, timeout=10000)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to connect to CDP at %s: %s", DEFAULT_CDP_URL, exc)
            raise

        # Use the existing default context (which has the user's login state)
        contexts = browser.contexts
        if not contexts:
            raise RuntimeError("No browser context in CDP Chrome — is it freshly launched and empty?")
        ctx: BrowserContext = contexts[0]

        page = ctx.new_page()
        try:
            if initial_url:
                page.goto(initial_url, wait_until="domcontentloaded", timeout=30000)
            yield page
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
            # Don't close the browser — it's shared, persistent


def wait_through_cloudflare(page, timeout: float = 20.0) -> bool:
    """Wait out a Cloudflare 'Just a moment' interstitial before reading the page.

    The persistent real Chrome (real profile + fingerprint) solves CF's non-interactive JS
    challenge on its OWN — measured ~2s live (verified 2026-06-11 on 1point3acres). The
    only bug was reading title/content BEFORE it cleared (the cause of the false 'CF-walled,
    needs VNC' verdict). This polls cheaply until the CF markers are gone.

    Returns True once past CF (or never on it); False if it never clears within `timeout` — that
    would be an INTERACTIVE captcha (Turnstile) that genuinely needs a human in VNC, which the
    caller can then report honestly instead of silently returning a challenge page."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            title = page.title() or ""
            url = page.url or ""
        except Exception:  # noqa: BLE001 — page mid-navigation; retry
            time.sleep(0.6)
            continue
        on_cf = ("Just a moment" in title or "Attention Required" in title
                 or "Checking your browser" in title or "__cf_chl" in url)
        if not on_cf:
            return True
        try:
            page.wait_for_timeout(700)
        except Exception:  # noqa: BLE001
            time.sleep(0.7)
    return False


def cdp_health(cdp_url: str = DEFAULT_CDP_URL, *, ensure: bool = False) -> tuple[bool, str]:
    """Quick connectivity check to the CDP Chrome (defaults to the shared 9222).

    ``ensure`` picks WHICH question is being asked, and the two callers want opposite answers:

    * ``ensure=False`` (default) asks "is this browser running RIGHT NOW". The infra doctor and the
      keepalive need exactly that, and must NOT start anything: a probe that starts browsers would
      fight the reaper, which stops idle ones every 10 minutes by design.
    * ``ensure=True`` asks "would a real call reach this browser", which is what a SOURCE health
      check reports on. ``cdp_call`` starts the browser on demand (~1s), so without this a source
      whose browser is merely idle reads as DOWN: an unobserved state reported as a negative. That
      false verdict is what kept douyin at a 53-run failure streak while its searches worked fine.
    """
    import httpx

    if ensure:
        try:
            ensure_browser(cdp_url)
        except Exception as exc:  # noqa: BLE001: a failed start IS the health answer, not a crash
            return False, f"browser not running and could not be started: {type(exc).__name__}: {exc}"
    try:
        resp = httpx.get(f"{cdp_url}/json/version", timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            return True, f"OK ({data.get('Browser', 'Chrome')} via {_CDP_ENGINE})"
        return False, f"HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def is_logged_in_zhihu(page: Page) -> bool:
    """Heuristic: are we logged into 知乎 in this page's context?"""
    try:
        # Logged-in users have either: an avatar/notification element, or
        # they get redirected to /signin if not authed.
        # Cheap probe: check current URL doesn't include /signin
        page.goto("https://www.zhihu.com/", wait_until="domcontentloaded", timeout=15000)
        url = page.url
        if "/signin" in url or "/login" in url:
            return False
        # More positive check: presence of user-area DOM
        return page.locator("[data-za-detail-view-element_name='Avatar']").count() > 0 or \
               page.locator(".AppHeader-userInfo").count() > 0
    except Exception:  # noqa: BLE001
        return False


# ── "substance is in the images" surfacing ──────────────────────────────────────
# Many social/forum sources (xiaohongshu carousels, zhihu answer screenshots, Discuz
# attachments, Quora/X/脉脉 image posts) hide the real content in IMAGES, not the text.
# These helpers let any CDP source surface the content-image URLs (doc.media) so the
# consuming agent can VIEW them with its own vision. OmniSeek does NOT OCR (free CJK OCR
# is poor on stylized images; the agent's vision is better + free) — it just surfaces.
_CONTENT_IMAGES_JS = (
    "()=>{const seen=new Set(),out=[];"
    "const chrome=el=>!!(el.closest&&el.closest('nav,header,footer,aside'));"
    "for(const i of document.querySelectorAll('img')){"
    "const s=i.currentSrc||i.src||'';if(!s)continue;"
    "const l=s.toLowerCase();"
    "if(l.startsWith('data:')||l.includes('avatar')||l.includes('/emoji')||l.includes('sprite')||l.includes('/icon'))continue;"
    "if(chrome(i))continue;"
    "if(i.naturalWidth>=300&&i.naturalHeight>=300){"
    "const b=s.split('?')[0];if(!seen.has(b)){seen.add(b);out.push(s);}}}"
    "return out.slice(0,15);}"
)


def images_from_page(page) -> list:
    """Content-image URLs from a rendered page (best-effort): large (>=300px each side), not
    avatar/icon/sprite/data-uri, not inside nav/header/footer/aside chrome, deduped. Returns
    [] on any failure. Pair with content_with_media() so the agent knows to view them."""
    try:
        return page.evaluate(_CONTENT_IMAGES_JS) or []
    except Exception:  # noqa: BLE001
        return []


def content_with_media(text: str, images: list) -> str:
    """If the page text is thin but content images exist, append a hint pointing the agent to
    the media field (image URLs) so it views them. Returns the (possibly-augmented) text."""
    text = (text or "").strip()
    if images and len(text) < 200:
        hint = (f"[正文/干货可能在 {len(images)} 张图里:图片 URL 见 media 字段,"
                f"下载后用视觉读图(eye 不做 OCR)]")
        return (text + "\n\n" + hint) if text else hint
    return text


# ── video notes ───────────────────────────────────────────────────────────────────────────────
# A video note's substance is SPOKEN. OmniSeek does not transcribe here; it hands the agent a URL
# omniseek_transcribe can fetch, which means the URL has to be a real one.
_VIDEO_SRC_JS = (
    "()=>{"
    "const v=document.querySelector('video');"
    "if(!v)return '';"
    "const s=v.currentSrc||v.src||'';"
    "if(s&&s.indexOf('blob:')!==0)return s;"
    "const el=v.querySelector('source');"
    "const t=(el&&el.src)||'';"
    "return t.indexOf('blob:')===0?'':t;}"
)

# Direct media on the wire. Deliberately NOT anchored to one CDN host: the point is the response
# being a playable stream, and a host allowlist would go stale the first time the CDN is renamed.
_VIDEO_WIRE_RE = re.compile(r"https?://[^\s\"'<>]+?\.(?:mp4|m3u8)(?:\?[^\s\"'<>]*)?", re.I)

# Player markers. Their PRESENCE is content: a video note can legitimately carry no body text, and
# a content gate that only looks for text reads such a note as an empty page (or, worse, as a login
# wall) and drops something perfectly readable.
VIDEO_PLAYER_SELECTORS = ("video", "xg-video-player", "[class*='player-container']")


def video_from_page(page) -> "Optional[str]":
    """A DIRECT video URL for the page's main <video>, or None.

    `blob:` is rejected rather than returned: it is a MediaSource handle valid only inside that
    Chrome process, so passing one to a transcriber fails later and further from the cause than
    returning None does. When this returns None on a page that clearly has a player, the URL is on
    the wire instead: see attach_video_sniffer()."""
    try:
        return (page.evaluate(_VIDEO_SRC_JS) or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


def attach_video_sniffer(page) -> list:
    """Register a response listener that records direct video URLs as the page loads, and return
    the list it fills (ordered, deduped). Attach BEFORE navigating.

    This is the blob: case's only answer. The handler swallows everything: it runs on the page's
    event loop, where a raise would take down the navigation it is only observing."""
    seen: list = []

    def _on_video_resp(resp) -> None:
        try:
            u = resp.url or ""
            if _VIDEO_WIRE_RE.fullmatch(u) and u not in seen:
                seen.append(u)
        except Exception:  # noqa: BLE001
            pass

    try:
        page.on("response", _on_video_resp)
    except Exception:  # noqa: BLE001
        pass
    return seen


def video_with_origin(page, sniffed: "Optional[list]" = None) -> "tuple[Optional[str], Optional[str]]":
    """A direct video URL for this page AND the path that produced it: ``(url, origin)``.

    origin is "dom" (read off the <video> element), "wire" (only the response sniffer ever saw a
    direct stream -- the single answer for a blob:-fed player), or "unresolved" (url is None: it
    looks like a video note and NEITHER path yielded anything a transcriber can fetch).

    The DOM wins when both have something: it is the element the page is actually playing, while
    the wire may also have carried a preview or a neighbouring card's stream.

    This is ONE helper rather than an ``or`` in each adapter because the ORIGIN is the only way to
    learn, from production output, WHICH path actually fires. Before this stamp existed the two were
    indistinguishable in every document OmniSeek had ever returned, which is how a success once got
    attributed to the DOM path on no evidence at all. Do not write a verdict here from memory: read
    ``metadata.video_src`` off real notes."""
    url = video_from_page(page)
    if url:
        return url, "dom"
    for candidate in (sniffed or []):
        if candidate:
            return candidate, "wire"
    return None, "unresolved"


def video_metadata(url: "Optional[str]", origin: "Optional[str]", *, has_player: bool) -> dict:
    """The video keys a document should carry; ``{}`` when the note has no video at all.

    A note WITH a player but WITHOUT a URL still gets ``video_src: "unresolved"``, and that row is
    the point: it is the denominator for "does the wire path ever fire". Emitting nothing there
    would make a video note we could not read look exactly like an image note, and a traffic sample
    could then never separate "the sniffer never fires" from "no blob-only note ever came through".

    A URL handed over with no origin is stamped "unknown" rather than assumed: a wrong label is
    worse than a visibly missing one, because the whole purpose of the field is measurement."""
    if url:
        return {"video_url": url, "video_src": origin or "unknown"}
    return {"video_src": "unresolved"} if has_player else {}


def content_with_video(text: str, video_url: "Optional[str]", *, has_player: bool) -> str:
    """Append the video-note hint when the page is a video note.

    Names BOTH readers, because a video note carries content on two tracks and either one alone
    silently loses the other. Measured on the first real note this shipped for: the audio was
    background music and every fact was burned into the frames (a schedule, 思考中, working...).
    A hint naming only the transcriber sends the agent to read the music, come back empty, and
    conclude the note has nothing, which is worse than no hint because it reads as a check that
    was performed. The blob: case says so plainly: without a URL neither reader can be used."""
    text = (text or "").strip()
    if not (video_url or has_player):
        return text
    hint = ("[视频笔记:干货在视频里,两条轨都要看。metadata.video_url 可直接喂两件工具:"
            "omniseek_view(kind=video)读画面(字幕 / 屏幕内容 / 演示),omniseek_transcribe 取口述。"
            "只做其一会漏内容:不少笔记的音轨只是背景音乐,事实全在画面上]"
            if video_url else
            "[视频笔记:干货在视频里,但播放器只给出 blob: 地址,取不到直链,"
            "omniseek_view 与 omniseek_transcribe 都用不上]")
    return (text + "\n\n" + hint) if text else hint
