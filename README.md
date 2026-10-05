# OmniSeek source health

Up: 68 | Degraded: 6 | Rate limited: 0 | Blocked: 2 | Down: 2 | Skipped: 140 | Total: 218
Blocked means the source answered but refused this vantage or credential; it is not counted as Down.
Skipped breakdown: policy=134 | capability absent=6 | sweep budget=0

Generated UTC: 2026-10-05T10:38:06Z
Vantage: github-actions
OmniSeek version: 0.2.1
Sweep duration: 29.097 seconds

Checked from GitHub Actions runners; a residential or maintainer deployment typically reaches more. One probe per source per run; this is a health signal, not an availability guarantee.

## books

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| books_openlibrary_ia | free | up | 6933 ms |  |
| gutenberg | free | skipped | n/a | explicit-only |

## career

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| linkedin_posts | free | skipped | n/a | explicit-only |
| nature_careers | free | up | 3920 ms |  |
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
| github_releases | free | skipped | n/a | timeout (>25s): health_check did not return |
| github_trending | free | up | 443 ms |  |
| pypi | free | up | 1849 ms |  |
| rl_llm_frameworks | free | up | 16178 ms |  |

## community

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| academia_se | free | up | 606 ms |  |
| ai_se | free | up | 597 ms |  |
| crossvalidated | free | up | 586 ms |  |
| cs_se | free | up | 190 ms |  |
| datascience_se | free | up | 11 ms |  |
| discord_communities | walled | skipped | n/a | requires operator credentials |
| discourse_forums | free | up | 9961 ms |  |
| douban_groups | walled | skipped | n/a | explicit-only |
| gter | free | skipped | n/a | explicit-only |
| hackernews | free | up | 639 ms |  |
| hardwarezone | free | skipped | n/a | explicit-only |
| juejin | free | skipped | n/a | explicit-only |
| lobsters | free | up | 8353 ms |  |
| quora | free | skipped | n/a | explicit-only |
| reddit | free | skipped | n/a | not verified: rate-limited (Arctic Shift: HTTP 422, its "Timeout. Maybe slow down a bit" refusal; the far end's load-based rate limit refused the probe; not retried) |
| sogou_weixin | free | skipped | n/a | explicit-only |
| stackoverflow | free | up | 13 ms |  |
| tieba | free | up | 7426 ms |  |
| v2ex | free | up | 1368 ms |  |
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
| levels_fyi | free | down | 936 ms | role_ok=False comp_ok=False (page structure changed?) |
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
| gov_open_data | free | up | 15775 ms |  |
| statcan_wds | free | skipped | n/a | explicit-only |
| worldbank_stats | free | skipped | n/a | explicit-only |

## datasets

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| kaggle | free | skipped | n/a | explicit-only |

## deadlines

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| conference_deadlines | free | up | 17300 ms |  |
| ml_conferences | free | skipped | n/a | explicit-only |

## eval

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| llm_leaderboard | keyed | skipped | n/a | requires operator credentials |
| lmsys_arena | free | up | 5219 ms |  |
| ml_eval_safety | free | blocked | 2973 ms | all 2 feeds failed (epochai.substack.com: HTTP 403, metr.substack.com: HTTP 403) |
| scrape_ml_orgs | free | up | 11618 ms |  |

## filings

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| sec_edgar | free | up | 2119 ms |  |
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
| fellowships | free | up | 21980 ms |  |
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
| csrankings | free | up | 909 ms |  |
| exa | keyed | skipped | n/a | requires operator credentials |
| wikicfp_nlp | free | skipped | n/a | explicit-only |
| xiaohongshu_cn | walled | skipped | n/a | explicit-only |

## immigration

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| aaip_draws | free | skipped | n/a | explicit-only |
| bcpnp_invitations | free | skipped | n/a | explicit-only |
| canada_immigration | free | degraded | 15932 ms | 3/4 feeds OK (degraded; dead: www.immigration.ca: HTTP 403) |
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
| academic_jobs | free | up | 1050 ms |  |
| adzuna | keyed | skipped | n/a | requires operator credentials |
| ajo | free | up | 12100 ms |  |
| bytedance_seed | walled | skipped | n/a | explicit-only |
| feishu_jobs | walled | skipped | n/a | explicit-only |
| higheredjobs_cs | free | skipped | n/a | explicit-only |
| jobrxiv_canada | free | up | 9901 ms |  |
| layoffs_tracker | free | skipped | n/a | explicit-only |
| mycareersfuture | free | up | 2168 ms |  |
| overseas_ai_jobs | free | up | 18211 ms |  |
| remotive | free | skipped | n/a | explicit-only |
| vector_talent_hub | free | up | 3235 ms |  |

## media

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| chinese_ai_media | free | up | 3840 ms |  |
| kexue_fm | free | up | 5503 ms |  |
| substack_matrix | free | degraded | 10466 ms | 15/21 feeds OK (degraded; dead: benjamintodd.substack.com: HTTP 403, calnewport.com: HTTP 403, chinai.substack.com: HTTP 403, importai.substack.com: HTTP 403, thealgorithmicbridge.substack.com: HTTP 4 |
| wechat | walled | skipped | n/a | explicit-only |

## methodology

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| a_happy_phd | free | up | 824 ms |  |
| github_awesome_phd | free | up | 524 ms |  |
| lesswrong | free | up | 6521 ms |  |
| ml_collective | free | up | 6342 ms |  |
| thesis_whisperer | free | up | 7057 ms |  |

## models

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| epoch_ai_models | free | up | 395 ms |  |
| huggingface_hub | free | up | 174 ms |  |
| modelscope | free | skipped | n/a | explicit-only |
| openrouter_rankings | free | up | 3702 ms |  |

## news

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| academic_ai_labs | free | degraded | 1475 ms | 2/3 feeds OK (degraded; dead: news.mit.edu: HTTP 403) |
| ai_newsletters | free | degraded | 4124 ms | 4/5 feeds OK (degraded; dead: thesequence.substack.com: HTTP 403) |
| canada_ai_research | free | up | 22638 ms |  |
| frontier_labs | free | degraded | 10692 ms | 16/17 feeds OK (degraded; dead: pytorch.org: HTTP 403) |
| gov_policy | free | skipped | n/a | explicit-only |
| hk_career_research | free | up | 8857 ms |  |
| hk_universities | free | skipped | n/a | timeout (>25s): health_check did not return |
| scrape_canada | free | skipped | n/a | explicit-only |
| scrape_hongkong | free | skipped | n/a | explicit-only |
| scrape_js_sites | free | skipped | n/a | explicit-only |
| scrape_singapore | free | up | 13211 ms |  |
| singapore_ai_research | free | degraded | 6219 ms | 4/5 feeds OK (degraded; dead: www.techinasia.com: HTTP 403) |
| wayback | free | skipped | n/a | explicit-only |

## papers

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| acl_anthology | free | up | 1009 ms |  |
| ai2 | free | skipped | n/a | explicit-only |
| alphaxiv | free | up | 572 ms |  |
| amii | free | skipped | n/a | explicit-only |
| ant_ling | free | skipped | n/a | explicit-only |
| arxiv | free | up | 409 ms |  |
| astar_cfar | free | skipped | n/a | explicit-only |
| baai | free | skipped | n/a | explicit-only |
| baichuan | free | skipped | n/a | explicit-only |
| biorxiv | free | up | 13576 ms |  |
| bytedance_research | free | skipped | n/a | explicit-only |
| cohere | free | skipped | n/a | explicit-only |
| contextual | free | skipped | n/a | explicit-only |
| core | keyed | skipped | n/a | requires operator credentials |
| crossref | free | up | 463 ms |  |
| crossref_retractions | free | skipped | n/a | explicit-only |
| cvf_openaccess | free | up | 446 ms |  |
| databricks_mosaic | free | skipped | n/a | explicit-only |
| dblp | free | up | 4643 ms |  |
| deepseek | free | skipped | n/a | explicit-only |
| distill_pub | free | skipped | n/a | explicit-only |
| eleutherai | free | skipped | n/a | explicit-only |
| europepmc | free | skipped | n/a | explicit-only |
| google_deepmind | free | skipped | n/a | explicit-only |
| hf_daily_papers | free | up | 728 ms |  |
| huawei_noah | free | skipped | n/a | explicit-only |
| huggingface | free | skipped | n/a | explicit-only |
| liquid_ai | free | skipped | n/a | explicit-only |
| meta_fair | free | skipped | n/a | explicit-only |
| microsoft_research | free | skipped | n/a | explicit-only |
| mila | free | skipped | n/a | explicit-only |
| ml_cmu_blog | free | up | 6957 ms |  |
| mlrc | free | up | 1360 ms |  |
| moonshot | free | skipped | n/a | explicit-only |
| nous | free | skipped | n/a | explicit-only |
| nvidia_research | free | skipped | n/a | explicit-only |
| openai | free | skipped | n/a | explicit-only |
| openalex | free | up | 1473 ms |  |
| openalex_cn | free | skipped | n/a | explicit-only |
| openreview | keyed | skipped | n/a | requires operator credentials |
| pdf | free | down | 101 ms | PyMuPDF missing: No module named 'fitz' |
| pmlr | free | up | 2142 ms |  |
| qwen | free | skipped | n/a | explicit-only |
| rbc_borealis | free | skipped | n/a | explicit-only |
| reka | free | skipped | n/a | explicit-only |
| researcher_watch | free | up | 69 ms |  |
| s2_snippet | free | skipped | n/a | explicit-only |
| sakana | free | skipped | n/a | explicit-only |
| salesforce_research | free | skipped | n/a | explicit-only |
| scale_ai | free | skipped | n/a | explicit-only |
| sea_ai_lab | free | skipped | n/a | explicit-only |
| semantic_scholar | free | skipped | n/a | not verified: rate-limited (HTTP 429 from api.semanticscholar.org): RetryError |
| servicenow_research | free | skipped | n/a | explicit-only |
| shanghai_ai_lab | free | skipped | n/a | explicit-only |
| slideslive_talks | free | up | 15020 ms |  |
| stability | free | skipped | n/a | explicit-only |
| stepfun | free | skipped | n/a | explicit-only |
| tencent_hunyuan | free | skipped | n/a | explicit-only |
| together_ai | free | skipped | n/a | explicit-only |
| transformer_circuits | free | up | 6364 ms |  |
| underline_talks | free | up | 1244 ms |  |
| vector_institute | free | skipped | n/a | explicit-only |
| yi_01ai | free | skipped | n/a | explicit-only |
| zenodo | free | up | 2366 ms |  |
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
| apple_podcasts | free | up | 494 ms |  |
| chinese_podcasts | free | up | 17565 ms |  |
| podcast_index | keyed | skipped | n/a | requires operator credentials |
| xiaoyuzhou | free | up | 3033 ms |  |

## policy

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| cset | free | blocked | 4576 ms | HTTP 403: <!DOCTYPE HTML>       <html>         <head>           <title>403 - Forbidden</title>         </head>         <body style="font-family:Arial, Helvetica, sans-serif; text-align: center">       |
| federal_register | free | skipped | n/a | explicit-only |
| oecd_ai_policy | free | skipped | n/a | explicit-only |

## reference

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| wikidata_wikipedia | free | up | 1716 ms |  |

## safety

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| ai_incidents | free | skipped | n/a | explicit-only |
| alignment_forum | free | up | 1047 ms |  |

## social

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| bluesky | keyed | skipped | n/a | requires operator credentials |
| douyin | walled | skipped | n/a | requires operator credentials |
| mastodon | free | up | 9089 ms |  |
| x_search | free | skipped | n/a | explicit-only |

## tooling

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| agent_tooling_radar | free | skipped | n/a | explicit-only |

## video

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| bilibili | free | up | 2447 ms |  |
| youtube | walled | up | 12720 ms |  |
| youtube_channels | free | up | 11666 ms |  |
