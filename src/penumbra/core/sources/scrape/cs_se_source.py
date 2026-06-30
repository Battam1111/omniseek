"""Computer Science Stack Exchange — CS theory Q&A via the Stack Exchange API.

cs.stackexchange.com is the peer-curated Q&A site for the THEORY side of computer science:
algorithms + complexity, computability, formal languages + automata, data structures, graph
theory, the math behind ML. Distinct from Stack Overflow (which is programming/debugging): this is
the "why does this algorithm work / what is its complexity" site, where the gold is a careful
votes-ranked proof-shaped answer.

Same keyless Stack Exchange v2.3 public API as the other SE sources (site=cs), sharing the
10000/day-per-IP quota and the shared answer-fetching machinery in ``_stackexchange.py``. Each
returned question ships its own doc PLUS its top-3 votes-ranked answer docs (the gold).
"""

from __future__ import annotations

from typing import Any, Optional

from penumbra.core import _stackexchange
from penumbra.core.normalize import Document
from penumbra.core.sources.scrape._base import BaseScrapeAdapter

SITE = "cs"
SITE_HOST = "cs.stackexchange.com"


class CSStackExchangeAdapter(BaseScrapeAdapter):
    name = "cs_se"
    needs_credentials = False
    description = (
        "Computer Science Stack Exchange — CS-theory Q&A (algorithms, complexity, "
        "computability, automata, the math behind ML); not Stack Overflow's programming/debugging"
    )
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
        # Shared single-flight probe — all SE sources share the keyless per-IP quota.
        return _stackexchange.health()

# Registration is automatic via BaseScrapeAdapter.__init_subclass__ (no module-tail ceremony).
