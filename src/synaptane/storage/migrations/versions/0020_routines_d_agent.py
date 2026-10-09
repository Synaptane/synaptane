"""scheduled agent routines per space, and their log

Revision ID: 0020
Revises: 0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "routines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("template", sa.String(length=32), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("frequency", sa.String(length=8), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=True),
        sa.Column("monthday", sa.Integer(), nullable=True),
        sa.Column("at", sa.String(length=5), nullable=False),
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("output_page", sa.Text(), nullable=False),
        sa.Column("output_inbox", sa.Boolean(), nullable=False),
        sa.Column("output_email", sa.String(length=16), nullable=False),
        sa.Column("output_webhook", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_date", sa.Date(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested", sa.String(length=16), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "frequency IN ('daily', 'weekly', 'monthly')", name=op.f("ck_routines_frequency")
        ),
        sa.CheckConstraint(
            "weekday IS NULL OR weekday BETWEEN 0 AND 6", name=op.f("ck_routines_weekday")
        ),
        sa.CheckConstraint(
            "monthday IS NULL OR monthday BETWEEN 1 AND 28", name=op.f("ck_routines_monthday")
        ),
        sa.CheckConstraint(
            "output_email IN ('', 'admins', 'members')", name=op.f("ck_routines_output_email")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_routines_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_routines_requested_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["spaces.id"],
            name=op.f("fk_routines_space_id_spaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routines")),
        sa.UniqueConstraint("space_id", "name", name=op.f("uq_routines_space_id")),
    )
    op.create_table(
        "routine_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("routine_id", sa.Uuid(), nullable=True),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("report", sa.Text(), nullable=False),
        sa.Column("outputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("commit", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_tokens", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("quota", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'done', 'failed')", name=op.f("ck_routine_runs_status")
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_routine_runs_requested_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["routine_id"],
            ["routines.id"],
            name=op.f("fk_routine_runs_routine_id_routines"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["spaces.id"],
            name=op.f("fk_routine_runs_space_id_spaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routine_runs")),
    )
    op.create_index(
        op.f("ix_routine_runs_routine_id"), "routine_runs", ["routine_id"], unique=False
    )
    op.create_index(
        "ix_routine_runs_space_started", "routine_runs", ["space_id", "started_at"], unique=False
    )
    # Preference separate from tidy-up notices: a team summary is not a notice about one's
    # own notes. Enabled by default, recalled in each email (like notify_tidy).
    op.add_column(
        "users",
        sa.Column("notify_routines", sa.Boolean(), server_default=sa.true(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "notify_routines")
    op.drop_index("ix_routine_runs_space_started", table_name="routine_runs")
    op.drop_index(op.f("ix_routine_runs_routine_id"), table_name="routine_runs")
    op.drop_table("routine_runs")
    op.drop_table("routines")
