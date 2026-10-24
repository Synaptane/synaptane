"""Tidying journal on the web: it sums up, says what to do and follows a requested pass."""

from datetime import UTC, datetime, timedelta
from html import unescape

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from synaptane.core.access import OrgRole
from synaptane.services import ai_settings, audit, organizations
from synaptane.services.workspace import Workspace
from synaptane.storage.models import Space, TidyRun
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

REFUSED = "Fournisseur injoignable : [Errno 111] Connection refused"


def ollama(db: Session, ctx: Workspace, wiki: Wiki) -> None:
    ai_settings.configure(
        db,
        wiki.root,
        ctx.box,
        wiki.infra.organization,
        provider="ollama",
        model="qwen3:8b",
        base_url="http://127.0.0.1:9/v1",
    )


def failed_nights(db: Session, wiki: Wiki, nights: int = 2, attempts: int = 5) -> None:
    """Like the demo: every night, a scheduled pass and its retries, all failed."""
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    for night in range(nights):
        for attempt in range(attempts):
            at = start - timedelta(days=night) + timedelta(minutes=attempt * 3)
            db.add(
                TidyRun(
                    space_id=wiki.infra.id,
                    trigger="schedule",
                    status="failed",
                    model="qwen3:8b",
                    started_at=at,
                    finished_at=at,
                    error=REFUSED,
                )
            )
    db.add(
        TidyRun(
            space_id=wiki.infra.id,
            trigger="schedule",
            status="failed",
            started_at=start - timedelta(days=nights),
            error="Aucun fournisseur d'IA configuré pour l'organisation (page de l'organisation).",
        )
    )
    db.commit()


def test_failures_are_summed_up_with_what_to_do(
    client: TestClient, db: Session, ctx: Workspace, wiki: Wiki
) -> None:
    ollama(db, ctx, wiki)
    failed_nights(db, wiki)
    login(client, "root")
    page = unescape(client.get("/o/acme/infra/tidy").text)
    # Banner: since when, how many, the translated cause, and a way to test the connection.
    assert "Le rangement échoue depuis le" in page and "(11 tentatives)" in page
    assert "injoignable à http://127.0.0.1:9/v1" in page
    assert 'action="/o/acme/-/ai/test"' in page and 'href="/o/acme/-/ai"' in page
    assert page.count("Détail technique") == 1  # in the banner only, not on each card
    assert "page de l'organisation" not in page  # screen gone: no more pointer to it
    # Retries of the same night: one card each, expandable; title = outcome.
    assert page.count('class="card tidy-run"') == 3
    assert page.count("Voir les 5 tentatives") == 2
    assert "Échec · fournisseur injoignable" in page and "Échec · aucune IA configurée" in page
    assert "Modèle\xa0: qwen3:8b · 0 jeton" in page
    # "Tidy now" toned down, with the reason; no confirmation (Ollama is local).
    assert "Les 11 derniers passages ont échoué" in page
    assert 'button-primary" type="submit">Ranger maintenant' not in page
    assert 'data-confirm="Lancer' not in page
    # Badge on the tab, visible from the space's other sections.
    assert 'class="tab-alert"' in unescape(client.get("/o/acme/infra/inbox").text)


def test_a_run_before_the_first_sync_points_to_the_repository_not_the_ai(
    client: TestClient, db: Session, ctx: Workspace, wiki: Wiki
) -> None:
    ollama(db, ctx, wiki)
    db.add(
        TidyRun(
            space_id=wiki.infra.id,
            trigger="schedule",
            status="failed",
            started_at=datetime.now(UTC),
            error="This space has not been synchronized with its repository yet.",
        )
    )
    db.commit()
    login(client, "root")
    page = unescape(client.get("/o/acme/infra/tidy").text)
    assert "L'espace n'avait pas encore été synchronisé avec son dépôt" in page
    assert 'href="/o/acme/infra/-/git"' in page
    assert 'action="/o/acme/-/ai/test"' not in page and 'href="/o/acme/-/ai"' not in page


def test_readers_learn_who_to_tell_not_the_technical_detail(
    client: TestClient, db: Session, ctx: Workspace, wiki: Wiki
) -> None:
    ollama(db, ctx, wiki)
    failed_nights(db, wiki, nights=1)
    login(client, "bob")
    page = unescape(client.get("/o/acme/infra/tidy").text)
    assert "Le rangement est en panne depuis le" in page
    assert "Prévenez un administrateur de l'organisation." in page  # no one to name
    organizations.set_member(db, wiki.root, wiki.infra.organization, wiki.root.user, OrgRole.ADMIN)
    db.commit()
    page = unescape(client.get("/o/acme/infra/tidy").text)
    assert "Prévenez un administrateur de l'organisation\xa0: Root." in page
    assert "Cause\xa0: fournisseur injoignable." in page
    assert "127.0.0.1" not in page and "Errno" not in page and "Ranger maintenant" not in page
    found = db.get(Space, wiki.infra.id)
    assert found is not None
    audit(db, None, "alert.tidy_failed", space=found, error=REFUSED)
    db.commit()
    page = unescape(client.get("/o/acme/infra/tidy").text)
    assert "Les administrateurs de l'organisation en sont prévenus." in page


