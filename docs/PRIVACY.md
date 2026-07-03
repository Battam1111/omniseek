# Privacy Posture

> **Status: RELEASE-PERIOD DRAFT.** This document states the privacy posture of Penumbra / this
> project (public name TBD) as it prepares for an open-source release. The engine is the published
> artifact; an operator's private configuration is not. Where a separation is structural, this says
> so; where it is an operator's responsibility, this says that too.
>
> **THIS IS NOT LEGAL ADVICE.** It describes how the engine handles data, not whether any specific
> deployment satisfies any specific privacy regime. An operator who self-hosts Penumbra is responsible
> for their own compliance in their own jurisdiction.

---

## 1. No telemetry, no analytics

Penumbra sends **nothing home**. It transmits no usage data, no metrics, and no crash reports to the
authors or to any remote of this project. It creates **no identifier and no fingerprint** for an
operator or a caller. There is no phone-home, no opt-out to configure, because there is nothing to
opt out of: the only outbound traffic the engine makes is the retrieval traffic to the sources an
adapter reaches (see §4).

## 2. Self-hosted

Penumbra runs on the **operator's own machine** (the server side). The operator controls all of it:
the host, the process, the cache, the logs, and the data. There is no hosted service in the loop and
no account with this project. Whatever Penumbra stores, it stores on the operator's host, under the
operator's control.

## 3. Credentials

API keys and login sessions stay on the host. Specifically:

- API keys and credentials live under `~/.penumbra/credentials` on the operator's machine.
- Walled-source login state lives in the operator's own CDP Chrome profiles on the host.

These **never enter the git repository, never enter any release bundle, and are never transmitted to
any third party.** They are the operator's own secrets on the operator's own machine; the engine reads
them locally to act on the operator's behalf and does not export them.

## 4. Data flow

Penumbra reaches in two ways, and the results go to one place:

- It fetches from **public and curated sources** over ordinary requests.
- For walled sources, it uses the **operator's own logged-in browser session on the host** (the
  operator's CDP Chrome).

Results are returned **only to the calling agent** over MCP. Apart from the outbound retrieval to the
sources themselves, **data does not leave the host.** There is no intermediate collection point.

## 5. UNWALL: the conservative line

Penumbra's wall-crossing (UNWALL) is bounded to **credentialed access the operator is themselves
entitled to**. The engine blesses the operator using their own credentials for what those credentials
already grant them. It does **not** host, broker, or authenticate any access that bypasses a control
the operator is not entitled to be on the far side of. (The full legal posture is in
[SECURITY.md](../.github/SECURITY.md).)

## 6. Open source

Penumbra is released under **Apache-2.0**. What ships is the **general engine plus general adapters**.
An operator's **private configuration is not published**: credentials, any personal data, and
operator-specific or targeted source profiles stay with the operator and out of the release.

## 7. Third-party source Terms of Service

Penumbra accesses third-party sites. An operator is responsible for complying, in their own
jurisdiction, with each accessed site's Terms of Service. This project publishes an engine; it does
not adjudicate whether a given operator's access to a given source is permitted where that operator
runs.

## 8. Contact / audit

Questions, audit requests, or privacy concerns: open an issue (link TBD at release).

---

*See also [SECURITY.md](../.github/SECURITY.md) (the security + legal posture) and [NOTICE](../NOTICE).*
