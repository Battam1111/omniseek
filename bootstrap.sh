#!/usr/bin/env bash
# Penumbra: non-Docker first-run provisioning. Run from the repo root, inside your venv.
# (Docker users don't need this: the container entrypoint does the equivalent.)
set -euo pipefail

echo "==> 1/4  Install the package (CORE, Apache-clean)."
echo "         For optional extras you accept the license of (see NOTICE):"
echo "           pip install -e '.[pdf,asr,walled]'"
pip install -e .

echo "==> 2/4  Chromium for the scrape/render path (playwright does NOT pip-install the browser)."
python -m playwright install chromium || python -m playwright install --with-deps chromium

echo "==> 3/4  Bearer token (so the HTTP transport never serves open)."
python - <<'PY'
import json, os, pathlib, secrets
p = pathlib.Path(os.path.expanduser("~/.penumbra/credentials/penumbra_http.json"))
if p.exists():
    print(f"    token already present at {p} (leaving it)")
else:
    p.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    p.write_text(json.dumps({"token": tok}))
    try:
        p.chmod(0o600)
    except OSError:
        pass
    print(f"    generated a new bearer token at {p}:\n        {tok}")
    print("    clients send  Authorization: Bearer <token>  on every request.")
PY

echo "==> 4/4  Default profile (broad benign sources ON, walled login-sources OFF)."
PROF="$HOME/.penumbra/profile.json"
if [ -f "$PROF" ]; then
  echo "    profile already present at $PROF (leaving it)"
elif [ -f profile.example.json ]; then
  mkdir -p "$(dirname "$PROF")"
  cp profile.example.json "$PROF"
  echo "    seeded $PROF from profile.example.json (edit to taste)"
fi

echo
echo "Done. Optional model pre-pull (the recall vector layer fail-opens to lexical without it):"
echo "    # install the [recall] extra, then place the model at ~/.penumbra/models/qwen3-embedding-0.6b"
echo
echo "Optional: keyed sources (CORE, Adzuna, Podcast Index, …) need an API key you supply (most free)."
echo "    Run  python scripts/creds_doctor.py  to see which are set vs missing; each keyed adapter"
echo "    drops a ~/.penumbra/credentials/<source>.json.template with the sign-up URL inline."
echo
echo "Start Penumbra:   python -m penumbra.serve_http"
echo "Health check:    curl -s http://127.0.0.1:8765/healthz"
