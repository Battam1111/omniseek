"""ukri_gtr - UK Gateway to Research (GtR): awarded UKRI research grants (STRUCTURE).

Gateway to Research (https://gtr.ukri.org) is UKRI's public window onto every grant the UK
research councils fund (EPSRC / BBSRC / ESRC / MRC / AHRC / NERC / STFC / Innovate UK / Research
England). Its keyless JSON API returns the project entity graph: title, abstract, lead funder,
grant category, research subjects/topics, participating organisations + award amounts. Penumbra's
FIRST UK funding source (it had US NSF/NIH/grants_gov and Canadian NSERC/SSHRC/CIHR, but no UK):
for a PhD eyeing a UK postdoc/faculty track, knowing which UK labs hold EPSRC/BBSRC grants in
NLP/ML - amounts, institutions, funders, abstracts - is direct career-targeting intel web search
cannot return as structured records. The UK sibling of nserc_awards / nih_reporter.

Razor (STRUCTURE): the per-project record (title, abstract, funder, subjects, org, award amount)
beats web search's prose. Telos: a researcher mapping the fundable UK ML landscape = where the
funded work + hireable labs are.

Access via the public GtR API (no auth, no key):
  GET https://gtr.ukri.org/gtr/api/projects?q=<query>&s=<size>&p=1
      Accept: application/vnd.rcuk.gtr.json-v7   (REQUIRED - without it the API serves XML)
  -> {"totalSize", "page", "size", "project": [
        {id, href, identifiers.identifier[]{value(grant ref), type},
         title, status, grantCategory, leadFunder, leadOrganisationDepartment,
         abstractText, techAbstractText, potentialImpact,
         researchSubjects.researchSubject[]{text}, researchTopics.researchTopic[]{text},
         start, end (epoch ms, often null in the search projection),
         participantValues.participant[]{organisationName, role, projectCost, grantOffer}}, ...]}
The API REJECTS a page size below 10 ("Page size cannot be less than 10"), so _raw_fetch always
requests >=10 and _to_documents trims to the caller's limit.

The human project page is https://gtr.ukri.org/projects?ref=<grantReference> (ref = the identifier
value, e.g. 'BB/R008736/1' or '2403950'); the API href is the JSON endpoint, not a browsable page,
so the doc URL is built from the ref. PI names / lead-org details / fund lines live behind per-entity
links (LEAD_ORG / PI_PER / FUND hrefs) - resolving them would be N secondary fetches per search, so
this stays a SINGLE-GET adapter and extracts only what the project record itself carries (the lead
organisation + award amount are present in participantValues when the projection includes them).

rank stays default-False: the GtR ?q= endpoint returns its own relevance order. explicit_only: a
named funding drill (point it at a UK ML/NLP topic), like the other funding sources, not broad
fan-out fodder. Field paths verified live 2026-07-10 against the projects endpoint (totalSize 23156
for q='machine learning').
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from urllib.parse import quote

from penumbra.core import http
from penumbra.core.normalize import Document, jsonsafe
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

API_URL = "https://gtr.ukri.org/gtr/api/projects"
ACCEPT_JSON = "application/vnd.rcuk.gtr.json-v7"  # without this header the API returns XML
PROJECT_URL = "https://gtr.ukri.org/projects?ref={ref}"
_MIN_SIZE = 10   # the API rejects a page size below 10
_MAX_SIZE = 100
_CONTENT_CAP = 6000  # cap the inlined abstract(s) so one verbose project cannot dominate a payload


class UKRIGtRAdapter(BaseScrapeAdapter):
    name = "ukri_gtr"
    needs_credentials = False
    description = (
        "英国 UKRI Gateway to Research (GtR) - 英国研究理事会已批科研经费 (眼首个英国经费源, 此前有美国 "
        "NSF/NIH/grants_gov 与加拿大 NSERC/SSHRC/CIHR). 覆盖 EPSRC/BBSRC/ESRC/MRC/AHRC/NERC/STFC/Innovate "
        "UK. 逐笔项目: 标题 + 摘要 + 资助方 (leadFunder) + grant category + 研究学科/主题 + 参与机构与金额 (GBP). "
        "博士赴英找实验室/资助方向/机构的一手结构 (网搜给不出). Keyless JSON API. STRUCTURE, 命名钻取 (penumbra_search 单源 raw)."
    )
    cache_ttl = 21600  # 6h: an awarded-grant index changes slowly
    kind = "lookup"
    domains = ["funding"]
    regions = ["uk"]
    modes = ["STRUCTURE"]
    url_host = "gtr.ukri.org"
    explicit_only = ("ukri_gtr: a named UK research-council funding drill (by topic / funder), "
                     "not broad-fan-out fodder")

    # -- hooks --
    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        size = max(_MIN_SIZE, min(int(limit), _MAX_SIZE))  # API rejects size < 10
        return http.get_json(
            API_URL,
            headers={"Accept": ACCEPT_JSON},
            params={"q": query or "", "s": size, "p": 1},
            timeout=20,
        )

    async def _araw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """Async twin of _raw_fetch: byte-faithful mirror (same URL, headers, params, timeout);
        ONLY the shared-http egress swaps to its async twin (http.get_json -> await http.aget_json)."""
        size = max(_MIN_SIZE, min(int(limit), _MAX_SIZE))  # API rejects size < 10
        return await http.aget_json(
            API_URL,
            headers={"Accept": ACCEPT_JSON},
            params={"q": query or "", "s": size, "p": 1},
            timeout=20,
        )

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of search -> AsyncSearchCapable. Shares the base async cache round-trip;
        egress via _araw_fetch; mapping via the SAME pure-CPU _to_documents (byte-identical to search)."""
        return await self._asearch_via(
            query, limit,
            afetch=lambda: self._araw_fetch(query, limit),
            abuild=lambda raw: self._to_documents(raw, query, limit))

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, dict):
            return []
        projects = raw.get("project") or []
        docs: list[Document] = []
        for proj in projects[:limit]:
            doc = self._project_to_doc(proj)
            if doc is not None:
                docs.append(doc)
        return docs

    # -- doc builder (pure: no network, golden-fixture friendly) --
    def _project_to_doc(self, proj: Any) -> Optional[Document]:
        if not isinstance(proj, dict):
            return None
        title = (proj.get("title") or "").strip()
        if not title:
            return None  # an untitled project is unusable in a result list
        ref = self._grant_ref(proj)
        guid = (proj.get("id") or "").strip()
        source_id = ref or guid
        if not source_id:
            return None  # no stable identity -> drop

        # Human project page from the grant reference; fall back to the API href.
        if ref:
            url = PROJECT_URL.format(ref=quote(ref, safe=""))
        else:
            url = (proj.get("href") or "").replace("http://", "https://", 1)
            if not url:
                return None

        funder = (proj.get("leadFunder") or "").strip()
        category = (proj.get("grantCategory") or "").strip()
        status = (proj.get("status") or "").strip()
        subjects = self._texts(proj.get("researchSubjects"), "researchSubject")
        topics = self._texts(proj.get("researchTopics"), "researchTopic")
        lead_org = self._lead_org(proj)
        cost, offer = self._amounts(proj)

        return Document(
            source=self.name,
            source_id=source_id,
            url=url,
            title=title,
            content=self._content(proj, title, funder, category, subjects, lead_org),
            author=lead_org or funder or None,
            date=self._parse_ms(proj.get("start")) or self._parse_ms(proj.get("end")),
            signals={},
            tags=self._tags(funder, category, status, subjects, topics),
            metadata={
                "grant_ref": ref,
                "project_id": guid or None,
                "lead_funder": funder or None,
                "grant_category": category or None,
                "status": status or None,
                "lead_organisation": lead_org,
                "lead_org_department": (proj.get("leadOrganisationDepartment") or None),
                "research_subjects": subjects,
                "research_topics": topics,
                "project_cost_gbp": cost,
                "grant_offer_gbp": offer,
                "start": proj.get("start"),
                "end": proj.get("end"),
                "raw": jsonsafe(proj),
            },
        )

    # -- helpers --
    @staticmethod
    def _grant_ref(proj: dict) -> str:
        """The grant reference (identifiers.identifier[0].value, e.g. 'BB/R008736/1' or '2403950').
        Empty string when absent (the doc then falls back to the GUID id)."""
        ids = (proj.get("identifiers") or {}).get("identifier") or []
        for item in ids:
            if isinstance(item, dict):
                val = (item.get("value") or "").strip()
                if val:
                    return val
        return ""

    @staticmethod
    def _texts(block: Any, key: str) -> list[str]:
        """Pull the '.text' of each entry in a {<key>: [{text, ...}]} block, dropping empties
        and the GtR placeholder 'Unclassified'. Returns [] when the block is absent/misshaped."""
        if not isinstance(block, dict):
            return []
        items = block.get(key)
        if not isinstance(items, list):
            return []
        out: list[str] = []
        for it in items:
            if isinstance(it, dict):
                t = (it.get("text") or "").strip()
                if t and t.lower() != "unclassified" and t not in out:
                    out.append(t)
        return out

    @staticmethod
    def _lead_org(proj: dict) -> Optional[str]:
        """Lead organisation name from participantValues (role LEAD_PARTICIPANT), else the first
        participant's org. None when the search projection carries no participants."""
        parts = (proj.get("participantValues") or {}).get("participant") or []
        if not isinstance(parts, list):
            return None
        first = None
        for p in parts:
            if not isinstance(p, dict):
                continue
            name = (p.get("organisationName") or "").strip()
            if not name:
                continue
            if p.get("role") == "LEAD_PARTICIPANT":
                return name
            if first is None:
                first = name
        return first

    @staticmethod
    def _amounts(proj: dict) -> tuple[Optional[float], Optional[float]]:
        """(total projectCost, total grantOffer) in GBP summed across participants, or (None, None)
        when the projection carries no participant values."""
        parts = (proj.get("participantValues") or {}).get("participant") or []
        if not isinstance(parts, list) or not parts:
            return None, None
        cost = 0.0
        offer = 0.0
        seen = False
        for p in parts:
            if not isinstance(p, dict):
                continue
            c = p.get("projectCost")
            o = p.get("grantOffer")
            if isinstance(c, (int, float)):
                cost += float(c)
                seen = True
            if isinstance(o, (int, float)):
                offer += float(o)
                seen = True
        if not seen:
            return None, None
        return (cost or None), (offer or None)

    def _content(self, proj: dict, title: str, funder: str, category: str,
                 subjects: list[str], lead_org: str | None) -> str:
        """Human-readable body: a funding header line + the abstract(s), capped. Falls back to the
        header alone (then the title) when the project has no abstract text."""
        head_bits = ["UKRI GtR 项目."]
        if funder:
            head_bits.append(f"资助方: {funder}.")
        if category:
            head_bits.append(f"类别: {category}.")
        if lead_org:
            head_bits.append(f"牡头机构: {lead_org}.")
        if subjects:
            head_bits.append(f"学科: {', '.join(subjects)}.")
        head = " ".join(head_bits)
        abstract = " ".join(t for t in (
            (proj.get("abstractText") or "").strip(),
            (proj.get("techAbstractText") or "").strip(),
        ) if t).strip()
        body = f"{head} {abstract}".strip() if abstract else head
        if body == head and not abstract:
            body = f"{head} {title}".strip()
        return body[:_CONTENT_CAP]

    @staticmethod
    def _tags(funder: str, category: str, status: str,
              subjects: list[str], topics: list[str]) -> list[str]:
        tags = ["funding", "uk"]
        for t in [funder, category, status, *subjects, *topics]:
            t = (t or "").strip()
            if t and t not in tags:
                tags.append(t)
        return tags

    @staticmethod
    def _parse_ms(raw: Any) -> Optional[datetime]:
        """GtR start/end are epoch MILLISECONDS (e.g. 1359676800000) or null. None on anything
        unparseable; never raises."""
        if not isinstance(raw, (int, float)):
            return None
        try:
            return datetime.utcfromtimestamp(raw / 1000.0)
        except (ValueError, OverflowError, OSError):
            return None

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
