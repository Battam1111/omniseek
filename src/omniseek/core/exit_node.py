"""Test the proxy nodes and switch OmniSeek's exit node when sources broke because of it.

OmniSeek reaches the web through the operator's proxy app (Clash Verge's mihomo, its TUN), and the app
sends OmniSeek's hosts to ONE node, the one its selector ``where.PROXY_SELECTOR`` picks. When that node
breaks (2026-10-21: the Hong Kong nodes dropped every handshake to a batch of sites) or a site starts
refusing its address (2026-11-04: SiteGround's captcha and v2ex turned away the Japan Z11 address), a
batch of sources goes down until someone tests the nodes and switches by hand. This module does that.

Why the nodes are tested through egress2 and not by switching the selector: switching the selector to
test would move every other request of OmniSeek with it. The app's own delay probe cannot tell a refused
address from a working one (one request, any answer counts; measured 11-04 with expected=200 on Z11:
"ok" while the page answered 202). egress2 reads the same subscription, so the node names are the same;
it has a select group and a local port only for this (config.template.yaml, listener ``probe``; its
generator writes the port, the group and the control socket into state.json). Switching that group
moves no traffic but ours.

One sweep (``sweep``), from the source-health job when a run found sources newly down or newly degraded,
or by hand (``python -m omniseek.core.exit_node <source> ...`` from the release directory with
``PYTHONPATH=src`` and the live eye's own interpreter: OmniSeek-http launchd job starts it through
``/bin/sh``, so take the interpreter from the process listening on OmniSeek's port, ``ps -p <pid> -o command=``):
  1. The addresses the failing sources failed on this round (the diag records the health probe tapped;
     a source that left none takes no part), one per host.
  2. Through the test port, on the node the selector uses now, with OmniSeek's own User-Agent (never a
     browser's: zenodo refuses a faked browser), or, for an address whose failure record names the
     request headers its source sent (diag.note ``headers``: levels.fyi answers only a browser's
     navigation request), with those: every fetch answering (``_ok``) means it was a blip:
     logged, no sweep, no notice (a notice per blip of a flapping feed would only be noise).
  3. Each node of the selector that is in egress2's own pool (the members of its region groups: the
     template's filters already leave out Hong Kong, the free and download-only nodes and the notice
     entries) and whose name carries no billing rate above 1 (``_rate``), best first by egress2's own
     health readings. The rate matters because the selector stays where it is put: every request this
     machine sends through the proxy app goes out on the pick until the next sweep, so a 2x node doubles
     what all of it costs (11-04 23:02 live: the first passing node was the Taiwan Z01 x2 node; egress2's
     own fallback can hold a 2x node only while every cheaper region is down and leaves it by itself).
     For each: switch the test group to it, fetch those addresses and the four references (REFERENCES). The first
     node in that order where every fetch answers wins and the sweep stops there (11-04 live: testing
     every node to pick the fastest kept OmniSeek on the refused node for ten minutes, while the first
     node in egress2's order passed). "Answers" is 200, 401 (a keyed API refetched without its key,
     ``_ok``), or a rate limit (429; 403 with x-ratelimit-remaining 0, which is how GitHub's unauthenticated API answers on an address whose
     hourly allowance egress2's own health checks spend): the node reaches the site. A node is started
     only while its worst case and what must follow still fit before the deadline (see ``sweep``).
  4. A winner: switch the selector to it (only the selection; no rule, no config file), fetch the
     addresses again on the default route; all 200 keeps it, anything else switches back. No winner: no
     switch. Every sweep ends with one notice through the push outlet saying what it did and why.
  5. A switch that did not help (rolled back: the hosts still fail on the default route, so the proxy
     app's rules send them to another group, not the selector) is remembered in <state>/exit-node-futile.json:
     the hosts that still failed and when (2026-11-06; before, every health run that
     found the same hosts down switched for nothing and sent the same notice again). A later sweep whose
     failing hosts are all in one such batch, less than FUTILE_TTL_S old, fetches them once on the default
     route: if none answers it tests and switches nothing and says why (at most one notice per batch a
     day); a host of a batch answering on the default route, in that round or any other, drops the batch.
"""

from __future__ import annotations

import fcntl
import ipaddress
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

import httpx

from omniseek import where

log = logging.getLogger(__name__)

