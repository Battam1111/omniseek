"""Transplanted infra JOBS (P9): the movable services that used to be standalone launchd crons,
now zero-arg job fns the in-process scheduler (penumbra.core.jobs) runs on their declared schedules.

WHY THEY MOVED (the derived architecture): the test for what stays OUTSIDE the organ is "must this
still run when the organ is dead?" These do NOT -- a source-health probe, a wewe-rss feed check, an
xhs session warm, log rotation, the monthly curator pass, the weekly source audit, the weekly
digest are all self-maintenance of a LIVE eye. So each old script's LOOP body becomes a job fn here;
its hard-won lessons (the consecutive-fail thresholds, the cooldowns, the state-file formats, the
safety rationales) are transplanted verbatim, because those lessons are what kept the fleet quiet
and safe. The push path changes from scripts/_sentinel_common (urllib, out-of-process) to
penumbra.core.notify (httpx, in-process), keeping the same fail-open contract.

Each job runs INSIDE the writer process on the ONE scheduler thread, wrapped by jobs.run_due_jobs in
its own try/except (a failing job never stops the rest, and the scheduler Barks on an unhandled
exception with a 24h cooldown). The state files these jobs keep are the SAME paths the old crons
used, so the mini's existing state carries across the migration unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

log = logging.getLogger(__name__)

_STATE = Path.home() / ".penumbra" / "state"
_LOG_DIR = Path.home() / ".penumbra" / "logs"


# ── tiny local state helpers (the _sentinel_common load/save idiom, atomic) ───────────────────────
# In-process we cannot import scripts/_sentinel_common (it is the sentinel's isolated module and is
# not on the src path). These mirror its atomic load_state/save_state/should_alert so the state-file
# FORMATS + cooldown semantics the old crons relied on are preserved byte-for-byte.

def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 -- a corrupt state file quarantines + degrades to empty
        try:
            path.replace(path.with_suffix(path.suffix + ".corrupt"))
            log.warning("state file corrupt -> quarantined %s: %s", path.name, exc)
        except Exception:  # noqa: BLE001
            pass
        return {}


def _save_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)  # atomic: a kill mid-write can never truncate the real state file
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _should_alert(key: str, alerts: dict, cooldown: int) -> bool:
    """True at most once per ``cooldown`` seconds for ``key`` (mutates ``alerts``). The
    _sentinel_common gate, transplanted."""
    if time.time() - alerts.get(key, 0) < cooldown:
        return False
    alerts[key] = time.time()
    return True


def _bark(title: str, body: str, *, group: str = "Penumbra", level: str = "active") -> None:
    """Fail-open in-process Bark (via notify). The old crons pushed through _sentinel_common; the
    contract (never raise) is identical, including the level hint: health alarms stay "active",
    the periodic report pushes (digest / audit / curator) keep their old quiet "passive" lane."""
    try:
        from penumbra.core import notify
        notify.bark_push(title, body, group=group, level=level)
    except Exception as exc:  # noqa: BLE001 -- a push failure never breaks a job
        log.debug("infra_jobs bark swallowed (%s)", exc)


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# JOB: source-health probe   (transplanted from cron_watchdog.py 源体检 half; daily@05:00)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# The health watchdog detects SILENT source decay: an API deprecates a field, a site changes its
# HTML, a feed 404s, or a logged-in CDP session EXPIRES -- in an ~190-source fleet that failure is
# otherwise invisible (a dead adapter just returns []). Design (informed by the 2026-05-30 census,
# where probing 16-wide produced TRANSIENT false-failures that passed on immediate retry):
#   - probe each source's health_check (BOUNDED + one in-run retry to absorb a blip);
#   - require N_CONSECUTIVE failed RUNS before alerting -- a transient won't fail twice, hours apart;
#   - alert on RECOVERY too.
# CDP login-state monitoring: CDP sources are probed INDIVIDUALLY + SERIALLY via their health_check
# (a low-key homepage / login-wall check, NOT a search), so login expiry becomes visible WITHOUT
# account-risky activity. A DEAD CDP Chrome is relaunched (its launchd KeepAlive is
# SuccessfulExit=false, so a clean quit is not auto-relaunched); a FAILED self-heal escalates to Bark
# (the CDP safety net itself is down).
# State: ~/.penumbra/state/health-watchdog-state.json (unchanged path -> the mini's state carries).
_HEALTH_STATE = _STATE / "health-watchdog-state.json"
N_CONSECUTIVE = 2                       # consecutive failed runs before alerting
PROBE_WORKERS = 8                       # moderate -- 16-wide caused census false-fails
REALERT_COOLDOWN_S = 3 * 24 * 3600      # while still down, re-nag at most every 3d

# CDP (logged-in browser) sources -- probed INDIVIDUALLY + SERIALLY (login-state visibility).
# xiaohongshu lives on the ISOLATED 9223 Chrome (小号); the rest on the shared 9222.
_CDP_SOURCES = {"xiaohongshu", "xiaomuchong", "zhihu", "zhihu_users", "yipinsanfendi", "scrape_js_sites"}
# CDP-Chrome instances to liveness-check directly (label -> CDP URL). None = default 9222.
_CDP_INSTANCES = {"9222-shared": None, "9223-xhs": "http://127.0.0.1:9223"}
_SEALED_SOURCES: set[str] = set()


def _health_probe(adapter) -> tuple[bool, str]:
    """BOUNDED health_check (per-source hard timeout -> can never hang the loop) with one in-run
    retry to absorb transient blips. Routes through the same fetcher.health_check_bounded primitive
    the live MCP probe uses."""
    from penumbra.core import fetcher
    msg = "?"
    for attempt in (1, 2):
        ok, msg = fetcher.health_check_bounded(adapter)
        if ok:
            return True, str(msg)
        if attempt == 1:
            time.sleep(3)
    return False, str(msg)


def _health_track(name: str, ok: bool, msg: str, fails: dict, alerts: dict,
                  newly_down: list, recovered: list) -> None:
    """Shared consecutive-fail / recovery bookkeeping for one probed entity (cron_watchdog._track)."""
    prev = fails.get(name, 0)
    if ok:
        if prev >= N_CONSECUTIVE:
            recovered.append(name)
        fails[name] = 0
    else:
        fails[name] = prev + 1
        if fails[name] == N_CONSECUTIVE:
            newly_down.append((name, msg))
        elif fails[name] > N_CONSECUTIVE and _should_alert(f"down:{name}", alerts, REALERT_COOLDOWN_S):
            newly_down.append((name, msg))  # still down -- periodic re-nag


def _cdp_label_map() -> dict:
    """launchd label per CDP instance -- for SELF-HEAL relaunch. Reads services.py (the canonical
    registry, stdlib-only) so a CDP rename stays a one-file change. Their KeepAlive is
    SuccessfulExit=false (a CLEAN Chrome exit is NOT auto-relaunched), so a quit Chrome silently
    kills all its CDP sources until restarted."""
    try:
        import importlib.util
        svc_path = Path(__file__).resolve().parents[3] / "scripts" / "services.py"
        spec = importlib.util.spec_from_file_location("penumbra_services_ij", svc_path)
        svc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(svc)
        by_port = {r["port"]: r["label"] for r in svc.by_layer("cdp")}
        return {"9222-shared": by_port.get(9222), "9223-xhs": by_port.get(9223)}
    except Exception as exc:  # noqa: BLE001 -- fall back to the known labels if services.py is unreadable
        log.debug("cdp label map from services.py failed (%s); using literals", exc)
        return {"9222-shared": "com.penumbra.cdp.cn-forums", "9223-xhs": "com.penumbra.cdp.xhs"}


def _heal_cdp_chrome() -> list[str]:
    """Relaunch any DEAD CDP Chrome BEFORE probing, so the probes see live browsers (the chrome
    launchd agents don't auto-relaunch a clean exit). Returns the launchd services it FAILED to bring
    back -- a failed self-heal means the CDP safety net itself is down, so the caller escalates those
    to a Bark alert (otherwise the failure is silent)."""
    import subprocess
    from penumbra.core.sources.walled._cdp import cdp_health
    launchd = _cdp_label_map()
    failed: list[str] = []
    for label, url in _CDP_INSTANCES.items():
        service = launchd.get(label)
        if not service:
            continue
        try:
            alive = (cdp_health(url) if url else cdp_health())[0]
        except Exception:  # noqa: BLE001
            alive = False
        if alive:
            continue
        try:
            r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{service}"],
                               timeout=15, capture_output=True)
            time.sleep(8)
            try:  # VERIFY it came up -- kickstart returning 0 does NOT guarantee a live browser
                alive2 = (cdp_health(url) if url else cdp_health())[0]
            except Exception:  # noqa: BLE001
                alive2 = False
            if alive2:
                log.info("self-heal: %s was down -> relaunched OK", service)
            else:
                log.warning("self-heal: %s kickstart ran (rc=%s) but STILL down", service, r.returncode)
                failed.append(service)
        except Exception as exc:  # noqa: BLE001
            log.warning("self-heal: failed to relaunch %s: %s", service, exc)
            failed.append(service)
    return failed


def run_source_health() -> dict:
    """One source-health run (the cron_watchdog 源体检 half, minus the dissolved cron-liveness half):
    heal dead CDP Chrome, probe every source's health_check (non-CDP concurrent, CDP serial), track
    consecutive fails, and Bark newly-down / recovered (cooldown-gated). Returns a small summary."""
    from penumbra.server import load_sources
    from concurrent.futures import ThreadPoolExecutor
    load_sources()
    from penumbra.core import fetcher

    heal_failed = _heal_cdp_chrome()  # relaunch dead CDP Chrome (KeepAlive won't); collect failures

    names = sorted(fetcher.all_adapter_names())
    noncdp = [n for n in names if n not in _CDP_SOURCES and n not in _SEALED_SOURCES]
    cdp = [n for n in names if n in _CDP_SOURCES]

    def probe_named(n):
        return n, _health_probe(fetcher.get_adapter(n))

    results: dict[str, tuple[bool, str]] = {}
    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as ex:
        for n, res in ex.map(probe_named, noncdp):
            results[n] = res
    for n in cdp:  # CDP: SERIAL (one browser tab at a time -- gentle + account-safe)
        results[n] = _health_probe(fetcher.get_adapter(n))

    from penumbra.core.sources.walled._cdp import cdp_health
    infra: dict[str, tuple[bool, str]] = {}
    for label, url in _CDP_INSTANCES.items():
        try:
            infra[label] = cdp_health(url) if url else cdp_health()
        except Exception as exc:  # noqa: BLE001
            infra[label] = (False, f"{type(exc).__name__}: {exc}")

    state = _load_state(_HEALTH_STATE)
    alerts = state.get("_alerts", {})
    fails = state.get("fails", {})  # name -> consecutive fail count
    pushed = 0

    # Escalate a FAILED self-heal -- the CDP safety net is down; every CDP source on that instance
    # stays silently offline until the operator intervenes.
    for svc in heal_failed:
        if _should_alert(f"heal_fail:{svc}", alerts, REALERT_COOLDOWN_S):
            _bark(f"CDP 自愈失败 · {svc}",
                  f"launchctl 无法拉起 {svc} -- 该实例所有 CDP 源静默离线,需登录 Mac mini 检查",
                  group="Penumbra-Health")
            pushed += 1

    newly_down: list[tuple[str, str]] = []
    recovered: list[str] = []
    probed = noncdp + cdp
    for n in probed:
        ok, msg = results[n]
        _health_track(n, ok, msg, fails, alerts, newly_down, recovered)
    for label, (ok, msg) in infra.items():
        _health_track(f"_cdp:{label}", ok, msg, fails, alerts, newly_down, recovered)

    if newly_down:
        body = "\n".join(f"- {n}: {msg[:48]}" for n, msg in newly_down)
        _bark(f"源故障 · {len(newly_down)}", body, group="Penumbra-Health")
        pushed += 1
    if recovered:
        _bark(f"源恢复 · {len(recovered)}", "\n".join(f"- {n}" for n in recovered), group="Penumbra-Health")
        pushed += 1

    state["fails"] = fails
    state["_alerts"] = alerts
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    snap = {n: results[n][0] for n in probed}
    for label, (ok, _msg) in infra.items():
        snap[f"_cdp:{label}"] = ok
    state["last_status"] = snap
    _save_state(_HEALTH_STATE, state)

    n_green = sum(1 for n in probed if results[n][0])
    log.info("source-health: %d/%d healthy (%d CDP); newly_down=%d recovered=%d bark=%d",
             n_green, len(probed), len(cdp), len(newly_down), len(recovered), pushed)
    return {"healthy": n_green, "probed": len(probed), "newly_down": len(newly_down),
            "recovered": len(recovered), "bark": pushed}


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# JOB: wewe-rss probe   (transplanted from wewerss_watchdog.py; every:1800s)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# wechat2rss feed health check. (原 wewe-rss 双轨监控; 2026-06-06 弃用 AI寒武纪、退役 wewe-rss 自建后,
# 只剩这一轨。) Monitors 4 free wechat2rss.xlab.app feeds (no SLA): each reachable + whether the whole
# service froze. Any anomaly -> Bark. A 6h cooldown prevents a 30-min re-alert flood. There is no
# heal (a free public source has no local process to relaunch).
_WEWERSS_STATE = _STATE / "wewerss-last-alert.json"
_WEWERSS_COOLDOWN_S = 6 * 3600
# KEEP IN SYNC with wechat_source.DEFAULT_WECHAT2RSS_FEEDS.
_WECHAT2RSS_FEEDS = [
    ("PaperWeekly", "https://wechat2rss.xlab.app/feed/3be891c2f4e526629ab055a297cc2cd6c1f0a563.xml"),
    ("机器之心", "https://wechat2rss.xlab.app/feed/51e92aad2728acdd1fda7314be32b16639353001.xml"),
    ("量子位", "https://wechat2rss.xlab.app/feed/7131b577c61365cb47e81000738c10d872685908.xml"),
    ("新智元", "https://wechat2rss.xlab.app/feed/ede30346413ea70dbef5d485ea5cbb95cca446e7.xml"),
]
# All 4 are among China's most active AI媒体, so if NONE posted in this long the service froze (one
# quiet account will not trip it: we test the NEWEST across all feeds).
_WECHAT2RSS_FREEZE_LIMIT_S = 3 * 86400


def check_wechat2rss_feeds() -> tuple[bool, str]:
    """(all_ok, message). Flags a feed UNREACHABLE (per-feed; if xlab.app dies, all 4 trip) and a
    service-wide FREEZE (newest item across ALL feeds older than the limit, so one quiet account does
    not false-alarm). PURE except for the network reads (importable for the smoke)."""
    import re
    import urllib.request
    unreachable: list[str] = []
    newest = None
    for name, url in _WECHAT2RSS_FEEDS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                body = resp.read().decode("utf-8", "ignore")
        except Exception as exc:  # noqa: BLE001
            unreachable.append(f"{name}({type(exc).__name__})")
            continue
        for ds in re.findall(r"<pubDate>([^<]+)</pubDate>", body):
            try:
                dt = parsedate_to_datetime(ds)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if newest is None or dt > newest:
                    newest = dt
            except Exception:  # noqa: BLE001
                continue
    problems: list[str] = []
    if unreachable:
        problems.append("unreachable: " + ", ".join(unreachable))
    if newest is not None:
        age = datetime.now(timezone.utc) - newest
        if age.total_seconds() > _WECHAT2RSS_FREEZE_LIMIT_S:
            problems.append(f"all feeds frozen (newest {int(age.total_seconds() / 86400)}d old)")
    if problems:
        return False, "; ".join(problems)
    fresh = f"{int((datetime.now(timezone.utc) - newest).total_seconds() / 3600)}h" if newest else "?"
    return True, f"all {len(_WECHAT2RSS_FEEDS)} feeds OK (newest {fresh})"


def run_wewerss_probe() -> dict:
    """One wewe-rss run: probe the 4 feeds, Bark on anomaly (6h cooldown). No heal."""
    ok, msg = check_wechat2rss_feeds()
    log.info("wewerss: %s : %s", "OK" if ok else "FAIL", msg)
    state = _load_state(_WEWERSS_STATE)
    alerts = state.get("_alerts", {})
    pushed = 0
    if not ok and _should_alert("wechat2rss_down", alerts, _WEWERSS_COOLDOWN_S):
        _bark("wechat2rss feed 异常",
              f"{msg}。免费第三方源, 可能要换源或换号, 检查 wechat2rss.xlab.app。", group="Penumbra")
        pushed = 1
    state["_alerts"] = alerts
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    _save_state(_WEWERSS_STATE, state)
    return {"ok": ok, "bark": pushed}


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# JOB: session warmer   (transplanted from session_warmer.py; daily@09:17,14:17,19:17)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# WHY THIS EXISTS (root cause, 2026-06-22): the logged-in xhs sessions silently DEGRADE when the
# browser sits idle. The cookie that LOOKS like login (web_session, valid ~1yr) stays put, but the
# SHORT-LIVED anti-bot security cookies (acw_tc ~daily, websectiga, sec_poison_id, xsecappid) EXPIRE
# and are only re-minted by actual browser navigation. Once they lapse the eye goes DARK on those
# sources. This warmer drives each Chrome through light, READ-ONLY human-like activity (home ->
# scroll -> one search), which re-mints the security cookies + keeps search-auth alive. It ALSO
# doubles as the health probe: a genuinely degraded session Bark-alerts the operator.
#
# SAFETY (byte-preserved): read-only (navigation + scroll only -- never like/follow/comment, the
# 风控 write-risk path), jitter-paced, active-hours only (08-23 mini local), and it honours the same
# ~/.penumbra/state/cdp-maintenance pause flag cdp_keepalive uses (so it never fights a manual VNC
# login). One light search per account per run is trivial activity (a real user does dozens) and is
# the kind of passive browse 养号 recommends; it stays well under any rate concern.
_WARMER_STATE = _STATE / "session-warmer.json"
_MAINT_FLAG = _STATE / "cdp-maintenance"
_WARMER_COOLDOWN_S = 6 * 3600
_ACTIVE_START, _ACTIVE_END = 8, 23  # mini local hours; match the xiaohongshu_cn adapter gate

# Benign rotating warm queries (look like a real PhD-track user glancing at their feed).
_WARM_QUERIES = ["读博日常", "科研", "博士生活", "留学生活", "实验室日常", "读研"]

# label -> (cdp url, home url, search url template {kw}, site-key substring, probe-kind).
# probe-kind decides how a degraded session is DETECTED (the warm half -- home+scroll -- is identical):
#   "xhs_search"   = observe the search-result XHR + DOM (a login wall hides the results cards).
#   "douyin_login" = read the login state DIRECTLY (localStorage HasUserLogin / cookie LOGIN_STATUS,
#                    the same signal douyin_source.health_check uses). 抖音's search XHR is NOT reliably
#                    fired by a page nav (the adapter same-origin-FETCHes it), so observing the page's
#                    own XHR is flaky here; the login-state read is the reliable degradation signal.
_WARMER_INSTANCES = {
    "小号-rednote": (
        "http://127.0.0.1:9223", "https://www.rednote.com",
        "https://www.rednote.com/search_result?keyword={kw}&type=51", "rednote", "xhs_search"),
    "大陆号-xiaohongshu": (
        "http://127.0.0.1:9224", "https://www.xiaohongshu.com",
        "https://www.xiaohongshu.com/search_result?keyword={kw}", "xiaohongshu", "xhs_search"),
    "抖音": (
        "http://127.0.0.1:9225", "https://www.douyin.com",
        "https://www.douyin.com/search/{kw}?type=general", "douyin", "douyin_login"),
}

_DOM_JS = r"""(()=>({
  note_items: document.querySelectorAll("section.note-item, [class*='note-item'], a[href*='/explore/'], a[href*='/search_result/']").length,
  login_overlay: !!document.querySelector(".login-container,[class*='LoginModal'],.reds-mask"),
  login_btn: (document.body.innerText.match(/登录/g)||[]).length,
  body_len: document.body.innerText.length
}))()"""


def _jsleep(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


def _warm_one(p, label: str, cdp: str, home: str, search_tpl: str, key: str, probe: str) -> dict:
    """Drive ONE Chrome through home -> scroll -> one search. Returns a status dict. Never raises
    (per-instance isolation); records why if it could not warm. ``probe`` selects the degradation-
    detection (see _WARMER_INSTANCES): "xhs_search" observes the result XHR/DOM, "douyin_login" reads
    the login state directly."""
    from urllib.parse import quote
    res = {"label": label, "ok": False, "reason": "", "notes": 0, "acw_tc": False, "web_session": False}
    try:
        b = p.chromium.connect_over_cdp(cdp)
    except Exception as exc:  # noqa: BLE001
        res["reason"] = f"cdp connect failed: {exc!r}"
        return res
    try:
        ctx = b.contexts[0]
        cand = [pg for pg in ctx.pages if key in (pg.url or "")]
        page = cand[0] if cand else (ctx.pages[0] if ctx.pages else ctx.new_page())

        # 1) home + light human-like scroll (re-mints the short-lived security cookies)
        try:
            page.goto(home, wait_until="domcontentloaded", timeout=25000)
        except Exception as exc:  # noqa: BLE001
            res["reason"] = f"home goto failed: {exc!r}"
            return res
        _jsleep(2.5, 4.0)
        for _ in range(3):
            try:
                page.evaluate("window.scrollBy(0, 700 + Math.random()*500)")
            except Exception:  # noqa: BLE001
                break
            _jsleep(1.0, 2.2)

        # cookie freshness snapshot
        try:
            jar = ctx.cookies()
            names = {c["name"] for c in jar if key in (c.get("domain") or "") or (c.get("domain") or "").startswith(".")}
            res["acw_tc"] = "acw_tc" in names
            res["web_session"] = "web_session" in names
        except Exception:  # noqa: BLE001
            pass

        # 抖音 (login-probe instances): the home+scroll above already re-minted msToken + the security
        # cookies; 抖音's search XHR is NOT reliably page-fired (the adapter same-origin-FETCHes it), so
        # detect degradation by reading the LOGIN STATE directly -- the same signal
        # douyin_source.health_check uses. A light search-page nav adds a little extra warming.
        if probe == "douyin_login":
            kw = random.choice(_WARM_QUERIES)
            try:
                page.goto(search_tpl.format(kw=quote(kw)), wait_until="domcontentloaded", timeout=25000)
                _jsleep(3.0, 5.0)
            except Exception:  # noqa: BLE001
                pass
            try:
                st = page.evaluate("()=>({login: window.localStorage.getItem('HasUserLogin'),"
                                   "status:(document.cookie.match(/LOGIN_STATUS=([^;]+)/)||[])[1]||''})")
            except Exception:  # noqa: BLE001
                st = {}
            logged_in = (st or {}).get("login") == "1" or (st or {}).get("status") == "1"
            res["notes"] = 1 if logged_in else 0  # reuse the health-signal field
            res["ok"] = bool(logged_in)
            if not logged_in:
                res["reason"] = (f"degraded: 抖音 logged out (HasUserLogin={(st or {}).get('login')}, "
                                 f"LOGIN_STATUS={(st or {}).get('status')})")
            return res

        # 2) one light search (warms search-auth + serves as the health probe)
        notes = {"n": 0}

        def _on_resp(r):
            try:
                if "search/notes" in r.url or "/api/sns/web/v1/search" in r.url:
                    j = r.json()
                    notes["n"] += len((j.get("data") or {}).get("items") or [])
            except Exception:  # noqa: BLE001
                pass

        page.on("response", _on_resp)
        kw = random.choice(_WARM_QUERIES)
        try:
            page.goto(search_tpl.format(kw=quote(kw)), wait_until="domcontentloaded", timeout=25000)
        except Exception as exc:  # noqa: BLE001
            res["reason"] = f"search goto failed: {exc!r}"
            return res
        _jsleep(4.0, 6.0)
        try:
            dom = page.evaluate(_DOM_JS)
        except Exception:  # noqa: BLE001
            dom = {"note_items": 0, "login_overlay": True, "login_btn": 9, "body_len": 0}
        res["notes"] = max(notes["n"], 0)

        # Healthy iff search produced results AND no blocking login overlay.
        healthy = (res["notes"] > 0 or dom.get("note_items", 0) > 3) and not dom.get("login_overlay")
        res["ok"] = bool(healthy)
        if not healthy:
            res["reason"] = (f"degraded: search notes={res['notes']} dom_items={dom.get('note_items')} "
                             f"login_overlay={dom.get('login_overlay')} login_btn={dom.get('login_btn')} "
                             f"body_len={dom.get('body_len')}")
        return res
    except Exception as exc:  # noqa: BLE001
        res["reason"] = f"warm flow raised: {exc!r}"
        return res


def run_session_warmer() -> dict:
    """One warm run across the walled CDP Chromes: skip outside active hours or under the
    cdp-maintenance flag; else warm each account (home -> scroll -> one search) + Bark any degraded
    session (6h cooldown). The playwright driver is imported here (available in-process); a run that
    cannot import it degrades to a logged no-op rather than crashing the tick."""
    force = os.environ.get("WARMER_FORCE") == "1"   # bypass active-hours gate (testing / manual run)
    only = {s for s in os.environ.get("WARMER_ONLY", "").split(",") if s}  # limit to these labels
    now_h = time.localtime().tm_hour
    if not force and not (_ACTIVE_START <= now_h < _ACTIVE_END):
        log.info("session-warmer: outside active hours %d-%d (now %dh) -> skip",
                 _ACTIVE_START, _ACTIVE_END, now_h)
        return {"skipped": "active-hours"}
    if _MAINT_FLAG.exists():
        log.info("session-warmer: cdp-maintenance flag present -> skip (rm the flag to resume)")
        return {"skipped": "maintenance"}

    try:
        try:
            from patchright.sync_api import sync_playwright
        except ImportError:
            from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001 -- no browser driver in this deployment -> logged no-op
        log.warning("session-warmer: no playwright driver (%s) -> skip", exc)
        return {"skipped": "no-driver"}

    state = _load_state(_WARMER_STATE)
    alerts = state.get("_alerts", {})
    results = []
    with sync_playwright() as p:
        for label, (cdp, home, search_tpl, key, probe) in _WARMER_INSTANCES.items():
            if only and not (label in only or key in only):  # WARMER_ONLY accepts label or ASCII key
                continue
            r = _warm_one(p, label, cdp, home, search_tpl, key, probe)
            results.append(r)
            log.info("session-warmer %s: ok=%s notes=%s acw_tc=%s web_session=%s %s",
                     label, r["ok"], r["notes"], r["acw_tc"], r["web_session"],
                     ("| " + r["reason"]) if r["reason"] else "")
            _jsleep(3.0, 6.0)  # space the accounts apart

    degraded = [r for r in results if not r["ok"]]
    for r in degraded:
        if _should_alert(f"session_degraded:{r['label']}", alerts, _WARMER_COOLDOWN_S):
            _bark(f"{r['label']} session 退化",
                  f"暖号验证失败:{r['reason']}。登录态可能已失效,需 VNC 进 mini 重新扫码登录该账号"
                  f"({r['label']} 的 Chrome 窗口)。", group="Penumbra-Health")

    state["_alerts"] = alerts
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["last_results"] = [{k: r[k] for k in ("label", "ok", "notes", "acw_tc", "reason")} for r in results]
    _save_state(_WARMER_STATE, state)
    log.info("session-warmer: warmed=%s degraded=%s",
             [r["label"] for r in results if r["ok"]], [r["label"] for r in degraded])
    return {"warmed": [r["label"] for r in results if r["ok"]],
            "degraded": [r["label"] for r in degraded]}


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# JOB: log rotation   (transplanted from penumbra_http_watchdog.py rotation half; daily@04:50)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# Copytruncate any oversized log in ~/.penumbra/logs so one chatty source can't grow a log unbounded
# (eye-http.err reached 63MB from a reddit Arctic-Shift 429 storm). launchd opens these redirects
# with O_APPEND (verified 2026-06-20: truncate-in-place stayed small, no sparse hole), so this is
# safe with the writer still running: it resumes appending at the new EOF, NO restart needed. The
# last _LOG_KEEP_TAIL bytes are kept as <name>.1 for recent context.
_LOG_MAX_BYTES = 30 * 1024 * 1024   # copytruncate any penumbra log over 30MB
_LOG_KEEP_TAIL = 4 * 1024 * 1024    # keep the last 4MB of recent context after rotating


def rotate_logs() -> int:
    """Copytruncate every oversized log in ~/.penumbra/logs. Returns how many it rotated. One bad log
    never blocks the others; the whole pass is wrapped so log hygiene can never break the scheduler."""
    rotated = 0
    try:
        for lg in (*_LOG_DIR.glob("*.err"), *_LOG_DIR.glob("*.log")):  # .1 generations are not re-matched
            try:
                if lg.stat().st_size <= _LOG_MAX_BYTES:
                    continue
                with open(lg, "rb") as f:
                    f.seek(-_LOG_KEEP_TAIL, os.SEEK_END)
                    tail = f.read()
                (lg.parent / (lg.name + ".1")).write_bytes(tail)
                with open(lg, "r+b") as f:  # truncate in place; the O_APPEND writer resumes at EOF=0
                    f.truncate(0)
                rotated += 1
                log.info("rotated %s (>%dMB, kept last %dMB as %s.1)",
                         lg.name, _LOG_MAX_BYTES // (1024 * 1024), _LOG_KEEP_TAIL // (1024 * 1024), lg.name)
            except Exception as exc:  # noqa: BLE001 -- one bad log must not block the others
                log.warning("rotate %s failed: %s", lg.name, exc)
    except Exception as exc:  # noqa: BLE001 -- log hygiene must NEVER break the scheduler
        log.warning("log-rotate skipped: %s", exc)
    return rotated


def run_log_rotation() -> dict:
    return {"rotated": rotate_logs()}


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# JOB: curator monthly pass   (transplanted from curator.py; monthly@1-06:00, ENABLED per Captain)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# THE ONE-LINE RAZOR (every drift violates it): the pass is mechanical-only. It discovers, probes,
# persists, counts, and Barks pure FACTS, rendering ZERO editorial judgment. A spawned AGENT renders
# EVERY verdict (which gap matters, which candidate to pursue, admit/watch/reject). Captain owns only
# red-line/coverage POLICY DATA + the single irreversible sanction (committing an owner_review-staged
# config row). This code imports NO verdict-writer, NO model/anthropic client, NO WebSearch, NO
# profile.*/relevance/employer_hits. Every digest list is sorted(...) lexicographic, NEVER
# centrality/yield/relevance-ranked.
#
# GATES, as they actually ship (verified against curator_policy.json at the P9 gate; an earlier
# draft of this note claimed enabled:false, which was stale): the policy ships enabled:TRUE, so an
# enabled row DOES run the monthly discovery -> probe -> neutral-evidence-packet pass. What stays
# data-gated: cold_start.enabled ships FALSE (web-seeding of unreachable coverage cells is off,
# hard-capped by its budget when on), and discovery only pursues cells DECLARED in
# coverage_targets.json. The min-cadence guard means an out-of-schedule extra run within the
# cadence window is a clean no-op, so a deploy restart storm cannot defeat the monthly cadence.
# State: ~/.penumbra/state/curator/curator-loop-state.json (unchanged path).
#
# NOTE (ignition, P9): the ROW is ENABLED per Captain: expect one Bark digest of neutral facts per
# month (admit/watch/reject verdicts stay the agent's, in the /curator session). Runs in the writer
# process; discovery's own probe fetches use _run_bounded, and the pass records no yield.
_CURATOR_STATE = _STATE / "curator" / "curator-loop-state.json"
_CURATOR_RUN_HISTORY_CAP = 12  # bounded ring of run summaries for the STOP streak


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def _curator_read_policy():
    """Read curator_policy.json (the operator DATA). Tolerant: a missing/corrupt file degrades to the
    INERT built-in (enabled:false), so a broken policy never accidentally ENABLES discovery."""
    p = (Path(__file__).resolve().parent / "curator" / "curator_policy.json")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as exc:  # noqa: BLE001
        log.warning("curator_policy.json unreadable (%s) -> INERT built-in (enabled:false)", exc)
    return {"enabled": False, "cold_start": {"enabled": False, "budget": 3},
            "min_cadence_days": 25, "M_zero_new_streak": 3, "discover_topn": 12,
            "error_retry_budget": 2, "watching_max_reprobes": 3, "watching_ttl_days": 120,
            "max_new_probes": 24, "coverage_ceiling": {"_default": 4}}


def _curator_is_stopped(state: dict, policy: dict) -> bool:
    """True iff the loop has CONVERGED: M consecutive demonstrably-HEALTHY rounds with zero net-new
    placed cells. A pure read of the run history + state counter; renders no verdict."""
    m = int(policy.get("M_zero_new_streak", 3) or 3)
    return int(state.get("consecutive_zero_new_rounds", 0) or 0) >= m


def _curator_live_hosts_safe(fetcher) -> set:
    try:
        from penumbra.core.curator import apply as _apply
        return _apply._live_hosts()
    except Exception:  # noqa: BLE001
        return set()


def _curator_set_field(candidates, cid: str, key: str, value) -> None:
    """Persist a scalar bookkeeping field (error_count) on a candidate row WITHOUT a state change.
    Mechanical, no verdict. Degrades silently on any failure (a convenience counter, not load-bearing
    for safety)."""
    try:
        with candidates._LOCK:
            rows = candidates._load_all()
            for r in rows:
                if r.get("id") == cid:
                    r[key] = value
                    break
            candidates._save_all(rows)
    except Exception as exc:  # noqa: BLE001
        log.warning("curator set_field %s on %s failed: %s", key, cid, exc)


def _curator_sweep_watching(candidates, policy: dict) -> None:
    """Route a watching candidate past its re-probe budget / TTL to rejected ('watch-expired'). A
    mechanical lifecycle bound (the recurring re-probe set cannot grow unbounded), NOT a verdict on
    the source's worth: it uses the watching->rejected FSM edge with a fixed mechanical reason."""
    max_reprobes = int(policy.get("watching_max_reprobes", 3) or 3)
    ttl_days = float(policy.get("watching_ttl_days", 120) or 120)
    now = _now_epoch()
    for row in candidates.list("watching"):
        cid = row.get("id")
        reprobes = sum(1 for h in (row.get("history") or []) if h.get("state") == "probed")
        expired = reprobes >= max_reprobes
        if not expired:
            verdict = row.get("verdict") or {}
            at = verdict.get("at")
            if isinstance(at, str):
                try:
                    t = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
                    expired = (now - t) >= (ttl_days * 86400.0)
                except Exception:  # noqa: BLE001
                    expired = False
        if expired:
            try:
                candidates.set_state(cid, "rejected", note="watch-expired (TTL/re-probe budget)", by="curator")
                for u in (row.get("urls") or []):
                    candidates.record_tried_host(u)
            except Exception as exc:  # noqa: BLE001
                log.warning("curator watch-expire of %s failed: %s", cid, exc)


def _curator_net_new_placed(state: dict, empty_cells: list) -> int:
    """How many target cells became PLACED since last round (left empty_cells_for_discovery). A pure
    set-difference FACT for the STOP streak. First round (no prior) -> 0."""
    prev = set(state.get("prev_empty_cells") or [])
    if not prev:
        return 0
    return len(prev - set(empty_cells))


def run_curator() -> dict:
    """One curator P4 loop pass (curator.py main() transplanted). Mechanical-only: gap read ->
    discover (gated by policy.enabled) -> dedup+persist -> pre-build evidence -> watching sweep ->
    diff-gated Bark -> write run-summary state. Renders NO verdict."""
    from penumbra.server import load_sources
    load_sources()
    from penumbra.core.curator import candidates, discover, evidence, probe, redlines, source_audit  # noqa: F401
    from penumbra.core import fetcher

    policy = _curator_read_policy()
    state = _load_state(_CURATOR_STATE)
    first_run = not state

    # Step 0: min-interval guard. A run within the cadence window is a clean no-op (RunAtLoad restart
    # storms during dev redeploys cannot defeat the monthly cadence). Skipped on the first run.
    min_cadence_days = float(policy.get("min_cadence_days", 25) or 25)
    last_run_epoch = state.get("last_discovery_run_epoch")
    if not first_run and isinstance(last_run_epoch, (int, float)):
        if (_now_epoch() - float(last_run_epoch)) < (min_cadence_days * 86400.0):
            log.info("curator: within cadence (%sd); no-op", min_cadence_days)
            return {"noop": "within-cadence"}

    # Step 1: gap read (READ-ONLY). The SAME gather the weekly audit makes.
    dossier = source_audit.gather_source_dossier()
    empty_cells = list(dossier.get("empty_cells_for_discovery") or [])
    single_cells = list(dossier.get("single_occupant_cells") or [])
    try:
        from penumbra.core.curator import yield_tap
        yield_state = yield_tap._load_all()
    except Exception as exc:  # noqa: BLE001
        log.warning("curator: yield read failed (%s) -> empty", exc)
        yield_state = {}

    # Step 2: discover. Returns [] when policy.enabled is false (scaffold idle).
    try:
        findings = discover.discover(dossier, yield_state=yield_state, policy=policy)
    except Exception as exc:  # noqa: BLE001: a discovery failure is a degraded round, never fatal
        log.warning("curator: discover failed (%s) -> degraded round, no findings", exc)
        findings = []
    discovery_health = discover.discovery_health(findings, dossier)
    log.info("curator: discovered %d candidate(s); discovery_health=%s", len(findings), discovery_health)

    # Step 3: dedup + persist.
    try:
        live_hosts = _curator_live_hosts_safe(fetcher)
    except Exception:  # noqa: BLE001
        live_hosts = set()
    existing = {r.get("id"): r.get("state") for r in candidates.list()}
    survived = 0
    for f in findings:
        urls = f.get("urls") or []
        cand_hosts = {candidates.canonical_host(u) for u in urls}
        cand_hosts.discard("")
        if cand_hosts & {candidates.canonical_host(h) for h in live_hosts}:
            continue  # already a live source
        if any(candidates.host_is_tried(u) for u in urls):
            continue  # terminal-host ledger
        fid = f.get("id") or candidates.make_id(f.get("name") or "", urls)
        st = existing.get(fid)
        if st is not None and st not in ("new", "error"):
            continue  # already tracked downstream
        try:
            candidates.add(f)
            survived += 1
        except Exception as exc:  # noqa: BLE001: one bad row must not abort the round
            log.warning("curator: candidates.add failed for %s: %s", fid, exc)
    log.info("curator: survived dedup: %d", survived)

    # Step 4: pre-build evidence (drive P1's mechanical half). NEVER calls a verdict-writer.
    error_retry_budget = int(policy.get("error_retry_budget", 2) or 2)
    max_new_probes = int(policy.get("max_new_probes", 24) or 24)
    probed = 0
    for row in candidates.list("new"):
        if probed >= max_new_probes:
            break  # per-run probe ceiling
        cid = row.get("id")
        if not (row.get("urls") or []):
            continue  # URL-less cold-start stub: surfaces as an unfilled cell
        probed += 1
        hits = redlines.match(row)
        if any(h.get("severity") == "hard" for h in hits):
            candidates.set_state(cid, "redline_blocked",
                                 note=f"hard red-line: {[h['id'] for h in hits if h['severity']=='hard']}")
            for u in (row.get("urls") or []):
                candidates.record_tried_host(u)
            continue
        ok, probe_out = fetcher._run_bounded(lambda r=row: probe.mode_probe(r), 60.0)
        if not ok or (isinstance(probe_out, dict) and probe_out.get("probe_error")
                      and not probe_out.get("probe_reached")):
            reason = "deadline" if not ok else probe_out.get("probe_error")
            ec = int((candidates.get(cid) or {}).get("error_count", 0) or 0) + 1
            if ec >= error_retry_budget:
                candidates.set_state(cid, "probe_dead", note=f"probe failed {ec}x: {reason}")
                _curator_set_field(candidates, cid, "error_count", ec)
                for u in (row.get("urls") or []):
                    candidates.record_tried_host(u)
            else:
                candidates.set_state(cid, "error", note=f"probe failure {ec}/{error_retry_budget}: {reason}")
                _curator_set_field(candidates, cid, "error_count", ec)
            continue
        _curator_set_field(candidates, cid, "error_count", 0)
        row["_probe_cache"] = probe_out
        packet = evidence.build_packet_for(row)
        digest = evidence.safety_digest(packet)
        candidates.store_evidence(cid, packet, digest, "awaiting_verdict",
                                  note=f"probed (mode={probe_out.get('mode')})")
    log.info("curator: probed %d new row(s)", probed)

    _curator_sweep_watching(candidates, policy)

    # Step 5: Bark (diff-gated edge-alarm).
    prev = state.get("last_signals") or {}
    awaiting_ids = sorted(r.get("id") for r in candidates.list("awaiting_verdict") if r.get("id"))
    newly_awaiting = sorted(set(awaiting_ids) - set(prev.get("awaiting_verdict_ids") or []))
    newly_empty = sorted(set(empty_cells) - set(prev.get("empty_cells") or []))
    newly_single = sorted(set(single_cells) - set(prev.get("single_occupant_cells") or []))

    pushed = 0
    lines = []
    if newly_awaiting:
        names = sorted((candidates.get(i) or {}).get("name") or i for i in newly_awaiting)
        lines.append(f"{len(newly_awaiting)} NEW candidate(s) awaiting verdict: "
                     f"{', '.join(names[:8])}" + (" ..." if len(names) > 8 else ""))
    if newly_empty:
        lines.append(f"{len(newly_empty)} NEWLY-empty target cell(s): "
                     f"{', '.join(newly_empty[:10])}" + (" ..." if len(newly_empty) > 10 else ""))
    if newly_single:
        lines.append(f"{len(newly_single)} NEWLY-single-occupant cell(s): "
                     f"{', '.join(newly_single[:10])}" + (" ..." if len(newly_single) > 10 else ""))
    body = "\n".join(lines) if lines else "no new curator signals this round"
    log.info("curator: %s", body.replace("\n", " | "))

    if first_run:
        log.info("curator: first run -> SILENT baseline (no push); signals recorded for next diff")
    elif lines:
        title = f"源采集巡查 · {len(newly_awaiting)} 新候选 / {len(newly_empty)} 新空格"
        _bark(title, body + "\n\n(中性事实; admit/watch/reject 由采集 agent 判,本巡查不裁决)",
              group="Penumbra-Curator", level="passive")
        pushed += 1

    # Step 6: write run-summary state (for the STOP streak). Degraded FREEZES the streak.
    net_new_placed = _curator_net_new_placed(state, empty_cells)
    streak = int(state.get("consecutive_zero_new_rounds", 0) or 0)
    if discovery_health == "degraded":
        pass  # FREEZE: an outage round never masquerades as saturation
    elif net_new_placed == 0:
        streak += 1
    else:
        streak = 0
    summary = {
        "at": _now_iso(), "candidates_found": len(findings), "survived_dedup": survived,
        "newly_awaiting_count": len(newly_awaiting), "empty_cells_count": len(empty_cells),
        "net_new_placed": net_new_placed, "discovery_health": discovery_health,
    }
    history = list(state.get("run_history") or [])
    history.append(summary)
    history = history[-_CURATOR_RUN_HISTORY_CAP:]

    state["last_run"] = _now_iso()
    state["last_discovery_run_epoch"] = _now_epoch()
    state["consecutive_zero_new_rounds"] = streak
    state["prev_empty_cells"] = empty_cells
    state["run_history"] = history
    state["last_signals"] = {"awaiting_verdict_ids": awaiting_ids, "empty_cells": empty_cells,
                             "single_occupant_cells": single_cells}
    _save_state(_CURATOR_STATE, state)

    stopped = _curator_is_stopped(state, policy)
    log.info("curator: done. found=%d survived=%d probed=%d awaiting_new=%d streak=%d stopped=%s bark=%d",
             len(findings), survived, probed, len(newly_awaiting), streak, stopped, pushed)
    return {"found": len(findings), "survived": survived, "probed": probed,
            "awaiting_new": len(newly_awaiting), "streak": streak, "stopped": stopped, "bark": pushed}


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# JOB: source audit   (transplanted from source_audit.py; weekly@sun-06:00, ENABLED per Captain)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# The health watchdog reports a source that DIED; this reports the slow structural question: which
# sources look like dead weight (yield-measured), which feeds went silent, and -- the inverse of a
# prune -- which (domain x mode) coverage cells are EMPTY and want a NEW source. It PRUNES NOTHING and
# renders NO verdict: it calls the read-only mechanical gather (source_audit.gather_source_dossier)
# and surfaces NEUTRAL candidates + gaps for the spawned audit agent + the operator. Every
# keep/watch/prune call is the agent's, downstream of this push.
# Discipline: first run per signal-class is a SILENT baseline (no Bark flood); prune-FLAGGING is
# suppressed for any source below min_evidence_met (cold-start); one deduplicated Bark digest, diff-
# gated so a standing structural warning does not re-fire every week.
# State: ~/.penumbra/state/job.source-audit/state.json (unchanged path).
_AUDIT_STATE = _STATE / "job.source-audit" / "state.json"
_MIN_SEARCHES_TO_FLAG = 30        # only flag sole_share=0 once offered to >= this many searches
_DEAD_FAILS_TO_FLAG = 2           # consecutive watchdog fails before surfacing as a DEAD candidate
_SILENT_DAYS_TO_FLAG = 30         # live-feed-silent days before surfacing as a low-yield candidate


def _audit_redundant(sources: list) -> list:
    """Sources offered to enough searches, cold-start cleared, sole_share == 0 (others always
    co-surface their hits) AND not protected/tap-blind/deadline-starved. NEUTRAL surfacing only."""
    out = []
    for s in sources:
        f = s.get("safety_flags") or {}
        if not f.get("min_evidence_met"):
            continue  # cold-start: never surface as a prune candidate
        if f.get("protected_sole_contributor") or f.get("tap_blind") or f.get("deadline_starved"):
            continue
        y = s.get("yield") or {}
        offered = int(y.get("searches_present", 0)) + int(y.get("searches_timed_out", 0))
        if offered < _MIN_SEARCHES_TO_FLAG:
            continue
        if (s.get("ratios") or {}).get("sole_share", 0.0) == 0.0 and int(y.get("topk_appearances", 0)) > 0:
            out.append(s["name"])
    return sorted(out)


def _audit_dead(sources: list) -> list:
    """Sources with >= _DEAD_FAILS_TO_FLAG consecutive watchdog fails AND not CDP/credentialed (a
    benign auth failure is not death)."""
    out = []
    for s in sources:
        f = s.get("safety_flags") or {}
        if f.get("is_cdp_or_credentialed"):
            continue
        if int((s.get("watchdog") or {}).get("consecutive_fails", 0)) >= _DEAD_FAILS_TO_FLAG:
            out.append(s["name"])
    return sorted(out)


def _audit_silent(sources: list) -> list:
    """Live feed silent >= _SILENT_DAYS_TO_FLAG days but recall still carrying it (from_index_only>0),
    and NOT below its own cadence floor (a quarterly feed between cycles is expected silence)."""
    out = []
    for s in sources:
        f = s.get("safety_flags") or {}
        if f.get("below_cadence_floor"):
            continue
        days = (s.get("ingest") or {}).get("live_feed_silent_days")
        carrying = int((s.get("yield") or {}).get("from_index_only_appearances", 0)) > 0
        if isinstance(days, (int, float)) and days >= _SILENT_DAYS_TO_FLAG and carrying:
            out.append(s["name"])
    return sorted(out)


def run_source_audit() -> dict:
    """One source-audit pass (source_audit.py main() transplanted). Read-only + diff-gated Bark;
    renders NO verdict. First run per signal-class is a SILENT baseline."""
    from penumbra.server import load_sources
    load_sources()
    from penumbra.core.curator import source_audit

    dossier = source_audit.gather_source_dossier()  # READ-ONLY mechanical gather
    sources = dossier.get("sources", [])
    redundant = _audit_redundant(sources)
    dead = _audit_dead(sources)
    silent = _audit_silent(sources)
    empty_cells = dossier.get("empty_cells", [])
    single_cells = dossier.get("single_occupant_cells", [])
    revalidation = dossier.get("revalidation_candidates", [])  # stale prior judgments to re-look

    state = _load_state(_AUDIT_STATE)
    first_run = not state  # first ever run -> SILENT baseline, no Bark flood
    pushed = 0

    # Diff-gate the STANDING structural lines into EDGE alarms: compare against the saved baseline +
    # Bark only the cells that JUST changed (a cell that newly emptied / newly dropped to one occupant
    # = a real fragility event). Persist the full current sets below so a cell leaving a set re-arms it.
    prev = state.get("last_candidates") or {}
    newly_empty = sorted(set(empty_cells) - set(prev.get("empty_cells") or []))
    newly_single = sorted(set(single_cells) - set(prev.get("single_occupant_cells") or []))
    newly_stale = sorted(set(revalidation) - set(prev.get("revalidation_candidates") or []))

    lines = []
    if redundant:
        lines.append(f"{len(redundant)} sources sole_share=0 over >={_MIN_SEARCHES_TO_FLAG} "
                     f"searches: {', '.join(redundant[:8])}" + (" ..." if len(redundant) > 8 else ""))
    if dead:
        lines.append(f"{len(dead)} sources consecutive_fails>={_DEAD_FAILS_TO_FLAG}: "
                     f"{', '.join(dead[:8])}" + (" ..." if len(dead) > 8 else ""))
    if silent:
        lines.append(f"{len(silent)} sources live-silent >={_SILENT_DAYS_TO_FLAG}d but recall "
                     f"carrying: {', '.join(silent[:8])}" + (" ..." if len(silent) > 8 else ""))
    if newly_empty:
        lines.append(f"{len(newly_empty)} NEWLY-empty (domain x mode) cells = coverage GAPS to ADD: "
                     f"{', '.join(newly_empty[:10])}" + (" ..." if len(newly_empty) > 10 else ""))
    if newly_single:
        lines.append(f"{len(newly_single)} cells NEWLY dropped to ONE live occupant "
                     f"(prune-protected): {', '.join(newly_single[:10])}"
                     + (" ..." if len(newly_single) > 10 else ""))
    if newly_stale:
        _floor = int((dossier.get("policy") or {}).get("verdict_revalidation_floor_days", 90))
        lines.append(f"{len(newly_stale)} 个源上次裁决 >{_floor}d (复检 fruit): "
                     f"{', '.join(newly_stale[:10])}" + (" ..." if len(newly_stale) > 10 else ""))

    body = "\n".join(lines) if lines else "no audit candidates this week"
    log.info("source-audit: %s", body.replace("\n", " | "))

    if first_run:
        log.info("source-audit: first run -> SILENT baseline (no push); candidates recorded for next diff")
    elif lines:
        title = (f"源审计周报 · {len(redundant)+len(dead)+len(silent)} 候选 / "
                 f"{len(newly_empty)} 新空格 / {len(newly_stale)} 待复检")
        _bark(title, body + "\n\n(中性事实; KEEP/WATCH/PRUNE 由审计 agent 判,本哨兵不裁决)",
              group="Penumbra-Curator", level="passive")
        pushed += 1

    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    state["last_candidates"] = {"redundant": redundant, "dead": dead, "silent": silent,
                                "empty_cells": empty_cells, "single_occupant_cells": single_cells,
                                "revalidation_candidates": revalidation}
    _save_state(_AUDIT_STATE, state)
    log.info("source-audit: done. redundant=%d dead=%d silent=%d empty=%d(newly=%d) "
             "single=%d(newly=%d) reval=%d(newly=%d) bark=%d",
             len(redundant), len(dead), len(silent), len(empty_cells), len(newly_empty),
             len(single_cells), len(newly_single), len(revalidation), len(newly_stale), pushed)
    return {"redundant": len(redundant), "dead": len(dead), "silent": len(silent),
            "empty_cells": len(empty_cells), "bark": pushed}


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# JOB: weekly digest   (transplanted from digest.py; weekly@mon-09:00, DISABLED)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# A cross-source, deduped, ranked reading list. Where the sensors push individual NEW items (event-
# driven), the digest gives Captain a periodic CURATED view: per theme it runs fetcher.search_ranked
# across the relevant sources -> cross-source dedup + unified ranking -> the top items. Output: a
# Markdown digest (saved + archived) + a condensed Bark push. The synthesis is STRUCTURAL (dedup +
# rank + theme grouping), not LLM prose -- the Markdown is the substrate a session turns into prose
# on demand.
#
# THE THEMES LIST LEFT THE CODE (P9): it is now DATA at ~/.penumbra/state/digest-themes.json (seeding
# that file on the mini is the CEO's migration step). When the file is ABSENT the job NO-OPS with a
# log line -- it never invents a theme list. Row ships DISABLED; enabling is a profile jobs override
# once the themes file exists.
# State: ~/.penumbra/state/digests/ (Markdown out) + the themes file (in).
_DIGEST_DIR = _STATE / "digests"
_DIGEST_THEMES_PATH = _STATE / "digest-themes.json"
_DIGEST_PER_THEME = 6


def _load_digest_themes() -> list:
    """The theme rows (each {label, query, sources}) from ~/.penumbra/state/digest-themes.json, or []
    when the file is absent/corrupt. The job no-ops on []. Never a built-in default (the list is
    Captain's DATA, seeded on the mini)."""
    if not _DIGEST_THEMES_PATH.exists():
        return []
    try:
        data = json.loads(_DIGEST_THEMES_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:  # noqa: BLE001
        log.warning("digest-themes.json unreadable (%s) -> no-op", exc)
        return []


def _digest_theme_section(fetcher, theme: dict) -> tuple[list, "str | None"]:
    # Bounded per theme: the digest is a periodic background pulse -- timeliness over completeness,
    # and a slow venue must not stretch the serial multi-theme run.
    docs, _ = fetcher.search_ranked(theme.get("query", ""), theme.get("sources"),
                                    _DIGEST_PER_THEME, deadline_s=30)
    lines = [f"## {theme.get('label', '?')}", ""]
    if not docs:
        lines += ["_(无结果)_", ""]
        return lines, None
    for d in docs:
        date = d.date.date().isoformat() if d.date else "-"  # (was em-dash in digest.py; P9 no-em-dash)
        also = d.metadata.get("also_in")
        src = d.source + (f" (+{'+'.join(also)})" if also else "")
        lines.append(f"- **{d.title.strip()[:90]}**  ")
        lines.append(f"  `{src}` · {date} · rank={d.metadata.get('_rank', '?')} · {d.url}")
    lines.append("")
    return lines, docs[0].title.strip()[:48]


def run_digest() -> dict:
    """One weekly digest pass (digest.py main() transplanted). NO-OPS (with a log line) when the
    themes file is absent; else builds the Markdown + a condensed Bark. Row ships DISABLED."""
    themes = _load_digest_themes()
    if not themes:
        log.info("digest: no themes file at %s -> no-op (seed it on the mini to enable)",
                 _DIGEST_THEMES_PATH)
        return {"noop": "no-themes"}

    from penumbra.server import load_sources
    load_sources()
    from penumbra.core import fetcher

    ts = datetime.now()
    md = [f"# Penumbra 周报 · {ts.date().isoformat()}", "",
          f"_跨源去重 + 统一排序 · {len(themes)} 个主题 · 每主题 top {_DIGEST_PER_THEME}_", ""]
    highlights: list = []
    for theme in themes:
        try:
            sec, top = _digest_theme_section(fetcher, theme)
        except Exception as exc:  # noqa: BLE001 -- one theme must not kill the digest
            sec, top = [f"## {theme.get('label', '?')}", "", f"_(error: {exc})_", ""], None
        md += sec
        if top:
            highlights.append(f"{theme.get('label', '?')}: {top}")
        log.info("digest: %s: %s", theme.get("label", "?"), "ok" if top else "empty")

    _DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    body = "\n".join(md)
    (_DIGEST_DIR / "latest.md").write_text(body, encoding="utf-8")
    (_DIGEST_DIR / f"digest-{ts.strftime('%Y%m%d')}.md").write_text(body, encoding="utf-8")

    bark_body = "\n".join(f"- {h}" for h in highlights[:5]) or "本期无新内容"
    _bark(f"Penumbra 周报 · {ts.date().isoformat()}", bark_body, group="Penumbra-Digest",
          level="passive")
    log.info("digest: wrote %s (%d themes); bark sent", _DIGEST_DIR / "latest.md", len(themes))
    return {"themes": len(themes), "highlights": len(highlights)}
