"""Invitations into groups: the person joins them when accepting

An invitation can name groups of its organization: on acceptance, the person becomes a member
of the organization, then of these groups. A composite foreign key keeps them within the
invitation's organization (never a group of another one, see 0031). Deleting the group or the
invitation (revoked, renewed, organization deleted) removes the link: a group deleted before
the acceptance is simply not joined. No existing row changes: nobody's access changes.

Revision ID: 0032
Revises: 0031
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(op.f("uq_invitations_id"), "invitations", ["id", "org_id"])
    op.create_table(
        "invitation_groups",
        sa.Column("invitation_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id", "org_id"],
            ["groups.id", "groups.org_id"],
            name=op.f("fk_invitation_groups_group_id_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invitation_id", "org_id"],
            ["invitations.id", "invitations.org_id"],
            name=op.f("fk_invitation_groups_invitation_id_invitations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("invitation_id", "group_id", name=op.f("pk_invitation_groups")),
    )
    op.create_index(
        op.f("ix_invitation_groups_group_id"), "invitation_groups", ["group_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_invitation_groups_group_id"), table_name="invitation_groups")
    op.drop_table("invitation_groups")
    op.drop_constraint(op.f("uq_invitations_id"), "invitations", type_="unique")
