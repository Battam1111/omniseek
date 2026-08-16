#!/usr/bin/env bash
# OmniSeek: non-Docker first-run provisioning. Run inside your venv, from any directory
# (it locates the repo root itself). Docker users don't need this: the container entrypoint
# does the equivalent.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> 1/4  Install the package (core install, Apache-clean)."
echo "         For optional extras you accept the license of (see NOTICE):"
echo "           pip install -e '.[pdf,asr,walled]'"
pip install -e .

echo "==> 2/4  Chromium for the scrape/render path (playwright does NOT pip-install the browser)."
python -m playwright install chromium || python -m playwright install --with-deps chromium

echo "==> 3/4  Bearer token (so the HTTP transport never serves open)."
python - <<'PY'
import json, os, pathlib, secrets
p = pathlib.Path(os.path.expanduser("~/.omniseek/credentials/omniseek_http.json"))
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
PROF="$HOME/.omniseek/profile.json"
if [ -f "$PROF" ]; then
  echo "    profile already present at $PROF (leaving it)"
elif [ -f deploy/profile.example.json ]; then
  mkdir -p "$(dirname "$PROF")"
  cp deploy/profile.example.json "$PROF"
  echo "    seeded $PROF from deploy/profile.example.json (edit to taste)"
fi

echo
echo "Done. Optional model pre-pull (the recall vector layer fail-opens to lexical without it):"
echo "    # install the [recall] extra, then download Qwen/Qwen3-Embedding-0.6B (Hugging Face"
echo "    # or ModelScope) into ~/.omniseek/models/qwen3-embedding-0.6b"
echo
echo "Optional: keyed sources (CORE, Adzuna, Podcast Index, …) need an API key you supply (most free)."
echo "    Run  python scripts/creds_doctor.py  to see which are set vs missing; each keyed adapter"
echo "    drops a ~/.omniseek/credentials/<source>.json.template with the sign-up URL inline."
echo
echo "Optional: set OMNISEEK_CONTACT_EMAIL=you@example.org (a courtesy contact a few sources use"
echo "    for a faster lane; it also quiets first-boot OpenAlex rate-limit warnings)."
echo
echo "Start OmniSeek:   python -m omniseek.serve_http"
echo "Health check:    curl -s http://127.0.0.1:8765/healthz"
