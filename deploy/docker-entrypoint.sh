#!/bin/sh
# First-run provisioning for the container: generate a bearer token if absent (so OmniSeek never
# serves open), and seed the safe default profile. Then exec the CMD. Idempotent.
#
# EVERY message here goes to stderr, deliberately. The same image runs the stdio transport
# (Dockerfile.stdio, and any client that launches the container itself), and there stdout IS the
# JSON-RPC channel: a banner printed to it arrives ahead of the handshake and corrupts the stream.
# Printing the bearer token to stdout also leaked a live credential into whatever captures the
# container's output, which for a hosted build is somebody else's log.
set -e

# Every location comes from omniseek.where (the one place that names them), never written here.
TOK="$(python -m omniseek.where credentials)/omniseek_http.json"
if [ ! -f "$TOK" ]; then
  python - <<'PY'
import json, secrets, sys
from omniseek import where
p = where.credentials() / "omniseek_http.json"
p.parent.mkdir(parents=True, exist_ok=True)
tok = secrets.token_urlsafe(32)
p.write_text(json.dumps({"token": tok}))
try:
    p.chmod(0o600)
except OSError:
    pass
print("[omniseek] generated a new bearer token:\n    " + tok
      + "\n  clients send  Authorization: Bearer <token>  on every request.",
      file=sys.stderr)
PY
fi

# Seed the safe default profile if the deployer hasn't written one (broad benign sources ON,
# walled login-sources OFF). Edit the profile (`python -m omniseek.where profile` prints where) to
# taste; the _doc fields of profile.example.json explain each part.
PROF="$(python -m omniseek.where profile)"
if [ ! -f "$PROF" ] && [ -f /app/profile.example.json ]; then
  mkdir -p "$(dirname "$PROF")"
  cp /app/profile.example.json "$PROF"
  echo "[omniseek] seeded $PROF from profile.example.json (walled sources are OFF by default)" >&2
fi

exec "$@"
