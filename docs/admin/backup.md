English | [Français](backup.fr.md)

# Backup and restore

Three levels, to set up in this order:

1. **A verified local archive every night** (`synaptane backup`): it protects against a
   handling mistake or a failed upgrade.
2. **An encrypted copy off the server** (`synaptane backup --offsite`), to one or more
   destinations of your choice: S3 and compatibles, SFTP, WebDAV/Nextcloud, SMB NAS, Google
   Drive, OneDrive, Dropbox… It protects against a disk failure, a theft or a lost server.
3. **A restore drill every month** (`scripts/restore-drill.sh`), on your own computer. A
   backup that has never been restored is only a hope.

## What is backed up

`synaptane backup` produces a `.tar.gz` archive that contains:

| Item | Why |
|---|---|
| The PostgreSQL database (`pg_dump`) | Accounts, organizations, spaces, permissions, tokens, settings, encrypted secrets, tidy-up history, semantic index. |
| The **master key** | Without it, the encrypted secrets in the database are lost: Git credentials, AI keys, SSO and webhook secrets. |
| The local copies of the repositories | The content is already on the remote repositories. The copies keep what could not be pushed yet, for example after a conflict or a Git host outage. |
| A manifest | Synaptane version, database revision, date, number of accounts, organizations and spaces. |

**Not** backed up:
- text extracted from PDFs and images, which is rebuilt from Git;
- the Claude Code and Codex CLI sign-ins, to redo after a restore;
- the rclone configuration of the destinations (`/data/rclone/rclone.conf`): keep a copy of
  its credentials in your password manager.

The archive contains the master key in clear: **it gives access to every secret**. That is
why the offsite copy is always encrypted, and refused without an encryption key. With
`--without-key`, the key stays out of the archive; you must then back it up separately, once
and for all, since it does not change.

## Local backup

Each archive is **read back before it is reported**: compression intact (like `gzip -t`),
complete tar, manifest and key present, and a dump fully readable by `pg_restore`. A
truncated archive (full disk, interruption) makes the command fail the same day.

```sh
umask 077
docker compose exec -T app synaptane backup --output - > synaptane-$(date +%F).tar.gz
```

With `--output -`, nothing is written to standard output if the archive is bad.

Options:
- `--output`: a file, a directory, or `-` for standard output. Without `--output`, the
  archive goes to `$SYNAPTANE_BACKUP_DIR` (`/backups` with Docker), otherwise to the current
  directory;
- `--without-repos`: without the repository copies;
- `--without-key`: without the master key;
- `--offsite`: encrypted copy off the server, see below.

The `synaptane-backups` volume (`/backups`) has no rotation, sits on the same disk as the
data, and **`docker compose down -v` erases it** along with every other volume: it is not a
place to keep backups. Prefer standard output to a directory on the server. To get an archive
out of it:

```sh
docker compose exec app ls -l /backups
docker compose cp app:/backups/synaptane-2026-10-03-030000.tar.gz .
```

## Offsite copy

### How it works

```text
server: synaptane backup --offsite
  verified archive ──age──▶ synaptane-YYYY-MM-DD-HHMMSS.tar.gz.age ──rclone──▶ destination 1
                    (public key)                                         ├──▶ destination 2
                                                                         └──▶ …
your computer: age private key ──▶ decrypt ──▶ restore
```

