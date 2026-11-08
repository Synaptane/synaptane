English | [Français](installation.fr.md)

# Install and run Synaptane

Two ways to install:
- **Docker Compose** (recommended), on a Linux server, with the published image or an image
  you build yourself;
- **from source**, for development or for a server without Docker.

Synaptane runs on Linux, or WSL2 on Windows, on x86-64 or arm64 (ARM servers). macOS is not
tested.

## With Docker Compose

Requirements:
- Docker Engine with the Compose plugin;
- a domain name that points to the server: HTTPS is required for cookies, OAuth, single
  sign-on and the Claude.ai and ChatGPT connectors.

The published images exist for x86-64 and arm64: Docker pulls the one of the server. The image
`ghcr.io/synaptane/synaptane` contains everything else: Git, ripgrep,
Tesseract (English and French), catdoc (old Office files), the PostgreSQL client for backups,
age and rclone. It does **not** contain the Claude Code and Codex CLIs, needed only to run the
AI agent on a Claude or ChatGPT subscription: for that,
[build the image yourself](#build-the-image-yourself).

**Get the files and start.** The repository provides `compose.yaml`, `.env.example`, the nginx
configuration and the server scripts. Clone the release, then start the published image:

```sh
git clone --branch v1.12.0 https://github.com/synaptane/synaptane.git ~/synaptane
cd ~/synaptane
cp .env.example .env
chmod 600 .env                # it holds the database password
openssl rand -hex 24          # copy into POSTGRES_PASSWORD
nano .env                     # PUBLIC_URL, POSTGRES_PASSWORD, mode (see below)
docker compose pull
docker compose up -d
docker compose exec app synaptane admin create --username admin --display-name "Admin"
docker compose exec app synaptane doctor
```

Git says the copy is in "detached HEAD" state: this is normal after cloning a tag, and you
have nothing to commit there.

`admin create` asks for the password, at least 12 characters long.

`SYNAPTANE_VERSION` picks the image tag. `.env.example` sets it to the release you cloned, and
`compose.yaml` falls back to that same version if it is missing: the image and the Compose
file always match. There is no `latest` to follow: an upgrade is always a choice (see
[Upgrade](#upgrade)).

`doctor`, `synaptane --version` and the footer of the administration page show the version
and commit in service (for example `1.0.0rc1 (abc1234)`).

The services:
- `postgres`: PostgreSQL 17 with pgvector;
- `migrate`: applies the migrations, then stops;
- `app`: web, REST API and MCP;
- `worker`: Git sync, indexing, tidy-up, routines and webhooks.

Data lives in Docker volumes:
- `postgres-data`;
- `synaptane-data`: master key, repository clones, extracted text;
- `synaptane-agents`: CLI logins for subscriptions, mounted in the worker only;
- `synaptane-backups`: where `synaptane backup` writes without `--output`. It is on the same
  disk and **`docker compose down -v` deletes it** with the others: copy backups off the
  server (see [Backup](backup.md)).

### Build the image yourself

Build from a release tag if you want the Claude Code and Codex CLIs (subscriptions), or if you
prefer not to pull a prebuilt image. `compose.yaml` alone never builds: the build is declared
in `compose.build.yaml`, which you add through `COMPOSE_FILE`.

```sh
git clone --branch v1.12.0 https://github.com/synaptane/synaptane.git ~/synaptane
cd ~/synaptane
cp .env.example .env && chmod 600 .env
nano .env                     # as above, plus these two lines:
                              #   COMPOSE_FILE=compose.yaml:compose.build.yaml
                              #   SYNAPTANE_IMAGE=synaptane:local
GIT_SHA=$(git rev-parse HEAD) docker compose up -d --build   # commit recorded in the image
docker compose exec app synaptane admin create --username admin --display-name "Admin"
```

The build takes about ten minutes. `AGENT_CLIS=true` (the default) installs Claude Code and
Codex, which are third-party software under their own terms; `AGENT_CLIS=false` in `.env`
leaves them out.

Keep a local name such as `synaptane:local` (`compose.build.yaml` uses it when
`SYNAPTANE_IMAGE` is unset), never the name of the published image: the next
`docker compose pull` would replace your build. In proxy mode, the nginx image is built too,
as `synaptane-nginx:local`. To update only the downloaded images (PostgreSQL, certbot), use
`docker compose pull --ignore-buildable`.

### Choose the mode: behind the server's nginx, or all-in-one

**Default mode: the server already runs nginx** (or Caddy, Traefik…) for other sites. Leave
`COMPOSE_PROFILES` empty. Synaptane then listens on `127.0.0.1:8000` (`APP_PORT`), reachable
only from the server, and the host's nginx handles HTTPS:

```sh
sudo LETSENCRYPT_EMAIL=you@example.com deploy/host/install-nginx.sh brain.example.com
```

The script installs the site `deploy/nginx/host-site.conf` with your domain, after getting the
Let's Encrypt certificate (certbot in *webroot* mode, without touching other sites): HTTP
redirected to HTTPS, HSTS, `server_tokens off`, access log without tokens. Put
`APP_PORT=…` before the command if Synaptane does not listen on 8000. Without the script: copy
this file to `/etc/nginx/sites-available/`, replace `brain.example.com`, and get the
certificate before enabling the 443 block.

The site turns off buffering (`proxy_buffering off`) for MCP and accepts 100 MB uploads. Do
not publish port 8000 on the Internet (keep `APP_BIND` at `127.0.0.1`): the application trusts
the `X-Forwarded-*` headers that come from Docker networks (`FORWARDED_ALLOW_IPS`,
`172.16.0.0/12,192.168.0.0/16` by default). If the log shows the same address for every
visitor, this setting does not match your network, and the login attempt limit would count
everyone together.

**All-in-one mode: nothing else runs on the server.** In `.env`:

```sh
COMPOSE_PROFILES=proxy
DOMAIN=brain.example.com
LETSENCRYPT_EMAIL=you@example.com
```

An nginx container listens on ports 80 and 443:
- certbot gets the Let's Encrypt certificate, then renews it automatically;
- nginx serves a temporary self-signed certificate meanwhile, then reloads the real one as
  soon as it arrives (about a minute);
- for a first try, `LETSENCRYPT_STAGING=1` gives test certificates, without hitting Let's
  Encrypt rate limits.

The domain must already point to the server, with ports 80 and 443 open. If it fails, certbot
retries every hour (`docker compose logs certbot`).

To switch modes, change `COMPOSE_PROFILES`, then run `docker compose up -d --remove-orphans`.

### Upgrade

Read the [changelog](../../CHANGELOG.md) for the target version first. Migrations run on
their own at startup (the `migrate` service logs each one). Expect a few tens of seconds of
downtime.

**With the published image:**

```sh
cd ~/synaptane
umask 077   # the backup contains the master key
docker compose exec -T app synaptane backup --output - > synaptane-before-upgrade.tar.gz
git fetch --tags && git checkout v1.12.0      # Compose file and scripts of the new version
nano .env                                    # SYNAPTANE_VERSION=1.12.0
docker compose pull
docker compose up -d
docker compose exec app synaptane --version  # the expected version?
```

**With a self-built image** (`COMPOSE_FILE` and `SYNAPTANE_IMAGE` set as in
[Build the image yourself](#build-the-image-yourself)), `deploy/host/deploy.sh` does it all on
the server:

```sh
cd ~/synaptane
deploy/host/deploy.sh v1.12.0      # a tag, a branch or a commit
```

The script takes a backup, keeps the image in service, builds the new one with its commit,
restarts, then checks for up to 3 minutes (`/readyz`, version, worker, `doctor`). **If
something fails, it rolls back on its own**: previous commit and image, and restore of the
backup if a migration changed the database. Each deployment adds a line
`date commit result` to `~/.local/state/synaptane/deploy.log`. `DRY_RUN=1` shows what would
be done; `--check-ci` refuses a commit whose CI is not green. Details:
[`deploy/host/README.md`](../../deploy/host/README.md).

By hand, if the script does not suit you:

```sh
umask 077
docker compose exec -T app synaptane backup --output - > synaptane-before-upgrade.tar.gz
git fetch --tags && git checkout v1.12.0
docker compose pull --ignore-buildable      # downloaded images: PostgreSQL, certbot
GIT_SHA=$(git rev-parse HEAD) docker compose build --pull --no-cache  # with Debian fixes
docker compose up -d
docker compose exec app synaptane --version
```

Upgrade at least once a month to get operating system security fixes: with the published
image, move to the latest release (each one is built with the Debian fixes of the day); with a
self-built image, rebuild with `--pull --no-cache`, even without a new version (`deploy.sh`
rebuilds with `--pull`). Downloaded images (PostgreSQL, certbot) are updated separately, after a backup:
`docker compose pull --ignore-buildable && docker compose up -d`.

### Roll back

To go back to an earlier version **with no migration in between**: with the published image,
set `SYNAPTANE_VERSION` back to it in `.env` and run `docker compose up -d`; with a self-built
image, deploy its tag or commit (it is in the log): `deploy/host/deploy.sh <commit>`.
`deploy.sh` already rolls back on its own when its checks fail.

If a migration changed the database in between, the old code does not start on the migrated
database: also restore the backup taken just before the faulty upgrade
(`synaptane-before-upgrade.tar.gz`, or with `deploy.sh`
`~/synaptane-backups/synaptane-…-avant-<faulty commit>.tar.gz`):

```sh
docker compose stop app worker
git checkout --detach v1.11.0                   # the previous version
nano .env                                       # published image: SYNAPTANE_VERSION=1.11.0
# self-built image: GIT_SHA=$(git rev-parse HEAD) docker compose build
docker compose run --rm --no-deps -T app synaptane restore --force - < synaptane-before-upgrade.tar.gz
docker compose up -d
```

The database is restored to the state of the backup taken before the upgrade (schema
included); the Git repositories remain the reference for content.

### Logs

`docker compose logs -f app worker`: one line per event, with the time. The worker writes one
line per job (sync, indexing, tidy-up: duration, result). `SYNAPTANE_LOG_LEVEL=DEBUG` in
`.env` gives more detail; `WARNING` gives less.

### Claude and ChatGPT subscriptions

This requires a [self-built image](#build-the-image-yourself) with the CLIs. They log in once,
in the worker container. The login is stored in the `synaptane-agents` volume, so it survives
upgrades.

```sh
docker compose exec worker claude auth login         # Claude subscription
docker compose exec worker codex login --device-auth # ChatGPT subscription
```

Check the provider's terms first: see [AI and tidy-up](ai.md).

### Other settings

The options below use `SYNAPTANE_<OPTION>` variables. To add one, create a
`compose.override.yaml` file. Compose reads it automatically only when `COMPOSE_FILE` is not
set; if `.env` sets `COMPOSE_FILE` (self-built image, demo), add it to the list, for example
`COMPOSE_FILE=compose.yaml:compose.build.yaml:compose.override.yaml`.

```yaml
services:
  app:
    environment:
      SYNAPTANE_SESSION_DAYS: "14"
  worker:
    environment:
      SYNAPTANE_SESSION_DAYS: "14"
```

## From source

Requirements: Python 3.12 with [uv](https://docs.astral.sh/uv/), and PostgreSQL 17 with the
pgvector extension.

```sh
uv sync
uv run synaptane init --database-url postgresql+psycopg://synaptane:PASSWORD@localhost:5432/synaptane \
                      --data-dir /var/lib/synaptane --public-url https://brain.example.com
uv run synaptane db upgrade
uv run synaptane admin create --username admin --display-name "Admin"
uv run synaptane doctor
uv run synaptane serve --host 127.0.0.1 --port 8000   # web, REST and MCP
uv run synaptane worker                                # background jobs
```

System tools:
- Git 2.28 or later;
- OpenSSH;
- **ripgrep** (`rg`), for search;
- **Tesseract**, optional but recommended: text of scanned PDFs and images (`tesseract-ocr`
  with `tesseract-ocr-eng` and `tesseract-ocr-fra`);
- **catdoc**, optional but recommended: text of the old Word and Excel files `.doc` and `.xls`
  (`catdoc` package, which also provides `xls2csv`; old `.ppt` files are read without it);
- the PostgreSQL client (`pg_dump`, `pg_restore`), same version as the server, for backups;
- `age` and `rclone`, for encrypted off-site backups.

`synaptane doctor` checks that they are present.

## Configuration

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
| `api_docs` | Publishes the interactive documentation `/api/docs` and the schema `/api/v1/openapi.json`, `false` by default. |
| `metrics_token` | Bearer token of `/metrics` for Prometheus, 32 characters or more (see [Monitoring](#monitoring)). Empty by default: `/metrics` does not exist. With Docker: `METRICS_TOKEN` in `.env`. |
| `backup_age_recipients` | age public keys (`age1…`) that `backup --offsite` encrypts for. As an environment variable: comma-separated. See [Backup](backup.md). |
| `backup_age_recipients_file` | Or a file of age recipients. |
| `backup_remotes` | rclone destinations of the off-site copy (`name:path`, or a folder). |
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
| `BACKUP_AGE_RECIPIENTS`, `BACKUP_REMOTES`, `BACKUP_RETENTION_DAYS` | Off-site backup (see [Backup](backup.md)). |
| `SYNAPTANE_DEFAULT_LOCALE`, `OCR_LANGUAGES`, `TZ` | Interface language, OCR languages, time zone. |
| `SYNAPTANE_WORKER_MEMORY`, `SYNAPTANE_LOG_LEVEL` | Worker memory cap (`2g`), log level. |

## Email, invitations and password reset

**Administration → Email**: SMTP server, port, security (STARTTLS or SSL), username,
password (encrypted, never shown again) and sender address. **Send a test email** checks the
setting, to your account's address.

With email set up:
- an organization admin **invites** people from the **Members** tab. They choose the address,
  the role in the organization and, optionally, a role per space. The person gets a link
  valid for 14 days and creates their account. If the address is the **verified** address of
  an existing account, access is granted at once;
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
- the tidy-up agent reaches the deletion cap, or makes large changes.

The full list of alert kinds is in [Webhooks](webhooks.md). The same alert (kind and space)
is not sent again within 20 hours. The AI budget has its own alerts (see
[AI and tidy-up](ai.md)).

## Monitoring

**Administration > Health** (`/admin/health`, super admins) shows the instance on one page:
database, master key, worker, background tasks, last backup, off-site copy, disk, email sending
and HTTPS, then the sync and tidy-up of each space, then the system alerts of the last 30 days.
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
invite the others first), then give it a role, reader or contributor, on each space it opens.
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

## First steps

On first start, the super admin's home page lists these steps.

1. **Administration**: the **Organizations** tab to create them (box checked by default: you
   become their admin, otherwise your assistants would see nothing), the **Accounts** tab to
   create accounts (each account has its own page: rights, password).
2. On the organization page, one page per topic:
   - **Spaces**, then **New space**: name, identifier, Git repository address;
   - **Members**: add people, choose admins;
   - **Groups**: give a team access to several spaces at once;
   - **Artificial intelligence**: the provider of the nightly tidy-up;
   - **Semantic search** and **Webhooks**, optional;
   - **Settings**: name, description, default role in the spaces, deletion.
3. On a space, the **Settings** tab: **Git repository** (address, access, webhook, status
   and conflicts), **Members** (reader or contributor, groups, restricted space), **Tidy-up** (time,
   time zone, model),
   **Semantic search** and **General**.

Deleting a space or an organization requires typing its identifier again.

To link a space to its repository, see [Git](git.md). For sign-in with GitLab, GitHub, Google
or OIDC, see [SSO](sso.md); to notify other tools, see [Webhooks](webhooks.md). All of this is
also possible through the [REST API](../reference/api.md).

## Demo data

To try Synaptane before linking your own repositories, `synaptane demo` loads the wiki of a
fictional IT team ([examples/acme-wiki](../../examples/acme-wiki/README.md)): 50 pages, a space
purpose, six inbox notes covering typical tidy-up cases, verified pages, a page to review and a
broken link. Eight files that are not Markdown are linked from the pages, to see what
Synaptane does with them: a PDF contract, a scanned delivery note (OCR), a Word checklist, a
slide deck, two spreadsheets (one of them in the old `.xls` format), a rack diagram (OCR) and an
HTML status report. Search for "Kestrel", a word that only appears in the scan (Tesseract
required, included in the Docker image). On an initialized and migrated instance, it creates
what is missing, then syncs and indexes the space:
- a `demo` super admin, whose password is printed once (or given by
  `SYNAPTANE_DEMO_PASSWORD`, or `--password`);
- the `acme` organization, with `demo` as admin;
- the `acme/it` space, backed by a local Git repository, `<data_dir>/demo/acme-it.git`.

Running it again changes nothing: an existing account keeps its password, and what you changed
in the space stays.

The repository is local (`file://`): Synaptane refuses it unless
`SYNAPTANE_ALLOW_LOCAL_REPOS=true` is set for the command, the server and the worker. **Demo
and development only**: this setting lets an organization admin link a space to any Git folder
of the server. Run the demo on a throwaway instance, not on your production one.

With Docker Compose, `examples/compose.demo.yaml` sets it for `app` and `worker`:

```sh
echo 'COMPOSE_FILE=compose.yaml:examples/compose.demo.yaml' >> .env
docker compose up -d
docker compose exec app synaptane demo
```

With a self-built image: `COMPOSE_FILE=compose.yaml:compose.build.yaml:examples/compose.demo.yaml`.

From source:

```sh
export SYNAPTANE_ALLOW_LOCAL_REPOS=true   # in the shells of serve and worker too
uv run synaptane demo
```

The command prints what it created (`super admin demo`, `organization acme`…), the address to
open, the `demo` password and how to connect an AI assistant. It also says what keeps the demo
repository in sync: in a container, the Compose `worker` service, to leave running; from source,
a `synaptane worker` to keep running, with `SYNAPTANE_ALLOW_LOCAL_REPOS=true`. Tidying the inbox
needs an AI provider (organization page, **Artificial intelligence**). `docker compose down -v`
deletes the whole demo instance.

## Appendix: private fork

If you deploy from your own **private** fork, `git clone https://…` asks for credentials and
fails on a server. Give the server a **deploy key** instead: its own SSH key, read-only,
declared on that repository only (GitHub → Settings → Deploy keys, without write access). Then
clone through an SSH host alias that uses only that key:

```sh
git clone git@<ssh-alias>:<account>/synaptane.git ~/synaptane
```

Creating the key and the SSH alias in `~/.ssh/config`: see
[`deploy/host/README.md`](../../deploy/host/README.md). The rest of the
installation is the same as [building the image yourself](#build-the-image-yourself).
