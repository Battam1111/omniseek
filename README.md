# OmniSeek source health

Up: 72 | Degraded: 6 | Rate limited: 1 | Blocked: 1 | Down: 4 | Skipped: 134 | Total: 218
Blocked means the source answered but refused this vantage or credential; it is not counted as Down.
Skipped breakdown: policy=134 | capability absent=0 | sweep budget=0

Generated UTC: 2026-08-31T09:54:42Z
Vantage: github-actions
OmniSeek version: 0.2.0
Sweep duration: 23.993 seconds

Checked from GitHub Actions runners; a residential or maintainer deployment typically reaches more. One probe per source per run; this is a health signal, not an availability guarantee.

## books

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| books_openlibrary_ia | free | up | 7712 ms |  |
| gutenberg | free | skipped | n/a | explicit-only |

## career

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| linkedin_posts | free | skipped | n/a | explicit-only |
| nature_careers | free | up | 5070 ms |  |
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
| github_releases | free | up | 9912 ms |  |
| github_trending | free | up | 267 ms |  |
| pypi | free | up | 3701 ms |  |
| rl_llm_frameworks | free | up | 8750 ms |  |

## community

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| academia_se | free | up | 541 ms |  |
| ai_se | free | up | 530 ms |  |
| crossvalidated | free | up | 481 ms |  |
| cs_se | free | up | 181 ms |  |
| datascience_se | free | up | 11 ms |  |
| discord_communities | walled | skipped | n/a | requires operator credentials |
| discourse_forums | free | up | 11695 ms |  |
| douban_groups | walled | skipped | n/a | explicit-only |
| gter | free | skipped | n/a | explicit-only |
| hackernews | free | up | 599 ms |  |
| hardwarezone | free | skipped | n/a | explicit-only |
| juejin | free | skipped | n/a | explicit-only |
| lobsters | free | up | 5025 ms |  |
| quora | free | skipped | n/a | explicit-only |
| reddit | free | up | 7715 ms |  |
| sogou_weixin | free | skipped | n/a | explicit-only |
| stackoverflow | free | up | 13 ms |  |
| tieba | free | up | 4111 ms |  |
| v2ex | free | up | 355 ms |  |
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
| levels_fyi | free | down | 710 ms | role_ok=False comp_ok=False (page structure changed?) |
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
| gov_open_data | free | up | 13997 ms |  |
| statcan_wds | free | skipped | n/a | explicit-only |
| worldbank_stats | free | skipped | n/a | explicit-only |

## datasets

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| kaggle | free | skipped | n/a | explicit-only |

## deadlines

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| conference_deadlines | free | up | 3380 ms |  |
| ml_conferences | free | skipped | n/a | explicit-only |

## eval

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| llm_leaderboard | keyed | skipped | n/a | requires operator credentials |
| lmsys_arena | free | up | 1460 ms |  |
| ml_eval_safety | free | down | 3923 ms | all 2 feeds failed |
| scrape_ml_orgs | free | up | 4496 ms |  |

## filings

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| sec_edgar | free | up | 4401 ms |  |
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
| ai_residencies | free | up | 14490 ms |  |
| cihr_grants | free | skipped | n/a | explicit-only |
| cordis_eu | free | skipped | n/a | explicit-only |
| fellowships | free | up | 11125 ms |  |
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
| csrankings | free | up | 926 ms |  |
| exa | keyed | skipped | n/a | requires operator credentials |
| wikicfp_nlp | free | skipped | n/a | explicit-only |
| xiaohongshu_cn | walled | skipped | n/a | explicit-only |