# The four sites the 2026-10-21 hand sweep used to judge a node (crossref, zenodo, a CMU blog, GitHub's
# API): sources OmniSeek depends on, behind different networks. A node must pass them too, so a switch
# made for a few sources cannot break the rest.
REFERENCES = (
    "https://api.crossref.org/works?rows=0",
    "https://zenodo.org/api/records?size=1",
    "https://blog.ml.cmu.edu/feed/",
    "https://api.github.com/zen",
)

# Records whose address was never sent anywhere: OmniSeek's own gate or its SSRF guard held it back.
_NOT_SENT_MARKERS = ("request not sent", "blocked SSRF-class target")
# A redacted address (a key taken out: diag's mask, omniseek.redact's) cannot be fetched again as it was.
_MASKS = ("<redacted>", "***")


def _limit_s() -> float:
    """One request's bound: OmniSeek's own request timeout (http.DEFAULT_TIMEOUT), so a node "answers"
    exactly when OmniSeek's own fetch would. Every time bound of a sweep is a multiple of it."""
    from omniseek.core import http
    return float(http.DEFAULT_TIMEOUT)


def _user_agent(url: str) -> str:
    from omniseek.core import http, upstreams
    return upstreams.user_agent_for(url) or http.USER_AGENT


def failed_urls(records: list) -> list[str]:
    """The addresses in a probe's failure records (diag.tap) that can be fetched again: http(s), public
    host, not held back by OmniSeek itself, no redacted part. In order, without repeats."""
    out: list[str] = []
    for rec in records or ():
        if not isinstance(rec, dict):
            continue
        url = str(rec.get("url") or "")
        body = str(rec.get("body") or "")
        if any(m in body for m in _NOT_SENT_MARKERS) or any(m in url for m in _MASKS):
            continue
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if p.scheme not in ("http", "https") or not host or host == "localhost" or host.endswith(".local"):
            continue
        try:
            ip = ipaddress.ip_address(host)
            if not ip.is_global:
                continue
        except ValueError:
            pass
        if url not in out:
            out.append(url)
    return out


def failed_hosts(records: list) -> set:
    """The hosts of ``failed_urls(records)``."""
    return {urlparse(u).hostname for u in failed_urls(records)} - {None}


def only_hosts(records: list, hosts: set) -> list:
    """The records whose address is on one of ``hosts``."""
    return [r for r in records or () if isinstance(r, dict) and urlparse(str(r.get("url") or "")).hostname in hosts]


def refetch_headers(records: list) -> dict:
    """{address: the request headers its failure record says a refetch must send} for the records that
    carry them (diag.note ``headers``: a site that answers only a browser's navigation request, levels.fyi
    behind AWS WAF). An address without them is refetched with OmniSeek's own User-Agent."""
    out: dict = {}
    for rec in records or ():
        if isinstance(rec, dict) and isinstance(rec.get("headers"), dict) and rec.get("url"):
            out.setdefault(str(rec["url"]), dict(rec["headers"]))
    return out


def _targets(failed: dict) -> tuple[list[str], dict, list[str], dict]:
    """``failed`` {source: [failure records]} -> (one address per host, {host: [sources]}, the sources
    that left no address, {address: refetch headers} for the addresses whose record names them). One per
    host: a node's address is refused or reached per host, and it keeps the number of fetches per node
    at the number of hosts."""
    urls: list[str] = []
    by_host: dict[str, list[str]] = {}
    none: list[str] = []
    headers: dict = {}
    for src in sorted(failed):
        mine = failed_urls(failed[src])
        hdrs = refetch_headers(failed[src])
        if not mine:
            none.append(src)
        for u in mine:
            h = urlparse(u).hostname or u
            if h not in by_host:
                by_host[h] = []
                urls.append(u)
                if u in hdrs:
                    headers[u] = hdrs[u]
            if src not in by_host[h]:
                by_host[h].append(src)
    return urls, by_host, none, headers


