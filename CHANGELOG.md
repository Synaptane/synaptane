# Changelog

All notable changes to Synaptane are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Before each upgrade, back up your instance (see `docs/admin/backup.md`) and read the section
of the target version.

## [Unreleased]

### Added
- **Last change in search results**: when, by whom, and whether an AI wrote it (the tidy-up,
  a routine or an assistant). In the web search (date, author, **AI** badge), the REST API
  (`edited`, `edited_by`, `edited_by_ai`) and the MCP tools (`edited`, `edited_by`, `ai` in
  `search` and `fetch`, `edited:` in the `read` header). The server guidance asks assistants
  to be more careful with a page last written by an AI and not verified since. Computed from
  the Git history in one walk per space, then only the new commits.

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

[Unreleased]: https://github.com/synaptane/synaptane/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/synaptane/synaptane/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/synaptane/synaptane/compare/v1.0.0-rc.2...v1.0.0
[1.0.0-rc.2]: https://github.com/synaptane/synaptane/compare/v1.0.0-rc.1...v1.0.0-rc.2
[1.0.0-rc.1]: https://github.com/synaptane/synaptane/releases/tag/v1.0.0-rc.1
