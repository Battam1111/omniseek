# Security policy

## Reporting a vulnerability

Please report security issues privately via GitHub's "Report a vulnerability" (Security Advisories)
on this repository, rather than opening a public issue. Include a description, affected version /
commit, and a reproduction if you have one. We aim to acknowledge reports promptly and will
coordinate a fix and disclosure timeline with you.

## Threat model + posture

OmniSeek is a **self-hosted** retrieval service that drives credentialed tools on the operator's
behalf. It is designed to be run by its operator on a trusted host, bound to loopback. The security
boundaries it enforces:

- **Transport.** The HTTP transport binds `127.0.0.1` by default and refuses to start without a
  bearer token (constant-time compared). It enables DNS-rebinding protection on loopback and warns
  loudly when bound to a non-loopback address. Do not expose it to the internet without a reverse
  proxy + firewall.
- **SSRF.** Every outbound fetch goes through a shared guard (`_netguard`) that validates the URL
  shape (scheme / port / no userinfo), resolves the host, and pins + re-validates the IP on every
  redirect hop, rejecting private, loopback, and link-local ranges. Operators can widen the allowed
  CIDR set deliberately with `OMNISEEK_ALLOW_NETS` (needed for some split-tunnel setups).
- **Local-file sandbox.** `omniseek_read_document` resolves and contains every path to an allowlisted
  inbox (`~/omniseek-inbox` plus `OMNISEEK_DOC_ROOTS`); a path that escapes is refused.
- **Untrusted retrieved content.** Documents and snippets omniseek returns are external data, never
  instructions. A consuming agent must treat them as data and never let a fetched page redirect its
  task or disclose secrets (the MCP instructions state this to the agent).
- **Secrets.** Credentials, tokens, and cookies live under `~/.omniseek` (outside the repo), never in
  version control.

## Operator responsibility

OmniSeek fetches as the operator's own agent. The operator is responsible for using it within the law
and each site's terms of service in their jurisdiction. Login-walled sources are **off by default**,
on a fresh clone as well as a configured one; sources that defeat an access control (the
circumvention tier) are **absent from the shipped catalog**, and the mirror script refuses to publish
one that declares that tier. Enabling a walled source is a deliberate operator choice, and the legal
judgment belongs to the operator.

The full statement, with each claim tied to the code that enforces it, is in
**[LEGAL-POSTURE.md](../docs/LEGAL-POSTURE.md)**. See also [NOTICE](../NOTICE) and
[configuration](../docs/configuration.md).
