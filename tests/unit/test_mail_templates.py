"""E-mail templates: each one renders, in both languages, with the exact arguments of its
callers.

A broken template only shows when sending. For alerts and the budget, sending happens in the
session that closes the tidying log: a `KeyError` there would cancel the run's cost and
status, left "running" until `close_orphans`. Hence three guardrails:

- `CALLERS` lists, for each template, the arguments its callers pass; a template added
  without an entry makes the test fail;
- the template's `{…}` fields must be exactly these arguments (nothing missing, nothing extra);
- the real calls (`mail.compose`, `alerts.notify`) are read back from the source code, so that
  the table cannot lie.
"""

import ast
import string
from pathlib import Path

import pytest

from synaptane.services import alerts, invitations, mail, tidy_tools

SOURCE = Path(__file__).parents[2] / "src" / "synaptane"
LOCALES = ("fr", "en")
LINK = "https://brain.example.com/o/acme/infra"
PARAGRAPHS = {"tidy_notice_left"}  # paragraph inserted into another e-mail: no subject

# Arguments passed by the callers, with a value of the same type as the real one.
CALLERS: dict[str, dict[str, object]] = {
    # web/invite_pages.py, invitation of an unknown address
    "invitation": {
        "inviter": "Alice",
        "org": "Acme",
        "days": invitations.TTL.days,
        "link": "https://brain.example.com/invite/jeton",
    },
    # web/invite_pages.py, _notify_added: existing account added to the organization
    "added": {"inviter": "Alice", "org": "Acme", "link": "https://brain.example.com"},
    # web/invite_pages.py, forgotten password
    "reset": {"link": "https://brain.example.com/reset/jeton"},
    # services/budget.py, check_alerts
    "budget": {
        "org": "Acme",
        "level": 80,
        "spent": "8.00",
        "budget": "10.00",
        "link": "https://brain.example.com/o/acme/-/ai",
    },
    # services/alerts.py, notify: space, org and link, plus each caller's values
    "alert_deletion_cap": {
        "space": "Infra",
        "org": "Acme",
        "link": LINK,
        "limit": str(tidy_tools.MAX_DELETIONS),  # services/tidy.py
    },
    "alert_big_changes": {
        "space": "Infra",
        "org": "Acme",
        "link": LINK,
        "details": "supprimée : serveurs/proxmox.md",  # services/tidy.py
    },
    "alert_quota_high": {
        "space": "Infra",
        "org": "Acme",
        "link": LINK,
        "usage": "fenêtre de 5 h : 92 %",  # services/tidy.py
    },
    "alert_tidy_failed": {
        "space": "Infra",
        "org": "Acme",
        "link": LINK,
        "error": "Délai dépassé",  # services/tidy.py
    },
    "alert_sync_error": {
        "space": "Infra",
        "org": "Acme",
        "link": LINK,
        "error": "dépôt introuvable",  # services/git_sync.py
    },
    "alert_inbox_backlog": {
        "space": "Infra",
        "org": "Acme",
        "link": LINK,
        "count": "12",  # services/alerts.py, check_backlogs
        "days": "4",
    },
    "alert_tidy_interrupted": {
        "space": "Infra",
        "org": "Acme",
        "link": LINK,
        "reason": "Erreur Anthropic 529",  # services/tidy.py, _notify_interruption
        "count": "2",
        "attempts": "3",
        "given_up": "Nouveau NAS",
    },
    "alert_job_abandoned": {
        "space": "Infra",
        "org": "Acme",
        "link": LINK,
        "task": "index",  # services/alerts.py, check_abandoned_jobs
        "attempts": "5",
        "error": "Abandonnée : délai dépassé",
    },
    "alert_routine_failed": {
        "space": "Infra",
        "org": "Acme",
        "link": LINK,
        "routine": "Contrôle de cohérence",  # services/routine_agent.py, run_one
        "error": "Budget mensuel d'IA atteint",
    },
    # services/routine_agent.py, send_report: a routine's output sent to the team
    "routine_report": {
        "routine": "Résumé hebdomadaire",
        "space": "Infra",
        "org": "Acme",
        "text": "# Semaine\n\n- Le NAS passe à 8 To.",
        "link": "https://brain.example.com/o/acme/infra/p/rapports/semaine.md",
        "account": "https://brain.example.com/account",
    },
    # services/freshness.py, _send_reminder: an owner's stale pages
    "review_reminder": {
        "count": 2,
        "pages": "- Infra · serveurs/proxmox.md (à revoir depuis le 2026-09-01)\n  https://…",
        "account": "https://brain.example.com/account",
    },
    # services/alerts.py, notify_org: org and link, plus the caller's values
    "alert_webhook_disabled": {
        "org": "Acme",
        "link": "https://brain.example.com/o/acme",
        "url": "https://hooks.example.com/synaptane",  # services/webhooks.py
        "failures": "10",
        "status": "HTTP 502",
    },
    # services/tidy_notices.py, send: notice to the submitter, and paragraph of notes left
    "tidy_notice": {
        "space": "Infra",
        "org": "Acme",
        "count": "2",
        "items": "- Nouveau NAS : rangée dans stockage/nas.md",
        "left": "Une note laissée attend dans l'inbox.",
        "account": "https://brain.example.com/account",
    },
    "tidy_notice_left": {"link": LINK},
    # web/pages.py, send_email_check
    "verify_email": {"account": "ines", "link": "https://brain.example.com/verify-email/jeton"},
    # web/account_pages.py: notice sent to the old address
    "email_changed": {"new": "alice@example.org"},
    # web/invite_pages.py, test e-mail from the administration
    "test": {},
}


def fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}


def test_every_template_is_in_the_callers_table() -> None:
    """A new template must come with its callers in `CALLERS` (and hence this test)."""
    assert set(mail._TEXTS) == set(CALLERS)


def test_every_alert_kind_has_a_template() -> None:
    """`alerts.notify` composes `alert_{kind}`: each declared kind must have its template."""
    templates = {kind for kind in mail._TEXTS if kind.startswith("alert_")}
    assert templates == {f"alert_{kind}" for kind in (*alerts.KINDS, *alerts.ORG_KINDS)}


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("kind", sorted(CALLERS))
def test_template_renders_with_the_callers_arguments(kind: str, locale: str) -> None:
    """No forgotten field (KeyError when sending) and no ignored argument (lost information),
    and each value passed does end up in the message."""
    values = CALLERS[kind]
    assert set(mail._TEXTS[kind]) == set(LOCALES)
    subject_template, body_template = mail._TEXTS[kind][locale]
    assert fields(subject_template) | fields(body_template) == set(values)
    subject, body = mail.compose(kind, locale, **values)
    assert body.strip()
    assert subject.strip() or kind in PARAGRAPHS
    assert "\n" not in subject  # a newline would break the header
    assert "{" not in subject + body and "}" not in subject + body
    for value in values.values():
        assert str(value) in subject + body


def test_unknown_locale_falls_back_to_english() -> None:
    assert mail.compose("reset", "de", link="L") == mail.compose("reset", "en", link="L")


# --- Real calls read back from the source code -----------------------------------------------


def _keywords(call: ast.Call, ignore: frozenset[str] = frozenset()) -> frozenset[str]:
    assert all(k.arg is not None for k in call.keywords), "argument **dict inattendu"
    return frozenset(k.arg for k in call.keywords if k.arg is not None and k.arg not in ignore)


def _is_call_to(call: ast.Call, module: str, name: str, local: bool) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr == name and isinstance(func.value, ast.Name) and func.value.id == module
    return local and isinstance(func, ast.Name) and func.id == name


def real_calls() -> dict[str, set[frozenset[str]]]:
    """Template → argument sets found in the calls of `src/`.

    `alerts.notify` composes `f"alert_{kind}"` with space, org and link, and `alerts.notify_org`
    with org and link; the keyword arguments of each call are added (except `public_url`)."""
    found: dict[str, set[frozenset[str]]] = {}
    bases: dict[str, frozenset[str]] = {}  # alert function → arguments of its compose
    calls: list[tuple[str, str, frozenset[str]]] = []  # (function, alert kind, arguments)
    for path in sorted(SOURCE.rglob("*.py")):
        relative = path.relative_to(SOURCE).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        owner = {id(c): f.name for f in functions for c in ast.walk(f) if isinstance(c, ast.Call)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _is_call_to(node, "mail", "compose", relative == "services/mail.py"):
                kind = node.args[0]
                if isinstance(kind, ast.Constant) and isinstance(kind.value, str):
                    found.setdefault(kind.value, set()).add(_keywords(node))
                    continue
                # Computed templates: those of the alerts, which pass on **details.
                assert relative == "services/alerts.py", f"gabarit calculé dans {relative}"
                assert isinstance(kind, ast.JoinedStr)
                assert ast.unparse(kind) == "f'alert_{kind}'"
                bases[owner[id(node)]] = frozenset(k.arg for k in node.keywords if k.arg)
                continue
            for function in ("notify", "notify_org"):
                if _is_call_to(node, "alerts", function, relative == "services/alerts.py"):
                    named = {k.arg: k.value for k in node.keywords}
                    alert = node.args[3] if len(node.args) > 3 else named["kind"]
                    assert isinstance(alert, ast.Constant) and isinstance(alert.value, str)
                    ignore = frozenset({"public_url", "kind"})
                    calls.append((function, alert.value, _keywords(node, ignore)))
    assert {"notify", "notify_org"} <= set(bases), "compose introuvable dans les alertes"
    for function, alert_kind, values in calls:
        found.setdefault(f"alert_{alert_kind}", set()).add(bases[function] | values)
    return found


def test_real_calls_match_the_callers_table() -> None:
    """The `CALLERS` table reflects the code: an argument renamed by a caller, or a new call
    with other arguments, fails here before breaking a send."""
    expected = {kind: {frozenset(values)} for kind, values in CALLERS.items()}
    assert real_calls() == expected


def test_values_follow_the_recipients_language() -> None:
    """An interface text (`Label`) or a function of the language, in an alert e-mail: each
    recipient reads it in their language (here, the Git error of a lasting sync failure)."""
    from synaptane.git.repo import explain
    from synaptane.services.git_sync import alert_text

    error = alert_text(explain("fetch", "fatal: unable to access: Could not resolve host: h"), 7)
    french = mail.compose(
        "alert_sync_error", "fr", space="Infra", org="Acme", link=LINK, error=error
    )
    english = mail.compose(
        "alert_sync_error", "en", space="Infra", org="Acme", link=LINK, error=error
    )
    assert "Hôte introuvable : vérifiez l'adresse du dépôt." in french[1]
    assert "(erreur passagère depuis 7 h)" in french[1]
    assert "Host not found: check the repository address." in english[1]
    assert "(transient error for 7 h)" in english[1]
    assert "Could not resolve host: h" in french[1] and "Could not resolve host: h" in english[1]
