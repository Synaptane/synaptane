"""Routines in the web UI and the REST API: permissions (admins manage, contributors see,
readers nothing), adding from a template, editing, running, deleting, log."""

import html
import re

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.services import routine_agent, routines
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Job, Routine, User
from tests.integration.conftest import token_for
from tests.integration.test_routines import RoutineScript
from tests.integration.test_tidy import configure_ai
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

BASE = "/o/acme/infra/-/routines"
API = "/api/v1/organizations/acme/spaces/infra/routines"


def only(db: Session) -> Routine:
    db.expire_all()
    [routine] = db.scalars(select(Routine))
    return routine


def test_admin_manages_routines_from_the_space_settings(
    client: TestClient, db: Session, database: Database, ctx: Workspace, wiki: Wiki
) -> None:
    configure_ai(db, ctx, wiki)
    login(client, "root")
    page = client.get(BASE)
    assert page.status_code == 200
    assert "Contrôle de cohérence" in page.text and "Résumé hebdomadaire" in page.text
    assert "Aucune routine pour l'instant." in html.unescape(page.text)
    assert f'href="{BASE}"' in client.get("/o/acme/infra/-/members").text  # menu

    token = csrf(client, BASE)
    created = client.post(f"{BASE}/from/coherence", data={"csrf": token}, follow_redirects=False)
    assert created.status_code == 303
    routine = only(db)
    assert created.headers["location"] == f"{BASE}/{routine.id}"
    detail = client.get(f"{BASE}/{routine.id}").text
    assert "Chaque lundi à 06:00" in detail and "rapports/coherence.md" in detail
    assert "Aucun passage pour l'instant." in html.unescape(detail)
    listing = client.get(BASE).text
    assert "Déjà ajoutée" in listing and "Jamais lancée" in listing

    # Form refused: the error is shown, nothing that was typed is lost.
    form = client.get(f"{BASE}/{routine.id}/edit").text
    assert "Modifier la routine" in form and "contradictions entre pages" in form
    data = {
        "csrf": token,
        "name": "Cohérence infra",
        "instructions": "Mes consignes très précises.",
        "frequency": "monthly",
        "monthday": "3",
        "weekday": "0",
        "at": "07:30",
        "scope": "serveurs\n",
        "output_page": "",
        "output_email": "",
    }
    refused = client.post(f"{BASE}/{routine.id}", data=data)
    assert refused.status_code == 400
    assert "au moins une sortie" in refused.text and "Mes consignes très précises." in refused.text
    saved = client.post(
        f"{BASE}/{routine.id}",
        data={**data, "output_page": "rapports/infra", "enabled": "true"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    routine = only(db)
    assert (routine.name, routine.frequency, routine.monthday, routine.at) == (
        "Cohérence infra",
        "monthly",
        3,
        "07:30",
    )
    assert routine.scope == ["serveurs"] and routine.output_page == "rapports/infra.md"
    assert not routine.output_inbox and routine.weekday is None

    # Disable, re-enable, run: the request waits for the worker, the page waits too.
    client.post(f"{BASE}/{routine.id}/toggle", data={"csrf": token})
    assert only(db).enabled is False
    client.post(f"{BASE}/{routine.id}/toggle", data={"csrf": token})
    assert only(db).enabled is True
    client.post(f"{BASE}/{routine.id}/run", data={"csrf": token})
    assert only(db).requested == "manual"
    assert db.scalar(select(Job).where(Job.kind == routines.ROUTINE, Job.status == "queued"))
    detail = client.get(f"{BASE}/{routine.id}").text
    assert "En attente du worker" in detail and "data-tidy-state" in detail
    assert client.get(f"{BASE}/{routine.id}/state").json()["poll"] is True

    # The run (simulated model) appears in the log, with its output and its cost.
    script = RoutineScript(
        [[("write_report", {"content": "# Infra\n\nRAS."}), ("finish", {"summary": "RAS"})]]
    )
    assert routine_agent.run_pending(database, ctx, wiki.infra.id, factory=script.factory) == 1
    detail = client.get(f"{BASE}/{routine.id}").text
    assert "Terminé" in detail and "Page réécrite" in detail and "rapports/infra.md" in detail
    assert "Texte de sortie" in detail and "≈" in detail
    assert client.get(f"{BASE}/{routine.id}/state").json()["poll"] is False

    # A translated failure, with the cause and the technical details for the admin.
    routines.request_run(db, wiki.root, wiki.infra, only(db))
    db.commit()
    from synaptane.ai.base import AIError
    from tests.integration.test_routines import Failing

    failing = Failing(AIError("Connexion impossible : [Errno 111] Connection refused"))
    routine_agent.run_pending(database, ctx, wiki.infra.id, factory=failing.factory)
    detail = client.get(f"{BASE}/{routine.id}").text
    assert "Échec · fournisseur injoignable" in detail and "Rien n'a été publié." in html.unescape(
        detail
    )
    assert "Errno 111" in detail

    deleted = client.post(f"{BASE}/{routine.id}/delete", data={"csrf": token})
    assert deleted.status_code == 200 and "Routine supprimée." in deleted.text
    db.expire_all()
    assert db.scalars(select(Routine)).all() == []


def test_a_template_is_offered_in_the_language_of_the_user(
    client: TestClient, db: Session, wiki: Wiki
) -> None:
    login(client, "root")
    french = html.unescape(client.get(f"{BASE}/new?template=digest").text)
    assert "Rédige le résumé de la semaine" in french and "rapports/semaine.md" in french
    wiki.root.user.locale = "en"
    db.commit()
    english = client.get(f"{BASE}/new?template=digest").text
    assert "Write the summary of the week" in english and "reports/weekly-digest.md" in english


def test_blank_routine_from_the_form(client: TestClient, db: Session, wiki: Wiki) -> None:
    login(client, "root")
    form = client.get(f"{BASE}/new").text
    assert "Nouvelle routine" in form and 'name="template" value=""' in form
    model = client.get(f"{BASE}/new?template=digest").text
    assert "Nouvelle routine depuis un modèle" in model and 'value="digest"' in model
    created = client.post(
        BASE,
        data={
            "csrf": csrf(client, BASE),
            "name": "FAQ d'accueil",
            "instructions": "Régénère la FAQ.",
            "frequency": "weekly",
            "weekday": "2",
            "at": "09:00",
            "output_page": "accueil/faq.md",
            "output_email": "admins",
            "output_webhook": "true",
            "enabled": "true",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    routine = only(db)
    assert (routine.template, routine.weekday, routine.output_email) == ("", 2, "admins")
    assert routine.output_webhook and routine.enabled
    assert "Chaque mercredi à 09:00" in client.get(BASE).text


def test_contributors_see_readers_do_not_and_only_admins_change(
    client: TestClient, db: Session, wiki: Wiki
) -> None:
    routine = routines.create(db, wiki.root, wiki.infra, routines.Fields(), template="digest")
    db.commit()
    login(client, "alice")  # contributor
    page = client.get(BASE)
    assert page.status_code == 200 and "Résumé hebdomadaire" in page.text
    assert "Ajouter une routine" not in page.text and "Lancer maintenant" not in page.text
    assert "Seuls les administrateurs" in page.text
    assert client.get(f"{BASE}/{routine.id}").status_code == 200
    assert f'href="{BASE}"' in client.get("/o/acme/infra/-/members").text
    token = csrf(client, BASE)
    for path in (
        "/from/coherence",
        f"/{routine.id}/run",
        f"/{routine.id}/toggle",
        f"/{routine.id}/delete",
    ):
        refused = client.post(BASE + path, data={"csrf": token})
        assert "Action non autorisée." in refused.text, path  # message, nothing changed
    edit = {
        "csrf": token,
        "name": "Pirate",
        "instructions": "x",
        "frequency": "daily",
        "at": "01:00",
    }
    assert client.post(f"{BASE}/{routine.id}", data=edit).status_code == 403
    assert client.post(BASE, data={**edit, "output_inbox": "true"}).status_code == 403
    db.expire_all()
    [same] = db.scalars(select(Routine))
    assert (same.name, same.enabled, same.requested) == ("Résumé hebdomadaire", True, None)
    assert client.get(f"{BASE}/new").status_code == 403
    assert client.get(f"{BASE}/{routine.id}/edit").status_code == 403

    client.post("/logout", data={"csrf": token})
    login(client, "bob")  # reader
    assert client.get(BASE).status_code == 403
    assert client.get(f"{BASE}/{routine.id}").status_code == 403
    assert f'href="{BASE}"' not in client.get("/o/acme/infra/-/members").text


def test_rest_api(client: TestClient, db: Session, database: Database, wiki: Wiki) -> None:
    root = {"Authorization": f"Bearer {token_for(database, 'root')}"}
    alice = {"Authorization": f"Bearer {token_for(database, 'alice')}"}
    bob = {"Authorization": f"Bearer {token_for(database, 'bob')}"}
    created = client.post(API, json={"template": "coherence", "at": "05:45"}, headers=root)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "Contrôle de cohérence" and body["at"] == "05:45"
    assert body["output_inbox"] is True and body["next_run"] and body["last_run"] is None
    rid = body["id"]
    assert [r["id"] for r in client.get(API, headers=alice).json()] == [rid]
    assert client.get(API, headers=bob).status_code == 403
    assert client.post(API, json={"template": "digest"}, headers=alice).status_code == 403
    patched = client.patch(
        f"{API}/{rid}", json={"enabled": False, "scope": ["serveurs/"]}, headers=root
    )
    assert patched.status_code == 200 and patched.json()["enabled"] is False
    assert patched.json()["scope"] == ["serveurs"] and patched.json()["next_run"] is None
    bad = client.patch(f"{API}/{rid}", json={"frequency": "hourly"}, headers=root)
    assert bad.status_code == 422
    assert client.post(f"{API}/{rid}/run", headers=alice).status_code == 403
    assert client.post(f"{API}/{rid}/run", headers=root).status_code == 202
    assert client.get(f"{API}/{rid}", headers=alice).json()["id"] == rid
    assert client.get(f"{API}/{rid}/runs", headers=alice).json() == []
    assert client.delete(f"{API}/{rid}", headers=alice).status_code == 403
    assert client.delete(f"{API}/{rid}", headers=root).status_code == 204
    assert client.get(f"{API}/{rid}", headers=root).status_code == 404
    blank = client.post(API, json={"name": "Vide"}, headers=root)
    assert blank.status_code == 400


def test_routine_mails_can_be_turned_off_in_my_account(
    client: TestClient, db: Session, wiki: Wiki
) -> None:
    login(client, "alice")
    page = client.get("/account").text
    assert re.search(r'name="notify_routines" value="true" checked', page)
    client.post(
        "/account/notifications", data={"csrf": csrf(client, "/account"), "notify_tidy": "true"}
    )
    db.expire_all()
    user = db.get(User, wiki.alice.user.id)
    assert user is not None and user.notify_tidy and not user.notify_routines
