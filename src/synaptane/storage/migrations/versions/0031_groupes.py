"""Groups of members: roles on several spaces at once

A group belongs to an organization (name unique there, whatever the case) and gathers some of
its members; it has a role (reader or contributor) on some of its spaces, restricted ones
included. Composite foreign keys keep everything within the organization: a group member is a
row of `org_members` (leaving the organization removes them from its groups), a group's role is
on a space of the same organization (never another's). Deleting the group, the space or the
organization removes what depends on it. No existing row changes: nobody's access changes.

Revision ID: 0031
Revises: 0030
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(op.f("uq_spaces_id"), "spaces", ["id", "org_id"])
    op.create_table(
        "groups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_groups_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_groups")),
        sa.UniqueConstraint("id", "org_id", name=op.f("uq_groups_id")),
    )
    # Unique name in the organization, whatever the case: also holds against two creations at
    # the same time, which the service's check alone would let through.
    op.create_index(
        "uq_groups_org_id_lower_name",
        "groups",
        ["org_id", sa.text("lower(name)")],
        unique=True,
    )
    op.create_table(
        "group_members",
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id", "org_id"],
            ["groups.id", "groups.org_id"],
            name=op.f("fk_group_members_group_id_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["org_members.org_id", "org_members.user_id"],
            name=op.f("fk_group_members_org_id_org_members"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("group_id", "user_id", name=op.f("pk_group_members")),
    )
    op.create_index(op.f("ix_group_members_user_id"), "group_members", ["user_id"], unique=False)
    op.create_table(
        "group_space_roles",
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('reader', 'contributor')", name=op.f("ck_group_space_roles_role")
        ),
        sa.ForeignKeyConstraint(
            ["group_id", "org_id"],
            ["groups.id", "groups.org_id"],
            name=op.f("fk_group_space_roles_group_id_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["space_id", "org_id"],
            ["spaces.id", "spaces.org_id"],
            name=op.f("fk_group_space_roles_space_id_spaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("group_id", "space_id", name=op.f("pk_group_space_roles")),
    )
    op.create_index(
        op.f("ix_group_space_roles_space_id"), "group_space_roles", ["space_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_group_space_roles_space_id"), table_name="group_space_roles")
    op.drop_table("group_space_roles")
    op.drop_index(op.f("ix_group_members_user_id"), table_name="group_members")
    op.drop_table("group_members")
    op.drop_index("uq_groups_org_id_lower_name", table_name="groups")
    op.drop_table("groups")
    op.drop_constraint(op.f("uq_spaces_id"), "spaces", type_="unique")
