"""The audit log's pure logic: which actions it shows, what a line says of an event, cursors,
filters and the CSV export."""

import ast
import csv
import io
from datetime import UTC, date, datetime
from uuid import uuid4

from synaptane.services import alerts, audit_log, reports
from synaptane.services.audit_log import ACTIONS, FAMILIES, FIELDS, NOISE, Entry
from synaptane.storage.models import AuditEvent, User
from synaptane.web.audit_pages import cell, to_csv
from synaptane.web.i18n import translator
from tests.unit.test_i18n import CATALOG, SOURCE, worded

SECRET = "syn_not-to-be-seen-0123456789"


# Calls whose argument becomes an action written elsewhere: (function, position, keyword).
CARRIERS = {
    ("vocation", "Kind"): (4, "action"),  # `vocation.save` writes `kind.action`
    ("", "_after_write"): (3, "action"),  # content: `audit(…, action, …)`
}
# Writers whose action is not written out: the file, and the actions it can write. A new one
# fails the test until it is listed here.
INDIRECT = {
    "services/__init__.py": set(),  # `audit` itself, which every writer above goes through
    "services/vocation.py": set(),  # `kind.action`: the `vocation.Kind` carried above
    "services/content.py": set(),  # `action`: given to `_after_write`, carried above
    "services/alerts.py": {f"alert.{kind}" for kind in (*alerts.KINDS, *alerts.ORG_KINDS)},
    "services/reports.py": {
        f"page.report_{r}" for r in (reports.VERIFIED, reports.DISMISSED, reports.WITHDRAWN)
    },
}


def _modules() -> dict[str, ast.Module]:
    return {
        str(path.relative_to(SOURCE)): ast.parse(path.read_text(encoding="utf-8"))
        for path in SOURCE.rglob("*.py")
    }


