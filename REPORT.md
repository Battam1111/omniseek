# Truthful status repair report

## Outcome

D1 through D5 are implemented. The public surfaces now distinguish a source or document that
was observed to fail from a source or document that this deployment could not exercise.

No new runtime dependency was added. `pyproject.toml`, `bench/tasks/`,
`bench/RECONSTRUCTION-s3.md`, and `docs/i18n/` were not changed.

## D1: source health

- `classify_probe(None, detail)` now returns `("skipped", detail)`.
- `pdf_source.health_check` returns `None` when PyMuPDF is absent and continues to return
  `False` for actual PDF probe failures.
- The same optional-capability correction was applied to `cninfo`, `eastmoney`, `gov_policy`,
  `juejin`, `sogou_weixin`, and the no-browser/no-signed-path case in `xiaohongshu_cn`.
- D3 also found local CDP inability reported as `False`. The shared CDP base and the direct CDP
  health checks listed in `FINDINGS.md` now return `None` for that local capability gap.
- The health summary now carries `skipped_policy`, `skipped_capability`, and `skipped_budget`.
  The published page renders one line:

  `Skipped breakdown: policy=N | capability absent=N | sweep budget=N`

This keeps explicit-only and credential policy skips visibly separate from a missing capability
in the deployment.

## D2: benchmark liveness

The probe vocabulary is now:

- `alive` for 2xx and 3xx responses
- `blocked` for 401 and 403
- `rate_limited` for 429
- `timeout` for HTTP 408 and local timeout exceptions
- `probe_error` for other local exceptions
- `dead` for other non-2xx/3xx HTTP responses
- `self_contained` for tasks without a liveness URL

HTTP responses retain `status_code`. Stale entries publish that code, for example
`dead; HTTP 404`.

Denominator decision: only `dead` enters `stale_ids` and is removed from the scored denominator.
`blocked`, `probe_error`, `rate_limited`, and `timeout` remain scored because none of them proves
that the upstream document is stale. The runner records those outcomes in
`probe_observations`, and both `bench/gen_report.py` and `site/bench.html` render them in a
section explicitly separate from stale tasks. This prevents a local egress condition from both
making a false claim and shrinking the benchmark denominator.

The original manual-dispatch premise is confirmed: the live registry search returned
`io.github.Battam1111/omniseek` version `0.2.0`, which is the current committed `server.json`
version.

## D3: repository findings

`FINDINGS.md` contains the full ledger. It records 20 fixed candidates and 4 ambiguous candidates
left for driver adjudication. The fixed set consists of:

- the two task-confirmed root instances;
- seven optional dependency or capability-absence health checks;
- eleven local CDP availability checks.

The ambiguous set is intentionally short: the default API probe with no configured URL, web
fallback functions whose final empty result follows an existing fallback chain, the
`xiaoyuzhou` empty-list error contract, and `nowcoder`'s composite CDP plus Brave fallback.

Already-handled paths were not re-reported: search diagnostics, benchmark dormant extras, and
curator `parked_p2` / `wall_probe`.

## D4: three gates and bite proofs

The smoke gates use synthetic in-memory data and never hit the network. The following are the
verbatim relevant lines from full `python tests/smoke.py` runs.

### Gate 1: dependency absence cannot publish `down`

Green:

```text
  ok   D4 gate 1: dependency absence never publishes a down health row
SMOKE OK
```

故意回退为 `down`:

```text
  FAIL D4 gate 1: dependency absence never publishes a down health row: status=down detail=PyMuPDF missing: No module named 'fitz'
SMOKE FAILED: 2 problem(s)
```

恢复:

```text
  ok   D4 gate 1: dependency absence never publishes a down health row
SMOKE OK
```

### Gate 2: `classify_probe(None, ...)` is `skipped`

Green:

```text
  ok   D4 gate 2: classify_probe(None, ...) returns skipped
SMOKE OK
```

故意回退为 `down`:

```text
  FAIL D4 gate 2: classify_probe(None, ...) returns skipped
SMOKE FAILED: 2 problem(s)
```

恢复:

```text
  ok   D4 gate 2: classify_probe(None, ...) returns skipped
SMOKE OK
```

### Gate 3: HTTP 403 is `blocked`, not stale

Green:

```text
  ok   D4 gate 3: HTTP 403 is blocked and does not enter stale denominator
SMOKE OK
```

故意回退为 `dead`:

```text
  FAIL D4 gate 3: HTTP 403 is blocked and does not enter stale denominator: probe={'alive': False, 'status_code': 403, 'url': 'https://example.invalid/probe', 'class': 'dead'}
SMOKE FAILED: 1 problem(s)
```

恢复:

```text
  ok   D4 gate 3: HTTP 403 is blocked and does not enter stale denominator
SMOKE OK
```

## D5: workflow and provenance

- The registry job queries the public registry for the exact server name and version. A matching
  existing version emits a no-op message and skips install, OIDC login, and publish. A registry
  query failure remains fatal because the `curl --fail-with-body` command is not swallowed.
- The multi-platform image labels now include
  `org.opencontainers.image.revision=${{ github.sha }}`.

## Verification

The following checks were run offline or against synthetic data:

- `python tests/smoke.py`: green, including all three D4 gates and the repository unittest battery.
- `python scripts/brand_lint.py`: `brand_lint: OK (no em-dash on any human-facing surface)`.
- `python scripts/health_sweep.py --help`: imports and parses arguments.
- `python bench/run.py --help`: imports and parses arguments.
- `python scripts/test_health_tools.py -v`: 10 tests passed.
- `PYTHONPATH=src python bench/test_bench.py -v`: 48 tests passed.
- `python -m unittest tests.test_truthful_status -v`: 2 tests passed.

The first local smoke attempt exposed a Windows console encoding issue in the existing test
output: the cp1252 stream could not print a Chinese test name. `tests/smoke.py` now reconfigures
its own stdout and stderr to UTF-8 with replacement, without changing any tested behavior.
