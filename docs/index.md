English | [Français](index.fr.md)

# Synaptane documentation

Synaptane is a team second brain stored in Git: Markdown spaces backed by Git repositories,
used from your AI assistant over MCP, and tidied every night by an AI agent. Start with
[Concepts](concepts.md) for the vocabulary and the big picture.

## Users

- [Connect your AI assistant (MCP)](guide/mcp.md): Claude, ChatGPT, Claude Code; the tools,
  prompts and the inbox.
- [Read, write and search the wiki](guide/wiki.md): reading, editing, verifying pages, wiki
  health, search.

## Administrators

- [Install and run Synaptane](admin/installation.md): Docker Compose or from source,
  upgrades, configuration reference.
- [Git repositories](admin/git.md): link a space to its repository, sync, conflicts.
- [AI and tidy-up](admin/ai.md): providers and subscriptions, nightly tidy-up, purpose and
  rules, routines, cost and budget, search by meaning.
- [Single sign-on (SSO)](admin/sso.md): GitLab, GitHub, Google, OIDC.
- [Webhooks](admin/webhooks.md): outgoing events, format, delivery.
- [Backup and restore](admin/backup.md): local backup, encrypted offsite copies, restore
  drills.
- [Server scripts](../deploy/host/README.md): upgrade, nightly backup and nginx scripts for a
  Compose host.

## Reference

- [Concepts](concepts.md): vision, vocabulary, roles, inbox, tidy-up, purpose, routines,
  design decisions.
- [Repository format](reference/repository-format.md): `content/`, `.synaptane/`, front
  matter, inbox notes.
- [REST API](reference/api.md): every endpoint of `/api/v1`.
- [Command line](reference/cli.md): the `synaptane` command.

## Development

- [Development guide](dev/development.md): set up, run the app locally, tests, code
  layout.
- [Evaluation bench](dev/eval.md): measure the tidy-up and search before changing a prompt,
  a model or the ranking.
- [Contributing](../CONTRIBUTING.md), [changelog](../CHANGELOG.md),
  [security policy](../SECURITY.md).
