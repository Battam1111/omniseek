"""Source spec: typed filters, coverage, license and samples (a source's machine-readable self-description).

A source used to describe itself with one prose line (``param_hint``). That stays as a prose aid, but
what a source can FILTER on, which period it covers, under what terms its results come and which
queries are known to return results now live as four class attributes an agent and a program can both
read (the design follows the ``fa spec`` / ``-F`` filter syntax of DeepXiv's FA.md, sections 4 and 5):

    filters   {field: {type, ops, values?, desc, doc_key?, param?}}
                type    keyword | int | number | bool | date | text
                ops     the allowed subset of eq | any | gte | lte | range | text
                values  OPTIONAL closed list of the accepted values (keyword fields only). When given,
                        a value outside it is refused; leave it out for an open field.
                desc    one line
                doc_key OPTIONAL dotted path of the field in a stored document (``metadata.venue``,
                        ``tags``, ``date``): omniseek_sources(field=...) counts its values in the local
                        index.
                param   declarative rows only: the upstream parameter the field is sent as (a name, or
                        {"gte": name, "lte": name} for a range).
    coverage  {since: ISO date | null, until: ISO date | null, gaps: [text], note: text | null}
              since / until are on the same time axis as the source's date filters; null = not known.
    license   the terms the results come under, as text, or ``upstreams:<key>`` pointing at that
              upstream's declared terms in upstreams.json. Default: the upstream that lists the
              source, else None (reported as "unknown" on a result).
    samples   [{"query": text, "filters": {...}?}]: queries that return results (shown in omniseek_sources
              as examples of a working call).
    as_of_field  OPTIONAL name of the declared date (or int year) filter field that says when an item
              appeared upstream. omniseek_search(as_of=...) pushes ``<field> <= as_of`` down to the source
              through it; a source without one is still cut at the exit by each result's date.

A coded source declares them as class attributes; a declarative source as keys on its sources.json row.

Translation is the source's own job: an adapter that declares ``filters`` implements

    translate_filters(query: str, clauses: list[Clause]) -> str | tuple[str, dict]

returning the query rewritten in the source's own query grammar (the same tokens a caller could type,
so the source's cache key follows automatically) and, for a source whose upstream takes separate
parameters, a dict of upstream parameters the source reads back with ``upstream()`` during the call.
A field it cannot translate raises ``Unsupported``: never silently ignored.

Filter syntax (FA.md section 5): a dict ``{"year": {"gte": 2020}, "cat": ["cs.CL", "cs.LG"]}`` or a
list of expressions ``k=v`` (equals), ``k=a,b`` (any of), ``k>=n`` / ``k<=n`` (bounds; the two combine
on one field), ``k=n..m`` (inclusive range, either side may be open), ``k~text`` (text match). The same
field may not be given twice, except ``>=`` with ``<=``.
"""

from __future__ import annotations

import calendar
import contextvars
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from typing import Any, Optional

from omniseek.core.normalize import to_utc

TYPES = ("keyword", "int", "number", "bool", "date", "text")
OPS = ("eq", "any", "gte", "lte", "range", "text")
# Which operators make sense for which type (a declared spec outside this is a spec error).
_TYPE_OPS = {
    "keyword": {"eq", "any", "text"},
    "text": {"eq", "text"},
    "int": {"eq", "any", "gte", "lte", "range"},
    "number": {"eq", "gte", "lte", "range"},
    "bool": {"eq"},
    "date": {"eq", "gte", "lte", "range"},
}
_EMPTY_COVERAGE = {"since": None, "until": None, "gaps": [], "note": None}


class FilterError(ValueError):
    """A filter OmniSeek refuses before sending anything. ``problems`` lists each refusal
    ({source?, field, reason}); ``available`` maps source -> its declared fields and operators."""

    def __init__(self, message: str, problems: Optional[list] = None,
                 available: Optional[dict] = None) -> None:
        super().__init__(message)
        self.problems = list(problems or [])
        self.available = dict(available or {})

    def payload(self) -> dict:
        out: dict = {"error": str(self), "filter_problems": self.problems}
        if self.available:
            out["available_filters"] = self.available
        return out


