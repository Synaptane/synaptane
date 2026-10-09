# Acme IT wiki: demo data

A small, entirely fictional knowledge base to try Synaptane in two minutes: the wiki of the IT
team of "Acme", a made-up company. People, servers, vendors and dates are invented; addresses
only use reserved domains (`acme.example`, `*.acme.internal`).

This folder has the layout of a space repository:

- `content/`: 50 Markdown pages in eight folders (infrastructure, network, tools, team,
  procedures, projects, decisions, clients), with an index per folder, a glossary, a FAQ,
  tables, code blocks, relative links and `[[wiki links]]`.
- `.synaptane/vocation.md`: the space purpose, given to the tidy-up agent and to AI
  assistants.
- `.synaptane/inbox/`: six notes waiting to be tidied, one per typical case: a fact to add
  to an existing page (NAS capacity), a duplicate (VM backups), a contradiction (the team
  meeting moves), an ambiguous question to leave in the inbox, a note with a CSV attachment
  and a placement hint, and a new topic that deserves its own page.

It also shows trust and health signals: four verified pages with an owner and a review
period, one page whose review is overdue (`network/firewall.md`), and one deliberately broken
link (in `team/onboarding.md`) for the Health tab.

## Load it

On an initialised and migrated instance, with local repositories allowed:

```sh
SYNAPTANE_ALLOW_LOCAL_REPOS=true synaptane demo
```

The command creates a `demo` super-admin (password printed once), the `acme` organization and
the `acme/it` space, backed by a local Git repository filled from this folder, then indexes it.
Running it again changes nothing. With Docker, see "Demo data" in
[the installation guide](../../docs/admin/installation.md).

The verification dates are fixed: as time passes, more pages will show as "to review".