class _Mihomo:
    """A mihomo control socket (the proxy app's, or egress2's): read a group, read the proxies, select."""

    def __init__(self, sock: str, limit_s: float):
        self.sock = sock
        self._c = httpx.Client(transport=httpx.HTTPTransport(uds=sock), base_url="http://mihomo",
                               timeout=limit_s, trust_env=False)

    def close(self) -> None:
        self._c.close()

    def group(self, name: str) -> dict:
        r = self._c.get("/proxies/" + quote(name, safe=""))
        r.raise_for_status()
        return r.json()

    def proxies(self) -> dict:
        r = self._c.get("/proxies")
        r.raise_for_status()
        return r.json().get("proxies") or {}

    def select(self, group: str, node: str) -> None:
        r = self._c.put("/proxies/" + quote(group, safe=""), json={"name": node})
        if r.status_code != 204:
            raise RuntimeError(f"选 {node} 回了 HTTP {r.status_code}")
        now = self.group(group).get("now")
        if now != node:
            raise RuntimeError(f"选了 {node}，组里现在是 {now}")


def _fetch_one(client: httpx.Client, url: str, headers: Optional[dict] = None) -> tuple[Optional[int], float, str]:
    t0 = time.monotonic()
    try:
        with client.stream("GET", url, headers=headers or {"User-Agent": _user_agent(url)}) as r:
            limited = r.status_code == 429 or (r.status_code == 403
                                               and r.headers.get("x-ratelimit-remaining") == "0")
            return r.status_code, time.monotonic() - t0, "限流" if limited else ""
    except Exception as exc:  # noqa: BLE001 -- any failure is a reading, never a crash of the sweep
        return None, time.monotonic() - t0, type(exc).__name__


def _fetch_round(urls: list[str], proxy: Optional[str], limit_s: float, headers: Optional[dict] = None) -> dict:
    """Fetch ``urls`` at once through ``proxy`` (None = this machine's default route, which is the
    proxy app's TUN for OmniSeek), each with OmniSeek's own User-Agent, or with the headers ``headers``
    {address: request headers} names for it (the ones its source sent), on fresh connections (a kept-alive
    one would stay on the node it was opened on). Only the status is read. The round returns within
    ``limit_s``: a fetch still running then reads as timed out. {url: (status or None, seconds, error)}."""
    client = httpx.Client(proxy=proxy, trust_env=False, follow_redirects=True, timeout=limit_s,
                          limits=httpx.Limits(max_keepalive_connections=0))
    pool = ThreadPoolExecutor(max_workers=max(1, len(urls)), thread_name_prefix="exit-node")
    futs = {pool.submit(_fetch_one, client, u, (headers or {}).get(u)): u for u in urls}
    done, pending = wait(futs, timeout=limit_s)
    out = {}
    for f, u in futs.items():
        out[u] = f.result() if f in done else (None, limit_s, "超时")
    pool.shutdown(wait=False, cancel_futures=True)
    if not pending:
        client.close()  # with fetches still running the client is left to them; they end at their own timeout
    return out


def _ok(v) -> bool:
    """One fetch answered: 200, a rate limit (``_fetch_one`` marks it), or 401: the node reaches the site.
    A failure record never carries the key its source sent (diag.note drops credential headers), so a
    keyed API answers every refetch 401 on a node that reaches it (llm_leaderboard, 11-10); a wall
    answers 202, 403 or 405, never 401."""
    return v[0] in (200, 401) or v[2] == "限流"


def _all_ok(reading: dict) -> bool:
    return bool(reading) and all(_ok(v) for v in reading.values())


def _total_s(reading: dict) -> float:
    return sum(v[1] for v in reading.values())


def _bad(reading: dict) -> str:
    """The failed fetches of one reading, short: ``host 202, host2 ConnectError``."""
    parts = []
    for u, (st, _s, err) in reading.items():
        if not _ok((st, _s, err)):
            parts.append(f"{urlparse(u).hostname} {st if st is not None else err}")
    return ", ".join(parts)


# The billing rate the subscription writes as the last part of a node's name ("... | x2", "... | x0.8").
_RATE = re.compile(r"[x×]\s*(\d+(?:\.\d+)?)\s*$", re.IGNORECASE)


def _rate(name: str) -> float:
    """A node's billing rate from its name; 1 when the name carries none."""
    m = _RATE.search(name.rsplit("|", 1)[-1].strip())
    return float(m.group(1)) if m else 1.0


