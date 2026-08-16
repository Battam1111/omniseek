# Self-hosting a WeChat RSS bridge (optional)

<sub>[OmniSeek](../README.md)&nbsp;·&nbsp;[Configuration](configuration.md)&nbsp;·&nbsp;[Walled sources](walled-sources.md)</sub>

> Only needed if you want to monitor WeChat 公众号 NOT covered by the free hosted wechat2rss.xlab.app
> (e.g., 学术志, 科研圈, or any niche/new account).
>
> Default wechat adapter already covers PaperWeekly + 机器之心 + 量子位 via the free
> wechat2rss.xlab.app aggregator: zero setup, zero maintenance.

## Trade-off

| | wechat2rss.xlab.app (already wired in) | wewe-rss self-host |
|---|---|---|
| Setup | Zero | ~30 min |
| Cookie expiry | None (someone else maintains) | **2-3 days, manual re-scan via WeChat** |
| Accounts available | ~500 popular ones | Any account WeChat Read indexes |
| Daily maintenance | None | Check + re-scan if needed |

If 学术志 / 科研圈 are not critical to your automated retrieval, **skip this entire doc**.
If you really need them, follow the steps below.

## Prerequisites

- Node.js v22+ (the commands below assume a user-local install at `~/.local/node/bin`; adjust to your own PATH)
- pnpm 11+
- No Docker required
- No MySQL required (using SQLite)

## Installation

```bash
mkdir -p ~/Apps && cd ~/Apps
git clone --depth 1 --branch v2.6.1 https://github.com/cooderl/wewe-rss.git
cd wewe-rss

# Use SQLite schema (project ships with both MySQL and SQLite variants)
rm -rf apps/server/prisma
mv apps/server/prisma-sqlite apps/server/prisma
mkdir -p data

# Install (project's lockfile is pnpm 7 format; pnpm 11 will rewrite it)
~/.local/node/bin/pnpm install --no-frozen-lockfile

# Build
~/.local/node/bin/pnpm run -r build
```

## Configuration

```bash
cat > apps/server/.env <<'EOF'
HOST=127.0.0.1
PORT=4000

DATABASE_URL="file:../../data/wewe-rss.db"
DATABASE_TYPE="sqlite"

# REPLACE THIS with your own strong password
AUTH_CODE=OmniSeekRSS_ChangeMe_2026

MAX_REQUEST_PER_MINUTE=60
FEED_MODE=fulltext
SERVER_ORIGIN_URL=http://127.0.0.1:4000

# 7:35 AM + 7:35 PM, twice daily, conservative to avoid throttling
CRON_EXPRESSION="35 7,19 * * *"

ENABLE_CLEAN_HTML=true
UPDATE_DELAY_TIME=60

# Use the China-friendly mirror (default has DNS issues from CN)
PLATFORM_URL=https://weread.965111.xyz
EOF

# Init database
export $(grep -v '^#' apps/server/.env | xargs)
~/.local/node/bin/pnpm --filter server exec prisma generate
~/.local/node/bin/pnpm --filter server exec prisma migrate deploy
```

## Test Run (foreground)

```bash
~/.local/node/bin/pnpm --filter server start:prod
# Open http://127.0.0.1:4000/ in browser to confirm
# Ctrl-C when done
```

## launchd Persistence

```bash
cat > ~/Library/LaunchAgents/local.omniseek.wewerss.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>local.omniseek.wewerss</string>
    <key>ProgramArguments</key>
    <array>
        <string>$HOME/.local/node/bin/node</string>
        <string>$HOME/Apps/wewe-rss/apps/server/dist/main.js</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$HOME/Apps/wewe-rss/apps/server</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$HOME/.local/node/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>NODE_ENV</key>
        <string>production</string>
        <key>HOST</key>
        <string>127.0.0.1</string>
        <key>PORT</key>
        <string>4000</string>
        <key>DATABASE_URL</key>
        <string>file:../../data/wewe-rss.db</string>
        <key>DATABASE_TYPE</key>
        <string>sqlite</string>
        <key>AUTH_CODE</key>
        <string>OmniSeekRSS_ChangeMe_2026</string>
        <key>FEED_MODE</key>
        <string>fulltext</string>
        <key>SERVER_ORIGIN_URL</key>
        <string>http://127.0.0.1:4000</string>
        <key>CRON_EXPRESSION</key>
        <string>35 7,19 * * *</string>
        <key>ENABLE_CLEAN_HTML</key>
        <string>true</string>
        <key>UPDATE_DELAY_TIME</key>
        <string>60</string>
        <key>PLATFORM_URL</key>
        <string>https://weread.965111.xyz</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
        <key>Crashed</key>
        <true/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>$HOME/Library/Logs/wewerss.out.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/wewerss.err.log</string>
</dict>
</plist>
EOF

UID_NUM=$(id -u)
launchctl bootstrap gui/$UID_NUM ~/Library/LaunchAgents/local.omniseek.wewerss.plist
launchctl kickstart gui/$UID_NUM/local.omniseek.wewerss
```

## One-Time WeChat Read Account Binding

1. Open `http://127.0.0.1:4000/` in browser, log in with AUTH_CODE
2. 账号管理 → 添加账号 → 用普通微信扫弹出的 QR 码
3. **Do NOT check "24小时自动退出"**
4. 公众号源 → 添加 → paste any `mp.weixin.qq.com/s/<id>` URL from the account you want
5. **Wait 30-60s between adding accounts** (rapid additions trigger 24h "小黑屋")

## Wire to OmniSeek

After adding feeds, note each account's `MP_WXS_xxxxx` ID from the UI. Then:

```bash
# on the machine that runs OmniSeek:
cat > ~/.omniseek/credentials/wechat.json <<'EOF'
{
  "wewerss_base_url": "http://127.0.0.1:4000",
  "wewerss_auth_code": "OmniSeekRSS_ChangeMe_2026",
  "wewerss_subscribed_feed_ids": ["MP_WXS_xxxxx", "MP_WXS_yyyyy"]
}
EOF
chmod 600 ~/.omniseek/credentials/wechat.json
```

OmniSeek's `wechat` adapter `search()` will automatically include these feeds alongside the default wechat2rss ones.

## Maintenance Reality

- **Every 2-3 days**: check `http://127.0.0.1:4000/` → 账号管理. If account shows "失效", delete + re-scan QR. This is the structural reason most people drop wewe-rss after a month.
- If you can't deal with that cadence, **don't deploy**. Use only the wechat2rss feeds (already wired in by default).

## Verification

```bash
curl -s http://127.0.0.1:4000/feeds/all.atom | head -20
# Should see <?xml version=...> <feed xmlns="http://www.w3.org/2005/Atom">
```

## Known Gotchas

| Symptom | Solution |
|---|---|
| Account 2-3 day expiry | Re-scan QR (structural) |
| Adding accounts → "小黑屋" | ≥30s between additions; wait 24h for release |
| Single account >10 公众号 → banned | Cap at 10/account, add more accounts to scale |
| DNS pollution for default WeRead | Use `PLATFORM_URL=https://weread.965111.xyz` (already in config) |
| pnpm 11 lockfile warning | `--no-frozen-lockfile` (already in install steps) |
| launchd can't find node | Use absolute path `$HOME/.local/node/bin/node` (already in plist) |

---

<div align="center"><sub><a href="../README.md">← back to the README</a></sub></div>
