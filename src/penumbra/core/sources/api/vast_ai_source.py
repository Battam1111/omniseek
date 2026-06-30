"""Vast.ai — live GPU rental marketplace (spot + on-demand prices) via the keyless v0 API.

Vast.ai is a real-time GPU rental order book: per-offer on-demand and interruptible (spot)
prices, performance, perf-per-dollar, location and reliability. Penumbra's compute-cost
STRUCTURE source: the LIVE marketplace depth web search cannot give (it returns vendor pages
and stale aggregator estimates, never the current order book). Reinforces gpu_pricing, whose
own description noted that live spot pricing via the vast.ai / runpod API was not yet wired:
this is the wire.

Access via the public v0 console API (no auth, no key; the v1 cloud.vast.ai WRITE API needs a
key, this READ endpoint does not):
  GET https://console.vast.ai/api/v0/bundles?q=<json-filter>
  -> {"offers": [{id, gpu_name, num_gpus, gpu_ram, dph_total, min_bid, dlperf,
       dlperf_per_dphtotal, geolocation, reliability2, rentable, ...}, ...]}
The ``q`` param is a JSON filter object ({field:{op:value}} + an order array), so this is a
coded adapter. Query convention: pass an EXACT vast canonical GPU label to filter
("RTX 4090", "H100 SXM", "A100 PCIE"; note "RTX_4090" returns empty); a bare query lists the
cheapest offers across all GPUs. Offers are ranked cheapest-first (dph_total asc).

Offers are ephemeral marketplace rows, so the canonical url is the marketplace itself; the
value is the structured price/perf in each doc. explicit_only: a named compute-price drill.


"""

from __future__ import annotations

import json as _json
from typing import Any, Optional

from penumbra.core import http
from penumbra.core.normalize import Document, jsonsafe, mk_signal
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

API_URL = "https://console.vast.ai/api/v0/bundles"
MARKET_URL = "https://cloud.vast.ai/"
# vast canonical GPU labels are like "RTX 4090" (space, not underscore); a query that is plainly
# a GPU model is sent as an exact gpu_name filter.


class VastAIAdapter(BaseScrapeAdapter):
    name = "vast_ai"
    needs_credentials = False
    description = ("Vast.ai — LIVE GPU rental marketplace: per-offer on-demand + spot ($/hr), "
                   "perf-per-dollar, GPU RAM, location, reliability; name it with an EXACT GPU "
                   "label ('RTX 4090', 'H100 SXM') to price a model, or bare to list the cheapest "
                   "offers. STRUCTURE, keyless; the live compute-cost order book gpu_pricing's "
                   "static rates lack.")
    cache_ttl = 1800  # 30m: spot prices move, but not by the second
    kind = "lookup"
    domains = ["compute"]
    modes = ["STRUCTURE"]
    explicit_only = ("vast_ai: a named compute-price drill (price a GPU / list cheapest offers); "
                     "not broad-fan-out fodder")

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        # q is a JSON filter object: rank cheapest-first, and if the query is a GPU label,
        # filter by exact gpu_name. (vast's eq is exact: 'RTX 4090' works, 'RTX_4090' does not.)
        flt: dict[str, Any] = {"order": [["dph_total", "asc"]]}
        if query and query.strip():
            flt["gpu_name"] = {"eq": query.strip()}
        return http.get_json(API_URL, params={"q": _json.dumps(flt)}, timeout=20)

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, dict):
            return []
        offers = raw.get("offers") or []
        docs: list[Document] = []
        for offer in offers:
            if len(docs) >= limit:
                break
            if isinstance(offer, dict) and offer.get("rentable") is False:
                continue  # only show offers you can actually rent
            doc = self._offer_to_doc(offer)
            if doc is not None:
                docs.append(doc)
        return docs

    def _offer_to_doc(self, offer: Any) -> Optional[Document]:
        if not isinstance(offer, dict):
            return None
        oid = offer.get("id")
        gpu = offer.get("gpu_name")
        dph = offer.get("dph_total")
        if not oid or not gpu or not isinstance(dph, (int, float)):
            return None  # no id / model / on-demand price -> not a usable price row
        n = offer.get("num_gpus") or 1
        spot = offer.get("min_bid")
        title = f"{n}x {gpu} @ ${dph:.4f}/hr on-demand"
        if isinstance(spot, (int, float)):
            title += f" (spot ${spot:.4f})"
        loc = offer.get("geolocation")
        bits = [title]
        if offer.get("gpu_ram"):
            bits.append(f"{offer.get('gpu_ram')} MB VRAM")
        if loc:
            bits.append(str(loc))
        return Document(
            source=self.name,
            source_id=str(oid),
            url=MARKET_URL,  # offers are ephemeral; the marketplace is the canonical entry point
            title=title,
            content=" | ".join(bits),
            author=str(loc) if loc else None,
            date=None,
            signals=self._perf_signal(offer.get("dlperf_per_dphtotal")),
            tags=[t for t in (gpu, str(loc) if loc else None) if t],
            metadata={
                "gpu_name": gpu,
                "num_gpus": n,
                "on_demand_usd_hr": dph,
                "spot_usd_hr": spot,
                "gpu_ram_mb": offer.get("gpu_ram"),
                "dlperf": offer.get("dlperf"),
                "perf_per_dollar": offer.get("dlperf_per_dphtotal"),
                "reliability": offer.get("reliability2"),
                "geolocation": loc,
                "rentable": offer.get("rentable"),
                "raw": jsonsafe(offer),
            },
        )

    @staticmethod
    def _perf_signal(ppd: Any) -> dict:
        """Perf-per-dollar (dlperf / $/hr) -> an engagement-class signal (higher = better value).
        Absent/non-numeric -> {}."""
        if isinstance(ppd, (int, float)) and not isinstance(ppd, bool):
            return mk_signal("perf_per_dollar", ppd, kind="engagement", by="vast_ai/dlperf_per_dphtotal")
        return {}

    def health_check(self) -> tuple[bool, str]:
        try:
            raw = http.get_json(API_URL, params={"q": _json.dumps({"order": [["dph_total", "asc"]]})}, timeout=15)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"
        ok = isinstance(raw, dict) and isinstance(raw.get("offers"), list)
        return ok, "OK" if ok else "no offers envelope"

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
