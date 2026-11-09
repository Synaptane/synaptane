"""Routines described by files of the repository: a push creates, changes or removes a routine
without touching the state of its runs, an invalid file never runs and says why, the routines
kept in the database are written to the repository once (already there, name taken, push
failed, space not synced yet), and the web form refuses a file changed since it was opened."""

import html
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.core.errors import SynaptaneError
from synaptane.git.repo import GitError
from synaptane.services import (
    budget,
    content,
    git_sync,
    routine_files,
    routines,
    spaces,
    team_prompts,
)
from synaptane.services.routine_files import Config, render
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Routine, RoutineRun, Space
from tests.integration.conftest import token_for
from tests.integration.gitutil import COMMITTER, Remote, git
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

BASE = "/o/acme/infra/-/routines"
API = "/api/v1/organizations/acme/spaces/infra/routines"
PATH = ".synaptane/routines/serveurs.md"
FILE = """---
title: Revue des serveurs
schedule: daily 06:00
scope: serveurs
report: rapports/serveurs.md
---
Relis le dossier serveurs.
"""
INVALID = "---\nschedule: tous les jours\nreport: r.md\n---\nRelis.\n"


def plain(text: str) -> str:
    """Without the no-break spaces of French typography."""
    return text.replace("\u00a0", " ").replace("\u202f", " ")


def rows(db: Session) -> list[Routine]:
    db.expire_all()
    return list(db.scalars(select(Routine).order_by(Routine.name)))


def sync(database: Database, ctx: Workspace, space_id: object) -> None:
    git_sync.run_sync(database, ctx, space_id)  # type: ignore[arg-type]


def legacy(db: Session, wiki: Wiki, name: str, title: str, **values: object) -> Routine:
    """A routine as the migration leaves it: in the database only (`sha` NULL)."""
    row = Routine(
        space_id=wiki.infra.id,
        name=name,
        title=title,
        description="",
        template="",
        instructions="Relis les pages.",
        frequency="weekly",
        weekday=0,
        at="06:00",
        scope=[],
        output_page="rapports/revue.md",
        output_inbox=False,
        output_email="",
        output_webhook=False,
        enabled=True,
        warnings=[],
        **values,
    )
    db.add(row)
    db.commit()
    return row


def test_a_pushed_file_is_a_routine_whose_changes_keep_the_state_of_its_runs(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    wiki.remote.push({PATH: FILE})
    sync(database, ctx, wiki.infra.id)
    [routine] = rows(db)
    assert (routine.name, routine.title, routine.frequency, routine.at) == (
        "serveurs",
        "Revue des serveurs",
        "daily",
        "06:00",
    )
    assert routine.scope == ["serveurs"] and routine.output_page == "rapports/serveurs.md"
    assert routine.sha and routine.error is None and routine.created_by is None
    ran = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)
    routine.last_date, routine.last_run_at, routine.requested = date(2026, 10, 5), ran, "manual"
    db.add(
        RoutineRun(
            routine_id=routine.id,
            space_id=wiki.infra.id,
            name=routine.title,
            trigger="manual",
            status="done",
            cost=Decimal("0.50"),
            outputs={},
        )
    )
    db.commit()

    wiki.remote.push({PATH: FILE.replace("Relis le dossier serveurs.", "Relis tout.")})
    sync(database, ctx, wiki.infra.id)
    [same] = rows(db)
    assert same.id == routine.id and same.instructions == "Relis tout."
    assert (same.last_date, same.last_run_at, same.requested) == (date(2026, 10, 5), ran, "manual")

    # Removed: the routine goes, its runs stay in the log and in the month's budget.
    wiki.remote.push({PATH: None})
    sync(database, ctx, wiki.infra.id)
    assert rows(db) == []
    [run] = db.scalars(select(RoutineRun))
    assert run.routine_id is None and run.name == "Revue des serveurs"
    assert budget.spent(db, wiki.infra.org_id) == Decimal("0.50")


