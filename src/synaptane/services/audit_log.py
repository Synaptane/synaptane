"""The audit log: who changed a role, a token or a setting, and when.

Organization admins read their organization's events and its spaces' (`/o/<org>/-/journal`);
super admins read the whole instance, events without an organization included (accounts,
tokens, single sign-on, email sending and capture), at `/admin/journal`.

`audit_events` also keeps the operations of every day (page changes, inbox notes, tidy-up
requests, costs of semantic indexing, review reminders): they have their own pages (Activity,
Tidy-up, AI) and stay out of this log (`NOISE`). Only the actions of `ACTIONS` are shown, and of
each event only the detail fields its action declares: an older event that stored more, or a
secret by mistake, shows nothing more.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import Select, select, tuple_
from sqlalchemy.orm import Session, joinedload

from synaptane.core.access import Action, require
from synaptane.core.errors import Label
from synaptane.services import Actor, routines, vocation
from synaptane.services.instance import ALERT_TITLES
from synaptane.storage.models import AuditEvent, Organization, Space, User

PAGE_SIZE = 50
EXPORT_LIMIT = 10_000
TEXT_LIMIT = 300  # characters of a free text (an alert's error) shown

FAMILIES = {
    "members": Label("Members and roles"),
    "access": Label("Tokens and applications"),
    "settings": Label("Settings"),
    "security": Label("Accounts and security"),
    "alerts": Label("Alerts"),
}
# Accounts, tokens and connections belong to no organization: the super admin's view only.
ORG_FAMILIES = ("members", "settings", "alerts")

Details = Mapping[str, object]
Show = Callable[[Details], list[Label]]


def _text(details: Details, key: str) -> str:
    value = details.get(key)
    return str(value)[:TEXT_LIMIT] if value not in (None, "", "None") else ""


def _value(key: str, template: str = "{value}") -> Show:
    def show(details: Details) -> list[Label]:
        value = _text(details, key)
        return [Label(template, value=value)] if value else []

    return show


def _iso_day(key: str, template: str) -> Show:
    """A day kept in ISO form, written out in the reader's language; older forms as they are."""

    def show(details: Details) -> list[Label]:
        value = _text(details, key)
        if not value:
            return []
        try:
            return [Label(template, value=date.fromisoformat(value))]
        except ValueError:
            return [Label(template, value=value)]

    return show


def _choice(key: str, labels: Mapping[str, Label]) -> Show:
    """A value among known ones; any other one shows nothing."""
    return lambda details: [labels[v]] if (v := _text(details, key)) in labels else []


def _flag(key: str, label: Label) -> Show:
    return _choice(key, {"True": label})


def _host(details: Details) -> list[Label]:
    """A webhook's host only: the rest of the address may hold its secret (Slack, Teams…)."""
    try:
        host = urlsplit(_text(details, "url")).hostname
    except ValueError:
        host = None
    return [Label("{value}", value=host)] if host else []


def _hosted(details: Details) -> list[Label]:
    """A repository created by Synaptane: host and path (github.com/acme/brain)."""
    host, path = _text(details, "host"), _text(details, "repository")
    return [Label("{value}", value=f"{host}/{path}")] if host and path else []


CHANGED = {
    "display_name": Label("Display name"),
    "email": Label("Email"),
    "locale": Label("Language"),
    "name": Label("Name"),
    "description": Label("Description"),
    "repository": Label("Repository"),
    "branch": Label("Branch"),
    "review_every": Label("Review period"),
    "enabled": Label("On or off"),
    "schedule": Label("Time"),
    "timezone": Label("Time zone"),
    "model": Label("Model"),
    "delay": Label("Delay"),
    "maintain": Label("Nightly maintenance"),
}


def _groups(details: Details) -> list[Label]:
    """The groups of an invitation: one per name (stored as a list, a name may hold a
    comma)."""
    value = details.get("groups")
    if isinstance(value, list):
        return [Label("Group {value}", value=str(name)[:TEXT_LIMIT]) for name in value if name]
    return _value("groups", "Groups: {value}")(details)  # stored as one text


