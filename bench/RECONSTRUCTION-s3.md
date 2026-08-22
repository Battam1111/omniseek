# S3 cross-lingual: withdrawn 2026-08-17, and the pre-registered spec for its replacement

The eight S3 tasks have been **removed from this repository**, not merely disabled. We found that
we had built them in an order that put our own catalog inside the judge, which means every number
they ever produced overstates OmniSeek by an amount we cannot bound. They are gone from
`bench/tasks/` and from the default suite list; they remain in git history, and the seven-way
uniqueness audit that condemned them is summarized below.

This document is the tombstone and the replacement spec in one file. The spec half is published
**before a single replacement task is authored**, which is the only form of pre-registration worth
anything: you can check the timestamp on this commit against the commit that adds the new tasks.

## 0. What was wrong, in our own words

The retired tasks were built in this order: run OmniSeek, look at what it surfaced, write a
question around that document, key the answer to that document's identifier. Two defects follow
from the order itself, which is why no amount of per-task repair fixes it.

1. **Catalog bias, in the judge.** Ground truth was a document identifier, so any tool indexing
   the same feed matches the key, and any tool that finds an equally good answer elsewhere is
   scored wrong. Our own catalog was part of the judge.
2. **Provenance bias, in the question.** The question was written from the gold document, so the
   question already encodes the answer's vocabulary. This is the defect that makes fix (1)
   insufficient on its own: swapping the key from the document to a number inside that document
   only changes "did you find our paper" into "did you find the number in our paper".

Three further hazards surfaced in the audit and are gated below: accessibility tier, string
fragility, and the cross-lingual key hazard.

**How we found it.** A three-arm ablation on these tasks (closed book, web search, web search plus
OmniSeek) scored 0, 1 and 4 out of 7. Reading the complement arm's three misses, none of them was
a retrieval failure. Every one was a defect in our own task: in one, the answer it gave was more
on-topic than our gold; in another, two independent arms converged on a document our question
could not exclude; in the third, our key demanded a Chinese secondary commentary while even the
closed-book arm named the primary paper from memory.

**The number this instrument produced was bad for us.** The last published run scored S3 at 0 of 7
on a cold CI install. We are not withdrawing a flattering result. We are withdrawing an instrument
whose bias ran in our favor and which we therefore cannot cite in either direction.

## 1. The inversion

Old order: document, then question, then key.
New order: **need, then document, then key**, with the need frozen before anyone looks.

| Phase | Who | May use OmniSeek | Output |
| --- | --- | --- | --- |
| A. Need authoring | blind authors | **no** | a real information need, frozen |
| B. Prospecting | two independent prospectors | one yes, one no | candidate answering documents, per route |
| C. Key extraction and uniqueness | auditors | yes | a verbatim claim key plus its uniqueness evidence |
| D. Gates | mechanical | n/a | pass or reject, with the reason recorded |
| E. Admission | adjudicator | yes | admitted, or discarded with a published reason |

## 2. Phase A: blind need authoring

An author writes an information need **without knowing what OmniSeek can find**. This is the
single most important property in the spec: a need authored in ignorance of our catalog cannot
have been shaped to fit it.

Requirements per need:

- **Real.** A question a working person would actually ask, with one sentence saying who asks it
  and why. Not a quiz item, not a question about a document.
- **Answer-agnostic.** The author must not have a target document in mind. If the author already
  knows the answering paper, the need is disqualified and recorded as such.
- **Language-directional.** Half the needs are authored in Chinese, half in English, declared per
  need. The suite tests crossing languages, so the direction is part of the item.
- **Domain spread declared in advance**, so the set is not silently concentrated where we are
  strong.

**Blindness is enforced mechanically, not promised.** Authors run with OmniSeek's tools unloaded
and are instructed never to load them. Those tools are deferred, so they cannot be invoked without
an explicit load call, and the absence of that call in the preserved transcript is the check.
Author transcripts ship with the suite. Authors may use open web search freely: the point is that
they are blind to our catalog, not blind to the world.

