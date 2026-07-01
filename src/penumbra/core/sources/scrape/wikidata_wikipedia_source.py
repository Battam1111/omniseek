"""Wikipedia + Wikidata — the encyclopedia + structured-fact spine the eye lacked.

Two complementary keyless layers behind one query:

  (a) Wikipedia articles (the prose encyclopedia). The Action API full-text search
      (``action=query&list=search&srsearch=<q>``) returns the matching page titles;
      the REST v1 summary endpoint
      (``/api/rest_v1/page/summary/<title>``) then hands back the lead-paragraph
      ``extract`` plus the canonical page URL, a one-line ``description``, and the
      page's ``wikibase_item`` (its Wikidata QID — the bridge between the two layers).

  (b) Wikidata entities (the structured-fact graph). ``wbsearchentities`` matches the
      query to entities, each with an ``id`` (QID), ``label``, and ``description``.
      The QID is a STRUCTURED HANDLE: a stable identifier an agent can hang further
      structured queries off (SPARQL, claims, cross-wiki sitelinks). The TOP entity is
      enriched with a couple of key claims (instance-of / subclass-of / part-of) via
      ``wbgetentities``, with the opaque property + value QIDs resolved to human labels
      in one batched follow-up call (so a claim reads "subclass of: artificial
      intelligence", not "P279: Q11660").

APIs (all keyless, generous per-IP limits):
  - https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=<q>&format=json
  - https://en.wikipedia.org/api/rest_v1/page/summary/<title>
  - https://www.wikidata.org/w/api.php?action=wbsearchentities&search=<q>&language=en&format=json
  - https://www.wikidata.org/w/api.php?action=wbgetentities&ids=<ids>&format=json   (enrichment)

BaseScrapeAdapter (template method): the cache check / atomic set_docs /
self-registration ritual lives in the base; this adapter fans out across the four
endpoints in ``_raw_fetch`` (returning an already-assembled ``{"articles", "entities"}``
payload) and maps that to docs in ``_to_documents``. ``rank`` stays default-False:
each endpoint already returns server-relevance order (Wikipedia search relevance +
Wikidata entity-match order), so we keep the merged order faithful and let the eye's
ranked search re-score across sources when it needs cross-source relevance.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import quote

from penumbra.core import http
from penumbra.core.normalize import PolarisDocument, jsonsafe, mk_signal
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_REST_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_ENTITY_BASE = "https://www.wikidata.org/wiki"
TIMEOUT = 15

# A few high-signal taxonomy properties to surface on the top entity. Resolved to
# human labels at render time; chosen because they place the entity in the graph
# (what it IS / what it is a KIND of / what it is PART of) rather than dumping all 80+.
KEY_CLAIM_PROPS = ("P31", "P279", "P361")  # instance of / subclass of / part of


class WikidataWikipediaAdapter(BaseScrapeAdapter):
    name = "wikidata_wikipedia"
    needs_credentials = False
    description = (
        "Wikipedia + Wikidata — encyclopedia article summaries plus structured-fact "
        "entities (QID handles + key claims) for any topic (keyless MediaWiki/Wikibase APIs)"
    )
    cache_ttl = 900
    kind = "lookup"
    domains = ["reference", "papers"]
    modes = ["STRUCTURE"]

    # --------------------------------------------------------------- fetch hook
    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """Fan out across the four endpoints and return an assembled payload. Returns
        None only if BOTH layers come back empty/failed (the base then yields [])."""
        # Split the budget: roughly half articles, half entities (at least 1 each).
        n_articles = max(1, limit - limit // 2)
        n_entities = max(1, limit // 2)

        articles = self._fetch_articles(query, n_articles)
        entities = self._fetch_entities(query, n_entities)

        if not articles and not entities:
            return None
        return {"articles": articles, "entities": entities}

    def _fetch_articles(self, query: str, n: int) -> list[dict]:
        """Action-API search → per-title REST summary. Each item: the summary dict
        (extract / content_urls / description / wikibase_item / thumbnail)."""
        search = http.get_json(
            WIKI_API,
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": n,
                "format": "json",
            },
            timeout=TIMEOUT,
        )
        if not isinstance(search, dict):
            return []
        hits = (search.get("query") or {}).get("search") or []

        out: list[dict] = []
        for hit in hits[:n]:
            if not isinstance(hit, dict):
                continue
            title = hit.get("title")
            if not title:
                continue
            summary = http.get_json(
                WIKI_REST_SUMMARY + quote(title.replace(" ", "_"), safe=""),
                timeout=TIMEOUT,
            )
            if isinstance(summary, dict) and summary.get("type") != "disambiguation":
                out.append(summary)
        return out

    def _fetch_entities(self, query: str, n: int) -> list[dict]:
        """wbsearchentities → entity hits (id / label / description / concepturi),
        with the top entity enriched with a couple of resolved key claims."""
        resp = http.get_json(
            WIKIDATA_API,
            params={
                "action": "wbsearchentities",
                "search": query,
                "language": "en",
                "uselang": "en",
                "format": "json",
                "limit": n,
            },
            timeout=TIMEOUT,
        )
        if not isinstance(resp, dict):
            return []
        hits = [h for h in (resp.get("search") or []) if isinstance(h, dict)][:n]
        if not hits:
            return []

        # Enrich ONLY the top entity (one extra graph lookup, not n of them).
        top_qid = hits[0].get("id")
        if top_qid:
            claims = self._fetch_key_claims(top_qid)
            if claims:
                hits[0] = {**hits[0], "_key_claims": claims}
        return hits

    def _fetch_key_claims(self, qid: str) -> list[tuple[str, str]]:
        """Fetch the entity's claims, keep the KEY_CLAIM_PROPS, and resolve the opaque
        property + value QIDs to human labels in ONE batched follow-up call. Returns a
        list of (property_label, value_label) pairs, best-effort (empty on any failure)."""
        ent_resp = http.get_json(
            WIKIDATA_API,
            params={"action": "wbgetentities", "ids": qid,
                    "languages": "en", "format": "json", "props": "claims"},
            timeout=TIMEOUT,
        )
        if not isinstance(ent_resp, dict):
            return []
        claims = ((ent_resp.get("entities") or {}).get(qid) or {}).get("claims") or {}
        if not isinstance(claims, dict):
            return []

        # Collect (prop, value-qid) pairs for the key properties (cap a few per prop).
        pairs: list[tuple[str, str]] = []
        ids_to_label: set[str] = set()
        for prop in KEY_CLAIM_PROPS:
            statements = claims.get(prop) or []
            kept = 0
            for st in statements:
                if not isinstance(st, dict) or kept >= 3:
                    continue
                dv = (st.get("mainsnak") or {}).get("datavalue") or {}
                if dv.get("type") != "wikibase-entityid":
                    continue
                vid = (dv.get("value") or {}).get("id")
                if not vid:
                    continue
                pairs.append((prop, vid))
                ids_to_label.update((prop, vid))
                kept += 1
        if not pairs:
            return []

        labels = self._resolve_labels(sorted(ids_to_label))
        resolved: list[tuple[str, str]] = []
        for prop, vid in pairs:
            resolved.append((labels.get(prop, prop), labels.get(vid, vid)))
        return resolved

    @staticmethod
    def _resolve_labels(ids: list[str]) -> dict[str, str]:
        """Batch-resolve a mixed list of P-/Q-ids to their English labels in one call."""
        if not ids:
            return {}
        resp = http.get_json(
            WIKIDATA_API,
            params={"action": "wbgetentities", "ids": "|".join(ids[:50]),
                    "languages": "en", "format": "json", "props": "labels"},
            timeout=TIMEOUT,
        )
        if not isinstance(resp, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in (resp.get("entities") or {}).items():
            label = ((v or {}).get("labels") or {}).get("en", {}).get("value")
            if label:
                out[k] = label
        return out

    # ----------------------------------------------------------- documents hook
    def _to_documents(self, raw: Any, query: str, limit: int) -> list[PolarisDocument]:
        if not isinstance(raw, dict):
            return []
        docs: list[PolarisDocument] = []
        for art in raw.get("articles") or []:
            doc = self._article_to_doc(art)
            if doc is not None:
                docs.append(doc)
        for ent in raw.get("entities") or []:
            doc = self._entity_to_doc(ent)
            if doc is not None:
                docs.append(doc)
        return docs

    def _article_to_doc(self, art: dict) -> Optional[PolarisDocument]:
        title = (art.get("title") or "").strip()
        extract = (art.get("extract") or "").strip()
        if not title:
            return None

        url = (
            ((art.get("content_urls") or {}).get("desktop") or {}).get("page")
            or f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'), safe='')}"
        )
        description = (art.get("description") or "").strip()
        qid = art.get("wikibase_item")

        # content = lead-paragraph extract, prefixed with the short description if present.
        parts: list[str] = []
        if description:
            parts.append(f"_{description}_")
        parts.append(extract or "(no summary extract available)")
        if qid:
            parts.append(
                f"Wikidata entity: {qid} "
                f"({WIKIDATA_ENTITY_BASE}/{qid}) — a structured handle for this topic."
            )
        content = "\n\n".join(parts)

        thumb = (art.get("thumbnail") or {}).get("source")
        media = [thumb] if isinstance(thumb, str) and thumb else []

        return PolarisDocument(
            source=self.name,
            source_id=f"wp:{art.get('pageid') or title}",
            url=url,
            title=title,
            content=content,
            media=media,
            tags=["wikipedia", "article"],
            metadata={
                "kind": "article",
                "wikibase_item": qid,
                "description": description or None,
                "raw": jsonsafe(art),
            },
        )

    def _entity_to_doc(self, ent: dict) -> Optional[PolarisDocument]:
        qid = ent.get("id")
        label = (ent.get("label") or "").strip()
        if not qid or not label:
            return None

        description = (ent.get("description") or "").strip()
        url = ent.get("concepturi") or f"{WIKIDATA_ENTITY_BASE}/{qid}"
        # concepturi is the entity-data URI (entity/Qxxx); the human page is wiki/Qxxx.
        page_url = f"{WIKIDATA_ENTITY_BASE}/{qid}"

        parts: list[str] = []
        if description:
            parts.append(description)
        parts.append(
            f"Wikidata entity {qid}: a STRUCTURED HANDLE for this concept. The QID is a "
            f"stable identifier an agent can hang further structured queries off (SPARQL, "
            f"claims, cross-wiki sitelinks)."
        )
        claims = ent.get("_key_claims") or []
        if claims:
            claim_lines = "\n".join(f"- {prop}: {val}" for prop, val in claims)
            parts.append("Key claims:\n" + claim_lines)
        content = "\n\n".join(parts)

        signals = mk_signal("qid_numeric", _qid_numeric(qid), kind="other", by="wikidata/qid")

        return PolarisDocument(
            source=self.name,
            source_id=f"wd:{qid}",
            url=page_url,
            title=label,
            content=content,
            signals=signals,
            tags=["wikidata", "entity"],
            metadata={
                "kind": "entity",
                "qid": qid,
                "concepturi": url,
                "description": description or None,
                "key_claims": [list(c) for c in claims] if claims else None,
                "raw": jsonsafe({k: v for k, v in ent.items() if k != "_key_claims"}),
            },
        )

    # --------------------------------------------------------------- liveness
    def health_check(self) -> tuple[bool, str]:
        """Cheap probe: a trivial Wikidata entity search proves the keyless API answers.
        (Uses the lighter wbsearchentities rather than the full four-endpoint fan-out.)"""
        try:
            resp = http.get_json(
                WIKIDATA_API,
                params={"action": "wbsearchentities", "search": "test",
                        "language": "en", "format": "json", "limit": 1},
                timeout=TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"
        if not isinstance(resp, dict) or "search" not in resp:
            return False, "unexpected wbsearchentities shape"
        return True, "OK"


def _qid_numeric(qid: str) -> Optional[int]:
    """Q137181967 → 137181967 (a stable mechanical fact, not a judgment). None if unparseable."""
    if isinstance(qid, str) and qid[:1] in ("Q", "q") and qid[1:].isdigit():
        return int(qid[1:])
    return None


# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
