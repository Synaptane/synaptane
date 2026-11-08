#!/usr/bin/env bash
# Synaptane backup on the server: database, master key, copies of the repositories.
#
#   deploy/host/backup.sh                 # daily backup (cron, see README.md)
#   deploy/host/backup.sh avant-abc1234   # label added to the name (deploy.sh)
#
# Optional: the worker already makes a backup every night in the synaptane-backups
# volume. Without a label (the cron job), once its archive is checked and in place, this
# script tells Synaptane it made the backup of the day: the worker's automatic backup then
# steps aside, and takes over by itself as soon as the last one is more than 26 hours old
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
trap 'rm -f "$out.part"' EXIT
log() { echo "$(date -Is) $*" >> "$dir/journal.log"; }

cd "$repo"
command=(synaptane backup --output -)
[ "${OFFSITE:-0}" = 1 ] && command=(synaptane backup --offsite --output -)
# Code 3: archive checked and written, but an off-site destination failed.
status=0
docker compose exec -T app "${command[@]}" > "$out.part" || status=$?
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
# The backup is in place: the worker's automatic backup counts it (a deployment's) or, for the
# cron job, steps aside. A plain `touch`, harmless with an older image (deploy.sh runs this
# script before upgrading); a failure here only means one backup more.
marks=(.backup-complete)
[ -z "$label" ] && marks+=(.backup-host)
# shellcheck disable=SC2016  # expanded in the container
docker compose exec -T app sh -c 'cd "${SYNAPTANE_DATA_DIR:-/data}" && touch "$@"' sh "${marks[@]}" \
  > /dev/null 2>&1 || true
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
