#!/usr/bin/env bash
# sync_from_eye.sh: mechanically mirror the eye's code into OmniSeek.
#
# The eye is canonical. OmniSeek is the public mirror: full namespace +
# branding rename, personal sources excluded. The RENAME rules were first
# derived by diffing the hand-made penumbra-era mirror state (bb8a4af)
# against the eye, NOT invented; the 2026-08-15 rebrand re-targeted them
# penumbra -> omniseek with the same structure.
#
# Self-verifying: a residue gate (grep) runs at the end and FAILS the script
# loudly. Never push a sync whose gate did not pass.
#
# Syncs:  src/  (minus polyu + mokahr_ats); the code-bound artifacts listed in step 3.
#         The mirror carries no copy of the eye's tests: its CI checks that the package installs
#         and the server starts (.github/workflows/ci.yml).
# Keeps:  skills/, README*, CLAUDE.md, docs/, pyproject.toml, .github/, other scripts/
#
# Usage:  cd the mirror repo root && bash scripts/sync_from_eye.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PEN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# THE SOURCE MOVED (2026-08-12, and again on 2026-10-03). The mirror now reads the sibling ../03_eye,
# its own git repo, kept at the same HEAD as the canonical upstream by `git pull --ff-only`. An older
# copy of the source is a frozen archive; the check below refuses it.
EYE_ROOT="$(cd "${POLARIS_EYE_ROOT:-$PEN_ROOT/../03_eye}" && pwd)"

# REFUSE the archive by construction, not by memory: its deployer was rewritten to say so, and that
# marker is the cheapest unambiguous fingerprint of the frozen tree.
if [ -f "$EYE_ROOT/deploy.sh" ] && grep -q "frozen archive, not a deployable tree" "$EYE_ROOT/deploy.sh"; then
  echo "FATAL: $EYE_ROOT is the FROZEN ARCHIVE (Polaris/organs/eye), not the canonical eye." >&2
  echo "       The public mirror must be built from the canonical tree at ../03_eye" >&2
  echo "       (or set POLARIS_EYE_ROOT)." >&2
  exit 1
fi
# The package directory: this default, unless the private overlay below names another (the eye's
# package can be renamed privately; the check runs after the overlay so it sees the final choice).
EYE_SRC="$EYE_ROOT/src/polaris"
PEN_SRC="$PEN_ROOT/src/omniseek"

PYBIN=""
for p in python3 python /c/Python313/python.exe /c/Python312/python.exe; do
  if "$p" --version >/dev/null 2>&1; then PYBIN="$p"; break; fi
done
[ -n "$PYBIN" ] || { echo "FATAL: no python found (needed for line endings and docs/sources.md)"; exit 1; }

# The private half of this sync (2026-10-07). This script is published with the mirror, so a rule here
# that NAMES one of the operator's private things would publish the very word it removes. Such rules
# live in the eye's private repository, and the sync stops without them. The file defines PRIVATE_RENAME
# (sed -e arguments, run before the public rename rules), PRIVATE_TOKENS (words the residue gate
# refuses), PRIVATE_PATTERNS (regular expressions the residue gate refuses, for words that are also part
# of ordinary words), PRIVATE_PATTERNS_CASE (the same, matched WITH case, for short codes that are
# also ordinary words in another case), private_prepass (exact-match rewrites run before the rename
# pass) and private_artifact_prepass (the same for each artifact step 3 copies; arguments: the copied
# file and its path relative to the repo root). PRIVATE_PATTERNS (2026-10-11) and PRIVATE_PATTERNS_CASE
# (2026-10-12) may be absent.
PRIVATE_OVERLAY="$EYE_ROOT/mirror/omniseek_private.sh"
[ -f "$PRIVATE_OVERLAY" ] || { echo "FATAL: missing $PRIVATE_OVERLAY (the private half of this sync)" >&2; exit 1; }
# shellcheck source=/dev/null
. "$PRIVATE_OVERLAY"
[ -d "$EYE_SRC" ] || { echo "FATAL: no package source at $EYE_SRC" >&2; exit 1; }

