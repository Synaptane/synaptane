"""A routine that keeps both a memory (`memory: true`) and folders of its own (`writes`): one
commit for its pages and its memory, nothing published by an interrupted run, the memory out
of reach of the writing tools, and what the nightly maintenance makes of a team `warn` rule
broken in a page the routine keeps (fixed, like a person's) or in its memory and report page
(left out).

The model is simulated (`RoutineScript`): each test sets the tool calls."""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from synaptane.ai.base import AIError
from synaptane.core.errors import SynaptaneError
from synaptane.services import (
    git_sync,
    maintenance,
    routine_agent,
    routine_writes,
    routines,
    spaces,
    tidy,
)
from synaptane.services.routine_files import memory_path
from synaptane.services.tidy_tools import Overlay
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Routine, RoutineRun, Space
from tests.integration.gitutil import Remote, git
from tests.integration.test_routines import RoutineScript, head_files
from tests.integration.test_tidy import configure_ai
from tests.integration.wikidata import Wiki

NAME = "veille"
MEMORY = memory_path(NAME)
ADDRESS = "jean.dupont@gmail.com"


def watch(db: Session, database: Database, ctx: Workspace, wiki: Wiki, **values: Any) -> Routine:
    """A routine `veille` reading `serveurs/`, keeping `veille/`, with a report page and a
    memory, synced."""
    fields: dict[str, Any] = {
        "title": "Veille des serveurs",
        "instructions": "Tiens la veille à jour dans veille/ et note ce qui reste à suivre.",
        "frequency": "weekly",
        "weekday": 0,
        "at": "07:00",
        "scope": ["serveurs"],
        "output_page": "rapports/veille.md",
        "writes": ["veille"],
        "memory": True,
        **values,
    }
    routine = routines.create(
        db, database, wiki.root, ctx, wiki.infra, routines.Fields(**fields), name=NAME
    )
    db.commit()
    git_sync.run_sync(database, ctx, wiki.infra.id)
    return routine


def run(database: Database, ctx: Workspace, routine: Routine, script: RoutineScript) -> RoutineRun:
    record = routine_agent.run_one(
        database, ctx, routine.id, trigger="manual", factory=script.factory
    )
    assert record is not None
    return record


def text(ctx: Workspace, wiki: Wiki, path: str) -> str | None:
    data = ctx.local_repository(wiki.infra).blob(path)
    return data.decode() if data is not None else None


