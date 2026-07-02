#!/bin/sh
# First-run provisioning for the container: generate a bearer token if absent (so Penumbra never
# serves open), and seed the safe default profile. Then exec the CMD. Idempotent.
set -e

POLDIR="${HOME:-/root}/.penumbra"
TOK="$POLDIR/credentials/penumbra_http.json"
if [ ! -f "$TOK" ]; then
  mkdir -p "$POLDIR/credentials"
  python - <<'PY'
import json, os, pathlib, secrets
p = pathlib.Path(os.path.expanduser("~/.penumbra/credentials/penumbra_http.json"))
p.parent.mkdir(parents=True, exist_ok=True)
tok = secrets.token_urlsafe(32)
p.write_text(json.dumps({"token": tok}))
try:
    p.chmod(0o600)
except OSError:
    pass
print("[penumbra] generated a new bearer token:\n    " + tok
      + "\n  clients send  Authorization: Bearer <token>  on every request.")
PY
fi

# Seed the safe default profile if the deployer hasn't written one (broad benign sources ON,
# walled login-sources OFF). Edit ~/.penumbra/profile.json to taste; see the README.
PROF="$POLDIR/profile.json"
if [ ! -f "$PROF" ] && [ -f /app/profile.example.json ]; then
  mkdir -p "$POLDIR"
  cp /app/profile.example.json "$PROF"
  echo "[penumbra] seeded $PROF from profile.example.json (walled sources are OFF by default)"
fi

exec "$@"
