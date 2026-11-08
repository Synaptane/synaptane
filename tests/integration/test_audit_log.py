"""The audit log: `/o/<org>/-/journal` for an organization's admins, `/admin/journal` for super
admins; filters, pages, CSV export, and the actions logged for it."""

import csv
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.core.access import OrgRole
from synaptane.core.errors import SynaptaneError
from synaptane.services import (
    accounts,
    ai_settings,
    audit,
    audit_log,
    invitations,
    organizations,
    semantic,
    spaces,
    tidy,
)
from synaptane.services.workspace import Workspace
from synaptane.storage.models import AuditEvent, Organization
from tests.integration.conftest import actor_of, make_user
from tests.integration.test_web import csrf, login
from tests.integration.wikidata import Wiki

SECRET = "syn_secret-value-never-shown-42"


def beta(db: Session, wiki: Wiki) -> Organization:
    """A second organization, with its own admin (Dave) and space."""
    org = organizations.create_organization(db, wiki.root, slug="beta", name="Beta")
    dave = make_user(db, "dave")
    organizations.set_member(db, wiki.root, org, dave, OrgRole.ADMIN)
    spaces.create_space(db, wiki.root, org, slug="docs", name="Docs")
    db.commit()
    return org


def acme_admin(db: Session, wiki: Wiki) -> None:
    carol = make_user(db, "carol")
    organizations.set_member(db, wiki.root, wiki.infra.organization, carol, OrgRole.ADMIN)
    db.commit()


def actions(page: str) -> list[str]:
    """Actions of the lines shown, in order."""
    return [part.split('"', 1)[0] for part in page.split('data-action="')[1:]]


def test_only_admins_see_their_organization_log(
    client: TestClient,
    db: Session,
    wiki: Wiki,
) -> None:
    beta(db, wiki)
    acme_admin(db, wiki)
    assert client.get("/o/acme/-/journal", follow_redirects=False).status_code == 303
    login(client, "bob")  # reader of a space of Acme: a plain member
    assert client.get("/o/acme/-/journal").status_code == 404
    assert client.get("/o/acme/-/journal.csv").status_code == 404
    assert 'href="/o/acme/-/journal"' not in client.get("/o/acme").text
    assert client.get("/admin/journal").status_code == 403
    client.cookies.clear()
    login(client, "dave")  # admin of another organization
    assert client.get("/o/acme/-/journal").status_code == 404
    assert client.get("/o/acme/-/journal.csv").status_code == 404
    assert client.get("/o/beta/-/journal").status_code == 200
    client.cookies.clear()
    login(client, "carol")
    page = client.get("/o/acme/-/journal")
    assert page.status_code == 200
    assert 'href="/o/acme/-/journal" aria-current="page"' in page.text
    assert "Journal" in page.text


def test_an_organization_sees_its_events_only(
    client: TestClient,
    db: Session,
    wiki: Wiki,
) -> None:
    beta(db, wiki)
    acme_admin(db, wiki)
    accounts.create_api_token(db, wiki.alice, "laptop")  # no organization
    db.commit()
    login(client, "carol")
    page = client.get("/o/acme/-/journal").text
    shown = actions(page)
    assert "space.member_set" in shown and "org.created" in shown
    assert "token.created" not in shown and "user.created" not in shown
    assert "Beta" not in page and "beta/docs" not in page
    # The lines of a space name it, with a link.
    assert 'href="/o/acme/infra">acme/infra</a>' in page
    assert "Rôle dans l&#39;espace défini" in page and "Lecteur" in page and "@bob" in page
    rows = list(csv.reader(io.StringIO(client.get("/o/acme/-/journal.csv").text)))
    assert all(row[4] in ("", "acme", "acme/infra", "acme/autre") for row in rows[1:])
    assert {row[3] for row in rows[1:]} == set(shown)
    client.cookies.clear()
    login(client, "dave")
    assert "acme" not in client.get("/o/beta/-/journal").text.split('data-test="audit-log"')[1]


def test_the_super_admin_sees_the_whole_instance(
    client: TestClient,
    db: Session,
    wiki: Wiki,
) -> None:
    beta(db, wiki)
    accounts.create_api_token(db, wiki.alice, "laptop")
    db.commit()
    login(client, "root")
    page = client.get("/admin/journal")
    assert page.status_code == 200
    assert 'href="/admin/journal" aria-current="page"' in page.text
    shown = actions(page.text)
    assert {"token.created", "user.created", "org.created", "space.member_set"} <= set(shown)
    assert "Jeton d&#39;API créé" in page.text and "laptop" in page.text
    assert 'href="/o/beta">Beta</a>' in page.text  # organization of a line without space
    alone = actions(client.get("/admin/journal", params={"org": "-"}).text)
    assert "token.created" in alone and "org.created" not in alone and "space.created" not in alone
    of_beta = client.get("/admin/journal", params={"org": "beta"}).text
    assert set(actions(of_beta)) == {"org.created", "org.member_set", "space.created"}
    assert actions(client.get("/admin/journal", params={"org": "nope"}).text) == []


