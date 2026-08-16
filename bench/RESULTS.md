# OmniSeek benchmark results

| Suite | n | rate [Wilson interval] | noise band | p50 ms | p90 ms | stale | dormant |
| --- | ---: | --- | ---: | ---: | ---: | ---: | --- |
| s3-crosslingual | 7 | 0.0000 [0.0000, 0.2153] | 0.0000 | n/a | n/a | 1 | no |
| s4-depth | 9 | 1.0000 [0.8241, 1.0000] | 0.0000 | 1147.57 | 11230.98 | 1 | no |
| s5-scholar | 11 | 1.0000 [0.8513, 1.0000] | 0.0000 | 3.58 | 753.52 | 0 | no |
| s6-memory | 5 | 1.0000 [0.7225, 1.0000] | 0.0000 | 4.98 | 9.73 | 0 | no |

## Environment

```json
{
  "extras_detected": {
    "asr": false,
    "ocr": true,
    "pdf": true,
    "recall": true,
    "walled": false
  },
  "omniseek_version": "0.2.0",
  "platform": "Linux-6.17.0-1022-azure-x86_64-with-glibc2.39",
  "python": "3.11.15",
  "utc": "2026-08-16T17:51:52.479837+00:00",
  "vantage": "github-actions",
  "warmup_ms": 13096.91,
  "warmup_pass_ms": [
    6132.729,
    6964.182
  ]
}
```

## Stale task ids

- `s3-xling-007` (dead)
- `s4-depth-011` (rate_limited)

## Dormant suites

- none

## Conflict of interest

Conflict-of-interest note, printed on the results page: this benchmark is written and run by OmniSeek's maintainers. Its queries were selected to demonstrate modality reach. If you can break it, or author tasks in this format that OmniSeek fails, we want to see them: open an issue with the task file.
