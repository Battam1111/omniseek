"""LLM leaderboard — Artificial Analysis API (the field's live pulse, structured).

One endpoint carries what the scattered leaderboards could not give us cleanly
(verified 2026-06-10: LiveBench site is a JS shell, SWE-bench data needs
repo assembly): 500+ models with the AA Intelligence Index and the six AA
Capability Indexes, per-1M-token pricing and median speed/TTFT, each with
creator + release date.
MODE: STRUCTURE (queryable current scores) + MONITOR (source_id = model slug,
so every newly listed model surfaces as a new item in the watchtower).

2026-11-06: Artificial Analysis retired the legacy /api/v2/data/* endpoints on
2026-11-04 23:59:59 GMT (they answer 410 Gone; migration guide
https://artificialanalysis.ai/data-api/migrate-v2-data). The source now reads the
Free replacement /api/v2/language/models/free (same key, same x-api-key header):
it is paginated (``page``, ``pagination.has_more``), the speed/TTFT fields moved
under ``performance``, and the Free shape no longer carries the per-benchmark
scores (GPQA / AIME-25 / HLE / LiveCodeBench / MMLU-Pro / IFBench, Pro tier only)
nor the Math Index; the Coding / Agentic indexes are always null (no longer
published). Field reference: https://artificialanalysis.ai/api/v2/openapi
(schema FreeModelData).

Data by Artificial Analysis (https://artificialanalysis.ai), free API key,
attribution required. Key: <credentials>/artificial_analysis.json
{"api_key": "..."}. explicit_only: the free tier is rate-limited (100 requests
per 24h window), so this is a named lookup (the full list is cached; local
filtering costs no API calls).

2026-11-07 (LLM-LB-PROBE): a failed read of the list is no longer an empty list. ``_amodels`` raises
``ModelsUnavailable`` with what the API answered (HTTP status and body, e.g. the 410 Gone of the
retired endpoint, which read for two days as "0 models (key invalid / API down)"), so a search
reports the source as errored with that reason and the health check reports it. The health check
always reads the list from the API (``_amodels(fresh=True)``) and rewrites the cache: the recall
ingest kept the cache warm at every health round (ingest at :06 of 01/07/13/19h, health at :45 of
03/09/15/21h, TTL 6h), so the check never asked the API and a recovered source kept its failure
streak. One read is the 4 pages of the list; the health round's read refills the cache the next
ingest then reads, so the daily request count stays where it was. A 429 is not verified (None) with
the time the quota resets (``X-RateLimit-Reset`` / ``Retry-After``).
"""

from __future__ import annotations

import datetime
import functools
import logging
import time
from typing import Optional
from urllib.parse import urlparse

import anyio

from omniseek.core import _probe, auth, cache, diag, http, portal, upstreams
from omniseek.core.normalize import Document, to_utc
from omniseek import where
from omniseek.core.sources._base import SourceBase

logger = logging.getLogger(__name__)

API = "https://artificialanalysis.ai/api/v2/language/models/free"
TIMEOUT = 30
CACHE_TTL = 21600  # 6h: scores move slowly; the free tier is rate-limited

auth.register_template(
    "artificial_analysis",
    {"api_key": "aa_...", "_help": "free key from https://artificialanalysis.ai/api"},
)

# The Free shape's ``evaluations`` (FreeEvaluations in the OpenAPI spec), minus the Coding and
# Agentic indexes, which the API documents as always null.
_EVAL_KEYS = ("artificial_analysis_intelligence_index",
              "artificial_analysis_finance_and_accounting_index",
              "artificial_analysis_strategy_and_ops_index", "artificial_analysis_legal_index",
              "artificial_analysis_healthcare_and_medical_index",
              "artificial_analysis_engineering_index", "artificial_analysis_economics_index")
# Cache key of the slim list: "free" since the 11-06 move to the Free endpoint (its slim rows carry
# other eval keys than the retired v2/data rows did).
_CACHE_PARTS = ("llm_leaderboard", "models", "free")
_UPSTREAM = "artificial_analysis"  # its entry in upstreams.json (the rate-limit readings live there)


class ModelsUnavailable(RuntimeError):
    """The model list could not be read. The message is what the API answered (``HTTP <status>:
    <body>``) or why nothing came back; ``status`` is the HTTP status when there was an answer."""

    def __init__(self, reason: str, status: Optional[int] = None) -> None:
        super().__init__(reason)
        self.status = status


