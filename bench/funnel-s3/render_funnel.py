"""Render the S3 rebuild funnel diagram as self-contained SVGs, one per theme.

The numbers are frozen facts from bench/FUNNEL-s3.md; edit them there first, here second,
and re-render. Never retouch the emitted SVGs by hand (docs/BRAND.md: edit a master,
re-render). Palette tokens mirror bench/gen_report.py so this figure and the results chart
read as one family. Wording in the figure is plain reader language; the precise terms live
in FUNNEL-s3.md. Self-contained by construction: no script, no external font, no external
image.
"""

from __future__ import annotations

from pathlib import Path

PALETTES = {
    "light": {
        "background": "#FFFFFF",
        "text": "#0F172A",
        "muted": "#64748B",
        "track": "#E2E8F0",
        "ramp": ["#93C5FD", "#3B82F6", "#1D4ED8"],
        "amber": "#F59E0B",
    },
    "dark": {
        "background": "#0F172A",
        "text": "#E2E8F0",
        "muted": "#94A3B8",
        "track": "#334155",
        "ramp": ["#93C5FD", "#3B82F6", "#2563EB"],
        "amber": "#FBBF24",
    },
}

# (row label, count, fill role), top to bottom. Sentence case, short noun phrases;
# the blind-authoring detail lives in the subtitle.
STAGES = [
    ("Questions written", 33, "ramp0"),
    ("Answers found on the open web", 32, "ramp1"),
    ("Answers provable by one exact sentence", 13, "ramp2"),
    ("Final test set", 10, "amber"),
]

# What fell out between one row and the next, in plain words.
DROPS = [
    ["1 dropped: its author already knew the answer. 0 lacked a findable answer."],
    ["19 dropped: no single sentence settles the question (10), the sentence changes",
     "depending on how the page is fetched (5), it also appears elsewhere (3), login required (1)."],
    ["3 dropped in the final review, each with a recorded reason."],
]

# One square per final question, in task order s3-cl-001 .. -010.
# 2 = the answer sentence came back, 1 = the right page came back without it, 0 = page not in top 10.
VERIFY = [0, 0, 1, 2, 0, 1, 0, 1, 1, 0]

W = 940
LEFT = 34
BAR_MAX_W = 560
BAR_H = 30
FONT = "-apple-system, 'Segoe UI', 'Helvetica Neue', Helvetica, Arial, sans-serif"


def _bar_w(count: int) -> float:
    return BAR_MAX_W * count / STAGES[0][1]


def render(theme: str) -> str:
    p = PALETTES[theme]
    parts: list[str] = []

    # Vertical layout, computed first so the viewBox is exact.
    y = 106
    rows: list[tuple[float, float, float]] = []  # (label_y, bar_y, note_y or 0)
    for i in range(len(STAGES)):
        label_y = y
        bar_y = y + 12
        note_y = 0.0
        y = bar_y + BAR_H
        if i < len(DROPS):
            note_y = y + 24
            y = note_y + (len(DROPS[i]) - 1) * 18 + 30
        else:
            y += 26
        rows.append((label_y, bar_y, note_y))
    strip_head_y = y + 14
    strip_y = strip_head_y + 16
    height = strip_y + 26 + 22 + 30

    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" role="img" viewBox="0 0 {W} {height}" '
        f'width="{W}" height="{height}" font-family="{FONT}">'
    )
    parts.append("<title>How the 10 cross-lingual test questions were chosen</title>")
    parts.append(
        "<desc>Thirty-three real questions were written by authors who could not see our source "
        "list. Thirty-two had an answer findable on the open web. Thirteen had one exact sentence "
        "that proves the answer and survived every check. Ten form the final test set. Between the "
        "rows, every dropped question is counted with its reason. A ten-square strip shows one "
        "verification run per final question: the answer sentence came back for one, the right "
        "page without the sentence for four, and the page was not in the top ten for five.</desc>"
    )
    parts.append(f'<rect width="{W}" height="{height}" fill="{p["background"]}"/>')

    parts.append(
        f'<text x="{LEFT}" y="42" font-size="20" font-weight="600" fill="{p["text"]}">'
        "How the 10 cross-lingual test questions were chosen</text>"
    )
    parts.append(
        f'<text x="{LEFT}" y="68" font-size="13.5" fill="{p["muted"]}">'
        "The authors wrote 33 real questions without seeing our source list; the checks below cut them to 10.</text>"
    )

    for i, (label, count, role) in enumerate(STAGES):
        label_y, bar_y, note_y = rows[i]
        w = _bar_w(count)
        fill = p["amber"] if role == "amber" else p["ramp"][int(role[-1])]
        parts.append(
            f'<text x="{LEFT}" y="{label_y:.1f}" font-size="14" font-weight="600" '
            f'fill="{p["text"]}">{label}</text>'
        )
        # A quiet full-length track keeps every row the same visual width, so the shrinking
        # colored bar reads as a proportion of the starting 33.
        parts.append(
            f'<rect x="{LEFT}" y="{bar_y:.1f}" width="{BAR_MAX_W}" height="{BAR_H}" rx="7" '
            f'fill="{p["track"]}" opacity="0.45"/>'
        )
        parts.append(
            f'<rect x="{LEFT}" y="{bar_y:.1f}" width="{w:.1f}" height="{BAR_H}" rx="7" fill="{fill}"/>'
        )
        parts.append(
            f'<text x="{LEFT + BAR_MAX_W + 16}" y="{bar_y + BAR_H / 2 + 6:.1f}" font-size="18" '
            f'font-weight="700" fill="{p["text"]}">{count}</text>'
        )
        if note_y:
            for j, line in enumerate(DROPS[i]):
                parts.append(
                    f'<text x="{LEFT + 14}" y="{note_y + j * 18:.1f}" font-size="12.5" '
                    f'fill="{p["muted"]}">{line}</text>'
                )

    parts.append(
        f'<text x="{LEFT}" y="{strip_head_y}" font-size="14" font-weight="600" fill="{p["text"]}">'
        "Each final question was then run through OmniSeek once (2026-08-22):</text>"
    )
    sq, gap = 24, 8
    colors = {2: p["amber"], 1: p["ramp"][1], 0: p["track"]}
    for i, v in enumerate(VERIFY):
        x = LEFT + i * (sq + gap)
        parts.append(f'<rect x="{x}" y="{strip_y}" width="{sq}" height="{sq}" rx="6" fill="{colors[v]}"/>')
        parts.append(
            f'<text x="{x + sq / 2}" y="{strip_y + sq + 15}" font-size="10" '
            f'text-anchor="middle" fill="{p["muted"]}">{i + 1:03d}</text>'
        )
    legend_x = LEFT + 10 * (sq + gap) + 26
    legend = [
        (colors[2], "Answer sentence returned (1)"),
        (colors[1], "Right page returned, answer sentence missing (4)"),
        (colors[0], "Right page not in the top 10 (5)"),
    ]
    ly = strip_y - 3
    for color, text in legend:
        parts.append(f'<rect x="{legend_x}" y="{ly}" width="11" height="11" rx="3" fill="{color}"/>')
        parts.append(f'<text x="{legend_x + 18}" y="{ly + 10}" font-size="12.5" fill="{p["muted"]}">{text}</text>')
        ly += 20

    parts.append("</svg>")
    return "".join(parts) + "\n"


def main() -> None:
    here = Path(__file__).resolve().parent
    for theme in PALETTES:
        out = here / f"funnel-{theme}.svg"
        out.write_text(render(theme), encoding="utf-8", newline="\n")
        print(f"wrote {out.name} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
