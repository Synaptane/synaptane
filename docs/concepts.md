English | [Français](concepts.fr.md)

# Concepts

This page explains what Synaptane is, the words used throughout the documentation, and how
the pieces fit together. For the files inside a space's repository, see
[Repository format](reference/repository-format.md).

## Vision

Synaptane is a **team second brain stored in Git**.

- Each **space** is a remote Git repository (GitHub, GitLab, Gitea or any Git server). Its
  `content/` folder reads like a wiki.
- People use it first from their AI assistant (Claude, ChatGPT…) over **MCP**: they search,
  read, and drop what they learn into a space's **inbox**.
- Every night, an AI agent **tidies the inbox** into the `content/` tree, creating folders
  when needed. It commits the result directly: Git is the history and the undo button.
- Search is plain text by default, like grep. It becomes hybrid (text and embeddings) on the
  spaces where an embedding model is configured.

A typical day: an administrator creates an organization and a space linked to a GitLab
repository. A member connects Synaptane to Claude.ai and, from a conversation, drops a note
into the inbox. That night, the note is filed into `content/` by a commit pushed to GitLab.
The next morning, the page shows in the wiki and the assistant finds it through search.

## Vocabulary

| Term | Meaning |
|---|---|
| Instance | One deployment of Synaptane, with its accounts. |
| Organization | A group of spaces with its members and administrators (a company, a team, a personal use…). |
| Space | A remote Git repository and one of its branches. It belongs to a single organization. Named `organization/space`, for example `acme/infra`. |
| Content | The files under `content/`: what the wiki, search and MCP show. |
| Inbox | Items waiting to be filed: notes under `.synaptane/inbox/`, plus pages changed directly. |
| Tidy-up | The AI agent's daily pass that empties the inbox into `content/`. |
| Purpose | What a space is for, what goes in it and how it is organized (`.synaptane/vocation.md`). Read by the tidy-up, routines and assistants. |
| Tidy-up rules | Precise filing rules for the agent (`.synaptane/prompts/tidy.md`). |
| Routine | A scheduled agent task on a space (weekly consistency check, weekly digest…) that writes a report and can drop items into the inbox. |
| AI provider | Credentials for a language model or an embedding model, set per organization: an API key, or a Claude or ChatGPT subscription. |

## Roles and permissions

| Role | Scope | Can |
|---|---|---|
| Super admin | Instance | Create and disable accounts and organizations, appoint organization administrators, configure single sign-on, see the technical status. Reads all content of every organization in the web UI and the REST API. |
| Organization administrator | Organization | Create and configure spaces (repository, Git credentials, tidy-up time, embeddings, purpose, routines), manage members and roles, AI providers, webhooks. Has contributor rights on every space of the organization. |
| Contributor | Space | Read, search, drop notes into the inbox, change `content/` directly (web, MCP, REST), start a tidy-up by hand. |
| Reader | Space | Read and search. A reader does not drop notes into the inbox. |

- An account can hold roles in several spaces of several organizations.
- A personal token or an OAuth connection never has more rights than its owner. It can be
  limited to reading and to some spaces; a limited token administers nothing.
- Through MCP, a super admin only reaches the spaces they are actually a member of (or whose
  organization they administer): their assistant does not see the whole instance.

**Sign-in.** Local accounts (password) and single sign-on (GitLab, including self-hosted,
GitHub, Google, or any OIDC provider) coexist. Single sign-on grants no rights by itself:
allowed email domains restrict who can sign up, but they still grant no membership, and an
administrator adds each member. See [Single sign-on](admin/sso.md).

## Git, the source of truth

- The PostgreSQL database holds nothing that cannot be rebuilt from the repositories, apart
  from accounts, permissions, secrets and logs. `synaptane reindex` rebuilds the search index
  from Git.
- Each space has a local clone. Every write is a readable commit: the author is the person
  who acted (or the tidy-up agent, `Synaptane tidy-up`), and the message says what was done
  (`Inbox: …`, `Tidy-up: …`, `Synaptane: initialize space`…). The commit is pushed right
  away. Histories written by older versions keep their French author names and subjects
  (`Synaptane – rangement`, `Rangement : …`), still recognized.
- Changes made elsewhere (a push, an edit on the Git host) are fetched through an incoming
  webhook, with a fallback poll every 5 minutes, then indexed.
- When histories diverge, Synaptane rebases or merges. On a conflict, the space is marked
  as such and an administrator chooses, file by file, between the remote version and the
  local one. Synaptane never force-pushes and never lets an AI resolve a conflict.

Details: [Git repositories](admin/git.md).

## Inbox

**Notes.** From MCP, the REST API or the web UI, a contributor drops a note: a title, Markdown
content, optional attachments, where it comes from and an optional filing suggestion.
Synaptane commits and pushes the note right away under `.synaptane/inbox/`. A note is only
refused for its size, for permissions, or because it seems to contain a secret (password, API
key, private key, credentials in a URL); the person can confirm a false alarm.

**Changes.** A contributor can also change `content/` directly (web, REST, MCP), and anyone
with access to the repository can push to it. Each changed page shows in the inbox as a
**change** item, computed from the Git history (no file is written): the page, its authors,
its commits and the combined diff since the last tidy-up. Whitespace-only changes and changes
to the verification fields of a page are ignored. Synaptane's own commits (tidy-up, merges)
never produce change items.

## Tidy-up

Every day at the time set for the space (in its time zone), or on demand, if the inbox has
items:

