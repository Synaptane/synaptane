"""Capture by email, its settings (super admin): the mailbox the worker reads, the servers whose
authentication results are trusted, the answers to senders (`services.email_capture`)."""

from typing import Annotated

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, Response

from synaptane.core.access import Action, require
from synaptane.core.errors import Label, SynaptaneError
from synaptane.services import email_capture
from synaptane.web.pages import Csrf, WebPage, failure, redirect

router = APIRouter()
PAGE = "/admin/mail/capture"


@router.get(PAGE, response_class=HTMLResponse)
def capture_page(page: WebPage) -> Response:
    actor = page.user_actor()
    require(actor.access, Action.MANAGE_INSTANCE)
    return page.render(
        "admin-capture.html",
        section="mail",
        capture=email_capture.view(page.db, actor),
        allow_plain=page.settings.allow_insecure_imap,
        poll_pending=email_capture.pending(page.db),
    )


@router.post(PAGE)
def capture_save(
    page: WebPage,
    csrf: Csrf,
    host: Annotated[str, Form()],
    username: Annotated[str, Form()],
    address: Annotated[str, Form()],
    port: Annotated[int, Form()] = 993,
    security: Annotated[str, Form()] = "ssl",
    password: Annotated[str, Form()] = "",
    folder: Annotated[str, Form()] = "INBOX",
    authserv_ids: Annotated[str, Form()] = "",
    unnamed_results: Annotated[bool, Form()] = False,
    spf_domains: Annotated[str, Form()] = "",
    interval: Annotated[int, Form()] = email_capture.DEFAULT_INTERVAL,
    replies: Annotated[str, Form()] = "refusals",
) -> Response:
    actor = page.check_csrf(csrf)
    try:
        email_capture.configure(
            page.db,
            actor,
            page.box,
            host=host,
            port=port,
            security=security,
            username=username,
            password=password or None,
            folder=folder,
            address=address,
            authserv_ids=authserv_ids,
            unnamed_results=unnamed_results,
            spf_domains=spf_domains,
            interval=interval,
            replies=replies,
            allow_plain=page.settings.allow_insecure_imap,
        )
    except SynaptaneError as error:
        return failure(PAGE, page, error)
    return redirect(PAGE, page, "Capture by email saved.")


@router.post(f"{PAGE}/test")
def capture_test(page: WebPage, csrf: Csrf) -> Response:
    """Signs in to the mailbox from the web server, now (15 seconds at most)."""
    actor = page.check_csrf(csrf)
    try:
        result = email_capture.test(
            page.db, actor, page.box, allow_plain=page.settings.allow_insecure_imap
        )
    except email_capture.CaptureError as error:
        return redirect(PAGE, page, error.label.template, kind="error", **error.label.params)
    seen = ", ".join(f"{name} ({count})" for name, count in result.identifiers)
    if seen and result.unnamed:
        message = Label(
            "Connected: {count} emails waiting. Servers that authenticated the latest ones: "
            "{servers}; without identifier (Microsoft 365): {unnamed}."
        )
    elif seen:
        message = Label(
            "Connected: {count} emails waiting. Servers that authenticated the latest ones: "
            "{servers}."
        )
    elif result.unnamed:
        message = Label(
            "Connected: {count} emails waiting. The latest were authenticated by a server that "
            "gives no identifier (Microsoft 365): {unnamed}."
        )
    else:
        message = Label(
            "Connected: {count} emails waiting. None of the latest has an "
            "Authentication-Results header: senders cannot be authenticated."
        )
    return redirect(PAGE, page, message, count=result.waiting, servers=seen, unnamed=result.unnamed)


@router.post(f"{PAGE}/poll")
def capture_poll(page: WebPage, csrf: Csrf) -> Response:
    actor = page.check_csrf(csrf)
    email_capture.request(page.db, actor)
    return redirect(PAGE, page, "Poll requested: the worker reads the mailbox in a moment.")


@router.post(f"{PAGE}/delete")
def capture_delete(page: WebPage, csrf: Csrf) -> Response:
    actor = page.check_csrf(csrf)
    email_capture.remove(page.db, actor)
    return redirect(PAGE, page, "Capture by email turned off.")
