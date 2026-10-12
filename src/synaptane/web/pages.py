"""HTML pages: sign-in, home, organizations, spaces, administration, account.

Forms follow the POST → redirect pattern, with a flash message. An organization's or a space's
actions live under `/-/`, which cannot be a slug.
"""

import base64
import hashlib
import json
import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlsplit
from uuid import UUID

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from sqlalchemy.orm import Session

from synaptane import version_label
from synaptane.ai import quota
from synaptane.config import Settings
from synaptane.core.access import Action, Limits, SpaceRole, can
from synaptane.core.errors import ErrorCode, Label, SynaptaneError, forbidden, invalid, not_found
from synaptane.core.names import without_email
from synaptane.core.security import SecretBox, same
from synaptane.git.credentials import ssh_host
from synaptane.git.repo import describe_error
from synaptane.services import (
    Actor,
    accounts,
    budget,
    changes,
    git_sync,
    load_actor,
    mail,
    organizations,
    spaces,
    vocation,
)
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import Organization, Space, User, WebSession
from synaptane.web.i18n import LOCALES, negotiate, plural, translator

SESSION_COOKIE = "synaptane_session"


FLASH_COOKIE = "synaptane_flash"


REVEAL_COOKIE = "synaptane_reveal"  # secret to show once (encrypted, see `reveal`)


LANG_COOKIE = "synaptane_lang"


templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


log = logging.getLogger("synaptane.web")


def _static_version() -> str:
    """Hash of the static files: changes with every update, hence a long cache."""
    digest = hashlib.sha256()
    for path in sorted((Path(__file__).parent / "static").rglob("*")):
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


templates.env.globals["static_version"] = _static_version()
# Running version and commit, at the foot of the administration: know what is running without
# opening a terminal on the server.
templates.env.globals["synaptane_version"] = version_label()


# Labels (translated in templates by `_`).
templates.env.globals["provider_labels"] = {
    "anthropic": "Anthropic (Claude)",
    "openai": "OpenAI",
    "mistral": "Mistral",
    "ollama": "Ollama",
    "openai_compatible": "OpenAI-compatible",
    "claude_code": "Claude subscription (Claude Code)",
    "codex": "ChatGPT subscription (Codex)",
}


templates.env.globals["embedding_labels"] = {
    "openai": "OpenAI",
    "gemini": "Google Gemini",
    "mistral": "Mistral",
    "voyage": "Voyage AI",
    "ollama": "Ollama",
    "openai_compatible": "OpenAI-compatible",
}


EVENT_LABELS = {
    "inbox.item_added": "Inbox drop",
    "content.changed": "Content changed",
    "tidy.completed": "Tidy-up finished",
    "tidy.budget": "AI budget (80% or 100%)",
    "space.alert": "Space alert (tidy-up, sync)",
    "sync.conflict": "Git conflict",
    "routine.done": "Routine finished",
}


OPERATION_LABELS = {
    "créé": "Page created",
    "modifié": "Page edited",
    "supprimé": "Page deleted",
    "déplacé": "Page moved",
    "pièce jointe rangée": "Attachment filed",
    "rangé": "Item tidied",
    "laissé": "Item left in the inbox",
    "réduit": "Page heavily shortened",
    "non traité": "Item not handled, retried on the next run",
    "fusionné": "Page merged with a change made during the tidy-up",
    "conflit": "Page changed during the tidy-up: the human version was kept",
    "attention": "To verify",
}


@pass_context
def _event_label(context: Any, event: str) -> str:
    return str(context["_"](EVENT_LABELS.get(event, event)))


@pass_context
def _operation_label(context: Any, operation: str) -> str:
    return str(context["_"](OPERATION_LABELS.get(operation, operation)))