1. The agent receives the space's description and purpose, the default instructions and the
   space's own rules, the list of items and an overview of the `content/` tree.
2. It explores the wiki (tree, read, search, backlinks, related pages, history) and acts:
   create, complete, rewrite, move, rename, merge or delete pages and folders, including
   pages that did not come from the inbox. A move updates the links that pointed to the page.
3. Each item is closed: a note is removed from the inbox once filed; an item the agent cannot
   file stays, with its reason, and is tried again on the next pass. For a change item, the
   agent checks links, location, consistency with neighboring pages and duplicates.
4. The pass produces **one commit**, pushed right away. Its report (items, pages touched, cost)
   is shown in the web UI and can trigger a webhook. A pass can be undone (a revert of its
   commit).

**Language.** The agent's instructions are in English. It writes in the language of the
space's existing pages, or the one its purpose sets; in a space with no pages yet, in the
language of the note it files. A page it completes keeps its language. Routines follow the
same rule.

**Safeguards.** Each pass has caps on model calls, tokens, tool calls and deletions; two
passes never run at the same time on one space. An inbox too large for one pass (25 items)
is emptied by chained passes. Each pass has an estimated cost, and an optional monthly budget
limits spending. Content dropped into the inbox is always data, never instructions for the
agent.

**Providers.** Anthropic, OpenAI, Mistral, Ollama and any OpenAI-compatible endpoint (API
key), plus two subscriptions: Claude (Claude Code) and ChatGPT (Codex). The model is free to
choose; it is set per organization and can be overridden per space.

Details: [AI and tidy-up](admin/ai.md).

## Purpose and tidy-up rules

A team's infrastructure wiki, a marketing team's notes and a family's papers are not filed
the same way. Each space can describe, in two Markdown files versioned in its repository:

- its **purpose** (`.synaptane/vocation.md`): what the space is for and for whom, what goes in
  it and what does not, how folders are organized, conventions, what matters most,
  vocabulary. The tidy-up, routines and AI assistants read it.
- its **tidy-up rules** (`.synaptane/prompts/tidy.md`): precise rules for the agent only,
  such as "one page per supplier in `suppliers/`".

Templates are provided (`infra`, `marketing`, `product`, `client`, `personal`, `family`,
`job-search`, `blank`), in English and in French: each person is offered them in the
language of their account. Neither file lifts the safeguards.

## Routines

A routine is a set of instructions, a schedule (daily, weekly or monthly, at a local time of
the space), a scope (some folders, or the whole space) and outputs: a report page rewritten
on each run, items dropped into the inbox, an email, a `routine.done` webhook. The routine
agent reads the wiki and writes only to its outputs: actual fixes go through the inbox and
the nightly tidy-up, with its safeguards. Two templates are provided: a weekly **consistency
check** (contradictions, outdated information, duplicates) and a weekly **digest** of what
the team learned and decided, in English and in French (the language of the person who
creates the routine).

Organization administrators create, change, run and delete routines; contributors of the
space see them and their log. The nightly tidy-up is not a routine.

## Search

- **Text**: ripgrep over `content/` and over the text extracted from PDF, Office documents
  and images (kept outside the repository). Case-insensitive by default, regular expressions
  as an option. Results are ranked by relevance: matches in the title and path, number of
  matches, freshness.
- **By meaning (optional, per space)**: an organization administrator chooses an embedding
  provider and model. Pages are split into Markdown sections and indexed in pgvector on each
  change. A query on such a space combines text and vectors; a search across several spaces
  merges results without penalizing spaces without embeddings.
- Permissions are always applied before searching.

See [Search](guide/wiki.md#search).

## Interfaces

- **MCP**: a single `/mcp` endpoint (Streamable HTTP), OAuth 2.1 with PKCE and dynamic client
  registration, or personal tokens. A connection reaches the spaces chosen at consent time:
  all the person's spaces, including future ones, or only some (for a person in several
  organizations, only one organization's spaces are ticked by default). Administration is
  not exposed over MCP. See [Connect your AI assistant](guide/mcp.md).
- **Web UI**: wiki (tree, pages, links, PDF viewer, history and diff), search, Markdown
  editing, inbox, tidy-up log, routines, tokens and connections, administration, Git status
  and conflict resolution. See [Read, write and search the wiki](guide/wiki.md).
- **REST API** (`/api/v1`): the same capabilities as MCP plus all administration, with a
  generated OpenAPI schema. See [REST API](reference/api.md).
- **Outgoing webhooks**: per organization, signed with HMAC, retried on failure. See
  [Webhooks](admin/webhooks.md).

## Design decisions

- **Git is the source of truth.** Content lives in plain repositories that people already
  know how to host, review and back up; the database can be rebuilt from them.
- **One space, one repository branch, one organization.** Permissions and history stay
  simple; an organization groups spaces with their administrators.
- **Drop first, file later.** Assistants drop notes into an inbox instead of editing pages
  directly; an agent with the whole picture files them once a day, in one reviewable commit.
- **Direct commits, no review queue.** The tidy-up commits without waiting for approval. Git
  history and one-click revert are the safety net, together with caps and alerts.
- **No AI in conflict resolution.** Conflicts between Synaptane and the remote are resolved
  by a person, file by file; Synaptane never force-pushes.
- **Any provider.** The agent runs on any supported model, with an API key or a subscription,
  without an allowlist of models.
- **Grep first.** Plain-text search works everywhere with no setup; search by meaning is an
  option per space.
