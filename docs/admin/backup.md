English | [Français](backup.fr.md)

# Backup and restore

With Docker:

1. **Nothing to do: a verified backup every night.** The worker makes an archive at 04:00,
   reads it back, and keeps those of the last 7 days in the `synaptane-backups` volume. It
   protects against a handling mistake or a failed upgrade. **Administration → Health** shows
   the last one.
2. **One command, once, for the encrypted copy off the server**, which protects against a disk
   failure, a theft or a lost server:

   ```sh
   docker compose exec -it app synaptane backup setup
   ```

   It makes the encryption key (or takes yours), declares the storage (S3 and compatibles,
   SFTP, WebDAV/Nextcloud, SMB NAS, Google Drive, OneDrive, Dropbox…), tests it, and makes the
   first copy. The following ones leave every night with the backup.
3. **Then a habit: a restore drill every month** (`scripts/restore-drill.sh`), on your own
   computer. A backup
   that has never been restored is only a hope.

At any time, `docker compose exec app synaptane backup check` says what works and, for what is
missing, the exact command to run.

## What is backed up

Each backup is a `.tar.gz` archive that contains:

| Item | Why |
|---|---|
| The PostgreSQL database (`pg_dump`) | Accounts, organizations, spaces, permissions, tokens, settings, encrypted secrets, tidy-up history, semantic index. |
| The **master key** | Without it, the encrypted secrets in the database are lost: Git credentials, AI keys, SSO and webhook secrets. |
| The local copies of the repositories | The content is already on the remote repositories. The copies keep what could not be pushed yet, for example after a conflict or a Git host outage. |
| A manifest | Synaptane version, database revision, date, number of accounts, organizations and spaces. |

**Not** backed up:
- text extracted from PDFs and images, which is rebuilt from Git;
- the Claude Code and Codex CLI sign-ins, to redo after a restore;
- the settings of the off-site copy kept in the data volume: the rclone configuration of the
  storages (`/data/rclone/rclone.conf`), and the public keys and destinations saved by
  `backup setup` (`/data/backup/`). Keep the storages' credentials in your password manager,
  and run `backup setup` again after a restore on a new server.

The archive contains the master key in clear: **it gives access to every secret**. That is
why the off-site copy is always encrypted, and refused without an encryption key. With
`--without-key`, the key stays out of the archive; you must then back it up separately, once
and for all, since it does not change.

## The nightly backup, on the server

Every day at `BACKUP_TIME` (04:00, server time `TZ`), the worker:

1. deletes its own archives older than `BACKUP_LOCAL_RETENTION_DAYS` (7 days), always keeping
   the 3 newest: a backup that keeps failing does not end with an empty folder. Only the
   worker's archives, `synaptane-YYYY-MM-DD-HHMMSS-auto.tar.gz`, are concerned: a file put
   in `/backups` by hand or by `synaptane backup` is never deleted;
2. checks the free space: the new archive (the last one plus 20 %) must leave 2 GB free on
   the disk, or a tenth of a disk smaller than 20 GB, for PostgreSQL, which shares it.
   Otherwise the backup fails with an alert, and nothing is written;
3. writes the archive to a hidden `.part` file of `/backups`, **reads it back** (compression
   intact like `gzip -t`, complete tar, manifest and key present, dump fully readable by
   `pg_restore`), and only then gives it its name (mode 600): a truncated archive (full disk,
   interruption) never looks like a good one;
4. sends the encrypted off-site copy, if it is set up (below).

It needs nothing: no cron job, no setting. A worker started after its time (new installation,
restart, upgrade) makes the backup of the day right away, unless a backup was made less than
6 hours before (the one `deploy.sh` makes before upgrading, for example). If another backup
holds the lock at that moment, the worker waits for it to end, without giving up the day. With the 7 days kept, expect up to
10 archives of the size of your database plus repositories in the `synaptane-backups` volume,
on the disk of the server.

**A failure alerts the super admins**, whatever its cause (full disk, dump stuck for more than
`BACKUP_TIMEOUT_MINUTES`, unreachable destination, worker killed several times): by email (if
sending is configured) and in the system alerts of **Administration → Health**; the same alert
is not sent again within 20 hours. The previous archives are kept.

**Administration → Health** shows the last verified archive, how backups are made, the
worker's last run (waiting, in progress, done, skipped because another backup was running,
failed and why), and, when something is missing, the command to run. **Back up now** asks the
worker for a backup right away (written to the audit log).

Settings, in `.env` (then `docker compose up -d`):

