"""Declarative REST/JSON sources — discovery entry point.

This is the thin ``*_source.py`` whose only job is to be found by the
``walk_packages`` auto-discovery in ``omniseek.server`` and trigger registration of
every row in ``sources.json``. All mechanism lives in ``_declarative.py`` (which does
NOT end in ``_source`` and so stays inert until imported here) — exactly the two-part
shape ``rss_bundles_source.py`` + ``scrape/_rss.py`` already uses.

Adding a standard REST/JSON source = one row in ``sources.json``. Zero edits here.
"""

from __future__ import annotations

from omniseek.core.sources._declarative import load_declarative_sources

_REGISTERED = load_declarative_sources()
