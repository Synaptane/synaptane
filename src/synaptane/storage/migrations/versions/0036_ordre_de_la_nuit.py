"""The order of the night: routines run with the tidy-up, and one report

`routines.night` puts a routine in the night of its space ("before" or "after" the tidy-up, at
its time); "" keeps it at its own time, as every existing routine. `spaces.night_report_email`
and `spaces.night_report_always` say who receives the night's report and whether a quiet
night is reported too; its Slack webhook is a secret (`secrets`), not a column. `night_runs`
keeps each night: its steps in order, what each did, and what the report sent. No existing
value changes.

Revision ID: 0036
Revises: 0035
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "routines", sa.Column("night", sa.String(length=8), server_default="", nullable=False)
    )
    op.create_check_constraint(
        op.f("ck_routines_night"), "routines", "night IN ('', 'before', 'after')"
    )
    op.add_column(
        "spaces",
        sa.Column("night_report_email", sa.String(length=16), server_default="", nullable=False),
    )
    op.add_column(
        "spaces",
        sa.Column("night_report_always", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_spaces_night_report_email"),
        "spaces",
        "night_report_email IN ('', 'admins', 'members')",
    )
    op.create_table(
        "night_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("steps", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done')", name=op.f("ck_night_runs_status")
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["spaces.id"],
            name=op.f("fk_night_runs_space_id_spaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_night_runs")),
        sa.UniqueConstraint("space_id", "day", name=op.f("uq_night_runs_space_id")),
    )


def downgrade() -> None:
    op.drop_table("night_runs")
    op.drop_constraint(op.f("ck_spaces_night_report_email"), "spaces", type_="check")
    op.drop_column("spaces", "night_report_always")
    op.drop_column("spaces", "night_report_email")
    op.drop_constraint(op.f("ck_routines_night"), "routines", type_="check")
    op.drop_column("routines", "night")
