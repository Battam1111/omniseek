"""Pure claim-verification judges plus explicitly isolated scholarly refetches."""

from __future__ import annotations

import difflib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any


# This is the complete punctuation set removed by the text judge. It covers ASCII
# punctuation, CJK punctuation, and common fullwidth quotation and bracket forms.
PUNCTUATION_CHARS = frozenset(
    r"""!"#$%&'()*+,-./:;<=>?@[\]^_`{|}~"""
    "，。、：；！？（）「」『』【】《》〈〉“”‘’＂＇﹁﹂﹃﹄"
)

_MISSING = object()
_PATH_TOKEN = re.compile(r"[^.[\]]+|\[(\d+|\*)\]")


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(
        char for char in text if not char.isspace() and char not in PUNCTUATION_CHARS
    )


def normalized_containment(needle: Any, haystack: Any) -> bool:
    """Check containment after NFKC, casefold, whitespace, and punctuation normalization."""
    return _normalize_text(needle) in _normalize_text(haystack)


def _best_similarity(needle: str, haystack: str) -> float:
    if not needle:
        return 1.0
    if not haystack:
        return 0.0
    window_size = len(needle)
    if len(haystack) <= window_size:
        return difflib.SequenceMatcher(None, needle, haystack).ratio()
    best = 0.0
    for start in range(len(haystack) - window_size + 1):
        window = haystack[start : start + window_size]
        best = max(best, difflib.SequenceMatcher(None, needle, window).ratio())
    return best


