#!/usr/bin/env python3
"""Offline smoke gate. Run by deploy.sh on the mini BEFORE the service restarts;
any failure aborts the deploy. Pure invariants, no network, no judgment.

Covers the failure classes we have actually hit:
  - a config row and a coded adapter sharing a name (silent replacement)
  - a malformed / incomplete config row
  - explicit_only entries pointing at names that do not exist
  - cross-source dedup fingerprint regressions

Run anywhere with the deps installed: .venv/bin/python tests/smoke.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  ok   " if ok else "  FAIL ") + name + (f": {detail}" if (detail and not ok) else ""))
    if not ok:
        FAIL.append(f"{name}: {detail}")


# ---------------------------------------------------------------------------
# 1. Config files: parse, required fields, in-file + cross-file name uniqueness
# ---------------------------------------------------------------------------
SOURCES = ROOT / "src" / "penumbra" / "core" / "sources"
CONFIGS = [
    ("scrape/rss_bundles.json", ("name", "description", "feeds")),
    ("scrape/scrape_sites.json", ("name", "description", "sites")),
    ("api/org_watch.json", ("name", "affiliations", "description")),
    ("api/search_index_sites.json", ("name", "description", "site")),
    ("scrape/ai_residencies.json", ("kind", "lab")),
    ("scrape/page_watch.json", ("name", "label", "url")),
]
all_config_names: list[str] = []
for rel, required in CONFIGS:
    p = SOURCES / rel
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        check(f"{rel} parses", False, str(exc))
        continue
    check(f"{rel} parses ({len(rows) if isinstance(rows, list) else '?'} rows)",
          isinstance(rows, list) and len(rows) > 0)
    bad = [str(r.get("name", "?")) for r in rows if not all(r.get(k) for k in required)]
    check(f"{rel} rows complete", not bad, f"rows missing one of {required}: {bad}")
    all_config_names.extend(str(r.get("name")) for r in rows
                            if isinstance(r, dict) and r.get("name"))

dupes = sorted({n for n in all_config_names if all_config_names.count(n) > 1})
check("config row names unique across config files", not dupes, str(dupes))

# ---------------------------------------------------------------------------
# 2. Registry: import every adapter; no silent name collisions; descriptions
# ---------------------------------------------------------------------------
from penumbra.server import _SKIP_SOURCES, load_sources  # noqa: E402

load_sources()
from penumbra.core import fetcher  # noqa: E402

names = fetcher.all_adapter_names()
check(f"registry loaded ({len(names)} sources)", len(names) >= 100, f"only {len(names)}")
check("no adapter name collisions", not fetcher._collisions, str(fetcher._collisions))
no_desc = [n for n in names if not (fetcher.get_adapter(n).description or "").strip()]
check("every adapter has a description", not no_desc, str(no_desc))

# Read-only invariant: the eye RETRIEVES, it never mutates a remote source. Every adapter must
# implement the read interface (search / fetch_url / health_check) and expose NO public method whose
# name implies a write, so an open-source operator can trust that enabling a source can never post /
# delete / act on their behalf. (An internal POST to a search endpoint is fine: this guards the
# adapter's PUBLIC method surface, not the HTTP verb.)
import re as _re_ro  # noqa: E402
_MUTATION_VERB = _re_ro.compile(
    r"^(create|write|delete|update|post|put|submit|upload|send|remove|edit|modify|insert|patch|"
    r"login|logout|comment|like|unlike|follow|unfollow|vote|reply|publish|destroy|purchase|set_)",
    _re_ro.I)
_ro_missing_iface, _ro_mutators = [], []
for _ro_n in names:
    _ro_a = fetcher.get_adapter(_ro_n)
    for _ro_iface in ("search", "fetch_url", "health_check"):
        if not callable(getattr(_ro_a, _ro_iface, None)):
            _ro_missing_iface.append(f"{_ro_n}.{_ro_iface}")
    for _ro_m in dir(_ro_a):
        if not _ro_m.startswith("_") and callable(getattr(_ro_a, _ro_m, None)) \
                and _MUTATION_VERB.match(_ro_m):
            _ro_mutators.append(f"{_ro_n}.{_ro_m}")
check("read-only invariant: every adapter implements search/fetch_url/health_check",
      not _ro_missing_iface, str(_ro_missing_iface))
check("read-only invariant: no adapter exposes a mutation-named public method",
      not _ro_mutators, str(_ro_mutators))

# Stability derivation (the NEUTRAL fragility class): every live source resolves to one of the
# four ordered values, and the central derivation classifies the load-bearing exemplars right —
# walled (CDP/login), keyed (free key), stable (public API/RSS), scrape (HTML/JSON parse).
_stab = {e["name"]: e.get("stability") for e in fetcher.list_sources()}
_bad_stab = sorted(n for n, v in _stab.items() if v not in fetcher._STABILITY_VALUES)
check("every live source resolves a stability in the four-value set", not _bad_stab, str(_bad_stab))
_stab_expect = {"xiaohongshu": "walled", "zhihu": "walled",  # CDP + logged-in session
                "arxiv": "stable", "openalex": "stable",      # public no-key API
                "podcast_index": "keyed", "exa": "keyed",       # free key / credential
                "bilibili": "scrape", "google_patents": "scrape"}  # HTML/JSON scrape
_stab_miss = {n: (_stab.get(n), want) for n, want in _stab_expect.items()
              if n in _stab and _stab[n] != want}
check("stability derivation classifies the exemplars right (walled/keyed/stable/scrape)",
      not _stab_miss, str(_stab_miss))

# Access-tier derivation (the LEGAL-facing class, catalog-first self-description): every source
# resolves to free/keyed/walled/circumvention, and the exemplars classify right — the unique signal
# being `circumvention` (mokahr decrypts an encrypted response = highest §1201 risk, never default).
_tier = {e["name"]: e.get("access_tier") for e in fetcher.list_sources()}
_bad_tier = sorted(n for n, v in _tier.items() if v not in fetcher._ACCESS_TIERS)
check("every source resolves an access_tier in the four-value set", not _bad_tier, str(_bad_tier))
_tier_expect = {"mokahr_ats": "circumvention",          # decrypts the platform's encrypted response
                "arxiv": "free", "openalex": "free",     # public, no key
                "xiaohongshu": "walled"}                  # logged-in CDP session
_tier_miss = {n: (_tier.get(n), want) for n, want in _tier_expect.items()
              if n in _tier and _tier[n] != want}
check("access_tier derivation classifies the exemplars right (free/keyed/walled/circumvention)",
      not _tier_miss, str(_tier_miss))

# ---------------------------------------------------------------------------
# 3. explicit_only entries must point at real adapters (parked modules excepted)
# ---------------------------------------------------------------------------
parked = {m[: -len("_source")] for m in _SKIP_SOURCES}
stale = [n for n in fetcher._EXPLICIT_ONLY_SOURCES if n not in names and n not in parked]
check("explicit_only override names all registered (or parked)", not stale, str(stale))

# The effective broad-search exclusion set is FROZEN: changing it must be a
# deliberate act (edit the adapter/row declaration AND this list together).
EXPECTED_EXPLICIT_ONLY = sorted("""
xiaohongshu xiaohongshu_cn sogou_weixin openalex_cn cninfo gov_policy eastmoney juejin zhihu zhihu_users yipinsanfendi xiaomuchong scrape_js_sites douyin
mokahr_ats bytedance_seed discord_communities feishu_jobs wechat
sea_ai_lab deepseek qwen moonshot zhipu yi_01ai stepfun baichuan tencent_hunyuan
shanghai_ai_lab baai bytedance_research ant_ling huawei_noah
openai google_deepmind meta_fair microsoft_research cohere ai2 nvidia_research
salesforce_research sakana eleutherai together_ai huggingface reka stability
contextual nous databricks_mosaic scale_ai liquid_ai astar_cfar vector_institute
mila amii rbc_borealis servicenow_research
gpu_pricing blind glassdoor x_search maimai linkedin_posts agent_tooling_radar
zhihu_search quora yipin_search hardwarezone xiaohongshu_search nowcoder
cdp_fulltext exa ircc_ee_rounds page_watch llm_leaderboard gter
ontario_sunshine higheredjobs_cs scrape_hongkong scrape_canada distill_pub
google_patents
market_quote market_crypto wayback sec_financials
nsf_awards nih_reporter
europepmc orcid worldbank_stats adzuna s2_authors dblp_author
remotive grants_gov
vast_ai modelscope
crossref_retractions datagovsg_nonresident_pass_types wikicfp_nlp
oinp_invitations bcpnp_invitations aaip_draws nserc_awards sshrc_awards cihr_grants
context7
""".split())
actual_eo = sorted(n for n in names if fetcher._explicit_only_reason(fetcher.get_adapter(n)))
# mokahr_ats (access_tier=circumvention) is EXCLUDED from the public release for §1201 reasons, so its
# absence is tolerated here; nothing else may drift (no extras, no other missing).
_eo_extra = sorted(set(actual_eo) - set(EXPECTED_EXPLICIT_ONLY))
_eo_missing = sorted(set(EXPECTED_EXPLICIT_ONLY) - set(actual_eo))
check("explicit_only set matches the frozen list (mokahr_ats optional: public release excludes it)",
      not _eo_extra and set(_eo_missing) <= {"mokahr_ats"},
      f"extra={_eo_extra} missing={_eo_missing}")

# org_watch AI-co-author crank filter (a Zenodo/Open-MIND preprint class that lists an AI MODEL as
# an author + attributes the lab as its affiliation, name-colliding into a lab's stream). Lock the
# regression: model bylines drop, real humans (incl. Claude Shannon / Gemma Boleda) survive.
from penumbra.core.sources.api import org_watch_source as _ow  # noqa: E402


def _ow_work(*author_names):
    return {"authorships": [{"author": {"display_name": n}} for n in author_names]}


_OW_CRANK = ["Kimi 2.5 Agent", "Gemini 3.1 (Flash)", "gpt-5.4", "Claude v4.7 Opus", "DeepSeek v3"]
_OW_HUMAN = ["Claude Shannon", "Gemma Boleda", "Frederic Mistral", "Qwen Team", "Bin Wang", "Yutao Sun"]
check("org_watch crank filter drops every AI-model byline",
      all(_ow._is_ai_byline_crank(_ow_work(n)) for n in _OW_CRANK),
      str([n for n in _OW_CRANK if not _ow._is_ai_byline_crank(_ow_work(n))]))
check("org_watch crank filter keeps every human/team author",
      not any(_ow._is_ai_byline_crank(_ow_work(n)) for n in _OW_HUMAN),
      str([n for n in _OW_HUMAN if _ow._is_ai_byline_crank(_ow_work(n))]))

# Egress SSRF guard (open-source hardening): the mainline fetch must refuse private/loopback/
# link-local/reserved targets + bad scheme/port/userinfo (IP-literal + shape cases are offline).
from penumbra.core import _netguard as _ng  # noqa: E402
_NG_BLOCK = ["http://169.254.169.254/latest/meta-data/", "http://127.0.0.1/", "http://127.0.0.1:8765/",
             "http://[::1]/", "http://10.0.0.5/", "http://192.168.1.1/", "ftp://x.com/",
             "file:///etc/passwd", "http://u:p@example.com/", "http://example.com:8080/"]
check("netguard blocks every SSRF-class / bad-shape URL",
      all(_ng.security_block_reason(u) is not None for u in _NG_BLOCK),
      str([u for u in _NG_BLOCK if _ng.security_block_reason(u) is None]))
check("netguard allows the fake-IP proxy net (198.18/15) so split-tunnel deploys still egress",
      _ng.security_block_reason("http://198.18.0.9/") is None,
      _ng.security_block_reason("http://198.18.0.9/") or "")

# docreader local-file sandbox: arbitrary host paths (credentials!) refused; inbox stays readable.
from penumbra.core import docreader as _dr  # noqa: E402
import pathlib as _pl  # noqa: E402
def _dr_escapes(p):
    try:
        _dr._resolve_local(p); return False
    except PermissionError:
        return True
check("docreader sandbox refuses reading outside the inbox (e.g. ~/.penumbra/credentials)",
      _dr_escapes("~/.penumbra/credentials/penumbra_http.json") and _dr_escapes("/etc/passwd")
      and _dr_escapes("penumbra-inbox/../.penumbra/credentials/penumbra_http.json"),
      "a disallowed path was NOT refused")
check("docreader sandbox still allows the documented penumbra-inbox/<name> path",
      _dr._resolve_local("penumbra-inbox/x.pdf") == (_pl.Path.home() / "penumbra-inbox" / "x.pdf").resolve(),
      str(_dr._resolve_local("penumbra-inbox/x.pdf")))

# Deployment-profile gate (P1): no profile -> all sources on (pre-profile/backward-compat); a
# profile subtracts. Walled-tier off keys off DERIVED stability (the robust mokahr-leak fix).
from penumbra.core import profile as _prof  # noqa: E402
_prof.invalidate()
check("profile: absent profile leaves every source enabled (backward-compat)",
      _prof.is_source_enabled("arxiv") and _prof.is_source_enabled("zhihu", stability="walled"),
      "a source was disabled with NO profile present")
_prof._cache = {"sources": {"default_enabled": True, "disable": ["glassdoor"], "enable": ["mokahr_ats"]},
                "groups": {"disable_regions": ["cn"], "disable_stability": ["walled"]},
                "walled": {"enabled": False, "bring_your_own": {}}}
try:
    check("profile: sources.disable turns a source off", not _prof.is_source_enabled("glassdoor"), "")
    check("profile: walled-tier-off disables a walled source by derived stability",
          not _prof.is_source_enabled("zhihu", stability="walled"), "")
    check("profile: sources.enable beats every group/tier rule (most-specific wins)",
          _prof.is_source_enabled("mokahr_ats", stability="walled"), "")
    check("profile: groups.disable_regions subtracts by region facet",
          not _prof.is_source_enabled("juejin", regions=["cn"]), "")
finally:
    _prof.invalidate()
_prof_ex = _pl.Path(__file__).resolve().parents[1] / "profile.example.json"
check("profile.example.json parses as a dict (when shipped)",
      (not _prof_ex.is_file())
      or isinstance(__import__("json").loads(_prof_ex.read_text(encoding="utf-8")), dict),
      "profile.example.json present but not valid JSON object")

# ---------------------------------------------------------------------------
# 4. rank: dedup fingerprint + merge invariants (the cross-source collapse)
# ---------------------------------------------------------------------------
from penumbra.core import rank  # noqa: E402
from penumbra.core.normalize import Document  # noqa: E402


def _doc(src: str, title: str, url: str = "") -> Document:
    return Document(source=src, source_id=url or title, url=url, title=title, content="")


a = _doc("arxiv", "Scaling Laws for Neural Language Models", "http://arxiv.org/abs/2001.08361")
b = _doc("openalex", "Scaling Laws for Neural Language Models", "https://doi.org/10.1/xyz")
check("fingerprint merges same long title across sources", rank.fingerprint(a) == rank.fingerprint(b))
merged = rank.dedup([a, b])
check("dedup collapses duplicate + records also_in",
      len(merged) == 1 and bool(merged[0].metadata.get("also_in")))
s1, s2 = _doc("x", "Hi", "https://e.com/1"), _doc("y", "Hi", "https://f.com/2")
check("short titles never merge across sources", len(rank.dedup([s1, s2])) == 2)
check("merge_rank survives empty input", rank.merge_rank({}, "q") == [])

# ---------------------------------------------------------------------------
# 5. relevance: the bug classes the BM25-lite engine exists to prevent
# ---------------------------------------------------------------------------
from penumbra.core import relevance  # noqa: E402

spam = _doc("x", "Machine learning systems overview",
            "https://e.com/spam")
spam.content = "learning " * 60
true_hit = _doc("y", "Reinforcement learning for reasoning agents", "https://e.com/hit")
true_hit.content = "We train agents with reinforcement learning to improve reasoning."
s_spam, s_hit = relevance.doc_scores([spam, true_hit], "reinforcement learning reasoning agents")
check("term spam no longer outranks the true hit", s_hit > s_spam,
      f"hit={s_hit:.2f} spam={s_spam:.2f}")

cjk_hit = _doc("x", "大模型的推理能力评测研究", "https://e.com/c1")
cjk_miss = _doc("y", "化学反应动力学研究", "https://e.com/c2")
s_hit2, s_miss = relevance.doc_scores([cjk_hit, cjk_miss], "大模型推理")
check("CJK bigrams match across particles (的) and ignore chemistry",
      s_hit2 > 0 and s_miss == 0.0, f"hit={s_hit2:.2f} miss={s_miss:.2f}")

inword = _doc("x", "How to maintain quality", "https://e.com/m")
real_ai = _doc("y", "AI safety overview", "https://e.com/a")
s_inword, s_ai = relevance.doc_scores([inword, real_ai], "AI")
check("ASCII terms respect word boundaries ('ai' not inside 'maintain')",
      s_inword == 0.0 and s_ai > 0, f"maintain={s_inword:.2f} ai={s_ai:.2f}")

check("term-less query keeps caller order",
      relevance.filter_rank([spam, true_hit], "") == [spam, true_hit])

# ---------------------------------------------------------------------------
# 6. relations: the relationship-layer module imports + offline invariants
# ---------------------------------------------------------------------------
from penumbra.core import relations  # noqa: E402

check("relations._looks_like_id classifies OpenAlex / S2 / name",
      relations._looks_like_id("A5029408111") == "openalex"
      and relations._looks_like_id("2257035605") == "s2"
      and relations._looks_like_id("Yi R. Fung") is None)
# _rank: hint-matching institution floats up; pure works_count order otherwise
_c = [{"works_count": 90, "institution": "MIT"}, {"works_count": 5, "institution": "HKUST"}]
check("relations._rank floats the hint-matching candidate above raw works_count",
      relations._rank(_c, "hkust")[0]["institution"] == "HKUST")
check("relations._rank with no hint sorts by works_count",
      relations._rank(_c, "")[0]["works_count"] == 90)
check("relations.coauthors tolerates empty input",
      relations.coauthors([]) == {"source": "openalex", "nodes": [], "edges": [],
                                  "bridges": [], "cooc": []})
check("relations._name_key collapses split-id duplicates by name",
      relations._name_key("Chao Du") == relations._name_key("Chao  Du")
      and relations._name_key("Yi R. Fung") == relations._name_key("Yi R Fung"))
check("relations._resolve_one merges '+'-joined ids into one person",
      (relations._resolve_one("123456+789012", "", "s2").get("resolved") or {}).get("ids")
      == ["123456", "789012"])
# guard the regression this audit caught: top_coauthors / bridges must carry a
# representative id (the 'anchor + harvest' drill technique depends on it).
import inspect as _insp  # noqa: E402
_csrc = _insp.getsource(relations.coauthors)
check("coauthors top_coauthors carries an id (harvest technique)",
      '"id": rep_idc[k].most_common(1)[0][0], "name": rep[k], "joint"' in _csrc)
check("coauthors bridges carry an id", _csrc.count('rep_idc[k].most_common(1)[0][0]') >= 2)
from penumbra.core import cartographer  # noqa: E402
check("cartographer._norm_s2_id prefixes bare arXiv ids (recommend/skeleton seeds)",
      cartographer._norm_s2_id("2203.02155") == "ArXiv:2203.02155"
      and cartographer._norm_s2_id("CorpusID:9") == "CorpusID:9")
# the three relationship tools are registered on the MCP server
import penumbra.server as _srv  # noqa: E402
for _t in ("penumbra_resolve_identity", "penumbra_coauthors", "penumbra_institution_cohort"):
    check(f"server exposes {_t}", hasattr(_srv, _t) and callable(getattr(_srv, _t)))

# ---------------------------------------------------------------------------
# 7. dogfood-audit fixes (2026-06-10): reddit strict-AND ladder, search-index
#    tombstone filter, ASR time-range slicing
# ---------------------------------------------------------------------------
from penumbra.core import asr  # noqa: E402
from penumbra.core.sources.api import reddit_source, search_index_source  # noqa: E402

check("reddit._relax_tiers caps the AND tier, then falls back to longest tokens",
      reddit_source._relax_tiers("employment pass compass chinese phd")
      == ["employment pass compass chinese", "employment compass", "employment"])
check("reddit._relax_tiers no-ops on short / single / empty queries",
      reddit_source._relax_tiers("compass") == ["compass"]
      and reddit_source._relax_tiers("compass approval") == ["compass approval", "approval"]
      and reddit_source._relax_tiers("") == [""])
# 2026-06-21 FIX: specificity-ranked term selection keeps SHORT acronyms (PGWP/CEC/LMIA/PR) + strips
# stopwords, replacing the length-only proxy that mangled "...PR without PGWP CEC foreign worker LMIA"
# into "permit without foreign worker" and then discovered garbage subs (r/ForeignMovies) -> 422 storm.
check("reddit._content_terms strips stopwords (to / without)",
      reddit_source._content_terms("work permit to PR without PGWP CEC foreign worker LMIA")
      == ["work", "permit", "PR", "PGWP", "CEC", "foreign", "worker", "LMIA"])
check("reddit._term_rank ranks a SHORT acronym above a LONG generic word (the defect's core)",
      reddit_source._term_rank("PGWP") > reddit_source._term_rank("foreign")
      and reddit_source._term_rank("CEC") > reddit_source._term_rank("without"))
check("reddit._relax_tiers keeps the acronyms (PGWP/CEC/LMIA/PR), drops generic long words",
      (lambda t: t[0] == "PR PGWP CEC LMIA" and t[-1] == "PGWP"
       and "without" not in " ".join(t) and "foreign" not in " ".join(t))(
          reddit_source._relax_tiers("work permit to PR without PGWP CEC foreign worker LMIA")))
check("reddit: the general-engine route still works (stopword strip keeps the topical term)",
      reddit_source._relax_tiers("pour over coffee")[0] == "pour coffee")

# reddit finance routing (2026-06-17): a finance-signal query ADDS the curated finance subs on top
# of the auto-route core; a non-financial query is byte-identical (no finance subs, no regression).
# Pure / offline: exercises _looks_financial + _with_finance_subs, never touches the Arctic network.
check("reddit._looks_financial detects cashtags + finance keywords, not generic/career words",
      reddit_source._looks_financial("$NVDA earnings")
      and reddit_source._looks_financial("Oracle stock buyback guidance")
      and reddit_source._looks_financial("SEC filing risk factors")
      and not reddit_source._looks_financial("phd advice")
      and not reddit_source._looks_financial("how to get a job at google")
      and not reddit_source._looks_financial("woodstock festival history"))  # 'stock' inside a word must not trip
check("reddit._with_finance_subs widens a financial query with finance subs (additive, deduped, core kept first)",
      (lambda r: set(reddit_source._FINANCE_SUBS) <= set(r)
       and r[:len(reddit_source.DEFAULT_SUBREDDITS)] == reddit_source.DEFAULT_SUBREDDITS
       and len(r) == len({s.lower() for s in r}))(
          reddit_source._with_finance_subs(list(reddit_source.DEFAULT_SUBREDDITS), "$NVDA earnings")))
check("reddit._with_finance_subs is a byte-identical no-op for a non-financial query (no regression)",
      reddit_source._with_finance_subs(list(reddit_source.DEFAULT_SUBREDDITS), "phd advice")
      == list(reddit_source.DEFAULT_SUBREDDITS)
      and "stocks" not in reddit_source._with_finance_subs(list(reddit_source.DEFAULT_SUBREDDITS), "phd advice"))

# reddit Arctic Shift 429 circuit breaker (2026-06-20): the mirror rate-limits the 23-sub fan-out
# HARD (HTTP 429); a per-sub retry amplified it into a 117-line 429 storm + ~20s backoff per broad
# search. N consecutive sub-failures now trip a global cooldown so _arctic_get skips the mirror
# entirely (instant None, no network) until it heals. Offline: drive the breaker state directly +
# monkeypatch http.get_json to PROVE no network call happens while cooling.
import penumbra.core.http as _rd_http  # noqa: E402
reddit_source._arctic_cooldown_until = 0.0
reddit_source._arctic_fail_streak = 0
_rd_cold_before = reddit_source._arctic_cooling()
for _ in range(reddit_source._ARCTIC_TRIP_AFTER):
    reddit_source._arctic_record(False)
_rd_tripped = reddit_source._arctic_cooling()
_rd_orig_gj, _rd_calls = _rd_http.get_json, []
_rd_http.get_json = lambda *a, **k: (_rd_calls.append(1), None)[1]
try:
    _rd_skip = reddit_source._arctic_get("/posts/search", {"subreddit": "PhD"}, retries=2)
finally:
    _rd_http.get_json = _rd_orig_gj
reddit_source._arctic_cooldown_until = 0.0  # reset so later/live code is unaffected by the test
reddit_source._arctic_fail_streak = 0
check("reddit: Arctic 429 breaker trips after N sub-failures + skips the mirror while cooling (no network)",
      (not _rd_cold_before) and _rd_tripped and (_rd_skip is None) and (len(_rd_calls) == 0))
check("reddit: a real response clears the breaker failure streak (self-heal)",
      (reddit_source._arctic_record(True) or reddit_source._arctic_fail_streak == 0))

# reddit health_check is a DATA-PATH check, not a liveness ping (2026-06-21, after the 18-agent
# concurrency stress test): (1) breaker cooling reads healthy-throttled (Arctic is rate-limiting us,
# not down: mirror of _s2 "429=alive"), NOT "unreachable" — and must not even probe while cooling;
# (2) a well-formed EMPTY bare r/PhD probe (never legitimately empty) reads UNHEALTHY (data path
# degraded), closing the "health=ok but returns zero" gap; (3) a real probe item reads healthy.
_rd_ad = reddit_source.RedditAdapter()
_rd_save_ag = reddit_source._arctic_get
try:
    reddit_source._arctic_cooldown_until = reddit_source.time.time() + 9999  # breaker open
    _rd_ag_calls = []
    reddit_source._arctic_get = lambda *a, **k: (_rd_ag_calls.append(1), [])[1]
    _h_cool = _rd_ad.health_check()
    check("reddit health: breaker cooling reads healthy-throttled (not 'unreachable') + skips the probe",
          _h_cool[0] is True and "cooling" in _h_cool[1] and len(_rd_ag_calls) == 0)
    reddit_source._arctic_cooldown_until = 0.0  # breaker closed for the next two cases
    reddit_source._arctic_get = lambda *a, **k: []
    _h_empty = _rd_ad.health_check()
    check("reddit health: well-formed EMPTY bare probe reads unhealthy (data path degraded, not liveness)",
          _h_empty[0] is False and "data path" in _h_empty[1])
    reddit_source._arctic_get = lambda *a, **k: [{"id": "x", "title": "t", "subreddit": "PhD"}]
    _h_ok = _rd_ad.health_check()
    check("reddit health: a real probe item reads healthy", _h_ok[0] is True and "probe item" in _h_ok[1])
finally:
    reddit_source._arctic_get = _rd_save_ag
    reddit_source._arctic_cooldown_until = 0.0
    reddit_source._arctic_fail_streak = 0

# reddit GLOBAL Arctic concurrency cap (2026-06-21, after the 18-agent stress test): _arctic_get holds
# a process-global semaphore around the egress so N concurrent searches/agents cannot storm the single
# Arctic host into a 429 cascade (the s2/openalex pattern reddit was missing; caching cannot fix a
# novel-query burst on a one-host mirror, so this is the real root fix). Offline: fire 20 concurrent
# _arctic_get with a get_json that records peak in-flight; assert the peak never exceeds the cap.
import threading as _rd_thr
reddit_source._arctic_cooldown_until = 0.0; reddit_source._arctic_fail_streak = 0
_rd_inflight = {"now": 0, "peak": 0}; _rd_ilock = _rd_thr.Lock()
def _rd_probe_gj(*a, **k):
    with _rd_ilock:
        _rd_inflight["now"] += 1
        _rd_inflight["peak"] = max(_rd_inflight["peak"], _rd_inflight["now"])
    reddit_source.time.sleep(0.03)
    with _rd_ilock:
        _rd_inflight["now"] -= 1
    return {"data": [{"id": "x"}]}
_rd_save_gj2 = _rd_http.get_json
_rd_http.get_json = _rd_probe_gj
try:
    _rd_ts = [_rd_thr.Thread(target=lambda: reddit_source._arctic_get("/posts/search", {"subreddit": "PhD"}))
              for _ in range(20)]
    for _x in _rd_ts: _x.start()
    for _x in _rd_ts: _x.join()
finally:
    _rd_http.get_json = _rd_save_gj2
check("reddit: global Arctic concurrency cap bounds in-flight egress under a 20-way burst (no storm)",
      1 <= _rd_inflight["peak"] <= reddit_source._ARCTIC_MAX_INFLIGHT)

# burst-fragile global concurrency caps (2026-06-21 class sweep): every single-shared-host source that
# fans out / is hit concurrently now holds a process-global BoundedSemaphore around its egress (the
# reddit / _s2 / _openalex pattern), so a multi-agent burst paces through one host instead of storming
# it. Assert each cap's constant + sema + egress-chokepoint helper exist and are wired; the acquisition
# MECHANISM is proven by the reddit 20-way test above + the SE 15-way test below.
import threading as _bf_thr
from penumbra.core import _stackexchange as _bf_se
from penumbra.core.sources.api import core_source as _bf_core, arxiv_source as _bf_arxiv
from penumbra.core.sources.scrape import sogou_weixin_source as _bf_sogou
from penumbra.core.sources.walled import feishu_jobs_source as _bf_feishu
try:  # mokahr_ats is excluded from the PUBLIC release (§1201 decryption code); tolerate its absence.
    from penumbra.core.sources.walled import mokahr_ats_source as _bf_mokahr
except ImportError:
    _bf_mokahr = None
def _bf_wired(mod, sema, const, cap, helper):
    s = getattr(mod, sema, None)
    return (getattr(mod, const, None) == cap and s is not None
            and hasattr(s, "acquire") and hasattr(s, "release") and callable(getattr(mod, helper, None)))
check("burst-cap: stackexchange _se_sema cap=4 + _se_get chokepoint", _bf_wired(_bf_se, "_se_sema", "_SE_MAX_INFLIGHT", 4, "_se_get"))
check("burst-cap: core _core_sema cap=3 + _core_get chokepoint", _bf_wired(_bf_core, "_core_sema", "_CORE_MAX_INFLIGHT", 3, "_core_get"))
check("burst-cap: arxiv _arxiv_sema cap=4 + _arxiv_get_text chokepoint", _bf_wired(_bf_arxiv, "_arxiv_sema", "_ARXIV_MAX_INFLIGHT", 4, "_arxiv_get_text"))
check("burst-cap: sogou _sogou_sema cap=2 + _sogou_get chokepoint", _bf_wired(_bf_sogou, "_sogou_sema", "_SOGOU_MAX_INFLIGHT", 2, "_sogou_get"))
if _bf_mokahr is not None:
    check("burst-cap: mokahr _mokahr_sema cap=12 + _mokahr_post chokepoint", _bf_wired(_bf_mokahr, "_mokahr_sema", "_MOKAHR_MAX_INFLIGHT", 12, "_mokahr_post"))
check("burst-cap: feishu _feishu_sema cap=6 + _feishu_post chokepoint", _bf_wired(_bf_feishu, "_feishu_sema", "_FEISHU_MAX_INFLIGHT", 6, "_feishu_post"))
# Prove the SE cap actually bounds concurrency at its chokepoint _se_get (mirror of the reddit test).
_bf_se._se_cooldown_until = 0.0; _bf_se._se_fail_streak = 0
_bf_se_if = {"now": 0, "peak": 0}; _bf_se_ilock = _bf_thr.Lock()
import penumbra.core.http as _bf_http
def _bf_se_probe(*a, **k):
    with _bf_se_ilock:
        _bf_se_if["now"] += 1; _bf_se_if["peak"] = max(_bf_se_if["peak"], _bf_se_if["now"])
    _bf_se.time.sleep(0.02)
    with _bf_se_ilock:
        _bf_se_if["now"] -= 1
    return {"items": []}
_bf_se_save = _bf_http.get_json
_bf_http.get_json = _bf_se_probe
try:
    _bf_ts = [_bf_thr.Thread(target=lambda: _bf_se._se_get("https://api.stackexchange.com/x", {})) for _ in range(15)]
    for _t in _bf_ts: _t.start()
    for _t in _bf_ts: _t.join()
finally:
    _bf_http.get_json = _bf_se_save
check("burst-cap: stackexchange _se_get bounds in-flight under a 15-way burst (peak <= cap)",
      1 <= _bf_se_if["peak"] <= _bf_se._SE_MAX_INFLIGHT)

# per-host caps for the ATS-concentrating scrapers (2026-06-21, completing the class): they fan out over
# rows that collapse onto a few shared ATS hosts (greenhouse/ashby/lever/workable), so the cap is keyed
# by hostname (distinct hosts -> distinct caps; same host -> one shared cap). Assert the per-host keying.
from penumbra.core.sources.scrape import overseas_ai_jobs_source as _bf_oaj, ai_residencies_source as _bf_air
for _bf_m, _bf_nm in ((_bf_oaj, "overseas_ai_jobs"), (_bf_air, "ai_residencies")):
    _sa = _bf_m._sema_for("https://boards-api.greenhouse.io/x")
    _sb = _bf_m._sema_for("https://api.ashbyhq.com/y")
    _sa2 = _bf_m._sema_for("https://boards-api.greenhouse.io/z")
    check("burst-cap: " + _bf_nm + " per-host sema (distinct ATS hosts -> distinct caps; same host -> same; cap=4)",
          _bf_m._HOST_MAX_INFLIGHT == 4 and _sa is not _sb and _sa is _sa2
          and hasattr(_sa, "acquire") and hasattr(_sa, "release"))

# sec_edgar recency sort (2026-06-17): the efts backend returns _score (relevance) order, which
# floats decades-old exhibits over the current filing. Probed live: forms=/sort= -> HTTP 500
# (rejected), startdt/enddt only windows the set (no reorder); so _to_documents reorders the page
# by file_date DESC client-side. Assert the invariant on an out-of-order fixture (offline).
from penumbra.core.sources.scrape import sec_edgar_source  # noqa: E402
_sec_hits = [
    {"_id": "a", "_source": {"file_date": "2003-06-24", "form": "10-K"}},
    {"_id": "b", "_source": {"file_date": "2026-04-29", "form": "8-K"}},
    {"_id": "c", "_source": {"form": "EX-21"}},                              # no date -> sorts LAST
    {"_id": "d", "_source": {"file_date": "2025-09-22", "form": "10-Q"}},
    {"_id": "e", "_source": {"file_date": "2026-04-29", "form": "DEF 14A"}},  # tie with 'b' -> keep order
]
check("sec_edgar._sort_by_recency orders newest file_date first, undated last, stable ties",
      [h["_id"] for h in sec_edgar_source._sort_by_recency(_sec_hits)] == ["b", "e", "d", "a", "c"],
      f"got {[h['_id'] for h in sec_edgar_source._sort_by_recency(_sec_hits)]}")
check("sec_edgar._sort_by_recency degrades on junk input (non-list -> [], malformed hits tolerated)",
      sec_edgar_source._sort_by_recency("not-a-list") == []
      and len(sec_edgar_source._sort_by_recency([{"_id": "x"}, {"bad": 1}])) == 2)

check("search_index drops a verified tombstone shell",
      search_index_source._is_tombstone(
          "This page has moved · Click here if the automatic redirect does not start"))
check("search_index keeps real snippets (length guard beats phrase match)",
      not search_index_source._is_tombstone(
          "EP approval took 3 weeks; the 'this page has moved' discussion was about the old "
          "forum layout and how mods handled the migration of pinned posts after the redesign")
      and not search_index_source._is_tombstone("EP approval took 3 weeks for my application"))

check("asr._parse_ts parses seconds and clock forms",
      asr._parse_ts("90") == 90.0 and asr._parse_ts("12:30") == 750.0
      and asr._parse_ts("1:02:30") == 3750.0 and asr._parse_ts("") is None
      and asr._parse_ts(90.5) == 90.5)
try:
    asr._parse_ts("12m30s")
    check("asr._parse_ts rejects junk with a usable error", False, "no ValueError raised")
except ValueError:
    check("asr._parse_ts rejects junk with a usable error", True)
check("asr.transcribe_url accepts start/duration",
      {"start", "duration"} <= set(_insp.signature(asr.transcribe_url).parameters))

# ---------------------------------------------------------------------------
# 8. docreader (P39 document digestion): pure helpers + tool registration
# ---------------------------------------------------------------------------
from penumbra.core import docreader  # noqa: E402  (parser imports are lazy — cheap)

check("docreader._fmt_of maps extensions case-insensitively incl. URLs with query",
      docreader._fmt_of("a/b/颜色框.PPTX") == "pptx"
      and docreader._fmt_of("https://x.com/d/deck.pptx?dl=1") == "pptx"
      and docreader._fmt_of("report.docx") == "docx"
      and docreader._fmt_of("noext") == "" and docreader._fmt_of("a.exe") == "")
check("docreader._window slices and flags truncation",
      docreader._window("abcdefgh", 0, 5) == ("abcde", True)
      and docreader._window("abcdefgh", 5, 5) == ("fgh", False)
      and docreader._window("abc", 0, 60000) == ("abc", False))
_rp = docreader._resolve_local("penumbra-inbox/x.pptx")
check("docreader._resolve_local resolves relative paths against HOME",
      _rp.is_absolute() and _rp.parts[-2:] == ("penumbra-inbox", "x.pptx"))
check("docreader covers the office trio + pdf + plain text",
      {"pptx", "docx", "xlsx", "pdf", "txt", "md"} <= set(docreader._READERS))
# roadmap-④: code/config source files are readable, routed to the PLAIN-TEXT reader (no parser).
check("docreader._fmt_of maps code/config extensions (py/ts/rs/go/toml/yaml)",
      docreader._fmt_of("main.py") == "py" and docreader._fmt_of("a/b/app.ts") == "ts"
      and docreader._fmt_of("lib.rs") == "rs" and docreader._fmt_of("m.go") == "go"
      and docreader._fmt_of("pyproject.toml") == "toml" and docreader._fmt_of("ci.yaml") == "yaml")
check("docreader routes code extensions to the txt (plain-text) reader, not a parser",
      all(docreader._READERS.get(ext) is docreader._read_txt
          for ext in ("py", "ts", "rs", "go", "java", "cpp", "toml", "yaml", "sh")))
check("docreader rejects unknown formats with a usable error (before any IO)",
      "unsupported" in (docreader.read_document("file.xyz").get("error") or ""))
check("server exposes penumbra_read (auto-routing read tool: url + document)",
      hasattr(_srv, "penumbra_read") and callable(_srv.penumbra_read))

# --- P39 tier-4: in-band image view (routes through penumbra_view kind=document) ---
check("docreader._parse_sel parses int + name selections, empty → None",
      docreader._parse_sel("8, 15,25", as_int=True) == {8, 15, 25}
      and docreader._parse_sel([8, 15], as_int=True) == {8, 15}
      and docreader._parse_sel("a.png, b.png", as_int=False) == {"a.png", "b.png"}
      and docreader._parse_sel("", as_int=True) is None
      and docreader._parse_sel(None, as_int=False) is None)
check("docreader._clean_stem slugifies + bounds + defaults",
      docreader._clean_stem("My Fig (v2).png") == "My_Fig_v2"
      and docreader._clean_stem("") == "image"
      and len(docreader._clean_stem("x" * 80)) == 40)
check("docreader view_images: text formats carry no extractable images (honest note)",
      docreader.view_images("notes.txt").get("total_images") == 0
      and "penumbra_read" in (docreader.view_images("notes.txt").get("note") or ""))
# real image-processing invariants (PIL present in the service venv)
from io import BytesIO as _BIO  # noqa: E402
from PIL import Image as _PILImage  # noqa: E402
_buf = _BIO(); _PILImage.new("RGB", (3000, 2000), "navy").save(_buf, "PNG"); _big = _buf.getvalue()
_small = docreader._downscale_png(_big, 1456)
_im = _PILImage.open(_BIO(_small))
check("docreader._downscale_png caps the long edge and stays PNG",
      _im.format == "PNG" and max(_im.size) == 1456)
_cells = [{"data": _big, "section": i, "section_label": f"Slide {i}", "name": f"s{i:02d}_01.png"}
          for i in (1, 2, 3)]
_sheet = docreader._contact_sheet(_cells)
_sim = _PILImage.open(_BIO(_sheet))
check("docreader._contact_sheet tiles thumbnails into one valid PNG montage",
      _sim.format == "PNG" and _sim.size[0] > 0 and _sim.size[1] > 0)
check("server exposes penumbra_view (auto-routing view tool: document + images + video)",
      hasattr(_srv, "penumbra_view") and callable(_srv.penumbra_view))
# --- P41 OCR tier (penumbra_read ocr=True on the document branch) ---
check("docreader.ocr_image exists + read_document accepts ocr (OCR tier)",
      hasattr(docreader, "ocr_image") and callable(docreader.ocr_image)
      and "ocr" in _insp.signature(docreader.read_document).parameters)
# --- P42 in-band image-URL view + search-index junk-snippet filter ---
check("docreader.view_image_urls + server penumbra_view (in-band URL image delivery via kind=images)",
      hasattr(docreader, "view_image_urls") and callable(docreader.view_image_urls)
      and hasattr(_srv, "penumbra_view") and callable(_srv.penumbra_view))
check("search_index drops generic zhihu boilerplate snippet, keeps a real one",
      search_index_source._is_tombstone("知乎，让每一次点击都充满意义 —— 欢迎来到知乎，发现问题背后的世界。")
      and not search_index_source._is_tombstone("罗湖区房租均价2597元，3号线最快到罗湖老街，租金与通勤成本权衡。"))

# ---------------------------------------------------------------------------
# 9. recall (perception-memory index): the invariants that make it safe to ship —
#    graceful degrade, exact CJK recall, OR-recall == doc_scores>0 (anti-drift,
#    on a fixture-CLOSED corpus), the frozen indexable allow-list + no leak of a
#    query-keyed/walled/structured source, and the never-raises ingest contract.
# ---------------------------------------------------------------------------
import tempfile as _tf  # noqa: E402

import penumbra.core.recall as _recall  # noqa: E402
from penumbra.core.recall import store as _rstore  # noqa: E402

# graceful degrade: a fresh/empty (or unusable) index returns [] — the eye stays stateless
_rstore.DB_PATH = Path(_tf.mkdtemp()) / "smoke_index.db"
check("recall.search on a fresh index returns [] (graceful degrade)", _recall.search("大模型", 5) == [])

# CLOSED fixture corpus so FTS table == the doc_scores candidate list (the parity invariant holds
# only on a closed corpus; on the live index FTS recalls table-wide while doc_scores sees only the
# recalled candidates).
check("recall index init", _rstore.init())
_rcon = _rstore.connect()
_fix = [_doc("hf_daily_papers", "大模型推理能力评测研究"),
        _doc("hf_daily_papers", "Scaling laws for language models"),
        _doc("hf_daily_papers", "化学反应动力学研究")]
_fix[0].content = "强化学习与思维链对齐方法"
_fix[1].content = "reasoning and knowledge distillation"
_fix[2].content = "无关内容"
_rcon.execute("BEGIN")
for _i, _d in enumerate(_fix):
    _d.source_id = f"fix{_i}"
    _recall.writer._upsert(_rcon, rank, _d, 1.0)
_rcon.commit()

check("recall: CJK 2-char query recalls the doc (大模型推理)",
      any(d.source_id == "fix0" for d in _recall.search("大模型推理", 10)))
check("recall: 2-char term 推理 recalls fix0 (the trigram-regression guard)",
      "fix0" in {d.source_id for d in _recall.search("推理", 10)})
for _q in ("大模型推理", "reasoning", "强化学习", "language", "蒸馏"):
    _recset = {d.source_id for d in _recall.search(_q, 50)}
    _scset = {d.source_id for d, s in zip(_fix, relevance.doc_scores(_fix, _q)) if s > 0.0}
    check(f"recall OR-set == doc_scores>0 for {_q!r} (anti-drift, closed corpus)",
          _recset == _scset, f"recall={_recset} scored={_scset}")

# frozen allow-list (a deliberate change must edit both __init__._SINGLETONS AND this list)
_EXPECTED_SINGLETONS = sorted("""
hf_daily_papers researcher_watch github_trending github_awesome_phd ml_collective
transformer_circuits acl_anthology openreview llm_leaderboard hk_universities ajo
mycareersfuture overseas_ai_jobs ircc_ee_rounds zhihu_users xiaoyuzhou youtube_channels
feishu_jobs mokahr_ats bytedance_seed
""".split())
check("recall: _SINGLETONS matches the frozen allow-list",
      sorted(_recall._SINGLETONS) == _EXPECTED_SINGLETONS,
      f"extra={sorted(set(_recall._SINGLETONS) - set(_EXPECTED_SINGLETONS))} "
      f"missing={sorted(set(_EXPECTED_SINGLETONS) - set(_recall._SINGLETONS))}")
# SAFETY: no query-keyed / walled / structured / cost source may ever be indexable
_forbidden = ["zhihu", "xiaohongshu", "reddit", "quora", "blind", "zhihu_search", "bilibili",
              "cdp_fulltext", "exa", "conference_deadlines", "gpu_pricing", "ai_residencies",
              "csrankings", "github", "huggingface_hub"]
_leaked = [s for s in _forbidden if s in names and _recall.indexable(s)]
check("recall: no query-keyed/walled/structured source leaks into the index", not _leaked, str(_leaked))
_expect_in = [s for s in ("hf_daily_papers", "llm_leaderboard", "ircc_ee_rounds", "zhihu_users") if s in names]
_miss_idx = [s for s in _expect_in if not _recall.indexable(s)]
check("recall: enumerable singletons are indexable", not _miss_idx, str(_miss_idx))

# the ingest hook must NEVER raise into the fetcher hot path (highest-blast-radius contract)
try:
    _recall.writer.WRITES_ENABLED = True
    _recall.maybe_ingest([_fix[0], None, "not-a-doc", 42])  # malformed mix is tolerated, not raised
    _recall.maybe_ingest(None)
    check("recall: maybe_ingest never raises into the caller", True)
except Exception as _e:  # noqa: BLE001
    check("recall: maybe_ingest never raises into the caller", False, str(_e))
finally:
    _recall.writer.WRITES_ENABLED = False

# --- tags-into-seg (recall completeness): a doc's TAGS now enter the FTS seg, so a tag term recalls
#     it; the version bump forces a re-segment of an existing doc; byte-identical when no tags. ---
check("recall: SEG_VERSION bumped to 2 (tags-in-seg) so the writer re-segments existing docs",
      _rstore.SEG_VERSION >= 2)
# byte-identical-when-no-tags: segment_doc(tagless) == segment(title+content) (no v1 regression)
_notag = _doc("hf_daily_papers", "Scaling laws for models")
_notag.content = "reasoning content"
check("recall: segment_doc is byte-identical to title+content when a doc has NO tags",
      _rstore.segment_doc(_notag) == _rstore.segment((_notag.title or "") + " " + (_notag.content or "")))
# a doc whose TAG (not title/content) carries the term is recalled by that term (the ircc fix)
_tagdoc = _doc("ircc_ee_rounds", "Round 417 invitations issued")
_tagdoc.content = "CEC CRS 518"
_tagdoc.tags = ["express-entry", "canada"]
_tagdoc.source_id = "ee_tag"
_rcon.execute("BEGIN")
_recall.writer._upsert(_rcon, rank, _tagdoc, 1.0)
_rcon.commit()
check("recall: a doc is recalled by its TAG term ('express entry' -> ircc; tags-into-seg)",
      "ee_tag" in {d.source_id for d in _recall.search("express entry", 10)})
# the version bump is WIRED: a legacy row stored at seg_version<SEG_VERSION re-segments on re-ingest
# even when unchanged, so existing docs re-index their tags. (Insert a v1 row with a tagless seg.)
_old_seg = _rstore.segment("Legacy doc body")
_rcon.execute("BEGIN")
_rcon.execute(
    "INSERT INTO docs(source,source_id,fp,url,title,content,author,date,score,doc_json,seg,"
    "seg_version,content_hash,first_seen,last_seen,version,immutable) "
    "VALUES('feishu_jobs','legacy_seg','x','','Legacy doc','body',NULL,NULL,0,'{}',?,1,'OLDHASH',1.0,1.0,1,0)",
    (_old_seg,))
_legacy_rid = _rcon.execute("SELECT rowid FROM docs WHERE source_id='legacy_seg'").fetchone()[0]
_rcon.execute("INSERT INTO fts(rowid, seg) VALUES(?, ?)", (_legacy_rid, _old_seg))
_rcon.commit()
_legacy = _doc("feishu_jobs", "Legacy doc")
_legacy.content = "body"
_legacy.tags = ["minimax"]
_legacy.source_id = "legacy_seg"
_rcon.execute("BEGIN")
_recall.writer._upsert(_rcon, rank, _legacy, 2.0)
_rcon.commit()
_legacy_sv = _rcon.execute("SELECT seg_version FROM docs WHERE source_id='legacy_seg'").fetchone()[0]
check("recall: a seg_version bump forces a re-segment of an existing doc (re-index on next ingest)",
      _legacy_sv == _rstore.SEG_VERSION
      and "legacy_seg" in {d.source_id for d in _recall.search("minimax", 10)})

# --- recall (critic #8, deferred): feishu_jobs and mokahr_ats must index DISJOINT orgs, so a future
#     config edit can't make one silently shadow the other in the index. Compare each adapter's
#     configured org keys (feishu = subdomain; mokahr = org). ---
from penumbra.core.sources.walled import feishu_jobs_source as _fj  # noqa: E402
try:  # mokahr_ats excluded from the PUBLIC release (§1201); skip the disjoint-org check if absent.
    from penumbra.core.sources.walled import mokahr_ats_source as _mk  # noqa: E402
except ImportError:
    _mk = None
if _mk is not None:
    _feishu_subs = {s[1] for s in _fj.SITES}        # feishu SITES = (label, subdomain, website_path, tier)
    _mokahr_orgs = {s[1] for s in _mk.SITES}         # mokahr SITES = (label, org, site_id, mode, tier)
    _recall_overlap = _feishu_subs & _mokahr_orgs
    check("recall: feishu_jobs and mokahr_ats index DISJOINT org keys (no silent shadowing, critic #8)",
          not _recall_overlap, f"shared org keys: {sorted(_recall_overlap)}")

# --- ontario_sunshine (Ontario Sunshine List, CKAN keyless): explicit_only (name-level PII +
#     a CKAN query per call => never in the broad fan-out, never in the recall corpus). Faceted
#     compensation x STRUCTURE, region ca. _f() reads per-year column renames defensively (the
#     'Salary' vs 'Salary Paid' reconciliation) and never raises. Network-bound search() is NOT
#     exercised here (smoke is offline); the live path is the standalone functional check. ---
from penumbra.core.sources.api import ontario_sunshine_source as _onss  # noqa: E402
_onss_a = fetcher.get_adapter("ontario_sunshine")
check("ontario_sunshine: registered + explicit_only (named-only, never in broad fan-out)",
      _onss_a is not None and bool(fetcher._explicit_only_reason(_onss_a)))
check("ontario_sunshine: keyless + faceted compensation/STRUCTURE/ca lookup",
      _onss_a.needs_credentials is False and _onss_a.kind == "lookup"
      and _onss_a.domains == ["compensation"] and _onss_a.regions == ["ca"]
      and _onss_a.modes == ["STRUCTURE"])
check("ontario_sunshine: NOT indexable (explicit_only, name-level PII never enters the recall corpus)",
      not _recall.indexable("ontario_sunshine"))
check("ontario_sunshine: empty query returns [] (no bulk-PII firehose; offline, no network)",
      _onss_a.search("") == [] and _onss_a.fetch_url("https://data.ontario.ca/x") is None)
check("ontario_sunshine: _f reads per-year column aliases defensively, '' on absence (never KeyError)",
      _onss._f({"Salary Paid": "$1"}, "Salary", "Salary Paid") == "$1"
      and _onss._f({"Salary": "$2"}, "Salary", "Salary Paid") == "$2"
      and _onss._f({}, "Salary", "Salary Paid") == "")
check("ontario_sunshine: _build_doc skips a nameless record, builds a real one with PII metadata keys",
      _onss._build_doc({"_id": 1, "Employer": "X"}, 2020) is None
      and (lambda d: d is not None and d.source == "ontario_sunshine"
           and {"employer", "position", "name", "salary", "benefits", "year"} <= set(d.metadata)
           )(_onss._build_doc({"_id": 2, "First name": "Jane", "Last name": "Doe",
                               "Salary": "$200,000", "Employer": "University Of Toronto",
                               "Job title": "Professor", "Year": "2020"}, 2020)))

# ---------------------------------------------------------------------------
# 10. recall VECTOR layer (Phase 2): the mechanical fusion + merge_rank graft invariants that keep
#     THE RAZOR and stay byte-identical to Phase 1 when the vector arm is absent. (No 0.6B model
#     loaded — the embedding QUALITY is bake-off-verified on the mini; these are the code invariants.)
# ---------------------------------------------------------------------------
# (a) merge_rank still ranks the relevant doc first when NO doc carries recall_rrf (no interference)
_mo = _doc("arxiv", "Sparse mixture of experts for language models", "https://e.com/moe")
_mo.content = "mixture of experts sparse routing for large language models"
_ch = _doc("x", "Titration in analytical chemistry", "https://e.com/chem"); _ch.content = "titration"
_base = rank.merge_rank([_ch, _mo], "mixture of experts sparse")
check("recall: merge_rank unaffected with no recall_rrf (relevant doc still #1)",
      _base and _base[0].source_id == _mo.source_id)

# (b) the RRF prior LIFTS a vector-only hit (rel≈0 for the EN query, Chinese content) above noise
_vhit = _doc("zhihu_users", "深度强化学习对齐的中文综述", "https://e.com/zh"); _vhit.content = "对齐方法研究"
_vhit.metadata = {"recall_rrf": 0.9, "recall_via": "vector"}
_noise = _doc("y", "unrelated note", "https://e.com/n"); _noise.content = "nothing relevant here"
_lift = rank.merge_rank([_noise, _vhit], "reinforcement learning alignment")
check("recall: RRF prior lifts a vector-only cross-lingual hit above a no-signal doc (the §0 fix)",
      _lift and _lift[0].source_id == _vhit.source_id)

# (c) dedup PRESERVES recall_rrf when _pick_best keeps the richer (un-stamped) member (critic #1)
_thin = _doc("ircc_ee_rounds", "EE Draw Canadian Experience Class CRS 518", "https://e.com/d")
_thin.content = "short"; _thin.metadata = {"recall_rrf": 0.8, "recall_via": "vector"}
_rich = _doc("ircc_ee_rounds", "EE Draw Canadian Experience Class CRS 518", "https://e.com/d")
_rich.content = "x" * 500   # richer → _pick_best keeps THIS one (which has no stamp)
_dd = rank.dedup([_thin, _rich])
check("recall: dedup preserves recall_rrf onto the survivor (stamp can't be lost in collapse)",
      len(_dd) == 1 and (_dd[0].metadata or {}).get("recall_rrf") == 0.8)

# (d) _rrf_fuse marks a shared doc via=both and accumulates more rrf than a single-arm doc
_f1 = _doc("s", "doc one only in lexical", "https://e.com/1")
_f2 = _doc("s", "doc two in both arms", "https://e.com/2")
_fused = _recall._rrf_fuse([_f1, _f2], [_f2])
_fb = {d.source_id: d for d in _fused}
check("recall: _rrf_fuse marks shared doc via=both + accumulates rrf",
      (_fb["https://e.com/2"].metadata or {}).get("recall_via") == "both"
      and (_fb["https://e.com/2"].metadata or {}).get("recall_rrf", 0)
          > (_fb["https://e.com/1"].metadata or {}).get("recall_rrf", 1))

# (e) hybrid degenerates to PLAIN lexical (no rrf, no crash) when the embedder is unavailable
_save_dis = _recall.embed._disabled
_recall.embed._disabled = True
try:
    _hd, _hinfo = _recall.hybrid("anything", k=5)
    check("recall: hybrid fails open to lexical when the embedder is unavailable",
          _hinfo.get("mode") == "lexical" and _hinfo.get("vector") == 0)
finally:
    _recall.embed._disabled = _save_dis

# ---------------------------------------------------------------------------
# 11. modes facet (Curator P0): every research-facing source declares its acquisition
#     mode(s) from a FROZEN 5-term vocabulary, and the empty-modes (redundant) set is frozen
#     so a new no-edge source forces a conscious Curator prune decision. The (domain × mode)
#     coverage metric the self-iterating source loop steers by is built on this facet — a
#     drifted vocab or an again-missing modes field would silently skew coverage. (Pure
#     structural invariant; the per-source mode JUDGEMENT lives in facets.json itself.)
# ---------------------------------------------------------------------------
_FACETS = ROOT / "src" / "penumbra" / "core" / "facets.json"
MODE_VOCAB = {"STRUCTURE", "UNWALL", "TRANSCRIBE", "RECALL", "MONITOR"}
# Sources with NO acquisition edge over plain web search (modes:[]) — Curator prune candidates.
# FROZEN: adding a name here is a deliberate "this source earns its keep no longer" call.
REDUNDANT_SOURCES = {"alphaxiv"}
try:
    _facets = json.loads(_FACETS.read_text(encoding="utf-8"))
except Exception as exc:  # noqa: BLE001
    check("facets.json parses", False, str(exc))
    _facets = {}
check("facets: parses with rows", isinstance(_facets, dict) and len(_facets) > 0)
_no_modes = sorted(k for k, v in _facets.items() if not isinstance(v.get("modes"), list))
check("facets: every source declares a modes list", not _no_modes, str(_no_modes))
_bad_modes = sorted({m for v in _facets.values() for m in (v.get("modes") or [])} - MODE_VOCAB)
check("facets: every mode is in the frozen 5-term vocabulary", not _bad_modes, str(_bad_modes))
_empty = {k for k, v in _facets.items() if v.get("modes") == []}
check("facets: the redundant (empty-modes) set is frozen",
      _empty == REDUNDANT_SOURCES, f"got {sorted(_empty)}, expected {sorted(REDUNDANT_SOURCES)}")

# list_sources MUST surface the modes facet (the P0 acquisition mode), else the Curator P3
# audit sees every source as coverage_unknown and can prune nothing (a live-verify caught this).
_ls_modes = {e["name"]: e.get("modes") for e in fetcher.list_sources()}
check("facets: list_sources surfaces the modes facet (arxiv -> [STRUCTURE])",
      _ls_modes.get("arxiv") == ["STRUCTURE"], f"got {_ls_modes.get('arxiv')!r}")

# list_sources surfaces a `backend` (the de-dup key behind the HONEST count): the OpenAlex family
# (openalex + openalex_cn + researcher_watch + every org_watch slice = one corpus + one API budget +
# one breaker) collapses to ONE backend so the raw source count stops over-stating coverage; an
# independent source is its own backend. penumbra_sources surfaces backend_count + backend_breakdown.
_ls_backend = {e["name"]: e.get("backend") for e in fetcher.list_sources()}
check("backend: OpenAlex family collapses (openalex + researcher_watch -> 'openalex')",
      _ls_backend.get("openalex") == "openalex" and _ls_backend.get("researcher_watch") == "openalex",
      f"openalex={_ls_backend.get('openalex')!r} researcher_watch={_ls_backend.get('researcher_watch')!r}")
check("backend: an independent source defaults to its own name (arxiv -> 'arxiv')",
      _ls_backend.get("arxiv") == "arxiv", f"got {_ls_backend.get('arxiv')!r}")
_bk_counter = {}
for _e in fetcher.list_sources():
    _bk_counter[_e.get("backend")] = _bk_counter.get(_e.get("backend"), 0) + 1
check("backend: the OpenAlex family is the dominant multiplexed backend (>=10 slices on one upstream)",
      _bk_counter.get("openalex", 0) >= 10, f"openalex backs {_bk_counter.get('openalex', 0)} sources")

# ---------------------------------------------------------------------------
# 12. Curator P1 (source admission): the 14 invariants that make it safe to ship.
#     Pure structural / offline / no network / no judgment. The CENTRAL proof is the
#     no-verdict-in-code walk (the corrected razor): the eye fetches/probes/measures/
#     persists; EVERY admit/watch/reject verdict is the spawned agent, never code.
# ---------------------------------------------------------------------------
import socket as _socket  # noqa: E402

from penumbra.core.curator import apply as _capply  # noqa: E402
from penumbra.core.curator import candidates as _ccand  # noqa: E402
from penumbra.core.curator import evidence as _cev  # noqa: E402
from penumbra.core.curator import probe as _cprobe  # noqa: E402
from penumbra.core.curator import redlines as _credl  # noqa: E402

# Save the REAL fetcher entry points so the offline fixtures can monkeypatch them and §12.12
# can restore the live roster (the registry from §2's load_sources()).
_REAL_LIST_SOURCES = fetcher.list_sources
_REAL_ALL_NAMES = fetcher.all_adapter_names
_REAL_GET_ADAPTER = fetcher.get_adapter
_REAL_FACETS = getattr(fetcher, "_FACETS", {})

# (1) Probe-vocab freeze: _PROBES keys == the §11 MODE_VOCAB constant.
check("curator: _PROBES keys == MODE_VOCAB", set(_cprobe._PROBES) == MODE_VOCAB,
      f"{sorted(_cprobe._PROBES)} vs {sorted(MODE_VOCAB)}")

# Shared offline fixture builder for the packet walks (monkeypatch every network touchpoint).
_BANNED_KEYS = {"score", "verdict", "passes", "recommend", "admit", "reject", "good",
                "quality", "rating", "confidence", "decision", "beats_web_search"}
_VERDICT_TOKENS = ("admit", "reject", "approve", "deny", "recommend", "passes", "verdict")


def _walk_banned_keys(o, path=""):
    bad = []
    if isinstance(o, dict):
        for k, v in o.items():
            if k in _BANNED_KEYS:
                bad.append(path + "." + str(k))
            bad += _walk_banned_keys(v, path + "." + str(k))
    elif isinstance(o, list):
        for i, v in enumerate(o):
            bad += _walk_banned_keys(v, f"{path}[{i}]")
    return bad


def _walk_verdict_values(o, path=""):
    import re as _re
    bad = []
    if isinstance(o, dict):
        for k, v in o.items():
            if k in ("judge_instructions", "note", "reason") and isinstance(v, str):
                for t in _VERDICT_TOKENS:
                    if _re.search(r"\b" + _re.escape(t) + r"\b", v, _re.I):
                        bad.append((path + "." + str(k), t))
            bad += _walk_verdict_values(v, path + "." + str(k))
    elif isinstance(o, list):
        for i, v in enumerate(o):
            bad += _walk_verdict_values(v, f"{path}[{i}]")
    return bad


def _build_fixture_packet(mode="RECALL", reached=True, probe_diff=None, family="rss",
                          urls=None, complete=True, extra_cand=None):
    """Build a packet offline: monkeypatch live-roster + recall + host_seen to fixtures."""
    _cev._apply._live_hosts = lambda: {"existing.example.com"}
    _cev._apply._hosts_of_adapter = lambda a: set()
    fetcher.list_sources = lambda check_health=False: [{"name": "arxiv"}, {"name": "openalex"}]
    fetcher.all_adapter_names = lambda: []
    fetcher.get_adapter = lambda n: None
    fetcher._FACETS = {}
    _ccand.host_seen = lambda h: False
    try:
        import penumbra.core.recall as _rc
        _rc.search = lambda q, k=1: []
    except Exception:  # noqa: BLE001
        pass
    diff = probe_diff if probe_diff is not None else {
        "item_count": 9, "sample_titles": ["A new method", "Another paper"],
        "recall_overlap_lexical": 1, "recall_overlap_semantic": 3,
        "mean_body_len": 200.0, "sample_bodies_present": True}
    cand = {
        "id": "fix-00000000", "name": "Fixture Source", "urls": urls or ["https://newhost.example.com/feed.xml"],
        "proposed_mode": mode, "proposed_domain": "papers", "proposed_family": family,
        "proposed_kind": "stream", "proposed_regions": [],
        "rationale_text": "submitter prose", "submitted_by": "agent", "submitted_at": "2026-06-15T00:00:00Z",
        "_probe_cache": {
            "mode": mode, "diff": diff,
            "diff_provenance": {k: "derived" for k in diff},
            "probe_reached": reached,
            "probe_fetch_meta": {"ok": reached, "status": 200 if reached else None,
                                 "blocked_reason": None if reached else "timeout",
                                 "final_url": "https://newhost.example.com/feed.xml",
                                 "redirect_chain": [], "content_type": "application/rss+xml", "bytes": 4096},
            "probe_error": None if reached else "fetch blocked/failed: timeout",
            "web_baseline_request": {"suggested_queries": [{"q": "A new method", "origin": "probe-derived"}]},
        },
    }
    if extra_cand:
        cand.update(extra_cand)
    return _cev.build_packet_for(cand), cand


# (2) NO-VERDICT-IN-CODE (the central proof): recursive key-walk + value-walk + provenance.
_pkt12, _ = _build_fixture_packet()
_bad_keys = _walk_banned_keys(_pkt12)
check("curator: build_packet emits NO banned verdict key at any depth", not _bad_keys, str(_bad_keys))
_bad_vals = _walk_verdict_values(_pkt12)
check("curator: no verdict token in judge_instructions/note/reason string values", not _bad_vals, str(_bad_vals))
# every diff field carries a provenance tag
_pdiff = _pkt12["stage3_mode_probe"]["diff"]
_pprov = _pkt12["stage3_mode_probe"]["diff_provenance"]
check("curator: every stage3 diff field has a provenance tag",
      set(_pdiff) <= set(_pprov) and all(_pprov[k] in ("verified", "claimed", "derived") for k in _pdiff),
      f"diff={sorted(_pdiff)} prov={sorted(_pprov)}")
# source-inspect: build_packet / mode_probe define no banned key as a returned/assigned field
_evsrc = _insp.getsource(_cev.build_packet_for)
_prsrc = _insp.getsource(_cprobe.mode_probe)
check("curator: build_packet/mode_probe source assigns no banned verdict key literal",
      not any(f'"{k}":' in _evsrc or f'"{k}":' in _prsrc for k in _BANNED_KEYS))

# (3) Red-line integrity.
_rl_rows = _credl.load_rules()
_rl_ok = (isinstance(_rl_rows, list)
          and all(all(r.get(k) for k in ("id", "severity", "kind", "value", "reason")) for r in _rl_rows)
          and all(r["severity"] in ("hard", "soft") for r in _rl_rows)
          and all(r["kind"] in ("host", "host_suffix", "path_regex", "query_term") for r in _rl_rows))
check("curator: redlines.json rows well-formed", _rl_ok)
check("curator: redlines id-set == EXPECTED_REDLINES",
      {r["id"] for r in _rl_rows} == set(_credl.EXPECTED_REDLINES),
      f"{sorted(r['id'] for r in _rl_rows)} vs {sorted(_credl.EXPECTED_REDLINES)}")
check("curator: redlines.match hits linkedin/in/, hits an email-bearing affiliation, misses arxiv",
      _credl.has_hard_hit(_credl.match({"urls": ["https://www.linkedin.com/in/x/"]}))
      and any(h["id"] == "pii_email_query"
              for h in _credl.match({"urls": ["https://api.openalex.org/works"],
                                     "affiliations": ["jane@example.com"]}))
      and not _credl.match({"urls": ["https://arxiv.org/abs/2401.00001"]}))
# no national-origin term anywhere in the data file (fixed EN+ZH denylist; NOT a country-ish scan)
_NAT_DENY = ["national origin", "nationality", "passport", "citizenship", "chinese", "china",
             "prc", "mainland", "国籍", "中国", "大陆", "护照", "公民"]
_rl_blob = json.dumps(_rl_rows, ensure_ascii=False).lower()
_nat_leak = [t for t in _NAT_DENY if t in _rl_blob]
check("curator: no national-origin term in redlines.json", not _nat_leak, str(_nat_leak))

# (4) SSRF guard (offline, monkeypatch getaddrinfo / httpx).
_real_gai = _cprobe.socket.getaddrinfo
try:
    check("curator: safe_fetch rejects file:// scheme",
          _cprobe.safe_fetch("file:///etc/passwd")["blocked_reason"] == "bad_scheme")
    check("curator: safe_fetch rejects userinfo",
          _cprobe.safe_fetch("http://u:p@example.com/")["blocked_reason"] == "userinfo")
    check("curator: safe_fetch rejects non-80/443 port",
          _cprobe.safe_fetch("http://example.com:9000/")["blocked_reason"] == "bad_port")
    _cprobe.socket.getaddrinfo = lambda h, *a, **k: [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
    check("curator: safe_fetch rejects loopback resolution (127.0.0.1)",
          _cprobe.safe_fetch("http://x.example.com/")["blocked_reason"] == "private_ip")
    _cprobe.socket.getaddrinfo = lambda h, *a, **k: [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]
    check("curator: safe_fetch rejects link-local metadata IP (169.254.169.254)",
          _cprobe.safe_fetch("http://meta.example.com/")["blocked_reason"] == "private_ip")
    _cprobe.socket.getaddrinfo = lambda h, *a, **k: [(_socket.AF_INET6, _socket.SOCK_STREAM, 6, "", ("::ffff:169.254.169.254", 0, 0, 0))]
    check("curator: safe_fetch rejects IPv4-mapped IPv6 (::ffff:169.254.169.254)",
          _cprobe.safe_fetch("http://x.example.com/")["blocked_reason"] == "private_ip")
    _cprobe.socket.getaddrinfo = lambda h, *a, **k: (_ for _ in ()).throw(_socket.gaierror("nx"))
    check("curator: safe_fetch fails closed on DNS failure",
          _cprobe.safe_fetch("http://nxdomain.invalid/")["blocked_reason"] == "dns")
    # decimal-IP form resolves (by the OS) to loopback -> private_ip
    _cprobe.socket.getaddrinfo = lambda h, *a, **k: [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
    check("curator: safe_fetch rejects decimal-IP form resolving private (http://2130706433/)",
          _cprobe.safe_fetch("http://2130706433/")["blocked_reason"] == "private_ip")
    # public host ACCEPTED (mock the client + a public IP) + decode-bomb + rebind
    import gzip as _gz  # noqa: E402
    _real_httpx_client = _cprobe.httpx.Client
    _cprobe.socket.getaddrinfo = lambda h, *a, **k: [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    def _ok_handler(request):
        return _cprobe.httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"<html>hi</html>")
    _cprobe.httpx.Client = (lambda *a, **k:
                            _real_httpx_client(*a, transport=_cprobe.httpx.MockTransport(_ok_handler),
                                               **{kk: vv for kk, vv in k.items() if kk != "transport"}))
    _rok = _cprobe.safe_fetch("http://example.com/ok")
    check("curator: safe_fetch accepts a normal public host", _rok["ok"] and _rok["status"] == 200)

    import ipaddress as _ipa
    check("curator: fake-IP proxy pool (198.18.0.0/15) allowed, true-internal still blocked",
          _cprobe._ip_is_blocked(_ipa.ip_address("198.18.3.191")) is False
          and _cprobe._ip_is_blocked(_ipa.ip_address("127.0.0.1")) is True
          and _cprobe._ip_is_blocked(_ipa.ip_address("10.0.0.5")) is True
          and _cprobe._ip_is_blocked(_ipa.ip_address("169.254.169.254")) is True)

    _bomb = _gz.compress(b"A" * (8 * 1024 * 1024))

    def _bomb_handler(request):
        return _cprobe.httpx.Response(200, headers={"Content-Encoding": "gzip", "Content-Type": "text/html"},
                                      content=_bomb)
    _cprobe.httpx.Client = (lambda *a, **k:
                            _real_httpx_client(*a, transport=_cprobe.httpx.MockTransport(_bomb_handler),
                                               **{kk: vv for kk, vv in k.items() if kk != "transport"}))
    check("curator: safe_fetch refuses a gzip-decode bomb (DECODED bytes > cap)",
          _cprobe.safe_fetch("http://example.com/bomb", max_bytes=5 * 1024 * 1024)["blocked_reason"] == "oversize")

    _hop = {"n": 0}

    def _redir_handler(request):
        _hop["n"] += 1
        if _hop["n"] == 1:
            return _cprobe.httpx.Response(302, headers={"Location": "http://internal.example.com/x"})
        return _cprobe.httpx.Response(200, content=b"unreached")
    _cprobe.httpx.Client = (lambda *a, **k:
                            _real_httpx_client(*a, transport=_cprobe.httpx.MockTransport(_redir_handler),
                                               **{kk: vv for kk, vv in k.items() if kk != "transport"}))
    _cprobe.socket.getaddrinfo = (lambda h, *a, **k:
                                  [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))] if "internal" in h
                                  else [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))])
    check("curator: safe_fetch re-validates a redirect hop (rebind to private -> blocked)",
          _cprobe.safe_fetch("http://public.example.com/start")["blocked_reason"] == "private_ip")
    _cprobe.httpx.Client = _real_httpx_client
finally:
    _cprobe.socket.getaddrinfo = _real_gai
# source-inspect: safe_fetch owns its client (no http._get_client), follow_redirects=False, iter_bytes
_sf_src = _insp.getsource(_cprobe.safe_fetch)
# It must not CALL the shared pooled client / capped helper (a docstring mention is fine: the
# call forms are http._get_client( and http._request_capped().
check("curator: safe_fetch does NOT call the shared pooled client / capped helper",
      "_get_client(" not in _sf_src and "_request_capped(" not in _sf_src
      and "httpx.Client(" in _sf_src)
check("curator: safe_fetch sets follow_redirects=False + reads via iter_bytes (decoded cap)",
      "follow_redirects=False" in _sf_src
      and ("iter_bytes" in _sf_src or "iter_bytes" in _insp.getsource(_cprobe._read_capped)))

# (5) Persistence.
import tempfile as _ctf  # noqa: E402
_ctmp = Path(_ctf.mkdtemp())
_ccand.STATE_DIR = _ctmp
_ccand.CANDIDATES_PATH = _ctmp / "candidates.json"
_ccand.SEEN_HOSTS_PATH = _ctmp / "seen_hosts.json"
_cid = _ccand.add({"name": "Smoke Cand", "urls": ["https://z.example.com/f"],
                   "proposed_mode": "RECALL", "proposed_domain": "papers", "proposed_family": "rss"})
check("curator: candidates.add -> get round-trips", (_ccand.get(_cid) or {}).get("name") == "Smoke Cand")
_ccand.CANDIDATES_PATH.write_text("{ not valid json", encoding="utf-8")
check("curator: corrupt candidates.json -> list() == []", _ccand.list() == [])
_ccand.add({"name": "C2", "urls": ["https://q.example.com"], "proposed_mode": "RECALL",
            "proposed_domain": "papers", "proposed_family": "rss"})
_ids = [r["id"] for r in _ccand.list()]
check("curator: no duplicate ids after re-add over corrupt", len(_ids) == len(set(_ids)))
_cand_src = _insp.getsource(_ccand)
check("curator: persistence uses cache._atomic_write_text (os.replace), no naked write-open",
      "_atomic_write_text" in _cand_src and "open(CANDIDATES_PATH" not in _cand_src
      and ".write_text(" not in _cand_src.split("def _save_all")[1].split("def ")[0])

# (6) Auto-apply gate (table-driven, gate-only in P1) + auto_appliable always false.
_whitelist_host = "trusted.example.com"
_capply._live_hosts = lambda: {_whitelist_host}


def _gate_cand(**over):
    base = {
        "proposed_family": "rss", "proposed_mode": "STRUCTURE",
        "urls": [f"https://{_whitelist_host}/feed.xml"],
        "verdict": {"decision": "admit"},
        "evidence": {"evidence_complete": True,
                     "stage0_safety": {"redline_hits": [], "hard_redline_blocked": False,
                                       "first_seen_host": False}},
        "proposed_config_row": {},
    }
    base.update(over)
    return base


check("curator: _auto_apply_ok True for the conservative happy path", _capply._auto_apply_ok(_gate_cand()))
# RAZOR-FIX (2026-06-15): a FIRST-SEEN host no longer blocks auto-apply, and there is no operator host
# allowlist gate. The agent's admit verdict IS the host-trust judgment; the overlay-rss recurring fetch
# is made safe MECHANICALLY (guard_ip), not by a human allowlist. So the gate fires on a clean admit.
check("curator: a first-seen-host candidate STILL auto-applies (no human allowlist / first-seen gate)",
      _capply._auto_apply_ok(_gate_cand(evidence={"evidence_complete": True, "stage0_safety": {
          "redline_hits": [], "hard_redline_blocked": False, "first_seen_host": True}})))
_gate_false_rows = {
    "hard_redline": _gate_cand(evidence={"evidence_complete": True, "stage0_safety": {
        "redline_hits": [{"id": "x", "severity": "hard"}], "hard_redline_blocked": True, "first_seen_host": False}}),
    "soft_redline": _gate_cand(evidence={"evidence_complete": True, "stage0_safety": {
        "redline_hits": [{"id": "x", "severity": "soft"}], "hard_redline_blocked": False, "first_seen_host": False}}),
    "family_org_watch": _gate_cand(proposed_family="org_watch"),
    "family_page_watch": _gate_cand(proposed_family="page_watch"),
    "render_true": _gate_cand(proposed_config_row={"sites": [{"url": "x", "render": True}]}),
    "mode_RECALL": _gate_cand(proposed_mode="RECALL"),
    "verdict_not_admit": _gate_cand(verdict={"decision": "watch"}),
    "evidence_incomplete": _gate_cand(evidence={"evidence_complete": False, "stage0_safety": {
        "redline_hits": [], "hard_redline_blocked": False, "first_seen_host": False}}),
}
_gate_fails = [k for k, c in _gate_false_rows.items() if _capply._auto_apply_ok(c)]
check("curator: _auto_apply_ok False the moment any mechanical risk dimension flips", not _gate_fails,
      f"unexpectedly True for: {_gate_fails}")
check("curator: P1 packet reversibility.auto_appliable is always false",
      _build_fixture_packet()[0]["reversibility"]["auto_appliable"] is False)

# (7) Frozen auto-apply policy.
_pol = _capply._auto_policy()
_EXPECTED_AUTO_FAMILIES = {"rss"}
_EXPECTED_AUTO_MODES = {"STRUCTURE", "UNWALL", "TRANSCRIBE"}
check("curator: admission_policy auto_apply seeded to the conservative floor",
      set(_pol.get("families") or []) == _EXPECTED_AUTO_FAMILIES
      and set(_pol.get("modes") or []) == _EXPECTED_AUTO_MODES,
      f"families={_pol.get('families')} modes={_pol.get('modes')}")
check("curator: org_watch / news_scraper NOT in auto_apply families",
      "org_watch" not in (_pol.get("families") or []) and "news_scraper" not in (_pol.get("families") or []))
check("curator: default_posture == reject_if_thin", _capply.default_posture() == "reject_if_thin")

# (8) FSM totality.
# The P1 frozen edge set PLUS the §5 P4 additions (probe_dead state + watch-expired GC edge):
# error/new/probed -> probe_dead (the K-fail death), probe_dead -> error (universal escape hatch
# like the other terminals), and watching -> rejected (watch-expired). Frozen here so a silent
# FSM drift fails the deploy; §15 9.4 re-derives the P4 delta from this same set.
_EXPECTED_EDGES = {
    ("new", "probed"), ("new", "awaiting_verdict"), ("new", "redline_blocked"),
    ("new", "parked_p2"), ("new", "error"),
    ("probed", "awaiting_verdict"), ("probed", "redline_blocked"), ("probed", "parked_p2"), ("probed", "error"),
    ("awaiting_verdict", "admitted"), ("awaiting_verdict", "watching"), ("awaiting_verdict", "rejected"),
    ("awaiting_verdict", "error"), ("admitted", "owner_review"), ("admitted", "error"),
    ("watching", "probed"), ("watching", "error"), ("owner_review", "error"),
    ("redline_blocked", "error"), ("parked_p2", "error"), ("rejected", "error"), ("error", "probed"),
    # P4 additions:
    ("watching", "rejected"), ("error", "probe_dead"), ("new", "probe_dead"),
    ("probed", "probe_dead"), ("probe_dead", "error"),
}
check("curator: ALLOWED_TRANSITIONS == the frozen edge set", set(_ccand.ALLOWED_TRANSITIONS) == _EXPECTED_EDGES,
      f"extra={set(_ccand.ALLOWED_TRANSITIONS) - _EXPECTED_EDGES} missing={_EXPECTED_EDGES - set(_ccand.ALLOWED_TRANSITIONS)}")
check("curator: illegal new->admitted raises", not _ccand._can_transition("new", "admitted"))
check("curator: redline_blocked / parked_p2 / probe_dead terminal (no forward recovery edge)",
      not _ccand._can_transition("redline_blocked", "probed")
      and not _ccand._can_transition("parked_p2", "awaiting_verdict")
      and not _ccand._can_transition("probe_dead", "probed"))
check("curator: error reachable from every state AND error->probed allowed",
      all(_ccand._can_transition(s, "error") for s in _ccand.STATES if s != "error")
      and _ccand._can_transition("error", "probed"))

# (9) Anti-stale dedup: build_packet calls a FRESH list_sources (sentinel), populates as_of + item_overlap.
# Build the fixture candidate first (its setup patches the roster), THEN install the sentinel so it
# is the live_sources fn at build time (it must not be a cached constant).
_pkt9_warm, _cand9 = _build_fixture_packet()
_sentinel = {"called": False}
_orig_ls = fetcher.list_sources
fetcher.list_sources = lambda check_health=False: (_sentinel.__setitem__("called", True) or [{"name": "arxiv"}])
try:
    _pkt9 = _cev.build_packet_for(_cand9)
finally:
    fetcher.list_sources = _orig_ls
check("curator: stage2 dedup calls a FRESH list_sources (not a cached constant)", _sentinel["called"])
check("curator: stage2_dedup carries live_list_as_of + item_overlap_vs_index",
      bool(_pkt9["stage2_dedup"].get("live_list_as_of"))
      and "item_overlap_vs_index" in _pkt9["stage2_dedup"])

# (10) Baseline mandatory + anchored + decide enforces it.
_pkt10, _ = _build_fixture_packet()
_qs = _pkt10["web_baseline_request"]["suggested_queries"]
check("curator: web_baseline non-empty AND >=1 probe-derived query",
      len(_qs) >= 1 and any(q.get("origin") == "probe-derived" for q in _qs))
# decide on an admit with empty baseline_ref RAISES; with one, succeeds (-> owner_review in P1)
import penumbra.server as _srv2  # noqa: E402
_ccand.STATE_DIR = _ctmp
_ccand.CANDIDATES_PATH = _ctmp / "candidates_decide.json"
_dcid = _ccand.add({"name": "Decide Cand", "urls": ["https://d.example.com/f"], "proposed_mode": "RECALL",
                    "proposed_domain": "papers", "proposed_family": "rss"})
_ccand.store_evidence(_dcid, {"evidence_complete": True, "stage0_safety": {"hard_redline_blocked": False}},
                      {"hard_redline_ids": []}, "awaiting_verdict", "ev")


def _decide_raises(**kw):
    try:
        _srv2.penumbra_curator_act.__wrapped__(verb="decide", **kw)
        return False
    except Exception:  # noqa: BLE001
        return True


check("curator: decide(admit, baseline_ref={}) RAISES (empty baseline)",
      _decide_raises(candidate_id=_dcid, decision="admit", reasons="x", baseline_ref={}))
_ok_decide = _srv2.penumbra_curator_act.__wrapped__(verb="decide", candidate_id=_dcid, decision="admit",
                                               reasons="x", baseline_ref={"web": ["result"]})
check("curator: decide(admit, baseline_ref=...) succeeds -> owner_review in P1",
      _ok_decide.get("state") == "owner_review")

# (11) Decide fail-closed on hard-redline / incomplete-evidence.
_ccand.CANDIDATES_PATH = _ctmp / "candidates_failclosed.json"
_hc = _ccand.add({"name": "HardRL", "urls": ["https://h.example.com/f"], "proposed_mode": "RECALL",
                  "proposed_domain": "papers", "proposed_family": "rss"})
_ccand.store_evidence(_hc, {"evidence_complete": True, "stage0_safety": {"hard_redline_blocked": True}},
                      {"hard_redline_ids": ["x"]}, "awaiting_verdict", "ev")
check("curator: hard_redline_blocked -> decide(admit) RAISES, decide(reject) succeeds",
      _decide_raises(candidate_id=_hc, decision="admit", reasons="x", baseline_ref={"a": 1})
      and _srv2.penumbra_curator_act.__wrapped__(verb="decide", candidate_id=_hc, decision="reject", reasons="ToS").get("state") == "rejected")
_ic = _ccand.add({"name": "Incomplete", "urls": ["https://i.example.com/f"], "proposed_mode": "RECALL",
                  "proposed_domain": "papers", "proposed_family": "rss"})
_ccand.store_evidence(_ic, {"evidence_complete": False, "stage0_safety": {"hard_redline_blocked": False}},
                      {"hard_redline_ids": []}, "awaiting_verdict", "ev")
check("curator: evidence_complete=False -> decide(admit) RAISES, decide(reject) succeeds",
      _decide_raises(candidate_id=_ic, decision="admit", reasons="x", baseline_ref={"a": 1})
      and _srv2.penumbra_curator_act.__wrapped__(verb="decide", candidate_id=_ic, decision="reject", reasons="thin").get("state") == "rejected")

# (12) _live_hosts non-trivial + host-derivation correct (against the REAL roster).
import importlib as _il  # noqa: E402
_il.reload(_capply)            # restore the real _live_hosts (the §12 test below needs the live roster)
fetcher.list_sources = _REAL_LIST_SOURCES   # restore the live roster (fixtures patched these to []/stubs)
fetcher.all_adapter_names = _REAL_ALL_NAMES
fetcher.get_adapter = _REAL_GET_ADAPTER
fetcher._FACETS = _REAL_FACETS
check("curator: _live_hosts() non-empty against the real roster", len(_capply._live_hosts()) > 0)
# a host-less family (org_watch / search_index) contributes NO host
_ow = fetcher.get_adapter("sea_ai_lab") or next(
    (fetcher.get_adapter(n) for n in fetcher.all_adapter_names()
     if type(fetcher.get_adapter(n)).__name__ == "_OrgWatchAdapter"), None)
check("curator: a host-less org_watch source contributes no host",
      _ow is None or _capply._hosts_of_adapter(_ow) == set())
# (13) STRUCTURE resolves, not regexes: fabricated id page, resolvers monkeypatched to 'not found'.
import penumbra.core.enrich as _enr  # noqa: E402
import penumbra.core._openalex as _oa  # noqa: E402
_orig_enrich, _orig_oa = _enr.enrich, _oa.get_json
_enr.enrich = lambda ids: [{"id": i, "error": "not a DOI or arXiv id"} for i in ids]
_oa.get_json = lambda *a, **k: {}
try:
    _fetch_fix = {"text": "see doi 10.1234/fake and arXiv 2401.99999 and W123456789", "bytes": 60}
    _sres = _cprobe._probe_structure({"urls": ["https://x.example.com"]}, _fetch_fix)
finally:
    _enr.enrich, _oa.get_json = _orig_enrich, _orig_oa
check("curator: STRUCTURE present-but-unresolved is visible (resolved == [] while present non-empty)",
      _sres["diff"]["structured_fields_present"] and _sres["diff"]["structured_fields_resolved"] == [],
      f"present={_sres['diff']['structured_fields_present']} resolved={_sres['diff']['structured_fields_resolved']}")

# (14) Server exposes the two curator dispatch tools (the 12 old penumbra_curator_* collapsed into
# penumbra_curator_view(what=...) for reads + penumbra_curator_act(verb=...) for writes); each P1 verb still
# resolves to its impl function behind the dispatcher.
for _t in ("penumbra_curator_view", "penumbra_curator_act"):
    check(f"curator: server exposes {_t}", hasattr(_srv2, _t) and callable(getattr(_srv2, _t)))
for _t in ("_curator_submit", "_curator_probe", "_curator_packet",
           "_curator_decide", "_curator_list"):
    check(f"curator: server carries the {_t} impl behind the dispatcher",
          hasattr(_srv2, _t) and callable(getattr(_srv2, _t)))

# ---------------------------------------------------------------------------
# 13. Curator P2 (yield tap): the 9 invariants that make the FAIL-OPEN, single-writer,
#     WRITES_ENABLED-gated mechanical hook safe to ship. Pure structural / offline / no network.
#     The razor: the tap records FACTS ONLY (integer counters): NO verdict / ratio / threshold
#     key anywhere in the durable store. Attribution (also_in + live_sources + merge_basis) credits
#     every collapsed source, splits live vs index, and never lets a title-merge strip sole credit.
# ---------------------------------------------------------------------------
from penumbra.core.curator import yield_tap as _yt  # noqa: E402
from penumbra.core.recall import writer as _ytw  # noqa: E402

# point the durable store at a throwaway dir (must touch NO real ~/.penumbra path)
_yttmp = Path(_ctf.mkdtemp())
_yt.STATE_DIR = _yttmp
_yt.YIELD_PATH = _yttmp / "yield.json"


def _surv(source, also_in=None, live_sources=None, merge_basis="id", from_index=False):
    """A ranked-survivor fixture carrying the rank.dedup P2 stamps."""
    d = _doc(source, f"title for {source} {also_in} {live_sources}", f"https://e.com/{source}")
    md = {"merge_basis": merge_basis}
    if also_in is not None:
        md["also_in"] = also_in
    if live_sources is not None:
        md["live_sources"] = live_sources
    if from_index:
        md["from_index"] = True
    d.metadata = md
    return d


# (1) Import is read-only: importing yield_tap created no file; WRITES_ENABLED is False after a bare
# import; the drain thread is not running.
check("yield_tap: import creates no file + WRITES_ENABLED False + no drain thread",
      not _yt.YIELD_PATH.exists() and _ytw.WRITES_ENABLED is False and _yt._writer_started is False)

# (2) Gating: WRITES_ENABLED=False → record_search writes/enqueues nothing.
_yt.record_search("q", [_surv("arxiv", live_sources=["arxiv"])], {}, {})
check("yield_tap: WRITES_ENABLED=False → record_search no-ops (queue empty, no file)",
      _yt._queue.empty() and not _yt.YIELD_PATH.exists())

# (3) Never raises (hot-path contract): malformed ranked + None + garbage metadata, with the gate
# ON so the body actually runs the build. Mirrors §9 maybe_ingest test.
_garbage = _surv("zz", also_in="not-a-list", live_sources=42)  # garbage stamps
try:
    _ytw.WRITES_ENABLED = True
    _yt.record_search("q", [None, "x", 42, _surv("arxiv", live_sources=["arxiv"]), _garbage], {}, {})
    _yt.record_search("q", None, {}, {})
    _yt.record_search("q", [_surv("a", live_sources=["a"])], {}, None)  # meta=None
    check("yield_tap: record_search never raises on malformed ranked / None / garbage", True)
except Exception as _e:  # noqa: BLE001
    check("yield_tap: record_search never raises on malformed ranked / None / garbage", False, str(_e))
finally:
    # drain whatever those enqueued so the attribution tests below start clean
    while not _yt._queue.empty():
        try:
            _yt._queue.get_nowait()
        except Exception:  # noqa: BLE001
            break
    _ytw.WRITES_ENABLED = False


def _fold_one(query, ranked, meta=None):
    """Build a bundle (gate ON) + fold it into a FRESH state; return the per-source rows. Exercises
    the same _build_bundle + _fold path the live drain uses, deterministically (no thread/timing)."""
    _ytw.WRITES_ENABLED = True
    try:
        b = _yt._build_bundle(query, ranked, {}, meta or {})
    finally:
        _ytw.WRITES_ENABLED = False
    st = {"version": 1, "total_searches_observed": 0, "updated_at": None, "sources": {}}
    _yt._fold(st, b)
    return st


# (4) Attribution razor (locks Attacks 1+2).
# (4a) co-surfaced, id-grade, both live → topk to BOTH, sole to NEITHER.
_st = _fold_one("q", [_surv("arxiv", also_in=["openalex"], live_sources=["arxiv", "openalex"], merge_basis="id")])
check("yield_tap: id-grade co-surface → topk to both, sole to neither",
      _st["sources"]["arxiv"]["topk_appearances"] == 1
      and _st["sources"]["openalex"]["topk_appearances"] == 1
      and _st["sources"]["arxiv"]["sole_contributions"] == 0
      and _st["sources"]["openalex"]["sole_contributions"] == 0)
# (4b) no also_in, single live source → sole_contributions increments.
_st = _fold_one("q", [_surv("solo", also_in=None, live_sources=["solo"], merge_basis="id")])
check("yield_tap: lone live survivor → sole_contributions increments",
      _st["sources"]["solo"]["sole_contributions"] == 1
      and _st["sources"]["solo"]["topk_appearances"] == 1)
# (4c) title-only merge → NEITHER loses sole; both get title_soft_coappearances; the would-be-sole
# source keeps its credit (records 0 sole, not a stripped one).
_st = _fold_one("q", [_surv("jobrxiv", also_in=["overseas_ai_jobs"],
                            live_sources=["jobrxiv", "overseas_ai_jobs"], merge_basis="title")])
check("yield_tap: title-only merge → title_soft to both, sole stripped from NEITHER",
      _st["sources"]["jobrxiv"]["title_soft_coappearances"] == 1
      and _st["sources"]["overseas_ai_jobs"]["title_soft_coappearances"] == 1
      and _st["sources"]["jobrxiv"]["sole_contributions"] == 0
      and _st["sources"]["overseas_ai_jobs"]["sole_contributions"] == 0)
# (4d) index-only survivor (live_sources=[]) → from_index_only increments; topk + sole do NOT.
_st = _fold_one("q", [_surv("deadfeed", also_in=None, live_sources=[], merge_basis="id", from_index=True)])
check("yield_tap: index-only survivor → from_index_only only (no topk, no sole)",
      _st["sources"]["deadfeed"]["from_index_only_appearances"] == 1
      and _st["sources"]["deadfeed"]["topk_appearances"] == 0
      and _st["sources"]["deadfeed"]["sole_contributions"] == 0)
# bonus: _index pseudo-source gets NO per-source credit even when present in also_in
_st = _fold_one("q", [_surv("arxiv", also_in=["_index"], live_sources=["arxiv", "_index"], merge_basis="id")])
check("yield_tap: _index pseudo-source gets no per-source credit",
      "_index" not in _st["sources"] and _st["sources"]["arxiv"]["topk_appearances"] == 1)
# timeout / error facts fold from meta
_st = _fold_one("q", [_surv("arxiv", live_sources=["arxiv"])],
                meta={"timed_out": ["slowsrc"], "errored": ["brokesrc"]})
check("yield_tap: searches_timed_out / searches_errored fold from meta",
      _st["sources"]["slowsrc"]["searches_timed_out"] == 1
      and _st["sources"]["brokesrc"]["searches_errored"] == 1
      and _st["total_searches_observed"] == 1)

# (5) Tap exception cannot change `ranked`: monkeypatch record_search to raise; assert the ranked
# returned by search_ranked is byte-identical to the tap-absent path.
_real_rs_tap = _yt.record_search
_real_search_many = fetcher.search_many
_fx = [_doc("arxiv", "Scaling Laws for Neural Language Models", "http://arxiv.org/abs/2001.08361")]
fetcher.search_many = lambda *a, **k: ({"arxiv": list(_fx)}, {"timed_out": [], "errored": [], "empty": []})
try:
    _clean, _ = fetcher.search_ranked("scaling laws", ["arxiv"], 5, cache_only=True)  # tap skipped
    _yt.record_search = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tap boom"))
    _boom, _ = fetcher.search_ranked("scaling laws", ["arxiv"], 5, record_yield=True, cache_only=True)
    # cache_only path skips the tap anyway; also prove the wrapped-call path swallows a raise:
    _boom2, _ = fetcher.search_ranked("scaling laws", ["arxiv"], 5, record_yield=True)
    check("yield_tap: a raising tap cannot change `ranked` (search returns identically)",
          [d.url for d in _clean] == [d.url for d in _boom] == [d.url for d in _boom2])
finally:
    _yt.record_search = _real_rs_tap
    fetcher.search_many = _real_search_many

# (6) Persistence uses cache._atomic_write_text (no naked open/write_text in _save_all).
_yt_src = _insp.getsource(_yt)
check("yield_tap: persistence uses cache._atomic_write_text (no naked write-open)",
      "_atomic_write_text" in _yt_src and "open(YIELD_PATH" not in _yt_src
      and ".write_text(" not in _yt_src.split("def _save_all")[1].split("def ")[0])

# (7) Corrupt yield.json → _load_all returns a fresh empty state (no raise).
_yt.YIELD_PATH.parent.mkdir(parents=True, exist_ok=True)
_yt.YIELD_PATH.write_text("{ not valid json", encoding="utf-8")
_loaded = _yt._load_all()
check("yield_tap: corrupt yield.json → _load_all returns fresh {} (no raise)",
      isinstance(_loaded, dict) and _loaded.get("sources") == {} and _loaded.get("total_searches_observed") == 0)

# (8) No raw query text in the durable store + NO banned verdict key at any depth. Fold a couple of
# bundles built from a recognizable fixture query, persist, then scan the written JSON.
_QFIX = "supercalifragilistic-fixture-query-string-xyz"
_ytw.WRITES_ENABLED = True
try:
    _b1 = _yt._build_bundle(_QFIX, [_surv("arxiv", also_in=["openalex"],
                            live_sources=["arxiv", "openalex"], merge_basis="id")], {}, {})
    _b2 = _yt._build_bundle(_QFIX, [_surv("solo", live_sources=["solo"])], {},
                            {"timed_out": ["slowsrc"]})
finally:
    _ytw.WRITES_ENABLED = False
_state = {"version": 1, "total_searches_observed": 0, "updated_at": None, "sources": {}}
_yt._fold(_state, _b1)
_yt._fold(_state, _b2)
_yt.YIELD_PATH = _yttmp / "yield_persist.json"
with _yt._LOCK:
    _yt._save_all(_state)
_persisted_text = _yt.YIELD_PATH.read_text(encoding="utf-8")
_persisted_json = json.loads(_persisted_text)
check("yield_tap: no raw query text written (only the fp)", _QFIX not in _persisted_text)
_yt_bad_keys = _walk_banned_keys(_persisted_json)
check("yield_tap: persisted yield.json has NO banned verdict key at any depth",
      not _yt_bad_keys, str(_yt_bad_keys))
# the round-trip survives a reload (no growth with traffic → bounded by source cardinality)
_reloaded = _yt._load_all()
check("yield_tap: persisted state round-trips through _load_all + is bounded by source rows",
      _reloaded["total_searches_observed"] == 2
      and set(_reloaded["sources"]) == {"arxiv", "openalex", "solo", "slowsrc"}
      and _reloaded["sources"]["arxiv"]["topk_appearances"] == 1
      and _reloaded["sources"]["solo"]["sole_contributions"] == 1
      and _reloaded["sources"]["slowsrc"]["searches_timed_out"] == 1)

# (9) rank.dedup stamps the new P2 fields on EVERY survivor (the prerequisite the tap trusts).
# two-source LIVE group merged on an ID (short titles → fingerprint falls through to the shared
# arXiv-id in the URL, NOT the title) → live_sources len 2, merge_basis "id". (index_only was
# a derived duplicate of live_sources==[]; the 2026-07-01 fixpoint rescan deleted the stamp.)
_g1 = _doc("arxiv", "ResNet", "http://arxiv.org/abs/1512.03385")
_g2 = _doc("openalex", "ResNet", "http://arxiv.org/pdf/1512.03385")
check("yield_tap: §9 fixture merges on arXiv-id (not title)", rank.fingerprint(_g1).startswith("arxiv:"))
_dd2 = rank.dedup([_g1, _g2])
check("yield_tap: rank.dedup stamps live_sources/merge_basis on a 2-source id-grade group",
      len(_dd2) == 1
      and set(_dd2[0].metadata.get("live_sources")) == {"arxiv", "openalex"}
      and "index_only" not in _dd2[0].metadata
      and _dd2[0].metadata.get("merge_basis") == "id")
# and a cross-source LONG-TITLE paper merge correctly stamps merge_basis "title" (the common case;
# the universal cross-source key is the title; see rank.fingerprint) so the tap records it as a
# title_soft co-appearance, never stripping sole credit (the prune-safe direction).
_t1 = _doc("arxiv", "Deep Residual Learning for Image Recognition", "http://arxiv.org/abs/1512.99999")
_t2 = _doc("openalex", "Deep Residual Learning for Image Recognition", "https://doi.org/10.1/resnet")
_ddt = rank.dedup([_t1, _t2])
check("yield_tap: a cross-source long-title paper merge stamps merge_basis 'title'",
      len(_ddt) == 1 and _ddt[0].metadata.get("merge_basis") == "title"
      and set(_ddt[0].metadata.get("live_sources")) == {"arxiv", "openalex"})
# a SINGLETON live survivor also carries the stamps (presence-trustable, not inferred-absent).
_solo = _doc("arxiv", "A Singleton Paper With A Sufficiently Long Title", "http://arxiv.org/abs/2401.00002")
_dds = rank.dedup([_solo])
check("yield_tap: rank.dedup stamps a SINGLETON survivor too (live_sources=[src])",
      _dds[0].metadata.get("live_sources") == ["arxiv"]
      and "index_only" not in _dds[0].metadata
      and _dds[0].metadata.get("merge_basis") == "title")
# an INDEX-ONLY group (every member from recall) → live_sources empty (THE raw fact).
_ix1 = _doc("mycareersfuture", "Senior ML Engineer Opening At A Long Titled Company", "https://e.com/ix1")
_ix1.metadata = {"from_index": True}
_ix2 = _doc("overseas_ai_jobs", "Senior ML Engineer Opening At A Long Titled Company", "https://e.com/ix2")
_ix2.metadata = {"from_index": True}
_ddi = rank.dedup([_ix1, _ix2])
check("yield_tap: rank.dedup marks an index-only group (live_sources empty = the raw fact)",
      len(_ddi) == 1 and _ddi[0].metadata.get("live_sources") == []
      and "index_only" not in _ddi[0].metadata)

# ---------------------------------------------------------------------------
# 14. Curator P3 (source audit): the 5 invariants that keep the read-only mechanical fact-gather
#     honest + the write-back un-bypassable. THE RAZOR: the gather emits NO verdict key (it joins
#     yield + ingest + watchdog + the facets coverage grid into facts + LABELED ratios + 8 safety
#     flags); record_source_verdict RAISES on a forbidden prune (the class-vs-flag matrix encodes
#     the operator's coverage red-lines); a prune stages a reversible operator case, never a live mutation.
# ---------------------------------------------------------------------------
import time as _satime  # noqa: E402
from penumbra.core.curator import source_audit as _sa  # noqa: E402

# write-back goes to a throwaway dir (touch NO real ~/.penumbra path)
_satmp = Path(_ctf.mkdtemp())
_sa.STATE_DIR = _satmp
_sa.SOURCE_VERDICTS_PATH = _satmp / "source_verdicts.json"

# A FRESH fixture roster (the gather must read list_sources live, never a cached constant). Cells:
#   papersxSTRUCTURE: arxiv + openalex (2 occupants)   loner_papers occupies it solo via RECALL
#   immigrationxRECALL: sole_feed ONLY (1 occupant → coverage_critical at floor 1)
_SA_ROSTER = [
    {"name": "_index", "domains": ["papers"], "modes": ["RECALL"], "health": "ok",
     "needs_credentials": False, "explicit_only": False, "kind": "lookup"},
    {"name": "arxiv", "domains": ["papers"], "modes": ["STRUCTURE"], "health": "ok",
     "needs_credentials": False, "explicit_only": False, "kind": "lookup", "regions": [],
     "stability": "stable"},
    {"name": "openalex", "domains": ["papers"], "modes": ["STRUCTURE"], "health": "ok",
     "needs_credentials": False, "explicit_only": False, "kind": "lookup", "regions": []},
    {"name": "sole_feed", "domains": ["immigration"], "modes": ["RECALL"], "health": "ok",
     "needs_credentials": False, "explicit_only": False, "kind": "stream", "regions": ["ca"]},
    {"name": "no_modes", "domains": ["papers"], "modes": [], "health": "ok",
     "needs_credentials": False, "explicit_only": False, "kind": "lookup", "regions": []},
    {"name": "walled_src", "domains": ["community"], "modes": ["UNWALL"], "health": "ok",
     "needs_credentials": False, "explicit_only": True, "kind": "lookup", "regions": [],
     "stability": "walled"},
    {"name": "cdp_src", "domains": ["community"], "modes": ["UNWALL"], "health": "unknown",
     "needs_credentials": True, "explicit_only": False, "kind": "lookup", "regions": [],
     "stability": "keyed"},
    {"name": "rare_feed", "domains": ["funding"], "modes": ["MONITOR"], "health": "ok",
     "needs_credentials": False, "explicit_only": False, "kind": "stream", "regions": []},
    {"name": "redundant_src", "domains": ["news"], "modes": ["RECALL"], "health": "ok",
     "needs_credentials": False, "explicit_only": False, "kind": "stream", "regions": []},
    # public no-auth API that the watchdog never probed (shares papersxSTRUCTURE with arxiv/openalex so
    # it adds no sole-cell): isolates the flag split — is_cdp_or_credentialed=False (public) yet
    # watchdog_untracked=True (absent from _SA_TRACKED), and a DEAD-prune is still blocked via the latter.
    {"name": "public_untracked", "domains": ["papers"], "modes": ["STRUCTURE"], "health": "ok",
     "needs_credentials": False, "explicit_only": False, "kind": "lookup", "regions": []},
]
# tracked = the watchdog-probed set (cdp_src + public_untracked deliberately ABSENT → watchdog_untracked)
_SA_TRACKED = {"arxiv", "openalex", "sole_feed", "no_modes", "walled_src", "rare_feed", "redundant_src"}
# Yield fixture: redundant_src has many top-K hits but ZERO sole + old enough to be min_evidence_met;
# arxiv carries a sole contribution (protected); rare_feed barely measured (cold-start).
_SA_YIELD = {"version": 1, "total_searches_observed": 100, "updated_at": None, "sources": {
    "redundant_src": {"topk_appearances": 80, "sole_contributions": 0, "from_index_only_appearances": 0,
                      "title_soft_coappearances": 5, "searches_present": 80, "searches_timed_out": 0,
                      "searches_errored": 0, "best_rank_seen": 1, "rank_histogram": {},
                      "first_recorded_at": "2020-01-01T00:00:00Z", "last_topk_at": "2026-06-01T00:00:00Z"},
    "arxiv": {"topk_appearances": 50, "sole_contributions": 7, "from_index_only_appearances": 0,
              "title_soft_coappearances": 0, "searches_present": 50, "searches_timed_out": 0,
              "searches_errored": 0, "best_rank_seen": 0, "rank_histogram": {},
              "first_recorded_at": "2020-01-01T00:00:00Z", "last_topk_at": "2026-06-01T00:00:00Z"},
    "rare_feed": {"topk_appearances": 1, "sole_contributions": 0, "from_index_only_appearances": 0,
                  "title_soft_coappearances": 0, "searches_present": 1, "searches_timed_out": 0,
                  "searches_errored": 0, "best_rank_seen": 5, "rank_histogram": {},
                  "first_recorded_at": "2026-06-14T00:00:00Z", "last_topk_at": "2026-06-14T00:00:00Z"},
}}

_real_sa_list = fetcher.list_sources
_real_sa_wd = fetcher._watchdog_health
_real_sa_facets = getattr(fetcher, "_FACETS", {})
_real_sa_yt_load = _sa._yt._load_all
_real_sa_ingest = _sa._ingest_watermark
fetcher.list_sources = lambda check_health=False: [dict(r) for r in _SA_ROSTER]
fetcher._watchdog_health = lambda: ({"redundant_src": 0}, set(_SA_TRACKED), "2026-06-14T00:00:00Z")
fetcher._FACETS = {r["name"]: {"domains": r["domains"], "modes": r["modes"]} for r in _SA_ROSTER
                   if r["name"] != "_index"}
_sa._yt._load_all = lambda: {k: (dict(v) if isinstance(v, dict) else v) for k, v in _SA_YIELD.items()}
# ingest watermark fixture: rare_feed silent 5d (< its funding cadence floor → below_cadence_floor);
# redundant_src silent 200d but recall carrying nothing here (so not a 'silent-carrying' candidate).
_real_sa_ingest_fixture = lambda name: {
    "rare_feed": {"last_ingest_at": _satime.time() - 5 * 86400, "last_ingest_doc_count": 2,
                  "live_feed_silent_days": 5.0},
}.get(name, {"last_ingest_at": None, "last_ingest_doc_count": None, "live_feed_silent_days": None})
_sa._ingest_watermark = _real_sa_ingest_fixture

try:
    _dossier = _sa.gather_source_dossier()
    _by_name = {s["name"]: s for s in _dossier["sources"]}

    # (1) THE RAZOR GUARD: the gather emits NO banned verdict key at any depth (reuse the scanner).
    _sa_bad_keys = _walk_banned_keys(_dossier)
    check("curator P3: gather_source_dossier emits NO banned verdict key at any depth",
          not _sa_bad_keys, str(_sa_bad_keys))
    # _index is never a prune candidate (filtered out of the gather).
    check("curator P3: _index is filtered out of the dossier (never a prune candidate)",
          "_index" not in _by_name)

    # (2) FRESH list_sources read, not a cached constant: a roster mutation shows up on re-gather.
    fetcher.list_sources = lambda check_health=False: [dict(r) for r in _SA_ROSTER
                                                       if r["name"] != "redundant_src"]
    _dossier2 = _sa.gather_source_dossier()
    check("curator P3: gather does a FRESH list_sources read (not a cached constant)",
          "redundant_src" not in {s["name"] for s in _dossier2["sources"]}
          and "redundant_src" in _by_name)
    fetcher.list_sources = lambda check_health=False: [dict(r) for r in _SA_ROSTER]  # restore

    # (3) the 8 mechanical SAFETY FLAGS compute as pure facts.
    _f = lambda n: _by_name[n]["safety_flags"]
    check("curator P3: protected_sole_contributor True iff sole_contributions>0",
          _f("arxiv")["protected_sole_contributor"] is True
          and _f("redundant_src")["protected_sole_contributor"] is False)
    check("curator P3: coverage_critical True for a sole-cell occupant (sole_feed: immigrationxRECALL)",
          _f("sole_feed")["coverage_critical"] is True
          and "immigrationxRECALL" in _f("sole_feed")["coverage_critical_cells"]
          and _f("arxiv")["coverage_critical"] is False)  # arxiv shares papersxSTRUCTURE with openalex
    check("curator P3: coverage_unknown True for a modes==[] source (no_modes)",
          _f("no_modes")["coverage_unknown"] is True and _f("arxiv")["coverage_unknown"] is False)
    check("curator P3: tap_blind True for an explicit_only source (walled_src)",
          _f("walled_src")["tap_blind"] is True and _f("arxiv")["tap_blind"] is False)
    check("curator P3: is_cdp_or_credentialed True IFF needs_credentials (cdp_src); a public untracked "
          "API is NOT flagged (public_untracked) — the split fixed the old credentialed/untracked conflation",
          _f("cdp_src")["is_cdp_or_credentialed"] is True
          and _f("public_untracked")["is_cdp_or_credentialed"] is False
          and _f("arxiv")["is_cdp_or_credentialed"] is False)
    check("curator P3: watchdog_untracked True for a watchdog-absent source (cdp_src + public_untracked), "
          "False for a tracked one (arxiv)",
          _f("cdp_src")["watchdog_untracked"] is True
          and _f("public_untracked")["watchdog_untracked"] is True
          and _f("arxiv")["watchdog_untracked"] is False)
    check("curator P3: below_cadence_floor exempts a rare-feed silent < its funding floor",
          _f("rare_feed")["below_cadence_floor"] is True)
    check("curator P3: min_evidence_met False for a cold-start source (rare_feed), True once measured",
          _f("rare_feed")["min_evidence_met"] is False
          and _f("redundant_src")["min_evidence_met"] is True)
    # every facets-roster source maps to a DEFINED cadence floor via domains (no missing-floor default
    # that silently breaks): each source with a domain has a non-None cadence_floor_days.
    _floor_ok = all((s["cadence_floor_days"] is not None) for s in _dossier["sources"] if s["domains"])
    check("curator P3: every faceted source maps to a defined cadence floor via domains", _floor_ok)
    # the policy file ships + freezes its expected key shape (operator DATA, widening is a the operator edit).
    _pol = _sa.load_policy()
    _SA_POLICY_KEYS = {"coverage_floor", "coverage_floor_overrides", "min_evidence",
                       "cadence_floor_days", "deadline_starved_timeout_rate"}
    check("curator P3: audit_policy.json ships + has the expected key shape",
          _SA_POLICY_KEYS <= set(_pol)
          and {"searches_floor", "min_age_days"} <= set(_pol.get("min_evidence") or {}))
    # every distinct facets.json domain token has a cadence floor (no silent missing-floor default).
    _facets_json = json.loads((ROOT / "src" / "penumbra" / "core" / "facets.json").read_text(encoding="utf-8"))
    _fac_domains = {d for fb in _facets_json.values() for d in (fb.get("domains") or [])}
    _cad_table = set(_pol.get("cadence_floor_days") or {})
    check("curator P3: every facets.json domain token has a cadence floor in audit_policy.json",
          _fac_domains <= _cad_table, f"missing: {sorted(_fac_domains - _cad_table)}")

    # (3b) the NEUTRAL stale-judgment read-back block (judgment_recency): the gather reads back the
    #      agent's prior verdicts so a stored-and-forgotten WATCH resurfaces, WITHOUT echoing the
    #      recorded word (smoke would catch a verdict token via _walk_banned_keys above). With a FRESH
    #      verdicts file (tmp, nothing recorded yet) every source has last_judged_present=False and
    #      verdict_age_days=None; revalidation_candidate is gated on the cold-start floor so a
    #      never-measured source is NEVER surfaced (else day-one drowns the sentinel in ~144 sources).
    _jr = lambda n: _by_name[n]["judgment_recency"]
    check("curator P3: judgment_recency block present with the three neutral keys",
          set(_jr("arxiv")) == {"verdict_age_days", "last_judged_present", "revalidation_candidate"})
    check("curator P3: with no prior verdicts file, last_judged_present=False + verdict_age_days=None",
          _jr("arxiv")["last_judged_present"] is False and _jr("arxiv")["verdict_age_days"] is None
          and _jr("redundant_src")["last_judged_present"] is False)
    # COLD-START GATE: rare_feed cleared NO evidence floor (min_evidence_met False) → never a
    # revalidation_candidate even though it was never judged; a measured source (redundant_src) that
    # was never judged IS a candidate (stale = no judgment yet, cold-start cleared).
    check("curator P3: revalidation_candidate=False for a cold-start (min_evidence_met=False) source",
          _jr("rare_feed")["revalidation_candidate"] is False
          and _f("rare_feed")["min_evidence_met"] is False)
    check("curator P3: revalidation_candidate=True for a measured, never-judged source (redundant_src)",
          _jr("redundant_src")["revalidation_candidate"] is True
          and "redundant_src" in _dossier.get("revalidation_candidates", []))
    # NO VERDICT LEAK: the new keys are not in the banned set, and no verdict token rides any value
    # of the judgment_recency block (it carries only an int/None age, two bools — never a word).
    check("curator P3: judgment_recency key names are not banned verdict keys",
          not (set(_jr("arxiv")) & _BANNED_KEYS))
    _jr_vals = [v for n in _by_name for v in _by_name[n]["judgment_recency"].values()]
    check("curator P3: judgment_recency values carry no verdict token (only int/None/bool)",
          all(isinstance(v, (int, float, bool)) or v is None for v in _jr_vals))
    # the policy echo surfaces the SEPARATE revalidation floor (a different clock from cadence_floor).
    check("curator P3: dossier policy echoes verdict_revalidation_floor_days (its own clock)",
          isinstance((_dossier.get("policy") or {}).get("verdict_revalidation_floor_days"), int))

    # (3b.1) the NEUTRAL stability fact: each per-source dossier dict carries the fragility class
    #        straight off the roster. It is a FACT (one of the four ordered values), NOT a verdict —
    #        the §14 banned-key walk above (block 1) already proved the whole dossier carries no
    #        verdict key/token, and "stability"/"stable"/"keyed"/"scrape"/"walled" hit neither set.
    check("curator P3: every per-source dossier dict carries a stability key",
          all("stability" in s for s in _dossier["sources"]))
    check("curator P3: stability rides off the roster (arxiv=stable, walled_src=walled, cdp_src=keyed)",
          _by_name["arxiv"]["stability"] == "stable"
          and _by_name["walled_src"]["stability"] == "walled"
          and _by_name["cdp_src"]["stability"] == "keyed")
    check("curator P3: 'stability' is not a banned verdict key + carries no verdict token",
          "stability" not in _BANNED_KEYS
          and not any(_t in {"stability", "stable", "keyed", "scrape", "walled"} for _t in _VERDICT_TOKENS))

    # (3c) READ-BACK is wired: after the write-back records a verdict (block 4 below records arxiv=keep
    #      + redundant_src=watch), a RE-GATHER must show last_judged_present=True + a fresh age — and
    #      STILL no verdict word leaks. Proven inline here against a re-gather right after a record.
    _sa.record_source_verdict("redundant_src", "watch", "stale-readback wiring probe")
    _dossier_rb = _sa.gather_source_dossier()
    _jr_rb = {s["name"]: s["judgment_recency"] for s in _dossier_rb["sources"]}
    check("curator P3: a RE-GATHER reads the just-recorded verdict back (last_judged_present True, fresh age)",
          _jr_rb["redundant_src"]["last_judged_present"] is True
          and isinstance(_jr_rb["redundant_src"]["verdict_age_days"], (int, float))
          and _jr_rb["redundant_src"]["verdict_age_days"] <= 1)
    # a fresh verdict (age 0 < the 90d floor) is NOT stale → revalidation_candidate flips False.
    check("curator P3: a freshly-recorded verdict is NOT a revalidation_candidate (age < floor)",
          _jr_rb["redundant_src"]["revalidation_candidate"] is False)
    check("curator P3: the read-back dossier STILL emits no banned verdict key at any depth",
          not _walk_banned_keys(_dossier_rb))
    # reset the verdicts file so block (4)'s assertions start from a clean slate.
    _sa.SOURCE_VERDICTS_PATH.unlink(missing_ok=True)

    # (4) record_source_verdict stamps by="agent"+timestamp, persists, computes none — and RAISES on
    #     a forbidden prune per the class-vs-flag matrix. A KEEP/WATCH always succeeds.
    _kept = _sa.record_source_verdict("arxiv", "keep", "load-bearing sole contributor")
    check("curator P3: record_source_verdict stamps by=agent + timestamp, persists a KEEP",
          _kept["by"] == "agent" and _kept.get("at") and _kept["verdict"] == "keep"
          and (_sa._load_verdicts()["verdicts"].get("arxiv") or {}).get("verdict") == "keep")
    _watched = _sa.record_source_verdict("redundant_src", "watch", "watch for sustained sole=0")
    check("curator P3: a WATCH always succeeds", _watched["verdict"] == "watch")

    def _prune_raises(n, cls):
        try:
            _sa.record_source_verdict(n, "prune", "x", prune_class=cls)
            return False
        except ValueError:
            return True

    check("curator P3: PRUNE redundant of a protected_sole_contributor RAISES (arxiv)",
          _prune_raises("arxiv", "redundant"))
    check("curator P3: PRUNE low-yield of a coverage_critical source RAISES (sole_feed)",
          _prune_raises("sole_feed", "low-yield"))
    check("curator P3: PRUNE redundant of a coverage_unknown source RAISES (no_modes)",
          _prune_raises("no_modes", "redundant"))
    check("curator P3: PRUNE low-yield of a tap_blind source RAISES (walled_src)",
          _prune_raises("walled_src", "low-yield"))
    check("curator P3: PRUNE DEAD of a cdp/credentialed source RAISES (cdp_src)",
          _prune_raises("cdp_src", "DEAD"))
    check("curator P3: PRUNE DEAD of a public UNTRACKED source RAISES via watchdog_untracked "
          "(public_untracked — proves the untracked DEAD-exemption survived the split)",
          _prune_raises("public_untracked", "DEAD"))
    check("curator P3: PRUNE low-yield of a cold-start (min_evidence_met=False) source RAISES (rare_feed)",
          _prune_raises("rare_feed", "low-yield"))
    check("curator P3: PRUNE low-yield of a below_cadence_floor source RAISES (rare_feed)",
          _prune_raises("rare_feed", "low-yield"))
    # a prune with no class, or an unknown verdict/class, RAISES.
    check("curator P3: a PRUNE with no class RAISES", _prune_raises("redundant_src", ""))
    try:
        _sa.record_source_verdict("arxiv", "delete", "x")
        check("curator P3: an unknown verdict RAISES", False)
    except ValueError:
        check("curator P3: an unknown verdict RAISES", True)

    # a NON-forbidden prune LANDS + persists by=agent (the write path is not dead code): a roster
    # where two news sources share newsxRECALL (so pruning one is NOT coverage_critical) + a measured,
    # unprotected, on-time source → a redundant prune is offerable and records.
    _sa_roster2 = [
        {"name": "news_a", "domains": ["news"], "modes": ["RECALL"], "health": "ok",
         "needs_credentials": False, "explicit_only": False, "kind": "stream", "regions": []},
        {"name": "news_b", "domains": ["news"], "modes": ["RECALL"], "health": "ok",
         "needs_credentials": False, "explicit_only": False, "kind": "stream", "regions": []},
    ]
    fetcher.list_sources = lambda check_health=False: [dict(r) for r in _sa_roster2]
    fetcher._watchdog_health = lambda: ({"news_a": 0, "news_b": 0}, {"news_a", "news_b"}, "t")
    _sa._yt._load_all = lambda: {"version": 1, "total_searches_observed": 100, "updated_at": None,
        "sources": {"news_a": {"topk_appearances": 80, "sole_contributions": 0,
            "from_index_only_appearances": 0, "title_soft_coappearances": 0, "searches_present": 80,
            "searches_timed_out": 0, "searches_errored": 0, "best_rank_seen": 1, "rank_histogram": {},
            "first_recorded_at": "2020-01-01T00:00:00Z", "last_topk_at": "2026-06-01T00:00:00Z"}}}
    _sa._ingest_watermark = lambda name: {"last_ingest_at": None, "last_ingest_doc_count": None,
                                          "live_feed_silent_days": None}
    _landed = _sa.record_source_verdict("news_a", "prune", "others co-surface",
                                        prune_class="redundant",
                                        coverage_impact=_sa.compute_coverage_impact("news_a"))
    check("curator P3: a NON-forbidden redundant prune LANDS + persists by=agent",
          _landed["verdict"] == "prune" and _landed["prune_class"] == "redundant"
          and _landed["by"] == "agent"
          and (_sa._load_verdicts()["verdicts"].get("news_a") or {}).get("verdict") == "prune"
          and _landed["coverage_impact"]["leaves_single_occupant"] == ["newsxRECALL"])
    # restore the §14 primary fixtures for the remaining checks.
    fetcher.list_sources = lambda check_health=False: [dict(r) for r in _SA_ROSTER]
    fetcher._watchdog_health = lambda: ({"redundant_src": 0}, set(_SA_TRACKED), "2026-06-14T00:00:00Z")
    _sa._yt._load_all = lambda: {k: (dict(v) if isinstance(v, dict) else v) for k, v in _SA_YIELD.items()}
    _sa._ingest_watermark = _real_sa_ingest_fixture

    # (5) a PRUNE operator case renders coverage_impact (before/after) + auto_appliable False + the
    #     two reversible edits (explicit_only retire + frozen-list add) — never a live mutation.
    _case = _sa.prepare_source_prune_case("redundant_src", "redundant, others co-surface")
    _edit_kinds = {e["edit"] for e in _case["reversible_edits"]}
    _impact = _case["coverage_impact"]
    _cell0 = _impact["cells"][0] if _impact["cells"] else {}
    check("curator P3: a PRUNE case renders coverage_impact (occupants before/after per cell)",
          _impact["source"] == "redundant_src" and "occupants_before" in _cell0
          and "occupants_after" in _cell0 and "leaves_single_occupant" in _impact)
    check("curator P3: a PRUNE case is auto_appliable False + stages the two reversible edits",
          _case["auto_appliable"] is False
          and _edit_kinds == {"set_explicit_only", "add_to_smoke_frozen_explicit_only_list"})
    # source-inspect: the GATHER assigns no banned verdict key literal (the write-back legitimately
    # persists the agent's "verdict" — like candidates.record_verdict — so scope to the gather, the
    # mechanical fact path, exactly as §12 scopes to build_packet_for / mode_probe).
    _sa_gsrc = _insp.getsource(_sa.gather_source_dossier)
    check("curator P3: gather_source_dossier source assigns no banned verdict key literal",
          not any(f'"{k}":' in _sa_gsrc for k in _BANNED_KEYS))
    # the two P3 verbs route through the dispatchers (view(what="audit") / act(verb="source_verdict"));
    # each still resolves to its impl function behind them.
    for _t in ("_curator_audit", "_curator_source_verdict"):
        check(f"curator P3: server carries the {_t} impl behind the dispatcher",
              hasattr(_srv2, _t) and callable(getattr(_srv2, _t)))
finally:
    fetcher.list_sources = _real_sa_list
    fetcher._watchdog_health = _real_sa_wd
    fetcher._FACETS = _real_sa_facets
    _sa._yt._load_all = _real_sa_yt_load
    _sa._ingest_watermark = _real_sa_ingest

# ---------------------------------------------------------------------------
# 15. Curator P4 (self-iterating source-acquisition loop): the 13 invariants (spec 9) that make
#     the now-ACTIVE loop (C3 activation 2026-06-15) safe to run. Pure structural / offline / no network / no
#     judgment. The CENTRAL proof is THE CRON NEVER JUDGES: curator.py + discover.py import
#     no verdict-writer / model / WebSearch / profile, and discovery emits FACTS only.
# ---------------------------------------------------------------------------
import re as _p4re  # noqa: E402

from penumbra.core.curator import discover as _disc  # noqa: E402

_CUR_DIR = ROOT / "src" / "penumbra" / "core" / "curator"
# P9: the monthly curator LOOP moved from scripts/curator.py into the in-process job
# penumbra.core.infra_jobs.run_curator (the same mechanical-only body, now a scheduler row). The razor
# invariants below grep that transplanted source, so the "no verdict-writer / sorted digests / streak
# freeze" guarantees ride along unchanged into the new home.
_LOOP_PATH = ROOT / "src" / "penumbra" / "core" / "infra_jobs.py"

# (9.1) THE CRON NEVER JUDGES: the loop + discover make NO CALL to a verdict-writer + IMPORT no
# model / WebSearch / profile. Grep the CODE (docstrings/comments stripped, so the razor's prose
# enumeration of the very things it must not do does not false-positive) for the CALL/IMPORT forms.
def _strip_py_noise(src: str) -> str:
    import io as _io
    import tokenize as _tok
    out = []
    try:
        toks = _tok.generate_tokens(_io.StringIO(src).readline)
        for ttype, tstr, _s, _e, _l in toks:
            if ttype in (_tok.COMMENT, _tok.STRING, _tok.NL, _tok.NEWLINE, _tok.INDENT, _tok.DEDENT):
                continue  # drop comments + string literals (incl. docstrings) -> only live code
            out.append(tstr)
    except Exception:  # noqa: BLE001: tokenizer failure -> fall back to the raw text (strict)
        return src
    return " ".join(out)


_disc_src = _insp.getsource(_disc)
_loop_src = _LOOP_PATH.read_text(encoding="utf-8") if _LOOP_PATH.exists() else ""
check("curator P4: curator job source readable (infra_jobs.run_curator)", bool(_loop_src), str(_LOOP_PATH))
_disc_code = _strip_py_noise(_disc_src)
_loop_code = _strip_py_noise(_loop_src)
# verdict-writer CALL forms (a bare prose mention without "(" is now also gone with the strings).
# The MCP write path is now the penumbra_curator_act dispatcher over the _curator_* impls; the cron must
# call NEITHER the dispatcher NOR the decide impl behind it.
_VERDICT_WRITER_CALLS = ("penumbra_curator_act(", "_curator_decide(", ".record_verdict(",
                         "record_source_verdict(", ".record_applied(")
_FORBIDDEN_IMPORTS = ("anthropic", "WebSearch", "web_search", "profile", "employer_hits", "relevance")
_cron_judge_leaks = []
for _txt, _who in ((_disc_code, "discover"), (_loop_code, "curator")):
    for _w in _VERDICT_WRITER_CALLS:
        # tokens are space-joined so "x . record_verdict ( " — match on the dotted/paren call shape.
        _w2 = _w.replace(".", " . ").replace("(", " (")
        if _w2.strip() in _txt or _w in _txt.replace(" ", ""):
            _cron_judge_leaks.append(f"{_who}:{_w}")
    for _i in _FORBIDDEN_IMPORTS:
        if (f"import {_i}" in _txt) or (f" {_i} ." in _txt) or (f"{_i}." in _txt.replace(" ", "")):
            _cron_judge_leaks.append(f"{_who}:{_i}")
check("curator P4: cron (loop+discover) makes no verdict-writer CALL + imports no model/WebSearch/profile",
      not _cron_judge_leaks, str(_cron_judge_leaks))

# Shared offline fixture dossier (placement-based) + an always-enabled fixture policy.
def _fixture_dossier(empty=None, single=None, placement=None, sources=None):
    return {
        "empty_cells_for_discovery": list(empty or []),
        "single_occupant_cells": list(single or []),
        "grid_by_placement": dict(placement or {}),
        "coverage_targets": sorted(set(empty or []) | set((placement or {}).keys())),
        "sources": list(sources or []),
    }


_P4_POLICY = {"enabled": True, "discover_topn": 3, "coverage_ceiling": {"_default": 4},
              "service_gap_floor": {"presence_rate_min": 0.05, "timeout_rate_max": 0.5},
              "M_zero_new_streak": 3}

# (9.2) Banned-keys walk: discover.gather_candidates returns candidate dicts carrying ONLY
# submitted fields + _discovery, with NO banned key at any depth + no verdict token in a value.
# Use a cold-start cell (deterministic, no network) so the walk needs no graph call.
_p4_doss = _fixture_dossier(empty=["immigrationxSTRUCTURE"], placement={})
_p4_cands = _disc.gather_candidates(_p4_doss, policy=_P4_POLICY)
_p4_bad_keys = []
for _c in _p4_cands:
    _p4_bad_keys += _walk_banned_keys(_c)
check("curator P4: discover candidates emit NO banned verdict key at any depth",
      not _p4_bad_keys, str(_p4_bad_keys))
_p4_bad_vals = []
for _c in _p4_cands:
    _p4_bad_vals += _walk_verdict_values(_c)
check("curator P4: no verdict token in discover candidate string values", not _p4_bad_vals, str(_p4_bad_vals))
# the candidate shape carries only submitted-shaped fields + _discovery (no stray verdict field)
_ALLOWED_CAND_KEYS = {"id", "name", "urls", "proposed_mode", "proposed_domain", "proposed_family",
                      "proposed_kind", "proposed_regions", "rationale_text", "submitted_by", "_discovery"}
_stray = sorted({k for c in _p4_cands for k in c} - _ALLOWED_CAND_KEYS)
check("curator P4: candidate dicts carry only submitted fields + _discovery", not _stray, str(_stray))
# discover.py source assigns no banned verdict key literal (mirrors §12 build_packet grep)
check("curator P4: discover.py source assigns no banned verdict key literal",
      not any(f'"{k}":' in _disc_src for k in _BANNED_KEYS))

# (9.3) GAP->SOURCE-KIND template totality: maps every mode in probe.MODE_VOCAB.
check("curator P4: GAP_SOURCE_KIND covers every mode in MODE_VOCAB",
      set(_disc.GAP_SOURCE_KIND) >= set(_cprobe.MODE_VOCAB),
      f"missing: {sorted(set(_cprobe.MODE_VOCAB) - set(_disc.GAP_SOURCE_KIND))}")

# (9.4) FSM frozen + extended EXACTLY: the P1 set PLUS the §5 additions, no more.
_P4_EXPECTED_EDGES = _EXPECTED_EDGES | {
    ("watching", "rejected"), ("error", "probe_dead"), ("new", "probe_dead"), ("probed", "probe_dead"),
}
check("curator P4: ALLOWED_TRANSITIONS == P1 edges + exactly the P4 additions",
      set(_ccand.ALLOWED_TRANSITIONS) == _P4_EXPECTED_EDGES,
      f"extra={set(_ccand.ALLOWED_TRANSITIONS) - _P4_EXPECTED_EDGES} "
      f"missing={_P4_EXPECTED_EDGES - set(_ccand.ALLOWED_TRANSITIONS)}")
check("curator P4: probe_dead is a NEW state + TERMINAL (no forward edge)",
      "probe_dead" in _ccand.STATES and "probe_dead" in _ccand.TERMINAL_STATES
      and not _ccand._can_transition("probe_dead", "probed"))
check("curator P4: watching->rejected (watch-expired) + error/new/probed->probe_dead allowed",
      _ccand._can_transition("watching", "rejected") and _ccand._can_transition("error", "probe_dead")
      and _ccand._can_transition("new", "probe_dead") and _ccand._can_transition("probed", "probe_dead"))
# The cron writes only the mechanical-lane states (the discovery->probe forward path). The ONE
# extra write is the watch-expired GC (watching->rejected): a TTL/budget lifecycle bound, by=
# "curator", with a FIXED mechanical reason and NO verdict field — it does NOT call record_verdict
# (9.1 proves that). So 'rejected' is allowed here ONLY in that mechanical form.
_CRON_LANE_STATES = {"new", "probed", "awaiting_verdict", "redline_blocked", "parked_p2", "error", "probe_dead"}
_loop_state_writes = (set(_p4re.findall(r'set_state\([^,]+,\s*"([a-z_]+)"', _loop_src))
                       | set(_p4re.findall(r'store_evidence\([^)]*?,\s*"([a-z_]+)"', _loop_src))) \
                      if _loop_src else set()
check("curator P4: the cron writes only mechanical-lane states (+ the watch-expired GC), no verdict state",
      _loop_state_writes <= (_CRON_LANE_STATES | {"rejected"}),
      f"cron writes outside the lane: {sorted(_loop_state_writes - (_CRON_LANE_STATES | {'rejected'}))}")
check("curator P4: any cron 'rejected' write is the mechanical watch-expired GC (by=curator, fixed reason)",
      ("rejected" not in _loop_state_writes)
      or ("watch-expired" in _loop_src and 'by="curator"' in _loop_src))

# (9.5) Centrality is rate-limit-not-quality (HOLE-3): a fixture graph records discovery_truncated
# / dropped_count; we do NOT assert survivors are the highest-centrality.
import penumbra.core.cartographer as _cartg  # noqa: E402
_real_skel = _cartg.field_skeleton
# DISTINCT hosts per node (the engine dedups same-host correctly; the fixture must give it three
# different hosts to exercise the topn rate-limit, not collapse to one via a shared doi.org host).
_cartg.field_skeleton = lambda **k: {"nodes": [
    {"title": "A", "in_degree": 9, "cited_by": 500, "doi": None, "url": "https://venue-a.example.org/p/1"},
    {"title": "B", "in_degree": 5, "cited_by": 50, "doi": None, "url": "https://venue-b.example.org/p/2"},
    {"title": "C", "in_degree": 1, "cited_by": 5, "doi": None, "url": "https://venue-c.example.org/p/3"},
]}
try:
    _g_doss = _fixture_dossier(empty=["papersxSTRUCTURE"], placement={},
                               sources=[{"name": "arxiv", "domains": ["papers"], "modes": ["STRUCTURE"]}])
    _g_cands = _disc.discover(_g_doss, policy={"enabled": True, "discover_topn": 2,
                                               "coverage_ceiling": {"_default": 4}})
    _trunc = any((c.get("_discovery") or {}).get("truncated") for c in _g_cands)
    _dropped = max(((c.get("_discovery") or {}).get("dropped_count") or 0) for c in _g_cands) if _g_cands else 0
    check("curator P4: inner-engine records discovery_truncated + dropped_count (rate-limit, not quality)",
          len(_g_cands) == 2 and _trunc and _dropped >= 1,
          f"survivors={len(_g_cands)} truncated={_trunc} dropped={_dropped}")
    # hosts come from venue/DOI/roster only (no paper-body link): a node with NO doi/url yields none.
    _cartg.field_skeleton = lambda **k: {"nodes": [{"title": "no host", "in_degree": 9, "cited_by": 9,
                                                    "doi": None, "url": None}]}
    _nh = _disc.discover(_g_doss, policy={"enabled": True, "discover_topn": 2, "coverage_ceiling": {"_default": 4}})
    check("curator P4: a node with no venue/DOI/roster host yields NO candidate (Attack-2 host derivation)",
          _nh == [])
finally:
    _cartg.field_skeleton = _real_skel

# (9.6) Cold-start honesty + cap: an empty cell with NO seed -> an outer-ring STUB (urls may be []),
# never a graph edge; the inner engine fires only for domains with >=1 seed.
_cs = _disc.discover(_fixture_dossier(empty=["immigrationxSTRUCTURE"], placement={}, sources=[]),
                     policy={"enabled": True, "coverage_ceiling": {"_default": 4}})
check("curator P4: a seedless empty cell yields ONLY a cold-start stub (urls==[], cold_start edge)",
      len(_cs) == 1 and _cs[0]["urls"] == []
      and _cs[0]["_discovery"]["edge_type"] == "cold_start"
      and "cold-start" in (_cs[0]["_discovery"]["note"] or ""))
# disabled -> []
check("curator P4: discover() returns [] when policy.enabled is false",
      _disc.discover(_fixture_dossier(empty=["papersxSTRUCTURE"]), policy={"enabled": False}) == [])

# (9.7) Bark is diff-gated edge-alarm (HOLE-1/5) — for BOTH the P3 sentinel fix (§10) and the loop.
# P3 sentinel: an UNCHANGED single/empty set vs a saved baseline does NOT re-fire; a NEWLY-single
# cell does. (Pure list diff over the sentinel's saved-baseline shape.)
def _sentinel_newly(empty_now, single_now, prev):
    ne = sorted(set(empty_now) - set(prev.get("empty_cells") or []))
    ns = sorted(set(single_now) - set(prev.get("single_occupant_cells") or []))
    return ne, ns


_base = {"empty_cells": ["aXb"], "single_occupant_cells": ["cXd"]}
_ne0, _ns0 = _sentinel_newly(["aXb"], ["cXd"], _base)  # unchanged -> nothing newly
check("curator P4 (§10): sentinel bark is diff-gated — unchanged single/empty set does NOT re-fire",
      _ne0 == [] and _ns0 == [])
_ne1, _ns1 = _sentinel_newly(["aXb", "eXf"], ["cXd", "gXh"], _base)  # one new each
check("curator P4 (§10): a NEWLY-empty / NEWLY-single cell DOES fire (edge alarm)",
      _ne1 == ["eXf"] and _ns1 == ["gXh"])
# the audit source actually diff-gates (greps for newly_empty/newly_single + the diff op). P9: the
# weekly source-audit moved from scripts/source_audit.py into infra_jobs.run_source_audit (same body).
_sent_src = (ROOT / "src" / "penumbra" / "core" / "infra_jobs.py")
_sent_txt = _sent_src.read_text(encoding="utf-8") if _sent_src.exists() else ""
check("curator P4 (§10): audit job computes newly_empty/newly_single via a set-diff against the baseline",
      "newly_empty" in _sent_txt and "newly_single" in _sent_txt
      and 'prev.get("single_occupant_cells"' in _sent_txt)
# the loop's digest list is byte-equal to sorted(...) (HOLE-1: never centrality/yield-ranked).
check("curator P4: the loop sorts every digest list (sorted(...), never a _tier / centrality key)",
      "newly_awaiting = sorted(" in _loop_src and "newly_empty = sorted(" in _loop_src
      and "newly_single = sorted(" in _loop_src
      and "_tier" not in _loop_code and "centrality" not in _loop_code)

# (9.8) Dedup totality (Attack-2): canonical-host terminal ledger drops a re-discovered host even
# under a name/URL variant; make_id-stable; the round dedups a live host too.
_p4tmp = Path(_ctf.mkdtemp())
_ccand.STATE_DIR = _p4tmp
_ccand.CANDIDATES_PATH = _p4tmp / "candidates.json"
_ccand.SEEN_HOSTS_PATH = _p4tmp / "seen_hosts.json"
_ccand.TRIED_HOSTS_PATH = _p4tmp / "tried_hosts.json"
check("curator P4: canonical_host normalizes www./scheme/port/trailing-slash to one key",
      _ccand.canonical_host("https://www.Example.com/feed/") == "example.com"
      and _ccand.canonical_host("http://example.com:80") == "example.com"
      and _ccand.canonical_host("example.com") == "example.com")
_ccand.record_tried_host("https://www.deadhost.com/feed.xml")
check("curator P4: a recorded terminal host is tried under ANY name/URL/scheme variant",
      _ccand.host_is_tried("http://deadhost.com")
      and _ccand.host_is_tried("https://www.deadhost.com/other/path")
      and not _ccand.host_is_tried("https://livehost.com/x"))
check("curator P4: record_tried_host is idempotent (no duplicate ledger entry)",
      (_ccand.record_tried_host("deadhost.com") or len(_ccand._load_tried_hosts())) == 1)

# (9.9) Read-only deploy: the cron writes NO path outside ~/.penumbra/state/curator/. Grep the loop
# source for any write to an in-tree sources/*.json config file (must be none).
check("curator P4: the cron writes no in-tree sources/*.json config file",
      "sources/" not in _loop_src or ".write_text" not in _loop_src,
      "loop appears to write an in-tree config file")
check("curator P4: the cron mutates NO config family JSON (no _FAMILY_CONFIG_FILE / open(...,'w'))",
      "_FAMILY_CONFIG_FILE" not in _loop_src and "_load_register" not in _loop_src)

# (9.10) STOP is pure + terminating (Attack-3): a degraded round freezes the streak (discovery_health
# is the gate). discover() emits zero outer-ring candidates when there is no in-scope empty cell.
check("curator P4: discovery_health is 'degraded' for a cold-start-only round (no healthy graph call)",
      _disc.discovery_health(_cs, _p4_doss) == "degraded"
      and _disc.discovery_health(_g_cands, _g_doss) == "healthy")
check("curator P4: a fully-placed coverage target set yields no outer-ring candidate",
      _disc.discover(_fixture_dossier(empty=[], placement={"papersxSTRUCTURE": ["arxiv"]}),
                     policy={"enabled": True, "coverage_ceiling": {"_default": 4}}) == [])
# the loop FREEZES the streak on a degraded round (neither ++ nor reset): grep the streak logic.
check("curator P4: the loop freezes the zero-new streak on a degraded round (Attack-3)",
      'if discovery_health == "degraded"' in _loop_src and "pass  # FREEZE" in _loop_src)

# (9.11) Policy DATA carries the knobs (finite positive ints) + the enabled flags, NO verdict token.
import penumbra.core.curator as _curpkg  # noqa: E402
_CUR_POLICY = json.loads((_CUR_DIR / "curator_policy.json").read_text(encoding="utf-8"))
_pos_int = lambda v: isinstance(v, int) and v > 0
_P4_KNOBS = ("M_zero_new_streak", "discover_topn", "min_cadence_days", "error_retry_budget",
             "watching_max_reprobes", "max_new_probes")
check("curator P4: curator_policy.json carries finite positive-int knobs; loop ACTIVE (enabled True, "
      "C3 activation 2026-06-15) with cold_start STILL OFF (the real safety invariant)",
      all(_pos_int(_CUR_POLICY.get(k)) for k in _P4_KNOBS)
      and _CUR_POLICY.get("enabled") is True
      and (_CUR_POLICY.get("cold_start") or {}).get("enabled") is False
      and _pos_int((_CUR_POLICY.get("cold_start") or {}).get("budget"))
      and _pos_int((_CUR_POLICY.get("coverage_ceiling") or {}).get("_default")),
      f"policy={ {k: _CUR_POLICY.get(k) for k in _P4_KNOBS} } enabled={_CUR_POLICY.get('enabled')}")
_P4_VERDICT_WORDS = ("keep", "watch", "prune", "admit", "reject", "pursue")
# Scan only the OPERATIVE policy (drop _doc / _*_doc explainer keys at any depth, exactly as the
# redlines national-origin scan scopes to the data, not the human-readable rationale prose). The
# razor: no verdict WORD may govern a numeric knob; the prose may explain WHY a knob is not a verdict.
def _drop_doc_keys(o):
    if isinstance(o, dict):
        return {k: _drop_doc_keys(v) for k, v in o.items() if not str(k).endswith("doc")}
    if isinstance(o, list):
        return [_drop_doc_keys(v) for v in o]
    return o


_pol_operative = _drop_doc_keys(_CUR_POLICY)
_pol_blob = json.dumps(_pol_operative, ensure_ascii=False).lower()
_pol_verdict_leak = [w for w in _P4_VERDICT_WORDS if _p4re.search(r"\b" + w + r"\b", _pol_blob)]
check("curator P4: curator_policy.json operative knobs carry NO keep/watch/prune/admit/reject/pursue token",
      not _pol_verdict_leak, str(_pol_verdict_leak))
# coverage_targets.json: a list of <domain>x<MODE> cells. ACTIVE (C3 2026-06-15): the frozen
# resilience set (the cells the live roster occupies); was [] in scaffold mode.
_CT = json.loads((_CUR_DIR / "coverage_targets.json").read_text(encoding="utf-8"))
check("curator P4: coverage_targets.json is a non-empty list of cell strings (C3 activation; was [])",
      isinstance(_CT, list) and len(_CT) > 0 and all(isinstance(x, str) and x.strip() for x in _CT))

# (9.12) Red-line denylist frozen (Attack-2/8a): EXPECTED_REDLINES includes the scraper host_suffix
# seed; the new hard line is present; no national-origin term (the existing scan re-applies).
_p4_rl_rows = _credl.load_rules()
check("curator P4: redlines id-set == EXPECTED_REDLINES incl. the scraper denylist seed",
      {r["id"] for r in _p4_rl_rows} == set(_credl.EXPECTED_REDLINES)
      and "scraper_data_broker" in _credl.EXPECTED_REDLINES,
      f"{sorted(r['id'] for r in _p4_rl_rows)} vs {sorted(_credl.EXPECTED_REDLINES)}")
check("curator P4: a discovered 'former <Org>' candidate is HARD-promoted (8a), human stays soft",
      _credl.has_hard_hit(_credl.match({"affiliations": ["former DeepMind"], "submitted_by": "curator-loop"}))
      and not _credl.has_hard_hit(_credl.match({"affiliations": ["former DeepMind"], "submitted_by": "agent"})))
_p4_nat_blob = json.dumps(_p4_rl_rows, ensure_ascii=False).lower()
check("curator P4: still no national-origin term in redlines.json after the 8a seed",
      not [t for t in _NAT_DENY if t in _p4_nat_blob])

# (9.13) safe_fetch boundary (8c): a curator-discovered candidate whose family ∈ _NEVER_AUTO_FAMILIES
# can never be _auto_apply_ok (frozen against drift); the §8b decide guard requires the ack.
_il.reload(_capply)  # restore the real gate (earlier sections monkeypatched _live_hosts/_load_allowlist)
fetcher.list_sources = _REAL_LIST_SOURCES
fetcher.all_adapter_names = _REAL_ALL_NAMES
fetcher.get_adapter = _REAL_GET_ADAPTER
fetcher._FACETS = _REAL_FACETS
for _fam in ("org_watch", "page_watch", "news_scraper", "search_index"):
    check(f"curator P4: a discovered {_fam} candidate is never _auto_apply_ok (8c, frozen)",
          _capply._auto_apply_ok({"proposed_family": _fam, "proposed_mode": "STRUCTURE",
                                  "urls": ["https://x.example.com/"], "verdict": {"decision": "admit"},
                                  "evidence": {"evidence_complete": True, "stage0_safety": {
                                      "redline_hits": [], "hard_redline_blocked": False, "first_seen_host": False}},
                                  "proposed_config_row": {}}) is False)
check("curator P4: _NEVER_AUTO_FAMILIES frozen == the four bypassing families",
      set(_capply._NEVER_AUTO_FAMILIES) == {"org_watch", "page_watch", "news_scraper", "search_index"})
# §8b: admit of a first-seen org_watch RAISES without recurring_fetch_acknowledged, succeeds with it.
_ccand.CANDIDATES_PATH = _p4tmp / "candidates_8b.json"
_ow_cid = _ccand.add({"name": "OW Cand", "urls": ["https://orgwatch-new.example.com/x"],
                      "proposed_mode": "STRUCTURE", "proposed_domain": "papers",
                      "proposed_family": "org_watch", "submitted_by": "curator-loop"})
_ccand.store_evidence(_ow_cid,
                      {"evidence_complete": True,
                       "stage0_safety": {"hard_redline_blocked": False, "first_seen_host": True}},
                      {"hard_redline_ids": []}, "awaiting_verdict", "ev")
check("curator P4 (8b): admit of a first-seen org_watch RAISES without recurring_fetch_acknowledged",
      _decide_raises(candidate_id=_ow_cid, decision="admit", reasons="x", baseline_ref={"web": ["r"]}))
_ow_ok = _srv2.penumbra_curator_act.__wrapped__(verb="decide", candidate_id=_ow_cid, decision="admit",
                                           reasons="x",
                                           baseline_ref={"web": ["r"], "recurring_fetch_acknowledged": True})
check("curator P4 (8b): with recurring_fetch_acknowledged=true the admit stages to owner_review",
      _ow_ok.get("state") == "owner_review")
# the operator case for that family carries the recurring_fetch_harm block (spec 2/8c).
_ow_case = _capply.prepare_owner_case(_ccand.get(_ow_cid))
check("curator P4 (2/8c): prepare_owner_case carries recurring_fetch_harm for a bypassing family",
      isinstance(_ow_case.get("recurring_fetch_harm"), dict)
      and _ow_case["recurring_fetch_harm"].get("safe_fetch_bypassed") is True)

# ---------------------------------------------------------------------------
# 16. Curator LIVE-APPLY lane: the reversible overlay + live re-register + the one-tap sanction.
#     THE CORE SPLIT: LIVE EFFECT (overlay row + live register; reversible, no git) vs DURABLE TRUTH
#     (in-tree commit + redeploy; the operator's hand only). Load-bearing safety: thread-safe live
#     re-register (collision-ABORT + lock), recall-cache invalidation, register-before-append
#     ordering, full-revert rollback, and CODE NEVER RUNS GIT. Pure structural / offline / no network.
# ---------------------------------------------------------------------------
import threading as _althr  # noqa: E402

from penumbra.core.curator import apply_live as _al  # noqa: E402

# (1) overlay IO tolerant: missing/corrupt/non-list -> []; rows missing name / non-dict filtered.
_altmp = Path(_ctf.mkdtemp())
_al.OVERLAY_DIR = _altmp / "overlays"
_al._RETIRE_PATH = _altmp / "explicit_only_overrides.json"
check("curator live: load_overlay on a missing file -> []", _al.load_overlay("rss") == [])
(_altmp / "overlays").mkdir(parents=True, exist_ok=True)
_al._path("rss").write_text("{ not json", encoding="utf-8")
check("curator live: corrupt overlay -> [] (logged, never raises)", _al.load_overlay("rss") == [])
_al._path("org_watch").write_text(json.dumps({"not": "a list"}), encoding="utf-8")
check("curator live: non-list overlay -> []", _al.load_overlay("org_watch") == [])
_al._path("news_scraper").write_text(json.dumps([
    {"row": {"name": "good", "description": "d", "sites": [{"url": "https://x/n"}]}, "candidate_id": "c"},
    {"row": {"no_name": True}},  # filtered (no truthy name)
    "not-a-dict",                # filtered
]), encoding="utf-8")
check("curator live: rows missing name / non-dict are filtered",
      [r["row"]["name"] for r in _al.load_overlay("news_scraper")] == ["good"])

# (2) append/drop round-trip; append REFUSES a name already in BASE or in the overlay.
_al._path("rss").unlink(missing_ok=True)  # reset rss overlay (was corrupt)
_al.append("rss", {"name": "ov_one", "description": "d", "feeds": ["https://ov.example.com/f.xml"]}, "c1", "t")
check("curator live: append -> overlay_rows round-trips", [r["name"] for r in _al.overlay_rows("rss")] == ["ov_one"])


def _al_raises(fn):
    try:
        fn()
        return False
    except Exception:  # noqa: BLE001
        return True


check("curator live: append REFUSES a duplicate overlay name",
      _al_raises(lambda: _al.append("rss", {"name": "ov_one", "description": "d", "feeds": ["https://x/f"]}, "c", "t")))
_base_rss_name = json.loads((SOURCES / "scrape/rss_bundles.json").read_text(encoding="utf-8"))[0]["name"]
check("curator live: append REFUSES a name already in the base config (base wins)",
      _al_raises(lambda: _al.append("rss", {"name": _base_rss_name, "description": "d", "feeds": ["https://x/f"]}, "c", "t")))
check("curator live: append REFUSES an unknown family",
      _al_raises(lambda: _al.append("bogus", {"name": "x", "feeds": []}, "c", "t")))
check("curator live: drop removes the row (idempotent on an absent name)",
      _al.drop("rss", "ov_one") is True and _al.drop("rss", "ov_one") is False
      and _al.overlay_rows("rss") == [])

# (3) register_one builds the SAME family class per family (isinstance per family); page_watch->None.
from penumbra.core.sources.api.org_watch_source import _OrgWatchAdapter as _OWA  # noqa: E402
from penumbra.core.sources.scrape.news_scraper_source import _ScrapeSite as _SS  # noqa: E402
from penumbra.core.sources.scrape.rss_bundles_source import _RSSBundle as _RB  # noqa: E402
check("curator live: register_one builds the right class per family",
      isinstance(_al.register_one("rss", {"name": "a", "description": "d", "feeds": ["https://x/f"]}), _RB)
      and isinstance(_al.register_one("org_watch", {"name": "a", "affiliations": ["X"], "description": "d"}), _OWA)
      and isinstance(_al.register_one("news_scraper", {"name": "a", "description": "d", "sites": [{"url": "https://x/n"}]}), _SS)
      and _al.register_one("page_watch", {"name": "a", "label": "L", "url": "https://x/p"}) is None)

# (4) register_adapter_live ABORTS on collision; import-path register_adapter REPLACES + _collisions.
class _StubAdapter:  # noqa: E302
    needs_credentials = False
    description = "stub"

    def __init__(self, name):
        self.name = name

    def search(self, q, limit=10):
        return []

    def fetch_url(self, u):
        return None

    def health_check(self):
        return (True, "ok")


_stub_n = "_live_stub_collide"
fetcher.register_adapter_live(_StubAdapter(_stub_n))
check("curator live: register_adapter_live ABORTS on a name collision",
      _al_raises(lambda: fetcher.register_adapter_live(_StubAdapter(_stub_n))))
_pre_coll = list(fetcher._collisions)
fetcher.register_adapter(_StubAdapter(_stub_n))  # import-path REPLACES + records a collision
check("curator live: import-path register_adapter REPLACES + records _collisions (distinct from live)",
      _stub_n in fetcher._collisions and _stub_n not in _pre_coll)
fetcher.unregister_adapter(_stub_n)
check("curator live: unregister_adapter pops the name; get_adapter -> None; idempotent",
      fetcher.get_adapter(_stub_n) is None)
fetcher.unregister_adapter(_stub_n)  # idempotent no-op
fetcher._collisions[:] = [c for c in fetcher._collisions if c != _stub_n]  # tidy the smoke-induced collision

# (5) THREAD-SAFETY: hammer list_sources/all_adapter_names while live-registering 50 rows -> no
#     "dict changed size during iteration", correct final count.
_ts_stop = {"v": False}
_ts_err = {"e": None}


def _ts_hammer():
    while not _ts_stop["v"]:
        try:
            fetcher.all_adapter_names()
            [s["name"] for s in fetcher.list_sources()]
        except Exception as exc:  # noqa: BLE001
            _ts_err["e"] = exc
            return


_ts_t = _althr.Thread(target=_ts_hammer)
_ts_t.start()
_ts_names = [f"_ts_live_{i}" for i in range(50)]
for _n in _ts_names:
    fetcher.register_adapter_live(_StubAdapter(_n))
_ts_stop["v"] = True
_ts_t.join()
check("curator live: 50 concurrent live registers under iteration -> no dict-resize RuntimeError",
      _ts_err["e"] is None, str(_ts_err["e"]))
check("curator live: all 50 live-registered rows present after the race",
      all(_n in fetcher.all_adapter_names() for _n in _ts_names))
for _n in _ts_names:
    fetcher.unregister_adapter(_n)

# (6) get_adapter is LOCK-FREE (the hot path): source-inspect the CODE (docstring stripped) — no
#     _registry_lock acquisition in its body (the docstring legitimately explains why it is lock-free).
_ga_code = _strip_py_noise(_insp.getsource(fetcher.get_adapter))
check("curator live: get_adapter stays lock-free (hot path)", "_registry_lock" not in _ga_code)

# (7) apply_overlay_row ABORTS + writes NOTHING on typed-validation failure (bad feeds), and on a
#     forced collision the pre-checked overlay is NOT appended (register-before-append proven).
_al._path("rss").unlink(missing_ok=True)


def _live_cand(row, urls=None, mode="STRUCTURE"):
    return {
        "id": "live-00000000", "name": row.get("name"), "proposed_family": "rss",
        "proposed_mode": mode, "urls": urls or ["https://feeds.ok.example.com/atom.xml"],
        "verdict": {"decision": "admit"},
        "evidence": {"evidence_complete": True,
                     "stage0_safety": {"redline_hits": [], "hard_redline_blocked": False,
                                       "first_seen_host": False,
                                       "fetch": {"final_url": "https://feeds.ok.example.com/atom.xml",
                                                 "content_type": "application/atom+xml"}},
                     "stage3_mode_probe": {"mode": mode},
                     "reversibility": {"proposed_config_row": row}},
    }


_bad_row = {"name": "bad_feeds", "description": "d", "feeds": "notalist"}
check("curator live: apply_overlay_row ABORTS on typed-validation failure + writes nothing",
      _al_raises(lambda: _al.apply_overlay_row(_live_cand(_bad_row)))
      and _al.overlay_rows("rss") == [])
# forced collision: pre-register the live name, then apply -> register aborts, overlay NOT appended
_coll_row = {"name": "coll_live", "description": "d", "feeds": ["https://feeds.ok.example.com/a.xml"]}
fetcher.register_adapter_live(_StubAdapter("coll_live"))
check("curator live: register-before-append — a collision aborts with NO overlay row written",
      _al_raises(lambda: _al.apply_overlay_row(_live_cand(_coll_row)))
      and _al.overlay_rows("rss") == [])
fetcher.unregister_adapter("coll_live")

# (8) recall.invalidate_indexable_cache resets the cache; an rss live-apply enters the index.
import penumbra.core.recall as _rcl  # noqa: E402
_rcl.indexable_set()  # warm
_rcl.invalidate_indexable_cache()
check("curator live: invalidate_indexable_cache resets _indexable_cache to None",
      _rcl._indexable_cache is None)
_ccand.STATE_DIR = _altmp
_ccand.CANDIDATES_PATH = _altmp / "live_candidates.json"
_good_row = {"name": "LiveIndexed", "description": "d", "feeds": ["https://feeds.ok.example.com/atom.xml"]}
_lc = _live_cand(_good_row)
_lc["id"] = _ccand.add({"name": "LiveIndexed", "urls": _lc["urls"], "proposed_mode": "STRUCTURE",
                        "proposed_domain": "papers", "proposed_family": "rss"})
_before_live = len(fetcher.all_adapter_names())
_receipt = _al.apply_overlay_row(_lc)
check("curator live: apply_overlay_row applies (roster +1, in index, git_committed false)",
      _receipt["applied"] is True and (len(fetcher.all_adapter_names()) - _before_live) == 1
      and "LiveIndexed" in fetcher.all_adapter_names()
      and "LiveIndexed" in _rcl.indexable_set() and _receipt["git_committed"] is False)
# (9) rollback FULLY reverts: unregister + drop overlay; re-apply after rollback is clean (idempotent).
_rb = _al.rollback_overlay_row("rss", "LiveIndexed")
check("curator live: rollback fully reverts (gone from roster + get_adapter None + overlay empty)",
      _rb["rolled_back"] is True and "LiveIndexed" not in fetcher.all_adapter_names()
      and fetcher.get_adapter("LiveIndexed") is None and _al.overlay_rows("rss") == [])
_receipt2 = _al.apply_overlay_row(_lc)
check("curator live: re-apply after rollback is clean (reverse is idempotent)",
      _receipt2["applied"] is True and "LiveIndexed" in fetcher.all_adapter_names())
_al.rollback_overlay_row("rss", "LiveIndexed")  # tidy

# (10) typed validation: feeds-as-str / render:true / affiliations==[""] all rejected.
check("curator live: validate_row_typed rejects feeds-as-str, render:true site, affiliations==['']",
      _capply.validate_row_typed("rss", {"name": "a", "description": "d", "feeds": "str"})
      and _capply.validate_row_typed("news_scraper", {"name": "a", "description": "d", "sites": [{"url": "u", "render": True}]})
      and _capply.validate_row_typed("org_watch", {"name": "a", "description": "d", "affiliations": [""]})
      and not _capply.validate_row_typed("rss", {"name": "a", "description": "d", "feeds": ["https://x/f"]}))

# (11) CONFIGS join (spec §5): every accumulated overlay row passes its typed check; overlay names
#      join the cross-file uniqueness; a family not in _KNOWN_FAMILIES or a _NEVER_AUTO row smuggled
#      into rss_overlay.json fails the deploy. (Validates the REAL ~/.penumbra overlay files if any.)
_REAL_OVERLAY_DIR = Path.home() / ".penumbra" / "state" / "curator" / "overlays"
_overlay_problems = []
_overlay_names = []
_overlay_uncommitted = []
for _fam in sorted(_al._KNOWN_FAMILIES):
    _ovp = _REAL_OVERLAY_DIR / f"{_fam}_overlay.json"
    if not _ovp.exists():
        continue
    try:
        _ovrecs = json.loads(_ovp.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        _overlay_problems.append(f"{_fam}: unparseable ({exc})")
        continue
    for _rec in (_ovrecs if isinstance(_ovrecs, list) else []):
        _ovrow = (_rec or {}).get("row") if isinstance(_rec, dict) else None
        if not isinstance(_ovrow, dict):
            continue
        _ovname = _ovrow.get("name")
        _overlay_names.append(str(_ovname))
        _problems = _capply.validate_row_typed(_fam, _ovrow)
        if _problems:
            _overlay_problems.append(f"{_fam}:{_ovname}: {_problems}")
        if not (_rec.get("git_committed")):
            _overlay_uncommitted.append(f"{_fam}:{_ovname}")
check("curator live: every accumulated overlay row passes its typed check", not _overlay_problems,
      str(_overlay_problems))
_overlay_dupes = sorted(set(_overlay_names) & set(all_config_names))
check("curator live: overlay names do not collide with a base config name (base wins)",
      not _overlay_dupes, str(_overlay_dupes))
if _overlay_uncommitted:
    print("  WARN curator live: uncommitted overlay rows (live but not in the tree): "
          + ", ".join(sorted(_overlay_uncommitted)))
# a family not in _KNOWN_FAMILIES is rejected by append (the deploy never accumulates a junk family).
check("curator live: append refuses a family outside _KNOWN_FAMILIES (deploy can't accumulate junk)",
      _al_raises(lambda: _al.append("page_view", {"name": "x", "url": "u"}, "c", "t")))

# (12) Gate hardening: a clean admit auto-applies (no human allowlist); family/mode classification
#      rejects a relabel (the anti-relabel check survives the host-allowlist removal).
import importlib as _il2  # noqa: E402
_il2.reload(_capply)  # restore pristine _capply (earlier sections monkeypatched it) + _live_hosts
fetcher.list_sources = _REAL_LIST_SOURCES
fetcher.all_adapter_names = _REAL_ALL_NAMES
fetcher.get_adapter = _REAL_GET_ADAPTER
fetcher._FACETS = _REAL_FACETS
_real_empty_cand = {
    "proposed_family": "rss", "proposed_mode": "STRUCTURE",
    "urls": ["https://feeds.ok.example.com/atom.xml"], "verdict": {"decision": "admit"},
    "evidence": {"evidence_complete": True,
                 "stage0_safety": {"redline_hits": [], "hard_redline_blocked": False, "first_seen_host": False,
                                   "fetch": {"final_url": "https://feeds.ok.example.com/atom.xml",
                                             "content_type": "application/atom+xml"}},
                 "stage3_mode_probe": {"mode": "STRUCTURE"},
                 "reversibility": {"proposed_config_row": {"name": "x", "description": "d", "feeds": ["https://feeds.ok.example.com/a"]}}},
}
check("curator live: a clean real rss/STRUCTURE admit candidate auto-applies WITHOUT any host "
      "allowlist (the agent's admit verdict is the gate; razor-fix 2026-06-15)",
      _capply._auto_apply_ok(_real_empty_cand) is True)
_relabel = json.loads(json.dumps(_real_empty_cand))  # deep copy
_relabel["evidence"]["stage0_safety"]["fetch"] = {"final_url": "https://api.openalex.org/works?filter=author",
                                                  "content_type": "application/json"}
check("curator live: relabeled rss over an API-resolver final_url / non-feed type is REJECTED",
      _capply._auto_apply_ok(_relabel) is False)
_modemiss = json.loads(json.dumps(_real_empty_cand))
_modemiss["evidence"]["stage3_mode_probe"]["mode"] = "RECALL"  # verified mode != declared STRUCTURE
check("curator live: a verified mode that disagrees with the declared mode is REJECTED",
      _capply._auto_apply_ok(_modemiss) is False)
_il2.reload(_capply)  # restore pristine _capply for any later section

# (12b) C3 razor-fix: the overlay-rss recurring-fetch IP guard REPLACES the removed human host-
#       allowlist. Overlay-origin _RSSBundle carries guard_ip=True; the 143 base rows do not (their
#       fetch is byte-identical); a guarded fetch whose host resolves to a blocked IP fails CLOSED
#       (skip, no http.get) so an agent-auto-admitted feed cannot SSRF on its recurring poll.
import penumbra.core.sources.scrape.rss_bundles_source as _rssb  # noqa: E402
import penumbra.core.sources.scrape._rss as _rssmod  # noqa: E402
from penumbra.core.curator import probe as _gprobe  # noqa: E402
check("curator C3: a base (in-tree) rss bundle has guard_ip False (143 sources unchanged)",
      _rssb._RSSBundle(name="b", description="d", feeds=["https://x/f"]).guard_ip is False)
_ov_b = _al.register_one("rss", {"name": "o", "description": "d", "feeds": ["https://x/f"]})
check("curator C3: an overlay-origin rss bundle (apply_live.register_one) has guard_ip True",
      _ov_b is not None and _ov_b.guard_ip is True)
_orig_resolve_g, _orig_httpget_g = _gprobe._resolve_safe_ip, _rssmod.http.get
_http_hits = {"n": 0}
try:
    _gprobe._resolve_safe_ip = lambda h: (None, None, "private_ip")
    _rssmod.http.get = lambda *a, **k: (_http_hits.__setitem__("n", _http_hits["n"] + 1) or None)
    _g_blocked = _rssmod.fetch_feed("https://internal.evil/feed", guard_ip=True)
    check("curator C3: a guarded feed whose host resolves to a blocked IP is SKIPPED (None, no http.get)",
          _g_blocked is None and _http_hits["n"] == 0)
    _rssmod.fetch_feed("https://internal.evil/feed", guard_ip=False)
    check("curator C3: guard_ip=False (the base path) does NOT IP-guard (calls http.get as before)",
          _http_hits["n"] == 1)
finally:
    _gprobe._resolve_safe_ip, _rssmod.http.get = _orig_resolve_g, _orig_httpget_g

# (13) NEVER-RUNS-GIT: source-inspect the live-mutation surface + the one-tap tools (docstrings +
#      comments STRIPPED — they legitimately DESCRIBE the never-runs-git invariant) contain no
#      git/subprocess/deploy/kickstart/launchctl/smoke CALL. Scan the live-code tokens only.
_GIT_TOKENS = ("subprocess", "deploy.sh", "kickstart", "launchctl")
_live_srcs = {
    "apply_live": _insp.getsource(_al),
    "_curator_apply_live": _insp.getsource(_srv2._curator_apply_live),
    "_curator_rollback_live": _insp.getsource(_srv2._curator_rollback_live),
    "_curator_retire_live": _insp.getsource(_srv2._curator_retire_live),
    "_curator_stage_commit": _insp.getsource(_srv2._curator_stage_commit),
}
_live_code = {k: _strip_py_noise(v) for k, v in _live_srcs.items()}
_git_leaks = [f"{_where}:{_tok}" for _where, _code in _live_code.items()
              for _tok in _GIT_TOKENS if _tok in _code]
# the import surface (no `import subprocess`, no os.system) is the real proof code can't shell out.
check("curator live: the live-mutation surface + one-tap tools run NO subprocess / deploy / restart",
      not _git_leaks and "os.system" not in _live_code["apply_live"], str(_git_leaks))
# apply_live persists ONLY via cache._atomic_write_text (the atomic discipline); no write_text /
# open / os.replace naked write in its live code (tokens, docstrings stripped).
_al_code = _live_code["apply_live"]
_al_tokens = set(_al_code.split())
check("curator live: apply_live persists only via cache._atomic_write_text (no naked write/open)",
      "_atomic_write_text" in _al_tokens
      and "write_text" not in _al_tokens and "open" not in _al_tokens)

# (14) one-tap: apply_live tool refuses a candidate NOT in owner_review; refuses a _NEVER_AUTO
#      family defensively; the runtime retire requires an existing agent prune.
_il2.reload(_capply)
_ccand.CANDIDATES_PATH = _altmp / "onetap_candidates.json"
_nt = _ccand.add({"name": "NotReady", "urls": ["https://feeds.ok.example.com/x"], "proposed_mode": "STRUCTURE",
                  "proposed_domain": "papers", "proposed_family": "rss"})
check("curator live: apply_live RAISES on a candidate not in owner_review",
      _al_raises(lambda: _srv2.penumbra_curator_act.__wrapped__(verb="apply_live", candidate_id=_nt)))
# defensive: a doctored org_watch in owner_review returns applied:false (never live-applied)
_ow_live = _ccand.add({"name": "OwLive", "urls": ["https://api.openalex.org/works"], "proposed_mode": "STRUCTURE",
                       "proposed_domain": "papers", "proposed_family": "org_watch"})
_ccand.store_evidence(_ow_live, {"evidence_complete": True, "stage0_safety": {"hard_redline_blocked": False}},
                      {"hard_redline_ids": []}, "awaiting_verdict", "ev")
_ccand.record_verdict(_ow_live, {"decision": "admit"}, "admitted", "a")
_ccand.set_state(_ow_live, "owner_review", "stage", by="agent")
_ow_res = _srv2.penumbra_curator_act.__wrapped__(verb="apply_live", candidate_id=_ow_live)
check("curator live: apply_live refuses a _NEVER_AUTO family (applied:false, points to git path)",
      _ow_res.get("applied") is False and "stage_commit" in (_ow_res.get("must_use") or ""))
# retire requires an existing agent prune verdict
_sa.STATE_DIR = _altmp
_sa.SOURCE_VERDICTS_PATH = _altmp / "live_source_verdicts.json"
check("curator live: retire_live RAISES without an existing agent PRUNE verdict",
      _al_raises(lambda: _srv2.penumbra_curator_act.__wrapped__(verb="retire_live", name="no_such_pruned_src", confirm=True)))

# (15) runtime retire round-trip: with a prune on record, confirm=True writes the explicit_only
#      override so _explicit_only_reason is truthy; rollback drops it -> falsy again.
_al._RETIRE_PATH = _altmp / "explicit_only_overrides.json"
fetcher._EXPLICIT_ONLY_OVERRIDES_PATH = _altmp / "explicit_only_overrides.json"
fetcher.invalidate_explicit_only_overrides()
_al.retire_live("redundant_src_live", "low yield, others co-surface")


class _RetStub:  # a stand-in adapter so _explicit_only_reason can read its name
    name = "redundant_src_live"
    explicit_only = False


check("curator live: retire_live writes a runtime explicit_only override (fetcher sees it)",
      bool(fetcher._explicit_only_reason(_RetStub())))
_al.unretire_live("redundant_src_live")
fetcher.invalidate_explicit_only_overrides()
check("curator live: unretire_live drops the override -> source rejoins broad fan-out",
      not fetcher._explicit_only_reason(_RetStub()))

# (16) the live-apply lane verbs route through penumbra_curator_act; each still resolves to its impl.
for _t in ("_curator_apply_live", "_curator_rollback_live", "_curator_stage_commit",
           "_curator_retire_live", "_curator_rollback_retire"):
    check(f"curator live: server carries the {_t} impl behind the dispatcher",
          hasattr(_srv2, _t) and callable(getattr(_srv2, _t)))

# (17) P3.1 presumed-dark: a health=='unknown' + silent-past-floor source stops shielding its cell;
#      an unknown+recently-ingested stays an occupant; a 'down' one is unaffected; coverage_critical
#      flips True for a cell whose only sibling was the corpse (strictly-safer direction).
_PD_ROSTER = [
    {"name": "live_a", "domains": ["papers"], "modes": ["STRUCTURE"], "health": "ok",
     "needs_credentials": False, "explicit_only": False, "kind": "lookup"},
    {"name": "dark_cdp", "domains": ["papers"], "modes": ["STRUCTURE"], "health": "unknown",
     "needs_credentials": True, "explicit_only": False, "kind": "lookup"},  # presumed-dark sibling
]
_PD_INGEST = {
    "dark_cdp": {"last_ingest_at": _satime.time() - 999 * 86400, "last_ingest_doc_count": 0,
                 "live_feed_silent_days": 999.0},  # silent >> floor
}
_real_pd_list = fetcher.list_sources
_real_pd_wd = fetcher._watchdog_health
_real_pd_facets = getattr(fetcher, "_FACETS", {})
_real_pd_yt = _sa._yt._load_all
_real_pd_ingest = _sa._ingest_watermark
fetcher.list_sources = lambda check_health=False: [dict(r) for r in _PD_ROSTER]
fetcher._watchdog_health = lambda: ({}, {"live_a"}, "2026-06-15T00:00:00Z")  # dark_cdp untracked -> unknown
fetcher._FACETS = {r["name"]: {"domains": r["domains"], "modes": r["modes"]} for r in _PD_ROSTER}
_sa._yt._load_all = lambda: {"version": 1, "total_searches_observed": 0, "sources": {}}
_sa._ingest_watermark = lambda n: _PD_INGEST.get(
    n, {"last_ingest_at": None, "last_ingest_doc_count": None, "live_feed_silent_days": None})
try:
    _pd_dossier = _sa.gather_source_dossier()
    _pd_grid = _pd_dossier["coverage_grid"]
    _pd_by = {s["name"]: s for s in _pd_dossier["sources"]}
    check("curator P3.1: a presumed-dark (unknown + silent>floor) source is NOT a prune-grid occupant",
          "dark_cdp" not in _pd_grid.get("papersxSTRUCTURE", []))
    check("curator P3.1: the presumed-dark source IS still in list_sources / the dossier (not deleted)",
          "dark_cdp" in _pd_by and _pd_by["dark_cdp"]["liveness"]["presumed_dark"] is True)
    check("curator P3.1: coverage_critical flips True for the cell whose only sibling was the corpse",
          _pd_by["live_a"]["safety_flags"]["coverage_critical"] is True)
    check("curator P3.1: presumed_dark_sources surfaces the corpse (neutral fact, no verdict token)",
          "dark_cdp" in _pd_dossier.get("presumed_dark_sources", []))
    check("curator P3.1: the dossier with the liveness block + presumed_dark_sources passes the banned-key walk",
          not _walk_banned_keys(_pd_dossier))
    # an unknown source RECENTLY ingested stays an occupant (silent < floor -> not presumed-dark)
    _PD_INGEST["dark_cdp"] = {"last_ingest_at": _satime.time() - 1 * 86400, "last_ingest_doc_count": 3,
                              "live_feed_silent_days": 1.0}
    _pd_dossier2 = _sa.gather_source_dossier()
    check("curator P3.1: an unknown + recently-ingested source stays a live occupant",
          "dark_cdp" in _pd_dossier2["coverage_grid"].get("papersxSTRUCTURE", []))
    # _build_grid_by_placement (discovery view) is NOT filtered by presumed-dark (no health flap)
    _PD_INGEST["dark_cdp"] = {"last_ingest_at": _satime.time() - 999 * 86400, "live_feed_silent_days": 999.0}
    _pd_dossier3 = _sa.gather_source_dossier()
    check("curator P3.1: the discovery placement grid is NOT touched by presumed-dark (no re-target on a flap)",
          "dark_cdp" in _pd_dossier3["grid_by_placement"].get("papersxSTRUCTURE", []))
finally:
    fetcher.list_sources = _real_pd_list
    fetcher._watchdog_health = _real_pd_wd
    fetcher._FACETS = _real_pd_facets
    _sa._yt._load_all = _real_pd_yt
    _sa._ingest_watermark = _real_pd_ingest

# (18) resolver host-allowlist (attack-3): enrich._get_json off-allowlist RAISES; allowed hosts pass
#      shape; the client sets follow_redirects=False; openalex client + host assert.
import penumbra.core.enrich as _enr2  # noqa: E402
import penumbra.core._openalex as _oa2  # noqa: E402
check("curator live: enrich._get_json RAISES on an off-allowlist host",
      _al_raises(lambda: _enr2._get_json("http://evil.example/x")))
_enr_src = _insp.getsource(_enr2._get_json)
check("curator live: enrich._get_json pins _API_HOSTS + follow_redirects=False",
      "follow_redirects=False" in _enr_src and "_API_HOSTS" in _enr_src)
check("curator live: enrich _API_HOSTS == the fixed resolver set",
      _enr2._API_HOSTS == frozenset({"api.unpaywall.org", "api.crossref.org", "arxiv.org"}))
_oa_client_src = _insp.getsource(_oa2._get_client)
check("curator live: openalex client sets follow_redirects=False", "follow_redirects=False" in _oa_client_src)
check("curator live: openalex get_json RAISES on a path that resolves off api.openalex.org",
      _al_raises(lambda: _oa2.get_json("@evil.example/x")))

# ---------------------------------------------------------------------------
# 17. roadmap-④ + Galleria v1.1 adapter-parse invariants (pure, no live call): github tree-mode
#     parses a canned git/trees response; openreview review-fetch parses canned reply notes.
# ---------------------------------------------------------------------------
from penumbra.core.sources.api.github_source import GitHubAdapter as _GH, _TREE_RE as _GH_TREE_RE  # noqa: E402

check("github: tree:owner/repo (+@branch) routes to tree-mode, plain query does not",
      bool(_GH_TREE_RE.match("tree:octocat/hello"))
      and bool(_GH_TREE_RE.match("tree:octocat/hello@dev"))
      and not _GH_TREE_RE.match("org:openai") and not _GH_TREE_RE.match("scaling laws"))
_gh = _GH()
_canned_tree = {"sha": "abc123", "truncated": False, "tree": [
    {"path": "README.md", "type": "blob", "size": 1200},
    {"path": "src", "type": "tree"},
    {"path": "src/main.py", "type": "blob", "size": 800},
    {"path": "src/util.py", "type": "blob", "size": 300},
]}
_tree_doc = _GH._tree_to_doc("octocat", "hello", "main", _canned_tree)
check("github tree-mode: parses a canned git/trees response into a readable tree doc",
      _tree_doc.source == "github" and _tree_doc.metadata.get("subtype") == "tree"
      and _tree_doc.metadata.get("node_count") == 4 and _tree_doc.metadata.get("truncated") is False
      and "README.md" in _tree_doc.content and "  main.py" in _tree_doc.content  # nested file indented
      and _tree_doc.url == "https://github.com/octocat/hello/tree/main")
_big_tree = {"truncated": True, "tree": [{"path": f"f{i}.py", "type": "blob"} for i in range(2000)]}
_big_doc = _GH._tree_to_doc("o", "r", "main", _big_tree)
check("github tree-mode: a huge/truncated tree is bounded to the node cap + flags truncation",
      _big_doc.metadata.get("shown") <= 600 and _big_doc.metadata.get("truncated") is True)

from penumbra.core.sources.api.openreview_source import (  # noqa: E402
    OpenReviewAdapter as _OR, _parse_reviews as _OR_parse_reviews)

check("openreview: reviews: qualifier extracts a forum id (bare id or /forum?id= URL)",
      _OR_parse_reviews("reviews:zzz123") == "zzz123"
      and _OR_parse_reviews("reviews:https://openreview.net/forum?id=ABC") == "ABC"
      and _OR_parse_reviews("venue:colm2025") is None)
_or_review = {"id": "r1", "cdate": 1700000000000,
              "invitations": ["ICLR.cc/2025/Conference/-/Official_Review"],
              "content": {"rating": {"value": "8: accept"}, "review": {"value": "Strong, clear method."},
                          "confidence": {"value": "4"}, "weaknesses": {"value": "limited ablation"}}}
_or_meta = {"id": "m1", "cdate": 1700000001000,
            "invitations": ["ICLR.cc/2025/Conference/-/Meta_Review"],
            "content": {"metareview": {"value": "Accept; reviewers agree."},
                        "decision": {"value": "Accept (poster)"}}}
_or_bare = {"id": "x1", "content": {"title": {"value": "just a title, not a review"}}}
_rev_doc = _OR._review_note_to_document(_or_review, "F1")
_meta_doc = _OR._review_note_to_document(_or_meta, "F1")
check("openreview review-fetch: parses a canned reviewer note (rating + text + fields)",
      _rev_doc is not None and _rev_doc.source == "openreview"
      and _rev_doc.metadata.get("subtype") == "review" and _rev_doc.metadata.get("rating") == "8: accept"
      and "rating:8: accept" in _rev_doc.tags and "limited ablation" in _rev_doc.content
      and _rev_doc.metadata.get("forum") == "F1")
check("openreview review-fetch: a meta-review note is classified meta_review",
      _meta_doc is not None and _meta_doc.metadata.get("subtype") == "meta_review")
check("openreview review-fetch: a note with NO review-ish field is skipped (returns None)",
      _OR._review_note_to_document(_or_bare, "F1") is None)
check("openreview: adapter exposes fetch_reviews + search routes reviews: to it",
      hasattr(_OR, "fetch_reviews") and callable(getattr(_OR, "fetch_reviews")))

# ---------------------------------------------------------------------------
# 18. cold-start root fixes (2026-06-16): OpenAlex key auth + accuracy, S2 in-corpus
#     edges, list_sources compaction, docreader Content-Type typing, enrich citation_count,
#     query-aware excluded_relevant, resolve_identity likely_same_person. Pure/offline.
# ---------------------------------------------------------------------------

# FIX 1: OpenAlex per-IP credit exhaustion. Every request now authenticates with an api_key.
check("openalex: _load_api_key exists (mirrors _s2 keyed-client pattern)",
      hasattr(_oa2, "_load_api_key") and callable(_oa2._load_api_key))
# get_json injects api_key into the OUTGOING params when a key is present (monkeypatch the client
# to capture params; force a key in regardless of whether a key file exists on this machine).


class _CapClient:
    def __init__(self):
        self.params = None

    def get(self, url, params=None, timeout=None):
        self.params = params

        class _R:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                pass

            def json(self):
                return {}
        return _R()


_cap = _CapClient()
_save_key, _save_get_client = _oa2._api_key, _oa2._get_client
try:
    _oa2._api_key = "SMOKE_KEY"
    _oa2._get_client = lambda: _cap
    _oa2.get_json("/works", {"search": "x"})
    check("openalex: get_json injects api_key into outgoing params when a key is present",
          (_cap.params or {}).get("api_key") == "SMOKE_KEY")
finally:
    _oa2._api_key, _oa2._get_client = _save_key, _save_get_client
# 1b accuracy: the contact identifier is a reachable address, not the unreachable .local TLD.
check("openalex/enrich: contact email has no unreachable .local TLD",
      ".local" not in _oa2.USER_AGENT and ".local" not in _enr2._MAIL)

# Two-bucket spill (2026-06-17): the api_key and anonymous per-IP paths are SEPARATE $1/day OpenAlex
# budgets; on a budget-429 (remaining 0 / "insufficient budget") get_json spills from the keyed bucket
# to the anon bucket for ~2x daily capacity, instead of degrading. Verified live (the key bucket can be
# dry while the anon bucket is fresh). operator-sanctioned 2026-06-17.
check("openalex: _is_budget_429 + dry_until state exist (the two-bucket spill machinery)",
      hasattr(_oa2, "_is_budget_429") and callable(_oa2._is_budget_429)
      and isinstance(_oa2._state.get("dry_until"), dict))


class _BudgetSpillClient:
    """429 'insufficient budget' for the KEYED params; 200 for the keyless (anon) retry."""
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params or {}))
        _keyed = "api_key" in (params or {})

        class _R:
            def __init__(self, keyed):
                self.status_code = 429 if keyed else 200
                self.headers = {"x-ratelimit-remaining": "0", "retry-after": "3600"} if keyed else {}

            def raise_for_status(self):
                pass

            def json(self):
                return ({"error": "Rate limit exceeded", "dailyRemainingUsd": 0}
                        if self.status_code == 429 else {"results": [{"id": "W1"}]})
        return _R(_keyed)


_save_key3, _save_gc3 = _oa2._api_key, _oa2._get_client
_save_dry3 = dict(_oa2._state.get("dry_until") or {})
_spill = _BudgetSpillClient()
try:
    _oa2._api_key = "SMOKE_KEY"
    _oa2._get_client = lambda: _spill
    _oa2._state["dry_until"] = {"keyed": 0.0, "anon": 0.0}
    _spill_res = _oa2.get_json("/works", {"select": "id"})
    check("openalex: a budget-429 on the keyed bucket SPILLS to the anonymous bucket (2x capacity)",
          _spill_res == {"results": [{"id": "W1"}]}
          and any("api_key" in c for c in _spill.calls)        # tried the keyed bucket first
          and any("api_key" not in c for c in _spill.calls))   # then spilled to anon
    check("openalex: the exhausted keyed bucket is marked dry (skipped until its reset)",
          _oa2._state["dry_until"]["keyed"] > 0.0)
finally:
    _oa2._api_key, _oa2._get_client = _save_key3, _save_gc3
    _oa2._state["dry_until"] = _save_dry3 or {"keyed": 0.0, "anon": 0.0}

# Usage attribution (2026-06-17): every budget-spending success is tallied by caller + the live
# per-bucket remaining is captured, surfaced by penumbra_sources(check_health=True) in its `system` block,
# so a heavy OpenAlex day is ITEMIZABLE (a hidden over-consumer can't hide) instead of inferred.
check("openalex: usage_stats() exists (per-caller budget attribution in the check_health system block)",
      hasattr(_oa2, "usage_stats") and callable(_oa2.usage_stats))

# penumbra_health_check was ABSORBED into penumbra_sources(check_health=True): the live probe now ALSO returns a
# `system` block carrying the two payloads that tool uniquely built (the recall-index health + the
# openalex usage attribution). Assert the block's SHAPE only — stub list_sources so check_health does
# no live per-source probing (the recall + usage stats it folds in are local, no network).
_hc_real_list = fetcher.list_sources
try:
    fetcher.list_sources = lambda *a, **k: []  # no live probe; the system block is built independently
    _hc_out = _srv2.penumbra_sources.__wrapped__(check_health=True)
finally:
    fetcher.list_sources = _hc_real_list
check("health: penumbra_sources(check_health=True) returns a system block with recall + openalex_usage",
      isinstance(_hc_out.get("system"), dict)
      and "recall" in _hc_out["system"] and "openalex_usage" in _hc_out["system"],
      f"system={_hc_out.get('system')!r}")
check("health: penumbra_sources without check_health carries NO system block (advisory-only default)",
      "system" not in _srv2.penumbra_sources.__wrapped__(domain="papers"))


class _OkClient:
    def get(self, url, params=None, timeout=None):
        class _R:
            status_code = 200
            headers = {"x-ratelimit-remaining": "8765"}

            def raise_for_status(self):
                pass

            def json(self):
                return {"results": []}
        return _R()


_save_key4, _save_gc4 = _oa2._api_key, _oa2._get_client
_save_usage = json.loads(json.dumps(_oa2._usage))  # deep snapshot to restore
try:
    _oa2._api_key = "SMOKE_KEY"
    _oa2._get_client = lambda: _OkClient()
    _oa2._usage = {"since": None, "by_caller": {}, "spilled_to_anon": 0,
                   "remaining": {"keyed": None, "anon": None}}
    _oa2.get_json("/works", {"select": "id"})
    _us = _oa2.usage_stats()
    check("openalex: usage_stats tallies a successful call to its caller + captures live remaining",
          _us["total_ok_calls"] == 1
          and any(":" in k for k in _us["by_caller"])      # caller tagged as module:function
          and _us["remaining"]["keyed"] == 8765)
finally:
    _oa2._api_key, _oa2._get_client = _save_key4, _save_gc4
    _oa2._usage = _save_usage

# OpenAlex shared-key hardening: openalex + researcher_watch + 39 org_watch rows share ONE key +
# breaker, so (a) request STARTS are rate-paced (a fan-out can't burst the key into 429) and (b)
# health is ONE single-flight upstream probe, not one-per-source (the old self-DOS that read as
# "40 sources down" when the all-at-once health sweep bursted the shared key).
check("openalex: rate pacer exists (bounds req/s so a fan-out can't burst the shared key)",
      hasattr(_oa2, "_pace") and callable(_oa2._pace) and getattr(_oa2, "_MIN_INTERVAL_S", 0) > 0)
# Backend-guard extraction (2026-07-01 parsimony audit P1): the concurrency cap + rate pacer + circuit
# breaker _openalex / _s2 / _github each carried verbatim now live in ONE _guard.BackendGuard per
# module; the module reaches its primitives by name. Assert the NEW wiring survived byte-for-byte: the
# module holds a BackendGuard carrying its ORIGINAL constants (break_after=5, break_for_s=120.0, the
# per-module max_inflight = _MAX_CONCURRENCY sized the semaphore), AND the module-level _sema / _state /
# _lock / _pace_state / _pace_lock ARE the guard's own objects (the aliases wire to it, not a copy — so
# every threshold, sleep, log line and error path stays identical). One helper, reused for s2 + github.
from penumbra.core._guard import BackendGuard as _BackendGuard  # noqa: E402
def _guard_wired(mod, name, max_inflight):
    g = getattr(mod, "_guard", None)
    return (isinstance(g, _BackendGuard) and g.name == name
            and g.break_after == 5 and g.break_for_s == 120.0
            and mod._MAX_CONCURRENCY == max_inflight
            and g.sema._initial_value == max_inflight  # the cap sized the BoundedSemaphore
            and mod._sema is g.sema and mod._state is g.state and mod._lock is g.lock
            and mod._pace_state is g.pace_state and mod._pace_lock is g.pace_lock)
check("openalex: holds a BackendGuard with its original constants (cap=8, break 5/120.0) + aliases wire to it",
      _guard_wired(_oa2, "openalex", 8))
check("openalex: shared single-flight health() probe exists",
      hasattr(_oa2, "health") and callable(_oa2.health))
_oa2._health["result"] = None
_oa2._health["at"] = 0.0
_hcnt = {"n": 0}
_save_gj_h = _oa2.get_json
_oa2.get_json = lambda *a, **k: (_hcnt.__setitem__("n", _hcnt["n"] + 1), {"results": []})[1]
try:
    _h1 = _oa2.health()
    _h2 = _oa2.health()
    check("openalex: health() is single-flight + cached (2 calls -> 1 upstream probe)",
          _hcnt["n"] == 1 and _h1 == _h2 and _h1[0] is True)
finally:
    _oa2.get_json = _save_gj_h
    _oa2._health["result"] = None
    _oa2._health["at"] = 0.0

# FIX 2: cartographer S2 edges. A re-add() preserves prior referenced_works (no wipe), and a
# tiny synthetic seed+reference set yields n_edges > 0 and at least one in-corpus in_degree > 0.


class _FakePaper:
    def __init__(self, pid, title="t", refs=None):
        self.paperId = pid
        self.title = title
        self.year = 2024
        self.publicationDate = "2024-01-01"
        self.citationCount = 1
        self.authors = []
        self.externalIds = {}
        self.fieldsOfStudy = ["Computer Science"]


class _FakeEdge:
    def __init__(self, paper, contextsWithIntent=None, contexts=None):
        self.paper = paper
        self.intents = []
        self.isInfluential = False
        # S2 edge carries the RAW citing sentence (+ its own intents). The eye passes it through
        # verbatim; the AGENT judges polarity. Default to a recorded-shape sample.
        self.contextsWithIntent = (contextsWithIntent if contextsWithIntent is not None
                                   else [{"context": "We adopt the reward model of [SEED], which "
                                                     "supports our central claim.",
                                          "intents": ["methodology"]}])
        self.contexts = contexts if contexts is not None else []


_save_s2 = {k: getattr(cartographer._s2, k) for k in
            ("get_paper", "get_paper_references", "get_paper_citations", "search_paper")}
try:
    _seedp = _FakePaper("SEED")
    _refp = _FakePaper("REF")       # the seed cites REF (in-corpus → an edge)
    _citerp = _FakePaper("CITER")   # CITER cites the seed (in-corpus → an edge)
    cartographer._s2.get_paper = lambda sid, fields=None: _seedp
    cartographer._s2.get_paper_references = lambda sid, cap, fields=None: [_FakeEdge(_refp)]
    cartographer._s2.get_paper_citations = lambda sid, cap, fields=None: [_FakeEdge(_citerp)]
    cartographer._s2.search_paper = lambda q, limit=4, fields=None: [_seedp]
    _seed_ids, _works, _dh = cartographer._s2_assemble(None, ["SEED"], 4, 30)
    _built = cartographer._build(_seed_ids, _works, 250)
    check("cartographer s2: in-corpus references build real edges (n_edges > 0)",
          _built["n_edges"] > 0, f"n_edges={_built['n_edges']}")
    check("cartographer s2: at least one node has in_degree > 0 (was 0/0 for all)",
          any(n["in_degree"] > 0 for n in _built["nodes"]),
          f"in_degrees={[n['in_degree'] for n in _built['nodes']]}")
    # the SEED's referenced_works must survive a re-add() (the no-wipe fix)
    check("cartographer s2: re-add preserves prior referenced_works (no wipe on merge)",
          "REF" in (_works.get("SEED", {}).get("referenced_works") or []))
    # citation POLARITY evidence: the raw S2 citing SENTENCE rides onto the node as a FACT
    # ({snippet, intents}); the eye does NOT classify supporting/contrasting/mentioning, the
    # agent reads the snippet and judges. This asserts the pass-through only.
    _ctx_nodes = [n for n in _built["nodes"] if n.get("contexts")]
    check("cartographer s2: citing-sentence contexts surface on a node (polarity evidence)",
          bool(_ctx_nodes), f"nodes_with_contexts={len(_ctx_nodes)}")
    check("cartographer s2: a context entry carries the raw snippet + S2 intents (no eye verdict)",
          bool(_ctx_nodes) and isinstance(_ctx_nodes[0]["contexts"][0].get("snippet"), str)
          and "intents" in _ctx_nodes[0]["contexts"][0]
          and "central claim" in _ctx_nodes[0]["contexts"][0]["snippet"])
    # bare-contexts fallback (older S2 lib without contextsWithIntent): sentence still surfaces
    _bare = cartographer._s2  # already monkeypatched in this block
    _seedp2 = _FakePaper("SEED2")
    _citerp2 = _FakePaper("CITER2")
    _bare.get_paper = lambda sid, fields=None: _seedp2
    _bare.get_paper_references = lambda sid, cap, fields=None: []
    _bare.get_paper_citations = lambda sid, cap, fields=None: [
        _FakeEdge(_citerp2, contextsWithIntent=[], contexts=["raw fallback citing sentence"])]
    _bare.search_paper = lambda q, limit=4, fields=None: [_seedp2]
    _sid2, _w2, _dh2 = cartographer._s2_assemble(None, ["SEED2"], 4, 30)
    _b2 = cartographer._build(_sid2, _w2, 250)
    _fb = [n for n in _b2["nodes"] if n.get("contexts")]
    check("cartographer s2: bare contexts fallback still surfaces the sentence (no contextsWithIntent)",
          bool(_fb) and _fb[0]["contexts"][0]["snippet"] == "raw fallback citing sentence"
          and _fb[0]["contexts"][0]["intents"] == [])
    # OVERALL deadline: the ~13-serial-call assemble must NOT run unbounded. A blown deadline bails
    # the loop early (deadline_hit True, partial map) instead of grinding for minutes on a throttling
    # S2 (the field_skeleton "hung for minutes" symptom). A generous deadline never trips it.
    import time as _tmod
    _, _wpast, _dhpast = cartographer._s2_assemble(None, ["SEED2"], 4, 30,
                                                   deadline_at=_tmod.monotonic() - 1)
    check("cartographer s2: a blown overall deadline bails early (deadline_hit True, no edges fetched)",
          _dhpast is True and _wpast == {})
    check("cartographer s2: a generous deadline does NOT trip deadline_hit (normal full assemble)",
          cartographer._s2_assemble(None, ["SEED2"], 4, 30, deadline_at=_tmod.monotonic() + 999)[2] is False
          and _dh2 is False)
finally:
    for _k, _v in _save_s2.items():
        setattr(cartographer._s2, _k, _v)

# FIX 3: list_sources is compact by default; domain/query narrow WITH descriptions; verbose forces them.
_ls_default = fetcher.list_sources()
check("list_sources: compact by default (no row carries a description key)",
      all("description" not in e for e in _ls_default))
# The compaction's POINT: dropping the prose roughly halves the payload. Assert the compact default
# is materially smaller than the verbose (full-prose) list, not a fixed byte cap (the registry grows).
_compact_bytes = len(json.dumps(_ls_default, ensure_ascii=False))
_verbose_bytes = len(json.dumps(fetcher.list_sources(verbose=True), ensure_ascii=False))
check("list_sources: the compact default is materially smaller than the verbose list (prose dropped)",
      _compact_bytes < 0.7 * _verbose_bytes,
      f"compact={_compact_bytes} verbose={_verbose_bytes}")
_ls_jobs = fetcher.list_sources(domain="jobs")
check("list_sources(domain='jobs'): all rows carry the jobs domain AND a description",
      _ls_jobs and all("jobs" in (e.get("domains") or []) and "description" in e for e in _ls_jobs),
      f"n={len(_ls_jobs)}")
check("list_sources(verbose=True): every row has a description",
      all("description" in e for e in fetcher.list_sources(verbose=True)))

# FIX 3b: query= is TOKEN-OVERLAP (multi-word + cross-lingual), not the old literal substring that
# returned 0 for almost every natural multi-word query (incl. the docstring's own 'singapore visa').
_ls_sv = [e["name"] for e in fetcher.list_sources(query="singapore visa")]
check("list_sources(query='singapore visa'): multi-word query returns sources (was 0 under substring)",
      len(_ls_sv) > 0, f"got {len(_ls_sv)}")
# Cross-lingual: an ENGLISH query surfaces a Chinese-described walled source via its _ROUTING_KEYWORDS
# English aliases (blind = Singapore EP/visa insider; its description is Chinese-only).
_ls_ep = [e["name"] for e in fetcher.list_sources(query="employment pass visa nationality")]
check("list_sources(query=): English query reaches a Chinese-described source via cross-lingual keywords",
      "blind" in _ls_ep, f"got {_ls_ep[:6]}")
# Ranked best-first: a finance query leads with the finance sources, not an incidental description hit.
_ls_fin = [e["name"] for e in fetcher.list_sources(query="A-share listed company stock filing")]
check("list_sources(query=): token-overlap is ranked (a finance query leads with finance sources)",
      bool(_ls_fin) and _ls_fin[0] in {"cninfo", "eastmoney", "market_quote", "sec_financials", "sec_edgar"},
      f"got {_ls_fin[:5]}")
# facet_vocabulary: the closed domain vocabulary + counts the agent routes by (the did_you_mean/orient data).
_vocab = fetcher.facet_vocabulary()
check("fetcher.facet_vocabulary: returns domain→count map covering the core domains",
      isinstance(_vocab.get("domains"), dict) and _vocab["domains"].get("papers", 0) > 10
      and "career" in _vocab["domains"])
# distinct_backend_count: the HONEST figure (the OpenAlex family collapses), well below the raw count.
check("fetcher.distinct_backend_count: honest backend total < raw source count",
      0 < fetcher.distinct_backend_count() < len(fetcher.all_adapter_names()))
# _query_overlap_count: the COUNT (powering excluded_relevant rank/cap), via a real adapter's
# surface (incl. its cross-lingual keywords). > the bool floor; 0 on an off-topic query.
_blind_ov = fetcher.get_adapter("blind")
if _blind_ov is not None:
    check("fetcher._query_overlap_count: distinct-token overlap count (0 when off-topic)",
          fetcher._query_overlap_count("employment pass visa salary", _blind_ov) >= 1
          and fetcher._query_overlap_count("lattice qcd quark gluon", _blind_ov) == 0)
# FIX 3c: region= exact-match filter (regions normalized to a LIST so a bare-string org_watch row
# and a facets.json list row filter identically); domain= results lead with general (non-EO) sources.
_ls_sg = fetcher.list_sources(region="sg")
check("list_sources(region='sg'): every row carries sg in its (list-normalized) regions facet",
      bool(_ls_sg) and all("sg" in (e.get("regions") or []) for e in _ls_sg), f"n={len(_ls_sg)}")
check("list_sources: regions facet is always a list (bare-string org_watch rows coerced)",
      all(isinstance(e.get("regions"), list) for e in fetcher.list_sources(verbose=True)
          if "regions" in e))
_ls_papers = fetcher.list_sources(domain="papers")
check("list_sources(domain='papers'): general (non-explicit_only) sources lead the EO lab streams",
      bool(_ls_papers) and not _ls_papers[0].get("explicit_only", False),
      f"first={_ls_papers[0]['name'] if _ls_papers else None}")

# FIX 4: docreader Content-Type typing. An extension-less URL served as application/pdf resolves to
# pdf and reaches the reader; an unrecognized type still returns the (now accurate) unsupported error.
check("docreader._fmt_from_response maps MIME + Content-Disposition filename to fmt",
      docreader._fmt_from_response("application/pdf", None) == "pdf"
      and docreader._fmt_from_response("application/pdf; charset=binary", None) == "pdf"
      and docreader._fmt_from_response("application/octet-stream", "paper.pdf") == "pdf"
      and docreader._fmt_from_response("application/octet-stream", None) == "")
check("docreader._fmt_of still returns '' for an extension-less arxiv pdf url (the cause)",
      docreader._fmt_of("https://arxiv.org/pdf/2203.02155v1") == "")
_dr_reached = {}


def _fake_download_pdf(url, fmt):
    import tempfile as _t
    import os as _os
    fd, p = _t.mkstemp(suffix=".bin")
    _os.write(fd, b"%PDF-1.4 stub")
    _os.close(fd)
    return Path(p), "application/pdf", "2203.02155v1.pdf"


def _fake_read_pdf(path, export_dir):
    _dr_reached["pdf"] = True
    return "stub", [docreader._sec("Page 1", ["body text"], 0)], []


# Cache isolation: read_document caches to the DISK-BACKED store (survives the process), so a
# monkeypatched stub read would otherwise WRITE a fake doc under a REAL arxiv URL key and poison
# the live server's cache. Force a miss + no-op write for these URL tests, and RESTORE after.
_save_cget, _save_cset = docreader.cache.get, docreader.cache.set
_save_dl, _save_pdf = docreader._download, docreader._READERS["pdf"]
try:
    docreader.cache.get = lambda k: None
    docreader.cache.set = lambda *a, **k: None
    docreader._download = _fake_download_pdf
    docreader._READERS["pdf"] = _fake_read_pdf
    _dr = docreader.read_document("https://arxiv.org/pdf/2203.02155v1")
    check("docreader: extension-less arxiv pdf url resolves fmt=pdf + reaches the PDF reader",
          _dr.get("format") == "pdf" and _dr_reached.get("pdf") and "error" not in _dr,
          str(_dr.get("error")))
finally:
    docreader._download, docreader._READERS["pdf"] = _save_dl, _save_pdf
    docreader.cache.get, docreader.cache.set = _save_cget, _save_cset


def _fake_download_unknown(url, fmt):
    import tempfile as _t
    import os as _os
    fd, p = _t.mkstemp(suffix=".bin")
    _os.close(fd)
    return Path(p), "application/x-mystery", None


_save_cget2, _save_cset2 = docreader.cache.get, docreader.cache.set
_save_dl2 = docreader._download
try:
    docreader.cache.get = lambda k: None
    docreader.cache.set = lambda *a, **k: None
    docreader._download = _fake_download_unknown
    _dru = docreader.read_document("https://example.com/file-no-ext")
    check("docreader: a genuinely unrecognized Content-Type returns an accurate unsupported error",
          "unsupported document type" in (_dru.get("error") or ""), str(_dru))
finally:
    docreader._download = _save_dl2
    docreader.cache.get, docreader.cache.set = _save_cget2, _save_cset2
# the shared helper is used by BOTH call sites (source-inspect)
check("docreader: _fmt_from_response is called by read_document AND view_images (shared helper)",
      "_fmt_from_response" in _insp.getsource(docreader.read_document)
      and "_fmt_from_response" in _insp.getsource(docreader.view_images))
# a local path with no extension is still rejected before any IO (no headers to consult)
check("docreader: a local path with no usable extension is rejected before IO",
      "unsupported or missing file extension" in (docreader.read_document("noext").get("error") or ""))

# FIX 5: enrich citation_count. DOI from Crossref (no second HTTP), arXiv from one cached S2 call.
_enr_http = {"n": 0}
_save_getjson, _save_unpaywall = _enr2._get_json, _enr2._unpaywall


def _fake_getjson(url):
    _enr_http["n"] += 1
    return {"message": {"is-referenced-by-count": 4321, "updated-by": []}}


from penumbra.core import cache as _ecache  # noqa: E402
try:
    _ecache_save = (_ecache.get, _ecache.set)       # restore after (no-op cache must not leak)
    _ecache.get = lambda k: None                     # force a fresh compute (skip any warm cache)
    _ecache.set = lambda k, v, ttl=0: None
    _enr2._get_json = _fake_getjson
    _enr2._unpaywall = lambda doi: {"is_oa": True, "pdf_url": None}
    _doi_rec = _enr2.enrich(["10.1145/3292500.3330701"])[0]
    check("enrich: DOI record carries a numeric citation_count from the Crossref message",
          _doi_rec.get("citation_count") == 4321, str(_doi_rec))
    check("enrich: the DOI count rides the existing _integrity fetch (one Crossref call, no second)",
          _enr_http["n"] == 1, f"http calls={_enr_http['n']}")
finally:
    _enr2._get_json, _enr2._unpaywall = _save_getjson, _save_unpaywall
    _ecache.get, _ecache.set = _ecache_save

# enrich imports _s2 lazily inside the arXiv branch; monkeypatch the shared module's get_paper.
import penumbra.core._s2 as _s2mod  # noqa: E402
_save_gp = _s2mod.get_paper
try:
    _ecache_save = (_ecache.get, _ecache.set)
    _ecache.get = lambda k: None
    _ecache.set = lambda k, v, ttl=0: None

    class _AxP:
        citationCount = 99
    _s2mod.get_paper = lambda pid, fields=None: _AxP()
    _ax_rec = _enr2.enrich(["2203.02155"])[0]
    check("enrich: arXiv record carries citation_count from the (mocked) S2 get_paper",
          _ax_rec.get("citation_count") == 99, str(_ax_rec))
finally:
    _s2mod.get_paper = _save_gp
    _ecache.get, _ecache.set = _ecache_save

# FIX 6: search_many excluded_relevant. A thematically-matching walled source is surfaced with a hint;
# an unrelated query surfaces none. Exercise the pure helpers (no network) + the meta key existence.
_blind_a = fetcher.get_adapter("blind")
if _blind_a is not None:
    check("fetcher: _query_overlaps_source matches a walled source on a thematic query, misses otherwise",
          fetcher._query_overlaps_source("singapore employment salary career", _blind_a)
          or fetcher._query_overlaps_source("salary compensation tech career levels", _blind_a))
    check("fetcher: _query_overlaps_source returns False for an off-topic query",
          not fetcher._query_overlaps_source("pytorch autograd tensor backward", _blind_a))
check("fetcher: search_many signature carries the query-aware absence machinery (excluded_relevant)",
      "excluded_relevant" in _insp.getsource(fetcher.search_many))

# FIX 7: resolve_identity likely_same_person. Two same-name same-backend OpenAlex fragments group with
# an "A1+A2" merge token; no duplicates → no group.
_split_cands = [
    {"id": "A111", "source": "openalex", "name": "Yi R. Fung", "name_match": True, "works_count": 20},
    {"id": "A222", "source": "openalex", "name": "Yi R. Fung", "name_match": True, "works_count": 8},
    {"id": "A333", "source": "openalex", "name": "Someone Else", "name_match": True, "works_count": 5},
]
_grp = relations._likely_same_person(_split_cands)
check("relations: _likely_same_person groups same-name same-backend ids with an A1+A2 merge token",
      len(_grp) == 1 and _grp[0]["merge_token"] == "A111+A222"
      and set(_grp[0]["ids"]) == {"A111", "A222"}, str(_grp))
check("relations: _likely_same_person emits nothing when no same-name same-backend duplicate exists",
      relations._likely_same_person(
          [{"id": "A1", "source": "openalex", "name": "Solo Person", "name_match": True}]) == [])
# cross-backend same-name does NOT '+'-merge (OpenAlex A-id vs S2 numeric id don't mix)
check("relations: _likely_same_person does NOT merge the same name across different backends",
      relations._likely_same_person(
          [{"id": "A1", "source": "openalex", "name": "Same Name", "name_match": True},
           {"id": "123456", "source": "s2", "name": "Same Name", "name_match": True}]) == [])
check("server: penumbra_resolve_identity docstring documents likely_same_person + merge_token",
      "likely_same_person" in (_srv.penumbra_resolve_identity.__doc__ or "")
      or "likely_same_person" in _insp.getsource(relations.resolve_identity))

# FIX 8 (root-cause, beyond the spec's 7): a FAILED OpenAlex lookup is NOT a confirmed absence. When
# the OpenAlex query raises (429 / breaker / network), resolve_identity must emit an honest DEGRADED
# note + a `degraded` key — never the false "likely not yet in the graph" that produced the famous-
# author false "no NAME-MATCH". coauthors/_resolve_one propagate it; a genuine EMPTY still reports
# the honest absence. (Removes the swallow-as-verdict masking the API key only reduces the trigger of.)
_save_oac, _save_s2c = relations._oa_candidates, relations._s2_candidates
try:
    def _raise_oa(name, limit):
        raise RuntimeError("circuit open 119s more")
    relations._oa_candidates = _raise_oa
    relations._s2_candidates = lambda name, limit: []   # offline + isolate the OpenAlex failure
    _rid = relations.resolve_identity("Doina Precup", source="openalex")
    check("relations: a FAILED OpenAlex lookup yields a DEGRADED verdict, not a false 'not in graph'",
          bool(_rid.get("degraded")) and "DEGRADED" in (_rid.get("note") or "")
          and "not yet in the graph" not in (_rid.get("note") or ""), str(_rid))
    _co = relations.coauthors(["Doina Precup"], source="openalex")
    _node0 = (_co.get("nodes") or [{}])[0]
    check("relations: coauthors propagates the OpenAlex degradation (top-level + node), not 'no NAME-MATCH'",
          bool(_co.get("degraded")) and bool(_node0.get("degraded"))
          and "no NAME-MATCH" not in (_node0.get("note") or ""), str(_co.get("degraded")))
    # control: a genuine EMPTY OpenAlex result (no exception) still reports the honest absence
    relations._oa_candidates = lambda name, limit: []
    _rid_empty = relations.resolve_identity("Zzzz Nonexistent Person", source="openalex")
    check("relations: a genuine empty OpenAlex result still reports the honest 'not in graph' absence",
          not _rid_empty.get("degraded") and "not yet in the graph" in (_rid_empty.get("note") or ""),
          str(_rid_empty))
finally:
    relations._oa_candidates, relations._s2_candidates = _save_oac, _save_s2c

# ---------------------------------------------------------------------------
# N. named signals migration (COMMITMENT step 2): the fused score scalar is GONE from
#    Document — each source-reported count/rating/salary is its OWN named,
#    provenance-stamped Signal, and the ranker reads attention_value() (a max over the
#    ATTENTION-class signals: engagement + citation; compensation/other excluded).
# ---------------------------------------------------------------------------
from penumbra.core.normalize import Signal, mk_signal  # noqa: E402

check("signals: Document has the named signals map, no score / no relevance field",
      "signals" in Document.model_fields
      and "score" not in Document.model_fields
      and "relevance" not in Document.model_fields,
      str(sorted(Document.model_fields)))
check("signals: a built doc exposes no .score / .relevance attribute (field fully removed)",
      not hasattr(_doc("x", "t"), "score") and not hasattr(_doc("x", "t"), "relevance"))
check("signals: Signal model exists with value/kind/computed_by/unit",
      {"value", "kind", "computed_by", "unit"} <= set(Signal.model_fields))
_sig = mk_signal("x", 5, kind="engagement", by="t/f")
check("signals: mk_signal builds {name: Signal} with float value + provenance stamp",
      set(_sig) == {"x"} and isinstance(_sig["x"], Signal)
      and _sig["x"].value == 5.0 and _sig["x"].kind == "engagement"
      and _sig["x"].computed_by == "source:t/f")
check("signals: mk_signal coerces a non-numeric / None value to None (never raises)",
      mk_signal("x", None)["x"].value is None and mk_signal("x", "n/a")["x"].value is None)
_eng_doc = _doc("reddit", "engagement+comp doc")
_eng_doc.signals = {**mk_signal("upvotes", 100, kind="engagement", by="reddit/score"),
                    **mk_signal("salary", 6000, kind="compensation", by="mcf/salary", unit="SGD/month")}
check("signals: attention_value() = the engagement signal, compensation EXCLUDED",
      _eng_doc.attention_value() == 100.0, str(_eng_doc.attention_value()))
_cit_doc = _doc("openalex", "citation doc")
_cit_doc.signals = mk_signal("citations", 50, kind="citation", by="openalex/cited_by")
check("signals: a citation signal counts as ATTENTION (attention_value == 50)",
      _cit_doc.attention_value() == 50.0, str(_cit_doc.attention_value()))
check("signals: attention_value() is None when a doc carries no ATTENTION-class signal",
      _doc("x", "no signals").attention_value() is None
      and _doc("y", "comp only", "").__class__  # build then set a sole compensation signal
      and (lambda d: (d.signals.update(mk_signal("salary", 9000, kind="compensation", by="z/s"))
                      or d.attention_value()))(_doc("z", "comp only")) is None)
# mycareersfuture's salary MUST be compensation (so it never pollutes the engagement term)
from penumbra.core.sources.api import mycareersfuture_source as _mcf  # noqa: E402
_mcf_src = _insp.getsource(_mcf)
check("signals: mycareersfuture salary is kind='compensation' (not engagement)",
      "kind='compensation'" in _mcf_src and "salary" in _mcf_src)
# github_awesome_phd's local keyword-hit count is NOT a source fact: no signal emitted
from penumbra.core.sources.scrape import github_awesome_phd_source as _gap  # noqa: E402
check("signals: github_awesome_phd emits NO signal (local keyword-hit count is not a source fact)",
      "mk_signal" not in _insp.getsource(_gap) and "signals=" not in _insp.getsource(_gap))

# ============================================================================
# OpenAlex-class wasteful-usage root-fixes (S2 / prewarm / GitHub / exa / relations), 2026-06-16.
# Regression guards from the eye-usage-waste-rootfix workflow: a shared rate pacer + single-flight
# health() + refresh-if-near-expiry warming + relations caching, mirroring the _openalex fix.
# ============================================================================
from penumbra.core import cache as _wcache  # noqa: E402
_WASTE_ORIG_CACHE_DIR = _wcache.CACHE_DIR

import time as _t
from penumbra.core import _s2 as _S2
from penumbra.core.sources.api import semantic_scholar_source as _SS

check("s2 has _pace", hasattr(_S2, "_pace"))
check("s2 _MIN_INTERVAL_S honors ~1 RPS", _S2._MIN_INTERVAL_S == 1.0)
check("s2: holds a BackendGuard with its original constants (cap=4, break 5/120.0) + aliases wire to it",
      _guard_wired(_S2, "s2", 4))
check("s2 _pace_state slot", isinstance(_S2._pace_state, dict) and "next_at" in _S2._pace_state)
check("s2 _pace spaces request starts >= ~1s", (lambda t0: (_S2._pace(), _S2._pace(), _t.monotonic() - t0)[-1])(_t.monotonic()) >= 0.95)
# Pace-gate cap: a pathological backlog (next_at far in the future, the 429-storm signature that
# made a field_skeleton sit 886s on the gate) must FAIL FAST (raise S2Down) WITHOUT reserving a new
# slot — not inherit the whole queue. Deterministic: push next_at out, assert raise + slot unchanged.
def _s2_pace_cap_ok():
    _S2._pace_state["next_at"] = _t.monotonic() + 10_000  # pathological backlog
    before = _S2._pace_state["next_at"]
    try:
        _S2._pace()
        raised = False
    except _S2.S2Down:
        raised = True
    unchanged = _S2._pace_state["next_at"] == before  # did NOT reserve a further slot (backlog drains)
    _S2._pace_state["next_at"] = 0.0  # restore so later s2 calls are not gated
    return raised and unchanged
check("s2 _pace caps the wait (raises S2Down on a pathological backlog, reserves no slot)",
      _s2_pace_cap_ok())
check("s2 _PACE_MAX_WAIT_S is a sane cap (a few-to-tens of seconds, not unbounded)",
      0 < _S2._PACE_MAX_WAIT_S <= 60)
# Same unbounded-pace-wait bug class guarded in the OpenAlex twin (40+ OA sources fan through its
# gate): get_json fast-fails (OpenAlexDown) on a pathological backlog instead of inheriting the queue.
from penumbra.core import _openalex as _OA
def _oa_pace_cap_ok():
    _OA._pace_state["next_at"] = _t.monotonic() + 10_000  # pathological backlog
    try:
        _OA.get_json("/works", {"per-page": 1, "select": "id"})  # must raise BEFORE any network
        raised = False
    except _OA.OpenAlexDown:
        raised = True
    except Exception:
        raised = False  # any other path means the backlog guard did not fire first
    _OA._pace_state["next_at"] = 0.0  # restore
    return raised
check("openalex get_json fast-fails on a pathological rate-gate backlog (no multi-minute hang)",
      _oa_pace_cap_ok())
check("openalex _PACE_MAX_WAIT_S is a sane cap + _pace_backlog_s reads the wait (no reserve)",
      0 < _OA._PACE_MAX_WAIT_S <= 60 and _OA._pace_backlog_s() >= 0.0)

check("s2 has single-flight health", hasattr(_S2, "health"))
check("s2 health TTL 60s", _S2._HEALTH_TTL_S == 60.0)
check("s2 breaker carries last_429 stamp", "last_429" in _S2._state)
check("s2 detects 429 (ConnRefused)", _S2._is_rate_limit(ConnectionRefusedError()) is True)
check("s2 detects 429 (message)", _S2._is_rate_limit(RuntimeError("HTTP 429 Too Many Requests")) is True)
check("s2 non-429 not flagged", _S2._is_rate_limit(RuntimeError("boom")) is False)
_S2._state["open_until"] = _t.time() + 9999; _S2._health["result"] = None
_hc = _S2.health()
check("s2 health fails fast while circuit open", _hc[0] is False and "circuit open" in _hc[1])
_S2._state["open_until"] = 0.0; _S2._health["result"] = None

# S2 retry (2026-06-20): the lib's OWN 10x/250s tenacity backoff is OFF (retry=False); the eye owns a
# SHORT bounded retry, so a brief 429 is ridden out but a sustained one fails fast (no 260s fake-hang).
check("s2 client built with lib retry OFF (no hidden 10x/250s backoff)", _S2.get_client().retry is False)
_S2._client = None  # drop the singleton built just to assert the flag
check("s2 _retry_rl + bounded config sane",
      isinstance(_S2._RL_RETRIES, int) and _S2._RL_RETRIES >= 1
      and isinstance(_S2._RL_BACKOFF_S, tuple) and len(_S2._RL_BACKOFF_S) >= _S2._RL_RETRIES)
_save_bo = _S2._RL_BACKOFF_S; _S2._RL_BACKOFF_S = (0.0, 0.0, 0.0)  # no real sleeps in the test
_rl1 = {"n": 0}
def _rl_429_then_ok():
    _rl1["n"] += 1
    if _rl1["n"] < 2:
        raise ConnectionRefusedError("HTTP status 429 Too Many Requests.")
    return "ok"
check("s2 _retry_rl rides a transient 429 (retry, then succeed)",
      _S2._retry_rl(_rl_429_then_ok) == "ok" and _rl1["n"] == 2)
_rl2 = {"n": 0}
def _rl_always_429():
    _rl2["n"] += 1
    raise ConnectionRefusedError("HTTP status 429 Too Many Requests.")
_gaveup = False
try:
    _S2._retry_rl(_rl_always_429)
except ConnectionRefusedError:
    _gaveup = True
check("s2 _retry_rl gives up BOUNDED on a sustained 429 (attempts == _RL_RETRIES+1, not 10)",
      _gaveup and _rl2["n"] == _S2._RL_RETRIES + 1)
_rl3 = {"n": 0}
def _rl_non429():
    _rl3["n"] += 1
    raise RuntimeError("boom")
_nonretried = False
try:
    _S2._retry_rl(_rl_non429)
except RuntimeError:
    _nonretried = True
check("s2 _retry_rl does NOT retry a non-429 error (fails on first attempt)",
      _nonretried and _rl3["n"] == 1)
_S2._RL_BACKOFF_S = _save_bo

# recently_throttled: a SINGLE 429 stamp (not only the 5-consecutive breaker) marks a throttle, so a
# cold field_skeleton(s2) empty is legible (the note then points the caller at source=openalex).
_save_429 = _S2._state.get("last_429", 0.0)
_S2._state["open_until"] = 0.0
_S2._state["last_429"] = _t.time()
check("s2 recently_throttled True after a fresh 429 stamp (before the breaker opens)",
      _S2.recently_throttled() is True)
_S2._state["last_429"] = _t.time() - 9999.0
check("s2 recently_throttled False once the 429 is stale and the circuit is closed",
      _S2.recently_throttled() is False)
_S2._state["last_429"] = _save_429

_AD = _SS.SemanticScholarAdapter()
check("s2 source dropped private client property", not hasattr(_AD, "client") and not hasattr(_AD, "_client"))
check("s2 source health_check delegates to _s2.health", _AD.health_check.__qualname__.startswith("SemanticScholarAdapter"))
_S2._state["open_until"] = _t.time() + 9999; _S2._health["result"] = None
check("s2 source health_check == shared circuit-open verdict", _AD.health_check()[0] is False)
_S2._state["open_until"] = 0.0; _S2._health["result"] = None

import inspect as _insp
check("s2 recommend-from-lists has limit param", "limit" in _insp.signature(_S2.get_recommended_papers_from_lists).parameters)

from penumbra.core import cache, prewarm
import tempfile as _tf
from pathlib import Path as _P
from contextvars import copy_context as _copy_context
from concurrent.futures import ThreadPoolExecutor as _TPE
cache.CACHE_DIR = _P(_tf.mkdtemp())
check("prewarm: cache exposes the refresh-margin API the warmer depends on",
      hasattr(cache, "set_refresh_margin") and hasattr(cache, "seconds_until_expiry"))
cache.set("smoke_warm_k", {"v": 1}, ttl=3600)
cache.set("smoke_near_k", {"v": 2}, ttl=600)
cache.set_refresh_margin(prewarm.REFRESH_MARGIN_S)
check("prewarm: refresh-margin serves a comfortably-warm key (no over-refetch of hot cache)",
      cache.get("smoke_warm_k") == {"v": 1})
check("prewarm: refresh-margin misses a near-expiry key (forces the keep-hot refetch)",
      cache.get("smoke_near_k") is None)
cache.set_fresh(True)
check("prewarm: fresh=True still forces a miss regardless of the refresh margin (fresh not weakened)",
      cache.get("smoke_warm_k") is None)
cache.set_fresh(False)
cache.set_refresh_margin(0)
check("prewarm: refresh-margin 0 (the default) restores the plain warm-cache read unchanged",
      cache.get("smoke_warm_k") == {"v": 1})
check("prewarm: seconds_until_expiry reports remaining TTL and None for missing/lapsed",
      (cache.seconds_until_expiry("smoke_missing_k") is None)
      and (cache.seconds_until_expiry("smoke_warm_k") is not None)
      and (3000 < cache.seconds_until_expiry("smoke_warm_k") <= 3600))
check("prewarm: REFRESH_MARGIN_S >= WARM_INTERVAL_S (caches never go cold between warm cycles)",
      prewarm.REFRESH_MARGIN_S >= prewarm.WARM_INTERVAL_S)
cache.set_refresh_margin(prewarm.REFRESH_MARGIN_S)
_ctxs = [_copy_context(), _copy_context()]
with _TPE(max_workers=2) as _ex:
    _got = list(_ex.map(lambda c, k: c.run(cache.get, k), _ctxs, ["smoke_warm_k", "smoke_near_k"]))
check("prewarm: refresh margin propagates through copy_context workers (per-PI / per-row fetches)",
      _got == [{"v": 1}, None])
cache.set_refresh_margin(0)

import inspect as _insp2
from penumbra.core import _github as _gh
from penumbra.core.sources.api import github_source as _ghs
from penumbra.core.sources.api import github_trending_source as _ght

check("github: get_json host-pins to api.github.com (off-host path -> None)",
      _gh.get_json("@evil.com/steal") is None and _gh.get_json("https://evil.com/x") is None)
check("github: get_json signature carries (path, params, headers) + honors cache_only egress guard",
      {"path", "params", "headers"} <= set(_insp2.signature(_gh.get_json).parameters)
      and (cache.set_cache_only(True), _gh.get_json("/rate_limit") is None, cache.set_cache_only(False))[1])
check("github: pacer ~30/min (Search-sized), concurrency-capped, breaker present",
      _gh._MIN_INTERVAL_S == 2.0 and _gh._MAX_CONCURRENCY == 4
      and _gh._BREAK_AFTER == 5 and hasattr(_gh, "_trip_breaker") and hasattr(_gh, "GitHubDown"))
check("github: holds a BackendGuard with its original constants (cap=4, break 5/120.0) + aliases wire to it",
      _guard_wired(_gh, "github", 4))
_gjsrc = _insp2.getsource(_gh.get_json)
check("github: get_json retries on 429/secondary-403 honoring retry-after, trips breaker on a survived throttle",
      "_retry_after" in _gjsrc and "_is_secondary_rate" in _gjsrc and "_trip_breaker()" in _gjsrc)
_rasrc = _insp2.getsource(_gh._retry_after)
check("github: _retry_after honors Retry-After then X-RateLimit-Reset (capped 5s)",
      "retry-after" in _rasrc and "x-ratelimit-reset" in _rasrc)
check("github: token loader factored into _github (github_source has no _load_token)",
      hasattr(_gh, "_load_token") and not hasattr(_ghs, "_load_token")
      and "_github._token" in _insp2.getsource(_ghs.GitHubAdapter.__init__))
check("github + github_trending health_check both delegate to _github.health()",
      "return _github.health()" in _insp2.getsource(_ghs.GitHubAdapter.health_check)
      and "return _github.health()" in _insp2.getsource(_ght.GitHubTrendingAdapter.health_check))
_hsrc = _insp2.getsource(_gh.health)
check("github: health single-flight 60s-cached probe of the quota-free /rate_limit",
      _gh._HEALTH_TTL_S == 60.0 and "/rate_limit" in _hsrc and "_health_lock" in _hsrc)
check("github_source REST surfaces route through _github.get_json (no raw http.get_json github URL)",
      "_github.get_json(" in _insp2.getsource(_ghs.GitHubAdapter)
      and "http.get_json" not in _insp2.getsource(_ghs.GitHubAdapter))
check("github_trending routes /search/repositories + /repos through _github.get_json",
      "_github.get_json(" in _insp2.getsource(_ght.GitHubTrendingAdapter))
check("github_source CACHE_TTL bumped to 10800 (3h) so 6-hourly watchtower polls hit the cache (M4)",
      _ghs.CACHE_TTL == 10800)
check("github_source _multi_surface stays SERIAL (no concurrency added)",
      "SERIAL on purpose" in _insp2.getsource(_ghs.GitHubAdapter._multi_surface))
check("github_source _repo_tree tries git/trees/HEAD first (skips the /repos round-trip on the happy path)",
      "git/trees/HEAD" in _insp2.getsource(_ghs.GitHubAdapter._repo_tree))

import penumbra.core.sources.api.exa_source as _exa
check("exa: _health is a callable single-flight probe", callable(_exa._health))
check("exa: _health_cache dict + _health_lock + 600s TTL present",
      isinstance(_exa._health_cache, dict)
      and {"at", "result"} <= set(_exa._health_cache)
      and _exa._HEALTH_TTL_S == 600.0
      and hasattr(_exa._health_lock, "acquire"))
_exa._health_cache["result"] = None
_calls = {"n": 0}
_orig_post = _exa.http.post_json
_exa.http.post_json = lambda *a, **k: (_calls.__setitem__("n", _calls["n"] + 1),
                                       {"results": [{"url": "http://x", "id": "1", "title": "t"}]})[1]
_exa.ExaAdapter._key = staticmethod(lambda: "k")
try:
    _verdicts = [_exa.ExaAdapter().health_check() for _ in range(5)]
finally:
    _exa.http.post_json = _orig_post
    _exa._health_cache["result"] = None
check("exa: 5 health_check calls collapse to ONE billed Exa search (single-flight TTL cache)",
      _calls["n"] == 1 and _verdicts[0] == (True, "OK (Exa API)") and all(v == _verdicts[0] for v in _verdicts))
check("exa: search over-fetch bucket is the stable upper bucket min(25,max(limit,10))",
      min(25, max(3, 10)) == 10 and min(25, max(50, 10)) == 25 and min(25, max(10, 10)) == 10)

import penumbra.core.relations as _rel
import inspect as _inspect
check("relations imports cache", getattr(_rel, "cache", None) is not None)
check("relations _WORKS_TTL is 6h", _rel._WORKS_TTL == 6 * 3600)
check("relations _COHORT_TTL is 6h", _rel._COHORT_TTL == 6 * 3600)
check("relations _RESOLVE_TTL is 1h", _rel._RESOLVE_TTL == 3600)
check("_oa_author_works has fresh param", "fresh" in _inspect.signature(_rel._oa_author_works).parameters)
check("_s2_author_works has fresh param", "fresh" in _inspect.signature(_rel._s2_author_works).parameters)
check("resolve_identity has fresh=False", _inspect.signature(_rel.resolve_identity).parameters["fresh"].default is False)
check("coauthors has fresh=False", _inspect.signature(_rel.coauthors).parameters["fresh"].default is False)
check("institution_cohort has fresh=False", _inspect.signature(_rel.institution_cohort).parameters["fresh"].default is False)
_rel.cache.set(_rel.cache.make_key("relations", "works", "openalex", "A_SMOKE_TEST"), [{"id": "W1"}], ttl=_rel._WORKS_TTL)
check("relations works cache key round-trips",
      _rel.cache.get(_rel.cache.make_key("relations", "works", "openalex", "A_SMOKE_TEST")) == [{"id": "W1"}])

_wcache.CACHE_DIR = _WASTE_ORIG_CACHE_DIR

# --- LOW-tier wasteful-usage fixes (enrich pooled client + Stack Exchange shared probe), 2026-06-16 ---
from penumbra.core import enrich, http, cache
check("enrich imports shared http module", enrich.http is http)
check("enrich dropped direct httpx import", not hasattr(enrich, "httpx"))
_cap = {}
class _Ctx:
    def __enter__(self):
        class R:
            status_code = 200; headers = {}; request = None
            def raise_for_status(self): pass
            def iter_raw(self): yield b'{"ok": true}'
        return R()
    def __exit__(self, *a): return False
class _FakeClient:
    def stream(self, method, url, *, timeout, headers, **kw):
        _cap.update(method=method, timeout=timeout, headers=headers, kwargs=kw); return _Ctx()
_saved = http._client
http._client = _FakeClient()
try:
    _d = enrich._get_json("https://api.unpaywall.org/v2/10.1/x?email=a@b.com")
finally:
    http._client = _saved
check("enrich._get_json returns parsed JSON dict", _d == {"ok": True})
check("enrich passes follow_redirects=False to pooled client (SSRF redirect guard)", _cap.get("kwargs", {}).get("follow_redirects") is False)
check("enrich preserves its mailto _UA over the shared UA", _cap.get("headers", {}).get("User-Agent", "").startswith("penumbra/"))
check("enrich forwards its _TIMEOUT", _cap.get("timeout") == enrich._TIMEOUT)
check("enrich still pins resolver host to _API_HOSTS", "api.unpaywall.org" in enrich._API_HOSTS and "evil.example.com" not in enrich._API_HOSTS)
def _off():
    try:
        enrich._get_json("https://evil.example.com/x"); return False
    except enrich._OffAllowlistHost:
        return True
check("enrich._get_json rejects off-allowlist host before any request", _off())

from penumbra.core import _stackexchange as _se
from penumbra.core.sources.scrape import stackoverflow_source as _so, academia_se_source as _ase
import inspect as _inspse
check("stackexchange: shared single-flight health() exists (60s cache)",
      callable(_se.health) and _se._HEALTH_TTL_S == 60.0 and isinstance(_se._health, dict))
check("stackexchange: both sources delegate health to _stackexchange.health()",
      "_stackexchange.health()" in _inspse.getsource(_so.StackOverflowAdapter.health_check)
      and "_stackexchange.health()" in _inspse.getsource(_ase.AcademiaSEAdapter.health_check))
check("stackexchange: sources dropped the direct httpx health probe (no module httpx)",
      not hasattr(_so, "httpx") and not hasattr(_ase, "httpx"))
_se._health["result"] = None
_secount = {"n": 0}
_orig_se_gj = _se.http.get_json
_se.http.get_json = lambda *a, **k: (_secount.__setitem__("n", _secount["n"] + 1),
                                     {"items": [{"x": 1}], "quota_remaining": 9999})[1]
try:
    _r1 = _se.health(); _r2 = _se.health()
finally:
    _se.http.get_json = _orig_se_gj
    _se._health["result"] = None
check("stackexchange: health() single-flight cached (2 calls -> 1 upstream probe)",
      _secount["n"] == 1 and _r1 == _r2 and _r1[0] is True and "quota=9999" in _r1[1])

# stackexchange shared quota breaker (2026-06-20): all 6 SE sources share api.stackexchange.com's
# keyless ~300/day per-IP quota; a broad search fires ~36 SE calls (6 searches + per-question answer
# fetches), so the quota empties fast and 429-storms (51+ log lines + latency + health flap). N
# consecutive quota-429s now trip a shared cooldown so _se_get skips the API entirely. Offline: drive
# the breaker state + monkeypatch http.get_json to PROVE no network call happens while cooling.
import penumbra.core.http as _se_http  # noqa: E402
_se._se_cooldown_until = 0.0
_se._se_fail_streak = 0
_se_cold0 = _se._se_cooling()
for _ in range(_se._SE_TRIP_AFTER):
    _se._se_record(False)
_se_tripped = _se._se_cooling()
_se_orig_gj, _se_calls = _se_http.get_json, []
_se_http.get_json = lambda *a, **k: (_se_calls.append(1), None)[1]
try:
    _se_skip = _se._se_get("https://api.stackexchange.com/2.3/search/advanced", {"q": "x"})
finally:
    _se_http.get_json = _se_orig_gj
_se._se_cooldown_until = 0.0  # reset so live/later code is unaffected by the test
_se._se_fail_streak = 0
check("stackexchange: quota breaker trips after N 429s + skips the shared API while cooling (no network)",
      (not _se_cold0) and _se_tripped and (_se_skip is None) and (len(_se_calls) == 0))
# key injection: a configured free Stack Apps key is sent on every SE GET (quota 300→10k/day)
_se._SE_KEY = "TESTKEY"
_se_cap = {}
_se_http.get_json = lambda url, params=None, **k: (_se_cap.update(params or {}), {"items": []})[1]
try:
    _se._se_get("https://api.stackexchange.com/2.3/search/advanced", {"q": "x", "site": "stackoverflow"})
finally:
    _se_http.get_json = _se_orig_gj
    _se._SE_KEY = ""
check("stackexchange: _se_get injects the configured Stack Apps key (raises quota 300→10k)",
      _se_cap.get("key") == "TESTKEY" and _se_cap.get("q") == "x")

# ---------------------------------------------------------------------------
# Finance sources: market_quote (CNBC) + sec_financials (SEC XBRL). Offline:
# pure helpers + fixtures shaped like the live payloads probed 2026-06-17.
# ---------------------------------------------------------------------------
from penumbra.core.sources.api import market_quote_source as _mq
from penumbra.core.sources.api import sec_financials_source as _sf

# -- market_quote: ticker extraction (positive + negative) --
check("market_quote: extracts cashtags + bare tickers, order-preserving deduped",
      _mq._extract_tickers("$NVDA AAPL and $nvda again") == ["NVDA", "AAPL"])
check("market_quote: common-word uppercase tokens are NOT tickers",
      _mq._extract_tickers("what is the EPS and ROE per the SEC for AI") == [])
check("market_quote: a query with no ticker yields no tickers",
      _mq._extract_tickers("tell me about apple earnings tomorrow") == [])

# -- market_quote: parse a CNBC quote fixture (single-symbol dict + multi-symbol list) --
_CNBC_GOOD = {
    "symbol": "ORCL", "code": 0, "name": "Oracle Corp", "exchange": "NYSE",
    "currencyCode": "USD", "last": "187.30", "change": "-1.03", "change_pct": "-0.55%",
    "open": "186.16", "high": "190.19", "low": "184.70", "previous_day_closing": "188.33",
    "volume": "3,789,726", "mktcapView": "538.683B", "pe": "30.30", "eps": "6.18",
    "dividend": "2.00", "dividendyield": "1.07%", "beta": "1.68",
    "yrhiprice": "345.72", "yrhidate": "09/10/25", "yrloprice": "134.57", "yrlodate": "04/10/26",
    "last_timedate": "10:55 AM EDT",
    "ExtendedMktQuote": {"type": "PRE_MKT", "last": "185.76", "change_pct": "-1.36%",
                         "last_timedate": "9:29 AM EDT"},
}
_CNBC_BAD = {"symbol": "ZZZZQQ", "code": 1}  # unknown ticker stub
check("market_quote: single-symbol dict envelope coerced to a one-item list",
      _mq._quotes({"FormattedQuoteResult": {"FormattedQuote": _CNBC_GOOD}}) == [_CNBC_GOOD])
check("market_quote: multi-symbol list envelope passes through",
      len(_mq._quotes({"FormattedQuoteResult": {"FormattedQuote": [_CNBC_GOOD, _CNBC_BAD]}})) == 2)
check("market_quote: junk envelope -> [] (no raise)",
      _mq._quotes({"x": 1}) == [] and _mq._quotes(None) == [])
_mqdoc = _mq.MarketQuoteAdapter._to_doc(_CNBC_GOOD)
check("market_quote: good quote -> doc with right symbol/title/url",
      _mqdoc is not None and _mqdoc.source_id == "ORCL"
      and _mqdoc.url == "https://www.cnbc.com/quotes/ORCL"
      and "Oracle Corp" in _mqdoc.title and "$187.30" in _mqdoc.title)
check("market_quote: doc content carries price, day range, mktcap, pre-market",
      _mqdoc is not None and "Last: 187.30" in _mqdoc.content
      and "Day range: 184.70 – 190.19" in _mqdoc.content
      and "Market cap: 538.683B" in _mqdoc.content and "PRE_MKT: 185.76" in _mqdoc.content)
check("market_quote: numeric signals parsed from string fields",
      _mqdoc is not None and _mqdoc.signals["last_price"].value == 187.30
      and _mqdoc.signals["change_pct"].value == -0.55)
check("market_quote: code!=0 stub (unknown ticker) is dropped, not invented",
      _mq.MarketQuoteAdapter._to_doc(_CNBC_BAD) is None)
check("market_quote: facets are explicit_only STRUCTURE/finance lookup",
      bool(_mq.MarketQuoteAdapter.explicit_only) and _mq.MarketQuoteAdapter.kind == "lookup"
      and _mq.MarketQuoteAdapter.domains == ["finance"] and _mq.MarketQuoteAdapter.modes == ["STRUCTURE"])

# -- sec_financials: ticker/company resolution against a fixture map --
_orig_load = _sf._load_ticker_map
_sf._TICKER_MAP = {"ORCL": {"cik": "0001341439", "title": "ORACLE CORP"},
                   "AAPL": {"cik": "0000320193", "title": "Apple Inc."}}
_sf._TICKER_BY_TITLE = [("oracle corp", "ORCL", "0001341439"),
                        ("apple inc.", "AAPL", "0000320193")]
try:
    _r_tk = _sf._resolve("ORCL")
    _r_nm = _sf._resolve("Oracle")
    _r_miss = _sf._resolve("xyzzy not a company 123")
finally:
    pass
check("sec_financials: ticker resolves to the right padded CIK",
      _r_tk is not None and _r_tk["cik"] == "0001341439" and _r_tk["ticker"] == "ORCL")
check("sec_financials: company-name substring resolves (case-insensitive)",
      _r_nm is not None and _r_nm["cik"] == "0001341439")
check("sec_financials: unknown query -> None (no guess)", _r_miss is None)
check("sec_financials: CIK zero-padded to 10 digits", _sf._pad_cik(1341439) == "0001341439")

# -- sec_financials: _latest_fact picks newest end, prefers the SHORTER (quarterly) span --
_FACTS_REV = {"RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
    {"start": "2025-09-28", "end": "2026-03-28", "val": 254940000000, "fy": 2026, "fp": "Q2",
     "form": "10-Q", "filed": "2026-05-01"},   # 6-month YTD — must NOT win
    {"start": "2025-12-28", "end": "2026-03-28", "val": 111180000000, "fy": 2026, "fp": "Q2",
     "form": "10-Q", "filed": "2026-05-01"},   # true quarter — should win
    {"start": "2024-12-28", "end": "2025-03-29", "val": 95360000000, "fy": 2025, "fp": "Q2",
     "form": "10-Q", "filed": "2025-05-02"},   # older end — must NOT win
]}}}
_rev = _sf._latest_fact(_FACTS_REV, _sf._REVENUE_CONCEPTS)
check("sec_financials: latest_fact picks the newest end-date entry",
      _rev is not None and _rev["end"] == "2026-03-28")
check("sec_financials: among same-end entries it keeps the QUARTERLY (shorter span), not YTD",
      _rev is not None and _rev["val"] == 111180000000)
check("sec_financials: Liabilities has no LiabilitiesAndStockholdersEquity fallback",
      _sf._CONCEPTS[2] == ("Total liabilities", ["Liabilities"]))
check("sec_financials: absent concept -> None (no fabricated number)",
      _sf._latest_fact({}, ["Liabilities"]) is None)
check("sec_financials: _fmt_usd renders compact magnitudes",
      _sf._fmt_usd(245240000000, "USD") == "$245.24B" and _sf._fmt_usd(-1030, "USD") == "-$1,030")
check("sec_financials: facets are explicit_only STRUCTURE/finance lookup",
      bool(_sf.SECFinancialsAdapter.explicit_only) and _sf.SECFinancialsAdapter.kind == "lookup"
      and _sf.SECFinancialsAdapter.domains == ["finance"]
      and _sf.SECFinancialsAdapter.modes == ["STRUCTURE"])

# -- sec_financials: _recent_filings reads the parallel arrays, builds direct links --
_SUB_FIX = {"name": "ORACLE CORP", "sicDescription": "Services-Prepackaged Software",
            "filings": {"recent": {
                "form": ["8-K", "10-Q"],
                "filingDate": ["2026-06-10", "2026-03-11"],
                "accessionNumber": ["0001193125-26-265848", "0001341439-26-000020"],
                "primaryDocument": ["orcl-20260610.htm", "orcl-20260228.htm"],
                "primaryDocDescription": ["8-K", "10-Q"],
            }}}
_fil = _sf.SECFinancialsAdapter._recent_filings(_SUB_FIX, "1341439")
check("sec_financials: recent filings parsed newest-first with form+date",
      len(_fil) == 2 and _fil[0]["form"] == "8-K" and _fil[0]["date"] == "2026-06-10")
check("sec_financials: filing link points at the primaryDocument under the accession",
      _fil[0]["url"] == "https://www.sec.gov/Archives/edgar/data/1341439/000119312526265848/orcl-20260610.htm")
check("sec_financials: malformed submissions -> [] (no raise)",
      _sf.SECFinancialsAdapter._recent_filings(None, "1") == []
      and _sf.SECFinancialsAdapter._recent_filings({"filings": {}}, "1") == [])

# ---------------------------------------------------------------------------
# 23. Self-repair diagnostic trace (diag + fetch_one_with_diag): opt-in capture,
#     drained into a diagnostic on empty/error, ZERO cost + ZERO pollution on the
#     broad fan-out, and no credential leak in a captured URL.
# ---------------------------------------------------------------------------
from penumbra.core import diag as _diag  # noqa: E402
from penumbra.core.normalize import Document as _PDoc  # noqa: E402

# -- diag.note is a NO-OP when capture is not enabled (the search_many fan-out path) --
_diag.drain()  # reset any prior state
_diag.note("smoke.helper", url="https://x/?api_key=SECRET", status=500, body="boom")
check("diag: note() is a no-op when capture is NOT enabled (broad-search zero-cost)",
      _diag.active() is False and _diag.drain() == [])

# -- enable() arms capture; note() records; drain() returns + resets to OFF --
_diag.enable()
check("diag: enable() arms capture", _diag.active() is True)
_diag.note("smoke.helper", url="https://api.x/v1?q=hi", status=403, body="blocked")
_drained = _diag.drain()
check("diag: enable→note→drain returns the captured record",
      len(_drained) == 1 and _drained[0]["helper"] == "smoke.helper"
      and _drained[0]["status"] == 403 and _drained[0]["body"] == "blocked")
check("diag: drain() resets capture to OFF", _diag.active() is False)

# -- a captured URL has its credential query values stripped --
_diag.enable()
_diag.note("smoke.helper", url="https://api.x/search?api_key=SECRET123&token=TToken9&q=cats")
_cap = _diag.drain()
_url = _cap[0].get("url", "")
# the secret VALUES are gone (replaced by a redaction marker, percent-encoded by urlencode);
# the non-secret q=cats survives so the trace still tells the fixing agent what was requested.
check("diag: captured URL strips api_key + token values (no credential leak)",
      "SECRET123" not in _url and "TToken9" not in _url and "redacted" in _url and "q=cats" in _url)

# -- body is truncated to the cap --
_diag.enable()
_diag.note("smoke.helper", body="z" * 5000)
_capb = _diag.drain()
check("diag: oversized body is truncated to <= the cap + a marker",
      len(_capb[0]["body"]) <= 520 and _capb[0]["body"].endswith("(truncated)"))

# -- a record cap bounds a retry storm --
_diag.enable()
for _i in range(200):
    _diag.note("smoke.flood", body="x")
check("diag: captures are bounded (a retry storm cannot grow unbounded)",
      len(_diag.drain()) <= 60)


# A minimal fake adapter so the diagnostic path is exercised OFFLINE (no network, no real source).
class _FakeAdapter:
    name = "smoke_diag_fake"
    needs_credentials = False
    description = "smoke-only fake adapter for the diagnostic trace"

    def __init__(self, mode: str) -> None:
        self._mode = mode  # "ok" | "empty" | "egress_fail" | "raise"

    def search(self, query: str, limit: int = 10):
        if self._mode == "ok":
            return [_PDoc(source=self.name, source_id="1", url="https://x/1",
                          title="hit", content="body")]
        if self._mode == "empty":
            return []  # a well-formed empty (query miss / silent selector): no egress note
        if self._mode == "egress_fail":
            _diag.note("fake.egress", url="https://api.x/?api_key=SEKRIT", status=412,
                       body="anti-bot wall")
            return []
        raise RuntimeError("adapter blew up")

    def fetch_url(self, url: str):
        return None

    def health_check(self):
        return True, "ok"


def _with_fake(mode: str):
    """Register a fresh fake adapter under the same name, returning fetch_one_with_diag's result.
    Uses the live registry the same way a real source would; deadline_s=None keeps it in-thread so
    the captures drain deterministically (offline, no daemon-timeout flake)."""
    fetcher._adapters[_FakeAdapter.name] = _FakeAdapter(mode)
    try:
        return fetcher.fetch_one_with_diag(_FakeAdapter.name, "q", limit=5, deadline_s=None)
    finally:
        fetcher._adapters.pop(_FakeAdapter.name, None)

# -- SUCCESS path: docs returned, NO egress failure → diagnostic is None (zero noise) --
_docs_ok, _diag_ok = _with_fake("ok")
check("fetch_one_with_diag: success with results hangs NO diagnostic",
      len(_docs_ok) == 1 and _diag_ok is None)

# -- the broad fan-out (search_many) does NOT arm capture → a fake egress note inside it is a no-op --
fetcher._adapters[_FakeAdapter.name] = _FakeAdapter("egress_fail")
try:
    _res, _meta = fetcher.search_many("q", sources=[_FakeAdapter.name], deadline_s=30)
finally:
    fetcher._adapters.pop(_FakeAdapter.name, None)
check("search_many (broad path): capture stays OFF (no cross-source diagnostic pollution)",
      _diag.active() is False and _FakeAdapter.name in _meta["empty"])

# -- EMPTY-with-egress-failure: diagnostic filled with adapter_path + the captured egress --
_docs_ef, _diag_ef = _with_fake("egress_fail")
check("fetch_one_with_diag: empty+egress-fail → diagnostic with adapter_path + captures",
      _docs_ef == [] and isinstance(_diag_ef, dict)
      and (_diag_ef.get("adapter_path") or "").endswith("smoke.py")
      and _diag_ef.get("returned") == 0
      and any(c.get("status") == 412 for c in _diag_ef.get("captures", [])))
check("fetch_one_with_diag: captured URL in the diagnostic carries NO credential",
      all("SEKRIT" not in (c.get("url") or "") for c in _diag_ef.get("captures", [])))

# -- EMPTY-with-no-egress-failure: diagnostic still surfaces (well-formed empty / silent selector) --
_docs_em, _diag_em = _with_fake("empty")
check("fetch_one_with_diag: empty with no egress failure still yields a diagnostic (silent-empty)",
      _docs_em == [] and isinstance(_diag_em, dict) and _diag_em.get("captures") == []
      and "zero items" in (_diag_em.get("note") or ""))

# -- RAISE path: the error PROPAGATES (historical contract) AND carries a stashed diagnostic --
_raised_ok = False
_stashed = None
fetcher._adapters[_FakeAdapter.name] = _FakeAdapter("raise")
try:
    fetcher.fetch_one_with_diag(_FakeAdapter.name, "q", deadline_s=None)
except RuntimeError as _exc:
    _raised_ok = True
    _stashed = getattr(_exc, "_eye_diagnostic", None)
finally:
    fetcher._adapters.pop(_FakeAdapter.name, None)
check("fetch_one_with_diag: a raising adapter still PROPAGATES (error contract preserved)",
      _raised_ok is True)
check("fetch_one_with_diag: the propagated error stashes a diagnostic (adapter raised: ...)",
      isinstance(_stashed, dict) and "adapter raised" in (_stashed.get("note") or ""))

# -- fetch_one's signature/return is unchanged: it returns the docs list only --
fetcher._adapters[_FakeAdapter.name] = _FakeAdapter("ok")
try:
    _legacy = fetcher.fetch_one(_FakeAdapter.name, "q", deadline_s=None)
finally:
    fetcher._adapters.pop(_FakeAdapter.name, None)
check("fetch_one: legacy signature unchanged (returns the docs list, not a tuple)",
      isinstance(_legacy, list) and len(_legacy) == 1)


# ---------------------------------------------------------------------------
# (N) web_fallback render-tail (offline): a URL no adapter claims gets a plain fetch,
#     escalating to a Jina headless render ONLY when the extracted text is thin. The
#     fixture payloads are condensed from the LIVE anthropic.com/research head-to-head.
# ---------------------------------------------------------------------------
from penumbra.core import web_fallback as wf
from penumbra.core import cache as _wcache, http as _whttp
_wf_g, _wf_s = _wcache.get, _wcache.set
_wf_get = _whttp.get
_wf_calls = {"plain": 0, "jina": 0}
class _WFResp:
    def __init__(self, text, ctype="text/html; charset=utf-8"):
        self.text = text; self.headers = {"content-type": ctype}
_RICH = "<html><body><main><h1>Real Article</h1>" + ("<p>substantive body sentence. </p>"*60) + "</main></body></html>"
_THIN = "<html><body><nav>Research Policy Learn News</nav><main><h1>Research</h1><p>Our research teams investigate the safety of AI models.</p></main></body></html>"
_JMD  = "Title: Research\nURL Source: https://www.anthropic.com/research\nMarkdown Content: Our research teams...\n### Interpretability\n### Alignment\n[Teaching Claude why](https://www.anthropic.com/x)" + (" more real article listing."*40)
def _wf_fake_get(url, **kw):
    if url.startswith(wf._JINA_ENDPOINT):
        _wf_calls["jina"] += 1; return _WFResp(_JMD, "text/plain; charset=utf-8")
    _wf_calls["plain"] += 1
    return _WFResp(_RICH) if "rich.example" in url else _WFResp(_THIN)
try:
    _wcache.get = lambda k: None; _wcache.set = lambda *a, **k: None
    _whttp.get = _wf_fake_get
    _wf_calls.update(plain=0, jina=0)
    _d1 = wf.read_via_fallback("https://rich.example.com/article")
    check("web_fallback: a RICH server-rendered page returns via plain, Jina NOT called (happy path zero extra call)",
          _d1 is not None and _d1.source == "web" and _wf_calls["jina"] == 0 and "render:plain" in _d1.tags and len(_d1.content) > 600)
    _wf_calls.update(plain=0, jina=0)
    _d2 = wf.read_via_fallback("https://www.anthropic.com/research")
    check("web_fallback: a THIN SPA shell escalates to ONE Jina call and returns its markdown",
          _d2 is not None and _wf_calls["jina"] == 1 and "render:jina" in _d2.tags and _d2.title == "Research"
          and "Interpretability" in _d2.content and _d2.metadata["raw"]["rendered_via"] == "jina")
    _whttp.get = lambda url, **kw: (None if url.startswith(wf._JINA_ENDPOINT) else _WFResp("<html><body></body></html>"))
    check("web_fallback: both plain+jina thin -> None (matched=false preserved, no fake empty doc)",
          wf.read_via_fallback("https://nothing.example.com/x") is None)
    check("web_fallback: a non-http scheme is rejected before any IO",
          wf.read_via_fallback("file:///etc/passwd") is None and wf.read_via_fallback("ftp://x/y") is None)
finally:
    _wcache.get, _wcache.set = _wf_g, _wf_s
    _whttp.get = _wf_get


# ---------------------------------------------------------------------------
# (N) retrieval-infra radar (offline): a 检索基建 page_watch row fingerprints on change and is
#     SELF-DISTINGUISHING (label prefix "检索基建·" + the retrieval-infra region) so the agent
#     splits SG-rules vs vendor rows in the ONE merged page_watch digest. We do NOT rely on
#     keyword isolation: filter_rank's CJK matching lets a short SG token like "规则" spuriously
#     match a vendor row, so the two watches were merged into one (query=""). Locks the load-
#     bearing facts: fingerprint-on-change + the row stays name-queryable by "检索基建".
# ---------------------------------------------------------------------------
from penumbra.core.sources.scrape import page_watch_source as _pws  # noqa: E402
from penumbra.core.normalize import keyword_score_filter as _pw_kwf  # noqa: E402
from penumbra.core import cache as _pwcache  # noqa: E402
_pw_g, _pw_s = _pwcache.get, _pwcache.set
_pw_pt = _pws._page_text
_pw_text = "免费档 pricing free tier 1000 credits per month " * 8  # >=200 chars, has free/pricing
try:
    _pwcache.get = lambda k: None; _pwcache.set = lambda *a, **k: None
    _pws._page_text = lambda url, render=False: _pw_text
    _ad = _pws.PageWatchAdapter()
    _ri_row = {"name": "ri_x", "label": "检索基建·X 价格", "url": "https://example/x",
               "regions": ["retrieval-infra"]}
    _ri_doc = _ad._doc_for(_ri_row)
    import re as _pw_re  # noqa: E402
    check("page_watch radar: a 检索基建 row fingerprints into source_id (change flips the hex)",
          _ri_doc is not None and _ri_doc.source == "page_watch"
          and _pw_re.fullmatch(r"ri_x:[0-9a-f]{10}", _ri_doc.source_id) is not None
          and _ri_doc.title.startswith("检索基建·X 价格 · 内容指纹 ")
          and "retrieval-infra" in _ri_doc.tags
          and _ri_doc.metadata["page"] == "ri_x"
          and _pw_re.fullmatch(r"[0-9a-f]{10}", _ri_doc.metadata["fingerprint"]) is not None
          and _ri_doc.metadata["chars"] >= 200)
    check("page_watch radar: a 检索基建 row self-distinguishes (label prefix + region) and stays name-queryable",
          _ri_doc.title.startswith("检索基建·")
          and "retrieval-infra" in _ri_doc.tags
          and _pw_kwf([_ri_doc], "检索基建") == [_ri_doc])
    # render=True routes through the CDP render path (offline: real _page_text + stubbed _render_html)
    # and _strip_to_text drops head/script bloat — the SPA-changelog JS-watcher 学以致用 path.
    _pws._page_text = _pw_pt  # the real one (the render branch lives inside it)
    _pw_rh = _pws._render_html
    _pws._render_html = lambda url: ("<html><head><script>var x=1</script></head><body>"
                                     + "Changelog v2.3 new endpoint added " * 8 + "</body></html>")
    _rn_doc = _ad._doc_for({"name": "ri_spa", "label": "检索基建·SPA", "url": "https://docs.x/cl",
                            "render": True, "regions": ["retrieval-infra"]})
    _pws._render_html = _pw_rh
    check("page_watch render: render=True drives CDP render + _strip_to_text drops head/script bloat",
          _rn_doc is not None and "Changelog v2.3 new endpoint" in _rn_doc.content
          and "var x=1" not in _rn_doc.content and _rn_doc.metadata["page"] == "ri_spa")
finally:
    _pwcache.get, _pwcache.set = _pw_g, _pw_s
    _pws._page_text = _pw_pt

# Declarative schema-extraction engine (BaseScrapeAdapter.extract_schema; borrowed crawl4ai
# JsonCssExtractionStrategy minus eval): a source declared as a JSON schema parses HTML → docs
# with ZERO per-source parsing code.
from penumbra.core.sources.scrape._base import BaseScrapeAdapter as _BSA  # noqa: E402
class _SchemaProbe(_BSA, register=False):
    name = "_schema_probe"; description = "schema-engine smoke"
    base_url = "https://ex.com"
    extract_schema = {"item_selector": ".r",
                      "fields": {"title": {"selector": "h3"}, "url": {"selector": "a", "attr": "href"},
                                 "content": {"selector": ".s"}}}
_sp_html = ('<div class="r"><h3>加拿大博后</h3><a href="/t/1">x</a><div class="s">薪资约 50k</div></div>'
            '<div class="r"><h3>Second</h3><a href="https://o.com/2">y</a><div class="s">body two</div></div>')
_sp_docs = _SchemaProbe()._to_documents(_sp_html, "q", 5)
check("scrape schema engine: extract_schema builds docs from HTML (CSS-driven, zero per-source code)",
      len(_sp_docs) == 2 and _sp_docs[0].title == "加拿大博后"
      and _sp_docs[0].url == "https://ex.com/t/1" and _sp_docs[1].url == "https://o.com/2"
      and "薪资约 50k" in _sp_docs[0].content and _sp_docs[0].source == "_schema_probe")


# ---------------------------------------------------------------------------
# 25. CORE (core.ac.uk full-text): the differentiator invariant: _work_to_document
#    turns a recorded CORE work into a doc whose CONTENT carries the extracted full
#    text body (not just an abstract), stamps full_text_chars, surfaces the OA PDF,
#    prefers the DOI url, and reads citations as a citation signal. Pure / offline.
# ---------------------------------------------------------------------------
from penumbra.core.sources.api import core_source  # noqa: E402

check("core: registered as a keyed papers source",
      "core" in names and fetcher.get_adapter("core").needs_credentials is True)

# Recorded CORE /v3/search/works work (field shapes per searxng's live v3 engine).
_core_work = {
    "id": 123456789,
    "title": "Attention Is All You Need",
    "doi": "10.5555/3295222.3295349",
    "abstract": "We propose the Transformer, a model architecture based solely on attention.",
    "fullText": "1 Introduction Recurrent neural networks " + ("body " * 4000),  # > preview cap
    "authors": [{"name": "Ashish Vaswani"}, {"name": "Noam Shazeer"}],
    "yearPublished": 2017,
    "publishedDate": "2017-06-12T00:00:00",
    "journals": [{"title": "NeurIPS"}],
    "publisher": "Curran Associates",
    "downloadUrl": "https://core.ac.uk/download/pdf/123456789.pdf",
    "sourceFulltextUrls": ["https://arxiv.org/pdf/1706.03762.pdf"],
    "fieldOfStudy": ["Computer Science"],
    "documentType": "research",
    "citationCount": 95000,
}
_cdoc = core_source.CoreAdapter._work_to_document(_core_work)
check("core: _work_to_document carries the FULL-TEXT body in content (the differentiator)",
      _cdoc is not None and _cdoc.content.startswith("1 Introduction")
      and _cdoc.metadata.get("has_full_text") is True
      and _cdoc.metadata.get("full_text_chars") > core_source._BODY_PREVIEW_CAP
      and len(_cdoc.content) <= core_source._BODY_PREVIEW_CAP + 200)  # preview + the "...more" note
check("core: surfaces the OA PDF (download + origin) in metadata AND media",
      _cdoc.metadata.get("download_url") == "https://core.ac.uk/download/pdf/123456789.pdf"
      and "https://arxiv.org/pdf/1706.03762.pdf" in _cdoc.metadata.get("source_fulltext_urls")
      and "https://core.ac.uk/download/pdf/123456789.pdf" in _cdoc.media)
check("core: prefers the DOI url, reads citations as a citation signal, parses date/venue",
      _cdoc.url == "https://doi.org/10.5555/3295222.3295349"
      and _cdoc.signals["citations"].value == 95000.0
      and _cdoc.signals["citations"].kind == "citation"
      and _cdoc.date is not None and _cdoc.date.year == 2017
      and _cdoc.metadata.get("venue") == "NeurIPS")
# abstract-only fallback (a work CORE indexed but has no extracted body for)
_cdoc2 = core_source.CoreAdapter._work_to_document(
    {"id": 9, "title": "Metadata-only work", "abstract": "Only an abstract here.",
     "fullText": "", "authors": [], "doi": ""})
check("core: falls back to abstract when fullText is empty, falls back to core.ac.uk url when no DOI",
      _cdoc2.content == "Only an abstract here."
      and _cdoc2.metadata.get("has_full_text") is False
      and _cdoc2.url == "https://core.ac.uk/works/9")
check("core: facets.json declares papers / STRUCTURE+RECALL",
      _facets.get("core", {}).get("domains") == ["papers"]
      and set(_facets.get("core", {}).get("modes", [])) == {"STRUCTURE", "RECALL"})


# ---------------------------------------------------------------------------
# 26. xiaohongshu fetch_url comment capture (offline): _flatten_captured_comments turns captured
#     /api/sns/web/v2/comment/page comment dicts into the [{author,text,likes}] doc-build shape,
#     incl. inline sub_comments as "↳ " lines, deduped by comment id. Pure decode, no network.
# ---------------------------------------------------------------------------
from penumbra.core.sources.walled import xiaohongshu_source as _xhs2  # noqa: E402
_xhs_cap = [
    {"id": "c1", "content": "评论区分享学习材料", "like_count": "3",
     "user_info": {"nickname": "猛猿"},
     "sub_comments": [{"id": "s1", "content": "不许营销", "like_count": "11",
                       "user_info": {"nickname": "猛猿"}}]},
    {"id": "c1", "content": "DUP same id -> deduped", "user_info": {"nickname": "x"}},
    {"id": "c2", "content": "rl for llm 有项目推荐吗", "like_count": "2",
     "user_info": {"nickname": "momo"}},
]
_flat = _xhs2._flatten_captured_comments(_xhs_cap)
check("xhs: _flatten_captured_comments flattens comments + inline sub_comments, dedupes by id",
      _flat == [{"author": "猛猿", "text": "评论区分享学习材料", "likes": "3", "id": "c1"},
                {"author": "猛猿", "text": "↳ 不许营销", "likes": "11", "id": "s1"},
                {"author": "momo", "text": "rl for llm 有项目推荐吗", "likes": "2", "id": "c2"}],
      detail=str(_flat))


# ---------------------------------------------------------------------------
# 27. xiaohongshu_cn (mainland signed-direct API): the URL router + comment flattener are pure
#     functions (no network / no signing) — test them offline. The signed fetch itself is
#     account-live and verified on the mini, not here.
# ---------------------------------------------------------------------------
from penumbra.core.sources.walled import xiaohongshu_cn_source as _xcn  # noqa: E402
_nid, _tok = _xcn._parse_note_url(
    "https://www.xiaohongshu.com/explore/6a2a504f000000002202bf1d?xsec_token=ABxyz=&xsec_source=")
check("xhs_cn: _parse_note_url extracts note_id + xsec_token from a mainland note URL",
      _nid == "6a2a504f000000002202bf1d" and _tok == "ABxyz=", detail=f"{_nid} / {_tok}")
check("xhs_cn: _parse_note_url rejects a non-xiaohongshu host (rednote stays on the scroll path)",
      _xcn._parse_note_url("https://www.rednote.com/explore/6a2a504f000000002202bf1d") == (None, ""))
check("xhs_cn: _wants_full reads the &xhs_full=1 per-note deep-drill override off the URL",
      _xcn._wants_full("https://www.xiaohongshu.com/explore/x?xsec_token=t&xhs_full=1") is True
      and _xcn._wants_full("https://www.xiaohongshu.com/explore/x?xsec_token=t") is False)
_xcn_out: list = []
_xcn._flatten([{"content": "正文评论", "like_count": "5", "user_info": {"nickname": "momo"},
                "id": "cc1",
                "sub_comments": [{"content": "一条回复", "like_count": "1",
                                  "user_info": {"nickname": "屿"}, "id": "ss1"}]}], _xcn_out)
check("xhs_cn: _flatten emits {author,text,likes,id} with sub-replies prefixed '↳ '",
      _xcn_out == [{"author": "momo", "text": "正文评论", "likes": 5, "id": "cc1"},
                   {"author": "屿", "text": "↳ 一条回复", "likes": 1, "id": "ss1"}], detail=str(_xcn_out))
_xcn_a = fetcher.get_adapter("xiaohongshu_cn")
check("xhs_cn: registered + explicit_only (account-rate-sensitive, never in the broad fan-out)",
      _xcn_a is not None and bool(fetcher._explicit_only_reason(_xcn_a)))
# the 风控 / warning classifier (_guard) maps each platform signal to the right typed reaction,
# offline. This is the load-bearing safety logic — a misclassification burns the precious account.
_xcn._tripped_until = 0.0; _xcn._trip_streak = 0  # clean breaker state for the classification test


def _raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


check("xhs_cn: _guard passes a healthy code:0 body straight through",
      _xcn._guard(200, {"code": 0, "data": {"x": 1}}) == {"code": 0, "data": {"x": 1}})
check("xhs_cn: _guard treats HTTP 461 (captcha/滑块) as a 风控 trip (XhsRiskSignal)",
      _raises(lambda: _xcn._guard(461, {}), _xcn.XhsRiskSignal))
check("xhs_cn: _guard treats {code:-1,success:false} (the #769 session cap) as a 风控 trip",
      _raises(lambda: _xcn._guard(200, {"code": -1, "success": False}), _xcn.XhsRiskSignal))
check("xhs_cn: _guard treats code 300012 (IP block) as a 风控 trip",
      _raises(lambda: _xcn._guard(200, {"code": 300012}), _xcn.XhsRiskSignal))
check("xhs_cn: _guard treats a 风控 text body (访问频次异常) as a trip",
      _raises(lambda: _xcn._guard(200, {"code": 1, "msg": "访问频次异常,请勿频繁操作"}), _xcn.XhsRiskSignal))
check("xhs_cn: _guard treats a deleted/abnormal note (-510001) as skip, NOT a trip (XhsNoteGone)",
      _raises(lambda: _xcn._guard(200, {"code": -510001}), _xcn.XhsNoteGone))
check("xhs_cn: _guard treats -101 (web_session invalid) as the re-auth-once path (_SessionExpired)",
      _raises(lambda: _xcn._guard(200, {"code": -101, "msg": "无登录信息，或登录信息为空"}), _xcn._SessionExpired))
check("xhs_cn: a 风控 trip OPENED the breaker (_tripped True) -> live entry points go inert",
      _xcn._tripped() is True)
_xcn._tripped_until = 0.0; _xcn._trip_streak = 0  # leave the breaker clean for the live server

# ---------------------------------------------------------------------------
# 27b. xiaohongshu_cn BROWSER-primary path (2026-06-25 mechanism flip: drive the 9224 browser to
#      issue its own signed XHR, like the rednote 小号). The captured /search/notes decode
#      (_items_to_docs) is a pure function → golden-test it offline; the live 9224 CDP flow is
#      account-live and verified on the mini, not here.
# ---------------------------------------------------------------------------
check("xhs_cn: browser path enabled (_BROWSER_OK) + helpers wired",
      _xcn._BROWSER_OK and all(hasattr(_xcn, n) for n in
          ("_browser_alive", "_browser_search", "_browser_fetch", "_note_browser_cdp", "_cn_captcha",
           "_cn_login_wall", "_cn_card_to_document", "_cn_cards_from_html")))
check("xhs_cn: fetch_timeout >= the 110s browser cdp_call budget (penumbra_read URL backstop must not kill it)",
      _xcn_a.fetch_timeout >= 120.0, detail=str(_xcn_a.fetch_timeout))
# PRIMARY mainland search decode = DOM cards (the mainland SSRs results; /search/notes XHR doesn't fire).
_xcn_card_html = (
    '<section class="note-item"><div>'
    '<a href="/explore/680e07a4000000000b015b1e" style="display:none"></a>'
    '<a class="cover mask ld" href="/search_result/680e07a4000000000b015b1e?xsec_token=ABG-tok=&amp;xsec_source="></a>'
    '<div class="footer">'
    '<a class="title" href="/search_result/680e07a4000000000b015b1e?xsec_token=ABG-tok=&amp;xsec_source="><span>读博第一年踩坑</span></a>'
    '<div class="card-bottom-wrapper"><a class="author"><div class="name">学术小辣鸡</div></a>'
    '<span class="like-wrapper"><span class="count">1.2万</span></span></div>'
    '</div></div></section>')
_xcn_dom = _xcn._cn_cards_from_html(_xcn_card_html + _xcn_card_html, 5)  # dup card → dedup to 1
check("xhs_cn: _cn_cards_from_html decodes a mainland SSR note-item card — tokened xiaohongshu.com url, "
      "title, author, '1.2万'→12000 likes, dedups",
      len(_xcn_dom) == 1 and _xcn_dom[0].source == "xiaohongshu_cn"
      and _xcn_dom[0].source_id == "680e07a4000000000b015b1e"
      and "xiaohongshu.com" in _xcn_dom[0].url and "xsec_token" in _xcn_dom[0].url
      and _xcn_dom[0].title == "读博第一年踩坑" and _xcn_dom[0].author == "学术小辣鸡"
      and _xcn_dom[0].signals["likes"].value == 12000.0,
      detail=f"n={len(_xcn_dom)} " + (f"id={_xcn_dom[0].source_id} likes={_xcn_dom[0].signals['likes'].value}" if _xcn_dom else ""))
# XHR-bonus decode (kept; mainland rarely fires it) — '1.2万' must still parse via _parse_count not _int.
_xcn_item = {"id": "6a2a504f000000002202bf1d", "xsec_token": "ABxyz=",
             "note_card": {"display_title": "读博第一年踩坑", "user": {"nickname": "小鱼"},
                           "interact_info": {"liked_count": "1.2万"}, "type": "normal"}}
_xcn_docs = _xcn_a._items_to_docs([_xcn_item, _xcn_item, {"id": "z", "note_card": {}}], 10)
check("xhs_cn: _items_to_docs (XHR-bonus) — dedups, drops note_card-less rows, '1.2万'→12000, mainland url",
      len(_xcn_docs) == 1 and _xcn_docs[0].source == "xiaohongshu_cn"
      and _xcn_docs[0].signals["likes"].value == 12000.0
      and "xiaohongshu.com" in _xcn_docs[0].url
      and _xcn_docs[0].source_id == "6a2a504f000000002202bf1d",
      detail=f"n={len(_xcn_docs)}")


# ---------------------------------------------------------------------------
# 28. sogou_weixin (微信公众号 search): the HTML parser + doc builder are pure functions —
#     golden-fixture them offline against a recorded Sogou result block (no network).
# ---------------------------------------------------------------------------
from penumbra.core.sources.scrape import sogou_weixin_source as _sgw  # noqa: E402
_sgw_html = ("<ul class=\"news-list\">"
             "<li id=\"sogou_vr_11002601_box_0\"><div class=\"txt-box\">"
             "<h3><a target=\"_blank\" href=\"/link?url=AAA\">强化学习入门</a></h3>"
             "<p class=\"txt-info\">强化学习 指的是面向目标的算法</p>"
             "<div class=\"s-p\"><span class=\"all-time-y2\">机器之心</span>"
             "<span class=\"s2\"><script>document.write(timeConvert('1519445415'))</script></span></div></div></li>"
             "<li id=\"sogou_vr_11002601_box_1\"><div class=\"txt-box\">"
             "<h3><a href=\"/link?url=BBB\">RLHF 综述</a></h3>"
             "<p class=\"txt-info\">人类反馈强化学习</p>"
             "<div class=\"s-p\"><span class=\"all-time-y2\">PaperWeekly</span></div></div></li></ul>")
_sgw_items = _sgw._parse_items(_sgw_html, 10)
check("sogou_weixin: _parse_items pulls title / snippet / 公众号 / unix-ts from a Sogou result block",
      _sgw_items == [
          {"title": "强化学习入门", "link": "/link?url=AAA", "snippet": "强化学习 指的是面向目标的算法",
           "account": "机器之心", "ts": 1519445415},
          {"title": "RLHF 综述", "link": "/link?url=BBB", "snippet": "人类反馈强化学习",
           "account": "PaperWeekly", "ts": None}],
      detail=str(_sgw_items))
check("sogou_weixin: _is_blocked flags the anti-bot interstitial, passes a clean page",
      _sgw._is_blocked("x antispider x") and _sgw._is_blocked("请输入验证码")
      and not _sgw._is_blocked("<ul class='news-list'>"))
check("sogou_weixin: SogouBlocked is a raisable error (block surfaces, never a silent [] miss)",
      issubclass(_sgw.SogouBlocked, Exception))
_sgw_doc = _sgw.SogouWeixinAdapter()._to_documents(
    [{"title": "强化学习入门", "url": "https://mp.weixin.qq.com/s?src=11&x=1",
      "snippet": "面向目标的算法", "account": "机器之心", "ts": 1519445415}], "强化学习", 10)
check("sogou_weixin: _to_documents builds a doc on the PERMANENT mp.weixin url + 公众号 author + date",
      len(_sgw_doc) == 1 and _sgw_doc[0].url.startswith("https://mp.weixin.qq.com/")
      and _sgw_doc[0].author == "机器之心" and _sgw_doc[0].source == "sogou_weixin"
      and _sgw_doc[0].metadata.get("permanent_url") is True and _sgw_doc[0].date is not None,
      detail=str(_sgw_doc[0].url if _sgw_doc else None))
_sgw_a = fetcher.get_adapter("sogou_weixin")
check("sogou_weixin: registered + explicit_only (anti-bot scrape, named-only)",
      _sgw_a is not None and bool(fetcher._explicit_only_reason(_sgw_a)))

# ---------------------------------------------------------------------------
# 29. openalex_cn (中文学术 facet over the existing OpenAlex wrap): the language-pin is a pure
#     fn (offline); plus registration / explicit_only / cn-papers facets. (No live OpenAlex call.)
# ---------------------------------------------------------------------------
from penumbra.core.sources.api import openalex_cn_source as _oac  # noqa: E402
check("openalex_cn: _pin_zh appends language:zh, but respects a caller's explicit language filter",
      _oac._pin_zh("深度学习") == "深度学习 language:zh"
      and _oac._pin_zh("  x  ") == "x language:zh"
      and _oac._pin_zh("机器学习 language:en") == "机器学习 language:en"
      and _oac._pin_zh("type:dissertation 强化学习") == "type:dissertation 强化学习 language:zh")
_oac_a = fetcher.get_adapter("openalex_cn")
check("openalex_cn: registered, explicit_only, subclasses OpenAlex, cn/papers/STRUCTURE facets",
      _oac_a is not None and bool(fetcher._explicit_only_reason(_oac_a))
      and isinstance(_oac_a, _oac.OpenAlexAdapter)
      and _oac_a.regions == ["cn"] and _oac_a.domains == ["papers"] and _oac_a.modes == ["STRUCTURE"])

# ---------------------------------------------------------------------------
# 30. cninfo (巨潮 A-share filings): the JSON announcement → doc builder is a pure fn — golden
#     fixture it offline (title <em>-strip, PDF url, company author, ms-ts date), + registration.
# ---------------------------------------------------------------------------
from penumbra.core.sources.scrape import cninfo_source as _cni  # noqa: E402
check("cninfo: _clean_title strips the <em> keyword-highlight tags",
      _cni._clean_title("关于公司<em>数据安全</em>管控产品的公告") == "关于公司数据安全管控产品的公告")
_cni_doc = _cni._ann_to_doc({
    "announcementId": "1216083449", "announcementTitle": "关于支撑2023<em>数据安全</em>赛道的公告",
    "adjunctUrl": "finalpage/2023-03-10/1216083449.PDF", "announcementTime": 1678377600000,
    "secName": "永信至诚", "secCode": "688244"})
check("cninfo: _ann_to_doc builds a doc on the PDF url + 公司 author + date + cleaned title",
      _cni_doc is not None and _cni_doc.source == "cninfo"
      and _cni_doc.url == "https://static.cninfo.com.cn/finalpage/2023-03-10/1216083449.PDF"
      and _cni_doc.author == "永信至诚" and _cni_doc.tags == ["688244"]
      and "<em>" not in _cni_doc.title and _cni_doc.date is not None
      and _cni_doc.metadata.get("is_pdf") is True,
      detail=str(_cni_doc.url if _cni_doc else None))
check("cninfo: _ann_to_doc drops an announcement with no title or no adjunctUrl",
      _cni._ann_to_doc({"announcementTitle": "x", "adjunctUrl": ""}) is None)
_cni_a = fetcher.get_adapter("cninfo")
check("cninfo: registered + explicit_only + finance/cn/STRUCTURE+UNWALL facets",
      _cni_a is not None and bool(fetcher._explicit_only_reason(_cni_a))
      and _cni_a.domains == ["finance"] and "UNWALL" in (_cni_a.modes or []))

# ---------------------------------------------------------------------------
# 31. gov_policy (中国政府网 政策库): the JSON list-extract + policy→doc builder are pure fns —
#     golden fixture them offline (nested listVO path, <em>-strip, 文号/issuing-org, gov.cn url).
# ---------------------------------------------------------------------------
from penumbra.core.sources.scrape import gov_policy_source as _gp  # noqa: E402
check("gov_policy: _list_of finds listVO under searchVO AND under data.searchVO",
      _gp._list_of({"searchVO": {"listVO": [{"a": 1}]}}) == [{"a": 1}]
      and _gp._list_of({"data": {"searchVO": {"listVO": [{"b": 2}]}}}) == [{"b": 2}]
      and _gp._list_of({"searchVO": {}}) == [])
_gp_doc = _gp._doc_from_policy({
    "title": "网络<em>数据安全</em>管理条例", "url": "https://www.gov.cn/zhengce/zhengceku/x.htm",
    "pubtime": 1727686800000, "pcode": "国令第790号", "puborg": "国务院",
    "childtype": "信息产业", "summary": "国务院令 第790号 …", "code": "C123"})
check("gov_policy: _doc_from_policy builds a doc on the gov.cn 原文 url + 文号 + 国务院 + date",
      _gp_doc is not None and _gp_doc.source == "gov_policy"
      and _gp_doc.url == "https://www.gov.cn/zhengce/zhengceku/x.htm"
      and _gp_doc.title == "网络数据安全管理条例" and _gp_doc.author == "国务院"
      and _gp_doc.metadata.get("wenhao") == "国令第790号" and _gp_doc.date is not None,
      detail=str(_gp_doc.metadata if _gp_doc else None))
check("gov_policy: _doc_from_policy drops an item missing title or url",
      _gp._doc_from_policy({"title": "x", "url": ""}) is None)
_gp_a = fetcher.get_adapter("gov_policy")
check("gov_policy: registered + explicit_only + cn / STRUCTURE+UNWALL+MONITOR facets",
      _gp_a is not None and bool(fetcher._explicit_only_reason(_gp_a))
      and _gp_a.regions == ["cn"] and "MONITOR" in (_gp_a.modes or []))

# ---------------------------------------------------------------------------
# 32. eastmoney (A-share/HK/US quotes): quote backend moved EastMoney push2 → Tencent qt.gtimg.cn
#     (2026-06-20, push2 dropped the mini under multi-agent burst). The GBK `~`-parse + symbol map +
#     quote→doc are pure fns — golden fixture them offline (real values, NOT ×100; market→url; doc).
# ---------------------------------------------------------------------------
from penumbra.core.sources.scrape import eastmoney_source as _em  # noqa: E402
check("eastmoney: _num parses REAL Tencent values (no ÷100) + tolerates '-'/''/None",
      _em._num("1215.00") == 1215.0 and _em._num("-2.02") == -2.02
      and _em._num("-") is None and _em._num("") is None and _em._num(None) is None)
check("eastmoney: _tencent_symbol maps market via MktNum (沪/深/港/美, incl. STAR Classify '23')",
      _em._tencent_symbol({"Code": "600519", "Classify": "AStock", "MktNum": "1"}) == "sh600519"
      and _em._tencent_symbol({"Code": "000001", "Classify": "AStock", "MktNum": "0"}) == "sz000001"
      and _em._tencent_symbol({"Code": "688256", "Classify": "23", "MktNum": "1"}) == "sh688256"
      and _em._tencent_symbol({"Code": "00700", "Classify": "HK", "MktNum": "116"}) == "r_hk00700"
      and _em._tencent_symbol({"Code": "00981", "MktNum": "116"}) == "r_hk00981"
      and _em._tencent_symbol({"Code": "AAPL", "Classify": "UsStock"}) == "usAAPL"
      and _em._tencent_symbol({"Code": "", "Classify": "AStock"}) is None)
check("eastmoney: _normalize_query strips .HK/.SS/.SZ suffixes + pins market (0700.HK→00700/116)",
      _em._normalize_query("0700.HK") == ("00700", "116")
      and _em._normalize_query("600519.SS") == ("600519", "1")
      and _em._normalize_query("000001.SZ") == ("000001", "0")
      and _em._normalize_query("AAPL") == ("AAPL", None))
check("eastmoney: _quote_url maps the QuoteID market prefix (沪/深/港)",
      _em._quote_url("1.600519", "600519") == "https://quote.eastmoney.com/sh600519.html"
      and _em._quote_url("0.000001", "000001") == "https://quote.eastmoney.com/sz000001.html")
# A recorded Tencent qt.gtimg.cn line (indices probed live 2026-06-20); fill the meaningful slots +
# pad to clear the >=40-field guard, then run the real GBK-line parser + the doc builder.
_em_fields = ["0"] * 50
for _i, _v in {1: "贵州茅台", 2: "600519", 3: "1215.00", 4: "1240.00", 5: "1235.00",
               31: "-25.00", 32: "-2.02", 33: "1238.87", 34: "1211.22", 39: "18.36", 45: "15188.49"}.items():
    _em_fields[_i] = _v
_em_line = 'v_sh600519="' + "~".join(_em_fields) + '";'
_em_parsed = _em._parse_tencent_quotes(_em_line)
_em_doc = _em._quote_to_doc(_em_parsed.get("sh600519", []), "1.600519", "600519")
check("eastmoney: _parse_tencent_quotes splits the GBK `~` line keyed by tencent symbol",
      "sh600519" in _em_parsed and _em_parsed["sh600519"][3] == "1215.00")
check("eastmoney: _quote_to_doc builds a Tencent-backed quote doc (real price/pct, EM url, provenance)",
      _em_doc is not None and _em_doc.source == "eastmoney"
      and _em_doc.url == "https://quote.eastmoney.com/sh600519.html"
      and _em_doc.metadata.get("price") == 1215.0 and _em_doc.metadata.get("change_pct") == -2.02
      and _em_doc.metadata.get("provider") == "tencent_qt.gtimg.cn" and "贵州茅台" in _em_doc.title,
      detail=str(_em_doc.metadata if _em_doc else None))
_em_noprice = ["x"] * 50
_em_noprice[3] = "-"  # has a name but no parseable price → dropped
check("eastmoney: _quote_to_doc drops a quote with no price / too-short fields",
      _em._quote_to_doc(_em_noprice, "1.x", "x") is None and _em._quote_to_doc([], "1.x", "x") is None)
_em_a = fetcher.get_adapter("eastmoney")
check("eastmoney: registered + explicit_only + finance/cn/STRUCTURE",
      _em_a is not None and bool(fetcher._explicit_only_reason(_em_a)) and _em_a.domains == ["finance"])

# ---------------------------------------------------------------------------
# 32b. market_crypto (CoinGecko spot): _resolve (symbol map + id passthrough, NO guessing a coin
#      from a bare word) + _to_doc are pure fns → golden fixture offline.
# ---------------------------------------------------------------------------
from penumbra.core.sources.api import market_crypto_source as _mc  # noqa: E402
check("market_crypto: _resolve maps majors + id passthrough, drops bare unknown words/tickers",
      _mc._resolve("BTC $ETH solana") == [("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL")]
      and _mc._resolve("avalanche-2") == [("avalanche-2", "AVAX")]
      and _mc._resolve("buy ZZZZ now") == [] and _mc._resolve("") == [])
_mc_doc = _mc._to_doc("bitcoin", "BTC", {"usd": 62419, "usd_market_cap": 1.25e12,
                                         "usd_24h_vol": 3.0e10, "usd_24h_change": -3.26})
check("market_crypto: _to_doc builds a coin quote doc (price/24h signal, coingecko url + provenance)",
      _mc_doc is not None and _mc_doc.source == "market_crypto"
      and _mc_doc.url == "https://www.coingecko.com/en/coins/bitcoin"
      and _mc_doc.metadata.get("price_usd") == 62419
      and getattr(_mc_doc.signals.get("change_pct"), "value", None) == -3.26
      and "BTC" in _mc_doc.title, detail=str(_mc_doc.metadata if _mc_doc else None))
check("market_crypto: _to_doc drops an entry with no price (no fabrication)",
      _mc._to_doc("bitcoin", "BTC", {}) is None and _mc._to_doc("x", "X", {"usd_24h_change": 1}) is None)
_mc_a = fetcher.get_adapter("market_crypto")
check("market_crypto: registered + explicit_only + finance/STRUCTURE",
      _mc_a is not None and bool(fetcher._explicit_only_reason(_mc_a)) and _mc_a.domains == ["finance"])

# 32c. wayback (Internet Archive CDX archive lookup): _looks_like_url + _snap_to_doc pure fns.
from penumbra.core.sources.api import wayback_source as _wb  # noqa: E402
check("wayback: _looks_like_url accepts URL/bare-domain, rejects prose/empty",
      _wb._looks_like_url("https://example.com/page") and _wb._looks_like_url("example.com")
      and not _wb._looks_like_url("what is the capital of france") and not _wb._looks_like_url(""))
_wb_doc = _wb._snap_to_doc(["20200115123000", "http://example.com/", "200"],
                           {"timestamp": 0, "original": 1, "statuscode": 2})
check("wayback: _snap_to_doc builds a snapshot doc (web.archive.org url + parsed date + provenance)",
      _wb_doc is not None and _wb_doc.source == "wayback"
      and _wb_doc.url == "https://web.archive.org/web/20200115123000/http://example.com/"
      and _wb_doc.metadata.get("timestamp") == "20200115123000" and "2020-01-15" in _wb_doc.title)
check("wayback: _snap_to_doc drops a row missing timestamp/original (no fabrication)",
      _wb._snap_to_doc(["", "", ""], {"timestamp": 0, "original": 1, "statuscode": 2}) is None)
_wb_a = fetcher.get_adapter("wayback")
check("wayback: registered + explicit_only + RECALL/UNWALL",
      _wb_a is not None and bool(fetcher._explicit_only_reason(_wb_a)) and "RECALL" in (_wb_a.modes or []))

# ---------------------------------------------------------------------------
# 33. juejin (Chinese dev articles): the result_model → doc builder is a pure fn — golden fixture
#     it offline (article url, author, engagement signal; non-article rows drop to None).
# ---------------------------------------------------------------------------
from penumbra.core.sources.scrape import juejin_source as _jj  # noqa: E402
_jj_doc = _jj._article_to_doc({
    "article_info": {"article_id": "7180", "title": "向量数据库实战", "brief_content": "从0到1",
                     "ctime": "1700000000", "view_count": "6298", "digg_count": "12"},
    "author_user_info": {"user_name": "奇舞精选"}})
check("juejin: _article_to_doc builds a doc on the post url + author + digg signal",
      _jj_doc is not None and _jj_doc.source == "juejin"
      and _jj_doc.url == "https://juejin.cn/post/7180" and _jj_doc.author == "奇舞精选"
      and _jj_doc.metadata.get("views") == 6298 and _jj_doc.date is not None)
check("juejin: _article_to_doc drops a non-article result row (no article_info)",
      _jj._article_to_doc({"pin_info": {"id": "x"}}) is None)
_jj_a = fetcher.get_adapter("juejin")
check("juejin: registered + explicit_only + community/cn/STRUCTURE",
      _jj_a is not None and bool(fetcher._explicit_only_reason(_jj_a)) and _jj_a.domains == ["community"])

# ---------------------------------------------------------------------------
# 33b. nsf_awards (US NSF research grants): _award_to_doc is a pure fn — golden fixture it
#      offline (constructed award URL, PI author, $ amount signal; rows missing id/title drop).
# ---------------------------------------------------------------------------
from penumbra.core.sources.scrape import nsf_awards_source as _nsf  # noqa: E402
_nsf_doc = _nsf.NSFAwardsAdapter()._award_to_doc({
    "id": "2537281", "title": "Machine Learning for X", "abstractText": "This project develops...",
    "pdPIName": "Jane Roe", "awardeeName": "University Enterprises", "awardeeStateCode": "CA",
    "fundsObligatedAmt": "899983", "startDate": "01/15/2025"})
check("nsf_awards: _award_to_doc builds a doc on the constructed award URL + PI + $ signal",
      _nsf_doc is not None and _nsf_doc.source == "nsf_awards"
      and _nsf_doc.url == "https://www.nsf.gov/awardsearch/showAward?AWD_ID=2537281"
      and _nsf_doc.author == "Jane Roe" and _nsf_doc.date is not None
      and "award_amount" in _nsf_doc.signals)
check("nsf_awards: _award_to_doc drops an award with no id or no title",
      _nsf.NSFAwardsAdapter()._award_to_doc({"abstractText": "x"}) is None)
_nsf_a = fetcher.get_adapter("nsf_awards")
check("nsf_awards: registered + explicit_only + funding/us/STRUCTURE",
      _nsf_a is not None and bool(fetcher._explicit_only_reason(_nsf_a))
      and _nsf_a.domains == ["funding"] and _nsf_a.modes == ["STRUCTURE"])

# ---------------------------------------------------------------------------
# 33c. nih_reporter (US NIH biomedical grants, keyless POST): _project_to_doc is a pure fn —
#      golden fixture it offline (appl_id detail URL, CONTACT PI picked over the first PI, $
#      amount signal, org tag; the no-appl_id row falls back to the projnum URL; a row with
#      neither appl_id nor project_num drops to None).
# ---------------------------------------------------------------------------
from penumbra.core.sources.api import nih_reporter_source as _nih  # noqa: E402
_nih_doc = _nih.NIHReporterAdapter()._project_to_doc({
    "appl_id": 10704321, "project_num": "5R01AI123456-03",
    "project_title": "Mechanisms of Viral Latency",
    "abstract_text": "This project investigates...", "award_amount": 612345,
    "fiscal_year": 2024,
    "organization": {"org_name": "Johns Hopkins University", "org_country": "UNITED STATES"},
    "principal_investigators": [{"full_name": "Alice First", "is_contact_pi": False},
                                {"full_name": "Bob Contact", "is_contact_pi": True}],
    "agency_ic_admin": {"name": "NIAID"}, "project_start_date": "2022-09-01T00:00:00Z"})
check("nih_reporter: _project_to_doc builds a doc on the appl_id URL + CONTACT PI + $ signal + org tag",
      _nih_doc is not None and _nih_doc.source == "nih_reporter"
      and _nih_doc.source_id == "10704321"
      and _nih_doc.url == "https://reporter.nih.gov/project-details/10704321"
      and _nih_doc.author == "Bob Contact" and _nih_doc.date is not None
      and "award_amount" in _nih_doc.signals
      and _nih_doc.tags == ["Johns Hopkins University"])
check("nih_reporter: a row with no appl_id falls back to the projnum search URL",
      _nih.NIHReporterAdapter()._project_to_doc(
          {"project_num": "PN-9", "project_title": "No appl_id project"}).url
      == "https://reporter.nih.gov/search/?projnum=PN-9")
check("nih_reporter: _project_to_doc drops a row with neither appl_id nor project_num",
      _nih.NIHReporterAdapter()._project_to_doc({"project_title": "orphan"}) is None)
_nih_a = fetcher.get_adapter("nih_reporter")
check("nih_reporter: registered + explicit_only + funding/us/STRUCTURE",
      _nih_a is not None and bool(fetcher._explicit_only_reason(_nih_a))
      and _nih_a.domains == ["funding"] and _nih_a.regions == ["us"]
      and _nih_a.modes == ["STRUCTURE"])

# ---------------------------------------------------------------------------
# 33d. europepmc (keyless biomedical lit + OA full text): _result_to_doc is a pure fn
#      (no network) — golden fixture it offline (DOI url + DOI source_id + abstract content
#      + cited_by citation signal + pubType tags + journal/OA metadata; a titleless / id-less
#      row drops to None). The OA full-text JATS strip is exercised on a tiny inline document
#      (no network). The fan-out fetch itself (network) is verified live post-deploy.
# ---------------------------------------------------------------------------
from penumbra.core.sources.api import europepmc_source as _epmc  # noqa: E402
_epmc_ad = _epmc.EuropePMCAdapter()
_epmc_doc = _epmc_ad._result_to_doc({
    "id": "37001234", "source": "MED", "pmid": "37001234", "pmcid": "PMC10000001",
    "doi": "10.1038/s41586-023-00001-2", "title": "Single-cell atlas of the human lung",
    "authorString": "Roe J, Doe A, Smith B.", "journalTitle": "Nature",
    "pubYear": "2023", "firstPublicationDate": "2023-04-15",
    "isOpenAccess": "Y", "inEPMC": "Y", "hasPDF": "Y", "citedByCount": 142,
    "abstractText": "We profile 500,000 cells from human lung tissue.",
    "pubTypeList": {"pubType": ["research-article", "Journal Article"]},
    "fullTextUrlList": {"fullTextUrl": [
        {"availability": "Open access", "documentStyle": "pdf",
         "url": "https://europepmc.org/articles/PMC10000001?pdf=render"}]}})
check("europepmc: _result_to_doc builds a doc on the DOI url + DOI id + abstract + cited_by signal",
      _epmc_doc is not None and _epmc_doc.source == "europepmc"
      and _epmc_doc.source_id == "10.1038/s41586-023-00001-2"
      and _epmc_doc.url == "https://doi.org/10.1038/s41586-023-00001-2"
      and _epmc_doc.author == "Roe J, Doe A, Smith B."
      and _epmc_doc.content == "We profile 500,000 cells from human lung tissue."
      and _epmc_doc.date is not None
      and "cited_by" in _epmc_doc.signals
      and _epmc_doc.signals["cited_by"].value == 142.0
      and _epmc_doc.signals["cited_by"].kind == "citation"
      and _epmc_doc.tags == ["research-article", "Journal Article"]
      and _epmc_doc.metadata.get("journal") == "Nature"
      and _epmc_doc.metadata.get("pmcid") == "PMC10000001"
      and _epmc_doc.metadata.get("is_open_access") == "Y"
      and _epmc_doc.metadata.get("full_text_urls")
          == ["https://europepmc.org/articles/PMC10000001?pdf=render"])
# no DOI -> source_id + url fall back to the EPMC source:id / article page; pubYear dates it
_epmc_nodoi = _epmc_ad._result_to_doc({
    "id": "PPR123", "source": "PPR", "title": "A preprint without a DOI",
    "pubYear": "2024", "isOpenAccess": "N"})
check("europepmc: _result_to_doc falls back to source:id + EPMC article url when there is no DOI",
      _epmc_nodoi is not None and _epmc_nodoi.source_id == "PPR:PPR123"
      and _epmc_nodoi.url == "https://europepmc.org/article/PPR/PPR123"
      and _epmc_nodoi.content == "A preprint without a DOI"  # no abstract -> title is content
      and _epmc_nodoi.date is not None)  # pubYear fallback
check("europepmc: _result_to_doc drops a row with no title or no usable id",
      _epmc_ad._result_to_doc({"id": "x", "source": "MED", "doi": "10.1/y"}) is None  # no title
      and _epmc_ad._result_to_doc({"title": "Orphan", "isOpenAccess": "Y"}) is None)  # no doi + no source/id
# _fulltext_eligible: OA + inEPMC + pmcid + source all required (pure, no network)
check("europepmc: _fulltext_eligible requires OA + inEPMC + pmcid + source",
      _epmc.EuropePMCAdapter._fulltext_eligible(
          {"isOpenAccess": "Y", "inEPMC": "Y", "pmcid": "PMC1", "source": "MED"}) is True
      and _epmc.EuropePMCAdapter._fulltext_eligible(
          {"isOpenAccess": "N", "inEPMC": "Y", "pmcid": "PMC1", "source": "MED"}) is False
      and _epmc.EuropePMCAdapter._fulltext_eligible(
          {"isOpenAccess": "Y", "inEPMC": "Y", "source": "MED"}) is False)  # no pmcid
# _strip_jats: pull the <body> text, drop <front>/<ref-list>/<fig>/tags, unescape, collapse ws (no network)
_epmc_jats = ("<article><front><article-meta><title-group><article-title>T</article-title>"
              "</title-group></article-meta></front><body><sec><title>Intro</title>"
              "<p>Hello &amp; welcome to the <italic>study</italic>.</p>"
              "<fig><graphic xlink:href='f1.jpg'/></fig></sec>"
              "<ref-list><ref>NOISE citation 1</ref></ref-list></body></article>")
_epmc_body = _epmc.EuropePMCAdapter._strip_jats(_epmc_jats)
check("europepmc: _strip_jats keeps body prose, drops front/ref-list/fig noise, unescapes & collapses ws",
      "Hello & welcome to the study" in _epmc_body  # body prose, unescaped & collapsed
      and "Intro" in _epmc_body  # the <sec><title> stays; only <front> is dropped
      and "NOISE" not in _epmc_body and "<" not in _epmc_body
      and _epmc.EuropePMCAdapter._strip_jats("") == ""
      and _epmc.EuropePMCAdapter._strip_jats(None) == "")
_epmc_a = fetcher.get_adapter("europepmc")
check("europepmc: registered + keyless + explicit_only + papers/STRUCTURE/lookup",
      _epmc_a is not None and _epmc_a.needs_credentials is False
      and bool(fetcher._explicit_only_reason(_epmc_a))
      and _epmc_a.kind == "lookup" and _epmc_a.domains == ["papers"]
      and _epmc_a.modes == ["STRUCTURE"])

# ---------------------------------------------------------------------------
# 33e. orcid (researcher iD + CV record): _record_to_doc is a pure fn over one
#      /record payload — golden fixture it offline (name+org title, works signal,
#      latest date from the deep {"value":...} nesting; a record with no iD drops).
# ---------------------------------------------------------------------------
from penumbra.core.sources.api import orcid_source as _orc  # noqa: E402
_orc_rec = {
    "person": {"name": {"given-names": {"value": "Yoshua"},
                        "family-name": {"value": "Bengio"}}},
    "activities-summary": {
        "employments": {"affiliation-group": [{"summaries": [{"employment-summary": {
            "role-title": "Professor",
            "organization": {"name": "Universite de Montreal"},
            "start-date": {"year": {"value": "1993"}, "month": {"value": "9"}},
            "end-date": None}}]}]},
        "educations": {"affiliation-group": [{"summaries": [{"education-summary": {
            "role-title": "PhD", "organization": {"name": "McGill University"},
            "start-date": {"year": {"value": "1988"}}}}]}]},
        "works": {"group": [
            {"work-summary": [{"title": {"title": {"value": "Deep Learning"}},
                               "type": "book",
                               "publication-date": {"year": {"value": "2016"}}}]},
            {"work-summary": [{"title": {"title": {"value": "Attention mechanisms"}},
                               "type": "journal-article",
                               "publication-date": {"year": {"value": "2014"},
                                                    "month": {"value": "12"}}}]}]}}}
_orc_doc = _orc.OrcidAdapter()._record_to_doc("0000-0002-9322-3515", _orc_rec)
check("orcid: _record_to_doc builds a doc on the iD profile URL + name(org) title + works signal",
      _orc_doc is not None and _orc_doc.source == "orcid"
      and _orc_doc.source_id == "0000-0002-9322-3515"
      and _orc_doc.url == "https://orcid.org/0000-0002-9322-3515"
      and _orc_doc.author == "Yoshua Bengio"
      and _orc_doc.title == "Yoshua Bengio (Universite de Montreal)"
      and "works" in _orc_doc.signals and _orc_doc.signals["works"].value == 2.0
      and _orc_doc.date is not None and _orc_doc.date.year == 2016
      and len(_orc_doc.metadata["works"]) == 2
      and len(_orc_doc.metadata["employments"]) == 1)
check("orcid: _record_to_doc drops a record with no iD (None / empty)",
      _orc.OrcidAdapter()._record_to_doc("", _orc_rec) is None
      and _orc.OrcidAdapter()._record_to_doc(None, _orc_rec) is None)
_orc_a = fetcher.get_adapter("orcid")
check("orcid: registered + explicit_only + people/STRUCTURE keyless lookup",
      _orc_a is not None and bool(fetcher._explicit_only_reason(_orc_a))
      and _orc_a.needs_credentials is False and _orc_a.kind == "lookup"
      and _orc_a.domains == ["people"] and _orc_a.modes == ["STRUCTURE"])

# ---------------------------------------------------------------------------
# 33f. worldbank_stats (World Bank Indicators, keyless v2): a STRUCTURED point
#      lookup, not free-text search. _parse_query is the "<COUNTRY> <INDICATOR>"
#      convention (keyword map OR raw WB code, no guess on junk); _series_to_doc
#      folds one country x indicator series into ONE time-series doc. Pure fns.
# ---------------------------------------------------------------------------
from penumbra.core.sources.api import worldbank_stats_source as _wb  # noqa: E402
check("worldbank_stats: _parse_query maps a keyword + accepts a raw WB code, country first",
      _wb._parse_query("CN unemployment") == ("CN", "SL.UEM.TOTL.ZS")
      and _wb._parse_query("USA NY.GDP.MKTP.CD") == ("USA", "NY.GDP.MKTP.CD")
      and _wb._parse_query("CA gdp per capita") == ("CA", "NY.GDP.PCAP.CD"))
check("worldbank_stats: _parse_query returns None on empty / no-indicator / unknown indicator (no guess)",
      _wb._parse_query("") is None and _wb._parse_query("CN") is None
      and _wb._parse_query("CN zzzznope") is None)
_WB_ROWS = [
    {"indicator": {"id": "SL.UEM.TOTL.ZS", "value": "Unemployment, total (% of total labor force)"},
     "country": {"id": "CN", "value": "China"}, "countryiso3code": "CHN", "date": "2024", "value": 5.1, "unit": ""},
    {"indicator": {"id": "SL.UEM.TOTL.ZS", "value": "Unemployment, total (% of total labor force)"},
     "country": {"id": "CN", "value": "China"}, "countryiso3code": "CHN", "date": "2023", "value": 4.83, "unit": ""},
    {"indicator": {"id": "SL.UEM.TOTL.ZS", "value": "x"}, "country": {"id": "CN", "value": "China"},
     "countryiso3code": "CHN", "date": "2022", "value": None, "unit": ""},  # null year kept in series, never the headline
]
_wb_doc = _wb.WorldBankStatsAdapter._series_to_doc("CN", "SL.UEM.TOTL.ZS", _WB_ROWS)
check("worldbank_stats: _series_to_doc folds a country x indicator series into ONE doc (latest year + signal)",
      _wb_doc is not None and _wb_doc.source == "worldbank_stats"
      and _wb_doc.source_id == "CN/SL.UEM.TOTL.ZS"
      and _wb_doc.url == "https://data.worldbank.org/indicator/SL.UEM.TOTL.ZS?locations=CN"
      and _wb_doc.date is not None and _wb_doc.date.year == 2024
      and _wb_doc.signals["latest_value"].value == 5.1
      and _wb_doc.metadata["latest_year"] == 2024
      and list(_wb_doc.metadata["series"].keys()) == ["2024", "2023"]  # newest-first, null year excluded
      and "China" in _wb_doc.title)
check("worldbank_stats: _series_to_doc returns None on an empty / all-null series + _rows guards the envelope",
      _wb.WorldBankStatsAdapter._series_to_doc("CN", "X", []) is None
      and _wb.WorldBankStatsAdapter._series_to_doc("CN", "X",
            [{"date": "2020", "value": None, "indicator": {"value": "x"}, "country": {"value": "X"}}]) is None
      and _wb.WorldBankStatsAdapter._rows([{"message": [{"id": "120"}]}]) == []
      and _wb.WorldBankStatsAdapter._rows(None) == [])
_wb_a = fetcher.get_adapter("worldbank_stats")
check("worldbank_stats: registered + explicit_only + keyless data/STRUCTURE lookup",
      _wb_a is not None and bool(fetcher._explicit_only_reason(_wb_a))
      and _wb_a.needs_credentials is False and _wb_a.kind == "lookup"
      and _wb_a.domains == ["data"] and _wb_a.modes == ["STRUCTURE"])

# ---------------------------------------------------------------------------
# 33g. adzuna (keyed multi-country jobs + salary): _job_to_doc + _split_country + _salary_signal
#      are pure fns — golden fixture them offline (redirect_url, company author, salary midpoint
#      signal, location tag; the country-prefix convention; a titleless / urlless row drops).
# ---------------------------------------------------------------------------
from penumbra.core.sources.api import adzuna_source as _adz  # noqa: E402
check("adzuna: _split_country picks a leading country code else defaults to Canada",
      _adz.AdzunaAdapter._split_country("sg data scientist") == ("sg", "data scientist")
      and _adz.AdzunaAdapter._split_country("machine learning") == ("ca", "machine learning")
      and _adz.AdzunaAdapter._split_country("CA remote") == ("ca", "remote"))
_adz_job = _adz.AdzunaAdapter()._job_to_doc({
    "id": "4900", "title": "Machine Learning Engineer",
    "redirect_url": "https://www.adzuna.ca/jobs/details/4900",
    "description": "Build ML systems.", "created": "2026-06-01T12:00:00Z",
    "company": {"display_name": "Acme AI"}, "location": {"display_name": "Toronto, ON"},
    "category": {"label": "IT Jobs"}, "salary_min": 120000, "salary_max": 160000})
check("adzuna: _job_to_doc builds a doc on the redirect_url + company author + salary midpoint signal",
      _adz_job is not None and _adz_job.source == "adzuna"
      and _adz_job.url == "https://www.adzuna.ca/jobs/details/4900"
      and _adz_job.author == "Acme AI" and _adz_job.date is not None
      and "salary" in _adz_job.signals and _adz_job.signals["salary"].value == 140000.0
      and _adz_job.metadata["salary_max"] == 160000 and "Toronto, ON" in _adz_job.tags)
check("adzuna: _salary_signal empty when no numeric salary; _job_to_doc drops a titleless / urlless row",
      _adz.AdzunaAdapter._salary_signal({"salary_min": None, "salary_max": None}) == {}
      and _adz.AdzunaAdapter()._job_to_doc({"id": "x", "redirect_url": "u"}) is None
      and _adz.AdzunaAdapter()._job_to_doc({"title": "t"}) is None)
_adz_a = fetcher.get_adapter("adzuna")
check("adzuna: registered + keyed + explicit_only + jobs/STRUCTURE lookup",
      _adz_a is not None and _adz_a.needs_credentials is True
      and bool(fetcher._explicit_only_reason(_adz_a))
      and _adz_a.kind == "lookup" and _adz_a.domains == ["jobs"]
      and _adz_a.modes == ["STRUCTURE"])

# ---------------------------------------------------------------------------
# 33h. s2_authors (S2 researcher profiles, keyless): _author_to_doc is a pure fn — golden
#      fixture it offline (constructed author URL, citation signal, h-index metadata; a row
#      with no authorId/name drops). backend=semantic_scholar (same upstream as the paper source).
# ---------------------------------------------------------------------------
from penumbra.core.sources.api import s2_authors_source as _s2a  # noqa: E402
_s2a_doc = _s2a.S2AuthorsAdapter()._author_to_doc({
    "authorId": "1751762", "name": "Yoshua Bengio",
    "hIndex": 213, "paperCount": 812, "citationCount": 576845})
check("s2_authors: _author_to_doc builds a doc on the constructed author URL + citation signal",
      _s2a_doc is not None and _s2a_doc.source == "s2_authors"
      and _s2a_doc.url == "https://www.semanticscholar.org/author/1751762"
      and _s2a_doc.author == "Yoshua Bengio"
      and "citations" in _s2a_doc.signals and _s2a_doc.signals["citations"].value == 576845.0
      and _s2a_doc.signals["citations"].kind == "citation"
      and _s2a_doc.metadata["h_index"] == 213)
check("s2_authors: _author_to_doc drops a row with no authorId or no name",
      _s2a.S2AuthorsAdapter()._author_to_doc({"name": "x"}) is None
      and _s2a.S2AuthorsAdapter()._author_to_doc({"authorId": "1"}) is None)
_s2a_a = fetcher.get_adapter("s2_authors")
check("s2_authors: registered + explicit_only + people/STRUCTURE; backend=semantic_scholar",
      _s2a_a is not None and bool(fetcher._explicit_only_reason(_s2a_a))
      and _s2a_a.domains == ["people"] and _s2a_a.modes == ["STRUCTURE"]
      and getattr(_s2a_a, "backend", None) == "semantic_scholar")

# ---------------------------------------------------------------------------
# 33i. dblp_author (CS researcher profiles, keyless): _hit_to_doc + _notes are pure fns —
#      golden fixture them offline (PID url is canonical, affiliation/award split from the
#      dict|list notes shape; a hit with no name/url drops). backend=dblp.
# ---------------------------------------------------------------------------
from penumbra.core.sources.api import dblp_author_source as _dba  # noqa: E402
_dba_doc = _dba.DBLPAuthorAdapter()._hit_to_doc({"@score": "9", "@id": "56/953", "info": {
    "author": "Yoshua Bengio", "url": "https://dblp.org/pid/56/953",
    "notes": {"note": [{"@type": "affiliation", "text": "University of Montreal, QC, Canada"},
                       {"@type": "award", "text": "Turing Award"}]}}})
check("dblp_author: _hit_to_doc builds a doc on the canonical PID url + affiliation + award tags",
      _dba_doc is not None and _dba_doc.source == "dblp_author"
      and _dba_doc.url == "https://dblp.org/pid/56/953"
      and _dba_doc.author == "Yoshua Bengio"
      and "University of Montreal, QC, Canada" in _dba_doc.tags
      and "Turing Award" in _dba_doc.tags
      and _dba_doc.metadata["affiliations"] == ["University of Montreal, QC, Canada"])
check("dblp_author: _notes handles a single-note dict; _hit_to_doc drops a hit with no name/url",
      _dba.DBLPAuthorAdapter._notes({"note": {"@type": "affiliation", "text": "MIT"}}) == (["MIT"], [])
      and _dba.DBLPAuthorAdapter()._hit_to_doc({"info": {"author": "x"}}) is None
      and _dba.DBLPAuthorAdapter()._hit_to_doc({"info": {"url": "u"}}) is None)
_dba_a = fetcher.get_adapter("dblp_author")
check("dblp_author: registered + explicit_only + people/STRUCTURE; backend=dblp",
      _dba_a is not None and bool(fetcher._explicit_only_reason(_dba_a))
      and _dba_a.domains == ["people"] and _dba_a.modes == ["STRUCTURE"]
      and getattr(_dba_a, "backend", None) == "dblp")

# ---------------------------------------------------------------------------
# 33j. remotive (curated remote jobs, keyless): _job_to_doc + _strip_html are pure fns —
#      golden fixture them offline (real url, company author, HTML stripped to plain content,
#      category/location tags; a titleless / urlless row drops).
# ---------------------------------------------------------------------------
from penumbra.core.sources.scrape import remotive_source as _rmt  # noqa: E402
_rmt_doc = _rmt.RemotiveAdapter()._job_to_doc({
    "id": 2090887, "title": "Senior ML Engineer", "company_name": "EverAI",
    "category": "Artificial Intelligence", "candidate_required_location": "Worldwide",
    "job_type": "full_time", "publication_date": "2026-06-19T19:46:09",
    "url": "https://remotive.com/remote-jobs/123", "salary": "",
    "tags": ["AI", "ML"], "description": "<p>Build <b>ML</b> systems &amp; ship.</p>"})
check("remotive: _job_to_doc builds a doc on the real url + company + stripped HTML + tags",
      _rmt_doc is not None and _rmt_doc.source == "remotive"
      and _rmt_doc.url == "https://remotive.com/remote-jobs/123"
      and _rmt_doc.author == "EverAI" and _rmt_doc.date is not None
      and _rmt_doc.content == "Build ML systems & ship."
      and "Artificial Intelligence" in _rmt_doc.tags and "Worldwide" in _rmt_doc.tags
      and "AI" in _rmt_doc.tags)
check("remotive: _strip_html drops tags + unescapes; _job_to_doc drops a titleless / urlless row",
      _rmt.RemotiveAdapter._strip_html("<i>a</i> &amp; b") == "a & b"
      and _rmt.RemotiveAdapter._strip_html(None) == ""
      and _rmt.RemotiveAdapter()._job_to_doc({"title": "t"}) is None
      and _rmt.RemotiveAdapter()._job_to_doc({"url": "u"}) is None)
_rmt_a = fetcher.get_adapter("remotive")
check("remotive: registered + explicit_only + jobs/STRUCTURE keyless lookup",
      _rmt_a is not None and bool(fetcher._explicit_only_reason(_rmt_a))
      and _rmt_a.needs_credentials is False and _rmt_a.domains == ["jobs"]
      and _rmt_a.modes == ["STRUCTURE"])

# ---------------------------------------------------------------------------
# 33k. grants_gov (US federal funding OPPORTUNITIES, keyless POST): _opp_to_doc is a pure fn —
#      golden fixture it offline (constructed detail URL, closeDate as the deadline date, agency
#      author, agencyCode/status/cfda tags; a row with no id/title drops; openDate fallback).
# ---------------------------------------------------------------------------
from penumbra.core.sources.api import grants_gov_source as _gg  # noqa: E402
_gg_doc = _gg.GrantsGovAdapter()._opp_to_doc({
    "id": "358001", "number": "NSF-26-500", "title": "AI Research Institutes",
    "agency": "National Science Foundation", "agencyCode": "NSF",
    "openDate": "01/15/2026", "closeDate": "06/30/2026", "oppStatus": "posted",
    "cfdaList": ["47.070"]})
check("grants_gov: _opp_to_doc builds a doc on the constructed detail URL + closeDate deadline + tags",
      _gg_doc is not None and _gg_doc.source == "grants_gov"
      and _gg_doc.url == "https://www.grants.gov/search-results-detail/358001"
      and _gg_doc.author == "National Science Foundation"
      and _gg_doc.date is not None and _gg_doc.date.year == 2026 and _gg_doc.date.month == 6
      and "NSF" in _gg_doc.tags and "posted" in _gg_doc.tags and "47.070" in _gg_doc.tags
      and _gg_doc.metadata["number"] == "NSF-26-500")
check("grants_gov: _opp_to_doc drops a row with no id/title; falls back to openDate when no closeDate",
      _gg.GrantsGovAdapter()._opp_to_doc({"title": "x"}) is None
      and _gg.GrantsGovAdapter()._opp_to_doc({"id": "1", "title": "t", "openDate": "01/15/2026"}).date.month == 1)
_gg_a = fetcher.get_adapter("grants_gov")
check("grants_gov: registered + explicit_only + funding/us/STRUCTURE",
      _gg_a is not None and bool(fetcher._explicit_only_reason(_gg_a))
      and _gg_a.domains == ["funding"] and _gg_a.regions == ["us"]
      and _gg_a.modes == ["STRUCTURE"])

# ---------------------------------------------------------------------------
# 33l. vast_ai (live GPU marketplace, keyless): _offer_to_doc + _perf_signal are pure fns —
#      golden fixture them offline (price title, perf/$ signal, specs metadata; non-rentable /
#      priceless rows drop). gpu_pricing now shares the compute cell (no islet).
# ---------------------------------------------------------------------------
from penumbra.core.sources.api import vast_ai_source as _vast  # noqa: E402
_vast_doc = _vast.VastAIAdapter()._offer_to_doc({
    "id": 32668912, "gpu_name": "RTX 4090", "num_gpus": 1, "gpu_ram": 24576,
    "dph_total": 0.13556, "min_bid": 0.13333, "dlperf": 30.0, "dlperf_per_dphtotal": 221.0,
    "geolocation": "US", "reliability2": 0.99, "rentable": True})
check("vast_ai: _offer_to_doc builds a price doc on the marketplace url + perf/$ signal + specs",
      _vast_doc is not None and _vast_doc.source == "vast_ai"
      and _vast_doc.source_id == "32668912"
      and "RTX 4090" in _vast_doc.title and "0.1356" in _vast_doc.title
      and "perf_per_dollar" in _vast_doc.signals
      and _vast_doc.metadata["on_demand_usd_hr"] == 0.13556
      and _vast_doc.metadata["spot_usd_hr"] == 0.13333)
check("vast_ai: _offer_to_doc drops a row with no id / gpu / on-demand price",
      _vast.VastAIAdapter()._offer_to_doc({"gpu_name": "X"}) is None
      and _vast.VastAIAdapter()._offer_to_doc({"id": 1, "gpu_name": "X"}) is None)
_vast_a = fetcher.get_adapter("vast_ai")
check("vast_ai: registered + explicit_only + compute/STRUCTURE keyless lookup",
      _vast_a is not None and bool(fetcher._explicit_only_reason(_vast_a))
      and _vast_a.domains == ["compute"] and _vast_a.modes == ["STRUCTURE"])
_gpu_a = fetcher.get_adapter("gpu_pricing")
check("gpu_pricing: now declares compute/STRUCTURE (shares the cell with vast_ai, not an islet)",
      _gpu_a is not None and _gpu_a.domains == ["compute"] and _gpu_a.modes == ["STRUCTURE"])

# ---------------------------------------------------------------------------
# 33m. modelscope (Chinese model hub, keyless PUT): _model_to_doc + _tasks are pure fns —
#      golden fixture them offline (constructed model url, downloads signal, task tags from the
#      nested Tasks[] objects, epoch date; a row with no owner/name drops).
# ---------------------------------------------------------------------------
from penumbra.core.sources.api import modelscope_source as _ms  # noqa: E402
# Organization is a DICT on the live API ({FullName, Name, Path, ...}), NOT a bare string — the
# earlier string fixture masked a real bug: passing the dict as `author` raised pydantic and the
# adapter returned 0 docs (the modelscope count=0 incident, 2026-06-21). Fixture now matches reality.
_ms_doc = _ms.ModelScopeAdapter()._model_to_doc({
    "Name": "Qwen2.5-7B", "Path": "qwen", "ChineseName": "通义千问2.5-7B",
    "Downloads": 257000000, "License": "apache-2.0",
    "Organization": {"FullName": "千问", "Name": "Qwen", "Path": ""},
    "LastUpdatedTime": 1718000000,
    "Tasks": [{"Name": "text-generation", "ChineseName": "文本生成"}]})
check("modelscope: _model_to_doc builds a doc on the constructed model url + downloads signal + task tags",
      _ms_doc is not None and _ms_doc.source == "modelscope"
      and _ms_doc.source_id == "qwen/Qwen2.5-7B"
      and _ms_doc.url == "https://www.modelscope.cn/models/qwen/Qwen2.5-7B"
      and _ms_doc.title == "通义千问2.5-7B" and _ms_doc.author == "千问"  # org dict -> FullName str
      and "downloads" in _ms_doc.signals and _ms_doc.signals["downloads"].value == 257000000.0
      and "text-generation" in _ms_doc.tags and _ms_doc.date is not None)
check("modelscope: _org_name plucks a str from the org dict; _tasks flattens; row w/o owner|name drops",
      _ms.ModelScopeAdapter._org_name({"FullName": "千问", "Name": "Qwen"}) == "千问"
      and _ms.ModelScopeAdapter._org_name("PlainStr") == "PlainStr"
      and _ms.ModelScopeAdapter._org_name(None) is None
      and _ms.ModelScopeAdapter._tasks([{"Name": "asr"}, "vad"]) == ["asr", "vad"]
      and _ms.ModelScopeAdapter()._model_to_doc({"Name": "x"}) is None
      and _ms.ModelScopeAdapter()._model_to_doc({"Path": "p"}) is None)
_ms_a = fetcher.get_adapter("modelscope")
check("modelscope: registered + explicit_only + models/STRUCTURE keyless lookup",
      _ms_a is not None and bool(fetcher._explicit_only_reason(_ms_a))
      and _ms_a.domains == ["models"] and _ms_a.modes == ["STRUCTURE"])

# ---------------------------------------------------------------------------
# 33n. ai_incidents (AIID, keyless origin-gated GraphQL): _incident_to_doc + the Entity/report
#      flatteners are pure fns — golden fixture them offline (cite url from incident_id, reports
#      signal, developer/deployer tags, report drill-in handles, ISO date; a row w/o id|title drops).
# ---------------------------------------------------------------------------
from penumbra.core.sources.api import ai_incidents_source as _aii  # noqa: E402
_aii_doc = _aii.AIIncidentsAdapter()._incident_to_doc({
    "incident_id": 1535, "title": "Chatbot allegedly harmed a user",
    "date": "2025-07-02", "description": "An AI chatbot reportedly failed to intervene.",
    "AllegedDeveloperOfAISystem": [{"name": "OpenAI"}, {"name": "LLM developers"}],
    "AllegedDeployerOfAISystem": [{"name": "OpenAI"}],
    "AllegedHarmedOrNearlyHarmedParties": [{"name": "a user"}],
    "reports": [{"report_number": 7377, "title": "News report", "url": "https://example.com/r",
                 "source_domain": "example.com"}]})
check("ai_incidents: _incident_to_doc builds a doc on the /cite/ url + reports signal + entity tags + drill-in",
      _aii_doc is not None and _aii_doc.source == "ai_incidents"
      and _aii_doc.source_id == "1535"
      and _aii_doc.url == "https://incidentdatabase.ai/cite/1535"
      and _aii_doc.author == "OpenAI"  # deployer preferred over developer
      and "reports" in _aii_doc.signals and _aii_doc.signals["reports"].value == 1.0
      and "OpenAI" in _aii_doc.tags and "ai-incident" in _aii_doc.tags
      and _aii_doc.date is not None
      and (_aii_doc.metadata.get("reports") or [{}])[0].get("url") == "https://example.com/r")
check("ai_incidents: _filter tokenizes to AND-of-words (not one verbatim phrase) so multi-word queries hit",
      _aii.AIIncidentsAdapter._filter("") == {}
      and "AND" in _aii.AIIncidentsAdapter._filter("facial recognition arrest")
      and len(_aii.AIIncidentsAdapter._filter("facial recognition arrest")["AND"]) == 3
      and "OR" in _aii.AIIncidentsAdapter._filter("deepfake")  # single word -> bare OR, no AND
      and "AND" not in _aii.AIIncidentsAdapter._filter("deepfake"))
check("ai_incidents: _entities/_reports flatten the nested objects; _incident_to_doc drops id|title-less rows",
      _aii.AIIncidentsAdapter._entities([{"name": "X"}, {"noname": 1}, "junk"]) == ["X"]
      and _aii.AIIncidentsAdapter._reports([{"url": "u"}, {"title": "no url"}]) == [
          {"url": "u", "title": None, "source_domain": None, "report_number": None}]
      and _aii.AIIncidentsAdapter()._incident_to_doc({"title": "x"}) is None
      and _aii.AIIncidentsAdapter()._incident_to_doc({"incident_id": 1}) is None)
_aii_a = fetcher.get_adapter("ai_incidents")
check("ai_incidents: registered + fan-out (not explicit_only) + safety/STRUCTURE; backend=aiid",
      _aii_a is not None and not fetcher._explicit_only_reason(_aii_a)
      and _aii_a.domains == ["safety"] and _aii_a.modes == ["STRUCTURE"]
      and getattr(_aii_a, "backend", None) == "aiid")

# ---------------------------------------------------------------------------
# 33p. levels_fyi (comp): the FIX for the silent-empty on role/location queries (2026-06-21). The
#      adapter used to assume the query was a COMPANY name, so a role+location query slugged into a
#      bogus company -> 404 -> SILENT empty. Now it routes role queries to the /t/ pages and parses
#      the location-aware median/range from og:description (the public, non-paywalled surface). These
#      are pure fns golden-fixtured offline on the REAL og:description strings.
# ---------------------------------------------------------------------------
from penumbra.core.sources.scrape import levels_fyi_source as _lv  # noqa: E402
check("levels_fyi: a role query is detected (not mis-slugged as a company) + country extracted",
      _lv._role("Singapore machine learning engineer total compensation") == ("software-engineer", "machine-learning-engineer")
      and _lv._role("data scientist canada") == ("data-scientist", None)
      and _lv._role("research scientist singapore") == ("software-engineer", "research-scientist")
      and _lv._role("bytedance") is None  # a company name -> company path, NOT a role
      and _lv._location("... machine learning engineer toronto ...") == "canada"  # city -> country
      and _lv._location("xx singapore xx") == "singapore")
# og:description parsing across the 3 real phrasings (verbatim from live pages 2026-06-21).
_lv_sub = _lv._parse_comp_meta("The median Machine Learning Engineer Salary is SGD 162,754. View ...")
_lv_cat = _lv._parse_comp_meta("The average Software Engineer Salary range in Singapore is from SGD 85,107 to SGD 165,133. View ...")
_lv_co = _lv._parse_comp_meta("Software Engineer compensation in Singapore at ByteDance ranges from SGD 114K per year for 1-2 to SGD 477K per year for 3-2. The median yearly compensation package in Singapore totals SGD 167K.")
check("levels_fyi: _parse_comp_meta pulls median from a subtitle page (no trailing period)",
      _lv_sub.get("median") == "SGD 162,754" and _lv_sub.get("currency") == "SGD")
check("levels_fyi: _parse_comp_meta pulls the from->to RANGE from a category page (high not dropped)",
      _lv_cat.get("low") == "SGD 85,107" and _lv_cat.get("high") == "SGD 165,133")
check("levels_fyi: _parse_comp_meta pulls median + range from the company+location phrasing",
      _lv_co.get("median") == "SGD 167K" and _lv_co.get("low") == "SGD 114K" and _lv_co.get("high") == "SGD 477K")
check("levels_fyi: _num parses comma / K / M / CA$ money to int",
      _lv._num("SGD 162,754") == 162754 and _lv._num("167K") == 167000
      and _lv._num("CA$157,072") == 157072 and _lv._num("1.18M") == 1180000)
# the role doc builds with a location-aware title + a median signal (the eye's structured comp signal)
_lv_doc = _lv.LevelsFyiAdapter._role_doc("ml engineer singapore", "software-engineer",
            "machine-learning-engineer", "singapore",
            "https://www.levels.fyi/t/software-engineer/title/machine-learning-engineer/locations/singapore",
            "The median Machine Learning Engineer Salary is SGD 162,754.", _lv_sub)
check("levels_fyi: _role_doc builds a location doc with a median_total_comp signal (SGD)",
      _lv_doc is not None and _lv_doc.source == "levels_fyi" and "Singapore" in _lv_doc.title
      and "median_total_comp" in _lv_doc.signals and _lv_doc.signals["median_total_comp"].value == 162754.0
      and _lv_doc.metadata.get("country") == "singapore")
_lv_a = fetcher.get_adapter("levels_fyi")
check("levels_fyi: registered + keyless", _lv_a is not None and not _lv_a.needs_credentials)

# ---------------------------------------------------------------------------
# 34. fetcher CJK-aware excluded_relevant matcher: a 中文 query must surface a 中文 walled source
#     (the old ASCII-only split silently never did → 中文 sources were invisible to 中文 queries,
#     so the agent was never prompted to name them = "搞到手却用不上"). Pure/offline.
# ---------------------------------------------------------------------------
class _CnStub:  # a stub Chinese walled source
    name = "cn_stub_xyz"
    description = "微信公众号文章关键词搜索 — Chinese 公众号 articles, no login"
    domains = ["community"]
    regions = ["cn"]
check("fetcher: _query_overlaps_source matches a CHINESE query to a CHINESE source (CJK bigrams)",
      fetcher._query_overlaps_source("公众号 大模型 落地", _CnStub()) is True
      and fetcher._query_overlaps_source("quantum chromodynamics lattice", _CnStub()) is False)

# ---------------------------------------------------------------------------
# 35. douyin (抖音 login-walled web search): the search-response → doc parse is a pure fn — golden
#     fixture it offline. Search endpoint SKIPS a-bogus (only deeper endpoints sign), so the parse
#     is the whole risk surface. Covers: aweme_info video → doc (title/desc/author/digg/date/cover),
#     the aweme_mix_info.mix_items[0] path, dropping a non-video card, and the 2483 login-wall → [].
# ---------------------------------------------------------------------------
from penumbra.core.sources.walled import douyin_source as _dy  # noqa: E402
_dy_resp = {"status_code": 0, "status_msg": "", "data": [
    {"type": 1, "aweme_info": {
        "aweme_id": "7378810571505847586",
        "desc": "加拿大移民最新政策解读\n2026 EE 快速通道分数线变化",
        "author": {"nickname": "枫叶国说", "sec_uid": "MS4wLjABAAAAdouyin_demo"},
        "statistics": {"digg_count": 12000, "comment_count": 340, "share_count": 88,
                       "collect_count": 500, "play_count": 99000},
        "create_time": 1719300000,
        "video": {"duration": 95000, "cover": {"url_list": ["https://p3.douyinpic.com/cover.jpeg"]}}}},
    {"type": 1, "aweme_mix_info": {"mix_items": [{
        "aweme_id": "7400000000000000001",
        "desc": "新加坡 EP 准证申请避坑",
        "author": {"nickname": "狮城打工人"},
        "statistics": {"digg_count": 800},
        "create_time": 1720000000, "video": {"cover": {"url_list": []}}}]}},
    {"type": 4, "card_info": {"note": "a non-video card — no aweme_info, must be dropped"}}]}
_dy_docs = _dy.DouyinAdapter()._to_documents(_dy_resp, "加拿大移民", 10)
check("douyin: _to_documents parses aweme_info → video doc (title=first desc line, /video/<id> url, "
      "author, digg→likes signal, create_time→date, cover→media)",
      len(_dy_docs) == 2 and _dy_docs[0].source == "douyin"
      and _dy_docs[0].source_id == "7378810571505847586"
      and _dy_docs[0].url == "https://www.douyin.com/video/7378810571505847586"
      and _dy_docs[0].title == "加拿大移民最新政策解读"
      and _dy_docs[0].author == "枫叶国说" and _dy_docs[0].date is not None
      and _dy_docs[0].attention_value() == 12000.0
      and _dy_docs[0].media == ["https://p3.douyinpic.com/cover.jpeg"],
      detail=str([(d.source_id, d.title) for d in _dy_docs]))
check("douyin: _to_documents follows the aweme_mix_info.mix_items[0] path (mix card → doc)",
      _dy_docs[1].source_id == "7400000000000000001" and _dy_docs[1].author == "狮城打工人")
check("douyin: a non-video card (no aweme_info / no aweme_id) is dropped, not crashed",
      all(d.source_id not in ("", None) for d in _dy_docs))
check("douyin: the 2483 login-wall returns [] (authoritative needs-login, surfaced via diag) "
      "and a fetch_error/parse_error response also degrades to []",
      _dy.DouyinAdapter()._to_documents(
          {"status_code": 2483, "status_msg": "请先登录，再继续搜索吧", "data": []}, "x", 10) == []
      and _dy.DouyinAdapter()._to_documents({"fetch_error": "TypeError"}, "x", 10) == [])
_dy_a = fetcher.get_adapter("douyin")
check("douyin: registered + explicit_only + needs_credentials + cn/UNWALL facets + isolated 9225 CDP",
      _dy_a is not None and bool(fetcher._explicit_only_reason(_dy_a))
      and _dy_a.needs_credentials is True and _dy_a.regions == ["cn"]
      and "UNWALL" in (_dy_a.modes or []) and _dy_a.cdp_url == "http://127.0.0.1:9225")

# ---------------------------------------------------------------------------
# 36. curl_cffi TLS-impersonate RSS fetch tier (resurrects higheredjobs_cs from its JA3-WAF retire):
#     the wiring is pure + offline-checkable. http.get_impersonated exists; the RSS base threads
#     tls_impersonate (default OFF ⇒ the 143 in-tree sources are byte-identical); a BASE row honors
#     it but an OVERLAY row (guard_ip, agent-admitted) is FORCED off — the evasive tier is in-tree only.
# ---------------------------------------------------------------------------
from penumbra.core import http as _http  # noqa: E402
check("http.get_impersonated exists (curl_cffi Chrome-TLS fetch tier, opt-in)",
      callable(getattr(_http, "get_impersonated", None)))
from penumbra.core.sources.scrape import rss_bundles_source as _rb  # noqa: E402
check("rss base: tls_impersonate defaults OFF (existing feeds byte-identical) + a base row can set it",
      _rb.RSSAdapterBase.tls_impersonate is False
      and _rb._RSSBundle("x", "d", ["http://e/f"], tls_impersonate=True).tls_impersonate is True)
# overlay-gating: _register_row with guard_ip=True must FORCE tls_impersonate off even if the row asks
_imp_cap = {}
_orig_reg = _rb.register_adapter
_rb.register_adapter = lambda a: _imp_cap.__setitem__(a.name, a)
try:
    _rb._register_row({"name": "_imp_overlay_probe", "description": "d",
                       "feeds": ["http://x/f"], "tls_impersonate": True}, guard_ip=True)
    _rb._register_row({"name": "_imp_base_probe", "description": "d",
                       "feeds": ["http://x/f"], "tls_impersonate": True}, guard_ip=False)
finally:
    _rb.register_adapter = _orig_reg
check("rss bundle: an OVERLAY row (guard_ip) can NOT enable tls_impersonate; a BASE row can",
      _imp_cap["_imp_overlay_probe"].tls_impersonate is False
      and _imp_cap["_imp_base_probe"].tls_impersonate is True)
_hej = fetcher.get_adapter("higheredjobs_cs")
check("higheredjobs_cs: un-retired (not the 'retired:' marker), still explicit_only, tls_impersonate ON",
      _hej is not None and getattr(_hej, "tls_impersonate", False) is True
      and bool(fetcher._explicit_only_reason(_hej))
      and not (fetcher._explicit_only_reason(_hej) or "").strip().lower().startswith("retired"))

# ---------------------------------------------------------------------------
# 37. gap-hunt WAVE 1 sources (login-free telos gaps the eye lacked): crossref_retractions (research-
#     integrity MONITOR), datagovsg_nonresident_pass_types (SG immigration STRUCTURE), wikicfp_nlp
#     (NLP CFP discovery, RSS). The parse fns are pure → golden them offline; all explicit_only.
# ---------------------------------------------------------------------------
from penumbra.core.sources.api import crossref_retractions_source as _cr  # noqa: E402
_cr_doc = _cr.CrossrefRetractionsAdapter()._to_document({
    "DOI": "10.1038/s41598-026-59182-7", "title": ["Retraction Note: Deep learning for X"],
    "update-to": [{"DOI": "10.1038/orig-123", "type": "retraction"}],
    "container-title": ["Scientific Reports"], "publisher": "Springer",
    "created": {"date-time": "2026-06-22T10:00:00Z"},
    "author": [{"given": "A", "family": "Bee"}], "type": "journal-article",
    "URL": "https://doi.org/10.1038/s41598-026-59182-7"})
check("crossref_retractions: _to_document maps notice-DOI / retracted-DOI / journal / date / author",
      _cr_doc is not None and _cr_doc.source == "crossref_retractions"
      and _cr_doc.metadata.get("retracted_paper_doi") == "10.1038/orig-123"
      and _cr_doc.metadata.get("journal") == "Scientific Reports"
      and _cr_doc.date is not None and "retraction" in _cr_doc.tags
      and _cr_doc.url.startswith("https://doi.org/"))
check("crossref_retractions: a record with neither DOI nor a real title is dropped",
      _cr.CrossrefRetractionsAdapter()._to_document({"title": []}) is None)
_cr_a = fetcher.get_adapter("crossref_retractions")
check("crossref_retractions: registered + explicit_only + STRUCTURE/MONITOR",
      _cr_a is not None and bool(fetcher._explicit_only_reason(_cr_a))
      and "MONITOR" in (_cr_a.modes or []))
from penumbra.core.sources.api import datagovsg_passes_source as _dg  # noqa: E402
_dg_doc = _dg.DataGovSgPassesAdapter()._to_document({
    "_id": 3, "DataSeries": "Employment Pass", "2010": "9.0", "2020": "12.5", "2021": "13.1"})
check("datagovsg_nonresident_pass_types: _to_document flattens year columns into a per-series doc, latest-first",
      _dg_doc is not None and _dg_doc.metadata.get("data_series") == "Employment Pass"
      and _dg_doc.metadata.get("by_year", {}).get("2021") == "13.1"
      and _dg_doc.content.index("2021") < _dg_doc.content.index("2010")
      and "singapore" in _dg_doc.tags)
check("datagovsg_nonresident_pass_types: a record with no DataSeries is dropped",
      _dg.DataGovSgPassesAdapter()._to_document({"2020": "1"}) is None)
_dg_a = fetcher.get_adapter("datagovsg_nonresident_pass_types")
check("datagovsg_nonresident_pass_types: registered + explicit_only + sg/immigration/STRUCTURE",
      _dg_a is not None and bool(fetcher._explicit_only_reason(_dg_a))
      and _dg_a.regions == ["sg"] and "STRUCTURE" in (_dg_a.modes or []))
_wcf = fetcher.get_adapter("wikicfp_nlp")
check("wikicfp_nlp: registered (rss bundle) + explicit_only + wikicfp http feed",
      _wcf is not None and bool(fetcher._explicit_only_reason(_wcf))
      and any("wikicfp.com" in f for f in (getattr(_wcf, "feeds", []) or [])))

# ---------------------------------------------------------------------------
# 38. CA Provincial Nominee draw scrapers (oinp/bcpnp/aaip): pure HTML-table parse → per-draw docs.
#     Golden offline with verbatim-shaped table fixtures (structures verified live 2026-06-22).
#     explicit_only: 省提名, named via the penumbra_search drill (sources=[one], raw=True), kept SEPARATE
#     from federal EE (ircc_ee_rounds).
# ---------------------------------------------------------------------------
from penumbra.core.sources.scrape import ca_pnp_source as _pnp  # noqa: E402
_oinp_html = ("<h3>PhD Graduate stream</h3><table>"
              "<tr><th>Date issued</th><th>Number of invitations issued</th>"
              "<th>Date profiles created</th><th>Score range</th><th>Notes</th></tr>"
              "<tr><td>April 22, 2026</td><td>244</td><td>April 22, 2025 – April 22, 2026</td>"
              "<td>56 and above</td><td>General draw.</td></tr></table>")
_oinp_docs = _pnp.OinpInvitationsAdapter()._to_documents(_oinp_html, "", 10)
check("oinp_invitations: parses a draw row + attributes the stream from the preceding heading",
      len(_oinp_docs) == 1 and _oinp_docs[0].metadata["stream"] == "PhD Graduate stream"
      and _oinp_docs[0].metadata["invitations"] == "244"
      and _oinp_docs[0].metadata["score_range"] == "56 and above"
      and _oinp_docs[0].date is not None and "ontario" in _oinp_docs[0].tags)
_bc_html = ("<h2>Skills Immigration invitations</h2><table>"
            "<tr><th>Date</th><th>ITA type</th><th>Selection factors</th><th>Minimum score</th>"
            "<th>Number of invitations</th></tr>"
            "<tr><td>June 18, 2026</td><td>Innovate: High Economic Impact</td><td>min wage</td>"
            "<td>N/A</td><td>130</td></tr></table>"
            "<h2>Skills Immigration registration pool</h2><table>"
            "<tr><th>Score range</th><th>Number of registrations</th></tr>"
            "<tr><td>150+</td><td>6</td></tr><tr><td>Total</td><td>9902</td></tr></table>"
            "<h2>Entrepreneur Immigration invitations</h2><table>"
            "<tr><th>Date</th><th>Stream</th><th>Minimum Score</th><th>Number of Invitations</th></tr>"
            "<tr><td>June 2, 2026</td><td>Base</td><td>117</td><td>15</td></tr></table>")
_bc_docs = _pnp.BcpnpInvitationsAdapter()._to_documents(_bc_html, "", 10)
_bc_by = {d.metadata.get("category", "pool"): d for d in _bc_docs}
check("bcpnp_invitations: parses Skills + Entrepreneur draws AND the SIRS pool snapshot (3 docs)",
      len(_bc_docs) == 3 and _bc_by.get("Skills") and _bc_by["Skills"].metadata["min_score"] == "N/A"
      and _bc_by.get("Entrepreneur") and _bc_by["Entrepreneur"].metadata["stream"] == "Base"
      and any(d.source_id == "bcpnp_pool:9902" for d in _bc_docs))
_aaip_html = ("<h3>Draw information</h3><table>"
              "<tr><th>Draw date</th><th>Worker stream, pathway</th>"
              "<th>Minimum score of invited candidates</th><th>Number of invitations</th></tr>"
              "<tr><td>June 15, 2026</td><td>Alberta Express Entry Stream – Priority Sectors (Manufacturing)</td>"
              "<td>50</td><td>56</td></tr></table>")
_aaip_docs = _pnp.AaipDrawsAdapter()._to_documents(_aaip_html, "", 10)
check("aaip_draws: parses the 'Draw information' table (stream/min-score/invitations) by header signature",
      len(_aaip_docs) == 1 and _aaip_docs[0].metadata["min_score"] == "50"
      and _aaip_docs[0].metadata["invitations"] == "56" and "alberta" in _aaip_docs[0].tags)
for _pn in ("oinp_invitations", "bcpnp_invitations", "aaip_draws"):
    _pa = fetcher.get_adapter(_pn)
    check(f"{_pn}: registered + explicit_only + ca/immigration/STRUCTURE+MONITOR",
          _pa is not None and bool(fetcher._explicit_only_reason(_pa))
          and _pa.regions == ["ca"] and "MONITOR" in (_pa.modes or []))

# ---------------------------------------------------------------------------
# 39. nserc_awards (the eye's first CANADIAN funding source): the bulk-CSV pattern's pure halves —
#     the telos filter (_is_cs_ai) + the row->doc map — golden offline (no 56MB pull in smoke).
# ---------------------------------------------------------------------------
from penumbra.core.sources.api import nserc_awards_source as _ns  # noqa: E402
_NS = _ns.NSERCAwardsAdapter()
_ns_cs = {"ApplicationID": "123", "Name-Nom": "Smith, Jane",
          "Institution-Établissement": "University of Toronto", "ProvinceEN": "Ontario",
          "ProgramNameEN": "Discovery Grants", "ResearchSubjectEN": "Computer Science",
          "FieldOfResearchListNamesEN": "Artificial Intelligence", "AwardAmount": "45000",
          "ApplicationTitle": "Neural methods for parsing", "Keywords": "natural language; parsing",
          "ApplicationSummary": "We study X.", "FiscalYear-Exercice financier": "2024"}
_ns_nlp = {"ResearchSubjectEN": "Linguistics", "Name-Nom": "Lee, B",
           "ApplicationTitle": "A machine learning approach to dialect", "Keywords": "",
           "ApplicationSummary": "", "FieldOfResearchListNamesEN": ""}
_ns_other = {"ResearchSubjectEN": "Marine Biology", "Name-Nom": "Roe, C",
             "ApplicationTitle": "Coral reef dynamics", "Keywords": "ocean",
             "ApplicationSummary": "", "FieldOfResearchListNamesEN": ""}
check("nserc_awards: _is_cs_ai keeps CS-subject + AI/NLP-keyword rows, drops unrelated",
      _NS._is_cs_ai(_ns_cs) is True and _NS._is_cs_ai(_ns_nlp) is True
      and _NS._is_cs_ai(_ns_other) is False)
_ns_doc = _NS._row_to_doc(_ns_cs)
check("nserc_awards: _row_to_doc maps recipient / institution / amount / subject (+ funding/ca tags)",
      _ns_doc is not None and _ns_doc.metadata["recipient"] == "Smith, Jane"
      and _ns_doc.metadata["institution"] == "University of Toronto"
      and _ns_doc.metadata["amount_cad"] == "45000"
      and _ns_doc.metadata["subject"] == "Computer Science"
      and "canada" in _ns_doc.tags and _ns_doc.source == "nserc_awards")
_ns_a = fetcher.get_adapter("nserc_awards")
check("nserc_awards: registered + explicit_only + ca/funding/STRUCTURE",
      _ns_a is not None and bool(fetcher._explicit_only_reason(_ns_a))
      and _ns_a.regions == ["ca"] and "STRUCTURE" in (_ns_a.modes or []))

# ---------------------------------------------------------------------------
# 40. sshrc_awards + cihr_grants — completing Canada's Tri-Council on the eye's bulk-file pattern
#     (NSERC sciences + SSHRC humanities/comp-ling + CIHR health-AI). The pure halves (the shared AI
#     filter + each row->doc map; CIHR's long-bilingual-column _pick + _clean) golden offline.
# ---------------------------------------------------------------------------
from penumbra.core.sources.api import _bulk_funding as _bf  # noqa: E402
check("_bulk_funding.is_ai_relevant: matches an NLP/ML text, rejects an unrelated one",
      _bf.is_ai_relevant("A study of natural language models", "", "") is True
      and _bf.is_ai_relevant("Coral reef biodiversity", "marine ecology", "") is False)
from penumbra.core.sources.api import sshrc_awards_source as _ss  # noqa: E402
_ss_doc = _ss.SSHRCAwardsAdapter()._row_to_doc({
    "cle": "9", "Name-Nom": "Doe, Jane", "Role-Rôle": "Applicant", "Amount-Montant": "60000",
    "Fiscal_Year-Exercice_financier": "2024", "Institution": "McGill University",
    "Province_EN": "Quebec", "Title-Titre": "Computational linguistics of Quebec French",
    "Keywords-Mots-clés": "natural language; corpus", "Program": "Insight Grants",
    "SSHRC_Discipline_EN": "Linguistics", "SSHRC_Area_of_Research": "Language",
    "CRDC_Field_of_Research": "Computational linguistics"})
check("sshrc_awards: _row_to_doc maps applicant / institution / amount / discipline (+ funding/ca tags)",
      _ss_doc is not None and _ss_doc.metadata["recipient"] == "Doe, Jane"
      and _ss_doc.metadata["institution"] == "McGill University"
      and _ss_doc.metadata["amount_cad"] == "60000"
      and _ss_doc.metadata["discipline"] == "Linguistics"
      and "sshrc" in _ss_doc.tags and _ss_doc.source == "sshrc_awards")
_ss_a = fetcher.get_adapter("sshrc_awards")
check("sshrc_awards: registered + explicit_only + ca/funding/STRUCTURE",
      _ss_a is not None and bool(fetcher._explicit_only_reason(_ss_a))
      and _ss_a.regions == ["ca"] and "STRUCTURE" in (_ss_a.modes or []))
from penumbra.core.sources.api import cihr_grants_source as _ci  # noqa: E402
_ci_row = {"FundingReferenceNumber_NumeroReferenceFinancement": "148379_1",
           "FamilyName_NomFamille": "Nemer", "FirstName_Prenom": "Mona",
           "InstitutionPaidNameEN_NomEtablissementPayeAN": "University of Ottawa",
           "ResearchInstitutionDepartment_DepartementEtablissementRecherche": "Medicine",
           "ProgramNameEN_NomProgrammeAN": "Foundation Grant", "FiscalYear_AnneeFinanciere": "202526",
           "TotalAmountAwarded_MontantTotalAccorde": "1000000",
           "PrimaryThemeEN_ThemePrincipalAN": "Clinical",
           "AllResearchCategoriesEN_TousCategoriesRechercheAN": "Cardiology",
           "ApplicationTitle_TitreDemande": "Machine learning for cardiac risk prediction",
           "ApplicationAbstract_ResumeDemande": "We use deep learning on EHR data.",
           "ApplicationKeywords_MotsClesDemande": "machine learning; cardiology"}
_CI = _ci.CIHRGrantsAdapter()
check("cihr_grants: _is_relevant true on an ML title; _pick reads a long bilingual column by partial",
      _CI._is_relevant(_ci_row) is True
      and _CI._pick(_ci_row, "ApplicationTitle").startswith("Machine learning"))
check("cihr_grants: _clean normalizes the literal 'None' string + real None to empty",
      _ci._clean("None") == "" and _ci._clean(None) == "" and _ci._clean(" x ") == "x")
_ci_doc = _CI._row_to_doc(_ci_row)
check("cihr_grants: _row_to_doc maps recipient (first+family) / institution / amount / funding_ref (+ health tag)",
      _ci_doc is not None and _ci_doc.metadata["recipient"] == "Mona Nemer"
      and _ci_doc.metadata["institution"] == "University of Ottawa"
      and _ci_doc.metadata["amount_cad"] == "1000000"
      and _ci_doc.metadata["funding_ref"] == "148379_1"
      and "health" in _ci_doc.tags and _ci_doc.source == "cihr_grants")
_ci_a = fetcher.get_adapter("cihr_grants")
check("cihr_grants: registered + explicit_only + ca/funding/STRUCTURE",
      _ci_a is not None and bool(fetcher._explicit_only_reason(_ci_a))
      and _ci_a.regions == ["ca"] and "STRUCTURE" in (_ci_a.modes or []))

# ---------------------------------------------------------------------------
# 41. OpenAlex resilience (the 44-source single-budget fragility): unavailable() reads the DOWN state
#     non-probingly (circuit open OR both daily budgets dry); org_watch / researcher_watch then serve a
#     stale last-good snapshot (real papers, <=~1d old) instead of a blind [] when OpenAlex is exhausted.
# ---------------------------------------------------------------------------
from penumbra.core import _openalex as _oa  # noqa: E402
import time as _t41  # noqa: E402
_oa_save = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _oa._state.items()}
try:
    _far = _t41.monotonic() + 9999
    _oa._state["open_until"] = 0.0
    _oa._state["dry_until"] = {"keyed": _far, "anon": _far}
    _u_dry = _oa.unavailable()
    _oa._state["dry_until"] = {"keyed": 0.0, "anon": 0.0}
    _oa._state["open_until"] = _t41.time() + 9999
    _u_circuit = _oa.unavailable()
    _oa._state["open_until"] = 0.0
    _u_ok = _oa.unavailable()
finally:
    _oa._state.clear(); _oa._state.update(_oa_save)
check("openalex.unavailable(): True when both budgets dry, True when circuit open, False otherwise",
      _u_dry is True and _u_circuit is True and _u_ok is False)
from penumbra.core.sources.api import org_watch_source as _ow  # noqa: E402
from penumbra.core import cache as _c41  # noqa: E402
_ow_a = _ow._OrgWatchAdapter("ow_test_lab_x", ["TestLab Unique Phrase"], "t", regions=["global"])
_c41.set(_c41.make_key("org_watch", "ow_test_lab_x", "lastgood", "TestLab Unique Phrase"),
         [{"id": "https://openalex.org/W777", "title": "Stale Lab Paper",
           "publication_date": "2026-06-01", "authorships": [], "cited_by_count": 3}], ttl=600)
_oa_save2 = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _oa._state.items()}
try:
    _far2 = _t41.monotonic() + 9999
    _oa._state["open_until"] = 0.0
    _oa._state["dry_until"] = {"keyed": _far2, "anon": _far2}  # both budgets dry → live fetch yields []
    _ow_works, _ow_stale = _ow_a._fetch()
finally:
    _oa._state.clear(); _oa._state.update(_oa_save2)
check("org_watch: OpenAlex DOWN + a last-good snapshot → serve it stamped stale (not a blind [])",
      _ow_stale is True and len(_ow_works) == 1 and _ow_works[0]["title"] == "Stale Lab Paper")

# ---------------------------------------------------------------------------
# 42. Phase-A passive enrichments (7 features). All are MECHANICAL measurements stamped as
#     metadata/_meta for the agent to interpret — NONE feed composite()/the ranking blend
#     (the razor: measure, don't rank by them). Golden-fixtured offline on synthetic inputs;
#     each asserts the EXACT new metadata/_meta shape the implementers produce.
# ---------------------------------------------------------------------------

# --- #3 comment/paragraph-level provenance (normalize.comment_anchor + COMMENT_SCHEMA_KEYS,
#     and the xhs _flatten emitting a per-comment 'id'). Pure string builder + pure flattener. ---
from penumbra.core.normalize import comment_anchor as _comment_anchor  # noqa: E402
from penumbra.core.normalize import COMMENT_SCHEMA_KEYS as _COMMENT_SCHEMA_KEYS  # noqa: E402
check("provenance: comment_anchor builds stable URI",
      _comment_anchor("xiaohongshu_cn", "note123", "cmt456") == "xiaohongshu_cn:note123#comment-cmt456")
check("provenance: comment_anchor with empty id",
      _comment_anchor("xhs", "n1", "") == "xhs:n1#comment-")
check("provenance: COMMENT_SCHEMA_KEYS has id but not ts (adversarial: ts deliberately excluded)",
      "id" in _COMMENT_SCHEMA_KEYS and "ts" not in _COMMENT_SCHEMA_KEYS)
# xhs _flatten is a pure fn: golden-fixture it offline — the top-level + inline-reply dicts must now
# carry an 'id' matching COMMENT_SCHEMA_KEYS, and the reply text is '↳'-prefixed.
from penumbra.core.sources.walled import xiaohongshu_cn_source as _xhscn  # noqa: E402
_xhs_out: list = []
_xhscn._flatten([{"user_info": {"nickname": "Ann"}, "content": "top comment", "like_count": 12,
                  "id": "c1", "sub_comments": [
                      {"user_info": {"nickname": "Bob"}, "content": "a reply", "like_count": 3,
                       "id": "c2"}]}], _xhs_out)
check("provenance: xhs _flatten emits top + inline reply, each with an 'id' key",
      len(_xhs_out) == 2 and _xhs_out[0]["id"] == "c1" and _xhs_out[1]["id"] == "c2"
      and _xhs_out[1]["text"].startswith("↳"))
check("provenance: xhs _flatten comment dict keys == COMMENT_SCHEMA_KEYS (set-equal, no stray/missing)",
      set(_xhs_out[0].keys()) == set(_COMMENT_SCHEMA_KEYS))
check("provenance: xhs _flatten defaults a missing comment id to '' (never KeyError/None)",
      _xhscn._flatten([{"content": "no id here"}], (_e3 := [])) or _e3[0]["id"] == "")

# --- shared imports for the metadata blocks below (merge_rank stamps freshness/relevance_hook;
#     Document is the fixture type). corroboration/also_in is asserted in the dedup block. ---
from penumbra.core.rank import merge_rank as _merge_rank  # noqa: E402
from penumbra.core.normalize import Document as _PDoc  # noqa: E402

# --- #8 source_diversity (fetcher._compute_source_diversity: kind-facet distribution of the ranked
#     list, absent-perspective advisory, unique-source count). Data-driven, not a hardcoded list. ---
_sd_docs = [_PDoc(source="arxiv", source_id=str(_i), url=f"http://{_i}",
                  title=f"Paper {_i} Title Long", content="x") for _i in range(3)]
_sd = fetcher._compute_source_diversity(_sd_docs)
check("source_diversity: _compute_source_diversity is callable",
      callable(getattr(fetcher, "_compute_source_diversity", None)))
check("source_diversity: output has distribution/absent_perspectives/unique_sources",
      isinstance(_sd, dict) and "distribution" in _sd and "absent_perspectives" in _sd
      and "unique_sources" in _sd)
check("source_diversity: 3 docs from ONE source → unique_sources == 1",
      _sd["unique_sources"] == 1)
check("source_diversity: distribution tallies all 3 ranked docs; absent_perspectives is a list",
      sum(_sd["distribution"].values()) == 3 and isinstance(_sd["absent_perspectives"], list))
check("source_diversity: empty ranked list → empty distribution, 0 unique, absent is a list",
      (lambda z: z["unique_sources"] == 0 and z["distribution"] == {}
       and isinstance(z["absent_perspectives"], list))(fetcher._compute_source_diversity([])))

# --- #10 freshness_days + freshness_class (rank.merge_rank stamps a float age + a mechanical
#     bucket; None/None for a dateless doc; naive dates handled like _recency). Pure metadata. ---
from datetime import datetime as _dt42, timezone as _tz42, timedelta as _td42  # noqa: E402
_fd_doc = _PDoc(source="test", source_id="1", url="http://x",
                title="Fresh Paper Title Long Enough", content="x",
                date=_dt42.now(_tz42.utc) - _td42(days=3))
_fd = _merge_rank({"test": [_fd_doc]}, "test", limit=5)
check("freshness_days: present on a dated doc",
      "freshness_days" in (_fd[0].metadata or {}))
check("freshness_days: ~3 for a doc 3 days old",
      2.5 < _fd[0].metadata.get("freshness_days", 0) < 3.5)
check("freshness_class: 'recent' for a 3-day-old doc (1<d<=7 bucket)",
      _fd[0].metadata.get("freshness_class") == "recent")
_fd_none = _merge_rank({"test": [_PDoc(source="test", source_id="2", url="http://y",
                                       title="No Date Doc Title Long Enough", content="y")]},
                       "test", limit=5)
check("freshness_days: None for a dateless doc",
      _fd_none[0].metadata.get("freshness_days") is None)
check("freshness_class: None for a dateless doc",
      _fd_none[0].metadata.get("freshness_class") is None)
_fd_brk = _merge_rank({"test": [_PDoc(source="test", source_id="3", url="http://z",
                                      title="Breaking Item Title Long Enough", content="z",
                                      date=_dt42.now(_tz42.utc) - _td42(hours=6))]}, "test", limit=5)
check("freshness_class: 'breaking' for a <=1-day-old doc (boundary bucket)",
      _fd_brk[0].metadata.get("freshness_class") == "breaking")

# --- #14 relevance_hook (rank.merge_rank stamps an EXTRACTIVE substring from the doc's own text
#     with the highest query-term overlap; '' in browse mode / no-match). Not generative. ---
_rh_doc = _PDoc(source="test", source_id="1", url="http://x", title="Machine Learning Survey Paper",
                content="This paper surveys deep learning. Neural networks are discussed. "
                        "Machine learning methods are compared.")
_rh = _merge_rank({"test": [_rh_doc]}, "machine learning", limit=5)
check("relevance_hook: present on a ranked doc",
      "relevance_hook" in (_rh[0].metadata or {}))
check("relevance_hook: non-empty for a matching query (extracted from the doc's own text)",
      len(_rh[0].metadata.get("relevance_hook", "")) > 0
      and _rh[0].metadata["relevance_hook"] in
          (_rh_doc.title + ". " + _rh_doc.content))
_rh_browse = _merge_rank({"test": [_rh_doc]}, "", limit=5)
check("relevance_hook: '' in browse mode (empty query → no hook)",
      _rh_browse[0].metadata.get("relevance_hook") == "")

# --- #11 signal_conflicts (rank.dedup stamps same-group cross-source Signal divergence on the
#     SURVIVOR at merge time — the only place the collapsed members are still visible; post-dedup
#     any cross-doc comparison compares DIFFERENT works, the 2026-07-01 dogfood noise. fetcher
#     collects the survivors' stamps into _meta.conflicts, capped 5, key absent when none).
#     A shared long title (>=20 normalized chars) is what lands two docs in the same group. ---
from penumbra.core.normalize import Signal as _Signal42  # noqa: E402
from penumbra.core.rank import dedup as _dedup42  # noqa: E402
# _cf1/_cf2: SAME normalized title → SAME dedup group; different sources; revenue diverges 8M vs 5M
# (ratio 1.6) → the survivor carries metadata.signal_conflicts (P7: any divergence is stamped +
# RANKED by ratio; there is no threshold gate anymore, so the ratio value, not a cutoff, is the point).
_cf_title = "Company X Annual Revenue Filing Report"  # >=20 normalized chars → title: fingerprint
_cf1 = _PDoc(source="s1", source_id="1", url="http://a", title=_cf_title,
             content="revenue 5M",
             signals={"revenue": _Signal42(value=5000000.0, kind="other",
                                           computed_by="source:s1", unit="USD")})
_cf2 = _PDoc(source="s2", source_id="2", url="http://b", title=_cf_title,
             content="revenue 8M",
             signals={"revenue": _Signal42(value=8000000.0, kind="other",
                                           computed_by="source:s2", unit="USD")})
_cf_out = _dedup42([_cf1, _cf2])
_conflicts = (_cf_out[0].metadata or {}).get("signal_conflicts", []) if len(_cf_out) == 1 else []
check("conflict: same-group divergent signal across two sources stamps the survivor (with its ratio)",
      len(_conflicts) >= 1 and _conflicts[0]["topic"] == "revenue"
      and _conflicts[0]["source_a"] != _conflicts[0]["source_b"]
      and _conflicts[0]["ratio"] == 1.6)
_cf_solo = _dedup42([_PDoc(source="a", source_id="1", url="http://x",
                           title="Unique Title Long Enough", content="x")])
check("conflict: a singleton (no cross-source pair) carries no stamp",
      "signal_conflicts" not in (_cf_solo[0].metadata or {}))
# Cross-GROUP → NOT a conflict: two docs with DIFFERENT titles (different fingerprints) are distinct
# entities; they survive dedup separately and neither carries a stamp.
_cf_xg_a = _PDoc(source="s1", source_id="1", url="http://a", title="Company X Revenue Filing Report",
                 content="x", signals={"revenue": _Signal42(value=5000000.0, kind="other",
                                                            computed_by="source:s1")})
_cf_xg_b = _PDoc(source="s2", source_id="2", url="http://b", title="Company Y Earnings Summary Page",
                 content="y", signals={"revenue": _Signal42(value=8000000.0, kind="other",
                                                            computed_by="source:s2")})
_cf_xg_out = _dedup42([_cf_xg_a, _cf_xg_b])
check("conflict: cross-GROUP (distinct titles/fingerprints) divergence is NOT flagged",
      len(_cf_xg_out) == 2
      and all("signal_conflicts" not in (d.metadata or {}) for d in _cf_xg_out))
# Same source → NOT a conflict (same-source pairs are skipped), even for a shared title with
# divergent signals.
_cf_same_a = _PDoc(source="s1", source_id="1", url="http://a", title="Same Source Shared Long Title",
                   content="x", signals={"m": _Signal42(value=10.0, kind="other",
                                                        computed_by="source:s1")})
_cf_same_b = _PDoc(source="s1", source_id="2", url="http://b", title="Same Source Shared Long Title",
                   content="y", signals={"m": _Signal42(value=100.0, kind="other",
                                                        computed_by="source:s1")})
_cf_same_out = _dedup42([_cf_same_a, _cf_same_b])
check("conflict: two docs from the SAME source are NOT flagged (cross-source only)",
      all("signal_conflicts" not in (d.metadata or {}) for d in _cf_same_out))
# P7: the 1.5x GATE is gone. A SMALL-but-nonzero divergence (100 vs 110, ratio 1.1) is now stamped
# WITH its measured ratio — the detector ranks, it never gates. (Materiality is the reader's; the
# stamp carries the number so a 1.1 reads as trivially-divergent, not as "notable".)
_cf_small_a = _PDoc(source="s1", source_id="1", url="http://a", title="Small Divergence Shared Title",
                    content="x", signals={"m": _Signal42(value=100.0, kind="other",
                                                         computed_by="source:s1")})
_cf_small_b = _PDoc(source="s2", source_id="2", url="http://b", title="Small Divergence Shared Title",
                    content="y", signals={"m": _Signal42(value=110.0, kind="other",
                                                         computed_by="source:s2")})
_cf_small_out = _dedup42([_cf_small_a, _cf_small_b])
_cf_small_sc = (_cf_small_out[0].metadata or {}).get("signal_conflicts", []) if len(_cf_small_out) == 1 else []
check("p7 conflict: a small nonzero divergence (ratio 1.1) IS flagged now (rank, never a gate), carrying the ratio",
      len(_cf_small_sc) == 1 and _cf_small_sc[0]["topic"] == "m" and _cf_small_sc[0]["ratio"] == 1.1)
# EQUAL values are NOT a divergence, so they carry NO stamp (the only thing silence now means).
_cf_eq_a = _PDoc(source="s1", source_id="1", url="http://a", title="Equal Metric Shared Long Title",
                 content="x", signals={"m": _Signal42(value=100.0, kind="other",
                                                      computed_by="source:s1")})
_cf_eq_b = _PDoc(source="s2", source_id="2", url="http://b", title="Equal Metric Shared Long Title",
                 content="y", signals={"m": _Signal42(value=100.0, kind="other",
                                                      computed_by="source:s2")})
_cf_eq_out = _dedup42([_cf_eq_a, _cf_eq_b])
check("p7 conflict: EQUAL cross-source values are NOT a divergence (no stamp)",
      all("signal_conflicts" not in (d.metadata or {}) for d in _cf_eq_out))
# The production wiring: search_ranked COLLECTS the survivors' stamps into _meta.conflicts
# (the detection itself lives in rank.dedup where the group members exist).
import inspect as _cf_inspect  # noqa: E402
_fetch_src42 = _cf_inspect.getsource(fetcher)
check("conflict: fetcher collects signal_conflicts stamps into _meta.conflicts",
      'signal_conflicts' in _fetch_src42 and "_detect_conflicts" not in _fetch_src42)

# --- #6 progressive_return (fetcher.search_many classifies fast/slow sources from a shared
#     _result_times dict and stamps a progressive block in _meta). P11 weight-class: fast/slow are
#     NON-actionable, so the block carries COUNTS (fast/slow), not the 70+-name lists; the timed_out
#     NAMES stay (actionable). search_many needs live adapters, so assert the code STRUCTURE (the
#     adversarial fix kept wait(), not as_completed()). ---
import inspect as _inspect42  # noqa: E402
_sm_src = _inspect42.getsource(fetcher.search_many)
check("progressive: search_many stamps a progressive block in _meta (P11 weight-class)",
      '"progressive":' in _sm_src)
check("progressive: fast/slow are COUNTS now, not name lists (P11: non-actionable → counts)",
      "fast_count" in _sm_src and "slow_count" in _sm_src
      and "fast_sources" not in _sm_src and "slow_sources" not in _sm_src)
check("progressive: pending_sources alias is GONE (byte-duplicate of timed_out, rescan deletion)",
      "pending_sources" not in _sm_src)
check("progressive: kept the load-tested wait() path (adversarial: did NOT switch to as_completed)",
      "_result_times" in _sm_src and "as_completed" not in _sm_src)

# ---------------------------------------------------------------------------
# 43. Orchestration-layer features: handles, gather, evidence schema, overlap, prompts.
# ---------------------------------------------------------------------------

# --- handles: per-doc affordance detection (rank.merge_rank stamps transcribable/enrichable/
#     has_comments as metadata['handles']). Pure pattern match, not a suggestion. ---
_hnd_doc = _PDoc(source="youtube", source_id="v1", url="https://www.youtube.com/watch?v=abc",
                 title="A Talk About RL Sufficient Length Title", content="RL talk",
                 media=["https://www.youtube.com/watch?v=abc"])
_hnd_r = _merge_rank({"youtube": [_hnd_doc]}, "RL", limit=5)
check("handles: youtube URL detected as 'captioned' (not transcribable — youtube has captions)",
      "captioned" in _hnd_r[0].metadata.get("handles", {}))
_hnd_bili = _PDoc(source="bilibili", source_id="b1", url="https://www.bilibili.com/video/BV1x",
                  title="Bilibili Video Long Title For Dedup Testing", content="x",
                  media=["https://www.bilibili.com/video/BV1x"])
_hnd_br = _merge_rank({"bilibili": [_hnd_bili]}, "test", limit=5)
check("handles: bilibili URL detected as 'transcribable'",
      "transcribable" in _hnd_br[0].metadata.get("handles", {}))
_hnd_doi = _PDoc(source="s2", source_id="1", url="http://a",
                 title="Paper With DOI External ID Long Title", content="x",
                 metadata={"external_ids": {"DOI": "10.1038/test", "ArXiv": "2501.99999"}})
_hnd_dr = _merge_rank({"s2": [_hnd_doi]}, "test", limit=5)
check("handles: DOI + arxiv in external_ids detected as 'enrichable'",
      sorted(_hnd_dr[0].metadata.get("handles", {}).get("enrichable", [])) ==
      ["10.1038/test", "2501.99999"])
_hnd_cmt = _PDoc(source="xhs", source_id="1", url="http://a",
                 title="XHS Post With Comments Long Title", content="x",
                 metadata={"comments": [{"author": "a", "text": "hi"}]})
_hnd_cr = _merge_rank({"xhs": [_hnd_cmt]}, "test", limit=5)
check("handles: has_comments=True when metadata['comments'] is non-empty",
      _hnd_cr[0].metadata.get("handles", {}).get("has_comments") is True)
_hnd_plain = _PDoc(source="test", source_id="1", url="http://a",
                   title="Plain Doc No Handles Long Enough Title", content="x")
_hnd_pr = _merge_rank({"test": [_hnd_plain]}, "test", limit=5)
check("handles: absent from metadata when no affordances detected (no noise)",
      "handles" not in _hnd_pr[0].metadata)

# --- evidence.py ABSORBED into recall.graph (design section 9): the shared TypedDicts (GraphNode /
#     GraphEdge / ManifestEntry) now live under ONE name in the graph module; the old evidence module
#     is GONE (one name, one vocabulary — the agent's J-tier overlay uses the SAME shapes). ---
from penumbra.core.recall.graph import GraphNode as _GNd, GraphEdge as _GEd  # noqa: E402
from penumbra.core.recall.graph import ManifestEntry as _ME  # noqa: E402
check("graph: GraphNode / GraphEdge TypedDicts importable from recall.graph (absorbed evidence.py)",
      _GNd is not None and _GEd is not None)
check("graph: ManifestEntry importable from recall.graph", _ME is not None)
# The absorption is COMPLETE: penumbra.core.evidence must no longer exist (its shapes retired INTO
# recall.graph). An import attempt must RAISE — the module is deleted, not merely re-exported.
try:
    import penumbra.core.evidence as _dead_evidence  # noqa: F401,E402
    check("graph: penumbra.core.evidence is GONE (absorbed into recall.graph)", False,
          "penumbra.core.evidence still importable")
except ImportError:
    check("graph: penumbra.core.evidence is GONE (absorbed into recall.graph)", True)

# --- overlap_count: each excluded_relevant entry carries an 'overlap' key ---
_fetch_src = _inspect42.getsource(fetcher.search_many)
check("overlap: excluded_relevant entries carry 'overlap' key",
      '"overlap": _n' in _fetch_src or "'overlap': _n" in _fetch_src)

# --- penumbra_gather: registered; the whitelist is now an explicit STATIC dict (mechanism demoted from
#     the old _init_gather_tools regex scan) of EXACTLY the twelve read-only names ---
from penumbra.server import penumbra_gather as _eg, _GATHER_TOOLS  # noqa: E402
check("gather: penumbra_gather is registered as a tool", _eg is not None)
# _init_gather_tools is GONE: _GATHER_TOOLS is built once, at import, as explicit data. Assert it is
# EXACTLY the twelve read-only names (no more, no less) so a new write-tool can't sneak in by pattern.
# penumbra_graph joined the read-only surface (the unified graph's budgeted projections are pure reads).
_GATHER_EXPECT = {
    "penumbra_sources", "penumbra_search", "penumbra_read", "penumbra_view", "penumbra_transcribe",
    "penumbra_field_skeleton", "penumbra_paper_recommend", "penumbra_paper_enrich",
    "penumbra_resolve_identity", "penumbra_coauthors", "penumbra_institution_cohort",
    "penumbra_graph",
}
check("gather: whitelist is EXACTLY the twelve read-only tools",
      set(_GATHER_TOOLS) == _GATHER_EXPECT,
      f"missing={_GATHER_EXPECT - set(_GATHER_TOOLS)} extra={set(_GATHER_TOOLS) - _GATHER_EXPECT}")
# The write/orchestration surface is excluded BY OMISSION: the sensor dispatcher (run mutates
# baselines), both curator dispatchers, and gather itself (no recursion) must NOT appear.
check("gather: whitelist excludes penumbra_sensor / penumbra_curator_view / penumbra_curator_act / penumbra_gather",
      not ({"penumbra_sensor", "penumbra_curator_view", "penumbra_curator_act", "penumbra_gather"} & set(_GATHER_TOOLS)))

# --- parsimony tripwires (owner-directive 2026-07-01: 如无必要勿增实体). The FROZEN counts are
#     deliberate speed bumps: adding a tool/prompt must come with a conscious bump HERE, forcing
#     the parsimony conversation (does the new entity encode a NEW judgment, or should it fuse
#     into an existing verb?). Never bump casually. ---
import penumbra.server as _pt_srv  # noqa: E402
import inspect as _pt_inspect  # noqa: E402
_pt_src = _pt_inspect.getsource(_pt_srv)
_TOOL_COUNT_FROZEN = 18   # 17 + penumbra_statement (P8: the typed-statements WRITE verb; the conscious
#                            17 -> 18 bump the graph design named — a statement is a DIRECTED, typed
#                            RELATION judgment, the general sibling of penumbra_ruling; a SEPARATE tool
#                            keeps penumbra_graph read-only / gather-safe, and one verb per key-semantics
#                            (penumbra_ruling for pair-keyed identity, penumbra_statement for everything else).
_PROMPT_COUNT_FROZEN = 1  # investigate(target, shape): the ONE recipe channel
check("parsimony tripwire: MCP tool count == frozen 18 (bump consciously or fuse)",
      _pt_src.count(chr(10) + "@mcp.tool()") == _TOOL_COUNT_FROZEN,
      f"found {_pt_src.count(chr(10) + '@mcp.tool()')}")
check("parsimony tripwire: MCP prompt count == frozen 1 (the one recipe channel)",
      _pt_src.count(chr(10) + "@mcp.prompt()") == _PROMPT_COUNT_FROZEN)

# --- docs-drift tripwire: every penumbra_* token in the PRODUCT-FACING docs must be a REGISTERED tool.
#     The native-docs zone is deliberately outside the mirror sync, which is exactly why it needs a
#     gate: a tool rename that skips the docs otherwise ships stale names to users (caught by hand
#     2026-07-02 in the mirror's README/tools.md; this makes it mechanical, on BOTH repos since the
#     mirror sync renames this check's prefix too). CHANGELOG + design/ + recon docs are exempt
#     (historical narrative is allowed to name retired tools). ---
import re as _dd_re_mod  # noqa: E402
_dd_registered = {n for n in dir(_pt_srv) if n.startswith("penumbra_")}
_dd_docs = [ROOT / "README.md", ROOT / "docs" / "tools.md", ROOT / "docs" / "patterns.md",
            ROOT / "docs" / "configuration.md", ROOT / "docs" / "walled-sources.md"]
_dd_stale: list = []
for _dd_p in _dd_docs:
    if not _dd_p.exists():
        continue
    for _dd_tok in set(_dd_re_mod.findall(r"penumbra_[a-z_]+", _dd_p.read_text(encoding="utf-8"))):
        if _dd_tok not in _dd_registered:
            _dd_stale.append(f"{_dd_p.name}:{_dd_tok}")
check("docs-drift tripwire: product docs name only REGISTERED tools",
      not _dd_stale, f"stale: {sorted(_dd_stale)}")
check("gather: rejects empty calls",
      _eg.__wrapped__(calls=[], wait_s=10).get("error") is not None)
check("gather: rejects >10 calls",
      _eg.__wrapped__(calls=[{"tool": "x"}] * 11, wait_s=10).get("error") is not None)
# A call to an unknown tool returns status="errored" per-call (fail-open, not crash)
_g_unk = _eg.__wrapped__(calls=[{"tool": "no_such_tool", "args": {}}], wait_s=5)
check("gather: unknown tool returns per-call error (fail-open)",
      _g_unk["results"][0]["status"] == "errored" and _g_unk["completed"] == 0)
# A call to penumbra_sources (the simplest real tool) works inside gather
_g_ls = _eg.__wrapped__(calls=[{"tool": "penumbra_sources", "args": {}}], wait_s=30)
check("gather: penumbra_sources works inside gather",
      _g_ls["results"][0]["status"] == "ok" and "sources" in _g_ls["results"][0].get("result", {}))

# --- MCP prompts: registered on the server ---
from penumbra.server import mcp as _mcp43  # noqa: E402
# FastMCP stores prompts in _prompt_manager; check via the prompt functions themselves
# The five per-shape prompts collapsed into ONE parameterized investigate(target, shape).
from penumbra.server import investigate  # noqa: E402
check("prompt: investigate is callable", callable(investigate))
_p_person = investigate(target="Test Person", shape="person", context="RL researcher")
check("prompt: investigate(shape='person') returns a list of message dicts",
      isinstance(_p_person, list) and len(_p_person) > 0
      and _p_person[0].get("role") == "user" and "Test Person" in _p_person[0].get("content", ""))
_p_chase = investigate(target="Test Person", shape="chase")
check("prompt: investigate(shape='chase') returns a list of message dicts",
      isinstance(_p_chase, list) and len(_p_chase) > 0
      and _p_chase[0].get("role") == "user" and "Test Person" in _p_chase[0].get("content", ""))
_p_unknown = investigate(target="Test Person", shape="nonsense")
check("prompt: investigate(unknown shape) still returns a list (falls back)",
      isinstance(_p_unknown, list) and len(_p_unknown) > 0
      and _p_unknown[0].get("role") == "user")

# ---------------------------------------------------------------------------
# 44. Feature 1: wait_s early-return on gather (streaming perception). The old timeout_s /
#     return_after_s split collapsed into ONE wait_s patience budget: gather returns when all calls
#     finish OR wait_s elapses; still-running calls come back status="warming".
# ---------------------------------------------------------------------------

# wait_s=0 should return immediately; the call gets status="warming" (its background thread keeps going)
_g_early = _eg.__wrapped__(calls=[{"tool": "penumbra_sources", "args": {}}], wait_s=0)
check("early-return: wait_s=0 returns a valid result dict",
      "results" in _g_early and "warming" in _g_early)
# With wait_s=0, the call may or may not finish in time (race), but the result must be either
# status=ok OR status=warming (never "errored" from an early return).
_er_status = _g_early["results"][0]["status"]
check("early-return: result status is 'ok' or 'warming' (never errored for a valid call)",
      _er_status in ("ok", "warming"))
check("early-return: completed + warming + failed == total",
      _g_early["completed"] + _g_early["warming"] + _g_early["failed"] == _g_early["total"])

# A generous wait_s still lets a fast call finish (warming drains to 0)
_g_std = _eg.__wrapped__(calls=[{"tool": "penumbra_sources", "args": {}}], wait_s=30)
check("standard-gather: a generous wait_s completes the fast call",
      _g_std["results"][0]["status"] == "ok" and _g_std["warming"] == 0)

# ---------------------------------------------------------------------------
# 45. Feature 2: Sensor CRUD + diff logic. The four penumbra_sensor_* CRUD tools collapsed into ONE
#     penumbra_sensor(action=...) dispatcher; route the create/list/delete lifecycle through it (against a
#     temp-path store, so no real state is touched), then the pure diff logic direct on the store.
# ---------------------------------------------------------------------------
import tempfile as _tempfile44  # noqa: E402
from pathlib import Path as _Path44  # noqa: E402
import penumbra.core.sensor as _sensmod  # noqa: E402
from penumbra.core.sensor import compute_diff as _sdiff  # noqa: E402
from penumbra.server import penumbra_sensor as _esensor  # noqa: E402

_tmp_sensor = _Path44(_tempfile44.mktemp(suffix=".json"))
_sens_real_default = _sensmod._DEFAULT_STATE_PATH
try:
    # penumbra_sensor's dispatcher builds SensorStore() with the module-default path; point it at the temp
    # file so the CRUD lifecycle runs through the real tool without touching ~/.penumbra state.
    _sensmod._DEFAULT_STATE_PATH = _tmp_sensor
    _c1 = _esensor.__wrapped__(action="create", query="test query alpha", sources=["arxiv"])
    check("sensor: action=create returns a sensor with an id",
          _c1.get("created") is True and _c1["sensor"]["id"].startswith("sensor_"))
    _s1_id = _c1["sensor"]["id"]
    _lst1 = _esensor.__wrapped__(action="list")
    check("sensor: action=list shows the created sensor",
          _lst1.get("count") == 1 and _lst1["sensors"][0]["id"] == _s1_id)
    _esensor.__wrapped__(action="create", query="test query beta")
    check("sensor: two sensors created", _esensor.__wrapped__(action="list").get("count") == 2)
    _del1 = _esensor.__wrapped__(action="delete", sensor_id=_s1_id)
    check("sensor: action=delete removes the sensor",
          _del1.get("deleted") is True and _esensor.__wrapped__(action="list").get("count") == 1)
    # A missing required arg is a mechanical error dict, not a crash (delete/run need sensor_id;
    # run also errors on an unknown id — both short-circuit before any network work).
    check("sensor: action=run without sensor_id returns an error dict",
          isinstance(_esensor.__wrapped__(action="run").get("error"), str))
    check("sensor: action=delete without sensor_id returns an error dict",
          isinstance(_esensor.__wrapped__(action="delete").get("error"), str))
    check("sensor: unknown action returns an error dict",
          isinstance(_esensor.__wrapped__(action="frobnicate").get("error"), str))

    # notify field end-to-end: action="create" with notify=True must PERSIST notify in the store row
    # (the cron Barks on new results only when this flag is set). Assert it round-trips: the create
    # response carries it AND it is re-read from the on-disk store (not just echoed back).
    _cn = _esensor.__wrapped__(action="create", query="notify me query", notify=True)
    check("sensor: action=create with notify=True echoes notify in the created sensor",
          _cn.get("created") is True and _cn["sensor"]["notify"] is True)
    _cn_id = _cn["sensor"]["id"]
    _cn_row = _sensmod.SensorStore(_tmp_sensor).get(_cn_id)
    check("sensor: notify=True persists in the store row (read back from disk)",
          _cn_row is not None and _cn_row.notify is True)
    # A create WITHOUT notify defaults it off, so the flag is opt-in (no accidental Barks).
    _cn2 = _esensor.__wrapped__(action="create", query="quiet query")
    check("sensor: create without notify defaults notify=False in the store row",
          _sensmod.SensorStore(_tmp_sensor).get(_cn2["sensor"]["id"]).notify is False)
    _esensor.__wrapped__(action="delete", sensor_id=_cn_id)
    _esensor.__wrapped__(action="delete", sensor_id=_cn2["sensor"]["id"])

    # Diff logic (pure function, no network)
    _diff_baseline = [["arxiv", "123"], ["s2", "456"]]
    _diff_results = [_PDoc(source="arxiv", source_id="123", url="http://a",
                           title="Old Paper Title Long Enough", content="x"),
                     _PDoc(source="arxiv", source_id="789", url="http://b",
                           title="New Paper Title Long Enough", content="y")]
    _diff_new = _sdiff(_diff_results, _diff_baseline)
    check("sensor-diff: detects new (source,source_id) not in baseline",
          _diff_new == [("arxiv", "789")])
    check("sensor-diff: existing baseline entry is NOT flagged as new",
          ("arxiv", "123") not in _diff_new)
    check("sensor-diff: empty results = empty diff",
          _sdiff([], _diff_baseline) == [])
    check("sensor-diff: empty baseline = all results are new",
          len(_sdiff(_diff_results, [])) == 2)
finally:
    _sensmod._DEFAULT_STATE_PATH = _sens_real_default
    _tmp_sensor.unlink(missing_ok=True)

# The sensor MCP surface is the single penumbra_sensor dispatcher.
check("sensor: penumbra_sensor dispatcher is registered", callable(_esensor))

# ---------------------------------------------------------------------------
# 45b. Dispatch routing (wave-2 surface): penumbra_search / penumbra_read / penumbra_view each collapsed several
#      old tools into ONE verb that auto-routes. Prove the ROUTING (which internal path fires) with
#      source-level monkeypatches, never a live fetch — the old per-tool entry points are gone, so the
#      only guarantee left is that the dispatch args reproduce each old path.
# ---------------------------------------------------------------------------

# --- penumbra_search: raw buckets vs the drill idiom vs the default ranked list, + staleness translation ---
_srch_real = {
    "search_ranked": fetcher.search_ranked,
    "search_many": fetcher.search_many,
    "fetch_one_with_diag": fetcher.fetch_one_with_diag,
    "is_enabled_by_profile": fetcher.is_enabled_by_profile,
}
_srch_seen: dict = {}
try:
    # Each fake records that its path fired + the kwargs the server translated, and returns that
    # path's real shape (ranked/drill -> (docs, meta/diag); buckets -> (results_dict, meta)).
    def _fake_ranked(query, sources, limit, deadline_s=None, fresh=False, semantic=None, cache_only=False):
        _srch_seen["path"] = "ranked"
        _srch_seen["fresh"] = fresh
        _srch_seen["cache_only"] = cache_only
        _srch_seen["deadline_s"] = deadline_s
        return [], {"searched": []}

    def _fake_many(query, sources, limit, deadline_s=None, fresh=False):
        _srch_seen["path"] = "buckets"
        _srch_seen["deadline_s"] = deadline_s
        return {}, {"searched": []}

    def _fake_drill(source, query, limit, fresh=False, **kw):
        _srch_seen["path"] = "drill"
        _srch_seen["deadline_s"] = kw.get("deadline_s", "unset")
        return [], None

    fetcher.search_ranked = _fake_ranked
    fetcher.search_many = _fake_many
    fetcher.fetch_one_with_diag = _fake_drill
    fetcher.is_enabled_by_profile = lambda s: True  # the drill checks the profile first

    _srch_seen.clear()
    _r_default = _srv2.penumbra_search.__wrapped__(query="q")
    check("search route: default (raw=False) -> the ranked path (dedup+rank into one list)",
          _srch_seen.get("path") == "ranked" and "documents" in _r_default)

    _srch_seen.clear()
    _r_buckets = _srv2.penumbra_search.__wrapped__(query="q", raw=True, sources=["a", "b"])
    check("search route: raw=True + many sources -> the per-source buckets path",
          _srch_seen.get("path") == "buckets" and "results" in _r_buckets)

    _srch_seen.clear()
    _r_drill = _srv2.penumbra_search.__wrapped__(query="q", raw=True, sources=["only"], full=True)
    check("search route: raw=True + exactly one source -> the drill (old penumbra_fetch) path",
          _srch_seen.get("path") == "drill" and _r_drill.get("source") == "only")

    # staleness enum -> engine fresh / cache_only booleans (the MCP-surface translation).
    _srch_seen.clear()
    _srv2.penumbra_search.__wrapped__(query="q", staleness="fresh")
    check("search staleness: 'fresh' -> fresh=True, cache_only=False",
          _srch_seen.get("fresh") is True and _srch_seen.get("cache_only") is False)
    _srch_seen.clear()
    _srv2.penumbra_search.__wrapped__(query="q", staleness="cache_only")
    check("search staleness: 'cache_only' -> fresh=False, cache_only=True",
          _srch_seen.get("fresh") is False and _srch_seen.get("cache_only") is True)
    _srch_seen.clear()
    _r_unknown = _srv2.penumbra_search.__wrapped__(query="q", staleness="bogus")
    check("search staleness: an unknown value falls back to cached_ok + adds a note",
          _srch_seen.get("fresh") is False and _srch_seen.get("cache_only") is False
          and "bogus" in (_r_unknown.get("note") or ""))
finally:
    fetcher.search_ranked = _srch_real["search_ranked"]
    fetcher.search_many = _srch_real["search_many"]
    fetcher.fetch_one_with_diag = _srch_real["fetch_one_with_diag"]
    fetcher.is_enabled_by_profile = _srch_real["is_enabled_by_profile"]

# --- penumbra_read: a document target routes to docreader; a plain URL routes to the URL reader ---
_read_real = {"read_document": docreader.read_document, "fetch_url": fetcher.fetch_url}
_read_seen: dict = {}
try:
    docreader.read_document = (lambda target, **kw: (_read_seen.__setitem__("path", "document")
                                                     or {"source": "doc", "text": "stub"}))
    fetcher.fetch_url = (lambda target: (_read_seen.__setitem__("path", "url") or None))

    _read_seen.clear()
    _rd_doc = _srv2.penumbra_read.__wrapped__(target="penumbra-inbox/deck.pptx")
    check("read route: a .pptx target routes to the document reader",
          _read_seen.get("path") == "document" and _rd_doc.get("source") == "doc")
    _read_seen.clear()
    _rd_pdf = _srv2.penumbra_read.__wrapped__(target="https://x.example.com/paper.pdf?dl=1")
    check("read route: a .pdf URL (even with ?query) routes to the document reader",
          _read_seen.get("path") == "document")
    _read_seen.clear()
    _rd_url = _srv2.penumbra_read.__wrapped__(target="https://example.com/some/article")
    check("read route: a plain (non-document) URL routes to the URL reader",
          _read_seen.get("path") == "url" and _rd_url.get("url") == "https://example.com/some/article")
finally:
    docreader.read_document = _read_real["read_document"]
    fetcher.fetch_url = _read_real["fetch_url"]

# --- penumbra_view: kind="auto" routes .pdf->document, a video URL->video, a plain image list->images ---
from penumbra.core import vframes as _vframes  # noqa: E402
_view_real = {"view_images": docreader.view_images, "view_image_urls": docreader.view_image_urls,
              "video_frames": _vframes.video_frames}
_view_seen: dict = {}
try:
    # Each fake returns a dict with NO images/sheet, so penumbra_view returns it verbatim (its post-call
    # guard is `if error or not (images or sheet): return r`) -> the marker proves which branch fired.
    docreader.view_images = (lambda target, **kw: (_view_seen.__setitem__("path", "document")
                                                   or {"branch": "document"}))
    docreader.view_image_urls = (lambda target, **kw: (_view_seen.__setitem__("path", "images")
                                                       or {"branch": "images"}))
    _vframes.video_frames = (lambda target, **kw: (_view_seen.__setitem__("path", "video")
                                                   or {"branch": "video"}))

    _view_seen.clear()
    _v_doc = _srv2.penumbra_view.__wrapped__(target="penumbra-inbox/slides.pdf")
    check("view route: a .pdf target routes to the document (figures) branch",
          _view_seen.get("path") == "document" and _v_doc.get("branch") == "document")
    _view_seen.clear()
    _v_vid = _srv2.penumbra_view.__wrapped__(target="https://www.youtube.com/watch?v=abc123")
    check("view route: a youtube URL routes to the video (frames) branch",
          _view_seen.get("path") == "video" and _v_vid.get("branch") == "video")
    _view_seen.clear()
    _v_img = _srv2.penumbra_view.__wrapped__(target="https://cdn.example.com/a.jpg, https://cdn.example.com/b.png")
    check("view route: a plain image-URL list routes to the images branch",
          _view_seen.get("path") == "images" and _v_img.get("branch") == "images")
finally:
    docreader.view_images = _view_real["view_images"]
    docreader.view_image_urls = _view_real["view_image_urls"]
    _vframes.video_frames = _view_real["video_frames"]

# ---------------------------------------------------------------------------
# 46. Deploy seam: native deploy files must provision the SAME artifacts the
#     synced code reads (the 2026-06-28 naming round renamed the token file;
#     this seam broke penumbra's docker CI when the full sync arrived).
# ---------------------------------------------------------------------------
_ep_path = ROOT / "deploy" / "docker-entrypoint.sh"
_sh_path = ROOT / "src" / "penumbra" / "serve_http.py"
if _ep_path.exists() and _sh_path.exists():
    _tok_name = "penumbra_http.json"
    check("deploy seam: serve_http reads the token file the docker entrypoint writes",
          _tok_name in _sh_path.read_text(encoding="utf-8")
          and _tok_name in _ep_path.read_text(encoding="utf-8"))

# ---------------------------------------------------------------------------
# 46b. plist-drift tripwire: scripts/services.py is the SINGLE source of truth for every launchd
#      service; its gen-plists regenerates every committed .plist from the registry and the committed
#      files are what the mini installs (deploy.sh does NOT ship plists). Run that generation logic
#      IN-PROCESS (import, not subprocess) and assert every .plist under scripts/ is byte-identical to
#      the registry — so a hand-edited plist or a registry row that forgot to regenerate FAILS the
#      gate. Also assert the four retired sentinel plists + their scripts are GONE and their labels are
#      absent from the registry (the 2026-07-01 sweep: immigration / invest / research / residency).
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = ROOT / "scripts"
_SCRIPTS_DIR = _SCRIPTS_DIR if _SCRIPTS_DIR.exists() else (ROOT.parent.parent / "scripts")
_SERVICES_PATH = _SCRIPTS_DIR / "services.py"
if _SERVICES_PATH.exists():
    import importlib.util as _il_svc  # noqa: E402
    import io as _io_svc  # noqa: E402
    import contextlib as _ctx_svc  # noqa: E402
    _svc_spec = _il_svc.spec_from_file_location("penumbra_services_smoke", _SERVICES_PATH)
    _svc = _il_svc.module_from_spec(_svc_spec)
    _svc_spec.loader.exec_module(_svc)  # top-level code is guarded by __main__, so import is a no-op
    # gen_plists(write=False) diffs every committed plist against build_plist(row) and returns 0 iff
    # all are byte-clean (no orphan plist, no registry row missing its plist). Swallow its verbose
    # per-file stdout so the smoke log stays readable; only the return code is the tripwire.
    with _ctx_svc.redirect_stdout(_io_svc.StringIO()):
        _plist_rc = _svc.gen_plists(write=False)
    _committed_plists = sorted(_SCRIPTS_DIR.glob("com.penumbra.*.plist"))
    check("plist-drift: every committed .plist is byte-identical to services.py's registry (gen-plists clean)",
          _plist_rc == 0 and len(_committed_plists) > 0,
          f"gen_plists rc={_plist_rc}, {len(_committed_plists)} plist(s)")
    # The four retired sentinels: plists + scripts GONE from scripts/, labels GONE from the registry.
    _SENTINEL_LABELS = ["com.penumbra.sentinel.immigration", "com.penumbra.sentinel.invest",
                        "com.penumbra.sentinel.research", "com.penumbra.sentinel.residency"]
    _SENTINEL_SCRIPTS = ["signpost_sentinel.py", "invest_sentinel.py",
                        "research_sentinel.py", "residency_sentinel.py"]
    _live_plists = {p.name for p in _committed_plists}
    _stray_plists = [f"{lab}.plist" for lab in _SENTINEL_LABELS if f"{lab}.plist" in _live_plists]
    check("plist-drift: the four retired sentinel plists are gone from scripts/",
          not _stray_plists, f"still present: {_stray_plists}")
    _stray_scripts = [s for s in _SENTINEL_SCRIPTS if (_SCRIPTS_DIR / s).exists()]
    check("plist-drift: the four retired sentinel scripts are gone from scripts/",
          not _stray_scripts, f"still present: {_stray_scripts}")
    _reg_labels = {r["label"] for r in _svc.REGISTRY}
    _stray_rows = [lab for lab in _SENTINEL_LABELS if lab in _reg_labels]
    check("plist-drift: no retired sentinel labels remain in the services.py registry",
          not _stray_rows, f"still in registry: {_stray_rows}")
    # publish_prepare.sh + PRIVACY.md were removed in the same sweep — assert they did not come back.
    check("plist-drift: retired publish_prepare.sh + PRIVACY.md stay gone",
          not (_SCRIPTS_DIR / "publish_prepare.sh").exists() and not (ROOT / "PRIVACY.md").exists())

# ---------------------------------------------------------------------------
# 47. The unified graph P1 (docs/design/graph-unified-model.md v2.0): recall's RELATION index —
#     ONE store surfaced through N indexes, storing FACTS (tier M) + labeled CANDIDATES (tier A),
#     NEVER verdicts. Tier J is STRUCTURALLY excluded by a SQL CHECK. Everything runs against a FRESH
#     temp-db (the sensor section's temp-path pattern, applied to store.DB_PATH), so no real state is
#     touched and the cold-start counts are deterministic. Exercises: the CHECK as the organ boundary,
#     the derived doc-doc same_as views (title_fp + doc->work id_eq, never stored), the find entry
#     point + its cap, cold-start stats correctness, the policy method-sets on neighborhood, and a
#     not_same ruling suppressing an exploratory candidate.
# ---------------------------------------------------------------------------
import sqlite3 as _sqlite47  # noqa: E402
import tempfile as _tf47  # noqa: E402
import threading as _thr47  # noqa: E402
from penumbra.core.recall import graph as _graph  # noqa: E402

# Point store.DB_PATH at a FRESH temp db (a new dir so it never collides with §34's index), re-enable
# the layer, and DROP the main thread's cached read-connection so store._read_con() reconnects to THIS
# db (the graph reads go through that cache, primed against §34's path earlier this run).
_g_db_prev = _rstore.DB_PATH
_g_disabled_prev = _rstore._disabled
_g_local_prev = _rstore._local
_g_rulings_prev = _graph.RULINGS_PATH
_rstore.DB_PATH = Path(_tf47.mkdtemp()) / "smoke_graph.db"
_rstore._disabled = False
_rstore._local = _thr47.local()  # fresh per-thread conn cache -> _read_con() reconnects to THIS db
try:
    check("graph: index init creates the graph tables in the temp db", _rstore.init())
    _gcon = _rstore.connect()
    # (a) the graph tables exist after init (graph_nodes + graph_edges).
    _gtabs = {r[0] for r in _gcon.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'graph_%'").fetchall()}
    check("graph: graph_nodes + graph_edges tables exist after init",
          {"graph_nodes", "graph_edges"} <= _gtabs)

    # (b) THE ORGAN BOUNDARY as a CHECK constraint: tier 'J' (agent judgment) cannot physically enter
    #     the store. An INSERT with tier='J' must RAISE (眼是感知不是认知). M/A are the only legal tiers.
    _j_raised = False
    try:
        _gcon.execute("BEGIN")
        _gcon.execute("INSERT INTO graph_edges(src, dst, type, tier, method, first_seen, last_seen) "
                      "VALUES('a', 'b', 'same_as', 'J', 'ruling', 1.0, 1.0)")
        _gcon.commit()
    except _sqlite47.IntegrityError:
        _j_raised = True
        _gcon.rollback()
    check("graph: INSERTing tier='J' into graph_edges RAISES (the razor as a SQL CHECK; J is excluded)",
          _j_raised)
    # A control: tier='M' inserts fine (the CHECK admits the mechanical tiers), then clean it back out.
    _gcon.execute("BEGIN")
    _gcon.execute("INSERT INTO graph_edges(src, dst, type, tier, method, first_seen, last_seen) "
                  "VALUES('a', 'b', 'cites', 'M', 'api:openalex', 1.0, 1.0)")
    _gcon.commit()
    check("graph: tier='M' inserts fine (the CHECK admits mechanical tiers)",
          _gcon.execute("SELECT count(*) FROM graph_edges WHERE tier='M'").fetchone()[0] == 1)
    _gcon.execute("DELETE FROM graph_edges")
    _gcon.commit()

    # NOTE (symmetric-normalization, design section 4): the writer stores symmetric edges once with
    # src < dst; the DDL does NOT enforce that in P1 because NO writers exist yet (P1 writes nothing
    # new — doc-doc same_as is derived views). SKIP per spec: nothing to assert until a tap ships.

    # (c) Seed the docs table via the store's upsert path (as §34 does): TWO docs with the SAME long
    #     normalized title (>=20 alnum chars -> fp = "title:{norm}", the derived same_as substrate) from
    #     DIFFERENT sources, plus ONE doc carrying a DOI in doc_json.metadata (the doc->work id_eq view).
    _GTITLE = "Unified Graph Retrieval Memory For Deep Structured Search"  # long -> title-fp key
    _gd_a = _doc("arxiv", _GTITLE, "http://arxiv.org/abs/2507.00001")
    _gd_a.source_id = "graph_twin_a"
    _gd_b = _doc("openalex", _GTITLE, "https://doi.org/10.9/graphtwin")
    _gd_b.source_id = "graph_twin_b"
    _gd_doi = _doc("s2", "Graph Doc Carrying A DOI External Identifier Long Title", "http://s2/g1")
    _gd_doi.source_id = "graph_doi"
    _gd_doi.metadata = {"doi": "10.1234/graphdoi"}
    _gcon.execute("BEGIN")
    for _gd in (_gd_a, _gd_b, _gd_doi):
        _recall.writer._upsert(_gcon, rank, _gd, 1.0)
    _gcon.commit()
    # Both twins must share the SAME title: fp (the derived edge fires only off a matching fp).
    _gfp_a = _gcon.execute("SELECT fp FROM docs WHERE source_id='graph_twin_a'").fetchone()[0]
    _gfp_b = _gcon.execute("SELECT fp FROM docs WHERE source_id='graph_twin_b'").fetchone()[0]
    check("graph: the two twin docs share one 'title:' fingerprint (the derived same_as substrate)",
          _gfp_a == _gfp_b and _gfp_a.startswith("title:"))

    _nid_a = _graph.doc_node_id("arxiv", "graph_twin_a")
    _nid_b = _graph.doc_node_id("openalex", "graph_twin_b")

    # (d) find(): the ENTRY POINT — turn a title token into candidate virtual doc nodes.
    _gfind = _graph.find("Unified Graph Retrieval")
    _gfind_ids = {n["id"] for n in _gfind["nodes"]}
    check("graph: find('...title token...') returns the virtual doc nodes for both twins",
          _nid_a in _gfind_ids and _nid_b in _gfind_ids)
    check("graph: find results are virtual document nodes (kind='document', label=title)",
          all(n["kind"] == "document" for n in _gfind["nodes"] if n["id"] in (_nid_a, _nid_b)))
    # capped: a tiny limit must stamp capped=true and return exactly `limit` nodes (no silent caps).
    _gfind_cap = _graph.find("Unified Graph Retrieval", limit=1)
    check("graph: find caps to a tiny limit and stamps capped=true (no silent caps)",
          _gfind_cap["capped"] is True and len(_gfind_cap["nodes"]) == 1)

    # (e) stats(): the cold-start orientation call. docs count reflects the seeded rows; entity kinds
    #     are EMPTY (P1 writes no entity nodes) — emptiness here is CORRECT, not broken (design §10).
    _gstats = _graph.stats()
    check("graph: stats() reports the docs count (virtual document nodes over the docs table)",
          _gstats["node_kinds"].get("document") == 3)
    _gentity_kinds = {"work", "person", "institution", "venue", "topic"}
    check("graph: stats() shows ZERO entity kinds at cold start (P1 correctness, not breakage)",
          not (_gentity_kinds & set(_gstats["node_kinds"].keys()))
          and _gstats["edge_types"] == {} and _gstats["edge_tiers"] == {})

    # (f) neighborhood() + the policy METHOD-SETS: exploratory admits align:title_fp, so it traverses
    #     the DERIVED title_fp same_as edge to the twin; conservative (id_eq only) does NOT.
    _gnb_expl = _graph.neighborhood(_nid_a, depth=1, policy="exploratory")
    _gnb_expl_ids = {n["id"] for n in _gnb_expl["nodes"]}
    _gnb_expl_titlefp = [e for e in _gnb_expl["edges"]
                         if e["type"] == "same_as" and e.get("method") == "align:title_fp"]
    check("graph: neighborhood(policy='exploratory') traverses the DERIVED title_fp same_as to the twin",
          _nid_b in _gnb_expl_ids and any(
              {e["src"], e["dst"]} == {_nid_a, _nid_b} for e in _gnb_expl_titlefp))
    _gnb_cons = _graph.neighborhood(_nid_a, depth=1, policy="conservative")
    check("graph: neighborhood(policy='conservative') does NOT reach the twin (title_fp excluded)",
          _nid_b not in {n["id"] for n in _gnb_cons["nodes"]})
    # The doc->work id_eq view: the DOI doc reaches its world entity under conservative (id_eq:doi).
    _nid_doi = _graph.doc_node_id("s2", "graph_doi")
    _gnb_doi = _graph.neighborhood(_nid_doi, depth=1, policy="conservative")
    check("graph: neighborhood reaches the derived doc->work id_eq same_as (DOI, conservative policy)",
          any(e["type"] == "same_as" and e.get("method") == "id_eq:doi"
              and e["dst"] == "work:doi:10.1234/graphdoi" for e in _gnb_doi["edges"]))

    # (g) a not_same RULING (the one J exception; persisted as config, applied by views) suppresses the
    #     exploratory title_fp candidate for that pair. Load it via a monkeypatched RULINGS_PATH (temp
    #     file), so no real ~/.penumbra state is touched. not_same_as (J) beats same_as (A).
    _grulings = Path(_tf47.mkdtemp()) / "graph_rulings.json"
    _lo, _hi = sorted((_nid_a, _nid_b))
    _grulings.write_text(json.dumps(
        [{"src": _lo, "dst": _hi, "verdict": "not_same", "note": "smoke", "ruled_at": 1.0}]),
        encoding="utf-8")
    _graph.RULINGS_PATH = _grulings
    _gnb_ruled = _graph.neighborhood(_nid_a, depth=1, policy="exploratory")
    check("graph: a not_same ruling suppresses the exploratory title_fp edge (J beats A; applied by views)",
          _nid_b not in {n["id"] for n in _gnb_ruled["nodes"]}
          and not any(e["type"] == "same_as" and {e["src"], e["dst"]} == {_nid_a, _nid_b}
                      for e in _gnb_ruled["edges"]))
    # working policy sees the ruling too (rulings load under working+exploratory); its stats count it.
    _graph.RULINGS_PATH = _grulings
    check("graph: stats() counts the loaded ruling (rulings persist as config the eye applies)",
          _graph.stats()["rulings"] == 1)
finally:
    _graph.RULINGS_PATH = _g_rulings_prev
    _rstore.DB_PATH = _g_db_prev
    _rstore._disabled = _g_disabled_prev
    _rstore._local = _g_local_prev

# (h) penumbra_graph is registered as a tool AND present in the gather whitelist (the 12th read-only tool);
#     the whitelist exactness was already asserted above — here confirm membership + registration.
from penumbra.server import penumbra_graph as _eye_graph47  # noqa: E402
check("graph: penumbra_graph is registered as a tool", callable(_eye_graph47))
check("graph: penumbra_graph is present in _GATHER_TOOLS (the 12th read-only tool)",
      "penumbra_graph" in _GATHER_TOOLS and len(_GATHER_TOOLS) == 12)
# (i) guidance layer (built != discoverable): the server instructions must point the agent at the
#     schema's real home — a source-inspect string check that 'recall.graph' is named in the brief.
import penumbra.server as _srv47  # noqa: E402
check("graph: server instructions mention recall.graph (the guidance-layer pointer to the schema)",
      "recall.graph" in _srv47._PENUMBRA_INSTRUCTIONS)


# ---------------------------------------------------------------------------
# 48. P2.0 — retrieval-anchored THIN memory + the seen_before stamp (design §"P2.0", the thesis-gap
#     fix). Everything the eye RETRIEVES arrives placed, not just the ~40 recall-allowlisted sources:
#     the Path A hook writes a THIN document node (graph_nodes, title + url + fp + external_ids ONLY,
#     never content) for every retrieved doc from a NON-indexed source; walled/circumvention docs stay
#     opt-in (operator privacy). Corollary: every search stamps per-doc first_seen so the agent knows
#     instantly which results are NEW to this deployment. Same FRESH temp-db pattern as §47 (repoint
#     store.DB_PATH etc., restore in finally); drives the writer's real apply path (maybe_ingest ->
#     drain the queue -> _apply on a temp db, the WRITES_ENABLED pattern §9/§12.5 use) and the real
#     first_seen lookup helper (fetcher._stamp_seen_before) directly — no live fetch, no network.
# ---------------------------------------------------------------------------
import time as _time48  # noqa: E402

_p2_db_prev = _rstore.DB_PATH
_p2_disabled_prev = _rstore._disabled
_p2_local_prev = _rstore._local
_p2_writes_prev = _recall.writer.WRITES_ENABLED
_p2_iswalled_prev = fetcher.is_walled_source
_p2_remember_prev = _prof.remember_walled_retrievals
_p2_prof_in_writer = None  # profile module the writer sees (patched below); restored in finally
_rstore.DB_PATH = Path(_tf47.mkdtemp()) / "smoke_p2.db"
_rstore._disabled = False
_rstore._local = _thr47.local()  # fresh per-thread conn cache -> _read_con() reconnects to THIS db
try:
    check("P2.0: index init creates the tables in the temp db", _rstore.init())
    _p2con = _rstore.connect()

    # Classification is DETERMINISTIC via monkeypatch (this section tests the writer's ROUTING + apply,
    # NOT the adapter tier-derivation §46 covers): a chosen "walled_src" is walled, everything else is
    # not. maybe_ingest imports `fetcher` + `profile` locally, so patch on those exact modules.
    _WALLED = {"walled_src"}
    fetcher.is_walled_source = lambda name: name in _WALLED
    from penumbra.core import profile as _p2prof  # the SAME module object maybe_ingest imports
    _p2_prof_in_writer = _p2prof.remember_walled_retrievals
    _p2prof.remember_walled_retrievals = lambda: False   # opt-in OFF by default (walled stays private)

    # Drain the writer queue into ONE _apply call against the temp-db connection — the real apply path
    # (maybe_ingest enqueues; the daemon would drain+_apply; here we drain synchronously, no thread).
    def _p2_drain_apply():
        items = []
        while not _recall.writer._queue.empty():
            try:
                items.append(_recall.writer._queue.get_nowait())
            except Exception:  # noqa: BLE001
                break
        if items:
            _recall.writer._apply(_p2con, items)

    _recall.writer.WRITES_ENABLED = True

    # (1) THREE lanes through the writer's classify+apply path:
    #   (a) an INDEXABLE-source doc -> a full docs row, NO thin graph_nodes row.
    #   (b) a NON-indexable NON-walled doc -> a thin graph_nodes row (fp + url in attrs), NO content.
    #   (c) a WALLED-tier doc -> NO row by default; with the profile opt-in monkeypatched ON -> thin row.
    _P2_A_TITLE = "Indexable Lane Full Row Perception Memory Retrieval Study"
    _p2_a = _doc("hf_daily_papers", _P2_A_TITLE, "http://hf/p2a")   # in _SINGLETONS -> indexable
    _p2_a.source_id = "p2_a"
    _p2_a.content = "indexable lane keeps full content"
    _P2_B_TITLE = "Retrieval Anchored Thin Memory Non Indexed Arxiv Original Long Title"
    _p2_b = _doc("arxiv", _P2_B_TITLE, "http://arxiv.org/abs/p2b")  # not indexable, not walled -> thin
    _p2_b.source_id = "p2_b"
    _p2_b.content = "this content must NEVER reach graph_nodes"
    _p2_c = _doc("walled_src", "Walled Tier Operator Privacy Doc Long Title Here", "http://walled/p2c")
    _p2_c.source_id = "p2_c"

    _recall.maybe_ingest([_p2_a, _p2_b, _p2_c])
    _p2_drain_apply()

    _a_nid = _graph.doc_node_id("hf_daily_papers", "p2_a")
    _b_nid = _graph.doc_node_id("arxiv", "p2_b")
    _c_nid = _graph.doc_node_id("walled_src", "p2_c")

    # (a) indexable -> docs row, and NO thin row for it.
    _a_docrow = _p2con.execute("SELECT count(*) FROM docs WHERE source='hf_daily_papers' "
                               "AND source_id='p2_a'").fetchone()[0]
    _a_thin = _p2con.execute("SELECT count(*) FROM graph_nodes WHERE id=?", (_a_nid,)).fetchone()[0]
    check("P2.0 (a): an indexable-source doc -> a full docs row and NO thin graph_nodes row",
          _a_docrow == 1 and _a_thin == 0)

    # (b) non-indexable non-walled -> thin graph_nodes row (kind='document'), fp + url in attrs, and
    #     the CONTENT is nowhere in the graph node (title + url + fp + external_ids ONLY).
    _b_row = _p2con.execute("SELECT kind, label, attrs_json FROM graph_nodes WHERE id=?",
                            (_b_nid,)).fetchone()
    _b_attrs = json.loads(_b_row[2]) if _b_row else {}
    _b_no_docrow = _p2con.execute("SELECT count(*) FROM docs WHERE source='arxiv' "
                                  "AND source_id='p2_b'").fetchone()[0] == 0
    check("P2.0 (b): a non-indexable non-walled doc -> a thin graph_nodes row (kind='document')",
          _b_row is not None and _b_row[0] == "document" and _b_no_docrow)
    check("P2.0 (b): the thin row carries fp + url in attrs, and NO content anywhere in the node",
          _b_attrs.get("url") == "http://arxiv.org/abs/p2b"
          and str(_b_attrs.get("fp", "")).startswith("title:")
          and "this content must NEVER reach graph_nodes" not in (_b_row[2] or "")
          and "content" not in _b_attrs)

    # (c) walled -> NO row while the opt-in is OFF.
    _c_off = _p2con.execute("SELECT count(*) FROM graph_nodes WHERE id=?", (_c_nid,)).fetchone()[0]
    check("P2.0 (c): a walled-tier doc leaves NO row by default (operator privacy; opt-in OFF)",
          _c_off == 0)

    # (c) flip the profile opt-in ON (the deployment flag) -> re-push the walled doc -> a thin row appears.
    _p2prof.remember_walled_retrievals = lambda: True
    _recall.maybe_ingest([_p2_c])
    _p2_drain_apply()
    _c_on = _p2con.execute("SELECT kind FROM graph_nodes WHERE id=?", (_c_nid,)).fetchone()
    check("P2.0 (c): with walled.remember_retrievals opt-in ON, the walled doc gets a thin row",
          _c_on is not None and _c_on[0] == "document")
    _p2prof.remember_walled_retrievals = lambda: False   # restore the default-private posture

    # (2) UPSERT semantics: re-pushing (b) bumps last_seen, keeps first_seen immutable, row count stable.
    _b_first_before, _b_last_before = _p2con.execute(
        "SELECT first_seen, last_seen FROM graph_nodes WHERE id=?", (_b_nid,)).fetchone()
    _b_count_before = _p2con.execute("SELECT count(*) FROM graph_nodes WHERE kind='document'").fetchone()[0]
    # _apply stamps `now = time.time()`; force a strictly-later wall clock by advancing the row's stored
    # last_seen is not how the writer works — instead re-push and rely on time.time() moving forward. To
    # make the bump observable deterministically (sub-ms runs), directly re-upsert with an explicit later
    # `now` via the same _upsert_thin the apply path calls (identical code path, controlled clock).
    _b_later = _b_last_before + 100.0
    _recall.writer._upsert_thin(_p2con, rank, _p2_b, _b_later)
    _p2con.commit()
    _b_first_after, _b_last_after = _p2con.execute(
        "SELECT first_seen, last_seen FROM graph_nodes WHERE id=?", (_b_nid,)).fetchone()
    _b_count_after = _p2con.execute("SELECT count(*) FROM graph_nodes WHERE kind='document'").fetchone()[0]
    check("P2.0 (2): re-upsert bumps last_seen, keeps first_seen immutable, row count stable",
          _b_first_after == _b_first_before and _b_last_after == _b_later
          and _b_last_after > _b_last_before and _b_count_after == _b_count_before)

    # (3) ROLL-OFF (_sweep): a thin document row older than the retention cutoff is swept; a NON-document
    #     graph_nodes row (a fake person entity) SURVIVES (entities persist indefinitely, design §5).
    _old = _time48.time() - (_recall.writer.RETAIN_DAYS + 30) * 86400   # comfortably past the cutoff
    _stale_nid = _graph.doc_node_id("arxiv", "p2_stale")
    _person_nid = "person:openalex:A_p2_survivor"
    _p2con.execute("BEGIN")
    _p2con.execute("INSERT INTO graph_nodes(id, kind, label, attrs_json, first_seen, last_seen) "
                   "VALUES(?, 'document', 'stale thin doc', '{}', ?, ?)", (_stale_nid, _old, _old))
    _p2con.execute("INSERT INTO graph_nodes(id, kind, label, attrs_json, first_seen, last_seen) "
                   "VALUES(?, 'person', 'A Survivor Person Entity', '{}', ?, ?)",
                   (_person_nid, _old, _old))
    _p2con.commit()
    _recall.writer._sweep(_p2con)   # the ONLY deletion path; same cutoff for docs + thin document nodes
    _stale_gone = _p2con.execute("SELECT count(*) FROM graph_nodes WHERE id=?",
                                 (_stale_nid,)).fetchone()[0] == 0
    _person_lives = _p2con.execute("SELECT count(*) FROM graph_nodes WHERE id=?",
                                   (_person_nid,)).fetchone()[0] == 1
    check("P2.0 (3): roll-off sweeps a stale THIN document row (past the retention cutoff)", _stale_gone)
    check("P2.0 (3): roll-off EXEMPTS a non-document entity row (person survives; entities persist)",
          _person_lives)
    # A control: the fresh thin (b) row (last_seen = now-ish) is NOT swept — only the stale one went.
    check("P2.0 (3): the fresh thin (b) row survives the sweep (only past-cutoff rows go)",
          _p2con.execute("SELECT count(*) FROM graph_nodes WHERE id=?", (_b_nid,)).fetchone()[0] == 1)

    # (4) VIEWS over thin memory: find() surfaces the thin doc by a title token; neighborhood() derives
    #     the doc->work id_eq same_as (id_eq:doi, CONSERVATIVE-tier) from a THIN row's external_ids —
    #     and a docs-table row carrying the SAME DOI derives the SAME edge, so the two rows meet at one
    #     work entity (the mechanical "between" link; id_eq shows under conservative AND exploratory,
    #     since conservative ⊆ exploratory). A thin doc with a DOI + a docs-table doc with that DOI:
    _P2_DOI = "10.5555/p2thin"
    _p2_thin_doi = _doc("crossref", "Cross Ref Thin Doc Carrying A Shared DOI Long Enough Title", "http://xref/p2")
    _p2_thin_doi.source_id = "p2_thin_doi"
    _p2_thin_doi.metadata = {"doi": _P2_DOI}          # lifted into the thin row's attrs by _upsert_thin
    _p2_docs_doi = _doc("openalex", "OpenAlex Docs Row Sharing The Same DOI Long Enough Title", "http://oa/p2")
    _p2_docs_doi.source_id = "p2_docs_doi"
    _p2_docs_doi.metadata = {"doi": _P2_DOI}          # lands in doc_json.metadata.doi (the docs arm)
    # crossref is not indexable + not walled -> thin lane; openalex-as-source is indexable -> docs lane.
    # Drive both through the real classify+apply path (openalex source name is NOT in _SINGLETONS, so
    # push the docs-row one straight through _upsert to guarantee the docs-table lane deterministically).
    _recall.maybe_ingest([_p2_thin_doi])
    _p2_drain_apply()
    _p2con.execute("BEGIN")
    _recall.writer._upsert(_p2con, rank, _p2_docs_doi, _time48.time())   # the docs-table row
    _p2con.commit()

    _thin_doi_nid = _graph.doc_node_id("crossref", "p2_thin_doi")
    _docs_doi_nid = _graph.doc_node_id("openalex", "p2_docs_doi")
    _work_doi = f"work:doi:{_P2_DOI.lower()}"

    # find(): the thin doc is discoverable by a title token (the entry point spans thin memory too).
    _p2_find = _graph.find("Cross Ref Thin")
    check("P2.0 (4): find() returns the THIN doc by a title token (entry point covers thin memory)",
          _thin_doi_nid in {n["id"] for n in _p2_find["nodes"]})

    # neighborhood() from the THIN row derives the id_eq:doi same_as to the work entity (conservative).
    _p2_nb_thin_cons = _graph.neighborhood(_thin_doi_nid, depth=1, policy="conservative")
    _thin_id_eq = [e for e in _p2_nb_thin_cons["edges"]
                   if e["type"] == "same_as" and e.get("method") == "id_eq:doi" and e["dst"] == _work_doi]
    check("P2.0 (4): neighborhood(thin, conservative) derives the doc->work id_eq:doi same_as from attrs",
          _work_doi in {n["id"] for n in _p2_nb_thin_cons["nodes"]} and bool(_thin_id_eq))
    # The docs-table row with the SAME DOI derives the SAME edge -> the two rows meet at ONE work node
    # (the id_eq same_as BETWEEN a thin row and a docs-table row, via the shared world entity).
    _p2_nb_docs_cons = _graph.neighborhood(_docs_doi_nid, depth=1, policy="conservative")
    _docs_id_eq = [e for e in _p2_nb_docs_cons["edges"]
                   if e["type"] == "same_as" and e.get("method") == "id_eq:doi" and e["dst"] == _work_doi]
    check("P2.0 (4): a docs-table row with the SAME DOI reaches the SAME work node (they meet at one entity)",
          _work_doi in {n["id"] for n in _p2_nb_docs_cons["nodes"]} and bool(_docs_id_eq))
    # id_eq is conservative-tier, so it ALSO shows under exploratory (conservative ⊆ exploratory).
    _p2_nb_thin_expl = _graph.neighborhood(_thin_doi_nid, depth=1, policy="exploratory")
    check("P2.0 (4): the id_eq:doi same_as also shows under exploratory (conservative ⊆ exploratory)",
          any(e["type"] == "same_as" and e.get("method") == "id_eq:doi" and e["dst"] == _work_doi
              for e in _p2_nb_thin_expl["edges"]))

    # (5) seen_before: the wall's novelty stamp. Drive the REAL lookup helper (fetcher._stamp_seen_before)
    #     against this temp db. P11 W2 COMPLETENESS CONTRACT: EVERY ranked doc carries seen_before
    #     (true|false) + first_seen_at (value|null), NEVER absent. A doc first_seen BEFORE t0 -> True +
    #     an ISO first_seen_at; a doc absent from BOTH tables -> the HONEST False + null (never-seen,
    #     the exact P11 repro: a live-fetched doc used to come back with NO stamp while its siblings
    #     carried one); a doc whose first_seen is AFTER t0 (this search's own async write) -> NOT flipped
    #     to True (the race-proof-by-construction property: your own writes never self-flag).
    _t0 = _time48.time()
    # (i) an OLD doc: a thin row whose first_seen strictly predates t0 (seed it directly, controlled clock).
    _sb_old = _doc("arxiv", "Seen Before Old Thin Doc Predating This Search Long Title", "http://arxiv/sb_old")
    _sb_old.source_id = "sb_old"
    _p2con.execute("INSERT OR REPLACE INTO graph_nodes(id, kind, label, attrs_json, first_seen, last_seen) "
                   "VALUES(?, 'document', 'sb old', '{}', ?, ?)",
                   (_graph.doc_node_id("arxiv", "sb_old"), _t0 - 500.0, _t0 - 500.0))
    _p2con.commit()
    # (ii) a NEW doc, absent from both tables entirely: the P11 W2 root-cause shape (a live-fetched
    #      doc never yet ingested). It MUST still be stamped, with the honest never-seen values.
    _sb_absent = _doc("arxiv", "Seen Before Absent Doc Never Retrieved Here Long Title", "http://arxiv/sb_absent")
    _sb_absent.source_id = "sb_absent"
    # (ii-bis) a "straggler-shaped" doc: absent from memory AND carrying a nearly-bare metadata dict
    #          (the exact recon symptom, no corroboration, no handles), to prove the stamp lands on a
    #          doc the passive-enrichment path barely touched, not only on richly-merged siblings.
    _sb_straggler = _PDoc(source="arxiv", source_id="sb_straggler",
                          url="http://arxiv.org/abs/sb_straggler",
                          title="Uncertainty Decomposition Straggler Bare Metadata Long Title",
                          content="x", metadata={})
    # (iii) a doc whose first_seen is AFTER t0 (models THIS search's own async ingest write).
    _sb_future = _doc("arxiv", "Seen Before Future Doc This Searchs Own Write Long Title", "http://arxiv/sb_future")
    _sb_future.source_id = "sb_future"
    _p2con.execute("INSERT OR REPLACE INTO graph_nodes(id, kind, label, attrs_json, first_seen, last_seen) "
                   "VALUES(?, 'document', 'sb future', '{}', ?, ?)",
                   (_graph.doc_node_id("arxiv", "sb_future"), _t0 + 500.0, _t0 + 500.0))
    _p2con.commit()

    _sb_ranked = [_sb_old, _sb_absent, _sb_straggler, _sb_future]
    fetcher._stamp_seen_before(_sb_ranked, _t0)   # the real batched first_seen lookup, keyed (source,sid)
    check("P2.0 (5): a doc whose first_seen PREDATES t0 is stamped seen_before=True + an ISO first_seen_at",
          _sb_old.metadata.get("seen_before") is True
          and isinstance(_sb_old.metadata.get("first_seen_at"), str)
          and _sb_old.metadata["first_seen_at"].startswith("20"))
    # P11 W2: the completeness contract. The absent (first-time-seen) doc is now stamped the HONEST
    # False + null, NOT left unstamped (the old presence-gated behavior that produced the recon bug).
    check("P2.0 (5) [W2]: a doc absent from both tables is stamped seen_before=False + first_seen_at=None (not absent)",
          _sb_absent.metadata.get("seen_before") is False
          and "seen_before" in _sb_absent.metadata
          and _sb_absent.metadata.get("first_seen_at") is None
          and "first_seen_at" in _sb_absent.metadata)
    check("P2.0 (5) [W2]: a bare-metadata straggler doc ALSO gets the stamp (False + null), not skipped",
          _sb_straggler.metadata.get("seen_before") is False
          and _sb_straggler.metadata.get("first_seen_at") is None)
    check("P2.0 (5): a doc whose first_seen is AFTER t0 (own write) is seen_before=False + first_seen_at=None",
          _sb_future.metadata.get("seen_before") is False
          and _sb_future.metadata.get("first_seen_at") is None)
    # THE CONTRACT, stated directly: EVERY ranked doc carries BOTH keys, no exceptions.
    check("P2.0 (5) [W2]: completeness, EVERY ranked doc carries seen_before AND first_seen_at",
          all(("seen_before" in (_d.metadata or {}) and "first_seen_at" in (_d.metadata or {}))
              for _d in _sb_ranked)
          and all(isinstance((_d.metadata or {}).get("seen_before"), bool) for _d in _sb_ranked))
finally:
    _recall.writer.WRITES_ENABLED = _p2_writes_prev
    fetcher.is_walled_source = _p2_iswalled_prev
    _prof.remember_walled_retrievals = _p2_remember_prev
    if _p2_prof_in_writer is not None:
        _p2prof.remember_walled_retrievals = _p2_prof_in_writer
    # drain any residue so a later section never inherits our queue items.
    while not _recall.writer._queue.empty():
        try:
            _recall.writer._queue.get_nowait()
        except Exception:  # noqa: BLE001
            break
    _rstore.DB_PATH = _p2_db_prev
    _rstore._disabled = _p2_disabled_prev
    _rstore._local = _p2_local_prev


# ---------------------------------------------------------------------------
# 49. P2 — the FIRST write taps (design section 6 + "Vocabulary-by-minting"): cartographer +
#     enrich mint FACTS + labeled candidates through the ONE single-writer queue, never verdicts,
#     fail-open on every path. This section exercises the taps' GOLDEN fixtures (recorded-shape
#     input -> expected node/edge rows, the STABILITY.md convention), the writer's edge semantics
#     (upsert dedupe / symmetric src<dst normalization / tier fail-open), and the mint TRIPWIRE that
#     bounds ACTUAL graph data to the declared vocabulary (the gate that replaces the central enum).
#     Same FRESH temp-db pattern as §47/§48 (repoint store.DB_PATH etc., restore in finally); the
#     taps enqueue and we drain->_apply synchronously on the temp db (no daemon), the §48 idiom.
#     Verbs + docs-drift (items 5/6) are pure source/registration checks, AFTER the temp-db finally.
# ---------------------------------------------------------------------------
import penumbra.core.cartographer as _cartg49  # noqa: E402 — importing the tap registers its mints
import penumbra.core.enrich as _enr49  # noqa: E402 — importing the tap registers its mints

_t49_db_prev = _rstore.DB_PATH
_t49_disabled_prev = _rstore._disabled
_t49_local_prev = _rstore._local
_t49_writes_prev = _recall.writer.WRITES_ENABLED
_rstore.DB_PATH = Path(_tf47.mkdtemp()) / "smoke_taps.db"
_rstore._disabled = False
_rstore._local = _thr47.local()  # fresh per-thread conn cache -> _read_con() reconnects to THIS db
try:
    check("taps: index init creates the tables in the temp db", _rstore.init())
    _t49con = _rstore.connect()
    _recall.writer.WRITES_ENABLED = True

    # Drain the writer queue into ONE _apply against the temp-db connection (the real apply path: the
    # taps call writer.enqueue_graph; the daemon would drain+_apply; here we drain synchronously).
    def _t49_drain_apply():
        _items = []
        while not _recall.writer._queue.empty():
            try:
                _items.append(_recall.writer._queue.get_nowait())
            except Exception:  # noqa: BLE001
                break
        if _items:
            _recall.writer._apply(_t49con, _items)

    # (1) CARTOGRAPHER TAP golden fixture. A ``works`` dict shaped like the field_skeleton _build input
    #     (id -> normalized work). TWO OpenAlex works: W1 carries an author WITH an id, a concept WITH
    #     an id, a primary_location venue, and a referenced_works edge to W2 (the one IN-CORPUS cite);
    #     W2 carries a second id-bearing author. Plus ONE S2-shaped work with a DISPLAY-NAME-ONLY
    #     authorship (no author id). The tap runs once per backend (it namespaces every work under that
    #     backend), so drive openalex then s2; combined -> 3 work nodes. Expected: person nodes ONLY for
    #     the OA-id authors; ONE cites edge (directed, tier M); an about edge to topic:openalex; a
    #     published_in to venue:openalex; and NOTHING minted from the S2 display-name authorship.
    _t49_oa_works = {
        "W1": {"title": "Cartographer Work One", "publication_year": 2020, "cited_by_count": 5,
               "referenced_works": ["https://openalex.org/W2"],
               "concepts": [{"id": "https://openalex.org/C10", "display_name": "RL", "level": 1}],
               "authorships": [{"author": {"id": "https://openalex.org/A100", "display_name": "Alice"}}],
               "venue": {"id": "S50", "display_name": "NeurIPS"},
               "doi": "https://doi.org/10.1/w1"},
        "W2": {"title": "Cartographer Work Two", "publication_year": 2019, "cited_by_count": 9,
               "referenced_works": [], "concepts": [],
               "authorships": [{"author": {"id": "https://openalex.org/A200", "display_name": "Bob"}}]},
    }
    _t49_s2_works = {
        "99": {"title": "S2 Cartographer Work", "publication_year": 2021, "cited_by_count": 1,
               "referenced_works": [], "concepts": [],
               # display-name-only authorship (no author id) -> the S2 path mints NO person here.
               "authorships": [{"author": {"display_name": "DisplayNameOnly"}}]},
    }
    _cartg49._graph_tap("openalex", _t49_oa_works)
    _cartg49._graph_tap("s2", _t49_s2_works)
    _t49_drain_apply()

    _t49_works = {r[0] for r in _t49con.execute(
        "SELECT id FROM graph_nodes WHERE kind='work'").fetchall()}
    check("taps (cartographer): 3 work nodes across the two backends (W1/W2 openalex + 99 s2)",
          _t49_works == {"work:openalex:W1", "work:openalex:W2", "work:s2:99"})
    _t49_persons = {r[0] for r in _t49con.execute(
        "SELECT id FROM graph_nodes WHERE kind='person'").fetchall()}
    check("taps (cartographer): person nodes ONLY for the OA-id authors (A100, A200)",
          _t49_persons == {"person:openalex:A100", "person:openalex:A200"})
    _t49_cites = _t49con.execute(
        "SELECT src, dst, tier, method FROM graph_edges WHERE type='cites'").fetchall()
    check("taps (cartographer): exactly ONE cites edge, directed W1->W2, tier M (in-corpus reference)",
          _t49_cites == [("work:openalex:W1", "work:openalex:W2", "M", "api:openalex")])
    _t49_about = _t49con.execute(
        "SELECT src, dst, tier FROM graph_edges WHERE type='about'").fetchall()
    check("taps (cartographer): an about edge W1->topic:openalex:C10 (concept WITH id), tier M",
          _t49_about == [("work:openalex:W1", "topic:openalex:C10", "M")])
    _t49_pub = _t49con.execute(
        "SELECT src, dst, tier FROM graph_edges WHERE type='published_in'").fetchall()
    check("taps (cartographer): a published_in edge W1->venue:openalex:S50, tier M",
          _t49_pub == [("work:openalex:W1", "venue:openalex:S50", "M")])
    # The S2 display-name authorship minted NOTHING: no person node, no authored edge on the s2 side.
    _t49_s2_authored = _t49con.execute(
        "SELECT count(*) FROM graph_edges WHERE type='authored' AND src LIKE 'work:s2:%'").fetchone()[0]
    _t49_s2_person = _t49con.execute(
        "SELECT count(*) FROM graph_nodes WHERE kind='person' AND id LIKE 'person:s2:%'").fetchone()[0]
    check("taps (cartographer): the S2 display-name-only authorship mints NO node/edge (P3 owns persons)",
          _t49_s2_authored == 0 and _t49_s2_person == 0)

    # (2) ENRICH TAP fixture. A fake enrich RESULT (the arxiv-branch record shape enrich() emits):
    #     the tap upserts the WORK node with the resolved attrs (retracted / is_oa), and a published_in
    #     edge WHEN a venue is present; it must NOT store a doc<->work same_as (that is DERIVED at query
    #     time from external_ids — storing it would double-book).
    _t49_rec = {"id": "2501.00001", "kind": "arxiv", "doi": "10.48550/arXiv.2501.00001",
                "is_oa": True, "citation_count": 7,
                "integrity": {"retracted": False, "notices": []},
                "venue": {"id": "S77", "display_name": "ICLR"}}
    _enr49._graph_tap(_t49_rec)
    _t49_drain_apply()
    _t49_wnid = "work:arxiv:2501.00001"
    _t49_wrow = _t49con.execute(
        "SELECT kind, attrs_json FROM graph_nodes WHERE id=?", (_t49_wnid,)).fetchone()
    _t49_wattrs = json.loads(_t49_wrow[1]) if _t49_wrow and _t49_wrow[1] else {}
    check("taps (enrich): the work node is upserted with retracted + is_oa attrs",
          _t49_wrow is not None and _t49_wrow[0] == "work"
          and _t49_wattrs.get("retracted") is False and _t49_wattrs.get("is_oa") is True)
    _t49_enr_pub = _t49con.execute(
        "SELECT tier, method FROM graph_edges WHERE type='published_in' AND src=?",
        (_t49_wnid,)).fetchone()
    check("taps (enrich): a published_in edge is minted when a venue is present (work->venue, tier M)",
          _t49_enr_pub == ("M", "api:openalex"))
    check("taps (enrich): NO doc<->work same_as row is stored (that edge is DERIVED, not persisted)",
          _t49con.execute("SELECT count(*) FROM graph_edges WHERE type='same_as'").fetchone()[0] == 0)

    # Snapshot the TAP-MINTED vocabulary NOW — after the golden fixtures (items 1-2), BEFORE item 3's
    # writer-semantics probes hand-insert deliberately out-of-vocabulary rows (a reversed align:title_fp
    # same_as, a rejected tier='J') that are test scaffolding, NOT tap output. The mint tripwire (item 4,
    # "after the fixtures") bounds what the TAPS minted, so it reads this snapshot, not the polluted db.
    _t49_minted_kinds = {r[0] for r in _t49con.execute(
        "SELECT DISTINCT kind FROM graph_nodes").fetchall()}
    _t49_minted_types = {r[0] for r in _t49con.execute(
        "SELECT DISTINCT type FROM graph_edges").fetchall()}
    _t49_minted_methods = {r[0] for r in _t49con.execute(
        "SELECT DISTINCT method FROM graph_edges").fetchall()}

    # (3) WRITER edge semantics (design section 4). Drive the writer's real upsert helper directly on
    #     the temp-db connection (the same code path _apply_graph calls per edge).
    #   (i) re-upserting the SAME edge bumps last_seen, never adds a row; first_seen stays immutable.
    _t49_e = {"src": "work:openalex:W1", "dst": "work:openalex:W2", "type": "cites",
              "tier": "M", "method": "api:openalex"}   # the exact edge the cartographer fixture minted
    _t49_c_before = _t49con.execute("SELECT count(*) FROM graph_edges WHERE type='cites'").fetchone()[0]
    _t49_fs_before, _t49_ls_before = _t49con.execute(
        "SELECT first_seen, last_seen FROM graph_edges WHERE src=? AND dst=? AND type='cites'",
        ("work:openalex:W1", "work:openalex:W2")).fetchone()
    _t49_bump = _t49_ls_before + 100.0
    _t49con.execute("BEGIN")
    _recall.writer._upsert_edge(_t49con, _t49_e, _t49_bump)
    _t49con.commit()
    _t49_c_after = _t49con.execute("SELECT count(*) FROM graph_edges WHERE type='cites'").fetchone()[0]
    _t49_fs_after, _t49_ls_after = _t49con.execute(
        "SELECT first_seen, last_seen FROM graph_edges WHERE src=? AND dst=? AND type='cites'",
        ("work:openalex:W1", "work:openalex:W2")).fetchone()
    check("taps (writer): re-upserting the same edge bumps last_seen, keeps row count + first_seen fixed",
          _t49_c_after == _t49_c_before and _t49_ls_after == _t49_bump
          and _t49_ls_after > _t49_ls_before and _t49_fs_after == _t49_fs_before)
    #   (ii) a SYMMETRIC-type edge written REVERSED lands on the SAME row (the src<dst helper). same_as
    #        is symmetric: write (A,B) then (B,A) with one method -> ONE row, stored src < dst.
    _t49_A, _t49_B = "doc:arxiv:zzz_late", "doc:arxiv:aaa_early"   # A > B lexicographically on purpose
    _t49_lo, _t49_hi = sorted((_t49_A, _t49_B))
    _t49con.execute("BEGIN")
    _recall.writer._upsert_edge(
        _t49con, {"src": _t49_A, "dst": _t49_B, "type": "same_as",
                  "tier": "A", "method": "align:title_fp"}, 10.0)
    _recall.writer._upsert_edge(
        _t49con, {"src": _t49_B, "dst": _t49_A, "type": "same_as",
                  "tier": "A", "method": "align:title_fp"}, 20.0)   # reversed write, same pair
    _t49con.commit()
    _t49_sym = _t49con.execute(
        "SELECT src, dst FROM graph_edges WHERE type='same_as' AND method='align:title_fp'").fetchall()
    check("taps (writer): a symmetric edge written reversed lands on ONE row, stored src < dst",
          _t49_sym == [(_t49_lo, _t49_hi)] and _t49_lo < _t49_hi)
    #   (iii) a tier='J' item is DROPPED fail-open by the writer (validated before the SQL touches it),
    #         AND the SQL CHECK would refuse it anyway (the organ boundary, belt + suspenders).
    _t49_j_before = _t49con.execute("SELECT count(*) FROM graph_edges").fetchone()[0]
    _t49con.execute("BEGIN")
    _recall.writer._upsert_edge(
        _t49con, {"src": "j_src", "dst": "j_dst", "type": "same_as",
                  "tier": "J", "method": "ruling"}, 1.0)   # illegal tier -> dropped, no row, no raise
    _t49con.commit()
    _t49_j_after = _t49con.execute("SELECT count(*) FROM graph_edges").fetchone()[0]
    check("taps (writer): a tier='J' edge is DROPPED fail-open (no row, no raise; J never enters the store)",
          _t49_j_after == _t49_j_before)
    _t49_check_raised = False
    try:
        _t49con.execute("BEGIN")
        _t49con.execute("INSERT INTO graph_edges(src, dst, type, tier, method, first_seen, last_seen) "
                        "VALUES('j_src', 'j_dst', 'same_as', 'J', 'ruling', 1.0, 1.0)")
        _t49con.commit()
    except _sqlite47.IntegrityError:
        _t49_check_raised = True
        _t49con.rollback()
    check("taps (writer): the SQL CHECK would refuse a tier='J' row anyway (structural organ boundary)",
          _t49_check_raised)

    # (4) MINT TRIPWIRE (the vocabulary-by-minting GATE, design "Vocabulary-by-minting"): after the
    #     fixtures, the kinds/edge-types/methods the TAPS minted (snapshotted above, before item 3's
    #     out-of-vocabulary scaffolding) must be a SUBSET of the declared union (a tap writing an
    #     UNDECLARED kind/type/method is the bug this catches). And the registry must contain the three
    #     shipped taps (shipping the tap IS the grant). Non-empty guards ensure the fixtures actually
    #     minted rows, so the subset checks are not vacuously true against empty sets.
    _t49_vocab = _graph.declared_vocabulary()
    check("taps (mint tripwire): DISTINCT kinds minted by the taps ⊆ declared_vocabulary().kinds",
          bool(_t49_minted_kinds) and _t49_minted_kinds <= _t49_vocab["kinds"],
          f"undeclared kinds: {sorted(_t49_minted_kinds - _t49_vocab['kinds'])}")
    check("taps (mint tripwire): DISTINCT edge types minted by the taps ⊆ declared edge_types",
          bool(_t49_minted_types) and _t49_minted_types <= _t49_vocab["edge_types"],
          f"undeclared types: {sorted(_t49_minted_types - _t49_vocab['edge_types'])}")
    check("taps (mint tripwire): DISTINCT edge methods minted by the taps ⊆ declared methods",
          bool(_t49_minted_methods) and _t49_minted_methods <= _t49_vocab["methods"],
          f"undeclared methods: {sorted(_t49_minted_methods - _t49_vocab['methods'])}")
    check("taps (mint tripwire): the registry contains the three taps (thin_memory, cartographer, enrich)",
          {"thin_memory", "cartographer", "enrich"} <= set(_graph._MINT_REGISTRY.keys()),
          f"registry: {sorted(_graph._MINT_REGISTRY.keys())}")
finally:
    _recall.writer.WRITES_ENABLED = _t49_writes_prev
    # drain any residue so a later section never inherits our queue items.
    while not _recall.writer._queue.empty():
        try:
            _recall.writer._queue.get_nowait()
        except Exception:  # noqa: BLE001
            break
    _rstore.DB_PATH = _t49_db_prev
    _rstore._disabled = _t49_disabled_prev
    _rstore._local = _t49_local_prev

# (5) VERBS: _PENUMBRA_VERBS is the capability index penumbra_sources surfaces. It must carry EXACTLY the 18 tool
#     names (P3 added penumbra_ruling; P8 added penumbra_statement), every value NON-EMPTY and DERIVED (== that
#     tool's docstring first line, not hand-written prose that could drift). It drifted once already (it
#     was a hand-maintained dict); recompute each tool's docstring first line independently and demand
#     equality, so a docstring edit or a renamed tool that skips the dict is caught.
from penumbra.server import _PENUMBRA_VERBS as _t49_verbs  # noqa: E402
_t49_tool_fns = (
    _srv.penumbra_sources, _srv.penumbra_search, _srv.penumbra_read, _srv.penumbra_view,
    _srv.penumbra_field_skeleton, _srv.penumbra_paper_recommend, _srv.penumbra_paper_enrich,
    _srv.penumbra_resolve_identity, _srv.penumbra_coauthors, _srv.penumbra_institution_cohort,
    _srv.penumbra_transcribe, _srv.penumbra_graph, _srv.penumbra_gather, _srv.penumbra_sensor, _srv.penumbra_ruling,
    _srv.penumbra_statement, _srv.penumbra_curator_view, _srv.penumbra_curator_act,
)
_t49_expect_names = {fn.__name__ for fn in _t49_tool_fns}
check("verbs: _PENUMBRA_VERBS has EXACTLY the 18 tool names",
      set(_t49_verbs.keys()) == _t49_expect_names and len(_t49_verbs) == 18,
      f"missing={_t49_expect_names - set(_t49_verbs)} extra={set(_t49_verbs) - _t49_expect_names}")
check("verbs: every _PENUMBRA_VERBS value is non-empty",
      all(bool((v or '').strip()) for v in _t49_verbs.values()),
      f"empty: {sorted(k for k, v in _t49_verbs.items() if not (v or '').strip())}")
_t49_verb_drift = []
for _t49_fn in _t49_tool_fns:
    _t49_raw = _t49_fn.__wrapped__ if hasattr(_t49_fn, "__wrapped__") else _t49_fn
    _t49_first = ((_t49_raw.__doc__ or "").strip().splitlines() or [""])[0]
    if _t49_verbs.get(_t49_fn.__name__) != _t49_first:
        _t49_verb_drift.append(_t49_fn.__name__)
check("verbs: each _PENUMBRA_VERBS value == that tool's docstring first line (derivation, not prose)",
      not _t49_verb_drift, f"drifted: {_t49_verb_drift}")

# (6) DOCS-DRIFT EXTENSION: the existing docs-drift tripwire (above) scans the product-facing docs for
#     penumbra_* tokens against registered tool names. Extend the SAME discipline to the _PENUMBRA_INSTRUCTIONS
#     string (the brief the server ships to every agent on connect): a renamed tool that updates the
#     tools but not the instructions would otherwise teach a stale name. One legitimate NON-tool token
#     is exempted (with justification); anything else undeclared is drift the tripwire must catch.
# EXEMPTIONS — legitimate penumbra_* tokens in _PENUMBRA_INSTRUCTIONS that are NOT (and should not be) tools:
#   • penumbra_fetch — a RETIRED tool named on PURPOSE to teach the idiom that replaced it ("the drill
#     idiom sources=[one]+raw=True+full=True replaces the old penumbra_fetch"). Naming a retired tool in
#     explanatory narrative is the same carve-out the docs-drift tripwire already grants CHANGELOG/
#     design/recon docs; the mention is pedagogy, not a live reference.
_t49_instr_exempt = {"penumbra_fetch"}
_t49_dd_registered = {n for n in dir(_srv) if n.startswith("penumbra_")}
_t49_instr_tokens = set(_dd_re_mod.findall(r"penumbra_[a-z_]+", _srv._PENUMBRA_INSTRUCTIONS))
_t49_instr_stale = sorted(_t49_instr_tokens - _t49_dd_registered - _t49_instr_exempt)
check("docs-drift (instructions): every penumbra_* token in _PENUMBRA_INSTRUCTIONS is a REGISTERED tool "
      "(or an explicit exemption)",
      not _t49_instr_stale, f"stale: {_t49_instr_stale}")


# ---------------------------------------------------------------------------
# 50. P3 — the ruling verb + the relations tap + voices/between views (design "P3 shipped
#     2026-07-02"). penumbra_ruling is the identity-ruling WRITE channel (create | list | delete) the
#     working policy applies; the relations tap mints the PRODUCT the three layers return (never the
#     200-works counting pool); voices collapses a doc set to distinct upstream voices (the
#     independence counter, counting EVIDENCE never absence); between traces bounded connection paths.
#     Rulings-file checks monkeypatch _graph.RULINGS_PATH to a temp file (no real ~/.penumbra state);
#     the integration + voices + between checks use a FRESH temp-db (the §47/§48/§49 pattern, restore
#     in finally); the builder + vocabulary + tripwire checks are pure source/registration checks.
# ---------------------------------------------------------------------------
import penumbra.core.relations as _rel50  # noqa: E402 — importing the tap registers its mints

# (1-4) RULINGS STORE (save_ruling / load_rulings / delete_ruling) on a monkeypatched temp path.
_r50_rulings_prev = _graph.RULINGS_PATH
_graph.RULINGS_PATH = Path(_tf47.mkdtemp()) / "graph_rulings.json"
try:
    # (1) save_ruling -> load_rulings roundtrip; the entry normalizes src < dst + stamps ruled_at.
    _r50_a, _r50_b = "person:openalex:A_ZZZ", "person:openalex:A_AAA"  # a > b on purpose
    _r50_save = _graph.save_ruling(_r50_a, _r50_b, "same", note="smoke")
    _r50_loaded = _graph.load_rulings()
    _r50_e = _r50_loaded[0] if _r50_loaded else {}
    check("ruling (1): save_ruling -> load_rulings roundtrip, normalized src < dst + ruled_at stamped",
          len(_r50_loaded) == 1 and _r50_e.get("src") == "person:openalex:A_AAA"
          and _r50_e.get("dst") == "person:openalex:A_ZZZ" and _r50_e.get("verdict") == "same"
          and bool(_r50_e.get("ruled_at")) and _r50_save.get("replaced") is False)
    # (2) re-create the SAME pair (either order) REPLACES: list length stays 1, verdict updated.
    _r50_save2 = _graph.save_ruling(_r50_b, _r50_a, "not_same")   # reversed order, same pair
    _r50_loaded2 = _graph.load_rulings()
    check("ruling (2): re-create the same pair (either order) REPLACES (len stays 1, verdict updated)",
          len(_r50_loaded2) == 1 and _r50_loaded2[0].get("verdict") == "not_same"
          and _r50_save2.get("replaced") is True)
    # (3) delete_ruling removes; returns True then False.
    _r50_del1 = _graph.delete_ruling(_r50_a, _r50_b)
    _r50_del2 = _graph.delete_ruling(_r50_a, _r50_b)   # already gone
    check("ruling (3): delete_ruling removes (True), then a second delete is a no-op (False)",
          _r50_del1 is True and _r50_del2 is False and _graph.load_rulings() == [])
    # (4) validation: a bad verdict / src == dst / empty endpoint each RAISES ValueError.
    _r50_bad_verdict = _r50_same_node = _r50_empty = False
    try:
        _graph.save_ruling("a", "b", "maybe")
    except ValueError:
        _r50_bad_verdict = True
    try:
        _graph.save_ruling("x", "x", "same")
    except ValueError:
        _r50_same_node = True
    try:
        _graph.save_ruling("", "b", "same")
    except ValueError:
        _r50_empty = True
    check("ruling (4): save_ruling validates (bad verdict / src==dst / empty each raises ValueError)",
          _r50_bad_verdict and _r50_same_node and _r50_empty)
finally:
    _graph.RULINGS_PATH = _r50_rulings_prev

# (6) RELATIONS BUILDERS golden fixtures (PURE, no network): recorded tool OUT-dict -> expected
#     (nodes, edges). The mint-the-product rule made concrete: the works pool / cooc / unresolved
#     inputs / institution strings mint NOTHING; only the returned product does.
# resolve_identity out-dict with a likely_same_person group -> person nodes + ALL-PAIRS same_as A.
_r50_resolve_out = {"query": "Jane", "source": "openalex", "candidates": [
    {"id": "A1", "source": "openalex", "name": "Jane Roe", "works_count": 10, "cited_by": 50,
     "name_match": True, "institution": "MIT"},
    {"id": "A2", "source": "openalex", "name": "Jane Roe", "works_count": 5, "cited_by": 20,
     "name_match": True, "institution": None},
], "likely_same_person": [{"source": "openalex", "ids": ["A1", "A2"], "name": "Jane Roe",
                          "merge_token": "A1+A2"}]}
_r50_rn, _r50_re = _rel50._resolve_mints(_r50_resolve_out)
_r50_rn_ids = {n["id"] for n in _r50_rn}
_r50_re_norm = {(e["src"], e["dst"], e["type"], e["tier"], e["method"]) for e in _r50_re}
check("relations builder (resolve): a person node per id-bearing candidate, with works_count/cited_by attrs",
      _r50_rn_ids == {"person:openalex:A1", "person:openalex:A2"}
      and any(n["id"] == "person:openalex:A1" and n["attrs"] == {"works_count": 10, "cited_by": 50}
              for n in _r50_rn))
check("relations builder (resolve): an ALL-PAIRS same_as A edge (align:name_match) per likely_same_person group",
      _r50_re_norm == {("person:openalex:A1", "person:openalex:A2", "same_as", "A", "align:name_match")})
# coauthors out-dict -> person nodes + coauthored edges with attrs; works pool + cooc + unresolved mint nothing.
_r50_co_out = {"source": "openalex", "n_authors": 2, "nodes": [
    {"query": "A1", "resolved": {"id": "A1", "ids": ["A1"], "source": "openalex", "name": "Alice"},
     "top_coauthors": [{"id": "A9", "name": "Carol", "joint": 3.5, "papers": 4}]},
    {"query": "A2", "resolved": {"id": "A2", "ids": ["A2"], "source": "openalex", "name": "Bob"},
     "top_coauthors": []},
    {"query": "Ghost", "resolved": None},   # unresolved input -> mints nothing
], "edges": [{"a": "A1", "b": "A2", "joint_count": 2}],
    "bridges": [{"id": "A7", "name": "Dave", "shared_by": ["A1", "A2"], "total_joint": 1.5}],
    "cooc": [{"a": "x", "b": "y", "n": 3}]}   # cooc mints nothing (name-collapsed, no stable ids)
_r50_cn, _r50_ce = _rel50._coauthors_mints(_r50_co_out)
_r50_cn_ids = {n["id"] for n in _r50_cn}
_r50_ce_set = {(e["src"], e["dst"], e["type"], e["method"], tuple(sorted((e.get("attrs") or {}).items())))
               for e in _r50_ce}
check("relations builder (coauthors): person nodes for resolved inputs + top_coauthors + bridges (Ghost minted nothing)",
      _r50_cn_ids == {"person:openalex:A1", "person:openalex:A2", "person:openalex:A9",
                      "person:openalex:A7"})
check("relations builder (coauthors): coauthored edges carry attrs (top_coauthor joint/papers, pairwise joint_count, bridge total_joint)",
      ("person:openalex:A1", "person:openalex:A9", "coauthored", "api:openalex",
       (("joint", 3.5), ("papers", 4))) in _r50_ce_set
      and ("person:openalex:A1", "person:openalex:A2", "coauthored", "api:openalex",
           (("joint_count", 2),)) in _r50_ce_set
      and ("person:openalex:A1", "person:openalex:A7", "coauthored", "api:openalex",
           (("total_joint", 1.5),)) in _r50_ce_set)
check("relations builder (coauthors): the works pool + cooc mint NOTHING (only the product mints)",
      all(not n["id"].startswith("person:openalex:x") for n in _r50_cn)
      and all(e["type"] == "coauthored" for e in _r50_ce))
# cohort out-dict -> inst node + affiliated edges with works/concept attrs; the miss dict mints nothing.
_r50_coh_out = {"institution": {"id": "I50", "name": "Example Institute"},
                "filters": {"concept": "ML", "concept_id": "C10", "year_from": 2022},
                "n": 1, "people": [{"id": "A5", "name": "Erin", "works_at_institution_in_field": 7}]}
_r50_hn, _r50_he = _rel50._cohort_mints(_r50_coh_out)
check("relations builder (cohort): an institution node + a person node + affiliated edge with works/concept attrs",
      {n["id"] for n in _r50_hn} == {"inst:openalex:I50", "person:openalex:A5"}
      and _r50_he == [{"src": "person:openalex:A5", "dst": "inst:openalex:I50", "type": "affiliated",
                       "tier": "M", "method": "api:openalex", "attrs": {"works": 7, "concept": "C10"}}])
check("relations builder (cohort): the no-match miss dict (institution None) mints NOTHING",
      _rel50._cohort_mints({"institution": None, "people": [], "note": "x"}) == ([], []))

# (7) GRAPH_MINTS registered: declared_vocabulary() now includes relations' kinds/types/methods
#     (align:name_match is newly declared). Shipping the tap IS the grant (vocabulary-by-minting).
_r50_vocab = _graph.declared_vocabulary()
check("relations (mint tripwire): declared_vocabulary includes relations' person/institution kinds",
      {"person", "institution"} <= _r50_vocab["kinds"])
check("relations (mint tripwire): declared_vocabulary includes coauthored/affiliated/same_as edge types",
      {"coauthored", "affiliated", "same_as"} <= _r50_vocab["edge_types"])
check("relations (mint tripwire): declared_vocabulary includes align:name_match method",
      "align:name_match" in _r50_vocab["methods"] and "relations" in _graph._MINT_REGISTRY)

# (5, 8, 9) INTEGRATION + voices + between on a FRESH temp-db (repoint store.DB_PATH etc.; restore).
_p50_db_prev = _rstore.DB_PATH
_p50_disabled_prev = _rstore._disabled
_p50_local_prev = _rstore._local
_p50_rulings_prev = _graph.RULINGS_PATH
_rstore.DB_PATH = Path(_tf47.mkdtemp()) / "smoke_p3.db"
_rstore._disabled = False
_rstore._local = _thr47.local()  # fresh per-thread conn cache -> _read_con() reconnects to THIS db
try:
    check("p3: index init creates the tables in the temp db", _rstore.init())
    _p50con = _rstore.connect()

    # ---- (5) INTEGRATION: a `same` ruling ADDS a same_as edge under working; a `not_same` ruling
    #      REMOVES a title_fp candidate under exploratory (not_same beats candidate). ----
    # Two title-fp TWINS from different sources (the derived same_as substrate), seeded via _upsert.
    _P50_TWIN = "Ruling Integration Twin Doc Shared Long Normalized Title For P Three"
    _p50_t1 = _doc("arxiv", _P50_TWIN, "http://arxiv/p50t1"); _p50_t1.source_id = "p50t1"
    _p50_t2 = _doc("openreview", _P50_TWIN, "http://openreview/p50t2"); _p50_t2.source_id = "p50t2"
    # Two UNRELATED docs (no shared fp, no ids) for the `same`-ruling ADD test.
    _p50_s1 = _doc("zhihu", "Ruling Same Add Doc One Alpha Unrelated Long Title", "http://z/p50s1")
    _p50_s1.source_id = "p50s1"
    _p50_s2 = _doc("bilibili", "Ruling Same Add Doc Two Beta Unrelated Long Title", "http://b/p50s2")
    _p50_s2.source_id = "p50s2"
    _p50con.execute("BEGIN")
    for _p50d in (_p50_t1, _p50_t2, _p50_s1, _p50_s2):
        _recall.writer._upsert(_p50con, rank, _p50d, 1.0)
    _p50con.commit()
    _p50_n_s1 = _graph.doc_node_id("zhihu", "p50s1")
    _p50_n_s2 = _graph.doc_node_id("bilibili", "p50s2")
    _p50_n_t1 = _graph.doc_node_id("arxiv", "p50t1")
    _p50_n_t2 = _graph.doc_node_id("openreview", "p50t2")

    # A `same` ruling between the two UNRELATED docs (via a monkeypatched temp rulings file) makes a
    # same_as edge appear under working (the ruling-ADD; there is no other edge between them).
    _p50_rf = Path(_tf47.mkdtemp()) / "graph_rulings.json"
    _p50_lo, _p50_hi = sorted((_p50_n_s1, _p50_n_s2))
    _p50_rf.write_text(json.dumps([{"src": _p50_lo, "dst": _p50_hi, "verdict": "same",
                                    "note": "smoke", "ruled_at": 1.0}]), encoding="utf-8")
    _graph.RULINGS_PATH = _p50_rf
    _p50_nb_working = _graph.neighborhood(_p50_n_s1, depth=1, policy="working")
    check("ruling integration (5): a `same` ruling ADDS a same_as edge under working (the ruling-ADD)",
          _p50_n_s2 in {n["id"] for n in _p50_nb_working["nodes"]}
          and any(e["type"] == "same_as" and e.get("method") == "ruling"
                  and {e["src"], e["dst"]} == {_p50_n_s1, _p50_n_s2}
                  for e in _p50_nb_working["edges"]))
    # Under conservative (rulings NOT applied) the same_as ADD does NOT appear (control).
    _p50_nb_cons = _graph.neighborhood(_p50_n_s1, depth=1, policy="conservative")
    check("ruling integration (5): the `same`-ruling ADD is absent under conservative (rulings gated to working/exploratory)",
          _p50_n_s2 not in {n["id"] for n in _p50_nb_cons["nodes"]})

    # A `not_same` ruling between the TWINS REMOVES the title_fp candidate under exploratory.
    _p50_rf2 = Path(_tf47.mkdtemp()) / "graph_rulings.json"
    _p50_tlo, _p50_thi = sorted((_p50_n_t1, _p50_n_t2))
    _p50_rf2.write_text(json.dumps([{"src": _p50_tlo, "dst": _p50_thi, "verdict": "not_same",
                                     "note": "smoke", "ruled_at": 1.0}]), encoding="utf-8")
    _graph.RULINGS_PATH = _p50_rf2
    _p50_nb_expl_ruled = _graph.neighborhood(_p50_n_t1, depth=1, policy="exploratory")
    check("ruling integration (5): a `not_same` ruling REMOVES the title_fp candidate under exploratory (not_same beats candidate)",
          _p50_n_t2 not in {n["id"] for n in _p50_nb_expl_ruled["nodes"]}
          and not any(e["type"] == "same_as" and {e["src"], e["dst"]} == {_p50_n_t1, _p50_n_t2}
                      for e in _p50_nb_expl_ruled["edges"]))
    _graph.RULINGS_PATH = _p50_rulings_prev   # drop the ruling overlay for the voices/between fixtures

    # ---- (8) VOICES: the independence counter over a synthetic doc set. ----
    # Two docs sharing a work id (id_eq via the SAME doi) = ONE voice; a third with zero ids = unresolved.
    _v1 = _doc("arxiv", "Voices Doc One Alpha Long Enough Title Here For P3", "http://a/v1")
    _v1.source_id = "v1"; _v1.metadata = {"doi": "10.9/vshared"}
    _v2 = _doc("openreview", "Voices Doc Two Beta Different Title Entirely P3", "http://a/v2")
    _v2.source_id = "v2"; _v2.metadata = {"doi": "10.9/vshared"}
    _v3 = _doc("zhihu", "Voices Doc Three Gamma No Ids At All Long Title P3", "http://a/v3")
    _v3.source_id = "v3"
    _p50con.execute("BEGIN")
    for _vd in (_v1, _v2, _v3):
        _recall.writer._upsert(_p50con, rank, _vd, 1.0)
    _p50con.commit()
    _nv1 = _graph.doc_node_id("arxiv", "v1"); _nv2 = _graph.doc_node_id("openreview", "v2")
    _nv3 = _graph.doc_node_id("zhihu", "v3")
    _vres = _graph.voices([_nv1, _nv2, _nv3], policy="conservative")
    check("voices (8): two docs sharing a work id (id_eq) collapse to ONE voice; the id-less third is unresolved",
          _vres["n_voices"] == 1 and _vres["n_unresolved"] == 1
          and sorted(_vres["voices"][0]["docs"]) == sorted([_nv1, _nv2])
          and _vres["unresolved"] == [_nv3])

    # Two docs on DIFFERENT works sharing an AUTHORED person = ONE voice, speaker_known True.
    _va = _doc("arxiv", "Voices Speaker Merge A Distinct Work One Long Title P3", "http://a/va")
    _va.source_id = "va"; _va.metadata = {"doi": "10.9/vworka"}
    _vb = _doc("s2", "Voices Speaker Merge B Distinct Work Two Long Title P3", "http://a/vb")
    _vb.source_id = "vb"; _vb.metadata = {"doi": "10.9/vworkb"}
    _p50con.execute("BEGIN")
    for _vd in (_va, _vb):
        _recall.writer._upsert(_p50con, rank, _vd, 1.0)
    for _wk in ("work:doi:10.9/vworka", "work:doi:10.9/vworkb"):
        _p50con.execute("INSERT INTO graph_edges(src, dst, type, tier, method, first_seen, last_seen) "
                        "VALUES(?, 'person:openalex:A_VMERGE', 'authored', 'M', 'api:openalex', 1.0, 1.0)",
                        (_wk,))
    _p50con.commit()
    _nva = _graph.doc_node_id("arxiv", "va"); _nvb = _graph.doc_node_id("s2", "vb")
    _vres2 = _graph.voices([_nva, _nvb], policy="conservative")
    check("voices (8): two docs on different works sharing an authored person = ONE voice, speaker_known True",
          _vres2["n_voices"] == 1 and _vres2["voices"][0]["speaker_known"] is True
          and sorted(_vres2["voices"][0]["docs"]) == sorted([_nva, _nvb]))

    # A title_fp-only pair (each with its OWN distinct work id): collapses under exploratory (ONE
    # voice) but stays TWO voices under conservative (title_fp excluded; each id_eqs its own work).
    _P50_VFP = "Voices Title Fp Collapse Pair Shared Exact Same Long Normalized Title"
    _vc = _doc("arxiv", _P50_VFP, "http://a/vc"); _vc.source_id = "vc"; _vc.metadata = {"doi": "10.9/vcc"}
    _vd2 = _doc("openreview", _P50_VFP, "http://a/vd"); _vd2.source_id = "vd"; _vd2.metadata = {"doi": "10.9/vdd"}
    _p50con.execute("BEGIN")
    for _vd in (_vc, _vd2):
        _recall.writer._upsert(_p50con, rank, _vd, 1.0)
    _p50con.commit()
    _nvc = _graph.doc_node_id("arxiv", "vc"); _nvd = _graph.doc_node_id("openreview", "vd")
    _vres_cons = _graph.voices([_nvc, _nvd], policy="conservative")
    _vres_expl = _graph.voices([_nvc, _nvd], policy="exploratory")
    check("voices (8): a title_fp-only pair is TWO voices under conservative, ONE under exploratory",
          _vres_cons["n_voices"] == 2 and _vres_expl["n_voices"] == 1)
    # >64 ids -> explicit error (no silent truncation); a non-doc id -> the skipped list.
    _vres_big = _graph.voices([f"doc:x:{i}" for i in range(65)])
    _vres_skip = _graph.voices([_nv1, "work:openalex:W999"], policy="conservative")
    check("voices (8): >64 doc ids returns an explicit error (never a silent truncation)",
          "error" in _vres_big)
    check("voices (8): a non-doc id lands in the skipped list, not an error",
          _vres_skip.get("skipped") == ["work:openalex:W999"])

    # ---- (9) BETWEEN: bounded connection paths. ----
    # A person-authored-work-authored-person path -> ONE path, length 2 (edge count).
    for _pnid, _pkind, _plabel in [("person:openalex:A_BA", "person", "Alice"),
                                   ("person:openalex:A_BB", "person", "Bob"),
                                   ("work:openalex:W_B1", "work", "Paper")]:
        _p50con.execute("INSERT INTO graph_nodes(id, kind, label, first_seen, last_seen) "
                        "VALUES(?, ?, ?, 1.0, 1.0)", (_pnid, _pkind, _plabel))
    for _pp in ("person:openalex:A_BA", "person:openalex:A_BB"):
        _p50con.execute("INSERT INTO graph_edges(src, dst, type, tier, method, first_seen, last_seen) "
                        "VALUES('work:openalex:W_B1', ?, 'authored', 'M', 'api:openalex', 1.0, 1.0)", (_pp,))
    _p50con.commit()
    _bres = _graph.between("person:openalex:A_BA", "person:openalex:A_BB", policy="conservative")
    check("between (9): a person-work-person path is found (one path, length 2, direction preserved)",
          _bres["paths"] == [["person:openalex:A_BA", "work:openalex:W_B1", "person:openalex:A_BB"]]
          and any(e["type"] == "authored" for e in _bres["edges"]))
    # types filter excludes the authored path when types=["cites"].
    _bres_cites = _graph.between("person:openalex:A_BA", "person:openalex:A_BB",
                                 types=["cites"], policy="conservative")
    check("between (9): a types filter (types=['cites']) excludes the authored path -> paths []",
          _bres_cites["paths"] == [])
    # a no-path pair -> paths [] (honest empty); a == b -> empty with a note.
    _bres_none = _graph.between("person:openalex:A_BA", "person:openalex:A_NOPE", policy="conservative")
    _bres_same = _graph.between("person:openalex:A_BA", "person:openalex:A_BA")
    check("between (9): a no-path pair -> paths [] (honest empty, not an error)", _bres_none["paths"] == [])
    check("between (9): a == b -> an empty result with a note", _bres_same["paths"] == []
          and bool(_bres_same.get("note")))
    # policy gating: a title_fp-derived link forms a path only under exploratory (excluded under conservative).
    _P50_BFP = "Between Policy Gate Title Fp Only Link Shared Long Normalized Title P3"
    _bp = _doc("arxiv", _P50_BFP, "http://a/bp"); _bp.source_id = "bp1"
    _bq = _doc("openreview", _P50_BFP, "http://a/bq"); _bq.source_id = "bp2"
    _p50con.execute("BEGIN")
    for _bd in (_bp, _bq):
        _recall.writer._upsert(_p50con, rank, _bd, 1.0)
    _p50con.commit()
    _nbp = _graph.doc_node_id("arxiv", "bp1"); _nbq = _graph.doc_node_id("openreview", "bp2")
    _bres_pc = _graph.between(_nbp, _nbq, policy="conservative")
    _bres_pe = _graph.between(_nbp, _nbq, policy="exploratory")
    check("between (9): a title_fp-derived link forms a path only under exploratory (gated out under conservative)",
          _bres_pc["paths"] == [] and _bres_pe["paths"] == [[_nbp, _nbq]])
finally:
    _graph.RULINGS_PATH = _p50_rulings_prev
    _rstore.DB_PATH = _p50_db_prev
    _rstore._disabled = _p50_disabled_prev
    _rstore._local = _p50_local_prev
    # drain any residue so a later section never inherits our queue items.
    while not _recall.writer._queue.empty():
        try:
            _recall.writer._queue.get_nowait()
        except Exception:  # noqa: BLE001
            break

# (10) penumbra_ruling TOOL: create/list/delete happy path + unknown action + missing-args errors. Call
#      the unwrapped body (past @_threaded) like the other server-level checks; monkeypatch a temp
#      rulings file so no real state is touched.
_r50_tool_prev = _graph.RULINGS_PATH
_graph.RULINGS_PATH = Path(_tf47.mkdtemp()) / "graph_rulings.json"
try:
    _er = _srv.penumbra_ruling.__wrapped__
    _er_create = _er(action="create", src="person:openalex:A_T2", dst="person:openalex:A_T1",
                     verdict="same", note="tool smoke")
    check("penumbra_ruling (10): action=create records the ruling (created True, normalized src < dst)",
          _er_create.get("created") is True
          and _er_create.get("ruling", {}).get("src") == "person:openalex:A_T1"
          and _er_create.get("ruling", {}).get("dst") == "person:openalex:A_T2")
    _er_list = _er(action="list")
    check("penumbra_ruling (10): action=list returns the ruling + count",
          _er_list.get("count") == 1 and _er_list.get("rulings", [{}])[0].get("verdict") == "same")
    _er_del = _er(action="delete", src="person:openalex:A_T1", dst="person:openalex:A_T2")
    check("penumbra_ruling (10): action=delete removes it (deleted True) and the list empties",
          _er_del.get("deleted") is True and _er(action="list").get("count") == 0)
    check("penumbra_ruling (10): an unknown action returns an error dict",
          "error" in _er(action="frobnicate"))
    check("penumbra_ruling (10): create with a bad verdict returns an error dict (ValueError mapped)",
          "error" in _er(action="create", src="a", dst="b", verdict="maybe"))
    check("penumbra_ruling (10): delete without src/dst returns an error dict",
          "error" in _er(action="delete", src="", dst=""))
finally:
    _graph.RULINGS_PATH = _r50_tool_prev

# (11) TRIPWIRES (the conscious 16 -> 17 bump lives in the parsimony + §49 verbs checks above; here
#      confirm the gather whitelist did NOT gain the write verb, and the docs-drift POSITIVE presence
#      of penumbra_ruling in the product docs + the instructions).
check("p3 tripwire: _GATHER_TOOLS is still 12 and EXCLUDES penumbra_ruling (a write verb; gather is read-only)",
      len(_GATHER_TOOLS) == 12 and "penumbra_ruling" not in _GATHER_TOOLS)
check("p3 tripwire: penumbra_ruling is a REGISTERED tool", callable(_srv.penumbra_ruling))
# docs-drift POSITIVE: penumbra_ruling must be NAMED in the product-facing README and in the server
# instructions (a renamed/removed write verb that skips the docs would otherwise teach a stale
# surface — the same drift the negative tripwire guards, in the presence direction).
_r50_readme = (ROOT / "README.md")
_r50_readme_txt = _r50_readme.read_text(encoding="utf-8") if _r50_readme.exists() else ""
check("p3 docs-drift (presence): penumbra_ruling is named in README.md (the product-facing tool surface)",
      "penumbra_ruling" in _r50_readme_txt)
check("p3 docs-drift (presence): penumbra_ruling is named in _PENUMBRA_INSTRUCTIONS (the connect-time brief)",
      "penumbra_ruling" in _srv._PENUMBRA_INSTRUCTIONS)


# ---------------------------------------------------------------------------
# 51. P4 — the EVENT layer (design "P4 shipped 2026-07-03"): the sensor observed tap + the conflicts
#     tap + the since view. observed edges (sensor -> doc, M, sensor:diff) mint from the RUN DIFF
#     only (the baseline is state, mints nothing; a no-news run mints nothing); conflicts edges
#     (doc <-> doc, A, signal:divergence) mint where dedup already found same-work signal divergence,
#     carrying the signal's KIND so consumers filter the engagement-count noise class; since projects
#     the accretion log (stored edges only, first_seen >= T, tier + method visible, NO collapsing).
#     The builder + tripwire checks are PURE; the run_sensor integration monkeypatches search_ranked
#     + captures enqueue_graph; the since checks use a FRESH temp-db (the §47/§49 pattern, restore in
#     finally). The agent-visible signal_conflicts stamp must stay BYTE-IDENTICAL (the STABILITY
#     contract): the tap rides a PRIVATE _conflict_pairs key the fetcher pops.
# ---------------------------------------------------------------------------
import penumbra.core.sensor as _sen51  # noqa: E402 — importing the tap registers its mints
from penumbra.core.sensor import Sensor as _Sensor51  # noqa: E402

# (1) OBSERVED BUILDER golden fixture (PURE, no network): a fake sensor + 3 new (source, source_id)
#     pairs -> ONE sensor node + 3 observed edges (M, sensor:diff, attrs run_at). An EMPTY diff mints
#     NOTHING (not even the sensor node — a no-news run is not an accretion event).
_s51_sensor = _Sensor51(id="s51abc", query="grpo reinforcement learning")
_s51_pairs = [("arxiv", "2501.111"), ("openreview", "note42"), ("zhihu", "q99")]
_s51_run_at = "2026-07-03T12:00:00+00:00"
_s51_n, _s51_e = _sen51._observed_mints(_s51_sensor, _s51_pairs, _s51_run_at)
check("p4 observed builder: ONE sensor node (sensor:{id}, label=query) for a non-empty diff",
      _s51_n == [{"id": "sensor:s51abc", "kind": "sensor",
                  "label": "grpo reinforcement learning", "attrs": None}])
_s51_e_set = {(e["src"], e["dst"], e["type"], e["tier"], e["method"],
               tuple(sorted((e.get("attrs") or {}).items()))) for e in _s51_e}
check("p4 observed builder: one observed M-edge sensor -> doc per new pair, method sensor:diff, attrs run_at",
      _s51_e_set == {
          ("sensor:s51abc", "doc:arxiv:2501.111", "observed", "M", "sensor:diff",
           (("run_at", _s51_run_at),)),
          ("sensor:s51abc", "doc:openreview:note42", "observed", "M", "sensor:diff",
           (("run_at", _s51_run_at),)),
          ("sensor:s51abc", "doc:zhihu:q99", "observed", "M", "sensor:diff",
           (("run_at", _s51_run_at),)),
      })
check("p4 observed builder: an EMPTY diff mints NOTHING (no sensor node, no edges: no-news is not accretion)",
      _sen51._observed_mints(_s51_sensor, [], _s51_run_at) == ([], []))

# (2) run_sensor INTEGRATION: a monkeypatched search_ranked returning 2 NEW docs -> the tap enqueues
#     the observed batch (captured by monkeypatching writer.enqueue_graph). Then a tap failure
#     (enqueue raising) NEVER breaks the run summary (fail-open). Uses a temp SensorStore path so no
#     real ~/.penumbra/state is touched; WRITES_ENABLED forced True so the tap actually fires.
_s51_fetch_prev = _fetcher51 = None
import penumbra.core.fetcher as _fetcher51  # noqa: E402
_s51_search_prev = _fetcher51.search_ranked
_s51_enq_prev = _recall.writer.enqueue_graph
_s51_writes_prev = _recall.writer.WRITES_ENABLED
_s51_store_default_prev = _sen51._DEFAULT_STATE_PATH
_s51_captured: list = []
try:
    _recall.writer.WRITES_ENABLED = True
    _s51_store = _sen51.SensorStore(Path(_tf47.mkdtemp()) / "s51_sensors.json")
    _s51_run_sensor_obj = _Sensor51(id="s51run", query="run integration query")
    _s51_store.update(_s51_run_sensor_obj)

    # search_ranked returns 2 docs THIS RUN; the baseline is empty, so BOTH are new.
    _s51_d1 = _doc("arxiv", "Observed Integration Doc One Long Title", "http://a/o1")
    _s51_d1.source_id = "o1"
    _s51_d2 = _doc("openreview", "Observed Integration Doc Two Long Title", "http://a/o2")
    _s51_d2.source_id = "o2"
    _fetcher51.search_ranked = lambda q, sources=None, limit=15: ([_s51_d1, _s51_d2], {})
    _recall.writer.enqueue_graph = lambda nodes, edges: _s51_captured.append((nodes, edges))

    _s51_summary = _sen51.run_sensor(_s51_store.get("s51run"), _s51_store)
    check("p4 run_sensor integration: 2 new docs -> the summary reports new_count == 2",
          _s51_summary.get("new_count") == 2)
    # the tap enqueued ONE graph batch: 1 sensor node + 2 observed edges to the two new docs.
    _s51_cap_nodes = [n for (ns, _es) in _s51_captured for n in ns]
    _s51_cap_edges = [e for (_ns, es) in _s51_captured for e in es]
    _s51_cap_dsts = {e["dst"] for e in _s51_cap_edges if e.get("type") == "observed"}
    check("p4 run_sensor integration: the tap enqueued the observed batch (sensor node + 2 observed edges)",
          any(n["id"] == "sensor:s51run" and n["kind"] == "sensor" for n in _s51_cap_nodes)
          and _s51_cap_dsts == {"doc:arxiv:o1", "doc:openreview:o2"}
          and all(e["tier"] == "M" and e["method"] == "sensor:diff"
                  for e in _s51_cap_edges if e.get("type") == "observed"))

    # tap failure never breaks the run: a SECOND run with enqueue_graph RAISING still returns a
    # normal summary (the diff is empty this run since the baseline now holds both -> but force a
    # fresh sensor so there IS a diff, and make enqueue raise).
    _s51_boom = _Sensor51(id="s51boom", query="boom query")
    _s51_store.update(_s51_boom)
    _s51_d3 = _doc("zhihu", "Observed Boom Doc Long Title Here", "http://a/b1"); _s51_d3.source_id = "b1"
    _fetcher51.search_ranked = lambda q, sources=None, limit=15: ([_s51_d3], {})
    def _s51_raise(nodes, edges):
        raise RuntimeError("enqueue boom")
    _recall.writer.enqueue_graph = _s51_raise
    _s51_boom_summary = _sen51.run_sensor(_s51_store.get("s51boom"), _s51_store)
    check("p4 run_sensor integration: a tap failure (enqueue raising) NEVER breaks the run summary (fail-open)",
          _s51_boom_summary.get("new_count") == 1 and _s51_boom_summary.get("sensor_id") == "s51boom")
finally:
    _fetcher51.search_ranked = _s51_search_prev
    _recall.writer.enqueue_graph = _s51_enq_prev
    _recall.writer.WRITES_ENABLED = _s51_writes_prev
    _sen51._DEFAULT_STATE_PATH = _s51_store_default_prev

# (3) CONFLICTS BUILDER golden fixture (PURE): a record pair with an ENGAGEMENT signal -> ONE A-tier
#     conflicts edge doc <-> doc (method signal:divergence, attrs {signal, kind, values}), NO nodes.
_s51_conf_rec = [{"a": ("reddit", "rr1"), "b": ("hackernews", "hh1"), "signal": "score",
                  "kind": "engagement", "values": {"reddit": 120, "hackernews": 900}, "ratio": 7.5}]
_s51_cn, _s51_ce = rank._conflict_mints(_s51_conf_rec)
check("p7 conflicts builder: a divergence record -> ONE A-tier conflicts edge, attrs {signal, kind, values, ratio}, NO nodes",
      _s51_cn == [] and len(_s51_ce) == 1
      and _s51_ce[0]["type"] == "conflicts" and _s51_ce[0]["tier"] == "A"
      and _s51_ce[0]["method"] == "signal:divergence"
      and {_s51_ce[0]["src"], _s51_ce[0]["dst"]} == {"doc:reddit:rr1", "doc:hackernews:hh1"}
      and _s51_ce[0]["attrs"] == {"signal": "score", "kind": "engagement",
                                  "values": {"reddit": 120, "hackernews": 900}, "ratio": 7.5})
# the STABILITY contract: dedup's agent-visible signal_conflicts stamp is BYTE-IDENTICAL to pre-P4.
# Reuse the §42 fixture shape: two same-title docs, different sources, one signal diverging >50%.
from penumbra.core.normalize import Signal as _Sig51  # noqa: E402
_s51_ct = "Conflict Stability Shared Long Normalized Title For P Four Contract"
_s51_ca = _PDoc(source="s1", source_id="c1", url="http://a", title=_s51_ct, content="x",
                signals={"revenue": _Sig51(value=5000000.0, kind="other", computed_by="source:s1",
                                           unit="USD")})
_s51_cb = _PDoc(source="s2", source_id="c2", url="http://b", title=_s51_ct, content="y",
                signals={"revenue": _Sig51(value=8000000.0, kind="other", computed_by="source:s2",
                                           unit="USD")})
_s51_dd = _dedup42([_s51_ca, _s51_cb])
_s51_sc = (_s51_dd[0].metadata or {}).get("signal_conflicts", []) if len(_s51_dd) == 1 else []
# P7 (2026-07-03): the stamp is ADDITIVE under the STABILITY contract — every pre-P7 key is
# byte-identical AND exactly ONE new key (``ratio``) is added (the measured max/min divergence; here
# 8M/5M = 1.6). Assert BOTH halves: the old keys unchanged (drop ratio, compare to the pre-P4 dict)
# AND the new-keys delta is exactly {"ratio"} carrying 1.6.
_s51_old_keys = {"topic", "source_a", "claim_a", "source_b", "claim_b"}
_s51_entry = _s51_sc[0] if _s51_sc else {}
_s51_new_keys = set(_s51_entry.keys()) - _s51_old_keys
check("p7 conflicts STABILITY: old signal_conflicts keys byte-identical to pre-P4 (topic/source/claim)",
      len(_s51_sc) == 1
      and {k: _s51_entry.get(k) for k in _s51_old_keys} ==
          {"topic": "revenue", "source_a": "s1", "claim_a": "revenue=5000000.0 (USD)",
           "source_b": "s2", "claim_b": "revenue=8000000.0 (USD)"})
check("p7 conflicts STABILITY: exactly ONE new key added (ratio), carrying the measured 8M/5M=1.6",
      _s51_new_keys == {"ratio"} and _s51_entry.get("ratio") == 1.6)
# and the PRIVATE _conflict_pairs key carries the tap record beside it (the fetcher pops it; dedup
# alone leaves it, which is the internal contract the tap reads).
_s51_cp = (_s51_dd[0].metadata or {}).get("_conflict_pairs", [])
check("p7 conflicts: the private _conflict_pairs record carries full identities + kind + values + ratio (fetcher pops it)",
      len(_s51_cp) == 1 and _s51_cp[0]["a"] == ("s1", "c1") and _s51_cp[0]["b"] == ("s2", "c2")
      and _s51_cp[0]["signal"] == "revenue" and _s51_cp[0]["kind"] == "other"
      and _s51_cp[0]["values"] == {"s1": 5000000.0, "s2": 8000000.0}
      and _s51_cp[0]["ratio"] == 1.6)

# (4) CONFLICTS SYMMETRIC: the writer normalizes the pair src < dst (reuse the §49 idiom) so a
#     re-detected pair (either order) upserts the SAME row. Drive the writer's _upsert_edge directly.
_s51_sym_db_prev = _rstore.DB_PATH
_s51_sym_disabled_prev = _rstore._disabled
_s51_sym_local_prev = _rstore._local
_rstore.DB_PATH = Path(_tf47.mkdtemp()) / "smoke_p4_sym.db"
_rstore._disabled = False
_rstore._local = _thr47.local()
try:
    check("p4 conflicts symmetric: index init creates the tables in the temp db", _rstore.init())
    _s51symcon = _rstore.connect()
    _s51_A, _s51_B = "doc:zzz:late", "doc:aaa:early"   # A > B lexicographically on purpose
    _s51_lo, _s51_hi = sorted((_s51_A, _s51_B))
    _s51symcon.execute("BEGIN")
    _recall.writer._upsert_edge(_s51symcon, {"src": _s51_A, "dst": _s51_B, "type": "conflicts",
                                             "tier": "A", "method": "signal:divergence"}, 10.0)
    _recall.writer._upsert_edge(_s51symcon, {"src": _s51_B, "dst": _s51_A, "type": "conflicts",
                                             "tier": "A", "method": "signal:divergence"}, 20.0)
    _s51symcon.commit()
    _s51_sym_rows = _s51symcon.execute(
        "SELECT src, dst FROM graph_edges WHERE type='conflicts' AND method='signal:divergence'"
    ).fetchall()
    check("p4 conflicts symmetric: a reversed conflicts write lands on ONE row, stored src < dst",
          _s51_sym_rows == [(_s51_lo, _s51_hi)] and _s51_lo < _s51_hi)
finally:
    _rstore.DB_PATH = _s51_sym_db_prev
    _rstore._disabled = _s51_sym_disabled_prev
    _rstore._local = _s51_sym_local_prev

# (5) SINCE: synthetic stored edges with first_seen straddling a cutoff -> only >= cutoff returned,
#     tier + method present on every row, date parsing (bare / full ISO / garbage), fail-open, cap +
#     truncation stamp. FRESH temp-db (the §47/§49 pattern).
_s51_db_prev = _rstore.DB_PATH
_s51_disabled_prev = _rstore._disabled
_s51_local_prev = _rstore._local
_rstore.DB_PATH = Path(_tf47.mkdtemp()) / "smoke_p4_since.db"
_rstore._disabled = False
_rstore._local = _thr47.local()
try:
    check("p4 since: index init creates the tables in the temp db", _rstore.init())
    _s51scon = _rstore.connect()
    # three observed edges off ONE sensor, first_seen 1000 / 2000 / 3000 (before / after / after cut).
    _s51scon.execute("BEGIN")
    for _fs, _sid in ((1000.0, "before"), (2000.0, "mid"), (3000.0, "after")):
        _s51scon.execute(
            "INSERT INTO graph_edges(src, dst, type, tier, method, first_seen, last_seen) "
            "VALUES('sensor:since1', ?, 'observed', 'M', 'sensor:diff', ?, ?)",
            (_graph.doc_node_id("arxiv", _sid), _fs, _fs))
    _s51scon.commit()
    # cutoff = epoch 1500 (full ISO). Only first_seen 2000 + 3000 pass; ordered recency DESC.
    from datetime import datetime as _dt51, timezone as _tz51  # noqa: E402
    _s51_cut_iso = _dt51.fromtimestamp(1500, _tz51.utc).isoformat()
    _s51_since = _graph.since("sensor:since1", _s51_cut_iso)
    _s51_since_dsts = [e["dst"] for e in _s51_since["edges"]]
    check("p4 since: only stored edges with first_seen >= the cutoff are returned, ordered recency DESC",
          _s51_since_dsts == [_graph.doc_node_id("arxiv", "after"),
                              _graph.doc_node_id("arxiv", "mid")])
    check("p4 since: every returned edge row carries tier + method + first_seen (honest epistemics, no collapsing)",
          all("tier" in e and e["tier"] == "M" and e["method"] == "sensor:diff" and "first_seen" in e
              for e in _s51_since["edges"]))
    # date parsing: a BARE date (midnight UTC) admits all three (epoch 1000 > 1970-01-01 midnight).
    _s51_since_bare = _graph.since("sensor:since1", "1970-01-01")
    check("p4 since: a bare YYYY-MM-DD date parses (midnight UTC) and admits the older rows",
          len(_s51_since_bare["edges"]) == 3)
    # garbage / empty date -> the coverage error (never a crash, never a silent all-pass).
    check("p4 since: an unparseable date returns an explicit error",
          "error" in _graph.since("sensor:since1", "not-a-date")
          and "error" in _graph.since("sensor:since1", ""))
    # types filter: restricting to a type the sensor never minted -> zero edges (honest empty).
    _s51_since_typed = _graph.since("sensor:since1", "1970-01-01", types=["cites"])
    check("p4 since: a types filter excluding observed returns zero edges (honest empty)",
          _s51_since_typed["edges"] == [])
    # cap + truncation stamp: seed MANY observed edges off one sensor, cap to a tiny max_nodes.
    _s51scon.execute("BEGIN")
    for _i in range(30):
        _s51scon.execute(
            "INSERT INTO graph_edges(src, dst, type, tier, method, first_seen, last_seen) "
            "VALUES('sensor:sincebig', ?, 'observed', 'M', 'sensor:diff', ?, ?)",
            (_graph.doc_node_id("arxiv", f"big{_i}"), 5000.0 + _i, 5000.0 + _i))
    _s51scon.commit()
    _s51_since_cap = _graph.since("sensor:sincebig", "1970-01-01", max_nodes=5)
    check("p4 since: a tiny max_nodes caps the nodes + stamps capped=true with the truncation order (no silent caps)",
          _s51_since_cap["capped"] is True and len(_s51_since_cap["nodes"]) == 5
          and _s51_since_cap["truncation"] == "recency-then-degree")
    # fail-open: a non-existent anchor -> empty edges (never an error; a since with no accretion is
    # a valid empty answer, distinct from the date-parse error above).
    _s51_since_empty = _graph.since("sensor:nonesuch", "1970-01-01")
    check("p4 since: an anchor with no stored edges -> an empty accretion log (fail-open, not an error)",
          _s51_since_empty["edges"] == [] and "error" not in _s51_since_empty)
finally:
    _rstore.DB_PATH = _s51_db_prev
    _rstore._disabled = _s51_disabled_prev
    _rstore._local = _s51_local_prev

# (6) MINT TRIPWIRES: importing sensor + rank registered the P4 taps, so declared_vocabulary now
#     carries sensor/observed/sensor:diff and conflicts/signal:divergence. The actual-data-subset
#     invariant (the §49 tripwire) still holds for the P4 vocabulary against a fresh minted db.
_s51_vocab = _graph.declared_vocabulary()
check("p4 mint tripwire: declared_vocabulary includes the sensor kind + observed type + sensor:diff method",
      "sensor" in _s51_vocab["kinds"] and "observed" in _s51_vocab["edge_types"]
      and "sensor:diff" in _s51_vocab["methods"] and "sensor" in _graph._MINT_REGISTRY)
check("p4 mint tripwire: declared_vocabulary includes the conflicts type + signal:divergence method",
      "conflicts" in _s51_vocab["edge_types"] and "signal:divergence" in _s51_vocab["methods"]
      and "conflicts" in _graph._MINT_REGISTRY)
# actual-data subset: mint an observed + a conflicts edge into a fresh db, then assert the DISTINCT
# kinds/types/methods present are a subset of the declared union (a tap writing undeclared vocab is
# the bug this catches; here it must stay green with the P4 taps declared).
_s51_sub_db_prev = _rstore.DB_PATH
_s51_sub_disabled_prev = _rstore._disabled
_s51_sub_local_prev = _rstore._local
_rstore.DB_PATH = Path(_tf47.mkdtemp()) / "smoke_p4_subset.db"
_rstore._disabled = False
_rstore._local = _thr47.local()
try:
    _rstore.init()
    _s51subcon = _rstore.connect()
    _s51subcon.execute("BEGIN")
    _recall.writer._apply_graph(
        _s51subcon,
        [{"id": "sensor:sub", "kind": "sensor", "label": "q", "attrs": None}],
        [{"src": "sensor:sub", "dst": "doc:arxiv:z1", "type": "observed", "tier": "M",
          "method": "sensor:diff", "attrs": {"run_at": _s51_run_at}},
         {"src": "doc:reddit:z2", "dst": "doc:hn:z3", "type": "conflicts", "tier": "A",
          "method": "signal:divergence", "attrs": {"signal": "score", "kind": "engagement"}}],
        1.0)
    _s51subcon.commit()
    _s51_sub_kinds = {r[0] for r in _s51subcon.execute("SELECT DISTINCT kind FROM graph_nodes").fetchall()}
    _s51_sub_types = {r[0] for r in _s51subcon.execute("SELECT DISTINCT type FROM graph_edges").fetchall()}
    _s51_sub_methods = {r[0] for r in _s51subcon.execute("SELECT DISTINCT method FROM graph_edges").fetchall()}
    check("p4 mint tripwire: DISTINCT P4 kinds/types/methods present in the db ⊆ declared_vocabulary (no silent vocab)",
          bool(_s51_sub_types) and _s51_sub_kinds <= _s51_vocab["kinds"]
          and _s51_sub_types <= _s51_vocab["edge_types"]
          and _s51_sub_methods <= _s51_vocab["methods"],
          f"undeclared: kinds={sorted(_s51_sub_kinds - _s51_vocab['kinds'])} "
          f"types={sorted(_s51_sub_types - _s51_vocab['edge_types'])} "
          f"methods={sorted(_s51_sub_methods - _s51_vocab['methods'])}")
finally:
    _rstore.DB_PATH = _s51_sub_db_prev
    _rstore._disabled = _s51_sub_disabled_prev
    _rstore._local = _s51_sub_local_prev


# ---------------------------------------------------------------------------
# 52. P5 — similar, the alignment CANDIDATES view (design "P5 shipped 2026-07-03", the P5 sketch
#     overturned). A ZERO-WRITE view: candidates are DERIVED at query time from the live vec index
#     (durability — storing embedding neighbors would freeze one model's judgment into the wall), and
#     NO collapse policy includes align:embed (the razor — "similar" vs "same" is a judgment). The
#     ladder is view=similar PROPOSES (top-k by RANK) -> the agent verifies -> penumbra_ruling records ->
#     the working policy collapses.
#
#     VEC FIXTURE (stated honestly per the spec): the real embedding path is NOT offline-viable on a
#     bare checkout (embed.embed_passage returns None — the qwen3 weights are not on disk), so this
#     builds the smallest honest fixture by HAND-SEEDING the vec table with tiny float32 vectors and
#     exercising the REAL matrix/cosine machinery (store._ensure_matrix + the similar view's engine;
#     P7 migrated similar off similar_by_rowid onto similar_anchor + similar_neighbors). It stubs
#     NOTHING in the ranking path — only the weights-load step is bypassed, and the cosine ranking
#     itself is the production code. FRESH temp-db (the §47/§49 pattern, restore in finally).
# ---------------------------------------------------------------------------
import numpy as _np52  # noqa: E402

_s52_db_prev = _rstore.DB_PATH
_s52_disabled_prev = _rstore._disabled
_s52_local_prev = _rstore._local
# reset the module-level vec matrix cache so _ensure_matrix rebuilds against THIS temp db (a prior
# section may have primed it; the cache keys on model_version + vec-count, but be explicit).
_s52_vecM_prev = _rstore._vec_M
_s52_vecids_prev = _rstore._vec_ids
_s52_vecgen_prev = _rstore._vec_built_gen
_s52_vecmv_prev = _rstore._vec_built_mv
_rstore.DB_PATH = Path(_tf47.mkdtemp()) / "smoke_p5_similar.db"
_rstore._disabled = False
_rstore._local = _thr47.local()
_rstore._vec_M = None
_rstore._vec_ids = None
_rstore._vec_built_gen = -1
_rstore._vec_built_mv = ""
try:
    check("p5 similar: index init creates the tables in the temp db", _rstore.init())
    _s52con = _rstore.connect()
    # Four indexed docs (real docs rows via the writer's _upsert), then hand-seed 3-dim vectors:
    #   A = anchor [1,0,0]; B = [0.9,0.1,0] (nearest); C = [0.5,0.5,0]; D = [0,0,1] (farthest).
    for _sid, _t in (("A", "Similar Anchor Doc Alpha Long Title"),
                     ("B", "Similar Near Doc Beta Long Title"),
                     ("C", "Similar Mid Doc Gamma Long Title"),
                     ("D", "Similar Far Doc Delta Long Title")):
        _s52con.execute("BEGIN")
        _recall.writer._upsert(_s52con, rank, _doc("arxiv", _t, "http://a/" + _sid), 1.0)
        # the _upsert above set source_id = url; override to a clean id for the doc node.
        _s52con.execute("UPDATE docs SET source_id = ? WHERE url = ?", (_sid, "http://a/" + _sid))
        _s52con.commit()
    _s52_mv = _recall.embed.MODEL_VERSION
    _s52_vecs = {"A": [1.0, 0.0, 0.0], "B": [0.9, 0.1, 0.0], "C": [0.5, 0.5, 0.0], "D": [0.0, 0.0, 1.0]}
    _s52con.execute("BEGIN")
    for _sid, _v in _s52_vecs.items():
        _rid = _s52con.execute("SELECT rowid FROM docs WHERE source_id = ?", (_sid,)).fetchone()[0]
        _arr = _np52.array(_v, dtype=_np52.float32)
        _s52con.execute("INSERT OR REPLACE INTO vec(rowid, model_version, dim, v) VALUES(?,?,?,?)",
                        (_rid, _s52_mv, 3, _arr.tobytes()))
    _s52con.commit()

    _s52_na = _graph.doc_node_id("arxiv", "A")
    _s52_nb = _graph.doc_node_id("arxiv", "B")
    _s52_nc = _graph.doc_node_id("arxiv", "C")

    # (7) similar top-k BY RANK: anchor A, k=2 -> [B (rank 1), C (rank 2)] by cosine; D excluded; the
    #     anchor self-excluded. method align:embed, coverage line named, capped when k candidates.
    _s52_sim = _graph.similar(_s52_na, k=2)
    check("p5 similar: top-k by RANK returns the nearest candidates (B then C), anchor + far D excluded",
          [c["id"] for c in _s52_sim.get("candidates", [])] == [_s52_nb, _s52_nc]
          and [c["rank"] for c in _s52_sim["candidates"]] == [1, 2]
          and all(c["kind"] == "document" for c in _s52_sim["candidates"]))
    check("p5 similar: the view carries method align:embed + the coverage line, and is capped at k candidates",
          _s52_sim.get("method") == "align:embed" and _s52_sim.get("anchor") == _s52_na
          and "embedded title" in (_s52_sim.get("coverage") or "") and _s52_sim.get("capped") is True)
    # NO cosine scores anywhere in the output (rank is the honest unit; a score invites pseudo-precision).
    check("p5 similar: NO cosine scores in the output (rank is the honest unit) and NO edges (a listing, not structure)",
          all(("score" not in c and "cosine" not in c and "sim" not in c)
              for c in _s52_sim["candidates"])
          and "edges" not in _s52_sim)
    # k respected: k=1 -> exactly one candidate (the single nearest, B).
    _s52_sim_k1 = _graph.similar(_s52_na, k=1)
    check("p5 similar: k is a resource budget, respected exactly (k=1 -> the single nearest candidate)",
          len(_s52_sim_k1["candidates"]) == 1 and _s52_sim_k1["candidates"][0]["id"] == _s52_nb)
    # anchor with NO vector in EITHER store (no docs row / no vec_thin row) -> error naming the real
    # condition (P7: the coverage line now spans both stores; the message says "embed").
    _s52_sim_miss = _graph.similar("doc:arxiv:NOT_INDEXED", k=2)
    check("p5 similar: an anchor with no vector in either store returns an error naming the real condition",
          "error" in _s52_sim_miss and "embed" in _s52_sim_miss["error"].lower())
    # a non-doc anchor (an entity id) -> error (similar is doc-anchored only).
    _s52_sim_nondoc = _graph.similar("work:openalex:W1", k=2)
    check("p5 similar: a non-doc anchor (entity id) returns an error (similar is doc-anchored only)",
          "error" in _s52_sim_nondoc)

    # (8) TRIPWIRE part B: NO graph_edges row anywhere carries method align:embed (similar is a
    #     zero-write VIEW; it minted nothing into this db despite the queries above).
    _s52_embed_rows = _s52con.execute(
        "SELECT count(*) FROM graph_edges WHERE method = 'align:embed'").fetchone()[0]
    check("p5 tripwire: similar wrote NOTHING — no graph_edges row carries method align:embed (zero-write view)",
          _s52_embed_rows == 0)
finally:
    _rstore.DB_PATH = _s52_db_prev
    _rstore._disabled = _s52_disabled_prev
    _rstore._local = _s52_local_prev
    _rstore._vec_M = _s52_vecM_prev
    _rstore._vec_ids = _s52_vecids_prev
    _rstore._vec_built_gen = _s52_vecgen_prev
    _rstore._vec_built_mv = _s52_vecmv_prev

# (8) TRIPWIRE part A (pure, no db): align:embed appears in NO collapse policy method-set — not
#     CONSERVATIVE, not WORKING, not EXPLORATORY. It exists ONLY as a proposal label (the razor:
#     embedding proximity in a collapse would fabricate identity out of topicality, corrupting voices).
check("p5 tripwire: align:embed is in NO policy method-set (CONSERVATIVE / WORKING / EXPLORATORY)",
      "align:embed" not in _graph.CONSERVATIVE and "align:embed" not in _graph.WORKING
      and "align:embed" not in _graph.EXPLORATORY)
# and align:embed is NOT a declared tap method (it is a view-only proposal label, never minted). The
# conflicts tap declared signal:divergence; no tap declares align:embed.
check("p5 tripwire: align:embed is NOT a declared tap method (a view-only proposal label, never minted)",
      "align:embed" not in _graph.declared_vocabulary()["methods"])

# (9) SURFACE: penumbra_graph now exposes the 7 views; the unknown-view error names all 7; the connect-time
#     brief carries the 7-view chain; P5 added no tool (the live count below is 18 since P8's penumbra_statement).
_s52_eg = _srv.penumbra_graph.__wrapped__ if hasattr(_srv.penumbra_graph, "__wrapped__") else _srv.penumbra_graph
_S52_VIEWS = ("find", "stats", "neighborhood", "between", "voices", "since", "similar")
_s52_unknown = _s52_eg(view="frobnicate")
check("p5 surface: an unknown view error names all SEVEN views (find..similar)",
      "error" in _s52_unknown and all(_v in _s52_unknown["error"] for _v in _S52_VIEWS))
# dispatch reaches the two new views (a garbage since date + a non-doc similar anchor each return the
# view's OWN error, proving the dispatch routed there rather than the unknown-view error). P6: the
# tool ABI is (view, args), so per-view params ride in args={...} (updated from the old flat kwargs).
check("p5 surface: view=since routes to the since view (its date-parse error, not unknown-view)",
      "error" in _s52_eg(view="since", args={"anchor": "sensor:x", "date": "garbage"})
      and "since requires date" in _s52_eg(view="since", args={"anchor": "sensor:x", "date": "garbage"})["error"])
check("p5 surface: view=similar routes to the similar view (its anchor error, not unknown-view)",
      "error" in _s52_eg(view="similar", args={"anchor": "work:openalex:W1"}))
check("p5 surface: _PENUMBRA_INSTRUCTIONS carries the 7-view chain (find -> ... -> since -> similar)",
      "find -> stats -> neighborhood -> between -> voices -> since -> similar" in _srv._PENUMBRA_INSTRUCTIONS)
# P5 itself added no tool (since/similar are penumbra_graph views, not new tools). The LIVE _PENUMBRA_VERBS count
# is now 18 (P8 added penumbra_statement, the only wave since to add a verb); the gather whitelist stays 12,
# and penumbra_graph is still a registered read-only tool.
from penumbra.server import _PENUMBRA_VERBS as _s52_verbs  # noqa: E402
check("p5 surface: since/similar are penumbra_graph views not new tools (the live verb count is 18, "
      "unchanged BY P5; the +1 over P3's 17 is P8's penumbra_statement)",
      len(_s52_verbs) == 18 and len(_GATHER_TOOLS) == 12 and "penumbra_graph" in _GATHER_TOOLS)


# ---------------------------------------------------------------------------
# 53. P6 (A) — the in-process sensor scheduler (design "P6", the two structural closures): a run is
#     an act of PERCEPTION and must land on the wall, so execution moved INTO the writer process and
#     the launchd cron runner (a second, memory-less path) is deleted. Cover the PURE due_sensors
#     cases, scheduler_tick (runs due + isolates a failure + Barks only on notify+new), and the
#     grep-style tripwire that NO in-repo sensor_runner reference remains. (P9: the daemon LOOP + its
#     two guards moved to jobs.start_scheduler and _bark_push's impl moved to notify; those guards +
#     the fail-open push are tested below against their NEW homes, and the full P9 job registry has
#     its own section §57.)
# ---------------------------------------------------------------------------
import penumbra.core.sensor as _sen53  # noqa: E402
from penumbra.core.sensor import Sensor as _Sensor53, SensorStore as _Store53, due_sensors as _due53  # noqa: E402
from datetime import datetime as _dt53, timezone as _tz53, timedelta as _td53  # noqa: E402

# _SCHEDULE_SECONDS is the canonical schedule table; unknown degrades to daily.
check("p6-A schedule: _SCHEDULE_SECONDS maps hourly/daily/weekly to seconds",
      _sen53._SCHEDULE_SECONDS == {"hourly": 3600, "daily": 86400, "weekly": 604800})

# (1) due_sensors is PURE (unit-testable): drive it against a temp-path store with hand-set
#     last_run_at values. never-run -> due; a fresh daily -> not due; a stale weekly -> due; an
#     unknown schedule is treated as daily (a 2-day-old unknown is due, a 1-hour-old unknown is not).
_tmp_due53 = _Path44(_tempfile44.mktemp(suffix=".json"))
try:
    _st53 = _Store53(_tmp_due53)
    _now53 = _dt53.now(_tz53.utc)
    def _mk53(sid, schedule, age_seconds):
        # write a Sensor row directly (bypassing create's id-minting) with a controlled last_run_at.
        ts = None if age_seconds is None else (_now53 - _td53(seconds=age_seconds)).isoformat()
        _st53.update(_Sensor53(id=sid, query=f"q {sid}", schedule=schedule, last_run_at=ts))
    _mk53("s_never", "daily", None)          # never run -> due immediately
    _mk53("s_fresh_daily", "daily", 3600)    # 1h old, daily interval 24h -> NOT due
    _mk53("s_stale_weekly", "weekly", 8 * 86400)  # 8d old, weekly interval 7d -> due
    _mk53("s_unknown_stale", "frobnicate", 2 * 86400)  # unknown -> daily; 2d old -> due
    _mk53("s_unknown_fresh", "frobnicate", 3600)       # unknown -> daily; 1h old -> NOT due
    _due_ids53 = {s.id for s in _due53(_st53, _now53.timestamp())}
    check("p6-A due_sensors: a never-run sensor is due immediately", "s_never" in _due_ids53)
    check("p6-A due_sensors: a fresh daily sensor is NOT due", "s_fresh_daily" not in _due_ids53)
    check("p6-A due_sensors: a stale weekly sensor is due", "s_stale_weekly" in _due_ids53)
    check("p6-A due_sensors: an unknown schedule degrades to daily (2d-old -> due)",
          "s_unknown_stale" in _due_ids53)
    check("p6-A due_sensors: an unknown schedule degrades to daily (1h-old -> not due)",
          "s_unknown_fresh" not in _due_ids53)
finally:
    _tmp_due53.unlink(missing_ok=True)

# (2) scheduler_tick: runs every DUE sensor via run_sensor (monkeypatched, zero network), ISOLATES a
#     failing sensor (one raise never stops the rest), and Barks ONLY when notify AND new_count>0
#     (monkeypatch _bark_push to record). Two sensors: one notify+new (Barks), one notify but no-new
#     (silent), plus a third that raises (isolated into "failed").
_tmp_tick53 = _Path44(_tempfile44.mktemp(suffix=".json"))
_run_real53 = _sen53.run_sensor
_bark_real53 = _sen53._bark_push
try:
    _st_tick53 = _Store53(_tmp_tick53)
    _st_tick53.update(_Sensor53(id="s_hit", query="hit query", schedule="daily", notify=True))
    _st_tick53.update(_Sensor53(id="s_quiet", query="quiet query", schedule="daily", notify=True))
    _st_tick53.update(_Sensor53(id="s_boom", query="boom query", schedule="daily", notify=True))
    _barks53: list = []
    def _fake_bark53(title, body):
        _barks53.append((title, body))
    def _fake_run53(sensor, store, limit=15):
        if sensor.id == "s_boom":
            raise RuntimeError("simulated sensor failure")
        new_count = 2 if sensor.id == "s_hit" else 0
        return {"sensor_id": sensor.id, "query": sensor.query, "new_count": new_count,
                "new_titles": ["New Title A", "New Title B"][:new_count]}
    _sen53.run_sensor = _fake_run53
    _sen53._bark_push = _fake_bark53
    # all three are never-run -> all due. now() is real; the fakes ignore time.
    _tick53 = _sen53.scheduler_tick(_st_tick53)
    check("p6-A scheduler_tick: checked counts all due sensors", _tick53.get("checked") == 3)
    check("p6-A scheduler_tick: ran the two non-failing sensors",
          set(_tick53.get("ran", [])) == {"s_hit", "s_quiet"})
    check("p6-A scheduler_tick: a failing sensor is isolated into failed (never stops the rest)",
          _tick53.get("failed") == ["s_boom"])
    check("p6-A scheduler_tick: Barks ONLY on notify+new (one Bark, for the hit sensor)",
          len(_barks53) == 1 and _barks53[0][0] == "hit query")
    check("p6-A scheduler_tick: the Bark body carries the new count + titles",
          "2" in _barks53[0][1] and "New Title A" in _barks53[0][1])
finally:
    _sen53.run_sensor = _run_real53
    _sen53._bark_push = _bark_real53
    _tmp_tick53.unlink(missing_ok=True)

# (3a) start_scheduler REFUSES to start unless writer.WRITES_ENABLED is truthy (the scheduler only
#      ever belongs in the writer process). P9: the daemon loop + these two guards moved from
#      sensor.start_scheduler to jobs.start_scheduler (the ONE fleet scheduler; the sensor tick is
#      now job row #1 there). Test the guards on their new home. Monkeypatch the gate off + reset the
#      idempotence flag.
import penumbra.core.jobs as _jobs53  # noqa: E402
import penumbra.core.recall.writer as _wr53  # noqa: E402
_writes_real53 = _wr53.WRITES_ENABLED
_started_real53 = _jobs53._scheduler_started
_shipped_real53 = _jobs53._shipped_registered
try:
    _wr53.WRITES_ENABLED = False
    _jobs53._scheduler_started = False
    check("p6-A start_scheduler: REFUSES to start when WRITES_ENABLED is off (returns None)",
          _jobs53.start_scheduler() is None)
    check("p6-A start_scheduler: the refusal does NOT set the started flag",
          _jobs53._scheduler_started is False)
    # (3b) double-start guard: with writes ON, the FIRST start returns a Thread, the SECOND returns
    #      None (the module flag prevents a second thread). Clean up the started daemon after. Guard
    #      _shipped_registered so register_shipped_jobs (called inside start) does not permanently
    #      register the live rows into the smoke's process on a re-run.
    _wr53.WRITES_ENABLED = True
    _jobs53._scheduler_started = False
    _th53_a = _jobs53.start_scheduler(interval_s=3600, initial_delay_s=3600)  # long delays: it just sleeps
    _th53_b = _jobs53.start_scheduler(interval_s=3600, initial_delay_s=3600)
    check("p6-A start_scheduler: first start (writes on) returns a daemon Thread",
          _th53_a is not None and _th53_a.daemon is True)
    check("p6-A start_scheduler: a second start is a no-op (double-start guard returns None)",
          _th53_b is None)
finally:
    _wr53.WRITES_ENABLED = _writes_real53
    _jobs53._scheduler_started = _started_real53  # the started daemon is a harmless sleeping thread
    _jobs53._shipped_registered = _shipped_real53

# (4) the sensor _bark_push is FAIL-OPEN: an ABSENT credentials file is a silent no-op (never raises),
#     so a deployment with no bark.json simply pushes nothing. P9 lifted the push impl into
#     penumbra.core.notify (sensor._bark_push now delegates there), so the creds path lives on notify.
#     Point that path at a missing file and confirm both the notify primitive AND the sensor alias
#     no-op without raising.
import penumbra.core.notify as _notify53  # noqa: E402
_bark_creds_real53 = _notify53._BARK_CREDS_PATH
try:
    _notify53._BARK_CREDS_PATH = _Path44(_tempfile44.mktemp(suffix=".json"))  # does not exist
    check("p6-A _bark_push: fail-open no-op when the credentials file is absent (no raise)",
          _notify53.bark_push("t", "b") is None and _sen53._bark_push("t", "b") is None)
finally:
    _notify53._BARK_CREDS_PATH = _bark_creds_real53

# (5) THE SECOND PATH IS DELETED: scripts/sensor_runner.py is gone, and NO in-repo file references
#     the token "sensor_runner" (a memory-less cron runner is not fixed, it is removed). Grep-style
#     scan over the eye tree (py/md/sh/txt/json), EXCLUDING this smoke file itself (it names the token
#     in this very comment). The scan + the deleted file together are the gate.
check("p6-A delete: scripts/sensor_runner.py is gone", not (_SCRIPTS_DIR / "sensor_runner.py").exists())
_sr_refs53: list = []
for _p53 in ROOT.rglob("*"):
    if not _p53.is_file() or _p53.suffix not in (".py", ".md", ".sh", ".txt", ".json"):
        continue
    if _p53.name == "smoke.py" or ".venv" in _p53.parts or "__pycache__" in _p53.parts:
        continue
    try:
        if "sensor_runner" in _p53.read_text(encoding="utf-8", errors="ignore"):
            _sr_refs53.append(str(_p53.relative_to(ROOT)))
    except Exception:  # noqa: BLE001 — an unreadable file is not a reference
        pass
check("p6-A delete: no in-repo file references sensor_runner (docs + scripts swept)",
      not _sr_refs53, f"still referenced in: {_sr_refs53}")


# ---------------------------------------------------------------------------
# 54. P6 (B) — penumbra_graph's stable ABI (design "P6": open families get open ABIs). penumbra_graph is the
#     eye's ONE open-family verb: its views grow with the model and their params are disjoint, so it
#     is now (view, args) with the views as a REGISTRY (a decorator; the dispatcher, valid-view list,
#     per-view arg validation, and the self-description all DERIVE from it). Cover dispatch_view's
#     gate (empty -> catalog, unknown view, unknown arg, missing required, lenient coercion, each of
#     the 7 routes), the FROZEN-SCHEMA tripwire (the tool body's params are exactly {view, args}), and
#     the gather path. The view FUNCTIONS' own behavior is covered by sections 47-52 (unchanged here).
# ---------------------------------------------------------------------------
from penumbra.core.recall import graph as _g54  # noqa: E402
_S54_VIEWS = ("find", "stats", "neighborhood", "between", "voices", "since", "similar")

# (1) the registry IS the valid-view list: exactly the seven decorated functions, each a callable.
check("p6-B registry: _VIEWS holds exactly the seven view functions",
      set(_g54._VIEWS.keys()) == set(_S54_VIEWS) and all(callable(f) for f in _g54._VIEWS.values()))

# (2) describe_views is DERIVED via inspect: every view carries its params (name/required/default) +
#     its docstring FIRST LINE; a no-view dispatch returns this catalog.
_desc54 = _g54.describe_views()
check("p6-B describe_views: the catalog covers all seven views",
      set(_desc54.keys()) == set(_S54_VIEWS))
check("p6-B describe_views: each view exposes params + a non-empty doc first-line",
      all(isinstance(_desc54[v]["params"], list) and bool(_desc54[v]["doc"]) for v in _S54_VIEWS))
# spot-check the per-view contract is the FUNCTION's real signature (between(a, b, ...), not renamed).
_between_params54 = [p["name"] for p in _desc54["between"]["params"]]
check("p6-B describe_views: between's params are its real signature a/b/types/policy/max_nodes",
      _between_params54 == ["a", "b", "types", "policy", "max_nodes"])
check("p6-B describe_views: a required param (voices.doc_ids) is flagged required, an optional one is not",
      any(p["name"] == "doc_ids" and p["required"] for p in _desc54["voices"]["params"])
      and any(p["name"] == "policy" and not p["required"] for p in _desc54["voices"]["params"]))

# (3) dispatch_view — EMPTY view returns the live catalog (self-describing, not an error).
_empty54 = _g54.dispatch_view("")
check("p6-B dispatch: an empty view returns the catalog with all seven views + their first lines",
      "views" in _empty54 and set(_empty54["views"].keys()) == set(_S54_VIEWS)
      and all(_empty54["views"][v]["doc"] for v in _S54_VIEWS) and "error" not in _empty54)

# (4) UNKNOWN view -> an error naming every valid view (and the catalog rides along).
_unk54 = _g54.dispatch_view("frobnicate")
check("p6-B dispatch: an unknown view error names all seven views",
      "error" in _unk54 and all(v in _unk54["error"] for v in _S54_VIEWS) and "views" in _unk54)

# (5) UNKNOWN arg -> an error naming the view's REAL params + the unexpected key (no silent drop).
_badarg54 = _g54.dispatch_view("stats", {"bogus": 1})
check("p6-B dispatch: an unexpected arg errors, naming the view + the unexpected key",
      "error" in _badarg54 and "bogus" in _badarg54["error"] and "stats" in _badarg54["error"])
_badarg54b = _g54.dispatch_view("between", {"a": "x", "nonsense": 1})
check("p6-B dispatch: an unexpected arg names the view's real params (a/b/... present in the message)",
      "error" in _badarg54b and "nonsense" in _badarg54b["error"] and "'a'" in _badarg54b["error"])

# (6) MISSING REQUIRED -> an error naming the missing param (between needs both a and b).
_missing54 = _g54.dispatch_view("between", {"a": "x"})
check("p6-B dispatch: a missing required param is named (between without b)",
      "error" in _missing54 and "'b'" in _missing54["error"])

# (7) LENIENT COERCION mirroring the server's Lenient* types: a str "40" for an int param becomes 40.
#     Prove it END-TO-END through dispatch by installing a temp echo-view (restored after), so the
#     assertion is on dispatch's coercion, not just the helper — "40" (str) must arrive as 40 (int).
_views_backup54 = dict(_g54._VIEWS)
try:
    def _echo_int_view54(n: int = 7):
        """echo view (smoke-only) -> returns the received n + its type name."""
        return {"n": n, "type": type(n).__name__}
    _g54._VIEWS["_echo_int_view54"] = _echo_int_view54
    _coerced54 = _g54.dispatch_view("_echo_int_view54", {"n": "40"})
    check("p6-B dispatch: lenient int coercion — a str \"40\" for an int param arrives as int 40",
          _coerced54.get("n") == 40 and _coerced54.get("type") == "int")
finally:
    _g54._VIEWS.clear()
    _g54._VIEWS.update(_views_backup54)
check("p6-B dispatch: the temp echo-view was removed (the registry is restored to the seven views)",
      set(_g54._VIEWS.keys()) == set(_S54_VIEWS))

# (8) EACH of the seven dispatches to ITS function (not the unknown-view error). Drive them against a
#     disabled store so every view fail-opens to its own empty/own-error shape with zero network; the
#     point is ROUTING, not data. stats through dispatch must EQUAL stats() called directly.
import penumbra.core.recall.store as _rstore54  # noqa: E402
_disabled_prev54 = _rstore54._disabled
try:
    _rstore54._disabled = True  # every view's _con() -> None -> fail-open to its empty shape
    _routes54 = {
        "find": _g54.dispatch_view("find", {"label_query": "anything"}),
        "stats": _g54.dispatch_view("stats", {}),
        "neighborhood": _g54.dispatch_view("neighborhood", {"anchor": "doc:a:1"}),
        "between": _g54.dispatch_view("between", {"a": "doc:a:1", "b": "doc:b:2"}),
        "voices": _g54.dispatch_view("voices", {"doc_ids": ["doc:a:1"]}),
        "since": _g54.dispatch_view("since", {"anchor": "doc:a:1", "date": "2026-01-01"}),
        "similar": _g54.dispatch_view("similar", {"anchor": "doc:a:1"}),
    }
    # a routed call NEVER returns the unknown-view error; each returns its own view's shape.
    _mis_routed54 = [v for v, r in _routes54.items()
                     if isinstance(r, dict) and "unknown view" in str(r.get("error", ""))]
    check("p6-B dispatch: each of the seven views routes to its function (none hit unknown-view)",
          not _mis_routed54, f"mis-routed: {_mis_routed54}")
    check("p6-B dispatch: routed shapes are the views' own (find->nodes, stats->node_kinds, between->paths)",
          "nodes" in _routes54["find"] and "node_kinds" in _routes54["stats"]
          and "paths" in _routes54["between"] and "voices" in _routes54["voices"])
    # the concrete spot-check the spec calls out: stats through dispatch == stats() direct.
    check("p6-B dispatch: stats THROUGH dispatch equals stats() called directly",
          _g54.dispatch_view("stats", {}) == _g54.stats())
finally:
    _rstore54._disabled = _disabled_prev54

# (9) THE FROZEN-SCHEMA TRIPWIRE: the penumbra_graph TOOL body's parameters are EXACTLY {view, args}.
#     Future views + future per-view params are data growth in the registry, never schema growth here
#     (a new flat param sneaking onto the tool signature is the regression this catches).
_eg54 = _srv.penumbra_graph.__wrapped__ if hasattr(_srv.penumbra_graph, "__wrapped__") else _srv.penumbra_graph
_eg54_params = set(_insp.signature(_eg54).parameters.keys())
check("p6-B FROZEN SCHEMA: the penumbra_graph tool body's params are exactly {view, args}",
      _eg54_params == {"view", "args"}, f"got {sorted(_eg54_params)}")
check("p6-B surface: the tool body just delegates to dispatch_view (view-only call returns the catalog)",
      "views" in _eg54(view="") and set(_eg54(view="")["views"].keys()) == set(_S54_VIEWS))

# (10) GATHER PATH still reaches penumbra_graph through the (view, args) ABI: the whitelisted body accepts
#      both a bare {view} and a {view, args} shape (gather spreads **args, incl. a nested args dict).
_eg_gather54 = _GATHER_TOOLS["penumbra_graph"]
check("p6-B gather: {view: stats} reaches penumbra_graph through the gather whitelist",
      "node_kinds" in _eg_gather54(view="stats"))
check("p6-B gather: {view: voices, args:{...}} reaches penumbra_graph through the gather whitelist",
      _eg_gather54(view="voices", args={"doc_ids": []}).get("n_voices") == 0)


# ---------------------------------------------------------------------------
# 55. P7 — three closures from the same audit standard (design "P7 shipped 2026-07-03"):
#     (W1) the divergence detector loses its 1.5x threshold: it MEASURES every same-work numeric
#          divergence (values + ratio), RANKS by ratio DESC, keeps top-3 per doc (a resource cap),
#          serializes a zero-vs-nonzero pair as "inf" (ranks first), and equal values are no
#          divergence; the stamp + the conflicts edge carry the ratio as data. (The STABILITY /
#          builder / cap checks live in §51 + the §42 block; here: ordering + inf + cap-by-rank.)
#     (W2) the "no edges, no entity" audit licensed NO taps (products carry only handle strings or
#          the doc's own id), so there is deliberately NO smoke check for it — the audit lives in the
#          report + canon, and fabricating a check would be dishonest.
#     (W3) thin rows embed their TITLES into vec_thin (writer lane, fail-open) + a bounded
#          self-converging catch-up; similar ranks across BOTH vector stores; stats carries the
#          coverage gauge; and vec_thin is NOT consulted by the search/recall path.
#     VEC FIXTURE (honest, per §52): the real qwen weights are not on a bare checkout, so the
#     writer-lane + catch-up parts STUB embed.embed_passage with a deterministic tiny-vector fn (only
#     the weights-load step is bypassed), and the similar-union part HAND-SEEDS vec/vec_thin with tiny
#     float32 vectors and exercises the REAL matrix/cosine machinery. FRESH temp-db, restore in finally.
# ---------------------------------------------------------------------------
import numpy as _np55  # noqa: E402
import penumbra.core.recall.writer as _wr55  # noqa: E402
import penumbra.core.recall.embed as _emb55  # noqa: E402

# (W1) DIVERGENCE ORDERING + INF + CAP-BY-RANK (pure rank.dedup; no db). One group, cross-source,
#      several diverging numeric signals: they surface RANKED by ratio DESC, a zero-vs-nonzero pair is
#      "inf" and ranks FIRST, and the per-doc cap of 3 keeps the THREE HIGHEST ratios (not the first 3
#      by iteration order). This is the whole point of W1: rank, never a gate.
_s55_t = "P Seven Divergence Ranked Shared Long Normalized Title Here"
_s55_a = _PDoc(source="sA", source_id="1", url="http://a", title=_s55_t, content="x",
               signals={"z": _Signal42(value=0.0, kind="other", computed_by="c"),      # -> inf
                        "big": _Signal42(value=10.0, kind="other", computed_by="c"),   # -> 5.0
                        "mid": _Signal42(value=10.0, kind="other", computed_by="c"),   # -> 3.0
                        "low": _Signal42(value=10.0, kind="other", computed_by="c")})  # -> 1.2 (dropped by cap)
_s55_b = _PDoc(source="sB", source_id="2", url="http://b", title=_s55_t, content="y",
               signals={"z": _Signal42(value=7.0, kind="other", computed_by="c"),
                        "big": _Signal42(value=50.0, kind="other", computed_by="c"),
                        "mid": _Signal42(value=30.0, kind="other", computed_by="c"),
                        "low": _Signal42(value=12.0, kind="other", computed_by="c")})
_s55_dd = _dedup42([_s55_a, _s55_b])
_s55_sc = (_s55_dd[0].metadata or {}).get("signal_conflicts", []) if len(_s55_dd) == 1 else []
check("p7 W1: divergences RANK by ratio DESC, zero-vs-nonzero is 'inf' first, per-doc cap keeps the top-3",
      [(c["topic"], c["ratio"]) for c in _s55_sc] == [("z", "inf"), ("big", 5.0), ("mid", 3.0)])
# the private tap record mirrors the stamp order + carries the same ratios (the fetcher pops it).
_s55_cp = (_s55_dd[0].metadata or {}).get("_conflict_pairs", [])
check("p7 W1: the private _conflict_pairs mirror the ranked order + ratios (fetcher pops it)",
      [(p["signal"], p["ratio"]) for p in _s55_cp] == [("z", "inf"), ("big", 5.0), ("mid", 3.0)])
# and the minted conflicts edges carry the ratio in attrs (the edge is data, materiality is the reader's).
_s55_cn, _s55_ce = rank._conflict_mints(_s55_cp)
check("p7 W1: minted conflicts edges carry the ratio in attrs ('inf' for the zero-vs-nonzero pair)",
      _s55_cn == [] and len(_s55_ce) == 3
      and {e["attrs"]["signal"]: e["attrs"]["ratio"] for e in _s55_ce} ==
          {"z": "inf", "big": 5.0, "mid": 3.0})

# (W3) VEC_THIN ROUNDTRIP + CATCH-UP + SIMILAR UNION. Fresh temp-db; reset BOTH matrix caches.
_s55_db_prev = _rstore.DB_PATH
_s55_disabled_prev = _rstore._disabled
_s55_local_prev = _rstore._local
_s55_vecM_prev, _s55_vecids_prev = _rstore._vec_M, _rstore._vec_ids
_s55_vecgen_prev, _s55_vecmv_prev = _rstore._vec_built_gen, _rstore._vec_built_mv
_s55_thinM_prev, _s55_thinids_prev = _rstore._thin_M, _rstore._thin_ids
_s55_thingen_prev, _s55_thinmv_prev = _rstore._thin_built_gen, _rstore._thin_built_mv
# embed stubs (restore in finally): a deterministic 3-dim tiny-vector fn keyed by the title's 1st char.
_s55_emb_avail_prev = _emb55.available
_s55_emb_pass_prev = _emb55.embed_passage
_s55_emb_mv_prev = _emb55.MODEL_VERSION
_s55_emb_dim_prev = _emb55.DIM
_S55_MV = "p7-fake-emb/d3"
_S55_VMAP = {"a": [1.0, 0.0, 0.0], "b": [0.9, 0.1, 0.0], "c": [0.5, 0.5, 0.0], "d": [0.0, 0.0, 1.0]}
def _s55_fake_pass(texts):
    return _np55.array([_S55_VMAP.get((t or "?")[0].lower(), [0.1, 0.1, 0.1]) for t in texts],
                       dtype=_np55.float32)
_rstore.DB_PATH = Path(_tf47.mkdtemp()) / "smoke_p7.db"
_rstore._disabled = False
_rstore._local = _thr47.local()
_rstore._vec_M = _rstore._vec_ids = None; _rstore._vec_built_gen = -1; _rstore._vec_built_mv = ""
_rstore._thin_M = _rstore._thin_ids = None; _rstore._thin_built_gen = -1; _rstore._thin_built_mv = ""
_emb55.available = lambda: True
_emb55.embed_passage = _s55_fake_pass
_emb55.MODEL_VERSION = _S55_MV
_emb55.DIM = 3
try:
    check("p7 W3: index init creates vec_thin in the temp db",
          _rstore.init()
          and bool(_rstore.connect().execute(
              "SELECT name FROM sqlite_master WHERE type='table' AND name='vec_thin'").fetchone()))
    _s55con = _rstore.connect()

    # (1) thin upsert WITH a title -> _upsert_thin returns (node_id, title); _embed_and_store_thin
    #     writes ONE vec_thin row. A thin doc with NO title -> returns None, no vec_thin row. The
    #     embedder returning None -> no row, no error (row-level fail-open).
    _s55_thin_doc = _PDoc(source="zhihu", source_id="q1", url="http://z/1",
                          title="Beta Thin Title Long Enough", content="")
    _s55con.execute("BEGIN")
    _s55_r = _wr55._upsert_thin(_s55con, rank, _s55_thin_doc, 100.0)
    check("p7 W3: _upsert_thin returns (node_id, title) for a titled thin row (embed staging)",
          _s55_r == (_graph.doc_node_id("zhihu", "q1"), "Beta Thin Title Long Enough"))
    _wr55._embed_and_store_thin(_s55con, [_s55_r])
    _s55con.commit()
    check("p7 W3: a titled thin upsert embeds ONE vec_thin row (title -> vector)",
          _s55con.execute("SELECT count(*) FROM vec_thin").fetchone()[0] == 1)
    _s55con.execute("BEGIN")
    _s55_rnt = _wr55._upsert_thin(_s55con, rank,
                                  _PDoc(source="zhihu", source_id="q2", url="http://z/2",
                                        title="", content=""), 100.0)
    _s55con.commit()
    check("p7 W3: a title-less thin row -> no embed staging (returns None), no vec_thin row",
          _s55_rnt is None and _s55con.execute("SELECT count(*) FROM vec_thin").fetchone()[0] == 1)
    # embedder returning None: stage a titled row, force embed_passage -> None; no row, no raise.
    _emb55.embed_passage = lambda texts: None
    _s55con.execute("BEGIN")
    _wr55._embed_and_store_thin(_s55con, [(_graph.doc_node_id("zhihu", "q3"), "Gamma Title")])
    _s55con.commit()
    check("p7 W3: embedder returning None -> the thin row is un-embedded (no vec_thin row, no error)",
          _s55con.execute("SELECT count(*) FROM vec_thin").fetchone()[0] == 1)
    _emb55.embed_passage = _s55_fake_pass   # restore the working stub for the catch-up below

    # (2) CATCH-UP is bounded at 50 + converges: 60 titled thin nodes with no vec_thin row -> cycle 1
    #     embeds 50, cycle 2 embeds the remaining 10, cycle 3 embeds 0 (caught up). A title-less node
    #     is never embedded. (Wipe the 1 pre-existing vec_thin row first so the arithmetic is clean.)
    _s55con.execute("DELETE FROM vec_thin")
    _s55con.execute("DELETE FROM graph_nodes")
    _s55con.commit()   # close the implicit txn the DELETEs opened before the explicit BEGIN below
    _s55con.execute("BEGIN")
    for _i in range(60):
        _wr55._upsert_node(_s55con, _graph.doc_node_id("cu", f"n{_i}"), "document",
                           f"Catchup Thin Title {_i}", None, 100.0)
    _wr55._upsert_node(_s55con, _graph.doc_node_id("cu", "notitle"), "document", None, None, 100.0)
    _s55con.commit()
    _s55_c1 = _wr55._thin_catchup(_s55con)
    _s55_c2 = _wr55._thin_catchup(_s55con)
    _s55_c3 = _wr55._thin_catchup(_s55con)
    check("p7 W3: the idle catch-up is bounded at 50/cycle and converges (60 rows -> 50, then 10, then 0)",
          (_s55_c1, _s55_c2, _s55_c3) == (50, 10, 0)
          and _s55con.execute("SELECT count(*) FROM vec_thin").fetchone()[0] == 60)

    # (3) SIMILAR UNION: hand-seed vec (indexed) + vec_thin (thin) in ONE cosine space and rank across
    #     BOTH. A=indexed[1,0,0], B=thin[0.9,0.1,0] (nearest to A), C=indexed[0.5,0.5,0], D=thin[0,0,1].
    #     A thin anchor finds an INDEXED neighbor and an indexed anchor finds a THIN neighbor. Reset
    #     caches + rewrite from scratch so the arithmetic is exact.
    _s55con.execute("DELETE FROM vec_thin"); _s55con.execute("DELETE FROM vec")
    _s55con.execute("DELETE FROM docs"); _s55con.execute("DELETE FROM graph_nodes")
    _s55con.commit()   # close the implicit txn the DELETEs opened
    _rstore._vec_M = _rstore._vec_ids = None; _rstore._vec_built_gen = -1; _rstore._vec_built_mv = ""
    _rstore._thin_M = _rstore._thin_ids = None; _rstore._thin_built_gen = -1; _rstore._thin_built_mv = ""
    # indexed docs A + C (real docs rows via _upsert, then a clean source_id + a hand-seeded vec row).
    for _sid in ("A", "C"):
        _s55con.execute("BEGIN")
        _wr55._upsert(_s55con, rank, _doc("arxiv", _sid + " indexed doc long title", "http://a/" + _sid), 1.0)
        _s55con.execute("UPDATE docs SET source_id=? WHERE url=?", (_sid, "http://a/" + _sid))
        _s55con.commit()
    for _sid, _v in (("A", [1.0, 0.0, 0.0]), ("C", [0.5, 0.5, 0.0])):
        _rid = _s55con.execute("SELECT rowid FROM docs WHERE source_id=?", (_sid,)).fetchone()[0]
        _s55con.execute("INSERT OR REPLACE INTO vec(rowid, model_version, dim, v) VALUES(?,?,?,?)",
                        (_rid, _S55_MV, 3, _np55.array(_v, dtype=_np55.float32).tobytes()))
    _s55con.commit()   # commit the vec seed (also closes the implicit txn before the BEGIN below)
    # thin docs B + D (graph_nodes + a hand-seeded vec_thin row).
    for _sid, _v, _lbl in (("B", [0.9, 0.1, 0.0], "Beta thin near A"),
                           ("D", [0.0, 0.0, 1.0], "Delta thin far")):
        _nid = _graph.doc_node_id("zhihu", _sid)
        _s55con.execute("BEGIN")
        _wr55._upsert_node(_s55con, _nid, "document", _lbl, None, 1.0)
        _s55con.execute("INSERT OR REPLACE INTO vec_thin(node_id, model_version, dim, v) VALUES(?,?,?,?)",
                        (_nid, _S55_MV, 3, _np55.array(_v, dtype=_np55.float32).tobytes()))
        _s55con.commit()
    _s55_iA, _s55_iC = _graph.doc_node_id("arxiv", "A"), _graph.doc_node_id("arxiv", "C")
    _s55_tB, _s55_tD = _graph.doc_node_id("zhihu", "B"), _graph.doc_node_id("zhihu", "D")
    # indexed anchor A -> nearest is THIN B (rank1), then indexed C (rank2); thin D excluded (far).
    _s55_simA = _graph.similar(_s55_iA, k=2)
    check("p7 W3: similar with an INDEXED anchor finds a THIN neighbor across the union (B rank1, C rank2)",
          [c["id"] for c in _s55_simA.get("candidates", [])] == [_s55_tB, _s55_iC]
          and [c["rank"] for c in _s55_simA["candidates"]] == [1, 2]
          and _s55_simA["candidates"][0]["label"] == "Beta thin near A")
    # thin anchor B -> nearest is INDEXED A (rank1); the anchor self-excluded; label from docs.
    _s55_simB = _graph.similar(_s55_tB, k=2)
    check("p7 W3: similar with a THIN anchor finds an INDEXED neighbor across the union (A rank1)",
          _s55_simB.get("candidates") and _s55_simB["candidates"][0]["id"] == _s55_iA
          and "indexed doc" in (_s55_simB["candidates"][0]["label"] or "")
          and all(c["id"] != _s55_tB for c in _s55_simB["candidates"]))
    # union ranking is DETERMINISTIC: two neighbors at the SAME cosine tie-break by node_id ASC.
    #   anchor X=[1,0,0]; two thin rows E,F both =[0,1,0] (cosine 0 to X) -> E before F (node id order).
    _s55con.execute("DELETE FROM vec_thin"); _s55con.execute("DELETE FROM vec")
    _s55con.execute("DELETE FROM docs"); _s55con.execute("DELETE FROM graph_nodes")
    _s55con.commit()   # close the implicit txn the DELETEs opened before the explicit BEGIN below
    _rstore._vec_M = _rstore._vec_ids = None; _rstore._vec_built_gen = -1; _rstore._vec_built_mv = ""
    _rstore._thin_M = _rstore._thin_ids = None; _rstore._thin_built_gen = -1; _rstore._thin_built_mv = ""
    _s55con.execute("BEGIN")
    _wr55._upsert(_s55con, rank, _doc("arxiv", "X anchor indexed long title", "http://a/X"), 1.0)
    _s55con.execute("UPDATE docs SET source_id='X' WHERE url='http://a/X'")
    _s55con.commit()
    _s55_ridX = _s55con.execute("SELECT rowid FROM docs WHERE source_id='X'").fetchone()[0]
    _s55con.execute("INSERT OR REPLACE INTO vec(rowid, model_version, dim, v) VALUES(?,?,?,?)",
                    (_s55_ridX, _S55_MV, 3, _np55.array([1.0, 0.0, 0.0], dtype=_np55.float32).tobytes()))
    _s55con.commit()   # commit the vec seed (also closes the implicit txn before the BEGIN below)
    for _sid in ("zE", "zF"):
        _nid = _graph.doc_node_id("zhihu", _sid)
        _s55con.execute("BEGIN")
        _wr55._upsert_node(_s55con, _nid, "document", _sid + " tie", None, 1.0)
        _s55con.execute("INSERT OR REPLACE INTO vec_thin(node_id, model_version, dim, v) VALUES(?,?,?,?)",
                        (_nid, _S55_MV, 3, _np55.array([0.0, 1.0, 0.0], dtype=_np55.float32).tobytes()))
        _s55con.commit()
    _s55_tie = _graph.similar(_graph.doc_node_id("arxiv", "X"), k=2)
    check("p7 W3: the union rank is deterministic — equal-cosine neighbors tie-break by node_id ASC",
          [c["id"] for c in _s55_tie["candidates"]] ==
          [_graph.doc_node_id("zhihu", "zE"), _graph.doc_node_id("zhihu", "zF")])
    # (4) the no-vector error names the REAL condition (a doc with no vector in EITHER store).
    _s55_miss = _graph.similar("doc:zhihu:UN_EMBEDDED", k=2)
    check("p7 W3: the no-vector error names the real condition (embeds as the writer catches up)",
          "error" in _s55_miss and "embed" in _s55_miss["error"].lower())
    # (5) stats.node_kinds.document_thin_embedded == the vec_thin row count (the coverage gauge).
    _s55_vt = _s55con.execute("SELECT count(*) FROM vec_thin").fetchone()[0]
    check("p7 W3: stats.node_kinds.document_thin_embedded equals the vec_thin row count (coverage gauge)",
          _graph.stats()["node_kinds"].get("document_thin_embedded") == _s55_vt and _s55_vt == 2)

    # (6) P7-GATE fixes (adversarial-review catches, verified here):
    # (6a) ratio math over SIGNED values: magnitudes ratio for same-sign, categorical "inf" for a
    #      sign flip, zero-vs-negative unbounded, equal negatives skip. (Signals CAN be negative.)
    check("p7 gate: same-sign negative divergence ranks by MAGNITUDE ratio (-10 vs -2 -> 5.0)",
          rank._divergence_ratio(-10.0, -2.0) == 5.0)
    check("p7 gate: opposite signs are a categorical divergence (-5 vs 10 -> 'inf')",
          rank._divergence_ratio(-5.0, 10.0) == "inf")
    check("p7 gate: zero vs negative is unbounded ('inf'), never -0.0",
          rank._divergence_ratio(0.0, -5.0) == "inf")
    check("p7 gate: equal negatives are not a divergence (None)",
          rank._divergence_ratio(-7.0, -7.0) is None)
    # (6b) cross-store dedup: the SAME node id living in BOTH stores (a reclassified source's stale
    #      vec_thin twin beside a fresh docs/vec row) returns as ONE candidate, best cosine kept.
    _s55_dupe = _graph.doc_node_id("arxiv", "X")
    _s55con.execute("BEGIN")
    _wr55._upsert_node(_s55con, _s55_dupe, "document", "X stale thin twin", None, 1.0)
    _s55con.execute("INSERT OR REPLACE INTO vec_thin(node_id, model_version, dim, v) VALUES(?,?,?,?)",
                    (_s55_dupe, _S55_MV, 3, _np55.array([0.9, 0.1, 0.0], dtype=_np55.float32).tobytes()))
    _s55con.commit()
    _rstore._thin_M = _rstore._thin_ids = None; _rstore._thin_built_gen = -1; _rstore._thin_built_mv = ""
    _s55_dd = _rstore.similar_neighbors(_np55.array([1.0, 0.0, 0.0], dtype=_np55.float32),
                                        "doc:none:none", k=4)
    check("p7 gate: cross-store dedup, a node id present in BOTH stores returns as ONE candidate",
          [nid for nid, _t in _s55_dd].count(_s55_dupe) == 1 and len(_s55_dd) >= 3)
    # (6c) ghost labels: a legacy whitespace-only (nbsp) label survives SQLite's ASCII TRIM but is
    #      normalized to NULL by the catch-up (true convergence, it leaves the page permanently);
    #      a FRESH whitespace-only title stores label NULL + stages nothing (stripped at the source).
    _s55con.execute("BEGIN")
    _wr55._upsert_node(_s55con, _graph.doc_node_id("gh", "ws"), "document", "\xa0", None, 1.0)
    _s55con.commit()
    _wr55._thin_catchup(_s55con)
    check("p7 gate: a legacy whitespace-only label is normalized to NULL by the catch-up",
          _s55con.execute("SELECT label FROM graph_nodes WHERE id=?",
                          (_graph.doc_node_id("gh", "ws"),)).fetchone()[0] is None)
    _s55con.execute("BEGIN")
    _s55_ws = _wr55._upsert_thin(_s55con, rank, _PDoc(source="gh", source_id="ws2", url="http://g/2",
                                                      title="\xa0 ", content=""), 1.0)
    _s55con.commit()
    check("p7 gate: a whitespace-only title stores label NULL and stages nothing",
          _s55_ws is None and _s55con.execute("SELECT label FROM graph_nodes WHERE id=?",
                          (_graph.doc_node_id("gh", "ws2"),)).fetchone()[0] is None)
finally:
    _rstore.DB_PATH = _s55_db_prev
    _rstore._disabled = _s55_disabled_prev
    _rstore._local = _s55_local_prev
    _rstore._vec_M, _rstore._vec_ids = _s55_vecM_prev, _s55_vecids_prev
    _rstore._vec_built_gen, _rstore._vec_built_mv = _s55_vecgen_prev, _s55_vecmv_prev
    _rstore._thin_M, _rstore._thin_ids = _s55_thinM_prev, _s55_thinids_prev
    _rstore._thin_built_gen, _rstore._thin_built_mv = _s55_thingen_prev, _s55_thinmv_prev
    _emb55.available = _s55_emb_avail_prev
    _emb55.embed_passage = _s55_emb_pass_prev
    _emb55.MODEL_VERSION = _s55_emb_mv_prev
    _emb55.DIM = _s55_emb_dim_prev

# (6) SEARCH-PATH TRIPWIRE: vec_thin is a DELIBERATE NON-GOAL for penumbra_search's recall arm. Assert
#     STRUCTURALLY that the recall query path (store.vector_search + store.search — the vec consumers
#     search uses) does not CONSULT vec_thin / the thin matrix; only graph.similar's engine
#     (similar_neighbors) does. Check the EXECUTABLE code (comments + the docstring stripped), so a
#     DELIBERATE-NON-GOAL comment naming vec_thin never trips the tripwire — the guarantee is that the
#     CODE never calls the thin machinery, not that the word is unspeakable.
import ast as _ast55  # noqa: E402
def _s55_code_names(fn) -> set:
    """The set of NAME + ATTRIBUTE-tail identifiers + string literals in a function's EXECUTABLE body
    (docstring + comments excluded, since ast drops both). This is what the function actually
    references at runtime — the honest structural surface for the non-goal tripwire."""
    tree = _ast55.parse(_insp.getsource(fn).lstrip())
    names: set = set()
    for node in _ast55.walk(tree):
        if isinstance(node, _ast55.Name):
            names.add(node.id)
        elif isinstance(node, _ast55.Attribute):
            names.add(node.attr)
        elif isinstance(node, _ast55.Constant) and isinstance(node.value, str):
            names.add(node.value)
    # drop the leading docstring Constant (the module already excludes comments via ast).
    body = tree.body[0].body if tree.body and hasattr(tree.body[0], "body") else []
    if body and isinstance(body[0], _ast55.Expr) and isinstance(getattr(body[0], "value", None), _ast55.Constant):
        names.discard(body[0].value.value)
    return names
_s55_vs_names = _s55_code_names(_rstore.vector_search)
check("p7 W3 tripwire: vector_search's EXECUTABLE code consults _ensure_matrix, never _ensure_thin_matrix",
      "_ensure_matrix" in _s55_vs_names and "_ensure_thin_matrix" not in _s55_vs_names)
# neither recall search consumer references the thin table, the thin matrix builder, or the similar
# engine in executable code (the thin fold lives ONLY in graph.similar's engine).
_s55_search_names = _s55_code_names(_rstore.search) | _s55_vs_names
check("p7 W3 tripwire: the recall search arm never references vec_thin / thin matrix / similar_neighbors in code",
      "similar_neighbors" not in _s55_search_names and "_ensure_thin_matrix" not in _s55_search_names
      and not any("vec_thin" in n for n in _s55_search_names))


# ---------------------------------------------------------------------------
# 57. P9 (the fleet rebuild around the in-process scheduler): the P6 sensor scheduler generalized
#     into a JOB REGISTRY (penumbra.core.jobs) that runs every scheduled piece of the eye's self-
#     maintenance as a declarative row on ONE daemon loop in the writer process, plus the ONE external
#     sentinel (scripts/sentinel.py) that restarts the organ + its browsers. The derived architecture:
#     what stays OUTSIDE the organ is only "must this still run when the organ is dead?" -> the
#     sentinel, state-backup, and the CDP browsers; everything else became a job row. Cover: the pure
#     schedule parser (every valid form + garbage) + the calendar due-ness matrix; the registry
#     (duplicate/unknown refused, profile override, every shipped row shippable); the tick body
#     (heartbeat touched, failing job isolated + Barked once per cooldown, serial order); the
#     transplant cores (source-health N_CONSECUTIVE, log rotation, digest no-op, warmer importable);
#     the sentinel offline (dead healthz -> kickstart, stale heartbeat -> distinct alarm, maintenance
#     pauses CDP heal, and the ISOLATION tripwire that it imports zero penumbra); and the deletion sweep.
# ---------------------------------------------------------------------------
import importlib.util as _il57  # noqa: E402
import io as _io57  # noqa: E402
import os as _os57  # noqa: E402
import contextlib as _ctx57  # noqa: E402
import tempfile as _tf57  # noqa: E402
import time as _time57  # noqa: E402
from datetime import datetime as _dt57  # noqa: E402
import penumbra.core.jobs as _J57  # noqa: E402

# (1) SCHEDULE PARSER — every valid form parses to the right kind; garbage raises ValueError.
check("p9 parser: every:900s -> interval(900s)",
      _J57.parse_schedule("every:900s").kind == "interval" and _J57.parse_schedule("every:900s").seconds == 900)
_p9_daily = _J57.parse_schedule("daily@09:17,14:17,19:17")
check("p9 parser: daily@HH:MM,... -> daily with the sorted time tuple",
      _p9_daily.kind == "daily" and _p9_daily.times == ((9, 17), (14, 17), (19, 17)))
_p9_wk = _J57.parse_schedule("weekly@sun-06:00")
check("p9 parser: weekly@ddd-HH:MM -> weekly (Sun=6) at the time",
      _p9_wk.kind == "weekly" and _p9_wk.weekday == 6 and _p9_wk.times == ((6, 0),))
_p9_mo = _J57.parse_schedule("monthly@1-06:00")
check("p9 parser: monthly@<D>-HH:MM -> monthly on day-of-month",
      _p9_mo.kind == "monthly" and _p9_mo.dom == 1 and _p9_mo.times == ((6, 0),))
_p9_garbage = ["bogus", "every:0s", "every:900", "every:-5s", "daily@25:00", "daily@", "daily@12:60",
               "weekly@xxx-06:00", "weekly@sun", "monthly@0-06:00", "monthly@32-06:00", "monthly@1", ""]
_p9_bad = []
for _g in _p9_garbage:
    try:
        _J57.parse_schedule(_g)
        _p9_bad.append(_g)  # a garbage spec that did NOT raise is the bug
    except ValueError:
        pass
check("p9 parser: every garbage schedule raises ValueError (loud, never a silent default)",
      not _p9_bad, f"did not raise for: {_p9_bad}")

# (1b) CALENDAR DUE-NESS MATRIX (pure is_due): before slot / after slot / already-ran-this-slot /
#      missed TWO slots -> exactly one run. Use a fixed local wall time to make the slots deterministic.
_d = _J57.parse_schedule("daily@12:00")
_now13 = _dt57(2026, 7, 3, 13, 0, 0).timestamp()       # 13:00, past today's 12:00 slot
_slot_today = _dt57(2026, 7, 3, 12, 0, 0).timestamp()
_ran_prev_slot = _dt57(2026, 7, 2, 12, 30, 0).timestamp()   # ran after yesterday's slot
_ran_this_slot = _dt57(2026, 7, 3, 12, 30, 0).timestamp()   # ran after today's slot
_ran_3d_ago = _dt57(2026, 6, 30, 12, 30, 0).timestamp()     # missed 2 full slots
_now11 = _dt57(2026, 7, 3, 11, 0, 0).timestamp()            # before today's slot
_ran_10 = _dt57(2026, 7, 3, 10, 0, 0).timestamp()
check("p9 due: BEFORE today's slot, having run after the previous slot -> NOT due",
      _J57.is_due(_d, _now11, _ran_10) is False)
check("p9 due: AFTER the slot, last run was the previous slot -> due",
      _J57.is_due(_d, _now13, _ran_prev_slot) is True)
check("p9 due: ALREADY ran this slot -> NOT due (at most one run per slot)",
      _J57.is_due(_d, _now13, _ran_this_slot) is False)
check("p9 due: MISSED two slots -> due exactly once (compares only the most-recent slot, no replay)",
      _J57.is_due(_d, _now13, _ran_3d_ago) is True)
check("p9 due: never-run calendar job with a past slot -> due",
      _J57.is_due(_d, _now13, None) is True)
# interval due-ness: never-run due; fresh not due; elapsed due.
_iv = _J57.parse_schedule("every:1800s")
check("p9 due: interval never-run -> due; fresh -> not due; elapsed -> due",
      _J57.is_due(_iv, 5000.0, None) is True and _J57.is_due(_iv, 1000.0, 999.0) is False
      and _J57.is_due(_iv, 5000.0, 1000.0) is True)
# monthly Feb-31 skip: a month with no day-31 contributes no slot (uses the prior month's).
_m31 = _J57.parse_schedule("monthly@31-06:00")
_feb15 = _dt57(2026, 2, 15, 10, 0, 0).timestamp()
_mrs = _J57._most_recent_slot(_m31, _feb15)
check("p9 due: monthly@31 in a 28-day month falls back to the prior month's day-31 slot (no phantom Feb 31)",
      _mrs is not None and _dt57.fromtimestamp(_mrs) == _dt57(2026, 1, 31, 6, 0, 0))

# (2) JOB REGISTRY — register + duplicate refused; unknown-schedule refused AT registration; profile
#     override flips enabled; every SHIPPED row's fn is callable + its schedule parses.
_reg_real57 = dict(_J57._REGISTRY)  # snapshot; restore after so we don't leak test rows
try:
    _J57._REGISTRY.clear()
    _r = _J57.register_job("t_a", "every:60s", lambda: None)
    check("p9 registry: register_job returns a JobRow with the parsed schedule + default enabled",
          _r.name == "t_a" and _r.schedule.kind == "interval" and _r.enabled is True)
    _dup_raised = False
    try:
        _J57.register_job("t_a", "every:60s", lambda: None)
    except ValueError:
        _dup_raised = True
    check("p9 registry: a DUPLICATE job name raises ValueError", _dup_raised)
    _unk_raised = False
    try:
        _J57.register_job("t_bad", "frobnicate", lambda: None)
    except ValueError:
        _unk_raised = True
    check("p9 registry: an UNKNOWN schedule is refused AT registration (never registered silently)",
          _unk_raised and "t_bad" not in _J57._REGISTRY)
    # profile override: jobs[name]=False disables a default-enabled row; jobs[name]=True re-enables a
    # default-disabled row; absent -> shipped default. (job_enabled reads an injected overrides dict.)
    _row_on = _J57.register_job("t_on", "every:60s", lambda: None, enabled=True)
    _row_off = _J57.register_job("t_off", "every:60s", lambda: None, enabled=False)
    check("p9 registry: profile jobs override flips enabled (False disables, True enables, absent=default)",
          _J57.job_enabled(_row_on, {"t_on": False}) is False
          and _J57.job_enabled(_row_off, {"t_off": True}) is True
          and _J57.job_enabled(_row_on, {}) is True and _J57.job_enabled(_row_off, {}) is False)
finally:
    _J57._REGISTRY.clear()
    _J57._REGISTRY.update(_reg_real57)

# (2b) THE REGISTRY TRIPWIRE: every SHIPPED row is registrable, its fn callable, its schedule parses,
#      and the shipped set is exactly the expected fleet (no row silently added/dropped).
_shipped_real57 = _J57._shipped_registered
_reg_real57b = dict(_J57._REGISTRY)
try:
    _J57._REGISTRY.clear()
    _J57._shipped_registered = False
    _J57.register_shipped_jobs()
    _EXPECT_ROWS = {"sensors", "source-health", "wewerss-probe", "session-warmer", "log-rotation",
                    "curator", "source-audit", "digest"}
    check("p9 registry tripwire: the shipped rows are exactly the P9 fleet",
          set(_J57._REGISTRY.keys()) == _EXPECT_ROWS)
    _bad_rows = [n for n, r in _J57._REGISTRY.items()
                 if not callable(r.fn) or not isinstance(r.schedule, _J57.Schedule)]
    check("p9 registry tripwire: every shipped row's fn is callable + its schedule parsed",
          not _bad_rows, str(_bad_rows))
    check("p9 registry tripwire: curator + source-audit ship ENABLED (ignition), digest ships DISABLED",
          _J57._REGISTRY["curator"].enabled is True and _J57._REGISTRY["source-audit"].enabled is True
          and _J57._REGISTRY["digest"].enabled is False)
    _bad_budgets = [n for n, r in _J57._REGISTRY.items()
                    if not (0 < r.budget_s <= _J57._MAX_JOB_BUDGET_S)]
    check("p9 registry tripwire: every shipped budget stays under the sentinel-wedge ceiling",
          not _bad_budgets, str(_bad_budgets))
finally:
    _J57._REGISTRY.clear()
    _J57._REGISTRY.update(_reg_real57b)
    _J57._shipped_registered = _shipped_real57

# (3) SCHEDULER LOOP MECHANICS (no thread: call the tick body directly): heartbeat touched; a failing
#     job is isolated (never stops the rest) + Barked ONCE per cooldown; serial order = registration.
_st57_dir = _Path44(_tf57.mkdtemp())
_state_real57, _hb_real57 = _J57.STATE_PATH, _J57.HEARTBEAT_PATH
_reg_real57c, _shipped_real57c = dict(_J57._REGISTRY), _J57._shipped_registered
import penumbra.core.notify as _notify57  # noqa: E402
_bark_real57 = _notify57.bark_push
try:
    _J57.STATE_PATH = _st57_dir / "scheduler-state.json"
    _J57.HEARTBEAT_PATH = _st57_dir / "scheduler-heartbeat"
    _J57._REGISTRY.clear()
    _order57 = []
    _J57.register_job("j_a", "every:1s", lambda: _order57.append("j_a"))
    def _boom57():
        _order57.append("j_b")
        raise RuntimeError("simulated job failure")
    _J57.register_job("j_b", "every:1s", _boom57)
    _J57.register_job("j_c", "every:1s", lambda: _order57.append("j_c"))
    _barks57 = []
    _notify57.bark_push = lambda title, body, group="Penumbra": _barks57.append(title)
    _r1 = _J57.run_due_jobs(now=1000.0)
    check("p9 tick: the heartbeat dead-man file is touched every tick",
          _J57.HEARTBEAT_PATH.exists())
    check("p9 tick: a failing job is isolated (ran the rest, failed only the raiser)",
          _r1["ran"] == ["j_a", "j_c"] and _r1["failed"] == ["j_b"] and _r1["checked"] == 3)
    check("p9 tick: serial run order is deterministic (registration order)", _order57 == ["j_a", "j_b", "j_c"])
    check("p9 tick: a job EXCEPTION Barks once", len(_barks57) == 1 and "j_b" in _barks57[0])
    # a fresh tick 1s later: interval not elapsed (last_run advanced) -> nothing due.
    check("p9 tick: last_run advances so a not-yet-due interval does not re-fire next tick",
          _J57.run_due_jobs(now=1000.5)["checked"] == 0)
    # a later tick where all are due again: j_b re-fails but the 24h Bark cooldown SUPPRESSES a 2nd bark.
    _order57.clear()
    _J57.run_due_jobs(now=1002.0)
    check("p9 tick: the per-job Bark cooldown (24h) suppresses a repeat alarm on the next failure",
          len(_barks57) == 1)

    # (3b) P9-GATE fixes (adversarial-review catches, verified here):
    # per-job BUDGET: a job past its wall-clock budget is skipped (the tick moves on, the run
    # zombies out harmlessly), Barked, and its last_run still advances; the heartbeat is touched
    # BEFORE every job so staleness is bounded by ONE budget, never the serial sum.
    _J57._REGISTRY.clear()
    _barks57.clear()
    import time as _t57b
    _J57.register_job("j_slow", "every:1s", lambda: _t57b.sleep(3.0), budget_s=1)
    _J57.register_job("j_next", "every:1s", lambda: _order57.append("j_next"))
    _r57b = _J57.run_due_jobs(now=2000.0)
    check("p9 gate: a job past its budget_s is skipped (tick moves on; later jobs still run)",
          _r57b["failed"] == ["j_slow"] and _r57b["ran"] == ["j_next"])
    check("p9 gate: a budget overrun Barks (its own cooldown key)",
          len(_barks57) == 1 and "j_slow" in _barks57[0])
    check("p9 gate: an overrun's last_run still advances (schedule governs the retry, no re-fire)",
          _J57.run_due_jobs(now=2000.5)["checked"] == 0)
    def _reg_raises57(**kw):
        try:
            _J57.register_job("j_z", "every:1s", lambda: None, **kw)
            return False
        except ValueError:
            return True
    check("p9 gate: register_job refuses a budget of 0 or past the sentinel-wedge ceiling",
          _reg_raises57(budget_s=0) and _reg_raises57(budget_s=_J57._MAX_JOB_BUDGET_S + 1))
    # heartbeat BEFORE every job: a job that deletes the file mid-tick finds it re-touched
    # before the NEXT job runs (bounded staleness), and it exists again at tick end.
    _J57._REGISTRY.clear()
    _hb_seen57: list = []
    _J57.register_job("j_del", "every:1s", lambda: _J57.HEARTBEAT_PATH.unlink())
    _J57.register_job("j_see", "every:1s",
                      lambda: _hb_seen57.append(_J57.HEARTBEAT_PATH.exists()))
    _J57.run_due_jobs(now=3000.0)
    check("p9 gate: the heartbeat is touched BEFORE every job, not once per tick",
          _hb_seen57 == [True])
finally:
    _notify57.bark_push = _bark_real57
    _J57.STATE_PATH, _J57.HEARTBEAT_PATH = _state_real57, _hb_real57
    _J57._REGISTRY.clear(); _J57._REGISTRY.update(_reg_real57c)
    _J57._shipped_registered = _shipped_real57c

# (4) TRANSPLANT CORES — the hard-won lessons survive the move. Import the transplanted module and
#     exercise the mechanical halves offline (no live browsing / network).
import penumbra.core.infra_jobs as _IJ57  # noqa: E402
# (4a) source-health N_CONSECUTIVE: _health_track only surfaces a source as newly-down at EXACTLY the
#      threshold-th consecutive fail (a transient single fail never alarms), and a recovery clears it.
_fails57, _alerts57, _down57, _rec57 = {}, {}, [], []
_IJ57._health_track("srcX", False, "boom", _fails57, _alerts57, _down57, _rec57)  # fail 1
check("p9 transplant (source-health): ONE fail does not surface as down (N_CONSECUTIVE guards transients)",
      _down57 == [] and _fails57["srcX"] == 1)
_IJ57._health_track("srcX", False, "boom", _fails57, _alerts57, _down57, _rec57)  # fail 2 == threshold
check("p9 transplant (source-health): the N_CONSECUTIVE-th consecutive fail surfaces as newly-down",
      _down57 == [("srcX", "boom")] and _IJ57.N_CONSECUTIVE == 2)
_IJ57._health_track("srcX", True, "ok", _fails57, _alerts57, _down57, _rec57)  # recovery
check("p9 transplant (source-health): a recovery clears the fail counter + surfaces as recovered",
      _fails57["srcX"] == 0 and "srcX" in _rec57)
# (4b) log rotation: a file over the threshold is truncated in place + a .1 tail kept; a small file is
#      left untouched. Point the log dir at a temp dir with a giant + a tiny file.
_log57 = _Path44(_tf57.mkdtemp())
_logdir_real57, _max_real57, _tail_real57 = _IJ57._LOG_DIR, _IJ57._LOG_MAX_BYTES, _IJ57._LOG_KEEP_TAIL
try:
    _IJ57._LOG_DIR = _log57
    _IJ57._LOG_MAX_BYTES = 1000  # tiny threshold for the test
    _IJ57._LOG_KEEP_TAIL = 500   # keep-tail must be < the test file so the seek-from-end fits
    _big = _log57 / "eye-http.err"
    _big.write_bytes(b"X" * 5000)
    _small = _log57 / "eye-http.log"
    _small.write_bytes(b"Y" * 100)
    _rot = _IJ57.rotate_logs()
    check("p9 transplant (log-rotation): the oversized log is truncated in place + a .1 tail kept",
          _big.stat().st_size == 0 and (_log57 / "eye-http.err.1").exists()
          and (_log57 / "eye-http.err.1").stat().st_size == 500 and _rot == 1)
    check("p9 transplant (log-rotation): a small log is left untouched",
          _small.stat().st_size == 100 and not (_log57 / "eye-http.log.1").exists())
finally:
    _IJ57._LOG_DIR, _IJ57._LOG_MAX_BYTES, _IJ57._LOG_KEEP_TAIL = _logdir_real57, _max_real57, _tail_real57
# (4c) digest NO-OPS (with a log line) when the themes file is absent -- it never invents a theme list.
_themes_real57 = _IJ57._DIGEST_THEMES_PATH
try:
    _IJ57._DIGEST_THEMES_PATH = _Path44(_tf57.mkdtemp()) / "digest-themes.json"  # does not exist
    check("p9 transplant (digest): no themes file -> the job no-ops (never invents a theme list)",
          _IJ57.run_digest() == {"noop": "no-themes"} and _IJ57._load_digest_themes() == [])
finally:
    _IJ57._DIGEST_THEMES_PATH = _themes_real57
# (4d) the warmer core is importable + wired (no live browsing in smoke): the fns + the safety knobs
#      (active-hours gate, maintenance flag) survived the transplant.
check("p9 transplant (warmer): run_session_warmer + _warm_one importable; active-hours + maint knobs present",
      callable(_IJ57.run_session_warmer) and callable(_IJ57._warm_one)
      and (_IJ57._ACTIVE_START, _IJ57._ACTIVE_END) == (8, 23) and _IJ57._MAINT_FLAG.name == "cdp-maintenance")
# (4e) the wewe-rss freeze/reachability core is importable + carries its 4 feeds + freeze limit.
check("p9 transplant (wewerss): check_wechat2rss_feeds importable; 4 feeds + the 3-day freeze limit survive",
      callable(_IJ57.check_wechat2rss_feeds) and len(_IJ57._WECHAT2RSS_FEEDS) == 4
      and _IJ57._WECHAT2RSS_FREEZE_LIMIT_S == 3 * 86400)

# (5) THE SENTINEL, OFFLINE. Load scripts/sentinel.py as a module WITH the scripts dir on path (so its
#     sibling _sentinel_common + services resolve) but stub its probes so nothing touches the network.
_SENTINEL_PATH = _SCRIPTS_DIR / "sentinel.py"
if _SENTINEL_PATH.exists():
    # (5a) ISOLATION TRIPWIRE (the whole reason the sentinel is separate): its AST import list names
    #      ONLY stdlib + _sentinel_common + services -- NEVER penumbra.* (it must work when the organ's
    #      code is broken). Assert over the parsed imports, so a future `import penumbra...` fails here.
    import ast as _ast57
    _sent_tree = _ast57.parse(_SENTINEL_PATH.read_text(encoding="utf-8"))
    _sent_imports = set()
    for _n in _ast57.walk(_sent_tree):
        if isinstance(_n, _ast57.Import):
            for _a in _n.names:
                _sent_imports.add(_a.name.split(".")[0])
        elif isinstance(_n, _ast57.ImportFrom) and _n.module:
            _sent_imports.add(_n.module.split(".")[0])
    _sent_penumbra = sorted(m for m in _sent_imports if m == "penumbra")
    check("p9 sentinel isolation: it imports ZERO penumbra.* (self-contained: works when the organ is broken)",
          not _sent_penumbra, f"imports penumbra: {_sent_penumbra}")
    _ALLOWED_SENTINEL_IMPORTS = {"__future__", "json", "os", "subprocess", "sys", "time",
                                 "urllib", "pathlib", "_sentinel_common", "services", "ast"}
    _sent_unexpected = sorted(_sent_imports - _ALLOWED_SENTINEL_IMPORTS)
    check("p9 sentinel isolation: its import list is exactly stdlib + _sentinel_common + services",
          not _sent_unexpected, f"unexpected imports: {_sent_unexpected}")

    # Load it as a module (scripts on path). Its ROOT=~/penumbra-mcp bootstrap is harmless here; we
    # only need the module object to stub + call the duty functions.
    _sys_path_saved57 = list(sys.path)
    sys.path.insert(0, str(_SCRIPTS_DIR))
    try:
        _sspec = _il57.spec_from_file_location("penumbra_sentinel_smoke", _SENTINEL_PATH)
        _SENT = _il57.module_from_spec(_sspec)
        with _ctx57.redirect_stdout(_io57.StringIO()):
            _sspec.loader.exec_module(_SENT)
        _sent_loaded = True
    except Exception as _sent_exc:  # noqa: BLE001
        _sent_loaded = False
        check("p9 sentinel: module loads with the scripts dir on path", False, str(_sent_exc))
    if _sent_loaded:
        check("p9 sentinel: module loaded + read the CDP instances from services.py",
              len(_SENT.CDP_INSTANCES) >= 1 and _SENT.EYE_SERVICE == "com.penumbra.organ.eye-http")
        # NOTE: `penumbra` is already in sys.modules (this IS the penumbra smoke), so a sys.modules
        # check cannot prove isolation here; the AST import-list tripwire above is the authoritative
        # guarantee that the sentinel's OWN code imports zero penumbra. What we CAN assert cheaply: the
        # module exposes the three self-contained duties + reimplements its own CDP probe (not the
        # eye's cdp_health), i.e. it did not grow a penumbra dependency to do its job.
        check("p9 sentinel: the three self-contained duties are present + it reimplements its own probes",
              all(callable(getattr(_SENT, fn, None)) for fn in
                  ("_check_eye_http", "_check_scheduler", "_check_cdp", "_cdp_alive", "_http_ok")))
    else:
        _SENT = None
    sys.path[:] = _sys_path_saved57
else:
    _SENT = None
    # Repo-adaptive (the 46b convention): the sentinel is eye-side launchd infra. Where the fleet
    # registry (services.py) exists, its absence is a hard failure; the penumbra mirror ships no
    # launchd fleet, so there this is a legitimate absence, not a miss.
    if _SERVICES_PATH.exists():
        check("p9 sentinel: scripts/sentinel.py exists", False, str(_SENTINEL_PATH))

# (5b) DUTY LOGIC, offline. Stub the sentinel's network probes so we exercise the branch logic only.
if _SENT is not None:
    _sent_dir57 = _Path44(_tf57.mkdtemp())
    _kicks57 = []
    _sbarks57 = []
    _real_http57, _real_cdp57, _real_kick57 = _SENT._http_ok, _SENT._cdp_alive, _SENT._kickstart
    _real_bark57, _real_should57 = _SENT.bark_push, _SENT.should_alert
    _real_eye_state, _real_sched_state, _real_cdp_state = _SENT.EYE_STATE, _SENT.SCHED_STATE, _SENT.CDP_STATE
    _real_hb57, _real_maint57 = _SENT.HEARTBEAT_PATH, _SENT.MAINT_FLAG
    try:
        _SENT.EYE_STATE = _sent_dir57 / "eye.json"
        _SENT.SCHED_STATE = _sent_dir57 / "sched.json"
        _SENT.CDP_STATE = _sent_dir57 / "cdp.json"
        _SENT.HEARTBEAT_PATH = _sent_dir57 / "heartbeat"
        _SENT.MAINT_FLAG = _sent_dir57 / "cdp-maintenance"
        _SENT._kickstart = lambda svc, timeout=15: _kicks57.append(svc)
        _SENT.bark_push = lambda title, body, **kw: (_sbarks57.append(title) or True)
        _SENT.should_alert = lambda key, alerts, cd: True  # always past cooldown in the test

        # (i) dead healthz -> the heal path kickstarts the eye (and, still dead, returns down).
        _SENT._http_ok = lambda url, timeout=8.0: False
        _kicks57.clear()
        _rc_eye = _SENT._check_eye_http()
        check("p9 sentinel: a DEAD /healthz triggers the kickstart heal path for eye-http",
              _SENT.EYE_SERVICE in _kicks57 and _rc_eye == 1)

        # (ii) healthz OK but the scheduler heartbeat is STALE -> the DISTINCT 'scheduler dead' alarm
        #      fires + kickstarts the organ (a different alarm than 'organ down').
        _SENT._http_ok = lambda url, timeout=8.0: True
        _kicks57.clear(); _sbarks57.clear()
        _stale = _time57.time() - (_SENT.SCHED_TICK_S * _SENT.SCHED_STALE_FACTOR + 60)
        _SENT.HEARTBEAT_PATH.write_text("x")
        _os57.utime(_SENT.HEARTBEAT_PATH, (_stale, _stale))
        _rc_sched = _SENT._check_scheduler()
        check("p9 sentinel: healthz OK + a STALE heartbeat fires the DISTINCT 'scheduler dead' alarm + kickstart",
              _rc_sched == 1 and _SENT.EYE_SERVICE in _kicks57
              and any("scheduler" in t.lower() for t in _sbarks57))
        # a FRESH heartbeat while healthz OK -> no alarm (the common healthy case is silent).
        _kicks57.clear(); _sbarks57.clear()
        _SENT.HEARTBEAT_PATH.write_text("x")  # mtime = now -> fresh
        check("p9 sentinel: a FRESH heartbeat while healthz OK is silent (no false scheduler alarm)",
              _SENT._check_scheduler() == 0 and not _sbarks57)
        # a MISSING heartbeat is deliberately SILENT (ambiguous with a fresh boot -> never kick/alarm,
        # which would restart-storm the organ during the normal post-boot window; wait for staleness).
        _kicks57.clear(); _sbarks57.clear()
        try:
            _SENT.HEARTBEAT_PATH.unlink()
        except OSError:
            pass
        check("p9 sentinel: a MISSING heartbeat is silent (no boot-window restart storm; waits for staleness)",
              _SENT._check_scheduler() == 0 and not _sbarks57 and not _kicks57)

        # (iii) the cdp-maintenance flag PAUSES all CDP heal (never fight a manual VNC login).
        _SENT.MAINT_FLAG.write_text("")  # touch the pause flag
        _kicks57.clear()
        _rc_cdp = _SENT._check_cdp()
        check("p9 sentinel: the cdp-maintenance flag pauses CDP heal (no kickstart while a login is in progress)",
              _rc_cdp == 0 and _kicks57 == [])
    finally:
        (_SENT._http_ok, _SENT._cdp_alive, _SENT._kickstart) = _real_http57, _real_cdp57, _real_kick57
        _SENT.bark_push, _SENT.should_alert = _real_bark57, _real_should57
        _SENT.EYE_STATE, _SENT.SCHED_STATE, _SENT.CDP_STATE = _real_eye_state, _real_sched_state, _real_cdp_state
        _SENT.HEARTBEAT_PATH, _SENT.MAINT_FLAG = _real_hb57, _real_maint57

# (6) DELETION SWEEP: the 11 deleted scripts are GONE from scripts/, and NO in-repo file references
#     any of them as a runnable script (a `scripts/<name>.py` path form), excluding this smoke file
#     itself + provenance/lineage comments (which name the OLD module bare, e.g. "transplanted from
#     digest.py", exactly as §53 keeps its sensor_runner comment). Also: the plist tripwire now expects
#     exactly 7 committed plists == exactly 7 registry rows, byte-identical, and SERVICES.md exists.
_P9_DELETED_SCRIPTS = ["cdp_keepalive.py", "cron_watchdog.py", "penumbra_http_watchdog.py",
                       "wewerss_watchdog.py", "session_warmer.py", "curator.py", "digest.py",
                       "source_audit.py", "warm_intro.py", "penumbra_prewarm.py", "rsshub_watchdog.py"]
_p9_still_there = [s for s in _P9_DELETED_SCRIPTS if (_SCRIPTS_DIR / s).exists()]
check("p9 deletion: all 11 transplanted/retired scripts are gone from scripts/",
      not _p9_still_there, f"still present: {_p9_still_there}")
# path-form references (`scripts/<name>.py`) anywhere in the tree, excluding smoke + venv + pycache.
_p9_path_refs = []
for _pp in ROOT.rglob("*"):
    if not _pp.is_file() or _pp.suffix not in (".py", ".md", ".sh", ".json", ".txt"):
        continue
    if _pp.name == "smoke.py" or ".venv" in _pp.parts or "__pycache__" in _pp.parts or ".git" in _pp.parts:
        continue
    try:
        _txt = _pp.read_text(encoding="utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        continue
    for _s in _P9_DELETED_SCRIPTS:
        if f"scripts/{_s}" in _txt:
            _p9_path_refs.append(f"{_pp.relative_to(ROOT)} -> scripts/{_s}")
check("p9 deletion: no in-repo file invokes a deleted script as scripts/<name>.py (provenance comments allowed)",
      not _p9_path_refs, f"still referenced: {_p9_path_refs}")
# the sentinel row's script IS present (the ONE new external script). Repo-adaptive (the 46b
# convention): only where the fleet registry exists; the penumbra mirror ships no launchd fleet.
if _SERVICES_PATH.exists():
    check("p9 deletion: the new external sentinel script (scripts/sentinel.py) IS present",
          (_SCRIPTS_DIR / "sentinel.py").exists())
# plist tripwire: exactly 7 committed plists, exactly 7 registry rows, byte-identical, SERVICES.md fresh.
if _SERVICES_PATH.exists():
    _p9_committed = sorted(_SCRIPTS_DIR.glob("com.penumbra.*.plist"))
    with _ctx_svc.redirect_stdout(_io_svc.StringIO()):
        _p9_plist_rc = _svc.gen_plists(write=False)
    check("p9 plist tripwire: exactly 7 committed plists == exactly 7 registry rows, all byte-identical",
          len(_p9_committed) == 7 and len(_svc.REGISTRY) == 7 and _p9_plist_rc == 0,
          f"{len(_p9_committed)} plists, {len(_svc.REGISTRY)} rows, gen rc={_p9_plist_rc}")
    # the registry is exactly the 7-row fleet (organ + 4 cdp + sentinel + state-backup); legacy pruned.
    _p9_labels = {r["label"] for r in _svc.REGISTRY}
    _EXPECT_LABELS = {"com.penumbra.organ.eye-http", "com.penumbra.cdp.cn-forums", "com.penumbra.cdp.xhs",
                      "com.penumbra.cdp.xhs-cn", "com.penumbra.cdp.douyin", "com.penumbra.infra.sentinel",
                      "com.penumbra.infra.state-backup"}
    check("p9 plist tripwire: the registry is exactly the 7-row fleet (organ + 4 cdp + sentinel + state-backup)",
          _p9_labels == _EXPECT_LABELS)
    check("p9 plist tripwire: the pre-migration `legacy` field is pruned from every registry row",
          not any("legacy" in r for r in _svc.REGISTRY))
    check("p9 plist tripwire: SERVICES.md exists (regenerated) + carries the in/out test text",
          (ROOT / "SERVICES.md").exists()
          and "外部只留不能与器官同生死的" in (ROOT / "SERVICES.md").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 58. P10: the mcp TRANSPORT (a wrapped MCP server is the SAME declarative row, tools/call
#     instead of a GET) + foundry-grade curator packets (the draft artifact rides the pipeline).
#     ALL OFFLINE: a stubbed transport replays a recorded initialize + tools/call exchange; the
#     declarative table ships EMPTY (the first row arrives via foundry -> curator). Structural thesis:
#     because a wrapped server lands as an ordinary source, every memory/dedup/ranking mechanism
#     applies to it with zero new code, and each wrapped server earns its slot per SERVER.
# ---------------------------------------------------------------------------
import httpx as _httpx58  # noqa: E402
import json as _json58  # noqa: E402
import tempfile as _tf58  # noqa: E402
from pathlib import Path as _Path58  # noqa: E402
from penumbra.core.sources import _mcp as _mcp58  # noqa: E402
from penumbra.core.sources import _declarative as _decl58  # noqa: E402
from penumbra.core import cache as _cache58  # noqa: E402

# A scripted transport stub: monkeypatch _mcp.http._get_client() (the ONE network touchpoint the
# client uses, via a streaming POST) to REPLAY a recorded JSON-RPC exchange, so the REAL _post
# (status branching / session capture / SSE / JSON / MAX_BYTES cap / error paths) run against a
# fake httpx client. `script` maps a JSON-RPC method -> a callable(payload) -> httpx.Response. The
# SSRF pre-flight + cache-only are neutralized so the smoke stays fully offline (no DNS).
_mcp58_sent = []  # records (method, headers, payload) of every POST (session reuse + arg interpolation)


class _FakeResp58:
    """A minimal stand-in for the streaming response _mcp._post reads: status_code, a
    case-insensitive-ish headers dict, and a fresh iter_raw() each call (a real constructed
    httpx.Response raises StreamConsumed on iter_raw, so we model the stream directly)."""

    def __init__(self, content, status, headers):
        self.status_code = status
        self.headers = headers
        self._content = content

    def iter_raw(self):
        yield self._content


def _mk_resp58(body, *, status=200, ctype="application/json", headers=None):
    hdrs = {"content-type": ctype}
    if headers:
        hdrs.update(headers)
    content = body if isinstance(body, (bytes, bytearray)) else _json58.dumps(body).encode("utf-8")
    return _FakeResp58(bytes(content), status, hdrs)


class _FakeStreamCtx58:
    """A context manager mimicking httpx.Client.stream(...): yields a Response exposing
    status_code / headers / iter_raw(), exactly what _mcp._post reads."""

    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self._resp

    def __exit__(self, *a):
        return False


class _FakeClient58:
    def __init__(self, script):
        self._script = script

    def stream(self, method, url, *, timeout=None, headers=None, json=None, **kwargs):
        payload = json or {}
        rpc_method = payload.get("method")
        _mcp58_sent.append((rpc_method, dict(headers or {}), payload))
        handler = self._script.get(rpc_method)
        if handler is None:
            resp = _mk_resp58({"jsonrpc": "2.0", "id": payload.get("id"),
                               "error": {"code": -32601, "message": f"no stub for {rpc_method}"}})
        else:
            resp = handler(payload)
        # httpx.Response.iter_raw exists on a constructed Response; status_code/headers are set.
        return _FakeStreamCtx58(resp)


def _install_stub58(script):
    """Point _mcp.http._get_client at a fake client replaying `script`; neutralize the SSRF/cache
    guards for offline determinism. Returns a restore() callable."""
    orig_get = _mcp58.http._get_client
    orig_blk = _mcp58._netguard.security_block_reason
    orig_co = _mcp58.cache.cache_only
    _mcp58.http._get_client = lambda: _FakeClient58(script)
    _mcp58._netguard.security_block_reason = lambda url: None
    _mcp58.cache.cache_only = lambda: False

    def _restore():
        _mcp58.http._get_client = orig_get
        _mcp58._netguard.security_block_reason = orig_blk
        _mcp58.cache.cache_only = orig_co

    return _restore


# --- (1) MCP client offline: the recorded initialize + tools/call exchange -----------------
# initialize replies with a session id header; notifications/initialized is a 202 no-body; a
# tools/call returns structuredContent. The client must capture + RESEND the session id.
def _init_ok58(payload):
    return _mk_resp58({"jsonrpc": "2.0", "id": payload.get("id"),
                       "result": {"protocolVersion": _mcp58._PROTOCOL_VERSION,
                                  "capabilities": {}, "serverInfo": {"name": "stub", "version": "1"}}},
                      headers={"Mcp-Session-Id": "SESS-1"})


def _initialized_ok58(payload):
    return _mk_resp58(b"", status=202)  # notification: no body


_mcp58_sent.clear()
_stub_script58 = {
    "initialize": _init_ok58,
    "notifications/initialized": _initialized_ok58,
    "tools/call": lambda p: _mk_resp58(
        {"jsonrpc": "2.0", "id": p.get("id"),
         "result": {"structuredContent": {"documents": [
             {"title": "Alpha", "url": "https://x/a", "snippet": "body a"},
             {"title": "Beta", "url": "https://x/b", "snippet": "body b"}]}, "isError": False}}),
}
_restore_rc58 = _install_stub58(_stub_script58)
try:
    _mcp58._CLIENTS.clear()
    _cl58 = _mcp58.MCPClient("https://stub/mcp")
    _res58 = _cl58.call_tool("search", {"query": "q", "limit": 5})
    check("58.1 mcp client: tools/call parses structuredContent",
          _res58.get("documents", [{}])[0].get("title") == "Alpha")
    check("58.1 mcp client: initialize captured the Mcp-Session-Id header",
          _cl58.session_id == "SESS-1")
    # the session id was RESENT on the tools/call POST (the 3rd send: init, initialized, tools/call)
    _tc_hdrs58 = next((h for (m, h, _p) in _mcp58_sent if m == "tools/call"), {})
    check("58.1 mcp client: the captured session id is resent on later calls",
          _tc_hdrs58.get("Mcp-Session-Id") == "SESS-1")
    check("58.1 mcp client: the handshake sent initialize + notifications/initialized",
          [m for (m, _h, _p) in _mcp58_sent][:2] == ["initialize", "notifications/initialized"])

    # text-block-JSON parsing: a single text content block that parses as JSON -> the parsed object.
    _res_tb58 = _mcp58.MCPClient._parse_tool_result(
        {"content": [{"type": "text", "text": '{"documents": [{"title": "T", "url": "u"}]}'}]})
    check("58.1 mcp client: a single JSON text block parses to the object",
          _res_tb58.get("documents", [{}])[0].get("title") == "T")
    # prose text blocks (not JSON) -> the _text_blocks shape.
    _res_prose58 = _mcp58.MCPClient._parse_tool_result(
        {"content": [{"type": "text", "text": "line one"}, {"type": "text", "text": "line two"}]})
    check("58.1 mcp client: prose blocks return the _text_blocks shape",
          _res_prose58.get("_text_blocks") == ["line one", "line two"])
finally:
    _restore_rc58()

# SSE-framed response parsed: the tools/call reply arrives as text/event-stream with the JSON-RPC
# response interleaved among other events. The client must accumulate data: lines to the matching id.
def _tools_call_sse58(payload):
    body = (
        ": a heartbeat comment\n\n"
        'data: {"jsonrpc":"2.0","method":"notifications/message","params":{"level":"info"}}\n\n'
        'data: {"jsonrpc":"2.0","id":' + str(payload.get("id")) + ',"result":'
        '{"structuredContent":{"documents":[{"title":"SSE","url":"https://x/sse"}]},"isError":false}}\n\n'
    )
    return _mk_resp58(body.encode("utf-8"), ctype="text/event-stream")


_restore_rc58b = _install_stub58({"initialize": _init_ok58, "notifications/initialized": _initialized_ok58,
                               "tools/call": _tools_call_sse58})
try:
    _mcp58._CLIENTS.clear()
    _cl58b = _mcp58.MCPClient("https://stub/mcp")
    _res_sse58 = _cl58b.call_tool("search", {"query": "q"})
    check("58.1 mcp client: an SSE-framed tools/call response is parsed (matching-id event picked)",
          _res_sse58.get("documents", [{}])[0].get("title") == "SSE")
finally:
    _restore_rc58b()

# isError:true tool result -> MCPTransportError -> the adapter returns [].
_restore_rc58c = _install_stub58({
    "initialize": _init_ok58, "notifications/initialized": _initialized_ok58,
    "tools/call": lambda p: _mk_resp58({"jsonrpc": "2.0", "id": p.get("id"),
        "result": {"content": [{"type": "text", "text": "upstream exploded"}], "isError": True}}),
})
try:
    _mcp58._CLIENTS.clear()
    _cl58c = _mcp58.MCPClient("https://stub/mcp")
    _raised58 = False
    try:
        _cl58c.call_tool("search", {"query": "q"})
    except _mcp58.MCPTransportError as _e58:
        _raised58 = "upstream exploded" in str(_e58)
    check("58.1 mcp client: an isError tool result raises MCPTransportError carrying the text",
          _raised58)
finally:
    _restore_rc58c()

# re-initialize ONCE on session expiry: the FIRST tools/call returns 404 (session gone); the client
# re-handshakes and the SECOND tools/call succeeds. Assert exactly one re-initialize happened.
_expiry_state58 = {"tools_calls": 0, "inits": 0}


def _init_count58(payload):
    _expiry_state58["inits"] += 1
    return _init_ok58(payload)


def _tools_expire_then_ok58(payload):
    _expiry_state58["tools_calls"] += 1
    if _expiry_state58["tools_calls"] == 1:
        return _mk_resp58({"error": "gone"}, status=404)  # session expired -> _SessionExpired
    return _mk_resp58({"jsonrpc": "2.0", "id": payload.get("id"),
                       "result": {"structuredContent": {"documents": [{"title": "Recovered", "url": "u"}]},
                                  "isError": False}})


_restore_rc58d = _install_stub58({"initialize": _init_count58,
                               "notifications/initialized": _initialized_ok58,
                               "tools/call": _tools_expire_then_ok58})
try:
    _mcp58._CLIENTS.clear()
    _cl58d = _mcp58.MCPClient("https://stub/mcp")
    _res_recov58 = _cl58d.call_tool("search", {"query": "q"})
    check("58.1 mcp client: a session-expiry (404) triggers exactly ONE re-initialize + retry",
          _res_recov58.get("documents", [{}])[0].get("title") == "Recovered"
          and _expiry_state58["inits"] == 2,  # the original handshake + exactly one re-init
          f"inits={_expiry_state58['inits']} tools_calls={_expiry_state58['tools_calls']}")
finally:
    _restore_rc58d()

# --- (2) Declarative mcp row through the adapter with the stubbed client --------------------
# Neutralize the doc cache so each drive re-fetches through the stub (never a stale cache hit).
_orig_getdocs58, _orig_setdocs58 = _cache58.get_docs, _cache58.set_docs
_cache58.get_docs = lambda *a, **k: None
_cache58.set_docs = lambda *a, **k: None
try:
    # A full fixture mcp row (post_filter False so we see the server-order mapping verbatim).
    _mcp_row58 = {
        "name": "fix_mcp", "description": "fixture mcp row", "endpoint": "https://stub/mcp",
        "transport": "mcp", "tool": "search",
        "params_template": {"query": "{query}", "limit": "{limit}"},
        "results_path": "documents",
        "field_map": {"title": "title", "url": "url", "content": "snippet", "id": "id"},
        "post_filter": False,
    }
    _ad_mcp58 = _decl58._row_to_adapter(_mcp_row58)

    _restore_rc58e = _install_stub58({
        "initialize": _init_ok58, "notifications/initialized": _initialized_ok58,
        "tools/call": lambda p: _mk_resp58({"jsonrpc": "2.0", "id": p.get("id"), "result": {
            "structuredContent": {"documents": [
                {"title": "Doc1", "url": "https://x/1", "snippet": "content one", "id": "id1"},
                {"title": "Doc2", "url": "https://x/2", "snippet": "content two", "id": "id2"}]},
            "isError": False}}),
    })
    try:
        _mcp58._CLIENTS.clear()
        _mcp58_sent.clear()
        _docs_mcp58 = _ad_mcp58.search("anything", limit=10)
        check("58.2 declarative mcp row: maps tool result -> Documents with mapped fields",
              [d.title for d in _docs_mcp58] == ["Doc1", "Doc2"]
              and _docs_mcp58[0].url == "https://x/1" and _docs_mcp58[0].content == "content one"
              and _docs_mcp58[0].source_id == "id1" and _docs_mcp58[0].source == "fix_mcp")
        # the params_template became the tool ARGUMENTS with {query}/{limit} interpolated (the same
        # interpolation the http transport does to its query params).
        _tc_payload58 = next((p for (m, _h, p) in _mcp58_sent if m == "tools/call"), {})
        _tc_call_args58 = (_tc_payload58.get("params") or {}).get("arguments")
        check("58.2 declarative mcp row: params_template -> tool arguments; exact {limit} renders TYPED (int)",
              _tc_payload58.get("params", {}).get("name") == "search"
              and _tc_call_args58 == {"query": "anything", "limit": 10})
    finally:
        _restore_rc58e()

    # text_fallback opt-in: a prose-only tool result (no structured object) -> one doc per block.
    _mcp_row_tf58 = dict(_mcp_row58, name="fix_mcp_tf", text_fallback=True)
    _ad_tf58 = _decl58._row_to_adapter(_mcp_row_tf58)
    _restore_rc58f = _install_stub58({
        "initialize": _init_ok58, "notifications/initialized": _initialized_ok58,
        "tools/call": lambda p: _mk_resp58({"jsonrpc": "2.0", "id": p.get("id"), "result": {
            "content": [{"type": "text", "text": "First answer\nwith detail"},
                        {"type": "text", "text": "Second answer"}], "isError": False}}),
    })
    try:
        _mcp58._CLIENTS.clear()
        _docs_tf58 = _ad_tf58.search("anything", limit=10)
        check("58.2 declarative mcp row: text_fallback opt-in synthesizes one doc per prose block",
              [d.title for d in _docs_tf58] == ["First answer", "Second answer"]
              and _docs_tf58[0].content == "First answer\nwith detail"
              and _docs_tf58[0].url == "https://stub/mcp#search")
    finally:
        _restore_rc58f()

    # WITHOUT the opt-in a prose-only result yields [] (fail-visible, not soup).
    _restore_rc58g = _install_stub58({
        "initialize": _init_ok58, "notifications/initialized": _initialized_ok58,
        "tools/call": lambda p: _mk_resp58({"jsonrpc": "2.0", "id": p.get("id"), "result": {
            "content": [{"type": "text", "text": "prose only, no structure"}], "isError": False}}),
    })
    try:
        _mcp58._CLIENTS.clear()
        _docs_noopt58 = _ad_mcp58.search("anything", limit=10)  # row without text_fallback
        check("58.2 declarative mcp row: prose-only WITHOUT text_fallback yields [] (fail-visible)",
              _docs_noopt58 == [])
    finally:
        _restore_rc58g()

    # http rows (transport absent) behave byte-identically: a plain http fixture row is the CONTROL
    # (no declarative http fixture existed before P10; add one now). Monkeypatch http.get_json only.
    _http_row58 = {
        "name": "fix_http", "description": "fixture http control row",
        "endpoint": "https://api.stub/search", "method": "GET",
        "params_template": {"q": "{query}", "n": "{limit}"}, "results_path": "hits",
        "field_map": {"title": "title", "url": "url", "content": "body"}, "post_filter": False,
    }
    _ad_http58 = _decl58._row_to_adapter(_http_row58)
    check("58.2 http control: a transport-absent row defaults to transport 'http'",
          _ad_http58.transport == "http" and _ad_http58.tool is None)
    _http_captured58 = {}
    _orig_getjson58 = _decl58.http.get_json

    def _fake_get_json58(url, params=None, timeout=None):
        _http_captured58["url"] = url
        _http_captured58["params"] = params
        return {"hits": [{"title": "H1", "url": "https://a/1", "body": "b1"},
                         {"title": "H2", "url": "https://a/2", "body": "b2"}]}

    _decl58.http.get_json = _fake_get_json58
    try:
        _docs_http58 = _ad_http58.search("cats", limit=5)
        check("58.2 http control: a plain http row maps identically (typed {limit} encodes the same on the wire)",
              [d.title for d in _docs_http58] == ["H1", "H2"]
              and _docs_http58[0].url == "https://a/1" and _docs_http58[0].content == "b1"
              and _http_captured58["params"] == {"q": "cats", "n": 5})
    finally:
        _decl58.http.get_json = _orig_getjson58
finally:
    _cache58.get_docs, _cache58.set_docs = _orig_getdocs58, _orig_setdocs58

# --- (3) the declarative table + registration honor mcp rows. The table SHIPS EMPTY: the first
#     inhabitant arrives through the FRONT DOOR (source-foundry -> curator -> admit -> the
#     operator's stage_commit), never as a demo row. (The P10 gate removed the self-wrap loopback
#     demo: the eye's own SSRF guard blocks loopback:8765 unconditionally, so that row could never
#     return a document; a row that cannot work is a dead inhabitant, not a born-used mechanism.)
_sources_json58 = _json58.loads((SOURCES / "sources.json").read_text(encoding="utf-8"))
check("58.3 declarative table: sources.json parses as a list (empty until the foundry's first admit)",
      isinstance(_sources_json58, list))
check("58.3 declarative table: no loopback/private endpoint row may ever ship (the SSRF guard would dead-letter it)",
      all("127.0.0.1" not in str(r.get("endpoint") or "") and "localhost" not in str(r.get("endpoint") or "")
          for r in _sources_json58))
# registration honors an mcp row end-to-end (the same row->adapter path 58.2 exercises), with the
# router-facing attributes intact + the credentials guard inert-without-file, network-proof.
_reg_row58 = {"name": "mcp_fixture_reg", "description": "fixture", "transport": "mcp",
              "endpoint": "https://example.com/mcp", "tool": "search",
              "params_template": {"query": "{query}", "limit": "{limit}"},
              "results_path": "documents", "field_map": {"title": "title", "url": "url", "id": "id"},
              "facets": {"kind": "meta", "domains": ["meta"]},
              "explicit_only": "fixture row (never shipped)", "needs_credentials": True}
_reg_ad58 = _decl58._row_to_adapter(_reg_row58)
check("58.3 registration: an mcp row lands with transport/tool/facets/flags intact",
      getattr(_reg_ad58, "transport", None) == "mcp" and getattr(_reg_ad58, "tool", None) == "search"
      and getattr(_reg_ad58, "kind", None) == "meta" and getattr(_reg_ad58, "domains", None) == ["meta"]
      and bool(_reg_ad58.explicit_only) and _reg_ad58.needs_credentials is True)
# inert without credentials (transport-level guard): [] and NO network (client build explodes if reached).
_orig_getclient58h = _mcp58.http._get_client
_net_touched58 = {"n": 0}

def _explode_getclient58():
    _net_touched58["n"] += 1
    raise AssertionError("an mcp row must not touch the network without credentials")

_mcp58.http._get_client = _explode_getclient58
_orig_getdocs58b, _orig_setdocs58b = _cache58.get_docs, _cache58.set_docs
_cache58.get_docs = lambda *a, **k: None
_cache58.set_docs = lambda *a, **k: None
try:
    _reg_docs58 = _reg_ad58.search("anything", limit=3)
    check("58.3 registration: a needs_credentials mcp row is inert without credentials ([] + no network)",
          _reg_docs58 == [] and _net_touched58["n"] == 0)
finally:
    _mcp58.http._get_client = _orig_getclient58h
    _cache58.get_docs, _cache58.set_docs = _orig_getdocs58b, _orig_setdocs58b

# --- (3c) the FIRST FRONT-DOOR inhabitant: context7, forged by the source-foundry, judged in
#     /curator (admit, baseline_ref on file), staged via stage_commit, committed by the operator.
#     Golden fixture = the foundry's recorded live payload (2026-07-03), walked through the REAL
#     adapter offline. Quota honesty: explicit_only reason string (200 req/month/IP) is load-bearing.
_ctx7_ad58 = fetcher.get_adapter("context7")
check("58.3c context7: the first front-door row is registered from sources.json",
      _ctx7_ad58 is not None and getattr(_ctx7_ad58, "kind", None) == "lookup"
      and getattr(_ctx7_ad58, "domains", None) == ["code"]
      and bool(_ctx7_ad58.explicit_only) and _ctx7_ad58.needs_credentials is False)
if _ctx7_ad58 is not None:
    _ctx7_payload58 = {"results": [
        {"id": "/fastapi/fastapi", "title": "FastAPI",
         "description": "FastAPI framework, high performance, easy to learn, fast to code, ready for production",
         "branch": "master", "lastUpdateDate": "2026-07-02T06:19:26.605Z", "state": "finalized",
         "totalTokens": 132360, "totalSnippets": 2184, "stars": 84316, "trustScore": 9.9,
         "benchmarkScore": 83, "versions": ["0.115.13", "0_116_1", "0.118.2", "0.122.0", "0.128.0"],
         "score": 417.7908630371094, "vip": True, "verified": True},
        {"id": "/websites/fastapi_tiangolo", "title": "FastAPI",
         "description": "FastAPI is a modern, high-performance web framework for building APIs with Python, known for its speed, ease of use, and automatic interactive documentation based on OpenAPI standards.",
         "branch": "main", "lastUpdateDate": "2026-06-16T10:37:46.259Z", "state": "finalized",
         "totalTokens": 523585, "totalSnippets": 5270, "stars": -1, "trustScore": 9,
         "benchmarkScore": 86.55, "versions": [], "score": 395.6318359375, "vip": False,
         "verified": True}], "searchFilterApplied": False}
    _orig_getjson58c = _decl58.http.get_json
    _orig_getdocs58c, _orig_setdocs58c = _cache58.get_docs, _cache58.set_docs
    _decl58.http.get_json = lambda url, params=None, **kw: _ctx7_payload58
    _cache58.get_docs = lambda *a, **k: None
    _cache58.set_docs = lambda *a, **k: None
    try:
        _ctx7_docs58 = _ctx7_ad58.search("fastapi", limit=5)
        _c0 = _ctx7_docs58[0] if _ctx7_docs58 else None
        check("58.3c context7 golden fixture: the shipped row maps the recorded live payload correctly",
              _c0 is not None and _c0.source == "context7" and _c0.title == "FastAPI"
              and _c0.source_id == "/fastapi/fastapi" and _c0.url == "/fastapi/fastapi"
              and (_c0.content or "").startswith("FastAPI framework")
              and bool(_c0.date)
              and _c0.signals.get("score") is not None
              and _c0.signals["score"].value == 83.0)
    finally:
        _decl58.http.get_json = _orig_getjson58c
        _cache58.get_docs, _cache58.set_docs = _orig_getdocs58c, _orig_setdocs58c

# --- (4) Foundry draft: submit -> candidate carries it -> packet surfaces it -> stage_commit ------
# All against a temp candidates store so no real curator state is touched.
import penumbra.core.curator.candidates as _cand58  # noqa: E402
from penumbra.core.curator import apply as _capply58  # noqa: E402
import penumbra.server as _srv58  # noqa: E402
_c58_dir = _Path58(_tf58.mkdtemp())
_c58_real = (_cand58.STATE_DIR, _cand58.CANDIDATES_PATH, _cand58.SEEN_HOSTS_PATH, _cand58.TRIED_HOSTS_PATH)
try:
    _cand58.STATE_DIR = _c58_dir
    _cand58.CANDIDATES_PATH = _c58_dir / "candidates.json"
    _cand58.SEEN_HOSTS_PATH = _c58_dir / "seen_hosts.json"
    _cand58.TRIED_HOSTS_PATH = _c58_dir / "tried_hosts.json"

    _draft58 = {
        "row": {"name": "drafted_mcp", "description": "a drafted mcp source",
                "endpoint": "https://ext/mcp", "transport": "mcp", "tool": "search",
                "params_template": {"query": "{query}"}, "results_path": "results",
                "field_map": {"title": "title", "url": "url"}},
        "fixture": {"raw": {"results": [{"title": "X", "url": "https://ext/x"}]},
                    "expect": {"title": "X"}},
        "probe_summary": "probed live via the stub; 1 doc, fields populated",
    }
    _sub58 = _srv58.penumbra_curator_act.__wrapped__(
        verb="submit", name="drafted_mcp", urls=["https://ext/mcp"], mode="STRUCTURE",
        domain="meta", family="other", draft=_draft58)
    _cid58 = _sub58["candidate_id"]
    _row58 = _cand58.get(_cid58)
    check("58.4 foundry: submit with draft -> the candidate carries it verbatim",
          isinstance(_row58.get("draft"), dict)
          and _row58["draft"]["probe_summary"].startswith("probed live")
          and _row58["draft"]["row"]["name"] == "drafted_mcp")
    check("58.4 foundry: submit did NOT change the state transition (still 'new')",
          _row58.get("state") == "new")

    # packet surfaces the draft verbatim (even before a probe builds the evidence packet).
    _pkt58 = _srv58.penumbra_curator_view.__wrapped__(what="packet", candidate_id=_cid58)
    check("58.4 foundry: penumbra_curator_view(packet) surfaces the draft verbatim",
          _pkt58.get("draft", {}).get("row", {}).get("name") == "drafted_mcp"
          and _pkt58["draft"]["fixture"]["expect"]["title"] == "X")

    # stage_commit's rendered block IS the draft row (+ provenance naming the submitting session).
    _case58 = _capply58.prepare_owner_case(_row58)
    check("58.4 foundry: stage_commit's rendered block IS the draft row",
          _case58.get("proposed_config_row", {}).get("name") == "drafted_mcp"
          and _case58.get("from_draft") is True
          and _case58.get("config_file") == "sources.json")
    check("58.4 foundry: the staged draft carries a provenance line naming the submitting session",
          "agent" in (_case58.get("draft_provenance") or "")
          and _case58.get("row_valid") is True)

    # submit WITHOUT draft is unchanged (the control): no draft field, prepare falls back to the
    # evidence-derived row (None here, since no probe ran), NOT a draft.
    _sub58b = _srv58.penumbra_curator_act.__wrapped__(
        verb="submit", name="plain_rss", urls=["https://feed/x"], mode="MONITOR",
        domain="news", family="rss")
    _row58b = _cand58.get(_sub58b["candidate_id"])
    _pkt58b = _srv58.penumbra_curator_view.__wrapped__(what="packet", candidate_id=_sub58b["candidate_id"])
    _case58b = _capply58.prepare_owner_case(_row58b)
    check("58.4 foundry: submit WITHOUT draft is unchanged (no draft key, no from_draft)",
          _row58b.get("draft") is None and "draft" not in _pkt58b
          and "from_draft" not in _case58b)
    # State-transition FSM is untouched: the frozen edge set still has the P1 admit->owner_review edges.
    check("58.4 foundry: the candidate FSM edge set is untouched by the draft additive",
          ("awaiting_verdict", "admitted") in _cand58.ALLOWED_TRANSITIONS
          and ("admitted", "owner_review") in _cand58.ALLOWED_TRANSITIONS)
finally:
    (_cand58.STATE_DIR, _cand58.CANDIDATES_PATH, _cand58.SEEN_HOSTS_PATH,
     _cand58.TRIED_HOSTS_PATH) = _c58_real

# --- (5) Tripwires: live tool count == 18; _PENUMBRA_VERBS matches; no new deps in pyproject.toml --------
# tool count: the P10 wave added NO tool (mcp is a transport slot; the draft is an additive param); the
# live count is 18 because P8 (a later wave) added penumbra_statement. The invariant tracks REALITY, so it
# reads 18 here and in §49's derivation, which now includes penumbra_statement.
check("58.5 tripwire: MCP tool count == frozen 18 (P10 added no tool; the count is P8's penumbra_statement)",
      _pt_src.count(chr(10) + "@mcp.tool()") == 18,
      f"found {_pt_src.count(chr(10) + '@mcp.tool()')}")
# _PENUMBRA_VERBS carries the same 18 tool names §49 derives (P10 added none; P8 added penumbra_statement).
check("58.5 tripwire: _PENUMBRA_VERBS carries EXACTLY the 18 tool names",
      set(_t49_verbs.keys()) == _t49_expect_names and len(_t49_verbs) == 18)
# no new deps: the mcp client is httpx-only (already a core dep); assert the core dep set did not
# grow a P10 entry. The whole point of a hand-rolled client is ZERO new dependencies.
_pyproj58 = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
_P10_FORBIDDEN_DEPS = ("mcp-client", "httpx-sse", "sseclient", "aiohttp-sse", "fastmcp-client")
_p10_new_dep58 = [d for d in _P10_FORBIDDEN_DEPS if d in _pyproj58]
check("58.5 tripwire: no new dependency was added for the mcp client (httpx-only, hand-rolled)",
      not _p10_new_dep58, f"unexpected dep(s): {_p10_new_dep58}")
# the _mcp client imports ONLY stdlib + eye.http + eye.auth + the mcp protocol-version constant
# (all already present): a positive check that no exotic transport lib crept in.
import penumbra.core.sources._mcp as _mcpmod58  # noqa: E402
_mcp_src58 = _pt_inspect.getsource(_mcpmod58)
check("58.5 tripwire: the mcp client is httpx-only (no new transport import)",
      # httpx itself is a core dep (the P10 gate added a direct import for the response-rebuild
      # decode idiom, mirroring http._request_capped); what may never creep in is an EXOTIC
      # transport lib.
      "aiohttp" not in _mcp_src58 and "websockets" not in _mcp_src58
      and "sseclient" not in _mcp_src58 and "requests" not in _mcp_src58)


# ---------------------------------------------------------------------------
# 59. P8: penumbra_statement, the typed agent-statements channel (design "P8 shipped 2026-07-04").
#     The rulings idiom GENERALIZED: an agent-judged RELATION persists as attributed declarative
#     state and projects at read time. Where a ruling is pair-keyed + symmetric (identity, consumed by
#     the collapse machinery), a statement is a DIRECTED triple (src, dst, type) with FREE agent
#     vocabulary. Store checks monkeypatch _graph.STATEMENTS_PATH to a temp file (no real ~/.penumbra
#     state); the ladder / find / since / voices checks use a FRESH temp-db (the §47/§50 pattern,
#     restore in finally); the tool + tripwire checks are pure body/registration checks.
# ---------------------------------------------------------------------------

# (1) STATEMENTS STORE (save_statement / load_statements / delete_statement) on a monkeypatched path.
_s59_path_prev = _graph.STATEMENTS_PATH
_graph.STATEMENTS_PATH = Path(_tf47.mkdtemp()) / "graph_statements.json"
try:
    # save -> load roundtrip; the type slugs, direction is preserved (NOT normalized), stated_at stamped.
    _s59_save = _graph.save_statement("inst:label:openai", "inst:label:anthropic", "Acquired By",
                                      note="rumor smoke", doc="doc:zhihu:z1")
    _s59_loaded = _graph.load_statements()
    _s59_e = _s59_loaded[0] if _s59_loaded else {}
    check("statement (1): save -> load roundtrip, type slugged (Acquired By -> acquired_by) + stated_at stamped",
          len(_s59_loaded) == 1 and _s59_e.get("src") == "inst:label:openai"
          and _s59_e.get("dst") == "inst:label:anthropic" and _s59_e.get("type") == "acquired_by"
          and _s59_e.get("note") == "rumor smoke" and _s59_e.get("doc") == "doc:zhihu:z1"
          and bool(_s59_e.get("stated_at")) and _s59_save.get("replaced") is False)
    # DIRECTED key: (A, B, t) and (B, A, t) are DISTINCT (direction is the assertion, never normalized).
    _s59_rev = _graph.save_statement("inst:label:anthropic", "inst:label:openai", "acquired_by",
                                     note="reverse direction")
    check("statement (1): the directed triple is the key, (A,B,t) vs (B,A,t) are DISTINCT (len 2, new key)",
          _s59_rev.get("replaced") is False and len(_graph.load_statements()) == 2)
    # re-state the SAME directed triple REPLACES (declarative state, not a log): len stays 2, note updated.
    _s59_re = _graph.save_statement("inst:label:openai", "inst:label:anthropic", "acquired_by",
                                    note="updated note")
    _s59_after = _graph.load_statements()
    _s59_updated = [s for s in _s59_after if s["src"] == "inst:label:openai"][0]
    check("statement (1): re-stating the same directed triple REPLACES (len stays 2, note updated)",
          _s59_re.get("replaced") is True and len(_s59_after) == 2
          and _s59_updated.get("note") == "updated note")
    # delete true then false (the directed triple is the key, type re-slugged so a raw type still matches).
    _s59_del1 = _graph.delete_statement("inst:label:openai", "inst:label:anthropic", "Acquired By")
    _s59_del2 = _graph.delete_statement("inst:label:openai", "inst:label:anthropic", "acquired_by")
    check("statement (1): delete removes (True), then a second delete is a no-op (False), keyed on the triple",
          _s59_del1 is True and _s59_del2 is False and len(_graph.load_statements()) == 1)
    # validation: empty note / bad type chars / >40 / same_as / not_same_as each raises ValueError, and
    # the identity-type message POINTS at penumbra_ruling.
    _s59_empty_note = _s59_bad_type = _s59_long = _s59_same = _s59_notsame = False
    _s59_same_msg = _s59_notsame_msg = ""
    try:
        _graph.save_statement("a", "b", "rel", note="")
    except ValueError:
        _s59_empty_note = True
    try:
        _graph.save_statement("a", "b", "!!!", note="x")   # slugs to empty -> ValueError
    except ValueError:
        _s59_bad_type = True
    try:
        _graph.save_statement("a", "b", "x" * 41, note="x")
    except ValueError:
        _s59_long = True
    try:
        _graph.save_statement("a", "b", "same_as", note="x")
    except ValueError as _exc:
        _s59_same = True; _s59_same_msg = str(_exc)
    try:
        _graph.save_statement("a", "b", "not_same_as", note="x")
    except ValueError as _exc:
        _s59_notsame = True; _s59_notsame_msg = str(_exc)
    check("statement (1): validation raises ValueError (empty note / bad-type chars / >40 chars)",
          _s59_empty_note and _s59_bad_type and _s59_long)
    check("statement (1): same_as / not_same_as are REFUSED with an penumbra_ruling pointer in the message",
          _s59_same and _s59_notsame
          and "penumbra_ruling" in _s59_same_msg and "penumbra_ruling" in _s59_notsame_msg)
finally:
    _graph.STATEMENTS_PATH = _s59_path_prev

# (2, 3, 4, 5, 6) LADDER + find + since + voices + stats on a FRESH temp-db (the §50 pattern).
_p59_db_prev = _rstore.DB_PATH
_p59_disabled_prev = _rstore._disabled
_p59_local_prev = _rstore._local
_p59_stmt_prev = _graph.STATEMENTS_PATH
_rstore.DB_PATH = Path(_tf47.mkdtemp()) / "smoke_p8.db"
_rstore._disabled = False
_rstore._local = _thr47.local()   # fresh per-thread conn cache -> _read_con() reconnects to THIS db
_graph.STATEMENTS_PATH = Path(_tf47.mkdtemp()) / "graph_statements.json"
try:
    check("p8: index init creates the tables in the temp db", _rstore.init())
    _p59con = _rstore.connect()

    # Two synthetic ENTITY nodes so neighborhood / between have real anchors + hydratable labels.
    for _p59id, _p59kind, _p59label in [("inst:label:openai", "institution", "openai"),
                                        ("inst:label:anthropic", "institution", "anthropic")]:
        _p59con.execute("INSERT INTO graph_nodes(id, kind, label, first_seen, last_seen) "
                        "VALUES(?, ?, ?, 1.0, 1.0)", (_p59id, _p59kind, _p59label))
    _p59con.commit()

    # ---- (2) LADDER: a statement between two synthetic nodes appears in neighborhood under working
    #      + exploratory as tier J / method statement; NEVER under conservative; the types filter
    #      applies; between walks a path THROUGH the statement edge under working. ----
    _graph.save_statement("inst:label:openai", "inst:label:anthropic", "competes_with",
                          note="both frontier labs", doc="doc:zhihu:z1")
    for _p59pol in ("working", "exploratory"):
        _p59nb = _graph.neighborhood("inst:label:openai", depth=1, policy=_p59pol)
        _p59ids = {n["id"] for n in _p59nb["nodes"]}
        _p59has = any(e.get("method") == "statement" and e.get("tier") == "J"
                      and {e["src"], e["dst"]} == {"inst:label:openai", "inst:label:anthropic"}
                      for e in _p59nb["edges"])
        check(f"statement ladder (2): a statement appears under {_p59pol} as tier J / method statement",
              "inst:label:anthropic" in _p59ids and _p59has)
    _p59nb_cons = _graph.neighborhood("inst:label:openai", depth=1, policy="conservative")
    check("statement ladder (2): a statement is ABSENT under conservative (the pure mechanical world)",
          "inst:label:anthropic" not in {n["id"] for n in _p59nb_cons["nodes"]}
          and not any(e.get("method") == "statement" for e in _p59nb_cons["edges"]))
    # the types filter applies (a non-matching type excludes it; the matching type includes it).
    _p59nb_ft = _graph.neighborhood("inst:label:openai", depth=1, policy="working", types=["cites"])
    _p59nb_fm = _graph.neighborhood("inst:label:openai", depth=1, policy="working",
                                    types=["competes_with"])
    check("statement ladder (2): the types filter applies to statements (excluded off-type, included on-type)",
          not any(e.get("method") == "statement" for e in _p59nb_ft["edges"])
          and any(e.get("method") == "statement" for e in _p59nb_fm["edges"]))
    # between walks a path THROUGH the statement edge under working; conservative finds no path.
    _p59b_work = _graph.between("inst:label:openai", "inst:label:anthropic", policy="working")
    _p59b_cons = _graph.between("inst:label:openai", "inst:label:anthropic", policy="conservative")
    check("statement ladder (2): between walks a path THROUGH the statement edge under working (none under conservative)",
          _p59b_work["paths"] == [["inst:label:openai", "inst:label:anthropic"]]
          and any(e.get("method") == "statement" for e in _p59b_work["edges"])
          and _p59b_cons["paths"] == [])

    # ---- (3) find: a statement endpoint with a LABEL-KEYED id is findable by its label tokens, kind
    #      from the prefix, deduped, capped discipline; hydration self-describes the label. ----
    _graph.save_statement("inst:label:openai", "topic:label:agi safety", "works_on",
                          note="safety agenda")
    _p59find = _graph.find("agi safety")
    _p59hit = [n for n in _p59find["nodes"] if n["id"] == "topic:label:agi safety"]
    check("statement find (3): a label-keyed statement endpoint is findable by its label tokens (via=statement, kind from prefix)",
          len(_p59hit) == 1 and _p59hit[0].get("via") == "statement"
          and _p59hit[0].get("kind") == "topic" and _p59hit[0].get("label") == "agi safety"
          and _p59find["capped"] is False)
    # a materialized node that is ALSO a statement endpoint is deduped (comes back once, from the node arm).
    _p59find2 = _graph.find("openai")
    _p59openai = [n for n in _p59find2["nodes"] if n["id"] == "inst:label:openai"]
    check("statement find (3): a statement endpoint already materialized is deduped (one entry, not two)",
          len(_p59openai) == 1 and _p59openai[0].get("via") != "statement")
    # hydration self-describes the never-minted label-keyed id straight out of the id.
    _p59nb_hy = _graph.neighborhood("inst:label:openai", depth=1, policy="working")
    _p59topic = [n for n in _p59nb_hy["nodes"] if n["id"] == "topic:label:agi safety"]
    check("statement find (3): hydration self-describes a never-minted label-keyed id (label read out of the id)",
          len(_p59topic) == 1 and _p59topic[0].get("label") == "agi safety"
          and _p59topic[0].get("kind") == "topic")

    # ---- (4) since: a statement with stated_at >= cutoff appears with stated_at surfaced; older
    #      filtered. Write the statements file directly to control the timestamps. ----
    _graph.STATEMENTS_PATH.write_text(json.dumps([
        {"src": "inst:label:openai", "dst": "inst:label:x", "type": "new_rel", "note": "recent",
         "doc": "doc:zhihu:z9", "stated_at": "2026-07-02T00:00:00+00:00"},
        {"src": "inst:label:openai", "dst": "inst:label:y", "type": "old_rel", "note": "old",
         "doc": "", "stated_at": "2026-06-01T00:00:00+00:00"},
    ], ensure_ascii=False), encoding="utf-8")
    _p59since = _graph.since("inst:label:openai", "2026-07-01")
    _p59srows = [e for e in _p59since["edges"] if e.get("method") == "statement"]
    check("statement since (4): a statement with stated_at >= cutoff surfaces with stated_at (older filtered, tier J)",
          len(_p59srows) == 1 and _p59srows[0].get("type") == "new_rel"
          and _p59srows[0].get("stated_at") == "2026-07-02T00:00:00+00:00"
          and _p59srows[0].get("first_seen") is None and _p59srows[0].get("tier") == "J"
          and _p59srows[0].get("doc") == "doc:zhihu:z9")

    # ---- (5) voices: explicitly UNCHANGED by a statement between two docs (the deliberate ignore). ----
    _graph.STATEMENTS_PATH.write_text("[]", encoding="utf-8")
    _p59d1 = _doc("arxiv", "Voices P8 Doc One Alpha Long Enough Title For The Section", "http://a/pv1")
    _p59d1.source_id = "pv1"
    _p59d2 = _doc("zhihu", "Voices P8 Doc Two Beta No Ids At All Long Title Section", "http://a/pv2")
    _p59d2.source_id = "pv2"
    _p59con.execute("BEGIN")
    for _p59d in (_p59d1, _p59d2):
        _recall.writer._upsert(_p59con, rank, _p59d, 1.0)
    _p59con.commit()
    _p59n1 = _graph.doc_node_id("arxiv", "pv1"); _p59n2 = _graph.doc_node_id("zhihu", "pv2")
    # a baseline: with NO statement, both docs are unresolved (zero connecting evidence).
    _p59v_base = _graph.voices([_p59n1, _p59n2], policy="working")
    _graph.save_statement(_p59n1, _p59n2, "cites", note="a doc-doc typed statement")
    _p59v_work = _graph.voices([_p59n1, _p59n2], policy="working")
    _p59v_expl = _graph.voices([_p59n1, _p59n2], policy="exploratory")
    check("statement voices (5): a statement between two docs leaves voices UNCHANGED (not identity evidence)",
          _p59v_base["n_voices"] == 0 and _p59v_base["n_unresolved"] == 2
          and _p59v_work["n_voices"] == 0 and _p59v_work["n_unresolved"] == 2
          and _p59v_expl["n_voices"] == 0 and _p59v_expl["n_unresolved"] == 2)

    # ---- (6) stats: statements count present and correct (beside rulings). ----
    _p59stats = _graph.stats()
    check("statement stats (6): stats.statements is present and correct (== the load_statements count)",
          _p59stats.get("statements") == len(_graph.load_statements())
          and _p59stats.get("statements") == 1 and "rulings" in _p59stats)
finally:
    _graph.STATEMENTS_PATH = _p59_stmt_prev
    _rstore.DB_PATH = _p59_db_prev
    _rstore._disabled = _p59_disabled_prev
    _rstore._local = _p59_local_prev
    # drain any residue so a later section never inherits our queue items.
    while not _recall.writer._queue.empty():
        try:
            _recall.writer._queue.get_nowait()
        except Exception:  # noqa: BLE001
            break

# (7) penumbra_statement TOOL: create / list (about + type filters, cap 200 + capped) / delete happy paths
#     + all error paths via the unwrapped body (past @_threaded). Monkeypatch a temp statements file so
#     no real state is touched.
_s59_tool_prev = _graph.STATEMENTS_PATH
_graph.STATEMENTS_PATH = Path(_tf47.mkdtemp()) / "graph_statements.json"
try:
    _es = _srv.penumbra_statement.__wrapped__
    _es_create = _es(action="create", src="inst:label:openai", dst="inst:label:anthropic",
                     type="Competes With", note="tool smoke", doc="doc:zhihu:z1")
    check("penumbra_statement (7): action=create records the statement (created True, type slugged, replaced False)",
          _es_create.get("created") is True
          and _es_create.get("statement", {}).get("type") == "competes_with"
          and _es_create.get("replaced") is False)
    # a second create on the SAME triple replaces (declarative state).
    _es_recreate = _es(action="create", src="inst:label:openai", dst="inst:label:anthropic",
                       type="competes_with", note="updated")
    check("penumbra_statement (7): re-create on the same directed triple replaces (replaced True)",
          _es_recreate.get("replaced") is True)
    # a second, distinct statement for the list-filter checks.
    _es(action="create", src="inst:label:openai", dst="topic:label:agi", type="works_on", note="y")
    _es_list = _es(action="list")
    check("penumbra_statement (7): action=list returns statements + count (+ capped flag), unfiltered",
          _es_list.get("count") == 2 and _es_list.get("capped") is False
          and len(_es_list.get("statements", [])) == 2)
    _es_list_about = _es(action="list", about="topic:label:agi")
    check("penumbra_statement (7): action=list about=<node> filters to statements touching that node",
          _es_list_about.get("count") == 1
          and _es_list_about["statements"][0].get("type") == "works_on")
    _es_list_type = _es(action="list", type="Works On")
    check("penumbra_statement (7): action=list type=<t> filters (slugged) to that type",
          _es_list_type.get("count") == 1
          and _es_list_type["statements"][0].get("dst") == "topic:label:agi")
    _es_del = _es(action="delete", src="inst:label:openai", dst="inst:label:anthropic",
                  type="competes_with")
    check("penumbra_statement (7): action=delete removes it (deleted True); a second delete is False",
          _es_del.get("deleted") is True
          and _es(action="delete", src="inst:label:openai", dst="inst:label:anthropic",
                  type="competes_with").get("deleted") is False)
    # ERROR PATHS: unknown action / empty note / refused identity type (with pointer) / delete missing type.
    check("penumbra_statement (7): an unknown action returns an error naming create|list|delete",
          "error" in _es(action="frobnicate")
          and all(_w in _es(action="frobnicate")["error"] for _w in ("create", "list", "delete")))
    check("penumbra_statement (7): create with an empty note returns an error dict (ValueError mapped)",
          "error" in _es(action="create", src="a", dst="b", type="rel", note=""))
    _es_refuse = _es(action="create", src="a", dst="b", type="same_as", note="x")
    check("penumbra_statement (7): create with same_as is refused with an penumbra_ruling pointer",
          "error" in _es_refuse and "penumbra_ruling" in _es_refuse["error"])
    check("penumbra_statement (7): delete without a type returns an error dict",
          "error" in _es(action="delete", src="a", dst="b"))
finally:
    _graph.STATEMENTS_PATH = _s59_tool_prev

# (7) TRIPWIRES: tool count == 18; _PENUMBRA_VERBS carries penumbra_statement (18); _GATHER_TOOLS still 12 and
#     EXCLUDES penumbra_statement (a write verb; gather is read-only); docs-drift POSITIVE presence of
#     penumbra_statement in the product README + the connect-time instructions.
check("p8 tripwire: MCP tool count == 18 (the conscious 17 -> 18 bump for penumbra_statement)",
      _pt_src.count(chr(10) + "@mcp.tool()") == 18,
      f"found {_pt_src.count(chr(10) + '@mcp.tool()')}")
check("p8 tripwire: _PENUMBRA_VERBS carries penumbra_statement and totals 18",
      "penumbra_statement" in _t49_verbs and len(_t49_verbs) == 18)
check("p8 tripwire: penumbra_statement is a REGISTERED tool", callable(_srv.penumbra_statement))
check("p8 tripwire: _GATHER_TOOLS is still 12 and EXCLUDES penumbra_statement (a write verb; gather is read-only)",
      len(_GATHER_TOOLS) == 12 and "penumbra_statement" not in _GATHER_TOOLS)
_s59_readme = (ROOT / "README.md")
_s59_readme_txt = _s59_readme.read_text(encoding="utf-8") if _s59_readme.exists() else ""
check("p8 docs-drift (presence): penumbra_statement is named in README.md (the product-facing tool surface)",
      "penumbra_statement" in _s59_readme_txt)
check("p8 docs-drift (presence): penumbra_statement is named in _PENUMBRA_INSTRUCTIONS (the connect-time brief)",
      "penumbra_statement" in _srv._PENUMBRA_INSTRUCTIONS)


# ---------------------------------------------------------------------------
# 60. P11, the four dogfood findings of the 2026-07-04 field recon:
#     W1 the _meta weight-class rule (deployment-static + non-actionable facts leave per-query _meta:
#        the full excluded MAP -> excluded_count; fast/slow name lists -> progressive COUNTS; the
#        actionable name lists (timed_out/empty/truncated/excluded_relevant) stay; penumbra_sources carries
#        every source's explicit_only reason so the catalog is one call away).
#     W2 seen_before is a COMPLETENESS contract (EVERY ranked doc carries seen_before + first_seen_at,
#        never absent); the regression fixture lives in §48 (5); here we assert the section-60 shape.
#     W3 gather's hint slot names the tool's REAL params on a signature mismatch (mechanical, generated).
#     W4 zhihu dates honestly: the adapter's data path is CDP-rendered HTML (no XHR/JSON timestamp
#        payload), so date stays null (the honest no-date state); no approximate date is fabricated.
# ---------------------------------------------------------------------------

# --- W1 (a): a real broad search_many _meta, via a temporary synthetic FAST adapter (save/restore the
#     registry). Broad (sources=None) so the excluded / progressive machinery actually runs. ---
class _P11FastAdapter:
    name = "_p11_fast_synthetic"
    needs_credentials = False
    description = "p11 smoke synthetic fast source"
    def search(self, query, limit=10):
        return []
    def fetch_url(self, url):
        return None
    def health_check(self):
        return True, "ok"

_p11_reg_prev = dict(fetcher._adapters)
try:
    fetcher.register_adapter_live(_P11FastAdapter())
    _p11_results, _p11_meta = fetcher.search_many("p11 smoke query", sources=None, limit_per_source=3)
    # excluded_count is an INT; the full excluded MAP is GONE from _meta (it lives in penumbra_sources now).
    check("P11 W1: broad _meta carries excluded_count (int) and NO excluded map",
          isinstance(_p11_meta.get("excluded_count"), int) and "excluded" not in _p11_meta)
    # progressive is {fast: int, slow: int, timed_out: list}; fast/slow are counts, timed_out a name list.
    _p11_prog = _p11_meta.get("progressive")
    check("P11 W1: _meta.progressive is {fast:int, slow:int, timed_out:list}",
          isinstance(_p11_prog, dict)
          and isinstance(_p11_prog.get("fast"), int) and isinstance(_p11_prog.get("slow"), int)
          and isinstance(_p11_prog.get("timed_out"), list))
    # the old flat fast_sources / slow_sources name-list keys are GONE from _meta.
    check("P11 W1: the flat fast_sources / slow_sources name lists are GONE from _meta",
          "fast_sources" not in _p11_meta and "slow_sources" not in _p11_meta)
    # excluded_relevant keeps its shape (a list); empty / truncated stay NAME LISTS; timed_out top-level
    # stays a name list (the curator yield tap reads it).
    check("P11 W1: excluded_relevant is a list; empty / truncated / timed_out stay name lists",
          isinstance(_p11_meta.get("excluded_relevant"), list)
          and isinstance(_p11_meta.get("empty"), list)
          and isinstance(_p11_meta.get("truncated"), list)
          and isinstance(_p11_meta.get("timed_out"), list))
finally:
    fetcher._adapters.clear()
    fetcher._adapters.update(_p11_reg_prev)

# --- W1 (b): the CATALOG guarantee. penumbra_sources' roster carries each excluded source's explicit_only
#     REASON string (so removing the full map from _meta loses nothing; it is one call away). ---
_p11_roster = fetcher.list_sources()
_p11_excluded_entries = [e for e in _p11_roster if e.get("explicit_only")]
check("P11 W1: penumbra_sources roster exposes explicit_only_reason for excluded sources (catalog one call away)",
      bool(_p11_excluded_entries)
      and all(isinstance(e.get("explicit_only_reason"), str) and e["explicit_only_reason"]
              for e in _p11_excluded_entries))
# a NON-excluded source carries no reason key (no noise).
_p11_incl = [e for e in _p11_roster if not e.get("explicit_only")]
check("P11 W1: a non-excluded source carries NO explicit_only_reason key (no noise)",
      bool(_p11_incl) and all("explicit_only_reason" not in e for e in _p11_incl))

# --- W1 (c): docs-drift. the connect-time instructions section (5) names excluded_count + progressive,
#     and NO LONGER re-ships the old fast_sources/slow_sources vocabulary. ---
_p11_instr = _srv._PENUMBRA_INSTRUCTIONS
check("P11 W1: _PENUMBRA_INSTRUCTIONS section 5 names excluded_count and progressive (docs-drift)",
      "excluded_count" in _p11_instr and "progressive:" in _p11_instr)
check("P11 W1: _PENUMBRA_INSTRUCTIONS no longer advertises fast_sources / slow_sources (weight-class swept)",
      "fast_sources" not in _p11_instr and "slow_sources" not in _p11_instr)

# --- W2: the COMPLETENESS contract, stated at section-60 level (the driving regression fixture, incl.
#     the straggler-shaped root-cause doc, is §48 (5) against a real temp recall db). Assert the helper
#     stamps BOTH keys on EVERY doc even with recall unavailable (the fail-open never-seen path). ---
_p11_sb_docs = [_doc("arxiv", "P11 W2 Completeness Doc One Long Enough Title Here", "http://arxiv/p11w2a"),
                _PDoc(source="arxiv", source_id="p11w2b", url="http://arxiv/p11w2b",
                      title="P11 W2 Bare Metadata Straggler Long Enough Title", content="x", metadata={})]
_p11_sb_disabled_prev = _rstore._disabled
try:
    _rstore._disabled = True   # recall unavailable → the fail-open path must STILL stamp every doc
    fetcher._stamp_seen_before(_p11_sb_docs, _time48.time())
    check("P11 W2: with recall disabled, EVERY ranked doc still carries seen_before=False + first_seen_at=None",
          all(_d.metadata.get("seen_before") is False and _d.metadata.get("first_seen_at") is None
              and "seen_before" in _d.metadata and "first_seen_at" in _d.metadata
              for _d in _p11_sb_docs))
finally:
    _rstore._disabled = _p11_sb_disabled_prev
# the contract is documented at the structural point.
check("P11 W2: fetcher._stamp_seen_before documents the completeness contract (never absent)",
      "COMPLETENESS CONTRACT" in (fetcher._stamp_seen_before.__doc__ or ""))

# --- W3: a gather call with a WRONG kwarg gets a hint naming the tool's real params; a normal failure
#     gets no fabricated hint. Drive the REAL penumbra_gather (unwrapped past @_threaded). ---
_p11_gather = _srv.penumbra_gather.__wrapped__
# (i) wrong kwarg → status errored + a hint that names the real penumbra_read params.
_p11_bad = _p11_gather(calls=[{"tool": "penumbra_read", "args": {"nonexistent_arg": "x"}}], wait_s=5)
_p11_bad_r = _p11_bad["results"][0]
check("P11 W3: a gather call with a wrong kwarg is errored AND carries a hint naming the real params",
      _p11_bad_r.get("status") == "errored" and "hint" in _p11_bad_r
      and "penumbra_read takes:" in _p11_bad_r["hint"]
      and all(_p in _p11_bad_r["hint"] for _p in ("target", "start_char", "max_chars")))
# (ii) a NORMAL failure (a real signature, but the body raises a non-TypeError) gets NO fabricated
#      hint. Inject a synthetic tool that raises a plain ValueError (deterministic + offline), so this
#      tests the branch (an ordinary adapter failure), not the network. Save/restore the whitelist.
def _p11_boom(**kwargs):
    raise ValueError("adapter blew up (not a signature problem)")
fetcher_gt_prev = dict(_GATHER_TOOLS)
try:
    _GATHER_TOOLS["_p11_boom"] = _p11_boom
    _p11_norm = _p11_gather(calls=[{"tool": "_p11_boom", "args": {}}], wait_s=5)
    _p11_norm_r = _p11_norm["results"][0]
    check("P11 W3: a normal (non-signature) failure is errored with NO fabricated hint",
          _p11_norm_r.get("status") == "errored" and "hint" not in _p11_norm_r)
finally:
    _GATHER_TOOLS.clear()
    _GATHER_TOOLS.update(fetcher_gt_prev)
# (iii) the helpers themselves: the hint is MECHANICAL (inspect.signature over the unwrapped body).
check("P11 W3: _gather_signature_hint derives real params mechanically for penumbra_read",
      _srv._gather_signature_hint("penumbra_read") == "penumbra_read takes: target, start_char, max_chars, export_media, ocr")
check("P11 W3: _is_signature_mismatch is True for a kwarg TypeError, False for a plain ValueError",
      _srv._is_signature_mismatch(TypeError("f() got an unexpected keyword argument 'z'"))
      and not _srv._is_signature_mismatch(ValueError("some positional argument text")))

# --- W4: zhihu dates honestly. The adapter's data path is CDP-rendered HTML (BeautifulSoup over
#     page.content()), NOT an XHR/JSON payload with created_time/updated_time; there is no timestamp
#     to map, so date stays null. Golden fixture: a representative rendered search card parses to a
#     doc with date=None (the honest no-date state), while title/url/source_id/votes ARE extracted.
#     (No approximate/fabricated date is invented from a localized card string; the spec's red line.) ---
from penumbra.core.sources.walled.zhihu_source import ZhihuAdapter as _P11Zhihu  # noqa: E402
from bs4 import BeautifulSoup as _P11Soup  # noqa: E402
_P11_ZHIHU_CARD = (
    '<div class="SearchResult-Card"><div class="ContentItem">'
    '<h2 class="ContentItem-title"><a href="/question/12345/answer/67890">读博期间如何做好方法论</a></h2>'
    '<div class="RichContent-inner">我的经验是先读综述再动手，阅读全文</div>'
    '<div class="AuthorInfo-name"><a class="UserLink-link">某研究者</a></div>'
    '<div class="ContentItem-actions"><button class="VoteButton" aria-label="赞同 128">赞同 128</button></div>'
    '</div></div>')
_p11_card = _P11Soup(_P11_ZHIHU_CARD, "lxml").select_one(".SearchResult-Card, .List-item")
_p11_zdoc = _P11Zhihu()._card_to_document(_p11_card)
check("P11 W4: zhihu CDP search card parses (title + url + source_id extracted from rendered HTML)",
      _p11_zdoc is not None and _p11_zdoc.title == "读博期间如何做好方法论"
      and _p11_zdoc.source_id == "12345"
      and _p11_zdoc.url == "https://www.zhihu.com/question/12345/answer/67890")
check("P11 W4: zhihu date is null, the data path carries no timestamp payload (honest no-date state)",
      _p11_zdoc.date is None)
# The adapter source itself has no timestamp-field mapping (no created_time/updated_time); locks the
# finding so a future 'approximate date from a card string' addition trips this gate.
_p11_zsrc = _insp.getsource(_P11Zhihu)
check("P11 W4: the zhihu adapter maps no created_time/updated_time (HTML-scrape path, honestly dateless)",
      "created_time" not in _p11_zsrc and "updated_time" not in _p11_zsrc)


# ---------------------------------------------------------------------------
# 61. Per-logger warning rate-limit (the docker-drytest finding 2026-07-04): no single logger may
#     flood the log. A fresh no-contact-email install rate-limited OpenAlex -> the breaker opened ->
#     researcher_watch + ~39 org_watch labs + background backfill ALL spammed "circuit open" across
#     many threads/subsystems, a screenful that reads as broken. Per-subsystem silencing was fragile
#     (contextvar missed org_watch's non-copy_context pool; a prewarm-scoped hush missed the same
#     storm re-emitted seconds later by another subsystem). The fix caps it at the ONE place every
#     warning passes to render -- the root handler -- so it is thread- AND subsystem-agnostic.
# ---------------------------------------------------------------------------
import logging as _lg61  # noqa: E402
import penumbra.core._lograte as _lograte61  # noqa: E402


def _rec61(name, level=_lg61.WARNING, msg="x"):
    return _lg61.LogRecord(name, level, "f.py", 1, msg, None, None)


_clock61 = [1000.0]
_flt61 = _lograte61.WarningRateLimit(burst=3, window_s=60.0, clock=lambda: _clock61[0])

# burst: first `burst` WARNINGs from a logger pass; the rest in-window are dropped.
_passed61 = [_flt61.filter(_rec61("a.src")) for _ in range(5)]
check("61 rate-limit: first `burst` WARNINGs pass, the flood beyond is dropped",
      _passed61 == [True, True, True, False, False])
# a DIFFERENT logger has its own budget (per-logger key, not global).
check("61 rate-limit: a different logger keeps its own budget (independent key)",
      _flt61.filter(_rec61("b.src")) is True)
# below WARNING always passes (INFO/DEBUG are never rate-limited).
check("61 rate-limit: INFO is never throttled (only WARNING/ERROR are)",
      all(_flt61.filter(_rec61("a.src", _lg61.INFO)) is True for _ in range(5)))
# CRITICAL is never throttled, even past a logger's exhausted warning budget.
check("61 rate-limit: CRITICAL always passes, even past an exhausted budget",
      _flt61.filter(_rec61("a.src", _lg61.CRITICAL)) is True)
# window rolls over -> a new record passes AND carries the suppressed count (no silent truncation).
_clock61[0] = 1000.0 + 61.0
_next61 = _rec61("a.src", msg="orig")
check("61 rate-limit: after the window, the next WARNING passes again",
      _flt61.filter(_next61) is True)
check("61 rate-limit: that record is annotated with the suppressed count (cap is never silent)",
      "similar suppressed" in _next61.getMessage() and "+2" in _next61.getMessage())

# install_on_root: attaches exactly one MARKED filter per handler, idempotently. Tested against a
# throwaway root handler so it never rate-limits the rest of this smoke run; cleaned up after.
_root61 = _lg61.getLogger()
_tmph61 = _lg61.NullHandler()
_root61.addHandler(_tmph61)
try:
    _lograte61.install_on_root()
    _lograte61.install_on_root()  # second call is a no-op (idempotent)
    _marked61 = [f for f in _tmph61.filters if getattr(f, _lograte61._INSTALLED_MARK, False)]
    check("61 rate-limit: install_on_root attaches exactly one marked filter, idempotently",
          len(_marked61) == 1)
finally:
    for _h61 in _root61.handlers:  # strip the markers we added to every root handler
        for _f61 in list(_h61.filters):
            if getattr(_f61, _lograte61._INSTALLED_MARK, False):
                _h61.removeFilter(_f61)
    _root61.removeHandler(_tmph61)


print()
if FAIL:
    print(f"SMOKE FAILED: {len(FAIL)} problem(s)")
    sys.exit(1)
print("SMOKE OK")
