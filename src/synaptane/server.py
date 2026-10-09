"""Application assembly: web, REST, static files, errors and security headers."""

import json
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from synaptane import __version__
from synaptane.api.content import router as content_router
from synaptane.api.health import router as health_router
from synaptane.api.hooks import router as hooks_router
from synaptane.api.integrations import router as integrations_router
from synaptane.api.routes import api_text
from synaptane.api.routes import router as api_router
from synaptane.api.routines import router as routines_router
from synaptane.api.tidy import router as tidy_router
from synaptane.api.vocation import router as vocation_router
from synaptane.config import Settings
from synaptane.core.errors import SynaptaneError, unavailable
from synaptane.core.security import SecretBox
from synaptane.core.throttle import LoginThrottle, RateLimit
from synaptane.git.repo import GitError
from synaptane.mcp.server import build_mcp, transport_security
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.web import (
    account_pages,
    admin_pages,
    health_pages,
    home_pages,
    inbox_pages,
    invite_pages,
    login_pages,
    oauth_pages,
    org_pages,
    pages,
    routine_pages,
    space_pages,
    sso_pages,
    tidy_pages,
    vocation_pages,
    wiki,
)
from synaptane.web.i18n import translator

log = logging.getLogger("synaptane.server")
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)


def load_secret_box(settings: Settings) -> SecretBox:
    return SecretBox(settings.key_file.read_bytes())


def create_app(settings: Settings, database: Database | None = None) -> FastAPI:
    database = database or Database(settings.database_url)
    box = load_secret_box(settings)
    workspace = Workspace.from_settings(settings, box)
    mcp = build_mcp(settings, database, workspace)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    app = FastAPI(
        lifespan=lifespan,
        title="Synaptane",
        version=__version__,
        description="Synaptane REST API. Authentication: `Authorization: Bearer syn_…`.",
        docs_url="/api/docs" if settings.api_docs else None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json" if settings.api_docs else None,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.secret_box = box
    app.state.workspace = workspace
    app.state.login_throttle = LoginThrottle()

    app.include_router(api_router)
    app.include_router(content_router)
    app.include_router(tidy_router)
    app.include_router(vocation_router)
    app.include_router(routines_router)
    app.include_router(health_router)
    app.include_router(integrations_router)
    app.include_router(hooks_router)
    # Web pages, outside the OpenAPI schema. Order matters: first routes, first served.
    app.include_router(login_pages.router, include_in_schema=False)
    app.include_router(home_pages.router, include_in_schema=False)
    app.include_router(org_pages.router, include_in_schema=False)
    app.include_router(space_pages.router, include_in_schema=False)
    app.include_router(admin_pages.router, include_in_schema=False)
    app.include_router(account_pages.router, include_in_schema=False)
    app.include_router(wiki.router, include_in_schema=False)
    app.include_router(oauth_pages.router, include_in_schema=False)
    app.include_router(inbox_pages.router, include_in_schema=False)
    app.include_router(tidy_pages.router, include_in_schema=False)
    app.include_router(vocation_pages.router, include_in_schema=False)
    app.include_router(routine_pages.router, include_in_schema=False)
    app.include_router(health_pages.router, include_in_schema=False)
    app.include_router(sso_pages.router, include_in_schema=False)
    app.include_router(invite_pages.router, include_in_schema=False)
    app.mount(
        "/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static"
    )

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        """The process responds (liveness)."""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> Response:
        """Ready to serve: the database responds (Docker healthcheck, monitoring)."""
        try:
            with database.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as error:  # noqa: BLE001 — the "unavailable" state is the answer
            return JSONResponse({"status": "unavailable", "error": type(error).__name__}, 503)
        return JSONResponse({"status": "ok"})

    @app.exception_handler(SynaptaneError)
    def business_error(request: Request, error: SynaptaneError) -> Response:
        status = pages.ERROR_STATUS[error.code]
        if request.url.path.startswith("/api/"):
            # `error`: stable code; `message`: in the caller's language (Accept-Language).
            message = api_text(request, error.template, **error.params)
            return JSONResponse({"error": error.code, "message": message}, status)
        return error_page(request, status, error.template, **error.params)

    @app.exception_handler(PoolTimeout)
    def saturated(request: Request, _: PoolTimeout) -> Response:
        """Every database connection is taken: "retry", without waiting 30 s."""
        log.warning("Connection pool exhausted: %s %s", request.method, request.url.path)
        response = business_error(
            request, unavailable("The server is very busy: please try again in a moment.")
        )
        response.headers["Retry-After"] = "5"
        return response

    @app.exception_handler(OperationalError)
    @app.exception_handler(InterfaceError)
    def database_down(request: Request, error: Exception) -> Response:
        """Database unreachable (restart, outage): 503 "retry", not a plain-text 500."""
        log.warning(
            "Database unreachable: %s %s: %s",
            request.method,
            request.url.path,
            str(error).splitlines()[0][:200],
        )
        response = business_error(
            request,
            unavailable("The database is temporarily unreachable: please retry."),
        )
        response.headers["Retry-After"] = "10"
        return response

    @app.exception_handler(GitError)
    def git_failure(request: Request, error: GitError) -> Response:
        """Unexpected Git failure in a request: readable 503, details in the log."""
        log.warning("Git: %s %s: %s", request.method, request.url.path, error)
        return business_error(
            request,
            unavailable(
                "The space's repository is temporarily unavailable: try again in a moment."
            ),
        )

    @app.exception_handler(pages.PasswordChangeRequired)
    def password_change_required(request: Request, _: pages.PasswordChangeRequired) -> Response:
        response = RedirectResponse("/account/security", status_code=303)
        text = translator(pages.visitor_locale(request))(
            "Welcome! Choose your own password to continue."
        )
        response.set_cookie(
            pages.FLASH_COOKIE,
            json.dumps({"kind": "warning", "text": text}),
            max_age=60,
            httponly=True,
            samesite="lax",
        )
        return response

    @app.exception_handler(pages.LoginRequired)
    def login_required(request: Request, _: pages.LoginRequired) -> Response:
        return pages.login_redirect(request)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith("/static/") and request.query_params.get("v"):
            # Address versioned by the content hash: the browser keeps the file.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        html = response.headers.get("content-type", "").startswith("text/html")
        if html and request.url.path != "/api/docs":  # Swagger UI loads its scripts inline
            response.headers.setdefault("Content-Security-Policy", CSP)
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, error: StarletteHTTPException) -> Response:
        """404, 405…: the application's error page (JSON for the API)."""
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "http", "message": error.detail}, error.status_code)
        message = {404: "Page not found.", 405: "This action is not possible on this page."}.get(
            error.status_code, "An error occurred."
        )
        return error_page(request, error.status_code, message)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> Response:
        """Missing or malformed parameter or form field: the application's page (the API
        keeps FastAPI's detailed 422 response)."""
        if request.url.path.startswith("/api/"):
            return await request_validation_exception_handler(request, error)
        return error_page(request, 400, "Invalid request: a field is missing or malformed.")

    @app.exception_handler(Exception)
    def internal_error(request: Request, error: Exception) -> Response:
        """Unexpected error: the application's page with a reference, details in the log."""
        ref = secrets.token_hex(4)
        log.error(
            "Internal error (ref. %s): %s %s",
            ref,
            request.method,
            request.url.path,
            exc_info=error,
        )
        if request.url.path.startswith("/api/"):
            message = api_text(request, "Internal error.")
            return JSONResponse({"error": "internal", "message": message, "ref": ref}, 500)
        return error_page(
            request,
            500,
            "Something went wrong (reference {ref}). It has been recorded in the server log.",
            ref=ref,
        )

    # MCP and OAuth (SDK): /mcp, /authorize, /token, /register, /revoke, /.well-known/…
    # Mounted last: Synaptane's routes keep priority. Any other unknown address gets the
    # application's error page, not the SDK's "Not Found".
    app.mount(
        "/",
        _OnlyPaths(
            mcp.streamable_http_app(
                stateless_http=True,
                json_response=True,
                transport_security=transport_security(settings),
            ),
            ("/mcp", "/authorize", "/token", "/register", "/revoke", "/.well-known/"),
        ),
    )
    return app


