"""First steps of a space: what is left to do before its first tidy-up.

Worked out from the space itself, with nothing to store: a step is done when the thing is
there. The list goes away after the first tidy-up that did something, and is never shown for a
space whose tidy-up is turned off.
"""

from dataclasses import dataclass

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from synaptane.core.access import Action, can
from synaptane.core.errors import SynaptaneError
from synaptane.core.security import SecretBox
from synaptane.services import Actor, ai_settings, content, vocation
from synaptane.services.workspace import Workspace
from synaptane.storage.models import Space, TidyRun


@dataclass(frozen=True)
class Step:
    key: str  # repository, vocation, ai, content, tidy
    done: bool
    url: str = ""  # empty: this person cannot do it


def _space_url(space: Space) -> str:
    return f"/o/{space.organization.slug}/{space.slug}"


def steps(
    session: Session, actor: Actor, ws: Workspace, box: SecretBox, space: Space
) -> list[Step] | None:
    """The steps of a space, None when there is nothing to show this person."""
    if not space.tidy_enabled or not can(
        actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id
    ):
        return None
    tidied = exists().where(TidyRun.space_id == space.id, TidyRun.status == "done")
    if session.scalar(select(tidied)):
        return None
    admin = can(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    base = _space_url(space)
    org = f"/o/{space.organization.slug}"
    purpose = vocation.space_vocation(actor, ws, space)
    try:
        files = content.list_files(actor, ws, space)
    except SynaptaneError:  # not synced yet
        files = None
    return [
        Step("repository", files is not None, f"{base}/-/git" if admin else ""),
        Step("vocation", bool(purpose and purpose.strip()), f"{base}/-/vocation"),
        Step(
            "ai",
            ai_settings.config_for(session, box, space) is not None,
            f"{org}/-/ai" if admin else "",
        ),
        Step("content", bool(files), f"{base}/-/import"),
        Step("tidy", False, f"{base}/tidy"),
    ]