# to_lf PATH...: turn CRLF line ends into LF in every TEXT file under the given files/directories.
# A file with a NUL byte is binary and left alone (the same test the OmniSelf sync uses). Byte-level
# I/O, one python process for the whole tree. Used at every place below that copies files in.
to_lf() {
  "$PYBIN" - "$@" <<'PYEOF'
import os, sys
changed = 0
for root in sys.argv[1:]:
    if os.path.isfile(root):
        paths = [root]
    else:
        paths = [os.path.join(d, n) for d, _, names in os.walk(root) for n in names]
    for p in paths:
        with open(p, "rb") as fh:
            data = fh.read()
        if b"\0" in data:
            continue
        new = data.replace(b"\r\n", b"\n")
        if new != data:
            with open(p, "wb") as fh:
                fh.write(new)
            changed += 1
print(f"    line endings: {changed} file(s) converted CRLF -> LF")
PYEOF
}

echo "=== sync_from_eye: $EYE_ROOT -> $PEN_ROOT ==="

# --- 1. copy src/ (exclude personal sources + caches) ---
echo "  [1/5] copying src/ ..."
rm -rf "$PEN_SRC"
cp -R "$EYE_SRC" "$PEN_SRC"
rm -f "$PEN_SRC/eye/sources/walled/polyu_source.py" \
      "$PEN_SRC/eye/sources/walled/mokahr_ats_source.py"
find "$PEN_SRC" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
[ -d "$PEN_SRC/eye" ] && mv "$PEN_SRC/eye" "$PEN_SRC/core"

# 1b. LINE ENDINGS. The mirror is LF-only (its .gitattributes: `* text=auto eol=lf`). The eye's
#     repository is LF by the same rule, but this copy reads the eye's Windows WORKING tree, where an
#     editor can still leave a CRLF file. Left alone, git would normalize it on commit and the working
#     tree would show a whole-file phantom diff. So convert here, right after the copy and before
#     anything reads these bytes.
to_lf "$PEN_SRC"

# --- 2. rename pass (ORDER MATTERS; semantics = the bb8a4af hand-made mirror) ---
#   module path first, then compound brands, then token-level, then catch-alls.
#   2026-08-16 flip: "the eye" prose is RENAMED to OmniSeek now (the runtime-surface
#   audit: server instructions and tool descriptions are strings an MCP client SEES, and they
#   said "the eye" to strangers). Bare standalone "eye" in comments stays: invisible at runtime.
echo "  [2/5] renaming namespace + branding ..."
RENAME=(
  -e 's/polaris\.eye/omniseek.core/g'
  # the PATH form of the module rename (docs cite files as src/polaris/eye/...): without this,
  # a synced doc points readers at a directory the mirror does not have.
  -e 's|polaris/eye/|omniseek/core/|g'
  -e 's/PolarisDocument/Document/g'
  -e 's/Polaris-eye/OmniSeek/g'
  -e 's/polaris-eye/omniseek/g'
  # the eye's DISTRIBUTION name maps to ours (2026-08-16, PyPI name = plain "omniseek"):
  # runtime hints like "pip install 'polaris-mcp[asr]'" must land as omniseek[asr], not
  # omniseek-mcp[asr], or a public user pip-installs a name we do not publish.
  -e 's/polaris-mcp/omniseek/g'
  # "the eye" prose becomes the product name (2026-08-16 runtime-surface audit).
  # This covers the surfaces an MCP client actually SEES (server instructions + tool
  # descriptions are runtime strings) and harmlessly modernizes comments along the way.
  # \b keeps eye_read-style identifiers out (underscore is a word char, no boundary).
  -e 's/[Tt]he [Ee]ye\b/OmniSeek/g'
  -e 's/\beye_/omniseek_/g'
  # The operator's name is a PRIVATE-era token (2026-08-16 sweep: it reached a public runtime
  # string through a source description). Runtime-visible strings were re-authored at the eye;
  # these rules neutralize the long tail (comments, prompts, identifiers) and the residue gate
  # below makes the whole class unshippable. captain_review first: it is a legacy-state literal
  # in a migration shim (still load-bearing at the eye; vacuous but harmless once renamed here).
  -e 's/captain_review/owner_review/g'
  -e "s/Captain's/the operator's/g"
  -e 's/Captain/the operator/g'
  -e 's/captain/operator/g'
  -e 's/"eye"/"core"/g'
  -e 's/_EYE_/_OMNISEEK_/g'
  -e 's/POLARIS_/OMNISEEK_/g'
  -e 's/polaris/omniseek/g'
  -e 's/Polaris/OmniSeek/g'
)
# The private rules run first, here and wherever the public rules run (step 3 too).
RENAME=("${PRIVATE_RENAME[@]}" "${RENAME[@]}")
# 2a'. The private prepass (PRIVATE_OVERLAY above): exact-match rewrites of lines that carry a private
# name, done before the rename pass. It stops the sync when a pattern no longer matches exactly once.
private_prepass "$PEN_SRC"
find "$PEN_SRC" \( -name "*.py" -o -name "*.json" \) -exec sed -i "${RENAME[@]}" {} +