def test_a_new_or_rescheduled_file_waits_for_its_next_time(
    db: Session, ctx: Workspace, wiki: Wiki
) -> None:
    space = db.get(Space, wiki.infra.id)
    assert space is not None
    space.tidy_timezone = "Europe/Paris"
    repo = ctx.local_repository(space)
    noon = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)  # Monday, 12:00 in Paris

    def commit(text: str) -> Routine:
        repo.commit_changes({PATH: text.encode()}, COMMITTER, "Routine")
        routine_files.reconcile(db, space, repo, now=noon)
        db.commit()
        [routine] = rows(db)
        return routine

    created = commit(FILE)  # after its time of the day: not before tomorrow
    assert created.last_date == date(2026, 10, 5)
    assert routines.due(created, space, noon) is None
    assert routines.due(created, space, noon + timedelta(days=1)) == date(2026, 10, 6)
    created.last_date = date(2026, 10, 4)
    db.commit()
    # New instructions: the state is untouched (the routine may still run today).
    changed = commit(FILE.replace("Relis le dossier serveurs.", "Relis tout."))
    assert changed.last_date == date(2026, 10, 4)
    # New time, already passed: no run right away because the file changed.
    moved = commit(FILE.replace("daily 06:00", "daily 07:00"))
    assert moved.last_date == date(2026, 10, 5) and routines.due(moved, space, noon) is None


