# Changelog

All notable changes to Synaptane are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Before each upgrade, back up your instance (see `docs/admin/backup.md`) and read the section
of the target version.

## [Unreleased]

### Added
- **Create the space's repository from Synaptane**. In **New space**, "Create the repository
  for me on GitHub or GitLab" creates a private, empty repository on github.com, gitlab.com or a
  self-hosted GitLab (HTTPS), for the token's account or an organization, group or subgroup,
  named after the space's identifier unless another name is given; the first sync then prepares
  it as for any space. The admin's token, which must be able to create repositories (GitHub:
  fine-grained token with the Administration permission; GitLab: the `api` scope), serves this
  creation only and is never saved, logged or returned. Synaptane keeps an access limited to the
  new repository: an SSH deploy key with write access on GitHub (github.com's SSH keys read from
  its API), a project access token (`read_repository`, `write_repository`, Maintainer) on GitLab.
  Every refusal is explained (token refused, missing rights, owner not found, name taken,
  provider unreachable or limiting requests) and creates nothing; a repository created whose
  access cannot be set up (on GitLab.com Free, project access tokens are not available) is
  still connected to the space, with a message giving its address, and is never deleted. A
  self-hosted GitLab must be at a public address, checked as for webhooks, except for the super
  admin. Audit log: "Git repository created by Synaptane" (`space.repository_created`). REST:
  `create_repository` in `POST /organizations/{org}/spaces`, with a `repository` block in the
  answer. No migration. See `docs/admin/git.md`.

## [1.12.0] - 2026-11-08

