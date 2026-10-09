"""nightly tidy-up

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "org_ai_settings",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("effort", sa.String(length=16), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_org_ai_settings_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("org_id", name=op.f("pk_org_ai_settings")),
    )
    op.create_table(
        "tidy_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("items_done", sa.Integer(), nullable=False),
        sa.Column("items_left", sa.Integer(), nullable=False),
        sa.Column("operations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("commit", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("reverted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reverted_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'done', 'nothing', 'failed')", name=op.f("ck_tidy_runs_status")
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_tidy_runs_requested_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reverted_by"],
            ["users.id"],
            name=op.f("fk_tidy_runs_reverted_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["spaces.id"],
            name=op.f("fk_tidy_runs_space_id_spaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tidy_runs")),
    )
    op.create_index(op.f("ix_tidy_runs_space_id"), "tidy_runs", ["space_id"], unique=False)
    op.add_column(
        "spaces", sa.Column("tidy_enabled", sa.Boolean(), nullable=False, server_default=sa.true())
    )
    op.add_column(
        "spaces",
        sa.Column("tidy_time", sa.String(length=5), nullable=False, server_default="02:00"),
    )
    op.add_column(
        "spaces",
        sa.Column(
            "tidy_timezone", sa.String(length=64), nullable=False, server_default="Europe/Paris"
        ),
    )
    op.add_column("spaces", sa.Column("tidy_last_date", sa.Date(), nullable=True))
    op.add_column("spaces", sa.Column("ai_model", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("spaces", "ai_model")
    op.drop_column("spaces", "tidy_last_date")
    op.drop_column("spaces", "tidy_timezone")
    op.drop_column("spaces", "tidy_time")
    op.drop_column("spaces", "tidy_enabled")
    op.drop_index(op.f("ix_tidy_runs_space_id"), table_name="tidy_runs")
    op.drop_table("tidy_runs")
    op.drop_table("org_ai_settings")
