"""quality of each tidy-up pass (metrics measured without AI)

Revision ID: 0019
Revises: 0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # Nullable: earlier passes have no measurement, and the log shows them that way.
    op.add_column("tidy_runs", sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text())))


def downgrade() -> None:
    op.drop_column("tidy_runs", "metrics")
