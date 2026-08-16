# FAQ

<sub>[OmniSeek](../README.md)&nbsp;·&nbsp;[Configuration](configuration.md)&nbsp;·&nbsp;[Tools](tools.md)&nbsp;·&nbsp;[Patterns](patterns.md)&nbsp;·&nbsp;[Case study](case-study.md)</sub>

Short answers to the questions people ask first. The one-line version of what OmniSeek is:
**self-hosted reach and memory for your AI agent. It reaches what plain search cannot,
remembers what it reached, and never thinks for you.**

---

## Is this just a search wrapper?

No. Search is one of the things it does, and the most visible, so it is the one people notice
first. But retrieval is only half. Everything your agent reaches accretes into a persistent
memory it can ask questions of later: what have I already seen, what is new since a date, how
do these two things connect, how many of these "independent" sources are actually one voice.
A search wrapper answers a query and forgets it; OmniSeek answers a query and keeps it. The
memory is a file on your disk that appreciates the more you use it.

## How is it different from Perplexity, or Exa / Tavily?

Two axes: who owns the index, and who does the thinking.

- **Perplexity** rents you answers: its index, its model, its editorial. You get a written
  conclusion.
- **Exa / Tavily** rent you reach: their index, your model. You get results to reason over,
  from an index they own and you cannot see.
- **OmniSeek** is the missing corner: your index, your judgments, your model's thinking. It
  returns evidence with provenance, never an answer; the index is yours (a self-hosted file);
  and it remembers across sessions, which a stateless search API cannot.

OmniSeek does not compete with an open-web search API on breadth: those index billions of
pages, while OmniSeek curates a catalog where every source earned its seat by beating plain
search via a mode (structure, unwalling, transcription, longitudinal recall, monitoring). It
competes on depth, memory, and ownership. Use both if you like: reach the open web with a
search API, and send OmniSeek where search can't follow.

## How is it different from GraphRAG?

GraphRAG extracts a knowledge graph from your corpus with a model at index time, which freezes
one model's reading of the text: re-run it with a better model and you re-extract and re-pay,
and an extraction mistake becomes a "fact" in the graph. OmniSeek stores mechanical facts and
your agent's attributed judgments; the embeddings are computed at query time, so a model
upgrade makes the whole memory smarter for free and nothing stored ever rots. Relations are
judged by your agent, with a note and a source, and are reversible and re-judgeable, never
batch-extracted and frozen. The tradeoff is honest: GraphRAG will summarize a private corpus
out of the box; OmniSeek will not summarize anything (it never generates), and it reaches the
world rather than a corpus you loaded.

## How is it different from agent-memory tools (mem0, Zep, Letta)?

They remember your conversations with the agent (what the user said, what the agent decided
about the user). OmniSeek remembers the world your agent reached (what it retrieved, and what
it judged about that). Different layer, not a competitor. Run both: one is the agent's memory
of you, the other is the agent's memory of what it has seen out there.

## Does it phone home? What about my data?

Never. OmniSeek binds to loopback, requires a bearer token on every request, and makes no
outbound call except to the sources you enable. It is self-hosted: your logins, your API
quotas, your memory file. There is no server for us to see, and no telemetry.

## Is it legal to reach login-walled sources?

Walled sources are off by default. When you turn one on, you supply your own logged-in browser
session: OmniSeek reaches only what your own account can already see, using your own credentials
on your own machine. It is not a scraper-for-hire or a paywall bypass; it is your agent reading,
with your eyes, pages you already have the right to read. Some source families carry more legal
risk than others and are excluded from the default pack; see the source notes.

## Does it think, summarize, or decide anything?

No, by design. OmniSeek refuses three things permanently: it never generates (there is no model
inside, so nothing hallucinated enters your data), it never judges (what counts as the same
thing, what relates to what, what matters, your agent decides and OmniSeek records the decision
with its evidence), and it never phones home. In a stack where every other layer is a model that
can hallucinate, OmniSeek is the one layer that structurally cannot, because it never generates.

## What does it cost / how do I run it?

Free and open source (Apache-2.0). One `docker compose up`, or a non-Docker bootstrap. It runs
on your machine; the only costs are any keyed sources you choose to add (most keys are free) and
your own compute. See the [README](../README.md) quick start.

## Who is it for?

Agent builders in the MCP ecosystem who want their agent to reach deeper and remember; anyone
doing research who wants a memory that compounds; and self-hosters who want to own
the whole stack. Commercial use is entirely yours to make (Apache-2.0); the project sells
nothing.

---

<div align="center"><sub><a href="../README.md">← back to the README</a></sub></div>
