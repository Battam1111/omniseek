"""Layered reading for omniseek_read: brief / head / section over one normalized body.

omniseek_read's two branches (URL, document) each end with one normalized text that the old reader only
windowed by characters. This module is the one place that cuts that text into sections, so the head
listing and the section body come from the same cut:

* ``split`` finds heading lines and turns them into a PARTITION of the text: every section is a
  half-open character range ``[start_char, end_char)``, the ranges are contiguous, the first starts at
  0 and the last ends at ``len(text)``. Every boundary sits at the start of a line, so the character
  before it is a newline; that is what lets a range be fed back into omniseek_read's ``start_char`` /
  ``max_chars`` window and come back byte for byte (the window only snaps back to a newline, and the
  range already ends on one).
* ``read_level`` builds the brief / head / section answer from that cut.

Heading detectors (one per kind of body, chosen by the caller, tried in order until one finds a cut):

* ``markdown``: ATX ``#``/``##``/``###`` lines and setext underlines, outside code fences. Web pages
  reach OmniSeek as markdown (h1 to h3 become these lines), and so do docx headings and .md files.
* ``paper``: text extracted from a PDF paper. PyMuPDF puts a section number and its title on two
  lines ("1" then "Introduction"), or on one; numbers are accepted only in sequence (1, 2, 2.1, 2.2,
  3, ... then A, B, ... for appendices), which drops the table cells and axis labels that look like
  numbers. Unnumbered Abstract / References / Acknowledgements / Appendix lines count too.
* ``sec``: SEC filings. "PART I" and "Item 1A." lines; the table of contents repeats every item, so
  each item keeps its LAST occurrence (the body comes after the contents table).
* ``outline``: the document reader's own sections ("## Slide 3", "## Page 5"), located by label.

Token counts are an ESTIMATE, not a tokenizer count: characters divided by 4, rounded up
(``TOKEN_RULE``). It is a provisional rule; the answer always names it.
"""
from __future__ import annotations

import re
from typing import Optional

LEVELS = ("brief", "head", "section", "full")
TOKEN_RULE = "est_tokens = ceil(chars / 4): an estimate from the character count, not a tokenizer"
FULL_TEXT_TITLE = "全文"
FRONT_TITLE = "开头"
# The brief's abstract is its payload and an abstract's last sentences carry the results, so it is kept
# whole up to the longest abstract arXiv accepts (1920 characters, info.arxiv.org/help/prep.html); a
# longer first paragraph standing in for one is cut at a sentence end inside that.
ABSTRACT_MAX_CHARS = 1920
_TITLE_CAP = 120

_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_EMPH = re.compile(r"(\*\*|__)")


