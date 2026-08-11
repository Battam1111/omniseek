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
from penumbra.core.normalize import Document, jsonsafe, mk_signal
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
    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, dict):
            return []
        docs: list[Document] = []
        for art in raw.get("articles") or []:
            doc = self._article_to_doc(art)
            if doc is not None:
                docs.append(doc)
        for ent in raw.get("entities") or []:
            doc = self._entity_to_doc(ent)
            if doc is not None:
                docs.append(doc)
        return docs

    def _article_to_doc(self, art: dict) -> Optional[Document]:
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

        return Document(
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

    def _entity_to_doc(self, ent: dict) -> Optional[Document]:
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

        return Document(
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

    # ---------------------------------------------------- async assembly twin
    # `_raw_fetch` is the egressing layer here (the four-endpoint per-record fan-out:
    # per-title REST summary + top-entity claims + one batched label resolve); `_to_documents`
    # is PURE CPU (article/entity dict -> doc, no network). So the native-async twin mirrors
    # `_raw_fetch` (+ its egressing helpers) with every `http.get_json` -> `await http.aget_json`
    # (byte-faithful params / timeout / order / per-record skip-on-fail), and passes the SAME
    # sync `_to_documents` to `_asearch_via` (run on the loop, pure CPU). This is the shipped
    # discourse A2 idiom, not the Stack Exchange two-layer one, because the egress is in FETCH,
    # not in the doc mapper -- there is no doc-layer egress to make async.
    async def _araw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """Async twin of `_raw_fetch`: SAME budget split, SAME both-empty->None contract; the
        per-record enrichment egress goes native async via the async helper twins below."""
        n_articles = max(1, limit - limit // 2)
        n_entities = max(1, limit // 2)

        articles = await self._afetch_articles(query, n_articles)
        entities = await self._afetch_entities(query, n_entities)

        if not articles and not entities:
            return None
        return {"articles": articles, "entities": entities}

    async def _afetch_articles(self, query: str, n: int) -> list[dict]:
        """Async twin of `_fetch_articles`: Action-API search -> per-title REST summary. SAME
        srlimit, SAME per-title await order, SAME disambiguation drop + skip-on-fail."""
        search = await http.aget_json(
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
            summary = await http.aget_json(
                WIKI_REST_SUMMARY + quote(title.replace(" ", "_"), safe=""),
                timeout=TIMEOUT,
            )
            if isinstance(summary, dict) and summary.get("type") != "disambiguation":
                out.append(summary)
        return out

    async def _afetch_entities(self, query: str, n: int) -> list[dict]:
        """Async twin of `_fetch_entities`: wbsearchentities -> hits, top entity enriched with
        resolved key claims (ONE extra graph lookup, not n). Byte-faithful shape + order."""
        resp = await http.aget_json(
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
            claims = await self._afetch_key_claims(top_qid)
            if claims:
                hits[0] = {**hits[0], "_key_claims": claims}
        return hits

    async def _afetch_key_claims(self, qid: str) -> list[tuple[str, str]]:
        """Async twin of `_fetch_key_claims`: wbgetentities claims -> keep KEY_CLAIM_PROPS ->
        ONE batched label-resolve follow-up. SAME 3-per-prop cap, SAME order, [] on any failure."""
        ent_resp = await http.aget_json(
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

        labels = await self._aresolve_labels(sorted(ids_to_label))
        resolved: list[tuple[str, str]] = []
        for prop, vid in pairs:
            resolved.append((labels.get(prop, prop), labels.get(vid, vid)))
        return resolved

    @staticmethod
    async def _aresolve_labels(ids: list[str]) -> dict[str, str]:
        """Async twin of `_resolve_labels`: batch-resolve a mixed P-/Q-id list to English labels
        in one call."""
        if not ids:
            return {}
        resp = await http.aget_json(
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

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of `search` -> AsyncSearchCapable (the async fan-out awaits this
        directly; the four-endpoint per-record fan-out costs COROUTINES, not held pool threads).
        Shares the base async cache round-trip; egress via `_araw_fetch`; mapping via the SAME
        pure-CPU `_to_documents` (byte-identical to `search`, run on the loop)."""
        return await self._asearch_via(
            query, limit,
            afetch=lambda: self._araw_fetch(query, limit),
            abuild=lambda raw: self._to_documents(raw, query, limit))

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


# ===========================================================================
# Identity facet: name -> QID -> external-identifier cluster.
#
# A SIBLING adapter (same Wikidata upstream, same file) that treats the query as
# a PERSON or ORGANISATION name and returns the entity's cross-platform IDENTITY
# cluster: the external-id properties that let an agent jump straight to that
# person's ORCID / Google Scholar / DBLP / Semantic Scholar / GitHub / LinkedIn /
# X profile and official site, or the org's stock ticker / subsidiaries /
# industries. This is the edge the general wikidata_wikipedia lookup does not
# give: the identifier CROSSWALK, not the prose. It is explicit_only (target it
# by name, sources=['wikidata_identity']) so it stays off the broad-sweep hot
# path, and it self-selects: it emits a doc ONLY when the resolved entity
# actually carries at least one such identifier.
# ===========================================================================

# (property, cluster-key, formatter-URL with {v}). All string-valued (external-id
# or url datatype). The formatter URLs are Wikidata's own P1630 for each property.
IDENTITY_STRING_IDS = (
    ("P496", "orcid", "https://orcid.org/{v}"),
    ("P1960", "google_scholar", "https://scholar.google.com/citations?user={v}"),
    ("P2456", "dblp", "https://dblp.org/pid/{v}"),
    ("P4012", "semantic_scholar", "https://www.semanticscholar.org/author/-/{v}"),
    ("P2037", "github", "https://github.com/{v}"),
    ("P6634", "linkedin", "https://www.linkedin.com/in/{v}/"),
    ("P2002", "twitter", "https://x.com/{v}"),
    ("P856", "website", "{v}"),
)

# QID-valued org/person context properties, resolved to human labels.
IDENTITY_ITEM_PROPS = (
    ("P108", "employers"),
    ("P1416", "affiliations"),
    ("P452", "industries"),
    ("P355", "subsidiaries"),
)

P_STOCK_EXCHANGE = "P414"   # mainsnak value = exchange QID
P_TICKER = "P249"           # qualifier on P414 = ticker symbol (string)
P_INSTANCE_OF = "P31"
Q_HUMAN = "Q5"
IDENTITY_ITEM_CAP = 8       # cap QID-list props (subsidiaries can run long)

# Display labels for the human-readable content block.
_ID_LABELS = {
    "orcid": "ORCID",
    "google_scholar": "Google Scholar",
    "semantic_scholar": "Semantic Scholar",
    "dblp": "DBLP",
    "github": "GitHub",
    "linkedin": "LinkedIn",
    "twitter": "X (Twitter)",
    "website": "Official website",
    "employers": "Employer(s)",
    "affiliations": "Affiliation(s)",
    "industries": "Industry",
    "subsidiaries": "Subsidiaries",
}
# The order identifiers are rendered in the content block (research ids first).
_ID_DISPLAY_ORDER = (
    "orcid", "google_scholar", "semantic_scholar", "dblp",
    "github", "linkedin", "twitter", "website",
)


class WikidataIdentityAdapter(BaseScrapeAdapter):
    name = "wikidata_identity"
    needs_credentials = False
    explicit_only = (
        "identity crosswalk: target directly by name (sources=['wikidata_identity'])"
    )
    description = (
        "Wikidata identity crosswalk — resolve a person or organisation NAME to its "
        "cross-platform identifier cluster (ORCID, Google Scholar, DBLP, Semantic Scholar, "
        "GitHub, LinkedIn, X, official site; org stock ticker / subsidiaries / industries), "
        "so an agent can jump straight to the canonical profiles (keyless Wikibase API)"
    )
    cache_ttl = 900
    kind = "lookup"
    domains = ["people"]
    regions = ["global"]
    modes = ["STRUCTURE"]

    # --------------------------------------------------------------- fetch hook
    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """Resolve the name to the best entity that actually carries an identity
        cluster (scan the top few search hits so a disambiguation stub does not win),
        assemble that cluster, and resolve its QID-valued fields to labels. Returns
        None if no matched entity carries any identity property (base yields [])."""
        for hit in self._search_entities(query, 5):
            qid = hit.get("id")
            if not qid:
                continue
            claims = self._fetch_claims(qid)
            if not claims:
                continue
            cluster = self._extract_cluster(claims)
            if not (cluster["external_ids"] or cluster["tickers"] or cluster["items"]):
                continue  # not an identity-bearing entity; try the next hit
            cluster = self._resolve_item_labels(cluster)
            return {
                "qid": qid,
                "label": (hit.get("label") or "").strip(),
                "description": (hit.get("description") or "").strip(),
                "is_human": _has_instance(claims, Q_HUMAN),
                **cluster,
            }
        return None

    def _search_entities(self, query: str, n: int) -> list[dict]:
        resp = http.get_json(
            WIKIDATA_API,
            params={"action": "wbsearchentities", "search": query, "language": "en",
                    "uselang": "en", "format": "json", "limit": n, "type": "item"},
            timeout=TIMEOUT,
        )
        if not isinstance(resp, dict):
            return []
        return [h for h in (resp.get("search") or []) if isinstance(h, dict)]

    def _fetch_claims(self, qid: str) -> dict:
        resp = http.get_json(
            WIKIDATA_API,
            params={"action": "wbgetentities", "ids": qid, "languages": "en",
                    "format": "json", "props": "claims"},
            timeout=TIMEOUT,
        )
        if not isinstance(resp, dict):
            return {}
        claims = ((resp.get("entities") or {}).get(qid) or {}).get("claims") or {}
        return claims if isinstance(claims, dict) else {}

    @staticmethod
    def _extract_cluster(claims: dict) -> dict:
        """Pull the string external-ids (with resolvable URLs), the stock tickers (with
        exchange QID), and the QID-valued org/person context lists out of the claims."""
        external_ids: dict[str, dict] = {}
        for prop, key, tmpl in IDENTITY_STRING_IDS:
            value = _first_string_value(claims.get(prop))
            if value:
                external_ids[key] = {"value": value, "url": tmpl.format(v=value)}

        tickers: list[dict] = []
        for st in claims.get(P_STOCK_EXCHANGE) or []:
            if not isinstance(st, dict):
                continue
            ticker = _first_qualifier_string(st, P_TICKER)
            if ticker:
                tickers.append({"ticker": ticker, "exchange_qid": _snak_qid(st.get("mainsnak"))})

        items: dict[str, list[str]] = {}
        for prop, key in IDENTITY_ITEM_PROPS:
            qids = _statement_qids(claims.get(prop), IDENTITY_ITEM_CAP)
            if qids:
                items[key] = qids
        return {"external_ids": external_ids, "tickers": tickers, "items": items}

    def _resolve_item_labels(self, cluster: dict) -> dict:
        """Batch-resolve every QID in the cluster (org/person items + stock exchanges)
        to an English label in ONE follow-up call, in place."""
        ids: set[str] = set()
        for qids in cluster["items"].values():
            ids.update(qids)
        for t in cluster["tickers"]:
            if t.get("exchange_qid"):
                ids.add(t["exchange_qid"])
        labels = _resolve_labels_batch(sorted(ids)) if ids else {}

        cluster["items"] = {
            key: [{"qid": q, "label": labels.get(q, q)} for q in qids]
            for key, qids in cluster["items"].items()
        }
        for t in cluster["tickers"]:
            eq = t.get("exchange_qid")
            t["exchange"] = labels.get(eq, eq) if eq else None
        return cluster

    # ----------------------------------------------------------- documents hook
    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, dict) or not raw.get("qid"):
            return []
        doc = self._identity_to_doc(raw)
        return [doc] if doc is not None else []

    def _identity_to_doc(self, raw: dict) -> Optional[Document]:
        qid = raw["qid"]
        label = raw.get("label") or qid
        description = raw.get("description") or ""
        ext = raw.get("external_ids") or {}
        tickers = raw.get("tickers") or []
        items = raw.get("items") or {}

        parts: list[str] = []
        if description:
            parts.append(f"_{description}_")
        parts.append(
            f"Identity cluster for {label} (Wikidata {qid}). Canonical cross-platform "
            f"identifiers an agent can resolve directly:"
        )
        id_lines = [
            f"- {_ID_LABELS[key]}: {ext[key]['url']}"
            for key in _ID_DISPLAY_ORDER if key in ext
        ]
        if id_lines:
            parts.append("\n".join(id_lines))
        if tickers:
            tl = ", ".join(f"{t.get('exchange') or '?'}: {t['ticker']}" for t in tickers)
            parts.append(f"Stock ticker(s): {tl}")
        for key in ("employers", "affiliations", "industries", "subsidiaries"):
            vals = items.get(key)
            if vals:
                parts.append(f"{_ID_LABELS[key]}: " + ", ".join(v["label"] for v in vals))
        content = "\n\n".join(parts)

        n_ids = len(ext) + len(tickers)
        signals = mk_signal("identifier_count", n_ids, kind="other", by="wikidata/identity")

        return Document(
            source=self.name,
            source_id=f"wdid:{qid}",
            url=f"{WIKIDATA_ENTITY_BASE}/{qid}",
            title=label,
            content=content,
            signals=signals,
            tags=["wikidata", "identity"],
            metadata={
                "kind": "identity",
                "qid": qid,
                "is_human": bool(raw.get("is_human")),
                "description": description or None,
                # flat machine-usable id -> raw value, plus the resolvable URL per id
                "identifiers": {k: v["value"] for k, v in ext.items()},
                "identifier_urls": {k: v["url"] for k, v in ext.items()},
                "tickers": [{"ticker": t["ticker"], "exchange": t.get("exchange")}
                            for t in tickers] or None,
                "employers": [v["label"] for v in items.get("employers", [])] or None,
                "affiliations": [v["label"] for v in items.get("affiliations", [])] or None,
                "industries": [v["label"] for v in items.get("industries", [])] or None,
                "subsidiaries": [v["label"] for v in items.get("subsidiaries", [])] or None,
            },
        )

    # ---------------------------------------------------- async assembly twin
    # As with the sibling adapter, the egress lives in `_raw_fetch` (entity search + per-hit
    # claims + one batched QID-label resolve); `_to_documents`/`_identity_to_doc` are pure CPU.
    # The async twin mirrors `_raw_fetch` (+ its egressing helpers) with `http.get_json` ->
    # `await http.aget_json` (byte-faithful params / order / the SAME top-5-hit scan for the
    # first identity-bearing entity), and passes the SAME sync `_to_documents` to `_asearch_via`.
    async def _araw_fetch(self, query: str, limit: int) -> Optional[Any]:
        """Async twin of `_raw_fetch`: SAME top-5 scan + first-identity-bearing-hit selection,
        SAME assemble+resolve, SAME None-when-none contract. Only the egress swaps to async; the
        pure-CPU `_extract_cluster` + `_has_instance` are reused unchanged."""
        for hit in await self._asearch_entities(query, 5):
            qid = hit.get("id")
            if not qid:
                continue
            claims = await self._afetch_claims(qid)
            if not claims:
                continue
            cluster = self._extract_cluster(claims)
            if not (cluster["external_ids"] or cluster["tickers"] or cluster["items"]):
                continue  # not an identity-bearing entity; try the next hit
            cluster = await self._aresolve_item_labels(cluster)
            return {
                "qid": qid,
                "label": (hit.get("label") or "").strip(),
                "description": (hit.get("description") or "").strip(),
                "is_human": _has_instance(claims, Q_HUMAN),
                **cluster,
            }
        return None

    async def _asearch_entities(self, query: str, n: int) -> list[dict]:
        """Async twin of `_search_entities`."""
        resp = await http.aget_json(
            WIKIDATA_API,
            params={"action": "wbsearchentities", "search": query, "language": "en",
                    "uselang": "en", "format": "json", "limit": n, "type": "item"},
            timeout=TIMEOUT,
        )
        if not isinstance(resp, dict):
            return []
        return [h for h in (resp.get("search") or []) if isinstance(h, dict)]

    async def _afetch_claims(self, qid: str) -> dict:
        """Async twin of `_fetch_claims`."""
        resp = await http.aget_json(
            WIKIDATA_API,
            params={"action": "wbgetentities", "ids": qid, "languages": "en",
                    "format": "json", "props": "claims"},
            timeout=TIMEOUT,
        )
        if not isinstance(resp, dict):
            return {}
        claims = ((resp.get("entities") or {}).get(qid) or {}).get("claims") or {}
        return claims if isinstance(claims, dict) else {}

    async def _aresolve_item_labels(self, cluster: dict) -> dict:
        """Async twin of `_resolve_item_labels`: batch-resolve every QID in the cluster (org/
        person items + stock exchanges) to an English label in ONE async follow-up call, in place."""
        ids: set[str] = set()
        for qids in cluster["items"].values():
            ids.update(qids)
        for t in cluster["tickers"]:
            if t.get("exchange_qid"):
                ids.add(t["exchange_qid"])
        labels = await _aresolve_labels_batch(sorted(ids)) if ids else {}

        cluster["items"] = {
            key: [{"qid": q, "label": labels.get(q, q)} for q in qids]
            for key, qids in cluster["items"].items()
        }
        for t in cluster["tickers"]:
            eq = t.get("exchange_qid")
            t["exchange"] = labels.get(eq, eq) if eq else None
        return cluster

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of `search` -> AsyncSearchCapable. The per-record egress (entity
        search + per-hit claims + QID-label resolve) goes native async in `_araw_fetch`; mapping
        via the SAME pure-CPU `_to_documents` (byte-identical to `search`)."""
        return await self._asearch_via(
            query, limit,
            afetch=lambda: self._araw_fetch(query, limit),
            abuild=lambda raw: self._to_documents(raw, query, limit))

    # --------------------------------------------------------------- liveness
    def health_check(self) -> tuple[bool, str]:
        """Cheap probe: a trivial entity search proves the keyless API answers (does NOT
        run the full resolve+claims fan-out)."""
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


def _first_string_value(statements: Optional[list]) -> Optional[str]:
    """First non-empty string-datavalue (external-id / url datatype) among statements."""
    for st in statements or []:
        if not isinstance(st, dict):
            continue
        val = ((st.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _snak_qid(snak: Optional[dict]) -> Optional[str]:
    """QID out of a wikibase-entityid mainsnak (None otherwise)."""
    dv = (snak or {}).get("datavalue") or {}
    if dv.get("type") == "wikibase-entityid":
        return (dv.get("value") or {}).get("id")
    return None


def _first_qualifier_string(statement: dict, prop: str) -> Optional[str]:
    """First string-valued qualifier (e.g. P249 ticker) on a statement."""
    for q in (statement.get("qualifiers") or {}).get(prop) or []:
        val = ((q or {}).get("datavalue") or {}).get("value")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _statement_qids(statements: Optional[list], cap: int) -> list[str]:
    """Ordered, de-duplicated QIDs of a QID-valued property, capped."""
    out: list[str] = []
    for st in statements or []:
        if not isinstance(st, dict):
            continue
        qid = _snak_qid(st.get("mainsnak"))
        if qid and qid not in out:
            out.append(qid)
        if len(out) >= cap:
            break
    return out


def _has_instance(claims: dict, target_qid: str) -> bool:
    """True if the entity has an instance-of (P31) statement pointing at target_qid."""
    for st in claims.get(P_INSTANCE_OF) or []:
        if isinstance(st, dict) and _snak_qid(st.get("mainsnak")) == target_qid:
            return True
    return False


def _resolve_labels_batch(ids: list[str]) -> dict[str, str]:
    """Batch-resolve a list of Q-/P-ids to their English labels in one call."""
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


async def _aresolve_labels_batch(ids: list[str]) -> dict[str, str]:
    """Async twin of `_resolve_labels_batch`: batch-resolve a list of Q-/P-ids to their English
    labels in one call."""
    if not ids:
        return {}
    resp = await http.aget_json(
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


# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