class Unsupported(FilterError):
    """Raised by a source's ``translate_filters`` for a declared field (or operator) it cannot send."""

    def __init__(self, field_name: str, why: str = "") -> None:
        super().__init__(f"该源不支持这个过滤: {field_name}" + (f" ({why})" if why else ""),
                         [{"field": field_name, "reason": "该源不支持这个过滤" + (f": {why}" if why else "")}])
        self.field = field_name


# ── parsed and typed clauses ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RawClause:
    """One parsed filter before typing. op: eq | any | range | text. value: eq -> scalar, any -> tuple,
    range -> (gte or None, lte or None), text -> str."""
    field: str
    op: str
    value: Any


@dataclass(frozen=True)
class Clause:
    """One filter typed against a source's declared field. ``value``: eq -> typed scalar (a date is a
    ``(first_day, last_day)`` pair of the period written, e.g. "2024-05" -> May 1 to May 31); any ->
    tuple of typed scalars; range -> ``(lo, hi)`` with either None (a date bound is the first day of
    its period for ``lo`` and the last day for ``hi``); text -> str. ``raw`` keeps the caller's text."""
    field: str
    op: str
    type: str
    value: Any
    raw: Any = None

    @property
    def lo(self):
        if self.op == "range":
            return self.value[0]
        if self.op == "eq" and self.type == "date":
            return self.value[0]
        return None

    @property
    def hi(self):
        if self.op == "range":
            return self.value[1]
        if self.op == "eq" and self.type == "date":
            return self.value[1]
        return None


_EXPR_RE = re.compile(r"^\s*([A-Za-z_][\w.\-]*)\s*(>=|<=|~|=)\s*(.*?)\s*$", re.DOTALL)


def _parse_expr(expr: str) -> tuple[str, str, Any]:
    m = _EXPR_RE.match(expr or "")
    if not m:
        raise FilterError(f"看不懂的过滤表达式: {expr!r} (写法: k=v, k=a,b, k>=n, k<=n, k=n..m, k~text)",
                          [{"expr": expr, "reason": "syntax"}])
    k, op, v = m.group(1), m.group(2), m.group(3)
    if v == "":
        raise FilterError(f"过滤 {k} 没有值: {expr!r}", [{"field": k, "reason": "empty value"}])
    if op == "~":
        return k, "text", v
    if op == ">=":
        return k, "gte", v
    if op == "<=":
        return k, "lte", v
    rm = re.fullmatch(r"(.*?)\.\.(.*)", v)
    if rm and (rm.group(1) or rm.group(2)):
        return k, "range", (rm.group(1).strip() or None, rm.group(2).strip() or None)
    if "," in v:
        parts = tuple(p.strip() for p in v.split(",") if p.strip())
        return k, "any", parts
    return k, "eq", v


def _from_dict_value(k: str, v: Any) -> list[tuple[str, str, Any]]:
    if isinstance(v, (list, tuple)):
        return [(k, "any", tuple(v))]
    if isinstance(v, dict):
        keys = set(v)
        if not keys or not keys <= {"eq", "any", "gte", "lte", "text"}:
            raise FilterError(f"过滤 {k} 的写法不对: {v!r} (可用 eq / any / gte / lte / text)",
                              [{"field": k, "reason": "syntax"}])
        out = []
        if "eq" in v:
            out.append((k, "eq", v["eq"]))
        if "any" in v:
            out.append((k, "any", tuple(v["any"] if isinstance(v["any"], (list, tuple)) else [v["any"]])))
        if "text" in v:
            out.append((k, "text", v["text"]))
        if "gte" in v:
            out.append((k, "gte", v["gte"]))
        if "lte" in v:
            out.append((k, "lte", v["lte"]))
        return out
    return [(k, "eq", v)]


