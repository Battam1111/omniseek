# Legal Posture

<sub>[omniseek](../README.md)&nbsp;·&nbsp;[Configuration](configuration.md)&nbsp;·&nbsp;[Tools](tools.md)&nbsp;·&nbsp;[Patterns](patterns.md)&nbsp;·&nbsp;[Walled sources](walled-sources.md)&nbsp;·&nbsp;[Privacy](PRIVACY.md)&nbsp;·&nbsp;**Legal posture**</sub>

> **THIS IS NOT LEGAL ADVICE.** It is an engineering description of what the published software does
> and does not do, written so that an operator, a reviewer, or a source's owner can check each claim
> against the code rather than take it on trust. Whether any particular deployment is lawful in any
> particular jurisdiction is the operator's question, and it is not one this project can answer.

OmniSeek retrieves. That is a capability with an edge to it, and the honest thing is to say exactly
where this project puts the edge, in terms that can be verified.

---

## 1. What is published, and what is not

The published artifact is **a general engine plus general adapters**. It is not a dataset, not a
corpus, and not a collection of anyone's credentials.

- No credentials, cookies, tokens, or session state are in this repository or in any release. They
  live under `~/.omniseek/credentials` and in the operator's own browser profiles, on the operator's
  own machine. See [PRIVACY.md §3](PRIVACY.md).
- No retrieved content is redistributed here. The engine fetches at call time and returns to the
  calling agent; it publishes nothing back.
- Sources written for one operator's private circumstances are excluded from the public mirror
  rather than genericised, because a targeted adapter is a description of a particular person's
  situation and does not belong in a public catalog.

## 2. The access tiers

The catalog is self-describing about how a source is reached. The engine classifies every source
into one of four **access tiers**, and the tier is machine-readable, not a matter of prose:

| Tier | What it means | Default |
|---|---|---|
| `free` | Public, no authentication | **on** |
| `keyed` | An API key the operator obtains from the provider | on when a key is present |
| `walled` | A login the operator personally holds | **off** |
| `circumvention` | Defeating an access control | **not shipped** |

The taxonomy lives in `src/omniseek/core/fetcher.py` (`_ACCESS_TIERS`), and the tier is derived from
the source's own declaration, so a source cannot quietly sit in a gentler tier than it belongs to.

## 3. The line this project draws

**OmniSeek will act on an operator's own entitlements. It will not manufacture entitlement.**

Concretely, the engine is willing to use a login the operator already holds, on the operator's own
machine, to read what that login already lets them read. It is not willing to defeat an access
control in order to read what no one gave the operator the right to read. The first is automation of
a person's own access. The second is a different act, and this project does not ship it.

That line is enforced in two places rather than asserted once:

1. **The walled tier is deny-by-default, including on a fresh clone.** A newly cloned OmniSeek with
   no configuration file at all enables every non-walled source and **no** walled source. Turning one
   on takes two deliberate edits (`walled.enabled`, then naming the source under
   `walled.bring_your_own`). This matters most for exactly the reader who is not thinking about it:
   someone who clones, runs, and never opens the config. See `is_source_enabled` in
   `src/omniseek/core/profile.py`.
2. **No shipped source declares the circumvention tier, and the mirror script refuses to publish one
   that does.** This is a gate in `scripts/sync_from_eye.sh`, not a promise: it scans the tree it is
   about to publish for a circumvention declaration and aborts the sync if it finds one. The claim in
   this document and the contents of the catalog therefore cannot drift apart silently.

## 4. What the operator is responsible for

Everything about the operator's own use, and this is not a formality.

- **Terms of service.** Each source has its own terms. Enabling a walled source means the operator
  has decided that their use of their own account, through their own browser, is consistent with the
  agreement they personally accepted. This project cannot make that judgment for a deployment it
  never sees.
- **Jurisdiction.** Automated access, and access to a computer system generally, is governed
  differently in different places. The operator runs the software; the operator is in a jurisdiction;
  the project is not a party to that.
- **Rate and burden.** The engine paces its walled access and bounds its concurrency, but an operator
  can widen those. Placing an unreasonable load on someone else's service is the operator's act.
- **What is done with what comes back.** Retrieval is not republication. Reading a page and then
  redistributing it are separate acts with separate rules.

## 5. Deliberate design choices with a legal shape

Three properties of the engine exist partly for this reason, and are worth naming so they are not
mistaken for accidents:

- **Loopback by default, bearer token required.** The service refuses to start without a token and
  binds `127.0.0.1`. It is built to be one person's tool on one person's machine, not a shared
  gateway that lets others reach a source through the operator's credentials. See
  [SECURITY.md](../.github/SECURITY.md).
- **Human-paced walled access.** Walled reads are paced, with gaps between requests and bounded
  concurrency, so the engine does not impose load a human reader would not. The shipped defaults are
  the conservative profile; a faster one exists and is an explicit operator choice, per source. This
  pacing is not a disguise: the traffic is the operator's own authenticated session, and the operator
  is not anonymous to the source they are logged in to.
- **Retrieved content is data, never instruction.** The MCP surface tells the consuming agent that
  everything OmniSeek returns is untrusted external data. A fetched page must never be able to
  redirect the agent's task or extract its secrets.

## 6. Reporting a concern

If you operate a source and believe an adapter in this repository is inconsistent with your terms,
open an issue, or use the private channel in [SECURITY.md](../.github/SECURITY.md) if the report is
sensitive. A concrete report about a specific adapter will get a concrete answer, and removing an
adapter is an available answer.

---

*See also [PRIVACY.md](PRIVACY.md) (data handling), [SECURITY.md](../.github/SECURITY.md) (threat
model), [walled sources](walled-sources.md) (how the walled tier works in practice), and
[NOTICE](../NOTICE).*
