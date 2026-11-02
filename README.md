# OmniSeek source health

Up: 66 | Degraded: 7 | Rate limited: 0 | Blocked: 2 | Down: 2 | Skipped: 143 | Total: 220
Blocked means the source answered but refused this vantage or credential; it is not counted as Down.
Skipped breakdown: policy=136 | capability absent=7 | sweep budget=0

Generated UTC: 2026-11-02T11:24:58Z
Vantage: github-actions
OmniSeek version: 0.2.1
Sweep duration: 34.111 seconds

Checked from GitHub Actions runners; a residential or maintainer deployment typically reaches more. One probe per source per run; this is a health signal, not an availability guarantee.

## bookmarks

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| cubox | keyed | skipped | n/a | requires operator credentials |
| github_starred | keyed | skipped | n/a | requires operator credentials |

## books

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| books_openlibrary_ia | free | up | 7956 ms |  |
| gutenberg | free | skipped | n/a | explicit-only |

## career

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| linkedin_posts | free | skipped | n/a | explicit-only |
| nature_careers | free | up | 4490 ms |  |
| nowcoder | free | skipped | n/a | explicit-only |

## clinical

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| clinicaltrials | free | skipped | n/a | explicit-only |

## code

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| context7 | free | skipped | n/a | explicit-only |
| github | keyed | skipped | n/a | requires operator credentials |
| github_releases | free | up | 23994 ms |  |
| github_trending | free | up | 547 ms |  |
| pypi | free | up | 3333 ms |  |
| rl_llm_frameworks | free | up | 16359 ms |  |