## 3. Phase B: prospecting

Only after the need is frozen does anyone look for an answer. Two prospectors work the same frozen
need independently:

- **Web-only prospector**: open web search and fetch, no OmniSeek.
- **Eye-enabled prospector**: OmniSeek plus open web.

Both record what they found and by which route. A need where **neither** finds a decisive answer is
discarded as unanswerable and published as such. A need where the **web-only** prospector finds a
decisive answer is not thereby discarded: it goes through the same gates, and if admitted it
becomes a task we are expected to pass and claim no advantage on. Admitting some of these is
deliberate. A suite composed only of needs where plain search fails would be rigged by selection
even with everything else here obeyed.

## 4. Phase C: the key is a claim, never a document

Ground truth is one short, verbatim, mechanically checkable fact that only the answering document
contains and that visibly answers the frozen need. The judge tests containment of that string, not
`identifier_in_topk`.

Uniqueness is tested, not assumed. For each candidate key the auditor checks it against (a) every
confusable document a competent searcher lands on for this need, and (b) an open-web search for
the key string itself. A candidate that was not tested is not a candidate.

The audit's own principle, which killed a candidate that looked strong:

> **A rare phrase is obscure, not unique.**

## 5. Phase D: the three gates

**D1. Accessibility tier.** Every task declares the tier its key lives at.

- `T1` open surface: title, abstract, landing page, first screen.
- `T2` open body: full text of a freely fetchable document, no login, no render required.
- `T3` gated: behind a login, requiring a browser render, or present only in audio or pixels.

A retrieval task must key at T1 or T2. Keying a retrieval task at T3 silently converts it into a
fetch-capability test, which S1, S2 and S4 already measure and measure better. Tier mismatch is a
rejection, not a note.

**D2. Multi-route render stability.** The key string must render identically through at least three
independent routes (publisher HTML, PDF text extraction, our own reader). Any divergence rejects
the candidate unless the divergence is fully absorbed by the judge's normalization. Observed
failures that motivate this gate: an accented term rendering three ways, a literal `43\%` in a
publisher's own HTML, and a number split apart by a PDF line-number column.

**D3. Cross-lingual keys.** Prefer a **language-neutral** key: a number, a proper name, an
identifier, a formula. Where no neutral key exists, the task declares `key_language` and ships a
**pre-registered set of accepted faithful translations**, frozen before any arm runs and authored
by an agent that has not seen any arm's output. A monolingual key with no accepted-translation set
measures the answer's output language rather than retrieval, and is rejected.

## 6. Phase E: admission, with the search-resistance receipt tightened

The existing prefilter records tool, query, date, first-page hit and top hits. Two changes:

1. **Expert rephrase round.** Resistance counts only if the original query **and** an expert
   rephrase in the other language both miss the first page. Measured basis: in the ablation, one
   English rephrase put two independent channels on the first page at once, so a single
   native-language query overstates resistance against any agent that can rewrite.
2. **Honest field name.** The field stays `search_resistance_prefilter`, and every public sentence
   about it says "first page miss under the recorded query and date", never "search cannot find
   this".

## 7. The funnel is published, and that is the anti-rigging device

Every authored need is published with its outcome: admitted, discarded as unanswerable, discarded
at a named gate, or admitted as a web-answerable control. The old process silently dropped
everything OmniSeek did not win, so the selection bias lived in the discard pile where no reader
could see it. Publishing the whole funnel puts the denominator back.

Pre-registered targets, fixed before authoring begins:

- **32 needs authored** (16 Chinese-directional, 16 English-directional).
- **No minimum admission count.** If five survive, the suite ships with five and says so. If
  thirty survive, it ships with thirty. Tuning the gates after seeing the yield is forbidden; a
  gate change after this point is published as a dated amendment with its reason.

## 8. Judge and runner changes required

- `identifier_in_topk` is retired for this suite. S3 tasks use containment against a claim key.
- Containment accepts a pre-registered accepted-forms list (D3), not a single string.
- Task schema gains `accessibility_tier`, `key_language`, `accepted_forms`, `render_routes`, and
  `funnel_id` linking a task back to its authored need.

