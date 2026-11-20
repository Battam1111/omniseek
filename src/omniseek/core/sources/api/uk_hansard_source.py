"""uk_hansard - UK Parliament Hansard: the official record of Commons and Lords debates (STRUCTURE).

Why a source and not the generic web read (2026-11-18): hansard.parliament.uk answers
every program, OmniSeek's exit, the worker exit and Jina's headless Chrome alike, with Cloudflare's
managed challenge (HTTP 403, 「Just a moment... Enable JavaScript and cookies to continue」), so omniseek_read
of a debate page came back as a refused challenge on both the plain and the Jina tier (job loc1118f-uk
could not read the 2026-11-02 「Immigration: Earned Settlement」 statement). A browser tier would not
change that: Jina's render IS a headless browser and gets the same page. Parliament publishes the same
record through a keyless JSON API on another host that is not behind the challenge:

  GET https://hansard-api.parliament.uk/debates/debate/<ExtId>.json
      -> {Overview: {Title, Date, House, Location, ExtId, HRSTag, ...},
          Items: [{ItemType "Contribution" | "Timestamp" | ..., AttributedTo, Value (HTML), HRSTag,
                   OrderInSection, ...}], ChildDebates: [<same shape>], Navigator: [...]}
  GET https://hansard-api.parliament.uk/search/debates.json?queryParameters.searchTerm=<q>
        &queryParameters.take=<n>&queryParameters.orderBy=SittingDateDesc
      -> {Results: [{Title, SittingDate, House, DebateSectionExtId, DebateSection, Rank}], TotalResultCount}
      (matches debate TITLES, not speech text)

A web debate address carries the same ExtId, which is how omniseek_read of a hansard.parliament.uk page is
answered here:
  https://hansard.parliament.uk/<House>/<YYYY-MM-DD>/debates/<ExtId>/<TitleSlug>
One document = one debate: the title, the sitting and chamber, then every contribution as
「<speaker>: <text>」 in order (child debates appended after the parent). Checked 2026-11-18: the
11-02 Commons statement is 155 items, 137 KB of JSON. The API host has no robots.txt (404) and
publishes no rate; the content is under the Open Parliament Licence.

Reliance map: the two endpoints above, the Overview / Items / ChildDebates keys, ItemType
"Contribution" with AttributedTo + Value, and the ExtId (a GUID) in the web address.
"""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import quote, urlparse

from omniseek.core import http
from omniseek.core.normalize import Document, html_fragment_text, to_utc
from omniseek.core.sources.api._base import BaseAPIAdapter

API = "https://hansard-api.parliament.uk"
WEB = "https://hansard.parliament.uk"
_WEB_DEBATE = re.compile(r"^/(?P<house>commons|lords)/(?P<date>\d{4}-\d{2}-\d{2})/debates/(?P<ext>[0-9a-f-]{36})(?:/|$)",
                         re.IGNORECASE)
_SLUG = re.compile(r"[^A-Za-z0-9]+")


def _web_url(house: str, date: str, ext_id: str, title: str) -> str:
    return f"{WEB}/{house}/{date[:10]}/debates/{ext_id}/{_SLUG.sub('', title or '')}"


def _debate_lines(debate: dict) -> list[str]:
    """Every contribution of one debate (and its child debates) as 「speaker: text」, in order."""
    lines: list[str] = []
    for item in debate.get("Items") or []:
        if not isinstance(item, dict) or item.get("ItemType") != "Contribution":
            continue
        text = html_fragment_text(item.get("Value") or "").strip()
        if not text:
            continue
        who = (item.get("AttributedTo") or "").strip()
        lines.append(f"{who}: {text}" if who else text)
    for child in debate.get("ChildDebates") or []:
        if isinstance(child, dict):
            title = ((child.get("Overview") or {}).get("Title") or "").strip()
            if title:
                lines.append(f"\n## {title}")
            lines.extend(_debate_lines(child))
    return lines


