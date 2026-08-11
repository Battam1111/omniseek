"""Shared CDP (Chrome DevTools Protocol) helper for walled-garden adapters.

Connects to the persistent Chrome instance launched by
`scripts/launch_cdp_cn_forums.sh` (registered as a launchd service on the
Mac mini). All adapters that need authenticated browser sessions
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
import queue
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

from penumbra.core import cache, diag

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
    logger.warning("patchright unavailable; CDP stealth degraded to vanilla playwright")

DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DEFAULT_THREAD_TIMEOUT = 90  # seconds


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


# Per-Chrome SERIALIZATION gate. The default cdp_call path spawns a fresh thread PER call with no
# concurrency bound, so two named walled fetches to the SAME Chrome run truly concurrently: two
# tabs, two same-site searches on one shared browser → the site's flood-control throttles one and
# the eye SILENTLY caches the empty as success (the gap-③ false-empty; proven 2026-06-22: two
# parallel 一亩三分地 fetches false-emptied one, while a serial fetch returned 35). Walled sources
# are slow + explicit_only (named, never in the broad fan-out), so STRICT serial-per-Chrome is the
# right trade: correctness over a little queueing latency. (Borrowed from exa-mcp's async job-queue
# serialization — the minimal stop-bleeding subset.) Keyed by cdp_url so different Chromes
# (9222 shared / 9223 xhs小号 / 9224 xhs大陆号) never block each other.
_cdp_gates: dict[str, "threading.Semaphore"] = {}
_cdp_gates_lock = threading.Lock()


def _gate_for(cdp_url: str) -> "threading.Semaphore":
    g = _cdp_gates.get(cdp_url)
    if g is None:
        with _cdp_gates_lock:
            g = _cdp_gates.get(cdp_url)
            if g is None:
                g = threading.Semaphore(1)
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


# ── Lever A: persistent CDP connection pool (opt-in via PENUMBRA_CDP_POOL=1) ──────────────────
# The default cdp_call() spawns a fresh thread + sync_playwright() + connect_over_cdp() PER call
# (~1.5-3s of pure driver-startup + CDP-handshake waste, since the browser itself is persistent
# but the connection is rebuilt every time). This pool keeps N persistent worker threads per
# Chrome, each holding ONE long-lived sync_playwright + connection for the process lifetime, and
# runs each callback on a fresh page inside its owning worker thread — which satisfies Playwright
# sync's thread-affinity (a connection is only ever touched by the one thread that created it).
#   * 9223 小号 → size 1 = strictly serial (anti-ban: one flow at a time, == the old _gate_serialize
#     + 9223-pool-of-1 invariant; the pool NEVER widens 9223 concurrency).
#   * 9222 shared 大号 → size 3 = a few reused connections so concurrent named walled fetches don't
#     head-of-line block each other (the single Chrome's CDP pump serializes commands anyway, but
#     page loads/renders still overlap).
# Self-heals after a Chrome restart (cdp-keepalive relaunch): a task that finds the connection dead
# reconnects on the NEXT task (the in-flight one fails exactly as the old per-call path would). The
# flag defaults OFF → behavior is byte-identical to before unless explicitly enabled, so it ships
# inert and is reversible by unsetting the env var.
_POOL_ENV = "PENUMBRA_CDP_POOL"


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
                size = 1 if ("9223" in cdp_url or "9224" in cdp_url) else 3  # 9223/9224 小号 stay serial (anti-ban)
                p = _CdpPool(cdp_url, size)
                _pools[cdp_url] = p
    return p


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

    def submit(self, callback: Callable, initial_url: Optional[str], timeout: int) -> Any:
        with self._lock:
            q = self._q  # snapshot: a concurrent _recover swap must not split put/get across queues
        reply: "queue.Queue" = queue.Queue(maxsize=1)
        q.put((callback, initial_url, reply))
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
            diag.note("cdp_call", url=initial_url, exc=payload)
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

        def _connect() -> None:
            nonlocal pw, browser
            pw = sync_playwright().start()
            browser = pw.chromium.connect_over_cdp(self.cdp_url, timeout=10000)

        while True:
            callback, initial_url, reply = q.get()
            try:
                # (Re)connect if this is the first task or the Chrome was restarted (keepalive).
                if browser is None or not browser.is_connected():
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
                    try:
                        if initial_url:
                            page.goto(initial_url, wait_until="domcontentloaded", timeout=30000)
                        reply.put(("ok", callback(page)))
                    finally:
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
             cdp_url: str = DEFAULT_CDP_URL) -> Any:
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
    if _pool_enabled():  # Lever A: route to the persistent connection pool (else per-call below)
        return _pool_for(cdp_url).submit(callback, initial_url, timeout)

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
                try:
                    if initial_url:
                        page.goto(initial_url, wait_until="domcontentloaded", timeout=30000)
                    value = callback(page)
                    result_queue.put(("ok", value))
                finally:
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
    with _gate_for(cdp_url):
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            to = TimeoutError(f"CDP call exceeded {timeout}s")
            diag.note("cdp_call", url=initial_url, exc=to)
            raise to
        status, payload = result_queue.get_nowait()
    if status == "err":
        # The CDP egress failed (TargetClosedError, a goto timeout, a dead CDP connection, a
        # selector raise inside the callback). Surface it so the fixing agent sees the wall.
        diag.note("cdp_call", url=initial_url, exc=payload)
        raise payload
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
    challenge on its OWN — measured ~2s on the mini (verified 2026-06-11 on 1point3acres). The
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


def cdp_health(cdp_url: str = DEFAULT_CDP_URL) -> tuple[bool, str]:
    """Quick connectivity check to the CDP Chrome (defaults to the shared 9222)."""
    import httpx

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
# consuming agent can VIEW them with its own vision. The eye does NOT OCR (free CJK OCR
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
