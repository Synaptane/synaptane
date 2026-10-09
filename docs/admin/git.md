English | [Français](git.fr.md)

# Git repositories

Each space maps to **one branch of a Git repository**: GitHub, GitLab, Gitea or any Git
server. This repository is the source of truth: Synaptane commits and pushes its changes to
it, and fetches the changes made elsewhere.

## Repository layout

```
content/                         visible: wiki, search, MCP
.synaptane/inbox/                items waiting to be tidied up
.synaptane/vocation.md           the space's purpose (optional)
.synaptane/prompts/tidy.md       tidy-up instructions (optional)
```

The former name of the instructions file, `.synaptane/prompts/rangement.md`, is still read
when it is the only one; the first save from Synaptane replaces it with `tidy.md`.

The purpose and the instructions are edited in **Settings → Purpose** or with a push (see
[the tidy-up](ai.md#purpose-and-tidy-up-instructions)).

On the first sync, Synaptane creates what is missing:
- `content/README.md`, if `content/` does not exist;
- `.synaptane/inbox/`.

The rest of the repository is ignored.

## 1. Prepare access

**GitHub, over HTTPS**: create a *fine-grained* token limited to the repository, with the
**Contents** permission set to read and write.

**GitLab, over HTTPS**: create a project access token (Settings → Access tokens) with the
`read_repository` and `write_repository` scopes, and the role:
- **Maintainer** if the branch is protected, which is the case for `main` by default;
- *Developer* is enough if the branch is not protected, or if Developers are allowed to push
  to it.

The *Guest* role, sometimes offered by default, cannot read the code of a private project:
the sync then fails with "You are not allowed to download code" (403). The username can be
left empty: GitLab accepts any name with a project token.

**SSH (any Git host)**: create a dedicated deploy key, with write access:

```sh
ssh-keygen -t ed25519 -N "" -C synaptane -f synaptane-deploy
# Add synaptane-deploy.pub to the Git host (Deploy keys, "Allow write access"),
# paste synaptane-deploy into Synaptane.
```

Changing the repository URL removes the saved credentials: they are never sent to any
repository other than the one they were entered for.

## 2. Configure the space

On the space page (organization admin):

1. **Settings**: repository URL and branch.
   - HTTPS: `https://github.com/acme/brain.git`.
   - SSH: `git@github.com:acme/brain.git` or `ssh://git@host:2222/acme/brain.git`.
   - Never put credentials in the URL.
2. **Repository access**: paste the token, or the private key.
   - Over SSH, **Read the SSH server keys** offers the host keys with their fingerprints.
     Compare them with the ones published by the Git host (GitHub publishes them in its
     "SSH key fingerprints" documentation) before saving.

Saving the credentials starts a sync. The **Repository** block then shows the status: up to
date, error (with Git's message) or in conflict.

Secrets are encrypted with the master key and never shown again. They never appear on Git's
command line or in its configuration.

## 3. Webhook (recommended)

Without a webhook, Synaptane checks the repository every `poll_minutes`, 5 minutes by
default. With a webhook, each push triggers an immediate sync.

1. **Webhook** block → **Generate the secret**.
2. On the Git host:
   - **GitHub**: Settings → Webhooks → Add webhook. Enter the URL shown, the content type
     `application/json`, the secret, and the *Just the push event* event.
   - **GitLab**: Settings → Webhooks → Add new webhook. Enter the URL and the *Secret token*,
     tick only *Push events*, and leave SSL verification on. **Test → Push events** must
     answer 202.
   - **Gitea**: same as GitHub.

The webhook URL is built from `public_url`, which must be reachable by the Git host.

## What the sync does

- It fetches the branch, then merges in what is new: fast-forward, or a merge if Synaptane
  also has unpushed commits.
- It pushes Synaptane's commits right away, never forcing. If the repository moved on in the
  meantime, it fetches, merges again and pushes again (three rounds at most).
- Pages changed by commits made **outside Synaptane** show up in the inbox (`change` items,
  computed from the history: nothing is written to the repository). The nightly tidy-up
  folds them in: links, location, consistency. On the first clone, the existing history is
  not treated as new.
- It records readable commits:
  - the author is the person acting, or for automatic actions `Synaptane` (space set-up,
    merges), `Synaptane tidy-up` (tidy-up runs), `Synaptane routine (<name>)` (routines) or
    `Synaptane recovery` (local changes reapplied after a history change);
  - the committer is `Synaptane <synaptane@public-host>`;
  - subjects are in English: `Inbox: …` (notes), `Tidy-up: …`, `Routine "<name>": …`,
    `Synaptane: initialize space`, `Synaptane: merge remote changes`… Histories written by
    older versions keep their French names and subjects (`Synaptane – rangement`,
    `Rangement : …`, `Inbox : …`), still recognized: see
    [Repository format](../reference/repository-format.md#commits).

Nothing local is lost when the history changes under Synaptane:
- **URL or branch changed** (moving to another Git host, corrected repository): the local
  copy is rebuilt from the new repository, then the pages and notes changed locally are
  reapplied to it and pushed.
- **Remote history rewritten** (force-push, a leaked secret purged, branch recreated):
  Synaptane adopts the new history; the removed commits do not come back. Only the changes
  made in Synaptane since the last sync are reapplied.

In both cases, the old local branch is first saved in full in a Git bundle
(`data/recuperation/<space id>/<date>.bundle`, which `git clone` can restore). A file changed
on both sides is not reapplied: it stays in the bundle, and the audit log
(`space.local_changes_recovered`) lists the files concerned.

Changing only the address of the **same** repository (switching from HTTPS to SSH, a new
domain name for the Git host) loses nothing for the tidy-up: changed pages waiting to be
tidied and pages left aside with a reason stay in the inbox. They are only recomputed if the
new repository does not contain the history already reviewed.

A local copy left half-done by a killed process (index lock, interrupted merge) is repaired
automatically on the next operation.

## Slow or unavailable Git host

A Git host that stops responding never blocks Synaptane: web, REST and MCP writes and the
tidy-up carry on with the local copy, and are sent on the next sync. All exchanges with the
Git host (clone, fetch, push) run without holding the local copy; only the local steps
(merge, commit) reserve it, for a few moments.

Timeouts:
- **HTTPS**: a transfer below 1,000 bytes per second for 60 seconds is abandoned;
- **SSH**: the connection is abandoned after 20 seconds, and the session is cut after one
  minute without an answer from the server;
- **overall timeout** for one exchange: 5 minutes (`git_timeout`, in seconds, see
  [the installation](installation.md)). Only raise it if the first clone of a very large
  repository does not complete.

An interrupted exchange puts the space **in error**, with Git's message; what was already
fetched and merged is kept. The sync is retried automatically.

The HTTPS token is only given to the host of the repository address: if the Git host
redirects to another server, that server does not receive it.

## Conflicts

When the same file was changed on both sides:
- the space goes **into conflict** and nothing is pushed;
- the **Conflicts to resolve** block lists the files, and for each one you keep either
  Synaptane's version or the remote repository's;
- the resolution produces a merge commit, in the name of the person who decided, stating
  which version was kept for each file. The other version stays in the Git history.

## The worker

The sync runs in a separate process:

```sh
uv run synaptane worker
```

It must run at all times next to `synaptane serve`. Several workers can run at the same time
safely: each job is taken only once, and each space is handled by only one worker at a time.
A network error is retried automatically, 5 times at most. An authentication error is shown
as is on the space page.
