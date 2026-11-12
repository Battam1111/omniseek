"""Transplanted infra JOBS (P9): the movable services that used to be standalone launchd crons,
now zero-arg job fns the in-process scheduler (omniseek.core.jobs) runs on their declared schedules.

WHY THEY MOVED (the derived architecture): the test for what stays OUTSIDE the organ is "must this
still run when the organ is dead?" These do NOT -- a source-health probe, an xhs session warm, the
idle-browser reaper and log rotation are all self-maintenance of a LIVE eye. (The wechat2rss probe,
the monthly curator pass and the weekly source audit were retired on 2026-10-19: wechat is probed
with every other source by source-health, and the curator tools stay callable by hand. The NSERC
cache prime and the weekly reading list were retired on 2026-10-20, with the NSERC source.) So each
old script's LOOP body becomes a job fn here;
its hard-won lessons (the consecutive-fail thresholds, the cooldowns, the state-file formats, the
safety rationales) are transplanted verbatim, because those lessons are what kept the fleet quiet
and safe. The push path changes from scripts/_sentinel_common (urllib, out-of-process) to
omniseek.core.notify (httpx, in-process), keeping the same fail-open contract.

Each job runs INSIDE the writer process on the ONE scheduler thread, wrapped by jobs.run_due_jobs in
its own try/except (a failing job never stops the rest, and the scheduler alerts on an unhandled
exception with a 24h cooldown). The state files these jobs keep are the SAME paths the old crons
used, so the existing deployment's state carries across the migration unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from omniseek import where

log = logging.getLogger(__name__)

_STATE = where.state()
_LOG_DIR = where.logs()


# ── tiny local state helpers (the _sentinel_common load/save idiom, atomic) ───────────────────────
# These were transplanted from the old external watchdog's helper module (scripts/_sentinel_common,
# retired 10-20 with it) when the crons moved in-process; they keep its atomic load_state/save_state/should_alert so the state-file
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
    # Every infra state file is written here, and their message fields (health "unmeasured" /
    # "unverified" notes, warmer summaries) can quote an exception that carries a full
    # request address. Mask on the way to disk; the caller's dict is left as it was.
    from omniseek import redact as _redact
    data = _redact.redact_obj(data)
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


def _alert(title: str, body: str, *, questions=None, **_ignored) -> None:
    """Fail-open in-process ALARM. Retired Bark's group/level hints are absorbed and ignored.

    2026-08-12: Bark was deleted from the fleet. It had been unreachable from the service host (three
    probes, the connection never establishing, 20s timeouts, nine "push failed" lines across the
    logs) while EVERY infra alarm pushed to it and to nothing else. Alarms were written, counted,
    logged as pushed, and delivered nowhere, which is worse than having no alarms because the quiet
    reads as calm. One channel now, WeCom, which answers in 0.06s and is where the operator actually
    reads. The contract is unchanged: never raise, a broken alarm must not break the job that
    raised it.

    ``questions``: what the operator must do or answer; given, the push goes to the operator
    whatever eye.infra_jobs routes to (see notify.wecom_push)."""
    try:
        from omniseek.core import notify
        if questions:
            notify.alert(title, body, questions=questions)
        else:
            notify.alert(title, body)
    except Exception as exc:  # noqa: BLE001 -- a push failure never breaks a job
        log.debug("infra_jobs alert swallowed (%s)", exc)


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# JOB: source-health probe   (transplanted from cron_watchdog.py 源体检 half; every:21600s, CDP once a day)
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
# ONE job, two lanes (2026-10-19; it used to be two jobs, a daily full run at 05:00 and a 6h fast
# non-CDP run). Every 6h run probes the non-CDP sources, so a dead open-API/RSS source surfaces in
# ~6-12h (N_CONSECUTIVE=2). The CDP lane (heal + serial CDP probes + the Chrome liveness rows) rides
# one run a day, decided by when a run last carried it (state "cdp_lane_at", the run's START epoch;
# see _cdp_lane_due): every fourth run carries it, once a day as before, never twice.
# State: <state>/health-watchdog-state.json (unchanged path -> the existing state carries).
_HEALTH_STATE = _STATE / "health-watchdog-state.json"
N_CONSECUTIVE = 2                       # consecutive failed runs before alerting
PROBE_WORKERS = 8                       # moderate -- 16-wide caused census false-fails
REALERT_COOLDOWN_S = 3 * 24 * 3600      # while still down, re-nag at most every 3d
HEALTH_EVERY_S = 6 * 3600               # the job's interval (jobs.py registers every:21600s, a test pins it)
HEALTH_BUDGET_S = 900                   # the job's wall-clock budget (jobs.py registers it from here); the
                                        # exit-node sweep a run may end with must finish inside it too
CDP_LANE_EVERY_S = 24 * 3600            # the CDP lane's cadence, the old daily full run's

# CDP (logged-in browser) sources -- probed INDIVIDUALLY + SERIALLY (login-state visibility).
# xiaohongshu lives on the ISOLATED 9223 Chrome (小号); the rest on the shared 9222.
# xianyu (2026-10-24): its check is a real search on a 小号 with a daily cap, so it rides the
# daily serial lane, not the 6-hourly one (four checks a day would be four account touches).
_CDP_SOURCES = {"xiaohongshu", "xiaomuchong", "zhihu", "zhihu_users", "yipinsanfendi", "scrape_js_sites",
                "xianyu"}
# CDP-Chrome instances to liveness-check directly (label -> CDP URL). None = default 9222.
_CDP_INSTANCES = {"9222-shared": None, "9223-xhs": "http://127.0.0.1:9223"}
# SEALED sources (switched off on purpose) are not a list kept here: each run reads them off the
# adapters through fetcher.sealed_reason, so the adapter's own flag is the one restore point. A sealed
# source is NOT probed (a probe of a sealed source asks nothing and would only add noise) and its
# stale fail / status / alert rows are dropped, so it never reads or alerts as down.

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
        throw away the fail history of every source a profile happens to gate (on the live deployment today the
        walled tier is deny-by-default and the profile re-enables it: 0 sources are profile-disabled,
        so that prune would look harmless right up until a profile changed).
      - a run without the CDP lane (three of every four 6h runs) probes only the non-CDP sources.
        Pruning by "probed" would delete every CDP source's row on those runs and hand the CDP lane
        a blank slate.
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
    unverified = _d("unverified")  # and the fourth, for the same reason
    fail_at = _d("fail_at")        # the time of each count's last failed check, beside the count
    rows = set(fails) | set(status) | set(unmeasured) | set(unverified) | set(fail_at)
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
        unverified.pop(n, None)
        fail_at.pop(n, None)
        alerts.pop(f"down:{n}", None)
    return {"pruned": stale, "orphan_infra": orphan_infra, "skipped": ""}


def _health_probe(adapter) -> tuple[Optional[bool], str, bool]:
    """BOUNDED health_check (per-source hard timeout -> can never hang the loop) with one in-run
    retry to absorb transient blips. Routes through the same fetcher.ahealth_check_outcome primitive
    the live MCP probe uses, reached from this job's thread through the portal (the service's loop).
    Returns ``(ok, msg, completed)``.

    THREE results, and two kinds of None. ``ok`` True = verified working, False = verified broken,
    None = not verified. ``completed`` tells the two Nones apart: False means OUR probe hit its hard
    timeout (recorded as ``unmeasured``); True means the adapter finished and reported that, by
    design, it asked the upstream nothing this run (a metered quota, a busy declared gate, a breaker,
    an answer from cache; recorded as ``unverified``). Neither is a failure.

    This function used to declare ``-> tuple[bool, str]`` and flatten None into False on the last
    line, so a probe we could not complete was recorded as a source that is down. On 2026-08-19 that
    flattening had ten sources (all six Stack Exchange slices, github, github_trending,
    github_releases, reddit) soft-skipped out of every broad sweep while a paced re-probe found nine
    of them healthy. Propagating None keeps "we did not measure" separate from "it failed"; the caller
    must not move the consecutive-fail counter on None.

    An unverified answer is NOT retried: the adapter chose not to ask (a quota it must not spend, a
    gate that is busy now), so a second call three seconds later would either say the same or spend
    exactly what the first one declined to spend."""
    from omniseek.core import fetcher, portal
    ok: Optional[bool] = False
    msg = "?"
    completed = True
    for attempt in (1, 2):
        ok, msg, completed = portal.run(fetcher.ahealth_check_outcome, adapter)
        if ok:
            return True, str(msg), True
        if ok is None and completed:
            return None, str(msg), True
        if attempt == 1:
            time.sleep(3)
    return (None if ok is None else False), str(msg), completed


def _probe_tapped(name: str, adapter, noted: dict) -> tuple[Optional[bool], str, bool]:
    """``_health_probe`` under a diag tap: every failure record the probe's attempts noted (whatever the
    source's own check armed and drained inside it) lands in ``noted[name]``, so a source that failed
    leaves the addresses it failed on for the exit-node sweep. One list per source: each
    probe thread taps in its own context."""
    from omniseek.core import diag
    with diag.tap() as seen:
        try:
            return _health_probe(adapter)
        finally:
            noted[name] = seen


# "They refuse us" is not "we are broken", and the two were indistinguishable in the alert. A 403
# from a WAF, a rate-ban, or a proof-of-work bot wall is the far end's decision: no code change here
# fixes it, and reading it as a defect sends the reader hunting through our own adapter. The public
# health page has counted `blocked` apart from `down` since 2026-08-18; the internal watchdog had
# not. Measured 2026-09-09: zenodo "restricted due to unusual traffic from your network", cifar.ca
# behind Sucuri, dblp behind an Anubis proof-of-work wall, all reported exactly like a real outage.
_REFUSED_MARKERS = ("403", "401", "forbidden", "unusual traffic", "bot wall", "not a bot",
                    "captcha", "access denied", "rate limit", "too many requests")


def _is_refused(msg: object) -> bool:
    """True when a health message says the FAR END turned us away rather than that WE broke."""
    return any(m in str(msg or "").lower() for m in _REFUSED_MARKERS)


def _health_track(name: str, ok: Optional[bool], msg: str, fails: dict, alerts: dict,
                  newly_down: list, recovered: list, fail_at: Optional[dict] = None,
                  fetched_at: Optional[float] = None, now: Optional[float] = None) -> str:
    """Shared consecutive-fail / recovery bookkeeping for one probed entity (cron_watchdog._track).

    ``ok is None`` means we learned nothing about this source this run: either OUR probe timed out
    (``unmeasured``) or the probe finished without asking the upstream anything (``unverified``).
    The counter must not move in EITHER direction on that alone: incrementing it quarantines a healthy
    source (the 2026-08-19 ten-source false-down), and zeroing it would erase a real failure streak on a
    coincidental timeout or a busy gate. No alert either way. The run body records the name in
    ``unmeasured`` or ``unverified`` so the gap stays visible.

    The one exception is evidence from outside the check (2026-11-07): ``fail_at``
    {name: epoch of its last failed check} is kept here, and ``fetched_at`` is the source's last live
    fetch that worked (fetcher.fetched_ok_at). A None after a fetch that worked LATER than the last
    failed check clears the count like a True would (recovered when it had been reported down).
    Without that, a check answering "not probed" while the listing is cached kept a repaired source
    reading as down for good: the recall ingest refetches on its own schedule and every check met a
    warm cache. A timeout, a busy gate, or no fetch since the failure still moves nothing.
    Returns the reason line when such a fetch cleared the count, else ""."""
    t = time.time() if now is None else now
    if ok is None:
        failed_at = (fail_at or {}).get(name)
        if (fails.get(name, 0) and isinstance(failed_at, (int, float))
                and isinstance(fetched_at, (int, float)) and fetched_at > failed_at):
            if fails[name] >= N_CONSECUTIVE:
                recovered.append(name)
            why = (f"上一次探失败（{_clock(failed_at)}）之后，{_clock(fetched_at)} 成功取数过，"
                   f"连续失败数 {fails[name]} 清零")
            fails[name] = 0
            fail_at.pop(name, None)
            return why
        return ""
    prev = fails.get(name, 0)
    if ok:
        if prev >= N_CONSECUTIVE:
            recovered.append(name)
        fails[name] = 0
        if fail_at is not None:
            fail_at.pop(name, None)
    else:
        fails[name] = prev + 1
        if fail_at is not None:
            fail_at[name] = t
        if fails[name] == N_CONSECUTIVE:
            newly_down.append((name, msg))
        elif fails[name] > N_CONSECUTIVE and _should_alert(f"down:{name}", alerts, REALERT_COOLDOWN_S):
            newly_down.append((name, msg))  # still down -- periodic re-nag
    return ""


def _clock(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%m-%d %H:%M")


def _track_probed(state: dict, results: dict, probed: list, retired: set, fails: dict, alerts: dict,
                  newly_down: list, recovered: list, fetched_ok_at, now: float) -> dict:
    """Run ``_health_track`` over this run's probed sources (retired ones excluded) and return
    {name: reason} for the counts a later live fetch cleared (the reason is also put in front of that
    source's message in ``results``, so its unverified / unmeasured row says why).

    ``fail_at`` lives in ``state`` beside ``fails``. A row that carries a count but no ``fail_at`` (written
    before 2026-11-07) gets ``now``: later than its real last failure, so only a fetch after this run
    can clear it, never one from before the failure. ``fetched_ok_at`` is fetcher.fetched_ok_at
    (a parameter so a replay can pass a record of its own)."""
    fail_at = state.get("fail_at") if isinstance(state.get("fail_at"), dict) else {}
    for n, c in fails.items():
        if c and n not in fail_at:
            fail_at[n] = now
    cleared: dict[str, str] = {}
    for n in probed:
        if n in retired:
            continue  # no fail streak, no alarm; the alive-again transition is handled by the caller
        ok, msg, completed = results[n]
        why = _health_track(n, ok, msg, fails, alerts, newly_down, recovered, fail_at,
                            fetched_ok_at(n) if ok is None else None, now)
        if why:
            cleared[n] = why
            results[n] = (ok, f"{why}；{msg}", completed)  # reason first: the row is cut at 200
    state["fail_at"] = fail_at
    return cleared


def _gate_held_rounds(state: dict, results: dict, probed: list, fails: dict) -> None:
    """A source whose check said it failed, re-read as "not verified" only because one of OmniSeek's
    gates also refused a request (``_probe.held_by_gate``), N_CONSECUTIVE runs in a row, is reported
    DOWN from that run on (2026-10-20). lobste.rs read "not verified" for two days
    while its host completed no handshake at all. A check that asked nothing (a busy gate in front of
    the whole check, a metered quota) is not this and stays not verified. The round count is kept in
    the state's ``gate_held`` map (merged like the other maps, so a run that did not probe a source
    keeps its count); any other answer clears it. Mutates ``results`` and ``fails`` in place."""
    from omniseek.core import _probe
    rounds = state.get("gate_held") if isinstance(state.get("gate_held"), dict) else {}
    for n in probed:
        ok, msg, completed = results[n]
        if not (ok is None and completed and _probe.held_by_gate(msg)):
            rounds.pop(n, None)
            continue
        rounds[n] = int(rounds.get(n, 0)) + 1
        if rounds[n] >= N_CONSECUTIVE:
            results[n] = (False, f"连续 {rounds[n]} 轮因闸未核实，照坏报: {msg}", True)
            # the failure streak starts where the held rounds reached, so this run raises the alarm
            fails[n] = max(int(fails.get(n, 0)), N_CONSECUTIVE - 1)
    state["gate_held"] = rounds


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
        return {"9222-shared": where.label("cdp-cn-forums"), "9223-xhs": where.label("cdp-xhs")}


def _heal_cdp_chrome() -> list[str]:
    """Relaunch any DEAD CDP Chrome BEFORE probing, so the probes see live browsers (the chrome
    launchd agents don't auto-relaunch a clean exit). Returns the launchd services it FAILED to bring
    back -- a failed self-heal means the CDP safety net itself is down, so the caller escalates those
    to an alert (otherwise the failure is silent)."""
    import subprocess
    from omniseek.core.sources.walled._cdp import cdp_health
    not_live = where.not_live_reason()
    if not_live:
        log.info("self-heal: skipped, this process is not the live eye (%s)", not_live)
        return []
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


def _cdp_lane_due(state: dict, now: float) -> bool:
    """Whether this run carries the CDP lane. PURE. Due when no run ever carried it, or the last one
    that did started at least CDP_LANE_EVERY_S minus half a run interval ago. The half interval
    absorbs how late a run starts behind the other jobs of its scheduler tick (it never starts early),
    and is less than one interval, so the lane lands on every fourth 6h run: once a day, not twice."""
    last = state.get("cdp_lane_at")
    if not isinstance(last, (int, float)):
        return True
    return (now - last) >= CDP_LANE_EVERY_S - HEALTH_EVERY_S / 2


def run_source_health(scope: Optional[str] = None) -> dict:
    """One source-health run (the cron_watchdog 源体检 half, minus the dissolved cron-liveness half):
    heal dead CDP Chrome, probe every source's health_check (non-CDP concurrent, CDP serial), track
    consecutive fails, and alert newly-down / recovered (cooldown-gated). Returns a small summary.

    scope=None (the scheduled job, every 6h): carry the CDP lane when it is due (_cdp_lane_due),
    else probe the non-CDP sources only.
    scope="all" forces the full run (heal CDP, probe non-CDP +
    CDP + infra); scope="noncdp" forces the non-CDP run, which skips the CDP heal/probe/infra entirely
    and MERGES its result into the stored state (never dropping the CDP entries the CDP lane owns)."""
    started = time.time()
    if scope is None:
        scope = "all" if _cdp_lane_due(_load_state(_HEALTH_STATE), started) else "noncdp"
    full = (scope != "noncdp")
    from omniseek.server import load_sources
    from concurrent.futures import ThreadPoolExecutor
    load_sources()
    from omniseek.core import fetcher

    heal_failed = _heal_cdp_chrome() if full else []  # CDP heal rides the CDP lane only (needs a browser)

    names = sorted(fetcher.all_adapter_names())
    # RETIRED sources are PROBED too (since 2026-10-04), and only watched for coming back: a curator
    # retire (reversible overlay, reason begins "retired...") parks a source as intentionally dead, so
    # its failing probe is the expected state and gets no fail streak, alert or status row (handled
    # after the probes). Its answering probe is the rollback signal its retire note asks for. Skipping
    # them here, as this used to, made that signal dead code: higheredjobs_cs sat retired by a stale
    # overlay from 2026-06-18 to 2026-10-04 while healthy, and nothing flagged it.
    def _is_retired(n: str) -> bool:
        # fetcher.retired_reason is the ONE retire derivation. get_adapter may return None if a
        # concurrent unregister raced the all_adapter_names snapshot; with no adapter to read, a
        # vanished source is not "retired".
        a = fetcher.get_adapter(n)
        return bool(fetcher.retired_reason(a)) if a is not None else False
    retired = {n for n in names if _is_retired(n)}

    def _is_sealed(n: str) -> bool:
        a = fetcher.get_adapter(n)
        return bool(fetcher.sealed_reason(a)) if a is not None else False
    sealed = {n for n in names if _is_sealed(n)}
    noncdp = [n for n in names if n not in _CDP_SOURCES and n not in sealed]
    cdp = [n for n in names if n in _CDP_SOURCES and n not in sealed] if full else []  # CDP ones, retired or not: full lane only

    # name -> the failure records its probe noted (_probe_tapped): the addresses a source failed on,
    # read by the exit-node sweep below.
    noted: dict[str, list] = {}

    def probe_named(n):
        return n, _probe_tapped(n, fetcher.get_adapter(n), noted)

    # name -> (ok, msg, completed): ok True / False / None, see _health_probe
    results: dict[str, tuple[Optional[bool], str, bool]] = {}
    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as ex:
        for n, res in ex.map(probe_named, noncdp):
            results[n] = res
    for n in cdp:  # CDP: SERIAL (one browser tab at a time -- gentle + account-safe)
        results[n] = _probe_tapped(n, fetcher.get_adapter(n), noted)

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
                  f"launchctl 无法拉起 {svc} -- 该实例所有 CDP 源静默离线,需登录主机检查",
                  group="OmniSeek-Health")
            pushed += 1

    newly_down: list[tuple[str, str]] = []
    recovered: list[str] = []
    probed = noncdp + cdp
    # A RETIRED source is parked on purpose (the curator's reversible retire overlay), so its probe
    # failing is the expected state, not news. Counting it as a failure is how sg_immigration, dead
    # upstream and retired for it on 2026-07-10, kept reporting as a broken source two months later.
    # It is still PROBED rather than skipped (see the probe list above): its retire note asks to roll
    # back if the feeds come back, and that signal only exists if someone keeps looking.
    retired &= set(probed)
    _gate_held_rounds(state, results, [n for n in probed if n not in retired], fails)
    cleared = _track_probed(state, results, probed, retired, fails, alerts, newly_down, recovered,
                            fetcher.fetched_ok_at, time.time())
    if cleared:
        log.info("source-health[%s]: %d count(s) cleared by a later live fetch: %s", scope, len(cleared),
                 "; ".join(f"{n}: {w}" for n, w in sorted(cleared.items())))
    for label, (ok, msg) in infra.items():
        _health_track(f"_cdp:{label}", ok, msg, fails, alerts, newly_down, recovered, state["fail_at"])

    if newly_down:
        body = "\n".join(f"- {n}{' [对方拒绝]' if _is_refused(msg) else ''}: {msg[:48]}"
                         for n, msg in newly_down)
        _alert(f"源故障 · {len(newly_down)}", body)
        pushed += 1
    if recovered:
        _alert(f"源恢复 · {len(recovered)}",
               "\n".join(f"- {n}" + (f"（体检没核到；{cleared[n]}）" if n in cleared else "")
                         for n in recovered))
        pushed += 1

    # DEGRADED transition: a multi-feed bundle can be healthy (ok=True) yet have lost members. Those
    # never enter newly_down, so member rot was invisible until ALL feeds died. Track the degraded set
    # across runs and alert on a source's full->degraded transition once (a bundle names its dead feeds in
    # the health message via the "degraded" marker). Recovery to full clears it silently. Only a
    # VERIFIED True counts: an unverified None whose message says "degraded" (a busy gate, a breaker)
    # lost no members, and before 2026-10-04 those read True and could fire this alert.
    degraded_now = {n for n in probed
                    if n not in retired and results[n][0] is True and "degraded" in results[n][1].lower()}
    prev_degraded = set(state.get("degraded", []))
    newly_degraded = [n for n in sorted(degraded_now) if n not in prev_degraded]
    if newly_degraded:
        body = "\n".join(f"- {n}: {results[n][1][:64]}" for n in newly_degraded)
        _alert(f"源降级 · {len(newly_degraded)}", body)
        pushed += 1
    # SCOPE-AWARE, by the same rule the snapshot below already follows: a scoped run may only speak
    # about what it PROBED. A non-CDP run never probes the CDP sources, so replacing the whole set
    # from it dropped every CDP source out of `degraded`; the next CDP-lane run re-added them, they read
    # as NEWLY degraded, and 源降级 re-fired twice a day forever. The set is a partial observation
    # being written as if it were a total one, which is exactly the defect the last_status merge
    # fixes one block down. This key was simply left behind.
    # Full run rebuilds (and thereby self-cleans a source that left the registry); a non-CDP run
    # keeps what it could not see.
    state["degraded"] = sorted(degraded_now if full
                               else (prev_degraded - set(probed)) | degraded_now)

    # EXIT NODE: sources that broke this run may have broken because the proxy node OmniSeek
    # leaves through broke or got refused. Test the nodes and switch to one that reaches them
    # (eye.exit_node; it sends its own notice either way). What broke this run: a source down for the
    # first time (a re-nag of one long down is not new), a bundle that just lost members, and a source
    # already down or degraded that failed on MORE hosts than last run (its new dead hosts only: 11-04 two
    # blogs went dark on Z11 inside substack_matrix, degraded for weeks by another feed, and a dead feed
    # would fail every node; 11-10 llm_leaderboard went down leaving no address, so its first-down sweep
    # tested nothing, and the run that first sees its host must test it).
    # dead_hosts {source: [hosts it failed on]} remembers the last run's, same scope rule as `degraded`.
    # Bounded by what is left of this job's budget; it runs after the state is saved (below), so a slow
    # sweep can never lose this run's bookkeeping.
    from omniseek.core import exit_node
    dead_now = {n: sorted(exit_node.failed_hosts(noted.get(n, []))) for n in probed
                if n not in retired and (results[n][0] is False or n in degraded_now)}
    # The first run that keeps dead_hosts only learns them: every degraded bundle would read as grown.
    seeding = not isinstance(state.get("dead_hosts"), dict)
    prev_dead = {} if seeding else state["dead_hosts"]
    first_down = [n for n, _m in newly_down if not n.startswith(_INFRA_ROW_PREFIX)
                  and fails.get(n) == N_CONSECUTIVE]
    broke = {n: noted.get(n, []) for n in first_down + newly_degraded}
    long_down = {n for n in dead_now if (fails.get(n) or 0) > N_CONSECUTIVE}
    for n in ([] if seeding else sorted((degraded_now | long_down) - set(broke))):
        grew = set(dead_now.get(n, [])) - set(prev_dead.get(n, []))
        if grew:
            broke[n] = exit_node.only_hosts(noted.get(n, []), grew)
    state["dead_hosts"] = (dead_now if full
                           else {**{k: v for k, v in prev_dead.items() if k not in set(probed)}, **dead_now})

    # REFUSED set, same scope rule as `degraded`: which of the failing sources are failing because
    # the far end turned us away. Purely additive bookkeeping (the fail streaks and the alert
    # trigger are untouched, since a refused source IS unavailable), so the reader can tell at a
    # glance which alarms are ours to fix and which are somebody else's decision.
    refused_now = {n for n in probed
                   if n not in retired and results[n][0] is False and _is_refused(results[n][1])}
    prev_refused = set(state.get("refused", []))
    state["refused"] = sorted(refused_now if full
                              else (prev_refused - set(probed)) | refused_now)

    # A retired source that answers again is the ROLLBACK signal its retire note asks for, and it is
    # the only reason to keep probing one at all. Alert on the transition once, the same shape as
    # 源降级: silence when it goes back to failing, so a flapping upstream cannot nag.
    alive_now = {n for n in retired if results[n][0] is True}
    prev_alive = set(state.get("retired_alive", []))
    newly_alive = sorted(alive_now - prev_alive)
    if newly_alive:
        _alert(f"已退役源复活 · {len(newly_alive)}",
               "退役后重新可用,可考虑回滚退役:\n"
               + "\n".join(f"- {n}: {results[n][1][:56]}" for n in newly_alive))
        pushed += 1
    state["retired_alive"] = sorted(alive_now if full
                                    else (prev_alive - set(probed)) | alive_now)

    state["fails"] = fails
    state["_alerts"] = alerts
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    if full:
        state["cdp_lane_at"] = started  # this run carried the CDP lane (read by _cdp_lane_due)
    # Full run rebuilds the snapshot; a non-CDP run MERGES so the CDP + infra entries the CDP lane
    # owns are preserved (a from-scratch snap would drop them -> list_sources 'unknown').
    snap = dict(state.get("last_status", {})) if not full else {}
    snap.update({n: results[n][0] for n in probed})
    for label, (ok, _msg) in infra.items():
        snap[f"_cdp:{label}"] = ok
    state["last_status"] = snap
    # The third state, kept as its OWN map. last_status can hold None, but a consumer reading only
    # its KEY set (fetcher._watchdog_health does exactly that) cannot tell None from True, which
    # would turn "we could not measure" into a reported ok: a false green replacing the false red.
    # Same merge rule as snap so a non-CDP run does not erase the CDP lane's rows.
    unm = dict(state.get("unmeasured", {})) if not full else {}
    for n in probed:
        if results[n][0] is None and not results[n][2]:
            unm[n] = str(results[n][1])[:200]
        else:
            unm.pop(n, None)
    state["unmeasured"] = unm
    # The fourth state, also its OWN map for the same key-set reason: the probe COMPLETED, but by
    # design asked the upstream nothing (a metered quota, a busy declared gate, a breaker, an answer
    # from cache). Neither healthy nor failing, so it moves no counter above and must not read as ok.
    # Same merge rule as snap / unm so a non-CDP run keeps the CDP lane's rows.
    unv = dict(state.get("unverified", {})) if not full else {}
    for n in probed:
        if results[n][0] is None and results[n][2]:
            unv[n] = str(results[n][1])[:200]
        else:
            unv.pop(n, None)
    state["unverified"] = unv
    # A retired source is probed only for the alive-again signal above -> drop its stale fail / status /
    # alert entries so a parked source self-cleans instead of freezing at a stale "down" (no manual
    # state edit ever needed), and this run's probe never lands in them.
    # A SEALED source was not probed at all; drop its old rows the same way, so a source sealed while
    # it read "down" stops showing (and re-alerting) as down.
    for r in retired | sealed:
        fails.pop(r, None)
        state["fail_at"].pop(r, None)
        snap.pop(r, None)
        unm.pop(r, None)
        unv.pop(r, None)
        alerts.pop(f"down:{r}", None)
    _save_state(_HEALTH_STATE, state)
    if broke:
        try:
            left = HEALTH_BUDGET_S - (time.time() - started)
            exit_node.sweep(broke, time.monotonic() + left, why=f"源体检[{scope}]")
        except Exception as exc:  # noqa: BLE001 -- the sweep never breaks the health run
            log.warning("source-health[%s]: exit-node sweep failed: %s", scope, exc)
            _alert("出口节点 · 测切出错", f"源体检后测、切出口节点出错（{type(exc).__name__}: {str(exc)[:120]}），没切。")

    # Counted apart, never summed into "healthy": only a VERIFIED True is green, and an unverified or
    # unmeasured None is neither green nor a failure.
    n_green = sum(1 for n in probed if results[n][0] is True)
    n_failed = sum(1 for n in probed if results[n][0] is False)
    n_unverified = sum(1 for n in probed if results[n][0] is None and results[n][2])
    n_unmeasured = sum(1 for n in probed if results[n][0] is None and not results[n][2])
    log.info("source-health[%s]: %d/%d healthy, %d failed, %d unverified, %d unmeasured (%d CDP); "
             "newly_down=%d recovered=%d alert=%d",
             scope, n_green, len(probed), n_failed, n_unverified, n_unmeasured, len(cdp),
             len(newly_down), len(recovered), pushed)
    return {"healthy": n_green, "failed": n_failed, "unverified": n_unverified,
            "unmeasured": n_unmeasured, "probed": len(probed), "newly_down": len(newly_down),
            "recovered": len(recovered), "alert": pushed, "pruned": _pr["pruned"],
            "sealed": sorted(sealed)}


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
# 风控 write-risk path), jitter-paced, active-hours only (08-23 host local time), and it honours the same
# <state>/cdp-maintenance pause flag cdp_keepalive uses (so it never fights a manual VNC
# login). One light search per account per run is trivial activity (a real user does dozens) and is
# the kind of passive browse 养号 recommends; it stays well under any rate concern.
_WARMER_STATE = _STATE / "session-warmer.json"
_MAINT_FLAG = _STATE / "cdp-maintenance"
_WARMER_COOLDOWN_S = 6 * 3600
_ACTIVE_START, _ACTIVE_END = 8, 23  # host local hours
# A blank search page (no results, no login wall) alerts only when it is the Nth run in a row. N=2 is
# PROVISIONAL: in the 19 runs of 2026-10-10..16 the two blank pages each came back on the next run, and
# 2 is the smallest N that lets both pass while a lasting blank still alerts within one run's gap.
# Calibration: after a month of runs, read _self_heals and the blank_streak values in the state file
# (and the warmer log's "blank search page" lines); if a streak of 2 ever healed by itself on run 3,
# raise N; if a real logout ever showed as blank pages with no login wall, lower it to 1.
_BLANK_ALERT_STREAK = 2
# Per-result keys copied into the state file's last_results when present (the xhs search probe sets them).
_WARMER_STATE_EXTRAS = ("skipped", "kind", "dom_items", "body_len", "retry", "self_healed", "blank_streak")

# Benign rotating warm queries (look like a real PhD-track user glancing at their feed).
_WARM_QUERIES = ["读博日常", "科研", "博士生活", "留学生活", "实验室日常", "读研"]

# label -> (cdp url, home url, search url template {kw}, site-key substring, probe-kind).
# probe-kind decides how a degraded session is DETECTED (the warm half -- home+scroll -- is identical):
#   "xhs_search"   = read the search-result DOM (a login wall hides the results cards).
# (The 抖音 browser and its login-state probe were withdrawn on 2026-10-20 with the browser itself.)
_WARMER_INSTANCES = {
    "小号-rednote": (
        "http://127.0.0.1:9223", "https://www.rednote.com",
        "https://www.rednote.com/search_result?keyword={kw}&type=51", "rednote", "xhs_search"),
}

_DOM_JS = r"""(()=>({
  note_items: document.querySelectorAll("section.note-item, [class*='note-item'], a[href*='/explore/'], a[href*='/search_result/']").length,
  login_overlay: !!document.querySelector(".login-container,[class*='LoginModal'],.reds-mask"),
  login_btn: (document.body.innerText.match(/登录/g)||[]).length,
  body_len: document.body.innerText.length
}))()"""


def _jsleep(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


def _search_dom(page, url: str) -> dict:
    """Open one search-result page and read _DOM_JS off it. A goto failure raises; a failed read
    returns an empty dict, which _classify_search calls blank (nothing was seen, so no login wall).
    A dropped connection is retried on the same page and exit (_cdp.goto_retrying)."""
    from omniseek.core.sources.walled import _cdp
    _cdp.goto_retrying(page, url, timeout=25000)
    _jsleep(4.0, 6.0)
    try:
        return page.evaluate(_DOM_JS) or {}
    except Exception:  # noqa: BLE001
        return {}


def _classify_search(dom: dict) -> str:
    """One search-result page, as the warmer reads it: "ok" (more than three note cards and no login
    overlay), "login_wall" (a login overlay or a login button on the page: the session is gone) or
    "blank" (no results and no login wall: the 2026-10-13 and 10-16 pages, body_len 85 and 133,
    which came back on the next run by themselves)."""
    if dom.get("note_items", 0) > 3 and not dom.get("login_overlay"):
        return "ok"
    if dom.get("login_overlay") or dom.get("login_btn", 0) > 0:
        return "login_wall"
    return "blank"


def _is_load_timeout(exc: BaseException) -> bool:
    """A page load that ran out of time (Playwright's / Patchright's TimeoutError from Page.goto): the
    page was never seen, so it says nothing about the login."""
    return isinstance(exc, TimeoutError) or any(c.__name__ == "TimeoutError" for c in type(exc).__mro__)


def _warm_one(p, label: str, cdp: str, home: str, search_tpl: str, key: str, probe: str) -> dict:
    """Drive ONE Chrome through home -> scroll -> one search. Returns a status dict. Never raises
    (per-instance isolation); records why if it could not warm. ``probe`` selects the degradation-
    detection (see _WARMER_INSTANCES): "xhs_search" reads the result page's DOM (see _classify_search;
    the result carries ``kind``, and ``retry`` / ``self_healed`` when a blank page was searched again).

    ``unprobed`` is set when the browser could not be reached at all, or when the home or search page
    did not finish loading in time (a Page.goto timeout). Neither says anything about the login, so
    the caller must not turn it into a "session degraded, re-login" alert (2026-09-26: it did, three
    times a day per account, for browsers the idle reaper had merely stopped; 2026-11-06: a home page
    that timed out was pushed as a re-scan). A page that loaded and shows a login wall still alerts."""
    from urllib.parse import quote
    from omniseek.core.sources.walled import _cdp
    res = {"label": label, "ok": False, "unprobed": False, "reason": "",
           "acw_tc": False, "web_session": False}
    # The idle reaper stops a browser nobody used for CDP_IDLE_TIMEOUT_S, and this job runs only
    # three times a day, so it usually finds its browser stopped. Start it the way every read does;
    # ensure_browser also stamps last-use, so the reaper leaves it running for the whole warm.
    try:
        _cdp.ensure_browser(cdp)
    except Exception as exc:  # noqa: BLE001
        res["unprobed"] = True
        res["reason"] = f"browser start failed: {exc!r}"
        return res
    try:
        b = p.chromium.connect_over_cdp(cdp)
    except Exception as exc:  # noqa: BLE001
        res["unprobed"] = True
        res["reason"] = f"cdp connect failed: {exc!r}"
        return res
    try:
        ctx = b.contexts[0]
        cand = [pg for pg in ctx.pages if key in (pg.url or "")]
        page = cand[0] if cand else (ctx.pages[0] if ctx.pages else ctx.new_page())

        # 1) home + light human-like scroll (re-mints the short-lived security cookies). A dropped
        # connection (2026-11-06: net::ERR_CONNECTION_CLOSED on the direct route to rednote.com) is
        # retried on the same page and exit; a load timeout still raises at once (unprobed, below).
        try:
            _cdp.goto_retrying(page, home, timeout=25000)
        except Exception as exc:  # noqa: BLE001
            res["unprobed"] = _is_load_timeout(exc)
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

        # 2) one light search (warms search-auth + serves as the health probe). A blank result page
        # (no notes, no login wall) is retried once in the same run: it is how a one-off rendering
        # hiccup looks, and the next search usually comes back fine.
        kw = random.choice(_WARM_QUERIES)
        url = search_tpl.format(kw=quote(kw))
        try:
            dom = _search_dom(page, url)
        except Exception as exc:  # noqa: BLE001
            res["unprobed"] = _is_load_timeout(exc)
            res["reason"] = f"search goto failed: {exc!r}"
            return res
        kind = _classify_search(dom)
        if kind == "blank":
            _jsleep(3.0, 6.0)
            try:
                dom = _search_dom(page, url)
                kind = _classify_search(dom)
                res["retry"] = kind
            except Exception as exc:  # noqa: BLE001
                res["retry"] = f"goto failed: {exc!r}"
        res["kind"] = kind
        res["dom_items"] = dom.get("note_items")
        res["body_len"] = dom.get("body_len")
        res["ok"] = kind == "ok"
        res["self_healed"] = res["ok"] and "retry" in res
        if not res["ok"]:
            res["reason"] = (f"degraded ({kind}): dom_items={dom.get('note_items')} "
                             f"login_overlay={dom.get('login_overlay')} login_btn={dom.get('login_btn')} "
                             f"body_len={dom.get('body_len')}"
                             + (f" retry={res['retry']}" if "retry" in res else ""))
        return res
    except Exception as exc:  # noqa: BLE001
        res["reason"] = f"warm flow raised: {exc!r}"
        return res


# The 9222 shared-Chrome FORUM sources (cn-forums Chrome) had NO proactive maintenance: only the
# 小号 Chromes were warmed above, so a silently-expired 一亩三分地 / zhihu session stayed dark
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
    flows through the same log / Bark / state machinery, and
    ``self_heals`` marks a source whose reactive search path already Barks (so the warmer skips a
    duplicate Bark for it). Never raises (per-instance isolation)."""
    from omniseek.core.fetcher import get_adapter
    res = {"label": label, "ok": False, "reason": "",
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
    res["ok"] = len(docs) > 0
    if not res["ok"]:
        res["reason"] = f"degraded: {source_name} search returned 0 (logged out / blocked / empty)"
    return res


# ── CDP idle reaper (2026-08-29) ──────────────────────────────────────────────────────────────
# Stops CDP browsers nobody has used for a while, so a 16 GB machine is not permanently holding
# 3.1 GB of idle Chrome. Pairs with _cdp.ensure_browser, which brings one back in ~1s on the next
# call (measured). Runs process-isolated, so it reads last-use stamps off DISK: it has its own
# interpreter and cannot see any in-process counter.
#
# ⚠️ CDP_IDLE_TIMEOUT_S is a PROBE VALUE, not derived from anything. Pre-registered calibration:
# after two weeks of real traffic, compare the share of calls that had to cold-start against the
# browsers' average daily uptime. Cold-start share above ~30% means the window is too short
# (we are closing browsers people are still using) -> lengthen. Average uptime still above ~6h/day
# means it is too long (we are not getting the memory back) -> shorten. Neither -> graduate it and
# replace this notice with the measurement. Do NOT let it graduate silently.
# Cold start costs 1 second, which is why getting this number wrong is cheap.
CDP_IDLE_TIMEOUT_S = int(os.environ.get("OMNISEEK_CDP_IDLE_S", 30 * 60))

# How the reaper stops a browser (2026-10-05): the CDP command Browser.close first, then waits for
# the main process to exit, and only falls back to `launchctl kill TERM` when that does not work.
# TERM made Chrome skip the end of its shutdown and leave its app-bundle clone behind every time
# (12 of 12 on the live host; Browser.close: 0 of 13; see _cdp.close_browser). KeepAlive does not
# restart either way: both exits are clean (last exit code 0).
# CDP_CLOSE_WAIT_S, confirmed 2026-10-06: the throwaway Chrome (empty profile) exited 0.3 s after
# Browser.close, 13 of 13, and on the live host the reaper's first nine stops of the logged-in browsers
# (2026-10-05 14:58 to 23:02) all exited within 1 to 2 s with no ":term" fallback, so only a hung
# browser waits the full 15 s. Recheck from the reaper log: a ":term" suffix in "stopped" means the
# TERM fallback ran.
CDP_CLOSE_WAIT_S = 15.0
# The job runs under a 120 s budget (jobs.py, cdp-reaper row); past it the whole process is killed and
# the run's result is lost. Everything the reaper starts must end by this many seconds after it
# began: a browser stop takes the Browser.close path only while CLOSE_PATH_NEEDS_S are left (its
# worst case: launchctl list 10 s, the CDP exchange about 18 s (/json/version 3 s, then connect,
# handshake and reply 5 s each), the exit wait, the TERM fallback 10 s; typical total well under
# 1 s), else it sends TERM at once; the clone sweep gets what is left.
REAPER_DEADLINE_S = 90.0
CLOSE_PATH_NEEDS_S = CDP_CLOSE_WAIT_S + 40.0


def _launchd_pid(label: str) -> Optional[int]:
    """PID of the running job ``label`` from `launchctl list <label>`, or None."""
    try:
        out = subprocess.run(["launchctl", "list", label], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'"PID"\s*=\s*(\d+);', out) if isinstance(out, str) else None
    return int(m.group(1)) if m else None


def _wait_exit(pid: int, limit_s: float) -> bool:
    """True once process ``pid`` is gone, False if it is still there after ``limit_s`` seconds."""
    deadline = time.monotonic() + limit_s
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def _stop_browser(url: str, label: str, deadline: Optional[float] = None) -> str:
    """Stop one CDP browser. Returns "close" when Browser.close did it, else "term" (the fallback:
    no pid, Chrome did not take the command, it was still running after CDP_CLOSE_WAIT_S, or fewer
    than CLOSE_PATH_NEEDS_S seconds are left before ``deadline``, a time.monotonic() value)."""
    from omniseek.core.sources.walled import _cdp

    if deadline is None or deadline - time.monotonic() >= CLOSE_PATH_NEEDS_S:
        pid = _launchd_pid(label)
        if pid is not None and _cdp.close_browser(url) and _wait_exit(pid, CDP_CLOSE_WAIT_S):
            return "close"
        log.info("cdp-reaper: %s did not close via CDP; sending TERM", label)
    else:
        log.info("cdp-reaper: too little time left to close %s via CDP; sending TERM", label)
    subprocess.run(
        ["launchctl", "kill", "TERM", f"gui/{os.getuid()}/{label}"],
        check=False, timeout=10,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return "term"


def run_cdp_reaper() -> dict:
    """Stop idle CDP browsers; leave busy ones and already-stopped ones alone."""
    if sys.platform != "darwin":
        return {"skipped": "not-darwin"}
    not_live = where.not_live_reason()
    if not_live:
        return {"skipped": f"not the live eye ({not_live})"}
    if _MAINT_FLAG.exists():
        log.info("cdp-reaper: cdp-maintenance flag present -> skip (rm the flag to resume)")
        return {"skipped": "maintenance"}
    from omniseek.core.sources.walled import _cdp

    deadline = time.monotonic() + REAPER_DEADLINE_S
    now = time.time()
    stopped: list[str] = []
    kept: list[str] = []
    already_down: list[str] = []
    for port, label in _cdp._CDP_SERVICES.items():
        url = f"http://127.0.0.1:{port}"
        if not _cdp.cdp_health(url)[0]:
            already_down.append(port)
            continue
        last = _cdp.read_last_use(port)
        if last is None:
            # Up but never stamped: either this shipped a moment ago, or something drives this
            # browser by a path that predates stamping. Seed a stamp instead of reaping, so the
            # FIRST reaper cycle after a deploy can never kill a browser that is genuinely in use.
            _cdp.touch_last_use(url)
            kept.append(f"{port}:seeded")
            continue
        idle_min = (now - last) / 60.0
        if now - last < CDP_IDLE_TIMEOUT_S:
            kept.append(f"{port}:{idle_min:.0f}m")
            continue
        try:
            how = _stop_browser(url, label, deadline)
            stopped.append(f"{port}:{idle_min:.0f}m" + ("" if how == "close" else f":{how}"))
        except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001 — one failure must not
            log.warning("cdp-reaper: could not stop %s: %s", label, exc)  # stop the other three
    log.info("cdp-reaper: stopped=%s kept=%s already-down=%s",
             stopped or "-", kept or "-", already_down or "-")
    clones = _sweep_chrome_clones(deadline)
    return {"stopped": stopped, "kept": kept, "already_down": already_down,
            "idle_timeout_s": CDP_IDLE_TIMEOUT_S, "clones": clones}


def _sweep_chrome_clones(deadline: Optional[float] = None) -> dict:
    """Delete the app-bundle clones Chrome leaves behind (omniseek.core.chrome_clones), as part of the
    reaper's cycle: the reaper is what stops browsers, it runs every 10 minutes in its own process,
    and it already only acts on macOS. The pass gets the time left before ``deadline`` (a
    time.monotonic() value). Any failure is logged and reported, never raised, so the reaping above
    is never undone by it. Returns counts plus the names deleted."""
    try:
        from omniseek.core import chrome_clones
        left = None if deadline is None else deadline - time.monotonic()
        res = chrome_clones.sweep(time_left_s=left)
    except Exception as exc:  # noqa: BLE001 -- the cleanup must never fail the reaper
        log.warning("cdp-reaper: chrome clone cleanup failed: %r", exc)
        return {"error": repr(exc)}
    if "deleted" not in res:
        return res
    if res["deleted"] or res["errors"]:
        log.info("cdp-reaper: chrome clones deleted=%d in_use=%d too_new=%d errors=%d",
                 len(res["deleted"]), len(res["in_use"]), len(res["too_new"]), len(res["errors"]))
    return {"deleted": res["deleted"], "in_use": len(res["in_use"]),
            "too_new": len(res["too_new"]), "errors": res["errors"],
            "left_for_next_pass": len(res["left_for_next_pass"])}


def _needs_relogin_alert(r: dict) -> bool:
    """The "re-login this account" alert is only true of a session the warmer actually LOOKED AT
    and found bad. Not for one it could not reach (``unprobed``: an unobserved state is not a
    negative), and not for an autofill-backed forum (``self_heals``: its reactive search path
    already alerts on a failed re-login), and not for a browser the warmer skipped (``skipped``:
    nothing was looked at)."""
    return not r["ok"] and not r.get("unprobed") and not r.get("self_heals") and not r.get("skipped")


def run_session_warmer() -> dict:
    """One warm run across the walled CDP Chromes: skip outside active hours or under the
    cdp-maintenance flag; else warm each account (home -> scroll -> one search) + alert any session
    it looked at and found degraded (6h cooldown; a browser it could not reach is logged, not alerted). The playwright driver is imported here (available in-process); a run that
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
            log.info("session-warmer %s: ok=%s%s acw_tc=%s web_session=%s %s",
                     label, r["ok"], " (blank page, healed on retry)" if r.get("self_healed") else "",
                     r["acw_tc"], r["web_session"],
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
            log.info("session-warmer %s: ok=%s self_heals=%s %s",
                     label, r["ok"], r["self_heals"],
                     ("| " + r["reason"]) if r["reason"] else "")
            _jsleep(3.0, 6.0)

    streaks = state.get("_blank_streak", {})
    heals = state.get("_self_heals", {})
    for r in results:
        if r.get("unprobed"):
            log.warning("session-warmer %s: browser unreachable or page load timed out, session NOT probed (no alert): %s",
                        r["label"], r["reason"])
        if r.get("self_healed"):
            heals[r["label"]] = heals.get(r["label"], 0) + 1
        if not _needs_relogin_alert(r):
            if r["ok"]:
                streaks.pop(r["label"], None)
            continue
        if r.get("kind") == "blank":
            n = streaks[r["label"]] = streaks.get(r["label"], 0) + 1
            r["blank_streak"] = n
            if n < _BLANK_ALERT_STREAK:
                log.warning("session-warmer %s: blank search page, no login wall (%d in a row, "
                            "alert at %d): %s", r["label"], n, _BLANK_ALERT_STREAK, r["reason"])
                continue
            if _should_alert(f"session_blank:{r['label']}", alerts, _WARMER_COOLDOWN_S):
                _alert(f"{r['label']} 搜索连续 {n} 轮空白页",
                       f"暖号连续 {n} 轮搜到空白页，没见登录墙（每轮都当场重试过一次）:{r['reason']}。"
                       f"登录态未必失效，也可能是小红书这几次没出结果。", group="OmniSeek-Health",
                       questions=[f"有空请看一眼 {r['label']}：小红书搜索现在还出结果吗？"])
            continue
        streaks.pop(r["label"], None)
        if _should_alert(f"session_degraded:{r['label']}", alerts, _WARMER_COOLDOWN_S):
            seen = "暖号搜索见到登录墙" if r.get("kind") == "login_wall" else "暖号验证失败"
            _alert(f"{r['label']} session 退化",
                  f"{seen}:{r['reason']}。登录态可能已失效,需 VNC 进主机重新扫码登录该账号"
                  f"({r['label']} 的 Chrome 窗口)。", group="OmniSeek-Health",
                  questions=[f"请 VNC 进主机，在 {r['label']} 的 Chrome 窗口重新扫码登录该账号"])

    state["_alerts"] = alerts
    state["_blank_streak"] = streaks
    state["_self_heals"] = heals
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["last_results"] = [
        {**{k: r[k] for k in ("label", "ok", "acw_tc", "reason")},
         "unprobed": bool(r.get("unprobed")),
         **{k: r[k] for k in _WARMER_STATE_EXTRAS if k in r}}
        for r in results]
    _save_state(_WARMER_STATE, state)
    degraded = [r["label"] for r in results
                if not r["ok"] and not r.get("unprobed") and not r.get("skipped")]
    unprobed = [r["label"] for r in results if r.get("unprobed")]
    log.info("session-warmer: warmed=%s degraded=%s unprobed=%s",
             [r["label"] for r in results if r["ok"]], degraded, unprobed)
    return {"warmed": [r["label"] for r in results if r["ok"]],
            "degraded": degraded, "unprobed": unprobed}


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# JOB: log rotation   (transplanted from omniseek_http_watchdog.py rotation half; daily@04:50)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# Copytruncate any oversized log in <logs> so one chatty source can't grow a log unbounded
# (eye-http.err reached 63MB from a reddit Arctic-Shift 429 storm). launchd opens these redirects
# with O_APPEND (verified 2026-06-20: truncate-in-place stayed small, no sparse hole), so this is
# safe with the writer still running: it resumes appending at the new EOF, NO restart needed. The
# last _LOG_KEEP_TAIL bytes are kept as <name>.1 for recent context.
_LOG_MAX_BYTES = 30 * 1024 * 1024   # copytruncate any eye log over 30MB
_LOG_KEEP_TAIL = 4 * 1024 * 1024    # keep the last 4MB of recent context after rotating


def rotate_logs() -> int:
    """Copytruncate every oversized log in <logs>. Returns how many it rotated. One bad log
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
    # 2026-11-03: the same daily pass deletes expired cache files. cache.clear_expired
    # ran only at a (re)start (serve_http), so a stretch with no release kept dead JSON on disk. No
    # size cap on top: the TTLs bound the total (11-03: 1,060 files, ~157 MB, oldest from 10-21).
    from omniseek.core import cache
    try:
        expired = cache.clear_expired()
    except Exception as exc:  # noqa: BLE001 -- housekeeping must never break the scheduler
        log.warning("cache sweep skipped: %s", exc)
        expired = None
    return {"rotated": rotate_logs(), "cache_expired": expired}


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# JOB: aiid-snapshot   (weekly; 2026-10-20)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# The ai_incidents source answers from a local index built off the AI Incident Database's weekly
# public snapshot. This job checks the snapshot page once a week and, only when a newer snapshot is
# listed, downloads it, checks it is whole, builds the index and switches to it (all in
# sources/api/ai_incidents_source.refresh, which also removes the downloaded archive and the older
# snapshot). A failed check keeps the snapshot in use; the source's health_check reports it stale
# once it is more than 14 days old, and source-health carries that to the usual alert.
def run_aiid_snapshot() -> dict:
    from omniseek.core.sources.api import ai_incidents_source as aiid
    summary = aiid.refresh()
    log.info("aiid-snapshot: %s", {k: summary.get(k) for k in
                                   ("outcome", "latest_listed", "in_use", "incidents", "timings_s", "error")})
    return summary