- **Encryption with [age](https://age-encryption.org)** to one or more public keys. The
  server encrypts but **cannot decrypt**: stealing the server or the destination yields
  nothing. The private key stays with you.
- **Copy with [rclone](https://rclone.org/overview/)**, which supports more than 70 storage
  types. The destinations' credentials stay in the rclone configuration, never in the
  database or on the command line.
- **Several destinations**: each one receives the copy. A failing destination does not stop
  the others; the message says which one failed, and the exit code reports it.
- **Retention**: after a successful copy, `synaptane-*.age` archives older than
  `BACKUP_RETENTION_DAYS` (30 days by default) are deleted from **that** destination, and
  only at its root. Nothing else is touched, even in a shared folder. A destination that did
  not receive today's copy keeps all its older copies.

`age` and `rclone` are in the Docker image. From source, install the `age` and `rclone`
packages.

### 1. Create the age key pair, on your computer

Not on the server: the private key must never be there.

```sh
age-keygen -o synaptane-backup.key
# Public key: age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq
```

Store `synaptane-backup.key` **with the master key**, once and for all, in two places: your
password manager, and a paper copy or a USB stick in a drawer. Without it, the offsite copies
are unreadable, including for you.

Several people can restore: each one creates their own pair, and all the public keys are
listed (any one of the private keys opens the archive). A public SSH key (`ssh-ed25519 …`)
works too.

### 2. Declare the public key

In `.env`, on the server:

```sh
BACKUP_AGE_RECIPIENTS=age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq
```

Several keys: separated by commas. Without a key, `backup --offsite` **refuses** to copy,
with a clear message.

### 3. Declare the destinations

The rclone wizard lists every storage type and asks the questions one by one:

```sh
docker compose exec -it app rclone config
```

The configuration is written to `/data/rclone/rclone.conf` (`synaptane-data` volume, mode
600). Then, in `.env`, the list of destinations, as `name:path`, separated by commas:

```sh
BACKUP_REMOTES=b2:synaptane-backups,nas:backups/synaptane
BACKUP_RETENTION_DAYS=30
```

Then `docker compose up -d` so that the application reads these settings.

For each destination, create access **dedicated to this purpose**: a dedicated bucket or
folder, an account or API key limited to that bucket. That way, a leak of the rclone
configuration exposes nothing else.

Examples of what the wizard writes to `rclone.conf` (values to replace):

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

### 4. Test

```sh
docker compose exec app synaptane offsite test
# [ok] b2:synaptane-backups
# [FAILED] nas:backups/synaptane — write: … connection refused
```

For each destination, a small probe file is written, read back, then deleted: write, read and
delete rights (retention needs the last one) are checked in one go. A failing destination is
marked `[FAILED]`, with the step that failed (`write`, `read back` or `delete`) and the end of
rclone's message. The command also reports a missing age key or a missing `age` program.
`synaptane doctor` summarizes the state of the offsite copy, always as a warning, never as a
failure.

First copy by hand, then check:

```sh
docker compose exec app synaptane backup --offsite
docker compose exec app synaptane offsite list
```

Without `--output`, `--offsite` keeps **no** local copy: the clear archive is deleted from
the server once encrypted.

### 5. Every day, with cron

The versioned script `deploy/host/backup.sh` keeps verified local archives (14 days by
default) and, with `OFFSITE=1`, sends the encrypted offsite copy in the same backup run. It
first writes to a `.part` file, renamed only if the archive is good: an incomplete archive
never replaces a good one. A failing offsite destination keeps the local archive, is noted in
the log (`~/synaptane-backups/journal.log`) and returns code 3.

```sh
# crontab -e
0 3 * * * OFFSITE=1 $HOME/synaptane/deploy/host/backup.sh > /dev/null 2>> $HOME/synaptane-backups/cron.err
```

Settings: `BACKUP_DIR`, `RETENTION_DAYS` (see the script header, and the
[server scripts](../../deploy/host/README.md)). Cron emails what the script writes to
standard error, if mail delivery is configured on the server.

Exit codes of `synaptane backup`:

| Code | Meaning |
|---|---|
| 0 | Archive verified, and copied to every destination (with `--offsite`). |
| 1 | No usable backup, or offsite copy refused (missing setting): nothing was written. |
| 3 | Archive verified (and written if `--output`), but at least one destination failed (copy or retention). The message says which one. |

To be warned when the backup stops running altogether (cron stopped, server down), add at the
end of the script a call to a "dead man's switch" monitor (Healthchecks.io, Uptime Kuma…)
when the code is 0.

## Restore

### From a clear archive

For example on a new server:

```sh
docker compose up -d                  # new instance: database without accounts and a new key
docker compose stop app worker
docker compose run --rm --no-deps -T app synaptane restore - < synaptane-2026-10-03.tar.gz
docker compose start app worker
```

### From an offsite copy

Decrypt **on your computer**, and pipe the decrypted archive straight into the restore: the
private key never touches the server.

```sh
# On your computer, with rclone configured to read the destination:
rclone lsf b2:synaptane-backups                         # pick the archive
rclone copyto b2:synaptane-backups/synaptane-2026-10-06-030000.tar.gz.age .
# On the server: docker compose up -d && docker compose stop app worker
age -d -i synaptane-backup.key synaptane-2026-10-06-030000.tar.gz.age \
  | ssh server 'cd ~/synaptane && docker compose run --rm --no-deps -T app synaptane restore -'
# On the server: docker compose start app worker
```

Without rclone on your computer, download the file from the storage's interface (S3 console,
Nextcloud, Google Drive…).

### What the restore does

- it replaces the database, the master key and the repository copies. Since an archive may
  come from elsewhere, the Git configuration of each copy is reduced to the essentials
  (remote address, branch) and its hooks are removed: nothing can run on restart;
- it brings the database up to the installed version;
- it asks the worker to resync and reindex every space. Pages written before the outage and
  not yet pushed then go to the repositories.

It stops without touching anything in these cases:
- **the application or the worker is still connected** to the database;
- **the backup comes from a newer version** of Synaptane: install that version first;
- **the instance already has accounts**: add `--force` to replace them;
- **the archive is damaged** (interrupted copy, full disk): the dump is read in full before
  touching the database;
- **the archive has no master key** (`--without-key`) and the key in place is not the one of
  the backup: put the saved key in place first, for example
  `docker compose cp master.key app:/data/master.key`.

Resetting and restoring the database happen in a single transaction: on error, the database
stays as it was. To be safe, still run `synaptane backup` just before a `restore --force`.

`synaptane doctor` then checks that the stored secrets decrypt with the key in place.

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

```sh
uv run synaptane backup --output /var/backups/synaptane/
uv run synaptane backup --offsite        # with backup_age_recipients and backup_remotes
# Stop synaptane serve and synaptane worker, then:
uv run synaptane restore /var/backups/synaptane/synaptane-2026-10-03-030000.tar.gz
```

You need `pg_dump` and `pg_restore` of the same major version as the PostgreSQL server, or a
newer one. In `synaptane.toml`:

```toml
backup_age_recipients = ["age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq"]
backup_remotes = ["b2:synaptane-backups", "nas:backups/synaptane"]
backup_retention_days = 30
# backup_rclone_config = "/etc/synaptane/rclone.conf"   # default: $RCLONE_CONFIG or <data_dir>/rclone/rclone.conf
```
