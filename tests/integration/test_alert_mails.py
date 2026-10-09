"""Alerts and budget e-mails actually sent.

The other alert tests stop at "sending not configured": here, a test SMTP server (the one
used for invitations) receives the messages, and recipients, subject and body are checked in
both languages. Tidying e-mails go through the real `tidy.run`: they are sent in the session
that closes the log, which must stay valid (status, cost).
"""

import threading
from collections.abc import Iterator
from decimal import Decimal
from email.header import decode_header, make_header
from email.message import Message
from typing import NoReturn

import pytest
from sqlalchemy.orm import Session

from synaptane.ai import quota
from synaptane.ai.base import AIConfig, ToolSpec
from synaptane.core.access import OrgRole
from synaptane.services import accounts, ai_settings, alerts, mail, organizations, tidy, tidy_tools
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Space, User
from tests.integration.conftest import PASSWORD
from tests.integration.test_invitations import Inbox
from tests.integration.test_tidy import CloseAll, deposit_note
from tests.integration.wikidata import Wiki

PUBLIC_URL = "https://brain.example.com"

# Values of the same type as those of the real callers (services/tidy.py, git_sync.py, alerts.py).
ALERT_VALUES: dict[str, dict[str, object]] = {
    "deletion_cap": {"limit": tidy_tools.MAX_DELETIONS},
    "tidy_failed": {"error": "Erreur interne : RuntimeError: panne"},
    "sync_error": {"error": "dépôt introuvable"},
    "inbox_backlog": {"count": 12, "days": 4},
    "quota_high": {"usage": quota.summary({"windows": {"five_hour": {"before": 80, "after": 93}}})},
    "big_changes": {"details": "supprimée : serveurs/proxmox.md\nrenommée : README.md"},
    "tidy_interrupted": {
        "reason": "Erreur Anthropic 529",
        "count": 2,
        "attempts": 3,
        "given_up": "Nouveau NAS",
    },
    "job_abandoned": {"task": "index", "attempts": 5, "error": "Abandonnée : délai dépassé"},
    "routine_failed": {"routine": "Contrôle de cohérence", "error": "Erreur Anthropic 529"},
}


@pytest.fixture
def smtp(db: Session, wiki: Wiki, ctx: Workspace) -> Iterator[Inbox]:
    """The invitations' test SMTP server, on a free port chosen by the system
    (tests can run in parallel)."""
    inbox = Inbox()
    threading.Thread(target=inbox.serve_forever, daemon=True).start()
    mail.configure(
        db,
        wiki.root,
        ctx.box,
        host="127.0.0.1",
        port=inbox.server_address[1],
        security="none",
        sender="synaptane@example.com",
    )
    db.commit()
    yield inbox
    inbox.shutdown()
    inbox.server_close()


def received(message: Message) -> tuple[str, str, str]:
    """(recipient, decoded subject, decoded body) of a received message."""
    subject = str(make_header(decode_header(message["Subject"])))
    payload = message.get_payload(decode=True)
    assert isinstance(payload, bytes)
    body = payload.decode(message.get_content_charset() or "utf-8").replace("\r\n", "\n")
    return message["To"], subject, body


def by_recipient(inbox: Inbox) -> dict[str, tuple[str, str]]:
    found = {}
    for message in inbox.messages:
        to, subject, body = received(message)
        assert "synaptane@example.com" in message["From"]
        found[to] = (subject, body)
    assert len(found) == len(inbox.messages)  # a single message per recipient
    return found


@pytest.fixture
def admins(db: Session, wiki: Wiki) -> dict[str, User]:
    """Two Acme administrators, one in French, the other in English; Alice (plain
    contributor, with an address) must receive nothing."""
    org = wiki.infra.organization
    people = {}
    for username, locale in (("ada", "fr"), ("grace", "en")):
        user = accounts.create_user(
            db,
            wiki.root,
            username=username,
            display_name=username.capitalize(),
            password=PASSWORD,
            email=f"{username}@example.com",
            locale=locale,
        )
        organizations.set_member(db, wiki.root, org, user, OrgRole.ADMIN)
        people[locale] = user
    wiki.alice.user.email = "alice@example.com"
    db.commit()
    return people


def test_alert_kinds_are_all_covered() -> None:
    assert set(ALERT_VALUES) == set(alerts.KINDS)


