#!/usr/bin/env bash
# Full run of the Compose stack, as on a server, with real data: start, doctor, super-admin
# account, login, token, organization, space linked to a Git repository synced by the worker,
# page written through the API and pushed by the worker, encrypted AI key, backup, automatic
# backup by the worker, encrypted off-site copy (local folder, throwaway age key), `down -v`
# (everything erased), restore from that copy, new login, page read back, decryptable secret,
# new sync by the worker.
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
# Separate Compose project (synaptane-e2e, or E2E_PROJECT) and port 18000 (E2E_PORT): leaves
# any local instance alone.
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
# E2E_PROJECT: another Compose project name, to run two at once (separate forge too).
project="${E2E_PROJECT:-synaptane-e2e}"
forge="${project}-forge"
compose="$PWD/compose.yaml"

# Image used by the stack; changed by the "upgrade" variant.
write_env() {
  cat > "$work/env" <<EOF
PUBLIC_URL=${url}
POSTGRES_PASSWORD=${postgres_password}
APP_PORT=${port}
SYNAPTANE_IMAGE=$1
COMPOSE_PROFILES=
BACKUP_TIME=00:00
EOF
}
write_env "$image"

cat > "$work/forge.yaml" <<EOF
services:
  app:
    environment:
      SYNAPTANE_ALLOW_LOCAL_REPOS: "true"
      SYNAPTANE_SUPERADMIN_2FA: "false"  # the script signs in with the password alone
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

dc() { docker compose -p "$project" -f "$compose" -f "$work/forge.yaml" --env-file "$work/env" "$@"; }
cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then dc logs --tail 80 app worker agents migrate || true; fi
  dc down -v --remove-orphans >/dev/null 2>&1 || true
  docker volume rm -f "$forge" >/dev/null 2>&1 || true
  rm -rf "$work"
  exit "$status"
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }

