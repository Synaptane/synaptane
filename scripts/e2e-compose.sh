#!/usr/bin/env bash
# Full run of the Compose stack, as on a server, with real data: start, doctor, super-admin
# account, login, token, organization, space linked to a Git repository synced by the worker,
# page written through the API and pushed by the worker, encrypted AI key, backup, encrypted
# off-site copy (local folder, throwaway age key), `down -v` (everything erased), restore from
# that copy, new login, page read back, decryptable secret, new sync by the worker.
#
#   SYNAPTANE_IMAGE=synaptane:ci ./scripts/e2e-compose.sh
#
# The image is built beforehand (`docker build -t synaptane:ci .`, as in ci.yml): compose.yaml
# does not build, and a missing image would be pulled, then fail.
#
# "Upgrade" variant: the data is created by the previous version (that version's image and
# compose.yaml), then the stack moves to SYNAPTANE_IMAGE and to the repository's compose.yaml,
# as during an upgrade in production: migrations on real data, doctor, page read back, sync
# by the new worker.
#
#   SYNAPTANE_PREVIOUS_IMAGE=synaptane:previous SYNAPTANE_PREVIOUS_COMPOSE=…/compose.yaml \
#     SYNAPTANE_IMAGE=synaptane:ci ./scripts/e2e-compose.sh
#
# The "forge" is a bare repository in an external Docker volume (synaptane-e2e-forge), mounted
# in app and worker: it survives `down -v`, as a real remote repository survives the loss of
# the server. `file://` repositories are only accepted with SYNAPTANE_ALLOW_LOCAL_REPOS, set
# by an override Compose file specific to this run.
#
# Separate Compose project (synaptane-e2e) and port 18000: leaves any local instance alone.
set -euo pipefail
cd "$(dirname "$0")/.."

image="${SYNAPTANE_IMAGE:-synaptane:local}"
previous="${SYNAPTANE_PREVIOUS_IMAGE:-}"
previous_compose="${SYNAPTANE_PREVIOUS_COMPOSE:-$PWD/compose.yaml}"
# The shell environment would win over --env-file: the image comes from the env file only.
unset SYNAPTANE_IMAGE
port="${E2E_PORT:-18000}"
url="http://127.0.0.1:${port}"
api="${url}/api/v1"
work="$(mktemp -d)"
password="password-e2e-$(date +%s)"
postgres_password="e2e$(date +%s)"
forge=synaptane-e2e-forge
compose="$PWD/compose.yaml"

# Image used by the stack; changed by the "upgrade" variant.
write_env() {
  cat > "$work/env" <<EOF
PUBLIC_URL=${url}
POSTGRES_PASSWORD=${postgres_password}
APP_PORT=${port}
SYNAPTANE_IMAGE=$1
COMPOSE_PROFILES=
EOF
}
write_env "$image"

cat > "$work/forge.yaml" <<EOF
services:
  app:
    environment:
      SYNAPTANE_ALLOW_LOCAL_REPOS: "true"
    volumes:
      - forge:/forge
  worker:
    environment:
      SYNAPTANE_ALLOW_LOCAL_REPOS: "true"
    volumes:
      - forge:/forge
volumes:
  forge:
    external: true
    name: ${forge}
EOF

dc() { docker compose -p synaptane-e2e -f "$compose" -f "$work/forge.yaml" --env-file "$work/env" "$@"; }
cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then dc logs --tail 80 app worker migrate || true; fi
  dc down -v --remove-orphans >/dev/null 2>&1 || true
  docker volume rm -f "$forge" >/dev/null 2>&1 || true
  rm -rf "$work"
  exit "$status"
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }

# app and worker do run on the expected image.
running_image() {
  for service in app worker; do
    actual="$(docker inspect -f '{{.Config.Image}}' "$(dc ps -q "$service")")"
    [ "$actual" = "$1" ] || { echo "$service runs on $actual and not $1" >&2; return 1; }
  done
}

wait_ready() {
  for _ in $(seq 1 90); do
    if [ "$(curl -s -o /dev/null -w '%{http_code}' "$url/readyz")" = 200 ]; then return 0; fi
    sleep 2
  done
  echo "The application is not ready after 3 minutes." >&2
  return 1
}

login() {
  rm -f "$work/cookies"
  code="$(curl -s -o /dev/null -w '%{http_code}' -c "$work/cookies" \
    --data-urlencode "username=root" --data-urlencode "password=${password}" "$url/login")"
  [ "$code" = 303 ] || { echo "Login refused ($code)" >&2; return 1; }
  # Page read in full before searching: `curl | grep -q` would fail at random (SIGPIPE).
  page="$(curl -s -b "$work/cookies" "$url/account")"
  grep -q "@root" <<<"$page" || { echo "Unexpected account page" >&2; return 1; }
}