def parse(filters: Any) -> list[RawClause]:
    """Parse a filters argument (dict, list of expressions, or one expression) into raw clauses.
    ``>=`` and ``<=`` on one field merge into one range; any other repeat of a field is refused.
    ``None`` / empty -> []."""
    if filters is None or filters == "" or filters == [] or filters == {}:
        return []
    items: list[tuple[str, str, Any]] = []
    if isinstance(filters, dict):
        for k, v in filters.items():
            items.extend(_from_dict_value(str(k), v))
    elif isinstance(filters, (list, tuple)):
        for e in filters:
            if isinstance(e, dict):
                for kk, vv in e.items():
                    items.extend(_from_dict_value(str(kk), vv))
            else:
                items.append(_parse_expr(str(e)))
    elif isinstance(filters, str):
        items.append(_parse_expr(filters))
    else:
        raise FilterError(f"filters 要是 dict 或表达式列表, 收到 {type(filters).__name__}",
                          [{"reason": "syntax"}])
    merged: dict[str, RawClause] = {}
    order: list[str] = []
    from_bounds: set = set()  # fields whose range was built from a lone >= or <= (may take the other)
    for k, op, v in items:
        if op in ("gte", "lte"):
            prev = merged.get(k)
            if prev is None:
                merged[k] = RawClause(k, "range", (v, None) if op == "gte" else (None, v))
                order.append(k)
                from_bounds.add(k)
                continue
            if k in from_bounds:
                lo, hi = prev.value
                if (op == "gte" and lo is None) or (op == "lte" and hi is None):
                    merged[k] = RawClause(k, "range", (v, hi) if op == "gte" else (lo, v))
                    from_bounds.discard(k)
                    continue
            raise FilterError(f"同一字段只能写一次 (>= 与 <= 可合并): {k}",
                              [{"field": k, "reason": "repeated field"}])
        if k in merged:
            raise FilterError(f"同一字段只能写一次 (>= 与 <= 可合并): {k}",
                              [{"field": k, "reason": "repeated field"}])
        if op == "any" and not v:
            raise FilterError(f"过滤 {k} 的取值列表是空的", [{"field": k, "reason": "empty value"}])
        merged[k] = RawClause(k, op, v)
        order.append(k)
    return [merged[k] for k in order]


# ── typing values ─────────────────────────────────────────────────────────────────────────────────

_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?$")
_TRUE = {"true", "yes", "1"}
_FALSE = {"false", "no", "0"}


def _date_span(v: Any) -> tuple[date, date]:
    if isinstance(v, date):
        return v, v
    m = _DATE_RE.match(str(v).strip())
    if not m:
        raise ValueError("日期要写成 YYYY、YYYY-MM 或 YYYY-MM-DD")
    y = int(m.group(1))
    if m.group(2) is None:
        return date(y, 1, 1), date(y, 12, 31)
    mo = int(m.group(2))
    if m.group(3) is None:
        return date(y, mo, 1), date(y, mo, calendar.monthrange(y, mo)[1])
    d = date(y, mo, int(m.group(3)))
    return d, d


def _typed(v: Any, typ: str):
    if typ in ("keyword", "text"):
        s = str(v).strip()
        if not s:
            raise ValueError("值是空的")
        return s
    if typ == "int":
        if isinstance(v, bool):
            raise ValueError("要整数")
        if isinstance(v, int):
            return v
        s = str(v).strip()
        if not re.fullmatch(r"[+-]?\d+", s):
            raise ValueError("要整数")
        return int(s)
    if typ == "number":
        if isinstance(v, bool):
            raise ValueError("要数")
        try:
            return float(v)
        except (TypeError, ValueError):
            raise ValueError("要数") from None
    if typ == "bool":
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        raise ValueError("要 true/false/yes/no/1/0")
    if typ == "date":
        return _date_span(v)
    raise ValueError(f"未知类型 {typ}")


def _op_allowed(raw: RawClause, ops: set) -> bool:
    if raw.op != "range":
        return raw.op in ops
    lo, hi = raw.value
    if "range" in ops:
        return True
    return (lo is None or "gte" in ops) and (hi is None or "lte" in ops)


def _op_label(raw: RawClause) -> str:
    if raw.op != "range":
        return raw.op
    lo, hi = raw.value
    if lo is not None and hi is not None:
        return "range"
    return "gte" if lo is not None else "lte"


