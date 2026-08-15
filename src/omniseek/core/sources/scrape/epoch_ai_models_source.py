"""Epoch AI - notable ML models with their training-scale facts (keyless bulk CSV).

Epoch AI maintains the canonical open dataset of significant ML models: for each
model it records the organization, publication date, parameter count, training
compute (FLOP), training dataset size, training hardware, country, notability
criteria, citation count and release/accessibility. It is the reference table
behind the compute-scaling literature (the "training compute doubles every N
months" charts). We consume its public keyless CSV
(https://epoch.ai/data/notable_ai_models.csv, ~1000 curated models) so an agent
can answer the STRUCTURE question the open web will not assemble cheaply: "what
was <model>'s training compute / parameters / dataset size?", "which models did
<org> ship and at what scale?", "the notable models in <domain> (Biology / Games
/ Language)?".

Shape: this is the keyless-bulk-CSV pattern (the csrankings sibling), expressed
on BaseScrapeAdapter. There is NO query API - the whole file is fetched ONCE per
process into a module cache (``_load_models``; rosters change slowly and the
service restarts on deploy, so no per-query re-download of the 2 MB file), and
each query BM25-filters the in-memory rows via the shared scorer. ``_raw_fetch``
returns those cached rows and ``_to_documents`` builds + ranks + slices, so the
base's per-query disk cache stores only the sliced result, never the full table.

This is the WHAT-SCALE. Pair it with: huggingface_hub / modelscope (a model's
weights + card), arxiv / semantic_scholar (its paper), org_watch (a lab's
shipping cadence). Query a MODEL name ('AlphaFold', 'GPT-3'), an ORG ('DeepMind',
'Anthropic'), a DOMAIN ('Biology', 'Games'), or a bare query for the newest.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from typing import Optional

import httpx

from omniseek.core import diag, http
from omniseek.core.normalize import Document, keyword_score_filter
from omniseek.core.sources.scrape._base import BaseScrapeAdapter

logger = logging.getLogger(__name__)

CSV_URL = "https://epoch.ai/data/notable_ai_models.csv"
DATASET_PAGE = "https://epoch.ai/data/notable-ai-models"
TIMEOUT = 30
USER_AGENT = "omniseek/0.1 (automated retrieval)"

# Clean scalar columns we surface (the scaling story). The CSV also carries long,
# occasionally mojibake'd free-text notes columns (Parameters notes / Abstract) which
# we deliberately do NOT read, so a garbled note never leaks into a doc.
_FIELDS = (
    "Model", "Organization", "Publication date", "Domain", "Task", "Parameters",
    "Training compute (FLOP)", "Training dataset size (total)", "Training hardware",
    "Country (of organization)", "Notability criteria", "Citations",
    "Model accessibility", "Base model", "Open model weights?", "Link",
)

# Process-local row cache (loaded once; None = not yet loaded / last fetch failed).
_MODELS: Optional[list[dict]] = None


def _load_models() -> list[dict]:
    """Fetch + parse the notable-models CSV once per process. [] on failure (leaves
    the cache None so the next call retries), matching the adapter empty contract."""
    global _MODELS
    if _MODELS is not None:
        return _MODELS
    try:
        resp = httpx.get(CSV_URL, headers={"User-Agent": USER_AGENT},
                         timeout=TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - leave _MODELS None so we retry next call
        logger.warning("epoch_ai_models: CSV fetch failed: %s", exc)
        st = getattr(getattr(exc, "response", None), "status_code", None)
        diag.note("notable_ai_models.csv", url=CSV_URL, status=st, exc=exc)
        return []
    rows: list[dict] = []
    for r in csv.DictReader(io.StringIO(resp.text)):
        model = (r.get("Model") or "").strip()
        if not model:
            continue
        rows.append({k: (r.get(k) or "").strip() for k in _FIELDS})
    # Newest first: a bare / term-less query then returns the most recent models.
    rows.sort(key=lambda r: r["Publication date"], reverse=True)
    _MODELS = rows
    logger.info("epoch_ai_models: loaded %d models", len(rows))
    return rows


async def _aload_models() -> list[dict]:
    """Async twin of ``_load_models`` (S4b): BYTE-FAITHFUL mirror changing ONLY the egress.
    The raw ``httpx.get(CSV).text`` -> the shared async leaf ``http.aget_text`` (shared pool + SSRF
    guard + cache_only + a 30MB cap; the CSV is ~2MB, well under). It keeps the sync client's UA +
    timeout + follow_redirects (client-level), returns the decoded ``.text`` byte-identically to
    ``_load_models``' ``resp.text``, and degrades to None on any failure (already logged +
    ``diag.note``'d as "http.get"), mirroring ``_load_models``' fetch-fail -> [] (which leaves
    ``_MODELS`` None so the next call retries). The csv parse + newest-first sort are pure CPU,
    byte-identical, stay ON the loop; ``_MODELS`` is the SAME process cache the sync twin fills, so
    once either twin has loaded the table the other returns it with zero egress."""
    global _MODELS
    if _MODELS is not None:
        return _MODELS
    text = await http.aget_text(CSV_URL, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    if text is None:  # egress failed (logged + diag.note'd as "http.get"); leave _MODELS None -> retry
        return []
    rows: list[dict] = []
    for r in csv.DictReader(io.StringIO(text)):
        model = (r.get("Model") or "").strip()
        if not model:
            continue
        rows.append({k: (r.get(k) or "").strip() for k in _FIELDS})
    # Newest first: a bare / term-less query then returns the most recent models.
    rows.sort(key=lambda r: r["Publication date"], reverse=True)
    _MODELS = rows
    logger.info("epoch_ai_models: loaded %d models", len(rows))
    return rows


def _parse_date(s: str) -> Optional[datetime]:
    """Epoch publication dates are uniformly YYYY-MM-DD (or blank)."""
    try:
        return datetime.strptime(s, "%Y-%m-%d") if s else None
    except ValueError:
        return None


class EpochAIModelsSource(BaseScrapeAdapter):
    """Keyless lookup over Epoch AI's notable-models scaling table."""

    name = "epoch_ai_models"
    description = (
        "Epoch AI notable-models dataset (keyless): training-scale facts for ~1000 "
        "significant ML models. Query a MODEL ('AlphaFold', 'GPT-3'), an ORG "
        "('DeepMind', 'Anthropic'), or a DOMAIN ('Biology', 'Games', 'Language') -> "
        "each model's parameters, training compute (FLOP), dataset size, training "
        "hardware, country, notability criteria, citations, accessibility + its "
        "paper link. The WHAT-SCALE behind the compute-scaling literature; pair with "
        "huggingface_hub / modelscope (weights) and arxiv (the paper). Term-less "
        "query returns the newest models."
    )
    needs_credentials = False
    explicit_only = False
    kind = "lookup"
    domains = ["models"]
    regions = ["global"]
    modes = ["STRUCTURE"]
    cache_ttl = 21600  # 6h per-query disk cache; the raw table is process-cached above
    rank = False       # _to_documents already BM25-filters + slices to limit

    # --------------------------------------------------------------- hooks
    def _raw_fetch(self, query: str, limit: int) -> Optional[list[dict]]:
        rows = _load_models()
        return rows or None  # [] -> None -> search returns [] (do not cache a miss)

    def _to_documents(self, raw: list[dict], query: str, limit: int) -> list[Document]:
        docs = [self._to_doc(r) for r in raw]
        # Empty / term-less query -> keep the newest-first order; real terms -> BM25.
        q = (query or "").strip()
        matched = docs if not q else keyword_score_filter(docs, q)
        return matched[:limit]

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """Native-async twin of ``BaseScrapeAdapter.search`` -> AsyncSearchCapable. Runs the base
        mechanism through ``_asearch_via`` (the search-result disk cache round-trip pushed OFF the
        loop, SAME cache key as ``search``), changing ONLY the egress: the inner ``afetch`` mirrors
        ``_raw_fetch`` (``rows or None``) but loads the process-cached table via the async
        ``_aload_models`` (raw ``httpx.get`` -> ``http.aget_text``) instead of the sync
        ``_load_models``. ``_to_documents`` (the BM25 filter + slice) is pure CPU, byte-identical,
        run on the loop by the helper. BEHAVIOR-IDENTICAL to ``search``: same cache key + TTL, same
        failure->[] (uncached) contract, same newest-first / BM25 ordering."""
        async def afetch() -> Optional[list[dict]]:
            rows = await _aload_models()
            return rows or None  # [] -> None -> search returns [] (do not cache a miss)

        return await self._asearch_via(
            query, limit, afetch,
            lambda raw: self._to_documents(raw, query, limit))

    def fetch_url(self, url: str) -> Optional[Document]:
        # Bulk-table lookup source: nothing meaningful to fetch by a single URL.
        return None

    def health_check(self) -> tuple[bool, str]:
        # Cheap liveness: HEAD the CSV, never parse the whole table on a health poll.
        try:
            resp = httpx.head(CSV_URL, headers={"User-Agent": USER_AGENT},
                              timeout=10, follow_redirects=True)
            return resp.status_code == 200, f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    # --------------------------------------------------------------- build
    @staticmethod
    def _to_doc(r: dict) -> Document:
        model = r["Model"]
        org = r["Organization"]
        date_s = r["Publication date"]

        # Human-readable content: the populated scaling fields, one per line.
        label = {
            "Organization": org,
            "Publication date": date_s,
            "Domain": r["Domain"],
            "Task": r["Task"],
            "Parameters": r["Parameters"],
            "Training compute (FLOP)": r["Training compute (FLOP)"],
            "Training dataset size (total)": r["Training dataset size (total)"],
            "Training hardware": r["Training hardware"],
            "Country": r["Country (of organization)"],
            "Notability criteria": r["Notability criteria"],
            "Citations": r["Citations"],
            "Accessibility": r["Model accessibility"],
            "Base model": r["Base model"],
            "Open weights": r["Open model weights?"],
        }
        content = "\n".join(f"{k}: {v}" for k, v in label.items() if v)

        title = f"{model} - {org}" if org else model
        if date_s:
            title += f" ({date_s[:4]})"

        tags = [t for t in [org] + [d.strip() for d in r["Domain"].split(",")] if t]
        tags.append("ai-model")

        return Document(
            source="epoch_ai_models",
            source_id=f"{model}|{date_s}",
            url=r["Link"] or DATASET_PAGE,
            title=title,
            content=content or model,
            author=org or None,
            date=_parse_date(date_s),
            tags=tags,
            metadata={
                "model": model,
                "organization": org,
                "publication_date": date_s,
                "domain": r["Domain"],
                "task": r["Task"],
                "parameters": r["Parameters"],
                "training_compute_flop": r["Training compute (FLOP)"],
                "training_dataset_size": r["Training dataset size (total)"],
                "training_hardware": r["Training hardware"],
                "country": r["Country (of organization)"],
                "notability_criteria": r["Notability criteria"],
                "citations": r["Citations"],
                "accessibility": r["Model accessibility"],
                "base_model": r["Base model"],
                "open_weights": r["Open model weights?"],
            },
        )
