"""CSRankings — CS faculty roster by institution / region (keyless, GitHub data).

CSRankings.org ranks CS departments by research output in selective venues and,
crucially for us, pins EVERY active CS faculty member to their institution. We
consume its public GitHub data (gh-pages ``csrankings.csv`` =
``name,affiliation,homepage,scholarid,orcid`` for ~20k faculty worldwide) to
answer a question the open web + dblp cannot assemble cheaply: "who are the CS
faculty at the institutions in region X (Singapore / Canada), with homepage +
Google Scholar + ORCID?" — the roster for targeting postdoc / PhD / PI groups in
the deployer's configured regions.

This is the WHO/WHERE. Pair it with: dblp (a faculty's papers), researcher_watch
(monitor a specific PI's new output by OpenAlex id), x_search ("joining lab X /
we are hiring") for the WHAT/WHEN.

NB on AREA: the raw CSV carries no per-author research area (CSRankings computes
area rankings client-side from DBLP venue counts). v1 filters by REGION /
INSTITUTION / NAME, not sub-area — for area-specific output, take a name from
here into dblp or researcher_watch. Intended queries: a region word
("singapore" / "canada" / "hong kong"), an institution ("National University of
Singapore", "University of Toronto"), or a faculty name. A region + an
unrecognised extra word (likely an area) falls back to the full region roster
rather than returning nothing.

Data is fetched once per process (rosters change slowly; the service restarts on
deploy) — no per-query refetch of the 4 MB file.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Optional
from urllib.parse import quote, urlparse

import httpx

from penumbra.core import diag, http
from penumbra.core.normalize import Document

logger = logging.getLogger(__name__)

GH = "https://raw.githubusercontent.com/emeryberger/CSrankings/gh-pages"
FACULTY_CSV = f"{GH}/csrankings.csv"
TIMEOUT = 30
USER_AGENT = "penumbra/0.1 (automated retrieval)"

# Region -> institution-name substrings (lowercased), filtered to the deployer's
# configured target geographies. Matching is substring-on-affiliation, so canonical
# CSRankings names ("National University of Singapore", "University of Toronto")
# are caught. A query that names an institution directly bypasses this map.
_REGIONS: dict[str, list[str]] = {
    "singapore": [
        "national university of singapore", "nanyang technological",
        "singapore management university", "singapore university of technology",
        "a*star", "institute for infocomm",
    ],
    "canada": [
        "university of toronto", "university of waterloo",
        "university of british columbia", "mcgill", "university of montreal",
        "université de montréal", "polytechnique montr", "university of alberta",
        "simon fraser", "mcmaster", "university of ottawa",
        "university of calgary", "university of western ontario",
        "western university", "queen's university", "concordia university",
        "dalhousie university", "university of victoria",
        "university of manitoba", "university of saskatchewan",
    ],
    "hong kong": ["hong kong"],
}

# Process-local roster cache (loaded once; None = not yet loaded / last fetch failed).
_FACULTY: Optional[list[dict]] = None


def _load_faculty() -> list[dict]:
    global _FACULTY
    if _FACULTY is not None:
        return _FACULTY
    try:
        resp = httpx.get(FACULTY_CSV, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — leave _FACULTY None so we retry next call
        logger.warning("csrankings: faculty CSV fetch failed: %s", exc)
        st = getattr(getattr(exc, "response", None), "status_code", None)
        diag.note("csrankings.csv", url=FACULTY_CSV, status=st, exc=exc)
        return []
    rows: list[dict] = []
    for r in csv.DictReader(io.StringIO(resp.text)):
        name = (r.get("name") or "").strip()
        aff = (r.get("affiliation") or "").strip()
        if not name or not aff:
            continue
        rows.append({
            "name": name,
            "affiliation": aff,
            "homepage": (r.get("homepage") or "").strip(),
            "scholarid": (r.get("scholarid") or "").strip(),
            "orcid": (r.get("orcid") or "").strip(),
        })
    _FACULTY = rows
    logger.info("csrankings: loaded %d faculty rows", len(rows))
    return rows


async def _aload_faculty() -> list[dict]:
    """Native-async twin of ``_load_faculty`` (a PURE ADDITION): shares the SAME process-local
    ``_FACULTY`` cache (loaded once per process; last-writer-wins on a cold-start race, exactly like the
    lock-free sync twin), the ONLY change being the lone raw-httpx CSV GET -> ``await http.aget_text``
    (the shared pool + SSRF guard + cache_only + a 30MB cap for free; the ~4MB roster sits well under the
    cap). The ``csv.DictReader`` walk is pure CPU and stays ON the loop, byte-identical to the sync path.
    On a fetch failure ``aget_text`` returns None (already logged + diag.note'd via the http.get tap):
    return [] and leave ``_FACULTY`` None so the next call retries, mirroring the sync except/return-[]."""
    global _FACULTY
    if _FACULTY is not None:
        return _FACULTY
    text = await http.aget_text(FACULTY_CSV, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    if text is None:
        return []
    rows: list[dict] = []
    for r in csv.DictReader(io.StringIO(text)):
        name = (r.get("name") or "").strip()
        aff = (r.get("affiliation") or "").strip()
        if not name or not aff:
            continue
        rows.append({
            "name": name,
            "affiliation": aff,
            "homepage": (r.get("homepage") or "").strip(),
            "scholarid": (r.get("scholarid") or "").strip(),
            "orcid": (r.get("orcid") or "").strip(),
        })
    _FACULTY = rows
    logger.info("csrankings: loaded %d faculty rows", len(rows))
    return rows


class CSRankingsAdapter:
    name = "csrankings"
    needs_credentials = False
    kind = "lookup"
    description = (
        "CSRankings — CS faculty roster by institution/region (keyless, via "
        "CSRankings.org GitHub data). Query a REGION (singapore / canada / "
        "hong kong), an INSTITUTION ('National University of Singapore', "
        "'University of Toronto'), or a faculty NAME → faculty with homepage + "
        "Google Scholar + ORCID + a DBLP link. The WHO/WHERE for PI/group/school "
        "targeting; pair with dblp / researcher_watch for their actual papers. "
        "(No per-area filter — take a name into dblp for that.)"
    )

    def search(self, query: str, limit: int = 10) -> list[Document]:
        faculty = _load_faculty()
        if not faculty:
            return []
        q = (query or "").strip().lower()

        # Detect a region word anywhere in the query; strip it, leaving any extra
        # term (an institution narrowing or, often, an area we can't filter on).
        region_patterns: Optional[list[str]] = None
        for rk, pats in _REGIONS.items():
            if rk in q:
                region_patterns = pats
                q = q.replace(rk, "").strip()
                break

        if region_patterns is not None:
            pool = [f for f in faculty
                    if any(p in f["affiliation"].lower() for p in region_patterns)]
            if q:
                narrowed = [f for f in pool
                            if q in f["affiliation"].lower() or q in f["name"].lower()]
                matched = narrowed or pool  # unrecognised extra word (area) → full roster
            else:
                matched = pool
        elif q:
            # No region word → treat the whole query as an institution / name substring.
            matched = [f for f in faculty
                       if q in f["affiliation"].lower() or q in f["name"].lower()]
        else:
            return []  # empty + no region → nothing to roster (this is a lookup source)

        matched.sort(key=lambda f: (f["affiliation"].lower(), f["name"].lower()))
        # CSRankings lists several name-spellings per person (same homepage / scholar
        # id) — collapse to one record so a roster isn't three rows of one professor.
        deduped, seen_people = [], set()
        for f in matched:
            sid = f["scholarid"]
            pid = sid if sid and sid != "NOSCHOLARPAGE" else (f["homepage"] or f["name"])
            if pid in seen_people:
                continue
            seen_people.add(pid)
            deduped.append(f)
        return [self._to_doc(f) for f in deduped[:limit]]

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``search`` (a PURE ADDITION): mirrors it line-for-line, the ONLY change
        being the roster egress (``_load_faculty`` -> ``await _aload_faculty``, whose lone raw-httpx CSV
        GET became ``await http.aget_text``). CSRankings keeps its roster in a process-local ``_FACULTY``
        global shared by both paths, NOT a disk cache, so there is no cache round-trip to push off the
        loop; the region-detection, substring filter, sort, person-dedup and ``_to_doc`` mapping are all
        pure CPU and stay ON the loop byte-identical to ``search``, so async and sync can never drift."""
        faculty = await _aload_faculty()
        if not faculty:
            return []
        q = (query or "").strip().lower()

        # Detect a region word anywhere in the query; strip it, leaving any extra
        # term (an institution narrowing or, often, an area we can't filter on).
        region_patterns: Optional[list[str]] = None
        for rk, pats in _REGIONS.items():
            if rk in q:
                region_patterns = pats
                q = q.replace(rk, "").strip()
                break

        if region_patterns is not None:
            pool = [f for f in faculty
                    if any(p in f["affiliation"].lower() for p in region_patterns)]
            if q:
                narrowed = [f for f in pool
                            if q in f["affiliation"].lower() or q in f["name"].lower()]
                matched = narrowed or pool  # unrecognised extra word (area) → full roster
            else:
                matched = pool
        elif q:
            # No region word → treat the whole query as an institution / name substring.
            matched = [f for f in faculty
                       if q in f["affiliation"].lower() or q in f["name"].lower()]
        else:
            return []  # empty + no region → nothing to roster (this is a lookup source)

        matched.sort(key=lambda f: (f["affiliation"].lower(), f["name"].lower()))
        # CSRankings lists several name-spellings per person (same homepage / scholar
        # id) — collapse to one record so a roster isn't three rows of one professor.
        deduped, seen_people = [], set()
        for f in matched:
            sid = f["scholarid"]
            pid = sid if sid and sid != "NOSCHOLARPAGE" else (f["homepage"] or f["name"])
            if pid in seen_people:
                continue
            seen_people.add(pid)
            deduped.append(f)
        return [self._to_doc(f) for f in deduped[:limit]]

    def fetch_url(self, url: str) -> Optional[Document]:
        # Roster-lookup source: nothing meaningful to fetch by a single URL.
        return None

    def health_check(self) -> tuple[bool, str]:
        try:
            resp = httpx.head(FACULTY_CSV, headers={"User-Agent": USER_AGENT},
                              timeout=10, follow_redirects=True)
            return resp.status_code == 200, f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _to_doc(f: dict) -> Document:
        name, aff = f["name"], f["affiliation"]
        homepage = f["homepage"]
        scholar = (f"https://scholar.google.com/citations?user={f['scholarid']}"
                   if f["scholarid"] else "")
        orcid_raw = f["orcid"]
        orcid = (f"https://orcid.org/{orcid_raw}"
                 if orcid_raw and orcid_raw != "0000-0000-0000-0000" else "")
        dblp_search = f"https://dblp.org/search?q={quote(name)}"

        parts = [f"Affiliation: {aff}"]
        if homepage:
            parts.append(f"Homepage: {homepage}")
        if scholar:
            parts.append(f"Google Scholar: {scholar}")
        if orcid:
            parts.append(f"ORCID: {orcid}")
        parts.append(f"DBLP: {dblp_search}")

        return Document(
            source="csrankings",
            source_id=f"{name}|{aff}",
            url=homepage or dblp_search,
            title=f"{name} — {aff}",
            content="\n".join(parts),
            author=name,
            date=None,
            tags=[aff, "faculty"],
            metadata={
                "name": name,
                "affiliation": aff,
                "homepage": homepage,
                "scholar_id": f["scholarid"],
                "scholar_url": scholar,
                "orcid": orcid_raw,
                "dblp_search": dblp_search,
            },
        )


from penumbra.core.fetcher import register_adapter

register_adapter(CSRankingsAdapter())
