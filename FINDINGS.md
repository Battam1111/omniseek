# D1 adapter findings

## Sweep method

The sweep covered every Python file below `src/omniseek/core/sources/`. I used an AST pass to
find exception handlers that return an empty list, then read the surrounding adapter flow. The
pass found 81 empty-list exception handlers. Most are not the defect in this task:

- date, number, JSON-shape, and per-record parsing helpers return an empty value for one malformed
  field or record, not for a failed adapter fetch;
- fan-out helpers return an empty child result so their parent can keep results from other feeds,
  channels, or ATS sites;
- several fetch helpers already call `diag.note`, so a named fetch produces the existing
  diagnostic even when the document list is empty.

The rule used for the final judgment was: a single failed source call must not return a bare `[]`;
an intentional query miss and a partial fan-out may remain empty only when the existing outcome or
diagnostic channel records why.

## Fixed

### `api/_search_backend.py`

The DDG sync and async leaves now raise `RuntimeError` for transport errors, non-200/non-202
responses, and a final three-attempt 202 rate-limit. A 200 response with zero parsed rows remains
a genuine query miss. Brave still falls back to DDG; if DDG also fails, the exception reaches the
adapter and then the fetcher.

This backend call has no partial result of its own. It either returns the engine response or fails,
so raising is the correct behavior.

### `scrape/xiaoyuzhou_source.py`

The sync and async per-podcast fetch helpers now raise on transport errors, HTTP errors, or a
shared async HTTP helper that returned no body. The parent search methods collect each podcast
independently:

- one or more successful podcasts plus a failed podcast returns the successful documents and emits
  `diag.note("xiaoyuzhou.partial", ...)`;
- if every podcast fetch fails, the first failure is raised and the fetcher records an errored
  outcome;
- an ordinary successful page with zero episodes remains an ordinary empty result.

This adapter can return partial results, so it records the failure when partial documents survive
and raises only for the total-failure case.

### End to end proof

The real `_SearchVenue` adapter was run through `fetcher.fetch_outcome` with its imported
`search_web` call replaced by a deterministic offline exception. No network was used:

```text
state=errored
reason=RuntimeError: DDG request failed: offline
captures=[{'helper': '_report_search_backend_2.search', 'exc': 'RuntimeError: DDG request failed: offline'}]
```

Before the D1 change, the same adapter path returned `state=completed` with an empty document
list because the backend swallowed the exception.

## Deliberately not mass-edited

These are the remaining review candidates from the sweep. They are recorded here instead of being
changed by a blanket replacement.

- `api/bluesky_source.py:search`, `api/core_source.py:_raw_fetch` and `_araw_fetch`,
  `api/ircc_ee_rounds_source.py:_rounds`, `api/nserc_awards_source.py` fetch helpers,
  `api/openalex_source.py:search` and `asearch`, and `api/wayback_source.py:search` and
  `asearch`: each is effectively a single upstream or a single aggregate response. Partial
  results are not available at the caught boundary. Recommendation: make the upstream failure
  raise, or add an explicit diagnostic if an existing shared helper intentionally converts it to
  an observable negative.
- `scrape/_base.py:search` and `_asearch_via`: this is a shared template used by many adapters.
  Some callers pass through shared HTTP diagnostics and some have parsing-only fallbacks. Partial
  results depend on the concrete subclass. Recommendation: adjudicate the template and its
  callers together before changing its contract.
- `api/cihr_grants_source.py`, `api/cordis_eu_source.py`, `api/crossref_source.py`,
  `api/csrankings_source.py`, `api/dblp_source.py`, `api/gpu_pricing_source.py`,
  `api/llm_leaderboard_source.py`, `api/mycareersfuture_source.py`,
  `api/openrouter_rankings_source.py`, `api/sshrc_awards_source.py`, and
  `scrape/ajo_source.py`, `scrape/conference_deadlines_source.py`,
  `scrape/cvf_openaccess_source.py`, `scrape/epoch_ai_models_source.py`,
  `scrape/levels_fyi_source.py`: their caught network or parse paths already emit `diag.note` in
  the relevant failure branches, or their parent combines multiple independent inputs. These
  can return partial results in several cases. Recommendation: keep the existing diagnostic
  channel and inspect each parent before deciding whether a total failure should raise.
- `scrape/ai_residencies_source.py`, `scrape/hk_universities_source.py`,
  `scrape/overseas_ai_jobs_source.py`, `scrape/youtube_channels_source.py`,
  `api/org_watch_source.py`, `api/reddit_source.py`, `scrape/bilibili_source.py`,
  `walled/discord_communities_source.py`, and `walled/zhihu_users_source.py`: these are
  multi-site, multi-channel, comment, or identity fan-outs. Partial results are available and
  should be retained. Recommendation: record the failed child with the existing diagnostic
  channel; do not turn a one-child failure into a total adapter failure.
- `walled/_base.py`, `walled/douban_groups_source.py`, `walled/xiaohongshu_source.py`, and
  `walled/xiaohongshu_cn_source.py`: these include login, browser, breaker, or wall states that
  are not all equivalent to an upstream fetch failure. Some branches already emit typed
  diagnostics. Recommendation: keep the current typed wall and account semantics and let the
  driver adjudicate any branch without a surviving record.
- `api/nowcoder_source.py:_fetch_job`: the empty return is part of a composite path. A local CDP
  failure is followed by a Brave fallback, and `health_check` reports a remote negative only when
  both paths fail. The task's premise is not sufficient to call this a silent total failure.
  Recommendation: no change in this round.

The candidates above are intentionally left for driver review. D1 fixed the two unambiguous
paths named by the task and did not replace partial-result or typed-wall behavior with a blanket
raise.
