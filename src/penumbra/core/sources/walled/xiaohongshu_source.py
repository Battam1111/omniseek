"""小红书 (xiaohongshu.com) adapter — CDP-based, uses the operator's logged-in session.

Connects (via `cdp_call`) to the persistent real Chrome over CDP and drives a
search inside the logged-in session.

## 🔒 SEALED 2026-05-29 → P13 stealth overhaul

This adapter was sealed after 小红书 officially warned that automated browsing
was detected (ban risk). Two opus research sub-agents (red team + blue team)
concluded:
- The `connect_over_cdp` + **real Chrome + real profile + real residential IP**
  base is the *correct* stealth foundation (MediaCrawler 30k★ uses the same).
- The `Runtime.enable` CDP leak is **already fixed on our Chrome 148**
  (empirically A/B-verified: both vanilla playwright and patchright undetected).
- The real detection vector was the **behavioral layer**: direct-goto search
  URLs, a fixed 3.5s wait, zero mouse/scroll/dwell, and no rate limiting.

P13 fixes (this file):
1. **Human navigation** — go to the homepage first, move the mouse to the
   search box, click, type the query character-by-character, press Enter
   (instead of `goto(search_result?keyword=...)`).
2. **Human behavior** — randomized log-normal delays, reading dwell, and
   multi-step scrolling (see `_human`).
3. **Frequency gate** — 6h content cache, a min-interval burst guard, and a
   multi-hour hard backoff if a login wall / captcha is seen.
4. **patchright** drop-in in `_cdp.py` (defense-in-depth).

✅ UN-SEALED 2026-05-29 — offline checks + ONE clean burner test on rednote.com
(24 real "读博" results, no captcha / login wall / risk signal) passed, with
operator approval. The conservative frequency gate (6h cache, 15s min-interval,
6h captcha backoff) keeps live usage human-paced. Re-seal in an emergency:
set `_SEALED = True` (makes every entry point inert, zero CDP / zero network).
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from contextlib import contextmanager
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from penumbra.core import cache, diag
from penumbra.core.normalize import Document, mk_signal
from penumbra.core.sources.walled import _human
from penumbra.core.sources.walled._cdp import cdp_call, cdp_health

logger = logging.getLogger(__name__)

# The operator's account is served the international **RedNote** site; the
# logged-in session lives on rednote.com (xiaohongshu.com 302s here for this
# region/account — verified 2026-05-29). The note-card DOM is identical
# (section.note-item / a.title / .name / .count). Target rednote.com directly to
# avoid the redirect hop.
HOME_URL = "https://www.rednote.com"

# Isolated xhs-ONLY CDP Chrome — port 9223, fresh ~/.penumbra/chrome-xhs profile
# (a dedicated Chrome on port 9223). PHYSICALLY separate from the shared 9222 Chrome
# (zhihu/一亩三分地/大号 residue) so the 小号 gets a fresh b1 and never re-exposes the
# other CDP sources. See docs/walled-sources.md.
_XHS_CDP_URL = "http://127.0.0.1:9223"

# ──────────────────────────────────────────────────────────────────────────────
# 🔓 UN-SEALED 2026-05-29 after the P13 stealth overhaul + a CLEAN single burner
# test (rednote.com, 24 real results, no captcha / no login wall / no risk
# signal — operator-approved). The frequency gate below keeps usage human-paced.
# EMERGENCY RE-SEAL: set _SEALED = True — every entry point goes inert
# (zero CDP / zero network) regardless of registration.
# 🔒 RE-SEALED 2026-05-31: 大号 got 违规处理 because the SHARED 9222 profile had both
#    大号 + 小号 logged in → device graph linked them. (History; see git/research note.)
# 🔓 UN-SEALED 2026-06-03 — the operator's INFORMED decision (D), executed the SAFEST way:
#    a FULLY ISOLATED instance, NOT the shared 9222 profile.
#    • Dedicated Chrome on port 9223 + fresh ~/.penumbra/chrome-xhs profile (NEVER had
#      the 大号; port-isolated Chrome) → fresh b1, and zhihu/一亩三分地 are
#      NOT re-exposed. The 小号 logs in fresh here via VNC.
#    • READ-ONLY only — this adapter never likes/follows/comments/posts.
#    • Frequency gate below keeps it human-paced.
# ⚠️ HONEST RESIDUAL RISK (xhs-safety research wf_d76576c5 →
#    docs/walled-sources.md): "万无一失" is NOT
#    achievable on this host. Two IRREDUCIBLE high-magnitude risks remain:
#    (1) the server-side device-graph edge (大号↔小号↔device/IP, written when the 大号
#        was penalized — no client action erases it); (2) hardware-fingerprint sameness
#        (Canvas/WebGL/audio/CPU of this physical host, identical regardless of
#        profile/IP); + same egress IP as the 大号. Net: this LOWERS the probability of
#        active punishment, it does NOT sever the association. The operator accepts this
#        residual (informed choice 2026-06-03).
# EMERGENCY RE-SEAL: set _SEALED = True — every entry point inert (zero CDP / zero network).
# ──────────────────────────────────────────────────────────────────────────────
_SEALED = False
_SEALED_MSG = "xiaohongshu adapter SEALED (小红书封号风险) — no browsing performed"

# ── frequency gate + anomaly backoff (account safety) ─────────────────────────
CACHE_TTL = 21600          # 6h — identical queries hit cache, no live request

# C (2026-06-14): build search docs from the INTERCEPTED /search/notes XHR JSON (CDP Network
# domain) instead of parsing the rendered DOM. More robust (JSON survives HTML changes that
# break DOM selectors), captures EVERY paginated XHR (recall >= DOM), and skips the
# page.content()+BeautifulSoup parse. Verified on the host: the JSON carries xsec_token (detail
# link), display_title, user.nickname, interact_info.liked_count, and a full publish date.
# Set False to instantly fall back to the DOM path if xhs ever changes the JSON schema.
_USE_XHR_CAPTURE = True

# C (2026-06-18): fetch_url note-detail captures the page's OWN /api/sns/web/v2/comment/page XHR
# responses (structured comment JSON, sub-replies inline) while scrolling like a reader, INSTEAD of
# _load_comments' expander-click loop + the DOM bs4 harvest. We FORGE nothing (the page fetches its
# own signed comment pages on scroll; we read the responses, same pattern as the search capture),
# and we do FEWER interactions than _load_comments (no expander clicks) -> faster AND lighter on the
# 小号. The body stays a DOM read: rednote SSRs it and exposes NO note-body XHR / __INITIAL_STATE__
# (probed live 2026-06-18). Set False to fall back to the DOM expand+harvest path.
_USE_XHR_COMMENTS = True


def _parse_count(s) -> Optional[int]:
    """Parse a 小红书 count: a plain string ("1410") or a "1.2万" / "3千" form."""
    if s is None:
        return None
    s = str(s).strip()
    m = re.match(r"^(\d+(?:\.\d+)?)([万千]?)$", s)
    if not m:
        try:
            return int(s)
        except ValueError:
            return None
    num = float(m.group(1))
    if m.group(2) == "万":
        num *= 10000
    elif m.group(2) == "千":
        num *= 1000
    return int(num)


def _flatten_captured_comments(items: list) -> list[dict]:
    """Captured /api/sns/web/v2/comment/page comment dicts -> the [{author,text,likes}] shape the
    fetch_url doc-build renders, INCLUDING each comment's inline sub_comments (replies) as a "↳ "
    line. Deduped by comment id (the same page can re-fire on scroll). Pure decode, no judgment."""
    out: list[dict] = []
    seen: set = set()
    for c in (items or []):
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        if cid and cid in seen:
            continue
        if cid:
            seen.add(cid)
        text = (c.get("content") or "").strip()
        if text:
            out.append({"author": (c.get("user_info") or {}).get("nickname") or "匿名",
                        "text": text, "likes": c.get("like_count") or ""})
        for sc in (c.get("sub_comments") or []):
            if not isinstance(sc, dict):
                continue
            scid = sc.get("id")
            if scid and scid in seen:
                continue
            if scid:
                seen.add(scid)
            stext = (sc.get("content") or "").strip()
            if stext:
                out.append({"author": (sc.get("user_info") or {}).get("nickname") or "匿名",
                            "text": "↳ " + stext, "likes": sc.get("like_count") or ""})
    return out


def _json_item_to_document(item: dict) -> Optional[Document]:
    """One intercepted /search/notes JSON item → Document. Field-aligned with the DOM
    path (_card_to_document) + the xsec_token detail-link contract; the JSON additionally
    carries a full publish date (corner_tag_info) the DOM only had as a short hint."""
    nc = item.get("note_card") or {}
    note_id = item.get("id") or ""
    title = (nc.get("display_title") or "").strip()
    if not note_id or not title:
        return None
    token = item.get("xsec_token") or ""
    # Same tokened search_result URL the DOM path picks (detail body is gated behind the token).
    url = f"https://www.rednote.com/search_result/{note_id}?xsec_token={token}&xsec_source="
    user = nc.get("user") or {}
    author = user.get("nickname") or user.get("nick_name")
    score = _parse_count((nc.get("interact_info") or {}).get("liked_count"))
    time_hint = None
    for tag in (nc.get("corner_tag_info") or []):
        if isinstance(tag, dict) and tag.get("type") == "publish_time":
            time_hint = tag.get("text")
            break
    return Document(
        source="xiaohongshu",
        source_id=note_id,
        url=url,
        title=title,
        content="(card preview; call penumbra_add_url on this url for the full note body)",
        author=author,
        signals=mk_signal('likes', score, kind='engagement', by='xiaohongshu/score'),
        metadata={"time_hint": time_hint, "like_count": score},
    )
# Jittered min-interval between LIVE calls (burst guard). A FIXED interval is itself a bot
# signature (humans aren't metronomic), so roll a fresh value per call. The operator set the 5-11s
# band (2026-06-11): drops the old fixed-15s signature, ~1.7x faster, still leaves a safety
# margin on the penalized 小号 (vs a flat 5s).
MIN_INTERVAL_LO, MIN_INTERVAL_HI = 5.0, 11.0
# Additive-only anti-ban jitter ON TOP of the operator's tuned 5-11s base band (never replaces it).
# Rationale: the base band is already non-metronomic, but its 5-11s spread is itself a learnable
# fingerprint over many calls (a fixed-width uniform has a recognizable shape). This adds a small,
# right-skewed extra wait that ONLY ever LENGTHENS the gap; the floor stays 5s and the resulting
# interval is always >= a plain base-band draw, so the change is strictly more human and can never
# burst the penalized 小号. Capped well under _GATE_QUEUE_TIMEOUT (75s) so the worst case
# (11 + EXTRA_JITTER_MAX) leaves ample headroom and never starves a queued live call into a []
# (preserves breadth, 铁律 1). Tiny floor keeps it non-degenerate; lognormal gives the human long tail.
EXTRA_JITTER_MIN, EXTRA_JITTER_MAX = 0.0, 4.0
BACKOFF_SECONDS = 6 * 3600  # hard stop for 6h after a login wall / captcha

_rate_lock = threading.Lock()
_last_live_call = 0.0
_next_interval = MIN_INTERVAL_LO   # the (jittered) gap required before the next live call
_backoff_until = 0.0
_consec_cdp_err = 0          # consecutive cdp_call failures (NOT login walls) → trips backoff
_CDP_ERR_THRESHOLD = 3       # sustained CDP failures look like an anomaly worth backing off


# Single-flight slot for LIVE 小号 calls. The isolated 9223 小号 Chrome serves ONE flow at a
# time ("9223 pool = 1"), and a single Chrome's CDP message pump is single-threaded anyway, so
# concurrent name-called walled requests (the operator's planned parallel walled) must QUEUE — never
# race. This lock serializes them; the min-interval is enforced by WAITING in that queue.
_gate_serialize = threading.Lock()
_GATE_QUEUE_TIMEOUT = 75.0   # max a queued live call waits for the slot before giving up ([],
                             # honest) — bounded to stay under the fetch_one 90s backstop.


@contextmanager
def _live_slot(timeout: float = _GATE_QUEUE_TIMEOUT):
    """Acquire the single live-call slot for the isolated 小号 Chrome (9223): serialize every
    live call + enforce the jittered min-interval by WAITING in a queue, never by rejecting.

    Fixes the two concurrency bugs in the old _gate_ok()/_mark_call() pair: (a) reject dropped
    BREADTH — a concurrent name-called xhs call within the interval got [] (silently, looking
    like 'no results') instead of waiting its turn (铁律 1); (b) check-then-act was NON-ATOMIC —
    two concurrent calls could both pass _gate_ok() before either _mark_call()'d, then fire at
    once → a burst that bans the penalized 小号. Here both are impossible: one flow at a time,
    each spaced a full jittered interval AFTER the previous flow ended.

    Yields ``(ok, reason)``. On ok=True the caller runs its live flow inside the ``with``; the
    slot auto-releases on EVERY exit (return / exception) so it can never deadlock. ``backoff``
    is a hard stop (not queued). On timeout the caller honestly returns []/None (overflow that
    the one 小号 Chrome physically cannot serve fast — the correct ceiling, not a silent drop)."""
    global _last_live_call, _next_interval
    now = time.time()
    with _rate_lock:
        if now < _backoff_until:
            yield (False, f"backoff active ({int(_backoff_until - now)}s left)")
            return
    if not _gate_serialize.acquire(timeout=timeout):
        yield (False, f"gate queue wait exceeded {timeout:.0f}s (9223 busy)")
        return
    try:
        with _rate_lock:
            if time.time() < _backoff_until:  # a call ahead of us in the queue may have tripped it
                yield (False, f"backoff active ({int(_backoff_until - time.time())}s left)")
                return
            wait = _next_interval - (time.time() - _last_live_call)
        if wait > 0:
            time.sleep(min(wait, timeout))  # queue out the jittered min-interval — do NOT reject
        yield (True, "ok")
    finally:
        with _rate_lock:
            _last_live_call = time.time()  # mark at flow END so the next queued call waits a
            # full interval after this: the operator's base band + an additive-only extra wait (only
            # ever lengthens; floor stays 5s; clamped so even max+max stays well under the 75s gate).
            _extra = max(EXTRA_JITTER_MIN, min(EXTRA_JITTER_MAX, random.lognormvariate(0.4, 0.7)))
            _next_interval = random.uniform(MIN_INTERVAL_LO, MIN_INTERVAL_HI) + _extra
        _gate_serialize.release()


def _trip_backoff(reason: str) -> None:
    global _backoff_until
    with _rate_lock:
        _backoff_until = time.time() + BACKOFF_SECONDS
    logger.warning("xiaohongshu backoff tripped (%s) — pausing live calls %ds",
                   reason, BACKOFF_SECONDS)


def _note_cdp_result(ok: bool) -> None:
    """Track consecutive CDP failures (timeouts / wedged Chrome / captcha-throws — NOT clean
    login walls, which back off on their own). Sustained failures trip the same hard backoff
    so we stop retrying the walled 小号 site every MIN_INTERVAL (account safety)."""
    global _consec_cdp_err
    with _rate_lock:
        if ok:
            _consec_cdp_err = 0
            return
        _consec_cdp_err += 1
        tripped = _consec_cdp_err >= _CDP_ERR_THRESHOLD
        if tripped:
            _consec_cdp_err = 0  # reset so we don't immediately re-trip when backoff clears
    if tripped:
        _trip_backoff(f"{_CDP_ERR_THRESHOLD} consecutive CDP failures")


# Search-box selectors, specific → generic. 小红书 doesn't expose a stable
# documented id (MediaCrawler/Spider_XHS search via the in-page API, not the UI),
# so we lead with the placeholder text (stable: "搜索小红书"/"搜索") and fall back
# to container + last-resort text input. _find_search_box picks the first visible.
_SEARCH_BOX_SELECTORS = [
    "input#search-input",
    "input.search-input",
    "input[placeholder*='搜索']",
    ".search-container input",
    "#global-search input",
    "header input[type='text']",
    "input[type='text']",
]

# Logged-in indicator confirmed via MediaCrawler login.py (2026): the "我" entry
# links to /user/profile/. Presence ⇒ session alive; absence + login-container ⇒
# logged out.
_LOGGED_IN_SELECTOR = "a[href*='/user/profile/']"


def _find_search_box(page):
    for sel in _SEARCH_BOX_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def _login_wall(page) -> bool:
    """Are we effectively logged OUT / blocked by a login prompt?

    Hard-won lesson (2026-05-29 diagnosis): rednote/小红书 renders **two**
    ``input.search-input`` elements. A DECOY with NO id and placeholder
    "登录探索更多内容" appears FIRST in the DOM **even when fully logged in**; the
    REAL search box is ``input#search-input`` with placeholder "搜索小红书".
    Reading ``.first`` of a broad ``.search-input`` selector therefore yields a
    false "logged out". The ``a[href*='/user/profile/']`` "我" link is likewise
    unreliable (present for guests as feed-author links).

    Reliable signal:
    - a VISIBLE blocking login overlay (.login-container / LoginModal / .reds-mask)
      ⇒ wall; else
    - the REAL ``input#search-input`` placeholder: contains "登录" ⇒ wall,
      contains "搜索" ⇒ logged in. We read that specific element, never .first
      of a broad match.
    """
    # 1) Visible blocking login overlay / popup mask.
    try:
        for sel in (".login-container", "[class*='LoginModal']", ".reds-mask"):
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return True
    except Exception:  # noqa: BLE001
        pass
    # 2) The REAL search box (#search-input specifically — NOT the decoy).
    try:
        real = page.locator("input#search-input").first
        if real.count() > 0:
            ph = real.get_attribute("placeholder") or ""
            return "登录" in ph  # "搜索小红书" ⇒ logged in ⇒ False
    except Exception:  # noqa: BLE001
        pass
    # No real search box at all ⇒ can't search anyway; treat as wall.
    return True


# ── comment extraction (2026-06-11, exhaustive v2 2026-06-11) ─────────────────
# The operator's insight: on 小红书 the 经验 (crowd-sourced senior advice) lives in the
# COMMENTS, not the note body. v1 got only 17 of 52: lazy-load wasn't reaching the
# real (inner) scroll container, and NESTED REPLIES sit behind "展开 X 条回复"
# expanders (not in the DOM until clicked). v2 (_load_comments) clicks every
# expander + drives the real scrollable container to the bottom + nudges the last
# item into view (the reliable lazy-load trigger) + tolerates async with a
# 3-round stability window. _COMMENTS_JS then harvests every comment AND reply by
# walking each .note-text (excluding the main #detail-desc), so replies count too;
# it also cleans the "赞" button label out of the like count. Read-only throughout:
# expander clicks just reveal content a human reader would (no like/follow/comment).
_COMMENTS_JS = r"""
() => {
  const out=[], seen=new Set();
  const desc=document.querySelector('#detail-desc');
  const texts=document.querySelectorAll('.note-text, [class*="note-text"]');
  for(const tEl of texts){
    if(desc && desc.contains(tEl)) continue;            // skip the main note body
    const text=(tEl.innerText||'').trim();
    if(!text) continue;
    const item=tEl.closest('[class*="comment-item"], [class*="reply-item"]') || tEl.parentElement;
    const ascope=tEl.closest('[class*="comment-item"], [class*="parent-comment"], [class*="reply"]') || item;
    // NB: the comment author is `<a class="name">`; do NOT lead with a[href*="/user/"] — that
    // also matches the AVATAR link (an <a> wrapping an <img>, no text) which sits earlier in the
    // DOM, so querySelector(union) returns it first → empty author (the bug, fixed 2026-06-11).
    const aEl=ascope.querySelector('.author .name, .author-wrapper .name, a.name, .name');
    const author=aEl?((aEl.innerText||aEl.textContent||'').trim()):'';
    const lEl=item.querySelector('[class*="like"] [class*="count"], .count, [class*="like"]');
    let likes=lEl?(lEl.innerText||'').trim():'';
    if(likes && !/^[0-9.]+[万千]?$/.test(likes)) likes='';   // drop the bare "赞" button label
    const key=author+'|'+text.slice(0,50);
    if(seen.has(key)) continue;
    seen.add(key);
    out.push({author, text, likes});
    if(out.length>=400) break;
  }
  return out;
}
"""

# Declared comment total ("共 52 条评论" / "评论 52") → lets the agent SEE if the
# harvest is complete or short (honest completeness signal).
_DECLARED_JS = r"""
() => {
  const t=document.body.innerText||'';
  const m=t.match(/共\s*(\d+)\s*条评论/)||t.match(/(\d+)\s*条评论/)||t.match(/评论\s*\(?(\d+)\)?/);
  return m?parseInt(m[1]):null;
}
"""


# Reply-expander matcher (text-based, class-name-proof). Deliberately reply-anchored
# ("展开 X 条回复" / "更多回复" / "查看…回复" / "加载更多") so it never clicks a bare "回复"
# (which would open a reply BOX — an interaction) or "查看全部评论" (which can navigate away).
_EXPANDER_JS_RE = r"""/展开|更多回复|加载更多|查看.{0,8}回复/"""


def _load_comments(page, max_rounds: int = 40) -> None:
    """Fully drill the comment thread: click EVERY 展开 X 条回复 / 更多回复 / 加载更多 expander
    and scroll the real container, until NO expander remains AND the comment+reply count is
    stable — i.e. every reply-chain is opened to its bottom. The operator's heuristic: the deepest
    sub-replies are where the conversation actually went in-depth, so we don't stop the moment
    the count plateaus (which strands collapsed deep threads); we stop when there is nothing
    left to expand. Read-only throughout (expanders only reveal content a human reader would —
    no like/follow/comment/post). Bounded (max_rounds + stable-backstop) for account-safety."""
    stable, prev = 0, -1
    for _ in range(max_rounds):
        try:  # 1) click every reply expander (JS .click reaches off-screen ones too)
            page.evaluate(r"""()=>{
              const re=%s;
              let n=0;
              for(const el of document.querySelectorAll('span,a,div,button')){
                const x=(el.innerText||'').trim();
                if(x && x.length<=16 && re.test(x) && !/收起/.test(x)){ try{el.click();n++;}catch(e){} }
                if(n>=30) break;
              }
            }""" % _EXPANDER_JS_RE)
        except Exception:  # noqa: BLE001
            pass
        try:  # 2) scroll the real (largest scrollable) comment container + last item into view
            page.evaluate(r"""()=>{
              const cs=[...document.querySelectorAll('.comments-el,.comments-container,[class*="comment-list"],[class*="comments"],[class*="scroller"]')];
              let best=null,bh=0;
              for(const c of cs){ if(c.scrollHeight>c.clientHeight+40 && c.scrollHeight>bh){best=c;bh=c.scrollHeight;} }
              if(best) best.scrollTop=best.scrollHeight;
              const it=document.querySelectorAll('[class*="comment-item"],[class*="reply-item"]');
              if(it.length) it[it.length-1].scrollIntoView({block:'end'});
              window.scrollBy(0, 2000);
            }""")
        except Exception:  # noqa: BLE001
            pass
        time.sleep(random.uniform(0.7, 1.3))  # brief jittered settle for async load (skim-paced, not a full read)
        try:
            n = page.evaluate(r"""()=>document.querySelectorAll('[class*="comment-item"],[class*="reply-item"]').length""")
        except Exception:  # noqa: BLE001
            break
        ended = False
        try:
            ended = bool(page.evaluate(r"""()=>/到底了|没有更多|已经到底|THE END/.test((document.body.innerText||'').slice(-500))"""))
        except Exception:  # noqa: BLE001
            pass
        if n == prev:
            stable += 1
        else:
            stable, prev = 0, n
        # Robust stop: a LONG plateau (async lazy-load is non-deterministic, so confirm the
        # ceiling over several rounds — a transient single-round stall must not stop us early)
        # or an explicit end-marker. We keep clicking expanders every round regardless, so a
        # late-appearing deep reply-chain still gets opened (and resets the plateau counter).
        if ended or stable >= 6:
            break


class XiaohongshuAdapter:
    name = "xiaohongshu"
    needs_credentials = False  # Login via VNC into persistent Chrome
    explicit_only = "sealed / CDP (isolated 9223 Chrome, account-rate-sensitive)"
    description = "小红书 — first-hand PhD daily life + real experience sharing (CDP session)"
    # fetch_url scrolls + expands a FULL comment thread (where the 经验 lives) — that legitimately
    # outlasts the 30s default fetch_url cap, so declare a larger budget (fetcher honours it; the
    # cdp_call timeout below stays under this so CDP cleans up before the bound fires).
    fetch_timeout = 120.0

    def search(self, query: str, limit: int = 10) -> list[Document]:
        if _SEALED:
            logger.warning(_SEALED_MSG)
            return []

        key = cache.make_key("xiaohongshu", "search", query, limit)
        cached = cache.get(key)
        if cached is not None:
            return [Document.model_validate(d) for d in cached]

        with _live_slot() as (ok, why):
            if not ok:
                logger.warning("xiaohongshu freq gate: %s — skipping live search", why)
                # Legible, not silent: a gate/backoff skip is NOT a query miss.
                diag.note("xiaohongshu.gate", body=f"live slot unavailable: {why} (returned [] — not a query miss)")
                return []
            return self._search_live(query, limit, key)

    def _search_live(self, query: str, limit: int, key: str) -> list[Document]:
        """Live (CDP) half of search(), run while holding the single 小号 slot (_live_slot
        serializes + rate-gates it). Split out so the slot's ``with`` auto-releases on EVERY
        return path below — no manual release bookkeeping, so no deadlock."""
        # C: accumulate items from EVERY /search/notes XHR the page fires (one per scroll
        # page) so recall >= the DOM path. The listener appends to this outer-scope list from
        # the CDP worker thread; cdp_call's join() flushes the writes before we read it below.
        _xhr_items: list = []

        def _flow(page) -> tuple[str, Optional[str]]:
            if _USE_XHR_CAPTURE:
                def _on_resp(resp):
                    try:
                        if "/api/sns/web/v1/search/notes" in (resp.url or ""):
                            for it in ((resp.json().get("data") or {}).get("items") or []):
                                if isinstance(it, dict) and it.get("id"):
                                    _xhr_items.append(it)
                    except Exception:  # noqa: BLE001 — one unparseable XHR never breaks the search
                        pass
                page.on("response", _on_resp)
            # 1) Homepage first (NOT a direct search-URL jump), then dwell.
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
            _human.read_dwell()
            if _login_wall(page):
                return ("login", None)
            # 2) Locate the search box; human mouse-move + click + type + Enter.
            box = _find_search_box(page)
            if box is None:
                logger.warning("xiaohongshu: search box not found on homepage")
                return ("nobox", None)
            # Human mouse path (behavioral) → reliable native click (focus) →
            # type → Enter. Coordinate-clicking 小红书's collapsed search bar can
            # miss/fail-to-focus, so the focus click is a native locator.click().
            _human.move_near(page, box)
            box.click(timeout=8000)
            _human.short_pause()
            _human.type_text(page, query)
            _human.action_pause()
            page.keyboard.press("Enter")
            # 3) Confirm navigation to the results page — Enter on the collapsed
            #    bar otherwise leaves us on /explore (we'd scrape the feed).
            try:
                page.wait_for_url("**/search_result**", timeout=10000)
            except Exception:  # noqa: BLE001
                pass
            try:
                page.wait_for_selector(
                    "section.note-item, .login-container, [class*='LoginModal']",
                    timeout=15000,
                )
            except Exception:  # noqa: BLE001
                pass
            if _login_wall(page):
                return ("login", None)
            _human.action_pause()
            # 4) Scroll the feed ADAPTIVELY. Each scroll fires one paginated /search/notes XHR
            #    (~22 notes) and the initial search already fired one BEFORE any scroll, so for a
            #    typical `limit` we already have enough — scroll one (jittered, human-like) screen
            #    at a time and STOP once the captured unique notes cover `limit` (+margin). Cap at
            #    4 screens so a large limit still scrolls enough: recall preserved (the first `limit`
            #    notes come from the initial XHR either way), we only skip scrolls we don't need
            #    (this was the ~4.5s the fixed 2-4 scroll spent needlessly on a typical limit).
            need = limit + 5  # margin for dedup + a few off-target cards
            for _ in range(4):
                _human.scroll_like_reading(page, screens=1)
                if _USE_XHR_CAPTURE and len({it.get("id") for it in _xhr_items if it.get("id")}) >= need:
                    break
            _human.read_dwell()
            return ("ok", page.content())

        try:
            # fast human-delay profile: the operator cleared xiaohongshu of ban risk (2026-06-14),
            # so shrink the dwell/scroll/type WAITS (jitter kept, scroll screens kept).
            status, html = cdp_call(_human.fast(_flow), initial_url=None, timeout=150, cdp_url=_XHS_CDP_URL)
            _note_cdp_result(True)
        except Exception as exc:  # noqa: BLE001
            _note_cdp_result(False)  # sustained CDP failures trip backoff (don't hammer 小号)
            logger.warning("Xiaohongshu search failed: %s", exc)
            diag.note("xiaohongshu.cdp", exc=exc, body="CDP search flow raised (9223 Chrome wedged / timeout?)")
            return []

        if status == "login":
            _trip_backoff("login wall during search")
            logger.warning("Xiaohongshu login wall — re-login needed in CDP Chrome")
            # THE key legibility fix: a logged-out 小号 session looked identical to a query miss.
            diag.note("xiaohongshu.login_wall",
                      body="小号 logged OUT of the isolated 9223 CDP Chrome — re-login via VNC required. "
                           "xiaohongshu data path is DARK until then (this is NOT a query miss / empty result).")
            return []
        if status != "ok" or not html:
            diag.note("xiaohongshu.flow",
                      body=f"search flow status={status!r}, html={'present' if html else 'none'} (not a query miss)")
            return []

        # C: prefer the intercepted XHR JSON (robust + full pagination). Fall back to the DOM
        # parse below if capture is off or yielded nothing usable (schema drift / empty).
        if _USE_XHR_CAPTURE and _xhr_items:
            jdocs: list[Document] = []
            jseen: set[str] = set()
            for it in _xhr_items:
                d = _json_item_to_document(it)
                if d and d.source_id not in jseen:
                    jseen.add(d.source_id)
                    jdocs.append(d)
                    if len(jdocs) >= limit:
                        break
            if jdocs:
                cache.set(key, [d.model_dump(mode="json") for d in jdocs], ttl=CACHE_TTL)
                return jdocs

        soup = BeautifulSoup(html, "lxml")
        cards = soup.select("section.note-item")

        docs: list[Document] = []
        seen: set[str] = set()
        for card in cards:
            try:
                doc = self._card_to_document(card)
                if doc and doc.url not in seen:
                    seen.add(doc.url)
                    docs.append(doc)
                    if len(docs) >= limit:
                        break
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping Xiaohongshu card: %s", exc)

        if not docs:
            # Ran ok + logged in + results page reached, yet 0 notes parsed: a genuine query miss
            # OR /api/sns/web/v1/search/notes XHR schema drift / section.note-item selector drift.
            diag.note("xiaohongshu.empty",
                      body="logged in + results page reached but 0 notes parsed: genuine query miss, "
                           "OR /search/notes XHR schema drift / section.note-item selector drift")
        cache.set(key, [d.model_dump(mode="json") for d in docs], ttl=CACHE_TTL)
        return docs

    def fetch_url(self, url: str) -> Optional[Document]:
        if _SEALED:
            logger.warning(_SEALED_MSG)
            return None
        host = urlparse(url).hostname or ""
        if not ("rednote.com" in host or "xiaohongshu.com" in host):
            return None

        with _live_slot() as (ok, why):
            if not ok:
                logger.warning("xiaohongshu freq gate: %s — skipping live fetch_url", why)
                diag.note("xiaohongshu.gate", body=f"live slot unavailable: {why} (returned None — not a missing note)")
                return None
            return self._fetch_url_live(url)

    def _fetch_url_live(self, url: str) -> Optional[Document]:
        """Live (CDP) half of fetch_url(), run while holding the single 小号 slot (see
        _search_live / _live_slot). The ``with`` auto-releases on every return path below."""
        # The 小号 session lives on the international rednote.com; a mainland xiaohongshu.com
        # share link does NOT share that login (shows a guest overlay) and doesn't redirect —
        # so rewrite to rednote.com to get FULL logged-in access instead of a guest read.
        nav_url = url.replace("xiaohongshu.com", "rednote.com")

        _cmt: list = []  # captured /comment/page comments (the listener appends from the CDP thread;
        #                  cdp_call's join() flushes the writes before we read it below)

        def _flow(page) -> tuple[str, Optional[str]]:
            if _USE_XHR_COMMENTS:
                def _on_cmt(resp):
                    try:
                        if "/api/sns/web/v2/comment/page" in (resp.url or ""):
                            for it in ((resp.json().get("data") or {}).get("comments") or []):
                                if isinstance(it, dict) and it.get("content"):
                                    _cmt.append(it)
                    except Exception:  # noqa: BLE001 — one unparseable XHR never breaks the fetch
                        pass
                page.on("response", _on_cmt)
            page.goto(nav_url, wait_until="domcontentloaded", timeout=30000)
            _human.read_dwell()
            # A note page renders #detail-title / #detail-desc even when a login-NUDGE
            # overlay is shown to guests (share links with xsec_token are guest-readable).
            # Prefer the content: only call it a wall if the note body is genuinely absent,
            # else _login_wall false-positives on the overlay and drops a readable note.
            has_content = False
            try:
                for sel in ("#detail-title", "#detail-desc"):
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.inner_text().strip():
                        has_content = True
                        break
            except Exception:  # noqa: BLE001
                has_content = False
            if not has_content and _login_wall(page):
                return ("login", None, [], {})
            _human.scroll_like_reading(page, screens=random.randint(1, 2))
            _human.read_dwell()
            # 小红书 puts the substance in CAROUSEL IMAGES (large, on the note-image CDN), not
            # always the text desc. Grab those URLs (skip avatars/icons; dedup) so the agent can
            # view them. Penumbra does not OCR; the consuming agent reads the images with vision.
            try:
                images = page.evaluate(
                    "()=>{const seen=new Set(),out=[];"
                    "for(const i of document.querySelectorAll('img')){"
                    "const s=i.currentSrc||i.src||'';"
                    "if(!s||s.includes('sns-avatar'))continue;"
                    "const big=i.naturalWidth>=400&&i.naturalHeight>=400;"
                    "const cdn=s.includes('rednotecdn')||s.includes('sns-web')||s.includes('ci.xhscdn');"
                    "if(big&&cdn){const b=s.split('?')[0];if(!seen.has(b)){seen.add(b);out.push(s);}}}"
                    "return out.slice(0,12);}")
            except Exception:  # noqa: BLE001
                images = []
            # Comments carry the 经验. MAX-COMPLETENESS path (completeness is non-negotiable): run
            # the EXHAUSTIVE _load_comments (scroll the comment container + click every 展开 expander
            # until stable) so the page loads ALL comments + replies, WHILE _on_cmt above PASSIVELY
            # captures the page's own signed /comment/page XHRs it fires (structured JSON, immune to
            # DOM virtualization). Then keep whichever source is MORE complete: the captured JSON
            # beats the DOM harvest when the list is virtualized; the DOM harvest beats capture when
            # deeper expander replies only render in the DOM. So we are NEVER below the DOM path, and
            # often above it. We forge nothing (the expander clicks reveal only what a human reader
            # would, same READ-ONLY contract). The expander loop is the slow part, kept because full
            # completeness requires it; speed is secondary to completeness here.
            cdata = {"list": [], "declared": None}
            dom_list: list = []
            try:
                _load_comments(page)  # exhaustive: every expander + container scroll, bounded by its own caps
                dom_list = page.evaluate(_COMMENTS_JS) or []
                cdata["declared"] = page.evaluate(_DECLARED_JS)
            except Exception:  # noqa: BLE001
                pass
            cap_list = _flatten_captured_comments(_cmt) if _USE_XHR_COMMENTS else []
            cdata["list"] = cap_list if len(cap_list) >= len(dom_list) else dom_list
            return ("ok", page.content(), images, cdata)

        try:
            # fast profile (operator-cleared); _load_comments keeps its OWN settle sleeps
            # (comment lazy-load window, NOT a human delay) so comment recall is unchanged.
            status, html, images, cdata = cdp_call(_human.fast(_flow), initial_url=None, timeout=110, cdp_url=_XHS_CDP_URL)
            _note_cdp_result(True)
        except Exception as exc:  # noqa: BLE001
            _note_cdp_result(False)  # sustained CDP failures trip backoff (don't hammer 小号)
            logger.warning("Xiaohongshu fetch_url failed: %s", exc)
            diag.note("xiaohongshu.cdp", exc=exc, body="CDP fetch_url flow raised (9223 Chrome wedged / timeout?)")
            return None

        if status == "login":
            _trip_backoff("login wall during fetch_url")
            diag.note("xiaohongshu.login_wall",
                      body="小号 logged OUT of the 9223 CDP Chrome — re-login via VNC required (note body unreadable until then)")
            return None
        if status != "ok" or not html:
            return None

        soup = BeautifulSoup(html, "lxml")
        # Prefer the note's specific IDs — the page also carries a login-overlay whose bare
        # .title / .desc sit BEFORE the note in DOM and would otherwise be grabbed instead.
        title_el = soup.select_one("#detail-title") or soup.select_one(".note-content .title")
        title = title_el.get_text(strip=True) if title_el else "(untitled)"

        body_el = soup.select_one("#detail-desc") or soup.select_one(".note-content .desc")
        body = body_el.get_text("\n", strip=True) if body_el else ""

        author_el = soup.select_one(".author-name, .name, .username")
        author = author_el.get_text(strip=True) if author_el else None

        m = re.search(r"/(?:explore|search_result|discovery/item)/([0-9a-f]{24})", url)
        source_id = m.group(1) if m else url

        # Substance is often in the images: surface them (media) + flag it when the text is thin.
        if images and len(body) < 200:
            note = (f"[正文主要在 {len(images)} 张图里(小红书常把干货放图中):图片 URL 见 media 字段,"
                    f"下载后用视觉读图中文字]")
            content = (body + "\n\n" + note) if body else note
        else:
            content = body or "(no body extracted; note may require interaction to load)"

        # Comments carry the crowd-sourced 经验 — render them into the body so any
        # penumbra_add_url consumer sees them, and keep the structured list in metadata.
        # "取到 N / 共 M" makes an incomplete harvest VISIBLE (honest completeness signal).
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

        return Document(
            source="xiaohongshu",
            source_id=source_id,
            url=url,
            title=title,
            content=content,
            author=author,
            media=images,
            metadata={"comments": comments, "comment_count": len(comments),
                      "comments_declared": declared},
        )

    def health_check(self) -> tuple[bool, str]:
        if _SEALED:
            return False, "SEALED (小红书封号风险): disabled until CDP is undetectable"
        cdp_ok, cdp_msg = cdp_health(_XHS_CDP_URL)
        if not cdp_ok:
            return False, f"CDP not reachable: {cdp_msg}"

        def _check(page) -> bool:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=20000)
            _human.read_dwell()
            return _login_wall(page)

        try:
            has_login_wall = cdp_call(_check, initial_url=None, timeout=60, cdp_url=_XHS_CDP_URL)
            if has_login_wall:
                return False, "CDP Chrome not logged into Xiaohongshu (the operator needs to log in)"
            return True, "OK (CDP + Xiaohongshu session)"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"

    @staticmethod
    def _card_to_document(card) -> Optional[Document]:
        # Title from a.title (the visible link in the footer)
        title_el = card.select_one("a.title, .title")
        if not title_el:
            return None
        title = title_el.get_text(strip=True)
        if not title:
            return None

        # Canonical URL + token. The card has TWO note anchors: a HIDDEN bare
        # <a href="/explore/<id>"> with NO token, and the VISIBLE cover/title
        # <a href="/search_result/<id>?xsec_token=...&xsec_source="> WITH it. xhs gates the note
        # DETAIL body behind that xsec_token (minted in the search context), so picking the bare
        # /explore/ link is exactly why fetch_url got "no body extracted". Prefer the tokened
        # anchor and keep its token verbatim so the body renders on fetch (verified 2026-06-07).
        tokened = card.select_one("a[href*='xsec_token']")
        src = tokened or card.select_one("a[href^='/explore/']")
        href = (src.get("href") if src else None) or (title_el.get("href") if title_el else "")
        if not href:
            return None
        href = href.replace("&amp;", "&")
        full_url = ("https://www.rednote.com" + href) if href.startswith("/") else href
        m = re.search(r"/(?:explore|search_result|discovery/item)/([0-9a-f]{24})", full_url)
        note_id = m.group(1) if m else full_url

        # Author from .name
        author_el = card.select_one(".name")
        author = author_el.get_text(strip=True) if author_el else None

        # Like count from .count
        score = None
        count_el = card.select_one(".count")
        if count_el:
            count_text = count_el.get_text(strip=True)
            # 小红书 likes can be like "1.2万", convert to int
            m_n = re.match(r"^(\d+(?:\.\d+)?)([万千]?)$", count_text)
            if m_n:
                num = float(m_n.group(1))
                unit = m_n.group(2)
                if unit == "万":
                    num *= 10000
                elif unit == "千":
                    num *= 1000
                score = int(num)

        # Time hint (relative, e.g. "04-10")
        time_el = card.select_one(".time")
        time_hint = time_el.get_text(strip=True) if time_el else None

        return Document(
            source="xiaohongshu",
            source_id=note_id or "",
            url=full_url,
            title=title,
            content="(card preview; call penumbra_add_url on this url for the full note body)",
            author=author,
            signals=mk_signal('likes', score, kind='engagement', by='xiaohongshu/score'),
            metadata={"time_hint": time_hint, "like_count": score},
        )


from penumbra.core.fetcher import register_adapter

register_adapter(XiaohongshuAdapter())
