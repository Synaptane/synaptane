"""Inviting someone straight into groups: an organization admin checks groups of the
organization when inviting; on acceptance, the person becomes a member of the organization,
then of these groups, in the same transaction as the rest.

The invitation page says the real access, groups included; a group deleted before the
acceptance is simply not joined, a renamed one shows its new name; a group of another
organization is refused, by the service and by the database. Checked from the web, the REST
API, the audit log, in French and in English.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from synaptane.core.access import OrgRole, SpaceRole
from synaptane.core.errors import SynaptaneError, conflict
from synaptane.services import accounts, groups, invitations, load_actor, spaces
from synaptane.storage.db import Database
from synaptane.storage.models import (
    AuditEvent,
    Group,
    GroupMember,
    Invitation,
    InvitationGroup,
    OrgMember,
    User,
)
from tests.htmlutil import data_test, select_one
from tests.htmlutil import select as select_html
from tests.integration.conftest import PASSWORD, make_user, token_for
from tests.integration.teamdata import Team, configure, role
from tests.integration.test_groups import make_group
from tests.integration.test_rights_html import (
    as_user,
    assert_done,
    assert_refused,
    post,
    snapshot,
)

NOT_FOUND = "Groupe introuvable dans cette organisation."


def invite(
    db: Session, team: Team, email: str = "zoe@example.com", *joined: Group, **options: object
) -> invitations.Created:
    """An invitation to Acme by Carol, its administrator."""
    created = invitations.invite(
        db,
        load_actor(db, accounts.get_user(db, "carol")),
        team.wiki.infra.organization,
        email=email,
        group_ids=[group.id for group in joined],
        **options,  # type: ignore[arg-type]
    )
    db.commit()
    return created


def accept(db: Session, token: str, username: str = "zoe") -> User:
    user = invitations.accept(
        db,
        token,
        username=username,
        display_name=username.capitalize(),
        password=PASSWORD,
        locale="fr",
    )
    db.commit()
    return user


def groups_of(db: Session, username: str) -> list[str]:
    user = accounts.get_user(db, username)
    return sorted(
        db.scalars(
            select(Group.name)
            .join(GroupMember, GroupMember.group_id == Group.id)
            .where(GroupMember.user_id == user.id)
        )
    )


def links(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(InvitationGroup)) or 0


def link_of(page: str) -> str:
    """The invitation link revealed to the admin (email sending not configured)."""
    return "/invite/" + page.split("/invite/", 1)[1].split("<", 1)[0].split('"', 1)[0]


def verified(db: Session, username: str, email: str) -> User:
    user = make_user(db, username)
    user.email, user.email_verified_at = email, datetime.now(UTC)
    db.commit()
    return user


# --- From the web, end to end -------------------------------------------------------------------


def test_an_admin_invites_into_groups_and_the_guest_joins_them(
    db: Session, client: TestClient, team: Team
) -> None:
    """Infra open to everyone as reader (default role), Autre restricted: RH opens Autre as
    contributor, Direction gives contributor on Infra."""
    configure(db, team, "reader")
    spaces.update_space(db, team.wiki.root, team.wiki.other, restricted=True)
    rh = make_group(db, team, "RH", members=(), roles={"autre": "contributor"})
    direction = make_group(db, team, "Direction", members=(), roles={"infra": "contributor"})
    empty = make_group(db, team, "Vide", members=())
    token = as_user(client, "carol")

    form = select_one(client.get("/o/acme/-/members").text, data_test("invite-form"))
    choices = select_one(form, data_test("invite-groups"))
    offered = {
        select_one(label, 'input[name="group"]').get("value"): tuple(
            span.text for span in select_html(label, ".checkbox-detail > span")
        )
        for label in select_html(choices, "label.checkbox")
    }
    assert offered == {
        str(direction.id): ("Direction", "Contributeur sur Infra"),
        str(rh.id): ("RH", "Contributeur sur Autre (restreint)"),
        str(empty.id): ("Vide", "N'ouvre encore aucun espace"),
    }

    response = client.post(
        "/o/acme/-/invitations",
        data={
            "csrf": token,
            "email": "zoe@example.com",
            "org_role": "member",
            "group": [str(rh.id), str(direction.id)],
        },
        follow_redirects=False,
    )
    assert_done(response)
    revealed = client.get(response.headers["location"]).text
    path = link_of(revealed)
    [pending] = select_html(revealed, data_test("invitation-groups"))
    assert pending.text == "Groupes : Direction, RH"
    assert groups_of(db, "carol") == []  # nobody joined anything yet

    client.cookies.clear()
    page = client.get(path).text
    access = [li.text for li in select_html(select_one(page, data_test("invitation-access")), "li")]
    assert access == [
        "Autre : contributeur, par le groupe RH",
        "Infra : contributeur, par le groupe Direction",
    ]
    assert select_one(page, data_test("invitation-groups")).text == (
        "Vous rejoignez aussi les groupes Direction, RH."
    )
    joined = client.post(
        path,
        data={"username": "zoe", "display_name": "Zoé", "password": PASSWORD},
        follow_redirects=False,
    )
    assert joined.status_code == 303, joined.text[:300]
    db.expire_all()
    assert groups_of(db, "zoe") == ["Direction", "RH"]
    assert role(db, "zoe", team.wiki.infra) == "contributor"
    assert role(db, "zoe", team.wiki.other) == "contributor"  # restricted, opened by RH
    zoe = accounts.get_user(db, "zoe")
    member = db.get(OrgMember, (team.wiki.infra.org_id, zoe.id))
    assert member is not None and member.role == OrgRole.MEMBER.value
    assert client.get("/o/acme/autre").status_code == 200  # signed in by the acceptance


def test_without_groups_the_form_and_the_page_stay_as_before(
    db: Session, client: TestClient, team: Team
) -> None:
    token = as_user(client, "carol")
    form = select_one(client.get("/o/acme/-/members").text, data_test("invite-form"))
    assert select_html(form, data_test("invite-groups")) == []  # no group in Acme
    response = post(client, "/o/acme/-/invitations", token, email="zoe@example.com")
    assert_done(response)
    revealed = client.get(response.headers["location"]).text
    assert select_html(revealed, data_test("invitation-groups")) == []
    client.cookies.clear()
    page = client.get(link_of(revealed)).text
    assert select_html(page, data_test("invitation-groups")) == []
    [event] = db.scalars(select(AuditEvent).where(AuditEvent.action == "org.invitation_created"))
    assert "groups" not in event.details  # as before groups


def test_the_role_of_the_invitation_and_the_groups_are_told_apart(db: Session, team: Team) -> None:
    """Per space, the highest of the role given in the invitation, of the groups' roles and of
    the default role, with where it comes from: given first, then a group, then the default."""
    configure(db, team, "reader")
    rh = make_group(db, team, "RH", members=(), roles={"infra": "contributor", "autre": "reader"})
    paie = make_group(db, team, "Paie", members=(), roles={"infra": "contributor"})
    created = invite(
        db,
        team,
        "zoe@example.com",
        rh,
        paie,
        space_roles={str(team.wiki.other.id): "contributor"},
    )
    assert created.invitation is not None
    offer = invitations.describe(db, created.invitation)
    assert offer.inviter == "Carol" and offer.groups == ["Paie", "RH"]
    assert [(g.space, g.role, g.source, g.groups) for g in offer.granted] == [
        ("Autre", SpaceRole.CONTRIBUTOR, "given", ()),
        ("Infra", SpaceRole.CONTRIBUTOR, "group", ("Paie", "RH")),
    ]
    # Same order as the members page of a space: a group's role equal to the default one is
    # said as the group's; without a group role, the default role.
    groups.set_space_role(db, team.wiki.root, rh, team.wiki.infra, SpaceRole.READER)
    groups.remove_space_role(db, team.wiki.root, paie, team.wiki.infra)
    db.commit()
    granted = invitations.describe(db, created.invitation).granted
    assert [(g.space, g.source, g.groups) for g in granted] == [
        ("Autre", "given", ()),
        ("Infra", "group", ("RH",)),
    ]
    groups.remove_space_role(db, team.wiki.root, rh, team.wiki.infra)
    db.commit()
    granted = invitations.describe(db, created.invitation).granted
    assert [(g.space, g.role, g.source) for g in granted] == [
        ("Autre", SpaceRole.CONTRIBUTOR, "given"),
        ("Infra", SpaceRole.READER, "default"),
    ]
    # An administrator contributes everywhere, and still joins the groups.
    admin = invite(db, team, "ada@example.com", rh, org_role=OrgRole.ADMIN)
    assert admin.invitation is not None and admin.token is not None
    offer = invitations.describe(db, admin.invitation)
    assert {(g.role, g.source) for g in offer.granted} == {(SpaceRole.CONTRIBUTOR, "admin")}
    accept(db, admin.token, "ada")
    assert groups_of(db, "ada") == ["RH"]


def test_the_invitation_page_in_english(db: Session, client: TestClient, team: Team) -> None:
    rh = make_group(db, team, "RH", members=(), roles={"autre": "reader"})
    direction = make_group(db, team, "Direction", members=(), roles={"autre": "reader"})
    created = invite(db, team, "zoe@example.com", rh, direction)
    page = client.get(f"/invite/{created.token}", headers={"Accept-Language": "en"}).text
    access = select_one(page, data_test("invitation-access")).text
    assert access == "Autre: reader, through the groups Direction, RH"
    assert select_one(page, data_test("invitation-groups")).text == (
        "You also join the groups Direction, RH."
    )
    one = invite(db, team, "ada@example.com", rh)
    page = client.get(f"/invite/{one.token}", headers={"Accept-Language": "en"}).text
    assert select_one(page, data_test("invitation-access")).text == (
        "Autre: reader, through the group RH"
    )
    assert select_one(page, data_test("invitation-groups")).text == "You also join the group RH."


# --- Edge cases -----------------------------------------------------------------------------------


def test_a_group_deleted_before_the_acceptance_is_not_joined(
    db: Session, client: TestClient, team: Team
) -> None:
    rh = make_group(db, team, "RH", members=(), roles={"autre": "contributor"})
    direction = make_group(db, team, "Direction", members=(), roles={"infra": "reader"})
    created = invite(db, team, "zoe@example.com", rh, direction)
    assert created.token is not None and links(db) == 2
    groups.delete_group(db, team.wiki.root, rh)
    db.commit()
    assert links(db) == 1  # ON DELETE CASCADE

    page = client.get(f"/invite/{created.token}").text
    assert select_one(page, data_test("invitation-access")).text == (
        "Infra : lecteur, par le groupe Direction"
    )
    assert "RH" not in page
    accept(db, created.token)
    assert groups_of(db, "zoe") == ["Direction"]
    assert role(db, "zoe", team.wiki.other) is None
    # Every group gone: the invitation still gives the organization.
    second = invite(db, team, "ada@example.com", direction)
    assert second.token is not None
    groups.delete_group(db, team.wiki.root, direction)
    db.commit()
    accept(db, second.token, "ada")
    assert groups_of(db, "ada") == []
    ada = accounts.get_user(db, "ada")
    assert db.get(OrgMember, (team.wiki.infra.org_id, ada.id)) is not None


def test_a_renamed_group_shows_its_new_name_and_is_joined(
    db: Session, client: TestClient, team: Team
) -> None:
    rh = make_group(db, team, "RH", members=(), roles={"autre": "reader"})
    created = invite(db, team, "zoe@example.com", rh)
    assert created.token is not None
    groups.update_group(db, team.wiki.root, rh, name="Ressources humaines")
    db.commit()
    page = client.get(f"/invite/{created.token}").text
    assert select_one(page, data_test("invitation-groups")).text == (
        "Vous rejoignez aussi le groupe Ressources humaines."
    )
    accept(db, created.token)
    assert groups_of(db, "zoe") == ["Ressources humaines"]
    # The log keeps the name at the time of the invitation.
    [event] = db.scalars(select(AuditEvent).where(AuditEvent.action == "org.invitation_created"))
    assert event.details["groups"] == "RH"


def test_a_group_of_another_organization_is_refused(
    db: Session, database: Database, client: TestClient, team: Team
) -> None:
    foreign = groups.create_group(db, team.wiki.root, team.ventes.organization, name="Globex")
    rh = make_group(db, team, "RH", members=())
    db.commit()
    for chosen in ([foreign.id], [rh.id, foreign.id], ["pas-un-uuid"]):
        with pytest.raises(SynaptaneError) as error:
            invitations.invite(
                db,
                load_actor(db, accounts.get_user(db, "carol")),
                team.wiki.infra.organization,
                email="zoe@example.com",
                group_ids=chosen,
            )
        assert error.value.code == "not_found"
        db.rollback()
    before = snapshot(database)
    token = as_user(client, "carol")
    assert_refused(
        client,
        post(
            client, "/o/acme/-/invitations", token, email="zoe@example.com", group=str(foreign.id)
        ),
        NOT_FOUND,
    )
    assert snapshot(database) == before
    assert db.scalar(select(func.count()).select_from(Invitation)) == 0

    # The database refuses it too: the link carries the invitation's organization.
    created = invite(db, team, "zoe@example.com")
    assert created.invitation is not None
    for org_id in (team.wiki.infra.org_id, team.ventes.org_id):
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO invitation_groups (invitation_id, group_id, org_id) "
                    "VALUES (:invitation, :group, :org)"
                ),
                {"invitation": created.invitation.id, "group": foreign.id, "org": org_id},
            )
        db.rollback()


def test_a_revoked_or_expired_invitation_joins_no_group(
    db: Session, client: TestClient, team: Team
) -> None:
    rh = make_group(db, team, "RH", members=(), roles={"autre": "reader"})
    revoked = invite(db, team, "zoe@example.com", rh)
    assert revoked.invitation is not None and revoked.token is not None
    invitations.revoke(
        db,
        load_actor(db, accounts.get_user(db, "carol")),
        team.wiki.infra.organization,
        revoked.invitation.id,
    )
    db.commit()
    assert links(db) == 0
    assert client.get(f"/invite/{revoked.token}").status_code == 410
    with pytest.raises(SynaptaneError):
        accept(db, revoked.token)
    db.rollback()

    expired = invite(db, team, "zoe@example.com", rh)
    assert expired.invitation is not None and expired.token is not None
    expired.invitation.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    assert client.get(f"/invite/{expired.token}").status_code == 410
    with pytest.raises(SynaptaneError):
        accept(db, expired.token)
    db.rollback()
    assert db.scalar(select(User).where(User.username == "zoe")) is None
    assert db.scalar(select(func.count()).select_from(GroupMember)) == 0

    # Re-inviting renews the invitation, groups included: only the new choice counts.
    direction = make_group(db, team, "Direction", members=())
    invite(db, team, "zoe@example.com", rh)
    renewed = invite(db, team, "zoe@example.com", direction)
    assert renewed.token is not None
    assert db.scalars(select(InvitationGroup.group_id)).all() == [direction.id]
    accept(db, renewed.token)
    assert groups_of(db, "zoe") == ["Direction"]


def test_an_existing_account_joins_the_groups_at_once(db: Session, team: Team) -> None:
    rh = make_group(db, team, "RH", members=("dora",), roles={"autre": "contributor"})
    direction = make_group(db, team, "Direction", members=())
    verified(db, "frank", "frank@example.com")
    created = invite(db, team, "frank@example.com", rh, direction)
    assert created.invitation is None and created.existing is not None
    assert [g.name for g in created.groups] == ["Direction", "RH"]
    assert groups_of(db, "frank") == ["Direction", "RH"]
    assert role(db, "frank", team.wiki.other) == "contributor"
    assert links(db) == 0  # nothing to keep: done

    # Already a member of the organization and of a group: no error, nothing twice.
    dora = accounts.get_user(db, "dora")
    dora.email, dora.email_verified_at = "dora@example.com", datetime.now(UTC)
    db.commit()
    invite(db, team, "dora@example.com", rh, direction)
    assert groups_of(db, "dora") == ["Direction", "RH"]
    added = db.scalars(
        select(AuditEvent.details).where(AuditEvent.action == "group.member_added")
    ).all()
    assert sorted((d["group"], d["username"]) for d in added) == [
        ("Direction", "dora"),
        ("Direction", "frank"),
        ("RH", "dora"),  # by make_group
        ("RH", "frank"),
    ]


def test_invitations_of_several_organizations_each_bring_their_groups(
    db: Session, team: Team
) -> None:
    """The link proves the address: the pending invitations of the other organizations are
    honored too, each with its own groups."""
    rh = make_group(db, team, "RH", members=(), roles={"autre": "reader"})
    globex = team.ventes.organization
    ventes = groups.create_group(db, team.wiki.root, globex, name="Ventes")
    groups.set_space_role(db, team.wiki.root, ventes, team.ventes, SpaceRole.READER)
    db.commit()
    acme = invite(db, team, "zoe@example.com", rh)
    invitations.invite(db, team.wiki.root, globex, email="zoe@example.com", group_ids=[ventes.id])
    db.commit()
    assert acme.token is not None
    accept(db, acme.token)
    assert groups_of(db, "zoe") == ["RH", "Ventes"]
    assert role(db, "zoe", team.ventes) == "contributor"  # Globex's default role
    assert role(db, "zoe", team.wiki.other) == "reader"


def test_the_acceptance_is_one_transaction(
    db: Session, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A group that cannot be joined (refused at the same time by the database) undoes the
    whole acceptance: no account, no membership, the invitation still pending."""
    rh = make_group(db, team, "RH", members=())
    created = invite(db, team, "zoe@example.com", rh)
    assert created.token is not None

    def refused(*args: object) -> bool:
        raise conflict(groups.CONCURRENT)

    monkeypatch.setattr(groups, "add_member", refused)
    with pytest.raises(SynaptaneError):
        accept(db, created.token)
    db.rollback()
    assert db.scalar(select(User).where(User.username == "zoe")) is None  # nor membership
    assert [i.accepted_at for i in db.scalars(select(Invitation))] == [None]
    assert links(db) == 1
    monkeypatch.undo()
    accept(db, created.token)
    assert groups_of(db, "zoe") == ["RH"]