def test_pages_and_memory_go_in_one_commit_and_an_interrupted_run_publishes_neither(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    routine = watch(db, database, ctx, wiki)
    repo = ctx.local_repository(wiki.infra)

    script = RoutineScript(
        [
            [
                ("write_page", {"path": "veille/s46.md", "content": "# Semaine 46\n\nRAS.\n"}),
                # The memory is reached through `remember` only, never by the writing tools.
                ("write_page", {"path": f"../{MEMORY}", "content": "Écrasée."}),
                ("write_page", {"path": MEMORY, "content": "Écrasée."}),
                ("remember", {"content": "- S46 : veille/s46.md écrite, rien à suivre."}),
            ],
            [("write_report", {"content": "Veille à jour."}), ("finish", {"summary": "Fait."})],
        ]
    )
    record = run(database, ctx, routine, script)
    assert record.status == "done", record.error
    assert {"remember", *routine_writes.NAMES} <= set(script.tools)
    assert script.results[0].is_error is False
    assert script.results[1].is_error and script.results[2].is_error
    assert record.outputs["written"] == [{"path": "veille/s46.md", "change": "created"}]
    assert record.outputs["memory"] == "saved"
    title, files = head_files(ctx, wiki)
    assert title.endswith('Routine "Veille des serveurs": report, 1 page(s) written, memory')
    assert sorted(files) == [MEMORY, "content/rapports/veille.md", "content/veille/s46.md"]
    assert record.commit == repo.head()
    message = git(ctx.clone_path(wiki.infra.id), "log", "-1", "--format=%B")
    assert f"Source: {routine_writes.SOURCE}" in message  # read by the maintenance
    assert text(ctx, wiki, MEMORY) == "- S46 : veille/s46.md écrite, rien à suivre.\n"

    # Interrupted after writing a page and its memory: neither is published.
    before = repo.head()

    def fail(index: int) -> None:
        if index == 1:
            raise AIError("Provider overloaded")

    broken = RoutineScript(
        [
            [
                ("write_page", {"path": "veille/s47.md", "content": "# Semaine 47\n"}),
                ("remember", {"content": "- S47 : écrite."}),
            ],
            [],
        ],
        on_step=fail,
    )
    record = run(database, ctx, routine, broken)
    assert record.status == "failed" and repo.head() == before
    assert text(ctx, wiki, "content/veille/s47.md") is None
    assert text(ctx, wiki, MEMORY) == "- S46 : veille/s46.md écrite, rien à suivre.\n"
    assert "written" not in record.outputs and "memory" not in record.outputs


def test_a_warn_rule_broken_in_a_kept_page_is_fixed_at_night_not_in_the_memory(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """A `warn` rule does not stop the routine; personal data it lets through in a page the
    routine keeps counts like a person's change, so the nightly maintenance fixes it. The
    report page and the memory stay out of it, as for any other problem."""
    configure_ai(db, ctx, wiki)
    wiki.remote.push(
        {".synaptane/checks.md": "## No personal email\n- preset: email\n- level: warn\n"}
    )
    git_sync.run_sync(database, ctx, wiki.infra.id)
    tidy.update_settings(db, wiki.root, wiki.infra, maintain=True)
    db.commit()
    routine = watch(db, database, ctx, wiki)
    repo = ctx.local_repository(wiki.infra)
    db.expire_all()
    space = db.get(Space, wiki.infra.id)
    assert space is not None
    space.maintain_watermark = repo.head()
    db.commit()

    script = RoutineScript(
        [
            [
                (
                    "write_page",
                    {"path": "veille/contacts.md", "content": f"# Contacts\n\nJean : {ADDRESS}\n"},
                ),
                ("remember", {"content": f"- Contact : {ADDRESS}"}),
                ("write_report", {"content": f"Écrire à {ADDRESS}."}),
            ],
            [("finish", {"summary": "Fait."})],
        ]
    )
    record = run(database, ctx, routine, script)
    assert record.status == "done", record.error
    assert not any(r.is_error for r in script.results)  # a warning refuses nothing
    assert ADDRESS in (text(ctx, wiki, "content/veille/contacts.md") or "")
    head = repo.head()
    assert head is not None
    db.expire_all()
    space = db.get(Space, wiki.infra.id)
    assert space is not None
    found = maintenance.pending(db, ctx, space, Overlay(repo, head))
    db.commit()
    assert found is not None
    team = [p for p in found.problems if p.kind == maintenance.TEAM_KIND]
    assert [(p.page, p.private) for p in team] == [("veille/contacts.md", True)]
    assert ADDRESS not in team[0].detail  # masked, as for a person's page


def test_a_space_not_synced_yet_refuses_a_memory_as_it_refuses_folders(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, tmp_path: Path
) -> None:
    """Both live in the routine's file only: kept in the database until the first
    synchronization, they would be dropped silently."""
    remote = Remote(tmp_path / "neuf")
    remote.push({"content/README.md": "# Neuf\n"})
    org = wiki.infra.organization
    space = spaces.create_space(db, wiki.root, org, slug="neuf", name="Neuf", repo_url=remote.url)
    db.commit()
    base: dict[str, Any] = {
        "title": "Revue",
        "instructions": "Relis tout.",
        "frequency": "daily",
        "at": "06:00",
        "output_page": "rapports/revue.md",
    }
    extras: list[tuple[dict[str, Any], str]] = [
        ({"memory": True}, "Memory of the routine"),
        ({"writes": ["v"]}, "writes"),
    ]
    for extra, refusal in extras:
        with pytest.raises(SynaptaneError, match=refusal):
            routines.create(
                db, database, wiki.root, ctx, space, routines.Fields(**base, **extra), name="revue"
            )
        db.rollback()
    created = routines.create(
        db, database, wiki.root, ctx, space, routines.Fields(**base, memory=False), name="revue"
    )
    assert created.sha is None
