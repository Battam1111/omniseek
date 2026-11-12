"""The root of every native source: the ONE sync entry, and the cache round-trip every source repeats.

A source writes its search ONCE, as ``async def asearch(self, query, limit)``. The fan-out awaits it on
the service's loop (``fetcher._dispatch_search``). A caller that has no loop in hand (a standalone
script, a thread of the service such as a health probe) calls ``search`` here, which runs that same
``asearch`` through ``portal.call``: on the service's loop when the process has one, else on the
process's own loop. There is no second, synchronous implementation of any source to keep in step:
never define ``search`` on a native source (and never a sync twin of an async helper).

A source whose fetch can only block (a browser, a library with no async client) is the one exception:
it writes a plain ``def search`` and no ``asearch``; the fan-out runs it on a worker thread
(``fetcher._aegress``). The two template bases (``scrape._base.BaseScrapeAdapter``,
``api._base.BaseAPIAdapter``) take such a blocking hook (``_raw_fetch``) too and run it on a thread
themselves, so a base subclass is native either way.

A health check is written the same way, ONCE, as ``async def ahealth_check(self)``; a blocking source
writes a plain ``def health_check`` and SourceBase makes that its ``ahealth_check``, run on a thread of
the legacy lane (``SourceBase._athreaded_health``).

``_acached`` is the skeleton the hand-written sources each used to spell out (cache read, fetch, a
failure is ``[]`` and is NOT cached, cache write): read off the loop, fetch, write off the loop.

``aparse`` is the ONE place that decides where parsing an answer runs: here on the loop when it is
short (the answer's size times the parser's measured rate), in the process lane when it is long
(the parse split). The scrape template base
(``scrape._base.BaseScrapeAdapter``) calls it at its parse step with the bytes its fetch step read
(``http.count_bytes``); a hand-written source calls it where it parses.

This module's filename does not end in ``_source``, so the server's auto-discovery skips it.
"""

from __future__ import annotations

import functools
import time
import types
from typing import Any, Awaitable, Callable, Optional

import anyio

from omniseek.core import cache, portal
from omniseek.core.normalize import Document

# PARSE HERE OR IN THE PROCESS LANE (2026-11-04).
# Parsing an answer is pure CPU: on the loop it holds every other request for as long as it runs; in
# the process lane it costs a pickle of the input and of the result on the loop (about 1 ms per
# 100 KB) and a wait for a free worker of the two. So a parse that would take long is sent there and a
# short one stays here.
# PARSE_INLINE_BUDGET_MS: one parse left on the loop may take at most this long, a fifth of the 100 ms
# probe line, so a few sources parsing at once still stay under it (PROVISIONAL; the live probe after
# the deploy confirms it or replaces it).
# How long a parse WOULD take is its answer's size times that parser's milliseconds per MB of answer.
# Parsers differ six-hundred-fold (a replay tape, results/parse_rates.json:
# json.loads 1.3 to 3.5 ms/MB, cset's markdownify 40 to 60, a podcast feed 3 to 5, csrankings' csv
# 10, bs4 pages 5 to 264, the iacr listing regex 195, yaml 966), so no one byte threshold fits them all
# (200 KB of yaml is 190 ms). Each parser's rate is therefore MEASURED as it runs (the last reading,
# here or in the worker, per parser and per process); a parser not seen yet in this process gets
# PARSE_DEFAULT_MS_PER_MB, the reading behind the first cut (cset and
# overseas_ai_jobs parsed 1 to 2.4 MB answers in 103 to 148 ms on the loop, about 100 ms/MB), so its
# first answer goes to the lane above 200 KB.
PARSE_INLINE_BUDGET_MS = 20.0
PARSE_DEFAULT_MS_PER_MB = 100.0
# parser key -> its last measured ms per MB of answer. Read and written on the loop only.
_parse_ms_per_mb: dict[str, float] = {}


def _parser_key(fn: Callable[..., Any]) -> str:
    """A parser's name: its module and qualified name (a source's method: named by the source's own class,
    so two sources sharing an inherited ``_to_documents`` are two parsers); for a ``functools.partial``
    the function's and those of the callables it binds (a shared decode step bound to one parser is that
    parser)."""
    if isinstance(fn, functools.partial):
        return "+".join([_parser_key(fn.func)] + [_parser_key(a) for a in fn.args if callable(a)])
    owner = getattr(fn, "__self__", None)
    if owner is not None and not isinstance(owner, types.ModuleType):  # a method: of THIS source's class
        cls = owner if isinstance(owner, type) else type(owner)
        return f"{cls.__module__}.{cls.__qualname__}.{fn.__name__}"
    return f"{getattr(fn, '__module__', '')}.{getattr(fn, '__qualname__', type(fn).__name__)}"


