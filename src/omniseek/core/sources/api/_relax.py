"""Relaxing a short keyword AND match, shared by the keyword-only paper upstreams (dblp, arXiv).

Why (2026-10-18): a person writes a sentence ("threshold verifiable random function
post-quantum lattice"); an upstream that only matches keywords reads it by its own rule. dblp's title
text index wants EVERY word in the title, so a long sentence almost always matches nothing; arXiv
joined the bare words with OR and ranked physics papers on "lattice" first. Both adapters now send the
words ANDed, and when that match comes back short this module drops words one at a time until it is
long enough. The order comes from the upstream itself: it reports how many records each word matches
on its own, and the least distinctive word (the one matching the most) goes first. A word matching
nothing goes before all of them, since every AND that keeps it is empty.

The adapters own the I/O (what a search or a count request is for their upstream); this module owns
only the plan, written once as a generator so the sync and the async driver cannot drift apart.

PROVISIONAL NUMBERS (measured 2026-10-18):
``MIN_HITS`` and ``MAX_DROP``. ``MIN_WORDS`` is not a tunable: a result is on topic when its title
holds at least two of the query's content words, so a one-word match cannot be on topic by that rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Generator, Optional

# Stop relaxing once a word set returns at least this many records (capped by the caller's limit).
MIN_HITS = 3
# Drop at most this many words from the full sentence.
MAX_DROP = 4
# Never relax below two words (see the module docstring: derived, not tuned).
MIN_WORDS = 2
# More words only narrow an AND match further; past this the query is long and the match tiny.
MAX_WORDS = 8

# English function words that carry no title signal (arXiv even treats ``all:the`` as a filter that
# matches 9k records, so ANDing one in would cut the match instead of being ignored).
STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into", "is", "it",
    "its", "of", "on", "or", "the", "their", "this", "that", "to", "via", "was", "were", "with",
})


@dataclass
class Outcome:
    """What a relaxed search ended with: the records of the word set used, that set, every set the
    upstream answered (in order), and why relaxing stopped early ("" when it ran its course). ``failed`` is True only
    when the first, full search failed: the adapter then reports a failure, not an empty answer."""
    rows: list
    words: list
    tried: list = field(default_factory=list)
    stopped: str = ""
    failed: bool = False


def drop_order(words: list, counts: dict) -> list:
    """The words to drop, first to last: words matching nothing, then the most frequent; ties keep
    the query's order. A word missing from ``counts`` matched nothing."""
    pos = {w: i for i, w in enumerate(words)}

    def key(w):
        n = int(counts.get(w) or 0)
        return (0 if n == 0 else 1, -n, pos[w])
    return sorted(words, key=key)


def _plan(words: list, want: int, max_drop: int) -> Generator:
    """Yields ``("search", words)`` (reply: records or None on failure) and ``("counts", words)``
    (reply: ``{word: count}`` or None); returns an ``Outcome``."""
    words = list(words)
    rows = yield ("search", list(words))
    if rows is None:
        return Outcome([], words, [], "search failed", failed=True)
    tried = [list(words)]
    if len(rows) >= want or len(words) <= MIN_WORDS or max_drop <= 0:
        return Outcome(rows, words, tried)
    counts = yield ("counts", list(words))
    if counts is None:
        return Outcome(rows, words, tried, "word counts unavailable, not relaxed")
    best_rows, best_words = rows, list(words)
    current = list(words)
    for w in drop_order(words, counts)[:max_drop]:
        if len(current) <= MIN_WORDS:
            break
        current = [x for x in current if x != w]
        got = yield ("search", list(current))
        if got is None:
            return Outcome(best_rows, best_words, tried,
                           f"the relaxed search for '{' '.join(current)}' failed")
        tried.append(list(current))
        if len(got) >= len(best_rows):
            best_rows, best_words = got, list(current)
        if len(got) >= want:
            break
    return Outcome(best_rows, best_words, tried)


async def arun(words: list, search: Callable[[list], Awaitable[Optional[list]]],
               counts: Callable[[list], Awaitable[Optional[dict]]], *, want: int,
               max_drop: Optional[int] = None) -> Outcome:
    """Async twin of ``run``: the same plan, awaited callables."""
    gen = _plan(words, max(1, want), MAX_DROP if max_drop is None else max_drop)
    reply = None
    try:
        while True:
            kind, ws = gen.send(reply)
            reply = await (search(ws) if kind == "search" else counts(ws))
    except StopIteration as stop:
        return stop.value


def tried_text(outcome: Outcome) -> str:
    """The word sets sent, for a diagnostic: ``a b c | a b``."""
    return " | ".join(" ".join(ws) for ws in outcome.tried)