Give the whole team access to every space at once, keep a few spaces apart, see who changed what
in the audit log, and have the costs of OpenAI, Gemini, Mistral and DeepSeek estimated without
entering a price. Upgrading from 1.11.0: two migrations (`0029`, `0030`), applied at startup;
existing organizations get "no access" as default role and no space is restricted, so nobody's
access changes. Back up first: going back to 1.11.0 then means restoring that backup. With a
monthly budget, a model whose public price is not known (it used to borrow a close model's price)
now needs its prices entered on the AI page.

### Added
- **Default role in the spaces**. In the organization's **Settings**, "Default role in the
  spaces" gives every member a role on all the spaces of the organization, present and future,
  without adding them one by one: no access (the default), reader or contributor. A member's
  effective role is the higher of the role given in a space and the default role;
  organization admins keep contributing everywhere. It applies wherever access is checked:
  web UI, search (semantic too), REST API, MCP (`list_spaces`, search across spaces, writing
  tools), routine emails, freshness reminders. A token or a connection limited to some spaces,
  to reading or to the inbox stays limited, and nobody reaches the spaces of an organization
  they do not belong to. REST: `default_space_role` in `GET` and `PATCH /organizations/{org}`.
  One migration (`0029`), applied at startup: existing organizations get "no access" and no
  space is restricted, so nobody's access changes.
- **Restricted spaces**. In a space's **Settings → Members**, "Restricted space" keeps it to
  the members added to it (and the organization admins), whatever the default role: for human
  resources or management. A space can also be created restricted (**New space**, or
  `"restricted": true` in `POST /organizations/{org}/spaces`), so it is never open to everyone,
  not even right after its creation. REST: `restricted` in `GET` and `PATCH …/spaces/{space}`,
  and in `GET /spaces`. A restricted space shows a badge in the organization's list of spaces.
  Changes of the default role (`org.default_space_role`) and of the restriction
  (`space.restricted`) are written to the audit log.
- **Audit log**. Who changed a role, a token or a setting, and when. An organization's admins
  read its log in its **Audit log** tab: members and roles, invitations, settings of the
  organization and of its spaces, alerts. Super admins read the whole instance in
  **Administration > Audit log**, accounts, personal tokens, connected applications, single
  sign-on and email sending included. Filters by type and period (UTC days), 50 lines per page, CSV
  export of the selection (its 10,000 newest lines, said in the file when it stops there). Each line shows only the known details of its action, never a key, a token
  or a password, and of a webhook only its host. One migration (`0030`) replaces the index of
  the audit events on the organization with one on the organization and the date, applied at
  startup.
- **Automatic cost estimates for more providers**. Leave the price empty on the organization's
  AI page: the main models of OpenAI, Google Gemini, Mistral and DeepSeek are now counted at
  their public price, like Claude models, and the page shows the price applied, its cache price
  and, where there is one, its long-prompt price. Gemini and DeepSeek go through an
  OpenAI-compatible API; a model is recognized behind a router (`openai/gpt-5`,
  `models/gemini-2.5-pro`) or as a dated snapshot. The price you enter still comes first. More
  embedding models are priced too (`gemini-embedding-2`, `text-embedding-ada-002`,
  `codestral-embed`, `voyage-3`, `voyage-context-4`…), and more Claude models
  (`claude-haiku-5-5`, `claude-opus-4-8`, `claude-sonnet-4-6`…).

### Changed
- **Members of a space**. The page now says whether the organization's default role applies,
  then lists the members added to the space (with a note when they have more through the
  organization), then those who access it through the organization: by default, or as its
  administrators. `GET …/spaces/{space}/members` still lists only the members added to it.
  With a default role, the add form warns that someone who is not a member of the
  organization yet becomes one, and so gets the default role on every space that is not
  restricted; the confirmation says so again. Removing someone from a space that is not
  restricted says that they keep the default role.
- **Invitations**. With a default role, the form offers it as a floor in each space that is
  not restricted ("Reader (by default)") and only the roles above; the invitation page lists
  the access actually given, the spaces opened by the default role included.
- **Routine emails "to the members"** also reach those who access the space through the
  default role.
- **More precise audit events**. Granting or removing super admin rights, and disabling or
  re-enabling an account, are now events of their own; a profile change says which fields
  changed. The AI and semantic search settings say whether the API key was replaced or removed
  and whether the monthly budget changed; a space's and its tidy-up's settings say which fields
  changed; a space's purpose, tidy-up rules and assistant prompts say which file and whether it
  was removed; an invitation keeps its role; a connected application and a personal token, their
  name and restrictions. The keys themselves are never recorded.
- **Costs follow each provider's prices more closely**: the cache is billed as the provider
  bills it (cache writes cost 1.25 times the input with Anthropic and OpenAI's GPT-5.6 and
  later, nothing extra elsewhere), requests with a long prompt are counted at the higher price
  where there is one (OpenAI over 272,000 tokens, Gemini Pro over 200,000, Claude Haiku 5.5
  over 100,000), and announced price changes apply on their date. Cache reads of
  `claude-sonnet-5-5` now count for a twentieth of the input price, as Anthropic bills them,
  instead of a tenth.
- **A model with no known public price no longer borrows one**: `claude-opus-5-7`, say, used to
  take the price of `claude-opus-5`. With a monthly budget, such a model now needs its prices
  entered, as any unknown model; a run already set up with one is counted at the highest known
  price, and its log says so. A known model keeps its price whatever the provider that calls it
  (a Claude model through the OpenAI provider at a custom address, for instance), by its exact
  name.
- **Ollama has no public price any more**: a local model named like a Claude model used to be
  counted at Claude's price; it now costs nothing, unless you enter a price. The prudent price
  (highest known price, $10 / $50) is unchanged: models with restricted access, priced higher,
  are not in the table, so their price must be entered.
- **Embeddings of an unknown model** are counted at the highest price of their provider, no
  longer at the highest price of all providers.

## [1.11.0] - 2026-11-06

The state of the instance on one page: is the worker alive, is the last backup recent, is anything
stuck? Upgrading from 1.10.0: no migration, nothing to change. Backups made through
`deploy/host/backup.sh`, and off-site copies, only show on the page from the first one made after
the upgrade.

### Added
- **Health page of the instance**. **Administration > Health** shows the state of the instance
  on one page: database, master key, worker, background tasks, last backup, off-site copy, disk,
  email sending, HTTPS; then the sync and tidy-up of each space; then the system alerts of the
  last 30 days.

### Changed
- **`synaptane_backup_last_timestamp_seconds`** now follows the last verified archive wherever it
  was written, standard output included (`deploy/host/backup.sh`), and not only `/backups`.

## [1.10.0] - 2026-11-06

Coming from Obsidian, or from any folder of Markdown files: import it in one go, it reads as it
did there, and Mermaid diagrams are drawn. A new space also shows what is left to set up. Upgrading
from 1.9.1: no migration, nothing to change.

### Added
- **Obsidian syntax when reading a page**. `![[image.png]]` shows the image (`|300` sets its
  size), `> [!warning] Title` is a callout (GitHub's `> [!WARNING]` too), `==text==` is
  highlighted, and `%%comments%%` and block ids (`^id`) are hidden. Files are not changed, so a
  vault still edited in Obsidian stays as it is.
- **Import a zip of Markdown files**. An Obsidian vault, or any folder of Markdown files zipped
  with its attachments (100 MB at most): in a folder's menu, **Import a zip of Markdown files**.
  You first see what the archive holds; then everything goes in one commit in your name, and the
  tidy-up counts it as one item. Files are kept as they are, nothing in the space is overwritten,
  hidden folders (`.obsidian`, `.trash`) are left out, and a file with a likely secret stays out
  unless you tick it.
- **Mermaid diagrams**. A ` ```mermaid ` code block is drawn when reading a page, in light and
  dark themes. The drawing happens in the browser, in a sandboxed frame with no access to
  Synaptane, so a page cannot do anything through a diagram; if it cannot be drawn, the code
  stays on the page. Mermaid is loaded only on pages that have a diagram.
- **First steps of a space**. Until its first tidy-up, the home of a space shows contributors
  what is left to do: repository, vocation, AI provider, content, tidy-up. Each step ticks itself
  when the thing is there.

## [1.9.1] - 2026-11-05

Publishes what 1.9.0 could not: its release stopped halfway, so 1.9.0 has no nginx image, no
GitHub Release and no PyPI package. The application is the same. Upgrading from 1.8.0 or 1.9.0:
no migration, nothing to change. For what is new, read 1.9.0.

### Fixed
- **Release**: the check of the published image failed on its second platform (arm64), which
  stopped the publication. Both platforms are now checked, one after the other.

## [1.9.0] - 2026-11-05

Not published in full: use 1.9.1.

Running an instance: images for ARM servers, and `/metrics` for Prometheus. Upgrading from
1.8.0: no migration, nothing to change. `/metrics` stays closed until you set `METRICS_TOKEN`
in `.env`.

### Added
- **Images for ARM servers (arm64)**. The published images `ghcr.io/synaptane/synaptane` and
  `ghcr.io/synaptane/synaptane-nginx` now exist for linux/arm64 as well as linux/amd64: on an
  ARM server (Ampere, Graviton…), Docker pulls the right one, with nothing to change. Building
  the image yourself, subscription CLIs included, also works there; the CI checks it on an
  ARM machine at every commit.
- **`/metrics` for Prometheus**. With a token of 32 characters or more (`METRICS_TOKEN` in
  `.env`, `metrics_token`), `/metrics` gives the state of the instance in the Prometheus
  format: sync state of each space, items waiting in the inbox, last tidy-up pass and whether
  it failed, the worker's queue and the wait of its oldest job, jobs abandoned, AI spending of
  the month, last local backup, requests and response times by area (web, REST API, MCP), and
  the process (CPU, memory). Without a token, `/metrics` does not exist. Examples of scrape
  configuration and alert rules: `docs/admin/installation.md`, Monitoring.

## [1.8.0] - 2026-11-04

Account security: two-factor authentication, and the sessions signed in to your account, to see
and close. Upgrading from 1.7.0: two migrations (`0027`, `0028`), applied at startup; nobody is
signed out. **Super admins who sign in with a password must turn on two-factor authentication
at their next sign-in**, before anything else: have an authenticator app at hand and keep the
recovery codes in a safe place (a password manager). To keep it optional, set
`superadmin_2fa = false` (with Docker, `SYNAPTANE_SUPERADMIN_2FA: "false"` in
`compose.override.yaml`, see "Other settings" in `docs/admin/installation.md`).

### Added
- **Two-factor authentication**. In **My account → Security**, anyone who signs in with a
  password can add a code from an authenticator app (Google Authenticator, Aegis, 1Password,
  Bitwarden…): a QR code to scan, a code to confirm it, and ten single-use recovery codes for
  a lost phone. Each sign-in, and each password reset by email, then asks for the code (five
  tries per account every 15 minutes). **Super admins who sign in with a password must turn it
  on** before anything else (`superadmin_2fa`, `false` only for a demo or test instance, as in
  `examples/compose.demo.yaml`). Single sign-on leaves the second factor to the identity
  provider. Personal tokens and assistant connections already made keep working; connecting
  a new assistant goes through the web sign-in, code included. A super admin turns it
  off for someone who lost their phone (**Administration → Accounts**), and
  `synaptane admin reset-2fa` does it for the last super admin locked out. New dependency:
  segno (BSD-3-Clause), for the QR code.
- **Sessions you can see and close**. **My account → Security** lists the browsers signed in
  to your account (browser and system, address, when signed in, last activity) and signs out
  one of them, or all but the current one: a lost phone, a shared computer. A web session now
  also ends after 7 days without use (`session_idle_days`, `0` for no limit), on top of its
  30-day length (`session_days`). Existing sessions count as used at the upgrade.

## [1.7.0] - 2026-11-03

Two new MCP tools to answer from the spaces in one call, what they say on a topic and what they
said at a past date; and wiki health that spots tables that disagree. Upgrading from 1.6.0: no
migration. Reconnect the connector (or start a new conversation) so the host sees `brief` and
`then_and_now`.

### Added
- **`brief`, what the spaces say on a topic in one call**. The new MCP tool gives, within a
  budget of characters, the best section of each relevant page (the one the search found the
  topic in), with its URL, what makes it trustworthy or not (verified, due for review,
  reported outdated, last change and whether an AI made it) and the pages it links to. An
  assistant answers a question in one call instead of a search then a read per page. The list
  of tools given to the model grows by about 700 characters.
- **`then_and_now`, what the spaces said on a topic at a past date**. "What did we say about
  the backup in September?": for each page on the topic, the new MCP tool gives its section
  now and the change since that date (what it said then, what it says since), and the pages
  that spoke of it then and no longer do (deleted, or the passage removed), with what they
  said. Each space is read as it was that day, from its Git history. The list of tools given
  to the model grows by about 700 characters.
- **Tables that disagree**. Wiki health (the Health tab, the REST API and what agent routines
  receive) now reports the same row of two aligned tables of two pages with a different value:
  the NAS with 8 TB in the inventory and 12 TB in the backup plan. Tables that disagree more
  than they agree (two companies' sheets) and dated notes are left out, to keep false alarms
  away: on a real space of 150 pages and 89 tables, none was reported.

### Fixed
- **Views in the conversation**: counts agree with their number ("7 derniers jours",
  "1 commit", "2 pièces jointes") instead of "(s)", and long paths wrap after a slash rather
  than in the middle of a name.

## [1.6.0] - 2026-11-02

A fourth view in the conversation: what the assistants wrote, undoable page by page; and edits
that say where they went wrong, with the change saved shown after each write. Upgrading from
1.5.0: no migration. Reconnect the connector (or start a new conversation) so the host sees
`assistant_runs` and its view.

### Added
- **A fourth view: what the assistants wrote**. The new MCP tool `assistant_runs` answers
  "what did Claude do yesterday?": the pages that assistants connected over MCP wrote lately,
  in one space or all, as runs (one assistant for one person in one go: consecutive changes,
  no pause over 30 minutes, nobody else's change in between). In hosts that render MCP Apps,
  each page has **See the change**, **Open** and **Undo**, and each run **Undo the whole
  run** (contributors, confirmed by a second click): the page goes back as before the run, in
  a commit under the person's name, through `undo_assistant_pages`, a tool meant for the
  view. Until now only the nightly filing could be undone page by page.

### Changed
- **Edits that say where they went wrong, and writes that show what they saved**. When the
  passage `edit_page` must replace is not in the file (a space, an accent or a number quoted
  wrong), the error quotes the closest passage with its lines, so the assistant quotes it
  again without reading the whole page; when it appears several times, the error gives the
  line of each occurrence. `edit_page`, and `write_page` when it replaces a file, now return
  the change saved as a short diff, to check it at once. The server log keeps only the first
  line of a refusal: the page's text stays out of it.

## [1.5.0] - 2026-10-31

Two more views in the conversation: the last tidy-up, undoable page by page, and the inbox,
to sort from the conversation. Upgrading from 1.4.0: no migration. Reconnect the connector
(or start a new conversation) so the host sees `last_tidy` and the views.

### Added
- **A second view: the last tidy-up**. The new MCP tool `last_tidy` shows what the last run
  of the nightly filing changed in a space (or a given run): pages created, edited, moved or
  deleted, and the notes filed. In hosts that render MCP Apps, each page has **See the
  change**, **Open** and **Undo** (contributors, confirmed by a second click), which puts it
  back as before the run, as on the run's page in Synaptane, through `undo_tidy_pages`, a
  tool meant for the view (hosts do not offer it to the assistant).

- **A third view: the inbox**. `inbox_list` (same text as before) now comes with a view in
  hosts that render MCP Apps: one card per item, with **Remove** for a note, **File it with
  the assistant** for a note the filing left aside, **Fix it with the assistant** or **Not a
  problem** (with why) for a problem an agent routine reported, **Nothing to file** for a
  page changed by hand, and **Tidy up now**. The view acts through `inbox_delete` and
  `inbox_triage`, a tool meant for the view.

### Changed
- The views share their bridge to the host and their style (`mcp/apps/bridge.js` and
  `base.css`, inlined when served). A view drawn again follows a change of the host's
  language.

## [1.4.0] - 2026-10-30

A first view in the conversation: assistants that render MCP Apps show the pages to review as
cards. Upgrading from 1.3.0: no migration. Hosts read a server's tools when the connector is
added or refreshed: reconnect it (or start a new conversation) to see `pages_to_review`.

### Added
- **A first view in the conversation (MCP Apps)**: the new MCP tool `pages_to_review` lists
  the pages due for review and those reported as outdated (text for any client), and comes
  with a view in hosts that render apps (Claude web and desktop, ChatGPT, VS Code): one card
  per page, **Still right** (verifies it), **Needs an update** (asks the assistant to propose
  one), **Open**. The view holds no data, loads nothing from elsewhere and shows titles and
  reasons as text only. The server now declares the MCP Apps extension in its `initialize`
  answer, which hosts require before rendering a view.

## [1.3.0] - 2026-10-30

Assistants (MCP): measured with a new bench, limited to the inbox if you choose, with your
team's own prompts and lighter tool descriptions. Upgrading from 1.2.0: one migration (`0026`,
an `inbox_only` column on tokens and OAuth connections; existing ones keep their level),
applied on start as usual; back up first (`deploy/host/deploy.sh` does).

### Added
- **Your team's prompts for assistants**: one Markdown file per prompt in a space's
  `.synaptane/prompts/` (written in **Settings → Assistant prompts** by the organization's
  administrators, or by a push), with arguments (`{{name}}`) and included pages
  (`{{include page.md}}`, read with the person's rights, framed as untrusted). Every
  assistant connected over MCP lists them with the built-in prompts, named
  `<org>-<space>-<name>`.
- **Connections limited to the inbox**: a third level next to "read only" and "read and
  write", on the OAuth consent screen and for personal tokens (web, and `inbox_only` in
  `POST /me/tokens`). The assistant reads, and drops, corrects or removes notes in the inbox,
  but changes no page directly: what it notes goes through the nightly tidy-up. It is only
  offered the read and inbox tools; a page write is refused with the reason. It limits what
  instructions hidden in a page could make it change.

### Changed
- Shorter MCP tool descriptions and server guidance (same rules): the tool list goes from
  about 20,800 to 17,100 characters and the guidance from 4,700 to 4,000, about 1,100 tokens
  less on every request an assistant makes. Empty defaults and `anyOf … null` are left out of
  the published schemas.
- MCP guidance: to record information, an assistant may add a short fact to the one page it
  obviously belongs to (`edit_page`, keeping what is there); otherwise it drops it with
  `inbox_add`. Measured with the new MCP bench (`./scripts/eval.sh mcp`, docs/dev/eval.md):
  Sonnet 5.5 did the former, correctly, against the previous "prefer `inbox_add`".

## [1.2.0] - 2026-10-30

Trust in the content: who wrote a page last, pages that still hold, pages reported as
outdated, false alarms that do not come back. Upgrading from 1.1.0: two migrations (`0024`
and `0025`, new tables `page_reports` and `finding_verdicts`), applied on start as usual;
back up first (`deploy/host/deploy.sh` does, and restores the backup if the upgrade fails).

### Added
- **Report a page as outdated**: anyone who reads a page can say what no longer holds (⋮
  menu). The page shows a banner with the reports, search marks it, the space's **Health**
  tab lists it, and assistants see it (`reported_outdated` in `search` and `fetch`, the reasons
  in the `read` header, framed as untrusted). A report stays open until the page is verified,
  a contributor dismisses it, or its author withdraws it; a moved page keeps its reports.
  REST: `GET`, `POST` and `DELETE …/spaces/{space}/reports/{path}`.
- **A false alarm from a routine does not come back every week**: when an item a routine
  dropped leaves the inbox (**Not a problem**, with why; deleted; or handled by the tidy-up),
  the verdict is remembered, bound to the pages concerned. The routine is given the standing
  verdicts and does not drop the problem again until one of its pages changes; an item with
  the same title is refused. REST: `DELETE …/inbox/{name}?not_a_problem=…`.
- **Last change in search results**: when, by whom, and whether an AI wrote it (the tidy-up,
  a routine or an assistant). In the web search (date, author, **AI** badge), the REST API
  (`edited`, `edited_by`, `edited_by_ai`) and the MCP tools (`edited`, `edited_by`, `ai` in
  `search` and `fetch`, `edited:` in the `read` header). The server guidance asks assistants
  to be more careful with a page last written by an AI and not verified since. Computed from
  the Git history in one walk per space, then only the new commits.
- **A page that still holds waits longer**: verified again with no change since its previous
  verification, its review interval doubles, up to two years (`reconfirmed: N` in its front
  matter, protected from AI agents like the other verification fields). A change to the page,
  or an interval chosen by hand, starts again from its interval. The verification panel says
  so, and the REST API gives `reconfirmed` (MCP: `review_due` already accounts for it).

## [1.1.0] - 2026-10-30

Keeping control of what the AI writes. Upgrading from 1.0.0 needs no migration and no
setting: the history already written is attributed (person or AI) from the first page view.

### Added
- **Restore a past version** of a file, a deleted file too: in the wiki, from its history
  (**Restore this version**, with **Compare with the current version** to see first what it
  would change), with the MCP tool `restore`, and with `POST …/spaces/{space}/restore` in the
  REST API. A new commit in the person's name: the replaced text stays in the history. A page
  keeps its current verification fields.
- MCP `read` takes `commit` to read a file as it was in a past version.
- The address of a deleted page says so and links to its history; that of a moved page gives
  its new address.
- Versions from before a rename are read and restored under the name the file had then.
- **Person or AI** in a space's activity: an **AI** badge on what the nightly tidy-up, a
  routine or an MCP assistant (named: "Alice via Claude") wrote, a **Written by AI** filter,
  and the share of the AI in the page changes of the last 30 days, by origin. MCP
  `recent_changes` and `history` give the same origin in the author. Read from the `Source:`
  and `Via:` lines of Synaptane's commits; a commit pushed from elsewhere counts as pushed
  with Git, whatever its message says.
- Notes dropped in the inbox carry the same `Source:` and `Via:` lines in their commit.
- **Undo a tidy-up run for some pages only**: **Undo some pages…** in the tidy-up log
  (contributors) lists the pages the run changed, with their diff, and puts the ticked ones
  back as they were before the run, in one commit. A page changed since is refused (restore it
  from its history); a filed note returns to the inbox once all its pages are undone. REST:
  `GET …/tidy/runs/{id}/pages` and `POST …/tidy/runs/{id}/undo-pages`.

### Security
- The name of an OAuth application or token goes on a single line in the `Via:` line of a
  commit: a line break in it could add a forged line.

## [1.0.0] - 2026-10-30

First stable release. It contains no change of behavior or data since 1.0.0-rc.2 apart from
the items below: upgrading from 1.0.0-rc.2 needs no migration and no setting.

### Changed
- **Stable status**: Synaptane is no longer presented as alpha software. Versions follow
  Semantic Versioning: within 1.x, the REST API, the MCP tools and the configuration only
  change in a compatible way, and an incompatible change waits for 2.0. Security fixes go to
  the latest 1.x release (`SECURITY.md`).
- Dependencies: anthropic 2.3.0, FastAPI 0.162.0, Alembic 1.20.1.
- Agent CLIs of the image (subscription mode): Claude Code 2.1.314 and Codex 0.166.0.
  - Claude Code now offers the model a placeholder tool to keep deferred tool loading
    ("tool search") active. Runs switch it off (`ENABLE_TOOL_SEARCH=false`), so the model
    still sees Synaptane's tools only, all of them upfront. The isolation contract
    (`synaptane check-clis`) checks it with that same setting.
  - Codex's new `plugin_discovery_prefetch` feature, which downloads the plugin catalog at
    startup, is turned off on each run. Its other new features enabled by default (Bedrock
    sign-in, MCP OAuth refresh, tool descriptions in the context, prediction forks, reasoning
    effort changes) open nothing.

## [1.0.0-rc.2] - 2026-10-25

### Changed
- Semantic search with Voyage AI: pages and queries are embedded with the prompts Voyage
  prepends for its `input_type` ("document" and "query"), which its models are tuned for.
  Vectors made without them stay compatible; `synaptane reindex` rebuilds an existing index
  with them.
- Agent CLIs of the image (subscription mode): Claude Code 2.1.303 and Codex 0.163.0. Codex's
  new `browser_annotation_api` feature is turned off on each run; its other new features
  (service tiers, access programs, interruptions, desktop voice) open nothing.
- Anthropic provider: `anthropic` 2.0.0, which calls the Claude API with version 2026-10-21
  and sends the API key in `Authorization: Bearer` instead of `X-Api-Key`. Requests to the
  Claude API itself are unchanged. **If the organization's AI goes through a gateway or proxy
  (an "API address" set for Anthropic), check that it accepts the key in `Authorization`**;
  one that only reads `X-Api-Key` now refuses the requests ("key refused"). The `anthropic`
  package is now under Apache-2.0 (MIT before; see `THIRD_PARTY_NOTICES.md`).

### Security
- Pillow 13.0.0: fixes in the image decoders (JPEG, TIFF, GIF, PDF, EPS), which open the
  images uploaded for OCR.
- opentelemetry-api 1.45.1 (pulled in by anthropic, fastapi and mcp): its `tracestate` parser
  no longer takes quadratic time on a crafted header (CVE-2026-114603). Synaptane does not
  extract trace headers itself.

### Added
- Prices of the Voyage 4 embedding models (`voyage-4-large`, `voyage-4`, `voyage-4-lite`), so
  that their indexing is counted at its real cost instead of the table's highest price.
- Semantic search: an optional **minimal similarity** per organization, next to its embedding
  model (Semantic search page, REST `meaning_min_similarity`). Set, the best page found by
  meaning must reach it instead of standing out from the median of its neighbours: in a space
  of similar pages (call transcripts, tickets), relevant questions never stood out enough and
  meaning added nothing. Empty by default: nothing changes. Cleared when the model changes,
  since its scale is the model's.

### Fixed
- A space created after its tidy-up time (02:00 by default) no longer gets a scheduled run
  right away, before its repository is even synchronized, which failed with "This space has
  not been synchronized with its repository yet". Its first run is the next night, and no
  scheduled run starts before the first synchronization.
- The failed-run alert offers to test the AI connection only when the cause may lie with the
  AI; a space not synchronized yet points to the repository status.
- A UTF-8 text file whose 8 KiB detection window cut a multi-byte character (an accented
  letter at byte 8192) was taken for a binary file: shown as a download, without its text for
  search and AI assistants. It is text again; an invalid byte still makes a file binary.

## [1.0.0-rc.1] - 2026-10-12

First public release.

### Added

#### Wiki stored in Git
- Organizations group spaces. Each space is a Git repository on any host (GitHub, GitLab,
  Gitea…), over HTTPS with a token or over SSH. Its `content/` folder is the wiki, in plain
  Markdown; PDF, Office documents and images are stored next to the pages.
- Markdown first, and all your files: PDF (built-in viewer; text layer, or OCR for scanned
  pages), Word, PowerPoint and Excel documents and their OpenDocument equivalents (`.docx`,
  `.pptx`, `.xlsx`, `.odt`, `.odp`, `.ods`: download, with the extracted text), images
  (displayed, text read by OCR), standalone HTML pages (rendered in an isolated frame, source
  one click away) and any text file (syntax highlighting) are searchable and readable by AI
  assistants. Other files are stored, versioned and downloadable.
- The old binary Office formats `.doc`, `.xls` and `.ppt` are searchable too, their text read
  in the same bounded process and shown under the download link. Word 6 to 2003 and Excel 5 to
  2003 are read by catdoc (`catdoc`, `xls2csv`, included in the Docker image); without it,
  these files are kept without text and `synaptane doctor` warns. PowerPoint 97 to 2003 is read
  by Synaptane itself (with olefile), whether the file was saved by PowerPoint or LibreOffice:
  titles, bodies, free text boxes, tables and speaker notes, slide after slide. A
  password-protected file is kept without text, its failure recorded in the extraction
  manifest.
- Two-way sync, triggered by an incoming webhook or by polling, with conflict resolution in
  the web UI. Nothing local is lost when the repository URL changes or after a force-push.
- Reading and editing in the web UI: page tree, outline, backlinks, wiki links `[[name]]`,
  history and diffs. Moving a page rewrites the links that point to it, in the same commit.
- **Health** tab per space: broken links with suggestions, orphan, empty or untitled pages,
  pages not changed for months.
- **Page verification**: owner, verification date and review cycle in the front matter
  (`owner`, `verified`, `review_every`). Badges, a list of pages due for review, and a weekly
  email to owners.
- **Related pages** under each page (by meaning, rare words in common, links, folder), with a
  one-click proposal to link two pages.

#### Inbox and AI tidy-up
- Each space has an inbox. Notes, with attachments, come from AI assistants (MCP), the web UI
  or the REST API. Pages changed directly are integrated as well.
- Every night (or a few minutes after each drop, if enabled), an AI agent files the notes into
  the wiki, updates existing pages, fixes links and commits. A run can be undone in one click.
- **Space purpose** (`.synaptane/vocation.md`) and space-specific **tidy rules**
  (`.synaptane/prompts/tidy.md`), edited in the web UI, with eight templates (infrastructure,
  marketing, product, client project, personal, family, job search, blank).
- Guardrails: at most 10 deletions per run; caps on tokens, model calls and tool calls per run
  (2 million tokens, 120 model calls and 400 tool calls for the tidy-up; 1 million, 60 and 200
  for a routine); an optional monthly budget per organization with alerts at 80 % and 100 %;
  file-by-file merge when someone edits a page during a run.
- The agent reads attachments (text, PDF, Office, OCR) and searches by words and meaning
  before writing, to complete pages rather than duplicate them.
- Run log with cost, tokens, quality metrics (broken links introduced, orphan pages created…)
  and a link to the commit. People who dropped notes get an email saying where they were
  filed.
- Offline evaluation bench for the tidy-up and search (`tests/eval/`).

#### Agent routines
- Scheduled AI agents per space (daily, weekly or monthly) that read a part of the wiki and
  write only to their outputs: a report page, inbox items, an email, the `routine.done`
  webhook.
- Two ready-made routines: **Consistency check** (contradictions, outdated information,
  duplicates) and **Weekly digest**.

#### Search
- Full-text search with ripgrep: accent and ligature folding, light English and French
  stemming, BM25-style ranking, one readable excerpt per result that links to its section.
- Text extracted from PDF, Office documents and images (Tesseract OCR) in a separate, bounded
  process.
- Optional hybrid semantic search with pgvector and reciprocal rank fusion. Embedding
  providers: Ollama, OpenAI, Google Gemini, Mistral, Voyage AI, any OpenAI-compatible API.
- Notes waiting in the inbox are shown above the results. Search palette on every page
  (Ctrl/Cmd+K or `/`).

#### MCP server for AI assistants
- Streamable HTTP server at `/mcp` with OAuth 2.1 (PKCE, dynamic client registration, token
  rotation) or personal tokens. A connection can be limited to read-only or to some spaces.
- 20 tools to list spaces, search, read, browse, follow history, manage the inbox, write,
  move, delete and verify pages. `search` and `fetch` follow the ChatGPT connector format.
- 3 prompts: `save`, `answer_with_sources`, `briefing`.
- Tested with Claude.ai, Claude Desktop, Claude Code and ChatGPT (developer mode). A
  **Test the connection** page shows calls as they arrive.

#### AI providers
- Anthropic (default model `claude-sonnet-5-5`), OpenAI, Mistral, Ollama and any
  OpenAI-compatible endpoint, configured per organization, with a per-space model override.
- Claude and ChatGPT subscriptions through the official Claude Code and Codex CLIs (super
  admin only, self-built image), with quota tracking for Claude.
- Automatic cost estimates for Claude models; manual price per million tokens for the others.
- The monthly budget covers tidy-up, routines and semantic indexing (embedding tokens priced
  for OpenAI, Gemini, Mistral and Voyage AI). With a provider billed per use, the form
  suggests a budget of 20 € a month and the AI page warns while none is set.

#### Accounts and teams
- Roles: super admin, organization admin, contributor, reader. Invitations by email or by
  link, password reset, email verification.
- Single sign-on: OIDC, GitHub, GitLab, Google, with optional allowed email domains per
  provider.
- Personal tokens and connected applications, managed in **My account**.
- Alerts to organization admins: failing tidy-up, routine or sync, inbox not reviewed,
  deletion cap reached, big changes, subscription quota above 90 %, budget.

#### APIs
- REST API covering the web features, with an optional OpenAPI schema and interactive docs
  (`api_docs`).
- Webhooks signed with HMAC-SHA256, with automatic disabling after repeated failures.

#### Operations
- Docker Compose deployment, behind the host's reverse proxy or all-in-one with nginx and
  Let's Encrypt. Image published at `ghcr.io/synaptane/synaptane`, without the Claude Code and
  Codex CLIs; the Dockerfile can build them in.
- `SYNAPTANE_VERSION` selects the image tag; `SYNAPTANE_DEFAULT_LOCALE` sets the default
  language of the UI (`en` or `fr`).
- The Compose file pulls the published images at its own version by default
  (`SYNAPTANE_VERSION`, `1.0.0-rc.1` for this release; there is no `latest` to follow) and
  never builds. Building from the repository goes through `compose.build.yaml`, under the local
  name `synaptane:local`. Both images carry `LICENSE`, `NOTICE` and the third-party notices in
  `/usr/share/doc/synaptane/`.
- Demo data set (`examples/acme-wiki/`, the wiki of a fictional IT team) and the
  `synaptane demo` command, which loads it into a disposable instance.
- Eight sample files in the demo data set, linked from its pages: a PDF contract, a scanned
  delivery note (OCR), a Word checklist, a PowerPoint review, an Excel inventory, the 2019
  inventory in the old Excel format (`.xls`), a rack diagram (PNG, OCR) and an HTML status
  report, generated by `scripts/demo_files.py` (the `.xls` file by `scripts/legacy_files.py`).
- `synaptane doctor` checks the installation; `synaptane check-clis` checks that the agent
  CLIs only expose Synaptane's tools.
- Verified backups and atomic restore. Off-site copies encrypted with age and sent with
  rclone to one or more destinations (S3, B2, SFTP, WebDAV, SMB, Google Drive…), with
  retention. Monthly restore drill script.
- Server scripts in `deploy/host/`: `deploy.sh` (backup, build, checks, automatic rollback),
  `backup.sh`, `install-nginx.sh`.
- The running version and commit are shown by `synaptane --version`, `doctor`, the
  administration page and backup manifests.

#### Interface
- Web UI in English and French, light and dark themes, usable on mobile and with the
  keyboard.

### Changed
- Agent instructions in English; the agent writes in the language of the space.
- Interface strings now have English source keys; French is a translation (fr.json); REST
  errors follow Accept-Language.

### Security
- No default account. A random master key (mode 0600) encrypts secrets stored in the
  database; changing an API or repository URL requires entering its secret again.
- Passwords hashed with Argon2, tokens stored hashed, CSRF protection on every form, login
  throttling per account and per address.
- One authorization check shared by the web UI, the REST API and MCP. Through an assistant,
  even a super admin reaches only the spaces they are a member of.
- Email addresses are verified by a link that must be confirmed from the signed-in account
  that asked for it.
- OAuth consent screen warns when an application name does not match its redirect address.
  For an account in several organizations, the consent screen and the personal token form
  select only the spaces of one organization by default.
- Single sign-on never creates accounts unless enabled per provider (off by default), and
  can be limited to allowed email domains. A GitHub, Google or gitlab.com provider that would
  let anyone sign up is flagged as "Open to everyone".
- Rate limits, answered with HTTP 429: 3 email verification links per account per hour, 30
  invitations per organization per hour, 120 inbox drops per account per hour, 5 manual
  tidy-up runs per person and space per day (20 for an organization admin).
- Sign-in logs never contain an unknown login as typed, only a short fingerprint.
- A monthly budget requires a known price for every model in use; a model that loses its
  price is counted at the highest known price, never zero.
- Probable secrets are refused before any commit (pages, notes, attachments, agent output).
- Content read by AI agents is framed as untrusted data, including everything the MCP tools
  return to assistants. Every MCP write tool is declared destructive, so that clients ask
  for confirmation. The tidy-up agent and routines have no shell, file or network access;
  subscription CLIs are restricted to Synaptane's tools, checked in CI.
- Hardened Git use: Git hooks disabled, no symbolic links, literal paths.
- Markdown rendered through an HTML sanitizer, with a strict Content Security Policy.
- Document extraction isolated in a process limited in memory and time.
- Webhooks protected against requests to private networks.
- Hardened containers: read-only file system, all capabilities dropped, no privilege
  escalation, PostgreSQL not exposed, app bound to `127.0.0.1`, forwarded headers trusted
  only from configured proxies.

### Notes
- Synaptane is **alpha software (preview)**, provided as is and maintained on a best-effort
  basis by one person. The REST API, MCP tools and configuration may still change.
- See [Known limitations](README.md#known-limitations) in the README.

[Unreleased]: https://github.com/synaptane/synaptane/compare/v1.12.0...HEAD
[1.12.0]: https://github.com/synaptane/synaptane/compare/v1.11.0...v1.12.0
[1.11.0]: https://github.com/synaptane/synaptane/compare/v1.10.0...v1.11.0
[1.10.0]: https://github.com/synaptane/synaptane/compare/v1.9.1...v1.10.0
[1.9.1]: https://github.com/synaptane/synaptane/compare/v1.9.0...v1.9.1
[1.9.0]: https://github.com/synaptane/synaptane/compare/v1.8.0...v1.9.0
[1.8.0]: https://github.com/synaptane/synaptane/compare/v1.7.0...v1.8.0
[1.7.0]: https://github.com/synaptane/synaptane/compare/v1.6.0...v1.7.0
[1.6.0]: https://github.com/synaptane/synaptane/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/synaptane/synaptane/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/synaptane/synaptane/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/synaptane/synaptane/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/synaptane/synaptane/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/synaptane/synaptane/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/synaptane/synaptane/compare/v1.0.0-rc.2...v1.0.0
[1.0.0-rc.2]: https://github.com/synaptane/synaptane/compare/v1.0.0-rc.1...v1.0.0-rc.2
[1.0.0-rc.1]: https://github.com/synaptane/synaptane/releases/tag/v1.0.0-rc.1
