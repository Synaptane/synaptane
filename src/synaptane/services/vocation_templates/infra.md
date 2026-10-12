# Space purpose

## What this space is for, and for whom

The technical memory of the infrastructure and operations team: what you need to know to run, troubleshoot and evolve our servers, networks and services. Read by the team, the people on call and the AI assistants that help with diagnosis.

## What goes here

- One sheet per machine, service or network device: role, location, access, dependencies.
- Procedures (installation, upgrade, backup, restore, failover).
- Incidents and their post-mortems: timeline, cause, fix, follow-up actions.
- Architecture decisions, with their date and their reason.
- Contracts, suppliers and due dates (certificates, domains, licenses).

## What does not go here

- No password, token or private key: only where to find them (vault).
- Day-to-day tickets without a lasting lesson: leave in the inbox with the reason.
- A piece of software's public documentation: a link is enough.

## Folder organization

- `servers/`: one sheet per machine (`machine-name.md`).
- `services/`: one sheet per service (database, mail, monitoring…).
- `network/`: addressing plan, firewall, VPN, DNS.
- `procedures/`: one page per procedure, titled with a verb ("Restore a backup").
- `incidents/`: one post-mortem per incident, `YYYY-MM-DD-subject.md`.
- `decisions/`: one page per decision, `YYYY-MM-DD-subject.md`.
- `suppliers/`: contracts, contacts, due dates.

## Conventions

- Language: English; usual technical terms as they are (load balancer, failover).
- Tone: factual and precise, short sentences.
- File names: lowercase with hyphens, no accents.
- Sheet: role, access, useful configuration, monitoring, backup, links. Procedure: prerequisites, numbered steps, commands in code blocks, verification, rollback.

## What matters most

- Accuracy: a wrong command or address is costly during an incident. In case of doubt or contradiction, leave the item in the inbox with the question.
- Freshness: date the information likely to change (versions, IP addresses).
- Verified pages: procedures and service sheets have an owner and are verified every 6 months ("Mark as verified"); a page due for review is read again before relying on it during an incident.
- Confidentiality: never a secret in a page.

## Vocabulary

- DRP: disaster recovery plan.
- On call: the person reachable outside business hours.
