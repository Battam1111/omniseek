#!/usr/bin/env bash
# sync_from_eye.sh: mechanically sync source code from the eye to penumbra.
#
# The eye is the canonical source. Penumbra is the public mirror with namespace
# rename + personal sources excluded. This script does the mechanical part;
# the agent reviews the diff for edge cases.
#
# What syncs:  src/ (minus personal sources), tests/smoke.py, scripts/sensor_runner.py
# What stays:  skills/, README, CLAUDE.md, docs/, .github/, this script itself
#
# Usage:  cd organs/penumbra && bash scripts/sync_from_eye.sh
# Works on: macOS (native), Windows (Git Bash), Linux

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PEN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
EYE_ROOT="$(cd "$PEN_ROOT/../eye" && pwd)"

EYE_SRC="$EYE_ROOT/src/polaris"
PEN_SRC="$PEN_ROOT/src/penumbra"

echo "=== sync_from_eye: $EYE_ROOT -> $PEN_ROOT ==="

# --- 1. Clean + copy src/ (exclude personal sources + __pycache__) ---
echo "  [1/6] copying src/ ..."
rm -rf "$PEN_SRC"
cp -R "$EYE_SRC" "$PEN_SRC"
rm -rf "$PEN_SRC/eye/sources/walled/polyu_source.py" 2>/dev/null || true
rm -rf "$PEN_SRC/eye/sources/walled/mokahr_ats_source.py" 2>/dev/null || true
find "$PEN_SRC" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# --- 2. Rename directory: src/penumbra/eye -> src/penumbra/core ---
if [ -d "$PEN_SRC/eye" ]; then
  mv "$PEN_SRC/eye" "$PEN_SRC/core"
fi

# --- 3. Namespace rename in all .py files ---
echo "  [2/6] renaming namespaces ..."
find "$PEN_SRC" -name "*.py" -print0 | while IFS= read -r -d '' f; do
  sed -i \
    -e 's/polaris\.eye/penumbra.core/g' \
    -e 's/polaris\./penumbra./g' \
    -e 's/"eye_/"penumbra_/g' \
    -e "s/'eye_/'penumbra_/g" \
    -e 's/_EYE_/_PENUMBRA_/g' \
    -e 's/"polaris"/"penumbra"/g' \
    "$f"
done

# --- 4. Remove personal sources from facets.json ---
echo "  [3/6] cleaning facets.json ..."
PYBIN=""
for p in python3 python /c/Python313/python.exe /c/Python312/python.exe; do
  if "$p" --version &>/dev/null 2>&1; then PYBIN="$p"; break; fi
done
if [ -n "$PYBIN" ]; then
  "$PYBIN" -c "
import json, pathlib
p = pathlib.Path('$PEN_SRC/core/facets.json')
if p.exists():
    d = json.loads(p.read_text())
    changed = False
    for k in ['mokahr_ats', 'polyu']:
        if k in d:
            del d[k]
            changed = True
    if changed:
        p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n')
        print('    removed personal source entries')
    else:
        print('    no personal entries found')
"
else
  echo "  WARNING: no python found, skipping facets.json cleanup"
fi

# --- 5. Sync smoke tests ---
echo "  [4/6] syncing smoke tests ..."
cp "$EYE_ROOT/tests/smoke.py" "$PEN_ROOT/tests/smoke.py"
sed -i \
  -e 's/polaris\.eye/penumbra.core/g' \
  -e 's/polaris\./penumbra./g' \
  -e 's/"eye_/"penumbra_/g' \
  -e "s/'eye_/'penumbra_/g" \
  -e 's/_EYE_/_PENUMBRA_/g' \
  -e 's/"polaris"/"penumbra"/g' \
  "$PEN_ROOT/tests/smoke.py"

# --- 6. Sync sensor_runner.py ---
echo "  [5/6] syncing sensor_runner.py ..."
cp "$EYE_ROOT/scripts/sensor_runner.py" "$PEN_ROOT/scripts/sensor_runner.py"
sed -i \
  -e 's/polaris\.eye/penumbra.core/g' \
  -e 's/polaris\./penumbra./g' \
  "$PEN_ROOT/scripts/sensor_runner.py"

# --- 7. Verify no residue ---
echo "  [6/6] checking for residue ..."
RESIDUE_POLARIS=$(grep -rl 'polaris\.' "$PEN_SRC" --include="*.py" 2>/dev/null | head -5 || true)
RESIDUE_EYE=$(grep -rn '"eye_\|'"'"'eye_' "$PEN_SRC" --include="*.py" 2>/dev/null | grep -v '#' | grep -v 'eye_candy\|eye_contact\|eye_level' | head -5 || true)

if [ -n "$RESIDUE_POLARIS" ]; then
  echo "  WARNING: 'polaris.' residue in src:"
  echo "$RESIDUE_POLARIS"
fi
if [ -n "$RESIDUE_EYE" ]; then
  echo "  WARNING: 'eye_' tool-name residue in src:"
  echo "$RESIDUE_EYE"
fi

echo "=== sync complete. Review the diff before committing. ==="
