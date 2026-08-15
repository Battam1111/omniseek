"""S2 snippet search — passage-level full-text retrieval (Semantic Scholar /graph/v1/snippet/search).

The eye's semantic_scholar / openalex adapters search papers by TITLE + ABSTRACT; this one retrieves
the exact PASSAGES (sentences / sections) across S2's open-access full-text corpus that match a query
— "which papers say which sentences about X", not just which papers are about X. That passage layer is
the STRUCTURE web search cannot cleanly return. explicit_only (named drill): a passage search is a
targeted lit-review move, not something every broad omniseek_search should pay S2 for. English CS/AI corpus
(S2 open-access full text). Reuses the shared _s2 guard (pace / semaphore / breaker) via
``_s2.snippet_search`` — this adapter never touches S2 directly.

Each snippet -> one Document (source_id = corpusId#kind#offset, so multiple passages of one
paper stay distinct; rank's cross-source title-fingerprint may still fold them into also_in on a broad
merge). The url is the S2 paper page so ``omniseek_paper_enrich`` / ``omniseek_read`` can drill the full paper.
"""

from __future__ import annotations

import logging
from typing import Optional

from omniseek.core import _s2
from omniseek.core.normalize import Document, mk_signal

logger = logging.getLogger(__name__)


class S2SnippetAdapter:
    name = "s2_snippet"
    needs_credentials = False  # S2 key optional (shared pool without); the shared _s2 guard applies either way
    kind = "lookup"
    domains = ["papers"]
    modes = ["STRUCTURE"]
    description = (
        "S2 段落级全文检索 (/graph/v1/snippet/search) — 跨 Semantic Scholar 开放获取全文语料, 检索匹配查询的"
        "具体段落/句子 (不止论文/摘要级). 回答 '哪些论文里的哪些句子在讲 X'. 补 semantic_scholar/openalex "
        "(论文级) 与 omniseek_paper_enrich (单篇全文) 的中间层: passage 级. 英文 CS/AI 语料. 命名钻取 (omniseek_search 单源 raw)."
    )
    explicit_only = (
        "S2 passage 级全文检索; 命名钻取 (omniseek_search 单源 raw); 段落检索是定向文献综述动作, 不进广扇出 "
        "(每 broad query 不付 S2 一次调用)"
    )
    cache_ttl = 21600  # 6h: the OA full-text corpus moves slowly

    def _to_doc(self, hit: dict) -> Optional[Document]:
        snip = hit.get("snippet") or {}
        paper = hit.get("paper") or {}
        text = (snip.get("text") or "").strip()
        corpus = str(paper.get("corpusId") or "")
        if not text or not corpus:
            return None  # a snippet with no body or no paper anchor is not a document
        kind = snip.get("snippetKind") or "body"
        offset = (snip.get("snippetOffset") or {}).get("start", 0)
        authors = paper.get("authors") or []
        author_names = ", ".join(
            (a.get("name") if isinstance(a, dict) else str(a)) for a in authors[:6] if a
        ) or None
        # signals is a dict[str, Signal]; the default snippet/search fields carry NO citationCount, so
        # this is usually {} (never fabricated) and only populates when a citation count is present.
        cc = paper.get("citationCount")
        signals = (mk_signal("citations", cc, kind="citation", by="semantic_scholar/citationCount")
                   if cc is not None else {})
        return Document(
            source="s2_snippet",
            source_id=f"{corpus}#{kind}#{offset}",
            url=f"https://www.semanticscholar.org/paper/{corpus}",
            title=(paper.get("title") or "(untitled)").strip()[:300],
            content=text,
            author=author_names,
            date=paper.get("publicationDate") or None,
            signals=signals,
            tags=["s2-snippet", "passage", kind],
            metadata={"corpus_id": corpus, "section": snip.get("section"),
                      "snippet_kind": kind, "score": hit.get("score")},
        )

    def search(self, query: str, limit: int = 20) -> list[Document]:
        hits = _s2.snippet_search(query or "", limit=limit)
        docs = [d for d in (self._to_doc(h) for h in hits if isinstance(h, dict)) if d]
        return docs[:limit]

    def fetch_url(self, url: str) -> Optional[Document]:
        # Search-only (passage retrieval): an S2 paper URL is semantic_scholar / omniseek_paper_enrich's
        # job, not a snippet source's. Claim nothing so omniseek_read routes it to the right adapter.
        return None

    def health_check(self) -> tuple[bool, str]:
        # Delegate to the ONE shared, single-flighted S2 probe (60s cache): the all-source health
        # sweep must NOT fire a second live S2 call per S2-backed adapter (that bursts the key into a
        # 429 storm). Mirrors semantic_scholar_source / s2_authors_source.
        return _s2.health()


from omniseek.core.fetcher import register_adapter

register_adapter(S2SnippetAdapter())
