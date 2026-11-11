"""小红书 (xiaohongshu.com) adapter — CDP-based, uses the operator's logged-in session.

Connects (via `cdp_call`) to the persistent real Chrome over CDP and drives a
search inside the logged-in session.

## 🔒 SEALED 2026-05-29 → P13 stealth overhaul

This adapter was sealed after 小红书 officially warned that automated browsing
was detected (ban risk). Two research passes (red team + blue team)
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

import json
import logging
import random
import re
import threading
import time
from contextlib import contextmanager
from typing import Optional
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

import anyio

from omniseek.core import cache, diag, portal
from omniseek.core.sources._base import SourceBase
from omniseek.core.normalize import Document, mk_signal
from omniseek.core.sources.walled import _cdp, _human
from omniseek.core.sources.walled._cdp import (
    VIDEO_PLAYER_SELECTORS,
    CacheOnlyMiss,
    attach_video_sniffer,
    cdp_call,
    cdp_health,
    content_with_video,
    video_metadata,
    video_with_origin,
)

logger = logging.getLogger(__name__)

# The operator's account is served the international **RedNote** site; the
# logged-in session lives on rednote.com ONLY. Re-verified 2026-08-11: the old
# "xiaohongshu.com 302s here" behavior is gone — www.xiaohongshu.com now serves
# this profile directly, but as a GUEST (search there is login-walled; only
# tokened note pages are guest-readable). So the search flow MUST ride
# rednote.com, and fetch_url reads every note on rednote.com too: a xiaohongshu.com
# note link is navigated to rednote.com/explore/<id> with its query kept, because
# read as given it got the guest view (2026-11-11: 19 of 41 comments, no reply
# expansion; the same id and token on rednote.com read all 41). fetch_url may
# still fall back to xiaohongshu.com for the note body when the host's egress
# blackholes rednote.com alone (a proxy MATCH-lane
# exit node dying takes rednote.com with it while xiaohongshu.com rides the
# direct lane — the 2026-08-11 outage). The note DOM is identical on both hosts.
HOME_URL = "https://www.rednote.com"


def _mainland_sealed() -> bool:
    """True while the mainland 9224 adapter is sealed (its module-level _SEALED, the one restore
    point; docs/platform-notes/xiaohongshu-cn-seal.md). Read lazily so the two modules keep no
    import-order coupling; an import failure reads as unsealed (ownership stays rednote-only)."""
    try:
        from omniseek.core.sources.walled import xiaohongshu_cn_source as _cn
    except Exception:  # noqa: BLE001
        return False
    return bool(getattr(_cn, "_SEALED", False))


def _goto_note_dual_host(page, url: str) -> None:
    """Navigate to a tokened note URL with a sibling-host fallback. A note page
    with its xsec_token is guest-readable on www.xiaohongshu.com too (verified
    2026-08-11: same URL, host swapped, renders the full body in this Chrome),
    so when rednote.com fails at the NETWORK layer the note is retried once on
    the sibling host instead of losing the read. That fallback read is a GUEST one
    (first comment page only, 2026-11-11), which beats losing the note. Non-network errors, and
    failures on non-rednote URLs, re-raise unchanged. Search deliberately has
    no such fallback: the logged session does not ride to xiaohongshu.com, and
    guest search is login-walled there (probed 2026-08-11)."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return
    except Exception as exc:  # noqa: BLE001 — classified below; only net-layer failures earn the retry
        text = f"{type(exc).__name__}: {exc}"
        fallback = url.replace("://www.rednote.com/", "://www.xiaohongshu.com/", 1)
        if fallback == url or ("net::ERR_" not in text and "Timeout" not in text):
            raise
        logger.warning("xiaohongshu fetch_url: rednote.com unreachable (%s) — retrying note on xiaohongshu.com", text[:90])
        diag.note("xiaohongshu.nav_fallback", url=url, exc=exc,
                  body="rednote.com goto failed at the network layer; retrying the tokened note on xiaohongshu.com (guest-readable)")
    page.goto(fallback, wait_until="domcontentloaded", timeout=30000)

# Isolated xhs-ONLY CDP Chrome — port 9223, fresh <root>/chrome-xhs profile
# (launchd com.omniseek.cdp.xhs). PHYSICALLY separate from the shared 9222 Chrome
# (zhihu/一亩三分地/大号 residue) so the 小号 gets a fresh b1 and never re-exposes the
# other CDP sources. See scripts/launch_cdp_xhs.sh + the safety research note.
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
#    • Dedicated Chrome on port 9223 + fresh <root>/chrome-xhs profile (NEVER had
#      the 大号; launchd com.omniseek.cdp.xhs) → fresh b1, and zhihu/一亩三分地 are
#      NOT re-exposed. The 小号 logs in fresh here via VNC.
#    • READ-ONLY only — this adapter never likes/follows/comments/posts.
#    • Frequency gate below keeps it human-paced.
# ⚠️ HONEST RESIDUAL RISK (xhs-safety research wf_d76576c5 →
#    docs/platform-notes/xiaohongshu-safety-research.md): "万无一失" is NOT
#    achievable on this host. Two IRREDUCIBLE high-magnitude risks remain:
#    (1) the server-side device-graph edge (大号↔小号↔device/IP, written when the 大号
#        was penalized — no client action erases it); (2) hardware-fingerprint sameness
#        (Canvas/WebGL/audio/CPU of this physical machine, identical regardless of
#        profile/IP); + same egress IP as the 大号. Net: this LOWERS the probability of
#        active punishment, it does NOT sever the association. The operator accepts this
#        residual (informed choice 2026-06-03).
# EMERGENCY RE-SEAL: set _SEALED = True — every entry point inert (zero CDP / zero network).
# ──────────────────────────────────────────────────────────────────────────────
_SEALED = False
# Time the search flow may spend reaching the homepage, retries included (_cdp.goto_retrying). The
# flow runs under cdp_call(timeout=150); the steps after the homepage need at most about 61 s in the
# fast profile (two 10 s results waits, a 15 s card wait, two 8 s click timeouts, and at most about
# 10 s of dwell, typing and four scrolled screens, from _human's fast bounds), and connecting to the
# browser up to 10 s: 150 - 61 - 10 = 79, kept at 75 so the flow ends on its own, not on the cap.
_NAV_BUDGET_S = 75.0
_SEALED_MSG = "xiaohongshu adapter SEALED (小红书封号风险) — no browsing performed"

# ── frequency gate + anomaly backoff (account safety) ─────────────────────────
CACHE_TTL = 21600          # 6h — identical queries hit cache, no live request

# C (2026-06-14): build search docs from the INTERCEPTED /search/notes XHR JSON (CDP Network
# domain) instead of parsing the rendered DOM. More robust (JSON survives HTML changes that
# break DOM selectors), captures EVERY paginated XHR (recall >= DOM), and skips the
# page.content()+BeautifulSoup parse. Verified live: the JSON carries xsec_token (detail
# link), display_title, user.nickname, interact_info.liked_count, and a full publish date.
# Set False to instantly fall back to the DOM path if xhs ever changes the JSON schema.
_USE_XHR_CAPTURE = True

# C (2026-06-18): fetch_url note-detail captures the page's OWN /api/sns/web/v2/comment/page XHR
# responses (structured comment JSON, sub-replies inline) while scrolling like a reader, INSTEAD of
# _load_comments' expander-click loop + the DOM bs4 harvest. We FORGE nothing (the page fetches its
# own signed comment pages on scroll; we read the responses, same pattern as the search capture),
# and we do FEWER interactions than _load_comments (no expander clicks) -> faster AND lighter on the
# 小号. The body text stays a DOM read (#detail-title / #detail-desc); there is no note-body XHR. The
# 2026-06-18 probe also said rednote has no __INITIAL_STATE__; it read the global with page.evaluate in
# patchright's isolated world, where no page global is ever visible (2026-10-24). The note page's HTML
# carries window.__INITIAL_STATE__ with the note's full imageList (2026-10-08, read by
# _state_note_from_html). Set False to fall back to the DOM expand+harvest path.
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


# ── note images: the page's own initial data, not what the page has drawn (2026-10-08) ─────────
# The old read scanned the DOM <img> tags after one or two screens of scrolling, so a carousel note
# returned only the slides the page had already rendered (4 of 17 on a real note), capped at 12, and
# also picked up images posted in the comments. The note page ships its whole note in
# window.__INITIAL_STATE__.note.noteDetailMap[<note id>].note, and that object's imageList holds
# every slide in order. Reading it costs no request and no carousel click: it is already in the page.
_IMAGE_SCENE_DEFAULT = "WB_DFT"  # the full-size scene; WB_PRV is the small preview


