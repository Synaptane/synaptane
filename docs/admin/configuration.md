English | [Français](configuration.fr.md)

# Configuration

Configuration and administration of an installed instance (see
[Install and run Synaptane](installation.md)).

`init` writes `synaptane.toml` (mode 600) and generates the **master key** `master.key` in the
data folder. With Docker, configuration goes through environment variables and the key is
generated on first start. This key encrypts the secrets stored in the database, such as Git
credentials and AI keys. **Back it up outside the database**: without it, these secrets are
lost. See [Backup and restore](backup.md).

Each option of `synaptane.toml` can be overridden by an environment variable
`SYNAPTANE_<OPTION>`, for example `SYNAPTANE_DATABASE_URL`. The file is chosen with
`--config` or `SYNAPTANE_CONFIG`.

| Option | Purpose |
|---|---|
| `public_url` | Public URL. With `https://`, cookies are marked `Secure`. It is also the OAuth issuer and the MCP address (`<public_url>/mcp`): for Claude.ai, it must use HTTPS and be reachable from the Internet. |
| `database_url` | PostgreSQL connection (`postgresql+psycopg://…`). |
| `data_dir` | Master key and local copies of the repositories. |
| `default_locale` | Default language of the interface: `en` (default) or `fr`. It applies when the browser asks for neither, and to accounts created by an administrator or the CLI; everyone can change their own in **My account**. Variable: `SYNAPTANE_DEFAULT_LOCALE`. |
| `session_days` | Length of a web session, 30 days by default. |
| `superadmin_2fa` | Super admins who sign in with a password must turn on two-factor authentication (an authenticator app) before anything else: `true` by default. Single sign-on leaves the second factor to the identity provider. `false` only for a demo or test instance (`examples/compose.demo.yaml` sets it). |
| `session_idle_days` | A web session not used for this many days ends: 7 by default, `0` for no limit. Each person sees their sessions in **My account → Security** and can close them. |
| `poll_minutes` | Interval of the fallback sync, 5 minutes by default. |
| `git_timeout` | Overall timeout of an exchange with the Git host (clone, fetch, push), in seconds, 300 by default. Raise it only for the first clone of a very large repository. See [Git](git.md). |
| `allow_local_repos` | Allows `file://` repositories. **Development only.** |
| `ocr_languages` | Tesseract languages, `fra+eng` by default. |
| `ocr_images` | Extract text from images (OCR), `true` by default. |
| `allow_private_webhooks` | Allows webhooks over HTTP and to the local network. **Development only.** |
| `allow_insecure_imap` | Allows the [capture by email](email-capture.md) to read its mailbox over an unencrypted IMAP connection. **Development only** (a local test server). |
| `routine_sources` | Routines may read the [external HTTP sources](ai.md#external-sources) the organization's administrators declare in a space, `true` by default: only administrators declare a source (address and secret), and a routine's file only names those it reads. `false` turns the feature off for the whole instance: the routines then run without the `fetch` tool, and their page says so. With Docker: `ROUTINE_SOURCES` in `.env`. |
| `api_docs` | Publishes the interactive documentation `/api/docs` and the schema `/api/v1/openapi.json`, `false` by default. |
| `metrics_token` | Bearer token of `/metrics` for Prometheus, 32 characters or more (see [Monitoring](#monitoring)). Empty by default: `/metrics` does not exist. With Docker: `METRICS_TOKEN` in `.env`. |
| `backup_dir` | Folder of the nightly backup made by the worker (`/backups` in the image, `SYNAPTANE_BACKUP_DIR`). Unset: no automatic backup. See [Backup](backup.md). |
| `backup_time` | Time of the nightly backup, server time: `04:00` by default (`4:00`, `04h00` are read too); `off`: none. A value that is not a time gives 04:00, with a warning. |
| `backup_local_retention_days` | Days the worker's archives of `backup_dir` are kept, 7 by default (the 3 newest always); `0`: all. |
| `backup_timeout_minutes` | Longest a database dump, or its read-back, may take: 120 minutes by default. |
| `backup_age_recipients` | age public keys (`age1…`) the off-site copy is encrypted for, in addition to those saved by `backup setup`. As an environment variable: comma-separated. |
| `backup_age_recipients_file` | Or a file of age recipients. |
| `backup_remotes` | rclone destinations of the off-site copy (`name:path`, or a folder), in addition to those saved by `backup setup`. |
| `backup_retention_days` | Older remote archives are deleted, 30 days by default; `0`: never. |
| `backup_rclone_config` | rclone configuration. Defaults to `$RCLONE_CONFIG` (`/data/rclone/rclone.conf` with Docker), otherwise `<data_dir>/rclone/rclone.conf`. |

With Docker Compose, these variables are set in `.env` (see the comments in `.env.example`):

| Variable | Purpose |
|---|---|
| `PUBLIC_URL`, `POSTGRES_PASSWORD` | Public address and database password (required). |
| `COMPOSE_PROFILES` | Empty (behind the server's nginx) or `proxy` (all-in-one). |
| `COMPOSE_FILE` | Compose files, separated by `:`. Unset: `compose.yaml`, plus `compose.override.yaml` if it exists. |
| `SYNAPTANE_VERSION` | Tag of the published images, the version of `compose.yaml` by default. |
| `SYNAPTANE_IMAGE`, `SYNAPTANE_NGINX_IMAGE` | Another image for Synaptane, or for nginx in proxy mode (`synaptane:local`, `synaptane-nginx:local` for a build). |
| `GIT_SHA`, `AGENT_CLIS` | Build only (`compose.build.yaml`): commit recorded in the image, subscription CLIs (`true` by default). |
| `APP_PORT`, `APP_BIND`, `FORWARDED_ALLOW_IPS` | Default mode: local port (8000), its address (`127.0.0.1`), trusted proxy networks. |
| `DOMAIN`, `LETSENCRYPT_EMAIL`, `LETSENCRYPT_STAGING`, `HTTP_PORT`, `HTTPS_PORT` | Proxy mode: domain, Let's Encrypt account and test certificates, published ports (80 and 443). |
| `BACKUP_TIME`, `BACKUP_LOCAL_RETENTION_DAYS`, `BACKUP_TIMEOUT_MINUTES` | Nightly backup: time (`04:00`, `off`: none), days kept (7), longest dump (120 minutes). See [Backup](backup.md). |
| `BACKUP_AGE_RECIPIENTS`, `BACKUP_REMOTES`, `BACKUP_RETENTION_DAYS` | Off-site copy, without `backup setup` (see [Backup](backup.md)). |
| `SYNAPTANE_DEFAULT_LOCALE`, `OCR_LANGUAGES`, `TZ` | Interface language, OCR languages, time zone. |
| `SYNAPTANE_WORKER_MEMORY`, `SYNAPTANE_LOG_LEVEL` | Worker memory cap (`2g`), log level. |
| `ROUTINE_SOURCES` | `false`: no routine reads an [external source](ai.md#external-sources) (`true` by default). |

## Email, invitations and password reset

**Administration → Email**: SMTP server, port, security (STARTTLS or SSL), username,
password (encrypted, never shown again) and sender address. **Send a test email** checks the
setting, to your account's address.

With email set up:
- an organization admin **invites** people from the **Members** tab. They choose the address,
  the role in the organization and, optionally, a role per space and **groups** to join (see
  [Roles](#roles) below). The person gets a link valid for 14 days and creates their account.
  If the address is the **verified** address of an existing account, access is granted at
  once;
- **Forgot your password?** on the sign-in page sends a link valid for 30 minutes.

Without email, invitations still work: the link is shown once to the person who invites, who
passes it on. Password resets then go through a super admin (Administration → Accounts).

A password chosen by an admin, when creating the account or resetting it, must be changed at
first sign-in. People sign in with their username or their email.

**Verified addresses.** An address entered in **My account** or by an admin stays
"unverified" until it is confirmed. The link sent to that address (valid for 3 days) must be
opened while signed in to the account that asked for it, then confirmed with a button: a link
opened by someone else, or by a mail scanner, verifies nothing. Only a verified address is
used for invitations to an existing account, single sign-on linking and password reset.
Changing your email requires your current password, and the old address is notified. An
address that was entered but never verified does not block its real owner: it goes back to
them when they use it (invitation, single sign-on).

**Sending limits.** Each of these emails goes to an address typed in by someone who may not
own it, so their number is capped: 3 verification links per account per hour, and 30
invitations per organization per hour (counted whether or not the email is sent). Beyond
that, the request is refused (HTTP 429) until the hour has passed.

**Sign-in attempts.** After 10 failures in 15 minutes from the same IP address, the account is
blocked for that address; after 50 failures from all addresses, for everyone. Variants of a
login (spaces, case, email) count for the same account. The log names the account by its
identifier; a login that matches no account is logged as a short fingerprint
(`login:` and 8 hexadecimal characters), never as typed, since it may be a password entered
in the wrong field.

**Capture by email.** **Administration → Email → Capture by email** turns emails sent to
`brain+organization.space@example.com` into inbox notes, read by the worker from a dedicated
IMAP mailbox: see [Capture by email](email-capture.md).

## Search index

Text extracted from PDFs, Office documents and images is stored outside the repositories, in
`data_dir/extract/`.

Each document is read by a separate, bounded process, so that a malicious or oversized file
(large-format plan, scan of hundreds of pages) cannot bring the worker down:
- 1 GB of memory at most (Tesseract and catdoc included) and 10 minutes per document;
- pages rendered for OCR capped at 40 million pixels (300 dpi for a common format, less for
  a large format);
- 500 pages read, of which at most 100 go through OCR, 60 seconds per page; beyond that, the
  native text stays indexed and the worker log says so;
- images over 250 million pixels are skipped.

A file that fails (timeout, memory, invalid file) is recorded as such in
`data_dir/extract/<space>/manifest.json`, with the reason, and is not retried until it
changes: it is recorded before it is even read, so a worker stopping halfway does not repeat
forever (two attempts at most). `synaptane reindex` retries everything, failures included.

The index can be rebuilt from Git at any time:

```sh
uv run synaptane reindex                  # all spaces
uv run synaptane reindex --space acme/infra
docker compose exec app synaptane reindex   # with Docker
```

## Alerts

The admins of an organization are notified, by email if it is set up and by the `space.alert`
webhook, when something needs their attention, for example:
- the tidy-up of a space keeps failing (not just a network glitch), or stops before the end;
- a space no longer syncs with its repository (credentials refused, repository not found);
- an inbox holds items that nobody has looked at for more than 3 days;
- the tidy-up agent reaches the deletion cap, or makes large changes;
- a space's Git access token expires in 30 days, 7 days, tomorrow, or has expired (see
  [Token expiry](git.md#token-expiry)).

The full list of alert kinds is in [Webhooks](webhooks.md). The same alert (kind and space)
is not sent again within 20 hours; the expiry of a token is notified once per step. The AI budget has its own alerts (see
[AI and tidy-up](ai.md)).

The super admins are notified, by email if it is set up, when the nightly backup or its
off-site copy fails (see [Backup](backup.md)); the alert also shows in **Administration >
Health**.

## Monitoring

**Administration > Health** (`/admin/health`, super admins) shows the instance on one page:
database, master key, worker, background tasks, last backup, off-site copy, disk, email sending,
HTTPS and the Git access tokens expiring within 30 days or expired (with a link to each space's
Git page), then the sync and tidy-up of each space, then the system alerts of the last 30 days.
A backup counts once it is written and verified, wherever it went; an off-site copy, once every
destination has it. Copies made before 1.11.0 are not seen.

`/healthz` answers as soon as the process runs; `/readyz` also checks the database (the Docker
healthcheck uses it). Neither needs a sign-in.

For Prometheus, or any tool that reads its format (Grafana Alloy, VictoriaMetrics…),
`/metrics` gives the state of the instance. It is off until you set a token of 32 characters
or more, then restart:

```sh
openssl rand -hex 32   # the token
```

With Docker, put it in `.env` (`METRICS_TOKEN=…`), then `docker compose up -d`; from source,
`metrics_token` in `synaptane.toml`. The scraper sends it as a bearer token:

```yaml
scrape_configs:
  - job_name: synaptane
    scheme: https
    authorization:
      credentials_file: /etc/prometheus/synaptane.token
    static_configs:
      - targets: ["synaptane.example.com"]
```

`/metrics` goes through nginx like the rest of the application: the token is what protects it,
keep it like a password. The spaces appear as `organization/space`; no page content and no
person.

| Metric | What it measures |
|---|---|
| `synaptane_info{version,commit}` | Running version and commit (always 1). |
| `synaptane_database_up` | 1 if the database answers. At 0, the metrics that come from it are missing. |
| `synaptane_database_connections_in_use` | Database connections of the web server in use (20 at most). |
| `synaptane_http_requests_total{area,code}` | Requests answered since the start, by area (`web`, `api`, `mcp`, `oauth`, `static`) and status class (`2xx`, `4xx`…). |
| `synaptane_http_request_duration_seconds{area}` | Response times (histogram). |
| `synaptane_users{state}` | Accounts, `active` or `disabled`. |
| `synaptane_space_sync_status{space,status}` | 1 for the current sync state of each space: `ok`, `error`, `conflict` or `never`. |
| `synaptane_space_last_sync_timestamp_seconds{space}` | Last successful sync. |
| `synaptane_space_inbox_items{space}` | Items waiting in the inbox for the tidy-up. |
| `synaptane_space_last_tidy_timestamp_seconds{space}` | End of the last tidy-up pass. |
| `synaptane_space_last_tidy_failed{space}` | 1 if that pass failed. |
| `synaptane_jobs{kind,status}` | Worker jobs `queued` or `running`. |
| `synaptane_jobs_waiting_seconds` | Wait of the oldest job due to run: it grows when the worker is stopped or overwhelmed. |
| `synaptane_jobs_abandoned_24h{kind}` | Jobs abandoned in the last 24 hours, attempts used up. |
| `synaptane_ai_spend_month_euros{organization}` | Estimated AI spending since the 1st of the month (UTC). |
| `synaptane_backup_last_timestamp_seconds` | Last verified archive of `synaptane backup`, wherever it was written. |
| `process_*` | CPU, resident memory, open files and start time of the web server. |

Examples of alert rules:

```yaml
groups:
  - name: synaptane
    rules:
      - alert: SynaptaneWorkerStuck
        expr: synaptane_jobs_waiting_seconds > 900
        for: 5m
      - alert: SynaptaneSyncError
        expr: synaptane_space_sync_status{status=~"error|conflict"} == 1
        for: 30m
      - alert: SynaptaneNoRecentBackup
        expr: time() - synaptane_backup_last_timestamp_seconds > 2 * 86400
```

## Audit log

Who changed a role, a token or a setting, and when. Synaptane records these changes as they
happen; two pages read them, newest first, 50 per page (**Older** for the next ones):

- **Audit log** tab of an organization (`/o/<organization>/-/journal`, its admins): its members
  and roles, invitations, settings (organization, AI, semantic search, webhooks, spaces, Git
  access, tidy-up, purpose, assistant prompts, routines) and the alerts sent to its admins. Nothing of another
  organization.
- **Administration > Audit log** (`/admin/journal`, super admins): the whole instance, with
  what belongs to no organization: accounts (creation, super admin rights, disabling,
  password, two-factor authentication, sessions), personal tokens, connected applications,
  single sign-on and email sending. An organization filter keeps one of them, or what belongs
  to none.

Each line gives the date, the author (**System** for what Synaptane does by itself, such as an
alert), the action, what it is about (space, member, token…) and its useful details: role,
provider and model, "API key replaced". Never a secret: no key, no token, no password, and of
a webhook only its host. Filter by type (members and roles, tokens and applications, settings,
accounts and security, alerts) and by period (dates in UTC); **Export (CSV)** downloads the
selection, its 10,000 newest lines at most (a last line says when it stopped there).

Page edits, inbox notes, tidy-up runs and AI costs are not in it: they have their own pages
(**Activity** and **Tidy-up** of each space, **Artificial intelligence** of the organization).
The events of a deleted space stay in its organization's log, without its name. Changes recorded
before this page existed are there too, with fewer details (who became super admin, which key
changed).

## Roles

| Role | Scope | Can |
|---|---|---|
| Super admin | Instance | Everything: accounts, all organizations, and reading and writing all content in the web UI and the REST API. Through an assistant (MCP), only their actual memberships. |
| Organization admin | Organization | Create and configure spaces, manage members. Contributor on every space of the organization. |
| Contributor | Space | Read, search, drop notes into the inbox, edit content. |
| Reader | Space | Read and search. |

Adding someone to a space automatically makes them a plain member of the organization.
Removing them from the organization also removes them from its spaces and groups.

By default, a member only accesses the spaces they are added to. To give every member a role
on all the spaces of the organization at once, choose a **default role in the spaces**
(reader or contributor) in the organization's **Settings**: each member then has the higher
of that role and the one given in a space. Mark a space **restricted** (space **Settings →
Members**, or from its creation in **New space**) to keep it to its own members, for human
resources or management; the members page of a space shows who accesses it by default and
who was added to it.

**With a default role, adding someone to one space opens the others.** A person who is not a
member of the organization yet becomes one when you add them to a space (or invite them):
they then also get the default role on every space of the organization that is not
restricted, not only on the space you chose. The add form of a space and its confirmation say
so. To give someone access to one space only, keep the others restricted, or leave the
default role at "no access". Removing someone from a space that is not restricted leaves
them its default role: the confirmation says so too. In the invitation form, a space that is
not restricted offers the default role as a floor ("Reader (by default)") and only the roles
above it; the invitation page lists the access actually given, default role included.

**Groups: one team, several spaces.** To give some members access to several spaces at once,
beyond the default role, create a **group** in the organization's **Groups** tab (human
resources, management, a project team): add its members (members of the organization only:
invite the others, checking the group in the invitation form), then give it a role, reader or
contributor, on each space it opens.
Each member of the group then has the highest of that role, the one given to them in the space
and the default role. **A group opens a restricted space too**: restrict the human resources
space, then give access to the "HR" group only, and nobody else in the organization sees it.
You can also give a group a role from a space's **Settings → Members**, section **By a group**.
On a space that is not restricted, a group's role below the default role adds nothing: the
confirmation warns about it. The members page of a space lists the groups that access it and
says, for each person, whether their access comes from the space itself, from a group (which
one), from the default role or from the administration of the organization. The **Members**
tab of the organization shows each member's groups; removing someone from the organization
removes them from its groups (the confirmation says which). Deleting a group removes the
access it gave, unless its members have it otherwise; deleting a space removes the groups'
roles on it. Groups are managed through the REST API too (`/organizations/{org}/groups`), and
every change is written to the audit log.

**Inviting someone straight into groups.** The invitation form of the **Members** tab lists
the organization's groups, each with what it gives ("Contributor on HR"): check those the
person joins. When they accept, they become a member of the organization, then of the checked
groups, in one go; an existing account joins them at once, and its notice email lists the roles
given in spaces and the groups joined. Before creating their account, the person sees their
real access on the invitation page, groups included ("HR: contributor, through the group
“Management”"), and the **Pending invitations** list shows the groups of each
invitation. A group deleted before the acceptance is simply not joined, and a renamed group is
joined under its new name. Canceling an invitation drops its groups; inviting the same address
again replaces them with the new choice. Only groups of the organization can be chosen: the
database refuses any other, and so is a group deleted while the invitation is being sent. The
audit log keeps the groups, by their names at the time, in the **Invitation sent** event and
in the **Existing account added by invitation** event (there, only the groups actually
joined, not those the account was already in); the acceptance writes one **Added to a group**
event per group, by the system, like the other accesses it grants. Through the REST API:
`"groups": [<id>, …]` (200 at most) in `POST /organizations/{org}/invitations`.

**Next step:** the [first steps](first-steps.md): organizations, spaces, members, demo
data.
