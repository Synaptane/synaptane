English | [Français](api.fr.md)

# REST API

- Base: `/api/v1`.
- Interactive documentation: `/api/docs`, and schema: `/api/v1/openapi.json`. Published only
  if `api_docs = true` (variable `SYNAPTANE_API_DOCS`): they list every route, so there is no
  need to expose them on a production instance.

## Authentication

Create a personal token in **My account → Personal tokens**, then send it in a header:

```sh
curl -H "Authorization: Bearer syn_…" https://brain.example.com/api/v1/spaces
```

A token has at most the rights of its owner. It is revoked automatically if the password
changes or the account is deactivated.

A token can be **restricted** when it is created (web, or `POST /me/tokens` with
`{"name": …, "days": …, "read_only": true, "spaces": ["acme/infra"]}`):
- `read_only`: read and search only, no writes and no drops;
- `inbox_only`: read, search, and drop, correct or remove notes in the inbox, but no direct
  change to pages (not together with `read_only`);
- `spaces`: only these spaces, among those the account is a member of (or whose organization
  it administers); absent: all of them, including future ones. The web form checks only the
  spaces of the first organization (in alphabetical order) when the account belongs to
  several; the API applies no such default.

A restricted token administers nothing (instance, organization) and does not manage the
account: it cannot create a token, or change the password or email (403).

## Addressing

Organizations and spaces are designated by their readable identifiers:
`/organizations/acme/spaces/infra/members/alice`.

## Errors

An error returns `{"error": "<code>", "message": "<text>"}`, with the matching HTTP status.
The code never changes; the message is in English, or in French with
`Accept-Language: fr`. Test the code, not the message.

| Code | Status |
|---|---|
| `invalid` | 400 |
| `unauthenticated` | 401 |
| `forbidden` | 403 |
| `not_found` | 404 |
| `conflict` | 409 |
| `secret_detected` | 422 |
| `too_many_requests` | 429 (rate limit of an account or an organization: try again later) |
| `unavailable` | 503 (database, repository or server temporarily unavailable; `Retry-After` header) |

A malformed request body (missing field, wrong type) returns 422 in FastAPI's format:
`{"detail": [{"loc": […], "msg": …, "type": …}]}`. Too many sign-in attempts, or OAuth
registrations, return 429 as well.

`too_many_requests` limits, over a sliding window:
- inbox drops: 120 notes per hour per account, across all spaces and interfaces (web, REST,
  MCP), notes moved to another space included;
- **Tidy now** (`POST …/tidy`): 5 manual runs per person and per space over 24 hours (20 for
  an organization admin);
- invitations: 30 per organization per hour;
- email verification links: 3 per account per hour.

`secret_detected`: the content sent (drop, file write) seems to contain a secret (password, API
key, private key…). Nothing is committed. Remove it, or send the request again with
`"allow_secrets": true` if it is not one. For an existing page, only the added lines are
checked.

A resource the caller cannot see returns 404, like a resource that does not exist.

Rights: *super admin* (instance), *admin* (of the organization), *contributor* and *reader* (of
the space). An organization admin has all rights on its spaces.

## Account

| Method and path | Purpose |
|---|---|
| `GET /me` | Calling account: `username`, `display_name`, `email`, `locale`, `is_superadmin`, `is_active` |
| `GET /me/tokens` | Personal tokens: `id`, `name`, `hint` (last 4 characters), `created_at`, `expires_at`, `last_used_at`, `read_only`, `inbox_only`, `space_ids` |
| `POST /me/tokens` | `{"name": …, "days": 90, "read_only": false, "inbox_only": false, "spaces": ["acme/infra"]}` (201); `days: null`: no expiry. Returns the `token`, once only |
| `DELETE /me/tokens/{id}` | Revokes (204) |
| `GET /spaces` | All visible spaces: `organization`, `slug`, `name`, `description`, `repo_url`, `branch`, `role` |

## Accounts (super admin)