def _https(url: str) -> str:
    """The state stores http:// CDN links; the page itself renders the same paths over https."""
    return "https://" + url[len("http://"):] if url.startswith("http://") else url


def _image_url(entry) -> str:
    """One image entry -> its full-size URL. Reads both spellings the platform uses: the note
    state's camelCase (urlDefault / infoList / imageScene / urlPre) and the comment API's
    snake_case (url_default / info_list / image_scene / url_pre). Preference: the default URL,
    then the WB_DFT scene, then the bare url, then the preview."""
    if not isinstance(entry, dict):
        return ""
    url = entry.get("urlDefault") or entry.get("url_default") or ""
    if not url:
        for info in (entry.get("infoList") or entry.get("info_list") or []):
            if isinstance(info, dict) and \
                    (info.get("imageScene") or info.get("image_scene")) == _IMAGE_SCENE_DEFAULT:
                url = info.get("url") or ""
                if url:
                    break
    url = url or entry.get("url") or entry.get("urlPre") or entry.get("url_pre") or ""
    return _https(url) if isinstance(url, str) else ""


def _image_token(url: str) -> str:
    """The file token of a CDN image URL (last path segment before '!' or '?'): the same picture
    served at two sizes, or over http and https, has one token."""
    tail = (url or "").split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return tail.split("!", 1)[0]


def _image_urls(entries) -> list[str]:
    """Image entries -> full-size URLs in the given order, deduplicated by file token."""
    out: list[str] = []
    seen: set = set()
    for e in (entries or []):
        u = _image_url(e)
        t = _image_token(u)
        if u and t not in seen:
            seen.add(t)
            out.append(u)
    return out


_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S)


def _js_state_to_json(text: str) -> str:
    """The state is a JS literal, not JSON. Both hosts carry bare `undefined` values; rednote.com
    also carries `new Set([...])` (its translate store: "originalCommentIds":new Set([]), seen on
    a guest rednote.com note-route page 2026-11-11), which made json.loads fail there, so every
    rednote.com read fell back to the DOM images. Outside strings: undefined -> null, and the
    `new Set(` / `new Map(` wrappers and their closing `)` are dropped (a Set reads as its list, a
    Map as its list of pairs; JSON has no other parentheses outside strings)."""
    out: list[str] = []
    i, n, in_str = 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                out.append(text[i:i + 2])
                i += 2
                continue
            in_str = c != '"'
            out.append(c)
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("undefined", i):
            out.append("null")
            i += len("undefined")
            continue
        elif text.startswith("new Set(", i) or text.startswith("new Map(", i):
            i += len("new Set(")
            continue
        elif c != ")":
            out.append(c)
        i += 1
    return "".join(out)


def _pick_state_note(note_state, note_id: Optional[str]) -> Optional[dict]:
    """state.note -> the note object for ``note_id`` (else the page's currentNoteId). The entry must
    carry the same noteId: a page that moved to another note is never read as this one."""
    if not isinstance(note_state, dict):
        return None
    dmap = note_state.get("noteDetailMap")
    if not isinstance(dmap, dict):
        return None
    want = note_id or note_state.get("currentNoteId") or note_state.get("firstNoteId")
    entry = dmap.get(want) if want else None
    note = entry.get("note") if isinstance(entry, dict) else None
    if not isinstance(note, dict) or (note.get("noteId") and note.get("noteId") != want):
        return None
    return note


def _state_note_from_html(html: str, note_id: Optional[str]) -> Optional[dict]:
    """The note object from the page HTML's own ``window.__INITIAL_STATE__`` script, or None."""
    m = _STATE_RE.search(html or "")
    if not m:
        return None
    try:
        state = json.loads(_js_state_to_json(m.group(1)))
    except ValueError:
        return None
    return _pick_state_note(state.get("note") if isinstance(state, dict) else None, note_id)


# The DOM fallback (no initial state): large note-CDN images, split by where they sit. An image
# inside the comment area, or served from a /comment/ path, is a comment picture, never note body.
_DOM_IMAGES_JS = (
    "()=>{const seen=new Set(),body=[],cmt=[];"
    "for(const i of document.querySelectorAll('img')){"
    "const s=i.currentSrc||i.src||'';"
    "if(!s||s.includes('sns-avatar'))continue;"
    "const big=i.naturalWidth>=400&&i.naturalHeight>=400;"
    "const cdn=s.includes('rednotecdn')||s.includes('sns-web')||s.includes('ci.xhscdn');"
    "if(!(big&&cdn))continue;"
    "const b=s.split('?')[0];if(seen.has(b))continue;seen.add(b);"
    "if(i.closest('[class*=\"comment\"]')||b.includes('/comment/'))cmt.push(s);else body.push(s);}"
    "return {body:body.slice(0,12),comment:cmt};}")


def _comment_media(comments: list, dom_comment_images: list, body_media: list) -> list[str]:
    """Every comment picture once, in thread order (captured pictures first, then any the DOM showed
    that the capture did not), minus anything that is a note image."""
    taken = {_image_token(u) for u in body_media}
    out: list[str] = []
    for u in [x for c in (comments or []) if isinstance(c, dict) for x in (c.get("images") or [])] \
            + list(dom_comment_images or []):
        t = _image_token(u)
        if u and t not in taken:
            taken.add(t)
            out.append(u)
    return out


# Paths of the page's own comment requests (2026-11-11). The first page and the scroll pages of the
# top-level thread come from comment/page, each top-level comment carrying its first one or two
# replies inline (sub_comments). Clicking "查看 N 条回复" / "View N replies" makes the page fetch the
# rest of that thread from comment/sub/page (query: note_id, root_comment_id, num, cursor; response
# data.comments holds the replies). Matched on the exact path: a loose substring test is how the
# reply pages went uncollected (".../comment/sub/page" does not contain ".../comment/page").
_CMT_PAGE_PATH = "/api/sns/web/v2/comment/page"
_CMT_SUB_PATH = "/api/sns/web/v2/comment/sub/page"


def _comment_row(c: dict, reply: bool) -> Optional[dict]:
    """One captured comment dict -> a {author,text,likes,id[,images]} row; None for an empty one."""
    text = (c.get("content") or "").strip()
    pics = _image_urls(c.get("pictures"))
    if not (text or pics):
        return None
    row = {"author": (c.get("user_info") or {}).get("nickname") or "匿名",
           "text": ("↳ " if reply else "") + (text or "[图片]"),
           "likes": c.get("like_count") or "", "id": c.get("id") or ""}
    if pics:
        row["images"] = pics
    return row


def _flatten_comment_thread(items: list, replies: Optional[list] = None) -> tuple[list[dict], dict]:
    """Captured comments -> rows in thread order, plus counts.

    ``items`` are top-level comment dicts from comment/page; ``replies`` are (root id or None,
    reply dict) pairs from comment/sub/page, the root id read off the request's root_comment_id.
    Order: each top-level comment, then its inline sub_comments, then the replies its expanded
    pages brought, every reply as a "↳ " row; one id appears once (the same page can re-fire on
    scroll, and an expanded page repeats the inline replies). A reply whose request carried no
    root id is placed by its own root_comment_id, else by target_comment.id (the comment it answers,
    which may itself be a reply); one whose thread was never captured goes to the end.
    Pure decode, no judgment."""
    order: list = []          # top-level comments, first occurrence, inline replies merged
    by_id: dict = {}
    for c in (items or []):
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        if cid and cid in by_id:
            by_id[cid]["inline"].extend(x for x in (c.get("sub_comments") or []) if isinstance(x, dict))
            continue
        slot = {"c": c, "inline": [x for x in (c.get("sub_comments") or []) if isinstance(x, dict)],
                "more": []}
        order.append(slot)
        if cid:
            by_id[cid] = slot
    reply_root: dict = {}     # reply id -> root id, to place a reply that names only its target
    for slot in order:
        for sc in slot["inline"]:
            if sc.get("id") and slot["c"].get("id"):
                reply_root[sc["id"]] = slot["c"]["id"]
    for root, r in (replies or []):
        if isinstance(r, dict) and root and r.get("id"):
            reply_root.setdefault(r["id"], root)
    orphans: list = []
    for root, r in (replies or []):
        if not isinstance(r, dict):
            continue
        if not root:
            cand = r.get("root_comment_id") or (r.get("target_comment") or {}).get("id")
            root = cand if cand in by_id else reply_root.get(cand)
        if root in by_id:
            by_id[root]["more"].append(r)
        else:
            orphans.append(r)

    out: list[dict] = []
    seen: set = set()
    counts = {"main_comments": 0, "inline_replies": 0, "expanded_replies": 0}

    def _emit(c: dict, reply: bool, key: str) -> None:
        cid = c.get("id")
        if cid and cid in seen:
            return
        if cid:
            seen.add(cid)
        row = _comment_row(c, reply)
        if row:
            out.append(row)
            counts[key] += 1

    for slot in order:
        _emit(slot["c"], False, "main_comments")
        for sc in slot["inline"]:
            _emit(sc, True, "inline_replies")
        for sc in slot["more"]:
            _emit(sc, True, "expanded_replies")
    for sc in orphans:
        _emit(sc, True, "expanded_replies")
    return out, counts