# --- Rights and forged requests -------------------------------------------------------------------


def test_only_an_admin_invites_into_groups(
    db: Session, database: Database, client: TestClient, team: Team
) -> None:
    rh = make_group(db, team, "RH", members=("dora",), roles={"autre": "contributor"})
    before = snapshot(database)
    with pytest.raises(SynaptaneError) as error:
        invitations.invite(
            db,
            load_actor(db, accounts.get_user(db, "dora")),
            team.wiki.infra.organization,
            email="zoe@example.com",
            group_ids=[rh.id],
        )
    assert error.value.code == "forbidden"
    db.rollback()
    for username in ("dora", "alice"):  # a member of the group, a contributor of a space
        token = as_user(client, username)
        form = client.get("/o/acme/-/members").text
        assert 'name="group"' not in form
        refused = post(
            client, "/o/acme/-/invitations", token, email="zoe@example.com", group=str(rh.id)
        )
        assert_refused(client, refused)
    for username in ("dora", "alice", "eve"):
        headers = {"Authorization": f"Bearer {token_for(database, username)}"}
        answer = client.post(
            "/api/v1/organizations/acme/invitations",
            json={"email": "zoe@example.com", "groups": [str(rh.id)]},
            headers=headers,
        )
        assert answer.status_code in (403, 404), username
    assert snapshot(database) == before


