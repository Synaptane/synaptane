"""Incoming webhook from forges (GitHub, GitLab, Gitea): triggers a space's sync.

No session or token: the call is authenticated by the space's webhook secret.
An unknown space and a wrong signature give the same response.
"""

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool

from synaptane.core.security import SecretBox
from synaptane.services import git_sync
from synaptane.storage.db import Database

router = APIRouter()
MAX_BODY = 25 * 1024 * 1024  # maximum size of a GitHub event


@router.post("/hooks/git/{org_slug}/{space_slug}", include_in_schema=False)
async def git_webhook(org_slug: str, space_slug: str, request: Request) -> Response:
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        return Response(status_code=413)
    chunks, size = [], 0
    async for chunk in request.stream():  # streamed, cut off beyond MAX_BODY
        size += len(chunk)
        if size > MAX_BODY:
            return Response(status_code=413)
        chunks.append(chunk)
    body = b"".join(chunks)
    headers = {key.lower(): value for key, value in request.headers.items()}
    database: Database = request.app.state.database
    box: SecretBox = request.app.state.secret_box

    def accept() -> bool:
        with database.session() as session:
            return git_sync.accept_webhook(session, box, org_slug, space_slug, headers, body)

    accepted = await run_in_threadpool(accept)
    return Response(status_code=202 if accepted else 401)