def _order(candidates: list[str], known: dict) -> list[str]:
    """Best first by egress2's own health readings of each node (its provider checks every node against
    api.github.com every minute): alive before dead, then the lower last delay; a node it has no reading
    of comes after, in the selector's order. A measurement, not a choice of ours."""
    def key(item):
        i, name = item
        p = known.get(name) or {}
        hist = p.get("history") or []
        delay = (hist[-1] or {}).get("delay") if hist else None
        alive = p.get("alive") is True
        return (0 if alive else 1, delay if isinstance(delay, (int, float)) and delay > 0 else float("inf"), i)
    return [n for _i, n in sorted(enumerate(candidates), key=key)]


def _notify(title: str, body: str) -> None:
    """One notice through the push outlet, under the source-health job's source (its route reaches the
    operator who owns OmniSeek's exit). Fail-open, as every infra alert."""
    try:
        from omniseek.core import notify
        notify.alert(title, body, source="eye.infra_jobs")
    except Exception as exc:  # noqa: BLE001
        log.debug("exit-node notice swallowed (%s)", exc)


def _egress2_probe() -> tuple[Optional[dict], str]:
    """(port, group, control socket) of egress2's test port from its state file, or (None, why)."""
    path = where.egress2_state()
    if path is None:
        return None, "这套部署没有 egress2"
    try:
        st = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"读不了 egress2 的 state.json（{type(exc).__name__}）"
    port, group, sock = st.get("probe"), st.get("probe_group"), st.get("control_sock")
    skipped = st.get("regions_skipped") or []
    pool = [f"{st['group']}-{r}" for r in (st.get("regions") or []) if r not in skipped] if st.get("group") else []
    if not (isinstance(port, int) and group and sock and pool):
        return None, "egress2 的 state.json 里没有测试口 probe、probe_group、control_sock 或地区组 group、regions"
    return {"port": port, "group": group, "sock": sock, "pool": pool}, ""


def _lock():
    """One sweep at a time on this machine (the job and a hand run would switch the same groups)."""
    path = where.state() / "exit-node.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def sweep(failed: dict, deadline: float, *, why: str) -> dict:
    """Test the nodes for the sources in ``failed`` ({source: [diag failure records]}) and switch the
    proxy app's selector to the first node, in egress2's order, that reaches all of them, before
    ``deadline`` (time.monotonic()).

    Time (all from OmniSeek's request timeout L = http.DEFAULT_TIMEOUT): one node costs at most 2L (the
    switch of the test group, bounded by L; one round of fetches, bounded by L), the check on the node in
    use the same 2L. After the nodes come at most 4L: the selector switch, the round on the default route,
    a switch back, the notice. A node is started only while now + 2L + 4L is before ``deadline``; the
    nodes left are named in the notice. So how many nodes get tested follows from the deadline and how
    long each really took, never from a count picked in advance.

    Returns a summary dict (also written to <state>/exit-node-last.json) whose ``outcome`` is one of:
    not-live, no-url, futile-known, no-probe, control-down, busy, blip, no-node, switched, rolled-back,
    switch-failed."""
    not_live = where.not_live_reason()
    if not_live:
        # a rehearsal or a test home must never switch the operator's proxy
        log.info("exit-node: skipped, this process is not the live eye (%s)", not_live)
        return {"outcome": "not-live", "why": why, "reason": not_live}
    L = _limit_s()
    tail = 4 * L
    fh = _lock()
    if fh is None:
        log.warning("exit-node: another sweep is running; this one (%s) skipped", why)
        return {"outcome": "busy", "why": why}
    try:
        res = _sweep_locked(failed, deadline, why, L, tail)
    finally:
        fh.close()
    try:
        out = where.state() / "exit-node-last.json"
        out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        log.warning("exit-node: summary not written (%s)", exc)
    return res


