"""Render the generated benchmark Markdown page from one results JSON file."""

from __future__ import annotations

import argparse
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
    n_value = suite.get("n")
    if n_value is None:
        n_value = pooled.get("total")
    n = _emit_integer(n_value, available, f"{suite_name}.n")

    rate_value = pooled.get("rate")
    interval = pooled.get("wilson_95") or pooled.get("wilson")
    if rate_value is None or not isinstance(interval, list) or len(interval) != 2:
        raise ValueError(f"{suite_name} is missing pooled rate or Wilson interval")
    rate = _emit_number(rate_value, available, f"{suite_name}.pooled.rate")
    low = _emit_number(interval[0], available, f"{suite_name}.pooled.wilson.low")
    high = _emit_number(interval[1], available, f"{suite_name}.pooled.wilson.high")

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
    return n, f"{rate} [{low}, {high}]", noise, p50, p90, stale, (
        str(suite.get("dormant_note") or "yes")
        if suite.get("dormant")
        else "no"
    )


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

    rows: list[str] = []
    for suite_name in sorted(suites):
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
    dormant_lines = "\n".join(f"- `{suite}`" for suite in dormant) or "- none"
    env = json.dumps(results.get("env", {}), ensure_ascii=False, indent=2, sort_keys=True)
    conflict = _conflict_paragraph(design_file)

    report = "\n".join(
        [
            "# OmniSeek benchmark results",
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
            "## Conflict of interest",
            "",
            conflict,
            "",
        ]
    )
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(report, encoding="utf-8")
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
