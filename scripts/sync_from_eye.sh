#!/usr/bin/env bash
# sync_from_eye.sh: mechanically mirror the eye's code into penumbra.
#
# The eye is canonical. Penumbra is the public mirror: full namespace +
# branding rename, personal sources excluded. The RENAME rules below were
# derived by diffing the hand-made mirror state (bb8a4af) against the eye,
# NOT invented: match that proven-good state exactly.
#
# Self-verifying: a residue gate (grep) and a smoke gate (penumbra's own
# tests) run at the end and FAIL the script loudly. Never push a sync whose
# gates did not pass.
#
# Syncs:  src/  (minus polyu + mokahr_ats), tests/smoke.py
# Keeps:  skills/, README*, CLAUDE.md, docs/, pyproject.toml, .github/, other scripts/
#
# Usage:  cd organs/penumbra && bash scripts/sync_from_eye.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PEN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# THE SOURCE MOVED (2026-08-12). This used to read "$PEN_ROOT/../eye", i.e. Polaris/organs/eye,
# which was FROZEN AS AN ARCHIVE on 2026-08-11: it and the canonical tree are one lineage that
# forked at ba0f524 on 2026-07-25, and the canonical is now 41 commits ahead. Syncing the public
# mirror from the archive would have quietly published a tree five weeks stale, including missing
# every guard fix from the 2026-08-11 night. The canonical eye lives on the mini
# (~/polaris-mcp-maintenance); the Windows working copy and deploy client is the sibling
# ResearchProject/polaris-eye-maintenance, kept at the same HEAD by `git pull --ff-only`.
EYE_ROOT="$(cd "${POLARIS_EYE_ROOT:-$PEN_ROOT/../../../polaris-eye-maintenance}" && pwd)"

# REFUSE the archive by construction, not by memory: its deployer was rewritten to say so, and that
# marker is the cheapest unambiguous fingerprint of the frozen tree.
if [ -f "$EYE_ROOT/deploy.sh" ] && grep -q "frozen archive, not a deployable tree" "$EYE_ROOT/deploy.sh"; then
  echo "FATAL: $EYE_ROOT is the FROZEN ARCHIVE (Polaris/organs/eye), not the canonical eye." >&2
  echo "       The public mirror must be built from ResearchProject/polaris-eye-maintenance" >&2
  echo "       (or set POLARIS_EYE_ROOT). See Polaris INFRA.md section 8." >&2
  exit 1
fi
[ -d "$EYE_ROOT/src/polaris" ] || { echo "FATAL: no src/polaris under $EYE_ROOT" >&2; exit 1; }
EYE_SRC="$EYE_ROOT/src/polaris"
PEN_SRC="$PEN_ROOT/src/penumbra"

PYBIN=""
for p in python3 python /c/Python313/python.exe /c/Python312/python.exe; do
  if "$p" --version >/dev/null 2>&1; then PYBIN="$p"; break; fi
done
[ -n "$PYBIN" ] || { echo "FATAL: no python found (needed for the smoke gate)"; exit 1; }

echo "=== sync_from_eye: $EYE_ROOT -> $PEN_ROOT ==="

# --- 1. copy src/ (exclude personal sources + caches) ---
echo "  [1/6] copying src/ ..."
rm -rf "$PEN_SRC"
cp -R "$EYE_SRC" "$PEN_SRC"
rm -f "$PEN_SRC/eye/sources/walled/polyu_source.py" \
      "$PEN_SRC/eye/sources/walled/mokahr_ats_source.py"
find "$PEN_SRC" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
[ -d "$PEN_SRC/eye" ] && mv "$PEN_SRC/eye" "$PEN_SRC/core"

# --- 2. rename pass (ORDER MATTERS; semantics = the bb8a4af hand-made mirror) ---
#   module path first, then compound brands, then token-level, then catch-alls.
#   "the eye" PROSE is deliberately kept (the hand-made mirror kept it too).
echo "  [2/6] renaming namespace + branding ..."
RENAME=(
  -e 's/polaris\.eye/penumbra.core/g'
  -e 's/PolarisDocument/Document/g'
  -e 's/Polaris-eye/Penumbra/g'
  -e 's/polaris-eye/penumbra/g'
  -e 's/\beye_/penumbra_/g'
  -e 's/"eye"/"core"/g'
  -e 's/_EYE_/_PENUMBRA_/g'
  -e 's/POLARIS_/PENUMBRA_/g'
  -e 's/polaris/penumbra/g'
  -e 's/Polaris/Penumbra/g'
)
find "$PEN_SRC" \( -name "*.py" -o -name "*.json" \) -exec sed -i "${RENAME[@]}" {} +

# 2b. RE-PIN the one content digest the rename invalidates. The scheduler-heartbeat POLICY pins the
#     sha256 of its SCHEMA file; the rename pass just rewrote that schema's bytes (polaris ->
#     penumbra inside the JSON), so the pin no longer matches the schema sitting next to it and
#     load_contract_artifacts refuses to start ("schema digest mismatch"). The pin's job is to bind
#     a policy to the exact schema it was written against, so recomputing it for the MIRROR'S OWN
#     pair preserves that invariant; shipping the eye's hex would ship a self-inconsistent pair.
#     Targeted hex swap, never a json round-trip, so nothing else in the file moves.
"$PYBIN" - "$PEN_SRC/core/contracts" <<'PYEOF'
import hashlib, json, sys
from pathlib import Path

