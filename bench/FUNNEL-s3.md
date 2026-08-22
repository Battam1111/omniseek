# S3 rebuild: the published funnel

This is the funnel that `bench/RECONSTRUCTION-s3.md` section 7 promised: every authored need,
its outcome, and the reason, so the denominator is visible. The spec and its two dated
amendments (domain spread declared before authoring; the key criterion clarified after two
auditors diverged) were published before the work they govern, and the commit timestamps let
you check the order. The admitted tasks live in `bench/tasks/s3-crosslingual/`, one file per
task, each carrying its key, its uniqueness and render receipts, its admission record, its
search receipts, and its verification result.

## How the phases ran

- **A. Blind authoring.** Four authoring sessions, eight needs each, domains and language
  quotas assigned in advance. Blindness was verified by auditing each session's full preserved
  log for any invocation of this system's retrieval tools, load calls included: zero across all
  four. The sanitized tool-call sequences ship in `bench/funnel-s3/author-transcripts/`. One
  need (N11) was self-disqualified by its author, who realized they already knew the answering
  document; it was replaced by N11-r and both are in the table.
- **B. Prospecting.** Two independent prospectors per need, one web-only and one eye-enabled.
  No need was discarded here: zero needs came back empty from both arms.
- **C and D. Keys and gates.** Two auditors over disjoint halves, gates run inline. They
  diverged on one criterion; the divergence and its resolution are public as the 2026-08-22b
  amendment, and the thirteen affected records were re-audited under the clarified criterion
  with every gate unchanged. Rows marked with an asterisk carry both verdicts, original first.
