English | [Français](installation.fr.md)

# Install and run Synaptane

> [!TIP]
> **Just want to try it?** A throwaway instance on your own computer, with the demo data:
> [Try it locally in 2 minutes](../../README.md#try-it-locally-in-2-minutes), then
> [Demo data](first-steps.md#demo-data). This page installs a real instance.

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
git clone --branch v1.17.0 https://github.com/synaptane/synaptane.git ~/synaptane
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

**Backups.** The worker already makes a verified backup every night, in the `synaptane-backups`
volume. For the encrypted copy off the server, one command, which asks its questions one by one
(see [Backup](backup.md)):

```sh
docker compose exec -it app synaptane backup setup
```

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
- `worker`: Git sync, indexing, tidy-up, routines, webhooks and the nightly backup;
- `agents`: the Claude and ChatGPT subscription CLIs, apart from the worker, without the data
  or the database (see [The agents container](ai.md#the-agents-container)). With the
  published image, which has no CLI, it runs idle.

Data lives in Docker volumes:
- `postgres-data`;
- `synaptane-data`: master key, repository clones, extracted text;
- `synaptane-agents`: CLI logins for subscriptions, mounted in the agents container only (in
  the worker up to 1.14: same volume, the logins stay valid);
- `synaptane-agents-key`: the key the worker and the agents container share, created by the
  worker;
- `synaptane-backups`: the nightly backups (7 days kept), and `synaptane backup` without
  `--output`. It is on the same disk and **`docker compose down -v` deletes it** with the
  others: the encrypted off-site copy protects against that (see [Backup](backup.md)).

### Build the image yourself

Build from a release tag if you want the Claude Code and Codex CLIs (subscriptions), or if you
prefer not to pull a prebuilt image. `compose.yaml` alone never builds: the build is declared
in `compose.build.yaml`, which you add through `COMPOSE_FILE`.

```sh
git clone --branch v1.17.0 https://github.com/synaptane/synaptane.git ~/synaptane
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
git fetch --tags && git checkout v1.17.0      # Compose file and scripts of the new version
nano .env                                    # SYNAPTANE_VERSION=1.17.0
docker compose pull
docker compose up -d
docker compose exec app synaptane --version  # the expected version?
```

**With a self-built image** (`COMPOSE_FILE` and `SYNAPTANE_IMAGE` set as in
[Build the image yourself](#build-the-image-yourself)), `deploy/host/deploy.sh` does it all on
the server:

```sh
cd ~/synaptane
deploy/host/deploy.sh v1.17.0      # a tag, a branch or a commit
```

The script takes a backup, keeps the image in service, builds the new one with its commit,
restarts, then checks for up to 3 minutes (`/readyz`, version, worker, agents container,
`doctor`). **If
something fails, it rolls back on its own**: previous commit and image, and restore of the
backup if a migration changed the database. Each deployment adds a line
`date commit result` to `~/.local/state/synaptane/deploy.log`. `DRY_RUN=1` shows what would
be done; `--check-ci` refuses a commit whose CI is not green. Details:
[`deploy/host/README.md`](../../deploy/host/README.md).

By hand, if the script does not suit you:

```sh
umask 077
docker compose exec -T app synaptane backup --output - > synaptane-before-upgrade.tar.gz
git fetch --tags && git checkout v1.17.0
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
`deploy.sh` already rolls back on its own when its checks fail. Going back below 1.15 leaves
the `agents` container running, unused (the CLIs run in the worker again, with the same
logins): `docker compose up -d --remove-orphans` stops it (the automatic rollback of
`deploy.sh` does it).

If a migration changed the database in between, the old code does not start on the migrated
database: also restore the backup taken just before the faulty upgrade
(`synaptane-before-upgrade.tar.gz`, or with `deploy.sh`
`~/synaptane-backups/synaptane-…-avant-<faulty commit>.tar.gz`):

```sh
docker compose stop app worker
git checkout --detach v1.16.1                   # the previous version
nano .env                                       # published image: SYNAPTANE_VERSION=1.16.1
# self-built image: GIT_SHA=$(git rev-parse HEAD) docker compose build
docker compose run --rm --no-deps -T app synaptane restore --force - < synaptane-before-upgrade.tar.gz
docker compose up -d
```

The database is restored to the state of the backup taken before the upgrade (schema
included); the Git repositories remain the reference for content.

### Logs

`docker compose logs -f app worker agents`: one line per event, with the time. The worker writes one
line per job (sync, indexing, tidy-up: duration, result). `SYNAPTANE_LOG_LEVEL=DEBUG` in
`.env` gives more detail; `WARNING` gives less.

### Claude and ChatGPT subscriptions

This requires a [self-built image](#build-the-image-yourself) with the CLIs. They run in the
`agents` container, apart from the worker, and log in once there. The login is stored in the
`synaptane-agents` volume, so it survives upgrades (a login made in the worker up to 1.14
stays valid: nothing to redo).

```sh
docker compose exec agents claude auth login         # Claude subscription
docker compose exec agents codex login --device-auth # ChatGPT subscription
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

**Next step:** [configuration](configuration.md) (options, email, search index, alerts,
monitoring, audit log, roles), then the [first steps](first-steps.md) (organizations,
spaces, demo data).
