"""Run the OmniSeek benchmark against a real MCP server."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import importlib.metadata
import importlib.util
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from judges import judge, wilson_interval
from schema import TaskValidationError, load_tasks


DEFAULT_SUITES = "s3-crosslingual,s4-depth,s5-scholar,s6-memory"
WARMUP_TIMEOUT = 600.0
WARMUP_QUERY = "benchmark neutral warmup"
REQUIRED_EXTRAS = {
    "s1-audio": {"asr"},
    "s2-pixels": {"ocr"},
    "s3-crosslingual": set(),
    "s4-depth": set(),
    "s5-scholar": set(),
    "s6-memory": set(),
}
EXTRA_IMPORTS = {
    "asr": ("funasr", "imageio_ffmpeg"),
    "ocr": ("rapidocr_onnxruntime",),
    "pdf": ("fitz",),
    "recall": ("sentence_transformers",),
    "walled": ("patchright", "xhshow"),
}


class HarnessError(RuntimeError):
    """Raised for errors in the benchmark harness itself."""


class MCPCallError(RuntimeError):
    """Raised when the MCP server returns a tool error."""


class DormantExtraError(MCPCallError):
    """Raised when the server explicitly reports a missing optional extra."""


def _git_sha() -> str:
    try:
        output = subprocess.check_output(
            ["git", "rev-parse", "--short=7", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return output.strip() or "unknown"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def detect_extras() -> dict[str, bool]:
    return {
        extra: all(importlib.util.find_spec(module) is not None for module in modules)
        for extra, modules in EXTRA_IMPORTS.items()
    }


def _required_extras(task: dict[str, Any]) -> set[str]:
    explicit = task.get("required_extras")
    if isinstance(explicit, list):
        return {str(value) for value in explicit}
    truth_extra = task.get("ground_truth", {}).get("required_extra")
    if isinstance(truth_extra, str):
        return {truth_extra}
    required = set(REQUIRED_EXTRAS.get(task["suite"], set()))
    tool = task.get("input", {}).get("tool")
    args = task.get("input", {}).get("args", {})
    if tool == "omniseek_transcribe":
        required.add("asr")
    if tool == "omniseek_read" and args.get("ocr"):
        required.add("ocr")
    target = str(args.get("target", "")).split("?", 1)[0].casefold()
    if tool == "omniseek_read" and target.endswith(".pdf"):
        required.add("pdf")
    return required


def _is_dormant_message(message: str) -> bool:
    lowered = message.casefold()
    return (
        "install omniseek[" in lowered
        or "optional extra" in lowered
        or "extra is not installed" in lowered
        or "dependency is unavailable" in lowered
    )


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction)


def _rate(successes: int, total: int) -> float:
    return successes / total if total else 0.0


def _decode_mcp_result(result: Any) -> Any:
    if getattr(result, "isError", False):
        text = " ".join(
            str(getattr(block, "text", ""))
            for block in getattr(result, "content", [])
        )
        raise MCPCallError(text or "MCP tool returned an error")
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    blocks = getattr(result, "content", [])
    texts = [getattr(block, "text", "") for block in blocks if hasattr(block, "text")]
    if not texts:
        return None
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except json.JSONDecodeError:
            return texts[0]
    combined = "\n".join(texts)
    try:
        return json.loads(combined)
    except json.JSONDecodeError:
        return combined


class MCPInvoker:
    """One initialized MCP session, backed by stdio or streamable HTTP."""

    def __init__(
        self,
        transport: str,
        command: str,
        url: str | None,
        init_timeout: float,
        call_timeout: float,
    ):
        self.transport = transport
        self.command = command
        self.url = url
        self.init_timeout = init_timeout
        self.call_timeout = call_timeout
        self._stack: contextlib.AsyncExitStack | None = None
        self.session = None

    async def __aenter__(self):
        from mcp import ClientSession, StdioServerParameters, stdio_client

        self._stack = contextlib.AsyncExitStack()
        await self._stack.__aenter__()
        if self.transport == "stdio":
            params = StdioServerParameters(
                command=self.command,
                args=[],
                cwd=str(Path.cwd()),
                env=dict(os.environ),
            )
            streams = await self._stack.enter_async_context(stdio_client(params))
        else:
            if not self.url:
                raise HarnessError("--url is required for HTTP transport")
            from mcp.client.streamable_http import streamable_http_client

            streams = await self._stack.enter_async_context(
                streamable_http_client(
                    self.url,
                    timeout=self.call_timeout,
                    sse_read_timeout=max(self.call_timeout, self.init_timeout),
                )
            )
        self.session = await self._stack.enter_async_context(
            ClientSession(streams[0], streams[1])
        )
        try:
            await asyncio.wait_for(self.session.initialize(), self.init_timeout)
        except Exception:
            await self.__aexit__(*sys.exc_info())
            raise
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        if self._stack is not None:
            return await self._stack.__aexit__(exc_type, exc, traceback)
        return None

    async def call(self, tool: str, args: dict[str, Any], timeout_s: float) -> Any:
        if self.session is None:
            raise HarnessError("MCP session is not initialized")
        try:
            raw = await asyncio.wait_for(
                self.session.call_tool(tool, arguments=args),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"MCP call timed out after {timeout_s:.1f}s") from exc
        return _decode_mcp_result(raw)

    async def warmup(self) -> float:
        # The cold first live call's initialization cost is a documented property of the
        # server, and this benchmark measures retrieval, not boot.
        started = time.perf_counter()
        await self.call("omniseek_sources", {}, WARMUP_TIMEOUT)
        await self.call(
            "omniseek_search",
            {"query": WARMUP_QUERY, "staleness": "cache_only"},
            WARMUP_TIMEOUT,
        )
        return (time.perf_counter() - started) * 1000.0


def _liveness_probe(task: dict[str, Any], client: httpx.Client) -> dict[str, Any]:
    url = task["liveness_probe"]["url"]
    try:
        response = client.get(url)
        return {
            "alive": 200 <= response.status_code < 400,
            "status_code": response.status_code,
            "url": url,
        }
    except Exception as exc:
        return {"alive": False, "url": url, "error": str(exc)}


async def _run_rep(
    invoker: MCPInvoker,
    task: dict[str, Any],
    call_timeout: float,
) -> dict[str, Any]:
    input_spec = task["input"]
    tool = input_spec["tool"]
    args = dict(input_spec["args"])
    repeat = int(input_spec.get("repeat", 1))
    if repeat < 1:
        raise HarnessError(f"{task['id']} input.repeat must be positive")
    sequence = input_spec.get("calls")
    call_specs = sequence if isinstance(sequence, list) and sequence else [args] * repeat
    if len(call_specs) != repeat:
        raise HarnessError(f"{task['id']} input.calls length must equal input.repeat")

    results: list[Any] = []
    latencies: list[float] = []
    for call_args in call_specs:
        if not isinstance(call_args, dict):
            raise HarnessError(f"{task['id']} input.calls entries must be objects")
        started = time.perf_counter()
        result = await invoker.call(
            tool,
            call_args,
            float(task.get("timeout_s", call_timeout)),
        )
        degraded_text = (
            result
            if isinstance(result, str)
            else json.dumps(result, ensure_ascii=False, default=str)
        )
        if _is_dormant_message(degraded_text):
            raise DormantExtraError(degraded_text)
        latencies.append((time.perf_counter() - started) * 1000.0)
        results.append(result)

    last = results[-1]
    if repeat > 1 or sequence:
        if isinstance(last, dict):
            judge_input = dict(last)
        else:
            judge_input = {"result": last}
        judge_input["calls"] = results
        judge_input["result"] = last
    else:
        judge_input = last

    started = time.perf_counter()
    outcome = judge(task, judge_input)
    judge_latency = (time.perf_counter() - started) * 1000.0
    return {
        "status": outcome.get("status", "pass" if outcome.get("passed") else "fail"),
        "passed": bool(outcome.get("passed")),
        "latency_ms": round(sum(latencies), 3),
        "call_latencies_ms": [round(value, 3) for value in latencies],
        "judge_latency_ms": round(judge_latency, 3),
        "judge_branch": outcome.get("branch"),
        "judge_detail": outcome.get("detail"),
        "judge_reason": outcome.get("reason"),
    }


def _majority_status(reps: list[dict[str, Any]]) -> str:
    if not reps:
        return "failed"
    if all(rep.get("status") == "dormant" for rep in reps):
        return "dormant"
    if sum(bool(rep.get("passed")) for rep in reps) > len(reps) / 2:
        return "passed"
    if any(rep.get("status") == "skip" for rep in reps) and not any(
        rep.get("status") == "fail" for rep in reps
    ):
        return "skipped"
    return "failed"


async def _run_pass(
    tasks: list[dict[str, Any]],
    stale_ids: set[str],
    dormant_suites: set[str],
    pass_index: int,
    reps: int,
    invoker: MCPInvoker,
    call_timeout: float,
    task_records: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    suite_records: dict[str, dict[str, Any]] = {}
    for task in tasks:
        task_id = task["id"]
        suite = task["suite"]
        record = task_records.setdefault(
            task_id,
            {
                "id": task_id,
                "suite": suite,
                "claim": task["claim"],
                "liveness": None,
                "passes": [],
            },
        )
        if task_id in stale_ids:
            continue
        rep_records: list[dict[str, Any]] = []
        if suite in dormant_suites:
            rep_records = [
                {
                    "status": "dormant",
                    "passed": False,
                    "latency_ms": 0.0,
                    "judge_branch": None,
                    "error_class": "missing_extra",
                }
                for _ in range(reps)
            ]
        else:
            for rep_index in range(1, reps + 1):
                try:
                    outcome = await _run_rep(invoker, task, call_timeout)
                    outcome.update({"pass": pass_index, "rep": rep_index})
                    rep_records.append(outcome)
                    if outcome.get("status") == "dormant":
                        dormant_suites.add(suite)
                except DormantExtraError as exc:
                    dormant_suites.add(suite)
                    rep_records.append(
                        {
                            "pass": pass_index,
                            "rep": rep_index,
                            "status": "dormant",
                            "passed": False,
                            "latency_ms": 0.0,
                            "judge_branch": None,
                            "error_class": type(exc).__name__,
                            "error": str(exc),
                        }
                    )
                except Exception as exc:
                    message = str(exc)
                    status = "dormant" if _is_dormant_message(message) else "fail"
                    if status == "dormant":
                        dormant_suites.add(suite)
                    rep_records.append(
                        {
                            "pass": pass_index,
                            "rep": rep_index,
                            "status": status,
                            "passed": False,
                            "latency_ms": 0.0,
                            "judge_branch": "server_error",
                            "error_class": type(exc).__name__,
                            "error": message,
                        }
                    )
        task_status = _majority_status(rep_records)
        record["passes"].append(
            {
                "pass": pass_index,
                "status": task_status,
                "reps": rep_records,
            }
        )
        suite_record = suite_records.setdefault(
            suite,
            {"tasks": [], "successful_latencies_ms": [], "statuses": []},
        )
        suite_record["tasks"].append(task_id)
        suite_record["statuses"].append(task_status)
        for rep in rep_records:
            if rep.get("passed"):
                suite_record["successful_latencies_ms"].extend(
                    float(value) for value in rep.get("call_latencies_ms", [])
                )
    return suite_records


def _aggregate_pass(
    task_records: dict[str, dict[str, Any]],
    tasks: list[dict[str, Any]],
    pass_index: int,
    stale_ids: set[str],
    dormant_suites: set[str],
) -> dict[str, dict[str, Any]]:
    aggregate: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if task["id"] in stale_ids:
            continue
        suite = task["suite"]
        record = task_records[task["id"]]
        pass_record = next(
            item for item in record["passes"] if item["pass"] == pass_index
        )
        bucket = aggregate.setdefault(
            suite,
            {"successes": 0, "total": 0, "dormant": suite in dormant_suites},
        )
        if pass_record["status"] in {"dormant", "skipped"}:
            continue
        bucket["total"] += 1
        if pass_record["status"] == "passed":
            bucket["successes"] += 1
    for suite, bucket in aggregate.items():
        total = bucket["total"]
        successes = bucket["successes"]
        bucket["rate"] = _rate(successes, total)
        bucket["wilson_95"] = list(wilson_interval(successes, total))
    return aggregate


async def run_benchmark(args: argparse.Namespace) -> Path:
    suites = {value.strip() for value in args.suites.split(",") if value.strip()}
    tasks = load_tasks(args.tasks_dir, suites=suites)
    if not tasks:
        raise HarnessError(f"no tasks found for suites: {sorted(suites)}")
    tasks.sort(key=lambda task: (task["suite"], task["id"]))

    extras = detect_extras()
    dormant_suites = {
        task["suite"]
        for task in tasks
        if any(not extras.get(extra, False) for extra in _required_extras(task))
    }
    stale_ids: set[str] = set()
    task_records: dict[str, dict[str, Any]] = {}
    with httpx.Client(follow_redirects=True, timeout=20.0) as client:
        for task in tasks:
            record = task_records.setdefault(
                task["id"],
                {
                    "id": task["id"],
                    "suite": task["suite"],
                    "claim": task["claim"],
                    "liveness": None,
                    "passes": [],
                },
            )
            probe = _liveness_probe(task, client)
            record["liveness"] = probe
            if not probe["alive"]:
                stale_ids.add(task["id"])

    pass_aggregates: list[dict[str, dict[str, Any]]] = []
    latency_by_suite: dict[str, list[float]] = {}
    warmup_pass_ms: list[float] = []
    for pass_index in range(1, args.passes + 1):
        async with MCPInvoker(
            args.transport,
            args.command,
            args.url,
            args.init_timeout,
            args.call_timeout,
        ) as invoker:
            if not getattr(args, "no_warmup", False):
                warmup_pass_ms.append(await invoker.warmup())
            await _run_pass(
                tasks,
                stale_ids,
                dormant_suites,
                pass_index,
                args.reps,
                invoker,
                args.call_timeout,
                task_records,
            )
        pass_aggregates.append(
            _aggregate_pass(
                task_records,
                tasks,
                pass_index,
                stale_ids,
                dormant_suites,
            )
        )
        for task in tasks:
            record = task_records[task["id"]]
            if not record["passes"]:
                continue
            pass_record = record["passes"][-1]
            if pass_record["pass"] != pass_index:
                continue
            for rep in pass_record["reps"]:
                if rep.get("passed"):
                    latency_by_suite.setdefault(task["suite"], []).extend(
                        float(value) for value in rep.get("call_latencies_ms", [])
                    )

    suite_names = sorted({task["suite"] for task in tasks})
    suites_output: dict[str, Any] = {}
    for suite in suite_names:
        pass_values = []
        for aggregate in pass_aggregates:
            value = aggregate.get(
                suite,
                {"successes": 0, "total": 0, "rate": 0.0, "wilson_95": [0.0, 0.0]},
            )
            pass_values.append(
                {
                    "successes": value["successes"],
                    "total": value["total"],
                    "rate": value["rate"],
                    "wilson_95": value["wilson_95"],
                }
            )
        pooled_successes = sum(value["successes"] for value in pass_values)
        pooled_total = sum(value["total"] for value in pass_values)
        rates = [value["rate"] for value in pass_values]
        noise = max(rates) - min(rates) if len(rates) >= 2 else None
        latencies = latency_by_suite.get(suite, [])
        suites_output[suite] = {
            "n": pass_values[0]["total"] if pass_values else 0,
            "passes": pass_values,
            "pooled": {
                "successes": pooled_successes,
                "total": pooled_total,
                "rate": _rate(pooled_successes, pooled_total),
                "wilson_95": list(wilson_interval(pooled_successes, pooled_total)),
            },
            "noise_band": noise,
            "latency_ms": {
                "p50": _percentile(latencies, 0.50),
                "p90": _percentile(latencies, 0.90),
            },
            "stale_count": sum(1 for task in tasks if task["suite"] == suite and task["id"] in stale_ids),
            "dormant": suite in dormant_suites,
            "dormant_note": "required optional extra missing",
        }

    result = {
        "schema_version": "bench-v1.0",
        "run": {
            "git_sha7": _git_sha(),
            "passes": args.passes,
            "reps": args.reps,
            "suites": sorted(suites),
            "tasks_dir": str(Path(args.tasks_dir)),
        },
        "env": {
            "utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "omniseek_version": _package_version(),
            "extras_detected": extras,
            "vantage": args.vantage,
            "warmup_ms": round(sum(warmup_pass_ms), 3) if warmup_pass_ms else None,
            "warmup_pass_ms": [round(value, 3) for value in warmup_pass_ms],
        },
        "tasks": task_records,
        "suites": suites_output,
        "stale": sorted(stale_ids),
        "dormant": sorted(dormant_suites),
    }

    output = _output_path(args.out, result["run"]["git_sha7"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def _package_version() -> str:
    try:
        return importlib.metadata.version("omniseek")
    except importlib.metadata.PackageNotFoundError:
        return "uninstalled"


def _output_path(value: str | Path | None, sha7: str) -> Path:
    if value:
        path = Path(value)
        return path if path.suffix.casefold() == ".json" else path / f"run-{sha7}.json"
    date = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    return Path("bench") / "results" / f"run-{date}-{sha7}.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--url")
    parser.add_argument("--command", default="omniseek")
    parser.add_argument("--init-timeout", type=float, default=240.0)
    parser.add_argument("--call-timeout", type=float, default=120.0)
    parser.add_argument("--suites", default=DEFAULT_SUITES)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--passes", type=int, default=2)
    parser.add_argument("--tasks-dir", type=Path, default=Path("bench") / "tasks")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--vantage", default="local")
    parser.add_argument(
        "--no-warmup",
        action="store_true",
        help="skip the untimed server initialization warmup",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.reps < 1 or args.passes < 1:
        print("--reps and --passes must be positive", file=sys.stderr)
        return 1
    try:
        output = asyncio.run(run_benchmark(args))
    except (HarnessError, TaskValidationError, OSError, ValueError) as exc:
        print(f"benchmark harness error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"benchmark harness error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
