# Walled sources: bring your own browser

<sub>[penumbra](../README.md)&nbsp;·&nbsp;[Configuration](configuration.md)&nbsp;·&nbsp;[Tools](tools.md)&nbsp;·&nbsp;[Patterns](patterns.md)&nbsp;·&nbsp;**Walled sources**&nbsp;·&nbsp;[Brand](BRAND.md)</sub>

Some of the richest corners of the penumbra sit behind a **login**: Xiaohongshu, Zhihu, Douyin,
WeChat, and other platforms that no search engine indexes and no public API exposes. Penumbra can
reach them, but only on **your** behalf, through **your** logged-in browser. This guide explains the
model and the setup.

> Walled sources are **OFF by default**. They are the advanced, bring-your-own-account tier. You opt
> in deliberately (see [Enable a walled source](#5-enable-the-source)), and you are responsible for
> using each platform within its Terms of Service in your jurisdiction. See [SECURITY.md](../.github/SECURITY.md).

---

## The trust model (read this first)

Penumbra **never sees your password**. It does not store your credentials, and it does not log in
for you. Instead:

1. **You** run a real Chrome/Chromium on your own machine with remote debugging enabled.
2. **You** log into the platform in that browser, once, by hand.
3. Penumbra connects to that browser over the **Chrome DevTools Protocol (CDP)** and drives it to
   read what your logged-in session can already see.

Your session lives in the browser's own profile directory on your disk. Nothing credential-bearing
ever enters Penumbra's process, its cache, or this repository. If you close the browser or log out,
Penumbra simply can't reach that source until you log back in. This is the same posture as a person
opening a tab: Penumbra reaches only what you, the account holder, are already entitled to see.

---

## How it works

```
   your account                  a Chrome you run                    Penumbra
  ─────────────       log in     ───────────────       CDP        ─────────────
  xiaohongshu.com  ──────────▶   --remote-debugging   ◀────────   walled adapter
  (your session)   (you, once)   --port=9223          connect     (the penumbra_search drill)
                                  (session on disk)
```

Each walled platform is read by an **adapter** that connects to a CDP endpoint on a fixed local
port. Some platforms get their **own** Chrome (their own port + profile) so that one account's
activity never bleeds into another's, and so per-account anti-ban pacing stays isolated.

---

## Setup

### 1. Install the optional dependency

Walled sources need the stealth browser engine. Install the `[walled]` extra:

```bash
pip install -e ".[walled]"        # or "[all]"
python -m playwright install chromium
```

### 2. Know which port a source uses

| Source(s) | Port | Browser profile | Why isolated |
|-----------|------|-----------------|--------------|
| The shared majority (Zhihu, Yipin Sanfendi, CN forums, …) | **9222** | default | one main account, reused |
| Xiaohongshu (secondary account) | **9223** | isolated | per-account anti-ban, strictly serial |
| Xiaohongshu (mainland account) | **9224** | isolated | a second, independent account |
| Douyin | **9225** | `~/.penumbra/chrome-douyin` | account-rate-sensitive, fully isolated |

Sources that are NOT CDP browsers: **Discord** uses a bot token (see its adapter), and **WeChat**
public accounts are read through a self-hosted RSS bridge (see [wewe-rss-self-host.md](wewe-rss-self-host.md)).

### 3. Launch a browser on that port

Use the helper (it detects Chrome/Chromium on macOS and Linux):

```bash
./scripts/launch_cdp.sh 9222                                  # the shared Chrome
./scripts/launch_cdp.sh 9223 ~/.penumbra/chrome-xhs https://www.xiaohongshu.com
./scripts/launch_cdp.sh 9225 ~/.penumbra/chrome-douyin https://www.douyin.com
```

Arguments: `launch_cdp.sh <port> [profile-dir] [url-to-open]`. The profile dir defaults to
`~/.penumbra/chrome-<port>`; keeping it stable is what preserves your login across restarts.

### 4. Log in, once

In the window that opens, log into the platform by hand (scan the QR code, enter your credentials,
solve any challenge). Leave the browser **running**. Because the profile is persistent, you only do
this again when the platform expires your session (typically rare).

### 5. Enable the source

Walled sources are off until you opt in. In `~/.penumbra/profile.json`:

```json
{
  "walled": {
    "enabled": true,
    "bring_your_own": {
      "xiaohongshu": { "note": "my own account, logged in on port 9223" },
      "douyin":      { "note": "my own account, logged in on port 9225" }
    }
  }
}
```

`enabled: true` is your explicit acknowledgment that you accept operator responsibility for these
sources (see [SECURITY.md](../.github/SECURITY.md)). List under `bring_your_own` the sources you have logged
in for; the rest stay dark.

### 6. Use it

Walled sources are `explicit_only`: they never join the broad fan-out (they are slow and
account-rate-sensitive). Name one directly:

```
penumbra_search(query="深圳 租房 经验", sources=["xiaohongshu"], raw=True, full=True)
```

---

## Anti-ban and sessions

Platforms watch for automation. Penumbra is conservative by design, and you should be too:

- **Serial per browser.** Calls to one Chrome are strictly serialized (one flow at a time). This is
  deliberate: two parallel same-site searches on one account trip flood-control and silently return
  nothing. Correctness over speed.
- **One account per isolated Chrome.** Don't point two accounts at the same port, and don't drive one
  account from many machines at once.
- **Keep it human-paced.** The defaults already insert human-like delays. Don't lower them to chase
  throughput on a sensitive account.
- **Session expiry.** If a source starts returning empty, your login probably expired: bring the
  browser to the foreground and log back in. `penumbra_sources(check_health=True)` reports CDP reachability per
  source.

Advanced: set `PENUMBRA_CDP_POOL=1` to keep a persistent CDP connection per browser (lower per-call
latency, at the cost of a held connection). Off by default.

---

## Legal posture

The walled tier is the **unwall** acquisition mode: content behind a login you have a **right** to,
read with a credential **you** hold, to obtain access **you** are already entitled to. It crosses no
access control you are not authorized to be on the far side of.

It is **not** circumvention: the walled tier crosses logins you hold, not access controls you are
not authorized to pass. Sources that defeat a paywall, break a rate-limit, or decrypt a protected
response fall under the separate **circumvention** tier, which is off by default and absent from
the shipped catalog. The framework supports circumvention-class adapters as opt-in `explicit_only`
sources for deployers who choose to build or enable them; the legal judgment is theirs. See
[configuration](configuration.md), [SECURITY.md](../.github/SECURITY.md), and [NOTICE](../NOTICE).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Source returns empty | session expired, or browser not running | re-launch + log in; check the port |
| `cdp_health` fails | no Chrome on that port | run `launch_cdp.sh <port>` |
| `cdp_health` fails though Chrome is up | Chrome started without `--remote-allow-origins=*` (Chrome 111+ rejects the CDP connection) | use `launch_cdp.sh` (it sets the flag), or add `--remote-allow-origins=*` to your own launch command |
| `launch_cdp.sh` seems to do nothing, or `cdp_health` still fails after it | a previous Chrome already held this profile+port, so the new launch forwarded to it and exited | the launcher now kills a stale CDP instance on that port for you; if you had opened Chrome by hand on this profile, quit it and re-run |
| Works once, then empties | parallel calls tripping flood-control | calls are serial by design; reduce concurrency |
| Login challenge every time | profile dir not persistent | pass a STABLE `profile-dir` to `launch_cdp.sh` |

---

<div align="center"><sub><a href="../README.md">← back to the README</a></sub></div>