class UKHansardAdapter(BaseAPIAdapter):
    name = "uk_hansard"
    needs_credentials = False
    description = (
        "UK Parliament Hansard: the official record of House of Commons and House of Lords debates, "
        "ministerial statements and questions, keyless. Search matches debate TITLES (newest first); "
        "omniseek_read of a hansard.parliament.uk debate address returns the whole debate, every contribution "
        "with its speaker (the web pages themselves are behind a bot challenge)."
    )
    cache_ttl = 21600  # 6h: a sitting's record is settled within a day or two
    rank_locally = False  # the API's own order (newest sitting first)
    kind = "lookup"
    domains = ["policy", "immigration"]
    regions = ["uk"]
    modes = ["UNWALL", "STRUCTURE"]
    url_host = "hansard.parliament.uk"
    explicit_only = ("uk_hansard: a named UK parliamentary-record drill (by debate title) and the reader "
                     "for hansard.parliament.uk addresses, not broad fan-out fodder")

    async def _araw_fetch(self, query: str, limit: int) -> list:
        q = (query or "").strip()
        if not q:
            return []
        url = (f"{API}/search/debates.json?queryParameters.searchTerm={quote(q)}"
               f"&queryParameters.take={max(1, min(int(limit), 50))}&queryParameters.orderBy=SittingDateDesc")
        data = await http.aget_json(url, timeout=20)
        if not isinstance(data, dict):
            raise RuntimeError("hansard search did not answer with JSON")
        return [r for r in data.get("Results") or [] if isinstance(r, dict) and r.get("DebateSectionExtId")]

    def _to_document(self, raw) -> Optional[Document]:
        ext, title = raw.get("DebateSectionExtId"), (raw.get("Title") or "").strip()
        house, date = raw.get("House") or "Commons", raw.get("SittingDate") or ""
        if not ext or not title or not date:
            return None
        return Document(
            source=self.name,
            source_id=ext,
            url=_web_url(house, date, ext, title),
            title=title,
            content=f"{title}\n{house}, {raw.get('DebateSection') or ''}, sitting of {date[:10]}",
            date=to_utc(date),
            tags=["hansard", house.lower()],
            metadata={"ext_id": ext, "house": house, "section": raw.get("DebateSection"),
                      "sitting_date": date[:10], "api_url": f"{API}/debates/debate/{ext}.json"},
        )

    async def afetch_url(self, url: str) -> Optional[Document]:
        p = urlparse(url)
        if (p.hostname or "").lower() != self.url_host:
            return None
        m = _WEB_DEBATE.match(p.path or "")
        if not m:
            return None
        ext = m.group("ext")
        data = await http.aget_json(f"{API}/debates/debate/{ext}.json", timeout=30)
        if not isinstance(data, dict) or not isinstance(data.get("Overview"), dict):
            return None
        ov = data["Overview"]
        title = (ov.get("Title") or "").strip()
        house = ov.get("House") or m.group("house").capitalize()
        date = (ov.get("Date") or m.group("date"))[:10]
        lines = _debate_lines(data)
        if not lines:
            return None
        head = f"{title}\n{house}, {ov.get('Location') or ''}, sitting of {date}\n"
        return Document(
            source=self.name,
            source_id=ext,
            url=_web_url(house, date, ext, title),
            title=title,
            content=head + "\n" + "\n\n".join(lines),
            date=to_utc(date),
            tags=["hansard", str(house).lower()],
            metadata={"ext_id": ext, "house": house, "location": ov.get("Location"),
                      "sitting_date": date, "hrs_tag": ov.get("HRSTag"), "contributions": len(lines),
                      "api_url": f"{API}/debates/debate/{ext}.json"},
        )

    async def ahealth_check(self) -> tuple[Optional[bool], str]:
        data: Any = await http.aget_json(
            f"{API}/search/debates.json?queryParameters.searchTerm=immigration&queryParameters.take=1", timeout=20)
        if isinstance(data, dict) and isinstance(data.get("Results"), list):
            return True, f"OK ({data.get('TotalResultCount')} debates titled 'immigration')"
        return False, "hansard search did not answer with a Results list"

# Registration is automatic via BaseAPIAdapter.__init_subclass__ (no module-tail ceremony).