# 2c. The package ROOT (src/omniseek/__init__.py) is PUBLIC METADATA, not engine code: its
#     docstring is product positioning and its __version__ is the released PyPI version, and both
#     belong to the MIRROR (pyproject.toml is kept, not synced). The raw copy in step 1 ships the
#     eye's private-era docstring and whatever version string the eye last froze, so re-author
#     this one file from the mirror's own pyproject after every sync; drift is impossible.
#     Only the docstring and __version__ are metadata. CODE after the eye's __version__ line is
#     engine behaviour and is kept (2026-10-04: the eye installs its credential masking there, and
#     re-authoring the whole file silently dropped it from the mirror).
VER="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$PEN_ROOT/pyproject.toml" | head -1)"
[ -n "$VER" ] || { echo "FATAL: could not read version from pyproject.toml" >&2; exit 1; }
INIT_CODE="$(awk 'seen{print} /^__version__ = /{seen=1}' "$PEN_SRC/__init__.py")"
cat > "$PEN_SRC/__init__.py" <<PYEOF
"""OmniSeek: a self-hosted perception MCP server.

The package root. The MCP tool surface lives in \`\`omniseek.server\`\`, the HTTP
service in \`\`omniseek.serve_http\`\`, and the retrieval engine (sources, ranking,
relation graph) under \`\`omniseek.core\`\`.
"""

__version__ = "$VER"
PYEOF
if [ -n "$(printf '%s' "$INIT_CODE" | tr -d '[:space:]')" ]; then
  printf '%s\n' "$INIT_CODE" >> "$PEN_SRC/__init__.py"
fi
echo "    re-authored src/omniseek/__init__.py (version $VER from pyproject; code after __version__ kept)"

# 2d. Regenerate the source-catalog doc from the freshly renamed engine. The catalog is code,
#     so the doc rides the same sync that changes it and can never drift; hand counts stay out
#     of prose by mechanism, not memory.
#     PYTHONDONTWRITEBYTECODE: the generator imports the freshly synced src, and without this
#     it litters __pycache__ into that tree, whose .pyc embed the ABSOLUTE repo path, which on
#     this machine contains the private brand, which trips the step-5 residue gate. The gate
#     was right; the generator must not write bytecode into the tree it documents.
echo "    regenerating docs/sources.md ..."
(cd "$PEN_ROOT" && PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 "$PYBIN" scripts/gen_sources_doc.py >/dev/null)

# --- 3. the repo ARTIFACTS bound to the code: same renames as src/.
#
#     ONE list, used by both the copy below and the residue gate in step 5, so a newly synced
#     artifact can never slip past the gate that is supposed to cover it.
#
#     WHAT BELONGS HERE: artifacts bound to the CODE, whose meaning transfers to the mirror intact.
#     docs/BUDGETS.md names each wall-clock budget by the constant that holds it. docs/TOOLS.md is
#     the full tool and server reference: every tool description carries only its contract and ends
#     with "More: omniseek docs/TOOLS.md, <tool>", so the reference ships with the code that cites it.
#     WHAT DOES NOT: the eye's tests (the mirror carries none; its CI checks install and boot), and
#     artifacts bound to the DEPLOYMENT. SERVICES.md and the launchd plists describe one operator's
#     live fleet; the mirror ships no fleet.
#
#     Each artifact owns its name in every letter case: another file whose name differs only in case
#     is removed first, since on a case-insensitive disk (macOS, Windows) the copy would otherwise
#     land in that file and keep the wrong name. Before the rename rules, private_artifact_prepass
#     (from the private half) makes its exact-match rewrites of the passages that describe the
#     operator's own deployment. ---
SYNCED_ARTIFACTS=("docs/BUDGETS.md" "docs/TOOLS.md")

