# Changelog

All notable changes to Synaptane are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Before each upgrade, back up your instance (see `docs/admin/backup.md`) and read the section
of the target version.

## [Unreleased]

### Added
- **The nightly maintenance fixes the team checks people break**. A push cannot be refused,
  and a page someone edits can bring a customer's address or lose its `type`: Health showed
  it, nobody fixed it. Now, with **Maintain the wiki every night**, what people broke since
  the last run of a `block` rule or of personal data (a preset, or `mask: yes`) becomes one
  more problem of the "Wiki maintenance (N)" item: the rule (title, level, message), the
  page, the line and an excerpt. The agent removes the data or replaces it with a neutral
  mention, completes a field only with a value the page states, never deletes or moves a
  page, and otherwise leaves the problem with a reason. The other `warn` rules are only
  reported in Health. Same limits as for the wiki's problems: 10 a night, imports and
  Synaptane's own commits left out, a problem left not given again unless written again.
  The most urgent come first: personal data, then `block` rules, then broken links and the
  rest. The data itself is never in the clear outside the page: the excerpt is masked in the
  item, the log and what Synaptane remembers of a problem left, and a summary, a reason (of
  any item of that run) or a commit message in which the agent copied it anyway is masked the
  same way. A page the agent moves keeps its problems, under its new path. A faulty rule is
  left out (Health names its line), and a source of problems that fails is left out for the
  night: the others still run, and its window is read again the next night.
- **`fix` key in `.synaptane/checks.md`**: `fix: yes` lets the nightly maintenance fix a
  `warn` rule, `fix: no` keeps it from fixing a `block` or personal data one. The examples
  mention it.
- **Memory of a routine**. With `memory: true` in its file (or the **Keep notes from one run
  to the next** box of the form), a routine keeps notes for its next runs in
  `.synaptane/memory/<name>.md`: topics already reported, pages it follows, decisions
  noted, what remains to watch. The agent receives it at the start of each run, framed as
  untrusted data (4,000 characters at most), and rewrites it in full with a new `remember`
  tool (4,000 characters at most, secrets and the team's `block` checks on page text
  refused). It goes in the run's single commit: an interrupted run leaves it as it was, and a
  memory changed during the run (a push already synchronized, an administrator clearing it)
  keeps that change, which the log says. The routine's page shows it to contributors; administrators clear it
  (a commit in their name). REST: `memory` on the routines, `GET` and `DELETE
  …/routines/{id}/memory`, and `memory` in a run's `outputs`. Deleting a routine removes its
  memory in the same commit; a routine removed or renamed by a push loses it at the next
  synchronization (move the file along to keep it). Off by default: a memory is read at every
  run and costs tokens. Like the folders a routine keeps, it lives in the file only, so a
  space must have been synchronized once.
- **A routine can keep pages in folders assigned to it**. A new `writes` field of the routine
  file (Settings → Routines: **Folders the routine keeps**; REST: `writes`) gives it up to 5
  folders of the wiki, with the syntax of `scope`: a weekly digest writes
  `digests/2026-W46.md`, a watch routine keeps `watch/` up to date. There, the agent gets the
  tidy-up's writing tools (write, append, edit, move, delete) and their safeguards (page read
  in full before it is replaced, verification fields kept, broken links reported), with at
  most 15 pages changed and 5 deletions per run. Any other path is refused to the agent as a
  tool error, whatever a page it read asks: another folder, `.synaptane/`, a hidden file, a
  symbolic link, its own report page, a move out of its folders or one that would rewrite a
  page outside them. A likely secret and a team `block` check are refused too. The pages go
  in the run's single commit, with the report and the inbox items; a page someone changed
  during the run is merged, their version kept on a conflict; an interrupted run publishes
  nothing. The routine reads its folders even outside its scope. The log, the email, the
  `routine.done` webhook (`written`) and `GET …/routines/{id}/runs` (`outputs.written`) list
  the pages written. These pages are not reviewed by the tidy-up as changes, but the nightly
  maintenance treats them like a person's: it fixes the broken links and the personal data
  they bring (a team `warn` rule does not stop the routine). Without `writes`, nothing
  changes; the two existing templates keep no folder.
- **Digest archive template**: every Friday at 16:30, the summary of the week as a page of its
  own, `digests/2026-W46.md` (`resumes/2026-S46.md` in French), added at the top of the
  folder's `README.md`. It keeps that folder and writes nothing else. Every routine is now
  given the ISO week along with today's date, so that it does not have to compute it.

