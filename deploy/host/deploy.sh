#!/usr/bin/env bash
# Synaptane update on the server, with automatic rollback.
#
#   deploy/host/deploy.sh                    # latest published release (v* tag)
#   deploy/host/deploy.sh v1.0.1             # a tag, a branch or a commit
#   deploy/host/deploy.sh origin/main        # latest commit of main (in development)
#   deploy/host/deploy.sh --check-ci         # only if this commit's CI is green
#   DRY_RUN=1 deploy/host/deploy.sh          # dry run: shows what would be done
#
# Options:
#   --check-ci    asks the GitHub API whether this commit's CI (ci.yml) is green, and
#                 refuses otherwise. Private repository: token in GITHUB_TOKEN (fine-grained:
#                 Actions read-only); not needed for a public repository.
#   --no-backup   no backup beforehand: only if the application is not running
#                 (first installation). Without a backup, no restore is possible.
#
# Steps:
#   1. `git fetch` (with the deploy key, see README.md), target commit;
#   2. current state: commit, image, database revision (`alembic_version`);
#   3. backup by backup.sh; the current image is kept as `synaptane:precedent`;
#   4. `git checkout --detach`, build with GIT_SHA, image also named
#      `synaptane:<sha>`, then `docker compose up -d --no-build`;
#   5. checks, for at most DEPLOY_TIMEOUT seconds (180 by default): /readyz at 200,
#      `synaptane --version` at the right commit, worker `healthy`, `synaptane doctor`;
#   6. on failure, automatic rollback: previous commit and image, and restore of the
#      backup **only if** the database revision changed (a migration ran: the old code
#      could not read the new database);
#   7. a "date commit result" line in the log (STATE_DIR/deploy.log).
#
# Settings through environment variables:
#   SYNAPTANE_DIR    copy of the repository (default: this script's)
#   STATE_DIR        log and lock (default ~/.local/state/synaptane)
#   BACKUP_DIR, RETENTION_DAYS, OFFSITE: passed to backup.sh
#   DEPLOY_TIMEOUT   check timeout, in seconds (default 180)
#   GITHUB_REPO      repository for --check-ci (default: derived from the origin URL)
#
# Exit codes: 0 deployed; 1 failure (the log says whether the rollback succeeded);
# 2 usage, or server state to fix before deploying.
#
# Everything is in functions, called by the last line: bash reads a script as it runs,
# and `git checkout` replaces this file while it is running.
set -euo pipefail

usage() { sed -n '2,/^set -euo/{/^#/s/^# \{0,1\}//p}' "${BASH_SOURCE[0]}"; }
say() { printf '\n== %s\n' "$*"; }
die() { echo "$*" >&2; exit 2; }
dry() { [ "${DRY_RUN:-0}" = 1 ]; }

# Command that changes something: only printed in a dry run.
run() {
  if dry; then
    printf '[dry run] %s\n' "$*"
  else
    "$@"
  fi
}

journal() {
  dry && return 0
  printf '%s %s %s\n' "$(date -Is)" "$short" "$*" >> "$state/deploy.log"
}

# Alembic revision of the database, read from PostgreSQL; "?" if the database does not answer.
alembic_revision() {
  docker compose exec -T postgres \
    psql -U synaptane -d synaptane -tAc "SELECT version_num FROM alembic_version" \
    2>/dev/null | tr -d '[:space:]' || true
}

# Short commit reported by `synaptane --version` in the app container (empty otherwise).
running_commit() {
  docker compose exec -T app synaptane --version 2>/dev/null \
    | sed -nE 's/.*\(([^)]*)\).*/\1/p' || true
}

# One round of checks. Returns 1 with the reason in `why`.
check_once() {
  local expected=$1 address code cid health
  address="$(docker compose port app 8000 2>/dev/null || true)"
  if [ -z "$address" ]; then why="app container missing"; return 1; fi
  code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://$address/readyz" || true)"
  if [ "$code" != 200 ]; then why="/readyz answers ${code:-nothing}"; return 1; fi
  if [ -n "$expected" ] && [ "$(running_commit)" != "$expected" ]; then
    why="synaptane --version does not report $expected"
    return 1
  fi
  cid="$(docker compose ps -q worker 2>/dev/null || true)"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
    "$cid" 2>/dev/null || true)"
  if [ "$health" != healthy ]; then why="worker ${health:-stopped}"; return 1; fi
  if ! docker compose exec -T app synaptane doctor > "$state/doctor.txt" 2>&1; then
    why="synaptane doctor failed (see $state/doctor.txt)"
    return 1
  fi
}