def _sweep_locked(failed: dict, deadline: float, why: str, L: float, tail: float) -> dict:
    urls, by_host, no_url, hdrs = _targets(failed)
    res: dict = {"why": why, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "sources": sorted(failed),
                 "hosts": by_host, "no_url": no_url}
    head = f"为 {len(failed)} 个源（{why}）"
    if not urls:
        res["outcome"] = "no-url"
        _notify("出口节点 · 没测", f"{head}：都没留下可重取的网址（{', '.join(no_url)}），没测节点。")
        return res
    hosts_line = "；".join(f"{h}（{', '.join(s)}）" for h, s in by_host.items())
    if no_url:
        hosts_line += f"（没留下可重取网址、不参与的源：{', '.join(no_url)}）"
    futile = _futile_load()
    known = _futile_match(futile, set(by_host))
    if known is not None:
        # these hosts were found past the selector before: one round on the default route, and only a
        # host answering there lets the sweep go on (the batch is dropped); else no test, no switch
        if time.monotonic() + L <= deadline:
            now_read = _fetch_round(urls, None, L, hdrs)
            res["default_route"] = {u: list(v) for u, v in now_read.items()}
        else:
            now_read = {}
        if not _futile_forget(futile, now_read):
            res["outcome"] = "futile-known"
            res["futile"] = known
            until = time.strftime("%Y-%m-%d %H:%M", time.localtime(known["ts"] + FUTILE_TTL_S))
            if time.time() - float(known.get("notified") or 0) >= _FUTILE_NOTICE_S:
                known["notified"] = time.time()
                _futile_save(futile)
                seen = f"默认路由上重取：{_bad(now_read)}。" if now_read else ""
                _notify("出口节点 · 没测切", f"{head}，站：{hosts_line}。这几个站按代理规则走的不是选择节点，切节点帮不上："
                        f"{known.get('at')} 切到 {known.get('node')} 后在默认路由上仍不通，记录留到 {until}。{seen}"
                        f"没测、没切（这批一天至多知会一次）。")
            else:
                log.warning("exit-node: %s are in the futile record (since %s, until %s); no test, no switch, "
                            "notice already sent today", ", ".join(by_host), known.get("at"), until)
            return res
    eg2, reason = _egress2_probe()
    if eg2 is None:
        res["outcome"] = "no-probe"
        _notify("出口节点 · 没测", f"{head}：{reason}。要测的站：{hosts_line}")
        return res
    proxy = f"http://127.0.0.1:{eg2['port']}"
    app = egress = None
    try:
        app = _Mihomo(str(where.proxy_control()), L)
        egress = _Mihomo(eg2["sock"], L)
        try:
            sel = app.group(where.PROXY_SELECTOR)
        except Exception as exc:  # noqa: BLE001
            res["outcome"] = "control-down"
            _notify("出口节点 · 没测", f"{head}：代理软件的控制口 {app.sock} 读不到「{where.PROXY_SELECTOR}」"
                    f"（{type(exc).__name__}），没测。要测的站：{hosts_line}")
            return res
        try:
            probe_members = list(egress.group(eg2["group"]).get("all") or [])
            known = egress.proxies()
        except Exception as exc:  # noqa: BLE001
            res["outcome"] = "control-down"
            _notify("出口节点 · 没测", f"{head}：egress2 的控制口读不到测试组 {eg2['group']}"
                    f"（{type(exc).__name__}），没测。要测的站：{hosts_line}")
            return res
        original = sel.get("now")
        res["original"] = original
        have = set(probe_members)
        if original in have and time.monotonic() + 2 * L + tail <= deadline:
            try:
                egress.select(eg2["group"], original)
                pre = _fetch_round(urls, proxy, L, hdrs)
            except Exception as exc:  # noqa: BLE001
                pre = {u: (None, 0.0, type(exc).__name__) for u in urls}
            res["pre"] = {u: list(v) for u, v in pre.items()}
            if _all_ok(pre):
                res["outcome"] = "blip"
                log.warning("exit-node: %s all answer on %s through the test port: a blip, no sweep, no notice",
                            ", ".join(by_host), original)
                return res
            pre_line = f"当前节点 {original} 上重取：{_bad(pre)}"
        else:
            pre_line = (f"当前节点 {original} 不在 egress2 的订阅里，没先测" if original not in have
                        else "时间预算不够先测当前节点")
        pool = {n for g in eg2["pool"] for n in ((known.get(g) or {}).get("all") or [])}
        candidates = _order([m for m in (sel.get("all") or [])
                             if m in have and m in pool and m != original and _rate(m) <= 1], known)
        res["pool"] = len(candidates)
        readings: dict = {}
        untested: list[str] = []
        for i, node in enumerate(candidates):
            if time.monotonic() + 2 * L + tail > deadline:
                untested = candidates[i:]
                break
            try:
                egress.select(eg2["group"], node)
            except Exception as exc:  # noqa: BLE001
                readings[node] = {"_switch": (None, 0.0, type(exc).__name__)}
                continue
            readings[node] = _fetch_round(urls + [r for r in REFERENCES if r not in urls], proxy, L, hdrs)
            if _all_ok(readings[node]):
                break  # the first passing node in egress2's order wins; the rest are not needed
        res["readings"] = {n: {u: list(v) for u, v in r.items()} for n, r in readings.items()}
        res["untested"] = untested
        passing = [n for n, r in readings.items() if _all_ok(r)]
        summary = _summary(readings, passing)
        left = f"；预算内没测到 {len(untested)} 个：{', '.join(untested)}" if untested else ""
        if not passing:
            res["outcome"] = "no-node"
            verdict = ("都不通：是站点那头，或只有池外、倍率高于 1 的节点才通（那要人定）" if not untested
                       else "测了的都不通，没测完，还不能断定是站点那头")
            _notify("出口节点 · 没切", f"{head}，要测的站：{hosts_line}。{pre_line}。\n"
                    f"egress2 节点池里倍率不高于 1 的 {res['pool']} 个节点测了 {len(readings)} 个，{verdict}，"
                    f"没切（仍是 {original}）{left}。\n{summary}")
            return res
        best = passing[0]
        res["chosen"] = best
        try:
            app.select(where.PROXY_SELECTOR, best)
        except Exception as exc:  # noqa: BLE001
            res["outcome"] = "switch-failed"
            _notify("出口节点 · 没切成", f"{head}：按 egress2 的读数排第一个全通的是 {best}，经代理软件的控制口切「{where.PROXY_SELECTOR}」没成"
                    f"（{exc}），仍是 {original}。\n{summary}")
            return res
        after = _fetch_round(urls, None, L, hdrs)
        res["after"] = {u: list(v) for u, v in after.items()}
        _futile_forget(futile, after)
        if _all_ok(after):
            res["outcome"] = "switched"
            log.warning("exit-node: switched %s from %s to %s for %s", where.PROXY_SELECTOR, original, best,
                        ", ".join(sorted({s for v in by_host.values() for s in v})))
            _notify("出口节点 · 已切", f"「{where.PROXY_SELECTOR}」由 {original} 切到 {best}，{head}，"
                    f"站：{hosts_line}。{pre_line}；切完在默认路由上重取都回 200。\n{summary}{left}")
            return res
        try:
            app.select(where.PROXY_SELECTOR, original)
            back = f"已切回 {original}"
        except Exception as exc:  # noqa: BLE001
            back = f"切回 {original} 没成（{exc}），现在仍是 {best}"
        res["outcome"] = "rolled-back"
        _futile_record(futile, {urlparse(u).hostname for u, v in after.items() if not _ok(v)} - {None},
                       res["at"], best, original)
        log.warning("exit-node: %s on %s still failed on the default route (%s); %s",
                    ", ".join(by_host), best, _bad(after), back)
        _notify("出口节点 · 切了没用，已切回", f"{head}：切到 {best}（经测试口全通）后在默认路由上重取仍不通：{_bad(after)}"
                f"（这些站可能按代理软件的规则走了别的组），{back}。这批站记下了，{FUTILE_TTL_S // 86400} 天内同一批再坏不再测切。\n{summary}")
        return res
    finally:
        for m in (app, egress):
            if m is not None:
                m.close()


