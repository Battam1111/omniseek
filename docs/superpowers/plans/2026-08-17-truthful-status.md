# Truthful Status Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make all public health and benchmark status claims distinguish observed failures from local inability to test.

**Architecture:** Preserve the existing health and benchmark status surfaces while adding explicit neutral classes and visible disclosure buckets. The health sweep maps `None` to `skipped`; the benchmark keeps only HTTP `dead` tasks stale.

**Tech Stack:** Python 3.11+, standard library, existing httpx test doubles, Markdown and static JavaScript renderers, GitHub Actions, Docker Buildx.

## Global Constraints

- No new runtime dependencies.
- Do not touch `bench/tasks/`, `bench/RECONSTRUCTION-s3.md`, or anything under `docs/i18n/`.
- No em-dash and no Chinese full-width dash in any changed file.
- Create virtualenvs outside the worktree.
- Preserve existing public claim wording except for the D1.3 and D2.4 disclosure additions.

---

### Task 1: D1 health tri-state and dependency neutrality

**Files:**
- Modify: `scripts/health_sweep.py`
- Modify: `src/omniseek/core/sources/scrape/pdf_source.py`
- Modify: `src/omniseek/core/sources/scrape/cninfo_source.py`
- Modify: `src/omniseek/core/sources/scrape/eastmoney_source.py`
- Modify: `src/omniseek/core/sources/scrape/gov_policy_source.py`
- Modify: `src/omniseek/core/sources/scrape/juejin_source.py`
- Modify: `src/omniseek/core/sources/scrape/sogou_weixin_source.py`
- Modify: `src/omniseek/core/sources/walled/xiaohongshu_cn_source.py`
- Modify: `scripts/gen_health_page.py`
- Test: `scripts/test_health_tools.py`

**Interfaces:**
- `classify_probe(healthy: Optional[bool], message: object) -> tuple[str, str]` returns
  `("skipped", detail)` for `healthy is None`.
- `build_summary(rows)` returns existing status counts plus `skipped_policy`,
  `skipped_capability`, and `skipped_budget`.
- Optional dependency absence is represented by `health_check() -> (None, detail)`.

- [ ] Write tests for `classify_probe(None, ...)`, dependency absence, and skipped summary/page
  breakdown.
- [ ] Run `python -m unittest scripts.test_health_tools -v` and observe the expected failures.
- [ ] Implement the tri-state branch, neutral dependency returns, and one-line page breakdown.
- [ ] Run the targeted tests, then the complete health-tool test module.
- [ ] Commit the D1 changes.

### Task 2: D2 benchmark liveness vocabulary and denominator

**Files:**
- Modify: `bench/run.py`
- Modify: `bench/gen_report.py`
- Modify: `site/bench.html`
- Test: `bench/test_bench.py`

**Interfaces:**
- `_liveness_probe` returns `blocked` for 401/403 and `probe_error` for non-timeout local
  exceptions, retaining `status_code` on HTTP responses.
- `_stale_entries` emits `id`, `class`, and optional `status_code`.
- `run_benchmark` emits `stale` for `dead` tasks and `probe_observations` for non-stale
  non-live classes.

- [ ] Write failing tests for 403 classification, non-stale denominator behavior, status-code
  disclosure, and HTML/Markdown rendering.
- [ ] Run `python -m unittest bench.test_bench -v` and confirm the new tests fail.
- [ ] Implement the class mapping, stale selection, observation output, and both renderers.
- [ ] Run all benchmark unit tests and inspect generated fixture pages.
- [ ] Commit the D2 changes.

### Task 3: D3 repository sweep and findings

**Files:**
- Create: `FINDINGS.md`
- Modify: each source file with an unambiguous optional-dependency health conflation found by
  the repository sweep.

**Interfaces:**
- `FINDINGS.md` has one entry per candidate with file, line, exact conflation, public-surface
  reachability, severity, and fixed or adjudication status.

- [ ] Sweep health checks, exception fallbacks, empty-result contracts, and denominator updates
  with `rg` and targeted source inspection.
- [ ] Fix only the unambiguous optional dependency health cases.
- [ ] Run the repository tests affected by each fixed case.
- [ ] Write the short findings ledger, including ambiguous candidates left for adjudication.
- [ ] Commit D3 findings and fixes.

### Task 4: D4 smoke gates and proof capture

**Files:**
- Modify: `tests/smoke.py`

**Interfaces:**
- Gate 1 rejects synthetic health rows with dependency-absence detail when status is `down`.
- Gate 2 asserts `classify_probe(None, ...)` is `skipped`.
- Gate 3 asserts synthetic 403 liveness is `blocked` and absent from stale ids.

- [ ] Add the three offline gates after their imports and helpers are available.
- [ ] Run `python tests/smoke.py` green.
- [ ] Deliberately restore each corresponding defect one at a time and run the same command to
  capture a red output.
- [ ] Restore the implementation and run the same command green for each gate.
- [ ] Commit the smoke gates.

### Task 5: D5 workflow and OCI provenance

**Files:**
- Modify: `.github/workflows/publish-image.yml`
- Modify: `Dockerfile`

**Interfaces:**
- The registry publish step checks the existing version and exits zero with a no-op message when
  the version is already present.
- The image build passes `${{ github.sha }}` as `OCI_REVISION` and labels
  `org.opencontainers.image.revision=$OCI_REVISION`.

- [ ] Add workflow text tests or static assertions for the no-op guard and revision wiring.
- [ ] Implement the smallest workflow and Dockerfile changes.
- [ ] Run YAML/text validation available in the repository and inspect the diff.
- [ ] Commit the D5 changes.

### Task 6: Reports and final verification

**Files:**
- Create: `REPORT.md`
- Modify: `FINDINGS.md` if verification changes a finding.

- [ ] Run `python tests/smoke.py`.
- [ ] Run `python scripts/brand_lint.py`.
- [ ] Run `python scripts/health_sweep.py --help` and `python bench/run.py --help`.
- [ ] Re-run targeted unit tests and any repository test suites required by the changed files.
- [ ] Paste the three exact D4 green-red-green outputs into `REPORT.md`.
- [ ] Record D2.3 denominator reasoning, D3 summary, and any contradicted premise.
- [ ] Check hard-boundary paths, Unicode dash policy, and `git diff --check`.
- [ ] Commit the reports and final documentation.
