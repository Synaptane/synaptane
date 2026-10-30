"""Verdicts on the problems a routine reports: a false alarm judged by a person, or a problem
handled by the tidy-up, is not reported again while its pages stay the same."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.services import content, deposits, git_sync, routine_agent, routines, tidy
from synaptane.services import verdicts as verdicts_service
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Routine
from tests.integration.test_routines import RoutineScript
from tests.integration.test_tidy import Scripted, configure_ai
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

TITLE = "Heure de sauvegarde contradictoire"
PAGES = ["serveurs/proxmox.md", "serveurs/sauvegarde.md"]


def _routine(db: Session, database: Database, ctx: Workspace, wiki: Wiki) -> Routine:
    configure_ai(db, ctx, wiki)
    wiki.remote.push({"content/serveurs/sauvegarde.md": "# Sauvegarde\n\nElle tourne à 3h.\n"})
    git_sync.run_sync(database, ctx, wiki.infra.id)
    routine = routines.create(db, wiki.root, wiki.infra, routines.Fields(), template="coherence")
    db.commit()
    return routine


def _run(database: Database, ctx: Workspace, routine: Routine, title: str = TITLE) -> RoutineScript:
    script = RoutineScript(
        [
            [("write_report", {"content": "# Cohérence\n\nUne contradiction."})],
            [("add_to_inbox", {"title": title, "body": "2h contre 3h.", "pages": PAGES})],
            [("finish", {"summary": "Fini."})],
        ]
    )
    record = routine_agent.run_one(
        database, ctx, routine.id, trigger="manual", factory=script.factory
    )
    assert record is not None and record.status == "done", record and record.error
    return script


def test_a_false_alarm_is_not_reported_again_until_its_pages_change(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    routine = _routine(db, database, ctx, wiki)
    _run(database, ctx, routine)
    [item] = deposits.list_items(wiki.alice, ctx, wiki.infra)
    deposits.delete_item(
        db, database, wiki.alice, ctx, wiki.infra, item.name, not_a_problem="2h en hiver, 3h l'été."
    )
    db.commit()
    [verdict] = verdicts_service.standing(db, ctx, wiki.infra, ['Routine "Contrôle de cohérence"'])
    assert (verdict.kind, verdict.by, verdict.pages) == ("dismissed", "Alice", tuple(PAGES))

    # Next run: the verdict is given, the same problem (whatever the case) refused.
    script = _run(database, ctx, routine, TITLE.upper())
    assert "already judged" in script.first and "2h en hiver, 3h l'été." in script.first
    assert any(r.is_error and "Already judged" in r.content for r in script.results)
    assert deposits.list_items(wiki.alice, ctx, wiki.infra) == []

    # One of its pages changed: the problem may be real now, it can be reported again.
    page = content.read_file(wiki.alice, ctx, wiki.infra, PAGES[1])
    text = (page.text or "").replace("3h", "4h").encode()
    content.write_file(db, database, wiki.alice, ctx, wiki.infra, PAGES[1], text, base_sha=page.sha)
    db.commit()
    assert (
        verdicts_service.standing(db, ctx, wiki.infra, [routine_agent.Spec.of(routine).origin])
        == []
    )
    script = _run(database, ctx, routine)
    assert not any(r.is_error for r in script.results)
    assert [i.title for i in deposits.list_items(wiki.alice, ctx, wiki.infra)] == [TITLE]


def test_a_problem_handled_by_the_tidy_up_is_remembered(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    routine = _routine(db, database, ctx, wiki)
    _run(database, ctx, routine)
    [item] = deposits.list_items(wiki.alice, ctx, wiki.infra)
    script = Scripted(
        [
            [("read_inbox_item", {"item": item.name})],
            [("mark_done", {"item": item.name, "summary": "Déjà juste : 3h depuis mars."})],
            [("finish", {"summary": "1 rangé"})],
        ]
    )
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=script.factory)
    assert record is not None and record.commit, record
    [verdict] = verdicts_service.standing(db, ctx, wiki.infra, ['Routine "Contrôle de cohérence"'])
    assert (verdict.kind, verdict.reason) == ("filed", "Déjà juste : 3h depuis mars.")
    # A note deposited by a person leaves no verdict.
    deposits.deposit(
        db, database, wiki.alice, ctx, wiki.infra, title="Note", body="x", source="web"
    )
    db.commit()
    [note] = deposits.list_items(wiki.alice, ctx, wiki.infra)
    deposits.delete_item(db, database, wiki.alice, ctx, wiki.infra, note.name)
    db.commit()
    assert (
        len(verdicts_service.standing(db, ctx, wiki.infra, ['Routine "Contrôle de cohérence"']))
        == 1
    )


def test_not_a_problem_in_the_web(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    routine = _routine(db, database, ctx, wiki)
    _run(database, ctx, routine)
    [item] = deposits.list_items(wiki.alice, ctx, wiki.infra)
    login(client, "alice")
    url = f"/o/acme/infra/inbox/{item.name}"
    assert 'data-test="not-a-problem"' in client.get(url).text
    token = csrf(client, url)
    empty = client.post(f"/o/acme/infra/-/inbox/{item.name}/not-a-problem", data={"csrf": token})
    assert "Dites pourquoi" in empty.text
    done = client.post(
        f"/o/acme/infra/-/inbox/{item.name}/not-a-problem",
        data={"csrf": token, "reason": "Deux horaires selon la saison."},
    )
    assert "pas un problème" in done.text
    assert (
        len(verdicts_service.standing(db, ctx, wiki.infra, ['Routine "Contrôle de cohérence"']))
        == 1
    )
