"""HuggingFace Hub — models, datasets, and Spaces unified search.

HF Hub is the canonical ML model/dataset/code-app distribution platform.
Penumbra exposes it for:
- Finding pre-trained models by task/keyword
- Discovering datasets (e.g., instruction-tuned, multi-modal)
- Exploring deployed Spaces (interactive demos)

Public API (no auth, no rate-limit for read):
- huggingface.co/api/models?search=...
- huggingface.co/api/datasets?search=...
- huggingface.co/api/spaces?search=...

Each item dict: id (full path), author, downloads, likes, lastModified,
tags, library_name, pipeline_tag, cardData.

For a unified query, this adapter fans out to all 3 categories and merges
results sorted by downloads (popularity proxy).

Migrated onto ``BaseAPIAdapter`` (template method). The fan-out + merge + sort
lives in ``_raw_fetch`` (returns ``(item, kind)`` tuples already sorted by
``downloads`` desc + truncated to ``limit``), so the base's ``raw_items[:limit]``
is a no-op and ``_to_document`` maps each tuple via the unchanged
``_item_to_document``. ``rank_locally=False`` because the original applied NO
lexical filter — the base preserves the downloads-sorted order verbatim.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import httpx

from penumbra.core import http
from penumbra.core.normalize import Document, jsonsafe, mk_signal
from penumbra.core.sources.api._base import BaseAPIAdapter

logger = logging.getLogger(__name__)

HF_API_BASE = "https://huggingface.co/api"
TIMEOUT = 15
USER_AGENT = "penumbra/0.1 (automated retrieval)"


class HuggingFaceHubAdapter(BaseAPIAdapter):
    name = "huggingface_hub"
    needs_credentials = False
    description = "HuggingFace Hub — models / datasets / Spaces unified search (open API)"
    cache_ttl = 900
    rank_locally = False  # original applied no keyword filter — keep downloads order verbatim

    # ------------------------------------------------------------------ hooks
    def _raw_fetch(self, query: str, limit: int) -> list:
        """Fan out to the 3 categories, merge, sort by downloads desc, truncate.

        Returns ``(item, kind)`` tuples in final order. Sorting the raw items by
        ``item['downloads']`` desc reproduces the original's post-map
        ``docs.sort(key=lambda d: d.attention_value() or 0, reverse=True)`` exactly,
        because ``_item_to_document`` carries downloads as the engagement signal;
        Python's stable sort preserves the models→datasets→spaces (each in API order)
        tie-break, the same pre-sort order the original built. Truncating to ``limit``
        here makes the base's ``raw_items[:limit]`` a no-op.
        """
        # Fan out to 3 categories. Roughly equal share, plus one extra slot per
        # category for headroom; we'll trim to `limit` after sorting.
        per_cat = max(2, (limit // 3) + 1)
        pairs: list[tuple[dict, str]] = []
        for kind in ("models", "datasets", "spaces"):
            items = http.get_json(
                f"{HF_API_BASE}/{kind}",
                params={
                    "search": query,
                    "limit": per_cat,
                    "sort": "downloads",
                    "direction": -1,
                },
                timeout=TIMEOUT,
            )
            if items is None:
                logger.warning("HF %s search failed", kind)
                continue
            for item in items[:per_cat]:
                pairs.append((item, kind))

        # Sort merged across categories by downloads (popularity). Same key + stable
        # sort as the original's post-map docs.sort(by attention_value) since the
        # engagement signal == downloads.
        pairs.sort(key=lambda p: (p[0].get("downloads") or 0), reverse=True)
        return pairs[:limit]

    def _to_document(self, raw) -> Optional[Document]:
        item, kind = raw
        return self._item_to_document(item, kind)

    # --------------------------------------------------------------- fetch_url
    def fetch_url(self, url: str) -> Optional[Document]:
        host = (urlparse(url).hostname or "").lower()
        if "huggingface.co" not in host:
            return None
        path = urlparse(url).path.strip("/")
        parts = path.split("/")
        if len(parts) < 2:
            return None
        # Patterns:
        #   /<author>/<model>            → models
        #   /datasets/<author>/<dataset> → datasets
        #   /spaces/<author>/<space>     → spaces
        if parts[0] == "datasets":
            kind = "datasets"
            slug = "/".join(parts[1:3])
        elif parts[0] == "spaces":
            kind = "spaces"
            slug = "/".join(parts[1:3])
        elif parts[0] in ("blog", "papers", "docs", "course"):
            # Not a model/dataset/space; defer to other adapters (e.g., frontier_labs for /blog/)
            return None
        else:
            kind = "models"
            slug = "/".join(parts[0:2])

        if not slug or "/" not in slug:
            return None

        item = http.get_json(f"{HF_API_BASE}/{kind}/{slug}", timeout=TIMEOUT)
        if item is None:
            return None
        return self._item_to_document(item, kind)

    # ------------------------------------------------------------- health_check
    def health_check(self) -> tuple[bool, str]:
        try:
            resp = httpx.get(
                f"{HF_API_BASE}/models",
                params={"search": "bert", "limit": 1},
                headers={"User-Agent": USER_AGENT},
                timeout=8,
            )
            return resp.status_code == 200, f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _item_to_document(item: dict, kind: str) -> Document:
        item_id = item.get("id") or item.get("modelId") or item.get("datasetId") or "?"
        if kind == "models":
            url = f"https://huggingface.co/{item_id}"
            type_label = "Model"
        elif kind == "datasets":
            url = f"https://huggingface.co/datasets/{item_id}"
            type_label = "Dataset"
        elif kind == "spaces":
            url = f"https://huggingface.co/spaces/{item_id}"
            type_label = "Space"
        else:
            url = f"https://huggingface.co/{item_id}"
            type_label = kind.title()

        author = item.get("author")
        if not author and "/" in item_id:
            author = item_id.split("/", 1)[0]

        likes = item.get("likes") or 0
        downloads = item.get("downloads") or 0

        last_modified = item.get("lastModified") or item.get("createdAt")
        date = None
        if last_modified:
            try:
                date = datetime.fromisoformat(last_modified.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        # Build description from cardData if available, else use pipeline_tag
        description = ""
        card_data = item.get("cardData")
        if isinstance(card_data, dict):
            description = card_data.get("description") or ""
        if not description:
            pipeline = item.get("pipeline_tag")
            library = item.get("library_name")
            description = f"{type_label} on Hugging Face"
            if pipeline:
                description += f" • pipeline: {pipeline}"
            if library:
                description += f" • library: {library}"

        return Document(
            source="huggingface_hub",
            source_id=item_id,
            url=url,
            title=f"[{type_label}] {item_id}",
            content=description,
            author=author,
            date=date,
            signals=mk_signal('downloads', downloads, kind='engagement', by='huggingface_hub/downloads'),
            tags=[kind] + (item.get("tags") or [])[:10],
            metadata={
                "kind": kind,
                "downloads": downloads,
                "likes": likes,
                "library_name": item.get("library_name"),
                "pipeline_tag": item.get("pipeline_tag"),
                "raw": jsonsafe(item),
            },
        )
