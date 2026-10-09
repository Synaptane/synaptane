"""tidy-up cost and budget

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("org_ai_settings", sa.Column("price_input", sa.Numeric(10, 4), nullable=True))
    op.add_column("org_ai_settings", sa.Column("price_output", sa.Numeric(10, 4), nullable=True))
    op.add_column("org_ai_settings", sa.Column("monthly_budget", sa.Numeric(10, 2), nullable=True))
    op.add_column(
        "org_ai_settings",
        sa.Column("budget_alert", sa.String(length=16), server_default="", nullable=False),
    )
    op.add_column(
        "tidy_runs", sa.Column("cached_tokens", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("tidy_runs", sa.Column("cost", sa.Numeric(10, 4), nullable=True))


def downgrade() -> None:
    op.drop_column("tidy_runs", "cost")
    op.drop_column("tidy_runs", "cached_tokens")
    op.drop_column("org_ai_settings", "budget_alert")
    op.drop_column("org_ai_settings", "monthly_budget")
    op.drop_column("org_ai_settings", "price_output")
    op.drop_column("org_ai_settings", "price_input")