| Method and path | Purpose |
|---|---|
| `GET /users` | All accounts |
| `POST /users` | `{"username": …, "display_name": …, "password": …, "email": …, "is_superadmin": false}` (201) |
| `PATCH /users/{username}` | `display_name`, `email`, `locale`, `is_active`, `is_superadmin`. Anyone can change their own name and language; the rest is reserved for the super admin |

## Organizations

| Method and path | Right | Purpose |
|---|---|---|
| `GET /organizations` | member | Visible organizations: `slug`, `name`, `description`, `role` |
| `POST /organizations` | super admin | `{"slug": …, "name": …, "description": …, "join": true}` (201); `join`: the creator becomes its admin |
| `GET /organizations/{org}` | member | Details |
| `PATCH /organizations/{org}` | admin | `name`, `description` |
| `DELETE /organizations/{org}` | super admin | Deletes an organization that has no space (204; 400 if any remain) |
| `GET /organizations/{org}/members` | member | `username`, `display_name`, `role` (`admin` or `member`) |
| `PUT /organizations/{org}/members/{username}` | admin | `{"role": "member"}`: adds, or changes the role |
| `DELETE /organizations/{org}/members/{username}` | admin | Removes (204) |

## Spaces

| Method and path | Right | Purpose |
|---|---|---|
| `GET /organizations/{org}/spaces` | member | Visible spaces of the organization |
| `POST /organizations/{org}/spaces` | admin | `{"slug": …, "name": …, "description": …, "repo_url": …, "branch": "main"}` (201) |
| `GET …/spaces/{space}` | reader | Details, with the caller's `role` and `review_every` (default review interval of the pages, `""`: never) |
| `PATCH …/spaces/{space}` | admin | `name`, `description`, `repo_url`, `branch`, `review_every` (`90d`, `6m`, `1y`, `never`, `""`; 400 if unreadable). Changing `repo_url` removes the Git credentials |
| `DELETE …/spaces/{space}` | admin | Deletes the space in Synaptane; the remote repository is not touched (204) |
| `GET …/spaces/{space}/members` | reader | `username`, `display_name`, `role` (`reader` or `contributor`) |
| `PUT …/spaces/{space}/members/{username}` | admin | `{"role": "contributor"}` |
| `DELETE …/spaces/{space}/members/{username}` | admin | Removes (204) |
| `GET …/spaces/{space}/vocation` | reader | Purpose of the space (`.synaptane/vocation.md`): `path`, `text` (`""` if it does not exist), `sha`, `pending` (waiting for the first sync), `history` (last 5 changes: `commit`, `author`, `date`, `subject`) |
| `PUT …/spaces/{space}/vocation` | admin | `{"text": …, "base_sha": …, "allow_secrets": false}`: a commit in the caller's name, then a sync. `text`: 20,000 characters at most, `""` removes the file. `base_sha`: hash read before editing (`""`: the file must not exist); 409 if it changed in the meantime. 422 if a likely secret is detected. Returns `commit` (`null` if nothing changed) and `pending`: before the first sync, the text is held and written by that sync |
| `GET /vocation-templates` | account | Purpose templates: `key` (`infra`, `marketing`, `product`, `client`, `personal`, `family`, `job-search`, `blank`), `label`, `summary`, `text` (in the language of the account: English or French) |

`…` stands for `/organizations/{org}`. To create a space with a purpose, create it, then send
`PUT …/vocation` (the text waits for the first sync).

## Git of a space

| Method and path | Right | Purpose |
|---|---|---|
| `GET …/spaces/{space}/git` | reader | State: status, last sync, conflicts, webhook URL |
| `PUT …/spaces/{space}/git/credentials` | organization admin | `{"kind": "https", "token": …}` or `{"kind": "ssh", "private_key": …, "known_hosts": …}` |
| `DELETE …/spaces/{space}/git/credentials` | organization admin | Deletes the credentials |
| `POST …/spaces/{space}/git/sync` | contributor | Requests a sync (202) |
| `POST …/spaces/{space}/git/webhook-secret` | organization admin | Generates a secret, returns the URL and the secret |
| `POST …/spaces/{space}/git/conflicts/resolve` | organization admin | `{"choices": {"content/a.md": "local" or "remote"}}` (202) |

