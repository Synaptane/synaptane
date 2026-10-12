"""continuous tidy-up (delay per space) and notice to the dropper (per-account preference)

Revision ID: 0018
Revises: 0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("spaces", sa.Column("tidy_delay", sa.Integer(), nullable=True))
    op.add_column(
        "users",
        sa.Column("notify_tidy", sa.Boolean(), server_default=sa.true(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "notify_tidy")
    op.drop_column("spaces", "tidy_delay")
