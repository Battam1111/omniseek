# Honest empty result repair report

## Outcome

D1 through D3 are implemented in the working tree. The changes do not add runtime dependencies,
do not touch the protected task, documentation, or dependency paths, and do not create a process
directory.

## D1: total adapter failures surface

The existing fetcher contract was read before changing adapters. The relevant contract in
`src/omniseek/core/fetcher.py` is quoted here:

> The first-class OUTCOME of one bounded source fetch: what HAPPENED, not just what came back.
> Makes the three conflations the old docs-only return hid impossible to miss: a timeout, an
> adapter error, and a full page (possibly truncated) are all distinct states here.
>
> `state` is `"completed"` when the adapter returned, `"timed_out"` when it exceeded
> `deadline_s`, or `"errored"` when the adapter raised; the adapter exception is NOT re-raised by
> `fetch_outcome`.
>
> This function itself never raises for an ADAPTER fault: that becomes
> `state="errored"` with the reason and captures attached.

The fixed paths are:

- `_search_backend.py`: DDG sync and async total failures now raise after Brave fallback has
  failed. A valid HTTP 200 response with zero rows remains a real query miss.
- `xiaoyuzhou_source.py`: successful podcasts are retained and a failed sibling is recorded as a
  partial diagnostic. If all podcast fetches fail, the first error reaches the fetcher.

The deterministic end to end demonstration used a real `_SearchVenue` adapter and replaced its
search backend with an offline exception. It did not hit the network:

```text
state=errored
reason=RuntimeError: DDG request failed: offline
captures=[{'helper': '_report_search_backend_2.search', 'exc': 'RuntimeError: DDG request failed: offline'}]
```

The complete sweep ledger and the intentionally ambiguous candidates are in
[`FINDINGS.md`](FINDINGS.md).

## D2: no probe URL is our configuration gap

`BaseAPIAdapter.health_check()` now returns:

```text
(None, "our adapter configuration is missing health_probe_url")
```

The shared HTTP failure branch remains unchanged and still reports an observed request failure
as unhealthy.

## D3: health pages publish `blocked`

The health status vocabulary now includes `blocked` in both `scripts/health_sweep.py` and
`scripts/gen_health_page.py`.

- HTTP 401 and 403 details classify as `blocked`.
- `blocked` has its own summary count and is not included in `Down`.
- The page headline renders `Blocked: N`.
- The page explains: `Blocked means the source answered but refused this vantage or credential; it
  is not counted as Down.`
- The validator and summary builder enumerate the same status tuple.
