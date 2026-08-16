"""Generate docs/sources.md from the shipped catalog.

The catalog is code, so a hand-written roster would rot the day the curator admits or
retires a source. This generator imports the engine exactly the way the server does
(registration happens at source-module import), asks the same ``list_sources`` the
``omniseek_sources`` tool answers from, and rewrites the doc. The sync pipeline reruns it on
every engine update, so the doc can never drift from the code it describes.

Formatting notes: descriptions are the very strings the tool returns at runtime, lightly
brand-washed (em-dashes become colons; the repo-wide lint forbids them on reader-facing
surfaces) and truncated for the page. A source files under its FIRST domain only.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DESC_CAP = 300


def _wash(text: str) -> str:
    text = " ".join((text or "").split())
    text = text.replace(" — ", ": ").replace("—", ": ").replace("——", ": ")
    if len(text) > DESC_CAP:
        text = text[:DESC_CAP].rstrip() + " ..."
    return text


def main() -> int:
    import omniseek.server  # noqa: F401  (import-time source registration)
    from omniseek.core import fetcher

    entries = [s for s in fetcher.list_sources(verbose=True) if not s.get("retired")]
    version = re.search(r'^version = "(.*)"$',
                        (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)

    by_domain: dict[str, list[dict]] = {}
    for s in entries:
        primary = (s.get("domains") or ["general"])[0]
        by_domain.setdefault(primary, []).append(s)

    lines = [
        "# The source catalog",
        "",
        "<sub>[OmniSeek](../README.md)&nbsp;·&nbsp;[Configuration](configuration.md)&nbsp;·&nbsp;"
        "[Walled sources](walled-sources.md)&nbsp;·&nbsp;[Tools](tools.md)</sub>",
        "",
        "Every source below earned its place by beating plain web search at something, via one of",
        "five modes: structure, unwall, transcribe, recall, monitor. Access tiers: **free** is on by",
        "default; **keyed** activates once you supply the API key; **walled** stays off until you",
        "bring your own login. Sources marked `explicit-only` never join the broad sweep: name them",
        "in `sources=[...]` to use them. The catalog is not a fixed list: the built-in curator",
        "pipeline probes, judges, and admits new sources, and retires the ones that decay.",
        "",
        "**This file is generated.** `python scripts/gen_sources_doc.py` rebuilds it from the",
        "shipped catalog (the sync pipeline reruns it on every engine update); to change a line,",
        "change the source module it describes. Each description below is the same string the",
        "`omniseek_sources` tool returns at runtime, truncated for the page. A source files under",
        "its first domain only.",
        "",
        f"Generated from omniseek {version}: {len(entries)} live sources across "
        f"{len(by_domain)} domains.",
        "",
    ]

    for domain in sorted(by_domain, key=lambda d: (-len(by_domain[d]), d)):
        rows = sorted(by_domain[domain], key=lambda s: s["name"])
        lines.append(f"## {domain} ({len(rows)})")
        lines.append("")
        for s in rows:
            tags = [f"`{s.get('access_tier', 'free')}`"]
            if s.get("needs_credentials"):
                tags.append("`bring-your-own-login`")
            if s.get("explicit_only"):
                tags.append("`explicit-only`")
            if s.get("kind"):
                tags.append(f"`{s['kind']}`")
            lines.append(f"- **{s['name']}** {' '.join(tags)}: {_wash(s.get('description', ''))}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append('<div align="center"><sub><a href="../README.md">← back to the README</a></sub></div>')
    lines.append("")

    out = ROOT / "docs" / "sources.md"
    out.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"wrote {out.relative_to(ROOT)}: {len(entries)} sources, {len(by_domain)} domains")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
