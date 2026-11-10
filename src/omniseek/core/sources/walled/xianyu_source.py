"""闲鱼 (goofish.com): second-hand listings on sale, read through the operator's logged-in 闲鱼 小号 on 9222.

Why a walled source (EYE-XIANYU, 2026-10-24): goofish.com answers an anonymous search with
``RGV587_ERROR::SM`` and a passport login URL (the login wall; recorded in
the xianyu fixtures that TEST-CUT removed, still in git at f1da87a4 under tests/fixtures/xianyu/), so plain HTTP and web search see none of the live
listings, prices, seller credit or service tags. the operator asked for it (10-24 17:23:33).

How it reads (the acquisition choice, recon 2026-10-24 on xy-site 0.0.179):
  * The search page ``www.goofish.com/search?q=<kw>`` fires its own signed mtop call
    ``mtop.taobao.idlemtopsearch.pc.search`` (host h5api.m.goofish.com) as it loads. We listen to the
    page's responses and read that one: no signing code of ours, no second request for a plain query.
  * A filtered or paged query needs a body the page does not send on load, so after the page's own
    call came back SUCCESS we ask the page's OWN mtop client (``window.lib.mtop.request``, the same
    options its wrapper in p_search-index.js passes) to send one more search with that body, and read
    that response the same way. ``window.lib`` is a page global, so that call runs in the page's own
    JavaScript world (``page_world_evaluate``); in patchright's isolated world it is not there.
    Rendering and reading the DOM was the third option: it costs the same page load, loses fields
    the cards do not print, and breaks on every CSS rename.
  * The page also fires ``mtop.taobao.idlemessage.pc.loginuser.get`` on load; its answer
    (FAIL_SYS_SESSION_EXPIRED when nobody is logged in) tells a login wall from a risk-control stop.

Account safety (the 小号 is disposable, but must not look automated; spec 2026-10-24):
  * the upstream ``goofish`` in upstreams.json gates every call: one at a time, starts at least 5 s
    apart. The gate is held around the whole browser call (``upstreams.egress`` with waiting), not
    tried once at the browser turn, so back-to-back searches queue instead of failing; only this source
    uses the upstream, so holding it while waiting for a 9222 connection blocks nobody else.
  * at most ``_DAILY_REQ_CAP`` mtop searches a calendar day, health checks included, kept on disk.
  * any risk-control signal (``_RISK`` below) trips the breaker: 1 h, then 4 h, then 24 h for each
    trip in a row; while it is open nothing is sent. Kept on disk too, so a restart does not reopen it.
  * nothing here logs in, clicks the login box, solves a slider, orders, favourites or messages.

Sold prices: the web front end has no keyword-level sold list or price-trend call (recon: no such API
name in p_search-index / p_item-index / p_personal-index; 「我卖出的」 is disabled on the web and jumps
to the app, fleamarket://SoldItems). This source returns asking prices of listings on sale only.

Black box: a SUCCESS answer that parses to nothing, or an answer we do not recognise, is saved under
``<state>/xianyu/unparsed-<moment>.json`` (last 5 kept, each cut to 256 KB) and named in the
diagnostic, because the logged-in answer can only be fetched on the host machine (worker sandboxes cannot reach
9222) and the next repair should start from it.

Recon trail: brain note eye-recon-xianyu.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlsplit

from omniseek import where
from omniseek.core import _probe, cache, diag, upstreams
from omniseek.core import filters as _flt
from omniseek.core.normalize import Document, jsonsafe, mk_signal
from omniseek.core.sources.walled._base import EMPTY_TTL, BaseCDPAdapter
from omniseek.core.sources.walled._cdp import RateLimitedPage, cdp_call, cdp_health, page_world_evaluate

logger = logging.getLogger(__name__)

NAME = "xianyu"
_SEARCH_PAGE = "https://www.goofish.com/search?q="
_ITEM_URL = "https://www.goofish.com/item?id={}"
_API_SEARCH = "mtop.taobao.idlemtopsearch.pc.search"
_API_LOGIN = "mtop.taobao.idlemessage.pc.loginuser.get"
_APP_KEY = "34839810"   # the site's own mtop appKey, from its request wrapper (p_search-index.js, module 84229)
_ROWS_PER_PAGE = 30     # what the site asks for per page (rowsPerPage in the same wrapper)

# Calibrated values (provisional until 2026-11-07). The plan: two weeks after going live the driver
# reads <state>/xianyu/risk-events.jsonl; no trip in those two weeks keeps them, any trip doubles the
# gate's min_interval_s (upstreams.json, entry goofish) and halves this cap; the result goes into the
# brain note eye-recon-xianyu. Read 2026-11-07, live since 2026-10-24: the file was never written
# (0 trips), so all three stay. Only the day's count is kept (2 on 11-07, mostly the daily health
# check), so the reading says no trip at that load, not at a heavier one. _CALIBRATED stops the health
# line from saying the reading is due (see _calibration_line).
#   cap 100 a day: one build list is about 7 parts; for each, one search on sale plus one more page
#   (and later one for sold prices) is about 30 searches; three times that for slack.
_DAILY_REQ_CAP = 100
#   breaker 1 h / 4 h / 24 h: the ladder xiaohongshu_cn has used since 2026-06 on its 小号.
_COOLDOWN_LADDER = (3600, 14400, 86400)
_CALIBRATE_AFTER_DAYS = 14
_CALIBRATED = "2026-11-07"   # the reading above

_CDP_TIMEOUT = 60        # the whole browser call: page load 30 s + the page's search 20 s + own call
_SEARCH_WAIT_MS = 20000  # how long to wait for the page's own search answer after the page loaded
_OWN_CALL_MS = 15000     # our extra call: the mtop client's own timeout
_CACHE_TTL = 900
_PROBE_QUERY = "内存条"   # health check: a common part, so a working path always has listings
_BLACKBOX_KEEP = 5
_BLACKBOX_MAX_BYTES = 256 * 1024

# THE restore point: True -> every entry point inert (no browser, no request), named calls answer []
# with _SEALED_MSG, health reads "sealed", the sweep skips it (fetcher.sealed_reason).
_SEALED = False
_SEALED_MSG = "已封存（闲鱼源由源文件里的 _SEALED 关停）"

LOGIN_MSG = "闲鱼未登录：请在主机的 9222 浏览器里打开 goofish.com，用闲鱼小号登录"

_STATE_DIR = where.state() / "xianyu"
_lock = threading.Lock()


# ── durable state: the day's count, the breaker, the first live day ─────────────────────────────

def _state_path() -> Path:
    return _STATE_DIR / "state.json"


def _read_state() -> dict:
    try:
        row = json.loads(_state_path().read_text(encoding="utf-8"))
        return row if isinstance(row, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 — a broken file must not break retrieval; it is rewritten
        logger.warning("xianyu: state file unreadable (%s); starting from an empty state", exc)
        return {}


def _write_state(st: dict) -> None:
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        cache._atomic_write_text(_state_path(), json.dumps(st, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        logger.warning("xianyu: state not persisted (%s)", exc)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _spent_today(st: dict) -> int:
    return int(st.get("count") or 0) if st.get("date") == _today() else 0


def _take(n: int) -> bool:
    """Take ``n`` searches from today's budget; False (nothing taken) when they do not fit."""
    with _lock:
        st = _read_state()
        spent = _spent_today(st)
        if spent + n > _DAILY_REQ_CAP:
            return False
        st["date"], st["count"] = _today(), spent + n
        st.setdefault("first_live", _today())
        _write_state(st)
        return True


