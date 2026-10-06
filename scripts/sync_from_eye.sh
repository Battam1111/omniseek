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
# Syncs:  src/  (minus polyu + mokahr_ats); tests/: smoke.py, every test_*.py suite (minus the
#         deployment-bound ones listed in step 3), __init__.py and the modules the suites use; the
#         code-bound artifacts listed in step 3. Tests of the eye's code live in the eye: apart from
#         the mirror-owned tests/test_mirror_*.py, the mirror's tests/ holds no .py this script did
#         not write (a gate in step 3 aborts on one).
# Keeps:  tests/test_mirror_*.py: tests of the mirror's own material (bench/, scripts/, .github/).
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
# maintainer's machine; the Windows working copy and deploy client was the sibling
# ResearchProject/polaris-eye-maintenance until 2026-10-03, when it moved next to this mirror
# as ../03_eye (its own git repo), kept at the same HEAD by `git pull --ff-only`.
EYE_ROOT="$(cd "${POLARIS_EYE_ROOT:-$PEN_ROOT/../03_eye}" && pwd)"

# REFUSE the archive by construction, not by memory: its deployer was rewritten to say so, and that
# marker is the cheapest unambiguous fingerprint of the frozen tree.
if [ -f "$EYE_ROOT/deploy.sh" ] && grep -q "frozen archive, not a deployable tree" "$EYE_ROOT/deploy.sh"; then
  echo "FATAL: $EYE_ROOT is the FROZEN ARCHIVE (Polaris/organs/eye), not the canonical eye." >&2
  echo "       The public mirror must be built from the canonical tree at ../03_eye" >&2
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

# The private half of this sync (2026-10-07). This script is published with the mirror, so a rule here
# that NAMES one of the operator's private things would publish the very word it removes. Such rules
# live in the eye's private repository, and the sync stops without them. The file defines PRIVATE_RENAME
# (sed -e arguments, run before the public rename rules), PRIVATE_TOKENS (words the residue gate
# refuses) and private_prepass (exact-match rewrites run before the rename pass).
PRIVATE_OVERLAY="$EYE_ROOT/mirror/omniseek_private.sh"
[ -f "$PRIVATE_OVERLAY" ] || { echo "FATAL: missing $PRIVATE_OVERLAY (the private half of this sync)" >&2; exit 1; }
# shellcheck source=/dev/null
. "$PRIVATE_OVERLAY"

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
echo "  [1/6] copying src/ ..."
rm -rf "$PEN_SRC"
cp -R "$EYE_SRC" "$PEN_SRC"
rm -f "$PEN_SRC/eye/sources/walled/polyu_source.py" \
      "$PEN_SRC/eye/sources/walled/mokahr_ats_source.py"
find "$PEN_SRC" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
[ -d "$PEN_SRC/eye" ] && mv "$PEN_SRC/eye" "$PEN_SRC/core"

# 1b. LINE ENDINGS. The mirror is LF-only (its .gitattributes: `* text=auto eol=lf`). The eye's
#     repository is LF by the same rule, but this copy reads the eye's Windows WORKING tree, where an
#     editor can still leave a CRLF file. Left alone, git would normalize it on commit and the working
#     tree would show a whole-file phantom diff; worse, step 2b pins the sha256 of a schema file, and a
#     digest taken over CRLF bytes stops matching the moment git stores the file as LF. So convert
#     here, right after the copy and before anything reads or hashes these bytes.
to_lf "$PEN_SRC"

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
# The private rules run first, here and wherever the public rules run (step 3 too).
RENAME=("${PRIVATE_RENAME[@]}" "${RENAME[@]}")
# 2a'. The private prepass (PRIVATE_OVERLAY above): exact-match rewrites of lines that carry a private
# name, done before the rename pass. It stops the sync when a pattern no longer matches exactly once.
private_prepass "$PEN_SRC"
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
    # newline="\n": text mode on Windows would otherwise write every line end back as CRLF
    # (that is how this file came to be the one CRLF file in the mirror's src/).
    policy.write_text(text.replace(have, want, 1), encoding="utf-8", newline="\n")
    print(f"    re-pinned heartbeat schema digest {have[:12]}.. -> {want[:12]}..")