- **E. Admission.** One adjudicator (this repository's maintainer agent) re-verified every
  candidate's uniqueness with an independent exact-string search, applied the clarified
  criterion in both directions, and admitted or discarded with the reasons below.

## The task instrument, stated exactly

An admitted task is one deterministic tool call: `omniseek_search` with the frozen need text,
verbatim, as the query, routed to the open-web neural source (`sources: ["exa"], raw: true,
limit: 10`). The judge tests normalized containment of the claim key in the text the call
returns. Two consequences are worth stating plainly:

1. **The query is not authored.** It is the frozen need, byte for byte, so there is no query
   to tune toward what this system happens to surface. That closes the tuning channel the old
   suite died of.
2. **The judge measures delivered text, not ranked URLs.** A run passes only if the key
   sentence is in what the call actually returned. Surfacing the right document but returning
   an excerpt that misses the key sentence fails, which is also what the user experiences.

The catalog-wide sweep is not used as the instrument because these needs were deliberately
authored across domains outside the curated catalog's strengths (see the domain amendment);
the open-web route is the product surface that can honestly attempt them.

## Verification against the live pipeline, 2026-08-22

Every admitted task was run once, at authoring time, with arguments identical to the task
input. Results, in full:

| task | funnel | answering doc in top 10 | key sentence in returned text |
| --- | --- | --- | --- |
| s3-cl-001 | N01 | no | no |
| s3-cl-002 | N02 | no | no |
| s3-cl-003 | N03 | yes (same-publisher mirror) | no |
| s3-cl-004 | N04 | yes | yes |
| s3-cl-005 | N12 | no | no |
| s3-cl-006 | N16 | yes | no |
| s3-cl-007 | N22 | no | no |
| s3-cl-008 | N24 | yes | no |
| s3-cl-009 | N28 | yes | no |
| s3-cl-010 | N32 | no | no |

That is 5 of 10 needs where the single call surfaces the answering document and 1 of 10 where
it delivers the key sentence. The spec's section 3 said web-answerable controls are tasks "we
are expected to pass"; the recorded reality is that today's pipeline does not, and each task
file records which leg failed. This number ships as it is. The suite now measures exactly the
two product properties that would move it: ranking the destination document into the first
page, and returning the span that answers rather than the span that matches.

## Search resistance: none claimed

Every admitted task has at least one recorded first-page hit for the answering document, under
a recorded query and date (for eight tasks, the web-only prospector's own query; for the
others, the adjudicator's original-language query or expert rephrase in the other suite
language). Per the spec's section 6, no task in this suite claims search resistance, and every
receipt says only "first page hit or miss under the recorded query and date". The full
receipts are in each task file under `search_resistance_prefilter`.

## The funnel, all thirty-three records

Directions: zh means the need was authored in Chinese, en in English. Verdicts marked * were
re-audited under amendment 2026-08-22b; the row shows original then re-audit.

| id | domain | dir | phase C/D verdict | outcome |
| --- | --- | --- | --- | --- |
| N01 | jobs and workplace | zh | candidate | **admitted** (s3-cl-001) |
| N02 | jobs and workplace | zh | candidate | **admitted** (s3-cl-002) |
| N03 | jobs and workplace | en | candidate | **admitted** (s3-cl-003) |
| N04 | jobs and workplace | en | candidate | **admitted** (s3-cl-004) |
| N05 | visas and cross-border status | zh | rejected: uniqueness | discarded: the key sentence exists verbatim on both the official MOM page and a government mirror; a mirror is a confusable |
| N06 | visas and cross-border status | zh | candidate | discarded at admission, see below |
| N07 | visas and cross-border status | en | rejected: D2 | discarded: the publisher route failed at the TLS layer from the audit egress, so three independent render routes could not be shown |
| N08 | visas and cross-border status | en | rejected: uniqueness | discarded: the statutory sentence and its shorter replacement both appear in a legal commentary; a key found in a confusable fails |
| N09 | academic publishing and research practice | zh | rejected: D2 | discarded: the key survived uniqueness but could not be shown stable across three independent render routes |
| N10 | academic publishing and research practice | zh | candidate | discarded at admission, see below |
| N11 | academic publishing and research practice | en | none | self-disqualified by its author during blind authoring (author already knew the answering document); replaced by N11-r |
| N11-r | academic publishing and research practice | en | no viable key | discarded: no candidate document yielded a personally readable, verified claim string |
| N12 | academic publishing and research practice | en | candidate | **admitted** (s3-cl-005) |
| N13 | software engineering and open source | zh | candidate | discarded at admission, see below |
| N14 | software engineering and open source | zh | no viable key | discarded: no readable document reports the asked-for migration percentage and one-year rollback outcome |
| N15 | software engineering and open source | en | no viable key | discarded: no study reporting the asked-for before/after PR volume with contributor loss was found by either arm |
| N16 | software engineering and open source | en | candidate | **admitted** (s3-cl-006) |
| N17 | personal finance and tax | zh | no viable key \* no viable key | discarded: settling the combined tax and foreign-exchange action requires two documents; one-answering-document requirement fails |
| N18 | personal finance and tax | zh | no viable key | discarded: the sentence is repeated verbatim in a provincial tax-bureau answer (not unique), and the remaining heads have no single decisive document |
| N19 | personal finance and tax | en | no viable key \* no viable key | discarded: the readable HMRC sentence is one fact from one worked example; the limbs that decide the action live in other documents |
| N20 | personal finance and tax | en | no viable key \* rejected: D2 | discarded: the central action is decisively answered, but the publisher blocks two of the three required render routes |
| N21 | consumer electronics and hardware | zh | no viable key \* no viable key | discarded: the only decisive source now serves a verification challenge; the readable claim is not the central procurement decision |
| N22 | consumer electronics and hardware | zh | no viable key \* candidate | **admitted** (s3-cl-007) |
| N23 | consumer electronics and hardware | en | no viable key \* no viable key | discarded: the readable claim is component-level and cannot carry the central new-versus-used decision |
| N24 | consumer electronics and hardware | en | no viable key \* candidate | **admitted** (s3-cl-008) |
| N25 | games and popular culture | zh | rejected: D1 | discarded: the key lives behind a walled, render-required source; tier T3 converts a retrieval task into a fetch test |
| N26 | games and popular culture | zh | no viable key \* no viable key | discarded: the concrete claim covers one title, one merchandise class, an old guideline version; not the central decision |
| N27 | games and popular culture | en | rejected: uniqueness | discarded: the mandatory confusable-document leg could not be completed (two candidate bodies unreadable), so uniqueness is unproven |
| N28 | games and popular culture | en | no viable key \* candidate | **admitted** (s3-cl-009) |
| N29 | life administration and local services | zh | no viable key \* rejected: D2 | discarded: the official notice body was unreadable through every route in this run |
| N30 | life administration and local services | zh | no viable key \* no viable key | discarded: no official page was readable; the available article is a practice opinion on a different question |
| N31 | life administration and local services | en | no viable key \* rejected: D2 | discarded: only one of the three required routes yields the key |
| N32 | life administration and local services | en | no viable key \* candidate | **admitted** (s3-cl-010) |

Totals: 33 records authored (32 live), 0 discarded at prospecting, 13 candidates out of the
key audit, 10 admitted, no minimum was targeted and none was needed.

## The three admission-stage discards, in full

- **N06.** The central question (whether remote work from inside China for an overseas
  employer with no PRC entity is illegal employment) has no decisive readable answering
  document; both prospecting arms independently recorded that no authoritative PRC position
  exists. The candidate key was a practitioner FAQ's blanket sentence that answers the
  adjacent general work-permit question. Amendment 2026-08-22b's third bullet applies: the
  need stays without a viable key. The same clarified criterion that revived four
  over-rejected needs removes this over-admitted one; it cuts both ways or it is not a
  criterion.
- **N10.** The need ("what actually happened to people who self-reported an honest error")
  admits several independent, equally decisive answering documents: an interview report, a
  Web of Science study on the citation penalty vanishing for self-reports, and an academic
  commentary. Keying any one of them scores a system that finds the others as wrong. That is
  defect 2 of the withdrawn suite (a question that cannot exclude an equally good answer),
  so the need is discarded rather than keyed.
- **N13.** Both arms surfaced a more specific process document than the chosen one, and that
  better document failed render checks from this egress; the fallback key is a generic
  review-requirement sentence that many documents state in other words. A task keyed to it
  would penalize a system for retrieving the better answer. No render-stable decisive single
  document exists, so the need is discarded.

## Language balance, honestly

Authored 16 Chinese-directional and 16 English-directional. Admitted: 3 Chinese-directional
(N01, N02, N22) and 7 English-directional. Two admitted keys are Chinese, eight are English;
every monolingual key ships with its pre-registered accepted translation into the other suite
language, per gate D3. The imbalance is what survived the gates, and it is visible here
rather than rebalanced by hand.

## Known weaknesses carried forward

- Blindness is enforced by prohibition and verified by transcript audit; an author reasoning
  from memory of this catalog would not be caught (spec section 10, unchanged).
- The web-only prospector hit its search budget partway through batches 3 and 4, so some
  original-query receipts in that range come from the adjudicator's own recorded searches
  instead of the prospector's.
- Several official domains (gov.cn among them) refuse the plain fetch tool the web arm used;
  verification reads for Chinese primary sources leaned on the eye route, and two needs died
  at D2 precisely because no second independent route could be shown. Route scarcity is a
  gate, not an excuse.
- exa, the open-web route used by the instrument, returns query-relevant excerpts rather than
  whole documents; the verification table above shows how often the excerpt window misses the
  key even when the document is found. That is recorded as part of what the suite measures.