# API call with the personal token; the body of an error response is printed.
call() { curl -sS --fail-with-body -H "Authorization: Bearer ${token}" "$@"; }
send() { call -H "Content-Type: application/json" "$@"; }
# expect TEXT COMMAND…: the command's output contains TEXT. Output read in full before
# searching: a `| grep -q` would close the pipe early, and pipefail would make curl fail.
expect() {
  local out
  out="$("${@:2}")"
  grep -qF -- "$1" <<< "$out" || { printf '"%s" missing from: %s\n' "$1" "$out" >&2; return 1; }
}
field() { python3 -c 'import json, sys; print(json.load(sys.stdin).get(sys.argv[1]) or "")' "$1"; }

space_api="${api}/organizations/e2e/spaces/wiki"
page_text="Written through the API, pushed by the worker."

# Requests a sync and waits for the worker to do it: only the worker does it, so a new
# `synced_at` proves that it is running and that it reaches the forge.
sync_by_worker() {
  before="$(call "${space_api}/git" | field synced_at)"
  send -X POST "${space_api}/git/sync" > /dev/null
  for _ in $(seq 1 60); do
    call "${space_api}/git" > "$work/git.json"
    if [ "$(field status < "$work/git.json")" = ok ] \
      && [ "$(field synced_at < "$work/git.json")" != "$before" ]; then
      return 0
    fi
    sleep 2
  done
  echo "No sync by the worker within 2 minutes:" >&2
  cat "$work/git.json" >&2
  return 1
}

forge_show() {
  docker run --rm -v "${forge}:/forge:ro" --entrypoint git "$image" \
    -c safe.directory='*' --git-dir=/forge/wiki.git show "main:$1"
}

check_page() { expect "$page_text" call "${space_api}/raw/notes/e2e.md"; }

# Data created by the running version: account, token, organization, space linked to the
# forge, page, AI key.
populate() {
  step "Super-admin account and login"
  dc exec -T -e SYNAPTANE_ADMIN_PASSWORD="$password" app \
    synaptane admin create --username root --display-name Root --email root@example.com
  login

  step "Personal token created from the web"
  # `sed -n 1p` and not `head -n 1`: head would close the pipe before the end (pipefail).
  csrf="$(curl -s -b "$work/cookies" "$url/account/assistants" \
    | grep -o 'name="csrf" value="[^"]*"' | sed -n '1s/.*value="//; 1s/"$//p')"
  token="$(curl -s -L -b "$work/cookies" -c "$work/cookies" --data-urlencode "csrf=${csrf}" \
    --data-urlencode "name=e2e" "$url/account/tokens" | grep -o 'syn_[A-Za-z0-9_-]*' | sed -n 1p)"
  [ -n "$token" ] || { echo "No token displayed after its creation" >&2; return 1; }
  expect '"username":"root"' call "${api}/me"

  step "Organization and space linked to the forge, synced by the worker"
  send -X POST "${api}/organizations" -d '{"slug": "e2e", "name": "E2E"}' > /dev/null
  send -X POST "${api}/organizations/e2e/spaces" \
    -d '{"slug": "wiki", "name": "Wiki", "repo_url": "file:///forge/wiki.git"}' > /dev/null
  sync_by_worker
  # Home page coming from the forge (and not the skeleton Synaptane writes when there is no
  # content/ folder): the local copy is indeed the remote repository's.
  expect "Wiki e2e" call "${space_api}/raw/README.md"

  step "Page written through the API, pushed to the forge by the worker"
  send -X PUT "${space_api}/files/notes/e2e.md" \
    -d "{\"content\": \"# Page e2e\n\n${page_text}\n\", \"message\": \"Page e2e\"}" > /dev/null
  for _ in $(seq 1 60); do
    if expect "$page_text" forge_show content/notes/e2e.md 2> /dev/null; then break; fi
    sleep 2
  done
  expect "$page_text" forge_show content/notes/e2e.md

  step "Fake AI key, encrypted with the master key"
  send -X PUT "${api}/organizations/e2e/ai" \
    -d '{"provider": "anthropic", "api_key": "sk-ant-e2e-fake"}' > /dev/null
  expect '"provider":"anthropic"' call "${api}/organizations/e2e/ai"
}

step "Forge: bare repository in an external volume"
docker volume rm -f "$forge" > /dev/null 2>&1 || true
docker volume create "$forge" > /dev/null
# The new volume belongs to root: prepared as root, then handed over to the image's user.
docker run --rm --user 0 -v "${forge}:/forge" --entrypoint sh "$image" -c '
  set -e
  git init -q --bare -b main /forge/wiki.git
  git init -q -b main /tmp/wiki
  cd /tmp/wiki
  mkdir content
  echo "# Wiki e2e" > content/README.md
  git add content
  git -c user.name=e2e -c user.email=e2e@example.com commit -q -m "First commit"
  git push -q /forge/wiki.git main
  chown -R synaptane: /forge
'

if [ -n "$previous" ]; then
  step "Starting the previous version (${previous})"
  write_env "$previous"
  compose="$previous_compose"
  dc up -d --no-build
  wait_ready
  running_image "$previous"
  populate

  step "Upgrade to ${image}: migrations on the previous version's data"
  write_env "$image"
  compose="$PWD/compose.yaml"
  dc up -d --no-build --remove-orphans
  wait_ready
  running_image "$image"
  dc exec -T app synaptane doctor
  login
  check_page
  sync_by_worker
else
  step "Start"
  dc up -d --no-build
  wait_ready
  running_image "$image"
  step "doctor"
  dc exec -T app synaptane doctor
  populate
fi

step "Git token in the container (/tmp mounted noexec)"
dc exec -T app python - <<'PY'
import os, subprocess
from synaptane.git.credentials import GitCredentials, git_environment
credentials = GitCredentials.https("token-e2e", "bot")
with git_environment(credentials, "https://forge.example/acme/wiki.git") as env:
    def fill(host):
        return subprocess.run(
            ["git", "credential", "fill"], input=f"protocol=https\nhost={host}\n\n",
            capture_output=True, text=True,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, **env},
        )
    out = fill("forge.example")
    assert out.returncode == 0 and "password=token-e2e" in out.stdout, out
    other = fill("elsewhere.example")  # the token is only given to the repository host
    assert "token-e2e" not in other.stdout, other