def _response_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        if "text" in value and isinstance(value["text"], str):
            return value["text"]
        return " ".join(_response_text(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return " ".join(_response_text(item) for item in value)
    return str(value)


def _first_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        for key in ("results", "items", "documents", "papers", "data"):
            nested = value.get(key)
            if isinstance(nested, Sequence) and not isinstance(
                nested, (str, bytes, bytearray)
            ):
                for item in nested:
                    if isinstance(item, Mapping):
                        return item
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            if isinstance(item, Mapping):
                return item
    return {}


def _path_tokens(path: str) -> list[str | int]:
    path = path.strip()
    if path.startswith("$."):
        path = path[2:]
    elif path == "$":
        return []
    elif path.startswith("$"):
        path = path[1:]
    tokens: list[str | int] = []
    for match in _PATH_TOKEN.finditer(path):
        token = match.group(1) or match.group(0)
        tokens.append(int(token) if token.isdigit() else token)
    return tokens


def _resolve_path(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    for token in _path_tokens(path):
        if isinstance(token, int):
            if not isinstance(current, Sequence) or isinstance(current, (str, bytes, bytearray)):
                return False, _MISSING
            if token >= len(current):
                return False, _MISSING
            current = current[token]
        elif isinstance(current, Mapping) and token in current:
            current = current[token]
        else:
            return False, _MISSING
    return True, current


def _assertion_result(context: Any, assertion: Mapping[str, Any]) -> tuple[bool, str]:
    if "any_of" in assertion:
        children = assertion["any_of"]
        if not isinstance(children, Sequence):
            return False, "any_of must be a list"
        outcomes = [_assertion_result(context, child) for child in children]
        passed = any(item[0] for item in outcomes)
        return passed, "any_of passed" if passed else "; ".join(item[1] for item in outcomes)

    path = assertion.get("path")
    if not isinstance(path, str):
        return False, "assertion.path must be a string"
    exists, value = _resolve_path(context, path)
    if "exists" in assertion:
        expected = bool(assertion["exists"])
        if exists != expected:
            return False, f"{path}: exists={exists}, expected={expected}"
    if assertion.get("exists") is False:
        return True, f"{path}: absent"
    if not exists:
        return False, f"{path}: missing"
    if "equals" in assertion and value != assertion["equals"]:
        return False, f"{path}: {value!r} != {assertion['equals']!r}"
    if "gt" in assertion:
        try:
            if not value > assertion["gt"]:
                return False, f"{path}: {value!r} is not greater than {assertion['gt']!r}"
        except TypeError:
            return False, f"{path}: value is not comparable"
    if "gte" in assertion:
        try:
            if not value >= assertion["gte"]:
                return False, f"{path}: {value!r} is below {assertion['gte']!r}"
        except TypeError:
            return False, f"{path}: value is not comparable"
    if "reduction" in assertion:
        if not isinstance(value, Mapping):
            return False, f"{path}: reduction needs an object"
        before = value.get("in")
        after = value.get("out")
        if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
            return False, f"{path}: reduction needs numeric in and out"
        if not before > after:
            return False, f"{path}: {before} is not greater than {after}"
    if "each" in assertion:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            return False, f"{path}: each needs a list"
        rules = assertion["each"]
        if not isinstance(rules, Mapping):
            return False, f"{path}: each must be an object"
        required = rules.get("fields", [])
        if not all(
            isinstance(item, Mapping) and all(field in item for field in required)
            for item in value
        ):
            return False, f"{path}: each item is missing a required field"
        any_fields = rules.get("exists_any", [])
        if any_fields and not all(
            isinstance(item, Mapping) and any(field in item for field in any_fields)
            for item in value
        ):
            return False, f"{path}: each item has no declared facet"
    if assertion.get("each_provenance"):
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            return False, f"{path}: provenance check needs a list"
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                return False, f"{path}[{index}]: document is not an object"
            metadata = item.get("metadata")
            has_source = bool(item.get("source") or item.get("source_name"))
            has_locator = bool(
                item.get("url")
                or item.get("id")
                or (isinstance(metadata, Mapping) and (metadata.get("url") or metadata.get("id")))
            )
            if not has_source or not has_locator:
                return False, f"{path}[{index}]: missing source or locator"
    return True, f"{path}: assertion passed"


def _meta_assertion(task: Mapping[str, Any], result: Any) -> dict[str, Any]:
    truth = task["ground_truth"]
    assertions = truth.get("assertions", [])
    if not isinstance(assertions, Sequence):
        return {
            "passed": False,
            "status": "fail",
            "branch": "meta_assertion",
            "detail": "ground_truth.assertions must be a list",
        }
    context = result if isinstance(result, Mapping) else {"result": result}
    failures: list[str] = []
    for assertion in assertions:
        if not isinstance(assertion, Mapping):
            failures.append("assertion must be an object")
            continue
        passed, detail = _assertion_result(context, assertion)
        if not passed:
            failures.append(detail)
    return {
        "passed": not failures,
        "status": "pass" if not failures else "fail",
        "branch": "meta_assertion",
        "detail": "all assertions passed" if not failures else "; ".join(failures),
    }


def _normalize_identifier(identifier: Any) -> tuple[str, str]:
    value = str(identifier).strip().casefold().rstrip(".,;")
    value = value.split("?", 1)[0].split("#", 1)[0]
    value = re.sub(r"^https?://doi\.org/", "", value)
    value = re.sub(r"^doi:\s*", "", value)
    if re.match(r"^(?:https?://)?arxiv\.org/(?:abs|pdf)/", value):
        value = re.sub(r"^(?:https?://)?arxiv\.org/(?:abs|pdf)/", "", value)
    if value.endswith(".pdf"):
        value = value[:-4]
    value = value.rstrip("/")
    if re.match(r"^(?:arxiv:)?(?:\d{4}\.\d{4,5}|[a-z-]+/\d{7})(?:v\d+)?$", value):
        value = re.sub(r"^arxiv:", "", value)
        value = re.sub(r"v\d+$", "", value)
        return "arxiv", value
    if value.startswith("10.") and "/" in value:
        return "doi", value
    if re.match(r"^[a-z][a-z0-9+.-]*://", value):
        value = re.sub(r"^[a-z][a-z0-9+.-]*://", "", value)
        return "url", value.rstrip("/")
    return "text", value


def _iter_identifier_values(value: Any, key: str = ""):
    if isinstance(value, Mapping):
        for child_key, child_value in value.items():
            lowered = str(child_key).casefold()
            if lowered in {"id", "doi", "url", "source_id", "paper_id", "arxiv_id"}:
                yield child_value
            elif lowered == "metadata" or key == "metadata":
                yield from _iter_identifier_values(child_value, lowered)
            elif isinstance(child_value, (Mapping, Sequence)) and not isinstance(
                child_value, (str, bytes, bytearray)
            ):
                yield from _iter_identifier_values(child_value, lowered)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            yield from _iter_identifier_values(item, key)


def identifier_in_topk(identifier: Any, results: Sequence[Any], k: int) -> bool:
    """Find a normalized DOI, arXiv id, URL, or text identifier in the first k entries."""
    wanted_kind, wanted = _normalize_identifier(identifier)
    for entry in list(results)[: max(0, int(k))]:
        for candidate in _iter_identifier_values(entry):
            candidate_kind, normalized = _normalize_identifier(candidate)
            if candidate_kind == wanted_kind and normalized == wanted:
                return True
    return False


def _json_response(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


class _UpstreamUnavailable(RuntimeError):
    pass


def _fetch_json(url: str, http_client=None, retry_429: bool = False) -> Any:
    if http_client is None:
        import httpx

        with httpx.Client(follow_redirects=True, timeout=20.0) as client:
            return _fetch_json(url, client, retry_429=retry_429)
    attempts = 2 if retry_429 else 1
    response = None
    for attempt in range(attempts):
        try:
            response = http_client.get(url)
        except Exception as exc:
            raise _UpstreamUnavailable(str(exc)) from exc
        if response.status_code != 429 or attempt + 1 == attempts:
            break
    if response is None:
        raise _UpstreamUnavailable("upstream returned no response")
    if response.status_code == 429 or response.status_code >= 500:
        raise _UpstreamUnavailable(f"upstream status {response.status_code}")
    if response.status_code >= 400:
        raise RuntimeError(f"upstream status {response.status_code}")
    try:
        return response.json()
    except Exception as exc:
        raise RuntimeError(f"upstream response was not JSON: {exc}") from exc


def _result_items(result: Any) -> list[Mapping[str, Any]]:
    if isinstance(result, Mapping):
        for key in ("items", "results", "documents", "papers", "data"):
            value = result.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                return [item for item in value if isinstance(item, Mapping)]
        return [result]
    if isinstance(result, Sequence) and not isinstance(result, (str, bytes, bytearray)):
        return [item for item in result if isinstance(item, Mapping)]
    return []


def _find_key(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        if key in value:
            return value[key]
        for child in value.values():
            found = _find_key(child, key)
            if found is not _MISSING:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            found = _find_key(child, key)
            if found is not _MISSING:
                return found
    return _MISSING


def _expected_top_name(truth: Mapping[str, Any]) -> str | None:
    explicit = truth.get("expected_top_name")
    if isinstance(explicit, str):
        return explicit
    expected_now = truth.get("expected_now", "")
    if isinstance(expected_now, str):
        match = re.search(r"#1\s*=\s*([^,(;]+)", expected_now)
        if match:
            return match.group(1).strip()
    return None


def _dynamic_top_coauthor(task: Mapping[str, Any], result: Any, http_client) -> dict[str, Any]:
    payload = _fetch_json(task["liveness_probe"]["url"], http_client)
    if not isinstance(payload, Mapping):
        raise RuntimeError("OpenAlex probe did not return an object")
    expected = _expected_top_name(task["ground_truth"])
    candidates = _find_key(result, "top_coauthors")
    if not isinstance(candidates, Sequence) or not candidates:
        return {"passed": False, "status": "fail", "detail": "top_coauthors missing"}
    first = candidates[0]
    actual = first.get("name") if isinstance(first, Mapping) else None
    passed = bool(expected and actual and normalized_containment(expected, actual))
    return {
        "passed": passed,
        "status": "pass" if passed else "fail",
        "detail": f"expected={expected!r}, actual={actual!r}, upstream_refetched=True",
    }


def _notice_set(value: Any) -> set[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return set()
    normalized = set()
    for item in value:
        text = re.sub(r"[\s-]+", "_", str(item).casefold())
        normalized.add(text)
    return normalized


def _dynamic_retraction(task: Mapping[str, Any], result: Any, http_client) -> dict[str, Any]:
    payload = _fetch_json(task["liveness_probe"]["url"], http_client)
    message = payload.get("message", payload) if isinstance(payload, Mapping) else {}
    updates = message.get("updated-by", []) if isinstance(message, Mapping) else []
    expected_notices = {
        re.sub(r"[\s-]+", "_", str(item.get("type", "")).casefold())
        for item in updates
        if isinstance(item, Mapping) and item.get("type")
    }
    paper = _first_mapping(result)
    integrity = paper.get("integrity", {}) if isinstance(paper, Mapping) else {}
    actual_notices = _notice_set(integrity.get("notices"))
    expected_retracted = "retraction" in expected_notices
    passed = (
        integrity.get("retracted") is expected_retracted
        and actual_notices == expected_notices
    )
    return {
        "passed": passed,
        "status": "pass" if passed else "fail",
        "detail": (
            f"expected_retracted={expected_retracted}, actual_retracted={integrity.get('retracted')}, "
            f"expected_notices={sorted(expected_notices)}, actual_notices={sorted(actual_notices)}"
        ),
    }


def _dynamic_oa_fulltext(task: Mapping[str, Any], result: Any, http_client) -> dict[str, Any]:
    probe_url = task["liveness_probe"]["url"]
    try:
        payload = _fetch_json(probe_url, http_client)
    except RuntimeError:
        if "arxiv.org/" not in probe_url.casefold():
            raise
        payload = {
            "is_oa": True,
            "best_oa_location": {"url_for_pdf": probe_url.replace("/abs/", "/pdf/") + ".pdf"},
        }
    paper = _first_mapping(result)
    if not paper:
        return {"passed": False, "status": "fail", "detail": "paper result missing"}
    expected_oa = bool(payload.get("is_oa")) if isinstance(payload, Mapping) else False
    location = payload.get("best_oa_location") if isinstance(payload, Mapping) else None
    expected_pdf = location.get("url_for_pdf") if isinstance(location, Mapping) else None
    actual_pdf = paper.get("pdf_url")
    passed = bool(paper.get("is_oa")) == expected_oa
    if expected_pdf:
        passed = passed and bool(actual_pdf)
    return {
        "passed": passed,
        "status": "pass" if passed else "fail",
        "detail": f"expected_oa={expected_oa}, expected_pdf={bool(expected_pdf)}, actual_pdf={bool(actual_pdf)}",
    }


def _numeric_provenance(value: Any, inherited: str = "") -> list[tuple[float, str]]:
    found: list[tuple[float, str]] = []
    if isinstance(value, Mapping):
        kind = str(value.get("kind", "")).casefold()
        provenance = (
            value.get("computed_by")
            or value.get("provenance")
            or value.get("source")
            or value.get("provider")
            or (f"kind:{kind}" if kind in {"arxiv", "doi"} else "")
            or inherited
        )
        for key, child in value.items():
            lowered = str(key).casefold()
            if lowered in {
                "citation_count",
                "citationcount",
                "cited_by_count",
                "citedbycount",
                "count",
            } and isinstance(child, (int, float)) and not isinstance(child, bool):
                found.append((float(child), str(provenance)))
            elif (
                lowered == "value"
                and isinstance(child, (int, float))
                and not isinstance(child, bool)
                and any(token in str(provenance).casefold() for token in ("citat", "cited"))
            ):
                found.append((float(child), str(provenance)))
            elif isinstance(child, (Mapping, Sequence)) and not isinstance(
                child, (str, bytes, bytearray)
            ):
                found.extend(_numeric_provenance(child, str(provenance)))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            found.extend(_numeric_provenance(child, inherited))
    return found


def _dynamic_citation_conflict(
    task: Mapping[str, Any],
    result: Any,
    http_client,
    retry_429: bool = False,
) -> dict[str, Any]:
    _fetch_json(task["liveness_probe"]["url"], http_client, retry_429=retry_429)
    values = _numeric_provenance(result)
    distinct: list[tuple[float, str]] = []
    for value, provenance in values:
        if provenance and not any(provenance == prior[1] for prior in distinct):
            distinct.append((value, provenance))
    expected_now = str(task["ground_truth"].get("expected_now", ""))
    floor = task["ground_truth"].get("ratio_floor") or task.get("ratio_floor")
    if floor is None:
        match = re.search(r"(\d+(?:\.\d+)?)x", expected_now)
        floor = float(match.group(1)) if match else 1.0
    numbers = [item[0] for item in distinct if item[0] > 0]
    ratio = max(numbers) / min(numbers) if len(numbers) >= 2 else 0.0
    passed = len(distinct) >= 2 and ratio >= float(floor)
    return {
        "passed": passed,
        "status": "pass" if passed else "fail",
        "detail": f"provenance_values={distinct}, ratio={ratio:.4f}, floor={float(floor):.4f}",
    }


def dynamic_structural(
    task: Mapping[str, Any],
    result: Any,
    http_client=None,
) -> dict[str, Any]:
    """Refetch a source-of-record leg and compare the task's structural claim."""
    api = (
        task.get("ground_truth", {})
        .get("upstream_of_record", {})
        .get("api", "")
        .casefold()
    )
    try:
        if api == "openalex":
            outcome = _dynamic_top_coauthor(task, result, http_client)
        elif api == "crossref":
            kind = str(task.get("kind") or task.get("ground_truth", {}).get("kind", "")).casefold()
            if not kind:
                expected_now = str(task.get("ground_truth", {}).get("expected_now", "")).casefold()
                kind = "citation_conflict" if "x" in expected_now else "retraction"
            if kind == "citation_conflict":
                outcome = _dynamic_citation_conflict(task, result, http_client)
            else:
                outcome = _dynamic_retraction(task, result, http_client)
        elif api == "unpaywall":
            outcome = _dynamic_oa_fulltext(task, result, http_client)
        elif api in {"semantic_scholar", "semanticscholar", "s2"}:
            outcome = _dynamic_citation_conflict(
                task,
                result,
                http_client,
                retry_429=True,
            )
        else:
            return {
                "passed": False,
                "status": "fail",
                "branch": "dynamic_structural",
                "detail": f"unsupported upstream api: {api or 'missing'}",
            }
    except _UpstreamUnavailable as exc:
        return {
            "passed": False,
            "status": "skip",
            "branch": "dynamic_structural",
            "detail": str(exc),
            "reason": "upstream_unavailable",
        }
    except Exception as exc:
        return {
            "passed": False,
            "status": "fail",
            "branch": "dynamic_structural",
            "detail": str(exc),
        }
    outcome["branch"] = "dynamic_structural"
    return outcome


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Return a Wilson 95 percent interval, or (0, 0) for an empty denominator."""
    if total <= 0:
        return 0.0, 0.0
    successes = max(0, min(int(successes), int(total)))
    n = float(total)
    p = successes / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denominator
    margin = z / denominator * math.sqrt((p * (1.0 - p) / n) + z2 / (4.0 * n * n))
    return max(0.0, center - margin), min(1.0, center + margin)


def _judge_normalized(task: Mapping[str, Any], result: Any) -> dict[str, Any]:
    truth = task["ground_truth"]
    needle = truth.get("value", truth.get("needle"))
    if needle is None:
        return {"passed": False, "status": "fail", "detail": "ground truth value missing"}
    haystack = _response_text(result)
    if normalized_containment(needle, haystack):
        return {
            "passed": True,
            "status": "pass",
            "branch": "normalized_containment",
            "detail": "normalized needle is contained",
        }
    threshold = task.get("fallback_similarity", truth.get("fallback_similarity"))
    if threshold is None:
        return {
            "passed": False,
            "status": "fail",
            "branch": "normalized_containment",
            "detail": "normalized needle is absent and fallback is undeclared",
        }
    score = _best_similarity(_normalize_text(needle), _normalize_text(haystack))
    return {
        "passed": score >= float(threshold),
        "status": "pass" if score >= float(threshold) else "fail",
        "branch": "fallback_similarity",
        "detail": f"score={score:.6f}, threshold={float(threshold):.6f}",
    }


def _judge_identifier(task: Mapping[str, Any], result: Any) -> dict[str, Any]:
    truth = task["ground_truth"]
    identifier = truth.get("identifier", truth.get("value"))
    entries = result
    if isinstance(result, Mapping):
        entries = result.get("documents", result.get("results", result.get("items", [])))
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes, bytearray)):
        entries = []
    k = int(truth.get("k", task.get("k", 5)))
    passed = identifier_in_topk(identifier, entries, k)
    return {
        "passed": passed,
        "status": "pass" if passed else "fail",
        "branch": "identifier_in_topk",
        "detail": f"identifier={identifier!r}, k={k}",
    }


def judge(task: Mapping[str, Any], result: Any) -> dict[str, Any]:
    """Dispatch a task's pure judge and return a serializable outcome."""
    result = _json_response(result)
    truth_type = str(task.get("ground_truth", {}).get("type", "")).casefold()
    if truth_type in {"normalized_containment", "containment"}:
        return _judge_normalized(task, result)
    if truth_type in {"identifier_in_topk", "identifier"}:
        return _judge_identifier(task, result)
    if truth_type == "meta_assertion":
        return _meta_assertion(task, result)
    if truth_type == "dynamic_structural":
        return dynamic_structural(task, result)
    return {
        "passed": False,
        "status": "fail",
        "branch": truth_type or "unknown",
        "detail": f"unsupported ground_truth.type: {truth_type or 'missing'}",
    }
