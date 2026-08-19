#!/usr/bin/env bash
# sync_from_eye.sh: mechanically mirror the eye's code into OmniSeek.
#
# The eye is canonical. OmniSeek is the public mirror: full namespace +
# branding rename, personal sources excluded. The RENAME rules were first
# derived by diffing the hand-made penumbra-era mirror state (bb8a4af)
# against the eye, NOT invented; the 2026-08-15 rebrand re-targeted them
# penumbra -> omniseek with the same structure.
#
# Self-verifying: a residue gate (grep) and a smoke gate (the mirror's own
# tests) run at the end and FAIL the script loudly. Never push a sync whose
# gates did not pass.
#
# Syncs:  src/  (minus polyu + mokahr_ats), tests/smoke.py
# Keeps:  skills/, README*, CLAUDE.md, docs/, pyproject.toml, .github/, other scripts/
#
# Usage:  cd the mirror repo root && bash scripts/sync_from_eye.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PEN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# THE SOURCE MOVED (2026-08-12). This used to read "$PEN_ROOT/../eye", i.e. Polaris/organs/eye,
# which was FROZEN AS AN ARCHIVE on 2026-08-11: it and the canonical tree are one lineage that
# forked at ba0f524 on 2026-07-25, and the canonical is now 41 commits ahead. Syncing the public
# mirror from the archive would have quietly published a tree five weeks stale, including missing
# every guard fix from the 2026-08-11 night. The canonical upstream lives on the
# maintainer's machine; the Windows working copy and deploy client is the sibling
# ResearchProject/polaris-eye-maintenance, kept at the same HEAD by `git pull --ff-only`.
EYE_ROOT="$(cd "${POLARIS_EYE_ROOT:-$PEN_ROOT/../../../polaris-eye-maintenance}" && pwd)"

# REFUSE the archive by construction, not by memory: its deployer was rewritten to say so, and that
# marker is the cheapest unambiguous fingerprint of the frozen tree.
if [ -f "$EYE_ROOT/deploy.sh" ] && grep -q "frozen archive, not a deployable tree" "$EYE_ROOT/deploy.sh"; then
  echo "FATAL: $EYE_ROOT is the FROZEN ARCHIVE (Polaris/organs/eye), not the canonical eye." >&2
  echo "       The public mirror must be built from ResearchProject/polaris-eye-maintenance" >&2
  echo "       (or set POLARIS_EYE_ROOT)." >&2
  exit 1
fi
[ -d "$EYE_ROOT/src/polaris" ] || { echo "FATAL: no src/polaris under $EYE_ROOT" >&2; exit 1; }
EYE_SRC="$EYE_ROOT/src/polaris"
PEN_SRC="$PEN_ROOT/src/omniseek"

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
#   2026-08-16 flip: "the eye" prose is RENAMED to OmniSeek now (the runtime-surface
#   audit: server instructions and tool descriptions are strings an MCP client SEES, and they
#   said "the eye" to strangers). Bare standalone "eye" in comments stays: invisible at runtime.
echo "  [2/6] renaming namespace + branding ..."
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

# 2c. The package ROOT (src/omniseek/__init__.py) is PUBLIC METADATA, not engine code: its
#     docstring is product positioning and its __version__ is the released PyPI version, and both
#     belong to the MIRROR (pyproject.toml is kept, not synced). The raw copy in step 1 ships the
#     eye's private-era docstring and whatever version string the eye last froze, so re-author
#     this one file from the mirror's own pyproject after every sync; drift is impossible.
VER="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$PEN_ROOT/pyproject.toml" | head -1)"
[ -n "$VER" ] || { echo "FATAL: could not read version from pyproject.toml" >&2; exit 1; }
cat > "$PEN_SRC/__init__.py" <<PYEOF
"""OmniSeek: a self-hosted perception MCP server.

The package root. The MCP tool surface lives in \`\`omniseek.server\`\`, the HTTP
service in \`\`omniseek.serve_http\`\`, and the retrieval engine (sources, ranking,
relation graph) under \`\`omniseek.core\`\`.
"""

__version__ = "$VER"
PYEOF
echo "    re-authored src/omniseek/__init__.py (version $VER from pyproject)"

