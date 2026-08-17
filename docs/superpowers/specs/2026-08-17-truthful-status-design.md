# Truthful Status Design

## Goal

Stop publishing an inability to observe a source or document as a negative fact about that
source or document. The change covers the source-health sweep, the benchmark liveness probe,
the two public renderers, repository-wide candidates, smoke gates, and the image publication
workflow.

## Architecture

The health sweep keeps its existing five-status vocabulary. A `None` health result becomes
`skipped`, and skipped rows are categorized by detail as policy, capability absent, or budget.
Known optional dependency failures return `None` at the adapter boundary.

The benchmark liveness probe separates `alive`, `dead`, `blocked`, `probe_error`, `rate_limited`,
`timeout`, and `self_contained`. Only `dead` enters the stale denominator. Other non-live probe
classes remain scored and are published in a separate probe-observations section, with HTTP
status codes included where available.

## Components and data flow

- `scripts/health_sweep.py` classifies tri-state adapter results and emits skipped breakdown
  counts in the summary.
- Optional-dependency health checks return `(None, detail)` while real source failures continue
  to return `(False, detail)`.
- `scripts/gen_health_page.py` validates and renders the skipped breakdown on one line.
- `bench/run.py` classifies HTTP and local liveness outcomes, retains only `dead` as stale, and
  emits separate non-stale probe observations.
- `bench/gen_report.py` and `site/bench.html` render stale status codes and non-stale probe
  observations.
- `tests/smoke.py` contains three synthetic offline gates for the health and benchmark contracts.
- `.github/workflows/publish-image.yml` treats an existing registry version as a no-op.
- `Dockerfile` receives the built commit SHA through a build argument and applies it as the OCI
  revision label.

## Error handling

An HTTP 401 or 403 is `blocked` because the host answered and refused this prober. A timeout is
`timeout`. Any other local exception is `probe_error`. An HTTP response outside the 2xx/3xx
range is `dead` unless it has one of the explicit classes above. Only `dead` is evidence that
the task itself is stale.

## Testing and proof

Tests are written before production changes. Targeted unit tests establish the contracts first,
then `python tests/smoke.py` proves the three required gates. Each gate is run green, the
corresponding defect is deliberately restored and run red, then the fix is restored and run
green again. The exact outputs are copied into `REPORT.md`.
