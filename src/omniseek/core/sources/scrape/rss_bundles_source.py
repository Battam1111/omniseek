"""RSS-bundle adapters — 24 pure feed-list sources, one data table.

P16 refactor (2026-05-30). 24 adapters whose ONLY unique content was
(name, description, feeds, cache_ttl, url_pattern) — each a ~50-line file with a
class body + a register_adapter() call that were pure boilerplate — are now rows
in ``rss_bundles.json``, registered by the single loop below. Adding an
RSS-bundle source is now a one-line JSON edit; no new file, class, or server
edit. Adapters with real custom logic (``pypi``, ``fellowships``) keep their own
files and still subclass RSSAdapterBase.

Each row becomes a configured ``RSSAdapterBase`` instance, so search / fetch_url
/ health_check / caching / keyword scoring all come from the shared base
unchanged — behavior is byte-identical to the former per-file classes (verified
against the registry baseline). The JSON was generated FROM the live adapters,
so descriptions/feeds/TTLs/url_patterns carried over verbatim.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from omniseek.core.fetcher import register_adapter
from omniseek.core.sources.scrape._rss import RSSAdapterBase

logger = logging.getLogger(__name__)

_DATA = Path(__file__).with_name("rss_bundles.json")


class _RSSBundle(RSSAdapterBase):
    # A bundle's health probe walks EVERY member feed, so its honest duration scales with the
    # bundle size, not with any one host. Measured 2026-08-19 against the live feeds:
    # rl_llm_frameworks 65.6s, github_releases 40.6s, both answering healthy against a 25s
    # default cap, so every run recorded them as failures and the broad sweep skipped them.
    # Declared per-source (fetcher._probe_all_health) so only bundles pay this, not all ~220.
    health_timeout_s = 90
    """An RSSAdapterBase configured from a data row, not a hand-written class."""

    def __init__(self, name: str, description: str, feeds: list[str],
                 cache_ttl: int = 1800, url_pattern: Optional[str] = None,
                 explicit_only=False, guard_ip: bool = False,
                 tls_impersonate: bool = False) -> None:
        self.name = name
        self.description = description
        self.feeds = feeds
        self.cache_ttl = cache_ttl
        self.url_pattern = url_pattern
        self.explicit_only = explicit_only  # row-declared: True / reason string / absent
        self.guard_ip = guard_ip  # True for curator overlay-origin rows: per-fetch SSRF IP guard
        self.tls_impersonate = tls_impersonate  # opt-in curl_cffi fetch (JA3-walled host); base rows only


def _register_row(b: dict, guard_ip: bool = False) -> None:
    register_adapter(_RSSBundle(
        name=b["name"],
        description=b["description"],
        feeds=b["feeds"],
        cache_ttl=b.get("cache_ttl", 1800),
        url_pattern=b.get("url_pattern"),
        explicit_only=b.get("explicit_only", False),
        guard_ip=guard_ip,
        # TLS-impersonate is a privileged, more-evasive fetch tier: honor it ONLY for trusted
        # in-tree BASE rows (guard_ip=False), never for an agent-admitted overlay row (guard_ip=True).
        tls_impersonate=bool(b.get("tls_impersonate", False)) and not guard_ip,
    ))


def _load_and_register() -> list[str]:
    """Register the in-tree base rows, THEN the curator live-apply overlay rows (base wins on a
    name clash: a deploy that promoted an overlay row into the tree always wins; an overlay row can
    never collide-replace a base adapter). Each overlay row is TYPED-validated and a bad one is
    dropped + logged, never registered (a hand-edited/fuzzed overlay can't crash boot or register
    junk). The overlay file lives outside the deploy tree; absent overlay -> base-only (unchanged)."""
    registered: list[str] = []
    base = json.loads(_DATA.read_text(encoding="utf-8"))
    for b in base:
        _register_row(b)
        registered.append(b["name"])
    seen = set(registered)
    try:
        from omniseek.core.curator import apply as _apply
        from omniseek.core.curator import apply_live as _apply_live
        for r in _apply_live.overlay_rows("rss"):
            name = r.get("name")
            if name in seen:
                continue  # base wins
            problems = _apply.validate_row_typed("rss", r)
            if problems:
                logger.warning("rss overlay row %r dropped (invalid): %s", name, problems)
                continue
            _register_row(r, guard_ip=True)  # overlay-origin (agent-admitted, never human-vetted)
            registered.append(name)
            seen.add(name)
    except Exception as exc:  # noqa: BLE001, the overlay is best-effort; base must always load
        logger.warning("rss overlay load skipped: %s", exc)
    return registered


_REGISTERED = _load_and_register()