def type_clause(raw: RawClause, spec: dict) -> Clause:
    """Type one raw clause against a declared field spec. Raises FilterError (operator not allowed,
    value of the wrong type, value outside a closed ``values`` list)."""
    typ = spec.get("type")
    ops = set(spec.get("ops") or ())
    if not _op_allowed(raw, ops):
        raise FilterError(f"字段 {raw.field} 不允许运算符 {_op_label(raw)} (允许: {sorted(ops)})",
                          [{"field": raw.field, "reason": f"operator {_op_label(raw)} not allowed",
                            "ops": sorted(ops)}])
    try:
        if raw.op == "eq":
            value = _typed(raw.value, typ)
        elif raw.op == "any":
            value = tuple(_typed(x, typ) for x in raw.value)
        elif raw.op == "text":
            value = str(raw.value).strip()
            if not value:
                raise ValueError("值是空的")
        else:
            lo, hi = raw.value
            if typ == "date":
                lo = _date_span(lo)[0] if lo is not None else None
                hi = _date_span(hi)[1] if hi is not None else None
            else:
                lo = _typed(lo, typ) if lo is not None else None
                hi = _typed(hi, typ) if hi is not None else None
            if lo is not None and hi is not None and lo > hi:
                raise ValueError("下界大于上界")
            value = (lo, hi)
    except ValueError as exc:
        raise FilterError(f"字段 {raw.field} 的值类型不对 ({typ}): {exc}",
                          [{"field": raw.field, "reason": f"bad {typ} value: {exc}"}]) from None
    allowed = spec.get("values")
    if allowed and typ == "keyword" and raw.op in ("eq", "any"):
        canon = {str(a).lower(): a for a in allowed}
        vals = (value,) if raw.op == "eq" else value
        bad = [x for x in vals if str(x).lower() not in canon]
        if bad:
            raise FilterError(f"字段 {raw.field} 的值不在已知取值里: {bad} (可用: {list(allowed)})",
                              [{"field": raw.field, "reason": f"value not in values: {bad}"}])
        fixed = tuple(canon[str(x).lower()] for x in vals)
        value = fixed[0] if raw.op == "eq" else fixed
    return Clause(raw.field, raw.op, typ, value, raw.value)


# ── reading a source's spec ───────────────────────────────────────────────────────────────────────

def declared_filters(adapter: Any) -> dict:
    f = getattr(adapter, "filters", None)
    return dict(f) if isinstance(f, dict) else {}


def coverage_of(adapter: Any) -> dict:
    c = getattr(adapter, "coverage", None)
    out = dict(_EMPTY_COVERAGE)
    out["gaps"] = []
    if isinstance(c, dict):
        out.update({k: c.get(k) for k in ("since", "until", "note") if k in c})
        out["gaps"] = list(c.get("gaps") or [])
    return out


def samples_of(adapter: Any) -> list:
    s = getattr(adapter, "samples", None)
    return list(s) if isinstance(s, (list, tuple)) else []


def as_of_field_of(adapter: Any) -> Optional[str]:
    """The declared filter field that says when an item appeared upstream, or None."""
    f = getattr(adapter, "as_of_field", None)
    return f if isinstance(f, str) and f else None


def as_of_field_problems(name: str, adapter: Any) -> list[str]:
    """Mechanical checks on a declared ``as_of_field``: it must name a declared date or int field that
    takes an upper bound (``lte`` or ``range``). Used by the tests over every source."""
    raw = getattr(adapter, "as_of_field", None)
    if raw is None:
        return []
    if not isinstance(raw, str) or not raw:
        return [f"{name}: as_of_field must be a field name"]
    spec = declared_filters(adapter).get(raw)
    if spec is None:
        return [f"{name}: as_of_field {raw!r} is not a declared filter field"]
    p = []
    if spec.get("type") not in ("date", "int"):
        p.append(f"{name}: as_of_field {raw!r} must be a date or int field")
    if not ({"lte", "range"} & set(spec.get("ops") or ())):
        p.append(f"{name}: as_of_field {raw!r} must allow lte or range")
    return p


def describe_fields(spec: dict) -> dict:
    """The agent-facing view of a filters spec (type / ops / values / desc), for error payloads."""
    out = {}
    for k, v in (spec or {}).items():
        d = {"type": v.get("type"), "ops": list(v.get("ops") or [])}
        if v.get("values"):
            d["values"] = list(v["values"])
        if v.get("desc"):
            d["desc"] = v["desc"]
        out[k] = d
    return out