echo "  [3/5] syncing ${#SYNCED_ARTIFACTS[@]} code-bound artifact(s) ..."
for rel in "${SYNCED_ARTIFACTS[@]}"; do
  [ -f "$EYE_ROOT/$rel" ] || { echo "FATAL: $rel missing at the eye" >&2; exit 1; }
  dst_dir="$PEN_ROOT/$(dirname "$rel")"
  base="$(basename "$rel")"
  mkdir -p "$dst_dir"
  for f in "$dst_dir"/*; do
    [ -e "$f" ] || continue
    n="$(basename "$f")"
    if [ "$n" != "$base" ] && [ "$(printf '%s' "$n" | tr '[:upper:]' '[:lower:]')" = "$(printf '%s' "$base" | tr '[:upper:]' '[:lower:]')" ]; then
      rm -f "$f"
      echo "    removed $(dirname "$rel")/$n (same name as $rel but for letter case)"
    fi
  done
  cp "$EYE_ROOT/$rel" "$PEN_ROOT/$rel"
  private_artifact_prepass "$PEN_ROOT/$rel" "$rel"
  sed -i "${RENAME[@]}" "$PEN_ROOT/$rel"
done
# Same line-ending rule as step 1b, for the artifacts this step just copied in.
_synced_abs=()
for rel in "${SYNCED_ARTIFACTS[@]}"; do _synced_abs+=("$PEN_ROOT/$rel"); done
to_lf "${_synced_abs[@]}"

# --- 4. (retired) the standalone cron runner script was DELETED at the eye (P6, 2026-07-03):
#     it was a second, memory-less perception path; the scheduler moved in-process. Its stale
#     penumbra copy was removed the same day; nothing ships it anymore, so there is nothing to
#     sync or delete here (step kept as a numbered placeholder so the 1-5 narration stays stable).
echo "  [4/5] (retired step: the cron runner is gone; scheduler is in-process) ..."

# --- 5. RESIDUE GATE (hard fail) ---
echo "  [5/5] residue gate ..."
# Gate EVERYTHING this script wrote: the renamed src tree plus every synced artifact, off the same
# list step 3 copied from. A file that gets synced but not gated is exactly how a namespace leak
# reaches a public repo.
GATE_PATHS=("$PEN_SRC")
for rel in "${SYNCED_ARTIFACTS[@]}"; do GATE_PATHS+=("$PEN_ROOT/$rel"); done
# gate_hit LABEL GREP_ARGS...: run one gate's grep quietly. 0 is a hit and 1 is clean; any other status
# means grep itself failed, and a checker that failed must never read as a clean tree, so the sync stops.
# (2026-10-07: Git for Windows' grep 3.0 aborts on -i combined with -F, and the old `if grep -q` form
# read that crash as "no residue".)
gate_hit() {
  local label="$1" rc=0
  shift
  grep "$@" >/dev/null 2>&1 || rc=$?
  case "$rc" in
    0) return 0 ;;
    1) return 1 ;;
    *) echo "  GATE ERROR ($label): grep exited $rc, so this gate checked nothing" >&2
       echo "=== SYNC ABORTED: residue gate could not run ===" >&2
       exit 1 ;;
  esac
}
FAILED=0
if gate_hit polaris -rniq 'polaris' "${GATE_PATHS[@]}"; then
  echo "  GATE FAIL: 'polaris' residue:"
  grep -rni 'polaris' "${GATE_PATHS[@]}" | head -10
  FAILED=1
fi
# The RETIRED brand is residue too (2026-08-15): after the omniseek rebrand, a 'penumbra' token in
# freshly synced code means a rename rule regressed or a new upstream identifier slipped the table.
if gate_hit penumbra -rniq 'penumbra' "${GATE_PATHS[@]}"; then
  echo "  GATE FAIL: 'penumbra' residue (retired brand):"
  grep -rni 'penumbra' "${GATE_PATHS[@]}" | head -10
  FAILED=1
fi
# The operator's private words (PRIVATE_TOKENS, from the private half of this sync): a hit means a new
# mention slipped past the private rules. Only file and line are printed, so the log never carries the word.
for tok in "${PRIVATE_TOKENS[@]}"; do
  pat=$(printf '%s' "$tok" | sed 's/[][\.*^$]/\\&/g')   # a literal match without -F (see gate_hit)
  if gate_hit private-word -rniq -e "$pat" "${GATE_PATHS[@]}"; then
    echo "  GATE FAIL: private-word residue (a PRIVATE_TOKENS entry in $PRIVATE_OVERLAY):"
    grep -rni -e "$pat" "${GATE_PATHS[@]}" | cut -d: -f1,2 | head -10
    FAILED=1
  fi
done
# The same for PRIVATE_PATTERNS (2026-10-11): extended regular expressions, matched without case.
for pat in ${PRIVATE_PATTERNS[@]+"${PRIVATE_PATTERNS[@]}"}; do
  if gate_hit private-pattern -rniqE -e "$pat" "${GATE_PATHS[@]}"; then
    echo "  GATE FAIL: private-pattern residue (a PRIVATE_PATTERNS entry in $PRIVATE_OVERLAY):"
    grep -rniE -e "$pat" "${GATE_PATHS[@]}" | cut -d: -f1,2 | head -10
    FAILED=1
  fi
done
# The same for PRIVATE_PATTERNS_CASE (2026-10-12): extended regular expressions, matched WITH case.
for pat in ${PRIVATE_PATTERNS_CASE[@]+"${PRIVATE_PATTERNS_CASE[@]}"}; do
  if gate_hit private-pattern-case -rnqE -e "$pat" "${GATE_PATHS[@]}"; then
    echo "  GATE FAIL: private-pattern residue (a PRIVATE_PATTERNS_CASE entry in $PRIVATE_OVERLAY):"
    grep -rnE -e "$pat" "${GATE_PATHS[@]}" | cut -d: -f1,2 | head -10
    FAILED=1
  fi
done
if gate_hit eye_ -rnqE '\beye_' "${GATE_PATHS[@]}"; then
  echo "  GATE FAIL: 'eye_' tool-name residue:"
  grep -rnE '\beye_' "${GATE_PATHS[@]}" | head -10
  FAILED=1
fi
# The operator's name must never reach the public artifact (2026-08-16: one source description
# shipped with it; the rename rules above neutralize the class, this gate proves it).
if gate_hit captain -rniq 'captain' "${GATE_PATHS[@]}"; then
  echo "  GATE FAIL: operator-identity residue:"
  grep -rni 'captain' "${GATE_PATHS[@]}" | head -10
  FAILED=1
fi
# "the eye" is renamed to OmniSeek since 2026-08-16 (runtime surfaces must carry the brand);
# a survivor means the rename rule regressed or an upstream phrasing slipped it.
if gate_hit 'the eye' -rniqE '\bthe eye\b' "${GATE_PATHS[@]}"; then
  echo "  GATE FAIL: 'the eye' prose residue (runtime surfaces must say OmniSeek):"
  grep -rniE '\bthe eye\b' "${GATE_PATHS[@]}" | head -10
  FAILED=1
fi
# bare standalone "eye" in comments is fine (invisible at runtime); count for awareness only
EYE_PROSE=$(grep -rnoE '\beye\b' "$PEN_SRC" | wc -l || true)
echo "  (info: $EYE_PROSE bare 'eye' mentions remain in comments/docstrings)"

# LEGAL GATE: no shipped adapter may declare the CIRCUMVENTION access tier.
#
# This is the load-bearing factual claim of LEGAL-POSTURE.md and of SECURITY.md ("sources that
# defeat an access control are absent from the shipped catalog"). Until now it was true only by
# coincidence: the one source that declares it (mokahr_ats) happens to be deleted by name in step 1
# as a PERSONAL source. Delete that line, or add a second circumvention-tier source upstream, and a
# public repo would start making a claim its own code contradicts, with nothing to catch it.
#
# The detector is the engine's own: fetcher.py classifies the tier by matching this pattern against
# a source's explicit_only reason string. Gating on the same pattern means the document and the code
# cannot drift apart without this failing.
if gate_hit legal -rniE 'explicit_only.*(circumvention|§?[[:space:]]*1201|decrypt|defeat)' "$PEN_SRC"; then
  echo "  GATE FAIL: a shipped source declares the CIRCUMVENTION access tier."
  echo "  LEGAL-POSTURE.md and SECURITY.md both state the public catalog carries none. Either drop"
  echo "  the source from the mirror (step 1) or change what those documents claim. Offenders:"
  grep -rniE 'explicit_only.*(circumvention|§?[[:space:]]*1201|decrypt|defeat)' "$PEN_SRC" | head -5
  FAILED=1
fi
[ "$FAILED" -eq 0 ] || { echo "=== SYNC ABORTED: residue gate failed ==="; exit 1; }

echo "=== sync complete + residue gate green. Review the diff, then commit. ==="
