English | [Français](SECURITY.fr.md)

# Security policy

## Reporting a vulnerability

**Do not open a public issue, discussion or pull request for a security problem.**

Report it privately through GitHub Private Vulnerability Reporting:

**<https://github.com/synaptane/synaptane/security/advisories/new>**

This is the only channel. The report is visible only to you and the maintainers, and the
discussion, the fix and the advisory are prepared there. You may write in English or in
French.

Reports under the [Code of Conduct](CODE_OF_CONDUCT.md) go through the same private form:
choose "Report a vulnerability" and describe the conduct issue. As a fallback, use GitHub's
abuse report: <https://github.com/contact/report-abuse>.

## What to include

- The version: output of `synaptane --version` (it includes the commit).
- How Synaptane is installed: Docker Compose behind the host's own reverse proxy, Docker
  Compose with the bundled proxy profile, from source, or other.
- The steps to reproduce, ideally from a fresh install.
- The impact: what an attacker can read, change or run, and with which level of access
  (anonymous, member, space or organization administrator, instance administrator, MCP
  client, AI agent).
- Any proof of concept, logs or screenshots. Remove secrets and real personal data.

## Supported versions

| Version | Supported |
|---|---|
| Latest 1.0.x release, including release candidates | Yes |
| `main` branch | Yes |
| Anything older | No |

Fixes are released in a new patch version. Please update before reporting, if you can.

## What happens next

Synaptane is maintained by one person, on a best-effort basis. These are targets, not
guarantees:

- **Acknowledgement** within 7 days.
- **Initial assessment** (confirmed or not, severity) within 14 days.
- **Fix** within 90 days of the report, sooner for severe issues.
- **Disclosure** through a GitHub Security Advisory, with a CVE identifier when the issue
  warrants one, once a fixed version is available. We agree on the timing with you.
- **Credit**: you are named in the advisory if you wish, under the name you choose.

Please give us a reasonable time to fix the issue before any public disclosure.

## Scope

In scope:

- The Synaptane application in this repository: web interface, REST API, MCP server,
  background worker, command-line tool, AI tidy-up.
- The Docker image built from this repository (`ghcr.io/synaptane/synaptane`) and the Compose
  files provided here.
- **Prompt injection** that leads Synaptane's own tools to perform an action the user or the
  administrator did not intend: for example, content in a page or in the inbox that makes the
  AI agent read, write, move or delete data outside what it should, or leak data to another
  space or organization.

Out of scope:

- Third-party instances that are misconfigured (for example, exposed without HTTPS, with
  default passwords, or with permissions granted too widely by their administrators).
- Vulnerabilities in upstream dependencies (Python packages, PostgreSQL, Git, AI providers,
  base image): please report them to their authors. If Synaptane uses a dependency in an
  unsafe way, that is in scope.
- An AI model producing a poor or wrong answer, with no security impact.
- Denial of service that requires sending a very large volume of traffic.

