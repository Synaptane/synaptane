"""REST API: groups of an organization's members, and their roles on its spaces.

A group is referred to by its identifier (`id`, from `GET /organizations/{org}/groups`): its
name can change. Managing groups is for the organization's administrators; whoever reads a
space sees the groups that access it (`GET …/spaces/{space}/groups`).
"""

from uuid import UUID

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from synaptane.core.access import SpaceRole
from synaptane.services import Actor, accounts, groups, organizations
from synaptane.storage.models import Group

from .routes import CurrentActor, DbSession, SpaceRoleIn, find_space

router = APIRouter(prefix="/api/v1")


class GroupSpaceOut(BaseModel):
    space: str = Field(description="Slug of the space, in the group's organization")
    name: str
    restricted: bool
    role: SpaceRole


class GroupMemberOut(BaseModel):
    username: str
    display_name: str


class GroupOut(BaseModel):
    id: UUID
    name: str
    description: str
    members: list[GroupMemberOut]
    spaces: list[GroupSpaceOut] = Field(description="Spaces the group has a role on")


class GroupCreate(BaseModel):
    name: str
    description: str = ""


class GroupUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class SpaceGroupOut(BaseModel):
    id: UUID
    name: str
    role: SpaceRole
    members: list[GroupMemberOut]


def _out(session: Session, actor: Actor, group: Group) -> GroupOut:
    return GroupOut(
        id=group.id,
        name=group.name,
        description=group.description,
        members=[
            GroupMemberOut(username=u.username, display_name=u.display_name)
            for u in groups.list_members(session, actor, group)
        ],
        spaces=[
            GroupSpaceOut(space=s.slug, name=s.name, restricted=s.restricted, role=role)
            for s, role in groups.list_spaces(session, actor, group)
        ],
    )


def _group(session: Session, actor: Actor, org: str, group: str) -> Group:
    found = organizations.get_organization(session, actor, org)
    return groups.get_group(session, actor, found, group)


@router.get("/organizations/{org}/groups", tags=["groups"])
def list_groups(org: str, session: DbSession, actor: CurrentActor) -> list[GroupOut]:
    found = organizations.get_organization(session, actor, org)
    return [_out(session, actor, g.group) for g in groups.list_groups(session, actor, found)]


@router.post("/organizations/{org}/groups", status_code=status.HTTP_201_CREATED, tags=["groups"])
def create_group(org: str, body: GroupCreate, session: DbSession, actor: CurrentActor) -> GroupOut:
    found = organizations.get_organization(session, actor, org)
    group = groups.create_group(session, actor, found, name=body.name, description=body.description)
    return _out(session, actor, group)


@router.get("/organizations/{org}/groups/{group}", tags=["groups"])
def get_group(org: str, group: str, session: DbSession, actor: CurrentActor) -> GroupOut:
    return _out(session, actor, _group(session, actor, org, group))


@router.patch("/organizations/{org}/groups/{group}", tags=["groups"])
def update_group(
    org: str, group: str, body: GroupUpdate, session: DbSession, actor: CurrentActor
) -> GroupOut:
    found = _group(session, actor, org, group)
    groups.update_group(session, actor, found, **body.model_dump(exclude_unset=True))
    return _out(session, actor, found)


@router.delete(
    "/organizations/{org}/groups/{group}", status_code=status.HTTP_204_NO_CONTENT, tags=["groups"]
)
def delete_group(org: str, group: str, session: DbSession, actor: CurrentActor) -> Response:
    groups.delete_group(session, actor, _group(session, actor, org, group))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/organizations/{org}/groups/{group}/members", tags=["groups"])
def list_group_members(
    org: str, group: str, session: DbSession, actor: CurrentActor
) -> list[GroupMemberOut]:
    found = _group(session, actor, org, group)
    return [
        GroupMemberOut(username=u.username, display_name=u.display_name)
        for u in groups.list_members(session, actor, found)
    ]


@router.put("/organizations/{org}/groups/{group}/members/{username}", tags=["groups"])
def add_group_member(
    org: str, group: str, username: str, session: DbSession, actor: CurrentActor
) -> GroupMemberOut:
    found = _group(session, actor, org, group)
    user = accounts.get_user(session, username)
    groups.add_member(session, actor, found, user)
    return GroupMemberOut(username=user.username, display_name=user.display_name)


@router.delete(
    "/organizations/{org}/groups/{group}/members/{username}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["groups"],
)
def remove_group_member(
    org: str, group: str, username: str, session: DbSession, actor: CurrentActor
) -> Response:
    found = _group(session, actor, org, group)
    groups.remove_member(session, actor, found, accounts.get_user(session, username))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/organizations/{org}/groups/{group}/spaces", tags=["groups"])
def list_group_spaces(
    org: str, group: str, session: DbSession, actor: CurrentActor
) -> list[GroupSpaceOut]:
    found = _group(session, actor, org, group)
    return [
        GroupSpaceOut(space=s.slug, name=s.name, restricted=s.restricted, role=role)
        for s, role in groups.list_spaces(session, actor, found)
    ]


@router.put("/organizations/{org}/groups/{group}/spaces/{space}", tags=["groups"])
def set_group_space_role(
    org: str, group: str, space: str, body: SpaceRoleIn, session: DbSession, actor: CurrentActor
) -> GroupSpaceOut:
    found = _group(session, actor, org, group)
    target = find_space(session, actor, org, space)
    groups.set_space_role(session, actor, found, target, body.role)
    return GroupSpaceOut(
        space=target.slug, name=target.name, restricted=target.restricted, role=body.role
    )


@router.delete(
    "/organizations/{org}/groups/{group}/spaces/{space}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["groups"],
)
def remove_group_space_role(
    org: str, group: str, space: str, session: DbSession, actor: CurrentActor
) -> Response:
    found = _group(session, actor, org, group)
    groups.remove_space_role(session, actor, found, find_space(session, actor, org, space))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/organizations/{org}/spaces/{space}/groups", tags=["spaces"])
def list_space_groups(
    org: str, space: str, session: DbSession, actor: CurrentActor
) -> list[SpaceGroupOut]:
    """The groups with a role on the space, and their members (whoever reads the space)."""
    found = find_space(session, actor, org, space)
    return [
        SpaceGroupOut(
            id=entry.group.id,
            name=entry.group.name,
            role=entry.role,
            members=[
                GroupMemberOut(username=u.username, display_name=u.display_name)
                for u in entry.members
            ],
        )
        for entry in groups.on_space(session, actor, found)
    ]
