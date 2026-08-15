"""PyPI — Python Package Index updates RSS.

Tracks recent ML/AI package releases. PyPI's `/rss/updates.xml` carries the
last ~40 package version updates across the entire registry — querying
with ML-related keywords surfaces transformers / pytorch / datasets / etc.
releases.

Alternative feeds (not used here):
- `/rss/packages.xml` — newest packages (first-ever release), lower SNR
- `/pypi/<name>/json` — per-package JSON, used in fetch_url path

This adapter is a TRACK of ecosystem motion. For specific package lookup
(version, dependencies, author), use fetch_url with a pypi.org/project/<name>
URL.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import httpx

from omniseek.core import diag
from omniseek.core.normalize import Document, jsonsafe
from omniseek.core.sources.scrape._rss import RSSAdapterBase

logger = logging.getLogger(__name__)

TIMEOUT = 15
USER_AGENT = "omniseek/0.1 (automated retrieval)"


class PyPIAdapter(RSSAdapterBase):
    name = "pypi"
    description = "PyPI — Python package update stream (recent releases across all packages)"
    feeds = ["https://pypi.org/rss/updates.xml"]
    url_pattern = r"pypi\.org"
    cache_ttl = 1800  # 30 min — PyPI updates frequently

    def fetch_url(self, url: str) -> Optional[Document]:
        # Pattern: pypi.org/project/<name>/  or  pypi.org/project/<name>/<version>/
        host = (urlparse(url).hostname or "").lower()
        if "pypi.org" not in host:
            return None
        path = urlparse(url).path.strip("/").split("/")
        if len(path) < 2 or path[0] != "project":
            return None
        package_name = path[1]
        try:
            resp = httpx.get(
                f"https://pypi.org/pypi/{package_name}/json",
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("PyPI fetch_url failed (%s): %s", package_name, exc)
            st = getattr(getattr(exc, "response", None), "status_code", None)
            diag.note("pypi.fetch", url=f"https://pypi.org/pypi/{package_name}/json", status=st, exc=exc)
            return None

        info = data.get("info") or {}
        latest_version = info.get("version") or ""
        title = f"{package_name} {latest_version}".strip()
        summary = info.get("summary") or info.get("description") or ""
        author = info.get("author") or info.get("author_email") or info.get("maintainer")
        home = info.get("home_page") or info.get("package_url") or f"https://pypi.org/project/{package_name}/"

        # Latest release date
        releases = data.get("releases") or {}
        date = None
        if latest_version and latest_version in releases:
            files = releases[latest_version]
            if isinstance(files, list) and files:
                upload_time = files[0].get("upload_time_iso_8601") or files[0].get("upload_time")
                if upload_time:
                    try:
                        date = datetime.fromisoformat(upload_time.replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        pass

        return Document(
            source="pypi",
            source_id=package_name,
            url=home,
            title=title,
            content=summary or "(no summary)",
            author=author,
            date=date,
            tags=info.get("keywords", "").split(",")[:10] if info.get("keywords") else [],
            metadata={
                "package": package_name,
                "version": latest_version,
                "homepage": info.get("home_page"),
                "license": info.get("license"),
                "requires_python": info.get("requires_python"),
                "raw": jsonsafe(data),
            },
        )


from omniseek.core.fetcher import register_adapter

register_adapter(PyPIAdapter())