def spec_problems(name: str, spec: dict) -> list[str]:
    """Mechanical checks on a declared filters spec (used by the tests over every source)."""
    p = []
    if not isinstance(spec, dict):
        return [f"{name}: filters must be a dict"]
    for k, v in spec.items():
        if not isinstance(v, dict):
            p.append(f"{name}.{k}: field spec must be a dict")
            continue
        t = v.get("type")
        if t not in TYPES:
            p.append(f"{name}.{k}: type {t!r} not in {TYPES}")
            continue
        ops = v.get("ops") or []
        if not ops:
            p.append(f"{name}.{k}: no ops")
        for o in ops:
            if o not in OPS:
                p.append(f"{name}.{k}: op {o!r} not in {OPS}")
            elif o not in _TYPE_OPS[t]:
                p.append(f"{name}.{k}: op {o!r} does not fit type {t}")
        if v.get("values") is not None and t != "keyword":
            p.append(f"{name}.{k}: values only for keyword fields")
        if not v.get("desc"):
            p.append(f"{name}.{k}: desc missing")
        dk = v.get("doc_key")
        if dk is not None and not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*", str(dk)):
            p.append(f"{name}.{k}: doc_key {dk!r} must be a dotted path")
    return p


# ── coverage ──────────────────────────────────────────────────────────────────────────────────────

def _iso(v: Any) -> Optional[date]:
    if v in (None, ""):
        return None
    try:
        return _date_span(v)[0]
    except (ValueError, TypeError):
        return None


def out_of_coverage(clauses: list[Clause], coverage: dict) -> Optional[str]:
    """A reason string when some date filter's whole span lies outside the source's coverage
    (``since`` / ``until``), else None. Unknown coverage bounds never exclude anything."""
    since = _iso((coverage or {}).get("since"))
    until_raw = (coverage or {}).get("until")
    until = _date_span(until_raw)[1] if _iso(until_raw) else None
    if since is None and until is None:
        return None
    for c in clauses:
        if c.type != "date":
            continue
        spans = [c.value] if c.op == "eq" else ([(c.lo, c.hi)] if c.op == "range" else list(c.value))
        if not spans:
            continue
        outside = True
        for lo, hi in spans:
            if not ((since is not None and hi is not None and hi < since)
                    or (until is not None and lo is not None and lo > until)):
                outside = False
        if outside:
            return (f"在覆盖范围外: {c.field} 的范围整段落在本源覆盖 "
                    f"[{since or '?'} .. {until or 'now'}] 之外")
    return None


# ── as_of: point-in-time replay (only what already existed at a moment) ──────────────────────────

AS_OF_SIGNALS = ("现值，非当时值: citations, upvotes, stars and other signals are today's values, "
                 "not the values at as_of")


@dataclass(frozen=True)
class AsOf:
    """A parsed ``as_of``. ``at``: the cut moment in UTC (inclusive). ``day``: its UTC calendar day, the
    bound pushed down to a date field (an int year field gets ``day.year``). ``text``: the caller's text.
    A bare date means the end of that day in UTC."""
    at: datetime
    day: date
    text: str


