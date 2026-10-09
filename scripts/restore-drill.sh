#!/usr/bin/env bash
# Restore drill, to run every month on ANOTHER machine than the server (your workstation
# will do): proves that the latest off-site copy decrypts and restores.
#
#   ./scripts/restore-drill.sh --identity ~/synaptane-backup.key --from b2:my-bucket/synaptane
#   ./scripts/restore-drill.sh --identity ~/synaptane-backup.key --file synaptane-….tar.gz.age
#   ./scripts/restore-drill.sh --file synaptane-2026-10-06.tar.gz        # unencrypted archive
#
# Steps: fetches the latest archive (rclone) or takes the given file, decrypts it with the
# age private key, brings up a throwaway Compose project (synaptane-drill, no published port,
# no worker: nothing is pushed to your repositories, no tidying runs), restores, runs
# `doctor`, compares accounts, organizations and spaces with the manifest, then tears it all down.
#
# Needs Docker (with Compose), age for an encrypted archive, rclone for --from (configured
# with read access to the destination). Image: $SYNAPTANE_IMAGE, otherwise synaptane:latest,
# built from this repository if missing. It must be at least as recent as the version that
# made the backup.
set -euo pipefail
cd "$(dirname "$0")/.."

usage() {
  sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

identity="" from="" file=""
while [ $# -gt 0 ]; do
  case "$1" in
    --identity) identity="${2:?}"; shift 2 ;;
    --from) from="${2:?}"; shift 2 ;;
    --file) file="${2:?}"; shift 2 ;;
    -h | --help) usage ;;
    *) echo "Unknown option: $1" >&2; usage ;;
  esac
done
if [ -z "$from" ] && [ -z "$file" ]; then usage; fi

image="${SYNAPTANE_IMAGE:-synaptane:latest}"
umask 077 # the decrypted archive contains the master key in clear
work="$(mktemp -d)"
project="synaptane-drill"

cat > "$work/env" << EOF
PUBLIC_URL=http://127.0.0.1:8000
POSTGRES_PASSWORD=drill$(date +%s)
SYNAPTANE_IMAGE=${image}
COMPOSE_PROFILES=
EOF
# No port published on the machine: the drill instance is reachable by nobody.
cat > "$work/drill.yaml" << 'EOF'
services:
  app:
    ports: !reset []
EOF

dc() { docker compose -p "$project" -f compose.yaml -f "$work/drill.yaml" --env-file "$work/env" "$@"; }
cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then dc logs --tail 60 app migrate 2> /dev/null || true; fi
  dc down -v --remove-orphans > /dev/null 2>&1 || true
  rm -rf "$work"
  if [ "$status" -eq 0 ]; then echo "Drill passed."; else echo "Drill FAILED ($status)." >&2; fi
  exit "$status"
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }
need() { command -v "$1" > /dev/null || { echo "\"$1\" not found: $2" >&2; exit 1; }; }
need docker "install Docker"

step "Archive"
if [ -n "$from" ]; then
  need rclone "install rclone and configure the destination (rclone config)"
  latest="$(rclone lsf --files-only --max-depth 1 --include '/synaptane-*.age' -- "$from" | sort | tail -n 1)"
  [ -n "$latest" ] || { echo "No synaptane-*.age archive in $from" >&2; exit 1; }
  case "$from" in *: | */) source_path="${from}${latest}" ;; *) source_path="${from}/${latest}" ;; esac
  echo "Latest copy: $source_path"
  rclone copyto -- "$source_path" "$work/$latest"
  file="$work/$latest"
fi
[ -f "$file" ] || { echo "File not found: $file" >&2; exit 1; }
archive="$work/archive.tar.gz"
case "$file" in
  *.age)
    need age "install age (https://age-encryption.org)"
    [ -n "$identity" ] || { echo "--identity (age private key) is required for an .age archive" >&2; exit 1; }
    age --decrypt --identity "$identity" --output "$archive" -- "$file"
    ;;
  *) cp -- "$file" "$archive" ;;
esac
gzip -t "$archive"

step "Image $image"
if ! docker image inspect "$image" > /dev/null 2>&1; then
  echo "Image missing: building from this repository."
  docker build -t "$image" .
fi
# The manifest is read with the image's Python: nothing else to install on the workstation.
read -r want_users want_orgs want_spaces < <(
  tar -xzOf "$archive" manifest.json | docker run --rm -i --entrypoint python "$image" -c '
import json, sys
manifest = json.load(sys.stdin)
counts = manifest.get("counts") or {}
print(manifest.get("created_at", "?"), manifest.get("version", "?"), file=sys.stderr)
print(*(counts.get(k, "-") for k in ("users", "organizations", "spaces")))
'
)

step "Throwaway instance ($project)"
dc up -d --no-build app
dc stop app

step "Restore"
dc run --rm --no-deps -T app synaptane restore - < "$archive"
dc start app
for _ in $(seq 1 60); do
  if dc exec -T app python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=4)" 2> /dev/null; then
    break
  fi
  sleep 2
done
dc exec -T app python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=4)"

step "doctor"
dc exec -T app synaptane doctor

step "Comparison with the manifest"
count() { dc exec -T postgres psql -U synaptane -d synaptane -tAc "SELECT count(*) FROM $1"; }
got_users="$(count users)" got_orgs="$(count organizations)" got_spaces="$(count spaces)"
printf 'accounts: %s (manifest: %s)\norganizations: %s (manifest: %s)\nspaces: %s (manifest: %s)\n' \
  "$got_users" "$want_users" "$got_orgs" "$want_orgs" "$got_spaces" "$want_spaces"
if [ "$want_users" = "-" ]; then
  echo "Archive from before the version that counts rows: comparison impossible, only displayed."
elif [ "$got_users/$got_orgs/$got_spaces" != "$want_users/$want_orgs/$want_spaces" ]; then
  echo "The restored counts differ from the manifest." >&2
  exit 1
fi
copies="$(dc exec -T app sh -c 'ls /data/repos | wc -l')"
echo "local copies of the repositories: $copies"