# Commit subjects written by Synaptane: labels translated on display. A message typed by someone
# stays as is. Subjects are in English, and in French in histories written before the code was
# translated.
_SUBJECTS = [
    (re.compile(r"^Inbox: edit “(?P<title>.*)”$"), "Note edited: {title}"),
    (re.compile(r"^Inbox: delete “(?P<title>.*)”$"), "Note deleted: {title}"),
    (
        re.compile(r"^Inbox: “(?P<title>.*)” moved to (?P<space>\S+)$"),
        "Note moved to {space}: {title}",
    ),
    (
        re.compile(r"^Inbox: “(?P<title>.*)” from another space$"),
        "Note moved from another space: {title}",
    ),
    (re.compile(r"^Inbox: (?P<title>.*)$"), "Note dropped: {title}"),
    (re.compile(r"^Synaptane: initialize space$"), "Space initialization"),
    (re.compile(r"^Synaptane: remove old inbox format"), "Old inbox format removed"),
    (re.compile(r"^Synaptane: recover local changes"), "Local changes carried over"),
    (re.compile(r"^Synaptane: merge remote changes$"), "Remote repository merged"),
    (re.compile(r"^Inbox : modification de « (?P<title>.*) »$"), "Note edited: {title}"),
    (re.compile(r"^Inbox : suppression de « (?P<title>.*) »$"), "Note deleted: {title}"),
    (
        re.compile(r"^Inbox : « (?P<title>.*) » déplacé vers (?P<space>\S+)$"),
        "Note moved to {space}: {title}",
    ),
    (
        re.compile(r"^Inbox : « (?P<title>.*) » venu d'un autre espace$"),
        "Note moved from another space: {title}",
    ),
    (re.compile(r"^Inbox : (?P<title>.*)$"), "Note dropped: {title}"),
    (re.compile(r"^Synaptane : initialisation de l'espace$"), "Space initialization"),
    (re.compile(r"^Synaptane : ancien format d'inbox retiré"), "Old inbox format removed"),
    (
        re.compile(r"^Synaptane : reprise des modifications locales"),
        "Local changes carried over",
    ),
    (
        re.compile(r"^Synaptane : intégration des modifications distantes$"),
        "Remote repository merged",
    ),
    (re.compile(r"^(?:Tidy-up: |Rangement : )"), "Tidy-up run"),
]


def _plain_markdown(text: str) -> str:
    """Markdown line readable as an excerpt: without heading hashes, bullets, emphasis, code or
    link syntax (the link text stays)."""
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text)
    text = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?", "", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(\*\*|__|`)", "", text)
    return text


@pass_context
def _commit_label(context: Any, subject: str) -> str:
    for pattern, label in _SUBJECTS:
        if match := pattern.match(subject):
            return str(context["_"](label, **match.groupdict()))
    return subject


@pass_context
def _amount(context: Any, value: object) -> str:
    """Amount for an input: "10,5" in French, "10.5" in English (useless zeros removed)."""
    text = format(Decimal(str(value)).normalize(), "f")
    return text.replace(".", ",") if context["locale"] == "fr" else text


@pass_context
def _euros(context: Any, amount: object) -> str:
    """Amount in euros: "0,42 €" in French, "€0.42" in English."""
    value = float(str(amount))
    digits = 2 if value >= 0.01 or value == 0 else 4
    if context["locale"] == "fr":
        return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",") + " €"
    return f"€{value:,.{digits}f}"