def _flatten_captured_comments(items: list, replies: Optional[list] = None) -> list[dict]:
    """Captured comment/page comment dicts (and optional comment/sub/page replies) -> the
    [{author,text,likes,id[,images]}] rows the fetch_url doc-build renders; see
    _flatten_comment_thread for the order. A picture-only comment reads "[图片]"."""
    return _flatten_comment_thread(items, replies)[0]


# Emoji in a comment travel as codes like "[笑哭R]" in the API text and as <img> in the page, which
# innerText drops; whitespace differs between the two as well. Both are removed before comparing.
_EMOJI_CODE_RE = re.compile(r"\[[^\[\]\s]{1,10}\]")


def _comment_key_text(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("↳"):
        t = t[1:]
    return re.sub(r"\s+", "", _EMOJI_CODE_RE.sub("", t))


def _merge_comment_rows(captured: list, dom: list) -> tuple[list[dict], int]:
    """The captured rows (structured, with ids) plus every page-read row they lack.

    A page-read row is the same comment as a captured one when author and text match after
    _comment_key_text, or when the same author's captured text ends its text (the page prints a
    reply to a reply as "回复 某人 : 正文"). A missing row goes right after the row that precedes it
    in the page list and is already in the result; with none before it, at the end. Returns
    (rows, how many page-read rows were added)."""
    keys: dict = {}
    by_author: dict = {}
    for i, c in enumerate(captured or []):
        if not isinstance(c, dict):
            continue
        k = _comment_key_text(c.get("text") or "")
        a = (c.get("author") or "").strip()
        keys.setdefault((a, k), i)
        if k:
            by_author.setdefault(a, []).append((k, i))
    after: dict = {}
    tail: list = []
    last: Optional[int] = None
    added = 0
    for d in (dom or []):
        if not isinstance(d, dict):
            continue
        k = _comment_key_text(d.get("text") or "")
        a = (d.get("author") or "").strip()
        if not k:
            continue
        hit = keys.get((a, k))
        if hit is None:
            hit = next((i for ck, i in by_author.get(a, []) if len(ck) >= 2 and k.endswith(ck)), None)
        if hit is not None:
            last = hit
            continue
        (after.setdefault(last, []) if last is not None else tail).append(d)
        added += 1
    out: list[dict] = []
    for i, c in enumerate(captured or []):
        out.append(c)
        out.extend(after.get(i, []))
    out.extend(tail)
    return out, added


def _card_hint(comment_count: Optional[int]) -> str:
    """The card's read hint (``metadata.read_hint``; the body itself is empty, EYE-RANK 2026-10-20),
    naming what is MISSING, including the comment thread.

    On this platform the post is the hook and the COMMENTS are the dataset: a note whose body is one
    line plus a screenshot routinely carries the actual numbers in 50+ replies. The old text only
    promised a "full note body", so an agent that skipped omniseek_read believed it had merely lost some
    prose, when it had lost the substance. Naming the count (when the card carries one) also lets the
    agent pick WHICH notes to open instead of opening in like-order."""
    if comment_count is None:
        return ("(card preview; call omniseek_read on this url for the full note body AND its comment "
                "thread — this card path carries NO comment count, so likes are not the whole story)")
    return (f"(card preview; call omniseek_read on this url for the full note body AND its "
            f"{comment_count} comments)")


def _detail_has_substance(title: str, body: str, images: list, comments: list) -> bool:
    """Return whether a detail response contains anything from the note itself.

    A login/home/error shell can still produce valid HTML and a successful CDP navigation. It is
    not a readable document. The caller must reject that shell instead of manufacturing a
    ``matched=True`` placeholder.
    """
    clean_title = (title or "").strip()
    return bool(
        (body or "").strip()
        or images
        or comments
        or (clean_title and clean_title != "(untitled)")
    )


def _refused_landing(page_url: str) -> Optional[str]:
    """Where a note read ended up when the platform refused the link, or None.

    Measured 2026-09-26 on the mainland account: an App share link (xsec_source=app_share) for a
    note older than about 60 days opened, and the server redirected it to /404/sec_... and then to
    /404 (both hops marked as server redirects in that browser's history). That is the platform's
    verdict on the link's token, not a fault of the read: the same link fails the same way again,
    and the signed feed call answered it with HTTP 461 / code 300031. So a landing whose path
    starts with /404 ends the read. Only scheme, host and path are returned: the query of a
    redirect target can carry a token, and this string goes into a caller-facing diagnostic."""
    try:
        parts = urlparse(page_url or "")
    except Exception:  # noqa: BLE001 (an unparseable url is not a refusal)
        return None
    if not parts.path.startswith("/404"):
        return None
    return f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.netloc else parts.path


def _refused_reason(landed: str) -> str:
    """The caller-facing reason for a refused note link. The ``refused:`` prefix is the contract:
    it is how a caller (the nightly digest) tells a platform verdict from a transient failure."""
    return (f"refused: the platform redirected this note link to {landed}. That is 小红书's "
            f"final answer for this link's xsec_token (typical for an old note opened from an App "
            f"share link, xsec_source=app_share), not a transient failure: retrying the same link "
            f"will not help.")


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
    # interact_info carries FOUR counts, not one. Verified live 2026-07-25 against the real
    # /search/notes payload: {liked_count, comment_count, collected_count, shared_count, ...}. Only
    # liked_count was ever read, so the card exposed likes as if they were the whole picture while the
    # eye already held the rest. That mattered: on this platform a 41-like note with 53 comments beats
    # a 375-like note with a one-line body, and an agent sorting by the only number it could see picked
    # the wrong one. Now every count the source reports is surfaced.
    inter = nc.get("interact_info") or {}
    score = _parse_count(inter.get("liked_count"))
    comments = _parse_count(inter.get("comment_count"))
    collects = _parse_count(inter.get("collected_count"))
    shares = _parse_count(inter.get("shared_count"))
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
        content="",   # a card has no body; what is missing is named in metadata.read_hint
        author=author,
        signals={
            **mk_signal('likes', score, kind='engagement', by='xiaohongshu/liked_count'),
            **mk_signal('comments', comments, kind='engagement', by='xiaohongshu/comment_count'),
            **mk_signal('collects', collects, kind='engagement', by='xiaohongshu/collected_count'),
            **mk_signal('shares', shares, kind='engagement', by='xiaohongshu/shared_count'),
        },
        metadata={"read_hint": _card_hint(comments),
                  "time_hint": time_hint, "like_count": score, "comment_count": comments,
                  "collected_count": collects, "shared_count": shares,
                  # This doc is a CARD, not the note: full=True cannot conjure a body the
                  # search response never contained. The server reads this to say so out
                  # loud instead of returning the placeholder as if it were the article.
                  "body_needs_read": True},
    )


# ── the two search APIs (2026-11-07) ─────────────────────────────────────────────────────────
# The rednote front end (index.ec832f48.js, read 2026-11-07) posts a search to one of two endpoints:
# v1 for the classic layout, v2 (WEB_AI_SEARCH_NOTES_V2) when the AI search layout is on and the
# query came from its input box. Both answers go through the SAME client-side decode (module 1717,
# export R7) before the page reads ``items``, so both are read here the same way.
_SEARCH_NOTES_API = {"/api/sns/web/v1/search/notes": 1, "/api/sns/web/v2/search/notes": 2}
_MAX_SCENE_RESPONSES = 8   # one record per search/notes answer; pages past the first say nothing new


def _search_api_version(url: str) -> Optional[int]:
    """1 or 2 for a search/notes answer, else None. Matched on the path, so a query string or another
    API host (the front end posts to its own API base) does not change the answer."""
    try:
        return _SEARCH_NOTES_API.get(urlparse(url or "").path.rstrip("/"))
    except Exception:  # noqa: BLE001
        return None