# app, worker and the agents container do run on the expected image.
running_image() {
  local services
  services="$(dc config --services)"
  for service in app worker agents; do
    # The agents container came with 1.15: the previous version may not have it.
    grep -qx "$service" <<<"$services" || continue
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

# A CLI login as `codex login` leaves it in the CLIs' HOME (/agents): written where the CLIs
# run in the version that writes it (the worker up to 1.14), read back by the agents container
# after the upgrade, without signing in again (same volume, same HOME, same account).
fake_login() {
  local where=worker
  if grep -qx agents <<<"$(dc config --services)"; then where=agents; fi
  # shellcheck disable=SC2016 # expanded in the container
  dc exec -T "$where" sh -c 'mkdir -p "$HOME/.codex" && umask 077 && echo "{\"e2e\": 1}" > "$HOME/.codex/auth.json"'
}
check_login_kept() {
  step "CLI login kept by the agents container"
  # shellcheck disable=SC2016 # expanded in the container
  dc exec -T agents sh -c 'test "$(stat -c %u:%a /agents/.codex/auth.json)" = 1000:600 && grep -q e2e /agents/.codex/auth.json'
  echo "login of the previous version readable in the agents container"
}

# A subscription run through the agents container, the fake CLI of the tests mounted in place of
# `claude` (no login, no model call): login state, then a real call of the run's tool at the
# worker's address on their private network. The Claude subscription is set up last: the
# checks after the restore expect the Anthropic key.
check_agents_run() {
  step "Subscription run through the agents container (fake CLI)"
  cat > "$work/fake-cli.yaml" <<EOF
services:
  agents:
    volumes:
      - $PWD/tests/fake_cli_program.py:/usr/local/bin/claude:ro
EOF
  dc -f "$work/fake-cli.yaml" up -d --no-deps --wait agents
  send -X PUT "${api}/organizations/e2e/ai" -d '{"provider": "claude_code"}' > /dev/null
  send -X POST "${api}/organizations/e2e/ai/test" > /dev/null
  for _ in $(seq 1 60); do
    call "${api}/organizations/e2e/ai" > "$work/ai.json"
    if python3 -c 'import json, sys; d = json.load(sys.stdin); sys.exit(d["test_pending"] or d["test_ok"] is None)' \
      < "$work/ai.json"; then
      break
    fi
    sleep 2
  done
  cat "$work/ai.json"; echo
  expect '"test_ok":true' cat "$work/ai.json"
  expect "Tool call successful" cat "$work/ai.json"
  # The fake CLI's arguments: the exact options of a run.
  dc exec -T agents python -c 'import json; print(json.load(open("/agents/last-args.json")))' \
    > "$work/args"
  grep -q -- "--strict-mcp-config" "$work/args"
  dc up -d --no-deps --wait agents  # back to the image's CLI
}

# Agents container (subscription CLIs): no secret, no data, neither the database nor the web
# container within reach; the worker reaches it, with the key they share, and nobody else.
check_agents() {
  step "Agents container: isolated, reached by the worker only"
  local environment doctor
  environment="$(dc exec -T agents env)"
  if grep -E '^(SYNAPTANE_DATABASE_URL|POSTGRES_PASSWORD|SYNAPTANE_METRICS_TOKEN)=' <<<"$environment"; then
    echo "Secret of the instance in the agents container's environment" >&2; return 1
  fi
  # /data exists in the image, empty: no volume is mounted there.
  [ -z "$(dc exec -T agents ls -A /data)" ] || { echo "Data in the agents container" >&2; return 1; }
  # Names resolved by Docker on the networks a container is on: only the worker for agents.
  dc exec -T agents python - <<'PY'
import socket, sys
for host in ("postgres", "app"):
    try:
        socket.getaddrinfo(host, 80)
    except OSError:
        continue
    sys.exit(f"{host} reachable from the agents container")
socket.getaddrinfo("worker", 80)
print("agents: only the worker is within reach")
PY
  dc exec -T app python - <<'PY'
import socket, sys
try:
    socket.getaddrinfo("agents", 8700)
except OSError:
    print("app: the agents container is out of reach")
else:
    sys.exit("the web container reaches the agents container")
PY
  dc exec -T worker python - <<'PY'
import urllib.error, urllib.request
try:
    urllib.request.urlopen("http://agents:8700/v1/info", timeout=10)
except urllib.error.HTTPError as error:
    assert error.code == 401, error.code
    print("worker without the key: refused")
else:
    raise SystemExit("the agents container answers without the key")
PY
  for _ in $(seq 1 30); do
    doctor="$(dc exec -T worker synaptane doctor 2>&1 || true)"
    if grep -q '^\[ok\] Agents container (subscription CLIs' <<<"$doctor"; then break; fi
    sleep 2
  done
  grep '^\[[a-z]*\] Agents container' <<<"$doctor"
  grep -q '^\[ok\] Agents container (subscription CLIs' <<<"$doctor"
  grep -q '^\[ok\] Agents container isolated' <<<"$doctor"
  # Isolation contract of the CLIs where they run (a CLI missing from the image: a warning).
  dc exec -T agents synaptane check-clis --installed-only
}

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
  fake_login

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
  check_agents
  check_login_kept
else
  step "Start"
  dc up -d --no-build
  wait_ready
  running_image "$image"
  step "doctor"
  dc exec -T app synaptane doctor
  populate
  check_agents
fi

# Right after the start, before any manual backup: a full backup less than 6 hours old would
# count as the worker's backup of the day (as after a deployment).
step "Automatic backup by the worker, without any setting (BACKUP_TIME=00:00: right away)"
for _ in $(seq 1 60); do
  if dc exec -T app sh -c 'ls /backups/synaptane-*-auto.tar.gz' > "$work/automatic" 2> /dev/null; then
    break
  fi
  sleep 2
done
automatic="$(head -n 1 "$work/automatic")"
[ -n "$automatic" ] || { echo "No automatic backup in /backups after 2 minutes." >&2; exit 1; }
dc exec -T app tar -tzf "$automatic" > "$work/listing"
grep -qx "master.key" "$work/listing"
dc exec -T app synaptane backup check --no-test

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

check_agents_run

step "Run complete"