def _changed(details: Details) -> list[Label]:
    return [CHANGED[name] for name in _text(details, "fields").split() if name in CHANGED]


def _budget(details: Details) -> list[Label]:
    value = _text(details, "budget")
    if not value:
        return []
    if value == "none":
        return [Label("No monthly budget")]
    return [Label("Monthly budget: {value} €", value=value)]


def _budget_alert(details: Details) -> list[Label]:
    level, spent, budget = (_text(details, key) for key in ("level", "spent", "budget"))
    if not (level and spent and budget):
        return []
    return [
        Label(
            "{level}% of the monthly budget ({spent} € of {budget} €)",
            level=level,
            spent=spent,
            budget=budget,
        )
    ]


def _prompt(details: Details) -> list[Label]:
    """A team prompt's name, from its file (`.synaptane/prompts/<name>.md`)."""
    name = _text(details, "file").rsplit("/", 1)[-1].removesuffix(".md")
    return [Label("{value}", value=name)] if name else []


def _template(names: Mapping[str, str]) -> Show:
    def show(details: Details) -> list[Label]:
        key = _text(details, "template")
        if not key:
            return []
        name = names.get(key)
        return [Label("Template: {value}", value=Label(name) if name else key)]

    return show


ROLES = {
    "admin": Label("Administrator"),
    "member": Label("Member"),
    "reader": Label("Reader"),
    "contributor": Label("Contributor"),
}


@dataclass(frozen=True)
class Field:
    show: Show
    subject: bool = False  # what the action is about (an account, a token…), not a detail


FIELDS = {
    # Subjects
    "account": Field(_value("username", "@{value}"), subject=True),
    "email": Field(_value("email"), subject=True),
    "token": Field(_value("name"), subject=True),
    "app": Field(_value("app"), subject=True),
    "sso": Field(_value("provider"), subject=True),
    "routine": Field(_value("routine"), subject=True),
    "webhook": Field(_host, subject=True),
    "slug": Field(_value("slug"), subject=True),  # of a deleted space or organization
    "mail_host": Field(_value("host"), subject=True),
    "capture_address": Field(_value("address"), subject=True),
    "prompt": Field(_prompt, subject=True),
    "group": Field(_value("group", "Group {value}"), subject=True),
    # Details
    "role": Field(_choice("role", ROLES)),
    "default_role": Field(_choice("role", {**ROLES, "none": Label("No access")})),
    "restricted": Field(
        _choice("restricted", {"True": Label("Restricted"), "False": Label("Not restricted")})
    ),
    "provider": Field(_value("provider", "Provider: {value}")),
    "model": Field(_value("model", "Model: {value}")),
    "key": Field(
        _choice("key", {"replaced": Label("API key replaced"), "removed": Label("API key removed")})
    ),
    "budget": Field(_budget),
    "read_only": Field(_flag("read_only", Label("Read only"))),
    "inbox_only": Field(_flag("inbox_only", Label("Reading and inbox only"))),
    "scoped": Field(_flag("scoped", Label("Chosen spaces only"))),
    "enabled": Field(_choice("enabled", {"True": Label("On"), "False": Label("Off")})),
    "superadmin": Field(_flag("superadmin", Label("Super admin"))),
    "credentials": Field(_choice("kind", {"https": Label("HTTPS token"), "ssh": Label("SSH key")})),
    "changed": Field(_changed),
    "forge": Field(_choice("provider", {"github": Label("GitHub"), "gitlab": Label("GitLab")})),
    "hosted_repository": Field(_hosted),
    "repository_access": Field(
        _choice(
            "access",
            {
                "ssh": Label("SSH deploy key"),
                "https": Label("Project access token"),
                "none": Label("Access to enter by hand"),
            },
        )
    ),
    "auto_create": Field(_flag("auto_create", Label("Creates accounts"))),
    "domains": Field(_value("domains", "Allowed domains: {value}")),
    "vocation_template": Field(_template({k: t.label for k, t in vocation.TEMPLATES.items()})),
    "routine_template": Field(_template({k: t.name for k, t in routines.TEMPLATES.items()})),
    "budget_alert": Field(_budget_alert),
    "error": Field(_value("error")),
    "reason": Field(_value("reason")),
    "status": Field(_value("status")),
    "removed": Field(_flag("removed", Label("Text removed"))),
    "previous_name": Field(_value("previous", "Formerly: {value}")),
    # The groups an invitation makes the person join (their names at the time).
    "invited_groups": Field(_groups),
    "date": Field(_iso_day("date", "Expiry date: {value}")),  # of a Git token
    # Capture by email: the less safe options, when they are on.
    "capture_spf": Field(_flag("spf", Label("Internal domains accepted on SPF alone"))),
    "capture_unnamed": Field(
        _flag("unnamed", Label("Results without a server identifier accepted (Microsoft 365)"))
    ),
}

