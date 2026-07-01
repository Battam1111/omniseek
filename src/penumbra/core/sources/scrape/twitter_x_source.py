"""X / Twitter — curated ML researchers via twscrape (direct GraphQL, burner cookie).

2026-05-31: migrated OFF RSSHub (its twitter route stayed unstable on public AND
self-hosted instances through 2026). Uses **twscrape** against X's GraphQL Web API
with a BURNER account's ``auth_token`` plus a **self-generated ct0** — X's CSRF is a
double-submit check (it only verifies the ct0 cookie matches the x-csrf-token
header, not the value), so a random ct0 used consistently works and we never need
the account's real ct0.

Account safety (the burner must not get banned) — LOW frequency BY DESIGN:
  * 1h result cache → X is hit at most once/hour for the whole handle set;
  * a small random jitter between per-handle fetches (no fixed cadence);
  * residential IP (the Mac mini); twscrape queues requests respecting X's
    per-endpoint rate windows.
Treat the burner as disposable — if X flags it the account goes read-only/inactive,
health_check reports it, and the health watchdog alerts.

asyncio isolation: twscrape is async; SourceAdapter.search is sync and may run on
FastMCP's event-loop thread, so every twscrape call goes through ``_run_async``
(fresh thread + new loop) — the same isolation the CDP adapters use.

Config: ``~/.polaris/credentials/twitter_x.json`` →
  {"auth_token": "...", "handles": ["karpathy", ...]}   (handles optional)
twscrape account db: ``~/.polaris/state/twscrape_accounts.db``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from penumbra.core import cache
from penumbra.core.fetcher import register_adapter
from penumbra.core.normalize import PolarisDocument, jsonsafe, keyword_score_filter, mk_signal

logger = logging.getLogger(__name__)

CRED = Path.home() / ".polaris" / "credentials" / "twitter_x.json"
DB_PATH = Path.home() / ".polaris" / "state" / "twscrape_accounts.db"
CACHE_TTL = 3600          # 1h — keep X access low-frequency (account safety)
PER_HANDLE_TWEETS = 20
_JITTER = (1.0, 3.5)      # random seconds between per-handle fetches
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)
_ID_CACHE = Path.home() / ".polaris" / "state" / "twitter_x_ids.json"  # handle→user_id


def _load_id_cache() -> dict:
    """handle(lower) → user_id. Caching this lets fetches SKIP the rate-limited
    UserByScreenName endpoint after each handle is resolved once — the root-cause fix
    for twitter_x's recurring rate-limit stalls (45 handles = 45 resolves every fetch)."""
    try:
        return json.loads(_ID_CACHE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_id_cache(d: dict) -> None:
    try:
        _ID_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _ID_CACHE.write_text(json.dumps(d), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

# Curated high-SNR starter (kept small → lower scrape pressure): a default set of
# frontier ML research accounts (the deployer overrides via twitter_x.json handles).
_DEFAULT_HANDLES = [
    "karpathy", "ylecun", "DrJimFan", "hardmaru", "_akhaliq", "DAIR_AI",
    "Yoshua_Bengio", "AaronCourville", "rogergrosse", "aidangomez", "nickfrosst",
    "AnthropicAI", "GoogleDeepMind", "huggingface", "VectorInst",
]

# ──────────────────────────────────────────────────────────────────────────────
# 🔓 UN-SEALED 2026-06-04 — FRESH burner auth_token provisioned by the operator + verified
# working (resolves handles in ~2s each, no hang). The prior token had died X-side
# (local active=1 was a stale flag). ⚠️ ON A TOKEN SWAP you MUST first clear the old
# twscrape account row (`DELETE FROM accounts` in twscrape_accounts.db) — else
# _ensure_account sees the stale active=1 and never adds the new cookie.
# SAFE-USE DISCIPLINE: explicit_only (never broad fan-out) + 1h cache + WEEKLY digest
# only (NOT the 6h content-watchtower — tweet volume is noisy) + health watchdog probes
# just ONE handle / 6h. Handles = 45 workflow-curated high-signal accounts in twitter_x.json.
# EMERGENCY RE-SEAL: set _SEALED = True (every entry point inert, zero X network).
# ──────────────────────────────────────────────────────────────────────────────
_SEALED = False
_SEALED_MSG = "twitter_x adapter SEALED (operator, 2026-06-03) — no X access performed"


# Single-flight guard for twscrape's SQLite-backed account DB. When a call TIMES OUT we
# abandon its runner thread, but that thread keeps its DB connection open until its op
# unwinds. Starting a second concurrent runner then races the same accounts.db →
# "database is locked". So we admit ONE runner at a time; a call arriving while one is in
# flight degrades (RuntimeError → caller returns []/None) instead of corrupting access.
# A stale claim auto-expires after _INFLIGHT_MAX so a presumed-dead runner can't disable X
# forever; a generation token stops a late-finishing stale runner from clearing a newer claim.
_inflight_lock = threading.Lock()
_inflight_since = 0.0   # 0 = idle; else monotonic start time of the live runner
_inflight_token = 0     # bumped each claim — runner only clears if it still owns the token
_INFLIGHT_MAX = 300.0   # force-reclaim a runner still "in flight" past this (presumed dead)


def _run_async(coro, timeout: float | None = None):
    """Run a coroutine in a dedicated thread + fresh event loop (isolates twscrape's
    asyncio from FastMCP's running loop). With ``timeout`` set, abandon the (daemon)
    thread and raise ``TimeoutError`` if it doesn't finish in time — so a hung twscrape
    call can't block the caller. Critical for health_check, which holds ``self._lock``:
    an unbounded probe there would deadlock every real search behind it.

    Single-flight: raises ``RuntimeError`` if a prior (possibly abandoned) runner is still
    live, rather than racing its SQLite connection."""
    global _inflight_since, _inflight_token
    box: dict = {}

    with _inflight_lock:
        now = time.monotonic()
        if _inflight_since and (now - _inflight_since) < _INFLIGHT_MAX:
            raise RuntimeError("twscrape busy — a prior op is still in flight (single-flight guard)")
        _inflight_token += 1
        my_token = _inflight_token
        _inflight_since = now  # claim (also force-reclaims a presumed-dead stale runner)

    def runner():
        global _inflight_since
        loop = asyncio.new_event_loop()
        try:
            box["v"] = loop.run_until_complete(coro)
        except Exception as exc:  # noqa: BLE001 — surface to caller thread
            box["e"] = exc
        finally:
            loop.close()
            with _inflight_lock:
                if _inflight_token == my_token:  # don't clear a newer claim that reclaimed us
                    _inflight_since = 0.0

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        # Leave the claim SET — the abandoned runner clears it when it unwinds; until then
        # new calls degrade rather than race the locked DB.
        raise TimeoutError(f"twscrape op exceeded {timeout}s")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _config() -> dict:
    try:
        return json.loads(CRED.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _handles() -> list[str]:
    hs = [h.lstrip("@").strip() for h in (_config().get("handles") or []) if h.strip()]
    return hs or _DEFAULT_HANDLES


def _api():
    from twscrape import API
    try:
        from twscrape.logger import set_log_level
        set_log_level("ERROR")  # twscrape is chatty on INFO; keep MCP stderr clean
    except Exception:  # noqa: BLE001
        pass
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return API(str(DB_PATH))


async def _ensure_account(api) -> bool:
    """Ensure an ACTIVE burner account exists. Adds it (auth_token + self-gen ct0)
    if missing/inactive. Returns False if the auth_token is missing or rejected."""
    info = await api.pool.accounts_info()
    if any(a.get("active") for a in info):
        return True
    tok = _config().get("auth_token")
    if not tok:
        return False
    try:
        await api.pool.delete_inactive()
    except Exception:  # noqa: BLE001
        pass
    ct0 = secrets.token_hex(16)
    await api.pool.add_account(
        "polaris_x_burner", "x", "x@local", "x",
        cookies=f"auth_token={tok}; ct0={ct0}",
    )
    info = await api.pool.accounts_info()
    return any(a.get("active") for a in info)


def _tweet_media(tw) -> list[str]:
    """Image URLs (photos + video/gif thumbnails) a vision-capable agent can view.

    All getattr-guarded against twscrape schema drift — a wrong/absent attr just
    yields no media, never an error. Full media payload is also in metadata['raw'].
    """
    out: list[str] = []
    m = getattr(tw, "media", None)
    if m is None:
        return out
    for p in (getattr(m, "photos", None) or []):
        u = getattr(p, "url", None)
        if u:
            out.append(u)
    for v in (getattr(m, "videos", None) or []):
        u = getattr(v, "thumbnailUrl", None)
        if u:
            out.append(u)
    for g in (getattr(m, "animated", None) or []):
        u = getattr(g, "thumbnailUrl", None)
        if u:
            out.append(u)
    return out


class TwitterXAdapter:
    name = "twitter_x"
    needs_credentials = True
    explicit_only = "low-frequency burner (account-rate-sensitive)"
    description = (
        "X/Twitter 顶级 ML 研究者 + 加拿大/SG AI 信号 (twscrape 直连 burner cookie, "
        "低频). 起步 curated 名单, 可配 ~/.polaris/credentials/twitter_x.json handles"
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()  # serialize twscrape (one sqlite db)

    # ------------------------------------------------------------ async cores
    async def _fetch_all(self, handles: list[str], per: int) -> list[PolarisDocument]:
        api = _api()
        if not await _ensure_account(api):
            logger.warning("twitter_x: no active burner account (auth_token missing/rejected)")
            return []
        id_cache = _load_id_cache()  # handle→id: skip the rate-limited resolve when cached
        dirty = False
        out: list[PolarisDocument] = []
        for h in handles:
            try:
                uid = id_cache.get(h.lower())
                if uid is None:  # only the FIRST fetch per handle hits UserByScreenName
                    u = await api.user_by_login(h)
                    if not u:
                        continue
                    uid = u.id
                    id_cache[h.lower()] = uid
                    dirty = True
                async for tw in api.user_tweets(uid, limit=per):
                    if getattr(tw, "retweetedTweet", None):
                        continue  # drop retweets (noise)
                    out.append(self._tweet_to_doc(tw, h))
            except Exception as exc:  # noqa: BLE001 — one handle must not kill the rest
                logger.warning("twitter_x handle %s failed: %s", h, exc)
            await asyncio.sleep(random.uniform(*_JITTER))
        if dirty:
            _save_id_cache(id_cache)
        return out

    async def _one_tweet(self, tid: int) -> Optional[object]:
        api = _api()
        if not await _ensure_account(api):
            return None
        return await api.tweet_details(tid)

    async def _probe(self) -> bool:
        api = _api()
        if not await _ensure_account(api):
            return False
        # Real probe: resolve one handle so we actually detect a dead/flagged cookie
        # (the local account row stays "active" even after X-side expiry).
        return await api.user_by_login(_handles()[0]) is not None

    # -------------------------------------------------------------- Protocol
    def search(self, query: str, limit: int = 10) -> list[PolarisDocument]:
        if _SEALED:
            logger.warning(_SEALED_MSG)
            return []
        handles = _handles()
        key = cache.make_key("twitter_x", "timeline", ",".join(handles), PER_HANDLE_TWEETS)
        docs = cache.get_docs(key)
        if docs is None:
            with self._lock:
                docs = cache.get_docs(key)  # re-check inside lock
                if docs is None:
                    # BOUNDED: a hung twscrape (rate-limit / network stall) must never
                    # block the caller indefinitely — time out and degrade to [].
                    try:
                        docs = _run_async(self._fetch_all(handles, PER_HANDLE_TWEETS),
                                          timeout=max(60, len(handles) * 5))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("twitter_x fetch timed out / failed: %s", exc)
                        docs = []
                    if docs:
                        cache.set_docs(key, docs, ttl=CACHE_TTL)
        docs = docs or []
        q = (query or "").strip()
        if q:
            return keyword_score_filter(docs, q)[:limit]
        return sorted(docs, key=lambda d: d.date or _EPOCH, reverse=True)[:limit]

    def fetch_url(self, url: str) -> Optional[PolarisDocument]:
        if _SEALED:
            logger.warning(_SEALED_MSG)
            return None
        host = (urlparse(url).hostname or "").lower()
        if not any(h in host for h in ("x.com", "twitter.com")):
            return None
        parts = urlparse(url).path.strip("/").split("/")
        if len(parts) >= 3 and parts[1] == "status":
            try:
                tid = int(parts[2])
            except ValueError:
                return None
            try:
                with self._lock:
                    tw = _run_async(self._one_tweet(tid), timeout=30)
            except Exception as exc:  # noqa: BLE001 — bounded: never hang eye_add_url
                logger.warning("twitter_x fetch_url timed out / failed for %s: %s", url, exc)
                return None
            if tw is None:
                return None
            uname = getattr(getattr(tw, "user", None), "username", "") or "i"
            return self._tweet_to_doc(tw, uname)
        return None

    def health_check(self) -> tuple[bool, str]:
        if _SEALED:
            return False, "SEALED (operator, 2026-06-03) — disabled until a fresh burner auth_token is provisioned"
        if not _config().get("auth_token"):
            return False, "no auth_token in twitter_x.json"
        try:
            with self._lock:
                ok = _run_async(self._probe(), timeout=10)
        except TimeoutError:
            return False, "probe timed out (twscrape queue/network stalled)"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        return (True, "OK (twscrape; burner active)") if ok else \
               (False, "burner inactive — auth_token likely expired/flagged")

    # ----------------------------------------------------------- normalize
    def _tweet_to_doc(self, tw, handle: str) -> PolarisDocument:
        content = getattr(tw, "rawContent", "") or ""
        user = getattr(tw, "user", None)
        uname = getattr(user, "username", None) or handle
        title = content.strip().replace("\n", " ")[:80] or "(tweet)"
        tags = [f"@{uname}"] + [f"#{h}" for h in (getattr(tw, "hashtags", None) or [])[:5]]
        return PolarisDocument(
            source="twitter_x",
            source_id=str(getattr(tw, "id", "")),
            url=getattr(tw, "url", "") or f"https://x.com/{uname}",
            title=title,
            content=content,
            author=f"@{uname}",
            date=getattr(tw, "date", None),
            signals=mk_signal("likes", getattr(tw, "likeCount", None),
                              kind="engagement", by="twitter_x/likeCount"),
            tags=tags,
            media=_tweet_media(tw),
            metadata={
                "handle": uname,
                "likes": getattr(tw, "likeCount", None),
                "retweets": getattr(tw, "retweetCount", None),
                "replies": getattr(tw, "replyCount", None),
                "views": getattr(tw, "viewCount", None),
                "lang": getattr(tw, "lang", None),
                "raw": jsonsafe(tw),
            },
        )


register_adapter(TwitterXAdapter())