def test_a_forged_invitation_changes_nothing(
    db: Session, database: Database, client: TestClient, team: Team
) -> None:
    rh = make_group(db, team, "RH", members=())
    before = snapshot(database)
    as_user(client, "carol")
    forged = client.post(
        "/o/acme/-/invitations",
        data={"csrf": "faux", "email": "zoe@example.com", "group": str(rh.id)},
        follow_redirects=False,
    )
    assert forged.status_code in (303, 400, 403)
    missing = client.post(
        "/o/acme/-/invitations",
        data={"email": "zoe@example.com", "group": str(rh.id)},
        follow_redirects=False,
    )
    assert missing.status_code in (303, 400, 403, 422)
    assert snapshot(database) == before


# --- REST API -------------------------------------------------------------------------------------


def test_rest_api_invites_into_groups(
    db: Session, database: Database, client: TestClient, team: Team
) -> None:
    rh = make_group(db, team, "RH", members=(), roles={"autre": "reader"})
    direction = make_group(db, team, "Direction", members=())
    foreign = groups.create_group(db, team.wiki.root, team.ventes.organization, name="Globex")
    verified(db, "frank", "frank@example.com")
    headers = {"Authorization": f"Bearer {token_for(database, 'carol')}"}
    url = "/api/v1/organizations/acme/invitations"

    created = client.post(
        url,
        json={"email": "zoe@example.com", "groups": [str(rh.id), str(direction.id)]},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["groups"] == [
        {"id": str(direction.id), "name": "Direction"},
        {"id": str(rh.id), "name": "RH"},
    ]
    listed = client.get(url, headers=headers).json()
    assert [i["groups"] for i in listed] == [body["groups"]]

    granted = client.post(
        url, json={"email": "frank@example.com", "groups": [str(rh.id)]}, headers=headers
    )
    assert granted.status_code == 201 and granted.json()["granted"] is True
    assert granted.json()["groups"] == [{"id": str(rh.id), "name": "RH"}]
    db.expire_all()
    assert groups_of(db, "frank") == ["RH"]

    for wrong in ([str(foreign.id)], [str(rh.id), "00000000-0000-0000-0000-000000000000"]):
        refused = client.post(
            url, json={"email": "x@example.com", "groups": wrong}, headers=headers
        )
        assert refused.status_code == 404, refused.text
        assert refused.json()["error"] == "not_found"
    malformed = client.post(url, json={"email": "x@example.com", "groups": ["RH"]}, headers=headers)
    assert malformed.status_code == 422
    assert [i["email"] for i in client.get(url, headers=headers).json()] == ["zoe@example.com"]

    # A group deleted since: no longer listed.
    groups.delete_group(db, team.wiki.root, rh)
    db.commit()
    assert client.get(url, headers=headers).json()[0]["groups"] == [
        {"id": str(direction.id), "name": "Direction"}
    ]


# --- Audit log ------------------------------------------------------------------------------------


def test_the_log_keeps_the_groups_of_the_invitation(
    db: Session, client: TestClient, team: Team
) -> None:
    rh = make_group(db, team, "RH", members=())
    direction = make_group(db, team, "Direction", members=())
    verified(db, "frank", "frank@example.com")
    created = invite(db, team, "zoe@example.com", rh, direction)
    invite(db, team, "frank@example.com", rh)
    assert created.token is not None
    start = datetime.now(UTC)
    accept(db, created.token)

    def details(action: str) -> list[dict[str, object]]:
        return [
            dict(e.details)
            for e in db.scalars(
                select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.at)
            )
        ]

    assert details("org.invitation_created") == [
        {"email": "zoe@example.com", "role": "member", "groups": "Direction, RH"}
    ]
    assert details("org.member_invited") == [
        {"username": "frank", "role": "member", "groups": "RH"}
    ]
    # The acceptance adds to the groups like an addition by hand, by the system (the
    # invitation signed by an admin), in the same transaction as the rest.
    accepted = db.scalars(
        select(AuditEvent).where(AuditEvent.at >= start).order_by(AuditEvent.at, AuditEvent.id)
    ).all()
    assert {(e.action, e.actor_id) for e in accepted if e.action.startswith("group.")} == {
        ("group.member_added", None)
    }
    assert sorted(
        str(e.details["group"]) for e in accepted if e.action == "group.member_added"
    ) == [
        "Direction",
        "RH",
    ]
    assert {e.action for e in accepted} >= {"org.member_set", "org.invitation_accepted"}

    as_user(client, "carol")
    journal = client.get("/o/acme/-/journal", params={"family": "members"}).text
    assert "Groupes : Direction, RH" in " ".join(journal.split()).replace(" ", " ")