PYEOF

# 2c. The package ROOT (src/omniseek/__init__.py) is PUBLIC METADATA, not engine code: its
#     docstring is product positioning and its __version__ is the released PyPI version, and both
#     belong to the MIRROR (pyproject.toml is kept, not synced). The raw copy in step 1 ships the
#     eye's private-era docstring and whatever version string the eye last froze, so re-author
#     this one file from the mirror's own pyproject after every sync; drift is impossible.
#     Only the docstring and __version__ are metadata. CODE after the eye's __version__ line is
#     engine behaviour and is kept (2026-10-04: the eye installs its credential masking there, and
#     re-authoring the whole file silently dropped it from the mirror; the smoke gate caught it).
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
# EXCEPT the deployment-bound ones, LISTED here with the reason for each (driver ruling of 2026-09-29):
# they test the eye's release machinery (scripts/release_layout.py, release_transaction.py and the
# bridges) or, since 2026-10-03, its launchd watchdog (scripts/sentinel.py), which the mirror does not
# ship by the same rule that keeps SERVICES.md out. Carried anyway
# they do not fail meaningfully, they fail at IMPORT (`from scripts. ...`), which looks like the mirror
# is broken rather than like the suite does not apply. Every test_*.py NOT on this list is synced.
DEPLOYMENT_BOUND_SUITES=(
  "test_release_bridges.py"      # imports scripts.release_layout / release_transaction: the deploy bridges
  "test_release_layout.py"       # imports scripts.release_layout: the release directory layout
  "test_release_transaction.py"  # imports scripts.release_transaction: the atomic release switch
  "test_sentinel_watch.py"       # loads scripts/sentinel.py: the operator's launchd watchdog (2026-10-03)
  "test_notify_outlet.py"        # loads scripts/_sentinel_common.py and the operator's push outlet (2026-10-06)
)
# THE MIRROR-OWNED PREFIX (driver ruling of 2026-09-29). tests/test_mirror_*.py test the mirror's own
# material (bench/, scripts/, .github/) and belong to the mirror: never written by this sync, never
# checked by the unwritten gate. The eye must never carry a file with that prefix, or its copy would
# overwrite the mirror's own; stop before any test file is written.
_eye_mirror_named="$(ls "$EYE_ROOT"/tests/test_mirror_*.py 2>/dev/null || true)"
if [ -n "$_eye_mirror_named" ]; then
  echo "FATAL: the eye carries test file(s) with the mirror-owned prefix test_mirror_:" >&2
  echo "$_eye_mirror_named" | sed 's/^/         /' >&2
  echo "       That prefix belongs to the mirror's own tests; rename the file at the eye." >&2
  exit 1
fi
while IFS= read -r _suite; do
  [ -n "$_suite" ] || continue
  case " ${DEPLOYMENT_BOUND_SUITES[*]} " in
    *" $(basename "$_suite") "*)
      echo "    (skipping $(basename "$_suite"): deployment-bound, the mirror ships no release machinery or launchd watchdog)"
      continue ;;
  esac
  SYNCED_ARTIFACTS+=("tests/$(basename "$_suite")")
done < <(ls "$EYE_ROOT"/tests/test_*.py "$EYE_ROOT"/tests/_repo_only.py "$EYE_ROOT"/tests/__init__.py \
           2>/dev/null || true)

# THE SUITES' OWN DEPENDENCIES, added 2026-08-29. The discovery rule above is a FILENAME rule, so it
# carries test_job_process_isolation.py and silently leaves behind isolated_job_fixture.py, the module
# that suite spawns as a subprocess. The mirror then held a test referring to a module that did not
# exist there. It cost a long hunt because every cheap signal said fine: the copy does not fail, the
# eye's own macOS deploy is green (the fixture is right there), Windows skips the suite outright, and
# only Linux CI actually runs it -- where the subprocess cannot start, so nothing times out and the
# assertion fails with no hint about why.
#
# So discovery is now DEPENDENCY-driven, not name-driven: whatever a carried suite references as
# tests.<module> is carried too. A fixture added tomorrow rides along with no edit here, which is the
# same reason the suite list itself is globbed rather than hand-written.
while IFS= read -r _dep; do
  [ -n "$_dep" ] || continue
  [ -f "$EYE_ROOT/tests/$_dep.py" ] || continue          # tests.<pkg> that is not a local module
  case " ${SYNCED_ARTIFACTS[*]} " in *" tests/$_dep.py "*) continue ;; esac
  echo "    (carrying tests/$_dep.py: referenced by a synced suite)"
  SYNCED_ARTIFACTS+=("tests/$_dep.py")
