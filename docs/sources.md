# The source catalog

<sub>[OmniSeek](../README.md)&nbsp;·&nbsp;[Configuration](configuration.md)&nbsp;·&nbsp;[Walled sources](walled-sources.md)&nbsp;·&nbsp;[Tools](tools.md)</sub>

Every source below earned its place by beating plain web search at something, via one of
five modes: structure, unwall, transcribe, recall, monitor. Access tiers: **free** is on by
default; **keyed** activates once you supply the API key; **walled** stays off until you
bring your own login. Sources marked `explicit-only` never join the broad sweep: name them
in `sources=[...]` to use them. The catalog is not a fixed list: the built-in curator
pipeline probes, judges, and admits new sources, and retires the ones that decay.

**This file is generated.** `python scripts/gen_sources_doc.py` rebuilds it from the
shipped catalog (the sync pipeline reruns it on every engine update); to change a line,
change the source module it describes. Each description below is the same string the
`omniseek_sources` tool returns at runtime, truncated for the page. A source files under
its first domain only.

Generated from omniseek 0.1.2: 218 live sources across 32 domains.

## papers (65)

- **acl_anthology** `free` `lookup`: ACL Anthology: NLP venue-of-record 按卷浏览 (官方数据仓 XML, keyless). 查某届会议实际收了什么: 'acl 2025 reasoning' / 'emnlp2024 retrieval' / 'volume:2024.findings-emnlp'(裸集合 id). 必须带 venue+年份 token (这是卷浏览器; 跨会关键词搜索请用 dblp / semantic_scholar / arxiv)
- **ai2** `free` `explicit-only` `stream`: Ai2 近期论文流 (经 OpenAlex 两全名短语精确隔离: 长名=819 + 短名=570, 名称唯一无误中). OLMo/Tulu 开源 LLM、agentic LLM contexts、reasoning, 对 LLM PhD 工业+研究监测高度相关. 宽口径机构, 浏览需带主题词查 (Ai2 还带气候 emulator/保护工具/软件 release).
- **alphaxiv** `free` `lookup`: AlphaXiv: trending preprint buzz (Hot feed: visits + votes) + per-paper community discussion + AI-generated paper overview, all via the keyless api.alphaxiv.org REST API
- **amii** `free` `explicit-only` `stream`: Amii (埃德蒙顿) 近期论文流 (经 OpenAlex 全名 'Alberta Machine Intelligence Institute'=542 为锚 + 裸 'Amii'=279 经 10/10 抽样验证干净加召回). 对 PhD 的 Canada 路线是落地目标; 强 RL/reasoning 产出 (offline policy selection、Nash 均衡 meta-learning、K-Step return 蒸馏). 宽口径机构, 浏览需带主题词查 (亦发计算生物/化学材料/医学影像).
- **ant_ling** `free` `explicit-only` `stream`: 蚂蚁集团 (Ant Group,含 Ling / Inclusion AI 实验室) 近期论文,经 OpenAlex 'Ant Group'(count=2260,14/14 抽样真带连续公司短语,零昆虫/生物误匹配)捕获。透传捕获 Ling/Inclusion AI 论文 (其署名读作 'Inclusion AI, Ant Group, Hangzhou, China')。强 on-topic LLM/RL/agent/RAG/reasoning:多 agent RAG、test-time-compute survey、GraphRAG、越狱攻击、LLM 数据合成。窄串 'Inclusion ...
- **arxiv** `free` `lookup`: arXiv preprints: 3M+ papers across physics, math, CS, biology
- **astar_cfar** `free` `explicit-only` `stream`: A*STAR CFAR (新加坡) 近期论文流 (经 OpenAlex 全名短语 'Centre for Frontier AI Research'=422 精确隔离, 10/10 抽样绑 A*STAR/Singapore, 零 false positive; 裸缩写 'CFAR' 禁用: 误中雷达 Constant False Alarm Rate). on-topic (Tool-Using LLM Agents、dataset distillation、rectified flow). 对 PhD 的 SG 路线是落地目标. 宽口径机构, 浏览需带主题词查 (CFAR 跨医疗 AI/MR ...
- **baai** `free` `explicit-only` `stream`: 北京智源人工智能研究院 (BAAI) 近期论文,经 OpenAlex 引号短语 "Beijing Academy of Artificial Intelligence"(640,15/15 抽样真带) + 干净缩写 'BAAI'(85,10/10 真) 精确隔离。on-topic 核心:VLA、RL 机器人规划 (RoboGPT-R1)、LLM 上下文-记忆调和、多模态 next-token 预测、高效 LLM 探索。对 CN ML PhD on-topic。precision 强制:必须引号短语;未引号 (11,338) 在 Beijing/Artificial/Intelligence t ...
- **baichuan** `free` `explicit-only` `stream`: 百川智能 (Baichuan) 近期论文,经 OpenAlex 两个更具体短语 "Baichuan Inc"(14 篇全真)+"Baichuan Intelligent Technology"(英文/北京公司变体,捕 AITQE、HtmlRAG、Sequential Preference Optimization)精确隔离北京 LLM 实验室。覆盖 LLM 医生、多模态数学 benchmark、长上下文 KV cache (PQCache)、长上下文数据框架。对 CN ML PhD on-topic。precision 关键:bare 'Baichuan'(190)被生物医药/中医/机械 (唐 ...
- **biorxiv** `free` `stream`: bioRxiv preprints: ML/AI cross-disciplinary applications (bioinformatics + computational neuroscience subjects)
- **bytedance_research** `free` `explicit-only` `stream`: 字节跳动 Seed 研究实验室近期论文,经 OpenAlex 'ByteDance Seed'(79,~10/10 真带 'ByteDance Seed, Beijing/Shanghai/Singapore' 等)精确隔离。'Seed' 限定词关键:bare 'ByteDance' 会过匹配游戏/广告/推荐/电商,'ByteDance Seed' 锁定研究实验室。on-topic:统一视觉-语言生成模型、超大规模多模态 LLM 训练系统 (MegaScale-Omni)、MoE 训练、语音自回归扩散 DPO。对 CN ML PhD 含 Singapore 站点 (Bytedance Seed ...
- **cohere** `free` `explicit-only` `stream`: Cohere 研究部近期论文流 (经 OpenAlex 'Cohere for AI'=14 + 'Cohere Labs'=8 精确隔离, 两者均 100% 干净; Cohere Labs 是 Cohere For AI 的 2025 改名). 裸 'Cohere' 被拒 (误中 Cohere Health 等). 多语言 LLM/RL/agent/MoE/LLM 安全, 多伦多本部对 PhD 的 Canada 路线是落地目标. 量小但隔离精准.
- **contextual** `free` `explicit-only` `stream`: Contextual AI 近期论文流 (经 OpenAlex 'Contextual AI'=13, 抽样多为真实公司地址 'Contextual AI, Mountain View'). RAG/LLM eval/alignment 公司, on-topic (LMUNIT eval、APO alignment、MIEB embedding benchmark). 注意: 'Contextual AI' 也是通用英文短语, ~1-2/13 为描述性 false positive, 量小可容忍但需偶尔人眼核. 不宽.
- **core** `keyed` `bring-your-own-login` `lookup`: CORE (core.ac.uk): largest open-access research aggregator; the ONLY eye paper source that returns the EXTRACTED FULL-TEXT body (work.fullText) + a working OA PDF url, where arxiv/openalex/semantic_scholar/crossref give only metadata. Reach for it to read a paper's actual text, not just its abstract ...
- **crossref** `free` `lookup`: Crossref: DOI registration agency, ~150M formally-published works (complement to arxiv/openalex/semantic_scholar)
- **crossref_retractions** `free` `explicit-only` `stream`: Crossref 撤稿通知流 (filter=update-type:retraction): 最新撤稿的结构化记录: 撤稿通知 DOI + 被撤论文 DOI (update-to) + 期刊/出版商/撤稿日期/原作者. MONITOR 研究诚信 + STRUCTURE (网搜只给撤稿的散文报道, 这里给逐条机读记录, 最新在前). query= 可按主题过滤 (如 'language model'). 注意整体偏生物医学, AI/NLP 信号稀疏: 当可过滤的 firehose 用, 非预筛 NLP 榜. 命名钻取 (omniseek_search 单源 raw). 补 omniseek_p ...
- **cvf_openaccess** `free` `lookup`: CVF Open Access: 计算机视觉 venue-of-record 按会议浏览 (CVPR/ICCV/WACV, 官方开放获取, keyless). 查某届会议实际收了什么: 'cvpr 2024 diffusion' / 'iccv2023 segmentation' / 'venue:WACV2024'(裸会议 id). 必须带 venue+年份 token (这是会议浏览器; 跨会关键词搜索请用 dblp / semantic_scholar / arxiv). 列表页给标题/作者/PDF/BibTeX, 摘要在每篇论文页 (对论文 URL 用 omniseek_read 补全 ...
- **databricks_mosaic** `free` `explicit-only` `stream`: Databricks/Mosaic 近期论文流 (经 OpenAlex 'Databricks'=189 精确隔离公司 + LLM 专属 'Databricks Mosaic AI Research'; MosaicML 被收购后 ML/LLM 研究归于 Databricks). 拒裸 'Mosaic'/'Mosaic Research' (2744/75, 几乎全为无关机构/医院). 含 LLM/agent (skill shadowing、agentic coding). 宽口径机构, 浏览需带主题词查 (流偏数据工程/系统 + GitHub release).
- **dblp** `free` `lookup`: DBLP: CS bibliography database (~7M publications + 3M authors, venue + year + DOI metadata; canonical CS publication lens)
- **deepseek** `free` `explicit-only` `stream`: DeepSeek (深度求索) 近期论文,经 OpenAlex raw-affiliation 短语 "DeepSeek-AI" 精确隔离单实验室产出 (meta.count=25),保留 authorship 真带该串者。覆盖 LLM/RL/reasoning/multimodal 前沿 (DeepSeek-R1、V3、Janus 多模态、Fire-Flyer AI-HPC)。对一名 CN ML PhD (LLM/RL/reasoning/agent) 极具风向标价值:中国最受关注的开源前沿团队,引用与方法直接影响 SG/Canada 求职与研究选题。注意必须用连字符短语,bare 'Dee ...
- **distill_pub** `free` `explicit-only` `stream`: Distill.pub: ML visualization & interpretability archive (Chris Olah et al., 2016-2021, ~40 articles, canonical reference)
- **eleutherai** `free` `explicit-only` `stream`: EleutherAI 近期论文流 (经 OpenAlex 'EleutherAI'=43 精确隔离, 造词名无 false-friend; type:article=18 为真实论文流, 余为 Zenodo 软件 release 也属 lab 产出). 真实成员 (Nora Belrose/Stella Biderman/Hailey Schoelkopf). LLM/interp/eval/reasoning 集中, 不宽 (建议带 type:article 跳过软件 release).
- **europepmc** `free` `explicit-only` `lookup`: Europe PMC: keyless biomedical / life-sciences literature (abstracts + citations + open-access JATS full text); name it to drill a clinical / biomedical question. OmniSeek's SECOND keyless full-text spine (CORE needs a key, this does not). STRUCTURE, keyless www.ebi.ac.uk.
- **google_deepmind** `free` `explicit-only` `stream`: Google DeepMind 近期论文流 (经 OpenAlex "Google DeepMind, London" 精确隔离: 375 篇均真实, 已核 raw_affiliation). 裸 "Google DeepMind"/"DeepMind" 被把 Gemini 当共同作者的博客垃圾 ('My Weird Prompts') 污染, 故以 London 总部限定为主, 'DeepMind, London' 兜旧地址. LLM/RL/agent/reasoning 第一梯队, 对标 LLM PhD 必盯. 宽口径机构, 浏览需带主题词查 (DeepMind 还跨生物/物理/数学).
- **hf_daily_papers** `free` `stream`: HuggingFace Daily Papers: AK / community-curated daily ML papers, with upvotes, ai_summary, githubRepo links. Curation layer above raw arXiv.
- **huawei_noah** `free` `explicit-only` `stream`: 华为诺亚方舟实验室 (Huawei Noah's Ark Lab) 近期论文,经 OpenAlex 'Huawei Noah's Ark Lab'(1604,主串,10/10 真) + 可选更宽 'Noah's Ark Lab'(1622,~+18 同精度) 精确隔离。名字高度独特,跨深圳/北京/Paris/Montreal 研究中心,零误匹配。强 on-topic LLM/RL/reasoning/agent:扩散 LLM 训练 (Mask Is What DLLM Needs)、LLM 数学推理 (CAMA)、LLM 多 agent 协调的自适应心智理论、LLM 极致压缩 (PocketL ...
- **huggingface** `free` `explicit-only` `stream`: Hugging Face 近期论文流 (经 OpenAlex 'Hugging Face'=144 精确隔离, 双词短语无通用碰撞; 真实成员 Loubna Ben Allal/Lewis Tunstall/Thomas Wolf 等, 最新 2026-06-08 仍活跃). LLM/RL/agent (TRL/RLHF、开源模型训练、Nash learning from human feedback). 不宽, 无需主题词.
- **liquid_ai** `free` `explicit-only` `stream`: Liquid AI 论文流 (经 OpenAlex 'Liquid AI'=4 精确; 引号 bigram 不与化学/流体碰撞, 抽样 raw_affiliation 含 'Liquid AI, San Francisco', 零 false positive). Liquid 神经网络/基础模型创业公司, on-topic (flow matching、基因组基础模型 Evo 2). 量低 (年轻公司多在 arXiv/blog), 精确, 随索引增长. 不宽.
- **meta_fair** `free` `explicit-only` `stream`: Meta FAIR 近期论文流 (经 OpenAlex 三短语精确隔离: 旧名 'Facebook AI Research'=1352 + 现名 'FAIR at Meta'=32 + 'Fundamental AI Research'=48, 均干净). 故意不用裸 'Meta AI' (有拼接伪影且更宽). LLM/RL/reasoning/agent 领军 lab, 对标 LLM PhD 工业研究前沿. 宽口径机构, 浏览需带主题词查 (FAIR 还跨催化/分子晶体/神经影像).
- **microsoft_research** `free` `explicit-only` `stream`: Microsoft Research 近期论文流 (经 OpenAlex 短语 'Microsoft Research'=37227 精确隔离, 每条 raw_affiliation 均含该短语; 不像裸 'Microsoft' 误中硬件/产品). 各子单元 (Redmond/New England/AI for Science/Asia) 都带母短语, 单串覆盖全 lab. 含真实 on-topic 工作 (如 'The Geometry of LLM-as-Judge'). 宽口径机构, 浏览需带主题词查 (MSR 横跨生物/系统/理论/材料).
- **mila** `free` `explicit-only` `stream`: Mila (蒙特利尔) 近期论文流 (经 OpenAlex 多词短语 'Quebec AI Institute'=892 为主 + 'Mila - Quebec AI Institute'=849 兜底; 裸 'Mila' 禁用: =4203 误中阿尔及利亚/马来西亚大学+城市). 对 PhD 的 Canada 路线是落地目标. on-topic 密度高 (Drift Q-Learning、MoE 语言适配、LLM query 重写、AI 供应链后门). 宽口径机构, 浏览需带主题词查 (Mila 亦发化学/医疗 AI/优化), 但 on-topic 密度远高于 Vector, 主题词可选.
- **ml_cmu_blog** `free` `stream`: ML@CMU 博客: CMU 机器学习系学生/教师撰写的 RL/LLM/reasoning/agent 深读 (低传播、不会自己上 HN/Twitter 的高信号源, 正方向). 补 academic_ai_labs(CRFM/BAIR/CSAIL)
- **mlrc** `free` `lookup`: Reproducibility methodology papers (broad search across ICLR/NeurIPS/ICML/TMLR)
- **moonshot** `free` `explicit-only` `stream`: Moonshot AI (月之暗面 / Kimi) 近期论文,经 OpenAlex raw-affiliation "Moonshot AI"(meta.count=24)精确隔离,authorship 真带 'Moonshot AI'/'Moonshot AI, Beijing, China'。覆盖 LLM serving/reasoning (Mooncake KVCache 服务、ExtendAttack on LRMs、VisionLLaMA)。对 CN ML PhD on-topic:长上下文/推理服务系统是热门方向。caveat:~3-4 篇 AI-generated spam 预 ...
- **nous** `free` `explicit-only` `stream`: Nous Research 近期论文流 (经 OpenAlex 'Nous Research'=15 隔离 AI lab; 近 4 篇 2026 LLM 论文带 'Nous Research Nous Research Nous Research' 三连真实 affiliation, 其余 11 篇为 2000-2011 已停的都柏林同名机构, 因按 publication_date desc 监控近期流而自然落出窗口). LLM 预训练/attention/tokenization/reasoning, 不宽 (可加 from 2024 过滤旧同名).
- **nvidia_research** `free` `explicit-only` `stream`: NVIDIA Research 近期论文流 (经 OpenAlex 'NVIDIA Research'=548 精确隔离, 10/10 抽样 raw_affiliation 均含该短语). 选 'NVIDIA Research' 而非裸 'NVIDIA Corporation' (1813, 误中 HPC/基因组/气候/量子硬件). 含 long-horizon agent 记忆、code-reasoning RL、RL 策略摘要等 on-topic. 宽口径机构, 浏览需带主题词查 (NVIDIA 还跨图形/神经渲染/气候/自动驾驶).
- **openai** `free` `explicit-only` `stream`: OpenAI 近期论文流 (经 OpenAlex raw-affiliation 短语 "OpenAI, San Francisco" 精确隔离: 73 篇均为真实研究, 已逐条核 raw_affiliation_strings). 裸 "OpenAI" 被 GitHub release-bot / 以 'Codex' 'Claude' 为伪作者的 '@openai' 垃圾污染, 故只用带城市的短语. LLM/RL/reasoning/agent 核心 lab, 对标 LLM PhD 工业前沿. precision-over-recall: 量小 (多数 arXiv-only 不带城市) 但干 ...
- **openalex** `free` `lookup`: OpenAlex: open academic graph (250M+ scholarly works, institutions + concepts ontology; open alternative to Semantic Scholar)
- **openalex_cn** `free` `explicit-only` `lookup`: 中文学术: Chinese-LANGUAGE scholarship via OpenAlex (language:zh): 中文期刊论文 + 学位论文 (add inline `type:dissertation`) that OmniSeek's English paper sources (arxiv/s2/crossref) miss. Returns structured 题录: title / authors / 中文期刊 venue / year / citations / OA / abstract. Optional inline filters: `type:dissert ...
- **openreview** `keyed` `bring-your-own-login` `lookup`: OpenReview: peer reviews, rebuttals, meta-reviews from ICLR/NeurIPS/ICML; venue browse via `venue:` qualifier (venue:colm2025 / venue:iclr2026 / raw venueid) and a submission's actual reviews via `reviews:` (reviews:<forum_id> or a /forum?id=… URL); browse a venue's accepted papers via its venueid
- **pdf** `free` `portal`: PDF full-text: download a paper PDF (arxiv.org/pdf/… or any *.pdf) and extract its text so you can read the WHOLE paper, not just the abstract (pair with omniseek_paper_enrich's pdf_url)
- **pmlr** `free` `stream`: PMLR: Proceedings of Machine Learning Research (ICML / AISTATS / COLT / NeurIPS comp / UAI / IJCAI / CoRL / AutoML / MIDL + 等; 232 volumes)
- **qwen** `free` `explicit-only` `stream`: 阿里通义 (Qwen) 实验室近期论文,经 OpenAlex 短语对 "Tongyi Lab"(90 篇,全部 'Tongyi Lab, Alibaba Group')+"Qwen Team"(显式 Qwen-LLM-team 论文如 ProcessBench)精确隔离阿里旗舰 AI 实验室及 Qwen 线。覆盖 agentic-RL/RL-蒸馏 (AgentJet、EvoRubric、TCOD) 与多模态/视频生成。对 CN ML PhD (LLM/agent/RL+multimodal) on-topic:Qwen 是开源权重生态主力。precision 关键:勿用 bare 'Qwen' ...
- **rbc_borealis** `free` `explicit-only` `stream`: RBC Borealis 近期论文流 (经 OpenAlex 'Borealis AI'=97 + 改名后 'RBC Borealis'=5 精确隔离, 抽样 raw_affiliation 均含该串, 双词无误中). 对 PhD 的 Canada 路线相关 (多伦多/温哥华/蒙特利尔 RBC 工业 lab). on-topic: LLM/RL/agent (LLM 先验启动 bandit、微电网 RL、金融 AI×LLM). 聚焦工业 ML, 不宽 (软 LLM/RL 过滤仍有助).
- **reka** `free` `explicit-only` `stream`: Reka AI 论文流 (经 OpenAlex 'Reka AI'=6 精确; 裸 'Reka' 禁用: =151 几乎全为马来/印尼 'Reka'=设计 院系, 零 AI 公司). 精确但低产+陈旧 (最新 2025-01, 多为 2022-2023, Reka 已转产品/API). 作低频 tripwire 监控, 不会误报; 切勿换裸 'Reka' 凑数. 不宽.
- **researcher_watch** `free` `stream`: Researcher watch: newest papers from tracked PIs via OpenAlex (default: 10 SG/Canada/HK ML faculty; customize via ~/.omniseek/credentials/researcher_watch.json). Postdoc/collab upstream signal.
- **s2_snippet** `free` `explicit-only` `lookup`: S2 段落级全文检索 (/graph/v1/snippet/search): 跨 Semantic Scholar 开放获取全文语料, 检索匹配查询的具体段落/句子 (不止论文/摘要级). 回答 '哪些论文里的哪些句子在讲 X'. 补 semantic_scholar/openalex (论文级) 与 omniseek_paper_enrich (单篇全文) 的中间层: passage 级. 英文 CS/AI 语料. 命名钻取 (omniseek_search 单源 raw).
- **sakana** `free` `explicit-only` `stream`: Sakana AI 近期论文流 (经 OpenAlex 'Sakana AI'=16 完全精确, 造词名零误中). 东京小 lab (David Ha/Llion Jones/Takuya Akiba), 量小即真实总量, 正是 org_watch 的价值. 核心 on-topic: AI-Scientist 端到端自动化、Darwin Gödel Machine 自改进 agent、模型融合、LLM agent 合谋. 单串足够, 不宽.
- **salesforce_research** `free` `explicit-only` `stream`: Salesforce Research 近期论文流 (经 OpenAlex 'Salesforce Research'=385 为主 + 'Salesforce AI Research'=131 为 LLM-纯子集, 均干净短语匹配). '...Research' 限定避开产品/营销裸提及. 近期流以 LLM/RL/agent/multimodal 为主 (GUI grounding、多模态 deep-search agent、text-to-viz 多目标 RL); 含 Singapore (Salesforce Research Asia) 节点, 对 PhD 的 SG 路线相关. 不算宽 ...
- **scale_ai** `free` `explicit-only` `stream`: Scale AI 近期论文流 (经 OpenAlex 'Scale AI'=63 精确隔离 SF eval/数据公司, raw_affiliation 含 'Scale AI, Inc., San Francisco, CA' 等; 双词短语足够消歧, 少数 topic-cooccurrence false positive). on-topic: LLM benchmark (专家级题库、核决策 benchmark)、安全 alignment、对抗/agent eval. 不宽.
- **sea_ai_lab** `free` `explicit-only` `stream`: Sea AI Lab (SAIL): Sea 集团新加坡工业 AI 研究院最新论文 (OpenAlex affiliation 搜 Sea AI Lab, keyless, 无 CDP/无作者消歧). LLM/RL/reasoning/AI-systems, 正中 SG 落地与方向; 无 OpenAlex 机构实体且官网 SPA, 故走 affiliation 文本搜.
- **semantic_scholar** `free` `lookup`: Semantic Scholar: 225M+ papers, citation graphs, TLDR summaries
- **servicenow_research** `free` `explicit-only` `stream`: ServiceNow Research 近期论文流 (经 OpenAlex 'ServiceNow Research'=103 精确自隔离, raw_affiliation 含 'ServiceNow Research, Montreal/Santa Clara'; '...Research' 限定避开 SaaS 产品裸提及). on-topic: LLM/agent/RL/reasoning (agent 后门、LLM 性能预测、goal-conditioned RL、多模态 abstention). 紧贴 PhD 目标的 Mila/蒙特利尔 ML 生态. 主题溢出小, 不宽.
- **shanghai_ai_lab** `free` `explicit-only` `stream`: 上海人工智能实验室 (Shanghai AI Lab) 近期论文,经 OpenAlex 引号短语对 "Shanghai AI Laboratory"(1212)+"Shanghai Artificial Intelligence Laboratory"(1836,两写法仅重叠 23 篇,互补 union ~3025)精确隔离。重度 LLM/RL/MLLM/agent on-topic:LLM 训练 survey、置信度作奖励推进 LLM 推理、Human-LLM 协写、Minecraft 多 agent。对 CN ML PhD 是头部 prolific 实验室风向标。precision 强制: ...
- **slideslive_talks** `free` `lookup`: SlidesLive: recorded conference talks (NeurIPS/ICML/ICLR/ACL) absent from YouTube and untranscribed. Keyword-search the SlidesLive library for talks, or pass a talk URL / numeric id to resolve its audio for TRANSCRIBE (local ASR)
- **stability** `free` `explicit-only` `stream`: Stability AI 近期论文流 (经 OpenAlex 'Stability AI'=49 精确隔离, 抽样 raw_affiliation 含 'Stability AI, <city>', 无通用误中). 生成式 AI lab (diffusion 图/视频/3D/音频、音频 LLM、多模态), 部分 RAG/LLM 重叠. 集中于生成媒体/ML, 不宽, 单串可用.
- **stepfun** `free` `explicit-only` `stream`: 阶跃星辰 (StepFun) 近期论文,经 OpenAlex bare 'StepFun'(34 篇)精确隔离 (名字独特、零 off-target 碰撞),authorship 真带 'StepFun'/'StepFun Inc'/'Stepfun, Shanghai, China'。全部前沿 AI:自动形式化 LLM、code agent (GitTaskBench)、视频扩散 (Sparse-vDiT)、多模态 LLM 训练 (DIP)、MLLM 幻觉缓解。对 CN ML PhD on-topic 且来源干净。'StepFun Inc' 为子集冗余安全网;hint 'StepStar' ...
- **tencent_hunyuan** `free` `explicit-only` `stream`: 腾讯混元 (Hunyuan) 实验室近期论文,经 OpenAlex 短语对 "Tencent Hunyuan"(33)+"Hunyuan Team"(6,捕写作 'Hunyuan Team, Tencent' 的 agent/RL 论文)精确隔离 (~39 篇,100% 腾讯零 off-target)。强 on-topic LLM/RL/reasoning/agent:强化微调熵极性、技能图任务合成 (agent)、On-Policy 上下文扩展、混合奖励 RL、LLM 权重量化。precision:必须用两短语,勿用 bare 'Hunyuan'(误捕山西浑源县医院/CDC)或 'Tence ...
- **together_ai** `free` `explicit-only` `stream`: Together AI 近期论文流 (经 OpenAlex 'Together AI'=4 精确, 尾缀 'AI' 消歧, raw_affiliation 均为公司真实地址; 裸 'Together' 会误中, 禁用). on-topic (RAG/LLM benchmark). 量极少 (主产出 RedPajama/FlashAttention 在 arXiv 未一致索引此短语), 精确但稀疏, 作低频监控.
- **transformer_circuits** `free`: Transformer Circuits Thread: Anthropic mechanistic interpretability research
- **underline_talks** `free` `lookup`: Underline: recorded conference talks + video for the ACL-family venues (EMNLP/NAACL/EACL/AACL, also SIGIR/KDD/AAAI) that SlidesLive does not host; pass an underline.io/lecture URL or numeric id to resolve its abstract and public video for TRANSCRIBE (no public keyword search: that is a follow-up)
- **vector_institute** `free` `explicit-only` `stream`: Vector Institute (多伦多) 近期论文流 (经 OpenAlex 全名短语 'Vector Institute for Artificial Intelligence'=1051 精确隔离; 裸 'Vector Institute'=2688 噪声更高, 用全名串). 对 PhD 的 Canada 路线是落地目标, 托管核心 ML/LLM/RL faculty. 宽口径机构, 浏览需带主题词查 (即使精确串仍重偏化学/生医/药物发现 ML, 需主题词捞 LLM/RL/agent).
- **yi_01ai** `free` `explicit-only` `stream`: 零一万物 (01.AI / Yi,李开复创办) 近期论文,经 OpenAlex 引号短语 "01.AI, Beijing"(count=7,全部 7 篇真带 '01.AI, Beijing, China')精确隔离。on-topic ML/LLM:LLM 情绪控制、LLM 引导故事可视化、多模态几何题求解、人体动作视频生成。体量偏小 (7,因 01.AI 2024 后收缩) 但 latest 到 2026-05,仍可监控。precision:bare '01.AI' 在点号上 token-split 过匹配 (raw 99 含无关 EU/医学);必须用 Beijing 锚定引号短语,可选加 S ...
- **zenodo** `free` `lookup`: Zenodo: open research repository (papers, datasets, software, theses) with minted DOIs (keyless REST API)
- **zhipu** `free` `explicit-only` `stream`: 智谱 AI (Zhipu / GLM) 近期论文,经 OpenAlex "Zhipu AI"(meta.count=34)+点号变体 "Zhipu.AI" 精确隔离,authorship 真带 'Zhipu AI, Beijing, China' 等。高度 on-topic (GLM 系 LLM、视频生成、RL/alignment、agent):VPO 文生视频对齐、LVBench 长视频理解、Concat-ID 身份保持视频。对 CN ML PhD 是 GLM 生态风向标。precision:勿用 bare 'Zhipu'(过匹配 'Beijing Zhipu Medical Laborat ...

## community (27)

- **academia_se** `free` `lookup`: Academia Stack Exchange: English PhD/postdoc/faculty Q&A (Stack Exchange API)
- **ai_se** `free` `lookup`: Artificial Intelligence Stack Exchange: AI-concepts Q&A (NN architectures, RL, search/planning, the theory + intuition behind AI techniques); conceptual, not applied pipelines
- **crossvalidated** `free` `lookup`: Cross Validated (stats.stackexchange): statistics/ML/data-analysis Q&A; the canonical English methodology site (model selection, bias-variance, estimators, tests)
- **cs_se** `free` `lookup`: Computer Science Stack Exchange: CS-theory Q&A (algorithms, complexity, computability, automata, the math behind ML); not Stack Overflow's programming/debugging
- **datascience_se** `free` `lookup`: Data Science Stack Exchange: applied ML/data-science Q&A (architecture choices, feature engineering, training/eval gotchas, imbalanced data, NLP/CV pipelines)
- **discord_communities** `walled` `bring-your-own-login` `explicit-only` `lookup`: Discord 研究/peer/求职 频道 (REST bot): 仅能读 部署者有管理员权限、已邀请 bot 的 server (大社区加不进 bot, 走其他源). 配 ~/.omniseek/credentials/discord.json + 开 Message Content Intent
- **discourse_forums** `free` `lookup`: Discourse ML forums (Hugging Face / PyTorch / fast.ai): practitioner Q&A threads on transformers, training, CUDA, fine-tuning, as STRUCTURED docs with sortable engagement fields (reply/like/post counts) + verbatim threads web search does not rank (keyless API)
- **douban_groups** `walled` `explicit-only` `lookup`: 豆瓣小组: China grassroots community graph for overseas-study/immigration/diaspora life (groups + discussion threads via m.douban.com rexxar, logged-in 9222 CDP)
- **gter** `free` `explicit-only` `stream`: 寄托天下 gter: 中文留学申请/海外生活主社区, 按地区分区 (CDP 渲染: f.gter.net 是 JS 壳, 2026-06-10 实证). 盯 新加坡/加拿大/香港 三区的新帖 (申请经验/签证/录取汇报). 与 yipinsanfendi(北美 CS 重)互补的泛留学层. explicit_only
- **hackernews** `free` `lookup`: Hacker News: tech news (story submissions) + threaded community discussion (full-text comment search), both via the Algolia HN API
- **hardwarezone** `free` `explicit-only` `proxy`: HardwareZone/EDMW 经搜索索引 (snippet): 新加坡本地最大论坛, 本地人视角的 EP/外籍人才/薪资/公司/移民讨论 (与 Blind/脉脉的'圈内人'视角互补的'本地人'地面真相). explicit_only (点名即达). 全文: 本站封数据中心 IP、直抓不到 → 用 exa 源 site:hardwarezone.com.sg 借 Exa 爬虫取全文
- **juejin** `free` `explicit-only` `lookup`: 掘金 Juejin: Chinese developer-article search (keyless). query → an engagement-ranked feed of CN dev深度 articles: title / brief / author / 赞(digg) / 看(views). OmniSeek's source for Chinese dev/tech writing (前端/后端/AI工程/架构) that web search can't rank or structure. No login.
- **lobsters** `free` `stream`: Lobste.rs: invite-only tech link aggregator (smaller than HN, higher SNR; ai/ml tag feeds curated by mods)
- **quora** `free` `explicit-only` `proxy`: Quora 经搜索索引: 英文 Q&A: 移民/签证(EP/PR/Express Entry)、读博、公司文化、城市/offer 对比. 与中文源(脉脉/一亩三分地/知乎)互补的英文视角. explicit_only (点名即达)
- **reddit** `free` `lookup`: Reddit: GENERAL topic search (via Arctic Shift mirror; Reddit's own API is WAF-blocked). 自动按查询路由到对应话题子版 (如 'pour over coffee'→r/Coffee; 含金融信号如 '$NVDA earnings' → 追加 r/stocks·investing·wallstreetbets 等), 同时常驻搜索 r/PhD·AskAcademia·MachineLearning + 移民/求职 核心子版 (科研/职业意图永不丢失). 查询语义=全词 AND、无 OR: 1-3 个词且含一个 ...
- **sogou_weixin** `free` `explicit-only` `lookup`: 微信公众号文章关键词搜索 (Sogou Weixin): the only free keyword index over WeChat 公众号 articles, which Google does NOT crawl. Reach for Chinese first-hand / specialist 公众号 writing (industry/行业号, 学院官方号, niche expertise, 经验贴) on a topic. Returns title + snippet + 公众号 name + date + a permanent mp.weixin.qq.com link.
- **stackoverflow** `free` `lookup`: Stack Overflow: programming Q&A (parallel to academia_se; primary destination for pytorch/CUDA/JAX/data-preprocessing implementation issues)
- **tieba** `free` `lookup`: Baidu Tieba (百度贴吧): China's largest topical BBS; CN-community threads + forum structure via the keyless mobile JSON endpoints
- **v2ex** `free` `lookup`: V2EX (www.v2ex.com): the CN tech / 润学 forum; 职场 / 求职 / 远程工作 / 移民 / 海外留学 community threads via the keyless topics JSON
- **xiaohongshu** `walled` `explicit-only` `lookup`: 小红书: first-hand PhD daily life + real experience sharing (CDP session)
- **xiaohongshu_search** `free` `explicit-only` `proxy`: 小红书 经搜索索引 (Brave site: → 笔记 snippet; 永不用账号/CDP 碰站 → 零设备风险, 与封印的 CDP xiaohongshu 完全隔离, ToS 干净): 留学/读博/求职/城市/公司体验的海量一手笔记 (实测出字节面经/HKPFS 攻略). url_filter 只留真实笔记(discovery/item·explore), 滤掉 pro./job. 导航页. explicit_only (点名即达)
- **xiaomuchong** `free` `explicit-only` `lookup`: 小木虫: China's oldest PhD/master's academic forum (since 2001, 5M users)
- **yipin_search** `free` `explicit-only` `proxy`: 一亩三分地 经搜索索引 (keyless-via-Brave, CDP yipinsanfendi 的稳健 fallback): 北美/海外 CS PhD 申请+毕业去向+签证核心中文社区. CDP 防灌水间隔挂时此路仍通. explicit_only (点名即达)
- **yipinsanfendi** `walled` `explicit-only` `lookup`: 一亩三分地: North America CS PhD application + grad school community
- **zhihu** `walled` `explicit-only` `lookup`: 知乎: long-form PhD methodology discussions (via CDP Chrome session)
- **zhihu_search** `free` `explicit-only` `proxy`: 知乎 经搜索索引 (keyless-via-Brave 稳健补充, 非 CDP): 中文海量一手经验: 读博/求职/公司内幕/签证/城市对比. 与 CDP zhihu 互补 (CDP 取登录全文, 此取引擎索引快照, CDP 挂时仍通). explicit_only (点名即达)
- **zhihu_users** `walled` `explicit-only` `stream`: 知乎 followed researchers: 张俊林 等 senior 中文 NLP/ML 作者跟踪 (via CDP, configurable via ~/.omniseek/credentials/zhihu_users.json)

## jobs (13)

- **academic_job_boards** `free` `stream`: 学术职位板 (scrape, 实证 2026-06-10: 无 RSS, 静态列表可取): jobs.ac.uk (英国/全球学术岗, ML+CS 关键词搜索页, 47 条干净条目验证). 开放性准则: 学术 track 永久在册, 不随职业分支砍. (AJO 已试: 通用 scraper 抽出的是表格代码碎片, 职位标题不在锚文本内 → 需小型专用解析器, 在 backlog.) 补 academic_jobs(Nature Careers) / higheredjobs_cs / jobrxiv_canada
- **academic_jobs** `free` `stream`: Academic faculty / postdoc / staff scientist job RSS: Nature Careers per-country (Singapore / Canada / Hong Kong + UK / US / Australia backup). HK PhD → overseas placement signal.
- **adzuna** `keyed` `bring-your-own-login` `explicit-only` `lookup`: Adzuna: multi-country job listings with employer SALARY ranges (salary_min/max + company / location / contract); name it to drill a job market. Query = optional 2-letter country code + role, e.g. 'ca machine learning' / 'sg data scientist' (defaults to Canada). STRUCTURE, keyed.
- **ajo** `free` `stream`: AcademicJobsOnline (AJO): 北美教职/博后申请主板的最新职位列表 (专用解析器: 真标题在 span#j{ID}, 机构在前置 h3; 通用 scraper 在此只会抽出代码碎片). 开放性准则: 学术 track 永久在册. 补 academic_job_boards(jobs.ac.uk) / higheredjobs_cs / academic_jobs(Nature Careers)
- **bytedance_seed** `walled` `explicit-only` `stream`: 字节跳动 Top Seed 校招 + 实习: 大模型 / 前沿技术 PhD 人才招聘 (httpx 直连 jobs.bytedance.com JSON API, 无需 CDP/auth/sign)
- **feishu_jobs** `walled` `explicit-only` `stream`: Feishu 招聘: 6 个 Tier 1 大模型 startup (MiniMax/智谱/01.AI/生数/无问芯穹/百川), 549+ 活跃岗位; 与 mokahr_ats + bytedance_seed 互补
- **higheredjobs_cs** `free` `explicit-only` `stream`: HigherEdJobs CS/IT: 美国/北美 高校 CS·ML 教职 + 博后 + 研究岗 RSS (category 102). 学术 track 落地板, 补 academic_jobs(Nature Careers per-country)
- **jobrxiv_canada** `free` `stream`: jobRxiv (加拿大): 学术研究岗位板 (博后/研究科学家/教职), 按地区过滤加拿大. 补 academic_jobs/vector_talent_hub 的加拿大研究岗覆盖
- **layoffs_tracker** `free` `explicit-only` `stream`: 科技裁员追踪: layoffs.fyi (Roger Lee 的 Airtable, ~4500 起裁员事件, 2020 至今: 公司/总部/裁员人数/占比/日期/行业/融资阶段/累计融资/国家/新闻源). STRUCTURE: 空 query=按日期倒序; 关键词过滤公司/行业/国家/阶段 ('google' / 'crypto' / 'india' / 'Series C'). MONITOR: 新裁员事件=watchtower 新条目. Data by layoffs.fyi (attribution required)
- **mycareersfuture** `free` `lookup`: MyCareersFuture: 新加坡政府求职板 (开放 API, **强制薪资范围** + 公司 UEN). SG 全境岗位 (本地/MNC/政府), 自由文本查询; 新加坡求职主板
- **overseas_ai_jobs** `free` `stream`: 海外工业界 AI lab 全职研究岗 (RS/RE/MTS): Cohere / DeepMind / Reka / Mistral / Anthropic / xAI / Together / Scale + 更多, 跨 Greenhouse/Ashby/Lever/SmartRecruiters/Workable; 标注 Singapore/Canada/remote (按部署方配置的目标地区)
- **remotive** `free` `explicit-only` `lookup`: Remotive: curated REMOTE job board (category taxonomy, candidate-required-location, job type, salary, tags) via the keyless API; name it to drill remote AI/ML roles. STRUCTURE, keyless. Rate-limited (a few calls/day): a low-frequency curated drill, not a hot path.
- **vector_talent_hub** `free` `stream`: Vector Institute Talent Hub: 加拿大顶尖 AI 研究所策展的 AI 职位板 (research + industry, 多伦多/加拿大). 落地加拿大主信号; 与 overseas_ai_jobs(跨 lab ATS)/academic_jobs(Nature 教职) 互补

## news (13)

- **academic_ai_labs** `free` `stream`: Top academic AI lab blogs: Stanford CRFM / Berkeley BAIR / MIT CSAIL. Methodology-heavy counterpart to frontier_labs (industry).
- **ai_newsletters** `free` `stream`: AI newsletter / long-form recap matrix: The Sequence / Last Week in AI / The Gradient / Nicholas Carlini. Periodical ML research + security digest.
- **canada_ai_research** `free` `stream`: 加拿大 AI 研究所 + tech ecosystem: Vector / CIFAR / UWaterloo CS / Layer 6 / BetaKit / Globe and Mail Tech / Mila / UofT Schwartz Reisman (Amii 经 scrape_js_sites; IVADO 经 ivado_news 源)
- **frontier_labs** `free` `stream`: Frontier AI research labs: Anthropic / DeepMind / Hugging Face / OpenAI / Mistral / Meta FAIR / Microsoft Research / MSRA / Google Research / Apple ML / Ai2 / Sakana / EleutherAI / Character.AI / PyTorch
- **gov_policy** `free` `explicit-only` `lookup`: 中国政府网 政策文件库 (gov.cn): the AUTHORITATIVE 国务院/国办 policy-document corpus (法规/条例/通知/国令), keyword search with each hit linking the FULL policy text on gov.cn (omniseek_read it). Google can't return this faceted policy index keyed to 文号 + issuing-org + date. No login. Reach for Chinese central-government ...
- **hk_career_research** `free` `stream`: Hong Kong AI / career / 媒体信号: SCMP News + HKFP + ASTRI (R&D 院所) + info.gov.hk (SenseTime 经 scrape_js_sites). HK PhD 出海+本地路径专用
- **hk_universities** `free` `stream`: HK 5 大学 CS/CSE/COMP dept news: HKU CS / HKUST CSE / CUHK CSE / CityU CS / PolyU COMP (HTML scrape，HK CS 院系动态信号)
- **scrape_canada** `free` `explicit-only` `stream`: 加拿大工业界 AI lab (scrape, 无 RSS): RBC Borealis research blog 博文 (加拿大最大工业 AI 实验室, Toronto/Edmonton, 落地招聘目标). 论文已由 rbc_borealis 覆盖,这里补 blog 长文
- **scrape_hongkong** `free` `explicit-only` `stream`: 香港创新生态 (scrape, 无 RSS): HKSTP 科技园 news & events (AI co-incubation / launchpad, 本地招 ML 的深科技公司). 补 hk_career_research
- **scrape_js_sites** `free` `explicit-only` `stream`: JS 渲染源 (explicit_only, 经共享 CDP Chrome 渲染, 较慢, 显式点名才查): Amii (加拿大 AI 研究所) + NTU CCDS (新加坡 CS 院系新闻) + ITIB (香港创新科技及工业局) + Apollo Research (前沿评测/AI safety) + SenseTime 商汤. 这些站初始 HTML 是空壳, 内容靠运行时 JS, 故必须渲染
- **scrape_singapore** `free` `stream`: 新加坡 AI 机构 + 移民 (scrape, 无 RSS): SEA-LION (AISG 开源 SEA LLM 产品/发布) + A*STAR press releases + SMU SCIS news + Fragomen-SG 工准证/EP 解读 + MOM newsroom (官方 press releases: EP/COMPASS/工准证政策一手, 实证 2026-06-10 静态可取). 补 singapore_ai_research / sg_immigration
- **singapore_ai_research** `free` `stream`: 新加坡 AI 学术 + tech ecosystem: NUS News + AI Singapore + HardwareZone + Grab Eng + Vulcan Post + Tech in Asia (A*STAR/SMU/SEA-LION 经 scrape_singapore; NTU 经 scrape_js_sites)
- **wayback** `free` `explicit-only` `lookup`: Wayback Machine 时光机: 一个 URL 的历史/被删快照 (keyless, Internet Archive CDX). query = 一个 URL → 该页的存档快照列表(时间戳 + web.archive.org 存档链接,再 omniseek_read 读历史正文). web 搜只给 LIVE 页;这取开放网已遗忘/已删改的旧版本(对抗检索、读历史、读被删)。命名查询;非 URL 返空.

## funding (11)

- **ai_residencies** `free` `stream`: AI research residency / fellows / scholars programs: Anthropic Fellows / MATS / Cohere Scholars + Catalyst Grants / NVIDIA / OpenAI Safety + Residency / Constellation Astra / EleutherAI SOAR / Ai2 PYI / 上海 AI Lab / Mistral Intern / Vector Institute (Tier 1-3; config rows in ai_residencies.json, addi ...
- **cihr_grants** `free` `explicit-only` `lookup`: 加拿大 CIHR 健康研究经费: 临床 AI/健康 NLP 切片 (加拿大三大联邦研究局之三: NSERC 理工、SSHRC 人文、CIHR 健康). NLP/ML 在健康这边以 临床 NLP/医学机器学习/健康数据科学/EHR 预测模型 形式存在, 是另两局不覆盖的. 开放数据为逐年 bulk XLSX (FY2025-26 ~12MB, 无查询 API; openpyxl 解析). 逐笔奖助: 获奖人 + 机构/系 + 金额(CAD) + program + 主题/类别 + 标题/摘要/关键词. 仅收 AI/ML/NLP 相关切片. 命名钻取 (omniseek_search 单源 raw ...
- **cordis_eu** `free` `explicit-only` `lookup`: 欧盟 CORDIS 科研经费: Horizon Europe (2021-2027) AI/ML/NLP 切片 (眼首个欧盟经费源, 此前有美国 NSF/NIH + 加拿大 NSERC/SSHRC/CIHR, 无欧盟). CORDIS 是欧委会科研成果服务, Horizon Europe 项目数据仅以逐月 bulk zip 发布 (CSV/JSON, 无查询 API; CSV zip ~35MB, project.csv ~2.25 万项目 + organization.csv 参与机构含协调方). 逐项目: 标题 + 目标摘要 + 关键词 + EC 出资(EUR) + 协调机构 + 国别 + ...
- **fellowships** `free` `stream`: 海外 PhD/postdoc 资助与 Fellowship: Vector/CIFAR/IVADO(加) + AISG/NRF/SINGA(新) + Google/Apple/NVIDIA/Meta/MS PhD Fellowship + Schmidt/OpenPhil/LTFF(全球 AI). 国际开放 + HK→SG/加拿大路径; RSS 自动 + 静态链接人工核窗口
- **grants_gov** `free` `explicit-only` `lookup`: Grants.gov: OPEN + forecasted US federal funding OPPORTUNITIES you can apply to, across all ~26 grant-making agencies (NSF / DOE / DARPA / NIH / ...); name it to find applyable funding by topic. The prospective complement to nsf_awards / nih_reporter (which show awards already granted). STRUCTURE, k ...
- **nih_reporter** `free` `explicit-only` `lookup`: NIH RePORTER: US NIH biomedical research grants (contact PI / award amount / awardee organization / full abstract + terms); name it to drill US federal biomedical funding by topic / PI / organization. STRUCTURE, keyless POST api.reporter.nih.gov; the biomedical sibling of nsf_awards.
- **nserc_awards** `free` `explicit-only` `lookup`: 加拿大 NSERC 科研经费: 计算机/AI/ML/NLP 切片 (眼首个加拿大经费源, 此前只有美国 NSF/NIH). NSERC 是加拿大主科学资助局, 开放数据仅以逐年 bulk CSV 发布 (无查询 API, FY2024 ~56MB/~6 万行). 逐笔奖助: 获奖人 + 机构 + 金额 (CAD) + program + 学科 + 关键词. 博士赴加找实验室/PI/资助方向的一手结构 (网搜给不出). 仅收 CS 学科 + AI/ML/NLP 关键词的子集 (~3-4k 行, telos 视角, 非全 NSERC). 命名钻取 (omniseek_search 单源 raw).
- **nsf_awards** `free` `explicit-only` `lookup`: NSF Award Search: US National Science Foundation research grants (PI / award amount / awardee institution / full abstract); name it to drill US federal funding by topic / institution / PI. STRUCTURE, keyless api.nsf.gov; fills the grants/funding gap.
- **nsfc_awards** `free` `explicit-only` `lookup`: 国家自然科学基金 NSFC 已批准项目检索: 中国基础科研主资助局 (美国 NSF 的对位), 眼首个中国经费源 (此前只有美国 NSF/NIH + 加拿大 NSERC/SSHRC/CIHR). 官方门户 kd.nsfc.cn 有验证码 + 加密响应不可脚本化, 故走 LetPub 第三方基金索引 (letpub.com.cn) 的免登录切片. 关键词 (题目) 搜索, 逐笔奖助: 负责人 + 单位 + 金额 (万元) + 项目批准号 + 项目类型 + 学部 + 批准年份 + 题目. 博士/研究者查某课题 (NLP/ML/视觉) 谁在中国拿了基金、在哪家机构、多少钱: 一手经费格局 (网搜给不 ...
- **sshrc_awards** `free` `explicit-only` `lookup`: 加拿大 SSHRC 人文社科经费: 计算语言学/NLP/数字人文 切片 (加拿大三大联邦研究局之二: NSERC 理工、SSHRC 人文社科、CIHR 健康). NLP 在人文社科这边以 计算语言学/语言技术/数字人文/语料文本/计算社科 形式存在, 是 NSERC(理工) 不覆盖的一块. 开放数据为逐年 Payments bulk CSV (FY2024 ~6.9MB, 无查询 API). 逐笔奖助: 获奖人 + 机构 + 金额(CAD) + program + 学科/方向 + 关键词. 仅收 AI/ML/NLP 相关切片. 命名钻取 (omniseek_search 单源 raw).
- **ukri_gtr** `free` `explicit-only` `lookup`: 英国 UKRI Gateway to Research (GtR) - 英国研究理事会已批科研经费 (眼首个英国经费源, 此前有美国 NSF/NIH/grants_gov 与加拿大 NSERC/SSHRC/CIHR). 覆盖 EPSRC/BBSRC/ESRC/MRC/AHRC/NERC/STFC/Innovate UK. 逐笔项目: 标题 + 摘要 + 资助方 (leadFunder) + grant category + 研究学科/主题 + 参与机构与金额 (GBP). 博士赴英找实验室/资助方向/机构的一手结构 (网搜给不出). Keyless JSON API. STRUCTURE, 命 ...

## immigration (10)

- **aaip_draws** `free` `explicit-only` `stream`: 阿尔伯塔省提名 AAIP 抽签历史: Alberta Advantage Immigration Program 的 'Draw information' 表: 逐次抽签日期 + Worker stream/pathway (Alberta Opportunity / Rural Renewal / Tourism / Dedicated Health Care / Alberta Express Entry 各 priority sector 等) + 最低分 + 邀请数. 省提名自有分, 非联邦 CRS. 命名钻取 (omniseek_search 单源 raw).
- **bcpnp_invitations** `free` `explicit-only` `stream`: BC 省提名 BCPNP 抽签: Skills Immigration (技术移民, 按 ITA type + 分数线 SIRS + 邀请数) 与 Entrepreneur Immigration 的逐次抽签, 外加 registration pool 的 SIRS 分数分布快照. BC 用 SIRS (0-200 注册分), 不是联邦 CRS, 别混. 分数/人数可能是 'N/A' 或 '<5' 字符串. 命名钻取 (omniseek_search 单源 raw).
- **canada_immigration** `free` `stream`: 加拿大移民信号: IRCC 官方 + CIC Times + immigration.ca + Moving2Canada + Express Entry (每周 EE draws / Tech Talent / GTS / PNP 重组 / STEM 优先策略)
- **datagovsg_nonresident_pass_types** `free` `explicit-only` `lookup`: 新加坡非居民人口按准证类型占比 (data.gov.sg DataStore 官方统计): Work Permit / S Pass / Employment Pass / 家属准证 / 学生准证 等占非居民人口的逐年百分比序列. SG 移民/外籍劳动力结构一手官方数据 (网搜给不出可解析的时间序列). 新加坡移民问题的决策语境: 准证构成 = decision-context. 命名钻取 (omniseek_search 单源 raw).
- **ircc_ee_rounds** `free` `explicit-only` `stream`: IRCC Express Entry 抽签轮 (加拿大官方 JSON, 全史 400+ 轮, 经 CDP 真浏览器取): 每轮的类别 / CRS 分数线 / 邀请数 / 池内 CRS 分布, 结构化可查 + watchtower 盯新轮. 加拿大 Express Entry 抽签的核心决策信号. 空 query=最近各轮; 关键词过滤类别 (CEC / French / STEM / PNP...)
- **ircc_processing_times** `free` `explicit-only` `lookup`: IRCC 处理时长表 (加拿大官方 JSON, 经 CDP 真浏览器取): 每种申请类型 x 每个国家的当前处理时长估计, 结构化可查, 即官方 Check processing times 工具背后的数据. 覆盖境外服务 (访客 / 学签 / 工签 / 超级签证 / 团聚 / 难民) 与境内服务 (延期 / PR 卡 / 公民 / eTA / IEC...). 例: 'study permit China' / 'work permit India' / 'PR card'. 空 query=各路由一批; 关键词按申请类型 + 国家过滤.
- **mpnp_draws** `free` `explicit-only` `stream`: 曼省提名 MPNP EOI 抽签历史 - Manitoba Provincial Nominee Program 的 Expression of Interest 逐次抽签: 抽签号 + 日期 + stream (Skilled Worker in Manitoba / Skilled Worker Overseas / 职业定向 occupation-specific / 战略招募 strategic recruitment) + 发出的 Letters of Advice to Apply (LAA) 总数与按招募渠道 (Employer Services / Francophone / ...
- **oinp_invitations** `free` `explicit-only` `stream`: 安省提名 OINP 抽签历史: Ontario Immigrant Nominee Program 各 stream (雇主担保/硕士毕业生/博士毕业生/企业家等) 的逐次抽签: 日期 + 邀请数 + 分数线 (Score range) + EOI 窗口 + 备注. 省提名, 用 OINP 自有分数, 别与联邦 EE (ircc_ee_rounds, CRS) 混. 博士/硕士 stream 2026-05-30 改版后已停, 旧行为历史参考; 页面继续发新 stream 抽签, 故仍是活的 monitor. 命名钻取 (omniseek_search 单源 raw).
- **page_watch** `free` `explicit-only` `stream`: 规则页变更哨兵: 盯无 feed 但变了就重要的政策/规则页 (MOM EP/COMPASS 资格、ONE Pass 标准、ICA PR 申请). 每页一个文档, source_id 内嵌内容指纹: 页面一变, 指纹即变, watchtower 视为新条目自动报. 加一页 = page_watch.json 加一行. (指纹可能因页面动态碎片偶发翻动, 故盯哨先 passive 观察)
- **sg_immigration** `free` `explicit-only` `stream`: 新加坡移民/工准证落地信号: Immigration@SG (IASG 独立咨询: EP/PR 路径 + MOM/EDB 解读) + Singapore Expats Forum (申请人真实 EP/PR 审批时间线/COMPASS 结果). 政策解读+地面实情两层 (官方 MOM newsroom 无有效 RSS; Fragomen 经 scrape_singapore)

## code (6)

- **context7** `free` `explicit-only` `lookup`: Context7 (Upstash) live library-docs registry. Query a library / framework / SDK name -> ranked registry records: Context7 ID (/org/project, also the docs-fetch key), description, trustScore (0-10), benchmark quality score (0-100), code-snippet + token counts, pinned doc versions, lastUpdateDate. ST ...
- **github** `keyed` `bring-your-own-login` `lookup`: GitHub platform: code search + issues/PRs + discussions, plus org/user newest-repo activity (query `org:NAME` / `user:NAME`) and repo file-tree browse (`tree:owner/repo` / `tree:owner/repo@branch`). Complements github_trending (repo discovery) + github_releases (infra release feeds).
- **github_releases** `free` `stream`: GitHub release events for ML infra stack: vLLM / transformers / SGLang / llama.cpp / PyTorch / Unsloth / OpenCompass. Engineering delivery stream (complement to github_trending).
- **github_trending** `free` `stream`: GitHub Trending: recently active ML/AI repos by stars (via GitHub Search API; complement to github_awesome_phd curated lists)
- **pypi** `free` `stream`: PyPI: Python package update stream (recent releases across all packages)
- **rl_llm_frameworks** `free` `stream`: RL-on-LLM 训练框架发布流: OpenRLHF / verl(volcengine) / TRL(HuggingFace) / LLaMA-Factory. RL/reasoning 方向的工程交付层; github_releases(推理栈 vLLM/SGLang/...)未覆盖的训练框架补充

## finance (5)

- **cninfo** `free` `explicit-only` `lookup`: 巨潮资讯网 CNINFO: the SSE/SZSE OFFICIAL A-share disclosure repository (the Chinese EDGAR). Keyword full-text search over every listed-company filing (年报/季报/招股书/ad-hoc 公告), each hit a DIRECT PDF link (static.cninfo.com.cn): pair with omniseek_read for the body. Google can't return this structured filing ...
- **eastmoney** `free` `explicit-only` `lookup`: A股 / 港股 / 美股 实时(延迟)行情 (keyless, no login). query = a stock NAME or CODE (贵州茅台 / 600519 / 0700.HK / AAPL) → structured quote: price / 涨跌 / 今开高低昨收 / 总市值 / 市盈率. The Chinese analog of market_quote (US-only). Name resolution via EastMoney suggest; quote numbers via Tencent qt.gtimg.cn (burst-tolerant, af ...
- **market_crypto** `free` `explicit-only` `lookup`: 加密现货行情: BTC/ETH 等币种的实时(秒级)报价 (keyless, CoinGecko 后端). query 点名币种 (BTC / ETH / $SOL / bitcoin) → 每币一条: 美元现价 / 24h 涨跌% / 市值 / 24h 成交额. 多币一次. 命名查询, 不进广搜; 主流币静态映射、未知大写符号不臆测 → query 无币种或无法解析返空.
- **market_quote** `free` `explicit-only` `lookup`: 美股行情: 实时(延迟)股票报价 (keyless, CNBC quote 后端). query 里点名 ticker ($NVDA / ORCL) → 每个 ticker 一条: 现价/涨跌/涨跌幅/成交量/市值/PE/EPS/股息/日内区间/52周区间 + 盘前盘后. 多 symbol 一次. 命名查询, 不进广搜; query 无 ticker 返空.
- **sec_financials** `free` `explicit-only` `lookup`: SEC 结构化财务: 公司官方基本面 + 最新备案 (keyless, data.sec.gov XBRL). query 给 ticker 或公司名 → 一条: 最新营收/净利/总资产/股东权益/递延收入 (us-gaap XBRL) + 最近 10 条备案 (form/日期/直达链接). 命名查询, 不进广搜. web search 给不了干净的结构化数字.

## general (5)

- **cdp_fulltext** `walled` `explicit-only` `portal`: Full-text via the CDP real browser for venues that wall/403 headless but render for a real browser (Quora, Blind/teamblind, Glassdoor, 脉脉/maimai, LinkedIn public posts, X/Twitter profiles+tweets). Discover via the matching search-index source, then omniseek_read the URL here to turn a snippet into t ...
- **csrankings** `free` `lookup`: CSRankings: CS faculty roster by institution/region (keyless, via CSRankings.org GitHub data). Query a REGION (singapore / canada / hong kong), an INSTITUTION ('National University of Singapore', 'University of Toronto'), or a faculty NAME → faculty with homepage + Google Scholar + ORCID + a DBLP li ...
- **exa** `keyed` `bring-your-own-login` `explicit-only` `proxy`: Exa neural/semantic web search (exa.ai): finds open-web pages by MEANING, not keywords. Use for conceptual queries where keyword search misses the right page; each result carries relevant-excerpt highlights. omniseek_read the URL for the full page. Complements ordinary web search + the curated sourc ...
- **wikicfp_nlp** `free` `explicit-only` `stream`: WikiCFP 自然语言处理征稿发现 (cat=natural language processing): NLP 方向会议/研讨会的征稿 (CFP) 条目: 会议名 + 地点 + 日期范围 + 事件页链接. 长尾 CFP 发现, 补 conference_deadlines/ml_conferences 的主榜. 注意: 截稿日不在 feed 里 (feed 只给会议日期), 真截稿需打开事件页看. 命名 omniseek_fetch.
- **xiaohongshu_cn** `walled` `explicit-only`: 小红书 mainland (xiaohongshu.com): 真浏览器驱动 (与 rednote 小号同一安全机制; 2026-06-25 由 self-signed direct-API 切换): search 读 SSR 笔记卡片, 笔记正文 + 完整评论区走拦截+DOM; forge nothing. signed direct-API 为 degraded fallback.

## methodology (5)

- **a_happy_phd** `free` `stream`: A Happy PhD: PhD wellbeing + productivity (Luis P. Prieto, ed. psychology lens)
- **github_awesome_phd** `free` `lookup`: GitHub Awesome-PhD curated lists: highest SNR PhD resource collections
- **lesswrong** `free` `stream`: LessWrong + Alignment Forum: research methodology meta-reflection (RSS, recent posts only)
- **ml_collective** `free`: ML Collective: free ML research mentorship + research jams + DLCT reading group
- **thesis_whisperer** `free` `stream`: The Thesis Whisperer: Inger Mewburn's authoritative PhD blog

## data (4)

- **eurostat_stats** `free` `explicit-only` `lookup`: Eurostat dissemination API: EU official statistics (harmonised unemployment, employment, real GDP, GDP per capita, HICP inflation, population, and the migration set: first residence permits, citizenship acquisitions, asylum applicants) as a JSON-stat time series, keyless. STRUCTURED point lookup, NO ...
- **gov_open_data** `free` `lookup`: Government open-data datasets (income/labour/census/economic stats) across data.gov.sg, data.gov.hk, open.canada.ca (keyless)
- **statcan_wds** `free` `explicit-only` `lookup`: Statistics Canada WDS: official Canadian statistics (labour force, CPI/inflation, population, real GDP) as a latest-N-period time series, keyless. STRUCTURED point lookup, NOT free-text search: query a keyword (unemployment / employment / participation / 'labour force' / cpi / inflation / population ...
- **worldbank_stats** `free` `explicit-only` `lookup`: World Bank Indicators: cross-country economic / labor / education statistics (GDP, unemployment, population, enrollment, ...) as a year-by-year time series, keyless. STRUCTURED point lookup, NOT free-text search: query is '<COUNTRY> <INDICATOR>' where COUNTRY is an ISO2/ISO3 code (CN, USA, CA) and I ...

## eval (4)

- **llm_leaderboard** `keyed` `bring-your-own-login` `explicit-only` `lookup`: LLM 榜单: Artificial Analysis 全模型实时评测 (500+ 模型: AA 智能/代码/数学指数, GPQA/AIME-25/HLE/LiveCodeBench/MMLU-Pro, $/1M tokens, tok/s, 发布日期). 领域脉搏的结构化层: 空 query=按智能指数排序; 关键词过滤模型/厂商 ('claude' / 'deepseek' / 'openai'). 新模型上榜=watchtower 新条目. Data by Artificial Analysis (attribution required)
- **lmsys_arena** `free` `stream`: LMArena (原 LMSys) 官方博客: Chatbot Arena Elo 方法论 + LLM 评测深度文章 + red-teaming 报告 (LLM 评测方法论的第一手来源). 站点已不再提供 RSS, 故直接抓其服务端渲染的博客列表.
- **ml_eval_safety** `free` `stream`: LLM 评测 + AI 安全/威胁评估 机构: METR (前沿危险能力/autonomy 评估 + Frontier Risk Report) + Epoch AI (基准 DB + 算力/数据/成本趋势量化). 评测/安全职业线信号, 区别于 lmsys_arena(实时 Elo) 与 alignment_forum(社区)
- **scrape_ml_orgs** `free` `stream`: ML 工程 / 评测 / 科研方法 机构 (scrape, 无 RSS): Baseten (推理云工程深文) + UK AISI (政府级前沿评测/安全研究) + Simon Peyton Jones research-skills (写论文/做报告 canonical craft)

## media (4)

- **chinese_ai_media** `free` `stream`: 中文 AI 媒体 + 个人博客: 量子位 QbitAI (直取) + 李博杰 01.me (agent/career 长文) + PaperWeekly (社区 wechat2rss 镜像). 机器之心/新智元 已移交 wechat 适配器 (经 wechat2rss, 比已死的 AnyFeeder 桥可靠)
- **kexue_fm** `free` `stream`: 科学空间 kexue.fm: 苏剑林（月之暗面）中文 NLP/LLM 数学派旗手个人博客
- **substack_matrix** `free` `stream`: Newsletter & individual researcher/career blogs: ML methodology / PhD career / LLM tools / Bayesian stats / China AI / career impact
- **wechat** `walled` `explicit-only` `portal`: 微信公众号: single-URL fetch (mp.weixin.qq.com/s/<id>); discovery via wewe-rss (Layer B)

## models (4)

- **epoch_ai_models** `free` `lookup`: Epoch AI notable-models dataset (keyless): training-scale facts for ~1000 significant ML models. Query a MODEL ('AlphaFold', 'GPT-3'), an ORG ('DeepMind', 'Anthropic'), or a DOMAIN ('Biology', 'Games', 'Language') -> each model's parameters, training compute (FLOP), dataset size, training hardware, ...
- **huggingface_hub** `free` `lookup`: HuggingFace Hub: models / datasets / Spaces unified search (open API)
- **modelscope** `free` `explicit-only` `lookup`: ModelScope (魔搭): the Chinese model hub (Alibaba): name a model / keyword to search China-ecosystem models (Qwen / iic / DeepSeek) by download count, with task taxonomy + license + lineage. The China-side analog of huggingface_hub. STRUCTURE, keyless.
- **openrouter_rankings** `free` `lookup`: OpenRouter 用量榜: 市场真金白银在跑哪些模型 (近一周 prompt+completion token 聚合排名). 补 llm_leaderboard 的另一面: 那个是'评测有多强', 这个是'实际用得多狠'. 空 query=按用量 token 从高到低; 关键词过滤厂商/模型 ('anthropic' / 'deepseek' / 'gemini'). 新模型冲上用量榜=watchtower 新条目. 用量数据来自 OpenRouter

## people (4)

- **dblp_author** `free` `explicit-only` `lookup`: DBLP authors: resolve a CS researcher by NAME to a canonical DBLP PID page (gateway to their full publication record) + affiliation + award notes; name a researcher to disambiguate them in computer science. STRUCTURE, keyless, people-lookup. CS-native; pairs with orcid / s2_authors / omniseek_resolv ...
- **orcid** `free` `explicit-only` `lookup`: ORCID: researcher iD + authenticated CV record (employments / educations / works / fundings) via the keyless public v3.0 API; name it to resolve a researcher by name and pull their structured career record. STRUCTURE, people-lookup, pub.orcid.org.
- **s2_authors** `free` `explicit-only` `lookup`: Semantic Scholar authors: resolve a researcher by NAME to citation metrics (h-index / citation count / paper count) + disambiguated candidate entities; name a researcher to rank who's who. STRUCTURE, keyless, people-lookup. Pairs with orcid (self-asserted CV) and omniseek_resolve_identity (OpenAlex) ...
- **wikidata_identity** `free` `explicit-only` `lookup`: Wikidata identity crosswalk: resolve a person or organisation NAME to its cross-platform identifier cluster (ORCID, Google Scholar, DBLP, Semantic Scholar, GitHub, LinkedIn, X, official site; org stock ticker / subsidiaries / industries), so an agent can jump straight to the canonical profiles (keyl ...

## podcast (4)

- **apple_podcasts** `free` `lookup`: Apple Podcasts: find a podcast show + its RSS feedUrl (iTunes Search, keyless); pull an episode .mp3 for omniseek_transcribe
- **chinese_podcasts** `free` `stream`: 中文 AI / 科技 / PhD 方法论 播客: 晚点 LateTalk / 硅谷101 / 时差 in-betweenness + 1 (张小珺/OnBoard!/42章经 等走 xiaoyuzhou 原生适配器, 无需 RSSHub; 口语内容配 omniseek_transcribe)
- **podcast_index** `keyed` `bring-your-own-login` `explicit-only` `lookup`: Podcast Index: cross-network podcast catalog; flags which episodes ship a podcast:transcript (read it, skip ASR) vs which need omniseek_transcribe (free key/secret)
- **xiaoyuzhou** `free` `stream`: 小宇宙播客: 张小珺商业访谈录 / OnBoard! / 42章经 等中文 AI/科技/创投深度访谈 (原生抓 xiaoyuzhoufm __NEXT_DATA__, 无需 RSSHub; 可配 ~/.omniseek/credentials/xiaoyuzhou.json)

## social (4)

- **bluesky** `keyed` `bring-your-own-login` `lookup`: Bluesky: academic Twitter migration target, AT Protocol open API
- **douyin** `walled` `bring-your-own-login` `explicit-only` `stream`: 抖音: 中国第一短视频平台的登录墙网页搜索 (UNWALL). 一手 留学/移民/海外生活、政务/官方号公告、创作者实时评论, 视频原生, 与 知乎(文字问答)/小红书(生活笔记)/一亩三分地(北美技术移民) 不重叠. 隔离 小号 会话 (9225 专属 Chrome, 同小红书 9223 模式). 返回视频的标题/文案、作者、互动数 + 视频 URL (再对该 url 调 omniseek_transcribe 可转写语音正文). 命名调用 (omniseek_search 单源钻取), 不进广搜.
- **mastodon** `free` `lookup`: Mastodon / the fediverse: public HASHTAG timelines across high-signal instances (mastodon.social / sigmoid.social ML / fosstodon.org FOSS), keyless. Reaches walled same-day fediverse posts web search barely indexes; best for BROAD / trending topics (retrieval is hashtag-level, not precise keyword se ...
- **x_search** `free` `explicit-only` `proxy`: X/Twitter 经搜索索引 (非 twscrape, 与封印的 twitter_x 互补): 研究者 'joining lab X' / 'we are hiring' / 招聘意图, 新 PI/新 lab 形成 (暖、不拥挤的 faculty/postdoc 入口). explicit_only; 查询带 joining/hiring/recruiting + 方向 + 机构(Vector/Mila/NUS/NTU/A*STAR)

## career (3)

- **linkedin_posts** `free` `explicit-only` `proxy`: LinkedIn 公开招聘帖 经搜索索引 (仅 Track A 公开 /posts; 人脉/档案/国籍属法务封死的 Track B, 不碰): 具名 hiring manager 'we are hiring' = 最暖的当前招聘意图 + 直接暖介绍目标. explicit_only
- **nature_careers** `free` `stream`: Nature Careers: authoritative PhD survey data + career articles
- **nowcoder** `free` `explicit-only` `stream`: 牛客网 面经 + 内推: 中文 AI/ML 真实面试 bar (八股 vs 重思维 / 全流程时间线 / 内推码), 成于面试后数天. 直连 JSON 被 Aliyun WAF 墙(指纹闸非登录闸)→ 经共享 9222 CDP Chrome 原生取 (带真实指纹, 无需登录); CDP 挂了才降级走 site:nowcoder.com Brave 检索. 默认 算法工程师(645), 可配 ~/.omniseek/credentials/nowcoder.json job_ids

## compensation (3)

- **canada_jobbank_wages** `free` `explicit-only` `lookup`: Job Bank 加拿大: 官方岗位现行时薪 (低/中位/高, 每小时; 全国 + 各省 + 经济区), 数据源EI 就业保险工资调查 / 加拿大统计局劳动力调查 (LFS). keyless. 传一个职业名 ("software engineer" / "registered nurse" / "electrician" / "data scientist") → 先经站内 Solr typeahead 解析成 Job Bank 职业 id, 再抓该职业的官方工资表. 用于加拿大薪酬参考、LMIA / Express Entry 现行工资锚点, 与 levels.fyi (美国众包总包) 互 ...
- **levels_fyi** `free` `lookup`: levels.fyi: 科技公司/岗位薪酬 (TC 黄金标准, keyless). 两种查法: (1) 岗位[+国家] ("machine learning engineer singapore" / "data scientist canada") → 该岗位在该国的中位 + 区间 (本币, location-accurate, 取自页面 og:description); (2) 公司名 ("bytedance") → 该公司各职位族/级别总包 (US/USD 基准). 用于谈 offer / 比较雇主薪酬参考.
- **ontario_sunshine** `free` `explicit-only` `lookup`: 安大略 Sunshine List: 公共部门薪酬披露 (>= CAD 10万, 含大学/教职, data.ontario.ca 官方 CKAN, keyless). 查人名/雇主/职称 → 该人薪资+福利+部门+年份. 安大略大学 PI/教职 薪酬的公开参考. 开放数据目前止于 2020 (2021+ 仅前端). 空 query 无意义 → 传人名或机构名 (如 'University of Toronto').

## insider (3)

- **blind** `free` `explicit-only` `proxy`: Blind 在职员工匿名爆料 (经搜索索引, 非爬墙): 按国籍的 EP/签证真实结果、firm 真实薪资、招聘冻结、内推渠道. 全 insider 层最高价值生信号 (HK/中国护照 EP 可批性). explicit_only; 查询里带 公司名 + EP/visa/offer/freeze/referral
- **glassdoor** `free` `explicit-only` `proxy`: Glassdoor 面试经历 + 公司评价 + 文化 (经搜索索引): 按角色的真实面试 bar/流程/红旗 (Cohere/RBC Borealis/Google SG/DBS). 与 levels.fyi(薪酬) 互补, 定位面试 bar+文化. explicit_only
- **maimai** `free` `explicit-only` `proxy`: 脉脉职言 经搜索索引 (仅 /article/detail 公开层, 评论登录墙不取): CN-HQ 大厂(字节/Sea/Shopee)staffing SG/海外 的内幕: 内推/真实薪资/裁员风声/招聘冻结. CN-HQ-SG 切片最佳. explicit_only

## policy (3)

- **cset** `free` `lookup`: CSET (Georgetown): US AI-policy think-tank reports on compute/export controls, AI safety, and national security (keyless WordPress REST API)
- **federal_register** `free` `explicit-only` `stream`: US Federal Register keyless JSON API (federalregister.gov/api/v1). Full-text term search over the daily journal of the US government: proposed and final Rules, Notices, and Presidential Documents (executive orders, proclamations). Each hit carries title, document type (Rule / Proposed Rule / Notice ...
- **oecd_ai_policy** `free` `explicit-only` `lookup`: OECD.AI Policy Navigator: 全球官方 AI 政策倡议活册 (STRUCTURE/MONITOR). 全世界最大的政府 AI 政策目录: 2364 项倡议, 覆盖 80+ 法域与政府间组织, 每项带 法域/工具类型/类别/约束力状态/起止年份 + 原始政策文件链接. 端点无全文检索且 perPage 固定 20 (119 页 × ~3s 无法在抓取窗口内快照全量), 故本源取 最新切片: 抓最新 ~300 项 (按加入时间倒序 = 最近新增/更新的倡议, 跨法域), 缓存 30 天, 逐查询 BM25 过滤. 这是对 谁在进入导航册 的 MONITOR 视角, 非全史注册 ...

## video (3)

- **bilibili** `free` `lookup`: Bilibili: Chinese academic video (论文精读, 科研 vlog, 方法论讲解); pass a BV-id/video URL as the query to get its top comments as docs
- **youtube** `walled` `lookup`: YouTube: video search + transcript + top comments (PhD methodology channels, lectures, talks; pass a video URL/id as the query to get its comments as docs)
- **youtube_channels** `free` `stream`: Curated YouTube channels: MLST / Yannic Kilcher / 3Blue1Brown / Dwarkesh / GPU MODE (latest uploads via yt-dlp; the RSS endpoint is IP-blocked for this host)

## books (2)

- **books_openlibrary_ia** `free` `lookup`: Books: Open Library bibliographic records + Internet Archive full-text-inside-books (read links to openlibrary.org / archive.org/details), keyless
- **gutenberg** `free` `explicit-only` `lookup`: Project Gutenberg: full-text public-domain books (literature/philosophy/classics) via the keyless Gutendex API

## compute (2)

- **gpu_pricing** `free` `explicit-only` `lookup`: GPU 价格对比: 跨云 GPU $/hr 结构化查询 (keyless, Full Stack DL cloud-gpus 数据). compute-bound 时'现在最便宜的 H100/A100/4090 在哪家云'. 查 GPU 型号 (H100/A100/4090) 和/或云 (lambda/runpod/vast) → 报价按便宜优先. 快照非实时; 实时 spot 见 vast.ai/runpod API. 配 reference/compute-access-map.md (现在能申的免费额度).
- **vast_ai** `free` `explicit-only` `lookup`: Vast.ai: LIVE GPU rental marketplace: per-offer on-demand + spot ($/hr), perf-per-dollar, GPU RAM, location, reliability; name it with an EXACT GPU label ('RTX 4090', 'H100 SXM') to price a model, or bare to list the cheapest offers. STRUCTURE, keyless; the live compute-cost order book gpu_pricing's ...

## deadlines (2)

- **conference_deadlines** `free` `lookup`: ML/AI conference submission deadlines (ccfddl): NeurIPS / ICML / ICLR / CVPR / ACL / AAAI / IJCAI / CoRL / ICRA + 47 more AI venues, ranked by nearest upcoming deadline with CCF/CORE rank + location
- **ml_conferences** `free` `explicit-only` `stream`: NeurIPS / ICML / ICLR 官方博客: 获奖公告 (Outstanding Papers / Test of Time / Awards)、投稿与评审政策变化 (如 NeurIPS 对 AI 生成论文的处理)、注册与容量公告、keynote 名单、newsletter. 与 conference_deadlines 互补: 那个只给截稿日期, 这个给公告与政策. 站点已把 feed 挡在反爬后, 故经 CDP 真浏览器渲染抓取, 命名钻取 (omniseek_search 单源 raw).

## filings (2)

- **sec_edgar** `free` `lookup`: SEC EDGAR full-text filing search: US public-company disclosures (10-K/8-K/DEF 14A proxy/etc.), keyless UA-gated
- **uk_companies_house** `keyed` `bring-your-own-login` `explicit-only` `lookup`: UK Companies House: the official company register (free key). Name search returns UK companies (number/status/type/incorporation date/registered office); drill a company by CRN with 'officers:12345678' (directors/secretaries) or 'psc:12345678' (beneficial owners, persons with significant control). f ...

## safety (2)

- **ai_incidents** `free` `explicit-only` `lookup`: AI Incident Database (AIID): the curated ledger of real-world AI HARMS: search incidents by keyword (facial recognition, chatbot, autonomous vehicle, biased hiring) → each with the alleged developer + deployer, harmed parties, date, and the source-report URLs to drill into. STRUCTURE, keyless; the e ...
- **alignment_forum** `free` `stream`: AI Alignment Forum: alignment / safety / interpretability research (complement to lesswrong adapter; AF-filtered view)

## clinical (1)

- **clinicaltrials** `free` `explicit-only` `lookup`: ClinicalTrials.gov: registered clinical trials (status/phase/condition/sponsor) via the keyless NIH v2 API

## datasets (1)

- **kaggle** `free` `explicit-only` `lookup`: Kaggle: public ML dataset catalog (vote/download-ranked tabular/image/text corpora, keyless listing API)

## patents (1)

- **google_patents** `free` `explicit-only` `lookup`: Google Patents: patent / prior-art search (publication, assignee, inventor, abstract) via the keyless XHR endpoint. Google's query operators pass straight through in the query string, e.g. assignee:"NVIDIA Corporation" (verified to narrow + rank toward that assignee); inventor:/before:priority:YYYYM ...

## reference (1)

- **wikidata_wikipedia** `free` `lookup`: Wikipedia + Wikidata: encyclopedia article summaries plus structured-fact entities (QID handles + key claims) for any topic (keyless MediaWiki/Wikibase APIs)

## tooling (1)

- **agent_tooling_radar** `free` `explicit-only` `stream`: Agent/Skill 生态雷达: Claude Code/MCP/Agent SDK 发布 + 研究加速工具(paper-qa/paper-search-mcp/context7/serena)+ eval/observability(inspect/phoenix/langfuse)发布 + awesome-lists(agent-papers/agent-skills/superpowers)+ Latent Space/Simon Willison。盯'存在哪些好工具'的信息差(2026-06-05 基线扫描产物;explicit_only,看门狗/点名才查)

---

<div align="center"><sub><a href="../README.md">← back to the README</a></sub></div>
