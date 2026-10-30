"""Pages reported as outdated by their readers

A reader cannot write to a page: the report lives in the database, open until the page is
verified, the report dismissed by a contributor or withdrawn by its author.

Revision ID: 0024
Revises: 0023
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "page_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("reported_by", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
        sa.Column("resolution", sa.String(length=16), nullable=True),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution IN ('verified', 'dismissed', 'withdrawn')",
            name=op.f("ck_page_reports_resolution"),
        ),
        sa.ForeignKeyConstraint(
            ["reported_by"],
            ["users.id"],
            name=op.f("fk_page_reports_reported_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"],
            ["users.id"],
            name=op.f("fk_page_reports_resolved_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["spaces.id"],
            name=op.f("fk_page_reports_space_id_spaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_page_reports")),
    )
    op.create_index(
        "ix_page_reports_open",
        "page_reports",
        ["space_id", "path"],
        unique=False,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.create_index(
        op.f("ix_page_reports_reported_by"), "page_reports", ["reported_by"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_page_reports_reported_by"), table_name="page_reports")
    op.drop_index(
        "ix_page_reports_open",
        table_name="page_reports",
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.drop_table("page_reports")
