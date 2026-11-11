"""Bluesky adapter — uses the atproto Python package.

Reads credentials from <credentials>/bluesky.json:
    {"handle": "name.bsky.social", "app_password": "xxxx-xxxx-xxxx-xxxx"}

App passwords are generated at https://bsky.app/settings/app-passwords
(they are scoped, revocable, and don't expose your main password).

AT Protocol is the best-documented social API among all the platforms
in our OmniSeek stack — 5000 points/hour, 35000/day, no payment required.
"""

from __future__ import annotations

import logging
import time
from typing import Optional
from urllib.parse import urlparse

from atproto import AsyncClient, models
from atproto_client.client.session import Session
from atproto_client.models.utils import get_model_as_dict, get_or_create

from omniseek.core import _probe, auth, cache, http, upstreams
from omniseek.core.normalize import Document, jsonsafe, mk_signal, to_utc
from omniseek import where
from omniseek.core.sources._base import SourceBase

logger = logging.getLogger(__name__)

# Template dropped on first import for user discoverability
auth.register_template(
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


# Health probe: the search path itself, for ONE post on a word that always has posts.
_PROBE_QUERY = "science"
_PROBE_HOST = "bsky.social"
_RELOGIN = object()   # _probe_search's answer when the session itself was refused


def _xrpc_url(pds_endpoint: str, nsid: str) -> str:
    """The XRPC endpoint of ``nsid`` on a PDS, the way atproto builds it (base url + /xrpc)."""
    base = (pds_endpoint or "").rstrip("/")
    if not base.endswith("/xrpc"):
        base += "/xrpc"
    return f"{base}/{nsid}"


def _search_params(query: str, limit: int) -> dict:
    """searchPosts query parameters exactly as atproto sends them (its Params model, lexicon defaults
    such as sort=latest included)."""
    return get_model_as_dict(get_or_create({"q": query, "limit": min(limit, 100)},
                                           models.AppBskyFeedSearchPosts.Params))


def _xrpc_response(exc):
    """The error response atproto attaches to its request exceptions (None for anything else)."""
    return getattr(exc, "response", None)


def _xrpc_status(exc) -> Optional[int]:
    st = getattr(_xrpc_response(exc), "status_code", None)
    return st if isinstance(st, int) else None


def _xrpc_error(exc) -> Optional[str]:
    content = getattr(_xrpc_response(exc), "content", None)
    return getattr(content, "error", None) or (content.get("error") if isinstance(content, dict) else None)


class BlueskyAdapter(SourceBase):
    name = "bluesky"
    needs_credentials = True
    description = "Bluesky: academic Twitter migration target, AT Protocol open API"

    _client: Optional[AsyncClient] = None
    _logged_in: bool = False
    _login_error: Optional[BaseException] = None   # why the last login failed, for health_check
    _request_s: Optional[float] = None   # the client's own timeout, for the gate's lease

    @staticmethod
    def _wire_progress(client: AsyncClient) -> Optional[float]:
        """atproto (0.0.65) sends through an httpx.AsyncClient it keeps at ``client.request._client``:
        give it the lease renewal by progress (the request sent, its headers and each block of its body
        renew the Bluesky gate's lease, design decisions of 2026-09-29 on section 17.5, item 2, and on
        review Q1) and read its timeout for the lease. None when
        the library keeps it elsewhere: then the permit has no lease and is held until given back."""
        try:
            hc = client.request._client
            hooks = dict(hc.event_hooks)
            for kind, extra in http.aprogress_hooks().items():   # sent, headers, every block of the body
                hooks[kind] = list(hooks.get(kind, [])) + extra
            hc.event_hooks = hooks
            return http._timeout_s(hc.timeout)
        except Exception:  # noqa: BLE001
            logger.warning("bluesky: atproto's http client is not where expected; no progress renewal")
            return None

    async def _aheld(self, fn, *args):
        """``await fn(*args)``, one call of atproto's async client, under the declared Bluesky gate
        (upstreams.json "bluesky": the PDS / entryway allows 3000 requests per 5 minutes per IP), in this
        task, so the client's progress hooks renew the gate's lease. Raises what the call raises, and
        ``UpstreamBusy`` (nothing sent) when the gate cannot admit it in time. A token refresh atproto
        sends before the call (``AsyncClient._invoke``) goes out under the same hold."""
        async with upstreams.ahold("bluesky", request_s=self._request_s):
            return await fn(*args)

    async def _aensure_client(self) -> Optional[AsyncClient]:
        if self._client is None:
            creds = auth.load("bluesky")
            if not creds or not creds.get("handle") or not creds.get("app_password"):
                logger.info("Bluesky credentials not configured.")
                return None
            self._login_error = None
            try:
                self._client = AsyncClient()
                self._request_s = self._wire_progress(self._client)
                # Every atproto call takes the declared Bluesky gate (upstreams.json "bluesky": the
                # PDS / entryway allows 3000 requests per 5 minutes per IP); UpstreamBusy is a failure.
                await self._aheld(self._client.login, creds["handle"], creds["app_password"])
                self._logged_in = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("Bluesky login failed: %s", exc)
                self._login_error = exc   # why, for health_check (a 429 or a busy gate is not "down")
                self._client = None
                return None
        return self._client

    async def asearch(self, query: str, limit: int = 10) -> list[Document]:
        """The search itself is one XRPC GET (searchPosts) sent with the kept session's access token
        through http.aget, under the declared Bluesky gate, and the answer is parsed into the atproto
        model the client's own ``search_posts`` returns. The session is the atproto client's own (login
        and token refresh are atproto's), so no second session exists to rotate tokens against it; its
        upkeep runs only when there is no session yet or its access token would end within one
        request."""
        session = await self._asession()
        if session is None:
            return []

        key = cache.make_key("bluesky", "search", query, limit)

        async def _aget() -> Optional[list[Document]]:
            try:
                async with upstreams.ahold("bluesky", request_s=self._request_s):
                    resp = await http.aget(
                        _xrpc_url(session.pds_endpoint, "app.bsky.feed.searchPosts"),
                        params=_search_params(query, limit),
                        headers={"Authorization": f"Bearer {session.access_jwt}"},
                        timeout=self._request_s or http.DEFAULT_TIMEOUT)
                if resp is None:
                    raise RuntimeError("searchPosts failed (the http diagnostic has the answer)")
                response = get_or_create(resp.json(), models.AppBskyFeedSearchPosts.Response, strict=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Bluesky search failed: %s", exc)
                return None  # a failure: not cached

            docs = self._posts_to_documents(response, limit)
            return docs

        return await self._acached(key, lambda docs: 900 if docs else 300, _aget)

    def _posts_to_documents(self, response, limit: int) -> list[Document]:
        docs: list[Document] = []
        for post in (response.posts or [])[:limit]:
            try:
                docs.append(self._post_to_document(post))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping malformed Bluesky post: %s", exc)
        return docs

    def _live_session(self) -> Optional[Session]:
        """The kept session when its access token outlives one more request; None when there is no
        session yet or the token is at its end (then atproto must refresh it first)."""
        client = self._client
        if client is None:
            return None
        try:
            session = Session.decode(client.export_session_string())
            exp = session.access_jwt_payload.exp
        except Exception:  # noqa: BLE001
            return None
        if not exp or exp - time.time() <= (self._request_s or http.DEFAULT_TIMEOUT):
            return None
        return session

    async def _asession_upkeep(self) -> None:
        """Log in when there is no session; else send atproto's smallest call (getSession), before
        which the library refreshes a token near its end, as it does before every call it sends."""
        kept = self._client is not None
        client = await self._aensure_client()
        if client is None or not kept:
            return
        try:
            await self._aheld(client.com.atproto.server.get_session)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bluesky session refresh failed: %s", exc)

    async def _asession(self) -> Optional[Session]:
        session = self._live_session()
        if session is None:
            await self._asession_upkeep()
            session = self._live_session()
        return session

    async def afetch_url(self, url: str) -> Optional[Document]:
        host = urlparse(url).hostname or ""
        if "bsky.app" not in host and "bsky.social" not in host:
            return None
        # Pattern: bsky.app/profile/<handle>/post/<rkey>
        parts = urlparse(url).path.strip("/").split("/")
        if len(parts) < 4 or parts[0] != "profile" or parts[2] != "post":
            return None
        return await self._aread_post(parts[1], parts[3])

    async def _aread_post(self, handle: str, rkey: str) -> Optional[Document]:
        client = await self._aensure_client()
        if client is None:
            return None
        try:
            profile = await self._aheld(client.get_profile, handle)
            at_uri = f"at://{profile.did}/app.bsky.feed.post/{rkey}"
            thread = await self._aheld(client.get_post_thread, at_uri)
            return self._post_to_document(thread.thread.post)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bluesky fetch_url failed: %s", exc)
            return None

    async def ahealth_check(self) -> tuple[Optional[bool], str]:
        """Ask the search path itself: the same ``search_posts`` call, through the same client and
        login, for ONE post on a word that always has posts. Reusing the kept session is not
        evidence by itself (until 2026-10-04 it read None and the source was never verified in a
        long-running process); the session is reused, never re-made per check.

        True: the answer parses into posts and has at least one. None: HTTP 429 or the declared gate
        busy (nothing sent). False: any other error, an unparseable answer, zero posts. A kept session
        the search refuses (401 / 403 / expired token) is logged in again ONCE, as a fresh client
        would; a failed re-login is False."""
        if not auth.is_configured("bluesky"):
            return False, f"credentials not configured: {where.show(where.credentials())}/bluesky.json ({auth.template_hint('bluesky')})"
        kept = self._client is not None
        client = await self._aensure_client()
        if client is None:
            return self._login_verdict("login")
        verdict = await self._aprobe_search(client)
        if verdict is _RELOGIN and kept:
            self._client = None
            client = await self._aensure_client()
            if client is None:
                return self._login_verdict("the kept session was refused; re-login")
            verdict = await self._aprobe_search(client)
        if verdict is _RELOGIN:
            return False, "search refused a fresh login (HTTP 401 / 403 / expired token)"
        return verdict

    def _login_verdict(self, what: str) -> tuple[Optional[bool], str]:
        exc = getattr(self, "_login_error", None)
        if isinstance(exc, upstreams.UpstreamBusy):
            return None, f"not verified: {what} held back by the declared Bluesky gate, nothing sent ({exc})"
        if _xrpc_status(exc) == 429:
            wait = upstreams.parse_retry_after(getattr(_xrpc_response(exc), "headers", None))
            return None, f"{_probe.rate_limited(_PROBE_HOST, wait)} ({what})"
        why = f": {type(exc).__name__}" if exc is not None else ""
        return False, f"{what} failed{why}"

    async def _aprobe_search(self, client: AsyncClient):
        try:
            resp = await self._aheld(client.app.bsky.feed.search_posts, {"q": _PROBE_QUERY, "limit": 1})
        except upstreams.UpstreamBusy as exc:
            return None, f"not verified: the declared Bluesky gate was busy, nothing sent ({exc})"
        except Exception as exc:  # noqa: BLE001
            status = _xrpc_status(exc)
            if status == 429:
                wait = upstreams.parse_retry_after(getattr(_xrpc_response(exc), "headers", None))
                return None, _probe.rate_limited(_PROBE_HOST, wait)
            if status in (401, 403) or _xrpc_error(exc) in ("ExpiredToken", "InvalidToken"):
                return _RELOGIN
            return False, f"search probe failed: {type(exc).__name__}" + (f" (HTTP {status})" if status else "")
        posts = getattr(resp, "posts", None)
        if not isinstance(posts, list):
            return False, "search answered without a posts list"
        if not posts:
            return False, f"search returned 0 posts for {_PROBE_QUERY!r}"
        return True, f"OK (search answered {len(posts)} post for {_PROBE_QUERY!r})"

    @staticmethod
    def _post_to_document(post) -> Document:
        record = post.record
        author = post.author
        text = getattr(record, "text", "") or ""
        created_at = getattr(record, "created_at", None)
        date = to_utc(created_at)

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
