"""Sign-in and sign-out."""

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from synaptane.core.throttle import LoginThrottle
from synaptane.services import (
    accounts,
    sso,
)
from synaptane.web.i18n import LOCALES
from synaptane.web.pages import (
    LANG_COOKIE,
    SESSION_COOKIE,
    Csrf,
    WebPage,
    _same_origin,
    log,
    safe_next,
    sign_in,
)

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_form(page: WebPage, next: str = "/", lang: str | None = None) -> Response:
    if page.actor is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    if lang in LOCALES:
        page.locale = lang
    response = page.render(
        "login.html",
        next_url=safe_next(next),
        username="",
        error=None,
        providers=sso.list_providers(page.db, enabled_only=True),
    )
    if lang in LOCALES:
        response.set_cookie(LANG_COOKIE, lang, max_age=365 * 86400, samesite="lax")
    return response


@router.post("/login", response_class=HTMLResponse)
def login(
    page: WebPage,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
) -> Response:
    settings = page.settings
    throttle: LoginThrottle = page.request.app.state.login_throttle
    address = page.request.client.host if page.request.client else "?"
    account = accounts.login_key(page.db, username)
    wait = throttle.wait(account, address)
    if wait:
        log.warning("Sign-in blocked (too many attempts): %s from %s", account, address)
        return page.render(
            "login.html",
            status_code=429,
            next_url=safe_next(next),
            username=username,
            error="Too many attempts: try again in {minutes} min.",
            error_values={"minutes": -(-wait // 60)},
            providers=sso.list_providers(page.db, enabled_only=True),
        )
    user = accounts.authenticate(page.db, username, password)
    if user is None or not _same_origin(page.request, settings):
        throttle.failed(account, address)
        log.info("Sign-in failed: %s from %s", account, address)
        return page.render(
            "login.html",
            status_code=401,
            next_url=safe_next(next),
            username=username,
            error="Incorrect username or password.",
            providers=sso.list_providers(page.db, enabled_only=True),
        )
    throttle.succeeded(account)
    return sign_in(page, user, safe_next(next))


@router.post("/logout")
def logout(page: WebPage, csrf: Csrf, next: Annotated[str, Form()] = "") -> Response:
    page.check_csrf(csrf)
    if token := page.request.cookies.get(SESSION_COOKIE):
        accounts.close_web_session(page.db, token)
    target = f"/login?next={quote(safe_next(next))}" if next else "/login"
    response = RedirectResponse(target, status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response