d = Path(sys.argv[1])
schema, policy = d / "scheduler-heartbeat-v1.json", d / "scheduler-heartbeat-policy-v1.json"
want = hashlib.sha256(schema.read_bytes()).hexdigest()
text = policy.read_text(encoding="utf-8")
have = json.loads(text)["heartbeat_schema_digest"]
if have == want:
    print("    heartbeat schema digest already matches")
else:
    assert text.count(have) == 1, f"pin appears {text.count(have)} times, refusing to guess"
    policy.write_text(text.replace(have, want, 1), encoding="utf-8")
    print(f"    re-pinned heartbeat schema digest {have[:12]}.. -> {want[:12]}..")
PYEOF

# --- 3. smoke tests + the repo ARTIFACTS the suite reads: same renames + drop polyu from the
#     frozen explicit_only list (the public mirror has no polyu source; mokahr_ats is already
#     tolerated at the source: "<= {mokahr_ats}").
#
#     ONE list, used by both the copy below and the residue gate in step 5, so a newly synced
#     artifact can never slip past the gate that is supposed to cover it.
#
#     WHAT BELONGS HERE: artifacts bound to the CODE, whose meaning transfers to the mirror intact.
#     docs/BUDGETS.md is a doc-vs-code drift rail (S0.5 imports each live constant and compares);
#     tests/egress_baseline.json is the egress ratchet baseline (S0.6). Both guard code the mirror
#     ships, so a mirror without them has a gate with holes in it.
#     WHAT DOES NOT: artifacts bound to the DEPLOYMENT. SERVICES.md and the launchd plists describe
#     one operator's live fleet; the mirror ships no fleet, and those checks are already written
#     repo-adaptive at the source (`if _SERVICES_PATH.exists():`), so they skip cleanly here. ---
SYNCED_ARTIFACTS=("tests/smoke.py" "tests/egress_baseline.json" "docs/BUDGETS.md")
echo "  [3/6] syncing smoke tests + the repo artifacts they read ..."
for rel in "${SYNCED_ARTIFACTS[@]}"; do
  [ -f "$EYE_ROOT/$rel" ] || { echo "FATAL: $rel missing at the eye" >&2; exit 1; }
  mkdir -p "$PEN_ROOT/$(dirname "$rel")"
  cp "$EYE_ROOT/$rel" "$PEN_ROOT/$rel"
  sed -i "${RENAME[@]}" "$PEN_ROOT/$rel"
done
sed -i 's/\bpolyu\b *//g' "$PEN_ROOT/tests/smoke.py"

# --- 4. (retired) the standalone cron runner script was DELETED at the eye (P6, 2026-07-03):
#     it was a second, memory-less perception path; the scheduler moved in-process. Its stale
#     penumbra copy was removed the same day; nothing ships it anymore, so there is nothing to
#     sync or delete here (step kept as a numbered placeholder so the 1-6 narration stays stable).
echo "  [4/6] (retired step: the cron runner is gone; scheduler is in-process) ..."

# --- 5. RESIDUE GATE (hard fail) ---
echo "  [5/6] residue gate ..."
# Gate EVERYTHING this script wrote: the renamed src tree plus every synced artifact, off the same
# list step 3 copied from. A file that gets synced but not gated is exactly how a namespace leak
# reaches a public repo.
GATE_PATHS=("$PEN_SRC")
for rel in "${SYNCED_ARTIFACTS[@]}"; do GATE_PATHS+=("$PEN_ROOT/$rel"); done
FAILED=0
if grep -rniq 'polaris' "${GATE_PATHS[@]}"; then
  echo "  GATE FAIL: 'polaris' residue:"
  grep -rni 'polaris' "${GATE_PATHS[@]}" | head -10
  FAILED=1
fi
if grep -rnqE '\beye_' "${GATE_PATHS[@]}"; then
  echo "  GATE FAIL: 'eye_' tool-name residue:"
  grep -rnE '\beye_' "${GATE_PATHS[@]}" | head -10
  FAILED=1
fi
# prose "the eye" is kept by design; report count for awareness only
EYE_PROSE=$(grep -rnoE '\beye\b' "$PEN_SRC" | wc -l || true)
echo "  (info: $EYE_PROSE prose 'eye' mentions kept, matching the hand-made mirror)"
[ "$FAILED" -eq 0 ] || { echo "=== SYNC ABORTED: residue gate failed ==="; exit 1; }

# --- 6. SMOKE GATE (hard fail) ---
echo "  [6/6] smoke gate (penumbra's own tests) ..."
if ! (cd "$PEN_ROOT" && PYTHONIOENCODING=utf-8 "$PYBIN" tests/smoke.py >/tmp/penumbra_smoke.log 2>&1); then
  echo "=== SYNC ABORTED: smoke gate failed. Tail: ==="
  tail -20 /tmp/penumbra_smoke.log
  exit 1
fi
tail -1 /tmp/penumbra_smoke.log

echo "=== sync complete + gates green. Review the diff, then commit. ==="
