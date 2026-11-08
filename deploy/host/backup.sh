#!/usr/bin/env bash
# Synaptane backup on the server: database, master key, copies of the repositories.
#
#   deploy/host/backup.sh                 # daily backup (cron, see README.md)
#   deploy/host/backup.sh avant-abc1234   # label added to the name (deploy.sh)
#
# Optional: the worker already makes a backup every night in the synaptane-backups
# volume. Once its archive is checked and in place, this script marks it in the data volume
# (verified, complete). Without a label (the cron job), it also says it is running, then that
# it made the backup of the day: the worker's automatic backup steps aside while it runs, and
# takes over by itself as soon as its last good backup is more than 26 hours old
# (docs/admin/backup.md).
#
# Settings through environment variables:
#   BACKUP_DIR      archive folder (default ~/synaptane-backups)
#   RETENTION_DAYS  older archives are deleted (default 14 days)
#   SYNAPTANE_DIR   copy of the repository where Docker Compose runs (default: this script's)
#   OFFSITE=1       encrypted off-site copy in the same backup
#                   (`synaptane backup --offsite`, see docs/admin/backup.md)
#
# The archive contains the master key: folder in 700, files in 600. It is written to a
# `.part`, checked, then renamed: a failed backup never leaves a truncated `.tar.gz` that
# would look like a real one. Prints the archive path on standard output (everything else
# goes to standard error).
set -euo pipefail
umask 077

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dir="${BACKUP_DIR:-$HOME/synaptane-backups}"
retention="${RETENTION_DAYS:-14}"
repo="${SYNAPTANE_DIR:-$(cd "$here/../.." && pwd)}"
label="${1:-}"

[[ "$retention" =~ ^[0-9]+$ ]] || { echo "RETENTION_DAYS: a number of days." >&2; exit 2; }
[[ -z "$label" || "$label" =~ ^[A-Za-z0-9._-]+$ ]] || {
  echo "Invalid label: letters, digits, \".\", \"_\" and \"-\"." >&2
  exit 2
}

mkdir -p "$dir"
chmod 700 "$dir"
out="$dir/synaptane-$(date +%F)${label:+-$label}.tar.gz"
log() { echo "$(date -Is) $*" >> "$dir/journal.log"; }

cd "$repo"
# Marks of the data volume, set in the app container: a plain `touch` or `rm`, harmless with
# an older image (a checkout not deployed yet). A failure here only means one backup more.
in_data() {
  # shellcheck disable=SC2016  # expanded in the container
  docker compose exec -T app sh -c 'cd "${SYNAPTANE_DATA_DIR:-/data}" && "$@"' sh "$@" \
    > /dev/null 2>&1 || true
}
running=0
finish() {
  rm -f "$out.part"
  if [ "$running" = 1 ]; then in_data rm -f .backup-host-running; fi
}
trap finish EXIT
if [ -z "$label" ]; then
  in_data touch .backup-host-running
  running=1
fi
command=(synaptane backup --output -)
[ "${OFFSITE:-0}" = 1 ] && command=(synaptane backup --offsite --output -)
# Code 3: archive checked and written, but an off-site destination failed.
status=0
# SYNAPTANE_BACKUP_HOST_MARKS: the marks are this script's, once the archive is in place (a
# variable, not an option: an older image ignores it).
docker compose exec -T -e SYNAPTANE_BACKUP_HOST_MARKS=1 app "${command[@]}" > "$out.part" \
  || status=$?
if [ "$status" -ne 0 ] && [ "$status" -ne 3 ]; then
  log "FAILED backup ${out##*/}"
  echo "Backup impossible (is the application running? docker compose ps)." >&2
  exit 1
fi
# Full read of the archive (gzip intact to the end), then presence of the database.
# No `tar | grep -q`: grep would stop at the first match and tar would die of SIGPIPE.
if ! listing="$(tar -tzf "$out.part")" || ! grep -qx "database.dump" <<<"$listing"; then
  log "FAILED backup ${out##*/}: unreadable or incomplete archive"
  echo "Unreadable or incomplete archive: nothing was replaced." >&2
  exit 1
fi
chmod 600 "$out.part"
mv "$out.part" "$out"
# The backup is in place: the health page sees it, the worker counts it as the backup of the
# day (a deployment's) and, for the cron job, steps aside.
marks=(.backup-done .backup-complete)
[ -z "$label" ] && marks+=(.backup-host)
in_data touch "${marks[@]}"
find "$dir" -maxdepth 1 -name 'synaptane-*.tar.gz' -mtime +"$retention" -delete
log "$out $(du -h "$out" | cut -f1)"

if [ "${OFFSITE:-0}" = 1 ]; then
  # A failed destination does not delete the local backup, but it shows: exit code 3 and a
  # line in the log (Synaptane's message names the destination).
  if [ "$status" -eq 3 ]; then
    log "FAILED off-site copy (at least one destination)"
    echo "$out"
    exit 3
  fi
  log "off-site copy done"
fi
echo "$out"