def est_tokens(chars: int) -> int:
    return -(-max(int(chars), 0) // 4)


def _clean(s: str) -> str:
    """A heading line as plain text: links to their text, emphasis marks and table pipes dropped."""
    s = (s or "").replace("\xa0", " ")
    s = _LINK.sub(r"\1", s)
    s = _EMPH.sub("", s)
    s = s.replace("|", " ").replace("¶", " ")
    return re.sub(r"\s+", " ", s).strip()


def _lines(text: str):
    """(start offset, line without its newline) for every line."""
    pos = 0
    for ln in text.split("\n"):
        yield pos, ln
        pos += len(ln) + 1


# ---------------------------------------------------------------------------
# Heading detectors: each returns [(start, title, level)] with start at a line start.
# ---------------------------------------------------------------------------

_ATX = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(```|~~~)")
_SETEXT = re.compile(r"^ {0,3}(=+|-{3,})[ \t]*$")


def _markdown_headings(text: str, skip_to: int = 0, max_level: int = 3) -> list:
    out = []
    lines = list(_lines(text))
    in_fence = False
    for i, (start, ln) in enumerate(lines):
        if _FENCE.match(ln):
            in_fence = not in_fence
            continue
        if in_fence or start < skip_to:
            continue
        m = _ATX.match(ln)
        if m:
            level = len(m.group(1))
            title = _clean(m.group(2))
            if level <= max_level and title:
                out.append((start, title, level))
            continue
        # setext: a one-line paragraph directly underlined by === (h1) or --- (h2)
        if i + 1 < len(lines) and ln.strip() and _SETEXT.match(lines[i + 1][1]):
            prev_blank = i == 0 or not lines[i - 1][1].strip()
            s = ln.strip()
            if prev_blank and len(s) <= _TITLE_CAP and not s.startswith(("|", ">", "-", "*", "+")):
                level = 1 if lines[i + 1][1].strip().startswith("=") else 2
                if level <= max_level:
                    out.append((start, _clean(s), level))
    return out


_PAPER_NUM = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,2}|[A-Z](?:\.\d{1,2}){0,2})\.?$")
_PAPER_ONE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,2}|[A-Z](?:\.\d{1,2}){0,2})\.?\s+(\S.*)$")
_PAPER_APPENDIX = re.compile(r"^appendix\s+([A-Z])\b[\s.:\u2014\u2013-]*(.*)$", re.I)
_PAPER_KEYWORDS = {
    "abstract": "Abstract", "references": "References", "bibliography": "Bibliography",
    "acknowledgements": "Acknowledgements", "acknowledgments": "Acknowledgments",
    "acknowledgement": "Acknowledgement", "acknowledgment": "Acknowledgment",
    "appendix": "Appendix", "appendices": "Appendices", "supplementary material": "Supplementary Material",
}
_NOT_TITLE = re.compile(r"^(Table|Figure|Fig\.|Algorithm|Eq\.|Equation|Listing)\b")
_PURE_NUMBER = re.compile(r"^[\d.,%()+\-\s]+$")


def _plausible_title(s: str, strict_case: bool = False) -> bool:
    s = s.strip()
    if not (2 <= len(s) <= 90) or not (s[0].isalpha() and s[0].isupper()):
        return False
    if _NOT_TITLE.match(s) or s.endswith((",", ";")) or " = " in s:
        return False
    chars = [c for c in s if not c.isspace()]
    if sum(c.isalpha() for c in chars) < 0.6 * len(chars):
        return False
    words = s.split()
    if s.endswith(".") and len(words) > 6:
        return False
    if strict_case:
        long_words = [w for w in words if len(w) >= 4]
        if long_words and sum(w[0].isupper() for w in long_words) * 2 < len(long_words):
            return False
    return True


class _PaperNumbering:
    """Accepts section numbers only in document order: 1, 2, 2.1, 2.2, 2.2.1, 3, ..., then A, B, A.1."""

    def __init__(self):
        self.top = 0          # last numeric top level
        self.appendix = ""    # last appendix letter
        self.sub = 0
        self.subsub = 0
        self.refs_seen = False
        self.appendix_kw = False  # an "Appendix" / "Appendices" line was seen

    def accept(self, num: str) -> bool:
        parts = num.split(".")
        head = parts[0]
        try:
            rest = [int(p) for p in parts[1:]]
        except ValueError:
            return False
        if head.isdigit():
            if self.appendix:
                return False
            cur = self.top
            if len(parts) == 1:
                if int(head) != cur + 1:
                    return False
                self.top, self.sub, self.subsub = int(head), 0, 0
                return True
            if int(head) != cur or cur == 0:
                return False
        else:
            if len(parts) == 1:
                ok = ((not self.appendix and head == "A" and (self.refs_seen or self.appendix_kw))
                      or (self.appendix and ord(head) == ord(self.appendix) + 1))
                if not ok:
                    return False
                self.appendix, self.sub, self.subsub = head, 0, 0
                return True
            if head != self.appendix:
                return False
        if len(rest) == 1:
            if rest[0] != self.sub + 1:
                return False
            self.sub, self.subsub = rest[0], 0
            return True
        if rest[0] != self.sub or rest[1] != self.subsub + 1:
            return False
        self.subsub = rest[1]
        return True


def _paper_headings(text: str) -> list:
    lines = list(_lines(text))
    numbering = _PaperNumbering()
    out = []
    i = 0
    while i < len(lines):
        start, ln = lines[i]
        s = ln.strip()
        if not s:
            i += 1
            continue
        prev = lines[i - 1][1].strip() if i > 0 else ""
        nxt = lines[i + 1][1].strip() if i + 1 < len(lines) else ""
        m = _PAPER_NUM.match(s)
        if m and nxt and _plausible_title(nxt) and not _PURE_NUMBER.match(prev or "x"):
            num = m.group(1)
            if numbering.accept(num):
                out.append((start, f"{num} {_clean(nxt)}", num.count(".") + 1))
                i += 2
                continue
        m = _PAPER_ONE.match(s)
        if m:
            num, title = m.group(1), m.group(2)
            letter = not num[0].isdigit()
            # "A. Radford and K. Narasimhan" opens reference lines; a one-line appendix heading has no dot
            dotted = s[len(num):len(num) + 1] == "."
            if (_plausible_title(title, strict_case=letter) and not _PURE_NUMBER.match(prev or "x")
                    and not (letter and dotted) and numbering.accept(num)):
                out.append((start, f"{num} {_clean(title)}", num.count(".") + 1))
                i += 1
                continue
        m = _PAPER_APPENDIX.match(s)
        if m and len(s) <= 90 and numbering.accept(m.group(1).upper()):
            out.append((start, _clean(s), 1))
            i += 1
            continue
        kw = s.rstrip(".:").strip().lower()
        if kw in _PAPER_KEYWORDS or re.match(r"^abstract\s*[\u2014\u2013:.-]\s*\S", s, re.I):
            key = kw if kw in _PAPER_KEYWORDS else "abstract"
            if key in ("references", "bibliography"):
                numbering.refs_seen = True
            elif key in ("appendix", "appendices", "supplementary material"):
                numbering.appendix_kw = True
            out.append((start, _PAPER_KEYWORDS[key], 1))
        i += 1
    numbered = sum(1 for _, t, _ in out if t not in _PAPER_KEYWORDS.values())
    return out if numbered >= 2 else []


_SEC_ITEM = re.compile(r"(?i)^[\s#>*\[_]*item\s+(\d{1,2}(?:\.\d{2})?[a-d]?)\b(?!\.\d)(.*)$")
_SEC_PART = re.compile(r"(?i)^[\s#>*\[_]*part\s+(iv|i{1,3})\b(.*)$")


_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4}


def _sec_order(kind: str, key: str, part: int) -> tuple:
    if kind == "PART":
        return (_ROMAN.get(key, 0), -1, 0, "")
    m = re.match(r"(\d+)(?:\.(\d+))?([A-D]?)$", key)
    return (part, int(m.group(1)), int(m.group(2) or 0), m.group(3)) if m else (part, 0, 0, key)


def _sec_headings(text: str) -> list:
    """PART and Item lines. An item is keyed by the PART it sits under (a 10-Q has an Item 1 in
    PART I and another in PART II); the contents table repeats every key, so each key keeps its LAST
    occurrence. Then only the longest run whose order agrees with document order is kept: when the
    body was cut short (a capped render), a contents-table line can be the last occurrence of an item
    the body lost, and it would otherwise open a section before PART I."""
    found: dict = {}
    part = 0
    for start, ln in _lines(text):
        s = _clean(ln)
        if not s or len(s) > 200:
            continue
        for rx, kind, level in ((_SEC_PART, "PART", 1), (_SEC_ITEM, "Item", 2)):
            m = rx.match(s)
            if m:
                key = m.group(1).upper()
                if kind == "PART":
                    part = _ROMAN.get(key, part)
                rest = m.group(2).strip(" .:-\u2014\u2013")
                label = f"{kind} {key}." if kind == "Item" else f"{kind} {key}"
                title = f"{label} {rest}".strip() if rest else label
                okey = _sec_order(kind, key, part)
                found[okey] = (start, title[:_TITLE_CAP], level, okey)   # last occurrence wins
                break
    heads = sorted(found.values())
    # longest run increasing in (part, item) order, in document order
    best = [1] * len(heads)
    back = [-1] * len(heads)
    for i in range(len(heads)):
        for j in range(i):
            if heads[j][3] < heads[i][3] and best[j] + 1 > best[i]:
                best[i], back[i] = best[j] + 1, j
    if not heads:
        return []
    i = max(range(len(heads)), key=lambda k: best[k])
    keep = []
    while i >= 0:
        keep.append(heads[i][:3])
        i = back[i]
    return keep[::-1]


def _outline_headings(text: str, labels: list) -> list:
    out, pos = [], 0
    for label in labels or []:
        needle = f"## {label}"
        at = pos
        while True:
            at = text.find(needle, at)
            if at < 0:
                break
            line_end = text.find("\n", at)
            line_end = len(text) if line_end < 0 else line_end
            if (at == 0 or text[at - 1] == "\n") and text[at:line_end].rstrip() == needle:
                break
            at += 1
        if at < 0:
            continue
        out.append((at, str(label), 1))
        pos = at + len(needle)
    return out


_NO_STRUCTURE_REASON = {
    "markdown": "no heading lines (# / ## / ###) found",
    "paper": "no numbered section headings found in sequence",
    "sec": "no PART / Item headings found",
    "outline": "the document has a single part",
}


def split(text: str, kinds, *, labels: Optional[list] = None, skip_to: int = 0) -> dict:
    """Cut ``text`` into sections with the first detector in ``kinds`` that finds a cut.

    Returns {"structure": the kind used or "none", "sections": [section dicts], "note": reason or
    None}. Section dict: {index (1-based), title, level, start_char, end_char, chars, est_tokens}.
    The ranges partition the text. With no cut, one section titled 全文 covers the whole text and
    ``note`` says why."""
    text = text or ""
    tried = []
    for kind in kinds or []:
        if kind == "markdown":
            heads = _markdown_headings(text, skip_to=skip_to)
        elif kind == "paper":
            heads = _paper_headings(text)
        elif kind == "sec":
            heads = _sec_headings(text)
        elif kind == "outline":
            heads = _outline_headings(text, labels or [])
        else:
            raise ValueError(f"unknown section kind {kind!r}")
        heads = sorted({h[0]: h for h in heads if h[0] < len(text)}.values())
        bounds = _bounds(text, heads, skip_to)
        if len(bounds) >= 2:
            return {"structure": kind, "sections": _section_dicts(bounds), "note": None}
        tried.append(f"{kind}: {_NO_STRUCTURE_REASON[kind]}")
    note = "no section structure found; " + ("; ".join(tried) if tried else
                                             "this format carries no heading markup")
    return {"structure": "none",
            "sections": _section_dicts([(0, len(text), FULL_TEXT_TITLE, 1)]), "note": note}


def _bounds(text: str, heads: list, skip_to: int) -> list:
    if not heads:
        return []
    out = []
    first = heads[0][0]
    if text[skip_to:first].strip():
        out.append((0, first, FRONT_TITLE, 1))
    else:
        heads = [(0, heads[0][1], heads[0][2])] + list(heads[1:])
    for i, (start, title, level) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        out.append((start, end, title[:_TITLE_CAP], level))
    return out


def _section_dicts(bounds: list) -> list:
    return [{"index": i, "title": title, "level": level, "start_char": s, "end_char": e,
             "chars": e - s, "est_tokens": est_tokens(e - s)}
            for i, (s, e, title, level) in enumerate(bounds, 1)]


# ---------------------------------------------------------------------------
# Section lookup
# ---------------------------------------------------------------------------

_LEAD_NUMBER = re.compile(r"^(?:(?:item|part)\s+)?(?:\d{1,2}(?:\.\d{1,2})*[a-d]?|[A-Z](?:\.\d{1,2})*|[ivx]+)\.?\s+",
                          re.I)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().casefold())


def _prefix_at_boundary(query: str, name: str) -> bool:
    if not name.startswith(query):
        return False
    return len(name) == len(query) or not name[len(query)].isalnum() or not query[-1].isalnum()


def find_section(sections: list, query) -> tuple:
    """(section, other matches) for a title (case-insensitive, prefix allowed, the leading number
    may be left out) or an ``index`` from the head listing; (None, []) when nothing matches."""
    q = str(query if query is not None else "").strip()
    if not q:
        return None, []
    if q.isdigit():
        n = int(q)
        hit = next((s for s in sections if s["index"] == n), None)
        return hit, []
    nq = _norm(q)
    names = [(s, _norm(s["title"]), _norm(_LEAD_NUMBER.sub("", s["title"], count=1))) for s in sections]
    exact = [s for s, full, bare in names if nq in (full, bare)]
    if exact:
        return exact[0], exact[1:]
    pref = [s for s, full, bare in names if _prefix_at_boundary(nq, full) or _prefix_at_boundary(nq, bare)]
    if not pref:
        pref = [s for s, full, bare in names if full.startswith(nq) or bare.startswith(nq)]
    return (pref[0], pref[1:]) if pref else (None, [])


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------

_ABSTRACT_TITLES = {"abstract", "summary", "tl;dr", "tldr", "executive summary", "摘要", "概要"}
_SENTENCE_END = re.compile(r"[.!?。！？](\s|$)")
_ARXIV_STAMP = re.compile(r"arXiv:\S+\s+\[[^\]]+\]\s+(\d{1,2} [A-Z][a-z]{2} \d{4})")
_FISCAL_YEAR = re.compile(r"(?i)for the (?:fiscal )?year ended\s+([a-z]+\.?\s\d{1,2},\s\d{4})")


def _flat(s: str) -> str:
    return re.sub(r"\s+", " ", _LINK.sub(r"\1", s or "")).strip()


def _cap(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    cut = s[: max(n - 1, 1)]
    sp = cut.rfind(" ")
    if sp > n // 2:
        cut = cut[:sp]
    return cut.rstrip() + "…"


def _cap_sentences(s: str, n: int) -> str:
    """``s`` whole when it fits in ``n``; else up to the last sentence end inside ``n`` (a word cut
    when no sentence ends past the half)."""
    if len(s) <= n:
        return s
    end = 0
    for m in _SENTENCE_END.finditer(s):
        if m.end() > n:
            break
        end = m.end()
    return s[:end].rstrip() + " …" if end > n // 2 else _cap(s, n)


def _body_after_heading(text: str, sec: dict) -> str:
    chunk = text[sec["start_char"]:sec["end_char"]]
    nl = chunk.find("\n")
    first = chunk if nl < 0 else chunk[:nl]
    body = "" if nl < 0 else chunk[nl + 1:]
    # an inline abstract ("Abstract: We present ...") keeps the words after the label
    m = re.match(r"(?i)^\s*abstract\s*[\u2014\u2013:.-]\s*(\S.*)$", first)
    return (m.group(1) + "\n" + body) if m else body


def _first_paragraph(text: str, start: int) -> str:
    for para in re.split(r"\n\s*\n", text[start:]):
        p = para.strip()
        if not p or p.startswith(("|", "#", "-", "*", "+", ":", "`", ">", "!", "[")):
            continue
        flat = _flat(p)
        if len(flat.split()) >= 12 and _SENTENCE_END.search(flat):
            return flat
    return ""


def _looks_like_name(title: str) -> bool:
    t = (title or "").strip()
    return not t or " " not in t


def _brief(text: str, cut: dict, meta: dict) -> dict:
    sections = cut["sections"]
    kind = cut["structure"]
    title = (meta.get("title") or "").strip()
    author = (meta.get("author") or "").strip()
    date = (meta.get("date") or "").strip()
    date_from = "source metadata" if date else ""
    first_lines = [ln.strip() for ln in text[:4000].split("\n")
                   if ln.strip() and not re.match(r"^## (Page|Slide|Sheet:?) ", ln.strip())]
    if kind == "paper":
        front = next((s for s in sections if s["title"] == FRONT_TITLE), None)
        lines = ([ln.strip() for ln in text[front["start_char"]:front["end_char"]].split("\n")
                  if ln.strip() and not ln.startswith("## ")] if front else first_lines[:1])
        # a title wraps when its line ends in ":" or "-", or the next line goes on in lower case
        n_title = 1
        while (n_title < min(len(lines), 3)
               and (lines[n_title - 1].endswith((":", "-", "\u2013", "\u2014")) or lines[n_title][:1].islower())):
            n_title += 1
        if _looks_like_name(title) and lines and len(" ".join(lines[:n_title])) <= 250:
            title = " ".join(lines[:n_title])
        if not author and len(lines) > n_title:
            author = _flat(" ".join(lines[n_title:]))
    if not date:
        m = _ARXIV_STAMP.search(text[:20000])
        if m:
            date, date_from = m.group(1), "arXiv stamp in the text"
        elif kind == "sec":
            m = _FISCAL_YEAR.search(text[:20000])
            if m:
                date, date_from = _flat(m.group(1)), "fiscal year end on the cover page"

    abstract, abstract_from = "", ""
    for s in sections:
        bare = _norm(_LEAD_NUMBER.sub("", s["title"], count=1))
        if bare in _ABSTRACT_TITLES:
            abstract, abstract_from = _flat(_body_after_heading(text, s)), f"section {s['title']!r}"
            break
    if not abstract:
        begin = 0
        if kind == "sec":
            item1 = next((s for s in sections if re.match(r"(?i)^item 1\.", s["title"])), None)
            begin = item1["start_char"] if item1 else 0
        abstract = _first_paragraph(text, begin)
        abstract_from = "first paragraph" if abstract else ""
    if not abstract:
        abstract, abstract_from = _flat(text[:1200]), "start of the text"

    out = {"title": _cap(_flat(title), 200), "author": _cap(author, 300) or None,
           "source": _cap(meta.get("source") or "", 120), "date": date or None,
           "abstract": _cap_sentences(abstract, ABSTRACT_MAX_CHARS), "abstract_from": abstract_from}
    if date_from:
        out["date_from"] = date_from
    return out


# ---------------------------------------------------------------------------
# The one entry point both omniseek_read branches call
# ---------------------------------------------------------------------------

def read_level(text: str, level: str, *, section="", kinds=("markdown",), labels=None,
               skip_to: int = 0, meta: Optional[dict] = None, max_chars: int = 24000) -> dict:
    """The brief / head / section answer for ``text`` (``level="full"`` stays with the caller).

    brief: {"level", "brief": {title, author, source, date, abstract, abstract_from}, ...}
    head: {"level", "structure", "sections": [...], "note"?}
    section: {"level", "section": {...}, "returned_chars", "truncated", "next_start_char"?, "text"}
      or {"level", "error", "available_sections"} when ``section`` names nothing.
    All carry total_chars, est_tokens and token_rule."""
    text = text or ""
    meta = meta or {}
    cut = split(text, kinds, labels=labels, skip_to=skip_to)
    base = {"level": level, "total_chars": len(text), "est_tokens": est_tokens(len(text)),
            "token_rule": TOKEN_RULE}
    if level == "brief":
        base["brief"] = _brief(text, cut, meta)
        base["n_sections"] = len(cut["sections"])
        return base
    base["structure"] = cut["structure"]
    if cut["note"]:
        base["note"] = cut["note"]
    if level == "head":
        base["sections"] = cut["sections"]
        return base
    if level != "section":
        raise ValueError(f"read_level handles brief/head/section, not {level!r}")
    hit, others = find_section(cut["sections"], section)
    if hit is None:
        base["error"] = (f"section {section!r} not found; pass a title (case-insensitive, prefix "
                         f"allowed) or an index from level='head'")
        base["available_sections"] = [f"{s['index']}: {s['title']}" for s in cut["sections"]]
        return base
    from omniseek.core.docreader import _window
    s, e = hit["start_char"], hit["end_char"]
    chunk, truncated = _window(text[:e], s, max_chars)
    base["section"] = hit
    base["returned_chars"] = len(chunk)
    base["truncated"] = truncated
    if truncated:
        base["next_start_char"] = s + len(chunk)
    if others:
        base["other_matches"] = [f"{o['index']}: {o['title']}" for o in others[:10]]
    base["text"] = chunk  # last: an answer cut short keeps where to continue (next_start_char)
    return base


def kinds_for_url(url: str, doc: dict) -> list:
    """Which detectors suit a URL-branch body: a PDF paper, a SEC filing, else markdown (h1 to h3)."""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    meta = doc.get("metadata") or {}
    if host == "sec.gov" or host.endswith(".sec.gov"):
        return ["sec", "markdown"]
    if doc.get("source") == "pdf" or "pages" in meta:
        return ["paper", "markdown"]
    return ["markdown"]


def read_url_level(url: str, doc: dict, level: str, *, section="", max_chars: int = 24000) -> dict:
    """read_level for a fetched URL document (Document.to_tool_dict output)."""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    src = doc.get("source") or ""
    meta = {"title": doc.get("title") or "", "author": doc.get("author") or "",
            "date": str(doc.get("date") or "")[:10],
            "source": f"{src}: {host}" if host and src else (src or host)}
    return read_level(doc.get("content") or "", level, section=section,
                      kinds=kinds_for_url(url, doc), meta=meta, max_chars=max_chars)
