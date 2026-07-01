"""Human-behavior simulation helpers for CDP browser sessions.

Phase 4 P13 (2026-05-29). Anti-detection research (two opus sub-agents)
concluded that Polaris's `connect_over_cdp` + real-Chrome architecture is the
correct stealth base (MediaCrawler 30k★ uses the same), and that the
`Runtime.enable` CDP leak is already fixed on our Chrome 148 (empirically
A/B-verified: vanilla & patchright both undetected). The ACTUAL detection
vector for 小红书 was the **behavioral layer**: direct-goto search URLs, fixed
3.5s waits, zero mouse/scroll/dwell, no rate limiting.

This module provides stdlib-only (+ Playwright built-ins) human-like:
- delays drawn from a clamped log-normal distribution (long tail like real
  humans, never a fixed constant)
- mouse movement to an element along a jittered multi-step path (not teleport)
- per-character typing with randomized inter-key delay
- multi-step scrolling with reading pauses

No new dependencies: uses `random` + `time` + Playwright's mouse/keyboard APIs.

All functions are deliberately *slow* — that is the point. They must only be
used on rate-limited, low-frequency paths (see caller's frequency gate).
"""

from __future__ import annotations

import logging
import random
import threading
import time
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

# Human-delay PROFILE, per-thread. 'safe' (default everywhere) keeps the original
# anti-detection magnitudes. A source the operator has explicitly cleared of ban risk
# (xiaohongshu, 2026-06-14) runs its CDP callback under fast(...) to SHRINK the wait
# magnitudes — while keeping jitter (never zero, so the pattern stays non-robotic) and
# keeping scroll screens / wheel steps intact (lazy-load coverage = recall unchanged). It
# is set per worker thread, so it never bleeds into a concurrent fetch of another source.
_local = threading.local()


def _profile() -> str:
    return getattr(_local, "profile", "safe")


@contextmanager
def profile(name: str):
    prev = getattr(_local, "profile", "safe")
    _local.profile = name
    try:
        yield
    finally:
        _local.profile = prev


def fast(callback):
    """Wrap a CDP callback so its human delays run in the 'fast' profile (ban-cleared
    sources only). The wrap executes inside cdp_call's worker thread, so the thread-local
    profile is set/reset around exactly this callback. Usage: cdp_call(fast(_flow), ...)."""
    def _wrapped(page):
        with profile("fast"):
            return callback(page)
    return _wrapped


# (mu, sigma, lo, hi) for the clamped-lognormal dwell/action; (lo, hi) for the uniforms.
# 'fast' tightened 2026-06-14 (operator: compress xhs human delays to ~2-3s total). Magnitudes
# roughly halved, but the two anti-detection essentials are KEPT: (1) JITTER — lognormal spread +
# uniform ranges with non-zero floors, so no delay is ever a fixed constant (a metronomic cadence
# was the ORIGINAL 小红书 ban signature, see module header); (2) the call STRUCTURE — dwell on load,
# pause between actions, settle after scroll — stays, only shorter. Only 'fast' changes, so only
# xiaohongshu (the sole operator-cleared source) is affected; 'safe' (every other CDP source +
# the 大号 9222 sessions) is byte-for-byte untouched.
_DWELL = {"safe": (1.7, 0.45, 3.0, 15.0), "fast": (-0.6, 0.4, 0.2, 1.1)}
_ACTION = {"safe": (0.35, 0.5, 0.8, 4.0), "fast": (-1.0, 0.4, 0.1, 0.6)}
_SHORT = {"safe": (0.2, 0.9), "fast": (0.03, 0.13)}
_SCROLL_SETTLE = {"safe": (1.0, 3.0), "fast": (0.2, 0.5)}
_TYPE = {"safe": (0.05, 0.18), "fast": (0.02, 0.05)}


# ── delays ────────────────────────────────────────────────────────────────
def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def action_pause() -> None:
    """Pause between discrete UI actions (click, submit). safe ~0.8-4s / fast ~0.2-1s."""
    mu, sg, lo, hi = _ACTION[_profile()]
    time.sleep(_clamp(random.lognormvariate(mu, sg), lo, hi))