def test_an_invalid_file_never_runs_and_says_why(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    newer = FILE.replace("report:", "writes:\n  - brouillons\nreport:")
    wiki.remote.push(
        {
            PATH: FILE,
            ".synaptane/routines/casse.md": INVALID,
            ".synaptane/routines/futur.md": newer,
            ".synaptane/routines/Majuscule.md": FILE,
            ".synaptane/routines/notes.txt": "pas une routine",
        }
    )
    sync(database, ctx, wiki.infra.id)
    found = {r.name: r for r in rows(db)}
    upper, broken, newer_row, valid = (
        found[n] for n in ("Majuscule", "casse", "futur", "serveurs")
    )
    assert broken.name == "casse" and broken.error and "is not understood" in broken.error
    assert upper.name == "Majuscule" and upper.error and "Invalid file name" in upper.error
    assert newer_row.error is None and newer_row.warnings and valid.error is None
    later = datetime(2027, 1, 4, 10, 0, tzinfo=UTC)
    assert routines.schedule(database, later) == 2  # the two valid ones only
    db.expire_all()
    assert broken.requested is None and newer_row.requested == "schedule"

    root = {"Authorization": f"Bearer {token_for(database, 'root')}", "Accept-Language": "fr"}
    listed = {r["name"]: r for r in client.get(API, headers=root).json()}
    assert "« tous les jours » n'est pas compris" in plain(listed["casse"]["error"])
    assert [plain(w) for w in listed["futur"]["warnings"]] == ["Champ inconnu, ignoré : writes."]
    assert listed["serveurs"]["error"] is None and listed["serveurs"]["pending"] is False
    assert client.post(f"{API}/{broken.id}/run", headers=root).status_code == 400
    assert client.patch(
        f"{API}/{broken.id}", json={"enabled": False}, headers=root
    ).status_code == (400)
    login(client, "root")
    page = plain(html.unescape(client.get(BASE).text))
    assert "Fichier invalide" in page and "« tous les jours » n'est pas compris" in page
    detail = plain(html.unescape(client.get(f"{BASE}/{newer_row.id}").text))
    assert "Champ inconnu, ignoré : writes." in detail

    # Changed from Synaptane, the newer version's field stays in the file.
    routines.update(
        db, database, wiki.root, ctx, wiki.infra, newer_row, routines.Fields(enabled=False)
    )
    text = (ctx.local_repository(wiki.infra).blob(".synaptane/routines/futur.md") or b"").decode()
    assert "enabled: false" in text and "writes:\n  - brouillons\n---" in text


def test_routines_of_the_database_are_written_to_the_repository_once(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    ran = datetime.now(UTC) - timedelta(days=1)
    kept = legacy(db, wiki, "coherence", "Cohérence", last_date=date.today(), last_run_at=ran)
    there = legacy(db, wiki, "deja-la", "Déjà là")
    taken = legacy(db, wiki, "pris", "Pris")
    odd = legacy(db, wiki, "Résumé du lundi", "Résumé du lundi")
    wiki.remote.push(
        {
            ".synaptane/routines/deja-la.md": render(Config.of(there)),  # an earlier attempt
            ".synaptane/routines/pris.md": FILE,  # someone else's routine, same name
        }
    )
    before = wiki.remote.files(".synaptane/routines")
    sync(database, ctx, wiki.infra.id)

    assert wiki.remote.files(".synaptane/routines") == [
        ".synaptane/routines/coherence.md",
        ".synaptane/routines/deja-la.md",
        ".synaptane/routines/pris-2.md",
        ".synaptane/routines/pris.md",
        ".synaptane/routines/resume-du-lundi.md",
    ]
    assert before == [".synaptane/routines/deja-la.md", ".synaptane/routines/pris.md"]
    assert wiki.remote.last_commit().endswith("/ Synaptane: routines written to the repository")
    assert "Relis les pages." in (wiki.remote.read(".synaptane/routines/coherence.md") or "")
    found = {r.name: r for r in rows(db)}
    assert set(found) == {"coherence", "deja-la", "pris", "pris-2", "resume-du-lundi"}
    assert all(r.sha and r.error is None for r in found.values())
    # Same rows: same state, same runs; nothing runs twice.
    assert found["coherence"].id == kept.id and found["coherence"].last_run_at == ran
    assert found["coherence"].last_date == date.today()
    assert found["deja-la"].id == there.id and found["pris-2"].id == taken.id
    assert found["pris"].title == "Revue des serveurs" and found["pris"].id != taken.id
    assert found["resume-du-lundi"].id == odd.id and found["resume-du-lundi"].title == odd.title

    head = wiki.remote.last_commit()
    sync(database, ctx, wiki.infra.id)  # nothing left to write
    assert wiki.remote.last_commit() == head and len(rows(db)) == 5


def test_a_failed_push_loses_no_routine(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    routine = legacy(db, wiki, "revue", "Revue")
    hook = wiki.remote.bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho refused >&2\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(GitError):
        sync(database, ctx, wiki.infra.id)
    [row] = rows(db)  # written to the local copy: active, the next push will carry it
    assert row.id == routine.id and row.sha
    assert ctx.local_repository(wiki.infra).blob(".synaptane/routines/revue.md")
    assert wiki.remote.read(".synaptane/routines/revue.md") is None
    hook.unlink()
    sync(database, ctx, wiki.infra.id)
    assert wiki.remote.read(".synaptane/routines/revue.md")
    [row] = rows(db)
    assert row.id == routine.id


def test_a_routine_created_before_the_first_sync_is_written_by_it(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki, tmp_path: Path
) -> None:
    remote = Remote(tmp_path / "neuf")
    remote.push({"content/README.md": "# Neuf\n"})
    org = wiki.infra.organization
    space = spaces.create_space(db, wiki.root, org, slug="neuf", name="Neuf", repo_url=remote.url)
    db.commit()
    routine = routines.create(
        db, database, wiki.root, ctx, space, routines.Fields(), template="digest"
    )
    assert routine.sha is None and routine.name == "resume-hebdomadaire"
    routine = routines.update(
        db, database, wiki.root, ctx, space, routine, routines.Fields(at="17:00")
    )
    assert routine.sha is None and routine.at == "17:00"
    sync(database, ctx, space.id)
    text = remote.read(".synaptane/routines/resume-hebdomadaire.md") or ""
    assert "schedule: weekly friday 17:00" in text and "template: digest" in text
    db.expire_all()
    found = db.get(Routine, routine.id)
    assert found is not None and found.sha and found.created_by == wiki.root.user.id


def test_the_form_refuses_a_file_changed_since_it_was_opened(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    login(client, "root")
    token = csrf(client, BASE)
    client.post(f"{BASE}/from/digest", data={"csrf": token})
    [routine] = rows(db)
    path = routines.path_of(routine)
    clone = ctx.clone_path(wiki.infra.id)
    author = git(clone, "log", "-1", "--format=%an|%s")
    assert author == f"{wiki.root.user.display_name}|Routine updated: resume-hebdomadaire"
    opened = routine.sha
    sync(database, ctx, wiki.infra.id)  # pushed
    text = wiki.remote.read(path) or ""
    wiki.remote.push({path: text.replace("16:00", "17:30")})  # an edit by push meanwhile
    sync(database, ctx, wiki.infra.id)
    [routine] = rows(db)
    assert routine.at == "17:30" and routine.sha != opened
    data = {
        "csrf": token,
        "base_sha": opened or "",
        "title": "Résumé de l'équipe",
        "instructions": "Mes consignes.",
        "frequency": "weekly",
        "weekday": "4",
        "at": "16:00",
        "output_email": "members",
        "enabled": "true",
    }
    refused = client.post(f"{BASE}/{routine.id}", data=data)
    assert refused.status_code == 409 and "Mes consignes." in refused.text
    assert f'name="overwrite_sha" value="{routine.sha}"' in refused.text
    assert rows(db)[0].at == "17:30"
    saved = client.post(
        f"{BASE}/{routine.id}", data={**data, "overwrite_sha": routine.sha}, follow_redirects=False
    )
    assert saved.status_code == 303
    [routine] = rows(db)
    assert (routine.title, routine.at, routine.instructions) == (
        "Résumé de l'équipe",
        "16:00",
        "Mes consignes.",
    )
    assert "Mes consignes." in (ctx.local_repository(wiki.infra).blob(path) or b"").decode()

    deleted = client.post(f"{BASE}/{routine.id}/delete", data={"csrf": token})
    assert "Routine supprimée." in deleted.text and rows(db) == []
    assert ctx.local_repository(wiki.infra).blob(path) is None


def test_no_other_write_path_reaches_the_routines(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """Pages, notes and team prompts are written elsewhere: a contributor cannot add a
    routine through Synaptane (only through a push, if the forge lets them)."""
    for path in ("../.synaptane/routines/x.md", "/../.synaptane/routines/x.md", "a/../../x.md"):
        with pytest.raises(SynaptaneError):
            content.write_file(db, database, wiki.alice, ctx, wiki.infra, path, FILE.encode())
        db.rollback()
    content.write_file(
        db, database, wiki.alice, ctx, wiki.infra, ".synaptane/routines/x.md", FILE.encode()
    )
    db.commit()
    sync(database, ctx, wiki.infra.id)
    assert rows(db) == []  # content/.synaptane/routines/x.md: a page, not a routine
    for name in ("../routines/x", "a/b"):
        with pytest.raises(SynaptaneError):
            team_prompts.kind(name)
    with pytest.raises(SynaptaneError):
        routines.create(
            db, database, wiki.alice, ctx, wiki.infra, routines.Fields(), template="digest"
        )


def test_an_edit_waiting_for_the_lock_while_the_sync_writes_the_routine_reaches_the_file(
    db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    """The request read the routine before the space lock; the synchronization holding the
    lock wrote it to the repository meanwhile. The edit must go to the file, not to the
    database only (the copy would differ from Git for good), and a removal must remove the
    file (the routine would come back at the next synchronization)."""
    legacy(db, wiki, "revue", "Revue")
    legacy(db, wiki, "veille", "Veille")
    routine, edited = rows(db)  # as the requests read them (`routines.get`)
    assert routine.sha is None and edited.sha is None
    sync(database, ctx, wiki.infra.id)  # in another session: both written, `sha` set
    path = ".synaptane/routines/veille.md"

    routines.update(db, database, wiki.root, ctx, wiki.infra, edited, routines.Fields(at="17:00"))
    text = (ctx.local_repository(wiki.infra).blob(path) or b"").decode()
    assert "schedule: weekly monday 17:00" in text
    found = {r.name: r for r in rows(db)}
    assert found["veille"].at == "17:00"
    assert found["veille"].sha == ctx.local_repository(wiki.infra).blob_sha(path)

    routines.delete(db, database, wiki.root, ctx, wiki.infra, routine)
    assert ctx.local_repository(wiki.infra).blob(".synaptane/routines/revue.md") is None
    sync(database, ctx, wiki.infra.id)
    assert [r.name for r in rows(db)] == ["veille"]