Incoming webhook (Git hosts): `POST /hooks/git/{org}/{space}`. It is authenticated by
`X-Hub-Signature-256` (GitHub), `X-Gitea-Signature` (Gitea) or `X-Gitlab-Token` (GitLab).

## Content of a space

Paths are relative to `content/`:
`…/spaces/infra/files/servers/proxmox.md`.

| Method and path | Right | Purpose |
|---|---|---|
| `GET …/spaces/{space}/tree` | reader | All files: path, size, hash |
| `GET …/spaces/{space}/files/{path}` | reader | `kind`, `sha`, and `text`: the content, or the text extracted from a PDF, an Office document or an image; for a Markdown page, `freshness` (see below) |
| `GET …/spaces/{space}/raw/{path}` | reader | Raw bytes |
| `PUT …/spaces/{space}/files/{path}` | contributor | `{"content": "…"}` or `{"content_base64": "…"}`, with `message`. Creating a file needs nothing more; **replacing** an existing file requires its `base_sha` (the `sha` read by `GET …/files/{path}`), otherwise 409: you do not overwrite a change you have not read. 409 as well if the file has changed since; `"base_sha": ""` requires a creation; `"force": true` knowingly replaces without `base_sha` |
| `DELETE …/spaces/{space}/files/{path}` | contributor | Deletes |
| `POST …/spaces/{space}/move` | contributor | `{"from": "a.md", "to": "b/a.md"}`; links to the file and its own links are rewritten in the same commit |
| `GET …/spaces/{space}/files/{path}/related?limit=8` | reader | Related pages (**See also** panel): `path`, `title`, `score` (0 to 1, nothing below 0.2), `reason` (main signal: `meaning` same topic, `words` shared words, `backlink` cites this page, `link` linked from this page, `shared` shared links, `folder` same folder), `reasons` (all of them, strongest first), `label` (the reason in plain words) and `linked` (the page already links to it). Empty list if nothing is close enough; `limit` from 1 to 20. A file named exactly `related` is read through `…/raw/{path}` |
| `POST …/spaces/{space}/verify/{path}` | contributor | Marks a Markdown page as verified today: `{"owner": …, "review_every": "6m", "verify": true}`, all optional (`owner` absent: unchanged, or the caller if the page has none; `review_every` `""`: the space's interval; `"verify": false`: owner and interval only). Returns `commit` (`null` if nothing changed), `path` and `freshness`. The commit only touches the front matter and is not reported to the tidy-up; 400 for a file that is not a Markdown page |
| `GET …/spaces/{space}/reports/{path}` | reader | Open reports that the page is outdated: `reason`, `by`, `at` |
| `POST …/spaces/{space}/reports/{path}` | reader | `{"reason": "…"}` (500 characters at most): reports a Markdown page as outdated; again: updates your reason. 201 |
| `DELETE …/spaces/{space}/reports/{path}` | reader | Withdraws your report; `?all=true` dismisses them all (contributor). A verification of the page closes them too |
| `GET …/spaces/{space}/history/{path}` | reader | Commits that touched the file (a deleted one too): `sha`, `author`, `date`, `subject` |
| `POST …/spaces/{space}/restore` | contributor | Puts a file back as it was in a past version, a deleted file too: `{"path": …, "commit": …}` (`commit`: a `sha` from the history), with `message`, `base_sha` (the current `sha`, 409 if the file changed since; `""` for a deleted file) and `allow_secrets`, optional. Returns `commit` (`null` if this version is already the current one). A page keeps its current verification fields |
| `GET …/spaces/{space}/health?months=12&limit=500` | reader | Wiki health: `broken` (page, link, suggestion), `orphans`, `empty`, `untitled`, `stale` (page, date), `due` (pages to review: `path`, `owner`, `verified`, `due`), `unowned` (verified without an owner) and their totals `counts` (with `verified`: verified, up-to-date pages) |

`freshness` (page read, search results, verification): `state` (`verified`, `stale` or
`never`), `verified` and `stale` (booleans), `verified_at`, `owner`, `review_due` (due date,
`null` without an interval) and `review_every` (effective interval). The fields are read from
the page's front matter (`owner`, `verified`, `review_every`), see
[the guide](../guide/wiki.md#verify-a-page).

Each write produces a commit in the caller's name, pushed at once. The commit message ends
with the channel and the token (`Source: api`, `Via: API · <token name>`); the page shows
up as a `change` item in the inbox, computed from the Git history.

## Search

`GET /api/v1/search?q=…&spaces=acme/infra&spaces=perso/notes&regex=false&limit=20&mode=auto`

Search covers all readable spaces if `spaces` is absent. Each result gives `space`, `path`,
`title`, `score`, `match` (`text`: by words, `meaning`: by meaning, `both`), `extracted` (found
in extracted text), `verified` and `stale` (page verified and up to date, or due for review),
`freshness` (details, `null` for non-Markdown files), the last change (`edited`, its date;
`edited_by`, its author; `edited_by_ai`: `tidy`, `routine` or `assistant` when an AI wrote
it, `null` for a person), `reported_outdated` (open reports that the page is outdated) and
up to three `snippets` (`line`, `text`, `before`, `after`). `mode=text`: by words only. A verified, up-to-date page ranks a
little higher (+10%), a page due for review a little lower (−7%).

**Pending notes.** A dropped note can only be found among the pages once it has been tidied.
To avoid breaking existing clients, the response remains a list of pages: the
`X-Pending-Count` header counts the pending notes that contain all the words of `q` (20 at
most), and `GET /api/v1/search/pending?q=…&spaces=…` lists them: `space`, `name` (for
`GET …/inbox/{name}`), `title`, `deposited_by`, `deposited_at`, newest first.

## Inbox

| Method and path | Right | Purpose |
|---|---|---|
| `GET …/spaces/{space}/inbox` | reader | Pending items, paginated (`limit`, `offset`; total in `X-Total-Count`), filtered by `kind` (`note`, `change` or `left`: left by the tidy-up): `name`, `type` (`note` or `change`), `title`, `deposited_by`, `deposited_at`, `status`, `reason`, `hint`, `source` (channel), `via` (client or token), `origin` (where the information comes from), `attachments` |
| `POST …/spaces/{space}/inbox` | contributor | `{"title": …, "content": …, "hint": …, "source": …, "attachments": [{"name": …, "content_base64": …}]}` (201). `source`: where the information comes from (URL, meeting…), stored in `origin` |
| `GET …/spaces/{space}/inbox/{name}` | reader | One item, with `body`: the text of a note, or the commits and the diff of a changed page |
| `PATCH …/spaces/{space}/inbox/{name}` | contributor | Corrects a note: `title`, `content`, `hint`, `source`, `allow_secrets`; an absent field stays unchanged. A note left by the tidy-up goes back to "to process". Returns the note |
| `DELETE …/spaces/{space}/inbox/{name}` | contributor | Removes a note and its attachments (204). For a problem an agent routine reported, `?not_a_problem=…` (why, 500 characters at most) records that it is not one: the routine will not report it again while its pages stay the same |
| `POST …/spaces/{space}/inbox/{name}/move` | contributor of both spaces | `{"space": "acme/other"}`: moves the note to the inbox of another space; returns `space` and `path` |
| `POST …/spaces/{space}/inbox/{name}/seen` | contributor | Marks a changed page (`change`) as seen: it asks nothing of the tidy-up until its next change (204) |
| `GET …/spaces/{space}/inbox/{name}/files/{filename}` | reader | Attachment of a note, as raw bytes (downloaded, never executed) |

## AI tidy-up

| Method and path | Right | Purpose |
|---|---|---|
| `GET /organizations/{org}/ai` | admin | AI settings, or `null`: `provider`, `model`, `base_url`, `effort`, `has_key`, `price_input`, `price_output` (per million tokens), `monthly_budget`, `month_spent`, `metered` (provider billed per use: without `monthly_budget`, spending has no limit) |
| `PUT /organizations/{org}/ai` | admin | `{"provider": "anthropic", "model": …, "effort": "medium", "api_key": …, "monthly_budget": …}`. `api_key` absent: the key is kept, unless the provider or address changes; `base_url` is reserved for the super admin. A `monthly_budget` is refused (400) if the organization's model, or the model chosen by one of its spaces, has no known price and no price is entered |
| `POST /organizations/{org}/ai/test` | admin | Test call: `{"message": …}` |
| `GET …/spaces/{space}/tidy-rules` | admin | Tidy-up rules of the space (`.synaptane/prompts/tidy.md`, or its former name `.synaptane/prompts/rangement.md` if it is the only one; `path` says which): same shape as `GET …/vocation` |
| `PUT …/spaces/{space}/tidy-rules` | admin | Same fields and same rules as `PUT …/vocation` |
| `PATCH …/spaces/{space}/tidy-settings` | admin | `enabled`, `time` (`"03:00"`, local time), `timezone` (`Europe/Paris`), `model` (`""`: the organization's), `delay` (minutes between a drop and the continuous tidy-up, `0`: off) (204) |
| `POST …/spaces/{space}/tidy` | contributor | Starts a tidy-up (202); 429 beyond the manual run limit (see [Errors](#errors)) |
| `GET …/spaces/{space}/tidy/runs` | reader | Runs: `status`, `trigger`, `model`, `items_done`, `items_left`, `summary`, `operations`, `commit`, tokens used, `cost`, `error`, `reverted_at` |
| `POST …/spaces/{space}/tidy/runs/{id}/revert` | admin | Reverts a run with an inverse commit |
| `GET …/spaces/{space}/tidy/runs/{id}/pages` | reader | Pages the run changed: `path`, `status` (`created`, `edited`, `deleted`, `moved`), `old` (former path of a moved page), `state` (`undoable`, `changed` since the run, `undone`) |
| `POST …/spaces/{space}/tidy/runs/{id}/undo-pages` | contributor | `{"paths": […]}`: puts these pages back as they were before the run, in one commit. 409 for a page changed since the run (restore it from its history). Returns `commit`, `pages`, `notes_back` (notes back in the inbox: all their pages are undone) and `notes_kept` (still filed: other pages keep them) |

## Agent routines

A routine is a scheduled AI agent that rereads the wiki of a space and only writes to its
outputs (see [Agent routines](../admin/ai.md#agent-routines-per-space)). Paths under
`…/spaces/{space}/routines`.

| Method and path | Right | Purpose |
|---|---|---|
| `GET …/routines` | contributor | Routines of the space: `id`, `name`, `template` (`coherence`, `digest` or `""`), `instructions`, `frequency` (`daily`, `weekly`, `monthly`), `weekday` (0 = Monday), `monthday` (1 to 28), `at` (`"06:00"`, the space's local time), `scope` (folders; empty: the whole space), `output_page`, `output_inbox`, `output_email` (`""`, `admins`, `members`), `output_webhook`, `enabled`, `next_run`, `last_run` |
| `POST …/routines` | admin | Creates a routine (201). `{"template": "coherence"}` starts from a provided template (its settings, then the given fields); blank: `name`, `instructions` and at least one output |
| `GET …/routines/{id}` | contributor | One routine |
| `PATCH …/routines/{id}` | admin | Changes the given fields (`enabled: false` disables it) |
| `DELETE …/routines/{id}` | admin | Deletes the routine (204); its runs still count toward the budget, its report page stays in the wiki |
| `POST …/routines/{id}/run` | admin | Runs the routine as soon as the worker is free (202); refused (400) if the month's budget is reached |
| `GET …/routines/{id}/runs` | contributor | Runs, newest first: `status` (`running`, `done`, `failed`), `trigger`, `model`, `summary`, `report` (output text), `outputs` (`page`, `inbox`: titles of the items dropped, `emails`: number sent, `webhook`), `commit`, tokens, `cost`, `quota`, `error` |

## Semantic search

| Method and path | Right | Purpose |
|---|---|---|
| `GET /organizations/{org}/embeddings` | admin | Embeddings provider, or `null`: `provider`, `model`, `base_url`, `has_key`, `meaning_min_similarity` |
| `PUT /organizations/{org}/embeddings` | admin | `{"provider": "openai", "model": …, "api_key": …, "meaning_min_similarity": 0.55}` (similarity floor, `null` by default; dropped when the model changes) |
| `PUT …/spaces/{space}/semantic` | admin | `{"enabled": true}`: enables the space's index; returns the `files` and `chunks` indexed |

## SSO sign-in (super admin)

| Method and path | Purpose |
|---|---|
| `GET /sso-providers` | Providers: `slug`, `kind`, `name`, `issuer`, `client_id`, `auto_create`, `link_by_email`, `allowed_domains`, `enabled`, `callback_url`, `open_to_anyone` (automatic creation without allowed domains on GitHub, Google or gitlab.com: anyone with an account there gets one here) |
| `PUT /sso-providers/{slug}` | Creates or updates: `{"kind": "gitlab", "name": …, "client_id": …, "client_secret": …, "issuer": …, "auto_create": false, "link_by_email": null, "allowed_domains": ["example.com"], "enabled": true}` (`client_secret: null` keeps the secret; `auto_create` absent: `false`; `link_by_email: null`: yes for GitHub and Google, no otherwise; `allowed_domains`: email domains accepted for automatic linking and creation, exact match, `[]`: no restriction) |
| `DELETE /sso-providers/{slug}` | Deletes (204) |

See [SSO](../admin/sso.md).

## Webhooks (organization admin)

| Method and path | Purpose |
|---|---|
| `GET /organizations/{org}/webhooks` | List: `id`, `url`, `events`, `active`, `last_delivery_at`, `last_status`, `failures` (consecutive failed deliveries) |
| `POST /organizations/{org}/webhooks` | `{"url": …, "events": [...]}` (all events by default). Returns the `secret`, once only (201) |
| `DELETE /organizations/{org}/webhooks/{id}` | Deletes (204) |
| `POST /organizations/{org}/webhooks/{id}/test` | Sends a `ping` event (202) |
| `POST /organizations/{org}/webhooks/{id}/reactivate` | Puts back into service a webhook switched off after repeated failures (204) |

See [Webhooks](../admin/webhooks.md) for the format and the signature.

## Invitations (organization admin)

| Method and path | Purpose |
|---|---|
| `GET /organizations/{org}/invitations` | Pending invitations: `id`, `email`, `org_role`, `expires_at` |
| `POST /organizations/{org}/invitations` | `{"email": …, "org_role": "member", "spaces": {"infra": "reader"}}` (201). Returns the `link` to pass on, once only; `granted: true` if the address already had an account (access granted at once). 30 per organization per hour at most (429 beyond) |
| `DELETE /organizations/{org}/invitations/{id}` | Cancels (204) |

## MCP

`/mcp` (Streamable HTTP), authenticated by OAuth 2.1 (dynamic client registration, PKCE) or by
a personal token. See [the MCP guide](../guide/mcp.md). The server speaks the handshake
protocols (up to November 25, 2025) and the stateless protocol of July 28, 2026. Client ID
metadata documents (CIMD), which replace dynamic registration in that revision, are not
accepted yet: the SDK does not handle them on the server side.

Dynamic registration (`POST /register`) is limited to 20 per hour per IP address (429
beyond). A client that registered but was never authorized is deleted after 24 hours.
