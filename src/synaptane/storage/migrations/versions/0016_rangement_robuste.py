"""robust tidy-up: unprocessed pages and lasting sync errors

Revision ID: 0016
Revises: 0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # Changed page shown to an interrupted pass: neither filed nor left.
    op.drop_constraint(op.f("ck_change_marks_status"), "change_marks", type_="check")
    op.create_check_constraint(
        op.f("ck_change_marks_status"), "change_marks", "status IN ('done', 'left', 'pending')"
    )
    # Start of the current sync error streak (alert if it lasts).
    op.add_column(
        "spaces", sa.Column("sync_error_since", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("spaces", "sync_error_since")
    # Attempt counters are lost: these pages simply go back to being examined.
    op.execute("DELETE FROM change_marks WHERE status = 'pending'")
    op.drop_constraint(op.f("ck_change_marks_status"), "change_marks", type_="check")
    op.create_check_constraint(
        op.f("ck_change_marks_status"), "change_marks", "status IN ('done', 'left')"
    )
