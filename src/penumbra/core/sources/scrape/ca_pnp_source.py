"""Canada Provincial Nominee Program (PNP) draw histories — Ontario / BC / Alberta.

Each province runs its OWN nominee program with its OWN draw cadence + scoring system, published as
HTML tables on a gov page (no feed, no API). These adapters parse those tables into per-draw
documents: date + stream + score cutoff + invitations issued. STRUCTURE + MONITOR — web search
returns prose about "the latest draw"; this returns the longitudinal per-draw record an agent can
monitor or query by stream/score.

Why: the PNPs (esp. their graduate/PhD + tech streams) are a primary Canada PR route alongside
federal Express Entry, and the score-cutoff history is decision-critical. NOT the federal system —
keep these SEPARATE from ``ircc_ee_rounds`` (federal CRS): Ontario uses its own OINP score, BC uses
SIRS (0-200), Alberta its own. explicit_only: named via eye_fetch after routing to the immigration
domain (slower HTML scrapes, not the broad sweep).

Structures verified live 2026-06-22:
  * OINP  — 35 tables, header ['Date issued','Number of invitations issued','Date profiles created',
            'Score range','Notes']; the STREAM is the table's preceding heading (Employer Job Offer
            sub-streams / Masters Graduate / PhD Graduate / Entrepreneur). (Masters/PhD streams were
            revoked 2026-05-30 in the OINP redesign, so those rows are now HISTORICAL; the page keeps
            publishing the redesigned streams, so this stays a live monitor.)
  * BCPNP — 3 tables: Skills Immigration draws (Date/ITA type/Selection factors/Min score/Invitations),
            a registration-pool SIRS distribution (Score range/registrations), Entrepreneur draws
            (Date/Stream/Min Score/Invitations). min_score/counts can be 'N/A' or '<5' strings.
  * AAIP  — the draw table is the one headed 'Draw information' (Draw date / Worker stream… / Minimum
            score of invited candidates / Number of invitations); the page leads with allocation
            summaries, so SELECT BY HEADER, not index.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from bs4 import BeautifulSoup

from penumbra.core.normalize import PolarisDocument
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

# Recent-window cap: a draw-history page can hold hundreds of rows (OINP ~500 across 35 tables); a
# MONITOR source wants the recent window, not the full archive, so we sort newest-first and keep the
# most recent N (still plenty for the BM25 query-filter the base applies on top).
_RECENT_CAP = 60


def _parse_date(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:  # noqa: BLE001
            continue
    return None


def _cells(tr) -> list[str]:
    return [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]


def _header(tbl) -> list[str]:
    rows = tbl.find_all("tr")
    return [c.lower() for c in _cells(rows[0])] if rows else []


def _heading_before(tbl) -> str:
    for el in tbl.find_all_previous(["h2", "h3", "h4"]):
        t = el.get_text(" ", strip=True)
        if t:
            return t
    return ""


def _finish(docs: list[PolarisDocument], limit: int) -> list[PolarisDocument]:
    """Sort newest-first (None dates last), cap to the recent window."""
    docs.sort(key=lambda d: (d.date is not None, d.date or datetime.min), reverse=True)
    return docs[:max(limit, _RECENT_CAP)]


class _CaPnpBase(BaseScrapeAdapter, register=False):
    """Shared knobs for the three PNP draw scrapers (each subclass overrides _to_documents)."""
    needs_credentials = False
    cache_ttl = 21600  # 6h: draws land roughly weekly
    rank = True         # BM25-filter the parsed draws by the agent's query (stream/score/region)
    fetch_html = True   # _raw_fetch returns the page TEXT (see scrape/_base) for our table parse
    kind = "stream"
    domains = ["immigration"]
    regions = ["ca"]
    modes = ["STRUCTURE", "MONITOR"]

    @staticmethod
    def _html(raw) -> str:
        return raw if isinstance(raw, str) else (raw[0] if isinstance(raw, tuple) else str(raw))


class OinpInvitationsAdapter(_CaPnpBase):
    name = "oinp_invitations"
    description = (
        "安省提名 OINP 抽签历史 — Ontario Immigrant Nominee Program 各 stream (雇主担保/硕士毕业生/"
        "博士毕业生/企业家等) 的逐次抽签: 日期 + 邀请数 + 分数线 (Score range) + EOI 窗口 + 备注. "
        "省提名, 用 OINP 自有分数, 别与联邦 EE (ircc_ee_rounds, CRS) 混. 博士/硕士 stream 2026-05-30 "
        "改版后已停, 旧行为历史参考; 页面继续发新 stream 抽签, 故仍是活的 monitor. 命名 eye_fetch."
    )
    explicit_only = "OINP 安省提名抽签历史 (HTML 抓取, 命名 eye_fetch); 省提名非联邦 EE"
    search_url = "https://www.ontario.ca/page/ontario-immigrant-nominee-program-oinp-invitations-apply"
    url_host = "ontario.ca"

    def _to_documents(self, raw, query, limit) -> list[PolarisDocument]:
        soup = BeautifulSoup(self._html(raw) or "", "lxml")
        docs: list[PolarisDocument] = []
        for tbl in soup.find_all("table"):
            hdr = _header(tbl)
            if not (any("date issued" in h for h in hdr) and any("score range" in h for h in hdr)):
                continue
            stream = _heading_before(tbl) or "OINP"
            for tr in tbl.find_all("tr")[1:]:
                c = _cells(tr)
                if len(c) < 5 or not c[0].strip():
                    continue
                date_issued, invitations, window, score, notes = c[:5]
                docs.append(PolarisDocument(
                    source=self.name,
                    source_id=f"oinp:{stream}:{date_issued}:{score}",
                    url=self.search_url,
                    title=f"OINP {stream} · {date_issued}",
                    content=(f"安省提名 OINP 抽签 — {stream}. 发出 {invitations} 份邀请, 分数 {score}, "
                             f"EOI 窗口 {window}. 备注: {notes or '无'}."),
                    date=_parse_date(date_issued),
                    tags=["canada", "immigration", "pnp", "ontario"],
                    metadata={"stream": stream, "draw_date": date_issued, "invitations": invitations,
                              "score_range": score, "profile_window": window, "notes": notes},
                ))
        return _finish(docs, limit)


class BcpnpInvitationsAdapter(_CaPnpBase):
    name = "bcpnp_invitations"
    description = (
        "BC 省提名 BCPNP 抽签 — Skills Immigration (技术移民, 按 ITA type + 分数线 SIRS + 邀请数) 与 "
        "Entrepreneur Immigration 的逐次抽签, 外加 registration pool 的 SIRS 分数分布快照. BC 用 SIRS "
        "(0-200 注册分), 不是联邦 CRS, 别混. 分数/人数可能是 'N/A' 或 '<5' 字符串. 命名 eye_fetch."
    )
    explicit_only = "BCPNP BC 省提名抽签 + SIRS 分布 (HTML 抓取, 命名 eye_fetch); SIRS 非联邦 CRS"
    search_url = "https://www.welcomebc.ca/immigrate-to-b-c/about-the-bc-provincial-nominee-program/invitations-to-apply"
    url_host = "welcomebc.ca"

    def _to_documents(self, raw, query, limit) -> list[PolarisDocument]:
        soup = BeautifulSoup(self._html(raw) or "", "lxml")
        docs: list[PolarisDocument] = []
        for tbl in soup.find_all("table"):
            hdr = _header(tbl)
            rows = tbl.find_all("tr")[1:]
            if any("score range" in h for h in hdr) and any("registration" in h for h in hdr):
                # the SIRS registration-pool distribution: one snapshot doc
                bands = [(c[0], c[1]) for tr in rows if len(c := _cells(tr)) >= 2]
                total = next((v for k, v in bands if k.lower().strip() in ("total", "total:")), "")
                body = "; ".join(f"{k}: {v}" for k, v in bands)
                docs.append(PolarisDocument(
                    source=self.name,
                    source_id=f"bcpnp_pool:{total or len(bands)}",
                    url=self.search_url,
                    title=f"BCPNP Skills 注册池 SIRS 分数分布 (total {total or 'n/a'})",
                    content=f"BC 省提名 Skills Immigration 注册池当前 SIRS 分数段分布: {body}.",
                    tags=["canada", "immigration", "pnp", "british-columbia", "pool"],
                    metadata={"distribution": dict(bands), "total": total},
                ))
                continue
            is_skills = any("ita type" in h for h in hdr)
            is_entre = any("stream" in h for h in hdr) and any(h == "date" for h in hdr)
            if not (is_skills or is_entre):
                continue
            kind = "Skills" if is_skills else "Entrepreneur"
            for tr in rows:
                c = _cells(tr)
                if len(c) < 4 or not c[0].strip():
                    continue
                if is_skills:  # Date | ITA type | Selection factors | Min score | Invitations
                    date_s, ita, factors, score, inv = (c + [""] * 5)[:5]
                    stream, extra = ita, factors
                else:          # Date | Stream | Min Score | Invitations
                    date_s, stream, score, inv = c[:4]
                    extra = ""
                docs.append(PolarisDocument(
                    source=self.name,
                    source_id=f"bcpnp:{kind}:{stream}:{date_s}:{score}",
                    url=self.search_url,
                    title=f"BCPNP {kind} · {stream} · {date_s}",
                    content=(f"BC 省提名 {kind} Immigration 抽签 — {stream}. 发出 {inv} 份邀请, "
                             f"最低 SIRS 分 {score}." + (f" 选择条件: {extra}." if extra else "")),
                    date=_parse_date(date_s),
                    tags=["canada", "immigration", "pnp", "british-columbia"],
                    metadata={"category": kind, "stream": stream, "draw_date": date_s,
                              "min_score": score, "invitations": inv, "selection_factors": extra},
                ))
        return _finish(docs, limit)


class AaipDrawsAdapter(_CaPnpBase):
    name = "aaip_draws"
    description = (
        "阿尔伯塔省提名 AAIP 抽签历史 — Alberta Advantage Immigration Program 的 'Draw information' 表: "
        "逐次抽签日期 + Worker stream/pathway (Alberta Opportunity / Rural Renewal / Tourism / "
        "Dedicated Health Care / Alberta Express Entry 各 priority sector 等) + 最低分 + 邀请数. "
        "省提名自有分, 非联邦 CRS. 命名 eye_fetch."
    )
    explicit_only = "AAIP 阿省提名抽签历史 (HTML 抓取, 命名 eye_fetch); 省提名非联邦 EE"
    search_url = "https://www.alberta.ca/aaip-processing-information"
    url_host = "alberta.ca"

    def _to_documents(self, raw, query, limit) -> list[PolarisDocument]:
        soup = BeautifulSoup(self._html(raw) or "", "lxml")
        docs: list[PolarisDocument] = []
        for tbl in soup.find_all("table"):
            hdr = _header(tbl)
            if not (any("draw date" in h for h in hdr)
                    and any("minimum score" in h for h in hdr)):
                continue
            for tr in tbl.find_all("tr")[1:]:
                c = _cells(tr)
                if len(c) < 4 or not c[0].strip():
                    continue
                draw_date, stream, score, inv = c[:4]
                docs.append(PolarisDocument(
                    source=self.name,
                    source_id=f"aaip:{draw_date}:{stream}:{score}",
                    url=self.search_url,
                    title=f"AAIP {stream} · {draw_date}",
                    content=(f"阿省提名 AAIP 抽签 — {stream}. 发出 {inv} 份邀请, 最低分 {score}."),
                    date=_parse_date(draw_date),
                    tags=["canada", "immigration", "pnp", "alberta"],
                    metadata={"stream": stream, "draw_date": draw_date,
                              "min_score": score, "invitations": inv},
                ))
        return _finish(docs, limit)