_AS_OF_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_as_of(value: Any) -> Optional[AsOf]:
    """Parse omniseek_search's ``as_of``: ``YYYY-MM-DD`` (through the end of that day, UTC) or an ISO moment
    WITH a time zone (``2024-05-01T12:00:00+08:00``, ``...Z``). None / "" -> None. Anything else, a
    moment without a time zone included, raises FilterError before anything is sent."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None

    def bad(why: str) -> FilterError:
        return FilterError(f"as_of 写法不对: {value!r} ({why})", [{"field": "as_of", "reason": why}])

    if _AS_OF_DAY_RE.match(s):
        try:
            d = date.fromisoformat(s)
        except ValueError:
            raise bad("not a real date") from None
        return AsOf(datetime.combine(d, time.max, tzinfo=timezone.utc), d, s)
    t = s[:-1] + "+00:00" if s[-1] in "Zz" else s
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        raise bad("use YYYY-MM-DD or an ISO moment with a time zone, e.g. 2024-05-01T12:00:00+08:00") from None
    if dt.tzinfo is None:
        raise bad("a moment needs a time zone, e.g. +08:00 or Z")
    at = dt.astimezone(timezone.utc)
    return AsOf(at, at.date(), s)


def as_of_out_of_coverage(as_of: AsOf, coverage: dict) -> Optional[str]:
    """A reason string when ``as_of`` falls before the source's coverage ``since`` (nothing the source
    holds existed then), else None. An unknown ``since`` never excludes."""
    since = _iso((coverage or {}).get("since"))
    if since is not None and as_of.day < since:
        return f"在覆盖范围外: as_of {as_of.day.isoformat()} 早于本源覆盖起点 {since.isoformat()}"
    return None


def merge_as_of(clauses: list[Clause], field_name: str, spec: dict,
                as_of: AsOf) -> tuple[list[Clause], Optional[str]]:
    """Add ``<field_name> <= as_of`` to a source's typed clauses. A caller clause on the same field keeps
    the tighter upper bound (its lower bound stays). Returns ``(clauses, None)``, or ``(clauses, reason)``
    when the caller's own range lies wholly after as_of (nothing in it existed yet: nothing is sent)."""
    typ = spec.get("type")
    bound = as_of.day if typ == "date" else as_of.day.year
    after = f"晚于 as_of: {field_name} 的过滤范围整段在 as_of {as_of.text} 之后"
    out: list[Clause] = []
    found = False
    for c in clauses:
        if c.field != field_name:
            out.append(c)
            continue
        found = True
        if c.op == "any":
            vals = tuple(v for v in c.value if v <= bound)
            if not vals:
                return clauses, after
            out.append(c if vals == c.value else Clause(c.field, "any", c.type, vals, c.raw))
            continue
        if c.op == "eq" and c.type != "date":
            if c.value > bound:
                return clauses, after
            out.append(c)
            continue
        lo, hi = c.lo, c.hi
        new_hi = bound if hi is None else min(hi, bound)
        if lo is not None and lo > new_hi:
            return clauses, after
        out.append(c if new_hi == hi else Clause(c.field, "range", c.type, (lo, new_hi), c.raw))
    if not found:
        out.append(Clause(field_name, "range", typ, (None, bound), f"<= as_of {as_of.text}"))
    return out, None


def as_of_cut(docs: list, as_of: AsOf) -> tuple[list, int, int]:
    """The exit cut every as_of result passes: keep the documents whose ``date`` is not after
    ``as_of``. Returns ``(kept, dropped_after, dropped_undated)``; a document without a date is dropped
    (it cannot be shown to have existed then)."""
    kept: list = []
    later = undated = 0
    for d in docs or []:
        d_date = getattr(d, "date", None)
        m = to_utc(d_date) if isinstance(d_date, date) else None
        if m is None:
            undated += 1
        elif m > as_of.at:
            later += 1
        else:
            kept.append(d)
    return kept, later, undated


# ── the per-call upstream parameters (read by a source during a filtered call) ────────────────────

_upstream_var: contextvars.ContextVar = contextvars.ContextVar("omniseek_filter_upstream", default=None)


def set_upstream(params: Optional[dict]) -> None:
    """Set (or clear with None) the upstream parameters of the source call running in this context."""
    _upstream_var.set(dict(params) if params else None)


def upstream() -> dict:
    """The upstream parameters a filtered call translated to ({} on an unfiltered call)."""
    return dict(_upstream_var.get() or {})


