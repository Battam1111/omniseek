"""小红书 mainland (xiaohongshu.com) — BROWSER-primary adapter, SIGNED-API fallback.

## 2026-06-25 mechanism flip (operator decision: align with the international account's method)
The PRIMARY data path is now the SAME posture as the rednote 小号 (``xiaohongshu_source.py``): drive
the mainland account's OWN 9224 Chrome to xiaohongshu.com and READ what the real browser renders /
fetches — we forge nothing. NOTE (probed live 2026-06-25): the mainland renders SEARCH results
SERVER-SIDE (the /search/notes XHR does NOT fire), so search is a DOM parse of the SSR'd
``section.note-item`` cards (``_cn_cards_from_html``); note bodies + comments use the page's own
``/comment/page`` XHR + a DOM harvest. The browser issues its own real signed navigation either way.
This replaces self-signing as the posture: the browser's own signature is real, so the account traffic
is indistinguishable from a human's (no re-implemented-signer bot-tell). The operator made this call WITH
the safety-research caveat in hand (docs/platform-notes/xiaohongshu-safety-research.md flags driving
CDP automation on the mainland 小号 as a high-risk vector); the basis is the international 小号's
zero-incident track record on the same browser method. The browser flow below carries the full
human-behavior layer (_human) + the 风控 breaker + the read-only posture.

## SIGNED DIRECT-API — now the DEGRADED FALLBACK (was the primary path pre-2026-06-25)
Used ONLY when the browser path is unavailable / errors. Computes the signature itself and calls
``edith.xiaohongshu.com`` directly:

    xhshow (pure-Python x-s / x-s-common / x-t signer)  +  curl_cffi (Chrome-TLS impersonation)
    →  signed GET/POST to edith.xiaohongshu.com  →  cursor pagination

NO page render, NO scroll — lightweight + concurrent, but self-signed (the fragility/bot-tell axis
the operator is moving away from) → kept as the fallback, not the default.

Proven live 2026-06-18 with this account's cookies (all returned ``code:0 成功``):
  - search  POST /api/sns/web/v1/search/notes   (22 results, each with xsec_token)
  - feed    POST /api/sns/web/v1/feed           (title + desc body + interact counts)
  - comment GET  /api/sns/web/v2/comment/page   (+ /comment/sub/page) full cursor pagination

WHY xhshow and not the in-browser signer: ``window._webmsxyw`` is GONE on the current site
(probed 2026-06-18: no global, the request is signed off the main thread), so the old
MediaCrawler "page.evaluate the signer" trick is dead. xhshow re-implements the ALGORITHM, so it
survives the common input/constant rotation; on a FULL scheme rotation it breaks and is fixed by
a lib bump, with the rednote browser-scroll adapter staying as the degraded fallback.

IDENTITY = (mainland account logged into the 9224 Chrome, its cookies, the mini's Shenzhen
residential DIRECT IP). The 9224 browser stays alive ONLY to mint/refresh cookies (and for
captcha / manual fallback); every data fetch here is browserless. READ-ONLY. Paced under the safe
rate (a jittered min-interval between signed calls + the 6h per-note cache). explicit_only so it
never joins the broad fan-out and gets hammered (account-rate-sensitive).

Operator note 2026-06-18: the 大号 was only WARNED, never banned, and this is a fresh dedicated mainland
account on its own clean isolated profile — so the catastrophic device-graph-cascade premise does
not apply. Keep it read-only + paced so it does not get ITSELF flagged.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qs, quote, urlparse

from penumbra.core import cache, diag
from penumbra.core.normalize import Document, mk_signal
from penumbra.core.sources.walled._cdp import cdp_call

logger = logging.getLogger(__name__)

# The mainland account's OWN isolated CDP Chrome (port 9224, profile ~/.penumbra/chrome-xhs-cn,
# launchd com.penumbra.cdp.xhs-cn). 2026-06-25 (operator: align with the international account): this
# browser is now DRIVEN to xiaohongshu.com to issue its OWN signed XHR (the PRIMARY path) — not only
# cookie-minting (which the signed-API fallback below still uses it for).
_CN_CDP_URL = "http://127.0.0.1:9224"
_CN_HOME_URL = "https://www.xiaohongshu.com"  # the mainland account is logged in HERE (not rednote)

# ── BROWSER path deps (operator 2026-06-25: align the mainland account's METHOD with the rednote
# 小号 — let the REAL browser issue its own signed /search/notes + /comment/page XHR; we only
# INTERCEPT, forging nothing). Account-agnostic browser helpers are REUSED from the proven
# xiaohongshu_source so there is ONE copy of that anti-detection logic. GUARDED: any import failure
# disables the browser path and the adapter degrades to the signed-API fallback (never goes dark). ──
try:
    from bs4 import BeautifulSoup as _BS
    from penumbra.core.sources.walled import _human
    from penumbra.core.sources.walled._cdp import (
        images_from_page as _images_from_page,
        content_with_media as _content_with_media,
    )
    from penumbra.core.sources.walled.xiaohongshu_source import (
        _load_comments as _xhs_load_comments,
        _flatten_captured_comments as _xhs_flatten_comments,
        _parse_count as _xhs_parse_count,   # handles "1.2万"/"3千" (note-card count formats)
        _COMMENTS_JS as _XHS_COMMENTS_JS,
        _DECLARED_JS as _XHS_DECLARED_JS,
    )
    _BROWSER_OK = True
except Exception as _bexc:  # noqa: BLE001 — an import issue must NOT dark this adapter (signed-API stays)
    logger.warning("xhs_cn: browser path deps unavailable (%s) — signed-API fallback only", _bexc)
    _BROWSER_OK = False

# Strict single-flight + human min-interval for the 9224 browser (== the rednote 小号's "9223 pool=1"
# invariant: ONE flow at a time on the account-rate-sensitive 小号). cdp_call(9224) already serializes
# per-Chrome; this slot ALSO enforces the human inter-call gap and makes concurrent named xhs_cn calls
# QUEUE (breadth-safe) rather than burst. Same 5-11s human band the operator tuned for the rednote 小号.
_browser_slot = threading.Lock()
_browser_rate_lock = threading.Lock()
_browser_last_flow = 0.0
_browser_next_gap = 5.0
_BROWSER_GAP_LO, _BROWSER_GAP_HI = 5.0, 11.0
# Additive-only right-skew jitter ON TOP of the 5-11s band (only ever LENGTHENS the gap; floor stays
# 5s), so this WARNED account's inter-call gap is always >= the ban-cleared rednote 小号's — never
# tighter (the rednote 小号 carries the identical EXTRA_JITTER, see xiaohongshu_source.py).
_BROWSER_GAP_EXTRA_MAX = 4.0
_BROWSER_SLOT_TIMEOUT = 75.0   # queue wait before an honest [] (kept under the fetch backstop)

# Consecutive 9224-browser-CDP-failure breaker. The rednote 小号 has this (_note_cdp_result → 6h
# backoff); the WARNED 大陆号 needs it MORE: a wedged / risk-throttled 9224 Chrome must not be re-hit
# (a fresh xiaohongshu.com nav) on every query. N raises in a row → _trip the same exponential cooldown.
_browser_cdp_fail_lock = threading.Lock()
_browser_cdp_fail = 0
_BROWSER_CDP_FAIL_TRIP = 3

EDITH = "https://edith.xiaohongshu.com"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Endpoints (all proven 2026-06-18).
_SEARCH = "/api/sns/web/v1/search/notes"   # POST
_FEED = "/api/sns/web/v1/feed"             # POST (note body)
_COMMENT = "/api/sns/web/v2/comment/page"  # GET  (top-level)
_COMMENT_SUB = "/api/sns/web/v2/comment/sub/page"  # GET (replies)

# Pacing + safety caps (account-WARNING avoidance). Borrowed from MediaCrawler's proven posture
# (config/base_config.py) + the community playbook, HALVED for an already-warned account. The
# load-bearing finding (MediaCrawler #769): the binding limit is cumulative VOLUME, not per-request
# rate — sleeping longer does NOT cure a {code:-1} session cap, only a daily ceiling + a hard STOP
# on any 风控 signal do. So pacing buys headroom; the circuit-breaker below is what actually protects.
_RATE_MIN, _RATE_MAX = 1.5, 3.5   # jittered sleep between signed calls (sub-second cadence is a bot tell)
_COOKIE_TTL = 240.0               # re-pull cookies from the live browser every ~4 min (< ~10min expiry)
_CACHE_TTL = 21600                # 6h per-note content cache (identical to the rednote adapter)
_MAX_TOP_PAGES = 10               # per-note top-comment page cap (MediaCrawler COUNT_SINGLENOTES=10)
_MAX_SUB_PAGES = 25               # only reached when sub-comments are explicitly enabled
_FETCH_SUB_COMMENTS = False       # the #769 {code:-1} danger path — OFF by default (MediaCrawler default);
                                  # top-level comments stay on, deep reply walks are opt-in.
_DAILY_REQ_CAP = 150              # daily signed-request budget (community ~300/day conservative, HALVED) — the
                                  # REAL volume guard. (No time-of-day curfew: interactive use follows the operator,
                                  # not a wall-clock; cumulative volume + the 风控 breaker are the binding constraints.)
_COOLDOWN_LADDER = (3600, 14400, 86400)        # 1h / 4h / 24h, exponential per consecutive 风控 trip
_TIMEOUT = 25

_SEALED = False  # emergency kill-switch: True → every entry point inert (zero network).
                 # 2026-06-21: this account got a platform 警告, but the operator's call = do NOT seal —
                 # it is a disposable 小号 (BOTH logged xhs accounts are 小号, neither is a precious
                 # 大号). Rely on the 风控 breaker below (auto-trips + exponential backoff on a signal).

try:
    from xhshow import Xhshow
    from curl_cffi import requests as _creq
    _signer = Xhshow()
    _DEPS_OK = True
except Exception as exc:  # noqa: BLE001 — missing deps must not break server import
    logger.warning("xiaohongshu_cn: xhshow/curl_cffi unavailable (%s) — adapter inert", exc)
    _DEPS_OK = False


# ── 风控 / warning circuit-breaker ─────────────────────────────────────────────
# The single most important safety layer (the adapter had none). Community consensus (MediaCrawler
# #769 + 风控 post-mortems): on the FIRST warning signal you STOP, you do NOT retry/sleep-through it
# (proven useless for the cumulative cap) — every retried call after a flag just burns the precious
# already-warned account toward the next strike. We classify each response, and on any hard 风控
# signal we OPEN the breaker (exponential cooldown) and abort the run; health_check surfaces it.
class XhsRiskSignal(Exception):
    """A platform 风控 / warning signal — trips the breaker, aborts the run, never retried."""


class XhsNoteGone(Exception):
    """Note abnormal / deleted / not found — skip this note; NOT a 风控 signal (no trip)."""


class _SessionExpired(Exception):
    """web_session invalid (code -101) — the one re-auth-once path (handled in _with_cookie_retry)."""


# Body text that means throttle / risk-control notice (any → hard stop).
_RISK_TEXTS = ("访问频次异常", "当前笔记暂时无法浏览", "访问链接异常", "检测到非正常操作", "账号异常")

_breaker_lock = threading.Lock()
_tripped_until = 0.0
_trip_streak = 0
_last_signal = ""
_daily_count = 0
_daily_key = ""


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _tripped() -> bool:
    return time.time() < _tripped_until


def _trip(signal: str) -> None:
    """Open the breaker for an EXPONENTIAL window per consecutive trip; record the signal so
    health_check can surface it to the operator. While tripped, every entry point returns empty."""
    global _tripped_until, _trip_streak, _last_signal
    with _breaker_lock:
        cooldown = _COOLDOWN_LADDER[min(_trip_streak, len(_COOLDOWN_LADDER) - 1)]
        _tripped_until = time.time() + cooldown
        _trip_streak += 1
        _last_signal = f"{signal} @ {datetime.now().isoformat(timespec='seconds')} (cooldown {cooldown}s)"
    logger.warning("xhs_cn 风控 BREAKER TRIPPED: %s", _last_signal)


def _clear_streak() -> None:
    """A clean run resets the consecutive-trip escalation (next trip restarts at the 1h floor)."""
    global _trip_streak
    _trip_streak = 0


def _bump_daily() -> None:
    """Count one signed request against the daily budget; hard-stop (trip until tomorrow) on breach.
    The cumulative-volume cap is the real binding constraint (#769), so this gate matters most."""
    global _daily_count, _daily_key
    with _breaker_lock:
        if _today() != _daily_key:
            _daily_key, _daily_count = _today(), 0
        _daily_count += 1
        over = _daily_count > _DAILY_REQ_CAP
    if over:
        _trip(f"daily_cap_{_DAILY_REQ_CAP}_exceeded")
        raise XhsRiskSignal("daily_cap")


def _guard(status: int, body: dict) -> dict:
    """Classify a signed-API response → return the body on success, else raise a typed signal.
    Status is checked FIRST: a 461/471 challenge page is often non-JSON and lacks Verifytype."""
    if status in (461, 471):
        _trip(f"http_{status}_captcha")
        raise XhsRiskSignal(f"http_{status}")
    if status != 200:
        raise XhsNoteGone(f"http_{status}")  # transient/other 4xx-5xx → skip, do not trip the breaker
    code = body.get("code")
    msg = body.get("msg") or ""
    if code == 300012:                                   # IP blocked — severe on the single residential IP
        _trip("ip_block_300012")
        raise XhsRiskSignal("ip_block_300012")
    if code in (-510001, -510000):                       # note abnormal / not found — skip, not 风控
        raise XhsNoteGone(f"note_{code}")
    if code == -1 and body.get("success") is False:      # the #769 session/account cumulative cap
        _trip("code_-1_session_cap")
        raise XhsRiskSignal("code_-1_session_cap")
    if code in (-101, -100) and "登录" in msg:            # web_session invalid → re-auth once
        raise _SessionExpired()
    if any(t in msg for t in _RISK_TEXTS):               # explicit throttle / risk-control text
        _trip(f"text_risk:{msg[:30]}")
        raise XhsRiskSignal(f"text_risk:{msg[:40]}")
    return body


# ── cookie provider (pull from the live 9224 browser, cached) ─────────────────
_cookie_lock = threading.Lock()
_cookies: dict = {}
_cookies_at = 0.0


def _xhs_cookie(c: dict) -> bool:
    dom = (c.get("domain") or "")
    return "xiaohongshu" in dom or dom.startswith(".")


def _pull_cookies_live() -> dict:
    """Read the mainland account's cookies straight from the 9224 browser context (incl httpOnly
    web_session, which document.cookie can't see). No navigation — context.cookies() returns the
    whole jar — so it never disturbs the logged-in tab."""
    def _flow(page):
        return json.dumps(page.context.cookies())
    raw = cdp_call(_flow, initial_url=None, timeout=40, cdp_url=_CN_CDP_URL)
    jar = json.loads(raw) if raw else []
    return {c["name"]: c["value"] for c in jar if _xhs_cookie(c)}


def _get_cookies(force: bool = False) -> dict:
    global _cookies, _cookies_at
    with _cookie_lock:
        if force or not _cookies or (time.time() - _cookies_at) > _COOKIE_TTL:
            fresh = _pull_cookies_live()
            if fresh:
                _cookies, _cookies_at = fresh, time.time()
        return dict(_cookies)


# ── signed request helpers ────────────────────────────────────────────────────
_rate_lock = threading.Lock()
_last_call = 0.0


def _pace() -> None:
    """Serialize + jitter-pace signed calls (account safety). One identity, so a global gate."""
    global _last_call
    with _rate_lock:
        wait = (_last_call + random.uniform(_RATE_MIN, _RATE_MAX)) - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()


def _base_headers(cookies: dict, signed: dict) -> dict:
    h = dict(signed)
    h["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    h["user-agent"] = _UA
    h["referer"] = "https://www.xiaohongshu.com/"
    h["origin"] = "https://www.xiaohongshu.com"
    return h


def _safe_json(r) -> dict:
    try:
        return r.json()
    except Exception:  # noqa: BLE001 — a 461/471 challenge page is HTML; _guard branches on status first
        return {}


def _signed_get(path: str, params: dict, cookies: dict) -> dict:
    _bump_daily()  # count + enforce the daily budget BEFORE issuing the request
    signed = _signer.sign_headers_get(uri=path, cookies=cookies, params=params)
    url = _signer.build_url(EDITH + path, params)
    _pace()
    r = _creq.get(url, headers=_base_headers(cookies, signed), impersonate="chrome", timeout=_TIMEOUT)
    return _guard(r.status_code, _safe_json(r))


def _signed_post(path: str, payload: dict, cookies: dict) -> dict:
    _bump_daily()
    signed = _signer.sign_headers_post(uri=path, cookies=cookies, payload=payload)
    h = _base_headers(cookies, signed)
    h["content-type"] = "application/json;charset=UTF-8"
    body = _signer.build_json_body(payload)
    _pace()
    r = _creq.post(EDITH + path, data=body, headers=h, impersonate="chrome", timeout=_TIMEOUT)
    return _guard(r.status_code, _safe_json(r))


def _with_cookie_retry(call):
    """Run a signed call. On a web_session-invalid signal (_SessionExpired), force ONE cookie
    refresh from the live browser and retry once; if it recurs, that is cookie churn = itself a
    flagged pattern → trip the breaker + abort (never loop re-auth)."""
    try:
        return call(_get_cookies())
    except _SessionExpired:
        try:
            return call(_get_cookies(force=True))
        except _SessionExpired:
            _trip("session_churn")
            raise XhsRiskSignal("session_churn")


# ── comment fetch (full cursor pagination: top-level + every deep reply thread) ─
def _flatten(comments: list, out: list) -> None:
    for c in comments:
        ui = (c.get("user_info") or {})
        out.append({"author": ui.get("nickname") or "", "text": c.get("content") or "",
                    "likes": _int(c.get("like_count")), "id": c.get("id") or ""})
        for sub in (c.get("sub_comments") or []):
            sui = (sub.get("user_info") or {})
            out.append({"author": sui.get("nickname") or "", "text": "↳ " + (sub.get("content") or ""),
                        "likes": _int(sub.get("like_count")), "id": sub.get("id") or ""})


def fetch_all_comments(note_id: str, xsec_token: str, fetch_sub: Optional[bool] = None) -> list[dict]:
    """Top-level comments (+ their INLINE replies) on a note, via signed cursor pagination (no
    browser). Returns a flat [{author, text, likes}] list (inline sub-replies prefixed '↳ '). Loops
    until has_more=False, bounded by _MAX_TOP_PAGES. Deep sub-reply DRILLING (/comment/sub/page) is
    the #769 {code:-1} danger path → OFF unless fetch_sub=True (or the _FETCH_SUB_COMMENTS default).
    A note-gone signal ends this note cleanly; a 风控 signal (XhsRiskSignal) propagates up to abort."""
    do_sub = _FETCH_SUB_COMMENTS if fetch_sub is None else fetch_sub
    out: list = []
    cursor, rounds = "", 0
    while rounds < _MAX_TOP_PAGES:
        rounds += 1
        params = {"note_id": note_id, "cursor": cursor, "top_comment_id": "",
                  "image_formats": "jpg,webp,avif", "xsec_token": xsec_token}
        try:
            j = _with_cookie_retry(lambda c: _signed_get(_COMMENT, params, c))
        except XhsNoteGone:
            break  # note deleted/abnormal/transient — stop this note (not a 风控 trip)
        if j.get("code") != 0:  # unknown non-zero (not a classified signal) — stop cleanly
            break
        data = j.get("data") or {}
        batch = data.get("comments") or []
        _flatten(batch, out)
        if do_sub:  # opt-in only: deep reply walks trip the #769 session cap fastest
            for c in batch:
                if c.get("sub_comment_has_more"):
                    _drill_sub(note_id, c, xsec_token, out)
        if not data.get("has_more"):
            break
        cursor = data.get("cursor") or ""
        if not cursor:
            break
    return out


def _drill_sub(note_id: str, parent: dict, xsec_token: str, out: list) -> None:
    """Walk one deep reply thread (/comment/sub/page). Reached ONLY when _FETCH_SUB_COMMENTS is on.
    A 风控 signal (XhsRiskSignal) propagates up to abort the whole fetch — never swallowed here."""
    rc = parent.get("id")
    cursor = parent.get("sub_comment_cursor") or ""
    for _ in range(_MAX_SUB_PAGES):
        params = {"note_id": note_id, "root_comment_id": rc, "num": "10", "cursor": cursor,
                  "image_formats": "jpg,webp,avif", "xsec_token": xsec_token}
        try:
            j = _with_cookie_retry(lambda c: _signed_get(_COMMENT_SUB, params, c))
        except XhsNoteGone:
            return
        if j.get("code") != 0:
            return
        data = j.get("data") or {}
        for sub in (data.get("comments") or []):
            sui = (sub.get("user_info") or {})
            out.append({"author": sui.get("nickname") or "", "text": "↳ " + (sub.get("content") or ""),
                        "likes": _int(sub.get("like_count"))})
        if not data.get("has_more"):
            return
        cursor = data.get("cursor") or ""
        if not cursor:
            return


# ── helpers ───────────────────────────────────────────────────────────────────
def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _note_url(note_id: str, token: str) -> str:
    return f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={token}&xsec_source=pc_search"


def _parse_note_url(url: str) -> tuple[Optional[str], str]:
    p = urlparse(url)
    if "xiaohongshu.com" not in (p.hostname or ""):
        return None, ""
    parts = [x for x in p.path.split("/") if x]
    note_id = parts[-1] if parts and parts[-1] not in ("explore", "discovery", "search_result") else None
    if not note_id or len(note_id) < 16:
        return None, ""
    token = parse_qs(p.query).get("xsec_token", [""])[0]
    return note_id, token


def _wants_full(url: str) -> bool:
    """A per-note opt-in (&xhs_full=1 on the note URL) that turns DEEP sub-reply drilling ON for
    THIS fetch only — the override of the safe _FETCH_SUB_COMMENTS=False default. Lets the operator pull
    one specific note to ~100% completeness on demand without globally enabling the #769 danger path
    (deep-drill can trip the breaker on a heavily-replied note; that is the conscious per-note cost)."""
    q = parse_qs(urlparse(url).query)
    return (q.get("xhs_full") or q.get("full") or [""])[0].lower() in ("1", "true", "yes")


# ── BROWSER path (PRIMARY, 2026-06-25) — the real 9224 browser issues its OWN signed XHR ─────────
def _browser_alive() -> bool:
    return _BROWSER_OK and not _SEALED


def _note_browser_cdp(ok: bool) -> None:
    """Feed one 9224 browser-CDP outcome to the breaker: a real return clears the streak; a fully-failed
    flow extends it and, on a sustained streak, trips this account's exponential 风控 cooldown — so a
    wedged / throttled 9224 Chrome is not re-navigated every query (mirrors the rednote 小号's
    _note_cdp_result, reusing _trip / _COOLDOWN_LADDER instead of a separate 6h backoff)."""
    global _browser_cdp_fail
    with _browser_cdp_fail_lock:
        if ok:
            _browser_cdp_fail = 0
            return
        _browser_cdp_fail += 1
        tripped = _browser_cdp_fail >= _BROWSER_CDP_FAIL_TRIP
        if tripped:
            _browser_cdp_fail = 0  # reset so it doesn't immediately re-trip once the cooldown clears
    if tripped:
        _trip(f"{_BROWSER_CDP_FAIL_TRIP}_consecutive_browser_cdp_failures")


# Specific redcaptcha / slider containers (NOT the over-broad [class*='captcha'], which false-trips on
# benign telemetry elements). Plus a 风控-TEXT fallback: phrases that never occur in normal note content
# (mirrors the signed path's _RISK_TEXTS), catching sliders the selectors miss (renamed class / iframe).
_CN_CAPTCHA_SELECTORS = ("#red-captcha", "[class*='redcaptcha']", "[class*='verify-bar']", "[class*='captcha-']")
_CN_RISK_TEXTS = ("访问频次异常", "检测到非正常操作", "账号异常", "拖动滑块", "向右滑动", "完成验证", "安全验证")


def _cn_captcha(page) -> bool:
    """A VISIBLE redcaptcha / slider → a hard 风控 signal (safety research's "滑块即刹车": STOP, NEVER
    auto-solve). Hardened against the漏判 paths the review flagged: scans ALL frames (sliders often sit
    in an iframe), uses SPECIFIC captcha containers (no false-trip), AND a specific-phrase text fallback.
    Read-only. A miss here = continuing to operate on a风控 page = the worst outcome, so it over-detects."""
    try:
        frames = list(page.frames)
    except Exception:  # noqa: BLE001
        frames = [page]
    for fr in frames:
        for sel in _CN_CAPTCHA_SELECTORS:
            try:
                loc = fr.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return True
            except Exception:  # noqa: BLE001
                continue
    try:
        txt = (page.locator("body").inner_text(timeout=1500) or "")[:1500]
        if any(t in txt for t in _CN_RISK_TEXTS):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _cn_login_wall(page) -> bool:
    """Mainland xiaohongshu.com login-wall check. The reused rednote _login_wall FALSE-POSITIVES here:
    rednote's real search box is an ``input#search-input``, but the mainland homepage search box is a
    HIDDEN ``<textarea id=search-input name=aiSearchTextarea>`` (AI search) — so rednote's "no input
    search box ⇒ wall" fallback always fired (probed live 2026-06-25, account WAS logged in). The
    reliable mainland signal is a VISIBLE blocking login overlay; a logged-in page renders note cards
    with no overlay (and `a[href*='/user/profile/']` present). Read-only."""
    try:
        for sel in (".login-container", "[class*='LoginModal']", ".reds-mask", ".login-mask"):
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _cn_card_to_document(card) -> "Optional[Document]":
    """One mainland ``section.note-item`` card (BeautifulSoup) → xiaohongshu_cn doc. Mainland search
    results are SSR'd into the DOM and the /search/notes XHR does NOT fire (probed 2026-06-25), so this
    DOM parse is the PRIMARY search decode (vs the rednote 小号's XHR intercept). Same card shape as
    rednote (a.title / a[href*=xsec_token] / .name / .count); only the URL host differs."""
    title_el = card.select_one("a.title, .title")
    if not title_el:
        return None
    title = title_el.get_text(strip=True)
    if not title:
        return None
    # The card has a hidden bare /explore/<id> link (no token) AND a visible /search_result/<id>?
    # xsec_token=... link; the note DETAIL body is gated behind that token, so prefer the tokened one.
    tokened = card.select_one("a[href*='xsec_token']")
    src = tokened or card.select_one("a[href^='/explore/']")
    href = (src.get("href") if src else None) or (title_el.get("href") if title_el else "")
    if not href:
        return None
    href = href.replace("&amp;", "&")
    full_url = ("https://www.xiaohongshu.com" + href) if href.startswith("/") else href
    m = re.search(r"/(?:explore|search_result|discovery/item)/([0-9a-f]{24})", full_url)
    note_id = m.group(1) if m else full_url
    author_el = card.select_one(".name")
    author = author_el.get_text(strip=True) if author_el else None
    count_el = card.select_one("span.count, .like-wrapper .count, .count")
    likes = _xhs_parse_count(count_el.get_text(strip=True)) if count_el else None
    return Document(
        source="xiaohongshu_cn",
        source_id=note_id,
        url=full_url,
        title=title,
        content="(card preview; call penumbra_read on this url for the full note body)",
        author=author,
        signals=mk_signal("likes", likes, kind="engagement", by="xhs/liked_count"),
        metadata={"note_id": note_id, "via": "browser-dom"},
    )


def _cn_cards_from_html(html: str, limit: int) -> list:
    """Parse all mainland search cards from the rendered page HTML → deduped docs (the PRIMARY mainland
    search decode; the page SSRs section.note-item, no XHR)."""
    docs: list = []
    seen: set = set()
    try:
        soup = _BS(html, "lxml")
    except Exception:  # noqa: BLE001
        return docs
    for card in soup.select("section.note-item"):
        try:
            d = _cn_card_to_document(card)
        except Exception:  # noqa: BLE001
            continue
        if d and d.source_id not in seen:
            seen.add(d.source_id)
            docs.append(d)
            if len(docs) >= limit:
                break
    return docs


def _cn_items_to_docs(items: list, limit: int) -> list:
    """Decode intercepted /search/notes XHR items → docs. Kept as a BONUS path: mainland search is
    SSR (no XHR), so this is normally unused, but if a query ever fires the XHR we prefer its
    structured JSON over the DOM."""
    docs: list = []
    seen: set = set()
    for it in items:
        nc = it.get("note_card") or {}
        if not nc:
            continue
        nid = it.get("id") or ""
        if not nid or nid in seen:
            continue
        seen.add(nid)
        token = it.get("xsec_token") or ""
        user = nc.get("user") or {}
        inter = nc.get("interact_info") or {}
        likes = _xhs_parse_count(inter.get("liked_count"))
        docs.append(Document(
            source="xiaohongshu_cn",
            source_id=nid,
            url=_note_url(nid, token),
            title=(nc.get("display_title") or "(untitled)").strip(),
            content=(nc.get("display_title") or "").strip(),
            author=user.get("nickname"),
            signals=mk_signal("likes", likes, kind="engagement", by="xhs/liked_count"),
            metadata={"note_id": nid, "xsec_token": token, "type": nc.get("type"),
                      "liked_count": likes, "via": "browser-xhr"},
        ))
        if len(docs) >= limit:
            break
    return docs


def _browser_search(query: str, limit: int) -> tuple[str, list]:
    """PRIMARY mainland search: drive the 9224 browser to the search_result URL and read the SSR'd
    ``section.note-item`` cards. The mainland renders search results SERVER-SIDE — the /search/notes
    XHR does NOT fire (probed live 2026-06-25), so this is a DOM parse, unlike the rednote 小号's XHR
    intercept. The browser still issues its OWN real signed navigation; we forge nothing, only read
    what it renders. Returns (status, docs): 'ok' | 'login' (login overlay / captcha → caller trips the
    breaker) | 'capped' (daily volume cap) | 'error' (CDP failure → caller falls back to signed-API).
    Strictly serial + human-paced via _browser_slot (== the 9223 pool-of-1 invariant)."""
    global _browser_last_flow, _browser_next_gap
    if not _browser_slot.acquire(timeout=_BROWSER_SLOT_TIMEOUT):
        diag.note("xiaohongshu_cn.browser_gate",
                  body=f"9224 busy: queued live search exceeded {_BROWSER_SLOT_TIMEOUT:.0f}s slot wait "
                       "(returned [] — NOT a query miss)")
        return ("error", [])
    try:
        try:
            _bump_daily()  # count this live account-touch against the SHARED daily volume cap (#769)
        except XhsRiskSignal:
            return ("capped", [])  # over the daily volume budget; _bump_daily already tripped the breaker
        with _browser_rate_lock:
            wait = _browser_next_gap - (time.time() - _browser_last_flow)
        if wait > 0:
            time.sleep(min(wait, _BROWSER_SLOT_TIMEOUT))
        items: list = []  # XHR bonus path (mainland search is SSR, so this is normally empty)
        search_url = f"{_CN_HOME_URL}/search_result?keyword={quote(query)}&source=web_explore_feed"

        def _flow(page) -> tuple:
            def _on_resp(resp):
                try:
                    if "/api/sns/web/v1/search/notes" in (resp.url or ""):
                        for it in ((resp.json().get("data") or {}).get("items") or []):
                            if isinstance(it, dict) and it.get("id"):
                                items.append(it)
                except Exception:  # noqa: BLE001 — one unparseable XHR never breaks the search
                    pass
            page.on("response", _on_resp)
            # Direct-nav to the search results (the mainland search box is a hidden AI-search textarea,
            # NOT a keyword input — the warmer already navigates this URL for this account).
            page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            _human.read_dwell()
            if _cn_captcha(page) or _cn_login_wall(page):
                return ("login", None)
            try:
                page.wait_for_selector("section.note-item, .login-container, [class*='LoginModal']", timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            if _cn_captcha(page) or _cn_login_wall(page):
                return ("login", None)
            _human.action_pause()
            need = limit + 5
            # Scroll to lazy-load more cards (mainland loads ~22 SSR'd then more on scroll). 'safe'
            # profile keeps this human-paced; re-detect a captcha that pops mid-scroll → stop.
            for _ in range(2):
                if _cn_captcha(page):
                    return ("login", None)
                _human.scroll_like_reading(page, screens=1)
                try:
                    if page.locator("section.note-item").count() >= need:
                        break
                except Exception:  # noqa: BLE001
                    pass
            _human.read_dwell()
            return ("ok", page.content())

        # 'safe' (default) human profile — NOT _human.fast (fast is cleared ONLY for the international
        # 小号; this WARNED account stays slower, safety research §5). timeout 85s < the penumbra_search 单源钻取 search
        # deadline (~90s) so cdp_call cleans up before the fetcher backstop fires.
        try:
            status, html = cdp_call(_flow, initial_url=None, timeout=85, cdp_url=_CN_CDP_URL)
        except Exception as exc:  # noqa: BLE001
            _note_browser_cdp(False)  # sustained 9224 CDP failures → trip a cooldown (don't re-nav every query)
            diag.note("xiaohongshu_cn.browser_cdp", exc=exc,
                      body="9224 CDP search flow raised (Chrome wedged / timeout?) — falling back to signed-API")
            return ("error", [])
        finally:
            with _browser_rate_lock:
                _browser_last_flow = time.time()
                _extra = max(0.0, min(_BROWSER_GAP_EXTRA_MAX, random.lognormvariate(0.4, 0.7)))
                _browser_next_gap = random.uniform(_BROWSER_GAP_LO, _BROWSER_GAP_HI) + _extra
        _note_browser_cdp(True)  # a real return (even login/empty) clears the CDP-failure streak
        if status == "login":
            return ("login", [])
        if status != "ok" or not html:
            return ("error", [])
        # DOM cards are the mainland PRIMARY decode; XHR items (if any fired) are the preferred bonus.
        docs = _cn_items_to_docs(items, limit) if items else _cn_cards_from_html(html, limit)
        return ("ok", docs)
    finally:
        _browser_slot.release()


def _browser_fetch(note_id: str, token: str, url: str) -> tuple[str, Optional["Document"]]:
    """PRIMARY mainland note fetch via the 9224 browser: navigate the note, INTERCEPT the page's OWN
    /comment/page XHR + exhaustively DOM-load the comment thread + surface carousel images. Mirrors
    the rednote 小号's _fetch_url_live. Returns (status, doc): 'login' → caller trips the breaker;
    'error' → caller falls back to signed-API. READ-ONLY (the expander clicks reveal only what a
    human reader would; no like / follow / comment)."""
    global _browser_last_flow, _browser_next_gap
    if not _browser_slot.acquire(timeout=_BROWSER_SLOT_TIMEOUT):
        diag.note("xiaohongshu_cn.browser_gate", url=url,
                  body=f"9224 busy: queued live fetch exceeded {_BROWSER_SLOT_TIMEOUT:.0f}s slot wait "
                       "(returned None — NOT a missing note)")
        return ("error", None)
    try:
        try:
            _bump_daily()  # count this live account-touch against the SHARED daily volume cap (#769)
        except XhsRiskSignal:
            return ("capped", None)
        with _browser_rate_lock:
            wait = _browser_next_gap - (time.time() - _browser_last_flow)
        if wait > 0:
            time.sleep(min(wait, _BROWSER_SLOT_TIMEOUT))
        nav_url = url or _note_url(note_id, token)
        cmt: list = []

        def _flow(page):
            def _on_cmt(resp):
                try:
                    if "/api/sns/web/v2/comment/page" in (resp.url or ""):
                        for it in ((resp.json().get("data") or {}).get("comments") or []):
                            if isinstance(it, dict) and it.get("content"):
                                cmt.append(it)
                except Exception:  # noqa: BLE001
                    pass
            page.on("response", _on_cmt)
            page.goto(nav_url, wait_until="domcontentloaded", timeout=30000)
            _human.read_dwell()
            if _cn_captcha(page):  # UNCONDITIONAL: a visible slider means 风控 regardless of body — 刹车
                return ("login", None, [], {})
            has_content = False
            try:
                for sel in ("#detail-title", "#detail-desc"):
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.inner_text().strip():
                        has_content = True
                        break
            except Exception:  # noqa: BLE001
                has_content = False
            if not has_content and _cn_login_wall(page):
                return ("login", None, [], {})
            _human.scroll_like_reading(page, screens=random.randint(1, 2))
            _human.read_dwell()
            images = _images_from_page(page)
            cdata = {"list": [], "declared": None}
            dom_list: list = []
            try:
                _xhs_load_comments(page)
                dom_list = page.evaluate(_XHS_COMMENTS_JS) or []
                cdata["declared"] = page.evaluate(_XHS_DECLARED_JS)
            except Exception:  # noqa: BLE001
                pass
            cap_list = _xhs_flatten_comments(cmt)
            cdata["list"] = cap_list if len(cap_list) >= len(dom_list) else dom_list
            return ("ok", page.content(), images, cdata)

        # 'safe' (default) human profile — NOT _human.fast (fast is cleared only for the international
        # 小号). timeout 110s < fetch_timeout (120) so cdp_call cleans up before the fetcher backstop.
        try:
            status, html, images, cdata = cdp_call(_flow, initial_url=None,
                                                    timeout=110, cdp_url=_CN_CDP_URL)
        except Exception as exc:  # noqa: BLE001
            _note_browser_cdp(False)  # sustained 9224 CDP failures → trip a cooldown (don't re-nav every query)
            diag.note("xiaohongshu_cn.browser_cdp", exc=exc, url=url,
                      body="9224 CDP fetch flow raised — falling back to signed-API")
            return ("error", None)
        finally:
            with _browser_rate_lock:
                _browser_last_flow = time.time()
                _extra = max(0.0, min(_BROWSER_GAP_EXTRA_MAX, random.lognormvariate(0.4, 0.7)))
                _browser_next_gap = random.uniform(_BROWSER_GAP_LO, _BROWSER_GAP_HI) + _extra
        _note_browser_cdp(True)
        if status == "login":
            return ("login", None)
        if status != "ok" or not html:
            return ("error", None)
        soup = _BS(html, "lxml")
        title_el = soup.select_one("#detail-title") or soup.select_one(".note-content .title")
        title = title_el.get_text(strip=True) if title_el else "(untitled)"
        body_el = soup.select_one("#detail-desc") or soup.select_one(".note-content .desc")
        body = body_el.get_text("\n", strip=True) if body_el else ""
        author_el = soup.select_one(".author-name, .name, .username")
        author = author_el.get_text(strip=True) if author_el else None
        content = _content_with_media(body or "(no body extracted)", images)
        comments = cdata.get("list") or []
        declared = cdata.get("declared")
        if comments:
            short = f" / 共 {declared} 条" if declared else ""
            lines = [f"\n\n—— 评论区(取到 {len(comments)} 条{short})——"]
            for c in comments:
                who = c.get("author") or "匿名"
                lk = f" ·赞{c['likes']}" if c.get("likes") else ""
                lines.append(f"[{who}{lk}] {c.get('text', '')}")
            content += "\n".join(lines)
        doc = Document(
            source="xiaohongshu_cn",
            source_id=note_id,
            url=url or _note_url(note_id, token),
            title=title,
            content=content,
            author=author,
            media=images,
            metadata={"note_id": note_id, "xsec_token": token, "comments": comments,
                      "comments_fetched": len(comments), "comments_declared": declared, "via": "browser"},
        )
        return ("ok", doc)
    finally:
        _browser_slot.release()


# ── adapter ────────────────────────────────────────────────────────────────────
class XiaohongshuCNAdapter:
    name = "xiaohongshu_cn"
    needs_credentials = False  # cookies come from the logged-in 9224 browser
    explicit_only = "signed API (9224 mainland account, account-rate-sensitive)"
    description = ("小红书 mainland (xiaohongshu.com) — 真浏览器驱动 (与 rednote 小号同一安全机制; 2026-06-25 由 "
                   "self-signed direct-API 切换): search 读 SSR 笔记卡片, 笔记正文 + 完整评论区走拦截+DOM; "
                   "forge nothing. signed direct-API 为 degraded fallback.")
    fetch_timeout = 120.0  # >= the browser path's 110s cdp_call (matches the rednote 小号); the old 90s
                           # would let penumbra_read's backstop kill an in-progress fetch + orphan a 9224 tab.

    def _alive(self) -> bool:
        # Alive if EITHER path is available: the browser path (primary) or the signed-API (fallback).
        return (_BROWSER_OK or _DEPS_OK) and not _SEALED

    def health_check(self) -> tuple[bool, str]:
        """Pure STATE read — NO network, so the health SWEEP itself never adds an account touch on the
        warned 9224 account (the sweep is recurring/automated; the safety research's §5 'low-frequency'
        line is about minimizing such automated activity). NOTE: the sweep is not the ONLY automated
        9224 driver — scripts/session_warmer.py runs an active-hours cookie-warm search on 9224. With the
        browser path now self-warming cookies on every named call, the operator may want to drop the 大陆号
        from session_warmer to cut total automated touches (the operator's risk call; flagged, not auto-changed)."""
        if not (_BROWSER_OK or _DEPS_OK):
            return False, "browser deps (bs4/lxml + xiaohongshu_source helpers) AND signed deps (xhshow/curl_cffi) both unavailable"
        if _SEALED:
            return False, "sealed (manual kill-switch)"
        if _tripped():
            return False, f"风控 breaker OPEN: {_last_signal}; {int(_tripped_until - time.time())}s cooldown left"
        primary = "browser (9224 自发签名 XHR)" if _BROWSER_OK else "signed-API"
        return True, (f"ok (primary={primary}; today {_daily_count}/{_DAILY_REQ_CAP} live account-touches "
                      f"[browser + signed]; sub_comments={'ON' if _FETCH_SUB_COMMENTS else 'off'})")

    def search(self, query: str, limit: int = 20) -> list[Document]:
        if _SEALED:
            return []
        key = cache.make_key("xiaohongshu_cn", "search", query, limit)
        cached = cache.get(key)
        if cached:  # non-empty hit only (a cached [] is a miss — xhs transient empties aren't authoritative)
            return [Document.model_validate(d) for d in cached]
        if _tripped():  # gate LIVE calls when the 风控 breaker is open (cache above served regardless)
            reason = f"风控 breaker OPEN: {_last_signal}"
            logger.info("xhs_cn search skip (breaker open): %s", reason)
            diag.note("xiaohongshu_cn.skip", body=(
                f"NO live call was made: {reason}. NOT an empty/failed response: the 风控 breaker is open "
                f"after a risk signal and auto-clears after the cooldown."))
            return []
        # PRIMARY: the browser path (the real browser's OWN signed XHR — operator 2026-06-25, align with the international account).
        if _browser_alive():
            status, docs = _browser_search(query, limit)
            if status == "login":
                _trip("login_wall_or_captcha_browser")
                diag.note("xiaohongshu_cn.login_wall", body=(
                    "9224 大陆号: 登录浮层 / 验证码 on xiaohongshu.com — re-login via VNC. xhs_cn data path "
                    "is DARK until then (this is NOT a query miss / empty result)."))
                return []
            if status == "capped":  # over the daily volume cap (breaker already tripped) — do NOT also hit signed
                return []
            if status == "ok":
                # AUTHORITATIVE: a completed browser flow IS the answer (even when empty). Do NOT fall
                # through to a SECOND live signed touch on the warned account for one query (review HIGH).
                if docs:
                    cache.set(key, [d.model_dump(mode="json") for d in docs], ttl=_CACHE_TTL)
                _clear_streak()  # a clean live run resets the 风控-trip escalation
                return docs
            # status == 'error' (a CDP failure, not a real empty) → fall through to the signed-API fallback.
        # FALLBACK: signed direct-API (only when its deps are present).
        if _DEPS_OK:
            return self._search_signed(query, limit)
        return []

    def _items_to_docs(self, items: list, limit: int) -> list[Document]:
        """Decode intercepted /search/notes XHR items → docs. Delegates to the module-level
        _cn_items_to_docs (kept as the XHR-bonus decoder + for the smoke golden); the mainland search
        PRIMARY decode is _cn_cards_from_html (DOM cards), since the mainland SSRs results."""
        return _cn_items_to_docs(items, limit)

    def _search_signed(self, query: str, limit: int = 20) -> list[Document]:
        """FALLBACK: the self-signed direct edith API search (the pre-2026-06-25 primary path)."""
        if not _DEPS_OK:
            return []
        key = cache.make_key("xiaohongshu_cn", "search", query, limit)
        cached = cache.get(key)
        if cached:  # non-empty hit only; a cached [] is treated as a miss (xhs transient empties
            return [Document.model_validate(d) for d in cached]  # are never authoritative)
        if _tripped():  # cache above is served regardless; gate LIVE calls only when the 风控 breaker is open
            reason = f"风控 breaker OPEN: {_last_signal}"
            logger.info("xhs_cn search skip (breaker open): %s", reason)
            diag.note("xiaohongshu_cn.skip", body=(
                f"NO live API call was made: {reason}. This is NOT an empty/failed API response: the 风控 breaker is "
                f"open after a risk signal and auto-clears after the cooldown. Daily cap + pacing remain the active guards."))
            return []
        try:
            sid = _signer.get_search_id()
        except Exception:  # noqa: BLE001
            sid = ""
        # The search API rejects a small page_size (returns 0 items, code 0), so ALWAYS request a
        # full page of 20 and slice to `limit` locally below.
        payload = {"keyword": query, "page": 1, "page_size": 20, "search_id": sid,
                   "sort": "general", "note_type": 0, "ext_flags": [],
                   "image_formats": ["jpg", "webp", "avif"]}
        try:
            j = _with_cookie_retry(lambda c: _signed_post(_SEARCH, payload, c))
        except XhsRiskSignal:
            return []  # breaker tripped inside _guard; abort cleanly (already logged by _trip)
        except Exception as exc:  # noqa: BLE001 — failure → empty (adapter contract)
            logger.warning("xhs_cn search failed: %s", exc)
            return []
        if not isinstance(j, dict) or j.get("code") != 0:
            logger.warning("xhs_cn search non-zero: %s", (j or {}).get("msg"))
            return []
        docs: list[Document] = []
        for it in ((j.get("data") or {}).get("items") or []):
            nc = it.get("note_card") or {}
            if not nc:
                continue  # ads / hint rows carry no note_card
            nid = it.get("id") or ""
            token = it.get("xsec_token") or ""
            if not nid:
                continue
            user = nc.get("user") or {}
            inter = nc.get("interact_info") or {}
            docs.append(Document(
                source="xiaohongshu_cn",
                source_id=nid,
                url=_note_url(nid, token),
                title=(nc.get("display_title") or "(untitled)").strip(),
                content=(nc.get("display_title") or "").strip(),  # search card has no body; drill via fetch_url
                author=user.get("nickname"),
                signals=mk_signal("likes", _int(inter.get("liked_count")), kind="engagement",
                                  by="xhs/liked_count"),
                metadata={"note_id": nid, "xsec_token": token, "type": nc.get("type"),
                          "liked_count": _int(inter.get("liked_count"))},
            ))
            if len(docs) >= limit:
                break
        if docs:  # never cache an empty search as authoritative (poisons future identical queries)
            cache.set(key, [d.model_dump(mode="json") for d in docs], ttl=_CACHE_TTL)
            _clear_streak()  # a clean live run resets the 风控-trip escalation back to the 1h floor
        return docs

    def fetch_url(self, url: str) -> Optional[Document]:
        if _SEALED:
            return None
        note_id, token = _parse_note_url(url)
        if not note_id:
            return None
        full = _wants_full(url)  # &xhs_full=1 → deep sub-reply drilling for THIS note (signed fallback only)
        key = cache.make_key("xiaohongshu_cn", "note", note_id, "full" if full else "top")
        cached = cache.get(key)
        if cached is not None:
            return Document.model_validate(cached)
        if _tripped():  # gate LIVE here only when the 风控 breaker is open (cache above served regardless)
            reason = f"风控 breaker OPEN: {_last_signal}"
            logger.info("xhs_cn fetch skip (breaker open): %s", reason)
            diag.note("xiaohongshu_cn.skip", url=url, body=(
                f"NO live call was made: {reason}. NOT an empty/failed response: the 风控 breaker is open "
                f"and auto-clears after the cooldown."))
            return None
        # PRIMARY: the browser path (intercept the page's OWN /comment/page XHR — "看齐国际号"). A
        # &xhs_full=1 deep-drill is the SIGNED path's exclusive capability (the browser path only does
        # top+inline), so route full requests straight to signed — never cache a shallow browser result
        # under the 'full' key (that would poison the deep-completeness contract for 6h).
        if _browser_alive() and not full:
            status, doc = _browser_fetch(note_id, token, url)
            if status == "login":
                _trip("login_wall_or_captcha_browser")
                diag.note("xiaohongshu_cn.login_wall", url=url, body=(
                    "9224 大陆号 logged OUT / captcha — re-login via VNC (note body unreadable until then)."))
                return None
            if status == "capped":
                return None
            if status == "ok" and doc is not None:
                cache.set(key, doc.model_dump(mode="json"), ttl=_CACHE_TTL)
                _clear_streak()
                return doc
            # status == 'error' → fall through to the signed-API fallback below
        # FALLBACK: signed direct-API (a full deep-drill, or when the browser path errored / is off).
        if _DEPS_OK:
            return self._fetch_signed(url)
        return None

    def _fetch_signed(self, url: str) -> Optional[Document]:
        """FALLBACK: the self-signed feed + cursor-paginated comment fetch (pre-2026-06-25 primary)."""
        if not _DEPS_OK:
            return None
        note_id, token = _parse_note_url(url)
        if not note_id:
            return None
        full = _wants_full(url)  # &xhs_full=1 → deep sub-reply drilling for THIS note (per-note override)
        key = cache.make_key("xiaohongshu_cn", "note", note_id, "full" if full else "top")
        cached = cache.get(key)
        if cached is not None:
            return Document.model_validate(cached)
        if _tripped():  # cache above is served regardless; gate LIVE here only when the 风控 breaker is open
            reason = f"风控 breaker OPEN: {_last_signal}"
            logger.info("xhs_cn fetch skip (breaker open): %s", reason)
            diag.note("xiaohongshu_cn.skip", url=url, body=(
                f"NO live API call was made: {reason}. This is NOT an empty/failed API response: the 风控 breaker is "
                f"open and auto-clears after the cooldown."))
            return None
        try:
            doc = self._fetch_note(note_id, token, url, full)
        except XhsRiskSignal:
            return None  # breaker tripped inside; abort cleanly (already logged by _trip)
        except Exception as exc:  # noqa: BLE001
            logger.warning("xhs_cn fetch_url failed (%s): %s", note_id, exc)
            return None
        if doc is not None:
            cache.set(key, doc.model_dump(mode="json"), ttl=_CACHE_TTL)
            _clear_streak()  # a clean live run resets the 风控-trip escalation back to the 1h floor
        return doc

    def _fetch_note(self, note_id: str, token: str, url: str, full: bool = False) -> Optional[Document]:
        # 1) body via the signed feed API
        feed_payload = {"source_note_id": note_id, "image_formats": ["jpg", "webp", "avif"],
                        "extra": {"need_body_topic": "1"}, "xsec_source": "pc_feed", "xsec_token": token}
        j = _with_cookie_retry(lambda c: _signed_post(_FEED, feed_payload, c))
        items = (j.get("data") or {}).get("items") if isinstance(j, dict) else None
        if not items:
            logger.warning("xhs_cn feed empty for %s: %s", note_id, (j or {}).get("msg"))
            return None
        nc = items[0].get("note_card") or {}
        title = (nc.get("title") or "").strip()
        desc = (nc.get("desc") or "").strip()
        user = nc.get("user") or {}
        inter = nc.get("interact_info") or {}
        media = [im.get("url_default") for im in (nc.get("image_list") or [])
                 if isinstance(im, dict) and im.get("url_default")]
        date = None
        ts = nc.get("time")
        if ts:
            try:
                date = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                date = None

        # 2) comment thread via signed cursor pagination (deep sub-reply drilling only if full=True)
        comments = fetch_all_comments(note_id, token, fetch_sub=full)

        body = desc or title or "(no body)"
        return Document(
            source="xiaohongshu_cn",
            source_id=note_id,
            url=url or _note_url(note_id, token),
            title=title or "(untitled)",
            content=body,
            author=user.get("nickname"),
            date=date,
            signals=mk_signal("likes", _int(inter.get("liked_count")), kind="engagement",
                              by="xhs/liked_count"),
            media=media,
            metadata={
                "note_id": note_id,
                "xsec_token": token,
                "liked_count": _int(inter.get("liked_count")),
                "collected_count": _int(inter.get("collected_count")),
                "comment_count_declared": _int(inter.get("comment_count")),
                "comments_fetched": len(comments),
                "sub_comments_drilled": full,  # False = top+inline only (~82%); True (&xhs_full=1) = deep
                "comments": comments,  # [{author, text, likes}] — sub-replies prefixed '↳ '
            },
        )


from penumbra.core.fetcher import register_adapter  # noqa: E402

register_adapter(XiaohongshuCNAdapter())