def _breaker() -> tuple[float, str, int]:
    """(seconds left, last signal, cooldown of that trip in seconds); 0 left = closed."""
    st = _read_state()
    left = float(st.get("tripped_until") or 0) - time.time()
    return max(0.0, left), str(st.get("last_signal") or ""), int(st.get("last_cooldown") or 0)


def _hours(seconds: float) -> int:
    return max(1, int(round(seconds / 3600.0)))


def _risk_msg(cooldown: int) -> str:
    return f"闲鱼要求人工验证，已停 {_hours(cooldown)} 小时"


def _trip(signal: str) -> int:
    """Open the breaker one rung further up the ladder; log the trip for the calibration reading."""
    with _lock:
        st = _read_state()
        streak = int(st.get("streak") or 0)
        cooldown = _COOLDOWN_LADDER[min(streak, len(_COOLDOWN_LADDER) - 1)]
        now = time.time()
        st.update(tripped_until=now + cooldown, streak=streak + 1, last_signal=signal,
                  last_cooldown=cooldown)
        _write_state(st)
        try:
            with (_STATE_DIR / "risk-events.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "signal": signal,
                                     "cooldown_s": cooldown, "streak": streak + 1},
                                    ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning("xianyu: risk event not logged (%s)", exc)
    logger.warning("xianyu 风控 breaker open for %ss: %s", cooldown, signal)
    return cooldown


def _clear_streak() -> None:
    with _lock:
        st = _read_state()
        if st.get("streak"):
            st["streak"] = 0
            _write_state(st)


def _calibration_line() -> str:
    st = _read_state()
    first = st.get("first_live")
    if not first:
        return "not live yet"
    try:
        days = (datetime.now() - datetime.strptime(first, "%Y-%m-%d")).days
    except ValueError:
        return f"first_live={first!r} unreadable"
    try:
        trips = sum(1 for line in (_STATE_DIR / "risk-events.jsonl").read_text(encoding="utf-8").splitlines()
                    if line.strip())
    except FileNotFoundError:
        trips = 0
    due = "; 暂定值校准到期，读 risk-events.jsonl" if days >= _CALIBRATE_AFTER_DAYS and not _CALIBRATED else ""
    return f"live {days} d since {first}, {trips} risk trips{due}"