# camelCase -> snake_case for the item fields _json_item_to_document reads. The front end requests
# with transform:true and reads camelCase (noteCard, displayTitle, xsecToken, interactInfo ...); the
# wire answer read by this adapter since 2026-06-14 is snake_case. Should a v2 answer arrive in the
# camelCase form, these are the names the front end itself reads (all found in the bundle); a key
# not listed here is left as it is and shows up in the scene's key lists, never guessed at.
_CAMEL_ITEM_KEYS = {"noteCard": "note_card", "xsecToken": "xsec_token", "modelType": "model_type"}
_CAMEL_CARD_KEYS = {"displayTitle": "display_title", "interactInfo": "interact_info",
                    "cornerTagInfo": "corner_tag_info"}
_CAMEL_COUNT_KEYS = {"likedCount": "liked_count", "commentCount": "comment_count",
                     "collectedCount": "collected_count"}
_CAMEL_COVER_KEYS = {"urlDefault": "url_default", "urlPre": "url_pre"}


def _rename(d: dict, table: dict) -> dict:
    out = dict(d)
    for camel, snake in table.items():
        if camel in out:
            out.setdefault(snake, out.pop(camel))
    return out


def _snake_search_item(item: dict) -> dict:
    """A copy of ``item`` with the camelCase keys above renamed (a snake_case key already present wins)."""
    out = _rename(item, _CAMEL_ITEM_KEYS)
    nc = out.get("note_card")
    if isinstance(nc, dict):
        nc = _rename(nc, _CAMEL_CARD_KEYS)
        if isinstance(nc.get("interact_info"), dict):
            nc["interact_info"] = _rename(nc["interact_info"], _CAMEL_COUNT_KEYS)
        if isinstance(nc.get("cover"), dict):
            nc["cover"] = _rename(nc["cover"], _CAMEL_COVER_KEYS)
        if isinstance(nc.get("user"), dict):
            nc["user"] = dict(nc["user"])
        out["note_card"] = nc
    return out


def _hidden_id(text, marker: str, n: int = 24) -> Optional[tuple[str, str]]:
    """The front end's id carrier (module 1717, function i): the ``n`` characters after the LAST
    ``marker`` in ``text`` are a note id; returns (id, text without marker and id), or None."""
    if not isinstance(text, str) or not text:
        return None
    t = text.rfind(marker)
    if t < 0:
        return None
    o = t + len(marker)
    if len(text) < o + n:
        return None
    return text[o:o + n], text[:t] + text[o + n:]


def _is_decoy_cover(cover) -> bool:
    """The front end drops an item whose cover has width 0, or a url_pre ending ?v=cb1, or a
    url_default ending ?v=cb2 (module 1717, R7): such items are never shown on the page."""
    if not isinstance(cover, dict):
        return False
    w = cover.get("width")
    if isinstance(w, (int, float)) and not isinstance(w, bool) and w == 0:
        return True
    pre, dft = cover.get("url_pre"), cover.get("url_default")
    return (isinstance(pre, str) and pre.endswith("?v=cb1")) or \
        (isinstance(dft, str) and dft.endswith("?v=cb2"))


def _normalize_search_item(item) -> tuple[Optional[dict], str]:
    """One search/notes item (v1 or v2) as the page itself would show it: ``(item, how)``, ``how``
    one of "ok", "recovered" (the real note id was carried elsewhere and moved back to ``id``),
    "decoy" (item None: the page drops it) or "not_dict" (item None). A transcription of the front
    end's R7 decode, nothing added: the id moves back only when the marker is there."""
    if not isinstance(item, dict):
        return None, "not_dict"
    item = _snake_search_item(item)
    nc = item.get("note_card")
    if not isinstance(nc, dict):
        return item, "ok"
    cover, user = nc.get("cover"), nc.get("user")
    if _is_decoy_cover(cover):
        return None, "decoy"
    cover = cover if isinstance(cover, dict) else {}
    user = user if isinstance(user, dict) else {}
    dft, pre, avatar = cover.get("url_default"), cover.get("url_pre"), user.get("avatar")
    got = None
    if isinstance(dft, str) and dft.endswith("##w=vp1"):
        got = _hidden_id(avatar, "#")
        if got:
            user["avatar"], cover["url_default"] = got[1], dft[:-len("##w=vp1")]
    elif isinstance(pre, str) and pre.endswith("##w=vp4"):
        got = _hidden_id(avatar, "%%")
        if got:
            user["avatar"], cover["url_pre"] = got[1], pre[:-len("##w=vp4")]
    elif isinstance(avatar, str) and avatar.endswith("##w=vp6"):
        got = _hidden_id(pre, "#%")
        if got:
            cover["url_pre"], user["avatar"] = got[1], avatar[:-len("##w=vp6")]
    if not got:
        return item, "ok"
    item["id"] = got[0]
    return item, "recovered"


def _keys(d, limit: int = 20) -> Optional[list]:
    return sorted(d.keys())[:limit] if isinstance(d, dict) else None


def _read_search_answer(version: int, status: Optional[int], payload) -> tuple[list, dict]:
    """One search/notes answer -> (normalized items, a record of what it held). The record is the
    scene line for this answer: version, status, the platform's code / msg, the top-level and data
    keys, the item count, the first item's keys and its note_card's keys, and how many items were
    decoys, carried their id elsewhere, or were note items this adapter cannot read (no id, or no
    note_card object: the first such item's keys are kept). A shape it does not know is recorded,
    never guessed at: without ``data.items`` as a list nothing is read."""
    rec: dict = {"v": version, "status": status}
    if not isinstance(payload, dict):
        rec["payload"] = type(payload).__name__
        return [], rec
    rec["top"] = _keys(payload)
    for k in ("code", "success", "msg"):
        if k in payload:
            rec[k] = str(payload[k])[:60] if k == "msg" else payload[k]
    data = payload.get("data")
    rec["data"] = _keys(data)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        rec["items"] = None
        return [], rec
    rec["items"] = len(items)
    if items:
        rec["first"] = _keys(items[0])
        if isinstance(items[0], dict):
            rec["first_note_card"] = _keys(items[0].get("note_card") or items[0].get("noteCard"))
    out: list = []
    decoys = recovered = unparsed = 0
    for raw in items:
        it, how = _normalize_search_item(raw)
        if how == "decoy":
            decoys += 1
            continue
        if it is None:
            unparsed += 1
            continue
        recovered += how == "recovered"
        if it.get("model_type") in (None, "note") and \
                (not it.get("id") or not isinstance(it.get("note_card"), dict)):
            unparsed += 1
            rec.setdefault("unparsed_first", _keys(it))
            continue
        if it.get("id"):
            out.append(it)
    rec.update(decoys=decoys, ids_recovered=recovered, unparsed=unparsed)
    return out, rec


def _on_ai_results_route(url: str) -> bool:
    """The AI search layout's results route (/search_result_ai, front-end AISearchPath)."""
    path = urlparse(url or "").path
    return path == "/search_result_ai" or path.startswith("/search_result_ai/")


def _on_classic_results_route(url: str) -> bool:
    path = urlparse(url or "").path
    return path == "/search_result" or path.startswith("/search_result/")


# Read in the page's own world (_cdp.page_world_evaluate): the layout switch lives in the front end's
# Pinia store "aiAbExp" (isAIMode, and webLabRes, the /web/lib answer it is computed from), which the
# isolated world of page.evaluate cannot see. Every step says what it found, so an absent store reads
# as absent, not as "off".
_AI_MODE_JS = r"""() => {
  const out = {};
  try {
    const el = document.querySelector('[data-v-app]');
    const app = el && el.__vue_app__;
    out.vue_app = !!app;
    const gp = app && app.config && app.config.globalProperties;
    const pinia = gp && gp.$pinia;
    out.pinia = !!pinia;
    const st = pinia && pinia._s && pinia._s.get && pinia._s.get('aiAbExp');
    out.aiAbExp_store = !!st;
    if (st) {
      out.isAIMode = st.isAIMode;
      const lab = st.webLabRes;
      out.webLab = (lab && typeof lab === 'object') ? Object.keys(lab).slice(0, 12)
        : (lab === undefined ? 'undefined' : lab);
    }
  } catch (e) { out.error = String(e).slice(0, 120); }
  return out;
}"""


