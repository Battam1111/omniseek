"""Discord communities — read research/peer/job channels via a bot token (REST).

Phase 4 P14 (2026-05-29). opus sub-agent verified the mechanism against the
official Discord API docs. We poll `GET /api/v10/channels/{id}/messages` with
`Authorization: Bot {token}` — no gateway/websocket needed for periodic reads,
and plain httpx is enough (no discord.py).

## ⚠️ Hard reality (be honest about coverage)
A bot can ONLY read servers it has been **added to**, and adding a bot requires
**Manage Server / Administrator** on that server. So this reaches:
  ✅ servers the operator owns / is admin of (own server, lab/课题组 server)
  ❌ big public communities (EleutherAI / HuggingFace / Nous / LAION / Cohere
     For AI) — the operator is a normal member, cannot add a bot → unreachable here.
For those, the research output already flows through Polaris via arXiv /
hf_daily_papers / frontier_labs / youtube_channels and the labs' blog RSS.

## ⚠️ The #1 config gotcha
REST returns empty `content` unless **Message Content Intent** is enabled in the
Developer Portal (it is a privileged intent, independent of the gateway, default
OFF). With it off you get message metadata but blank text.

## Setup (operator)
1. Developer Portal → New Application → Bot → copy the **bot token**.
2. Bot settings → enable **Message Content Intent**.
3. Invite the bot to a server you admin:
   https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot&permissions=66560
   (66560 = View Channel + Read Message History — no manage perms needed)
4. Discord → enable Developer Mode → right-click each channel → Copy Channel ID
   (optionally also Copy Server ID for clean message links).
5. Write ~/.polaris/credentials/discord.json:
   {
     "bot_token": "MT...",
     "channels": [
       {"server": "MyLab", "guild_id": "111", "channel_id": "222", "label": "research"}
     ]
   }
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from penumbra.core import auth, cache
from penumbra.core.normalize import PolarisDocument, jsonsafe, keyword_score_filter

logger = logging.getLogger(__name__)

API = "https://discord.com/api/v10"
TIMEOUT = 20
CACHE_TTL = 600          # 10 min — real-time-ish
PER_CHANNEL = 40         # messages pulled per channel per refresh

# Drop a template on first import so the operator knows the shape.
auth.write_template(
    "discord",
    {
        "bot_token": "PASTE — and enable Message Content Intent in the Dev Portal",
        "channels": [
            {"server": "MyLab", "guild_id": "GUILD_ID", "channel_id": "CHANNEL_ID", "label": "research"}
        ],
    },
)


class DiscordCommunitiesAdapter:
    name = "discord_communities"
    needs_credentials = True
    explicit_only = "walled(需 bot 凭证);命名 eye_fetch 才调,不进广搜"
    description = (
        "Discord 研究/peer/求职 频道 (REST bot) — 仅能读 部署者有管理员权限、"
        "已邀请 bot 的 server (大社区加不进 bot, 走其他源). 配 "
        "~/.polaris/credentials/discord.json + 开 Message Content Intent"
    )

    def _config(self) -> tuple[Optional[str], list[dict]]:
        cfg = auth.load("discord") or {}
        token = cfg.get("bot_token")
        if token and "PASTE" in token:
            token = None
        channels = [c for c in (cfg.get("channels") or []) if c.get("channel_id")]
        return token, channels

    def _discover_channels(self, token: str) -> list[dict]:
        """Auto-discover every text/announcement channel across the bot's guilds.

        Used when discord.json lists no explicit channels — so the operator can Follow
        new announcement channels into the server and they're picked up with zero
        re-config. Cached briefly (channel topology changes rarely)."""
        ck = cache.make_key("discord_communities", "channels")
        cached = cache.get(ck)
        if cached is not None:
            return cached
        out: list[dict] = []
        hdr = {"Authorization": f"Bot {token}"}
        try:
            guilds = httpx.get(f"{API}/users/@me/guilds", headers=hdr, timeout=TIMEOUT).json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("discord guild list failed: %s", exc)
            return out
        for g in guilds if isinstance(guilds, list) else []:
            try:
                chans = httpx.get(f"{API}/guilds/{g['id']}/channels", headers=hdr, timeout=TIMEOUT).json()
            except Exception:  # noqa: BLE001
                continue
            for c in chans if isinstance(chans, list) else []:
                if c.get("type") in (0, 5):  # text / announcement
                    out.append({
                        "server": g.get("name", "?"),
                        "guild_id": g.get("id"),
                        "channel_id": c["id"],
                        "label": c.get("name", ""),
                    })
        cache.set(ck, out, ttl=1800)  # 30 min
        return out

    def _pull(self, token: str, channel_id: str) -> list[dict]:
        try:
            r = httpx.get(
                f"{API}/channels/{channel_id}/messages",
                params={"limit": min(PER_CHANNEL, 100)},
                headers={"Authorization": f"Bot {token}"},
                timeout=TIMEOUT,
            )
            if r.status_code in (401, 403):
                logger.warning("discord channel %s: %s (bot not in server / missing perms)",
                               channel_id, r.status_code)
                return []
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("discord channel %s pull failed: %s", channel_id, exc)
            return []

    def search(self, query: str, limit: int = 10) -> list[PolarisDocument]:
        token, channels = self._config()
        if not token:
            return []
        if not channels:
            # No explicit channels → auto-discover everything the bot can see.
            channels = self._discover_channels(token)
        if not channels:
            return []
        key = cache.make_key("discord_communities", "all")
        cached = cache.get(key)
        if cached is not None:
            docs = [PolarisDocument.model_validate(d) for d in cached]
        else:
            docs = []
            for ch in channels:
                for msg in self._pull(token, ch["channel_id"]):
                    try:
                        doc = self._to_doc(msg, ch)
                        if doc:
                            docs.append(doc)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("discord msg skip: %s", exc)
            # newest first
            docs.sort(key=lambda d: d.date or datetime.min.replace(tzinfo=timezone.utc),
                      reverse=True)
            cache.set(key, [d.model_dump(mode="json") for d in docs], ttl=CACHE_TTL)
        return keyword_score_filter(docs, query)[:limit]

    def fetch_url(self, url: str) -> Optional[PolarisDocument]:
        return None

    def health_check(self) -> tuple[bool, str]:
        token, channels = self._config()
        if not token:
            return False, "no bot_token (see ~/.polaris/credentials/discord.json.template)"
        try:
            r = httpx.get(f"{API}/users/@me", headers={"Authorization": f"Bot {token}"}, timeout=15)
            if r.status_code != 200:
                return False, f"HTTP {r.status_code} (bad token?)"
            who = r.json().get("username", "?")
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        mode = "explicit" if channels else "auto-discover"
        n = len(channels) if channels else len(self._discover_channels(token))
        return True, f"OK (bot @{who}; {n} channels, {mode})"

    @staticmethod
    def _to_doc(m: dict, ch: dict) -> Optional[PolarisDocument]:
        content = m.get("content") or ""
        if not content and m.get("embeds"):
            content = " | ".join(
                (e.get("title") or e.get("description") or "") for e in m["embeds"]
            ).strip()
        msg_id = str(m.get("id") or "")
        if not msg_id:
            return None
        guild = ch.get("guild_id")
        if guild:
            url = f"https://discord.com/channels/{guild}/{ch['channel_id']}/{msg_id}"
        else:
            url = f"https://discord.com/channels/{ch['channel_id']}"
        ts = m.get("timestamp")
        date = None
        if ts:
            try:
                date = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass
        author = (m.get("author") or {}).get("username", "?")
        server = ch.get("server", "?")
        label = ch.get("label", "")
        return PolarisDocument(
            source="discord_communities",
            source_id=msg_id,
            url=url,
            title=(content[:80] or "(embed / empty — enable Message Content Intent)"),
            content=content or "(no text — enable Message Content Intent in the Dev Portal)",
            author=f"{author} @{server}#{label}",
            date=date,
            tags=["discord", f"server:{server}", f"channel:{label}"],
            metadata={
                "server": server,
                "label": label,
                "channel_id": ch["channel_id"],
                "reactions": len(m.get("reactions") or []),
                "raw": jsonsafe(m),
            },
        )


from penumbra.core.fetcher import register_adapter

register_adapter(DiscordCommunitiesAdapter())
