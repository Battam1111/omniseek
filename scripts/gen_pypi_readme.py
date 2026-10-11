#!/usr/bin/env python3
"""Write docs/pypi.md, the page PyPI shows for the package, from the English README.

PyPI does not know GitHub's theme fragments (#gh-light-mode-only / #gh-dark-mode-only), so the
README's light and dark pictures would both show there, one under the other. PyPI gets its own copy
instead: the README from the top through the "See the difference" section, each themed pair cut down
to the light image with no fragment and no <picture> (PyPI drops <source>). Everything else is the
README's text as written, so edit README.md, never docs/pypi.md, then run this.

    python scripts/gen_pypi_readme.py           # write docs/pypi.md
    python scripts/gen_pypi_readme.py --check   # exit 1 if docs/pypi.md is not what README.md gives

scripts/brand_lint.py runs the check, so the two cannot drift apart. pyproject.toml points `readme`
at docs/pypi.md.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
OUT = ROOT / "docs" / "pypi.md"
LAST_SECTION = "## See the difference"
NOTE = "<!-- Generated from README.md by scripts/gen_pypi_readme.py: edit README.md, then run it. -->"

PAIR = re.compile(
    r'(?P<ind>[ \t]*)<a href="[^"]*#gh-(?P<theme>light|dark)-mode-only"><picture>\s*'
    r'(?:<source [^>]*>\s*)*'
    r'<img src="(?P<src>[^"#]*)#gh-(?P=theme)-mode-only"(?P<rest>[^>]*)>\s*'
    r'</picture></a>[ \t]*\n')


def render(readme: str) -> str:
    lines = readme.splitlines()
    start = lines.index(LAST_SECTION)
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "---")
    head = "\n".join(lines[:end]).rstrip() + "\n"

    def one(m: re.Match) -> str:
        if m.group("theme") == "dark":
            return ""
        return f'{m.group("ind")}<img src="{m.group("src")}"{m.group("rest")}>\n'

    text = PAIR.sub(one, head)
    if "mode-only" in text or "<picture" in text or "<source" in text:
        raise SystemExit("gen_pypi_readme: a themed image in README.md is not in the expected form "
                         "(docs/BRAND.md, Rules); fix README.md or this script")
    first, _, rest = text.partition("\n")
    if not first.startswith("<!-- mcp-name:"):
        raise SystemExit("gen_pypi_readme: README.md must start with the mcp-name comment "
                         "(the MCP Registry reads it from the PyPI description)")
    return f"{first}\n{NOTE}\n{rest}"


def main() -> int:
    want = render(README.read_text(encoding="utf-8"))
    if "--check" in sys.argv[1:]:
        have = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if have != want:
            print("gen_pypi_readme: docs/pypi.md is out of date with README.md; "
                  "run python scripts/gen_pypi_readme.py")
            return 1
        return 0
    OUT.write_text(want, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
