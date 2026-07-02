"""Exa neural/semantic web search (exa.ai).

Finds open-web pages by MEANING, not just keyword overlap, closing the gap where keyword
search drowns the semantically-right page under popular-but-off-target hits. Each result
carries Exa "highlights" (the relevant excerpts) + a short text snippet; penumbra_add_url the
result URL (via cdp_fulltext / pdf / ordinary fetch) for the full page.

Complements ordinary keyword web search + the curated sources: reach for Exa on CONCEPTUAL
queries ("blogs on the lived experience of X", "essays arguing Y"). explicit_only: every
call spends an Exa API credit, so it stays OUT of the broad fan-out (name it / penumbra_fetch).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional

from penumbra.core import auth, cache, http
from penumbra.core.normalize import Document

logger = logging.getLogger(__name__)

_API = "https://api.exa.ai/search"
_TTL = 3600

# Exa bills a credit per /search call and exposes NO zero-credit liveness endpoint (the only
# API surface in this client is the billed search), so the health probe MUST be a real search.
# Cache + single-flight it: before this, health_check fired a fresh billed search on every
# 6-hourly health sweep AND every agent penumbra_health_check, spending ~4+ credits/day purely on
# liveness. _health() caches the (ok, msg) verdict for _HEALTH_TTL_S and double-checks under a
# module lock (mirrors _openalex.health single-flight) so repeated probes within the TTL collapse
# to ONE billed call, and a concurrent probe storm causes exactly one upstream search.
_HEALTH_TTL_S = 600.0  # 10 min: longer than the OpenAlex probe TTL because this probe COSTS a credit
_health_cache: dict = {"at": 0.0, "result": None}
_health_lock = threading.Lock()


def _health(key: str, timeout: int = 20) -> tuple[bool, str]:
    """ONE shared, TTL-cached Exa liveness probe (single-flight under the module lock).

    Exa has no free liveness endpoint, so this spends ONE credit per _HEALTH_TTL_S window
    instead of one per sweep/agent call. The probe runs under the lock so 40 concurrent
    callers cause exactly ONE billed search; the verdict is cached for the window."""
    now = time.monotonic()
    with _health_lock:
        if _health_cache["result"] is not None and now - _health_cache["at"] < _HEALTH_TTL_S:
            return _health_cache["result"]
        data = http.post_json(_API, json={"query": "test", "numResults": 1},
                              headers={"x-api-key": key}, timeout=timeout)
        if isinstance(data, dict) and "results" in data:
            result = (True, "OK (Exa API)")
        else:
            result = (False, "Exa API: no/invalid response (key valid?)")
        _health_cache["at"] = time.monotonic()
        _health_cache["result"] = result
        return result


class ExaAdapter:
    name = "exa"
    needs_credentials = True
    kind = "proxy"
    explicit_only = "Exa neural/semantic web search (spends an API credit per call)"
    description = (
        "Exa neural/semantic web search (exa.ai): finds open-web pages by MEANING, not "
        "keywords. Use for conceptual queries where keyword search misses the right page; "
        "each result carries relevant-excerpt highlights. penumbra_add_url the URL for the full "
        "page. Complements ordinary web search + the curated sources. Add site:domain to a query "
        "to scope to that domain AND return its full page TEXT, a full-text route for IP-blocked / "
        "anti-datacenter sites Exa's crawler can reach but our direct fetch cannot (e.g. HardwareZone)."
    )

    @staticmethod
    def _key() -> Optional[str]:
        return (auth.load("exa") or {}).get("api_key")

    def search(self, query: str, limit: int = 10) -> list[Document]:
        q = (query or "").strip()
        if not q:
            return []
        key = self._key()
        if not key:
            logger.warning("exa: no api_key configured (~/.penumbra/credentials/exa.json)")
            return []
        # Over-fetch to a stable upper bucket (Exa bills per QUERY, not per result), so the same
        # query at different limits reuses ONE cached search instead of re-billing per limit. Key
        # on the bucket, not the raw limit, then slice to limit from the cached docs below.
        bucket = min(25, max(limit, 10))
        ck = cache.make_key("exa", "search", q, bucket)
        cached = cache.get_docs(ck)
        if cached is not None:
            return cached[:limit]

        # `site:domain` tokens -> Exa includeDomains. This doubles as a full-text route for
        # IP-blocked / anti-datacenter sites (e.g. HardwareZone): Exa's own crawler reaches them
        # and returns the page TEXT, so we get content our direct fetch (datacenter egress) cannot.
        sites = re.findall(r"site:(\S+)", q)
        q_clean = re.sub(r"site:\S+", "", q).strip() or q
        body = {
            "query": q_clean,
            "numResults": bucket,
            "type": "auto",  # let Exa pick neural vs keyword per query
        }
        if sites:
            body["includeDomains"] = sites
            body["contents"] = {"text": {"maxCharacters": 6000}}  # scoped: return real page text
        else:
            body["contents"] = {
                "highlights": {"numSentences": 3, "highlightsPerUrl": 2},
                "text": {"maxCharacters": 800},
            }
        data = http.post_json(_API, json=body, headers={"x-api-key": key}, timeout=30)
        if not isinstance(data, dict):
            return []

        docs: list[Document] = []
        for r in (data.get("results") or []):
            url = r.get("url")
            if not url:
                continue
            highlights = [h.strip() for h in (r.get("highlights") or []) if h and h.strip()]
            text = (r.get("text") or "").strip()
            content = "\n\n".join(highlights) or text or "(no preview; open the URL for full text)"
            docs.append(Document(
                source="exa",
                source_id=r.get("id") or url,
                url=url,
                title=(r.get("title") or "(untitled)")[:300],
                content=content[:6000],
                author=r.get("author") or None,
                metadata={"via": "exa-neural", "published": r.get("publishedDate"),
                          "score": r.get("score")},
            ))
            if len(docs) >= bucket:
                break

        # Cache the full bucket (keyed on the bucket, not limit), then slice to the caller's
        # limit: a later call at a smaller/larger limit (up to the bucket) reuses this search.
        cache.set_docs(ck, docs, ttl=_TTL if docs else 600)
        return docs[:limit]

    def fetch_url(self, url: str) -> Optional[Document]:
        return None  # search-only; use cdp_fulltext / pdf / ordinary fetch for the full page

    def health_check(self) -> tuple[bool, str]:
        key = self._key()
        if not key:
            return False, "no api_key (~/.penumbra/credentials/exa.json)"
        # Delegate to the shared TTL-cached single-flight probe so the 6-hourly sweep + every
        # agent penumbra_health_check collapse to ONE billed search per window, not one credit each.
        return _health(key)


from penumbra.core.fetcher import register_adapter

register_adapter(ExaAdapter())