def test_filters_by_family_and_period(client: TestClient, db: Session, wiki: Wiki) -> None:
    old = AuditEvent(
        action="org.updated",
        org_id=wiki.infra.org_id,
        at=datetime(2025, 1, 15, 12, tzinfo=UTC),
    )
    db.add(old)
    audit(db, None, "alert.sync_error", space=wiki.infra, error="repository gone")
    db.commit()
    login(client, "root")

    def shown(**params: str) -> list[str]:
        return actions(client.get("/o/acme/-/journal", params=params).text)

    members = shown(family="members")
    assert members and set(members) == {"space.member_set"}
    assert shown(family="alerts") == ["alert.sync_error"]
    assert shown(since="2025-01-15", until="2025-01-15") == ["org.updated"]
    assert shown(until="2025-01-14") == []
    assert "org.updated" not in shown(since="2025-01-16")
    assert len(shown(family="nope", since="garbage")) == len(shown())
    assert len(shown(since="0001-01-01", until="9999-12-31")) == len(shown())
    page = client.get("/o/acme/-/journal", params={"family": "alerts"}).text
    assert '<option value="alerts" selected>Alertes</option>' in page
    assert "repository gone" in page and "Système" in page
    assert '<option value="security"' not in page  # accounts: not an organization's
    assert (
        "Aucun événement dans cette sélection."
        in client.get("/o/acme/-/journal", params={"until": "2020-01-01"}).text
    )
    export = client.get("/o/acme/-/journal.csv", params={"family": "alerts"})
    assert export.headers["content-type"].startswith("text/csv")
    assert 'filename="synaptane-audit-acme-' in export.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(export.text.removeprefix("﻿"))))
    assert [row[3] for row in rows[1:]] == ["alert.sync_error"]


def test_pages_of_fifty_then_older(client: TestClient, db: Session, wiki: Wiki) -> None:
    start = datetime.now(UTC) - timedelta(days=1)
    same = start - timedelta(hours=1)  # same date for two events: the identifier separates them
    for n in range(audit_log.PAGE_SIZE + 4):
        db.add(
            AuditEvent(
                action="org.updated",
                org_id=wiki.infra.org_id,
                at=same if n < 2 else start - timedelta(hours=n),
            )
        )
    db.commit()
    total = len(
        list(db.scalars(select(AuditEvent.id).where(AuditEvent.org_id == wiki.infra.org_id)))
    )
    login(client, "root")
    first = client.get("/o/acme/-/journal").text
    assert len(actions(first)) == audit_log.PAGE_SIZE
    assert "Plus récent" not in first
    link = first[: first.index('">Plus ancien')].rsplit('href="', 1)[1].replace("&amp;", "&")
    assert link.startswith("/o/acme/-/journal?before=")
    second = client.get(link).text
    assert len(actions(second)) == total - audit_log.PAGE_SIZE
    assert "Plus ancien" not in second and 'href="/o/acme/-/journal">Plus récent' in second
    # A filter follows the pages.
    filtered = client.get("/o/acme/-/journal", params={"family": "settings"}).text
    assert "family=settings" in filtered.split('">Plus ancien')[0].rsplit('href="', 1)[1]
    # A forged cursor gives the first page.
    assert len(actions(client.get("/o/acme/-/journal?before=xyz").text)) == audit_log.PAGE_SIZE


def test_noise_and_secrets_stay_out(client: TestClient, db: Session, wiki: Wiki) -> None:
    audit(db, wiki.alice, "content.written", space=wiki.infra, path="serveurs/proxmox.md")
    audit(db, None, "ai.embedding_cost", space=wiki.infra, cost="0.01")
    # Older events that kept more than their action declares, a secret among it.
    audit(
        db,
        wiki.root,
        "org.webhook_created",
        org_id=wiki.infra.org_id,
        url=f"https://hooks.example.com/services/{SECRET}",
        secret=SECRET,
    )
    audit(db, wiki.root, "space.git_credentials_set", space=wiki.infra, kind="https", token=SECRET)
    value, _ = accounts.create_api_token(db, wiki.root, "ci")
    db.commit()
    login(client, "root")
    for url in ("/o/acme/-/journal", "/admin/journal", "/admin/journal.csv"):
        text = client.get(url).text
        assert SECRET not in text and value not in text, url
        assert "content.written" not in text and "ai.embedding_cost" not in text, url
    page = client.get("/o/acme/-/journal").text
    assert "hooks.example.com" in page and "Jeton HTTPS" in page