## immigration

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| aaip_draws | free | skipped | n/a | explicit-only |
| bcpnp_invitations | free | skipped | n/a | explicit-only |
| canada_immigration | free | degraded | 11005 ms | 3/4 feeds OK (degraded; dead: www.immigration.ca) |
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
| academic_job_boards | free | up | 14687 ms |  |
| academic_jobs | free | up | 1672 ms |  |
| adzuna | keyed | skipped | n/a | requires operator credentials |
| ajo | free | up | 5045 ms |  |
| bytedance_seed | walled | skipped | n/a | explicit-only |
| feishu_jobs | walled | skipped | n/a | explicit-only |
| higheredjobs_cs | free | skipped | n/a | explicit-only |
| jobrxiv_canada | free | up | 6076 ms |  |
| layoffs_tracker | free | skipped | n/a | explicit-only |
| mycareersfuture | free | up | 1811 ms |  |
| overseas_ai_jobs | free | up | 8245 ms |  |
| remotive | free | skipped | n/a | explicit-only |
| vector_talent_hub | free | up | 1954 ms |  |

## media

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| chinese_ai_media | free | up | 6996 ms |  |
| kexue_fm | free | up | 4913 ms |  |
| substack_matrix | free | degraded | 3456 ms | 15/21 feeds OK (degraded; dead: benjamintodd.substack.com, chinai.substack.com, importai.substack.com, thealgorithmicbridge.substack.com, www.aisnakeoil.com, www.dwarkesh.com) |
| wechat | walled | skipped | n/a | explicit-only |

## methodology

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| a_happy_phd | free | up | 603 ms |  |
| github_awesome_phd | free | up | 520 ms |  |
| lesswrong | free | up | 5784 ms |  |
| ml_collective | free | up | 4531 ms |  |
| thesis_whisperer | free | up | 1989 ms |  |

## models

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| epoch_ai_models | free | up | 356 ms |  |
| huggingface_hub | free | up | 260 ms |  |
| modelscope | free | skipped | n/a | explicit-only |
| openrouter_rankings | free | up | 949 ms |  |

## news

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| academic_ai_labs | free | degraded | 15605 ms | 1/3 feeds OK (degraded; dead: bair.berkeley.edu, news.mit.edu) |
| ai_newsletters | free | degraded | 5520 ms | 3/4 feeds OK (degraded; dead: thesequence.substack.com) |
| canada_ai_research | free | up | 10572 ms |  |
| frontier_labs | free | degraded | 10001 ms | 17/18 feeds OK (degraded; dead: pytorch.org) |
| gov_policy | free | skipped | n/a | explicit-only |
| hk_career_research | free | up | 10168 ms |  |
| hk_universities | free | up | 22524 ms |  |
| scrape_canada | free | skipped | n/a | explicit-only |
| scrape_hongkong | free | skipped | n/a | explicit-only |
| scrape_js_sites | free | skipped | n/a | explicit-only |
| scrape_singapore | free | up | 6731 ms |  |
| singapore_ai_research | free | degraded | 4742 ms | 4/6 feeds OK (degraded; dead: feeds.feedburner.com, www.techinasia.com) |
| wayback | free | skipped | n/a | explicit-only |

