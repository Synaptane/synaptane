English | [Français](README.fr.md)

# Server scripts

Scripts run **on the server** that runs Synaptane with Docker Compose, behind the host's
nginx (the default mode of `compose.yaml`). They are written to work on any server.

| Script | Role | Run by |
|---|---|---|
| `deploy.sh [<ref>]` | Upgrade: backup, build, checks, automatic rollback | you, at each upgrade |
| `backup.sh [<label>]` | Backup to `~/synaptane-backups`, 14 days kept | cron, every night; `deploy.sh` |
| `install-nginx.sh <domain>` | nginx site (HTTP → HTTPS, HSTS) and Let's Encrypt certificate | `sudo`, once (then at each change of `deploy/nginx/host-site.conf`) |

The repository is assumed to be cloned in `~/synaptane`, with its `.env` (see
[installation](../../docs/admin/installation.md)):

```sh
git clone https://github.com/synaptane/synaptane.git ~/synaptane
```

These scripts **build the image from source** (useful for the subscription CLIs). Give it a
local name in `.env`; otherwise it would take the name of the published image, and
`deploy.sh` refuses to go on:

```sh
SYNAPTANE_IMAGE=synaptane:latest
```

To follow the published image instead, you do not need these scripts: `docker compose pull &&
docker compose up -d` (see [installation](../../docs/admin/installation.md)).

## Private fork: deploy key (optional)

If you deploy from your own **private** fork, the server reads the GitHub repository with its
own SSH key, **read-only** and limited to that repository: it cannot push anything, and
revoking it affects nothing else.

1. On the server, create the key (without a passphrase: `deploy.sh` must be able to use it on
   its own):

   ```sh
   ssh-keygen -t ed25519 -N "" -C "synaptane deploy $(hostname)" -f ~/.ssh/synaptane_deploy
   cat ~/.ssh/synaptane_deploy.pub
   ```

2. On GitHub, in the repository: **Settings → Deploy keys → Add deploy key**. Title: the
   server's name; key: the line shown; **do not check** "Allow write access".

3. On the server, a host alias that uses only this key, in `~/.ssh/config`:

   ```
   Host github-synaptane
       HostName github.com
       User git
       IdentityFile ~/.ssh/synaptane_deploy
       IdentitiesOnly yes
   ```

   Then `chmod 600 ~/.ssh/config`, and accept GitHub's host key once:
   `ssh -T git@github-synaptane` (expected answer: "successfully authenticated… does not
   provide shell access").

4. Point the repository to this alias:

   ```sh
   # New server:
   git clone git@github-synaptane:<account>/synaptane.git ~/synaptane
   # Existing copy (brought over with git bundle, for example):
   cd ~/synaptane
   git remote set-url origin git@github-synaptane:<account>/synaptane.git   # or: git remote add origin …
   git fetch origin
   ```

With several servers: one key per server (GitHub refuses the same key on two repositories,
and a per-server key can be revoked on its own).

## Upgrade: `deploy.sh`

```sh
cd ~/synaptane
deploy/host/deploy.sh                 # latest published version (v* tag)
deploy/host/deploy.sh v1.0.1          # a tag, a branch or a commit
deploy/host/deploy.sh origin/main     # latest commit on main (development)
DRY_RUN=1 deploy/host/deploy.sh       # dry run: what would be done, without changing anything
```

Read the [changelog](../../CHANGELOG.md) before a new version. The script:

1. fetches the code (`git fetch`) and checks out the target commit (`git checkout --detach`);
2. takes a backup (`backup.sh avant-<commit>`), and keeps the image in service under the name
   `synaptane:precedent`;
3. builds the image with the commit recorded in it (`synaptane --version` shows it), also
   tagged `synaptane:<commit>`, then restarts (`docker compose up -d --no-build`);
4. checks for up to 3 minutes: `/readyz` returns 200, `synaptane --version` shows the right
   commit, the worker is `healthy`, `synaptane doctor` passes;
5. **if something fails, rolls back on its own**: previous commit and image, and restore of
   the backup **only if** a migration changed the database;
6. writes a `date commit result` line to `~/.local/state/synaptane/deploy.log`.

Exit code 0: deployed; 1: failed (the log says whether the rollback succeeded); 2: nothing
was done (usage, local changes, CI not green…).

Options and settings (details in the script header):
- `--check-ci`: refuses a commit whose GitHub CI is not green. For a public repository,
  nothing to provide. For a private fork, put a token in `GITHUB_TOKEN`: a fine-grained token,
  limited to that repository, with the **Actions: read** permission. For example in
  `~/.config/synaptane/github-token` (mode 600), then
  `GITHUB_TOKEN=$(cat ~/.config/synaptane/github-token) deploy/host/deploy.sh --check-ci`;
- `--no-backup`: no backup, only when the application is not running yet;
- `DEPLOY_TIMEOUT` (180 s), `STATE_DIR`, `BACKUP_DIR`, `SYNAPTANE_DIR`, `GITHUB_REPO`.

Downloaded images (PostgreSQL, certbot) do not change with `deploy.sh`. To update them, run
`docker compose pull --ignore-buildable && docker compose up -d`, after a backup. The last
three `synaptane:<commit>` images are kept, the others deleted.

To go back to an earlier version, deploy its commit: `deploy/host/deploy.sh <commit>` (the
commit of each deployment is in the log). If the database was migrated in between, restore the
backup taken before (manual procedure in
[installation](../../docs/admin/installation.md#roll-back)).

## Nightly backup: `backup.sh`

`crontab -e`, as the account that runs Docker:

```cron
0 3 * * * $HOME/synaptane/deploy/host/backup.sh > /dev/null 2>> $HOME/synaptane-backups/cron.err
```

- archive `~/synaptane-backups/synaptane-YYYY-MM-DD.tar.gz`, mode 600 (it contains the master
  key), directory in 700;
- written to a `.part` file, read in full, then renamed: never a truncated archive that looks
  like a real one;
- archives older than `RETENTION_DAYS` days (14) are deleted; one line per backup in
  `~/synaptane-backups/journal.log`;
- `OFFSITE=1` adds the offsite copy (`synaptane backup --offsite`, see
  [Backup](../../docs/admin/backup.md)): `0 3 * * * OFFSITE=1 $HOME/synaptane/…`.

These backups are on the same disk as the server: they protect against a mistake, not against
losing the server.

## nginx site: `install-nginx.sh`

```sh
sudo LETSENCRYPT_EMAIL=you@example.com deploy/host/install-nginx.sh brain.example.com
```

It installs `deploy/nginx/host-site.conf` with your domain: HTTP redirected to HTTPS (except
ACME challenges), HSTS, `server_tokens off`, access log without tokens, MCP streaming. The
certificate is specific to the domain (certbot in *webroot* mode, renewed by certbot's timer),
without touching the server's other sites. It can be run again: that is also how you apply a
new version of the site. Set `APP_PORT` if Synaptane does not listen on 8000.