# ── black box ────────────────────────────────────────────────────────────────────────────────────

def _blackbox(raw: str, why: str) -> str:
    """Save an answer we could not use; keep the newest _BLACKBOX_KEEP. Returns the path ('' on failure)."""
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        path = _STATE_DIR / f"unparsed-{stamp}.json"
        body = (raw or "").encode("utf-8")[:_BLACKBOX_MAX_BYTES].decode("utf-8", "ignore")
        cache._atomic_write_text(path, json.dumps({"why": why, "raw": body}, ensure_ascii=False))
        for old in sorted(_STATE_DIR.glob("unparsed-*.json"))[:-_BLACKBOX_KEEP]:
            old.unlink(missing_ok=True)  # the black box is this source's own rotating store
        return str(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("xianyu: black box not written (%s)", exc)
        return ""


# ── classifying what the page saw ────────────────────────────────────────────────────────────────

# mtop ret codes lib-mtop 2.7.3 itself treats as "log in again" (mtop.js, its session middleware).
_LOGIN_CODES = ("SESSION_EXPIRED", "SID_INVALID", "AUTH_REJECT", "NEED_LOGIN")
# Risk-control signals. Every one stops the source (breaker) until the cooldown ends:
#   RGV587_ERROR / ASSIST_FLAG with a punish URL: the baxia slider (lib-mtop's anti-creep branch);
#   RGV587_ERROR with a login URL while the session is valid: the platform asks to log in again;
#   FAIL_SYS_USER_VALIDATE: lib-mtop's anti-flood branch, which sends the browser to a verify page;
#   FAIL_SYS_ILLEGAL_ACCESS: the request was refused as illegal;
#   FAIL_SYS_FLOWLIMIT / HTTP 429: rate limited;
#   a visible baxia dialog / J_MIDDLEWARE_FRAME_WIDGET on a logged-in page, or the page itself sent
#   to a punish / _____tmd_____ page.
_RATE_CODES = ("FAIL_SYS_FLOWLIMIT",)
_OTHER_RISK_CODES = ("FAIL_SYS_USER_VALIDATE", "FAIL_SYS_ILLEGAL_ACCESS")


def _ret(payload: Any) -> str:
    ret = payload.get("ret") if isinstance(payload, dict) else None
    if isinstance(ret, list):
        return ",".join(str(x) for x in ret)
    return str(ret or "")


def _logged_in(cap: dict) -> Optional[bool]:
    """From the page's own loginuser.get answer: True / False / None (not seen)."""
    row = cap.get("login") or {}
    try:
        ret = _ret(json.loads(row.get("text") or ""))
    except (TypeError, ValueError):
        return None
    if "SUCCESS" in ret:
        return True
    if any(c in ret for c in _LOGIN_CODES):
        return False
    return None


# The search call answered with the Taobao login page itself (HTML, <title>登录</title>) instead of mtop JSON:
# seen live 2026-10-24 in the 9222 browser while logged out (recorded in a fixture TEST-CUT removed; git f1da87a4).
_HTML_LOGIN_RE = re.compile(r"<title>\s*登录\s*</title>|login\.taobao\.com|passport\.goofish\.com", re.I)


def _html_login_page(text: str) -> bool:
    head = (text or "").lstrip()[:4096]
    return head.startswith("<") and bool(_HTML_LOGIN_RE.search(head))


def _wall_kind(payload: dict) -> str:
    """For an RGV587 / ASSIST_FLAG answer: 'login' (a passport login URL) or 'slider' (anything else)."""
    url = str(((payload.get("data") or {}) if isinstance(payload.get("data"), dict) else {}).get("url") or "")
    parts = urlsplit(url)
    if parts.hostname and parts.hostname.startswith("passport.") and "login" in parts.path:
        return "login"
    return "slider"


def classify(cap: dict) -> tuple[str, str, Any]:
    """What one browser call saw -> (kind, detail, parsed payload or raw text).

    kind: ok (SUCCESS, payload parsed), login (nobody logged in), risk (any risk-control signal; detail
    names it), rate (rate limited, also a stop), unknown (an answer we do not recognise), none (no search
    answer at all). Pure: the unit tests feed it the recorded answers."""
    logged = _logged_in(cap)
    page_url = str(cap.get("page_url") or "")
    if "/punish" in page_url or "_____tmd_____" in page_url:
        return "risk", f"page sent to a verify page ({urlsplit(page_url).path})", None
    rows = list(cap.get("search") or [])
    if cap.get("own_from") is not None:
        # a filtered / paged search: only the answer to OUR call counts, never the page's own default
        rows = rows[int(cap["own_from"]):]
    if not rows:
        if logged is False:
            return "login", "loginuser.get: session expired, the page sent no search", None
        return "none", "the page sent no search call", None
    row = rows[-1]
    if row.get("status") == 429:
        return "rate", "HTTP 429 from h5api.m.goofish.com", None
    text = row.get("text") or ""
    try:
        payload = json.loads(text)
    except ValueError:
        if _html_login_page(text):
            if logged:
                return "risk", "logged in, but the search answer is the login page (HTML)", None
            return "login", "search answer is the login page (HTML)", None
        return "unknown", "search answer is not JSON", text
    ret = _ret(payload)
    dom = cap.get("dom") or {}
    if "SUCCESS" in ret:
        if logged and (dom.get("baxia") or dom.get("middleware")):
            return "risk", "verification dialog on the page (baxia-dialog / J_MIDDLEWARE_FRAME_WIDGET)", None
        return "ok", "", payload
    if any(c in ret for c in _LOGIN_CODES):
        return "login", f"search ret {ret[:80]}", None
    if "RGV587_ERROR" in ret or "ASSIST_FLAG" in ret:
        if _wall_kind(payload) == "login":
            if logged:
                return "risk", "logged in, but the platform asks to log in again (RGV587 + passport login)", None
            return "login", "RGV587 login wall (not logged in)", None
        return "risk", f"slider / verification (RGV587, {ret[:60]})", None
    if any(c in ret for c in _RATE_CODES):
        return "rate", f"rate limited ({ret[:60]})", None
    for c in _OTHER_RISK_CODES:
        if c in ret:
            return "risk", f"{c} ({ret[:60]})", None
    return "unknown", f"search ret {ret[:80]!r} not recognised", text


# ── decoding listings ────────────────────────────────────────────────────────────────────────────

# img tags carry an icon name; the text each icon shows was read off the icon images (recon 10-24).
_ICON_TEXT = {"freeShippingIcon": "包邮", "sevenIcon": "7天无理由退货", "nfrIcon": "描述不符包邮退"}
_CONDITION_RE = re.compile(r"全新|几乎全新|准新|未使用|\d{2}新|[\d一二三四五六七八九]成新|使用痕迹")
_LEVEL_RE = re.compile(r"^level(\d+)$")
_MONEY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(万)?")
_STATUS = {0: "在售", 1: "已卖出", -2: "已下架"}  # the site's own enum (p_personal-index.js: vaild/sold/offShelf)


def _money(text: Any) -> Optional[float]:
    m = _MONEY_RE.search(str(text or "").replace(",", ""))
    if not m:
        return None
    v = float(m.group(1))
    return v * 10000 if m.group(2) else v


def _price(parts: Any) -> Optional[float]:
    """The search card's price: a list of typed parts (sign / integer / decimal / unit / suffix)."""
    if isinstance(parts, (int, float)) and not isinstance(parts, bool):
        return float(parts)
    if not isinstance(parts, list):
        return _money(parts)
    by = {}
    for p in parts:
        if isinstance(p, dict):
            by.setdefault(p.get("type"), str(p.get("text") or ""))
    if by.get("integer"):
        dec = by.get("decimal") or ""
        num = by["integer"] + (dec if not dec or dec.startswith(".") else "." + dec)
        v = _money(num)
        if v is not None and (by.get("unit") or "").strip() == "万":
            v *= 10000
        return v
    return _money("".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)))


