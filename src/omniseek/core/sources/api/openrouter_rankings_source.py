"""OpenRouter usage rankings: which models the market actually runs (by tokens).

Artificial Analysis (llm_leaderboard) tells us how good a model scores; this tells
us how much a model is actually USED. OpenRouter routes a large share of the
independent-app LLM traffic, and its /rankings page publishes a per-day usage
series per model+variant (prompt + completion tokens, request count). We aggregate
that week-long series into a usage leaderboard: the models the market is voting for
with real inference spend, not benchmark numbers.

MODE: MONITOR (source_id = model_permaslug, so a newly climbing model surfaces as a
new watchtower item) + STRUCTURE (queryable current usage ranking). models domain.

Access: the public frontend endpoint openrouter.ai/api/frontend/v1/rankings/models
(no key, no cookie: verified 2026-07-10). NOT the /api/v1/datasets/* path, which
401s with "No cookie auth credentials found" (that one is account-scoped). Usage
data is OpenRouter's; attribution kept in metadata.
"""

from __future__ import annotations

import functools
import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import anyio
import httpx

from omniseek.core import cache, diag, http
from omniseek.core.normalize import Document, mk_signal

logger = logging.getLogger(__name__)

# view=week => a ~6-day per-day series per model+variant. The default is week; we
# pin it so the window is stable across the deployment.
API = "https://openrouter.ai/api/frontend/v1/rankings/models?view=week"
TIMEOUT = 30
CACHE_TTL = 21600  # 6h: the ranking is a slow weekly aggregate, not a live tick
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def _fmt_tokens(n: float) -> str:
    """Human token count: 5205100000000 -> '5.21T', 938700000000 -> '939B'."""
    if n >= 1e12:
        return f"{n / 1e12:.2f}T"
    if n >= 1e9:
        return f"{n / 1e9:.0f}B"
    if n >= 1e6:
        return f"{n / 1e6:.0f}M"
    return str(int(n))


