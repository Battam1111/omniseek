"""Fellowships — overseas PhD / postdoc funding (international-open awards).

Phase 4 P14 (2026-05-29). The funding/fellowship dimension was a total blind
spot. opus sub-agent (40+ calls) mapped the landscape, verifying which awards
are **open to international / HK applicants** (many are citizens-only → excluded)
and which are feed-trackable.

Two layers:
1. **RSS bundle** (Batch A — feeds verified healthy on Mac mini): institutional
   blogs that announce calls. Keyword-filtered to funding posts so we don't drown
   in general research news. Vector / CIFAR / IVADO / AI Singapore / Schmidt are
   dedicated; Google Research / Apple ML / NVIDIA blogs are where the big
   industry PhD Fellowships are announced (also in `frontier_labs`, but here we
   surface only the funding-relevant subset).
2. **Static reference links** (Batch C): awards with NO trackable feed (rolling /
   nomination-based / SPA portals). Listed with international-openness + window so
   the deployer can check timing manually. (Batch B — HTML-scraping NSERC/SINGA/NRF
   deadlines — is a future refinement; deadlines are annually stable.)

Key corrections from the recon (don't re-add these):
- **Banting & Vanier are DISCONTINUED** — replaced by NSERC CGRS-Doctoral / CPRA.
- **A*STAR AIF / CDF** are citizens/PR-only or internal → NOT open to a HK PhD.
- Mitacs Globalink = undergrad only; Marie Curie = Europe-bound. Excluded.
"""

from __future__ import annotations

import re

from penumbra.core.normalize import Document, jsonsafe, keyword_score_filter
from penumbra.core.sources.scrape._rss import RSSAdapterBase

# Funding-relevance keyword gate (applied to RSS entries).
_KW = re.compile(
    r"fellow|scholar|phd|ph\.d|postdoc|post-doc|grant|award|call for|nomination|funding|stipend",
    re.IGNORECASE,
)

# Batch C — awards with no trackable feed. (name, url, note: international-openness + window)
LINKS: list[tuple[str, str, str]] = [
    # 工业 AI PhD fellowships（与研究者方向最契合，多数全球开放）
    ("Microsoft Research PhD Fellowship", "https://www.microsoft.com/en-us/research/academic-program/microsoft-research-fellowship/", "国际·明列 HK · 截~12月 · 学费+$17K"),
    ("Meta PhD Research Fellowship", "https://metaresearchphdfellowship.smapply.io/", "全球·无国籍限 · 8月开/9月截"),
    ("Qualcomm Innovation Fellowship", "https://www.qualcomm.com/research/university-relations/innovation-fellowship", "有 APAC 区 · 截~4月 · $40K"),
    # 新加坡轴（落地通道：在读→博后→独立 PI）
    ("NRF Fellowship (Singapore)", "https://www.nrf.gov.sg/grants/nrf-fellowship", "任何国籍·PhD 后≤7y · 独立 PI 落地 SG"),
    ("NUS Presidential Postdoctoral Fellowship", "https://www.nus.edu.sg/careers/nus-programmes/", "全球·PhD±1y · 全年滚动"),
    ("A*STAR SINGA (Singapore PhD)", "https://www.a-star.edu.sg/Scholarships/for-graduate-studies/singapore-international-graduate-award-singa", "国际博士 · 截~12/1"),
    ("Lee Kuan Yew Postdoctoral Fellowship (NTU)", "https://www.ntu.edu.sg/research/research-careers", "Eng/Sci/Med · 截~1月"),
    # 加拿大轴
    ("NSERC CGS-D / CGRS-Doctoral (Canada)", "https://www.nserc-crsng.canada.ca/Students-Etudiants/PG-CS/CGSD-BESCD_eng.asp", "须在加就读 · 截~10/17 (替代已停办的 Vanier)"),
    ("Canada Postdoctoral Research Award (CPRA)", "https://www.nserc-crsng.canada.ca/", "国际≤20%名额·须在加 (替代已停办的 Banting)"),
    # 全球可携带 AI / AI-safety（给独立/早期研究者，多数滚动）
    ("Cooperative AI PhD Fellowship", "https://www.cooperativeai.com/phd-fellowship", "无国籍/地点限 · $40K/yr×3 · 截~11/16"),
    ("Open Philanthropy — early-career / AI safety", "https://www.openphilanthropy.org/how-to-apply-for-funding/", "任何国家 · 滚动"),
    ("EA Long-Term Future Fund (LTFF)", "https://funds.effectivealtruism.org/funds/far-future", "个人/PhD · 滚动 · 中位~$25K"),
    ("Branco Weiss Fellowship (ETH Zürich)", "https://brancoweissfellowship.org/", "所有国籍 · PhD≤5y · 截~1/15"),
    ("Foresight AI-Safety / AI-for-Science grants", "https://foresight.org/grants/grants-ai-for-science-safety/", "每月末滚动 · $10–100K"),
    ("Manifund (AI-safety regranting)", "https://manifund.org/about/open-call", "公开提案 · $5–50K"),
]


class FellowshipsAdapter(RSSAdapterBase):
    name = "fellowships"
    description = (
        "海外 PhD/postdoc 资助与 Fellowship — Vector/CIFAR/IVADO(加) + AISG/NRF/"
        "SINGA(新) + Google/Apple/NVIDIA/Meta/MS PhD Fellowship + Schmidt/OpenPhil/"
        "LTFF(全球 AI). 国际开放 + HK→SG/加拿大路径; RSS 自动 + 静态链接人工核窗口"
    )
    cache_ttl = 21600  # 6h
    feeds = [
        # 专用 fellowship/机构 feed（高信噪比）
        "https://vectorinstitute.ai/feed/",
        "https://cifar.ca/feed/",
        "https://ivado.ca/en/feed/",
        "https://aisingapore.org/feed/",
        "https://schmidtsciencefellows.org/feed/",
        # 工业实验室 blog（PhD Fellowship 在此公告；关键词过滤后仅留资助相关）
        "https://research.google/blog/rss/",
        "https://machinelearning.apple.com/rss.xml",
        "https://blogs.nvidia.com/feed/",
    ]

    def _static_docs(self) -> list[Document]:
        return [
            Document(
                source=self.name,
                source_id=url,
                url=url,
                title=f"[资助] {name}",
                content=f"{name} — {note}",
                tags=["fellowship", "manual-track"],
                metadata={
                    "manual_track": True,
                    "note": note,
                    "raw": jsonsafe({"name": name, "url": url, "note": note}),
                },
            )
            for name, url, note in LINKS
        ]

    def search(self, query: str, limit: int = 10) -> list[Document]:
        # RSS entries, keyword-gated to funding-relevant posts.
        rss = [
            d for d in super().search(query, max(limit * 4, 40))
            if _KW.search((d.title or "") + " " + (d.content or ""))
        ]
        static = self._static_docs()
        if not query:
            # No query: most-recent funding posts first, then the reference shelf.
            return (rss + static)[:limit]
        # Rank the curated award shelf and the live RSS announcements SEPARATELY,
        # then interleave (curated first). Otherwise terse fellowship entries get
        # buried under verbose institutional news on raw keyword counts.
        sr = keyword_score_filter(static, query)
        rr = keyword_score_filter(rss, query)
        out: list[Document] = []
        si = ri = 0
        while len(out) < limit and (si < len(sr) or ri < len(rr)):
            if si < len(sr):
                out.append(sr[si])
                si += 1
            if ri < len(rr) and len(out) < limit:
                out.append(rr[ri])
                ri += 1
        return out[:limit]


from penumbra.core.fetcher import register_adapter

register_adapter(FellowshipsAdapter())
