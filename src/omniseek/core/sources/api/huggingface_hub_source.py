"""HuggingFace Hub — models, datasets, and Spaces unified search.

HF Hub is the canonical ML model/dataset/code-app distribution platform.
OmniSeek exposes it for:
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

import asyncio
import logging
from typing import Optional
from urllib.parse import urlparse

from omniseek.core import diag, http, upstreams
from omniseek.core.normalize import Document, jsonsafe, mk_signal, to_utc
from omniseek.core.sources.api._base import BaseAPIAdapter

logger = logging.getLogger(__name__)

HF_API_BASE = "https://huggingface.co/api"
TIMEOUT = 15
USER_AGENT = "omniseek/0.1 (automated retrieval)"

# First path parts of huggingface.co that are site sections, never an author of a model repository.
_SITE_SECTIONS = frozenset({
    "api", "blog", "papers", "docs", "course", "learn", "organizations", "models", "datasets", "spaces",
    "collections", "posts", "settings", "login", "join", "pricing", "enterprise", "tasks", "chat", "new",
    "notifications", "search", "inference-endpoints",
})
_NO_PUBLIC_REPO = "HF 上没有这个公开仓库（私有、已删或不存在；HF 对没登录的请求一律回 401）"


class HuggingFaceHubAdapter(BaseAPIAdapter):
    name = "huggingface_hub"
    needs_credentials = False
    description = "HuggingFace Hub: models / datasets / Spaces unified search (open API)"
    cache_ttl = 900
    rank_locally = False  # original applied no keyword filter — keep downloads order verbatim

    async def _araw_fetch(self, query: str, limit: int) -> list:
        """Async replacement of the deleted sync ``_raw_fetch`` (11-04, this is the only one now): the bounded 3-listing fan-out, run CONCURRENTLY.

        Same per-category budget, same three categories in the same order, same URL / params /
        timeout, same None → warn+skip contract, same merge + downloads-desc stable sort, same
        ``pairs[:limit]`` truncation, and the shared-http egress is ``await http.aget_json`` per
        listing. What changed on 2026-08-30 is that the three listings are issued CONCURRENTLY
        instead of one after another: /api/models, /api/datasets and /api/spaces are independent
        queries whose only meeting point is the sort below, and ``per_cat`` is computed before any
        of them, so no call reads a previous answer. Results are re-paired against the category
        order, which the stable sort's models → datasets → spaces tie-break depends on. The
        per-model detail fetch lives in ``fetch_url`` (not here), so nothing else egresses.
        """
        kinds = ("models", "datasets", "spaces")
        per_cat = max(2, (limit // 3) + 1)

        async def _one(kind: str) -> Optional[list]:
            items = await http.aget_json(
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
                return None
            return items[:per_cat]

        settled = await asyncio.gather(*(_one(k) for k in kinds), return_exceptions=True)

        pairs: list[tuple[dict, str]] = []
        for kind, items in zip(kinds, settled):
            if isinstance(items, BaseException):
                # aget_json returns None on failure, so the serial loop could not raise here.
                # One category blowing up must not cost the other two.
                logger.warning("HF %s search raised: %s", kind, items)
                continue
            if items is None:
                continue
            for item in items:
                pairs.append((item, kind))

        pairs.sort(key=lambda p: (p[0].get("downloads") or 0), reverse=True)
        return pairs[:limit]

    def _to_document(self, raw) -> Optional[Document]:
        item, kind = raw
        return self._item_to_document(item, kind)

    # --------------------------------------------------------------- fetch_url
    @staticmethod
    def _repo_of(url: str) -> Optional[tuple[str, str]]:
        """``(kind, "<author>/<name>")`` when ``url`` is a repository page on huggingface.co, else None.

        Only these are repository pages (each may end in ``/tree/<rev>``):
          /<author>/<name>            → models
          /datasets/<author>/<name>   → datasets
          /spaces/<author>/<name>     → spaces
        Any other address (/api/..., /blob/, /resolve/, /organizations/, /blog/ ...) is not ours: the
        generic web read takes it. Before 2026-11-06 every path of two or more parts was looked up as a
        model, so /api/datasets/<a>/<b>/commits/main became the repository "api/datasets" and came back
        as HF's 401 "Invalid username or password"."""
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if host not in ("huggingface.co", "www.huggingface.co"):
            return None
        parts = [s for s in p.path.split("/") if s]
        if len(parts) >= 2 and parts[-2] == "tree":
            parts = parts[:-2]
        if len(parts) == 3 and parts[0] in ("datasets", "spaces"):
            return parts[0], f"{parts[1]}/{parts[2]}"
        if len(parts) == 2 and parts[0] not in _SITE_SECTIONS:
            return "models", f"{parts[0]}/{parts[1]}"
        return None

    async def afetch_url(self, url: str) -> Optional[Document]:
        repo = self._repo_of(url)
        if repo is None:
            return None
        kind, slug = repo
        with diag.tap() as seen:
            item = await http.aget_json(f"{HF_API_BASE}/{kind}/{slug}", timeout=TIMEOUT)
        if item is None:
            if any(r.get("status") in (401, 404) for r in seen if isinstance(r, dict)):
                # A settled "not there" (final): the read ends with this reason, so neither another reader
                # nor the generic web read gets the URL (it read HF's "404" page as the content), and HF's
                # own "Invalid username or password", which reads as a broken token, is not the reason.
                diag.note("huggingface_hub.fetch_url", url=url, body=_NO_PUBLIC_REPO, final=True)
            else:
                # Not a "not there": a failed request (5xx, timeout, a gate that sent nothing). Said about
                # this URL, so a generic read that then gets the page carries owner_adapter_missed.
                recs = [r for r in seen if isinstance(r, dict)]
                diag.note("huggingface_hub.fetch_url", url=url, body=(
                    f"HF API 没读到 {kind}/{slug} 的元数据（{diag.failure_reason(recs, fallback='没有记下原因')}）"))
            return None
        doc = self._item_to_document(item, kind)
        prefix = "" if kind == "models" else f"{kind}/"
        card = await http.aget_text(f"https://huggingface.co/{prefix}{slug}/raw/main/README.md", timeout=TIMEOUT)
        if card and card.strip():
            doc.content = card
        else:
            doc.content = "没有模型卡（README.md 取不到），下面只有元数据。" + (f"\n{doc.content}" if doc.content else "")
        return doc

    # ------------------------------------------------------------- health_check
    async def ahealth_check(self) -> tuple[Optional[bool], str]:
        try:
            async with upstreams.aegress(f"{HF_API_BASE}/models", request_s=8):  # declared Hugging Face gate
                resp = await http.adirect(
                    "GET", f"{HF_API_BASE}/models",
                    params={"search": "bert", "limit": 1},
                    headers={"User-Agent": USER_AGENT},
                    timeout=8,
                )
            upstreams.observe_response(HF_API_BASE, resp)
            return resp.status_code == 200, f"HTTP {resp.status_code}"
        except upstreams.UpstreamBusy as exc:  # nothing sent: not verified (None), not healthy
            return None, f"degraded: declared Hugging Face gate busy, not probed this cycle ({exc})"
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
        date = to_utc(last_modified)

        # Build description from cardData if available, else use pipeline_tag
        description = ""
        card_data = item.get("cardData")
        if isinstance(card_data, dict):
            description = card_data.get("description") or ""
        # no card description: no body (pipeline_tag / library_name ride in metadata)

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
