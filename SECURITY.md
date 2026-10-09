# Security policy

[Version française plus bas](#français).

## Reporting a vulnerability

**Do not open a public issue, discussion or pull request for a security problem.**

Report it privately through GitHub Private Vulnerability Reporting:

**<https://github.com/synaptane/synaptane/security/advisories/new>**

This is the only channel. The report is visible only to you and the maintainers, and the
discussion, the fix and the advisory are prepared there.

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
| Latest 1.0.x release | Yes |
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

## Français

**N'ouvrez jamais d'issue, de discussion ni de pull request publique pour un problème de
sécurité.** Signalez-le en privé via le formulaire GitHub de signalement privé de
vulnérabilité : <https://github.com/synaptane/synaptane/security/advisories/new>. C'est le
seul canal ; vous pouvez écrire en français.

Indiquez la sortie de `synaptane --version`, le mode d'installation, les étapes pour
reproduire et l'impact (sans secret ni donnée personnelle réelle). Versions prises en charge :
la dernière version 1.0.x et la branche `main`.

Le projet est maintenu au mieux par une seule personne. Délais visés : accusé de réception
sous 7 jours, première évaluation sous 14 jours, correctif sous 90 jours, puis publication
d'un avis de sécurité GitHub (avec un identifiant CVE si justifié). Si vous le souhaitez, vous
êtes crédité dans l'avis.

Le périmètre comprend l'application, l'image Docker et les fichiers Compose de ce dépôt, ainsi
que l'injection de prompt qui amène les outils de Synaptane à une action non voulue. Hors
périmètre : les instances tierces mal configurées et les failles des dépendances, à signaler à
leurs auteurs.
