# OmniSeek Bench: design charter

OmniSeek's README makes a small number of falsifiable claims: it transcribes speech nobody
wrote down, it reads text baked into pixels, it crosses languages, it reaches evidence buried
deep in threads and documents, it returns provenance you can re-check, and it remembers what
it has already seen. This benchmark exists to measure exactly those claims and nothing else.
Every design choice below follows from one rule: **the judge must test a claim we actually
made, on a substrate where the effect is measurable, with a metric whose dimensions match the
claim.**

## What this benchmark is not

- **Not a competitor leaderboard.** Our task suites are constructed, by design, around
  modalities that plain web search does not serve. Scoring third-party search APIs on tasks
  built to be adversarial to them would be the asymmetric-tool-grant pattern that (rightly)
  gets vendor benchmarks dismissed. The baseline is instead built into every task as recorded
  evidence: named engines, named dates, logged proof that the answer was not surfaced (see
  "search resistance" below). We publish our own hit rates against that recorded floor, and
  no one else's numbers.
- **Not an answer-accuracy benchmark.** OmniSeek never generates or judges; it retrieves
  evidence. So the unit of scoring is "the returned evidence contains the verifiable ground
  truth", never "the answer is correct". SimpleQA-style QA scores measure a claim this
  project does not make.
- **Not a walled-source benchmark.** The login-walled tier runs on the operator's own
  credentials. A public benchmark of it would be irreproducible without handing out accounts
  and would republish walled content as ground truth. Walled capability is documented and
  demonstrable, but it is deliberately outside these numbers. Absence here is a scope
  boundary, not an omission.

## Claim-to-suite map

| Suite | README claim under test | OmniSeek path | Required extras |
|---|---|---|---|
| S1 audio | transcribes speech nobody wrote down | `omniseek_transcribe` | `asr` |
| S2 pixels | reads text baked into images and scans | `omniseek_read(ocr=True)` | `ocr` |
| S3 cross-lingual | crosses languages | `omniseek_search` | core (`recall` strengthens) |
| S4 depth | evidence deep in threads and long documents | `omniseek_search` / `omniseek_read` | core (`pdf` for PDF tasks) |
| S5 scholar graph | structured scholarly evidence | `omniseek_coauthors` / `omniseek_paper_enrich` | core |
| S6 memory | remembers, deduplicates, traces provenance | `_meta` contract of repeat runs | core |

Suites declare their required extras, which makes the benchmark double as an honest
capability-tier map: a core-only install is expected to pass core suites and to report the
others as "sense dormant", not as failures.

## Task anatomy

One task is one JSON file, versioned in-repo, human-auditable:

```json
{
  "id": "s1-audio-007",
  "suite": "s1-audio",
  "claim": "the exact phrase is spoken in the episode and written nowhere indexable",
  "input": {"tool": "omniseek_transcribe", "args": {"url": "...", "start": "46:30", "duration": "3:00"}},
  "ground_truth": {"type": "normalized_containment", "value": "...", "normalize": "casefold_strip_cjk"},
  "search_resistance": [
    {"engine": "google", "query": "...", "date": "2026-08-16", "first_page_hit": false},
    {"engine": "bing", "query": "...", "date": "2026-08-16", "first_page_hit": false}
  ],
  "liveness_probe": {"url": "...", "expect": "http_200"},
  "added_in": "bench-v1.0"
}
```

- **Ground truth is mechanically verifiable by a human without any model**: a phrase you can
  hear at a timestamp, a number you can see in a figure, a DOI you can click. If verifying a
  task requires trusting an LLM, the task is rejected.
- **Search resistance is recorded evidence, not an assertion.** Following the BrowseComp
  construction discipline: a task is admitted only after logged queries on named engines, on
  a named date, failed to surface the ground truth on the first page. Tasks that fail this
  gate are discarded, however good they look.
- **Dynamic ground truth (S5 only):** scholarly counts drift, so S5 tasks re-fetch the truth
  from the upstream source of record at judge time and compare structurally (the
  MCP-Universe dynamic-evaluator pattern), rather than freezing a number that will rot.
- `liveness_probe` is optional. An absent or null probe means the task is self-contained
  and always live; tasks that depend on an external source may provide a URL probe.

## Judges

All judges are mechanical: normalized string containment, numeric equality, identifier
match in top-k (k pre-registered per suite, default 5), or field assertions on `_meta`.
There is no LLM judge anywhere in the harness. Where ASR output varies, the fallback judge
is a character-similarity ratio against the target span with a pre-registered threshold,
which is still a pure computation. Judge code lives beside the tasks and is part of the
versioned benchmark: changing a judge is a benchmark version bump, never a silent edit.

## Statistics

Live sources are noisy, so single runs are noise:

- Every published run executes each task **N=3 times**; a task scores by majority.
- Every published run is **two identical back-to-back passes**. The between-pass difference
  is published as the run's noise band. A change smaller than the band is reported as "within
  noise", never as movement.
- Per-suite success rates carry **Wilson 95% intervals**. Suite sizes are small and the
  intervals are wide; we print them anyway, wide intervals included, because a point estimate
  without an interval is how retrieval benchmarks lie.
- **There is no aggregate single score.** A weighted average across modalities would be a
  tunable dial; per-suite tables are the honest unit.
- Latency is reported per suite as median and p90 of successful calls, labeled with the
  vantage point (CI runner or maintainer instance).

## Rot policy

- Before scoring, every present `liveness_probe` runs. A 2xx or 3xx response is alive;
  an absent or null probe is self-contained and always live. A non-live upstream marks
  the task **stale**: excluded from the denominator, counted, and listed with a class such
  as `dead`, `timeout`, or `rate_limited` in the run report. Silent shrinkage of the
  denominator is forbidden; the stale list is part of the results.
- The task set is versioned (`bench-v1.0`, `v1.1`, ...). New tasks enter with the wave that
  authored them; stale tasks are retired by id in the changelog, never edited in place.
- Tasks are public plaintext by choice. This benchmark measures retrieval reach, not model
  knowledge, so training-set contamination does not let anyone cheat it; reproducibility is
  worth more here than secrecy. The real overfitting risk is us tuning adapters to our own
  tasks, which is exactly why the construction standard and the resistance-evidence format
  are published: anyone can author counter-tasks in the same format and hold us to them.

## Publishing

- Results are dated JSON artifacts (environment, versions, git SHA, per-task outcomes,
  stale list, noise band) plus a rendered page **generated from the JSON by script**. No
  number on any page is typed by hand; number drift across pages is an unforced error this
  design makes impossible.
- The harness runs from a plain checkout: install, run one command, get the same artifact.
  It is not part of the shipped wheel; it lives in the repo.
- Conflict-of-interest note, printed on the results page: this benchmark is written and run
  by OmniSeek's maintainers. Its queries were selected to demonstrate modality reach. If you
  can break it, or author tasks in this format that OmniSeek fails, we want to see them:
  open an issue with the task file.

## Relation to the source-health page

The benchmark answers "can the claims be demonstrated on demand". The weekly source-health
sweep answers "is the catalog alive this week". They share the vantage-point disclosure and
the generated-not-hand-written rule, but they are separate artifacts: a benchmark run failing
because one upstream was down that hour is a health fact, not a capability fact, and the
stale/skip taxonomy exists to keep those two ledgers from contaminating each other.