def test_a_requested_run_shows_at_once_and_the_page_follows_it(
    client: TestClient, db: Session, ctx: Workspace, wiki: Wiki
) -> None:
    ai_settings.configure(
        db, wiki.root, ctx.box, wiki.infra.organization, provider="anthropic", api_key="sk-ant-x"
    )
    db.commit()
    login(client, "alice")
    journal = unescape(client.get("/o/acme/infra/tidy").text)
    assert 'data-confirm="Lancer un passage maintenant' in journal  # billed per token
    assert "button-primary" in journal and "Aucun passage pour l'instant." in journal
    token = csrf(client, "/o/acme/infra/tidy")
    after = unescape(client.post("/o/acme/infra/-/tidy", data={"csrf": token}).text)
    assert "Rangement demandé" in after and "En attente du worker" in after
    assert 'data-tidy-state="/o/acme/infra/-/tidy/state"' in after
    assert "Aucun passage pour l'instant." not in after
    state = client.get("/o/acme/infra/-/tidy/state")
    assert state.headers["cache-control"] == "no-store"
    assert state.json()["poll"] is True and state.json()["version"].endswith(":queued")
    before = state.json()["version"]

    # The worker takes the request: the pass is logged "running", then done.
    from synaptane.storage.models import Job

    job = db.query(Job).filter(Job.kind == "tidy").one()
    job.status = "running"
    run = TidyRun(space_id=wiki.infra.id, trigger="manual", status="running", model="m")
    db.add(run)
    db.commit()
    running = client.get("/o/acme/infra/-/tidy/state").json()
    assert running["poll"] is True and running["version"] != before
    assert "En cours…" in unescape(client.get("/o/acme/infra/tidy").text)
    job.status, run.status = "done", "done"
    run.items_done, run.operations = (
        1,
        [
            {"op": "créé", "path": "serveurs/nas.md"},
            {"op": "rangé", "path": "Nouveau NAS", "detail": "Ajouté → serveurs/nas.md"},
            {"op": "réduit", "path": "serveurs/proxmox.md", "detail": "−40 % (900 → 540 octets)"},
        ],
    )
    db.commit()
    done = client.get("/o/acme/infra/-/tidy/state").json()
    assert done["poll"] is False and done["version"] != running["version"]
    page = unescape(client.get("/o/acme/infra/tidy").text)
    assert "1 élément rangé" in page and "data-tidy-state" not in page
    assert "Intégré dans\xa0:" in page
    assert page.count('href="/o/acme/infra/p/serveurs/nas.md"') == 2
    assert 'href="/o/acme/infra/p/serveurs/proxmox.md"' in page
    assert "Page fortement réduite" in page


def test_a_reader_cannot_follow_another_space(client: TestClient, wiki: Wiki) -> None:
    login(client, "bob")
    assert client.get("/o/acme/autre/-/tidy/state").status_code == 404


def test_the_inbox_without_ai_informs_rather_than_alarms(client: TestClient, wiki: Wiki) -> None:
    """No AI provider yet: the inbox works, its notes wait. An information (neutral notice),
    with the way out: configure AI (administrator) or who to ask (others)."""
    login(client, "alice")
    page = unescape(client.get("/o/acme/infra/inbox").text)
    assert '<div class="notice" role="status"><p>Les notes attendent ici' in page
    assert "notice-warning" not in page and "notice-error" not in page
    assert "Demandez-le à un administrateur de l'organisation." in page
    assert "/o/acme/-/ai" not in page
    login(client, "root")
    page = unescape(client.get("/o/acme/infra/inbox").text)
    assert 'href="/o/acme/-/ai">Configurer l\'IA</a>' in page


def test_the_inbox_says_when_the_next_run_is(
    client: TestClient, db: Session, ctx: Workspace, wiki: Wiki
) -> None:
    ollama(db, ctx, wiki)
    db.commit()
    login(client, "alice")
    page = unescape(client.get("/o/acme/infra/inbox").text)
    assert "Journal des rangements" not in page  # duplicate of the "Tidy-up" tab
    assert "Prochain rangement\xa0:" in page
    assert 'class="tab-alert"' not in page
    space = db.get(Space, wiki.infra.id)
    assert space is not None
    space.tidy_enabled = False
    db.commit()
    assert "Prochain rangement" not in unescape(client.get("/o/acme/infra/inbox").text)
    assert "Rangement automatique désactivé" in unescape(client.get("/o/acme/infra/tidy").text)
