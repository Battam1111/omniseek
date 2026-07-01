"""Unified document model for all Polaris eye sources.

Every adapter returns content normalized to PolarisDocument so downstream
processing doesn't care whether content came from Reddit, Zhihu, or arXiv.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


# ── lenient tool-arg coercion (borrowed: exa-mcp validation.ts) ─────────────────────────────────
def _coerce_int(v: Any) -> Any:
    """Accept '10' / 10.0 for an int param. LLMs routinely pass numbers as strings → a hard
    ValidationError that wastes the whole tool call. Non-coercible values pass through so the real
    int validator still raises a clear message."""
    if isinstance(v, str):
        s = v.strip()
        try:
            return int(s)
        except ValueError:
            try:
                return int(float(s))
            except ValueError:
                return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _coerce_bool(v: Any) -> Any:
    """Accept 'true'/'false'/'1'/'0'/'yes'/'no' (any case) for a bool param."""
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "y", "on"):
            return True
        if s in ("false", "0", "no", "n", "off", ""):
            return False
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    return v


LenientInt = Annotated[int, BeforeValidator(_coerce_int)]
LenientBool = Annotated[bool, BeforeValidator(_coerce_bool)]


# ── block detection (borrowed: crawl4ai antibot_detector + the eye's own 风控 texts) ────────────
# A fetch that comes back EMPTY but whose RAW page matches one of these is a BLOCK, not authoritative
# 'no results'. Lets an adapter LABEL a [] as a block (surface + pace/retry) instead of silently
# treating it as 'nothing there' — the gap-③ false-empty's detect-half.
_BLOCK_MARKERS = (
    "just a moment", "attention required", "checking your browser", "verify you are human",
    "enable javascript and cookies", "access denied", "403 forbidden", "unusual traffic",
    "are you a robot", "captcha", "cf-chl", "__cf_chl", "ddos-guard",
    "访问频次异常", "操作太频繁", "请稍候再试", "请稍后再试", "您两次操作间隔", "人机验证",
    "拖动滑块", "访问链接异常", "检测到非正常操作", "账号异常",
)


def is_blocked(text: str) -> tuple[bool, str]:
    """Heuristic: does this page look BLOCKED (anti-bot / captcha / flood-control) vs genuinely
    empty? Returns (True, marker) on a block signal else (False, ''). Pure, zero-dependency."""
    if not text:
        return False, ""
    low = text.lower()
    for m in _BLOCK_MARKERS:
        if m in low:
            return True, m
    if len(text) < 600 and ("challenge-platform" in low or "/cdn-cgi/challenge" in low):
        return True, "short_challenge_shell"
    return False, ""


# ── declarative HTML extraction (borrowed: crawl4ai JsonCssExtractionStrategy, MINUS eval/LLM) ──
def schema_extract(html: str, schema: dict) -> list[dict]:
    """Declarative HTML → list[dict] via CSS selectors. ``schema`` =
    ``{item_selector, fields: {name: {selector?, attr?}}}``: each ``item_selector`` match yields one
    dict of fields; ``attr`` is 'text' (default) | 'html' | any HTML attribute name; a missing
    ``selector`` reads the item element itself. Lets a scrape source be declared as DATA instead of
    hand-written BeautifulSoup. ZERO code execution — crawl4ai's ``_compute_field`` eval path is
    deliberately NOT ported (it was an injection face)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "lxml")
    item_sel = schema.get("item_selector")
    items = soup.select(item_sel) if item_sel else [soup]
    fields = schema.get("fields") or {}
    out: list[dict] = []
    for it in items:
        row: dict = {}
        for name, spec in fields.items():
            sel = spec.get("selector") if isinstance(spec, dict) else spec
            el = it.select_one(sel) if sel else it
            if el is None:
                row[name] = None
                continue
            attr = (spec.get("attr") if isinstance(spec, dict) else None) or "text"
            if attr == "text":
                row[name] = el.get_text(" ", strip=True)
            elif attr == "html":
                row[name] = str(el)
            else:
                row[name] = el.get(attr)
        out.append(row)
    return out