def _failure(records: list) -> ModelsUnavailable:
    """The ``ModelsUnavailable`` for one failed page read, from the failure records the shared http
    helper noted (``diag.tap``): the last answer's status and body, else the note's own text (a
    declared gate that sent nothing says so in its body), else the exception."""
    recs = [r for r in records if isinstance(r, dict)]
    last = recs[-1] if recs else {}
    if last.get("status") is None and not last.get("exc") and last.get("body"):
        return ModelsUnavailable(str(last["body"]))
    return ModelsUnavailable(
        diag.failure_reason(recs, fallback="the API answered no JSON object and no failure was recorded"),
        last.get("status"))


def _quota_back_at() -> str:
    """When the API's quota comes back, from its last answer's ``X-RateLimit-Reset`` (Unix time) or
    ``Retry-After`` (seconds), as local time; "" when neither was seen."""
    lanes = (upstreams.readings(_UPSTREAM).get("lanes") or {}).values()
    for lane in lanes:
        fields = lane.get("fields") or {}
        at: Optional[float] = None
        try:
            at = float(fields.get("x-ratelimit-reset") or "")
        except ValueError:
            ra = upstreams.parse_retry_after(fields.get("retry-after"))
            if ra:
                at = float(lane.get("at") or time.time()) + ra
        if at:
            return datetime.datetime.fromtimestamp(at).astimezone().isoformat(timespec="minutes")
    return ""


