# D3 Findings: inability to observe versus negative fact

## Sweep scope and method

The sweep covered `src/`, `scripts/`, `bench/`, `site/`, `tests/`, and workflow files. It
searched health probes, optional dependency flags, broad `except Exception` fallbacks, empty
result returns, stale and denominator counters, and public renderers. The required boundary
paths were not searched for edits and were left unchanged.

The two task-confirmed instances were found exactly as described. The sweep also found a
cohort of health checks that reported local CDP or optional dependency absence as `False`.
Those are fixed below. Search-path empty-result cases with existing diagnostics were not
re-reported because the task explicitly marks that path as handled.

## Fixed candidates

1. `scripts/health_sweep.py:45`: `healthy is None` fell through to `down`. This reached the
   weekly source-health page. Severity: high. Fixed by mapping it to `skipped` while retaining
   the detail.
2. `src/omniseek/core/sources/scrape/pdf_source.py:104`: missing PyMuPDF returned `False`.
   The health page published a false PDF outage. Severity: high. Fixed by returning `None`.
3. `src/omniseek/core/sources/scrape/cninfo_source.py:117`: missing `curl_cffi` returned
   `False`. This reached the health page. Severity: high. Fixed by returning `None`.
4. `src/omniseek/core/sources/scrape/eastmoney_source.py:215`: missing `curl_cffi` returned
   `False`. This reached the health page. Severity: high. Fixed by returning `None`.
5. `src/omniseek/core/sources/scrape/gov_policy_source.py:116`: missing `curl_cffi`
   returned `False`. This reached the health page. Severity: high. Fixed by returning `None`.
6. `src/omniseek/core/sources/scrape/juejin_source.py:115`: missing `curl_cffi` returned
   `False`. This reached the health page. Severity: high. Fixed by returning `None`.
7. `src/omniseek/core/sources/scrape/sogou_weixin_source.py:203`: missing `bs4` or
   `curl_cffi` returned `False`. This reached the health page. Severity: high. Fixed by
   returning `None`.
8. `src/omniseek/core/sources/walled/xiaohongshu_cn_source.py:1233`: both optional browser
   and signed API paths being unavailable returned `False`. This reached the health page when
   the source was probed. Severity: high. Fixed by returning `None`.
9. `src/omniseek/core/sources/walled/_base.py:375`: shared CDP unavailability returned
   `False` for every regular CDP adapter. This reached the health page as a source outage.
   Severity: high. Fixed by returning `None`.
10. `src/omniseek/core/sources/walled/cdp_fulltext_source.py:132`: local CDP unavailability
    returned `False`. Public health reachability: yes. Severity: high. Fixed by returning
    `None`.
11. `src/omniseek/core/sources/walled/douban_groups_source.py:200`: local CDP unavailability
    returned `False`. Public health reachability: yes. Severity: high. Fixed by returning
    `None`.
12. `src/omniseek/core/sources/walled/douyin_source.py:237`: the local 9225 CDP process being
    down returned `False`. Public health reachability: yes. Severity: high. Fixed by returning
    `None`.
13. `src/omniseek/core/sources/walled/yipinsanfendi_source.py:254`: local CDP unavailability
    returned `False`. Public health reachability: yes. Severity: high. Fixed by returning
    `None`.
14. `src/omniseek/core/sources/walled/xiaohongshu_source.py:966`: local CDP unavailability
    returned `False`. Public health reachability: yes. Severity: high. Fixed by returning
    `None`.
15. `src/omniseek/core/sources/walled/zhihu_source.py:162`: local CDP unavailability returned
    `False`. Public health reachability: yes. Fixed by returning `None`.
16. `src/omniseek/core/sources/walled/zhihu_users_source.py:210`: local CDP unavailability
    returned `False`. Public health reachability: yes. Fixed by returning `None`.
17. `src/omniseek/core/sources/scrape/xiaomuchong_source.py:165`: local CDP unavailability
    returned `False`. Public health reachability: yes. Fixed by returning `None`.
18. `src/omniseek/core/sources/api/ircc_processing_times_source.py:232,234`: local CDP
    import or connectivity failure returned `False` before reading the cache. Public health
    reachability: yes. Severity: high. Fixed by returning `None`.
19. `src/omniseek/core/sources/api/ircc_ee_rounds_source.py:96,98`: local CDP import or
    connectivity failure returned `False` before reading the cache. Public health reachability:
    yes. Severity: high. Fixed by returning `None`.
20. `bench/run.py:400-412`: HTTP 401 and 403, plus local non-timeout exceptions, were collapsed
    into `dead`. The result page then removed those tasks from the benchmark denominator. Severity:
    critical. Fixed with `blocked` and `probe_error`, and only observed `dead` remains stale.

## Ambiguous candidates left for driver adjudication

1. `src/omniseek/core/sources/api/_base.py:220-224`: a missing `health_probe_url` and a
   shared HTTP failure both return `False`. The first is local adapter configuration, while
   the second may be a remote failure. Public health reachability: yes. Severity: medium.
   Recommendation: make the no-URL branch `None`, then decide whether the shared HTTP helper
   should expose transport failure separately from an HTTP negative response.
2. `src/omniseek/core/sources/api/_search_backend.py:114,151,223,255`: web backend exceptions
   fall back to another backend or ultimately return `[]`. This is internal fallback plumbing,
   and the normal search path has diagnostics, but the final empty result can still be read as
   no matches by a caller that ignores metadata. Public reachability: indirect. Severity:
   medium. Recommendation: preserve the existing fallback behavior but require a typed
   backend-outcome record at the final boundary.
3. `src/omniseek/core/sources/scrape/xiaoyuzhou_source.py:105,131`: fetch exceptions return
   `[]` from search while the separate health probe reports `False`. The source logs the
   failure, but this candidate needs a check against the fetcher's current metadata contract
   before changing its public empty-list behavior. Public reachability: yes. Severity: medium.
   Recommendation: carry a source error outcome alongside `[]`, without changing the
   documented document-list return type.
4. `src/omniseek/core/sources/api/nowcoder_source.py:186-190`: a local CDP failure plus a
   failed Brave fallback becomes `False`. The combined failure may represent both local and
   remote inability, so changing it to `None` without a composite outcome would hide a real
   fallback outage. Public reachability: yes. Severity: medium. Recommendation: split the
   two component outcomes before changing the final class.

## Explicitly excluded from re-reporting

- Search-path degradation in `meta["diagnostics"]`, as stated in the task.
- Missing benchmark extras handled by `REQUIRED_EXTRAS` and `dormant`.
- Curator `parked_p2` and `wall_probe` handling for plain-probe misses.