def read_dwell() -> None:
    """Dwell as if reading a freshly loaded page. safe ~3-15s / fast ~0.4-2s."""
    mu, sg, lo, hi = _DWELL[_profile()]
    time.sleep(_clamp(random.lognormvariate(mu, sg), lo, hi))


def short_pause() -> None:
    """Small pause between micro-steps (mouse hops, scroll steps). safe ~0.2-0.9s / fast ~0.05-0.25s."""
    lo, hi = _SHORT[_profile()]
    time.sleep(random.uniform(lo, hi))


# ── mouse ───────────────────────────────────────────────────────────────────
def move_to_and_click(page, locator) -> bool:
    """Move the mouse to `locator` along a jittered path, then click.

    Falls back to a plain locator.click() if the bounding box is unavailable.
    Returns True if the human-path click ran, False if it fell back.
    """
    try:
        box = locator.bounding_box()
    except Exception:  # noqa: BLE001
        box = None
    if not box:
        try:
            locator.click(timeout=8000)
        except Exception as exc:  # noqa: BLE001
            logger.debug("move_to_and_click fallback click failed: %s", exc)
        return False

    # Aim for a random interior point (not dead-center — humans are sloppy)
    tx = box["x"] + box["width"] * random.uniform(0.3, 0.7)
    ty = box["y"] + box["height"] * random.uniform(0.35, 0.65)

    # One or two waypoints so the path is not a straight teleport
    wx = tx * random.uniform(0.4, 0.7)
    wy = ty * random.uniform(0.4, 0.7)
    try:
        page.mouse.move(wx, wy, steps=random.randint(5, 12))
        short_pause()
        page.mouse.move(tx, ty, steps=random.randint(8, 20))
        short_pause()
        page.mouse.click(tx, ty)
    except Exception as exc:  # noqa: BLE001
        logger.debug("human mouse path failed (%s); plain click", exc)
        try:
            locator.click(timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        return False
    return True


def move_near(page, locator) -> None:
    """Move the mouse to `locator` along a jittered path WITHOUT clicking.

    Use this for behavioral signature before a reliable native `locator.click()`
    (coordinate-clicking can miss / fail to focus on some SPA widgets like
    小红书's collapsed search bar)."""
    try:
        box = locator.bounding_box()
    except Exception:  # noqa: BLE001
        box = None
    if not box:
        return
    tx = box["x"] + box["width"] * random.uniform(0.3, 0.7)
    ty = box["y"] + box["height"] * random.uniform(0.35, 0.65)
    wx = tx * random.uniform(0.4, 0.7)
    wy = ty * random.uniform(0.4, 0.7)
    try:
        page.mouse.move(wx, wy, steps=random.randint(5, 12))
        short_pause()
        page.mouse.move(tx, ty, steps=random.randint(8, 20))
        short_pause()
    except Exception as exc:  # noqa: BLE001
        logger.debug("move_near failed: %s", exc)


# ── typing ───────────────────────────────────────────────────────────────────
def type_text(page, text: str) -> None:
    """Type `text` into the currently focused element, one char at a time with
    randomized inter-key delay (~50–180ms). Caller must focus/click the input
    first (e.g. via move_to_and_click)."""
    _tlo, _thi = _TYPE[_profile()]
    for ch in text:
        page.keyboard.type(ch)
        time.sleep(random.uniform(_tlo, _thi))


# ── scrolling ─────────────────────────────────────────────────────────────────
def scroll_like_reading(page, screens: Optional[int] = None) -> None:
    """Scroll down a few screens in small steps with reading pauses, as if
    skimming a results feed. `screens` defaults to a random 2–4."""
    if screens is None:
        screens = random.randint(2, 4)
    for _ in range(screens):
        for _ in range(random.randint(2, 4)):
            try:
                page.mouse.wheel(0, random.randint(120, 420))
            except Exception as exc:  # noqa: BLE001
                logger.debug("scroll step failed: %s", exc)
                return
            short_pause()
        # pause at the bottom of each "screen" as if reading (profile-scaled; screens +
        # wheel-step COUNT stay fixed so lazy-load coverage is unchanged — only the wait shrinks)
        _slo, _shi = _SCROLL_SETTLE[_profile()]
        time.sleep(random.uniform(_slo, _shi))