def _capture_scene(page) -> dict:
    """Where the page ended up: final URL, title, the section.note-item count and the AI layout
    switch. Each read stands alone; a read that fails says why in its own field."""
    scene: dict = {}
    try:
        scene["url"] = page.url or ""
    except Exception as exc:  # noqa: BLE001
        scene["url_error"] = repr(exc)[:120]
    try:
        scene["title"] = (page.title() or "")[:120]
    except Exception as exc:  # noqa: BLE001
        scene["title_error"] = repr(exc)[:120]
    try:
        scene["note_items"] = page.evaluate("()=>document.querySelectorAll('section.note-item').length")
    except Exception as exc:  # noqa: BLE001
        scene["note_items_error"] = repr(exc)[:120]
    try:
        scene["ai_mode"] = _cdp.page_world_evaluate(page, _AI_MODE_JS)
    except Exception as exc:  # noqa: BLE001
        scene["ai_mode"] = {"error": repr(exc)[:120]}
    return scene


def _box_record(sel: str, loc) -> dict:
    """The search box the flow clicked: its selector and the first 200 characters of its outerHTML."""
    rec = {"selector": sel}
    try:
        rec["outer_html"] = (loc.evaluate("e=>e.outerHTML") or "")[:200]
    except Exception as exc:  # noqa: BLE001
        rec["outer_html_error"] = repr(exc)[:120]
    return rec


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))


def _note_scene(page, boxes: list, responses: list) -> None:
    """Put what the page looked like when the search flow ended into the diagnostic
    (_meta.diagnostic.captures), one capture per part so none is cut by the 500-character body limit.
    Called from inside the flow (cdp_call runs it in the caller's context). Never raises."""
    try:
        scene = _capture_scene(page)
        diag.note("xiaohongshu.scene", url=scene.pop("url", None), body=_dump(scene))
        for b in list(boxes):
            diag.note("xiaohongshu.scene.search_box", body=_dump(b))
        for r in list(responses):
            diag.note("xiaohongshu.scene.search_notes", status=r.get("status"), body=_dump(r))
        if not responses:
            diag.note("xiaohongshu.scene.search_notes",
                      body="no /api/sns/web/v1/search/notes or /api/sns/web/v2/search/notes answer was seen")
    except Exception:  # noqa: BLE001 (the scene is evidence; it must never break the search)
        pass


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


def _reached_results(page, timeout: int = 10000, v2_seen=lambda: False) -> bool:
    """Is the page on a search results page? Waits up to ``timeout`` ms for the URL, then reads
    it, so a wait that timed out is answered from where the page actually is.

    The classic results route (/search_result, /search_result/<id>) counts on its own. The AI
    layout's route (/search_result_ai) does NOT count by its URL (2026-11-07): there the page is a
    results page only once a v2 search/notes answer has come in (``v2_seen``), waited for in the
    same ``timeout``. Polling goes through page.wait_for_timeout, which lets the response listener
    run; time.sleep would not."""
    deadline = time.monotonic() + timeout / 1000.0
    try:
        page.wait_for_url("**/search_result**", timeout=timeout)
    except Exception:  # noqa: BLE001
        pass
    url = page.url or ""
    if _on_ai_results_route(url):
        while not v2_seen():
            left_ms = (deadline - time.monotonic()) * 1000.0
            if left_ms <= 0:
                return False
            try:
                page.wait_for_timeout(min(250.0, left_ms))
            except Exception:  # noqa: BLE001
                return bool(v2_seen())
        return True
    return _on_classic_results_route(url)


def _find_search_box(page):
    """The first visible search box: ``(locator, selector)``, or ``(None, None)``."""
    for sel in _SEARCH_BOX_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return loc, sel
        except Exception:  # noqa: BLE001
            continue
    return None, None


def _login_overlay(page) -> bool:
    """A VISIBLE blocking login overlay / popup mask (the first half of _login_wall)."""
    try:
        for sel in (".login-container", "[class*='LoginModal']", ".reds-mask"):
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


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
    if _login_overlay(page):
        return True
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
# The 小号 reads on rednote.com, whose interface may be English: "View 6 replies", "View more
# replies", "Show more replies", "Load more" (2026-11-11); a bare "Reply" stays unmatched.
_EXPANDER_JS_RE = (r"""/展开|更多回复|加载更多|查看.{0,8}回复"""
                   r"""|\bview\s+(\d+\s+|more\s+)?repl(y|ies)\b|\bshow\s+more\s+repl(y|ies)\b"""
                   r"""|\bload\s+more\b/i""")
# Never clicked: these fold a thread back up.
_COLLAPSER_JS_RE = r"""/收起|\bhide\b|\bcollapse\b/i"""


_MAIN_LINE = ""  # key of the top-level thread in the has_more record; a reply chain is keyed by its root id


def _note_line(lines: dict, key: str, has_more, cursor, from_sub: bool) -> None:
    """Record one line's paging state from the page's own answer. ``key`` is _MAIN_LINE for the
    top-level thread (comment/page data.has_more / data.cursor) or a root comment id for its reply
    chain: from_sub=True for comment/sub/page data.has_more / data.cursor, False for what a
    comment/page item carries (sub_comment_count above its inline sub_comments, the page's own
    test, and sub_comment_cursor). has_more False is
    final (the page said there is no next page; a re-fired earlier page must not reopen it), and a
    reply chain's own sub/page answer is never overwritten by the item copy in a comment/page."""
    old = lines.get(key)
    if old is not None and (old["has_more"] is False or (old["from_sub"] and not from_sub)):
        return
    lines[key] = {"has_more": bool(has_more), "cursor": cursor or "", "from_sub": from_sub}


def _lines_open(lines: Optional[dict]) -> tuple[int, int]:
    """(lines still marked has_more, of them reply chains)."""
    vals = [(k, v) for k, v in (lines or {}).items() if v.get("has_more")]
    return len(vals), sum(1 for k, _ in vals if k != _MAIN_LINE)


def _load_comments(page, max_rounds: int = 40, lines: Optional[dict] = None) -> dict:
    """Fully drill the comment thread: click EVERY 展开 X 条回复 / 更多回复 / 加载更多 expander
    and scroll the real container, until NO expander remains AND the comment+reply count is
    stable — i.e. every reply-chain is opened to its bottom. The operator's heuristic: the deepest
    sub-replies are where the conversation actually went in-depth, so we don't stop the moment
    the count plateaus (which strands collapsed deep threads); we stop when there is nothing
    left to expand. Read-only throughout (expanders only reveal content a human reader would —
    no like/follow/comment/post). Bounded (max_rounds + stable-backstop) for account-safety.

    Each round clicks only the innermost element whose text reads as an expander (an outer span
    or div wrapping the same words is skipped), so one button gets one click per round.

    When to stop follows the page's own answers (2026-11-11): ``lines`` is the has_more record the
    response listener keeps (_note_line), one entry for the top-level thread and one per reply
    chain, read live while this loop runs. The bottom marker ("没有更多" / "THE END") only closes
    the top-level thread: a reply chain the page still marks has_more keeps the loop going as long
    as a round still finds an expander to click (an opened chain shows its next "展开更多回复" only
    after the previous page lands; the 20-comment note read 16 of 20 when the loop stopped in the very
    round the bottom showed, after one expander click). The round right after the bottom first shows always clicks once more, since
    the scroll that reached the bottom may have rendered new expanders. Returns {"rounds",
    "expander_clicks", "stop", "has_more_lines"}; stop is "bottom_marker", "count_stable",
    "has_more_unclickable" (a line still has_more but a round after the bottom found no expander to
    click), "max_rounds" or "page_error"; has_more_lines counts the lines still marked has_more at
    the stop (top-level thread included)."""
    stable, prev = 0, -1
    ended = False
    stats = {"rounds": 0, "expander_clicks": 0, "stop": "max_rounds", "has_more_lines": 0}
    for _ in range(max_rounds):
        stats["rounds"] += 1
        was_ended = ended
        clicked = 0
        try:  # 1) click every reply expander (JS .click reaches off-screen ones too)
            clicked = int(page.evaluate(r"""()=>{
              const re=%s, no=%s;
              const hit=(el)=>{const x=(el.innerText||'').trim();
                               return !!x && x.length<=20 && re.test(x) && !no.test(x);};
              let n=0;
              for(const el of document.querySelectorAll('span,a,div,button')){
                if(!hit(el)) continue;
                let inner=false;
                for(const ch of el.querySelectorAll('*')){ if(hit(ch)){inner=true;break;} }
                if(inner) continue;
                try{el.click();n++;}catch(e){}
                if(n>=30) break;
              }
              return n;
            }""" % (_EXPANDER_JS_RE, _COLLAPSER_JS_RE)) or 0)
        except Exception:  # noqa: BLE001
            pass
        stats["expander_clicks"] += clicked
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
            stats["stop"] = "page_error"
            break
        try:
            ended = bool(page.evaluate(r"""()=>/到底了|没有更多|已经到底|THE END/.test((document.body.innerText||'').slice(-500))""")) or ended
        except Exception:  # noqa: BLE001
            pass
        if n == prev:
            stable += 1
        else:
            stable, prev = 0, n
        # Robust stop: a LONG plateau (async lazy-load is non-deterministic, so confirm the
        # ceiling over several rounds — a transient single-round stall must not stop us early)
        # or an explicit end-marker for the top-level thread. We keep clicking expanders every
        # round regardless, so a late-appearing deep reply-chain still gets opened (and resets the
        # plateau counter). The page evaluates above have run the response listener, so ``lines``
        # holds every answer that landed during this round's settle.
        if not (ended or stable >= 6):
            continue
        open_replies = _lines_open(lines)[1]
        if not open_replies:
            stats["stop"] = "bottom_marker" if ended else "count_stable"
            break
        if was_ended and clicked == 0:
            stats["stop"] = "has_more_unclickable"
            break
        if stable >= 6:
            stats["stop"] = "count_stable"
            break
    stats["has_more_lines"] = _lines_open(lines)[0]
    return stats


