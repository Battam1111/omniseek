#!/usr/bin/env bash
# Launch a Chrome/Chromium with remote debugging, so OmniSeek can read a walled source through your
# own logged-in session. See docs/walled-sources.md for the full model.
#
#   ./scripts/launch_cdp.sh <port> [profile-dir] [url-to-open]
#
#   port         the CDP port the adapter expects (9222 shared, 9223/9224 xiaohongshu, 9225 douyin)
#   profile-dir  persistent browser profile; default ~/.omniseek/chrome-<port>. Keep it stable so
#                your login survives restarts.
#   url          optional page to open (e.g. https://www.xiaohongshu.com) so you can log in directly.
#
# After it opens: log into the platform by hand, then leave the browser running. OmniSeek connects
# to http://127.0.0.1:<port>.
set -euo pipefail

PORT="${1:-}"
if [ -z "$PORT" ]; then
  echo "usage: $0 <port> [profile-dir] [url]" >&2
  echo "  ports: 9222 shared | 9223 xiaohongshu | 9224 xiaohongshu(mainland) | 9225 douyin" >&2
  exit 1
fi
PROFILE="${2:-$HOME/.omniseek/chrome-$PORT}"
URL="${3:-}"

# Locate a Chrome/Chromium binary across macOS and Linux.
find_chrome() {
  local candidates=(
    "${CHROME_BIN:-}"                                                    # explicit override wins
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    "/Applications/Chromium.app/Contents/MacOS/Chromium"
    "$(command -v google-chrome 2>/dev/null || true)"
    "$(command -v google-chrome-stable 2>/dev/null || true)"
    "$(command -v chromium 2>/dev/null || true)"
    "$(command -v chromium-browser 2>/dev/null || true)"
  )
  for c in "${candidates[@]}"; do
    [ -n "$c" ] && [ -x "$c" ] && { echo "$c"; return 0; }
  done
  return 1
}

CHROME="$(find_chrome || true)"
if [ -z "$CHROME" ]; then
  echo "ERROR: no Chrome/Chromium found. Install Google Chrome, or point CHROME_BIN at it:" >&2
  echo "  CHROME_BIN=/path/to/chrome $0 $*" >&2
  exit 1
fi

mkdir -p "$PROFILE"
echo "==> launching $(basename "$CHROME")"
echo "    port    : $PORT  (OmniSeek connects to http://127.0.0.1:$PORT)"
echo "    profile : $PROFILE  (your login persists here)"
[ -n "$URL" ] && echo "    opening : $URL"
echo "    Log in by hand in the window, then leave this browser running."

# Pre-flight: if a Chrome is already bound to this CDP port (a stale or zombie instance from a
# previous launch), a fresh `chrome --user-data-dir=<same profile>` will NOT open a new debugging
# port. Chrome sees the profile is already owned, forwards these flags to the running instance, and
# exits, so cdp_health keeps failing even though "a Chrome is up". Kill the stale one first.
# (pgrep/pkill exist on macOS and Linux; on systems without them this just no-ops.)
if command -v pkill >/dev/null 2>&1 && pgrep -f "remote-debugging-port=$PORT" >/dev/null 2>&1; then
  echo "==> a Chrome is already bound to port $PORT; killing the stale instance so this launch owns it" >&2
  pkill -f "remote-debugging-port=$PORT" || true
  sleep 2
fi

# Flag rationale (all loopback-only, so safe on your own machine):
#   --remote-allow-origins=*  REQUIRED on Chrome 111+ (Mar 2023). Without it, Chrome rejects the
#                             CDP WebSocket upgrade and OmniSeek cannot attach (the symptom is a
#                             walled source silently returning empty even though Chrome is running).
#   --disable-blink-features=AutomationControlled  hides the navigator.webdriver automation tell, so
#                             the site sees an ordinary Chrome. Do NOT add --enable-automation (it
#                             sets navigator.webdriver=true, the opposite of what you want).
#   --disable-features=PrivacySandboxAdsAPIs  suppresses the Privacy Sandbox prompt that can steal focus.
exec "$CHROME" \
  --remote-debugging-port="$PORT" \
  --remote-allow-origins=* \
  --user-data-dir="$PROFILE" \
  --no-first-run \
  --no-default-browser-check \
  --disable-blink-features=AutomationControlled \
  --disable-features=PrivacySandboxAdsAPIs \
  ${URL:+"$URL"}