## 9. Why the suite was withdrawn outright rather than annotated

The first draft of this spec kept the published 0 of 7 and added a caveat to the results row, on
the reasoning that pulling an unfavorable number because our own instrument was flawed is the move
that most deserves suspicion. We changed it, and the reason is worth recording.

Once the tasks are deleted, the row stops existing on the next run, so an "under reconstruction"
report state would be machinery built for a row that will not be there. The disclosure obligation
is better served by this document, which explains the whole thing, than by a note in a table cell
that could not. The old number stays reachable in git history and is quoted in section 0 above,
where it is doing more work than it ever did in the table.

The published results page will still show the retired S3 row until the next benchmark run
regenerates it. That is a stale artifact of a manually dispatched workflow, not a claim we are
still making.

## 10. Scope and limitations, stated up front

- This rebuild covers **S3 cross-lingual** only. Of the 46 remaining tasks, 40 hand the agent an
  explicit target and therefore never measure finding, so neither catalog nor provenance bias
  applies to them. The 6 that are query-driven (one in S4, five in S6) are reviewed against D1 to
  D3 individually; S6's are assertions about the memory contract rather than document retrieval.
- Blindness is enforced by tool availability and verified by transcript, not by sandboxing. An
  author who ignored the instruction and reasoned from memory of our catalog would not be caught.
  Recorded as a known weakness rather than claimed away.
- Search resistance is measured with the engines we can actually drive, on a recorded date. It is
  a snapshot, and the receipts say so.

## Amendment 2026-08-22: domain spread, declared before authoring

Phase A begins after this commit; check this commit's timestamp against the commit that adds the
authored needs. The 32 needs distribute as four per domain, two Chinese-directional and two
English-directional in each:

1. jobs and workplace
2. visas and cross-border status
3. academic publishing and research practice
4. software engineering and open source
5. personal finance and tax
6. consumer electronics and hardware
7. games and popular culture
8. life administration and local services

Domains 7 and 8 are deliberately outside this catalog's strengths, per section 2's
anti-concentration requirement: a spread chosen only where we expect to win would be selection
bias moved one level up. Authoring agents receive their domains and direction quotas and nothing
about this repository's sources.

One enforcement detail recorded honestly: for the authoring agents used here, tool availability
is not fully controllable, so blindness is enforced by explicit prohibition and verified by
auditing each author's full preserved transcript for ANY invocation of this system's retrieval
tools, load calls included. That check is strictly stronger than the load-call absence described
in section 2; the section 10 caveat about memory-based reasoning stands unchanged.

## Amendment 2026-08-22b: what "visibly answers the frozen need" means, clarified after two
## auditors diverged

Phase C ran as two independent auditors over disjoint halves of the funnel. They diverged on one
criterion: whether a claim key must cover EVERY sub-question of a multi-part need, or must settle
the need's central actionable question. One auditor applied the maximal reading and returned
zero candidates across sixteen needs, in several cases while its own recorded measurements
showed a key that was unique, render-stable, and readable at tier T1 or T2.

The maximal reading is rejected, for a reason internal to this spec: section 4 requires the key
to be ONE SHORT verbatim fact, and one short fact cannot cover a two- or three-headed need by
construction, so the maximal reading makes most real needs un-keyable and would push the suite
back toward single-fact quiz items, which section 2 forbids. The clarified criterion:

- A key must settle the need's CENTRAL question, the one the asker would act on.
- The admission record must state which sub-questions the key settles and which remain open.
- A need whose central question itself has no decisive, readable answering document remains
  `no_viable_key`.

No gate changes. Uniqueness, D1, D2 and D3 are untouched, and every rejection already recorded
under those gates stands. The affected `no_viable_key` records are being re-audited under this
clarified criterion, with all gate legs run in full for any newly proposed key; both the original
and the re-audited records ship in the published funnel.