class XiaohongshuAdapter(SourceBase):
    name = "xiaohongshu"
    needs_credentials = False  # Login via VNC into persistent Chrome
    explicit_only = "sealed / CDP (isolated 9223 Chrome, account-rate-sensitive)"
    description = "小红书: first-hand PhD daily life + real experience sharing (CDP session)"
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
        if cache.cache_only():
            # A cache-only collect that missed is a miss, not a browser failure: stop BEFORE the
            # live slot. Before 2026-10-07 the miss went on into cdp_call, which raised
            # CacheOnlyMiss, and the except below counted it as a CDP failure; three such
            # collects in 20s tripped the 6h 小号 backoff (2026-10-07 17:45 CST).
            diag.note("xiaohongshu.cache_only", body="cache-only collect: no cached result yet (no live call, not a query miss)")
            return []

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
        # The search boxes clicked and one record per search/notes answer (v1 and v2); the flow notes
        # them with the page's state when the search does not come back clean (_note_scene). The
        # response listener only records here and the flow notes, because the note needs the page's
        # state (a listener itself runs in the caller's context since 2026-11-07, see _cdp).
        boxes: list = []
        responses: list = []
        _v2_seen: list = []

        def _flow(page) -> tuple[str, Optional[str]]:
            result = None
            try:
                result = _flow_steps(page)
                return result
            finally:
                # An answer held note items this adapter cannot read (no id, or no note_card object):
                # noted even when other items made results, so a schema change shows up before it
                # empties them.
                for rec in list(responses):
                    if rec.get("unparsed"):
                        diag.note("xiaohongshu.search_notes_unparsed", status=rec.get("status"),
                                  body=_dump(rec))
                # Note the scene unless the captured answers already make results: a clean search
                # leaves no diagnostic, so it pays for no extra reads (the AI-switch read runs in the
                # page's own world).
                if not (result and result[0] == "ok" and _USE_XHR_CAPTURE
                        and any(_json_item_to_document(it) for it in _xhr_items)):
                    _note_scene(page, boxes, responses)

        def _flow_steps(page) -> tuple[str, Optional[str]]:
            def _on_resp(resp):
                try:
                    version = _search_api_version(resp.url or "")
                    if version is None or resp.request.method == "OPTIONS":
                        return
                    if version == 2:
                        _v2_seen.append(True)
                    try:
                        payload = resp.json()
                    except Exception as exc:  # noqa: BLE001 (recorded, the search goes on)
                        payload = None
                        err = repr(exc)[:80]
                    else:
                        err = None
                    items, rec = _read_search_answer(version, resp.status, payload)
                    if err:
                        rec["json_error"] = err
                    if len(responses) < _MAX_SCENE_RESPONSES:
                        responses.append(rec)
                    if _USE_XHR_CAPTURE:
                        _xhr_items.extend(items)
                except Exception:  # noqa: BLE001 — one unparseable XHR never breaks the search
                    pass
            page.on("response", _on_resp)

            # 1) Homepage first (NOT a direct search-URL jump), then dwell. A dropped connection is
            #    retried on the same page and exit (_cdp.goto_retrying, which notes each retry); see
            #    _NAV_BUDGET_S.
            _cdp.goto_retrying(page, HOME_URL, timeout=30000, budget_s=_NAV_BUDGET_S)
            _human.read_dwell()
            if _login_wall(page):
                return ("login", None)
            # 2) Locate the search box; human mouse-move + click + type + Enter.
            box, sel = _find_search_box(page)
            if box is None:
                logger.warning("xiaohongshu: search box not found on homepage")
                return ("nobox", None)
            boxes.append(_box_record(sel, box))
            # Human mouse path (behavioral) → reliable native click (focus) →
            # type → Enter. Coordinate-clicking 小红书's collapsed search bar can
            # miss/fail-to-focus, so the focus click is a native locator.click().
            _human.move_near(page, box)
            box.click(timeout=8000)
            _human.short_pause()
            _human.type_text(page, query)
            _human.action_pause()
            page.keyboard.press("Enter")
            # 3) Confirm navigation to the results page. Enter on the collapsed bar can leave us
            #    on /explore, whose recommendation feed renders the same section.note-item cards:
            #    parsed, that feed came back as confident results for a query it never ran
            #    (2026-10-14, "新加坡 周末" returned three unrelated feed notes and cached them for
            #    6h). So the landing is CHECKED, not merely waited for: one more Enter, and if the
            #    page is still not a results page the flow reports it and nothing is parsed.
            #    The AI layout's route (/search_result_ai) counts only once a v2 answer came in.
            v2_seen = lambda: bool(_v2_seen)  # noqa: E731
            if not _reached_results(page, v2_seen=v2_seen):
                box, sel = _find_search_box(page)
                if box is not None:
                    boxes.append(_box_record(sel, box))
                    box.click(timeout=8000)
                    _human.short_pause()
                    page.keyboard.press("Enter")
                if not _reached_results(page, v2_seen=v2_seen):
                    return ("nav", page.url or "")
            if _on_ai_results_route(page.url or ""):
                # The v2 answer already confirmed the results; this layout's cards and search box are
                # not the classic ones, so only a visible login overlay counts as a wall here (the
                # classic check reads a missing input#search-input as logged out).
                if _login_overlay(page):
                    return ("login", None)
            else:
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
            if not isinstance(exc, CacheOnlyMiss):  # a suppressed live call says nothing about Chrome
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
        if status == "nav":
            # html carries the URL the page stayed on. Not a query miss and not a login wall:
            # the search never ran, so there is nothing true to parse or to cache.
            diag.note("xiaohongshu.nav",
                      body=f"search never reached the results page (stayed on {html!r}); "
                           "nothing parsed or cached, this is NOT a query miss")
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

    async def afetch_url(self, url: str) -> Optional[Document]:
        host = urlparse(url).hostname or ""
        if not _SEALED and "rednote.com" not in host and not (
                host.endswith("xiaohongshu.com") and _mainland_sealed()):
            return None
        # the CDP read blocks (and while sealed, _read_url says so in the log, as before): a worker thread
        return await anyio.to_thread.run_sync(self._read_url, url, limiter=portal.IO_LANE)

    def _read_url(self, url: str) -> Optional[Document]:
        if _SEALED:
            logger.warning(_SEALED_MSG)
            return None
        host = urlparse(url).hostname or ""
        # URL ownership is explicit and decided in ONE place: the mainland adapter's _SEALED.
        # While the mainland 9224 source is sealed (2026-10-07), this adapter also owns
        # xiaohongshu.com note links, so every 小红书 read rides the international account.
        # Unsealed, xiaohongshu.com belongs to the 9224 adapter again (claiming both hosts then
        # made registration order decide which account read the note).
        if "rednote.com" not in host and not (
                host.endswith("xiaohongshu.com") and _mainland_sealed()):
            return None
        if cache.cache_only():
            # Same reason as in search(): a cache-only read never reaches the browser, so it
            # must not enter the live slot or count against the 小号.
            diag.note("xiaohongshu.cache_only", url=url, body="cache-only read: no live call")
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
        # This adapter owns rednote.com URLs, plus xiaohongshu.com URLs while the mainland 9224
        # adapter is sealed (see fetch_url). The 小号's login session lives on rednote.com only,
        # so a xiaohongshu.com NOTE link is navigated to https://www.rednote.com/explore/<id> with
        # its query string kept (xsec_token, xsec_source, ...). Read as given, xiaohongshu.com
        # showed this Chrome the GUEST view: first comment page only, and the reply expanders send
        # no request (2026-11-11, note 6af03ef1000000001a025bf5, 41 comments declared: the
        # xiaohongshu.com link read 19 = 10 main + 9 inline replies, 1 page request, 0 reply
        # requests after 28 expander clicks; the same id and token on rednote.com read all 41 =
        # 16 + 13 inline + 12 expanded, 2 page and 4 reply requests). The returned document keeps
        # the caller's url. _goto_note_dual_host still falls back rednote -> xiaohongshu when
        # rednote.com fails at the network layer; whether a read is a guest one is decided by the
        # host the page actually landed on (see _flow), never by the link passed in.
        parsed = urlparse(url)
        _id_m = re.search(r"/(?:explore|search_result|discovery/item)/([0-9a-f]{24})", parsed.path)
        nav_note_id = _id_m.group(1) if _id_m else None
        nav_url = url
        if nav_note_id and (parsed.hostname or "").endswith("xiaohongshu.com"):
            nav_url = f"https://www.rednote.com/explore/{nav_note_id}" + (f"?{parsed.query}" if parsed.query else "")

        _cmt: list = []  # captured /comment/page comments (the listener appends from the CDP thread;
        #                  cdp_call's join() flushes the writes before we read it below)
        _sub: list = []  # captured /comment/sub/page replies as (root_comment_id or None, reply)
        _cmt_seen = {"page": 0, "sub": 0}  # comment/page and comment/sub/page responses captured
        _lines: dict = {}  # has_more / cursor per line (_note_line), read by _load_comments while it runs
        _nav = {"landed": ""}  # a refused read's /404 address, written from the CDP thread like _cmt

        def _flow(page) -> tuple[str, Optional[str]]:
            if _USE_XHR_COMMENTS:
                def _on_cmt(resp):
                    try:
                        u = urlparse(resp.url or "")
                        if u.path == _CMT_PAGE_PATH:
                            _cmt_seen["page"] += 1
                            data = resp.json().get("data") or {}
                            if "has_more" in data:
                                _note_line(_lines, _MAIN_LINE, data["has_more"], data.get("cursor"), True)
                            for it in (data.get("comments") or []):
                                if isinstance(it, dict) and (it.get("content") or it.get("pictures")):
                                    _cmt.append(it)
                                if isinstance(it, dict) and it.get("id") and (
                                        "sub_comment_count" in it or "sub_comment_has_more" in it):
                                    # the page's own rule (rednote xhs-pc-web index.ec832f48.js,
                                    # 2026-11-11): a thread has more while sub_comment_count exceeds
                                    # the inline sub_comments
                                    if "sub_comment_count" in it:
                                        try:
                                            more = int(it.get("sub_comment_count") or 0) > len(it.get("sub_comments") or [])
                                        except (TypeError, ValueError):
                                            more = bool(it.get("sub_comment_has_more"))
                                    else:
                                        more = bool(it.get("sub_comment_has_more"))
                                    _note_line(_lines, it["id"], more, it.get("sub_comment_cursor"), False)
                        elif u.path == _CMT_SUB_PATH:
                            _cmt_seen["sub"] += 1
                            root = (parse_qs(u.query).get("root_comment_id") or [None])[0]
                            data = resp.json().get("data") or {}
                            if root and "has_more" in data:
                                _note_line(_lines, root, data["has_more"], data.get("cursor"), True)
                            for it in (data.get("comments") or []):
                                if isinstance(it, dict):
                                    _sub.append((root, it))
                    except Exception:  # noqa: BLE001 — one unparseable XHR never breaks the fetch
                        pass
                page.on("response", _on_cmt)
            # Attach BEFORE navigating: on a video note the player often gets a blob: URL,
            # and then the only place the real CDN URL ever appears is the response stream.
            seen_video = attach_video_sniffer(page)
            _goto_note_dual_host(page, nav_url)
            _human.read_dwell()
            # A refused link lands on /404 (see _refused_landing). Stop BEFORE the content gate:
            # the 404 page has no note body, and _login_wall reads a page with no real search box
            # as a wall, which would trip the 6h account backoff over one link the platform declined.
            landed = _refused_landing(page.url)
            if landed:
                _nav["landed"] = landed
                return ("refused", None, [], {}, (None, None))
            # A note page renders #detail-title / #detail-desc even when a login-NUDGE
            # overlay is shown to guests (share links with xsec_token are guest-readable).
            # Prefer the content: only call it a wall if the note body is genuinely absent,
            # else _login_wall false-positives on the overlay and drops a readable note.
            # A VIDEO note legitimately carries no body text, so a text-only gate reads it as
            # an empty page and _login_wall then drops a perfectly readable note. The PRESENCE
            # of a player is itself content: for those selectors, existence is the test.
            has_content = False
            try:
                for sel in ("#detail-title", "#detail-desc") + VIDEO_PLAYER_SELECTORS:
                    loc = page.locator(sel).first
                    if loc.count() <= 0:
                        continue
                    if sel in VIDEO_PLAYER_SELECTORS:
                        has_content = True
                        break
                    try:
                        if loc.inner_text().strip():
                            has_content = True
                            break
                    except Exception:  # noqa: BLE001 -- an unreadable node is not a wall
                        pass
            except Exception:  # noqa: BLE001
                has_content = False
            if not has_content and _login_wall(page):
                if (urlparse(page.url or "").hostname or "").endswith("xiaohongshu.com"):
                    # The 小号 session lives on rednote.com; a wall on a page that LANDED on
                    # xiaohongshu.com (the network-layer fallback, or a non-note link there) is the
                    # GUEST view of one link, so it must not trip the 6h account backoff.
                    return ("guest_wall", None, [], {}, (None, None))
                return ("login", None, [], {}, (None, None))
            _human.scroll_like_reading(page, screens=random.randint(1, 2))
            _human.read_dwell()
            # 小红书 puts the substance in CAROUSEL IMAGES, not always the text desc. The full list
            # comes from the page's own state (the __INITIAL_STATE__ script in the page HTML, read
            # after the flow); this DOM scan is the fallback for a page without it, split into body
            # and comment pictures. OmniSeek does not OCR; the consuming agent reads them with vision.
            try:
                split = page.evaluate(_DOM_IMAGES_JS) or {}
                images = list(split.get("body") or [])
                dom_comment_images = list(split.get("comment") or [])
            except Exception:  # noqa: BLE001
                images, dom_comment_images = [], []
            # DOM first; the wire sniffer is the answer for a blob:-fed player. Either way the
            # eye hands over a URL a transcriber can fetch, and never a process-local handle.
            video = video_with_origin(page, seen_video)  # (url, "dom"|"wire"|"unresolved")
            # Comments carry the 经验. MAX-COMPLETENESS path (completeness is non-negotiable): run
            # the EXHAUSTIVE _load_comments (scroll the comment container + click every reply
            # expander until stable) so the page loads ALL comments + replies, WHILE _on_cmt above
            # PASSIVELY captures the page's own signed comment/page and comment/sub/page responses
            # (structured JSON, immune to DOM virtualization). The two lists are then MERGED, not
            # ranked: the captured thread is the base, and any comment the page showed that the
            # capture lacks is slotted in after its neighbour (2026-11-11; keeping only the longer
            # list lost whatever the other one alone had). We forge nothing (the expander clicks
            # reveal only what a human reader would, same READ-ONLY contract).
            cdata = {"list": [], "declared": None, "dom_comment_images": dom_comment_images}
            dom_list: list = []
            load = {"rounds": 0, "expander_clicks": 0, "stop": "not_run", "has_more_lines": 0}
            try:
                load = _load_comments(page, lines=_lines)  # exhaustive: every expander + container scroll, bounded by its own caps
                dom_list = page.evaluate(_COMMENTS_JS) or []
                cdata["declared"] = page.evaluate(_DECLARED_JS)
            except Exception:  # noqa: BLE001
                pass
            if _USE_XHR_COMMENTS:
                cap_list, counts = _flatten_comment_thread(_cmt, _sub)
            else:
                cap_list, counts = [], {"main_comments": 0, "inline_replies": 0, "expanded_replies": 0}
            cdata["list"], merged = _merge_comment_rows(cap_list, dom_list)
            # Comment pictures ride on the captured comments.
            cdata["captured_images"] = [c for c in cap_list if c.get("images")]
            # Counts only, for whoever checks or repairs this read.
            cdata["harvest"] = {**counts,
                                "page_responses": _cmt_seen["page"], "sub_responses": _cmt_seen["sub"],
                                "dom_rows": len(dom_list), "merged_from_dom": merged,
                                "expander_clicks": load.get("expander_clicks", 0),
                                "rounds": load.get("rounds", 0), "stop": load.get("stop"),
                                "has_more_lines": load.get("has_more_lines", 0)}
            return ("ok", page.content(), images, cdata, video)

        try:
            # fast profile (operator-cleared); _load_comments keeps its OWN settle sleeps
            # (comment lazy-load window, NOT a human delay) so comment recall is unchanged.
            flow_result = cdp_call(
                _human.fast(_flow), initial_url=None, timeout=110, cdp_url=_XHS_CDP_URL)
            _note_cdp_result(True)
        except Exception as exc:  # noqa: BLE001
            if not isinstance(exc, CacheOnlyMiss):  # a suppressed live call says nothing about Chrome
                _note_cdp_result(False)  # sustained CDP failures trip backoff (don't hammer 小号)
            logger.warning("Xiaohongshu fetch_url failed: %s", exc)
            diag.note("xiaohongshu.cdp", exc=exc, body="CDP fetch_url flow raised (9223 Chrome wedged / timeout?)")
            return None

        # Unpacked OUTSIDE the CDP try on purpose. The shape of our own return tuple is OUR bug;
        # inside that try a ValueError here is charged to the browser -- it trips the 小号 backoff
        # and files a diagnostic blaming a wedged Chrome. Out here it surfaces as an adapter error
        # the fetcher records with a real traceback.
        status, html, images, cdata, video = flow_result
        video_url, video_src = video

        if status == "login":
            _trip_backoff("login wall during fetch_url")
            diag.note("xiaohongshu.login_wall",
                      body="小号 logged OUT of the 9223 CDP Chrome — re-login via VNC required (note body unreadable until then)")
            return None
        if status == "guest_wall":
            diag.note("xiaohongshu.guest_wall", url=url,
                      body="the page landed on xiaohongshu.com (rednote.com unreachable, or not a note "
                           "link) and was shown to a GUEST behind a login wall (the 小号 session lives "
                           "on rednote.com). Nothing charged to the 小号; re-find the note "
                           "with omniseek_search(sources=['xiaohongshu']) and read the rednote.com link it returns.")
            return None
        if status == "refused":
            # The platform's word on this link, not on the session: nothing is charged to the 小号.
            diag.note("xiaohongshu.refused", url=url, body=_refused_reason(_nav["landed"]))
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

        # The note's images: the page's own state when it has them (every slide, in order), else
        # the DOM scan. Comment pictures never enter media; they go to metadata.comment_media.
        # The state comes from the page HTML. The live read through page.evaluate this flow used to
        # make never saw window.__INITIAL_STATE__ (patchright's isolated world, see
        # _cdp.page_world_evaluate), so the HTML copy is the one that has always answered.
        state_note = _state_note_from_html(html, nav_note_id)
        state_video = bool(state_note) and state_note.get("type") == "video"
        state_images = _image_urls(state_note.get("imageList")) if state_note else []
        if state_images and not state_video:
            images, media_source = state_images, "initial_state"
        else:
            images, media_source = list(images or []), "dom"

        # Substance is often in the images: surface them (media) + flag it when the text is thin.
        comments = cdata.get("list") or []
        declared = cdata.get("declared")
        comment_media = _comment_media(
            list(cdata.get("captured_images") or []) + [c for c in comments if isinstance(c, dict)],
            cdata.get("dom_comment_images") or [], images)
        if not _detail_has_substance(title, body, images, comments):
            # NAME THE CAUSE. The old message ("navigation succeeded but returned nothing") is true
            # and useless: it tells the caller a page was blank, not why or what to do instead, so
            # a real caller spent a long session hunting the wrong layer and nearly patched the MCP
            # handler. The dominant cause is a URL with no xsec_token: 小红书 note pages are only
            # readable through the tokened SHARE link, and a bare /explore/<id> renders an empty
            # shell. Controlled A/B (2026-08-12, same tool, same session, minutes apart): the same
            # note id read fine WITH its token and produced exactly this branch WITHOUT it.
            # Reported as the LIKELY cause rather than a refusal: two observations plus the
            # documented mechanism justify pointing, not asserting an impossibility, and a hard
            # pre-flight refusal would remove capability the day the platform changes.
            if "xsec_token=" not in url:
                diag.note(
                    "xiaohongshu.missing_xsec_token", url=url,
                    body="note page returned an empty shell, and this URL carries no xsec_token. "
                         "小红书 note pages are readable only via the tokened share link; a bare "
                         "/explore/<id> renders nothing. Use the URL as returned by omniseek_search "
                         "(it already carries xsec_token), or append the token from a share link.")
                return None
            diag.note("xiaohongshu.empty_detail", url=url,
                      body="detail navigation succeeded but returned no title/body/media/comments "
                           "(the URL DOES carry an xsec_token, so this is not the token case: the "
                           "note may be deleted, private, or the session may have lost its login)")
            return None

        is_video = bool(video_url) or state_video or bool(soup.select_one("video, xg-video-player"))
        if is_video:
            content = content_with_video(body, video_url, has_player=True)
        elif images and len(body) < 200:
            note = (f"[正文主要在 {len(images)} 张图里(小红书常把干货放图中):图片 URL 见 media 字段,"
                    f"下载后用视觉读图中文字]")
            content = (body + "\n\n" + note) if body else note
        else:
            content = body or ""

        # Comments carry the crowd-sourced 经验, and they are returned ONCE: structured, in
        # metadata.comments. They used to ALSO be rendered into the body, so every note shipped its
        # whole comment thread twice; a 4-note omniseek_gather measured 68,106 chars, about half of it the
        # same text again, which overflowed the tool channel and effectively halved how many notes a
        # call could carry. Nothing is lost by dropping the inline copy: the structured list is
        # strictly richer (per-comment author / text / likes, machine-readable), and this path does
        # NOT feed the recall index (only adapter.search results are ingested), so comment text stays
        # exactly as searchable as before. The "取到 N / 共 M" completeness signal survives here,
        # because an incomplete harvest must stay VISIBLE.
        if comments:
            short = f" / 共 {declared} 条" if declared else ""
            content += (f"\n\n(评论 {len(comments)} 条{short},结构化在 metadata.comments:"
                        f" 每条含 author / text / likes"
                        + (f"; 评论里的 {len(comment_media)} 张图在 metadata.comment_media"
                           if comment_media else "") + ")")

        return Document(
            source="xiaohongshu",
            source_id=source_id,
            url=url,
            title=title,
            content=content,
            author=author,
            media=([video_url] + images) if video_url else images,
            metadata={"comments": comments, "comment_count": len(comments),
                      "comments_declared": declared, "comment_media": comment_media,
                      "comment_harvest": cdata.get("harvest") or {},
                      "media_source": media_source,
                      **video_metadata(video_url, video_src, has_player=is_video)},
        )

    def health_check(self) -> tuple[Optional[bool], str]:
        """Probe transport state only, without touching the account session.

        The old probe navigated the international homepage, waited like a human, and then
        inspected the login wall. That is a capability probe with a 60s CDP budget, while the
        source-health fan-out has a 25s hard deadline. It therefore reported ``timeout`` even
        when named search/read calls were healthy, and it added an unnecessary account touch to
        every health sweep. Named search/read remains the capability oracle; this probe only
        answers whether the isolated CDP transport is reachable. That transport is OUR browser,
        not xiaohongshu, so a reachable one is None (not verified), never True.
        """
        if _SEALED:
            return False, "SEALED (小红书封号风险): disabled until CDP is undetectable"
        cdp_ok, cdp_msg = cdp_health(_XHS_CDP_URL, ensure=True)
        if not cdp_ok:
            return False, f"CDP not reachable: {cdp_msg}"
        return None, "not probed (CDP reachable; named search/read is the content-capability probe)"

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
            content="",   # a card has no body; metadata.read_hint names what is missing
            author=author,
            signals=mk_signal('likes', score, kind='engagement', by='xiaohongshu/score'),
            # comment_count is None, NOT 0: this fallback path genuinely cannot see it (the rendered
            # card shows a like count only), and reporting 0 would read as "no discussion" on exactly
            # the notes worth opening. The JSON path above carries the real number; this one says so.
            metadata={"read_hint": _card_hint(None),   # the DOM card exposes only .count (likes)
                      "time_hint": time_hint, "like_count": score, "comment_count": None,
                      "body_needs_read": True},
        )


from omniseek.core.fetcher import register_adapter

register_adapter(XiaohongshuAdapter())
