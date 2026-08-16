"""Render the generated benchmark Markdown page from one results JSON file."""

from __future__ import annotations

import argparse
import html
import json
import math
from pathlib import Path
from typing import Any


def _json_numbers(value: Any) -> set[float]:
    numbers: set[float] = set()
    if isinstance(value, bool):
        return numbers
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        numbers.add(float(value))
    elif isinstance(value, dict):
        for child in value.values():
            numbers.update(_json_numbers(child))
    elif isinstance(value, list):
        for child in value:
            numbers.update(_json_numbers(child))
    return numbers


def _emit_number(value: Any, available: set[float], label: str, decimals: int = 4) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} is not numeric in results JSON")
    if not math.isfinite(float(value)) or float(value) not in available:
        raise ValueError(f"{label} is not present as a numeric value in results JSON")
    return f"{float(value):.{decimals}f}"


def _emit_integer(value: Any, available: set[float], label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} is not an integer in results JSON")
    if float(value) not in available:
        raise ValueError(f"{label} is not present as a numeric value in results JSON")
    return str(value)


def _emit_record_count(value: int, label: str) -> str:
    """Emit a count of records that this page itself counted.

    The _emit_number guard exists so that no MEASUREMENT is typed by hand. A count of
    records present in the parsed JSON is not a measurement copied out of the JSON: it is
    computed from the JSON's own structure, so demanding that it appear as a literal JSON
    number would reject correct values (a run carrying three receipts whose JSON happens
    to contain no literal 3). The guard that does apply is the one that can be checked
    here: the value must be a plain non-negative integer produced by counting.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative count")
    return str(value)


def _xml_text(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _xml_attr(value: Any) -> str:
    return html.escape(str(value), quote=True)


# Chart geometry. Layout only: no measurement is ever read from these.
_CHART_WIDTH = 1000
_CHART_HEADER = 78
_CHART_FOOTER = 56
_CHART_ROW = 56
_LABEL_X = 24
_TRACK_X = 232
_TRACK_WIDTH = 430
_VALUE_X = 686
_BAR_HEIGHT = 20

_PALETTES = {
    "light": {
        "background": "#FFFFFF",
        "text": "#0F172A",
        "muted": "#64748B",
        "track": "#E2E8F0",
        "bar": "#2563EB",
        "interval": "#B45309",
        "dormant": "#B45309",
        "axis": "#CBD5E1",
    },
    "dark": {
        "background": "#0F172A",
        "text": "#E2E8F0",
        "muted": "#94A3B8",
        "track": "#334155",
        "bar": "#60A5FA",
        "interval": "#FBBF24",
        "dormant": "#FBBF24",
        "axis": "#334155",
    },
}

_DORMANT_FALLBACK_NOTE = "required optional extra missing"


def _is_dormant(results: dict[str, Any], suite_name: str, suite: dict[str, Any]) -> bool:
    declared = results.get("dormant")
    listed = suite_name in declared if isinstance(declared, list) else False
    return bool(suite.get("dormant")) or listed


def _dormant_note(suite: dict[str, Any]) -> str:
    note = suite.get("dormant_note")
    if isinstance(note, str) and note.strip():
        return note
    return _DORMANT_FALLBACK_NOTE


def _fraction(value: Any) -> float:
    return max(0.0, min(1.0, float(value)))


def _chart_row_values(
    suite_name: str,
    suite: dict[str, Any],
    available: set[float],
) -> dict[str, Any]:
    """Validated, ready-to-draw values for one measured suite row.

    Every string returned here has passed the results-JSON guard, and every geometry
    number the caller draws is a function of these same validated values, so the chart
    cannot show a number the run did not produce.
    """
    pooled = suite.get("pooled")
    if not isinstance(pooled, dict):
        raise ValueError(f"{suite_name} is missing pooled results for the chart")
    interval = pooled.get("wilson_95")
    if not isinstance(interval, list):
        interval = pooled.get("wilson")
    if not isinstance(interval, list) or len(interval) != 2:
        raise ValueError(f"{suite_name} is missing a two-sided Wilson interval")
    successes = _emit_integer(
        pooled.get("successes"), available, f"{suite_name}.pooled.successes"
    )
    total = _emit_integer(pooled.get("total"), available, f"{suite_name}.pooled.total")
    rate = _emit_number(pooled.get("rate"), available, f"{suite_name}.pooled.rate")
    low = _emit_number(interval[0], available, f"{suite_name}.pooled.wilson.low")
    high = _emit_number(interval[1], available, f"{suite_name}.pooled.wilson.high")
    return {
        "successes": successes,
        "total": total,
        "rate": rate,
        "low": low,
        "high": high,
        "rate_fraction": _fraction(pooled.get("rate")),
        "low_fraction": _fraction(interval[0]),
        "high_fraction": _fraction(interval[1]),
    }


def _stale_label(
    suite_name: str,
    suite: dict[str, Any],
    available: set[float],
) -> str:
    """Return "stale N" when this suite dropped tasks from its denominator, else "".

    Drawn for dormant rows too: a dormant sense and a rotted upstream are separate facts
    and a run must not hide the second behind the first.
    """
    count = suite.get("stale_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError(f"{suite_name}.stale_count must be a non-negative integer")
    if count == 0:
        return ""
    return "stale " + _emit_integer(count, available, f"{suite_name}.stale_count")


def render_results_svg(results: dict[str, Any], theme: str = "light") -> str:
    """Render the per-suite pass-rate chart as one self-contained SVG string.

    Self-contained by construction: no script, no external font, no external image and
    no stylesheet, so it renders unchanged inside a GitHub markdown page and inside a
    plain img tag.
    """
    if theme not in _PALETTES:
        raise ValueError("chart theme must be light or dark")
    palette = _PALETTES[theme]
    available = _json_numbers(results)
    suites = results.get("suites")
    if not isinstance(suites, dict):
        raise ValueError("results JSON must contain suites")
    suite_names = sorted(suites)

    height = _CHART_HEADER + _CHART_ROW * len(suite_names) + _CHART_FOOTER
    parts: list[str] = [
        (
            '<svg xmlns="http://www.w3.org/2000/svg" role="img" '
            f'width="{_CHART_WIDTH}" height="{height}" '
            f'viewBox="0 0 {_CHART_WIDTH} {height}" '
            'font-family="Segoe UI, Helvetica, Arial, sans-serif">'
        ),
        "<title>OmniSeek benchmark: pooled pass rate by suite</title>",
        (
            "<desc>One row per benchmark suite. The filled bar is that suite's pooled "
            "pass rate, drawn on a track that spans a pass rate of zero at its left edge "
            "to one at its right edge. The bracket across the bar spans the Wilson "
            "interval. The text on the right is passes over attempts and the rate, plus a "
            "stale count when tasks were dropped from the denominator. A suite whose "
            "required sense is not installed is drawn as a dormant row rather than as a "
            "zero bar, because a dormant sense is not a failure.</desc>"
        ),
        (
            f'<rect x="0" y="0" width="{_CHART_WIDTH}" height="{height}" '
            f'fill="{palette["background"]}"/>'
        ),
        (
            f'<text x="{_LABEL_X}" y="34" font-size="19" font-weight="600" '
            f'fill="{palette["text"]}">OmniSeek benchmark: pooled pass rate by suite</text>'
        ),
        (
            f'<text x="{_LABEL_X}" y="58" font-size="13" fill="{palette["muted"]}">'
            "Bar: pooled pass rate. Bracket: Wilson interval. A dormant sense is reported, "
            "not scored.</text>"
        ),
    ]

    for index, suite_name in enumerate(suite_names):
        suite = suites[suite_name]
        if not isinstance(suite, dict):
            raise ValueError(f"suite {suite_name} must be an object")
        top = _CHART_HEADER + index * _CHART_ROW
        middle = top + _CHART_ROW / 2
        baseline = middle + 5
        dormant = _is_dormant(results, suite_name, suite)
        stale = _stale_label(suite_name, suite, available)
        parts.append(
            f'<g data-suite="{_xml_attr(suite_name)}" '
            f'data-state="{"dormant" if dormant else "measured"}">'
        )
        if dormant:
            summary = f"{suite_name}: sense dormant, {_dormant_note(suite)}"
            if stale:
                summary = f"{summary}, {stale}"
            parts.append(f"<title>{_xml_text(summary)}</title>")
            parts.append(
                f'<text x="{_LABEL_X}" y="{baseline:.1f}" font-size="15" '
                f'fill="{palette["text"]}">{_xml_text(suite_name)}</text>'
            )
            # No track rectangle and no bar: there is no measurement, so none is drawn.
            # A dashed rule marks where a bar would have sat, and the note sits on the
            # same baseline as the suite name so the row still reads as one line.
            parts.append(
                f'<line x1="{_TRACK_X}" y1="{middle + 12:.1f}" '
                f'x2="{_TRACK_X + _TRACK_WIDTH}" y2="{middle + 12:.1f}" '
                f'stroke="{palette["axis"]}" stroke-width="2" stroke-dasharray="5 5"/>'
            )
            label = f"sense dormant: {_dormant_note(suite)}"
            if stale:
                label = f"{label} ({stale})"
            parts.append(
                f'<text x="{_TRACK_X}" y="{baseline:.1f}" font-size="13" '
                f'fill="{palette["dormant"]}">{_xml_text(label)}</text>'
            )
            parts.append("</g>")
            continue

        values = _chart_row_values(suite_name, suite, available)
        summary = (
            f"{suite_name}: pooled pass rate {values['rate']}, "
            f"Wilson interval {values['low']} to {values['high']}, "
            f"{values['successes']} of {values['total']} attempts passed"
        )
        if stale:
            summary = f"{summary}, {stale}"
        parts.append(f"<title>{_xml_text(summary)}</title>")
        parts.append(
            f'<text x="{_LABEL_X}" y="{baseline:.1f}" font-size="15" '
            f'fill="{palette["text"]}">{_xml_text(suite_name)}</text>'
        )
        bar_top = middle - _BAR_HEIGHT / 2
        bar_width = values["rate_fraction"] * _TRACK_WIDTH
        low_x = _TRACK_X + values["low_fraction"] * _TRACK_WIDTH
        high_x = _TRACK_X + values["high_fraction"] * _TRACK_WIDTH
        bracket_top = bar_top - 5
        bracket_bottom = bar_top + _BAR_HEIGHT + 5
        parts.extend(
            [
                (
                    f'<rect x="{_TRACK_X}" y="{bar_top:.1f}" width="{_TRACK_WIDTH}" '
                    f'height="{_BAR_HEIGHT}" rx="4" fill="{palette["track"]}"/>'
                ),
                (
                    f'<rect x="{_TRACK_X}" y="{bar_top:.1f}" width="{bar_width:.2f}" '
                    f'height="{_BAR_HEIGHT}" rx="4" fill="{palette["bar"]}"/>'
                ),
                (
                    f'<line x1="{low_x:.2f}" y1="{middle:.1f}" x2="{high_x:.2f}" '
                    f'y2="{middle:.1f}" stroke="{palette["interval"]}" stroke-width="2"/>'
                ),
                (
                    f'<line x1="{low_x:.2f}" y1="{bracket_top:.1f}" x2="{low_x:.2f}" '
                    f'y2="{bracket_bottom:.1f}" stroke="{palette["interval"]}" '
                    'stroke-width="2"/>'
                ),
                (
                    f'<line x1="{high_x:.2f}" y1="{bracket_top:.1f}" x2="{high_x:.2f}" '
                    f'y2="{bracket_bottom:.1f}" stroke="{palette["interval"]}" '
                    'stroke-width="2"/>'
                ),
            ]
        )
        label = f"{values['successes']}/{values['total']}  {values['rate']}"
        if stale:
            label = f"{label}  {stale}"
        parts.append(
            f'<text x="{_VALUE_X}" y="{baseline:.1f}" font-size="14" '
            f'font-family="Consolas, Menlo, monospace" fill="{palette["text"]}">'
            f"{_xml_text(label)}</text>"
        )
        parts.append("</g>")

    axis_y = _CHART_HEADER + _CHART_ROW * len(suite_names) + 12
    parts.extend(
        [
            (
                f'<line x1="{_TRACK_X}" y1="{axis_y}" x2="{_TRACK_X + _TRACK_WIDTH}" '
                f'y2="{axis_y}" stroke="{palette["axis"]}" stroke-width="1"/>'
            ),
            (
                f'<line x1="{_TRACK_X}" y1="{axis_y - 4}" x2="{_TRACK_X}" '
                f'y2="{axis_y + 4}" stroke="{palette["axis"]}" stroke-width="1"/>'
            ),
            (
                f'<line x1="{_TRACK_X + _TRACK_WIDTH}" y1="{axis_y - 4}" '
                f'x2="{_TRACK_X + _TRACK_WIDTH}" y2="{axis_y + 4}" '
                f'stroke="{palette["axis"]}" stroke-width="1"/>'
            ),
            (
                f'<text x="{_TRACK_X}" y="{axis_y + 22}" font-size="12" '
                f'fill="{palette["muted"]}">Track: a pass rate of zero at the left edge, '
                "one at the right edge.</text>"
            ),
            "</svg>",
        ]
    )
    return "\n".join(parts)


# Why a suite can carry no web-search receipt at all. These are scope statements from the
# benchmark charter, not gaps: neither suite has a plain web result that a baseline could
# have been recorded against.
_NO_RECEIPT_REASONS = {
    "s5-scholar": (
        "the suite tests structured scholarly evidence, and its ground truth is re-fetched "
        "from the upstream source of record at judge time, so there is no first-page web "
        "result for a baseline to be recorded against"
    ),
    "s6-memory": (
        "the suite tests the server's own memory contract, which is observable only inside "
        "a session and is published nowhere for a search engine to return"
    ),
}
_NO_RECEIPT_FALLBACK = "no task record in this run carries a recorded receipt"


def _receipt_fields(task_id: str, receipt: Any) -> tuple[str, str, str, bool]:
    if not isinstance(receipt, dict):
        raise ValueError(f"{task_id} receipt must be an object")
    tool = receipt.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        tool = receipt.get("engine")
    query = receipt.get("query")
    date = receipt.get("date")
    hit = receipt.get("first_page_hit")
    if not isinstance(tool, str) or not tool.strip():
        raise ValueError(f"{task_id} receipt must name the tool or engine")
    if not isinstance(query, str) or not query.strip():
        raise ValueError(f"{task_id} receipt must record the query")
    if not isinstance(date, str) or not date.strip():
        raise ValueError(f"{task_id} receipt must record the date")
    if not isinstance(hit, bool):
        raise ValueError(f"{task_id} receipt first_page_hit must be a boolean")
    return tool, query, date, hit


def _baseline_section(results: dict[str, Any], suite_names: list[str]) -> list[str]:
    """Render the recorded search-resistance receipts the runner carried into the JSON."""
    tasks = results.get("tasks")
    if tasks is None:
        tasks = {}
    if not isinstance(tasks, dict):
        raise ValueError("tasks must be an object in results JSON")

    by_suite: dict[str, list[tuple[str, list[Any]]]] = {}
    for task_id in sorted(tasks):
        record = tasks[task_id]
        if not isinstance(record, dict):
            continue
        receipts = record.get("search_resistance_prefilter")
        if not isinstance(receipts, list) or not receipts:
            continue
        suite = record.get("suite")
        if not isinstance(suite, str):
            continue
        by_suite.setdefault(suite, []).append((str(record.get("id", task_id)), receipts))

    rows: list[str] = []
    items: list[str] = []
    has_first_page_hit = False
    for suite_name in suite_names:
        carried = by_suite.get(suite_name)
        if not carried:
            continue
        hit_tasks = 0
        for task_id, receipts in carried:
            fields = [_receipt_fields(task_id, receipt) for receipt in receipts]
            if any(hit for _, _, _, hit in fields):
                hit_tasks += 1
                has_first_page_hit = True
            for tool, query, date, hit in fields:
                items.append(
                    "<li><code>{task}</code>: {tool} on {date}, first-page hit "
                    "{hit}, query <code>{query}</code></li>".format(
                        task=_xml_text(task_id),
                        tool=_xml_text(tool),
                        date=_xml_text(date),
                        hit="yes" if hit else "no",
                        query=_xml_text(query),
                    )
                )
        rows.append(
            "| {suite} | {carried} | {hits} |".format(
                suite=suite_name,
                carried=_emit_record_count(len(carried), f"{suite_name} receipt count"),
                hits=_emit_record_count(hit_tasks, f"{suite_name} first-page hit count"),
            )
        )

    lines = ["## Reachability baseline", ""]
    if rows:
        lines.extend(
            [
                (
                    "A task enters these suites only after a recorded query failed to "
                    "surface its ground truth on the first page of a plain web search. "
                    "Those receipts travel with the run, so the floor the rates above are "
                    "measured against is on this page rather than buried in a task file."
                ),
                "",
                (
                    "| Suite | tasks carrying a receipt | "
                    "tasks whose receipt records a first-page hit |"
                ),
                "| --- | ---: | ---: |",
                *rows,
                "",
                "<details>",
                "<summary>Recorded receipts, one line per logged query</summary>",
                "<ul>",
                *items,
                "</ul>",
                "</details>",
                "",
                (
                    (
                        "What the receipts do and do not show: these are first-page "
                        "results for the recorded query on the recorded date, not proof "
                        "that no query could ever surface the answer. Engine "
                        "personalization, the region a query ran from, and any indexing "
                        "that happened afterwards were neither controlled nor recorded."
                    )
                    if has_first_page_hit
                    else (
                        "What the receipts do and do not show: these are first-page "
                        "non-hits for the recorded query on the recorded date, not proof "
                        "that no query could ever surface the answer. Engine "
                        "personalization, the region a query ran from, and any indexing "
                        "that happened afterwards were neither controlled nor recorded."
                    )
                ),
                "",
            ]
        )
    else:
        lines.extend(["No task record in this run carries a recorded receipt.", ""])

    missing = [name for name in suite_names if not by_suite.get(name)]
    if missing:
        lines.extend(["Baseline not applicable:", ""])
        for suite_name in missing:
            reason = _NO_RECEIPT_REASONS.get(suite_name, _NO_RECEIPT_FALLBACK)
            lines.append(f"- `{suite_name}`: baseline not applicable, because {reason}.")
        lines.append("")
    return lines


def _conflict_paragraph(design_file: Path) -> str:
    text = design_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    for index, line in enumerate(lines):
        marker = "Conflict-of-interest note, printed on the results page:"
        if marker not in line:
            continue
        first = line.strip()
        if first.startswith("- "):
            first = first[2:]
        paragraph = [first]
        for continuation in lines[index + 1 :]:
            if not continuation.strip():
                break
            paragraph.append(continuation.strip())
        return " ".join(paragraph)
    raise ValueError(f"publishing conflict paragraph not found in {design_file}")


def _suite_values(
    suite_name: str,
    suite: dict[str, Any],
    available: set[float],
) -> tuple[str, str, str, str, str, str, str]:
    pooled = suite.get("pooled") or {}
    dormant = bool(suite.get("dormant"))
    n_value = suite.get("n")
    if n_value is None:
        n_value = pooled.get("total")
    n = "n/a" if (dormant and n_value is None) else _emit_integer(
        n_value, available, f"{suite_name}.n"
    )

    rate_value = pooled.get("rate")
    interval = pooled.get("wilson_95") or pooled.get("wilson")
    if dormant:
        # A dormant sense never ran, so it has no rate. Printing 0.0000 here would read
        # as "failed everything", which is the reading the charter exists to prevent,
        # and it would contradict the chart, which draws this suite as dormant.
        scored = "not scored"
    else:
        if rate_value is None or not isinstance(interval, list) or len(interval) != 2:
            raise ValueError(f"{suite_name} is missing pooled rate or Wilson interval")
        rate = _emit_number(rate_value, available, f"{suite_name}.pooled.rate")
        low = _emit_number(interval[0], available, f"{suite_name}.pooled.wilson.low")
        high = _emit_number(interval[1], available, f"{suite_name}.pooled.wilson.high")
        scored = f"{rate} [{low}, {high}]"

    noise_value = suite.get("noise_band")
    noise = "n/a" if noise_value is None else _emit_number(
        noise_value, available, f"{suite_name}.noise_band"
    )
    latency = suite.get("latency_ms") or {}
    p50_value = latency.get("p50")
    p90_value = latency.get("p90")
    p50 = "n/a" if p50_value is None else _emit_number(
        p50_value, available, f"{suite_name}.latency_ms.p50", 2
    )
    p90 = "n/a" if p90_value is None else _emit_number(
        p90_value, available, f"{suite_name}.latency_ms.p90", 2
    )
    stale = _emit_integer(
        suite.get("stale_count", 0),
        available,
        f"{suite_name}.stale_count",
    )
    return n, scored, noise, p50, p90, stale, (
        str(suite.get("dormant_note") or "yes")
        if suite.get("dormant")
        else "no"
    )


# The repo's README pattern for a themed image: a dark source, a light img fallback.
_CHART_PICTURE = [
    "<picture>",
    '  <source media="(prefers-color-scheme: dark)" srcset="results-dark.svg">',
    (
        '  <img src="results-light.svg" alt="Horizontal bar chart, one row per benchmark '
        "suite: the pooled pass rate as a bar, the Wilson interval as a bracket across it, "
        'and a dormant sense reported as dormant rather than as a zero bar.">'
    ),
    "</picture>",
]


def generate_report(
    result_file: str | Path,
    output_file: str | Path = Path("bench") / "RESULTS.md",
    design_file: str | Path | None = None,
) -> Path:
    result_file = Path(result_file)
    output_file = Path(output_file)
    if design_file:
        design_file = Path(design_file)
    else:
        candidate_design = result_file.parent.parent / "DESIGN.md"
        design_file = (
            candidate_design
            if candidate_design.exists()
            else Path(__file__).resolve().parent / "DESIGN.md"
        )
    results = json.loads(result_file.read_text(encoding="utf-8"))
    available = _json_numbers(results)
    suites = results.get("suites")
    if not isinstance(suites, dict):
        raise ValueError("results JSON must contain suites")

    suite_names = sorted(suites)
    rows: list[str] = []
    for suite_name in suite_names:
        values = _suite_values(suite_name, suites[suite_name], available)
        rows.append(f"| {suite_name} | " + " | ".join(values) + " |")

    stale = results.get("stale", [])
    dormant = results.get("dormant", [])
    if not isinstance(stale, list) or not isinstance(dormant, list):
        raise ValueError("stale and dormant must be lists")
    stale_lines = "\n".join(
        (
            f"- `{entry.get('id')}` ({entry.get('class')})"
            if isinstance(entry, dict)
            else f"- `{entry}`"
        )
        for entry in stale
    ) or "- none"
    dormant_lines = "\n".join(
        f"- `{suite}` ({_dormant_note(suites.get(suite) or {})})" for suite in dormant
    ) or "- none"
    env = json.dumps(results.get("env", {}), ensure_ascii=False, indent=2, sort_keys=True)
    conflict = _conflict_paragraph(design_file)

    report = "\n".join(
        [
            "# OmniSeek benchmark results",
            "",
            *_CHART_PICTURE,
            "",
            "| Suite | n | rate [Wilson interval] | noise band | p50 ms | p90 ms | stale | dormant |",
            "| --- | ---: | --- | ---: | ---: | ---: | ---: | --- |",
            *rows,
            "",
            "## Environment",
            "",
            "```json",
            env,
            "```",
            "",
            "## Stale task ids",
            "",
            stale_lines,
            "",
            "## Dormant suites",
            "",
            dormant_lines,
            "",
            *_baseline_section(results, suite_names),
            "## Conflict of interest",
            "",
            conflict,
            "",
        ]
    )
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(report, encoding="utf-8")
    for theme in ("light", "dark"):
        chart_file = output_file.parent / f"results-{theme}.svg"
        chart_file.write_text(
            render_results_svg(results, theme=theme) + "\n",
            encoding="utf-8",
        )
    return output_file


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_file", type=Path)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("bench") / "RESULTS.md",
    )
    parser.add_argument("--design", type=Path)
    args = parser.parse_args()
    generate_report(args.result_file, args.out, args.design)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
