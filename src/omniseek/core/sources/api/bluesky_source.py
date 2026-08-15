"""Bluesky adapter — uses the atproto Python package.

Reads credentials from ~/.omniseek/credentials/bluesky.json:
    {"handle": "name.bsky.social", "app_password": "xxxx-xxxx-xxxx-xxxx"}

App passwords are generated at https://bsky.app/settings/app-passwords
(they are scoped, revocable, and don't expose your main password).

AT Protocol is the best-documented social API among all the platforms
in our OmniSeek stack — 5000 points/hour, 35000/day, no payment required.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from atproto import Client

from omniseek.core import auth, cache
from omniseek.core.normalize import Document, jsonsafe, mk_signal

logger = logging.getLogger(__name__)

# Template dropped on first import for user discoverability
auth.write_template(
    "bluesky",
    {
        "handle": "your-handle.bsky.social",
        "app_password": "xxxx-xxxx-xxxx-xxxx",
        "_help": "App passwords at https://bsky.app/settings/app-passwords",
    },
)


def _image_media(post) -> list[str]:
    """Collect full-resolution image URLs from a post's embed (if any).

    Bluesky carries images on ``PostView.embed`` as an ``app.bsky.embed.images#view``
    (``.images[].fullsize``); a post with both images and a quoted record uses
    ``app.bsky.embed.recordWithMedia#view`` whose ``.media`` is that images view.
    Other embed kinds (external link cards, bare quoted records, video) have no
    inline image URL we surface here. Best-effort + duck-typed — never raises.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(u) -> None:
        if isinstance(u, str) and u.startswith("http") and u not in seen:
            seen.add(u)
            out.append(u)

    embed = getattr(post, "embed", None)
    if embed is None:
        return out
    # recordWithMedia#view nests the media view under .media
    media_view = getattr(embed, "media", None)
    images = getattr(media_view, "images", None) or getattr(embed, "images", None)
    for img in images or []:
        _add(getattr(img, "fullsize", None) or getattr(img, "thumb", None))
    return out


class BlueskyAdapter:
    name = "bluesky"
    needs_credentials = True
    description = "Bluesky — academic Twitter migration target, AT Protocol open API"

    _client: Optional[Client] = None
    _logged_in: bool = False

    def _ensure_client(self) -> Optional[Client]:
        if self._client is None:
            creds = auth.load("bluesky")
            if not creds or not creds.get("handle") or not creds.get("app_password"):
                logger.info("Bluesky credentials not configured.")
                return None
            try:
                self._client = Client()
                self._client.login(creds["handle"], creds["app_password"])
                self._logged_in = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("Bluesky login failed: %s", exc)
                self._client = None
                return None
        return self._client

    def search(self, query: str, limit: int = 10) -> list[Document]:
        client = self._ensure_client()
        if client is None:
            return []

        key = cache.make_key("bluesky", "search", query, limit)
        cached = cache.get(key)
        if cached is not None:
            return [Document.model_validate(d) for d in cached]

        try:
            response = client.app.bsky.feed.search_posts(
                {"q": query, "limit": min(limit, 100)}
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bluesky search failed: %s", exc)
            return []

        docs: list[Document] = []
        for post in (response.posts or [])[:limit]:
            try:
                docs.append(self._post_to_document(post))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping malformed Bluesky post: %s", exc)

        # An auth-lapse / outage empty must not pin [] for 15m (masks the outage); genuine empties
        # self-heal in 5m. bluesky is credentialed, so a lapsed session is the likely empty here.
        cache.set(key, [d.model_dump(mode="json") for d in docs], ttl=900 if docs else 300)
        return docs

    def fetch_url(self, url: str) -> Optional[Document]:
        host = urlparse(url).hostname or ""
        if "bsky.app" not in host and "bsky.social" not in host:
            return None
        # Pattern: bsky.app/profile/<handle>/post/<rkey>
        parts = urlparse(url).path.strip("/").split("/")
        if len(parts) < 4 or parts[0] != "profile" or parts[2] != "post":
            return None
        handle = parts[1]
        rkey = parts[3]
        client = self._ensure_client()
        if client is None:
            return None
        try:
            profile = client.get_profile(handle)
            at_uri = f"at://{profile.did}/app.bsky.feed.post/{rkey}"
            thread = client.get_post_thread(at_uri)
            return self._post_to_document(thread.thread.post)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bluesky fetch_url failed: %s", exc)
            return None

    def health_check(self) -> tuple[bool, str]:
        if not auth.is_configured("bluesky"):
            return False, "credentials not configured (see ~/.omniseek/credentials/bluesky.json.template)"
        client = self._ensure_client()
        if client is None:
            return False, "login failed"
        return True, "OK (logged in)"

    @staticmethod
    def _post_to_document(post) -> Document:
        record = post.record
        author = post.author
        text = getattr(record, "text", "") or ""
        created_at = getattr(record, "created_at", None)
        date = None
        if created_at:
            try:
                date = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                date = None

        # Construct canonical Bluesky URL
        rkey = post.uri.rsplit("/", 1)[-1] if post.uri else ""
        url = f"https://bsky.app/profile/{author.handle}/post/{rkey}"

        return Document(
            source="bluesky",
            source_id=post.uri or "",
            url=url,
            title=text[:80] + ("..." if len(text) > 80 else ""),
            content=text,
            author=f"@{author.handle}" + (f" ({author.display_name})" if author.display_name else ""),
            date=date,
            signals=mk_signal("likes", getattr(post, "like_count", None),
                              kind="engagement", by="bluesky/like_count"),
            media=_image_media(post),
            metadata={
                "author_did": author.did,
                "reply_count": getattr(post, "reply_count", None),
                "repost_count": getattr(post, "repost_count", None),
                "like_count": getattr(post, "like_count", None),
                "indexed_at": getattr(post, "indexed_at", None),
                "raw": jsonsafe(post),  # atproto PostView, dict-ified
            },
        )


from omniseek.core.fetcher import register_adapter

register_adapter(BlueskyAdapter())