@pass_context
def _filesize(context: Any, size: int) -> str:
    """Human-readable size, with the language's units (Kio / KiB)."""
    units = ("o", "Kio", "Mio", "Gio") if context["locale"] == "fr" else ("B", "KiB", "MiB", "GiB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == units[0] else f"{value:.1f} {unit}"
        value /= 1024
    return str(size)


templates.env.filters["event_label"] = _event_label


templates.env.filters["operation_label"] = _operation_label


templates.env.filters["filesize"] = _filesize


templates.env.filters["euros"] = _euros


templates.env.filters["amount"] = _amount


templates.env.globals["pl"] = pass_context(
    lambda c, n, one, many: plural(c["locale"], n, one, many)
)


@pass_context
def _period_label(context: Any, value: str) -> str:
    """Human-readable verification cadence: "every 6 months", "never"…"""
    from synaptane.services.freshness import NEVER, parse_period

    translate = translator(context["locale"])
    period = parse_period(value)
    if period is None or period == NEVER:
        return translate("never")
    assert not isinstance(period, str)
    one, many = {
        "d": ("every day", "every {n} days"),
        "w": ("every week", "every {n} weeks"),
        "m": ("every month", "every {n} months"),
        "y": ("every year", "every {n} years"),
    }[period.unit]
    return translate(many, n=period.count) if period.count > 1 else translate(one)


templates.env.globals["period_label"] = _period_label


# A text computed by the services (`Label`) is translated; any other (a note's title typed by
# someone) is shown as it is.
templates.env.filters["tr"] = pass_context(
    lambda c, value: c["_"](value) if isinstance(value, Label) else value
)


@pass_context
def _log_text(context: Any, text: object) -> str:
    """A text of a tidy-up log (operation, summary): Synaptane's own notes and the titles of
    page changes translated, the rest (the agent's words, a note's title) as it is."""
    translate = context["_"]
    return changes.localize_title(budget.localize_notes(str(text), translate), translate)


templates.env.globals["log_text"] = _log_text


templates.env.globals["git_error"] = pass_context(lambda c, text: describe_error(str(text), c["_"]))


templates.env.filters["commit_label"] = _commit_label


templates.env.filters["plain"] = lambda text: _plain_markdown(str(text))


# "Alice <alice@example.com>" → "Alice": the address need not be shown to every reader.
templates.env.filters["names"] = lambda value: without_email(str(value))


templates.env.filters["pathurl"] = lambda path: quote(str(path), safe="/")

# A file's type for its icon (tree, lists): recognized at a glance.
FILE_TYPES = {
    "md": (".md", ".markdown", ".mdx"),
    "html": (".html", ".htm", ".xhtml"),
    "pdf": (".pdf",),
    "image": (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".bmp", ".ico", ".heic"),
    "doc": (".doc", ".docx", ".odt", ".rtf", ".pages"),
    "sheet": (".xls", ".xlsx", ".ods", ".csv", ".tsv", ".numbers"),
    "slides": (".ppt", ".pptx", ".odp", ".key"),
    "code": (
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".xml",
        ".ini",
        ".cfg",
        ".conf",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".sh",
        ".bash",
        ".sql",
        ".css",
        ".scss",
        ".go",
        ".rs",
        ".java",
        ".php",
        ".rb",
        ".c",
        ".h",
        ".cpp",
        ".tf",
        ".hcl",
        ".dockerfile",
    ),
    "text": (".txt", ".log", ".text", ".rst", ".adoc"),
    "archive": (".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".bz2", ".xz"),
    "media": (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".mp4", ".mov", ".webm", ".mkv", ".avi"),
}
_TYPE_OF = {suffix: kind for kind, suffixes in FILE_TYPES.items() for suffix in suffixes}


def _file_type(name: str) -> str:
    """md, html, pdf, image, doc, sheet, slides, code, text, archive, media or file."""
    lowered = str(name).lower()
    if lowered.rsplit("/", 1)[-1] in ("dockerfile", "makefile", ".gitignore"):
        return "code"
    dot = lowered.rfind(".")
    return _TYPE_OF.get(lowered[dot:], "file") if dot > lowered.rfind("/") else "file"


templates.env.filters["filetype"] = _file_type
templates.env.filters["quota_summary"] = pass_context(
    lambda c, value: quota.summary(value, c["locale"])
)


class LoginRequired(Exception):
    pass


class PasswordChangeRequired(Exception):
    """Password chosen by an admin: to change before going anywhere else."""


PASSWORD_CHANGE_PATHS = ("/account/security", "/account/password", "/logout")


@dataclass
class Page:
    """Context of a web request: database, signed-in account, session and language."""

    request: Request
    db: Session
    actor: Actor | None
    web_session: WebSession | None
    locale: str

    @property
    def settings(self) -> Settings:
        settings: Settings = self.request.app.state.settings
        return settings

    @property
    def box(self) -> SecretBox:
        box: SecretBox = self.request.app.state.secret_box
        return box

    @property
    def workspace(self) -> Workspace:
        workspace: Workspace = self.request.app.state.workspace
        return workspace

    @property
    def database(self) -> Database:
        database: Database = self.request.app.state.database
        return database

    @staticmethod
    def can_contribute(actor: Actor, space: Space) -> bool:
        return can(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)

    def user_actor(self) -> Actor:
        if self.actor is None:
            raise LoginRequired
        if self.actor.user.must_change_password and self.request.url.path not in (
            PASSWORD_CHANGE_PATHS
        ):
            raise PasswordChangeRequired
        return self.actor

    def check_csrf(self, token: str) -> Actor:
        actor = self.user_actor()
        if self.web_session is None or not same(token, self.web_session.csrf_token):
            raise forbidden("This form has expired: reload the page and try again.")
        return actor

    def translate(self, text: str, **values: object) -> str:
        return translator(self.locale)(text, **values)

    def space_files(self, actor: Actor, space: Space) -> Any:
        """Files of a space's wiki, computed once per request (the page and the left sidebar use
        them). Raises like `content.list_files`."""
        from synaptane.services import content

        cache: dict[object, Any] = self.__dict__.setdefault("_files_cache", {})
        if space.id not in cache:
            cache[space.id] = content.list_files(actor, self.workspace, space)
        return cache[space.id]

    def _space_tree(self, space: Space) -> Any:
        """Wiki tree of the current space (None if it is not synced yet)."""
        from synaptane.services import content

        if self.actor is None:
            return None
        try:
            return content.build_tree(self.space_files(self.user_actor(), space))
        except SynaptaneError:
            return None

    def inbox_counts(self, space: Space) -> dict[str, int] | None:
        """Pending items in a space's inbox (the "Inbox · N" line of the left sidebar, home page
        cards). None if the space is not synced yet.

        Changes are cached as long as the repository head does not move: the cost per page
        view is limited to reading the notes."""
        from synaptane.services import deposits

        cache: dict[object, dict[str, int] | None] = self.__dict__.setdefault("_inbox_cache", {})
        if self.actor is None:
            return None
        if space.id not in cache:
            try:
                cache[space.id] = deposits.counts(
                    deposits.inbox(self.db, self.actor, self.workspace, space)
                )
            except SynaptaneError:
                cache[space.id] = None
        return cache[space.id]

    def render(self, template: str, status_code: int = 200, **context: object) -> HTMLResponse:
        space = context.get("space")
        if isinstance(space, Space) and "space_tree" not in context:
            context["space_tree"] = self._space_tree(space)
        if isinstance(space, Space) and "inbox_counts" not in context:
            context["inbox_counts"] = self.inbox_counts(space)
        translate = translator(self.locale)
        revealed = self._revealed()
        flash = None
        if raw := self.request.cookies.get(FLASH_COOKIE):
            try:
                flash = json.loads(raw)
            except ValueError:
                flash = None
        response = templates.TemplateResponse(
            self.request,
            template,
            {
                "_": translate,
                "locale": self.locale,
                "locales": LOCALES,
                "actor": self.actor,
                "user": self.actor.user if self.actor else None,
                "csrf": self.web_session.csrf_token if self.web_session else "",
                "flash": flash,
                "nav": (nav := self.navigation()),
                # "Drop" button: only if the account contributes to at least one space.
                "can_deposit": self.actor is not None
                and any(
                    spaces.role_in(self.actor, s) is SpaceRole.CONTRIBUTOR
                    for _, org_spaces in nav
                    for s in org_spaces
                ),
                "can": can,
                "Action": Action,
                "revealed": revealed,
                **context,
            },
            status_code=status_code,
        )
        if flash is not None:
            response.delete_cookie(FLASH_COOKIE)
        if revealed is not None:
            response.delete_cookie(REVEAL_COOKIE, path=self.request.url.path)
        return response

    def _revealed(self) -> str | None:
        raw = self.request.cookies.get(REVEAL_COOKIE)
        if not raw or self.actor is None:
            return None
        try:
            context = _reveal_context(self.request.url.path, self.actor.user.id)
            return self.box.decrypt(base64.urlsafe_b64decode(raw), context)
        except Exception:  # noqa: BLE001 — cookie expired, from another account or forged
            return None

    def navigation(self) -> list[tuple[Organization, list[Space]]]:
        """Visible organizations and spaces (left sidebar, home page), once per request."""
        if "_nav_cache" not in self.__dict__:
            self.__dict__["_nav_cache"] = self._navigation()
        nav: list[tuple[Organization, list[Space]]] = self.__dict__["_nav_cache"]
        return nav

    def _navigation(self) -> list[tuple[Organization, list[Space]]]:
        if self.actor is None:
            return []
        visible = spaces.list_all_visible_spaces(self.db, self.actor)
        tree: dict[object, tuple[Organization, list[Space]]] = {
            org.id: (org, []) for org in organizations.list_organizations(self.db, self.actor)
        }
        for space in visible:
            tree.setdefault(space.org_id, (space.organization, []))[1].append(space)
        return list(tree.values())


def visitor_locale(request: Request, account: str | None = None) -> str:
    """A page's language: the signed-in account's, otherwise the language cookie, otherwise the
    browser's (`Accept-Language`), otherwise the instance's default language."""
    default = request.app.state.settings.default_locale
    locale = account or request.cookies.get(LANG_COOKIE)
    locale = locale or negotiate(request.headers.get("accept-language"), default)
    return locale if locale in LOCALES else default


def _web_session(request: Request) -> Iterator[Page]:
    database: Database = request.app.state.database
    with database.session() as db:
        actor = web_session = None
        if token := request.cookies.get(SESSION_COOKIE):
            resolved = accounts.resolve_web_session(db, token)
            if resolved is not None:
                user, web_session = resolved
                actor = load_actor(db, user)
        locale = visitor_locale(request, actor.user.locale if actor is not None else None)
        yield Page(request, db, actor, web_session, locale)


# scope="function": the transaction is committed BEFORE the response is sent. Otherwise a client
# that follows a redirect right away (sign-in, save) may read the previous state.
WebPage = Annotated[Page, Depends(_web_session, scope="function")]


Csrf = Annotated[str, Form(alias="csrf")]


def redirect(
    url: str, page: Page, message: str | None = None, kind: str = "ok", **params: object
) -> Response:
    """303 redirect, with a message shown once on the landing page."""
    return linked_redirect(url, page, message, None, kind=kind, params=params)


def linked_redirect(
    url: str,
    page: Page,
    message: str | None,
    link: tuple[str, str] | None,
    *,
    kind: str = "ok",
    params: Mapping[str, object] | None = None,
) -> Response:
    """Like `redirect`, with a link added to the message: (local address, label), for example
    "See the note" after a drop made from another page."""
    response = RedirectResponse(url, status_code=303)
    if message:
        translate = translator(page.locale)
        flash: dict[str, object] = {"kind": kind, "text": translate(message, **(params or {}))}
        if link is not None and safe_next(link[0]) == link[0]:
            flash["link"] = {"url": link[0], "label": translate(link[1])}
        response.set_cookie(
            FLASH_COOKIE,
            json.dumps(flash),
            max_age=60,
            httponly=True,
            samesite="lax",
            secure=page.settings.secure_cookies,
        )
    return response


def _reveal_context(path: str, user_id: object) -> str:
    return f"reveal:{path}:{user_id}"


def reveal(url: str, page: Page, actor: Actor, message: str, secret: str) -> Response:
    """Redirects to `url`, which will show `secret` only once.

    The secret travels in a cookie encrypted with the master key, bound to this page and this
    account, valid for 2 minutes and deleted once displayed. Reloading the page recreates
    nothing, and a forged link cannot make a fake secret appear.
    """
    response = redirect(url, page, message)
    blob = page.box.encrypt(secret, _reveal_context(url, actor.user.id))
    response.set_cookie(
        REVEAL_COOKIE,
        base64.urlsafe_b64encode(blob).decode(),
        max_age=120,
        path=url,
        httponly=True,
        samesite="lax",
        secure=page.settings.secure_cookies,
    )
    return response


def _confirmed(typed: str, slug: str) -> None:
    """Irreversible deletion: the identifier must be typed again (also checked without JS)."""
    if typed.strip() != slug:
        raise invalid("To confirm, type exactly “{slug}”.", slug=slug)


def failure(url: str, page: Page, error: SynaptaneError) -> Response:
    page.db.rollback()  # nothing partial is committed
    return redirect(url, page, error.template, kind="error", **error.params)


def safe_next(value: str | None) -> str:
    """Accepts only a local path (no open redirect)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def _same_origin(request: Request, settings: Settings) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True  # old browsers: the SameSite=Lax cookie already protects
    allowed = {settings.public_url, f"{request.url.scheme}://{request.url.netloc}"}
    parts = urlsplit(origin)
    return f"{parts.scheme}://{parts.netloc}" in allowed


def sign_in(page: Page, user: User, target: str, message: str | None = None) -> Response:
    """Opens a web session and redirects (sign-in, accepted invitation, password reset)."""
    settings = page.settings
    token, _ = accounts.open_web_session(page.db, user, settings.session_days)
    response = redirect(target, page, message) if message else RedirectResponse(target, 303)
    if user.locale in LOCALES:  # error and sign-in pages in the account's language
        response.set_cookie(LANG_COOKIE, user.locale, max_age=365 * 86400, samesite="lax")
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_days * 86400,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
    )
    return response


def org_url(org: Organization) -> str:
    return f"/o/{org.slug}"


def space_settings_url(space: Space, section: str = "settings") -> str:
    """A space's settings page (`settings`: general, `git`, `members`…)."""
    return f"/o/{space.address}/-/{section}"


def find_space(page: Page, actor: Actor, org_slug: str, space_slug: str) -> Space:
    org = organizations.get_organization(page.db, actor, org_slug)
    return spaces.get_space(page.db, actor, org, space_slug)


def role_label(actor: Actor, space: Space) -> str:
    """Effective role, with its source (shown in the space's settings)."""
    if actor.access.is_superadmin:
        return "Super admin"
    if actor.access.is_org_admin(space.org_id):
        return "Organization administrator"
    role = spaces.role_in(actor, space)
    return "Contributor" if role is SpaceRole.CONTRIBUTOR else "Reader"


def render_space_settings(
    page: Page,
    actor: Actor,
    space: Space,
    section: str,
    template: str,
    status_code: int = 200,
    **extra: object,
) -> HTMLResponse:
    manage = can(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    if section in ("general", "vocation", "tidy", "semantic") and not manage:
        raise not_found("Page not found.")
    return page.render(
        template,
        status_code=status_code,
        org=space.organization,
        space=space,
        section=section,
        manage=manage,
        role_label=page.translate(role_label(actor, space)),
        contribute=can(
            actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id
        ),
        **extra,
    )


def _render_git(
    page: Page,
    actor: Actor,
    space: Space,
    scanned_keys: str = "",
    fingerprints: list[str] | None = None,
) -> HTMLResponse:
    manage = can(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    org = space.organization
    return render_space_settings(
        page,
        actor,
        space,
        "git",
        "space-git.html",
        credential_kind=git_sync.credential_kind(page.db, page.box, space) if manage else None,
        has_webhook_secret=bool(git_sync.webhook_secret(page.db, actor, page.box, space))
        if manage
        else False,
        webhook_url=f"{page.settings.public_url}/hooks/git/{org.slug}/{space.slug}",
        ssh_remote=ssh_host(space.repo_url or "") is not None,
        sync_activity=git_sync.sync_activity(page.db, space.id),
        sync_version=git_sync.sync_version(page.db, space),
        vocation_pending=manage and vocation.pending(page.workspace, space),
        scanned_keys=scanned_keys,
        fingerprints=fingerprints or [],
    )


def send_email_check(page: "Page", user: User) -> bool:
    """Sends the verification link to the account's address. False if sending is impossible."""
    if not user.email or not mail.configured(page.db):
        return False
    token = accounts.start_email_check(page.db, user)
    page.db.commit()
    subject, body = mail.compose(
        "verify_email",
        user.locale,
        account=user.username,
        link=f"{page.settings.public_url}/verify-email/{token}",
    )
    try:
        mail.send(page.db, page.box, user.email, subject, body)
    except mail.MailError as error:
        log.warning("Verification link not sent to %s: %s", user.email, error)
        return False
    return True


def member_spaces(page: "Page", actor: Actor) -> list[Space]:
    """Spaces the account is a member of (or organization admin of), without the super-admin's
    powers: those a restricted token or an MCP connection can reach."""
    real = load_actor(page.db, actor.user, limits=Limits(members_only=True))
    return spaces.list_all_visible_spaces(page.db, real)


def chosen_limits(
    access: str, scope: str, space: list[str] | None
) -> tuple[bool, list[UUID] | None]:
    """Choice from the `access_limits` form: (read-only, spaces or None for all). A checked space
    means "only these spaces", even if "all" stayed selected: never grant more than what the
    person believed they limited."""
    if scope != "some" and not space:
        return access == "read", None
    try:
        return access == "read", [UUID(value) for value in space or []]
    except ValueError:
        raise invalid("Invalid space.") from None


def login_redirect(request: Request) -> Response:
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target)}", status_code=303)


ERROR_STATUS = {
    ErrorCode.INVALID: 400,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.UNAVAILABLE: 503,
    ErrorCode.SECRET: 422,
    ErrorCode.TOO_MANY: 429,
}
