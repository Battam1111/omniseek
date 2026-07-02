# Patterns

<sub>[penumbra](../README.md)&nbsp;·&nbsp;[Configuration](configuration.md)&nbsp;·&nbsp;[Tools](tools.md)&nbsp;·&nbsp;**Patterns**&nbsp;·&nbsp;[Walled sources](walled-sources.md)&nbsp;·&nbsp;[Brand](BRAND.md)</sub>

This is not a new tool. It is how to get more out of the tools in [Tools](tools.md): how to route a
query, how to read what came back, and how to notice what didn't.

---

## Route before you search

Don't hardcode a source list from memory: the catalog keeps growing, so read it live.
`penumbra_sources(domain=..., query=...)` narrows to the sources that actually match, each
with a description.

Then pick the simplest call for the intent:

| Call | When |
|------|------|
| `penumbra_search` | The default. Deduped, ranked, cross-lingual recall folded into one list. |
| `penumbra_search(raw=True)` | You want each source's raw bucket separately, to compare source-by-source. |
| `penumbra_search(sources=["<one>"], raw=True, full=True)` | The drill idiom: ONE named source, unbounded, whole content. The escape hatch for a slow or walled source a broad sweep would drop. |

## Walled sources: name them, don't sweep them

A broad `penumbra_search` sweep is deadline-bounded (`wait_s`) and skips
`explicit_only` sources (slow, login-walled, or otherwise not safe to include in every sweep) so
one query never hangs on the slowest source.

`_meta.excluded_relevant` is the query-aware subset of those: the walled sources whose facets
actually match what you asked, each with a copy-paste `sources=[...]` hint to re-run against just
them. Name the ones that match the query's domain; don't sweep the whole walled cluster into every
call; that only serializes them against each other for no gain.

## Corroboration over hit count

`penumbra_search` results carry `metadata.also_in`: other sources mirroring the same
underlying item. Count **independent upstreams**, not hits: a paper mirrored across five indexes is
one corroborating source, not five.

`penumbra_graph` view=`voices` is the tool-layer form of this count: hand it the doc ids from a
search (`doc:{source}:{source_id}`) and it collapses them to distinct upstream voices via same-work
identity and shared authorship, so "five sources agree" becomes "N independent voices agree" (or
fewer). Docs with zero connecting evidence come back in `unresolved` and are never counted as
voices; counting unknowns as independent would fabricate corroboration.

When the `exploratory` policy surfaces a same-work candidate you then verify yourself, record the
judgment with `penumbra_ruling(action="create")`; the `working` policy applies it from then on.
The graph projects candidates mechanically; whether two things are the same is always your call.

When sources disagree, that disagreement is the finding. Surface it (who said what, which is the
first-party source vs. a reblog) rather than averaging it away.

## The gap ledger

`_meta` carries what did NOT come back, not just what did: `empty`, `timed_out`, `errored`,
`excluded`, `excluded_relevant`. A dimension that returned nothing is a fact to act on, not a fact to
paper over: sharpen the query and re-run it, or say plainly that this angle wasn't covered. Never
fill a gap from memory instead.

## Contributing a source

Building the other direction, a new source instead of a query? See the admission razor in
[CONTRIBUTING](../.github/CONTRIBUTING.md).

---

<div align="center"><sub><a href="../README.md">← back to the README</a></sub></div>
