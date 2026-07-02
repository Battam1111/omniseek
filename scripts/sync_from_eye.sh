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
EYE_ROOT="$(cd "$PEN_ROOT/../eye" && pwd)"
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

# --- 3. smoke tests: same renames + drop polyu from the frozen explicit_only list
#     (the public mirror has no polyu source; mokahr_ats is already tolerated
#      at the source: "<= {mokahr_ats}") ---
echo "  [3/6] syncing smoke tests ..."
cp "$EYE_ROOT/tests/smoke.py" "$PEN_ROOT/tests/smoke.py"
sed -i "${RENAME[@]}" "$PEN_ROOT/tests/smoke.py"
sed -i 's/\bpolyu\b *//g' "$PEN_ROOT/tests/smoke.py"

# --- 4. (retired) the standalone cron runner script was DELETED at the eye (P6, 2026-07-03):
#     it was a second, memory-less perception path; the scheduler moved in-process. Its stale
#     penumbra copy was removed the same day; nothing ships it anymore, so there is nothing to
#     sync or delete here (step kept as a numbered placeholder so the 1-6 narration stays stable).
echo "  [4/6] (retired step: the cron runner is gone; scheduler is in-process) ..."

# --- 5. RESIDUE GATE (hard fail) ---
echo "  [5/6] residue gate ..."
FAILED=0
if grep -rniq 'polaris' "$PEN_SRC" "$PEN_ROOT/tests/smoke.py"; then
  echo "  GATE FAIL: 'polaris' residue:"
  grep -rni 'polaris' "$PEN_SRC" "$PEN_ROOT/tests/smoke.py" | head -10
  FAILED=1
fi
if grep -rnqE '\beye_' "$PEN_SRC" "$PEN_ROOT/tests/smoke.py"; then
  echo "  GATE FAIL: 'eye_' tool-name residue:"
  grep -rnE '\beye_' "$PEN_SRC" "$PEN_ROOT/tests/smoke.py" | head -10
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
