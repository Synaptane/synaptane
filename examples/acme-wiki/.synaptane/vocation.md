# Space purpose

## What this space is for, and who uses it

The technical memory of Acme's IT team: what you need to know to run, troubleshoot and evolve
our servers, network and tools. Read by the team, by the person on call, and by the AI
assistants that help with diagnosis and onboarding.

## What goes here

- One page per server, service or network component: role, location, access, dependencies.
- Procedures (deployment, incident, restore, rotation, onboarding and offboarding).
- Architecture and policy decisions, with their date and reason.
- Projects in progress and their status.
- Hosting contracts and client support rules.

## What does not go here

- No password, token or private key: only where to find it (the password vault).
- Day-to-day tickets with no lasting lesson: leave them in the inbox with the reason.
- Public documentation of a piece of software: a link is enough.
- Questions without a clear subject: leave them in the inbox and ask who wrote them.

## Folder layout

- `infra/`: servers, virtualisation, storage, backups, certificates, monitoring, DNS.
- `network/`: Wi-Fi, firewall, VLANs, topology.
- `tools/`: one page per tool the team uses or runs.
- `team/`: members, meetings, on-call, onboarding, time off.
- `procedures/`: one page per runbook, with numbered steps.
- `projects/`: one page per project.
- `decisions/`: one page per decision, `YYYY-MM-topic.md`, listed in `decisions/README.md`.
- `clients/`: hosting contracts and client support.

Each folder has a `README.md` that lists its pages: keep it up to date.

## Conventions

- Language: English; tool names as they are (vzdump, WireGuard, GitLab CI).
- Tone: factual and precise, short sentences.
- File names: lower case and hyphens.
- Procedure: prerequisites, numbered steps, commands in code blocks, how to check it worked.
- When a fact changes, keep the previous value with its date ("Previously: …").

## What matters most

- Accuracy: a wrong command or address is costly during an incident. When two sources
  disagree, leave the item in the inbox with the question.
- Freshness: date anything likely to change (versions, capacities, IP addresses).
- Verified pages: procedures and security pages have an owner and are reviewed every year;
  read a page marked "to review" carefully before relying on it during an incident.
- Confidentiality: never a secret in a page.

## Vocabulary

- Bastion: the single SSH entry point, `bastion.acme.internal`.
- DRP: disaster recovery plan.
- On-call: the person reachable outside business hours, one week out of four.
- P1 to P4: ticket and incident priorities (see `tools/tickets.md`).
