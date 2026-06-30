"""LLM leaderboard — Artificial Analysis API (the field's live pulse, structured).

One endpoint carries what the scattered leaderboards could not give us cleanly
(verified 2026-06-10: LiveBench site is a JS shell, SWE-bench data needs
repo assembly): 500+ models with AA intelligence / coding / math indices,
benchmark scores (GPQA / AIME-25 / HLE / LiveCodeBench / MMLU-Pro / IFBench),
per-1M-token pricing and median speed/TTFT, each with creator + release date.
MODE: STRUCTURE (queryable current scores) + MONITOR (source_id = model slug,
so every newly listed model surfaces as a new item in the watchtower).

Data by Artificial Analysis (https://artificialanalysis.ai), free API key,
attribution required. Key: ~/.penumbra/credentials/artificial_analysis.json
{"api_key": "..."}. explicit_only: the free tier is rate-limited, so this is a
named lookup (the full list is cached; local filtering costs no API calls).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

from penumbra.core import auth, cache
from penumbra.core.normalize import Document

logger = logging.getLogger(__name__)

API = "https://artificialanalysis.ai/api/v2/data/llms/models"
TIMEOUT = 30
CACHE_TTL = 21600  # 6h: scores move slowly; the free tier is rate-limited

auth.write_template(
    "artificial_analysis",
    {"api_key": "aa_...", "_help": "free key from https://artificialanalysis.ai/api"},
)

_EVAL_KEYS = ("artificial_analysis_intelligence_index", "artificial_analysis_coding_index",
              "artificial_analysis_math_index", "gpqa", "aime_25", "hle",
              "livecodebench", "mmlu_pro", "ifbench")


class LLMLeaderboardAdapter:
    name = "llm_leaderboard"
    needs_credentials = True
    kind = "lookup"
    domains = ["eval", "models"]
    explicit_only = "Artificial Analysis API (free key, rate-limited); named lookup + watchtower"
    description = (
        "LLM 榜单 — Artificial Analysis 全模型实时评测 (500+ 模型: AA 智能/代码/数学指数, "
        "GPQA/AIME-25/HLE/LiveCodeBench/MMLU-Pro, $/1M tokens, tok/s, 发布日期). "
        "领域脉搏的结构化层: 空 query=按智能指数排序; 关键词过滤模型/厂商 "
        "('claude' / 'deepseek' / 'openai'). 新模型上榜=watchtower 新条目. "
        "Data by Artificial Analysis (attribution required)"
    )

    def _models(self) -> list[dict]:
        key = cache.make_key("llm_leaderboard", "models", "v2")
        cached = cache.get(key)
        if cached is not None:
            return cached
        creds = auth.load("artificial_analysis") or {}
        api_key = creds.get("api_key")
        if not api_key:
            logger.info("llm_leaderboard: API key not configured")
            return []
        try:
            resp = httpx.get(API, headers={"x-api-key": api_key}, timeout=TIMEOUT)
            resp.raise_for_status()
            raw = resp.json().get("data", [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_leaderboard: fetch failed: %s", exc)
            return []
        slim = []
        for m in raw:
            ev = m.get("evaluations") or {}
            pr = m.get("pricing") or {}
            slim.append({
                "name": m.get("name"),
                "slug": m.get("slug"),
                "creator": ((m.get("model_creator") or {}).get("name")) or "",
                "release_date": m.get("release_date"),
                "tps": m.get("median_output_tokens_per_second"),
                "ttft": m.get("median_time_to_first_token_seconds"),
                "price_in": pr.get("price_1m_input_tokens"),
                "price_out": pr.get("price_1m_output_tokens"),
                "evals": {k: ev.get(k) for k in _EVAL_KEYS if ev.get(k) is not None},
            })
        cache.set(key, slim, ttl=CACHE_TTL)
        return slim

    def search(self, query: str, limit: int = 10) -> list[Document]:
        models = self._models()
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

    def fetch_url(self, url: str) -> Optional[Document]:
        host = (urlparse(url).hostname or "").lower()
        if "artificialanalysis.ai" not in host:
            return None
        slug = urlparse(url).path.rstrip("/").split("/")[-1]
        for m in self._models():
            if m["slug"] == slug:
                return self._to_doc(m)
        return None

    def health_check(self) -> tuple[bool, str]:
        if not auth.is_configured("artificial_analysis"):
            return False, "API key not configured (~/.penumbra/credentials/artificial_analysis.json)"
        n = len(self._models())
        if n:
            return True, f"OK ({n} models)"
        return False, "0 models (key invalid / API down)"

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
        bench = {k: v for k, v in ev.items() if not k.startswith("artificial_analysis")}
        if bench:
            lines.append("Benchmarks: " + "  ".join(f"{k}={v}" for k, v in bench.items()))
        lines.append(f"Pricing: in ${m.get('price_in')}/1M, out ${m.get('price_out')}/1M  ·  "
                     f"Speed: {m.get('tps')} tok/s, TTFT {m.get('ttft')}s")
        lines.append("Data by Artificial Analysis")

        date = None
        if m.get("release_date"):
            try:
                date = datetime.fromisoformat(m["release_date"]).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass
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


from penumbra.core.fetcher import register_adapter

register_adapter(LLMLeaderboardAdapter())