class LLMLeaderboardAdapter(SourceBase):
    name = "llm_leaderboard"
    needs_credentials = True
    kind = "lookup"
    domains = ["eval", "models"]
    explicit_only = "Artificial Analysis API (free key, rate-limited); named lookup + watchtower"
    description = (
        "LLM 榜单：Artificial Analysis 全模型实时评测 (500+ 模型: AA 智能指数 + 六个能力指数 "
        "(金融会计/战略运营/法律/医疗/工程/经济), $/1M tokens, tok/s, 发布日期). "
        "领域脉搏的结构化层: 空 query=按智能指数排序; 关键词过滤模型/厂商 "
        "('claude' / 'deepseek' / 'openai'). 新模型上榜=watchtower 新条目. "
        "Data by Artificial Analysis (attribution required)"
    )

    async def _amodels(self, *, fresh: bool = False) -> list[dict]:
        """The slim model list: from the disk cache (read and written off the loop) while it is warm,
        else every page of the Free endpoint through the shared async egress (``http.aget_json``),
        cached for ``CACHE_TTL``. ``fresh`` skips the cache read (the health check: it must ask the
        API) and still writes the list it got.

        A failed read raises ``ModelsUnavailable`` carrying what the API answered (status and body,
        from the failure records the shared helper notes) and caches nothing: the caller sees why,
        not an empty list that reads like "no models", and a transient miss is not pinned for the
        TTL. A list missing pages would sort and filter as if it were whole, so one failed page
        fails the read. In cache-only mode a cold cache is an empty list (nothing may be sent)."""
        key = cache.make_key(*_CACHE_PARTS)
        if not fresh:
            cached = await cache.aget_once(key)  # disk read OFF loop
            if cached is not None:
                return cached
        if cache.cache_only():
            return []
        creds = auth.load("artificial_analysis") or {}
        api_key = creds.get("api_key")
        if not api_key:
            raise ModelsUnavailable(
                f"API key not configured ({where.show(where.credentials())}/artificial_analysis.json)")
        raw: list = []
        page = 1
        while True:  # the Free endpoint is paginated: walk every page the API says there is
            with diag.tap() as seen:
                data = await http.aget_json(API, headers={"x-api-key": api_key}, params={"page": page},
                                            timeout=TIMEOUT)  # async network
            if not isinstance(data, dict):
                raise _failure(seen)
            raw.extend(data.get("data") or [])
            pg = data.get("pagination") or {}
            if not pg.get("has_more") or page >= (pg.get("total_pages") or page):
                break
            page += 1
        slim = []
        for m in raw:
            ev = m.get("evaluations") or {}
            pr = m.get("pricing") or {}
            perf = m.get("performance") or {}
            slim.append({
                "name": m.get("name"),
                "slug": m.get("slug"),
                "creator": ((m.get("model_creator") or {}).get("name")) or "",
                "release_date": m.get("release_date"),
                "tps": perf.get("median_output_tokens_per_second"),
                "ttft": perf.get("median_time_to_first_token_seconds"),
                "price_in": pr.get("price_1m_input_tokens"),
                "price_out": pr.get("price_1m_output_tokens"),
                "evals": {k: ev.get(k) for k in _EVAL_KEYS if ev.get(k) is not None},
            })
        await anyio.to_thread.run_sync(  # disk write OFF loop
            functools.partial(cache.set, key, slim, ttl=CACHE_TTL), limiter=portal.IO_LANE)
        return slim

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async replacement of the deleted sync ``search`` (11-04, S9; this is the only one now) (S4b): the fan-out awaits this DIRECTLY (no pool thread),
        so this source's dominant NETWORK wait costs a coroutine, not a held thread. Mirrors ``search``
        line-for-line: only the cached fetch swaps to the async ``_amodels`` (async egress + off-loop
        cache); the term filter / intelligence-index sort / ``_to_doc`` map below is PURE CPU and stays
        ON the loop, byte-identical to ``search``."""
        models = await self._amodels()
        if not models:
            return []
        terms = [t for t in (query or "").lower().split() if t]
        if terms:
            models = [m for m in models
                      if all(t in f"{m['name']} {m['creator']} {m['slug']}".lower()
                             for t in terms)]
        models.sort(key=lambda m: m["evals"].get("artificial_analysis_intelligence_index")
                    or -1, reverse=True)
        return [self._to_doc(m) for m in models[:limit]]

    async def afetch_url(self, url: str) -> Optional[Document]:
        host = (urlparse(url).hostname or "").lower()
        if "artificialanalysis.ai" not in host:
            return None
        slug = urlparse(url).path.rstrip("/").split("/")[-1]
        for m in await self._amodels():
            if m["slug"] == slug:
                return self._to_doc(m)
        return None

    async def ahealth_check(self) -> tuple[Optional[bool], str]:
        if not auth.is_configured("artificial_analysis"):
            return False, f"API key not configured ({where.show(where.credentials())}/artificial_analysis.json)"
        # Always asks the API (fresh): a check answered from the cache verifies nothing, and the cache
        # is warm at every health round (module docstring, 2026-11-07).
        try:
            n = len(await self._amodels(fresh=True))
        except ModelsUnavailable as exc:
            if exc.status == 429 or _probe.says_rate_limited(str(exc)):
                back = _quota_back_at()
                return None, (_probe.rate_limited("artificialanalysis.ai", status=exc.status or 429)
                              + (f"; the quota resets at {back}" if back else "") + f"; the API said: {exc}")
            return False, str(exc)
        if n:
            return True, f"OK ({n} models)"
        return False, "the API answered every page with no models"

    @staticmethod
    def _to_doc(m: dict) -> Document:
        ev = m["evals"]
        ii = ev.get("artificial_analysis_intelligence_index")
        bits = []
        if ii is not None:
            bits.append(f"AA智能 {ii:.0f}" if isinstance(ii, (int, float)) else f"AA智能 {ii}")
        if m.get("price_out") is not None:
            bits.append(f"${m['price_out']}/1M out")
        if m.get("tps") is not None:
            bits.append(f"{m['tps']:.0f} tok/s")
        title = f"{m['name']} ({m['creator']})" + (" · " + " · ".join(bits) if bits else "")

        lines = [f"Creator: {m['creator']}  ·  Released: {m.get('release_date') or '?'}"]
        idx = {k.replace("artificial_analysis_", "AA "): v for k, v in ev.items()
               if k.startswith("artificial_analysis")}
        if idx:
            lines.append("Indices: " + "  ".join(f"{k}={v}" for k, v in idx.items()))
        lines.append(f"Pricing: in ${m.get('price_in')}/1M, out ${m.get('price_out')}/1M  ·  "
                     f"Speed: {m.get('tps')} tok/s, TTFT {m.get('ttft')}s")
        lines.append("Data by Artificial Analysis")

        date = to_utc(m.get("release_date"))
        return Document(
            source="llm_leaderboard",
            source_id=m["slug"] or m["name"],
            url=f"https://artificialanalysis.ai/models/{m['slug']}",
            title=title,
            content="\n".join(lines),
            author=m["creator"] or None,
            date=date,
            tags=["leaderboard", "eval"],
            metadata={"slug": m["slug"], "creator": m["creator"],
                      "release_date": m.get("release_date"), "evals": ev,
                      "price_1m_in": m.get("price_in"), "price_1m_out": m.get("price_out"),
                      "tokens_per_second": m.get("tps"), "ttft_seconds": m.get("ttft"),
                      "attribution": "Artificial Analysis (artificialanalysis.ai)"},
        )


from omniseek.core.fetcher import register_adapter

register_adapter(LLMLeaderboardAdapter())
