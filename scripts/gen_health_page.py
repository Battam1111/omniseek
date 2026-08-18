"""Render a Markdown source health page from one sweep JSON document."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Optional


STATUSES = ("up", "degraded", "rate_limited", "blocked", "down", "skipped")
VANTAGE_SENTENCE = (
    "Checked from GitHub Actions runners; a residential or maintainer deployment "
    "typically reaches more. One probe per source per run; this is a health signal, "
    "not an availability guarantee."
)
_SKIPPED_POLICY_DETAILS = {
    "explicit-only",
    "requires operator credentials",
}
_SKIPPED_BREAKDOWN_FIELDS = {
    "policy": "skipped_policy",
    "capability": "skipped_capability",
    "budget": "skipped_budget",
}


def _number(value: object, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a JSON number")
    return value


def _count(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative JSON integer")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a JSON string")
    return value


def _skipped_category(detail: str) -> str:
    if detail in _SKIPPED_POLICY_DETAILS:
        return "policy"
    if detail == "sweep budget exhausted":
        return "budget"
    return "capability"


def _markdown(value: object) -> str:
    return (
        str(value)
        .replace("\N{EM DASH}", "-")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("|", "\\|")
    )


def _validate(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise ValueError("sweep JSON must be an object")
    _text(payload.get("generated_utc"), "generated_utc")
    _text(payload.get("vantage"), "vantage")
    _text(payload.get("omniseek_version"), "omniseek_version")
    _number(payload.get("sweep_seconds"), "sweep_seconds")

    sources = payload.get("sources")
    if not isinstance(sources, list):
        raise ValueError("sources must be a JSON array")
    seen: set[str] = set()
    actual = Counter()
    for index, row in enumerate(sources):
        if not isinstance(row, dict):
            raise ValueError(f"sources[{index}] must be an object")
        name = _text(row.get("name"), f"sources[{index}].name")
        if name in seen:
            raise ValueError(f"duplicate source name: {name}")
        seen.add(name)
        _text(row.get("domain"), f"sources[{index}].domain")
        _text(row.get("tier"), f"sources[{index}].tier")
        status = _text(row.get("status"), f"sources[{index}].status")
        if status not in STATUSES:
            raise ValueError(f"unknown source status: {status}")
        actual[status] += 1
        latency = row.get("latency_ms")
        if latency is not None:
            _number(latency, f"sources[{index}].latency_ms")
        if status == "skipped" and latency is not None:
            raise ValueError("skipped sources must have null latency_ms")
        _text(row.get("detail"), f"sources[{index}].detail")

    summary = payload.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("summary must be a JSON object")
    for status in STATUSES:
        count = _count(summary.get(status), f"summary.{status}")
        if count != actual[status]:
            raise ValueError(f"summary.{status} does not match sources")
    total = _count(summary.get("total"), "summary.total")
    if total != len(sources):
        raise ValueError("summary.total does not match sources")
    skipped_actual = Counter(
        _skipped_category(row["detail"])
        for row in sources
        if row["status"] == "skipped"
    )
    for category, field in _SKIPPED_BREAKDOWN_FIELDS.items():
        count = _count(summary.get(field), f"summary.{field}")
        if count != skipped_actual[category]:
            raise ValueError(f"summary.{field} does not match sources")


def render_page(payload: dict) -> str:
    _validate(payload)
    summary = payload["summary"]
    lines = [
        "# OmniSeek source health",
        "",
        (
            f"Up: {summary['up']} | Degraded: {summary['degraded']} | "
            f"Rate limited: {summary['rate_limited']} | Blocked: {summary['blocked']} | "
            f"Down: {summary['down']} | "
            f"Skipped: {summary['skipped']} | Total: {summary['total']}"
        ),
        (
            "Blocked means the source answered but refused this vantage or credential; "
            "it is not counted as Down."
        ),
        (
            f"Skipped breakdown: policy={summary['skipped_policy']} | "
            f"capability absent={summary['skipped_capability']} | "
            f"sweep budget={summary['skipped_budget']}"
        ),
        "",
        f"Generated UTC: {_markdown(payload['generated_utc'])}",
        f"Vantage: {_markdown(payload['vantage'])}",
        f"OmniSeek version: {_markdown(payload['omniseek_version'])}",
        f"Sweep duration: {_markdown(payload['sweep_seconds'])} seconds",
        "",
        VANTAGE_SENTENCE,
        "",
    ]

    by_domain: dict[str, list[dict]] = {}
    for row in payload["sources"]:
        by_domain.setdefault(row["domain"], []).append(row)

    for domain in sorted(by_domain):
        lines.extend([
            f"## {_markdown(domain)}",
            "",
            "| Source | Tier | Status | Latency | Detail |",
            "| --- | --- | --- | --- | --- |",
        ])
        for row in sorted(by_domain[domain], key=lambda item: item["name"]):
            latency = (
                "n/a"
                if row["latency_ms"] is None
                else f"{row['latency_ms']} ms"
            )
            detail = "" if row["status"] == "up" else _markdown(row["detail"])
            lines.append(
                f"| {_markdown(row['name'])} | {_markdown(row['tier'])} | "
                f"{_markdown(row['status'])} | {latency} | {detail} |"
            )
        lines.append("")

    page = "\n".join(lines)
    if "\N{EM DASH}" in page:
        raise ValueError("generated page contains an em dash")
    return page


def _parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sweep_json")
    parser.add_argument("--out", default="README.md")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    source = Path(args.sweep_json)
    payload = json.loads(source.read_text(encoding="utf-8"))
    page = render_page(payload)
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8", newline="\n")
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
