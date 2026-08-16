"""中文学术 — Chinese-LANGUAGE scholarship via OpenAlex's language:zh facet.

OmniSeek's paper sources (arxiv / semantic_scholar / crossref / dblp / core / openalex) are
English/Western-centric; none routes the Chinese-LANGUAGE corpus. OpenAlex actually holds
~5M language:zh works (~3.4M with abstracts), ~30k Chinese dissertations (type:dissertation),
and ~1.3k CN-domiciled journals — but the plain ``openalex`` source never pins language:zh, so
the agent never reaches them (a grep of OmniSeek showed zero language:zh routing). This is a thin
ROUTING facet over the EXISTING OpenAlex wrap (no new API, keyless, same circuit breaker): it
pins ``language:zh`` and re-labels the docs, giving OmniSeek structured Chinese 题录 + abstracts +
OA links that Google can't assemble.

It is the NO-LOGIN STRUCTURE answer to the Chinese-scholarship gap (verified by OmniSeek-nologin
discovery wf: 百度学术 = captcha-blocked, CNKI/万方/维普 = login-walled, AMiner = unstable — OpenAlex
is the one clean no-login Chinese-scholarship index). The deep full-text corpora (CNKI/万方 PDFs,
学位论文 full text) stay account-walled and are NOT reached here — this gives 题录-level structure.

explicit_only: pinning language:zh into the BROAD fan-out would add Chinese-paper noise to every
(often English) query and spend the shared OpenAlex search budget; the router still surfaces it as
excluded_relevant for Chinese / papers queries, where the agent names it.

Recon trail: brain note eye-recon-openalex_cn.
"""

from __future__ import annotations

from omniseek.core.normalize import Document
from omniseek.core.sources.api.openalex_source import OpenAlexAdapter


def _pin_zh(query: str) -> str:
    """Append language:zh unless the caller set an explicit language filter (so an agent can
    still override with e.g. language:en). Pure string fn → golden-fixture testable offline."""
    q = (query or "").strip()
    return q if "language:" in q.lower() else (q + " language:zh").strip()


class OpenAlexCNAdapter(OpenAlexAdapter):
    name = "openalex_cn"
    needs_credentials = False
    explicit_only = "中文学术 facet over OpenAlex (pins language:zh); name it for Chinese-scholarship search"
    description = (
        "中文学术 — Chinese-LANGUAGE scholarship via OpenAlex (language:zh): 中文期刊论文 + 学位论文 "
        "(add inline `type:dissertation`) that OmniSeek's English paper sources (arxiv/s2/crossref) "
        "miss. Returns structured 题录 — title / authors / 中文期刊 venue / year / citations / OA / "
        "abstract. Optional inline filters: `type:dissertation`, `institutions.country_code:cn`, "
        "`publication_year:2024`. No login (keyless OpenAlex)."
    )
    # routing facets (class attrs — the router reads these)
    kind = "lookup"
    domains = ["papers"]
    regions = ["cn"]
    modes = ["STRUCTURE"]

    def search(self, query: str, limit: int = 10) -> list[Document]:
        docs = super().search(_pin_zh(query), limit)
        # re-stamp source so omniseek_search results + recall index label them as this facet, not 'openalex'
        return [d.model_copy(update={"source": self.name}) for d in docs]

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        # Native-async twin of THIS source's OVERRIDDEN search — it MUST be defined here, never inherited.
        # openalex_cn subclasses OpenAlexAdapter and overrides search (pins language:zh via _pin_zh, then
        # re-stamps the source). If it did NOT define its own asearch it would inherit OpenAlexAdapter's,
        # which reproduces the PARENT search (no zh-pin, source='openalex') → the live async omniseek_search
        # path would SILENTLY drop this facet's customization (the RSS-fellowships override hazard). So
        # mirror ITS search line-for-line: delegate to super().asearch — the parent's byte-faithful async
        # egress (same cache key as super().search + await oa.aget_json), exactly as the sync search above
        # delegates to super().search — on the zh-pinned query, then re-stamp. No per-item fan-out and no
        # cache round-trip live here (the parent owns both), so nothing else moves off the loop; the
        # model_copy re-stamp is pure CPU and stays on the loop, byte-identical to search.
        docs = await super().asearch(_pin_zh(query), limit)
        return [d.model_copy(update={"source": self.name}) for d in docs]


from omniseek.core.fetcher import register_adapter  # noqa: E402

register_adapter(OpenAlexCNAdapter())