## papers

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| acl_anthology | free | up | 1097 ms |  |
| ai2 | free | skipped | n/a | explicit-only |
| alphaxiv | free | up | 854 ms |  |
| amii | free | skipped | n/a | explicit-only |
| ant_ling | free | skipped | n/a | explicit-only |
| arxiv | free | up | 354 ms |  |
| astar_cfar | free | skipped | n/a | explicit-only |
| baai | free | skipped | n/a | explicit-only |
| baichuan | free | skipped | n/a | explicit-only |
| biorxiv | free | up | 992 ms |  |
| bytedance_research | free | skipped | n/a | explicit-only |
| cohere | free | skipped | n/a | explicit-only |
| contextual | free | skipped | n/a | explicit-only |
| core | keyed | skipped | n/a | requires operator credentials |
| crossref | free | up | 998 ms |  |
| crossref_retractions | free | skipped | n/a | explicit-only |
| cvf_openaccess | free | up | 882 ms |  |
| databricks_mosaic | free | skipped | n/a | explicit-only |
| dblp | free | up | 779 ms |  |
| deepseek | free | skipped | n/a | explicit-only |
| distill_pub | free | skipped | n/a | explicit-only |
| eleutherai | free | skipped | n/a | explicit-only |
| europepmc | free | skipped | n/a | explicit-only |
| google_deepmind | free | skipped | n/a | explicit-only |
| hf_daily_papers | free | up | 703 ms |  |
| huawei_noah | free | skipped | n/a | explicit-only |
| huggingface | free | skipped | n/a | explicit-only |
| liquid_ai | free | skipped | n/a | explicit-only |
| meta_fair | free | skipped | n/a | explicit-only |
| microsoft_research | free | skipped | n/a | explicit-only |
| mila | free | skipped | n/a | explicit-only |
| ml_cmu_blog | free | up | 13019 ms |  |
| mlrc | free | up | 567 ms |  |
| moonshot | free | skipped | n/a | explicit-only |
| nous | free | skipped | n/a | explicit-only |
| nvidia_research | free | skipped | n/a | explicit-only |
| openai | free | skipped | n/a | explicit-only |
| openalex | free | up | 646 ms |  |
| openalex_cn | free | skipped | n/a | explicit-only |
| openreview | keyed | skipped | n/a | requires operator credentials |
| pdf | free | down | 2 ms | PyMuPDF missing: No module named 'fitz' |
| pmlr | free | up | 4371 ms |  |
| qwen | free | skipped | n/a | explicit-only |
| rbc_borealis | free | skipped | n/a | explicit-only |
| reka | free | skipped | n/a | explicit-only |
| researcher_watch | free | up | 19 ms |  |
| s2_snippet | free | skipped | n/a | explicit-only |
| sakana | free | skipped | n/a | explicit-only |
| salesforce_research | free | skipped | n/a | explicit-only |
| scale_ai | free | skipped | n/a | explicit-only |
| sea_ai_lab | free | skipped | n/a | explicit-only |
| semantic_scholar | free | rate_limited | 8782 ms | OK (HTTP 429: API alive, rate-limiting us) |
| servicenow_research | free | skipped | n/a | explicit-only |
| shanghai_ai_lab | free | skipped | n/a | explicit-only |
| slideslive_talks | free | up | 5680 ms |  |
| stability | free | skipped | n/a | explicit-only |
| stepfun | free | skipped | n/a | explicit-only |
| tencent_hunyuan | free | skipped | n/a | explicit-only |
| together_ai | free | skipped | n/a | explicit-only |
| transformer_circuits | free | up | 2927 ms |  |
| underline_talks | free | up | 752 ms |  |
| vector_institute | free | skipped | n/a | explicit-only |
| yi_01ai | free | skipped | n/a | explicit-only |
| zenodo | free | up | 3601 ms |  |
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
| apple_podcasts | free | up | 670 ms |  |
| chinese_podcasts | free | up | 5133 ms |  |
| podcast_index | keyed | skipped | n/a | requires operator credentials |
| xiaoyuzhou | free | up | 1754 ms |  |

## policy

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| cset | free | blocked | 2533 ms | HTTP 403 |
| federal_register | free | skipped | n/a | explicit-only |
| oecd_ai_policy | free | skipped | n/a | explicit-only |

## reference

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| wikidata_wikipedia | free | down | 1726 ms | unexpected wbsearchentities shape |

## safety

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| ai_incidents | free | skipped | n/a | explicit-only |
| alignment_forum | free | up | 1426 ms |  |

## social

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| bluesky | keyed | skipped | n/a | requires operator credentials |
| douyin | walled | skipped | n/a | requires operator credentials |
| mastodon | free | up | 11140 ms |  |
| x_search | free | skipped | n/a | explicit-only |

## tooling

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| agent_tooling_radar | free | skipped | n/a | explicit-only |

## video

| Source | Tier | Status | Latency | Detail |
| --- | --- | --- | --- | --- |
| bilibili | free | up | 2228 ms |  |
| youtube | walled | up | 4173 ms |  |
| youtube_channels | free | up | 4150 ms |  |
