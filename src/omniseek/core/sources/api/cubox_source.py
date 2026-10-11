"""Cubox: the card list of the owner's Cubox account (list only, never the article body).

Feeds the shadow run (``shadow_feed = True``): a daily sensor over this source finds the new
cards and the sensor writes them to ``~/.omniseek/state/shadow_feed/cubox.jsonl`` (format in
``omniseek.core.shadow_feed``). The digest's own Cubox puller keeps running until the comparison
passes; this source only LISTS.

Endpoint: ``POST https://cubox.pro/c/api/cli/card/filter`` (the endpoint the official cubox-cli and
the digest puller call) with ``Authorization: Bearer <key>`` and a JSON body ``{limit, archived,
last_card_id?}``. Answer ``{code, message, data: [card]}``; ``code == 200`` is success. A card carries
id, title, article_title, description, domain, url, create_time (``2026-09-24T20:32:25.123+0800``),
tags, folder. The filter answers ONE archive state per call, and the digest puller archives every card
it has ingested, so a run lists both ``archived: false`` and ``archived: true``, newest first, paging
by ``last_card_id`` until a card is older than the lookback.

Credential: ``~/.omniseek/credentials/cubox.json`` holding ``{"token": "<API key>"}`` (the whole API
link pasted as the token also works: its last path segment is the key, the puller's rule). No file,
or no key in it: the search returns ``[]`` with a diagnostic saying so; it never raises and never
guesses another location.

Quota (the hard limit): Cubox caps API calls per account per day, 200 on a standard account and 500 on
a premium one (help.cubox.pro/save/89d3, checked 2026-10-10), and answers ``-3030`` past it; the
puller assumes 500 and which tier this account has is not measured yet. The
quota is SHARED with the digest puller, so this source keeps its own persistent counter
(``~/.omniseek/state/cubox_budget.json``, override ``$EYE_CUBOX_BUDGET_PATH``) and runs only when:
- fewer than 50 calls of ours today (local day, the puller's day),
- at least 2 hours since our last run,
- Cubox has not answered ``-3030`` today (to us, or to the puller: read from its state file,
  ``$EYE_CUBOX_PULLER_STATE``, read only),
- the estimated remaining quota, 500 minus the puller's calls minus ours, is at least 60 (the API
  reports no remaining count, so this is an estimate).
A refused run returns ``[]`` with a diagnostic naming the reason. Results are cached 2 hours, so a
named drill right after the sensor run is served from cache and spends nothing.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from omniseek.core import auth, diag, http
from omniseek.core.normalize import Document, jsonsafe, keyword_score_filter
from omniseek.core.sources.api._base import BaseAPIAdapter

logger = logging.getLogger(__name__)

BASE = "https://cubox.pro"
FILTER_PATH = "/c/api/cli/card/filter"
CRED_NAME = "cubox"
PAGE = 50
MAX_PAGES_PER_STATE = 8
LOOKBACK_DAYS = 7
TIMEOUT = 30

DAILY_CALL_CAP = 50
MIN_INTERVAL_S = 2 * 3600
ASSUMED_DAILY_QUOTA = 500
STOP_BELOW_REMAINING = 60

ENV_BUDGET = "EYE_CUBOX_BUDGET_PATH"
ENV_PULLER_STATE = "EYE_CUBOX_PULLER_STATE"
_DEFAULT_PULLER_STATE = None  # the public build ships no default puller state

_WILDCARD = {"", "*", "all"}
_ERR_TEXT = {
    3030: "Cubox says today's call quota is used up (-3030); listing resumes tomorrow",
    1025: "Cubox says the API needs a premium account (1025)",
    1100: "Cubox rejected the API key (1100): re-copy it into ~/.omniseek/credentials/cubox.json",
}
_budget_lock = threading.Lock()


def _budget_path() -> Path:
    env = os.environ.get(ENV_BUDGET, "").strip()
    return Path(env).expanduser() if env else Path.home() / ".omniseek" / "state" / "cubox_budget.json"


def _puller_state_path() -> Optional[Path]:
    env = os.environ.get(ENV_PULLER_STATE, "").strip()
    return Path(env).expanduser() if env else _DEFAULT_PULLER_STATE


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def load_budget() -> dict:
    try:
        b = json.loads(_budget_path().read_text(encoding="utf-8"))
        if not isinstance(b, dict):
            b = {}
    except (OSError, ValueError):
        b = {}
    if b.get("day") != _today():
        b = {"day": _today(), "calls": 0, "last_run_at": b.get("last_run_at")}
    b.setdefault("calls", 0)
    return b


def save_budget(b: dict) -> None:
    path = _budget_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(b, fh, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def puller_budget() -> Optional[dict]:
    """The digest puller's budget for TODAY (``{"day", "calls", "capped_at"?}``), or None when its
    state file is missing / unreadable / from another day. Reads ``state.json`` only."""
    path = _puller_state_path()
    if path is None:
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    b = (state or {}).get("budget") if isinstance(state, dict) else None
    if not isinstance(b, dict) or b.get("day") != _today():
        return None
    return b


def gate_reason(b: dict, now: Optional[float] = None) -> Optional[str]:
    """Why a run may NOT start now (None = it may)."""
    now = time.time() if now is None else now
    if b.get("capped"):
        return _ERR_TEXT[3030]
    if int(b.get("calls") or 0) >= DAILY_CALL_CAP:
        return f"cubox: our daily cap of {DAILY_CALL_CAP} calls is spent; listing resumes tomorrow"
    last = b.get("last_run_at")
    if isinstance(last, (int, float)) and now - last < MIN_INTERVAL_S:
        nxt = _dt.datetime.fromtimestamp(last + MIN_INTERVAL_S).strftime("%H:%M")
        return f"cubox: at most one run per 2 hours; next run allowed after {nxt} (results of the last run are cached)"
    return _quota_reason(b)


def _call_reason(b: dict) -> Optional[str]:
    """Why the NEXT call of a run already under way may not go out (None = it may)."""
    if b.get("capped"):
        return _ERR_TEXT[3030]
    if int(b.get("calls") or 0) >= DAILY_CALL_CAP:
        return f"cubox: our daily cap of {DAILY_CALL_CAP} calls was reached mid-run; the rest waits for tomorrow"
    return _quota_reason(b)


def _quota_reason(b: dict) -> Optional[str]:
    pb = puller_budget()
    if pb is not None and "capped_at" in pb:
        return "cubox: the digest puller hit today's quota (-3030); listing resumes tomorrow"
    puller_calls = int((pb or {}).get("calls") or 0)
    remaining = ASSUMED_DAILY_QUOTA - puller_calls - int(b.get("calls") or 0)
    if remaining < STOP_BELOW_REMAINING:
        return (f"cubox: estimated remaining quota {remaining} < {STOP_BELOW_REMAINING} "
                f"(assumed {ASSUMED_DAILY_QUOTA}/day, puller {puller_calls} calls, ours {b.get('calls')}); "
                "stopped for today")
    return None


def _token() -> str:
    cred = auth.load(CRED_NAME) or {}
    tok = ""
    if isinstance(cred, dict):
        tok = str(cred.get("token") or cred.get("api_key") or cred.get("api_link") or "")
    tok = tok.strip().rstrip("/")
    if "/" in tok:
        tok = tok.rsplit("/", 1)[-1]
    return tok


_TZ_NOCOLON = re.compile(r"([+-]\d{2})(\d{2})$")


def parse_time(value) -> Optional[_dt.datetime]:
    if not value or not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    s = _TZ_NOCOLON.sub(r"\1:\2", s)
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)


def _note(reason: str) -> None:
    logger.info("%s", reason)
    diag.note("cubox.list", url=BASE + FILTER_PATH, body=reason)


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class CuboxAdapter(BaseAPIAdapter):
    name = "cubox"
    description = (
        "Cubox: the owner's saved cards, list only (id, url, title, saved time, folder, tags), "
        "newest first over the last 7 days; no article body. Personal feed for a digest shadow run."
    )
    needs_credentials = True
    explicit_only = (
        "personal Cubox card list on a daily call quota shared with the digest puller: "
        "named drill or its sensor only, never the broad sweep"
    )
    kind = "stream"
    domains = ["bookmarks"]
    regions: list = []
    modes = ["MONITOR", "RECALL"]
    rank_locally = False
    cache_ttl = MIN_INTERVAL_S
    search_label = "cards"
    url_host = "cubox.pro"
    sensor_window = 2 * PAGE * MAX_PAGES_PER_STATE
    shadow_feed = True

    def search(self, query: str, limit: int = 10) -> list[Document]:
        # ONE cache entry and ONE upstream run serve every query and limit: the quota is too tight for
        # a separate run per drill. A non-wildcard query filters the cached window lexically.
        docs = super().search("*", self.sensor_window)
        q = (query or "").strip().lower()
        if q not in _WILDCARD:
            docs = keyword_score_filter(docs, query)
        return docs[:limit]

    def _raw_fetch(self, query: str, limit: int) -> list:
        token = _token()
        if not token:
            _note("cubox: no credential; put {\"token\": \"<API key>\"} in ~/.omniseek/credentials/cubox.json "
                  "(nothing was sent)")
            return []
        with _budget_lock:
            budget = load_budget()
            reason = gate_reason(budget)
            if reason:
                _note(reason)
                return []
            budget["last_run_at"] = time.time()
            save_budget(budget)
        cutoff = _now() - _dt.timedelta(days=LOOKBACK_DAYS)
        cards: dict[str, dict] = {}
        stop = False
        for archived in (False, True):
            last_id = None
            for _ in range(MAX_PAGES_PER_STATE):
                with _budget_lock:
                    budget = load_budget()
                    reason = _call_reason(budget)
                    if reason:
                        _note(reason)
                        stop = True
                        break
                    budget["calls"] = int(budget.get("calls") or 0) + 1
                    save_budget(budget)
                body = {"limit": PAGE, "archived": archived}
                if last_id:
                    body["last_card_id"] = last_id
                payload = http.post_json(
                    BASE + FILTER_PATH, json=body, timeout=TIMEOUT,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                if not isinstance(payload, dict):
                    _note(f"cubox: the card list request failed (archived={archived})")
                    stop = True
                    break
                code = payload.get("code")
                if code != 200:
                    c = abs(code) if isinstance(code, int) else code
                    if c == 3030:
                        with _budget_lock:
                            budget = load_budget()
                            budget["capped"] = True
                            budget["capped_at"] = budget.get("calls")
                            save_budget(budget)
                    _note(_ERR_TEXT.get(c) or f"cubox: API answered code {code}: {payload.get('message') or ''}")
                    stop = True
                    break
                page = payload.get("data") or []
                older = False
                for card in page:
                    if not isinstance(card, dict) or not card.get("id"):
                        continue
                    t = parse_time(card.get("create_time"))
                    if t is not None and t < cutoff:
                        older = True
                        break
                    cards.setdefault(str(card["id"]), {**card, "_archived": archived})
                if older or len(page) < PAGE:
                    break
                last_id = page[-1].get("id")
            if stop:
                break
        epoch = _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)
        return sorted(cards.values(), key=lambda c: parse_time(c.get("create_time")) or epoch, reverse=True)

    def health_check(self) -> tuple[Optional[bool], str]:
        if not _token():
            return False, "no credential: ~/.omniseek/credentials/cubox.json is missing or has no token"
        return None, "not probed: the daily call quota is shared with the digest puller"

    def _to_document(self, card: dict) -> Optional[Document]:
        cid = str(card.get("id") or "")
        if not cid:
            return None
        cubox_url = f"{BASE}/web/card/{cid}"
        url = card.get("url") or cubox_url
        title = card.get("title") or card.get("article_title") or url
        folder = card.get("folder") or {}
        folder_name = (folder.get("nested_name") or folder.get("name")) if isinstance(folder, dict) else None
        tags = []
        for t in card.get("tags") or []:
            name = (t.get("nested_name") or t.get("name")) if isinstance(t, dict) else t
            if name:
                tags.append(str(name))
        return Document(
            source="cubox",
            source_id=cid,
            url=url,
            title=title,
            content=card.get("description") or "",
            date=parse_time(card.get("create_time")),
            tags=tags,
            metadata={
                "cubox_url": cubox_url,
                "folder": folder_name,
                "domain": card.get("domain"),
                "archived": bool(card.get("_archived")),
                "article_title": card.get("article_title"),
                "update_time": card.get("update_time"),
                "raw": jsonsafe({k: v for k, v in card.items() if k != "_archived"}),
            },
        )
