"""Academia Stack Exchange — academic Q&A via Stack Exchange API.

academia.stackexchange.com is the highest-signal English Q&A site for
PhD / postdoc / faculty career methodology — structured, peer-curated,
and searchable. Sub-agent reflection identified it as Penumbra's
largest English meta-community gap.

Access via the public Stack Exchange v2.3 API (no auth required for
read; 10000 quota/day per IP — far more than typical search use).

Docs: https://api.stackexchange.com/docs
Key endpoints:
- /2.3/search/advanced?site=academia&q=...                 — full-text search
- /2.3/questions/{ids}?site=academia                        — fetch by question id
- /2.3/questions/{id}/answers?site=academia&sort=votes&...  — the votes-ranked ANSWERS (the gold)

Response: gzipped JSON. httpx handles decompression automatically.

Now a thin subclass over the shared SE machinery in ``_stackexchange.py``: the cache check /
atomic set_docs / self-registration ritual lives in BaseScrapeAdapter; the SE-API I/O + the
question→doc / answer→doc map live in ``_stackexchange`` (so all six SE sources share the
answer-fetching gold). This adapter just declares its site/facets and wires the hooks.

Each returned question yields its own doc PLUS its top-3 votes-ranked answer docs (source_id
"{qid}a{aid}", title "A: <title>", metadata.is_accepted) — the accepted answer is the actual gold,
which the old question-body-only mapping silently dropped. rank stays default-False because the
search endpoint already returns server-relevance order (sort=relevance).
"""

from __future__ import annotations

from typing import Any, Optional

from penumbra.core import _stackexchange
from penumbra.core.normalize import Document
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

SITE = "academia"
SITE_HOST = "academia.stackexchange.com"


class AcademiaSEAdapter(BaseScrapeAdapter):
    name = "academia_se"
    needs_credentials = False
    description = "Academia Stack Exchange — English PhD/postdoc/faculty Q&A (Stack Exchange API)"
    cache_ttl = 900
    kind = "lookup"
    domains = ["community", "methodology"]
    modes = ["STRUCTURE"]

    def _raw_fetch(self, query: str, limit: int) -> Optional[Any]:
        return _stackexchange.search(query, limit, SITE)

    def _to_documents(self, raw: Any, query: str, limit: int) -> list[Document]:
        return _stackexchange.build_documents(raw, limit, self.name, SITE, SITE_HOST)

    def fetch_url(self, url: str) -> Optional[Document]:
        return _stackexchange.fetch_question_document(url, self.name, SITE, SITE_HOST)

    def health_check(self) -> tuple[bool, str]:
        # Shared single-flight probe (see _stackexchange.health): all SE sources share the
        # api.stackexchange.com keyless per-IP quota, so they delegate to ONE 60s-cached probe
        # instead of each firing its own live /questions GET on every health sweep.
        return _stackexchange.health()

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