class Signal(BaseModel):
    """One named, source-reported mechanical FACT about a document: a count, a rating, a salary.
    Not a judgment. The eye records what a source asserted (with provenance); the agent / the
    ranker decide what it is worth. Replaces the old fused score scalar (see docs/PHILOSOPHY.md)."""

    model_config = ConfigDict(frozen=True)

    value: Optional[float] = None      # the number a source reported (None if the source had none)
    kind: str = 'other'                # semantic class: engagement | citation | compensation | other
    computed_by: str = ''              # provenance, e.g. source:reddit/score
    unit: Optional[str] = None         # e.g. SGD/month, citations, stars


# Signal kinds the ranker treats as ATTENTION (preserves the old score-fed engagement term:
# social counts AND citations both fed it). compensation / other are NOT attention.
_ATTENTION_KINDS = frozenset({'engagement', 'citation'})


class PolarisDocument(BaseModel):
    """Normalized representation of content from any Polaris eye source."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Identity
    source: str = Field(description="Source identifier, e.g. 'reddit', 'zhihu', 'arxiv'")
    source_id: str = Field(description="Source-specific ID (post id, paper id, etc.)")
    url: str = Field(description="Canonical URL")

    # Content
    title: str
    content: str = Field(description="Main content as Markdown")
    author: Optional[str] = None
    date: Optional[datetime] = None

    # Discovery signals: named, source-reported FACTS (a count, a citation tally, a salary), each
    # with provenance + a semantic kind. Never a judgment: the agent / the ranker decide worth.
    # Replaces the old fused score / relevance scalars (see docs/PHILOSOPHY.md).
    signals: dict[str, Signal] = Field(default_factory=dict)

    # Categorization
    tags: list[str] = Field(default_factory=list)

    # Multimodal — image/video URLs from the source (a vision-capable agent can view these)
    media: list[str] = Field(default_factory=list)

    # Free-form per-source metadata. Convention: metadata["raw"] holds the source's
    # original payload — a lossless escape hatch under the normalized view.
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_markdown(self) -> str:
        """Render as Markdown with YAML frontmatter."""
        lines = ["---"]
        lines.append(f"source: {self.source}")
        lines.append(f"source_id: {self.source_id}")
        lines.append(f"url: {self.url}")
        lines.append(f"title: {_yaml_quote(self.title)}")
        if self.author:
            lines.append(f"author: {_yaml_quote(self.author)}")
        if self.date:
            lines.append(f"date: {self.date.isoformat()}")
        for _name, _s in self.signals.items():
            lines.append(f"signal {_name}: {_s.value} ({_s.kind})")
        if self.tags:
            lines.append("tags: [" + ", ".join(_yaml_quote(t) for t in self.tags) + "]")
        lines.append("---")
        lines.append("")
        lines.append(f"# {self.title}")
        lines.append("")
        lines.append(self.content)
        return "\n".join(lines)

    def to_summary(self) -> str:
        """One-line summary for list views."""
        parts = [f"[{self.source}]", self.title]
        if self.author:
            parts.append(f"— {self.author}")
        if self.date:
            parts.append(f"({self.date.date().isoformat()})")
        return " ".join(parts)

    def attention_value(self) -> Optional[float]:
        """Max value among ATTENTION-class signals (engagement + citation). The ranker engagement
        term and the recall index read this; it replaces the old fused score. None if no such
        signal. Mechanical (a max over named facts), not a judgment."""
        vals = [s.value for s in self.signals.values()
                if s.kind in _ATTENTION_KINDS and isinstance(s.value, (int, float))]
        return max(vals) if vals else None

    def to_tool_dict(self, *, full: bool = False, content_cap: int = 2000) -> dict:
        """Agent-facing serialization for the MCP eye_* tools — the single lean projection.

        ALWAYS drops ``metadata['raw']``: that lossless escape-hatch is write-only (no code
        path consumes it — every useful field is already parsed out of it), so it is pure
        token weight in a tool response (~65% of a ranked-search payload). It stays on the
        cached document; it just isn't sent to the agent.

        ``full=False`` (DISCOVERY — eye_search / eye_search_ranked, where the agent triages
        many results) caps ``content`` to a ``content_cap``-char preview and sets
        ``content_truncated`` + ``content_full_chars`` so the agent knows to drill in
        (``eye_add_url`` on this doc's ``url``) for the whole text. ``full=True`` (DRILL-DOWN —
        eye_fetch / eye_add_url, a source/URL the agent already chose) keeps content whole.
        """
        d = self.model_dump(mode="json")
        meta = d.get("metadata")
        if isinstance(meta, dict) and "raw" in meta:
            d["metadata"] = {k: v for k, v in meta.items() if k != "raw"}
        if not full:
            content = d.get("content") or ""
            if len(content) > content_cap:
                d["content"] = content[:content_cap]
                d["content_truncated"] = True
                d["content_full_chars"] = len(content)
        return d


def jsonsafe(obj, _depth: int = 0):
    """Best-effort convert an arbitrary value to a JSON-serializable structure, for
    stashing a source's raw payload in ``metadata['raw']`` (the lossless escape hatch).

    Dicts/lists recurse; objects try ``model_dump``/``dict``/``_asdict``; datetimes →
    isoformat; anything else → ``str``. Depth-capped against pathological nesting.
    """
    if _depth > 6:
        return str(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): jsonsafe(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonsafe(v, _depth + 1) for v in obj]
    for attr in ("model_dump", "dict", "_asdict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return jsonsafe(fn(), _depth + 1)
            except Exception:  # noqa: BLE001
                break
    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except Exception:  # noqa: BLE001
            pass
    return str(obj)


def keyword_score_filter(docs: list[PolarisDocument], query: str) -> list[PolarisDocument]:
    """Filter + rank docs by lexical relevance to ``query`` (title weighted 3x over content).

    Delegates to ``penumbra.core.relevance`` (BM25-shaped: tf saturation + length
    normalization + CJK bigrams + ASCII word boundaries), which replaced raw
    term-counting after it measurably drowned true hits (term spam, long docs,
    CJK exact-substring only).

    Contract unchanged: an empty / term-less query returns ``docs`` as given, so
    the caller's own pre-sort order is preserved. With real terms, only docs
    matching at least one term come back, best first; no match means ``[]``, an
    honest "no match" rather than a silent fallback to the unfiltered list.
    """
    from penumbra.core.relevance import filter_rank  # local import: keep normalize dependency-free
    return filter_rank(docs, query)


def mk_signal(name: str, value, kind: str = 'engagement', by: str = '', unit: Optional[str] = None) -> dict:
    """Build a one-entry {name: Signal} map for an adapter to pass as signals=. by is the
    source-side provenance (e.g. reddit/score), stamped as computed_by=source:<by>. Coerces a
    numeric value to float, else None. Replaces the old score=<n> constructor kwarg."""
    val = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return {name: Signal(value=val, kind=kind, computed_by=('source:' + by if by else ''), unit=unit)}


# Comment/paragraph-level provenance (feature #3). Pure helpers: the agent reads them,
# they do NOT feed composite()/ranking coefficients.
def comment_anchor(source: str, source_id: str, comment_id: str) -> str:
    """Build a stable per-comment provenance URI from (source, source_id, comment_id), so a
    quoted comment can be cited back to its exact anchor. Pure string builder, no side effects."""
    return f'{source}:{source_id}#comment-{comment_id}'


# The keys each per-comment dict is guaranteed to carry in adapter output. Documents what
# EXISTS, not what might exist: 'ts' is deliberately EXCLUDED because the raw XHS comment
# API timestamp field name is unverified. A future builder who verifies it adds 'ts' then.
COMMENT_SCHEMA_KEYS = ('author', 'text', 'likes', 'id')


def _yaml_quote(s: str) -> str:
    """Quote a string for YAML if it contains special characters."""
    if any(c in s for c in ':#\n"\''):
        # Escape any inner double quotes and wrap
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s
