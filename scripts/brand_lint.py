#!/usr/bin/env python3
"""Brand-voice lint: mechanically enforce the brand invariants that hand-authoring keeps leaking,
so they stop being prose in BRAND.md and start being a CI gate.

One rule, run over the reader- and deployer-facing surfaces (NOT src/, whose comments follow the
eye's own file style and are mechanically synced from it):

  1. NO EM-DASH (U+2014) anywhere a human reads the project outside code. It is the single most
     common AI-writing tell, and the project bans it in human-facing text; use a colon, semicolon,
     comma, period, or parentheses instead.

A second rule used to live here and was RETIRED with the OmniSeek rebrand (2026-08-15): the
penumbra era banned "perceive/perception" on prose surfaces, because penumbra positioned itself as
a reach utility rather than a perception organ. OmniSeek's positioning is the opposite: perception
IS the category ("perception MCP server for AI agents"), so the vocabulary is now brand-correct and
the ban would fail the tagline itself. If a future rebrand flips the positioning again, restore the
rule from git history rather than rewriting it.

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

# Directories never linted: real code (comments follow the eye's synced file style), the synced
# smoke tests, vendored assets, build caches, local runtime state.
SKIP_DIRS = {"src", "tests", "assets", ".git", "__pycache__",
             ".penumbra", ".penumbra-inbox", ".venv", "node_modules"}

# Rule 1 (em-dash) scope: every non-code file a human reads (docs + the deploy / onboarding config
# a stranger is told to run or edit).
EM_DASH_SUFFIXES = {".md", ".yml", ".yaml", ".toml", ".cff", ".sh", ".service"}
EM_DASH_NAMES = {"Dockerfile", "NOTICE"}


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
        if not (p.suffix in EM_DASH_SUFFIXES or p.name in EM_DASH_NAMES):
            continue
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(lines, 1):
            if EM_DASH in line:
                violations.append((rel, n, "em-dash (U+2014); use : ; , . or ()", line.strip()))

    if violations:
        print(f"brand_lint: FAIL, {len(violations)} violation(s)\n")
        for rel, n, why, text in violations:
            print(f"  {rel}:{n}  {why}")
            print(f"      {text[:120]}")
        print("\nFix these, or narrow the rule in scripts/brand_lint.py if it is a genuine false positive.")
        return 1
    print("brand_lint: OK (no em-dash on any human-facing surface)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
