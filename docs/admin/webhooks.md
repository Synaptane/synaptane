English | [Français](webhooks.fr.md)

# Webhooks

An organization admin can notify other tools (Slack, n8n, a CI…) of what happens in the
organization's spaces. Configure them on the organization page, **Webhooks** section, or
through the API (`/api/v1/organizations/{org}/webhooks`).

## Events

| Event | When | `data` |
|---|---|---|
| `inbox.item_added` | Something is dropped into the inbox (web, REST, MCP) | `path`, `title`, `source`, `via`, `origin`, `by` |
| `content.changed` | Content changes: web, REST, MCP, tidy-up or push to the repository | `paths`, `action` (`written`, `deleted`, `moved`, `tidied`, `pushed`), `by`, `source`, `via` (web, REST, MCP writes) |
| `tidy.completed` | A tidy-up run ends, successfully or not | `status`, `items_done`, `items_left`, `summary`, `commit`, `error` |
| `routine.done` | A routine run ends, successfully or not (only for routines that have the webhook among their outputs) | `routine`, `template`, `status`, `summary`, `text` (output text), `page`, `inbox` (titles of the items dropped), `commit`, `error` |
| `tidy.budget` | The organization's monthly AI budget crosses 80% or 100% | `level`, `spent`, `budget` |
| `space.alert` | Alert to the admins: `deletion_cap` (tidy-up deletion cap), `tidy_failed` (tidy-up failing persistently), `sync_error` (sync failing persistently, or failing transiently for more than 6 h), `tidy_interrupted` (tidy-up stopped before the end: provider error, cap, budget; or items left after 3 interrupted runs), `job_abandoned` (background job abandoned after 5 attempts on a transient error: `task`, `attempts`, `error`), `inbox_backlog` (items waiting for more than 3 days), `quota_high` (an AI subscription quota above 90%), `big_changes` (tidy-up that made large changes: pages heavily shortened, many moves), `routine_failed` (agent routine failing: `routine`, `error`) | `kind`, details depending on the type |
| `sync.conflict` | A space goes into conflict with its remote repository | `paths` |
| `ping` | **Send a test** button | `message` |

## Format

A JSON `POST` request:

```json
{
  "id": "5f0c…",
  "event": "content.changed",
  "occurred_at": "2026-10-03T21:04:11+00:00",
  "organization": "acme",
  "space": "acme/infra",
  "data": {"paths": ["storage/nas.md"], "action": "written", "by": "alice", "source": "mcp"}
}
```

Headers:
- `X-Synaptane-Event`: the event name;
- `X-Synaptane-Delivery`: the delivery identifier (the same on every retry);
- `X-Synaptane-Signature`: `sha256=` followed by the HMAC-SHA256 of the raw body, with the
  webhook secret.

The secret (`whsec_…`) is shown only once, at creation. To change it, delete the webhook and
create it again.

Verification in Python:

```python
import hashlib, hmac


def valid(secret: str, body: bytes, signature: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

## Delivery

Deliveries are sent by the worker.
- A 2xx response is a success.
- A 5xx or 429 response, or a network error, leads to a retry: up to 5 attempts, after
  30 s, 2 min, 10 min, then 1 h.
- Other 4xx responses are not retried.
- Redirects are not followed.

The latest status is shown on the organization page.

The address must use HTTPS. To keep a webhook from being used to probe the server's internal
network, private, loopback and link-local addresses are refused at delivery time. In
development, the `allow_private_webhooks` option lifts both restrictions.