def error_page(request: Request, status: int, template: str, **params: object) -> Response:
    """Error page in the visitor's language (cookie, else browser)."""
    locale = pages.visitor_locale(request)
    return pages.templates.TemplateResponse(
        request,
        "error.html",
        {
            "_": translator(locale),
            "locale": locale,
            "status": status,
            "message": translator(locale)(template, **params),
        },
        status_code=status,
    )


REGISTRATIONS_PER_HOUR = 20  # dynamic OAuth client registrations per IP address


class _OnlyPaths:
    """Only lets the mounted application's own paths through to it, and limits dynamic OAuth
    client registrations (open to anyone) per address."""

    def __init__(self, app: ASGIApp, prefixes: tuple[str, ...]) -> None:
        self.app, self.prefixes = app, prefixes
        self.registrations = RateLimit(REGISTRATIONS_PER_HOUR, 3600)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if path == "/register" and scope.get("method") == "POST":
            address = (scope.get("client") or ("?", 0))[0]
            if not self.registrations.allow(str(address)):
                refused = JSONResponse(
                    {
                        "error": "too_many_requests",
                        "error_description": "Too many registrations from this address.",
                    },
                    status_code=429,
                    headers={"Retry-After": "3600"},
                )
                await refused(scope, receive, send)
                return
        if scope["type"] != "http" or path.startswith(self.prefixes):
            await self.app(scope, receive, send)
            return
        response = error_page(Request(scope, receive), 404, "Page not found.")
        await response(scope, receive, send)