done < <(grep -rhoE 'tests\.[A-Za-z_][A-Za-z0-9_]*' "$EYE_ROOT"/tests/test_*.py 2>/dev/null \
         | sed 's/^tests\.//' | sort -u)

echo "  [3/6] syncing smoke tests + ${#SYNCED_ARTIFACTS[@]} code-bound artifacts ..."
# NO MIRROR-ONLY TESTS (driver ruling of 2026-09-29). Tests of the eye's code live in the eye and ride
# this sync; the eye's deploy runs them, which it cannot do for a test that exists only here (three did:
# test_honest_empty, test_truthful_status, test_s3_rebuild, and a source changed under them without
# either side noticing until the mirror's smoke failed). So nothing is pruned and nothing is kept aside:
# every .py under tests/ except the mirror-owned tests/test_mirror_*.py is written by the copy below, and
# the UNWRITTEN gate after it aborts, naming the files, when the mirror holds one this sync did not write
# (a suite deleted upstream, or one written here by hand without the prefix). The deletion gate further
# down still catches a tracked test that disappears.
for rel in "${SYNCED_ARTIFACTS[@]}"; do
  [ -f "$EYE_ROOT/$rel" ] || { echo "FATAL: $rel missing at the eye" >&2; exit 1; }
  mkdir -p "$PEN_ROOT/$(dirname "$rel")"
  cp "$EYE_ROOT/$rel" "$PEN_ROOT/$rel"
  sed -i "${RENAME[@]}" "$PEN_ROOT/$rel"
done
sed -i 's/\bpolyu\b *//g' "$PEN_ROOT/tests/smoke.py"
# The egress baseline (S0.6) must list exactly the modules this tree has: the eye's smoke check judges a
# listed module that does not exist as stale. Step 1 left the two personal sources out, so they leave the
# list here (read and rewritten as JSON, never by line, so the list stays valid wherever they sat).
"$PYBIN" - "$PEN_ROOT/tests/egress_baseline.json" <<'PY'
import json, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    data = json.load(f)
data["modules"] = [m for m in data["modules"]
                   if m.rsplit(".", 1)[-1] not in ("polyu_source", "mokahr_ats_source")]
with open(path, "w", encoding="utf-8", newline="\n") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
PY
# Same line-ending rule as step 1b, for the artifacts this step just copied in.
_synced_abs=()
for rel in "${SYNCED_ARTIFACTS[@]}"; do _synced_abs+=("$PEN_ROOT/$rel"); done
to_lf "${_synced_abs[@]}"

# UNWRITTEN GATE. Every .py under the mirror's tests/ must be one this sync just wrote, or a mirror-owned
# tests/test_mirror_*.py.
_written=" "
for rel in "${SYNCED_ARTIFACTS[@]}"; do _written="$_written$rel "; done
_unwritten=""
while IFS= read -r _py; do
  [ -n "$_py" ] || continue
  _rel="${_py#"$PEN_ROOT"/}"
  case "$_rel" in tests/test_mirror_*.py) continue ;; esac   # mirror-owned
  case "$_written" in *" $_rel "*) ;; *) _unwritten="$_unwritten $_rel" ;; esac
done < <(find "$PEN_ROOT/tests" -name "*.py" -not -path "*/__pycache__/*" | sort)
if [ -n "$_unwritten" ]; then
  echo "FATAL: the mirror's tests/ holds .py file(s) this sync did not write:" >&2
  for _f in $_unwritten; do echo "         $_f" >&2; done
  echo "       A test of the eye's code belongs in the eye (it rides this sync from there); a test of" >&2
  echo "       this repository's own material is named tests/test_mirror_*.py; a suite deleted" >&2
  echo "       upstream is deleted here in its own commit, with a reason." >&2
  exit 1
fi

