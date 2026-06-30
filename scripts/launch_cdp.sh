#!/usr/bin/env bash
# Launch a Chrome/Chromium with remote debugging, so Penumbra can read a walled source through your
# own logged-in session. See docs/walled-sources.md for the full model.
#
#   ./scripts/launch_cdp.sh <port> [profile-dir] [url-to-open]
#
#   port         the CDP port the adapter expects (9222 shared, 9223/9224 xiaohongshu, 9225 douyin)
#   profile-dir  persistent browser profile; default ~/.penumbra/chrome-<port>. Keep it stable so
#                your login survives restarts.
#   url          optional page to open (e.g. https://www.xiaohongshu.com) so you can log in directly.
#
# After it opens: log into the platform by hand, then leave the browser running. Penumbra connects
# to http://127.0.0.1:<port>.
set -euo pipefail

PORT="${1:-}"
if [ -z "$PORT" ]; then
  echo "usage: $0 <port> [profile-dir] [url]" >&2
  echo "  ports: 9222 shared | 9223 xiaohongshu | 9224 xiaohongshu(mainland) | 9225 douyin" >&2
  exit 1
fi
PROFILE="${2:-$HOME/.penumbra/chrome-$PORT}"
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
echo "    port    : $PORT  (Penumbra connects to http://127.0.0.1:$PORT)"
echo "    profile : $PROFILE  (your login persists here)"
[ -n "$URL" ] && echo "    opening : $URL"
echo "    Log in by hand in the window, then leave this browser running."

exec "$CHROME" \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE" \
  --no-first-run \
  --no-default-browser-check \
  ${URL:+"$URL"}
