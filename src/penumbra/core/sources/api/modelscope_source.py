"""ModelScope (魔搭) — the Chinese model hub, via the keyless Hub API.

ModelScope is Alibaba's model/dataset hub: the China-side analog of HuggingFace Hub, indexing
the Chinese AI ecosystem (Qwen, iic/FunASR, DeepSeek mirrors, Chinese-language models) that HF
does NOT surface. Penumbra's models-STRUCTURE reinforcement: a second, disjoint model hub so
the "what models exist + adoption" facet no longer rests on huggingface_hub alone, with a
distinct China-AI cut (bilingual task taxonomy, download counts on the China side).

Access via the public Hub API (no token for read):
  PUT https://www.modelscope.cn/api/v1/models   (no trailing slash; '/models/' 307-redirects)
  body {"PageNumber": 1, "PageSize": <n>, "SortBy": "DownloadsCount", "Name": "<q>"}
  -> {"Code": 200, "Data": {"TotalCount": N, "Models": [{Name, Path, ChineseName, Downloads,
       License, Tasks: [{Name, ChineseName, ...}], Organization, LastUpdatedTime, ...}, ...]}}
The model listing is a PUT with a JSON body (an API quirk), so this is a coded adapter on the
``http.put_json`` helper. The model-page url is constructed from Path + Name.

backend="modelscope" (its own upstream). explicit_only: a named model drill.


"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from penumbra.core import http
from penumbra.core.normalize import Document, jsonsafe, mk_signal
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

API_URL = "https://www.modelscope.cn/api/v1/models"  # no trailing slash (avoids the 307)
MODEL_URL = "https://www.modelscope.cn/models/{path}/{name}"


class ModelScopeAdapter(BaseScrapeAdapter):
    name = "modelscope"
    needs_credentials = False
    description = ("ModelScope (魔搭) — the Chinese model hub (Alibaba): name a model / keyword to "
                   "search China-ecosystem models (Qwen / iic / DeepSeek) by download count, with "
                   "task taxonomy + license + lineage. The China-side analog of huggingface_hub. "
                   "STRUCTURE, keyless.")
    cache_ttl = 21600  # 6h: model listings change slowly
    kind = "lookup"
    domains = ["models"]
    modes = ["STRUCTURE"]
    explicit_only = ("modelscope: a named China-model drill (search the ModelScope hub); not "
                     "broad-fan-out fodder")

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        body: dict[str, Any] = {
            "PageNumber": 1,
            "PageSize": max(1, min(int(limit), 50)),
            "SortBy": "DownloadsCount",
        }
        if query and query.strip():
            body["Name"] = query.strip()
        return http.put_json(API_URL, json=body, timeout=20)

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        if not isinstance(raw, dict):
            return []
        models = ((raw.get("Data") or {}).get("Models")) or []
        docs: list[Document] = []
        for model in models[:limit]:
            doc = self._model_to_doc(model)
            if doc is not None:
                docs.append(doc)
        return docs

    def _model_to_doc(self, model: Any) -> Optional[Document]:
        if not isinstance(model, dict):
            return None
        name = model.get("Name")
        path = model.get("Path")  # the owner / namespace
        if not name or not path:
            return None  # no owner/name -> no canonical model page
        cn = model.get("ChineseName")
        title = cn or name
        tasks = self._tasks(model.get("Tasks"))
        org = self._org_name(model.get("Organization"))  # Organization is a dict, not a str
        bits = [f"{path}/{name}"]
        if tasks:
            bits.append("Tasks: " + ", ".join(tasks))
        if model.get("License"):
            bits.append(f"License: {model.get('License')}")
        return Document(
            source=self.name,
            source_id=f"{path}/{name}",
            url=MODEL_URL.format(path=path, name=name),
            title=title,
            content=" | ".join(bits),
            author=org or path,
            date=self._parse_epoch(model.get("LastUpdatedTime")),
            signals=self._downloads_signal(model.get("Downloads")),
            tags=tasks,
            metadata={
                "owner": path,
                "chinese_name": cn,
                "downloads": model.get("Downloads"),
                "license": model.get("License"),
                "organization": org,
                "tasks": tasks,
                "raw": jsonsafe(model),
            },
        )

    @staticmethod
    def _org_name(org: Any) -> Optional[str]:
        """Organization is a dict ({Name, FullName, Path, ...}) on this API, NOT a bare string.
        Pluck a human label (FullName '千问' / Name 'Qwen'); tolerate a plain string or None.
        Passing the raw dict as a Document field raises (author/source must be str)."""
        if isinstance(org, dict):
            for k in ("FullName", "Name", "Path"):
                v = org.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return None
        if isinstance(org, str) and org.strip():
            return org.strip()
        return None

    @staticmethod
    def _tasks(tasks: Any) -> list[str]:
        """Tasks is a list of {Name, ChineseName, ...} objects (or strings). Flatten to names."""
        out: list[str] = []
        if not isinstance(tasks, list):
            return out
        for t in tasks:
            if isinstance(t, dict):
                nm = t.get("Name") or t.get("ChineseName")
                if isinstance(nm, str) and nm.strip():
                    out.append(nm.strip())
            elif isinstance(t, str) and t.strip():
                out.append(t.strip())
        return out

    @staticmethod
    def _downloads_signal(dl: Any) -> dict:
        """Download count -> an engagement-class signal (model adoption). Absent/non-numeric -> {}."""
        if isinstance(dl, (int, float)) and not isinstance(dl, bool):
            return mk_signal("downloads", dl, kind="engagement", by="modelscope/Downloads")
        return {}

    @staticmethod
    def _parse_epoch(ts: Any) -> Optional[datetime]:
        """LastUpdatedTime is a Unix timestamp (seconds, sometimes ms). None on anything else."""
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            return None
        try:
            secs = ts / 1000 if ts > 1e12 else ts  # tolerate ms
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None

    def health_check(self) -> tuple[bool, str]:
        try:
            raw = http.put_json(API_URL, json={"PageNumber": 1, "PageSize": 1, "SortBy": "DownloadsCount"}, timeout=15)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"
        ok = isinstance(raw, dict) and isinstance(raw.get("Data"), dict)
        return ok, "OK" if ok else "no Data envelope"

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