@pytest.mark.parametrize("kind", sorted(ALERT_VALUES))
def test_each_alert_reaches_the_org_admins_in_their_language(
    db: Session, ctx: Workspace, wiki: Wiki, admins: dict[str, User], smtp: Inbox, kind: str
) -> None:
    space = db.get(Space, wiki.infra.id)
    assert space is not None
    values = ALERT_VALUES[kind]
    assert alerts.notify(db, ctx.box, space, kind, PUBLIC_URL, **values)
    db.commit()
    mails = by_recipient(smtp)
    assert set(mails) == {"ada@example.com", "grace@example.com"}
    link = f"{PUBLIC_URL}/o/acme/infra"
    details = {k: str(v) for k, v in values.items()}
    for locale, user in admins.items():
        expected = mail.compose(
            f"alert_{kind}", locale, space="Infra", org="Acme", link=link, **details
        )
        assert mails[user.email or ""] == expected
        subject, body = expected
        assert "Infra" in subject
        assert link in body and all(value in body for value in details.values())
    french, english = mails["ada@example.com"][1], mails["grace@example.com"][1]
    assert french.startswith("Bonjour") and english.startswith("Hello")

    # Same alert, same space: no new e-mail before `QUIET`.
    assert not alerts.notify(db, ctx.box, space, kind, PUBLIC_URL, **values)
    assert len(smtp.messages) == 2


def test_budget_mails_at_80_then_100_percent_through_tidying(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    admins: dict[str, User],
    smtp: Inbox,
) -> None:
    """Each run costs €0.008 (1,000 × 4 + 200 × 20, per million) on a budget of €0.01:
    the first crosses 80%, the second 100%. The e-mail is sent from the session that
    closes the log: the run must stay "done", with its cost."""
    ai_settings.configure(
        db,
        wiki.root,
        ctx.box,
        wiki.infra.organization,
        provider="anthropic",
        model="modele-maison",
        api_key="sk-ant-test",
        price_input=Decimal(4),
        price_output=Decimal(20),
        monthly_budget=Decimal("0.01"),
    )
    db.commit()
    link = f"{PUBLIC_URL}/o/acme/-/ai"
    for level, spent in ((80, "0.01"), (100, "0.02")):  # 0.008 then 0.016, rounded to 2
        smtp.messages.clear()
        deposit_note(db, database, ctx, wiki)
        record = tidy.run(
            database,
            ctx,
            wiki.infra.id,
            trigger="manual",
            factory=CloseAll().factory,
            public_url=PUBLIC_URL,
        )
        assert record is not None and record.status == "done" and record.cost
        assert record.finished_at is not None
        mails = by_recipient(smtp)
        assert set(mails) == {"ada@example.com", "grace@example.com"}
        french_subject, french_body = mails["ada@example.com"]
        english_subject, english_body = mails["grace@example.com"]
        assert french_subject == f"Budget d'IA de « Acme » : {level} % atteints"
        assert english_subject == f"AI budget of “Acme”: {level}% reached"
        assert f"{spent} € sur un budget mensuel de 0.01 € ({level} %)" in french_body
        assert f"{spent} € of a monthly budget of 0.01 € ({level}%)" in english_body
        assert link in french_body and link in english_body

    # Budget exhausted: the next run is refused. No new budget notice, but the
    # "tidying failed" alert is sent, with the reason for the refusal (current behavior).
    smtp.messages.clear()
    deposit_note(db, database, ctx, wiki)
    record = tidy.run(database, ctx, wiki.infra.id, trigger="manual", factory=CloseAll().factory)
    assert record is not None and record.status == "failed"
    mails = by_recipient(smtp)
    assert set(mails) == {"ada@example.com", "grace@example.com"}
    assert mails["ada@example.com"][0] == "Rangement de « Infra » en échec"
    # The reason, in each recipient's language.
    assert "Budget mensuel d'IA atteint" in mails["ada@example.com"][1]
    assert "Monthly AI budget reached" in mails["grace@example.com"][1]


def _broken(config: AIConfig, system: str, first: str, tools: list[ToolSpec]) -> NoReturn:
    raise RuntimeError("panne du modèle")


def test_a_failed_tidying_mails_the_admins_and_closes_its_log(
    db: Session,
    database: Database,
    ctx: Workspace,
    wiki: Wiki,
    admins: dict[str, User],
    smtp: Inbox,
) -> None:
    """Real path of `tidy_failed` (services/tidy.py): the error reaches the e-mail and the
    log is closed as "failed" despite the sending done in the same session."""
    ai_settings.configure(
        db,
        wiki.root,
        ctx.box,
        wiki.infra.organization,
        provider="anthropic",
        model="modele-maison",
        api_key="sk-ant-test",
    )
    db.commit()
    deposit_note(db, database, ctx, wiki)
    record = tidy.run(
        database, ctx, wiki.infra.id, trigger="manual", factory=_broken, public_url=PUBLIC_URL
    )
    assert record is not None and record.status == "failed"
    assert record.finished_at is not None
    mails = by_recipient(smtp)
    assert set(mails) == {"ada@example.com", "grace@example.com"}
    french_subject, french_body = mails["ada@example.com"]
    english_subject, english_body = mails["grace@example.com"]
    assert french_subject == "Rangement de « Infra » en échec"
    assert english_subject == "Tidying of “Infra” failed"
    for body in (french_body, english_body):
        assert "RuntimeError: panne du modèle" in body
        assert f"{PUBLIC_URL}/o/acme/infra/tidy" in body
