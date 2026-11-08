"""The audit log's pure logic: which actions it shows, what a line says of an event, cursors,
filters and the CSV export."""

import ast
import csv
import io
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from synaptane.services import alerts, audit_log, budget, freshness, reports, vocation
from synaptane.services.audit_log import ACTIONS, FAMILIES, FIELDS, NOISE, Entry
from synaptane.storage.models import AuditEvent, User
from synaptane.web.audit_pages import cell, to_csv
from synaptane.web.i18n import translator
from tests.unit.test_i18n import CATALOG, SOURCE, worded

SECRET = "syn_not-to-be-seen-0123456789"


def written_actions() -> set[str]:
    """Actions the code writes to `audit_events`: literal ones (`audit(…, "x.y")`,
    `AuditEvent(action="x.y")`), and those it builds or keeps in constants."""
    found: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(Path(path).read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            argument: ast.expr | None
            if name == "audit" and len(node.args) >= 3:
                argument = node.args[2]
            elif name == "AuditEvent":
                argument = next((k.value for k in node.keywords if k.arg == "action"), None)
            else:
                continue
            # `"a" if x else "b"`: both.
            choices = (
                [argument.body, argument.orelse] if isinstance(argument, ast.IfExp) else [argument]
            )
            for choice in choices:
                if isinstance(choice, ast.Constant) and isinstance(choice.value, str):
                    found.add(choice.value)
    found |= {f"alert.{kind}" for kind in (*alerts.KINDS, *alerts.ORG_KINDS)}
    found |= {f"page.report_{r}" for r in (reports.VERIFIED, reports.DISMISSED, reports.WITHDRAWN)}
    found |= {kind.action for kind in vocation.KINDS.values()}
    found |= {budget.EMBEDDING_COST, freshness.REMINDER}
    found |= {"content.written", "content.deleted", "content.moved"}  # `content._after_write`
    return found


def test_every_action_written_is_shown_or_left_out_on_purpose() -> None:
    """A new action must be given a line in the log (`ACTIONS`) or be declared noise."""
    written = written_actions()
    assert "org.member_set" in written and "user.superadmin_granted" in written
    assert sorted(written - set(ACTIONS) - NOISE) == []
    assert sorted(set(ACTIONS) & NOISE) == []
    assert sorted(NOISE - written) == []  # no stale exception


def test_actions_are_well_formed() -> None:
    assert set(audit_log.ORG_FAMILIES) <= set(FAMILIES)
    for action, kind in ACTIONS.items():
        assert kind.family in FAMILIES, action
        assert set(kind.fields) <= set(FIELDS), action
        assert kind.author in (audit_log.SYSTEM, audit_log.SUBJECT, audit_log.VISITOR), action


def event(action: str, actor: User | None = None, /, **details: object) -> AuditEvent:
    return AuditEvent(
        id=uuid4(),
        at=datetime(2026, 11, 8, 9, 30, tzinfo=UTC),
        actor_id=actor.id if actor else None,
        action=action,
        details={k: str(v) for k, v in details.items()},
    )


# Every detail an action may hold, and a secret under names that should never be shown.
EVERYTHING = {
    "username": "alice",
    "email": "alice@example.com",
    "name": "laptop",
    "app": "Claude",
    "provider": "anthropic",
    "routine": "Weekly",
    "url": "https://hooks.example.com/services/T0/B0/" + SECRET,
    "slug": "infra",
    "host": "smtp.example.com",
    "role": "admin",
    "model": "claude-sonnet",
    "key": "replaced",
    "budget": "15.00",
    "read_only": "True",
    "inbox_only": "True",
    "scoped": "True",
    "enabled": "False",
    "superadmin": "True",
    "kind": "ssh",
    "fields": "name repository unknown",
    "auto_create": "True",
    "domains": "example.com",
    "template": "knowledge-base",
    "level": "80",
    "spent": "12.00",
    "error": "repository gone",
    "reason": "interrupted",
    "status": "500",
    "token": SECRET,
    "password": SECRET,
    "secret": SECRET,
    "api_key": SECRET,
}


def labels(entry: Entry) -> list[str]:
    french = translator("fr")
    found = [entry.title, *entry.subject, *entry.details]
    if entry.author_label is not None:
        found.append(entry.author_label)
    return [french(label) for label in found]


def test_a_line_shows_only_the_fields_of_its_action_and_never_a_secret() -> None:
    for action in ACTIONS:
        entry = audit_log.describe(event(action, **EVERYTHING), None)
        assert all(SECRET not in text for text in labels(entry)), action
    entry = audit_log.describe(event("org.member_set", **EVERYTHING), None)
    assert labels(entry)[1:] == ["@alice", "Administrateur", "Système"]
    # Free texts are cut.
    long = audit_log.describe(event("alert.sync_error", error="x" * 1000), None)
    assert len(long.details[0]) == audit_log.TEXT_LIMIT


def test_every_text_of_a_line_is_translated() -> None:
    """Including the texts built from a template (`Provider: {value}`), which the catalog
    tests do not see."""
    missing = set()
    for action in ACTIONS:
        entry = audit_log.describe(event(action, **EVERYTHING), None)
        for label in [entry.title, *entry.subject, *entry.details, entry.author_label]:
            if label is not None and worded(label.template) and label.template not in CATALOG:
                missing.add(label.template)
    for family in FAMILIES.values():
        if family not in CATALOG:
            missing.add(family)
    for name in audit_log.CHANGED.values():
        if name not in CATALOG:
            missing.add(name)
    assert sorted(missing) == []


def test_a_webhook_shows_its_host_only() -> None:
    entry = audit_log.describe(event("org.webhook_created", url=EVERYTHING["url"]), None)
    assert entry.subject == ["hooks.example.com"]
    broken = audit_log.describe(event("org.webhook_created", url="http://[::1"), None)
    assert broken.subject == []


def test_details_in_french() -> None:
    french = translator("fr")

    def details(action: str, **values: object) -> list[str]:
        return [french(d) for d in audit_log.describe(event(action, **values), None).details]

    assert details("org.ai_configured", provider="openai", model="m", key="removed", budget="none")
    assert details("org.ai_configured", key="removed", budget="none") == [
        "Clé d'API retirée",
        "Aucun budget mensuel",
    ]
    assert details("org.ai_configured", budget="12.50") == ["Budget mensuel : 12.50 €"]
    assert details("space.updated", fields="name branch") == ["Nom", "Branche"]
    assert details("space.embeddings", enabled="True") == ["Activée"]
    assert details("space.git_credentials_set", kind="https") == ["Jeton HTTPS"]
    assert details("token.created", read_only="False", inbox_only="True", scoped="True") == [
        "Lecture et inbox seulement",
        "Espaces choisis seulement",
    ]
    assert details("org.ai_budget_alert", level="80", spent="8.00", budget="10.00") == [
        "80 % du budget mensuel (8.00 € sur 10.00 €)"
    ]
    assert details("org.ai_budget_alert", level="80") == []  # incomplete: nothing
    assert details("space.vocation_updated", template="nope") == ["Modèle : nope"]
    assert details("space.member_set", role="owner") == []  # unknown value: nothing


def test_the_author_of_an_event_without_an_account() -> None:
    french = translator("fr")

    def author(action: str, actor: User | None = None, /, **values: object) -> str:
        entry = audit_log.describe(event(action, actor, **values), None)
        assert entry.author_label is not None
        return french(entry.author_label)

    assert author("alert.sync_error") == "Système"
    assert author("org.invitation_accepted", username="alice") == "@alice"
    assert author("org.invitation_accepted") == "Système"  # no name kept
    assert author("user.password_reset_requested", username="alice") == "Non connecté"
    ghost = User(id=uuid4(), username="ghost", display_name="Ghost")
    assert author("org.updated", ghost) == "Compte supprimé"  # account not loaded any more


def test_an_unknown_action_shows_its_code_only() -> None:
    entry = audit_log.describe(event("later.added", token=SECRET), None)
    assert entry.title == "later.added" and entry.subject == [] and entry.details == []


def test_cursor_round_trip_and_garbage() -> None:
    entry = audit_log.describe(event("org.updated"), None)
    at, ident = audit_log.parse_cursor(audit_log.cursor(entry)) or (None, None)
    assert at == entry.at and ident == entry.id
    for garbage in ("", "abc", "12.zz", "99999999999999999999999.0", "1"):
        assert audit_log.parse_cursor(garbage) is None


def test_filters_from_the_form() -> None:
    filters = audit_log.parse_filters("members", "2026-11-01", " 2026-11-08 ", " acme ")
    assert filters == audit_log.Filters("members", date(2026, 11, 1), date(2026, 11, 8), "acme")
    assert audit_log.parse_filters("nope", "yesterday", "") == audit_log.Filters()


def test_actions_of_a_view() -> None:
    org_view = audit_log.ORG_FAMILIES
    assert "token.created" not in audit_log.actions_of(org_view)
    assert "org.member_set" in audit_log.actions_of(org_view)
    assert audit_log.actions_of(org_view, "security") == audit_log.actions_of(org_view)
    members = audit_log.actions_of(org_view, "members")
    assert members and all(ACTIONS[a].family == "members" for a in members)
    assert "alert.sync_error" in audit_log.actions_of(org_view, "alerts")


def test_csv_cells_are_never_formulas() -> None:
    assert cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert cell("@alice") == "'@alice" and cell("-1") == "'-1" and cell("+1") == "'+1"
    assert cell("Alice") == "Alice" and cell("") == ""


def test_csv_export() -> None:
    root = User(id=uuid4(), username="root", display_name="Root")
    entries = [
        audit_log.describe(event("space.member_set", root, username="alice", role="reader"), root),
        audit_log.describe(event("alert.sync_error", error="=cmd()"), None),
        audit_log.describe(event("org.invitation_accepted", username="bob"), None),
    ]
    text = to_csv(entries, translator("fr"))
    assert text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(text.removeprefix("﻿"))))
    assert rows[0] == ["Date (UTC)", "Auteur", "Action", "Code", "Lieu", "Cible", "Détails"]
    assert rows[1] == [
        "2026-11-08 09:30:00",
        "root",
        "Rôle dans l'espace défini",
        "space.member_set",
        "",
        "alice",
        "Lecteur",
    ]
    assert rows[2][1] == "Système" and rows[2][6] == "'=cmd()"
    assert rows[3][1] == "bob" and rows[3][5] == "bob"  # done by the account itself
