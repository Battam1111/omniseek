"""ACL Anthology — the NLP venue-of-record, browsable by volume (keyless, official data).

The Anthology's own data repo ships one structured XML per collection
(verified live 2026-06-10: 2025.acl / 2024.emnlp / 2025.naacl all 200 on raw
.githubusercontent). That gives OmniSeek what dblp keyword search cannot: "list
what actually appeared at ACL 2025", with titles, authors, abstracts and
canonical aclanthology.org links. MODE: STRUCTURE (venue-of-record browse).

Query syntax (a volume token is REQUIRED; this is a browser, not a search
engine: cross-venue keyword search belongs to dblp / semantic_scholar / arxiv):
  "acl 2025 reasoning"          venue+year token + optional keyword filter
  "emnlp2024 retrieval"         same, compact form
  "volume:2024.findings-emnlp"  raw collection id passthrough
Supported acronyms: acl, emnlp, naacl, eacl, aacl, coling, tacl, cl, conll.
"""

from __future__ import annotations

import functools
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import anyio
import httpx

from omniseek.core import cache, diag, http
from omniseek.core.normalize import Document, keyword_score_filter

logger = logging.getLogger(__name__)

RAW = "https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml"
TIMEOUT = 30
USER_AGENT = "omniseek/0.1 (automated retrieval)"
CACHE_TTL = 7 * 86400  # published volumes are immutable; a week is conservative

_VENUES = "acl|emnlp|naacl|eacl|aacl|coling|tacl|cl|conll"
_TOKEN_RE = re.compile(rf"\b({_VENUES})[\s-]?(\d{{4}})\b", re.IGNORECASE)
_RAW_RE = re.compile(r"(?:^|\s)volume\s*:\s*(\S+)", re.IGNORECASE)


def _parse_collection(query: str) -> tuple[str, Optional[str]]:
    """Split a collection out of the query: raw `volume:` wins, else venue+year."""
    m = _RAW_RE.search(query or "")
    if m:
        return _RAW_RE.sub(" ", query).strip(), m.group(1)
    m = _TOKEN_RE.search(query or "")
    if m:
        coll = f"{m.group(2)}.{m.group(1).lower()}"
        return _TOKEN_RE.sub(" ", query).strip(), coll
    return (query or "").strip(), None


def _text(el) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