# Checks repeated until success or timeout. $1: expected short commit (empty: ignored).
verify() {
  local expected=$1 deadline
  if dry; then
    echo "[dry run] checks: /readyz, version ${expected:-?}, worker, doctor"
    return 0
  fi
  deadline=$((SECONDS + ${DEPLOY_TIMEOUT:-180}))
  why=""
  until check_once "$expected"; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "Check failed after ${DEPLOY_TIMEOUT:-180} s: $why" >&2
      return 1
    fi
    sleep "${CHECK_INTERVAL:-5}"
  done
  echo "Checks passed: /readyz, version ${expected:-(not checked)}, worker, doctor."
}

check_ci() {
  local sha=$1 slug answer count
  slug="${GITHUB_REPO:-$(git remote get-url origin | sed -E 's#^.*[:/]([^/]+/[^/]+)$#\1#; s#\.git$##')}"
  local auth=()
  [ -z "${GITHUB_TOKEN:-}" ] || auth=(-H "Authorization: Bearer $GITHUB_TOKEN")
  answer="$(curl -fsS -m 20 "${auth[@]}" -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$slug/actions/workflows/ci.yml/runs?head_sha=$sha&status=success&per_page=1")" \
    || die "GitHub API unreachable for $slug (private repository: set GITHUB_TOKEN, fine-grained token, Actions read-only)."
  count="$(sed -nE 's/.*"total_count": *([0-9]+).*/\1/p' <<<"$answer" | head -n 1)"
  [ "${count:-0}" -gt 0 ] \
    || die "No green CI for ${sha:0:7} on $slug: deployment refused (wait for the CI to finish)."
  echo "Green CI for ${sha:0:7}."
}

# Restores the archive with the previous image (put back under its name by rollback).
restore_backup() {
  run docker compose stop app worker || return 1
  if dry; then
    echo "[dry run] docker compose run --rm --no-deps -T app synaptane restore --force - < $archive"
  else
    docker compose run --rm --no-deps -T app synaptane restore --force - < "$archive"
  fi
}

rollback() {
  local now
  say "Rolling back to ${prev_commit:0:7}"
  # Called inside an `if`: `set -e` does not apply here, so each failure is returned by hand.
  run git checkout --quiet --detach "$prev_commit" || return 1
  if [ -z "$prev_image" ]; then
    echo "No previous image (first installation): nothing to put back." >&2
    return 1
  fi
  run docker tag synaptane:precedent "$image_name" || return 1
  now="$(alembic_revision)"
  if [ "$now" != "$rev_before" ]; then
    if [ -z "$archive" ]; then
      echo "The database went from ${rev_before:-?} to ${now:-?} and there is no backup:" \
        "manual restore needed (docs/admin/installation.md)." >&2
      return 1
    fi
    echo "Database went from ${rev_before:-?} to ${now:-?}: restoring $archive."
    restore_backup || return 1
  else
    echo "Database unchanged (${now:-?}): no restore."
  fi
  run docker compose up -d --no-build || return 1
  verify "$prev_running"
}

