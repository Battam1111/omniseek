"""Which world a suite belongs to: the SOURCE REPO, or the shipped artifact.

A release payload is `src scripts tests pyproject.toml README.md SERVICES.md docs` and nothing else
(scripts/deploy_payload.sh). deploy.sh, public_prepare.sh and the .git working tree are absent from
an activated release BY DESIGN. Three suites read exactly those things, so run from a release they
fail on files whose absence is correct.

That was enough, once, to make the whole battery look unrunnable at the deploy gate, and the gate
went in without them: 23 suites that only ever ran when someone remembered. The fix is not to weaken
the gate but to say WHICH world each suite needs, so the gate can run everything and still tell a
genuine red from a not-applicable.

The predicate is deploy.sh at the repo root: it is the file they actually read, it is absent from a
release by construction, and it is present in every checkout. Not `.git`, which a shallow export or
a worktree can lack while the scripts are all there.

Not named test_*.py on purpose: unittest discover must not collect this as a suite. Importers need
the two-form guard below, because `unittest discover -s tests` puts tests/ on sys.path while
`python -m unittest tests.test_x` puts the repo root there instead:

    try:
        from _repo_only import requires_source_repo
    except ImportError:
        from tests._repo_only import requires_source_repo
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_REPO = (ROOT / "deploy.sh").is_file()

# ONE reason string. tests/smoke.py's gate carries it verbatim in its declared-skips allowlist, so
# editing it here without editing there fails the gate. That is deliberate: every skip at the gate
# must be a declared one, or a suite could quietly start skipping itself and read as green.
REASON = "source-repo suite: deploy.sh absent, so this is a packaged release and not a checkout"

requires_source_repo = unittest.skipUnless(SOURCE_REPO, REASON)