## community

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| academia_se | free | up | 370 ms |  |
| ai_se | free | up | 371 ms |  |
| crossvalidated | free | up | 367 ms |  |
| cs_se | free | up | 185 ms |  |
| datascience_se | free | up | 36 ms |  |
| discord_communities | walled | skipped | n/a | requires operator credentials |
| discourse_forums | free | up | 6200 ms |  |
| douban_groups | walled | skipped | n/a | explicit-only |
| gter | free | skipped | n/a | explicit-only |
| hackernews | free | up | 635 ms |  |
| hardwarezone | free | skipped | n/a | explicit-only |
| juejin | free | skipped | n/a | explicit-only |
| lobsters | free | up | 8274 ms |  |
| quora | free | skipped | n/a | explicit-only |
| reddit | free | skipped | n/a | not verified: rate-limited (Arctic Shift: HTTP 422, its "Timeout. Maybe slow down a bit" refusal; the far end's load-based rate limit refused the probe; not retried) |
| sogou_weixin | free | skipped | n/a | explicit-only |
| stackoverflow | free | up | 119 ms |  |
| tieba | free | up | 6536 ms |  |
| v2ex | free | up | 1234 ms |  |
| xiaohongshu | walled | skipped | n/a | explicit-only |
| xiaohongshu_search | free | skipped | n/a | explicit-only |
| xiaomuchong | free | skipped | n/a | explicit-only |
| yipin_search | free | skipped | n/a | explicit-only |
| yipinsanfendi | walled | skipped | n/a | explicit-only |
| zhihu | walled | skipped | n/a | explicit-only |
| zhihu_search | free | skipped | n/a | explicit-only |
| zhihu_users | walled | skipped | n/a | explicit-only |

## compensation

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| canada_jobbank_wages | free | skipped | n/a | explicit-only |
| levels_fyi | free | down | 1269 ms | role_ok=False comp_ok=False (page structure changed?) |
| ontario_sunshine | free | skipped | n/a | explicit-only |

## compute

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| gpu_pricing | free | skipped | n/a | explicit-only |
| vast_ai | free | skipped | n/a | explicit-only |

## data

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| eurostat_stats | free | skipped | n/a | explicit-only |
| gov_open_data | free | up | 14350 ms |  |
| statcan_wds | free | skipped | n/a | explicit-only |
| worldbank_stats | free | skipped | n/a | explicit-only |

## datasets

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| kaggle | free | skipped | n/a | explicit-only |

## deadlines

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| conference_deadlines | free | up | 20223 ms |  |
| ml_conferences | free | skipped | n/a | explicit-only |

## eval

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| llm_leaderboard | keyed | skipped | n/a | requires operator credentials |
| lmsys_arena | free | up | 5449 ms |  |
| ml_eval_safety | free | blocked | 4041 ms | all 2 feeds failed (epochai.substack.com: HTTP 403, metr.substack.com: HTTP 403) |
| scrape_ml_orgs | free | up | 15138 ms |  |

## filings

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| sec_edgar | free | up | 2008 ms |  |
| uk_companies_house | keyed | skipped | n/a | requires operator credentials |

## finance

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| cninfo | free | skipped | n/a | explicit-only |
| eastmoney | free | skipped | n/a | explicit-only |
| market_crypto | free | skipped | n/a | explicit-only |
| market_quote | free | skipped | n/a | explicit-only |
| sec_financials | free | skipped | n/a | explicit-only |

## funding

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| ai_residencies | free | skipped | n/a | timeout (>25s): health_check did not return |
| cihr_grants | free | skipped | n/a | explicit-only |
| cordis_eu | free | skipped | n/a | explicit-only |
| fellowships | free | skipped | n/a | timeout (>25s): health_check did not return |
| grants_gov | free | skipped | n/a | explicit-only |
| nih_reporter | free | skipped | n/a | explicit-only |
| nserc_awards | free | skipped | n/a | explicit-only |
| nsf_awards | free | skipped | n/a | explicit-only |
| nsfc_awards | free | skipped | n/a | explicit-only |
| sshrc_awards | free | skipped | n/a | explicit-only |
| ukri_gtr | free | skipped | n/a | explicit-only |

## general

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| cdp_fulltext | walled | skipped | n/a | explicit-only |
| csrankings | free | up | 846 ms |  |
| exa | keyed | skipped | n/a | requires operator credentials |
| wikicfp_nlp | free | skipped | n/a | explicit-only |
| xiaohongshu_cn | walled | skipped | n/a | explicit-only |

## immigration

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| aaip_draws | free | skipped | n/a | explicit-only |
| bcpnp_invitations | free | skipped | n/a | explicit-only |
| canada_immigration | free | degraded | 17191 ms | 3/4 feeds OK (degraded; dead: www.immigration.ca: HTTP 403) |
| datagovsg_nonresident_pass_types | free | skipped | n/a | explicit-only |
| ircc_ee_rounds | free | skipped | n/a | explicit-only |
| ircc_processing_times | free | skipped | n/a | explicit-only |
| mpnp_draws | free | skipped | n/a | explicit-only |
| oinp_invitations | free | skipped | n/a | explicit-only |
| page_watch | free | skipped | n/a | explicit-only |
| sg_immigration | free | skipped | n/a | explicit-only |

## insider

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| blind | free | skipped | n/a | explicit-only |
| glassdoor | free | skipped | n/a | explicit-only |
| maimai | free | skipped | n/a | explicit-only |

## jobs

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| academic_job_boards | free | skipped | n/a | timeout (>25s): health_check did not return |
| academic_jobs | free | up | 1611 ms |  |
| adzuna | keyed | skipped | n/a | requires operator credentials |
| ajo | free | up | 19993 ms |  |
| bytedance_seed | walled | skipped | n/a | explicit-only |
| feishu_jobs | walled | skipped | n/a | explicit-only |
| higheredjobs_cs | free | skipped | n/a | explicit-only |
| jobrxiv_canada | free | up | 11332 ms |  |
| layoffs_tracker | free | skipped | n/a | explicit-only |
| mycareersfuture | free | up | 3066 ms |  |
| overseas_ai_jobs | free | up | 18874 ms |  |
| remotive | free | skipped | n/a | explicit-only |
| vector_talent_hub | free | up | 4856 ms |  |

## media

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| chinese_ai_media | free | up | 2454 ms |  |
| kexue_fm | free | up | 7420 ms |  |
| substack_matrix | free | degraded | 13789 ms | 17/21 feeds OK (degraded; dead: benjamintodd.substack.com: HTTP 403, chinai.substack.com: HTTP 403, importai.substack.com: HTTP 403, thealgorithmicbridge.substack.com: HTTP 403) |
| wechat | walled | skipped | n/a | explicit-only |

## methodology

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| a_happy_phd | free | up | 1195 ms |  |
| github_awesome_phd | free | up | 714 ms |  |
| lesswrong | free | up | 9835 ms |  |
| ml_collective | free | up | 11518 ms |  |
| thesis_whisperer | free | up | 10137 ms |  |

## models

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| epoch_ai_models | free | up | 549 ms |  |
| huggingface_hub | free | up | 773 ms |  |
| modelscope | free | skipped | n/a | explicit-only |
| openrouter_rankings | free | up | 5393 ms |  |

## news

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| academic_ai_labs | free | degraded | 1460 ms | 2/3 feeds OK (degraded; dead: news.mit.edu: HTTP 403) |
| ai_newsletters | free | degraded | 5974 ms | 4/5 feeds OK (degraded; dead: thesequence.substack.com: HTTP 403) |
| canada_ai_research | free | skipped | n/a | timeout (>25s): health_check did not return |
| frontier_labs | free | degraded | 15357 ms | 16/17 feeds OK (degraded; dead: pytorch.org: HTTP 403) |
| gov_policy | free | skipped | n/a | explicit-only |
| hk_career_research | free | degraded | 12266 ms | 4/5 feeds OK (degraded; dead: www.astri.org: HTTP 200) |
| hk_universities | free | skipped | n/a | timeout (>25s): health_check did not return |
| scrape_canada | free | skipped | n/a | explicit-only |
| scrape_hongkong | free | skipped | n/a | explicit-only |
| scrape_js_sites | free | skipped | n/a | explicit-only |
| scrape_singapore | free | up | 15877 ms |  |
| singapore_ai_research | free | degraded | 8807 ms | 4/5 feeds OK (degraded; dead: www.techinasia.com: HTTP 403) |
| wayback | free | skipped | n/a | explicit-only |

## papers

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| acl_anthology | free | up | 880 ms |  |
| ai2 | free | skipped | n/a | explicit-only |
| alphaxiv | free | up | 539 ms |  |
| amii | free | skipped | n/a | explicit-only |
| ant_ling | free | skipped | n/a | explicit-only |
| arxiv | free | up | 370 ms |  |
| astar_cfar | free | skipped | n/a | explicit-only |
| baai | free | skipped | n/a | explicit-only |
| baichuan | free | skipped | n/a | explicit-only |
| biorxiv | free | up | 14115 ms |  |
| bytedance_research | free | skipped | n/a | explicit-only |
| cohere | free | skipped | n/a | explicit-only |
| contextual | free | skipped | n/a | explicit-only |
| core | keyed | skipped | n/a | requires operator credentials |
| crossref | free | up | 3247 ms |  |
| crossref_retractions | free | skipped | n/a | explicit-only |
| cvf_openaccess | free | up | 1001 ms |  |
| databricks_mosaic | free | skipped | n/a | explicit-only |
| dblp | free | up | 4488 ms |  |
| deepseek | free | skipped | n/a | explicit-only |
| distill_pub | free | skipped | n/a | explicit-only |
| eleutherai | free | skipped | n/a | explicit-only |
| europepmc | free | skipped | n/a | explicit-only |
| google_deepmind | free | skipped | n/a | explicit-only |
| hf_daily_papers | free | up | 1875 ms |  |
| huawei_noah | free | skipped | n/a | explicit-only |
| huggingface | free | skipped | n/a | explicit-only |
| liquid_ai | free | skipped | n/a | explicit-only |
| meta_fair | free | skipped | n/a | explicit-only |
| microsoft_research | free | skipped | n/a | explicit-only |
| mila | free | skipped | n/a | explicit-only |
| ml_cmu_blog | free | up | 4309 ms |  |
| mlrc | free | up | 6563 ms |  |
| moonshot | free | skipped | n/a | explicit-only |
| nous | free | skipped | n/a | explicit-only |
| nvidia_research | free | skipped | n/a | explicit-only |
| openai | free | skipped | n/a | explicit-only |
| openalex | free | up | 2348 ms |  |
| openalex_cn | free | skipped | n/a | explicit-only |
| openreview | keyed | skipped | n/a | requires operator credentials |
| pdf | free | down | 800 ms | PyMuPDF missing: No module named 'fitz' |
| pmlr | free | up | 3495 ms |  |
| qwen | free | skipped | n/a | explicit-only |
| rbc_borealis | free | skipped | n/a | explicit-only |
| reka | free | skipped | n/a | explicit-only |
| researcher_watch | free | up | 39 ms |  |
| s2_snippet | free | skipped | n/a | explicit-only |
| sakana | free | skipped | n/a | explicit-only |
| salesforce_research | free | skipped | n/a | explicit-only |
| scale_ai | free | skipped | n/a | explicit-only |
| sea_ai_lab | free | skipped | n/a | explicit-only |
| semantic_scholar | free | skipped | n/a | not verified: rate-limited (HTTP 429 from api.semanticscholar.org): RetryError |
| servicenow_research | free | skipped | n/a | explicit-only |
| shanghai_ai_lab | free | skipped | n/a | explicit-only |
| slideslive_talks | free | up | 16461 ms |  |
| stability | free | skipped | n/a | explicit-only |
| stepfun | free | skipped | n/a | explicit-only |
| tencent_hunyuan | free | skipped | n/a | explicit-only |
| together_ai | free | skipped | n/a | explicit-only |
| transformer_circuits | free | up | 10790 ms |  |
| underline_talks | free | up | 1939 ms |  |
| vector_institute | free | skipped | n/a | explicit-only |
| yi_01ai | free | skipped | n/a | explicit-only |
| zenodo | free | up | 1994 ms |  |
| zhipu | free | skipped | n/a | explicit-only |

## patents

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| google_patents | free | skipped | n/a | explicit-only |

## people

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| dblp_author | free | skipped | n/a | explicit-only |
| orcid | free | skipped | n/a | explicit-only |
| s2_authors | free | skipped | n/a | explicit-only |
| wikidata_identity | free | skipped | n/a | explicit-only |

## podcast

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| apple_podcasts | free | up | 249 ms |  |
| chinese_podcasts | free | up | 22199 ms |  |
| podcast_index | keyed | skipped | n/a | requires operator credentials |
| xiaoyuzhou | free | up | 4358 ms |  |

## policy

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| cset | free | blocked | 599 ms | HTTP 403: <!DOCTYPE HTML>       <html>         <head>           <title>403 - Forbidden</title>         </head>         <body style="font-family:Arial, Helvetica, sans-serif; text-align: center">       |
| federal_register | free | skipped | n/a | explicit-only |
| oecd_ai_policy | free | skipped | n/a | explicit-only |

## reference

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| wikidata_wikipedia | free | up | 1203 ms |  |

## safety

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| ai_incidents | free | skipped | n/a | explicit-only |
| alignment_forum | free | up | 2066 ms |  |

## social

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| bluesky | keyed | skipped | n/a | requires operator credentials |
| douyin | walled | skipped | n/a | requires operator credentials |
| mastodon | free | up | 6535 ms |  |
| x_search | free | skipped | n/a | explicit-only |

## tooling

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| agent_tooling_radar | free | skipped | n/a | explicit-only |

## video

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| bilibili | free | up | 2535 ms |  |
| youtube | walled | up | 13339 ms |  |
| youtube_channels | free | up | 12180 ms |  |
