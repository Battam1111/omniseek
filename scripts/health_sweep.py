"""Run a bounded live probe across the registered OmniSeek source catalog."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

STATUSES = ("up", "degraded", "rate_limited", "down", "skipped")
BUDGET_DETAIL = "sweep budget exhausted"
DETAIL_CAP = 200
_SKIPPED_POLICY_DETAILS = {
    "explicit-only",
    "requires operator credentials",
}
_RATE_LIMIT_RE = re.compile(
    r"(?:\b429\b|rate[\s_-]*limit(?:ed|ing)?|throttl(?:e|ed|ing)|too many requests)",
    re.IGNORECASE,
)
_CREDENTIAL_ALIASES = {
    "discord_communities": ("discord",),
    "llm_leaderboard": ("artificial_analysis",),
    "podcast_index": ("podcastindex",),
}


def _detail_head(message: object) -> str:
    return str(message or "")[:DETAIL_CAP]


def classify_probe(healthy: Optional[bool], message: object) -> tuple[str, str]:
    """Map the existing health result into the public status taxonomy."""
    detail = _detail_head(message)
    if healthy is None:
        return "skipped", detail
    if _RATE_LIMIT_RE.search(detail):
        return "rate_limited", detail
    if healthy is True and "degraded" in detail.lower():
        return "degraded", detail
    if healthy is True:
        return "up", ""
    return "down", detail


def _skipped_category(detail: object) -> str:
    detail_text = str(detail or "")
    if detail_text in _SKIPPED_POLICY_DETAILS:
        return "policy"
    if detail_text == BUDGET_DETAIL:
        return "budget"
    return "capability"


def build_summary(rows: list[dict]) -> dict[str, int]:
    counts = {status: 0 for status in STATUSES}
    counts.update({
        "skipped_policy": 0,
        "skipped_capability": 0,
        "skipped_budget": 0,
    })
    for row in rows:
        status = row["status"]
        if status not in STATUSES:
            raise ValueError(f"unknown source status: {status}")
        counts[status] += 1
        if status == "skipped":
            counts[f"skipped_{_skipped_category(row.get('detail'))}"] += 1
    counts["total"] = len(rows)
    return counts


def _base_row(entry: dict) -> dict:
    domains = entry.get("domains") or ["general"]
    return {
        "name": entry["name"],
        "domain": domains[0],
        "tier": entry.get("access_tier", "free"),
    }


def _skipped_row(entry: dict, detail: str) -> dict:
    return {
        **_base_row(entry),
        "status": "skipped",
        "latency_ms": None,
        "detail": detail,
    }


def sweep_sources(
    catalog: list[dict],
    *,
    get_adapter: Callable[[str], object],
    probe_adapter: Callable[[object, float], tuple[Optional[bool], object, Optional[int]]],
    credentials_configured: Callable[[str], bool],
    budget_seconds: float,
    max_workers: int = 24,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[dict], float]:
    """Probe active catalog rows without exceeding the shared wall-clock deadline."""
    started = clock()
    deadline = started + max(0.0, budget_seconds)
    active = sorted(
        (entry for entry in catalog if not entry.get("retired")),
        key=lambda entry: entry["name"],
    )
    rows_by_name: dict[str, dict] = {}
    probe_entries: list[dict] = []

    for entry in active:
        configured = credentials_configured(entry["name"])
        if entry.get("needs_credentials") and not configured:
            rows_by_name[entry["name"]] = _skipped_row(
                entry, "requires operator credentials"
            )
        elif entry.get("explicit_only") and not configured:
            rows_by_name[entry["name"]] = _skipped_row(entry, "explicit-only")
        else:
            probe_entries.append(entry)

    if clock() >= deadline:
        for entry in probe_entries:
            rows_by_name[entry["name"]] = _skipped_row(entry, BUDGET_DETAIL)
        rows = [rows_by_name[entry["name"]] for entry in active]
        return rows, round(clock() - started, 3)

    adapters = {
        entry["name"]: get_adapter(entry["name"])
        for entry in probe_entries
    }
    executor = ThreadPoolExecutor(
        max_workers=min(max_workers, len(probe_entries) or 1)
    )
    futures = {
        executor.submit(probe_adapter, adapters[entry["name"]], deadline): entry
        for entry in probe_entries
    }
    remaining = max(0.0, deadline - clock())
    done, unfinished = wait(futures, timeout=remaining)

    try:
        for future in done:
            entry = futures[future]
            healthy, message, latency_ms = future.result()
            if latency_ms is None and str(message) == BUDGET_DETAIL:
                rows_by_name[entry["name"]] = _skipped_row(entry, BUDGET_DETAIL)
                continue
            status, detail = classify_probe(healthy, message)
            rows_by_name[entry["name"]] = {
                **_base_row(entry),
                "status": status,
                "latency_ms": latency_ms,
                "detail": detail,
            }
        for future in unfinished:
            entry = futures[future]
            rows_by_name[entry["name"]] = _skipped_row(entry, BUDGET_DETAIL)
    finally:
        for future in unfinished:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)

    rows = [rows_by_name[entry["name"]] for entry in active]
    return rows, round(clock() - started, 3)


def _select_catalog(catalog: list[dict], only: str, limit: Optional[int]) -> list[dict]:
    active = [entry for entry in catalog if not entry.get("retired")]
    by_name = {entry["name"]: entry for entry in active}
    if only:
        requested = [name.strip() for name in only.split(",") if name.strip()]
        unknown = sorted(set(requested) - set(by_name))
        if unknown:
            raise ValueError("unknown or retired source(s): " + ", ".join(unknown))
        selected = [by_name[name] for name in requested]
    else:
        selected = active
    selected = sorted({entry["name"]: entry for entry in selected}.values(),
                      key=lambda entry: entry["name"])
    if limit is not None:
        selected = selected[:limit]
    return selected


def _version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _generated_utc() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _runtime():
    import omniseek.server  # noqa: F401
    from omniseek.core import auth, fetcher

    def credentials_configured(name: str) -> bool:
        candidates = (name, *_CREDENTIAL_ALIASES.get(name, ()))
        return any(auth.is_configured(candidate) for candidate in candidates)

    def probe_adapter(
        adapter: object, deadline: float
    ) -> tuple[Optional[bool], object, Optional[int]]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, BUDGET_DETAIL, None
        probe_started = time.monotonic()
        timeout = min(fetcher._HEALTH_TIMEOUT_S, remaining)
        healthy, message = fetcher.health_check_bounded(adapter, timeout=timeout)
        finished = time.monotonic()
        if finished > deadline:
            return None, BUDGET_DETAIL, None
        return healthy, message, round((finished - probe_started) * 1000)

    return fetcher, credentials_configured, probe_adapter


def _parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="health-latest.json")
    parser.add_argument("--vantage", default="local")
    parser.add_argument("--budget-seconds", type=float, default=2700)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--only", default="")
    args = parser.parse_args(argv)
    if args.budget_seconds < 0:
        parser.error("--budget-seconds must be non-negative")
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be non-negative")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    fetcher, credentials_configured, probe_adapter = _runtime()
    catalog = _select_catalog(
        fetcher.list_sources(verbose=True), args.only, args.limit
    )
    rows, sweep_seconds = sweep_sources(
        catalog,
        get_adapter=fetcher.get_adapter,
        probe_adapter=probe_adapter,
        credentials_configured=credentials_configured,
        budget_seconds=args.budget_seconds,
        max_workers=fetcher._HEALTH_WORKERS,
    )
    payload = {
        "generated_utc": _generated_utc(),
        "vantage": args.vantage,
        "omniseek_version": _version(),
        "sweep_seconds": sweep_seconds,
        "sources": rows,
        "summary": build_summary(rows),
    }
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    summary = payload["summary"]
    print(
        "summary: "
        + " ".join(f"{status}={summary[status]}" for status in STATUSES)
        + f" total={summary['total']}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(f"health sweep harness error: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