def _tag_rows(fish: Any) -> dict:
    """fishTags {r1..r5: {tagList: [{data: {type, content}}]}} -> {row: [(type, content)]}."""
    out: dict = {}
    if not isinstance(fish, dict):
        return out
    for row, v in fish.items():
        for t in ((v or {}).get("tagList") or []) if isinstance(v, dict) else []:
            d = (t or {}).get("data") or {}
            if d.get("content"):
                out.setdefault(row, []).append((str(d.get("type") or ""), str(d["content"])))
    return out


def decode_tags(fish: Any) -> dict:
    """The card tags, read the way the site's own card renderer reads them (p_search-index.js: r1 title
    icon, r2 property / condition, r3 price decoration, r4 seller credit, r5 service). Shared by the
    search cards and the recommend-feed cards (same fishTags system)."""
    rows = _tag_rows(fish)
    out: dict = {"condition": None, "seller_credit": None, "shop_level": None, "service_tags": [],
                 "wants": None, "texts": []}
    for row, tags in sorted(rows.items()):
        for typ, content in tags:
            if typ != "img":
                out["texts"].append(content)
            if row == "r2" and typ != "img" and out["condition"] is None and _CONDITION_RE.search(content):
                out["condition"] = content
            elif row == "r4":
                lv = _LEVEL_RE.match(content)
                if typ == "img" and lv:
                    out["shop_level"] = int(lv.group(1))
                elif typ != "img" and "信用" in content:
                    out["seller_credit"] = content
            elif row == "r3":
                m = re.match(r"^(\d+)人想要$", content)
                if m:
                    out["wants"] = int(m.group(1))
            elif row in ("r1", "r5"):
                tag = _ICON_TEXT.get(content, content) if typ == "img" else content
                if tag not in out["service_tags"]:
                    out["service_tags"].append(tag)
    return out


