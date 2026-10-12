"""Jobs queued by the real service, then run by the worker.

The worker handlers read back the payload (`by`, `chain`, `choices`, `webhook_id`, `body`): a
misnamed key between `jobs.enqueue` and `runner.py` would pass the tests that call the
service directly. Here, each job type makes the full trip.
"""

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.ai import cli_agents
from synaptane.ai.base import AIConfig, ToolSpec, Usage
from synaptane.config import Settings
from synaptane.services import ai_settings, deposits, git_sync, tidy, webhooks
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Job, Space, TidyRun, Webhook
from synaptane.worker.runner import INDEX, Worker
from tests.integration.gitutil import ALICE, git
from tests.integration.wikidata import Wiki


def statuses(database: Database, kind: str) -> list[str]:
    with database.session() as session:
        return list(session.scalars(select(Job.status).where(Job.kind == kind)))


def test_tidy_requested_then_chained_by_the_worker(
    settings: Settings,
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`request_run` then the chained run: who requested it (`by`) follows the whole chain,
    and the rank (`chain`) is read back, otherwise the `MAX_CHAIN` limit would never stop it."""
    monkeypatch.setattr(tidy, "MAX_ITEMS", 1)
    monkeypatch.setattr(tidy, "MAX_CHAIN", 1)
    ai_settings.configure(db, wiki.root, ctx.box, wiki.infra.organization, provider="claude_code")
    for n in range(3):
        deposits.deposit(
            db, database, wiki.alice, ctx, wiki.infra, title=f"Note {n}", body="x", source="web"
        )
    db.commit()

    def close_all(
        config: AIConfig, system: str, prompt: str, specs: list[ToolSpec], handler: Any
    ) -> cli_agents.CLIRun:
        for line in prompt.splitlines():
            if line.startswith("- "):
                name = line[2:].split(" [", 1)[0]
                handler("read_inbox_item", {"item": name})
                handler("mark_done", {"item": name, "summary": "vu"})
        handler("finish", {"summary": "fini"})
        return cli_agents.CLIRun(Usage(input_tokens=100, output_tokens=10, turns=1), "Fini")

    monkeypatch.setattr(cli_agents, "run_agent", close_all)
    tidy.request_run(db, wiki.alice, wiki.infra)
    db.commit()
    worker = Worker(settings, database, ctx)
    assert worker.run_once(kinds={tidy.TIDY})
    assert worker.run_once(kinds={tidy.TIDY})  # chained run, queued by the first one
    assert not worker.run_once(kinds={tidy.TIDY})  # rank 1 = MAX_CHAIN: the chain stops
    with database.session() as session:
        runs = [
            (r.trigger, r.requested_by, r.status, r.items_done)
            for r in session.scalars(select(TidyRun).order_by(TidyRun.started_at))
        ]
    assert runs == [
        ("manual", wiki.alice.user.id, "done", 1),
        ("chain", wiki.alice.user.id, "done", 1),
    ]
    assert statuses(database, tidy.TIDY) == ["done", "done"]
    assert len(deposits.list_items(wiki.alice, ctx, wiki.infra)) == 1  # for the nightly run


def test_conflict_resolution_requested_then_run_by_the_worker(
    settings: Settings, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """`request_resolution`: the choices (`choices`) are applied and the merge commit bears
    the name of whoever decided (`by`)."""
    repo = ctx.local_repository(wiki.infra)
    repo.commit_changes({"content/README.md": b"# Local\n"}, ALICE, "local")
    wiki.remote.push({"content/README.md": "# Distant\n"})
    git_sync.run_sync(database, ctx, wiki.infra.id)
    db.expire_all()
    space = db.get(Space, wiki.infra.id)
    assert space is not None and space.sync_status == "conflict"
    git_sync.request_resolution(db, wiki.root, space, {"content/README.md": "local"})
    db.commit()

    worker = Worker(settings, database, ctx)
    assert worker.run_once(kinds={git_sync.RESOLVE})
    assert statuses(database, git_sync.RESOLVE) == ["done"]
    assert wiki.remote.read("content/README.md") == "# Local\n"
    author = git(ctx.clone_path(wiki.infra.id), "log", "-1", "--format=%an")
    assert author == wiki.root.user.display_name
    db.expire_all()
    space = db.get(Space, wiki.infra.id)
    assert space is not None and space.sync_status == "ok"
    assert statuses(database, INDEX) == ["queued"]  # reindexing after the merge


@dataclass
class Receiver:
    """Local webhook receiver: keeps the received headers and body."""

    url: str = ""
    received: list[tuple[dict[str, str], bytes]] = field(default_factory=list)


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    found = Receiver()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 — name imposed by http.server
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            found.received.append(({k.lower(): v for k, v in self.headers.items()}, body))
            self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    found.url = f"http://127.0.0.1:{server.server_address[1]}/hook"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield found
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_webhook_emitted_then_delivered_by_the_worker(
    settings: Settings,
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    receiver: Receiver,
) -> None:
    """A submission emits `inbox.item_added`; the worker finds the webhook (`webhook_id`) and
    delivers the signed body (`body`), as it was queued."""
    _, secret = webhooks.create(
        db,
        wiki.root,
        ctx.box,
        wiki.infra.organization,
        receiver.url,
        ["inbox.item_added"],
        allow_private=True,
    )
    db.commit()
    deposits.deposit(
        db, database, wiki.alice, ctx, wiki.infra, title="Note", body="x", source="web"
    )
    db.commit()

    worker = Worker(settings.model_copy(update={"allow_private_webhooks": True}), database, ctx)
    assert worker.run_once(kinds={webhooks.WEBHOOK})
    assert statuses(database, webhooks.WEBHOOK) == ["done"]
    [(headers, body)] = receiver.received
    payload = json.loads(body)
    assert (payload["event"], payload["space"], payload["data"]["title"]) == (
        "inbox.item_added",
        "acme/infra",
        "Note",
    )
    assert headers["x-synaptane-signature"] == webhooks.sign(secret, body)
    with database.session() as session:
        assert session.scalars(select(Webhook.last_status)).one() == "204"
