"""Spaces (one Git repository each) and their members."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from synaptane.core import names
from synaptane.core.access import Action, SpaceRole, can, require
from synaptane.core.errors import conflict, invalid, not_found
from synaptane.services import Actor, audit
from synaptane.services.freshness import clean_period
from synaptane.services.organizations import ensure_member
from synaptane.services.secrets import delete_space_secret
from synaptane.storage.models import Organization, Space, SpaceMember, User


def _validate_repo_url(value: str | None) -> str | None:
    """`file://` is only accepted in development: synchronization refuses it otherwise."""
    value = (value or "").strip()
    if not value:
        return None
    if not value.startswith(("https://", "ssh://", "git@", "file://")) or any(
        c.isspace() for c in value
    ):
        raise invalid("Invalid repository URL: https://…, ssh://… or git@host:path.")
    if value.startswith("https://") and "@" in value.split("/")[2]:
        raise invalid("Do not put credentials in the URL: enter them under “Repository access”.")
    return value


def _validate_branch(value: str) -> str:
    value = value.strip()
    if not value or value.startswith(("-", "/")) or ".." in value or " " in value:
        raise invalid("Invalid branch name.")
    return value


def list_spaces(session: Session, actor: Actor, org: Organization) -> list[Space]:
    require(actor.access, Action.VIEW_ORG, org_id=org.id)
    query = select(Space).where(Space.org_id == org.id).order_by(Space.name)
    if not actor.access.is_org_admin(org.id):
        query = query.where(Space.id.in_(actor.access.space_roles))
    return list(session.scalars(query))


def list_all_visible_spaces(session: Session, actor: Actor) -> list[Space]:
    """All the spaces the account can read, across organizations."""
    query = select(Space).join(Space.organization).order_by(Organization.name, Space.name)
    if not actor.access.is_superadmin:
        admin_orgs = [o for o in actor.access.org_roles if actor.access.is_org_admin(o)]
        query = query.where(Space.id.in_(actor.access.space_roles) | Space.org_id.in_(admin_orgs))
    return list(session.scalars(query))


def get_space(session: Session, actor: Actor, org: Organization, slug: str) -> Space:
    space = session.scalar(select(Space).where(Space.org_id == org.id, Space.slug == slug))
    if space is None or not can(actor.access, Action.READ_SPACE, org_id=org.id, space_id=space.id):
        raise not_found("Space not found.")
    return space


def role_in(actor: Actor, space: Space) -> SpaceRole | None:
    """Effective role: an organization admin or the super-admin is a contributor everywhere."""
    if actor.access.is_org_admin(space.org_id):
        return SpaceRole.CONTRIBUTOR
    return actor.access.space_roles.get(space.id)


def create_space(
    session: Session,
    actor: Actor,
    org: Organization,
    *,
    slug: str,
    name: str,
    description: str = "",
    repo_url: str | None = None,
    branch: str = "main",
) -> Space:
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    slug = names.slug(slug)
    if session.scalar(select(Space).where(Space.org_id == org.id, Space.slug == slug)):
        raise conflict("The space “{slug}” already exists in this organization.", slug=slug)
    space = Space(
        org_id=org.id,
        slug=slug,
        name=names.display_name(name),
        description=description.strip(),
        repo_url=_validate_repo_url(repo_url),
        branch=_validate_branch(branch),
    )
    session.add(space)
    session.flush()
    audit(session, actor, "space.created", space=space, slug=slug)
    return space


def update_space(
    session: Session,
    actor: Actor,
    space: Space,
    *,
    name: str | None = None,
    description: str | None = None,
    repo_url: str | None = None,
    branch: str | None = None,
    review_every: str | None = None,
) -> Space:
    """Settings of a space (organization admin). `review_every`: default review period of
    the pages (`6m`, `1y`…; "": never due for review)."""
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    if review_every is not None:
        space.review_every = clean_period(review_every)
    if name is not None:
        space.name = names.display_name(name)
    if description is not None:
        space.description = description.strip()
    if repo_url is not None:
        url = _validate_repo_url(repo_url)
        if url != space.repo_url:
            space.repo_url, space.remote_head, space.sync_status = url, None, "never"
            # Marker and decisions kept: synchronization resets them only if the new
            # repository does not contain the marker (same repository over https then ssh:
            # nothing is lost).
            # The token or key does not follow to another repository: to be entered again.
            if delete_space_secret(session, space.id, "git"):
                audit(session, actor, "space.git_credentials_cleared", space=space)
    if branch is not None:
        new_branch = _validate_branch(branch)
        if new_branch != space.branch:
            space.branch, space.remote_head, space.sync_status = new_branch, None, "never"
    audit(session, actor, "space.updated", space=space)
    return space


def delete_space(session: Session, actor: Actor, space: Space) -> None:
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    audit(session, actor, "space.deleted", org_id=space.org_id, slug=space.slug)
    session.delete(space)


def list_members(session: Session, actor: Actor, space: Space) -> list[tuple[User, SpaceRole]]:
    require(actor.access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id)
    rows = session.execute(
        select(User, SpaceMember.role)
        .join(SpaceMember, SpaceMember.user_id == User.id)
        .where(SpaceMember.space_id == space.id)
        .order_by(User.username)
    )
    return [(user, SpaceRole(role)) for user, role in rows]


def set_member(session: Session, actor: Actor, space: Space, user: User, role: SpaceRole) -> None:
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    ensure_member(session, space.organization, user)
    member = session.get(SpaceMember, (space.id, user.id))
    if member is None:
        session.add(SpaceMember(space_id=space.id, user_id=user.id, role=role.value))
    else:
        member.role = role.value
    audit(session, actor, "space.member_set", space=space, username=user.username, role=role)


def remove_member(session: Session, actor: Actor, space: Space, user: User) -> None:
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    member = session.get(SpaceMember, (space.id, user.id))
    if member is None:
        raise not_found("This account is not a member of the space.")
    session.delete(member)
    audit(session, actor, "space.member_removed", space=space, username=user.username)