class OpenRouterRankingsAdapter:
    name = "openrouter_rankings"
    needs_credentials = False
    kind = "lookup"
    domains = ["models"]
    regions = ["global"]
    modes = ["MONITOR", "STRUCTURE"]
    explicit_only = False
    description = (
        "OpenRouter 用量榜: 市场真金白银在跑哪些模型 (近一周 prompt+completion token 聚合排名). "
        "补 llm_leaderboard 的另一面: 那个是'评测有多强', 这个是'实际用得多狠'. "
        "空 query=按用量 token 从高到低; 关键词过滤厂商/模型 ('anthropic' / 'deepseek' / 'gemini'). "
        "新模型冲上用量榜=watchtower 新条目. 用量数据来自 OpenRouter"
    )

    def _rankings(self) -> list[dict]:
        key = cache.make_key("openrouter_rankings", "models", "week")
        cached = cache.get(key)
        if cached is not None:
            return cached
        try:
            resp = httpx.get(API, headers={"User-Agent": UA}, timeout=TIMEOUT,
                             follow_redirects=True)
            resp.raise_for_status()
            rows = resp.json().get("data", [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("openrouter_rankings: fetch failed: %s", exc)
            st = getattr(getattr(exc, "response", None), "status_code", None)
            diag.note("openrouter_rankings.fetch", url=API, status=st, exc=exc)
            return []

        # Aggregate the per-day, per-variant series into one row per model.
        agg: dict[str, dict] = {}
        for r in rows:
            slug = r.get("model_permaslug")
            if not slug:
                continue
            e = agg.get(slug)
            if e is None:
                e = agg[slug] = {"slug": slug, "prompt": 0, "completion": 0,
                                 "requests": 0, "variants": set(), "latest": None}
            e["prompt"] += r.get("total_prompt_tokens") or 0
            e["completion"] += r.get("total_completion_tokens") or 0
            e["requests"] += r.get("count") or 0
            v = r.get("variant")
            if v:
                e["variants"].add(v)
            d = r.get("date")
            if d and (e["latest"] is None or d > e["latest"]):
                e["latest"] = d

        ranked = sorted(agg.values(),
                        key=lambda e: e["prompt"] + e["completion"], reverse=True)
        slim = []
        for i, e in enumerate(ranked, 1):
            slim.append({
                "rank": i,
                "slug": e["slug"],
                "prompt_tokens": e["prompt"],
                "completion_tokens": e["completion"],
                "total_tokens": e["prompt"] + e["completion"],
                "requests": e["requests"],
                "variants": sorted(e["variants"]),
                "latest": e["latest"],
            })
        cache.set(key, slim, ttl=CACHE_TTL)
        return slim

    async def _arankings(self) -> list[dict]:
        """Native-async twin of ``_rankings`` (S4b): the SAME cache key + a byte-identical per-day
        aggregation, changing ONLY the egress path so the async fan-out awaits this without holding a
        pool thread. Off-loop discipline:
          - the disk cache read + write hop OFF the loop (``anyio.to_thread.run_sync``; cache.get/set do file IO);
          - the raw ``httpx.get(API, ...).json()`` becomes ``await http.aget_json`` (the shared pooled async
            client + SSRF guard + 30MB cap; the network wait stays ON the loop via epoll, no held thread).
            The source's browser UA is preserved (this is a browser frontend endpoint), same TIMEOUT, and the
            shared client already follows redirects (the sync ``follow_redirects=True``);
          - the aggregate / sort / slim map is PURE CPU and stays ON the loop, identical to ``_rankings``.
        A fetch failure (None / non-dict body) returns [] WITHOUT caching, mirroring the sync except branch
        (which returns [] before ``cache.set``); a reached-but-empty series still caches like the sync path."""
        key = cache.make_key("openrouter_rankings", "models", "week")
        cached = await anyio.to_thread.run_sync(cache.get, key)  # disk read OFF loop
        if cached is not None:
            return cached
        data = await http.aget_json(API, headers={"User-Agent": UA}, timeout=TIMEOUT)
        if not isinstance(data, dict):
            logger.warning("openrouter_rankings: fetch failed (async): non-dict/None response")
            diag.note("openrouter_rankings.fetch", url=API, status=None,
                      body="async fetch failed / non-dict response")
            return []
        rows = data.get("data", [])

        # Aggregate the per-day, per-variant series into one row per model.
        agg: dict[str, dict] = {}
        for r in rows:
            slug = r.get("model_permaslug")
            if not slug:
                continue
            e = agg.get(slug)
            if e is None:
                e = agg[slug] = {"slug": slug, "prompt": 0, "completion": 0,
                                 "requests": 0, "variants": set(), "latest": None}
            e["prompt"] += r.get("total_prompt_tokens") or 0
            e["completion"] += r.get("total_completion_tokens") or 0
            e["requests"] += r.get("count") or 0
            v = r.get("variant")
            if v:
                e["variants"].add(v)
            d = r.get("date")
            if d and (e["latest"] is None or d > e["latest"]):
                e["latest"] = d

        ranked = sorted(agg.values(),
                        key=lambda e: e["prompt"] + e["completion"], reverse=True)
        slim = []
        for i, e in enumerate(ranked, 1):
            slim.append({
                "rank": i,
                "slug": e["slug"],
                "prompt_tokens": e["prompt"],
                "completion_tokens": e["completion"],
                "total_tokens": e["prompt"] + e["completion"],
                "requests": e["requests"],
                "variants": sorted(e["variants"]),
                "latest": e["latest"],
            })
        await anyio.to_thread.run_sync(  # disk write OFF loop
            functools.partial(cache.set, key, slim, ttl=CACHE_TTL))
        return slim

    def search(self, query: str, limit: int = 10) -> list[Document]:
        ranked = self._rankings()
        if not ranked:
            return []
        terms = [t for t in (query or "").lower().split() if t]
        if terms:
            ranked = [e for e in ranked if all(t in e["slug"].lower() for t in terms)]
        return [self._to_doc(e) for e in ranked[:limit]]

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` (S4b): mirrors it line-for-line, awaiting ``_arankings``
        (async cache + egress) instead of the sync ``_rankings``. The term filter + ``_to_doc`` map are
        PURE CPU and stay ON the loop, byte-identical to ``search`` so the two can never drift. Exposing
        ``asearch`` makes this adapter AsyncSearchCapable, routing it to the fetcher's native async branch."""
        ranked = await self._arankings()
        if not ranked:
            return []
        terms = [t for t in (query or "").lower().split() if t]
        if terms:
            ranked = [e for e in ranked if all(t in e["slug"].lower() for t in terms)]
        return [self._to_doc(e) for e in ranked[:limit]]

    def fetch_url(self, url: str) -> Optional[Document]:
        host = (urlparse(url).hostname or "").lower()
        if "openrouter.ai" not in host:
            return None
        slug = urlparse(url).path.strip("/")
        for e in self._rankings():
            if e["slug"] == slug:
                return self._to_doc(e)
        return None

    def health_check(self) -> tuple[bool, str]:
        n = len(self._rankings())
        if n:
            return True, f"OK ({n} models ranked)"
        return False, "0 models (endpoint down / shape changed)"

    @staticmethod
    def _to_doc(e: dict) -> Document:
        tot = _fmt_tokens(e["total_tokens"])
        title = f"#{e['rank']} {e['slug']} · {tot} tokens/week"

        lines = [
            f"Rank #{e['rank']} by OpenRouter usage (past week)",
            f"Total tokens: {tot}  ·  prompt {_fmt_tokens(e['prompt_tokens'])}  ·  "
            f"completion {_fmt_tokens(e['completion_tokens'])}",
            f"Requests: {e['requests']:,}  ·  variants: {', '.join(e['variants']) or '?'}",
            "Usage data by OpenRouter (openrouter.ai/rankings)",
        ]

        date = None
        if e.get("latest"):
            try:
                date = datetime.fromisoformat(e["latest"]).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass
        author = e["slug"].split("/")[0] if "/" in e["slug"] else None
        signals = mk_signal("usage_tokens", e["total_tokens"], kind="engagement",
                            by="openrouter_rankings/tokens_week", unit="tokens/week")
        signals.update(mk_signal("requests", e["requests"], kind="engagement",
                                 by="openrouter_rankings/requests_week", unit="requests/week"))
        return Document(
            source="openrouter_rankings",
            source_id=e["slug"],
            url=f"https://openrouter.ai/{e['slug']}",
            title=title,
            content="\n".join(lines),
            author=author,
            date=date,
            signals=signals,
            tags=["leaderboard", "usage", "models"],
            metadata={"rank": e["rank"], "slug": e["slug"],
                      "total_tokens": e["total_tokens"],
                      "prompt_tokens": e["prompt_tokens"],
                      "completion_tokens": e["completion_tokens"],
                      "requests": e["requests"], "variants": e["variants"],
                      "window": "week", "latest_date": e.get("latest"),
                      "attribution": "OpenRouter (openrouter.ai/rankings)"},
        )


from omniseek.core.fetcher import register_adapter

register_adapter(OpenRouterRankingsAdapter())