main() {
  local ref="" check_ci=0 backup=1 here
  while [ $# -gt 0 ]; do
    case "$1" in
      --check-ci) check_ci=1 ;;
      --no-backup) backup=0 ;;
      -h | --help) usage; exit 0 ;;
      -*) die "Unknown option: $1 (see --help)" ;;
      *) ref=$1 ;;
    esac
    shift
  done

  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  repo="${SYNAPTANE_DIR:-$(cd "$here/../.." && pwd)}"
  state="${STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/synaptane}"
  export SYNAPTANE_DIR="$repo"  # for backup.sh
  cd "$repo"
  [ -f .env ] || die "No .env in $repo: see docs/admin/installation.md."
  [ "$(stat -c %a .env)" = 600 ] \
    || echo "Warning: .env (database password) readable by others: chmod 600 .env" >&2
  mkdir -p "$state"
  exec 9> "$state/deploy.lock"
  flock -n 9 || die "Another deployment is in progress (lock $state/deploy.lock)."
  [ -z "$(git status --porcelain --untracked-files=no)" ] \
    || die "Local changes in $repo (git status): put them away before deploying."

  say "1. Code"
  git fetch --quiet --prune --tags origin
  if [ -z "$ref" ]; then
    # Latest published release (vX.Y.Z, pre-releases vX.Y.Z-rc.N included, nothing else);
    # `versionsort.suffix` sorts v1.0.0-rc.1 before v1.0.0.
    ref="$(git -c versionsort.suffix=- tag --list 'v[0-9]*' --sort=-v:refname \
      | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$' | head -n 1 || true)"
    [ -n "$ref" ] || die "No published release (v* tag): give the reference, for example origin/main."
  fi
  target="$(git rev-parse --verify --quiet "$ref^{commit}")" || die "Unknown reference: $ref"
  short="${target:0:7}"
  prev_commit="$(git rev-parse HEAD)"
  echo "Current: ${prev_commit:0:7}; target: $short ($ref)"
  [ "$check_ci" = 0 ] || check_ci "$target"

  say "2. Current state"
  local cid
  cid="$(docker compose ps -q app 2>/dev/null || true)"
  if [ -n "$cid" ]; then
    image_name="$(docker inspect -f '{{.Config.Image}}' "$cid")"
    prev_image="$(docker inspect -f '{{.Image}}' "$cid")"
    prev_running="$(running_commit)"
    # Image from before GIT_SHA ("inconnu" up to 1.0.0.dev0, "unknown" afterwards).
    case "$prev_running" in inconnu | unknown) prev_running="" ;; esac
  else
    # No container: image name from compose.yaml and .env (PostgreSQL excluded).
    image_name="$(docker compose config --images app | sed -n '/^pgvector\//!{p;q;}')"
    prev_image="" prev_running=""
  fi
  rev_before="$(alembic_revision)"
  echo "Image ${image_name} (${prev_image:-none}), commit ${prev_running:-unknown}, database ${rev_before:-?}"

  archive=""
  if [ "$backup" = 1 ]; then
    say "3. Backup"
    [ -n "$cid" ] || die "The application is not running: backup impossible (--no-backup to override)."
    if dry; then
      echo "[dry run] $here/backup.sh avant-$short"
    else
      archive="$("$here/backup.sh" "avant-$short")" || {
        journal "refused: backup impossible"
        echo "Backup impossible: nothing was changed." >&2
        exit 1
      }
      echo "Backup: $archive"
    fi
  fi
  [ -z "$prev_image" ] || run docker tag "$prev_image" synaptane:precedent

  say "4. Building $short"
  run git checkout --quiet --detach "$target"
  # This script builds from source and, on failure, puts the previous image back under the
  # running name: compose.yaml (and .env) must therefore keep that name, and not the name of
  # the published image, which a `docker compose pull` would replace.
  local built
  built="$(docker compose config --images app | sed -n '/^pgvector\//!{p;q;}')"
  if [ "$built" != "$image_name" ] || [[ "$built" == ghcr.io/synaptane/* ]]; then
    run git checkout --quiet --detach "$prev_commit"
    journal "refused: image \"$built\" instead of \"$image_name\""
    die "compose.yaml would name the built image \"$built\" (running: \"$image_name\").
This script builds from source: set the name in .env, for example
SYNAPTANE_IMAGE=synaptane:latest, then run it again. The published image is installed without
this script: docker compose pull && docker compose up -d"
  fi
  if dry; then
    echo "[dry run] GIT_SHA=$target docker compose build --pull"
  elif ! GIT_SHA="$target" docker compose build --pull; then
    git checkout --quiet --detach "$prev_commit"
    journal "build failed, nothing changed"
    echo "Build failed: the running service was not touched." >&2
    exit 1
  fi
  run docker tag "$image_name" "synaptane:$short"

  say "5. Start and checks"
  if run docker compose up -d --no-build && verify "$short"; then
    journal "ok"
    # Images of older deployments: the last three are kept.
    if ! dry; then
      # (`docker image ls` lists from newest to oldest.)
      docker image ls --format '{{.Repository}}:{{.Tag}}' synaptane \
        | grep -E '^synaptane:[0-9a-f]{7}$' | tail -n +4 \
        | xargs -r docker rmi >/dev/null 2>&1 || true
      docker image prune -f >/dev/null 2>&1 || true
    fi
    say "Deployed: $short"
    return 0
  fi

  if rollback; then
    journal "failed, rolled back to ${prev_commit:0:7}: ok"
    say "Deployment of $short failed: rollback to ${prev_commit:0:7} succeeded"
  else
    journal "failed, rollback to ${prev_commit:0:7}: FAILED, intervention needed"
    say "Deployment of $short failed, and so did the rollback: intervention needed"
    echo "Backup from before the deployment: ${archive:-none}" >&2
  fi
  exit 1
}

main "$@"; exit $?