# 2d. Regenerate the source-catalog doc from the freshly renamed engine. The catalog is code,
#     so the doc rides the same sync that changes it and can never drift; hand counts stay out
#     of prose by mechanism, not memory.
#     PYTHONDONTWRITEBYTECODE: the generator imports the freshly synced src, and without this
#     it litters __pycache__ into that tree, whose .pyc embed the ABSOLUTE repo path, which on
#     this machine contains the private brand, which trips the step-5 residue gate. The gate
#     was right; the generator must not write bytecode into the tree it documents.
echo "    regenerating docs/sources.md ..."
(cd "$PEN_ROOT" && PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 "$PYBIN" scripts/gen_sources_doc.py >/dev/null)

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

# The CONTRACT SUITES, added 2026-08-12. smoke.py grew a gate that runs every tests/test_*.py suite,
# and the mirror carried none of them, so the first sync after that change aborted with
# "0 suites, 0 tests" -- correctly: the gate refuses to report green when there is nothing to check.
# The fix is the one this list's own rule already prescribes (code-bound artifacts ride along, or the
# mirror ships a gate with holes in it), and it is the better answer anyway: a public engine that
# carries its own 23 suites is a stronger artifact than one that asks you to trust it.
#
# DISCOVERED, not enumerated. A hand-list is a second place to forget a file, which is the exact
# defect class this script keeps finding elsewhere; globbing means a suite written tomorrow is
# carried, renamed and residue-gated with no edit here.
# _repo_only.py rides too: it is what lets the three repo-hygiene suites SKIP cleanly in a tree with
# no deploy.sh (which the mirror is) instead of failing on an absence that is correct.
# EXCEPT the deployment-bound ones, and that exclusion is DERIVED too: a suite that imports from
# `scripts.` is testing release machinery (release_layout / release_transaction / bridges) which the
# mirror does not ship by the same rule that keeps SERVICES.md out. Carried anyway they do not fail
# meaningfully, they fail at IMPORT, which is a worse signal: it looks like the mirror is broken
# rather than like the suite does not apply. A deployment suite written tomorrow is excluded with no
# edit here.
while IFS= read -r _suite; do
  [ -n "$_suite" ] || continue
  if grep -qE '^\s*(from|import)\s+scripts[.[:space:]]' "$_suite"; then
    echo "    (skipping $(basename "$_suite"): deployment-bound, the mirror ships no release machinery)"
    continue
  fi
  SYNCED_ARTIFACTS+=("tests/$(basename "$_suite")")
done < <(ls "$EYE_ROOT"/tests/test_*.py "$EYE_ROOT"/tests/_repo_only.py 2>/dev/null || true)

echo "  [3/6] syncing smoke tests + ${#SYNCED_ARTIFACTS[@]} code-bound artifacts ..."
# PRUNE FIRST. A mirror that only ever ADDS is a mirror that drifts: a suite deleted upstream, or
# newly excluded here, would sit in the public repo forever, still running, still being believed.
# Found the hard way: the run that first carried the suites also carried three deployment-bound ones,
# and after they were excluded they kept failing at import because nothing removes a file.
#
# The old comment here claimed "every tests/test_*.py in the mirror comes from this loop, so
# clearing them is safe". True when written, rotted by 2026-08-19: two suites had been authored
# DIRECTLY in the mirror, and this prune deleted both. That mattered more than a lost file, because
# those two suites were the only thing proving the honest-empty behaviour, and the sync had ALSO
# reverted the engine fixes they cover (they lived in the mirror's src/, which step 1 replaces).
# Prune first, gate green, work silently undone. The smoke gate below cannot catch it by
# construction: removing a test makes the suite pass more easily. So the invariant is now declared,
# and enforced twice: this list survives the prune, and the gate after the copy fails on ANY
# deleted test file, including one nobody thought to list.
MIRROR_ONLY_TESTS=(
  "tests/test_honest_empty.py"     # the honest-empty contract: an empty result cannot mean two things
  "tests/test_truthful_status.py"  # the public health sweep's classes (healthy/blocked/rate_limited/down)
)
for rel in "${MIRROR_ONLY_TESTS[@]}"; do
  [ -f "$PEN_ROOT/$rel" ] || { echo "FATAL: declared mirror-only test missing: $rel" >&2; exit 1; }
  cp "$PEN_ROOT/$rel" "$PEN_ROOT/$rel.keep"