def _date_ms(v: Any) -> Optional[datetime]:
    s = str(v or "")
    if not s.isdigit():
        return None
    try:
        return datetime.fromtimestamp(int(s) / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _status(v: Any) -> Optional[str]:
    try:
        return _STATUS.get(int(v)) if v is not None and str(v) != "" else None
    except (TypeError, ValueError):
        return None


def item_doc(item: dict, *, personal: bool = False) -> Optional[Document]:
    """One search result -> Document. Two card shapes, both read by the site's own search page:
    data.item.main.exContent + clickParam.args (the usual one), and a flat data {id, title, price, ...}."""
    d = (item or {}).get("data") or {}
    main = (d.get("item") or {}).get("main") if isinstance(d.get("item"), dict) else None
    if isinstance(main, dict):
        ex = main.get("exContent") or {}
        args = (main.get("clickParam") or {}).get("args") or {}
        item_id = ex.get("itemId")
        title = ex.get("title")
        price = _price(ex.get("price"))
        area = ex.get("area")
        nick = ex.get("userNickName")
        pic = ex.get("picUrl")
        fish = ex.get("fishTags")
        ori = ex.get("oriPrice")
        status = _status(ex.get("itemStatus", args.get("itemStatus")))
        when = _date_ms(args.get("publishTime"))
        # args.tag is a "/" list ("freeship/credit-5"). args.wantNum is "0" on every logged-in search
        # card (60 of 60 on 10-24), so the wants count comes from the 「N人想要」 tag alone.
        freeship = "freeship" in str(args.get("tag") or "").split("/")
    elif d.get("id"):
        ex, args = d, {}
        item_id, title = d.get("id"), d.get("title")
        price = _money(f"{d.get('price') or ''}{d.get('priceUnit') or ''}")
        area, nick, pic, fish = d.get("city"), None, d.get("picUrl"), d.get("fishTags")
        ori, status, when, freeship = d.get("oriPrice"), _status(d.get("itemStatus")), None, False
    else:
        return None
    if not item_id or not title:
        return None
    tags = decode_tags(fish)
    if freeship and "包邮" not in tags["service_tags"]:
        tags["service_tags"].append("包邮")
    if tags["shop_level"] is not None:
        kind = "鱼小铺"
    elif personal:
        kind = "个人"     # the search itself was limited to 个人闲置
    else:
        kind = None
    meta: dict = {"seller_kind": kind}
    for k, v in (("price", price), ("original_price", _money(ori) if ori else None),
                 ("condition", tags["condition"]), ("seller_nick", nick), ("shop_level", tags["shop_level"]),
                 ("seller_credit", tags["seller_credit"]), ("area", area),
                 ("service_tags", tags["service_tags"] or None), ("status", status)):
        if v not in (None, ""):
            meta[k] = v
    meta["raw"] = jsonsafe(item)
    pic_url = ("https:" + pic) if isinstance(pic, str) and pic.startswith("//") else pic
    return Document(
        source=NAME, source_id=str(item_id), url=_ITEM_URL.format(item_id), title=str(title),
        content="", author=nick or None, date=when,
        signals=mk_signal("wants", tags["wants"], kind="engagement", by="goofish/fishTags"),
        tags=["闲鱼"] + [t for t in tags["texts"] if t not in ("闲鱼",)],
        media=[pic_url] if pic_url else [], metadata=meta,
    )


def parse(payload: dict, limit: int, *, personal: bool = False) -> tuple[list[Document], Optional[int]]:
    """SUCCESS payload -> (docs, number of result rows); rows is None when the payload has no result list
    at all (a structure we do not know: the caller sends it to the black box)."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("resultList"), list):
        return [], None
    rows = data["resultList"]
    docs: list[Document] = []
    for it in rows:
        try:
            doc = item_doc(it, personal=personal)
        except Exception as exc:  # noqa: BLE001 — one odd card must not sink the page
            logger.debug("xianyu: card skipped (%s)", exc)
            doc = None
        if doc is not None:
            docs.append(doc)
        if len(docs) >= limit:
            break
    return docs, len(rows)


# ── the browser flow ─────────────────────────────────────────────────────────────────────────────

_JS_OWN_CALL = """async ({data, waitMs, api, appKey}) => {
    const m = window.lib && window.lib.mtop;
    if (!m || !m.request) return 'no_mtop';
    const p = m.request({api: api, v: '1.0', type: 'POST', dataType: 'json', appKey: appKey,
        accountSite: 'xianyu', timeout: waitMs, needLogin: false, needLoginPC: false,
        showErrorToast: false, sessionOption: 'AutoLoginOnly', ecode: 0, data: data});
    try { await Promise.race([p, new Promise(r => setTimeout(r, waitMs))]); } catch (e) {}
    return 'sent';
}"""

_JS_DOM = """() => ({baxia: !!document.querySelector('div.baxia-dialog-mask'),
                    middleware: !!document.querySelector('div.J_MIDDLEWARE_FRAME_WIDGET')})"""


def _wait(page, pred, ms: int) -> bool:
    end = time.monotonic() + ms / 1000.0
    while not pred():
        if time.monotonic() > end:
            return False
        page.wait_for_timeout(200)  # responses are dispatched to the listener while Playwright waits
    return True


def _make_flow(url: str, own_data: Optional[dict]):
    def flow(page):
        cap: dict = {"search": [], "login": None, "page_url": "", "dom": {}, "own": None}

        def on_resp(r):
            try:
                u = r.url or ""
                if f"/h5/{_API_SEARCH}/" in u:
                    cap["search"].append({"status": r.status, "text": r.text()})
                elif f"/h5/{_API_LOGIN}/" in u:
                    cap["login"] = {"status": r.status, "text": r.text()}
            except Exception:  # noqa: BLE001 — a body that cannot be read stays out; classify sees none
                pass

        page.on("response", on_resp)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            _wait(page, lambda: bool(cap["search"]), _SEARCH_WAIT_MS)
            first_ok = bool(cap["search"]) and '"SUCCESS' in (cap["search"][0].get("text") or "")
            if own_data is not None and first_ok:
                if _take(1):
                    n = len(cap["search"])
                    cap["own_from"] = n
                    cap["own"] = page_world_evaluate(page, _JS_OWN_CALL, {
                        "data": own_data, "waitMs": _OWN_CALL_MS, "api": _API_SEARCH, "appKey": _APP_KEY})
                    _wait(page, lambda: len(cap["search"]) > n, 3000)
                else:
                    cap["own"] = "capped"
            if cap["login"] is None:
                _wait(page, lambda: cap["login"] is not None, 3000)
            cap["page_url"] = page.url
            try:
                cap["dom"] = page.evaluate(_JS_DOM) or {}
            except Exception:  # noqa: BLE001
                cap["dom"] = {}
        finally:
            try:
                page.remove_listener("response", on_resp)
            except Exception:  # noqa: BLE001
                pass
        return cap
    return flow


# ── filters (only what the site's own search sends; p_search-index.js, function S) ──────────────

_SORTS = {"综合": ("", ""), "新发布": ("desc", "create"),
          "价格从低到高": ("asc", "price"), "价格从高到低": ("desc", "price")}
_QUICK = {"personal": "filterPersonal", "appraise": "filterAppraise", "brand_new": "filterNew",
          "free_shipping": "filterFreePostage"}


def _num_text(v: Any) -> str:
    if v is None:
        return ""
    f = float(v)
    return str(int(f)) if f == int(f) else str(f)


def request_body(keyword: str, params: dict) -> dict:
    """The search body the site sends for these filters (mirrors its own builder, field by field)."""
    sort_value, sort_field = _SORTS.get(params.get("sort") or "综合", ("", ""))
    sf = ""
    if params.get("price"):
        lo, hi = params["price"]
        # the site writes an open upper bound as the JS value undefined, so do we
        sf += f"priceRange:{_num_text(lo)},{_num_text(hi) if hi is not None else 'undefined'};"
    quick = [v for k, v in _QUICK.items() if params.get(k)]
    if quick:
        sf += "quickFilter:" + ",".join(quick) + ";"
    from_filter = bool(sf or sort_field)
    return {"pageNumber": int(params.get("page") or 1), "keyword": keyword, "fromFilter": from_filter,
            "rowsPerPage": _ROWS_PER_PAGE, "sortValue": sort_value, "sortField": sort_field,
            "customDistance": "", "gps": "", "propValueStr": {"searchFilter": sf}, "customGps": "",
            "searchReqFromPage": "pcSearch", "extraFilterValue": "{}", "userPositionJson": "{}"}


class XianyuAdapter(BaseCDPAdapter):
    name = NAME
    description = ("闲鱼 (goofish.com) 二手在售列表：按关键词搜在售商品，每条带挂牌价、原价、成色、卖家昵称、"
                   "鱼小铺或个人、卖家信用、所在地、服务标签（包邮、7天无理由退货、验货宝等）、多少人想要、链接。"
                   "只有在售挂牌价，没有成交价（网页版没有已卖出列表或行情接口）。走主机 9222 浏览器里登录的闲鱼小号，"
                   "一次一个、两次至少隔 5 秒、每天至多 100 次；没登录或要人工验证时回空并在诊断里写明。"
                   "筛选：price（区间）、personal、appraise（验货宝）、brand_new（全新）、free_shipping（包邮）、"
                   "sort（综合 / 新发布 / 价格从低到高 / 价格从高到低）、page（每页 30 条）。"
                   "按价格排时网站不管相关性：把关键词宽松匹配到的整批（多词查询里带任一个词的都算）按价排，"
                   "前面多是擦边的便宜货；要相关的里面最便宜的，用 price 区间加综合排序，拿回来自己按 metadata.price 排、"
                   "要更多就翻页。")
    explicit_only = ("logged-in 闲鱼小号 on the shared 9222 Chrome, one request at a time (upstream gate "
                     "goofish) with a daily cap and a risk-control breaker: never in the broad sweep")
    kind = "lookup"
    domains = ["shopping"]
    regions = ["cn"]
    modes = ["STRUCTURE", "UNWALL"]
    fetch_timeout = 100.0   # above the browser call's 60 s plus the gate wait
    cache_ttl = _CACHE_TTL
    url_host = ""           # search only: an item page is read by omniseek_read's generic path
    license = "upstreams:goofish"
    filters = {
        "price": {"type": "number", "ops": ["gte", "lte", "range"], "doc_key": "metadata.price",
                  "desc": "挂牌价区间，元（网站的 priceRange）"},
        "personal": {"type": "bool", "ops": ["eq"], "desc": "只看个人闲置（filterPersonal）"},
        "appraise": {"type": "bool", "ops": ["eq"], "desc": "只看验货宝（filterAppraise）"},
        "brand_new": {"type": "bool", "ops": ["eq"], "desc": "只看全新（filterNew）"},
        "free_shipping": {"type": "bool", "ops": ["eq"], "desc": "只看包邮（filterFreePostage）"},
        "sort": {"type": "keyword", "ops": ["eq"], "values": list(_SORTS),
                 "desc": "排序：综合、新发布、价格从低到高、价格从高到低。按价格排不管相关性（网站把宽松匹配到的整批"
                         "按价排，多词查询前面多是擦边货）；找相关的里面最便宜的，用 price 区间加综合，结果自己按价排"},
        "page": {"type": "int", "ops": ["eq"], "desc": "第几页（每页 30 条，从 1 起）"},
    }
    coverage = {"since": None, "until": None, "gaps": ["只有在售挂牌价，没有成交价"],
                "note": "当前在售的挂牌；网站搜索只给当下的列表"}
    samples: list = []      # none on purpose: a sample run is an account touch

    @property
    def sealed(self) -> str:
        return _SEALED_MSG if _SEALED else ""

    # BaseCDPAdapter hooks: this source drives its own flow (it must listen before the page loads).
    def _search_url(self, query: str) -> str:
        return _SEARCH_PAGE + quote(query or "", safe="")

    def translate_filters(self, query: str, clauses: list):
        params: dict = {}
        for c in clauses:
            if c.field == "price":
                lo, hi = (c.value, c.value) if c.op == "eq" else (c.lo, c.hi)
                params["price"] = (lo, hi)
            elif c.field in _QUICK:
                if c.value:
                    params[c.field] = True
                else:
                    raise _flt.Unsupported(c.field, "the site can only narrow to it, not exclude it")
            elif c.field == "sort":
                params["sort"] = c.value
            elif c.field == "page":
                if int(c.value) < 1:
                    raise _flt.Unsupported("page", "pages start at 1")
                params["page"] = int(c.value)
            else:
                raise _flt.Unsupported(c.field)
        return query or "", params

    def _run_search(self, query: str, limit: int, params: dict) -> tuple[str, list[Document], str]:
        """One live search -> (status, docs, detail). status: ok, empty, login, risk, rate, unknown,
        capped, breaker, busy, error, sealed. Notes the diagnostic; never raises."""
        url = self._search_url(query)
        if _SEALED:
            diag.note(f"{NAME}.sealed", body=_SEALED_MSG)
            return "sealed", [], _SEALED_MSG
        left, signal, cooldown = _breaker()
        if left > 0:
            msg = f"{_risk_msg(cooldown)}（{signal}；还剩 {left / 3600:.1f} 小时，期间不发任何请求）"
            diag.note(f"{NAME}.breaker", url=url, body=msg)
            return "breaker", [], msg
        body = request_body(query, params)
        own = body if body != request_body(query, {}) else None   # the page's own load already asks the default
        if not _take(1):
            msg = f"闲鱼今天的 {_DAILY_REQ_CAP} 次请求已用完（暂定上限，含健康检查），明天再查；没发任何请求"
            diag.note(f"{NAME}.daily_cap", url=url, body=msg)
            return "capped", [], msg
        try:
            with upstreams.egress(url, browser=True, request_s=_CDP_TIMEOUT):
                cap = cdp_call(_make_flow(url, own), initial_url=None, timeout=_CDP_TIMEOUT)
        except upstreams.UpstreamBusy as exc:
            diag.note(f"{NAME}.gate", url=url, exc=exc)
            return "busy", [], str(exc)
        except RateLimitedPage as exc:
            cooldown = _trip(f"HTTP 429 on the search page: {exc}")
            msg = f"{_risk_msg(cooldown)}（search page answered HTTP 429）"
            diag.note(f"{NAME}.risk", url=url, body=msg)
            return "rate", [], str(exc)
        except Exception as exc:  # noqa: BLE001 — browser failure: empty by contract, said out loud
            logger.warning("xianyu browser call failed: %s", exc)
            diag.note(f"{NAME}.cdp", url=url, exc=exc)
            return "error", [], f"{type(exc).__name__}: {str(exc)[:120]}"
        if cap.get("own") == "capped":
            msg = f"闲鱼今天的 {_DAILY_REQ_CAP} 次请求已用完：筛选那一次没发"
            diag.note(f"{NAME}.daily_cap", url=url, body=msg)
            return "capped", [], msg
        kind, detail, payload = classify(cap)
        if kind == "login":
            diag.note(f"{NAME}.login", url=url, body=LOGIN_MSG)
            return "login", [], LOGIN_MSG + f"（{detail}）"
        if kind in ("risk", "rate"):
            cooldown = _trip(detail)
            msg = f"{_risk_msg(cooldown)}（{detail}）"
            diag.note(f"{NAME}.risk", url=url, body=msg)
            return kind, [], msg
        if kind == "none":
            diag.note(f"{NAME}.no_answer", url=url, body=f"闲鱼页面没发出搜索请求：{detail}；不是“没有结果”")
            return "error", [], detail
        if kind == "unknown":
            path = _blackbox(payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
                             detail)
            msg = f"闲鱼返回认不出：{detail}；原始返回存在 {path or '（没存下）'}"
            diag.note(f"{NAME}.unparsed", url=url, body=msg)
            return "unknown", [], msg
        docs, rows = parse(payload, limit, personal=bool(params.get("personal")))
        if rows is None or (rows and not docs):
            why = "SUCCESS without a result list" if rows is None else f"{rows} rows, none decoded"
            path = _blackbox(json.dumps(payload, ensure_ascii=False), why)
            msg = f"闲鱼返回解析失败：{why}；原始返回存在 {path or '（没存下）'}"
            diag.note(f"{NAME}.unparsed", url=url, body=msg)
            return "unknown", [], msg
        _clear_streak()
        return ("ok" if docs else "empty"), docs, ""

    def search(self, query: str, limit: int = 10) -> list[Document]:
        q = (query or "").strip()
        if not q:
            return []
        limit = max(1, min(int(limit or 10), _ROWS_PER_PAGE))
        params = _flt.upstream()
        key = cache.make_key(NAME, "search", q, limit)
        cached = cache.get_docs(key)
        if cached is not None:
            return cached
        status, docs, _detail = self._run_search(q, limit, params)
        if status in ("ok", "empty"):
            cache.set_docs(key, docs, ttl=self.cache_ttl if docs else EMPTY_TTL)
        return docs

    async def afetch_url(self, url: str) -> Optional[Document]:
        return None

    def health_check(self) -> tuple[Optional[bool], str]:
        """Asks the search path once (limit 1) through the logged-in browser: True when it serves,
        False when verified unusable (not logged in, a verification wall met by this check, an answer
        we cannot parse, the browser down), None when nothing was asked (sealed, breaker open, today's
        cap used, the gate busy, the answer cached) or the site rate-limited the check."""
        if _SEALED:
            return None, f"sealed: {_SEALED_MSG}"
        cal = _calibration_line()
        left, signal, cooldown = _breaker()
        if left > 0:
            return None, _probe.breaker_open(f"{_risk_msg(cooldown)}: {signal}", left) + f" [{cal}]"
        st = _read_state()
        if _spent_today(st) >= _DAILY_REQ_CAP:
            return None, f"not verified: today's {_DAILY_REQ_CAP} searches are used up; no request sent [{cal}]"
        ok, msg = cdp_health(self.cdp_url, ensure=True)
        if not ok:
            return None, f"CDP not reachable: {msg}"
        if cache.get(cache.make_key(NAME, "search", _PROBE_QUERY, 1)) is not None:
            return None, "not probed (the probe search is still cached; goofish is asked again when it expires)"
        status, docs, detail = self._run_search(_PROBE_QUERY, 1, {})
        if status in ("ok", "empty"):
            if docs:
                cache.set_docs(cache.make_key(NAME, "search", _PROBE_QUERY, 1), docs, ttl=self.cache_ttl)
            return True, f"OK ({len(docs)} listing via the logged-in 9222 browser; " \
                         f"today {_spent_today(_read_state())}/{_DAILY_REQ_CAP}) [{cal}]"
        if status == "rate":
            return None, _probe.rate_limited("h5api.m.goofish.com") + f"; {detail}"
        if status in ("busy", "breaker", "capped", "sealed"):
            return None, f"not verified: {detail}"
        return False, detail