# 2026-11-06: the batches of hosts a switch did not help. One small file in OmniSeek's
# state directory, read and written only under the sweep lock.
_FUTILE_FILE = "exit-node-futile.json"
# How long a batch is trusted: 7 days, PROVISIONAL. Basis: the proxy app's rules are the operator's and are
# not changed by anything here; a week without a rule change is the
# usual case, so within it a host the rules send past the selector stays there. Past it the next sweep for
# those hosts tests and switches again, which re-checks the record itself (a rule change in between shows
# as a switch that helps). Recalibrate from the outcomes in exit-node-last.json and the notices: if the
# sweeps after expiry keep ending rolled-back for the same hosts, lengthen it; if a rule change inside the
# week ever left a fixable host unswitched, shorten it. Recheck by 2026-12-06.
FUTILE_TTL_S = 7 * 86400
_FUTILE_NOTICE_S = 86400  # one notice per batch a day: the health job runs every 6 hours


def _futile_path() -> Path:
    return where.state() / _FUTILE_FILE


def _futile_load() -> list[dict]:
    """The batches still inside FUTILE_TTL_S; an unreadable file reads as none (the old behaviour)."""
    try:
        data = json.loads(_futile_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    now = time.time()
    out = []
    for b in (data.get("batches") if isinstance(data, dict) else None) or ():
        if (isinstance(b, dict) and isinstance(b.get("hosts"), list) and b["hosts"]
                and isinstance(b.get("ts"), (int, float)) and now - b["ts"] < FUTILE_TTL_S):
            out.append(b)
    return out


def _futile_save(batches: list[dict]) -> None:
    try:
        path = _futile_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"batches": batches}, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        log.warning("exit-node: futile record not written (%s)", exc)