# DANGLING-REFERENCE GATE. The carry rule above is the fix; this is the check that the fix held.
# A tests.<module> reference that resolves at the eye and not here is exactly the failure that shipped
# on 2026-08-29, and it is invisible to everything else in this script: the copy succeeds, and the
# smoke gate below passes on any platform that skips the affected suite. Cheap, and it fails at sync
# time with the filename instead of in CI with a bare "failures=1".
_dangling=""
while IFS= read -r _ref; do
  [ -n "$_ref" ] || continue
  [ -f "$EYE_ROOT/tests/$_ref.py" ] || continue          # not a local module at the eye either
  [ -f "$PEN_ROOT/tests/$_ref.py" ] || _dangling="$_dangling tests/$_ref.py"
done < <(grep -rhoE 'tests\.[A-Za-z_][A-Za-z0-9_]*' "$PEN_ROOT"/tests/*.py 2>/dev/null \
         | sed 's/^tests\.//' | sort -u)
if [ -n "$_dangling" ]; then
  echo "FATAL: synced tests reference module(s) that did not come with them:$_dangling" >&2
  echo "       They exist at the eye, so the carry rule above missed them." >&2
  exit 1
fi

# DELETION GATE. The smoke gate below proves the surviving tests pass; it says nothing about tests
# that stopped existing, because a smaller suite passes more easily. This is the only check in the
# script that can see a test being removed, so it runs before anything is believed.
_gone="$(git -C "$PEN_ROOT" status --porcelain -- tests/ | sed -n 's/^ *D //p')"
if [ -n "$_gone" ]; then
  echo "FATAL: this sync would DELETE tracked test file(s):" >&2
  echo "$_gone" | sed 's/^/         /' >&2
  echo "       If a suite is genuinely gone upstream, delete it in its own commit with a reason." >&2
  echo "       If it tests this repository's own material, name it tests/test_mirror_*.py." >&2
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

# --- 6. SMOKE GATE (hard fail) ---
# On failure, print every failure section of the log in full before the tail: unittest ERROR/FAIL
# blocks with their tracebacks, bare Python tracebacks, and smoke's own "  FAIL " lines, capped at
# 400 lines in total. The tail alone lost the cause of the 2026-10-05 intermittent failure, because
# the error text sat further up the log than its last 20 lines.
# BEGIN smoke_failure_sections
smoke_failure_sections() {
  awk -v cap=400 '
    function emit(s) {
      if (n < cap) { print s; n++ }
      else if (!capped) { print "  ... (failure sections capped at " cap " lines)"; capped = 1 }
    }
    inblk {
      if ($0 ~ /^Ran [0-9]+ tests? in / || $0 ~ /^======+$/) { inblk = 0 }
      else { emit($0); prev = $0; next }
    }
    intb {
      emit($0)
      if ($0 !~ /^[ \t]/) { intb = 0 }
      prev = $0; next
    }
    /^(ERROR|FAIL): / && prev ~ /^======+$/ { emit(prev); emit($0); inblk = 1; prev = $0; next }
    /^Traceback \(most recent call last\):/ { emit($0); intb = 1; prev = $0; next }
    /^  FAIL / || /^SMOKE FAILED/ { emit($0) }
    { prev = $0 }
  ' "$1"
}
# END smoke_failure_sections
SMOKE_LOG=/tmp/omniseek_smoke.log
echo "  [6/6] smoke gate (the mirror's own tests) ..."
if ! (cd "$PEN_ROOT" && PYTHONIOENCODING=utf-8 "$PYBIN" tests/smoke.py >"$SMOKE_LOG" 2>&1); then
  echo "=== SYNC ABORTED: smoke gate failed. Failure sections (full text, at most 400 lines): ==="
  smoke_failure_sections "$SMOKE_LOG"
  echo "=== Tail: ==="
  tail -20 "$SMOKE_LOG"
  echo "=== Full log: $SMOKE_LOG ==="
  if command -v cygpath >/dev/null 2>&1; then echo "    (Windows path: $(cygpath -w "$SMOKE_LOG"))"; fi
  exit 1
fi
tail -1 "$SMOKE_LOG"

echo "=== sync complete + gates green. Review the diff, then commit. ==="