### Changed
- **The Consistency check template keeps a memory**, so that it no longer reports the same
  contradiction every week while its pages stay the same, and rereads other old pages in a
  quiet week. The Weekly digest does not (the week's history is enough).

### Fixed
- A front matter rule with `mask: yes` showed the field's value in its excerpt when the
  value had the wrong form (`expect: date`, `list`): it is masked now, in Health too.

### Upgrade
- No migration: the memory and the folders of a routine live in its file only, the `fix` key
  in `.synaptane/checks.md`. Update the image and restart.
- Routines created before keep their behavior: to give an existing
  consistency check its memory, tick the box in its form or add `memory: true` to its file
  (the new instructions of the template can be copied from a routine created from it).

## [1.16.1] - 2026-11-10

A broken AI connection no longer goes unnoticed. With an expired Claude Code login, each
nightly tidy-up stopped in a second but was logged as done: no banner, no alert, and after
three nights the pending items were given up. Now such a run is a failure, the
organization's administrators see a banner on every page until the connection works again,
and an outage no longer counts against the items waiting in the inbox. No migration: update
the image and restart.

### Added
- **Broken AI connection banner**. When the nightly tidy-up of an organization stops working
  for a cause on the AI side (an expired Claude Code or Codex login, a refused key, a model
  not found, an unreachable provider or agents container; a rate limit or an overloaded
  provider after 3 failures in a row), or its last **Test the connection** failed, its
  administrators see a red banner on every page: "The nightly tidy-up of Acme has not been
  working since …: the Claude Code login has expired", what to do, and a link to the
  organization's Artificial intelligence page (which gives the login command). It goes away
  as soon as a test or a run works again. Members do not see it (the Tidy-up tab still tells
  them). A few small queries per page, for organization admins only.
- **Tidy now in Settings → Tidy-up**: the button is also at the top of the space's tidy-up
  settings, with the same confirmation and caution as in the log; the request leads to the
  Tidy-up tab, which follows the run.

### Fixed
- **A run the provider stops before any tool call is a failed run**. With an expired subscription
  login, each run ended in a second as "done", the error tucked away in the log: no banner,
  no badge on the Tidy-up tab, no `tidy_failed` alert, and nothing in `GET …/tidy/runs`,
  the health page or the metrics told it had failed (the CLI counts one turn even when its
  login has expired, which passed for work). Now a run stopped by the provider, or by a CLI
  that does not start, before the agent called any tool is `failed`, with the
  error: retried later if the error is transient, otherwise reported to the administrators.
  Nothing is committed: the notes and changed pages waiting in the inbox no longer count an
  interrupted run (after 3, they were left with the reason "Not processed after 3 interrupted
  runs"), and the nightly maintenance neither moves its watermark nor uses up its attempts.
  A run stopped once the agent has started reading stays done, its work kept, and still
  sends the `tidy_interrupted` alert: the items count the attempt, so one that breaks every
  run is still given up in the end, and the next try waits for the next run.
- A rate limit or an overloaded API that Claude Code or Codex reports in its final result is
  a passing error, as it already was on the CLI's error output: the run is retried by the
  queue, and the administrators are not alerted for a provider overloaded for a few minutes.
- The tidy-up log recognizes the CLI's own words for an expired login ("Failed to
  authenticate: OAuth session expired…", "Please run /login") as a logged-out subscription,
  with the advice to log in again, instead of a refused API key.

## [1.16.0] - 2026-11-09

Describe the night in the repository: each agent routine of a space becomes a file of its
repository, a team writes its own checks (patterns, front matter, personal data) that Health
shows and the tidy-up follows, and the nightly tidy-up maintains the wiki even when the inbox
is empty. Upgrading from 1.15.0: two migrations (`0034`, `0035`), applied at startup; at each
space's next synchronization, Synaptane writes its routines to the repository in one commit
(see Upgrade below). One new dependency, `google-re2`, comes with the image. Back up first:
going back to 1.15.0 then means restoring that backup.

### Added
- **Team checks**. A space can list, in its repository, the rules every page must follow:
  `.synaptane/checks.md`, readable and editable by hand. Each level-2 heading is a rule, the
  `- key: value` items under it describe it: a `pattern` on the text (regular expression), a
  front matter `field` with `expect` (`present`, a YAML `list`, a `date` without a time) or a
  `pattern` on its value, or a `preset` for personal data: `email` (with the domains allowed in
  `allow`), `phone`, `card` (Luhn check) and `iban` (check digits), which see through no-break
  spaces, dashes and zero-width characters between digits; optional `paths` and
  `except` (folders or globs), a `level` (`warn`: reported, `block`: the write is refused) and
  the `message` that says what to do. Patterns run on RE2 (the `google-re2` package), in linear
  time: no team pattern can freeze the server whatever the text of a page, and the number of
  rules (50), the length of a pattern (500 characters), its cost per byte (3,000 RE2 steps per
  pattern, 15,000 per file: `.{0,1000}` is refused) and the text Health examines (1 MB per
  page; a write is checked in full) are capped. A faulty rule is ignored and named, never fatal. Organization admins write them in
  **Settings → Purpose → Team checks**, next to the purpose and the filing instructions: a
  commit in their name, conflict detected, an example ready to adapt (English or French), and a
  **Preview** that lists the rules understood, the lines in error and the number of current
  pages that break each rule; a file with a faulty rule is refused there. REST:
  `GET`/`PUT …/spaces/{space}/checks`. A `block` rule refuses the writes that add a violation,
  with the rule's message, wherever they come from: web editor, MCP, REST (`422`,
  `check_failed`, not lifted by `allow_secrets`), moves (a page moved into a folder a rule
  covers), import (the file is left out, with the reason), the tidy-up, the report page of a
  routine (refused to its agent, which fixes the text) and the undo of a tidy-up or of an
  assistant's run (as a restore); as for secrets, a
  page that already broke a rule stays editable. A push cannot be refused: the **Health** tab
  gets a **Team checks** section (rule, page, line, excerpt with personal data masked:
  `j•••@gmail.com`, `••• 1111`), computed per page digest like the rest of the tab, and the
  REST health gets `checks`, `check_rules`, `check_errors` and `counts.checks`. The tidy-up
  agent receives the rules with its instructions (configuration data, like the filing
  instructions); a write that a `block` rule refuses is sent back to it, and the violations of
  a `warn` rule that its run introduced come back in its end-of-run check, in the same round of
  fixes as broken links, then in the **Quality** block of its log (`checks` in the metrics).
  `services.checks` also gives the violations of a page, of a commit or between two commits.
  No migration.
- **Nightly wiki maintenance**. The scheduled tidy-up (and **Tidy now**) also fixes the
  problems people introduced since the last run, through the web, REST, MCP or pushes, even
  when the inbox is empty: broken links introduced in the pages they changed (or
  in pages that pointed to a page they deleted or moved), and their new pages that nothing
  links to, missing from their folder's index, untitled or empty. A check without AI finds
  them; they reach the agent as one "Wiki maintenance (N)" item listing each problem (page,
  link, suggestion), framed as data. The agent fixes what is safe (a link to the right page,
  a link from the topic's page or the index, a line in the index), adds no content, never
  deletes or moves a page for it, and leaves the rest with a reason; its tools, deletion cap,
  alerts and undo do not change. At most 10 problems a night; Synaptane's own commits never
  count (the tidy-up's end-of-run check already covers them), nor do imports (more than 20
  pages in a commit, already reviewed as one "Import: N pages" item); a problem left is not
  proposed again unless it is introduced again (gone and back, the same broken link written
  once more, the page created anew), so a page edited every day with a link broken on purpose
  costs nothing; runs after a drop and chained runs do not maintain; nothing is
  computed without an AI provider or once the month's budget is used up. Each problem gets a
  line in the run's log (fixed, or left with the reason); contributors get no notice for it.
  A run stopped before the end gives the problems again; after three interrupted runs in a
  row, they are left, as an inbox item would be.
  The **Maintain the wiki every night** box (Settings → Tidy-up, `maintain` in
  `PATCH …/tidy-settings`) is **on by default**: a night without problems costs nothing. See
  [Wiki maintenance](docs/admin/ai.md#wiki-maintenance).

### Changed
- The installation guide is split into three pages: **Install and run** (Docker Compose, modes,
  upgrade, rollback, from source), **Configuration** (options and variables, email, search
  index, alerts, monitoring, audit log, roles) and **First steps** (organizations, spaces,
  demo data). Links to the moved sections now go to `docs/admin/configuration.md` and
  `docs/admin/first-steps.md`.
- **Agent routines are files of the repository.** Each routine of a space is now
  `.synaptane/routines/<name>.md`, versioned like the purpose and the tidy-up rules: a header
  (`title`, `description`, `schedule` such as `weekly monday 06:00`, or in French
  `hebdo lundi 06:00`, written back in English by Synaptane, `scope`, `report`,
  `inbox`, `email`, `webhook`, `enabled`, `template`), then the instructions. Git is the source
  of truth; the database keeps a copy of the settings and the state of the runs. At each
  synchronization, Synaptane reads the files at the space's current commit: a new file creates
  the routine (which waits for its next slot), a changed file updates its settings without
  making it run again, a removed file removes it (its log and its cost stay in the month's
  budget). An invalid file does not run, and its error shows in **Settings → Routines** and in
  the REST API (`error`); a field this version does not know is reported (`warnings`), ignored,
  and kept when Synaptane rewrites the file. **Settings → Routines**, the provided templates
  (**Enable**, **Customize first**) and the REST API keep working and write the file: a commit
  in the administrator's name, under the space lock, then a synchronization; a file changed
  since the form was opened is refused, with the choice to replace it, as for the purpose.
  Anyone who can push to the repository can now change a routine, as they can change the
  tidy-up rules: grant push access accordingly. Inside Synaptane, only organization
  administrators write these files. REST: a routine gains `title`, `description`, `path`,
  `sha`, `pending`, `error` and `warnings`; `name` is now the file name (a free text given at
  creation is still taken as the title); `PATCH` and `DELETE` accept `base_sha`. A routine
  created from a template has the same file name in every language: `consistency-check`,
  `weekly-digest`. See
  [Agent routines](docs/admin/ai.md#the-routine-file).

### Upgrade
- New dependency, `google-re2` (BSD license, wheels for Linux, macOS and Windows): installed
  with the image, or by `uv sync` from source.
- One migration (`0034`), applied at startup. Each existing routine keeps its row, so its
  state and its runs; its name becomes its title, and it gets a file name made from it
  (`Contrôle de cohérence` → `controle-de-coherence`, `-2` if the name is taken in the space),
  even for a routine created from a template, which a new one would name `consistency-check`.
  At the next synchronization of each space, Synaptane writes its routines to the repository
  in one commit, `Synaptane: routines written to the repository`, and pushes it. A routine
  keeps running from the database until then, and runs neither twice nor not at all: its last
  run day stays. A space not synchronized yet writes them at its first synchronization. If a
  file of the same name is already in the repository with other content, the repository wins
  and the routine of the database takes a free name (`-2`); with the same content, it is
  adopted. A failed push keeps the commit in the local copy, and the next synchronization
  pushes it. REST clients that change a routine's `name` with `PATCH` must change `title`
  instead. As before any upgrade, back up first.
- A second migration (`0035`), applied after it: three columns on spaces and an empty table of
  the problems left. No existing column changes. Maintenance starts from each space's last
  synchronized head: the past is not caught up.

## [1.15.0] - 2026-11-09

Send or forward an email to a space's inbox; import a Notion or Confluence export; and run the
subscription CLIs (Claude Code, Codex) in a container of their own, away from the data and the
secrets of the instance. Upgrading from 1.14.0: one migration (`0033`), applied at startup; it
creates the email capture tables, empty, and the capture stays off until a super admin sets it
up. With Docker Compose, the new `agents` service starts with the others; the CLIs' logins stay
valid, nothing to sign in again (with a custom Compose file, see the upgrade note of
[The agents container](docs/admin/ai.md#the-agents-container)). Back up first: going back to
1.14.0 then means restoring that backup.

### Added
- **Capture by email**. An email sent or forwarded to `brain+organization.space@example.com`
  becomes a note in the inbox of that space, attachments included, filed by the tidy-up like
  any other note. The worker reads a dedicated mailbox over IMAP (SSL/TLS or STARTTLS) every 2
  minutes by default, in its own lane: no webhook, no incoming mail server. The super admin
  sets it up in **Administration → Email → Capture by email**: server, username, password
  (encrypted, never shown again), folder read, capture address, trusted servers, interval,
  answers; **Test the connection** signs in and names the servers that authenticated the latest
  emails; **Read the mailbox now** asks for a poll. An email is dropped only if its `From` is
  the verified address of an active account that may drop notes in the space (same rules as the
  web form and `inbox_add`, restricted spaces and groups included; a super admin only where
  they are a member), and if the receiving server authenticated it: the topmost
  `Authentication-Results` header (RFC 8601), written by a trusted server, says `dmarc=pass` or
  `dkim=pass` aligned on the `From` domain (a DMARC pass must name that domain in
  `header.from`); a header written by the sender is never read, nor one that could be read two
  ways. Against a signed email replayed by someone else: a header allowed once (From, To, Cc,
  Subject, Date, Message-ID…) present twice, or a line of the headers that is not a header,
  refuses the email in silence, its `Date` must be within 48 hours of its reception (IMAP
  `INTERNALDATE`), and the capture address must be in To or Cc (a blind copy, or an address
  only in `Delivered-To`, aims at nothing); a Cc added to a signed email that had none still
  passes within those 48 hours (documented). A poll is cut
  after 5 minutes whatever the server does. Two options, off by default and documented as less
  safe: internal domains accepted on SPF alone, and the Microsoft 365 header without
  identifier. Same limits as every note (10 attachments of 25 MB, 35 MB per email, secrets
  refused, 120 notes per hour); the plain text version is preferred, HTML is turned into
  Markdown without scripts or remote images. Handled emails are moved to `Synaptane/Processed`
  or `Synaptane/Refused`; an email that cannot be dropped now (space busy, rate limit) waits
  for the next poll without holding back the others, and is refused after 24 hours; the parts
  of an email are only read once its sender is authenticated and has an account, and an email
  of too many nested parts is refused; the same email (its `Message-ID`) is never dropped
  twice; a single poll runs at a time. The note is written in the person's name, with "by
  email" in the space's activity (a person, not an AI). Only an authenticated sender with an
  account may get an answer (the reason of a refusal by default, or a confirmation too, or
  nothing; 20 per hour and per account at most), never a forged address. Members whose emails
  would be accepted (the same rule, super admin included) see the address of the space, with a
  copy button, on its **Inbox** page and in its first steps. The health page shows the polls
  and the latest refusals (reason, sender's domain, space aimed at: never the content),
  `/metrics` gives `synaptane_email_capture_messages_24h`,
  `synaptane_email_capture_poll_failing` and
  `synaptane_email_capture_last_poll_timestamp_seconds`, and each change of the settings goes
  to the audit log. New option `allow_insecure_imap` (development only). One migration
  (`0033`), applied at startup: it creates the two tables of the capture, empty, so nothing is
  read before the super admin sets it up. See `docs/admin/email-capture.md`.
- **The subscription CLIs run in a container of their own**. With a Claude or ChatGPT
  subscription, Claude Code and Codex no longer run in the worker but in the `agents` service of
  `compose.yaml` (`synaptane agents-runner`): same image, but neither the data volume (master key,
  repositories), nor the backups, nor the database, nor any secret of the instance; only the CLIs'
  logins (`synaptane-agents`, mounted there only) and a key it shares with the worker. It is on a
  network shared with the worker alone, without Internet, and on one of its own for Anthropic and
  OpenAI: neither the database nor the web container are within its reach. Non-root, read-only
  file system, no Linux capability, memory capped (`SYNAPTANE_AGENTS_MEMORY`, 2 GB) and one CLI at
  a time (`SYNAPTANE_AGENTS_MAX_RUNS`, 1: CLIs running together share an account and could read
  each other's run token; a run that arrives during another one waits for its turn in the worker,
  without failing). The worker sends it each run, with the key (created by the worker on its first
  call in the `synaptane-agents-key` volume, never logged); the CLI is launched exactly as before,
  with the same options, and reaches the run's tools at the worker's address on their private
  network, with the run's single-use token. A run is stopped at its time limit, as soon as the
  worker gives up, or beyond 4 MB of results; only the events Synaptane reads come back to the
  worker. The isolation contract (`synaptane check-clis`) runs in the agents container, where the
  CLIs run. Claude Code now runs with `--setting-sources ""`: hooks, settings and instructions
  left in its `HOME` (`~/.claude/settings.json`, `CLAUDE.md`, skills) no longer apply to the runs,
  which the contract checks by planting some; Codex leaves out the descriptions of the skills of
  its home (`~/.codex/skills`, `~/.agents/skills`), which `--ignore-user-config` kept; a Codex run
  is refused while `~/.codex/AGENTS.md` (which Codex adds to every run, with no option to leave it
  out) is there. Sign in with `docker compose exec agents claude auth login` (or `codex login
  --device-auth`): the AI page gives the command. This closes the known limitation of the earlier
  versions, where a flaw in the CLI could read the server through the worker's account. See [The
  agents container](docs/admin/ai.md#the-agents-container), with what it protects and what it does
  not.
- **Where the CLIs run, on the health page and in `synaptane doctor`**. **Administration →
  Health** has a "Subscription agents" line: agents container reachable, CLI versions and
  isolation contract, or the CLIs running in the worker (less isolated), or what the agents
  container can reach and should not (a secret, the master key, the Docker socket, the
  database). The worker reports it every 10 minutes, since the web container does not reach
  the agents container; `docker compose exec worker synaptane doctor` asks it directly.
- **Import from Notion and Confluence**. The import of a folder's **⋮** menu (now **Import
  Obsidian, Notion, Confluence…**) also takes the zip of a Notion export (**Markdown & CSV**,
  including the zip of zips Notion makes for a large workspace, `Export-…-Part-1.zip`…) and the
  zip of a Confluence space exported in **HTML**. The format is recognized from the archive and
  shown on the check screen, where it can be changed (**Read it this way**); the send form can
  also force it. Notion: names lose their 32-character id (a second "Notes" in a folder becomes
  "Notes (2)"), a page with subpages or files becomes its folder's home page
  (`Notes/README.md`), links to the export's pages, files and databases are rewritten to the new
  paths, `https://www.notion.so/…` addresses of an exported page too; a database becomes an index
  page (a table of its rows, each linked to its page) and each row's properties go to its page's
  front matter; callouts and toggles become callouts. Confluence: the page tree is rebuilt from
  `index.html` and the breadcrumbs, pages become Markdown (headings, lists, tables, code with its
  language, links, images; info, tip, note and warning panels as callouts, expand as a folded
  callout), attachments go next to their page under their real name, and links to the export's
  pages and attachments are rewritten, addresses of the Confluence site with a page id too. The
  check screen counts the rewritten links, lists those that lead nowhere (a page that is not in
  the export) and the files of the export not imported as they are (styles, icons, a database's
  second CSV); converted pages go through the same secret check as any file. The HTML is not
  trusted: nothing in it is run or loaded, scripts, frames and forms are dropped, and paths come
  from the layout made by Synaptane. A Confluence XML export is refused with a message saying to
  export in HTML. No new dependency, no migration.

### Changed
- **Test the connection** of a subscription goes through the agents container, and a tidy-up
  or routine that failed because of it says so in its log ("agents container unavailable" or
  "busy") instead of blaming the AI provider.
- `deploy/host/deploy.sh` also waits for the agents container to be healthy, and rolls back
  otherwise. `scripts/e2e-compose.sh` checks its isolation (no secret, no data, neither the
  database nor the web container within reach, key required) and that a login made by the
  previous version stays readable; `E2E_PROJECT` runs two at once.

## [1.14.0] - 2026-11-09

See every Markdown text as it will read while writing it, not only pages; invite someone straight
into groups; be warned before a Git access token expires; and publish the tidy-up bench's results
in the open. Upgrading from 1.13.0: one migration (`0032`), applied at startup; it creates the
table of invitation groups, empty, so nobody's access changes. Back up first: going back to 1.13.0
then means restoring that backup. pydantic 2.14.3 fixes two advisories (Synaptane was not exposed
in practice), and an access token pasted with an invisible character is now refused with a clear
message instead of being saved.

### Added
- **Preview while editing, in the other Markdown editors**. The page editor's **Write**,
  **Preview** and **Side by side** (on a large screen) now come above the text of an inbox
  note, when correcting it (**Edit**) or dropping it, in a space's inbox or from **Drop
  information** (in the space chosen in the form; no side by side in these narrow forms), and
  above the purpose, the filing instructions (**Settings → Purpose**) and each team prompt
  (**Settings → Assistant prompts**). Same server rendering as reading, same protections
  (form token, 262,144 characters at most, one preview at a time per account whatever the
  editor, database connection given back before rendering), nothing committed, indexed,
  dropped or logged; the right asked is exactly that of the corresponding edit: dropping or
  correcting the note (`POST /o/<org>/<space>/-/inbox/preview`, with `note` for a note being
  corrected), the organization administrator for the purpose, the instructions and the prompts
  (`POST /o/<org>/<space>/-/vocation/preview`, with `kind`). The purpose's **Preview** button,
  which reloads the page, stays for browsers without JavaScript (hidden otherwise), and team
  prompts get one too. No migration.
- **Invite someone straight into groups**. The invitation form of the organization's
  **Members** tab lists its groups, each with what it gives ("Contributor on HR"): the
  administrator checks those the person joins. On acceptance, the person becomes a member of
  the organization, then of these groups, in the same transaction as the rest of their access;
  an existing account joins them at once, and its notice email now lists the roles given in
  spaces and the groups joined. The invitation page says the real access before the
  account is created, groups included ("HR: contributor, through the group “Management”", "You
  also join the group “Management”"; names are quoted, a name may hold a comma), and **Pending invitations** shows each invitation's groups.
  A group deleted before the acceptance is simply not joined, a renamed group is joined under its
  new name, canceling or renewing the invitation drops its groups, and a group of another
  organization, or one deleted while the invitation is being sent, is refused, by the service
  and by the database. REST: `"groups": [<id>, …]` (200 at most) in
  `POST /organizations/{org}/invitations` (404 for an unknown group or one of another
  organization), and `groups` (`id`, `name`) in the invitations listed and created. The audit
  log keeps the groups, as a list of their names at the time, in the **Invitation sent** and
  **Existing account added by invitation** events (for the latter, only the groups actually
  joined), and the acceptance writes one `group.member_added` per group,
  by the system, like the other accesses it grants. One migration (`0032`), applied at startup:
  it creates the table linking invitations to groups, empty, so no pending invitation changes.
- **Warnings before a Git access token expires**. When the expiry date of a space's HTTPS
  token is known, the organization's admins get an alert by email (in their language), in the
  audit log (**Alerts**) and through the `space.alert` webhook, **30 days, 7 days and 1 day
  before, then on the day** it expires (`git_token_expiring`, `git_token_expired`): once per
  step, checked every hour by the worker, a stopped worker only sending the step reached, and
  a new token, with another date, starting over. **Administration > Health** gets a **Git
  access tokens** line, a warning when a token expires within 30 days and a problem once one
  has expired, listing each space with a link to its Git page and how to renew the token. A
  sync refused for authentication after this date now says that the token expired, and when,
  on the space's page, in the REST API and in the sync alert. The date is known for the GitLab
  project access token Synaptane creates, and now also for a token saved by hand in
  **Settings → Git repository → Repository access** or with `PUT …/git/credentials`: Synaptane
  asks the provider once, as it saves it (GitHub: the `GitHub-Authentication-Token-Expiration`
  header of `GET api.github.com/rate_limit`; any other HTTPS host, taken for a GitLab:
  `GET /api/v4/personal_access_tokens/self`), with the protections of the repository creation
  (10 seconds at most, no redirect followed, a self-hosted host at a public address only,
  except for the super admin, no database connection held meanwhile); when the provider does
  not tell, the token is saved all the same (a repository at an IP address or at a host name
  that is not plain ASCII is not asked). GitHub
  gives the moment: on the last day, before that hour, the token "expires today". If every email
  of a step fails, the next hourly check sends them again; the audit log and the system alerts
  show the expiry date, written out in the reader's language (ISO in the `space.alert`
  webhook). The expiry date in a sync error is shown to the organization's admins
  only, like the rest of the repository access. REST: `credentials_expire` in
  `GET …/spaces/{space}/git`, for organization admins. The token never appears in an alert, an
  email, the audit log or a log line. No migration: the date is kept with the token, encrypted.
  See `docs/admin/git.md#token-expiry`.
- **Publishable results of the tidy-up bench**. A run with a real model
  (`./scripts/eval.sh tidy --model …`) now writes a versioned run file in `eval/results/runs/`
  (`synaptane-tidy-bench-run`, format 1): Synaptane's version and commit, read from the
  repository, and whether files changed since, the dates and duration, the fingerprints of the
  bench, of each scenario, of the prompt and of the agent's tools, the provider, model, effort
  actually sent and price applied, and every trial with its graders, tokens, cost, duration,
  interruption and the model that served it. `./scripts/eval.sh report` aggregates one or more
  run files into `eval/results/results.json` and two tables, `results.md` and `results.fr.md`,
  with the method and its limits at the top: per configuration, the pass rate with its 95 %
  Wilson interval (rows sorted by its lower bound), the score per sweep (mean and standard
  deviation), the cost per run and per passed run, the tokens and the duration, the passes per
  scenario, and between every two configurations a paired comparison by scenario (exact sign
  test, adjusted by Holm's method for the number of pairs). The report only depends on the run files, so the same runs give the same bytes, and it
  refuses what does not compare honestly: a run stopped by its budget or by the provider, a
  trial cut, trials missing or doubled, fewer than 3 trials per scenario, a trial served by
  another model, different scenarios, different commits (unless `--allow-mixed-commits`), a
  modified tree (unless `--allow-dirty`). Nothing is masked silently: a run file or a report
  that would contain the key, the provider's address or credentials, anything that looks like a
  secret or a path of the machine is not written, and the provider's errors are recorded by
  class and HTTP status, never by their text. A trial hit by a passing provider error (429,
  529) is played again, twice at most, and its cost counted in the total; with a local or
  compatible server, only 429, 503 and a refused connection are replayed, since a 500 or a
  time-out may come from the model. Above 5 % of trials replayed, the report is refused unless
  `--allow-replays`, and the replays are shown per scenario. Claude's fallback to another model is
  turned off in the bench. `--trials` is 3 by default for a real model, fewer needs `--draft`;
  `--prompt FILE` tries other instructions for the agent in the bench only, and `--label` names
  the configuration. `./scripts/eval.sh tidy --estimate --model M` gives the order of magnitude
  of a run's cost without calling any model. Each turn of a provider now carries the name of the
  model that served it (`Turn.model`). See `docs/dev/eval.md` ("Publish the results") and
  `eval/results/README.md`. No migration.

### Changed
- **Tidy-up bench with a real model**: the trials now run as sweeps, trial 1 of every
  scenario, then trial 2, and `--json` gives the path of the run file in its new format (a JSON
  object, no longer a list of results).
- **Settings → Git repository → Repository access** shows any known expiry date of the saved
  token, not only the one of a GitLab project access token created by Synaptane: muted beyond
  30 days, a warning within 30 days, a problem once it has passed, with where to create the
  next token on GitHub or GitLab. Saving a token whose expiry the provider gives says it
  ("Repository access saved. The token expires on …").
- An HTTPS access token with an invisible or control character (a zero-width space, a tab or a
  no-break space pasted with it) is refused with "Invalid access token", on the web page and
  with `PUT …/git/credentials` (400): it could never authenticate. Spaces and accented letters
  stay allowed (a Basic password for Gitea or Bitbucket Server). The token that creates a
  repository must be plain ASCII.
- Two workers no longer send the same alert twice: its check is serialized in the database.
- The worker's hourly checks (change watermarks, inbox backlog, review reminders, Git token
  expiry) run each on its own: a lasting error in one no longer skips the others.
- The purpose's preview now resolves `[[wiki links]]` in the space, and refuses a text above
  262,144 characters.
- Dependencies: anthropic 2.6.0, SQLAlchemy 2.1.8, python-multipart 0.0.33. Since anthropic
  2.5.0, an overloaded (529) or unavailable (503) Claude API raises a subclass of its server
  error: Synaptane still reports it as a temporary error, retried later, as before.
- Downloaded images, still pinned by digest in `compose.yaml`: PostgreSQL
  (`pgvector/pgvector:pg17`) rebuilt on 31 October 2026 with the Debian fixes of its base,
  still PostgreSQL 17.11 and pgvector 0.8.7, so the data needs nothing; certbot 5.9.0, whose
  renewal hooks no longer receive the names that failed to renew as renewed (Synaptane's
  renewal uses no hook). They are fetched on the next `docker compose pull` (or `up -d`) of an
  upgrade.
- Development and CI tools: Ruff 0.17.4, mypy 2.4.1, pytest 9.2.0, setup-uv 10.3.3.

### Fixed
- **Worker**: its hourly checks (inbox backlog, change watermarks, Git token expiry) now run
  right after it starts, also on a machine booted less than an hour before; they used to wait
  for the first hour.
- After **Preview** (without JavaScript) or an error, saving the purpose or the filing
  instructions no longer replaces, without a conflict, a version saved by someone else in the
  meantime: the form keeps the version the text started from.

### Security
- pydantic 2.14.3: fixes two validation bypasses when extra data is allowed
  (GHSA-qpmf-fgh3-ff25, keys of `__pydantic_extra__` in JSON; GHSA-3r8g-cqwr-jr29, aliased
  fields of a `TypedDict` or dataclass). Synaptane's own models allow no extra data; the only
  affected type among its dependencies (the `_meta` of MCP requests, in the MCP SDK) carries a
  progress token Synaptane does not use, so it was not exposed in practice. The minimum version
  required is raised to pydantic 2.14.2, so that an installation with pip cannot keep an affected
  version.

## [1.13.0] - 2026-11-08

Give a team several spaces at once with groups, have a new space's repository created on GitHub or
GitLab, see a page as it will read while writing it, and get a verified backup every night without
setting anything up. Upgrading from 1.12.0: one migration (`0031`), applied at startup; it creates
the group tables, empty, so nobody's access changes. Back up first: going back to 1.12.0 then means
restoring that backup. From now on the worker makes a verified backup every night at 04:00 (server
time) in the `synaptane-backups` volume and keeps its own archives for 7 days; with a cron job on
`deploy/host/backup.sh`, the cron job stays in charge and the worker only takes over if it stops.
`BACKUP_TIME=off` in `.env` turns the automatic backup off. A REST request body refused with 422 no
longer quotes the values received.

### Added
- **Preview while editing**. Above the text of a Markdown page, when editing it or creating it
  (**Edit**, **New page**), **Preview** shows the page as it will read, without saving
  anything: the server renders it exactly as the reading view does (Obsidian syntax, links and
  `[[wiki links]]` resolved in the space, images, highlighted code, sanitized HTML), and Mermaid
  diagrams are drawn in the same sandboxed frame as when reading. **Write** goes back to the
  text, kept as it is; on a large screen, **Side by side** shows both, the preview following the
  typing after a short pause (remembered in the browser). Links in the preview open in a new
  tab. Contributors only, with the form's token, Markdown pages only; nothing is committed, indexed or
  dropped in the inbox, a text above 262,144 characters is not previewed, and each account
  gets one preview at a time (the database connection is given back before rendering). No
  migration.
- **Groups of members**. In the organization's new **Groups** tab, an administrator creates
  groups (a name unique in the organization, an optional description), adds members of the
  organization to them, and gives each group a role, reader or contributor, on one or several
  spaces. A member's effective role on a space is the highest of the role given in the space,
  the roles of their groups on it, and the organization's default role. A group opens a
  restricted space too: that is how the human resources or management space is opened to its
  team only. Organization admins keep every right, a token or a connection limited to some
  spaces, to reading or to the inbox stays limited, and nobody joins the group of an
  organization they do not belong to, nor does a group get a role on another organization's
  space. It applies wherever access is checked: web UI, search (semantic too), REST API, MCP
  (`list_spaces`, search across spaces, writing tools), routine emails "to the members", review
  reminders, the owners offered when verifying a page. Removing someone from the organization
  removes them from its groups, deleting a group removes the access it gave, deleting a space
  removes the groups' roles on it. REST: `GET`, `POST /organizations/{org}/groups`, `GET`,
  `PATCH`, `DELETE …/groups/{id}`, `PUT`, `DELETE …/groups/{id}/members/{username}`, `PUT`,
  `DELETE …/groups/{id}/spaces/{space}`, and `GET …/spaces/{space}/groups` for whoever reads
  the space. Every change is written to the audit log (`group.created`, `group.updated`,
  `group.deleted`, `group.member_added`, `group.member_removed`, `group.space_role_set`,
  `group.space_role_removed`), shown in the **Members and roles** family. One migration
  (`0031`), applied at startup: it creates the group tables, empty, so nobody's access changes.
- **Create the space's repository from Synaptane**. In **New space**, "Create the repository
  for me on GitHub or GitLab" creates a private, empty repository on github.com, gitlab.com or a
  self-hosted GitLab (HTTPS), for the token's account or an organization, group or subgroup,
  named after the space's identifier unless another name is given; the first sync then prepares
  it as for any space. The admin's token, which must be able to create repositories (GitHub:
  fine-grained token with the Administration permission; GitLab: the `api` scope), serves this
  creation only and is never saved, logged or returned. Synaptane keeps an access limited to the
  new repository: an SSH deploy key with write access on GitHub (github.com's SSH keys read from
  its API, and read again from it if GitHub changes them), a project access token
  (`read_repository`, `write_repository`, Maintainer) on GitLab, whose expiry date is shown in
  **Settings → Git repository**. Every refusal is explained (token refused, missing rights,
  owner not found, name taken, provider unreachable, too slow or limiting requests, GitLab.com
  namespace on the free plan, where project access tokens do not exist) and creates nothing; a
  repository created whose access cannot be set up is still connected to the space, with a
  message giving its address, and is never deleted. Each call to the provider is abandoned
  after 20 seconds (name resolution included) and the whole creation after 60, without holding
  a database connection; an account creates one repository at a time; a refused TLS
  certificate is reported as such. A
  self-hosted GitLab must be at a public address, checked as for webhooks, except for the super
  admin. Audit log: "Git repository created by Synaptane" (`space.repository_created`) and
  "GitHub SSH host keys updated" (`space.git_host_keys_refreshed`). REST: `create_repository`
  in `POST /organizations/{org}/spaces`, with a `repository` block in the answer. No migration.
  See `docs/admin/git.md`.
- **Nightly backup with nothing to set up**. The worker makes a verified backup every night at
  04:00 (server time) in the `synaptane-backups` volume (`/backups`), and keeps its archives of
  the last 7 days, always the 3 newest, deleted before the space check so that a full disk
  recovers. No cron job is needed anymore. Each archive is written to a hidden `.part` file,
  read back like those of `synaptane backup`, and only then named; the encrypted off-site copy
  leaves with it when it is set up, once the backup lock is released. A worker started after
  the time makes the backup of the day right away. Settings: `BACKUP_TIME` (`off`: none;
  `4:00` and `04h00` are read too, and a value that is not a time gives 04:00 with a warning
  instead of keeping Synaptane from starting), `BACKUP_LOCAL_RETENTION_DAYS` and
  `BACKUP_TIMEOUT_MINUTES` (longest dump, 120) in `.env`; from source, `backup_dir` (required
  for an automatic backup), `backup_time`, `backup_local_retention_days` and
  `backup_timeout_minutes`. Any failure (full disk, stuck dump, unreachable destination, worker
  killed until the task is given up) alerts the super admins, by email and in
  **Administration > Health**. One backup at a time: the worker's, a cron job's, a
  deployment's and a manual one wait for each other (saying so, for twice the dump timeout at
  most), and a restore refuses to start while a backup runs. Several workers queue a single
  backup.
- **`synaptane backup setup`**: the encrypted off-site copy in one command, question by
  question (`docker compose exec -it app synaptane backup setup`): encryption key (a new pair
  whose private key is shown once and saved nowhere, or your own public key), storage (rclone's
  wizard), destination tested before it is saved, first backup. Keys and destinations are saved
  in the data volume (`/data/backup/`), without editing `.env` or restarting;
  `BACKUP_AGE_RECIPIENTS` and `BACKUP_REMOTES` still work and add to them. The same steps
  without questions: `synaptane offsite key [PUBLIC_KEY]`, `synaptane offsite add NAME:PATH`,
  `synaptane offsite remove NAME:PATH`.
- **`synaptane backup check`**: the state of every piece of the backups (automatic backup,
  folder, free space, retention, last archive, encryption key, each destination tested with a
  probe file unless `--no-test`, last off-site copy) and, for what is missing, the exact
  command to run. Exit code 1 if something fails.
- **Back up now** in **Administration > Health**: a backup by the worker, right away, with its
  off-site copy. Written to the audit log (`instance.backup_requested`).

### Changed
- **Administration > Health** says how backups are made (automatically, by the server's cron
  job, or not at all), shows the worker's last run (waiting, in progress, done, skipped because
  another backup was running, failed and why) and, when the backup or the off-site copy is
  missing or late, the command to run and a link to the documentation. Destinations without an
  encryption key are reported. `synaptane doctor` reports the automatic backup too.
- **`deploy/host/backup.sh`**: existing cron jobs keep working unchanged. Once its archive is
  checked and in place, the script marks it as verified (and, for the health page, as the last
  archive) instead of `synaptane backup`. Run without a label (the cron job), it also says
  while it runs that it is running, and that it made the backup of the day: the worker's
  automatic backup then steps aside, and takes over by itself, the same day, once the last good
  backup of the cron job ended more than 26 hours ago and no run is in progress (a run marked
  for more than 12 hours counts as killed). A night the cron job is only slow, the worker waits
  for it; if another backup holds the lock, it waits for it too, without giving up the day.
- **`synaptane backup`** writes its archive to a hidden `.part` file renamed once read back,
  also in a folder. A backup sent to standard output is marked as verified only once it has
  gone through, and never as the backup of the day (with `deploy/host/backup.sh`, the script
  marks it once in place). A second backup in the same second into a folder gets a suffix
  (`-2`) instead of failing; an explicit file is still never overwritten.
- The backup documentation starts with the simplest path, then the options.
- **New space** without JavaScript: the repository address is now required by the server when
  the repository is not created by Synaptane (the browser already required it).
- REST: a malformed request body (422) no longer quotes the values received in `detail`
  (`input`, `ctx`), only `loc`, `msg` and `type`: they could hold a token or a key sent in the
  same body.
- **Members of a space**. A **By a group** section lists the groups with a role on the space
  and their members; an administrator gives a group access, or removes it, from there. The list
  of who accesses the space through the organization says which group gives each person their
  access ("Contributor, through the group HR"), and a member added to the space with more
  through a group says so next to their role. Giving a group access to a restricted space says
  that the group opens it; a role that the default role already gives is flagged. Removing
  someone from the space says what they keep: the access of their groups (which ones), of the
  default role, or of an administrator.
  `GET …/spaces/{space}/members` keeps its form and still lists only the members added to the
  space.
- **Members of an organization**. Each member's groups are shown next to their role, and the
  confirmation of a removal says which groups they leave.

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

[Unreleased]: https://github.com/synaptane/synaptane/compare/v1.16.1...HEAD
[1.16.1]: https://github.com/synaptane/synaptane/compare/v1.16.0...v1.16.1
[1.16.0]: https://github.com/synaptane/synaptane/compare/v1.15.0...v1.16.0
[1.15.0]: https://github.com/synaptane/synaptane/compare/v1.14.0...v1.15.0
[1.14.0]: https://github.com/synaptane/synaptane/compare/v1.13.0...v1.14.0
[1.13.0]: https://github.com/synaptane/synaptane/compare/v1.12.0...v1.13.0
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