done
rm -f "$PEN_ROOT"/tests/test_*.py "$PEN_ROOT"/tests/_repo_only.py
for rel in "${MIRROR_ONLY_TESTS[@]}"; do mv "$PEN_ROOT/$rel.keep" "$PEN_ROOT/$rel"; done
for rel in "${SYNCED_ARTIFACTS[@]}"; do
  [ -f "$EYE_ROOT/$rel" ] || { echo "FATAL: $rel missing at the eye" >&2; exit 1; }
  mkdir -p "$PEN_ROOT/$(dirname "$rel")"
  cp "$EYE_ROOT/$rel" "$PEN_ROOT/$rel"
  sed -i "${RENAME[@]}" "$PEN_ROOT/$rel"
done
sed -i 's/\bpolyu\b *//g' "$PEN_ROOT/tests/smoke.py"

# DELETION GATE. The smoke gate below proves the surviving tests pass; it says nothing about tests
# that stopped existing, because a smaller suite passes more easily. This is the only check in the
# script that can see a test being removed, so it runs before anything is believed.
_gone="$(git -C "$PEN_ROOT" status --porcelain -- tests/ | sed -n 's/^ *D //p')"
if [ -n "$_gone" ]; then
  echo "FATAL: this sync would DELETE tracked test file(s):" >&2
  echo "$_gone" | sed 's/^/         /' >&2
  echo "       If a suite is genuinely gone upstream, delete it in its own commit with a reason." >&2
  echo "       If it is mirror-only, add it to MIRROR_ONLY_TESTS above." >&2
  exit 1
fi

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
# The RETIRED brand is residue too (2026-08-15): after the omniseek rebrand, a 'penumbra' token in
# freshly synced code means a rename rule regressed or a new upstream identifier slipped the table.
if grep -rniq 'penumbra' "${GATE_PATHS[@]}"; then
  echo "  GATE FAIL: 'penumbra' residue (retired brand):"
  grep -rni 'penumbra' "${GATE_PATHS[@]}" | head -10
  FAILED=1
fi
if grep -rnqE '\beye_' "${GATE_PATHS[@]}"; then
  echo "  GATE FAIL: 'eye_' tool-name residue:"
  grep -rnE '\beye_' "${GATE_PATHS[@]}" | head -10
  FAILED=1
fi
# The operator's name must never reach the public artifact (2026-08-16: one source description
# shipped with it; the rename rules above neutralize the class, this gate proves it).
if grep -rniq 'captain' "${GATE_PATHS[@]}"; then
  echo "  GATE FAIL: operator-identity residue:"
  grep -rni 'captain' "${GATE_PATHS[@]}" | head -10
  FAILED=1
fi
# "the eye" is renamed to OmniSeek since 2026-08-16 (runtime surfaces must carry the brand);
# a survivor means the rename rule regressed or an upstream phrasing slipped it.
if grep -rniqE '\bthe eye\b' "${GATE_PATHS[@]}"; then
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
if grep -rniE 'explicit_only.*(circumvention|§?[[:space:]]*1201|decrypt|defeat)' "$PEN_SRC" >/dev/null 2>&1; then
  echo "  GATE FAIL: a shipped source declares the CIRCUMVENTION access tier."
  echo "  LEGAL-POSTURE.md and SECURITY.md both state the public catalog carries none. Either drop"
  echo "  the source from the mirror (step 1) or change what those documents claim. Offenders:"
  grep -rniE 'explicit_only.*(circumvention|§?[[:space:]]*1201|decrypt|defeat)' "$PEN_SRC" | head -5
  FAILED=1
fi
[ "$FAILED" -eq 0 ] || { echo "=== SYNC ABORTED: residue gate failed ==="; exit 1; }

# --- 6. SMOKE GATE (hard fail) ---
echo "  [6/6] smoke gate (the mirror's own tests) ..."
if ! (cd "$PEN_ROOT" && PYTHONIOENCODING=utf-8 "$PYBIN" tests/smoke.py >/tmp/omniseek_smoke.log 2>&1); then
  echo "=== SYNC ABORTED: smoke gate failed. Tail: ==="
  tail -20 /tmp/omniseek_smoke.log
  exit 1
fi
tail -1 /tmp/omniseek_smoke.log

echo "=== sync complete + gates green. Review the diff, then commit. ==="
