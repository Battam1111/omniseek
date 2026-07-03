# Case study: what plain search missed

<sub>[penumbra](../README.md)&nbsp;·&nbsp;[Configuration](configuration.md)&nbsp;·&nbsp;[Tools](tools.md)&nbsp;·&nbsp;[Patterns](patterns.md)</sub>

We gave an agent one job: map a fast-moving research frontier, credit assignment in
multi-agent LLM systems, as it stood that week. It had two ways to reach the world: ordinary
web search, and Penumbra. Here is what the difference looked like. Every fact below is from a
single real session; nothing is dramatized.

## It already remembered the field

A standing query had been watching this topic for a week before the investigation began, so
the agent did not start cold. Almost every result came back marked as already seen, with the
date it first appeared. "Is this paper new?" stopped being a judgment call and became a fact
the memory could answer. A week of watching the agent never did itself was simply inherited.

## The most important find was invisible to English search

The single most valuable document in the whole investigation was a complete survey of the
field, the first to map the full lineage from reasoning-RL to agentic to multi-agent credit
assignment. Two separate English-language sweeps did not surface it. It came back through a
Chinese-language forum, in a highly upvoted explainer post that pointed straight at the paper.

The same thing happened again with a brand-new method, an unsupervised entropy-based approach
that the Chinese technical community was discussing before the English index had caught up.
This is not a story about one lucky query. It is the structural advantage: the knowledge
existed, in a language the English web had not yet indexed as data, and reaching it was worth
more than any amount of re-phrasing an English search.

## It counted sources, not hits, and flagged the disagreement

Four records of the same paper across four indexes are not four sources. Penumbra collapsed the
mirrors and reported the distinct upstream voices, so "everyone agrees" could be checked
instead of assumed. And when two indexes reported different citation counts for the same paper,
one said 1, another said 9, the conflict surfaced on its own, carrying the ratio, instead of
being quietly averaged into a false single number.

## What did NOT come back was reported too

At the end, Penumbra was explicit about the blind spots: no audio or social perspective in this
pass, two sources timed out, one author affiliation went unverified. A dimension that returned
nothing is a fact to act on, not a gap to paper over.

## The advantage compounds

The agent recorded three judgments during the investigation: that two records were the same
paper, that one work anchors the turn-level line of research, that the Chinese post covers the
new method. Those are now permanent, attributed, and reversible. The next investigation into
this field does not start from zero. It starts from everything this one concluded.

## What Penumbra did not do

It never summarized. It never decided the survey was "the most important", the agent did that.
It never wrote a word of this. Penumbra reached, and it remembered; the thinking was the
agent's, start to finish. That is the whole point: nothing that can hallucinate ever touched
the data.

---

<div align="center"><sub><a href="../README.md">back to the README</a></sub></div>
