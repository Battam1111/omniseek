#!/usr/bin/env python3
"""Brand-voice lint: mechanically enforce the penumbra brand invariants that hand-authoring keeps
leaking, so they stop being prose in BRAND.md and start being a CI gate.

Two rules, run over the reader- and deployer-facing surfaces (NOT src/, whose comments follow the
eye's own file style and are mechanically synced from it):

  1. NO EM-DASH (U+2014) anywhere a human reads the project outside code. It is the single most
     common AI-writing tell, and the project bans it in human-facing text; use a colon, semicolon,
     comma, period, or parentheses instead.
  2. NO RETIRED ORGAN VOCABULARY on a penumbra PROSE surface. Penumbra is a utility (what it lets
     you REACH), not the eye's perception organ, so "perceive(s/d)", "perception", "senses" (and the
     CJK 感官 / 感覚) must not describe what Penumbra does. Use reach / retrieve / memory instead.

Exit 0 = clean; exit 1 = violations, each printed as file:line. Wired into CI (.github/workflows/
ci.yml) so a regression fails the build. This is the structural fix for a whole class the launch
audit found by hand: a guard at the one place every surface must pass, not a per-file human
re-audit each time (the same "fix it at the one choke point" move as the log rate-limiter).

Run locally:  python scripts/brand_lint.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EM_DASH = "—"

# Rule 2 vocabulary: organ words that must not describe Penumbra. Latin "perceive/perception" (the
# actual retired framing) plus the CJK organ noun 感官 / 感覚 (the term swapped to 触及 / 届く力 on the
# i18n surfaces). English "senses" is deliberately NOT banned: "in some senses" is a legitimate
# idiom, so the false-positive risk outweighs the value; perceive/perception carry the real rule.
# (The \b + "perceiv" stem spares "imperceptible/perceptible"; it would match a legitimate
# "perceivable" or a non-organ "perception", but neither occurs on a brand surface today, so narrow
# the stem here if that ever changes.)
ORGAN = re.compile(r"\b(?:perceiv\w*|perception)\b|感官|感覚", re.IGNORECASE)

# Directories never linted: real code (comments follow the eye's synced file style), the synced
# smoke tests, vendored assets, build caches, local runtime state.
SKIP_DIRS = {"src", "tests", "assets", ".git", "__pycache__",
             ".penumbra", ".penumbra-inbox", ".venv", "node_modules"}

# Rule 1 (em-dash) scope: every non-code file a human reads (docs + the deploy / onboarding config
# a stranger is told to run or edit).
EM_DASH_SUFFIXES = {".md", ".yml", ".yaml", ".toml", ".cff", ".sh", ".service"}
EM_DASH_NAMES = {"Dockerfile", "NOTICE"}


def _is_prose_surface(rel: str) -> bool:
    """Rule 2 scope: every PROSE surface a reader meets that describes what Penumbra does. A
    dependency comment (pyproject) may legitimately discuss 'perception'; a brand sentence may not.
    Covers the README + docs, the changelog, NOTICE / CITATION, the skill docs, and the .github
    governance prose (CONTRIBUTING / SECURITY / CODE_OF_CONDUCT / templates)."""
    return (rel == "README.md" or rel.startswith("docs/") or rel == "CHANGELOG.md"
            or rel == "NOTICE" or rel.endswith(".cff")
            or (rel.startswith("skills/") and rel.endswith(".md"))
            or (rel.startswith(".github/") and rel.endswith(".md")))


def _iter_files():
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        if p.relative_to(ROOT).parts[0] in SKIP_DIRS:
            continue
        yield p


def main() -> int:
    violations = []
    for p in _iter_files():
        rel = p.relative_to(ROOT).as_posix()
        lint_em = p.suffix in EM_DASH_SUFFIXES or p.name in EM_DASH_NAMES
        lint_organ = _is_prose_surface(rel)
        if not (lint_em or lint_organ):
            continue
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(lines, 1):
            if lint_em and EM_DASH in line:
                violations.append((rel, n, "em-dash (U+2014); use : ; , . or ()", line.strip()))
            if lint_organ:
                m = ORGAN.search(line)
                if m:
                    violations.append(
                        (rel, n, f"organ word '{m.group(0)}'; penumbra REACHES, it does not perceive",
                         line.strip()))

    if violations:
        print(f"brand_lint: FAIL, {len(violations)} violation(s)\n")
        for rel, n, why, text in violations:
            print(f"  {rel}:{n}  {why}")
            print(f"      {text[:120]}")
        print("\nFix these, or narrow the rule in scripts/brand_lint.py if it is a genuine false positive.")
        return 1
    print("brand_lint: OK (no em-dash, no organ vocabulary on brand surfaces)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
