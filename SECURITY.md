# Security Policy

## Supported versions

Only the latest released version on `main` receives security fixes.

## Reporting a vulnerability

Please report vulnerabilities **privately** via GitHub's
["Report a vulnerability"](../../security/advisories/new) (Security Advisories)
on this repository — not as public issues.

You can expect an acknowledgement within a few days. Please include a minimal
reproduction (store layout + call sequence) where possible.

## Threat model notes

The toolkit is designed to run **locally**, serving context from directories the
operator already controls. Keep these properties in mind when deploying:

- **The store is trusted input.** Rules and memories are injected into an LLM
  agent's context. Anyone who can write to your store directories can influence
  the agent (prompt-injection by design surface). Protect the store like you
  protect your shell profile: repo permissions, code review for shared tiers.
- **No network access.** The engine reads local files only; the MCP server
  speaks stdio to its client. It performs no outbound calls. A change that adds
  network I/O is a security-relevant change and needs explicit review.
- **Path handling.** Store roots are configured by the operator; the engine
  does not follow paths outside its configured roots. Report any traversal
  you can construct.
- **No secrets in stores.** Memories/rules are plain text on disk and travel
  into model context. Do not put credentials in them; the toolkit does not
  (and cannot) redact.