def _constants(module: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "text"`."""
    return {
        target.id: node.value.value
        for node in module.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _names(node: ast.expr | None, local: dict[str, str], shared: dict[str, str]) -> set[str] | None:
    """The texts an expression can be, or None if they cannot be read from the code."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):  # `"a" if x else "b"`: both
        body, orelse = _names(node.body, local, shared), _names(node.orelse, local, shared)
        return body | orelse if body is not None and orelse is not None else None
    if isinstance(node, ast.Name) and node.id in local:
        return {local[node.id]}
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):  # module.NAME
        return {shared[node.attr]} if node.attr in shared else None
    return None


def _argument(call: ast.Call, position: int, keyword: str) -> ast.expr | None:
    if len(call.args) > position:
        return call.args[position]
    return next((k.value for k in call.keywords if k.arg == keyword), None)


def _callee(call: ast.Call) -> tuple[str, str]:
    func = call.func
    if isinstance(func, ast.Attribute):
        owner = func.value.id if isinstance(func.value, ast.Name) else ""
        return owner, func.attr
    return "", func.id if isinstance(func, ast.Name) else ""


def written_actions() -> tuple[set[str], set[str], list[str]]:
    """Every action the code writes to `audit_events`: (actions, files whose writes are not
    written out, carriers whose action cannot be read)."""
    modules = _modules()
    locals_ = {name: _constants(module) for name, module in modules.items()}
    shared = {k: v for constants in locals_.values() for k, v in constants.items()}
    found: set[str] = set()
    indirect: set[str] = set()
    unreadable: list[str] = []
    for name, module in modules.items():
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            owner, function = _callee(node)
            if function == "audit":
                argument = _argument(node, 2, "action")
            elif function == "AuditEvent":
                argument = _argument(node, 99, "action")
            elif (owner, function) in CARRIERS or (
                function == "Kind" and name == "services/vocation.py"
            ):
                position, keyword = CARRIERS.get((owner, function), CARRIERS[("vocation", "Kind")])
                carried = _names(_argument(node, position, keyword), locals_[name], shared)
                if carried is None:
                    unreadable.append(f"{name}:{node.lineno}")
                else:
                    found |= carried
                continue
            else:
                continue
            names = _names(argument, locals_[name], shared)
            if names is None:
                indirect.add(name)
            else:
                found |= names
    for actions in INDIRECT.values():
        found |= actions
    return found, indirect, unreadable


def test_every_action_written_is_shown_or_left_out_on_purpose() -> None:
    """A new action must be given a line in the log (`ACTIONS`) or be declared noise, however
    it is written: literal, constant, `vocation.Kind`, `_after_write`…"""
    written, indirect, unreadable = written_actions()
    assert unreadable == []
    assert sorted(indirect) == sorted(INDIRECT)  # a new indirect writer must be explained
    for action in (
        "org.member_set",  # literal
        "user.superadmin_granted",  # `"a" if x else "b"`
        "ai.embedding_cost",  # constant of the module
        "freshness.reminder",  # `AuditEvent(action=CONSTANT)`
        "space.vocation_updated",  # `Kind` of the module
        "space.prompt_updated",  # `vocation.Kind` elsewhere
        "content.moved",  # `_after_write`
        "alert.sync_error",
    ):
        assert action in written, action
    assert sorted(written - set(ACTIONS) - NOISE) == []
    assert sorted(set(ACTIONS) & NOISE) == []
    assert sorted(NOISE - written) == []  # no stale exception


def test_the_guard_reads_every_form() -> None:
    """The forms `written_actions` understands, and the one it refuses."""
    local, shared = {"COST": "a.cost"}, {"REMINDER": "a.reminder"}
    code = ast.parse(
        'f("x.y"); f("a" if c else "b"); f(COST); f(mod.REMINDER); f(other); f(f"alert.{k}")'
    )
    calls = [node for node in ast.walk(code) if isinstance(node, ast.Call) and node.args]
    assert [_names(call.args[0], local, shared) for call in calls] == [
        {"x.y"},
        {"a", "b"},
        {"a.cost"},
        {"a.reminder"},
        None,
        None,
    ]
    keyword = ast.parse('vocation.Kind("k", "p", "c", "r", action="z.w")').body[0]
    assert isinstance(keyword, ast.Expr) and isinstance(keyword.value, ast.Call)
    assert _callee(keyword.value) == ("vocation", "Kind")
    assert _names(_argument(keyword.value, 4, "action"), {}, {}) == {"z.w"}


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
        details={
            k: [str(i) for i in v] if isinstance(v, list) else str(v) for k, v in details.items()
        },
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
    "group": "RH",
    "groups": ["RH", "Direction"],
    "previous": "Ressources humaines",
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
    assert details("org.default_space_role", role="reader") == ["Lecteur"]
    assert details("org.default_space_role", role="none") == ["Pas d'accès"]
    assert details("space.restricted", restricted=True) == ["Restreint"]
    assert details("space.restricted", restricted=False) == ["Non restreint"]


def test_group_events_in_french() -> None:
    """The group is the subject of its events, with the account when there is one; a renaming
    says the former name, a role its value."""
    french = translator("fr")

    def line(action: str, **values: object) -> list[str]:
        return labels(audit_log.describe(event(action, **values), None))

    assert line("group.created", group="RH") == ["Groupe créé", "Groupe RH", "Système"]
    assert line("group.member_added", group="RH", username="dora") == [
        "Ajout à un groupe",
        "Groupe RH",
        "@dora",
        "Système",
    ]
    assert line("group.updated", group="RH", fields="name", previous="Paie")[2:4] == [
        "Nom",
        "Auparavant\xa0: Paie",
    ]
    assert line("group.updated", group="RH", fields="description", previous="")[2:] == [
        "Description",
        "Système",
    ]
    assert line("group.space_role_set", group="RH", role="contributor")[2] == "Contributeur"
    assert line("group.space_role_removed", group="RH")[0] == french(
        "Group role in the space removed"
    )
    for action in ("group.created", "group.deleted", "group.space_role_set"):
        assert audit_log.ACTIONS[action].family == "members"  # shown to organization admins


def test_invitation_events_say_their_groups() -> None:
    """The groups an invitation makes the person join, by their names at the time, stored as a
    list (a name may hold a comma) and shown one by one; none without groups."""
    assert labels(
        audit_log.describe(
            event(
                "org.invitation_created",
                email="z@x.fr",
                role="member",
                groups=["Direction", "RH, paie"],
            ),
            None,
        )
    )[:5] == ["Invitation envoyée", "z@x.fr", "Membre", "Groupe Direction", "Groupe RH, paie"]
    assert labels(
        audit_log.describe(event("org.member_invited", username="zoe", role="admin"), None)
    )[:3] == ["Compte existant ajouté par invitation", "@zoe", "Administrateur"]
    # Stored as one text (never released, but read all the same).
    assert labels(
        audit_log.describe(event("org.member_invited", username="zoe", groups="RH"), None)
    ) == ["Compte existant ajouté par invitation", "@zoe", "Groupes\xa0: RH", "Système"]
    long = ["x" * 400]
    [shown] = audit_log.describe(event("org.member_invited", groups=long), None).details
    assert len(translator("fr")(shown)) < 320


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
