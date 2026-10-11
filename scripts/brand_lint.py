#!/usr/bin/env python3
"""Brand-voice lint: mechanically enforce the brand invariants that hand-authoring keeps leaking,
so they stop being prose in BRAND.md and start being a CI gate.

Two rules, run over the reader- and deployer-facing surfaces (NOT src/, whose comments are
mechanically synced from the private upstream and keep its file style):

  1. NO EM-DASH (U+2014) anywhere a human reads the project outside code. It is the single most
     common AI-writing tell, and the project bans it in human-facing text; use a colon, semicolon,
     comma, period, or parentheses instead.
  2. VERSIONED IMAGE URLS. A README or site page that loads a brand image (png / gif) from
     raw.githubusercontent.com must end the URL with ?v=YYYY-MM-DD, optionally followed by one
     lowercase letter for a second re-render on the same day (?v=YYYY-MM-DDb), and then optionally by
     GitHub's theme fragment (#gh-light-mode-only / #gh-dark-mode-only). GitHub's image proxy
     keeps serving the old picture under an unchanged URL for a while after the file changes, so a
     re-render goes with a new version in every URL that points at it (docs/BRAND.md, Rules).
  3. NO STUCK EMPHASIS in Markdown. CommonMark opens emphasis only on a left-flanking run of *
     and closes it only on a right-flanking one, and a run wedged between a letter and punctuation
     is not left-flanking, so 头条说的*「最多待 4 年」*其实 shows its asterisks on GitHub and PyPI
     instead of italics (no space before a Chinese or Japanese quote mark, so it bites CJK text
     most). Use <em> / <strong> there, or put a space or punctuation on the outer side.
  4. THE PYPI PAGE MATCHES THE README. docs/pypi.md (what PyPI shows; pyproject.toml `readme`) is
     generated from README.md by scripts/gen_pypi_readme.py, light images only, because PyPI shows
     both images of a GitHub light/dark pair. Editing README.md without regenerating fails here.

A second rule used to live here and was RETIRED with the OmniSeek rebrand (2026-08-15): the
project's earlier brand banned "perceive/perception" on prose surfaces, because it positioned
itself as a reach utility rather than a perception organ. OmniSeek's positioning is the opposite: perception
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
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EM_DASH = "—"

# Directories never linted: real code (comments follow the eye's synced file style), the synced
# smoke tests, vendored assets, build caches, local runtime state.
SKIP_DIRS = {"src", "tests", "assets", ".git", "__pycache__",
             ".omniseek", ".omniseek-inbox", ".venv", "node_modules"}

# Rule 1 (em-dash) scope: every non-code file a human reads (docs + the deploy / onboarding config
# a stranger is told to run or edit).
EM_DASH_SUFFIXES = {".md", ".yml", ".yaml", ".toml", ".cff", ".sh", ".service"}
EM_DASH_NAMES = {"Dockerfile", "NOTICE"}

# Rule 2 scope: the READMEs (GitHub and PyPI render them) and the site pages.
RAW_IMAGE = re.compile(r"https://raw\.githubusercontent\.com/[^\s\"')]+?\.(?:png|gif)(\?[^\s\"')]*)?(?=[\s\"')])")
VERSION_QUERY = re.compile(r"\?v=\d{4}-\d{2}-\d{2}[a-z]?(?:#gh-(?:light|dark)-mode-only)?$")


INLINE_CODE = re.compile(r"`[^`]*`")


def _punct(ch: str) -> bool:
    return unicodedata.category(ch).startswith(("P", "S"))


def _stuck_emphasis(text: str) -> list[int]:
    """Offsets of * runs in one paragraph that cannot do their job: a run that pairs as an opener
    (1st, 3rd, ... run of that length) but is not left-flanking, or one that pairs as a closer but
    is not right-flanking (CommonMark 0.31, section 6.2)."""
    text = INLINE_CODE.sub(lambda m: "`" * len(m.group(0)), text)
    seen: dict[int, int] = {}
    bad = []
    for m in re.finditer(r"\*+", text):
        before = text[m.start() - 1] if m.start() else " "
        after = text[m.end()] if m.end() < len(text) else " "
        if after.isspace() and before.isspace():
            continue
        left = not after.isspace() and (not _punct(after) or before.isspace() or _punct(before))
        right = not before.isspace() and (not _punct(before) or after.isspace() or _punct(after))
        k = len(m.group(0))
        opener = seen.get(k, 0) % 2 == 0
        seen[k] = seen.get(k, 0) + 1
        if (opener and not left) or (not opener and not right):
            bad.append(m.start())
    return bad


def _md_paragraphs(lines: list[str]):
    """Yield (first line number, joined text, line start offsets) for each paragraph outside code
    fences; a blank line, a fence, a heading or a new list item starts a new one."""
    block: list[tuple[int, str]] = []
    fenced = False

    def flush():
        if block:
            starts, text = [], ""
            for _, ln in block:
                starts.append(len(text))
                text += ln + " "
            yield block[0][0], text, starts
        block.clear()

    for n, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("```"):
            yield from flush()
            fenced = not fenced
            continue
        if fenced:
            continue
        if not stripped or stripped.startswith(("#", "|")) or re.match(r"(?:[-+*]|\d+\.)\s", stripped):
            yield from flush()
        if stripped:
            block.append((n, stripped))
        if stripped.startswith(("#", "|")):
            yield from flush()
    yield from flush()


def _versioned_scope(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return ((name.startswith("README") and rel.endswith(".md")) or rel == "docs/pypi.md"
            or (rel.startswith("site/") and rel.endswith(".html")))


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
        em_dash = p.suffix in EM_DASH_SUFFIXES or p.name in EM_DASH_NAMES
        versioned = _versioned_scope(rel)
        if not (em_dash or versioned):
            continue
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        if p.suffix == ".md":
            for first, text, starts in _md_paragraphs(lines):
                for off in _stuck_emphasis(text):
                    i = max(j for j, st in enumerate(starts) if st <= off)
                    violations.append((rel, first + i, "emphasis * that GitHub shows as a literal *; use <em> or add a space",
                                       text[max(0, off - 40):off + 40]))
        for n, line in enumerate(lines, 1):
            if em_dash and EM_DASH in line:
                violations.append((rel, n, "em-dash (U+2014); use : ; , . or ()", line.strip()))
            if versioned:
                for m in RAW_IMAGE.finditer(line):
                    if not VERSION_QUERY.fullmatch(m.group(1) or ""):
                        violations.append((rel, n, "image URL without ?v=YYYY-MM-DD (or YYYY-MM-DDb)", m.group(0)))

    sys.path.insert(0, str(ROOT / "scripts"))
    import gen_pypi_readme
    pypi = gen_pypi_readme.render((ROOT / "README.md").read_text(encoding="utf-8"))
    have = gen_pypi_readme.OUT.read_text(encoding="utf-8") if gen_pypi_readme.OUT.exists() else ""
    if have != pypi:
        violations.append(("docs/pypi.md", 1, "out of date with README.md; run python scripts/gen_pypi_readme.py",
                           "PyPI shows this file, not README.md"))

    if violations:
        print(f"brand_lint: FAIL, {len(violations)} violation(s)\n")
        for rel, n, why, text in violations:
            print(f"  {rel}:{n}  {why}")
            print(f"      {text[:120]}")
        print("\nFix these, or narrow the rule in scripts/brand_lint.py if it is a genuine false positive.")
        return 1
    print("brand_lint: OK (no em-dash on any human-facing surface; every raw image URL is versioned; no stuck emphasis; "
          "docs/pypi.md matches README.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