| Variable | Default | Purpose |
|---|---|---|
| `BACKUP_TIME` | `04:00` | Time of the nightly backup, server time (`TZ`): `04:00`, `4:00` or `04h00`. `off`: no automatic backup (a cron job of yours does it, see below). A value that is not a time does not keep Synaptane from starting: 04:00 is used, and `doctor`, `backup check` and the health page say so. |
| `BACKUP_LOCAL_RETENTION_DAYS` | `7` | Days the worker's archives of `/backups` are kept (the 3 newest always); `0`: all. |
| `BACKUP_TIMEOUT_MINUTES` | `120` | Longest a database dump, or its read-back, may take: a stuck one is abandoned, with an alert. |

To turn the automatic backup off: `BACKUP_TIME=off` in `.env`, then `docker compose up -d`.

One backup at a time: the worker's, a cron job's, a deployment's or a manual one wait for each
other (a lock in the data volume), for twice `BACKUP_TIMEOUT_MINUTES` at most, saying so. The
off-site copy is sent once the lock is released: hours of copy hold up no other backup. A
restore refuses to start while a backup runs.

The `synaptane-backups` volume sits on the same disk as the data, and **`docker compose down
-v` erases it** along with every other volume: the off-site copy protects against that. To get
an archive out of it:

```sh
docker compose exec app ls -l /backups
docker compose cp app:/backups/synaptane-2026-10-03-040000-auto.tar.gz .
```

## Encrypted copy off the server

### How it works

```text
server: nightly backup (or synaptane backup --offsite)
  verified archive ──age──▶ synaptane-YYYY-MM-DD-HHMMSS.tar.gz.age ──rclone──▶ destination 1
                    (public key)                                         ├──▶ destination 2
                                                                         └──▶ …
your computer: age private key ──▶ decrypt ──▶ restore
```