def fingerprint() -> str:
    """'' on an unfiltered call; else a short stable hash of the upstream parameters. The disk cache
    folds it into every key read or written during the call, so a parameter-translated filter can
    never be answered from (or poison) the unfiltered entry."""
    p = _upstream_var.get()
    if not p:
        return ""
    return hashlib.sha256(json.dumps(p, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


# ── planning a filtered search ────────────────────────────────────────────────────────────────────

@dataclass
class Plan:
    """What a filtered search does per source. ``run``: source -> (translated query, upstream params);
    ``out_of_coverage``: source -> reason (nothing sent); ``skipped``: source -> reason (broad sweep only:
    the source does not declare every filtered field); ``invalid``: source -> reason (broad only: the
    source declares the fields but refused a value or operator)."""
    run: dict = field(default_factory=dict)
    out_of_coverage: dict = field(default_factory=dict)
    skipped: dict = field(default_factory=dict)
    invalid: dict = field(default_factory=dict)
    filtered: bool = False  # the caller passed filters (as_of alone plans too, but filters nothing)
    as_of: Optional[AsOf] = None
    as_of_pushed: list = field(default_factory=list)  # sources the as_of bound was sent to

    def meta(self) -> dict:
        m: dict = {"applied": sorted(self.run)}
        if self.skipped:
            m["skipped_unsupported"] = sorted(self.skipped)
        if self.invalid:
            m["skipped_invalid"] = dict(sorted(self.invalid.items()))
        if self.out_of_coverage:
            m["out_of_coverage"] = sorted(self.out_of_coverage)
        return m


def translate(adapter: Any, query: str, clauses: list[Clause]) -> tuple[str, dict]:
    """Ask the source to translate typed clauses; normalize the answer to (query, params)."""
    fn = getattr(adapter, "translate_filters", None)
    if not callable(fn):
        raise Unsupported(clauses[0].field if clauses else "?", "the source has no translate_filters")
    out = fn(query or "", list(clauses))
    if isinstance(out, tuple):
        q, params = out
        return str(q), dict(params or {})
    return str(out), {}


def _typed_for(adapter: Any, raws: list[RawClause]) -> list[Clause]:
    spec = declared_filters(adapter)
    return [type_clause(r, spec[r.field]) for r in raws]


def plan(raws: list[RawClause], adapters: dict, query: str, named: bool,
         as_of: Optional[AsOf] = None) -> Plan:
    """Validate and translate the filters for every target source BEFORE anything is sent.

    Named search: every named source must accept every clause (field known, operator allowed, value
    typed, translation implemented); any refusal raises FilterError listing each source's fields.
    Broad sweep: a source that does not declare every filtered field is skipped (``skipped``); one
    that declares them but refuses a value is skipped too (``invalid``); a field no target source
    declares, or nothing left to run and nothing out of coverage, raises FilterError.
    A source whose coverage the date filters fall wholly outside lands in ``out_of_coverage``.

    ``as_of`` (parse_as_of): every source runs (none is skipped for lacking a field). One whose coverage
    starts after as_of lands in ``out_of_coverage``; one that declares ``as_of_field`` gets
    ``<field> <= as_of`` merged into its clauses (``as_of_pushed``); the rest run as they are and rely
    on the exit cut (as_of_cut). A source left with no clause runs its query unchanged."""
    p = Plan(filtered=bool(raws), as_of=as_of)
    if not raws and as_of is None:
        return p
    fields = [r.field for r in raws]
    problems: list = []
    available: dict = {}
    for name, adapter in adapters.items():
        spec = declared_filters(adapter)
        missing = [f for f in fields if f not in spec]
        if missing:
            if named:
                available[name] = describe_fields(spec)
                for f in missing:
                    problems.append({"source": name, "field": f, "reason": "unknown field"})
            else:
                p.skipped[name] = f"does not declare {', '.join(missing)}"
            continue
        try:
            clauses = _typed_for(adapter, raws)
            pushed = False
            if as_of is not None:
                reason = as_of_out_of_coverage(as_of, coverage_of(adapter))
                if reason:
                    p.out_of_coverage[name] = reason
                    continue
                af = as_of_field_of(adapter)
                if af and af in spec:
                    clauses, reason = merge_as_of(clauses, af, spec[af], as_of)
                    if reason:
                        p.out_of_coverage[name] = reason
                        continue
                    pushed = True
            reason = out_of_coverage(clauses, coverage_of(adapter))
            if reason:
                p.out_of_coverage[name] = reason
                continue
            p.run[name] = translate(adapter, query, clauses) if clauses else (query, {})
            if pushed:
                p.as_of_pushed.append(name)
        except FilterError as exc:
            if named:
                available[name] = describe_fields(spec)
                problems.extend({"source": name, **pr} for pr in (exc.problems or [{"reason": str(exc)}]))
            else:
                p.invalid[name] = str(exc)
    if named and problems:
        raise FilterError("过滤没通过校验, 没有发出任何请求", problems, available)
    if not named and raws:
        declared_any = {f for a in adapters.values() for f in declared_filters(a)}
        unknown = [f for f in fields if f not in declared_any]
        if unknown or (not p.run and not p.out_of_coverage):
            avail = {n: describe_fields(declared_filters(a)) for n, a in adapters.items()
                     if declared_filters(a)}
            if unknown:
                probs = [{"field": f, "reason": "no searched source declares this field"} for f in unknown]
            else:
                probs = [{"source": n, "reason": r} for n, r in sorted(p.invalid.items())] or [
                    {"reason": "no searched source declares all of these fields"}]
            raise FilterError("没有哪个源能执行这组过滤, 没有发出任何请求", probs, avail)
    return p


def license_text(value: Any) -> str:
    return str(value) if value else "unknown"
