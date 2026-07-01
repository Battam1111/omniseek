#!/usr/bin/env python3
"""Offline smoke gate. Run by deploy.sh on the host BEFORE the service restarts;
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

# Read-only invariant: Penumbra RETRIEVES, it never mutates a remote source. Every adapter must
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
      _dr_escapes("~/.penumbra/credentials/http.json") and _dr_escapes("/etc/passwd")
      and _dr_escapes("penumbra-inbox/../.penumbra/credentials/http.json"),
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
check("server exposes penumbra_read_document",
      hasattr(_srv, "penumbra_read_document") and callable(_srv.penumbra_read_document))

# --- P39 tier-4: in-band image view (penumbra_view_doc_images) ---
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
      and "penumbra_read_document" in (docreader.view_images("notes.txt").get("note") or ""))
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
check("server exposes penumbra_view_doc_images",
      hasattr(_srv, "penumbra_view_doc_images") and callable(_srv.penumbra_view_doc_images))
# --- P41 OCR tier (penumbra_read_document ocr=True) ---
check("docreader.ocr_image exists + read_document accepts ocr (OCR tier)",
      hasattr(docreader, "ocr_image") and callable(docreader.ocr_image)
      and "ocr" in _insp.signature(docreader.read_document).parameters)
# --- P42 in-band image-URL view + search-index junk-snippet filter ---
check("docreader.view_image_urls + server penumbra_view_images (in-band URL image delivery)",
      hasattr(docreader, "view_image_urls") and callable(docreader.view_image_urls)
      and hasattr(_srv, "penumbra_view_images") and callable(_srv.penumbra_view_images))
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

# graceful degrade: a fresh/empty (or unusable) index returns [] — Penumbra stays stateless
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
#     loaded — the embedding QUALITY is bake-off-verified on the host; these are the code invariants.)
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
# independent source is its own backend. penumbra_list_sources surfaces backend_count + backend_breakdown.
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
#     no-verdict-in-code walk (the corrected razor): Penumbra fetches/probes/measures/
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
        _srv2.penumbra_curator_decide.__wrapped__(**kw)
        return False
    except Exception:  # noqa: BLE001
        return True


check("curator: decide(admit, baseline_ref={}) RAISES (empty baseline)",
      _decide_raises(candidate_id=_dcid, decision="admit", reasons="x", baseline_ref={}))
_ok_decide = _srv2.penumbra_curator_decide.__wrapped__(candidate_id=_dcid, decision="admit", reasons="x",
                                                  baseline_ref={"web": ["result"]})
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
      and _srv2.penumbra_curator_decide.__wrapped__(candidate_id=_hc, decision="reject", reasons="ToS").get("state") == "rejected")
_ic = _ccand.add({"name": "Incomplete", "urls": ["https://i.example.com/f"], "proposed_mode": "RECALL",
                  "proposed_domain": "papers", "proposed_family": "rss"})
_ccand.store_evidence(_ic, {"evidence_complete": False, "stage0_safety": {"hard_redline_blocked": False}},
                      {"hard_redline_ids": []}, "awaiting_verdict", "ev")
check("curator: evidence_complete=False -> decide(admit) RAISES, decide(reject) succeeds",
      _decide_raises(candidate_id=_ic, decision="admit", reasons="x", baseline_ref={"a": 1})
      and _srv2.penumbra_curator_decide.__wrapped__(candidate_id=_ic, decision="reject", reasons="thin").get("state") == "rejected")

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

# (14) Server exposes the 5 penumbra_curator_* tools.
for _t in ("penumbra_curator_submit", "penumbra_curator_probe", "penumbra_curator_packet",
           "penumbra_curator_decide", "penumbra_curator_list"):
    check(f"curator: server exposes {_t}", hasattr(_srv2, _t) and callable(getattr(_srv2, _t)))

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
# arXiv-id in the URL, NOT the title) → live_sources len 2, index_only False, merge_basis "id".
_g1 = _doc("arxiv", "ResNet", "http://arxiv.org/abs/1512.03385")
_g2 = _doc("openalex", "ResNet", "http://arxiv.org/pdf/1512.03385")
check("yield_tap: §9 fixture merges on arXiv-id (not title)", rank.fingerprint(_g1).startswith("arxiv:"))
_dd2 = rank.dedup([_g1, _g2])
check("yield_tap: rank.dedup stamps live_sources/index_only/merge_basis on a 2-source id-grade group",
      len(_dd2) == 1
      and set(_dd2[0].metadata.get("live_sources")) == {"arxiv", "openalex"}
      and _dd2[0].metadata.get("index_only") is False
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
check("yield_tap: rank.dedup stamps a SINGLETON survivor too (live_sources=[src], index_only False)",
      _dds[0].metadata.get("live_sources") == ["arxiv"]
      and _dds[0].metadata.get("index_only") is False
      and _dds[0].metadata.get("merge_basis") == "title")
# an INDEX-ONLY group (every member from recall) → index_only True, live_sources empty.
_ix1 = _doc("mycareersfuture", "Senior ML Engineer Opening At A Long Titled Company", "https://e.com/ix1")
_ix1.metadata = {"from_index": True}
_ix2 = _doc("overseas_ai_jobs", "Senior ML Engineer Opening At A Long Titled Company", "https://e.com/ix2")
_ix2.metadata = {"from_index": True}
_ddi = rank.dedup([_ix1, _ix2])
check("yield_tap: rank.dedup marks an index-only group (index_only True, live_sources empty)",
      len(_ddi) == 1 and _ddi[0].metadata.get("index_only") is True
      and _ddi[0].metadata.get("live_sources") == [])

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
    # the policy file ships + freezes its expected key shape (operator DATA, widening is an operator edit).
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
    # server exposes the two P3 tools.
    for _t in ("penumbra_curator_audit", "penumbra_curator_source_verdict"):
        check(f"curator P3: server exposes {_t}", hasattr(_srv2, _t) and callable(getattr(_srv2, _t)))
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
_LOOP_PATH = ROOT / "scripts" / "curator.py"  # ROOT == repo root (smoke ROOT = parents[1])

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
check("curator P4: curator.py source readable", bool(_loop_src), str(_LOOP_PATH))
_disc_code = _strip_py_noise(_disc_src)
_loop_code = _strip_py_noise(_loop_src)
# verdict-writer CALL forms (a bare prose mention without "(" is now also gone with the strings).
_VERDICT_WRITER_CALLS = ("penumbra_curator_decide(", ".record_verdict(", "record_source_verdict(",
                         ".record_applied(")
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
# the sentinel source actually diff-gates (greps for newly_empty/newly_single + the diff op).
_sent_src = (ROOT / "scripts" / "source_audit.py")
_sent_src = _sent_src if _sent_src.exists() else (ROOT.parent.parent / "scripts" / "source_audit.py")
_sent_txt = _sent_src.read_text(encoding="utf-8") if _sent_src.exists() else ""
check("curator P4 (§10): sentinel computes newly_empty/newly_single via a set-diff against the baseline",
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
_ow_ok = _srv2.penumbra_curator_decide.__wrapped__(candidate_id=_ow_cid, decision="admit", reasons="x",
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
    "penumbra_curator_apply_live": _insp.getsource(_srv2.penumbra_curator_apply_live.__wrapped__),
    "penumbra_curator_rollback_live": _insp.getsource(_srv2.penumbra_curator_rollback_live.__wrapped__),
    "penumbra_curator_retire_live": _insp.getsource(_srv2.penumbra_curator_retire_live.__wrapped__),
    "penumbra_curator_stage_commit": _insp.getsource(_srv2.penumbra_curator_stage_commit.__wrapped__),
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
check("curator live: penumbra_curator_apply_live RAISES on a candidate not in owner_review",
      _al_raises(lambda: _srv2.penumbra_curator_apply_live.__wrapped__(candidate_id=_nt)))
# defensive: a doctored org_watch in owner_review returns applied:false (never live-applied)
_ow_live = _ccand.add({"name": "OwLive", "urls": ["https://api.openalex.org/works"], "proposed_mode": "STRUCTURE",
                       "proposed_domain": "papers", "proposed_family": "org_watch"})
_ccand.store_evidence(_ow_live, {"evidence_complete": True, "stage0_safety": {"hard_redline_blocked": False}},
                      {"hard_redline_ids": []}, "awaiting_verdict", "ev")
_ccand.record_verdict(_ow_live, {"decision": "admit"}, "admitted", "a")
_ccand.set_state(_ow_live, "owner_review", "stage", by="agent")
_ow_res = _srv2.penumbra_curator_apply_live.__wrapped__(candidate_id=_ow_live)
check("curator live: penumbra_curator_apply_live refuses a _NEVER_AUTO family (applied:false, points to git path)",
      _ow_res.get("applied") is False and "stage_commit" in (_ow_res.get("must_use") or ""))
# retire requires an existing agent prune verdict
_sa.STATE_DIR = _altmp
_sa.SOURCE_VERDICTS_PATH = _altmp / "live_source_verdicts.json"
check("curator live: penumbra_curator_retire_live RAISES without an existing agent PRUNE verdict",
      _al_raises(lambda: _srv2.penumbra_curator_retire_live.__wrapped__(name="no_such_pruned_src", confirm=True)))

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

# (16) server exposes the live-apply lane tools.
for _t in ("penumbra_curator_apply_live", "penumbra_curator_rollback_live", "penumbra_curator_stage_commit",
           "penumbra_curator_retire_live", "penumbra_curator_rollback_retire"):
    check(f"curator live: server exposes {_t}", hasattr(_srv2, _t) and callable(getattr(_srv2, _t)))

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
# per-bucket remaining is captured, exposed via penumbra_health_check, so a heavy OpenAlex day is
# ITEMIZABLE (a hidden over-consumer can't hide) instead of inferred.
check("openalex: usage_stats() exists (per-caller budget attribution surfaced by penumbra_health_check)",
      hasattr(_oa2, "usage_stats") and callable(_oa2.usage_stats))


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
        # S2 edge carries the RAW citing sentence (+ its own intents). Penumbra passes it through
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
    # ({snippet, intents}); Penumbra does NOT classify supporting/contrasting/mentioning, the
    # agent reads the snippet and judges. This asserts the pass-through only.
    _ctx_nodes = [n for n in _built["nodes"] if n.get("contexts")]
    check("cartographer s2: citing-sentence contexts surface on a node (polarity evidence)",
          bool(_ctx_nodes), f"nodes_with_contexts={len(_ctx_nodes)}")
    check("cartographer s2: a context entry carries the raw snippet + S2 intents (no Penumbra verdict)",
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
# Regression guards from Penumbra-usage-waste-rootfix workflow: a shared rate pacer + single-flight
# health() + refresh-if-near-expiry warming + relations caching, mirroring the _openalex fix.
# ============================================================================
from penumbra.core import cache as _wcache  # noqa: E402
_WASTE_ORIG_CACHE_DIR = _wcache.CACHE_DIR

import time as _t
from penumbra.core import _s2 as _S2
from penumbra.core.sources.api import semantic_scholar_source as _SS

check("s2 has _pace", hasattr(_S2, "_pace"))
check("s2 _MIN_INTERVAL_S honors ~1 RPS", _S2._MIN_INTERVAL_S == 1.0)
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

# S2 retry (2026-06-20): the lib's OWN 10x/250s tenacity backoff is OFF (retry=False); Penumbra owns a
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
    _stashed = getattr(_exc, "_diagnostic", None)
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
#     account-live and verified on the host, not here.
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
#      account-live and verified on the host, not here.
# ---------------------------------------------------------------------------
check("xhs_cn: browser path enabled (_BROWSER_OK) + helpers wired",
      _xcn._BROWSER_OK and all(hasattr(_xcn, n) for n in
          ("_browser_alive", "_browser_search", "_browser_fetch", "_note_browser_cdp", "_cn_captcha",
           "_cn_login_wall", "_cn_card_to_document", "_cn_cards_from_html")))
check("xhs_cn: fetch_timeout >= the 110s browser cdp_call budget (penumbra_add_url backstop must not kill it)",
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
#     (2026-06-20, push2 dropped the host under multi-agent burst). The GBK `~`-parse + symbol map +
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
# the role doc builds with a location-aware title + a median signal (Penumbra's structured comp signal)
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
# 37. gap-hunt WAVE 1 sources (login-free telos gaps Penumbra lacked): crossref_retractions (research-
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
#     explicit_only: 省提名, named via penumbra_fetch, kept SEPARATE from federal EE (ircc_ee_rounds).
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
# 39. nserc_awards (Penumbra's first CANADIAN funding source): the bulk-CSV pattern's pure halves —
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
# 40. sshrc_awards + cihr_grants — completing Canada's Tri-Council on Penumbra's bulk-file pattern
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
# 42. Phase A signals: advisory metadata stamps (independence_score, freshness_days/class,
#     relevance_hook, source_diversity, conflict_detection, progressive_return). Each tests
#     the EXACT new metadata/_meta shape the implementers produce. Golden-fixtured offline
#     on synthetic inputs; each asserts the EXACT new metadata/_meta shape.
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

# --- #7 independence_score (rank.merge_rank stamps it on each survivor; 0.7x discount for a
#     title-only merge; a singleton gets 0.0). Metadata-only, does NOT touch composite(). ---
from penumbra.core.rank import merge_rank as _merge_rank  # noqa: E402
from penumbra.core.normalize import Document as _PDoc42  # noqa: E402
_ind_d1 = _PDoc42(source="arxiv", source_id="1", url="http://a",
                title="A Long Enough Title For Dedup Testing Here", content="x")
_ind_d2 = _PDoc42(source="s2", source_id="2", url="http://b",
                title="A Long Enough Title For Dedup Testing Here", content="xx")
_ind_ranked = _merge_rank({"arxiv": [_ind_d1], "s2": [_ind_d2]}, "test", limit=5)
check("independence: merged doc carries independence_score",
      "independence_score" in (_ind_ranked[0].metadata or {}))
check("independence: score is a float in [0,1]",
      0.0 <= _ind_ranked[0].metadata.get("independence_score", -1) <= 1.0)
check("independence: a title-only merge is discounted (< 0.5)",
      _ind_ranked[0].metadata["independence_score"] < 0.5)
_ind_solo = _merge_rank({"arxiv": [_PDoc42(source="arxiv", source_id="3", url="http://c",
                                         title="Solo Unique Title That Is Long Enough",
                                         content="y")]}, "test", limit=5)
check("independence: a singleton (no corroboration) gets exactly 0.0",
      _ind_solo[0].metadata.get("independence_score") == 0.0)

# --- #8 source_diversity (fetcher._compute_source_diversity: kind-facet distribution of the ranked
#     list, absent-perspective advisory, unique-source count). Data-driven, not a hardcoded list. ---
_sd_docs = [_PDoc42(source="arxiv", source_id=str(_i), url=f"http://{_i}",
                  title=f"Paper {_i} Title Long", content="x") for _i in range(3)]
_sd = fetcher._compute_source_diversity(_sd_docs)
check("source_diversity: _compute_source_diversity is callable",
      callable(getattr(fetcher, "_compute_source_diversity", None)))
check("source_diversity: output has distribution/absent_perspectives/unique_sources",
      isinstance(_sd, dict) and "distribution" in _sd and "absent_perspectives" in _sd
      and "unique_sources" in _sd)
check("source_diversity: 3 docs from ONE source -> unique_sources == 1",
      _sd["unique_sources"] == 1)
check("source_diversity: distribution tallies all 3 ranked docs; absent_perspectives is a list",
      sum(_sd["distribution"].values()) == 3 and isinstance(_sd["absent_perspectives"], list))
check("source_diversity: empty ranked list -> empty distribution, 0 unique, absent is a list",
      (lambda z: z["unique_sources"] == 0 and z["distribution"] == {}
       and isinstance(z["absent_perspectives"], list))(fetcher._compute_source_diversity([])))

# --- #10 freshness_days + freshness_class (rank.merge_rank stamps a float age + a mechanical
#     bucket; None/None for a dateless doc; naive dates handled like _recency). Pure metadata. ---
from datetime import datetime as _dt42, timezone as _tz42, timedelta as _td42  # noqa: E402
_fd_doc = _PDoc42(source="test", source_id="1", url="http://x",
                title="Fresh Paper Title Long Enough", content="x",
                date=_dt42.now(_tz42.utc) - _td42(days=3))
_fd = _merge_rank({"test": [_fd_doc]}, "test", limit=5)
check("freshness_days: present on a dated doc",
      "freshness_days" in (_fd[0].metadata or {}))
check("freshness_days: ~3 for a doc 3 days old",
      2.5 < _fd[0].metadata.get("freshness_days", 0) < 3.5)
check("freshness_class: 'recent' for a 3-day-old doc (1<d<=7 bucket)",
      _fd[0].metadata.get("freshness_class") == "recent")
_fd_none = _merge_rank({"test": [_PDoc42(source="test", source_id="2", url="http://y",
                                       title="No Date Doc Title Long Enough", content="y")]},
                       "test", limit=5)
check("freshness_days: None for a dateless doc",
      _fd_none[0].metadata.get("freshness_days") is None)
check("freshness_class: None for a dateless doc",
      _fd_none[0].metadata.get("freshness_class") is None)
_fd_brk = _merge_rank({"test": [_PDoc42(source="test", source_id="3", url="http://z",
                                      title="Breaking Item Title Long Enough", content="z",
                                      date=_dt42.now(_tz42.utc) - _td42(hours=6))]}, "test", limit=5)
check("freshness_class: 'breaking' for a <=1-day-old doc (boundary bucket)",
      _fd_brk[0].metadata.get("freshness_class") == "breaking")

# --- #14 relevance_hook (rank.merge_rank stamps an EXTRACTIVE substring from the doc's own text
#     with the highest query-term overlap; '' in browse mode / no-match). Not generative. ---
_rh_doc = _PDoc42(source="test", source_id="1", url="http://x", title="Machine Learning Survey Paper",
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
check("relevance_hook: '' in browse mode (empty query -> no hook)",
      _rh_browse[0].metadata.get("relevance_hook") == "")

# --- #11 conflict_detection (fetcher._detect_conflicts: flags same-named Signal values that
#     diverge >50% across docs from DIFFERENT sources; capped at 5; key absent when none).
#     Adversarial fix: compares deduped Signal values, NOT an O(n^2) title-similarity heuristic. ---
from penumbra.core.normalize import Signal as _Signal42  # noqa: E402
_cf1 = _PDoc42(source="s1", source_id="1", url="http://a", title="Company X Revenue Report Long Title",
             content="revenue 5M",
             signals={"revenue": _Signal42(value=5000000.0, kind="other",
                                           computed_by="source:s1", unit="USD")})
_cf2 = _PDoc42(source="s2", source_id="2", url="http://b", title="Company X Revenue Report 2025 Long",
             content="revenue 8M",
             signals={"revenue": _Signal42(value=8000000.0, kind="other",
                                           computed_by="source:s2", unit="USD")})
_conflicts = fetcher._detect_conflicts([_cf1, _cf2])
check("conflict: a >50%-divergent shared signal across two sources is flagged",
      len(_conflicts) >= 1 and _conflicts[0]["topic"] == "revenue"
      and _conflicts[0]["source_a"] != _conflicts[0]["source_b"])
check("conflict: a singleton (no cross-source pair) produces an empty list",
      fetcher._detect_conflicts([_PDoc42(source="a", source_id="1", url="http://x",
                                       title="Unique Title Long Enough", content="x")]) == [])
# Same source -> NOT a conflict (the fn skips d1.source == d2.source), even with divergent signals.
_cf_same_a = _PDoc42(source="s1", source_id="1", url="http://a", title="Same Source Doc One Long",
                   content="x", signals={"m": _Signal42(value=10.0, kind="other",
                                                        computed_by="source:s1")})
_cf_same_b = _PDoc42(source="s1", source_id="2", url="http://b", title="Same Source Doc Two Long",
                   content="y", signals={"m": _Signal42(value=100.0, kind="other",
                                                        computed_by="source:s1")})
check("conflict: two docs from the SAME source are NOT flagged (cross-source only)",
      fetcher._detect_conflicts([_cf_same_a, _cf_same_b]) == [])
# A shared signal that agrees (ratio <= 1.5) -> no conflict.
_cf_agree_a = _PDoc42(source="s1", source_id="1", url="http://a", title="Agreeing Doc One Long Title",
                    content="x", signals={"m": _Signal42(value=100.0, kind="other",
                                                         computed_by="source:s1")})
_cf_agree_b = _PDoc42(source="s2", source_id="2", url="http://b", title="Agreeing Doc Two Long Title",
                    content="y", signals={"m": _Signal42(value=110.0, kind="other",
                                                         computed_by="source:s2")})
check("conflict: a shared signal within 50% (ratio<=1.5) is NOT flagged",
      fetcher._detect_conflicts([_cf_agree_a, _cf_agree_b]) == [])

# --- #6 progressive_return (fetcher.search_many classifies fast/slow/pending sources from a
#     shared _result_times dict and stamps them in _meta). search_many needs live adapters, so
#     assert the code STRUCTURE (the adversarial fix kept wait(), not as_completed()). ---
import inspect as _inspect42  # noqa: E402
_sm_src = _inspect42.getsource(fetcher.search_many)
check("progressive: search_many stamps fast_sources in _meta",
      "fast_sources" in _sm_src)
check("progressive: search_many stamps slow_sources in _meta",
      "slow_sources" in _sm_src)
check("progressive: search_many stamps pending_sources in _meta",
      "pending_sources" in _sm_src)
check("progressive: kept the load-tested wait() path (adversarial: did NOT switch to as_completed)",
      "_result_times" in _sm_src and "as_completed" not in _sm_src)

# ---------------------------------------------------------------------------
# 43. Orchestration-layer features: handles, gather, evidence schema, overlap, prompts.
# ---------------------------------------------------------------------------

# --- handles: per-doc affordance detection (rank.merge_rank stamps transcribable/enrichable/
#     has_comments as metadata['handles']). Pure pattern match, not a suggestion. ---
_hnd_doc = _PDoc42(source="youtube", source_id="v1", url="https://www.youtube.com/watch?v=abc",
                 title="A Talk About RL Sufficient Length Title", content="RL talk",
                 media=["https://www.youtube.com/watch?v=abc"])
_hnd_r = _merge_rank({"youtube": [_hnd_doc]}, "RL", limit=5)
check("handles: youtube URL detected as 'captioned' (not transcribable, youtube has captions)",
      "captioned" in _hnd_r[0].metadata.get("handles", {}))
_hnd_bili = _PDoc42(source="bilibili", source_id="b1", url="https://www.bilibili.com/video/BV1x",
                  title="Bilibili Video Long Title For Dedup Testing", content="x",
                  media=["https://www.bilibili.com/video/BV1x"])
_hnd_br = _merge_rank({"bilibili": [_hnd_bili]}, "test", limit=5)
check("handles: bilibili URL detected as 'transcribable'",
      "transcribable" in _hnd_br[0].metadata.get("handles", {}))
_hnd_doi = _PDoc42(source="s2", source_id="1", url="http://a",
                 title="Paper With DOI External Id Long Title", content="x",
                 metadata={"external_ids": {"DOI": "10.1038/test", "ArXiv": "2501.99999"}})
_hnd_dr = _merge_rank({"s2": [_hnd_doi]}, "test", limit=5)
check("handles: DOI + arxiv in external_ids detected as 'enrichable'",
      sorted(_hnd_dr[0].metadata.get("handles", {}).get("enrichable", [])) ==
      ["10.1038/test", "2501.99999"])
_hnd_cmt = _PDoc42(source="xhs", source_id="1", url="http://a",
                 title="XHS Post With Comments Long Title", content="x",
                 metadata={"comments": [{"author": "a", "text": "hi"}]})
_hnd_cr = _merge_rank({"xhs": [_hnd_cmt]}, "test", limit=5)
check("handles: has_comments=True when metadata['comments'] is non-empty",
      _hnd_cr[0].metadata.get("handles", {}).get("has_comments") is True)
_hnd_plain = _PDoc42(source="test", source_id="1", url="http://a",
                   title="Plain Doc No Handles Long Enough Title", content="x")
_hnd_pr = _merge_rank({"test": [_hnd_plain]}, "test", limit=5)
check("handles: absent from metadata when no affordances detected (no noise)",
      "handles" not in _hnd_pr[0].metadata)

# --- evidence.py: EvidencePackage TypedDict is importable (pure schema, zero logic) ---
from penumbra.core.evidence import EvidencePackage as _EP, GapEntry as _GE  # noqa: E402
check("evidence: EvidencePackage is importable", _EP is not None)
check("evidence: GapEntry is importable", _GE is not None)

# --- overlap_count: each excluded_relevant entry carries an 'overlap' key ---
_fetch_src = _inspect42.getsource(fetcher.search_many)
check("overlap: excluded_relevant entries carry 'overlap' key",
      '"overlap": _n' in _fetch_src or "'overlap': _n" in _fetch_src)

# --- penumbra_gather: registered, whitelist excludes curator + gather itself ---
from penumbra.server import penumbra_gather as _eg, _init_gather_tools, _GATHER_TOOLS  # noqa: E402
check("gather: penumbra_gather is registered as a tool", _eg is not None)
_GATHER_TOOLS.clear()
_init_gather_tools()
check("gather: whitelist has 15+ read-only tools", len(_GATHER_TOOLS) >= 15)
check("gather: whitelist excludes all curator tools",
      not any("curator" in k for k in _GATHER_TOOLS))
check("gather: whitelist excludes penumbra_gather itself (no recursion)",
      "penumbra_gather" not in _GATHER_TOOLS)
check("gather: rejects empty calls",
      _eg.__wrapped__(calls=[], timeout_s=10).get("error") is not None)
check("gather: rejects >10 calls",
      _eg.__wrapped__(calls=[{"tool": "x"}] * 11, timeout_s=10).get("error") is not None)
# A call to an unknown tool returns status=error per-call (fail-open, not crash)
_g_unk = _eg.__wrapped__(calls=[{"tool": "no_such_tool", "args": {}}], timeout_s=5)
check("gather: unknown tool returns per-call error (fail-open)",
      _g_unk["results"][0]["status"] == "error" and _g_unk["completed"] == 0)
# A call to penumbra_list_sources (the simplest real tool) works inside gather
_g_ls = _eg.__wrapped__(calls=[{"tool": "penumbra_list_sources", "args": {}}], timeout_s=30)
check("gather: penumbra_list_sources works inside gather",
      _g_ls["results"][0]["status"] == "ok" and "sources" in _g_ls["results"][0].get("result", {}))

# --- MCP prompts: registered on the server ---
from penumbra.server import mcp as _mcp43  # noqa: E402
# FastMCP stores prompts in _prompt_manager; check via the prompt functions themselves
from penumbra.server import investigate_person, investigate_lab  # noqa: E402
from penumbra.server import investigate_field, investigate_product, saturation_chase  # noqa: E402
check("prompt: investigate_person is callable", callable(investigate_person))
check("prompt: investigate_lab is callable", callable(investigate_lab))
check("prompt: investigate_field is callable", callable(investigate_field))
check("prompt: investigate_product is callable", callable(investigate_product))
check("prompt: saturation_chase is callable", callable(saturation_chase))
_p_person = investigate_person(target="Test Person", context="RL researcher")
check("prompt: investigate_person returns a list of message dicts",
      isinstance(_p_person, list) and len(_p_person) > 0
      and _p_person[0].get("role") == "user" and "Test Person" in _p_person[0].get("content", ""))


print()
if FAIL:
    print(f"SMOKE FAILED: {len(FAIL)} problem(s)")
    sys.exit(1)
print("SMOKE OK")