def test_role_changes_through_the_web_are_logged(
    client: TestClient, db: Session, wiki: Wiki
) -> None:
    login(client, "root")
    token = csrf(client, "/o/acme/-/members")
    client.post("/o/acme/-/members", data={"csrf": token, "username": "bob", "role": "admin"})
    client.post("/o/acme/-/members/bob/remove", data={"csrf": token})
    client.post("/o/acme/infra/-/members/alice/remove", data={"csrf": token})
    page = client.get("/o/acme/-/journal", params={"family": "members"}).text
    assert actions(page)[:3] == ["space.member_removed", "org.member_removed", "org.member_set"]
    assert "Rôle dans l&#39;organisation défini" in page and "Administrateur" in page
    assert "Retiré de l&#39;organisation" in page and "Retiré de l&#39;espace" in page
    assert 'Root <span class="muted">@root</span>' in page


def test_super_admin_and_account_state_changes_are_logged(
    client: TestClient, db: Session, wiki: Wiki
) -> None:
    login(client, "root")
    token = csrf(client, "/admin/users/alice")
    url = "/admin/users/alice"
    client.post(url, data={"csrf": token, "is_active": "true", "is_superadmin": "true"})
    client.post(url, data={"csrf": token, "is_active": "true", "is_superadmin": "true"})  # same
    client.post(url, data={"csrf": token, "is_active": "true"})
    client.post(url, data={"csrf": token})
    client.post(url, data={"csrf": token, "is_active": "true"})
    page = client.get("/admin/journal", params={"family": "security"}).text
    assert actions(page)[:4] == [
        "user.reactivated",
        "user.deactivated",
        "user.superadmin_revoked",
        "user.superadmin_granted",
    ]
    assert "user.updated" not in actions(page)  # nothing else changed
    assert "Droits de super-admin accordés" in page and "Compte désactivé" in page
    accounts.update_user(db, wiki.alice, accounts.get_user(db, "alice"), display_name="Alice B.")
    db.commit()
    [changed] = db.scalars(select(AuditEvent).where(AuditEvent.action == "user.updated"))
    assert changed.details == {"username": "alice", "fields": "display_name"}


def test_settings_say_what_changed_without_the_key(db: Session, ctx: Workspace, wiki: Wiki) -> None:
    org = wiki.infra.organization

    def last(action: str) -> dict[str, object]:
        found = db.scalars(
            select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.at.desc())
        ).first()
        assert found is not None
        return found.details

    sonnet = {"provider": "anthropic", "model": "claude-sonnet-5-5"}

    def configure(**values: object) -> None:
        ai_settings.configure(db, wiki.root, ctx.box, org, **sonnet, **values)  # type: ignore[arg-type]

    configure(api_key="sk-ant-" + SECRET)
    assert last("org.ai_configured") == {**sonnet, "key": "replaced", "budget": ""}
    configure(monthly_budget=Decimal("12.5"))
    assert last("org.ai_configured") == {**sonnet, "key": "", "budget": "12.5"}
    configure(api_key="")
    assert last("org.ai_configured") == {**sonnet, "key": "removed", "budget": "none"}
    semantic.configure(db, wiki.root, ctx.box, org, provider="openai", api_key=SECRET)
    assert last("org.embeddings_configured")["key"] == "replaced"
    spaces.update_space(db, wiki.root, wiki.infra, name="Infra 2", description="")
    assert last("space.updated")["fields"] == "name"
    tidy.update_settings(db, wiki.root, wiki.infra, enabled=False, delay=30)
    assert last("space.tidy_settings")["fields"] == "enabled delay"
    rows = db.scalars(select(AuditEvent.details)).all()
    assert all(SECRET not in str(details) for details in rows)


def test_invitations_keep_the_role(db: Session, wiki: Wiki) -> None:
    org = wiki.infra.organization
    invitations.invite(db, wiki.root, org, email="new@example.com", org_role=OrgRole.ADMIN)
    db.commit()
    [sent] = db.scalars(select(AuditEvent).where(AuditEvent.action == "org.invitation_created"))
    assert sent.details == {"email": "new@example.com", "role": "admin"}


def test_service_refuses_other_people(db: Session, wiki: Wiki) -> None:
    org = wiki.infra.organization
    filters = audit_log.Filters()
    with pytest.raises(SynaptaneError):
        audit_log.select_events(db, wiki.alice, org, filters)
    with pytest.raises(SynaptaneError):
        audit_log.select_events(db, wiki.alice, None, filters)
    admin = make_user(db, "erin")
    organizations.set_member(db, wiki.root, org, admin, OrgRole.ADMIN)
    db.commit()
    assert audit_log.select_events(db, actor_of(db, admin), org, filters).entries
    with pytest.raises(SynaptaneError):
        audit_log.select_events(db, actor_of(db, admin), None, filters)


