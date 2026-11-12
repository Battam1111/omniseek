"""Exa neural/semantic web search (exa.ai).

Finds open-web pages by MEANING, not just keyword overlap, closing the gap where keyword
search drowns the semantically-right page under popular-but-off-target hits. Each result
carries Exa "highlights" (the relevant excerpts) + a short text snippet; omniseek_read the
result URL (via cdp_fulltext / pdf / ordinary fetch) for the full page.

Complements ordinary keyword web search + the curated sources: reach for Exa on CONCEPTUAL
queries ("blogs on the lived experience of X", "essays arguing Y"). explicit_only: every
call spends an Exa API credit, so it stays OUT of the broad fan-out (name it / omniseek_search 单源钻取).
"""

from __future__ import annotations

import logging
import re
from typing import Optional


from omniseek.core import _probe, auth, cache, http
from omniseek.core.normalize import Document
from omniseek import where
from omniseek.core.sources._base import SourceBase

logger = logging.getLogger(__name__)

_API = "https://api.exa.ai/search"
_TTL = 3600

# Exa bills a credit per /search call and exposes NO zero-credit liveness endpoint (the only
# API surface in this client is the billed search), so the health probe MUST be a real search.
# Cache + single-flight it: before this, health_check fired a fresh billed search on every
# 6-hourly health sweep AND every agent omniseek_health_check, spending ~4+ credits/day purely on
# liveness. _ahealth() caches the (ok, msg) verdict for _HEALTH_TTL_S and single-flights the probe
# (``_probe.shared_verdict``, as _openalex.ahealth) so repeated probes within the TTL collapse
# to ONE billed call, and a concurrent probe storm causes exactly one upstream search.
_HEALTH_TTL_S = 600.0  # 10 min: longer than the OpenAlex probe TTL because this probe COSTS a credit
_health_cache: dict = {"at": 0.0, "result": None}


async def _ahealth(key: str, timeout: int = 20) -> tuple[Optional[bool], str]:
    """ONE shared, TTL-cached Exa liveness probe (single-flight, ``_probe.shared_verdict``).

    Exa has no free liveness endpoint, so this spends ONE credit per _HEALTH_TTL_S window
    instead of one per sweep/agent call. The probe is single-flighted so 40 concurrent
    callers cause exactly ONE billed search; the verdict is cached for the window."""
    return await _probe.shared_verdict(_health_cache, _HEALTH_TTL_S, lambda: _aprobe(key, timeout))


async def _aprobe(key: str, timeout: int) -> tuple[Optional[bool], str]:
    """The probe behind ``_ahealth``."""
    with _probe.watching() as led:
        data = await http.apost_json(_API, json={"query": "test", "numResults": 1},
                                     headers={"x-api-key": key}, timeout=timeout)
    if isinstance(data, dict) and "results" in data:
        return True, "OK (Exa API)"
    # Cached for the window, so re-read HERE: a 429 (or a refused gate) is not verified.
    return _probe.reread(False, "Exa API: no/invalid response (key valid?)", led)


class ExaAdapter(SourceBase):
    name = "exa"
    needs_credentials = True
    kind = "proxy"
    explicit_only = "Exa neural/semantic web search (spends an API credit per call)"
    description = (
        "Exa neural/semantic web search (exa.ai): finds open-web pages by MEANING, not "
        "keywords. Use for conceptual queries where keyword search misses the right page; "
        "each result carries relevant-excerpt highlights. omniseek_read the URL for the full "
        "page. Complements ordinary web search + the curated sources. Add site:domain to a query "
        "to scope to that domain AND return its full page TEXT, a full-text route for IP-blocked / "
        "anti-datacenter sites Exa's crawler can reach but our direct fetch cannot (e.g. HardwareZone)."
    )

    @staticmethod
    def _key() -> Optional[str]:
        return (auth.load("exa") or {}).get("api_key")

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async replacement of the deleted sync ``search`` (11-04, this is the only one now) (mirrors it line-for-line): the fan-out awaits this
        DIRECTLY so Exa's billed POST costs a COROUTINE, not a held pool thread. Three changes only:
          - the disk CACHE read + write go OFF the loop (anyio.to_thread.run_sync: get_docs/set_docs
            do file IO), with the SAME cache key as search so async + sync share one cached bucket;
          - the NETWORK egress swaps to its async twin (await http.apost_json, epoll not a thread);
          - the PURE-CPU result map stays ON the loop, byte-identical to search.
        Exa's search has NO egress semaphore/lock (``_health_lock`` guards only the separate liveness
        probe), so there is nothing to acquire off-loop here."""
        q = (query or "").strip()
        if not q:
            return []
        key = self._key()
        if not key:
            logger.warning(f"exa: no api_key configured ({where.show(where.credentials())}/exa.json)")
            return []
        # Same bucket + cache key as search (Exa bills per QUERY): async and sync reuse ONE search.
        bucket = min(25, max(limit, 10))
        ck = cache.make_key("exa", "search", q, bucket)

        async def _aget() -> Optional[list[Document]]:
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
            data = await http.apost_json(_API, json=body, headers={"x-api-key": key}, timeout=30)  # async network
            if not isinstance(data, dict):
                return None  # a failure: not cached

            docs: list[Document] = []
            for r in (data.get("results") or []):
                url = r.get("url")
                if not url:
                    continue
                highlights = [h.strip() for h in (r.get("highlights") or []) if h and h.strip()]
                text = (r.get("text") or "").strip()
                content = "\n\n".join(highlights) or text
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
            return docs

        return (await self._acached(ck, lambda docs: _TTL if docs else 600, _aget))[:limit]

    async def afetch_url(self, url: str) -> Optional[Document]:
        return None  # search-only; use cdp_fulltext / pdf / ordinary fetch for the full page

    async def ahealth_check(self) -> tuple[Optional[bool], str]:
        key = self._key()
        if not key:
            return False, f"no api_key ({where.show(where.credentials())}/exa.json)"
        # Delegate to the shared TTL-cached single-flight probe so the 6-hourly sweep + every
        # agent omniseek_health_check collapse to ONE billed search per window, not one credit each.
        return await _ahealth(key)


from omniseek.core.fetcher import register_adapter

register_adapter(ExaAdapter())