def _timed(fn: Callable[..., Any], *args: Any) -> tuple[float, Any]:
    """Runs IN a worker of the process lane: ``fn(*args)`` and how many ms it took there."""
    t0 = time.perf_counter()
    out = fn(*args)
    return (time.perf_counter() - t0) * 1000.0, out


async def aparse(size: int, fn: Callable[..., Any], *args: Any, lane: Optional[Callable[..., Any]] = None) -> Any:
    """``fn(*args)``, the parse of an answer of ``size`` bytes (a str's length is close enough): here on
    the loop when this parser's measured rate says it takes at most ``PARSE_INLINE_BUDGET_MS``, else in
    a worker of the process lane (``portal.run_cpu``; ``fn`` module-level, ``args`` plain data). Either
    way the time it took updates the parser's rate. ``lane``: what the worker runs instead when ``fn``
    is a bound method (a module-level function that finds the source by name, e.g. a
    ``functools.partial``), called with the same ``args``. Returns the result or re-raises the parse's
    exception."""
    key = _parser_key(fn)
    mb = size / 1e6
    if mb * _parse_ms_per_mb.get(key, PARSE_DEFAULT_MS_PER_MB) > PARSE_INLINE_BUDGET_MS:
        ms, out = await portal.run_cpu(_timed, lane or fn, *args)
    else:
        t0 = time.perf_counter()
        out = fn(*args)
        ms = (time.perf_counter() - t0) * 1000.0
    if size > 0:
        _parse_ms_per_mb[key] = ms / mb
    return out


class SourceBase:
    """Root of the source bases (and of a hand-written native source): ``search`` from ``asearch``."""

    def search(self, query: str, limit: int = 10) -> list[Document]:
        """The one sync entry: this source's ``asearch``, run through ``portal.call``. For scripts and
        threads only; the fan-out awaits ``asearch`` itself."""
        return portal.call(self.asearch, query, limit)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """The base's health check for a blocking source: a class that writes a sync ``def
        health_check`` (and no ``ahealth_check`` of its own) gets ``_athreaded_health`` as its
        ``ahealth_check``, so that sync body is its check even when a base above it (a template base,
        the CDP base) writes a native ``ahealth_check``: the nearest health check in the class tree
        always wins, sync or async, and none is dead code."""
        super().__init_subclass__(**kwargs)
        if "health_check" in cls.__dict__ and "ahealth_check" not in cls.__dict__:
            cls.ahealth_check = SourceBase._athreaded_health

    async def _athreaded_health(self) -> tuple[Optional[bool], str]:
        """A blocking source's sync ``health_check`` (a CDP page, a library client) on a thread of the
        legacy lane, the lane the fan-out gives its ``search``. Abandoned when its caller stops waiting
        (``abandon_on_cancel``: the thread finishes on its own and holds no lane token). Every other
        source writes ``async def ahealth_check`` itself and never defines ``health_check``."""
        from omniseek.core import fetcher  # local: the bases import this module before fetcher is wired
        return await anyio.to_thread.run_sync(self.health_check, limiter=fetcher._LEGACY_LIMITER,
                                              abandon_on_cancel=True)

    async def _acached(self, key: str, ttl: "Optional[float] | Callable[[list[Document]], float]",
                       aget: Callable[[], Awaitable[Optional[list[Document]]]], *,
                       empty_ttl: Optional[float] = None) -> list[Document]:
        """``key`` cached? return it. Else ``await aget()``: None is a failure (``[]``, not cached), a
        list (even an empty one) is cached for ``ttl`` seconds and returned (an empty one for
        ``empty_ttl`` when given, else under the cache's own floor for empties: ``cache.set_docs``).
        ``ttl`` may be a function of the documents (``lambda docs: 900 if docs else 300``)."""
        cached = await cache.aget_docs(key)                      # disk read OFF loop, one fetch per key
        if cached is not None:
            return cached
        docs = await aget()
        if docs is None:
            return []
        if callable(ttl):
            ttl = ttl(docs)
        await anyio.to_thread.run_sync(                          # disk write OFF loop
            functools.partial(cache.set_docs, key, docs, ttl=ttl, empty_ttl=empty_ttl), limiter=portal.IO_LANE)
        return docs


async def ablocking(fn: Callable[..., Any], *args: Any) -> Any:
    """``fn(*args)`` on a worker thread of the legacy limiter: a blocking hook of a source (it waits on
    the network AND parses), the same lane the fan-out gives a blocking source's whole ``search``."""
    from omniseek.core import fetcher  # local: the bases import this module before fetcher is wired
    return await anyio.to_thread.run_sync(functools.partial(fn, *args), limiter=fetcher._LEGACY_LIMITER)
