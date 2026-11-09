"""Nightly wiki maintenance: the tidy-up also fixes the problems people introduced

`spaces.tidy_maintain` turns it on per space (on by default: it only costs when there are
problems); `spaces.maintain_watermark` is the commit the problems are counted from: the last
synchronized head (no catching up on the past), or, for a space never synchronized, its first
head; `spaces.maintain_attempts` counts the runs interrupted in a row before the agent decided.
`maintenance_marks` remembers the problems the agent left, with their reason and the commit
the decision was made on. No existing column changes.

Revision ID: 0035
Revises: 0034
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "spaces",
        sa.Column("tidy_maintain", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column("spaces", sa.Column("maintain_watermark", sa.String(length=64), nullable=True))
    op.add_column(
        "spaces",
        sa.Column("maintain_attempts", sa.Integer(), server_default="0", nullable=False),
    )
    # From the last synchronized head: the edit that triggers the next sync is not the past.
    op.execute("UPDATE spaces SET maintain_watermark = remote_head")
    op.create_table(
        "maintenance_marks",
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("page", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("until", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["spaces.id"],
            name=op.f("fk_maintenance_marks_space_id_spaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("space_id", "key", name=op.f("pk_maintenance_marks")),
    )


def downgrade() -> None:
    op.drop_table("maintenance_marks")
    op.drop_column("spaces", "maintain_attempts")
    op.drop_column("spaces", "maintain_watermark")
    op.drop_column("spaces", "tidy_maintain")
