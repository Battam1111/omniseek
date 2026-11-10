"""Does this host name exist in public DNS? Asked only after a read failed before any answer.

SMALL-BATCH-4 (2026-11-09). The host machine's main proxy runs in fake-ip mode: every name first resolves to
a 30.0.x.x stand-in address, so a name that does not exist at all still "resolves", and the connection
is cut at the TLS handshake. OmniSeek then reported SSL_ERROR_SYSCALL or ConnectError and the reader took
it for a broken network (smartech.gatech.edu, gone since Georgia Tech moved to repository.gatech.edu,
cost the research line several calls on 11-09). The same class as brain
finding-eye-definitive-absence-vs-unhealthy-upstream-2026-11-07: "definitely not there" reported as
"the upstream is broken".

So a read that failed at the connection or TLS step asks one public DoH resolver about the name, through
OmniSeek's own second exit egress2 (its mixed port, read from its state file; the main proxy's fake-ip
answer is exactly what cannot be trusted here). NXDOMAIN is a definite answer: the caller reports it and
treats the read as settled (not a source fault, not counted toward a breaker). Any other outcome (the
name exists, the lookup failed, no egress2) returns None and the caller reports its original error.
A successful read never comes here. A lookup that could not be made (no egress2 state, the DoH request
failed) leaves a WARNING line and, once per process, one eye alert, so the check does not go quiet
unseen.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Optional

import httpx

from omniseek import where

logger = logging.getLogger(__name__)

DOH_URL = "https://cloudflare-dns.com/dns-query"   # Cloudflare's DoH JSON API (RFC 8484 JSON form)
_NXDOMAIN = 3   # DNS RCODE 3, the "Status" field of a DoH JSON answer
_alerted = threading.Event()   # one alert per process for a lookup that could not be made
_TIMEOUT_S = 10.0   # the read timeout of safeurl.safe_fetch, the generic read this follows


def _egress2_proxy() -> Optional[str]:
    path = where.egress2_state()
    try:
        port = json.loads(path.read_text(encoding="utf-8")).get("mixed") if path else None
    except (OSError, ValueError):
        return None
    return f"http://127.0.0.1:{int(port)}" if port else None


def _trouble(host: str, why: str) -> None:
    logger.warning("dns_absence: cannot ask public DNS about %s: %s", host, why)
    if _alerted.is_set():
        return
    _alerted.set()
    try:
        from omniseek.core import notify
        notify.alert("眼查不了公网 DNS", f"读网址失败后查域名在不在时查不了（{host}：{why}）。这期间域名不存在的网址"
                                       f"照旧报连接错误，不报「域名查不到」。本进程只报这一次（omniseek.core.dns_absence）")
    except Exception as exc:  # noqa: BLE001 (an alert that fails must not break the read)
        logger.warning("dns_absence: alert failed: %s", exc)


def absent(host: str) -> Optional[str]:
    """The sentence to report when public DNS says ``host`` does not exist (NXDOMAIN); else None (it
    exists, or the lookup itself failed: then nothing is known and the original error stands)."""
    host = (host or "").strip().rstrip(".").lower()
    proxy = _egress2_proxy()
    if not host:
        return None
    if proxy is None:
        _trouble(host, f"no egress2 mixed port in {where.egress2_state()}")
        return None
    try:
        with httpx.Client(proxy=proxy, trust_env=False, timeout=_TIMEOUT_S) as client:
            resp = client.get(DOH_URL, params={"name": host, "type": "A"},
                              headers={"accept": "application/dns-json"})
            resp.raise_for_status()
            status = resp.json().get("Status")
    except Exception as exc:  # noqa: BLE001 (no answer about the name: the original error stands)
        _trouble(host, f"DoH {DOH_URL} via {proxy}: {type(exc).__name__}: {exc}")
        return None
    if status != _NXDOMAIN:
        return None
    return (f"这个域名在公网 DNS 上查不到：{host}（公网 DoH 回 NXDOMAIN）。网址里的站点已不存在或拼错了，"
            f"不是网络故障；换成它现在的地址再读")