# Author of an event without an account (`actor_id` empty).
SYSTEM, SUBJECT, VISITOR = "system", "subject", "visitor"


@dataclass(frozen=True)
class Kind:
    family: str
    title: Label
    fields: tuple[str, ...] = ()
    # SUBJECT: done by the account named in `username` itself, without a session (link of an
    # email, recovery code, SSO); VISITOR: by someone not signed in.
    author: str = SYSTEM


_ALERT_FIELDS = ("routine", "webhook", "error", "reason", "status", "date")

ACTIONS: dict[str, Kind] = {
    # Members and roles
    "org.member_set": Kind("members", Label("Organization role set"), ("account", "role")),
    "org.member_removed": Kind("members", Label("Removed from the organization"), ("account",)),
    "space.member_set": Kind("members", Label("Space role set"), ("account", "role")),
    "space.member_removed": Kind("members", Label("Removed from the space"), ("account",)),
    "org.invitation_created": Kind(
        "members", Label("Invitation sent"), ("email", "role", "invited_groups")
    ),
    "org.member_invited": Kind(
        "members",
        Label("Existing account added by invitation"),
        ("account", "role", "invited_groups"),
    ),
    "org.invitation_revoked": Kind("members", Label("Invitation revoked"), ("email",)),
    "org.invitation_accepted": Kind(
        "members", Label("Invitation accepted"), ("account",), author=SUBJECT
    ),
    "group.created": Kind("members", Label("Group created"), ("group",)),
    "group.updated": Kind("members", Label("Group changed"), ("group", "changed", "previous_name")),
    "group.deleted": Kind("members", Label("Group deleted"), ("group",)),
    "group.member_added": Kind("members", Label("Added to a group"), ("group", "account")),
    "group.member_removed": Kind("members", Label("Removed from a group"), ("group", "account")),
    "group.space_role_set": Kind(
        "members", Label("Group role in the space set"), ("group", "role")
    ),
    "group.space_role_removed": Kind(
        "members", Label("Group role in the space removed"), ("group",)
    ),
    # Tokens and applications
    "token.created": Kind(
        "access", Label("API token created"), ("token", "read_only", "inbox_only", "scoped")
    ),
    "token.revoked": Kind("access", Label("API token revoked"), ("token",)),
    "oauth.approved": Kind(
        "access", Label("Application connected"), ("app", "read_only", "inbox_only", "scoped")
    ),
    "oauth.denied": Kind("access", Label("Application connection refused"), ("app",)),
    "oauth.revoked": Kind("access", Label("Application disconnected"), ("app",)),
    # Settings
    "org.created": Kind("settings", Label("Organization created")),
    "org.updated": Kind("settings", Label("Organization settings changed")),
    "org.default_space_role": Kind(
        "settings", Label("Default role in the spaces"), ("default_role",)
    ),
    "org.deleted": Kind("settings", Label("Organization deleted"), ("slug",)),
    "org.ai_configured": Kind(
        "settings", Label("AI settings changed"), ("provider", "model", "key", "budget")
    ),
    "org.embeddings_configured": Kind(
        "settings", Label("Semantic search settings changed"), ("provider", "model", "key")
    ),
    "org.webhook_created": Kind("settings", Label("Webhook added"), ("webhook",)),
    "org.webhook_deleted": Kind("settings", Label("Webhook deleted"), ("webhook",)),
    "org.webhook_reactivated": Kind("settings", Label("Webhook re-enabled"), ("webhook",)),
    "space.created": Kind("settings", Label("Space created")),
    "space.updated": Kind("settings", Label("Space settings changed"), ("changed",)),
    "space.restricted": Kind("settings", Label("Space restriction changed"), ("restricted",)),
    "space.deleted": Kind("settings", Label("Space deleted"), ("slug",)),
    "space.repository_created": Kind(
        "settings",
        Label("Git repository created by Synaptane"),
        ("forge", "hosted_repository", "repository_access"),
    ),
    "space.git_credentials_set": Kind("settings", Label("Git access set"), ("credentials",)),
    "space.git_credentials_cleared": Kind("settings", Label("Git access removed")),
    "space.git_host_keys_refreshed": Kind("settings", Label("GitHub SSH host keys updated")),
    "space.webhook_secret_rotated": Kind("settings", Label("Git webhook secret renewed")),
    "space.tidy_settings": Kind("settings", Label("Tidy-up settings changed"), ("changed",)),
    "space.night_report_test": Kind("settings", Label("Test night report sent")),
    "space.embeddings": Kind("settings", Label("Semantic search of the space"), ("enabled",)),
    "space.vocation_updated": Kind(
        "settings", Label("Space purpose changed"), ("vocation_template", "removed")
    ),
    "space.tidy_rules_updated": Kind("settings", Label("Tidy-up rules changed"), ("removed",)),
    "space.checks_updated": Kind("settings", Label("Team checks changed"), ("removed",)),
    "space.prompt_updated": Kind(
        "settings", Label("Assistant prompt changed"), ("prompt", "removed")
    ),
    "routine.created": Kind("settings", Label("Routine created"), ("routine", "routine_template")),
    "routine.updated": Kind("settings", Label("Routine changed"), ("routine",)),
    "routine.deleted": Kind("settings", Label("Routine deleted"), ("routine",)),
    "sso.provider_saved": Kind(
        "settings", Label("SSO provider saved"), ("sso", "auto_create", "domains")
    ),
    "sso.provider_deleted": Kind("settings", Label("SSO provider deleted"), ("sso",)),
    "instance.mail_configured": Kind("settings", Label("Email sending set up"), ("mail_host",)),
    "instance.mail_removed": Kind("settings", Label("Email sending removed")),
    "instance.capture_configured": Kind(
        "settings",
        Label("Capture by email set up"),
        ("capture_address", "mail_host", "capture_spf", "capture_unnamed"),
    ),
    "instance.capture_removed": Kind(
        "settings", Label("Capture by email removed"), ("capture_address",)
    ),
    "instance.backup_requested": Kind("settings", Label("Backup requested")),
    # Accounts and security
    "user.created": Kind("security", Label("Account created"), ("account", "superadmin")),
    "user.created_by_sso": Kind(
        "security", Label("Account created through SSO"), ("account", "sso"), author=SUBJECT
    ),
    "user.updated": Kind("security", Label("Account changed"), ("account", "changed")),
    "user.superadmin_granted": Kind("security", Label("Made super admin"), ("account",)),
    "user.superadmin_revoked": Kind("security", Label("Super admin rights removed"), ("account",)),
    "user.deactivated": Kind("security", Label("Account disabled"), ("account",)),
    "user.reactivated": Kind("security", Label("Account re-enabled"), ("account",)),
    "user.password_changed": Kind("security", Label("Password changed"), ("account",)),
    "user.password_reset_requested": Kind(
        "security", Label("Password reset requested"), ("account",), author=VISITOR
    ),
    "user.password_reset": Kind(
        "security", Label("Password reset by email"), ("account",), author=SUBJECT
    ),
    "user.email_verified": Kind(
        "security", Label("Email address verified"), ("account",), author=SUBJECT
    ),
    "user.unverified_email_released": Kind(
        "security", Label("Unverified email address released"), ("account",)
    ),
    "user.2fa_enabled": Kind("security", Label("Two-factor authentication turned on")),
    "user.2fa_disabled": Kind("security", Label("Two-factor authentication turned off")),
    "user.2fa_reset": Kind("security", Label("Two-factor authentication reset"), ("account",)),
    "user.recovery_codes_renewed": Kind("security", Label("Recovery codes renewed")),
    "user.recovery_code_used": Kind(
        "security", Label("Recovery code used"), ("account",), author=SUBJECT
    ),
    "user.session_closed": Kind("security", Label("Session closed")),
    "user.other_sessions_closed": Kind("security", Label("Other sessions closed")),
    "sso.linked": Kind("security", Label("SSO identity linked"), ("account", "sso"), SUBJECT),
    "sso.unlinked": Kind("security", Label("SSO identity unlinked"), ("sso",)),
    # Alerts
    **{
        f"alert.{kind}": Kind("alerts", title, _ALERT_FIELDS)
        for kind, title in ALERT_TITLES.items()
    },
    "org.ai_budget_alert": Kind("alerts", Label("AI budget threshold reached"), ("budget_alert",)),
    "space.local_changes_recovered": Kind(
        "alerts", Label("Local changes recovered during a sync"), ("reason",)
    ),
}