def _futile_match(batches: list[dict], hosts: set) -> Optional[dict]:
    """The first batch holding every one of ``hosts``, or None."""
    return next((b for b in batches if hosts and hosts <= set(b["hosts"])), None)


def _futile_forget(batches: list[dict], reading: dict) -> list[str]:
    """Drop the batches with a host that answered in ``reading`` (a round on the default route). Returns
    the hosts of the batches dropped; writes the file only when one was."""
    answered = {urlparse(u).hostname for u, v in reading.items() if _ok(v)}
    gone = [b for b in batches if answered & set(b["hosts"])]
    if gone:
        batches[:] = [b for b in batches if b not in gone]
        _futile_save(batches)
        log.warning("exit-node: %s answered on the default route; futile record dropped for %s",
                    ", ".join(sorted(h for h in answered if h)), ", ".join(h for b in gone for h in b["hosts"]))
    return [h for b in gone for h in b["hosts"]]


def _futile_record(batches: list[dict], hosts: set, at: str, node: str, original: Optional[str]) -> None:
    """Remember the hosts a switch to ``node`` did not help; replaces an older batch of the same hosts.
    The rolled-back notice just sent counts as that batch's notice of the day."""
    now = time.time()
    batches[:] = [b for b in batches if set(b["hosts"]) != hosts]
    batches.append({"hosts": sorted(hosts), "ts": now, "at": at, "node": node, "original": original,
                    "notified": now})
    _futile_save(batches)


def _summary(readings: dict, passing: list) -> str:
    """Each tested node's reading, one line: the passing one first with its total time, then the failing
    ones with what failed."""
    lines = [f"- {n}：全通 {_total_s(readings[n]):.1f}s" for n in passing]
    lines += [f"- {n}：{_bad(r)}" for n, r in readings.items() if n not in passing]
    return "各节点读数：\n" + "\n".join(lines) if lines else "没有节点读数。"


def sweep_for(names: list[str], deadline: Optional[float] = None) -> dict:
    """The hand entry (``main``; from a thread with a bound portal): probe the named sources once (the
    source-health probe, tapped), take the
    addresses they failed on (a degraded bundle's dead feeds count too) and run ``sweep``. The deadline
    defaults to the source-health job's budget from now, so a hand run is bounded like the job's."""
    from omniseek.core import fetcher, infra_jobs
    from omniseek.server import load_sources
    load_sources()
    failed: dict = {}
    report: dict = {}
    for n in names:
        a = fetcher.get_adapter(n)
        if a is None:
            report[n] = "没有这个源"
            continue
        noted: dict = {}
        ok, msg, _done = infra_jobs._probe_tapped(n, a, noted)
        report[n] = f"{ok}: {msg[:120]}"
        if failed_urls(noted.get(n)):
            failed[n] = noted[n]
    if not failed:
        return {"outcome": "nothing-failed", "probes": report}
    if deadline is None:
        deadline = time.monotonic() + infra_jobs.HEALTH_BUDGET_S
    res = sweep(failed, deadline, why="手动：" + ", ".join(names))
    res["probes"] = report
    return res


def main(argv: Optional[list] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or any(a.startswith("-") for a in args):
        print("usage: python -m omniseek.core.exit_node <source> [<source> ...]", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from omniseek.core import portal
    # a process of its own has no service loop: the probes reach the sources through this one
    res = portal.run_standalone(sweep_for, args)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res.get("outcome") in ("switched", "blip", "no-node", "futile-known") else 1


if __name__ == "__main__":
    raise SystemExit(main())