- **Encryption with [age](https://age-encryption.org)** to one or more public keys. The
  server encrypts but **cannot decrypt**: stealing the server or the destination yields
  nothing. The private key stays with you.
- **Copy with [rclone](https://rclone.org/overview/)**, which supports more than 70 storage
  types. The storages' credentials stay in the rclone configuration, never in the database or
  on the command line.
- **Several destinations**: each one receives the copy. A failing destination does not stop
  the others; the alert says which one failed.
- **Retention**: after a successful copy, `synaptane-*.age` archives older than
  `BACKUP_RETENTION_DAYS` (30 days by default) are deleted from **that** destination, and
  only at its root. Nothing else is touched, even in a shared folder. A destination that did
  not receive today's copy keeps all its older copies.

`age` and `rclone` are in the Docker image. From source, install the `age` and `rclone`
packages.

### Set it up: `backup setup`

```sh
docker compose exec -it app synaptane backup setup
```

The command says what is already done and asks only for what is missing. Run again, it changes
nothing that works.

**1. Backup on this server**: it reminds you of the time and of the folder; nothing to answer.

**2. Encryption key.** Two choices:
- **press Enter**: a key pair is made in the container. The private key is **shown once and
  saved nowhere**; the command waits until you type `saved`, then keeps only the public key
  (in `/data/backup/age-recipients.txt`);
- **paste the public key** (`age1…`) of a pair made on your computer, for those who do not
  want the private key to ever go through the server:

  ```sh
  age-keygen -o synaptane-backup.key
  # Public key: age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq
  ```

Store the private key **with the master key**, once and for all, in two places: your password
manager, and a paper copy or a USB stick in a drawer. **Never on the server**: whoever holds
the server and this key reads the copies. Without it, the copies are unreadable, including for
you.

Several people can restore: each one adds their own public key (`synaptane offsite key age1…`),
and any one of the private keys opens the archive. A public SSH key (`ssh-ed25519 …`) works
too.

**3. Destination.** The storage and its access are declared once in rclone: the command offers
to start rclone's wizard (`rclone config`), which lists every storage type and asks the
questions one by one (examples below). The configuration is written to
`/data/rclone/rclone.conf` (`synaptane-data` volume, mode 600). Then give the destination, as
`name:folder` (for example `b2:synaptane-backups`): a small probe file is written, read back
and deleted (write, read and delete rights, retention needs the last one), and the destination
is saved only if all of it works (in `/data/backup/remotes.txt`).

For each destination, create access **dedicated to this purpose**: a dedicated bucket or
folder, an account or API key limited to that bucket. That way, a leak of the rclone
configuration exposes nothing else.

**4. First backup**: made right away if you agree, with its off-site copy. The next ones leave
every night.

Nothing to restart, nothing to write in `.env`: the worker reads the saved keys and
destinations at each backup.

### Without questions

For a script or configuration management, the same steps one by one:

```sh
docker compose exec app synaptane offsite key            # new pair: private key printed once
docker compose exec app synaptane offsite key age1…      # or your public key
docker compose exec -it app rclone config                # the storage
docker compose exec app synaptane offsite add b2:synaptane-backups   # tested, then saved
docker compose exec app synaptane offsite remove b2:synaptane-backups
```

Or in `.env`, then `docker compose up -d`; these variables add to what `setup` saved:

```sh
BACKUP_AGE_RECIPIENTS=age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq
BACKUP_REMOTES=b2:synaptane-backups,nas:backups/synaptane
BACKUP_RETENTION_DAYS=30
```

Several keys or destinations: separated by commas.

### Storage examples

What rclone's wizard writes to `rclone.conf` (values to replace):

**S3 and compatibles** (AWS, Scaleway, OVHcloud, Wasabi, MinIO…):

```ini
[s3]
type = s3
provider = Scaleway
access_key_id = SCWXXXXXXXXXXXXXXXXX
secret_access_key = xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
region = fr-par
endpoint = s3.fr-par.scw.cloud
```

Destination: `s3:my-bucket/synaptane`. For OVHcloud, `provider = Other` and the `endpoint` of
your region (`s3.gra.io.cloud.ovh.net`…); for AWS, `provider = AWS` and `region`.

**Backblaze B2** (application key limited to one bucket):

```ini
[b2]
type = b2
account = 0012345678900000000000001
key = K001xxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Destination: `b2:synaptane-backups`.

**SFTP** (another server, an internet box, a NAS): put the private SSH key and the host key
in the volume, then point rclone to them.

```sh
docker compose cp id_backup app:/data/rclone/id_backup
ssh-keyscan -t ed25519 nas.example.com > known_hosts
docker compose cp known_hosts app:/data/rclone/known_hosts
```

```ini
[nas]
type = sftp
host = nas.example.com
user = backup
key_file = /data/rclone/id_backup
known_hosts_file = /data/rclone/known_hosts
```

Destination: `nas:backups/synaptane`. `known_hosts_file` protects against server
impersonation.

**WebDAV / Nextcloud** (create an app password in Nextcloud; the wizard encrypts it with
`rclone obscure`):

```ini
[nextcloud]
type = webdav
url = https://cloud.example.com/remote.php/dav/files/alice/
vendor = nextcloud
user = alice
pass = <password obscured by rclone>
```

Destination: `nextcloud:Backups/Synaptane`.

**SMB NAS** (Windows share, Synology, QNAP…):

```ini
[smb]
type = smb
host = 192.168.1.20
user = backup
pass = <password obscured by rclone>
```

Destination: `smb:share/synaptane` (the first element is the share name).

**Google Drive, OneDrive, Dropbox, pCloud**: signing in goes through a browser, which the
server does not have. In the wizard, answer **no** to "Use auto config?": it shows an
`rclone authorize "drive" "…"` command. Run it on your computer (with rclone installed),
approve in the browser, then paste the answer into the wizard.

```ini
[gdrive]
type = drive
scope = drive.file
token = {"access_token":"…","refresh_token":"…"}
```

Destination: `gdrive:Synaptane Backups`. `scope = drive.file` limits rclone to the files it
created.

**A folder mounted on the server** (USB disk, NFS mount): just give its path as seen from the
container, after mounting it in `compose.yaml`. Beware: a disk on the same server does not
protect against theft or fire.

**Against ransomware** that would also erase the copies: turn on versioning and, if the
service offers it, *object lock* on the bucket. Then let the storage handle retention
(lifecycle rule) and set `BACKUP_RETENTION_DAYS=0`, or give the server a key without delete
rights.

### Check

```sh
docker compose exec app synaptane backup check
# [ok] Automatic backup: every day at 04:00, in /backups
# [ok] Free space: 41.2 GB in /backups
# [ok] Local retention: 7 days for the automatic backups (the 3 newest always kept)
# [ok] PostgreSQL client
# [ok] Last verified backup: 2026-11-08 04:00
# [ok] Encryption: 1 public key(s)
# [ok] Destination b2:synaptane-backups: writable
# [FAILED] Destination nas:backups/synaptane — write: … connection refused
# [ok] Off-site retention: 30 days
# [ok] Last off-site copy: 2026-11-08 04:02
```

Each line that is not `[ok]` says what to run. Each destination receives a probe file
(written, read back, deleted); `--no-test` skips it. Exit code 1 if something fails.

Other commands:
- `synaptane offsite test`: the probe file alone, on each destination;
- `synaptane offsite list`: the encrypted archives present on each destination;
- `synaptane doctor`: summarizes the automatic backup and the off-site copy, always as a
  warning, never as a failure.

## Your own cron job (optional)

Before the automatic backup, the backups were made by a cron job on the server, with the
versioned script `deploy/host/backup.sh`. **An installation that has one changes nothing**:
the script keeps its archives (14 days by default in `~/synaptane-backups`), sends the off-site
copy with `OFFSITE=1`, says while it runs that it is running and, once its archive is
checked and in place, tells Synaptane it made the backup of the day. The worker's automatic
backup then steps aside (**Administration → Health** says so), and deletes its own archives
older than the retention (the cron job keeps its own). It looks again every minute and **takes
over by itself** when both are true:
- the cron job's last good backup (archive checked and moved on the server, with its off-site
  copy if destinations are set up) ended more than 26 hours ago;
- no run of the cron job is in progress (a run still marked after 12 hours is taken for one
  that was killed).

A night the cron job fails, the worker therefore makes the backup 26 hours after the end of the
previous good one, the same day; a night it is only slow (a long off-site copy), the worker
waits for it. If the archive left the container but the server side failed (disk full there),
nothing is marked: the health page keeps showing the previous archive, and the worker takes
over in the same way.

- With `OFFSITE=1`, the cron job does everything; without it, while off-site destinations are
  set up, the worker still makes its own backup every night to send the copy. Add `OFFSITE=1`,
  or remove the cron job.
- `BACKUP_TIME=off` in `.env` leaves backups to the cron job alone.

To set one up anyway (a folder outside the Docker volumes, for example):

```sh
mkdir -m 700 -p ~/synaptane-backups   # before the first run: cron writes its errors there
# crontab -e
0 3 * * * OFFSITE=1 $HOME/synaptane/deploy/host/backup.sh > /dev/null 2>> $HOME/synaptane-backups/cron.err
```

The script first writes to a `.part` file, renamed only if the archive is good. A failing
off-site destination keeps the local archive, is noted in the log
(`~/synaptane-backups/journal.log`) and returns code 3. Settings: `BACKUP_DIR`,
`RETENTION_DAYS` (see the script header, and the [server scripts](../../deploy/host/README.md)).
Cron emails what the script writes to standard error, if mail delivery is configured on the
server.

To be warned when no backup runs at all anymore (server down), add a "dead man's switch"
monitor (Healthchecks.io, Uptime Kuma…), or the Prometheus alert on
`synaptane_backup_last_timestamp_seconds` (see [Monitoring](installation.md#monitoring)).

## Backup by hand: `synaptane backup`

```sh
umask 077
docker compose exec -T app synaptane backup --output - > synaptane-$(date +%F).tar.gz
```

The archive is read back like the nightly one before it is reported. With `--output -`,
nothing is written to standard output if the archive is bad.

Options:
- `--output`: a file, a directory, or `-` for standard output. Without `--output`, the
  archive goes to `$SYNAPTANE_BACKUP_DIR` (`/backups` with Docker), otherwise to the current
  directory;
- `--without-repos`: without the repository copies;
- `--without-key`: without the master key;
- `--offsite`: encrypted copy off the server, to the destinations set up. Without `--output`,
  it keeps **no** local copy: the clear archive is deleted from the server once encrypted.

The archive is written to a hidden `.part` file, renamed once read back. If another backup is
running, the command waits for it to finish (twice `BACKUP_TIMEOUT_MINUTES` at most), and says
so. A full backup (with the repositories and the key) kept in `/backups`, or one of
`deploy/host/backup.sh` once in place on the server (a deployment's), counts as the backup of
the day if it is less than 6 hours old when the worker's time comes. An archive sent to
standard output by hand is marked as verified, not as the backup of the day: only the one who
receives it knows it was kept.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Archive verified, and copied to every destination (with `--offsite`). |
| 1 | No usable backup, or off-site copy refused (missing setting): nothing was written. |
| 3 | Archive verified (and written if `--output`), but at least one destination failed (copy or retention). The message says which one. |

## Restore

### From a clear archive

For example on a new server:

```sh
docker compose up -d                  # new instance: database without accounts and a new key
docker compose stop app worker
docker compose run --rm --no-deps -T app synaptane restore - < synaptane-2026-10-03.tar.gz
docker compose start app worker
```

### From an off-site copy

Decrypt **on your computer**, and pipe the decrypted archive straight into the restore: the
private key never touches the server.

```sh
# On your computer, with rclone configured to read the destination:
rclone lsf b2:synaptane-backups                         # pick the archive
rclone copyto b2:synaptane-backups/synaptane-2026-10-06-040000.tar.gz.age .
# On the server: docker compose up -d && docker compose stop app worker
age -d -i synaptane-backup.key synaptane-2026-10-06-040000.tar.gz.age \
  | ssh server 'cd ~/synaptane && docker compose run --rm --no-deps -T app synaptane restore -'
# On the server: docker compose start app worker
```

Without rclone on your computer, download the file from the storage's interface (S3 console,
Nextcloud, Google Drive…). If the key pair was made by `backup setup`, put the private key it
showed into a file on your computer (`synaptane-backup.key`), for `age -d -i`.

### What the restore does

- it replaces the database, the master key and the repository copies. Since an archive may
  come from elsewhere, the Git configuration of each copy is reduced to the essentials
  (remote address, branch) and its hooks are removed: nothing can run on restart;
- it brings the database up to the installed version;
- it asks the worker to resync and reindex every space. Pages written before the outage and
  not yet pushed then go to the repositories.

It stops without touching anything in these cases:
- **the application or the worker is still connected** to the database;
- **a backup is running**: wait for it to finish;
- **the backup comes from a newer version** of Synaptane: install that version first;
- **the instance already has accounts**: add `--force` to replace them;
- **the archive is damaged** (interrupted copy, full disk): the dump is read in full before
  touching the database;
- **the archive has no master key** (`--without-key`) and the key in place is not the one of
  the backup: put the saved key in place first, for example
  `docker compose cp master.key app:/data/master.key`.

Resetting and restoring the database happen in a single transaction: on error, the database
stays as it was. To be safe, still run `synaptane backup` just before a `restore --force`.

`synaptane doctor` then checks that the stored secrets decrypt with the key in place. On a new
server, run `backup setup` again: the off-site copy's keys and destinations are not in the
archive.

A backup from an **earlier** version restores normally: the database is emptied, restored,
then upgraded. Going back to the previous version after a failed upgrade therefore means:
put the old code back, then restore the backup taken before the upgrade. A new instance,
without accounts, simply takes the archive's key instead of the one generated at startup.

## Restore drill, every month

On your computer (not on the server), from a copy of this repository, with Docker:

```sh
./scripts/restore-drill.sh --identity ~/synaptane-backup.key --from b2:synaptane-backups
# or with a file already downloaded:
./scripts/restore-drill.sh --identity ~/synaptane-backup.key --file synaptane-….tar.gz.age
```

The script:
1. fetches the latest archive from the destination (rclone) and decrypts it;
2. starts a throwaway Compose project, `synaptane-drill`, **with no published port and no
   worker**: nothing is pushed to your repositories, no tidy-up runs, no email goes out;
3. restores the archive, waits for the application to respond, runs `synaptane doctor`;
4. compares the number of accounts, organizations and spaces with the manifest;
5. tears everything down (`down -v`) and deletes the decrypted archive, even on failure.

It ends with `Drill passed.`, or with `Drill FAILED (n).` (`n`: the exit code) after showing
the end of the application's logs. The image used is `$SYNAPTANE_IMAGE`, otherwise
`synaptane:local`:
- `synaptane:local` missing: it is built from this repository, without the subscription CLIs;
- a published image (`SYNAPTANE_IMAGE=ghcr.io/synaptane/synaptane:1.0.0-rc.1`) missing: it
  is downloaded.

It must be at least as recent as the version that made the backup. Docker Compose 2.24 or
later is required.

Write down the date of each successful drill. Use it to check that the age private key and
the master key are still where you stored them.

## From source

Without `backup_dir`, the worker makes no automatic backup: set it to have one.

```sh
uv run synaptane backup --output /var/backups/synaptane/
uv run synaptane backup setup            # key, destination, first copy
uv run synaptane backup --offsite        # copy to the destinations set up
# Stop synaptane serve and synaptane worker, then:
uv run synaptane restore /var/backups/synaptane/synaptane-2026-10-03-040000.tar.gz
```

You need `pg_dump` and `pg_restore` of the same major version as the PostgreSQL server, or a
newer one. In `synaptane.toml`:

```toml
backup_dir = "/var/backups/synaptane"   # automatic backup every night (none without it)
backup_time = "04:00"                   # server time; "off": none
backup_local_retention_days = 7         # 0: all kept
backup_age_recipients = ["age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq"]
backup_remotes = ["b2:synaptane-backups", "nas:backups/synaptane"]
backup_retention_days = 30
# backup_rclone_config = "/etc/synaptane/rclone.conf"   # default: $RCLONE_CONFIG or <data_dir>/rclone/rclone.conf
```