# Written to `audit_events`, but not part of the audit log: content and daily operations,
# personal preferences, costs. `tests/unit/test_audit_log.py` finds every action the code
# writes (literal, constant, or given to `vocation.Kind`, `content._after_write`…) and fails
# when one is neither here nor in ACTIONS.
NOISE = frozenset(
    {
        "content.written",
        "content.deleted",
        "content.moved",
        "inbox.deposited",
        "inbox.updated",
        "inbox.deleted",
        "inbox.moved",
        "inbox.change_dismissed",
        "page.reported",
        "page.report_verified",
        "page.report_dismissed",
        "page.report_withdrawn",
        "tidy.requested",
        "tidy.reverted",
        "tidy.pages_undone",
        "assistant_run.pages_undone",
        "routine.requested",
        "space.conflict_resolution_requested",
        "org.ai_test_requested",
        "ai.embedding_cost",
        "freshness.reminder",
        "user.tidy_notices",
        "user.routine_notices",
    }
)


@dataclass(frozen=True)
class Filters:
    family: str = ""  # "" every family of the view
    since: date | None = None
    until: date | None = None  # included
    org: str = ""  # instance view: "" every event, "-" those without organization, or a slug


def _day(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def parse_filters(family: str = "", since: str = "", until: str = "", org: str = "") -> Filters:
    """From the form: an unknown family or a malformed date filters nothing."""
    return Filters(
        family=family if family in FAMILIES else "",
        since=_day(since),
        until=_day(until),
        org=org.strip(),
    )


@dataclass(frozen=True)
class Entry:
    id: UUID
    at: datetime
    action: str
    title: Label
    family: str
    author: User | None
    author_label: Label | None  # without an account: "System", "Not signed in", "@username"
    space: Space | None
    organization: Organization | None
    subject: list[Label] = field(default_factory=list)
    details: list[Label] = field(default_factory=list)


@dataclass(frozen=True)
class Selection:
    entries: list[Entry]
    older: str  # cursor of the next page ("" if none)
    families: tuple[str, ...]  # those of the view, for the filter


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def cursor(entry: Entry) -> str:
    """Position after `entry`, newest first: its date (microseconds) and identifier."""
    return f"{(entry.at - _EPOCH) // timedelta(microseconds=1)}.{entry.id.hex}"


def parse_cursor(value: str) -> tuple[datetime, UUID] | None:
    micros, _, ident = value.partition(".")
    try:
        return _EPOCH + timedelta(microseconds=int(micros)), UUID(hex=ident)
    except (ValueError, OverflowError):
        return None


def actions_of(families: tuple[str, ...], family: str = "") -> list[str]:
    wanted = (family,) if family in families else families
    return sorted(action for action, kind in ACTIONS.items() if kind.family in wanted)


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _query(
    session: Session, org: Organization | None, filters: Filters, families: tuple[str, ...]
) -> Select[AuditEvent] | None:
    query = select(AuditEvent).where(AuditEvent.action.in_(actions_of(families, filters.family)))
    if org is not None:
        query = query.where(AuditEvent.org_id == org.id)
    elif filters.org == "-":
        query = query.where(AuditEvent.org_id.is_(None))
    elif filters.org:
        org_id = session.scalar(select(Organization.id).where(Organization.slug == filters.org))
        if org_id is None:
            return None
        query = query.where(AuditEvent.org_id == org_id)
    if filters.since:
        query = query.where(AuditEvent.at >= _midnight(filters.since))
    if filters.until and filters.until < date.max:  # the day after must exist
        query = query.where(AuditEvent.at < _midnight(filters.until) + timedelta(days=1))
    return query.order_by(AuditEvent.at.desc(), AuditEvent.id.desc())


def select_events(
    session: Session,
    actor: Actor,
    org: Organization | None,
    filters: Filters,
    before: str = "",
    limit: int = PAGE_SIZE,
) -> Selection:
    """A page of the log, newest first: of `org` (its admins), or of the whole instance (`org`
    None, super admins). `before`: the `older` cursor of the previous page."""
    if org is None:
        require(actor.access, Action.MANAGE_INSTANCE)
        families = tuple(FAMILIES)
    else:
        require(actor.access, Action.MANAGE_ORG, org_id=org.id)
        families = ORG_FAMILIES
    query = _query(session, org, filters, families)
    if query is None:
        return Selection([], "", families)
    position = parse_cursor(before) if before else None
    if position is not None:
        query = query.where(tuple_(AuditEvent.at, AuditEvent.id) < tuple_(*position))
    events = list(session.scalars(query.limit(limit + 1)))
    entries = _entries(session, events[:limit])
    older = cursor(entries[-1]) if len(events) > limit and entries else ""
    return Selection(entries, older, families)


def _entries(session: Session, events: list[AuditEvent]) -> list[Entry]:
    """Accounts, spaces and organizations loaded in one query each."""
    user_ids = {e.actor_id for e in events if e.actor_id}
    space_ids = {e.space_id for e in events if e.space_id}
    org_ids = {e.org_id for e in events if e.org_id}
    users = (
        {u.id: u for u in session.scalars(select(User).where(User.id.in_(user_ids)))}
        if user_ids
        else {}
    )
    spaces = (
        {
            s.id: s
            for s in session.scalars(
                select(Space).where(Space.id.in_(space_ids)).options(joinedload(Space.organization))
            )
        }
        if space_ids
        else {}
    )
    orgs = (
        {o.id: o for o in session.scalars(select(Organization).where(Organization.id.in_(org_ids)))}
        if org_ids
        else {}
    )
    return [
        describe(
            event,
            users.get(event.actor_id) if event.actor_id else None,
            spaces.get(event.space_id) if event.space_id else None,
            orgs.get(event.org_id) if event.org_id else None,
        )
        for event in events
    ]


def describe(
    event: AuditEvent,
    author: User | None,
    space: Space | None = None,
    organization: Organization | None = None,
) -> Entry:
    """What a line of the log shows of an event: only the fields its action declares."""
    kind = ACTIONS.get(event.action) or Kind("", Label("{value}", value=event.action))
    details: Details = event.details if isinstance(event.details, dict) else {}
    subject: list[Label] = []
    shown: list[Label] = []
    for name in kind.fields:
        found = FIELDS[name]
        (subject if found.subject else shown).extend(found.show(details))
    return Entry(
        id=event.id,
        at=event.at,
        action=event.action,
        title=kind.title,
        family=kind.family,
        author=author,
        author_label=None if author else _author_label(event, kind, details),
        space=space,
        organization=organization,
        subject=subject,
        details=shown,
    )


def _author_label(event: AuditEvent, kind: Kind, details: Details) -> Label:
    if event.actor_id is not None:
        return Label("Deleted account")
    if kind.author == SUBJECT and (username := _text(details, "username")):
        return Label("@{value}", value=username)
    if kind.author == VISITOR:
        return Label("Not signed in")
    return Label("System")
