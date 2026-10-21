# Changelog

All notable changes to Synaptane are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Before each upgrade, back up your instance (see `docs/admin/backup.md`) and read the section
of the target version.

## [Unreleased]

### Changed
- Semantic search with Voyage AI: pages and queries are embedded with the prompts Voyage
  prepends for its `input_type` ("document" and "query"), which its models are tuned for.
  Vectors made without them stay compatible; `synaptane reindex` rebuilds an existing index
  with them.
- Agent CLIs of the image (subscription mode): Claude Code 2.1.303 and Codex 0.163.0. Codex's
  new `browser_annotation_api` feature is turned off on each run; its other new features
  (service tiers, access programs, interruptions, desktop voice) open nothing.

### Security
- Pillow 13.0.0: fixes in the image decoders (JPEG, TIFF, GIF, PDF, EPS), which open the
  images uploaded for OCR.

### Added
- Prices of the Voyage 4 embedding models (`voyage-4-large`, `voyage-4`, `voyage-4-lite`), so
  that their indexing is counted at its real cost instead of the table's highest price.

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

[Unreleased]: https://github.com/synaptane/synaptane/compare/v1.0.0-rc.1...HEAD
[1.0.0-rc.1]: https://github.com/synaptane/synaptane/releases/tag/v1.0.0-rc.1
