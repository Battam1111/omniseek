"""crossref_retractions — the newest RETRACTION NOTICES as structured records (MONITOR + STRUCTURE).

Crossref exposes every registered retraction notice as a queryable JSON list
(``filter=update-type:retraction``). This is a longitudinal research-integrity stream the open web
forgets: each item carries the notice's own DOI, the RETRACTED paper's DOI (update-to[0]), journal,
publisher, retraction date, and the original authors. It complements ``penumbra_paper_enrich`` (which
checks ONE paper's retraction/integrity) by giving the live FIREHOSE of recent retractions, filterable
by topic via ``query=``.

Razor (STRUCTURE + MONITOR): web search returns prose ABOUT a famous retraction; this returns the
machine-readable per-notice record (notice DOI -> retracted DOI -> date), newest-first, so an agent
can monitor the stream or pull the integrity record for a topic. Verified 2026-06-22: status ok,
total-results 72654, newest item retracted same day.

Caveat (in the description so the agent routes right): the raw stream skews heavily biomedical, so
AI/NLP signal is sparse — treat it as a filterable firehose (pass ``query=``), not a pre-curated
NLP feed. explicit_only: named via penumbra_search 单源钻取 (a topic-filtered integrity probe), not the broad sweep.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

from penumbra.core import auth, http
from penumbra.core.normalize import Document
from penumbra.core.sources.api._base import BaseAPIAdapter

# Crossref's "polite pool" courtesy contact (NOT a credential / not auth): a working mailto gets the
# eye into Crossref's faster, monitored request pool. The address is the DEPLOYER's, host-injected via
# auth.contact_email() (~/.penumbra/credentials/contact.json or PENUMBRA_CONTACT_EMAIL), never in-tree.
_ENDPOINT = "https://api.crossref.org/works"


class CrossrefRetractionsAdapter(BaseAPIAdapter):
    name = "crossref_retractions"
    description = (
        "Crossref 撤稿通知流 (filter=update-type:retraction) — 最新撤稿的结构化记录: 撤稿通知 DOI + "
        "被撤论文 DOI (update-to) + 期刊/出版商/撤稿日期/原作者. MONITOR 研究诚信 + STRUCTURE (网搜只给"
        "撤稿的散文报道, 这里给逐条机读记录, 最新在前). query= 可按主题过滤 (如 'language model'). "
        "注意整体偏生物医学, AI/NLP 信号稀疏 — 当可过滤的 firehose 用, 非预筛 NLP 榜. 命名钻取 (penumbra_search 单源 raw). "
        "补 penumbra_paper_enrich (查单篇论文撤稿/诚信) 的逆向: 给最近撤稿的流."
    )
    explicit_only = "Crossref 撤稿 MONITOR firehose (偏生物医学); 命名钻取 (penumbra_search 单源 raw) 按主题查最近撤稿"
    cache_ttl = 21600  # 6h: a retraction stream moves slowly
    rank_locally = False  # crossref sorts by created(desc) [no query] or relevance [query]; preserve it
    url_host = "crossref.org"
    health_probe_url = "https://api.crossref.org/works?filter=update-type:retraction&rows=1"
    kind = "stream"
    domains = ["papers", "methodology"]
    regions = ["global"]
    modes = ["STRUCTURE", "MONITOR"]

    def _raw_fetch(self, query: str, limit: int) -> list:
        params = {
            "filter": "update-type:retraction",
            "rows": max(1, min(limit, 50)),
            "sort": "created", "order": "desc",
            "mailto": auth.contact_email(),
        }
        if (query or "").strip():
            params["query"] = query.strip()  # server-side full-text filter within the retraction set
        url = f"{_ENDPOINT}?{urlencode(params)}"
        data = http.get_json(url, headers={"User-Agent": f"PenumbraEye/1.0 (mailto:{auth.contact_email()})"})
        if not isinstance(data, dict):
            return []
        return ((data.get("message") or {}).get("items")) or []

    def _to_document(self, raw) -> Optional[Document]:
        if not isinstance(raw, dict):
            return None
        doi = (raw.get("DOI") or "").strip()
        titles = raw.get("title") or []
        real_title = titles[0].strip() if titles and isinstance(titles[0], str) else ""
        if not (doi or real_title):
            return None  # nothing to key or show on
        title = real_title or "(retraction notice)"
        upd = raw.get("update-to") or []
        retracted_doi = ""
        if upd and isinstance(upd[0], dict):
            retracted_doi = (upd[0].get("DOI") or "").strip()
        journal = ""
        ct = raw.get("container-title") or []
        if ct and isinstance(ct[0], str):
            journal = ct[0].strip()
        publisher = raw.get("publisher")
        date = None
        created = (raw.get("created") or {}).get("date-time")
        if isinstance(created, str):
            try:
                date = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except Exception:  # noqa: BLE001
                date = None
        authors = raw.get("author") or []
        names = [f"{a.get('given', '')} {a.get('family', '')}".strip()
                 for a in authors[:6] if isinstance(a, dict)]
        author = ", ".join(n for n in names if n) or (publisher or None)
        url = (raw.get("URL") or (f"https://doi.org/{doi}" if doi else "")).strip()
        content = (f"撤稿通知. 被撤论文 DOI: {retracted_doi or 'n/a'}. "
                   f"期刊: {journal or 'n/a'}. 出版商: {publisher or 'n/a'}.")
        return Document(
            source=self.name,
            source_id=doi or url or title,
            url=url,
            title=title,
            content=content,
            author=author,
            date=date,
            tags=["retraction", "integrity"],
            metadata={"notice_doi": doi, "retracted_paper_doi": retracted_doi,
                      "journal": journal, "publisher": publisher, "type": raw.get("type")},
        )