def test_a_team_prompt_change_is_shown(client: TestClient, db: Session, wiki: Wiki) -> None:
    """Changing what assistants are told is a setting: in the log, with the prompt's name."""
    login(client, "root")
    token = csrf(client, "/o/acme/infra/-/prompts")
    response = client.post(
        "/o/acme/infra/-/prompts",
        data={"csrf": token, "name": "revue", "text": "Résume les décisions de la semaine."},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    page = client.get("/o/acme/-/journal", params={"family": "settings"}).text
    assert actions(page)[0] == "space.prompt_updated"
    assert "Prompt d&#39;assistant modifié" in page and "acme/infra</a> · revue" in page
    assert "Résume les décisions" not in page  # the text itself is not shown


def test_anonymous_gets_nothing(client: TestClient, wiki: Wiki) -> None:
    for url in (
        "/o/acme/-/journal",
        "/o/acme/-/journal.csv",
        "/admin/journal",
        "/admin/journal.csv",
    ):
        response = client.get(url, follow_redirects=False)
        assert response.status_code == 303, url
        assert "token.created" not in response.text and "acme/infra" not in response.text


def test_forged_parameters_keep_an_organization_to_itself(
    client: TestClient, db: Session, wiki: Wiki
) -> None:
    beta(db, wiki)
    acme_admin(db, wiki)
    accounts.create_api_token(db, wiki.alice, "laptop")
    db.commit()
    login(client, "carol")
    future = audit_log.cursor(
        audit_log.describe(AuditEvent(id=uuid4(), at=datetime(2100, 1, 1, tzinfo=UTC)), None)
    )
    for params in (
        {"org": "beta"},
        {"org": "-"},
        {"family": "security"},
        {"family": "access"},
        {"before": future},
        {"family": "members' OR 1=1 --"},
    ):
        page = client.get("/o/acme/-/journal", params=params)
        assert page.status_code == 200, params
        shown = set(actions(page.text))
        assert shown and not shown & {"token.created", "user.created"}, params
        assert "beta/docs" not in page.text and ">Beta<" not in page.text, params
        export = client.get("/o/acme/-/journal.csv", params=params).text
        rows = list(csv.reader(io.StringIO(export.removeprefix("﻿"))))
        assert all(row[4] in ("", "acme", "acme/infra", "acme/autre") for row in rows[1:])
        assert not {row[3] for row in rows[1:]} & {"token.created", "user.created"}, params
    client.cookies.clear()
    login(client, "alice")  # a plain member: nothing, even with a forged parameter
    assert client.get("/o/acme/-/journal.csv", params={"org": "-"}).status_code == 404
    assert client.get("/admin/journal.csv").status_code == 403


def test_free_texts_are_escaped_and_never_formulas(
    client: TestClient, db: Session, wiki: Wiki
) -> None:
    evil = "<script>alert(1)</script>"
    audit(db, wiki.alice, "oauth.approved", app=evil, read_only=True)
    audit(db, wiki.alice, "oauth.denied", app='=HYPERLINK("http://x","y")')
    audit(db, wiki.alice, "token.created", name="+cmd|' /C calc'!A0")
    audit(db, None, "alert.sync_error", space=wiki.infra, error="@SUM(1+1)")
    db.commit()
    login(client, "root")
    page = client.get("/admin/journal").text
    assert evil not in page and "&lt;script&gt;" in page
    export = client.get("/admin/journal.csv").text.removeprefix("﻿")
    for row in list(csv.reader(io.StringIO(export)))[1:]:
        assert all(value[:1] not in ("=", "+", "-", "@") for value in row), row


def test_the_export_says_where_it_stops(
    client: TestClient, db: Session, wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client, "root")
    page = client.get("/o/acme/-/journal").text
    assert "au plus les 10 000 lignes les plus récentes" in page
    assert "Du (UTC)" in page and "Au (UTC)" in page

    def rows() -> list[list[str]]:
        text = client.get("/o/acme/-/journal.csv").text.removeprefix("﻿")
        return list(csv.reader(io.StringIO(text)))

    assert all(row[3] for row in rows()[1:])  # everything fits: no note
    monkeypatch.setattr(audit_log, "EXPORT_LIMIT", 2)
    found = rows()
    assert len(found) == 1 + 2 + 1 and all(row[3] for row in found[1:3])
    assert found[-1][3] == "" and found[-1][2].startswith("Export arrêté aux 10")
