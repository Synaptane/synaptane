"""Quality metrics of a pass: computed without AI before the commit, recorded in the journal,
exposed over REST and shown in the "Quality" block."""

from decimal import Decimal
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.ai.base import Usage
from synaptane.services import tidy, tidy_metrics
from synaptane.services.tidy_tools import Overlay, RunState, Tools
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from tests.integration.conftest import token_for
from tests.integration.test_tidy import Scripted, configure_ai, deposit_note
from tests.integration.test_web import login
from tests.integration.wikidata import Wiki


def test_pass_records_quality_metrics_shown_in_journal_and_rest(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    note = deposit_note(db, database, ctx, wiki)
    script = Scripted(
        [
            [("read_inbox_item", {"item": note})],
            [
                # Link to a missing page: the move, on the other hand, rewrites links.
                (
                    "write_page",
                    {"path": "stockage/nas.md", "content": "# NAS\n\n8 To. [Ceph](ceph.md)\n"},
                ),
                ("move", {"source": "serveurs/proxmox.md", "target": "infra/proxmox.md"}),
                ("mark_done", {"item": note, "summary": "NAS noté"}),  # no page given
            ],
            [("finish", {"summary": "ok"})],
        ]
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.status == "done" and record.metrics is not None
    metrics = record.metrics
    # README.md is rewritten by the move: its link follows the page.
    assert metrics["pages"] == {"created": 1, "modified": 1, "moved": 1, "deleted": 0, "touched": 3}
    assert metrics["broken_links"] == {
        "before": 0,
        "after": 1,
        "introduced": 1,
        "examples": [{"page": "stockage/nas.md", "link": "ceph.md"}],
    }
    assert metrics["orphans"] == ["stockage/nas.md"] and metrics["orphans_count"] == 1
    assert metrics["not_in_index"] == ["stockage/nas.md"]
    assert metrics["closed_without_page"] == ["Nouveau NAS"]
    assert metrics["untitled"] == [] and metrics["empty"] == []
    assert (
        cast(int, metrics["removed_chars"]) < 100
    )  # only the line of the rewritten link in README.md
    assert metrics["items"] == {"done": 1, "left": 0, "left_rate": 0.0}
    assert metrics["issues"] == 4
    usage = cast(dict[str, Any], metrics["usage"])
    assert usage["tokens"] == record.input_tokens + record.output_tokens + record.cached_tokens
    # The final check sends the issues back to the agent once: one more turn.
    assert usage["turns"] == 3
    assert usage["per_item"] == {"tokens": usage["tokens"], "turns": 3.0, "cost": str(record.cost)}

    runs = client.get(
        "/api/v1/organizations/acme/spaces/infra/tidy/runs",
        headers={"Authorization": f"Bearer {token_for(database, 'alice')}"},
    ).json()
    assert runs[0]["metrics"]["broken_links"]["introduced"] == 1

    login(client, "alice")
    journal = client.get("/o/acme/infra/tidy").text
    assert "Qualité\xa0: 4 points à vérifier" in journal
    assert "stockage/nas.md → <code>ceph.md</code>" in journal
    assert "Éléments rangés sans page indiquée\xa0: 1" in journal


def test_a_failing_measure_never_costs_the_pass(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_ai(db, ctx, wiki)
    note = deposit_note(db, database, ctx, wiki)

    def broken(overlay: Overlay, state: RunState) -> dict[str, object]:
        raise RuntimeError("mesure impossible")

    monkeypatch.setattr(tidy_metrics, "measure", broken)
    script = Scripted(
        [
            [("read_inbox_item", {"item": note})],
            [
                ("write_page", {"path": "nas.md", "content": "# NAS\n"}),
                ("mark_done", {"item": note, "summary": "ok", "pages": ["nas.md"]}),
            ],
        ]
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.status == "done" and record.commit
    assert record.metrics is None


def test_measure_removed_text_moves_and_quiet_pages(ctx: Workspace, wiki: Wiki) -> None:
    repo = ctx.local_repository(wiki.infra)
    base = repo.head()
    assert base is not None
    overlay = Overlay(repo, base)
    state = RunState(items={})
    tools = Tools(overlay, state, ctx, wiki.infra, lambda query: [])
    # A line removed from a kept page, a page created then moved (that is a creation),
    # an empty untitled page, a well-filed page.
    tools.t_edit_page("serveurs/proxmox.md", "La sauvegarde nocturne tourne à 2h.\n\n", "")
    tools.t_write_page("brouillon.md", "---\ntags: [x]\n---\n## Sous-titre\n")
    tools.t_move("brouillon.md", "archives/brouillon.md")
    tools.t_write_page("serveurs/README.md", "# Serveurs\n\n- [Proxmox](proxmox.md)\n")
    metrics = tidy_metrics.measure(overlay, state)
    assert metrics["pages"] == {"created": 2, "modified": 1, "moved": 0, "deleted": 0, "touched": 3}
    assert metrics["removed_chars"] == len("La sauvegarde nocturne tourne à 2h.\n\n")
    assert metrics["untitled"] == ["archives/brouillon.md"]
    assert metrics["empty"] == ["archives/brouillon.md"]
    assert metrics["orphans"] == ["archives/brouillon.md", "serveurs/README.md"]
    # A README is expected in the parent folder's index; at the root, the one in content/.
    assert metrics["not_in_index"] == ["archives/brouillon.md", "serveurs/README.md"]
    assert metrics["broken_links"]["introduced"] == 0


def test_usage_per_item_and_no_division_by_zero() -> None:
    usage = Usage(input_tokens=900, output_tokens=100, cached_tokens=1000, turns=5)
    usage.cache_write_tokens = 300
    base = {"items": {"done": 2, "left": 1, "left_rate": 0.333}}
    full = tidy_metrics.with_usage(base, usage, Decimal("0.0100"))
    assert full["usage"] == {
        "tokens": 2000,
        "cache_write_tokens": 300,
        "turns": 5,
        "per_item": {"tokens": 1000, "turns": 2.5, "cost": "0.0050"},
    }
    nothing = tidy_metrics.with_usage({"items": {"done": 0}}, usage, None)
    assert nothing["usage"]["per_item"] is None
    assert tidy_metrics.issues(None) == 0
