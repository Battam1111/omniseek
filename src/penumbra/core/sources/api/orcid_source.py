"""ORCID — authenticated researcher CV records via the keyless public API.

ORCID is the global researcher-identifier registry: a stable iD per person plus a
self-asserted, often institution-verified CV (employments, educations, works,
fundings). Polaris-eye's people-STRUCTURE lookup — given a researcher name it
resolves the iD and hands back the structured career record the open web only
scatters across faculty pages and CVs.

Access via the ORCID Public API v3.0 (no auth, no token needed in practice — the
docs describe OAuth but the public read endpoints answer with a bare
``Accept: application/json`` header; this is live-confirmed, decode-by-probe):
  (a) name resolution
      GET https://pub.orcid.org/v3.0/expanded-search/?q=<query>
      → {"expanded-result": [{"orcid-id": "0000-0002-...", ...}, ...]}
  (b) one researcher's record
      GET https://pub.orcid.org/v3.0/<orcid-id>/record
      → a deeply nested CV (see _record_to_doc for the exact field map)

This is a FAN-OUT lookup: one expanded-search, then a polite per-iD GET /record
for the top ``limit`` hits (one PolarisDocument per researcher). The fan-out lives
in ``_raw_fetch`` (it returns the already-fetched (iD, record) pairs); the per-record
decode is the pure, golden-fixturable ``_record_to_doc``.

Robustness contract: a failed expanded-search ⇒ _raw_fetch returns None ⇒ search ⇒
[] (the adapter contract). A single record that fails to fetch or to decode is
skipped, never fatal to the batch. The ORCID record JSON wraps almost every scalar
in {"value": ...}, so the decode unwraps defensively layer by layer (every .get
guards None).

explicit_only: a named drill — you look ORCID up BY a researcher's name, it is not
broad-fan-out fodder (a bare topic query would burn the polite per-iD fan-out on
irrelevant people). rank stays default-False: expanded-search returns server
relevance order for the name and the fan-out preserves it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from penumbra.core import http
from penumbra.core.normalize import PolarisDocument, jsonsafe, mk_signal
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

API_BASE = "https://pub.orcid.org/v3.0"
SEARCH_URL = f"{API_BASE}/expanded-search/"
PROFILE_URL = "https://orcid.org/{orcid}"
# ORCID public read endpoints answer to a bare Accept header (no OAuth token in practice).
_ACCEPT = {"Accept": "application/json"}
# Cap the per-iD detail fan-out so a large limit can never hammer the unregistered
# public endpoint; expanded-search itself returns far more than we ever drill.
_MAX_FANOUT = 8


class OrcidAdapter(BaseScrapeAdapter):
    name = "orcid"
    needs_credentials = False
    description = ("ORCID — researcher iD + authenticated CV record (employments / educations / "
                   "works / fundings) via the keyless public v3.0 API; name it to resolve a "
                   "researcher by name and pull their structured career record. STRUCTURE, "
                   "people-lookup, pub.orcid.org.")
    cache_ttl = 86400  # 24h: a researcher's CV changes slowly
    kind = "lookup"
    domains = ["people"]
    modes = ["STRUCTURE"]
    explicit_only = ("orcid: a named drill — look a researcher up BY name to pull their CV; "
                     "not broad-fan-out fodder (a topic query would waste the polite per-iD fan-out)")

    def _raw_fetch(self, query: str, limit: int) -> Optional[list[tuple[str, dict]]]:
        """expanded-search → top iDs → polite per-iD GET /record. Returns the list of
        (orcid_id, record) pairs (decode happens in _to_documents). None ONLY if the
        search call fails (the adapter contract); a per-record miss is just skipped."""
        search = http.get_json(SEARCH_URL, params={"q": query},
                               headers=_ACCEPT, timeout=15)
        if not isinstance(search, dict):
            return None  # search endpoint unreachable / unparseable ⇒ [] upstream
        results = search.get("expanded-result") or []
        if not isinstance(results, list):
            return None

        n = max(1, min(int(limit), _MAX_FANOUT))
        pairs: list[tuple[str, dict]] = []
        seen: set[str] = set()
        for item in results:
            if len(pairs) >= n:
                break
            if not isinstance(item, dict):
                continue
            oid = item.get("orcid-id")
            if not isinstance(oid, str) or not oid or oid in seen:
                continue
            seen.add(oid)
            record = http.get_json(f"{API_BASE}/{oid}/record",
                                   headers=_ACCEPT, timeout=15)
            if isinstance(record, dict):
                pairs.append((oid, record))
        return pairs

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[PolarisDocument]:
        if not isinstance(raw, list):
            return []
        docs: list[PolarisDocument] = []
        for pair in raw[:limit]:
            if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                continue
            oid, record = pair
            doc = self._record_to_doc(oid, record)
            if doc is not None:
                docs.append(doc)
        return docs

    # ------------------------------------------------------------------ decode
    def _record_to_doc(self, orcid_id: Any, record: Any) -> Optional[PolarisDocument]:
        """One ORCID /record payload → one PolarisDocument. Pure + total: returns None
        on a missing iD, never raises on the deep nesting (every layer guards None)."""
        if not isinstance(orcid_id, str) or not orcid_id:
            return None  # no stable iD ⇒ no doc
        if not isinstance(record, dict):
            record = {}

        person = record.get("person") or {}
        activities = record.get("activities-summary") or {}

        name = self._full_name(person)
        employments = self._summaries(activities.get("employments"), "employment-summary")
        educations = self._summaries(activities.get("educations"), "education-summary")
        works = self._work_titles(activities.get("works"))

        current_org = self._latest_org(employments)
        title = name or orcid_id
        if current_org:
            title = f"{title} ({current_org})"

        content = self._compose_content(name, employments, educations, works)
        date = self._latest_date(employments, works)

        return PolarisDocument(
            source=self.name,
            source_id=orcid_id,
            url=PROFILE_URL.format(orcid=orcid_id),
            title=title,
            content=content,
            author=name or None,
            date=date,
            signals=self._works_signal(works),
            tags=[o for o in (current_org,) if o],
            metadata={
                "orcid_id": orcid_id,
                "name": name,
                "employments": employments,
                "educations": educations,
                "works": works,
                "raw": jsonsafe(record),
            },
        )

    # ------------------------------------------------------------- field helpers
    @staticmethod
    def _unwrap(node: Any) -> Optional[str]:
        """ORCID wraps scalars as {"value": "..."}; pull the string out, else None."""
        if isinstance(node, dict):
            val = node.get("value")
            return val if isinstance(val, str) and val.strip() else None
        if isinstance(node, str) and node.strip():
            return node
        return None

    @classmethod
    def _full_name(cls, person: dict) -> str:
        """person.name.{given-names,family-name} → "Given Family" (either part may be absent)."""
        name = person.get("name") or {}
        if not isinstance(name, dict):
            return ""
        given = cls._unwrap(name.get("given-names")) or ""
        family = cls._unwrap(name.get("family-name")) or ""
        full = " ".join(p for p in (given, family) if p).strip()
        if full:
            return full
        # Fall back to the public credit-name if structured parts are absent.
        return cls._unwrap(name.get("credit-name")) or ""

    @classmethod
    def _summaries(cls, group_block: Any, summary_key: str) -> list[dict]:
        """employments / educations share one shape:
            {affiliation-group: [{summaries: [{<summary_key>: {...}}, ...]}, ...]}
        Flatten each affiliation summary to a compact {role, org, start, end} dict."""
        out: list[dict] = []
        if not isinstance(group_block, dict):
            return out
        groups = group_block.get("affiliation-group") or []
        if not isinstance(groups, list):
            return out
        for group in groups:
            if not isinstance(group, dict):
                continue
            for summary in (group.get("summaries") or []):
                if not isinstance(summary, dict):
                    continue
                node = summary.get(summary_key) or {}
                if not isinstance(node, dict):
                    continue
                org = node.get("organization") or {}
                out.append({
                    "role": node.get("role-title"),
                    "org": org.get("name") if isinstance(org, dict) else None,
                    "start": cls._date_str(node.get("start-date")),
                    "end": cls._date_str(node.get("end-date")),
                })
        return out

    @classmethod
    def _work_titles(cls, works_block: Any, cap: int = 10) -> list[dict]:
        """activities.works.{group: [{work-summary: [{title:{title:{value}}, type,
        publication-date, ...}]}]} → a flat list of compact work dicts (first ``cap``)."""
        out: list[dict] = []
        if not isinstance(works_block, dict):
            return out
        groups = works_block.get("group") or []
        if not isinstance(groups, list):
            return out
        for group in groups:
            if not isinstance(group, dict):
                continue
            for ws in (group.get("work-summary") or []):
                if not isinstance(ws, dict):
                    continue
                title_block = ws.get("title") or {}
                title = None
                if isinstance(title_block, dict):
                    title = cls._unwrap(title_block.get("title"))
                if not title:
                    continue
                out.append({
                    "title": title,
                    "type": ws.get("type"),
                    "date": cls._date_str(ws.get("publication-date")),
                })
                if len(out) >= cap:
                    return out
        return out

    @staticmethod
    def _date_str(date_block: Any) -> Optional[str]:
        """ORCID dates are {year:{value}, month:{value}, day:{value}} (month/day optional).
        → "YYYY", "YYYY-MM", or "YYYY-MM-DD". None if there is no year."""
        if not isinstance(date_block, dict):
            return None

        def part(key: str) -> Optional[str]:
            node = date_block.get(key)
            if isinstance(node, dict):
                v = node.get("value")
                return v if isinstance(v, str) and v.strip() else None
            return None

        year = part("year")
        if not year:
            return None
        month = part("month")
        day = part("day")
        out = year
        if month:
            out += f"-{month.zfill(2)}"
            if day:
                out += f"-{day.zfill(2)}"
        return out

    @staticmethod
    def _latest_org(employments: list[dict]) -> Optional[str]:
        """Best-effort "current affiliation": the first employment with an open end-date
        (still active), else the first listed org. ORCID lists most-recent first."""
        for emp in employments:
            if emp.get("org") and not emp.get("end"):
                return emp["org"]
        for emp in employments:
            if emp.get("org"):
                return emp["org"]
        return None

    @classmethod
    def _latest_date(cls, employments: list[dict], works: list[dict]) -> Optional[datetime]:
        """Newest YYYY[-MM[-DD]] across employment starts + work dates → a datetime, else None."""
        candidates: list[datetime] = []
        for src in (employments, works):
            for item in src:
                d = cls._parse_partial(item.get("start") or item.get("date"))
                if d is not None:
                    candidates.append(d)
        return max(candidates) if candidates else None

    @staticmethod
    def _parse_partial(raw: Any) -> Optional[datetime]:
        """Parse a "YYYY", "YYYY-MM", or "YYYY-MM-DD" string (padding partials). None else."""
        if not isinstance(raw, str) or not raw.strip():
            return None
        for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
            try:
                return datetime.strptime(raw.strip(), fmt)
            except ValueError:
                continue
        return None

    @staticmethod
    def _compose_content(name: str, employments: list[dict],
                         educations: list[dict], works: list[dict]) -> str:
        """Human-readable CV summary: recent affiliations + a few representative work titles."""
        lines: list[str] = []
        if name:
            lines.append(f"# {name}")

        def fmt_aff(a: dict) -> str:
            role = a.get("role")
            org = a.get("org")
            span = a.get("start") or ""
            if a.get("end"):
                span = f"{span} to {a['end']}".strip()
            head = " at ".join(p for p in (role, org) if p) or (org or "")
            return f"{head} ({span})".strip() if span else head

        emp_lines = [fmt_aff(e) for e in employments[:4] if (e.get("role") or e.get("org"))]
        if emp_lines:
            lines.append("")
            lines.append("## Employment")
            lines.extend(f"- {l}" for l in emp_lines)

        edu_lines = [fmt_aff(e) for e in educations[:3] if (e.get("role") or e.get("org"))]
        if edu_lines:
            lines.append("")
            lines.append("## Education")
            lines.extend(f"- {l}" for l in edu_lines)

        work_lines = []
        for w in works[:5]:
            t = w.get("title")
            if not t:
                continue
            d = w.get("date")
            work_lines.append(f"- {t}" + (f" ({d})" if d else ""))
        if work_lines:
            lines.append("")
            lines.append("## Selected works")
            lines.extend(work_lines)

        return "\n".join(lines).strip()

    @staticmethod
    def _works_signal(works: list[dict]) -> dict:
        """Number of listed works → an engagement-class signal (the researcher's output scale).
        Empty ⇒ {} (no fabricated zero)."""
        if not works:
            return {}
        return mk_signal("works", len(works), kind="engagement",
                         by="orcid/works.count", unit="works")

    # ---------------------------------------------------------------- liveness
    def health_check(self) -> tuple[bool, str]:
        """Cheap liveness: a bare expanded-search answers without any per-iD fan-out."""
        try:
            raw = http.get_json(SEARCH_URL, params={"q": "Einstein"},
                                headers=_ACCEPT, timeout=15)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"
        if not isinstance(raw, dict):
            return False, "expanded-search returned nothing"
        return True, "OK"

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