print("token passed to git, for the repository host only")
PY

step "Backup"
(umask 077 && dc exec -T app synaptane backup --output - > "$work/backup.tar.gz")
tar -tzf "$work/backup.tar.gz" > "$work/listing"
grep -qx "master.key" "$work/listing"

step "Off-site copy: refused without an age key, then encrypted to a folder"
offsite=(-e SYNAPTANE_BACKUP_REMOTES=/data/offsite)
if dc exec -T "${offsite[@]}" app synaptane backup --offsite 2> "$work/refusal"; then
  echo "Off-site copy accepted without an age recipient?" >&2; exit 1
fi
grep -q "SYNAPTANE_BACKUP_AGE_RECIPIENTS" "$work/refusal"
# Throwaway key: the private key only stays in $work (erased at the end) and in the
# container's /tmp (in memory), long enough to decrypt.
key="$(dc exec -T app age-keygen 2> /dev/null)"
public="$(printf '%s\n' "$key" | dc exec -T app age-keygen -y)"
offsite+=(-e "SYNAPTANE_BACKUP_AGE_RECIPIENTS=${public}")
dc exec -T "${offsite[@]}" app synaptane offsite test
dc exec -T "${offsite[@]}" app synaptane backup --offsite
name="$(dc exec -T "${offsite[@]}" app synaptane offsite list | grep -o 'synaptane-[^ ]*\.age' | tail -n 1)"
printf '%s\n' "$key" | dc exec -T app sh -c 'umask 077 && cat > /tmp/e2e-age.key'
(umask 077 && dc exec -T app age -d -i /tmp/e2e-age.key "/data/offsite/${name}" \
  > "$work/from-copy.tar.gz")
tar -tzf "$work/from-copy.tar.gz" > "$work/listing"
grep -qx "master.key" "$work/listing"

step "Disaster: down -v erases everything (except the forge, which is elsewhere)"
dc down -v
dc up -d --no-build
wait_ready
if login 2>/dev/null; then echo "The account still exists after down -v?" >&2; exit 1; fi

step "Restore from the decrypted off-site copy"
dc stop app worker
dc run --rm --no-deps -T app synaptane restore - < "$work/from-copy.tar.gz"
dc start app worker
wait_ready
login
# doctor also checks that the AI key and the other secrets decrypt with the restored key.
dc exec -T app synaptane doctor

step "After the restore: token, page, AI key and sync by the worker"
check_page
expect '"provider":"anthropic"' call "${api}/organizations/e2e/ai"
sync_by_worker

step "Run complete"
