"""webhook circuit breaker: consecutive failures

Revision ID: 0017
Revises: 0016
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "webhooks", sa.Column("failures", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("webhooks", sa.Column("failing_since", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("webhooks", "failing_since")
    op.drop_column("webhooks", "failures")
