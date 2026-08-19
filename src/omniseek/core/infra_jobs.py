"""Transplanted infra JOBS (P9): the movable services that used to be standalone launchd crons,
now zero-arg job fns the in-process scheduler (omniseek.core.jobs) runs on their declared schedules.

WHY THEY MOVED (the derived architecture): the test for what stays OUTSIDE the organ is "must this
still run when the organ is dead?" These do NOT -- a source-health probe, a wewe-rss feed check, an
xhs session warm, log rotation, the monthly curator pass, the weekly source audit, the weekly
digest are all self-maintenance of a LIVE eye. So each old script's LOOP body becomes a job fn here;
its hard-won lessons (the consecutive-fail thresholds, the cooldowns, the state-file formats, the
safety rationales) are transplanted verbatim, because those lessons are what kept the fleet quiet
and safe. The push path changes from scripts/_sentinel_common (urllib, out-of-process) to
omniseek.core.notify (httpx, in-process), keeping the same fail-open contract.

Each job runs INSIDE the writer process on the ONE scheduler thread, wrapped by jobs.run_due_jobs in
its own try/except (a failing job never stops the rest, and the scheduler alerts on an unhandled
exception with a 24h cooldown). The state files these jobs keep are the SAME paths the old crons
used, so the mini's existing state carries across the migration unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_STATE = Path.home() / ".omniseek" / "state"
_LOG_DIR = Path.home() / ".omniseek" / "logs"


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


def _alert(title: str, body: str, **_ignored) -> None:
    """Fail-open in-process ALARM. Retired Bark's group/level hints are absorbed and ignored.

    2026-08-12: Bark was deleted from the fleet. It had been unreachable from the mini (three
    probes, the connection never establishing, 20s timeouts, nine "push failed" lines across the
    logs) while EVERY infra alarm pushed to it and to nothing else. Alarms were written, counted,
    logged as pushed, and delivered nowhere, which is worse than having no alarms because the quiet
    reads as calm. One channel now, WeCom, which answers in 0.06s and is where the operator actually
    reads. The contract is unchanged: never raise, a broken alarm must not break the job that
    raised it."""
    try:
        from omniseek.core import notify
        notify.alert(title, body)
    except Exception as exc:  # noqa: BLE001 -- a push failure never breaks a job
        log.debug("infra_jobs alert swallowed (%s)", exc)


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
# SuccessfulExit=false, so a clean quit is not auto-relaunched); a FAILED self-heal escalates to an alert
# (the CDP safety net itself is down).
# State: ~/.omniseek/state/health-watchdog-state.json (unchanged path -> the mini's state carries).
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

# Rows in the watchdog state that are NOT sources. _health_track writes one "_cdp:<label>" row per
# CDP-Chrome instance alongside the per-source rows, so the state file mixes two namespaces in one
# flat dict. The leading "_" is the RESERVED infra namespace (no adapter name starts with one), which
# is why the prune below is a NAMESPACE rule and not a hand-kept literal list that rots the next time
# a CDP instance is added -- and why an unknown infra row fails SAFE (kept, and reported).
_INFRA_ROW_PREFIX = "_"


def _prune_stale_health_rows(state: dict, live: set) -> dict:
    """Drop the watchdog rows of sources that are NO LONGER REGISTERED. Returns what it saw.

    THE KEY IS REGISTRATION, deliberately -- not enablement, and not "was probed this run":
      - run_source_health probes fetcher.all_adapter_names() with NO profile filter, so a source the
        deployer's profile DISABLES is still registered, still probed, and still rewrites its row
        every run. Pruning by enablement would delete a counter the same run re-creates, and would
        throw away the fail history of every source a profile happens to gate (on the mini today the
        walled tier is deny-by-default and the profile re-enables it: 0 sources are profile-disabled,
        so that prune would look harmless right up until a profile changed).
      - the 6h fast lane probes only the non-CDP sources. Pruning by "probed" would delete every CDP
        source's row twice a day and hand the daily lane a blank slate.
    A "_"-prefixed row is infrastructure and is never a prune candidate.

    Fail-safe: an EMPTY ``live`` means the registry did not load, not that every source vanished, so
    it prunes nothing and says so. PURE: mutates ``state`` in place, no IO, no clock, no network.

    Returns {"pruned": [names dropped], "orphan_infra": [infra rows no current job writes],
             "skipped": "" | reason}.
    """
    def _d(key: str) -> dict:
        v = state.get(key)
        return v if isinstance(v, dict) else {}

    fails, status, alerts = _d("fails"), _d("last_status"), _d("_alerts")
    unmeasured = _d("unmeasured")  # the third state gets pruned like the other two, or it fossilizes
    rows = set(fails) | set(status) | set(unmeasured)
    written_infra = {f"_cdp:{label}" for label in _CDP_INSTANCES}
    orphan_infra = sorted(r for r in rows
                          if r.startswith(_INFRA_ROW_PREFIX) and r not in written_infra)
    if not live:
        return {"pruned": [], "orphan_infra": orphan_infra, "skipped": "empty-registry"}
    stale = sorted(r for r in rows if not r.startswith(_INFRA_ROW_PREFIX) and r not in live)
    for n in stale:
        fails.pop(n, None)
        status.pop(n, None)
        unmeasured.pop(n, None)
        alerts.pop(f"down:{n}", None)
    return {"pruned": stale, "orphan_infra": orphan_infra, "skipped": ""}


def _health_probe(adapter) -> tuple[Optional[bool], str]:
    """BOUNDED health_check (per-source hard timeout -> can never hang the loop) with one in-run
    retry to absorb transient blips. Routes through the same fetcher.health_check_bounded primitive
    the live MCP probe uses.

    THREE states, not two. health_check_bounded returns None when OUR probe hit its hard timeout,
    which is not the same fact as the source answering with a failure. This function used to
    declare ``-> tuple[bool, str]`` and flatten None into False on the last line, so a probe we
    could not complete was recorded as a source that is down. On 2026-08-19 that flattening had
    ten sources (all six Stack Exchange slices, github, github_trending, github_releases, reddit)
    soft-skipped out of every broad sweep while a paced re-probe found nine of them healthy.
    Propagating None keeps "we did not measure" separate from "it failed"; the caller must not
    move the consecutive-fail counter on None."""
    from omniseek.core import fetcher
    ok: Optional[bool] = False
    msg = "?"
    for attempt in (1, 2):
        ok, msg = fetcher.health_check_bounded(adapter)
        if ok:
            return True, str(msg)
        if attempt == 1:
            time.sleep(3)
    return (None if ok is None else False), str(msg)


def _health_track(name: str, ok: Optional[bool], msg: str, fails: dict, alerts: dict,
                  newly_down: list, recovered: list) -> None:
    """Shared consecutive-fail / recovery bookkeeping for one probed entity (cron_watchdog._track).

    ``ok is None`` means OUR probe timed out, so we learned nothing about this source. The counter
    must not move in EITHER direction: incrementing it quarantines a healthy source (the 2026-08-19
    ten-source false-down), and zeroing it would erase a real failure streak on a coincidental
    timeout. The run body records the name in ``unmeasured`` so the gap stays visible."""
    if ok is None:
        return
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
        spec = importlib.util.spec_from_file_location("omniseek_services_ij", svc_path)
        svc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(svc)
        by_port = {r["port"]: r["label"] for r in svc.by_layer("cdp")}
        return {"9222-shared": by_port.get(9222), "9223-xhs": by_port.get(9223)}
    except Exception as exc:  # noqa: BLE001 -- fall back to the known labels if services.py is unreadable
        log.debug("cdp label map from services.py failed (%s); using literals", exc)
        return {"9222-shared": "com.omniseek.cdp.cn-forums", "9223-xhs": "com.omniseek.cdp.xhs"}


def _heal_cdp_chrome() -> list[str]:
    """Relaunch any DEAD CDP Chrome BEFORE probing, so the probes see live browsers (the chrome
    launchd agents don't auto-relaunch a clean exit). Returns the launchd services it FAILED to bring
    back -- a failed self-heal means the CDP safety net itself is down, so the caller escalates those
    to an alert (otherwise the failure is silent)."""
    import subprocess
    from omniseek.core.sources.walled._cdp import cdp_health
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


def run_source_health_fast() -> dict:
    """The fast lane: probe ONLY the non-CDP sources (cheap, no browser), scheduled every 6h so a
    dead source surfaces in ~6-12h instead of the daily lane's worst-case ~48h. Shares the watchdog
    state + consecutive-fail counter with the full daily run; CDP heal/probe stays daily + serial
    (gentle + account-safe). See run_source_health(scope=...)."""
    return run_source_health(scope="noncdp")


def run_source_health(scope: str = "all") -> dict:
    """One source-health run (the cron_watchdog 源体检 half, minus the dissolved cron-liveness half):
    heal dead CDP Chrome, probe every source's health_check (non-CDP concurrent, CDP serial), track
    consecutive fails, and alert newly-down / recovered (cooldown-gated). Returns a small summary.

    scope="all" (daily): the full run (heal CDP, probe non-CDP + CDP + infra). scope="noncdp" (the
    6h fast lane): probe ONLY non-CDP sources, skip the CDP heal/probe/infra entirely, and MERGE the
    result into the stored last_status (never dropping the CDP entries the daily run owns)."""
    from omniseek.server import load_sources
    from concurrent.futures import ThreadPoolExecutor
    load_sources()
    from omniseek.core import fetcher

    full = (scope != "noncdp")
    heal_failed = _heal_cdp_chrome() if full else []  # CDP heal is daily-only (needs a browser)

    names = sorted(fetcher.all_adapter_names())
    # Skip RETIRED sources: a curator retire (reversible overlay, reason begins "retired...") parks a
    # source as intentionally dead. Probing it just re-confirms "down" every run -- a standing false
    # alarm on something we deliberately retired. The retire IS the decision; health-probing it is noise.
    # Reversible: a rollback clears the overlay and the source is probed again next run.
    def _is_retired(n: str) -> bool:
        # fetcher.retired_reason is the ONE retire derivation. get_adapter may return None if a
        # concurrent unregister raced the all_adapter_names snapshot; with no adapter to read, a
        # vanished source is simply not-probed this run (not "retired").
        a = fetcher.get_adapter(n)
        return bool(fetcher.retired_reason(a)) if a is not None else False
    retired = {n for n in names if _is_retired(n)}
    live = [n for n in names if n not in retired]
    noncdp = [n for n in live if n not in _CDP_SOURCES and n not in _SEALED_SOURCES]
    cdp = [n for n in live if n in _CDP_SOURCES] if full else []

    def probe_named(n):
        return n, _health_probe(fetcher.get_adapter(n))

    results: dict[str, tuple[bool, str]] = {}
    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as ex:
        for n, res in ex.map(probe_named, noncdp):
            results[n] = res
    for n in cdp:  # CDP: SERIAL (one browser tab at a time -- gentle + account-safe)
        results[n] = _health_probe(fetcher.get_adapter(n))

    infra: dict[str, tuple[bool, str]] = {}
    if full:
        from omniseek.core.sources.walled._cdp import cdp_health
        for label, url in _CDP_INSTANCES.items():
            try:
                infra[label] = cdp_health(url) if url else cdp_health()
            except Exception as exc:  # noqa: BLE001
                infra[label] = (False, f"{type(exc).__name__}: {exc}")

    state = _load_state(_HEALTH_STATE)
    alerts = state.get("_alerts", {})
    fails = state.get("fails", {})  # name -> consecutive fail count
    pushed = 0

    # PRUNE the rows of sources that are no longer registered. Placed HERE, at the top of the state
    # phase: `alerts` / `fails` above are bound to the very dicts inside `state`, and `last_status`
    # is copied into `snap` further down, so pruning any later would be silently overwritten by this
    # run's own writes. VISIBLE by construction -- a run that quietly drops rows is exactly how state
    # disappears without anyone learning, and the job's return value is discarded by the scheduler
    # (jobs._run_with_budget calls row.fn() and throws the result away), so the log line alone would
    # reach nobody. It fires ONCE per removal: the next run has nothing left to prune.
    _pr = _prune_stale_health_rows(state, set(names))
    if _pr["pruned"]:
        log.warning("source-health[%s]: pruned %d watchdog row(s), source no longer registered: %s",
                    scope, len(_pr["pruned"]), ", ".join(_pr["pruned"]))
        _alert(f"体检状态清理 · {len(_pr['pruned'])}",
               "已从 health-watchdog-state 移除(源已不在注册表):\n"
               + "\n".join(f"- {n}" for n in _pr["pruned"]))
        pushed += 1
    if _pr["skipped"]:
        log.warning("source-health[%s]: prune SKIPPED (%s) -- every row kept", scope, _pr["skipped"])
    if _pr["orphan_infra"]:  # kept (reserved namespace), but surfaced so a fossil row cannot hide
        log.warning("source-health[%s]: %d infra row(s) kept that no current job writes: %s",
                    scope, len(_pr["orphan_infra"]), ", ".join(_pr["orphan_infra"]))

    # Escalate a FAILED self-heal -- the CDP safety net is down; every CDP source on that instance
    # stays silently offline until the operator intervenes.
    for svc in heal_failed:
        if _should_alert(f"heal_fail:{svc}", alerts, REALERT_COOLDOWN_S):
            _alert(f"CDP 自愈失败 · {svc}",
                  f"launchctl 无法拉起 {svc} -- 该实例所有 CDP 源静默离线,需登录 Mac mini 检查",
                  group="OmniSeek-Health")
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
        _alert(f"源故障 · {len(newly_down)}", body)
        pushed += 1
    if recovered:
        _alert(f"源恢复 · {len(recovered)}", "\n".join(f"- {n}" for n in recovered))
        pushed += 1

    # DEGRADED transition: a multi-feed bundle can be healthy (ok=True) yet have lost members. Those
    # never enter newly_down, so member rot was invisible until ALL feeds died. Track the degraded set
    # across runs and alert on a source's full->degraded transition once (a bundle names its dead feeds in
    # the health message via the "degraded" marker). Recovery to full clears it silently.
    degraded_now = {n for n in probed if results[n][0] and "degraded" in results[n][1].lower()}
    prev_degraded = set(state.get("degraded", []))
    newly_degraded = [n for n in sorted(degraded_now) if n not in prev_degraded]
    if newly_degraded:
        body = "\n".join(f"- {n}: {results[n][1][:64]}" for n in newly_degraded)
        _alert(f"源降级 · {len(newly_degraded)}", body)
        pushed += 1
    # SCOPE-AWARE, by the same rule the snapshot below already follows: a scoped run may only speak
    # about what it PROBED. The 6h fast lane never probes the CDP sources, so replacing the whole set
    # from it dropped every CDP source out of `degraded`; the next daily run re-added them, they read
    # as NEWLY degraded, and 源降级 re-fired twice a day forever. The set is a partial observation
    # being written as if it were a total one, which is exactly the defect the last_status merge
    # fixes one block down. This key was simply left behind.
    # Full run rebuilds (and thereby self-cleans a source that left the registry); the fast lane
    # keeps what it could not see.
    state["degraded"] = sorted(degraded_now if full
                               else (prev_degraded - set(probed)) | degraded_now)

    state["fails"] = fails
    state["_alerts"] = alerts
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    # Full run rebuilds the snapshot; the fast (noncdp) lane MERGES so the CDP + infra entries the
    # daily run owns are preserved (a from-scratch snap would drop them -> list_sources 'unknown').
    snap = dict(state.get("last_status", {})) if not full else {}
    snap.update({n: results[n][0] for n in probed})
    for label, (ok, _msg) in infra.items():
        snap[f"_cdp:{label}"] = ok
    state["last_status"] = snap
    # The third state, kept as its OWN map. last_status can hold None, but a consumer reading only
    # its KEY set (fetcher._watchdog_health does exactly that) cannot tell None from True, which
    # would turn "we could not measure" into a reported ok: a false green replacing the false red.
    # Same merge rule as snap so the fast lane does not erase the daily lane's rows.
    unm = dict(state.get("unmeasured", {})) if not full else {}
    for n in probed:
        if results[n][0] is None:
            unm[n] = str(results[n][1])[:200]
        else:
            unm.pop(n, None)
    state["unmeasured"] = unm
    # A retired source is no longer probed -> drop its stale fail / status / alert entries so a parked
    # source self-cleans instead of freezing at a stale "down" (no manual state edit ever needed).
    for r in retired:
        fails.pop(r, None)
        snap.pop(r, None)
        unm.pop(r, None)
        alerts.pop(f"down:{r}", None)
    _save_state(_HEALTH_STATE, state)

    n_green = sum(1 for n in probed if results[n][0])
    log.info("source-health[%s]: %d/%d healthy (%d CDP); newly_down=%d recovered=%d alert=%d",
             scope, n_green, len(probed), len(cdp), len(newly_down), len(recovered), pushed)
    return {"healthy": n_green, "probed": len(probed), "newly_down": len(newly_down),
            "recovered": len(recovered), "alert": pushed, "pruned": _pr["pruned"]}


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# JOB: wechat2rss probe   (transplanted from wewerss_watchdog.py; every:1800s)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# RENAMED 2026-08-12, from wewerss-probe. It has monitored wechat2rss and NOT wewe-rss since the
# self-hosted install was retired, and on 2026-08-12 that install was deleted from the mini
# outright. A job whose name points at something that no longer exists is a trap: the next reader
# finds "wewerss-probe" in the roster, goes looking for wewe-rss, finds nothing, and either
# "repairs" a healthy job or deletes a live one. The name now says what it probes.
#
# wechat2rss feed health check. (原 wewe-rss 双轨监控; 2026-06-06 弃用 AI寒武纪、退役 wewe-rss 自建后,
# 只剩这一轨。) Monitors 4 free wechat2rss.xlab.app feeds (no SLA): each reachable + whether the whole
# service froze. Any anomaly -> an alert. A 6h cooldown prevents a 30-min re-alert flood. There is no
# heal (a free public source has no local process to relaunch).
_WECHAT2RSS_STATE = _STATE / "wechat2rss-last-alert.json"
_WECHAT2RSS_COOLDOWN_S = 6 * 3600
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
# The probe reads a PREFIX and only needs the newest <pubDate>; 256 KB covers many
# entries even on the chattiest of these accounts. The timeout then bounds a genuinely
# unreachable host rather than a large-but-healthy one.
_WECHAT2RSS_PREFIX_BYTES = 256 * 1024
_WECHAT2RSS_TIMEOUT_S = 25


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
            with urllib.request.urlopen(req, timeout=_WECHAT2RSS_TIMEOUT_S) as resp:
                # BOUNDED PREFIX, not the whole feed (2026-08-11). These feeds run 1-2.4 MB and the
                # probe only needs the NEWEST pubDate, which RSS puts near the top. Reading the whole
                # body made the probe's cost scale with the publisher's backlog: measured the same
                # night, one feed took 16.8s against a 12s budget, so the probe raised URLError and
                # raised "unreachable" about a feed that was serving fine. A liveness check whose
                # own cost can exceed its own timeout manufactures its own false alarms.
                body = resp.read(_WECHAT2RSS_PREFIX_BYTES).decode("utf-8", "ignore")
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


def run_wechat2rss_probe() -> dict:
    """One wechat2rss run: probe the 4 feeds, alert on anomaly (6h cooldown). No heal."""
    ok, msg = check_wechat2rss_feeds()
    log.info("wechat2rss: %s : %s", "OK" if ok else "FAIL", msg)
    state = _load_state(_WECHAT2RSS_STATE)
    alerts = state.get("_alerts", {})
    pushed = 0
    if not ok and _should_alert("wechat2rss_down", alerts, _WECHAT2RSS_COOLDOWN_S):
        _alert("wechat2rss feed 异常",
              f"{msg}。免费第三方源, 可能要换源或换号, 检查 wechat2rss.xlab.app。", group="OmniSeek")
        pushed = 1
    state["_alerts"] = alerts
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    _save_state(_WECHAT2RSS_STATE, state)
    return {"ok": ok, "alert": pushed}


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# JOB: off-machine backup AUDIT   (daily@06:30)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# WHY THIS EXISTS (2026-08-11): the brain's off-machine mirror was dead for FIFTEEN DAYS and nothing
# noticed. Its Windows scheduled task ran on time, failed on time (its remote pinned a LAN alias that
# stopped resolving when the fleet moved to Tailscale), and told nobody, because its exit code had no
# reader. A backup in that state is worse than no backup: it manufactures exactly the confidence that
# stops anyone from checking by hand. 79 notes sat on a single disk while the ledger said "mirrored".
#
# THE ONLY CRITERION THAT SURVIVES THAT FAILURE IS CONTENT AT THE DESTINATION. Not "the task ran",
# not "no error was raised", not "it looked fine last time": every one of those stayed TRUE straight
# through the outage. So this job measures what is actually there:
#   * MIRRORS -- the Windows side writes a heartbeat here after each run carrying every mirror's HEAD
#     and item count. Mini then asks its OWN repo how far behind that HEAD is (ancestor check +
#     rev-list count), which is exact rather than a guess from file counts. A STALE heartbeat is
#     itself an alarm, so a Windows box that simply stops running cannot fail silently either:
#     absence of news is news.
#   * EXTERNAL DRIVE -- read directly (newest wall-*.db.gz under /Volumes/*/omniseek-backups versus
#     the newest local one), because that lane needs no second machine to report on it.
# Read-only and fail-visible: this job never repairs, it only measures and alerts. Every helper
# swallows its own errors, because an audit that can crash is an audit that quietly stops running.
_OFFMACHINE_STATE = _STATE / "offmachine-audit.json"
_OFFMACHINE_HEARTBEAT = _STATE / "offmachine-mirror-heartbeat.json"
_OFFMACHINE_COOLDOWN_S = 6 * 3600
_HEARTBEAT_STALE_S = 3 * 24 * 3600   # the mirror task is daily; 3 quiet days is a fault, not jitter
_WALL_STALE_S = 3 * 24 * 3600        # the wall backup is daily
_MIRROR_LAG_MAX_COMMITS = 60         # a busy writing day stays well under this; 15 days did not
_BACKUPS = Path.home() / ".omniseek" / "backups"
_VOLUMES = Path("/Volumes")
_BACKUP_LOG = Path.home() / ".omniseek" / "logs" / "infra.state-backup.log"
_ALERT_DELIVERY_PATH = _STATE / "alert-delivery.json"
# name -> the LIVE repo each off-machine mirror is supposed to be a copy of.
_MIRROR_TARGETS = {
    "brain": Path.home() / "omniseek-brain",
    "core": Path.home() / "omniseek-maintenance",
}


def _git(root: Path, *args: str, timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", "-C", str(root), *args],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip()
    except Exception:  # noqa: BLE001 -- an audit that raises is an audit nobody runs
        return 1, ""


def _mirror_lag(root: Path, mirror_head: str) -> tuple[str, int]:
    """How far the mirror trails the live repo: ("", n) behind by n commits, or (fault, -1).

    An ancestor check comes FIRST: a mirror head the live repo does not contain means divergence or
    corruption, which is a different (and worse) problem than being behind."""
    if not mirror_head:
        return ("心跳没带 HEAD", -1)
    rc, _ = _git(root, "cat-file", "-e", f"{mirror_head}^{{commit}}")
    if rc != 0:
        return (f"镜像 HEAD {mirror_head[:8]} 在活仓里不存在(分叉或损坏)", -1)
    rc, _ = _git(root, "merge-base", "--is-ancestor", mirror_head, "HEAD")
    if rc != 0:
        return (f"镜像 HEAD {mirror_head[:8]} 不是活仓 HEAD 的祖先(分叉)", -1)
    rc, out = _git(root, "rev-list", "--count", f"{mirror_head}..HEAD")
    if rc != 0:
        return ("", 0)
    try:
        return ("", int(out))
    except ValueError:
        return ("", 0)


def _audit_mirrors(now: float) -> list[str]:
    """Every off-machine git mirror, judged by content rather than by a run log."""
    hb = _load_state(_OFFMACHINE_HEARTBEAT)
    if not hb:
        return ["镜像心跳缺失:Windows 侧镜像任务从未成功写回过"]
    faults: list[str] = []
    stamp = str(hb.get("at") or "")
    try:
        age = now - datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return [f"镜像心跳时间戳无法解析:{stamp!r}"]
    if age > _HEARTBEAT_STALE_S:
        faults.append(f"镜像心跳已 {age / 86400:.1f} 天未更新(Windows 侧镜像任务可能已死)")
    rows = hb.get("mirrors") or {}
    for name, root in _MIRROR_TARGETS.items():
        row = rows.get(name) or {}
        if not row:
            faults.append(f"{name}:心跳里没有这面镜子")
            continue
        if not row.get("ok"):
            faults.append(f"{name}:上次镜像更新失败({str(row.get('error') or '')[:90]})")
            continue
        fault, behind = _mirror_lag(root, str(row.get("head") or ""))
        if fault:
            faults.append(f"{name}:{fault}")
        elif behind > _MIRROR_LAG_MAX_COMMITS:
            faults.append(f"{name}:镜像落后活仓 {behind} 个 commit")
    return faults


def _newest_mtime(paths) -> float:
    best = 0.0
    for p in paths:
        try:
            best = max(best, p.stat().st_mtime)
        except OSError:
            continue
    return best


def _removable_volumes_visible() -> bool:
    """Can THIS process see removable volumes at all?

    macOS gates /Volumes/* behind a privacy grant (Files and Folders -> Removable Volumes, or Full
    Disk Access) that a launchd-spawned process does not inherit from an interactive shell. When the
    grant is missing the directory reads as EMPTY rather than raising, so a naive check concludes
    "there is no backup on the drive" when the truth is "I am not allowed to look". Measured
    2026-08-11 with the same script a minute apart: under launchd off-machine=NONE, over ssh
    off-machine=ext:OmniSeekRecovery. Distinguishing the two is the whole point, because they need
    opposite responses (grant a permission vs plug in a drive)."""
    try:
        return any(p.name != "Macintosh HD" and p.is_dir() for p in _VOLUMES.iterdir())
    except OSError:
        return False


def _scheduled_offmachine_verdict() -> str:
    """What the BACKUP ITSELF last recorded about its off-machine destination ("" if unknown).

    This is not "did the task run" (the thing this whole job exists to distrust): it is the lane's
    own verdict about the DESTINATION, written by the process that has the same view of the disk as
    the scheduled run. That makes it the one honest signal available from inside a blinded process."""
    try:
        lines = _BACKUP_LOG.read_text(encoding="utf-8", errors="ignore").strip().splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        if "off-machine=" in line:
            return line.rsplit("off-machine=", 1)[1].strip()
    return ""


def _declared_resident_labels() -> list[str]:
    """The launchd labels the service REGISTRY says must be resident (status="running").

    scripts/services.py is the single declaration of the fleet and is deliberately importable
    without omniseek (the external sentinel depends on that), so the registry is read rather than
    duplicated: a hardcoded copy here would drift from the thing it is supposed to guard, which is
    the exact failure this check exists to catch."""
    try:
        scripts_dir = Path(__file__).resolve().parents[3] / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import services  # noqa: PLC0415 -- deliberately late + guarded
        rows = []
        for layer in ("organ", "cdp", "infra"):
            rows.extend(services.by_layer(layer))
        return [r["label"] for r in rows
                if r.get("status") == "running" and r.get("repo") == "core"]
    except Exception as exc:  # noqa: BLE001 -- an unreadable registry must not break the audit
        log.debug("fleet check: registry unreadable (%s)", exc)
        return []


def _loaded_labels() -> set:
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return set()
        return {line.split("\t")[-1].strip() for line in out.stdout.splitlines()[1:] if line.strip()}
    except Exception:  # noqa: BLE001
        return set()


def _audit_launchd_fleet() -> list[str]:
    """Is every service the registry declares resident actually LOADED?

    WHY (2026-08-12): com.omniseek.infra.sentinel, the ONE external watchdog (the hands that restart
    the organ and its browsers when the organ's own code is broken), was absent from launchd for
    FOUR DAYS. Its plist sat on disk, the registry declared it resident, its log simply stopped, and
    nothing noticed, because the thing that would have noticed IS the watchdog. Its two alarm state
    files still carried timestamps from eight days earlier, which read as "no incidents" rather than
    "no observer". A guard that can silently cease to exist needs an outside check on its EXISTENCE,
    not just on its verdicts."""
    declared = _declared_resident_labels()
    if not declared:
        return []          # registry unreadable: say nothing rather than cry wolf
    loaded = _loaded_labels()
    if not loaded:
        return []          # launchctl unavailable (not macOS, or sandboxed): unauditable, not broken
    missing = [lbl for lbl in declared if lbl not in loaded]
    if missing:
        return [f"launchd 舰队缺员:{', '.join(missing)} 在注册表里声明常驻,但没有加载"]
    return []


def _audit_alert_delivery() -> list[str]:
    """Is the siren connected? An alarm nobody receives is the failure mode this whole job exists
    for, one level up: every other check here reports through the very lane being checked."""
    try:
        row = json.loads(_ALERT_DELIVERY_PATH.read_text(encoding="utf-8"))
    except OSError:
        return []          # nothing has alarmed yet; silence here is honest
    except Exception:      # noqa: BLE001
        return ["告警投递台账无法解析"]
    streak = int(row.get("undelivered_streak") or 0)
    if streak:
        return [f"最近 {streak} 条告警一条都没送达(所有通道都失败),最后一条:{str(row.get('title'))[:60]}"]
    return []


def _audit_external_drive(now: float) -> list[str]:
    """The wall's external-drive lane, read at the destination."""
    local_newest = _newest_mtime(_BACKUPS.glob("wall-*.db.gz"))
    if not local_newest:
        return ["本地一个 wall 备份都没有(state_backup 可能没在跑)"]
    faults: list[str] = []
    if now - local_newest > _WALL_STALE_S:
        faults.append(f"本地最新 wall 备份已是 {(now - local_newest) / 86400:.1f} 天前")
    verdict = _scheduled_offmachine_verdict()
    if verdict == "NONE":
        faults.append("上一次 state_backup 报 off-machine=NONE:计划路径下没有任何机外目的地"
                      "(launchd 进程若无「可移动卷」授权,/Volumes 对它是空的)")
    offsite_newest = _newest_mtime(_VOLUMES.glob("*/omniseek-backups/wall-*.db.gz"))
    if not offsite_newest:
        if not _removable_volumes_visible():
            faults.append("本进程看不到可移动卷(macOS 隐私授权),外置盘这条 lane 无法从这里核对;"
                          "要么给 launchd 服务授权,要么以此为准信上面那条 state_backup 的判决")
        else:
            faults.append("外置盘上没有任何 wall 备份(盘没挂上,或 off-machine lane 没跑成)")
        return faults
    lag_days = (local_newest - offsite_newest) / 86400.0
    if lag_days > 2.0:
        faults.append(f"外置盘最新 wall 比本地旧 {lag_days:.1f} 天")
    return faults


def run_offmachine_audit() -> dict:
    """Audit every off-machine copy by measuring the destination, never by trusting a run log."""
    now = time.time()
    # The siren is audited FIRST and separately: every other check here reports through the very
    # lane being checked, so if it is disconnected none of the rest can reach anyone anyway.
    faults = (_audit_alert_delivery() + _audit_launchd_fleet()
              + _audit_mirrors(now) + _audit_external_drive(now))
    log.info("offmachine-audit: %s", " / ".join(faults) if faults else "所有机外目的地内容已核对")
    state = _load_state(_OFFMACHINE_STATE)
    alerts = state.get("_alerts", {})
    pushed = 0
    if faults and _should_alert("offmachine_degraded", alerts, _OFFMACHINE_COOLDOWN_S):
        _alert("机外备份异常", " / ".join(faults)[:400])
        pushed = 1
    state["_alerts"] = alerts
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    state["faults"] = faults
    _save_state(_OFFMACHINE_STATE, state)
    return {"ok": not faults, "faults": faults, "alert": pushed}


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# JOB: session warmer   (transplanted from session_warmer.py; daily@09:17,14:17,19:17)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# WHY THIS EXISTS (root cause, 2026-06-22): the logged-in xhs sessions silently DEGRADE when the
# browser sits idle. The cookie that LOOKS like login (web_session, valid ~1yr) stays put, but the
# SHORT-LIVED anti-bot security cookies (acw_tc ~daily, websectiga, sec_poison_id, xsecappid) EXPIRE
# and are only re-minted by actual browser navigation. Once they lapse OmniSeek goes DARK on those
# sources. This warmer drives each Chrome through light, READ-ONLY human-like activity (home ->
# scroll -> one search), which re-mints the security cookies + keeps search-auth alive. It ALSO
# doubles as the health probe: a genuinely degraded session alerts the operator.
#
# SAFETY (byte-preserved): read-only (navigation + scroll only -- never like/follow/comment, the
# 风控 write-risk path), jitter-paced, active-hours only (08-23 mini local), and it honours the same
# ~/.omniseek/state/cdp-maintenance pause flag cdp_keepalive uses (so it never fights a manual VNC
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


# The 9222 shared-Chrome FORUM sources (cn-forums Chrome) had NO proactive maintenance: only the
# xhs/douyin Chromes were warmed above, so a silently-expired 一亩三分地 / zhihu session stayed dark
# until a user query hit it (the root cause of the 2026-07-06 mis-diagnosis). Warm them the SAME way
# the reactive path heals: a benign search THROUGH the adapter. An autofill-backed source (login_url
# set, e.g. yipinsanfendi) self-heals inside search() and alerts on its own if that relogin fails; a
# non-autofill source (zhihu: QR/SMS login, cannot auto-heal) treats a 0-result warm as its proactive
# fail-loud signal, and the alert below is the operator's cue to VNC re-login. (label, source, query.)
_FORUM_WARMERS = [
    ("一亩三分地", "yipinsanfendi", "实习"),
    ("知乎", "zhihu", "读博"),
    # douban: non-autofill (account/password login, no login_url) like zhihu, so a 0-result warm is its
    # proactive fail-loud signal and the Bark below is the cue to VNC re-login the 9222 douban session.
    ("豆瓣小组", "douban_groups", "上海租房"),
]


def _warm_forum_one(source_name: str, label: str, query: str) -> dict:
    """Health-probe + warm ONE 9222 forum source by running a benign search through its adapter
    (which self-heals if it is autofill-backed). Returns a status dict shaped like _warm_one's, so it
    flows through the same log / Bark / state machinery: ``notes`` reuses the result count, and
    ``self_heals`` marks a source whose reactive search path already Barks (so the warmer skips a
    duplicate Bark for it). Never raises (per-instance isolation)."""
    from omniseek.core.fetcher import get_adapter
    res = {"label": label, "ok": False, "reason": "", "notes": 0,
           "acw_tc": False, "web_session": False, "self_heals": False}
    ad = get_adapter(source_name)
    if ad is None:
        res["reason"] = f"adapter '{source_name}' not registered"
        return res
    res["self_heals"] = bool(getattr(ad, "login_url", ""))
    try:
        docs = ad.search(query, 3) or []
    except Exception as exc:  # noqa: BLE001 -- per-instance isolation
        res["reason"] = f"search raised: {exc!r}"
        return res
    res["notes"] = len(docs)
    res["ok"] = len(docs) > 0
    if not res["ok"]:
        res["reason"] = f"degraded: {source_name} search returned 0 (logged out / blocked / empty)"
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

    # 9222 forum sources: warm + health-probe via each adapter's OWN search (self-heals if
    # autofill-backed). OUTSIDE the sync_playwright block above -- search() drives its own cdp_call
    # worker thread, which must not nest inside this thread's playwright context. get_adapter needs
    # the source modules imported, so bootstrap the registry first (idempotent; how every other job
    # populates it) instead of relying on the ambient service-startup load.
    try:
        from omniseek.server import load_sources
        load_sources()
        forum_ready = True
    except Exception as exc:  # noqa: BLE001 -- forum-warm is best-effort; never crash the XHS warm
        log.warning("session-warmer: load_sources failed (%s) -> skip forum warm", exc)
        forum_ready = False
    if forum_ready:
        for label, source_name, q in _FORUM_WARMERS:
            if only and not (label in only or source_name in only):
                continue
            r = _warm_forum_one(source_name, label, q)
            results.append(r)
            log.info("session-warmer %s: ok=%s notes=%s self_heals=%s %s",
                     label, r["ok"], r["notes"], r["self_heals"],
                     ("| " + r["reason"]) if r["reason"] else "")
            _jsleep(3.0, 6.0)

    degraded = [r for r in results if not r["ok"]]
    for r in degraded:
        if r.get("self_heals"):
            continue  # autofill-backed: the reactive search path already Barked on relogin failure
        if _should_alert(f"session_degraded:{r['label']}", alerts, _WARMER_COOLDOWN_S):
            _alert(f"{r['label']} session 退化",
                  f"暖号验证失败:{r['reason']}。登录态可能已失效,需 VNC 进 mini 重新扫码登录该账号"
                  f"({r['label']} 的 Chrome 窗口)。", group="OmniSeek-Health")

    state["_alerts"] = alerts
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["last_results"] = [{k: r[k] for k in ("label", "ok", "notes", "acw_tc", "reason")} for r in results]
    _save_state(_WARMER_STATE, state)
    log.info("session-warmer: warmed=%s degraded=%s",
             [r["label"] for r in results if r["ok"]], [r["label"] for r in degraded])
    return {"warmed": [r["label"] for r in results if r["ok"]],
            "degraded": [r["label"] for r in degraded]}


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# JOB: log rotation   (transplanted from omniseek_http_watchdog.py rotation half; daily@04:50)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# Copytruncate any oversized log in ~/.omniseek/logs so one chatty source can't grow a log unbounded
# (eye-http.err reached 63MB from a reddit Arctic-Shift 429 storm). launchd opens these redirects
# with O_APPEND (verified 2026-06-20: truncate-in-place stayed small, no sparse hole), so this is
# safe with the writer still running: it resumes appending at the new EOF, NO restart needed. The
# last _LOG_KEEP_TAIL bytes are kept as <name>.1 for recent context.
_LOG_MAX_BYTES = 30 * 1024 * 1024   # copytruncate any omniseek log over 30MB
_LOG_KEEP_TAIL = 4 * 1024 * 1024    # keep the last 4MB of recent context after rotating


def rotate_logs() -> int:
    """Copytruncate every oversized log in ~/.omniseek/logs. Returns how many it rotated. One bad log
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
# JOB: nserc cache prime   (monthly@1-03:30; keep the bulk-CSV source warm OFF the query path)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# WHY (2026-07-14, approved by the operator as a standing job): nserc_awards is a BULK source — its data is
# one ~56MB annual CSV, pulled at most monthly and cached (the CS/AI subset, ~3.6k docs) so queries
# are zero-network. The catch: the cold 56MB pull takes ~96s (routed via a fast node in the mini's
# mihomo, since the mainland-direct link throttles bulk to ~116KB/s), which BLOWS OmniSeek's 90s
# single-source deadline. So if the cache ever expires and a QUERY triggers the refetch, it times out
# mid-pull, never caches, and stays stuck. This job DECOUPLES the refetch from the query path: it runs
# the pull in the background monthly and caches with a TTL that OUTLASTS the cadence, so an eye query
# always hits a warm cache and never waits. Fail-open: a failed pull logs + leaves the existing cache
# intact (a stale-but-present cache beats a broken query). Structurally the right shape for any future
# bulk source (a big file that must not be pulled on the synchronous query path).
_NSERC_PRIME_TTL_S = 45 * 86400   # 45d > the monthly cadence, so each monthly refresh always overlaps


def run_nserc_prime() -> dict:
    """Prime the nserc_awards cache OFF the query path: pull the FY bulk CSV, build the CS/AI subset,
    and cache it with a TTL that outlasts the monthly cadence. Fail-open: a raised/empty pull keeps
    the existing cache (never caches an empty) and never crashes the tick. Returns a small summary."""
    from omniseek.core.sources.api.nserc_awards_source import NSERCAwardsAdapter
    from omniseek.core import cache
    a = NSERCAwardsAdapter()
    try:
        docs = a._fetch_filter_build()   # always fetches the 56MB CSV (no cache short-circuit)
    except Exception as exc:  # noqa: BLE001 -- a pull failure keeps the existing cache, never kills the tick
        log.warning("nserc-prime: fetch raised (%s) -> kept existing cache", exc)
        return {"ok": False, "reason": str(exc)[:80]}
    if not docs:
        log.warning("nserc-prime: fetch returned no docs -> kept existing cache")
        return {"ok": False, "docs": 0}
    cache.set_docs(cache.make_key(a.name, "subset", a._YEAR), docs, ttl=_NSERC_PRIME_TTL_S)
    log.info("nserc-prime: cached %d CS/AI docs (ttl %dd)", len(docs), _NSERC_PRIME_TTL_S // 86400)
    return {"ok": True, "docs": len(docs)}


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# JOB: curator monthly pass   (transplanted from curator.py; monthly@1-06:00, ENABLED per the operator)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# THE ONE-LINE RAZOR (every drift violates it): the pass is mechanical-only. It discovers, probes,
# persists, counts, and Barks pure FACTS, rendering ZERO editorial judgment. A spawned AGENT renders
# EVERY verdict (which gap matters, which candidate to pursue, admit/watch/reject). the operator owns only
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
# State: ~/.omniseek/state/curator/curator-loop-state.json (unchanged path).
#
# NOTE (ignition, P9): the ROW is ENABLED per the operator: expect one Bark digest of neutral facts per
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
        from omniseek.core.curator import apply as _apply
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
    from omniseek.server import load_sources
    load_sources()
    from omniseek.core.curator import candidates, discover, evidence, probe, redlines, source_audit  # noqa: F401
    from omniseek.core import fetcher

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
        from omniseek.core.curator import yield_tap
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
        _alert(title, body + "\n\n(中性事实; admit/watch/reject 由采集 agent 判,本巡查不裁决)",
              group="OmniSeek-Curator", level="passive")
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
# JOB: source audit   (transplanted from source_audit.py; weekly@sun-06:00, ENABLED per the operator)
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
# State: ~/.omniseek/state/job.source-audit/state.json (unchanged path).
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
    from omniseek.server import load_sources
    load_sources()
    from omniseek.core.curator import source_audit

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
        _alert(title, body + "\n\n(中性事实; KEEP/WATCH/PRUNE 由审计 agent 判,本哨兵不裁决)",
              group="OmniSeek-Curator", level="passive")
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
# driven), the digest gives the operator a periodic CURATED view: per theme it runs fetcher.search_ranked
# across the relevant sources -> cross-source dedup + unified ranking -> the top items. Output: a
# Markdown digest (saved + archived) + a condensed Bark push. The synthesis is STRUCTURAL (dedup +
# rank + theme grouping), not LLM prose -- the Markdown is the substrate a session turns into prose
# on demand. PUSH CHANNEL: 企业微信 (WeCom) ONLY, per the operator (2026-07-14) -- notify.wecom_push, not Bark.
#
# THE THEMES LIST LEFT THE CODE (P9): it is now DATA at ~/.omniseek/state/digest-themes.json (seeding
# that file on the mini is the CEO's migration step). When the file is ABSENT the job NO-OPS with a
# log line -- it never invents a theme list. Row ships ENABLED (the operator 2026-07-14) but is SAFE on any
# deployment BECAUSE of that no-op: a fresh deploy without themes pushes nothing. It is enabled in CODE
# (not a profile override) on purpose -- a profile file, once present, gates the walled source fleet
# OFF by default (profile.is_source_enabled), so enabling a JOB via the profile would silently disable
# the walled sources; the mini runs profile-less.
# State: ~/.omniseek/state/digests/ (Markdown out) + the themes file (in).
_DIGEST_DIR = _STATE / "digests"
_DIGEST_THEMES_PATH = _STATE / "digest-themes.json"
_DIGEST_PER_THEME = 6


def _load_digest_themes() -> list:
    """The theme rows (each {label, query, sources}) from ~/.omniseek/state/digest-themes.json, or []
    when the file is absent/corrupt. The job no-ops on []. Never a built-in default (the list is
    the operator's DATA, seeded on the mini)."""
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
    themes file is absent; else builds the Markdown + a condensed WeCom (企业微信) push. Row ships
    ENABLED (the no-op makes that safe without a themes file)."""
    themes = _load_digest_themes()
    if not themes:
        log.info("digest: no themes file at %s -> no-op (seed it on the mini to enable)",
                 _DIGEST_THEMES_PATH)
        return {"noop": "no-themes"}

    from omniseek.server import load_sources
    load_sources()
    ts = datetime.now()

    # PRIMARY: an AGENT-synthesized briefing (a frontier LLM with READ-ONLY use of OmniSeek + brain ->
    # insight tied to the operator's goals, not a link list). Fail-open by contract: None -> the mechanical
    # ranked-link digest below (the agent is an enrichment, never a hard dependency).
    mode = "agent"
    try:
        from omniseek.core import briefing
        agent_md = briefing.build_briefing(themes)
    except Exception as exc:  # noqa: BLE001 -- the briefing agent must never crash the job
        log.warning("digest: briefing agent raised (%s) -> link fallback", exc)
        agent_md = None

    if agent_md:
        body = f"# OmniSeek 周报 · {ts.date().isoformat()}\n\n{agent_md}"
        push_body = agent_md  # already concise markdown; wecom_push byte-caps it defensively
    else:
        # FALLBACK: the mechanical cross-source dedup+rank link list (the pre-agent behaviour).
        mode = "links"
        from omniseek.core import fetcher
        md = [f"# OmniSeek 周报 · {ts.date().isoformat()}", "",
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
        body = "\n".join(md)
        push_body = "\n".join(f"- {h}" for h in highlights[:5]) or "本期无新内容"

    _DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    (_DIGEST_DIR / "latest.md").write_text(body, encoding="utf-8")
    (_DIGEST_DIR / f"digest-{ts.strftime('%Y%m%d')}.md").write_text(body, encoding="utf-8")

    # Push to 企业微信 (WeCom, the operator's MAIN channel) ONLY -- NOT Bark (the operator 2026-07-14). The full
    # Markdown is saved above; WeCom carries the briefing (agent) or the top-5 highlights (fallback).
    from omniseek.core import notify
    notify.wecom_push(f"OmniSeek 周报 · {ts.date().isoformat()}", push_body)
    log.info("digest[%s]: wrote %s (%d themes); wecom push sent", mode, _DIGEST_DIR / "latest.md", len(themes))
    return {"themes": len(themes), "mode": mode}
