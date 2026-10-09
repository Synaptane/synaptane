"""subscription quota in the tidy-up log

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tidy_runs", sa.Column("quota", postgresql.JSONB(astext_type=sa.Text())))


def downgrade() -> None:
    op.drop_column("tidy_runs", "quota")