class ACLAnthologyAdapter:
    name = "acl_anthology"
    needs_credentials = False
    kind = "lookup"
    domains = ["papers"]
    description = (
        "ACL Anthology — NLP venue-of-record 按卷浏览 (官方数据仓 XML, keyless). "
        "查某届会议实际收了什么: 'acl 2025 reasoning' / 'emnlp2024 retrieval' / "
        "'volume:2024.findings-emnlp'(裸集合 id). 必须带 venue+年份 token "
        "(这是卷浏览器; 跨会关键词搜索请用 dblp / semantic_scholar / arxiv)"
    )

    def _papers(self, coll: str) -> list[dict]:
        key = cache.make_key("acl_anthology", "coll", coll)
        cached = cache.get(key)
        if cached is not None:
            return cached
        try:
            resp = httpx.get(f"{RAW}/{coll}.xml", headers={"User-Agent": USER_AGENT},
                             timeout=TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("acl_anthology: fetch/parse failed for %s: %s", coll, exc)
            st = getattr(getattr(exc, "response", None), "status_code", None)
            diag.note("acl_anthology.fetch", url=f"{RAW}/{coll}.xml", status=st, exc=exc)
            return []
        year = coll.split(".", 1)[0]
        papers: list[dict] = []
        for vol in root.iter("volume"):
            vol_id = vol.get("id") or ""
            for p in vol.iter("paper"):
                title = _text(p.find("title"))
                if not title or len(title) < 4:
                    continue  # frontmatter / malformed
                authors = []
                for a in p.findall("author"):
                    nm = " ".join(x for x in (_text(a.find("first")), _text(a.find("last"))) if x)
                    if not nm:
                        nm = _text(a)
                    if nm:
                        authors.append(nm)
                anth_id = _text(p.find("url")) or ""
                url = (f"https://aclanthology.org/{anth_id}/"
                       if anth_id and not anth_id.startswith("http") else anth_id)
                papers.append({
                    "title": title,
                    "authors": authors[:8],
                    "abstract": _text(p.find("abstract"))[:1200],
                    "url": url,
                    "volume": vol_id,
                    "year": year,
                    "id": anth_id or f"{coll}-{vol_id}-{p.get('id', '?')}",
                })
        cache.set(key, papers, ttl=CACHE_TTL)
        return papers

    async def _apapers(self, coll: str) -> list[dict]:
        """Async twin of ``_papers`` (S4b): BYTE-FAITHFUL mirror changing ONLY the blocking parts.
          - the disk cache read + write -> ``anyio.to_thread.run_sync`` (SAME cache key, so sync +
            async share one warmed collection entry);
          - the raw ``httpx.get`` XML fetch -> the shared async leaf ``http.aget`` (shared pool +
            SSRF guard + 30MB cap). ``http.aget`` returns the Response so ``resp.content`` (BYTES)
            stays byte-identical to ``_papers`` — ``aget_text`` would hand ``ET.fromstring`` a str,
            which raises on the XML's ``encoding=`` declaration; and it degrades to None on failure
            (already logged + ``diag.note``'d as "http.get"), mirroring ``_papers``' fetch-fail -> [];
          - the XML parse (ET + the volume/paper walk) is pure CPU, byte-identical, stays ON the loop.
        A fetch failure returns [] WITHOUT caching, exactly as ``_papers`` does."""
        key = cache.make_key("acl_anthology", "coll", coll)
        cached = await anyio.to_thread.run_sync(cache.get, key)  # disk read OFF loop
        if cached is not None:
            return cached
        resp = await http.aget(f"{RAW}/{coll}.xml", timeout=TIMEOUT)  # async network, ON loop
        if resp is None:
            return []  # egress failed (http.aget logged + diag.note'd); mirror _papers' fetch-fail -> []
        try:
            root = ET.fromstring(resp.content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("acl_anthology: parse failed for %s: %s", coll, exc)
            diag.note("acl_anthology.fetch", url=f"{RAW}/{coll}.xml", exc=exc)
            return []
        year = coll.split(".", 1)[0]
        papers: list[dict] = []
        for vol in root.iter("volume"):
            vol_id = vol.get("id") or ""
            for p in vol.iter("paper"):
                title = _text(p.find("title"))
                if not title or len(title) < 4:
                    continue  # frontmatter / malformed
                authors = []
                for a in p.findall("author"):
                    nm = " ".join(x for x in (_text(a.find("first")), _text(a.find("last"))) if x)
                    if not nm:
                        nm = _text(a)
                    if nm:
                        authors.append(nm)
                anth_id = _text(p.find("url")) or ""
                url = (f"https://aclanthology.org/{anth_id}/"
                       if anth_id and not anth_id.startswith("http") else anth_id)
                papers.append({
                    "title": title,
                    "authors": authors[:8],
                    "abstract": _text(p.find("abstract"))[:1200],
                    "url": url,
                    "volume": vol_id,
                    "year": year,
                    "id": anth_id or f"{coll}-{vol_id}-{p.get('id', '?')}",
                })
        await anyio.to_thread.run_sync(  # disk write OFF loop
            functools.partial(cache.set, key, papers, ttl=CACHE_TTL))
        return papers

    def search(self, query: str, limit: int = 10) -> list[Document]:
        terms, coll = _parse_collection(query)
        if not coll:
            return []  # no volume token: not this source's job (see description)
        docs = [self._to_doc(p, coll) for p in self._papers(coll)]
        docs = keyword_score_filter(docs, terms)
        return docs[:limit]

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` -> AsyncSearchCapable. Mirrors ``search`` line-for-line,
        awaiting ``_apapers`` (disk cache OFF the loop, XML fetch via the async leaf) instead of the
        sync ``_papers``. The ``_to_doc`` mapping + ``keyword_score_filter`` are pure CPU, so this is
        behavior-identical to ``search`` (same collection parse, same BM25 filter, same limit)."""
        terms, coll = _parse_collection(query)
        if not coll:
            return []  # no volume token: not this source's job (see description)
        docs = [self._to_doc(p, coll) for p in await self._apapers(coll)]
        docs = keyword_score_filter(docs, terms)
        return docs[:limit]

    def fetch_url(self, url: str) -> Optional[Document]:
        host = (urlparse(url).hostname or "").lower()
        if "aclanthology.org" not in host:
            return None
        anth_id = urlparse(url).path.strip("/")
        # id shape: {year}.{venue}-{volume}.{n} → collection = {year}.{venue}
        m = re.match(r"(\d{4}\.[a-z0-9-]+?)-[a-z0-9]+\.\d+$", anth_id)
        if not m:
            return None
        for p in self._papers(m.group(1)):
            if p["id"] == anth_id:
                return self._to_doc(p, m.group(1))
        return None

    def health_check(self) -> tuple[bool, str]:
        try:
            resp = httpx.head(f"{RAW}/2025.acl.xml", headers={"User-Agent": USER_AGENT},
                              timeout=10, follow_redirects=True)
            return resp.status_code == 200, f"HTTP {resp.status_code} (2025.acl probe)"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _to_doc(p: dict, coll: str) -> Document:
        date = None
        try:
            date = datetime(int(p["year"]), 1, 1, tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass
        content = (f"Volume: {coll} ({p['volume']})\n"
                   f"Authors: {', '.join(p['authors'])}\n\n{p['abstract'] or '(no abstract in XML)'}")
        return Document(
            source="acl_anthology",
            source_id=p["id"],
            url=p["url"],
            title=p["title"],
            content=content,
            author=", ".join(p["authors"][:4]) or None,
            date=date,
            tags=[coll, p["volume"], "paper"],
            metadata={"collection": coll, "volume": p["volume"], "anthology_id": p["id"]},
        )


from omniseek.core.fetcher import register_adapter

register_adapter(ACLAnthologyAdapter())
