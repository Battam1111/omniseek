"""GPU pricing — cross-cloud GPU $/hr lookup (keyless, structured).

For a compute-bound PhD renting GPUs while grant credits are pending: "what is the
cheapest H100 / A100 / 4090 right now, across clouds?" — a structured price
comparison the open web can't return cleanly on demand. Source: the Full Stack
Deep Learning cloud-gpus dataset (raw CSV on GitHub, community-maintained),
tabulating Cloud / GPU Type / on-demand + spot $/hr across AWS/GCP/Azure/Lambda/
RunPod/Vast/CoreWeave/etc. Pair with reference/compute-access-map.md (free credits
to apply for NOW) and the vast.ai/runpod APIs (live spot) for depth.

Query a GPU type ("H100", "A100", "4090") and/or a cloud ("lambda", "runpod") →
matching offerings sorted cheapest-first. Snapshot data (updates via repo PRs),
not real-time. Fetched once per process.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Optional

import httpx

from penumbra.core.normalize import PolarisDocument, mk_signal

logger = logging.getLogger(__name__)

CSV_URL = "https://raw.githubusercontent.com/the-full-stack/website/main/docs/cloud-gpus/cloud-gpus.csv"
TIMEOUT = 20
USER_AGENT = "polaris-eye/0.1 (automated retrieval)"

_ROWS: Optional[list[dict]] = None


def _price(s) -> Optional[float]:
    try:
        return float(str(s).replace("$", "").replace(",", "").strip())
    except (ValueError, AttributeError, TypeError):
        return None


def _load() -> list[dict]:
    global _ROWS
    if _ROWS is not None:
        return _ROWS
    try:
        resp = httpx.get(CSV_URL, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("gpu_pricing: CSV fetch failed: %s", exc)
        return []
    rows = [{k: (v or "").strip() for k, v in r.items()}
            for r in csv.DictReader(io.StringIO(resp.text))]
    _ROWS = rows
    logger.info("gpu_pricing: loaded %d offerings", len(rows))
    return rows


class GPUPricingAdapter:
    name = "gpu_pricing"
    needs_credentials = False
    kind = "lookup"
    # compute/STRUCTURE: shares the compute-cost cell with vast_ai (the live marketplace),
    # so neither is a coverage islet (2026-06-21).
    domains = ["compute"]
    modes = ["STRUCTURE"]
    explicit_only = "GPU price lookup (named lookup only)"
    description = (
        "GPU 价格对比 — 跨云 GPU $/hr 结构化查询 (keyless, Full Stack DL cloud-gpus 数据). "
        "compute-bound 时'现在最便宜的 H100/A100/4090 在哪家云'. 查 GPU 型号 (H100/A100/4090) "
        "和/或云 (lambda/runpod/vast) → 报价按便宜优先. 快照非实时; 实时 spot 见 vast.ai/runpod API. "
        "配 reference/compute-access-map.md (现在能申的免费额度)."
    )

    def search(self, query: str, limit: int = 12) -> list[PolarisDocument]:
        rows = _load()
        if not rows:
            return []
        terms = [t for t in (query or "").strip().lower().split() if t]

        def match(r: dict) -> bool:
            blob = " ".join((r.get(k, "") or "") for k in
                            ("Cloud", "GPU Type", "GPU Arch", "Name")).lower()
            return all(t in blob for t in terms)

        sel = [r for r in rows if match(r)] if terms else list(rows)

        def keyf(r: dict):
            return (_price(r.get("Per-GPU")) or _price(r.get("On-demand"))
                    or _price(r.get("Spot")) or 1e9)

        sel.sort(key=keyf)
        return [d for d in (self._to_doc(r) for r in sel[:limit]) if d]

    def fetch_url(self, url: str) -> Optional[PolarisDocument]:
        return None

    def health_check(self) -> tuple[bool, str]:
        try:
            resp = httpx.head(CSV_URL, headers={"User-Agent": USER_AGENT},
                              timeout=10, follow_redirects=True)
            return resp.status_code == 200, f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _to_doc(r: dict) -> Optional[PolarisDocument]:
        cloud, gpu = r.get("Cloud", ""), r.get("GPU Type", "")
        if not cloud and not gpu:
            return None
        od, per, spot = r.get("On-demand", ""), r.get("Per-GPU", ""), r.get("Spot", "")
        n, ram = r.get("GPUs", ""), r.get("GPU RAM", "")
        title = f"{gpu} x{n or '?'} on {cloud}: {per or od or spot or '?'} /GPU·hr"
        parts = [
            f"Cloud: {cloud}",
            f"GPU: {gpu} ({r.get('GPU Arch', '')}) x{n}, {ram} VRAM",
            f"On-demand: {od}  ·  Per-GPU: {per}  ·  Spot: {spot}",
            f"vCPUs: {r.get('vCPUs', '')}  ·  RAM: {r.get('RAM', '')}",
            f"Instance: {r.get('Name', '')}",
        ]
        # Source-reported fact (not a judgment): the effective on-demand $/GPU-hr, same priority
        # the rows are sorted by (Per-GPU, else On-demand, else Spot). kind="other" + unit mirrors
        # sec_financials/market_quote so the price is a first-class signal, not buried in prose.
        price = _price(per) or _price(od) or _price(spot)
        signals = mk_signal("price_per_gpu_hr", price, kind="other",
                            by="cloud-gpus/on-demand", unit="USD")
        return PolarisDocument(
            source="gpu_pricing",
            source_id=f"{cloud}|{gpu}|{r.get('Name', '')}",
            url="https://cloud-gpus.com/",
            title=title,
            content="\n".join(parts),
            signals=signals,
            tags=[cloud, gpu, "gpu-price"],
            metadata={k: r.get(k) for k in
                      ("Cloud", "GPU Type", "On-demand", "Per-GPU", "Spot", "Name")},
        )


from penumbra.core.fetcher import register_adapter

register_adapter(GPUPricingAdapter())
