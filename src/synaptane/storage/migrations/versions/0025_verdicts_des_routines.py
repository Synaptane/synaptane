"""Verdicts on the problems reported by agent routines

What became of an item a routine deposited (not a problem, removed, handled by the tidy-up),
bound to the state of its pages: the routine does not report it again while they stay the
same.

Revision ID: 0025
Revises: 0024
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "finding_verdicts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("origin", sa.String(length=200), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("pages", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('dismissed', 'deleted', 'filed')", name=op.f("ck_finding_verdicts_kind")
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"],
            ["users.id"],
            name=op.f("fk_finding_verdicts_decided_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["spaces.id"],
            name=op.f("fk_finding_verdicts_space_id_spaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_finding_verdicts")),
    )
    op.create_index(
        "ix_finding_verdicts_space_id_decided_at",
        "finding_verdicts",
        ["space_id", "decided_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_finding_verdicts_space_id_decided_at", table_name="finding_verdicts")
    op.drop_table("finding_verdicts")
