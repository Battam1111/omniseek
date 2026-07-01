"""Kaggle — public dataset listing via the keyless v1 datasets/list endpoint.

Kaggle is the largest public catalog of machine-learning DATASETS (plus the notebooks
and competitions built on them), the place where applied-ML practitioners publish,
version, and license tabular / image / text corpora. It fills Polaris-eye's gap on
the *data artifact* a researcher wants to actually train on, complementing Zenodo's
DOI-minted long tail and HuggingFace's model/dataset hub: Kaggle's signal is the
community vote + download tally, which marks the de-facto canonical dataset for a topic.

Access via the public Kaggle v1 API listing endpoint (verified keyless 2026-06-17):

    GET https://www.kaggle.com/api/v1/datasets/list?search=<query>

Despite the official ``kaggle`` python client wanting a token, the listing endpoint
answers anonymously (HTTP 200, no auth) and returns a JSON ARRAY of dataset objects:
``{ref, url, title, subtitle, description, voteCount, downloadCount, viewCount,
totalBytes, licenseName, ownerName, lastUpdated, usabilityRating, tags:[{name,...}]}``.
It returns ~20 rows per page (no client-side page-size control on the keyless path),
so the adapter slices to ``limit``.

Thin subclass over BaseScrapeAdapter: the cache check / atomic set_docs / self-registration
ritual lives in the base; this adapter only declares its facets and fills the two hooks.
``rank`` stays default-False — the endpoint returns the site's own relevance order for the
search term, and the eye's ranked search re-scores across sources when it needs to.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from penumbra.core import http
from penumbra.core.normalize import PolarisDocument, jsonsafe, mk_signal
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

API_URL = "https://www.kaggle.com/api/v1/datasets/list"
DATASET_BASE = "https://www.kaggle.com/datasets/"


class KaggleAdapter(BaseScrapeAdapter):
    name = "kaggle"
    needs_credentials = False
    description = "Kaggle — public ML dataset catalog (vote/download-ranked tabular/image/text corpora, keyless listing API)"
    cache_ttl = 900
    kind = "lookup"
    domains = ["datasets", "code"]
    modes = ["STRUCTURE"]

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        return http.get_json(
            API_URL,
            params={"search": query},
            timeout=15,
        )

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[PolarisDocument]:
        if not isinstance(raw, list):
            return []
        docs: list[PolarisDocument] = []
        for rec in raw[:limit]:
            if not isinstance(rec, dict):
                continue
            doc = self._record_to_doc(rec)
            if doc is not None:
                docs.append(doc)
        return docs

    def _record_to_doc(self, rec: dict) -> Optional[PolarisDocument]:
        title = (rec.get("title") or "").strip()
        ref = (rec.get("ref") or "").strip()
        if not title and not ref:
            return None
        if not title:
            title = ref

        # Canonical dataset page: prefer the server-supplied url, else build from ref.
        url = (rec.get("url") or "").strip() or (f"{DATASET_BASE}{ref}" if ref else "")

        # Content: subtitle is the reliable one-liner (description is usually empty on the
        # listing payload); prefer the longer of the two so we never drop a real description.
        subtitle = (rec.get("subtitle") or "").strip()
        description = (rec.get("description") or "").strip()
        content = description if len(description) > len(subtitle) else subtitle

        author = (rec.get("ownerName") or rec.get("creatorName") or "").strip() or None
        date = _parse_date(rec.get("lastUpdated"))

        # tags: each is a dict like {"name": "weather and climate", "fullPath": "subject > ..."}.
        tags = _extract_tags(rec.get("tags") or [])

        # Two source-reported engagement facts: community votes + downloads (None-safe).
        signals = {
            **mk_signal("votes", rec.get("voteCount"), kind="engagement", by="kaggle/voteCount"),
            **mk_signal("downloads", rec.get("downloadCount"), kind="engagement", by="kaggle/downloadCount"),
        }

        metadata: dict[str, Any] = {
            "ref": ref or None,
            "size": rec.get("totalBytes"),
            "licenseName": rec.get("licenseName") or None,
            "usabilityRating": rec.get("usabilityRating"),
            "viewCount": rec.get("viewCount"),
            "raw": jsonsafe(rec),
        }

        return PolarisDocument(
            source=self.name,
            source_id=ref or str(rec.get("id") or title),
            url=url,
            title=title,
            content=content,
            author=author,
            date=date,
            signals=signals,
            tags=tags,
            metadata=metadata,
        )


def _extract_tags(tags_raw: list) -> list[str]:
    """Kaggle tags are objects (``{"name": ..., "fullPath": "subject > earth > ..."}``);
    pull the human ``name`` (falling back to ``ref``). Plain strings pass through too."""
    out: list[str] = []
    for t in tags_raw:
        if isinstance(t, dict):
            nm = (t.get("name") or t.get("ref") or "").strip()
            if nm:
                out.append(nm)
        elif isinstance(t, str) and t.strip():
            out.append(t.strip())
    return out


def _parse_date(s: Any) -> Optional[datetime]:
    """Kaggle ``lastUpdated`` is ISO 8601 with a ``Z`` suffix (e.g. 2024-02-22T08:53:54.627Z).
    The shared convention: swap ``Z`` for ``+00:00`` and ``fromisoformat`` (3.11+ handles the
    fractional seconds). None on anything unparseable — a missing date is not a failure."""
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
