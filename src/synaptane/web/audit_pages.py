"""The audit log: an organization's (`/o/<org>/-/journal`, its admins) and the instance's
(`/admin/journal`, super admins), with a CSV export of the same selection.

What a line shows is decided by `services.audit_log`; this module only renders it."""

import csv
import io
from datetime import UTC, datetime
from urllib.parse import urlencode

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

from synaptane.core.access import Action, can, require
from synaptane.core.errors import Label, not_found
from synaptane.services import Actor, audit_log, organizations
from synaptane.services.audit_log import Entry, Filters, Selection
from synaptane.storage.models import Organization
from synaptane.web.i18n import Translate
from synaptane.web.org_pages import _org_page
from synaptane.web.pages import Page, WebPage, org_url

router = APIRouter()

COLUMNS = (
    Label("Date (UTC)"),
    Label("Author"),
    Label("Action"),
    Label("Code"),
    Label("Place"),
    Label("Target"),
    Label("Details"),
)


def _org_admin(page: Page, actor: Actor, org_slug: str) -> Organization:
    """Like the other sections of an organization: missing for its plain members."""
    org = organizations.get_organization(page.db, actor, org_slug)
    if not can(actor.access, Action.MANAGE_ORG, org_id=org.id):
        raise not_found("Page not found.")
    return org


def _query_string(filters: Filters, **extra: str) -> str:
    """The filters of the page, for its links (older entries, export)."""
    values = {
        "family": filters.family,
        "since": filters.since.isoformat() if filters.since else "",
        "until": filters.until.isoformat() if filters.until else "",
        "org": filters.org,
        **extra,
    }
    return urlencode({k: v for k, v in values.items() if v})


def _links(base: str, filters: Filters, selection: Selection) -> dict[str, str]:
    query = _query_string(filters)
    return {
        "base": base,
        "newest_url": f"{base}?{query}" if query else base,
        "older_url": f"{base}?{_query_string(filters, before=selection.older)}"
        if selection.older
        else "",
        "export_url": f"{base}.csv" + (f"?{query}" if query else ""),
    }


@router.get("/o/{org_slug}/-/journal", response_class=HTMLResponse)
def organization_journal_page(
    page: WebPage,
    org_slug: str,
    family: str = "",
    since: str = "",
    until: str = "",
    before: str = "",
) -> Response:
    actor = page.user_actor()
    org = _org_admin(page, actor, org_slug)
    filters = audit_log.parse_filters(family, since, until)
    selection = audit_log.select_events(page.db, actor, org, filters, before)
    return _org_page(
        page,
        actor,
        org,
        "journal",
        "org-journal.html",
        log=selection,
        filters=filters,
        families=audit_log.FAMILIES,
        first_page=not before,
        instance_view=False,
        links=_links(f"{org_url(org)}/-/journal", filters, selection),
    )


@router.get("/o/{org_slug}/-/journal.csv")
def organization_journal_export(
    page: WebPage, org_slug: str, family: str = "", since: str = "", until: str = ""
) -> Response:
    actor = page.user_actor()
    org = _org_admin(page, actor, org_slug)
    filters = audit_log.parse_filters(family, since, until)
    selection = audit_log.select_events(page.db, actor, org, filters, limit=audit_log.EXPORT_LIMIT)
    return _csv_response(page, selection, f"synaptane-audit-{org.slug}")


@router.get("/admin/journal", response_class=HTMLResponse)
def admin_journal_page(
    page: WebPage,
    family: str = "",
    since: str = "",
    until: str = "",
    org: str = "",
    before: str = "",
) -> Response:
    actor = page.user_actor()
    require(actor.access, Action.MANAGE_INSTANCE)
    filters = audit_log.parse_filters(family, since, until, org)
    selection = audit_log.select_events(page.db, actor, None, filters, before)
    return page.render(
        "admin-journal.html",
        section="journal",
        log=selection,
        filters=filters,
        families=audit_log.FAMILIES,
        first_page=not before,
        instance_view=True,
        orgs=organizations.list_organizations(page.db, actor),
        links=_links("/admin/journal", filters, selection),
    )


@router.get("/admin/journal.csv")
def admin_journal_export(
    page: WebPage, family: str = "", since: str = "", until: str = "", org: str = ""
) -> Response:
    actor = page.user_actor()
    require(actor.access, Action.MANAGE_INSTANCE)
    filters = audit_log.parse_filters(family, since, until, org)
    selection = audit_log.select_events(page.db, actor, None, filters, limit=audit_log.EXPORT_LIMIT)
    return _csv_response(page, selection, "synaptane-audit")


def _csv_response(page: Page, selection: Selection, name: str) -> Response:
    day = datetime.now(UTC).date().isoformat()
    return Response(
        to_csv(selection.entries, page.translate),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}-{day}.csv"'},
    )


def cell(value: str) -> str:
    """A spreadsheet must not read a value as a formula (`=…`, `+…`, `@…`): quoted."""
    return f"'{value}" if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def where(entry: Entry) -> str:
    if entry.space is not None:
        return entry.space.address
    return entry.organization.slug if entry.organization is not None else ""


def _plain(label: Label, translate: Translate) -> str:
    """An account without its "@" (which a spreadsheet would read as a formula)."""
    return str(label.params["value"]) if label.template == "@{value}" else translate(label)


def to_csv(entries: list[Entry], translate: Translate) -> str:
    """One line per entry, in the reader's language; the action code as well, for scripts.
    A leading BOM: spreadsheets then read the file as UTF-8."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([translate(column) for column in COLUMNS])
    for entry in entries:
        if entry.author is not None:
            author = entry.author.username
        else:
            author = _plain(entry.author_label or Label("System"), translate)
        writer.writerow(
            [
                cell(value)
                for value in (
                    entry.at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                    author,
                    translate(entry.title),
                    entry.action,
                    where(entry),
                    ", ".join(_plain(label, translate) for label in entry.subject),
                    " · ".join(translate(label) for label in entry.details),
                )
            ]
        )
    return "﻿" + out.getvalue()
